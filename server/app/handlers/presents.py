
from __future__ import annotations

import copy
import time

from .. import master_data
from .. import state as state_store
from . import collection, registry, shop
from ..result_codes import ResultCode, refuse

PRESENT_BOX_KEY = "present_box_state"
NO_LIMIT_TIME = 4133948399
_FIRST_PRESENT_ID = 900000001
_CANNED_MESSAGE_ID = 9000
_FREE_MESSAGE_ID = 99999
_TYPE_CARD = 50
_TYPE_SUPPORT = 51
_TYPE_CLOTH = 70
_TYPE_MUSIC = 80
_TYPE_CARAT = 90
_TYPE_PIECE = 102
_INT32_MAX = 2_147_483_647


def _clamp_int32(value: int) -> int:
    return min(value, _INT32_MAX)


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


def _add_item(full_state: dict, item_id: int, delta: int) -> int:
    before = shop._item_count(full_state, item_id)
    return shop._add_item(full_state, item_id, delta) - before


def _summary_add_item(summary: dict, item_id: int, num: int) -> None:
    for i in summary["add_item_list"]:
        if i.get("item_id") == item_id:
            i["number"] += num
            return
    summary["add_item_list"].append({"item_id": item_id, "number": num})


def _is_time_limited(present: dict) -> bool:
    return (present.get("reward_limit_time") or NO_LIMIT_TIME) < NO_LIMIT_TIME


def _hidden_custom_ids() -> set:
    from .. import custom_support
    ids = custom_support.known_ids()
    return ids - custom_support.visible_ids() if ids else set()


def _visible(present: dict, hidden: set | None = None) -> bool:
    if hidden is None:
        hidden = _hidden_custom_ids()
    return not (present.get("reward_type") == _TYPE_SUPPORT
                and present.get("reward_id") in hidden)


def _claimable(present: dict, now_epoch: int, hidden: set | None = None) -> bool:
    return ((present.get("state") or 0) == 0
            and _visible(present, hidden)
            and (present.get("reward_limit_time") or NO_LIMIT_TIME) >= now_epoch)


_CAT_ALL = 0
_CAT_CARD = 1
_CAT_SUPPORT = 2
_CAT_ITEM = 3
_CAT_OTHER = 99

_CATEGORY_OF_TYPE = {
    _TYPE_CARD: _CAT_CARD,
    _TYPE_SUPPORT: _CAT_SUPPORT,
    _TYPE_CARAT: _CAT_ITEM,
    _TYPE_PIECE: _CAT_ITEM,
    _TYPE_CLOTH: _CAT_OTHER,
    _TYPE_MUSIC: _CAT_OTHER,
}


def _category_of(present: dict) -> int:
    return _CATEGORY_OF_TYPE.get(present.get("reward_type") or 0, _CAT_ITEM)


def _match_category(present: dict, categories: list) -> bool:
    if not categories or _CAT_ALL in categories:
        return True
    return _category_of(present) in categories


def _match_filters(present: dict, time_filter: int, categories: list) -> bool:
    if time_filter == 1 and not _is_time_limited(present):
        return False
    if time_filter == 2 and _is_time_limited(present):
        return False
    return _match_category(present, categories)


def _pending(full_state: dict, now_epoch: int) -> list:
    box = full_state.get(PRESENT_BOX_KEY) or {}
    hidden = _hidden_custom_ids()
    return [p for p in box.get("presents", []) if _claimable(p, now_epoch, hidden)]


def _pending_counts(pending: list) -> dict:
    limited = sum(1 for p in pending if _is_time_limited(p))
    return {"no_time_limit_present_num": len(pending) - limited,
            "time_limit_present_num": limited}


