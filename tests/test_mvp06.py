from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from genos.cli import build_parser
from genos.contracts import SupportClass
from genos.drive_bridge import DRIVE_CONSUMER_SCOPE, DriveConnectionService, DriveNeedsAction
from genos.drive_store import DriveStoreError, _clean_payload
from genos.observability import ObservabilityService
from genos.report_bridge import DriveReportService
from genos.state import JsonStateStore


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RAW_TOKEN = "ya29.fixture-super-secret-token"


class FakeMetadataStore:
    def __init__(self) -> None:
        self.value = None
        self.history = []

    def get_drive_binding(self):
        return None if self.value is None else dict(self.value)

    def upsert_drive_binding(self, payload):
        serialized = json.dumps(payload, sort_keys=True)
        assert RAW_TOKEN not in serialized
        self.value = dict(payload)
        self.history.append(dict(payload))
        return dict(payload)


class FakeCredentials:
    def __init__(self) -> None:
        self.calls = []

    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        self.calls.append((secret_id, consumer))
        if secret_id != SECRET_ID or consumer != DRIVE_CONSUMER_SCOPE:
            raise RuntimeError("unexpected credential resolution")
        return RAW_TOKEN


class FakeDriveRemote:
    def __init__(self, token: str) -> None:
        assert token == RAW_TOKEN
        self.folders = {}
        self.files = {}
        self.names = {}
        self.write_count = 0
        self.read_count = 0
        self.fail_writes = False

    def account_identity(self):
        return {"email": "owner@example.test", "permission_id": "permission-fixture"}

    def ensure_folder(self, *, name: str, parent_id: str | None = None) -> str:
        key = (parent_id or "root", name)
        if key not in self.folders:
            self.folders[key] = f"folder-{len(self.folders) + 1}"
        return self.folders[key]

    def upsert_text_file(self, *, name, parent_id, content, file_id=None, mime_type="text/plain; charset=utf-8"):
        if self.fail_writes:
            raise RuntimeError("fixture outage")
        key = (parent_id, name)
        target = file_id or self.names.get(key) or f"file-{len(self.files) + 1}"
        self.names[key] = target
        self.files[target] = content
        self.write_count += 1
        assert RAW_TOKEN not in content
        return target

    def read_text_file(self, file_id: str) -> str:
        self.read_count += 1
        return self.files[file_id]


class RemoteFactory:
    def __init__(self, remote: FakeDriveRemote) -> None:
        self.remote = remote
        self.calls = 0

    def __call__(self, token: str):
        self.calls += 1
        assert token == RAW_TOKEN
        return self.remote


class StubObservability:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {
            "schema_version": "1.2",
            "authority": "genos-observability-v1",
            "read_only": True,
            "generated_at": f"2026-08-21T00:00:0{self.calls}Z",
            "freshness": {"state": "FRESH", "observed_at": f"2026-08-21T00:00:0{self.calls}Z"},
            "health": {"state": "HEALTHY"},
            "observations": [
                {
                    "check_id": "platform",
                    "state": "PASS",
                    "observed": {"os": "ubuntu", "age_seconds": self.calls},
                    "source": "fixture",
                    "observed_at": f"2026-08-21T00:00:0{self.calls}Z",
                }
            ],
        }


