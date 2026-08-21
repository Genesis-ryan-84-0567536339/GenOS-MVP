from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from genos.agent_cli_update import (
    AGY_TRUSTED_MANIFEST_HOST,
    AgentCliUpdateError,
    AntigravityCliManager,
)


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


class FakeOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float):
        self.calls.append(url)
        try:
            return io.BytesIO(self.payloads[url])
        except KeyError as exc:
            raise OSError(f"unexpected fixture URL: {url}") from exc


class OfficialManifestStagingTests(unittest.TestCase):
    @staticmethod
    def _release_archive(version: str) -> bytes:
        executable = (
            "#!/bin/sh\n"
            f"if [ \"$1\" = \"--version\" ]; then echo '{version}'; exit 0; fi\n"
            "if [ \"$1\" = \"--help\" ]; then echo '--model --effort --output-format'; exit 0; fi\n"
            "exit 0\n"
        ).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("antigravity")
            info.size = len(executable)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(executable))
        return output.getvalue()

    def test_stage_uses_installer_published_manifest_and_sha512(self) -> None:
        version = "1.2.3"
        manifest_base = f"https://{AGY_TRUSTED_MANIFEST_HOST}"
        manifest_url = f"{manifest_base}/manifests/linux_amd64.json"
        archive_url = (
            "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
            f"{version}-fixture/linux-x64/cli_linux_x64.tar.gz"
        )
        archive = self._release_archive(version)
        installer = f'DOWNLOAD_BASE_URL="{manifest_base}"\n'.encode()
        manifest = json.dumps(
            {
                "version": version,
                "url": archive_url,
                "sha512": hashlib.sha512(archive).hexdigest(),
            }
        ).encode()
        opener = FakeOpener(
            {
                "https://antigravity.google/cli/install.sh": installer,
                manifest_url: manifest,
                archive_url: archive,
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = AntigravityCliManager(root / "tools", agent_state_root=root / "agy-gen", opener=opener)
            manager._ensure_writable_layout()
            staged = manager._stage_latest()
            candidate = Path(staged["binary"])
            try:
                self.assertEqual(staged["version"], version)
                self.assertEqual(staged["installer_sha256"], hashlib.sha256(installer).hexdigest())
                self.assertEqual(staged["binary_sha256"], hashlib.sha256(candidate.read_bytes()).hexdigest())
                self.assertEqual(opener.calls, ["https://antigravity.google/cli/install.sh", manifest_url, archive_url])
            finally:
                candidate.unlink(missing_ok=True)

    def test_manifest_rejects_non_google_release_url(self) -> None:
        manager = AntigravityCliManager("/tmp/unused-agy-test")
        with self.assertRaises(AgentCliUpdateError):
            manager._validate_manifest(
                {
                    "version": "1.2.3",
                    "url": "https://example.com/cli_linux_x64.tar.gz",
                    "sha512": "a" * 128,
                }
            )

    def test_release_archive_rejects_symlink_entries(self) -> None:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("antigravity")
            info.type = tarfile.SYMTYPE
            info.linkname = "/bin/sh"
            archive.addfile(info)
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "bad.tar.gz"
            archive_path.write_bytes(output.getvalue())
            with self.assertRaises(AgentCliUpdateError):
                AntigravityCliManager._extract_release_binary(archive_path, Path(temp) / "agy")

    def test_release_archive_rejects_path_escape(self) -> None:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            payload = b"x"
            info = tarfile.TarInfo("../antigravity")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "bad.tar.gz"
            archive_path.write_bytes(output.getvalue())
            with self.assertRaises(AgentCliUpdateError):
                AntigravityCliManager._extract_release_binary(archive_path, Path(temp) / "agy")


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
