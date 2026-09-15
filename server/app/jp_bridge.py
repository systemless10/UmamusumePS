"""
JP request/response bridge.

The Hachimi plugin (tools/hachimi_jp_capture) runs inside the real game
process and sees the plaintext envelope -- including the `seed` the client
derives its per-request ephemeral X25519 key from -- *before* the request
gets sealed. It POSTs that here the instant it's captured: a side channel a
passive network observer could never have (see docs/here/HANDOFF_JP_CRYPTO.md
for why passive decryption is architecturally impossible without it).

We never decrypt the real sealed request that arrives over the network at
all -- by the time it does, we already have its plaintext from the side
channel. All the real request contributes at that point is the HTTP path
(which endpoint) and its SID header, which we use purely as the correlation
key to find the matching side-channel submission (see main.py's
_dispatch_jp). SID rides in the clear as a normal header on the real request,
exactly like on Global's.

This listens on its own plain-HTTP port rather than joining the main HTTPS:443
app: the game's real traffic needs a cert the client trusts, but the Rust
plugin posting to this from inside the game process has no such requirement
and no easy way to speak TLS -- plain loopback HTTP is fine for a same-machine
side channel. It runs in the same Python process as the main app (a
background thread, started from main.py at import time) so both sides share
this module's single in-memory `_pending` dict.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import msgpack

from . import jp_crypto

log = logging.getLogger("uma-server")

SIDE_CHANNEL_PORT = 8090

_PENDING_TTL_SECONDS = 30
_lock = threading.Lock()
_pending: dict[str, dict] = {}
_server_started = False
_server_lock = threading.Lock()


def _prune_locked() -> None:
    cutoff = time.time() - _PENDING_TTL_SECONDS
    for sid_hex in [k for k, v in _pending.items() if v["ts"] < cutoff]:
        del _pending[sid_hex]


def _submit(body: dict) -> None:
    sid_hex = body["sid_hex"].lower()
    envelope = bytes.fromhex(body["envelope_hex"])
    msgpack_bytes = bytes.fromhex(body["msgpack_hex"])

    server_pubkey = envelope[4:36]
    _, sbox = jp_crypto.ec_export_and_secret(envelope, ec_u=server_pubkey)
    sid_raw = envelope[56:72]
    tail16 = envelope[154:170]
    payload = msgpack.unpackb(msgpack_bytes, raw=False, strict_map_key=False)

    with _lock:
        _prune_locked()
        _pending[sid_hex] = {
            "sbox": sbox,
            "sid_raw": sid_raw,
            "tail16": tail16,
            "payload": payload,
            "ts": time.time(),
        }


def pop_pending(sid_hex: str) -> dict | None:
    """One-shot: a reply is only ever sealed once per submitted request."""
    with _lock:
        return _pending.pop(sid_hex.lower(), None)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/jp_bridge/submit":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            _submit(json.loads(raw))
            status, resp_body = 200, b'{"ok":true}'
        except Exception as exc:
            log.warning("jp_bridge submit failed: %r", exc)
            status, resp_body = 400, b'{"ok":false}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


def start() -> None:
    """Idempotent -- safe to call on every import (uvicorn's --reload
    re-imports main.py in the same process on most code changes)."""
    global _server_started
    with _server_lock:
        if _server_started:
            return
        httpd = ThreadingHTTPServer(("127.0.0.1", SIDE_CHANNEL_PORT), _Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        _server_started = True
        log.info("JP bridge side-channel listening on 127.0.0.1:%d", SIDE_CHANNEL_PORT)
