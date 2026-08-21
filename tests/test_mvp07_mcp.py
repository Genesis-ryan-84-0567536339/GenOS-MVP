from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import base64
import hashlib
from importlib import resources
import json
import time
import unittest
import urllib.error
import urllib.request

from genos.mcp_hub import (
    GenOSMcpHub,
    McpForbidden,
    McpUpstreamError,
    ToolSpec,
    UpstreamMcpClient,
    _validate_tool_arguments,
)
from genos.mcp_store import McpStoreError, normalize_endpoint, scope_allows
from genos.mcp_transport import GENOS_RATE_LIMITED, McpHttpHandler
from genos.secret_provider import SecretProviderError


PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"


def valid_meta(*, client_info=True, version=PROTOCOL_VERSION):
    result = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if client_info:
        result["io.modelcontextprotocol/clientInfo"] = {"name": "genos-test-client", "version": "1.0"}
    return result


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
        self.fail_terminal_audit = False
        self.fail_mark_healthy = False

    def authenticate(self, token):
        return self.principal if self.active and token == "fixture-token" else None

    def list_principals(self):
        return [self.principal]

    def list_upstreams(self, active_only=False):
        if active_only:
            return [item for item in self.upstreams if item["status"] == "ACTIVE"]
        return list(self.upstreams)

    def mark_upstream(self, upstream_id, *, state):
        if self.fail_mark_healthy and state == "HEALTHY":
            raise RuntimeError("fixture health persistence failed")
        self.states[upstream_id] = state

    def audit(self, **kwargs):
        if self.fail_terminal_audit and kwargs.get("result_class") not in {"DISPATCH", "FORBIDDEN", "UNAUTHORIZED"}:
            raise RuntimeError("fixture terminal audit failure")
        self.audit_rows.append(kwargs)


class FakeUpstream:
    def __init__(self) -> None:
        self.fail = False
        self.calls = []
        self.input_required = False

    def list_tools(self, upstream, access_token):
        if self.fail:
            raise McpUpstreamError("offline")
        return [
            {"name": "read_issue", "description": "read", "inputSchema": {"type": "object"}},
            {"name": "delete_repo", "description": "delete", "inputSchema": {"type": "object"}},
        ]

    def call_tool(
        self,
        upstream,
        access_token,
        *,
        name,
        arguments,
        input_responses=None,
        request_state=None,
        before_dispatch=None,
    ):
        if self.fail:
            raise McpUpstreamError("offline")
        if before_dispatch is not None:
            before_dispatch()
        self.calls.append(
            {
                "name": name,
                "arguments": arguments,
                "input_responses": input_responses,
                "request_state": request_state,
            }
        )
        if self.input_required and request_state is None:
            return {"resultType": "input_required", "requestState": "raw-upstream-state"}
        if self.input_required and request_state != "raw-upstream-state":
            raise McpUpstreamError("invalid upstream continuation")
        return {"resultType": "complete", "content": [{"type": "text", "text": f"{name}:ok"}], "isError": False}


class BrokenCredentials:
    def get_secret_for_consumer(self, secret_id, *, consumer):
        raise SecretProviderError("fixture secret material missing")


