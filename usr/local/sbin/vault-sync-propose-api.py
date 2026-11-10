#!/usr/bin/env python3
"""Minimal vault-sync propose API for Hermes (host-side)."""
from __future__ import annotations
import json, os, re, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("VSP_HOST", "0.0.0.0")
PORT = int(os.environ.get("VSP_PORT", "8011"))
TOKEN = Path(os.environ.get("VSP_TOKEN_FILE", "/root/.creds/vault-sync-propose.token")).read_text().strip()
ALLOW = set(x.strip() for x in os.environ.get("VSP_ALLOW_IPS", "192.168.4.63,127.0.0.1,192.168.5.11").split(",") if x.strip())
COMMIT_BIN = os.environ.get("VSP_COMMIT_BIN", "/usr/local/sbin/vault-sync-commit")
AUDIT = Path(os.environ.get("VSP_AUDIT", "/var/log/vault-sync-propose.jsonl"))
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
        return auth == ("Bearer " + TOKEN) or self.headers.get("X-Vault-Sync-Token") == TOKEN

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            if not self._auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            return self._json(200, {"ok": True, "service": "vault-sync-propose", "commit_bin": COMMIT_BIN})
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
        title = str(data.get("title") or "hermes-note").strip()[:120]
        body = str(data.get("body") or "").strip()
        rel = str(data.get("path") or "").strip()
        if not body:
            return self._json(400, {"ok": False, "error": "empty_body"})
        low = (title + "\n" + body).lower()
        for bad in ("begin private key", "api_key=", "password=", "bearer ", "ntfy token", "matrix_access"):
            if bad in low:
                audit(False, {"error": "secret_like_content"})
                return self._json(400, {"ok": False, "error": "secret_like_content_blocked"})
        day = time.strftime("%Y-%m-%d", time.gmtime())
        slug = SLUG_RE.sub("-", title.lower()).strip("-")[:40] or "note"
        if not rel:
            rel = "inbox/%s-hermes-%s.md" % (day, slug)
        if ".." in rel.split("/") or rel.startswith("/") or not rel.endswith(".md"):
            return self._json(400, {"ok": False, "error": "invalid_path"})
        if not rel.startswith("inbox/"):
            return self._json(400, {"ok": False, "error": "path_must_be_under_inbox"})
        ts = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
        md = "# %s\n\n_Proposed by Hermes via vault_sync_propose at %s_\n\n%s\n" % (title, ts, body)
        env = os.environ.copy()
        env["VAULT_SYNC_MESSAGE"] = ("hermes propose: " + title)[:200]
        try:
            proc = subprocess.run([COMMIT_BIN, rel], input=md.encode(), capture_output=True, env=env, timeout=60)
            out = (proc.stdout or b"").decode() + (proc.stderr or b"").decode()
            ok = proc.returncode == 0
            audit(ok, {"path": rel, "title": title, "rc": proc.returncode, "out": out[:300]})
            if not ok:
                return self._json(500, {"ok": False, "error": "commit_failed", "detail": out[:500]})
            return self._json(200, {"ok": True, "path": rel, "detail": out.strip()[:500]})
        except Exception as e:
            audit(False, {"error": str(e)[:300], "path": rel})
            return self._json(500, {"ok": False, "error": str(e)})

if __name__ == "__main__":
    print("vault-sync-propose on %s:%s" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