def _grant(viewer_id, full_state: dict, present: dict, summary: dict, now: str) -> None:
    rtype = present.get("reward_type") or 0
    rid = present.get("reward_id")
    num = present.get("reward_count") or 1
    if rtype == _TYPE_CARAT:
        wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
        before = wallet.get("fcoin") or 0
        wallet["fcoin"] = _clamp_int32(before + num)
        summary["add_fcoin"] += wallet["fcoin"] - _clamp_int32(before)
    elif rtype == _TYPE_SUPPORT:
        cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
        owned = next((c for c in cards if c.get("support_card_id") == rid), None)
        if owned:
            owned["stock"] = (owned.get("stock") or 0) + num
        else:
            owned = {"viewer_id": viewer_id, "support_card_id": rid, "exp": 0,
                     "limit_break_count": 0, "favorite_flag": 0, "stock": num - 1,
                     "possess_time": now, "create_time": now}
            cards.append(owned)
        collection.echo_support_card(summary, owned)
        summary["add_support_card_num_array"].append(
            {"support_card_id": rid, "number": num, "item_type": 51})
    elif rtype == _TYPE_CLOTH:
        cloths = full_state.setdefault("cloth_list_state", [])
        if not any(c.get("cloth_id") == rid for c in cloths):
            cloths.append({"cloth_id": rid})
        summary["add_cloth_list"].append({"cloth_id": rid})
    elif rtype == _TYPE_MUSIC:
        musics = full_state.setdefault("music_list_state", [])
        if not any(m.get("music_id") == rid for m in musics):
            musics.append({"music_id": rid, "acquisition_time": now})
        summary["add_music_list"].append({"music_id": rid, "acquisition_time": now})
    elif rtype == _TYPE_PIECE:
        pieces = full_state.setdefault("piece_list_state", [])
        p = next((p for p in pieces if p.get("piece_id") == rid), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + num
        else:
            pieces.append({"piece_id": rid, "piece_num": num})
        summary["add_piece_list"].append({"piece_id": rid, "piece_num": num})
    elif rtype == _TYPE_CARD:
        cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
        if not any(c.get("card_id") == rid for c in cards):
            rar = master_data.query_one(
                "SELECT MIN(rarity) AS r FROM card_rarity_data WHERE card_id=?", (rid,))
            cards.append({"card_id": rid, "rarity": (rar["r"] if rar and rar["r"] else 1),
                          "talent_level": 1, "skill_data_array": []})
            summary["add_card_list"].append(
                {"card_id": rid, "rarity": 3, "talent_level": 1, "create_time": now})
            bonus = _empty_summary()
            bonus["add_cloth_list"] = [{"cloth_id": rid}]
            bonus["add_total_fan"] = 1
            summary["add_card_bonus_info"] = bonus
            chara = master_data.query_one(
                "SELECT chara_id FROM card_data WHERE id=?", (rid,))
            chara_id = chara["chara_id"] if chara else rid // 100
            charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
            entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                     "fan": 1, "max_grade": 0, "dress_id": 2,
                     "mini_dress_id": 2, "love_point_pool": 0}
            if not any(c.get("chara_id") == chara_id for c in charas):
                charas.append(entry)
                summary["add_chara_list"].append({k: v for k, v in entry.items()
                                                  if k != "love_point_pool"})
    else:
        applied = _add_item(full_state, rid, num)
        if applied:
            _summary_add_item(summary, rid, applied)


def _receive(payload: dict, wanted_ids=None) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    box = full_state.get(PRESENT_BOX_KEY) or {}
    presents = box.get("presents", [])
    now_epoch = int(time.time())
    now = _now()
    time_filter = payload.get("time_filter_type") or 0
    categories = payload.get("category_filter_type") or []
    summary = _empty_summary()
    claimed = 0
    hidden = _hidden_custom_ids()
    for p in presents:
        if not _claimable(p, now_epoch, hidden):
            continue
        if wanted_ids is not None and p.get("present_id") not in wanted_ids:
            continue
        if wanted_ids is None and not _match_filters(p, time_filter, categories):
            continue
        _grant(viewer_id, full_state, p, summary, now)
        p["state"] = 1
        p["receive_time"] = now
        claimed += 1
    if claimed:
        state_store.save_state(viewer_id, full_state)
    if wanted_ids is not None and not claimed:
        if any(p.get("present_id") in wanted_ids and (p.get("state") or 0) == 0
               for p in presents):
            return _refuse(ResultCode.PRESENT_NOT_RECEIVE_OVERDUE)
        return _refuse()
    return _ok({"reward_summary_info": summary,
                "receive_present_num": claimed,
                "rest_present_num": len(_pending(full_state, now_epoch)),
                "no_receive_support_card_count": 0,
                "no_receive_item_flg": False})


