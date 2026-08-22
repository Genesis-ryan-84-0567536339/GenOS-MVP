from __future__ import annotations

import unittest

from genos.product_api_mvp09 import _drive_oauth_browser_projection


class DriveOAuthBrowserProjectionTests(unittest.TestCase):
    def test_canonical_verification_url_is_projected_to_approved_ui_alias(self) -> None:
        payload = {
            "state": "WAITING_USER",
            "verification_url": "https://www.google.com/device",
            "user_code": "FIXTURE-CODE",
        }
        projected = _drive_oauth_browser_projection(payload)
        self.assertEqual(projected["verification_url"], "https://www.google.com/device")
        self.assertEqual(projected["verification_uri"], "https://www.google.com/device")
        self.assertEqual(projected["user_code"], "FIXTURE-CODE")

    def test_legacy_verification_uri_is_kept_compatible_without_new_secret_fields(self) -> None:
        payload = {"state": "WAITING_USER", "verification_uri": "https://www.google.com/device"}
        projected = _drive_oauth_browser_projection(payload)
        self.assertEqual(projected["verification_url"], "https://www.google.com/device")
        self.assertEqual(projected["verification_uri"], "https://www.google.com/device")
        self.assertNotIn("access_token", projected)
        self.assertNotIn("refresh_token", projected)
        self.assertNotIn("client_secret", projected)


if __name__ == "__main__":
    unittest.main()
