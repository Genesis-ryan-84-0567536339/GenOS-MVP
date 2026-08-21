from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
import uuid

from genos.install import ReleaseArtifact
from genos.lifecycle import LifecycleError, LifecyclePaths
from genos.lifecycle_hardened import HardenedLifecycleService, restore_preserved_install_identity


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, *, check=True, timeout=120.0):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")


class LiveFixtureLifecycle(HardenedLifecycleService):
    def _is_live_layout(self) -> bool:
        return True

    def _require_root_for_live_mutation(self) -> None:
        return


class MVP11LifecycleTests(unittest.TestCase):
    def _layout(self, root: Path):
        paths = LifecyclePaths(
            state=root / "state",
            config=root / "config",
            opt=root / "opt",
            systemd=root / "systemd",
            run=root / "run",
        )
        for path in (paths.state, paths.config, paths.releases, paths.systemd, paths.run):
            path.mkdir(parents=True, exist_ok=True)
        instance_id = str(uuid.uuid4())
        (paths.config / "instance-id").write_text(instance_id + "\n", encoding="utf-8")
        (paths.config / "mcp-port").write_text("17883\n", encoding="utf-8")
        (paths.config / "genos.env").write_text(f"GENOS_INSTANCE_ID={instance_id}\nGENOS_MCP_PORT=17883\n", encoding="utf-8")
        (paths.state / "fixture-product-db.dump").write_bytes(b"DB-V1\n")
        (paths.state / "data.json").write_text('{"value":"before"}\n', encoding="utf-8")
        secrets = paths.state / "secrets" / "fixture"
        secrets.mkdir(parents=True)
        (secrets / "1.secret").write_text("RAW_FIXTURE_SECRET_KEEP_ME", encoding="utf-8")
        old = paths.releases / ("a" * 40)
        (old / "src" / "genos").mkdir(parents=True)
        (old / "src" / "genos" / "__init__.py").write_text("__version__='old'\n", encoding="utf-8")
        (old / ".genos-release-sha256").write_text("0" * 64 + "\n", encoding="utf-8")
        paths.current.symlink_to(old)
        return paths, instance_id, old

    def _release(self, root: Path, git_sha: str):
        source = root / "candidate"
        package = source / "src" / "genos"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("__version__='candidate'\n", encoding="utf-8")
        archive = root / "candidate.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for path in sorted(source.rglob("*")):
                tf.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return ReleaseArtifact(archive=archive, git_sha=git_sha, sha256=digest)

    def test_default_backup_restore_preserves_current_secret_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            paths, instance_id, _old = self._layout(Path(temp))
            service = HardenedLifecycleService(paths=paths, health_probe=lambda: {"state": "PASS"})
            backup = service.backup(output=Path(temp) / "backup.tar.gz")
            self.assertFalse(backup["include_secrets"])

            (paths.state / "data.json").write_text('{"value":"mutated"}\n', encoding="utf-8")
            secret = paths.state / "secrets" / "fixture" / "1.secret"
            secret.write_text("CURRENT_SECRET_AFTER_BACKUP", encoding="utf-8")
            (paths.state / "fixture-product-db.dump").write_bytes(b"DB-MUTATED\n")

            restored = service.restore(
                archive=Path(backup["archive"]),
                expected_sha256=str(backup["sha256"]),
            )
            self.assertEqual(restored["state"], "SUCCEEDED")
            self.assertEqual(restored["instance_id"], instance_id)
            self.assertIn("before", (paths.state / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(secret.read_text(encoding="utf-8"), "CURRENT_SECRET_AFTER_BACKUP")
            self.assertEqual((paths.state / "fixture-product-db.dump").read_bytes(), b"DB-V1\n")

    def test_update_failure_restores_previous_release_state_and_database_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, _instance_id, old = self._layout(root)
            release = self._release(root, "b" * 40)
            states = iter(({"state": "FAIL"}, {"state": "PASS"}))
            service = HardenedLifecycleService(paths=paths, health_probe=lambda: next(states))
            with self.assertRaisesRegex(LifecycleError, "checkpoint were restored"):
                service.update(release=release)
            self.assertEqual(paths.current.resolve(), old.resolve())
            self.assertIn("before", (paths.state / "data.json").read_text(encoding="utf-8"))
            self.assertEqual((paths.state / "fixture-product-db.dump").read_bytes(), b"DB-V1\n")
            self.assertTrue((paths.state / "secrets" / "fixture" / "1.secret").is_file())

    def test_uninstall_preserved_identity_can_be_reused_before_reinstall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            config = root / "config"
            preserved = state / "uninstall-preserved-config"
            preserved.mkdir(parents=True)
            instance_id = str(uuid.uuid4())
            (preserved / "instance-id").write_text(instance_id + "\n", encoding="utf-8")
            (preserved / "mcp-port").write_text("17891\n", encoding="utf-8")
            result = restore_preserved_install_identity(state_root=state, config_root=config)
            self.assertEqual(result["state"], "RESTORED")
            self.assertEqual((config / "instance-id").read_text(encoding="utf-8").strip(), instance_id)
            self.assertEqual((config / "mcp-port").read_text(encoding="utf-8").strip(), "17891")
            self.assertFalse((config / "genos.env").exists())

    def test_purge_drops_local_product_database_and_never_deletes_remote_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, instance_id, _old = self._layout(root)
            runner = FakeRunner()
            service = LiveFixtureLifecycle(paths=paths, runner=runner, health_probe=lambda: {"state": "PASS"})
            result = service.purge(confirm_instance_id=instance_id)
            self.assertEqual(result["state"], "PURGED")
            self.assertTrue(result["product_database_deleted"])
            self.assertFalse(result["remote_resources_deleted"])
            commands = [" ".join(call) for call in runner.calls]
            self.assertTrue(any("dropdb --if-exists genos" in call for call in commands), commands)
            self.assertTrue(any("DROP ROLE IF EXISTS genos" in call for call in commands), commands)
            self.assertFalse(paths.state.exists())
            self.assertFalse(paths.config.exists())
            self.assertFalse(paths.opt.exists())

    def test_support_bundle_excludes_secret_provider_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, _instance_id, _old = self._layout(root)
            raw = "RAW_FIXTURE_SECRET_KEEP_ME"
            service = HardenedLifecycleService(paths=paths, health_probe=lambda: {"state": "PASS"})
            result = service.support_bundle(output=root / "support.tar.gz")
            self.assertTrue(result["redacted"])
            self.assertFalse(result["raw_secret_included"])
            with tarfile.open(result["archive"], "r:gz") as tf:
                combined = b"".join(
                    tf.extractfile(member).read()
                    for member in tf.getmembers()
                    if member.isfile() and tf.extractfile(member) is not None
                )
            self.assertNotIn(raw.encode(), combined)

    def test_bootstrap_requires_checksum_before_release_execution(self):
        root = Path(__file__).resolve().parents[1]
        bootstrap = (root / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("release checksum mismatch", bootstrap)
        self.assertIn("unsafe release member", bootstrap)
        self.assertIn("python3 -m genos install", bootstrap)
        self.assertNotIn("curl |", bootstrap)
        self.assertNotIn("wget |", bootstrap)


if __name__ == "__main__":
    unittest.main()
