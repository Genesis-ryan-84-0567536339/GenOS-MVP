from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import uuid

from genos.agent_library import AgentLibraryService
from genos.agent_runtime import AgentRuntimeStore
from genos.agent_tasks import AgentTaskService
from genos.contracts import JobRun, RunState
from genos.mission_control import MissionControlHandler, PRODUCT_API_ORIGIN, WEB_ROOT
from genos.report_history import ReportHistoryStore
from genos.state import JsonStateStore


class AgentLibraryTests(unittest.TestCase):
    def test_revision_activate_disable_survives_new_service_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentRuntimeStore(Path(tmp) / "agy-gen")
            store.ensure_seed(instance_id=str(uuid.uuid4()))
            library = AgentLibraryService(store)
            first = library.append_revision(kind="memory", name="owner-context", content="revision one")
            second = library.append_revision(kind="memory", name="owner-context", content="revision two")
            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            activated = library.activate(kind="memory", name="owner-context", revision=1)
            self.assertTrue(activated["active"])
            disabled = library.disable(kind="memory", name="owner-context")
            self.assertEqual(disabled["state"], "DISABLED")

            reloaded = AgentLibraryService(AgentRuntimeStore(Path(tmp) / "agy-gen")).inventory()
            item = reloaded["memory"][0]
            self.assertEqual(item["active_revision"], 1)
            self.assertEqual(item["state"], "DISABLED")
            self.assertEqual(item["revision_count"], 2)


class AgentTaskProjectionTests(unittest.TestCase):
    def test_submit_uses_existing_durable_queue_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentRuntimeStore(Path(tmp) / "agy-gen")
            store.ensure_seed(instance_id=str(uuid.uuid4()))
            store.write_provider(
                {
                    "provider_cli": "antigravity",
                    "state": "ACTIVE",
                    "model": "fixture",
                    "thinking_level": "HIGH",
                    "approval_mode": "yolo",
                    "evidence": "FIXTURE_ACTIVE",
                }
            )
            service = AgentTaskService(store)
            submitted = service.submit("perform fixture task")
            self.assertEqual(submitted["state"], "QUEUED")
            task_id = submitted["task_id"]
            self.assertTrue((store.queue_dir / f"{task_id}.json").is_file())
            history = AgentTaskService(AgentRuntimeStore(Path(tmp) / "agy-gen")).history()
            self.assertEqual(history[0]["task_id"], task_id)
            self.assertEqual(history[0]["prompt"], "perform fixture task")


class DurableActivityTests(unittest.TestCase):
    def test_job_history_survives_store_reinstantiation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = JsonStateStore(root)
            job = JobRun(kind="mvp09-fixture", state=RunState.RUNNING, progress_percent=55, current_step="verify")
            first.save_job(job)
            rows = JsonStateStore(root).list_jobs()
            self.assertEqual(rows[0]["job_id"], job.job_id)
            self.assertEqual(rows[0]["progress_percent"], 55)
            self.assertEqual(rows[0]["state"], "RUNNING")

    def test_report_history_lists_diff_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportHistoryStore(Path(tmp))
            one = {
                "fingerprint": "sha256:first",
                "job": {"job_id": str(uuid.uuid4())},
                "files": {"markdown": "md-one", "json": "json-one"},
            }
            two = {
                "fingerprint": "sha256:second",
                "job": {"job_id": str(uuid.uuid4())},
                "files": {"markdown": "md-two", "json": "json-two"},
            }
            self.assertEqual(store.record(one, manual=True)["diff"]["state"], "INITIAL")
            self.assertEqual(store.record(two, manual=False)["diff"]["state"], "CHANGED")
            rows = ReportHistoryStore(Path(tmp)).list_history()
            self.assertEqual(rows[0]["fingerprint"], "sha256:second")
            self.assertNotIn("snapshot", json.dumps(rows))
            self.assertNotIn("secret", json.dumps(rows).lower())


class MissionControlStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MissionControlHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health_and_static_assets_are_real(self) -> None:
        with urllib.request.urlopen(self.origin + "/health", timeout=2) as response:  # noqa: S310 - loopback fixture
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["role"], "mission-control")
            self.assertEqual(payload["ui_state"], "READY")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        for path in ("/", "/assets/app.css", "/assets/app.js", "/kanban", "/connections"):
            with urllib.request.urlopen(self.origin + path, timeout=2) as response:  # noqa: S310 - loopback fixture
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_web_assets_and_proxy_boundary_are_fixed(self) -> None:
        self.assertTrue((WEB_ROOT / "index.html").is_file())
        self.assertTrue((WEB_ROOT / "app.css").is_file())
        self.assertTrue((WEB_ROOT / "app.js").is_file())
        self.assertEqual(PRODUCT_API_ORIGIN, "http://127.0.0.1:17880")
        js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("localStorage", js)
        self.assertNotIn("/shell", js)
        self.assertIn("/api/v1/agents/agy-gen/tasks", js)
        self.assertIn("/api/v1/mcp/principals", js)


if __name__ == "__main__":
    unittest.main()
