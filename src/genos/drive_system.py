from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any
import uuid

from .auth_service import CredentialError, CredentialService
from .contracts import utc_now
from .drive_bridge import DriveConnectionService, DriveNeedsAction, DriveRemote
from .drive_mcp import DriveMcpGrantProbe, OptionalDriveMcpGrantProbe
from .drive_oauth import (
    DevicePoll,
    DeviceRequest,
    GoogleDriveDeviceAuthService,
    GoogleDriveRemoteFactory,
    GoogleOAuthClientConfig,
)
from .drive_store import PostgresDriveMetadataStore
from .observability import ObservabilityService
from .product_store import PostgresProductStore
from .report_bridge import DriveReportService
from .report_history import ReportHistoryStore
from .secret_provider import LocalFileSecretProvider
from .state import JsonStateStore


class DriveSystemError(RuntimeError):
    pass


class HistoryAwareDriveReports:
    def __init__(self, reports: DriveReportService, history: ReportHistoryStore) -> None:
        self.reports = reports
        self.history = history
        self.remote_factory = reports.remote_factory

    def publish(self, *, manual: bool = True) -> dict[str, Any]:
        result = dict(self.reports.publish(manual=manual))
        if result.get("state") == "PUBLISHED":
            result["history"] = self.history.record(result, manual=manual)
        elif result.get("state") == "NO_CHANGE":
            result["history"] = {
                "recorded": False,
                "reason": "NO_CHANGE",
                "diff": {
                    "state": "UNCHANGED",
                    "previous_fingerprint": result.get("fingerprint"),
                    "current_fingerprint": result.get("fingerprint"),
                },
            }
        return result


@dataclass(frozen=True, slots=True)
class DriveSystemServices:
    connection: DriveConnectionService
    reports: HistoryAwareDriveReports
    metadata: PostgresDriveMetadataStore
    oauth: GoogleDriveDeviceAuthService | None = None
    credentials: CredentialService | None = None
    mcp_grant_probe: DriveMcpGrantProbe = field(default_factory=OptionalDriveMcpGrantProbe)

    def connect(self, *, secret_id: str, root_name: str = "GenOS") -> dict[str, Any]:
        drive = self.connection.connect(secret_id=secret_id, root_name=root_name)
        instance_id = _required_text(drive, "instance_id")
        root_folder_id = _required_text(drive, "root_folder_id")
        try:
            mcp_grant = dict(self.mcp_grant_probe.test(instance_id=instance_id, root_folder_id=root_folder_id))
        except Exception:
            mcp_grant = {
                "state": "UNKNOWN",
                "configured": None,
                "agent_id": "agy-gen",
                "scope": "drive-collaboration-replica",
                "credential_passthrough": False,
                "reason": "MCP_GRANT_PROBE_UNAVAILABLE",
            }
        checkpoint = dict(drive)
        checkpoint.update(
            {
                "state": "MCP_GRANT_TESTED",
                "mcp_grant_state": str(mcp_grant.get("state") or "UNKNOWN"),
                "mcp_grant_agent_id": _optional_text(mcp_grant.get("agent_id")),
                "mcp_grant_scope": _optional_text(mcp_grant.get("scope")),
                "mcp_grant_checked_at": utc_now(),
            }
        )
        checkpoint = self.metadata.upsert_drive_binding(checkpoint)
        if mcp_grant.get("configured") is True and mcp_grant.get("state") != "PASS":
            blocked = dict(checkpoint)
            blocked.update({"state": "NEEDS_ACTION", "last_error_code": "MCP_GRANT_TEST_FAILED"})
            self.metadata.upsert_drive_binding(blocked)
            raise DriveNeedsAction("Configured Drive MCP grant test failed")
        ready = dict(checkpoint)
        ready.update({"state": "READY", "last_error_code": None})
        ready = self.metadata.upsert_drive_binding(ready)
        initial_report = self.reports.publish(manual=True)
        return {
            "state": "READY",
            "drive": ready,
            "mcp_grant": mcp_grant,
            "initial_report": initial_report,
        }

    def oauth_start(self, *, root_name: str = "GenOS") -> dict[str, Any]:
        return self._oauth().start(root_name=root_name)

    def oauth_status(self) -> dict[str, Any]:
        return self._oauth().status()

    def oauth_poll(self) -> dict[str, Any]:
        auth = self._oauth().poll()
        if auth.get("state") != "AUTHORIZED":
            return {"state": str(auth.get("state") or "UNKNOWN"), "auth": auth}
        secret_id = _required_text(auth, "secret_id")
        root_name = _optional_text(auth.get("root_name")) or "GenOS"
        current = self.connection.status()
        if current.get("state") == "READY" and current.get("secret_id") == secret_id:
            return {"state": "READY", "auth": auth, "connection": {"state": "READY", "drive": current}}
        connection = self.connect(secret_id=secret_id, root_name=root_name)
        return {"state": "READY", "auth": auth, "connection": connection}

    def disconnect(self) -> dict[str, Any]:
        current = self.connection.status()
        secret_id = _optional_text(current.get("secret_id"))
        if secret_id and self.credentials is not None:
            try:
                self.credentials.disable(secret_id)
            except CredentialError as exc:
                raise DriveNeedsAction("Drive credential could not be disabled") from exc
        instance_id = _required_text(current, "instance_id")
        root_name = _optional_text(current.get("root_name")) or "GenOS"
        disconnected = self.metadata.upsert_drive_binding(
            {
                "state": "DISCONNECTED",
                "instance_id": instance_id,
                "secret_id": None,
                "root_name": root_name,
                "last_error_code": None,
            }
        )
        if self.oauth is not None:
            self.oauth.clear(state="DISCONNECTED")
        return {
            "state": "DISCONNECTED",
            "remote_content_deleted": False,
            "drive": disconnected,
        }

    def reauthorize(self, *, root_name: str | None = None) -> dict[str, Any]:
        current = self.connection.status()
        selected_root = root_name or _optional_text(current.get("root_name")) or "GenOS"
        self.disconnect()
        return self.oauth_start(root_name=selected_root)

    def reconnect(self, *, root_name: str | None = None) -> dict[str, Any]:
        current = self.connection.status()
        state = str(current.get("state") or "UNCONFIGURED")
        selected_root = root_name or _optional_text(current.get("root_name")) or "GenOS"
        secret_id = _optional_text(current.get("secret_id"))
        if state == "READY":
            return {"state": "READY", "drive": current}
        if state == "DEGRADED" and secret_id:
            return self.connect(secret_id=secret_id, root_name=selected_root)
        return self.oauth_start(root_name=selected_root)

    def scheduled_scan(self) -> dict[str, Any]:
        status = self.connection.status()
        drive_state = str(status.get("state") or "UNKNOWN")
        if drive_state == "DEGRADED":
            secret_id = _required_text(status, "secret_id")
            root_name = _optional_text(status.get("root_name")) or "GenOS"
            recovered = self.connect(secret_id=secret_id, root_name=root_name)
            initial_report = recovered.get("initial_report") if isinstance(recovered.get("initial_report"), dict) else {}
            return {
                "state": "RECOVERED",
                "remote_write": bool(initial_report.get("remote_write", False)),
                "report": initial_report,
            }
        if drive_state == "NEEDS_ACTION":
            return {"state": "NEEDS_ACTION", "remote_write": False}
        if drive_state != "READY":
            return {"state": "NOT_CONFIGURED", "remote_write": False}
        return self.reports.publish(manual=False)

    def _oauth(self) -> GoogleDriveDeviceAuthService:
        if self.oauth is None:
            raise DriveNeedsAction("Google Drive OAuth service is not configured")
        return self.oauth


