from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import tempfile
import uuid


class SecretProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecretMaterialRef:
    secret_id: str
    revision: int
    fingerprint: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "secret_id": self.secret_id,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "provider": "local-file",
        }


class LocalFileSecretProvider:
    """Raw-secret authority stored outside Product DB with strict file modes.

    Public services should return only `SecretMaterialRef`. `read_secret` exists
    for typed internal consumers/tests and must never be serialized to API,
    reports or logs.
    """

    def __init__(self, root: str | os.PathLike[str] = "/var/lib/genos/secrets") -> None:
        self.root = Path(root)

    def store_revision(self, secret_id: str, revision: int, raw_secret: str) -> SecretMaterialRef:
        normalized = _normalize_uuid(secret_id)
        if revision < 1:
            raise SecretProviderError("secret revision must be >= 1")
        if not raw_secret:
            raise SecretProviderError("secret value must not be empty")
        secret_bytes = raw_secret.encode("utf-8")
        secret_dir = self.root / normalized
        self._ensure_root()
        secret_dir.mkdir(parents=False, exist_ok=True, mode=0o700)
        os.chmod(secret_dir, 0o700)
        path = secret_dir / f"{revision}.secret"
        if path.exists():
            existing = path.read_bytes()
            if existing != secret_bytes:
                raise SecretProviderError("secret revision already exists with different material")
            return SecretMaterialRef(normalized, revision, fingerprint(secret_bytes))
        _atomic_secret_write(path, secret_bytes)
        return SecretMaterialRef(normalized, revision, fingerprint(secret_bytes))

    def read_secret(self, secret_id: str, revision: int) -> str:
        path = self._path(secret_id, revision)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise SecretProviderError("secret material not found") from exc
        return data.decode("utf-8")

    def verify_revision(self, ref: SecretMaterialRef) -> bool:
        try:
            data = self._path(ref.secret_id, ref.revision).read_bytes()
        except OSError:
            return False
        return fingerprint(data) == ref.fingerprint

    def delete_revision(self, secret_id: str, revision: int) -> None:
        path = self._path(secret_id, revision)
        path.unlink(missing_ok=True)
        secret_dir = path.parent
        try:
            secret_dir.rmdir()
        except OSError:
            pass

    def _path(self, secret_id: str, revision: int) -> Path:
        normalized = _normalize_uuid(secret_id)
        if revision < 1:
            raise SecretProviderError("secret revision must be >= 1")
        return self.root / normalized / f"{revision}.secret"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)


def fingerprint(secret: bytes) -> str:
    # Deliberately not reversible and short enough for UI comparison. The full
    # raw secret must never be derived from or replaced by this metadata.
    return "sha256:" + hashlib.sha256(secret).hexdigest()[:16]


def _normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise SecretProviderError("invalid secret id") from exc


def _atomic_secret_write(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
