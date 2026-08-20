from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets


SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LENGTH = 12
SESSION_TOKEN_BYTES = 32
DEFAULT_SESSION_HOURS = 12


class SecurityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PasswordDigest:
    salt: bytes
    digest: bytes


def hash_password(password: str, *, salt: bytes | None = None) -> PasswordDigest:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SecurityError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    material = password.encode("utf-8")
    salt_value = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        material,
        salt=salt_value,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return PasswordDigest(salt=salt_value, digest=digest)


def verify_password(password: str, *, salt: bytes, expected_digest: bytes) -> bool:
    try:
        actual = hash_password(password, salt=salt).digest
    except SecurityError:
        # Authentication still fails for short invalid candidates without
        # relaxing the bootstrap password policy.
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
        )
    return hmac.compare_digest(actual, expected_digest)


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def session_expiry(hours: int = DEFAULT_SESSION_HOURS) -> datetime:
    if hours < 1 or hours > 168:
        raise SecurityError("session expiry must be between 1 and 168 hours")
    return datetime.now(timezone.utc) + timedelta(hours=hours)
