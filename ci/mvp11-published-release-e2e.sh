#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <downloaded-release-dir>" >&2
  exit 2
fi

RELEASE_DIR="$(cd "$1" && pwd)"
METADATA="$RELEASE_DIR/install-metadata.json"
SUMS="$RELEASE_DIR/SHA256SUMS"
BOOTSTRAP="$RELEASE_DIR/bootstrap.sh"

for path in "$METADATA" "$SUMS" "$BOOTSTRAP"; do
  test -f "$path"
done

readarray -t META < <(python3 - "$METADATA" <<'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding='utf-8'))
assert p.get('release_state') == 'RELEASE_CANDIDATE', p
assert p.get('certified_target') == 'ubuntu-24.04-amd64-native', p
for key in ('git_sha','archive','sha256','bootstrap','bootstrap_sha256'):
    value=p.get(key)
    assert isinstance(value,str) and value, (key,p)
print(p['git_sha'])
print(p['archive'])
print(p['sha256'])
print(p['bootstrap_sha256'])
PY
)
RELEASE_SHA="${META[0]}"
ARCHIVE_NAME="${META[1]}"
ARCHIVE_SHA="${META[2]}"
BOOTSTRAP_SHA="${META[3]}"
ARCHIVE="$RELEASE_DIR/$ARCHIVE_NAME"
test -f "$ARCHIVE"

(
  cd "$RELEASE_DIR"
  sha256sum --check SHA256SUMS
)
test "$(sha256sum "$ARCHIVE" | awk '{print $1}')" = "$ARCHIVE_SHA"
test "$(sha256sum "$BOOTSTRAP" | awk '{print $1}')" = "$BOOTSTRAP_SHA"

IMAGE_URL="https://cloud-images.ubuntu.com/releases/noble/release-20260801/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
WORK_DIR="${RUNNER_TEMP:-/tmp}/genos-published-release-e2e"
SSH_PORT="${GENOS_PUBLISHED_RELEASE_SSH_PORT:-2249}"
SSH_USER="genos-ci"
KEY="$WORK_DIR/id_ed25519"
BASE_IMAGE="$WORK_DIR/ubuntu-24.04.img"
VM_IMAGE="$WORK_DIR/genos-vm.qcow2"
SEED_IMAGE="$WORK_DIR/seed.img"
SERIAL_LOG="$WORK_DIR/qemu-serial.log"
PID_FILE="$WORK_DIR/qemu.pid"
EVIDENCE="$WORK_DIR/published-release-evidence.json"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"

cleanup() {
  set +e
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 2
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

ssh_guest() {
  ssh -i "$KEY" -p "$SSH_PORT" \
    -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
    "$SSH_USER@127.0.0.1" "$@"
}

scp_guest() {
  scp -i "$KEY" -P "$SSH_PORT" \
    -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$@"
}

verify_enabled() {
  ssh_guest "sudo systemctl is-enabled postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service"
}

verify_core() {
  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service"
  ssh_guest "sudo python3 - <<'PY'
import json, urllib.request
mcp=int(open('/etc/genos/mcp-port', encoding='utf-8').read().strip())
for role, port in [('product-api',17880),('runtime',17881),('mcp-hub',mcp),('mission-control',17882)]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=8) as response:
        payload=json.load(response)
    assert response.status == 200 and payload['status'] == 'ok' and payload['role'] == role, (role,payload)
worker=json.load(open('/var/lib/genos/worker/heartbeat.json', encoding='utf-8'))
assert worker['status'] == 'ok' and worker['role'] == 'worker', worker
PY"
  ssh_guest "sudo -u genos psql -d genos -tAc 'SELECT 1' | grep -qx 1"
}

diagnose_core() {
  set +e
  echo "==> GenOS post-reboot diagnostics" >&2
  ssh_guest "sudo systemctl is-system-running || true; sudo systemctl --failed --no-pager || true" >&2
  ssh_guest "sudo systemctl is-enabled postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service || true" >&2
  ssh_guest "sudo systemctl --no-pager --full status postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service || true" >&2
  set -e
}

wait_for_core() {
  local attempts="${1:-60}"
  for _ in $(seq 1 "$attempts"); do
    if verify_core >/dev/null 2>&1; then
      verify_core
      return 0
    fi
    sleep 2
  done
  diagnose_core
  return 1
}

echo "==> Boot pinned Ubuntu 24.04 amd64 fresh target"
curl --fail --location --retry 3 --retry-delay 2 "$IMAGE_URL" -o "$BASE_IMAGE"
echo "$IMAGE_SHA256  $BASE_IMAGE" | sha256sum -c -
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
instance-id: genos-published-${RELEASE_SHA}
local-hostname: genos-published-e2e
EOF
cloud-localds "$SEED_IMAGE" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"
qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$VM_IMAGE"
qemu-img resize "$VM_IMAGE" 12G
QEMU_ACCEL=(-accel "tcg,thread=multi" -cpu max)
if [[ -c /dev/kvm ]]; then
  sudo chmod 666 /dev/kvm || true
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then QEMU_ACCEL=(-accel kvm -cpu host); fi
fi
qemu-system-x86_64 "${QEMU_ACCEL[@]}" -m 3072 -smp 2 -display none -monitor none \
  -serial "file:$SERIAL_LOG" \
  -drive "file=$VM_IMAGE,format=qcow2,if=virtio" \
  -drive "file=$SEED_IMAGE,format=raw,if=virtio,readonly=on" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
  -device virtio-net-pci,netdev=net0 -pidfile "$PID_FILE" -daemonize

