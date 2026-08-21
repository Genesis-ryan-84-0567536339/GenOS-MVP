from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one anchor, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def patch_product_api() -> None:
    path = "src/genos/product_api.py"
    replace_once(
        path,
        "from .drive_system import DriveSystemError, DriveSystemServices, build_drive_system\n",
        "from .drive_system import DriveSystemError, DriveSystemServices, build_drive_system\n"
        "from .artifact_store import ArtifactStoreError\n"
        "from .drive_collab import DriveCollaborationError\n"
        "from .kanban import InvalidCardTransition, KanbanError, KanbanSystem, build_kanban_system\n"
        "from .kanban_store import CardConflict, CardNotFound, KanbanStoreError\n"
        "from .mcp_store import McpConflict, McpNotFound, McpStoreError, PostgresMcpStore\n",
    )
    replace_once(
        path,
        '_SYSTEM_REPORT = "/api/v1/reports/system"\n',
        '_SYSTEM_REPORT = "/api/v1/reports/system"\n'
        '_CARDS_BASE = "/api/v1/cards"\n'
        '_KANBAN_SYNC = "/api/v1/kanban/sync"\n'
        '_KANBAN_AGENT_TICK = "/api/v1/kanban/agent-tick"\n'
        '_MCP_BASE = "/api/v1/mcp"\n'
        '_MCP_PRINCIPALS = f"{_MCP_BASE}/principals"\n'
        '_MCP_UPSTREAMS = f"{_MCP_BASE}/upstreams"\n'
        '_MCP_AUDIT = f"{_MCP_BASE}/audit"\n'
        '_CARD_ITEM = re.compile(r"^/api/v1/cards/([0-9a-fA-F-]{36})$")\n'
        '_CARD_ACTION = re.compile(r"^/api/v1/cards/([0-9a-fA-F-]{36})/(transition|comment)$")\n'
        '_MCP_PRINCIPAL_ACTION = re.compile(r"^/api/v1/mcp/principals/([0-9a-fA-F-]{36})/(rotate|revoke|scopes)$")\n'
        '_MCP_UPSTREAM_ACTION = re.compile(r"^/api/v1/mcp/upstreams/([0-9a-fA-F-]{36})/(disable)$")\n',
    )
    replace_once(
        path,
        "        drive_system: DriveSystemServices | None = None,\n    ) -> None:\n",
        "        drive_system: DriveSystemServices | None = None,\n"
        "        kanban_system: KanbanSystem | None = None,\n"
        "        mcp_store: PostgresMcpStore | None = None,\n"
        "    ) -> None:\n",
    )
    replace_once(
        path,
        "        self.drive_system = drive_system\n",
        "        self.drive_system = drive_system\n"
        "        self.kanban_system = kanban_system\n"
        "        self.mcp_store = mcp_store\n",
    )
    replace_once(
        path,
        "        drive_system = build_drive_system(product_store=store, credentials=credentials, observability=observability)\n",
        "        drive_system = build_drive_system(product_store=store, credentials=credentials, observability=observability)\n"
        "        kanban_system = build_kanban_system(product_store=store, credentials=credentials, agent_root=agent_root)\n"
        "        mcp_store = PostgresMcpStore(store)\n"
        "        mcp_store.ensure_schema()\n",
    )
    replace_once(
        path,
        "            observability,\n            drive_system,\n        )\n",
        "            observability,\n"
        "            drive_system,\n"
        "            kanban_system,\n"
        "            mcp_store,\n"
        "        )\n",
    )
    replace_once(
        path,
        "    def publish_system_report(self, *, manual: bool = True) -> dict[str, Any]:\n        return self._drive().reports.publish(manual=manual)\n\n    def _drive(self) -> DriveSystemServices:\n",
        "    def publish_system_report(self, *, manual: bool = True) -> dict[str, Any]:\n"
        "        return self._drive().reports.publish(manual=manual)\n\n"
        "    def cards_list(self) -> list[dict[str, Any]]:\n"
        "        return self._kanban().list_cards()\n\n"
        "    def card_get(self, card_id: str) -> dict[str, Any]:\n"
        "        return self._kanban().get_card(card_id)\n\n"
        "    def card_create(self, *, title: str, description: str = \"\", assignee_agent_id: str | None = \"agy-gen\") -> dict[str, Any]:\n"
        "        return self._kanban().create_card(title=title, description=description, assignee_agent_id=assignee_agent_id)\n\n"
        "    def card_transition(self, card_id: str, *, to_state: str, reason: str = \"OWNER_ACTION\") -> dict[str, Any]:\n"
        "        return self._kanban().transition(card_id, to_state=to_state, reason=reason)\n\n"
        "    def card_comment(self, card_id: str, *, text: str) -> dict[str, Any]:\n"
        "        return self._kanban().add_comment(card_id, text=text)\n\n"
        "    def kanban_sync(self) -> dict[str, Any]:\n"
        "        return self._kanban().sync_drive_inbox()\n\n"
        "    def kanban_agent_tick(self) -> dict[str, Any]:\n"
        "        return self._kanban().agent_tick()\n\n"
        "    def mcp_status(self) -> dict[str, Any]:\n"
        "        store = self._mcp()\n"
        "        port = os.environ.get(\"GENOS_MCP_PORT\")\n"
        "        if not port:\n"
        "            port_path = Path(\"/etc/genos/mcp-port\")\n"
        "            port = port_path.read_text(encoding=\"utf-8\").strip() if port_path.is_file() else None\n"
        "        return {\"protocol_version\": \"2026-07-28\", \"endpoint\": f\"http://127.0.0.1:{port}/mcp\" if port else None, \"principal_count\": len(store.list_principals()), \"upstreams\": store.list_upstreams()}\n\n"
        "    def mcp_create_principal(self, *, name: str, scopes: list[str]) -> dict[str, Any]:\n"
        "        return self._mcp().create_principal(name=name, scopes=scopes).one_way_response()\n\n"
        "    def mcp_rotate_principal(self, principal_id: str) -> dict[str, Any]:\n"
        "        return self._mcp().rotate_principal(principal_id).one_way_response()\n\n"
        "    def mcp_revoke_principal(self, principal_id: str) -> dict[str, Any]:\n"
        "        return self._mcp().revoke_principal(principal_id)\n\n"
        "    def mcp_replace_scopes(self, principal_id: str, scopes: list[str]) -> dict[str, Any]:\n"
        "        return self._mcp().replace_scopes(principal_id, scopes)\n\n"
        "    def mcp_register_upstream(self, *, namespace: str, name: str, endpoint: str, secret_id: str | None) -> dict[str, Any]:\n"
        "        if secret_id:\n"
        "            record = self.store.get_credential(secret_id)\n"
        "            if record is None or record.status != \"ACTIVE\" or \"mcp-hub\" not in record.consumer_scopes:\n"
        "                raise CredentialError(\"SecretRef must be ACTIVE and granted to consumer mcp-hub\")\n"
        "        return self._mcp().register_upstream(namespace=namespace, name=name, endpoint=endpoint, secret_id=secret_id)\n\n"
        "    def _kanban(self) -> KanbanSystem:\n"
        "        if self.kanban_system is None:\n"
        "            raise KanbanError(\"Kanban system is not configured\")\n"
        "        return self.kanban_system\n\n"
        "    def _mcp(self) -> PostgresMcpStore:\n"
        "        if self.mcp_store is None:\n"
        "            raise McpStoreError(\"MCP store is not configured\")\n"
        "        return self.mcp_store\n\n"
        "    def _drive(self) -> DriveSystemServices:\n",
    )
    replace_once(
        path,
        "            if self.path == _DRIVE_BASE:\n",
        "            if self.path == _CARDS_BASE:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, {\"cards\": self.app.cards_list()})\n"
        "                return\n"
        "            card_item = _CARD_ITEM.match(self.path)\n"
        "            if card_item:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, self.app.card_get(card_item.group(1)))\n"
        "                return\n"
        "            if self.path == _MCP_BASE:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, {\"mcp\": self.app.mcp_status()})\n"
        "                return\n"
        "            if self.path == _MCP_PRINCIPALS:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, {\"principals\": self.app._mcp().list_principals()})\n"
        "                return\n"
        "            if self.path == _MCP_UPSTREAMS:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, {\"upstreams\": self.app._mcp().list_upstreams()})\n"
        "                return\n"
        "            if self.path == _MCP_AUDIT:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._json(200, {\"audit\": self.app._mcp().recent_audit(limit=100)})\n"
        "                return\n"
        "            if self.path == _DRIVE_BASE:\n",
    )
    replace_once(
        path,
        "            if self.path == f\"{_DRIVE_BASE}/connect\":\n",
        "            if self.path == _CARDS_BASE:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                body = self._read_json()\n"
        "                description = body.get(\"description\", \"\")\n"
        "                if not isinstance(description, str):\n"
        "                    raise AuthError(\"description must be a string\")\n"
        "                assignee = body.get(\"assignee_agent_id\", \"agy-gen\")\n"
        "                if assignee is not None and assignee != \"agy-gen\":\n"
        "                    raise AuthError(\"MVP only supports assignee agy-gen\")\n"
        "                self._json(201, {\"card\": self.app.card_create(title=_required_text(body, \"title\"), description=description, assignee_agent_id=assignee)})\n"
        "                return\n"
        "            if self.path == _KANBAN_SYNC:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._reject_nonempty_body()\n"
        "                self._json(200, {\"sync\": self.app.kanban_sync()})\n"
        "                return\n"
        "            if self.path == _KANBAN_AGENT_TICK:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                self._reject_nonempty_body()\n"
        "                self._json(200, {\"agent_tick\": self.app.kanban_agent_tick()})\n"
        "                return\n"
        "            card_action = _CARD_ACTION.match(self.path)\n"
        "            if card_action:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                card_id, operation = card_action.groups()\n"
        "                body = self._read_json()\n"
        "                if operation == \"transition\":\n"
        "                    reason = body.get(\"reason\", \"OWNER_ACTION\")\n"
        "                    if not isinstance(reason, str):\n"
        "                        raise AuthError(\"reason must be a string\")\n"
        "                    self._json(200, self.app.card_transition(card_id, to_state=_required_text(body, \"to_state\"), reason=reason))\n"
        "                    return\n"
        "                self._json(200, self.app.card_comment(card_id, text=_required_text(body, \"text\")))\n"
        "                return\n"
        "            if self.path == _MCP_PRINCIPALS:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                body = self._read_json()\n"
        "                scopes = _required_string_list(body, \"scopes\")\n"
        "                self._json(201, {\"mcp\": self.app.mcp_create_principal(name=_required_text(body, \"name\"), scopes=scopes)})\n"
        "                return\n"
        "            principal_action = _MCP_PRINCIPAL_ACTION.match(self.path)\n"
        "            if principal_action:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                principal_id, operation = principal_action.groups()\n"
        "                if operation == \"rotate\":\n"
        "                    self._reject_nonempty_body(); self._json(200, {\"mcp\": self.app.mcp_rotate_principal(principal_id)}); return\n"
        "                if operation == \"revoke\":\n"
        "                    self._reject_nonempty_body(); self._json(200, {\"principal\": self.app.mcp_revoke_principal(principal_id)}); return\n"
        "                body = self._read_json(); self._json(200, {\"principal\": self.app.mcp_replace_scopes(principal_id, _required_string_list(body, \"scopes\"))}); return\n"
        "            if self.path == _MCP_UPSTREAMS:\n"
        "                self.app.auth.authenticate(self._bearer_token())\n"
        "                body = self._read_json()\n"
        "                secret_id = body.get(\"secret_id\")\n"
        "                if secret_id is not None and not isinstance(secret_id, str): raise AuthError(\"secret_id must be a string\")\n"
        "                upstream = self.app.mcp_register_upstream(namespace=_required_text(body, \"namespace\"), name=_required_text(body, \"name\"), endpoint=_required_text(body, \"endpoint\"), secret_id=secret_id)\n"
        "                self._json(201, {\"upstream\": upstream}); return\n"
        "            upstream_action = _MCP_UPSTREAM_ACTION.match(self.path)\n"
        "            if upstream_action:\n"
        "                self.app.auth.authenticate(self._bearer_token()); self._reject_nonempty_body()\n"
        "                self._json(200, {\"upstream\": self.app._mcp().disable_upstream(upstream_action.group(1))}); return\n"
        "            if self.path == f\"{_DRIVE_BASE}/connect\":\n",
    )
    replace_once(
        path,
        "        if isinstance(exc, CredentialNotFound):\n            self._json(404, {\"error\": \"credential_not_found\"})\n            return\n",
        "        if isinstance(exc, CredentialNotFound):\n"
        "            self._json(404, {\"error\": \"credential_not_found\"})\n"
        "            return\n"
        "        if isinstance(exc, (CardNotFound, McpNotFound)):\n"
        "            self._json(404, {\"error\": \"not_found\"})\n"
        "            return\n"
        "        if isinstance(exc, (CardConflict, InvalidCardTransition, McpConflict)):\n"
        "            self._json(409, {\"error\": \"conflict\"})\n"
        "            return\n",
    )
    replace_once(
        path,
        "        if isinstance(exc, (AuthError, CredentialError, AgentAuthError, DriveBridgeError, ValueError)):\n",
        "        if isinstance(exc, (AuthError, CredentialError, AgentAuthError, DriveBridgeError, KanbanError, ArtifactStoreError, DriveCollaborationError, McpStoreError, ValueError)):\n",
    )
    replace_once(
        path,
        "            (ProductStoreError, SecretProviderError, AgentRuntimeError, DriveStoreError, DriveSystemError, ReportBridgeError),\n",
        "            (ProductStoreError, SecretProviderError, AgentRuntimeError, DriveStoreError, DriveSystemError, ReportBridgeError, KanbanStoreError),\n",
    )
    replace_once(
        path,
        "def _optional_root_name(payload: dict[str, Any]) -> str:\n",
        "def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:\n"
        "    value = payload.get(key)\n"
        "    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):\n"
        "        raise AuthError(f\"{key} must be a list of strings\")\n"
        "    return list(value)\n\n\n"
        "def _optional_root_name(payload: dict[str, Any]) -> str:\n",
    )


