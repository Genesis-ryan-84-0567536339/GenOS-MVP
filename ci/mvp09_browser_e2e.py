from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

from playwright.sync_api import sync_playwright

import genos.mission_control as mission_control


TOKEN = "mvp09-fixture-session"


class FixtureProductAPI(BaseHTTPRequestHandler):
    cards = [
        {
            "card_id": "11111111-1111-4111-8111-111111111111",
            "title": "Verify Mission Control",
            "description": "Fixture card from deterministic Product API.",
            "status": "BACKLOG",
            "assignee_agent_id": "agy-gen",
            "last_sync_state": "SYNCED",
        }
    ]
    tasks = []

    def log_message(self, _fmt: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/v1/auth/me":
            return self._protected({"owner": {"owner_id": "fixture-owner", "username": "ryan"}})
        if self.path == "/api/v1/observability":
            return self._protected(
                {
                    "observability": {
                        "authority": "genos-observability-v1",
                        "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "health": {"state": "PASS"},
                        "observations": [
                            {"check_id": "product-api", "state": "PASS", "source": "fixture"},
                            {"check_id": "runtime", "state": "PASS", "source": "fixture"},
                        ],
                    }
                }
            )
        if self.path == "/api/v1/cards":
            return self._protected({"cards": list(self.cards)})
        if self.path.startswith("/api/v1/cards/") and self.path.count("/") == 5:
            card_id = self.path.rsplit("/", 1)[-1]
            card = next((item for item in self.cards if item["card_id"] == card_id), self.cards[0])
            return self._protected(
                {
                    "card": card,
                    "events": [{"event_id": "event-1", "event_type": "CARD_CREATED", "payload": {"status": card["status"]}}],
                    "artifacts": [],
                }
            )
        if self.path == "/api/v1/agents/agy-gen":
            return self._protected(
                {
                    "agent": {
                        "status": {
                            "identity": {"agent_id": "agy-gen", "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "provider_target": {"model": "gemini-3.7-flash-high"}},
                            "runtime": {"state": "READY", "reason": "FIXTURE_READY", "tmux_state": "RUNNING"},
                            "provider": {"state": "ACTIVE", "model": "gemini-3.7-flash-high", "thinking_level": "HIGH", "evidence": "FIXTURE_MODEL_PASS"},
                        },
                        "auth": {"state": "AUTHENTICATED"},
                    }
                }
            )
        if self.path == "/api/v1/agents/agy-gen/tasks":
            return self._protected({"tasks": list(self.tasks)})
        if self.path == "/api/v1/agents/agy-gen/library":
            return self._protected(
                {
                    "library": {
                        "agent_id": "agy-gen",
                        "memory": [
                            {
                                "kind": "memory",
                                "name": "owner-context",
                                "state": "ACTIVE",
                                "active_revision": 1,
                                "revision_count": 1,
                                "revisions": [
                                    {"kind": "memory", "name": "owner-context", "revision": 1, "source": "fixture", "created_at": "2026-08-21T00:00:00Z", "state": "ACTIVE", "active": True, "content": "Fixture memory content."}
                                ],
                            }
                        ],
                        "skills": [],
                    }
                }
            )
        if self.path == "/api/v1/credentials":
            return self._protected({"credentials": []})
        if self.path == "/api/v1/drive":
            return self._protected({"drive": {"state": "READY", "account_email": "fixture@example.test", "root_folder_id": "fixture-root"}})
        if self.path == "/api/v1/drive/oauth":
            return self._protected({"oauth": {"state": "AUTHORIZED"}})
        if self.path == "/api/v1/mcp":
            return self._protected({"mcp": {"protocol_version": "2026-07-28", "endpoint": "http://127.0.0.1:17883/mcp", "principal_count": 1}})
        if self.path == "/api/v1/mcp/principals":
            return self._protected({"principals": [{"principal_id": "22222222-2222-4222-8222-222222222222", "name": "fixture-agent", "status": "ACTIVE", "scopes": ["genos.status"]}]})
        if self.path == "/api/v1/mcp/upstreams":
            return self._protected({"upstreams": [{"upstream_id": "33333333-3333-4333-8333-333333333333", "namespace": "github", "name": "Fixture GitHub", "endpoint": "https://mcp.example.test", "status": "ACTIVE"}]})
        if self.path == "/api/v1/mcp/audit":
            return self._protected({"audit": [{"created_at": "2026-08-21T00:00:00Z", "principal_id": "fixture-agent", "tool_name": "genos.status", "decision": "ALLOW"}]})
        if self.path == "/api/v1/jobs":
            return self._protected({"jobs": [{"job_id": "job-fixture", "kind": "drive-system-report", "state": "SUCCEEDED", "progress_percent": 100, "current_step": "completed", "updated_at": "2026-08-21T00:00:00Z"}]})
        if self.path == "/api/v1/reports/history":
            report = {"history_id": "job-fixture", "job_id": "job-fixture", "fingerprint": "sha256:fixture", "manual": True, "recorded_at": "2026-08-21T00:00:00Z", "diff": {"state": "INITIAL"}}
            return self._protected({"latest": report, "reports": [report]})
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/v1/auth/login":
            body = self._body()
            if body.get("username") != "ryan" or not body.get("password"):
                return self._json(401, {"error": "invalid_credentials"})
            return self._json(200, {"session_token": TOKEN, "owner": {"owner_id": "fixture-owner", "username": "ryan"}, "expires_at": "2099-01-01T00:00:00Z"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path == "/api/v1/cards":
            body = self._body()
            card = {"card_id": str(uuid.uuid4()), "title": body.get("title", "Untitled"), "description": body.get("description", ""), "status": "BACKLOG", "assignee_agent_id": "agy-gen", "last_sync_state": None}
            self.cards.append(card)
            return self._json(201, {"card": card})
        if self.path == "/api/v1/agents/agy-gen/tasks":
            body = self._body()
            task = {"task_id": str(uuid.uuid4()), "agent_id": "agy-gen", "state": "QUEUED", "prompt": body.get("prompt", ""), "created_at": "2026-08-21T00:00:00Z"}
            self.tasks.append(task)
            return self._json(202, {"task": task})
        if self.path in {
            "/api/v1/kanban/sync",
            "/api/v1/agents/agy-gen/auth/start",
            "/api/v1/agents/agy-gen/auth/verify",
            "/api/v1/agents/agy-gen/runtime/restart",
            "/api/v1/drive/oauth/start",
            "/api/v1/drive/oauth/poll",
            "/api/v1/drive/reconnect",
            "/api/v1/drive/disconnect",
            "/api/v1/reports/system",
        }:
            self._body(optional=True)
            return self._json(200, {"state": "PASS"})
        self._body(optional=True)
        return self._json(200, {"state": "PASS"})

    def _protected(self, payload: dict) -> None:
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        self._json(200, payload)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _body(self, *, optional: bool = False) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {} if optional else {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> int:
    product, product_thread, product_origin = start_server(FixtureProductAPI)
    mission_control.PRODUCT_API_ORIGIN = product_origin
    web, web_thread, web_origin = start_server(mission_control.MissionControlHandler)
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(f"pageerror:{error}"))
            page.on("console", lambda message: errors.append(f"console:{message.text}") if message.type == "error" else None)
            page.goto(web_origin + "/dashboard", wait_until="networkidle")
            page.locator("#login-username").fill("ryan")
            page.locator("#login-password").fill("fixture-password")
            page.locator("#login-form button[type=submit]").click()
            page.get_by_role("heading", name="Dashboard").wait_for()
            assert "PASS" in page.locator("#global-state").inner_text()

            page.get_by_role("button", name="Kanban").click()
            page.get_by_role("heading", name="Kanban").wait_for()
            page.locator("#new-card-title").fill("Browser fixture card")
            page.locator("#new-card-description").fill("Created through typed Product API fixture")
            page.locator("#new-card-form button[type=submit]").click()
            page.get_by_text("Browser fixture card").wait_for()

            page.get_by_role("button", name="Agy-gen").click()
            page.get_by_role("heading", name="Agy-gen Chat / Runtime").wait_for()
            page.locator("#agent-task-prompt").fill("Browser fixture task")
            page.locator("#agent-task-form button[type=submit]").click()
            page.get_by_text("Browser fixture task").wait_for()

            page.get_by_role("button", name="Memory & Skills").click()
            page.get_by_role("heading", name="Memory & Skills").wait_for()
            page.get_by_text("owner-context").first.wait_for()

            page.get_by_role("button", name="Connections").click()
            page.get_by_role("heading", name="Connections & Credentials").wait_for()
            page.get_by_text("http://127.0.0.1:17883/mcp").first.wait_for()

            page.get_by_role("button", name="Reports").click()
            page.get_by_role("heading", name="Reports & Job progress").wait_for()
            page.get_by_text("LATEST SYSTEM REPORT").wait_for()

            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="Dashboard").click()
            page.get_by_role("heading", name="Dashboard").wait_for()
            assert page.locator("#nav").is_visible()
            browser.close()
        if errors:
            raise AssertionError("browser emitted errors: " + " | ".join(errors))
        print("MVP-09 browser E2E PASS: Owner login + six surfaces + typed Card/Agent task + responsive navigation")
        return 0
    finally:
        web.shutdown(); web.server_close(); web_thread.join(timeout=2)
        product.shutdown(); product.server_close(); product_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
