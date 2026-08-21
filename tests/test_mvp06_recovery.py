from __future__ import annotations

import unittest

from genos.drive_system import DriveSystemServices


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class Metadata:
    def __init__(self) -> None:
        self.value = {"state": "DEGRADED"}

    def upsert_drive_binding(self, payload):
        self.value = dict(payload)
        return dict(payload)


class RecoverableConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.state = {
            "state": "DEGRADED",
            "instance_id": INSTANCE_ID,
            "secret_id": SECRET_ID,
            "root_name": "Custom GenOS Root",
            "root_folder_id": "root-1",
        }

    def status(self):
        return dict(self.state)

    def connect(self, *, secret_id: str, root_name: str = "GenOS"):
        self.calls.append((secret_id, root_name))
        self.state = {
            "state": "READY",
            "instance_id": INSTANCE_ID,
            "secret_id": secret_id,
            "root_name": root_name,
            "root_folder_id": "root-1",
            "reports_folder_id": "reports-1",
            "kanban_folder_id": "kanban-1",
            "index_file_id": "index-1",
            "protocol_file_id": "protocol-1",
            "protocol_version": "1.0",
            "schema_version": "1.0",
            "last_error_code": None,
        }
        return dict(self.state)


class Reports:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def publish(self, *, manual: bool = True):
        self.calls.append(manual)
        return {
            "state": "PUBLISHED",
            "remote_write": True,
            "fingerprint": "sha256:recovered",
            "job": {"job_id": "job-recovery"},
        }


class ScheduledRecoveryTests(unittest.TestCase):
    def test_degraded_binding_retries_guided_connect_with_persisted_root_name(self) -> None:
        connection = RecoverableConnection()
        reports = Reports()
        metadata = Metadata()
        services = DriveSystemServices(
            connection=connection,  # type: ignore[arg-type]
            reports=reports,  # type: ignore[arg-type]
            metadata=metadata,  # type: ignore[arg-type]
        )
        result = services.scheduled_scan()
        self.assertEqual(result["state"], "RECOVERED")
        self.assertTrue(result["remote_write"])
        self.assertEqual(connection.calls, [(SECRET_ID, "Custom GenOS Root")])
        self.assertEqual(reports.calls, [True])
        self.assertEqual(metadata.value["state"], "READY")

    def test_needs_action_binding_does_not_blindly_retry_remote_mutation(self) -> None:
        connection = RecoverableConnection()
        connection.state["state"] = "NEEDS_ACTION"
        reports = Reports()
        services = DriveSystemServices(
            connection=connection,  # type: ignore[arg-type]
            reports=reports,  # type: ignore[arg-type]
            metadata=Metadata(),  # type: ignore[arg-type]
        )
        result = services.scheduled_scan()
        self.assertEqual(result, {"state": "NEEDS_ACTION", "remote_write": False})
        self.assertEqual(connection.calls, [])
        self.assertEqual(reports.calls, [])


if __name__ == "__main__":
    unittest.main()
