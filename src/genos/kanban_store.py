from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import uuid

from .product_store import PostgresProductStore, ProductStoreError, _jsonb_expr, _text_expr, _uuid_literal


CARD_STATES = (
    "BACKLOG",
    "PROCESS",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "VERIFY",
    "DONE",
    "FAILED",
    "CANCELLED",
)


class KanbanStoreError(RuntimeError):
    pass


class CardNotFound(KanbanStoreError):
    pass


class CardConflict(KanbanStoreError):
    pass


@dataclass(frozen=True, slots=True)
class CardRecord:
    card_id: str
    title: str
    description: str
    status: str
    assignee_agent_id: str | None
    source_kind: str
    source_remote_id: str | None
    source_revision_hash: str | None
    agent_task_id: str | None
    mirror_folder_id: str | None
    mirror_card_file_id: str | None
    mirror_status_file_id: str | None
    last_sync_state: str | None
    last_sync_error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "assignee_agent_id": self.assignee_agent_id,
            "source_kind": self.source_kind,
            "source_remote_id": self.source_remote_id,
            "source_revision_hash": self.source_revision_hash,
            "agent_task_id": self.agent_task_id,
            "mirror_folder_id": self.mirror_folder_id,
            "mirror_card_file_id": self.mirror_card_file_id,
            "mirror_status_file_id": self.mirror_status_file_id,
            "last_sync_state": self.last_sync_state,
            "last_sync_error": self.last_sync_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "authority": "local-product-store",
        }


