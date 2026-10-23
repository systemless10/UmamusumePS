
from __future__ import annotations

import secrets

from . import region

_BASE_LEN = 32
_SUFFIX_LEN = 10

_bases: dict[str, str] = {}


def _key(viewer_id: str) -> str:
    return f"{region.CURRENT_REGION.get()}:{viewer_id}"


def start_session(viewer_id: str) -> str:
    _bases[_key(viewer_id)] = secrets.token_hex(_BASE_LEN // 2)
    return _roll(viewer_id)


def get_or_create_sid(viewer_id: str) -> str:
    key = _key(viewer_id)
    if key not in _bases:
        _bases[key] = secrets.token_hex(_BASE_LEN // 2)
    return _roll(viewer_id)


def _roll(viewer_id: str) -> str:
    return _bases[_key(viewer_id)] + secrets.token_hex(_SUFFIX_LEN // 2)
