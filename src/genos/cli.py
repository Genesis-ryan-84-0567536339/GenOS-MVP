from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .agent_permissions import ensure_agent_runtime_ownership
from .agent_runtime import AgentNeedsAction, AgentRuntimeError, AgentRuntimeStore, GeminiCliAdapter
from .agent_secure_runtime import SecretAwareGeminiAdapter, SecureTmuxController
from .agent_tool_links import ensure_system_links
from .agent_tools import AgentToolError, AgentToolProvisioner
from .install import InstallError, NativeProvisioner, ReleaseArtifact, build_native_install
from .recon import collect_all
from .redaction import redact
from .state import JsonStateStore


_RESERVED_MUTATION_COMMANDS = {
    "repair",
    "update",
    "reconfigure",
    "backup",
    "restore",
    "support-bundle",
    "uninstall",
    "purge",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genos", description="GenOS MVP lifecycle CLI")
    parser.add_argument("--version", action="version", version=f"genos {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show local GenOS lifecycle state without mutation")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument("--state-dir", default=None)

    for name in ("recon", "doctor"):
        command = sub.add_parser(name, help="Run typed read-only host observations")
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("--cwd", default=None, help="Optional repository path for read-only Git facts")

    install = sub.add_parser("install", help="Plan or execute the approved fresh-install provisioner")
    install.add_argument("--mode", choices=("native", "vm"), default=None)
    install.add_argument("--release", type=Path, required=True, help="Verified release tar archive supplied by the bootstrap")
    install.add_argument("--release-sha256", required=True)
    install.add_argument("--git-sha", required=True)
    install.add_argument("--plan-only", action="store_true")
    install.add_argument("--json", action="store_true", dest="as_json")
    install.add_argument("--candidate-e2e", action="store_true", help=argparse.SUPPRESS)

    agent = sub.add_parser("agent", help="Operate the single MVP Core Agent agy-gen through typed controls")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    for name in ("status", "probe", "restart"):
        item = agent_sub.add_parser(name)
        item.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
        item.add_argument("--json", action="store_true", dest="as_json")
    activate = agent_sub.add_parser("activate")
    activate.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
    activate.add_argument("--credential-id", default=None, help="Optional SecretRef UUID granted to consumer agy-gen")
    activate.add_argument("--json", action="store_true", dest="as_json")
    provision = agent_sub.add_parser("provision")
    provision.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
    provision.add_argument("--json", action="store_true", dest="as_json")
    task = agent_sub.add_parser("task")
    task.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
    task.add_argument("--prompt", required=True)
    task.add_argument("--json", action="store_true", dest="as_json")

    for name in sorted(_RESERVED_MUTATION_COMMANDS):
        sub.add_parser(name, help="Lifecycle surface reserved for later MVP packages")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return _status(as_json=args.as_json, state_dir=args.state_dir)
    if args.command in {"recon", "doctor"}:
        return _recon(mode=args.command, as_json=args.as_json, cwd=args.cwd)
    if args.command == "install":
        return _install(args)
    if args.command == "agent":
        return _agent(args)
    return _not_implemented(args.command)


def _status(as_json: bool, state_dir: str | None) -> int:
    store = _status_store(state_dir)
    try:
        manifest = store.load_manifest()
    except PermissionError:
        payload = {"state": "NO_PERMISSION", "state_dir": str(store.root), "evidence": "manifest_not_readable"}
        _emit(payload, as_json=as_json)
        return 3
    payload: dict[str, Any]
    if manifest is None:
        payload = {"state": "NOT_INSTALLED", "state_dir": str(store.root), "evidence": "manifest_absent"}
    else:
        payload = {"state": manifest.get("state", "UNKNOWN"), "manifest": manifest, "state_dir": str(store.root)}
    _emit(payload, as_json=as_json)
    return 0


def _status_store(state_dir: str | None) -> JsonStateStore:
    if state_dir:
        return JsonStateStore(state_dir)
    system_manifest = Path("/var/lib/genos/manifest.json")
    if system_manifest.exists():
        return JsonStateStore("/var/lib/genos")
    return JsonStateStore()


def _recon(mode: str, as_json: bool, cwd: str | None) -> int:
    observations, support_class, reason = collect_all(cwd=cwd)
    payload = {
        "mode": mode,
        "read_only": True,
        "support_class": support_class.value,
        "support_reason": reason,
        "observations": [item.to_dict() for item in observations],
    }
    _emit(payload, as_json=as_json)
    return 0


def _install(args: argparse.Namespace) -> int:
    allow_candidate = bool(args.candidate_e2e and os.environ.get("GENOS_FRESH_HOST_E2E") == "1")
    release = ReleaseArtifact(
        archive=args.release,
        git_sha=args.git_sha,
        sha256=args.release_sha256,
    )
    try:
        observations, _support, _reason = collect_all(cwd=None)
        planned = build_native_install(
            observations,
            requested_mode=args.mode,
            release=release,
            allow_candidate_e2e=allow_candidate,
        )
        payload: dict[str, Any] = planned.to_dict()
        payload["command"] = "install"
        payload["plan_only"] = bool(args.plan_only)
        if args.plan_only or not planned.decision.mutation_allowed:
            _emit(payload, as_json=args.as_json)
            return 0 if args.plan_only else 3
        run = NativeProvisioner(planned).execute()
        payload["run"] = run.to_dict()
        payload["state"] = run.state.value
        _emit(payload, as_json=args.as_json)
        return 0
    except InstallError as exc:
        payload = redact(
            {
                "command": "install",
                "state": "FAILED_PRECONDITION" if args.plan_only else "FAILED",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        _emit(payload, as_json=args.as_json)
        return 4


def _agent(args: argparse.Namespace) -> int:
    store = AgentRuntimeStore(args.state_dir)
    try:
        if args.agent_command == "provision":
            toolchain = AgentToolProvisioner().provision()
            links = ensure_system_links()
            probe = GeminiCliAdapter(store).probe_installation()
            store.write_provider(probe)
            ensure_agent_runtime_ownership(store.root)
            payload = {
                "agent_id": "agy-gen",
                "state": "NEEDS_ACTION" if probe.state == "INSTALLED" else "DEGRADED",
                "reason": "PROVIDER_AUTH_REQUIRED" if probe.state == "INSTALLED" else probe.evidence,
                "toolchain": toolchain.to_dict(),
                "system_links": links,
                "provider_probe": probe.to_dict(),
            }
            _emit_agent(payload, as_json=args.as_json)
            return 0 if toolchain.state == "READY" and probe.state == "INSTALLED" else 3
        if args.agent_command == "status":
            payload = store.status()
            _emit_agent(payload, as_json=args.as_json)
            runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
            return 0 if runtime.get("state") in {"READY", "BUSY", "NEEDS_ACTION", "DEGRADED"} else 3
        if args.agent_command == "probe":
            probe = GeminiCliAdapter(store).probe_installation()
            store.write_provider(probe)
            ensure_agent_runtime_ownership(store.root)
            payload = probe.to_dict()
            _emit_agent(payload, as_json=args.as_json)
            return 0 if probe.state == "INSTALLED" else 3
        if args.agent_command == "activate":
            probe = SecretAwareGeminiAdapter(store, credential_id=args.credential_id).activate_with_real_probe()
            ensure_agent_runtime_ownership(store.root)
            payload = probe.to_dict()
            _emit_agent(payload, as_json=args.as_json)
            return 0 if probe.state == "ACTIVE" else 3
        if args.agent_command == "restart":
            provider = store.provider() or {}
            if provider.get("state") != "ACTIVE":
                raise AgentNeedsAction("provider must be ACTIVE before starting/restarting agy-gen tmux")
            SecureTmuxController(store).restart_worker_session()
            ensure_agent_runtime_ownership(store.root)
            payload = {"agent_id": "agy-gen", "state": "RESTARTED", "tmux_state": "RUNNING"}
            _emit_agent(payload, as_json=args.as_json)
            return 0
        if args.agent_command == "task":
            task_id = store.queue_task(args.prompt)
            ensure_agent_runtime_ownership(store.root)
            payload = {"agent_id": "agy-gen", "task_id": task_id, "state": "QUEUED"}
            _emit_agent(payload, as_json=args.as_json)
            return 0
    except (AgentRuntimeError, AgentToolError, PermissionError, OSError) as exc:
        ensure_agent_runtime_ownership(store.root)
        payload = {
            "agent_id": "agy-gen",
            "state": "NEEDS_ACTION" if isinstance(exc, AgentNeedsAction) else "FAILED",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        _emit_agent(redact(payload), as_json=args.as_json)
        return 3 if isinstance(exc, AgentNeedsAction) else 4
    raise SystemExit(2)


def _not_implemented(command: str) -> int:
    payload = {
        "command": command,
        "state": "NOT_IMPLEMENTED_IN_CURRENT_PACKAGE",
        "message": "This lifecycle command is reserved for a later approved MVP package.",
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2


def _emit_agent(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2))
        return
    state = payload.get("state")
    if state is None and isinstance(payload.get("runtime"), dict):
        state = payload["runtime"].get("state")
    print("agent: agy-gen")
    print(f"state: {state or 'UNKNOWN'}")
    if payload.get("reason"):
        print(f"reason: {payload['reason']}")
    if payload.get("evidence"):
        print(f"evidence: {payload['evidence']}")


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2))
        return
    if "observations" in payload:
        print(f"support: {payload['support_class']}")
        print(f"reason: {payload['support_reason']}")
        for item in payload["observations"]:
            print(f"- {item['check_id']}: {item['state']}")
        return
    if payload.get("command") == "install" and payload.get("decision"):
        decision = payload["decision"]
        print(f"state: {decision['state']}")
        print(f"mode: {decision['mode'] or 'UNSELECTED'}")
        print(f"profile: {decision['profile_id'] or 'NONE'}")
        print(f"reason: {decision['reason']}")
        if payload.get("run"):
            print(f"run: {payload['run']['state']}")
        return
    print(f"state: {payload.get('state', 'UNKNOWN')}")


if __name__ == "__main__":
    raise SystemExit(main())
