#!/usr/bin/env python3
"""lif-watcher daemon for r2.

Polls GitHub for new repos under user Zuela and open issues on
Zuela/sage-ops. Single state file at /tank/ingest/state/lif-watcher-seen.jsonl.
Single audit log at /var/log/lif-watcher.jsonl.

Handlers:
  source_tick  : /users/Zuela/repos, sorted created desc
  issue_tick   : /repos/Zuela/sage-ops/issues, label muse-task, state open

Both append a single log event per change; both write state atomically.
flock guards against overlap. tick interval defaults to 15m.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("LIF_STATE_PATH", "/tank/ingest/state/lif-watcher-seen.jsonl"))
LOG_PATH = Path(os.environ.get("LIF_LOG_PATH", "/var/log/lif-watcher.jsonl"))
LOCK_PATH = Path("/var/lock/lif-watcher.lock")
LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

USER = os.environ.get("LIF_USER", "Zuela")
OP_REPO = os.environ.get("LIF_OP_REPO", "Zuela/sage-ops")
INTENT_LABEL = os.environ.get("LIF_INTENT_LABEL", "muse-task")
TICK_DEFAULT = os.environ.get("LIF_TICK", "15m")
DRY_RUN = os.environ.get("LIF_DRY_RUN", "0") == "1"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(event: str, **kw) -> None:
    if DRY_RUN and event not in ("startup", "tick", "shutdown"):
        pass  # in dry run we still log but avoid mutating state, see writers
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), "event": event, **kw}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "repos": {}, "issues": {}}
    last = None
    for line in STATE_PATH.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last or {"version": 1, "repos": {}, "issues": {}}


def write_state(state: dict) -> None:
    if DRY_RUN:
        log_event("state_write_skipped_dry_run", repos=len(state["repos"]), issues=len(state["issues"]))
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False) + "\n")
    tmp.replace(STATE_PATH)


def load_gh_headers() -> dict:
    token_path = Path(os.environ.get("LIF_GH_TOKEN_PATH", "/root/.creds/r2-lif-sources-watch.token"))
    if not token_path.exists():
        log_event("token_missing", token_path=str(token_path))
        return {}
    token = token_path.read_text().strip()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "lif-watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(url: str) -> object | None:
    headers = load_gh_headers()
    if not headers:
        return None
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (401, 403):
            log_event("gh_auth_or_rate", url=url, status=e.code)
            return None
        log_event("gh_http_error", url=url, status=e.code)
        return None
    except Exception as e:
        log_event("gh_exception", url=url, error=str(e))
        return None


def hash_labels(labels: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(labels)).encode()).hexdigest()[:16]


def source_tick(state: dict) -> None:
    url = f"https://api.github.com/users/{USER}/repos?per_page=100&sort=created&direction=desc"
    repos = gh_get(url)
    if repos is None:
        log_event("source_tick_no_response", url=url)
        return
    if not isinstance(repos, list):
        log_event("source_tick_unexpected", payload_type=type(repos).__name__)
        return
    new_count = 0
    for r in repos:
        rid = str(r.get("id", ""))
        if not rid:
            continue
        name = r.get("full_name", "")
        if not name:
            continue
        if rid in state["repos"]:
            continue
        state["repos"][rid] = {
            "name": name,
            "status": "pending",
            "plan": None,
            "default_branch": r.get("default_branch", "main"),
            "description": r.get("description"),
            "topics": r.get("topics", []) or [],
            "created_at": r.get("created_at"),
            "first_seen": now_iso(),
            "last_seen": now_iso(),
        }
        new_count += 1
        log_event("repo_new", name=name, description=r.get("description"),
                  default_branch=r.get("default_branch"), topics=r.get("topics") or [],
                  created_at=r.get("created_at"))
    write_state(state)
    log_event("source_tick_done", seen=len(state["repos"]), new=new_count)


def issue_tick(state: dict) -> None:
    url = f"https://api.github.com/repos/{OP_REPO}/issues?state=open&labels={urllib.parse.quote(INTENT_LABEL)}&per_page=100"
    issues = gh_get(url)
    if issues is None:
        log_event("issue_tick_no_response", url=url)
        return
    if not isinstance(issues, list):
        log_event("issue_tick_unexpected", payload_type=type(issues).__name__)
        return
    new_count = 0
    for issue in issues:
        n = str(issue.get("number", ""))
        if not n:
            continue
        labels = sorted([l.get("name", "") for l in issue.get("labels", []) or []])
        label_hash = hash_labels(labels)
        cur = state["issues"].get(n)
        updated_at = issue.get("updated_at")
        if cur and cur.get("label_set_hash") == label_hash and cur.get("updated_at") == updated_at:
            cur["last_seen"] = now_iso()
            continue
        state["issues"][n] = {
            "number": n,
            "title": issue.get("title", ""),
            "state": issue.get("state", "open"),
            "label_set": labels,
            "label_set_hash": label_hash,
            "updated_at": updated_at,
            "last_seen": now_iso(),
        }
        new_count += 1
        log_event("issue_new", repo=OP_REPO, number=n, title=issue.get("title"),
                  labels=labels, state=issue.get("state"))
    write_state(state)
    log_event("issue_tick_done", seen=len(state["issues"]), new=new_count)


def matrix_announce(state: dict) -> None:
    if DRY_RUN:
        return
    api = os.environ.get("LIF_MATRIX_API")
    room = os.environ.get("LIF_MATRIX_ROOM")
    token_path = os.environ.get("LIF_MATRIX_TOKEN_PATH")
    if not (api and room and token_path):
        log_event("matrix_not_configured")
        return
    tok_path = Path(token_path)
    if not tok_path.exists():
        log_event("matrix_token_missing", token_path=str(tok_path))
        return
    pending = [r for r in state["repos"].values() if r.get("status") == "pending"]
    if not pending:
        return
    body_lines = [f"{r['name']} (description: {r['description']}, default_branch: {r['default_branch']}, topics: {r['topics']})" for r in pending]
    msg = ("new repos pending\n"
           + "\n".join(body_lines)
           + "\n\nUse !add <repo> bucket:<name> to activate. !ignore <repo> to skip.")
    token = tok_path.read_text().strip()
    payload = json.dumps({"msgtype": "m.text", "body": msg}).encode("utf-8")
    url = f"{api.rstrip('/')}/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message"
    req = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}",
                                                            "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log_event("matrix_post", room=room, status=resp.status, repos=[r["name"] for r in pending])
    except Exception as e:
        log_event("matrix_post_error", error=str(e))


def parse_interval(s: str) -> int:
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    raise ValueError(f"unrecognised tick interval: {s}")


def run_tick(lock_fd) -> None:
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        state = read_state()
        source_tick(state)
        issue_tick(state)
        matrix_announce(state)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", default=TICK_DEFAULT, help="internal tick interval, e.g. 15m")
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--dry-run", action="store_true", help="read only, do not mutate state")
    args = ap.parse_args()

    if args.dry_run:
        os.environ["LIF_DRY_RUN"] = "1"

    interval = parse_interval(args.tick)

    log_event("startup", tick=interval, dry_run=args.dry_run, pid=os.getpid())
    with open(LOCK_PATH, "w") as lock_fd:
        if args.once:
            run_tick(lock_fd)
            log_event("shutdown", once=True)
            return 0
        while True:
            run_tick(lock_fd)
            time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
