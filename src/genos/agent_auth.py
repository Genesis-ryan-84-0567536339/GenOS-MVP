from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any
import uuid

from .agent_runtime import AgentNeedsAction, AgentRuntimeError, AgentRuntimeStore, CORE_AGENT_SESSION, utc_now


AUTH_WINDOW = "auth"
RUNTIME_WINDOW = "runtime"
MAX_AUTH_CODE_CHARS = 4096
MAX_AUTH_URL_CHARS = 8192

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_URL_RE = re.compile(r"https://[^\s\x00-\x1f]+")
_ALLOWED_AUTH_HOSTS = ("accounts.google.com", "antigravity.google")


class AgentAuthError(AgentRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthProjection:
    state: str
    auth_url: str | None
    tmux_session: str
    tmux_window: str
    observed_at: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": "agy-gen",
            "provider_cli": "antigravity",
            "state": self.state,
            "auth_url": self.auth_url,
            "tmux_session": self.tmux_session,
            "tmux_window": self.tmux_window,
            "observed_at": self.observed_at,
            "evidence": self.evidence,
        }


def normalize_auth_code(value: str) -> str:
    code = value.strip()
    if not code or len(code) > MAX_AUTH_CODE_CHARS:
        raise AgentAuthError("authorization code must be 1..4096 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in code):
        raise AgentAuthError("authorization code contains control characters")
    return code


def parse_auth_terminal(text: str) -> AuthProjection:
    clean = _ANSI_RE.sub("", text).replace("\r", "")
    auth_url: str | None = None
    for match in _URL_RE.finditer(clean):
        candidate = match.group(0).rstrip(")]}>.,;'\"")[:MAX_AUTH_URL_CHARS]
        if any(host in candidate for host in _ALLOWED_AUTH_HOSTS):
            auth_url = candidate
            break
    lowered = clean.lower()
    if any(phrase in lowered for phrase in ("authentication succeeded", "authentication successful", "successfully authenticated", "signed in successfully")):
        state, evidence = "AUTHENTICATED", "AGY_OAUTH_USER_CODE_ACCEPTED"
    elif any(phrase in lowered for phrase in ("failed to authenticate", "authorization code was rejected", "authentication failed")):
        state, evidence = "FAILED", "AGY_OAUTH_USER_CODE_REJECTED"
    elif "authorization code" in lowered and any(word in lowered for word in ("enter", "paste", "input")):
        state, evidence = "WAITING_CODE", "AGY_OAUTH_WAITING_FOR_USER_CODE"
    elif auth_url:
        state, evidence = "WAITING_BROWSER", "AGY_OAUTH_URL_READY"
    else:
        state, evidence = "STARTING", "AGY_AUTH_TERMINAL_STARTING"
    return AuthProjection(state, auth_url, CORE_AGENT_SESSION, AUTH_WINDOW, utc_now(), evidence)


class AgentAuthBridge:
    """Typed bridge for Antigravity CLI remote/SSH OAuth.

    `agy` lives in the persistent `agy-gen:auth` tmux window. The browser-facing
    layer receives only a sanitized authorization URL and one-way code ingress.
    The code is streamed through tmux stdin and never appears in process argv,
    GenOS state, logs or the API response.
    """

    def __init__(
        self,
        store: AgentRuntimeStore,
        *,
        tmux_binary: str | None = None,
        agy_binary: str | None = None,
        gemini_binary: str | None = None,
    ) -> None:
        self.store = store
        self.tmux = tmux_binary or shutil.which("tmux")
        # `gemini_binary` remains a constructor compatibility alias for older
        # fixtures; it is treated as an agy-compatible test binary only.
        managed = Path("/var/lib/genos/tools/antigravity-cli/current/agy")
        self.agy = agy_binary or gemini_binary or (str(managed) if managed.is_file() else shutil.which("agy"))

    def start(self, *, restart: bool = False) -> dict[str, Any]:
        self.store.identity()
        if not self.tmux:
            raise AgentNeedsAction("tmux is not installed")
        if not self.agy:
            raise AgentNeedsAction("Antigravity CLI is not installed")
        self._ensure_user_settings()
        if restart and self.has_auth_window():
            self._run_tmux(["kill-window", "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}"], check=False)
        if not self.has_auth_window():
            self._create_auth_window()
        deadline = time.monotonic() + 2.0
        projection = self.status()
        while projection["state"] == "STARTING" and time.monotonic() < deadline:
            time.sleep(0.1)
            projection = self.status()
        return projection

    def status(self) -> dict[str, Any]:
        if not self.tmux or not self.has_auth_window():
            return AuthProjection("IDLE", None, CORE_AGENT_SESSION, AUTH_WINDOW, utc_now(), "AUTH_WINDOW_NOT_RUNNING").to_dict()
        return parse_auth_terminal(self._capture()).to_dict()

    def submit_code(self, value: str) -> dict[str, Any]:
        if not self.tmux or not self.has_auth_window():
            raise AgentNeedsAction("agy-gen authentication terminal is not running")
        projection = self.status()
        if projection["state"] not in {"WAITING_CODE", "WAITING_BROWSER"}:
            raise AgentAuthError("authentication terminal is not waiting for an authorization code")
        code = normalize_auth_code(value)
        buffer_name = f"genos-auth-{uuid.uuid4().hex}"
        try:
            loaded = subprocess.run(
                [self.tmux, "load-buffer", "-b", buffer_name, "-"], input=code, capture_output=True, text=True,
                check=False, timeout=10, shell=False, env=self._env(),
            )
            if loaded.returncode != 0:
                raise AgentAuthError("authorization code could not be staged for agy-gen")
            pasted = self._run_tmux(["paste-buffer", "-d", "-b", buffer_name, "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}"], check=False)
            if pasted.returncode != 0:
                raise AgentAuthError("authorization code could not be delivered to agy-gen")
            self._run_tmux(["send-keys", "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}", "Enter"])
        finally:
            self._run_tmux(["delete-buffer", "-b", buffer_name], check=False)
        return {
            "agent_id": "agy-gen",
            "provider_cli": "antigravity",
            "state": "SUBMITTED",
            "tmux_session": CORE_AGENT_SESSION,
            "tmux_window": AUTH_WINDOW,
            "observed_at": utc_now(),
            "evidence": "AUTH_CODE_STREAMED_TO_TMUX_STDIN",
        }

    def has_auth_window(self) -> bool:
        if not self.tmux:
            return False
        completed = self._run_tmux(["list-windows", "-t", CORE_AGENT_SESSION, "-F", "#{window_name}"], check=False)
        if completed.returncode != 0:
            return False
        return AUTH_WINDOW in {line.strip() for line in completed.stdout.splitlines()}

    def _create_auth_window(self) -> None:
        assert self.tmux is not None and self.agy is not None
        # Antigravity officially uses a manual URL/code flow when it detects a
        # remote SSH session. The service itself is not reached through SSH, so
        # we project a minimal SSH environment into this typed auth process to
        # select the documented remote flow without enabling a browser shell.
        env_command = [
            "/usr/bin/env",
            "NO_COLOR=1",
            "AGY_CLI_DISABLE_AUTO_UPDATE=true",
            "SSH_CONNECTION=127.0.0.1 0 127.0.0.1 0",
            f"HOME={self.store.root}",
            self.agy,
        ]
        command = shlex.join(env_command)
        if self._has_session():
            argv = [self.tmux, "new-window", "-d", "-t", CORE_AGENT_SESSION, "-n", AUTH_WINDOW, "-c", str(self.store.workspace), command]
        else:
            argv = [self.tmux, "new-session", "-d", "-s", CORE_AGENT_SESSION, "-n", AUTH_WINDOW, "-c", str(self.store.workspace), command]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10, shell=False, env=self._env())
        if completed.returncode != 0:
            raise AgentAuthError("agy-gen authentication tmux window could not be created")

    def _capture(self) -> str:
        completed = self._run_tmux(["capture-pane", "-p", "-J", "-S", "-240", "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}"], check=False)
        return completed.stdout[-256 * 1024 :] if completed.returncode == 0 else ""

    def _has_session(self) -> bool:
        return self._run_tmux(["has-session", "-t", CORE_AGENT_SESSION], check=False).returncode == 0

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.store.root)
        env["SHELL"] = "/bin/sh"
        env["TMUX_TMPDIR"] = str(self.store.root)
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        return env

    def _run_tmux(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if not self.tmux:
            raise AgentNeedsAction("tmux is not installed")
        return subprocess.run([self.tmux, *args], capture_output=True, text=True, check=check, timeout=10, shell=False, env=self._env())

    def _ensure_user_settings(self) -> None:
        path = self.store.settings_path
        payload: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.setdefault("permissions", {})
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(".settings.json.tmp")
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
