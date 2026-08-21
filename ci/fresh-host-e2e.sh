#!/usr/bin/env bash
set -euo pipefail

IMAGE_URL="https://cloud-images.ubuntu.com/releases/noble/release-20260801/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
WORK_DIR="${RUNNER_TEMP:-/tmp}/genos-fresh-host"
SSH_PORT="${GENOS_E2E_SSH_PORT:-2222}"
SSH_USER="genos-ci"
KEY="$WORK_DIR/id_ed25519"
BASE_IMAGE="$WORK_DIR/ubuntu-24.04.img"
VM_IMAGE="$WORK_DIR/genos-vm.qcow2"
SEED_IMAGE="$WORK_DIR/seed.img"
SERIAL_LOG="$WORK_DIR/qemu-serial.log"
PID_FILE="$WORK_DIR/qemu.pid"
RELEASE="$WORK_DIR/genos-release.tar.gz"
EVIDENCE="$WORK_DIR/fresh-host-evidence.json"
TESTED_SHA="$(git rev-parse HEAD)"
MCP_FIXTURE_PORT="${GENOS_E2E_MCP_FIXTURE_PORT:-18777}"
MCP_TOKEN=""
MCP_PRINCIPAL_ID=""
MCP_UPSTREAM_ID=""
MCP_SECRET_ID=""
UPSTREAM_SECRET=""
OWNER_SESSION_TOKEN=""

normalize_port() {
  local port_value="$1" normalized decimal
  if [[ ! "$port_value" =~ ^0*([0-9]{1,5})$ ]]; then
    echo "fresh-host port overrides must be decimal values in 1024..65535" >&2
    return 2
  fi
  normalized="${BASH_REMATCH[1]}"
  decimal=$((10#$normalized))
  if (( decimal < 1024 || decimal > 65535 )); then
    echo "fresh-host port overrides must be decimal values in 1024..65535" >&2
    return 2
  fi
  printf '%d' "$decimal"
}

SSH_PORT="$(normalize_port "$SSH_PORT")"
MCP_FIXTURE_PORT="$(normalize_port "$MCP_FIXTURE_PORT")"
if [[ "$SSH_PORT" == "$MCP_FIXTURE_PORT" ]]; then
  echo "SSH and MCP fixture ports must be distinct" >&2
  exit 2
fi
if (( MCP_FIXTURE_PORT >= 17880 && MCP_FIXTURE_PORT <= 17932 )); then
  echo "MCP fixture port collides with a GenOS managed service range" >&2
  exit 2
fi
MCP_FIXTURE_ENDPOINT="http://127.0.0.1:${MCP_FIXTURE_PORT}/mcp"

mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"

cleanup() {
  set +e
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

ssh_guest() {
  ssh \
    -i "$KEY" \
    -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 \
    "$SSH_USER@127.0.0.1" "$@"
}

scp_guest() {
  scp \
    -i "$KEY" \
    -P "$SSH_PORT" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$@"
}

install_mcp_fixture_helpers() {
  ssh_guest "sudo -u genos tee /var/lib/genos/mcp-upstream-fixture.py >/dev/null" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os

PROTOCOL = "2026-07-28"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"
SECRET_PATH = Path("/run/genos-mcp-e2e/upstream-secret")
PORT = int(os.environ.get("MCP_FIXTURE_PORT", "18777"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "role": "mcp-upstream-fixture"})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        request_id = None
        try:
            if self.path != "/mcp":
                self._send(404, {"error": "not_found"})
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise ValueError("content_type")
            accept = self.headers.get("Accept", "").lower()
            if "application/json" not in accept or "text/event-stream" not in accept:
                self._send(406, {"error": "accept_required"})
                return
            expected = SECRET_PATH.read_text(encoding="utf-8").strip()
            if not expected or self.headers.get("Authorization") != f"Bearer {expected}":
                self._send(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 * 1024:
                raise ValueError("content_length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}
            meta = params.get("_meta") if isinstance(params, dict) else None
            if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
                raise ValueError("envelope")
            if self.headers.get("MCP-Protocol-Version") != PROTOCOL:
                raise ValueError("protocol_header")
            if self.headers.get("Mcp-Method") != method:
                raise ValueError("method_header")
            if not isinstance(meta, dict) or meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL:
                raise ValueError("protocol_meta")
            if not isinstance(meta.get("io.modelcontextprotocol/clientInfo"), dict):
                raise ValueError("client_info")
            if not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
                raise ValueError("client_capabilities")

            if method == "tools/list":
                result = {
                    "resultType": "complete",
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Fresh-host federation fixture.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "additionalProperties": False,
                            },
                        }
                    ],
                    "ttlMs": 0,
                    "cacheScope": "private",
                }
            elif method == "tools/call":
                name = params.get("name")
                if name != "echo" or self.headers.get("Mcp-Name") != name:
                    raise ValueError("name_header")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise ValueError("arguments")
                message = arguments.get("message", "")
                if not isinstance(message, str):
                    raise ValueError("message")
                result = {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": f"fixture:{message}"}],
                    "structuredContent": {"echo": message, "source": "fixture"},
                    "isError": False,
                }
            else:
                self._send(
                    404,
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}},
                )
                return
            result["_meta"] = {SERVER_INFO: {"name": "fresh-host-fixture", "version": "0.1"}}
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._send(
                400,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "invalid_request"}},
            )

    def _send(self, status, payload):
        body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt, *_args):
        # Headers, tokens and tool arguments are deliberately never logged.
        return


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
server.daemon_threads = True
server.serve_forever()
PY

  ssh_guest "sudo -u genos tee /var/lib/genos/mcp-e2e-client.py >/dev/null" <<'PY'
