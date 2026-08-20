from __future__ import annotations

import os
from pathlib import Path
import pwd


AGENT_AUTHORITY_ROOT = Path("/var/lib/genos/agents").resolve()


def ensure_agent_runtime_ownership(root: Path | str) -> None:
    """Return privileged CLI-created runtime files to the unprivileged agent.

    No-op for tests/custom state roots. Production agent authority must remain
    owned by `genos` so the worker/tmux process can resume after an Owner action.
    """
    if os.geteuid() != 0:
        return
    path = Path(root).resolve()
    if path != AGENT_AUTHORITY_ROOT and AGENT_AUTHORITY_ROOT not in path.parents:
        return
    account = pwd.getpwnam("genos")
    if not path.exists():
        return
    for current_root, directories, files in os.walk(path):
        current = Path(current_root)
        os.chown(current, account.pw_uid, account.pw_gid)
        for name in directories:
            os.chown(current / name, account.pw_uid, account.pw_gid)
        for name in files:
            os.chown(current / name, account.pw_uid, account.pw_gid)
