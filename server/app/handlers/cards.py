
from __future__ import annotations

import copy
import time

from .. import event_engine
from .. import gametora
from .. import master_data
from .. import state as state_store
from . import collection, note_archive, registry, shop
from .stories import _grant_reward
from ..result_codes import ResultCode, refuse

_MONEY_ITEM_ID = 59
_GLOBAL_EXP_ITEM_ID = 110
_CLEAT_ITEM_BY_RARITY = {1: 48, 2: 49, 3: 50}
_CLEAT_ITEM_CATEGORY = 160
_CLEATS_PER_CARD = 10
_CLOVER_ITEM_ID = 57
_CLOVER_ITEM_CATEGORY = 99
_CLOVERS_PER_PIECE = 1
_MAX_LIMIT_BREAK = 4
_MAX_DECK_NAME_LEN = 30

_TALENT_MONEY = {2: 10000, 3: 50000, 4: 150000, 5: 500000}


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


@registry.endpoint("card/get_release_card_array")
def handle_get_release_card_array(payload: dict) -> dict:
    return _ok({"release_card_array": note_archive._release_card_array()})


def _find(entries: list, key: str, value) -> dict | None:
    return next((e for e in entries if isinstance(e, dict) and e.get(key) == value), None)


def _card_entry(full_state: dict, card_id: int) -> dict | None:
    return _find(full_state.setdefault(collection.CARD_LIST_KEY, []), "card_id", card_id)


def _support_entry(full_state: dict, support_card_id: int) -> dict | None:
    return _find(full_state.setdefault(collection.SUPPORT_CARD_KEY, []),
                 "support_card_id", support_card_id)


@registry.endpoint("card/rarity_upgrade")
def handle_rarity_upgrade(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    now_rarity = payload.get("now_rarity")
    new_rarity = payload.get("new_rarity")

    entry = _card_entry(full_state, card_id)
    master = master_data.query_one(
        "SELECT default_rarity, get_piece_id FROM card_data WHERE id=?", (card_id,))
    if entry is None or master is None:
        return _refuse()
    if entry.get("rarity") != now_rarity or not new_rarity or new_rarity <= now_rarity:
        return _refuse()
    if not master_data.query_one(
            "SELECT id FROM card_rarity_data WHERE card_id=? AND rarity=?",
            (card_id, new_rarity)):
        return _refuse()

    need = master_data.query_one(
        "SELECT piece_num FROM need_piece_num_data WHERE rarity=?",
        (master["default_rarity"],))
    if need is None:
        return _refuse()
    cost = need["piece_num"] * (new_rarity - now_rarity)

    piece_id = master["get_piece_id"] or card_id
    pieces = full_state.setdefault("piece_list_state", [])
    piece = _find(pieces, "piece_id", piece_id)
    if piece is None or (piece.get("piece_num") or 0) < cost:
        return _refuse()
    piece["piece_num"] -= cost
    entry["rarity"] = new_rarity

    summary = shop._empty_summary()
    cloth = full_state.setdefault("cloth_list_state", [])
    for r in range(now_rarity + 1, new_rarity + 1):
        row = master_data.query_one(
            "SELECT get_dress_id_1, get_dress_id_2 FROM card_rarity_data "
            "WHERE card_id=? AND rarity=?", (card_id, r))
        for key in ("get_dress_id_1", "get_dress_id_2"):
            dress = row[key] if row else 0
            if dress and not _find(cloth, "cloth_id", dress):
                cloth.append({"cloth_id": dress})
                summary["add_cloth_list"].append({"cloth_id": dress})

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "card_data": copy.deepcopy(entry),
        "piece_data": [{"piece_id": piece_id, "piece_num": piece["piece_num"]}],
        "reward_result_info": summary,
    })


def _jp_talent_bill(talent_group_id: int, now_level: int, new_level: int):
    row = master_data.query_one(
        "SELECT * FROM card_talent_level_upgrade_item WHERE talent_group_id=? "
        "AND talent_level=? AND upgrade_talent_level=?",
        (talent_group_id, now_level, new_level))
    if row is None or int(row["start_date"] or 0) > time.time():
        return None
    money = master_data.query_one(
        "SELECT money_num FROM card_talent_level_upgrade_money "
        "WHERE talent_level=? AND upgrade_talent_level=?", (now_level, new_level))
    if money is None:
        return None
    costs: dict[int, int] = {}
    for i in range(1, 14):
        if row[f"item_category_{i}"]:
            iid = row[f"item_id_{i}"]
            costs[iid] = costs.get(iid, 0) + row[f"item_num_{i}"]
    return costs, money["money_num"]


