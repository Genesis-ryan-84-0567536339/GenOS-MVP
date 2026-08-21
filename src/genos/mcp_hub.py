from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Callable
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import Draft202012Validator, RefResolver

from .agent_runtime import AgentRuntimeStore
from .auth_service import CredentialError, CredentialService
from .drive_system import DriveSystemServices, build_drive_system
from .kanban import KanbanSystem, build_kanban_system
from .mcp_store import PostgresMcpStore, scope_allows
from .observability import ObservabilityService
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider, SecretProviderError


MCP_PROTOCOL_VERSION = "2026-07-28"


class McpHubError(RuntimeError):
    pass


class McpUnauthorized(McpHubError):
    pass


class McpForbidden(McpHubError):
    pass


class McpUnknownTool(McpHubError):
    pass


class McpUpstreamError(McpHubError):
    pass


class McpHeaderMismatch(McpHubError):
    pass


class McpRateLimited(McpHubError):
    pass


class _RejectRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class _NoRemoteRefResolver(RefResolver):
    def resolve_remote(self, uri: str) -> Any:
        del uri
        raise ValueError("remote JSON Schema retrieval is disabled")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def public_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


class UpstreamMcpClient:
    """Bounded Streamable HTTP MCP client for registered upstream servers."""

    def __init__(self, *, timeout: float = 20.0, max_response_bytes: int = 4 * 1024 * 1024) -> None:
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        # Private MCP cache entries are isolated by a non-reversible
        # authorization-context fingerprint. Raw bearer material is never stored.
        self._schema_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    def list_tools(self, upstream: dict[str, Any], access_token: str | None) -> list[dict[str, Any]]:
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        cache_ttls: list[int] = []
        cache_scope: str | None = None
        for _page in range(16):
            params = {"cursor": cursor} if cursor is not None else {}
            response = self._call(upstream, access_token, method="tools/list", name=None, params=params)
            result = response.get("result") if isinstance(response, dict) else None
            page_tools = result.get("tools") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("resultType") != "complete"
                or not _valid_cache_hints(result)
                or not isinstance(page_tools, list)
            ):
                raise McpUpstreamError("upstream tools/list returned invalid shape")
            page_scope = str(result["cacheScope"])
            if cache_scope is None:
                cache_scope = page_scope
            elif cache_scope != page_scope:
                raise McpUpstreamError("upstream tools/list changed cacheScope across pages")
            tools.extend(page_tools)
            if len(tools) > 512:
                raise McpUpstreamError("upstream tools/list exceeded bounded tool count")
            cache_ttls.append(int(result["ttlMs"]))
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise McpUpstreamError("upstream tools/list returned an invalid cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise McpUpstreamError("upstream tools/list exceeded bounded page count")
        safe: list[dict[str, Any]] = []
        if cache_scope is None:
            raise McpUpstreamError("upstream tools/list returned no cache policy")
        cache_until = time.monotonic() + min(min(cache_ttls), 300_000) / 1000
        upstream_key = _upstream_cache_key(upstream)
        cache_context = "public" if cache_scope == "public" else _private_cache_context(access_token)
        schemas: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            tool_name = str(tool["name"])
            input_schema = tool.get("inputSchema")
            if (
                not tool_name
                or len(tool_name.encode("utf-8")) > 160
                or not isinstance(input_schema, dict)
                or input_schema.get("type") != "object"
            ):
                continue
            try:
                _validate_schema_document(input_schema)
                _mcp_header_annotations(input_schema)
            except (SchemaError, ValueError):
                print(
                    json.dumps(
                        {"event": "mcp_upstream_tool_rejected", "reason": "invalid_schema_or_x_mcp_header"},
                        sort_keys=True,
                    ),
                    flush=True,
                )
                continue
            if tool_name in schemas:
                raise McpUpstreamError("upstream tools/list returned duplicate tool names")
            schemas[tool_name] = input_schema
            safe.append(
                {
                    "name": tool_name,
                    "description": str(tool.get("description") or "")[:2048],
                    # The Hub is a distinct MCP server. It does not advertise the
                    # upstream routing annotations to its own clients; it applies
                    # them itself when acting as the upstream Streamable HTTP client.
                    "inputSchema": _strip_mcp_header_annotations(input_schema),
                }
            )
        with self._cache_lock:
            # A GenOS upstream has exactly one active SecretRef. Drop all older
            # authorization contexts on refresh so credential rotations cannot
            # accumulate stale private schemas in memory.
            stale_keys = [key for key in self._schema_cache if key[0] == upstream_key]
            for key in stale_keys:
                del self._schema_cache[key]
            for tool_name, input_schema in schemas.items():
                self._schema_cache[(upstream_key, cache_context, tool_name)] = (cache_until, input_schema)
        return safe

    def call_tool(
        self,
        upstream: dict[str, Any],
        access_token: str | None,
        *,
        name: str,
        arguments: dict[str, Any],
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        input_schema = self._cached_schema(upstream, name, access_token)
        if input_schema is None:
            self.list_tools(upstream, access_token)
            input_schema = self._cached_schema(upstream, name, access_token, allow_stale=True)
        if input_schema is None:
            raise McpUpstreamError("upstream MCP tool is unavailable or invalid")
        validation_error = _validate_tool_arguments(input_schema, arguments)
        if validation_error is not None:
            return _tool_error_result(validation_error)
        try:
            parameter_headers = _mcp_parameter_headers(input_schema, arguments)
        except McpUpstreamError:
            return _tool_error_result("Tool arguments cannot be safely mirrored to the upstream transport.")
        call_params: dict[str, Any] = {"name": name, "arguments": arguments}
        if input_responses is not None:
            return _tool_error_result("Federated MCP inputResponses are not enabled by Hub policy.")
        if request_state is not None:
            call_params["requestState"] = request_state
        if before_dispatch is not None:
            before_dispatch()
        response = self._call(
            upstream,
            access_token,
            method="tools/call",
            name=name,
            params=call_params,
            extra_headers=parameter_headers,
        )
        if "error" in response:
            raise McpUpstreamError("upstream MCP tool returned an error")
        result = response.get("result")
        if not _valid_call_tool_result(result):
            raise McpUpstreamError("upstream tools/call returned invalid result")
        return result

    def _cached_schema(
        self,
        upstream: dict[str, Any],
        name: str,
        access_token: str | None,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        upstream_key = _upstream_cache_key(upstream)
        private_context = _private_cache_context(access_token)
        with self._cache_lock:
            cached = self._schema_cache.get((upstream_key, private_context, name))
            if cached is None:
                cached = self._schema_cache.get((upstream_key, "public", name))
        if cached is None:
            return None
        expires_at, schema = cached
        if not allow_stale and expires_at <= time.monotonic():
            return None
        return schema

    def _call(
        self,
        upstream: dict[str, Any],
        access_token: str | None,
        *,
        method: str,
        name: str | None,
        params: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        request_params = dict(params)
        current_meta = request_params.get("_meta")
        meta = dict(current_meta) if isinstance(current_meta, dict) else {}
        meta.update(
            {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "genos-mcp-hub", "version": "0.1"},
                # Federation intentionally advertises no MRTR input-request
                # capabilities in MVP-07. This prevents an upstream from gaining
                # sampling/roots/elicitation authority through the Hub.
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        )
        request_params["_meta"] = meta
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": request_params,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = _encode_mcp_header_value(name)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if extra_headers:
            headers.update(extra_headers)
        req = urlrequest.Request(
            str(upstream["endpoint"]),
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            opener = urlrequest.build_opener(_RejectRedirectHandler())
            with opener.open(req, timeout=self.timeout) as response:  # noqa: S310 - endpoint validated at registration
                content_type = response.headers.get_content_type().lower()
                if content_type == "text/event-stream":
                    return _read_sse_response(
                        response,
                        request_id=request_id,
                        max_response_bytes=self.max_response_bytes,
                        deadline=time.monotonic() + self.timeout,
                    )
                raw = response.read(self.max_response_bytes + 1)
        except urlerror.HTTPError as exc:
            code = exc.code
            exc.close()
            raise McpUpstreamError(f"upstream MCP HTTP {code}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise McpUpstreamError("upstream MCP unavailable") from exc
        if len(raw) > self.max_response_bytes:
            raise McpUpstreamError("upstream MCP response exceeded limit")
        value = _decode_mcp_response(raw, content_type=content_type, request_id=request_id)
        return value


class GenOSMcpHub:
    def __init__(
        self,
        *,
        store: Any,
        local_tools: dict[str, ToolSpec],
        credentials: CredentialService | None = None,
        upstream_client: Any | None = None,
        tool_rate_limit_per_minute: int | None = None,
    ) -> None:
        self.store = store
        self.local_tools = local_tools
        self.credentials = credentials
        self.upstream_client = upstream_client or UpstreamMcpClient()
        configured_limit = tool_rate_limit_per_minute
        if configured_limit is None:
            raw_limit = os.environ.get("GENOS_MCP_TOOL_RATE_LIMIT_PER_MINUTE", "120")
            try:
                configured_limit = int(raw_limit)
            except ValueError:
                configured_limit = 120
        self.tool_rate_limit_per_minute = min(max(int(configured_limit), 1), 10_000)
        self._rate_windows: dict[str, deque[float]] = {}
        self._rate_lock = threading.Lock()
        self._continuations: dict[str, dict[str, Any]] = {}
        self._continuation_lock = threading.Lock()

    @classmethod
    def from_system(cls) -> "GenOSMcpHub":
        product_store = PostgresProductStore()
        product_store.ensure_schema()
        secret_root = os.environ.get("GENOS_SECRET_DIR", "/var/lib/genos/secrets")
        credentials = CredentialService(product_store, LocalFileSecretProvider(secret_root))
        mcp_store = PostgresMcpStore(product_store)
        mcp_store.ensure_schema()
        observability = ObservabilityService()
        agent_root = Path(os.environ.get("GENOS_AGY_GEN_DIR", "/var/lib/genos/agents/agy-gen"))
        kanban = build_kanban_system(product_store=product_store, credentials=credentials, agent_root=agent_root)
        drive = build_drive_system(product_store=product_store, credentials=credentials, observability=observability)
        agent = AgentRuntimeStore(agent_root)
        tools = build_local_tools(observability=observability, kanban=kanban, drive=drive, agent=agent)
        return cls(store=mcp_store, local_tools=tools, credentials=credentials)

    def discover(self, principal: dict[str, Any], *, correlation_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        result = {
            "resultType": "complete",
            "supportedVersions": [MCP_PROTOCOL_VERSION],
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": "GenOS unified MCP Hub. Tool discovery and invocation are filtered by the authenticated principal grant.",
            "ttlMs": 0,
            "cacheScope": "private",
        }
        self._audit(str(principal["principal_id"]), None, "server/discover", "ALLOW", "PASS", started, correlation_id)
        return result

    def list_tools(self, principal: dict[str, Any], *, correlation_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        principal_id = str(principal["principal_id"])
        scopes = list(principal.get("scopes") or [])
        tools = [spec.public_dict() for name, spec in sorted(self.local_tools.items()) if scope_allows(scopes, name)]
        for upstream in self.store.list_upstreams(active_only=True):
            namespace = str(upstream["namespace"])
            if not any(scope.startswith(namespace + ".") for scope in scopes):
                continue
            try:
                token = self._upstream_secret(upstream)
                remote_tools = self.upstream_client.list_tools(upstream, token)
                self._mark_upstream_nonblocking(str(upstream["upstream_id"]), state="HEALTHY")
            except (McpUpstreamError, CredentialError, SecretProviderError):
                upstream_id = str(upstream["upstream_id"])
                self._mark_upstream_nonblocking(upstream_id, state="DEGRADED")
                self._audit_nonblocking(
                    principal_id,
                    upstream_id,
                    f"{namespace}.*",
                    "ALLOW",
                    "UPSTREAM_DEGRADED",
                    started,
                    correlation_id,
                )
                continue
            for remote in remote_tools:
                full_name = f"{namespace}.{remote['name']}"
                if full_name in self.local_tools or not scope_allows(scopes, full_name):
                    continue
                tools.append({**remote, "name": full_name, "x-genos-upstream": namespace})
        tools.sort(key=lambda item: str(item.get("name") or ""))
        # Principal grants and upstream health can change immediately; do not
        # permit clients or HTTP intermediaries to reuse this projection.
        result = {"resultType": "complete", "tools": tools, "ttlMs": 0, "cacheScope": "private"}
        self._audit(principal_id, None, "tools/list", "ALLOW", "PASS", started, correlation_id)
        return result

    def call_tool(
        self,
        principal: dict[str, Any],
        *,
        name: str,
        arguments: dict[str, Any],
        correlation_id: str | None = None,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        principal_id = str(principal["principal_id"])
        if not self._consume_tool_rate_limit(principal_id, now=started):
            self._audit(principal_id, None, name, "DENY", "RATE_LIMITED", started, correlation_id)
            raise McpRateLimited("MCP tool invocation rate limit exceeded")
        scopes = list(principal.get("scopes") or [])
        if not scope_allows(scopes, name):
            self._audit(principal_id, None, name, "DENY", "FORBIDDEN", started, correlation_id)
            raise McpForbidden("MCP tool is not granted")
        local = self.local_tools.get(name)
        if local is not None:
            if input_responses is not None or request_state is not None:
                self._audit(principal_id, None, name, "ALLOW", "INVALID_ARGUMENTS", started, correlation_id)
                return _tool_error_result("This local tool does not accept a continuation response.")
            validation_error = _validate_tool_arguments(local.input_schema, arguments)
            if validation_error is not None:
                self._audit(principal_id, None, name, "ALLOW", "INVALID_ARGUMENTS", started, correlation_id)
                return _tool_error_result(validation_error)
            self._audit(principal_id, None, name, "ALLOW", "DISPATCH", started, correlation_id)
            try:
                result = local.handler(arguments)
                envelope = _tool_result(result)
                self._audit_nonblocking(principal_id, None, name, "ALLOW", "PASS", started, correlation_id)
                return envelope
            except Exception as exc:
                self._audit_nonblocking(
                    principal_id,
                    None,
                    name,
                    "ALLOW",
                    type(exc).__name__,
                    started,
                    correlation_id,
                )
                return _tool_error_result("The local GenOS tool failed to complete safely.")

        namespace, sep, remote_name = name.partition(".")
        if not sep or namespace == "genos" or not remote_name:
            self._audit(principal_id, None, name, "ALLOW", "UNKNOWN_TOOL", started, correlation_id)
            raise McpUnknownTool("unknown MCP tool")
        upstream = next((item for item in self.store.list_upstreams(active_only=True) if item.get("namespace") == namespace), None)
        if upstream is None:
            self._audit(principal_id, None, name, "ALLOW", "UNKNOWN_UPSTREAM", started, correlation_id)
            raise McpUnknownTool("unknown MCP upstream namespace")
        upstream_id = str(upstream["upstream_id"])
        if input_responses is not None:
            self._audit(principal_id, upstream_id, name, "ALLOW", "INVALID_ARGUMENTS", started, correlation_id)
            return _tool_error_result("Federated MCP inputResponses are not enabled by Hub policy.")
        upstream_request_state: str | None = None
        if request_state is not None:
            upstream_request_state = self._take_continuation(
                request_state,
                principal_id=principal_id,
                upstream_id=upstream_id,
                tool_name=name,
                arguments=arguments,
            )
            if upstream_request_state is None:
                self._audit(principal_id, upstream_id, name, "ALLOW", "INVALID_ARGUMENTS", started, correlation_id)
                return _tool_error_result("The federated continuation is invalid, expired, or belongs to another caller.")
        try:
            access_token = self._upstream_secret(upstream)
            dispatched = False

            def audit_dispatch() -> None:
                nonlocal dispatched
                if dispatched:
                    raise McpUpstreamError("upstream dispatch callback was invoked more than once")
                self._audit(principal_id, upstream_id, name, "ALLOW", "DISPATCH", started, correlation_id)
                dispatched = True

            call_options: dict[str, Any] = {"name": remote_name, "arguments": arguments}
            if upstream_request_state is not None:
                call_options["request_state"] = upstream_request_state
            call_options["before_dispatch"] = audit_dispatch
            result = self.upstream_client.call_tool(upstream, access_token, **call_options)
            self._mark_upstream_nonblocking(upstream_id, state="HEALTHY")
            if result.get("resultType") == "input_required":
                raw_state = result.get("requestState")
                if not isinstance(raw_state, str):
                    raise McpUpstreamError("upstream input_required result did not include safe continuation state")
                result = {
                    **result,
                    "requestState": self._store_continuation(
                        raw_state,
                        principal_id=principal_id,
                        upstream_id=upstream_id,
                        tool_name=name,
                        arguments=arguments,
                    ),
                }
            result_class = "INPUT_REQUIRED" if result.get("resultType") == "input_required" else (
                "TOOL_ERROR" if result.get("isError") is True else "PASS"
            )
            self._audit_nonblocking(principal_id, upstream_id, name, "ALLOW", result_class, started, correlation_id)
            return result
        except (McpUpstreamError, CredentialError, SecretProviderError) as exc:
            self._mark_upstream_nonblocking(upstream_id, state="DEGRADED")
            self._audit_nonblocking(
                principal_id,
                upstream_id,
                name,
                "ALLOW",
                "UPSTREAM_DEGRADED",
                started,
                correlation_id,
            )
            raise McpUpstreamError("registered upstream MCP is degraded") from exc

    def authenticate(self, access_token: str) -> dict[str, Any]:
        principal = self.store.authenticate(access_token)
        if principal is None:
            raise McpUnauthorized("invalid or revoked MCP access token")
        return principal

    def audit_unauthorized(self, *, method: str, correlation_id: str | None) -> None:
        try:
            self.store.audit(
                principal_id=None,
                upstream_id=None,
                tool_name=method,
                decision="DENY",
                result_class="UNAUTHORIZED",
                duration_ms=0,
                correlation_id=correlation_id,
            )
        except Exception:
            # Authentication remains fail-closed even when durable audit storage is unavailable.
            print(json.dumps({"event": "mcp_audit_degraded", "phase": "unauthorized"}, sort_keys=True), flush=True)

    def status(self) -> dict[str, Any]:
        return {
            "protocol_version": MCP_PROTOCOL_VERSION,
            "principal_count": len(self.store.list_principals()),
            "upstreams": self.store.list_upstreams(),
            "local_tool_count": len(self.local_tools),
            "authority": "genos-product-typed-capabilities",
        }

    def _upstream_secret(self, upstream: dict[str, Any]) -> str | None:
        secret_id = upstream.get("secret_id")
        if not secret_id:
            return None
        if self.credentials is None:
            raise CredentialError("MCP credential service unavailable")
        return self.credentials.get_secret_for_consumer(str(secret_id), consumer="mcp-hub")

    def _audit(
        self,
        principal_id: str,
        upstream_id: str | None,
        name: str,
        decision: str,
        result_class: str,
        started: float,
        correlation_id: str | None,
    ) -> None:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        self.store.audit(
            principal_id=principal_id,
            upstream_id=upstream_id,
            tool_name=name,
            decision=decision,
            result_class=result_class,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )

    def _audit_nonblocking(
        self,
        principal_id: str,
        upstream_id: str | None,
        name: str,
        decision: str,
        result_class: str,
        started: float,
        correlation_id: str | None,
    ) -> None:
        try:
            self._audit(principal_id, upstream_id, name, decision, result_class, started, correlation_id)
        except Exception:
            # A durable DISPATCH row was written before execution. Do not make a completed
            # side effect ambiguous to the caller merely because the terminal audit update failed.
            print(json.dumps({"event": "mcp_audit_degraded", "phase": "terminal"}, sort_keys=True), flush=True)

    def _mark_upstream_nonblocking(self, upstream_id: str, *, state: str) -> None:
        try:
            self.store.mark_upstream(upstream_id, state=state)
        except Exception:
            print(
                json.dumps({"event": "mcp_upstream_state_degraded", "state": state}, sort_keys=True),
                flush=True,
            )

    def _consume_tool_rate_limit(self, principal_id: str, *, now: float) -> bool:
        cutoff = now - 60.0
        with self._rate_lock:
            window = self._rate_windows.setdefault(principal_id, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.tool_rate_limit_per_minute:
                return False
            window.append(now)
            return True

    def _store_continuation(
        self,
        upstream_state: str,
        *,
        principal_id: str,
        upstream_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        now = time.monotonic()
        handle = "genos-mrtr:" + str(uuid.uuid4())
        record = {
            "principal_id": principal_id,
            "upstream_id": upstream_id,
            "tool_name": tool_name,
            "arguments_hash": _arguments_hash(arguments),
            "upstream_state": upstream_state,
            "expires_at": now + 300.0,
        }
        with self._continuation_lock:
            expired = [key for key, item in self._continuations.items() if item["expires_at"] <= now]
            for key in expired:
                del self._continuations[key]
            if len(self._continuations) >= 4096:
                oldest = min(self._continuations, key=lambda key: self._continuations[key]["expires_at"])
                del self._continuations[oldest]
            self._continuations[handle] = record
        return handle

    def _take_continuation(
        self,
        handle: str,
        *,
        principal_id: str,
        upstream_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        now = time.monotonic()
        arguments_hash = _arguments_hash(arguments)
        with self._continuation_lock:
            record = self._continuations.get(handle)
            if record is None:
                return None
            if record["expires_at"] <= now:
                del self._continuations[handle]
                return None
            if (
                record["principal_id"] != principal_id
                or record["upstream_id"] != upstream_id
                or record["tool_name"] != tool_name
                or record["arguments_hash"] != arguments_hash
            ):
                return None
            del self._continuations[handle]
        state = record.get("upstream_state")
        return state if isinstance(state, str) else None


def build_local_tools(
    *,
    observability: ObservabilityService,
    kanban: KanbanSystem,
    drive: DriveSystemServices,
    agent: AgentRuntimeStore,
) -> dict[str, ToolSpec]:
    object_schema = {"type": "object", "additionalProperties": False, "properties": {}}
    tools = [
        ToolSpec("genos.observability.get", "Read the authoritative GenOS observability snapshot.", object_schema, lambda _a: observability.snapshot()),
        ToolSpec("genos.cards.list", "List authoritative local Kanban Cards.", {"type": "object", "properties": {"status": {"type": "string"}}, "additionalProperties": False}, lambda a: {"cards": kanban.list_cards(status=_optional_str(a.get("status")))}),
        ToolSpec("genos.cards.get", "Read one authoritative local Kanban Card with events/artifacts.", {"type": "object", "required": ["card_id"], "properties": {"card_id": {"type": "string"}}, "additionalProperties": False}, lambda a: kanban.get_card(_required_str(a, "card_id"))),
        ToolSpec("genos.cards.create", "Create a local BACKLOG Card for agy-gen.", {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "additionalProperties": False}, lambda a: {"card": kanban.create_card(title=_required_str(a, "title"), description=_optional_str(a.get("description")) or "")}),
        ToolSpec("genos.cards.transition", "Run one typed Card lifecycle transition.", {"type": "object", "required": ["card_id", "to_state"], "properties": {"card_id": {"type": "string"}, "to_state": {"type": "string"}, "reason": {"type": "string"}}, "additionalProperties": False}, lambda a: kanban.transition(_required_str(a, "card_id"), to_state=_required_str(a, "to_state"), reason=_optional_str(a.get("reason")) or "MCP_ACTION")),
        ToolSpec("genos.cards.comment", "Add a comment to an authoritative local Card.", {"type": "object", "required": ["card_id", "text"], "properties": {"card_id": {"type": "string"}, "text": {"type": "string"}}, "additionalProperties": False}, lambda a: kanban.add_comment(_required_str(a, "card_id"), text=_required_str(a, "text"))),
        ToolSpec("genos.kanban.sync_drive", "Explicitly synchronize the configured Drive Inbox into local Cards.", object_schema, lambda _a: kanban.sync_drive_inbox()),
        ToolSpec("genos.agent.status", "Read resident agy-gen identity/provider/runtime status.", object_schema, lambda _a: agent.status()),
        ToolSpec("genos.agent.task", "Queue one typed task for the resident agy-gen Agent.", {"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}}, "additionalProperties": False}, lambda a: {"task_id": agent.queue_task(_required_str(a, "prompt")), "state": "QUEUED"}),
        ToolSpec("genos.drive.status", "Read the configured Google Drive integration state.", object_schema, lambda _a: drive.connection.status()),
        ToolSpec("genos.report.system", "Publish a manual sanitized System Report through the configured Drive bridge.", object_schema, lambda _a: drive.reports.publish(manual=True)),
    ]
    return {tool.name: tool for tool in tools}


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 64 * 1024:
        raise McpHubError(f"invalid {key}")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64 * 1024:
        raise McpHubError("invalid string argument")
    return value


def _tool_result(value: Any) -> dict[str, Any]:
    safe = value if isinstance(value, (dict, list, str, int, float, bool)) or value is None else str(value)
    text = json.dumps(safe, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise McpHubError("tool result exceeded MCP response limit")
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "structuredContent": safe,
        "isError": False,
    }


def _tool_error_result(message: str) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    try:
        validator = _validate_schema_document(schema)
        validator.validate(arguments)
    except (SchemaError, ValidationError, ValueError, RecursionError):
        return "Tool arguments do not conform to the advertised input schema."
    except Exception:
        # Reference-resolution failures differ between jsonschema 4.10 and
        # newer releases. Upstream schemas are untrusted, so every validator
        # failure must remain fail-closed and must never escape as an HTTP 500.
        return "Tool arguments do not conform to the advertised input schema."
    return None


def _validate_schema_document(schema: dict[str, Any]) -> Any:
    _bound_schema(schema)
    dialect = schema.get("$schema")
    if dialect not in {
        None,
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }:
        raise SchemaError("MCP tool schemas must use JSON Schema 2020-12")
    Draft202012Validator.check_schema(schema)
    resolver = _NoRemoteRefResolver.from_schema(schema)
    return Draft202012Validator(schema, resolver=resolver)


def _bound_schema(schema: dict[str, Any]) -> None:
    nodes = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 4096:
            raise ValueError("JSON Schema exceeds validation bounds")
        if not isinstance(value, dict):
            return
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            reference = value.get(keyword)
            if isinstance(reference, str) and not reference.startswith("#"):
                raise ValueError("network and external JSON Schema references are disabled")
        for child in _schema_children(value):
            walk(child, depth + 1)

    walk(schema, 0)


def _valid_cache_hints(result: dict[str, Any]) -> bool:
    ttl_ms = result.get("ttlMs")
    return isinstance(ttl_ms, int) and not isinstance(ttl_ms, bool) and ttl_ms >= 0 and result.get("cacheScope") in {
        "private",
        "public",
    }


_HTTP_TCHAR = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_SCHEMA_MAP_KEYWORDS = {
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
}
_SCHEMA_SINGLE_KEYWORDS = {
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}
_SCHEMA_LIST_KEYWORDS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _schema_children(schema: dict[str, Any], *, exclude: set[str] | None = None) -> list[Any]:
    excluded = exclude or set()
    children: list[Any] = []
    for keyword in _SCHEMA_MAP_KEYWORDS - excluded:
        child_map = schema.get(keyword)
        if isinstance(child_map, dict):
            children.extend(child for child in child_map.values() if isinstance(child, (dict, bool)))
    for keyword in _SCHEMA_SINGLE_KEYWORDS - excluded:
        child = schema.get(keyword)
        if isinstance(child, (dict, bool)):
            children.append(child)
    for keyword in _SCHEMA_LIST_KEYWORDS - excluded:
        child_list = schema.get(keyword)
        if isinstance(child_list, list):
            children.extend(child for child in child_list if isinstance(child, (dict, bool)))
    return children


def _upstream_cache_key(upstream: dict[str, Any]) -> str:
    return str(upstream.get("upstream_id") or upstream.get("endpoint") or "")


def _private_cache_context(access_token: str | None) -> str:
    token = access_token or ""
    return "private:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _arguments_hash(arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        encoded = b"invalid"
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def _mcp_schema_document() -> dict[str, Any]:
    # Verbatim official schema from modelcontextprotocol/modelcontextprotocol
    # tag 2026-07-28 (commit 5f5440bb26a62e2cf3440b92da5a667efa03b267).
    schema_path = resources.files("genos").joinpath("schemas/mcp_2026_07_28.json")
    value = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("$defs"), dict):
        raise RuntimeError("bundled MCP 2026-07-28 schema is invalid")
    return value


@lru_cache(maxsize=16)
def _mcp_definition_validator(definition: str) -> Draft202012Validator:
    schema = _mcp_schema_document()
    if definition not in schema["$defs"]:
        raise RuntimeError(f"unknown bundled MCP schema definition: {definition}")
    root = {
        "$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$ref": f"#/$defs/{definition}",
        "$defs": schema["$defs"],
    }
    return Draft202012Validator(root, format_checker=FormatChecker())


def mcp_value_matches_definition(definition: str, value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        _mcp_definition_validator(definition).validate(value)
    except (SchemaError, ValidationError, TypeError, ValueError, RecursionError):
        return False
    return True


def _mcp_header_annotations(schema: dict[str, Any]) -> list[tuple[str, tuple[str, ...], str]]:
    annotations: list[tuple[str, tuple[str, ...], str]] = []
    seen_names: set[str] = set()

    def walk(value: Any, *, path: tuple[str, ...], statically_reachable: bool) -> None:
        if not isinstance(value, dict):
            return
        if "x-mcp-header" in value:
            header_name = value.get("x-mcp-header")
            declared_type = value.get("type")
            folded = str(header_name).casefold() if isinstance(header_name, str) else ""
            if (
                not statically_reachable
                or not path
                or not isinstance(header_name, str)
                or not _HTTP_TCHAR.fullmatch(header_name)
                or folded in seen_names
                or declared_type not in {"string", "integer", "boolean"}
            ):
                raise ValueError("invalid x-mcp-header annotation")
            seen_names.add(folded)
            annotations.append((header_name, path, declared_type))
        properties = value.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                raise ValueError("invalid properties schema")
            for property_name, child in properties.items():
                if not isinstance(property_name, str) or not isinstance(child, (dict, bool)):
                    raise ValueError("invalid property schema")
                walk(
                    child,
                    path=(*path, property_name),
                    statically_reachable=statically_reachable,
                )
        for child in _schema_children(value, exclude={"properties"}):
            walk(child, path=path, statically_reachable=False)

    walk(schema, path=(), statically_reachable=True)
    return annotations


def _strip_mcp_header_annotations(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = {key: child for key, child in value.items() if key != "x-mcp-header"}
    for keyword in _SCHEMA_MAP_KEYWORDS:
        child_map = result.get(keyword)
        if isinstance(child_map, dict):
            result[keyword] = {
                name: _strip_mcp_header_annotations(child)
                for name, child in child_map.items()
            }
    for keyword in _SCHEMA_SINGLE_KEYWORDS:
        child = result.get(keyword)
        if isinstance(child, dict):
            result[keyword] = _strip_mcp_header_annotations(child)
    for keyword in _SCHEMA_LIST_KEYWORDS:
        children = result.get(keyword)
        if isinstance(children, list):
            result[keyword] = [_strip_mcp_header_annotations(child) for child in children]
    return result


def _mcp_parameter_headers(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header_name, path, declared_type in _mcp_header_annotations(schema):
        current: Any = arguments
        present = True
        for part in path:
            if not isinstance(current, dict) or part not in current:
                present = False
                break
            current = current[part]
        if not present or current is None:
            continue
        if declared_type == "string":
            if not isinstance(current, str):
                raise McpUpstreamError("upstream MCP header parameter has invalid type")
            text = current
        elif declared_type == "integer":
            if (
                not isinstance(current, int)
                or isinstance(current, bool)
                or not -_MAX_SAFE_INTEGER <= current <= _MAX_SAFE_INTEGER
            ):
                raise McpUpstreamError("upstream MCP header integer is outside the safe range")
            text = str(current)
        else:
            if not isinstance(current, bool):
                raise McpUpstreamError("upstream MCP header parameter has invalid type")
            text = "true" if current else "false"
        headers[f"Mcp-Param-{header_name}"] = _encode_mcp_header_value(text)
    return headers


def _encode_mcp_header_value(value: str) -> str:
    plain_safe = bool(value) and value == value.strip() and all(0x20 <= ord(char) <= 0x7E for char in value)
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if plain_safe and not sentinel:
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def _valid_call_tool_result(
    value: Any,
    *,
    client_capabilities: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    result_type = value.get("resultType")
    if not isinstance(result_type, str):
        return False
    if result_type == "input_required":
        if "ttlMs" in value or "cacheScope" in value:
            return False
        input_requests = value.get("inputRequests")
        request_state = value.get("requestState")
        has_requests = isinstance(input_requests, dict) and bool(input_requests)
        has_state = isinstance(request_state, str)
        return (
            (has_requests or has_state)
            and mcp_value_matches_definition("InputRequiredResult", value)
            and _input_requests_allowed(input_requests or {}, client_capabilities or {})
        )
    # Extension result tags require an explicitly negotiated extension schema.
    # MVP-07 advertises no such extension, so only the two core tags are valid.
    if result_type != "complete":
        return False
    return mcp_value_matches_definition("CallToolResult", value)


def _input_requests_allowed(
    requests: dict[str, Any],
    client_capabilities: dict[str, Any],
) -> bool:
    if not mcp_value_matches_definition("InputRequests", requests):
        return False
    for request in requests.values():
        method = request.get("method") if isinstance(request, dict) else None
        params = request.get("params") if isinstance(request, dict) else None
        if method == "roots/list":
            if not isinstance(client_capabilities.get("roots"), dict):
                return False
        elif method == "sampling/createMessage":
            sampling = client_capabilities.get("sampling")
            if not isinstance(sampling, dict):
                return False
            if isinstance(params, dict):
                if ("tools" in params or "toolChoice" in params) and not isinstance(sampling.get("tools"), dict):
                    return False
                if params.get("includeContext") in {"thisServer", "allServers"} and not isinstance(
                    sampling.get("context"), dict
                ):
                    return False
        elif method == "elicitation/create":
            elicitation = client_capabilities.get("elicitation")
            if not isinstance(elicitation, dict) or not isinstance(params, dict):
                return False
            mode = params.get("mode", "form")
            if mode not in {"form", "url"} or not isinstance(elicitation.get(mode), dict):
                return False
        else:
            return False
    return True


def _decode_mcp_response(raw: bytes, *, content_type: str, request_id: str) -> dict[str, Any]:
    if content_type == "application/json":
        messages = [_decode_json_object(raw)]
    elif content_type == "text/event-stream":
        messages = _decode_sse_messages(raw)
    else:
        raise McpUpstreamError("upstream MCP returned unsupported content type")
    final: dict[str, Any] | None = None
    for message in messages:
        _validate_upstream_message(message)
        if message.get("id") == request_id and (("result" in message) != ("error" in message)):
            final = message
    if final is None or final.get("jsonrpc") != "2.0":
        raise McpUpstreamError("upstream MCP response id did not match request")
    return final


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise McpUpstreamError("upstream MCP returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise McpUpstreamError("upstream MCP response must be an object")
    return value


def _validate_upstream_message(message: dict[str, Any]) -> None:
    if "method" in message and "id" in message:
        raise McpUpstreamError("upstream MCP attempted a server request")
    if "result" in message and "error" in message:
        raise McpUpstreamError("upstream MCP response contained both result and error")


def _read_sse_response(
    response: Any,
    *,
    request_id: str,
    max_response_bytes: int,
    deadline: float,
) -> dict[str, Any]:
    consumed = 0
    data_lines: list[str] = []
    while True:
        if time.monotonic() > deadline:
            raise McpUpstreamError("upstream MCP SSE response timed out")
        remaining = max_response_bytes - consumed
        if remaining <= 0:
            raise McpUpstreamError("upstream MCP response exceeded limit")
        line = response.readline(remaining + 1)
        consumed += len(line)
        if consumed > max_response_bytes:
            raise McpUpstreamError("upstream MCP response exceeded limit")
        if not line:
            if data_lines:
                message = _decode_json_object("\n".join(data_lines).encode("utf-8"))
                _validate_upstream_message(message)
                if message.get("id") == request_id and (("result" in message) != ("error" in message)):
                    if message.get("jsonrpc") != "2.0":
                        raise McpUpstreamError("upstream MCP response used invalid JSON-RPC version")
                    return message
            raise McpUpstreamError("upstream MCP SSE stream ended without a matching response")
        try:
            text = line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise McpUpstreamError("upstream MCP returned invalid SSE encoding") from exc
        if text == "":
            if not data_lines:
                continue
            message = _decode_json_object("\n".join(data_lines).encode("utf-8"))
            data_lines = []
            _validate_upstream_message(message)
            if message.get("id") == request_id and (("result" in message) != ("error" in message)):
                if message.get("jsonrpc") != "2.0":
                    raise McpUpstreamError("upstream MCP response used invalid JSON-RPC version")
                return message
            continue
        if text.startswith(":"):
            continue
        field, separator, value = text.partition(":")
        if field == "data" and separator:
            data_lines.append(value[1:] if value.startswith(" ") else value)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_sse_messages(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpUpstreamError("upstream MCP returned invalid SSE encoding") from exc
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if data_lines:
                messages.append(_decode_json_object("\n".join(data_lines).encode("utf-8")))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data" and separator:
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        messages.append(_decode_json_object("\n".join(data_lines).encode("utf-8")))
    if not messages:
        raise McpUpstreamError("upstream MCP SSE stream contained no messages")
    return messages
