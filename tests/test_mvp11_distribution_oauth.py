from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from genos.distribution_runtime import (
    GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV,
    GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV,
    DistributionConfigError,
    apply_distribution_environment,
    load_google_drive_oauth_config,
)


class DistributionOAuthTests(unittest.TestCase):
    def _write_config(self, root: Path, *, client_id: str = "fixture-client-id", client_secret: str = "fixture-client-secret") -> Path:
        path = root / "distribution" / "google-drive-oauth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "provider": "google-drive",
                    "flow": "limited-input-device",
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_missing_distribution_config_is_nonfatal_and_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env: dict[str, str] = {}
            state = apply_distribution_environment(root=Path(tmp), environ=env)
            self.assertEqual(state, "NOT_CONFIGURED")
            self.assertNotIn(GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV, env)
            self.assertNotIn(GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV, env)

    def test_valid_release_config_populates_process_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root)
            env: dict[str, str] = {}
            state = apply_distribution_environment(root=root, environ=env)
            self.assertEqual(state, "CONFIGURED")
            self.assertEqual(env[GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV], "fixture-client-id")
            self.assertEqual(env[GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV], "fixture-client-secret")
            # Status is safe to persist/report; raw distribution values are not.
            self.assertNotIn("fixture-client", state)

    def test_explicit_environment_pair_wins_without_reading_release_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, client_id="release-id", client_secret="release-secret")
            env = {
                GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV: "override-id",
                GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV: "override-secret",
            }
            self.assertEqual(apply_distribution_environment(root=root, environ=env), "ENVIRONMENT")
            self.assertEqual(env[GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV], "override-id")
            self.assertEqual(env[GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV], "override-secret")

    def test_partial_environment_is_never_mixed_with_distribution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root)
            env = {GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV: "override-id"}
            self.assertEqual(apply_distribution_environment(root=root, environ=env), "PARTIAL_ENVIRONMENT")
            self.assertNotIn(GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV, env)

    def test_malformed_or_oversized_config_does_not_break_core_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "distribution" / "google-drive-oauth.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(DistributionConfigError):
                load_google_drive_oauth_config(root=root)
            env: dict[str, str] = {}
            self.assertEqual(apply_distribution_environment(root=root, environ=env), "INVALID")
            self.assertEqual(env, {})

            path.write_bytes(b"x" * (16 * 1024 + 1))
            with self.assertRaises(DistributionConfigError):
                load_google_drive_oauth_config(root=root)
            self.assertEqual(apply_distribution_environment(root=root, environ={}), "INVALID")

    def test_newline_or_nul_in_publisher_fields_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, client_secret="bad\nsecret")
            with self.assertRaises(DistributionConfigError):
                load_google_drive_oauth_config(root=root)


if __name__ == "__main__":
    unittest.main()
