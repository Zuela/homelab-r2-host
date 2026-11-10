#!/usr/bin/env python3
"""INT-009 Muse outbox adapter for LIF.
Pulls markdown from a configured private git outbox into LIF inbox.
If not configured, exits 0 and audits skipped (safe default).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import sys
sys.path.insert(0, "/usr/local/sbin")
import lif_state
import hashlib

CFG = Path(os.environ.get("MUSE_OUTBOX_ENV", "/etc/lif/muse-outbox.env"))
MUSE_ROOT = Path(os.environ.get("MUSE_ROOT", "/tank/muse"))
GIT_DIR = MUSE_ROOT / "inbox" / "git"
LIF_INBOX = Path(os.environ.get("LIF_INBOX", "/tank/ingest/inbox"))
AUDIT = Path(os.environ.get("MUSE_ADAPTER_AUDIT", "/var/log/lif-muse-adapter.jsonl"))
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


ALLOWED_EXT = {
    ".md", ".markdown", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif",
}


def log(ok: bool, detail: dict) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()), "ok": ok, **detail}
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True, check=False)


def ensure_dirs() -> None:
    for d in (MUSE_ROOT / "inbox", MUSE_ROOT / "processed", MUSE_ROOT / "rejected", GIT_DIR, LIF_INBOX):
        d.mkdir(parents=True, exist_ok=True)


def main() -> int:
    ensure_dirs()
    cfg = load_env(CFG)
    url = cfg.get("MUSE_OUTBOX_URL", "").strip()
    if not url:
        log(True, {"phase": "skipped", "reason": "not_configured", "hint": f"create {CFG} from example"})
        print("muse adapter: not configured (ok)")
        return 0

    branch = cfg.get("MUSE_OUTBOX_BRANCH", "main")
    mirror = cfg.get("MUSE_OUTBOX_DIR", str(GIT_DIR / "repo"))
    repo = Path(mirror)
    git_env = os.environ.copy()
    # optional key
    key = cfg.get("MUSE_OUTBOX_SSH_KEY", "").strip()
    if key:
        git_env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o StrictHostKeyChecking=accept-new"

    if not (repo / ".git").is_dir():
        repo.parent.mkdir(parents=True, exist_ok=True)
        cp = run(["git", "clone", "--depth", "1", "--branch", branch, url, str(repo)], env=git_env)
        if cp.returncode != 0:
            log(False, {"phase": "clone_failed", "stderr": (cp.stderr or "")[:500]})
            print("muse adapter: clone failed")
            return 1
    else:
        cp = run(["git", "fetch", "origin", branch], cwd=repo, env=git_env)
        if cp.returncode != 0:
            log(False, {"phase": "fetch_failed", "stderr": (cp.stderr or "")[:500]})
            return 1
        run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo, env=git_env)

    # copy new/changed md into LIF inbox (flat, unique names)
    copied = 0
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_EXT:
            continue
        if ".git" in path.parts:
            continue
        rel = path.relative_to(repo).as_posix().replace("/", "__")
        dest = LIF_INBOX / f"muse-{rel}"
        digest = sha256_file(path)
        force = lif_state.force_enabled()
        # Skip if this content was already ingested/promoted unless forced.
        if not force and lif_state.is_ingested(digest):
            continue
        # only copy if missing or content changed
        if dest.exists() and dest.read_bytes() == path.read_bytes():
            continue
        shutil.copy2(path, dest)
        try:
            os.chown(dest, 1000, 1000)
        except OSError:
            pass
        copied += 1

    log(True, {"phase": "pull_ok", "copied_to_lif_inbox": copied, "repo": str(repo), "branch": branch, "force": lif_state.force_enabled()})
    print(f"muse adapter: ok copied={copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
