from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time
from typing import Any, Callable, Mapping
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid

from .auth_service import CredentialService
from .drive_bridge import DRIVE_CONSUMER_SCOPE, DriveNeedsAction, DriveRemoteError, GoogleDriveRemote


GOOGLE_DEVICE_ENDPOINT = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
MAX_CREDENTIAL_BUNDLE_BYTES = 64 * 1024
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
MAX_DEVICE_RESPONSE_BYTES = 64 * 1024
TokenExchange = Callable[[str, str, str | None], str]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class GoogleDriveCredentialDescriptor:
    mode: str
    refresh_capable: bool
    recommended_scope: str = GOOGLE_DRIVE_FILE_SCOPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "refresh_capable": self.refresh_capable,
            "recommended_scope": self.recommended_scope,
        }


@dataclass(frozen=True, slots=True)
class GoogleOAuthClientConfig:
    """Distribution-level Google OAuth application identity.

    This identifies the GenOS OAuth application. It is not the end user's
    Google credential and must never be confused with the refresh token stored
    through SecretProvider/SecretRef after user consent.
    """

    client_id: str
    client_secret: str
    scope: str = GOOGLE_DRIVE_FILE_SCOPE

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> GoogleOAuthClientConfig | None:
        source = env if env is not None else os.environ
        client_id = str(source.get("GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_ID") or "").strip()
        client_secret = str(source.get("GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_SECRET") or "").strip()
        if not client_id and not client_secret:
            return None
        if not client_id or not client_secret:
            raise DriveNeedsAction("Google Drive OAuth application configuration is incomplete")
        return cls(_checked_secret(client_id, "OAuth client id"), _checked_secret(client_secret, "OAuth client secret"))


@dataclass(frozen=True, slots=True)
class GoogleDeviceAuthorizationProjection:
    state: str
    verification_url: str | None = None
    user_code: str | None = None
    expires_at: str | None = None
    poll_interval_seconds: int | None = None
    retry_after_seconds: int | None = None
    secret_id: str | None = None
    root_name: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "expires_at": self.expires_at,
            "poll_interval_seconds": self.poll_interval_seconds,
            "retry_after_seconds": self.retry_after_seconds,
            "secret_id": self.secret_id,
            "root_name": self.root_name,
            "reason": self.reason,
            "scope": GOOGLE_DRIVE_FILE_SCOPE,
            "credential_authority": "SecretProvider/SecretRef",
        }


@dataclass(slots=True)
class _DeviceSession:
    device_code: str
    projection: GoogleDeviceAuthorizationProjection
    expires_monotonic: float
    next_poll_monotonic: float
    poll_interval_seconds: int


class _AuthorizationPending(RuntimeError):
    pass


class _SlowDown(RuntimeError):
    pass


class _AccessDenied(RuntimeError):
    pass


class _AuthorizationExpired(RuntimeError):
    pass


DeviceRequest = Callable[[GoogleOAuthClientConfig], dict[str, Any]]
DevicePoll = Callable[[GoogleOAuthClientConfig, str], dict[str, Any]]