class PostgresKanbanStore:
    """PostgreSQL authority for MVP-07 Cards, events and artifact metadata."""

    def __init__(self, product_store: PostgresProductStore) -> None:
        self.product_store = product_store

    def ensure_schema(self) -> None:
        states = ",".join(_text_expr(item) for item in CARD_STATES)
        self.product_store._execute(  # noqa: SLF001 - package persistence extension
            f"""
BEGIN;
CREATE TABLE IF NOT EXISTS card (
  card_id UUID PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ({states})),
  assignee_agent_id TEXT,
  source_kind TEXT NOT NULL,
  source_remote_id TEXT,
  source_revision_hash TEXT,
  agent_task_id UUID,
  mirror_folder_id TEXT,
  mirror_card_file_id TEXT,
  mirror_status_file_id TEXT,
  last_sync_state TEXT,
  last_sync_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS card_remote_identity_unique
  ON card(source_kind, source_remote_id) WHERE source_remote_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS card_status_created_idx ON card(status, created_at);
CREATE TABLE IF NOT EXISTS card_event (
  event_id UUID PRIMARY KEY,
  card_id UUID NOT NULL REFERENCES card(card_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS card_event_card_created_idx ON card_event(card_id, created_at);
CREATE TABLE IF NOT EXISTS card_artifact (
  artifact_id UUID PRIMARY KEY,
  card_id UUID NOT NULL REFERENCES card(card_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
  sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('ACCEPTED','QUARANTINED')),
  local_path TEXT,
  remote_file_id TEXT,
  quarantine_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS card_artifact_card_created_idx ON card_artifact(card_id, created_at);
INSERT INTO genos_schema_migration(version) VALUES (5) ON CONFLICT (version) DO NOTHING;
COMMIT;
"""
        )

    def create_card(
        self,
        *,
        title: str,
        description: str = "",
        status: str = "BACKLOG",
        assignee_agent_id: str | None = "agy-gen",
        source_kind: str = "LOCAL",
        source_remote_id: str | None = None,
        source_revision_hash: str | None = None,
    ) -> dict[str, Any]:
        card_id = str(uuid.uuid4())
        _validate_card_text(title, description)
        _validate_state(status)
        try:
            self.product_store._execute(  # noqa: SLF001
                "INSERT INTO card(card_id,title,description,status,assignee_agent_id,source_kind,source_remote_id,source_revision_hash) VALUES ("
                f"{_uuid_literal(card_id)},{_text_expr(title.strip())},{_text_expr(description)},"
                f"{_text_expr(status)},{_nullable_text(assignee_agent_id)},{_text_expr(_safe_token(source_kind, 64))},"
                f"{_nullable_text(source_remote_id)},{_nullable_text(source_revision_hash)});"
            )
        except ProductStoreError as exc:
            if source_remote_id and self.get_by_remote(source_kind=source_kind, source_remote_id=source_remote_id):
                raise CardConflict("remote request is already bound to a Card") from exc
            raise KanbanStoreError("Card creation failed") from exc
        self.add_event(card_id, "CARD_CREATED", {"source_kind": source_kind, "status": status})
        return self.require_card(card_id)

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        return _card_from_row(self.product_store._json_row(_card_select() + f" WHERE card_id={_uuid_literal(card_id)} LIMIT 1;"))  # noqa: SLF001

    def require_card(self, card_id: str) -> dict[str, Any]:
        card = self.get_card(card_id)
        if card is None:
            raise CardNotFound("Card not found")
        return card

    def get_by_remote(self, *, source_kind: str, source_remote_id: str) -> dict[str, Any] | None:
        return _card_from_row(
            self.product_store._json_row(  # noqa: SLF001
                _card_select()
                + f" WHERE source_kind={_text_expr(source_kind)} AND source_remote_id={_text_expr(source_remote_id)} LIMIT 1;"
            )
        )

    def list_cards(self, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        where = ""
        if status is not None:
            _validate_state(status)
            where = f" WHERE status={_text_expr(status)}"
        text = self.product_store._execute(  # noqa: SLF001
            _card_select() + where + f" ORDER BY created_at, card_id LIMIT {bounded};",
            return_stdout=True,
        )
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            card = _card_from_row(value)
            if card is not None:
                result.append(card)
        return result

    def transition(self, card_id: str, *, expected_state: str, new_state: str, reason: str) -> dict[str, Any]:
        _validate_state(expected_state)
        _validate_state(new_state)
        changed = self.product_store._scalar(  # noqa: SLF001
            "WITH updated AS (UPDATE card SET "
            f"status={_text_expr(new_state)},updated_at=NOW() WHERE card_id={_uuid_literal(card_id)} "
            f"AND status={_text_expr(expected_state)} RETURNING 1) SELECT COUNT(*)::text FROM updated;"
        )
        if int(changed or "0") != 1:
            current = self.get_card(card_id)
            if current is None:
                raise CardNotFound("Card not found")
            raise CardConflict("Card state changed concurrently")
        self.add_event(card_id, "STATUS_CHANGED", {"from": expected_state, "to": new_state, "reason": _safe_text(reason, 512)})
        return self.require_card(card_id)

    def set_agent_task(self, card_id: str, *, task_id: str | None) -> dict[str, Any]:
        task_sql = "NULL" if task_id is None else _uuid_literal(task_id)
        self.product_store._execute(  # noqa: SLF001
            f"UPDATE card SET agent_task_id={task_sql},updated_at=NOW() WHERE card_id={_uuid_literal(card_id)};"
        )
        return self.require_card(card_id)

    def set_remote_revision(self, card_id: str, *, revision_hash: str, sync_state: str, sync_error: str | None = None) -> dict[str, Any]:
        self.product_store._execute(  # noqa: SLF001
            "UPDATE card SET "
            f"source_revision_hash={_text_expr(_safe_token(revision_hash, 160))},"
            f"last_sync_state={_text_expr(_safe_token(sync_state, 64))},last_sync_error={_nullable_text(sync_error)},updated_at=NOW() "
            f"WHERE card_id={_uuid_literal(card_id)};"
        )
        return self.require_card(card_id)

    def set_sync_state(self, card_id: str, *, sync_state: str, sync_error: str | None = None) -> dict[str, Any]:
        self.product_store._execute(  # noqa: SLF001
            "UPDATE card SET "
            f"last_sync_state={_text_expr(_safe_token(sync_state, 64))},last_sync_error={_nullable_text(sync_error)},updated_at=NOW() "
            f"WHERE card_id={_uuid_literal(card_id)};"
        )
        return self.require_card(card_id)

    def set_mirror(
        self,
        card_id: str,
        *,
        folder_id: str,
        card_file_id: str,
        status_file_id: str,
        sync_state: str = "SYNCED",
    ) -> dict[str, Any]:
        self.product_store._execute(  # noqa: SLF001
            "UPDATE card SET "
            f"mirror_folder_id={_text_expr(_safe_token(folder_id, 512))},"
            f"mirror_card_file_id={_text_expr(_safe_token(card_file_id, 512))},"
            f"mirror_status_file_id={_text_expr(_safe_token(status_file_id, 512))},"
            f"last_sync_state={_text_expr(_safe_token(sync_state, 64))},last_sync_error=NULL,updated_at=NOW() "
            f"WHERE card_id={_uuid_literal(card_id)};"
        )
        return self.require_card(card_id)

    def add_event(self, card_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_card(card_id)
        event_id = str(uuid.uuid4())
        clean = _clean_json(payload)
        self.product_store._execute(  # noqa: SLF001
            "INSERT INTO card_event(event_id,card_id,event_type,payload) VALUES ("
            f"{_uuid_literal(event_id)},{_uuid_literal(card_id)},{_text_expr(_safe_token(event_type, 64))},"
            f"{_jsonb_expr(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(',', ':')))});"
        )
        return {"event_id": event_id, "card_id": card_id, "event_type": event_type, "payload": clean}

    def list_events(self, card_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        self.require_card(card_id)
        bounded = max(1, min(int(limit), 500))
        text = self.product_store._execute(  # noqa: SLF001
            "SELECT json_build_object('event_id',event_id::text,'card_id',card_id::text,'event_type',event_type,"
            "'payload',payload,'created_at',created_at::text)::text FROM card_event "
            f"WHERE card_id={_uuid_literal(card_id)} ORDER BY created_at,event_id LIMIT {bounded};",
            return_stdout=True,
        )
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def add_artifact(
        self,
        *,
        card_id: str,
        kind: str,
        name: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        state: str,
        local_path: str | None,
        remote_file_id: str | None,
        quarantine_reason: str | None = None,
    ) -> dict[str, Any]:
        self.require_card(card_id)
        if state not in {"ACCEPTED", "QUARANTINED"}:
            raise KanbanStoreError("invalid artifact state")
        artifact_id = str(uuid.uuid4())
        self.product_store._execute(  # noqa: SLF001
            "INSERT INTO card_artifact(artifact_id,card_id,kind,name,mime_type,size_bytes,sha256,state,local_path,remote_file_id,quarantine_reason) VALUES ("
            f"{_uuid_literal(artifact_id)},{_uuid_literal(card_id)},{_text_expr(_safe_token(kind, 64))},"
            f"{_text_expr(_safe_text(name, 255))},{_text_expr(_safe_text(mime_type, 128))},{int(size_bytes)},"
            f"{_text_expr(_safe_token(sha256, 128))},{_text_expr(state)},{_nullable_text(local_path)},"
            f"{_nullable_text(remote_file_id)},{_nullable_text(quarantine_reason)});"
        )
        return {
            "artifact_id": artifact_id,
            "card_id": card_id,
            "kind": kind,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": int(size_bytes),
            "sha256": sha256,
            "state": state,
            "local_path": local_path,
            "remote_file_id": remote_file_id,
            "quarantine_reason": quarantine_reason,
        }

    def list_artifacts(self, card_id: str) -> list[dict[str, Any]]:
        self.require_card(card_id)
        text = self.product_store._execute(  # noqa: SLF001
            "SELECT json_build_object('artifact_id',artifact_id::text,'card_id',card_id::text,'kind',kind,'name',name,"
            "'mime_type',mime_type,'size_bytes',size_bytes,'sha256',sha256,'state',state,'local_path',local_path,"
            "'remote_file_id',remote_file_id,'quarantine_reason',quarantine_reason,'created_at',created_at::text)::text "
            f"FROM card_artifact WHERE card_id={_uuid_literal(card_id)} ORDER BY created_at,artifact_id;",
            return_stdout=True,
        )
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _card_select() -> str:
    return (
        "SELECT json_build_object('card_id',card_id::text,'title',title,'description',description,'status',status,"
        "'assignee_agent_id',assignee_agent_id,'source_kind',source_kind,'source_remote_id',source_remote_id,"
        "'source_revision_hash',source_revision_hash,'agent_task_id',agent_task_id::text,'mirror_folder_id',mirror_folder_id,"
        "'mirror_card_file_id',mirror_card_file_id,'mirror_status_file_id',mirror_status_file_id,"
        "'last_sync_state',last_sync_state,'last_sync_error',last_sync_error,'created_at',created_at::text,'updated_at',updated_at::text)::text FROM card"
    )


def _card_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return CardRecord(
        card_id=str(row["card_id"]),
        title=str(row["title"]),
        description=str(row.get("description") or ""),
        status=str(row["status"]),
        assignee_agent_id=_optional(row.get("assignee_agent_id")),
        source_kind=str(row["source_kind"]),
        source_remote_id=_optional(row.get("source_remote_id")),
        source_revision_hash=_optional(row.get("source_revision_hash")),
        agent_task_id=_optional(row.get("agent_task_id")),
        mirror_folder_id=_optional(row.get("mirror_folder_id")),
        mirror_card_file_id=_optional(row.get("mirror_card_file_id")),
        mirror_status_file_id=_optional(row.get("mirror_status_file_id")),
        last_sync_state=_optional(row.get("last_sync_state")),
        last_sync_error=_optional(row.get("last_sync_error")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    ).to_dict()


def _validate_card_text(title: str, description: str) -> None:
    _safe_text(title, 240)
    _safe_text(description, 64 * 1024, allow_empty=True)
    if not title.strip():
        raise KanbanStoreError("Card title is required")


def _validate_state(state: str) -> None:
    if state not in CARD_STATES:
        raise KanbanStoreError("invalid Card state")


def _safe_token(value: str, limit: int) -> str:
    text = str(value).strip()
    if not text or len(text) > limit or any(ord(char) < 0x20 for char in text):
        raise KanbanStoreError("invalid token field")
    return text


def _safe_text(value: str, limit: int, *, allow_empty: bool = False) -> str:
    text = str(value)
    if len(text.encode("utf-8")) > limit or "\x00" in text:
        raise KanbanStoreError("text field exceeds supported bounds")
    if not allow_empty and not text.strip():
        raise KanbanStoreError("text field is required")
    return text


def _nullable_text(value: Any) -> str:
    if value is None:
        return "NULL"
    return _text_expr(_safe_text(str(value), 4096))


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean_json(payload: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lowered = serialized.lower()
    for forbidden in ("access_token", "refresh_token", "client_secret", "raw_secret", "password"):
        if forbidden in lowered:
            raise KanbanStoreError("event payload contains forbidden credential field")
    if len(serialized.encode("utf-8")) > 32 * 1024:
        raise KanbanStoreError("event payload is too large")
    return json.loads(serialized)
