from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from genos.artifact_store import CardArtifactStore
from genos.drive_collab import ATTACHMENTS_FOLDER, DriveCollaborationService
from genos.kanban import InvalidCardTransition, KanbanSystem


class FakeCredentials:
    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        assert secret_id == "secret-fixture" and consumer == "drive-sync"
        return "raw-fixture-token"


class FakeMetadata:
    def get_drive_binding(self):
        return {"state": "READY", "secret_id": "secret-fixture", "kanban_folder_id": "kanban-root"}


class FakeCards:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.artifacts: dict[str, list[dict]] = {}

    def create_card(self, **kwargs):
        remote = kwargs.get("source_remote_id")
        if remote and self.get_by_remote(source_kind=kwargs["source_kind"], source_remote_id=remote):
            raise RuntimeError("duplicate remote")
        card_id = str(uuid.uuid4())
        card = {
            "card_id": card_id,
            "title": kwargs["title"],
            "description": kwargs.get("description", ""),
            "status": kwargs.get("status", "BACKLOG"),
            "assignee_agent_id": kwargs.get("assignee_agent_id"),
            "source_kind": kwargs.get("source_kind", "LOCAL"),
            "source_remote_id": remote,
            "source_revision_hash": kwargs.get("source_revision_hash"),
            "agent_task_id": None,
            "mirror_folder_id": None,
            "mirror_card_file_id": None,
            "mirror_status_file_id": None,
            "last_sync_state": None,
            "last_sync_error": None,
        }
        self.cards[card_id] = card
        self.events[card_id] = []
        self.artifacts[card_id] = []
        self.add_event(card_id, "CARD_CREATED", {"status": card["status"]})
        return dict(card)

    def get_by_remote(self, *, source_kind, source_remote_id):
        return next(
            (
                dict(card)
                for card in self.cards.values()
                if card["source_kind"] == source_kind and card["source_remote_id"] == source_remote_id
            ),
            None,
        )

    def require_card(self, card_id):
        return dict(self.cards[card_id])

    def list_cards(self, *, status=None, limit=200):
        values = list(self.cards.values())
        if status:
            values = [card for card in values if card["status"] == status]
        return [dict(card) for card in values[:limit]]

    def transition(self, card_id, *, expected_state, new_state, reason):
        card = self.cards[card_id]
        if card["status"] != expected_state:
            raise RuntimeError("concurrent")
        card["status"] = new_state
        self.add_event(card_id, "STATUS_CHANGED", {"from": expected_state, "to": new_state, "reason": reason})
        return dict(card)

    def set_agent_task(self, card_id, *, task_id):
        self.cards[card_id]["agent_task_id"] = task_id
        return dict(self.cards[card_id])

    def set_remote_revision(self, card_id, *, revision_hash, sync_state, sync_error=None):
        self.cards[card_id]["source_revision_hash"] = revision_hash
        self.cards[card_id]["last_sync_state"] = sync_state
        self.cards[card_id]["last_sync_error"] = sync_error
        return dict(self.cards[card_id])

    def set_sync_state(self, card_id, *, sync_state, sync_error=None):
        self.cards[card_id]["last_sync_state"] = sync_state
        self.cards[card_id]["last_sync_error"] = sync_error
        return dict(self.cards[card_id])

    def set_mirror(self, card_id, *, folder_id, card_file_id, status_file_id, sync_state="SYNCED"):
        card = self.cards[card_id]
        card["mirror_folder_id"] = folder_id
        card["mirror_card_file_id"] = card_file_id
        card["mirror_status_file_id"] = status_file_id
        card["last_sync_state"] = sync_state
        card["last_sync_error"] = None
        return dict(card)

    def add_event(self, card_id, event_type, payload):
        event = {"event_id": str(uuid.uuid4()), "event_type": event_type, "payload": dict(payload)}
        self.events.setdefault(card_id, []).append(event)
        return event

    def list_events(self, card_id, *, limit=200):
        return list(self.events.get(card_id, []))[:limit]

    def add_artifact(self, **kwargs):
        record = {"artifact_id": str(uuid.uuid4()), **kwargs}
        self.artifacts[kwargs["card_id"]].append(record)
        return record

    def list_artifacts(self, card_id):
        return list(self.artifacts.get(card_id, []))


