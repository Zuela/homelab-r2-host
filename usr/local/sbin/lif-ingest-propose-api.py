#!/usr/bin/env python3
"""LIF intake API: Hermes/host clients drop markdown into /tank/ingest/inbox for LIF scan/promote."""
from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("LIF_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("LIF_API_PORT", "8012"))
TOKEN = Path(
    os.environ.get("LIF_API_TOKEN_FILE", "/root/.creds/lif-ingest-propose.token")
).read_text(encoding="utf-8").strip()
ALLOW = {
    x.strip()
    for x in os.environ.get(
        "LIF_API_ALLOW_IPS", "192.168.4.63,127.0.0.1,192.168.5.11"
    ).split(",")
    if x.strip()
}
INBOX = Path(os.environ.get("LIF_INBOX", "/tank/ingest/inbox"))
AUDIT = Path(os.environ.get("LIF_API_AUDIT", "/var/log/lif-ingest-propose-api.jsonl"))
MAX_BODY = 50_000
SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def audit(ok: bool, detail: dict) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()), "ok": ok, **detail}
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self) -> bool:
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip.split("::ffff:")[-1]
        if ip not in ALLOW:
            return False
        auth = self.headers.get("Authorization", "")
        return auth == ("Bearer " + TOKEN) or self.headers.get("X-LIF-Token") == TOKEN

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            return self._json(
                200,
                {
                    "ok": True,
                    "service": "lif-ingest-propose",
                    "inbox": str(INBOX),
                    "port": PORT,
                },
            )
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p != "/propose":
            return self._json(404, {"ok": False, "error": "not_found"})
        if not self._auth():
            audit(False, {"error": "unauthorized", "ip": self.client_address[0]})
            return self._json(401, {"ok": False, "error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._json(400, {"ok": False, "error": "bad_size"})
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "bad_json"})
        title = str(data.get("title") or "hermes-lif-note").strip()[:120]
        body = str(data.get("body") or "").strip()
        fname = str(data.get("filename") or "").strip()
        if not body:
            return self._json(400, {"ok": False, "error": "empty_body"})
        low = (title + "\n" + body).lower()
        for bad in (
            "begin private key",
            "api_key=",
            "password=",
            "bearer ",
            "aws_secret_access_key",
            "age-secret-key-1",
        ):
            if bad in low:
                audit(False, {"error": "secret_like_content", "title": title})
                return self._json(400, {"ok": False, "error": "secret_like_content_blocked"})
        slug = SLUG_RE.sub("-", title.lower()).strip("-")[:40] or "note"
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if fname:
            base = Path(fname).name
            if ".." in base or "/" in base or not base.endswith(".md"):
                return self._json(400, {"ok": False, "error": "invalid_filename"})
            out_name = base
        else:
            out_name = f"{ts}-hermes-{slug}.md"
        INBOX.mkdir(parents=True, exist_ok=True)
        dest = INBOX / out_name
        if dest.exists():
            out_name = f"{ts}-{slug}.md"
            dest = INBOX / out_name
        md = "# %s\n\n_Proposed by Hermes via lif_ingest_propose at %s_\n\n%s\n" % (
            title,
            time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
            body,
        )
        try:
            dest.write_text(md, encoding="utf-8")
            os.chmod(dest, 0o664)
            # best-effort ownership for operator readability
            try:
                os.chown(dest, 1000, 1000)
            except OSError:
                pass
            audit(True, {"title": title, "path": str(dest), "bytes": dest.stat().st_size})
            return self._json(
                200,
                {
                    "ok": True,
                    "path": str(dest),
                    "note": "queued in LIF inbox; promote cron scans within ~5 min",
                },
            )
        except Exception as e:
            audit(False, {"error": str(e)[:300], "title": title})
            return self._json(500, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    print("lif-ingest-propose on %s:%s inbox=%s" % (HOST, PORT, INBOX), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