def patch_cli() -> None:
    path = "src/genos/cli.py"
    replace_once(
        path,
        "from .install import InstallError, NativeProvisioner, ReleaseArtifact, build_native_install\n",
        "from .install import InstallError, NativeProvisioner, ReleaseArtifact, build_native_install\n"
        "from .kanban import InvalidCardTransition, KanbanError, build_kanban_system\n"
        "from .kanban_store import CARD_STATES, CardConflict, CardNotFound, KanbanStoreError\n"
        "from .mcp_store import McpConflict, McpNotFound, McpStoreError, PostgresMcpStore\n",
    )
    replace_once(
        path,
        "    report = sub.add_parser(\"report\", help=\"Build/publish reports from the shared observability authority\")\n",
        "    kanban = sub.add_parser(\"kanban\", help=\"Operate the authoritative local Kanban and Drive collaboration bridge\")\n"
        "    kanban_sub = kanban.add_subparsers(dest=\"kanban_command\", required=True)\n"
        "    item = kanban_sub.add_parser(\"list\"); item.add_argument(\"--status\", choices=CARD_STATES, default=None); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = kanban_sub.add_parser(\"show\"); item.add_argument(\"--card-id\", required=True); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = kanban_sub.add_parser(\"create\"); item.add_argument(\"--title\", required=True); item.add_argument(\"--description\", default=\"\"); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = kanban_sub.add_parser(\"transition\"); item.add_argument(\"--card-id\", required=True); item.add_argument(\"--to\", choices=CARD_STATES, required=True, dest=\"to_state\"); item.add_argument(\"--reason\", default=\"OWNER_ACTION\"); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = kanban_sub.add_parser(\"comment\"); item.add_argument(\"--card-id\", required=True); item.add_argument(\"--text\", required=True); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    for name in (\"sync\", \"agent-tick\"):\n"
        "        item = kanban_sub.add_parser(name); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n\n"
        "    mcp = sub.add_parser(\"mcp\", help=\"Manage the unified GenOS MCP Hub\")\n"
        "    mcp_sub = mcp.add_subparsers(dest=\"mcp_command\", required=True)\n"
        "    for name in (\"status\", \"principal-list\", \"upstream-list\", \"audit\"):\n"
        "        item = mcp_sub.add_parser(name); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = mcp_sub.add_parser(\"principal-create\"); item.add_argument(\"--name\", required=True); item.add_argument(\"--scope\", action=\"append\", default=[]); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    for name in (\"principal-rotate\", \"principal-revoke\"):\n"
        "        item = mcp_sub.add_parser(name); item.add_argument(\"--principal-id\", required=True); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = mcp_sub.add_parser(\"principal-scopes\"); item.add_argument(\"--principal-id\", required=True); item.add_argument(\"--scope\", action=\"append\", default=[]); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = mcp_sub.add_parser(\"upstream-add\"); item.add_argument(\"--namespace\", required=True); item.add_argument(\"--name\", required=True); item.add_argument(\"--endpoint\", required=True); item.add_argument(\"--secret-id\", default=None); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n"
        "    item = mcp_sub.add_parser(\"upstream-disable\"); item.add_argument(\"--upstream-id\", required=True); item.add_argument(\"--json\", action=\"store_true\", dest=\"as_json\")\n\n"
        "    report = sub.add_parser(\"report\", help=\"Build/publish reports from the shared observability authority\")\n",
    )
    replace_once(
        path,
        "    if args.command == \"report\":\n        return _report(args)\n",
        "    if args.command == \"kanban\":\n"
        "        return _kanban(args)\n"
        "    if args.command == \"mcp\":\n"
        "        return _mcp(args)\n"
        "    if args.command == \"report\":\n"
        "        return _report(args)\n",
    )
    replace_once(
        path,
        "def _report(args: argparse.Namespace) -> int:\n",
        "def _kanban(args: argparse.Namespace) -> int:\n"
        "    try:\n"
        "        system = build_kanban_system()\n"
        "        if args.kanban_command == \"list\": payload = {\"cards\": system.list_cards(status=args.status)}\n"
        "        elif args.kanban_command == \"show\": payload = system.get_card(args.card_id)\n"
        "        elif args.kanban_command == \"create\": payload = {\"card\": system.create_card(title=args.title, description=args.description)}\n"
        "        elif args.kanban_command == \"transition\": payload = system.transition(args.card_id, to_state=args.to_state, reason=args.reason)\n"
        "        elif args.kanban_command == \"comment\": payload = system.add_comment(args.card_id, text=args.text)\n"
        "        elif args.kanban_command == \"sync\": payload = system.sync_drive_inbox()\n"
        "        elif args.kanban_command == \"agent-tick\": payload = system.agent_tick()\n"
        "        else: raise SystemExit(2)\n"
        "        _emit_safe(payload, as_json=args.as_json)\n"
        "        return 3 if isinstance(payload, dict) and payload.get(\"state\") in {\"NEEDS_ACTION\", \"PARTIAL\", \"RETRY\"} else 0\n"
        "    except (DriveNeedsAction, AgentNeedsAction):\n"
        "        _emit_safe({\"state\": \"NEEDS_ACTION\"}, as_json=args.as_json); return 3\n"
        "    except (KanbanError, InvalidCardTransition, CardConflict, CardNotFound, KanbanStoreError, ProductStoreError, SecretProviderError, DriveBridgeError, DriveStoreError):\n"
        "        _emit_safe({\"state\": \"FAILED\", \"error_type\": \"KANBAN_OPERATION_FAILED\"}, as_json=args.as_json); return 4\n\n\n"
        "def _mcp(args: argparse.Namespace) -> int:\n"
        "    try:\n"
        "        product = PostgresProductStore(); product.ensure_schema(); store = PostgresMcpStore(product); store.ensure_schema()\n"
        "        if args.mcp_command == \"status\":\n"
        "            port_path = Path(\"/etc/genos/mcp-port\"); port = port_path.read_text(encoding=\"utf-8\").strip() if port_path.is_file() else None\n"
        "            payload = {\"protocol_version\": \"2026-07-28\", \"endpoint\": f\"http://127.0.0.1:{port}/mcp\" if port else None, \"principals\": len(store.list_principals()), \"upstreams\": store.list_upstreams()}\n"
        "        elif args.mcp_command == \"principal-list\": payload = {\"principals\": store.list_principals()}\n"
        "        elif args.mcp_command == \"principal-create\": payload = store.create_principal(name=args.name, scopes=args.scope).one_way_response()\n"
        "        elif args.mcp_command == \"principal-rotate\": payload = store.rotate_principal(args.principal_id).one_way_response()\n"
        "        elif args.mcp_command == \"principal-revoke\": payload = {\"principal\": store.revoke_principal(args.principal_id)}\n"
        "        elif args.mcp_command == \"principal-scopes\": payload = {\"principal\": store.replace_scopes(args.principal_id, args.scope)}\n"
        "        elif args.mcp_command == \"upstream-list\": payload = {\"upstreams\": store.list_upstreams()}\n"
        "        elif args.mcp_command == \"upstream-add\":\n"
        "            if args.secret_id:\n"
        "                record = product.get_credential(args.secret_id)\n"
        "                if record is None or record.status != \"ACTIVE\" or \"mcp-hub\" not in record.consumer_scopes: raise McpStoreError(\"SecretRef must be ACTIVE and granted to mcp-hub\")\n"
        "            payload = {\"upstream\": store.register_upstream(namespace=args.namespace, name=args.name, endpoint=args.endpoint, secret_id=args.secret_id)}\n"
        "        elif args.mcp_command == \"upstream-disable\": payload = {\"upstream\": store.disable_upstream(args.upstream_id)}\n"
        "        elif args.mcp_command == \"audit\": payload = {\"audit\": store.recent_audit(limit=100)}\n"
        "        else: raise SystemExit(2)\n"
        "        _emit_safe(payload, as_json=args.as_json); return 0\n"
        "    except (McpStoreError, McpConflict, McpNotFound, ProductStoreError):\n"
        "        _emit_safe({\"state\": \"FAILED\", \"error_type\": \"MCP_OPERATION_FAILED\"}, as_json=args.as_json); return 4\n\n\n"
        "def _report(args: argparse.Namespace) -> int:\n",
    )


