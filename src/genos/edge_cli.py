from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

from .auth_service import CredentialService
from .edge import EdgeBindingStore, EdgeError, EdgeService
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider


KEY_URL = "https://pkg.cloudflare.com/cloudflare-main.gpg"
APT_REPO = "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main\n"
EDGE_UNIT = """[Unit]
Description=GenOS optional Cloudflare Edge runtime
After=network.target postgresql.service genos-mission-control.service
Requires=postgresql.service

[Service]
Type=simple
User=genos
Group=genos
WorkingDirectory=/var/lib/genos
EnvironmentFile=/etc/genos/genos.env
Environment=PYTHONPATH=/opt/genos/current/src
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m genos.edge_runtime
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/genos /var/log/genos /run/genos
ProtectHome=true

[Install]
WantedBy=multi-user.target
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genos-edge", description="GenOS guided Local/Cloudflare edge lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "verify", "disable", "rollback"):
        item = sub.add_parser(name)
        item.add_argument("--json", action="store_true", dest="as_json")
    configure = sub.add_parser("configure")
    configure.add_argument("--api-secret-id", required=True)
    configure.add_argument("--account-id", required=True)
    configure.add_argument("--zone-id", required=True)
    configure.add_argument("--hostname", required=True)
    configure.add_argument("--tunnel-name", default="genos")
    configure.add_argument("--json", action="store_true", dest="as_json")
    provision = sub.add_parser("provision-cloudflared")
    provision.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _service() -> EdgeService:
    state_root = Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
    secret_root = Path(os.environ.get("GENOS_SECRET_DIR", str(state_root / "secrets")))
    store = PostgresProductStore()
    store.ensure_schema()
    credentials = CredentialService(store, LocalFileSecretProvider(secret_root))
    return EdgeService(store=EdgeBindingStore(state_root), credentials=credentials)


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _run_checked(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise EdgeError(f"typed system action failed: {argv[0]} {argv[1] if len(argv) > 1 else ''}")


def _provision() -> dict[str, object]:
    if os.geteuid() != 0:
        raise EdgeError("provision-cloudflared requires root")
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or "VERSION_ID=\"24.04\"" not in os_release:
        raise EdgeError("cloudflared provisioning is certified only for Ubuntu 24.04 in this MVP")
    keyring = Path("/usr/share/keyrings/cloudflare-main.gpg")
    keyring.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with urllib.request.urlopen(KEY_URL, timeout=20) as response:  # noqa: S310 - fixed trusted provider URL
        key = response.read(256 * 1024 + 1)
    if not key or len(key) > 256 * 1024:
        raise EdgeError("Cloudflare signing key response is invalid")
    keyring.write_bytes(key)
    os.chmod(keyring, 0o644)
    source = Path("/etc/apt/sources.list.d/cloudflared.list")
    source.write_text(APT_REPO, encoding="utf-8")
    os.chmod(source, 0o644)
    _run_checked(["/usr/bin/apt-get", "update"])
    _run_checked(["/usr/bin/apt-get", "install", "-y", "cloudflared"])
    binary = shutil.which("cloudflared")
    if not binary:
        raise EdgeError("cloudflared package installed without executable")
    version = subprocess.run(
        [binary, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        shell=False,
        check=False,
    )
    if version.returncode != 0:
        raise EdgeError("cloudflared version probe failed")
    unit_path = Path("/etc/systemd/system/genos-edge.service")
    unit_path.write_text(EDGE_UNIT, encoding="utf-8")
    os.chmod(unit_path, 0o644)
    _run_checked(["/usr/bin/systemctl", "daemon-reload"])
    _run_checked(["/usr/bin/systemctl", "enable", "--now", "genos-edge.service"])
    return {
        "state": "READY",
        "cloudflared": version.stdout.strip()[:200],
        "service": "genos-edge.service",
        "token_in_argv": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "provision-cloudflared":
            result = _provision()
        else:
            service = _service()
            if args.command == "status":
                result = service.status()
            elif args.command == "configure":
                result = service.configure(
                    api_secret_id=args.api_secret_id,
                    account_id=args.account_id,
                    zone_id=args.zone_id,
                    hostname=args.hostname,
                    tunnel_name=args.tunnel_name,
                )
            elif args.command == "verify":
                result = service.verify()
            elif args.command == "disable":
                result = service.disable()
            elif args.command == "rollback":
                result = service.rollback()
            else:
                raise EdgeError("unsupported edge command")
        _emit(result, as_json=args.as_json)
        return 0
    except EdgeError as exc:
        _emit(
            {"state": "NEEDS_ACTION", "error_type": type(exc).__name__, "message": str(exc)},
            as_json=getattr(args, "as_json", False),
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
