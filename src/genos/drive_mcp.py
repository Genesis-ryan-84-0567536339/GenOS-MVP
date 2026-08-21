from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


AGENT_ID = "agy-gen"
MCP_SCOPE = "drive-collaboration-replica"


class DriveMcpGrantProbe(Protocol):
    def test(self, *, instance_id: str, root_folder_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OptionalDriveMcpGrantProbe:
    """Truthful default for the optional agy-gen Drive MCP grant.

    MVP-06 must execute the grant-test stage, but the grant itself is optional.
    Until a scoped MCP adapter is configured, the probe reports NOT_CONFIGURED
    and never implies that agy-gen can access Drive or any raw credential.
    """

    agent_id: str = AGENT_ID
    scope: str = MCP_SCOPE

    def test(self, *, instance_id: str, root_folder_id: str) -> dict[str, Any]:
        return {
            "state": "NOT_CONFIGURED",
            "configured": False,
            "agent_id": self.agent_id,
            "scope": self.scope,
            "instance_id": instance_id,
            "root_folder_id": root_folder_id,
            "credential_passthrough": False,
            "reason": "OPTIONAL_MCP_GRANT_NOT_CONFIGURED",
        }
