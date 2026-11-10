#!/usr/bin/env python3
"""Matrix long-poll daemon: !commands + emoji reactions in resources-review room."""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr  # noqa: E402
from matrix_client_simple import MatrixClient, resolve_resources_room_id  # noqa: E402

# 2026-10-11: 429 backoff wrapper for client.send_event.
# Additive: callers can opt in. No existing call sites modified yet.

import time

RETRY_AFTER_HARD_CAP_S = 5.0


def safe_send(client, room_id, event_type, body, attempts=4):
    """Send a Matrix event. On M_LIMIT_EXCEEDED, honour retry_after_ms."""
    delay = 1.0
    last_err = None
    for i in range(attempts):
        try:
            return client.send_event(room_id, event_type, body)
        except Exception as e:
            last_err = e
            errcode = getattr(e, "errcode", None)
            if errcode == "M_LIMIT_EXCEEDED":
                wait = min(
                    getattr(e, "retry_after_ms", 1000) / 1000.0,
                    RETRY_AFTER_HARD_CAP_S,
                )
                time.sleep(wait * (i + 1) / 4.0)
                delay = delay * 1.5
                continue
            raise
    raise last_err if last_err else Exception("safe_send exhausted retries")


SYNC_TOKEN_PATH = Path(os.environ.get("RESOURCES_REVIEW_SYNC_TOKEN", "/var/lib/resources-review/sync-token"))
ALLOW_SENDERS = {
    x.strip()
    for x in os.environ.get(
        "RESOURCES_REVIEW_ALLOW_SENDERS",
        "@daniel:matrix.leonbasu.lan",
    ).split(",")
    if x.strip()
}
CMD_RE = re.compile(
    r"^\s*!(approve|deny|show|queue|help)(?:\s+(\S+))?(?:\s+(.*))?$",
    re.I | re.S,
)
APPROVE_KEYS = {"✅", "✅️", "✔", "☑️", "👍", "🆗", "heavy_check_mark", "white_check_mark"}
DENY_KEYS = {"❌", "✖️", "✖", "👎", "❎", "x", "cross_mark"}
PID_RE = re.compile(r"`?(res-\d{8}-[a-f0-9]{8})`?", re.I)


def load_since() -> str | None:
    if SYNC_TOKEN_PATH.is_file():
        t = SYNC_TOKEN_PATH.read_text(encoding="utf-8").strip()
        return t or None
    return None


def save_since(v: str) -> None:
    SYNC_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(v + "\n", encoding="utf-8")
    tmp.replace(SYNC_TOKEN_PATH)


def normalize_key(key: str | None) -> str:
    if not key:
        return ""
    # strip variation selectors / whitespace
    return key.replace("\ufe0f", "").strip()


def is_approve_key(key: str | None) -> bool:
    k = normalize_key(key)
    if not k:
        return False
    if k in {normalize_key(x) for x in APPROVE_KEYS}:
        return True
    # raw emoji often is ✅
    return "✅" in (key or "") or k in {"+1", "thumbsup"}


def is_deny_key(key: str | None) -> bool:
    k = normalize_key(key)
    if not k:
        return False
    if k in {normalize_key(x) for x in DENY_KEYS}:
        return True
    return "❌" in (key or "") or k in {"-1", "thumbsdown"}


def proposal_id_from_event(client: MatrixClient, room_id: str, event_id: str) -> str | None:
    # 1) ledger by matrix_event_id
    data = rr.migrate_ledger()
    for e in data.get("entries", {}).values():
        e = rr.ensure_entry_shape(e.get("sha256") or "", e) if False else e
        if e.get("matrix_event_id") == event_id:
            return e.get("id")
    # reshape properly
    for sha, entry in data.get("entries", {}).items():
        entry = rr.ensure_entry_shape(sha, entry)
        if entry.get("matrix_event_id") == event_id:
            return entry.get("id")
    # 2) fetch event body and parse res- id
    try:
        ev = client.get_event(room_id, event_id)
        body = ((ev.get("content") or {}).get("body")) or ""
        m = PID_RE.search(body)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def moved_card_id(event_id: str) -> str | None:
    """Proposal id if event_id is a card that was re-posted (old copy kept in history)."""
    for e in rr.load_ledger().get("entries", {}).values():
        if event_id in (e.get("matrix_old_event_ids") or []) and e.get("matrix_event_id") != event_id:
            return e.get("id")
    return None


