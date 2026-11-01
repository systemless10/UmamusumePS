
from __future__ import annotations

from ... import state as state_store
from ...handlers import single_mode_team

KEY = "jp_continue_unlocked"
BONUS = 2

ENDINGS = {
    1: {4}, 2: {4}, 3: {4}, 4: {4},
    5: {205026, 205027},
    7: {207027, 207028},
    8: {208029, 208030, 208038},
    9: {209041},
    10: {210050, 210051, 210052},
    11: {211040, 211041},
    12: {212039, 212040},
    13: {213905},
    14: {214037},
}


def _scenario(full_state: dict) -> int:
    return int(single_mode_team._career_chara(full_state).get("scenario_id") or 1)


def unlocked(full_state: dict) -> bool:
    return _scenario(full_state) in set(full_state.get(KEY) or ())


def after(endpoint: str, payload: dict, resp: dict) -> None:
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    data = resp.get("data")
    if not isinstance(data, dict):
        return
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    home = data.get("home_info")
    fresh = False
    if endpoint.endswith("/check_event") and not unlocked(full_state):
        sc = _scenario(full_state)
        if int(payload.get("event_id") or 0) in ENDINGS.get(sc, ()):
            full_state[KEY] = sorted(set(full_state.get(KEY) or ()) | {sc})
            state_store.save_state(viewer_id, full_state)
            fresh = True
    if isinstance(home, dict) and "available_continue_num" in home:
        home["continue_unlock_flag"] = unlocked(full_state)
        if fresh:
            home.update(single_mode_team._continue_counts(full_state, None))


single_mode_team.CONTINUE_BONUS_HOOKS.append(lambda fs: BONUS if unlocked(fs) else 0)

HANDLERS: dict = {}
