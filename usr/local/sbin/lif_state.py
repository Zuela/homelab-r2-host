#!/usr/bin/env python3
"""Shared LIF ingest ledger: content-addressed (sha256) to prevent duplicate pulls/promotes."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("LIF_STATE_DIR", "/tank/ingest/state"))
LEDGER = STATE_DIR / "ingested-sha256.json"


def _empty() -> dict:
    return {"version": 1, "entries": {}}


def load_ledger() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not LEDGER.is_file():
        return _empty()
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            return _empty()
        if not isinstance(data["entries"], dict):
            data["entries"] = {}
        return data
    except Exception:
        return _empty()


def save_ledger(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(LEDGER)


def is_ingested(digest: str, data: dict | None = None) -> bool:
    data = data or load_ledger()
    return digest in data.get("entries", {})


def mark_ingested(
    digest: str,
    *,
    source: str = "",
    promoted_to: str = "",
    status: str = "promoted",
    data: dict | None = None,
    persist: bool = True,
) -> dict:
    data = data or load_ledger()
    entries = data.setdefault("entries", {})
    prev = entries.get(digest, {})
    sources = list(prev.get("sources") or [])
    if source and source not in sources:
        sources.append(source)
    entries[digest] = {
        "first_ts": prev.get("first_ts")
        or time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
        "last_ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
        "status": status,
        "sources": sources[-20:],
        "promoted_to": promoted_to or prev.get("promoted_to") or "",
    }
    if persist:
        save_ledger(data)
    return data


def force_enabled(argv: list[str] | None = None) -> bool:
    argv = argv if argv is not None else os.sys.argv[1:]
    if os.environ.get("LIF_FORCE", "").strip() in {"1", "true", "yes", "YES"}:
        return True
    return any(a in {"--force", "-f"} for a in argv)
