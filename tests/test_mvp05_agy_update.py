from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from genos.agent_cli_update import AntigravityCliManager


class FixtureManager(AntigravityCliManager):
    def __init__(self, root: Path, agent_root: Path, version: str) -> None:
        super().__init__(root, agent_state_root=agent_root, update_interval_seconds=21600)
        self.fixture_version = version

    def _stage_latest(self) -> dict[str, str]:
        candidate = self.root / f".fixture-{self.fixture_version}"
        candidate.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo '" + self.fixture_version + "'; exit 0; fi\n"
            "if [ \"$1\" = \"--help\" ]; then echo '--model --effort --output-format'; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        candidate.chmod(0o755)
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return {
            "version": self.fixture_version,
            "binary": str(candidate),
            "installer_sha256": "a" * 64,
            "binary_sha256": digest,
        }


class ManagedAgyUpdateTests(unittest.TestCase):
    def test_initial_install_and_upgrade_keep_previous_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = FixtureManager(root / "tools", root / "agy-gen", "1.0.0")
            first = manager.ensure_latest(force=True)
            self.assertEqual(first.update_state, "INSTALLED")
            self.assertEqual(first.installed_version, "1.0.0")
            manager.fixture_version = "1.1.0"
            second = manager.ensure_latest(force=True)
            self.assertEqual(second.update_state, "UPDATED")
            self.assertEqual(second.installed_version, "1.1.0")
            self.assertEqual(second.rollback_version, "1.0.0")
            self.assertEqual(manager.status()["rollback_version"], "1.0.0")

    def test_active_work_claim_defers_update_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agy-gen"
            manager = FixtureManager(root / "tools", agent, "1.0.0")
            manager.ensure_latest(force=True)
            manager.fixture_version = "1.1.0"
            manager.claim_path.parent.mkdir(parents=True, exist_ok=True)
            manager.claim_path.write_text('{"task_id":"busy"}\n', encoding="utf-8")
            result = manager.ensure_latest(force=True)
            self.assertEqual(result.update_state, "UPDATE_DEFERRED_BUSY")
            self.assertEqual(result.installed_version, "1.0.0")

    def test_failed_post_cutover_probe_rolls_back_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = FixtureManager(root / "tools", root / "agy-gen", "1.0.0")
            manager.ensure_latest(force=True)
            manager.fixture_version = "1.1.0"
            result = manager.ensure_latest(force=True, post_cutover_probe=lambda _binary: False)
            self.assertEqual(result.update_state, "ROLLED_BACK")
            self.assertEqual(result.installed_version, "1.0.0")
            self.assertEqual(manager.status()["installed_version"], "1.0.0")

    def test_six_hour_throttle_avoids_rechecking_stable_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = FixtureManager(root / "tools", root / "agy-gen", "1.0.0")
            manager.ensure_latest(force=True)
            manager.fixture_version = "9.9.9"
            result = manager.ensure_latest(force=False)
            self.assertEqual(result.installed_version, "1.0.0")
            self.assertEqual(result.evidence, "UPDATE_CHECK_THROTTLED_6H")


if __name__ == "__main__":
    unittest.main()
