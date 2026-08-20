from __future__ import annotations

import io
import hashlib
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

from genos.boundary import ProfileState, REFERENCE_PROFILES, decide_boundary
from genos.contracts import Observation, ObservationState, SupportClass
from genos.install import InstallError, ReleaseArtifact, _safe_extract_tar, build_native_install


def _obs(check_id: str, state: ObservationState, observed: object) -> Observation:
    return Observation(
        check_id=check_id,
        state=state,
        observed=observed,
        source="fixture",
        observed_at="2026-08-20T00:00:00Z",
    )


def supported_observations() -> list[Observation]:
    return [
        _obs(
            "platform",
            ObservationState.PASS,
            {
                "system": "Linux",
                "distribution": "ubuntu",
                "distribution_version": "24.04",
                "machine": "x86_64",
            },
        ),
        _obs("systemd", ObservationState.PASS, {"state": "running"}),
    ]


def _release(root: Path) -> ReleaseArtifact:
    archive = root / "release.tar.gz"
    archive.write_bytes(b"fixture")
    return ReleaseArtifact(
        archive=archive,
        git_sha="a" * 40,
        sha256=hashlib.sha256(b"fixture").hexdigest(),
    )


class BoundaryTests(unittest.TestCase):
    def test_missing_mode_requires_action_without_mutation(self) -> None:
        decision = decide_boundary(supported_observations(), None)
        self.assertEqual(decision.state, "NEEDS_ACTION")
        self.assertFalse(decision.mutation_allowed)
        self.assertTrue(decision.requires_confirmation)

    def test_offline_systemd_blocks_before_mutation(self) -> None:
        observations = supported_observations()
        observations[-1] = _obs("systemd", ObservationState.UNKNOWN, {"state": "offline"})
        decision = decide_boundary(observations, "native")
        self.assertEqual(decision.support_class, SupportClass.UNSUPPORTED)
        self.assertFalse(decision.mutation_allowed)

    def test_vm_mode_does_not_fake_certification(self) -> None:
        decision = decide_boundary(supported_observations(), "vm")
        self.assertEqual(decision.support_class, SupportClass.SUPPORTED_WITH_ACTION)
        self.assertFalse(decision.mutation_allowed)
        self.assertIsNone(decision.profile_id)

    def test_verified_profile_is_supported_for_normal_install(self) -> None:
        self.assertEqual(REFERENCE_PROFILES[0].state, ProfileState.VERIFIED)
        decision = decide_boundary(supported_observations(), "native")
        self.assertEqual(decision.state, "SUPPORTED")
        self.assertEqual(decision.support_class, SupportClass.SUPPORTED)
        self.assertTrue(decision.mutation_allowed)

    def test_candidate_profile_can_only_be_opened_for_e2e(self) -> None:
        # The public matrix currently contains only verified profiles. This
        # fixture temporarily replaces the tuple's state through module-level
        # patching only to prove the candidate-only gate contract.
        from unittest.mock import patch
        import genos.boundary as boundary

        verified = REFERENCE_PROFILES[0]
        candidate = type(verified)(
            profile_id=verified.profile_id,
            distribution=verified.distribution,
            version=verified.version,
            architecture=verified.architecture,
            mode=verified.mode,
            state=ProfileState.CANDIDATE,
            package_manager=verified.package_manager,
            notes="candidate fixture",
        )
        with patch.object(boundary, "REFERENCE_PROFILES", (candidate,)):
            decision = boundary.decide_boundary(supported_observations(), "native", allow_candidate_e2e=True)
        self.assertEqual(decision.state, "CANDIDATE_E2E_ONLY")
        self.assertTrue(decision.mutation_allowed)
        self.assertEqual(decision.support_class, SupportClass.SUPPORTED_WITH_ACTION)

    def test_candidate_profile_is_not_supported_for_normal_install(self) -> None:
        from unittest.mock import patch
        import genos.boundary as boundary

        verified = REFERENCE_PROFILES[0]
        candidate = type(verified)(
            profile_id=verified.profile_id,
            distribution=verified.distribution,
            version=verified.version,
            architecture=verified.architecture,
            mode=verified.mode,
            state=ProfileState.CANDIDATE,
            package_manager=verified.package_manager,
            notes="candidate fixture",
        )
        with patch.object(boundary, "REFERENCE_PROFILES", (candidate,)):
            decision = boundary.decide_boundary(supported_observations(), "native")
        self.assertEqual(decision.state, "SUPPORTED_WITH_ACTION")
        self.assertFalse(decision.mutation_allowed)


class ReleaseAndPlanTests(unittest.TestCase):
    def test_release_checksum_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = _release(root)
            invalid = ReleaseArtifact(archive=artifact.archive, git_sha=artifact.git_sha, sha256="0" * 64)
            with self.assertRaisesRegex(InstallError, "checksum mismatch"):
                invalid.verify()

    def test_plan_hash_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = _release(Path(tmp))
            first = build_native_install(supported_observations(), requested_mode="native", release=release)
            second = build_native_install(supported_observations(), requested_mode="native", release=release)
            self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)
            self.assertTrue(first.plan.steps)

    def test_vm_boundary_has_no_native_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = _release(Path(tmp))
            planned = build_native_install(supported_observations(), requested_mode="vm", release=release)
            self.assertEqual(planned.plan.steps, [])

    def test_unresolved_boundary_has_no_native_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = _release(Path(tmp))
            planned = build_native_install(supported_observations(), requested_mode=None, release=release)
            self.assertEqual(planned.plan.steps, [])

    def test_candidate_plan_cannot_execute_without_e2e_gate(self) -> None:
        from unittest.mock import patch
        import genos.boundary as boundary
        import genos.install as install

        verified = REFERENCE_PROFILES[0]
        candidate = type(verified)(
            profile_id=verified.profile_id,
            distribution=verified.distribution,
            version=verified.version,
            architecture=verified.architecture,
            mode=verified.mode,
            state=ProfileState.CANDIDATE,
            package_manager=verified.package_manager,
            notes="candidate fixture",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(boundary, "REFERENCE_PROFILES", (candidate,)), patch.object(
            install, "get_profile", boundary.get_profile
        ):
            release = _release(Path(tmp))
            normal = install.build_native_install(supported_observations(), requested_mode="native", release=release)
            gated = install.build_native_install(
                supported_observations(), requested_mode="native", release=release, allow_candidate_e2e=True
            )
        self.assertFalse(normal.decision.mutation_allowed)
        self.assertTrue(gated.decision.mutation_allowed)

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
        # This is a generic HTTP service lifecycle test, so keep it independent
        # of Product API's PostgreSQL/Owner authority introduced in MVP-03.
        # Product API + DB integration is covered by the dedicated fresh-host
        # E2E. The runtime role exercises the same serve_http/SIGTERM path.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [sys.executable, "-m", "genos.core_service", "runtime", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            payload = None
            for _ in range(100):
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=1)
                    self.fail(f"core service exited before health: stdout={stdout[-500:]!r} stderr={stderr[-500:]!r}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.25) as response:
                        payload = json.load(response)
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertIsNotNone(payload, "core service did not become healthy")
            self.assertEqual(payload["role"], "runtime")
            process.terminate()
            returncode = process.wait(timeout=5)
            self.assertEqual(returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
