from __future__ import annotations

import json
import unittest

from genos.drive_bridge import DRIVE_CONSUMER_SCOPE, DriveConnectionService


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RAW_TOKEN = "ya29.fixture-token"


class MetadataStore:
    def __init__(self) -> None:
        self.value = {
            "state": "READY",
            "instance_id": INSTANCE_ID,
            "secret_id": SECRET_ID,
            "root_folder_id": "root-1",
            "reports_folder_id": "reports-1",
            "kanban_folder_id": "kanban-1",
            "index_file_id": "index-1",
            "protocol_file_id": "protocol-1",
            "report_markdown_file_id": "report-md-1",
            "report_json_file_id": "report-json-1",
            "protocol_version": "1.0",
            "schema_version": "1.0",
            "last_report_fingerprint": "sha256:report",
            "mcp_grant_state": "NOT_CONFIGURED",
            "mcp_grant_agent_id": "agy-gen",
            "mcp_grant_scope": "drive-collaboration-replica",
            "mcp_grant_checked_at": "2026-08-21T00:00:00Z",
            "last_verified_at": "2026-08-21T00:00:00Z",
            "last_error_code": None,
        }

    def get_drive_binding(self):
        return dict(self.value)

    def upsert_drive_binding(self, payload):
        self.value = dict(payload)
        return dict(payload)


class Credentials:
    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        assert secret_id == SECRET_ID
        assert consumer == DRIVE_CONSUMER_SCOPE
        return RAW_TOKEN


class Remote:
    def account_identity(self):
        return {"email": "owner@example.test", "permission_id": "permission-1"}

    def read_text_file(self, file_id: str) -> str:
        assert file_id == "protocol-1"
        return json.dumps({"instance_id": INSTANCE_ID})

    def ensure_folder(self, *, name: str, parent_id: str | None = None):
        raise AssertionError("verify must not create folders")

    def upsert_text_file(self, **_kwargs):
        raise AssertionError("verify must not write remote files")


class CheckpointPreservationTests(unittest.TestCase):
    def test_verify_preserves_report_and_mcp_binding_metadata(self) -> None:
        store = MetadataStore()
        service = DriveConnectionService(
            store=store,  # type: ignore[arg-type]
            credentials=Credentials(),  # type: ignore[arg-type]
            remote_factory=lambda token: Remote() if token == RAW_TOKEN else None,  # type: ignore[arg-type]
            instance_id=INSTANCE_ID,
        )
        result = service.verify()
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["report_markdown_file_id"], "report-md-1")
        self.assertEqual(result["report_json_file_id"], "report-json-1")
        self.assertEqual(result["last_report_fingerprint"], "sha256:report")
        self.assertEqual(result["mcp_grant_state"], "NOT_CONFIGURED")
        self.assertEqual(result["mcp_grant_agent_id"], "agy-gen")
        self.assertEqual(result["mcp_grant_scope"], "drive-collaboration-replica")
        self.assertEqual(result["mcp_grant_checked_at"], "2026-08-21T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
