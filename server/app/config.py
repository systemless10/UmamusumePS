"""
Hand-editable runtime knobs, read from client_config.json next to the server
package.

Kept in its own module (rather than in main) so both the dispatcher and patch.py
can read it without an import cycle. The file is re-read whenever its mtime
changes, so edits take effect by relaunching the GAME -- no server restart. A
missing or malformed file falls back to the caller's default and logs, never
raising into a response.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("uma-server")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client_config.json")

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
        except Exception:  # noqa: BLE001 - a bad hand-edit must not break responses
            log.exception("client_config.json unreadable; using built-in defaults")
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def get(key: str, default=None):
    """Config value, or `default` when unset/null."""
    value = load().get(key)
    return default if value is None else value


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        log.warning("client_config.json %s is not an integer; using %s", key, default)
        return default
