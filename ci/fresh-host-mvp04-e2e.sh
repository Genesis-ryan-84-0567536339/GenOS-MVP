#!/usr/bin/env bash
set -euo pipefail

IMAGE_URL="https://cloud-images.ubuntu.com/releases/noble/release-20260801/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
WORK_DIR="${RUNNER_TEMP:-/tmp}/genos-mvp04-fresh-host-${MVP04_REQUIRE_PROVIDER:-0}"
SSH_PORT="${GENOS_MVP04_E2E_SSH_PORT:-2224}"
SSH_USER="genos-ci"
KEY="$WORK_DIR/id_ed25519"
BASE_IMAGE="$WORK_DIR/ubuntu-24.04.img"
VM_IMAGE="$WORK_DIR/genos-vm.qcow2"
SEED_IMAGE="$WORK_DIR/seed.img"
SERIAL_LOG="$WORK_DIR/qemu-serial.log"
PID_FILE="$WORK_DIR/qemu.pid"
RELEASE="$WORK_DIR/genos-release.tar.gz"
EVIDENCE="$WORK_DIR/mvp04-fresh-host-evidence.json"
TESTED_SHA="$(git rev-parse HEAD)"
REQUIRE_PROVIDER="${MVP04_REQUIRE_PROVIDER:-0}"
GUEST_TOOL_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"

cleanup() {
  set +e
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
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
  ssh -i "$KEY" -p "$SSH_PORT" \
    -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
    "$SSH_USER@127.0.0.1" "$@"
}

scp_guest() {
  scp -i "$KEY" -P "$SSH_PORT" \
    -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$@"
}

echo "==> Verify pinned Ubuntu 24.04 image"
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
instance-id: genos-mvp04-${TESTED_SHA}-${REQUIRE_PROVIDER}
local-hostname: genos-mvp04-e2e
EOF
cloud-localds "$SEED_IMAGE" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"
qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$VM_IMAGE"
qemu-img resize "$VM_IMAGE" 14G

QEMU_ACCEL=(-accel "tcg,thread=multi" -cpu max)
if [[ -c /dev/kvm ]]; then
  sudo chmod 666 /dev/kvm || true
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    QEMU_ACCEL=(-accel kvm -cpu host)
  fi
fi

qemu-system-x86_64 \
  "${QEMU_ACCEL[@]}" -m 4096 -smp 2 -display none -monitor none \
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
ssh_guest "sudo cloud-init status --wait" >/dev/null
GUEST_OS="$(ssh_guest '. /etc/os-release; printf "%s %s" "$ID" "$VERSION_ID"')"
GUEST_ARCH="$(ssh_guest 'uname -m')"
[[ "$GUEST_OS" == "ubuntu 24.04" && "$GUEST_ARCH" == "x86_64" ]]

echo "==> Install exact GenOS head"
git archive --format=tar.gz --output="$RELEASE" HEAD
RELEASE_SHA256="$(sha256sum "$RELEASE" | awk '{print $1}')"
scp_guest "$RELEASE" "$SSH_USER@127.0.0.1:/tmp/genos-release.tar.gz"
ssh_guest "rm -rf /tmp/genos-bootstrap && mkdir /tmp/genos-bootstrap && tar -xzf /tmp/genos-release.tar.gz -C /tmp/genos-bootstrap"
INSTALL_CMD="sudo env PYTHONPATH=/tmp/genos-bootstrap/src python3 -m genos install --mode native --release /tmp/genos-release.tar.gz --release-sha256 $RELEASE_SHA256 --git-sha $TESTED_SHA --json"
ssh_guest "$INSTALL_CMD" > "$WORK_DIR/install.json"
INSTANCE_ID="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
FIRST_BOOT_ID="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"

for _ in $(seq 1 60); do
  if ssh_guest "sudo test -f /var/lib/genos/agents/agy-gen/identity.json" >/dev/null 2>&1; then break; fi
  sleep 1
done
ssh_guest "sudo test -f /var/lib/genos/agents/agy-gen/identity.json"
IDENTITY_BEFORE="$(ssh_guest "sudo python3 -c \"import json; print(json.load(open('/var/lib/genos/agents/agy-gen/identity.json'))['agent_id'])\"")"
[[ "$IDENTITY_BEFORE" == "agy-gen" ]]

echo "==> Provision pinned Node/Gemini/tmux toolchain"
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos agent provision --json" > "$WORK_DIR/provision.json"
ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH node --version | grep -qx 'v24.19.0'"
ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH gemini --version | grep -qx '0.53.0'"
ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH tmux -V | grep -q '^tmux '"
ssh_guest "sudo python3 - <<'PY'
import json
p=json.load(open('/var/lib/genos/tools/agy-gen-toolchain.json'))
assert p['state']=='READY', p
assert p['node']['version']=='24.19.0', p
assert p['node']['sha256']=='14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647', p
assert p['gemini_cli']['version']=='0.53.0', p
assert str(p['gemini_cli']['npm_integrity']).startswith('sha512-'), p
PY"

