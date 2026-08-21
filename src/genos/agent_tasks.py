from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .agent_runtime import AgentNeedsAction, AgentRuntimeStore
from .redaction import redact


class AgentTaskService:
    """Owner-facing task projection over the durable agy-gen task store.

    This service does not introduce a second queue or execution authority. It
    reads/writes only the existing AgentRuntimeStore task directories and claim.
    """

    def __init__(self, store: AgentRuntimeStore) -> None:
        self.store = store

    def submit(self, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task prompt is required")
        if len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("task prompt is too large")
        task_id = self.store.queue_task(prompt.strip())
        queued = self._read_json(self.store.queue_dir / f"{task_id}.json") or {}
        return self._public_task({"task_id": task_id, **queued})

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        rows: list[tuple[float, dict[str, Any]]] = []
        for state_dir in (self.store.result_dir, self.store.queue_dir):
            if not state_dir.is_dir():
                continue
            for path in state_dir.glob("*.json"):
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    modified = 0.0
                payload = self._read_json(path)
                if payload is None:
                    rows.append(
                        (
                            modified,
                            {
                                "task_id": path.stem,
                                "state": "UNKNOWN",
                                "error": "TASK_RECORD_UNREADABLE",
                            },
                        )
                    )
                else:
                    rows.append((modified, self._public_task(payload)))
        rows.sort(key=lambda item: item[0], reverse=True)
        return [payload for _modified, payload in rows[:bounded]]

    def current(self) -> dict[str, Any]:
        status = self.store.status()
        runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
        claim = status.get("claim") if isinstance(status.get("claim"), dict) else None
        return {
            "agent_id": "agy-gen",
            "runtime": redact(runtime),
            "claim": redact(claim) if claim else None,
            "provider": redact(status.get("provider") if isinstance(status.get("provider"), dict) else {}),
            "identity": redact(status.get("identity") if isinstance(status.get("identity"), dict) else {}),
        }

    def _public_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        output = payload.get("output") if isinstance(payload.get("output"), str) else None
        return redact(
            {
                "task_id": payload.get("task_id"),
                "agent_id": payload.get("agent_id") or "agy-gen",
                "state": payload.get("state") or "UNKNOWN",
                "prompt": prompt[:16 * 1024] if prompt is not None else None,
                "output": output[:64 * 1024] if output is not None else None,
                "output_sha256": payload.get("output_sha256"),
                "error": payload.get("error"),
                "created_at": payload.get("created_at"),
                "observed_at": payload.get("observed_at"),
            }
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
