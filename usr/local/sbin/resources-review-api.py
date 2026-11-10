#!/usr/bin/env python3
"""Deterministic resources-review API (approve/deny/queue/show/notify)."""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, "/usr/local/sbin")
import resources_review_lib as rr  # noqa: E402
from matrix_client_simple import MatrixClient, resolve_resources_room_id  # noqa: E402

HOST = os.environ.get("RESOURCES_REVIEW_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("RESOURCES_REVIEW_API_PORT", "8013"))
TOKEN_FILE = Path(os.environ.get("RESOURCES_REVIEW_TOKEN_FILE", "/root/.creds/resources-review.token"))
ALLOW_IPS = {
    x.strip()
    for x in os.environ.get(
        "RESOURCES_REVIEW_ALLOW_IPS",
        "192.168.4.63,127.0.0.1,192.168.5.11,192.168.4.62",
    ).split(",")
    if x.strip()
}
AUDIT = Path(os.environ.get("RESOURCES_REVIEW_API_AUDIT", "/var/log/resources-review-api.jsonl"))


def _token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()


def _log(event: dict) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()), **event}, ensure_ascii=False) + "\n")


def notify_entry(entry: dict) -> dict:
    # card + screenshots in Matrix thread
    return rr.notify_matrix_with_images(entry)


def notify_pending(limit: int = 20, force: bool = False) -> dict:
    data = rr.migrate_ledger()
    pending = rr.list_by_status(data, "pending")
    sent = []
    skipped = []
    for e in pending:
        if len(sent) >= limit:
            break
        if not force and e.get("matrix_notified_ts"):
            skipped.append(e.get("id"))
            continue
        try:
            r = notify_entry(e)
            if r.get("ok"):
                sent.append({"id": e.get("id"), "event_id": r.get("event_id")})
            else:
                skipped.append({"id": e.get("id"), "error": r.get("error")})
        except Exception as ex:
            skipped.append({"id": e.get("id"), "error": str(ex)[:200]})
    return {"ok": True, "sent": sent, "skipped": skipped}


class Handler(BaseHTTPRequestHandler):
    server_version = "resources-review-api/1.0"

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _auth(self) -> bool:
        if self._client_ip() not in ALLOW_IPS and self._client_ip() != "::1":
            return False
        auth = self.headers.get("Authorization", "")
        tok = self.headers.get("X-Resources-Review-Token", "")
        expect = _token()
        if auth.startswith("Bearer ") and auth.split(" ", 1)[1].strip() == expect:
            return True
        if tok and tok.strip() == expect:
            return True
        return False

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 200_000:
            raise ValueError("body_too_large")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/health":
            self._json(200, {"ok": True, "service": "resources-review-api"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        qs = parse_qs(u.query)
        try:
            if u.path == "/queue":
                limit = int((qs.get("limit") or ["20"])[0])
                out = rr.queue(limit=limit)
                self._json(200, out)
                return
            if u.path == "/show":
                pid = (qs.get("id") or [""])[0]
                out = rr.show(pid)
                self._json(200 if out.get("ok") else 404, out)
                return
            self._json(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            _log({"path": u.path, "error": str(e)[:300]})
            self._json(500, {"ok": False, "error": str(e)[:300]})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            body = self._read_json()
        except Exception as e:
            self._json(400, {"ok": False, "error": f"bad_json:{e}"})
            return
        try:
            if u.path == "/approve":
                out = rr.approve(str(body.get("id") or ""), by=str(body.get("by") or "api"))
                _log({"action": "approve", **{k: out.get(k) for k in ("ok", "error")}})
                self._json(200 if out.get("ok") else 400, out)
                return
            if u.path == "/deny":
                out = rr.deny(
                    str(body.get("id") or ""),
                    reason=str(body.get("reason") or ""),
                    by=str(body.get("by") or "api"),
                )
                _log({"action": "deny", **{k: out.get(k) for k in ("ok", "error")}})
                self._json(200 if out.get("ok") else 400, out)
                return
            if u.path == "/notify":
                if body.get("id"):
                    data = rr.migrate_ledger()
                    sha, entry = rr.find_by_id(data, str(body["id"]))
                    if not entry:
                        self._json(404, {"ok": False, "error": "not_found"})
                        return
                    out = notify_entry(entry)
                else:
                    out = notify_pending(
                        limit=int(body.get("limit") or 20),
                        force=bool(body.get("force")),
                    )
                self._json(200 if out.get("ok") else 400, out)
                return
            self._json(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            _log({"path": u.path, "error": str(e)[:300]})
            self._json(500, {"ok": False, "error": str(e)[:300]})


def main() -> int:
    # ensure token exists
    if not TOKEN_FILE.is_file():
        import secrets

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
    rr.migrate_ledger()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"resources-review-api on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
