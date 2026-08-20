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

INSTALL_CMD="sudo env GENOS_FRESH_HOST_E2E=1 PYTHONPATH=/tmp/genos-bootstrap/src python3 -m genos install --mode native --release /tmp/genos-release.tar.gz --release-sha256 $RELEASE_SHA256 --git-sha $TESTED_SHA --candidate-e2e --json"

echo "==> First one-command install"
ssh_guest "$INSTALL_CMD" | tee "$WORK_DIR/install.json"
FIRST_INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
FIRST_BOOT_ID="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"

verify_guest() {
  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mission-control.service"
  ssh_guest "python3 - <<'PY'
import json, urllib.request
for role, port in [('product-api',17880),('runtime',17881),('mission-control',17882)]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
        payload=json.load(response)
    assert response.status == 200, (role, response.status)
    assert payload['status'] == 'ok', payload
    assert payload['role'] == role, payload
assert json.load(open('/var/lib/genos/worker/heartbeat.json', encoding='utf-8'))['status'] == 'ok'
manifest=json.load(open('/var/lib/genos/manifest.json', encoding='utf-8'))
assert manifest['state'] == 'READY_LOCAL_CORE', manifest
assert manifest['release']['git_sha'] == '$TESTED_SHA', manifest
assert manifest['release']['sha256'] == '$RELEASE_SHA256', manifest
assert manifest['profile_id'] == 'ubuntu-24.04-amd64-native', manifest
assert manifest['support_evidence'] == 'CANDIDATE_E2E', manifest
assert manifest['services']['mission_control_ui'] == 'NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL', manifest
PY"
  ssh_guest "sudo -u genos psql -d genos -tAc 'SELECT 1' | grep -qx 1"
}
verify_guest

echo "==> Idempotent rerun"
ssh_guest "$INSTALL_CMD" | tee "$WORK_DIR/rerun.json"
SECOND_INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
if [[ "$SECOND_INSTANCE_ID" != "$FIRST_INSTANCE_ID" ]]; then
  echo "instance_id changed across rerun" >&2
  exit 1
fi
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
verify_guest

python3 - "$EVIDENCE" <<PY
import json, sys
payload = {
    "schema_version": "1.0",
    "profile_id": "ubuntu-24.04-amd64-native",
    "profile_state_during_run": "CANDIDATE_E2E_ONLY",
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
    "mission_control_ui": "NOT_IMPLEMENTED_BEFORE_MVP_08_VISUAL_APPROVAL"
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(json.dumps(payload, indent=2, sort_keys=True))
PY

echo "GENOS_MVP02_FRESH_HOST_E2E_PASS"
