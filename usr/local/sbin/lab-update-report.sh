#!/bin/bash
# lab-update-report (2026-11-10). Weekly, cron Mon 08:00 local (/etc/cron.d/lab-update-report).
# Collects pending apt updates (security flagged) for r2 and every running CT, and checks each
# running Docker container's pinned image against the current digest of its tag.
# Sends JSON to the n8n webhook "update-report", which posts the summary to #homelab-alerts.
# Read-only apart from `apt-get update` (package lists). Secret: /root/.creds/n8n-update-report.secret
set -uo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WEBHOOK="http://192.168.4.54:5678/webhook/update-report"
SECRET_FILE=/root/.creds/n8n-update-report.secret
LOG=/var/log/lab-update-report.log
OUT=/var/lib/lab-update-report/last.json
mkdir -p "$(dirname "$OUT")"

apt_counts() {  # prints "total security"
  "$@" sh -c 'apt-get update -qq >/dev/null 2>&1; apt list --upgradable 2>/dev/null | grep -c upgradable; apt list --upgradable 2>/dev/null | grep -c -- "-security"' 2>/dev/null | tr '\n' ' '
}

# Image check, run inside a CT: one line per container "name|tag|status"
IMG_CHECK='command -v docker >/dev/null || exit 0
for c in $(docker ps --format "{{.Names}}"); do
  img=$(docker inspect -f "{{.Config.Image}}" "$c")
  case "$img" in
    *@sha256:*) tag=${img%@*}; pinned=${img#*@} ;;
    *) tag=$img; pinned=$(docker image inspect -f "{{join .RepoDigests \" \"}}" "$(docker inspect -f "{{.Image}}" "$c")" 2>/dev/null | tr " " "\n" | head -1 | sed "s/.*@//") ;;
  esac
  last=${tag##*/}
  case "$last" in *:*) ;; *) echo "$c|$tag|digest-only"; continue ;; esac
  remote=$(timeout 30 docker buildx imagetools inspect "$tag" 2>/dev/null | awk "/^Digest:/{print \$2; exit}")
  if [ -z "$remote" ]; then st=unknown; elif [ "$remote" = "$pinned" ]; then st=current; else st=newer; fi
  echo "$c|$tag|$st"
done'

json_items=()
read -r ht hs <<<"$(apt_counts)"
json_items+=("{\"name\":\"r2 (host)\",\"apt\":${ht:-0},\"security\":${hs:-0},\"images\":[]}")

for ct in $(pct list | awk 'NR>1 && $2=="running"{print $1}'); do
  name=$(pct config "$ct" | awk '/^hostname:/{print $2}')
  read -r t s <<<"$(apt_counts pct exec "$ct" --)"
  imgs=$(pct exec "$ct" -- sh -c "$IMG_CHECK" 2>/dev/null | python3 -c '
import sys,json
print(json.dumps([dict(zip(["container","tag","status"],l.strip().split("|",2))) for l in sys.stdin if l.count("|")==2]))')
  json_items+=("{\"name\":\"$ct $name\",\"apt\":${t:-0},\"security\":${s:-0},\"images\":${imgs:-[]}}")
done

body="{\"generated\":\"$(date -u +%FT%TZ)\",\"items\":[$(IFS=,; echo "${json_items[*]}")]}"
printf '%s\n' "$body" > "$OUT"
if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$OUT"; then
  echo "$(date -u +%FT%TZ) invalid json, not sent" >> "$LOG"; exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST "$WEBHOOK" \
  -H "Content-Type: application/json" -H "X-Report-Secret: $(cat "$SECRET_FILE")" --data-binary @"$OUT")
echo "$(date -u +%FT%TZ) update-report sent http=$code" >> "$LOG"
[ "$code" = 200 ]
