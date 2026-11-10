#!/usr/bin/env python3
"""One-shot / reusable: promote accepted ledger notes into vault-sync 3 - Resources/Music/."""
from __future__ import annotations
import os, sys
from pathlib import Path
sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr

LIVE_PREFIX = os.environ.get("LIF_LIVE_RESOURCES_PREFIX", "3 - Resources/")
LIVE_MUSIC = os.environ.get("LIF_LIVE_MUSIC_FOLDER", "Music")


def main() -> int:
    data = rr.migrate_ledger()
    n = 0
    for sha, entry in list(data.get("entries", {}).items()):
        entry = rr.ensure_entry_shape(sha, entry)
        if entry.get("status") != "accepted":
            continue
        rel = entry.get("accepted_path") or entry.get("vault_sync_path")
        if not rel:
            continue
        # already live?
        if str(rel).startswith(LIVE_PREFIX):
            print("already_live", entry.get("id"), rel)
            continue
        src = rr.VAULT_REPO / rel
        if not src.is_file():
            print("missing", rel)
            continue
        bucket = entry.get("bucket") or "music"
        live_bucket = LIVE_MUSIC if bucket == "music" else bucket
        base = Path(rel).name
        dest = f"{LIVE_PREFIX}{live_bucket}/{base}"
        try:
            detail = rr._git_mv_or_copy(rel, dest, f"lif promote accepted->Resources: {entry.get('id')}")
            entry["accepted_path"] = dest
            entry["vault_sync_path"] = dest
            entry["obsidian_path"] = f"3 - Resources/{live_bucket}/{base}"
            entry["promoted_ts"] = rr.now_ts()
            data["entries"][sha] = entry
            rr.save_ledger(data)
            print("promoted", entry.get("id"), "->", dest)
            n += 1
        except Exception as e:
            print("fail", entry.get("id"), e)
    print("done", n)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
