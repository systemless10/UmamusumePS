
from __future__ import annotations

import copy
import functools
import time

from ... import master_data, scenarios
from ... import state as state_store
from ...handlers import missions, single_mode_team
from ...result_codes import ResultCode, refuse

COLLECTION_KEY = "jp_plushies"
LAST_CHECKED_KEY = "jp_plushie_last_checked_time"

FAILED, SUCCESS, GREAT_SUCCESS, SUCCESS_WITH_BIG, GREAT_SUCCESS_WITH_BIG = 1, 2, 3, 4, 5
_GREAT_SUCCESS_MIN = 6
_BIG = 2
_APPEARANCES = (0, 1, _BIG)
_STORY_TIER = {FAILED: 1, SUCCESS: 2, GREAT_SUCCESS: 3,
               SUCCESS_WITH_BIG: 2, GREAT_SUCCESS_WITH_BIG: 3}

_COND_COLLECTION_KINDS = 600040


def _ok(data: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _state(viewer_id) -> dict:
    return state_store.get_state(viewer_id) or {}


def _key(p: dict) -> tuple:
    return (int(p["chara_id"]), int(p["dress_id"]), int(p.get("appearance") or 0))


def _order(row: dict) -> tuple:
    return (row["dress_id"], row["chara_id"], row["appearance"])


@functools.lru_cache(maxsize=4096)
def _dress_chara(dress_id: int) -> int | None:
    row = master_data.query_one("SELECT chara_id FROM dress_data WHERE id=?", (dress_id,))
    return row["chara_id"] if row else None


@functools.lru_cache(maxsize=512)
def _is_chara(chara_id: int) -> bool:
    return master_data.query_one("SELECT id FROM chara_data WHERE id=?", (chara_id,)) is not None


def _valid(plushies) -> bool:
    if not isinstance(plushies, list):
        return False
    for p in plushies:
        try:
            chara, dress, appearance = _key(p)
        except (KeyError, TypeError, ValueError):
            return False
        if appearance not in _APPEARANCES or not _is_chara(chara):
            return False
        if _dress_chara(dress) not in (0, chara):
            return False
    return True


def result_state(plushies: list) -> int:
    caught = len(plushies)
    if not caught:
        return FAILED
    big = any(int(p.get("appearance") or 0) == _BIG for p in plushies)
    if caught >= _GREAT_SUCCESS_MIN:
        return GREAT_SUCCESS_WITH_BIG if big else GREAT_SUCCESS
    return SUCCESS_WITH_BIG if big else SUCCESS


def record(full_state: dict, plushies: list, now: int) -> bool:
    rows = full_state.setdefault(COLLECTION_KEY, [])
    by_key = {_key(r): r for r in rows}
    new = False
    for p in plushies:
        k = _key(p)
        row = by_key.get(k)
        if row is None:
            row = {"chara_id": k[0], "dress_id": k[1], "appearance": k[2],
                   "count": 0, "first_acquired_time": now}
            rows.append(row)
            by_key[k] = row
            new = True
        row["count"] += 1
    rows.sort(key=_order)
    return new


def new_plushie_exists(full_state: dict) -> bool:
    seen = full_state.get(LAST_CHECKED_KEY) or 0
    return any(r["first_acquired_time"] > seen for r in full_state.get(COLLECTION_KEY) or [])


def handle_collection(payload: dict) -> dict:
    full_state = _state(payload["viewer_id"])
    return _ok({"collected_plushies": copy.deepcopy(full_state.get(COLLECTION_KEY) or []),
                "last_checked_time": full_state.get(LAST_CHECKED_KEY) or 0})


def handle_update_last_checked_time(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = _state(viewer_id)
    full_state[LAST_CHECKED_KEY] = int(time.time())
    state_store.save_state(viewer_id, full_state)
    return _ok({})


def handle_finish_claw_crane(payload: dict) -> dict:
    plushies = payload.get("plushies") or []
    if not _valid(plushies):
        return refuse(ResultCode.PARAM_ERROR)
    viewer_id = payload["viewer_id"]
    full_state = _state(viewer_id)
    is_new = record(full_state, plushies, int(time.time()))
    state_store.save_state(viewer_id, full_state)
    return _ok({"result_state": result_state(plushies), "is_new_plushie_contained": is_new})


def handle_career_finish_claw_crane(payload: dict) -> dict:
    plushies = payload.get("plushies") or []
    if not _valid(plushies):
        return refuse(ResultCode.PARAM_ERROR)
    state = result_state(plushies)
    viewer_id = payload["viewer_id"]
    full_state = _state(viewer_id)
    if not isinstance(full_state.get(single_mode_team.STATE_KEY), dict):
        return single_mode_team.handle_minigame_end(payload)
    is_new = record(full_state, plushies, int(time.time()))
    state_store.save_state(viewer_id, full_state)
    resp = single_mode_team.handle_minigame_end(dict(payload, result={
        "result_state": _STORY_TIER[state],
        "result_value": len(plushies),
        "result_detail_array": [{k: p.get(k) for k in ("get_id", "chara_id", "dress_id", "motion", "face")}
                                for p in plushies]}))
    data = resp.get("data")
    if isinstance(data, dict):
        data["result_state"] = state
        data["is_new_plushie_contained"] = is_new
    return resp


def after(endpoint: str, payload: dict, resp: dict) -> None:
    if endpoint != "load/index" or (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    data = resp.get("data")
    if isinstance(data, dict):
        data["new_plushie_exists"] = new_plushie_exists(_state(payload.get("viewer_id")))


def _collection_kinds(viewer_id, full_state: dict, row) -> int:
    return len(full_state.get(COLLECTION_KEY) or [])


missions.CONDITION_HOOKS[_COND_COLLECTION_KINDS] = _collection_kinds

HANDLERS = {
    "plushie/collection": handle_collection,
    "plushie/update_last_checked_time": handle_update_last_checked_time,
    "plushie/finish_claw_crane": handle_finish_claw_crane,
    **{f"{s.endpoint_prefix}/finish_claw_crane": handle_career_finish_claw_crane
       for s in scenarios.all_scenarios()},
}
