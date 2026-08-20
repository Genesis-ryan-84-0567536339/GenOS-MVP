from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from genos.agent_auth import (
    AUTH_SELECTED_TYPE,
    AgentAuthBridge,
    AgentAuthError,
    normalize_auth_code,
    parse_auth_terminal,
)
from genos.agent_runtime import AgentRuntimeStore


class AuthProjectionTests(unittest.TestCase):
    def test_manual_oauth_url_and_code_prompt_are_projected(self) -> None:
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture"
            "&redirect_uri=https%3A%2F%2Fcodeassist.google.com%2Fauthcode&state=ephemeral"
        )
        projection = parse_auth_terminal(
            "Please visit the following URL to authorize the application:\n\n"
            + url
            + "\n\nEnter the authorization code: "
        )
        self.assertEqual(projection.state, "WAITING_CODE")
        self.assertEqual(projection.auth_url, url)
        self.assertEqual(projection.tmux_session, "agy-gen")
        self.assertEqual(projection.tmux_window, "auth")

    def test_auth_success_is_truthful(self) -> None:
        projection = parse_auth_terminal("Authentication succeeded\n")
        self.assertEqual(projection.state, "AUTHENTICATED")
        self.assertEqual(projection.evidence, "GEMINI_OAUTH_USER_CODE_ACCEPTED")

    def test_auth_failure_is_truthful(self) -> None:
        projection = parse_auth_terminal("Failed to authenticate with authorization code: denied\n")
        self.assertEqual(projection.state, "FAILED")


class AuthCodeHygieneTests(unittest.TestCase):
    def test_code_is_trimmed_but_not_transformed(self) -> None:
        self.assertEqual(normalize_auth_code(" 4/0AbCd-EfGh "), "4/0AbCd-EfGh")

    def test_control_characters_are_rejected(self) -> None:
        with self.assertRaises(AgentAuthError):
            normalize_auth_code("abc\x00def")

    def test_empty_code_is_rejected(self) -> None:
        with self.assertRaises(AgentAuthError):
            normalize_auth_code("   ")


class AuthSettingsTests(unittest.TestCase):
    def test_bridge_selects_google_oauth_without_storing_auth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            bridge = AgentAuthBridge(store, tmux_binary="/bin/false", gemini_binary="/bin/false")
            bridge._ensure_user_auth_settings()  # settings-only unit contract
            path = store.root / ".gemini" / "settings.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["security"]["auth"]["selectedType"], AUTH_SELECTED_TYPE)
            text = path.read_text(encoding="utf-8").lower()
            for forbidden in ("access_token", "refresh_token", "api_key", "authorization_code"):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
