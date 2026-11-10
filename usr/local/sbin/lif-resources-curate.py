#!/usr/bin/env python3
"""LIF -> second-brain curator.

Reads newly promoted LIF markdown, drafts PARA Resources notes via Ollama,
and commits proposals into vault-sync inbox/resources-staging/<bucket>/.
Music-related notes go under music/ until Muse scope expands.
Notifies Matrix #resources-review with stable proposal ids.

2026-10-22 Sage contract (social-intake muse-intake/v2):
- Only posts/ are proposals; themes/, README and the index note are skipped.
- A proposal is keyed by its Muse path (source_key). A new version of a pending
  post updates that proposal in place (same id, staging file and Matrix post).
  A new version of an accepted or denied post is recorded, not re-proposed.
- Title, summary, buckets and date come from Sage's frontmatter. The model only
  writes the body sections; frontmatter and the H1 are built here.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import yaml

sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr  # noqa: E402

LIF_INCOMING = Path(os.environ.get("LIF_INCOMING", "/tank/studio-homelab/vault-drop/lif/incoming"))
CURATE_LEDGER = Path(os.environ.get("LIF_CURATE_LEDGER", "/tank/ingest/state/curated-sha256.json"))
AUDIT = Path(os.environ.get("LIF_CURATE_AUDIT", "/var/log/lif-resources-curate.jsonl"))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://192.168.4.35:11434").rstrip("/")
MODEL = os.environ.get("LIF_CURATE_MODEL", "qwen3-agent:latest")
COMMIT_BIN = os.environ.get("VAULT_SYNC_COMMIT_BIN", "/usr/local/sbin/vault-sync-commit")
MAX_FILES = int(os.environ.get("LIF_CURATE_MAX_FILES", "8"))
MAX_SOURCE_CHARS = int(os.environ.get("LIF_CURATE_MAX_CHARS", "12000"))
DEFAULT_BUCKET = os.environ.get("LIF_CURATE_DEFAULT_BUCKET", "music").strip().strip("/")
NOTIFY = os.environ.get("LIF_CURATE_MATRIX_NOTIFY", "1").strip() not in {"0", "false", "no"}
# 2026-10-19: free ComfyUI VRAM before summarizing; retry instead of posting a placeholder.
COMFY = os.environ.get("LIF_CURATE_COMFY_URL", "http://192.168.4.41:8188").rstrip("/")
RETRY_STATE = Path(os.environ.get("LIF_CURATE_RETRY_STATE", "/tank/ingest/state/curate-ollama-retries.json"))
# 2026-10-29 (Dan): hourly runs set this so only Sage pass-through notes are carded; no model, no GPU.
PASSTHROUGH_ONLY = os.environ.get("LIF_CURATE_PASSTHROUGH_ONLY", "0").strip() in {"1", "true", "yes"}
UPDATE_ACCEPTED = os.environ.get("LIF_CURATE_UPDATE_ACCEPTED", "1").strip() not in {"0", "false", "no"}
MAX_OLLAMA_ATTEMPTS = int(os.environ.get("LIF_CURATE_MAX_OLLAMA_ATTEMPTS", "4"))
THINK = os.environ.get("LIF_CURATE_THINK", "0").strip() in {"1", "true", "yes"}

MUSIC_HINTS = (
    "music", "spotify", "dsp", "streaming", "artist", "label", "song", "album",
    "playlist", "bandsintown", "tour", "concert", "royalt", "payout", "tiktok",
    "instagram", "soundcloud", "apple music", "catalog", "a&r", "indie",
    "hip-hop", "hip hop", "rap", "producer", "beat", "sync", "publishing",
    "muse", "social-intake", "fan-data", "fan data", "ccdt", "conceit",
)


def log(event: dict) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()), **event}
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def force_enabled() -> bool:
    if os.environ.get("LIF_CURATE_FORCE", "").strip() in {"1", "true", "yes"}:
        return True
    return any(a in {"--force", "-f"} for a in sys.argv[1:])


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if m:
        try:
            meta = yaml.safe_load(m.group(1))
            if isinstance(meta, dict):
                return meta, text[m.end():].lstrip("\n")
        except yaml.YAMLError:
            pass
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_raw, body = parts[1], parts[2]
    meta: dict = {}
    for line in meta_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip().strip("'").strip('"')
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            meta[key] = [x.strip().strip("'").strip('"') for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[key] = val
    if "tags" not in meta:
        tags = []
        in_tags = False
        for line in meta_raw.splitlines():
            if line.strip() == "tags:" or line.strip().startswith("tags:"):
                in_tags = True
                continue
            if in_tags:
                if line.strip().startswith("- "):
                    tags.append(line.strip()[2:].strip().strip("'").strip('"'))
                elif line.strip() and not line.startswith(" "):
                    break
        if tags:
            meta["tags"] = tags
    return meta, body.lstrip("\n")


def collect_candidates() -> list[Path]:
    if not LIF_INCOMING.exists():
        return []
    files = [p for p in LIF_INCOMING.rglob("*.md") if not p.name.endswith(".lif.json")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def is_music_related(title: str, meta: dict, body: str, source_path: str) -> bool:
    sp = source_path.lower()
    if "muse" in sp or "social-intake" in sp or "posts__" in sp or "themes__" in sp:
        return True
    blob_parts = [title, source_path, body[:4000]]
    tags = meta.get("tags") or []
    if isinstance(tags, list):
        blob_parts.extend(str(t) for t in tags)
    elif tags:
        blob_parts.append(str(tags))
    for k, v in meta.items():
        blob_parts.append(f"{k} {v}")
    blob = " ".join(blob_parts).lower()
    return any(h in blob for h in MUSIC_HINTS)


def resources_bucket(title: str, meta: dict, body: str, source_path: str) -> str:
    explicit = (meta.get("resources_folder") or meta.get("para_folder") or "").strip().strip("/")
    if explicit:
        seg = explicit.split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]+", seg):
            return seg.lower()
    if is_music_related(title, meta, body, source_path):
        return "music"
    return DEFAULT_BUCKET or "music"



def comfy_free_vram() -> None:
    """Ask ComfyUI to unload its models so Ollama gets the whole GPU. Skipped if Comfy is busy."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{COMFY}/queue", timeout=5) as resp:
            q = json.loads(resp.read().decode("utf-8"))
        busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
        if busy:
            log({"phase": "comfy_free", "ok": False, "skipped": "comfy_busy", "busy": busy})
            return
        req = urllib.request.Request(
            f"{COMFY}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        time.sleep(5)
        log({"phase": "comfy_free", "ok": True, "status": status})
    except Exception as e:
        log({"phase": "comfy_free", "ok": False, "error": str(e)[:200]})


def load_retries() -> dict:
    try:
        return json.loads(RETRY_STATE.read_text())
    except Exception:
        return {}


def save_retries(data: dict) -> None:
    RETRY_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RETRY_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(RETRY_STATE)


BODY_SECTIONS = ("Summary", "Key claims", "Connections", "Open questions", "Attachments")
# LIF promotes Muse files as <ts>-<sha12>-muse-<path with / flattened to __>.md
MUSE_NAME_RE = re.compile(r"^(?:\d{8}T\d{6}Z-[0-9a-f]{12}-)?muse-(.+)\.md$")
LIVE_STATUSES = ("pending", "accepted", "denied")


def sage_body(body: str) -> str | None:
    """Sage's own analysis body for a verified social-intake post, or None when Sage left it empty.
    2026-10-29 (Dan): pass Sage's text through unchanged instead of a second model rewrite.
    2026-11-02 (Dan): image embeds become vault paths, and approve() copies the files. Other embeds are dropped."""
    m = re.search(r"^## Summary\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if not m or not m.group(1).strip():
        return None
    lines = []
    for l in body.strip().splitlines():
        if re.match(r"^!\[[^\]]*\]\([^)]*\)\s*$", l.strip()):
            l = rr.vault_embed(l.strip())
            if not l:
                continue
        lines.append(l)
    return "\n".join(lines).strip()


def ollama_curate(title: str, meta: dict, body: str, source_path: str, bucket: str) -> str:
    """Return the note body (H2 sections only). Frontmatter and H1 are built by build_note."""
    import urllib.request
    sections = "\n".join(f"## {s}" for s in BODY_SECTIONS)
    prompt = f"""You are curating research intake into an Obsidian second brain (PARA).
The note is destined for folder 3 - Resources/{bucket}/
(music industry / DSP / creator economy when bucket is music).

Output ONLY the note body in Markdown, using exactly these H2 sections in this order:
{sections}

Rules:
- Do not output YAML frontmatter, a "# " title line, or code fences. Start with "## Summary".
- Summary: 5-10 lines. Key claims: bullets. Connections: wikilink-style [[Theme]] ideas.
  Attachments: images or slides the source mentions, or "None mentioned in source."
- Preserve important authors and source URLs from the input
- Do not invent facts not supported by the source
- No secrets
- Keep under ~800 words

Title: {title}
Frontmatter: {json.dumps(meta, ensure_ascii=False, default=str)}

SOURCE MARKDOWN:
---
{body[:MAX_SOURCE_CHARS]}
---
"""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        # qwen3 thinking tokens count against num_predict; long thinking used the
        # whole budget and returned an empty response (5 of 31 on 2026-10-22).
        "think": THINK,
        "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": 32768, "num_predict": 1800},
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # 2026-10-22: a reply cut off at num_predict (done_reason "length") or missing
    # sections was saved as the note (5 truncated notes). Retry, then fail.
    problem = "empty_ollama_response"
    for _ in range(3):
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = unwrap_model_output(data.get("response") or "")
        missing = [s for s in BODY_SECTIONS if not re.search(rf"^## {re.escape(s)}\s*$", text, re.M)]
        if not text:
            problem = "empty_ollama_response"
        elif data.get("done_reason") == "length":
            problem = "truncated_ollama_response"
        elif missing:
            problem = f"ollama_response_missing_sections:{','.join(missing)}"
        else:
            return text
        log({"phase": "ollama_retry", "title": title, "problem": problem, "eval_count": data.get("eval_count")})
    raise RuntimeError(problem)


def unwrap_model_output(text: str) -> str:
    """Strip what the model sometimes adds around the body: think blocks, an outer
    code fence, its own frontmatter and an H1. Seen from 2026-10-20 (fenced, no H1)."""
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    lines = [ln.rstrip() for ln in t.splitlines()]
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    t = "\n".join(lines).strip()
    if t.startswith("---"):
        m = re.match(r"---\n.*?\n---\n?", t, re.S)
        if m:
            t = t[m.end():].strip()
    t = re.sub(r"\A(?:#\s+[^\n]*\n+)+", "", t).strip()
    return t


def fallback_body(body: str, source_path: str, bucket: str) -> str:
    return f"""## Summary
(Auto template; model unavailable.) Review source and expand.

## Source excerpt
{body[:2500]}

## Connections
- Related theme notes in Resources/{bucket}/
- Link from social-intake archive

## Attachments
- Original LIF path: `{source_path}`
"""


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def build_note(*, pid: str, title: str, meta: dict, note_body: str, bucket: str,
               digest: str, source_key: str | None, src: Path) -> str:
    """Deterministic frontmatter from Sage's contract, then H1, then the model body."""
    fm: dict = {"proposal_id": pid, "title": title}
    tags = _as_list(meta.get("tags"))
    if tags:
        fm["tags"] = tags
    sources = [s for s in (str(meta.get("source") or "").strip(), str(meta.get("author_url") or "").strip()) if s]
    if sources:
        fm["sources"] = sources
    if meta.get("author"):
        fm["author"] = str(meta["author"])
    fm["curated_from"] = "lif"
    fm["resources_folder"] = bucket
    buckets = [b.lower() for b in _as_list(meta.get("buckets"))]
    if buckets:
        fm["buckets"] = buckets
    for key in ("summary", "posted", "date_saved", "priority"):
        if str(meta.get(key) or "").strip():
            fm[key] = str(meta[key]).strip()
    refs = _as_list(meta.get("muse_refs"))
    if refs:
        fm["muse_refs"] = refs
    if source_key:
        fm["source_key"] = source_key
    fm["lif_sha256"] = digest
    fm["lif_source"] = str(src)
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=10000).strip()
    return f"---\n{head}\n---\n\n# {title}\n\n{note_body.strip()}\n"


