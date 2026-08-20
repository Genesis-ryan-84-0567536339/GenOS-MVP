from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import json
import os
import subprocess
import uuid


class ProductStoreError(RuntimeError):
    pass


class ProductStoreConflict(ProductStoreError):
    pass


class ProductStoreNotFound(ProductStoreError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerRecord:
    owner_id: str
    username: str
    password_salt: bytes
    password_hash: bytes
    created_at: str

    def public_dict(self) -> dict[str, str]:
        return {"owner_id": self.owner_id, "username": self.username, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    secret_id: str
    name: str
    provider: str
    active_revision: int
    status: str
    fingerprint: str
    consumer_scopes: tuple[str, ...]
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "secret_id": self.secret_id,
            "name": self.name,
            "provider": self.provider,
            "active_revision": self.active_revision,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "consumer_scopes": list(self.consumer_scopes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class PostgresProductStore:
    """Product authority metadata store.

    Values are encoded to hexadecimal before being embedded in typed SQL
    expressions. This keeps raw passwords/tokens/secrets out of SQL entirely;
    only hashes, salts, fingerprints and SecretRef metadata reach PostgreSQL.
    """

    def __init__(self, database: str = "genos", psql: str = "/usr/bin/psql") -> None:
        self.database = database
        self.psql = psql

    def ensure_schema(self) -> None:
        self._execute(
            """
BEGIN;
CREATE TABLE IF NOT EXISTS genos_schema_migration (
  version INTEGER PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS owner_account (
  singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
  owner_id UUID NOT NULL UNIQUE,
  username TEXT NOT NULL UNIQUE,
  password_salt BYTEA NOT NULL,
  password_hash BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS owner_session (
  session_id UUID PRIMARY KEY,
  owner_id UUID NOT NULL REFERENCES owner_account(owner_id) ON DELETE CASCADE,
  token_hash BYTEA NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS owner_session_token_hash_idx ON owner_session(token_hash);
CREATE TABLE IF NOT EXISTS secret_ref (
  secret_id UUID PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL,
  active_revision INTEGER NOT NULL CHECK (active_revision >= 1),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','DISABLED')),
  fingerprint TEXT NOT NULL,
  consumer_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS credential_revision (
  secret_id UUID NOT NULL REFERENCES secret_ref(secret_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  fingerprint TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(secret_id, revision)
);
INSERT INTO genos_schema_migration(version) VALUES (3) ON CONFLICT (version) DO NOTHING;
COMMIT;
"""
        )

    def owner_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*)::text FROM owner_account;") or "0")

    def insert_owner(self, owner_id: str, username: str, salt: bytes, password_hash: bytes) -> OwnerRecord:
        owner_uuid = _uuid_literal(owner_id)
        sql = f"""
INSERT INTO owner_account(singleton, owner_id, username, password_salt, password_hash)
VALUES (1, {owner_uuid}, {_text_expr(username)}, {_bytea_expr(salt)}, {_bytea_expr(password_hash)});
"""
        try:
            self._execute(sql)
        except ProductStoreError as exc:
            if self.owner_count() >= 1:
                raise ProductStoreConflict("Owner already exists") from exc
            raise
        record = self.get_owner_by_username(username)
        if record is None:
            raise ProductStoreError("Owner insert succeeded but record was not readable")
        return record

    def get_owner_by_username(self, username: str) -> OwnerRecord | None:
        row = self._json_row(
            "SELECT json_build_object("
            "'owner_id', owner_id::text, 'username', username, "
            "'password_salt', encode(password_salt,'hex'), 'password_hash', encode(password_hash,'hex'), "
            "'created_at', created_at::text)::text FROM owner_account "
            f"WHERE username={_text_expr(username)} LIMIT 1;"
        )
        if row is None:
            return None
        return OwnerRecord(
            owner_id=str(row["owner_id"]),
            username=str(row["username"]),
            password_salt=bytes.fromhex(str(row["password_salt"])),
            password_hash=bytes.fromhex(str(row["password_hash"])),
            created_at=str(row["created_at"]),
        )

    def get_owner_by_id(self, owner_id: str) -> OwnerRecord | None:
        row = self._json_row(
            "SELECT json_build_object("
            "'owner_id', owner_id::text, 'username', username, "
            "'password_salt', encode(password_salt,'hex'), 'password_hash', encode(password_hash,'hex'), "
            "'created_at', created_at::text)::text FROM owner_account "
            f"WHERE owner_id={_uuid_literal(owner_id)} LIMIT 1;"
        )
        if row is None:
            return None
        return OwnerRecord(
            owner_id=str(row["owner_id"]),
            username=str(row["username"]),
            password_salt=bytes.fromhex(str(row["password_salt"])),
            password_hash=bytes.fromhex(str(row["password_hash"])),
            created_at=str(row["created_at"]),
        )

    def create_session(self, session_id: str, owner_id: str, token_hash: bytes, expires_at: datetime) -> None:
        self._execute(
            "INSERT INTO owner_session(session_id, owner_id, token_hash, expires_at) VALUES ("
            f"{_uuid_literal(session_id)}, {_uuid_literal(owner_id)}, {_bytea_expr(token_hash)}, "
            f"{_text_expr(expires_at.isoformat())}::timestamptz);"
        )

    def resolve_session(self, token_hash: bytes) -> OwnerRecord | None:
        row = self._json_row(
            "SELECT json_build_object('owner_id',o.owner_id::text,'username',o.username,"
            "'password_salt',encode(o.password_salt,'hex'),'password_hash',encode(o.password_hash,'hex'),"
            "'created_at',o.created_at::text)::text "
            "FROM owner_session s JOIN owner_account o ON o.owner_id=s.owner_id "
            f"WHERE s.token_hash={_bytea_expr(token_hash)} AND s.revoked_at IS NULL AND s.expires_at > NOW() "
            "ORDER BY s.created_at DESC LIMIT 1;"
        )
        if row is None:
            return None
        return OwnerRecord(
            owner_id=str(row["owner_id"]),
            username=str(row["username"]),
            password_salt=bytes.fromhex(str(row["password_salt"])),
            password_hash=bytes.fromhex(str(row["password_hash"])),
            created_at=str(row["created_at"]),
        )

    def revoke_session(self, token_hash: bytes) -> bool:
        changed = self._scalar(
            "WITH updated AS (UPDATE owner_session SET revoked_at=NOW() "
            f"WHERE token_hash={_bytea_expr(token_hash)} AND revoked_at IS NULL RETURNING 1) "
            "SELECT COUNT(*)::text FROM updated;"
        )
        return int(changed or "0") > 0

    def insert_credential(
        self,
        *,
        secret_id: str,
        name: str,
        provider: str,
        fingerprint: str,
        consumer_scopes: list[str],
        source: str,
    ) -> CredentialRecord:
        scopes = json.dumps(_normalize_scopes(consumer_scopes), separators=(",", ":"))
        try:
            self._execute(
                "BEGIN;"
                "INSERT INTO secret_ref(secret_id,name,provider,active_revision,status,fingerprint,consumer_scopes) VALUES ("
                f"{_uuid_literal(secret_id)}, {_text_expr(name)}, {_text_expr(provider)}, 1, 'ACTIVE', "
                f"{_text_expr(fingerprint)}, {_jsonb_expr(scopes)});"
                "INSERT INTO credential_revision(secret_id,revision,fingerprint,source) VALUES ("
                f"{_uuid_literal(secret_id)},1,{_text_expr(fingerprint)},{_text_expr(source)});"
                "COMMIT;"
            )
        except ProductStoreError as exc:
            if self.get_credential_by_name(name) is not None:
                raise ProductStoreConflict("credential name already exists") from exc
            raise
        record = self.get_credential(secret_id)
        if record is None:
            raise ProductStoreError("credential insert succeeded but record was not readable")
        return record

    def get_credential(self, secret_id: str) -> CredentialRecord | None:
        return self._credential_from_row(
            self._json_row(
                _credential_select() + f" WHERE secret_id={_uuid_literal(secret_id)} LIMIT 1;"
            )
        )

    def get_credential_by_name(self, name: str) -> CredentialRecord | None:
        return self._credential_from_row(
            self._json_row(_credential_select() + f" WHERE name={_text_expr(name)} LIMIT 1;")
        )

    def list_credentials(self) -> list[CredentialRecord]:
        text = self._execute(_credential_select() + " ORDER BY name;", return_stdout=True)
        records: list[CredentialRecord] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            record = self._credential_from_row(row)
            if record is not None:
                records.append(record)
        return records

    def rotate_credential(
        self,
        *,
        secret_id: str,
        expected_revision: int,
        new_revision: int,
        fingerprint: str,
        source: str,
    ) -> CredentialRecord:
        # Update and revision insert share one data-modifying CTE. If the
        # optimistic revision check misses, no revision row is inserted.
        changed = self._scalar(
            "WITH updated AS ("
            "UPDATE secret_ref SET active_revision="
            f"{int(new_revision)}, fingerprint={_text_expr(fingerprint)}, status='ACTIVE', updated_at=NOW() "
            f"WHERE secret_id={_uuid_literal(secret_id)} AND active_revision={int(expected_revision)} "
            "RETURNING secret_id"
            "), inserted AS ("
            "INSERT INTO credential_revision(secret_id,revision,fingerprint,source) "
            f"SELECT secret_id,{int(new_revision)},{_text_expr(fingerprint)},{_text_expr(source)} FROM updated "
            "RETURNING 1"
            ") SELECT COUNT(*)::text FROM inserted;"
        )
        if int(changed or "0") != 1:
            raise ProductStoreConflict("credential revision changed concurrently")
        record = self.get_credential(secret_id)
        if record is None:
            raise ProductStoreNotFound("credential not found after rotation")
        return record

    def disable_credential(self, secret_id: str) -> CredentialRecord:
        changed = self._scalar(
            "WITH updated AS (UPDATE secret_ref SET status='DISABLED', updated_at=NOW() "
            f"WHERE secret_id={_uuid_literal(secret_id)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            raise ProductStoreNotFound("credential not found")
        record = self.get_credential(secret_id)
        if record is None:
            raise ProductStoreNotFound("credential not found")
        return record

    def _credential_from_row(self, row: dict[str, Any] | None) -> CredentialRecord | None:
        if row is None:
            return None
        scopes = row.get("consumer_scopes") or []
        return CredentialRecord(
            secret_id=str(row["secret_id"]),
            name=str(row["name"]),
            provider=str(row["provider"]),
            active_revision=int(row["active_revision"]),
            status=str(row["status"]),
            fingerprint=str(row["fingerprint"]),
            consumer_scopes=tuple(str(item) for item in scopes),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _json_row(self, sql: str) -> dict[str, Any] | None:
        value = self._scalar(sql)
        if value is None or value == "":
            return None
        return json.loads(value)

    def _scalar(self, sql: str) -> str | None:
        output = self._execute(sql, return_stdout=True)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else None

    def _execute(self, sql: str, *, return_stdout: bool = False) -> str:
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PGAPPNAME": "genos-product-api",
        }
        try:
            completed = subprocess.run(
                [self.psql, "-X", "-v", "ON_ERROR_STOP=1", "-d", self.database, "-Atq"],
                input=sql,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                shell=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProductStoreError("Product DB command unavailable") from exc
        if completed.returncode != 0:
            # Never bubble SQL/stdout/stderr: caller data is represented as
            # encoded literals and operational DB messages do not belong in
            # public API/log surfaces.
            raise ProductStoreError("Product DB operation failed")
        return completed.stdout if return_stdout else ""


def _credential_select() -> str:
    return (
        "SELECT json_build_object('secret_id',secret_id::text,'name',name,'provider',provider,"
        "'active_revision',active_revision,'status',status,'fingerprint',fingerprint,"
        "'consumer_scopes',consumer_scopes,'created_at',created_at::text,'updated_at',updated_at::text)::text "
        "FROM secret_ref"
    )


def _normalize_scopes(scopes: list[str]) -> list[str]:
    result: list[str] = []
    for item in scopes:
        value = str(item).strip()
        if not value or len(value) > 128:
            raise ProductStoreError("invalid consumer scope")
        if value not in result:
            result.append(value)
    if len(result) > 64:
        raise ProductStoreError("too many consumer scopes")
    return result


def _uuid_literal(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ProductStoreError("invalid UUID") from exc
    return f"'{normalized}'::uuid"


def _text_expr(value: str) -> str:
    return f"convert_from(decode('{value.encode('utf-8').hex()}','hex'),'UTF8')"


def _bytea_expr(value: bytes) -> str:
    return f"decode('{value.hex()}','hex')"


def _jsonb_expr(json_text: str) -> str:
    return _text_expr(json_text) + "::jsonb"
