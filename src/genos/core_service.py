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

from . import __version__


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "GenOSCore/0.1"

    def do_GET(self) -> None:  # noqa: N802
        role = getattr(self.server, "genos_role", "unknown")
        if self.path == "/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "role": role,
                    "version": __version__,
                    "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
                    "ui_state": "NOT_IMPLEMENTED" if role == "mission-control" else None,
                    "observed_at": _utc_now(),
                },
            )
            return
        if role == "mission-control" and self.path == "/":
            self._json(
                503,
                {
                    "status": "not_ready",
                    "role": role,
                    "reason": "MISSION_CONTROL_UI_NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL",
                },
            )
            return
        self._json(404, {"status": "not_found"})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write(json.dumps({"event": "http", "message": fmt % args}, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_http(role: str, port: int) -> int:
    handler: type[BaseHTTPRequestHandler] = HealthHandler
    product_app = None
    if role == "product-api":
        # Import only for the Product API role so runtime/mission-control health
        # processes have no credential/database authority attached to them.
        from .product_api import ProductAPIApp, ProductAPIHandler

        product_app = ProductAPIApp.from_system()
        handler = ProductAPIHandler

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.genos_role = role  # type: ignore[attr-defined]
    if product_app is not None:
        server.genos_app = product_app  # type: ignore[attr-defined]
    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(json.dumps({"event": "service_start", "role": role, "port": port, "observed_at": _utc_now()}), flush=True)

    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name=f"genos-{role}-http",
        daemon=True,
    )
    server_thread.start()
    try:
        stop_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    return 0


def _reconcile_core_agent(state_dir: Path, instance_id: str) -> dict[str, object]:
    """One watchdog reconciliation tick for the single MVP Core Agent.

    This intentionally does not perform a provider auth/model request. External
    auth activation is an explicit Owner action; the background worker only
    preserves identity, observes installed/provider state, and restores tmux
    after provider activation has already been verified.
    """
    from .agent_runtime import AgentRuntimeError, AgentRuntimeStore, GeminiCliAdapter, TmuxController

    store = AgentRuntimeStore(state_dir / "agents" / "agy-gen")
    store.ensure_seed(instance_id=instance_id)
    provider = store.provider()
    if provider is None:
        installed = GeminiCliAdapter(store).probe_installation()
        store.write_provider(installed)
        provider = installed.to_dict()

    tmux = TmuxController(store)
    claim = store.status().get("claim")
    if provider.get("state") != "ACTIVE":
        store.write_runtime(
            state="NEEDS_ACTION",
            reason=str(provider.get("evidence") or "PROVIDER_NOT_ACTIVE"),
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
                state="DEGRADED",
                reason="TMUX_WORKER_START_FAILED",
                tmux_state="STOPPED",
                task_id=str(claim.get("task_id")) if isinstance(claim, dict) and claim.get("task_id") else None,
            )
    status = store.status()
    runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    return {
        "agent_id": "agy-gen",
        "state": runtime.get("state", "UNKNOWN"),
        "reason": runtime.get("reason", "UNKNOWN"),
        "tmux_state": runtime.get("tmux_state", "UNKNOWN"),
    }


def run_worker(state_dir: Path, interval_seconds: float) -> int:
    heartbeat = state_dir / "worker" / "heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop_event.is_set():
        instance_id = os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN"
        try:
            agent = _reconcile_core_agent(state_dir, instance_id)
        except Exception as exc:  # watchdog must not take down the core worker
            agent = {
                "agent_id": "agy-gen",
                "state": "DEGRADED",
                "reason": f"RECONCILE_{type(exc).__name__}",
                "tmux_state": "UNKNOWN",
            }
        payload = {
            "status": "ok",
            "role": "worker",
            "version": __version__,
            "instance_id": instance_id,
            "core_agent": agent,
            "observed_at": _utc_now(),
        }
        temp = heartbeat.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, heartbeat)
        stop_event.wait(interval_seconds)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m genos.core_service")
    parser.add_argument("role", choices=("product-api", "runtime", "worker", "mission-control"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--state-dir", default=os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
    parser.add_argument("--worker-interval", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.role == "worker":
        return run_worker(Path(args.state_dir), args.worker_interval)
    if args.port is None:
        raise SystemExit("--port is required for HTTP roles")
    return serve_http(args.role, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
