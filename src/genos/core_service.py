from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from . import __version__


DRIVE_SCAN_INTERVAL_SECONDS = 30 * 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "GenOSCore/0.1"

    def do_GET(self) -> None:  # noqa: N802
        role = getattr(self.server, "genos_role", "unknown")
        if self.path == "/health":
            self._json(200, {"status": "ok", "role": role, "version": __version__, "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN", "ui_state": "NOT_IMPLEMENTED" if role == "mission-control" else None, "observed_at": _utc_now()})
            return
        if role == "mission-control" and self.path == "/":
            self._json(503, {"status": "not_ready", "role": role, "reason": "MISSION_CONTROL_UI_NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL"})
            return
        self._json(404, {"status": "not_found"})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write(json.dumps({"event": "http", "message": fmt % args}, ensure_ascii=False) + "\n"); sys.stdout.flush()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def serve_http(role: str, port: int) -> int:
    handler: type[BaseHTTPRequestHandler] = HealthHandler
    product_app = None
    if role == "product-api":
        from .product_api import ProductAPIApp, ProductAPIHandler
        product_app = ProductAPIApp.from_system(); handler = ProductAPIHandler
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True; server.genos_role = role  # type: ignore[attr-defined]
    if product_app is not None:
        server.genos_app = product_app  # type: ignore[attr-defined]
    stop_event = threading.Event()
    def _stop(_signum: int, _frame: object) -> None: stop_event.set()
    signal.signal(signal.SIGTERM, _stop); signal.signal(signal.SIGINT, _stop)
    print(json.dumps({"event": "service_start", "role": role, "port": port, "observed_at": _utc_now()}), flush=True)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, name=f"genos-{role}-http", daemon=True)
    server_thread.start()
    try: stop_event.wait()
    finally:
        server.shutdown(); server.server_close(); server_thread.join(timeout=5)
    return 0


def _reconcile_core_agent(state_dir: Path, instance_id: str, *, force_cli_update: bool = False) -> dict[str, object]:
    """One watchdog tick for resident `agy-gen` with live provider truth.

    Antigravity CLI (`agy`) is a replaceable tool binding, not Agent identity.
    When the managed toolchain exists, the worker checks the stable channel on
    boot and then through the updater's six-hour throttle. A busy work claim
    defers cutover. Provider installation is re-probed whenever it is not ACTIVE
    so a stale pre-provision `CLI_NOT_FOUND` projection cannot survive after the
    CLI becomes available.
    """
    from .agent_auth import AgentAuthBridge
    from .agent_runtime import AgentRuntimeError, AgentRuntimeStore, AntigravityCliAdapter, managed_cli_update
    from .agent_secure_runtime import SecretAwareAntigravityAdapter, SecureTmuxController

    store = AgentRuntimeStore(state_dir / "agents" / "agy-gen")
    store.ensure_seed(instance_id=instance_id)

    managed_root = state_dir / "tools" / "antigravity-cli"
    if managed_root.is_dir():
        try:
            managed_cli_update(store, force=force_cli_update)
        except (AgentRuntimeError, OSError):
            # Update failure is separately projected by the toolchain state and
            # must not erase the last-known-good provider/runtime projection.
            pass

    provider = store.provider()
    if provider is None or provider.get("state") != "ACTIVE":
        installed = AntigravityCliAdapter(store).probe_installation()
        store.write_provider(installed)
        provider = installed.to_dict()

    tmux = SecureTmuxController(store)
    claim = store.status().get("claim")
    auth_reason: str | None = None

    if provider.get("state") == "INSTALLED":
        try:
            auth = AgentAuthBridge(store)
            projection = auth.status()
            if projection.get("state") == "IDLE":
                projection = auth.start()
            auth_state = str(projection.get("state") or "UNKNOWN")
            auth_reason = f"AUTH_{auth_state}"
            if auth_state == "AUTHENTICATED":
                verified = SecretAwareAntigravityAdapter(store).activate_with_real_probe()
                provider = verified.to_dict()
                auth_reason = str(provider.get("evidence") or "AUTH_MODEL_VERIFY_REQUIRED")
        except AgentRuntimeError:
            auth_reason = "AUTH_TERMINAL_START_FAILED"

    if provider.get("state") != "ACTIVE":
        store.write_runtime(
            state="NEEDS_ACTION",
            reason=auth_reason or str(provider.get("evidence") or "PROVIDER_NOT_ACTIVE"),
            tmux_state="RUNNING" if tmux.has_session() else "STOPPED",
            task_id=str(claim.get("task_id")) if isinstance(claim, dict) and claim.get("task_id") else None,
        )
    else:
        try:
            tmux.ensure_worker_session()
            store.write_runtime(
                state="BUSY" if claim else "READY",
                reason="WORK_CLAIM_ACTIVE" if claim else "PROVIDER_AND_TMUX_ACTIVE",
                tmux_state="RUNNING",
                task_id=str(claim.get("task_id")) if isinstance(claim, dict) and claim.get("task_id") else None,
            )
        except AgentRuntimeError:
            store.write_runtime(
                state="DEGRADED", reason="TMUX_WORKER_START_FAILED", tmux_state="STOPPED",
                task_id=str(claim.get("task_id")) if isinstance(claim, dict) and claim.get("task_id") else None,
            )
    status = store.status(); runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    return {"agent_id": "agy-gen", "state": runtime.get("state", "UNKNOWN"), "reason": runtime.get("reason", "UNKNOWN"), "tmux_state": runtime.get("tmux_state", "UNKNOWN")}


