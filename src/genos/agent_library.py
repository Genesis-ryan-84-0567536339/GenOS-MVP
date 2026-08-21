from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import os
import tempfile

from .agent_runtime import AgentRuntimeStore, AgentRuntimeError
from .redaction import redact


MAX_LIBRARY_CONTENT_BYTES = 48 * 1024
MAX_PUBLIC_CONTENT_CHARS = 16 * 1024
MAX_PUBLIC_REVISIONS = 50


class AgentLibraryError(AgentRuntimeError):
    pass


class AgentLibraryService:
    """Typed Owner-facing memory/skill revision projection.

    Revision content remains under the existing AgentRuntimeStore authority. This
    service adds only a tiny durable binding record describing which revision is
    active/disabled so Mission Control can perform explicit rollback/disable
    without coupling revision identity to the live tmux/provider process.
    """

    def __init__(self, store: AgentRuntimeStore) -> None:
        self.store = store
        self.binding_path = store.root / "library-bindings.json"

    def inventory(self) -> dict[str, Any]:
        bindings = self._bindings()
        return {
            "agent_id": "agy-gen",
            "memory": self._inventory_kind("memory", self.store.memory_dir, bindings),
            "skills": self._inventory_kind("skill", self.store.skills_dir, bindings),
        }

    def append_revision(self, *, kind: str, name: str, content: str, source: str = "owner-ui") -> dict[str, Any]:
        if kind not in {"memory", "skill"}:
            raise AgentLibraryError("kind must be memory or skill")
        if not isinstance(content, str) or not content.strip():
            raise AgentLibraryError("revision content is required")
        if len(content.encode("utf-8")) > MAX_LIBRARY_CONTENT_BYTES:
            raise AgentLibraryError("revision content is too large")
        revision = self.store.append_revision(kind, name, content, source=source)
        bindings = self._bindings()
        key = self._binding_key(kind, name)
        bindings[key] = {"state": "ACTIVE", "active_revision": int(revision["revision"])}
        self._save_bindings(bindings)
        return self._public_revision(revision, active=True, state="ACTIVE")

    def activate(self, *, kind: str, name: str, revision: int) -> dict[str, Any]:
        revisions = self.store.list_revisions(kind, name)
        selected = next((item for item in revisions if int(item.get("revision", -1)) == int(revision)), None)
        if selected is None:
            raise AgentLibraryError("revision not found")
        bindings = self._bindings()
        bindings[self._binding_key(kind, name)] = {"state": "ACTIVE", "active_revision": int(revision)}
        self._save_bindings(bindings)
        return self._public_revision(selected, active=True, state="ACTIVE")

    def disable(self, *, kind: str, name: str) -> dict[str, Any]:
        revisions = self.store.list_revisions(kind, name)
        if not revisions:
            raise AgentLibraryError("library item not found")
        bindings = self._bindings()
        key = self._binding_key(kind, name)
        current = bindings.get(key) if isinstance(bindings.get(key), dict) else {}
        active_revision = int(current.get("active_revision") or revisions[-1].get("revision") or 1)
        bindings[key] = {"state": "DISABLED", "active_revision": active_revision}
        self._save_bindings(bindings)
        return {"kind": kind, "name": name, "state": "DISABLED", "active_revision": active_revision}

    def _inventory_kind(self, kind: str, root: Path, bindings: dict[str, Any]) -> list[dict[str, Any]]:
        if not root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for target in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
            revisions = self.store.list_revisions(kind, target.name)
            if not revisions:
                continue
            key = self._binding_key(kind, target.name)
            binding = bindings.get(key) if isinstance(bindings.get(key), dict) else {}
            active_revision = int(binding.get("active_revision") or revisions[-1].get("revision") or 1)
            state = str(binding.get("state") or "ACTIVE")
            visible = revisions[-MAX_PUBLIC_REVISIONS:]
            public_revisions = [
                self._public_revision(
                    revision,
                    active=state == "ACTIVE" and int(revision.get("revision", -1)) == active_revision,
                    state=state if int(revision.get("revision", -1)) == active_revision else "SUPERSEDED",
                )
                for revision in visible
            ]
            items.append(
                {
                    "kind": kind,
                    "name": target.name,
                    "state": state,
                    "active_revision": active_revision,
                    "revision_count": len(revisions),
                    "revisions": list(reversed(public_revisions)),
                }
            )
        return items

    def _public_revision(self, revision: dict[str, Any], *, active: bool, state: str) -> dict[str, Any]:
        content = str(revision.get("content") or "")
        return redact(
            {
                "kind": revision.get("kind"),
                "name": revision.get("name"),
                "revision": revision.get("revision"),
                "source": revision.get("source"),
                "created_at": revision.get("created_at"),
                "state": state,
                "active": bool(active),
                "content": content[:MAX_PUBLIC_CONTENT_CHARS],
                "content_sha256": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
            }
        )

    def _bindings(self) -> dict[str, Any]:
        if not self.binding_path.is_file():
            return {}
        try:
            payload = json.loads(self.binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentLibraryError("library binding state is unreadable") from exc
        if not isinstance(payload, dict):
            raise AgentLibraryError("library binding state is invalid")
        return payload

    def _save_bindings(self, payload: dict[str, Any]) -> None:
        self.binding_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.binding_path.name}.", dir=str(self.binding_path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.binding_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _binding_key(kind: str, name: str) -> str:
        clean = name.strip()
        if kind not in {"memory", "skill"} or not clean or any(char in clean for char in "/\\\x00"):
            raise AgentLibraryError("invalid library item")
        return f"{kind}:{clean}"
