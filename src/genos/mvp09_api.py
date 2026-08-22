from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import re
from urllib.parse import unquote

from .agent_library import AgentLibraryError, AgentLibraryService
from .agent_tasks import AgentTaskService
from .auth_service import AuthError, AuthorizationFailed
from .product_api import ProductAPIApp, ProductAPIHandler, _required_text
from .report_history import ReportHistoryStore
from .state import JsonStateStore


_AGENT_ID = "agy-gen"
_AGENT_BASE = f"/api/v1/agents/{_AGENT_ID}"
_AGENT_TASKS = f"{_AGENT_BASE}/tasks"
_LIBRARY = "/api/v1/library"
_LIBRARY_REVISIONS = f"{_LIBRARY}/revisions"
_JOBS = "/api/v1/jobs"
_REPORT_HISTORY = "/api/v1/reports/history"
_LIBRARY_ACTIVATE = re.compile(r"^/api/v1/library/(memory|skill)/([^/]+)/([1-9][0-9]*)/activate$")
_LIBRARY_DISABLE = re.compile(r"^/api/v1/library/(memory|skill)/([^/]+)/disable$")


class MissionControlProductAPIApp(ProductAPIApp):
    """MVP-09 Product API surface over existing durable authorities.

    The UI does not own state. This class only projects the existing agy-gen
    runtime/task store, revision store, JobRun store and report-history store.
    """

    @property
    def agent_tasks(self) -> AgentTaskService:
        return AgentTaskService(self.agent_store)

    @property
    def agent_library(self) -> AgentLibraryService:
        return AgentLibraryService(self.agent_store)

    @property
    def jobs(self) -> JsonStateStore:
        state_root = Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
        return JsonStateStore(state_root)

    @property
    def report_history(self) -> ReportHistoryStore:
        state_root = Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
        return ReportHistoryStore(state_root)


class MissionControlProductAPIHandler(ProductAPIHandler):
    @property
    def app(self) -> MissionControlProductAPIApp:  # type: ignore[override]
        return getattr(self.server, "genos_app")  # type: ignore[no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"agent": self.app.agent_tasks.current()})
                return
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(
                    200,
                    {
                        "agent": self.app.agent_tasks.current(),
                        "tasks": self.app.agent_tasks.history(limit=100),
                    },
                )
                return
            if self.path == _LIBRARY:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"library": self.app.agent_library.inventory()})
                return
            if self.path == _JOBS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"jobs": self.app.jobs.list_jobs(limit=200)})
                return
            if self.path == _REPORT_HISTORY:
                self.app.auth.authenticate(self._bearer_token())
                self._json(
                    200,
                    {
                        "latest": self.app.report_history.latest(),
                        "history": self.app.report_history.list_history(limit=200),
                    },
                )
                return
            super().do_GET()
        except Exception as exc:
            self._handle_mvp09_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                prompt = _required_text(body, "prompt")
                self._json(202, {"task": self.app.agent_tasks.submit(prompt)})
                return
            if self.path == _LIBRARY_REVISIONS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                kind = _required_text(body, "kind")
                name = _required_text(body, "name")
                content = _required_text(body, "content")
                if kind not in {"memory", "skill"}:
                    raise AuthError("kind must be memory or skill")
                self._json(
                    201,
                    {
                        "revision": self.app.agent_library.append_revision(
                            kind=kind,
                            name=name,
                            content=content,
                            source="owner-ui",
                        )
                    },
                )
                return
            activated = _LIBRARY_ACTIVATE.match(self.path)
            if activated:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                kind, encoded_name, revision = activated.groups()
                self._json(
                    200,
                    {
                        "revision": self.app.agent_library.activate(
                            kind=kind,
                            name=_safe_name(encoded_name),
                            revision=int(revision),
                        )
                    },
                )
                return
            disabled = _LIBRARY_DISABLE.match(self.path)
            if disabled:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                kind, encoded_name = disabled.groups()
                self._json(
                    200,
                    {
                        "library_item": self.app.agent_library.disable(
                            kind=kind,
                            name=_safe_name(encoded_name),
                        )
                    },
                )
                return
            super().do_POST()
        except Exception as exc:
            self._handle_mvp09_error(exc)

    def _handle_mvp09_error(self, exc: Exception) -> None:
        if isinstance(exc, AuthorizationFailed):
            self._json(401, {"error": "unauthorized"})
            return
        if isinstance(exc, (AgentLibraryError, ValueError)):
            self._json(400, {"error": "invalid_request"})
            return
        self._handle_error(exc)


def _safe_name(value: str) -> str:
    decoded = unquote(value).strip()
    if not decoded or any(char in decoded for char in "/\\\x00"):
        raise AuthError("invalid library item")
    return decoded
