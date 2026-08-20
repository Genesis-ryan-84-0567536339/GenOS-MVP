from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from genos.auth_service import (
    AuthConflict,
    AuthenticationFailed,
    AuthorizationFailed,
    CredentialError,
    CredentialService,
    OwnerAuthService,
)
from genos.product_store import CredentialRecord, OwnerRecord, ProductStoreConflict, ProductStoreNotFound
from genos.secret_provider import LocalFileSecretProvider
from genos.security import hash_password, token_hash


class FakeStore:
    def __init__(self) -> None:
        self.owner: OwnerRecord | None = None
        self.sessions: dict[bytes, tuple[str, bool]] = {}
        self.credentials: dict[str, CredentialRecord] = {}
        self.names: dict[str, str] = {}

    def owner_count(self) -> int:
        return 1 if self.owner else 0

    def insert_owner(self, owner_id: str, username: str, salt: bytes, password_hash: bytes) -> OwnerRecord:
        if self.owner is not None:
            raise ProductStoreConflict("owner exists")
        self.owner = OwnerRecord(owner_id, username, salt, password_hash, datetime.now(timezone.utc).isoformat())
        return self.owner

    def get_owner_by_username(self, username: str) -> OwnerRecord | None:
        return self.owner if self.owner and self.owner.username == username else None

    def create_session(self, session_id: str, owner_id: str, token_hash_value: bytes, expires_at: datetime) -> None:
        self.sessions[token_hash_value] = (owner_id, False)

    def resolve_session(self, token_hash_value: bytes) -> OwnerRecord | None:
        value = self.sessions.get(token_hash_value)
        if not value or value[1] or not self.owner or value[0] != self.owner.owner_id:
            return None
        return self.owner

    def revoke_session(self, token_hash_value: bytes) -> bool:
        value = self.sessions.get(token_hash_value)
        if not value or value[1]:
            return False
        self.sessions[token_hash_value] = (value[0], True)
        return True

    def insert_credential(self, *, secret_id: str, name: str, provider: str, fingerprint: str, consumer_scopes: list[str], source: str) -> CredentialRecord:
        if name in self.names:
            raise ProductStoreConflict("name exists")
        now = datetime.now(timezone.utc).isoformat()
        record = CredentialRecord(secret_id, name, provider, 1, "ACTIVE", fingerprint, tuple(consumer_scopes), now, now)
        self.credentials[secret_id] = record
        self.names[name] = secret_id
        return record

    def get_credential(self, secret_id: str) -> CredentialRecord | None:
        return self.credentials.get(secret_id)

    def rotate_credential(self, *, secret_id: str, expected_revision: int, new_revision: int, fingerprint: str, source: str) -> CredentialRecord:
        current = self.credentials.get(secret_id)
        if current is None:
            raise ProductStoreNotFound("missing")
        if current.active_revision != expected_revision:
            raise ProductStoreConflict("revision")
        now = datetime.now(timezone.utc).isoformat()
        updated = CredentialRecord(
            current.secret_id,
            current.name,
            current.provider,
            new_revision,
            "ACTIVE",
            fingerprint,
            current.consumer_scopes,
            current.created_at,
            now,
        )
        self.credentials[secret_id] = updated
        return updated

    def disable_credential(self, secret_id: str) -> CredentialRecord:
        current = self.credentials.get(secret_id)
        if current is None:
            raise ProductStoreNotFound("missing")
        now = datetime.now(timezone.utc).isoformat()
        updated = CredentialRecord(
            current.secret_id,
            current.name,
            current.provider,
            current.active_revision,
            "DISABLED",
            current.fingerprint,
            current.consumer_scopes,
            current.created_at,
            now,
        )
        self.credentials[secret_id] = updated
        return updated

    def list_credentials(self) -> list[CredentialRecord]:
        return list(self.credentials.values())


