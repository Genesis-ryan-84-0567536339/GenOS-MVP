from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from genos.agent_auth import AgentAuthBridge
from genos.agent_runtime import AgentRuntimeStore


FAKE_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture"
    "&redirect_uri=https%3A%2F%2Fcodeassist.google.com%2Fauthcode&state=fixture-state"
)
FAKE_CODE = "fixture-user-code-123"


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "agy-gen"
        store = AgentRuntimeStore(root)
        store.ensure_seed(instance_id="fixture-instance")

        fake = Path(temp) / "gemini"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('Please visit the following URL to authorize the application:\\n\\n{FAKE_URL}', flush=True)\n"
            "print('Enter the authorization code:', flush=True)\n"
            "code = sys.stdin.readline().strip()\n"
            f"print('Authentication succeeded' if code == '{FAKE_CODE}' else 'Failed to authenticate with authorization code: denied', flush=True)\n"
            "sys.stdin.readline()\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

        bridge = AgentAuthBridge(store, gemini_binary=str(fake))
        started = bridge.start(restart=True)
        assert started["state"] == "WAITING_CODE", started
        assert started["auth_url"] == FAKE_URL, started
        submitted = bridge.submit_code(FAKE_CODE)
        assert submitted["state"] == "SUBMITTED", submitted
        time.sleep(0.2)
        status = bridge.status()
        assert status["state"] == "AUTHENTICATED", status

        # GenOS projections/state must not contain the one-way authorization code.
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert FAKE_CODE not in text, f"authorization code persisted at {path}"

        tmux_env = bridge._env()
        tmux = subprocess.run(
            ["tmux", "list-windows", "-t", "agy-gen", "-F", "#{window_name}"],
            capture_output=True,
            text=True,
            check=True,
            env=tmux_env,
        )
        assert "auth" in tmux.stdout.splitlines(), tmux.stdout
        print(json.dumps({"state": "PASS", "auth_url_projection": "PASS", "code_state_persistence_negative": "PASS"}))
        subprocess.run(["tmux", "kill-session", "-t", "agy-gen"], check=False, env=tmux_env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
