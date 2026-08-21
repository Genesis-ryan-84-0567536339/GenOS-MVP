from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import cli as legacy_cli
from .install import ReleaseArtifact
from .lifecycle import LifecycleError, LifecycleNeedsAction
from .lifecycle_hardened import HardenedLifecycleService, restore_preserved_install_identity
from .redaction import redact


_LIFECYCLE = {"update", "backup", "restore", "support-bundle", "uninstall", "purge"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return legacy_cli.main(args)

    command = args[0]
    if command == "install":
        # `uninstall` removes generated /etc config but preserves it beneath
        # durable state. Restore only instance-id/MCP port before a real reinstall
        # so the same machine does not silently become a new GenOS instance.
        if "--plan-only" not in args and os.geteuid() == 0:
            restore_preserved_install_identity()
        return legacy_cli.main(args)
    if command not in _LIFECYCLE:
        return legacy_cli.main(args)

    parser = _parser_for(command)
    parsed = parser.parse_args(args[1:])
    service = HardenedLifecycleService()
    try:
        if command == "backup":
            result = service.backup(
                output=Path(parsed.output) if parsed.output else None,
                include_secrets=bool(parsed.include_secrets),
            )
        elif command == "restore":
            result = service.restore(
                archive=Path(parsed.archive),
                expected_sha256=parsed.sha256,
                allow_instance_replace=bool(parsed.allow_instance_replace),
            )
        elif command == "update":
            result = service.update(
                release=ReleaseArtifact(
                    archive=Path(parsed.release),
                    sha256=parsed.release_sha256,
                    git_sha=parsed.git_sha,
                )
            )
        elif command == "support-bundle":
            result = service.support_bundle(output=Path(parsed.output) if parsed.output else None)
        elif command == "uninstall":
            result = service.uninstall()
        elif command == "purge":
            result = service.purge(confirm_instance_id=parsed.confirm_instance_id)
        else:  # pragma: no cover
            raise LifecycleError("unsupported lifecycle command")
        _emit(result, as_json=bool(parsed.as_json))
        return 0
    except LifecycleNeedsAction as exc:
        _emit(
            {"state": "NEEDS_ACTION", "command": command, "error_type": type(exc).__name__, "message": str(exc)},
            as_json=bool(parsed.as_json),
        )
        return 3
    except (LifecycleError, OSError, ValueError) as exc:
        _emit(
            {"state": "FAILED", "command": command, "error_type": type(exc).__name__, "message": str(exc)},
            as_json=bool(parsed.as_json),
        )
        return 4


def _parser_for(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"genos {command}")
    parser.add_argument("--json", action="store_true", dest="as_json")
    if command == "update":
        parser.add_argument("--release", required=True)
        parser.add_argument("--release-sha256", required=True)
        parser.add_argument("--git-sha", required=True)
    elif command == "backup":
        parser.add_argument("--output", default=None)
        parser.add_argument(
            "--include-secrets",
            action="store_true",
            help="Explicitly include SecretProvider material in a permission-restricted backup.",
        )
    elif command == "restore":
        parser.add_argument("--archive", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--allow-instance-replace", action="store_true")
    elif command == "support-bundle":
        parser.add_argument("--output", default=None)
    elif command == "purge":
        parser.add_argument("--confirm-instance-id", required=True)
    return parser


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    safe = redact(payload)
    if as_json:
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    state = safe.get("state", "UNKNOWN")
    print(f"GenOS lifecycle: {state}")
    print(json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
