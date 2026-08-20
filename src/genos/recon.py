from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import platform
import shutil
import socket
import subprocess

from .contracts import Observation, ObservationState, SupportClass
from .redaction import redact


@dataclass(slots=True)
class CommandResult:
    state: ObservationState
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None


class ReadOnlyCommandRunner:
    """Execute only a narrow, explicit set of non-mutating command forms."""

    _ALLOWED: dict[str, tuple[tuple[str, ...], ...]] = {
        "git": (("--version",), ("rev-parse", "--show-toplevel"), ("status", "--porcelain")),
        "systemctl": (("is-system-running",),),
        "docker": (("--version",),),
        "podman": (("--version",),),
        "tmux": (("-V",),),
        "node": (("--version",),),
    }

    def __init__(self, timeout_seconds: float = 3.0) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, argv: list[str], cwd: str | None = None) -> CommandResult:
        if not argv or not self._is_allowed(argv):
            raise ValueError(f"command is not on the read-only allowlist: {argv!r}")
        executable = shutil.which(argv[0])
        if executable is None:
            return CommandResult(ObservationState.NOT_FOUND)
        try:
            completed = subprocess.run(
                [executable, *argv[1:]],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
                check=False,
                env=_minimal_env(),
            )
        except subprocess.TimeoutExpired:
            return CommandResult(ObservationState.TIMEOUT)
        except PermissionError:
            return CommandResult(ObservationState.NO_PERMISSION)
        except OSError:
            return CommandResult(ObservationState.UNKNOWN)
        state = ObservationState.PASS if completed.returncode == 0 else ObservationState.WARN
        return CommandResult(
            state=state,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
            returncode=completed.returncode,
        )

    def _is_allowed(self, argv: list[str]) -> bool:
        forms = self._ALLOWED.get(argv[0])
        if forms is None:
            return False
        args = tuple(argv[1:])
        return args in forms


def collect_all(cwd: str | None = None) -> tuple[list[Observation], SupportClass, str]:
    runner = ReadOnlyCommandRunner()
    observations = [
        _platform_observation(),
        _resources_observation(),
        _disk_observation(),
        _network_observation(),
        _ports_observation(),
        _systemd_observation(runner),
        _container_observation(runner),
        _current_genos_observation(),
        _git_observation(runner, cwd=cwd),
        _runtime_observation(runner),
    ]
    support, reason = classify_support(observations)
    return observations, support, reason


def classify_support(observations: list[Observation]) -> tuple[SupportClass, str]:
    by_id = {item.check_id: item for item in observations}
    platform_obs = by_id.get("platform")
    systemd_obs = by_id.get("systemd")
    if not platform_obs or not isinstance(platform_obs.observed, dict):
        return SupportClass.UNSUPPORTED, "platform could not be identified"
    if platform_obs.observed.get("system") != "Linux":
        return SupportClass.UNSUPPORTED, "MVP baseline currently targets Linux with systemd"
    if not systemd_obs or systemd_obs.state in {ObservationState.NOT_FOUND, ObservationState.FAIL}:
        return SupportClass.UNSUPPORTED, "systemd is required by the current installer architecture"
    return (
        SupportClass.SUPPORTED_WITH_ACTION,
        "host has Linux/systemd capabilities, but distro/version is not certified until fresh-host E2E is defined",
    )


def _platform_observation() -> Observation:
    os_release = _read_os_release()
    observed = {
        "system": platform.system() or None,
        "release": platform.release() or None,
        "machine": platform.machine() or None,
        "python": platform.python_version(),
        "distribution": os_release.get("ID"),
        "distribution_version": os_release.get("VERSION_ID"),
    }
    return Observation("platform", ObservationState.PASS, observed=observed, source="python-platform+/etc/os-release")


def _resources_observation() -> Observation:
    memory = _read_meminfo()
    observed = {
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": memory.get("MemTotal", 0) * 1024 if memory else None,
        "memory_available_bytes": memory.get("MemAvailable", 0) * 1024 if memory else None,
    }
    state = ObservationState.PASS if observed["cpu_count"] else ObservationState.UNKNOWN
    return Observation("resources", state, observed=observed, source="os.cpu_count+/proc/meminfo")


