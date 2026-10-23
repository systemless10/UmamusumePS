
from __future__ import annotations

import copy
import functools
import random
import re
import time

from ... import master_data
from ... import state as state_store
from ...handlers import conditions, shop, single_mode_team
from ...result_codes import ResultCode, refuse

STATE_KEY = "jp_trainer_skills"
CHECK_KEY = "jp_trainer_skill_check_time"
CAREER_KEY = "jp_career_trainer_skills"
GRANT_KEY = "jp_career_trainer_skill_grants"
COIN_ITEM_ID = 333

MOTIVATION_GUARD = 1
CONDITION_GUARD = 2
RACE_CONTINUE_COUNT_UP = 3
FACTOR_GET = 4
FACTOR_UPGRADE = 5
RELATION_UP = 6
FACTOR_SUCCESSION = 7
SKILL_POINT_DISCOUNT = 8


def _ok(data: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@functools.lru_cache(maxsize=1)
def master() -> dict:
    return {r["id"]: dict(r) for r in master_data.query("SELECT * FROM trainer_skill")}


def active(full_state: dict, now: float | None = None) -> list:
    now = time.time() if now is None else now
    return [dict(s) for s in full_state.get(STATE_KEY) or [] if s.get("active_end_time", 0) >= now]


def usable_in_career(skill_id: int, idle: bool = False) -> bool:
    row = master().get(skill_id)
    return bool(row and row["enable_idle_single_mode" if idle else "enable_single_mode"])


def handle_index(payload: dict) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    return _ok({"trainer_skill_info": {"trainer_skill_array": active(full_state)},
                "last_activate_id_array": [],
                "new_trainer_skill_last_check_time": int(full_state.get(CHECK_KEY) or 0)})


def handle_activate(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now = int(time.time())
    want = payload.get("activate_trainer_skill_array") or []
    cost, plan = 0, []
    for a in want:
        try:
            sid, num = int(a["trainer_skill_id"]), int(a.get("activate_num") or 1)
        except (KeyError, TypeError, ValueError):
            return refuse()
        row = master().get(sid)
        if row is None or num < 1 or row["start_date"] > now:
            return refuse()
        cost += row["need_item_num"] * num
        plan.append((sid, num, row["active_time"] * 60))
    if not plan:
        return refuse()
    have = shop.item_count(full_state, COIN_ITEM_ID)
    if have < cost:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)
    shop.add_item(full_state, COIN_ITEM_ID, -cost)
    running = {s["trainer_skill_id"]: s for s in active(full_state, now)}
    for sid, num, secs in plan:
        start = max(now, running[sid]["active_end_time"] + 1) if sid in running else now
        running[sid] = {"trainer_skill_id": sid, "active_end_time": start + secs * num - 1}
    full_state[STATE_KEY] = sorted(running.values(), key=lambda s: s["trainer_skill_id"])
    state_store.save_state(viewer_id, full_state)
    touched = {sid for sid, _, _ in plan}
    return _ok({"trainer_skill_array": [copy.deepcopy(s) for s in full_state[STATE_KEY]
                                        if s["trainer_skill_id"] in touched],
                "user_item_array": [{"item_id": COIN_ITEM_ID,
                                     "number": shop.item_count(full_state, COIN_ITEM_ID)}],
                "maintenance_id_array": []})


def handle_save_notify_last_check_time(payload: dict) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    full_state[CHECK_KEY] = int(payload.get("save_time") or time.time())
    state_store.save_state(payload["viewer_id"], full_state)
    return _ok({})


def _ack(payload: dict) -> dict:
    return _ok({})


_START = re.compile(r"^single_mode(_[a-z]+)?/start$")
_CHECK_EVENT = re.compile(r"^single_mode(_[a-z]+)?/check_event$")
_GRANTS = {MOTIVATION_GUARD: (41, 42), CONDITION_GUARD: (43,), SKILL_POINT_DISCOUNT: (40,)}


def _carried(full_state: dict) -> set:
    return set(full_state.get(CAREER_KEY) or ())


def before(endpoint: str, payload: dict) -> dict | None:
    if not _START.match(endpoint):
        return None
    want = (payload.get("start_chara") or {}).get("request_trainer_skill_id_array") or []
    if not want:
        return None
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    running = {s["trainer_skill_id"] for s in active(full_state)}
    if any(int(sid) not in running or not usable_in_career(int(sid)) for sid in want):
        return refuse(ResultCode.SINGLE_MODE_TRAINER_SKILL_MISS_MATCH_ERROR)
    return None


def after(endpoint: str, payload: dict, resp: dict) -> None:
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    data = resp.get("data")
    if not isinstance(data, dict):
        return
    viewer_id = payload.get("viewer_id")
    if endpoint == "load/index":
        full_state = state_store.get_state(viewer_id) or {}
        data["trainer_skill_info"] = {"trainer_skill_array": active(full_state)}
        data["new_trainer_skill_last_check_time"] = int(full_state.get(CHECK_KEY) or 0)
    elif _START.match(endpoint):
        want = sorted({int(s) for s in (payload.get("start_chara") or {})
                       .get("request_trainer_skill_id_array") or []})
        full_state = state_store.get_state(viewer_id) or {}
        full_state[CAREER_KEY] = want
        full_state[GRANT_KEY] = [e for sid in want for e in _GRANTS.get(sid, ())]
        state_store.save_state(viewer_id, full_state)
        data["effected_trainer_skill_id_array"] = want
    elif _CHECK_EVENT.match(endpoint):
        full_state = state_store.get_state(viewer_id) or {}
        owed = full_state.get(GRANT_KEY)
        career = full_state.get(single_mode_team.STATE_KEY)
        if not owed or not isinstance(career, dict):
            return
        ci = career["data"]["chara_info"]
        for effect_id in owed:
            conditions.add(ci, effect_id)
        full_state[GRANT_KEY] = []
        state_store.save_state(viewer_id, full_state)
        if isinstance(data.get("chara_info"), dict):
            data["chara_info"]["chara_effect_id_array"] = list(ci.get("chara_effect_id_array") or [])


def _factor_hook(full_state: dict, factors: list, missed: list) -> list:
    carried = _carried(full_state)
    out = [dict(f) for f in factors]
    if FACTOR_GET in carried and missed:
        for fid in random.sample(missed, min(3, len(missed))):
            out.append({"factor_id": fid, "level": 0,
                        "effected_trainer_skill_id_array": [FACTOR_GET], "before_factor_id": 0})
    if FACTOR_UPGRADE in carried:
        pool = [f for f in out if f["factor_id"] % 100 in (1, 2) and _has_three_star(f["factor_id"] // 100)]
        for f in random.sample(pool, min(5, len(pool))):
            f["before_factor_id"] = f["factor_id"]
            f["factor_id"] = f["factor_id"] // 100 * 100 + 3
            f["effected_trainer_skill_id_array"] = (
                list(f.get("effected_trainer_skill_id_array") or []) + [FACTOR_UPGRADE])
    return out


@functools.lru_cache(maxsize=None)
def _has_three_star(group: int) -> bool:
    return master_data.query_one("SELECT 1 FROM succession_factor WHERE factor_group_id=? "
                                 "AND rarity=3", (group,)) is not None


def _viewer_carries(skill_id: int, amount: int):
    def hook(viewer_id) -> int:
        return amount if skill_id in _carried(state_store.get_state(viewer_id) or {}) else 0
    return hook


single_mode_team.FACTOR_ROLL_HOOKS.append(_factor_hook)
single_mode_team.INSPIRATION_COMPAT_HOOKS.append(_viewer_carries(RELATION_UP, 10))
single_mode_team.SENIOR_UNIQUE_HOOKS.append(_viewer_carries(FACTOR_SUCCESSION, 1))
single_mode_team.CONTINUE_BONUS_HOOKS.append(
    lambda full_state: 10 if RACE_CONTINUE_COUNT_UP in _carried(full_state) else 0)


HANDLERS = {
    "trainer_skill/index": handle_index,
    "trainer_skill/activate": handle_activate,
    "trainer_skill/save_notify_last_check_time": handle_save_notify_last_check_time,
    "trainer_skill/save_show_refund_dialog": _ack,
}
