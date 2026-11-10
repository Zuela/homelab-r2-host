#!/usr/bin/env bash
# PBS backup for studio role datasets (not secrets; skips huge mod-comfy by default).
set -euo pipefail

PBS_FP="37:f2:8c:7c:3a:ac:2b:d3:8d:4c:84:8a:b1:21:b8:61:16:86:8c:88:e2:b9:f5:a3:12:85:5a:b8:00:16:ed:1c"
PBS_REPO="r2-backup@pbs!r2-token@192.168.5.10:r2-configs"
PBS_HOST="192.168.5.10"
PBS_PORT="8007"
LOG=/var/log/r2-studio-datasets-backup.jsonl
BACKUP_ID="r2-studio-$(date -u +%Y%m%d-%H%M%S)"   # run label
PBS_BACKUP_ID="r2-studio"   # fixed PBS group so prune works (2026-11-07)
HOSTNAME_FQDN=$(hostname -f 2>/dev/null || hostname)

log_json() {
  printf '{"ts":"%s",%s}\n' "$(date -u +%FT%TZ)" "$1" >>"$LOG"
}

if ! timeout 3 bash -c "echo >/dev/tcp/${PBS_HOST}/${PBS_PORT}" 2>/dev/null; then
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"rc\":0,\"pbs\":\"unreachable\""
  echo "WARN: PBS unreachable; skipped studio dataset backup" >&2
  exit 0
fi

if [[ ! -r /etc/pve/priv/storage/r2-backup-target.pw ]]; then
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"rc\":2,\"error\":\"missing_pbs_password_file\""
  exit 2
fi

export PBS_PASSWORD PBS_REPOSITORY PBS_FINGERPRINT
PBS_PASSWORD=$(cat /etc/pve/priv/storage/r2-backup-target.pw)
PBS_REPOSITORY="$PBS_REPO"
PBS_FINGERPRINT="$PBS_FP"

log_json "\"phase\":\"start\",\"backup_id\":\"$BACKUP_ID\",\"host\":\"$HOSTNAME_FQDN\""

set +e
OUT=$(proxmox-backup-client backup \
  --backup-type host \
  --backup-id "$PBS_BACKUP_ID" \
  --change-detection-mode metadata \
  "studio-homelab.pxar:/tank/studio-homelab" \
  2>&1)
RC=$?
set -e

OUT_JSON=$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[-2000:]))')
if [[ $RC -eq 0 ]]; then
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"rc\":0,\"pbs\":\"ok\",\"detail\":$OUT_JSON"
  echo "PBS studio datasets ok: backup_id=$BACKUP_ID"
else
  log_json "\"phase\":\"end\",\"backup_id\":\"$BACKUP_ID\",\"rc\":$RC,\"pbs\":\"fail\",\"detail\":$OUT_JSON"
  echo "PBS studio datasets failed rc=$RC" >&2
  exit "$RC"
fi
