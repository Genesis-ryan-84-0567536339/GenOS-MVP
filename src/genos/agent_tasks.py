from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .agent_runtime import AgentRuntimeStore
from .redaction import redact


MAX_TASK_PROMPT_BYTES = 48 * 1024
MAX_PUBLIC_PROMPT_CHARS = 4 * 1024
MAX_PUBLIC_OUTPUT_CHARS = 4 * 1024


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
        clean = prompt.strip()
        if len(clean.encode("utf-8")) > MAX_TASK_PROMPT_BYTES:
            raise ValueError("task prompt is too large")
        task_id = self.store.queue_task(clean)
        queued = self._read_json(self.store.queue_dir / f"{task_id}.json") or {}
        return self._public_task({"task_id": task_id, **queued})

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        # A task can exist briefly in both queued/results while the worker is
        # committing the result. Deduplicate by task_id and prefer result state.
        by_task: dict[str, tuple[float, int, dict[str, Any]]] = {}
        for priority, state_dir in ((0, self.store.queue_dir), (1, self.store.result_dir)):
            if not state_dir.is_dir():
                continue
            for path in state_dir.glob("*.json"):
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    modified = 0.0
                payload = self._read_json(path)
                if payload is None:
                    public = {
                        "task_id": path.stem,
                        "state": "UNKNOWN",
                        "error": "TASK_RECORD_UNREADABLE",
                    }
                else:
                    public = self._public_task(payload)
                task_id = str(public.get("task_id") or path.stem)
                previous = by_task.get(task_id)
                candidate = (modified, priority, public)
                if previous is None or priority > previous[1] or (priority == previous[1] and modified > previous[0]):
                    by_task[task_id] = candidate
        rows = sorted(by_task.values(), key=lambda item: item[0], reverse=True)
        return [payload for _modified, _priority, payload in rows[:bounded]]

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
                "prompt": prompt[:MAX_PUBLIC_PROMPT_CHARS] if prompt is not None else None,
                "output": output[:MAX_PUBLIC_OUTPUT_CHARS] if output is not None else None,
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
