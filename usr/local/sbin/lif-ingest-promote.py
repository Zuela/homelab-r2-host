#!/usr/bin/env python3
"""LIF v1: quarantine drop -> secret scan -> promote or reject + audit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
import sys
sys.path.insert(0, "/usr/local/sbin")
import lif_state

INBOX_STAGING = Path(os.environ.get("LIF_INBOX_STAGING", "/tank/inbox-staging"))
INGEST_ROOT = Path(os.environ.get("LIF_ROOT", "/tank/ingest"))
INBOX = INGEST_ROOT / "inbox"
PROCESSING = INGEST_ROOT / "processing"
PROCESSED = INGEST_ROOT / "processed"
REJECTED = INGEST_ROOT / "rejected"
PROMOTE_ROOT = Path(
    os.environ.get("LIF_PROMOTE_ROOT", "/tank/studio-homelab/vault-drop/lif")
)
AUDIT = Path(os.environ.get("LIF_AUDIT_LOG", "/var/log/lif-ingest.jsonl"))

TEXT_EXT = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".toml",
}
IMAGE_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
}
ALLOWED_EXT = TEXT_EXT | IMAGE_EXT
MAX_BYTES_TEXT = int(os.environ.get("LIF_MAX_BYTES", str(5 * 1024 * 1024)))
MAX_BYTES_IMAGE = int(os.environ.get("LIF_MAX_BYTES_IMAGE", str(20 * 1024 * 1024)))
SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitkeep", "desktop.ini"}
SKIP_PREFIXES = ("._",)

# High-signal secret patterns (v1; gitleaks optional later)
SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_assign", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S{20,}")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_live", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("generic_bearer", re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9\-._~+/]+=*")),
    ("age_secret_key", re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]{50,}\b")),
    ("sops_mac_block", re.compile(r"(?i)sops_mac\s*:")),
]


def log(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()), **event}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_candidate(path: Path) -> bool:
    name = path.name
    if not path.is_file():
        return False
    if name in SKIP_NAMES or name.startswith(SKIP_PREFIXES):
        return False
    if path.suffix.lower() not in ALLOWED_EXT:
        return False
    try:
        size = path.stat().st_size
        if size <= 0:
            return False
        limit = MAX_BYTES_IMAGE if path.suffix.lower() in IMAGE_EXT else MAX_BYTES_TEXT
        if size > limit:
            return False
    except OSError:
        return False
    return True


def collect_inputs() -> list[Path]:
    found: list[Path] = []
    # Primary: dedicated ingest inbox
    if INBOX.is_dir():
        found.extend(p for p in INBOX.iterdir() if is_candidate(p))
    # Secondary: top-level inbox-staging drops (not recursive)
    if INBOX_STAGING.is_dir():
        for p in INBOX_STAGING.iterdir():
            if p.name in {"lif", "lost+found"}:
                continue
            if is_candidate(p):
                found.append(p)
    return found


def scan_file(path: Path) -> list[str]:
    hits: list[str] = []
    try:
        data = path.read_bytes()
    except OSError as e:
        return [f"read_error:{e}"]
    suf = path.suffix.lower()

    if suf in IMAGE_EXT:
        if suf == ".png" and not data.startswith(bytes([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A])):
            return ["invalid_png_magic"]
        if suf in {".jpg", ".jpeg"} and not data.startswith(bytes([0xFF,0xD8,0xFF])):
            return ["invalid_jpeg_magic"]
        if suf == ".gif" and not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return ["invalid_gif_magic"]
        if suf == ".webp":
            if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
                return ["invalid_webp_magic"]
        if suf in {".heic", ".heif"} and b"ftyp" not in data[:64]:
            return ["invalid_heic_magic"]
        return []

    if b'\x00' in data[:4096]:
        return ["binary_null_byte"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:
            return ["decode_error"]
    for rule_name, cre in SECRET_RULES:
        if cre.search(text):
            hits.append(rule_name)
    return hits


def safe_dest_name(src: Path, digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", src.stem)[:80] or "file"
    return f"{time.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}-{stem}{src.suffix.lower()}"


def ensure_dirs() -> None:
    for d in (INBOX, PROCESSING, PROCESSED, REJECTED, PROMOTE_ROOT):
        d.mkdir(parents=True, exist_ok=True)


def process_one(src: Path, force: bool = False) -> None:
    ensure_dirs()
    digest = sha256_file(src)
    if not force and lif_state.is_ingested(digest):
        # Already promoted earlier (even if vault-drop file was moved/deleted).
        log({
            "phase": "skip_duplicate",
            "src": str(src),
            "sha256": digest,
            "ok": True,
            "reason": "sha256_already_ingested",
        })
        try:
            src.unlink()
        except OSError:
            pass
        return
    work_name = safe_dest_name(src, digest)
    work = PROCESSING / work_name
    shutil.move(str(src), str(work))
    log(
        {
            "phase": "accept",
            "src": str(src),
            "work": str(work),
            "sha256": digest,
            "bytes": work.stat().st_size,
        }
    )

    hits = scan_file(work)
    if hits:
        dest = REJECTED / work_name
        shutil.move(str(work), str(dest))
        meta = dest.with_suffix(dest.suffix + ".reject.json")
        meta.write_text(
            json.dumps(
                {
                    "src_name": src.name,
                    "sha256": digest,
                    "hits": hits,
                    "status": "rejected_secret_or_policy",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        log(
            {
                "phase": "reject",
                "work": str(dest),
                "sha256": digest,
                "hits": hits,
                "ok": False,
            }
        )
        return

    day = time.strftime("%Y-%m-%d")
    kind = "images" if work.suffix.lower() in IMAGE_EXT else "notes"
    promote_dir = PROMOTE_ROOT / "incoming" / day / kind
    promote_dir.mkdir(parents=True, exist_ok=True)
    promoted = promote_dir / work_name
    # copy then move work to processed (keep quarantine history)
    shutil.copy2(str(work), str(promoted))
    processed = PROCESSED / day
    processed.mkdir(parents=True, exist_ok=True)
    final_processed = processed / work_name
    shutil.move(str(work), str(final_processed))
    # sidecar receipt
    receipt = promoted.with_suffix(promoted.suffix + ".lif.json")
    receipt.write_text(
        json.dumps(
            {
                "src_name": src.name,
                "sha256": digest,
                "promoted": str(promoted),
                "processed": str(final_processed),
                "status": "promoted",
                "scanner": "lif-v1-regex",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log(
        {
            "phase": "promote",
            "src_name": src.name,
            "sha256": digest,
            "promoted": str(promoted),
            "processed": str(final_processed),
            "ok": True,
        }
    )
    lif_state.mark_ingested(
        digest,
        source=str(src),
        promoted_to=str(promoted),
        status="promoted",
    )


def main() -> int:
    ensure_dirs()
    # symlink convenience for operators
    link = INBOX_STAGING / "lif"
    try:
        if not link.exists() and INBOX_STAGING.is_dir():
            link.symlink_to(INBOX)
    except OSError:
        pass

    force = lif_state.force_enabled()
    items = collect_inputs()
    if not items:
        log({"phase": "idle", "count": 0, "force": force})
        return 0
    for src in items:
        try:
            process_one(src, force=force)
        except Exception as e:
            log(
                {
                    "phase": "error",
                    "src": str(src),
                    "error": str(e)[:300],
                    "ok": False,
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
