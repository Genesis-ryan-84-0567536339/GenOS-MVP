from __future__ import annotations

import argparse
import json
from typing import Any

from . import __version__
from .recon import collect_all
from .state import JsonStateStore


_MUTATION_COMMANDS = {
    "install",
    "repair",
    "update",
    "reconfigure",
    "backup",
    "restore",
    "support-bundle",
    "uninstall",
    "purge",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genos", description="GenOS MVP lifecycle CLI")
    parser.add_argument("--version", action="version", version=f"genos {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show local GenOS lifecycle state without mutation")
    status.add_argument("--json", action="store_true", dest="as_json")

    for name in ("recon", "doctor"):
        command = sub.add_parser(name, help="Run typed read-only host observations")
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("--cwd", default=None, help="Optional repository path for read-only Git facts")

    for name in sorted(_MUTATION_COMMANDS):
        sub.add_parser(name, help="Lifecycle surface reserved for later MVP packages")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return _status(as_json=args.as_json)
    if args.command in {"recon", "doctor"}:
        return _recon(mode=args.command, as_json=args.as_json, cwd=args.cwd)
    return _not_implemented(args.command)


def _status(as_json: bool) -> int:
    store = JsonStateStore()
    manifest = store.load_manifest()
    payload: dict[str, Any]
    if manifest is None:
        payload = {"state": "NOT_INSTALLED", "state_dir": str(store.root), "evidence": "manifest_absent"}
    else:
        payload = {"state": manifest.get("state", "UNKNOWN"), "manifest": manifest, "state_dir": str(store.root)}
    _emit(payload, as_json=as_json)
    return 0


def _recon(mode: str, as_json: bool, cwd: str | None) -> int:
    observations, support_class, reason = collect_all(cwd=cwd)
    payload = {
        "mode": mode,
        "read_only": True,
        "support_class": support_class.value,
        "support_reason": reason,
        "observations": [item.to_dict() for item in observations],
    }
    _emit(payload, as_json=as_json)
    return 0


def _not_implemented(command: str) -> int:
    payload = {
        "command": command,
        "state": "NOT_IMPLEMENTED_IN_MVP_01",
        "read_only": True,
        "message": "The lifecycle command surface is reserved; host mutation begins in later approved MVP packages.",
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if "observations" in payload:
        print(f"support: {payload['support_class']}")
        print(f"reason: {payload['support_reason']}")
        for item in payload["observations"]:
            print(f"- {item['check_id']}: {item['state']}")
        return
    print(f"state: {payload.get('state', 'UNKNOWN')}")


if __name__ == "__main__":
    raise SystemExit(main())