class GoogleDriveDeviceAuthService:
    """Typed user-owned Google Drive authorization for headless GenOS.

    The provider device code remains process-internal. The public projection
    contains only the verification URL/user code that Google explicitly expects
    the user to see. On success the refresh token is written one-way through
    CredentialService and only the resulting SecretRef id leaves this service.
    """

    def __init__(
        self,
        *,
        credentials: CredentialService,
        client_config: GoogleOAuthClientConfig | None,
        device_request: DeviceRequest | None = None,
        device_poll: DevicePoll | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.client_config = client_config
        self.device_request = device_request or request_google_device_authorization
        self.device_poll = device_poll or poll_google_device_authorization
        self.clock = clock
        self._lock = threading.RLock()
        self._session: _DeviceSession | None = None
        self._last = GoogleDeviceAuthorizationProjection(
            state="NOT_CONFIGURED" if client_config is None else "UNCONFIGURED",
            reason="OAUTH_CLIENT_NOT_CONFIGURED" if client_config is None else None,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._expire_if_needed()
            return self._last.to_dict()

    def start(self, *, root_name: str = "GenOS") -> dict[str, Any]:
        config = self._require_config()
        root = _checked_root_name(root_name)
        payload = self.device_request(config)
        device_code = _checked_secret(payload.get("device_code"), "device code")
        user_code = _checked_public_text(payload.get("user_code"), "user code", max_length=256)
        verification_url = _checked_verification_url(payload.get("verification_url") or payload.get("verification_uri"))
        expires_in = _bounded_int(payload.get("expires_in"), "expires_in", minimum=30, maximum=7200)
        interval = _bounded_int(payload.get("interval", 5), "interval", minimum=1, maximum=120)
        now = self.clock()
        projection = GoogleDeviceAuthorizationProjection(
            state="WAITING_USER",
            verification_url=verification_url,
            user_code=user_code,
            expires_at=_future_utc(expires_in),
            poll_interval_seconds=interval,
            retry_after_seconds=interval,
            root_name=root,
        )
        with self._lock:
            self._session = _DeviceSession(
                device_code=device_code,
                projection=projection,
                expires_monotonic=now + expires_in,
                next_poll_monotonic=now + interval,
                poll_interval_seconds=interval,
            )
            self._last = projection
            return projection.to_dict()

    def poll(self) -> dict[str, Any]:
        config = self._require_config()
        with self._lock:
            self._expire_if_needed()
            session = self._session
            if session is None:
                if self._last.state in {"AUTHORIZED", "DENIED", "EXPIRED"}:
                    return self._last.to_dict()
                raise DriveNeedsAction("Google Drive authorization has not been started")
            now = self.clock()
            if now < session.next_poll_monotonic:
                retry = max(1, int(session.next_poll_monotonic - now + 0.999))
                self._last = _replace_projection(session.projection, retry_after_seconds=retry)
                session.projection = self._last
                return self._last.to_dict()
            device_code = session.device_code

        try:
            token_payload = self.device_poll(config, device_code)
        except _AuthorizationPending:
            return self._continue_waiting(slow_down=False)
        except _SlowDown:
            return self._continue_waiting(slow_down=True)
        except _AccessDenied:
            return self._terminal("DENIED", "USER_DENIED")
        except _AuthorizationExpired:
            return self._terminal("EXPIRED", "AUTHORIZATION_EXPIRED")

        access_token = _checked_secret(token_payload.get("access_token"), "access token")
        refresh_token = _checked_secret(token_payload.get("refresh_token"), "refresh token")
        token_type = _checked_public_text(token_payload.get("token_type", "Bearer"), "token type", max_length=32)
        if token_type.lower() != "bearer":
            raise DriveNeedsAction("Google Drive authorization returned an unsupported token type")
        granted = str(token_payload.get("scope") or "").split()
        if GOOGLE_DRIVE_FILE_SCOPE not in granted:
            raise DriveNeedsAction("Google Drive authorization did not grant the required drive.file scope")
        # Validate the access token but deliberately do not persist or expose it.
        if not access_token:
            raise DriveNeedsAction("Google Drive authorization did not return a usable access token")
        bundle = json.dumps(
            {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": refresh_token,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        credential = self.credentials.add(
            name=f"google-drive-{uuid.uuid4().hex[:12]}",
            provider_name="google-drive",
            raw_secret=bundle,
            consumer_scopes=[DRIVE_CONSUMER_SCOPE],
            source="google-device-oauth",
        )
        secret_id = str(credential["secret_id"])
        with self._lock:
            root_name = self._session.projection.root_name if self._session is not None else "GenOS"
            self._session = None
            self._last = GoogleDeviceAuthorizationProjection(
                state="AUTHORIZED",
                secret_id=secret_id,
                root_name=root_name,
            )
            return self._last.to_dict()

    def clear(self, *, state: str = "DISCONNECTED", reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._session = None
            self._last = GoogleDeviceAuthorizationProjection(state=state, reason=reason)
            return self._last.to_dict()

    def _continue_waiting(self, *, slow_down: bool) -> dict[str, Any]:
        with self._lock:
            self._expire_if_needed()
            session = self._session
            if session is None:
                return self._last.to_dict()
            if slow_down:
                session.poll_interval_seconds = min(120, session.poll_interval_seconds + 5)
            session.next_poll_monotonic = self.clock() + session.poll_interval_seconds
            self._last = _replace_projection(
                session.projection,
                poll_interval_seconds=session.poll_interval_seconds,
                retry_after_seconds=session.poll_interval_seconds,
                reason="SLOW_DOWN" if slow_down else None,
            )
            session.projection = self._last
            return self._last.to_dict()

    def _terminal(self, state: str, reason: str) -> dict[str, Any]:
        with self._lock:
            self._session = None
            self._last = GoogleDeviceAuthorizationProjection(state=state, reason=reason)
            return self._last.to_dict()

    def _expire_if_needed(self) -> None:
        session = self._session
        if session is not None and self.clock() >= session.expires_monotonic:
            self._session = None
            self._last = GoogleDeviceAuthorizationProjection(state="EXPIRED", reason="AUTHORIZATION_EXPIRED")

    def _require_config(self) -> GoogleOAuthClientConfig:
        if self.client_config is None:
            raise DriveNeedsAction("Google Drive OAuth application is not configured in this GenOS distribution")
        return self.client_config


class GoogleDriveRemoteFactory:
    """Resolve SecretProvider material into one ephemeral Drive access token.

    Supported SecretRef raw formats:
    - a bearer access token string (compatibility/manual short-lived mode);
    - a JSON OAuth refresh bundle with client_id + refresh_token and optional
      client_secret. A fresh access token is obtained at every remote factory
      call and is never persisted into Product DB/Drive/report state.
    """

    def __init__(self, *, token_exchange: TokenExchange | None = None) -> None:
        self.token_exchange = token_exchange or exchange_google_refresh_token

    def __call__(self, raw_secret: str) -> GoogleDriveRemote:
        access_token, _descriptor = self.resolve(raw_secret)
        return GoogleDriveRemote(access_token)

    def resolve(self, raw_secret: str) -> tuple[str, GoogleDriveCredentialDescriptor]:
        value = raw_secret.strip()
        if not value or len(value.encode("utf-8")) > MAX_CREDENTIAL_BUNDLE_BYTES:
            raise DriveNeedsAction("Drive credential material is unavailable")
        if not value.startswith("{"):
            return value, GoogleDriveCredentialDescriptor(mode="access_token", refresh_capable=False)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DriveNeedsAction("Drive OAuth credential bundle is invalid") from exc
        if not isinstance(payload, dict):
            raise DriveNeedsAction("Drive OAuth credential bundle must be an object")

        client_id = _optional_secret(payload, "client_id")
        refresh_token = _optional_secret(payload, "refresh_token")
        client_secret = _optional_secret(payload, "client_secret")
        if client_id and refresh_token:
            token = self.token_exchange(client_id, refresh_token, client_secret)
            return token, GoogleDriveCredentialDescriptor(mode="oauth_refresh", refresh_capable=True)

        access_token = _optional_secret(payload, "access_token")
        if access_token:
            return access_token, GoogleDriveCredentialDescriptor(mode="access_token_bundle", refresh_capable=False)
        raise DriveNeedsAction("Drive OAuth bundle needs client_id+refresh_token or access_token")


def request_google_device_authorization(config: GoogleOAuthClientConfig) -> dict[str, Any]:
    fields = {"client_id": config.client_id, "scope": config.scope}
    return _post_google_json(GOOGLE_DEVICE_ENDPOINT, fields, max_bytes=MAX_DEVICE_RESPONSE_BYTES, purpose="device authorization")


def poll_google_device_authorization(config: GoogleOAuthClientConfig, device_code: str) -> dict[str, Any]:
    fields = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "device_code": device_code,
        "grant_type": GOOGLE_DEVICE_GRANT_TYPE,
    }
    try:
        return _post_google_json(GOOGLE_TOKEN_ENDPOINT, fields, max_bytes=MAX_TOKEN_RESPONSE_BYTES, purpose="device token")
    except _GoogleOAuthResponseError as exc:
        if exc.error == "authorization_pending":
            raise _AuthorizationPending from exc
        if exc.error == "slow_down":
            raise _SlowDown from exc
        if exc.error == "access_denied":
            raise _AccessDenied from exc
        if exc.error in {"expired_token", "invalid_grant"}:
            raise _AuthorizationExpired from exc
        if exc.error in {"admin_policy_enforced", "invalid_client", "org_internal", "unsupported_grant_type"}:
            raise DriveNeedsAction("Google Drive authorization requires configuration or policy action") from exc
        raise DriveNeedsAction("Google Drive authorization was rejected") from exc


def exchange_google_refresh_token(client_id: str, refresh_token: str, client_secret: str | None = None) -> str:
    fields = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        fields["client_secret"] = client_secret
    try:
        payload = _post_google_json(GOOGLE_TOKEN_ENDPOINT, fields, max_bytes=MAX_TOKEN_RESPONSE_BYTES, purpose="token refresh")
    except _GoogleOAuthResponseError as exc:
        if exc.status in {400, 401, 403}:
            raise DriveNeedsAction("Drive OAuth refresh credential was rejected") from exc
        raise DriveRemoteError("Google OAuth token endpoint returned an unavailable status") from exc
    token = payload.get("access_token")
    token_type = str(payload.get("token_type") or "Bearer")
    if not isinstance(token, str) or not token.strip() or token_type.lower() != "bearer":
        raise DriveNeedsAction("Google OAuth refresh did not return a usable bearer token")
    return token.strip()


class _GoogleOAuthResponseError(RuntimeError):
    def __init__(self, *, status: int, error: str) -> None:
        super().__init__(f"Google OAuth response error: {error}")
        self.status = status
        self.error = error


def _post_google_json(endpoint: str, fields: dict[str, str], *, max_bytes: int, purpose: str) -> dict[str, Any]:
    body = urlparse.urlencode(fields).encode("utf-8")
    request = urlrequest.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urlrequest.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Google OAuth endpoints only
            raw = response.read(max_bytes + 1)
            status = int(getattr(response, "status", 200))
    except urlerror.HTTPError as exc:
        raw = exc.read(max_bytes + 1)
        payload = _decode_json_object(raw, max_bytes=max_bytes, purpose=purpose)
        error_name = payload.get("error")
        if isinstance(error_name, str) and error_name:
            raise _GoogleOAuthResponseError(status=exc.code, error=error_name) from exc
        raise DriveRemoteError(f"Google OAuth {purpose} endpoint rejected the request") from exc
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise DriveRemoteError(f"Google OAuth {purpose} request failed") from exc
    if status < 200 or status >= 300:
        raise DriveRemoteError(f"Google OAuth {purpose} endpoint returned an unavailable status")
    return _decode_json_object(raw, max_bytes=max_bytes, purpose=purpose)


def _decode_json_object(raw: bytes, *, max_bytes: int, purpose: str) -> dict[str, Any]:
    if len(raw) > max_bytes:
        raise DriveRemoteError(f"Google OAuth {purpose} response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveRemoteError(f"Google OAuth {purpose} response is invalid") from exc
    if not isinstance(payload, dict):
        raise DriveRemoteError(f"Google OAuth {purpose} response must be an object")
    return payload


def _replace_projection(projection: GoogleDeviceAuthorizationProjection, **changes: Any) -> GoogleDeviceAuthorizationProjection:
    values = projection.to_dict()
    values.pop("scope", None)
    values.pop("credential_authority", None)
    values.update(changes)
    return GoogleDeviceAuthorizationProjection(**values)


def _checked_verification_url(value: Any) -> str:
    text = _checked_public_text(value, "verification URL", max_length=2048)
    parsed = urlparse.urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "google.com" or host.endswith(".google.com")):
        raise DriveNeedsAction("Google Drive authorization returned an unsafe verification URL")
    return text


def _checked_root_name(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 128 or any(ord(char) < 0x20 for char in text):
        raise DriveNeedsAction("Drive root name is invalid")
    return text


def _checked_secret(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise DriveNeedsAction(f"Google Drive {label} is unavailable")
    text = value.strip()
    if not text or len(text) > 16384 or any(ord(char) < 0x20 for char in text):
        raise DriveNeedsAction(f"Google Drive {label} is invalid")
    return text


def _checked_public_text(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise DriveNeedsAction(f"Google Drive {label} is unavailable")
    text = value.strip()
    if not text or len(text) > max_length or any(ord(char) < 0x20 for char in text):
        raise DriveNeedsAction(f"Google Drive {label} is invalid")
    return text


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise DriveNeedsAction(f"Google Drive {label} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DriveNeedsAction(f"Google Drive {label} is invalid") from exc
    if result < minimum or result > maximum:
        raise DriveNeedsAction(f"Google Drive {label} is outside supported bounds")
    return result


def _future_utc(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _optional_secret(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return _checked_secret(value, "OAuth credential bundle field")