if [[ "$REQUIRE_PROVIDER" == "1" ]]; then
  if [[ -z "${GENOS_MVP_GEMINI_API_KEY:-}" ]]; then
    echo "MVP04_NEEDS_ACTION_GEMINI_API_KEY" >&2
    exit 3
  fi
  echo "==> Bind SecretRef and verify real Gemini model"
  printf '%s' "$GENOS_MVP_GEMINI_API_KEY" | ssh_guest \
    "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 /opt/genos/current/ci/mvp04-agent-e2e.py bootstrap" \
    > "$WORK_DIR/credential-ref.txt"
  ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 -m genos agent restart --json" >/dev/null
  ssh_guest "sudo -u genos tmux has-session -t agy-gen"
  ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 /opt/genos/current/ci/mvp04-agent-e2e.py task-before-reboot"
else
  echo "==> Verify truthful pre-auth NEEDS_ACTION with persistent auth tmux"
  auth_ready=0
  for _ in $(seq 1 60); do
    if ssh_guest "sudo -u genos tmux has-session -t agy-gen && sudo -u genos tmux list-windows -t agy-gen -F '#{window_name}' | grep -qx auth" >/dev/null 2>&1; then
      auth_ready=1
      break
    fi
    sleep 1
  done
  [[ "$auth_ready" == "1" ]]
  ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 - <<'PY'
import json, subprocess
p=subprocess.run(['python3','-m','genos','agent','status','--json'],capture_output=True,text=True,check=True,env={'PYTHONPATH':'/opt/genos/current/src','PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/var/lib/genos','LANG':'C.UTF-8'})
s=json.loads(p.stdout)
assert s['identity']['agent_id']=='agy-gen', s
assert s['provider']['state']=='INSTALLED', s
assert s['runtime']['state']=='NEEDS_ACTION', s
assert s['runtime']['tmux_state']=='RUNNING', s
assert str(s['runtime']['reason']).startswith('AUTH_'), s
PY"
  ssh_guest "sudo -u genos tmux list-windows -t agy-gen -F '#{window_name}' | grep -qx auth"
  if ssh_guest "sudo -u genos tmux list-windows -t agy-gen -F '#{window_name}' | grep -qx runtime"; then
    echo "runtime window must not start before provider verification" >&2
    exit 1
  fi
fi

echo "==> Reboot and verify durable identity/runtime binding"
ssh_guest "sudo systemctl reboot" >/dev/null 2>&1 || true
sleep 8
rebooted=0
NEW_BOOT_ID=""
for _ in $(seq 1 180); do
  if ssh_guest true >/dev/null 2>&1; then
    NEW_BOOT_ID="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
    if [[ -n "$NEW_BOOT_ID" && "$NEW_BOOT_ID" != "$FIRST_BOOT_ID" ]]; then rebooted=1; break; fi
  fi
  sleep 3
done
[[ "$rebooted" == "1" ]]
IDENTITY_AFTER="$(ssh_guest "sudo python3 -c \"import json; print(json.load(open('/var/lib/genos/agents/agy-gen/identity.json'))['agent_id'])\"")"
[[ "$IDENTITY_AFTER" == "$IDENTITY_BEFORE" ]]

if [[ "$REQUIRE_PROVIDER" == "1" ]]; then
  for _ in $(seq 1 60); do
    if ssh_guest "sudo -u genos tmux has-session -t agy-gen" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  ssh_guest "sudo -u genos tmux has-session -t agy-gen"
  ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 /opt/genos/current/ci/mvp04-agent-e2e.py after-reboot"
  RESULT="PASS_REAL_PROVIDER"
else
  auth_recovered=0
  for _ in $(seq 1 60); do
    if ssh_guest "sudo -u genos tmux has-session -t agy-gen && sudo -u genos tmux list-windows -t agy-gen -F '#{window_name}' | grep -qx auth" >/dev/null 2>&1; then
      auth_recovered=1
      break
    fi
    sleep 1
  done
  [[ "$auth_recovered" == "1" ]]
  ssh_guest "sudo -u genos env PATH=$GUEST_TOOL_PATH PYTHONPATH=/opt/genos/current/src HOME=/var/lib/genos python3 -m genos agent status --json" > "$WORK_DIR/status-after-reboot.json"
  if ssh_guest "sudo -u genos tmux list-windows -t agy-gen -F '#{window_name}' | grep -qx runtime"; then
    echo "pre-auth reboot unexpectedly created runtime window" >&2
    exit 1
  fi
  RESULT="PASS_PREAUTH_AUTH_TMUX_NEEDS_ACTION"
fi

python3 - "$EVIDENCE" <<PY
import json, sys
payload={
  'schema_version':'1.0',
  'tested_git_sha':'$TESTED_SHA',
  'profile_id':'ubuntu-24.04-amd64-native',
  'instance_id':'$INSTANCE_ID',
  'agent_id':'agy-gen',
  'node_version':'24.19.0',
  'gemini_cli_version':'0.53.0',
  'target_model':'gemini-3.7-flash',
  'thinking_level':'HIGH',
  'approval_mode':'yolo',
  'provider_required': '$REQUIRE_PROVIDER' == '1',
  'result':'$RESULT',
  'identity_survived_reboot': True,
  'preauth_tmux_policy':'AUTH_WINDOW_PERSISTENT_RUNTIME_WINDOW_GATED'
}
with open(sys.argv[1],'w',encoding='utf-8') as h:
  json.dump(payload,h,indent=2,sort_keys=True); h.write('\n')
print(json.dumps(payload,indent=2,sort_keys=True))
PY

echo "GENOS_MVP04_FRESH_HOST_E2E_${RESULT}"
