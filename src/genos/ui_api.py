from __future__ import annotations

from pathlib import Path
import os

from .agent_library import AgentLibraryError, AgentLibraryService
from .agent_secure_runtime import SecureTmuxController
from .agent_tasks import AgentTaskService
from .product_api import ProductAPIHandler
from .report_history import ReportHistoryStore
from .state import JsonStateStore


_AGENT_BASE = "/api/v1/agents/agy-gen"
_AGENT_TASKS = f"{_AGENT_BASE}/tasks"
_AGENT_RUNTIME_RESTART = f"{_AGENT_BASE}/runtime/restart"
_LIBRARY = f"{_AGENT_BASE}/library"
_LIBRARY_REVISIONS = f"{_LIBRARY}/revisions"
_LIBRARY_ACTIVATE = f"{_LIBRARY}/activate"
_LIBRARY_DISABLE = f"{_LIBRARY}/disable"
_JOBS = "/api/v1/jobs"
_REPORT_HISTORY = "/api/v1/reports/history"


class MissionProductAPIHandler(ProductAPIHandler):
    """MVP-09 Owner-authenticated Mission Control routes.

    These endpoints are projections/actions over existing Product authorities:
    AgentRuntimeStore, JsonStateStore and ReportHistoryStore. They do not create
    another queue, report authority, library registry or shell surface.
    """

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_BASE:
                self.app.auth.authenticate(self._bearer_token())
                tasks = AgentTaskService(self.app.agent_store)
                self._json(
                    200,
                    {
                        "agent": {
                            "status": tasks.current(),
                            "auth": self.app.agent_auth.status(),
                        }
                    },
                )
                return
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"tasks": AgentTaskService(self.app.agent_store).history(limit=100)})
                return
            if self.path == _LIBRARY:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"library": AgentLibraryService(self.app.agent_store).inventory()})
                return
            if self.path == _JOBS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"jobs": JsonStateStore(self._state_root()).list_jobs(limit=200)})
                return
            if self.path == _REPORT_HISTORY:
                self.app.auth.authenticate(self._bearer_token())
                store = ReportHistoryStore(self._state_root())
                rows = store.list_history(limit=200)
                self._json(200, {"reports": rows, "latest": store.latest()})
                return
            super().do_GET()
        except (ValueError, AgentLibraryError) as exc:
            self._json(400, {"error": "invalid_request", "message": str(exc)})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                prompt = body.get("prompt")
                if not isinstance(prompt, str):
                    raise ValueError("prompt must be a string")
                self._json(202, {"task": AgentTaskService(self.app.agent_store).submit(prompt)})
                return
            if self.path == _AGENT_RUNTIME_RESTART:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                SecureTmuxController(self.app.agent_store).restart_worker_session()
                self._json(
                    200,
                    {
                        "runtime": {
                            "state": "RESTARTED",
                            "agent_id": "agy-gen",
                            "identity_preserved": True,
                        }
                    },
                )
                return
            if self.path == _LIBRARY_REVISIONS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                kind = body.get("kind")
                name = body.get("name")
                content = body.get("content")
                if not all(isinstance(value, str) for value in (kind, name, content)):
                    raise ValueError("kind, name and content must be strings")
                result = AgentLibraryService(self.app.agent_store).append_revision(
                    kind=kind,
                    name=name,
                    content=content,
                    source="owner-ui",
                )
                self._json(201, {"revision": result})
                return
            if self.path == _LIBRARY_ACTIVATE:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                kind = body.get("kind")
                name = body.get("name")
                revision = body.get("revision")
                if not isinstance(kind, str) or not isinstance(name, str) or not isinstance(revision, int):
                    raise ValueError("kind/name/revision are required")
                result = AgentLibraryService(self.app.agent_store).activate(
                    kind=kind,
                    name=name,
                    revision=revision,
                )
                self._json(200, {"revision": result})
                return
            if self.path == _LIBRARY_DISABLE:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                kind = body.get("kind")
                name = body.get("name")
                if not isinstance(kind, str) or not isinstance(name, str):
                    raise ValueError("kind and name are required")
                result = AgentLibraryService(self.app.agent_store).disable(kind=kind, name=name)
                self._json(200, {"item": result})
                return
            super().do_POST()
        except (ValueError, AgentLibraryError) as exc:
            self._json(400, {"error": "invalid_request", "message": str(exc)})
        except Exception as exc:
            self._handle_error(exc)

    @staticmethod
    def _state_root() -> Path:
        return Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
