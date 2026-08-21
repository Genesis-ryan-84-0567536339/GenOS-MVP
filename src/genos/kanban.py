from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .agent_runtime import AgentBusyError, AgentNeedsAction, AgentRuntimeStore
from .artifact_store import CardArtifactStore
from .auth_service import CredentialService
from .drive_collab import DriveCollaborationService
from .drive_store import PostgresDriveMetadataStore
from .kanban_store import CardConflict, KanbanStoreError, PostgresKanbanStore
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider


TRANSITIONS: dict[str, frozenset[str]] = {
    "BACKLOG": frozenset({"PROCESS", "CANCELLED"}),
    "PROCESS": frozenset({"WAITING_INPUT", "WAITING_APPROVAL", "VERIFY", "FAILED", "CANCELLED"}),
    "WAITING_INPUT": frozenset({"PROCESS", "CANCELLED"}),
    "WAITING_APPROVAL": frozenset({"PROCESS", "VERIFY", "CANCELLED"}),
    "VERIFY": frozenset({"DONE", "PROCESS", "FAILED", "CANCELLED"}),
    "FAILED": frozenset({"BACKLOG", "CANCELLED"}),
    "CANCELLED": frozenset({"BACKLOG"}),
    "DONE": frozenset(),
}


class KanbanError(RuntimeError):
    pass


class InvalidCardTransition(KanbanError):
    pass


