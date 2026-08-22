#!/usr/bin/env bash
set -euo pipefail

IMAGE_URL="https://cloud-images.ubuntu.com/releases/noble/release-20260801/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
WORK_DIR="${RUNNER_TEMP:-/tmp}/genos-mvp11-fresh-host"
SSH_PORT="${GENOS_MVP11_SSH_PORT:-2241}"
SSH_USER="genos-ci"
KEY="$WORK_DIR/id_ed25519"
BASE_IMAGE="$WORK_DIR/ubuntu-24.04.img"
VM_IMAGE="$WORK_DIR/genos-vm.qcow2"
SEED_IMAGE="$WORK_DIR/seed.img"
SERIAL_LOG="$WORK_DIR/qemu-serial.log"
PID_FILE="$WORK_DIR/qemu.pid"
RELEASE="$WORK_DIR/genos-release.tar.gz"
GOOD_RELEASE="$WORK_DIR/genos-good-update.tar.gz"
BAD_RELEASE="$WORK_DIR/genos-bad-update.tar.gz"
SUPPORT_LOCAL="$WORK_DIR/genos-support.tar.gz"
EVIDENCE="$WORK_DIR/mvp11-evidence.json"
TESTED_SHA="$(git rev-parse HEAD)"
GOOD_SHA="dddddddddddddddddddddddddddddddddddddddd"
BAD_SHA="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
RAW_SECRET="MVP11_RAW_SECRET_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
RAW_SECRET_SHA="$(printf '%s' "$RAW_SECRET" | sha256sum | awk '{print $1}')"

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

verify_core() {
  ssh_guest "sudo systemctl is-active postgresql.service genos-product-api.service genos-runtime.service genos-worker.service genos-mcp.service genos-mission-control.service"
  ssh_guest "sudo python3 - <<'PY'
import json, urllib.request
mcp=int(open('/etc/genos/mcp-port', encoding='utf-8').read().strip())
for role, port in [('product-api',17880),('runtime',17881),('mcp-hub',mcp),('mission-control',17882)]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
        payload=json.load(response)
    assert response.status == 200 and payload['status'] == 'ok' and payload['role'] == role, (role,payload)
worker=json.load(open('/var/lib/genos/worker/heartbeat.json', encoding='utf-8'))
assert worker['status'] == 'ok' and worker['role'] == 'worker', worker
PY"
  ssh_guest "sudo -u genos psql -d genos -tAc 'SELECT 1' | grep -qx 1"
}

echo "==> Prepare pinned Ubuntu 24.04 fresh host"
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
instance-id: genos-mvp11-${TESTED_SHA}
local-hostname: genos-mvp11-e2e
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
if [[ "$ready" != 1 ]]; then tail -n 200 "$SERIAL_LOG" || true; exit 1; fi
ssh_guest "sudo cloud-init status --wait"
[[ "$(ssh_guest '. /etc/os-release; printf "%s %s" "$ID" "$VERSION_ID"')" == "ubuntu 24.04" ]]
[[ "$(ssh_guest 'uname -m')" == "x86_64" ]]

echo "==> Build exact-head install release and update fixtures"
git archive --format=tar.gz --output="$RELEASE" HEAD
RELEASE_SHA="$(sha256sum "$RELEASE" | awk '{print $1}')"
GOOD_SRC="$WORK_DIR/good-src"; BAD_SRC="$WORK_DIR/bad-src"
mkdir -p "$GOOD_SRC" "$BAD_SRC"
git archive HEAD | tar -x -C "$GOOD_SRC"
cp -a "$GOOD_SRC/." "$BAD_SRC/"
echo "MVP-11 compatible update fixture" > "$GOOD_SRC/MVP11_UPDATE_MARKER"
cat > "$BAD_SRC/src/genos/core_service.py" <<'PY'
from __future__ import annotations
raise RuntimeError("MVP11_BAD_UPDATE_FIXTURE")
PY
tar -czf "$GOOD_RELEASE" -C "$GOOD_SRC" .
tar -czf "$BAD_RELEASE" -C "$BAD_SRC" .
GOOD_RELEASE_SHA="$(sha256sum "$GOOD_RELEASE" | awk '{print $1}')"
BAD_RELEASE_SHA="$(sha256sum "$BAD_RELEASE" | awk '{print $1}')"
scp_guest "$RELEASE" "$GOOD_RELEASE" "$BAD_RELEASE" scripts/bootstrap.sh "$SSH_USER@127.0.0.1:/tmp/"
ssh_guest "chmod 700 /tmp/bootstrap.sh"

