from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .observability import GENOS_SERVICES, ObservabilityService
from .redaction import redact


class RepairError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepairPlan:
    action: str
    target: str
    unit: str
    observed_state: str
    mutation_allowed: bool
    reason: str
    operation: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "action": self.action,
            "target": self.target,
            "unit": self.unit,
            "observed_state": self.observed_state,
            "mutation_allowed": self.mutation_allowed,
            "reason": self.reason,
            "operation": list(self.operation),
        }


class RepairService:
    """Typed repair registry with observation-backed preconditions.

    MVP-05 intentionally starts with one narrow operation: restart one known
    GenOS systemd unit. There is no arbitrary command, arbitrary unit, package
    install or reinstall action in this registry.
    """

    ACTION_RESTART_SERVICE = "restart-service"

    def __init__(self, observability: ObservabilityService | None = None) -> None:
        self.observability = observability or ObservabilityService()

    @property
    def actions(self) -> tuple[str, ...]:
        return (self.ACTION_RESTART_SERVICE,)

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(GENOS_SERVICES)

    def plan(self, *, action: str, target: str) -> RepairPlan:
        if action not in self.actions:
            raise RepairError("repair action is not on the typed registry")
        unit = GENOS_SERVICES.get(target)
        if unit is None:
            raise RepairError("repair target is not a fixed GenOS service")

        snapshot = self.observability.snapshot()
        observed_state = _service_state(snapshot, target)
        if observed_state == "active":
            return RepairPlan(
                action=action,
                target=target,
                unit=unit,
                observed_state=observed_state,
                mutation_allowed=False,
                reason="NO_REPAIR_REQUIRED",
                operation=["systemctl", "restart", unit],
            )
        if observed_state in {"unknown", "not_installed", "UNKNOWN"}:
            return RepairPlan(
                action=action,
                target=target,
                unit=unit,
                observed_state=observed_state,
                mutation_allowed=False,
                reason="INSUFFICIENT_LIVE_EVIDENCE",
                operation=["systemctl", "restart", unit],
            )
        return RepairPlan(
            action=action,
            target=target,
            unit=unit,
            observed_state=observed_state,
            mutation_allowed=True,
            reason="SERVICE_NOT_ACTIVE",
            operation=["systemctl", "restart", unit],
        )

    def execute(self, plan: RepairPlan) -> dict[str, Any]:
        if not plan.mutation_allowed:
            return {"state": "NO_ACTION", "plan": plan.to_dict()}
        if plan.action != self.ACTION_RESTART_SERVICE or GENOS_SERVICES.get(plan.target) != plan.unit:
            raise RepairError("repair plan is not a current typed registry action")
        if os.geteuid() != 0:
            raise RepairError("typed service repair requires root privileges")
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise RepairError("systemctl is unavailable")
        restarted = _run([systemctl, "restart", plan.unit], timeout=30.0)
        if restarted.returncode != 0:
            raise RepairError("typed service restart failed")
        verified = _run([systemctl, "is-active", plan.unit], timeout=10.0)
        current = (verified.stdout.strip() or "unknown")[:80]
        state = "SUCCEEDED" if verified.returncode == 0 and current == "active" else "FAILED_VERIFY"
        return redact(
            {
                "state": state,
                "plan": plan.to_dict(),
                "verification": {"unit": plan.unit, "state": current},
            }
        )


def _service_state(snapshot: dict[str, Any], target: str) -> str:
    observations = snapshot.get("observations")
    if not isinstance(observations, list):
        return "UNKNOWN"
    for item in observations:
        if not isinstance(item, dict) or item.get("check_id") != "genos_services":
            continue
        observed = item.get("observed")
        if not isinstance(observed, dict):
            return "UNKNOWN"
        units = observed.get("units")
        if not isinstance(units, dict):
            return "UNKNOWN"
        value = units.get(target)
        return str(value) if value is not None else "UNKNOWN"
    return "UNKNOWN"


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            shell=False,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepairError("typed repair command failed to execute") from exc
