
from __future__ import annotations

import json
import os
import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import icons
from . import settings as S
from .jobs import Jobs
from .workers import Pool, WorkerError

STATIC = (Path(__file__).resolve().parent / "static").resolve()
MAX_BODY = 128 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server: "PanelServer"

    def log_message(self, *_):
        pass

    def _allowed(self) -> bool:
        ok = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}", *self.server.extra_hosts}
        if self.headers.get("Host") not in ok:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc in ok

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def do_GET(self):
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        url = urlsplit(self.path)
        path = url.path
        if path == "/api/_setup":
            return self._json(200, {"result": self.server.setup_view()})
        if path == "/api/status":
            return self._json(200, self.server.pool.status())
        if path == "/api/_jobs":
            return self._json(200, {"result": self.server.jobs.list()})
        if path.startswith("/api/_jobs/"):
            try:
                off = int((parse_qs(url.query).get("from") or ["0"])[0])
                return self._json(200, {"result": self.server.jobs.get(int(path.rsplit("/", 1)[1]), off)})
            except (KeyError, ValueError):
                return self._json(404, {"error": "No such job."})
        if path.startswith("/custom-icons/"):
            import base64
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[1] not in ("global", "jp") or parts[2] not in ("support", "supportart"):
                return self._send(404)
            try:
                r = self.server.pool.call(parts[1], "custom_support.art", None, {"id": int(parts[3])})["result"]
                art = r.get("art", "")
                body = base64.b64decode(art.removeprefix("data:image/png;base64,")) if art else icons.get(parts[2], r["donor"])
                return self._send(200, body, "image/png") if body else self._send(404)
            except (ValueError, WorkerError):
                return self._send(404)
        if path.startswith("/icons/"):
            parts = path.strip("/").split("/")
            try:
                body = icons.get(parts[1], int(parts[2])) if len(parts) == 3 else None
            except ValueError:
                body = None
            if body is None:
                return self._send(404, b"no icon", "text/plain", "no-store")
            return self._send(200, body, "image/png", "max-age=604800")
        if path == "/":
            path = "/index.html"
        file = (STATIC / path.lstrip("/")).resolve()
        if not file.is_file() or STATIC not in file.parents:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix == ".js":
            ctype = "text/javascript"
        cache = "max-age=31536000, immutable" if file.suffix == ".woff2" else "no-cache"
        self._send(200, file.read_bytes(), ctype, cache)

    def do_POST(self):
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        parts = urlsplit(self.path).path.strip("/").split("/")
        if parts[:2] == ["api", "_jobs"]:
            return self._jobs_post(parts[2:])
        if parts[:2] == ["api", "_setup"]:
            return self._setup_post(parts[2:])
        if len(parts) != 3 or parts[0] != "api":
            return self._json(404, {"error": "No such endpoint."})
        region, op = parts[1], parts[2]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "Request too large."})
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if op == "_restart":
                self.server.pool.restart(region)
                return self._json(200, {"result": {"restarted": region}})
            msg = self.server.pool.call(region, op, body.get("viewer"), body.get("args") or {})
            self._json(200, {"result": msg.get("result"), "log": msg.get("log") or "",
                             "history": msg.get("history") or []})
        except WorkerError as ex:
            self._json(400, {"error": str(ex), "detail": ex.detail})
        except Exception as ex:
            self._json(500, {"error": f"{type(ex).__name__}: {ex}", "detail": traceback.format_exc()})

    def _jobs_post(self, rest):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if 0 < n <= MAX_BODY else {}
            if rest == ["start"]:
                region = body.get("region") or "global"
                plan = self.server.pool.call(region, "jobs.command", None,
                                             {"recipe": body.get("recipe"), "params": body.get("params") or {}})["result"]
                return self._json(200, {"result": self.server.jobs.start(region, body.get("recipe"), plan)})
            if len(rest) == 2 and rest[1] == "cancel":
                return self._json(200, {"result": self.server.jobs.cancel(int(rest[0]))})
            return self._json(404, {"error": "No such endpoint."})
        except WorkerError as ex:
            self._json(400, {"error": str(ex), "detail": ex.detail})
        except (KeyError, ValueError) as ex:
            self._json(400, {"error": str(ex) or "Bad job request."})


    def _setup_post(self, rest):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if 0 < n <= MAX_BODY else {}
            srv = self.server
            if rest == []:
                srv.save_settings(body.get("settings") or {})
                return self._json(200, {"result": srv.setup_view()})
            if rest == ["check"]:
                region = body.get("region")
                if region not in S.REGIONS:
                    raise ValueError("Unknown region.")
                path = str(body.get("path") or "").strip()
                if not path:
                    raise ValueError("Enter a path.")
                fn = S.check_master if body.get("kind") == "master" else S.check_game_dir
                return self._json(200, {"result": fn(path, region)})
            if rest == ["finish"]:
                eff = S.resolve()
                required = getattr(srv, "required_region", None)
                if required and required not in eff["usable"]:
                    raise ValueError(f"Enable {required.upper()} and select its valid master.mdb before starting this server.")
                if not eff["usable"]:
                    raise ValueError("Turn on at least one region with a valid master.mdb first.")
                if not S.exists():
                    S.save(S.load())
                srv.pool.setup_mode = False
                return self._json(200, {"result": srv.setup_view()})
            return self._json(404, {"error": "No such endpoint."})
        except (KeyError, TypeError, ValueError) as ex:
            self._json(400, {"error": str(ex) or "Bad setup request."})


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, extra_hosts=()):
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        self.extra_hosts = frozenset(extra_hosts)
        self.pool = Pool()
        self.jobs = Jobs()
        self.pool.usable = set(S.apply()["usable"])
        self.pool.setup_mode = not self.pool.usable

    def setup_view(self) -> dict:
        eff = S.resolve()
        return {"settings": S.load(), "saved": S.exists(), "setup_mode": self.pool.setup_mode, **eff,
                "file": str(S.FILE),
                "dev": os.environ.get("UMA_ADMIN_DEV") == "1"}

    def save_settings(self, raw: dict) -> None:
        s = S.load()
        if "game_install" in raw:
            s["game_install"] = bool(raw["game_install"])
        for r in S.REGIONS:
            got = (raw.get("regions") or {}).get(r)
            if got is None:
                continue
            cur = s["regions"][r]
            if "enabled" in got:
                cur["enabled"] = bool(got["enabled"])
            for k in ("game_dir", "master"):
                if k in got:
                    cur[k] = str(got[k] or "").strip()
        for r in S.REGIONS:
            cur = s["regions"][r]
            if cur["master"] and not (c := S.check_master(cur["master"], r))["ok"]:
                raise ValueError(f"{r.upper()} master.mdb: {c['error']}")
            if cur["game_dir"] and s["game_install"] and not (c := S.check_game_dir(cur["game_dir"], r))["ok"]:
                raise ValueError(f"{r.upper()} game folder: {c['error']}")
        S.save(s)
        self.pool.configure(S.apply(s)["usable"])