echo "==> Verified one-command post-download bootstrap"
ssh_guest "sudo /tmp/bootstrap.sh --release /tmp/genos-release.tar.gz --sha256 $RELEASE_SHA --git-sha $TESTED_SHA" | tee "$WORK_DIR/install.log"
FIRST_INSTANCE="$(ssh_guest 'sudo cat /etc/genos/instance-id')"
FIRST_MCP_PORT="$(ssh_guest 'sudo cat /etc/genos/mcp-port')"
FIRST_BOOT="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
verify_core

echo "==> Seed representative Product/Agent/MCP/SecretRef durable state"
cat > "$WORK_DIR/seed.py" <<'PY'
from pathlib import Path
import json, sys
from genos.agent_runtime import AgentRuntimeStore
from genos.auth_service import CredentialService
from genos.kanban import build_kanban_system
from genos.mcp_store import PostgresMcpStore
from genos.product_store import PostgresProductStore
from genos.secret_provider import LocalFileSecretProvider
raw=sys.stdin.readline().rstrip('\n')
instance=Path('/etc/genos/instance-id').read_text(encoding='utf-8').strip()
store=PostgresProductStore(); store.ensure_schema()
credentials=CredentialService(store, LocalFileSecretProvider('/var/lib/genos/secrets'))
cred=credentials.add(name='mvp11-lifecycle-fixture', provider_name='fixture', raw_secret=raw, consumer_scopes=['agy-gen'])
kanban=build_kanban_system(product_store=store, credentials=credentials)
card=kanban.create_card(title='MVP-11 lifecycle persistence', description='Must survive backup/restore/uninstall/reinstall.')
agent=AgentRuntimeStore('/var/lib/genos/agents/agy-gen'); agent.ensure_seed(instance_id=instance)
rev=agent.append_revision('memory','mvp11-lifecycle','revision-one',source='mvp11-e2e')
mcp=PostgresMcpStore(store); mcp.ensure_schema(); issued=mcp.create_principal(name='mvp11-external-agent', scopes=['genos.status'])
print(json.dumps({'secret_id':cred['secret_id'],'card_id':card['card_id'],'principal_id':issued.principal['principal_id'],'mcp_token':issued.access_token,'revision':rev['revision']}))
PY
scp_guest "$WORK_DIR/seed.py" "$SSH_USER@127.0.0.1:/tmp/mvp11-seed.py"
SEED_JSON="$(printf '%s\n' "$RAW_SECRET" | ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /tmp/mvp11-seed.py")"
SECRET_ID="$(printf '%s' "$SEED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret_id"])')"
CARD_ID="$(printf '%s' "$SEED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["card_id"])')"
PRINCIPAL_ID="$(printf '%s' "$SEED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["principal_id"])')"
MCP_TOKEN="$(printf '%s' "$SEED_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mcp_token"])')"
unset SEED_JSON

echo "==> Backup Product DB/state/config without raw SecretProvider material"
BACKUP_JSON="$(ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos backup --output /tmp/genos-backup.tar.gz --json")"
BACKUP_SHA="$(printf '%s' "$BACKUP_JSON" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["state"]=="SUCCEEDED" and p["include_secrets"] is False; print(p["sha256"])')"
ssh_guest "sudo test \$(stat -c %a /tmp/genos-backup.tar.gz) = 600"

echo "==> Mutate authority after backup"
ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 -m genos kanban transition --card-id $CARD_ID --to PROCESS --reason MVP11_MUTATION --json" >/dev/null
ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 -m genos mcp principal-revoke --principal-id $PRINCIPAL_ID --json" >/dev/null
cat > "$WORK_DIR/mutate-agent.py" <<'PY'
from genos.agent_runtime import AgentRuntimeStore
AgentRuntimeStore('/var/lib/genos/agents/agy-gen').append_revision('memory','mvp11-lifecycle','revision-two-after-backup',source='mvp11-e2e')
PY
scp_guest "$WORK_DIR/mutate-agent.py" "$SSH_USER@127.0.0.1:/tmp/mvp11-mutate-agent.py"
ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 /tmp/mvp11-mutate-agent.py"

