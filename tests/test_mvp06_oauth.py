from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import genos.drive_oauth as oauth_module
from genos.cli import main
from genos.drive_bridge import DRIVE_CONSUMER_SCOPE, DriveNeedsAction
from genos.drive_oauth import (
    GOOGLE_DRIVE_FILE_SCOPE,
    GoogleDriveDeviceAuthService,
    GoogleDriveRemoteFactory,
    GoogleOAuthClientConfig,
)
from genos.drive_system import DriveSystemServices, build_drive_system


INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
SECRET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CLIENT_ID = "fixture-client.apps.googleusercontent.com"
CLIENT_SECRET = "fixture-app-client-secret"
DEVICE_CODE = "fixture-private-device-code"
REFRESH_TOKEN = "fixture-user-refresh-token"
ACCESS_TOKEN = "ya29.fixture-ephemeral-access-token"


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _RecordingCredentials:
    def __init__(self) -> None:
        self.add_calls = []
        self.disabled = []

    def add(self, **kwargs):
        self.add_calls.append(dict(kwargs))
        return {
            "secret_id": SECRET_ID,
            "name": kwargs["name"],
            "provider": kwargs["provider_name"],
            "consumer_scopes": kwargs["consumer_scopes"],
        }

    def disable(self, secret_id: str):
        self.disabled.append(secret_id)
        return {"secret_id": secret_id, "status": "DISABLED"}


class GoogleDriveRefreshCredentialTests(unittest.TestCase):
    def test_refresh_bundle_exchanges_ephemeral_token_without_echoing_bundle(self) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def exchange(client_id: str, refresh_token: str, client_secret: str | None) -> str:
            calls.append((client_id, refresh_token, client_secret))
            return ACCESS_TOKEN

        raw = json.dumps(
            {
                "client_id": CLIENT_ID,
                "refresh_token": REFRESH_TOKEN,
                "client_secret": CLIENT_SECRET,
            }
        )
        access_token, descriptor = GoogleDriveRemoteFactory(token_exchange=exchange).resolve(raw)
        self.assertEqual(access_token, ACCESS_TOKEN)
        self.assertEqual(calls, [(CLIENT_ID, REFRESH_TOKEN, CLIENT_SECRET)])
        self.assertEqual(descriptor.mode, "oauth_refresh")
        self.assertTrue(descriptor.refresh_capable)
        self.assertEqual(descriptor.recommended_scope, GOOGLE_DRIVE_FILE_SCOPE)
        public = json.dumps(descriptor.to_dict(), sort_keys=True)
        for secret in (REFRESH_TOKEN, CLIENT_SECRET, ACCESS_TOKEN):
            self.assertNotIn(secret, public)

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


