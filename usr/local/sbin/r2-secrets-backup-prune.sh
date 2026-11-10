#!/usr/bin/env bash
# Trim PBS snapshots for r2-secrets backups.
# Soft-succeeds when PBS is unreachable or no snapshots exist yet.
set -euo pipefail

PBS_FP="37:f2:8c:7c:3a:ac:2b:d3:8d:4c:84:8a:b1:21:b8:61:16:86:8c:88:e2:b9:f5:a3:12:85:5a:b8:00:16:ed:1c"
PBS_REPO="r2-backup@pbs!r2-token@192.168.5.10:r2-configs"
PBS_HOST="192.168.5.10"
PBS_PORT="8007"
LOG=/var/log/r2-secrets-backup.jsonl
ARCHIVES=/var/backups/r2-secrets/archives
LOCAL_KEEP=14

log_json() {
  printf '{"ts":"%s",%s}\n' "$(date -u +%FT%TZ)" "$1" >>"$LOG"
}

# Always prune local archives first (independent of PBS)
if [[ -d "$ARCHIVES" ]]; then
  DELETED=$(ls -1dt "$ARCHIVES"/r2-secrets-* 2>/dev/null | tail -n +"$((LOCAL_KEEP+1))" | tee /tmp/r2-secrets-local-prune.list | wc -l)
  xargs -r rm -rf -- < /tmp/r2-secrets-local-prune.list || true
  log_json "\"phase\":\"local_prune\",\"kept\":$LOCAL_KEEP,\"deleted\":$DELETED"
fi

if ! timeout 3 bash -c "echo >/dev/tcp/${PBS_HOST}/${PBS_PORT}" 2>/dev/null; then
  log_json "\"phase\":\"prune\",\"rc\":0,\"pbs\":\"unreachable\",\"detail\":\"${PBS_HOST}:${PBS_PORT} unreachable; skipped PBS prune\""
  echo "WARN: PBS unreachable; skipped remote prune" >&2
  exit 0
fi

if [[ -r /etc/pve/priv/storage/r2-backup-target.pw ]]; then
  export PBS_PASSWORD
  PBS_PASSWORD=$(cat /etc/pve/priv/storage/r2-backup-target.pw)
else
  log_json "\"phase\":\"prune\",\"rc\":2,\"error\":\"missing_pbs_password_file\""
  echo "FATAL: PBS password file missing" >&2
  exit 2
fi

export PBS_REPOSITORY="$PBS_REPO"
export PBS_FINGERPRINT="$PBS_FP"

set +e
LIST_OUT=$(proxmox-backup-client snapshot list --output-format json 2>&1)
LIST_RC=$?
set -e

if [[ $LIST_RC -ne 0 ]]; then
  DETAIL=$(printf '%s' "$LIST_OUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[-1500:]))')
  log_json "\"phase\":\"prune\",\"rc\":0,\"pbs\":\"list_failed\",\"detail\":$DETAIL"
  echo "WARN: PBS snapshot list failed rc=$LIST_RC; skipped prune" >&2
  exit 0
fi

# 2026-11-07: backups now use fixed PBS groups (host/r2-secrets, host/r2-studio),
# so prune each group with the retention policy. Old timestamped groups
# (host/r2-secrets-YYYYMMDD-..., host/r2-studio-...) were cleaned up once by hand.
RC_ALL=0
for GROUP in host/r2-secrets host/r2-studio; do
  if ! printf "%s" "$LIST_OUT" | grep -Eq "\"backup-id\": *\"${GROUP#host/}\""; then
    log_json "\"phase\":\"prune\",\"rc\":0,\"group\":\"$GROUP\",\"detail\":{\"status\":\"no_snapshots_yet\"}"
    echo "PBS prune: $GROUP has no snapshots yet (ok)"
    continue
  fi
  echo "[+] pruning group=${GROUP}"
  set +e
  RES=$(proxmox-backup-client prune "$GROUP" \
    --keep-last 14 --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-yearly 2 \
    --output-format json 2>&1)
  RC=$?
  set -e
  RES_JSON=$(printf "%s" "$RES" | python3 -c "import json,sys
raw=sys.stdin.read()
try:
  json.loads(raw); print(raw)
except Exception:
  print(json.dumps({\"raw\": raw[-1500:], \"parse_error\": True}))")
  log_json "\"phase\":\"prune\",\"rc\":$RC,\"keep_last\":14,\"keep_daily\":7,\"keep_weekly\":4,\"keep_monthly\":6,\"keep_yearly\":2,\"group\":\"$GROUP\",\"detail\":$RES_JSON"
  if [[ $RC -ne 0 ]]; then echo "PBS prune rc=$RC group=$GROUP" >&2; RC_ALL=$RC; fi
done
[[ $RC_ALL -eq 0 ]] && echo "PBS prune ok"
exit "$RC_ALL"