def muse_source_key(name: str) -> str | None:
    """'...-muse-posts__35-foo.md' -> 'posts/35-foo'; None for non-Muse files."""
    m = MUSE_NAME_RE.match(name)
    if not m:
        return None
    return m.group(1).replace("__", "/")


def entry_source_key(entry: dict) -> str | None:
    return entry.get("source_key") or muse_source_key(Path(entry.get("src") or "").name)


def title_for(meta: dict, src: Path) -> str:
    if str(meta.get("title") or "").strip():
        return str(meta["title"]).strip()
    key = muse_source_key(src.name)
    stem = key.split("/")[-1] if key else src.stem
    stem = re.sub(r"^\d+-", "", stem)
    return stem.replace("-", " ").strip() or "untitled"


def display_date(meta: dict, src: Path) -> str:
    for key in ("posted", "date_saved"):
        v = str(meta.get(key) or "").strip()
        if v:
            return v
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(src.stat().st_mtime))


def find_prev(curated: dict, source_key: str, digest: str) -> tuple[str, dict] | tuple[None, None]:
    """Latest live ledger entry for the same Muse path (any earlier version)."""
    best_sha, best = None, None
    for sha, e in curated.get("entries", {}).items():
        if sha == digest or e.get("status") not in LIVE_STATUSES:
            continue
        if entry_source_key(e) != source_key:
            continue
        if best is None or (e.get("ts") or "") > (best.get("ts") or ""):
            best_sha, best = sha, e
    return best_sha, best


