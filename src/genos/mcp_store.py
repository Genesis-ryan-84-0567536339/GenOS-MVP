from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import hashlib
import json
import re
import secrets
import uuid
from urllib.parse import urlparse

from .product_store import (
    PostgresProductStore,
    ProductStoreError,
    _bytea_expr,
    _jsonb_expr,
    _text_expr,
    _uuid_literal,
)


class McpStoreError(RuntimeError):
    pass


class McpNotFound(McpStoreError):
    pass


class McpConflict(McpStoreError):
    pass


_NAMESPACE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_SCOPE = re.compile(r"^[a-z][a-z0-9_-]{1,31}\.(?:\*|[a-zA-Z0-9_.-]{1,96})$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class IssuedPrincipal:
    principal: dict[str, Any]
    access_token: str

    def one_way_response(self) -> dict[str, Any]:
        return {"principal": self.principal, "access_token": self.access_token, "token_visibility": "ONE_TIME_ONLY"}


class PostgresMcpStore:
    """Durable MCP principal/grant/upstream/audit metadata.

    Plain MCP access tokens and upstream credentials never enter Product DB.
    Access tokens are persisted as SHA-256 hashes; upstream secrets remain
    SecretProvider values referenced by secret_id only.
    """

    def __init__(self, product_store: PostgresProductStore) -> None:
        self.product_store = product_store

    def ensure_schema(self) -> None:
        self.product_store._execute(  # noqa: SLF001 - package persistence extension
            """
BEGIN;
CREATE TABLE IF NOT EXISTS mcp_principal (
  principal_id UUID PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  token_hash BYTEA NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS mcp_upstream (
  upstream_id UUID PRIMARY KEY,
  namespace TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  secret_id UUID REFERENCES secret_ref(secret_id),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','DISABLED')),
  last_state TEXT,
  last_verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS mcp_audit_event (
  audit_id UUID PRIMARY KEY,
  principal_id UUID REFERENCES mcp_principal(principal_id),
  upstream_id UUID REFERENCES mcp_upstream(upstream_id),
  tool_name TEXT NOT NULL,
  decision TEXT NOT NULL,
  result_class TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  correlation_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mcp_audit_created_idx ON mcp_audit_event(created_at DESC);
CREATE INDEX IF NOT EXISTS mcp_audit_principal_idx ON mcp_audit_event(principal_id, created_at DESC);
INSERT INTO genos_schema_migration(version) VALUES (6) ON CONFLICT (version) DO NOTHING;
COMMIT;
"""
        )

    def create_principal(self, *, name: str, scopes: list[str]) -> IssuedPrincipal:
        clean_name = _bounded_name(name)
        clean_scopes = normalize_scopes(scopes)
        token = _new_token()
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        principal_id = str(uuid.uuid4())
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        try:
            self.product_store._execute(  # noqa: SLF001
                "INSERT INTO mcp_principal(principal_id,name,token_hash,fingerprint,status,scopes) VALUES ("
                f"{_uuid_literal(principal_id)},{_text_expr(clean_name)},{_bytea_expr(digest)},"
                f"{_text_expr(fingerprint)},'ACTIVE',{_jsonb_expr(json.dumps(clean_scopes, separators=(',', ':')))});"
            )
        except ProductStoreError as exc:
            raise McpConflict("MCP principal name already exists or token collision occurred") from exc
        principal = self.require_principal(principal_id)
        return IssuedPrincipal(principal=principal, access_token=token)

    def list_principals(self) -> list[dict[str, Any]]:
        return self._json_lines(_principal_select() + " ORDER BY created_at, name;")

    def require_principal(self, principal_id: str) -> dict[str, Any]:
        row = self.product_store._json_row(  # noqa: SLF001
            _principal_select() + f" WHERE principal_id={_uuid_literal(principal_id)} LIMIT 1;"
        )
        if row is None:
            raise McpNotFound("MCP principal not found")
        return _principal_public(row)

    def authenticate(self, access_token: str) -> dict[str, Any] | None:
        token = str(access_token).strip()
        if not token or len(token) > 512:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        row = self.product_store._json_row(  # noqa: SLF001
            _principal_select()
            + f" WHERE token_hash={_bytea_expr(digest)} AND status='ACTIVE' LIMIT 1;"
        )
        return _principal_public(row) if row else None

    def rotate_principal(self, principal_id: str) -> IssuedPrincipal:
        token = _new_token()
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        changed = self.product_store._scalar(  # noqa: SLF001
            "WITH updated AS (UPDATE mcp_principal SET "
            f"token_hash={_bytea_expr(digest)},fingerprint={_text_expr(fingerprint)},status='ACTIVE',updated_at=NOW() "
            f"WHERE principal_id={_uuid_literal(principal_id)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            raise McpNotFound("MCP principal not found")
        return IssuedPrincipal(principal=self.require_principal(principal_id), access_token=token)

    def revoke_principal(self, principal_id: str) -> dict[str, Any]:
        changed = self.product_store._scalar(  # noqa: SLF001
            "WITH updated AS (UPDATE mcp_principal SET status='REVOKED',updated_at=NOW() "
            f"WHERE principal_id={_uuid_literal(principal_id)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            raise McpNotFound("MCP principal not found")
        return self.require_principal(principal_id)

    def replace_scopes(self, principal_id: str, scopes: list[str]) -> dict[str, Any]:
        clean = normalize_scopes(scopes)
        changed = self.product_store._scalar(  # noqa: SLF001
            "WITH updated AS (UPDATE mcp_principal SET "
            f"scopes={_jsonb_expr(json.dumps(clean, separators=(',', ':')))},updated_at=NOW() "
            f"WHERE principal_id={_uuid_literal(principal_id)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            raise McpNotFound("MCP principal not found")
        return self.require_principal(principal_id)

    def register_upstream(
        self,
        *,
        namespace: str,
        name: str,
        endpoint: str,
        secret_id: str | None = None,
    ) -> dict[str, Any]:
        ns = normalize_namespace(namespace)
        if ns == "genos":
            raise McpConflict("genos namespace is reserved for local tools")
        clean_name = _bounded_name(name)
        clean_endpoint = normalize_endpoint(endpoint)
        upstream_id = str(uuid.uuid4())
        secret_sql = "NULL" if secret_id is None else _uuid_literal(secret_id)
        try:
            self.product_store._execute(  # noqa: SLF001
                "INSERT INTO mcp_upstream(upstream_id,namespace,name,endpoint,secret_id,status,last_state) VALUES ("
                f"{_uuid_literal(upstream_id)},{_text_expr(ns)},{_text_expr(clean_name)},{_text_expr(clean_endpoint)},"
                f"{secret_sql},'ACTIVE','UNKNOWN');"
            )
        except ProductStoreError as exc:
            raise McpConflict("MCP upstream namespace already exists or SecretRef is invalid") from exc
        return self.require_upstream(upstream_id)

    def list_upstreams(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = _upstream_select()
        if active_only:
            sql += " WHERE status='ACTIVE'"
        return self._json_lines(sql + " ORDER BY namespace;")

    def require_upstream(self, upstream_id: str) -> dict[str, Any]:
        row = self.product_store._json_row(  # noqa: SLF001
            _upstream_select() + f" WHERE upstream_id={_uuid_literal(upstream_id)} LIMIT 1;"
        )
        if row is None:
            raise McpNotFound("MCP upstream not found")
        return _upstream_public(row)

    def disable_upstream(self, upstream_id: str) -> dict[str, Any]:
        changed = self.product_store._scalar(  # noqa: SLF001
            "WITH updated AS (UPDATE mcp_upstream SET status='DISABLED',updated_at=NOW() "
            f"WHERE upstream_id={_uuid_literal(upstream_id)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            raise McpNotFound("MCP upstream not found")
        return self.require_upstream(upstream_id)

    def mark_upstream(self, upstream_id: str, *, state: str) -> None:
        safe_state = _bounded_token(state, 64)
        self.product_store._execute(  # noqa: SLF001
            "UPDATE mcp_upstream SET "
            f"last_state={_text_expr(safe_state)},last_verified_at=NOW(),updated_at=NOW() "
            f"WHERE upstream_id={_uuid_literal(upstream_id)};"
        )

    def audit(
        self,
        *,
        principal_id: str | None,
        upstream_id: str | None,
        tool_name: str,
        decision: str,
        result_class: str,
        duration_ms: int,
        correlation_id: str | None,
    ) -> None:
        principal_sql = "NULL" if principal_id is None else _uuid_literal(principal_id)
        upstream_sql = "NULL" if upstream_id is None else _uuid_literal(upstream_id)
        correlation_sql = "NULL" if not correlation_id else _text_expr(_bounded_token(correlation_id, 128))
        self.product_store._execute(  # noqa: SLF001
            "INSERT INTO mcp_audit_event(audit_id,principal_id,upstream_id,tool_name,decision,result_class,duration_ms,correlation_id) VALUES ("
            f"{_uuid_literal(str(uuid.uuid4()))},{principal_sql},{upstream_sql},{_text_expr(_bounded_token(tool_name, 160))},"
            f"{_text_expr(_bounded_token(decision, 48))},{_text_expr(_bounded_token(result_class, 64))},{max(0, int(duration_ms))},{correlation_sql});"
        )

    def recent_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        return self._json_lines(
            "SELECT json_build_object('audit_id',audit_id::text,'principal_id',principal_id::text,"
            "'upstream_id',upstream_id::text,'tool_name',tool_name,'decision',decision,'result_class',result_class,"
            "'duration_ms',duration_ms,'correlation_id',correlation_id,'created_at',created_at::text)::text "
            f"FROM mcp_audit_event ORDER BY created_at DESC LIMIT {bounded};"
        )

    def _json_lines(self, sql: str) -> list[dict[str, Any]]:
        text = self.product_store._execute(sql, return_stdout=True)  # noqa: SLF001
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
        return result


def normalize_namespace(value: str) -> str:
    result = str(value).strip().lower()
    if not _NAMESPACE.fullmatch(result):
        raise McpStoreError("invalid MCP namespace")
    return result


def normalize_scopes(scopes: list[str]) -> list[str]:
    if not isinstance(scopes, list) or len(scopes) > 256:
        raise McpStoreError("invalid MCP scopes")
    result: list[str] = []
    for item in scopes:
        value = str(item).strip()
        if not _SCOPE.fullmatch(value):
            raise McpStoreError("invalid MCP scope")
        if value not in result:
            result.append(value)
    return sorted(result)


def scope_allows(scopes: list[str] | tuple[str, ...], tool_name: str) -> bool:
    for scope in scopes:
        if scope == tool_name:
            return True
        if scope.endswith(".*") and tool_name.startswith(scope[:-1]):
            return True
    return False


def normalize_endpoint(value: str) -> str:
    text = str(value).strip()
    if len(text) > 2048:
        raise McpStoreError("MCP endpoint too long")
    parsed = urlparse(text)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise McpStoreError("MCP endpoint must not embed credentials, query parameters, or fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return text
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return text
    raise McpStoreError("MCP upstream endpoint must use HTTPS or loopback HTTP")


def _new_token() -> str:
    return "gmcp_" + secrets.token_urlsafe(32)


def _bounded_name(value: str) -> str:
    text = str(value).strip()
    if not text or "\x00" in text or len(text.encode("utf-8")) > 160:
        raise McpStoreError("invalid MCP name")
    return text


def _bounded_token(value: str, limit: int) -> str:
    text = str(value).strip()
    if not text or "\x00" in text or len(text) > limit:
        raise McpStoreError("invalid MCP metadata token")
    return text


def _principal_select() -> str:
    return (
        "SELECT json_build_object('principal_id',principal_id::text,'name',name,'fingerprint',fingerprint,"
        "'status',status,'scopes',scopes,'created_at',created_at::text,'updated_at',updated_at::text)::text FROM mcp_principal"
    )


def _principal_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "principal_id": str(row["principal_id"]),
        "name": str(row["name"]),
        "fingerprint": str(row["fingerprint"]),
        "status": str(row["status"]),
        "scopes": [str(item) for item in (row.get("scopes") or [])],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _upstream_select() -> str:
    return (
        "SELECT json_build_object('upstream_id',upstream_id::text,'namespace',namespace,'name',name,'endpoint',endpoint,"
        "'secret_id',secret_id::text,'status',status,'last_state',last_state,'last_verified_at',last_verified_at::text,"
        "'created_at',created_at::text,'updated_at',updated_at::text)::text FROM mcp_upstream"
    )


def _upstream_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "upstream_id": str(row["upstream_id"]),
        "namespace": str(row["namespace"]),
        "name": str(row["name"]),
        "endpoint": str(row["endpoint"]),
        "secret_id": str(row["secret_id"]) if row.get("secret_id") else None,
        "status": str(row["status"]),
        "last_state": str(row["last_state"]) if row.get("last_state") else "UNKNOWN",
        "last_verified_at": str(row["last_verified_at"]) if row.get("last_verified_at") else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
