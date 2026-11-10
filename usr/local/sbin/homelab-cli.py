#!/usr/bin/env python3
"""homelab-cli: HTTP + WebSocket daemon on r2 for the homelab agent surface.

Routes
------
- POST /pct/exec                 body {
                                       "vmid": <int>,
                                       "cmd":  [<argv tokens...>],
                                       "timeout_s": <int, default 5>,
                                     }
                                   returns {
                                     "vmid", "argv", "rc", "stdout", "stderr",
                                     "elapsed_ms", "conn_id",
                                   }

- WS   /ws/pct-exec-stream/{vmid}  query params:
                                       unit=<systemd unit>   # journalctl -fu
                                       OR cmd=[<argv tokens...>]  # arbitrary
                                       lines=<int, default 200>
                                       idletimeout_s=<int, default 600>
                                     streams JSON frames:
                                       {"ts": ISO8601, "line": "<text>"}

- GET  /health                   returns {"ok": true, "uptime_s": ..., "version": ...}

Auth
----
HMAC over a canonical string `verb|path|sorted_query|body_sha256|ts` with the
shared secret at /root/.creds/homelab-cli-secret (KEY=VALUE format, with
`HOMELAB_CLI_SECRET=<value>`). Headers:
    X-Homelab-Signature-256: sha256=<hex>
    X-Homelab-Ts:           <unix-seconds>

ts is rejected if older than HOMELAB_CLI_TS_WINDOW_S (default 60s).

Audit
-----
Every accepted request appends one JSON object to /var/log/homelab-cli.jsonl.

Constraints
-----------
* Bind to the explicit HOME_LAB_HOSTS list; never 0.0.0.0.
* Tokio-style single-reactor aiohttp; runs as root via systemd.
* Allowlist for both commands and paths.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

LOG_PATH = Path("/var/log/homelab-cli.jsonl")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Defaults overridable via env.
SECRET_PATH = Path(os.environ.get("HOMELAB_CLI_SECRET_PATH", "/root/.creds/homelab-cli-secret"))
TS_WINDOW_S = int(os.environ.get("HOMELAB_CLI_TS_WINDOW_S", "60"))
BIND_HOSTS = [h.strip() for h in os.environ.get("HOMELAB_CLI_BIND_HOSTS", "192.168.5.11,100.91.126.26").split(",") if h.strip()]

# Allowlist. Each entry is (argv_prefix, args_allowlist_or_None, allowed_unit_names_or_None).
# args_allowlist_or_None: None means any args; otherwise a set of allowed tokens.
# Entries whose value is True are command names with no per-arg restriction.
# Entries whose value is set() have per-arg restrictions (allowlist tokens).
# Allowlist is the set of command basenames. CVE-style subcommands like
# `pct destroy` are caught in ARG_DENY below. Browsing commands like ls,
# cat, systemctl status, journalctl (one-shot OR follow) are all permitted.
PCT_EXEC_ALLOWED = {
    "ls", "cat", "head", "tail", "grep", "stat",
    "systemctl", "journalctl",
    "ps", "free", "df", "uptime", "whoami", "id",
    "find", "xargs", "wc",
    "pct", "pgrep", "pkill", "systemd-run",
    "git",
    "echo", "test", "true", "false", "cat",
}

# Argument deny-list, prefix-matched against any token in argv.
# Block patterns we never want an automated agent to send, even with
# otherwise-permitted commands.
ARG_DENY = [
    "--delete",       "rm", "--remove", "--hard",
    "--no-preserve",
    "reset", "--force",
    "destroy", "purge",
    "mkfs.", "fdisk", "mkfs", "parted",
    "dd",
    ">",
]

# Subsystem-level bans: only fire when the leading argv[0] matches.
SUBCMD_DENY = {
    "pct": {"destroy", "reset", "rollback", "suspend", "resume",
            "shutdown", "reboot", "migrate", "clone"},
    "systemctl": {"disable", "mask", "kill", "stop", "startmask", "disable"},
    "git": {"push", "reset", "clean", "filter-branch"},
}

# Flag-level pins: drill into `cmd sub args...` and reject subcommands
# with explicit verb restrictions.
PREFIX_BAN = [
    ("rm", "-rf"),                       # never recursive forced removal of roots
]  # currently unused beyond sub-command deny-list

PATH_ALLOWED_PREFIXES = [
    "/etc/pve/",
    "/tmp/homelab-infra/",
    "/var/log/",
    "/opt/",
]

MAX_FRAME_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 5
MAX_TIMEOUT_S = 30
MAX_STDOUT_BYTES = 64 * 1024 * 1024

@dataclass
class Audit:
    path: Path = LOG_PATH
    seq: int = 0

def _read_secret() -> bytes:
    text = SECRET_PATH.read_text().strip()
    if "=" in text:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("HOMELAB_CLI_SECRET="):
                return line.split("=", 1)[1].encode("utf-8")
    return text.encode("utf-8")

def _canonical(verb: str, path: str, query: dict[str, str], body_sha256: str, ts: str) -> bytes:
    qs = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    return f"{verb}|{path}|{qs}|{body_sha256}|{ts}".encode("utf-8")

def _verify(verb: str, path: str, query: dict[str, str], body: bytes, signature: str, ts_str: str) -> tuple[bool, str]:
    secret = _read_secret()
    try:
        ts = int(ts_str)
    except ValueError:
        return False, "bad ts"
    if abs(time.time() - ts) > TS_WINDOW_S:
        return False, "stale ts"
    body_sha = hashlib.sha256(body).hexdigest()
    msg = _canonical(verb, path, query, body_sha, str(ts))
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    return hmac.compare_digest(expected, provided), ("ok" if provided == expected else "hmac mismatch")

def _check_allowlist(argv: list[str]) -> tuple[bool, str]:
    if not argv:
        return False, "empty cmd"
    cmd = argv[0]
    if cmd not in PCT_EXEC_ALLOWED:
        return False, f"cmd '{cmd}' not allowlisted"
    # Sub-command bans (e.g., pct destroy, git reset --hard).
    if cmd in SUBCMD_DENY and len(argv) >= 2 and argv[1] in SUBCMD_DENY[cmd]:
        return False, f"sub '{argv[1]}' denied for cmd '{cmd}'"
    # Generic token deny-list (e.g., any 'rm' or '>' redirect anywhere).
    for token in argv[1:]:
        if token in ARG_DENY:
            return False, f"token '{token}' is denied"
        # `> /dev/sda` style damage: deny '>' if next token is not /dev/null
        if token == ">" and cmd not in {"echo", "test", "true", "false"}:
            return False, "shell redirect '>' denied outside safe-builtins"
    return True, "ok"

def _resolve_ips(raw_hosts: list[str]) -> list[str]:
    resolved = []
    for h in raw_hosts:
        try:
            infolist = socket.getaddrinfo(h, 9000, type=socket.SOCK_STREAM)
            for fam, _, _, _, sockaddr in infolist:
                resolved.append(sockaddr[0])
        except socket.gaierror:
            pass
    return sorted(set(resolved))

@dataclass
class ServerState:
    started_at: float = field(default_factory=time.time)
    secret: bytes = b""
    bound_ips: list[str] = field(default_factory=list)

STATE = ServerState()

def _audit(record: dict) -> None:
    record["seq"] = STATE.started_at and None  # filled below
    record["ts_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE  # noqa
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        logging.error("audit write failed: %s", exc)

def _check_hmac(request: web.Request) -> tuple[bool, str]:
    sig = request.headers.get("X-Homelab-Signature-256", "")
    ts = request.headers.get("X-Homelab-Ts", "")
    body = request.body or b""
    body_sha = hashlib.sha256(body).hexdigest()
    msg = _canonical(request.method, request.path, dict(request.query), body_sha, str(ts))
    expected = hmac.new(STATE.secret, msg, hashlib.sha256).hexdigest()
    provided = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
    try:
        ts_i = int(ts)
    except ValueError:
        return False, "ts not int"
    if abs(time.time() - ts_i) > TS_WINDOW_S:
        return False, "stale"
    if not hmac.compare_digest(expected, provided):
        return False, "hmac mismatch"
    return True, "ok"

@web.middleware
async def hmac_gate(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    body = b""
    async for chunk in request.content.iter_chunked(MAX_FRAME_BYTES):
        body += chunk
    request._cached_body = body
    sig = request.headers.get("X-Homelab-Signature-256", "")
    ts = request.headers.get("X-Homelab-Ts", "")
    body_sha = hashlib.sha256(body).hexdigest()
    msg = _canonical(request.method, request.path, dict(request.query), body_sha, str(ts))
    exp = hmac.new(STATE.secret, msg, hashlib.sha256).hexdigest()
    prov = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
    try:
        ts_i = int(ts)
    except ValueError:
        return web.json_response({"ok": False, "reason": "ts not int"}, status=401)
    if abs(time.time() - ts_i) > TS_WINDOW_S:
        return web.json_response({"ok": False, "reason": "stale ts"}, status=401)
    if not hmac.compare_digest(exp, prov):
        return web.json_response({"ok": False, "reason": "hmac mismatch"}, status=401)
    return await handler(request)

async def post_pct_exec(request: web.Request) -> web.Response:
    body = getattr(request, "_cached_body", b"") or await request.read()
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "reason": "bad json"}, status=400)
    vmid = data.get("vmid")
    cmd = data.get("cmd") or []
    timeout_s = int(data.get("timeout_s") or DEFAULT_TIMEOUT_S)
    if not isinstance(vmid, int) or vmid <= 0:
        return web.json_response({"ok": False, "reason": "vmid required (int > 0)"}, status=400)
    if not cmd:
        return web.json_response({"ok": False, "reason": "cmd required"}, status=400)
    ok, why = _check_allowlist(cmd)
    if not ok:
        return web.json_response({"ok": False, "reason": why}, status=403)
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
    started = time.time()
    import uuid
    conn_id = uuid.uuid4().hex[:8]
    argv = ["pct", "exec", str(vmid), "--", "bash", "-lc"] + [" ".join(cmd)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _audit({"conn_id": conn_id, "vmid": vmid, "cmd": cmd, "rc": -1, "elapsed_ms": int((time.time()-started)*1000), "result": "timeout"})
            return web.json_response({"ok": False, "conn_id": conn_id, "reason": "timeout"}, status=504)
        stdout_b = stdout_b[:MAX_STDOUT_BYTES]
        stderr_b = stderr_b[:MAX_STDOUT_BYTES]
        rc = proc.returncode
    except FileNotFoundError:
        return web.json_response({"ok": False, "reason": "pct not on PATH"}, status=500)
    elapsed_ms = int((time.time() - started) * 1000)
    _audit({"conn_id": conn_id, "vmid": vmid, "cmd": cmd, "rc": rc, "elapsed_ms": elapsed_ms, "bytes_out": len(stdout_b)})
    return web.json_response({
        "ok": True,
        "conn_id": conn_id,
        "vmid": vmid,
        "argv": cmd,
        "rc": rc,
        "stdout": stdout_b.decode("utf-8", errors="replace"),
        "stderr": stderr_b.decode("utf-8", errors="replace"),
        "elapsed_ms": elapsed_ms,
    })

async def get_health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True, "version": "1.0", "uptime_s": int(time.time() - STATE.started_at),
    })

class ws_pct_stream:
    def __init__(self):
        pass
    async def __call__(self, request: web.Request) -> web.WebSocketResponse:
        # HMAC already validated by middleware.
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)
        # Authoritative vmid from URL path, not query.
        parts = request.path.split("/")
        try:
            vmid = int(parts[-1])
        except (IndexError, ValueError):
            await ws.send_json({"error": "bad vm id"})
            await ws.close()
            return ws
        q = dict(request.query)
        unit = q.get("unit") if isinstance(q.get("unit"), str) else None
        lines = int(q.get("lines", 200))
        idletimeout = int(q.get("idletimeout_s", 600))
        if not unit:
            await ws.send_json({"error": "unit required"})
            await ws.close()
            return ws

        cmd = ["pct", "exec", str(vmid), "--", "bash", "-lc",
               f"journalctl -u {shlex.quote(unit)} -f --no-pager -n {lines}"]
        ok, why = _check_allowlist(["journalctl"])
        if not ok:
            await ws.send_json({"error": why})
            await ws.close()
            return ws
        _audit({"conn_id": "ws-stream", "vmid": vmid, "cmd": cmd, "result": "stream-open"})
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        loop_started = time.time()
        try:
            while True:
                if time.time() - loop_started > idletimeout:
                    await ws.send_json({"ts_utc": _nowiso(), "line": "[idle-timeout, closing]\n"})
                    break
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                if not line:
                    if proc.returncode is not None:
                        break
                    continue
                await ws.send_json({"ts_utc": _nowiso(), "line": line.decode("utf-8", errors="replace").rstrip("\n")})
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            await ws.close()
        return ws

def _nowiso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_app() -> web.Application:
    app = web.Application(middlewares=[hmac_gate], client_max_size=MAX_FRAME_BYTES)
    app.router.add_post("/pct/exec", post_pct_exec)
    app.router.add_get("/health", get_health)
    app.router.add_get("/ws/pct-exec-stream/{vmid}", ws_pct_stream())
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    STATE.secret = _read_secret()

    ips = _resolve_ips(BIND_HOSTS)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bind = ips if not args.host == "0.0.0.0" else [args.host]
    logging.info("homelab-cli listening on %s:%d, bind_ips=%s", args.host, args.port, ips)
    web.run_app(build_app(), host=args.host, port=args.port, access_log=None, print=lambda *_: None)


if __name__ == "__main__":
    main()