BUCKET_EMOJI = {
    "music": "🎵",
    "business": "📊",
    "tech": "⚙️",
    "photo": "🖼️",
    "personal": "🌱",
    "writing": "📝",
    "reference": "📖",
    "general": "📚",
}


def strip_frontmatter_and_h1(note: str) -> str:
    t = note
    if t.startswith("---"):
        m = re.match(r"---\n.*?\n---\n?", t, re.S)
        if m:
            t = t[m.end():]
    return re.sub(r"\A\s*#\s+[^\n]*\n+", "", t).strip()


def format_proposal(*, title: str, pid: str, note: str, lift: str, date_iso: str,
                    source: str, buckets: list[str], bucket: str,
                    vault_rel: str = "", short_sha: str = "") -> dict:
    """Matrix card (parent) and thread body. Plain bodies are what phone
    notifications show, so they carry no HTML."""
    keys = [b.lower() for b in buckets] or [bucket.lower() if bucket else "general"]
    emoji = " ".join(BUCKET_EMOJI.get(k, BUCKET_EMOJI["general"]) for k in list(dict.fromkeys(keys))[:2])
    lift = lift.strip() or extract_summary_line(note)
    head_plain = (
        f"{emoji} {title}\n"
        f"{date_iso} · proposed by LIF\n"
        f"{lift}\n"
        f"{pid} · react ✅ accept, ❌ deny"
    )
    head_html = (
        f"<strong>{html.escape(emoji + ' ' + title)}</strong>"
        f"<br><em>{html.escape(date_iso)} · proposed by LIF</em>"
        f"<br>{html.escape(lift)}"
        f"<br><code>{pid}</code> · react ✅ accept, ❌ deny"
    )
    body_md = f"### {title}\n_Source_: {source or 'n/a'}\n_Date_: {date_iso}\n\n{strip_frontmatter_and_h1(note)}"
    if vault_rel or short_sha:
        body_md += "\n\n---\n"
        if vault_rel:
            body_md += "_staging: `" + vault_rel + "`_\n"
        if short_sha:
            body_md += "_sha: `" + short_sha + "`_\n"
    return {
        "head_html": head_html,
        "head_plain": head_plain,
        "body_md": body_md,
        "body_html": _md_to_html(body_md),
        "title": title,
        "date_iso": date_iso,
    }


