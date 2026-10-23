
from __future__ import annotations

import copy

from ... import state as state_store
from ...result_codes import refuse

KEY = "jp_schedule_book_setting"


def stored(full_state: dict) -> dict | None:
    value = full_state.get(KEY)
    return value if isinstance(value, dict) else None


def handle_setting(payload: dict) -> dict:
    setting = payload.get("schedule_book_setting")
    if not isinstance(setting, dict):
        return refuse()
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[KEY] = copy.deepcopy(setting)
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": None}


def handle_index(payload: dict) -> dict:
    setting = stored(state_store.get_state(payload["viewer_id"]) or {})
    data = {"schedule_book_setting": copy.deepcopy(setting)} if setting else {}
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def note_walk(full_state: dict, chara_id: int) -> None:
    setting = stored(full_state)
    walking = (setting or {}).get("schedule_book_const_walking_setting")
    if isinstance(walking, dict):
        walking["selected_chara_id"] = int(chara_id)


HANDLERS = {
    "schedule_book/setting": handle_setting,
    "schedule_book/index": handle_index,
}
