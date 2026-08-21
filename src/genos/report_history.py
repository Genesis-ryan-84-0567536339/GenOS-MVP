from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os
import tempfile


class ReportHistoryError(RuntimeError):
    pass


class ReportHistoryStore:
    """Small local report history/diff authority for published report metadata.

    It stores only job IDs, fingerprints, remote file IDs and a fingerprint
    delta. Report source dumps and credentials are deliberately excluded.
    """

    def __init__(self, root: Path | str = Path("/var/lib/genos")) -> None:
        self.root = Path(root)

    @property
    def history_dir(self) -> Path:
        return self.root / "reports" / "history"

    @property
    def latest_path(self) -> Path:
        return self.root / "reports" / "latest.json"

    def record(self, publish_result: dict[str, Any], *, manual: bool) -> dict[str, Any]:
        fingerprint = _required_text(publish_result, "fingerprint")
        job = publish_result.get("job") if isinstance(publish_result.get("job"), dict) else {}
        job_id = _required_text(job, "job_id")
        files = publish_result.get("files") if isinstance(publish_result.get("files"), dict) else {}
        previous = self.latest()
        previous_fingerprint = previous.get("fingerprint") if previous else None
        if previous_fingerprint is None:
            diff_state = "INITIAL"
        elif previous_fingerprint == fingerprint:
            diff_state = "UNCHANGED"
        else:
            diff_state = "CHANGED"
        recorded_at = _utc_now()
        entry = {
            "schema_version": "1.0",
            "history_id": job_id,
            "job_id": job_id,
            "fingerprint": fingerprint,
            "manual": bool(manual),
            "recorded_at": recorded_at,
            "files": {
                "markdown": _optional_text(files.get("markdown")),
                "json": _optional_text(files.get("json")),
            },
            "diff": {
                "state": diff_state,
                "previous_fingerprint": previous_fingerprint,
                "current_fingerprint": fingerprint,
            },
        }
        self._atomic_write(self.history_dir / f"{job_id}.json", entry)
        self._atomic_write(self.latest_path, entry)
        return {
            "recorded": True,
            "history_id": job_id,
            "recorded_at": recorded_at,
            "diff": dict(entry["diff"]),
        }

    def latest(self) -> dict[str, Any] | None:
        if not self.latest_path.is_file():
            return None
        return self._read_entry(self.latest_path)

    def list_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        if not self.history_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        paths = sorted(
            self.history_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0.0,
            reverse=True,
        )
        for path in paths[:bounded]:
            try:
                rows.append(self._read_entry(path))
            except ReportHistoryError:
                rows.append(
                    {
                        "history_id": path.stem,
                        "job_id": path.stem,
                        "state": "UNKNOWN",
                        "diff": {"state": "UNKNOWN"},
                    }
                )
        return rows

    def _read_entry(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportHistoryError("report history record is unreadable") from exc
        if not isinstance(payload, dict):
            raise ReportHistoryError("report history record is invalid")
        # The store contains metadata only, but whitelist the public projection so
        # future internal fields cannot silently become a browser data surface.
        files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
        diff = payload.get("diff") if isinstance(payload.get("diff"), dict) else {}
        return {
            "schema_version": payload.get("schema_version"),
            "history_id": payload.get("history_id"),
            "job_id": payload.get("job_id"),
            "fingerprint": payload.get("fingerprint"),
            "manual": bool(payload.get("manual", False)),
            "recorded_at": payload.get("recorded_at"),
            "files": {
                "markdown": _optional_text(files.get("markdown")),
                "json": _optional_text(files.get("json")),
            },
            "diff": {
                "state": diff.get("state") or "UNKNOWN",
                "previous_fingerprint": diff.get("previous_fingerprint"),
                "current_fingerprint": diff.get("current_fingerprint"),
            },
        }

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReportHistoryError(f"report history is missing {key}")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
