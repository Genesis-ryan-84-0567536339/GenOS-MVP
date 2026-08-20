from __future__ import annotations

import os
from pathlib import Path

from .agent_tools import AgentToolError, GEMINI_CLI_VERSION, NODE_VERSION


LINKS: tuple[tuple[Path, Path], ...] = (
    (Path("/usr/local/bin/node"), Path(f"/var/lib/genos/tools/node-v{NODE_VERSION}/bin/node")),
    (Path("/usr/local/bin/npm"), Path(f"/var/lib/genos/tools/node-v{NODE_VERSION}/bin/npm")),
    (Path("/usr/local/bin/npx"), Path(f"/var/lib/genos/tools/node-v{NODE_VERSION}/bin/npx")),
    (Path("/usr/local/bin/gemini"), Path(f"/var/lib/genos/tools/gemini-cli-v{GEMINI_CLI_VERSION}/bin/gemini")),
)


def ensure_system_links() -> list[dict[str, str]]:
    """Expose only GenOS-pinned tools, refusing to overwrite host binaries."""
    if os.geteuid() != 0:
        raise AgentToolError("tool link activation requires root")
    evidence: list[dict[str, str]] = []
    for link, target in LINKS:
        if not target.exists():
            raise AgentToolError(f"verified tool target is missing: {target}")
        if link.is_symlink():
            current = link.resolve()
            if current != target.resolve():
                raise AgentToolError(f"host tool conflict at {link}")
            evidence.append({"path": str(link), "target": str(target), "state": "EXISTING"})
            continue
        if link.exists():
            raise AgentToolError(f"host tool conflict at {link}; GenOS will not overwrite it")
        link.symlink_to(target)
        evidence.append({"path": str(link), "target": str(target), "state": "CREATED"})
    return evidence
