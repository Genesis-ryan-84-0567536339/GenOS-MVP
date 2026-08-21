from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .drive_bridge import DriveNeedsAction, DriveRemoteError, GoogleDriveRemote


GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
MAX_CREDENTIAL_BUNDLE_BYTES = 64 * 1024
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
TokenExchange = Callable[[str, str, str | None], str]


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


def exchange_google_refresh_token(client_id: str, refresh_token: str, client_secret: str | None = None) -> str:
    fields = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        fields["client_secret"] = client_secret
    body = urlparse.urlencode(fields).encode("utf-8")
    request = urlrequest.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Google OAuth endpoint
            raw = response.read(MAX_TOKEN_RESPONSE_BYTES)
    except urlerror.HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise DriveNeedsAction("Drive OAuth refresh credential was rejected") from exc
        raise DriveRemoteError("Google OAuth token endpoint returned an unavailable status") from exc
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise DriveRemoteError("Google OAuth token refresh request failed") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveRemoteError("Google OAuth token response is invalid") from exc
    if not isinstance(payload, dict):
        raise DriveRemoteError("Google OAuth token response must be an object")
    token = payload.get("access_token")
    token_type = str(payload.get("token_type") or "Bearer")
    if not isinstance(token, str) or not token.strip() or token_type.lower() != "bearer":
        raise DriveNeedsAction("Google OAuth refresh did not return a usable bearer token")
    return token.strip()


def _optional_secret(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DriveNeedsAction("Drive OAuth credential bundle contains an invalid field")
    text = value.strip()
    if not text or len(text) > 16384 or any(ord(char) < 0x20 for char in text):
        raise DriveNeedsAction("Drive OAuth credential bundle contains an invalid field")
    return text
