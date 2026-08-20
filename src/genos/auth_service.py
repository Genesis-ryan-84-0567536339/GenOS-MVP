from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from .product_store import (
    CredentialRecord,
    OwnerRecord,
    PostgresProductStore,
    ProductStoreConflict,
    ProductStoreNotFound,
)
from .secret_provider import LocalFileSecretProvider, SecretMaterialRef, SecretProviderError
from .security import hash_password, new_session_token, session_expiry, token_hash, verify_password


class AuthError(RuntimeError):
    pass


class AuthConflict(AuthError):
    pass


class AuthenticationFailed(AuthError):
    pass


class AuthorizationFailed(AuthError):
    pass


class CredentialError(RuntimeError):
    pass


class CredentialConflict(CredentialError):
    pass


class CredentialNotFound(CredentialError):
    pass


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_token: str
    owner: dict[str, str]
    expires_at: str

    def one_way_response(self) -> dict[str, Any]:
        # The raw token is intentionally returned exactly once to the caller.
        return {
            "session_token": self.session_token,
            "owner": self.owner,
            "expires_at": self.expires_at,
        }


class OwnerAuthService:
    def __init__(self, store: PostgresProductStore) -> None:
        self.store = store

    def bootstrap_owner(self, username: str, password: str) -> dict[str, str]:
        username_value = _normalize_username(username)
        if self.store.owner_count() != 0:
            raise AuthConflict("Owner already exists")
        digest = hash_password(password)
        owner_id = str(uuid.uuid4())
        try:
            record = self.store.insert_owner(owner_id, username_value, digest.salt, digest.digest)
        except ProductStoreConflict as exc:
            raise AuthConflict("Owner already exists") from exc
        return record.public_dict()

    def login(self, username: str, password: str, *, session_hours: int = 12) -> LoginResult:
        username_value = _normalize_username(username)
        record = self.store.get_owner_by_username(username_value)
        if record is None:
            # A fixed dummy digest reduces easy username-timing distinction.
            dummy = bytes.fromhex("a1" * 16)
            verify_password(password, salt=dummy, expected_digest=bytes.fromhex("b2" * 32))
            raise AuthenticationFailed("invalid credentials")
        if not verify_password(password, salt=record.password_salt, expected_digest=record.password_hash):
            raise AuthenticationFailed("invalid credentials")
        token = new_session_token()
        expiry = session_expiry(session_hours)
        self.store.create_session(str(uuid.uuid4()), record.owner_id, token_hash(token), expiry)
        return LoginResult(token, record.public_dict(), expiry.isoformat())

    def authenticate(self, session_token: str) -> dict[str, str]:
        if not session_token:
            raise AuthorizationFailed("missing session token")
        owner = self.store.resolve_session(token_hash(session_token))
        if owner is None:
            raise AuthorizationFailed("invalid or expired session")
        return owner.public_dict()

    def logout(self, session_token: str) -> None:
        if not session_token or not self.store.revoke_session(token_hash(session_token)):
            raise AuthorizationFailed("invalid session")


class CredentialService:
    def __init__(self, store: PostgresProductStore, provider: LocalFileSecretProvider) -> None:
        self.store = store
        self.provider = provider

    def add(
        self,
        *,
        name: str,
        provider_name: str,
        raw_secret: str,
        consumer_scopes: list[str] | None = None,
        source: str = "owner",
    ) -> dict[str, Any]:
        name_value = _normalize_name(name)
        provider_value = _normalize_provider(provider_name)
        secret_id = str(uuid.uuid4())
        material = self.provider.store_revision(secret_id, 1, raw_secret)
        try:
            record = self.store.insert_credential(
                secret_id=secret_id,
                name=name_value,
                provider=provider_value,
                fingerprint=material.fingerprint,
                consumer_scopes=consumer_scopes or [],
                source=_normalize_source(source),
            )
        except ProductStoreConflict as exc:
            self.provider.delete_revision(secret_id, 1)
            raise CredentialConflict("credential name already exists") from exc
        except Exception:
            self.provider.delete_revision(secret_id, 1)
            raise
        return record.public_dict()

    def rotate(self, secret_id: str, raw_secret: str, *, source: str = "owner") -> dict[str, Any]:
        record = self.store.get_credential(secret_id)
        if record is None:
            raise CredentialNotFound("credential not found")
        new_revision = record.active_revision + 1
        material = self.provider.store_revision(secret_id, new_revision, raw_secret)
        try:
            updated = self.store.rotate_credential(
                secret_id=secret_id,
                expected_revision=record.active_revision,
                new_revision=new_revision,
                fingerprint=material.fingerprint,
                source=_normalize_source(source),
            )
        except ProductStoreConflict as exc:
            self.provider.delete_revision(secret_id, new_revision)
            raise CredentialConflict("credential changed concurrently") from exc
        except Exception:
            self.provider.delete_revision(secret_id, new_revision)
            raise
        return updated.public_dict()

    def test(self, secret_id: str) -> dict[str, Any]:
        record = self.store.get_credential(secret_id)
        if record is None:
            raise CredentialNotFound("credential not found")
        ref = SecretMaterialRef(record.secret_id, record.active_revision, record.fingerprint)
        healthy = record.status == "ACTIVE" and self.provider.verify_revision(ref)
        return {
            "secret_id": record.secret_id,
            "state": "PASS" if healthy else "FAIL",
            "provider": record.provider,
            "active_revision": record.active_revision,
            "fingerprint": record.fingerprint,
        }

    def disable(self, secret_id: str) -> dict[str, Any]:
        try:
            record = self.store.disable_credential(secret_id)
        except ProductStoreNotFound as exc:
            raise CredentialNotFound("credential not found") from exc
        return record.public_dict()

    def list(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in self.store.list_credentials()]

    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        """Internal-only secret resolution for typed consumers.

        This method must never be wired to a public GET endpoint. It enforces
        status and consumer scope before returning raw material inside the
        trusted GenOS execution boundary.
        """
        record = self.store.get_credential(secret_id)
        if record is None:
            raise CredentialNotFound("credential not found")
        if record.status != "ACTIVE":
            raise CredentialError("credential disabled")
        consumer_value = _normalize_scope(consumer)
        if consumer_value not in record.consumer_scopes:
            raise CredentialError("consumer is not granted")
        return self.provider.read_secret(record.secret_id, record.active_revision)


def _normalize_username(value: str) -> str:
    result = value.strip()
    if len(result) < 3 or len(result) > 64:
        raise AuthError("username must be 3..64 characters")
    if not all(char.isalnum() or char in "._-" for char in result):
        raise AuthError("username contains unsupported characters")
    return result


def _normalize_name(value: str) -> str:
    result = value.strip()
    if not result or len(result) > 128:
        raise CredentialError("credential name must be 1..128 characters")
    return result


def _normalize_provider(value: str) -> str:
    result = value.strip().lower()
    if not result or len(result) > 64 or not all(char.isalnum() or char in "._-" for char in result):
        raise CredentialError("invalid credential provider")
    return result


def _normalize_source(value: str) -> str:
    result = value.strip().lower()
    if not result or len(result) > 64:
        raise CredentialError("invalid credential source")
    return result


def _normalize_scope(value: str) -> str:
    result = value.strip()
    if not result or len(result) > 128:
        raise CredentialError("invalid consumer scope")
    return result
