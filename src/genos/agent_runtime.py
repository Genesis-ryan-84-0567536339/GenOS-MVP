from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from typing import Any

from .agent_cli_update import AntigravityCliManager
from .redaction import redact


CORE_AGENT_ID = "agy-gen"
CORE_AGENT_SESSION = "agy-gen"
TARGET_PROVIDER = "antigravity"
TARGET_MODEL = "gemini-3.7-flash-high"
TARGET_THINKING_LEVEL = "HIGH"
TARGET_APPROVAL_MODE = "yolo"
DEFAULT_ROOT = Path("/var/lib/genos/agents/agy-gen")
MAX_RESULT_CHARS = 64 * 1024


class AgentRuntimeError(RuntimeError):
    pass


class AgentBusyError(AgentRuntimeError):
    pass


class AgentNeedsAction(AgentRuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(redact(payload), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AgentRuntimeError(f"invalid JSON object: {path}")
    return redact(value)


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    state: str
    cli_path: str | None
    cli_version: str | None
    model: str
    thinking_level: str
    approval_mode: str
    observed_at: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_cli": TARGET_PROVIDER,
            "state": self.state,
            "cli_path": self.cli_path,
            "cli_version": self.cli_version,
            "model": self.model,
            "thinking_level": self.thinking_level,
            "approval_mode": self.approval_mode,
            "observed_at": self.observed_at,
            "evidence": self.evidence,
        }


class AgentRuntimeStore:
    """Durable local projection for the single MVP Core Agent.

    Agent identity, memory/skill revisions, work claim and tmux binding are
    durable authorities independent from the replaceable provider CLI version.
    Provider credentials remain provider-owned and are never persisted here.
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.identity_path = self.root / "identity.json"
        self.runtime_path = self.root / "runtime.json"
        self.provider_path = self.root / "provider.json"
        self.settings_path = self.root / ".gemini" / "antigravity-cli" / "settings.json"
        self.workspace = self.root / "workspace"
        self.queue_dir = self.root / "tasks" / "queued"
        self.result_dir = self.root / "tasks" / "results"
        self.claim_path = self.root / "tasks" / "active-claim.json"
        self.memory_dir = self.root / "memory"
        self.skills_dir = self.root / "skills"

    def ensure_seed(self, *, instance_id: str) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for path in (self.workspace, self.queue_dir, self.result_dir, self.memory_dir, self.skills_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

        existing = _load_json(self.identity_path)
        if existing is not None:
            if existing.get("agent_id") != CORE_AGENT_ID:
                raise AgentRuntimeError("Core Agent identity path belongs to another agent")
            if existing.get("instance_id") != instance_id:
                raise AgentRuntimeError("Core Agent identity belongs to another GenOS instance")
            # Migrate only provider intent metadata. Identity itself remains the
            # same durable agy-gen record across CLI replacement.
            target = existing.get("provider_target")
            if isinstance(target, dict) and target.get("provider") != TARGET_PROVIDER:
                target.update(
                    {
                        "provider": TARGET_PROVIDER,
                        "model": TARGET_MODEL,
                        "thinking_level": TARGET_THINKING_LEVEL,
                        "approval_mode": TARGET_APPROVAL_MODE,
                    }
                )
                _atomic_json(self.identity_path, existing)
        else:
            existing = {
                "schema_version": "1.1",
                "agent_id": CORE_AGENT_ID,
                "display_name": CORE_AGENT_ID,
                "agent_kind": "CORE",
                "instance_id": instance_id,
                "concurrency": 1,
                "provider_target": {
                    "provider": TARGET_PROVIDER,
                    "model": TARGET_MODEL,
                    "thinking_level": TARGET_THINKING_LEVEL,
                    "approval_mode": TARGET_APPROVAL_MODE,
                },
                "runtime_binding": {"type": "tmux", "session_name": CORE_AGENT_SESSION},
                "created_at": utc_now(),
            }
            _atomic_json(self.identity_path, existing)

        self._ensure_antigravity_settings()
        if _load_json(self.runtime_path) is None:
            self.write_runtime(state="NEEDS_ACTION", reason="PROVIDER_NOT_VERIFIED", tmux_state="UNKNOWN")
        return existing

    def identity(self) -> dict[str, Any]:
        value = _load_json(self.identity_path)
        if value is None:
            raise AgentNeedsAction("agy-gen identity is not seeded")
        return value

    def provider(self) -> dict[str, Any] | None:
        return _load_json(self.provider_path)

    def write_provider(self, probe: Any) -> None:
        payload = probe.to_dict() if hasattr(probe, "to_dict") else dict(probe)
        _atomic_json(self.provider_path, {"schema_version": "1.1", **payload})

    def write_runtime(self, *, state: str, reason: str, tmux_state: str, task_id: str | None = None) -> None:
        _atomic_json(
            self.runtime_path,
            {
                "schema_version": "1.0",
                "agent_id": CORE_AGENT_ID,
                "state": state,
                "reason": reason,
                "tmux_state": tmux_state,
                "active_task_id": task_id,
                "observed_at": utc_now(),
            },
        )

    def status(self) -> dict[str, Any]:
        return {
            "identity": self.identity(),
            "provider": self.provider() or {
                "provider_cli": TARGET_PROVIDER,
                "state": "NEEDS_ACTION",
                "evidence": "provider_not_probed",
                "model": TARGET_MODEL,
                "thinking_level": TARGET_THINKING_LEVEL,
                "approval_mode": TARGET_APPROVAL_MODE,
            },
            "runtime": _load_json(self.runtime_path) or {
                "state": "UNKNOWN",
                "reason": "runtime_not_observed",
                "tmux_state": "UNKNOWN",
            },
            "claim": _load_json(self.claim_path),
        }

    def append_revision(self, kind: str, name: str, content: str, *, source: str) -> dict[str, Any]:
        if kind not in {"memory", "skill"}:
            raise ValueError("kind must be memory or skill")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 128 or any(char in clean_name for char in "/\\\x00"):
            raise ValueError("invalid revision name")
        root = self.memory_dir if kind == "memory" else self.skills_dir
        target = root / clean_name
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        revision = len(sorted(target.glob("*.json"))) + 1
        payload = {
            "schema_version": "1.0",
            "kind": kind,
            "name": clean_name,
            "revision": revision,
            "source": source,
            "content": content,
            "created_at": utc_now(),
        }
        _atomic_json(target / f"{revision:06d}.json", payload)
        return redact(payload)

    def list_revisions(self, kind: str, name: str) -> list[dict[str, Any]]:
        root = self.memory_dir if kind == "memory" else self.skills_dir
        target = root / name
        if not target.is_dir():
            return []
        return [value for path in sorted(target.glob("*.json")) if (value := _load_json(path)) is not None]

    def claim_work(self, *, task_id: str) -> dict[str, Any]:
        self.claim_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"agent_id": CORE_AGENT_ID, "task_id": task_id, "claimed_at": utc_now(), "concurrency": 1}
        try:
            fd = os.open(self.claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise AgentBusyError("agy-gen already has an active work claim") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.claim_path.unlink(missing_ok=True)
            raise
        return payload

    def release_work(self, *, task_id: str) -> None:
        claim = _load_json(self.claim_path)
        if claim is None:
            return
        if claim.get("task_id") != task_id:
            raise AgentBusyError("active work claim belongs to a different task")
        self.claim_path.unlink(missing_ok=True)

    def queue_task(self, prompt: str) -> str:
        provider = self.provider() or {}
        if provider.get("state") != "ACTIVE":
            raise AgentNeedsAction("provider is not ACTIVE; run an explicit provider activation probe first")
        task_id = str(uuid.uuid4())
        self.claim_work(task_id=task_id)
        try:
            _atomic_json(
                self.queue_dir / f"{task_id}.json",
                {"task_id": task_id, "agent_id": CORE_AGENT_ID, "prompt": prompt, "state": "QUEUED", "created_at": utc_now()},
            )
        except Exception:
            self.release_work(task_id=task_id)
            raise
        return task_id

    def _ensure_antigravity_settings(self) -> None:
        payload: dict[str, Any] = {}
        if self.settings_path.is_file():
            try:
                current = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload = current
            except (OSError, json.JSONDecodeError):
                payload = {}
        # Keep permissions explicit in product state; the Owner-selected yolo
        # behavior is passed only to the dedicated task process.
        payload.setdefault("permissions", {})
        self.settings_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_json(self.settings_path, payload)


class AntigravityCliAdapter:
    def __init__(self, store: AgentRuntimeStore, binary: str | None = None) -> None:
        self.store = store
        managed = Path("/var/lib/genos/tools/antigravity-cli/current/agy")
        self.binary = binary or (str(managed) if managed.is_file() else shutil.which("agy"))

    def probe_installation(self) -> ProviderProbe:
        if not self.binary:
            return self._probe("NEEDS_ACTION", None, "AGY_CLI_NOT_FOUND")
        try:
            version = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=15, check=False, shell=False, env=self._env()
            )
            help_result = subprocess.run(
                [self.binary, "--help"], capture_output=True, text=True, timeout=20, check=False, shell=False, env=self._env()
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._probe("DEGRADED", None, "AGY_CLI_CAPABILITY_PROBE_FAILED")
        lines = version.stdout.strip().splitlines()
        cli_version = lines[0].strip()[:160] if version.returncode == 0 and lines else None
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        capabilities = all(flag in help_text for flag in ("--model", "--effort", "--output-format"))
        if version.returncode != 0 or help_result.returncode != 0 or not capabilities:
            return self._probe("DEGRADED", cli_version, "AGY_CLI_REQUIRED_FLAGS_MISSING")
        return self._probe("INSTALLED", cli_version, "AGY_CLI_CAPABILITY_OK")

    def activate_with_real_probe(self, *, timeout: float = 90.0) -> ProviderProbe:
        installed = self.probe_installation()
        if installed.state != "INSTALLED" or not self.binary:
            self.store.write_provider(installed)
            return installed
        marker = f"GENOS_AGY_GEN_PROBE_OK_{uuid.uuid4().hex[:12]}"
        try:
            completed = subprocess.run(
                self.command_for_prompt(f"Reply with exactly this marker and nothing else: {marker}"),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=self._env(),
                cwd=self.store.workspace,
            )
        except subprocess.TimeoutExpired:
            probe = self._probe("NEEDS_ACTION", installed.cli_version, "PROVIDER_AUTH_OR_MODEL_PROBE_TIMEOUT")
            self.store.write_provider(probe)
            return probe
        except OSError:
            probe = self._probe("DEGRADED", installed.cli_version, "PROVIDER_EXEC_UNAVAILABLE")
            self.store.write_provider(probe)
            return probe
        success = completed.returncode == 0 and marker in completed.stdout
        if success:
            try:
                envelope = json.loads(completed.stdout)
                success = isinstance(envelope, dict) and envelope.get("status") == "SUCCESS" and marker in str(envelope.get("response", ""))
            except json.JSONDecodeError:
                success = marker in completed.stdout
        probe = self._probe(
            "ACTIVE" if success else "NEEDS_ACTION",
            installed.cli_version,
            "REAL_MODEL_PROBE_PASS" if success else "AUTH_MODEL_OR_CONFIG_NOT_VERIFIED",
        )
        self.store.write_provider(probe)
        return probe

    def command_for_prompt(self, prompt: str) -> list[str]:
        if not self.binary:
            raise AgentNeedsAction("Antigravity CLI is not installed")
        return [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            TARGET_MODEL,
            "--effort",
            "high",
            "--dangerously-skip-permissions",
        ]

    def run_task(self, prompt: str, *, timeout: float = 900.0) -> dict[str, Any]:
        completed = subprocess.run(
            self.command_for_prompt(prompt),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=self._env(),
            cwd=self.store.workspace,
        )
        stdout = completed.stdout[:MAX_RESULT_CHARS]
        digest = hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest()
        if completed.returncode != 0:
            return {"state": "FAILED", "error": "PROVIDER_COMMAND_FAILED", "output_sha256": digest, "observed_at": utc_now()}
        return {"state": "SUCCEEDED", "output": redact(stdout), "output_sha256": digest, "observed_at": utc_now()}

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.store.root)
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        env.setdefault("NO_COLOR", "1")
        return env

    def _probe(self, state: str, version: str | None, evidence: str) -> ProviderProbe:
        return ProviderProbe(
            state=state,
            cli_path=self.binary,
            cli_version=version,
            model=TARGET_MODEL,
            thinking_level=TARGET_THINKING_LEVEL,
            approval_mode=TARGET_APPROVAL_MODE,
            observed_at=utc_now(),
            evidence=evidence,
        )


# Compatibility import name for MVP-04 callers. It intentionally points to the
# forward Antigravity adapter; no Gemini binary is invoked through this alias.
GeminiCliAdapter = AntigravityCliAdapter


class TmuxController:
    def __init__(self, store: AgentRuntimeStore, tmux_binary: str | None = None) -> None:
        self.store = store
        self.binary = tmux_binary or shutil.which("tmux")

    def has_session(self) -> bool:
        if not self.binary:
            return False
        completed = subprocess.run(
            [self.binary, "has-session", "-t", CORE_AGENT_SESSION], capture_output=True, text=True, check=False,
            timeout=5, shell=False, env=self._env(),
        )
        return completed.returncode == 0

    def ensure_worker_session(self) -> bool:
        if not self.binary:
            raise AgentNeedsAction("tmux is not installed")
        if self.has_session():
            return False
        python = shutil.which("python3") or "/usr/bin/python3"
        command = shlex.join([python, "-m", "genos.agent_runtime", "worker", "--state-dir", str(self.store.root)])
        completed = subprocess.run(
            [self.binary, "new-session", "-d", "-s", CORE_AGENT_SESSION, "-c", str(self.store.workspace), command],
            capture_output=True, text=True, check=False, timeout=10, shell=False, env=self._env(),
        )
        if completed.returncode != 0:
            raise AgentRuntimeError("tmux worker session could not be created")
        return True

    def restart_worker_session(self) -> None:
        if not self.binary:
            raise AgentNeedsAction("tmux is not installed")
        subprocess.run(
            [self.binary, "kill-session", "-t", CORE_AGENT_SESSION], capture_output=True, text=True, check=False,
            timeout=5, shell=False, env=self._env(),
        )
        self.ensure_worker_session()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.store.root)
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        return env


def managed_cli_update(store: AgentRuntimeStore, *, force: bool = False) -> dict[str, Any]:
    manager = AntigravityCliManager(agent_state_root=store.root)
    was_active = (store.provider() or {}).get("state") == "ACTIVE"

    def post_cutover(binary: str) -> bool:
        if not was_active:
            return True
        return AntigravityCliAdapter(store, binary=binary).activate_with_real_probe(timeout=90).state == "ACTIVE"

    result = manager.ensure_latest(force=force, post_cutover_probe=post_cutover)
    return result.to_dict()


def supervisor_loop(store: AgentRuntimeStore, *, interval: float = 5.0) -> int:
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    adapter = AntigravityCliAdapter(store)
    tmux = TmuxController(store)
    first_tick = True
    while not stop:
        managed_cli_update(store, force=first_tick)
        first_tick = False
        provider = store.provider()
        if provider is None:
            installed = adapter.probe_installation()
            store.write_provider(installed)
            provider = installed.to_dict()
        claim = _load_json(store.claim_path)
        if provider.get("state") != "ACTIVE":
            store.write_runtime(
                state="NEEDS_ACTION", reason=str(provider.get("evidence") or "PROVIDER_NOT_ACTIVE"),
                tmux_state="RUNNING" if tmux.has_session() else "STOPPED",
                task_id=str(claim.get("task_id")) if claim else None,
            )
        else:
            try:
                tmux.ensure_worker_session()
                store.write_runtime(
                    state="BUSY" if claim else "READY",
                    reason="WORK_CLAIM_ACTIVE" if claim else "PROVIDER_AND_TMUX_ACTIVE",
                    tmux_state="RUNNING", task_id=str(claim.get("task_id")) if claim else None,
                )
            except AgentRuntimeError:
                store.write_runtime(
                    state="DEGRADED", reason="TMUX_WORKER_START_FAILED", tmux_state="STOPPED",
                    task_id=str(claim.get("task_id")) if claim else None,
                )
        time.sleep(max(0.5, interval))
    return 0


def worker_loop(store: AgentRuntimeStore, *, interval: float = 1.0) -> int:
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    adapter = AntigravityCliAdapter(store)
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
            result = {"state": "FAILED", "error": type(exc).__name__, "observed_at": utc_now()}
        _atomic_json(store.result_dir / f"{task_id}.json", {"task_id": task_id, "agent_id": CORE_AGENT_ID, **result})
        task_path.unlink(missing_ok=True)
        store.release_work(task_id=task_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m genos.agent_runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("supervisor", "worker", "status", "probe", "activate", "restart", "update-cli"):
        item = sub.add_parser(name)
        item.add_argument("--state-dir", default=str(DEFAULT_ROOT))
        if name in {"supervisor", "worker"}:
            item.add_argument("--interval", type=float, default=5.0 if name == "supervisor" else 1.0)
        if name == "update-cli":
            item.add_argument("--force", action="store_true")
    task = sub.add_parser("task")
    task.add_argument("--state-dir", default=str(DEFAULT_ROOT))
    task.add_argument("--prompt", required=True)
    seed = sub.add_parser("seed")
    seed.add_argument("--state-dir", default=str(DEFAULT_ROOT))
    seed.add_argument("--instance-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AgentRuntimeStore(args.state_dir)
    if args.command == "seed":
        print(json.dumps(store.ensure_seed(instance_id=args.instance_id), sort_keys=True)); return 0
    if args.command == "supervisor":
        store.identity(); return supervisor_loop(store, interval=args.interval)
    if args.command == "worker":
        store.identity(); return worker_loop(store, interval=args.interval)
    if args.command == "status":
        print(json.dumps(store.status(), sort_keys=True)); return 0
    if args.command == "probe":
        probe = AntigravityCliAdapter(store).probe_installation(); store.write_provider(probe)
        print(json.dumps(probe.to_dict(), sort_keys=True)); return 0 if probe.state == "INSTALLED" else 3
    if args.command == "activate":
        probe = AntigravityCliAdapter(store).activate_with_real_probe()
        print(json.dumps(probe.to_dict(), sort_keys=True)); return 0 if probe.state == "ACTIVE" else 3
    if args.command == "restart":
        TmuxController(store).restart_worker_session(); print(json.dumps({"agent_id": CORE_AGENT_ID, "state": "RESTARTED"}, sort_keys=True)); return 0
    if args.command == "update-cli":
        result = managed_cli_update(store, force=bool(args.force)); print(json.dumps(result, sort_keys=True)); return 0 if result.get("update_state") not in {"FAILED"} else 4
    if args.command == "task":
        task_id = store.queue_task(args.prompt); print(json.dumps({"agent_id": CORE_AGENT_ID, "task_id": task_id, "state": "QUEUED"}, sort_keys=True)); return 0
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
