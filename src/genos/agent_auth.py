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

from .agent_runtime import (
    AgentNeedsAction,
    AgentRuntimeError,
    AgentRuntimeStore,
    CORE_AGENT_SESSION,
    TARGET_MODEL,
    utc_now,
)


AUTH_WINDOW = "auth"
RUNTIME_WINDOW = "runtime"
AUTH_SELECTED_TYPE = "oauth-personal"
MAX_AUTH_CODE_CHARS = 4096
MAX_AUTH_URL_CHARS = 8192

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_AUTH_URL_RE = re.compile(r"https://accounts\.google\.com/[^\s\x00-\x1f]+")


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
    url_match = _AUTH_URL_RE.search(clean)
    auth_url = url_match.group(0).rstrip(")]}>.,;'\"")[:MAX_AUTH_URL_CHARS] if url_match else None
    lowered = clean.lower()

    if "authentication succeeded" in lowered:
        state, evidence = "AUTHENTICATED", "GEMINI_OAUTH_USER_CODE_ACCEPTED"
    elif "failed to authenticate with user code" in lowered or "failed to authenticate with authorization code" in lowered:
        state, evidence = "FAILED", "GEMINI_OAUTH_USER_CODE_REJECTED"
    elif "enter the authorization code:" in lowered:
        state, evidence = "WAITING_CODE", "GEMINI_OAUTH_WAITING_FOR_USER_CODE"
    elif auth_url:
        state, evidence = "WAITING_BROWSER", "GEMINI_OAUTH_URL_READY"
    else:
        state, evidence = "STARTING", "GEMINI_OAUTH_TERMINAL_STARTING"

    return AuthProjection(
        state=state,
        auth_url=auth_url,
        tmux_session=CORE_AGENT_SESSION,
        tmux_window=AUTH_WINDOW,
        observed_at=utc_now(),
        evidence=evidence,
    )


