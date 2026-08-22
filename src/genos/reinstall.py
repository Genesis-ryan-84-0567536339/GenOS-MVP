from __future__ import annotations

from pathlib import Path
from typing import Any

from .lifecycle_hardened import restore_preserved_install_identity


def prepare_reinstall_from_preserved_state(
    *,
    state_root: Path = Path("/var/lib/genos"),
    config_root: Path = Path("/etc/genos"),
) -> dict[str, Any]:
    """Prepare an intentional reinstall after `genos uninstall`.

    Durable Product state/DB/secrets remain intact. Stable instance/MCP identity
    is restored to /etc and only the old install *execution checkpoint* is reset,
    because those completed steps described services/releases that uninstall has
    deliberately removed.
    """

    result = restore_preserved_install_identity(state_root=state_root, config_root=config_root)
    reset = False
    if result.get("state") == "RESTORED":
        checkpoint = state_root / "install-run.json"
        if checkpoint.exists():
            checkpoint.unlink()
            reset = True
    return {**result, "install_checkpoint_reset": reset}