class FixtureUpstreamHandler(BaseHTTPRequestHandler):
    seen = []
    wrong_id = False
    malformed_result = False
    custom_result = None
    read_schema_override = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.seen.append({"headers": dict(self.headers.items()), "body": body})
        request_id = "wrong" if self.__class__.wrong_id else body["id"]
        if body["method"] == "tools/list":
            cursor = body["params"].get("cursor")
            if cursor is None:
                read_schema = type(self).read_schema_override or {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer", "x-mcp-header": "Issue-Number"},
                        "route": {
                            "type": "object",
                            "properties": {
                                "region": {"type": "string", "x-mcp-header": "Region"},
                                "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
                            },
                            "required": ["region", "enabled"],
                            "additionalProperties": False,
                        },
                        "payload": {
                            "type": "object",
                            "default": {"x-mcp-header": "literal-instance-data"},
                        },
                    },
                    "required": ["number", "route"],
                    "additionalProperties": False,
                }
                tools = [
                    {
                        "name": "read_issue",
                        "description": "read",
                        "inputSchema": read_schema,
                    }
                ]
                next_cursor = "page-2"
            elif cursor == "page-2":
                tools = [
                    {"name": "resume_tool", "inputSchema": {"type": "object", "additionalProperties": False}},
                    {
                        "name": "invalid_header_tool",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "number", "x-mcp-header": "Invalid"}},
                        },
                    },
                ]
                next_cursor = None
            else:
                raise ValueError("unexpected cursor")
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "tools": tools,
                    "ttlMs": 60_000,
                    "cacheScope": "private",
                },
            }
            if next_cursor is not None:
                payload["result"]["nextCursor"] = next_cursor
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        params = body["params"]
        if self.__class__.custom_result is not None:
            result = self.__class__.custom_result
        elif params["name"] == "resume_tool" and params.get("requestState") is None:
            result = {"resultType": "input_required", "requestState": "resume-1"}
        elif params["name"] == "resume_tool":
            if params.get("requestState") != "resume-1" or "inputResponses" in params:
                raise ValueError("invalid continuation")
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "resumed"}],
                "isError": False,
            }
        elif self.__class__.malformed_result:
            result = {"resultType": "complete", "isError": False}
        else:
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "fixture-ok"}],
                "isError": False,
            }
        final = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 0.5}}
        raw = (
            ": keepalive\n\n"
            + "data: "
            + json.dumps(progress, separators=(",", ":"))
            + "\n\n"
            + "data: "
            + json.dumps(final, separators=(",", ":"))
            + "\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _fmt, *_args):
        return


class RedirectTargetHandler(BaseHTTPRequestHandler):
    authorization_headers = []

    def do_POST(self):  # noqa: N802
        type(self).authorization_headers.append(self.headers.get("Authorization"))
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _fmt, *_args):
        return


class RedirectSourceHandler(BaseHTTPRequestHandler):
    target = ""

    def do_POST(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _fmt, *_args):
        return


class OpenSseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release = Event()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request_id = json.loads(self.rfile.read(length))["id"]
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "resultType": "complete",
                "tools": [],
                "ttlMs": 0,
                "cacheScope": "private",
            },
        }
        raw = ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()
        type(self).release.wait(timeout=2)

    def log_message(self, _fmt, *_args):
        return


