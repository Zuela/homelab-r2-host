#!/usr/bin/env python3
"""One-off: regenerate accepted Resources notes that are broken (placeholder
summary, bad or missing frontmatter, no H1, leftover appendix) from Sage's
latest version of the post, with the 2026-10-22 curator. Keeps the accepted id
and paths. Dry run by default; --apply writes one vault-sync commit.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr  # noqa: E402

spec = importlib.util.spec_from_file_location("cur", "/usr/local/sbin/lif-resources-curate.py")
cur = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cur)

APPLY = "--apply" in sys.argv
# --ids a,b: regenerate exactly these accepted notes (2026-10-22 truncated notes)
ONLY = {x for a in sys.argv if a.startswith("--ids=") for x in a[6:].split(",") if x}
VAULT = rr.VAULT_REPO


def problems(text: str) -> list[str]:
    out = []
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        out.append("no frontmatter")
    else:
        try:
            if not (yaml.safe_load(m.group(1)) or {}).get("title"):
                out.append("no title")
        except yaml.YAMLError:
            out.append("bad yaml")
    if not re.search(r"^# ", text, re.M):
        out.append("no H1")
    if "model unavailable" in text:
        out.append("placeholder")
    if "```" in text:
        out.append("fence")
    if "\n---\nlif_sha256:" in text:
        out.append("appendix")
    return out


def latest_source(key: str) -> Path | None:
    flat = key.replace("/", "__")
    hits = list(cur.LIF_INCOMING.glob(f"*/notes/*muse-{flat}.md"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def main() -> int:
    git = ["git", "-C", str(VAULT)]
    env = {**os.environ, "GIT_ASKPASS": "/usr/local/sbin/gitea-vault-sync-askpass", "GIT_TERMINAL_PROMPT": "0"}
    if APPLY:
        subprocess.run(git + ["pull", "--rebase", "origin", "main"], check=True, env=env)
    data = rr.load_ledger()
    run = {"comfy_freed": False, "retries": {}}
    done = []
    for sha, e in data["entries"].items():
        if e.get("status") != "accepted":
            continue
        live = e.get("accepted_path")
        if not live or not (VAULT / live).is_file():
            continue
        issues = problems((VAULT / live).read_text(encoding="utf-8", errors="replace"))
        if ONLY:
            if e.get("id") not in ONLY:
                continue
            issues = issues or ["truncated"]
        if not issues:
            continue
        key = cur.entry_source_key(e)
        src = latest_source(key) if key else None
        print(f"{e['id']}  {live}  :: {', '.join(issues)}  <- {src.name if src else 'NO SOURCE'}")
        if not APPLY or not src:
            continue
        meta, body = cur.parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
        title = cur.title_for(meta, src)
        bucket = e.get("bucket") or "music"
        if not run["comfy_freed"]:
            cur.comfy_free_vram()
            run["comfy_freed"] = True
        try:
            note_body = cur.ollama_curate(title, meta, body, str(src), bucket)
        except Exception as ex:
            print(f"  model failed, left as is: {ex}")
            continue
        note = cur.build_note(pid=e["id"], title=title, meta=meta, note_body=note_body, bucket=bucket,
                              digest=cur.sha256_file(src), source_key=key, src=src)
        for rel in {live, e.get("archive_path")} - {None}:
            if (VAULT / rel).is_file():
                (VAULT / rel).write_text(note, encoding="utf-8")
        e["title"] = title
        e["regenerated_ts"] = rr.now_ts()
        e["regenerated_from"] = str(src)
        done.append(e["id"])
        print("  regenerated")
    if not APPLY:
        print("dry run: nothing written")
        return 0
    if done:
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["-c", "user.name=r2-vault-sync", "-c", "user.email=vault-sync@homelab.local", "commit", "-m",
                              f"lif: regenerate {len(done)} accepted Resources notes from Sage v2 (placeholder/broken frontmatter)" if not ONLY else f"lif: regenerate {len(done)} truncated accepted Resources notes"], check=True)
        subprocess.run(git + ["push", "origin", "HEAD:main"], check=True, env=env)
        fresh = rr.load_ledger()
        for sha, e in data["entries"].items():
            if e.get("id") in done and e.get("status") == "accepted":
                fresh["entries"][sha].update({k: e[k] for k in ("title", "regenerated_ts", "regenerated_from")})
        rr.save_ledger(fresh)
        rr.audit({"action": "regen_accepted", "ids": done})
    print("regenerated", len(done))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
