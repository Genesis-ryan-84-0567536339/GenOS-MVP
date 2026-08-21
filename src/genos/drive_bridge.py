from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Callable, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid

from .auth_service import CredentialService
from .redaction import redact


DRIVE_CONSUMER_SCOPE = "drive-sync"
DRIVE_PROTOCOL_VERSION = "1.0"
DRIVE_SCHEMA_VERSION = "1.0"
DEFAULT_ROOT_NAME = "GenOS"


class DriveBridgeError(RuntimeError):
    pass


class DriveNeedsAction(DriveBridgeError):
    pass


class DriveRemoteError(DriveBridgeError):
    pass


class DriveRemote(Protocol):
    def account_identity(self) -> dict[str, str | None]: ...

    def ensure_folder(self, *, name: str, parent_id: str | None = None) -> str: ...

    def upsert_text_file(
        self,
        *,
        name: str,
        parent_id: str,
        content: str,
        file_id: str | None = None,
        mime_type: str = "text/plain; charset=utf-8",
    ) -> str: ...

    def read_text_file(self, file_id: str) -> str: ...


class DriveMetadataStore(Protocol):
    def get_drive_binding(self) -> dict[str, Any] | None: ...

    def upsert_drive_binding(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DriveConnectionProjection:
    state: str
    instance_id: str
    secret_id: str | None
    root_folder_id: str | None
    reports_folder_id: str | None
    kanban_folder_id: str | None
    index_file_id: str | None
    protocol_file_id: str | None
    account_email: str | None
    account_id: str | None
    last_verified_at: str | None
    last_error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "instance_id": self.instance_id,
            "secret_id": self.secret_id,
            "root_folder_id": self.root_folder_id,
            "reports_folder_id": self.reports_folder_id,
            "kanban_folder_id": self.kanban_folder_id,
            "index_file_id": self.index_file_id,
            "protocol_file_id": self.protocol_file_id,
            "account_email": self.account_email,
            "account_id": self.account_id,
            "protocol_version": DRIVE_PROTOCOL_VERSION,
            "schema_version": DRIVE_SCHEMA_VERSION,
            "last_verified_at": self.last_verified_at,
            "last_error_code": self.last_error_code,
            "authority": "local-product-store",
            "remote_role": "collaboration-replica",
        }


