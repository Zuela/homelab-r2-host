#!/usr/bin/env python3
"""Minimal Matrix Client-Server API helper (stdlib only)."""
from __future__ import annotations

import itertools
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_TXN_SEQ = itertools.count()


def _load_kv(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_homeserver_config() -> dict:
    cfg = {
        "url": os.environ.get("MATRIX_URL", "http://192.168.4.55:8008").rstrip("/"),
        "server_name": os.environ.get("MATRIX_SERVER_NAME", "matrix.leonbasu.lan"),
    }
    for p in (
        Path(os.environ.get("MATRIX_N8N_BOT_CREDS", "/root/.creds/matrix-n8n-bot")),
        Path("/root/.creds/matrix-admin"),
    ):
        if p.is_file():
            kv = _load_kv(p)
            cfg["url"] = kv.get("URL", cfg["url"]).rstrip("/")
            cfg["server_name"] = kv.get("SERVER_NAME", cfg["server_name"])
            if "USERNAME" in kv:
                cfg.setdefault("username", kv["USERNAME"])
            if "PASSWORD" in kv:
                cfg.setdefault("password", kv["PASSWORD"])
            if "MXID" in kv:
                cfg.setdefault("mxid", kv["MXID"])
            break
    bot = Path(os.environ.get("MATRIX_RESOURCES_BOT_CREDS", "/root/.creds/matrix-resources-bot"))
    if bot.is_file():
        raw = bot.read_text(encoding="utf-8")
        if raw.strip().startswith("{"):
            d = json.loads(raw)
            cfg["username"] = d.get("username") or cfg.get("username")
            cfg["password"] = d.get("password") or cfg.get("password")
            if d.get("mxid"):
                cfg["mxid"] = d["mxid"]
            if d.get("url"):
                cfg["url"] = d["url"].rstrip("/")
        else:
            kv = _load_kv(bot)
            for k, v in kv.items():
                if k == "URL":
                    cfg["url"] = v.rstrip("/")
                elif k == "SERVER_NAME":
                    cfg["server_name"] = v
                elif k == "USERNAME":
                    cfg["username"] = v
                elif k == "PASSWORD":
                    cfg["password"] = v
                elif k == "MXID":
                    cfg["mxid"] = v
                elif k == "ACCESS_TOKEN":
                    cfg["access_token"] = v
    tok = Path(os.environ.get("MATRIX_RESOURCES_TOKEN_FILE", "/root/.creds/matrix-resources-bot.token"))
    if tok.is_file():
        t = tok.read_text(encoding="utf-8").strip()
        if t and not t.startswith("#"):
            if "ACCESS_TOKEN=" in t:
                for line in t.splitlines():
                    if line.startswith("ACCESS_TOKEN="):
                        cfg["access_token"] = line.split("=", 1)[1].strip()
            else:
                cfg["access_token"] = t.splitlines()[0].strip()
    return cfg


def _req(method: str, url: str, token: str | None = None, body: dict | None = None, timeout: int = 30, data: bytes | None = None, content_type: str | None = None) -> dict:
    if data is not None:
        payload = data
        headers = {"Content-Type": content_type or "application/octet-stream"}
    else:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Exception | None = None
    for attempt in range(5):
        r = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")
            last_err = RuntimeError(f"matrix_http_{e.code}:{err[:300]}")
            if e.code != 429:
                raise last_err from e
            # honor retry_after_ms when present
            import re as _re
            m = _re.search(r"retry_after_ms\"?:\s*(\d+)", err)
            wait_ms = int(m.group(1)) if m else (400 * (attempt + 1))
            time.sleep(min(12.0, wait_ms / 1000.0 + 0.15))
            continue
    raise last_err if last_err else RuntimeError("matrix_http_unknown")


class MatrixClient:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_homeserver_config()
        self.url = self.cfg["url"].rstrip("/")
        self.token = self.cfg.get("access_token")
        self.user_id = self.cfg.get("mxid")

    def login(self, force: bool = False) -> str:
        if self.token and not force:
            return self.token
        user = self.cfg.get("username")
        password = self.cfg.get("password")
        if not user or not password:
            raise RuntimeError("matrix_missing_credentials")
        body = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "initial_device_display_name": os.environ.get("MATRIX_DEVICE_NAME", "resources-review-r2"),
        }
        data = _req("POST", f"{self.url}/_matrix/client/v3/login", body=body)
        self.token = data["access_token"]
        self.user_id = data.get("user_id") or self.user_id
        tok_path = Path(os.environ.get("MATRIX_RESOURCES_TOKEN_FILE", "/root/.creds/matrix-resources-bot.token"))
        try:
            tok_path.parent.mkdir(parents=True, exist_ok=True)
            tok_path.write_text(
                f"ACCESS_TOKEN={self.token}\nUSER_ID={self.user_id}\nDEVICE_ID={data.get('device_id','')}\n",
                encoding="utf-8",
            )
            os.chmod(tok_path, 0o600)
        except Exception:
            pass
        return self.token

    def ensure_token(self) -> str:
        if not self.token:
            return self.login()
        return self.token

    def whoami(self) -> dict:
        self.ensure_token()
        return _req("GET", f"{self.url}/_matrix/client/v3/account/whoami", token=self.token)

    def create_room(self, alias_localpart: str, name: str, topic: str, invite: list[str]) -> dict:
        self.ensure_token()
        body = {
            "name": name,
            "topic": topic,
            "preset": "private_chat",
            "visibility": "private",
            "room_alias_name": alias_localpart,
            "invite": invite,
            "is_direct": False,
        }
        return _req("POST", f"{self.url}/_matrix/client/v3/createRoom", token=self.token, body=body)

    def join(self, room_id_or_alias: str) -> dict:
        self.ensure_token()
        enc = urllib.parse.quote(room_id_or_alias, safe="")
        return _req("POST", f"{self.url}/_matrix/client/v3/join/{enc}", token=self.token, body={})

    def invite(self, room_id: str, user_id: str) -> dict:
        self.ensure_token()
        enc = urllib.parse.quote(room_id, safe="")
        return _req("POST", f"{self.url}/_matrix/client/v3/rooms/{enc}/invite", token=self.token, body={"user_id": user_id})

    def _send_event(self, room_id: str, content: dict, event_type: str = "m.room.message") -> dict:
        self.ensure_token()
        # Counter: two sends in the same millisecond reused one txn id, and the
        # server dedupes by txn id, so the second event was silently dropped.
        txn = f"txn{int(time.time()*1000)}{os.getpid()}-{next(_TXN_SEQ)}"
        enc = urllib.parse.quote(room_id, safe="")
        return _req(
            "PUT",
            f"{self.url}/_matrix/client/v3/rooms/{enc}/send/{event_type}/{txn}",
            token=self.token,
            body=content,
        )

    def send_text(self, room_id: str, text: str, thread_root: str | None = None, html: str | None = None) -> dict:
        content = {"msgtype": "m.text", "body": text}
        if html is not None:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html
        if thread_root:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root}
        return self._send_event(room_id, content)

    def edit_text(self, room_id: str, event_id: str, text: str, *, html: str | None = None, thread_root: str | None = None) -> dict:
        """Send an m.replace edit that updates event_id in place.
        Sets m.new_content to the new body, m.relates_to.rel_type=m.replace.
        Optionally preserve the thread anchor so edits stay in their thread."""
        new_content = {"msgtype": "m.text", "body": text}
        if html is not None:
            new_content["format"] = "org.matrix.custom.html"
            new_content["formatted_body"] = html
        content = {
            "body": "* edit *",
            "msgtype": "m.text",
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
        }
        if thread_root:
            content["m.relates_to"]["m.thread"] = {"event_id": thread_root}
        return self._send_event(room_id, content)

    def send_reaction(self, room_id: str, event_id: str, key: str) -> dict:
        """Drop a reaction annotation onto an existing event."""
        return self._send_event(room_id, {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": key,
            }
        }, event_type="m.reaction")

    def upload_media(self, path: Path, content_type: str | None = None) -> str:
        """Upload file; returns mxc:// URI."""
        self.ensure_token()
        data = path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        filename = urllib.parse.quote(path.name)
        url = f"{self.url}/_matrix/media/v3/upload?filename={filename}"
        # older servers: /_matrix/media/r0/upload
        try:
            resp = _req("POST", url, token=self.token, data=data, content_type=ctype, timeout=120)
        except RuntimeError as e:
            if "matrix_http_404" in str(e) or "matrix_http_405" in str(e):
                url = f"{self.url}/_matrix/media/r0/upload?filename={filename}"
                resp = _req("POST", url, token=self.token, data=data, content_type=ctype, timeout=120)
            else:
                raise
        mxc = resp.get("content_uri")
        if not mxc:
            raise RuntimeError(f"upload_no_uri:{resp}")
        return mxc

    def send_image(self, room_id: str, path: Path, caption: str | None = None, thread_root: str | None = None) -> dict:
        mxc = self.upload_media(path)
        ctype = mimetypes.guess_type(str(path))[0] or "image/png"
        size = path.stat().st_size
        body = caption or path.name
        content = {
            "msgtype": "m.image",
            "body": body,
            "url": mxc,
            "info": {"mimetype": ctype, "size": size},
        }
        if thread_root:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root}
        return self._send_event(room_id, content)

    def get_event(self, room_id: str, event_id: str) -> dict:
        self.ensure_token()
        renc = urllib.parse.quote(room_id, safe="")
        eenc = urllib.parse.quote(event_id, safe="")
        return _req("GET", f"{self.url}/_matrix/client/v3/rooms/{renc}/event/{eenc}", token=self.token)

    def sync(self, since: str | None = None, timeout_ms: int = 30000) -> dict:
        self.ensure_token()
        q = f"timeout={int(timeout_ms)}"
        if since:
            q += f"&since={urllib.parse.quote(since)}"
        return _req("GET", f"{self.url}/_matrix/client/v3/sync?{q}", token=self.token, timeout=max(60, timeout_ms // 1000 + 15))


def resolve_resources_room_id() -> str | None:
    env_id = os.environ.get("MATRIX_RESOURCES_ROOM_ID", "").strip()
    if env_id:
        return env_id
    p = Path(os.environ.get("MATRIX_NAMESPACE_ROOMS", "/root/.creds/matrix-namespace-rooms.json"))
    if p.is_file():
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("resources_review_room_id") or d.get("homelab_resources_review_room_id")
    return None
