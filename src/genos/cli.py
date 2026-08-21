from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import __version__
from .agent_auth import AgentAuthBridge, AgentAuthError
from .agent_permissions import ensure_agent_runtime_ownership
from .agent_runtime import AgentNeedsAction, AgentRuntimeError, AgentRuntimeStore, GeminiCliAdapter
from .agent_secure_runtime import SecretAwareGeminiAdapter, SecureTmuxController
from .agent_tool_links import ensure_system_links
from .agent_tools import AgentToolError, AgentToolProvisioner
from .auth_service import CredentialError
from .drive_bridge import DriveBridgeError, DriveNeedsAction, DriveRemoteError
from .drive_store import DriveStoreError
from .drive_system import DriveSystemError, build_drive_system
from .install import InstallError, NativeProvisioner, ReleaseArtifact, build_native_install
from .observability import GENOS_SERVICES, ObservabilityService
from .product_store import ProductStoreError
from .recon import collect_all
from .redaction import redact
from .repair import RepairError, RepairService
from .report_bridge import ReportBridgeError
from .secret_provider import SecretProviderError
from .state import JsonStateStore


_RESERVED_MUTATION_COMMANDS = {
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

    recon = sub.add_parser("recon", help="Run typed read-only host observations")
    recon.add_argument("--json", action="store_true", dest="as_json")
    recon.add_argument("--cwd", default=None, help="Optional repository path for read-only Git facts")

    doctor = sub.add_parser("doctor", help="Run the shared read-only GenOS observability model")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--cwd", default=None, help="Optional repository path for read-only Git facts")

    repair = sub.add_parser("repair", help="Run one observation-backed typed repair action")
    repair.add_argument("--action", required=True, choices=(RepairService.ACTION_RESTART_SERVICE,))
    repair.add_argument("--target", required=True, choices=tuple(GENOS_SERVICES))
    repair.add_argument("--plan-only", action="store_true")
    repair.add_argument("--json", action="store_true", dest="as_json")

    install = sub.add_parser("install", help="Plan or execute the approved fresh-install provisioner")
    install.add_argument("--mode", choices=("native", "vm"), default=None)
    install.add_argument("--release", type=Path, required=True, help="Verified release tar archive supplied by the bootstrap")
    install.add_argument("--release-sha256", required=True)
    install.add_argument("--git-sha", required=True)
    install.add_argument("--plan-only", action="store_true")
    install.add_argument("--json", action="store_true", dest="as_json")
    install.add_argument("--candidate-e2e", action="store_true", help=argparse.SUPPRESS)

    drive = sub.add_parser("drive", help="Operate the typed Google Drive collaboration bridge")
    drive_sub = drive.add_subparsers(dest="drive_command", required=True)
    drive_status = drive_sub.add_parser("status", help="Show local Drive binding state without remote mutation")
    drive_status.add_argument("--json", action="store_true", dest="as_json")
    drive_connect = drive_sub.add_parser("connect", help="Bind a drive-sync SecretRef and verify the remote collaboration root")
    drive_connect.add_argument("--secret-id", required=True, help="SecretRef UUID granted to consumer drive-sync")
    drive_connect.add_argument("--root-name", default="GenOS")
    drive_connect.add_argument("--json", action="store_true", dest="as_json")
    drive_verify = drive_sub.add_parser("verify", help="Re-verify the current Drive account and protocol binding")
    drive_verify.add_argument("--json", action="store_true", dest="as_json")

    report = sub.add_parser("report", help="Build/publish reports from the shared observability authority")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    system_report = report_sub.add_parser("system", help="Publish the sanitized System Report to the bound Drive replica")
    system_report.add_argument("--scheduled", action="store_true", help="Skip remote write when significant state is unchanged")
    system_report.add_argument("--json", action="store_true", dest="as_json")

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

    auth = agent_sub.add_parser("auth", help="Run Antigravity interactive auth in the persistent agy-gen tmux session")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    for name in ("start", "status", "verify"):
        item = auth_sub.add_parser(name)
        item.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
        item.add_argument("--json", action="store_true", dest="as_json")
        if name == "start":
            item.add_argument("--restart", action="store_true", help="Restart only the agy-gen auth window")
    submit = auth_sub.add_parser("submit", help="Read one authorization code from stdin and send it to tmux")
    submit.add_argument("--state-dir", default="/var/lib/genos/agents/agy-gen")
    submit.add_argument("--json", action="store_true", dest="as_json")

    for name in sorted(_RESERVED_MUTATION_COMMANDS):
        sub.add_parser(name, help="Lifecycle surface reserved for later MVP packages")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return _status(as_json=args.as_json, state_dir=args.state_dir)
    if args.command == "recon":
        return _recon(as_json=args.as_json, cwd=args.cwd)
    if args.command == "doctor":
        return _doctor(as_json=args.as_json, cwd=args.cwd)
    if args.command == "repair":
        return _repair(args)
    if args.command == "install":
        return _install(args)
    if args.command == "drive":
        return _drive(args)
    if args.command == "report":
        return _report(args)
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


def _recon(as_json: bool, cwd: str | None) -> int:
    observations, support_class, reason = collect_all(cwd=cwd)
    payload = {
        "mode": "recon",
        "read_only": True,
        "support_class": support_class.value,
        "support_reason": reason,
        "observations": [item.to_dict() for item in observations],
    }
    _emit(payload, as_json=as_json)
    return 0


def _doctor(as_json: bool, cwd: str | None) -> int:
    payload = ObservabilityService().snapshot(cwd=cwd)
    _emit(payload, as_json=as_json)
    return 0


def _repair(args: argparse.Namespace) -> int:
    service = RepairService()
    try:
        plan = service.plan(action=args.action, target=args.target)
        payload: dict[str, Any] = {
            "command": "repair",
            "plan_only": bool(args.plan_only),
            "state": "PLANNED" if plan.mutation_allowed else "NO_ACTION",
            "plan": plan.to_dict(),
        }
        if not args.plan_only and plan.mutation_allowed:
            result = service.execute(plan)
            payload["result"] = result
            payload["state"] = str(result.get("state") or "UNKNOWN")
        _emit(payload, as_json=args.as_json)
        if not args.plan_only and not plan.mutation_allowed and plan.reason == "INSUFFICIENT_LIVE_EVIDENCE":
            return 3
        return 0 if payload["state"] not in {"FAILED", "FAILED_VERIFY"} else 4
    except RepairError as exc:
        payload = {
            "command": "repair",
            "state": "FAILED_PRECONDITION",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        _emit(redact(payload), as_json=args.as_json)
        return 4


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


def _drive(args: argparse.Namespace) -> int:
    try:
        services = build_drive_system()
        if args.drive_command == "status":
            payload = services.connection.status()
            _emit_safe(payload, as_json=args.as_json)
            return 0 if payload.get("state") == "READY" else 3
        if args.drive_command == "connect":
            payload = services.connection.connect(secret_id=args.secret_id, root_name=args.root_name)
            _emit_safe(payload, as_json=args.as_json)
            return 0 if payload.get("state") == "READY" else 3
        if args.drive_command == "verify":
            payload = services.connection.verify()
            _emit_safe(payload, as_json=args.as_json)
            return 0 if payload.get("state") == "READY" else 3
    except DriveNeedsAction:
        _emit_safe({"state": "NEEDS_ACTION", "error_type": "DriveNeedsAction"}, as_json=args.as_json)
        return 3
    except (DriveBridgeError, DriveStoreError, DriveSystemError, CredentialError, ProductStoreError, SecretProviderError, OSError):
        _emit_safe({"state": "FAILED", "error_type": "DRIVE_BACKEND_UNAVAILABLE"}, as_json=args.as_json)
        return 4
    raise SystemExit(2)


def _report(args: argparse.Namespace) -> int:
    if args.report_command != "system":
        raise SystemExit(2)
    try:
        result = build_drive_system().reports.publish(manual=not bool(args.scheduled))
        _emit_safe(result, as_json=args.as_json)
        return 0 if result.get("state") in {"PUBLISHED", "NO_CHANGE"} else 3
    except DriveNeedsAction:
        _emit_safe({"state": "NEEDS_ACTION", "error_type": "DriveNeedsAction"}, as_json=args.as_json)
        return 3
    except (ReportBridgeError, DriveRemoteError, DriveStoreError, DriveSystemError, CredentialError, ProductStoreError, SecretProviderError, OSError):
        _emit_safe({"state": "FAILED", "error_type": "REPORT_BACKEND_UNAVAILABLE"}, as_json=args.as_json)
        return 4


def _agent(args: argparse.Namespace) -> int:
    store = AgentRuntimeStore(args.state_dir)
    try:
        if args.agent_command == "auth":
            return _agent_auth(args, store)
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
                raise AgentNeedsAction("provider must be ACTIVE before starting/restarting agy-gen runtime")
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
    except (AgentRuntimeError, AgentToolError, AgentAuthError, PermissionError, OSError) as exc:
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


def _agent_auth(args: argparse.Namespace, store: AgentRuntimeStore) -> int:
    bridge = AgentAuthBridge(store)
    if args.auth_command == "start":
        payload = bridge.start(restart=bool(args.restart))
        ensure_agent_runtime_ownership(store.root)
        _emit_agent(payload, as_json=args.as_json)
        return 0 if payload.get("state") in {"WAITING_BROWSER", "WAITING_CODE", "AUTHENTICATED", "STARTING"} else 3
    if args.auth_command == "status":
        payload = bridge.status()
        _emit_agent(payload, as_json=args.as_json)
        return 0 if payload.get("state") != "IDLE" else 3
    if args.auth_command == "submit":
        # Never accept auth codes on argv: process listings must not expose them.
        code = sys.stdin.readline()
        payload = bridge.submit_code(code)
        ensure_agent_runtime_ownership(store.root)
        _emit_agent(payload, as_json=args.as_json)
        return 0
    if args.auth_command == "verify":
        probe = SecretAwareGeminiAdapter(store).activate_with_real_probe()
        ensure_agent_runtime_ownership(store.root)
        payload = probe.to_dict()
        _emit_agent(payload, as_json=args.as_json)
        return 0 if probe.state == "ACTIVE" else 3
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
    if payload.get("auth_url"):
        print(f"auth_url: {payload['auth_url']}")
    if payload.get("evidence"):
        print(f"evidence: {payload['evidence']}")


def _emit_safe(payload: dict[str, Any], *, as_json: bool) -> None:
    """Emit typed Drive/report projections whose schemas reject raw secrets."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(f"state: {payload.get('state', 'UNKNOWN')}")
    if payload.get("job") and isinstance(payload["job"], dict):
        print(f"job: {payload['job'].get('job_id', 'UNKNOWN')}")


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(redact(payload), ensure_ascii=False, sort_keys=True, indent=2))
        return
    if "observations" in payload:
        print(f"support: {payload['support_class']}")
        print(f"reason: {payload['support_reason']}")
        if payload.get("health"):
            print(f"health: {payload['health'].get('state', 'UNKNOWN')}")
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
