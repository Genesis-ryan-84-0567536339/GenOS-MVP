from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import json
import os
import re

from . import __version__
from .agent_auth import AgentAuthBridge, AgentAuthError
from .agent_runtime import AgentNeedsAction, AgentRuntimeError, AgentRuntimeStore
from .agent_secure_runtime import SecretAwareGeminiAdapter
from .auth_service import (
    AuthConflict,
    AuthError,
    AuthenticationFailed,
    AuthorizationFailed,
    CredentialConflict,
    CredentialError,
    CredentialNotFound,
    CredentialService,
    OwnerAuthService,
)
from .drive_bridge import DriveBridgeError, DriveNeedsAction, DriveRemoteError
from .drive_store import DriveStoreError
from .drive_system import DriveSystemError, DriveSystemServices, build_drive_system
from .artifact_store import ArtifactStoreError
from .drive_collab import DriveCollaborationError
from .kanban import InvalidCardTransition, KanbanError, KanbanSystem, build_kanban_system
from .kanban_store import CardConflict, CardNotFound, KanbanStoreError
from .mcp_store import McpConflict, McpNotFound, McpStoreError, PostgresMcpStore
from .observability import ObservabilityService
from .product_store import PostgresProductStore, ProductStoreError
from .report_bridge import ReportBridgeError
from .secret_provider import LocalFileSecretProvider, SecretProviderError


MAX_JSON_BODY = 64 * 1024
_CREDENTIAL_ACTION = re.compile(r"^/api/v1/credentials/([0-9a-fA-F-]{36})/(rotate|test|disable)$")
_AGENT_ID = "agy-gen"
_AGENT_AUTH_BASE = f"/api/v1/agents/{_AGENT_ID}/auth"
_DRIVE_BASE = "/api/v1/drive"
_DRIVE_OAUTH = f"{_DRIVE_BASE}/oauth"
_SYSTEM_REPORT = "/api/v1/reports/system"
_CARDS_BASE = "/api/v1/cards"
_KANBAN_SYNC = "/api/v1/kanban/sync"
_KANBAN_AGENT_TICK = "/api/v1/kanban/agent-tick"
_MCP_BASE = "/api/v1/mcp"
_MCP_PRINCIPALS = f"{_MCP_BASE}/principals"
_MCP_UPSTREAMS = f"{_MCP_BASE}/upstreams"
_MCP_AUDIT = f"{_MCP_BASE}/audit"
_CARD_ITEM = re.compile(r"^/api/v1/cards/([0-9a-fA-F-]{36})$")
_CARD_ACTION = re.compile(r"^/api/v1/cards/([0-9a-fA-F-]{36})/(transition|comment)$")
_MCP_PRINCIPAL_ACTION = re.compile(r"^/api/v1/mcp/principals/([0-9a-fA-F-]{36})/(rotate|revoke|scopes)$")
_MCP_UPSTREAM_ACTION = re.compile(r"^/api/v1/mcp/upstreams/([0-9a-fA-F-]{36})/(disable)$")