class FakeRemote:
    FOLDER = "application/vnd.google-apps.folder"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {
            "kanban-root": {"id": "kanban-root", "name": "Kanban", "mimeType": self.FOLDER, "parent": "root", "version": "1"}
        }
        self.content: dict[str, bytes] = {}
        self.write_count = 0
        self.fail_writes = False

    def folder(self, *, name: str, parent: str, file_id: str | None = None) -> str:
        file_id = file_id or f"folder-{uuid.uuid4().hex[:8]}"
        self.nodes[file_id] = {"id": file_id, "name": name, "mimeType": self.FOLDER, "parent": parent, "version": "1"}
        return file_id

    def file(self, *, name: str, parent: str, data: bytes, mime: str = "text/plain", file_id: str | None = None) -> str:
        file_id = file_id or f"file-{uuid.uuid4().hex[:8]}"
        self.nodes[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parent": parent,
            "version": "1",
            "size": len(data),
        }
        self.content[file_id] = data
        return file_id

    def ensure_folder(self, *, name, parent_id=None):
        if self.fail_writes:
            raise RuntimeError("fixture outage")
        parent = parent_id or "root"
        for item in self.nodes.values():
            if item["parent"] == parent and item["name"] == name and item["mimeType"] == self.FOLDER:
                return item["id"]
        self.write_count += 1
        return self.folder(name=name, parent=parent)

    def list_children(self, parent_id):
        return [dict(item) for item in self.nodes.values() if item["parent"] == parent_id]

    def read_bytes(self, file_id, *, max_bytes):
        return self.content[file_id][: max_bytes + 1]

    def upsert_text_file(self, *, name, parent_id, content, file_id=None, mime_type="text/plain; charset=utf-8"):
        return self.upsert_bytes(name=name, parent_id=parent_id, content=content.encode(), mime_type=mime_type, file_id=file_id)

    def upsert_bytes(self, *, name, parent_id, content, mime_type, file_id=None):
        if self.fail_writes:
            raise RuntimeError("fixture outage")
        target = file_id
        if not target:
            for item in self.nodes.values():
                if item["parent"] == parent_id and item["name"] == name and item["mimeType"] != self.FOLDER:
                    target = item["id"]
                    break
        if not target:
            target = self.file(name=name, parent=parent_id, data=content, mime=mime_type.split(";", 1)[0])
        else:
            self.content[target] = content
            self.nodes[target]["version"] = str(int(self.nodes[target].get("version", "0")) + 1)
        self.write_count += 1
        return target


class RemoteFactory:
    def __init__(self, remote):
        self.remote = remote

    def __call__(self, raw_secret):
        assert raw_secret == "raw-fixture-token"
        return self.remote


class DriveCollaborationTests(unittest.TestCase):
    def _fixture(self, temp):
        cards = FakeCards()
        remote = FakeRemote()
        inbox = remote.folder(name="Inbox", parent="kanban-root", file_id="inbox")
        request = remote.folder(name="REQ-001", parent=inbox, file_id="req-folder")
        remote.file(name="REQUEST.md", parent=request, data=b"# Build a fixture\n\nPlease process this request.", file_id="request-md")
        attachments = remote.folder(name=ATTACHMENTS_FOLDER, parent=request, file_id="req-att")
        remote.file(name="notes.txt", parent=attachments, data=b"fixture attachment", file_id="notes")
        service = DriveCollaborationService(
            metadata=FakeMetadata(),
            credentials=FakeCredentials(),  # type: ignore[arg-type]
            cards=cards,  # type: ignore[arg-type]
            artifacts=CardArtifactStore(Path(temp) / "artifacts"),
            remote_factory=RemoteFactory(remote),
        )
        return cards, remote, service

    def test_repeated_sync_creates_exactly_one_card_and_imports_attachment(self):
        with tempfile.TemporaryDirectory() as temp:
            cards, _remote, service = self._fixture(temp)
            first = service.sync_inbox()
            second = service.sync_inbox()
            self.assertEqual(first["created"], 1)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(len(cards.cards), 1)
            card_id = next(iter(cards.cards))
            self.assertEqual(cards.cards[card_id]["status"], "BACKLOG")
            self.assertEqual(len(cards.artifacts[card_id]), 1)
            self.assertEqual(cards.artifacts[card_id][0]["state"], "ACCEPTED")
            self.assertEqual(cards.cards[card_id]["last_sync_state"], "SYNCED")

    def test_remote_revision_change_does_not_overwrite_local_card(self):
        with tempfile.TemporaryDirectory() as temp:
            cards, remote, service = self._fixture(temp)
            service.sync_inbox()
            card_id = next(iter(cards.cards))
            original = cards.cards[card_id]["description"]
            remote.content["request-md"] = b"# Changed remotely\n\nDo not silently overwrite local authority."
            remote.nodes["request-md"]["version"] = "2"
            result = service.sync_inbox()
            self.assertEqual(result["conflicts"], 1)
            self.assertEqual(cards.cards[card_id]["description"], original)
            self.assertEqual(cards.cards[card_id]["last_sync_state"], "CONFLICT")

    def test_outbound_failure_keeps_local_truth_and_marks_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            cards, remote, service = self._fixture(temp)
            service.sync_inbox()
            card_id = next(iter(cards.cards))
            remote.fail_writes = True
            cards.add_event(card_id, "COMMENT_ADDED", {"text": "local comment survives"})
            projected = service.project_after_local_change(card_id)
            self.assertEqual(projected["state"], "RETRY")
            self.assertEqual(cards.cards[card_id]["last_sync_state"], "RETRY")
            self.assertTrue(any(e["payload"].get("text") == "local comment survives" for e in cards.events[card_id]))


