
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response

from . import crypto
from . import dispatch as pipeline
from . import region
from .admin_lifecycle import lifespan

if region.PROCESS_REGION != "global":
    raise RuntimeError("app.main is the Global server; JP is app.jp.server")

_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "server.log", maxBytes=20_000_000, backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
logging.basicConfig(level=logging.DEBUG,
                    handlers=[logging.StreamHandler(), _file_handler])
logging.getLogger().handlers[0].setLevel(
    os.environ.get("UMA_LOG_LEVEL", "INFO").upper())
log = logging.getLogger("uma-server")

app = FastAPI(title="Umamusume private server (dev)", lifespan=lifespan)
from . import custom_support, custom_support_http
try:
    from . import ios_cdn
except ImportError:
    ios_cdn = None
from .jp.features import skill_upgrade
app.include_router(custom_support_http.router)
from . import profile_images
app.include_router(profile_images.router)


from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
_CDN_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="ioscdn") if ios_cdn else None


async def ios_cdn_index():
    from starlette.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    idx = await run_in_threadpool(ios_cdn.index)
    return JSONResponse(idx) if idx else Response(status_code=404)


async def ios_cdn_file(path: str):
    from fastapi.responses import FileResponse, RedirectResponse
    from starlette.concurrency import run_in_threadpool
    kind, target = ios_cdn.resolve(path)
    if kind == "file":
        return FileResponse(target, media_type="application/octet-stream")
    if (ios_cdn.active() or {}).get("upstream") == "proxy":
        import asyncio
        status, body, ctype = await asyncio.get_running_loop().run_in_executor(_CDN_POOL, ios_cdn.fetch_upstream, target)
        return Response(content=body, status_code=status, media_type=ctype)
    return RedirectResponse(target, status_code=302)


if ios_cdn:
    app.get("/ioscdn-index")(ios_cdn_index)
    app.get("/" + ios_cdn.PREFIX + "{path:path}")(ios_cdn_file)


@app.post("/{endpoint_path:path}")
async def dispatch(endpoint_path: str, request: Request):
    raw = await request.body()
    decoded = crypto.decode_request(raw, dict(request.headers))
    headers = dict(request.headers)
    profile_images.set_request_base(headers)
    cdn = ios_cdn.request_context(decoded.payload, headers) if ios_cdn else nullcontext(None)
    with cdn as overlay:
        evo_ep = endpoint_path.removeprefix("umamusume/")
        noted = skill_upgrade.before(evo_ep, decoded.payload)
        resp = pipeline.handle_decoded(endpoint_path, decoded.payload, decoded.sid or None)
        skill_upgrade.after(evo_ep, decoded.payload, resp, noted)
        patched = custom_support.project(resp)
        if overlay:
            patched = ios_cdn.patch_response(patched, overlay)
        rc = (patched.get("data_headers") or {}).get("result_code") if isinstance(patched, dict) else None
        if rc not in (None, 1):
            logging.getLogger("uma-server").warning("result_code %s for %s (ios overlay %s)", rc, endpoint_path, bool(overlay))
    body = crypto.encode_response(patched, decoded.udid_raw)
    return Response(content=body, media_type="application/x-msgpack; charset=utf-8")
