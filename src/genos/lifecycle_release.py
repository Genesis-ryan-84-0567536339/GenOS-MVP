from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import grp
import json
import os
import tempfile

from .install import ReleaseArtifact
from .lifecycle import LifecycleError
from .lifecycle_hardened import HardenedLifecycleService


class ReleaseCandidateLifecycleService(HardenedLifecycleService):
    """Public MVP-11 lifecycle authority with release identity convergence."""

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
            self._write_release_identity(release)
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

    def _write_release_identity(self, release: ReleaseArtifact) -> None:
        env_path = self.paths.config / "genos.env"
        if not env_path.is_file():
            raise LifecycleError("GenOS environment configuration is missing")
        lines = env_path.read_text(encoding="utf-8").splitlines()
        replaced = False
        updated_lines: list[str] = []
        for line in lines:
            if line.startswith("GENOS_RELEASE_SHA="):
                updated_lines.append(f"GENOS_RELEASE_SHA={release.git_sha}")
                replaced = True
            else:
                updated_lines.append(line)
        if not replaced:
            updated_lines.append(f"GENOS_RELEASE_SHA={release.git_sha}")
        self._atomic_text(env_path, "\n".join(updated_lines) + "\n", mode=0o640)

        manifest_path = self.paths.state / "manifest.json"
        if not manifest_path.is_file():
            raise LifecycleError("GenOS install manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleError("GenOS install manifest is unreadable") from exc
        if not isinstance(manifest, dict):
            raise LifecycleError("GenOS install manifest is invalid")
        release_meta = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
        release_meta.update({"git_sha": release.git_sha, "sha256": release.sha256.lower()})
        manifest["release"] = release_meta
        manifest["updated_at"] = _utc_now()
        self._atomic_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            mode=0o640,
        )

        if self._is_live_layout():
            gid = grp.getgrnam("genos").gr_gid
            os.chown(env_path, 0, gid)
            os.chown(manifest_path, 0, gid)

    @staticmethod
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
