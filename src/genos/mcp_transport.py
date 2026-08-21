from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import base64
import binascii
import hashlib
import json
import os
import re
import signal
import threading
import uuid
from urllib.parse import urlsplit

from .mcp_hub import (
    MCP_PROTOCOL_VERSION,
    GenOSMcpHub,
    McpForbidden,
    McpHeaderMismatch,
    McpHubError,
    McpRateLimited,
    McpUnauthorized,
    McpUnknownTool,
    McpUpstreamError,
    mcp_value_matches_definition,
)


MAX_MCP_BODY = 1024 * 1024
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
GENOS_UNAUTHORIZED = -31999
GENOS_FORBIDDEN = -31998
GENOS_UPSTREAM_DEGRADED = -31997
GENOS_RATE_LIMITED = -31996


class McpInvalidRequest(McpHubError):
    pass


class McpUnsupportedProtocol(McpHubError):
    def __init__(self, requested: str) -> None:
        super().__init__("unsupported MCP protocol version")
        self.requested = requested


class McpInvalidParams(McpHubError):
    pass


class McpParseError(McpHubError):
    pass


class McpHttpHandler(BaseHTTPRequestHandler):
    server_version = "GenOSMCP/0.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    @property
    def hub(self) -> GenOSMcpHub:
        return getattr(self.server, "genos_mcp_hub")  # type: ignore[no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json(403, {"error": "forbidden_origin"})
            return
        if self.path == "/mcp":
            self._json(405, {"error": "method_not_allowed"})
            return
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
        if not self._origin_allowed():
            self._rpc_error(None, GENOS_FORBIDDEN, "forbidden_origin", status=403)
            return
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        request_id: Any = None
        try:
            payload = self._read_json()
            if "id" not in payload or not _valid_request_id(payload.get("id")):
                raise McpInvalidRequest("MCP request id must be a string or number")
            request_id = payload["id"]
            if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
                raise McpInvalidRequest("invalid JSON-RPC envelope")
            method = str(payload["method"])
            header_version = self._singleton_header("MCP-Protocol-Version")
            header_method = self._singleton_header("Mcp-Method")
            if header_method != method:
                raise McpHeaderMismatch("Mcp-Method header/body mismatch")
            params = payload.get("params")
            if not isinstance(params, dict):
                raise McpInvalidParams("MCP params must be an object")
            meta = params.get("_meta")
            requested_version, _ = _validate_request_meta(meta)
            if header_version != requested_version:
                raise McpHeaderMismatch("MCP-Protocol-Version header/body mismatch")
            mcp_name_source = {
                "tools/call": "name",
                "prompts/get": "name",
                "resources/read": "uri",
            }.get(method)
            if mcp_name_source is not None:
                body_mcp_name = params.get(mcp_name_source)
                max_name_bytes = 64 * 1024 if mcp_name_source == "uri" else 160
                if (
                    not isinstance(body_mcp_name, str)
                    or not body_mcp_name
                    or "\x00" in body_mcp_name
                    or len(body_mcp_name.encode("utf-8")) > max_name_bytes
                ):
                    raise McpInvalidParams(f"{mcp_name_source} is invalid")
                if _decode_mcp_header_value(self._singleton_header("Mcp-Name")) != body_mcp_name:
                    raise McpHeaderMismatch("Mcp-Name header/body mismatch")
            if requested_version != MCP_PROTOCOL_VERSION:
                raise McpUnsupportedProtocol(requested_version)
            correlation_id = _correlation_id(self.headers.get("traceparent") or self.headers.get("X-Request-ID"))
            try:
                principal = self.hub.authenticate(self._bearer_token())
            except McpUnauthorized:
                self.hub.audit_unauthorized(method=method, correlation_id=correlation_id)
                raise

            if method == "server/discover":
                result = self.hub.discover(principal, correlation_id=correlation_id)
            elif method == "tools/list":
                if "cursor" in params:
                    raise McpInvalidParams("GenOS tools/list does not issue or accept cursors")
                result = self.hub.list_tools(principal, correlation_id=correlation_id)
            elif method == "tools/call":
                body_name = params.get("name")
                assert isinstance(body_name, str)
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise McpInvalidParams("tool arguments must be an object")
                input_responses = params.get("inputResponses")
                if input_responses is not None and not mcp_value_matches_definition(
                    "InputResponses", input_responses
                ):
                    raise McpInvalidParams("inputResponses does not conform to MCP 2026-07-28")
                request_state = params.get("requestState")
                if request_state is not None and (
                    not isinstance(request_state, str) or len(request_state.encode("utf-8")) > 64 * 1024
                ):
                    raise McpInvalidParams("requestState must be a bounded string")
                result = self.hub.call_tool(
                    principal,
                    name=body_name,
                    arguments=arguments,
                    correlation_id=correlation_id,
                    input_responses=input_responses,
                    request_state=request_state,
                )
            else:
                self._rpc_error(request_id, -32601, "method_not_found", status=404)
                return
            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": _with_server_meta(result)})
        except McpUnsupportedProtocol as exc:
            self._rpc_error(
                request_id,
                -32022,
                "unsupported_protocol_version",
                status=400,
                data={"supported": [MCP_PROTOCOL_VERSION], "requested": exc.requested},
            )
        except McpHeaderMismatch:
            self._rpc_error(request_id, -32020, "header_mismatch", status=400)
        except McpParseError:
            self._rpc_error(None, -32700, "parse_error", status=400)
        except McpInvalidParams:
            self._rpc_error(request_id, -32602, "invalid_params", status=400)
        except McpInvalidRequest:
            self._rpc_error(request_id, -32600, "invalid_request", status=400)
        except McpUnauthorized:
            self._rpc_error(request_id, GENOS_UNAUTHORIZED, "unauthorized", status=401)
        except McpForbidden:
            self._rpc_error(request_id, GENOS_FORBIDDEN, "forbidden", status=403)
        except McpUnknownTool:
            self._rpc_error(request_id, -32602, "unknown_tool", status=400)
        except McpUpstreamError:
            self._rpc_error(request_id, GENOS_UPSTREAM_DEGRADED, "upstream_degraded", status=503)
        except McpRateLimited:
            self._rpc_error(request_id, GENOS_RATE_LIMITED, "rate_limited", status=429)
        except (McpHubError, ValueError):
            self._rpc_error(request_id, -32600, "invalid_request", status=400)
        except Exception:
            self._rpc_error(request_id, -32603, "internal_error", status=500)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json(403, {"error": "forbidden_origin"})
            return
        if self.path == "/mcp":
            self._json(405, {"error": "method_not_allowed"})
            return
        self._json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_PUT(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_TRACE(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._unsupported_method()

    def _unsupported_method(self) -> None:
        if not self._origin_allowed():
            self._json(403, {"error": "forbidden_origin"})
            return
        self._json(405, {"error": "method_not_allowed"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log Authorization or request body/tool arguments.
        print(json.dumps({"event": "mcp_http", "message": fmt % args}, ensure_ascii=False), flush=True)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if hasattr(self, "headers") and not self._origin_allowed():
            self._json(403, {"error": "forbidden_origin"})
            return
        super().send_error(code, message, explain)

    def _bearer_token(self) -> str:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            raise McpUnauthorized("missing or duplicate MCP bearer token")
        value = values[0]
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
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type().lower() != "application/json":
            raise McpHubError("application/json required")
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError) as exc:
            raise McpParseError("request body read failed") from exc
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise McpParseError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise McpInvalidRequest("JSON object required")
        return value

    def _rpc_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        *,
        status: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        payload: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
        if request_id is not None:
            payload["id"] = request_id
        self._json(status, payload)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _origin_allowed(self) -> bool:
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return True
        if len(origins) != 1:
            return False
        origin = origins[0]
        configured = {item.strip() for item in os.environ.get("GENOS_MCP_ALLOWED_ORIGINS", "").split(",") if item.strip()}
        if origin in configured:
            return True
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
            and (port is None or 1 <= port <= 65535)
        )

    def _singleton_header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        if len(values) != 1 or not values[0]:
            raise McpHeaderMismatch(f"required {name} header is missing or duplicated")
        return values[0]


def _with_server_meta(result: Any) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("resultType"), str):
        raise McpHubError("MCP result is missing resultType")
    existing = result.get("_meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta[SERVER_INFO_META_KEY] = {"name": "genos-mcp-hub", "version": "0.1"}
    return {**result, "_meta": meta}


def _validate_request_meta(value: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or not mcp_value_matches_definition("RequestMetaObject", value):
        raise McpInvalidParams("MCP params._meta does not conform to 2026-07-28")
    version = value.get(PROTOCOL_VERSION_META_KEY)
    capabilities = value.get(CLIENT_CAPABILITIES_META_KEY)
    assert isinstance(version, str)
    assert isinstance(capabilities, dict)
    return version, capabilities


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_mcp_header_value(value: str) -> str:
    if not value:
        raise McpHeaderMismatch("required MCP header is missing")
    if value.startswith("=?base64?") and value.endswith("?="):
        encoded = value[len("=?base64?") : -2]
        try:
            raw = base64.b64decode(encoded, validate=True)
            if base64.b64encode(raw).decode("ascii") != encoded:
                raise McpHeaderMismatch("non-canonical base64 MCP header value")
            return raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise McpHeaderMismatch("invalid base64 MCP header value") from exc
    if value != value.strip() or not all(0x20 <= ord(char) <= 0x7E for char in value):
        raise McpHeaderMismatch("invalid MCP header value")
    return value


def _correlation_id(value: str | None) -> str:
    if value is not None and value:
        candidate = value.strip()
        try:
            return str(uuid.UUID(candidate))
        except (ValueError, AttributeError):
            pass
        if _TRACEPARENT.fullmatch(candidate):
            _version, trace_id, span_id, _flags = candidate.split("-")
            if trace_id != "0" * 32 and span_id != "0" * 16:
                return candidate.lower()
        return "opaque-" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:32]
    return str(uuid.uuid4())


_TRACEPARENT = re.compile(r"^(?!ff)[0-9A-Fa-f]{2}-[0-9A-Fa-f]{32}-[0-9A-Fa-f]{16}-[0-9A-Fa-f]{2}$")


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