class DriveConnectionTests(unittest.TestCase):
    def test_guided_connection_reaches_ready_with_idempotent_bootstrap(self) -> None:
        store = FakeMetadataStore()
        credentials = FakeCredentials()
        remote = FakeDriveRemote(RAW_TOKEN)
        factory = RemoteFactory(remote)
        service = DriveConnectionService(
            store=store,
            credentials=credentials,  # type: ignore[arg-type]
            remote_factory=factory,
            instance_id=INSTANCE_ID,
        )
        self.assertEqual(service.status()["state"], "UNCONFIGURED")
        result = service.connect(secret_id=SECRET_ID)
        self.assertEqual(result["state"], "READY")
        states = [item["state"] for item in store.history]
        for expected in ("NEEDS_AUTH", "AUTHENTICATED", "FOLDER_BOUND", "WRITE_VERIFIED", "READ_VERIFIED", "INSTANCE_BOUND", "READY"):
            self.assertIn(expected, states)
        self.assertEqual(credentials.calls, [(SECRET_ID, DRIVE_CONSUMER_SCOPE)])
        self.assertEqual(remote.write_count, 2)
        self.assertEqual(len(remote.folders), 3)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(RAW_TOKEN, serialized)
        self.assertEqual(json.loads(remote.files[result["protocol_file_id"]])["instance_id"], INSTANCE_ID)

        second = service.connect(secret_id=SECRET_ID)
        self.assertEqual(second["state"], "READY")
        self.assertEqual(len(remote.folders), 3)
        self.assertEqual(remote.write_count, 4)
        self.assertEqual(second["protocol_file_id"], result["protocol_file_id"])
        self.assertEqual(second["index_file_id"], result["index_file_id"])

    def test_ready_binding_rejects_silent_credential_rebind(self) -> None:
        store = FakeMetadataStore()
        credentials = FakeCredentials()
        remote = FakeDriveRemote(RAW_TOKEN)
        service = DriveConnectionService(
            store=store,
            credentials=credentials,  # type: ignore[arg-type]
            remote_factory=RemoteFactory(remote),
            instance_id=INSTANCE_ID,
        )
        service.connect(secret_id=SECRET_ID)
        with self.assertRaises(DriveNeedsAction):
            service.connect(secret_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")

    def test_binding_payload_rejects_raw_secret_fields(self) -> None:
        with self.assertRaises(DriveStoreError):
            _clean_payload({"instance_id": INSTANCE_ID, "state": "READY", "access_token": RAW_TOKEN})


class DriveObservabilityTests(unittest.TestCase):
    def test_ready_drive_binding_is_projected_from_read_only_binding_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text("{}\n", encoding="utf-8")
            calls = []

            def baseline(_cwd):
                return [], SupportClass.SUPPORTED, "fixture"

            def binding_reader():
                calls.append("read")
                return {
                    "state": "READY",
                    "account_email": "owner@example.test",
                    "root_folder_id": "folder-1",
                    "protocol_version": "1.0",
                    "last_verified_at": "2026-08-21T01:00:00Z",
                    "last_report_fingerprint": "sha256:fixture",
                }

            snapshot = ObservabilityService(
                state_root=root,
                baseline_collector=baseline,
                drive_binding_reader=binding_reader,
            ).snapshot()
            drive = next(item for item in snapshot["observations"] if item["check_id"] == "drive")
            self.assertEqual(drive["state"], "PASS")
            self.assertEqual(drive["observed"]["state"], "READY")
            self.assertEqual(drive["source"], "Product DB drive_binding read-only")
            self.assertEqual(calls, ["read"])
            self.assertIn("drive", snapshot["sections"]["integrations"])

    def test_uninstalled_host_does_not_touch_drive_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            touched = []

            def baseline(_cwd):
                return [], SupportClass.SUPPORTED, "fixture"

            def forbidden_reader():
                touched.append(True)
                raise AssertionError("uninstalled host must not query Drive binding")

            snapshot = ObservabilityService(
                state_root=Path(temp),
                baseline_collector=baseline,
                drive_binding_reader=forbidden_reader,
            ).snapshot()
            drive = next(item for item in snapshot["observations"] if item["check_id"] == "drive")
            self.assertEqual(drive["state"], "NOT_INSTALLED")
            self.assertEqual(touched, [])


class DriveCliContractTests(unittest.TestCase):
    def test_drive_connect_requires_typed_secretref_id(self) -> None:
        args = build_parser().parse_args(["drive", "connect", "--secret-id", SECRET_ID, "--json"])
        self.assertEqual(args.command, "drive")
        self.assertEqual(args.drive_command, "connect")
        self.assertEqual(args.secret_id, SECRET_ID)

    def test_scheduled_report_is_explicit(self) -> None:
        args = build_parser().parse_args(["report", "system", "--scheduled", "--json"])
        self.assertEqual(args.command, "report")
        self.assertTrue(args.scheduled)


class ReportBridgeTests(unittest.TestCase):
    def _ready_fixture(self):
        store = FakeMetadataStore()
        credentials = FakeCredentials()
        remote = FakeDriveRemote(RAW_TOKEN)
        factory = RemoteFactory(remote)
        connection = DriveConnectionService(
            store=store,
            credentials=credentials,  # type: ignore[arg-type]
            remote_factory=factory,
            instance_id=INSTANCE_ID,
        )
        connection.connect(secret_id=SECRET_ID)
        return store, credentials, remote, factory

    def test_scheduled_no_change_performs_zero_remote_writes(self) -> None:
        store, credentials, remote, factory = self._ready_fixture()
        observability = StubObservability()
        with tempfile.TemporaryDirectory() as temp:
            reports = DriveReportService(
                metadata_store=store,
                credentials=credentials,  # type: ignore[arg-type]
                remote_factory=factory,
                observability=observability,  # type: ignore[arg-type]
                jobs=JsonStateStore(Path(temp)),
            )
            first = reports.publish(manual=False)
            self.assertEqual(first["state"], "PUBLISHED")
            writes_after_first = remote.write_count
            factory_calls_after_first = factory.calls
            second = reports.publish(manual=False)
            self.assertEqual(second["state"], "NO_CHANGE")
            self.assertFalse(second["remote_write"])
            self.assertEqual(remote.write_count, writes_after_first)
            self.assertEqual(factory.calls, factory_calls_after_first)
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            loaded = JsonStateStore(Path(temp)).load_job(second["job"]["job_id"])
            self.assertEqual(loaded.state.value, "SUCCEEDED")
            self.assertEqual(loaded.evidence[-1]["code"], "NO_CHANGE")

    def test_manual_report_writes_even_when_fingerprint_is_unchanged(self) -> None:
        store, credentials, remote, factory = self._ready_fixture()
        observability = StubObservability()
        reports = DriveReportService(
            metadata_store=store,
            credentials=credentials,  # type: ignore[arg-type]
            remote_factory=factory,
            observability=observability,  # type: ignore[arg-type]
        )
        first = reports.publish(manual=False)
        before = remote.write_count
        second = reports.publish(manual=True)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["state"], "PUBLISHED")
        self.assertEqual(remote.write_count, before + 2)
        for content in remote.files.values():
            self.assertNotIn(RAW_TOKEN, content)

    def test_remote_outage_marks_replica_degraded_without_mutating_observability(self) -> None:
        store, credentials, remote, factory = self._ready_fixture()
        observability = StubObservability()
        reports = DriveReportService(
            metadata_store=store,
            credentials=credentials,  # type: ignore[arg-type]
            remote_factory=factory,
            observability=observability,  # type: ignore[arg-type]
        )
        remote.fail_writes = True
        with self.assertRaises(Exception):
            reports.publish(manual=True)
        self.assertEqual(store.value["state"], "DEGRADED")
        self.assertEqual(store.value["last_error_code"], "REPORT_REMOTE_WRITE_FAILED")
        self.assertEqual(observability.calls, 1)


if __name__ == "__main__":
    unittest.main()
