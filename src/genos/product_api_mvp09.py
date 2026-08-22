from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .agent_library import AgentLibraryError, AgentLibraryService
from .agent_runtime import AgentNeedsAction, AgentRuntimeError
from .agent_secure_runtime import SecureTmuxController
from .agent_tasks import AgentTaskService
from .product_api import ProductAPIHandler, _required_text
from .report_history import ReportHistoryError, ReportHistoryStore
from .state import JsonStateStore


_AGENT_BASE = "/api/v1/agents/agy-gen"
_AGENT_TASKS = f"{_AGENT_BASE}/tasks"
_AGENT_LIBRARY = f"{_AGENT_BASE}/library"
_AGENT_LIBRARY_REVISIONS = f"{_AGENT_LIBRARY}/revisions"
_AGENT_LIBRARY_ACTIVATE = f"{_AGENT_LIBRARY}/activate"
_AGENT_LIBRARY_DISABLE = f"{_AGENT_LIBRARY}/disable"
_AGENT_RUNTIME_RESTART = f"{_AGENT_BASE}/runtime/restart"
_DRIVE_OAUTH = "/api/v1/drive/oauth"
_JOBS = "/api/v1/jobs"
_REPORT_HISTORY = "/api/v1/reports/history"
_MCP_PRINCIPAL_PREFIX = "/api/v1/mcp/principals/"
_MCP_ROTATE_SUFFIX = "/rotate"


class MVP09ProductAPIHandler(ProductAPIHandler):
    """MVP-09 UI endpoints layered over existing Product domain authorities.

    No endpoint in this handler executes arbitrary shell input. Agent actions are
    existing typed queue/tmux/auth operations; Job/report/library reads are
    projections of their durable stores.
    """

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_BASE:
                self.app.auth.authenticate(self._bearer_token())
                self._json(
                    200,
                    {
                        "agent": {
                            "status": self.app.agent_store.status(),
                            "auth": self.app.agent_auth.status(),
                        }
                    },
                )
                return
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"tasks": self._tasks().history(limit=8)})
                return
            if self.path == _AGENT_LIBRARY:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"library": self._library().inventory()})
                return
            if self.path == _JOBS:
                self.app.auth.authenticate(self._bearer_token())
                self._json(200, {"jobs": self._jobs().list_jobs(limit=100)})
                return
            if self.path == _REPORT_HISTORY:
                self.app.auth.authenticate(self._bearer_token())
                history = self._report_history()
                self._json(
                    200,
                    {
                        "latest": history.latest(),
                        "reports": history.list_history(limit=50),
                    },
                )
                return
            if self.path == _DRIVE_OAUTH:
                self.app.auth.authenticate(self._bearer_token())
                oauth = dict(self.app.drive_oauth_status())
                # The backend canonical name is verification_url. MVP-09's
                # approved browser contract originally consumed verification_uri.
                # Project both aliases to the same sanitized HTTPS URL so old and
                # new Mission Control assets remain compatible during update.
                verification_url = oauth.get("verification_url") or oauth.get("verification_uri")
                if isinstance(verification_url, str) and verification_url:
                    oauth["verification_url"] = verification_url
                    oauth["verification_uri"] = verification_url
                self._json(200, {"oauth": oauth})
                return
            super().do_GET()
        except Exception as exc:
            self._handle_mvp09_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == _AGENT_TASKS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                self._reject_fields(body, {"prompt"})
                self._json(202, {"task": self._tasks().submit(_required_text(body, "prompt"))})
                return
            if self.path == _AGENT_RUNTIME_RESTART:
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                SecureTmuxController(self.app.agent_store).restart_worker_session()
                self._json(
                    200,
                    {
                        "agent": {
                            "state": "RESTARTED",
                            "status": self.app.agent_store.status(),
                            "auth": self.app.agent_auth.status(),
                        }
                    },
                )
                return
            if self.path == _AGENT_LIBRARY_REVISIONS:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                self._reject_fields(body, {"kind", "name", "content"})
                result = self._library().append_revision(
                    kind=_required_text(body, "kind"),
                    name=_required_text(body, "name"),
                    content=_required_text(body, "content"),
                    source="owner-ui",
                )
                self._json(201, {"revision": result})
                return
            if self.path == _AGENT_LIBRARY_ACTIVATE:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                self._reject_fields(body, {"kind", "name", "revision"})
                revision = body.get("revision")
                if not isinstance(revision, int) or revision < 1:
                    raise AgentLibraryError("revision must be a positive integer")
                result = self._library().activate(
                    kind=_required_text(body, "kind"),
                    name=_required_text(body, "name"),
                    revision=revision,
                )
                self._json(200, {"revision": result})
                return
            if self.path == _AGENT_LIBRARY_DISABLE:
                self.app.auth.authenticate(self._bearer_token())
                body = self._read_json()
                self._reject_fields(body, {"kind", "name"})
                result = self._library().disable(
                    kind=_required_text(body, "kind"),
                    name=_required_text(body, "name"),
                )
                self._json(200, {"library": result})
                return
            if self.path.startswith(_MCP_PRINCIPAL_PREFIX) and self.path.endswith(_MCP_ROTATE_SUFFIX):
                self.app.auth.authenticate(self._bearer_token())
                self._reject_nonempty_body()
                principal_id = self.path[len(_MCP_PRINCIPAL_PREFIX) : -len(_MCP_ROTATE_SUFFIX)]
                issued = self.app.mcp_rotate_principal(principal_id)
                # Keep the established access_token contract while supplying the
                # approved UI's token alias in the same one-time response. No raw
                # token is persisted or exposed by any GET endpoint.
                one_time = dict(issued)
                one_time["token"] = issued.get("access_token")
                self._json(200, {"mcp": one_time})
                return
            super().do_POST()
        except Exception as exc:
            self._handle_mvp09_error(exc)

    def log_message(self, _fmt: str, *_args: object) -> None:
        # Never log query strings, Authorization, raw request bodies or one-time
        # credentials. Route paths are sufficient operational evidence.
        path = self.path.split("?", 1)[0]
        print(
            json.dumps(
                {"event": "product_api_http", "method": self.command, "path": path},
                ensure_ascii=False,
            ),
            flush=True,
        )

    def _library(self) -> AgentLibraryService:
        return AgentLibraryService(self.app.agent_store)

    def _tasks(self) -> AgentTaskService:
        return AgentTaskService(self.app.agent_store)

    def _jobs(self) -> JsonStateStore:
        return JsonStateStore(Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos")))

    def _report_history(self) -> ReportHistoryStore:
        return ReportHistoryStore(Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos")))

    @staticmethod
    def _reject_fields(body: dict[str, Any], allowed: set[str]) -> None:
        if any(key not in allowed for key in body):
            raise AgentLibraryError("request contains unsupported fields")

    def _handle_mvp09_error(self, exc: Exception) -> None:
        if isinstance(exc, (AgentLibraryError, ValueError)):
            self._json(400, {"error": "invalid_request"})
            return
        if isinstance(exc, AgentNeedsAction):
            self._json(409, {"error": "agent_needs_action"})
            return
        if isinstance(exc, (ReportHistoryError, AgentRuntimeError, OSError)):
            self._json(503, {"error": "backend_unavailable"})
            return
        self._handle_error(exc)