def handle_command(client: MatrixClient, room_id: str, sender: str, body: str) -> None:
    m = CMD_RE.match(body.strip())
    if not m:
        return
    if sender not in ALLOW_SENDERS:
        client.send_text(room_id, f"ignored: sender not allowlisted (`{sender}`)")
        return
    cmd = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    rest = (m.group(3) or "").strip()
    by = f"matrix:{sender}"

    if cmd == "help":
        client.send_text(
            room_id,
            "resources-review commands:\n"
            "`!queue`\n"
            "`!show <id>`\n"
            "`!approve <id>`\n"
            "`!deny <id> [reason]`\n"
            "Reactions on a card: ✅ approve, ❌ deny\n"
            "`!help`",
        )
        return
    if cmd == "queue":
        q = rr.queue(limit=20)
        if not q["items"]:
            client.send_text(room_id, "queue empty (no pending)")
            return
        lines = [f"pending: {q['count']}"]
        for it in q["items"]:
            lines.append(f"- `{it['id']}` {it.get('title')}")
        client.send_text(room_id, "\n".join(lines))
        return
    if cmd == "show":
        if not arg:
            client.send_text(room_id, "usage: `!show <id>`")
            return
        out = rr.show(arg)
        if not out.get("ok"):
            client.send_text(room_id, f"not found: `{arg}`")
            return
        client.send_text(room_id, out.get("card") or out.get("preview") or "ok")
        # also attach images in a new mini-thread under this reply if possible
        try:
            entry = out.get("entry") or {}
            imgs = rr.find_related_images(entry, limit=4)
            # send images without thread if no root; keep simple as follow-ups
            for img in imgs:
                client.send_image(room_id, img, caption=f"{entry.get('id')} {img.name}")
        except Exception as e:
            client.send_text(room_id, f"(images skipped: {e})")
        return
    if cmd == "approve":
        if not arg:
            client.send_text(room_id, "usage: `!approve <id>`")
            return
        out = rr.approve(arg, by=by)
        if not out.get("ok"):
            client.send_text(room_id, f"approve failed: {out.get('error')}")
            return
        e = out["entry"]
        msg = f"accepted `{e.get('id')}` → `{e.get('accepted_path')}`"
        if out.get("noop"):
            msg = f"already accepted `{e.get('id')}`"
        client.send_text(room_id, msg)
        return
    if cmd == "deny":
        if not arg:
            client.send_text(room_id, "usage: `!deny <id> [reason]`")
            return
        out = rr.deny(arg, reason=rest, by=by)
        if not out.get("ok"):
            client.send_text(room_id, f"deny failed: {out.get('error')}")
            return
        e = out["entry"]
        msg = f"denied `{e.get('id')}`"
        if rest:
            msg += f" ({rest})"
        if out.get("noop"):
            msg = f"already denied `{e.get('id')}`"
        client.send_text(room_id, msg)
        return


def handle_reaction(client: MatrixClient, room_id: str, sender: str, key: str, relates_event_id: str) -> None:
    if sender not in ALLOW_SENDERS:
        return
    if not (is_approve_key(key) or is_deny_key(key)):
        return
    moved = moved_card_id(relates_event_id)
    if moved:
        client.send_text(room_id, f"that card moved: react on the latest card for `{moved}` (re-posted 2026-10-22). Nothing changed.")
        return
    pid = proposal_id_from_event(client, room_id, relates_event_id)
    if not pid:
        client.send_text(room_id, f"reaction noted but no proposal id for event `{relates_event_id}`")
        return
    by = f"matrix-reaction:{sender}"
    if is_approve_key(key):
        out = rr.approve(pid, by=by)
        if out.get("ok"):
            e = out["entry"]
            msg = f"✅ accepted `{e.get('id')}` → `{e.get('accepted_path')}`"
            if out.get("noop"):
                msg = f"✅ already accepted `{e.get('id')}`"
            client.send_text(room_id, msg)
        else:
            client.send_text(room_id, f"approve failed for `{pid}`: {out.get('error')}")
        return
    if is_deny_key(key):
        out = rr.deny(pid, reason="matrix-reaction", by=by)
        if out.get("ok"):
            e = out["entry"]
            msg = f"❌ denied `{e.get('id')}`"
            if out.get("noop"):
                msg = f"❌ already denied `{e.get('id')}`"
            client.send_text(room_id, msg)
        else:
            client.send_text(room_id, f"deny failed for `{pid}`: {out.get('error')}")


def main() -> int:
    room_id = resolve_resources_room_id()
    if not room_id:
        print("MATRIX_RESOURCES_ROOM_ID not set", flush=True)
        return 2
    client = MatrixClient()
    client.ensure_token()
    me = client.whoami().get("user_id")
    print(f"resources-review-matrixd me={me} room={room_id}", flush=True)
    since = load_since()
    while True:
        try:
            data = client.sync(since=since, timeout_ms=30000)
            since = data.get("next_batch") or since
            if since:
                save_since(since)
            rooms = (data.get("rooms") or {}).get("join") or {}
            room = rooms.get(room_id) or {}
            events = ((room.get("timeline") or {}).get("events")) or []
            for ev in events:
                et = ev.get("type")
                sender = ev.get("sender") or ""
                if me and sender == me:
                    continue
                if et == "m.room.message":
                    content = ev.get("content") or {}
                    if content.get("msgtype") != "m.text":
                        continue
                    body = content.get("body") or ""
                    if not body.lstrip().startswith("!"):
                        continue
                    try:
                        handle_command(client, room_id, sender, body)
                    except Exception as e:
                        try:
                            client.send_text(room_id, f"error: {str(e)[:200]}")
                        except Exception:
                            pass
                        print(f"cmd_error:{e}", flush=True)
                elif et == "m.reaction":
                    content = ev.get("content") or {}
                    rel = content.get("m.relates_to") or {}
                    if rel.get("rel_type") and rel.get("rel_type") != "m.annotation":
                        # some servers omit; key+event_id still present
                        pass
                    key = rel.get("key")
                    rid = rel.get("event_id")
                    if not rid:
                        continue
                    try:
                        handle_reaction(client, room_id, sender, key or "", rid)
                    except Exception as e:
                        try:
                            client.send_text(room_id, f"reaction error: {str(e)[:200]}")
                        except Exception:
                            pass
                        print(f"reaction_error:{e}", flush=True)
        except Exception as e:
            print(f"sync_error:{e}", flush=True)
            time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
