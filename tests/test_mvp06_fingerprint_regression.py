from __future__ import annotations

import unittest

from genos.report_bridge import DriveReportService


class SequencedObservability:
    def __init__(self) -> None:
        self.calls = 0
        self.health = "HEALTHY"

    def snapshot(self):
        self.calls += 1
        n = self.calls
        return {
            "schema_version": "1.1",
            "authority": "genos-observability-v1",
            "read_only": True,
            "generated_at": f"2026-08-21T00:{n:02d}:00Z",
            "freshness": {"state": "FRESH", "observed_at": f"2026-08-21T00:{n:02d}:00Z"},
            "health": {"state": self.health},
            "observations": [
                {
                    "check_id": "drive",
                    "state": "PASS",
                    "source": "Product DB drive_binding read-only",
                    "observed_at": f"2026-08-21T00:{n:02d}:01Z",
                    "observed": {
                        "configured": True,
                        "state": "READY",
                        "last_verified_at": f"2026-08-21T00:{n:02d}:02Z",
                        "last_report_fingerprint": f"sha256:previous-{n}",
                        "mcp_grant_checked_at": f"2026-08-21T00:{n:02d}:03Z",
                    },
                },
                {
                    "check_id": "provider_cli_update",
                    "state": "PASS",
                    "source": "fixture",
                    "observed": {
                        "update_state": "CURRENT",
                        "installed_version": "1.2.3",
                        "last_check_at": f"2026-08-21T00:{n:02d}:04Z",
                        "last_success_at": f"2026-08-21T00:{n:02d}:05Z",
                        "age_seconds": n * 60,
                    },
                },
            ],
        }


class SignificantFingerprintRegressionTests(unittest.TestCase):
    def test_report_bookkeeping_and_clock_churn_do_not_change_fingerprint(self) -> None:
        observability = SequencedObservability()
        service = DriveReportService(
            metadata_store=None,  # type: ignore[arg-type]
            credentials=None,  # type: ignore[arg-type]
            remote_factory=None,  # type: ignore[arg-type]
            observability=observability,  # type: ignore[arg-type]
        )
        first = service.build()["fingerprint"]
        second = service.build()["fingerprint"]
        self.assertEqual(first, second)

    def test_actual_health_state_change_changes_fingerprint(self) -> None:
        observability = SequencedObservability()
        service = DriveReportService(
            metadata_store=None,  # type: ignore[arg-type]
            credentials=None,  # type: ignore[arg-type]
            remote_factory=None,  # type: ignore[arg-type]
            observability=observability,  # type: ignore[arg-type]
        )
        first = service.build()["fingerprint"]
        observability.health = "DEGRADED"
        second = service.build()["fingerprint"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
