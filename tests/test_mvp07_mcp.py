from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread
import json
import unittest
import urllib.error
import urllib.request

from genos.mcp_hub import GenOSMcpHub, McpForbidden, McpUpstreamError, ToolSpec
from genos.mcp_store import scope_allows
from genos.mcp_transport import McpHttpHandler


class FakeStore:
    def __init__(self) -> None:
        self.active = True
        self.principal = {
            "principal_id": "11111111-1111-4111-8111-111111111111",
            "name": "fixture-agent",
            "status": "ACTIVE",
            "scopes": ["genos.status", "github.read_issue"],
        }
        self.upstreams = [
            {
                "upstream_id": "22222222-2222-4222-8222-222222222222",
                "namespace": "github",
                "name": "GitHub fixture",
                "endpoint": "https://fixture.invalid/mcp",
                "secret_id": None,
                "status": "ACTIVE",
                "last_state": "UNKNOWN",
            }
        ]
        self.audit_rows = []
        self.states = {}

    def authenticate(self, token):
        return self.principal if self.active and token == "fixture-token" else None

    def list_principals(self):
        return [self.principal]

    def list_upstreams(self, active_only=False):
        if active_only:
            return [item for item in self.upstreams if item["status"] == "ACTIVE"]
        return list(self.upstreams)

    def mark_upstream(self, upstream_id, *, state):
        self.states[upstream_id] = state

    def audit(self, **kwargs):
        self.audit_rows.append(kwargs)


class FakeUpstream:
    def __init__(self) -> None:
        self.fail = False

    def list_tools(self, upstream, access_token):
        if self.fail:
            raise McpUpstreamError("offline")
        return [
            {"name": "read_issue", "description": "read", "inputSchema": {"type": "object"}},
            {"name": "delete_repo", "description": "delete", "inputSchema": {"type": "object"}},
        ]

    def call_tool(self, upstream, access_token, *, name, arguments):
        if self.fail:
            raise McpUpstreamError("offline")
        return {"content": [{"type": "text", "text": f"{name}:ok"}], "isError": False}


class McpHubContractTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.upstream = FakeUpstream()
        local = {
            "genos.status": ToolSpec(
                "genos.status", "status", {"type": "object", "additionalProperties": False}, lambda _a: {"status": "ok"}
            ),
            "genos.secret": ToolSpec(
                "genos.secret", "not granted", {"type": "object"}, lambda _a: {"should": "not run"}
            ),
        }
        self.hub = GenOSMcpHub(store=self.store, local_tools=local, upstream_client=self.upstream)

    def test_scope_matching_is_exact_or_namespace_wildcard(self):
        self.assertTrue(scope_allows(["genos.*"], "genos.status"))
        self.assertTrue(scope_allows(["github.read_issue"], "github.read_issue"))
        self.assertFalse(scope_allows(["github.read_issue"], "github.delete_repo"))
        self.assertFalse(scope_allows(["genos.status"], "genos.status.extra"))

    def test_discovery_filters_local_and_federated_tools_by_grant(self):
        tools = self.hub.list_tools(self.store.principal)["tools"]
        names = [item["name"] for item in tools]
        self.assertEqual(names, ["genos.status", "github.read_issue"])
        self.assertNotIn("genos.secret", names)
        self.assertNotIn("github.delete_repo", names)

    def test_denied_tool_never_dispatches(self):
        with self.assertRaises(McpForbidden):
            self.hub.call_tool(self.store.principal, name="genos.secret", arguments={})
        self.assertEqual(self.store.audit_rows[-1]["decision"], "DENY")

    def test_upstream_failure_isolated_from_local_tools(self):
        self.upstream.fail = True
        tools = self.hub.list_tools(self.store.principal)["tools"]
        self.assertEqual([item["name"] for item in tools], ["genos.status"])
        local = self.hub.call_tool(self.store.principal, name="genos.status", arguments={})
        self.assertFalse(local["isError"])
        self.assertEqual(self.store.states["22222222-2222-4222-8222-222222222222"], "DEGRADED")

    def test_streamable_http_auth_headers_and_revoke(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), McpHttpHandler)
        server.genos_mcp_hub = self.hub
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                    "Authorization": "Bearer fixture-token",
                },
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                body = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual([item["name"] for item in body["result"]["tools"]], ["genos.status", "github.read_issue"])

            bad = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Authorization": "Bearer fixture-token",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(bad, timeout=3)
            self.assertEqual(caught.exception.code, 400)

            self.store.active = False
            with self.assertRaises(urllib.error.HTTPError) as revoked:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(revoked.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