from pathlib import Path
import json
import sys
import urllib.error
import urllib.request

PROTOCOL = "2026-07-28"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"
token = sys.stdin.readline().strip()
phase = sys.argv[1]
if not token.startswith("gmcp_"):
    raise AssertionError("missing one-time MCP token")
port = int(Path("/etc/genos/mcp-port").read_text(encoding="utf-8").strip())
endpoint = f"http://127.0.0.1:{port}/mcp"
request_number = 0


def rpc(method, *, name=None, arguments=None):
    global request_number
    request_number += 1
    params = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "fresh-host-external-agent", "version": "0.1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    if name is not None:
        params["name"] = name
        params["arguments"] = arguments or {}
    payload = {"jsonrpc": "2.0", "id": request_number, "method": method, "params": params}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.load(response)
            status = response.status
            assert response.headers.get_content_type() == "application/json"
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = json.loads(exc.read().decode("utf-8"))
        exc.close()
    serialized = json.dumps(body, sort_keys=True)
    assert token not in serialized
    assert "upstream_fixture_" not in serialized
    assert "Authorization" not in serialized
    return status, body


def assert_local_call():
    status, body = rpc("tools/call", name="genos.observability.get", arguments={})
    assert status == 200, (status, body)
    assert body["result"]["isError"] is False, body


if phase == "live":
    status, body = rpc("server/discover")
    assert status == 200, (status, body)
    result = body["result"]
    assert PROTOCOL in result["supportedVersions"], result
    assert isinstance(result["_meta"][SERVER_INFO], dict), result

    status, body = rpc("tools/list")
    assert status == 200, (status, body)
    names = {item["name"] for item in body["result"]["tools"]}
    assert "genos.observability.get" in names, names
    assert "fixture.echo" in names, names
    assert "genos.cards.create" not in names, names

    assert_local_call()
    status, body = rpc("tools/call", name="fixture.echo", arguments={"message": "fresh-host-federation"})
    assert status == 200, (status, body)
    assert body["result"]["structuredContent"] == {
        "echo": "fresh-host-federation",
        "source": "fixture",
    }, body

    status, body = rpc("tools/call", name="genos.cards.create", arguments={"title": "must-not-run"})
    assert status == 403, (status, body)
    assert body["error"]["message"] == "forbidden", body
elif phase == "outage":
    status, body = rpc("tools/list")
    assert status == 200, (status, body)
    names = {item["name"] for item in body["result"]["tools"]}
    assert "genos.observability.get" in names, names
    assert "fixture.echo" not in names, names
    assert_local_call()
    status, body = rpc("tools/call", name="fixture.echo", arguments={"message": "offline"})
    assert status == 503, (status, body)
    assert body["error"]["message"] == "upstream_degraded", body
elif phase == "revoked":
    status, body = rpc("server/discover")
    assert status == 401, (status, body)
    assert body["error"]["message"] == "unauthorized", body
else:
    raise AssertionError(f"unknown phase: {phase}")

print(f"MCP_FRESH_HOST_{phase.upper()}_PASS")
PY

  ssh_guest "sudo -u genos tee /var/lib/genos/mcp-e2e-setup.py >/dev/null" <<'PY'
import json
import sys
import urllib.error
import urllib.request

password = sys.stdin.readline().rstrip("\n")
upstream_secret = sys.stdin.readline().rstrip("\n")
fixture_endpoint = sys.argv[1]
if len(password) < 12 or not upstream_secret.startswith("upstream_fixture_"):
    raise AssertionError("invalid setup material")


def post(path, body, *, token=None, expected=200):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        "http://127.0.0.1:17880" + path,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
            status = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise AssertionError(f"setup HTTP {code}") from None
    assert status == expected, (status, expected)
    return payload


