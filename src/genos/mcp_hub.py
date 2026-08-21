from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import json
import os
import time
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest

from .agent_runtime import AgentRuntimeStore
from .auth_service import CredentialError, CredentialService
from .drive_system import DriveSystemServices, build_drive_system
from .kanban import KanbanSystem, build_kanban_system
from .mcp_store import PostgresMcpStore, scope_allows
from .observability import ObservabilityService
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider


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

    def list_tools(self, upstream: dict[str, Any], access_token: str | None) -> list[dict[str, Any]]:
        response = self._call(upstream, access_token, method="tools/list", name=None, params={})
        result = response.get("result") if isinstance(response, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpUpstreamError("upstream tools/list returned invalid shape")
        safe: list[dict[str, Any]] = []
        for tool in tools[:512]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            safe.append(
                {
                    "name": str(tool["name"])[:160],
                    "description": str(tool.get("description") or "")[:2048],
                    "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object"},
                }
            )
        return safe

    def call_tool(
        self,
        upstream: dict[str, Any],
        access_token: str | None,
        *,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._call(upstream, access_token, method="tools/call", name=name, params={"name": name, "arguments": arguments})
        if "error" in response:
            raise McpUpstreamError("upstream MCP tool returned an error")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpUpstreamError("upstream tools/call returned invalid result")
        return result

    def _call(
        self,
        upstream: dict[str, Any],
        access_token: str | None,
        *,
        method: str,
        name: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
            "_meta": {"io.modelcontextprotocol/clientInfo": {"name": "genos-mcp-hub", "version": "0.1"}},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = name
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        req = urlrequest.Request(
            str(upstream["endpoint"]),
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:  # noqa: S310 - endpoint validated at registration
                raw = response.read(self.max_response_bytes + 1)
        except urlerror.HTTPError as exc:
            raise McpUpstreamError(f"upstream MCP HTTP {exc.code}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise McpUpstreamError("upstream MCP unavailable") from exc
        if len(raw) > self.max_response_bytes:
            raise McpUpstreamError("upstream MCP response exceeded limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpUpstreamError("upstream MCP returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise McpUpstreamError("upstream MCP response must be an object")
        return value


class GenOSMcpHub:
    def __init__(
        self,
        *,
        store: Any,
        local_tools: dict[str, ToolSpec],
        credentials: CredentialService | None = None,
        upstream_client: Any | None = None,
    ) -> None:
        self.store = store
        self.local_tools = local_tools
        self.credentials = credentials
        self.upstream_client = upstream_client or UpstreamMcpClient()

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

    def discover(self, principal: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {"name": "genos-mcp-hub", "version": "0.1"},
            "capabilities": {"tools": {"listChanged": False}},
            "authorization": {"principal_id": principal["principal_id"], "grant_mode": "deny-by-default"},
        }

    def list_tools(self, principal: dict[str, Any]) -> dict[str, Any]:
        scopes = list(principal.get("scopes") or [])
        tools = [spec.public_dict() for name, spec in sorted(self.local_tools.items()) if scope_allows(scopes, name)]
        for upstream in self.store.list_upstreams(active_only=True):
            namespace = str(upstream["namespace"])
            if not any(scope.startswith(namespace + ".") for scope in scopes):
                continue
            try:
                token = self._upstream_secret(upstream)
                remote_tools = self.upstream_client.list_tools(upstream, token)
                self.store.mark_upstream(str(upstream["upstream_id"]), state="HEALTHY")
            except (McpUpstreamError, CredentialError):
                self.store.mark_upstream(str(upstream["upstream_id"]), state="DEGRADED")
                continue
            for remote in remote_tools:
                full_name = f"{namespace}.{remote['name']}"
                if full_name in self.local_tools or not scope_allows(scopes, full_name):
                    continue
                tools.append({**remote, "name": full_name, "x-genos-upstream": namespace})
        tools.sort(key=lambda item: str(item.get("name") or ""))
        return {"tools": tools, "ttlMs": 15000, "cacheScope": "private"}

    def call_tool(
        self,
        principal: dict[str, Any],
        *,
        name: str,
        arguments: dict[str, Any],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        principal_id = str(principal["principal_id"])
        scopes = list(principal.get("scopes") or [])
        if not scope_allows(scopes, name):
            self._audit(principal_id, None, name, "DENY", "FORBIDDEN", started, correlation_id)
            raise McpForbidden("MCP tool is not granted")
        local = self.local_tools.get(name)
        if local is not None:
            try:
                result = local.handler(arguments)
                envelope = _tool_result(result)
                self._audit(principal_id, None, name, "ALLOW", "PASS", started, correlation_id)
                return envelope
            except Exception as exc:
                self._audit(principal_id, None, name, "ALLOW", type(exc).__name__, started, correlation_id)
                raise McpHubError("local GenOS MCP tool failed") from exc

        namespace, sep, remote_name = name.partition(".")
        if not sep or namespace == "genos" or not remote_name:
            self._audit(principal_id, None, name, "ALLOW", "UNKNOWN_TOOL", started, correlation_id)
            raise McpUnknownTool("unknown MCP tool")
        upstream = next((item for item in self.store.list_upstreams(active_only=True) if item.get("namespace") == namespace), None)
        if upstream is None:
            self._audit(principal_id, None, name, "ALLOW", "UNKNOWN_UPSTREAM", started, correlation_id)
            raise McpUnknownTool("unknown MCP upstream namespace")
        upstream_id = str(upstream["upstream_id"])
        try:
            result = self.upstream_client.call_tool(
                upstream,
                self._upstream_secret(upstream),
                name=remote_name,
                arguments=arguments,
            )
            self.store.mark_upstream(upstream_id, state="HEALTHY")
            self._audit(principal_id, upstream_id, name, "ALLOW", "PASS", started, correlation_id)
            return result
        except (McpUpstreamError, CredentialError) as exc:
            self.store.mark_upstream(upstream_id, state="DEGRADED")
            self._audit(principal_id, upstream_id, name, "ALLOW", "UPSTREAM_DEGRADED", started, correlation_id)
            raise McpUpstreamError("registered upstream MCP is degraded") from exc

    def authenticate(self, access_token: str) -> dict[str, Any]:
        principal = self.store.authenticate(access_token)
        if principal is None:
            raise McpUnauthorized("invalid or revoked MCP access token")
        return principal

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
    text = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise McpHubError("tool result exceeded MCP response limit")
    return {"content": [{"type": "text", "text": text}], "structuredContent": safe, "isError": False}