def patch_core_service() -> None:
    path = "src/genos/core_service.py"
    replace_once(
        path,
        '    parser.add_argument("role", choices=("product-api", "runtime", "worker", "mission-control"))\n',
        '    parser.add_argument("role", choices=("product-api", "runtime", "worker", "mcp", "mission-control"))\n',
    )
    replace_once(
        path,
        '    drive_projection: dict[str, object] = {"state": "SCHEDULED", "remote_write": False, "observed_at": _utc_now()}\n',
        '    drive_projection: dict[str, object] = {"state": "SCHEDULED", "remote_write": False, "observed_at": _utc_now()}\n'
        '    try:\n'
        '        from .kanban import build_kanban_system\n'
        '        kanban_system = build_kanban_system()\n'
        '        kanban_projection: dict[str, object] = {"state": "IDLE", "reason": "NOT_TICKED", "observed_at": _utc_now()}\n'
        '    except Exception as exc:\n'
        '        kanban_system = None\n'
        '        kanban_projection = {"state": "DEGRADED", "reason": f"KANBAN_INIT_{type(exc).__name__}", "observed_at": _utc_now()}\n',
    )
    replace_once(
        path,
        '        payload = {"status": "ok", "role": "worker", "version": __version__, "instance_id": instance_id, "core_agent": agent, "drive_report": drive_projection, "observed_at": _utc_now()}\n',
        '        if kanban_system is not None:\n'
        '            try:\n'
        '                tick = kanban_system.agent_tick()\n'
        '                kanban_projection = {**tick, "observed_at": _utc_now()}\n'
        '            except Exception as exc:\n'
        '                kanban_projection = {"state": "DEGRADED", "reason": f"KANBAN_TICK_{type(exc).__name__}", "observed_at": _utc_now()}\n'
        '        payload = {"status": "ok", "role": "worker", "version": __version__, "instance_id": instance_id, "core_agent": agent, "drive_report": drive_projection, "kanban_agent": kanban_projection, "observed_at": _utc_now()}\n',
    )
    replace_once(
        path,
        '    if args.role == "worker": return run_worker(Path(args.state_dir), args.worker_interval)\n    if args.port is None: raise SystemExit("--port is required for HTTP roles")\n',
        '    if args.role == "worker": return run_worker(Path(args.state_dir), args.worker_interval)\n'
        '    if args.role == "mcp":\n'
        '        from .mcp_transport import serve_mcp\n'
        '        port = args.port or int(os.environ.get("GENOS_MCP_PORT", "0") or "0")\n'
        '        if not port: raise SystemExit("GENOS_MCP_PORT or --port is required for MCP role")\n'
        '        return serve_mcp(port=port)\n'
        '    if args.port is None: raise SystemExit("--port is required for HTTP roles")\n',
    )


