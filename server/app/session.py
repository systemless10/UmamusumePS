"""
Session/sid tracking.

VERIFIED 2026-08-11 against a real login (tools/capture_proxy.py --upstream
real, captures/20260811_141604/): the real server's data_headers.sid is NOT a
static per-viewer token. It's 42 hex chars shaped as a stable 32-hex BASE
(established fresh at tool/start_session) + a 10-hex SUFFIX that changes on
EVERY subsequent response -- confirmed across 15 consecutive real responses
(load/index, present/*, gacha/*, jukebox/*) sharing one base with a different
suffix each time, and two separate start_session calls producing two
different bases entirely.

Our sid used to be one value cached per viewer for the whole server process
lifetime -- fixed length now, but still completely static, which is a real
structural difference from what a working real session looks like. This
generates a fresh sid shaped the same way on every call: unchanged base,
new random suffix. The exact algorithm behind the real suffix is unknown (the
client only ever needs to store and re-send whatever the server hands it, per
the reference client's own next_sid handling -- it's not something the client
independently recomputes/verifies), so a random one matches the observed wire
shape without needing to reverse the real formula.
"""

from __future__ import annotations

import secrets

from . import region

_BASE_LEN = 32  # hex chars
_SUFFIX_LEN = 10  # hex chars

_bases: dict[str, str] = {}


def _key(viewer_id: str) -> str:
    # Region-namespaced so a JP and a Global account sharing a numeric
    # viewer_id never share a session base -- see region.py's docstring.
    return f"{region.CURRENT_REGION.get()}:{viewer_id}"


def start_session(viewer_id: str) -> str:
    """Establish a fresh session base for this viewer (tool/start_session)."""
    _bases[_key(viewer_id)] = secrets.token_hex(_BASE_LEN // 2)
    return _roll(viewer_id)


def get_or_create_sid(viewer_id: str) -> str:
    """The sid for this response: same base as the session, new suffix."""
    key = _key(viewer_id)
    if key not in _bases:
        _bases[key] = secrets.token_hex(_BASE_LEN // 2)
    return _roll(viewer_id)


def _roll(viewer_id: str) -> str:
    return _bases[_key(viewer_id)] + secrets.token_hex(_SUFFIX_LEN // 2)