def extract_summary_line(note: str, limit: int = 300) -> str:
    m = re.search(r"##\s+Summary\s*\n+(.*?)(?:\n\n|\n##\s|\Z)", note, re.S)
    s = " ".join((m.group(1) if m else "").split())
    return s[:limit] + ("..." if len(s) > limit else "")


def _md_to_html(md: str) -> str:
    if not md:
        return ""
    s = html.escape(md, quote=False)
    s = re.sub(r"^### (.+)$", r"<h3>\1</h3>", s, flags=re.M)
    s = re.sub(r"^## (.+)$", r"<h2>\1</h2>", s, flags=re.M)
    s = re.sub(r"^# (.+)$", r"<h1>\1</h1>", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"(?<![\w<])_([^_\n]+?)_(?![\w>])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"^---$", "<hr>", s, flags=re.M)
    s = re.sub(r"((?:^[-*] .+\n?)+)", lambda m: "<ul>" + "".join(
        f"<li>{ln[2:].strip()}</li>" for ln in m.group(1).splitlines() if ln.strip()) + "</ul>\n", s, flags=re.M)
    blocks = [b.strip() for b in re.split(r"\n{2,}", s) if b.strip()]
    out = []
    for b in blocks:
        if re.match(r"<(h[1-6]|ul|hr)", b):
            out.append(b.replace("\n", ""))
        else:
            out.append("<p>" + b.replace("\n", "<br>") + "</p>")
    return "".join(out)


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "note"


def commit_proposal(rel_path: str, content: str, title: str) -> str:
    env = os.environ.copy()
    env["VAULT_SYNC_MESSAGE"] = f"lif curate: {title}"[:200]
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "/usr/local/sbin/gitea-vault-sync-askpass")
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        [COMMIT_BIN, rel_path],
        input=content.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=90,
    )
    out = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"commit_failed rc={proc.returncode}: {out[:400]}")
    return out.strip()[:500]