def patch_drive_collab() -> None:
    path = "src/genos/drive_collab.py"
    replace_once(path, "from urllib import parse as urlparse\n", "from urllib import error as urlerror\nfrom urllib import parse as urlparse\nfrom urllib import request as urlrequest\n")
    replace_once(
        path,
        '    def read_bytes(self, file_id: str, *, max_bytes: int) -> bytes:\n        data = self._bytes_request("GET", f"{self.API}/files/{urlparse.quote(file_id)}?alt=media")  # noqa: SLF001\n        if len(data) > max_bytes:\n            return data[: max_bytes + 1]\n        return data\n',
        '    def read_bytes(self, file_id: str, *, max_bytes: int) -> bytes:\n'
        '        request = urlrequest.Request(\n'
        '            f"{self.API}/files/{urlparse.quote(file_id)}?alt=media", method="GET",\n'
        '            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/octet-stream"},  # noqa: SLF001\n'
        '        )\n'
        '        try:\n'
        '            with urlrequest.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed Google Drive endpoint\n'
        '                return response.read(max_bytes + 1)\n'
        '        except urlerror.HTTPError as exc:\n'
        '            if exc.code in {401, 403}: raise DriveNeedsAction("Drive credential rejected") from exc\n'
        '            raise DriveRemoteError(f"Drive HTTP status {exc.code}") from exc\n'
        '        except (urlerror.URLError, TimeoutError, OSError) as exc:\n'
        '            raise DriveRemoteError("Drive network request failed") from exc\n',
    )