post("/api/v1/owner/bootstrap", {"username": "fresh-host-owner", "password": password}, expected=201)
login = post("/api/v1/auth/login", {"username": "fresh-host-owner", "password": password})
session_token = login["session_token"]
credential = post(
    "/api/v1/credentials",
    {
        "name": "fresh-host-mcp-upstream",
        "provider": "mcp-fixture",
        "secret": upstream_secret,
        "consumer_scopes": ["mcp-hub"],
    },
    token=session_token,
    expected=201,
)["credential"]
issued = post(
    "/api/v1/mcp/principals",
    {
        "name": "fresh-host-external-agent",
        "scopes": ["genos.observability.get", "fixture.echo"],
    },
    token=session_token,
    expected=201,
)["mcp"]
upstream = post(
    "/api/v1/mcp/upstreams",
    {
        "namespace": "fixture",
        "name": "fresh-host-fixture",
        "endpoint": fixture_endpoint,
        "secret_id": credential["secret_id"],
    },
    token=session_token,
    expected=201,
)["upstream"]
print(
    json.dumps(
        {
            "secret_id": credential["secret_id"],
            "fingerprint": credential["fingerprint"],
            "owner_session_token": session_token,
            "mcp_access_token": issued["access_token"],
            "principal_id": issued["principal"]["principal_id"],
            "upstream_id": upstream["upstream_id"],
        },
        sort_keys=True,
    )
)
PY

  ssh_guest "sudo -u genos tee /var/lib/genos/mcp-e2e-owner-api.py >/dev/null" <<'PY'
import json
import sys
import urllib.error
import urllib.request
import uuid

token = sys.stdin.readline().rstrip("\n")
action = sys.argv[1]
if not token:
    raise AssertionError("missing Owner session")


def request(method, path):
    req = urllib.request.Request(
        "http://127.0.0.1:17880" + path,
        data=None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            assert response.status == 200, response.status
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise AssertionError(f"Owner API HTTP {code}") from None
    assert token not in json.dumps(payload, sort_keys=True)
    return payload


if action == "registry":
    print(
        json.dumps(
            {
                "principals": request("GET", "/api/v1/mcp/principals")["principals"],
                "upstreams": request("GET", "/api/v1/mcp/upstreams")["upstreams"],
            },
            sort_keys=True,
        )
    )
elif action == "audit":
    print(json.dumps(request("GET", "/api/v1/mcp/audit"), sort_keys=True))
elif action == "cards":
    print(json.dumps(request("GET", "/api/v1/cards"), sort_keys=True))
elif action == "revoke":
    principal_id = str(uuid.UUID(sys.argv[2]))
    print(json.dumps(request("POST", f"/api/v1/mcp/principals/{principal_id}/revoke"), sort_keys=True))
else:
    raise AssertionError(f"unknown Owner API action: {action}")
PY

  ssh_guest "sudo -u genos tee /var/lib/genos/mcp-e2e-hygiene.py >/dev/null" <<'PY'
from pathlib import Path
import os
import pwd
import grp
import stat
import subprocess
import sys

secret_id = sys.argv[1]
mcp_token = sys.stdin.readline().rstrip("\n")
upstream_secret = sys.stdin.readline().rstrip("\n")
owner_session = sys.stdin.readline().rstrip("\n")
owner_password = sys.stdin.readline().rstrip("\n")
assert mcp_token.startswith("gmcp_")
assert upstream_secret.startswith("upstream_fixture_")
assert len(owner_session) >= 32
assert len(owner_password) >= 12

secret_root = Path("/var/lib/genos/secrets")
secret_dir = secret_root / secret_id
secret_file = secret_dir / "1.secret"
assert secret_file.read_text(encoding="utf-8") == upstream_secret
for path, mode in ((secret_root, 0o700), (secret_dir, 0o700), (secret_file, 0o600)):
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == mode, (path, oct(stat.S_IMODE(info.st_mode)))
    assert pwd.getpwuid(info.st_uid).pw_name == "genos", path
    assert grp.getgrgid(info.st_gid).gr_name == "genos", path

dump = subprocess.run(
    ["runuser", "-u", "genos", "--", "pg_dump", "--data-only", "--inserts", "genos"],
    check=True,
    capture_output=True,
    text=True,
    timeout=30,
).stdout
journal = subprocess.run(
    ["journalctl", "-u", "genos-mcp.service", "-u", "genos-product-api.service", "--no-pager"],
    check=False,
    capture_output=True,
    text=True,
    timeout=30,
).stdout
fixture_log = Path("/var/lib/genos/mcp-upstream-fixture.log").read_text(encoding="utf-8") if Path("/var/lib/genos/mcp-upstream-fixture.log").is_file() else ""
for material in (mcp_token, upstream_secret, owner_session, owner_password):
    assert material not in dump
    assert material not in journal
    assert material not in fixture_log
print("MCP_SECRET_HYGIENE_PASS")
PY

  ssh_guest "sudo chmod 700 /var/lib/genos/mcp-upstream-fixture.py /var/lib/genos/mcp-e2e-client.py /var/lib/genos/mcp-e2e-setup.py /var/lib/genos/mcp-e2e-owner-api.py /var/lib/genos/mcp-e2e-hygiene.py"
  ssh_guest "sudo chown genos:genos /var/lib/genos/mcp-upstream-fixture.py /var/lib/genos/mcp-e2e-client.py /var/lib/genos/mcp-e2e-setup.py /var/lib/genos/mcp-e2e-owner-api.py /var/lib/genos/mcp-e2e-hygiene.py"
}