class DriveConnectionService:
    """Resumable typed Drive setup without making Drive a product authority."""

    def __init__(
        self,
        *,
        store: DriveMetadataStore,
        credentials: CredentialService,
        remote_factory: Callable[[str], DriveRemote],
        instance_id: str,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.remote_factory = remote_factory
        self.instance_id = _normalize_uuid(instance_id)

    def status(self) -> dict[str, Any]:
        current = self.store.get_drive_binding()
        if current is None:
            return DriveConnectionProjection(
                state="UNCONFIGURED",
                instance_id=self.instance_id,
                secret_id=None,
                root_folder_id=None,
                reports_folder_id=None,
                kanban_folder_id=None,
                index_file_id=None,
                protocol_file_id=None,
                account_email=None,
                account_id=None,
                last_verified_at=None,
                last_error_code=None,
            ).to_dict()
        return redact(dict(current))

    def connect(self, *, secret_id: str, root_name: str = DEFAULT_ROOT_NAME) -> dict[str, Any]:
        root_name = _normalize_name(root_name)
        current = self.store.get_drive_binding() or {}
        existing_instance = current.get("instance_id")
        if existing_instance and str(existing_instance) != self.instance_id:
            raise DriveNeedsAction("Drive binding belongs to another GenOS instance")
        existing_secret = current.get("secret_id")
        if existing_secret and str(existing_secret) != secret_id and current.get("state") == "READY":
            raise DriveNeedsAction("Drive credential change requires explicit rebind")

        checkpoint = self._checkpoint(
            current,
            state="NEEDS_AUTH",
            secret_id=secret_id,
            last_error_code=None,
        )
        try:
            raw_access_token = self.credentials.get_secret_for_consumer(secret_id, consumer=DRIVE_CONSUMER_SCOPE)
            remote = self.remote_factory(raw_access_token)
            identity = remote.account_identity()
            checkpoint = self._checkpoint(
                checkpoint,
                state="AUTHENTICATED",
                account_email=_safe_identity(identity.get("email")),
                account_id=_safe_identity(identity.get("permission_id") or identity.get("account_id")),
            )

            root_id = remote.ensure_folder(name=root_name)
            checkpoint = self._checkpoint(checkpoint, state="FOLDER_BOUND", root_folder_id=root_id)

            reports_id = remote.ensure_folder(name="Reports", parent_id=root_id)
            kanban_id = remote.ensure_folder(name="Kanban", parent_id=root_id)
            protocol_text = self._protocol_json(root_id=root_id)
            protocol_id = remote.upsert_text_file(
                name="PROTOCOL.json",
                parent_id=root_id,
                content=protocol_text,
                file_id=_optional_text(checkpoint.get("protocol_file_id")),
                mime_type="application/json; charset=utf-8",
            )
            index_text = self._index_markdown()
            index_id = remote.upsert_text_file(
                name="INDEX.md",
                parent_id=root_id,
                content=index_text,
                file_id=_optional_text(checkpoint.get("index_file_id")),
                mime_type="text/markdown; charset=utf-8",
            )
            checkpoint = self._checkpoint(
                checkpoint,
                state="WRITE_VERIFIED",
                reports_folder_id=reports_id,
                kanban_folder_id=kanban_id,
                protocol_file_id=protocol_id,
                index_file_id=index_id,
            )

            protocol_readback = remote.read_text_file(protocol_id)
            index_readback = remote.read_text_file(index_id)
            if protocol_readback != protocol_text or index_readback != index_text:
                raise DriveRemoteError("Drive write/read verification mismatch")
            checkpoint = self._checkpoint(checkpoint, state="READ_VERIFIED")

            decoded = json.loads(protocol_readback)
            if not isinstance(decoded, dict) or decoded.get("instance_id") != self.instance_id:
                raise DriveRemoteError("Drive protocol instance binding mismatch")
            checkpoint = self._checkpoint(checkpoint, state="INSTANCE_BOUND")
            return self._checkpoint(
                checkpoint,
                state="READY",
                last_verified_at=_utc_now(),
                last_error_code=None,
            )
        except DriveNeedsAction:
            raise
        except Exception as exc:
            error_code = _error_code(exc)
            state = "NEEDS_ACTION" if error_code in {"AUTH_REJECTED", "CREDENTIAL_UNAVAILABLE"} else "DEGRADED"
            self._checkpoint(checkpoint, state=state, last_error_code=error_code)
            raise DriveNeedsAction("Drive connection requires action") from exc

    def verify(self) -> dict[str, Any]:
        current = self.store.get_drive_binding()
        if not current or current.get("state") != "READY":
            raise DriveNeedsAction("Drive connection is not ready")
        secret_id = _required_text(current, "secret_id")
        protocol_id = _required_text(current, "protocol_file_id")
        raw_access_token = self.credentials.get_secret_for_consumer(secret_id, consumer=DRIVE_CONSUMER_SCOPE)
        remote = self.remote_factory(raw_access_token)
        try:
            identity = remote.account_identity()
            protocol = json.loads(remote.read_text_file(protocol_id))
            if not isinstance(protocol, dict) or protocol.get("instance_id") != self.instance_id:
                raise DriveRemoteError("Drive binding verification failed")
            return self._checkpoint(
                current,
                state="READY",
                account_email=_safe_identity(identity.get("email")),
                account_id=_safe_identity(identity.get("permission_id") or identity.get("account_id")),
                last_verified_at=_utc_now(),
                last_error_code=None,
            )
        except Exception as exc:
            error_code = _error_code(exc)
            self._checkpoint(current, state="DEGRADED", last_error_code=error_code)
            raise DriveNeedsAction("Drive verification requires action") from exc

    def _checkpoint(self, base: dict[str, Any], **changes: Any) -> dict[str, Any]:
        payload = {
            "state": str(base.get("state") or "UNCONFIGURED"),
            "instance_id": self.instance_id,
            "secret_id": _optional_text(base.get("secret_id")),
            "root_folder_id": _optional_text(base.get("root_folder_id")),
            "reports_folder_id": _optional_text(base.get("reports_folder_id")),
            "kanban_folder_id": _optional_text(base.get("kanban_folder_id")),
            "index_file_id": _optional_text(base.get("index_file_id")),
            "protocol_file_id": _optional_text(base.get("protocol_file_id")),
            "account_email": _optional_text(base.get("account_email")),
            "account_id": _optional_text(base.get("account_id")),
            "protocol_version": DRIVE_PROTOCOL_VERSION,
            "schema_version": DRIVE_SCHEMA_VERSION,
            "sync_cursor": _optional_text(base.get("sync_cursor")),
            "last_report_fingerprint": _optional_text(base.get("last_report_fingerprint")),
            "last_verified_at": _optional_text(base.get("last_verified_at")),
            "last_error_code": _optional_text(base.get("last_error_code")),
            "updated_at": _utc_now(),
        }
        payload.update(changes)
        return redact(self.store.upsert_drive_binding(payload))

    def _protocol_json(self, *, root_id: str) -> str:
        payload = {
            "protocol_version": DRIVE_PROTOCOL_VERSION,
            "schema_version": DRIVE_SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "root_folder_id": root_id,
            "authority": {"product": "local", "drive": "collaboration-replica"},
            "ownership": {
                "INDEX.md": "genos",
                "PROTOCOL.json": "genos",
                "Reports/": "genos",
                "Kanban/": "shared-protocol-mvp07",
            },
            "supported_operations": ["bootstrap", "report-read", "report-write", "kanban-reserved-mvp07"],
            "compatibility": {"min_genos_mvp": "MVP-06"},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def _index_markdown(self) -> str:
        return (
            "# GenOS Collaboration Root\n\n"
            f"Instance: `{self.instance_id}`\n\n"
            "This Drive tree is a collaboration/report replica. Local GenOS remains the product/data authority.\n\n"
            "- `Reports/` — sanitized system reports\n"
            "- `Kanban/` — collaboration bridge reserved for MVP-07\n"
            "- `PROTOCOL.json` — protocol and instance binding metadata\n"
        )


class GoogleDriveRemote:
    """Small stdlib Google Drive v3 adapter using a caller-supplied OAuth access token."""

    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, access_token: str, *, timeout: float = 20.0) -> None:
        token = access_token.strip()
        if not token or len(token) > 16384:
            raise DriveNeedsAction("Drive access token is unavailable")
        self._token = token
        self.timeout = timeout

    def account_identity(self) -> dict[str, str | None]:
        payload = self._json_request("GET", f"{self.API}/about?fields=user")
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            raise DriveRemoteError("Drive account identity unavailable")
        return {
            "email": _safe_identity(user.get("emailAddress")),
            "permission_id": _safe_identity(user.get("permissionId")),
        }

    def ensure_folder(self, *, name: str, parent_id: str | None = None) -> str:
        found = self._find(name=name, parent_id=parent_id, mime_type="application/vnd.google-apps.folder")
        if found:
            return found
        metadata: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            metadata["parents"] = [parent_id]
        payload = self._json_request("POST", f"{self.API}/files?fields=id", metadata)
        return _id_from(payload)

    def upsert_text_file(
        self,
        *,
        name: str,
        parent_id: str,
        content: str,
        file_id: str | None = None,
        mime_type: str = "text/plain; charset=utf-8",
    ) -> str:
        target = file_id or self._find(name=name, parent_id=parent_id, mime_type=None)
        if not target:
            metadata = {"name": name, "parents": [parent_id], "mimeType": "text/plain"}
            payload = self._json_request("POST", f"{self.API}/files?fields=id", metadata)
            target = _id_from(payload)
        self._bytes_request(
            "PATCH",
            f"{self.UPLOAD}/files/{urlparse.quote(target)}?uploadType=media",
            content.encode("utf-8"),
            content_type=mime_type,
        )
        return target

    def read_text_file(self, file_id: str) -> str:
        data = self._bytes_request("GET", f"{self.API}/files/{urlparse.quote(file_id)}?alt=media")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DriveRemoteError("Drive text file is not UTF-8") from exc

    def _find(self, *, name: str, parent_id: str | None, mime_type: str | None) -> str | None:
        q = [f"name = '{_drive_q(name)}'", "trashed = false"]
        if parent_id:
            q.append(f"'{_drive_q(parent_id)}' in parents")
        if mime_type:
            q.append(f"mimeType = '{_drive_q(mime_type)}'")
        query = urlparse.urlencode({"q": " and ".join(q), "fields": "files(id,name,mimeType)", "pageSize": "2"})
        payload = self._json_request("GET", f"{self.API}/files?{query}")
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            return None
        if len(files) > 1:
            raise DriveNeedsAction("duplicate Drive artifact requires explicit resolution")
        item = files[0]
        return _id_from(item) if isinstance(item, dict) else None

    def _json_request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        raw = self._bytes_request(method, url, data, content_type="application/json")
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriveRemoteError("Drive API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise DriveRemoteError("Drive API response must be an object")
        return decoded

    def _bytes_request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        *,
        content_type: str | None = None,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urlrequest.Request(url, data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                return response.read(4 * 1024 * 1024)
        except urlerror.HTTPError as exc:
            if exc.code in {401, 403}:
                raise DriveNeedsAction("Drive credential rejected") from exc
            raise DriveRemoteError(f"Drive HTTP status {exc.code}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise DriveRemoteError("Drive network request failed") from exc


def _normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise DriveBridgeError("invalid GenOS instance id") from exc


def _normalize_name(value: str) -> str:
    result = value.strip()
    if not result or len(result) > 128 or any(char in result for char in "\r\n\x00"):
        raise DriveBridgeError("invalid Drive root name")
    return result


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DriveNeedsAction(f"Drive binding is missing {key}")
    return value


def _optional_text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _safe_identity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:320] if text else None


def _id_from(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value or len(value) > 512:
        raise DriveRemoteError("Drive object id unavailable")
    return value


def _drive_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _error_code(exc: Exception) -> str:
    if isinstance(exc, DriveNeedsAction):
        return "AUTH_REJECTED" if "credential" in str(exc).lower() else "NEEDS_ACTION"
    name = type(exc).__name__.upper()
    if "CREDENTIAL" in name or "SECRET" in name:
        return "CREDENTIAL_UNAVAILABLE"
    if isinstance(exc, DriveRemoteError):
        return "DRIVE_REMOTE_ERROR"
    return "DRIVE_OPERATION_FAILED"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
