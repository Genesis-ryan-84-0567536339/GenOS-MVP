from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import json
import os
import signal
import threading
import uuid

from .mcp_hub import (
    MCP_PROTOCOL_VERSION,
    GenOSMcpHub,
    McpForbidden,
    McpHubError,
    McpUnauthorized,
    McpUnknownTool,
    McpUpstreamError,
)


MAX_MCP_BODY = 1024 * 1024
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"


class McpHeaderMismatch(McpHubError):
    pass


class McpUnsupportedProtocol(McpHubError):
    pass


class McpHttpHandler(BaseHTTPRequestHandler):
    server_version = "GenOSMCP/0.1"

    @property
    def hub(self) -> GenOSMcpHub:
        return getattr(self.server, "genos_mcp_hub")  # type: ignore[no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            status = self.hub.status()
            self._json(
                200,
                {
                    "status": "ok",
                    "role": "mcp-hub",
                    "protocol_version": MCP_PROTOCOL_VERSION,
                    "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
                    "local_tool_count": status["local_tool_count"],
                    "authority": status["authority"],
                },
            )
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        request_id: Any = None
        try:
            payload = self._read_json()
            request_id = payload.get("id")
            if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
                raise McpHubError("invalid JSON-RPC envelope")
            method = str(payload["method"])
            header_version = self.headers.get("MCP-Protocol-Version", "")
            header_method = self.headers.get("Mcp-Method", "")
            if header_version != MCP_PROTOCOL_VERSION:
                raise McpUnsupportedProtocol("unsupported MCP protocol version")
            if header_method != method:
                raise McpHeaderMismatch("Mcp-Method header/body mismatch")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise McpHubError("MCP params must be an object")
            meta = params.get("_meta")
            if not isinstance(meta, dict) or meta.get(PROTOCOL_VERSION_META_KEY) != MCP_PROTOCOL_VERSION:
                raise McpUnsupportedProtocol("MCP params._meta protocol version missing or unsupported")
            principal = self.hub.authenticate(self._bearer_token())
            correlation_id = self.headers.get("traceparent") or self.headers.get("X-Request-ID") or str(uuid.uuid4())

            if method == "server/discover":
                result = self.hub.discover(principal)
            elif method == "tools/list":
                result = self.hub.list_tools(principal)
            elif method == "tools/call":
                body_name = params.get("name")
                header_name = self.headers.get("Mcp-Name", "")
                if not isinstance(body_name, str) or not body_name or header_name != body_name:
                    raise McpHeaderMismatch("Mcp-Name header/body mismatch")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise McpHubError("tool arguments must be an object")
                result = self.hub.call_tool(
                    principal,
                    name=body_name,
                    arguments=arguments,
                    correlation_id=correlation_id,
                )
            else:
                self._rpc_error(request_id, -32601, "method_not_found", status=404)
                return
            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": _with_server_meta(result)})
        except McpUnsupportedProtocol:
            self._rpc_error(request_id, -32022, "unsupported_protocol_version", status=400)
        except McpHeaderMismatch:
            self._rpc_error(request_id, -32020, "header_mismatch", status=400)
        except McpUnauthorized:
            self._rpc_error(request_id, -32001, "unauthorized", status=401)
        except McpForbidden:
            self._rpc_error(request_id, -32003, "forbidden", status=403)
        except McpUnknownTool:
            self._rpc_error(request_id, -32602, "unknown_tool", status=404)
        except McpUpstreamError:
            self._rpc_error(request_id, -32053, "upstream_degraded", status=503)
        except (McpHubError, ValueError):
            self._rpc_error(request_id, -32600, "invalid_request", status=400)
        except Exception:
            self._rpc_error(request_id, -32603, "internal_error", status=500)

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log Authorization or request body/tool arguments.
        print(json.dumps({"event": "mcp_http", "message": fmt % args}, ensure_ascii=False), flush=True)

    def _bearer_token(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise McpUnauthorized("missing MCP bearer token")
        token = value[7:].strip()
        if not token or len(token) > 512:
            raise McpUnauthorized("invalid MCP bearer token")
        return token

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise McpHubError("Content-Length required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise McpHubError("invalid Content-Length") from exc
        if length < 0 or length > MAX_MCP_BODY:
            raise McpHubError("MCP request body too large")
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise McpHubError("application/json required")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpHubError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise McpHubError("JSON object required")
        return value

    def _rpc_error(self, request_id: Any, code: int, message: str, *, status: int) -> None:
        self._json(status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _with_server_meta(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    existing = result.get("_meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta[SERVER_INFO_META_KEY] = {"name": "genos-mcp-hub", "version": "0.1"}
    return {**result, "_meta": meta}


def serve_mcp(*, host: str = "127.0.0.1", port: int) -> int:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("MCP Hub core service must bind loopback; public exposure belongs to guided edge")
    hub = GenOSMcpHub.from_system()
    server = ThreadingHTTPServer((host, int(port)), McpHttpHandler)
    server.daemon_threads = True
    server.genos_mcp_hub = hub  # type: ignore[attr-defined]
    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    thread.start()
    print(
        json.dumps(
            {
                "event": "mcp_hub_start",
                "host": host,
                "port": int(port),
                "protocol_version": MCP_PROTOCOL_VERSION,
                "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        stop_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0