def _global_talent_bill(talent_group_id: int, now_level: int, new_level: int):
    if new_level > 5:
        return None
    costs: dict[int, int] = {}
    money = 0
    for level in range(now_level + 1, new_level + 1):
        row = master_data.query_one(
            "SELECT * FROM card_talent_upgrade WHERE talent_group_id=? AND talent_level=?",
            (talent_group_id, level))
        if row is None:
            return None
        for i in range(1, 7):
            if row[f"item_category_{i}"]:
                iid = row[f"item_id_{i}"]
                costs[iid] = costs.get(iid, 0) + row[f"item_num_{i}"]
        money += _TALENT_MONEY.get(level, 0)
    return costs, money


@registry.endpoint("card/talent_strengthen")
def handle_talent_strengthen(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    now_level = payload.get("talent_level")
    new_level = payload.get("new_talent_level")

    entry = _card_entry(full_state, card_id)
    master = master_data.query_one(
        "SELECT talent_group_id FROM card_data WHERE id=?", (card_id,))
    if entry is None or master is None:
        return _refuse()
    if entry.get("talent_level") != now_level or not isinstance(new_level, int) \
            or new_level <= now_level:
        return _refuse()

    bill_for = (_jp_talent_bill if master_data.has_table("card_talent_level_upgrade_item")
                else _global_talent_bill)
    bill = bill_for(master["talent_group_id"], now_level, new_level)
    if bill is None:
        return _refuse()
    costs, money = bill
    costs[_MONEY_ITEM_ID] = costs.get(_MONEY_ITEM_ID, 0) + money

    if any(shop._item_count(full_state, iid) < num for iid, num in costs.items()):
        return _refuse()
    item_data_array = []
    for iid, num in costs.items():
        item_data_array.append({"item_id": iid,
                                "number": shop._add_item(full_state, iid, -num)})
    entry["talent_level"] = new_level

    state_store.save_state(viewer_id, full_state)
    return _ok({"card_data": copy.deepcopy(entry),
                "item_data_array": item_data_array})


_HINT_LEVEL_UNLOCK = ((7, 5), (6, 4), (1, 3))


def card_max_talent(card_id: int) -> int:
    row = master_data.query_one("SELECT talent_group_id FROM card_data WHERE id=?", (card_id,))
    if row is None or not master_data.has_table("card_talent_level_upgrade_item"):
        return 5
    top = master_data.query_one(
        "SELECT MAX(upgrade_talent_level) AS m FROM card_talent_level_upgrade_item "
        "WHERE talent_group_id=? AND COALESCE(start_date, 0) <= ?",
        (row["talent_group_id"], int(time.time())))
    return max(5, int(top["m"])) if top and top["m"] else 5


def clamp_card_talents(entries: list) -> int:
    fixed = 0
    for e in entries:
        if not isinstance(e, dict) or int(e.get("talent_level") or 1) <= 5:
            continue
        cap = card_max_talent(int(e.get("card_id") or 0))
        if e["talent_level"] > cap:
            e["talent_level"] = cap
            hint_cap = _max_hint_level(cap)
            for s in e.get("skill_data_array") or []:
                if isinstance(s, dict) and int(s.get("level") or 0) > hint_cap:
                    s["level"] = hint_cap
            fixed += 1
    return fixed


def _max_hint_level(talent_level: int) -> int:
    return next(cap for need, cap in _HINT_LEVEL_UNLOCK if talent_level >= need)


def _hint_upgrade_row(card_id: int, skill_rarity: int, level: int):
    if master_data.has_column("card_talent_hint_upgrade", "card_id"):
        return master_data.query_one(
            "SELECT * FROM card_talent_hint_upgrade WHERE card_id IN (?, 0) "
            "AND rarity=? AND talent_level=? ORDER BY card_id DESC",
            (card_id, skill_rarity, level))
    return master_data.query_one(
        "SELECT * FROM card_talent_hint_upgrade WHERE rarity=? AND talent_level=?",
        (skill_rarity, level))


@registry.endpoint("card/skill_upgrade")
def handle_skill_upgrade(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    skill_id = payload.get("skill_id")
    now_level = payload.get("skill_level")
    new_level = payload.get("new_skill_level")

    entry = _card_entry(full_state, card_id)
    skill = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (skill_id,)) \
        if isinstance(skill_id, int) else None
    if entry is None or skill is None or skill["rarity"] not in (1, 2):
        return _refuse()
    if not isinstance(now_level, int) or not isinstance(new_level, int) \
            or new_level <= now_level \
            or new_level > _max_hint_level(int(entry.get("talent_level") or 1)):
        return _refuse()

    skills = entry.setdefault("skill_data_array", [])
    current = next((s for s in skills if s.get("skill_id") == skill_id), None)
    if (current.get("level") if current else 0) != now_level:
        return _refuse()

    costs: dict[int, int] = {}
    money = 0
    for level in range(now_level + 1, new_level + 1):
        row = _hint_upgrade_row(card_id, skill["rarity"], level)
        if row is None:
            return _refuse()
        for i in range(1, 7):
            if f"item_category_{i}" in row.keys() and row[f"item_category_{i}"]:
                iid = row[f"item_id_{i}"]
                costs[iid] = costs.get(iid, 0) + row[f"item_num_{i}"]
        money += row["money_num"] or 0
    costs[_MONEY_ITEM_ID] = costs.get(_MONEY_ITEM_ID, 0) + money

    if any(shop._item_count(full_state, iid) < num for iid, num in costs.items()):
        return _refuse()
    item_data_array = []
    for iid, num in costs.items():
        item_data_array.append({"item_id": iid,
                                "number": shop._add_item(full_state, iid, -num)})

    if current:
        current["level"] = new_level
    else:
        skills.append({"skill_id": skill_id, "level": new_level})

    state_store.save_state(viewer_id, full_state)
    return _ok({"card_data": copy.deepcopy(entry), "item_data_array": item_data_array})


_EVENT_TITLE_CATEGORY = 181
_SUPPORT_STORY_BASE = 800000000


def _event_story_ids(support_card_id: int) -> dict:
    row = master_data.query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (support_card_id,))
    owners = [support_card_id]
    if row and row["chara_id"]:
        owners.append(row["chara_id"])
    out = {}
    for owner in owners:
        lo = _SUPPORT_STORY_BASE + owner * 1000
        for r in master_data.query(
                "SELECT [index], text FROM text_data WHERE category=? "
                "AND [index]>=? AND [index]<? ORDER BY [index]",
                (_EVENT_TITLE_CATEGORY, lo, lo + 1000)):
            key = event_engine.title_key(r["text"])
            if key:
                out.setdefault(key, r["index"])
    return out


