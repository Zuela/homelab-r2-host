#!/bin/bash
# homelab-cli-start: small systemd ExecStart wrapper. sops-decrypts the
# secret into a tmpfs file under /run/secrets/ and points the daemon at
# it via HOMELAB_CLI_SECRET_PATH.
set -eu
export SOPS_AGE_KEY_FILE="${HOMELAB_SOPS_KEY:-/root/.sops/age.key}"
ENV_DIR="/run/secrets"
mkdir -p "$ENV_DIR"; chmod 700 "$ENV_DIR"
ENV_FILE="$ENV_DIR/homelab-cli-secret.env"
sops --decrypt /etc/secrets/homelab-cli-secret.sops > "$ENV_FILE"
chmod 600 "$ENV_FILE"
export HOMELAB_CLI_SECRET_PATH="$ENV_FILE"
export HOMELAB_CLI_BIND_HOSTS="${HOMELAB_CLI_BIND_HOSTS:-192.168.5.11,100.91.126.26}"
export HOMELAB_CLI_TS_WINDOW_S="${HOMELAB_CLI_TS_WINDOW_S:-60}"
exec /usr/bin/python3 /usr/local/sbin/homelab-cli.py