@registry.endpoint("present/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_epoch = int(time.time())
    pending = _pending(full_state, now_epoch)
    time_filter = payload.get("time_filter_type") or 0
    categories = payload.get("category_filter_type") or []
    shown = [p for p in pending if _match_filters(p, time_filter, categories)]
    shown.sort(key=lambda p: (p.get("create_time") or "", p.get("present_id") or 0),
               reverse=not payload.get("is_asc", True))
    offset = int(payload.get("offset") or 0)
    limit = int(payload.get("limit") or 100)
    return _ok({"present_array": copy.deepcopy(shown[offset:offset + limit]),
                "no_receive_present_num": _pending_counts(pending)})


_HISTORY_MAX_LIMIT = 500


@registry.endpoint("present/history")
def handle_history(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    box = full_state.get(PRESENT_BOX_KEY) or {}
    categories = payload.get("category_filter_type") or []

    hidden = _hidden_custom_ids()
    claimed = [p for p in box.get("presents", [])
               if (p.get("state") or 0) != 0
               and _visible(p, hidden)
               and _match_category(p, categories)]
    claimed.sort(key=lambda p: (p.get("receive_time") or "",
                                p.get("present_id") or 0),
                 reverse=not payload.get("is_asc", True))

    offset = max(0, int(payload.get("offset") or 0))
    limit = int(payload.get("limit") or 100)
    limit = max(1, min(limit, _HISTORY_MAX_LIMIT))
    return _ok({"present_array": copy.deepcopy(claimed[offset:offset + limit])})


@registry.endpoint("present/receive_all")
def handle_receive_all(payload: dict) -> dict:
    return _receive(payload, wanted_ids=None)


@registry.endpoint("present/receive")
def handle_receive(payload: dict) -> dict:
    ids = set()
    if payload.get("present_id") is not None:
        ids.add(payload["present_id"])
    for pid in payload.get("present_id_array") or []:
        ids.add(pid)
    if not ids:
        return _refuse()
    return _receive(payload, wanted_ids=ids)


def send(full_state: dict, item_category, item_id, item_num, message="",
         message_id: int | None = None, message_params=()) -> dict:
    params = (list(message_params) + [0, 0, 0, 0])[:4]
    box = full_state.setdefault(PRESENT_BOX_KEY, {})
    presents = box.setdefault("presents", [])
    pid = box.get("next_present_id") or _FIRST_PRESENT_ID
    entry = {
        "present_id": pid,
        "state": 0,
        "reward_type": int(item_category),
        "reward_id": int(item_id),
        "reward_count": int(item_num),
        "message_id": (message_id if message_id is not None
                       else _FREE_MESSAGE_ID if message else _CANNED_MESSAGE_ID),
        "message_param_value_1": int(params[0]),
        "message_param_value_2": int(params[1]),
        "message_param_value_3": int(params[2]),
        "message_param_value_4": int(params[3]),
        "reward_limit_time": NO_LIMIT_TIME,
        "receive_time": "0000-00-00 00:00:00",
        "create_time": _now(),
        "free_message": message or "",
    }
    presents.append(entry)
    box["next_present_id"] = pid + 1
    return copy.deepcopy(entry)


def admin_send(viewer_id, item_category, item_id, item_num, message="",
               message_id: int | None = None, message_params=()) -> dict:
    full_state = state_store.get_state(viewer_id) or {}
    entry = send(full_state, item_category, item_id, item_num, message,
                 message_id=message_id, message_params=message_params)
    state_store.save_state(viewer_id, full_state)
    return entry
