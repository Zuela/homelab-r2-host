#!/usr/bin/env bash
# Stage and ship r2 host secrets + config to PBS (datastore r2-configs).
# Always keeps a local dated archive even if PBS is unreachable.
# Schedule: homelab-r2-secrets-backup.timer
set -euo pipefail

PBS_FP="37:f2:8c:7c:3a:ac:2b:d3:8d:4c:84:8a:b1:21:b8:61:16:86:8c:88:e2:b9:f5:a3:12:85:5a:b8:00:16:ed:1c"
PBS_REPO="r2-backup@pbs!r2-token@192.168.5.10:r2-configs"
PBS_HOST="192.168.5.10"
PBS_PORT="8007"
STAGE_ROOT=/var/backups/r2-secrets
CURRENT="$STAGE_ROOT/current"
ARCHIVES="$STAGE_ROOT/archives"
LOG=/var/log/r2-secrets-backup.jsonl
HOSTNAME_FQDN=$(hostname -f 2>/dev/null || hostname)
TS=$(date -u +%FT%TZ)
BACKUP_ID="r2-secrets-$(date -u +%Y%m%d-%H%M%S)"   # local archive name
PBS_BACKUP_ID="r2-secrets"   # fixed PBS group so prune works (2026-11-07)
LOCAL_KEEP=14

umask 077
mkdir -p "$CURRENT" "$ARCHIVES"
# rebuild current staging only
find "$CURRENT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

log_json() {
  # usage: log_json '{"phase":"x",...}'  (must be valid single-line JSON object body fields)
  printf '{"ts":"%s",%s}\n' "$(date -u +%FT%TZ)" "$1" >>"$LOG"
}

collect() {
  local src="$1" dst="$2"
  if [[ -r "$src" ]]; then
    install -D -m "$(stat -c %a "$src")" -o root -g root "$src" "$dst"
  else
    printf 'WARN missing=%s\n' "$src" >&2
  fi
}

# Encrypted secrets
collect /etc/secrets/homelab-cli-secret.sops        "$CURRENT/etc/secrets/homelab-cli-secret.sops"
collect /etc/secrets/mcp-bridge-hmac.sops            "$CURRENT/etc/secrets/mcp-bridge-hmac.sops"
# SOPS identity
collect /root/.sops/age.key                          "$CURRENT/root/.sops/age.key"
collect /root/.sops/age.pub                          "$CURRENT/root/.sops/age.pub"
# Plaintext local copies (PBS transport is TLS; local archives are mode 0700 dir)
collect /root/.creds/homelab-cli-secret              "$CURRENT/root/creds/homelab-cli-secret"
collect /root/.creds/mcp-bridge-hmac                 "$CURRENT/root/creds/mcp-bridge-hmac"
collect /root/.creds/matrix-namespace-rooms.json     "$CURRENT/root/creds/matrix-namespace-rooms.json"
collect /root/.creds/matrix-test-roundtrip.json      "$CURRENT/root/creds/matrix-test-roundtrip.json"
# Config / ops docs
collect /etc/samba/smb.conf                          "$CURRENT/etc/samba/smb.conf"
collect /var/log/homelab-cli.jsonl                   "$CURRENT/var/log/homelab-cli.jsonl"
collect /etc/systemd/system/homelab-cli.service      "$CURRENT/etc/systemd/system/homelab-cli.service"
collect /usr/local/sbin/homelab-cli.py               "$CURRENT/usr/local/sbin/homelab-cli.py"
collect /etc/systemd/system/homelab-r2-ollama-nat-proxy.service "$CURRENT/etc/systemd/system/homelab-r2-ollama-nat-proxy.service"
collect /usr/local/sbin/ollama-on-r2-nat-proxy.sh    "$CURRENT/usr/local/sbin/ollama-on-r2-nat-proxy.sh"
collect /root/homelab-integrations.md                "$CURRENT/root/homelab-integrations.md"
collect /tmp/homelab-infra/bin/homelab-infra-sync.py "$CURRENT/repo/bin/homelab-infra-sync.py" 2>/dev/null || true
collect /tmp/homelab-infra/homelab/infra/mcp-bridge/bridge.py "$CURRENT/repo/mcp-bridge/bridge.py" 2>/dev/null || true
collect /tmp/homelab-infra/homelab/infra/mcp-bridge/mcp-bridge.service "$CURRENT/repo/mcp-bridge/mcp-bridge.service" 2>/dev/null || true

( cd "$CURRENT" && find . -type f -print0 | xargs -0 sha256sum | sort -k 2 ) > "$CURRENT/.SHA256SUMS"
chmod 0600 "$CURRENT/.SHA256SUMS"

# Local dated archive (survives PBS outage)
ARCHIVE_DIR="$ARCHIVES/$BACKUP_ID"
mkdir -p "$ARCHIVE_DIR"
cp -a "$CURRENT"/. "$ARCHIVE_DIR"/
# prune local archives
ls -1dt "$ARCHIVES"/r2-secrets-* 2>/dev/null | tail -n +"$((LOCAL_KEEP+1))" | xargs -r rm -rf --

BYTES=$(find "$CURRENT" -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}')
log_json "\"phase\":\"start\",\"backup_id\":\"$BACKUP_ID\",\"host\":\"$HOSTNAME_FQDN\",\"bytes\":$BYTES,\"local_archive\":\"$ARCHIVE_DIR\""

# PBS connectivity preflight
PBS_REACHABLE=0
if timeout 3 bash -c "echo >/dev/tcp/${PBS_HOST}/${PBS_PORT}" 2>/dev/null; then
  PBS_REACHABLE=1
fi

if [[ "$PBS_REACHABLE" -ne 1 ]]; then
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"host\":\"$HOSTNAME_FQDN\",\"rc\":0,\"pbs\":\"unreachable\",\"detail\":\"${PBS_HOST}:${PBS_PORT} no route/connect; local archive kept\""
  echo "WARN: PBS ${PBS_HOST}:${PBS_PORT} unreachable; kept local archive $ARCHIVE_DIR" >&2
  # soft success so timer does not flap red every night while PBS host is down
  exit 0
fi

if [[ -r /etc/pve/priv/storage/r2-backup-target.pw ]]; then
  export PBS_PASSWORD
  PBS_PASSWORD=$(cat /etc/pve/priv/storage/r2-backup-target.pw)
else
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"host\":\"$HOSTNAME_FQDN\",\"rc\":2,\"error\":\"missing_pbs_password_file\""
  echo "FATAL: PBS password file missing" >&2
  exit 2
fi

export PBS_REPOSITORY="$PBS_REPO"
export PBS_FINGERPRINT="$PBS_FP"

set +e
OUT=$(proxmox-backup-client backup \
  --backup-type host \
  --backup-id "$PBS_BACKUP_ID" \
  --change-detection-mode metadata \
  "host-r2-secrets.pxar:$CURRENT" 2>&1)
RC=$?
set -e

# sanitize OUT for JSON string
OUT_JSON=$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[-2000:]))')
log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"host\":\"$HOSTNAME_FQDN\",\"rc\":$RC,\"pbs\":\"ok\",\"detail\":$OUT_JSON"

if [[ $RC -ne 0 ]]; then
  echo "PBS backup rc=$RC (local archive still at $ARCHIVE_DIR)" >&2
  exit "$RC"
fi
echo "PBS backup ok: backup_id=$BACKUP_ID"
