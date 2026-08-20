from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request

from genos.boundary import BoundaryMode, decide_boundary
from genos.contracts import Observation, ObservationState, SupportClass
from genos.install import (
    InstallError,
    NativeProvisioner,
    ReleaseArtifact,
    _safe_extract_tar,
    build_native_install,
)


GIT_SHA = "1" * 40


def candidate_observations() -> list[Observation]:
    return [
        Observation(
            "platform",
            ObservationState.PASS,
            observed={
                "system": "Linux",
                "distribution": "ubuntu",
                "distribution_version": "24.04",
                "machine": "x86_64",
            },
        ),
        Observation("systemd", ObservationState.PASS, observed={"status": "running"}),
    ]


def make_release(root: Path) -> ReleaseArtifact:
    source = root / "source"
    package = source / "src" / "genos"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "test"\n', encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    archive = root / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source / "src", arcname="src")
        handle.add(source / "README.md", arcname="README.md")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return ReleaseArtifact(archive=archive, git_sha=GIT_SHA, sha256=digest)


class BoundaryTests(unittest.TestCase):
    def test_missing_mode_requires_action_without_mutation(self) -> None:
        decision = decide_boundary(candidate_observations(), None)
        self.assertEqual(decision.state, "NEEDS_ACTION")
        self.assertFalse(decision.mutation_allowed)
        self.assertTrue(decision.requires_confirmation)

    def test_candidate_profile_is_not_supported_for_normal_install(self) -> None:
        decision = decide_boundary(candidate_observations(), "native")
        self.assertEqual(decision.support_class, SupportClass.SUPPORTED_WITH_ACTION)
        self.assertEqual(decision.state, "SUPPORTED_WITH_ACTION")
        self.assertFalse(decision.mutation_allowed)

    def test_candidate_profile_can_only_be_opened_for_e2e(self) -> None:
        decision = decide_boundary(candidate_observations(), "native", allow_candidate_e2e=True)
        self.assertEqual(decision.mode, BoundaryMode.NATIVE)
        self.assertEqual(decision.state, "CANDIDATE_E2E_ONLY")
        self.assertFalse(decision.support_class is SupportClass.SUPPORTED)
        self.assertTrue(decision.mutation_allowed)

    def test_vm_mode_does_not_fake_certification(self) -> None:
        decision = decide_boundary(candidate_observations(), "vm")
        self.assertEqual(decision.mode, BoundaryMode.VM)
        self.assertEqual(decision.support_class, SupportClass.SUPPORTED_WITH_ACTION)
        self.assertFalse(decision.mutation_allowed)

    def test_offline_systemd_blocks_before_mutation(self) -> None:
        observations = candidate_observations()
        observations[1] = Observation("systemd", ObservationState.FAIL, observed={"status": "offline"})
        decision = decide_boundary(observations, "native", allow_candidate_e2e=True)
        self.assertEqual(decision.support_class, SupportClass.UNSUPPORTED)
        self.assertFalse(decision.mutation_allowed)


class ReleaseAndPlanTests(unittest.TestCase):
    def test_release_checksum_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            bad = ReleaseArtifact(release.archive, release.git_sha, "0" * 64)
            with self.assertRaises(InstallError):
                bad.verify()

    def test_plan_hash_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            first = build_native_install(candidate_observations(), requested_mode="native", release=release)
            second = build_native_install(candidate_observations(), requested_mode="native", release=release)
            self.assertNotEqual(first.plan.plan_id, second.plan.plan_id)
            self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)
            self.assertEqual(len(first.plan.steps), 12)

    def test_normal_candidate_plan_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            planned = build_native_install(candidate_observations(), requested_mode="native", release=release)
            with self.assertRaisesRegex(InstallError, "mutation blocked"):
                NativeProvisioner(planned, state_root=Path(tmp) / "state").execute()

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "evil.tar.gz"
            payload = b"escape"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("../escape.txt")
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(InstallError, "escapes destination"):
                _safe_extract_tar(archive, destination)
            self.assertFalse((root / "escape.txt").exists())

    def test_archive_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "link.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("src/genos/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                handle.addfile(info)
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(InstallError, "unsupported link/device"):
                _safe_extract_tar(archive, destination)


class CoreServiceTests(unittest.TestCase):
    def test_http_service_exits_cleanly_on_sigterm(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [sys.executable, "-m", "genos.core_service", "product-api", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            payload = None
            for _ in range(50):
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                        payload = json.load(response)
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertIsNotNone(payload, "core service did not become healthy")
            self.assertEqual(payload["role"], "product-api")
            process.terminate()
            returncode = process.wait(timeout=5)
            self.assertEqual(returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
