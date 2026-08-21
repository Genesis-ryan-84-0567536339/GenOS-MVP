from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from genos.cli import main
from genos.drive_bridge import DriveNeedsAction
from genos.drive_oauth import GOOGLE_DRIVE_FILE_SCOPE, GoogleDriveRemoteFactory
from genos.drive_system import DriveSystemServices, build_drive_system


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class GoogleDriveOAuthTests(unittest.TestCase):
    def test_refresh_bundle_exchanges_ephemeral_token_without_echoing_bundle(self) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def exchange(client_id: str, refresh_token: str, client_secret: str | None) -> str:
            calls.append((client_id, refresh_token, client_secret))
            return "ya29.ephemeral-access-token"

        raw = json.dumps(
            {
                "client_id": "client-id-fixture.apps.googleusercontent.com",
                "refresh_token": "refresh-token-fixture",
                "client_secret": "client-secret-fixture",
            }
        )
        access_token, descriptor = GoogleDriveRemoteFactory(token_exchange=exchange).resolve(raw)
        self.assertEqual(access_token, "ya29.ephemeral-access-token")
        self.assertEqual(
            calls,
            [("client-id-fixture.apps.googleusercontent.com", "refresh-token-fixture", "client-secret-fixture")],
        )
        self.assertEqual(descriptor.mode, "oauth_refresh")
        self.assertTrue(descriptor.refresh_capable)
        self.assertEqual(descriptor.recommended_scope, GOOGLE_DRIVE_FILE_SCOPE)
        public = json.dumps(descriptor.to_dict(), sort_keys=True)
        self.assertNotIn("refresh-token-fixture", public)
        self.assertNotIn("client-secret-fixture", public)
        self.assertNotIn("ya29.ephemeral-access-token", public)

    def test_access_token_compatibility_mode_does_not_call_refresh_endpoint(self) -> None:
        def forbidden_exchange(_client_id: str, _refresh_token: str, _client_secret: str | None) -> str:
            raise AssertionError("compatibility access token must not trigger refresh")

        token, descriptor = GoogleDriveRemoteFactory(token_exchange=forbidden_exchange).resolve("ya29.short-lived-token")
        self.assertEqual(token, "ya29.short-lived-token")
        self.assertEqual(descriptor.mode, "access_token")
        self.assertFalse(descriptor.refresh_capable)

    def test_incomplete_refresh_bundle_requires_action(self) -> None:
        factory = GoogleDriveRemoteFactory(token_exchange=lambda *_args: "unexpected")
        with self.assertRaises(DriveNeedsAction):
            factory.resolve(json.dumps({"client_id": "client-only"}))
        with self.assertRaises(DriveNeedsAction):
            factory.resolve("{not-json")


class _StubConnection:
    def __init__(self, *, state: str = "READY") -> None:
        self.state = state
        self.connect_calls: list[tuple[str, str]] = []

    def connect(self, *, secret_id: str, root_name: str = "GenOS"):
        self.connect_calls.append((secret_id, root_name))
        return {"state": "READY", "root_folder_id": "root-fixture"}

    def status(self):
        return {"state": self.state}

    def verify(self):
        return {"state": self.state}


class _StubReports:
    def __init__(self) -> None:
        self.manual_calls: list[bool] = []

    def publish(self, *, manual: bool = True):
        self.manual_calls.append(manual)
        return {"state": "PUBLISHED" if manual else "NO_CHANGE", "remote_write": manual}


class DriveSystemWiringTests(unittest.TestCase):
    def test_connect_publishes_initial_report_and_scheduled_scan_uses_nonmanual_path(self) -> None:
        connection = _StubConnection()
        reports = _StubReports()
        services = DriveSystemServices(connection=connection, reports=reports, metadata=object())  # type: ignore[arg-type]
        result = services.connect(secret_id=SECRET_ID, root_name="GenOS")
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["initial_report"]["state"], "PUBLISHED")
        self.assertEqual(connection.connect_calls, [(SECRET_ID, "GenOS")])
        self.assertEqual(reports.manual_calls, [True])

        scheduled = services.scheduled_scan()
        self.assertEqual(scheduled["state"], "NO_CHANGE")
        self.assertEqual(reports.manual_calls, [True, False])

    def test_unconfigured_scheduled_scan_does_not_publish(self) -> None:
        connection = _StubConnection(state="UNCONFIGURED")
        reports = _StubReports()
        services = DriveSystemServices(connection=connection, reports=reports, metadata=object())  # type: ignore[arg-type]
        result = services.scheduled_scan()
        self.assertEqual(result, {"state": "NOT_CONFIGURED", "remote_write": False})
        self.assertEqual(reports.manual_calls, [])

    def test_build_drive_system_shares_one_remote_factory_between_connection_and_reports(self) -> None:
        class FakeProductStore:
            def __init__(self) -> None:
                self.ensure_calls = 0
                self.sql_calls: list[str] = []

            def ensure_schema(self) -> None:
                self.ensure_calls += 1

            def _execute(self, sql: str, **_kwargs):
                self.sql_calls.append(sql)
                return ""

        class FakeCredentials:
            pass

        class FakeObservability:
            def snapshot(self):
                return {"authority": "genos-observability-v1"}

        class SharedFactory:
            def __call__(self, _raw_secret: str):
                raise AssertionError("factory should not be invoked during composition")

        store = FakeProductStore()
        shared = SharedFactory()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"GENOS_INSTANCE_ID": INSTANCE_ID, "GENOS_STATE_DIR": temp},
            clear=False,
        ):
            services = build_drive_system(
                product_store=store,  # type: ignore[arg-type]
                credentials=FakeCredentials(),  # type: ignore[arg-type]
                observability=FakeObservability(),  # type: ignore[arg-type]
                remote_factory=shared,
            )
        self.assertEqual(store.ensure_calls, 1)
        self.assertTrue(any("drive_binding" in sql for sql in store.sql_calls))
        self.assertIs(services.connection.remote_factory, shared)
        self.assertIs(services.reports.remote_factory, shared)

    def test_cli_drive_connect_uses_system_connect_wrapper(self) -> None:
        class ConnectionThatMustNotConnect(_StubConnection):
            def connect(self, *, secret_id: str, root_name: str = "GenOS"):
                raise AssertionError("CLI must use DriveSystemServices.connect so initial report is included")

        class FakeServices:
            def __init__(self) -> None:
                self.connection = ConnectionThatMustNotConnect()
                self.calls: list[tuple[str, str]] = []

            def connect(self, *, secret_id: str, root_name: str = "GenOS"):
                self.calls.append((secret_id, root_name))
                return {"state": "READY", "drive": {"state": "READY"}, "initial_report": {"state": "PUBLISHED"}}

        services = FakeServices()
        output = io.StringIO()
        with patch("genos.cli.build_drive_system", return_value=services), redirect_stdout(output):
            rc = main(["drive", "connect", "--secret-id", SECRET_ID, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [(SECRET_ID, "GenOS")])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["initial_report"]["state"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
