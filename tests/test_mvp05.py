from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from genos.cli import _doctor
from genos.contracts import Observation, ObservationState, SupportClass
from genos.observability import ObservabilityService
from genos.product_api import ProductAPIApp
from genos.repair import RepairError, RepairService


_BASELINE_IDS = (
    "platform",
    "resources",
    "disk",
    "network",
    "ports",
    "systemd",
    "container_runtime",
    "current_genos",
    "git",
    "runtime_basics",
)


def _baseline(_cwd: str | None = None):
    observations = [
        Observation(check_id, ObservationState.PASS, observed={"fixture": True}, source="fixture")
        for check_id in _BASELINE_IDS
    ]
    return observations, SupportClass.SUPPORTED_WITH_ACTION, "fixture support"


class ObservabilityReadModelTests(unittest.TestCase):
    def test_snapshot_is_read_only_and_exposes_truthful_future_surface_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            snapshot = ObservabilityService(state_root=Path(temp), baseline_collector=_baseline).snapshot()
        self.assertEqual(snapshot["authority"], "genos-observability-v1")
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["freshness"]["state"], "FRESH")
        facts = {item["check_id"]: item for item in snapshot["observations"]}
        for check_id in (
            "gpu",
            "gateway",
            "genos_services",
            "timers",
            "database",
            "worker_daemon",
            "agent",
            "provider",
            "mcp",
            "drive",
            "tunnel",
        ):
            self.assertIn(check_id, facts)
        for check_id in ("mcp", "drive", "tunnel"):
            self.assertEqual(facts[check_id]["state"], "NOT_INSTALLED")
            self.assertEqual(facts[check_id]["observed"]["state"], "NOT_CONFIGURED")
        self.assertNotEqual(snapshot["health"]["state"], "HEALTHY")

    def test_doctor_and_product_api_use_the_same_observability_service_contract(self) -> None:
        sentinel = {
            "schema_version": "1.0",
            "authority": "genos-observability-v1",
            "read_only": True,
            "generated_at": "2026-08-20T00:00:00Z",
            "freshness": {"state": "FRESH"},
            "health": {"state": "NEEDS_ACTION"},
            "support_class": "SUPPORTED_WITH_ACTION",
            "support_reason": "fixture",
            "observations": [{"check_id": "fixture", "state": "PASS"}],
        }

        class StubObservability:
            def __init__(self) -> None:
                self.calls = 0

            def snapshot(self, *, cwd: str | None = None):
                self.calls += 1
                return sentinel

        stub = StubObservability()
        app = ProductAPIApp(None, None, None, None, None, observability=stub)  # type: ignore[arg-type]
        self.assertIs(app.read_observability(), sentinel)

        output = io.StringIO()
        with patch("genos.cli.ObservabilityService", return_value=stub), redirect_stdout(output):
            rc = _doctor(as_json=True, cwd=None)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue()), sentinel)
        self.assertEqual(stub.calls, 2)


class TypedRepairTests(unittest.TestCase):
    class FakeObservability:
        def __init__(self, worker_state: str) -> None:
            self.worker_state = worker_state

        def snapshot(self):
            return {
                "observations": [
                    {
                        "check_id": "genos_services",
                        "observed": {"units": {"worker": self.worker_state}},
                    }
                ]
            }

    class SequencedObservability:
        def __init__(self, states: list[str]) -> None:
            self.states = list(states)
            self.calls = 0

        def snapshot(self):
            index = min(self.calls, len(self.states) - 1)
            state = self.states[index]
            self.calls += 1
            return {
                "observations": [
                    {
                        "check_id": "genos_services",
                        "observed": {"units": {"worker": state}},
                    }
                ]
            }

    def test_arbitrary_service_target_is_rejected(self) -> None:
        service = RepairService(observability=self.FakeObservability("failed"))  # type: ignore[arg-type]
        with self.assertRaises(RepairError):
            service.plan(action="restart-service", target="sshd")

    def test_repair_plan_is_narrow_and_never_reinstalls(self) -> None:
        service = RepairService(observability=self.FakeObservability("failed"))  # type: ignore[arg-type]
        plan = service.plan(action="restart-service", target="worker")
        self.assertTrue(plan.mutation_allowed)
        self.assertEqual(plan.unit, "genos-worker.service")
        self.assertEqual(plan.operation, ["systemctl", "restart", "genos-worker.service"])
        serialized = json.dumps(plan.to_dict()).lower()
        self.assertNotIn("install", serialized)
        self.assertNotIn("apt", serialized)
        self.assertNotIn("dnf", serialized)

    def test_healthy_service_produces_no_mutation(self) -> None:
        service = RepairService(observability=self.FakeObservability("active"))  # type: ignore[arg-type]
        plan = service.plan(action="restart-service", target="worker")
        self.assertFalse(plan.mutation_allowed)
        self.assertEqual(plan.reason, "NO_REPAIR_REQUIRED")

    def test_unknown_service_evidence_blocks_mutation(self) -> None:
        service = RepairService(observability=self.FakeObservability("unknown"))  # type: ignore[arg-type]
        plan = service.plan(action="restart-service", target="worker")
        self.assertFalse(plan.mutation_allowed)
        self.assertEqual(plan.reason, "INSUFFICIENT_LIVE_EVIDENCE")

    def test_execute_rechecks_live_evidence_and_cancels_stale_plan(self) -> None:
        observability = self.SequencedObservability(["failed", "active"])
        service = RepairService(observability=observability)  # type: ignore[arg-type]
        plan = service.plan(action="restart-service", target="worker")
        self.assertTrue(plan.mutation_allowed)
        with patch("genos.repair._run") as run:
            result = service.execute(plan)
        run.assert_not_called()
        self.assertEqual(result["state"], "NO_ACTION")
        self.assertEqual(result["reason"], "PRECONDITION_CHANGED")
        self.assertEqual(result["plan"]["reason"], "NO_REPAIR_REQUIRED")
        self.assertEqual(observability.calls, 2)


if __name__ == "__main__":
    unittest.main()
