from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ObservationState(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    NOT_INSTALLED = "NOT_INSTALLED"
    NO_PERMISSION = "NO_PERMISSION"
    TIMEOUT = "TIMEOUT"


class SupportClass(str, Enum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_WITH_ACTION = "SUPPORTED_WITH_ACTION"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICT = "CONFLICT"


class RunState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_ACTION = "NEEDS_ACTION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Observation:
    check_id: str
    state: ObservationState
    observed: Any = None
    expected: Any = None
    source: str = ""
    observed_at: str = field(default_factory=utc_now)
    remediation: str | None = None
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(slots=True)
class InstallPlan:
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=utc_now)
    support_class: SupportClass = SupportClass.SUPPORTED_WITH_ACTION
    profile: str = "unselected"
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(slots=True)
class InstallRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    plan_id: str | None = None
    state: RunState = RunState.QUEUED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    checkpoint: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(slots=True)
class JobRun:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "generic"
    state: RunState = RunState.QUEUED
    progress_percent: int = 0
    current_step: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "JobRun":
        return cls(
            job_id=str(payload["job_id"]),
            kind=str(payload.get("kind", "generic")),
            state=RunState(payload.get("state", RunState.QUEUED.value)),
            progress_percent=int(payload.get("progress_percent", 0)),
            current_step=payload.get("current_step"),
            created_at=str(payload.get("created_at", utc_now())),
            updated_at=str(payload.get("updated_at", utc_now())),
            evidence=list(payload.get("evidence", [])),
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [_json_ready(v) for v in value]
    return value
