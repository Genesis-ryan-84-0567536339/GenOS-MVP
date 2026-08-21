from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid

from genos.auth_service import CredentialService
from genos.drive_bridge import DRIVE_CONSUMER_SCOPE
from genos.drive_oauth import GoogleDriveRemoteFactory
from genos.drive_system import build_drive_system
from genos.observability import ObservabilityService
from genos.product_store import PostgresProductStore
from genos.secret_provider import LocalFileSecretProvider


EVIDENCE_PATH = Path(os.environ.get("GENOS_MVP06_EXTERNAL_EVIDENCE", "/tmp/mvp06-external-drive-evidence.json"))
RAW_CREDENTIAL_ENV = "GENOS_MVP06_DRIVE_CREDENTIAL"


class RecordingMetadata:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.states: list[str] = []

    def get_drive_binding(self):
        return self.inner.get_drive_binding()

    def upsert_drive_binding(self, payload):
        state = str(payload.get("state") or "UNKNOWN")
        self.states.append(state)
        return self.inner.upsert_drive_binding(payload)


def _write_evidence(payload: dict[str, Any]) -> None:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _delete_drive_object(access_token: str, file_id: str) -> None:
    request = urlrequest.Request(
        "https://www.googleapis.com/drive/v3/files/" + urlparse.quote(file_id) + "?supportsAllDrives=true",
        method="DELETE",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urlrequest.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Google API endpoint
            response.read(1024)
    except urlerror.HTTPError as exc:
        if exc.code == 404:
            return
        raise


def _cleanup(access_token: str, result: dict[str, Any]) -> None:
    drive = result.get("drive") if isinstance(result.get("drive"), dict) else {}
    report = result.get("initial_report") if isinstance(result.get("initial_report"), dict) else {}
    files = report.get("files") if isinstance(report.get("files"), dict) else {}
    ids = [
        files.get("markdown"),
        files.get("json"),
        drive.get("index_file_id"),
        drive.get("protocol_file_id"),
        drive.get("reports_folder_id"),
        drive.get("kanban_folder_id"),
        drive.get("root_folder_id"),
    ]
    seen: set[str] = set()
    for value in ids:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        _delete_drive_object(access_token, value)


def main() -> int:
    raw_credential = os.environ.get(RAW_CREDENTIAL_ENV, "")
    tested_sha = os.environ.get("GITHUB_HEAD_SHA") or os.environ.get("GITHUB_SHA") or "UNKNOWN"
    if not raw_credential:
        _write_evidence(
            {
                "schema_version": "1.0",
                "state": "NEEDS_EXTERNAL_GENOS_CREDENTIAL",
                "tested_git_sha": tested_sha,
                "remote_mutation": False,
                "observed_at": _utc_now(),
            }
        )
        print("GENOS_MVP06_EXTERNAL_DRIVE_E2E_NEEDS_EXTERNAL_GENOS_CREDENTIAL")
        return 0

    instance_id = str(uuid.uuid4())
    root_name = f"GenOS-MVP06-E2E-{tested_sha[:8]}-{uuid.uuid4().hex[:8]}"
    result: dict[str, Any] | None = None
    cleanup_state = "NOT_RUN"
    factory = GoogleDriveRemoteFactory()
    access_token = ""

    try:
        with tempfile.TemporaryDirectory(prefix="genos-mvp06-external-") as temp:
            root = Path(temp)
            os.environ["GENOS_INSTANCE_ID"] = instance_id
            os.environ["GENOS_STATE_DIR"] = str(root / "state")

            store = PostgresProductStore()
            store.ensure_schema()
            provider = LocalFileSecretProvider(root / "secrets")
            credentials = CredentialService(store, provider)
            credential = credentials.add(
                name=f"mvp06-external-{uuid.uuid4().hex[:12]}",
                provider_name="google-drive",
                raw_secret=raw_credential,
                consumer_scopes=[DRIVE_CONSUMER_SCOPE],
                source="mvp06-external-e2e",
            )
            secret_id = str(credential["secret_id"])
            test_projection = credentials.test(secret_id)
            if test_projection.get("state") != "PASS":
                raise RuntimeError("SecretRef material verification failed")

            observability = ObservabilityService(state_root=root / "state")
            services = build_drive_system(
                product_store=store,
                credentials=credentials,
                observability=observability,
                remote_factory=factory,
            )
            recording = RecordingMetadata(services.metadata)
            services.connection.store = recording
            services.reports.reports.metadata_store = recording

            result = services.connect(secret_id=secret_id, root_name=root_name)
            if result.get("state") != "READY":
                raise RuntimeError("external Drive connection did not reach READY")
            initial_report = result.get("initial_report") if isinstance(result.get("initial_report"), dict) else {}
            if initial_report.get("state") != "PUBLISHED" or initial_report.get("remote_write") is not True:
                raise RuntimeError("initial external Drive report was not published")
            verify = services.connection.verify()
            if verify.get("state") != "READY":
                raise RuntimeError("external Drive verify did not remain READY")

            expected = ["WRITE_VERIFIED", "READ_VERIFIED", "UPDATE_VERIFIED", "INSTANCE_BOUND", "READY"]
            positions: list[int] = []
            for state in expected:
                if state not in recording.states:
                    raise RuntimeError(f"missing guided Drive state: {state}")
                positions.append(recording.states.index(state))
            if positions != sorted(positions):
                raise RuntimeError("guided Drive state order is invalid")

            dump = subprocess.run(
                ["pg_dump", "-d", "genos"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
            if raw_credential in dump:
                raise RuntimeError("raw Drive credential leaked into Product DB dump")

            access_token, descriptor = factory.resolve(raw_credential)
            _cleanup(access_token, result)
            cleanup_state = "PASS"
            _write_evidence(
                {
                    "schema_version": "1.0",
                    "state": "PASS",
                    "tested_git_sha": tested_sha,
                    "secretref_state": "PASS",
                    "credential_mode": descriptor.mode,
                    "refresh_capable": descriptor.refresh_capable,
                    "guided_states": expected,
                    "drive_state": "READY",
                    "initial_report_state": "PUBLISHED",
                    "verify_state": "READY",
                    "raw_secret_in_product_db": False,
                    "cleanup": cleanup_state,
                    "observed_at": _utc_now(),
                }
            )
        print("GENOS_MVP06_EXTERNAL_DRIVE_E2E_PASS")
        return 0
    except Exception as exc:
        if result is not None and access_token:
            try:
                _cleanup(access_token, result)
                cleanup_state = "PASS_AFTER_FAILURE"
            except Exception:
                cleanup_state = "FAILED"
        _write_evidence(
            {
                "schema_version": "1.0",
                "state": "FAIL",
                "tested_git_sha": tested_sha,
                "error_type": type(exc).__name__,
                "cleanup": cleanup_state,
                "observed_at": _utc_now(),
            }
        )
        print(f"GENOS_MVP06_EXTERNAL_DRIVE_E2E_FAIL:{type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
