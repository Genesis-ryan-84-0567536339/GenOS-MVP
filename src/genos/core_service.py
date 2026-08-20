from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import time

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
        # Keep systemd logs compact and structured without request headers/secrets.
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
    server = ThreadingHTTPServer(("127.0.0.1", port), HealthHandler)
    server.genos_role = role  # type: ignore[attr-defined]
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(json.dumps({"event": "service_start", "role": role, "port": port, "observed_at": _utc_now()}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0 if stop else 0


def run_worker(state_dir: Path, interval_seconds: float) -> int:
    heartbeat = state_dir / "worker" / "heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        payload = {
            "status": "ok",
            "role": "worker",
            "version": __version__,
            "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
            "observed_at": _utc_now(),
        }
        temp = heartbeat.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, heartbeat)
        time.sleep(interval_seconds)
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