class OwnerAuthTests(unittest.TestCase):
    def test_exactly_one_owner_and_session_hash_only(self) -> None:
        store = FakeStore()
        service = OwnerAuthService(store)  # type: ignore[arg-type]
        password = "owner-password-12345"
        owner = service.bootstrap_owner("ryan", password)
        self.assertEqual(owner["username"], "ryan")
        self.assertNotIn(password, json.dumps(owner))
        with self.assertRaises(AuthConflict):
            service.bootstrap_owner("ryan2", "another-password-123")

        login = service.login("ryan", password)
        self.assertTrue(login.session_token)
        self.assertNotIn(login.session_token.encode(), store.sessions)
        self.assertIn(token_hash(login.session_token), store.sessions)
        self.assertEqual(service.authenticate(login.session_token)["username"], "ryan")
        service.logout(login.session_token)
        with self.assertRaises(AuthorizationFailed):
            service.authenticate(login.session_token)

    def test_wrong_password_rejected(self) -> None:
        store = FakeStore()
        service = OwnerAuthService(store)  # type: ignore[arg-type]
        service.bootstrap_owner("ryan", "owner-password-12345")
        with self.assertRaises(AuthenticationFailed):
            service.login("ryan", "wrong-password-12345")

    def test_password_digest_never_contains_password(self) -> None:
        password = "owner-password-12345"
        digest = hash_password(password)
        self.assertNotEqual(digest.digest, password.encode())
        self.assertNotEqual(digest.salt, password.encode())


class SecretProviderTests(unittest.TestCase):
    def test_secret_files_are_restricted_and_public_ref_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = LocalFileSecretProvider(Path(tmp) / "secrets")
            secret_id = str(uuid.uuid4())
            raw = "fixture-secret-value-123"
            ref = provider.store_revision(secret_id, 1, raw)
            root_mode = provider.root.stat().st_mode & 0o777
            dir_mode = (provider.root / secret_id).stat().st_mode & 0o777
            file_path = provider.root / secret_id / "1.secret"
            file_mode = file_path.stat().st_mode & 0o777
            self.assertEqual(root_mode, 0o700)
            self.assertEqual(dir_mode, 0o700)
            self.assertEqual(file_mode, 0o600)
            self.assertEqual(provider.read_secret(secret_id, 1), raw)
            public = json.dumps(ref.to_public_dict())
            self.assertNotIn(raw, public)
            self.assertTrue(provider.verify_revision(ref))


class CredentialLifecycleTests(unittest.TestCase):
    def test_add_rotate_test_disable_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FakeStore()
            provider = LocalFileSecretProvider(Path(tmp) / "secrets")
            service = CredentialService(store, provider)  # type: ignore[arg-type]
            raw_one = "fixture-api-token-one-123"
            raw_two = "fixture-api-token-two-456"
            record = service.add(
                name="google-drive",
                provider_name="google",
                raw_secret=raw_one,
                consumer_scopes=["agy-gen", "drive-sync"],
            )
            serialized = json.dumps(record)
            self.assertNotIn(raw_one, serialized)
            self.assertEqual(record["active_revision"], 1)
            self.assertEqual(service.test(record["secret_id"])["state"], "PASS")
            self.assertEqual(service.get_secret_for_consumer(record["secret_id"], consumer="agy-gen"), raw_one)
            with self.assertRaises(CredentialError):
                service.get_secret_for_consumer(record["secret_id"], consumer="unknown-agent")

            rotated = service.rotate(record["secret_id"], raw_two)
            self.assertEqual(rotated["active_revision"], 2)
            self.assertNotIn(raw_two, json.dumps(rotated))
            self.assertEqual(service.get_secret_for_consumer(record["secret_id"], consumer="drive-sync"), raw_two)

            disabled = service.disable(record["secret_id"])
            self.assertEqual(disabled["status"], "DISABLED")
            self.assertEqual(service.test(record["secret_id"])["state"], "FAIL")
            with self.assertRaises(CredentialError):
                service.get_secret_for_consumer(record["secret_id"], consumer="agy-gen")

    def test_duplicate_name_rolls_back_raw_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FakeStore()
            provider = LocalFileSecretProvider(Path(tmp) / "secrets")
            service = CredentialService(store, provider)  # type: ignore[arg-type]
            service.add(name="same", provider_name="test", raw_secret="fixture-secret-111", consumer_scopes=[])
            before_dirs = {path.name for path in provider.root.iterdir()}
            from genos.auth_service import CredentialConflict
            with self.assertRaises(CredentialConflict):
                service.add(name="same", provider_name="test", raw_secret="fixture-secret-222", consumer_scopes=[])
            after_dirs = {path.name for path in provider.root.iterdir()}
            self.assertEqual(before_dirs, after_dirs)


if __name__ == "__main__":
    unittest.main()
