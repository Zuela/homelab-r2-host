#!/usr/bin/env python3
"""Create #resources-review room and record id (admin + hermes-bot)."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
sys.path.insert(0, "/usr/local/sbin")
from matrix_client_simple import MatrixClient, _load_kv

NS = Path("/root/.creds/matrix-namespace-rooms.json")
SERVER = "matrix.leonbasu.lan"

def admin_client() -> MatrixClient:
    # Prefer admin token file
    tok = Path("/root/.creds/matrix-admin-token")
    admin = _load_kv(Path("/root/.creds/matrix-admin"))
    cfg = {
        "url": admin.get("URL", "http://192.168.4.55:8008").rstrip("/"),
        "server_name": admin.get("SERVER_NAME", SERVER),
        "username": admin.get("USERNAME", "admin"),
        "password": admin.get("PASSWORD"),
        "mxid": admin.get("MXID"),
    }
    if tok.is_file():
        kv = _load_kv(tok)
        if kv.get("ACCESS_TOKEN"):
            cfg["access_token"] = kv["ACCESS_TOKEN"]
        if kv.get("URL"):
            cfg["url"] = kv["URL"].rstrip("/")
        if kv.get("USERNAME") and not cfg.get("username"):
            cfg["username"] = kv["USERNAME"]
    return MatrixClient(cfg)

def hermes_client() -> MatrixClient:
    p = Path("/root/.creds/matrix-hermes-bot")
    d = json.loads(p.read_text())
    admin = _load_kv(Path("/root/.creds/matrix-admin"))
    cfg = {
        "url": admin.get("URL", "http://192.168.4.55:8008").rstrip("/"),
        "server_name": admin.get("SERVER_NAME", SERVER),
        "username": d["username"],
        "password": d["password"],
        "mxid": f"@{d['username']}:{SERVER}",
    }
    # resources bot token cache path
    os.environ["MATRIX_RESOURCES_TOKEN_FILE"] = "/root/.creds/matrix-resources-bot.token"
    # write creds pointer
    Path("/root/.creds/matrix-resources-bot").write_text(
        f"URL={cfg['url']}\nSERVER_NAME={cfg['server_name']}\nUSERNAME={cfg['username']}\nPASSWORD={d['password']}\nMXID={cfg['mxid']}\n",
        encoding="utf-8",
    )
    os.chmod("/root/.creds/matrix-resources-bot", 0o600)
    return MatrixClient(cfg)

def main() -> int:
    ns = {}
    if NS.is_file():
        ns = json.loads(NS.read_text())
    if ns.get("resources_review_room_id") and "--force" not in sys.argv:
        print(json.dumps({"ok": True, "existing": ns["resources_review_room_id"]}))
        # still ensure join
        room = ns["resources_review_room_id"]
        try:
            hc = hermes_client()
            hc.login(force=False)
            try:
                hc.join(room)
            except Exception as e:
                print("join_note", str(e)[:120])
        except Exception as e:
            print("hermes_login_note", str(e)[:120])
        return 0

    ac = admin_client()
    try:
        ac.ensure_token()
    except Exception:
        ac.login(force=True)
    invite = [
        f"@daniel:{SERVER}",
        f"@hermes-bot:{SERVER}",
        f"@n8n-bot:{SERVER}",
    ]
    try:
        created = ac.create_room(
            alias_localpart="resources-review",
            name="resources-review",
            topic="LIF second-brain proposals. Commands: !queue !show !approve !deny",
            invite=invite,
        )
    except Exception as e:
        # alias may exist
        print("create_error", str(e)[:300])
        # try resolve alias
        import urllib.parse, urllib.request
        ac.ensure_token()
        alias = f"#resources-review:{SERVER}"
        enc = urllib.parse.quote(alias)
        req = urllib.request.Request(
            f"{ac.url}/_matrix/client/v3/directory/room/{enc}",
            headers={"Authorization": f"Bearer {ac.token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            created = json.loads(resp.read().decode())
            created = {"room_id": created["room_id"]}
    room_id = created["room_id"]
    ns["resources_review_room_id"] = room_id
    ns.setdefault("server_name", SERVER)
    ns.setdefault("homeserver", ac.url)
    NS.write_text(json.dumps(ns, indent=2) + "\n", encoding="utf-8")
    os.chmod(NS, 0o600)
    # env file for services
    Path("/etc/default/resources-review").write_text(
        f"MATRIX_RESOURCES_ROOM_ID={room_id}\nMATRIX_URL={ac.url}\nMATRIX_SERVER_NAME={SERVER}\n",
        encoding="utf-8",
    )
    hc = hermes_client()
    hc.login(force=True)
    try:
        hc.join(room_id)
    except Exception as e:
        print("hermes_join", str(e)[:200])
    # invite already done; try send hello
    try:
        hc.send_text(room_id, "resources-review online.\nCommands: `!help` `!queue` `!approve <id>` `!deny <id>` `!show <id>`")
    except Exception as e:
        print("hello_err", str(e)[:200])
    print(json.dumps({"ok": True, "room_id": room_id}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