def patch_install() -> None:
    path = "src/genos/install.py"
    replace_once(path, "import shutil\n", "import shutil\nimport socket\n")
    replace_once(
        path,
        "MISSION_CONTROL_PORT = 17882\n",
        "MISSION_CONTROL_PORT = 17882\nMCP_PORT_MIN = 17883\nMCP_PORT_MAX = 17932\n",
    )
    replace_once(
        path,
        '        env = (\n            f"GENOS_INSTANCE_ID={instance_id}\\n"\n            "GENOS_STATE_DIR=/var/lib/genos\\n"\n            f"GENOS_RELEASE_SHA={self.planned.release.git_sha}\\n"\n        )\n',
        '        mcp_port = _resolve_mcp_port(Path("/etc/genos/mcp-port"))\n'
        '        env = (\n'
        '            f"GENOS_INSTANCE_ID={instance_id}\\n"\n'
        '            "GENOS_STATE_DIR=/var/lib/genos\\n"\n'
        '            f"GENOS_RELEASE_SHA={self.planned.release.git_sha}\\n"\n'
        '            f"GENOS_MCP_PORT={mcp_port}\\n"\n'
        '        )\n',
    )
    replace_once(
        path,
        '        os.chown("/etc/genos/genos.env", 0, gid)\n',
        '        os.chown("/etc/genos/genos.env", 0, gid)\n'
        '        os.chown("/etc/genos/mcp-port", 0, gid)\n'
        '        os.chmod("/etc/genos/mcp-port", 0o640)\n',
    )
    replace_once(
        path,
        '            "genos-worker.service",\n            "genos-mission-control.service",\n',
        '            "genos-worker.service",\n            "genos-mcp.service",\n            "genos-mission-control.service",\n',
    )
    replace_once(
        path,
        '        for role, service, port in (\n            ("product-api", "genos-product-api.service", PRODUCT_API_PORT),\n            ("runtime", "genos-runtime.service", RUNTIME_PORT),\n            ("mission-control", "genos-mission-control.service", MISSION_CONTROL_PORT),\n        ):\n',
        '        mcp_port = int(Path("/etc/genos/mcp-port").read_text(encoding="utf-8").strip())\n'
        '        for role, service, port in (\n'
        '            ("product-api", "genos-product-api.service", PRODUCT_API_PORT),\n'
        '            ("runtime", "genos-runtime.service", RUNTIME_PORT),\n'
        '            ("mcp-hub", "genos-mcp.service", mcp_port),\n'
        '            ("mission-control", "genos-mission-control.service", MISSION_CONTROL_PORT),\n'
        '        ):\n',
    )
    replace_once(
        path,
        '                "worker": "/var/lib/genos/worker/heartbeat.json",\n                "postgresql_database": CORE_DB,\n',
        '                "worker": "/var/lib/genos/worker/heartbeat.json",\n'
        '                "mcp_hub": f"http://127.0.0.1:{int(Path(\'/etc/genos/mcp-port\').read_text(encoding=\'utf-8\').strip())}/mcp",\n'
        '                "mcp_protocol_version": "2026-07-28",\n'
        '                "postgresql_database": CORE_DB,\n',
    )
    replace_once(
        path,
        "def _replace_symlink(link: Path, target: Path) -> None:\n",
        'def _resolve_mcp_port(path: Path) -> int:\n'
        '    if path.is_file():\n'
        '        try: port = int(path.read_text(encoding="utf-8").strip())\n'
        '        except (OSError, ValueError) as exc: raise InstallError("existing MCP port configuration is invalid") from exc\n'
        '        if not (MCP_PORT_MIN <= port <= MCP_PORT_MAX): raise InstallError("existing MCP port is outside the managed range")\n'
        '        return port\n'
        '    for port in range(MCP_PORT_MIN, MCP_PORT_MAX + 1):\n'
        '        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n'
        '        try:\n'
        '            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)\n'
        '            sock.bind(("127.0.0.1", port))\n'
        '        except OSError:\n'
        '            continue\n'
        '        finally:\n'
        '            sock.close()\n'
        '        _atomic_text(path, f"{port}\\n", mode=0o640)\n'
        '        return port\n'
        '    raise InstallError("no conflict-free MCP port is available in the managed range")\n\n\n'
        'def _replace_symlink(link: Path, target: Path) -> None:\n',
    )
    replace_once(
        path,
        '        "genos-worker.service": common\n        + "ExecStart=/usr/bin/python3 -m genos.core_service worker --state-dir /var/lib/genos\\n"\n        + install,\n        "genos-mission-control.service": common\n',
        '        "genos-worker.service": common\n'
        '        + "ExecStart=/usr/bin/python3 -m genos.core_service worker --state-dir /var/lib/genos\\n"\n'
        '        + install,\n'
        '        "genos-mcp.service": common\n'
        '        + "ExecStart=/usr/bin/python3 -m genos.core_service mcp\\n"\n'
        '        + install,\n'
        '        "genos-mission-control.service": common\n',
    )