def matrix_notify(entry: dict, *, note: str, meta: dict, src: Path) -> None:
    if not NOTIFY:
        return
    try:
        source = str(meta.get("source") or "").strip()
        if str(meta.get("author_url") or "").strip():
            source = f"{source} ({meta['author_url']})" if source else str(meta["author_url"])
        formatted = format_proposal(
            title=entry["title"],
            pid=entry["id"],
            note=note,
            lift=str(meta.get("summary") or ""),
            date_iso=display_date(meta, src),
            source=source or str(src),
            buckets=entry.get("buckets") or [],
            bucket=str(entry.get("bucket") or ""),
            vault_rel=str(entry.get("vault_sync_path") or ""),
            short_sha=(entry.get("sha256") or "")[:8],
        )
        out = rr.notify_matrix_with_images({**entry, **formatted})
        log({"phase": "matrix_notify", "ok": bool(out.get("ok")), "id": entry.get("id"),
             "edited": bool(entry.get("matrix_event_id")),
             "detail": {k: out.get(k) for k in ("event_id", "images", "image_errors", "error")}})
    except Exception as e:
        log({"phase": "matrix_notify", "ok": False, "id": entry.get("id"), "error": str(e)[:300]})


def curate_file(src: Path, digest: str, prev_sha: str | None, prev: dict | None, run: dict,
                update_of: bool = False) -> str:
    """Curate one source. Returns 'curated', 'deferred' or 'error'.
    prev is the live entry for an earlier version of the same Muse path; the new
    version takes over its id, staging file and Matrix post."""
    text = src.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    title = title_for(meta, src)
    source_key = muse_source_key(src.name)
    bucket = resources_bucket(title, meta, body, str(src))
    passthrough = sage_body(body) if (source_key or "").startswith("posts/") else None
    if passthrough is None and PASSTHROUGH_ONLY:
        return "needs_model"  # hourly runs never touch the GPU; the 06:40 and 18:40 runs take it
    try:
        if passthrough is None and not run["comfy_freed"]:
            comfy_free_vram()
            run["comfy_freed"] = True
        retries = run["retries"]
        try:
            note_body = passthrough if passthrough is not None else ollama_curate(title, meta, body, str(src), bucket)
            retries.pop(digest, None)
        except Exception as e:
            attempts = int(retries.get(digest, 0)) + 1
            if attempts < MAX_OLLAMA_ATTEMPTS:
                retries[digest] = attempts
                save_retries(retries)
                log({"phase": "ollama_deferred", "src": str(src), "attempt": attempts, "error": str(e)[:200]})
                return "deferred"
            retries.pop(digest, None)
            log({"phase": "ollama_fallback", "src": str(src), "attempt": attempts, "error": str(e)[:200]})
            note_body = fallback_body(body, str(src), bucket)
        save_retries(retries)
        pid = prev["id"] if (prev and not update_of) else rr.proposal_id_for_sha(digest)
        if update_of:
            # Update card for an accepted post: stage under the accepted file name so approving
            # overwrites the vault copy in place (rr.approve replaces an existing destination).
            accepted = prev.get("accepted_path") or prev.get("vault_sync_path") or ""
            rel = f"inbox/resources-staging/{bucket}/{Path(accepted).name}"
        elif prev and prev.get("staging_path"):
            rel = prev["staging_path"]
        else:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            rel = f"inbox/resources-staging/{bucket}/{day}-{slugify(title)}.md"
        note = build_note(pid=pid, title=title, meta=meta, note_body=note_body, bucket=bucket,
                          digest=digest, source_key=source_key, src=src)
        detail = commit_proposal(rel, note, title)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
            "src": str(src),
            "vault_sync_path": rel,
            "staging_path": rel,
            "title": title,
            "bucket": bucket,
            "buckets": [b.lower() for b in _as_list(meta.get("buckets"))],
            "sha256": digest,
            "id": pid,
            "status": "pending",
        }
        if source_key:
            entry["source_key"] = source_key
        if update_of:
            entry["update_of"] = prev.get("id")
            entry["update_of_sha"] = prev_sha
        elif prev:
            for k in ("matrix_event_id", "matrix_thread_event_id"):
                if prev.get(k):
                    entry[k] = prev[k]
            if prev_sha != digest:
                entry["supersedes"] = prev_sha
        # Reload: matrix_notify for the previous file saved event ids into the ledger.
        curated = rr.load_ledger()
        if prev and not update_of and prev_sha != digest and prev_sha in curated["entries"]:
            curated["entries"][prev_sha]["status"] = "superseded"
            curated["entries"][prev_sha]["superseded_by"] = digest
            curated["entries"][prev_sha]["superseded_ts"] = entry["ts"]
        curated["entries"][digest] = entry
        rr.save_ledger(curated)
        log({"phase": "curated", "ok": True, "src": str(src), "sha256": digest, "out": rel, "id": pid,
             "bucket": bucket, "supersedes": entry.get("supersedes"), "detail": detail[:200]})
        card = {**entry, "title": f"Update to accepted note: {title}"} if update_of else entry
        matrix_notify(card, note=note, meta=meta, src=src)
        print(f"curated {src.name} -> {rel} id={pid}" + (" (update of accepted)" if update_of else " (update)" if prev else ""))
        return "curated"
    except Exception as e:
        log({"phase": "error", "ok": False, "src": str(src), "error": (str(e) + "\n" + traceback.format_exc())[:2000]})
        print(f"error {src}: {e}")
        return "error"


