from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .artifact_store import CardArtifactStore, MAX_ATTACHMENT_BYTES
from .auth_service import CredentialService
from .drive_bridge import DRIVE_CONSUMER_SCOPE, DriveNeedsAction, DriveRemoteError, GoogleDriveRemote
from .drive_oauth import GoogleDriveRemoteFactory


REQUEST_NAMES = ("REQUEST.md", "REQUEST.txt")
INBOX_FOLDER = "Inbox"
CARDS_FOLDER = "Cards"
ATTACHMENTS_FOLDER = "Attachments"
OUTPUTS_FOLDER = "Outputs"
INCOMING_FOLDER = "Incoming"
MAX_REQUEST_BYTES = 256 * 1024


class DriveCollaborationError(RuntimeError):
    pass


class CollaborationRemote(Protocol):
    def ensure_folder(self, *, name: str, parent_id: str | None = None) -> str: ...
    def list_children(self, parent_id: str) -> list[dict[str, Any]]: ...
    def read_bytes(self, file_id: str, *, max_bytes: int) -> bytes: ...
    def upsert_text_file(self, *, name: str, parent_id: str, content: str, file_id: str | None = None, mime_type: str = "text/plain; charset=utf-8") -> str: ...
    def upsert_bytes(self, *, name: str, parent_id: str, content: bytes, mime_type: str, file_id: str | None = None) -> str: ...