def _scheduled_drive_scan() -> dict[str, object]:
    """One isolated 30-minute scan; failure is queued for the next cadence."""
    try:
        from .drive_system import build_drive_system
        result = build_drive_system().scheduled_scan()
        return {
            "state": str(result.get("state") or "UNKNOWN"),
            "remote_write": bool(result.get("remote_write", False)),
            "observed_at": _utc_now(),
        }
    except Exception as exc:
        return {
            "state": "RETRY_SCHEDULED",
            "reason": f"DRIVE_SCAN_{type(exc).__name__}",
            "remote_write": False,
            "retry_after_seconds": DRIVE_SCAN_INTERVAL_SECONDS,
            "observed_at": _utc_now(),
        }


def run_worker(state_dir: Path, interval_seconds: float) -> int:
    heartbeat = state_dir / "worker" / "heartbeat.json"; heartbeat.parent.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event(); first_tick = True
    next_drive_scan = time.monotonic() + DRIVE_SCAN_INTERVAL_SECONDS
    drive_projection: dict[str, object] = {"state": "SCHEDULED", "remote_write": False, "observed_at": _utc_now()}
    try:
        from .kanban import build_kanban_system
        kanban_system = build_kanban_system()
        kanban_projection: dict[str, object] = {"state": "IDLE", "reason": "NOT_TICKED", "observed_at": _utc_now()}
    except Exception as exc:
        kanban_system = None
        kanban_projection = {"state": "DEGRADED", "reason": f"KANBAN_INIT_{type(exc).__name__}", "observed_at": _utc_now()}
    def _stop(_signum: int, _frame: object) -> None: stop_event.set()
    signal.signal(signal.SIGTERM, _stop); signal.signal(signal.SIGINT, _stop)
    while not stop_event.is_set():
        instance_id = os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN"
        try:
            agent = _reconcile_core_agent(state_dir, instance_id, force_cli_update=first_tick)
        except Exception as exc:
            agent = {"agent_id": "agy-gen", "state": "DEGRADED", "reason": f"RECONCILE_{type(exc).__name__}", "tmux_state": "UNKNOWN"}
        first_tick = False
        now = time.monotonic()
        if now >= next_drive_scan:
            drive_projection = _scheduled_drive_scan()
            next_drive_scan = now + DRIVE_SCAN_INTERVAL_SECONDS
        if kanban_system is not None:
            try:
                tick = kanban_system.agent_tick()
                kanban_projection = {**tick, "observed_at": _utc_now()}
            except Exception as exc:
                kanban_projection = {"state": "DEGRADED", "reason": f"KANBAN_TICK_{type(exc).__name__}", "observed_at": _utc_now()}
        payload = {"status": "ok", "role": "worker", "version": __version__, "instance_id": instance_id, "core_agent": agent, "drive_report": drive_projection, "kanban_agent": kanban_projection, "observed_at": _utc_now()}
        temp = heartbeat.with_suffix(".tmp"); temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"); os.replace(temp, heartbeat)
        stop_event.wait(interval_seconds)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m genos.core_service")
    parser.add_argument("role", choices=("product-api", "runtime", "worker", "mcp", "mission-control"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--state-dir", default=os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
    parser.add_argument("--worker-interval", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.role == "worker": return run_worker(Path(args.state_dir), args.worker_interval)
    if args.role == "mcp":
        from .mcp_transport import serve_mcp
        port = args.port or int(os.environ.get("GENOS_MCP_PORT", "0") or "0")
        if not port: raise SystemExit("GENOS_MCP_PORT or --port is required for MCP role")
        return serve_mcp(port=port)
    if args.port is None: raise SystemExit("--port is required for HTTP roles")
    return serve_http(args.role, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