class SchemaFetchProbeHandler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        raw = json.dumps({"type": "object"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/schema+json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _fmt, *_args):
        return


def start_server(handler, *, hub=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if hub is not None:
        server.genos_mcp_hub = hub
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def read_error_json(caught):
    try:
        return json.loads(caught.exception.read())
    finally:
        caught.exception.close()


def rpc_request(
    endpoint,
    *,
    method,
    params=None,
    token="fixture-token",
    version=PROTOCOL_VERSION,
    headers=None,
    request_id=1,
):
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"_meta": valid_meta(version=version), **(params or {})},
    }
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
        "Authorization": f"Bearer {token}",
    }
    if method == "tools/call" and isinstance(body["params"].get("name"), str):
        request_headers["Mcp-Name"] = body["params"]["name"]
    request_headers.update(headers or {})
    return urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )


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
        with self.assertRaises(McpStoreError):
            normalize_endpoint("https://mcp.example.test/api?access_token=raw-secret")

    def test_bundled_protocol_schema_is_exact_official_2026_release(self):
        raw = resources.files("genos").joinpath("schemas/mcp_2026_07_28.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "ef70b61f99b6d2e5e3b46863822eab08dff6a45bedc7a08914e0e5b133f40203",
        )

    def test_tool_schema_external_references_are_rejected_without_network(self):
        SchemaFetchProbeHandler.hits = []
        server, thread = start_server(SchemaFetchProbeHandler)
        try:
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                with self.subTest(keyword=keyword):
                    schema = {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "allOf": [{keyword: f"http://127.0.0.1:{server.server_port}/{keyword[1:]}"}],
                    }
                    self.assertIsNotNone(_validate_tool_arguments(schema, {}))

            unsupported_dialect = {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$ref": f"http://127.0.0.1:{server.server_port}/draft-07",
            }
            self.assertIsNotNone(_validate_tool_arguments(unsupported_dialect, {}))
            self.assertEqual(SchemaFetchProbeHandler.hits, [])

            local_reference = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": {"payload": {"type": "object", "additionalProperties": False}},
                "$ref": "#/$defs/payload",
            }
            self.assertIsNone(_validate_tool_arguments(local_reference, {}))
            self.assertIsNotNone(_validate_tool_arguments(local_reference, {"unexpected": True}))

            dangling_cases = [
                (
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$ref": "#/missing",
                    },
                    {},
                ),
                (
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "properties": {
                            "payload": {
                                "$id": "https://schema.invalid/nested",
                                "$ref": "#/missing",
                            }
                        },
                        "required": ["payload"],
                    },
                    {"payload": {}},
                ),
                (
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$dynamicRef": "#missing",
                    },
                    {},
                ),
            ]
            for dangling_schema, arguments in dangling_cases:
                with self.subTest(dangling_schema=dangling_schema):
                    self.assertIsNotNone(_validate_tool_arguments(dangling_schema, arguments))
            self.assertEqual(SchemaFetchProbeHandler.hits, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dangling_upstream_schema_reference_never_dispatches_and_is_audited(self):
        FixtureUpstreamHandler.seen = []
        FixtureUpstreamHandler.wrong_id = False
        FixtureUpstreamHandler.malformed_result = False
        FixtureUpstreamHandler.custom_result = None
        FixtureUpstreamHandler.read_schema_override = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "$ref": "#/missing",
        }
        server, thread = start_server(FixtureUpstreamHandler)
        try:
            self.store.upstreams[0]["endpoint"] = f"http://127.0.0.1:{server.server_port}/mcp"
            hub = GenOSMcpHub(
                store=self.store,
                local_tools={},
                upstream_client=UpstreamMcpClient(timeout=3),
            )
            listed = hub.list_tools(self.store.principal)
            self.assertEqual([tool["name"] for tool in listed["tools"]], ["github.read_issue"])
            seen_before_call = len(FixtureUpstreamHandler.seen)

            result = hub.call_tool(
                self.store.principal,
                name="github.read_issue",
                arguments={},
            )
            self.assertTrue(result["isError"])
            self.assertEqual(result["resultType"], "complete")
            self.assertEqual(len(FixtureUpstreamHandler.seen), seen_before_call)
            self.assertFalse(any(row["body"]["method"] == "tools/call" for row in FixtureUpstreamHandler.seen))
            self.assertTrue(
                any(
                    row["tool_name"] == "github.read_issue" and row["result_class"] == "TOOL_ERROR"
                    for row in self.store.audit_rows
                )
            )
        finally:
            FixtureUpstreamHandler.read_schema_override = None
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_discovery_filters_local_and_federated_tools_by_grant(self):
        result = self.hub.list_tools(self.store.principal)
        self.assertEqual(result["resultType"], "complete")
        self.assertGreaterEqual(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "private")
        tools = result["tools"]
        names = [item["name"] for item in tools]
        self.assertEqual(names, ["genos.status", "github.read_issue"])
        self.assertNotIn("genos.secret", names)
        self.assertNotIn("github.delete_repo", names)

    def test_all_success_results_use_2026_result_envelope(self):
        discover = self.hub.discover(self.store.principal)
        self.assertEqual(discover["resultType"], "complete")
        self.assertEqual(discover["supportedVersions"], [PROTOCOL_VERSION])
        self.assertEqual(discover["ttlMs"], 0)
        self.assertEqual(discover["cacheScope"], "private")
        called = self.hub.call_tool(self.store.principal, name="genos.status", arguments={})
        self.assertEqual(called["resultType"], "complete")
        self.assertFalse(called["isError"])

    def test_denied_tool_never_dispatches(self):
        with self.assertRaises(McpForbidden):
            self.hub.call_tool(self.store.principal, name="genos.secret", arguments={})
        self.assertEqual(self.store.audit_rows[-1]["decision"], "DENY")

    def test_invalid_local_tool_arguments_never_dispatch(self):
        calls = []
        self.hub.local_tools["genos.status"] = ToolSpec(
            "genos.status",
            "status",
            {"type": "object", "additionalProperties": False, "properties": {}},
            lambda _a: calls.append("called") or {"status": "ok"},
        )
        result = self.hub.call_tool(self.store.principal, name="genos.status", arguments={"unexpected": True})
        self.assertTrue(result["isError"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(calls, [])
        self.assertEqual(self.store.audit_rows[-1]["result_class"], "INVALID_ARGUMENTS")

    def test_upstream_failure_isolated_from_local_tools(self):
        self.upstream.fail = True
        tools = self.hub.list_tools(self.store.principal)["tools"]
        self.assertEqual([item["name"] for item in tools], ["genos.status"])
        local = self.hub.call_tool(self.store.principal, name="genos.status", arguments={})
        self.assertFalse(local["isError"])
        self.assertEqual(self.store.states["22222222-2222-4222-8222-222222222222"], "DEGRADED")

    def test_missing_secret_material_isolated_from_local_tools(self):
        self.store.upstreams[0]["secret_id"] = "33333333-3333-4333-8333-333333333333"
        self.hub.credentials = BrokenCredentials()
        tools = self.hub.list_tools(self.store.principal)["tools"]
        self.assertEqual([item["name"] for item in tools], ["genos.status"])
        self.assertEqual(self.store.states["22222222-2222-4222-8222-222222222222"], "DEGRADED")
        self.assertTrue(any(row["result_class"] == "UPSTREAM_DEGRADED" for row in self.store.audit_rows))

        with self.assertRaises(McpUpstreamError):
            self.hub.call_tool(self.store.principal, name="github.read_issue", arguments={"number": 1})
        remote_rows = [row for row in self.store.audit_rows if row["tool_name"] == "github.read_issue"]
        self.assertFalse(any(row["result_class"] == "DISPATCH" for row in remote_rows))
        self.assertTrue(any(row["result_class"] == "UPSTREAM_DEGRADED" for row in remote_rows))

    def test_pre_dispatch_audit_prevents_ambiguous_retry(self):
        calls = []
        self.hub.local_tools["genos.status"] = ToolSpec(
            "genos.status", "status", {"type": "object"}, lambda _a: calls.append("called") or {"status": "ok"}
        )
        self.store.fail_terminal_audit = True
        result = self.hub.call_tool(self.store.principal, name="genos.status", arguments={})
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(calls, ["called"])
        self.assertTrue(any(row["result_class"] == "DISPATCH" for row in self.store.audit_rows))

    def test_remote_health_projection_failure_does_not_make_completed_effect_ambiguous(self):
        self.store.fail_mark_healthy = True
        result = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 1},
        )
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(len(self.upstream.calls), 1)
        self.assertTrue(any(row["result_class"] == "PASS" for row in self.store.audit_rows))

    def test_federated_request_state_is_opaque_bound_and_single_use(self):
        self.upstream.input_required = True
        waiting = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 1},
        )
        self.assertEqual(waiting["resultType"], "input_required")
        self.assertTrue(waiting["requestState"].startswith("genos-mrtr:"))
        self.assertNotEqual(waiting["requestState"], "raw-upstream-state")

        rejected = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 2},
            request_state=waiting["requestState"],
        )
        self.assertTrue(rejected["isError"])
        self.assertEqual(len(self.upstream.calls), 1)

        resumed = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 1},
            request_state=waiting["requestState"],
        )
        self.assertEqual(resumed["resultType"], "complete")
        self.assertEqual(self.upstream.calls[-1]["request_state"], "raw-upstream-state")
        replay = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 1},
            request_state=waiting["requestState"],
        )
        self.assertTrue(replay["isError"])

        forbidden_responses = self.hub.call_tool(
            self.store.principal,
            name="github.read_issue",
            arguments={"number": 1},
            input_responses={"approval": {"action": "accept", "content": {}}},
        )
        self.assertTrue(forbidden_responses["isError"])

    def test_tool_calls_are_rate_limited_per_principal_before_dispatch(self):
        calls = []
        hub = GenOSMcpHub(
            store=self.store,
            local_tools={
                "genos.status": ToolSpec(
                    "genos.status",
                    "status",
                    {"type": "object", "additionalProperties": False},
                    lambda _arguments: calls.append("called") or {"status": "ok"},
                )
            },
            tool_rate_limit_per_minute=2,
        )
        server, thread = start_server(McpHttpHandler, hub=hub)
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            for _ in range(2):
                request = rpc_request(
                    endpoint,
                    method="tools/call",
                    params={"name": "genos.status", "arguments": {}},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            limited = rpc_request(
                endpoint,
                method="tools/call",
                params={"name": "genos.status", "arguments": {}},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(limited, timeout=3)
            try:
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(json.loads(caught.exception.read())["error"]["code"], GENOS_RATE_LIMITED)
            finally:
                caught.exception.close()
            self.assertEqual(calls, ["called", "called"])
            self.assertTrue(any(row["result_class"] == "RATE_LIMITED" for row in self.store.audit_rows))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_streamable_http_auth_headers_and_revoke(self):
        server, thread = start_server(McpHttpHandler, hub=self.hub)
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            discover_req = rpc_request(endpoint, method="server/discover")
            with urllib.request.urlopen(discover_req, timeout=3) as response:
                discover = json.load(response)["result"]
            self.assertEqual(discover["resultType"], "complete")
            self.assertEqual(discover["supportedVersions"], [PROTOCOL_VERSION])
            self.assertEqual(discover["cacheScope"], "private")
            self.assertEqual(discover["_meta"][SERVER_INFO_KEY]["name"], "genos-mcp-hub")

            req = rpc_request(endpoint, method="tools/list", headers={"X-Request-ID": "x" * 1024})
            with urllib.request.urlopen(req, timeout=3) as response:
                body = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(body["result"]["resultType"], "complete")
            self.assertEqual([item["name"] for item in body["result"]["tools"]], ["genos.status", "github.read_issue"])
            list_audit = next(row for row in reversed(self.store.audit_rows) if row["tool_name"] == "tools/list")
            self.assertLessEqual(len(list_audit["correlation_id"]), 128)

            secret_request_id = "api-key-like-secret-value"
            secret_header_req = rpc_request(
                endpoint,
                method="tools/list",
                headers={"X-Request-ID": secret_request_id},
            )
            with urllib.request.urlopen(secret_header_req, timeout=3):
                pass
            secret_audit = next(row for row in reversed(self.store.audit_rows) if row["tool_name"] == "tools/list")
            self.assertNotEqual(secret_audit["correlation_id"], secret_request_id)
            self.assertNotIn(secret_request_id, secret_audit["correlation_id"])
            self.assertTrue(secret_audit["correlation_id"].startswith("opaque-"))

            bad = rpc_request(endpoint, method="tools/list", headers={"Mcp-Method": "tools/call"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(bad, timeout=3)
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32020)

            self.store.active = False
            with self.assertRaises(urllib.error.HTTPError) as revoked:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(revoked.exception.code, 401)
            read_error_json(revoked)
            self.assertTrue(any(row["result_class"] == "UNAUTHORIZED" for row in self.store.audit_rows))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_request_metadata_and_version_errors_match_2026_contract(self):
        server, thread = start_server(McpHttpHandler, hub=self.hub)
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            missing_caps = rpc_request(endpoint, method="tools/list")
            payload = json.loads(missing_caps.data)
            del payload["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
            missing_caps.data = json.dumps(payload).encode("utf-8")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(missing_caps, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32602)

            no_client_info = rpc_request(endpoint, method="tools/list")
            payload = json.loads(no_client_info.data)
            del payload["params"]["_meta"]["io.modelcontextprotocol/clientInfo"]
            no_client_info.data = json.dumps(payload).encode("utf-8")
            with urllib.request.urlopen(no_client_info, timeout=3) as response:
                self.assertEqual(json.load(response)["result"]["resultType"], "complete")

            empty_client_info = rpc_request(endpoint, method="tools/list")
            payload = json.loads(empty_client_info.data)
            payload["params"]["_meta"]["io.modelcontextprotocol/clientInfo"] = {"name": "", "version": ""}
            empty_client_info.data = json.dumps(payload).encode("utf-8")
            with urllib.request.urlopen(empty_client_info, timeout=3) as response:
                self.assertEqual(json.load(response)["result"]["resultType"], "complete")

            malformed_info = rpc_request(endpoint, method="tools/list")
            payload = json.loads(malformed_info.data)
            payload["params"]["_meta"]["io.modelcontextprotocol/clientInfo"] = {"name": "missing-version"}
            malformed_info.data = json.dumps(payload).encode("utf-8")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(malformed_info, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32602)

            mismatch = rpc_request(endpoint, method="tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(mismatch, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32020)

            unsupported = rpc_request(endpoint, method="tools/list", version="2099-01-01")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(unsupported, timeout=3)
            error = read_error_json(caught)["error"]
            self.assertEqual(error["code"], -32022)
            self.assertEqual(error["data"], {"supported": [PROTOCOL_VERSION], "requested": "2099-01-01"})

            for meta_patch in (
                {"io.modelcontextprotocol/clientCapabilities": {"roots": "yes"}},
                {"io.modelcontextprotocol/logLevel": "verbose"},
                {"progressToken": True},
            ):
                malformed_meta = rpc_request(endpoint, method="tools/list")
                payload = json.loads(malformed_meta.data)
                payload["params"]["_meta"].update(meta_patch)
                malformed_meta.data = json.dumps(payload).encode("utf-8")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(malformed_meta, timeout=3)
                self.assertEqual(read_error_json(caught)["error"]["code"], -32602)

            empty_string_id = rpc_request(endpoint, method="tools/list", request_id="")
            with urllib.request.urlopen(empty_string_id, timeout=3) as response:
                self.assertEqual(json.load(response)["id"], "")

            fractional_id = rpc_request(endpoint, method="tools/list", request_id=1.5)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(fractional_id, timeout=3)
            try:
                body = json.loads(caught.exception.read())
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(body["error"]["code"], -32600)
                self.assertNotIn("id", body)
            finally:
                caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_request_shapes_routing_headers_and_media_type_are_strict(self):
        self.store.principal["scopes"].append("f")
        self.hub.local_tools["f"] = ToolSpec("f", "short", {"type": "object"}, lambda _a: "ok")
        server, thread = start_server(McpHttpHandler, hub=self.hub)
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            for cursor in (42, "stale-cursor"):
                request = rpc_request(endpoint, method="tools/list", params={"cursor": cursor})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(read_error_json(caught)["error"]["code"], -32602)

            invalid_responses = rpc_request(
                endpoint,
                method="tools/call",
                params={
                    "name": "genos.status",
                    "arguments": {},
                    "inputResponses": {"owner": {"action": "bogus"}},
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(invalid_responses, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32602)

            jsonp = rpc_request(endpoint, method="tools/list", headers={"Content-Type": "application/jsonp"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(jsonp, timeout=3)
            self.assertEqual(caught.exception.code, 400)
            read_error_json(caught)

            noncanonical = rpc_request(
                endpoint,
                method="tools/call",
                params={"name": "f", "arguments": {}},
                headers={"Mcp-Name": "=?base64?Zh==?="},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(noncanonical, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32020)

            missing_resource_name = rpc_request(
                endpoint,
                method="resources/read",
                params={"uri": "file:///safe/path"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(missing_resource_name, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32020)

            matched_resource_name = rpc_request(
                endpoint,
                method="resources/read",
                params={"uri": "file:///safe/path"},
                headers={"Mcp-Name": "file:///safe/path"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(matched_resource_name, timeout=3)
            self.assertEqual(read_error_json(caught)["error"]["code"], -32601)

            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/list",
                    "params": {"_meta": valid_meta()},
                }
            ).encode("utf-8")
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.putrequest("POST", "/mcp")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("MCP-Protocol-Version", PROTOCOL_VERSION)
                connection.putheader("MCP-Protocol-Version", PROTOCOL_VERSION)
                connection.putheader("Mcp-Method", "tools/list")
                connection.putheader("Authorization", "Bearer fixture-token")
                connection.endheaders(body)
                response = connection.getresponse()
                duplicate_error = json.loads(response.read())
                self.assertEqual(response.status, 400)
                self.assertEqual(duplicate_error["error"]["code"], -32020)
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_origin_validation_and_base64_mcp_name(self):
        unicode_name = "genos.đọc"
        self.store.principal["scopes"].append(unicode_name)
        self.hub.local_tools[unicode_name] = ToolSpec(unicode_name, "unicode", {"type": "object"}, lambda _a: "ok")
        server, thread = start_server(McpHttpHandler, hub=self.hub)
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        try:
            hostile = rpc_request(endpoint, method="tools/list", headers={"Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(hostile, timeout=3)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("BREW", "/mcp", headers={"Origin": "https://evil.example"})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
            finally:
                connection.close()

            encoded = "=?base64?" + base64.b64encode(unicode_name.encode("utf-8")).decode("ascii") + "?="
            call = rpc_request(
                endpoint,
                method="tools/call",
                params={"name": unicode_name, "arguments": {}},
                headers={"Mcp-Name": encoded, "Origin": "http://localhost:3000"},
            )
            with urllib.request.urlopen(call, timeout=3) as response:
                result = json.load(response)["result"]
            self.assertEqual(result["resultType"], "complete")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upstream_client_accepts_json_and_request_scoped_sse(self):
        FixtureUpstreamHandler.seen = []
        FixtureUpstreamHandler.wrong_id = False
        FixtureUpstreamHandler.malformed_result = False
        FixtureUpstreamHandler.custom_result = None
        server, thread = start_server(FixtureUpstreamHandler)
        upstream = {"endpoint": f"http://127.0.0.1:{server.server_port}/mcp"}
        try:
            client = UpstreamMcpClient(timeout=3)
            tools = client.list_tools(upstream, "upstream-token")
            self.assertEqual([item["name"] for item in tools], ["read_issue", "resume_tool"])
            read_schema = tools[0]["inputSchema"]
            self.assertNotIn("x-mcp-header", read_schema["properties"]["number"])
            self.assertNotIn("x-mcp-header", read_schema["properties"]["route"]["properties"]["region"])
            self.assertNotIn("x-mcp-header", read_schema["properties"]["route"]["properties"]["enabled"])
            self.assertEqual(
                read_schema["properties"]["payload"]["default"],
                {"x-mcp-header": "literal-instance-data"},
            )
            seen_before_invalid = len(FixtureUpstreamHandler.seen)
            invalid = client.call_tool(
                upstream,
                "upstream-token",
                name="read_issue",
                arguments={"number": 1},
            )
            self.assertTrue(invalid["isError"])
            self.assertEqual(len(FixtureUpstreamHandler.seen), seen_before_invalid)
            result = client.call_tool(
                upstream,
                "upstream-token",
                name="read_issue",
                arguments={"number": 1, "route": {"region": "Hà Nội", "enabled": False}},
            )
            self.assertEqual(result["resultType"], "complete")
            self.assertEqual(result["content"][0]["text"], "fixture-ok")
            call_headers = FixtureUpstreamHandler.seen[-1]["headers"]
            self.assertEqual(call_headers["Mcp-Param-Issue-Number"], "1")
            self.assertEqual(call_headers["Mcp-Param-Enabled"], "false")
            expected_region = "=?base64?" + base64.b64encode("Hà Nội".encode()).decode() + "?="
            self.assertEqual(call_headers["Mcp-Param-Region"], expected_region)

            seen_before_rotated_token = len(FixtureUpstreamHandler.seen)
            rotated = client.call_tool(
                upstream,
                "rotated-upstream-token",
                name="read_issue",
                arguments={"number": 2, "route": {"region": "x", "enabled": True}},
            )
            self.assertEqual(rotated["resultType"], "complete")
            rotated_calls = FixtureUpstreamHandler.seen[seen_before_rotated_token:]
            self.assertGreaterEqual(len(rotated_calls), 3)
            self.assertEqual(rotated_calls[0]["body"]["method"], "tools/list")
            self.assertEqual(rotated_calls[0]["headers"]["Authorization"], "Bearer rotated-upstream-token")

            waiting = client.call_tool(upstream, None, name="resume_tool", arguments={})
            self.assertEqual(waiting, {"resultType": "input_required", "requestState": "resume-1"})
            resumed = client.call_tool(
                upstream,
                None,
                name="resume_tool",
                arguments={},
                request_state=waiting["requestState"],
            )
            self.assertEqual(resumed["content"][0]["text"], "resumed")
            for seen in FixtureUpstreamHandler.seen:
                meta = seen["body"]["params"]["_meta"]
                self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], PROTOCOL_VERSION)
                self.assertIsInstance(meta["io.modelcontextprotocol/clientCapabilities"], dict)
                self.assertIn("application/json", seen["headers"]["Accept"])
                self.assertIn("text/event-stream", seen["headers"]["Accept"])
            self.assertNotIn("Mcp-Session-Id", FixtureUpstreamHandler.seen[-1]["headers"])

            FixtureUpstreamHandler.malformed_result = True
            with self.assertRaises(McpUpstreamError):
                client.call_tool(
                    upstream,
                    None,
                    name="read_issue",
                    arguments={"number": 1, "route": {"region": "x", "enabled": True}},
                )
            FixtureUpstreamHandler.malformed_result = False

            invalid_results = [
                {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "x", "annotations": "bad"}],
                },
                {
                    "resultType": "complete",
                    "content": [
                        {"type": "resource_link", "name": "x", "uri": "https://example.test", "size": "bad"}
                    ],
                },
                {"resultType": "input_required", "inputRequests": {}},
                {"resultType": "input_required", "requestState": "x", "ttlMs": 10, "cacheScope": "private"},
                {
                    "resultType": "input_required",
                    "inputRequests": {
                        "owner": {
                            "method": "elicitation/create",
                            "params": {
                                "mode": "form",
                                "message": "Approve",
                                "requestedSchema": {"type": "object", "properties": {}},
                            },
                        }
                    },
                },
                {
                    "resultType": "com.example/partial",
                    "content": [{"type": "text", "text": "extension not negotiated"}],
                },
            ]
            for invalid_result in invalid_results:
                FixtureUpstreamHandler.custom_result = invalid_result
                with self.assertRaises(McpUpstreamError):
                    client.call_tool(
                        upstream,
                        None,
                        name="read_issue",
                        arguments={"number": 1, "route": {"region": "x", "enabled": True}},
                    )
            FixtureUpstreamHandler.custom_result = None

            FixtureUpstreamHandler.wrong_id = True
            with self.assertRaises(McpUpstreamError):
                client.call_tool(
                    upstream,
                    None,
                    name="read_issue",
                    arguments={"number": 1, "route": {"region": "x", "enabled": True}},
                )
        finally:
            FixtureUpstreamHandler.custom_result = None
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upstream_redirect_cannot_exfiltrate_secretref_bearer(self):
        RedirectTargetHandler.authorization_headers = []
        target_server, target_thread = start_server(RedirectTargetHandler)
        source_server, source_thread = start_server(RedirectSourceHandler)
        RedirectSourceHandler.target = f"http://127.0.0.1:{target_server.server_port}/capture"
        try:
            client = UpstreamMcpClient(timeout=1)
            with self.assertRaises(McpUpstreamError):
                client.list_tools(
                    {"endpoint": f"http://127.0.0.1:{source_server.server_port}/mcp"},
                    "must-not-cross-redirect",
                )
            self.assertEqual(RedirectTargetHandler.authorization_headers, [])
        finally:
            source_server.shutdown()
            source_server.server_close()
            source_thread.join(timeout=3)
            target_server.shutdown()
            target_server.server_close()
            target_thread.join(timeout=3)

    def test_request_scoped_sse_returns_on_final_response_without_waiting_for_eof(self):
        OpenSseHandler.release.clear()
        server, thread = start_server(OpenSseHandler)
        try:
            started = time.monotonic()
            tools = UpstreamMcpClient(timeout=0.5).list_tools(
                {"endpoint": f"http://127.0.0.1:{server.server_port}/mcp"},
                None,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(tools, [])
            self.assertLess(elapsed, 0.5)
        finally:
            OpenSseHandler.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
