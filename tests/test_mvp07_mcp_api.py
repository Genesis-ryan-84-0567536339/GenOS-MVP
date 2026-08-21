from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from typing import Any
import json
import unittest

from genos.auth_service import AuthorizationFailed
from genos.mcp_store import (
    IssuedPrincipal,
    McpConflict,
    McpNotFound,
    normalize_endpoint,
    normalize_namespace,
    normalize_scopes,
)
from genos.product_api import ProductAPIApp, ProductAPIHandler


OWNER_SESSION = "owner-session-fixture"
NOW = "2026-08-21T00:00:00Z"


class FakeOwnerAuth:
    def authenticate(self, token: str) -> dict[str, str]:
        if token != OWNER_SESSION:
            raise AuthorizationFailed("invalid or expired session")
        return {"owner_id": "owner-1", "username": "ryan", "created_at": NOW}


@dataclass(frozen=True)
class FakeCredential:
    status: str
    consumer_scopes: tuple[str, ...]


class FakeProductStore:
    def __init__(self) -> None:
        self.credentials: dict[str, FakeCredential] = {}

    def get_credential(self, secret_id: str) -> FakeCredential | None:
        return self.credentials.get(secret_id)


class InMemoryMcpStore:
    """Faithful public MCP projection with hash-only private token storage."""

    def __init__(self) -> None:
        self._principal_sequence = 0
        self._upstream_sequence = 0
        self._token_sequence = 0
        self._principals: dict[str, dict[str, Any]] = {}
        self._upstreams: dict[str, dict[str, Any]] = {}

    def _new_token(self) -> str:
        self._token_sequence += 1
        return f"gmcp_fixture_token_{self._token_sequence}"

    def create_principal(self, *, name: str, scopes: list[str]) -> IssuedPrincipal:
        self._principal_sequence += 1
        principal_id = f"00000000-0000-4000-8000-{self._principal_sequence:012d}"
        token = self._new_token()
        digest = sha256(token.encode("utf-8")).hexdigest()
        self._principals[principal_id] = {
            "principal_id": principal_id,
            "name": name,
            "token_hash": digest,
            "fingerprint": digest[:16],
            "status": "ACTIVE",
            "scopes": normalize_scopes(scopes),
            "created_at": NOW,
            "updated_at": NOW,
        }
        return IssuedPrincipal(self._public_principal(principal_id), token)

    def list_principals(self) -> list[dict[str, Any]]:
        return [self._public_principal(principal_id) for principal_id in sorted(self._principals)]

    def rotate_principal(self, principal_id: str) -> IssuedPrincipal:
        record = self._require_principal_record(principal_id)
        token = self._new_token()
        digest = sha256(token.encode("utf-8")).hexdigest()
        record["token_hash"] = digest
        record["fingerprint"] = digest[:16]
        record["status"] = "ACTIVE"
        record["updated_at"] = NOW
        return IssuedPrincipal(self._public_principal(principal_id), token)

    def revoke_principal(self, principal_id: str) -> dict[str, Any]:
        record = self._require_principal_record(principal_id)
        record["status"] = "REVOKED"
        record["updated_at"] = NOW
        return self._public_principal(principal_id)

    def replace_scopes(self, principal_id: str, scopes: list[str]) -> dict[str, Any]:
        record = self._require_principal_record(principal_id)
        record["scopes"] = normalize_scopes(scopes)
        record["updated_at"] = NOW
        return self._public_principal(principal_id)

    def register_upstream(
        self,
        *,
        namespace: str,
        name: str,
        endpoint: str,
        secret_id: str | None,
    ) -> dict[str, Any]:
        normalized_namespace = normalize_namespace(namespace)
        if normalized_namespace == "genos":
            raise McpConflict("genos namespace is reserved")
        if any(record["namespace"] == normalized_namespace for record in self._upstreams.values()):
            raise McpConflict("namespace already exists")
        self._upstream_sequence += 1
        upstream_id = f"10000000-0000-4000-8000-{self._upstream_sequence:012d}"
        self._upstreams[upstream_id] = {
            "upstream_id": upstream_id,
            "namespace": normalized_namespace,
            "name": name,
            "endpoint": normalize_endpoint(endpoint),
            "secret_id": secret_id,
            "status": "ACTIVE",
            "last_state": "UNKNOWN",
            "last_verified_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        return self._public_upstream(upstream_id)

    def list_upstreams(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        upstreams = [self._public_upstream(upstream_id) for upstream_id in sorted(self._upstreams)]
        if active_only:
            return [record for record in upstreams if record["status"] == "ACTIVE"]
        return upstreams

    def disable_upstream(self, upstream_id: str) -> dict[str, Any]:
        record = self._upstreams.get(upstream_id)
        if record is None:
            raise McpNotFound("MCP upstream not found")
        record["status"] = "DISABLED"
        record["updated_at"] = NOW
        return self._public_upstream(upstream_id)

    def recent_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return []

    def private_snapshot(self) -> dict[str, Any]:
        return {
            "principals": list(self._principals.values()),
            "upstreams": list(self._upstreams.values()),
        }

    def _require_principal_record(self, principal_id: str) -> dict[str, Any]:
        record = self._principals.get(principal_id)
        if record is None:
            raise McpNotFound("MCP principal not found")
        return record

    def _public_principal(self, principal_id: str) -> dict[str, Any]:
        record = self._require_principal_record(principal_id)
        return {
            "principal_id": record["principal_id"],
            "name": record["name"],
            "fingerprint": record["fingerprint"],
            "status": record["status"],
            "scopes": list(record["scopes"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def _public_upstream(self, upstream_id: str) -> dict[str, Any]:
        record = self._upstreams[upstream_id]
        return dict(record)


class QuietProductAPIHandler(ProductAPIHandler):
    def log_message(self, _fmt: str, *_args: object) -> None:
        return


class McpManagementApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product_store = FakeProductStore()
        self.mcp_store = InMemoryMcpStore()
        app = ProductAPIApp(
            auth=FakeOwnerAuth(),  # type: ignore[arg-type]
            credentials=object(),  # type: ignore[arg-type]
            store=self.product_store,  # type: ignore[arg-type]
            agent_store=object(),  # type: ignore[arg-type]
            agent_auth=object(),  # type: ignore[arg-type]
            mcp_store=self.mcp_store,  # type: ignore[arg-type]
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietProductAPIHandler)
        self.server.genos_app = app  # type: ignore[attr-defined]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = OWNER_SESSION,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers: dict[str, str] = {}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, {key.lower(): value for key, value in response.getheaders()}
        finally:
            connection.close()

    def test_every_mcp_management_route_requires_owner_authentication(self) -> None:
        principal_id = "00000000-0000-4000-8000-000000000001"
        upstream_id = "10000000-0000-4000-8000-000000000001"
        requests = [
            ("GET", "/api/v1/mcp", None),
            ("GET", "/api/v1/mcp/principals", None),
            ("GET", "/api/v1/mcp/upstreams", None),
            ("GET", "/api/v1/mcp/audit", None),
            ("POST", "/api/v1/mcp/principals", {"name": "agent", "scopes": ["genos.status"]}),
            ("POST", f"/api/v1/mcp/principals/{principal_id}/rotate", None),
            ("POST", f"/api/v1/mcp/principals/{principal_id}/revoke", None),
            ("POST", f"/api/v1/mcp/principals/{principal_id}/scopes", {"scopes": []}),
            (
                "POST",
                "/api/v1/mcp/upstreams",
                {"namespace": "github", "name": "GitHub", "endpoint": "https://mcp.example.test"},
            ),
            ("POST", f"/api/v1/mcp/upstreams/{upstream_id}/disable", None),
        ]
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                status, payload, headers = self.request(method, path, body=body, token=None)
                self.assertEqual(status, 401)
                self.assertEqual(payload, {"error": "unauthorized"})
                self.assertEqual(headers["cache-control"], "no-store")

    def test_principal_create_list_scope_rotate_and_revoke_keep_tokens_one_time_only(self) -> None:
        status, created, headers = self.request(
            "POST",
            "/api/v1/mcp/principals",
            body={"name": "desktop-agent", "scopes": ["github.*", "genos.status", "genos.status"]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["cache-control"], "no-store")
        issued = created["mcp"]
        first_token = issued["access_token"]
        principal_id = issued["principal"]["principal_id"]
        self.assertEqual(issued["token_visibility"], "ONE_TIME_ONLY")
        self.assertEqual(issued["principal"]["scopes"], ["genos.status", "github.*"])
        self.assertNotIn("token_hash", issued["principal"])

        status, listed, _ = self.request("GET", "/api/v1/mcp/principals")
        self.assertEqual(status, 200)
        self.assertEqual(listed["principals"], [issued["principal"]])
        self.assertNotIn(first_token, json.dumps(listed, sort_keys=True))
        self.assertNotIn(first_token, json.dumps(self.mcp_store.private_snapshot(), sort_keys=True))

        status, scoped, _ = self.request(
            "POST",
            f"/api/v1/mcp/principals/{principal_id}/scopes",
            body={"scopes": ["gdrive.files.read", "genos.status"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(scoped["principal"]["scopes"], ["gdrive.files.read", "genos.status"])
        self.assertNotIn("access_token", scoped)

        status, rotated, headers = self.request(
            "POST", f"/api/v1/mcp/principals/{principal_id}/rotate"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        second_token = rotated["mcp"]["access_token"]
        self.assertNotEqual(second_token, first_token)
        self.assertEqual(rotated["mcp"]["token_visibility"], "ONE_TIME_ONLY")
        self.assertNotIn("token_hash", rotated["mcp"]["principal"])

        status, after_rotate, _ = self.request("GET", "/api/v1/mcp/principals")
        self.assertEqual(status, 200)
        serialized_public = json.dumps(after_rotate, sort_keys=True)
        serialized_private = json.dumps(self.mcp_store.private_snapshot(), sort_keys=True)
        self.assertNotIn(first_token, serialized_public)
        self.assertNotIn(second_token, serialized_public)
        self.assertNotIn(first_token, serialized_private)
        self.assertNotIn(second_token, serialized_private)

        status, revoked, _ = self.request(
            "POST", f"/api/v1/mcp/principals/{principal_id}/revoke"
        )
        self.assertEqual(status, 200)
        self.assertEqual(revoked["principal"]["status"], "REVOKED")
        self.assertNotIn("access_token", revoked)

    def test_upstream_registration_accepts_only_active_mcp_hub_secretrefs_and_never_raw_secret(self) -> None:
        allowed_id = "20000000-0000-4000-8000-000000000001"
        wrong_scope_id = "20000000-0000-4000-8000-000000000002"
        disabled_id = "20000000-0000-4000-8000-000000000003"
        self.product_store.credentials = {
            allowed_id: FakeCredential("ACTIVE", ("mcp-hub",)),
            wrong_scope_id: FakeCredential("ACTIVE", ("drive-sync",)),
            disabled_id: FakeCredential("DISABLED", ("mcp-hub",)),
        }

        base_body = {
            "namespace": "github",
            "name": "GitHub MCP",
            "endpoint": "https://mcp.example.test/api",
        }
        for rejected_id in (wrong_scope_id, disabled_id):
            with self.subTest(secret_id=rejected_id):
                status, payload, _ = self.request(
                    "POST", "/api/v1/mcp/upstreams", body={**base_body, "secret_id": rejected_id}
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": "invalid_request"})
                self.assertEqual(self.mcp_store.list_upstreams(), [])

        raw_secret = "github-raw-secret-must-never-cross-the-api"
        status, rejected, _ = self.request(
            "POST",
            "/api/v1/mcp/upstreams",
            body={**base_body, "secret_id": allowed_id, "secret": raw_secret},
        )
        self.assertEqual(status, 400)
        self.assertEqual(rejected, {"error": "invalid_request"})
        self.assertEqual(self.mcp_store.list_upstreams(), [])

        status, registered, _ = self.request(
            "POST",
            "/api/v1/mcp/upstreams",
            body={**base_body, "secret_id": allowed_id},
        )
        self.assertEqual(status, 201)
        upstream = registered["upstream"]
        upstream_id = upstream["upstream_id"]
        self.assertEqual(upstream["secret_id"], allowed_id)
        self.assertEqual(upstream["status"], "ACTIVE")
        self.assertNotIn(raw_secret, json.dumps(registered, sort_keys=True))
        self.assertNotIn(raw_secret, json.dumps(self.mcp_store.private_snapshot(), sort_keys=True))

        status, listed, _ = self.request("GET", "/api/v1/mcp/upstreams")
        self.assertEqual(status, 200)
        self.assertEqual(listed["upstreams"], [upstream])
        self.assertNotIn(raw_secret, json.dumps(listed, sort_keys=True))

        status, disabled, _ = self.request(
            "POST", f"/api/v1/mcp/upstreams/{upstream_id}/disable"
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled["upstream"]["status"], "DISABLED")
        self.assertEqual(disabled["upstream"]["secret_id"], allowed_id)


if __name__ == "__main__":
    unittest.main()
