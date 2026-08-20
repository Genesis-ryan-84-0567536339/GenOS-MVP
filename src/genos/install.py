from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid

from .boundary import BoundaryDecision, BoundaryMode, decide_boundary, get_profile
from .contracts import InstallPlan, InstallRun, Observation, RunState, utc_now
from .redaction import redact
from .state import JsonStateStore


PRODUCT_API_PORT = 17880
RUNTIME_PORT = 17881
MISSION_CONTROL_PORT = 17882
CORE_USER = "genos"
CORE_GROUP = "genos"
CORE_DB = "genos"


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    archive: Path
    git_sha: str
    sha256: str

    def verify(self) -> None:
        if not self.archive.is_file():
            raise InstallError(f"release archive does not exist: {self.archive}")
        actual = sha256_file(self.archive)
        if actual.lower() != self.sha256.lower():
            raise InstallError(f"release checksum mismatch: expected {self.sha256}, got {actual}")
        if not _looks_like_git_sha(self.git_sha):
            raise InstallError("release git SHA must be a 40-character hexadecimal commit id")


@dataclass(frozen=True, slots=True)
class PlannedInstall:
    decision: BoundaryDecision
    plan: InstallPlan
    release: ReleaseArtifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "plan": self.plan.to_dict(),
            "release": {
                "archive": str(self.release.archive),
                "git_sha": self.release.git_sha,
                "sha256": self.release.sha256,
            },
        }


class SystemCommandRunner:
    """Internal typed command runner; never accepts a shell command string."""

    def run(self, argv: list[str], *, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        if not argv:
            raise ValueError("empty command")
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "DEBIAN_FRONTEND": "noninteractive",
        }
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            shell=False,
            env=env,
        )


def build_native_install(
    observations: list[Observation],
    *,
    requested_mode: str | None,
    release: ReleaseArtifact,
    allow_candidate_e2e: bool = False,
) -> PlannedInstall:
    release.verify()
    decision = decide_boundary(
        observations,
        requested_mode,
        allow_candidate_e2e=allow_candidate_e2e,
    )
    steps = [
        {"step_id": "prepare_install_state", "action": "filesystem_state_root"},
        {"step_id": "install_packages", "action": "apt_postgresql_packages"},
        {"step_id": "create_service_identity", "action": "system_user_group"},
        {"step_id": "create_directories", "action": "owned_directories"},
        {"step_id": "stage_release", "action": "verified_release_extract"},
        {"step_id": "write_configuration", "action": "instance_and_environment"},
        {"step_id": "install_systemd_units", "action": "typed_systemd_units"},
        {"step_id": "start_postgresql", "action": "systemd_postgresql"},
        {"step_id": "provision_database", "action": "postgres_role_database"},
        {"step_id": "start_core_services", "action": "systemd_core_services"},
        {"step_id": "verify_local_core", "action": "local_health_gate"},
        {"step_id": "finalize_manifest", "action": "install_manifest"},
    ]
    profile = get_profile(decision.profile_id) if decision.profile_id else None
    canonical = {
        "mode": decision.mode.value if decision.mode else None,
        "profile_id": decision.profile_id,
        "profile_state": profile.state.value if profile else None,
        "release_git_sha": release.git_sha,
        "release_sha256": release.sha256.lower(),
        "steps": steps,
    }
    plan_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    plan = InstallPlan(
        support_class=decision.support_class,
        profile=decision.profile_id or "unselected",
        steps=steps,
        plan_hash=plan_hash,
    )
    return PlannedInstall(decision=decision, plan=plan, release=release)