class KanbanStoreProtocol(Protocol):
    def create_card(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_by_remote(self, *, source_kind: str, source_remote_id: str) -> dict[str, Any] | None: ...
    def require_card(self, card_id: str) -> dict[str, Any]: ...
    def list_events(self, card_id: str, *, limit: int = 200) -> list[dict[str, Any]]: ...
    def list_artifacts(self, card_id: str) -> list[dict[str, Any]]: ...
    def add_artifact(self, **kwargs: Any) -> dict[str, Any]: ...
    def add_event(self, card_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def set_remote_revision(self, card_id: str, *, revision_hash: str, sync_state: str, sync_error: str | None = None) -> dict[str, Any]: ...
    def set_sync_state(self, card_id: str, *, sync_state: str, sync_error: str | None = None) -> dict[str, Any]: ...
    def set_mirror(self, card_id: str, *, folder_id: str, card_file_id: str, status_file_id: str, sync_state: str = "SYNCED") -> dict[str, Any]: ...


class DriveMetadataProtocol(Protocol):
    def get_drive_binding(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class InboxAttachment:
    file_id: str
    name: str
    mime_type: str
    version: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class InboxRequest:
    folder_id: str
    folder_name: str
    request_file_id: str
    request_version: str
    title: str
    description: str
    revision_hash: str
    attachments: tuple[InboxAttachment, ...]


class GoogleDriveCollaborationRemote(GoogleDriveRemote):
    """Drive v3 collaboration operations using the same ephemeral bearer-token boundary."""

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        query = urlparse.urlencode(
            {
                "q": f"'{_drive_q(parent_id)}' in parents and trashed = false",
                "fields": "files(id,name,mimeType,modifiedTime,version,size,md5Checksum)",
                "pageSize": "200",
                "orderBy": "name",
            }
        )
        payload = self._json_request("GET", f"{self.API}/files?{query}")  # noqa: SLF001 - typed provider extension
        files = payload.get("files")
        if not isinstance(files, list):
            raise DriveRemoteError("Drive child listing is invalid")
        result: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = item.get("id")
            name = item.get("name")
            mime = item.get("mimeType")
            if not all(isinstance(value, str) and value for value in (file_id, name, mime)):
                continue
            result.append(
                {
                    "id": file_id,
                    "name": name,
                    "mimeType": mime,
                    "modifiedTime": item.get("modifiedTime"),
                    "version": str(item.get("version") or "0"),
                    "size": int(item.get("size") or 0),
                    "md5Checksum": item.get("md5Checksum"),
                }
            )
        return result

    def read_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        request = urlrequest.Request(
            f"{self.API}/files/{urlparse.quote(file_id)}?alt=media", method="GET",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/octet-stream"},  # noqa: SLF001
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed Google Drive endpoint
                return response.read(max_bytes + 1)
        except urlerror.HTTPError as exc:
            if exc.code in {401, 403}: raise DriveNeedsAction("Drive credential rejected") from exc
            raise DriveRemoteError(f"Drive HTTP status {exc.code}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise DriveRemoteError("Drive network request failed") from exc

    def upsert_bytes(
        self,
        *,
        name: str,
        parent_id: str,
        content: bytes,
        mime_type: str,
        file_id: str | None = None,
    ) -> str:
        target = file_id or self._find(name=name, parent_id=parent_id, mime_type=None)  # noqa: SLF001
        if not target:
            payload = self._json_request(  # noqa: SLF001
                "POST",
                f"{self.API}/files?fields=id",
                {"name": name, "parents": [parent_id], "mimeType": mime_type},
            )
            target = _required_id(payload)
        self._bytes_request(  # noqa: SLF001
            "PATCH",
            f"{self.UPLOAD}/files/{urlparse.quote(target)}?uploadType=media",
            content,
            content_type=mime_type,
        )
        return target


class GoogleDriveCollaborationRemoteFactory:
    def __init__(self, credential_factory: GoogleDriveRemoteFactory | None = None) -> None:
        self.credential_factory = credential_factory or GoogleDriveRemoteFactory()

    def __call__(self, raw_secret: str) -> GoogleDriveCollaborationRemote:
        access_token, _descriptor = self.credential_factory.resolve(raw_secret)
        return GoogleDriveCollaborationRemote(access_token)


class DriveCollaborationService:
    """Explicit inbound Sync + automatic outbound Card projection.

    Local Card/artifact state is authoritative. Remote conflicts and outages are
    recorded as sync evidence and never roll back a committed local mutation.
    """

    def __init__(
        self,
        *,
        metadata: DriveMetadataProtocol,
        credentials: CredentialService,
        cards: KanbanStoreProtocol,
        artifacts: CardArtifactStore,
        remote_factory: Callable[[str], CollaborationRemote] | None = None,
    ) -> None:
        self.metadata = metadata
        self.credentials = credentials
        self.cards = cards
        self.artifacts = artifacts
        self.remote_factory = remote_factory or GoogleDriveCollaborationRemoteFactory()

    def sync_inbox(self) -> dict[str, Any]:
        binding, remote = self._ready_remote()
        kanban_root = _required_text(binding, "kanban_folder_id")
        inbox_id = remote.ensure_folder(name=INBOX_FOLDER, parent_id=kanban_root)
        remote.ensure_folder(name=CARDS_FOLDER, parent_id=kanban_root)
        created = 0
        unchanged = 0
        conflicts = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for item in remote.list_children(inbox_id):
            if item.get("mimeType") != "application/vnd.google-apps.folder":
                continue
            try:
                request = self._load_request(remote, item)
                existing = self.cards.get_by_remote(source_kind="DRIVE_INBOX", source_remote_id=request.folder_id)
                if existing is not None:
                    if existing.get("source_revision_hash") == request.revision_hash:
                        unchanged += 1
                        results.append({"remote_id": request.folder_id, "state": "UNCHANGED", "card_id": existing["card_id"]})
                        continue
                    self.cards.add_event(
                        str(existing["card_id"]),
                        "REMOTE_REVISION_CONFLICT",
                        {"remote_id": request.folder_id, "observed_revision_hash": request.revision_hash},
                    )
                    self.cards.set_sync_state(
                        str(existing["card_id"]), sync_state="CONFLICT", sync_error="REMOTE_REVISION_CHANGED"
                    )
                    conflicts += 1
                    results.append({"remote_id": request.folder_id, "state": "CONFLICT", "card_id": existing["card_id"]})
                    continue
                card = self.cards.create_card(
                    title=request.title,
                    description=request.description,
                    status="BACKLOG",
                    assignee_agent_id="agy-gen",
                    source_kind="DRIVE_INBOX",
                    source_remote_id=request.folder_id,
                    source_revision_hash=request.revision_hash,
                )
                card_id = str(card["card_id"])
                for attachment in request.attachments:
                    written = self.artifacts.ingest(
                        card_id=card_id,
                        name=attachment.name,
                        mime_type=attachment.mime_type,
                        content=attachment.content,
                    )
                    self.cards.add_artifact(
                        card_id=card_id,
                        kind="ATTACHMENT",
                        name=written.name,
                        mime_type=written.mime_type,
                        size_bytes=written.size_bytes,
                        sha256=written.sha256,
                        state=written.state,
                        local_path=written.local_path,
                        remote_file_id=attachment.file_id,
                        quarantine_reason=written.quarantine_reason,
                    )
                self.cards.add_event(
                    card_id,
                    "DRIVE_INBOX_IMPORTED",
                    {
                        "remote_id": request.folder_id,
                        "revision_hash": request.revision_hash,
                        "attachment_count": len(request.attachments),
                    },
                )
                try:
                    self.sync_card(card_id, remote=remote, binding=binding)
                except Exception as exc:
                    self.cards.set_sync_state(card_id, sync_state="RETRY", sync_error=f"OUTBOUND_{type(exc).__name__}")
                created += 1
                results.append({"remote_id": request.folder_id, "state": "CREATED", "card_id": card_id})
            except Exception as exc:
                failed += 1
                results.append({"remote_id": str(item.get("id") or "UNKNOWN"), "state": "FAILED", "reason": type(exc).__name__})
        return {
            "state": "COMPLETED" if failed == 0 else "PARTIAL",
            "authority": "local-product-store",
            "remote_role": "collaboration-replica",
            "created": created,
            "unchanged": unchanged,
            "conflicts": conflicts,
            "failed": failed,
            "items": results,
        }

    def sync_card(
        self,
        card_id: str,
        *,
        remote: CollaborationRemote | None = None,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        card = self.cards.require_card(card_id)
        active_binding = binding
        active_remote = remote
        if active_binding is None or active_remote is None:
            active_binding, active_remote = self._ready_remote()
        kanban_root = _required_text(active_binding, "kanban_folder_id")
        cards_root = active_remote.ensure_folder(name=CARDS_FOLDER, parent_id=kanban_root)
        folder = active_remote.ensure_folder(name=f"CARD-{card_id}", parent_id=cards_root)
        incoming = active_remote.ensure_folder(name=INCOMING_FOLDER, parent_id=folder)
        attachments_folder = active_remote.ensure_folder(name=ATTACHMENTS_FOLDER, parent_id=folder)
        outputs_folder = active_remote.ensure_folder(name=OUTPUTS_FOLDER, parent_id=folder)

        card_text = self._card_markdown(card_id)
        card_file = active_remote.upsert_text_file(
            name="CARD.md",
            parent_id=folder,
            content=card_text,
            file_id=_optional(card.get("mirror_card_file_id")),
            mime_type="text/markdown; charset=utf-8",
        )
        status_text = json.dumps(self._status_projection(card_id), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        status_file = active_remote.upsert_text_file(
            name="STATUS.json",
            parent_id=folder,
            content=status_text,
            file_id=_optional(card.get("mirror_status_file_id")),
            mime_type="application/json; charset=utf-8",
        )
        active_remote.upsert_text_file(
            name="REQUEST.md",
            parent_id=incoming,
            content=f"# {card['title']}\n\n{card.get('description') or ''}\n",
            mime_type="text/markdown; charset=utf-8",
        )
        for artifact in self.cards.list_artifacts(card_id):
            if artifact.get("state") != "ACCEPTED" or not artifact.get("local_path"):
                continue
            content = self.artifacts.read(str(artifact["local_path"]))
            destination = outputs_folder if artifact.get("kind") == "OUTPUT" else attachments_folder
            active_remote.upsert_bytes(
                name=str(artifact["name"]),
                parent_id=destination,
                content=content,
                mime_type=str(artifact["mime_type"]),
            )
        updated = self.cards.set_mirror(
            card_id,
            folder_id=folder,
            card_file_id=card_file,
            status_file_id=status_file,
            sync_state="SYNCED",
        )
        return {"state": "SYNCED", "card": updated, "remote_write": True}

    def project_after_local_change(self, card_id: str) -> dict[str, Any]:
        try:
            return self.sync_card(card_id)
        except Exception as exc:
            self.cards.set_sync_state(card_id, sync_state="RETRY", sync_error=f"OUTBOUND_{type(exc).__name__}")
            return {"state": "RETRY", "remote_write": False, "reason": type(exc).__name__}

    def _load_request(self, remote: CollaborationRemote, folder: dict[str, Any]) -> InboxRequest:
        folder_id = _required_text(folder, "id")
        folder_name = _required_text(folder, "name")
        children = remote.list_children(folder_id)
        request_meta = next((item for item in children if item.get("name") in REQUEST_NAMES), None)
        if request_meta is None:
            raise DriveCollaborationError("Inbox request folder has no REQUEST.md or REQUEST.txt")
        request_file_id = _required_text(request_meta, "id")
        request_content = remote.read_bytes(request_file_id, max_bytes=MAX_REQUEST_BYTES + 1)
        if len(request_content) > MAX_REQUEST_BYTES:
            raise DriveCollaborationError("Inbox request exceeds supported size")
        try:
            request_text = request_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DriveCollaborationError("Inbox request is not UTF-8") from exc
        title, description = _parse_request(request_text, fallback=folder_name)
        attachments_meta = next(
            (
                item
                for item in children
                if item.get("name") == ATTACHMENTS_FOLDER
                and item.get("mimeType") == "application/vnd.google-apps.folder"
            ),
            None,
        )
        attachments: list[InboxAttachment] = []
        if attachments_meta is not None:
            for item in remote.list_children(_required_text(attachments_meta, "id")):
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                file_id = _required_text(item, "id")
                content = remote.read_bytes(file_id, max_bytes=MAX_ATTACHMENT_BYTES + 1)
                mime = str(item.get("mimeType") or "application/octet-stream").split(";", 1)[0].strip().lower()
                attachments.append(
                    InboxAttachment(
                        file_id=file_id,
                        name=_required_text(item, "name"),
                        mime_type=mime,
                        version=str(item.get("version") or "0"),
                        content=content,
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                )
        request_version = str(request_meta.get("version") or "0")
        revision_payload = {
            "folder_id": folder_id,
            "request_id": request_file_id,
            "request_version": request_version,
            "request_sha256": hashlib.sha256(request_content).hexdigest(),
            "attachments": [
                {"id": item.file_id, "version": item.version, "sha256": item.sha256} for item in attachments
            ],
        }
        revision_hash = "sha256:" + hashlib.sha256(
            json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return InboxRequest(
            folder_id=folder_id,
            folder_name=folder_name,
            request_file_id=request_file_id,
            request_version=request_version,
            title=title,
            description=description,
            revision_hash=revision_hash,
            attachments=tuple(attachments),
        )

    def _ready_remote(self) -> tuple[dict[str, Any], CollaborationRemote]:
        binding = self.metadata.get_drive_binding()
        if not binding or binding.get("state") != "READY":
            raise DriveNeedsAction("Drive connection is not READY")
        secret_id = _required_text(binding, "secret_id")
        raw_secret = self.credentials.get_secret_for_consumer(secret_id, consumer=DRIVE_CONSUMER_SCOPE)
        return binding, self.remote_factory(raw_secret)

    def _card_markdown(self, card_id: str) -> str:
        card = self.cards.require_card(card_id)
        events = self.cards.list_events(card_id)
        lines = [
            f"# {card['title']}",
            "",
            f"Card: `{card_id}`",
            f"Status: `{card['status']}`",
            f"Assignee: `{card.get('assignee_agent_id') or 'UNASSIGNED'}`",
            "",
            str(card.get("description") or ""),
            "",
            "## Activity",
            "",
        ]
        for event in events:
            event_type = str(event.get("event_type") or "EVENT")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "COMMENT_ADDED":
                lines.append(f"- Comment: {payload.get('text', '')}")
            elif event_type == "AGENT_RESULT":
                lines.append(f"- Agent result: `{payload.get('state', 'UNKNOWN')}`")
            elif event_type == "STATUS_CHANGED":
                lines.append(f"- Status: `{payload.get('from', '?')}` → `{payload.get('to', '?')}`")
        lines.extend(["", "Drive is a collaboration replica. Local GenOS remains authoritative.", ""])
        return "\n".join(lines)

    def _status_projection(self, card_id: str) -> dict[str, Any]:
        card = self.cards.require_card(card_id)
        return {
            "schema_version": "1.0",
            "card_id": card_id,
            "status": card["status"],
            "assignee_agent_id": card.get("assignee_agent_id"),
            "source_kind": card["source_kind"],
            "last_sync_state": card.get("last_sync_state"),
            "authority": "local-product-store",
            "remote_role": "collaboration-replica",
        }


def _parse_request(text: str, *, fallback: str) -> tuple[str, str]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    title = ""
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        title = stripped.lstrip("#").strip()
        body_start = index + 1
        break
    if not title:
        title = fallback.strip() or "Drive request"
    description = "\n".join(lines[body_start:]).strip()
    if len(title.encode("utf-8")) > 240:
        title = title.encode("utf-8")[:240].decode("utf-8", errors="ignore")
    if len(description.encode("utf-8")) > 64 * 1024:
        description = description.encode("utf-8")[: 64 * 1024].decode("utf-8", errors="ignore")
    return title, description


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DriveCollaborationError(f"Drive collaboration payload is missing {key}")
    return value


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_id(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise DriveRemoteError("Drive object id unavailable")
    return value


def _drive_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