@registry.endpoint("support_card/get_support_card_event_skill")
def handle_get_support_card_event_skill(payload: dict) -> dict:
    support_card_id = payload.get("support_card_id") or 0
    events = gametora.load_cached("support", support_card_id) or {}
    story_ids = _event_story_ids(support_card_id)
    out = []
    for event in events.values():
        if not isinstance(event, dict):
            continue
        skills = []
        for choice in event.get("choices") or ():
            for effect in choice.get("effects") or ():
                sid = effect.get("skill_id")
                if sid and sid not in skills:
                    skills.append(sid)
        if not skills:
            continue
        story_id = story_ids.get(event_engine.title_key(event.get("name") or ""))
        if story_id is None:
            continue
        out.append({"story_id": story_id, "skill_id_array": skills})
    out.sort(key=lambda e: e["story_id"])
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"event_skill_list": out}}


@registry.endpoint("support_card/limit_break")
def handle_limit_break(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    support_card_id = payload.get("support_card_id")

    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    lb = entry.get("limit_break_count") or 0
    if lb >= _MAX_LIMIT_BREAK:
        return _refuse()

    materials = int(payload.get("material_support_card_num") or 0)
    if materials > 0:
        if lb + materials > _MAX_LIMIT_BREAK or (entry.get("stock") or 0) < materials:
            return _refuse()
        entry["stock"] = entry["stock"] - materials
        entry["limit_break_count"] = lb + materials
    else:
        rarity = master_data.query_one(
            "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
        if rarity is None:
            return _refuse()
        row = master_data.query_one(
            "SELECT item_id, item_num FROM support_card_limit_break WHERE rarity=?",
            (rarity["rarity"],))
        if row is None:
            return _refuse()
        if shop._item_count(full_state, row["item_id"]) < row["item_num"]:
            return _refuse()
        shop._add_item(full_state, row["item_id"], -row["item_num"])
        entry["limit_break_count"] = lb + 1

    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_data": copy.deepcopy(entry)})


def _support_level_cap(support_card_id: int, limit_break_count: int):
    rarity = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
    if rarity is None:
        return None
    limits = master_data.query_one(
        "SELECT * FROM support_card_limit WHERE rarity=?", (rarity["rarity"],))
    if limits is None:
        return None
    cap_level = limits[f"limit_{max(0, min(_MAX_LIMIT_BREAK, limit_break_count))}"]
    cap = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (rarity["rarity"], cap_level))
    return (cap_level, cap["total_exp"]) if cap else None


@registry.endpoint("support_card/strengthen")
def handle_support_strengthen(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    support_card_id = payload.get("support_card_id")
    use_exp = int(payload.get("use_global_exp") or 0)
    use_money = int(payload.get("use_money") or 0)

    entry = _support_entry(full_state, support_card_id)
    if entry is None or use_exp <= 0 or use_money < 0:
        return _refuse()
    cap = _support_level_cap(support_card_id, entry.get("limit_break_count") or 0)
    if cap is None or (entry.get("exp") or 0) + use_exp > cap[1]:
        return _refuse()
    if shop._item_count(full_state, _GLOBAL_EXP_ITEM_ID) < use_exp \
            or shop._item_count(full_state, _MONEY_ITEM_ID) < use_money:
        return _refuse()

    exp_left = shop._add_item(full_state, _GLOBAL_EXP_ITEM_ID, -use_exp)
    money_left = shop._add_item(full_state, _MONEY_ITEM_ID, -use_money)
    entry["exp"] = (entry.get("exp") or 0) + use_exp

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "support_card_data": copy.deepcopy(entry),
        "item_data_array": [{"item_id": _GLOBAL_EXP_ITEM_ID, "number": exp_left},
                            {"item_id": _MONEY_ITEM_ID, "number": money_left}],
    })


