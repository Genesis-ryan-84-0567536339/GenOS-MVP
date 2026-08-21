from __future__ import annotations

import json
import unittest

from genos.drive_bridge import DRIVE_CONSUMER_SCOPE, DriveConnectionService


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RAW_TOKEN = "ya29.update-verify-fixture"


class Metadata:
    def __init__(self) -> None:
        self.value = None
        self.history: list[dict] = []

    def get_drive_binding(self):
        return None if self.value is None else dict(self.value)

    def upsert_drive_binding(self, payload):
        serialized = json.dumps(payload, sort_keys=True)
        assert RAW_TOKEN not in serialized
        self.value = dict(payload)
        self.history.append(dict(payload))
        return dict(payload)


class Credentials:
    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        assert secret_id == SECRET_ID
        assert consumer == DRIVE_CONSUMER_SCOPE
        return RAW_TOKEN


class Remote:
    def __init__(self) -> None:
        self.folders: dict[tuple[str, str], str] = {}
        self.files: dict[str, str] = {}
        self.names: dict[tuple[str, str], str] = {}
        self.writes: list[tuple[str, str]] = []

    def account_identity(self):
        return {"email": "owner@example.test", "permission_id": "permission-fixture"}

    def ensure_folder(self, *, name: str, parent_id: str | None = None) -> str:
        key = (parent_id or "root", name)
        if key not in self.folders:
            self.folders[key] = f"folder-{len(self.folders) + 1}"
        return self.folders[key]

    def upsert_text_file(self, *, name, parent_id, content, file_id=None, mime_type="text/plain; charset=utf-8"):
        key = (parent_id, name)
        target = file_id or self.names.get(key) or f"file-{len(self.files) + 1}"
        self.names[key] = target
        self.files[target] = content
        self.writes.append((name, target))
        return target

    def read_text_file(self, file_id: str) -> str:
        return self.files[file_id]


class UpdateVerificationTests(unittest.TestCase):
    def test_guided_connect_explicitly_verifies_update_before_instance_binding(self) -> None:
        store = Metadata()
        remote = Remote()
        service = DriveConnectionService(
            store=store,
            credentials=Credentials(),  # type: ignore[arg-type]
            remote_factory=lambda token: remote if token == RAW_TOKEN else None,  # type: ignore[arg-type,return-value]
            instance_id=INSTANCE_ID,
        )
        result = service.connect(secret_id=SECRET_ID)
        self.assertEqual(result["state"], "READY")
        states = [item["state"] for item in store.history]
        self.assertIn("WRITE_VERIFIED", states)
        self.assertIn("READ_VERIFIED", states)
        self.assertIn("UPDATE_VERIFIED", states)
        self.assertLess(states.index("READ_VERIFIED"), states.index("UPDATE_VERIFIED"))
        self.assertLess(states.index("UPDATE_VERIFIED"), states.index("INSTANCE_BOUND"))
        protocol_writes = [target for name, target in remote.writes if name == "PROTOCOL.json"]
        self.assertEqual(len(protocol_writes), 2)
        self.assertEqual(protocol_writes[0], protocol_writes[1])
        self.assertEqual(len(remote.writes), 3)


if __name__ == "__main__":
    unittest.main()
