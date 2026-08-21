from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
import uuid

from .auth_service import CredentialService
from .drive_bridge import DriveConnectionService, DriveRemote
from .drive_oauth import GoogleDriveRemoteFactory
from .drive_store import PostgresDriveMetadataStore
from .observability import ObservabilityService
from .product_store import PostgresProductStore
from .report_bridge import DriveReportService
from .secret_provider import LocalFileSecretProvider
from .state import JsonStateStore


class DriveSystemError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DriveSystemServices:
    connection: DriveConnectionService
    reports: DriveReportService
    metadata: PostgresDriveMetadataStore

    def connect(self, *, secret_id: str, root_name: str = "GenOS") -> dict[str, Any]:
        drive = self.connection.connect(secret_id=secret_id, root_name=root_name)
        initial_report = self.reports.publish(manual=True)
        return {"state": drive.get("state", "UNKNOWN"), "drive": drive, "initial_report": initial_report}

    def scheduled_scan(self) -> dict[str, Any]:
        status = self.connection.status()
        if status.get("state") != "READY":
            return {"state": "NOT_CONFIGURED", "remote_write": False}
        return self.reports.publish(manual=False)


def build_drive_system(
    *,
    product_store: PostgresProductStore | None = None,
    credentials: CredentialService | None = None,
    observability: ObservabilityService | None = None,
    remote_factory: Callable[[str], DriveRemote] | None = None,
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
    reports = DriveReportService(
        metadata_store=metadata,
        credentials=credentials,
        remote_factory=shared_remote_factory,
        observability=observability or ObservabilityService(state_root=state_root),
        jobs=JsonStateStore(state_root),
    )
    return DriveSystemServices(connection=connection, reports=reports, metadata=metadata)


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
