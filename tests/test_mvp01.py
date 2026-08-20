from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from genos.cli import main
from genos.contracts import JobRun, Observation, ObservationState, RunState, SupportClass
from genos.recon import ReadOnlyCommandRunner, collect_all
from genos.redaction import redact
from genos.state import JsonStateStore


class ContractTests(unittest.TestCase):
    def test_observation_is_machine_serializable(self) -> None:
        item = Observation("platform", ObservationState.PASS, observed={"system": "Linux"})
        payload = item.to_dict()
        self.assertEqual(payload["state"], "PASS")
        json.dumps(payload)

    def test_support_taxonomy_is_stable(self) -> None:
        self.assertEqual(
            {item.value for item in SupportClass},
            {"SUPPORTED", "SUPPORTED_WITH_ACTION", "UNSUPPORTED", "CONFLICT"},
        )


class RedactionTests(unittest.TestCase):
    def test_recursive_secret_redaction(self) -> None:
        secret = "super-secret-value-123"
        payload = {
            "password": secret,
            "nested": {"api_key": secret, "message": f"Bearer {secret}"},
            "safe": "visible",
        }
        cleaned = redact(payload)
        serialized = json.dumps(cleaned)
        self.assertNotIn(secret, serialized)
        self.assertEqual(cleaned["safe"], "visible")


class StateTests(unittest.TestCase):
    def test_jobrun_survives_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = JsonStateStore(root)
            job = JobRun(kind="recon", state=RunState.RUNNING, progress_percent=41, current_step="ports")
            first.save_job(job)
            second = JsonStateStore(root)
            loaded = second.load_job(job.job_id)
            self.assertEqual(loaded.job_id, job.job_id)
            self.assertEqual(loaded.state, RunState.RUNNING)
            self.assertEqual(loaded.progress_percent, 41)
            self.assertEqual(loaded.current_step, "ports")

    def test_manifest_write_redacts_secret(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = JsonStateStore(root)
            store.save_manifest({"state": "READY", "token": "never-write-this"})
            raw = store.manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("never-write-this", raw)
            self.assertIn("[REDACTED]", raw)


class ReconTests(unittest.TestCase):
    def test_command_allowlist_rejects_mutation(self) -> None:
        runner = ReadOnlyCommandRunner()
        with self.assertRaises(ValueError):
            runner.run(["git", "clean", "-fdx"])

    def test_collectors_return_required_ids(self) -> None:
        observations, support, reason = collect_all(cwd=None)
        ids = {item.check_id for item in observations}
        self.assertTrue(
            {"platform", "resources", "disk", "network", "ports", "systemd", "container_runtime", "current_genos", "git", "runtime_basics"}.issubset(ids)
        )
        self.assertIn(support, set(SupportClass))
        self.assertTrue(reason)

    def test_recon_does_not_create_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_dir = Path(root) / "state-that-must-not-exist"
            old = os.environ.get("GENOS_STATE_DIR")
            os.environ["GENOS_STATE_DIR"] = str(state_dir)
            try:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = main(["recon", "--json"])
                payload = json.loads(buffer.getvalue())
                self.assertEqual(code, 0)
                self.assertTrue(payload["read_only"])
                self.assertFalse(state_dir.exists())
            finally:
                if old is None:
                    os.environ.pop("GENOS_STATE_DIR", None)
                else:
                    os.environ["GENOS_STATE_DIR"] = old

    def test_mutation_surface_is_disabled_in_mvp01(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["install"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["state"], "NOT_IMPLEMENTED_IN_MVP_01")
        self.assertTrue(payload["read_only"])


if __name__ == "__main__":
    unittest.main()