start_upstream_fixture() {
  ssh_guest "sudo install -d -o genos -g genos -m 700 /run/genos-mcp-e2e"
  printf '%s\n' "$UPSTREAM_SECRET" | ssh_guest "sudo -u genos python3 -c 'from pathlib import Path; import os,sys; p=Path(\"/run/genos-mcp-e2e/upstream-secret\"); p.write_text(sys.stdin.readline().rstrip(\"\\n\"), encoding=\"utf-8\"); os.chmod(p, 0o600)'"
  ssh_guest "sudo -u genos env MCP_FIXTURE_PORT=$MCP_FIXTURE_PORT /bin/bash -s" <<'SH'
set -euo pipefail
pid_file=/var/lib/genos/mcp-upstream-fixture.pid
fixture=/var/lib/genos/mcp-upstream-fixture.py
if [[ -f "$pid_file" ]]; then
  read -r old_pid < "$pid_file" || true
  if [[ "${old_pid:-}" =~ ^[0-9]+$ && -r "/proc/$old_pid/cmdline" ]]; then
    old_command="$(tr '\0' ' ' < "/proc/$old_pid/cmdline")"
    if [[ "$old_command" == *"$fixture"* ]]; then
      exit 0
    fi
  fi
  rm -f "$pid_file"
fi
nohup /usr/bin/python3 "$fixture" >/var/lib/genos/mcp-upstream-fixture.log 2>&1 &
printf '%s\n' "$!" > "$pid_file"
SH
  fixture_ready=0
  for _ in $(seq 1 40); do
    if ssh_guest "python3 -c 'import json,urllib.request; p=json.load(urllib.request.urlopen(\"http://127.0.0.1:$MCP_FIXTURE_PORT/health\", timeout=2)); assert p[\"status\"] == \"ok\"'" >/dev/null 2>&1; then
      fixture_ready=1
      break
    fi
    sleep 0.5
  done
  if [[ "$fixture_ready" != 1 ]]; then
    ssh_guest "sudo cat /var/lib/genos/mcp-upstream-fixture.log" || true
    echo "MCP upstream fixture did not become ready" >&2
    return 1
  fi
}

stop_upstream_fixture() {
  ssh_guest "sudo /bin/bash -s" <<'SH'
set -euo pipefail
pid_file=/var/lib/genos/mcp-upstream-fixture.pid
fixture=/var/lib/genos/mcp-upstream-fixture.py
if [[ -f "$pid_file" ]]; then
  read -r pid < "$pid_file" || true
  if [[ "${pid:-}" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]]; then
    command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    if [[ "$command" != *"$fixture"* ]]; then
      echo "refusing to stop an unrelated process from stale fixture PID" >&2
      exit 1
    fi
    kill "$pid"
    for _ in $(seq 1 40); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "fixture process did not stop" >&2
      exit 1
    fi
  fi
  rm -f "$pid_file"
fi
rm -f /run/genos-mcp-e2e/upstream-secret
SH
}

run_mcp_client() {
  local phase="$1"
  printf '%s\n' "$MCP_TOKEN" | ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-client.py $phase"
}

assert_mcp_registry_state() {
  local expected_status="${1:-ACTIVE}"
  local registry principals upstreams
  registry="$(printf '%s\n' "$OWNER_SESSION_TOKEN" | ssh_guest 'sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-owner-api.py registry')"
  printf '%s' "$registry" | python3 -c 'import json,sys; p=json.load(sys.stdin); principals=[x for x in p["principals"] if x["principal_id"] == sys.argv[1]]; upstreams=[x for x in p["upstreams"] if x["upstream_id"] == sys.argv[3]]; assert len(principals) == 1 and principals[0]["status"] == sys.argv[2], principals; assert len(upstreams) == 1, upstreams; row=upstreams[0]; assert row["namespace"] == "fixture" and row["endpoint"] == sys.argv[4] and row["secret_id"] == sys.argv[5] and row["status"] == "ACTIVE", row; assert "access_token" not in json.dumps(p)' "$MCP_PRINCIPAL_ID" "$expected_status" "$MCP_UPSTREAM_ID" "$MCP_FIXTURE_ENDPOINT" "$MCP_SECRET_ID"

  # CLI is retained as an additional projection check; lifecycle mutation above
  # and revocation below go through the deployed Owner-authenticated Product API.
  principals="$(ssh_guest 'sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 -m genos mcp principal-list --json')"
  upstreams="$(ssh_guest 'sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 -m genos mcp upstream-list --json')"
  printf '%s' "$principals" | python3 -c 'import json,sys; p=json.load(sys.stdin); rows=[x for x in p["principals"] if x["principal_id"] == sys.argv[1]]; assert len(rows) == 1 and rows[0]["status"] == sys.argv[2], rows; assert "access_token" not in json.dumps(p)' "$MCP_PRINCIPAL_ID" "$expected_status"
  printf '%s' "$upstreams" | python3 -c 'import json,sys; p=json.load(sys.stdin); rows=[x for x in p["upstreams"] if x["upstream_id"] == sys.argv[1]]; assert len(rows) == 1, rows; row=rows[0]; assert row["namespace"] == "fixture" and row["endpoint"] == sys.argv[2] and row["secret_id"] == sys.argv[3] and row["status"] == "ACTIVE", row' "$MCP_UPSTREAM_ID" "$MCP_FIXTURE_ENDPOINT" "$MCP_SECRET_ID"
}

