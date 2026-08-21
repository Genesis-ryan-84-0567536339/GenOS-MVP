from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from genos.agent_auth import AgentAuthBridge, AgentAuthError, normalize_auth_code, parse_auth_terminal
from genos.agent_runtime import AgentRuntimeStore
from genos.agent_secure_runtime import SecureTmuxController


class AuthProjectionTests(unittest.TestCase):
    def test_manual_oauth_url_and_code_prompt_are_projected(self) -> None:
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture"
            "&redirect_uri=https%3A%2F%2Fantigravity.google%2Fauthcode&state=ephemeral"
        )
        projection = parse_auth_terminal("Open this URL in your browser:\n" + url + "\nPaste the authorization code: ")
        self.assertEqual(projection.state, "WAITING_CODE")
        self.assertEqual(projection.auth_url, url)
        self.assertEqual(projection.tmux_session, "agy-gen")
        self.assertEqual(projection.tmux_window, "auth")

    def test_auth_success_is_truthful(self) -> None:
        projection = parse_auth_terminal("Authentication successful\n")
        self.assertEqual(projection.state, "AUTHENTICATED")
        self.assertEqual(projection.evidence, "AGY_OAUTH_USER_CODE_ACCEPTED")

    def test_auth_failure_is_truthful(self) -> None:
        projection = parse_auth_terminal("Failed to authenticate with authorization code: denied\n")
        self.assertEqual(projection.state, "FAILED")
        self.assertEqual(projection.evidence, "AGY_OAUTH_USER_CODE_REJECTED")

    def test_untrusted_url_is_not_projected(self) -> None:
        projection = parse_auth_terminal("visit https://example.invalid/steal?token=x")
        self.assertIsNone(projection.auth_url)

    def test_trusted_hostname_in_path_is_not_projected(self) -> None:
        projection = parse_auth_terminal("visit https://evil.example/accounts.google.com/oauth?token=x")
        self.assertIsNone(projection.auth_url)
        self.assertEqual(projection.state, "STARTING")

    def test_trusted_hostname_suffix_confusion_is_not_projected(self) -> None:
        projection = parse_auth_terminal("visit https://accounts.google.com.evil.example/oauth")
        self.assertIsNone(projection.auth_url)
        self.assertEqual(projection.state, "STARTING")


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
    def test_bridge_settings_contain_no_auth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            bridge = AgentAuthBridge(store, tmux_binary="/bin/false", agy_binary="/bin/false")
            bridge._ensure_user_settings()
            payload = json.loads(store.settings_path.read_text(encoding="utf-8"))
            self.assertIn("permissions", payload)
            text = store.settings_path.read_text(encoding="utf-8").lower()
            for forbidden in ("access_token", "refresh_token", "api_key", "authorization_code"):
                self.assertNotIn(forbidden, text)

    def test_auth_and_runtime_share_durable_tmux_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            auth = AgentAuthBridge(store, tmux_binary="/bin/false", agy_binary="/bin/false")
            runtime = SecureTmuxController(store, tmux_binary="/bin/false")
            self.assertEqual(auth._env()["TMUX_TMPDIR"], str(store.root))
            self.assertEqual(runtime._env()["TMUX_TMPDIR"], str(store.root))
            self.assertEqual(auth._env()["HOME"], str(store.root))
            self.assertEqual(runtime._env()["HOME"], str(store.root))
            self.assertEqual(auth._env()["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
            self.assertEqual(runtime._env()["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")

    def test_tmux_uses_explicit_shell_without_enabling_service_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            auth = AgentAuthBridge(store, tmux_binary="/bin/false", agy_binary="/bin/false")
            runtime = SecureTmuxController(store, tmux_binary="/bin/false")
            self.assertEqual(auth._env()["SHELL"], "/bin/sh")
            self.assertEqual(runtime._env()["SHELL"], "/bin/sh")


if __name__ == "__main__":
    unittest.main()
