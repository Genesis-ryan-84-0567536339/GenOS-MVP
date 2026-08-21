from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/json",
        "application/pdf",
        "image/png",
        "image/jpeg",
    }
)


class ArtifactStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    artifact_id: str
    state: str
    name: str
    mime_type: str
    size_bytes: int
    sha256: str
    local_path: str | None
    quarantine_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "state": self.state,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "local_path": self.local_path,
            "quarantine_reason": self.quarantine_reason,
        }


class CardArtifactStore:
    """Local durable Card artifact authority with a separate quarantine lane."""

    def __init__(self, root: Path | str = "/var/lib/genos/artifacts") -> None:
        self.root = Path(root)

    def ingest(self, *, card_id: str, name: str, mime_type: str, content: bytes) -> ArtifactWrite:
        card = _uuid(card_id)
        clean_name = _safe_name(name)
        clean_mime = _safe_mime(mime_type)
        size = len(content)
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = str(uuid.uuid4())
        if size > MAX_ATTACHMENT_BYTES:
            return ArtifactWrite(
                artifact_id=artifact_id,
                state="QUARANTINED",
                name=clean_name,
                mime_type=clean_mime,
                size_bytes=size,
                sha256=digest,
                local_path=None,
                quarantine_reason="SIZE_LIMIT_EXCEEDED",
            )
        if clean_mime not in ALLOWED_ATTACHMENT_MIME_TYPES:
            quarantine = self.root / "quarantine" / card
            quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(quarantine, 0o700)
            target = quarantine / f"{artifact_id}.bin"
            self._atomic_bytes(target, content)
            return ArtifactWrite(
                artifact_id=artifact_id,
                state="QUARANTINED",
                name=clean_name,
                mime_type=clean_mime,
                size_bytes=size,
                sha256=digest,
                local_path=str(target),
                quarantine_reason="MIME_TYPE_NOT_ALLOWED",
            )
        accepted = self.root / "cards" / card
        accepted.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(accepted, 0o700)
        target = accepted / f"{artifact_id}-{clean_name}"
        self._atomic_bytes(target, content)
        return ArtifactWrite(
            artifact_id=artifact_id,
            state="ACCEPTED",
            name=clean_name,
            mime_type=clean_mime,
            size_bytes=size,
            sha256=digest,
            local_path=str(target),
            quarantine_reason=None,
        )

    def write_output(self, *, card_id: str, name: str, content: bytes, mime_type: str = "text/plain") -> ArtifactWrite:
        result = self.ingest(card_id=card_id, name=name, mime_type=mime_type, content=content)
        if result.state != "ACCEPTED":
            raise ArtifactStoreError("generated output was unexpectedly quarantined")
        return result

    def read(self, local_path: str, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> bytes:
        path = Path(local_path)
        try:
            resolved = path.resolve(strict=True)
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ArtifactStoreError("artifact is unavailable") from exc
        if root != resolved and root not in resolved.parents:
            raise ArtifactStoreError("artifact path escapes configured root")
        if resolved.stat().st_size > max_bytes:
            raise ArtifactStoreError("artifact exceeds read limit")
        data = resolved.read_bytes()
        if len(data) > max_bytes:
            raise ArtifactStoreError("artifact exceeds read limit")
        return data

    def _atomic_bytes(self, target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ArtifactStoreError("invalid Card id") from exc


def _safe_name(value: str) -> str:
    text = Path(str(value).strip()).name
    if not text or text in {".", ".."} or len(text.encode("utf-8")) > 255 or "\x00" in text:
        raise ArtifactStoreError("invalid artifact name")
    return text


def _safe_mime(value: str) -> str:
    text = str(value).split(";", 1)[0].strip().lower()
    if not text or len(text) > 128 or any(ord(char) < 0x20 for char in text):
        raise ArtifactStoreError("invalid artifact MIME type")
    return text
