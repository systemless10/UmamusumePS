
from __future__ import annotations

import base64
import json
import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import accounts, region
from . import keys, transport

log = logging.getLogger("uma-server")

PORT = int(os.environ.get("UMA_JP_ARM_PORT", "8090"))
HOST = os.environ.get("UMA_JP_ARM_HOST", "127.0.0.1")


def _tail20_path():
    with region.use_region("jp"):
        return region.data_dir() / "build_tail20.json"


_lock = threading.Lock()
_tail20: list[bytes] = []
_reported: dict[str, bytes] = {}
_last_udid: str | None = None


def _load_tail20() -> None:
    try:
        vals = json.loads(_tail20_path().read_text())
        _tail20[:] = [bytes.fromhex(v) for v in vals]
    except (OSError, ValueError):
        pass


def _learn(tail20: bytes | None, udid: bytes | None, tail50: bytes | None) -> None:
    global _last_udid
    with _lock:
        if tail20 and len(tail20) == 20 and (not _tail20 or _tail20[0] != tail20):
            if tail20 in _tail20:
                _tail20.remove(tail20)
            _tail20.insert(0, tail20)
            _tail20_path().write_text(json.dumps([t.hex() for t in _tail20[:8]]))
            if tail20 not in transport.KNOWN_TAIL20:
                log.warning("JP: client reported a NEW build tail20 %s -- a client update; "
                            "add it to transport.KNOWN_TAIL20", tail20.hex())
        if udid and len(udid) == 16 and tail50 and len(tail50) == 50:
            if _reported.get(udid.hex()) != tail50:
                log.info("JP: plugin reported an existing session udid=%s..", udid.hex()[:12])
            _reported[udid.hex()] = tail50
            _last_udid = udid.hex()


class Sessions:

    def session_for(self, viewer_id: str) -> transport.Session | None:
        acct = accounts.get_account(viewer_id) if viewer_id not in ("", "0") else None
        if acct and acct.get("udid") and acct.get("auth_key"):
            return transport.Session(udid=bytes.fromhex(acct["udid"]),
                                     tail50=base64.b64decode(acct["auth_key"]))
        with _lock:
            if _last_udid and _last_udid in _reported:
                return transport.Session(udid=bytes.fromhex(_last_udid),
                                         tail50=_reported[_last_udid])
        return None

    def reported_sessions(self) -> list[transport.Session]:
        with _lock:
            items = list(_reported.items())[::-1]
        return [transport.Session(udid=bytes.fromhex(u), tail50=t) for u, t in items]

    def tail20_candidates(self):
        with _lock:
            return list(_tail20)


SESSIONS = Sessions()


def _hex(qs: dict, key: str) -> bytes | None:
    try:
        v = (qs.get(key) or [""])[0]
        return bytes.fromhex(v) if v else None
    except ValueError:
        return None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/log":
            try:
                log.info("PHONE: %s", bytes.fromhex(parse_qs(url.query).get("m", [""])[0]).decode("utf-8", "replace"))
            except ValueError:
                pass
            self.send_response(204)
            self.end_headers()
            return
        if url.path != "/arm":
            self.send_error(404)
            return
        qs = parse_qs(url.query)
        _learn(_hex(qs, "tail20"), _hex(qs, "udid"), _hex(qs, "tail50"))
        body = keys.public_key().hex().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start() -> None:
    _load_tail20()
    httpd = ThreadingHTTPServer((HOST, PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, name="jp-arming", daemon=True).start()
    log.info("JP arming side channel on %s:%d (pubkey %s..)", HOST, PORT, keys.public_key().hex()[:16])
