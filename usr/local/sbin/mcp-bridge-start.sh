#!/bin/bash
# mcp-bridge-start: small systemd ExecStart wrapper. sops-decrypts the
# secret into a tmpfs file under /run/secrets/, sources it, then exec's
# the bridge so the env vars cross the process boundary cleanly.
set -eu
export SOPS_AGE_KEY_FILE="${HOMELAB_SOPS_KEY:-/root/.sops/age.key}"
ENV_DIR="/run/secrets"
mkdir -p "$ENV_DIR"; chmod 700 "$ENV_DIR"
ENV_FILE="$ENV_DIR/mcp-bridge-hmac.env"
sops --decrypt /etc/secrets/mcp-bridge-hmac.sops > "$ENV_FILE"
chmod 600 "$ENV_FILE"
set -a
. "$ENV_FILE"
set +a
export MCP_BRIDGE_PORT="${MCP_BRIDGE_PORT:-8000}"
export MCP_BRIDGE_LOG="${MCP_BRIDGE_LOG:-/var/log/mcp-bridge/inbound.jsonl}"
exec /usr/bin/python3 /opt/mcp-bridge/bridge.py