assert_mcp_audit() {
  local require_degraded="${1:-0}"
  local audit cards
  audit="$(printf '%s\n' "$OWNER_SESSION_TOKEN" | ssh_guest 'sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-owner-api.py audit')"
  printf '%s' "$audit" | python3 -c 'import json,sys; rows=json.load(sys.stdin)["audit"]; principal=sys.argv[1]; upstream=sys.argv[2]; degraded=sys.argv[3] == "1"; match=lambda tool,decision,result: any(x.get("principal_id") == principal and x.get("tool_name") == tool and x.get("decision") == decision and x.get("result_class") == result for x in rows); assert match("genos.observability.get","ALLOW","PASS"), rows; assert match("fixture.echo","ALLOW","PASS"), rows; assert match("genos.cards.create","DENY","FORBIDDEN"), rows; assert not degraded or any(x.get("principal_id") == principal and x.get("upstream_id") == upstream and x.get("tool_name") == "fixture.echo" and x.get("result_class") == "UPSTREAM_DEGRADED" for x in rows), rows' "$MCP_PRINCIPAL_ID" "$MCP_UPSTREAM_ID" "$require_degraded"
  cards="$(printf '%s\n' "$OWNER_SESSION_TOKEN" | ssh_guest 'sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-owner-api.py cards')"
  printf '%s' "$cards" | python3 -c 'import json,sys; cards=json.load(sys.stdin)["cards"]; assert all(card.get("title") != "must-not-run" for card in cards), cards'
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN EXISTS (SELECT 1 FROM mcp_audit_event WHERE principal_id='$MCP_PRINCIPAL_ID'::uuid AND tool_name='genos.observability.get' AND decision='ALLOW' AND result_class='PASS') AND EXISTS (SELECT 1 FROM mcp_audit_event WHERE principal_id='$MCP_PRINCIPAL_ID'::uuid AND tool_name='fixture.echo' AND decision='ALLOW' AND result_class='PASS') AND EXISTS (SELECT 1 FROM mcp_audit_event WHERE principal_id='$MCP_PRINCIPAL_ID'::uuid AND tool_name='genos.cards.create' AND decision='DENY' AND result_class='FORBIDDEN') THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT COUNT(*) FROM card WHERE title='must-not-run'\" | grep -qx 0"
  if [[ "$require_degraded" == 1 ]]; then
    ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN EXISTS (SELECT 1 FROM mcp_audit_event WHERE principal_id='$MCP_PRINCIPAL_ID'::uuid AND upstream_id='$MCP_UPSTREAM_ID'::uuid AND tool_name='fixture.echo' AND result_class='UPSTREAM_DEGRADED') THEN 1 ELSE 0 END\" | grep -qx 1"
  fi
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_audit_event' AND column_name ~ '(secret|token|argument|payload)'\" | grep -qx 0"
}

assert_mcp_secret_hygiene() {
  local expected_hash actual_hash
  expected_hash="$(printf '%s' "$MCP_TOKEN" | sha256sum | awk '{print $1}')"
  actual_hash="$(ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT encode(token_hash,'hex') FROM mcp_principal WHERE principal_id='$MCP_PRINCIPAL_ID'::uuid\"")"
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    echo "MCP principal token was not persisted as the expected one-way hash" >&2
    return 1
  fi
  printf '%s\n%s\n%s\n%s\n' "$MCP_TOKEN" "$UPSTREAM_SECRET" "$OWNER_SESSION_TOKEN" "$OWNER_PASSWORD" | ssh_guest "sudo python3 /var/lib/genos/mcp-e2e-hygiene.py $MCP_SECRET_ID"
}

assert_host_artifacts_sanitized() {
  local material path
  for material in "$MCP_TOKEN" "$UPSTREAM_SECRET" "$OWNER_SESSION_TOKEN" "$OWNER_PASSWORD"; do
    for path in "$SERIAL_LOG" "$WORK_DIR/install.json" "$WORK_DIR/rerun.json" "$EVIDENCE"; do
      if [[ -f "$path" ]] && grep -Fq -- "$material" "$path"; then
        echo "raw authentication material entered host evidence: $path" >&2
        return 1
      fi
    done
  done
}

echo "==> Pin and verify Ubuntu 24.04 fresh-host image"
curl --fail --location --retry 3 --retry-delay 2 "$IMAGE_URL" -o "$BASE_IMAGE"
echo "$IMAGE_SHA256  $BASE_IMAGE" | sha256sum -c -
qemu-img info "$BASE_IMAGE"

