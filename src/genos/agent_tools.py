from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from typing import Any

from .agent_cli_update import AGY_INSTALLER_URL, AntigravityCliManager
from .redaction import redact


NODE_VERSION = "24.19.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647"
GEMINI_CLI_VERSION = "0.53.0"
GEMINI_NPM_SPEC = f"@google/gemini-cli@{GEMINI_CLI_VERSION}"
DEFAULT_TOOLS_ROOT = Path("/var/lib/genos/tools")


class AgentToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentToolState:
    state: str
    node_version: str | None
    provider_cli: str
    provider_version: str | None
    tmux_version: str | None
    node_bin: str | None
    provider_bin: str | None
    update_state: str | None
    rollback_version: str | None
    evidence: str

    @property
    def gemini_version(self) -> None:
        return None

    @property
    def gemini_bin(self) -> None:
        return None

    @property
    def npm_integrity(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "node_version": self.node_version,
            "provider_cli": self.provider_cli,
            "provider_version": self.provider_version,
            "tmux_version": self.tmux_version,
            "node_bin": self.node_bin,
            "provider_bin": self.provider_bin,
            "update_state": self.update_state,
            "rollback_version": self.rollback_version,
            "evidence": self.evidence,
        }


class AgentToolProvisioner:
    """Typed toolchain provisioner for resident `agy-gen`."""

    def __init__(self, root: Path | str = DEFAULT_TOOLS_ROOT) -> None:
        self.root = Path(root)
        self.node_root = self.root / f"node-v{NODE_VERSION}"
        self.node_bin = self.node_root / "bin" / "node"
        self.npm_bin = self.node_root / "bin" / "npm"
        self.agy_root = self.root / "antigravity-cli"
        self.agy = AntigravityCliManager(self.agy_root)
        self.state_path = self.root / "agy-gen-toolchain.json"

    def inspect(self) -> AgentToolState:
        node_version = self._version([str(self.node_bin), "--version"]) if self.node_bin.is_file() else None
        agy_status = self.agy.status()
        agy_version = agy_status.get("installed_version")
        tmux_bin = shutil.which("tmux")
        tmux_version = self._version([tmux_bin, "-V"]) if tmux_bin else None
        ready = node_version == f"v{NODE_VERSION}" and bool(agy_version) and bool(tmux_version)
        return AgentToolState(
            state="READY" if ready else "NEEDS_ACTION",
            node_version=node_version,
            provider_cli="antigravity",
            provider_version=str(agy_version) if agy_version else None,
            tmux_version=tmux_version,
            node_bin=str(self.node_bin) if self.node_bin.is_file() else None,
            provider_bin=str(self.agy.active_binary) if self.agy.active_binary.is_file() else None,
            update_state=str(agy_status.get("update_state")) if agy_status.get("update_state") else None,
            rollback_version=str(agy_status.get("rollback_version")) if agy_status.get("rollback_version") else None,
            evidence="MANAGED_ANTIGRAVITY_TOOLCHAIN_VERIFIED" if ready else "TOOLCHAIN_INCOMPLETE",
        )

    def provision(self) -> AgentToolState:
        if os.geteuid() != 0:
            raise AgentToolError("agy-gen tool provisioning requires root after the typed Owner action")
        if not Path("/etc/os-release").is_file():
            raise AgentToolError("supported Linux host evidence unavailable")
        if not _is_verified_ubuntu_x64():
            raise AgentToolError("tool provisioner currently supports only verified Ubuntu 24.04 x86_64 native profile")
        cpu = self._agy_cpu_feature_evidence()
        if not cpu["aes"] or not cpu["pclmulqdq"]:
            raise AgentToolError(
                "Antigravity CLI CPU prerequisites unavailable: "
                f"CPU_AES={int(cpu['aes'])}; CPU_PCLMUL={int(cpu['pclmulqdq'])}"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._ensure_tmux()
        self._ensure_node()
        self.agy_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        update = self.agy.ensure_latest(force=True)
        if update.update_state in {"FAILED", "ROLLED_BACK"} or not self.agy.active_binary.is_file():
            raise AgentToolError(
                "Antigravity CLI stable provisioning failed: "
                f"{update.evidence}; CPU_AES={int(cpu['aes'])}; CPU_PCLMUL={int(cpu['pclmulqdq'])}"
            )
        self._grant_agy_update_ownership()
        state = self.inspect()
        if state.state != "READY":
            raise AgentToolError(f"agy-gen toolchain verification failed: {state.evidence}")
        payload = {
            "schema_version": "2.0",
            "state": state.state,
            "node": {
                "version": NODE_VERSION,
                "url": NODE_URL,
                "sha256": NODE_SHA256,
                "bin": str(self.node_bin),
            },
            "antigravity_cli": {
                "provider_cli": "antigravity",
                "version": state.provider_version,
                "bin": str(self.agy.active_binary),
                "source": AGY_INSTALLER_URL,
                "release_channel": "official-installer-published-stable-manifest",
                "managed_update": self.agy.status(),
                "native_auto_update_disabled": True,
                "cpu_prerequisites": cpu,
            },
            "tmux": {"version": state.tmux_version},
            "contains_secrets": False,
        }
        self._atomic_json(self.state_path, payload, 0o644)
        return self.inspect()

    def _grant_agy_update_ownership(self) -> None:
        try:
            genos_user = pwd.getpwnam("genos")
        except KeyError as exc:
            raise AgentToolError("genos service identity is missing") from exc
        for root, dirs, files in os.walk(self.agy_root):
            os.chown(root, genos_user.pw_uid, genos_user.pw_gid)
            for name in dirs:
                os.chown(Path(root) / name, genos_user.pw_uid, genos_user.pw_gid)
            for name in files:
                os.chown(Path(root) / name, genos_user.pw_uid, genos_user.pw_gid)
        os.chmod(self.agy_root, 0o755)

    def _ensure_tmux(self) -> None:
        if shutil.which("tmux"):
            return
        self._run(["apt-get", "update"], timeout=300)
        self._run(["apt-get", "install", "-y", "tmux", "ca-certificates", "xz-utils"], timeout=600)
        if not shutil.which("tmux"):
            raise AgentToolError("tmux install completed without a visible tmux binary")

    def _ensure_node(self) -> None:
        if self.node_bin.is_file() and self._version([str(self.node_bin), "--version"]) == f"v{NODE_VERSION}":
            return
        with tempfile.TemporaryDirectory(prefix="genos-node-") as temp:
            archive = Path(temp) / NODE_ARCHIVE
            try:
                with urllib.request.urlopen(NODE_URL, timeout=60) as response, archive.open("wb") as output:  # noqa: S310 fixed official URL
                    shutil.copyfileobj(response, output)
            except (OSError, TimeoutError) as exc:
                raise AgentToolError("official Node.js download failed") from exc
            actual = _sha256_file(archive)
            if actual != NODE_SHA256:
                raise AgentToolError("Node.js release checksum mismatch")
            extract = Path(temp) / "extract"
            extract.mkdir()
            try:
                with tarfile.open(archive, "r:xz") as handle:
                    _safe_extract_node(handle, extract)
            except (tarfile.TarError, OSError) as exc:
                raise AgentToolError("verified Node.js archive extraction failed") from exc
            source = extract / f"node-v{NODE_VERSION}-linux-x64"
            if not (source / "bin" / "node").is_file():
                raise AgentToolError("verified Node.js archive missing node binary")
            if self.node_root.exists():
                shutil.rmtree(self.node_root)
            shutil.copytree(source, self.node_root, symlinks=True)
        if self._version([str(self.node_bin), "--version"]) != f"v{NODE_VERSION}":
            raise AgentToolError("Node.js exact version verification failed")

    @staticmethod
    def _agy_cpu_feature_evidence() -> dict[str, bool]:
        flags: set[str] = set()
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("flags") and ":" in line:
                    flags.update(line.split(":", 1)[1].strip().split())
                    break
        except OSError:
            pass
        return {"aes": "aes" in flags, "pclmulqdq": "pclmulqdq" in flags}

    def _run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
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
                    "DEBIAN_FRONTEND": "noninteractive",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentToolError("typed tool provisioning command failed to execute") from exc
        if completed.returncode != 0:
            raise AgentToolError(f"typed tool provisioning command failed: {Path(argv[0]).name}")
        return completed

    def _version(self, argv: list[str | None], env: dict[str, str] | None = None) -> str | None:
        if not argv[0]:
            return None
        try:
            completed = subprocess.run(
                [str(value) for value in argv],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                shell=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout.strip().splitlines()
        return value[0].strip()[:160] if value else None

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any], mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(redact(payload), handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _is_verified_ubuntu_x64() -> bool:
    try:
        fields: dict[str, str] = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value.strip().strip('"')
        return fields.get("ID") == "ubuntu" and fields.get("VERSION_ID") == "24.04" and os.uname().machine == "x86_64"
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_node(handle: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    members = handle.getmembers()
    prefix = f"node-v{NODE_VERSION}-linux-x64/"
    for member in members:
        if member.name != prefix.rstrip("/") and not member.name.startswith(prefix):
            raise AgentToolError("Node.js archive contains an unexpected top-level path")
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise AgentToolError("Node.js archive path escapes destination")
        if member.isdev() or member.isfifo():
            raise AgentToolError("Node.js archive contains unsupported device entry")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            source_root = (destination / prefix.rstrip("/")).resolve()
            if link_target != source_root and source_root not in link_target.parents:
                raise AgentToolError("Node.js archive symlink escapes verified release tree")
    handle.extractall(destination, members=members, filter="data")