class GoogleDriveDeviceAuthorizationTests(unittest.TestCase):
    def _config(self) -> GoogleOAuthClientConfig:
        return GoogleOAuthClientConfig(CLIENT_ID, CLIENT_SECRET)

    def _start_payload(self):
        return {
            "device_code": DEVICE_CODE,
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.google.com/device",
            "expires_in": 1800,
            "interval": 5,
        }

    def test_environment_config_is_distribution_identity_not_user_credential(self) -> None:
        config = GoogleOAuthClientConfig.from_environment(
            {
                "GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_ID": CLIENT_ID,
                "GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
            }
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.client_id, CLIENT_ID)
        self.assertEqual(config.scope, GOOGLE_DRIVE_FILE_SCOPE)
        self.assertIsNone(GoogleOAuthClientConfig.from_environment({"IGNORED": "1"}))
        self.assertIsNone(
            GoogleOAuthClientConfig.from_environment({"GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_ID": CLIENT_ID})
        )

    def test_start_projects_only_user_visible_google_fields(self) -> None:
        credentials = _RecordingCredentials()
        service = GoogleDriveDeviceAuthService(
            credentials=credentials,  # type: ignore[arg-type]
            client_config=self._config(),
            device_request=lambda _config: self._start_payload(),
            device_poll=lambda _config, _device: {},
            clock=_Clock(),
        )
        result = service.start(root_name="GenOS User")
        self.assertEqual(result["state"], "WAITING_USER")
        self.assertEqual(result["verification_url"], "https://www.google.com/device")
        self.assertEqual(result["user_code"], "ABCD-EFGH")
        self.assertEqual(result["scope"], GOOGLE_DRIVE_FILE_SCOPE)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(DEVICE_CODE, serialized)
        self.assertNotIn(CLIENT_SECRET, serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("refresh_token", serialized)
        self.assertEqual(credentials.add_calls, [])

    def test_poll_respects_interval_pending_slow_down_then_persists_refresh_secretref(self) -> None:
        clock = _Clock()
        credentials = _RecordingCredentials()
        provider_calls = []
        outcomes = [
            oauth_module._AuthorizationPending(),
            oauth_module._SlowDown(),
            {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "token_type": "Bearer",
                "scope": GOOGLE_DRIVE_FILE_SCOPE,
            },
        ]

        def poll(_config, device_code):
            provider_calls.append(device_code)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service = GoogleDriveDeviceAuthService(
            credentials=credentials,  # type: ignore[arg-type]
            client_config=self._config(),
            device_request=lambda _config: self._start_payload(),
            device_poll=poll,
            clock=clock,
        )
        service.start(root_name="GenOS")

        early = service.poll()
        self.assertEqual(early["state"], "WAITING_USER")
        self.assertEqual(provider_calls, [], "poll before provider interval must perform no remote call")

        clock.advance(5)
        pending = service.poll()
        self.assertEqual(pending["state"], "WAITING_USER")
        self.assertEqual(provider_calls, [DEVICE_CODE])

        clock.advance(5)
        slowed = service.poll()
        self.assertEqual(slowed["state"], "WAITING_USER")
        self.assertEqual(slowed["reason"], "SLOW_DOWN")
        self.assertEqual(slowed["poll_interval_seconds"], 10)

        clock.advance(10)
        authorized = service.poll()
        self.assertEqual(authorized["state"], "AUTHORIZED")
        self.assertEqual(authorized["secret_id"], SECRET_ID)
        self.assertEqual(len(credentials.add_calls), 1)
        added = credentials.add_calls[0]
        self.assertEqual(added["provider_name"], "google-drive")
        self.assertEqual(added["consumer_scopes"], [DRIVE_CONSUMER_SCOPE])
        self.assertEqual(added["source"], "google-device-oauth")
        persisted = json.loads(added["raw_secret"])
        self.assertEqual(persisted["client_id"], CLIENT_ID)
        self.assertEqual(persisted["client_secret"], CLIENT_SECRET)
        self.assertEqual(persisted["refresh_token"], REFRESH_TOKEN)
        self.assertNotIn("access_token", persisted)
        public = json.dumps(authorized, sort_keys=True)
        for secret in (DEVICE_CODE, CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN):
            self.assertNotIn(secret, public)

    def test_denial_and_expiry_are_truthful_terminal_states(self) -> None:
        for outcome, expected in [
            (oauth_module._AccessDenied(), "DENIED"),
            (oauth_module._AuthorizationExpired(), "EXPIRED"),
        ]:
            with self.subTest(expected=expected):
                clock = _Clock()

                def poll(_config, _device, outcome=outcome):
                    raise outcome

                service = GoogleDriveDeviceAuthService(
                    credentials=_RecordingCredentials(),  # type: ignore[arg-type]
                    client_config=self._config(),
                    device_request=lambda _config: self._start_payload(),
                    device_poll=poll,
                    clock=clock,
                )
                service.start()
                clock.advance(5)
                result = service.poll()
                self.assertEqual(result["state"], expected)
                self.assertIsNone(result["user_code"])

    def test_local_expiry_never_calls_provider_after_deadline(self) -> None:
        clock = _Clock()
        calls = []
        payload = self._start_payload()
        payload["expires_in"] = 30
        service = GoogleDriveDeviceAuthService(
            credentials=_RecordingCredentials(),  # type: ignore[arg-type]
            client_config=self._config(),
            device_request=lambda _config: payload,
            device_poll=lambda *_args: calls.append(True) or {},
            clock=clock,
        )
        service.start()
        clock.advance(31)
        self.assertEqual(service.status()["state"], "EXPIRED")
        self.assertEqual(service.poll()["state"], "EXPIRED")
        self.assertEqual(calls, [])

    def test_missing_distribution_oauth_client_is_typed_needs_action(self) -> None:
        service = GoogleDriveDeviceAuthService(
            credentials=_RecordingCredentials(),  # type: ignore[arg-type]
            client_config=None,
        )
        self.assertEqual(service.status()["state"], "NOT_CONFIGURED")
        with self.assertRaises(DriveNeedsAction):
            service.start()


class _StubConnection:
    def __init__(self, *, state: str = "READY", secret_id: str | None = None) -> None:
        self.state = state
        self.secret_id = secret_id
        self.connect_calls: list[tuple[str, str]] = []

    def connect(self, *, secret_id: str, root_name: str = "GenOS"):
        self.connect_calls.append((secret_id, root_name))
        self.state = "READY"
        self.secret_id = secret_id
        return {
            "state": "READY",
            "instance_id": INSTANCE_ID,
            "secret_id": secret_id,
            "root_name": root_name,
            "root_folder_id": "root-fixture",
            "reports_folder_id": "reports-fixture",
            "kanban_folder_id": "kanban-fixture",
            "protocol_file_id": "protocol-fixture",
            "index_file_id": "index-fixture",
            "protocol_version": "1.0",
            "schema_version": "1.0",
            "last_error_code": None,
        }

    def status(self):
        return {
            "state": self.state,
            "instance_id": INSTANCE_ID,
            "secret_id": self.secret_id,
            "root_name": "GenOS",
        }

    def verify(self):
        return {"state": self.state}


class _StubReports:
    def __init__(self) -> None:
        self.manual_calls: list[bool] = []

    def publish(self, *, manual: bool = True):
        self.manual_calls.append(manual)
        return {"state": "PUBLISHED" if manual else "NO_CHANGE", "remote_write": manual}


class _StubMetadata:
    def __init__(self) -> None:
        self.value = None
        self.history = []

    def get_drive_binding(self):
        return None if self.value is None else dict(self.value)

    def upsert_drive_binding(self, payload):
        self.value = dict(payload)
        self.history.append(dict(payload))
        return dict(payload)


class _StubOAuth:
    def __init__(self, projection=None) -> None:
        self.projection = projection or {"state": "UNCONFIGURED"}
        self.starts = []
        self.clears = []

    def start(self, *, root_name="GenOS"):
        self.starts.append(root_name)
        self.projection = {"state": "WAITING_USER", "root_name": root_name, "user_code": "ABCD-EFGH"}
        return dict(self.projection)

    def status(self):
        return dict(self.projection)

    def poll(self):
        return dict(self.projection)

    def clear(self, *, state="DISCONNECTED", reason=None):
        self.clears.append((state, reason))
        self.projection = {"state": state, "reason": reason}
        return dict(self.projection)


class DriveSystemWiringTests(unittest.TestCase):
    def test_connect_publishes_initial_report_and_scheduled_scan_uses_nonmanual_path(self) -> None:
        connection = _StubConnection()
        reports = _StubReports()
        metadata = _StubMetadata()
        services = DriveSystemServices(connection=connection, reports=reports, metadata=metadata)  # type: ignore[arg-type]
        result = services.connect(secret_id=SECRET_ID, root_name="GenOS")
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["mcp_grant"]["state"], "NOT_CONFIGURED")
        self.assertEqual(result["initial_report"]["state"], "PUBLISHED")
        self.assertEqual(connection.connect_calls, [(SECRET_ID, "GenOS")])
        self.assertEqual(reports.manual_calls, [True])
        self.assertEqual(metadata.value["state"], "READY")

        scheduled = services.scheduled_scan()
        self.assertEqual(scheduled["state"], "NO_CHANGE")
        self.assertEqual(reports.manual_calls, [True, False])

    def test_authorized_oauth_poll_continues_through_drive_bootstrap_and_initial_report(self) -> None:
        connection = _StubConnection(state="UNCONFIGURED")
        reports = _StubReports()
        metadata = _StubMetadata()
        oauth = _StubOAuth({"state": "AUTHORIZED", "secret_id": SECRET_ID, "root_name": "GenOS"})
        services = DriveSystemServices(
            connection=connection,
            reports=reports,
            metadata=metadata,
            oauth=oauth,  # type: ignore[arg-type]
        )
        result = services.oauth_poll()
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["connection"]["initial_report"]["state"], "PUBLISHED")
        self.assertEqual(connection.connect_calls, [(SECRET_ID, "GenOS")])

    def test_disconnect_disables_secretref_and_does_not_delete_remote_content(self) -> None:
        connection = _StubConnection(state="READY", secret_id=SECRET_ID)
        metadata = _StubMetadata()
        credentials = _RecordingCredentials()
        oauth = _StubOAuth({"state": "AUTHORIZED", "secret_id": SECRET_ID})
        services = DriveSystemServices(
            connection=connection,
            reports=_StubReports(),
            metadata=metadata,
            oauth=oauth,  # type: ignore[arg-type]
            credentials=credentials,  # type: ignore[arg-type]
        )
        result = services.disconnect()
        self.assertEqual(result["state"], "DISCONNECTED")
        self.assertFalse(result["remote_content_deleted"])
        self.assertEqual(credentials.disabled, [SECRET_ID])
        self.assertIsNone(metadata.value["secret_id"])
        self.assertEqual(metadata.value["state"], "DISCONNECTED")

    def test_authorize_requires_explicit_reauthorize_when_drive_is_ready(self) -> None:
        services = DriveSystemServices(
            connection=_StubConnection(state="READY", secret_id=SECRET_ID),
            reports=_StubReports(),
            metadata=_StubMetadata(),
            oauth=_StubOAuth(),  # type: ignore[arg-type]
        )
        with self.assertRaises(DriveNeedsAction):
            services.oauth_start()

    def test_reconnect_needs_action_disables_old_secret_and_starts_new_user_authorization(self) -> None:
        connection = _StubConnection(state="NEEDS_ACTION", secret_id=SECRET_ID)
        credentials = _RecordingCredentials()
        oauth = _StubOAuth()
        services = DriveSystemServices(
            connection=connection,
            reports=_StubReports(),
            metadata=_StubMetadata(),
            oauth=oauth,  # type: ignore[arg-type]
            credentials=credentials,  # type: ignore[arg-type]
        )
        result = services.reconnect()
        self.assertEqual(result["state"], "WAITING_USER")
        self.assertEqual(credentials.disabled, [SECRET_ID])
        self.assertEqual(oauth.starts, ["GenOS"])

    def test_unconfigured_scheduled_scan_does_not_publish(self) -> None:
        connection = _StubConnection(state="UNCONFIGURED")
        reports = _StubReports()
        services = DriveSystemServices(connection=connection, reports=reports, metadata=_StubMetadata())  # type: ignore[arg-type]
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
                oauth_client_config=GoogleOAuthClientConfig(CLIENT_ID, CLIENT_SECRET),
            )
        self.assertEqual(store.ensure_calls, 1)
        self.assertTrue(any("drive_binding" in sql for sql in store.sql_calls))
        self.assertIs(services.connection.remote_factory, shared)
        self.assertIs(services.reports.remote_factory, shared)
        self.assertEqual(services.oauth_status()["state"], "UNCONFIGURED")

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
