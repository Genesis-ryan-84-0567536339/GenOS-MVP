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
    gemini_version: str | None
    tmux_version: str | None
    node_bin: str | None
    gemini_bin: str | None
    npm_integrity: str | None
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "node_version": self.node_version,
            "gemini_version": self.gemini_version,
            "tmux_version": self.tmux_version,
            "node_bin": self.node_bin,
            "gemini_bin": self.gemini_bin,
            "npm_integrity": self.npm_integrity,
            "evidence": self.evidence,
        }


class AgentToolProvisioner:
    """Typed installer for the agy-gen CLI prerequisites on the verified host.

    Node is downloaded from the official nodejs.org release URL and verified
    against a pinned SHA256 from the official signed release checksum list.
    Gemini CLI is installed as the unprivileged `genos` identity from an exact
    stable npm version. npm verifies registry package integrity; the resolved
    SRI is recorded as provenance evidence. No `latest` tag is used.
    """

    def __init__(self, root: Path | str = DEFAULT_TOOLS_ROOT) -> None:
        self.root = Path(root)
        self.node_root = self.root / f"node-v{NODE_VERSION}"
        self.gemini_root = self.root / f"gemini-cli-v{GEMINI_CLI_VERSION}"
        self.node_bin = self.node_root / "bin" / "node"
        self.npm_bin = self.node_root / "bin" / "npm"
        self.gemini_bin = self.gemini_root / "bin" / "gemini"
        self.state_path = self.root / "agy-gen-toolchain.json"

    def inspect(self) -> AgentToolState:
        node_version = self._version([str(self.node_bin), "--version"]) if self.node_bin.is_file() else None
        gemini_version = self._version([str(self.gemini_bin), "--version"], env=self._tool_env()) if self.gemini_bin.exists() else None
        tmux_bin = shutil.which("tmux")
        tmux_version = self._version([tmux_bin, "-V"]) if tmux_bin else None
        persisted = self._read_state()
        ready = (
            node_version == f"v{NODE_VERSION}"
            and gemini_version == GEMINI_CLI_VERSION
            and bool(tmux_version)
        )
        return AgentToolState(
            state="READY" if ready else "NEEDS_ACTION",
            node_version=node_version,
            gemini_version=gemini_version,
            tmux_version=tmux_version,
            node_bin=str(self.node_bin) if self.node_bin.is_file() else None,
            gemini_bin=str(self.gemini_bin) if self.gemini_bin.exists() else None,
            npm_integrity=str(persisted.get("npm_integrity")) if persisted and persisted.get("npm_integrity") else None,
            evidence="PINNED_TOOLCHAIN_VERIFIED" if ready else "TOOLCHAIN_INCOMPLETE",
        )

    def provision(self) -> AgentToolState:
        if os.geteuid() != 0:
            raise AgentToolError("agy-gen tool provisioning requires root after the typed Owner action")
        if not Path("/etc/os-release").is_file():
            raise AgentToolError("supported Linux host evidence unavailable")
        if not _is_verified_ubuntu_x64():
            raise AgentToolError("tool provisioner currently supports only verified Ubuntu 24.04 x86_64 native profile")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._ensure_tmux()
        self._ensure_node()
        integrity = self._ensure_gemini()
        state = self.inspect()
        if state.state != "READY":
            raise AgentToolError(f"agy-gen toolchain verification failed: {state.evidence}")
        payload = {
            "schema_version": "1.0",
            "state": state.state,
            "node": {
                "version": NODE_VERSION,
                "url": NODE_URL,
                "sha256": NODE_SHA256,
                "bin": str(self.node_bin),
            },
            "gemini_cli": {
                "package": "@google/gemini-cli",
                "version": GEMINI_CLI_VERSION,
                "npm_integrity": integrity,
                "bin": str(self.gemini_bin),
            },
            "tmux": {"version": state.tmux_version},
            "contains_secrets": False,
        }
        self._atomic_json(self.state_path, payload, 0o644)
        return self.inspect()

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

    def _ensure_gemini(self) -> str:
        env = self._tool_env()
        if self.gemini_bin.exists() and self._version([str(self.gemini_bin), "--version"], env=env) == GEMINI_CLI_VERSION:
            persisted = self._read_state()
            return str(persisted.get("npm_integrity") or "UNKNOWN") if persisted else "UNKNOWN"
        try:
            genos_user = pwd.getpwnam("genos")
        except KeyError as exc:
            raise AgentToolError("genos service identity is missing") from exc
        self.gemini_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chown(self.gemini_root, genos_user.pw_uid, genos_user.pw_gid)
        integrity_result = self._run_as_genos(
            [str(self.npm_bin), "view", GEMINI_NPM_SPEC, "dist.integrity", "--json"],
            env=env,
            timeout=60,
        )
        integrity = integrity_result.stdout.strip().strip('"')
        if not integrity.startswith("sha512-"):
            raise AgentToolError("npm registry did not return package integrity metadata")
        self._run_as_genos(
            [
                str(self.npm_bin),
                "install",
                "--global",
                "--prefix",
                str(self.gemini_root),
                "--no-audit",
                "--no-fund",
                GEMINI_NPM_SPEC,
            ],
            env=env,
            timeout=600,
        )
        version = self._version([str(self.gemini_bin), "--version"], env=env)
        if version != GEMINI_CLI_VERSION:
            raise AgentToolError(f"Gemini CLI exact version verification failed: {version or 'UNKNOWN'}")
        return integrity

    def _tool_env(self) -> dict[str, str]:
        env = {
            "PATH": f"{self.node_root / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/var/lib/genos",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "NO_COLOR": "1",
            "npm_config_update_notifier": "false",
        }
        return env

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

    def _run_as_genos(self, argv: list[str], *, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
        command = ["runuser", "-u", "genos", "--", "env"]
        command.extend(f"{key}={value}" for key, value in env.items())
        command.extend(argv)
        return self._run(command, timeout=timeout)

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

    def _read_state(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

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
