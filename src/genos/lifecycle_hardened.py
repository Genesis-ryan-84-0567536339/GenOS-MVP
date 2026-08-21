from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
import json
import os
import shutil
import tarfile
import tempfile

from .install import ReleaseArtifact
from .lifecycle import CORE_SERVICES, LifecycleError, LifecycleNeedsAction, LifecyclePaths, LifecycleService, sha256_file


MAX_RELEASE_MEMBER_BYTES = 1024 * 1024 * 1024


class HardenedLifecycleService(LifecycleService):
    """Final public lifecycle authority used by MVP-11.

    This layer intentionally overrides only the safety-sensitive operations that
    need stronger release-candidate semantics than the initial lifecycle draft:
    restore preserves existing SecretProvider material when a normal backup did
    not include secrets; update restores its pre-mutation DB/state checkpoint on
    failure; purge removes the local Product DB/role; release extraction rejects
    link/device/oversized members. External Drive/Cloudflare resources are never
    deleted by these local lifecycle operations.
    """

    def _verify_backup_manifest(self, root: Path, manifest: dict[str, Any]) -> None:
        super()._verify_backup_manifest(root, manifest)
        secret_dir = root / "payload" / "state" / "secrets"
        includes = bool(manifest.get("include_secrets", False))
        if includes != secret_dir.is_dir():
            raise LifecycleError("backup secret policy does not match archive contents")

    def _restore_files(self, payload: Path) -> None:
        state_source = payload / "state"
        config_source = payload / "config" / "genos"
        restore_secrets = (state_source / "secrets").is_dir()

        self.paths.state.mkdir(parents=True, exist_ok=True)
        for item in list(self.paths.state.iterdir()):
            if item.name == "backups":
                continue
            if item.name == "secrets" and not restore_secrets:
                # Normal backups intentionally omit SecretProvider material. A
                # restore must not turn that omission into secret destruction.
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)

        if state_source.is_dir():
            for item in state_source.iterdir():
                if item.name == "secrets" and not restore_secrets:
                    continue
                target = self.paths.state / item.name
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=False)
                else:
                    shutil.copy2(item, target)

        if config_source.is_dir():
            if self.paths.config.exists():
                shutil.rmtree(self.paths.config)
            shutil.copytree(config_source, self.paths.config, symlinks=False)

    def update(self, *, release: ReleaseArtifact) -> dict[str, Any]:
        self._require_installed()
        self._require_root_for_live_mutation()
        release.verify()
        previous = self._current_release()
        checkpoint = self.backup(include_secrets=False)
        target = self.paths.releases / release.git_sha
        self._stage_release(release, target)
        self._replace_current(target)
        try:
            self._reload_and_start_services(restart=True)
            health = self.health_probe()
            if str(health.get("state") or "UNKNOWN") not in {"PASS", "READY"}:
                raise LifecycleError("post-update health verification failed")
        except Exception as update_exc:
            rollback_errors: list[str] = []
            try:
                if previous is not None and previous.exists():
                    self._replace_current(previous)
            except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                rollback_errors.append(f"release:{type(exc).__name__}")
            try:
                self.restore(
                    archive=Path(str(checkpoint["archive"])),
                    expected_sha256=str(checkpoint["sha256"]),
                    allow_instance_replace=False,
                )
            except Exception as exc:
                rollback_errors.append(f"state:{type(exc).__name__}")
            if rollback_errors:
                raise LifecycleError(
                    "update failed and rollback was incomplete: " + ",".join(rollback_errors)
                ) from update_exc
            raise LifecycleError("update failed; previous release and checkpoint were restored") from update_exc

        return {
            "state": "SUCCEEDED",
            "release_git_sha": release.git_sha,
            "release_sha256": release.sha256.lower(),
            "previous_release": str(previous) if previous else None,
            "rollback_checkpoint": checkpoint["archive"],
            "rollback_checkpoint_sha256": checkpoint["sha256"],
            "health": health,
        }

    def purge(self, *, confirm_instance_id: str) -> dict[str, Any]:
        expected = self._instance_id(required=True)
        supplied = _uuid_text(confirm_instance_id)
        if supplied != expected:
            raise LifecycleNeedsAction("purge confirmation does not match this GenOS instance_id")
        self._require_root_for_live_mutation()
        external = self._external_unbind_projection()
        self._stop_services(disable=True)
        self._drop_product_database()
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
            "product_database_deleted": True,
            "remote_resources_deleted": False,
            "external_unbind": external,
        }

    def _drop_product_database(self) -> None:
        if not self._is_live_layout():
            fixture = self.paths.state / "fixture-product-db.dump"
            fixture.unlink(missing_ok=True)
            return
        self.runner.run(
            ["runuser", "-u", "postgres", "--", "dropdb", "--if-exists", "genos"],
            timeout=120,
        )
        self.runner.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                "postgres",
                "-c",
                "DROP ROLE IF EXISTS genos",
            ],
            timeout=120,
        )

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
                members = archive.getmembers()
                for member in members:
                    name = PurePosixPath(member.name)
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or member.issym()
                        or member.islnk()
                        or member.isdev()
                    ):
                        raise LifecycleError("release archive contains unsafe member")
                    if member.size < 0 or member.size > MAX_RELEASE_MEMBER_BYTES:
                        raise LifecycleError("release archive member exceeds safety limit")
                    if not (member.isdir() or member.isfile()):
                        raise LifecycleError("release archive contains unsupported member")
                archive.extractall(temp, filter="data")
            if not (temp / "src" / "genos" / "__init__.py").is_file():
                raise LifecycleError("release archive is missing GenOS package")
            (temp / ".genos-release-sha256").write_text(release.sha256.lower() + "\n", encoding="utf-8")
            os.rename(temp, target)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)


def restore_preserved_install_identity(
    *,
    state_root: Path = Path("/var/lib/genos"),
    config_root: Path = Path("/etc/genos"),
) -> dict[str, Any]:
    """Restore only stable non-secret identity before reinstall after uninstall.

    The installer will rewrite `genos.env`; this helper restores only instance-id
    and MCP port from the uninstall-preserved config so an uninstall/reinstall
    cycle cannot silently become a different GenOS instance/Agent endpoint.
    """

    preserved = state_root / "uninstall-preserved-config"
    if config_root.exists() or not preserved.is_dir():
        return {"state": "NO_CHANGE"}
    config_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    restored: list[str] = []
    for name in ("instance-id", "mcp-port"):
        source = preserved / name
        if not source.is_file():
            continue
        target = config_root / name
        shutil.copy2(source, target)
        os.chmod(target, 0o640)
        restored.append(name)
    return {"state": "RESTORED" if restored else "NO_CHANGE", "files": restored}


def _uuid_text(value: Any) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError) as exc:
        raise LifecycleError("invalid confirm_instance_id") from exc
