from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{path}: anchor count={text.count(old)} for {old[:80]!r}')
    p.write_text(text.replace(old, new), encoding='utf-8')


# Modern MCP requests carry version/client envelope in params._meta.
replace_once(
    'src/genos/mcp_hub.py',
    '''        body = {\n            "jsonrpc": "2.0",\n            "id": request_id,\n            "method": method,\n            "params": params,\n            "_meta": {"io.modelcontextprotocol/clientInfo": {"name": "genos-mcp-hub", "version": "0.1"}},\n        }\n''',
    '''        request_params = dict(params)\n        current_meta = request_params.get("_meta")\n        meta = dict(current_meta) if isinstance(current_meta, dict) else {}\n        meta.update(\n            {\n                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,\n                "io.modelcontextprotocol/clientInfo": {"name": "genos-mcp-hub", "version": "0.1"},\n                "io.modelcontextprotocol/clientCapabilities": {},\n            }\n        )\n        request_params["_meta"] = meta\n        body = {\n            "jsonrpc": "2.0",\n            "id": request_id,\n            "method": method,\n            "params": request_params,\n        }\n''',
)
replace_once(
    'src/genos/mcp_hub.py',
    '''    def discover(self, principal: dict[str, Any]) -> dict[str, Any]:\n        return {\n            "protocolVersion": MCP_PROTOCOL_VERSION,\n            "serverInfo": {"name": "genos-mcp-hub", "version": "0.1"},\n            "capabilities": {"tools": {"listChanged": False}},\n            "authorization": {"principal_id": principal["principal_id"], "grant_mode": "deny-by-default"},\n        }\n''',
    '''    def discover(self, principal: dict[str, Any]) -> dict[str, Any]:\n        return {\n            "supportedVersions": [MCP_PROTOCOL_VERSION],\n            "capabilities": {"tools": {"listChanged": False}},\n            "instructions": "GenOS unified MCP Hub. Tool discovery and invocation are filtered by the authenticated principal grant.",\n        }\n''',
)

# Enforce modern envelope/header validation and stamp server identity in result metadata.
replace_once(
    'src/genos/mcp_transport.py',
    '''MAX_MCP_BODY = 1024 * 1024\n\n\nclass McpHttpHandler''',
    '''MAX_MCP_BODY = 1024 * 1024\nPROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"\nSERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"\n\n\nclass McpHeaderMismatch(McpHubError):\n    pass\n\n\nclass McpUnsupportedProtocol(McpHubError):\n    pass\n\n\nclass McpHttpHandler''',
)
replace_once(
    'src/genos/mcp_transport.py',
    '''            header_version = self.headers.get("MCP-Protocol-Version", "")\n            header_method = self.headers.get("Mcp-Method", "")\n            if header_version != MCP_PROTOCOL_VERSION:\n                raise McpHubError("unsupported MCP protocol version")\n            if header_method != method:\n                raise McpHubError("Mcp-Method header/body mismatch")\n            params = payload.get("params") or {}\n            if not isinstance(params, dict):\n                raise McpHubError("MCP params must be an object")\n            principal = self.hub.authenticate(self._bearer_token())\n''',
    '''            header_version = self.headers.get("MCP-Protocol-Version", "")\n            header_method = self.headers.get("Mcp-Method", "")\n            if header_version != MCP_PROTOCOL_VERSION:\n                raise McpUnsupportedProtocol("unsupported MCP protocol version")\n            if header_method != method:\n                raise McpHeaderMismatch("Mcp-Method header/body mismatch")\n            params = payload.get("params") or {}\n            if not isinstance(params, dict):\n                raise McpHubError("MCP params must be an object")\n            meta = params.get("_meta")\n            if not isinstance(meta, dict) or meta.get(PROTOCOL_VERSION_META_KEY) != MCP_PROTOCOL_VERSION:\n                raise McpUnsupportedProtocol("MCP params._meta protocol version missing or unsupported")\n            principal = self.hub.authenticate(self._bearer_token())\n''',
)
replace_once(
    'src/genos/mcp_transport.py',
    '''                if not isinstance(body_name, str) or not body_name or header_name != body_name:\n                    raise McpHubError("Mcp-Name header/body mismatch")\n''',
    '''                if not isinstance(body_name, str) or not body_name or header_name != body_name:\n                    raise McpHeaderMismatch("Mcp-Name header/body mismatch")\n''',
)
replace_once(
    'src/genos/mcp_transport.py',
    '''            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})\n        except McpUnauthorized:\n''',
    '''            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": _with_server_meta(result)})\n        except McpUnsupportedProtocol:\n            self._rpc_error(request_id, -32022, "unsupported_protocol_version", status=400)\n        except McpHeaderMismatch:\n            self._rpc_error(request_id, -32020, "header_mismatch", status=400)\n        except McpUnauthorized:\n''',
)
replace_once(
    'src/genos/mcp_transport.py',
    '''def serve_mcp(*, host: str = "127.0.0.1", port: int) -> int:\n''',
    '''def _with_server_meta(result: Any) -> Any:\n    if not isinstance(result, dict):\n        return result\n    existing = result.get("_meta")\n    meta = dict(existing) if isinstance(existing, dict) else {}\n    meta[SERVER_INFO_META_KEY] = {"name": "genos-mcp-hub", "version": "0.1"}\n    return {**result, "_meta": meta}\n\n\ndef serve_mcp(*, host: str = "127.0.0.1", port: int) -> int:\n''',
)

# Tests and fresh-host probe must send a valid modern envelope.
replace_once(
    'tests/test_mvp07_mcp.py',
    '''            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}\n''',
    '''            payload = {\n                "jsonrpc": "2.0",\n                "id": 1,\n                "method": "tools/list",\n                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},\n            }\n''',
)
replace_once(
    'ci/fresh-host-e2e.sh',
    "mcp_body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode()\n",
    "mcp_body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{'_meta':{'io.modelcontextprotocol/protocolVersion':'2026-07-28'}}}).encode()\n",
)