class NativeProvisioner:
    def __init__(
        self,
        planned: PlannedInstall,
        *,
        state_root: Path = Path("/var/lib/genos"),
        runner: SystemCommandRunner | None = None,
    ) -> None:
        self.planned = planned
        self.runner = runner or SystemCommandRunner()
        self.store = JsonStateStore(state_root)
        self.run_path = state_root / "install-run.json"
        self._completed: set[str] = set()
        self._instance_id: str | None = None

    def execute(self) -> InstallRun:
        decision = self.planned.decision
        if decision.mode is not BoundaryMode.NATIVE:
            raise InstallError("NativeProvisioner only executes native boundary plans")
        if not decision.mutation_allowed:
            raise InstallError(f"mutation blocked by support gate: {decision.state}: {decision.reason}")
        if os.geteuid() != 0:
            raise InstallError("native install requires root privileges after the support gate passes")

        self._load_resume_state()
        run = self._load_or_create_run()
        run.state = RunState.RUNNING
        run.updated_at = utc_now()
        self._persist_run(run)

        try:
            for step in self.planned.plan.steps:
                step_id = str(step["step_id"])
                if step_id in self._completed:
                    continue
                self._execute_step(step_id)
                self._completed.add(step_id)
                run.checkpoint = step_id
                run.updated_at = utc_now()
                run.evidence.append({"step_id": step_id, "state": "PASS", "observed_at": utc_now()})
                self._persist_run(run)
            run.state = RunState.SUCCEEDED
            run.updated_at = utc_now()
            self._persist_run(run)
            return run
        except Exception as exc:
            run.state = RunState.FAILED
            run.updated_at = utc_now()
            run.evidence.append(
                {
                    "state": "FAIL",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "observed_at": utc_now(),
                }
            )
            self._persist_run(run)
            raise

    def _execute_step(self, step_id: str) -> None:
        dispatch = {
            "prepare_install_state": self._prepare_install_state,
            "install_packages": self._install_packages,
            "create_service_identity": self._create_service_identity,
            "create_directories": self._create_directories,
            "stage_release": self._stage_release,
            "write_configuration": self._write_configuration,
            "install_systemd_units": self._install_systemd_units,
            "start_postgresql": self._start_postgresql,
            "provision_database": self._provision_database,
            "start_core_services": self._start_core_services,
            "verify_local_core": self._verify_local_core,
            "finalize_manifest": self._finalize_manifest,
        }
        try:
            action = dispatch[step_id]
        except KeyError as exc:
            raise InstallError(f"unknown typed install step: {step_id}") from exc
        action()

    def _prepare_install_state(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.store.root, 0o700)

    def _install_packages(self) -> None:
        profile = get_profile(self.planned.plan.profile)
        if profile.package_manager != "apt":
            raise InstallError(f"package manager not implemented: {profile.package_manager}")
        if _dpkg_packages_present(self.runner, ["postgresql", "postgresql-client"]):
            return
        self.runner.run(["apt-get", "update"], timeout=300)
        self.runner.run(
            ["apt-get", "install", "-y", "postgresql", "postgresql-client", "ca-certificates"],
            timeout=600,
        )

    def _create_service_identity(self) -> None:
        try:
            grp.getgrnam(CORE_GROUP)
        except KeyError:
            self.runner.run(["groupadd", "--system", CORE_GROUP])
        try:
            pwd.getpwnam(CORE_USER)
        except KeyError:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--gid",
                    CORE_GROUP,
                    "--home-dir",
                    "/var/lib/genos",
                    "--shell",
                    "/usr/sbin/nologin",
                    CORE_USER,
                ]
            )

    def _create_directories(self) -> None:
        uid = pwd.getpwnam(CORE_USER).pw_uid
        gid = grp.getgrnam(CORE_GROUP).gr_gid
        specs = (
            (Path("/etc/genos"), 0, gid, 0o750),
            (Path("/var/lib/genos"), uid, gid, 0o750),
            (Path("/var/log/genos"), uid, gid, 0o750),
            (Path("/opt/genos/releases"), 0, gid, 0o755),
        )
        for path, owner, group, mode in specs:
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, owner, group)
            os.chmod(path, mode)

    def _stage_release(self) -> None:
        release = self.planned.release
        release.verify()
        target = Path("/opt/genos/releases") / release.git_sha
        digest_file = target / ".genos-release-sha256"
        if target.is_dir() and digest_file.is_file():
            if digest_file.read_text(encoding="utf-8").strip() == release.sha256.lower():
                _replace_symlink(Path("/opt/genos/current"), target)
                return
            raise InstallError(f"existing release directory has a different digest: {target}")

        parent = target.parent
        temp = Path(tempfile.mkdtemp(prefix=f".{release.git_sha}.", dir=parent))
        os.chmod(temp, 0o755)
        try:
            _safe_extract_tar(release.archive, temp)
            if not (temp / "src" / "genos" / "__init__.py").is_file():
                raise InstallError("release archive missing src/genos package")
            (temp / ".genos-release-sha256").write_text(release.sha256.lower() + "\n", encoding="utf-8")
            os.rename(temp, target)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
        _replace_symlink(Path("/opt/genos/current"), target)

    def _write_configuration(self) -> None:
        instance_path = Path("/etc/genos/instance-id")
        if instance_path.exists():
            instance_id = instance_path.read_text(encoding="utf-8").strip()
            try:
                uuid.UUID(instance_id)
            except ValueError as exc:
                raise InstallError("existing /etc/genos/instance-id is invalid") from exc
        else:
            instance_id = str(uuid.uuid4())
            _atomic_text(instance_path, instance_id + "\n", mode=0o640)
        self._instance_id = instance_id
        env = (
            f"GENOS_INSTANCE_ID={instance_id}\n"
            "GENOS_STATE_DIR=/var/lib/genos\n"
            f"GENOS_RELEASE_SHA={self.planned.release.git_sha}\n"
        )
        _atomic_text(Path("/etc/genos/genos.env"), env, mode=0o640)
        gid = grp.getgrnam(CORE_GROUP).gr_gid
        os.chown(instance_path, 0, gid)
        os.chown("/etc/genos/genos.env", 0, gid)

    def _install_systemd_units(self) -> None:
        for name, content in _systemd_units().items():
            _atomic_text(Path("/etc/systemd/system") / name, content, mode=0o644)
        self.runner.run(["systemctl", "daemon-reload"])

    def _start_postgresql(self) -> None:
        self.runner.run(["systemctl", "enable", "--now", "postgresql.service"])
        result = self.runner.run(["systemctl", "is-active", "postgresql.service"], check=False)
        if result.returncode != 0 or result.stdout.strip() != "active":
            raise InstallError("postgresql.service did not become active")

    def _provision_database(self) -> None:
        role_check = self.runner.run(
            ["runuser", "-u", "postgres", "--", "psql", "-tAc", "SELECT 1 FROM pg_roles WHERE rolname='genos'"],
            check=False,
        )
        if role_check.stdout.strip() != "1":
            self.runner.run(["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-c", "CREATE ROLE genos LOGIN"])

        db_check = self.runner.run(
            ["runuser", "-u", "postgres", "--", "psql", "-tAc", "SELECT 1 FROM pg_database WHERE datname='genos'"],
            check=False,
        )
        if db_check.stdout.strip() != "1":
            self.runner.run(["runuser", "-u", "postgres", "--", "createdb", "--owner", CORE_USER, CORE_DB])

    def _start_core_services(self) -> None:
        for service in (
            "genos-product-api.service",
            "genos-runtime.service",
            "genos-worker.service",
            "genos-mission-control.service",
        ):
            self.runner.run(["systemctl", "enable", "--now", service])

    def _verify_local_core(self) -> None:
        for role, port in (
            ("product-api", PRODUCT_API_PORT),
            ("runtime", RUNTIME_PORT),
            ("mission-control", MISSION_CONTROL_PORT),
        ):
            payload = _http_json(f"http://127.0.0.1:{port}/health")
            if payload.get("status") != "ok" or payload.get("role") != role:
                raise InstallError(f"{role} health gate failed: {payload}")
        heartbeat = Path("/var/lib/genos/worker/heartbeat.json")
        if not heartbeat.is_file():
            raise InstallError("worker heartbeat missing")
        worker = json.loads(heartbeat.read_text(encoding="utf-8"))
        if worker.get("status") != "ok" or worker.get("role") != "worker":
            raise InstallError("worker heartbeat invalid")
        db = self.runner.run(["runuser", "-u", CORE_USER, "--", "psql", "-d", CORE_DB, "-tAc", "SELECT 1"], check=False)
        if db.returncode != 0 or db.stdout.strip() != "1":
            raise InstallError("PostgreSQL local peer health gate failed")

    def _finalize_manifest(self) -> None:
        instance_id = self._instance_id or Path("/etc/genos/instance-id").read_text(encoding="utf-8").strip()
        manifest = {
            "schema_version": "1.0",
            "state": "READY_LOCAL_CORE",
            "instance_id": instance_id,
            "execution_boundary": "native",
            "profile_id": self.planned.plan.profile,
            "support_class": self.planned.decision.support_class.value,
            "support_evidence": "CANDIDATE_E2E" if self.planned.decision.state == "CANDIDATE_E2E_ONLY" else "VERIFIED_PROFILE",
            "plan_hash": self.planned.plan.plan_hash,
            "release": {
                "git_sha": self.planned.release.git_sha,
                "sha256": self.planned.release.sha256.lower(),
            },
            "services": {
                "product_api": f"http://127.0.0.1:{PRODUCT_API_PORT}/health",
                "runtime": f"http://127.0.0.1:{RUNTIME_PORT}/health",
                "mission_control_health": f"http://127.0.0.1:{MISSION_CONTROL_PORT}/health",
                "mission_control_ui": "NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL",
                "worker": "/var/lib/genos/worker/heartbeat.json",
                "postgresql_database": CORE_DB,
            },
            "updated_at": utc_now(),
        }
        self.store.save_manifest(manifest)
        gid = grp.getgrnam(CORE_GROUP).gr_gid
        os.chown(self.store.manifest_path, 0, gid)
        os.chmod(self.store.manifest_path, 0o640)

    def _load_resume_state(self) -> None:
        manifest = self.store.load_manifest()
        if manifest:
            existing_hash = manifest.get("plan_hash")
            if existing_hash and existing_hash != self.planned.plan.plan_hash:
                raise InstallError("existing GenOS install belongs to a different plan; reconfigure/update is required")
        if not self.run_path.is_file():
            return
        with self.run_path.open("r", encoding="utf-8") as handle:
            payload = redact(json.load(handle))
        existing_run_hash = payload.get("plan_hash")
        if existing_run_hash and existing_run_hash != self.planned.plan.plan_hash:
            raise InstallError("incomplete install run belongs to a different plan; explicit recovery is required")
        for item in payload.get("evidence", []):
            if item.get("state") == "PASS" and item.get("step_id"):
                self._completed.add(str(item["step_id"]))

    def _load_or_create_run(self) -> InstallRun:
        if self.run_path.is_file():
            with self.run_path.open("r", encoding="utf-8") as handle:
                payload = redact(json.load(handle))
            return InstallRun(
                run_id=str(payload.get("run_id") or uuid.uuid4()),
                plan_id=self.planned.plan.plan_id,
                state=RunState(payload.get("state", RunState.QUEUED.value)),
                created_at=str(payload.get("created_at") or utc_now()),
                updated_at=str(payload.get("updated_at") or utc_now()),
                checkpoint=payload.get("checkpoint"),
                evidence=list(payload.get("evidence", [])),
            )
        return InstallRun(plan_id=self.planned.plan.plan_id)

    def _persist_run(self, run: InstallRun) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = run.to_dict()
        payload["plan_hash"] = self.planned.plan.plan_hash
        _atomic_json(self.run_path, redact(payload), mode=0o600)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value)


