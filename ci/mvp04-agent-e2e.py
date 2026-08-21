from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


BASE = "http://127.0.0.1:17880"
USERNAME = "mvp04-owner"
PASSWORD = "MVP04-owner-password-fixture-12345"
AGENT_ROOT = Path("/var/lib/genos/agents/agy-gen")
TARGET_MODEL = "gemini-3.7-flash-high"


def request(method: str, path: str, *, payload: dict[str, object] | None = None, token: str | None = None) -> tuple[int, dict[str, object]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8"); headers["Content-Type"] = "application/json"
    if token: headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def run_genos(*args: str, timeout: int = 180) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", "genos", *args, "--json"], capture_output=True, text=True, check=False, timeout=timeout,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "/opt/genos/current/src", "HOME": "/var/lib/genos", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1", "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        },
    )
    try: payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"genos command returned non-JSON output rc={completed.returncode} stderr={completed.stderr[-500:]!r}") from exc
    if completed.returncode != 0: raise AssertionError(f"genos command failed rc={completed.returncode} payload={payload}")
    return payload


def login() -> str:
    status, payload = request("POST", "/api/v1/auth/login", payload={"username": USERNAME, "password": PASSWORD})
    assert status == 200, (status, payload)
    token = str(payload.get("session_token") or ""); assert len(token) >= 32
    return token


def bootstrap_with_stdin_secret() -> str:
    raw_api_key = sys.stdin.read(); assert raw_api_key and "\n" not in raw_api_key, "missing or malformed API key on stdin"
    status, payload = request("POST", "/api/v1/owner/bootstrap", payload={"username": USERNAME, "password": PASSWORD})
    assert status == 201, (status, payload)
    token = login()
    status, payload = request(
        "POST", "/api/v1/credentials", token=token,
        payload={"name": "mvp04-gemini-api-key", "provider": "gemini", "secret": raw_api_key, "consumer_scopes": ["agy-gen"]},
    )
    assert status == 201, (status, payload)
    credential = payload["credential"]; secret_id = str(credential["secret_id"])
    assert credential["status"] == "ACTIVE" and credential["consumer_scopes"] == ["agy-gen"]
    assert raw_api_key not in json.dumps(payload)

    activation = run_genos("agent", "activate", "--credential-id", secret_id, timeout=180)
    assert activation["state"] == "ACTIVE", activation
    assert activation["provider_cli"] == "antigravity", activation
    assert activation["model"] == TARGET_MODEL, activation
    assert activation["thinking_level"] == "HIGH", activation
    assert activation["approval_mode"] == "yolo", activation
    assert activation.get("binding_ref") == secret_id, activation

    provider_text = (AGENT_ROOT / "provider.json").read_text(encoding="utf-8")
    assert raw_api_key not in provider_text and "GEMINI_API_KEY" not in provider_text and secret_id in provider_text

    from genos.agent_runtime import AgentRuntimeStore
    store = AgentRuntimeStore(AGENT_ROOT)
    store.append_revision("memory", "mvp04-e2e", "persistent-evidence", source="acceptance")
    identity = store.identity(); (AGENT_ROOT / "acceptance-identity-id.txt").write_text(str(identity["agent_id"]) + "\n", encoding="utf-8")
    print(secret_id); return secret_id


def execute_real_task(label: str) -> None:
    marker = f"GENOS_MVP04_REAL_TASK_{label}_{uuid.uuid4().hex[:10]}"
    queued = run_genos("agent", "task", "--prompt", f"Reply with exactly this marker and nothing else: {marker}")
    task_id = str(queued["task_id"]); result_path = AGENT_ROOT / "tasks" / "results" / f"{task_id}.json"
    result: dict[str, object] | None = None
    for _ in range(300):
        if result_path.is_file(): result = json.loads(result_path.read_text(encoding="utf-8")); break
        time.sleep(1)
    assert result is not None, "agy-gen real task did not produce a durable result"
    assert result["state"] == "SUCCEEDED", result
    serialized = json.dumps(result); assert marker in serialized, "real model result did not contain requested marker"
    assert result.get("output_sha256"), result


def verify_runtime_state() -> None:
    status = run_genos("agent", "status")
    assert status["identity"]["agent_id"] == "agy-gen", status
    assert status["identity"]["concurrency"] == 1, status
    assert status["identity"]["provider_target"]["provider"] == "antigravity", status
    assert status["provider"]["provider_cli"] == "antigravity", status
    assert status["provider"]["state"] == "ACTIVE", status
    assert status["provider"]["model"] == TARGET_MODEL, status
    assert status["provider"]["thinking_level"] == "HIGH", status
    assert status["provider"]["approval_mode"] == "yolo", status
    from genos.agent_runtime import AgentRuntimeStore
    store = AgentRuntimeStore(AGENT_ROOT)
    revisions = store.list_revisions("memory", "mvp04-e2e")
    assert revisions and revisions[-1]["content"] == "persistent-evidence"
    assert store.identity()["agent_id"] == "agy-gen"


def main() -> int:
    if os.geteuid() == 0: raise SystemExit("run acceptance helper as unprivileged genos identity")
    mode = sys.argv[1] if len(sys.argv) > 1 else "bootstrap"
    if mode == "bootstrap": bootstrap_with_stdin_secret(); return 0
    if mode == "task-before-reboot": verify_runtime_state(); execute_real_task("BEFORE_REBOOT"); return 0
    if mode == "after-reboot": verify_runtime_state(); execute_real_task("AFTER_REBOOT"); return 0
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__": raise SystemExit(main())
