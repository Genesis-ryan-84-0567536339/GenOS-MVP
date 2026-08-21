from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from genos.core_service import DRIVE_SCAN_INTERVAL_SECONDS, _scheduled_drive_scan
from genos.drive_bridge import DriveNeedsAction
from genos.drive_mcp import OptionalDriveMcpGrantProbe
from genos.drive_system import DriveSystemServices, HistoryAwareDriveReports
from genos.report_history import ReportHistoryStore


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RAW_TOKEN = "ya29.never-persist-this"


class FakeMetadata:
    def __init__(self) -> None:
        self.value = None
        self.history: list[dict] = []

    def upsert_drive_binding(self, payload):
        serialized = json.dumps(payload, sort_keys=True)
        assert RAW_TOKEN not in serialized
        self.value = dict(payload)
        self.history.append(dict(payload))
        return dict(payload)


class FakeConnection:
    def connect(self, *, secret_id: str, root_name: str = "GenOS"):
        assert secret_id == SECRET_ID
        return {
            "state": "READY",
            "instance_id": INSTANCE_ID,
            "secret_id": SECRET_ID,
            "root_folder_id": "root-folder",
            "reports_folder_id": "reports-folder",
            "kanban_folder_id": "kanban-folder",
            "protocol_file_id": "protocol-file",
            "index_file_id": "index-file",
            "protocol_version": "1.0",
            "schema_version": "1.0",
            "last_error_code": None,
        }

    def status(self):
        return {"state": "READY"}


class FakeReports:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def publish(self, *, manual: bool = True):
        self.calls.append(manual)
        return {"state": "PUBLISHED", "remote_write": True, "fingerprint": "sha256:fixture", "job": {"job_id": "job-fixture"}}


class McpGrantCompletionTests(unittest.TestCase):
    def test_optional_mcp_grant_stage_is_truthful_and_does_not_fake_pass(self) -> None:
        metadata = FakeMetadata()
        reports = FakeReports()
        services = DriveSystemServices(
            connection=FakeConnection(),  # type: ignore[arg-type]
            reports=reports,  # type: ignore[arg-type]
            metadata=metadata,  # type: ignore[arg-type]
        )
        result = services.connect(secret_id=SECRET_ID)
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["mcp_grant"]["state"], "NOT_CONFIGURED")
        self.assertFalse(result["mcp_grant"]["configured"])
        self.assertFalse(result["mcp_grant"]["credential_passthrough"])
        states = [item["state"] for item in metadata.history]
        self.assertIn("MCP_GRANT_TESTED", states)
        self.assertEqual(states[-1], "READY")
        self.assertEqual(metadata.value["mcp_grant_state"], "NOT_CONFIGURED")
        self.assertEqual(reports.calls, [True])

    def test_configured_failed_mcp_grant_blocks_ready_without_initial_report(self) -> None:
        class FailingProbe:
            def test(self, *, instance_id: str, root_folder_id: str):
                return {
                    "state": "FAIL",
                    "configured": True,
                    "agent_id": "agy-gen",
                    "scope": "drive-collaboration-replica",
                    "credential_passthrough": False,
                }

        metadata = FakeMetadata()
        reports = FakeReports()
        services = DriveSystemServices(
            connection=FakeConnection(),  # type: ignore[arg-type]
            reports=reports,  # type: ignore[arg-type]
            metadata=metadata,  # type: ignore[arg-type]
            mcp_grant_probe=FailingProbe(),  # type: ignore[arg-type]
        )
        with self.assertRaises(DriveNeedsAction):
            services.connect(secret_id=SECRET_ID)
        self.assertEqual(metadata.value["state"], "NEEDS_ACTION")
        self.assertEqual(metadata.value["last_error_code"], "MCP_GRANT_TEST_FAILED")
        self.assertEqual(reports.calls, [])

    def test_default_probe_contains_no_credential_grant(self) -> None:
        result = OptionalDriveMcpGrantProbe().test(instance_id=INSTANCE_ID, root_folder_id="root-folder")
        self.assertEqual(result["state"], "NOT_CONFIGURED")
        self.assertFalse(result["credential_passthrough"])
        self.assertNotIn("token", json.dumps(result).lower())


class ReportHistoryCompletionTests(unittest.TestCase):
    def _result(self, job_id: str, fingerprint: str) -> dict:
        return {
            "state": "PUBLISHED",
            "fingerprint": fingerprint,
            "job": {"job_id": job_id},
            "files": {"markdown": f"md-{job_id}", "json": f"json-{job_id}"},
            "access_token": RAW_TOKEN,
        }

    def test_history_records_initial_unchanged_and_changed_fingerprint_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ReportHistoryStore(Path(temp))
            first = store.record(self._result("job-1", "sha256:a"), manual=True)
            same = store.record(self._result("job-2", "sha256:a"), manual=True)
            changed = store.record(self._result("job-3", "sha256:b"), manual=True)
            self.assertEqual(first["diff"]["state"], "INITIAL")
            self.assertEqual(same["diff"]["state"], "UNCHANGED")
            self.assertEqual(changed["diff"]["state"], "CHANGED")
            self.assertEqual(changed["diff"]["previous_fingerprint"], "sha256:a")
            latest = store.latest()
            self.assertEqual(latest["job_id"], "job-3")
            disk = "\n".join(path.read_text(encoding="utf-8") for path in store.history_dir.glob("*.json"))
            self.assertNotIn(RAW_TOKEN, disk)

    def test_history_aware_report_wrapper_does_not_record_no_change_as_remote_history(self) -> None:
        class Inner:
            remote_factory = object()
            def __init__(self) -> None:
                self.calls = 0
            def publish(self, *, manual: bool = True):
                self.calls += 1
                return {
                    "state": "NO_CHANGE",
                    "remote_write": False,
                    "fingerprint": "sha256:a",
                    "job": {"job_id": "job-no-change"},
                }

        with tempfile.TemporaryDirectory() as temp:
            wrapped = HistoryAwareDriveReports(Inner(), ReportHistoryStore(Path(temp)))  # type: ignore[arg-type]
            result = wrapped.publish(manual=False)
            self.assertFalse(result["history"]["recorded"])
            self.assertEqual(result["history"]["diff"]["state"], "UNCHANGED")
            self.assertFalse((Path(temp) / "reports" / "latest.json").exists())


class DriveRetryQueueTests(unittest.TestCase):
    def test_worker_marks_drive_failure_as_retry_scheduled_without_throwing(self) -> None:
        with patch("genos.drive_system.build_drive_system", side_effect=RuntimeError("fixture outage")):
            result = _scheduled_drive_scan()
        self.assertEqual(result["state"], "RETRY_SCHEDULED")
        self.assertFalse(result["remote_write"])
        self.assertEqual(result["retry_after_seconds"], DRIVE_SCAN_INTERVAL_SECONDS)
        self.assertEqual(DRIVE_SCAN_INTERVAL_SECONDS, 30 * 60)


if __name__ == "__main__":
    unittest.main()
