#!/usr/bin/env python3
"""Shared LIF resources-review ledger helpers (approve/deny/queue/cards)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CURATE_LEDGER = Path(os.environ.get("LIF_CURATE_LEDGER", "/tank/ingest/state/curated-sha256.json"))
REVIEW_AUDIT = Path(os.environ.get("RESOURCES_REVIEW_AUDIT", "/var/log/resources-review.jsonl"))
VAULT_REPO = Path(os.environ.get("VAULT_SYNC_REPO", "/tank/agent-net/vault-sync/repo"))
COMMIT_BIN = os.environ.get("VAULT_SYNC_COMMIT_BIN", "/usr/local/sbin/vault-sync-commit")
STAGING_PREFIX = "inbox/resources-staging/"
ACCEPTED_PREFIX = "inbox/resources-accepted/"
DENIED_PREFIX = "inbox/resources-denied/"
# Live PARA path inside vault-sync (pulled into Obsidian zueyrebro)
LIVE_RESOURCES_PREFIX = os.environ.get("LIF_LIVE_RESOURCES_PREFIX", "3 - Resources/")
LIVE_MUSIC_FOLDER = os.environ.get("LIF_LIVE_MUSIC_FOLDER", "Music")  # Obsidian casing



def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())


def audit(event: dict) -> None:
    REVIEW_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_ts(), **event}, ensure_ascii=False) + "\n")


def load_ledger() -> dict:
    CURATE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    if not CURATE_LEDGER.is_file():
        return {"version": 2, "entries": {}}
    try:
        data = json.loads(CURATE_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 2, "entries": {}}
    data.setdefault("entries", {})
    data["version"] = max(int(data.get("version") or 1), 2)
    return data


def save_ledger(data: dict) -> None:
    tmp = CURATE_LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(CURATE_LEDGER)


def proposal_id_for_sha(sha256: str, ts: str | None = None) -> str:
    day = (ts or now_ts())[:10].replace("-", "")
    return f"res-{day}-{sha256[:8]}"


def ensure_entry_shape(sha: str, entry: dict) -> dict:
    e = dict(entry)
    e.setdefault("sha256", sha)
    if not e.get("id"):
        e["id"] = proposal_id_for_sha(sha, e.get("ts"))
    e.setdefault("status", "pending")
    e.setdefault("bucket", "music")
    return e


def migrate_ledger() -> dict:
    data = load_ledger()
    changed = False
    for sha, entry in list(data.get("entries", {}).items()):
        shaped = ensure_entry_shape(sha, entry)
        if shaped != entry:
            data["entries"][sha] = shaped
            changed = True
    if changed:
        save_ledger(data)
    return data


def find_by_id(data: dict, pid: str) -> tuple[str, dict] | tuple[None, None]:
    # 2026-10-22: a Sage edit keeps the proposal id, so several versions can share
    # it. Prefer the live one; superseded/edit_after_decision records only match
    # when nothing live does.
    pid = pid.strip()
    fallback = (None, None)
    for sha, entry in data.get("entries", {}).items():
        e = ensure_entry_shape(sha, entry)
        if e.get("id") == pid or sha.startswith(pid) or sha == pid:
            if e.get("status") in ("pending", "accepted", "denied"):
                return sha, e
            if fallback[0] is None:
                fallback = (sha, e)
    if fallback[0] is not None:
        return fallback
    # short suffix match res-XXXXXXXX
    m = re.search(r"([a-f0-9]{8})$", pid)
    if m:
        suf = m.group(1)
        hits = []
        for sha, entry in data.get("entries", {}).items():
            if sha.startswith(suf):
                hits.append((sha, ensure_entry_shape(sha, entry)))
        if len(hits) == 1:
            return hits[0]
    return None, None


def list_by_status(data: dict, status: str | None = "pending") -> list[dict]:
    out = []
    for sha, entry in data.get("entries", {}).items():
        e = ensure_entry_shape(sha, entry)
        if status is None or e.get("status") == status:
            out.append(e)
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return out


def read_note_text(rel_path: str | None) -> str:
    if not rel_path:
        return ""
    p = VAULT_REPO / rel_path
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def extract_summary(md: str, limit: int = 900) -> str:
    if not md:
        return "(no body)"
    # drop frontmatter
    body = md
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
    # prefer ## Summary section
    m = re.search(r"##\s+Summary\s*\n(.*?)(?:\n##\s+|\Z)", body, re.S | re.I)
    if m:
        text = m.group(1).strip()
    else:
        text = body.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def extract_claims(md: str, max_items: int = 5) -> list[str]:
    if not md:
        return []
    m = re.search(r"##\s+Key claims\s*\n(.*?)(?:\n##\s+|\Z)", md, re.S | re.I)
    if not m:
        m = re.search(r"##\s+Claims\s*\n(.*?)(?:\n##\s+|\Z)", md, re.S | re.I)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith(("-", "*")):
            items.append(line.lstrip("-* ").strip())
        if len(items) >= max_items:
            break
    return items



def _image_ext_ok(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic"}


def _tokens_for_entry(entry: dict) -> list[str]:
    toks: list[str] = []
    for key in ("title", "src", "vault_sync_path", "staging_path", "id"):
        v = str(entry.get(key) or "")
        toks.append(v)
    blob = " ".join(toks).lower().replace("@", " ")
    parts = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", blob)
    stop = {
        "muse", "posts", "themes", "music", "sustainability", "assets", "incoming",
        "notes", "images", "tank", "studio", "homelab", "vault", "drop", "lif",
        "inbox", "resources", "staging", "accepted", "markdown", "the", "and", "for",
        "from", "with", "word", "actually", "work", "what", "how", "stop", "title",
    }
    out: list[str] = []

    def add_simple(x: str) -> None:
        x = x.strip("_-")
        if len(x) < 4 or x in stop or x.isdigit():
            return
        if x not in out:
            out.append(x)

    def add(x: str) -> None:
        x = x.strip("_-")
        if not x:
            return
        add_simple(x)
        collapsed = x.replace("-", "").replace("_", "")
        if collapsed != x:
            add_simple(collapsed)
        for part in re.split(r"[-_]+", x):
            if part != x:
                add_simple(part)

    for x in parts:
        add(x)
    for m in re.finditer(r"muse-(?:posts|themes)__\d+-([a-z0-9_-]+)", blob):
        add(m.group(1))
    for m in re.finditer(r"assets__([a-z0-9_-]+?)(?:__slide|\.png|\.jpg|$)", blob):
        add(m.group(1))
        # drop dates, pure hashes, path junk
    cleaned = []
    for x in out:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            continue
        if re.fullmatch(r"\d{8}", x):
            continue
        if re.fullmatch(r"\d{8}t\d{6}z", x):
            continue
        if re.fullmatch(r"[a-f0-9]{8,}", x):
            continue
        if x.startswith("res-") or x.startswith("res20"):
            continue
        if x in {"studio-homelab", "studiohomelab", "vault-drop", "vaultdrop", "resources-accepted", "resourcesaccepted", "resources-staging", "resourcesstaging"}:
            continue
        cleaned.append(x)
    return cleaned[:40]


def find_related_images(entry: dict, limit: int | None = None) -> list[Path]:
    """Locate LIF screenshots related to a curated proposal."""
    if limit is None:
        limit = int(os.environ.get("RESOURCES_REVIEW_MAX_IMAGES", "4"))
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            rp = str(path.resolve())
        except Exception:
            rp = str(path)
        if rp in seen or not path.is_file() or not _image_ext_ok(path):
            return
        name_l = path.name.lower()
        if "lif-smoke" in name_l or name_l.startswith("smoke"):
            return
        try:
            if path.stat().st_size > 12 * 1024 * 1024:
                return
        except Exception:
            return
        seen.add(rp)
        found.append(path)

    # 1) markdown image links in curated note + source note
    md_paths = []
    for key in ("vault_sync_path", "staging_path", "accepted_path", "src"):
        val = entry.get(key)
        if not val:
            continue
        if key == "src":
            md_paths.append(Path(str(val)))
        else:
            md_paths.append(VAULT_REPO / str(val))
    link_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    for mp in md_paths:
        if not mp.is_file():
            continue
        try:
            text = mp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in link_re.finditer(text):
            rel = m.group(1).strip().strip('"').strip("'")
            if rel.startswith("http://") or rel.startswith("https://"):
                continue
            cand = (mp.parent / rel).resolve() if not rel.startswith("/") else Path(rel)
            add(cand)

    if len(found) >= limit:
        return found[:limit]

    src = Path(str(entry.get("src") or ""))
    search_dirs: list[Path] = []
    if src.parts:
        if src.parent.name == "notes":
            search_dirs.append(src.parent.parent / "images")
        search_dirs.append(src.parent)
    incoming = Path(os.environ.get("LIF_INCOMING", "/tank/studio-homelab/vault-drop/lif/incoming"))
    if incoming.is_dir():
        days = sorted([d for d in incoming.iterdir() if d.is_dir()], reverse=True)[:5]
        for d in days:
            search_dirs.append(d / "images")

    tokens = _tokens_for_entry(entry)
    # Strong anchors: multi-part slugs and long handles
    strong = []
    for tok in tokens:
        if "-" in tok or "_" in tok or len(tok) >= 8:
            strong.append(tok)
    # author-like tokens from src path muse-posts__N-author-slug
    src_l = str(entry.get("src") or "").lower()
    m = re.search(r"muse-posts__\d+-([a-z0-9_-]+)", src_l)
    if m:
        strong.insert(0, m.group(1))
        for part in m.group(1).split("-"):
            # 2026-10-19: "music" is in every Muse slide filename; it matched unrelated posts.
            if len(part) >= 5 and part != "music":
                strong.append(part)
    m = re.search(r"muse-themes__([a-z0-9_-]+)", src_l)
    if m:
        strong.insert(0, m.group(1))
    # title handle @foo
    title = str(entry.get("title") or "")
    for m in re.finditer(r"@([A-Za-z0-9_]+)", title):
        strong.append(m.group(1).lower())
        strong.append(m.group(1).lower().replace("_", ""))

    # unique strong
    strong_u = []
    for s in strong:
        s = s.lower().strip("_-")
        if s and s not in strong_u:
            strong_u.append(s)
    strong = []
    for s in strong_u:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.fullmatch(r"\d{8}", s):
            continue
        if re.fullmatch(r"\d{8}t\d{6}z", s) or re.fullmatch(r"[a-f0-9]{8,}", s):
            continue
        if s.startswith("2026") or s.startswith("2025") or s.startswith("2024"):
            continue
        if s in {"studio-homelab", "studiohomelab", "vault-drop", "vaultdrop", "integrations", "resources-accepted", "resourcesaccepted"}:
            continue
        strong.append(s)

    candidates: list[tuple[int, float, Path]] = []
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            files = list(d.iterdir())
        except Exception:
            continue
        for f in files:
            if not _image_ext_ok(f):
                continue
            name = f.name.lower()
            if "lif-smoke" in name:
                continue
            name_c = name.replace("-", "").replace("_", "")
            score = 0
            # require at least one strong hit
            strong_hit = False
            for tok in strong:
                tc = tok.replace("-", "").replace("_", "")
                if tok and tok in name:
                    score += 10
                    strong_hit = True
                elif len(tc) >= 6 and tc in name_c:
                    score += 8
                    strong_hit = True
            if not strong_hit:
                continue
            # small bonus for slide assets under matching theme
            if "assets__" in name:
                score += 2
            try:
                mtime = f.stat().st_mtime
            except Exception:
                mtime = 0
            candidates.append((score, mtime, f))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen_core: set[str] = set()
    for score, _, f in candidates:
        # keep slide number so different slides are unique
        core = re.sub(r"^\d{8}t\d{6}z-[0-9a-f]+-", "", f.name, flags=re.I)
        if core in seen_core:
            continue
        seen_core.add(core)
        add(f)
        if len(found) >= limit:
            break
    return found[:limit]



def _matrix_send_with_retry(send_fn, *, retries: int = 4):
    """Call Matrix send function with backoff on HTTP 429."""
    import time as _time
    import re as _re
    last = None
    for attempt in range(retries):
        try:
            return send_fn()
        except Exception as e:
            last = e
            msg = str(e)
            if "429" not in msg and "M_LIMIT_EXCEEDED" not in msg:
                raise
            m = _re.search(r"retry_after_ms[\"\s:]+(\d+)", msg)
            wait_ms = int(m.group(1)) if m else (500 * (attempt + 1))
            _time.sleep(min(15, wait_ms / 1000.0 + 0.2))
    raise last

def notify_matrix_with_images(entry: dict, room_id: str | None = None) -> dict:
    """Post card + related screenshots as a Matrix thread."""
    from matrix_client_simple import MatrixClient, resolve_resources_room_id

    room = room_id or resolve_resources_room_id()
    if not room:
        return {"ok": False, "error": "no_room_id"}
    client = MatrixClient()
    card_full = card_text(entry)
    imgs = find_related_images(entry)
    if entry.get("head_html"):
        # Plain body is what push notifications show; HTML goes in formatted_body only.
        parent_text = entry.get("head_plain") or entry["head_html"]
    else:
        parent_text = card_full
        if imgs:
            parent_text += f"\n\nScreenshots: {len(imgs)} attached in thread"
        else:
            parent_text += "\n\nScreenshots: none matched in LIF images"
    prev_parent_eid, prev_thread_eid = lookup_matrix_event_id(entry.get("sha256"))
    parent_html = entry.get("head_html") or None
    body_html = entry.get("body_html") or None
    if prev_parent_eid:
        # Edit in place. Element renders m.replace as a strikethrough+new pair.
        _matrix_send_with_retry(lambda: client.edit_text(room, prev_parent_eid, parent_text, html=parent_html))
        event_id = prev_parent_eid
        thread_eid_return = prev_thread_eid
    else:
        resp = _matrix_send_with_retry(lambda: client.send_text(room, parent_text, html=parent_html))
        event_id = resp.get("event_id")
        thread_eid_return = None
    body_md = (entry.get("body_md") or "").strip()
    if event_id and body_md:
        try:
            if prev_thread_eid:
                _matrix_send_with_retry(lambda: client.edit_text(room, prev_thread_eid, body_md, html=body_html, thread_root=event_id))
            else:
                resp2 = _matrix_send_with_retry(lambda: client.send_text(room, body_md, html=body_html, thread_root=event_id))
                thread_eid_return = resp2.get("event_id")
        except Exception as e:
            print(f"warn: thread body send/edit failed: {e}")
    sha = entry.get("sha256")
    if sha:
        mark_notified(sha, event_id, thread_event_id=thread_eid_return)
    # Triage reactions on parent. Element dedupes via m.relates_to.event_id+key.
    if event_id:
        for key in ("⏳", "✅", "❌"):
            try:
                _matrix_send_with_retry(lambda: client.send_reaction(room, event_id, key), retries=1)
            except Exception:
                pass
    uploaded = []
    errors = []
    if prev_parent_eid:
        imgs = []  # in-place update: the thread already has its images
    for img in imgs:
        try:
            ir = client.send_image(room, img, caption=img.name, thread_root=event_id)
            uploaded.append({"file": img.name, "event_id": ir.get("event_id")})
            time.sleep(0.35)  # stay under Matrix rate limits
        except Exception as e:
            err = str(e)
            # one retry on 429
            if "429" in err or "M_LIMIT_EXCEEDED" in err:
                import re as _re
                m = _re.search(r"retry_after_ms[\"\s:]+(\d+)", err)
                wait_ms = int(m.group(1)) if m else 2000
                time.sleep(min(10, wait_ms / 1000.0 + 0.25))
                try:
                    ir = client.send_image(room, img, caption=img.name, thread_root=event_id)
                    uploaded.append({"file": img.name, "event_id": ir.get("event_id"), "retried": True})
                    time.sleep(0.5)
                    continue
                except Exception as e2:
                    errors.append({"file": img.name, "error": str(e2)[:200]})
                    continue
            errors.append({"file": img.name, "error": err[:200]})
    if uploaded or errors:
        # short footer in thread
        try:
            lines = [f"thread images for `{entry.get('id')}`: {len(uploaded)} ok"]
            if errors:
                lines.append(f"{len(errors)} failed")
            client.send_text(room, "\n".join(lines), thread_root=event_id)
        except Exception:
            pass
    return {"ok": True, "event_id": event_id, "room_id": room, "images": uploaded, "image_errors": errors}


def card_text(entry: dict) -> str:
    rel = entry.get("vault_sync_path") or entry.get("staging_path")
    md = read_note_text(rel)
    summary = extract_summary(md)
    claims = extract_claims(md)
    claims_block = "\n".join(f"- {c}" for c in claims) if claims else "- (see note)"
    pid = entry.get("id")
    title = entry.get("title") or "untitled"
    bucket = entry.get("bucket") or "music"
    status = entry.get("status") or "pending"
    return (
        f"📝 `{pid}` [{status}]\n"
        f"**{title}**\n"
        f"bucket: `{bucket}` → proposed `3 - Resources/{"Music" if bucket=="music" else bucket}/`\n"
        f"staging: `{rel}`\n\n"
        f"Summary:\n{summary}\n\n"
        f"Claims:\n{claims_block}\n\n"
        f"Commands:\n"
        f"`!approve {pid}`\n"
        f"`!deny {pid}`\n"
        f"`!show {pid}`\n"
        f"`!queue`"
    )


def _git_mv_or_copy(rel_src: str, rel_dst: str, message: str) -> str:
    """Move note inside vault-sync repo and push via commit helper semantics."""
    src = VAULT_REPO / rel_src
    dst = VAULT_REPO / rel_dst
    if not src.is_file():
        raise FileNotFoundError(f"missing_staging:{rel_src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "/usr/local/sbin/gitea-vault-sync-askpass")
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Prefer git mv in repo
    subprocess.run(["git", "-C", str(VAULT_REPO), "add", "-A"], check=False, capture_output=True)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))
    subprocess.run(["git", "-C", str(VAULT_REPO), "add", "-A", rel_src, rel_dst], check=False, capture_output=True)
    # Use commit bin with content rewrite to ensure push path works if git mv dirty
    # Fallback: write via vault-sync-commit then remove old
    content = dst.read_text(encoding="utf-8", errors="replace")
    env["VAULT_SYNC_MESSAGE"] = message[:200]
    proc = subprocess.run(
        [COMMIT_BIN, rel_dst],
        input=content.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=90,
    )
    out = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"commit_failed:{out[:400]}")
    # remove old path if still present
    if src.exists() and src.resolve() != dst.resolve():
        try:
            src.unlink()
            subprocess.run(
                [COMMIT_BIN, rel_src],
                input=b"",
                capture_output=True,
                env={**env, "VAULT_SYNC_MESSAGE": f"lif review: remove staging {rel_src}"[:200]},
                timeout=90,
            )
        except Exception:
            # empty commit delete may fail; best-effort rm + git
            try:
                subprocess.run(["git", "-C", str(VAULT_REPO), "rm", "-f", rel_src], capture_output=True, check=False)
                subprocess.run(
                    ["git", "-C", str(VAULT_REPO), "-c", "user.email=lif@local", "-c", "user.name=lif",
                     "commit", "-m", f"lif review: drop staging {rel_src}"],
                    capture_output=True, check=False,
                )
                subprocess.run(
                    ["git", "-C", str(VAULT_REPO), "push", "origin", "HEAD:main"],
                    capture_output=True, env=env, check=False,
                )
            except Exception:
                pass
    return out.strip()[:500]


def _not_live_reason(data: dict, entry: dict) -> str:
    status = entry.get("status")
    if status == "superseded":
        newer = data.get("entries", {}).get(entry.get("superseded_by") or "", {})
        return f"superseded by newer version `{newer.get('id') or entry.get('superseded_by')}`"
    return f"not a proposal (status {status})"


MUSE_REPO = Path(os.environ.get("LIF_MUSE_REPO", "/tank/muse/inbox/git/social-intake"))
ASSET_DIR = "music-sustainability-assets"
VAULT_ASSETS = f"{LIVE_RESOURCES_PREFIX}Assets"
_SOURCE_EMBED = re.compile(rf"^!\[[^\]]*\]\((?:\.\./)?{ASSET_DIR}/([\w-][\w.-]*)/([\w-][\w.-]*\.(?:jpg|jpeg|png|webp|gif))\)$", re.I)
_VAULT_EMBED = re.compile(rf"^!\[\[{re.escape(VAULT_ASSETS)}/([\w-][\w.-]*)/([\w-][\w.-]*)\]\]\s*$", re.M)


def vault_embed(line: str) -> str | None:
    """Vault path embed for a source image line, or None. Sage wrote the line, so only plain asset paths pass."""
    m = _SOURCE_EMBED.match(line)
    return f"![[{VAULT_ASSETS}/{m.group(1)}/{m.group(2)}]]" if m else None


def copy_note_assets(rel_note: str, message: str) -> int:
    """Copy the images a vault note embeds from the Muse mirror into the vault, in one commit. Returns files copied."""
    text = (VAULT_REPO / rel_note).read_text(encoding="utf-8", errors="replace")
    copied = 0
    for slug, name in _VAULT_EMBED.findall(text):
        src, dst = MUSE_REPO / ASSET_DIR / slug / name, VAULT_REPO / VAULT_ASSETS / slug / name
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied += 1
    if copied:
        env = {**os.environ, "VAULT_SYNC_MESSAGE": message[:200], "GIT_TERMINAL_PROMPT": "0"}
        proc = subprocess.run([COMMIT_BIN, rel_note], input=text.encode("utf-8"), capture_output=True, env=env, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"asset_commit_failed:{(proc.stderr or b'').decode('utf-8', 'replace')[:300]}")
    return copied


def approve(pid: str, by: str = "api") -> dict:
    data = migrate_ledger()
    sha, entry = find_by_id(data, pid)
    if not entry:
        return {"ok": False, "error": "not_found", "id": pid}
    if entry.get("status") not in ("pending", "accepted", "denied"):
        return {"ok": False, "error": _not_live_reason(data, entry), "entry": entry}
    if entry.get("status") == "accepted":
        return {"ok": True, "noop": True, "entry": entry, "message": "already accepted"}
    rel = entry.get("vault_sync_path") or entry.get("staging_path")
    if not rel:
        return {"ok": False, "error": "no_path", "entry": entry}
    bucket = entry.get("bucket") or "music"
    base = Path(rel).name
    # Live Obsidian path uses Music casing for music bucket
    live_bucket = LIVE_MUSIC_FOLDER if bucket == "music" else bucket
    live_dest = f"{LIVE_RESOURCES_PREFIX}{live_bucket}/{base}"
    archive_dest = f"{ACCEPTED_PREFIX}{bucket}/{base}"
    # Promote into live PARA path in vault-sync (source of truth for Mac pull)
    detail = _git_mv_or_copy(rel, live_dest, f"lif review accept->Resources: {entry.get('id')} {entry.get('title')}")
    # A failed image copy leaves the accepted note in place; its embeds show as missing until re-copied.
    try:
        copy_note_assets(live_dest, f"lif review accept images: {entry.get('id')}")
    except Exception as e:
        audit({"action": "copy_assets", "ok": False, "id": entry.get("id"), "error": str(e)[:300]})
    # Also keep an accepted archive copy when possible
    try:
        live_path = VAULT_REPO / live_dest
        if live_path.is_file():
            content = live_path.read_text(encoding="utf-8", errors="replace")
            env = os.environ.copy()
            env["VAULT_SYNC_MESSAGE"] = f"lif review archive accept: {entry.get('id')}"[:200]
            env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "/usr/local/sbin/gitea-vault-sync-askpass")
            env["GIT_TERMINAL_PROMPT"] = "0"
            subprocess.run(
                [COMMIT_BIN, archive_dest],
                input=content.encode("utf-8"),
                capture_output=True,
                env=env,
                timeout=90,
            )
    except Exception:
        archive_dest = None
    entry["status"] = "accepted"
    entry["accepted_path"] = live_dest
    entry["archive_path"] = archive_dest
    entry["vault_sync_path"] = live_dest
    entry["obsidian_path"] = f"3 - Resources/{live_bucket}/{base}"
    entry["decision_ts"] = now_ts()
    entry["decision_by"] = by
    entry["deny_reason"] = None
    data["entries"][sha] = entry
    # Update card (2026-10-29): approving it replaced the accepted vault copy; retire the old entry.
    old_sha = entry.get("update_of_sha")
    if old_sha and old_sha in data["entries"] and data["entries"][old_sha].get("status") == "accepted":
        data["entries"][old_sha]["status"] = "superseded"
        data["entries"][old_sha]["superseded_by"] = sha
        data["entries"][old_sha]["superseded_ts"] = entry["decision_ts"]
    save_ledger(data)
    audit({"action": "approve", "ok": True, "id": entry["id"], "by": by, "dest": live_dest,
           "update_of": entry.get("update_of")})
    return {"ok": True, "entry": entry, "detail": detail}


def deny(pid: str, reason: str = "", by: str = "api") -> dict:
    data = migrate_ledger()
    sha, entry = find_by_id(data, pid)
    if not entry:
        return {"ok": False, "error": "not_found", "id": pid}
    if entry.get("status") not in ("pending", "accepted", "denied"):
        return {"ok": False, "error": _not_live_reason(data, entry), "entry": entry}
    if entry.get("status") == "denied":
        return {"ok": True, "noop": True, "entry": entry, "message": "already denied"}
    rel = entry.get("vault_sync_path") or entry.get("staging_path")
    bucket = entry.get("bucket") or "music"
    detail = ""
    if rel and (VAULT_REPO / rel).is_file():
        base = Path(rel).name
        dest = f"{DENIED_PREFIX}{bucket}/{base}"
        try:
            detail = _git_mv_or_copy(rel, dest, f"lif review deny: {entry.get('id')} {entry.get('title')}")
            entry["denied_path"] = dest
            entry["vault_sync_path"] = dest
        except Exception as e:
            detail = f"move_failed:{e}"
            # still mark denied so curator won't re-notify forever
    entry["status"] = "denied"
    entry["decision_ts"] = now_ts()
    entry["decision_by"] = by
    entry["deny_reason"] = reason or None
    data["entries"][sha] = entry
    save_ledger(data)
    audit({"action": "deny", "ok": True, "id": entry["id"], "by": by, "reason": reason})
    return {"ok": True, "entry": entry, "detail": detail}


def show(pid: str) -> dict:
    data = migrate_ledger()
    sha, entry = find_by_id(data, pid)
    if not entry:
        return {"ok": False, "error": "not_found", "id": pid}
    rel = entry.get("vault_sync_path")
    md = read_note_text(rel)
    lines = md.splitlines()
    preview = "\n".join(lines[:40])
    return {"ok": True, "entry": entry, "preview": preview, "lines": len(lines), "card": card_text(entry)}


def queue(limit: int = 20) -> dict:
    data = migrate_ledger()
    pending = list_by_status(data, "pending")[:limit]
    return {
        "ok": True,
        "count": len(pending),
        "items": [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "bucket": e.get("bucket"),
                "path": e.get("vault_sync_path"),
                "ts": e.get("ts"),
            }
            for e in pending
        ],
    }


def mark_notified(sha: str, event_id: str | None = None, *, thread_event_id: str | None = None) -> None:
    data = load_ledger()
    entry = data.get("entries", {}).get(sha)
    if not entry:
        return
    entry = ensure_entry_shape(sha, entry)
    entry["matrix_notified_ts"] = now_ts()
    if event_id:
        entry["matrix_event_id"] = event_id
    if thread_event_id:
        entry["matrix_thread_event_id"] = thread_event_id
    data["entries"][sha] = entry
    save_ledger(data)


def lookup_matrix_event_id(sha: str | None) -> tuple[str | None, str | None]:
    """Return (parent_event_id, thread_event_id) from the ledger for a sha, if any."""
    if not sha:
        return None, None
    try:
        entry = load_ledger().get("entries", {}).get(sha, {})
    except Exception:
        return None, None
    return entry.get("matrix_event_id"), entry.get("matrix_thread_event_id")