class FakeDriveProjection:
    def __init__(self):
        self.calls = []
    def project_after_local_change(self, card_id):
        self.calls.append(card_id)
        return {"state": "SYNCED"}
    def sync_inbox(self):
        return {"state": "COMPLETED"}


class FakeAgent:
    def __init__(self, root: Path):
        self.root = root
        self.result_dir = root / "results"
        self.result_dir.mkdir(parents=True)
        self.claim_path = root / "claim.json"
        self.counter = 0
    def queue_task(self, prompt):
        self.counter += 1
        task_id = str(uuid.uuid4())
        self.claim_path.write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
        return task_id
    def release_work(self, *, task_id):
        self.claim_path.unlink(missing_ok=True)


class KanbanLifecycleTests(unittest.TestCase):
    def test_agy_idle_claim_result_verify_then_owner_done(self):
        with tempfile.TemporaryDirectory() as temp:
            cards = FakeCards()
            card = cards.create_card(title="Do work", description="Return a result", assignee_agent_id="agy-gen")
            artifacts = CardArtifactStore(Path(temp) / "artifacts")
            drive = FakeDriveProjection()
            agent = FakeAgent(Path(temp) / "agent")
            system = KanbanSystem(cards=cards, artifacts=artifacts, drive=drive, agent=agent)  # type: ignore[arg-type]
            claimed = system.agent_tick()
            self.assertEqual(claimed["state"], "CLAIMED")
            task_id = claimed["task_id"]
            self.assertEqual(cards.cards[card["card_id"]]["status"], "PROCESS")
            agent.claim_path.unlink()
            (agent.result_dir / f"{task_id}.json").write_text(
                json.dumps({"task_id": task_id, "state": "SUCCEEDED", "output": "fixture result", "output_sha256": hashlib.sha256(b"fixture result").hexdigest()}),
                encoding="utf-8",
            )
            applied = system.agent_tick()
            self.assertEqual(applied["state"], "RESULT_APPLIED")
            self.assertEqual(cards.cards[card["card_id"]]["status"], "VERIFY")
            self.assertEqual(cards.artifacts[card["card_id"]][0]["kind"], "OUTPUT")
            finished = system.transition(card["card_id"], to_state="DONE", reason="OWNER_VERIFIED")
            self.assertEqual(finished["card"]["status"], "DONE")

    def test_done_card_cannot_be_reopened_by_invalid_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            cards = FakeCards()
            card = cards.create_card(title="Done work", description="")
            cards.cards[card["card_id"]]["status"] = "DONE"
            system = KanbanSystem(
                cards=cards,
                artifacts=CardArtifactStore(Path(temp) / "artifacts"),
                drive=FakeDriveProjection(),
                agent=FakeAgent(Path(temp) / "agent"),
            )  # type: ignore[arg-type]
            with self.assertRaises(InvalidCardTransition):
                system.transition(card["card_id"], to_state="BACKLOG")


if __name__ == "__main__":
    unittest.main()
