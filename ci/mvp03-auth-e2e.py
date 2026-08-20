from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:17880"
USERNAME = "mvp-owner"
PASSWORD = "MVP03-owner-password-12345"
RAW_ONE = "MVP03-fixture-secret-alpha-123"
RAW_TWO = "MVP03-fixture-secret-beta-456"
CREDENTIAL_NAME = "mvp03-fixture-google-drive"


def request(method: str, path: str, *, payload: dict[str, object] | None = None, token: str | None = None) -> tuple[int, dict[str, object]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw)


def assert_no_raw_values(value: object, extra: list[str] | None = None) -> None:
    serialized = json.dumps(value, sort_keys=True)
    for secret in [PASSWORD, RAW_ONE, RAW_TWO, *(extra or [])]:
        assert secret not in serialized, "public response leaked raw authentication/credential material"


def login() -> str:
    status, payload = request("POST", "/api/v1/auth/login", payload={"username": USERNAME, "password": PASSWORD})
    assert status == 200, (status, payload)
    token = str(payload.get("session_token") or "")
    assert len(token) >= 32, "session token missing"
    assert_no_raw_values({"owner": payload.get("owner"), "expires_at": payload.get("expires_at")}, [token])
    return token


def pg_dump_text() -> str:
    completed = subprocess.run(
        ["pg_dump", "genos"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    return completed.stdout


def verify_secret_file_modes(secret_id: str) -> None:
    root = Path("/var/lib/genos/secrets")
    secret_dir = root / secret_id
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    for revision in (1, 2):
        path = secret_dir / f"{revision}.secret"
        assert path.is_file(), path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def initial() -> None:
    status, payload = request(
        "POST",
        "/api/v1/owner/bootstrap",
        payload={"username": USERNAME, "password": PASSWORD},
    )
    assert status == 201, (status, payload)
    assert_no_raw_values(payload)

    status, payload = request(
        "POST",
        "/api/v1/owner/bootstrap",
        payload={"username": "second-owner", "password": "MVP03-second-owner-password-123"},
    )
    assert status == 409 and payload.get("error") == "owner_exists", (status, payload)

    status, payload = request(
        "POST",
        "/api/v1/auth/login",
        payload={"username": USERNAME, "password": "MVP03-wrong-password-12345"},
    )
    assert status == 401 and payload.get("error") == "invalid_credentials", (status, payload)

    token = login()
    status, payload = request("GET", "/api/v1/auth/me", token=token)
    assert status == 200 and payload["owner"]["username"] == USERNAME, (status, payload)
    assert_no_raw_values(payload, [token])

    status, payload = request(
        "POST",
        "/api/v1/credentials",
        token=token,
        payload={
            "name": CREDENTIAL_NAME,
            "provider": "google",
            "secret": RAW_ONE,
            "consumer_scopes": ["agy-gen", "drive-sync"],
        },
    )
    assert status == 201, (status, payload)
    assert_no_raw_values(payload, [token])
    credential = payload["credential"]
    secret_id = str(credential["secret_id"])
    assert credential["active_revision"] == 1
    assert credential["consumer_scopes"] == ["agy-gen", "drive-sync"]

    status, payload = request("GET", "/api/v1/credentials", token=token)
    assert status == 200, (status, payload)
    assert_no_raw_values(payload, [token])
    assert len(payload["credentials"]) == 1

    status, payload = request("GET", f"/api/v1/credentials/{secret_id}", token=token)
    assert status == 404, "there must be no public raw-secret GET surface"

    status, payload = request("POST", f"/api/v1/credentials/{secret_id}/test", token=token)
    assert status == 200 and payload["test"]["state"] == "PASS", (status, payload)
    assert_no_raw_values(payload, [token])

    status, payload = request(
        "POST",
        f"/api/v1/credentials/{secret_id}/rotate",
        token=token,
        payload={"secret": RAW_TWO},
    )
    assert status == 200 and payload["credential"]["active_revision"] == 2, (status, payload)
    assert_no_raw_values(payload, [token])

    verify_secret_file_modes(secret_id)

    dump = pg_dump_text()
    for raw in (PASSWORD, RAW_ONE, RAW_TWO, token):
        assert raw not in dump, "Product DB dump contains raw authentication/credential material"

    status, payload = request("POST", f"/api/v1/credentials/{secret_id}/disable", token=token)
    assert status == 200 and payload["credential"]["status"] == "DISABLED", (status, payload)
    status, payload = request("POST", f"/api/v1/credentials/{secret_id}/test", token=token)
    assert status == 200 and payload["test"]["state"] == "FAIL", (status, payload)

    status, payload = request("POST", "/api/v1/auth/logout", token=token)
    assert status == 200 and payload["state"] == "REVOKED", (status, payload)
    status, _payload = request("GET", "/api/v1/auth/me", token=token)
    assert status == 401

    print("GENOS_MVP03_AUTH_SECRETREF_INITIAL_PASS")


def verify_after_reboot() -> None:
    token = login()
    status, payload = request("GET", "/api/v1/credentials", token=token)
    assert status == 200, (status, payload)
    assert len(payload["credentials"]) == 1, payload
    credential = payload["credentials"][0]
    assert credential["name"] == CREDENTIAL_NAME
    assert credential["active_revision"] == 2
    assert credential["status"] == "DISABLED"
    assert credential["consumer_scopes"] == ["agy-gen", "drive-sync"]
    secret_id = str(credential["secret_id"])
    verify_secret_file_modes(secret_id)
    dump = pg_dump_text()
    for raw in (PASSWORD, RAW_ONE, RAW_TWO, token):
        assert raw not in dump, "Product DB dump contains raw material after reboot"
    status, _payload = request("GET", f"/api/v1/credentials/{secret_id}", token=token)
    assert status == 404
    print("GENOS_MVP03_AUTH_SECRETREF_REBOOT_PASS")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "initial"
    if os.geteuid() == 0:
        raise SystemExit("run this acceptance helper as the unprivileged genos service identity")
    if mode == "initial":
        initial()
        return 0
    if mode == "verify":
        verify_after_reboot()
        return 0
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