@registry.endpoint("support_card/sell")
def handle_support_card_sell(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    requests = payload.get("sell_support_card_info_array") or []
    if not requests:
        return _refuse()

    plan = []
    for req in requests:
        support_card_id = req.get("support_card_id")
        sell_count = int(req.get("stock") or 0)
        entry = _support_entry(full_state, support_card_id)
        if entry is None or sell_count <= 0 or (entry.get("stock") or 0) < sell_count:
            return _refuse()
        if entry.get("is_locked"):
            return _refuse()
        rarity = master_data.query_one(
            "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
        cleat_item_id = _CLEAT_ITEM_BY_RARITY.get(rarity["rarity"]) if rarity else None
        if cleat_item_id is None:
            return _refuse()
        plan.append((entry, sell_count, cleat_item_id))

    summary = shop._empty_summary()
    reward_info_array = []
    for entry, sell_count, cleat_item_id in plan:
        entry["stock"] = (entry.get("stock") or 0) - sell_count
        reward_info_array.extend(_grant_reward(
            full_state, _CLEAT_ITEM_CATEGORY, cleat_item_id,
            sell_count * _CLEATS_PER_CARD, summary, viewer_id))

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "reward_info_array": reward_info_array,
    })


@registry.endpoint("card/sell_piece")
def handle_sell_piece(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    requests = payload.get("sell_piece_data_array") or []
    if not requests:
        return _refuse()

    pieces = full_state.setdefault("piece_list_state", [])
    plan = []
    for req in requests:
        piece_id = req.get("piece_id")
        sell_count = int(req.get("piece_num") or 0)
        piece = _find(pieces, "piece_id", piece_id)
        if piece is None or sell_count <= 0 or (piece.get("piece_num") or 0) < sell_count:
            return _refuse()
        plan.append((piece, sell_count))

    summary = shop._empty_summary()
    piece_data = []
    total_clovers = 0
    for piece, sell_count in plan:
        piece["piece_num"] -= sell_count
        total_clovers += sell_count * _CLOVERS_PER_PIECE
        piece_data.append({"piece_id": piece["piece_id"], "piece_num": piece["piece_num"]})
    _grant_reward(full_state, _CLOVER_ITEM_CATEGORY, _CLOVER_ITEM_ID,
                  total_clovers, summary, viewer_id)

    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary, "piece_data": piece_data})


_TRAINEE_STORY_BASE = 500000000


