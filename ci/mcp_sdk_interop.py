#!/usr/bin/env python3
"""Tier-1 MCP 2026-07-28 interoperability gate for GenOS."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from importlib.metadata import version as distribution_version
from threading import Lock, Thread
from typing import Any
import sys
import traceback

import anyio
import httpx2
from mcp import types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from genos.mcp_hub import MCP_PROTOCOL_VERSION, GenOSMcpHub, ToolSpec
from genos.mcp_transport import PROTOCOL_VERSION_META_KEY, SERVER_INFO_META_KEY, McpHttpHandler


EXPECTED_SDK_VERSION = "2.0.0"
FIXTURE_TOKEN = "genos-mcp-sdk-fixture-token"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"


class FakeStore:
    def __init__(self) -> None:
        self.principal = {
            "principal_id": "11111111-1111-4111-8111-111111111111",
            "name": "official-sdk-probe",
            "status": "ACTIVE",
            "scopes": ["genos.status"],
        }
        self.authenticate_calls = 0
        self.bad_auth_seen = False
        self.audit_rows: list[dict[str, Any]] = []

    def authenticate(self, token: str) -> dict[str, Any] | None:
        self.authenticate_calls += 1
        if token != FIXTURE_TOKEN:
            self.bad_auth_seen = True
            return None
        return self.principal

    def list_upstreams(self, active_only: bool = False) -> list[dict[str, Any]]:
        return []

    def audit(self, **kwargs: Any) -> None:
        self.audit_rows.append(kwargs)


class RecordingMcpHttpHandler(McpHttpHandler):
    records: list[dict[str, Any]] = []
    records_lock = Lock()

    def _read_json(self) -> dict[str, Any]:
        payload = super()._read_json()
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        raw_meta = params.get("_meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        client_info = meta.get(CLIENT_INFO_META_KEY)
        record = {
            "method": payload.get("method"),
            "method_header": self.headers.get("Mcp-Method"),
            "protocol_header": self.headers.get("MCP-Protocol-Version"),
            "name_header": self.headers.get("Mcp-Name"),
            "body_name": params.get("name"),
            "session_header": self.headers.get("Mcp-Session-Id"),
            "authorization_ok": self.headers.get("Authorization") == f"Bearer {FIXTURE_TOKEN}",
            "protocol_meta": meta.get(PROTOCOL_VERSION_META_KEY),
            "client_info_present": CLIENT_INFO_META_KEY in meta,
            "client_info_name": client_info.get("name") if isinstance(client_info, dict) else None,
            "client_capabilities_present": CLIENT_CAPABILITIES_META_KEY in meta,
        }
        with type(self).records_lock:
            type(self).records.append(record)
        return payload


def assert_server_info(result: Any) -> None:
    meta = result.meta
    assert isinstance(meta, dict), "response omitted result._meta"
    server_info = meta.get(SERVER_INFO_META_KEY)
    assert isinstance(server_info, dict), "response omitted result._meta serverInfo"
    assert server_info.get("name") == "genos-mcp-hub"
    assert isinstance(server_info.get("version"), str) and server_info["version"]


def assert_wire_records(records: list[dict[str, Any]]) -> None:
    expected_methods = ["server/discover", "tools/list", "tools/call"]
    assert [record["method"] for record in records] == expected_methods
    for record in records:
        assert record["authorization_ok"], f"{record['method']} omitted bearer auth"
        assert record["protocol_header"] == MCP_PROTOCOL_VERSION
        assert record["protocol_meta"] == MCP_PROTOCOL_VERSION
        assert record["method_header"] == record["method"]
        assert record["session_header"] is None
        assert record["client_info_present"]
        assert record["client_info_name"] == "genos-ci-sdk-probe"
        assert record["client_capabilities_present"]
    assert [record["name_header"] for record in records] == [None, None, "genos.status"]
    assert [record["body_name"] for record in records] == [None, None, "genos.status"]


async def run_probe() -> None:
    RecordingMcpHttpHandler.records.clear()
    store = FakeStore()
    hub = GenOSMcpHub(
        store=store,
        local_tools={
            "genos.status": ToolSpec(
                name="genos.status",
                description="Return deterministic GenOS fixture status.",
                input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                handler=lambda _arguments: {"status": "ok"},
            )
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingMcpHttpHandler)
    server.daemon_threads = True
    server.genos_mcp_hub = hub  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, name="genos-mcp-sdk-probe", daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {FIXTURE_TOKEN}"}) as http_client:
            transport = streamable_http_client(endpoint, http_client=http_client, terminate_on_close=False)
            async with Client(
                transport,
                mode="auto",
                client_info=types.Implementation(name="genos-ci-sdk-probe", version="1.0"),
                read_timeout_seconds=5,
                cache=None,
            ) as client:
                assert client.protocol_version == MCP_PROTOCOL_VERSION
                assert client.server_info is not None and client.server_info.name == "genos-mcp-hub"
                discover = client.session.discover_result
                assert discover is not None
                assert discover.supported_versions == [MCP_PROTOCOL_VERSION]
                assert discover.result_type == "complete"
                assert "result_type" in discover.model_fields_set
                assert_server_info(discover)
                listed = await client.list_tools()
                assert listed.result_type == "complete"
                assert "result_type" in listed.model_fields_set
                assert [tool.name for tool in listed.tools] == ["genos.status"]
                assert_server_info(listed)
                called = await client.call_tool("genos.status", {})
                assert called.result_type == "complete"
                assert "result_type" in called.model_fields_set
                assert called.is_error is False
                assert called.structured_content == {"status": "ok"}
                assert_server_info(called)
        with RecordingMcpHttpHandler.records_lock:
            records = list(RecordingMcpHttpHandler.records)
        assert_wire_records(records)
        assert store.authenticate_calls == 3
        assert store.bad_auth_seen is False
        assert any(row["tool_name"] == "server/discover" and row["result_class"] == "PASS" for row in store.audit_rows)
        assert any(row["tool_name"] == "tools/list" and row["result_class"] == "PASS" for row in store.audit_rows)
        assert any(row["tool_name"] == "genos.status" and row["result_class"] == "DISPATCH" for row in store.audit_rows)
        assert any(row["tool_name"] == "genos.status" and row["result_class"] == "PASS" for row in store.audit_rows)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "fixture MCP server failed to stop"


def main() -> int:
    try:
        assert distribution_version("mcp") == EXPECTED_SDK_VERSION
        assert distribution_version("mcp-types") == EXPECTED_SDK_VERSION
        anyio.run(run_probe)
    except Exception:
        print("MCP_TIER1_PYTHON_SDK_INTEROP_FAIL", file=sys.stderr)
        traceback.print_exc()
        return 1
    print("MCP_TIER1_PYTHON_SDK_INTEROP_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