ssh-keygen -q -t ed25519 -N "" -f "$KEY"
PUBLIC_KEY="$(cat "$KEY.pub")"
cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
users:
  - default
  - name: $SSH_USER
    groups: [adm, sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $PUBLIC_KEY
ssh_pwauth: false
disable_root: true
package_update: false
EOF
cat > "$WORK_DIR/meta-data" <<EOF
instance-id: genos-mvp-${TESTED_SHA}
local-hostname: genos-mvp-e2e
EOF
cloud-localds "$SEED_IMAGE" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"

qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$VM_IMAGE"
qemu-img resize "$VM_IMAGE" 12G

QEMU_ACCEL=(-accel "tcg,thread=multi" -cpu max)
if [[ -c /dev/kvm ]]; then
  sudo chmod 666 /dev/kvm || true
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    QEMU_ACCEL=(-accel kvm -cpu host)
  fi
fi

echo "==> Boot isolated fresh VM"
qemu-system-x86_64 \
  "${QEMU_ACCEL[@]}" \
  -m 3072 \
  -smp 2 \
  -display none \
  -monitor none \
  -serial "file:$SERIAL_LOG" \
  -drive "file=$VM_IMAGE,format=qcow2,if=virtio" \
  -drive "file=$SEED_IMAGE,format=raw,if=virtio,readonly=on" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
  -device virtio-net-pci,netdev=net0 \
  -pidfile "$PID_FILE" \
  -daemonize

ready=0
for _ in $(seq 1 180); do
  if ssh_guest true >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 3
done
if [[ "$ready" != 1 ]]; then
  echo "SSH did not become ready" >&2
  tail -n 200 "$SERIAL_LOG" || true
  exit 1
fi
ssh_guest "sudo cloud-init status --wait"

GUEST_OS="$(ssh_guest '. /etc/os-release; printf "%s %s" "$ID" "$VERSION_ID"')"
GUEST_ARCH="$(ssh_guest 'uname -m')"
if [[ "$GUEST_OS" != "ubuntu 24.04" || "$GUEST_ARCH" != "x86_64" ]]; then
  echo "unexpected guest profile: os=$GUEST_OS arch=$GUEST_ARCH" >&2
  exit 1
fi

echo "==> Build exact-head release artifact"
git archive --format=tar.gz --output="$RELEASE" HEAD
RELEASE_SHA256="$(sha256sum "$RELEASE" | awk '{print $1}')"
scp_guest "$RELEASE" "$SSH_USER@127.0.0.1:/tmp/genos-release.tar.gz"
ssh_guest "rm -rf /tmp/genos-bootstrap && mkdir /tmp/genos-bootstrap && tar -xzf /tmp/genos-release.tar.gz -C /tmp/genos-bootstrap"

# Final verification intentionally uses the normal public install path: no
# candidate-only environment variable and no hidden candidate flag.
INSTALL_CMD="sudo env PYTHONPATH=/tmp/genos-bootstrap/src python3 -m genos install --mode native --release /tmp/genos-release.tar.gz --release-sha256 $RELEASE_SHA256 --git-sha $TESTED_SHA --json"

echo "==> First one-command install"
ssh_guest "$INSTALL_CMD" | tee "$WORK_DIR/install.json"
FIRST_INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
FIRST_BOOT_ID="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
FIRST_MCP_PORT="$(ssh_guest 'sudo cat /etc/genos/mcp-port')"

verify_guest() {
  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service"
  ssh_guest "sudo python3 - <<'PY'
import grp, json, os, pwd, stat, urllib.error, urllib.request

mcp_port_path='/etc/genos/mcp-port'
mcp_port=int(open(mcp_port_path, encoding='utf-8').read().strip())
assert 1024 <= mcp_port <= 65535 and mcp_port != $MCP_FIXTURE_PORT, mcp_port
port_info=os.stat(mcp_port_path)
assert pwd.getpwuid(port_info.st_uid).pw_name == 'root', port_info
assert grp.getgrgid(port_info.st_gid).gr_name == 'genos', port_info
assert stat.S_IMODE(port_info.st_mode) == 0o640, oct(stat.S_IMODE(port_info.st_mode))
config_info=os.stat('/etc/genos')
assert pwd.getpwuid(config_info.st_uid).pw_name == 'root', config_info
assert grp.getgrgid(config_info.st_gid).gr_name == 'genos', config_info
assert stat.S_IMODE(config_info.st_mode) == 0o750, oct(stat.S_IMODE(config_info.st_mode))
assert os.path.realpath('/opt/genos/current') == '/opt/genos/releases/$TESTED_SHA'

listeners=[]
for family, path in (('ipv4','/proc/net/tcp'),('ipv6','/proc/net/tcp6')):
    for row in open(path, encoding='utf-8').read().splitlines()[1:]:
        fields=row.split()
        local_address, state=fields[1], fields[3]
        address_hex, port_hex=local_address.rsplit(':', 1)
        if state == '0A' and int(port_hex, 16) == mcp_port:
            listeners.append((family, address_hex))
assert listeners == [('ipv4', '0100007F')], listeners

for role, port in [('product-api',17880),('runtime',17881),('mcp-hub',mcp_port),('mission-control',17882)]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
        payload=json.load(response)
    assert response.status == 200, (role, response.status)
    assert payload['status'] == 'ok', payload
    assert payload['role'] == role, payload
with urllib.request.urlopen('http://127.0.0.1:17882/', timeout=5) as response:
    mission_html=response.read().decode('utf-8')
    mission_status=response.status
    mission_csp=response.headers.get('Content-Security-Policy', '')
    mission_nosniff=response.headers.get('X-Content-Type-Options', '')
assert mission_status == 200, mission_status
assert 'GenOS Mission Control' in mission_html, mission_html[:200]
assert "frame-ancestors 'none'" in mission_csp, mission_csp
assert mission_nosniff == 'nosniff', mission_nosniff
with urllib.request.urlopen('http://127.0.0.1:17882/assets/app.js', timeout=5) as response:
    mission_asset=response.read()
    mission_asset_status=response.status
assert mission_asset_status == 200, mission_asset_status
assert mission_asset, 'Mission Control app.js is empty'
for protected in ('/api/v1/drive', '/api/v1/cards', '/api/v1/mcp'):
    try:
        urllib.request.urlopen('http://127.0.0.1:17880' + protected, timeout=5)
        raise AssertionError(f'{protected} unexpectedly allowed unauthenticated access')
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, (protected, exc.code)
        error_payload = json.loads(exc.read().decode('utf-8'))
        exc.close()
        assert error_payload['error'] == 'unauthorized', error_payload
mcp_body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{'_meta':{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientInfo':{'name':'fresh-host-unauthenticated-probe','version':'0.1'},'io.modelcontextprotocol/clientCapabilities':{}}}}).encode()
mcp_req=urllib.request.Request(f'http://127.0.0.1:{mcp_port}/mcp', data=mcp_body, method='POST', headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2026-07-28','Mcp-Method':'tools/list'})
try:
    urllib.request.urlopen(mcp_req, timeout=5)
    raise AssertionError('MCP Hub unexpectedly allowed unauthenticated access')
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
    exc.close()
assert json.load(open('/var/lib/genos/worker/heartbeat.json', encoding='utf-8'))['status'] == 'ok'
manifest=json.load(open('/var/lib/genos/manifest.json', encoding='utf-8'))
assert manifest['state'] == 'READY_LOCAL_CORE', manifest
assert manifest['release']['git_sha'] == '$TESTED_SHA', manifest
assert manifest['release']['sha256'] == '$RELEASE_SHA256', manifest
assert manifest['profile_id'] == 'ubuntu-24.04-amd64-native', manifest
assert manifest['support_class'] == 'SUPPORTED', manifest
assert manifest['support_evidence'] == 'VERIFIED_PROFILE', manifest
assert manifest['services']['mission_control_ui'] == 'READY', manifest
PY"
  ssh_guest "sudo -u genos psql -d genos -tAc 'SELECT 1' | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN to_regclass('public.drive_binding') IS NOT NULL THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=4) THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN to_regclass('public.card') IS NOT NULL AND to_regclass('public.card_event') IS NOT NULL AND to_regclass('public.card_artifact') IS NOT NULL THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=5) THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN to_regclass('public.mcp_principal') IS NOT NULL AND to_regclass('public.mcp_upstream') IS NOT NULL AND to_regclass('public.mcp_audit_event') IS NOT NULL THEN 1 ELSE 0 END\" | grep -qx 1"
  ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT CASE WHEN EXISTS (SELECT 1 FROM genos_schema_migration WHERE version=6) THEN 1 ELSE 0 END\" | grep -qx 1"
}
verify_guest

echo "==> Unified MCP external-agent and SecretRef federation acceptance"
install_mcp_fixture_helpers
OWNER_PASSWORD="$(python3 -c 'import secrets; print("Owner!" + secrets.token_urlsafe(24))')"
UPSTREAM_SECRET="$(python3 -c 'import secrets; print("upstream_fixture_" + secrets.token_urlsafe(32))')"
MCP_SETUP_JSON="$(printf '%s\n%s\n' "$OWNER_PASSWORD" "$UPSTREAM_SECRET" | ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-setup.py $MCP_FIXTURE_ENDPOINT")"
MCP_SECRET_ID="$(printf '%s' "$MCP_SETUP_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret_id"])')"
OWNER_SESSION_TOKEN="$(printf '%s' "$MCP_SETUP_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner_session_token"])')"
MCP_TOKEN="$(printf '%s' "$MCP_SETUP_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mcp_access_token"])')"
MCP_PRINCIPAL_ID="$(printf '%s' "$MCP_SETUP_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["principal_id"])')"
MCP_UPSTREAM_ID="$(printf '%s' "$MCP_SETUP_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upstream_id"])')"
unset MCP_SETUP_JSON

start_upstream_fixture

assert_mcp_registry_state ACTIVE
run_mcp_client live
assert_mcp_audit 0
assert_mcp_secret_hygiene

echo "==> Idempotent rerun"
ssh_guest "$INSTALL_CMD" | tee "$WORK_DIR/rerun.json"
SECOND_INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
if [[ "$SECOND_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then
  echo "instance_id changed across rerun" >&2
  exit 1
fi
SECOND_MCP_PORT="$(ssh_guest 'sudo cat /etc/genos/mcp-port')"
if [[ "$SECOND_MCP_PORT" != "$FIRST_MCP_PORT" ]]; then echo "MCP port changed across rerun" >&2; exit 1; fi
verify_guest
assert_mcp_registry_state ACTIVE
run_mcp_client live
assert_mcp_audit 0

echo "==> Federated upstream outage isolation and recovery"
stop_upstream_fixture
run_mcp_client outage
assert_mcp_audit 1
start_upstream_fixture
run_mcp_client live
assert_mcp_secret_hygiene

echo "==> Reboot recovery"
ssh_guest "sudo systemctl reboot" >/dev/null 2>&1 || true
sleep 8
rebooted=0
for _ in $(seq 1 180); do
  if ssh_guest true >/dev/null 2>&1; then
    NEW_BOOT_ID="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
    if [[ -n "$NEW_BOOT_ID" && "$NEW_BOOT_ID" != "$FIRST_BOOT_ID" ]]; then
      rebooted=1
      break
    fi
  fi
  sleep 3
done
if [[ "$rebooted" != 1 ]]; then
  echo "guest did not complete a verified reboot" >&2
  tail -n 200 "$SERIAL_LOG" || true
  exit 1
fi
ssh_guest "sudo cloud-init status --wait" || true
THIRD_INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
if [[ "$THIRD_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then
  echo "instance_id changed across reboot" >&2
  exit 1
fi
THIRD_MCP_PORT="$(ssh_guest 'sudo cat /etc/genos/mcp-port')"
if [[ "$THIRD_MCP_PORT" != "$FIRST_MCP_PORT" ]]; then echo "MCP port changed across reboot" >&2; exit 1; fi
verify_guest
assert_mcp_registry_state ACTIVE
run_mcp_client outage
assert_mcp_audit 1
start_upstream_fixture
run_mcp_client live
assert_mcp_registry_state ACTIVE
assert_mcp_audit 1
assert_mcp_secret_hygiene

echo "==> Principal revoke is immediate at the single persisted endpoint"
printf '%s\n' "$OWNER_SESSION_TOKEN" | ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /var/lib/genos/mcp-e2e-owner-api.py revoke $MCP_PRINCIPAL_ID" >/dev/null
run_mcp_client revoked
assert_mcp_registry_state REVOKED
assert_mcp_audit 1
assert_mcp_secret_hygiene
stop_upstream_fixture

python3 - "$EVIDENCE" <<PY
import json, sys
payload = {
    "schema_version": "1.2",
    "profile_id": "ubuntu-24.04-amd64-native",
    "profile_state_during_run": "VERIFIED",
    "image_url": "$IMAGE_URL",
    "image_sha256": "$IMAGE_SHA256",
    "tested_git_sha": "$TESTED_SHA",
    "release_sha256": "$RELEASE_SHA256",
    "instance_id": "$FIRST_INSTANCE_ID",
    "first_boot_id": "$FIRST_BOOT_ID",
    "reboot_boot_id": "$NEW_BOOT_ID",
    "install": "PASS",
    "rerun": "PASS",
    "reboot_recovery": "PASS",
    "local_core_health": "PASS",
    "drive_schema_v4": "PASS",
    "drive_api_owner_auth_boundary": "PASS",
    "kanban_schema_v5": "PASS",
    "card_api_owner_auth_boundary": "PASS",
    "mcp_schema_v6": "PASS",
    "mcp_hub_local_auth_boundary": "PASS",
    "mcp_port_persistence": "PASS",
    "mcp_port_loopback_and_mode": "PASS",
    "mcp_protocol_2026_07_28": "PASS",
    "mcp_single_endpoint_discover_local_federated": "PASS",
    "mcp_owner_api_management_lifecycle": "PASS",
    "mcp_principal_and_upstream_persistence": "PASS",
    "mcp_upstream_outage_isolation": "PASS",
    "mcp_principal_revoke": "PASS",
    "mcp_audit_and_secret_hygiene": "PASS",
    "support_class": "SUPPORTED",
    "support_evidence": "VERIFIED_PROFILE",
    "mission_control_ui": "READY"
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(json.dumps(payload, indent=2, sort_keys=True))
PY

assert_host_artifacts_sanitized
unset MCP_TOKEN UPSTREAM_SECRET OWNER_SESSION_TOKEN OWNER_PASSWORD

echo "GENOS_MVP07_MCP_FRESH_HOST_E2E_PASS"
echo "GENOS_MVP02_FRESH_HOST_E2E_PASS"
