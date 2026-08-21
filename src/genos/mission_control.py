from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.request

from . import __version__


MAX_PROXY_BODY = 64 * 1024
PRODUCT_API_ORIGIN = "http://127.0.0.1:17880"
WEB_ROOT = Path(__file__).with_name("web")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_SPA_ROUTES = {"/dashboard", "/kanban", "/agy", "/memory", "/connections", "/reports"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MissionControlHandler(BaseHTTPRequestHandler):
    """Static Mission Control shell plus fixed loopback Product API proxy.

    The browser never receives a generic proxy destination. Only `/api/v1/*`
    requests are forwarded to the fixed local Product API, keeping Product API
    loopback-only while Mission Control remains a same-origin browser surface.
    Authorization/body values are deliberately omitted from HTTP logs.
    """

    server_version = "GenOSMissionControl/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "role": "mission-control",
                    "version": __version__,
                    "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
                    "ui_state": "READY",
                    "observed_at": _utc_now(),
                },
            )
            return
        if path.startswith("/api/v1/"):
            self._proxy("GET")
            return
        if path in _SPA_ROUTES:
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        asset = _STATIC.get(path)
        if asset is not None:
            self._serve_static(*asset)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/v1/"):
            self._proxy("POST")
            return
        self._json(405, {"error": "method_not_allowed"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Fixed route paths are safe to record; credentials, headers and bodies
        # are never logged by this service.
        print(json.dumps({"event": "mission_control_http", "message": fmt % args}, ensure_ascii=False), flush=True)

    def _proxy(self, method: str) -> None:
        raw_length = self.headers.get("Content-Length")
        length = 0
        if raw_length not in {None, ""}:
            try:
                length = int(raw_length)
            except ValueError:
                self._json(400, {"error": "invalid_content_length"})
                return
        if length < 0 or length > MAX_PROXY_BODY:
            self._json(413, {"error": "request_body_too_large"})
            return
        body = self.rfile.read(length) if length else None
        headers: dict[str, str] = {"Accept": "application/json"}
        authorization = self.headers.get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        content_type = self.headers.get("Content-Type")
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        target = PRODUCT_API_ORIGIN + self.path
        request = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed loopback origin only
                payload = response.read(MAX_PROXY_BODY + 1)
                if len(payload) > MAX_PROXY_BODY:
                    self._json(502, {"error": "backend_response_too_large"})
                    return
                self._raw(response.status, payload, response.headers.get("Content-Type") or "application/json; charset=utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_PROXY_BODY + 1)
            if len(payload) > MAX_PROXY_BODY:
                self._json(502, {"error": "backend_response_too_large"})
                return
            self._raw(exc.code, payload, exc.headers.get("Content-Type") or "application/json; charset=utf-8")
        except (urllib.error.URLError, TimeoutError, OSError):
            self._json(502, {"error": "product_api_unavailable"})

    def _serve_static(self, filename: str, content_type: str) -> None:
        # filename always comes from a fixed map; no arbitrary filesystem path.
        path = WEB_ROOT / filename
        try:
            body = path.read_bytes()
        except OSError:
            self._json(503, {"error": "ui_asset_unavailable"})
            return
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if filename == "index.html" else "public, max-age=300")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._raw(status, body, "application/json; charset=utf-8")

    def _raw(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )


def serve_mission_control(*, port: int) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), MissionControlHandler)
    server.daemon_threads = True
    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(json.dumps({"event": "mission_control_start", "port": port, "observed_at": _utc_now()}), flush=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    thread.start()
    try:
        stop_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0