class AgentAuthBridge:
    """Typed bridge for Gemini CLI's interactive NO_BROWSER OAuth flow.

    The Gemini process lives in the persistent `agy-gen` tmux session. The
    browser-facing layer receives only a short-lived authorization URL and a
    typed code-submission surface. Authorization codes are streamed to tmux via
    stdin and are never persisted in GenOS state, logged, put in a process argv,
    or returned by the API.
    """

    def __init__(
        self,
        store: AgentRuntimeStore,
        *,
        tmux_binary: str | None = None,
        gemini_binary: str | None = None,
    ) -> None:
        self.store = store
        self.tmux = tmux_binary or shutil.which("tmux")
        self.gemini = gemini_binary or shutil.which("gemini")

    def start(self, *, restart: bool = False) -> dict[str, Any]:
        self.store.identity()
        if not self.tmux:
            raise AgentNeedsAction("tmux is not installed")
        if not self.gemini:
            raise AgentNeedsAction("Gemini CLI is not installed")
        self._ensure_user_auth_settings()

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
            return AuthProjection(
                state="IDLE",
                auth_url=None,
                tmux_session=CORE_AGENT_SESSION,
                tmux_window=AUTH_WINDOW,
                observed_at=utc_now(),
                evidence="AUTH_WINDOW_NOT_RUNNING",
            ).to_dict()
        return parse_auth_terminal(self._capture()).to_dict()

    def submit_code(self, value: str) -> dict[str, Any]:
        if not self.tmux or not self.has_auth_window():
            raise AgentNeedsAction("agy-gen authentication terminal is not running")
        projection = self.status()
        if projection["state"] not in {"WAITING_CODE", "WAITING_BROWSER"}:
            raise AgentAuthError("authentication terminal is not waiting for an authorization code")
        code = normalize_auth_code(value)
        buffer_name = f"genos-auth-{uuid.uuid4().hex}"
        env = self._env()
        try:
            # tmux load-buffer reads the sensitive code from stdin; unlike
            # send-keys with a literal argument, the code is absent from ps/argv.
            loaded = subprocess.run(
                [self.tmux, "load-buffer", "-b", buffer_name, "-"],
                input=code,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                shell=False,
                env=env,
            )
            if loaded.returncode != 0:
                raise AgentAuthError("authorization code could not be staged for agy-gen")
            pasted = self._run_tmux(
                ["paste-buffer", "-d", "-b", buffer_name, "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}"],
                check=False,
            )
            if pasted.returncode != 0:
                raise AgentAuthError("authorization code could not be delivered to agy-gen")
            self._run_tmux(["send-keys", "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}", "Enter"])
        finally:
            # Defensive cleanup if paste-buffer did not delete the buffer.
            self._run_tmux(["delete-buffer", "-b", buffer_name], check=False)
        return {
            "agent_id": "agy-gen",
            "state": "SUBMITTED",
            "tmux_session": CORE_AGENT_SESSION,
            "tmux_window": AUTH_WINDOW,
            "observed_at": utc_now(),
            "evidence": "AUTH_CODE_STREAMED_TO_TMUX_STDIN",
        }

    def has_auth_window(self) -> bool:
        if not self.tmux:
            return False
        completed = self._run_tmux(
            ["list-windows", "-t", CORE_AGENT_SESSION, "-F", "#{window_name}"],
            check=False,
        )
        if completed.returncode != 0:
            return False
        return AUTH_WINDOW in {line.strip() for line in completed.stdout.splitlines()}

    def _create_auth_window(self) -> None:
        assert self.tmux is not None
        assert self.gemini is not None
        env_command = [
            "/usr/bin/env",
            "NO_BROWSER=true",
            "NO_COLOR=1",
            f"HOME={self.store.root}",
            f"GEMINI_CLI_SYSTEM_SETTINGS_PATH={self.store.settings_path}",
            self.gemini,
            "--model",
            TARGET_MODEL,
        ]
        command = shlex.join(env_command)
        if self._has_session():
            completed = self._run_tmux(
                [
                    "new-window",
                    "-d",
                    "-t",
                    CORE_AGENT_SESSION,
                    "-n",
                    AUTH_WINDOW,
                    "-c",
                    str(self.store.workspace),
                    command,
                ],
                check=False,
            )
        else:
            completed = self._run_tmux(
                [
                    "new-session",
                    "-d",
                    "-s",
                    CORE_AGENT_SESSION,
                    "-n",
                    AUTH_WINDOW,
                    "-c",
                    str(self.store.workspace),
                    command,
                ],
                check=False,
            )
        if completed.returncode != 0:
            raise AgentAuthError("agy-gen authentication tmux window could not be created")

    def _capture(self) -> str:
        completed = self._run_tmux(
            ["capture-pane", "-p", "-J", "-S", "-240", "-t", f"{CORE_AGENT_SESSION}:{AUTH_WINDOW}"],
            check=False,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout[-256 * 1024 :]

    def _has_session(self) -> bool:
        completed = self._run_tmux(["has-session", "-t", CORE_AGENT_SESSION], check=False)
        return completed.returncode == 0

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.store.root)
        # systemd services use PrivateTmp for sandboxing. Pin tmux's socket root
        # to durable Agent state so worker/API/CLI attach to one shared session
        # instead of process-private /tmp namespaces.
        env["TMUX_TMPDIR"] = str(self.store.root)
        return env

    def _run_tmux(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if not self.tmux:
            raise AgentNeedsAction("tmux is not installed")
        return subprocess.run(
            [self.tmux, *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
            shell=False,
            env=self._env(),
        )

    def _ensure_user_auth_settings(self) -> None:
        path = self.store.root / ".gemini" / "settings.json"
        payload: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
        security = payload.setdefault("security", {})
        if not isinstance(security, dict):
            security = {}
            payload["security"] = security
        auth = security.setdefault("auth", {})
        if not isinstance(auth, dict):
            auth = {}
            security["auth"] = auth
        auth["selectedType"] = AUTH_SELECTED_TYPE
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(".settings.json.tmp")
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)