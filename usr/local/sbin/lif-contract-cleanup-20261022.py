#!/usr/bin/env python3
"""One-off cleanup for the 2026-10-22 Sage-contract rework of lif-resources-curate.

Dry run by default; --apply to write.
1. Ledger: add source_key to Muse entries. Per Muse path keep one live proposal:
   latest accepted, else latest pending, else latest denied. Other pending
   versions become superseded (older than the live one) or edit_after_decision
   (newer than an accepted/denied one). themes/ and other non-posts become
   not_proposal. Accepted/denied history is never changed.
2. Staging files of entries retired in 1 move to inbox/resources-superseded/.
3. Accepted notes written fenced by the model (2026-10-20..21) are rebuilt with
   proper frontmatter, no model call.
4. Luminate post 34 gets its lost Matrix event ids back.
All vault changes land as one vault-sync commit.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

BIN = Path(os.environ.get("LIF_BIN", "/usr/local/sbin"))
sys.path.insert(0, str(BIN))
import resources_review_lib as rr  # noqa: E402
from matrix_client_simple import MatrixClient, _req, resolve_resources_room_id  # noqa: E402

spec = importlib.util.spec_from_file_location("cur", BIN / "lif-resources-curate.py")
cur = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cur)

APPLY = "--apply" in sys.argv
VAULT = rr.VAULT_REPO
SUPERSEDED_PREFIX = "inbox/resources-superseded/"
LUMINATE = ("res-20261021-aa19e272", "$UkDaAy0eInPUeBRguDGz2jr75K1iAcuErH1tQSf4AWk")
TS = rr.now_ts()


def main() -> int:
    git = ["git", "-C", str(VAULT)]
    env = {**os.environ, "GIT_ASKPASS": "/usr/local/sbin/gitea-vault-sync-askpass", "GIT_TERMINAL_PROMPT": "0"}
    if APPLY:
        # pull before any write: rebase refuses a dirty tree
        subprocess.run(git + ["pull", "--rebase", "origin", "main"], check=True, env=env)
    data = rr.migrate_ledger()
    entries = data["entries"]
    groups: dict[str, list[str]] = {}
    for sha, e in entries.items():
        key = cur.entry_source_key(e)
        if not key:
            continue
        if e.get("source_key") != key:
            e["source_key"] = key
        groups.setdefault(key, []).append(sha)

    retire: list[tuple[str, str, str | None]] = []  # (sha, new_status, live_sha)
    for key, shas in groups.items():
        live_versions = [s for s in shas if entries[s].get("status") in cur.LIVE_STATUSES]
        if not key.startswith("posts/"):
            retire += [(s, "not_proposal", None) for s in live_versions if entries[s]["status"] == "pending"]
            continue
        by_ts = sorted(live_versions, key=lambda s: entries[s].get("ts") or "")
        winner = None
        for status in ("accepted", "pending", "denied"):
            hits = [s for s in by_ts if entries[s]["status"] == status]
            if hits:
                winner = hits[-1]
                break
        for s in by_ts:
            if s == winner or entries[s]["status"] != "pending":
                continue
            newer = (entries[s].get("ts") or "") > (entries[winner].get("ts") or "")
            status = "edit_after_decision" if newer and entries[winner]["status"] != "pending" else "superseded"
            retire.append((s, status, winner))

    moves: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for sha, status, live in retire:
        e = entries[sha]
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:20} {e.get('id')}  {e.get('source_key')}  live={entries[live]['id'] + ' ' + entries[live]['status'] if live else '-'}")
        rel = e.get("staging_path")
        if rel and rel == e.get("vault_sync_path") and (VAULT / rel).is_file():
            dest = SUPERSEDED_PREFIX + rel[len(rr.STAGING_PREFIX):] if rel.startswith(rr.STAGING_PREFIX) else SUPERSEDED_PREFIX + Path(rel).name
            if (VAULT / dest).exists() or any(d == dest for _, d in moves):
                dest = dest[:-3] + f"-{sha[:8]}.md"
            moves.append((rel, dest))
            e["superseded_path"] = dest
            e["vault_sync_path"] = dest
        e["status"] = status
        e["retired_ts"] = TS
        e["retired_by"] = "lif-contract-cleanup-20261022"
        if live and status == "superseded":
            e["superseded_by"] = live
        if status == "edit_after_decision":
            e["decided_status"] = entries[live]["status"]
    print("retire counts:", counts, "staging moves:", len(moves))

    # 3. fenced accepted notes
    rebuilt: list[str] = []
    for sha, e in entries.items():
        if e.get("status") != "accepted":
            continue
        for rel in {e.get("accepted_path"), e.get("archive_path")} - {None}:
            p = VAULT / rel
            if not p.is_file() or not p.read_text().startswith("```"):
                continue
            src = Path(e["src"])
            meta, _ = cur.parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
            old = p.read_text().split("\n---\nlif_sha256:")[0]
            body = cur.unwrap_model_output(old)
            note = cur.build_note(pid=e["id"], title=cur.title_for(meta, src), meta=meta, note_body=body,
                                  bucket=e.get("bucket") or "music", digest=sha,
                                  source_key=cur.entry_source_key(e), src=src)
            rebuilt.append(rel)
            print("rebuild", rel)
            if APPLY:
                p.write_text(note, encoding="utf-8")
            e["title"] = cur.title_for(meta, src)

    # 4. Luminate event ids
    pid, parent = LUMINATE
    sha, e = rr.find_by_id(data, pid)
    if e and not e.get("matrix_event_id"):
        client = MatrixClient()
        client.ensure_token()
        room = resolve_resources_room_id()
        q = urllib.parse.quote
        rel = _req("GET", f"{client.url}/_matrix/client/v1/rooms/{q(room)}/relations/{q(parent)}/m.thread?limit=10", token=client.token)
        mine = [ev for ev in rel.get("chunk", []) if ev.get("sender") == client.cfg.get("user_id") or ev.get("sender", "").startswith("@hermes-bot")]
        thread = sorted(mine, key=lambda ev: ev["origin_server_ts"])[0]["event_id"] if mine else None
        print("luminate ids", parent, thread)
        entries[sha]["matrix_event_id"] = parent
        if thread:
            entries[sha]["matrix_thread_event_id"] = thread

    if not APPLY:
        print("dry run: nothing written")
        return 0

    for src_rel, dest in moves:
        (VAULT / dest).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(git + ["mv", src_rel, dest], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    msg = f"lif contract cleanup: retire {len(retire)} duplicate/theme proposals, unfence {len(rebuilt)} accepted notes"
    subprocess.run(git + ["-c", "user.name=r2-vault-sync", "-c", "user.email=vault-sync@homelab.local", "commit", "-m", msg], check=True)
    subprocess.run(git + ["push", "origin", "HEAD:main"], check=True, env=env)
    rr.save_ledger(data)
    rr.audit({"action": "contract_cleanup", "retired": counts, "moves": len(moves), "rebuilt": rebuilt})
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