class ProductAPIApp:
    def __init__(
        self,
        auth: OwnerAuthService,
        credentials: CredentialService,
        store: PostgresProductStore,
        agent_store: AgentRuntimeStore,
        agent_auth: AgentAuthBridge,
        observability: ObservabilityService | None = None,
        drive_system: DriveSystemServices | None = None,
        kanban_system: KanbanSystem | None = None,
        mcp_store: PostgresMcpStore | None = None,
    ) -> None:
        self.auth = auth
        self.credentials = credentials
        self.store = store
        self.agent_store = agent_store
        self.agent_auth = agent_auth
        self.observability = observability or ObservabilityService()
        self.drive_system = drive_system
        self.kanban_system = kanban_system
        self.mcp_store = mcp_store

    @classmethod
    def from_system(cls) -> "ProductAPIApp":
        store = PostgresProductStore()
        store.ensure_schema()
        secret_root = os.environ.get("GENOS_SECRET_DIR", "/var/lib/genos/secrets")
        provider = LocalFileSecretProvider(secret_root)
        credentials = CredentialService(store, provider)
        agent_root = Path(os.environ.get("GENOS_AGY_GEN_DIR", "/var/lib/genos/agents/agy-gen"))
        agent_store = AgentRuntimeStore(agent_root)
        observability = ObservabilityService()
        drive_system = build_drive_system(product_store=store, credentials=credentials, observability=observability)
        kanban_system = build_kanban_system(product_store=store, credentials=credentials, agent_root=agent_root)
        mcp_store = PostgresMcpStore(store)
        mcp_store.ensure_schema()
        return cls(
            OwnerAuthService(store),
            credentials,
            store,
            agent_store,
            AgentAuthBridge(agent_store),
            observability,
            drive_system,
            kanban_system,
            mcp_store,
        )

    def read_observability(self) -> dict[str, Any]:
        """Return the same authoritative read model used by `genos doctor`."""
        return self.observability.snapshot()

    def drive_status(self) -> dict[str, Any]:
        return self._drive().connection.status()

    def drive_connect(self, *, secret_id: str, root_name: str = "GenOS") -> dict[str, Any]:
        return self._drive().connect(secret_id=secret_id, root_name=root_name)

    def drive_verify(self) -> dict[str, Any]:
        return self._drive().connection.verify()

    def drive_oauth_status(self) -> dict[str, Any]:
        return self._drive().oauth_status()

    def drive_oauth_start(self, *, root_name: str = "GenOS") -> dict[str, Any]:
        return self._drive().oauth_start(root_name=root_name)

    def drive_oauth_poll(self) -> dict[str, Any]:
        return self._drive().oauth_poll()

    def drive_disconnect(self) -> dict[str, Any]:
        return self._drive().disconnect()

    def drive_reauthorize(self, *, root_name: str | None = None) -> dict[str, Any]:
        return self._drive().reauthorize(root_name=root_name)

    def drive_reconnect(self, *, root_name: str | None = None) -> dict[str, Any]:
        return self._drive().reconnect(root_name=root_name)

    def publish_system_report(self, *, manual: bool = True) -> dict[str, Any]:
        return self._drive().reports.publish(manual=manual)

    def cards_list(self) -> list[dict[str, Any]]:
        return self._kanban().list_cards()

    def card_get(self, card_id: str) -> dict[str, Any]:
        return self._kanban().get_card(card_id)

    def card_create(self, *, title: str, description: str = "", assignee_agent_id: str | None = "agy-gen") -> dict[str, Any]:
        return self._kanban().create_card(title=title, description=description, assignee_agent_id=assignee_agent_id)

    def card_transition(self, card_id: str, *, to_state: str, reason: str = "OWNER_ACTION") -> dict[str, Any]:
        return self._kanban().transition(card_id, to_state=to_state, reason=reason)

    def card_comment(self, card_id: str, *, text: str) -> dict[str, Any]:
        return self._kanban().add_comment(card_id, text=text)

    def kanban_sync(self) -> dict[str, Any]:
        return self._kanban().sync_drive_inbox()

    def kanban_agent_tick(self) -> dict[str, Any]:
        return self._kanban().agent_tick()

    def mcp_status(self) -> dict[str, Any]:
        store = self._mcp()
        port = os.environ.get("GENOS_MCP_PORT")
        if not port:
            port_path = Path("/etc/genos/mcp-port")
            port = port_path.read_text(encoding="utf-8").strip() if port_path.is_file() else None
        return {"protocol_version": "2026-07-28", "endpoint": f"http://127.0.0.1:{port}/mcp" if port else None, "principal_count": len(store.list_principals()), "upstreams": store.list_upstreams()}

    def mcp_create_principal(self, *, name: str, scopes: list[str]) -> dict[str, Any]:
        return self._mcp().create_principal(name=name, scopes=scopes).one_way_response()

    def mcp_rotate_principal(self, principal_id: str) -> dict[str, Any]:
        return self._mcp().rotate_principal(principal_id).one_way_response()

    def mcp_revoke_principal(self, principal_id: str) -> dict[str, Any]:
        return self._mcp().revoke_principal(principal_id)

    def mcp_replace_scopes(self, principal_id: str, scopes: list[str]) -> dict[str, Any]:
        return self._mcp().replace_scopes(principal_id, scopes)

    def mcp_register_upstream(self, *, namespace: str, name: str, endpoint: str, secret_id: str | None) -> dict[str, Any]:
        if secret_id:
            record = self.store.get_credential(secret_id)
            if record is None or record.status != "ACTIVE" or "mcp-hub" not in record.consumer_scopes:
                raise CredentialError("SecretRef must be ACTIVE and granted to consumer mcp-hub")
        return self._mcp().register_upstream(namespace=namespace, name=name, endpoint=endpoint, secret_id=secret_id)

    def _kanban(self) -> KanbanSystem:
        if self.kanban_system is None:
            raise KanbanError("Kanban system is not configured")
        return self.kanban_system

    def _mcp(self) -> PostgresMcpStore:
        if self.mcp_store is None:
            raise McpStoreError("MCP store is not configured")
        return self.mcp_store

    def _drive(self) -> DriveSystemServices:
        if self.drive_system is None:
            raise DriveSystemError("Drive system is not configured")
        return self.drive_system


