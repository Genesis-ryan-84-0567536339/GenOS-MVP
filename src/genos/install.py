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
import time
import urllib.error
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
BASE_PACKAGES = ("postgresql", "postgresql-client", "ca-certificates", "curl")


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
    decision = decide_boundary(observations, requested_mode, allow_candidate_e2e=allow_candidate_e2e)
    native_steps = [
        {"step_id": "prepare_install_state", "action": "filesystem_state_root"},
        {"step_id": "install_packages", "action": "apt_postgresql_and_runtime_packages"},
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
    steps = native_steps if decision.mode is BoundaryMode.NATIVE else []
    profile = get_profile(decision.profile_id) if decision.profile_id else None
    canonical = {
        "mode": decision.mode.value if decision.mode else None,
        "profile_id": decision.profile_id,
        "profile_state": profile.state.value if profile else None,
        "release_git_sha": release.git_sha,
        "release_sha256": release.sha256.lower(),
        "steps": steps,
    }
    plan_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
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
            run.evidence.append({"state": "FAIL", "error_type": type(exc).__name__, "message": str(exc), "observed_at": utc_now()})
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
        if _dpkg_packages_present(self.runner, list(BASE_PACKAGES)):
            return
        self.runner.run(["apt-get", "update"], timeout=300)
        self.runner.run(["apt-get", "install", "-y", *BASE_PACKAGES], timeout=600)

    def _create_service_identity(self) -> None:
        try:
            grp.getgrnam(CORE_GROUP)
        except KeyError:
            self.runner.run(["groupadd", "--system", CORE_GROUP])
        try:
            pwd.getpwnam(CORE_USER)
        except KeyError:
            self.runner.run([
                "useradd", "--system", "--gid", CORE_GROUP,
                "--home-dir", "/var/lib/genos", "--shell", "/usr/sbin/nologin", CORE_USER,
            ])

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

    def _write_configuration(self) -> None:
        # Remaining implementation intentionally unchanged from the current
        # release; this replacement continues below in the repository history.
        instance_id = self._load_or_create_instance_id()
        self._instance_id = instance_id
        env_path = Path("/etc/genos/genos.env")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            f"GENOS_INSTANCE_ID={instance_id}\nGENOS_STATE_DIR=/var/lib/genos\nGENOS_LOG_DIR=/var/log/genos\n",
            encoding="utf-8",
        )
        os.chmod(env_path, 0o640)
        os.chown(env_path, 0, grp.getgrnam(CORE_GROUP).gr_gid)

    def _install_systemd_units(self) -> None:
        units = _systemd_units()
        for name, content in units.items():
            path = Path("/etc/systemd/system") / name
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o644)
        self.runner.run(["systemctl", "daemon-reload"])

    def _start_postgresql(self) -> None:
        self.runner.run(["systemctl", "enable", "--now", "postgresql.service"])

    def _provision_database(self) -> None:
        role_sql = "SELECT 1 FROM pg_roles WHERE rolname='genos';"
        exists = self.runner.run(["runuser", "-u", "postgres", "--", "psql", "-tAc", role_sql], check=False)
        if exists.returncode != 0:
            raise InstallError("could not inspect PostgreSQL role state")
        if exists.stdout.strip() != "1":
            self.runner.run(["runuser", "-u", "postgres", "--", "createuser", "--no-superuser", "--no-createdb", "--no-createrole", "genos"])
        db_sql = "SELECT 1 FROM pg_database WHERE datname='genos';"
        db_exists = self.runner.run(["runuser", "-u", "postgres", "--", "psql", "-tAc", db_sql], check=False)
        if db_exists.returncode != 0:
            raise InstallError("could not inspect PostgreSQL database state")
        if db_exists.stdout.strip() != "1":
            self.runner.run(["runuser", "-u", "postgres", "--", "createdb", "--owner=genos", "genos"])
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        schema = """
CREATE TABLE IF NOT EXISTS owners (
    owner_id uuid PRIMARY KEY,
    username text UNIQUE NOT NULL,
    password_salt bytea NOT NULL,
    password_hash bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS owner_sessions (
    session_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES owners(owner_id) ON DELETE CASCADE,
    token_hash bytea UNIQUE NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS credentials (
    secret_id uuid PRIMARY KEY,
    name text UNIQUE NOT NULL,
    provider text NOT NULL,
    active_revision integer NOT NULL,
    status text NOT NULL,
    fingerprint text NOT NULL,
    consumer_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
""".strip()
        result = self.runner.run(["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-d", CORE_DB, "-c", schema], check=False)
        if result.returncode != 0:
            raise InstallError("could not provision PostgreSQL product schema")

    def _start_core_services(self) -> None:
        self.runner.run(["systemctl", "enable", "--now", "genos-product-api.service"])
        self.runner.run(["systemctl", "enable", "--now", "genos-runtime.service"])
        self.runner.run(["systemctl", "enable", "--now", "genos-worker.service"])
        self.runner.run(["systemctl", "enable", "--now", "genos-mission-control.service"])

    def _verify_local_core(self) -> None:
        checks = (
            (PRODUCT_API_PORT, "product-api"),
            (RUNTIME_PORT, "runtime"),
            (MISSION_CONTROL_PORT, "mission-control"),
        )
        for port, role in checks:
            last_error: Exception | None = None
            for _ in range(20):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("status") != "ok" or payload.get("role") != role:
                        raise InstallError(f"unexpected health response for {role}: {payload}")
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, InstallError) as exc:
                    last_error = exc
                    time.sleep(0.5)
            else:
                raise InstallError(f"service health gate failed for {role}: {type(last_error).__name__ if last_error else 'unknown'}")
        heartbeat = self.store.root / "worker" / "heartbeat.json"
        for _ in range(20):
            if heartbeat.is_file():
                return
            time.sleep(0.5)
        raise InstallError("worker heartbeat was not created")

    def _finalize_manifest(self) -> None:
        instance_id = self._instance_id or self._load_or_create_instance_id()
        self.store.save_manifest({
            "schema_version": "1.0",
            "state": "READY",
            "instance_id": instance_id,
            "profile_id": self.planned.plan.profile,
            "install_mode": "native",
            "release_git_sha": self.planned.release.git_sha,
            "release_sha256": self.planned.release.sha256.lower(),
            "plan_hash": self.planned.plan.plan_hash,
            "services": {
                "product_api": {"bind": f"127.0.0.1:{PRODUCT_API_PORT}"},
                "runtime": {"bind": f"127.0.0.1:{RUNTIME_PORT}"},
                "worker": {"state_file": "/var/lib/genos/worker/heartbeat.json"},
                "mission_control": {"bind": f"127.0.0.1:{MISSION_CONTROL_PORT}", "ui_state": "NOT_IMPLEMENTED"},
            },
            "updated_at": utc_now(),
        })

    def _load_or_create_instance_id(self) -> str:
        path = Path("/etc/genos/instance-id")
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o640)
        os.chown(path, 0, grp.getgrnam(CORE_GROUP).gr_gid)
        return value

    def _load_resume_state(self) -> None:
        if not self.run_path.is_file():
            return
        try:
            payload = json.loads(self.run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("plan_id") != self.planned.plan.plan_id:
            return
        self._completed = {str(item["step_id"]) for item in payload.get("evidence", []) if item.get("state") == "PASS" and item.get("step_id")}

    def _load_or_create_run(self) -> InstallRun:
        if self.run_path.is_file():
            try:
                payload = json.loads(self.run_path.read_text(encoding="utf-8"))
                if payload.get("plan_id") == self.planned.plan.plan_id:
                    return InstallRun(
                        run_id=str(payload.get("run_id") or uuid.uuid4()),
                        plan_id=self.planned.plan.plan_id,
                        state=RunState(payload.get("state", RunState.QUEUED.value)),
                        created_at=str(payload.get("created_at") or utc_now()),
                        updated_at=str(payload.get("updated_at") or utc_now()),
                        checkpoint=payload.get("checkpoint"),
                        evidence=list(payload.get("evidence", [])),
                    )
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        return InstallRun(plan_id=self.planned.plan.plan_id)

    def _persist_run(self, run: InstallRun) -> None:
        self.run_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.run_path.with_suffix(".tmp")
        temp.write_text(json.dumps(redact(run.to_dict()), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.run_path)


def _systemd_units() -> dict[str, str]:
    common = """[Unit]
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=genos
Group=genos
EnvironmentFile=/etc/genos/genos.env
WorkingDirectory=/var/lib/genos
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/genos /var/log/genos
ProtectHome=true
PrivateDevices=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictNamespaces=true
"""
    return {
        "genos-product-api.service": common + "ExecStart=/usr/bin/python3 -m genos.core_service product-api --port 17880\nRestart=on-failure\nRestartSec=2\n[Install]\nWantedBy=multi-user.target\n",
        "genos-runtime.service": common + "ExecStart=/usr/bin/python3 -m genos.core_service runtime --port 17881\nRestart=on-failure\nRestartSec=2\n[Install]\nWantedBy=multi-user.target\n",
        "genos-worker.service": common + "ExecStart=/usr/bin/python3 -m genos.core_service worker --worker-interval 5\nRestart=always\nRestartSec=2\n[Install]\nWantedBy=multi-user.target\n",
        "genos-mission-control.service": common + "ExecStart=/usr/bin/python3 -m genos.core_service mission-control --port 17882\nRestart=on-failure\nRestartSec=2\n[Install]\nWantedBy=multi-user.target\n",
    }


def _dpkg_packages_present(runner: SystemCommandRunner, packages: list[str]) -> bool:
    for package in packages:
        result = runner.run(["dpkg-query", "-W", "-f=${Status}", package], check=False)
        if result.returncode != 0 or result.stdout.strip() != "install ok installed":
            return False
    return True


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:*") as handle:
        root = destination.resolve()
        members = handle.getmembers()
        for member in members:
            if member.isdev() or member.isfifo():
                raise InstallError("release archive contains unsupported device entry")
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise InstallError("release archive contains path traversal")
            if member.issym() or member.islnk():
                raise InstallError("release archive must not contain symlinks or hardlinks")
        handle.extractall(destination, members=members, filter="data")


def _replace_symlink(link: Path, target: Path) -> None:
    temp = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.symlink_to(target)
        os.replace(temp, link)
    finally:
        temp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value)
