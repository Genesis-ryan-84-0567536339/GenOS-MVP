from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from genos.agent_runtime import (
    AgentBusyError,
    AgentRuntimeError,
    AgentRuntimeStore,
    GeminiCliAdapter,
    ProviderProbe,
    TARGET_APPROVAL_MODE,
    TARGET_MODEL,
    TARGET_THINKING_LEVEL,
)


class AgentIdentityTests(unittest.TestCase):
    def test_seed_is_durable_and_bound_to_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agy-gen"
            first = AgentRuntimeStore(root)
            identity = first.ensure_seed(instance_id="instance-a")
            second = AgentRuntimeStore(root)
            again = second.ensure_seed(instance_id="instance-a")
            self.assertEqual(identity["agent_id"], "agy-gen")
            self.assertEqual(identity, again)
            self.assertEqual(identity["concurrency"], 1)
            self.assertEqual(identity["provider_target"]["model"], TARGET_MODEL)
            self.assertEqual(identity["provider_target"]["thinking_level"], TARGET_THINKING_LEVEL)
            self.assertEqual(identity["provider_target"]["approval_mode"], TARGET_APPROVAL_MODE)
            self.assertEqual(identity["runtime_binding"]["session_name"], "agy-gen")
            with self.assertRaises(AgentRuntimeError):
                second.ensure_seed(instance_id="instance-b")

    def test_seed_creates_high_thinking_system_settings_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            payload = json.loads(store.settings_path.read_text(encoding="utf-8"))
            override = payload["modelConfigs"]["customOverrides"][0]
            self.assertEqual(override["match"]["model"], TARGET_MODEL)
            self.assertEqual(
                override["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"],
                TARGET_THINKING_LEVEL,
            )
            self.assertNotIn("token", store.settings_path.read_text(encoding="utf-8").lower())
            self.assertNotIn("api_key", store.settings_path.read_text(encoding="utf-8").lower())

    def test_memory_and_skill_revisions_survive_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agy-gen"
            store = AgentRuntimeStore(root)
            store.ensure_seed(instance_id="instance-a")
            store.append_revision("memory", "owner-preferences", "first", source="owner")
            store.append_revision("memory", "owner-preferences", "second", source="owner")
            store.append_revision("skill", "system-doctor", "v1", source="bootstrap")

            reopened = AgentRuntimeStore(root)
            memory = reopened.list_revisions("memory", "owner-preferences")
            skills = reopened.list_revisions("skill", "system-doctor")
            self.assertEqual([item["revision"] for item in memory], [1, 2])
            self.assertEqual(memory[-1]["content"], "second")
            self.assertEqual(skills[0]["source"], "bootstrap")


class WorkClaimTests(unittest.TestCase):
    def test_concurrency_one_rejects_overlapping_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            store.claim_work(task_id="task-1")
            with self.assertRaises(AgentBusyError):
                store.claim_work(task_id="task-2")
            store.release_work(task_id="task-1")
            store.claim_work(task_id="task-2")
            store.release_work(task_id="task-2")

    def test_queue_requires_direct_provider_activation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            with self.assertRaises(Exception) as raised:
                store.queue_task("do work")
            self.assertIn("provider is not ACTIVE", str(raised.exception))
            self.assertFalse(store.claim_path.exists())


class GeminiAdapterTests(unittest.TestCase):
    def _fake_gemini(self, root: Path, *, activation_pass: bool) -> Path:
        binary = root / "gemini"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('9.9.9-test')\n"
            "    raise SystemExit(0)\n"
            "prompt = sys.argv[sys.argv.index('--prompt') + 1] if '--prompt' in sys.argv else ''\n"
            + (
                "marker = prompt.split(':', 1)[-1].strip()\n"
                "print(json.dumps({'response': marker}))\n"
                "raise SystemExit(0)\n"
                if activation_pass
                else "print(json.dumps({'error': 'not-authenticated'}))\nraise SystemExit(2)\n"
            ),
            encoding="utf-8",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary

    def test_real_activation_probe_marks_active_only_after_marker_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_gemini(root, activation_pass=True)
            probe = GeminiCliAdapter(store, binary=str(binary)).activate_with_real_probe(timeout=10)
            self.assertEqual(probe.state, "ACTIVE")
            self.assertEqual(probe.evidence, "REAL_MODEL_PROBE_PASS")
            persisted = store.provider()
            self.assertEqual(persisted["state"], "ACTIVE")
            self.assertEqual(persisted["model"], TARGET_MODEL)

    def test_failed_auth_or_model_probe_never_fakes_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_gemini(root, activation_pass=False)
            probe = GeminiCliAdapter(store, binary=str(binary)).activate_with_real_probe(timeout=10)
            self.assertEqual(probe.state, "NEEDS_ACTION")
            self.assertEqual(probe.evidence, "AUTH_MODEL_OR_CONFIG_NOT_VERIFIED")

    def test_task_command_uses_exact_owner_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_gemini(root, activation_pass=True)
            adapter = GeminiCliAdapter(store, binary=str(binary))
            command = adapter.command_for_prompt("hello")
            self.assertIn("--model", command)
            self.assertEqual(command[command.index("--model") + 1], TARGET_MODEL)
            self.assertIn("--approval-mode=yolo", command)
            self.assertEqual(command[command.index("--output-format") + 1], "json")
            self.assertEqual(command[command.index("--prompt") + 1], "hello")

    def test_provider_metadata_contains_no_auth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            probe = ProviderProbe(
                state="NEEDS_ACTION",
                cli_path=None,
                cli_version=None,
                model=TARGET_MODEL,
                thinking_level=TARGET_THINKING_LEVEL,
                approval_mode=TARGET_APPROVAL_MODE,
                observed_at="2026-08-20T00:00:00Z",
                evidence="GEMINI_CLI_NOT_FOUND",
            )
            store.write_provider(probe)
            text = store.provider_path.read_text(encoding="utf-8").lower()
            for forbidden in ("password", "refresh_token", "api_key", "authorization"):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
