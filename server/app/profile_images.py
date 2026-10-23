from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from . import region

log = logging.getLogger(__name__)

PREFIX = "profile-cards"
REQUEST_BASE: contextvars.ContextVar[str] = contextvars.ContextVar("PROFILE_IMAGE_BASE", default="")
_KEY_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")
_MAX_BYTES = 4_000_000

router = APIRouter()


def set_request_base(headers: dict) -> None:
    host = headers.get("host") or headers.get("Host") or ""
    if host:
        REQUEST_BASE.set(f"https://{host}")


def _dir() -> Path:
    d = region.data_dir() / "profile_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store(owner: str, image_b64: str, client_hash: str) -> dict | None:
    try:
        raw = base64.b64decode(image_b64, validate=False)
    except Exception:
        log.warning("profile image from %s is not base64", owner)
        return None
    if not raw or len(raw) > _MAX_BYTES:
        log.warning("profile image from %s rejected (%d bytes)", owner, len(raw or b""))
        return None
    digest = hashlib.sha1(raw).hexdigest().upper()
    h = (client_hash or "").upper() or digest
    if h != digest:
        log.warning("profile image from %s: client hash %s != sha1 %s", owner, h, digest)
    key = f"{owner}G{h[:16]}"
    if not _KEY_RE.match(key):
        return None
    for old in _dir().glob(f"{owner}G*"):
        old.unlink(missing_ok=True)
    (_dir() / key).write_bytes(raw)
    return {"hash": h, "path": key, "unique_id": str(uuid.uuid4())}


def exists(path: str | None) -> bool:
    return bool(path) and bool(_KEY_RE.match(path)) and (_dir() / path).is_file()


def url(path: str | None) -> str | None:
    if not exists(path):
        return None
    base = REQUEST_BASE.get()
    return f"{base}/{PREFIX}/{path}" if base else None


def wire(photo: dict | None) -> dict:
    photo = photo or {}
    u = url(photo.get("path"))
    return {"image_file_status": 1 if u else 0,
            "image_file_url": u,
            "image_unique_id": photo.get("unique_id") or str(uuid.uuid4())}


@router.get("/" + PREFIX + "/{key}")
async def serve(key: str):
    if not _KEY_RE.match(key) or not (_dir() / key).is_file():
        raise HTTPException(404)
    return Response((_dir() / key).read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})