def _dpkg_packages_present(runner: SystemCommandRunner, packages: list[str]) -> bool:
    for package in packages:
        result = runner.run(["dpkg-query", "-W", "-f=${Status}", package], check=False)
        if result.returncode != 0 or result.stdout.strip() != "install ok installed":
            return False
    return True


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(archive, "r:*") as handle:
        members = handle.getmembers()
        for member in members:
            if member.issym() or member.islnk() or member.isdev():
                raise InstallError(f"release archive contains unsupported link/device entry: {member.name}")
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise InstallError(f"release archive path escapes destination: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise InstallError(f"release archive contains unsupported entry: {member.name}")

        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                os.chmod(target, 0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            source = handle.extractfile(member)
            if source is None:
                raise InstallError(f"release archive file has no readable payload: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, 0o644)


def _replace_symlink(link: Path, target: Path) -> None:
    temp = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.symlink_to(target)
        os.replace(temp, link)
    finally:
        if temp.exists() or temp.is_symlink():
            temp.unlink(missing_ok=True)


def _atomic_text(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    _atomic_text(path, json.dumps(redact(payload), sort_keys=True, indent=2) + "\n", mode=mode)


def _http_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - caller provides fixed loopback URLs only
        if response.status != 200:
            raise InstallError(f"health endpoint returned HTTP {response.status}: {url}")
        return json.loads(response.read().decode("utf-8"))


def _systemd_units() -> dict[str, str]:
    common = """[Unit]\nAfter=network.target postgresql.service\nRequires=postgresql.service\n\n[Service]\nType=simple\nUser=genos\nGroup=genos\nWorkingDirectory=/var/lib/genos\nEnvironmentFile=/etc/genos/genos.env\nEnvironment=PYTHONPATH=/opt/genos/current/src\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nRestart=on-failure\nRestartSec=2\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nReadWritePaths=/var/lib/genos /var/log/genos\nProtectHome=true\n\n"""
    install = "\n[Install]\nWantedBy=multi-user.target\n"
    return {
        "genos-product-api.service": common
        + f"ExecStart=/usr/bin/python3 -m genos.core_service product-api --port {PRODUCT_API_PORT}\n"
        + install,
        "genos-runtime.service": common
        + f"ExecStart=/usr/bin/python3 -m genos.core_service runtime --port {RUNTIME_PORT}\n"
        + install,
        "genos-worker.service": common
        + "ExecStart=/usr/bin/python3 -m genos.core_service worker --state-dir /var/lib/genos\n"
        + install,
        "genos-mission-control.service": common
        + f"ExecStart=/usr/bin/python3 -m genos.core_service mission-control --port {MISSION_CONTROL_PORT}\n"
        + install,
    }
