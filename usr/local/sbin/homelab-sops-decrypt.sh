#!/bin/bash
# homelab-sops-decrypt: small helper that decrypts a sops file from /etc/secrets
# into /run/secrets/<name>.env, so systemd EnvironmentFile= can load it.
#
# Usage: homelab-sops-decrypt.sh <name>
#   name: short identifier (maps to /etc/secrets/<name>.sops,
#                             writes to /run/secrets/<name>.env)
#
# Used from systemd ExecStartPre=. Secret handling here is the same shape as
# mcp-bridge.env / homelab-cli-secret.env plaintext files we had before.
# Replacement is right: only the on-disk shape changed (encrypted at rest).

set -eu
NAME="$1"
if [[ -z "$NAME" ]]; then
  echo "homelab-sops-decrypt.sh: usage homelab-sops-decrypt.sh <name>" >&2
  exit 2
fi

KEY="${HOMELAB_SOPS_KEY:-/root/.sops/age.key}"
SRC="/etc/secrets/${NAME}.sops"
DST_DIR="/run/secrets"
DST="${DST_DIR}/${NAME}.env"

mkdir -p "${DST_DIR}"
# SOPS reads the age private key from SOPS_AGE_KEY_FILE; we wire the
# Environment=HOMELAB_SOPS_KEY=/root/.sops/age.key from the systemd unit.
if [[ -n "${KEY:-}" ]]; then export SOPS_AGE_KEY_FILE="${KEY}"; fi
sops --decrypt "${SRC}" > "${DST}.tmp"
mv "${DST}.tmp" "${DST}"
chmod 600 "${DST}"
echo "homelab-sops-decrypt: decrypted ${SRC} -> ${DST}"