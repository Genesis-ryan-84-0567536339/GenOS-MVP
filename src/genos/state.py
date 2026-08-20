from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile

from .contracts import JobRun
from .redaction import redact


class JsonStateStore:
    """Small durable JSON checkpoint store used before PostgreSQL is provisioned."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        if root is None:
            env_root = os.environ.get("GENOS_STATE_DIR")
            root = env_root if env_root else Path.home() / ".local" / "state" / "genos"
        self.root = Path(root)

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def save_job(self, job: JobRun) -> None:
        path = self.jobs_dir / f"{job.job_id}.json"
        self._atomic_write(path, job.to_dict())

    def load_job(self, job_id: str) -> JobRun:
        path = self.jobs_dir / f"{job_id}.json"
        with path.open("r", encoding="utf-8") as handle:
            return JobRun.from_dict(json.load(handle))

    def save_manifest(self, payload: dict[str, Any]) -> None:
        self._atomic_write(self.manifest_path, redact(payload))

    def load_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        with self.manifest_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            try:
                dir_fd = os.open(path.parent, os.O_DIRECTORY)
            except (AttributeError, OSError):
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
