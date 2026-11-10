#!/bin/bash
# r2 LXC-level DNAT proxy: 192.168.5.11:11434 -> 192.168.4.35:11434
# Allows Tailscale-routed Hermes (CT 212 hermes) to reach the LAN Ollama host
# whose iptables allowlist does not include CT 212's LAN IP.
set -eu

PROXY_IP="192.168.5.11"
OLLAMA_IP="192.168.4.35"
OLLAMA_PORT="11434"

cmd="${1:-start}"

add_rules() {
  sysctl -w net.ipv4.ip_forward=1 >/dev/null || true
  iptables -t nat -C PREROUTING  -d "$PROXY_IP"  -p tcp --dport "$OLLAMA_PORT" \
    -j DNAT --to-destination "$OLLAMA_IP:$OLLAMA_PORT" 2>/dev/null \
    || iptables -t nat -A PREROUTING  -d "$PROXY_IP"  -p tcp --dport "$OLLAMA_PORT" \
       -j DNAT --to-destination "$OLLAMA_IP:$OLLAMA_PORT"
  iptables -t nat -C POSTROUTING -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" \
    -j SNAT --to-source "$PROXY_IP" 2>/dev/null \
    || iptables -t nat -A POSTROUTING -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" \
       -j SNAT --to-source "$PROXY_IP"
  iptables -C FORWARD -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" -j ACCEPT 2>/dev/null \
    || iptables -I FORWARD 1 -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" -j ACCEPT
  iptables -C FORWARD -s "$OLLAMA_IP" -p tcp --sport "$OLLAMA_PORT" -j ACCEPT 2>/dev/null \
    || iptables -I FORWARD 1 -s "$OLLAMA_IP" -p tcp --sport "$OLLAMA_PORT" -j ACCEPT
}

del_rules() {
  iptables -t nat -D PREROUTING  -d "$PROXY_IP"  -p tcp --dport "$OLLAMA_PORT" \
    -j DNAT --to-destination "$OLLAMA_IP:$OLLAMA_PORT" 2>/dev/null || true
  iptables -t nat -D POSTROUTING -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" \
    -j SNAT --to-source "$PROXY_IP" 2>/dev/null || true
  iptables -D FORWARD -d "$OLLAMA_IP" -p tcp --dport "$OLLAMA_PORT" -j ACCEPT 2>/dev/null || true
  iptables -D FORWARD -s "$OLLAMA_IP" -p tcp --sport "$OLLAMA_PORT" -j ACCEPT 2>/dev/null || true
}

case "$cmd" in
  start|reload|force-reload) add_rules ;;
  stop|cleanup) del_rules ;;
  status)
    echo "---nat---"
    iptables -t nat -S PREROUTING  | grep "$OLLAMA_PORT" || echo "no PREROUTING rule"
    iptables -t nat -S POSTROUTING | grep "$OLLAMA_PORT" || echo "no POSTROUTING rule"
    echo "---forward---"
    iptables -S FORWARD | grep "$OLLAMA_PORT" || echo "no FORWARD rules"
    ;;
  *)
    echo "Usage: $0 {start|stop|status}"; exit 2;;
esac
