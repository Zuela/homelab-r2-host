#!/usr/bin/env python3
"""Bootstrap LIF ledger from already processed/promoted files so history won't re-ingest."""
from __future__ import annotations
import hashlib
import sys
from pathlib import Path
sys.path.insert(0, "/usr/local/sbin")
import lif_state

ROOTS = [
    Path("/tank/ingest/processed"),
    Path("/tank/studio-homelab/vault-drop/lif"),
]

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    data = lif_state.load_ledger()
    n = 0
    for root in ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.name.endswith((".json", ".lif.json", ".reject.json")):
                continue
            if p.name.startswith("."):
                continue
            digest = sha256_file(p)
            lif_state.mark_ingested(
                digest,
                source=str(p),
                promoted_to=str(p) if "vault-drop" in str(p) else "",
                status="bootstrap",
                data=data,
                persist=False,
            )
            n += 1
    lif_state.save_ledger(data)
    print(f"bootstrap_ok files_scanned={n} unique_sha={len(data['entries'])} ledger={lif_state.LEDGER}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
