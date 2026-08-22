from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import uuid

from genos.reinstall import prepare_reinstall_from_preserved_state


class MVP11ReinstallTests(unittest.TestCase):
    def test_prepare_reinstall_restores_identity_and_resets_only_execution_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            config = root / "config"
            preserved = state / "uninstall-preserved-config"
            preserved.mkdir(parents=True)
            instance_id = str(uuid.uuid4())
            (preserved / "instance-id").write_text(instance_id + "\n", encoding="utf-8")
            (preserved / "mcp-port").write_text("17897\n", encoding="utf-8")
            (state / "install-run.json").write_text('{"checkpoint":"finalize_manifest"}\n', encoding="utf-8")
            durable = state / "cards-survive.txt"
            durable.write_text("durable", encoding="utf-8")
            secret = state / "secrets" / "x" / "1.secret"
            secret.parent.mkdir(parents=True)
            secret.write_text("never-delete-me", encoding="utf-8")

            result = prepare_reinstall_from_preserved_state(state_root=state, config_root=config)
            self.assertEqual(result["state"], "RESTORED")
            self.assertTrue(result["install_checkpoint_reset"])
            self.assertEqual((config / "instance-id").read_text(encoding="utf-8").strip(), instance_id)
            self.assertEqual((config / "mcp-port").read_text(encoding="utf-8").strip(), "17897")
            self.assertFalse((state / "install-run.json").exists())
            self.assertEqual(durable.read_text(encoding="utf-8"), "durable")
            self.assertEqual(secret.read_text(encoding="utf-8"), "never-delete-me")


if __name__ == "__main__":
    unittest.main()