def patch_fresh_host() -> None:
    path = "ci/fresh-host-e2e.sh"
    replace_once(
        path,
        'FIRST_BOOT_ID="$(ssh_guest \'cat /proc/sys/kernel/random/boot_id\')"\n',
        'FIRST_BOOT_ID="$(ssh_guest \'cat /proc/sys/kernel/random/boot_id\')"\nFIRST_MCP_PORT="$(ssh_guest \'sudo cat /etc/genos/mcp-port\')"\n',
    )
    replace_once(
        path,
        '  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mission-control.service"\n',
        '  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service"\n',
    )
    replace_once(
        path,
        "for role, port in [('product-api',17880),('runtime',17881),('mission-control',17882)]:\n",
        "mcp_port=int(open('/etc/genos/mcp-port', encoding='utf-8').read().strip())\nfor role, port in [('product-api',17880),('runtime',17881),('mcp-hub',mcp_port),('mission-control',17882)]:\n",
    )
    replace_once(
        path,
        "try:\n    urllib.request.urlopen('http://127.0.0.1:17880/api/v1/drive', timeout=5)\n    raise AssertionError('Drive API unexpectedly allowed unauthenticated access')\nexcept urllib.error.HTTPError as exc:\n    assert exc.code == 401, exc.code\n    error_payload = json.loads(exc.read().decode('utf-8'))\n    assert error_payload['error'] == 'unauthorized', error_payload\n",
        "for protected in ('/api/v1/drive', '/api/v1/cards', '/api/v1/mcp'):\n    try:\n        urllib.request.urlopen('http://127.0.0.1:17880' + protected, timeout=5)\n        raise AssertionError(f'{protected} unexpectedly allowed unauthenticated access')\n    except urllib.error.HTTPError as exc:\n        assert exc.code == 401, (protected, exc.code)\n        error_payload = json.loads(exc.read().decode('utf-8'))\n        assert error_payload['error'] == 'unauthorized', error_payload\nmcp_body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode()\nmcp_req=urllib.request.Request(f'http://127.0.0.1:{mcp_port}/mcp', data=mcp_body, method='POST', headers={'Content-Type':'application/json','MCP-Protocol-Version':'2026-07-28','Mcp-Method':'tools/list'})\ntry:\n    urllib.request.urlopen(mcp_req, timeout=5)\n    raise AssertionError('MCP Hub unexpectedly allowed unauthenticated access')\nexcept urllib.error.HTTPError as exc:\n    assert exc.code == 401, exc.code\n",
    )
    replace_once(
        path,
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=4) THEN 1 ELSE 0 END\\" | grep -qx 1"\n',
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=4) THEN 1 ELSE 0 END\\" | grep -qx 1"\n'
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN to_regclass(\'public.card\') IS NOT NULL AND to_regclass(\'public.card_event\') IS NOT NULL AND to_regclass(\'public.card_artifact\') IS NOT NULL THEN 1 ELSE 0 END\\" | grep -qx 1"\n'
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=5) THEN 1 ELSE 0 END\\" | grep -qx 1"\n'
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN to_regclass(\'public.mcp_principal\') IS NOT NULL AND to_regclass(\'public.mcp_upstream\') IS NOT NULL AND to_regclass(\'public.mcp_audit_event\') IS NOT NULL THEN 1 ELSE 0 END\\" | grep -qx 1"\n'
        '  ssh_guest "sudo -u genos psql -d genos -tAc \\"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=6) THEN 1 ELSE 0 END\\" | grep -qx 1"\n',
    )
    replace_once(
        path,
        'if [[ "$SECOND_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then\n  echo "instance_id changed across rerun" >&2\n  exit 1\nfi\n',
        'if [[ "$SECOND_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then\n  echo "instance_id changed across rerun" >&2\n  exit 1\nfi\nSECOND_MCP_PORT="$(ssh_guest \'sudo cat /etc/genos/mcp-port\')"\nif [[ "$SECOND_MCP_PORT" != "$FIRST_MCP_PORT" ]]; then echo "MCP port changed across rerun" >&2; exit 1; fi\n',
    )
    replace_once(
        path,
        'if [[ "$THIRD_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then\n  echo "instance_id changed across reboot" >&2\n  exit 1\nfi\n',
        'if [[ "$THIRD_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then\n  echo "instance_id changed across reboot" >&2\n  exit 1\nfi\nTHIRD_MCP_PORT="$(ssh_guest \'sudo cat /etc/genos/mcp-port\')"\nif [[ "$THIRD_MCP_PORT" != "$FIRST_MCP_PORT" ]]; then echo "MCP port changed across reboot" >&2; exit 1; fi\n',
    )
    replace_once(
        path,
        '    "drive_api_owner_auth_boundary": "PASS",\n',
        '    "drive_api_owner_auth_boundary": "PASS",\n'
        '    "kanban_schema_v5": "PASS",\n'
        '    "card_api_owner_auth_boundary": "PASS",\n'
        '    "mcp_schema_v6": "PASS",\n'
        '    "mcp_hub_local_auth_boundary": "PASS",\n'
        '    "mcp_port_persistence": "PASS",\n',
    )


