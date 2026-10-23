
from __future__ import annotations

import copy
import functools

from ... import master_data
from ... import state as state_store
from ...handlers import gacha
from ...result_codes import ResultCode, refuse

STEPUP_KEY = "jp_gacha_select_stepup"
PAID_COIN = 92
PROMISED_CARD = 1
_SLOT_MAX = 1000


def _ok(data: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@functools.lru_cache(maxsize=1)
def _ladders() -> dict:
    out: dict = {}
    for r in master_data.query(
            "SELECT stepup_id, target_gacha_id FROM gacha_stepup ORDER BY stepup_id, stepup_step"):
        out.setdefault(r["stepup_id"], []).append(r["target_gacha_id"])
    return out


@functools.lru_cache(maxsize=1)
def _rung_index() -> dict:
    return {gid: (sid, i) for sid, rungs in _ladders().items() for i, gid in enumerate(rungs)}


def ladder_of(gacha_id: int):
    return _rung_index().get(int(gacha_id))


@functools.lru_cache(maxsize=None)
def _rung(gacha_id: int) -> dict:
    return dict(master_data.query_one(
        "SELECT cost_type, cost_single, draw_limit, gacha_odds_id, draw_guarantee_type "
        "FROM gacha_data WHERE id=?", (gacha_id,)) or {})


@functools.lru_cache(maxsize=None)
def _slots(stepup_id: int) -> tuple:
    return tuple(r["card_id"] for r in master_data.query(
        "SELECT card_id FROM gacha_available WHERE gacha_id=? AND card_id<? ORDER BY card_id",
        (stepup_id, _SLOT_MAX)))


@functools.lru_cache(maxsize=None)
def _candidates(stepup_id: int) -> frozenset:
    return frozenset(r["card_id"] for r in master_data.query(
        "SELECT card_id FROM select_pickup WHERE gacha_id=?", (stepup_id,)))


def _progress(full_state: dict, stepup_id: int) -> dict:
    stored = (full_state.get(STEPUP_KEY) or {}).get(str(stepup_id)) or {}
    return {"loop_count": max(1, int(stored.get("loop_count") or 1)),
            "step": int(stored.get("step") or 0),
            "selected_info_array": list(stored.get("selected_info_array") or []),
            "before_selected_info_array": list(stored.get("before_selected_info_array") or [])}


def _info(stepup_id: int, prog: dict) -> dict:
    return {"stepup_id": stepup_id, "loop_count": prog["loop_count"],
            "selected_info_array": copy.deepcopy(prog["selected_info_array"]),
            "before_selected_info_array": copy.deepcopy(prog["before_selected_info_array"])}


def _finished(stepup_id: int, prog: dict) -> bool:
    limit = int(_rung(stepup_id).get("draw_limit") or 0)
    return bool(limit) and prog["loop_count"] > limit


def rung_banner(banner: dict) -> dict:
    hit = ladder_of(banner["gacha_id"])
    if not hit:
        return banner
    row = _rung(banner["gacha_id"])
    if not banner.get("pool") and row.get("gacha_odds_id"):
        banner["pool"] = [dict(r) for r in master_data.query(
            "SELECT card_id, rarity, odds, is_pickup FROM gacha_available WHERE gacha_id=?",
            (row["gacha_odds_id"],))]
    single = int(row.get("cost_single") or 0)
    banner.update({"single_cost": single, "multi_cost": single * 10,
                   "paid_only": row.get("cost_type") == PAID_COIN,
                   "promised_card": row.get("draw_guarantee_type") == PROMISED_CARD,
                   "counts_pity": False})
    return banner


def picked_pool(full_state: dict, gacha_id: int, pool: list) -> list:
    hit = ladder_of(gacha_id)
    if not hit:
        return pool
    picks = {int(s.get("target_card_id") or 0): int(s.get("selected_card_id") or 0)
             for s in _progress(full_state, hit[0])["selected_info_array"]}
    out = []
    for c in pool:
        if c["card_id"] < _SLOT_MAX:
            if not picks.get(c["card_id"]):
                continue
            c = {**c, "card_id": picks[c["card_id"]]}
        out.append(c)
    return out


if rung_banner not in gacha.BANNER_HOOKS:
    gacha.BANNER_HOOKS.append(rung_banner)
if picked_pool not in gacha.POOL_HOOKS:
    gacha.POOL_HOOKS.append(picked_pool)


def decorate_index(full_state: dict, data: dict) -> None:
    listed, infos = [], []
    for g in data.get("gacha_info_list") or []:
        hit = ladder_of(g["id"])
        if not hit:
            listed.append(g)
            continue
        stepup_id = hit[0]
        if any(i["stepup_id"] == stepup_id for i in infos):
            continue
        prog = _progress(full_state, stepup_id)
        if _finished(stepup_id, prog):
            continue
        rungs = _ladders()[stepup_id]
        listed.append({**g, "id": rungs[min(prog["step"], len(rungs) - 1)]})
        infos.append(_info(stepup_id, prog))
    data["gacha_info_list"] = listed
    data["gacha_select_stepup_info_array"] = infos


def handle_select_stepup(payload: dict) -> dict:
    info = payload.get("select_stepup_info") or {}
    stepup_id = int(info.get("stepup_id") or 0)
    if stepup_id not in _ladders():
        return refuse(ResultCode.PARAM_ERROR)
    slots, candidates = _slots(stepup_id), _candidates(stepup_id)
    picks = [{"target_card_id": int(s.get("target_card_id") or 0),
              "selected_card_id": int(s.get("selected_card_id") or 0)}
             for s in info.get("selected_info_array") or []]
    picks.sort(key=lambda p: p["target_card_id"])
    if ([p["target_card_id"] for p in picks] != list(slots)
            or len({p["selected_card_id"] for p in picks}) != len(picks)):
        return refuse(ResultCode.PARAM_ERROR)
    if any(p["selected_card_id"] not in candidates for p in picks):
        return refuse(ResultCode.GACHA_UNSELECTABLE_CARD_SELECTED)
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    prog = _progress(full_state, stepup_id)
    if _finished(stepup_id, prog):
        return refuse(ResultCode.GACHA_NOT_IN_TERM)
    prog["selected_info_array"] = picks
    full_state.setdefault(STEPUP_KEY, {})[str(stepup_id)] = prog
    state_store.save_state(viewer_id, full_state)
    return _ok({"select_stepup_info": _info(stepup_id, prog)})


def handle_exec(payload: dict) -> dict:
    gacha_id = int(payload.get("gacha_id") or 0)
    hit = ladder_of(gacha_id)
    if not hit:
        return gacha.handle_exec(payload)
    stepup_id, step = hit
    viewer_id = payload["viewer_id"]
    prog = _progress(state_store.get_state(viewer_id) or {}, stepup_id)
    if _finished(stepup_id, prog) or prog["step"] != step:
        return refuse(ResultCode.GACHA_NOT_IN_TERM)
    if len(prog["selected_info_array"]) != len(_slots(stepup_id)):
        return refuse(ResultCode.GACHA_UNSELECTABLE_CARD_SELECTED)
    if int(payload.get("draw_num") or 0) != 10 or payload.get("item_id"):
        return refuse(ResultCode.PARAM_ERROR)

    resp = gacha.handle_exec(payload)
    if (resp.get("data_headers") or {}).get("result_code") != 1:
        return resp
    full_state = state_store.get_state(viewer_id) or {}
    prog = _progress(full_state, stepup_id)
    if step + 1 < len(_ladders()[stepup_id]):
        prog["step"] = step + 1
    else:
        prog.update({"step": 0, "loop_count": prog["loop_count"] + 1,
                     "before_selected_info_array": prog["selected_info_array"],
                     "selected_info_array": []})
    full_state.setdefault(STEPUP_KEY, {})[str(stepup_id)] = prog
    state_store.save_state(viewer_id, full_state)
    return resp


HANDLERS = {
    "gacha/select_stepup": handle_select_stepup,
    "gacha/exec": handle_exec,
}
