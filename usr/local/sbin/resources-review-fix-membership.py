#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/sbin")
from matrix_client_simple import MatrixClient, _load_kv

SERVER = "matrix.leonbasu.lan"


def main() -> int:
    room = json.loads(Path("/root/.creds/matrix-namespace-rooms.json").read_text())["resources_review_room_id"]
    admin = _load_kv(Path("/root/.creds/matrix-admin"))
    print("room", room)

    dc = MatrixClient(
        {
            "url": admin["URL"].rstrip("/"),
            "server_name": admin.get("SERVER_NAME", SERVER),
            "username": admin["USERNAME"],
            "password": admin["PASSWORD"],
            "mxid": admin.get("MXID"),
        }
    )
    dc.token = None
    dc.login(force=True)
    print("creator", dc.whoami())

    for user in (f"@hermes-bot:{SERVER}", f"@n8n-bot:{SERVER}"):
        try:
            print("invite", user, dc.invite(room, user))
        except Exception as e:
            print("invite", user, str(e)[:200])

    hb = json.loads(Path("/root/.creds/matrix-hermes-bot").read_text())
    cred_path = Path("/root/.creds/matrix-resources-bot")
    cred_path.write_text(
        "URL={url}\nSERVER_NAME={server}\nUSERNAME={user}\nPASSWORD={pw}\nMXID=@{user}:{server}\n".format(
            url=admin["URL"].rstrip("/"),
            server=SERVER,
            user=hb["username"],
            pw=hb["password"],
        ),
        encoding="utf-8",
    )
    os.chmod(cred_path, 0o600)

    tok = Path("/root/.creds/matrix-resources-bot.token")
    if tok.exists():
        tok.unlink()

    os.environ["MATRIX_RESOURCES_BOT_CREDS"] = str(cred_path)
    os.environ["MATRIX_RESOURCES_TOKEN_FILE"] = str(tok)
    os.environ["MATRIX_RESOURCES_ROOM_ID"] = room

    hc = MatrixClient()
    hc.login(force=True)
    print("bot", hc.whoami())
    try:
        print("join", hc.join(room))
    except Exception as e:
        print("join_err", str(e)[:300])

    Path("/etc/default/resources-review").write_text(
        "MATRIX_RESOURCES_ROOM_ID={room}\n"
        "MATRIX_URL={url}\n"
        "MATRIX_SERVER_NAME={server}\n"
        "MATRIX_RESOURCES_BOT_CREDS=/root/.creds/matrix-resources-bot\n"
        "MATRIX_RESOURCES_TOKEN_FILE=/root/.creds/matrix-resources-bot.token\n"
        "RESOURCES_REVIEW_ALLOW_SENDERS=@daniel:matrix.leonbasu.lan\n".format(
            room=room,
            url=admin["URL"].rstrip("/"),
            server=SERVER,
        ),
        encoding="utf-8",
    )

    try:
        print("send", hc.send_text(room, "resources-review bot joined. Try `!help` then `!queue`."))
    except Exception as e:
        print("send_err", str(e)[:300])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