echo "==> Restore and verify Product DB/state rollback while SecretProvider remains"
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos restore --archive /tmp/genos-backup.tar.gz --sha256 $BACKUP_SHA --json" | tee "$WORK_DIR/restore.json"
verify_core
[[ "$(ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT status FROM card WHERE card_id='$CARD_ID'\"" | xargs)" == "BACKLOG" ]]
[[ "$(ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT status FROM mcp_principal WHERE principal_id='$PRINCIPAL_ID'\"" | xargs)" == "ACTIVE" ]]
REV_COUNT="$(ssh_guest "sudo -u genos env HOME=/var/lib/genos PYTHONPATH=/opt/genos/current/src python3 -c \"from genos.agent_runtime import AgentRuntimeStore; print(len(AgentRuntimeStore('/var/lib/genos/agents/agy-gen').list_revisions('memory','mvp11-lifecycle')))\"")"
[[ "$REV_COUNT" == "1" ]]
REMOTE_SECRET_SHA="$(ssh_guest "sudo python3 -c \"import hashlib; print(hashlib.sha256(open('/var/lib/genos/secrets/$SECRET_ID/1.secret','rb').read()).hexdigest())\"")"
[[ "$REMOTE_SECRET_SHA" == "$RAW_SECRET_SHA" ]]

echo "==> Compatible update and release identity convergence"
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos update --release /tmp/genos-good-update.tar.gz --release-sha256 $GOOD_RELEASE_SHA --git-sha $GOOD_SHA --json" | tee "$WORK_DIR/update-good.json"
verify_core
[[ "$(ssh_guest 'readlink -f /opt/genos/current')" == "/opt/genos/releases/$GOOD_SHA" ]]
ssh_guest "sudo grep -qx 'GENOS_RELEASE_SHA=$GOOD_SHA' /etc/genos/genos.env"
[[ "$(ssh_guest "sudo python3 -c \"import json; print(json.load(open('/var/lib/genos/manifest.json'))['release']['git_sha'])\"")" == "$GOOD_SHA" ]]

echo "==> Broken update must restore release + DB/state checkpoint"
set +e
BAD_OUTPUT="$(ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos update --release /tmp/genos-bad-update.tar.gz --release-sha256 $BAD_RELEASE_SHA --git-sha $BAD_SHA --json" 2>&1)"
BAD_RC=$?
set -e
if [[ "$BAD_RC" -eq 0 ]]; then echo "broken update unexpectedly succeeded" >&2; exit 1; fi
printf '%s\n' "$BAD_OUTPUT" | grep -q 'previous release and checkpoint were restored'
verify_core
[[ "$(ssh_guest 'readlink -f /opt/genos/current')" == "/opt/genos/releases/$GOOD_SHA" ]]
ssh_guest "sudo grep -qx 'GENOS_RELEASE_SHA=$GOOD_SHA' /etc/genos/genos.env"
[[ "$(ssh_guest "sudo python3 -c \"import json; print(json.load(open('/var/lib/genos/manifest.json'))['release']['git_sha'])\"")" == "$GOOD_SHA" ]]
[[ "$(ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT status FROM card WHERE card_id='$CARD_ID'\"" | xargs)" == "BACKLOG" ]]

echo "==> Redacted support bundle"
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos support-bundle --output /tmp/genos-support.tar.gz --json" | tee "$WORK_DIR/support.json"
ssh_guest "sudo cp /tmp/genos-support.tar.gz /home/$SSH_USER/genos-support.tar.gz && sudo chown $SSH_USER:$SSH_USER /home/$SSH_USER/genos-support.tar.gz"
scp_guest "$SSH_USER@127.0.0.1:/home/$SSH_USER/genos-support.tar.gz" "$SUPPORT_LOCAL"
if grep -aFq "$RAW_SECRET" "$SUPPORT_LOCAL"; then echo "raw SecretProvider value leaked into support bundle" >&2; exit 1; fi
if grep -aFq "$MCP_TOKEN" "$SUPPORT_LOCAL"; then echo "raw MCP token leaked into support bundle" >&2; exit 1; fi

echo "==> Uninstall preserves durable data, secrets, Product DB and reinstall identity"
ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos uninstall --json" | tee "$WORK_DIR/uninstall.json"
ssh_guest "sudo test -d /var/lib/genos && sudo test -f /var/lib/genos/secrets/$SECRET_ID/1.secret"
ssh_guest "sudo test -f /var/lib/genos/uninstall-preserved-config/instance-id && sudo test -f /var/lib/genos/uninstall-preserved-config/mcp-port"
ssh_guest "test ! -e /opt/genos/current && test ! -d /etc/genos"
ssh_guest "sudo -u genos psql -d genos -tAc 'SELECT 1' | grep -qx 1"

echo "==> Reinstall through same verified bootstrap must reuse instance and MCP endpoint"
ssh_guest "sudo /tmp/bootstrap.sh --release /tmp/genos-release.tar.gz --sha256 $RELEASE_SHA --git-sha $TESTED_SHA" | tee "$WORK_DIR/reinstall.log"
verify_core
[[ "$(ssh_guest 'sudo cat /etc/genos/instance-id')" == "$FIRST_INSTANCE" ]]
[[ "$(ssh_guest 'sudo cat /etc/genos/mcp-port')" == "$FIRST_MCP_PORT" ]]
[[ "$(ssh_guest "sudo -u genos psql -d genos -tAc \"SELECT status FROM card WHERE card_id='$CARD_ID'\"" | xargs)" == "BACKLOG" ]]
REMOTE_SECRET_SHA="$(ssh_guest "sudo python3 -c \"import hashlib; print(hashlib.sha256(open('/var/lib/genos/secrets/$SECRET_ID/1.secret','rb').read()).hexdigest())\"")"
[[ "$REMOTE_SECRET_SHA" == "$RAW_SECRET_SHA" ]]

echo "==> Reboot recovery after reinstall"
ssh_guest "sudo systemctl reboot" >/dev/null 2>&1 || true
sleep 8
rebooted=0
for _ in $(seq 1 180); do
  if ssh_guest true >/dev/null 2>&1; then
    NEW_BOOT="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
    if [[ -n "$NEW_BOOT" && "$NEW_BOOT" != "$FIRST_BOOT" ]]; then rebooted=1; break; fi
  fi
  sleep 3
done
[[ "$rebooted" == 1 ]]
verify_core
[[ "$(ssh_guest 'sudo cat /etc/genos/instance-id')" == "$FIRST_INSTANCE" ]]
[[ "$(ssh_guest 'sudo cat /etc/genos/mcp-port')" == "$FIRST_MCP_PORT" ]]

echo "==> Explicit purge deletes local GenOS + Product DB only"
PURGE_JSON="$(ssh_guest "sudo env PYTHONPATH=/opt/genos/current/src python3 -m genos purge --confirm-instance-id $FIRST_INSTANCE --json")"
printf '%s' "$PURGE_JSON" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["state"]=="PURGED" and p["product_database_deleted"] is True and p["remote_resources_deleted"] is False'
ssh_guest "test ! -e /var/lib/genos && test ! -e /etc/genos && test ! -e /opt/genos"
[[ -z "$(ssh_guest "sudo -u postgres psql -tAc \"SELECT 1 FROM pg_database WHERE datname='genos'\"" | xargs)" ]]
[[ -z "$(ssh_guest "sudo -u postgres psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='genos'\"" | xargs)" ]]

python3 - "$EVIDENCE" <<PY
import json, sys
payload = {
  "schema_version": "1.0",
  "tested_git_sha": "$TESTED_SHA",
  "image_sha256": "$IMAGE_SHA256",
  "instance_id": "$FIRST_INSTANCE",
  "mcp_port": int("$FIRST_MCP_PORT"),
  "verified_bootstrap_install": "PASS",
  "backup_restore_product_state": "PASS",
  "default_restore_secret_preservation": "PASS",
  "compatible_update": "PASS",
  "failed_update_release_state_db_rollback": "PASS",
  "support_bundle_redaction": "PASS",
  "uninstall_preserve_data": "PASS",
  "reinstall_preserve_instance_and_mcp_endpoint": "PASS",
  "reboot_recovery": "PASS",
  "purge_local_product_db_only": "PASS"
}
open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(payload, indent=2, sort_keys=True)+'\n')
print(json.dumps(payload, indent=2, sort_keys=True))
PY

echo "GENOS_MVP11_LIFECYCLE_FRESH_HOST_PASS"
