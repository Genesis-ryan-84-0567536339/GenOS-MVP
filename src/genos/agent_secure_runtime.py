from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
from typing import Any

from .agent_auth import RUNTIME_WINDOW
from .agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeStore,
    CORE_AGENT_ID,
    CORE_AGENT_SESSION,
    GeminiCliAdapter,
    ProviderProbe,
    TmuxController,
    _atomic_json,
    _load_json,
    utc_now,
)
from .auth_service import CredentialError, CredentialService
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider


@dataclass(frozen=True, slots=True)
class BoundProviderProbe:
    state: str
    cli_path: str | None
    cli_version: str | None
    model: str
    thinking_level: str
    approval_mode: str
    observed_at: str
    evidence: str
    credential_ref: str | None

    @classmethod
    def from_probe(cls, probe: ProviderProbe, credential_ref: str | None) -> "BoundProviderProbe":
        return cls(
            state=probe.state,
            cli_path=probe.cli_path,
            cli_version=probe.cli_version,
            model=probe.model,
            thinking_level=probe.thinking_level,
            approval_mode=probe.approval_mode,
            observed_at=probe.observed_at,
            evidence=probe.evidence,
            credential_ref=credential_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "cli_path": self.cli_path,
            "cli_version": self.cli_version,
            "model": self.model,
            "thinking_level": self.thinking_level,
            "approval_mode": self.approval_mode,
            "observed_at": self.observed_at,
            "evidence": self.evidence,
            "credential_ref": self.credential_ref,
        }


class AgentCredentialResolver:
    """Internal-only SecretRef resolution for the agy-gen process boundary."""

    def __init__(self) -> None:
        self.service = CredentialService(
            PostgresProductStore(),
            LocalFileSecretProvider(Path("/var/lib/genos/secrets")),
        )

    def resolve_api_key(self, secret_id: str) -> str:
        try:
            return self.service.get_secret_for_consumer(secret_id, consumer=CORE_AGENT_ID)
        except CredentialError as exc:
            raise AgentRuntimeError("configured Gemini credential is unavailable or not granted to agy-gen") from exc


class SecretAwareGeminiAdapter(GeminiCliAdapter):
    def __init__(
        self,
        store: AgentRuntimeStore,
        binary: str | None = None,
        *,
        credential_id: str | None = None,
        resolver: AgentCredentialResolver | None = None,
    ) -> None:
        super().__init__(store, binary=binary)
        persisted = store.provider() or {}
        self.credential_id = credential_id or (
            str(persisted.get("credential_ref")) if persisted.get("credential_ref") else None
        )
        self.resolver = resolver

    def activate_with_real_probe(self, *, timeout: float = 90.0) -> BoundProviderProbe:
        # Parent implementation performs the direct marker round-trip. _env()
        # below injects raw API material only into the child process when a
        # SecretRef is explicitly bound. OAuth uses Gemini's provider-owned
        # cached credential under agy-gen HOME and needs no SecretRef.
        probe = super().activate_with_real_probe(timeout=timeout)
        bound = BoundProviderProbe.from_probe(
            probe,
            self.credential_id if probe.state == "ACTIVE" and self.credential_id else None,
        )
        self.store.write_provider(bound)  # duck-typed safe public metadata
        return bound

    def _env(self) -> dict[str, str]:
        env = super()._env()
        if self.credential_id:
            resolver = self.resolver or AgentCredentialResolver()
            env["GEMINI_API_KEY"] = resolver.resolve_api_key(self.credential_id)
        return env


class SecureTmuxController(TmuxController):
    """Owns the persistent `agy-gen` tmux session.

    The session may contain an interactive `auth` window and a supervised
    `runtime` window. Restarting runtime must never destroy an in-progress auth
    flow or the session identity.
    """

    def has_runtime_window(self) -> bool:
        if not self.binary:
            return False
        completed = subprocess.run(
            [self.binary, "list-windows", "-t", CORE_AGENT_SESSION, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            shell=False,
            env=self._env(),
        )
        if completed.returncode != 0:
            return False
        return RUNTIME_WINDOW in {line.strip() for line in completed.stdout.splitlines()}

    def ensure_worker_session(self) -> bool:
        if not self.binary:
            raise AgentRuntimeError("tmux is not installed")
        if self.has_runtime_window():
            return False
        python = shutil.which("python3") or "/usr/bin/python3"
        command = shlex.join(
            [python, "-m", "genos.agent_secure_runtime", "worker", "--state-dir", str(self.store.root)]
        )
        if self.has_session():
            argv = [
                self.binary,
                "new-window",
                "-d",
                "-t",
                CORE_AGENT_SESSION,
                "-n",
                RUNTIME_WINDOW,
                "-c",
                str(self.store.workspace),
                command,
            ]
        else:
            argv = [
                self.binary,
                "new-session",
                "-d",
                "-s",
                CORE_AGENT_SESSION,
                "-n",
                RUNTIME_WINDOW,
                "-c",
                str(self.store.workspace),
                command,
            ]
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            shell=False,
            env=self._env(),
        )
        if completed.returncode != 0:
            raise AgentRuntimeError("tmux secure runtime window could not be created")
        return True

    def restart_worker_session(self) -> None:
        if not self.binary:
            raise AgentRuntimeError("tmux is not installed")
        if self.has_runtime_window():
            subprocess.run(
                [self.binary, "kill-window", "-t", f"{CORE_AGENT_SESSION}:{RUNTIME_WINDOW}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                shell=False,
                env=self._env(),
            )
        self.ensure_worker_session()


def worker_loop(store: AgentRuntimeStore, *, interval: float = 1.0) -> int:
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    adapter = SecretAwareGeminiAdapter(store)
    while not stop:
        queued = sorted(store.queue_dir.glob("*.json"))
        if not queued:
            time.sleep(max(0.2, interval))
            continue
        task_path = queued[0]
        task = _load_json(task_path)
        if task is None:
            task_path.unlink(missing_ok=True)
            continue
        task_id = str(task.get("task_id") or "")
        prompt = str(task.get("prompt") or "")
        if not task_id or not prompt:
            task_path.unlink(missing_ok=True)
            if task_id:
                store.release_work(task_id=task_id)
            continue
        store.write_runtime(state="BUSY", reason="EXECUTING_TASK", tmux_state="RUNNING", task_id=task_id)
        try:
            result = adapter.run_task(prompt)
        except (AgentRuntimeError, subprocess.TimeoutExpired) as exc:
            result = {
                "state": "FAILED",
                "error": type(exc).__name__,
                "observed_at": utc_now(),
            }
        _atomic_json(
            store.result_dir / f"{task_id}.json",
            {"task_id": task_id, "agent_id": CORE_AGENT_ID, **result},
        )
        task_path.unlink(missing_ok=True)
        store.release_work(task_id=task_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m genos.agent_secure_runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
    worker.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.command == "worker":
        store = AgentRuntimeStore(args.state_dir)
        store.identity()
        return worker_loop(store, interval=args.interval)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
