#!/usr/bin/env python3
"""One-off: re-post every pending proposal as a fresh card in post order, so the
review reads top to bottom. Old cards are edited to point at the new one and kept
in matrix_old_event_ids (the daemon answers reactions on them with a pointer).
No model calls: cards are built from the staging note and Sage's source.
Dry run by default; --apply to post.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr  # noqa: E402
from matrix_client_simple import MatrixClient, resolve_resources_room_id  # noqa: E402

spec = importlib.util.spec_from_file_location("cur", "/usr/local/sbin/lif-resources-curate.py")
cur = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cur)

APPLY = "--apply" in sys.argv


def main() -> int:
    pending = sorted(rr.list_by_status(rr.load_ledger(), "pending"), key=lambda e: e.get("source_key") or "~")
    pending = [e for e in pending if (e.get("source_key") or "").startswith("posts/")]
    for i, e in enumerate(pending, 1):
        print(f"{i:2}/{len(pending)} {e['id']} {e['source_key']}")
    if not APPLY:
        print("dry run: nothing posted")
        return 0
    client = MatrixClient()
    room = resolve_resources_room_id()
    client.send_text(room, f"Queue review, 2026-10-22: {len(pending)} proposals follow in post order (01 to 35). "
                           "React ✅ accept or ❌ deny on each card. Earlier copies of these cards now say moved.")
    for i, e in enumerate(pending, 1):
        data = rr.load_ledger()
        sha = e["sha256"]
        entry = data["entries"][sha]
        old = [x for x in (entry.get("matrix_event_id"),) if x]
        entry["matrix_old_event_ids"] = list(dict.fromkeys((entry.get("matrix_old_event_ids") or []) + old))
        entry["matrix_event_id"] = None
        entry["matrix_thread_event_id"] = None
        rr.save_ledger(data)
        src = Path(entry["src"])
        meta, _ = cur.parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
        note = rr.read_note_text(entry.get("vault_sync_path"))
        cur.matrix_notify(entry, note=note, meta=meta, src=src)
        new_eid = rr.load_ledger()["entries"][sha].get("matrix_event_id")
        for eid in old:
            try:
                client.edit_text(room, eid, f"↪ Moved: {entry['title']} was re-posted on 2026-10-22 in the queue review "
                                            f"run (card {i}/{len(pending)}). Reactions on this copy do nothing.")
            except Exception as ex:
                print("  old card edit failed", eid, ex)
        print(f"posted {i}/{len(pending)} {entry['id']} -> {new_eid} (old: {len(old)})")
        time.sleep(1.0)
    rr.audit({"action": "repost_queue", "count": len(pending)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