def patch_workflow() -> None:
    path = ".github/workflows/mvp07-kanban-drive-contract.yml"
    replace_once(
        path,
        "      - name: Authority and secret-boundary guard\n        shell: bash\n        run: |\n          set -euo pipefail\n          ! grep -R \"Drive.*authority\" -n src/genos/kanban*.py src/genos/drive_collab.py | grep -vi \"local\\|replica\" || true\n          ! grep -R -E \"access_token|refresh_token|client_secret|raw_secret\" -n tests/test_mvp07*.py src/genos/kanban*.py src/genos/artifact_store.py || {\n            echo 'MVP-07 Card/artifact surfaces must not contain provider credential fields'\n            exit 1\n          }\n          echo 'MVP07_AUTHORITY_GUARD_PASS'\n",
        "      - name: Authority and secret-boundary guard\n"
        "        shell: bash\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          PYTHONPATH=src python - <<'PY'\n"
        "          from genos.kanban_store import _clean_json, KanbanStoreError\n"
        "          from genos.mcp_store import normalize_endpoint, scope_allows, McpStoreError\n"
        "          for key in ('access_token','refresh_token','client_secret','raw_secret','password'):\n"
        "              try: _clean_json({key: 'fixture-secret'})\n"
        "              except KanbanStoreError: pass\n"
        "              else: raise AssertionError(f'forbidden Card event credential field accepted: {key}')\n"
        "          try: normalize_endpoint('https://user:secret@example.com/mcp')\n"
        "          except McpStoreError: pass\n"
        "          else: raise AssertionError('MCP endpoint embedded credential accepted')\n"
        "          assert scope_allows(['genos.*'], 'genos.cards.list')\n"
        "          assert not scope_allows(['genos.cards.list'], 'genos.cards.transition')\n"
        "          print('MVP07_SECRET_AUTHORITY_GUARD_PASS')\n"
        "          PY\n"
        "          grep -R \"remote_role.*collaboration-replica\" -n src/genos/drive_collab.py >/dev/null\n"
        "          ! grep -R \"shell=True\" -n src/genos/mcp_*.py\n"
        "          echo 'MVP07_AUTHORITY_GUARD_PASS'\n",
    )


def main() -> None:
    patch_product_api()
    patch_cli()
    patch_core_service()
    patch_drive_collab()
    patch_install()
    patch_fresh_host()
    patch_workflow()


if __name__ == "__main__":
    main()