def build_drive_system(
    *,
    product_store: PostgresProductStore | None = None,
    credentials: CredentialService | None = None,
    observability: ObservabilityService | None = None,
    remote_factory: Callable[[str], DriveRemote] | None = None,
    mcp_grant_probe: DriveMcpGrantProbe | None = None,
    oauth_client_config: GoogleOAuthClientConfig | None = None,
    oauth_device_request: DeviceRequest | None = None,
    oauth_device_poll: DevicePoll | None = None,
) -> DriveSystemServices:
    store = product_store or PostgresProductStore()
    store.ensure_schema()
    metadata = PostgresDriveMetadataStore(store)
    metadata.ensure_schema()
    if credentials is None:
        secret_root = os.environ.get("GENOS_SECRET_DIR", "/var/lib/genos/secrets")
        credentials = CredentialService(store, LocalFileSecretProvider(secret_root))
    state_root = Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
    shared_remote_factory = remote_factory or GoogleDriveRemoteFactory()
    connection = DriveConnectionService(
        store=metadata,
        credentials=credentials,
        remote_factory=shared_remote_factory,
        instance_id=system_instance_id(),
    )
    report_service = DriveReportService(
        metadata_store=metadata,
        credentials=credentials,
        remote_factory=shared_remote_factory,
        observability=observability or ObservabilityService(state_root=state_root),
        jobs=JsonStateStore(state_root),
    )
    reports = HistoryAwareDriveReports(report_service, ReportHistoryStore(state_root))
    resolved_client_config = oauth_client_config
    if resolved_client_config is None:
        resolved_client_config = GoogleOAuthClientConfig.from_environment()
    oauth = GoogleDriveDeviceAuthService(
        credentials=credentials,
        client_config=resolved_client_config,
        device_request=oauth_device_request,
        device_poll=oauth_device_poll,
    )
    return DriveSystemServices(
        connection=connection,
        reports=reports,
        metadata=metadata,
        oauth=oauth,
        credentials=credentials,
        mcp_grant_probe=mcp_grant_probe or OptionalDriveMcpGrantProbe(),
    )


def system_instance_id() -> str:
    candidates = [os.environ.get("GENOS_INSTANCE_ID")]
    path = Path("/etc/genos/instance-id")
    if path.is_file():
        try:
            candidates.append(path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return str(uuid.UUID(candidate.strip()))
        except ValueError:
            continue
    raise DriveSystemError("GenOS instance id is unavailable")


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DriveSystemError(f"Drive connection result is missing {key}")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
