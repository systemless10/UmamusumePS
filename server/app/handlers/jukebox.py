"""jukebox/index -- the home-screen Jukebox player.

Capture ground truth (2026-08-16, real server, op 0007): empty request;
response data {request_history {request_id, request_type, request_value,
requester_id, requester_request_id, music_id, request_time}, like_count,
summary_user_info: [], is_liked}. This REPLACES an earlier best-effort shape
(jukebox_info{...}, add_music_array) written before any capture of this
endpoint existed anywhere -- that shape was invented and wrong on every
field; this one is copied straight off the real response.

request_history is the most recent entry in this viewer's jukebox history --
the same list jukebox/play_user_request and jukebox/draw_random_request
append to (see jukebox_requests.py._jukebox_state), so all three endpoints
stay consistent with each other. A brand-new viewer with no history yet gets
null, matching what an account with nothing queued would show.

like_count / is_liked have NO other capture anywhere (no like/unlike
endpoint has ever been seen) -- there is nothing to compute a real count
from, so they are served at their empty defaults (0 / false) rather than
guessed at."""

from __future__ import annotations

import time

from .. import state as state_store
from .jukebox_requests import _jukebox_state

# Fallback default when a viewer has no jukebox history yet (fresh account,
# or one whose story/state was reset). A real capture of this exact case
# (brand-new account, never touched the jukebox) doesn't exist -- every
# capture we have is from an account that had already played it -- so
# `null` here is UNVERIFIED against real-server behavior. Sending a
# well-formed placeholder instead (music 1006: sort 1, condition_type 0 --
# always-unlocked, master.mdb's apparent "default" track) avoids exercising
# whatever the client's UI does with a genuinely-null request_history, which
# is a very plausible never-tested-on-the-real-client path.
_DEFAULT_MUSIC_ID = 1006
_DEFAULT_REQUESTER = 1001


def _default_history_entry() -> dict:
    return {
        "request_id": 0, "request_type": 1, "request_value": 0,
        "requester_id": _DEFAULT_REQUESTER, "requester_request_id": None,
        "music_id": _DEFAULT_MUSIC_ID,
        "request_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)
    history = st.get("history") or []
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "request_history": history[-1] if history else _default_history_entry(),
                "like_count": 0,
                "summary_user_info": [],
                "is_liked": False,
            }}
