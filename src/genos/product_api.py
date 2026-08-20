from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import json
import os
import re

from . import __version__
from .auth_service import (
    AuthConflict,
    AuthError,
    AuthenticationFailed,
    AuthorizationFailed,
    CredentialConflict,
    CredentialError,
    CredentialNotFound,
    CredentialService,
    OwnerAuthService,
)
from .product_store import PostgresProductStore, ProductStoreError
from .secret_provider import LocalFileSecretProvider, SecretProviderError


MAX_JSON_BODY = 64 * 1024
_CREDENTIAL_ACTION = re.compile(r"^/api/v1/credentials/([0-9a-fA-F-]{36})/(rotate|test|disable)$")


class ProductAPIApp:
    def __init__(
        self,
        auth: OwnerAuthService,
        credentials: CredentialService,
        store: PostgresProductStore,
    ) -> None:
        self.auth = auth
        self.credentials = credentials
        self.store = store

    @classmethod
    def from_system(cls) -> "ProductAPIApp":
        store = PostgresProductStore()
        store.ensure_schema()
        secret_root = os.environ.get("GENOS_SECRET_DIR", "/var/lib/genos/secrets")
        provider = LocalFileSecretProvider(secret_root)
        return cls(OwnerAuthService(store), CredentialService(store, provider), store)


class ProductAPIHandler(BaseHTTPRequestHandler):
    server_version = "GenOSProductAPI/0.1"

    @property
    def app(self) -> ProductAPIApp:
        return getattr(self.server, "genos_app")  # type: ignore[no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "role": "product-api",
                        "version": __version__,
                        "instance_id": os.environ.get("GENOS_INSTANCE_ID") or "UNKNOWN",
                    },
                )
                return
            if self.path == "/api/v1/auth/me":
                owner = self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"owner": owner})
                return
            if self.path == "/api/v1/credentials":
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"credentials": self.app.credentials.list()})
                return
            self._json(404, {"error": "not_found"})
        except Exception as exc:  # mapped centrally; no raw body/header logging
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/v1/owner/bootstrap":
                body = self._read_json()
                owner = self.app.auth.bootstrap_owner(
                    _required_text(body, "username"),
                    _required_text(body, "password"),
                )
                self._json(201, {"owner": owner})
                return
            if self.path == "/api/v1/auth/login":
                body = self._read_json()
                result = self.app.auth.login(
                    _required_text(body, "username"),
                    _required_text(body, "password"),
                )
                self._json(200, result.one_way_response())
                return
            if self.path == "/api/v1/auth/logout":
                self.app.auth.logout(self._bearer_token())
                self._json(200, {"state": "REVOKED"})
                return
            if self.path == "/api/v1/credentials":
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                scopes_raw = body.get("consumer_scopes", [])
                if not isinstance(scopes_raw, list) or not all(isinstance(item, str) for item in scopes_raw):
                    raise CredentialError("consumer_scopes must be a list of strings")
                record = self.app.credentials.add(
                    name=_required_text(body, "name"),
                    provider_name=_required_text(body, "provider"),
                    raw_secret=_required_text(body, "secret"),
                    consumer_scopes=list(scopes_raw),
                    source="owner-api",
                )
                self._json(201, {"credential": record})
                return
            action = _CREDENTIAL_ACTION.match(self.path)
            if action:
                self.app.auth.authenticate(self._bearer_token())
                secret_id, operation = action.groups()
                if operation == "rotate":
                    body = self._read_json()
                    record = self.app.credentials.rotate(
                        secret_id,
                        _required_text(body, "secret"),
                        source="owner-api",
                    )
                    self._json(200, {"credential": record})
                    return
                if operation == "test":
                    self._reject_nonempty_body()
                    self._json(200, {"test": self.app.credentials.test(secret_id)})
                    return
                if operation == "disable":
                    self._reject_nonempty_body()
                    self._json(200, {"credential": self.app.credentials.disable(secret_id)})
                    return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def log_message(self, fmt: str, *args: object) -> None:
        # Request paths are fixed/typed and never carry credentials. Headers and
        # bodies are intentionally omitted so Bearer tokens/raw secrets cannot
        # appear in service logs.
        message = fmt % args
        print(json.dumps({"event": "product_api_http", "message": message}, ensure_ascii=False), flush=True)

    def _bearer_token(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise AuthorizationFailed("missing session")
        token = value[7:].strip()
        if not token:
            raise AuthorizationFailed("missing session")
        return token

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise AuthError("Content-Length required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AuthError("invalid Content-Length") from exc
        if length < 0 or length > MAX_JSON_BODY:
            raise AuthError("request body too large")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise AuthError("application/json required")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AuthError("JSON object required")
        return payload

    def _reject_nonempty_body(self) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length and raw_length != "0":
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AuthError("invalid Content-Length") from exc
            if length > 0:
                # Drain a bounded body before returning an error.
                self.rfile.read(min(length, MAX_JSON_BODY))
                raise AuthError("request body not allowed")

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, AuthConflict):
            self._json(409, {"error": "owner_exists"})
            return
        if isinstance(exc, AuthenticationFailed):
            self._json(401, {"error": "invalid_credentials"})
            return
        if isinstance(exc, AuthorizationFailed):
            self._json(401, {"error": "unauthorized"})
            return
        if isinstance(exc, CredentialConflict):
            self._json(409, {"error": "credential_conflict"})
            return
        if isinstance(exc, CredentialNotFound):
            self._json(404, {"error": "credential_not_found"})
            return
        if isinstance(exc, (AuthError, CredentialError, ValueError)):
            self._json(400, {"error": "invalid_request"})
            return
        if isinstance(exc, (ProductStoreError, SecretProviderError)):
            self._json(503, {"error": "backend_unavailable"})
            return
        # Do not echo exception details: they may contain operational data.
        self._json(500, {"error": "internal_error"})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AuthError(f"{key} is required")
    return value


def attach_product_api(server: ThreadingHTTPServer) -> None:
    server.genos_app = ProductAPIApp.from_system()  # type: ignore[attr-defined]
