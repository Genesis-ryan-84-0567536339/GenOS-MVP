from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid

from .install import ReleaseArtifact, SystemCommandRunner
from .redaction import redact


CORE_SERVICES = (
    "genos-product-api.service",
    "genos-runtime.service",
    "genos-worker.service",
    "genos-mcp.service",
    "genos-mission-control.service",
    "genos-edge.service",
)
BACKUP_SCHEMA = "genos-backup-v1"
SUPPORT_SCHEMA = "genos-support-bundle-v1"
MAX_BACKUP_MEMBER_BYTES = 4 * 1024 * 1024 * 1024


class LifecycleError(RuntimeError):
    pass


class LifecycleNeedsAction(LifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class LifecyclePaths:
    state: Path = Path("/var/lib/genos")
    config: Path = Path("/etc/genos")
    opt: Path = Path("/opt/genos")
    systemd: Path = Path("/etc/systemd/system")
    run: Path = Path("/run/genos")

    @property
    def backups(self) -> Path:
        return self.state / "backups"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def releases(self) -> Path:
        return self.opt / "releases"


class LifecycleService:
    """Typed lifecycle mutations for the final MVP package.

    All external commands are fixed argv vectors through SystemCommandRunner.
    No operation accepts arbitrary command text. Remote Drive/Cloudflare content
    is never deleted by uninstall/purge; those operations only remove local
    bindings/state after explicit local destructive confirmation.
    """

    def __init__(
        self,
        *,
        paths: LifecyclePaths | None = None,
        runner: SystemCommandRunner | None = None,
        health_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.paths = paths or LifecyclePaths()
        self.runner = runner or SystemCommandRunner()
        self.health_probe = health_probe or self._default_health_probe

    # -------------------- public operations --------------------
    def backup(self, *, output: Path | None = None, include_secrets: bool = False) -> dict[str, Any]:
        self._require_installed()
        destination = output or self._default_backup_path()
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".genos-backup-", dir=str(destination.parent)))
        try:
            payload = staging / "payload"
            payload.mkdir(mode=0o700)
            state_target = payload / "state"
            config_target = payload / "config"
            state_target.mkdir(mode=0o700)
            config_target.mkdir(mode=0o700)

            self._copy_state_snapshot(state_target, include_secrets=include_secrets)
            if self.paths.config.is_dir():
                shutil.copytree(self.paths.config, config_target / "genos", symlinks=False)

            db_dump = payload / "product-db.dump"
            self._pg_dump(db_dump)

            manifest = self._backup_manifest(payload, include_secrets=include_secrets)
            (staging / "BACKUP_MANIFEST.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(staging / "BACKUP_MANIFEST.json", 0o600)
            self._create_archive(staging, destination)
            archive_sha = sha256_file(destination)
            return {
                "state": "SUCCEEDED",
                "schema": BACKUP_SCHEMA,
                "archive": str(destination),
                "sha256": archive_sha,
                "instance_id": manifest["instance_id"],
                "include_secrets": bool(include_secrets),
                "secret_policy": "EXPLICIT_INCLUDED" if include_secrets else "EXCLUDED_REAUTHORIZE_IF_REQUIRED",
                "remote_resources_mutated": False,
            }
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def restore(
        self,
        *,
        archive: Path,
        expected_sha256: str,
        allow_instance_replace: bool = False,
    ) -> dict[str, Any]:
        archive = archive.expanduser().resolve()
        if not archive.is_file():
            raise LifecycleError("backup archive does not exist")
        actual = sha256_file(archive)
        if actual.lower() != str(expected_sha256).lower():
            raise LifecycleError("backup archive checksum mismatch")

        work = Path(tempfile.mkdtemp(prefix="genos-restore-"))
        checkpoint = Path(tempfile.mkdtemp(prefix="genos-restore-checkpoint-"))
        try:
            self._safe_extract_archive(archive, work)
            manifest_path = work / "BACKUP_MANIFEST.json"
            if not manifest_path.is_file():
                raise LifecycleError("backup manifest is missing")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._verify_backup_manifest(work, manifest)
            backup_instance = _uuid_text(manifest.get("instance_id"), field="backup instance_id")
            current_instance = self._instance_id(required=False)
            if current_instance and backup_instance != current_instance and not allow_instance_replace:
                raise LifecycleNeedsAction("restore instance_id differs; explicit allow-instance-replace is required")

            self._require_root_for_live_mutation()
            self._capture_restore_checkpoint(checkpoint)
            self._stop_services()
            try:
                self._restore_files(work / "payload")
                self._restore_database(work / "payload" / "product-db.dump")
                self._reload_and_start_services()
                health = self.health_probe()
                if str(health.get("state") or "UNKNOWN") not in {"PASS", "READY"}:
                    raise LifecycleError("post-restore health verification failed")
            except Exception:
                self._rollback_restore_checkpoint(checkpoint)
                self._reload_and_start_services()
                raise
            return {
                "state": "SUCCEEDED",
                "schema": BACKUP_SCHEMA,
                "instance_id": backup_instance,
                "source_archive_sha256": actual,
                "secrets_restored": bool(manifest.get("include_secrets", False)),
                "external_resources_mutated": False,
                "health": health,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(checkpoint, ignore_errors=True)

    def update(self, *, release: ReleaseArtifact) -> dict[str, Any]:
        self._require_installed()
        self._require_root_for_live_mutation()
        release.verify()
        before = self._current_release()
        checkpoint = self.backup(include_secrets=False)
        target = self.paths.releases / release.git_sha
        self._stage_release(release, target)
        self._replace_current(target)
        try:
            self._reload_and_start_services(restart=True)
            health = self.health_probe()
            if str(health.get("state") or "UNKNOWN") not in {"PASS", "READY"}:
                raise LifecycleError("post-update health verification failed")
        except Exception:
            if before is not None and before.exists():
                self._replace_current(before)
                self._reload_and_start_services(restart=True)
            raise
        return {
            "state": "SUCCEEDED",
            "release_git_sha": release.git_sha,
            "release_sha256": release.sha256.lower(),
            "previous_release": str(before) if before else None,
            "rollback_checkpoint": checkpoint["archive"],
            "health": health,
        }

    def uninstall(self) -> dict[str, Any]:
        self._require_root_for_live_mutation()
        preserved_config = self.paths.state / "uninstall-preserved-config"
        if self.paths.config.is_dir():
            if preserved_config.exists():
                shutil.rmtree(preserved_config)
            shutil.copytree(self.paths.config, preserved_config, symlinks=False)
            os.chmod(preserved_config, 0o700)
        self._stop_services(disable=True)
        for unit in CORE_SERVICES:
            (self.paths.systemd / unit).unlink(missing_ok=True)
        self._daemon_reload()
        if self.paths.current.is_symlink() or self.paths.current.exists():
            self.paths.current.unlink(missing_ok=True)
        if self.paths.releases.is_dir():
            shutil.rmtree(self.paths.releases)
        if self.paths.config.is_dir():
            shutil.rmtree(self.paths.config)
        return {
            "state": "UNINSTALLED_DATA_PRESERVED",
            "durable_state_preserved": self.paths.state.exists(),
            "secret_material_preserved": (self.paths.state / "secrets").exists(),
            "preserved_config": str(preserved_config) if preserved_config.exists() else None,
            "remote_resources_deleted": False,
        }

    def purge(self, *, confirm_instance_id: str) -> dict[str, Any]:
        expected = self._instance_id(required=True)
        supplied = _uuid_text(confirm_instance_id, field="confirm_instance_id")
        if supplied != expected:
            raise LifecycleNeedsAction("purge confirmation does not match this GenOS instance_id")
        self._require_root_for_live_mutation()
        external = self._external_unbind_projection()
        self._stop_services(disable=True)
        for unit in CORE_SERVICES:
            (self.paths.systemd / unit).unlink(missing_ok=True)
        self._daemon_reload()
        for path in (self.paths.config, self.paths.state, self.paths.opt, self.paths.run):
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
        return {
            "state": "PURGED",
            "instance_id": expected,
            "local_data_deleted": True,
            "remote_resources_deleted": False,
            "external_unbind": external,
        }

    def support_bundle(self, *, output: Path | None = None) -> dict[str, Any]:
        destination = (output or self._default_support_path()).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".genos-support-", dir=str(destination.parent)))
        try:
            payload: dict[str, Any] = {
                "schema": SUPPORT_SCHEMA,
                "generated_at": _utc_now(),
                "instance_id": self._instance_id(required=False) or "UNKNOWN",
                "release": str(self._current_release() or "UNKNOWN"),
                "services": self._service_states(),
                "manifest": self._read_json_sanitized(self.paths.state / "manifest.json"),
                "edge": self._read_json_sanitized(self.paths.state / "edge" / "binding.json"),
                "worker": self._read_json_sanitized(self.paths.state / "worker" / "heartbeat.json"),
            }
            try:
                from .observability import ObservabilityService
                payload["observability"] = redact(ObservabilityService(state_root=self.paths.state).snapshot())
            except Exception as exc:
                payload["observability"] = {"state": "UNKNOWN", "reason": type(exc).__name__}
            serialized = json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if _contains_obvious_secret(serialized):
                raise LifecycleError("support bundle sanitizer rejected sensitive content")
            info = staging / "support.json"
            info.write_text(serialized, encoding="utf-8")
            os.chmod(info, 0o600)
            self._create_archive(staging, destination)
            return {
                "state": "SUCCEEDED",
                "schema": SUPPORT_SCHEMA,
                "archive": str(destination),
                "sha256": sha256_file(destination),
                "redacted": True,
                "raw_secret_included": False,
            }
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -------------------- backup helpers --------------------
    def _backup_manifest(self, payload: Path, *, include_secrets: bool) -> dict[str, Any]:
        members: dict[str, str] = {}
        for path in sorted(p for p in payload.rglob("*") if p.is_file()):
            members[path.relative_to(payload.parent).as_posix()] = sha256_file(path)
        return {
            "schema": BACKUP_SCHEMA,
            "created_at": _utc_now(),
            "instance_id": self._instance_id(required=True),
            "release": str(self._current_release() or "UNKNOWN"),
            "include_secrets": bool(include_secrets),
            "members": members,
        }

    def _copy_state_snapshot(self, target: Path, *, include_secrets: bool) -> None:
        if not self.paths.state.is_dir():
            return
        for item in self.paths.state.iterdir():
            if item.name == "backups":
                continue
            if item.name == "secrets" and not include_secrets:
                continue
            destination = target / item.name
            if item.is_symlink():
                continue
            if item.is_dir():
                shutil.copytree(item, destination, symlinks=False)
            elif item.is_file():
                shutil.copy2(item, destination)

    def _pg_dump(self, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._is_live_layout():
            self.runner.run(
                ["runuser", "-u", "postgres", "--", "pg_dump", "--format=custom", "--no-owner", "--file", str(output), "genos"],
                timeout=300,
            )
            if not output.is_file():
                raise LifecycleError("pg_dump completed without backup output")
        else:
            # Test/non-live layout: preserve deterministic fixture DB projection.
            source = self.paths.state / "fixture-product-db.dump"
            if source.is_file():
                shutil.copy2(source, output)
            else:
                output.write_bytes(b"GENOS_FIXTURE_DB\n")

    def _create_archive(self, source: Path, destination: Path) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
        os.close(fd)
        temp = Path(temp_name)
        try:
            with tarfile.open(temp, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                for item in sorted(source.rglob("*")):
                    archive.add(item, arcname=item.relative_to(source).as_posix(), recursive=False)
            os.chmod(temp, 0o600)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def _safe_extract_archive(self, archive_path: Path, destination: Path) -> None:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk():
                    raise LifecycleError("backup archive contains unsafe member")
                if member.size < 0 or member.size > MAX_BACKUP_MEMBER_BYTES:
                    raise LifecycleError("backup archive member exceeds safety limit")
            archive.extractall(destination, filter="data")

    def _verify_backup_manifest(self, root: Path, manifest: dict[str, Any]) -> None:
        if manifest.get("schema") != BACKUP_SCHEMA or not isinstance(manifest.get("members"), dict):
            raise LifecycleError("backup manifest schema is invalid")
        for relative, expected in manifest["members"].items():
            path = root / str(relative)
            if not path.is_file() or sha256_file(path) != str(expected):
                raise LifecycleError(f"backup member verification failed: {relative}")

    # -------------------- restore helpers --------------------
    def _capture_restore_checkpoint(self, checkpoint: Path) -> None:
        files = checkpoint / "files"
        files.mkdir()
        if self.paths.state.is_dir():
            shutil.copytree(self.paths.state, files / "state", symlinks=False, ignore=shutil.ignore_patterns("backups"))
        if self.paths.config.is_dir():
            shutil.copytree(self.paths.config, files / "config", symlinks=False)
        self._pg_dump(checkpoint / "db.dump")

    def _restore_files(self, payload: Path) -> None:
        state_source = payload / "state"
        config_source = payload / "config" / "genos"
        self.paths.state.mkdir(parents=True, exist_ok=True)
        for item in list(self.paths.state.iterdir()):
            if item.name == "backups":
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
        if state_source.is_dir():
            for item in state_source.iterdir():
                target = self.paths.state / item.name
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=False)
                else:
                    shutil.copy2(item, target)
        if config_source.is_dir():
            if self.paths.config.exists():
                shutil.rmtree(self.paths.config)
            shutil.copytree(config_source, self.paths.config, symlinks=False)

    def _restore_database(self, dump: Path) -> None:
        if not dump.is_file():
            raise LifecycleError("database dump is missing")
        if not self._is_live_layout():
            shutil.copy2(dump, self.paths.state / "fixture-product-db.dump")
            return
        self.runner.run(
            ["runuser", "-u", "postgres", "--", "pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", "genos", str(dump)],
            timeout=600,
        )

    def _rollback_restore_checkpoint(self, checkpoint: Path) -> None:
        files = checkpoint / "files"
        state_source = files / "state"
        config_source = files / "config"
        if self.paths.state.exists():
            shutil.rmtree(self.paths.state)
        if state_source.is_dir():
            shutil.copytree(state_source, self.paths.state, symlinks=False)
        else:
            self.paths.state.mkdir(parents=True, exist_ok=True)
        if self.paths.config.exists():
            shutil.rmtree(self.paths.config)
        if config_source.is_dir():
            shutil.copytree(config_source, self.paths.config, symlinks=False)
        dump = checkpoint / "db.dump"
        if dump.is_file():
            self._restore_database(dump)

    # -------------------- update helpers --------------------
    def _stage_release(self, release: ReleaseArtifact, target: Path) -> None:
        if target.exists():
            marker = target / ".genos-release-sha256"
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == release.sha256.lower():
                return
            raise LifecycleError("target release directory already exists with different digest")
        self.paths.releases.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=f".{release.git_sha}.", dir=str(self.paths.releases)))
        try:
            with tarfile.open(release.archive, "r:*") as archive:
                for member in archive.getmembers():
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk():
                        raise LifecycleError("release archive contains unsafe member")
                archive.extractall(temp, filter="data")
            if not (temp / "src" / "genos" / "__init__.py").is_file():
                raise LifecycleError("release archive is missing GenOS package")
            (temp / ".genos-release-sha256").write_text(release.sha256.lower() + "\n", encoding="utf-8")
            os.rename(temp, target)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

    def _replace_current(self, target: Path) -> None:
        self.paths.opt.mkdir(parents=True, exist_ok=True)
        temp = self.paths.opt / ".current-next"
        temp.unlink(missing_ok=True)
        temp.symlink_to(target)
        os.replace(temp, self.paths.current)

    # -------------------- system helpers --------------------
    def _stop_services(self, *, disable: bool = False) -> None:
        if not self._is_live_layout():
            return
        for service in reversed(CORE_SERVICES):
            self.runner.run(["systemctl", "stop", service], check=False, timeout=30)
            if disable:
                self.runner.run(["systemctl", "disable", service], check=False, timeout=30)

    def _reload_and_start_services(self, *, restart: bool = False) -> None:
        if not self._is_live_layout():
            return
        self._daemon_reload()
        action = "restart" if restart else "start"
        for service in CORE_SERVICES:
            unit_path = self.paths.systemd / service
            if unit_path.exists():
                self.runner.run(["systemctl", action, service], check=False, timeout=60)

    def _daemon_reload(self) -> None:
        if self._is_live_layout():
            self.runner.run(["systemctl", "daemon-reload"], check=False, timeout=30)

    def _service_states(self) -> dict[str, str]:
        if not self._is_live_layout():
            return {service: "UNKNOWN_TEST_LAYOUT" for service in CORE_SERVICES}
        states: dict[str, str] = {}
        for service in CORE_SERVICES:
            result = self.runner.run(["systemctl", "is-active", service], check=False, timeout=10)
            states[service] = result.stdout.strip() or "UNKNOWN"
        return states

    def _default_health_probe(self) -> dict[str, Any]:
        if not self._is_live_layout():
            return {"state": "PASS", "source": "non-live-fixture"}
        from .observability import ObservabilityService
        snapshot = ObservabilityService(state_root=self.paths.state).snapshot()
        health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
        state = str(health.get("state") or "UNKNOWN")
        return {"state": "PASS" if state in {"PASS", "READY", "HEALTHY"} else state, "health": health}

    def _external_unbind_projection(self) -> dict[str, Any]:
        drive = self._read_json_sanitized(self.paths.state / "drive" / "oauth-session.json")
        edge = self._read_json_sanitized(self.paths.state / "edge" / "binding.json")
        return {
            "drive": {"state": "LOCAL_BINDING_REMOVED", "remote_content_deleted": False, "previous": drive},
            "edge": {"state": "LOCAL_BINDING_REMOVED", "remote_resources_deleted": False, "previous": edge},
        }

    def _read_json_sanitized(self, path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state": "UNKNOWN", "reason": "UNREADABLE"}
        return redact(payload)

    def _require_installed(self) -> None:
        if not self.paths.state.exists() or self._instance_id(required=False) is None:
            raise LifecycleNeedsAction("GenOS install state is unavailable")

    def _instance_id(self, *, required: bool) -> str | None:
        candidates = [self.paths.config / "instance-id", self.paths.state / "uninstall-preserved-config" / "instance-id"]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                return str(uuid.UUID(path.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                continue
        if required:
            raise LifecycleNeedsAction("GenOS instance_id is unavailable")
        return None

    def _current_release(self) -> Path | None:
        try:
            return self.paths.current.resolve(strict=True)
        except (OSError, RuntimeError):
            return None

    def _default_backup_path(self) -> Path:
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        return self.paths.backups / f"genos-backup-{_stamp()}.tar.gz"

    def _default_support_path(self) -> Path:
        root = self.paths.state / "support"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"genos-support-{_stamp()}.tar.gz"

    def _require_root_for_live_mutation(self) -> None:
        if self._is_live_layout() and os.geteuid() != 0:
            raise LifecycleNeedsAction("this lifecycle mutation requires root on the installed host")

    def _is_live_layout(self) -> bool:
        return self.paths == LifecyclePaths()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _uuid_text(value: Any, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError) as exc:
        raise LifecycleError(f"invalid {field}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _contains_obvious_secret(text: str) -> bool:
    lowered = text.lower()
    forbidden = (
        "authorization: bearer ",
        "access_token\"",
        "refresh_token\"",
        "client_secret\"",
        "raw_secret\"",
        "tunnel_token\"",
    )
    return any(item in lowered for item in forbidden)
