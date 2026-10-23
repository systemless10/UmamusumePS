
from __future__ import annotations

import copy

from .. import state as state_store
from ..patch import _servertime

SAVE_KEY = "live_theater_save_state"
CHECK_KEY = "live_theater_last_checked_time"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now = _servertime()
    last = full_state.get(CHECK_KEY) or now
    full_state[CHECK_KEY] = now
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "live_theater_save_info_array": copy.deepcopy(full_state.get(SAVE_KEY) or []),
        "live_theater_last_checked_time": last,
    })


def handle_live_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    save_info = payload.get("live_theater_save_info") or {}
    full_state = state_store.get_state(viewer_id) or {}
    music_id = save_info.get("music_id")
    if music_id is not None:
        saves = [s for s in (full_state.get(SAVE_KEY) or [])
                 if s.get("music_id") != music_id]
        saves.append(copy.deepcopy(save_info))
        full_state[SAVE_KEY] = saves
        state_store.save_state(viewer_id, full_state)
    return _ok({"live_theater_save_info": copy.deepcopy(save_info)})
