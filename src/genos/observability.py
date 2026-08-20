from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from .agent_runtime import AgentRuntimeError, AgentRuntimeStore
from .contracts import Observation, ObservationState, SupportClass, utc_now
from .recon import collect_all
from .redaction import redact


DEFAULT_STATE_ROOT = Path("/var/lib/genos")
GENOS_SERVICES: dict[str, str] = {
    "product-api": "genos-product-api.service",
    "runtime": "genos-runtime.service",
    "worker": "genos-worker.service",
    "mission-control": "genos-mission-control.service",
}

# These surfaces are intentionally explicit even before their later MVP package
# exists. Missing evidence must render as NOT_INSTALLED/UNKNOWN, never HEALTHY.
_FUTURE_SURFACES: dict[str, str] = {
    "mcp": "MVP-06_or_later",
    "drive": "MVP-06",
    "tunnel": "MVP-10",
}

BaselineCollector = Callable[[str | None], tuple[list[Observation], SupportClass, str]]


class ObservabilityService:
    """Single read-only evidence authority for Doctor, Dashboard API and Reports.

    The service consumes the original MVP recon collector instead of replacing
    it, then appends GenOS-specific projections. No collector in this module
    mutates host state or executes a shell command string.
    """

    def __init__(
        self,
        *,
        state_root: Path | str = DEFAULT_STATE_ROOT,
        baseline_collector: BaselineCollector = collect_all,
    ) -> None:
        self.state_root = Path(state_root)
        self.baseline_collector = baseline_collector

    def snapshot(self, *, cwd: str | None = None) -> dict[str, Any]:
        baseline, support_class, support_reason = self.baseline_collector(cwd)
        observations = list(baseline)
        observations.extend(
            [
                self._gpu_observation(),
                self._gateway_observation(),
                self._services_observation(),
                self._timers_observation(),
                self._database_observation(),
                self._worker_observation(),
                self._agent_observation(),
                self._provider_observation(),
                *self._future_surface_observations(),
            ]
        )
        payloads = [item.to_dict() for item in observations]
        generated_at = utc_now()
        return redact(
            {
                "schema_version": "1.0",
                "authority": "genos-observability-v1",
                "read_only": True,
                "generated_at": generated_at,
                "freshness": {
                    "state": "FRESH",
                    "basis": "snapshot_collected_now",
                    "observed_at": generated_at,
                },
                "health": self._health_summary(observations),
                "support_class": support_class.value,
                "support_reason": support_reason,
                "observations": payloads,
            }
        )

    def _installed(self) -> bool:
        return (self.state_root / "manifest.json").is_file() or (self.state_root / "agents" / "agy-gen" / "identity.json").is_file()

    def _gpu_observation(self) -> Observation:
        device_present = Path("/dev/nvidia0").exists() or Path("/proc/driver/nvidia/gpus").exists()
        binary = shutil.which("nvidia-smi")
        if not device_present and not binary:
            return Observation(
                "gpu",
                ObservationState.NOT_FOUND,
                observed={"available": False, "provider": None},
                source="/dev/nvidia0+/proc/driver/nvidia/gpus+nvidia-smi presence",
            )
        if not binary:
            return Observation(
                "gpu",
                ObservationState.WARN,
                observed={"available": True, "provider": "nvidia", "details": None},
                source="NVIDIA device presence; nvidia-smi unavailable",
            )
        result = _run_fixed([binary, "--query-gpu=name", "--format=csv,noheader"])
        names = [line.strip()[:160] for line in result.stdout.splitlines() if line.strip()]
        return Observation(
            "gpu",
            ObservationState.PASS if result.returncode == 0 else ObservationState.UNKNOWN,
            observed={"available": bool(names) or device_present, "provider": "nvidia", "names": names},
            source="nvidia-smi --query-gpu=name --format=csv,noheader",
        )

    def _gateway_observation(self) -> Observation:
        path = Path("/proc/net/route")
        if not path.is_file():
            return Observation("gateway", ObservationState.NOT_FOUND, observed={}, source="/proc/net/route")
        try:
            gateway: str | None = None
            interface: str | None = None
            for line in path.read_text(encoding="utf-8").splitlines()[1:]:
                fields = line.split()
                if len(fields) < 4 or fields[1] != "00000000" or not (int(fields[3], 16) & 0x2):
                    continue
                raw = bytes.fromhex(fields[2])
                gateway = ".".join(str(value) for value in raw[::-1])
                interface = fields[0]
                break
            state = ObservationState.PASS if gateway else ObservationState.NOT_FOUND
            return Observation(
                "gateway",
                state,
                observed={"default_gateway": gateway, "interface": interface},
                source="/proc/net/route",
            )
        except (OSError, ValueError):
            return Observation("gateway", ObservationState.UNKNOWN, observed={}, source="/proc/net/route")

    def _services_observation(self) -> Observation:
        if not self._installed():
            return Observation(
                "genos_services",
                ObservationState.NOT_INSTALLED,
                observed={"units": {key: "not_installed" for key in GENOS_SERVICES}},
                source="systemctl is-active fixed GenOS units",
            )
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return Observation(
                "genos_services",
                ObservationState.NOT_FOUND,
                observed={"units": {}},
                source="systemctl is-active fixed GenOS units",
            )
        states: dict[str, str] = {}
        for key, unit in GENOS_SERVICES.items():
            result = _run_fixed([systemctl, "is-active", unit])
            states[key] = (result.stdout.strip() or "unknown")[:80]
        if states and all(value == "active" for value in states.values()):
            state = ObservationState.PASS
        elif any(value in {"failed", "inactive", "deactivating"} for value in states.values()):
            state = ObservationState.FAIL
        else:
            state = ObservationState.UNKNOWN
        return Observation(
            "genos_services",
            state,
            observed={"units": states},
            expected={"units": {key: "active" for key in GENOS_SERVICES}},
            source="systemctl is-active fixed GenOS units",
            remediation="Use a typed repair action for a failed GenOS unit; never reinstall blindly.",
        )

    def _timers_observation(self) -> Observation:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return Observation("timers", ObservationState.NOT_FOUND, observed={"units": []}, source="systemctl list-timers")
        result = _run_fixed([systemctl, "list-timers", "--all", "--no-pager", "--no-legend"])
        if result.returncode != 0:
            return Observation("timers", ObservationState.UNKNOWN, observed={"units": []}, source="systemctl list-timers")
        units: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            timer = next((field for field in fields if field.endswith(".timer")), None)
            if timer and timer not in units:
                units.append(timer[:200])
        return Observation("timers", ObservationState.PASS, observed={"units": sorted(units)}, source="systemctl list-timers --all")

    def _database_observation(self) -> Observation:
        if not self._installed():
            return Observation(
                "database",
                ObservationState.NOT_INSTALLED,
                observed={"engine": "postgresql", "reachable": False},
                source="pg_isready",
            )
        binary = shutil.which("pg_isready")
        if not binary:
            return Observation(
                "database",
                ObservationState.NOT_FOUND,
                observed={"engine": "postgresql", "reachable": None},
                source="pg_isready",
            )
        result = _run_fixed([binary, "-q"])
        return Observation(
            "database",
            ObservationState.PASS if result.returncode == 0 else ObservationState.FAIL,
            observed={"engine": "postgresql", "reachable": result.returncode == 0},
            expected={"reachable": True},
            source="pg_isready -q",
            remediation="Inspect PostgreSQL/service evidence before any typed repair.",
        )

    def _worker_observation(self) -> Observation:
        heartbeat = self.state_root / "worker" / "heartbeat.json"
        if not heartbeat.is_file():
            state = ObservationState.NOT_INSTALLED if not self._installed() else ObservationState.WARN
            return Observation(
                "worker_daemon",
                state,
                observed={"heartbeat": False, "freshness": "UNKNOWN"},
                source=str(heartbeat),
            )
        try:
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("heartbeat must be object")
            observed_at = str(payload.get("observed_at") or "")
            age = _age_seconds(observed_at)
            freshness = "FRESH" if age is not None and age <= 30 else "STALE" if age is not None else "UNKNOWN"
            state = ObservationState.PASS if freshness == "FRESH" else ObservationState.WARN
            return Observation(
                "worker_daemon",
                state,
                observed={
                    "heartbeat": True,
                    "freshness": freshness,
                    "age_seconds": round(age, 3) if age is not None else None,
                    "core_agent": redact(payload.get("core_agent")),
                },
                source=str(heartbeat),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return Observation(
                "worker_daemon",
                ObservationState.UNKNOWN,
                observed={"heartbeat": True, "freshness": "UNKNOWN"},
                source=str(heartbeat),
            )

    def _agent_observation(self) -> Observation:
        root = self.state_root / "agents" / "agy-gen"
        if not (root / "identity.json").is_file():
            return Observation(
                "agent",
                ObservationState.NOT_INSTALLED,
                observed={"agent_id": "agy-gen", "runtime_state": "UNKNOWN", "tmux_state": "UNKNOWN"},
                source=str(root),
            )
        try:
            status = AgentRuntimeStore(root).status()
            runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
            runtime_state = str(runtime.get("state") or "UNKNOWN")
            tmux_state = str(runtime.get("tmux_state") or "UNKNOWN")
            if runtime_state in {"READY", "BUSY"}:
                state = ObservationState.PASS
            elif runtime_state == "DEGRADED":
                state = ObservationState.FAIL
            elif runtime_state == "NEEDS_ACTION":
                state = ObservationState.WARN
            else:
                state = ObservationState.UNKNOWN
            return Observation(
                "agent",
                state,
                observed={
                    "agent_id": "agy-gen",
                    "runtime_state": runtime_state,
                    "tmux_state": tmux_state,
                    "reason": runtime.get("reason"),
                },
                source="agy-gen durable AgentRuntimeStore",
            )
        except (AgentRuntimeError, OSError, ValueError, json.JSONDecodeError):
            return Observation(
                "agent",
                ObservationState.UNKNOWN,
                observed={"agent_id": "agy-gen", "runtime_state": "UNKNOWN", "tmux_state": "UNKNOWN"},
                source="agy-gen durable AgentRuntimeStore",
            )

    def _provider_observation(self) -> Observation:
        path = self.state_root / "agents" / "agy-gen" / "provider.json"
        if not path.is_file():
            state = ObservationState.NOT_INSTALLED if not self._installed() else ObservationState.NOT_FOUND
            return Observation(
                "provider",
                state,
                observed={"provider": "gemini-cli", "state": "UNKNOWN", "model": "UNKNOWN"},
                source=str(path),
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            provider_state = str(payload.get("state") or "UNKNOWN")
            if provider_state == "ACTIVE":
                state = ObservationState.PASS
            elif provider_state in {"INSTALLED", "NEEDS_ACTION"}:
                state = ObservationState.WARN
            elif provider_state == "DEGRADED":
                state = ObservationState.FAIL
            else:
                state = ObservationState.UNKNOWN
            return Observation(
                "provider",
                state,
                observed=redact(
                    {
                        "provider": "gemini-cli",
                        "state": provider_state,
                        "model": payload.get("model") or "UNKNOWN",
                        "thinking_level": payload.get("thinking_level") or "UNKNOWN",
                        "evidence": payload.get("evidence"),
                        "observed_at": payload.get("observed_at"),
                    }
                ),
                source=str(path),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return Observation(
                "provider",
                ObservationState.UNKNOWN,
                observed={"provider": "gemini-cli", "state": "UNKNOWN", "model": "UNKNOWN"},
                source=str(path),
            )

    def _future_surface_observations(self) -> list[Observation]:
        return [
            Observation(
                check_id,
                ObservationState.NOT_INSTALLED,
                observed={"configured": False, "state": "NOT_CONFIGURED"},
                expected={"available_after_package": package},
                source="canonical MVP package state",
            )
            for check_id, package in _FUTURE_SURFACES.items()
        ]

    @staticmethod
    def _health_summary(observations: list[Observation]) -> dict[str, Any]:
        by_id = {item.check_id: item for item in observations}
        required_ids = (
            "platform",
            "resources",
            "disk",
            "network",
            "ports",
            "systemd",
            "current_genos",
            "genos_services",
            "database",
            "agent",
        )
        required = [by_id[item] for item in required_ids if item in by_id]
        states = {item.state for item in required}
        if any(state in {ObservationState.FAIL, ObservationState.NO_PERMISSION, ObservationState.TIMEOUT} for state in states):
            health = "DEGRADED"
        elif any(
            state
            in {
                ObservationState.WARN,
                ObservationState.UNKNOWN,
                ObservationState.NOT_FOUND,
                ObservationState.NOT_INSTALLED,
            }
            for state in states
        ):
            health = "NEEDS_ACTION"
        else:
            health = "HEALTHY"
        counts: dict[str, int] = {}
        for item in observations:
            counts[item.state.value] = counts.get(item.state.value, 0) + 1
        return {"state": health, "required_checks": list(required_ids), "counts": counts}


def _run_fixed(argv: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            shell=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(argv, 127, "", "unavailable")


def _age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return None