def _chara_event_story_ids(card_id: int) -> dict:
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    chara_id = (row["chara_id"] if row and row["chara_id"] else card_id // 100)
    lo = _TRAINEE_STORY_BASE + chara_id * 1000
    out = {}
    for r in master_data.query(
            "SELECT [index], text FROM text_data WHERE category=? "
            "AND [index]>=? AND [index]<? ORDER BY [index]",
            (_EVENT_TITLE_CATEGORY, lo, lo + 1000)):
        key = event_engine.title_key(r["text"])
        if key:
            out.setdefault(key, r["index"])
    return out


@registry.endpoint("card/get_card_event_skill")
def handle_get_card_event_skill(payload: dict) -> dict:
    card_id = payload.get("card_id") or 0
    events = gametora.load_cached("chara", card_id) or {}
    story_ids = _chara_event_story_ids(card_id)
    out = []
    for event in events.values():
        if not isinstance(event, dict):
            continue
        skills = []
        for choice in event.get("choices") or ():
            for effect in choice.get("effects") or ():
                sid = effect.get("skill_id")
                if sid and sid not in skills:
                    skills.append(sid)
        if not skills:
            continue
        story_id = story_ids.get(event_engine.title_key(event.get("name") or ""))
        if story_id is None:
            continue
        out.append({"story_id": story_id, "skill_id_array": skills})
    out.sort(key=lambda e: e["story_id"])
    return _ok({"event_skill_list": out})


@registry.endpoint("card/unlock")
def handle_card_unlock(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    card_id = payload.get("card_id")
    if not isinstance(card_id, int) or isinstance(card_id, bool):
        return _refuse()
    master = master_data.query_one(
        "SELECT default_rarity, get_piece_id FROM card_data WHERE id=?", (card_id,))
    if master is None or card_id not in note_archive._release_card_array():
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if _card_entry(full_state, card_id) is not None:
        return _refuse()

    need = master_data.query_one(
        "SELECT piece_num FROM need_piece_num_data WHERE rarity=?",
        (master["default_rarity"],))
    if need is None:
        return _refuse()
    cost = need["piece_num"]

    piece_id = master["get_piece_id"] or card_id
    pieces = full_state.setdefault("piece_list_state", [])
    piece = _find(pieces, "piece_id", piece_id)
    if piece is None or (piece.get("piece_num") or 0) < cost:
        return _refuse()
    piece["piece_num"] -= cost

    summary = shop._empty_summary()
    shop._grant(full_state, {"change_item_category": shop._CARD_CATEGORY,
                             "change_item_id": card_id,
                             "change_item_num": 1,
                             "additional_piece_num": 0}, 1, summary)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "piece_data": [{"piece_id": piece_id, "piece_num": piece["piece_num"]}],
    })


@registry.endpoint("support_card/change_lock")
def handle_support_card_change_lock(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    support_card_id = payload.get("support_card_id")
    lock_flag = payload.get("lock_flag")
    if not isinstance(support_card_id, int) or isinstance(support_card_id, bool):
        return _refuse()
    if not isinstance(lock_flag, int) or isinstance(lock_flag, bool):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    entry["is_locked"] = 1 if lock_flag else 0
    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_id": support_card_id,
                "is_locked": entry["is_locked"]})


@registry.endpoint("support_card/limit_break_item")
def handle_limit_break_item(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    support_card_id = payload.get("support_card_id")
    item_id = payload.get("limit_break_item_id")
    count = payload.get("limit_break_count")
    if not isinstance(support_card_id, int) or isinstance(support_card_id, bool):
        return _refuse()
    if count is None:
        count = 1
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    lb = entry.get("limit_break_count") or 0
    if lb + count > _MAX_LIMIT_BREAK:
        return _refuse()

    rarity = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
    if rarity is None:
        return _refuse()
    row = master_data.query_one(
        "SELECT item_id, item_num FROM support_card_limit_break WHERE rarity=?",
        (rarity["rarity"],))
    if row is None:
        return _refuse()
    if item_id is not None and item_id != row["item_id"]:
        return _refuse()

    total = row["item_num"] * count
    if shop._item_count(full_state, row["item_id"]) < total:
        return _refuse()
    remaining = shop._add_item(full_state, row["item_id"], -total)
    entry["limit_break_count"] = lb + count

    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_data": copy.deepcopy(entry),
                "item_data_array": [{"item_id": row["item_id"],
                                     "number": remaining}]})


@registry.endpoint("support_card_deck/change_name")
def handle_deck_change_name(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    deck_id = payload.get("deck_id")
    name = payload.get("name")
    if not isinstance(deck_id, int) or isinstance(deck_id, bool):
        return _refuse()
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_DECK_NAME_LEN:
        return _refuse()

    collection.get_or_seed(viewer_id, "support_card_deck_array",
                           collection._deck_seed())
    full_state = state_store.get_state(viewer_id) or {}
    decks = full_state.get(collection.SUPPORT_DECK_KEY) or []
    deck = _find(decks, "deck_id", deck_id)
    if deck is None:
        deck = {"deck_id": deck_id, "name": "", "support_card_id_array": []}
        decks.append(deck)
    deck["name"] = name.strip()
    full_state[collection.SUPPORT_DECK_KEY] = decks
    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_deck": copy.deepcopy(deck)})
