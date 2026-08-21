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
            return JobRun.from_dict(redact(json.load(handle)))

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded recent JobRun activity summaries without new state.

        Full evidence remains in the durable JobRun file and can be consumed by
        typed support/report paths. Mission Control needs only progress/activity
        metadata, which keeps refresh responses small and secret-safe.
        """
        bounded = max(1, min(int(limit), 200))
        if not self.jobs_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        try:
            paths = sorted(
                self.jobs_dir.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []
        for path in paths[:bounded]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    safe = redact(payload)
                    evidence = safe.get("evidence") if isinstance(safe.get("evidence"), list) else []
                    last_evidence = evidence[-1] if evidence and isinstance(evidence[-1], dict) else None
                    rows.append(
                        {
                            "job_id": safe.get("job_id") or path.stem,
                            "kind": safe.get("kind") or "generic",
                            "state": safe.get("state") or "UNKNOWN",
                            "progress_percent": safe.get("progress_percent", 0),
                            "current_step": safe.get("current_step"),
                            "created_at": safe.get("created_at"),
                            "updated_at": safe.get("updated_at"),
                            "last_evidence": last_evidence,
                        }
                    )
                    continue
            except (OSError, json.JSONDecodeError):
                pass
            rows.append(
                {
                    "job_id": path.stem,
                    "kind": "UNKNOWN",
                    "state": "UNKNOWN",
                    "progress_percent": 0,
                    "current_step": "job_record_unreadable",
                    "last_evidence": None,
                }
            )
        return rows

    def save_manifest(self, payload: dict[str, Any]) -> None:
        self._atomic_write(self.manifest_path, redact(payload))

    def load_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        with self.manifest_path.open("r", encoding="utf-8") as handle:
            return redact(json.load(handle))

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
