from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import uuid

from genos.agent_cli_update import AGY_INSTALLER_URL, AGY_UPDATE_INTERVAL_SECONDS
from genos.agent_runtime import (
    AgentBusyError,
    AgentRuntimeError,
    AgentRuntimeStore,
    AntigravityCliAdapter,
    ProviderProbe,
    TARGET_APPROVAL_MODE,
    TARGET_MODEL,
    TARGET_PROVIDER,
    TARGET_THINKING_LEVEL,
)
from genos.agent_secure_runtime import SecretAwareAntigravityAdapter
from genos.agent_tools import NODE_SHA256, NODE_URL, NODE_VERSION


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
            self.assertEqual(identity["provider_target"]["provider"], "antigravity")
            self.assertEqual(identity["provider_target"]["model"], TARGET_MODEL)
            self.assertEqual(identity["provider_target"]["thinking_level"], TARGET_THINKING_LEVEL)
            self.assertEqual(identity["provider_target"]["approval_mode"], TARGET_APPROVAL_MODE)
            self.assertEqual(identity["runtime_binding"]["session_name"], "agy-gen")
            with self.assertRaises(AgentRuntimeError):
                second.ensure_seed(instance_id="instance-b")

    def test_seed_creates_provider_settings_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            payload = json.loads(store.settings_path.read_text(encoding="utf-8"))
            self.assertIn("permissions", payload)
            raw = store.settings_path.read_text(encoding="utf-8").lower()
            self.assertNotIn("token", raw)
            self.assertNotIn("api_key", raw)

    def test_existing_identity_migrates_provider_binding_without_identity_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agy-gen"
            store = AgentRuntimeStore(root)
            original = store.ensure_seed(instance_id="instance-a")
            original_created = original["created_at"]
            payload = json.loads(store.identity_path.read_text(encoding="utf-8"))
            payload["provider_target"]["provider"] = "gemini-cli"
            store.identity_path.write_text(json.dumps(payload), encoding="utf-8")
            migrated = AgentRuntimeStore(root).ensure_seed(instance_id="instance-a")
            self.assertEqual(migrated["agent_id"], "agy-gen")
            self.assertEqual(migrated["created_at"], original_created)
            self.assertEqual(migrated["provider_target"]["provider"], TARGET_PROVIDER)

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


class AntigravityAdapterTests(unittest.TestCase):
    def _fake_agy(self, root: Path, *, activation_pass: bool) -> Path:
        binary = root / "agy"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('9.9.9-test')\n"
            "    raise SystemExit(0)\n"
            "if '--help' in sys.argv:\n"
            "    print('--model --effort --output-format --dangerously-skip-permissions')\n"
            "    raise SystemExit(0)\n"
            "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
            + (
                "marker = prompt.split(':', 1)[-1].strip()\n"
                "print(json.dumps({'status':'SUCCESS','response':marker}))\n"
                "raise SystemExit(0)\n"
                if activation_pass
                else "print(json.dumps({'status':'ERROR','error':'not-authenticated'}))\nraise SystemExit(2)\n"
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
            binary = self._fake_agy(root, activation_pass=True)
            probe = AntigravityCliAdapter(store, binary=str(binary)).activate_with_real_probe(timeout=10)
            self.assertEqual(probe.state, "ACTIVE")
            self.assertEqual(probe.evidence, "REAL_MODEL_PROBE_PASS")
            persisted = store.provider()
            self.assertEqual(persisted["provider_cli"], "antigravity")
            self.assertEqual(persisted["state"], "ACTIVE")
            self.assertEqual(persisted["model"], TARGET_MODEL)

    def test_failed_auth_or_model_probe_never_fakes_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_agy(root, activation_pass=False)
            probe = AntigravityCliAdapter(store, binary=str(binary)).activate_with_real_probe(timeout=10)
            self.assertEqual(probe.state, "NEEDS_ACTION")
            self.assertEqual(probe.evidence, "AUTH_MODEL_OR_CONFIG_NOT_VERIFIED")

    def test_task_command_uses_exact_owner_preset_and_disables_native_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_agy(root, activation_pass=True)
            adapter = AntigravityCliAdapter(store, binary=str(binary))
            command = adapter.command_for_prompt("hello")
            self.assertEqual(command[command.index("--model") + 1], "gemini-3.7-flash-high")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            self.assertIn("--dangerously-skip-permissions", command)
            self.assertEqual(command[command.index("--output-format") + 1], "json")
            self.assertEqual(command[command.index("-p") + 1], "hello")
            self.assertEqual(adapter._env()["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")

    def test_provider_metadata_contains_no_auth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentRuntimeStore(Path(temp) / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            probe = ProviderProbe(
                state="NEEDS_ACTION", cli_path=None, cli_version=None, model=TARGET_MODEL,
                thinking_level=TARGET_THINKING_LEVEL, approval_mode=TARGET_APPROVAL_MODE,
                observed_at="2026-08-20T00:00:00Z", evidence="AGY_CLI_NOT_FOUND",
            )
            store.write_provider(probe)
            text = store.provider_path.read_text(encoding="utf-8").lower()
            for forbidden in ("password", "refresh_token", "api_key", "authorization"):
                self.assertNotIn(forbidden, text)

    def test_secretref_api_key_is_process_only_and_binding_is_safe(self) -> None:
        class FakeResolver:
            def resolve_api_key(self, secret_id: str) -> str:
                self.secret_id = secret_id
                return "raw-test-api-key-never-persist"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = AgentRuntimeStore(root / "agy-gen")
            store.ensure_seed(instance_id="instance-a")
            binary = self._fake_agy(root, activation_pass=True)
            secret_id = str(uuid.uuid4())
            resolver = FakeResolver()
            adapter = SecretAwareAntigravityAdapter(store, binary=str(binary), credential_id=secret_id, resolver=resolver)  # type: ignore[arg-type]
            child_env = adapter._env()
            self.assertEqual(child_env["GEMINI_API_KEY"], "raw-test-api-key-never-persist")
            self.assertEqual(child_env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
            probe = adapter.activate_with_real_probe(timeout=10)
            self.assertEqual(probe.state, "ACTIVE")
            self.assertEqual(probe.binding_ref, secret_id)
            settings = json.loads(store.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["modelProvider"], "gemini")
            persisted = store.provider_path.read_text(encoding="utf-8")
            self.assertIn(secret_id, persisted)
            self.assertNotIn("raw-test-api-key-never-persist", persisted)
            reopened = SecretAwareAntigravityAdapter(store, binary=str(binary), resolver=resolver)  # type: ignore[arg-type]
            self.assertEqual(reopened.credential_id, secret_id)


class ToolchainPolicyTests(unittest.TestCase):
    def test_node_is_reproducibly_pinned_but_agy_uses_managed_stable_channel(self) -> None:
        self.assertEqual(NODE_VERSION, "24.19.0")
        self.assertEqual(len(NODE_SHA256), 64)
        self.assertEqual(NODE_URL, "https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz")
        self.assertEqual(AGY_INSTALLER_URL, "https://antigravity.google/cli/install.sh")
        self.assertEqual(AGY_UPDATE_INTERVAL_SECONDS, 21600)


if __name__ == "__main__":
    unittest.main()