ready=0
for _ in $(seq 1 180); do
  if ssh_guest true >/dev/null 2>&1; then ready=1; break; fi
  sleep 3
done
if [[ "$ready" != 1 ]]; then
  tail -n 200 "$SERIAL_LOG" || true
  exit 1
fi
ssh_guest "sudo cloud-init status --wait"
[[ "$(ssh_guest '. /etc/os-release; printf "%s %s" "$ID" "$VERSION_ID"')" == "ubuntu 24.04" ]]
[[ "$(ssh_guest 'uname -m')" == "x86_64" ]]

scp_guest "$ARCHIVE" "$BOOTSTRAP" "$SSH_USER@127.0.0.1:/tmp/"
ssh_guest "chmod 700 /tmp/bootstrap.sh"

echo "==> Install from actual GitHub prerelease assets"
ssh_guest "sudo /tmp/bootstrap.sh --release /tmp/$ARCHIVE_NAME --sha256 $ARCHIVE_SHA --git-sha $RELEASE_SHA"
verify_enabled
wait_for_core 30
FIRST_INSTANCE="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
FIRST_MCP_PORT="$(ssh_guest 'sudo cat /etc/genos/mcp-port')"
FIRST_BOOT="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
INSTALLED_SHA="$(ssh_guest "sudo python3 -c \"import json; print(json.load(open('/var/lib/genos/manifest.json'))['release']['git_sha'])\"")"
test "$INSTALLED_SHA" = "$RELEASE_SHA"

# External OAuth is intentionally not automated. The fresh instance must remain
# truthful and locally healthy while those user actions are pending.
STATUS_JSON="$(ssh_guest 'sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos status --json')"
python3 - "$STATUS_JSON" <<'PY'
import json, sys
p=json.loads(sys.argv[1])
text=json.dumps(p)
assert 'READY' in text or 'NEEDS_ACTION' in text, p
PY

echo "==> Reboot published-release instance"
ssh_guest "sudo reboot" >/dev/null 2>&1 || true
reboot_ready=0
for _ in $(seq 1 180); do
  sleep 3
  if ssh_guest true >/dev/null 2>&1; then reboot_ready=1; break; fi
done
if [[ "$reboot_ready" != 1 ]]; then
  tail -n 200 "$SERIAL_LOG" || true
  exit 1
fi
SECOND_BOOT="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
test "$SECOND_BOOT" != "$FIRST_BOOT"
# SSH can accept connections before multi-user.target and dependent GenOS units
# have settled. Prove persistence with is-enabled, then allow bounded startup
# time; this still fails if any service never becomes healthy.
ssh_guest "sudo systemctl is-system-running --wait >/dev/null 2>&1 || true"
verify_enabled
wait_for_core 60
test "$(ssh_guest 'sudo cat /etc/genos/instance-id')" = "$FIRST_INSTANCE"
test "$(ssh_guest 'sudo cat /etc/genos/mcp-port')" = "$FIRST_MCP_PORT"

# Produce and inspect a real support bundle from the published install.
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos support-bundle --output /tmp/genos-support.tar.gz --json" >/dev/null
ssh_guest "sudo cp /tmp/genos-support.tar.gz /home/$SSH_USER/genos-support.tar.gz && sudo chown $SSH_USER:$SSH_USER /home/$SSH_USER/genos-support.tar.gz"
scp_guest "$SSH_USER@127.0.0.1:/home/$SSH_USER/genos-support.tar.gz" "$WORK_DIR/genos-support.tar.gz"
mkdir -p "$WORK_DIR/support"
tar -xzf "$WORK_DIR/genos-support.tar.gz" -C "$WORK_DIR/support"
! grep -R -a -E 'genos_mcp_[A-Za-z0-9_-]+' "$WORK_DIR/support"
! grep -R -a -E 'Bearer [A-Za-z0-9._-]{12,}' "$WORK_DIR/support"

python3 - "$EVIDENCE" "$RELEASE_SHA" "$ARCHIVE_SHA" "$FIRST_INSTANCE" "$FIRST_MCP_PORT" "$FIRST_BOOT" "$SECOND_BOOT" <<'PY'
import json, sys
path, sha, archive_sha, instance, mcp_port, boot1, boot2 = sys.argv[1:]
payload={
  'schema_version':'1.1',
  'gate':'MVP11_PUBLISHED_RELEASE_FRESH_HOST',
  'state':'PASS',
  'release_git_sha':sha,
  'archive_sha256':archive_sha,
  'certified_target':'ubuntu-24.04-amd64-native',
  'instance_id':instance,
  'mcp_port':int(mcp_port),
  'reboot_verified':boot1 != boot2,
  'service_enablement_verified':True,
  'post_reboot_readiness_waited':True,
  'local_core_healthy':True,
  'external_auth_fabricated':False,
  'support_bundle_secret_scan':'PASS',
}
open(path,'w',encoding='utf-8').write(json.dumps(payload,indent=2,sort_keys=True)+'\n')
PY
cat "$EVIDENCE"