def _disk_observation() -> Observation:
    try:
        usage = shutil.disk_usage("/")
        observed = {"path": "/", "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
        return Observation("disk", ObservationState.PASS, observed=observed, source="shutil.disk_usage")
    except OSError as exc:
        return Observation("disk", ObservationState.UNKNOWN, observed={"error": type(exc).__name__}, source="shutil.disk_usage")


def _network_observation() -> Observation:
    hostname = socket.gethostname()
    addresses: list[str] = []
    try:
        for item in socket.getaddrinfo(hostname, None):
            address = item[4][0]
            if address not in addresses:
                addresses.append(address)
        state = ObservationState.PASS
    except socket.gaierror:
        state = ObservationState.WARN
    return Observation("network", state, observed={"hostname": hostname, "addresses": addresses}, source="socket.getaddrinfo(local-hostname)")


def _ports_observation() -> Observation:
    sources = [Path("/proc/net/tcp"), Path("/proc/net/tcp6")]
    if not any(path.exists() for path in sources):
        return Observation("ports", ObservationState.NOT_FOUND, observed={}, source="/proc/net/tcp*")
    try:
        listeners = sorted(_proc_listening_ports())
        return Observation("ports", ObservationState.PASS, observed={"tcp_listen_ports": listeners}, source="/proc/net/tcp*")
    except (OSError, ValueError) as exc:
        return Observation("ports", ObservationState.UNKNOWN, observed={"error": type(exc).__name__}, source="/proc/net/tcp*")


def _systemd_observation(runner: ReadOnlyCommandRunner) -> Observation:
    result = runner.run(["systemctl", "is-system-running"])
    observed = {"status": result.stdout or None, "returncode": result.returncode}
    return Observation("systemd", result.state, observed=observed, source="systemctl is-system-running")


def _container_observation(runner: ReadOnlyCommandRunner) -> Observation:
    found: dict[str, Any] = {}
    states: list[ObservationState] = []
    for tool in ("podman", "docker"):
        result = runner.run([tool, "--version"])
        states.append(result.state)
        if result.state != ObservationState.NOT_FOUND:
            found[tool] = result.stdout or {"state": result.state.value}
    if found:
        state = ObservationState.PASS
    elif all(item == ObservationState.NOT_FOUND for item in states):
        state = ObservationState.NOT_INSTALLED
    else:
        state = ObservationState.UNKNOWN
    return Observation("container_runtime", state, observed=found, source="podman/docker --version")


def _current_genos_observation() -> Observation:
    roots = [Path("/etc/genos"), Path("/var/lib/genos"), Path("/opt/genos/current")]
    observed = {str(path): path.exists() for path in roots}
    state = ObservationState.PASS if any(observed.values()) else ObservationState.NOT_INSTALLED
    return Observation("current_genos", state, observed=observed, source="filesystem exists checks")


def _git_observation(runner: ReadOnlyCommandRunner, cwd: str | None) -> Observation:
    version = runner.run(["git", "--version"])
    if version.state == ObservationState.NOT_FOUND:
        return Observation("git", ObservationState.NOT_INSTALLED, observed={}, source="git --version")
    observed: dict[str, Any] = {"version": version.stdout or None}
    state = version.state
    if cwd:
        root = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        if root.returncode == 0:
            observed["repository_root"] = root.stdout
            status = runner.run(["git", "status", "--porcelain"], cwd=cwd)
            observed["dirty"] = bool(status.stdout) if status.returncode == 0 else None
    return Observation("git", state, observed=redact(observed), source="git read-only commands")


def _runtime_observation(runner: ReadOnlyCommandRunner) -> Observation:
    observed: dict[str, Any] = {"python": platform.python_version()}
    for tool, argv in (("node", ["node", "--version"]), ("tmux", ["tmux", "-V"])):
        result = runner.run(argv)
        observed[tool] = result.stdout if result.state != ObservationState.NOT_FOUND else None
    return Observation("runtime_basics", ObservationState.PASS, observed=observed, source="python+node/tmux version probes")


def _read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    result: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line or raw_line.startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            result[key] = value.strip().strip('"')
    except OSError:
        pass
    return result


def _read_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, rest = line.split(":", 1)
            value = rest.strip().split()[0]
            result[key] = int(value)
    except (OSError, ValueError):
        return {}
    return result


def _proc_listening_ports() -> set[int]:
    ports: set[int] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        path = Path(name)
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            local_address = fields[1]
            _, port_hex = local_address.rsplit(":", 1)
            ports.add(int(port_hex, 16))
    return ports


def _minimal_env() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")
    return {key: os.environ[key] for key in allowed if key in os.environ}
