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
import json, urllib.error, urllib.request
mcp_port=int(open('/etc/genos/mcp-port', encoding='utf-8').read().strip())
for role, port in [('product-api',17880),('runtime',17881),('mcp-hub',mcp_port),('mission-control',17882)]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
        payload=json.load(response)
    assert response.status == 200, (role, response.status)
    assert payload['status'] == 'ok', payload
    assert payload['role'] == role, payload
try:
    urllib.request.urlopen('http://127.0.0.1:17882/', timeout=5)
    raise AssertionError('Mission Control root unexpectedly returned success before MVP-08')
except urllib.error.HTTPError as exc:
    assert exc.code == 503, exc.code
for protected in ('/api/v1/drive', '/api/v1/cards', '/api/v1/mcp'):
    try:
        urllib.request.urlopen('http://127.0.0.1:17880' + protected, timeout=5)
        raise AssertionError(f'{protected} unexpectedly allowed unauthenticated access')
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, (protected, exc.code)
        error_payload = json.loads(exc.read().decode('utf-8'))
        assert error_payload['error'] == 'unauthorized', error_payload
mcp_body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode()
mcp_req=urllib.request.Request(f'http://127.0.0.1:{mcp_port}/mcp', data=mcp_body, method='POST', headers={'Content-Type':'application/json','MCP-Protocol-Version':'2026-07-28','Mcp-Method':'tools/list'})
try:
    urllib.request.urlopen(mcp_req, timeout=5)
    raise AssertionError('MCP Hub unexpectedly allowed unauthenticated access')
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
assert json.load(open('/var/lib/genos/worker/heartbeat.json', encoding='utf-8'))['status'] == 'ok'
manifest=json.load(open('/var/lib/genos/manifest.json', encoding='utf-8'))
assert manifest['state'] == 'READY_LOCAL_CORE', manifest
assert manifest['release']['git_sha'] == '$TESTED_SHA', manifest
assert manifest['release']['sha256'] == '$RELEASE_SHA256', manifest
assert manifest['profile_id'] == 'ubuntu-24.04-amd64-native', manifest
assert manifest['support_class'] == 'SUPPORTED', manifest
assert manifest['support_evidence'] == 'VERIFIED_PROFILE', manifest
assert manifest['services']['mission_control_ui'] == 'NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL', manifest
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

python3 - "$EVIDENCE" <<PY
import json, sys
payload = {
    "schema_version": "1.1",
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
    "support_class": "SUPPORTED",
    "support_evidence": "VERIFIED_PROFILE",
    "mission_control_ui": "NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL"
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(json.dumps(payload, indent=2, sort_keys=True))
PY

echo "GENOS_MVP02_FRESH_HOST_E2E_PASS"
