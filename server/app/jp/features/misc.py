
from __future__ import annotations

import copy
import datetime
import functools
import json

from ... import master_data, region
from ... import state as state_store
from ...handlers import gacha, shop, single_mode_team
from . import paid_gacha
from . import stepup
from ...result_codes import ResultCode, refuse


_JST = datetime.timezone(datetime.timedelta(hours=9))


def _ok(data: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _lenders(payload: dict) -> dict:
    resp = single_mode_team.handle_friend_support_card_reload(
        {"viewer_id": payload["viewer_id"],
         "exclude_viewer_id_array": payload.get("exclude_viewer_id_array") or []})
    return resp.get("data") or {}


def handle_support_card_deck_index(payload: dict) -> dict:
    data = _lenders(payload)
    data["rental_support_card_info"] = {"rental_num": 0, "campaign_rental_support_data_id": 0}
    return _ok(data)


def handle_search_support_card(payload: dict) -> dict:
    want = int(payload.get("support_card_id") or 0)
    data = _lenders(payload)
    cards = [c for c in data.get("support_card_data_array") or [] if c.get("support_card_id") == want]
    owners = {c.get("viewer_id") for c in cards}
    users = [u for u in data.get("summary_user_info_array") or [] if u.get("viewer_id") in owners]
    return _ok({"summary_user_info_array": users, "support_card_data_array": cards})


@functools.lru_cache(maxsize=1)
def _race_fit_snapshot() -> dict:
    snap = json.loads((region.seeds_dir() / "race_fit_assistance.json").read_text(encoding="utf-8"))
    snap.pop("_source", None)
    return snap


def handle_race_fit_assistance(payload: dict) -> dict:
    data = _lenders(payload)
    data["recommended_scenario_support_card_array"] = copy.deepcopy(
        _race_fit_snapshot()["recommended_scenario_support_card_array"])
    return _ok(data)


def handle_race_fit_fetch(payload: dict) -> dict:
    snap = copy.deepcopy(_race_fit_snapshot())
    now = datetime.datetime.now(_JST)
    snap["race_fit_assistance_array"] = [
        e for e in snap["race_fit_assistance_array"]
        if datetime.datetime.strptime(e["end_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_JST) > now]
    return _ok(snap)


def handle_exchange_multi_resource(payload: dict) -> dict:
    resp = shop.handle_exchange_multi(payload)
    data = resp.get("data")
    if isinstance(data, dict):
        data["use_item_info_array"] = copy.deepcopy(payload.get("use_item_info_array") or [])
    return resp


PICKUP_KEY = "jp_gacha_select_pickup"


_STANDIN_MAX = 1000


def _candidates(gacha_id: int) -> list:
    return [r["card_id"] for r in master_data.query(
        "SELECT card_id FROM select_pickup WHERE gacha_id=? ORDER BY recommend_order",
        (gacha_id,))]


def _standins(pool: list) -> tuple:
    ids = sorted((c["card_id"], bool(c.get("is_pickup"))) for c in pool
                 if c["card_id"] < _STANDIN_MAX)
    return [i for i, p in ids if p], [i for i, p in ids if not p]


def _chosen(full_state: dict, gacha_id: int, candidates: list, targets: list) -> dict:
    stored = ((full_state.get(PICKUP_KEY) or {}).get(str(gacha_id)) or {})
    chosen: dict = {}
    for s in stored.get("selected_info_array") or []:
        target, card = int(s.get("target_card_id") or 0), int(s.get("selected_card_id") or 0)
        if target in targets and card in candidates and card not in chosen.values():
            chosen[target] = card
    spare = [c for c in candidates if c not in chosen.values()]
    for target in targets:
        if target not in chosen and spare:
            chosen[target] = spare.pop(0)
    return chosen


def pickup_pool(full_state: dict, gacha_id: int, pool: list) -> list:
    if stepup.ladder_of(gacha_id):
        return pool
    candidates = _candidates(int(gacha_id))
    targets, plain = _standins(pool)
    if not candidates or not (targets or plain):
        return pool
    swap = _chosen(full_state, int(gacha_id), candidates, targets)
    unchosen = [c for c in candidates if c not in swap.values()]
    swap.update(zip(plain, unchosen))
    out = []
    for c in pool:
        if c["card_id"] < _STANDIN_MAX:
            if c["card_id"] not in swap:
                continue
            c = {**c, "card_id": swap[c["card_id"]]}
        out.append(c)
    return out


if pickup_pool not in gacha.POOL_HOOKS:
    gacha.POOL_HOOKS.append(pickup_pool)


def handle_select_pickup(payload: dict) -> dict:
    info = payload.get("select_pickup_info") or {}
    gacha_id = int(info.get("gacha_id") or 0)
    banner = gacha._banner(gacha_id) if gacha_id else None
    candidates = _candidates(gacha_id)
    if not banner or not candidates:
        return refuse(ResultCode.PARAM_ERROR)
    targets, _ = _standins(banner["pool"])
    picks = [{"target_card_id": int(s.get("target_card_id") or 0),
              "selected_card_id": int(s.get("selected_card_id") or 0)}
             for s in info.get("selected_info_array") or []]
    if (not picks
            or any(p["target_card_id"] not in targets or p["selected_card_id"] not in candidates
                   for p in picks)
            or len({p["target_card_id"] for p in picks}) != len(picks)
            or len({p["selected_card_id"] for p in picks}) != len(picks)):
        return refuse(ResultCode.PARAM_ERROR)
    stored = {"gacha_id": gacha_id, "selected_info_array": picks}
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state.setdefault(PICKUP_KEY, {})[str(gacha_id)] = stored
    state_store.save_state(viewer_id, full_state)
    return _ok({"select_pickup_info": copy.deepcopy(stored)})


def handle_gacha_index(payload: dict) -> dict:
    resp = gacha.handle_index(payload)
    data = resp.get("data")
    if not isinstance(data, dict):
        return resp
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    stepup.decorate_index(full_state, data)
    from .. import assets
    assets.filter_gacha_index(data)
    paid_gacha.decorate_index(full_state, data)
    listed = {g["id"] for g in data.get("gacha_info_list") or []}
    data["gacha_select_pickup_info_array"] = [
        copy.deepcopy(info) for gid, info in (full_state.get(PICKUP_KEY) or {}).items()
        if int(gid) in listed]
    return resp


def handle_limit_exchange_ticket(payload: dict) -> dict:
    gacha_id = int(payload.get("gacha_id") or 0)
    row = master_data.query_one(
        "SELECT gacha_id, item_id, item_num, pay_item_num FROM gacha_item_exchange WHERE id=?",
        (int(payload.get("gacha_item_exchange_id") or 0),))
    if row is None or int(row["gacha_id"]) != gacha_id:
        return refuse(ResultCode.PARAM_ERROR)
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    cost = int(row["pay_item_num"])
    if gacha.limit_points(full_state, gacha_id) < cost:
        return refuse()
    converted = full_state.setdefault(gacha.CONVERTED_KEY, {})
    converted[str(gacha_id)] = (converted.get(str(gacha_id)) or 0) + cost
    item_id = int(row["item_id"])
    before = shop._item_count(full_state, item_id)
    credited = shop._add_item(full_state, item_id, int(row["item_num"])) - before
    state_store.save_state(viewer_id, full_state)
    summary = shop._empty_summary()
    if credited > 0:
        summary["add_item_list"] = [{"item_id": item_id, "number": credited}]
    return _ok({"reward_summary_info": summary,
                "limit_item_info": gacha.limit_item_info(full_state, gacha_id)})


HANDLERS = {
    "support_card_deck/index": handle_support_card_deck_index,
    "friend/search_support_card": handle_search_support_card,
    "race_fit_assistance/users_with_recommended_support_card": handle_race_fit_assistance,
    "race_fit_assistance/fetch": handle_race_fit_fetch,
    "item/exchange_multi_resource": handle_exchange_multi_resource,
    "gacha/index": handle_gacha_index,
    "gacha/select_pickup": handle_select_pickup,
    "gacha/limit_exchange_ticket": handle_limit_exchange_ticket,
}
