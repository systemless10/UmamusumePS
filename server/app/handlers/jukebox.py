
from __future__ import annotations

import time

from .. import state as state_store
from .jukebox_requests import _jukebox_state

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