@dataclass(frozen=True, slots=True)
class KanbanSystem:
    cards: PostgresKanbanStore
    artifacts: CardArtifactStore
    drive: DriveCollaborationService
    agent: AgentRuntimeStore

    def list_cards(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.cards.list_cards(status=status)

    def get_card(self, card_id: str) -> dict[str, Any]:
        return {
            "card": self.cards.require_card(card_id),
            "events": self.cards.list_events(card_id),
            "artifacts": self.cards.list_artifacts(card_id),
        }

    def create_card(self, *, title: str, description: str = "", assignee_agent_id: str | None = "agy-gen") -> dict[str, Any]:
        return self.cards.create_card(
            title=title,
            description=description,
            status="BACKLOG",
            assignee_agent_id=assignee_agent_id,
            source_kind="LOCAL",
        )

    def transition(self, card_id: str, *, to_state: str, reason: str = "OWNER_ACTION") -> dict[str, Any]:
        current = self.cards.require_card(card_id)
        from_state = str(current["status"])
        if to_state not in TRANSITIONS.get(from_state, frozenset()):
            raise InvalidCardTransition(f"transition {from_state} -> {to_state} is not allowed")
        updated = self.cards.transition(card_id, expected_state=from_state, new_state=to_state, reason=reason)
        mirror = self.drive.project_after_local_change(card_id)
        return {"card": updated, "mirror": mirror}

    def add_comment(self, card_id: str, *, text: str) -> dict[str, Any]:
        clean = _bounded_text(text, 16 * 1024, required=True)
        event = self.cards.add_event(card_id, "COMMENT_ADDED", {"text": clean})
        mirror = self.drive.project_after_local_change(card_id)
        return {"event": event, "mirror": mirror}

    def sync_drive_inbox(self) -> dict[str, Any]:
        return self.drive.sync_inbox()

    def agent_tick(self) -> dict[str, Any]:
        """Advance at most one agy-gen Card without bypassing the Card lifecycle."""
        for card in self.cards.list_cards(status="PROCESS"):
            task_id = card.get("agent_task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            result_path = self.agent.result_dir / f"{task_id}.json"
            if not result_path.is_file():
                continue
            result = _read_json(result_path)
            state = str(result.get("state") or "FAILED")
            self.cards.add_event(
                str(card["card_id"]),
                "AGENT_RESULT",
                {"task_id": task_id, "state": state, "output_sha256": result.get("output_sha256")},
            )
            if state == "SUCCEEDED":
                output = result.get("output")
                if isinstance(output, str) and output:
                    written = self.artifacts.write_output(
                        card_id=str(card["card_id"]),
                        name=f"agy-gen-{task_id}.txt",
                        content=output.encode("utf-8", errors="replace"),
                    )
                    self.cards.add_artifact(
                        card_id=str(card["card_id"]),
                        kind="OUTPUT",
                        name=written.name,
                        mime_type=written.mime_type,
                        size_bytes=written.size_bytes,
                        sha256=written.sha256,
                        state=written.state,
                        local_path=written.local_path,
                        remote_file_id=None,
                        quarantine_reason=written.quarantine_reason,
                    )
                updated = self.cards.transition(
                    str(card["card_id"]),
                    expected_state="PROCESS",
                    new_state="VERIFY",
                    reason="AGY_EXECUTION_SUCCEEDED",
                )
            else:
                updated = self.cards.transition(
                    str(card["card_id"]),
                    expected_state="PROCESS",
                    new_state="FAILED",
                    reason="AGY_EXECUTION_FAILED",
                )
            self.drive.project_after_local_change(str(card["card_id"]))
            return {"state": "RESULT_APPLIED", "card": updated, "task_id": task_id}

        if self.agent.claim_path.exists():
            return {"state": "BUSY", "reason": "AGY_GEN_WORK_CLAIM_ACTIVE"}

        backlog = [item for item in self.cards.list_cards(status="BACKLOG") if item.get("assignee_agent_id") == "agy-gen"]
        if not backlog:
            return {"state": "IDLE", "reason": "NO_BACKLOG_CARD"}
        card = backlog[0]
        prompt = self._prompt_for_card(str(card["card_id"]))
        try:
            task_id = self.agent.queue_task(prompt)
        except AgentNeedsAction:
            return {"state": "NEEDS_ACTION", "reason": "AGY_GEN_PROVIDER_NOT_ACTIVE", "card_id": card["card_id"]}
        except AgentBusyError:
            return {"state": "BUSY", "reason": "AGY_GEN_WORK_CLAIM_ACTIVE"}
        try:
            self.cards.set_agent_task(str(card["card_id"]), task_id=task_id)
            updated = self.cards.transition(
                str(card["card_id"]), expected_state="BACKLOG", new_state="PROCESS", reason="AGY_AUTO_CLAIM"
            )
            self.cards.add_event(str(card["card_id"]), "AGENT_TASK_QUEUED", {"task_id": task_id, "agent_id": "agy-gen"})
        except Exception:
            self.agent.release_work(task_id=task_id)
            raise
        self.drive.project_after_local_change(str(card["card_id"]))
        return {"state": "CLAIMED", "card": updated, "task_id": task_id}

    def _prompt_for_card(self, card_id: str) -> str:
        card = self.cards.require_card(card_id)
        parts = [
            "You are agy-gen executing one GenOS Kanban Card.",
            f"Card ID: {card_id}",
            f"Title: {card['title']}",
            "Request:",
            str(card.get("description") or ""),
        ]
        remaining = 48 * 1024
        attachment_sections: list[str] = []
        for artifact in self.cards.list_artifacts(card_id):
            if artifact.get("state") != "ACCEPTED" or artifact.get("kind") != "ATTACHMENT":
                continue
            mime = str(artifact.get("mime_type") or "")
            local_path = artifact.get("local_path")
            if not isinstance(local_path, str) or mime not in {"text/plain", "text/markdown", "application/json"}:
                continue
            try:
                data = self.artifacts.read(local_path, max_bytes=min(remaining, 256 * 1024))
                text = data.decode("utf-8")
            except Exception:
                continue
            section = f"\nAttachment {artifact['name']}:\n{text}"
            encoded = section.encode("utf-8")
            if len(encoded) > remaining:
                break
            attachment_sections.append(section)
            remaining -= len(encoded)
        parts.extend(attachment_sections)
        parts.append("Return a concise result suitable for Owner verification. Do not treat Drive as authority.")
        return "\n".join(parts)


def build_kanban_system(
    *,
    product_store: PostgresProductStore | None = None,
    credentials: CredentialService | None = None,
    artifact_root: Path | str | None = None,
    agent_root: Path | str | None = None,
    remote_factory: Any | None = None,
) -> KanbanSystem:
    store = product_store or PostgresProductStore()
    store.ensure_schema()
    drive_metadata = PostgresDriveMetadataStore(store)
    drive_metadata.ensure_schema()
    cards = PostgresKanbanStore(store)
    cards.ensure_schema()
    if credentials is None:
        secret_root = os.environ.get("GENOS_SECRET_DIR", "/var/lib/genos/secrets")
        credentials = CredentialService(store, LocalFileSecretProvider(secret_root))
    artifacts = CardArtifactStore(artifact_root or os.environ.get("GENOS_ARTIFACT_DIR", "/var/lib/genos/artifacts"))
    agent = AgentRuntimeStore(agent_root or os.environ.get("GENOS_AGY_GEN_DIR", "/var/lib/genos/agents/agy-gen"))
    drive = DriveCollaborationService(
        metadata=drive_metadata,
        credentials=credentials,
        cards=cards,
        artifacts=artifacts,
        remote_factory=remote_factory,
    )
    return KanbanSystem(cards=cards, artifacts=artifacts, drive=drive, agent=agent)


def _bounded_text(value: str, limit: int, *, required: bool = False) -> str:
    text = str(value)
    if required and not text.strip():
        raise KanbanError("text is required")
    if "\x00" in text or len(text.encode("utf-8")) > limit:
        raise KanbanError("text exceeds supported bounds")
    return text


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KanbanError("Agent result is unavailable") from exc
    if not isinstance(value, dict):
        raise KanbanError("Agent result is invalid")
    return value
