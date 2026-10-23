
from __future__ import annotations

import functools
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "cache" / "icons"
VIVLO_CACHE = Path.home() / "Documents/VivloBot/cache/icons"
LOCAL = Path(os.environ.get("UMA_TOOLS_ICONS", Path.home() / "Documents/Projects/uma-tools-master/icons"))
REMOTE = "https://gametora.com/images/umamusume/"
KINDS = ("item", "chara", "card", "support", "supportart", "skill", "banner", "bannerjp")
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


@functools.lru_cache(maxsize=1)
def _card_icons() -> dict:
    try:
        return json.loads((LOCAL.parent / "icons.json").read_text())
    except (OSError, ValueError):
        return {}


def _sources(kind: str, ident: int) -> list:
    if kind == "item":
        return [f"{REMOTE}items/item_icon_{ident:05d}.png"]
    if kind == "chara":
        return [LOCAL / "chara" / f"chr_icon_{ident}.png", f"{REMOTE}characters/icons/chr_icon_{ident}.png"]
    if kind == "card":
        names = _card_icons().get(str(ident))
        local = [LOCAL / "chara" / f"{names[0]}.png"] if isinstance(names, list) and names else []
        return local + [f"{REMOTE}characters/icons/chr_icon_{ident // 100}_{ident}_01.png"] + _sources("chara", ident // 100)
    if kind == "skill":
        return [LOCAL / "skill" / f"utx_ico_skill_{ident}.png", f"{REMOTE}skill_icons/utx_ico_skill_{ident}.png"]
    if kind == "support":
        return [LOCAL / "support" / f"support_card_s_{ident}.png", f"{REMOTE}supports/support_card_s_{ident}.png"]
    if kind == "banner":
        return [f"{REMOTE}en/gacha/img_bnr_gacha_{ident}.png"] + _sources("bannerjp", ident)
    if kind == "bannerjp":
        return [f"{REMOTE}gacha/img_bnr_gacha_{ident}.png"]
    if kind == "supportart":
        return [f"{REMOTE}supports/tex_support_card_{ident}.png"] + _sources("support", ident)
    return []


def _fetch(url: str) -> bytes | None | bool:
    req = urllib.request.Request(url, headers={"User-Agent": "UmaPS Admin Panel (personal icon cache)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return body if body[:4] == b"\x89PNG" else None
    except urllib.error.HTTPError as e:
        return None if e.code in (403, 404, 410) else False
    except Exception:
        return False


def get(kind: str, ident: int) -> bytes | None:
    if kind not in KINDS:
        return None
    cached = CACHE / kind / f"{ident}.png"
    if cached.is_file():
        return cached.read_bytes()
    theirs = VIVLO_CACHE / kind / f"{ident}.png"
    if theirs.is_file():
        return theirs.read_bytes()
    miss = cached.with_suffix(".miss")
    if miss.exists():
        return None
    with _guard:
        lock = _locks.setdefault(f"{kind}/{ident}", threading.Lock())
    with lock:
        if cached.is_file():
            return cached.read_bytes()
        transient = False
        for src in _sources(kind, ident):
            if isinstance(src, Path):
                if src.is_file():
                    return src.read_bytes()
                continue
            body = _fetch(src)
            if body:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(body)
                return body
            transient |= body is False
        if not transient:
            miss.parent.mkdir(parents=True, exist_ok=True)
            miss.touch()
        return None