class ProductAPIHandler(BaseHTTPRequestHandler):
    server_version = "GenOSProductAPI/0.1"

    @property
    def app(self) -> ProductAPIApp:
        return getattr(self.server, "genos_app")  # type: ignore[no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "role": "product-api",
                        "version": __version__,
                        "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
                    },
                )
                return
            if self.path == "/api/v1/auth/me":
                owner = self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"owner": owner})
                return
            if self.path == "/api/v1/observability":
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"observability": self.app.read_observability()})
                return
            if self.path == "/api/v1/credentials":
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"credentials": self.app.credentials.list()})
                return
            if self.path == _CARDS_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"cards": self.app.cards_list()})
                return
            card_item = _CARD_ITEM.match(self.path)
            if card_item:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, self.app.card_get(card_item.group(1)))
                return
            if self.path == _MCP_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"mcp": self.app.mcp_status()})
                return
            if self.path == _MCP_PRINCIPALS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"principals": self.app._mcp().list_principals()})
                return
            if self.path == _MCP_UPSTREAMS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"upstreams": self.app._mcp().list_upstreams()})
                return
            if self.path == _MCP_AUDIT:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"audit": self.app._mcp().recent_audit(limit=100)})
                return
            if self.path == _DRIVE_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"drive": self.app.drive_status()})
                return
            if self.path == _DRIVE_OAUTH:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"oauth": self.app.drive_oauth_status()})
                return
            if self.path == _AGENT_AUTH_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"auth": self.app.agent_auth.status()})
                return
            self._json(404, {"error": "not_found"})
        except Exception as exc:  # mapped centrally; no raw body/header logging
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/v1/owner/bootstrap":
                body = self._read_json()
                owner = self.app.auth.bootstrap_owner(
                    _required_text(body, "username"),
                    _required_text(body, "password"),
                )
                self._json(201, {"owner": owner})
                return
            if self.path == "/api/v1/auth/login":
                body = self._read_json()
                result = self.app.auth.login(
                    _required_text(body, "username"),
                    _required_text(body, "password"),
                )
                self._json(200, result.one_way_response())
                return
            if self.path == "/api/v1/auth/logout":
                self.app.auth.logout(self._bearer_token())
                self._json(200, {"state": "REVOKED"})
                return
            if self.path == "/api/v1/credentials":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                scopes_raw = body.get("consumer_scopes", [])
                if not isinstance(scopes_raw, list) or not all(isinstance(item, str) for item in scopes_raw):
                    raise CredentialError("consumer_scopes must be a list of strings")
                record = self.app.credentials.add(
                    name=_required_text(body, "name"),
                    provider_name=_required_text(body, "provider"),
                    raw_secret=_required_text(body, "secret"),
                    consumer_scopes=list(scopes_raw),
                    source="owner-api",
                )
                self._json(201, {"credential": record})
                return
            if self.path == _CARDS_BASE:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                description = body.get("description", "")
                if not isinstance(description, str):
                    raise AuthError("description must be a string")
                assignee = body.get("assignee_agent_id", "agy-gen")
                if assignee is not None and assignee != "agy-gen":
                    raise AuthError("MVP only supports assignee agy-gen")
                self._json(201, {"card": self.app.card_create(title=_required_text(body, "title"), description=description, assignee_agent_id=assignee)})
                return
            if self.path == _KANBAN_SYNC:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"sync": self.app.kanban_sync()})
                return
            if self.path == _KANBAN_AGENT_TICK:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"agent_tick": self.app.kanban_agent_tick()})
                return
            card_action = _CARD_ACTION.match(self.path)
            if card_action:
                self.app.auth.authenticate(self._bearer_token())
                card_id, operation = card_action.groups()
                body = self._read_json()
                if operation == "transition":
                    reason = body.get("reason", "OWNER_ACTION")
                    if not isinstance(reason, str):
                        raise AuthError("reason must be a string")
                    self._json(200, self.app.card_transition(card_id, to_state=_required_text(body, "to_state"), reason=reason))
                    return
                self._json(200, self.app.card_comment(card_id, text=_required_text(body, "text")))
                return
            if self.path == _MCP_PRINCIPALS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                _reject_unknown_fields(body, {"name", "scopes"})
                scopes = _required_string_list(body, "scopes")
                self._json(201, {"mcp": self.app.mcp_create_principal(name=_required_text(body, "name"), scopes=scopes)})
                return
            principal_action = _MCP_PRINCIPAL_ACTION.match(self.path)
            if principal_action:
                self.app.auth.authenticate(self._bearer_token())
                principal_id, operation = principal_action.groups()
                if operation == "rotate":
                    self._reject_nonempty_body(); self._json(200, {"mcp": self.app.mcp_rotate_principal(principal_id)}); return
                if operation == "revoke":
                    self._reject_nonempty_body(); self._json(200, {"principal": self.app.mcp_revoke_principal(principal_id)}); return
                body = self._read_json(); _reject_unknown_fields(body, {"scopes"}); self._json(200, {"principal": self.app.mcp_replace_scopes(principal_id, _required_string_list(body, "scopes"))}); return
            if self.path == _MCP_UPSTREAMS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                _reject_unknown_fields(body, {"namespace", "name", "endpoint", "secret_id"})
                secret_id = body.get("secret_id")
                if secret_id is not None and not isinstance(secret_id, str): raise AuthError("secret_id must be a string")
                upstream = self.app.mcp_register_upstream(namespace=_required_text(body, "namespace"), name=_required_text(body, "name"), endpoint=_required_text(body, "endpoint"), secret_id=secret_id)
                self._json(201, {"upstream": upstream}); return
            upstream_action = _MCP_UPSTREAM_ACTION.match(self.path)
            if upstream_action:
                self.app.auth.authenticate(self._bearer_token()); self._reject_nonempty_body()
                self._json(200, {"upstream": self.app._mcp().disable_upstream(upstream_action.group(1))}); return
            if self.path == f"{_DRIVE_BASE}/connect":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                result = self.app.drive_connect(
                    secret_id=_required_text(body, "secret_id"),
                    root_name=_optional_root_name(body),
                )
                self._json(200, {"drive": result})
                return
            if self.path == f"{_DRIVE_BASE}/verify":
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"drive": self.app.drive_verify()})
                return
            if self.path == f"{_DRIVE_OAUTH}/start":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_optional_json()
                self._json(200, {"oauth": self.app.drive_oauth_start(root_name=_optional_root_name(body))})
                return
            if self.path == f"{_DRIVE_OAUTH}/poll":
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"oauth": self.app.drive_oauth_poll()})
                return
            if self.path == f"{_DRIVE_BASE}/disconnect":
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"drive": self.app.drive_disconnect()})
                return
            if self.path == f"{_DRIVE_BASE}/reauthorize":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_optional_json()
                root_name = body.get("root_name")
                if root_name is not None and not isinstance(root_name, str):
                    raise AuthError("root_name must be a string")
                self._json(200, {"oauth": self.app.drive_reauthorize(root_name=root_name)})
                return
            if self.path == f"{_DRIVE_BASE}/reconnect":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_optional_json()
                root_name = body.get("root_name")
                if root_name is not None and not isinstance(root_name, str):
                    raise AuthError("root_name must be a string")
                self._json(200, {"drive": self.app.drive_reconnect(root_name=root_name)})
                return
            if self.path == _SYSTEM_REPORT:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                self._json(200, {"report": self.app.publish_system_report(manual=True)})
                return
            if self.path == f"{_AGENT_AUTH_BASE}/start":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_optional_json()
                projection = self.app.agent_auth.start(restart=bool(body.get("restart", False)))
                self._json(200, {"auth": projection})
                return
            if self.path == f"{_AGENT_AUTH_BASE}/code":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                # Authorization code is one-way ingress to tmux stdin. The
                # response deliberately contains no echo/fingerprint of it.
                result = self.app.agent_auth.submit_code(_required_text(body, "code"))
                self._json(200, {"auth": result})
                return
            if self.path == f"{_AGENT_AUTH_BASE}/verify":
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                probe = SecretAwareGeminiAdapter(self.app.agent_store).activate_with_real_probe()
                self._json(200, {"provider": probe.to_dict()})
                return
            action = _CREDENTIAL_ACTION.match(self.path)
            if action:
                self.app.auth.authenticate(self._bearer_token())
                secret_id, operation = action.groups()
                if operation == "rotate":
                    body = self._read_json()
                    record = self.app.credentials.rotate(
                        secret_id,
                        _required_text(body, "secret"),
                        source="owner-api",
                    )
                    self._json(200, {"credential": record})
                    return
                if operation == "test":
                    self._reject_nonempty_body()
                    self._json(200, {"test": self.app.credentials.test(secret_id)})
                    return
                if operation == "disable":
                    self._reject_nonempty_body()
                    self._json(200, {"credential": self.app.credentials.disable(secret_id)})
                    return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def log_message(self, fmt: str, *args: object) -> None:
        # Request paths are fixed/typed and never carry credentials. Headers and
        # bodies are intentionally omitted so Bearer tokens/raw secrets/auth
        # codes cannot appear in service logs.
        message = fmt % args
        print(json.dumps({"event": "product_api_http", "message": message}, ensure_ascii=False), flush=True)

    def _bearer_token(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise AuthorizationFailed("missing session")
        token = value[7:].strip()
        if not token:
            raise AuthorizationFailed("missing session")
        return token

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise AuthError("Content-Length required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AuthError("invalid Content-Length") from exc
        if length < 0 or length > MAX_JSON_BODY:
            raise AuthError("request body too large")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise AuthError("application/json required")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AuthError("JSON object required")
        return payload

    def _read_optional_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length in {None, "", "0"}:
            return {}
        return self._read_json()

    def _reject_nonempty_body(self) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length and raw_length != "0":
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AuthError("invalid Content-Length") from exc
            if length > 0:
                self.rfile.read(min(length, MAX_JSON_BODY))
                raise AuthError("request body not allowed")

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, AuthConflict):
            self._json(409, {"error": "owner_exists"})
            return
        if isinstance(exc, AuthenticationFailed):
            self._json(401, {"error": "invalid_credentials"})
            return
        if isinstance(exc, AuthorizationFailed):
            self._json(401, {"error": "unauthorized"})
            return
        if isinstance(exc, CredentialConflict):
            self._json(409, {"error": "credential_conflict"})
            return
        if isinstance(exc, CredentialNotFound):
            self._json(404, {"error": "credential_not_found"})
            return
        if isinstance(exc, (CardNotFound, McpNotFound)):
            self._json(404, {"error": "not_found"})
            return
        if isinstance(exc, (CardConflict, InvalidCardTransition, McpConflict)):
            self._json(409, {"error": "conflict"})
            return
        if isinstance(exc, AgentNeedsAction):
            self._json(409, {"error": "agent_needs_action"})
            return
        if isinstance(exc, DriveNeedsAction):
            self._json(409, {"error": "drive_needs_action"})
            return
        if isinstance(exc, DriveRemoteError):
            self._json(503, {"error": "backend_unavailable"})
            return
        if isinstance(exc, (AuthError, CredentialError, AgentAuthError, DriveBridgeError, KanbanError, ArtifactStoreError, DriveCollaborationError, McpStoreError, ValueError)):
            self._json(400, {"error": "invalid_request"})
            return
        if isinstance(
            exc,
            (ProductStoreError, SecretProviderError, AgentRuntimeError, DriveStoreError, DriveSystemError, ReportBridgeError, KanbanStoreError),
        ):
            self._json(503, {"error": "backend_unavailable"})
            return
        self._json(500, {"error": "internal_error"})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AuthError(f"{key} is required")
    return value


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    if any(key not in allowed for key in payload):
        raise AuthError("request contains unsupported fields")


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuthError(f"{key} must be a list of strings")
    return list(value)


def _optional_root_name(payload: dict[str, Any]) -> str:
    root_name = payload.get("root_name", "GenOS")
    if not isinstance(root_name, str):
        raise AuthError("root_name must be a string")
    return root_name


def attach_product_api(server: ThreadingHTTPServer) -> None:
    server.genos_app = ProductAPIApp.from_system()  # type: ignore[attr-defined]
