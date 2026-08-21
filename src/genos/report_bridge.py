from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable

from .auth_service import CredentialService
from .contracts import JobRun, RunState
from .drive_bridge import DRIVE_CONSUMER_SCOPE, DriveMetadataStore, DriveNeedsAction, DriveRemote
from .observability import ObservabilityService
from .redaction import redact
from .state import JsonStateStore


class ReportBridgeError(RuntimeError):
    pass


class DriveReportService:
    """Publish sanitized System Reports from the one MVP-05 observability authority."""

    def __init__(
        self,
        *,
        metadata_store: DriveMetadataStore,
        credentials: CredentialService,
        remote_factory: Callable[[str], DriveRemote],
        observability: ObservabilityService,
        jobs: JsonStateStore | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.credentials = credentials
        self.remote_factory = remote_factory
        self.observability = observability
        self.jobs = jobs

    def build(self) -> dict[str, Any]:
        snapshot = redact(self.observability.snapshot())
        normalized = _stable_report_projection(snapshot)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fingerprint = "sha256:" + hashlib.sha256(encoded).hexdigest()
        json_text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        markdown = _render_markdown(snapshot, fingerprint=fingerprint)
        return {
            "authority": snapshot.get("authority"),
            "fingerprint": fingerprint,
            "snapshot": snapshot,
            "json": json_text,
            "markdown": markdown,
        }

    def publish(self, *, manual: bool = False) -> dict[str, Any]:
        job = JobRun(kind="drive-system-report", state=RunState.RUNNING, progress_percent=5, current_step="collect_observability")
        self._save(job)
        binding = self.metadata_store.get_drive_binding()
        if not binding or binding.get("state") != "READY":
            job.state = RunState.NEEDS_ACTION
            job.progress_percent = 100
            job.current_step = "drive_not_ready"
            job.evidence.append({"code": "DRIVE_NOT_READY", "remote_write": False})
            job.updated_at = _utc_now()
            self._save(job)
            raise DriveNeedsAction("Drive connection is not ready")

        built = self.build()
        fingerprint = str(built["fingerprint"])
        previous = binding.get("last_report_fingerprint")
        if not manual and previous == fingerprint:
            job.state = RunState.SUCCEEDED
            job.progress_percent = 100
            job.current_step = "no_change"
            job.evidence.append({"code": "NO_CHANGE", "fingerprint": fingerprint, "remote_write": False})
            job.updated_at = _utc_now()
            self._save(job)
            return {
                "state": "NO_CHANGE",
                "job": job.to_dict(),
                "fingerprint": fingerprint,
                "remote_write": False,
            }

        secret_id = _required(binding, "secret_id")
        reports_folder_id = _required(binding, "reports_folder_id")
        job.progress_percent = 30
        job.current_step = "resolve_drive_credential"
        job.updated_at = _utc_now()
        self._save(job)
        raw_access_token = self.credentials.get_secret_for_consumer(secret_id, consumer=DRIVE_CONSUMER_SCOPE)
        remote = self.remote_factory(raw_access_token)

        try:
            job.progress_percent = 55
            job.current_step = "write_report_markdown"
            job.updated_at = _utc_now()
            self._save(job)
            md_id = remote.upsert_text_file(
                name="SYSTEM_REPORT.md",
                parent_id=reports_folder_id,
                content=str(built["markdown"]),
                file_id=_optional(binding.get("report_markdown_file_id")),
                mime_type="text/markdown; charset=utf-8",
            )
            job.progress_percent = 75
            job.current_step = "write_report_json"
            job.updated_at = _utc_now()
            self._save(job)
            json_id = remote.upsert_text_file(
                name="SYSTEM_REPORT.json",
                parent_id=reports_folder_id,
                content=str(built["json"]),
                file_id=_optional(binding.get("report_json_file_id")),
                mime_type="application/json; charset=utf-8",
            )
            updated = dict(binding)
            updated.update(
                {
                    "report_markdown_file_id": md_id,
                    "report_json_file_id": json_id,
                    "last_report_fingerprint": fingerprint,
                    "last_verified_at": _utc_now(),
                    "last_error_code": None,
                }
            )
            self.metadata_store.upsert_drive_binding(updated)
        except Exception as exc:
            degraded = dict(binding)
            degraded.update({"state": "DEGRADED", "last_error_code": "REPORT_REMOTE_WRITE_FAILED"})
            self.metadata_store.upsert_drive_binding(degraded)
            job.state = RunState.FAILED
            job.progress_percent = 100
            job.current_step = "remote_write_failed"
            job.evidence.append({"code": "REPORT_REMOTE_WRITE_FAILED", "remote_write": "UNKNOWN"})
            job.updated_at = _utc_now()
            self._save(job)
            raise ReportBridgeError("Drive report publish failed") from exc

        job.state = RunState.SUCCEEDED
        job.progress_percent = 100
        job.current_step = "completed"
        job.evidence.append(
            {
                "code": "REPORT_PUBLISHED",
                "fingerprint": fingerprint,
                "manual": bool(manual),
                "remote_write": True,
                "markdown_file_id": md_id,
                "json_file_id": json_id,
            }
        )
        job.updated_at = _utc_now()
        self._save(job)
        return {
            "state": "PUBLISHED",
            "job": job.to_dict(),
            "fingerprint": fingerprint,
            "remote_write": True,
            "files": {"markdown": md_id, "json": json_id},
        }

    def _save(self, job: JobRun) -> None:
        if self.jobs is not None:
            self.jobs.save_job(job)


def _stable_report_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove collection-time noise so unchanged state does not write remotely."""
    value = deepcopy(redact(snapshot))
    value.pop("generated_at", None)
    freshness = value.get("freshness")
    if isinstance(freshness, dict):
        freshness.pop("observed_at", None)
        freshness.pop("age_seconds", None)
    observations = value.get("observations")
    if isinstance(observations, list):
        for item in observations:
            if isinstance(item, dict):
                item.pop("observed_at", None)
                observed = item.get("observed")
                if isinstance(observed, dict):
                    observed.pop("observed_at", None)
                    observed.pop("age_seconds", None)
    return value


def _render_markdown(snapshot: dict[str, Any], *, fingerprint: str) -> str:
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    lines = [
        "# GenOS System Report",
        "",
        f"Authority: `{snapshot.get('authority', 'UNKNOWN')}`",
        f"Generated: `{snapshot.get('generated_at', 'UNKNOWN')}`",
        f"Health: `{health.get('state', 'UNKNOWN')}`",
        f"Fingerprint: `{fingerprint}`",
        "",
        "## Observations",
        "",
    ]
    observations = snapshot.get("observations")
    if isinstance(observations, list):
        for item in observations:
            if not isinstance(item, dict):
                continue
            check_id = str(item.get("check_id") or "unknown")
            state = str(item.get("state") or "UNKNOWN")
            source = str(item.get("source") or "")
            lines.append(f"- **{check_id}** — `{state}`" + (f" — {source}" if source else ""))
    lines.extend(
        [
            "",
            "This report is a sanitized projection of the GenOS observability read model. Google Drive is a replica, not product authority.",
            "",
        ]
    )
    return "\n".join(lines)


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DriveNeedsAction(f"Drive binding is missing {key}")
    return value


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