def record_without_proposal(src: Path, digest: str, prev_sha: str, prev: dict, status: str) -> None:
    """Ledger record for a version that gets no proposal of its own:
    edit_after_decision (Sage changed an accepted or denied post) or
    superseded (an older version than the live one, e.g. two versions in one run)."""
    curated = rr.load_ledger()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
        "src": str(src),
        "sha256": digest,
        "id": prev.get("id"),
        "source_key": entry_source_key(prev),
        "title": prev.get("title"),
        "bucket": prev.get("bucket") or "music",
        "status": status,
    }
    if status == "superseded":
        rec["superseded_by"] = prev_sha
    else:
        rec["decided_status"] = prev.get("status")
    curated["entries"][digest] = rec
    rr.save_ledger(curated)
    log({"phase": status, "src": str(src), "id": prev.get("id"), "prev_status": prev.get("status")})


def is_older_than(src: Path, entry: dict) -> bool:
    other = Path(entry.get("src") or "")
    try:
        return other.is_file() and other.stat().st_mtime > src.stat().st_mtime
    except OSError:
        return False


def refresh(pid: str) -> int:
    """Re-curate a live pending proposal from its own source, in place."""
    data = rr.migrate_ledger()
    sha, entry = rr.find_by_id(data, pid)
    if not entry or entry.get("status") != "pending":
        print(f"refresh: {pid} not found or not pending")
        return 1
    src = Path(entry["src"])
    if not src.is_file():
        print(f"refresh: missing source {src}")
        return 1
    run = {"comfy_freed": False, "retries": load_retries()}
    outcome = curate_file(src, sha, sha, entry, run)
    log({"phase": "refresh", "id": pid, "outcome": outcome})
    return 0 if outcome == "curated" else 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--refresh":
        return refresh(sys.argv[2])
    force = force_enabled()
    curated = rr.migrate_ledger()
    candidates = collect_candidates()
    done = skipped = deferred = updated = not_proposal = edits_after_decision = waiting_model = 0
    run = {"comfy_freed": False, "retries": load_retries()}
    for src in candidates:
        if done >= MAX_FILES:
            break
        source_key = muse_source_key(src.name)
        if source_key is not None and not source_key.startswith("posts/"):
            # themes/, README, index note: Sage hub notes, not sources
            not_proposal += 1
            continue
        name_l = src.name.lower()
        path_l = str(src).lower()
        junk = (
            name_l in {"readme.md"}
            or "lif-smoke" in path_l
            or "hello from r2" in path_l
            or name_l.endswith("hello-from-r2.md")
            or "/smoke" in path_l
        )
        if junk and "posts" not in path_l and not force:
            skipped += 1
            continue
        digest = sha256_file(src)
        known = curated.get("entries", {}).get(digest)
        # Even with --force never re-curate over a decision: on 2026-10-11 a forced
        # run reset accepted posts 22 and 23 to pending under new ids.
        if known and (not force or known.get("status") != "pending"):
            skipped += 1
            continue
        prev_sha, prev = find_prev(curated, source_key, digest) if source_key else (None, None)
        if prev and is_older_than(src, prev):
            record_without_proposal(src, digest, prev_sha, prev, "superseded")
            curated = rr.load_ledger()
            skipped += 1
            continue
        if prev and prev.get("status") == "accepted" and UPDATE_ACCEPTED:
            # 2026-10-29 (Dan): verified corrections must reach the vault. Propose an update card;
            # approving it overwrites the accepted vault copy. Denied posts stay record-only.
            outcome = curate_file(src, digest, prev_sha, prev, run, update_of=True)
            curated = rr.load_ledger()
            if outcome == "needs_model":
                waiting_model += 1
                continue
            if outcome == "deferred":
                deferred += 1
                break
            if outcome == "curated":
                done += 1
                updated += 1
            continue
        if prev and prev.get("status") in ("accepted", "denied"):
            record_without_proposal(src, digest, prev_sha, prev, "edit_after_decision")
            curated = rr.load_ledger()
            edits_after_decision += 1
            continue
        outcome = curate_file(src, digest, prev_sha, prev, run)
        curated = rr.load_ledger()
        if outcome == "needs_model":
            waiting_model += 1
            continue
        if outcome == "deferred":
            deferred += 1
            break  # Ollama is struggling; leave the rest for the next run
        if outcome == "curated":
            done += 1
            if prev:
                updated += 1
    log({"phase": "done", "curated": done, "updated": updated, "skipped": skipped, "deferred": deferred,
         "not_proposal": not_proposal, "edit_after_decision": edits_after_decision, "force": force,
         "waiting_model": waiting_model, "passthrough_only": PASSTHROUGH_ONLY})
    print(f"done curated={done} updated={updated} skipped={skipped} deferred={deferred} "
          f"not_proposal={not_proposal} edit_after_decision={edits_after_decision} force={force}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
