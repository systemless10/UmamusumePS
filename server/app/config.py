
from __future__ import annotations

import json
import logging
import os

from . import region

log = logging.getLogger("uma-server")

CONFIG_PATH = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client_config.json")
    if region.PROCESS_REGION == "global"
    else str(region.data_dir() / "client_config.json"))

_cache: dict = {"mtime": None, "data": {}}


def load() -> dict:
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                _cache["data"] = json.load(fh) or {}
            log.info("client_config.json reloaded: %s",
                     {k: v for k, v in _cache["data"].items() if not k.startswith("_")})
        except Exception:
            log.exception("client_config.json unreadable; using built-in defaults")
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def get(key: str, default=None):
    value = load().get(key)
    return default if value is None else value


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        log.warning("client_config.json %s is not an integer; using %s", key, default)
        return default


def get_float(key: str):
    value = load().get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("client_config.json %s is not a number: %r", key, value)
        return None
