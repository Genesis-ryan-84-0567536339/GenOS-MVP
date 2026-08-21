from __future__ import annotations

from typing import Any
import json

from .product_store import PostgresProductStore, ProductStoreError, _jsonb_expr, _text_expr, _uuid_literal


class DriveStoreError(RuntimeError):
    pass


_ALLOWED_KEYS = {
    "state",
    "instance_id",
    "secret_id",
    "root_folder_id",
    "reports_folder_id",
    "kanban_folder_id",
    "index_file_id",
    "protocol_file_id",
    "report_markdown_file_id",
    "report_json_file_id",
    "account_email",
    "account_id",
    "protocol_version",
    "schema_version",
    "sync_cursor",
    "last_report_fingerprint",
    "last_verified_at",
    "last_error_code",
    "mcp_grant_state",
    "mcp_grant_agent_id",
    "mcp_grant_scope",
    "mcp_grant_checked_at",
    "updated_at",
}
_RAW_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization_code",
    "raw_secret",
    "api_key",
    "password",
}


class PostgresDriveMetadataStore:
    """Product-authority metadata for the Drive collaboration replica.

    SecretRef identifiers are safe metadata and must remain intact. Raw OAuth
    or client material is rejected by the strict schema before persistence.
    """

    def __init__(self, product_store: PostgresProductStore) -> None:
        self.product_store = product_store

    def ensure_schema(self) -> None:
        self.product_store._execute(  # noqa: SLF001 - same package persistence extension
            """
BEGIN;
CREATE TABLE IF NOT EXISTS drive_binding (
  singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
  instance_id UUID NOT NULL,
  secret_id UUID REFERENCES secret_ref(secret_id),
  state TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO genos_schema_migration(version) VALUES (4) ON CONFLICT (version) DO NOTHING;
COMMIT;
"""
        )

    def get_drive_binding(self) -> dict[str, Any] | None:
        row = self.product_store._json_row(  # noqa: SLF001
            "SELECT json_build_object("
            "'instance_id',instance_id::text,'secret_id',secret_id::text,'state',state,"
            "'metadata',metadata,'updated_at',updated_at::text)::text "
            "FROM drive_binding WHERE singleton=1 LIMIT 1;"
        )
        if row is None:
            return None
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        result = dict(metadata)
        result.update(
            {
                "instance_id": row.get("instance_id"),
                "secret_id": row.get("secret_id"),
                "state": row.get("state"),
                "updated_at": row.get("updated_at"),
            }
        )
        return _clean_payload(result)

    def upsert_drive_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = _clean_payload(payload)
        instance_id = str(clean["instance_id"])
        state = str(clean["state"])
        secret_id = clean.get("secret_id")
        metadata = {key: value for key, value in clean.items() if key not in {"instance_id", "secret_id", "state"}}
        secret_sql = "NULL" if secret_id is None else _uuid_literal(str(secret_id))
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            self.product_store._execute(  # noqa: SLF001
                "INSERT INTO drive_binding(singleton,instance_id,secret_id,state,metadata,updated_at) VALUES ("
                f"1,{_uuid_literal(instance_id)},{secret_sql},{_text_expr(state)},{_jsonb_expr(encoded)},NOW()) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "instance_id=EXCLUDED.instance_id,secret_id=EXCLUDED.secret_id,state=EXCLUDED.state,"
                "metadata=EXCLUDED.metadata,updated_at=NOW();"
            )
        except ProductStoreError as exc:
            raise DriveStoreError("Drive binding metadata persistence failed") from exc
        result = self.get_drive_binding()
        if result is None:
            raise DriveStoreError("Drive binding metadata was not readable after persistence")
        return result


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unexpected = set(payload) - _ALLOWED_KEYS
    if unexpected:
        raise DriveStoreError("Drive binding contains unsupported metadata fields")
    for key in payload:
        if key.lower() in _RAW_SECRET_KEYS:
            raise DriveStoreError("raw credential material cannot be persisted in Drive binding")
    instance_id = payload.get("instance_id")
    state = payload.get("state")
    if not isinstance(instance_id, str) or not instance_id:
        raise DriveStoreError("Drive binding instance_id is required")
    if not isinstance(state, str) or not state or len(state) > 64:
        raise DriveStoreError("Drive binding state is invalid")
    clean = {key: value for key, value in payload.items() if key in _ALLOWED_KEYS}
    serialized = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) > 64 * 1024:
        raise DriveStoreError("Drive binding metadata is too large")
    return clean
