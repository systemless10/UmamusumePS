
from __future__ import annotations

import copy
import functools
import time

from .. import master_data
from .. import state as state_store
from . import collection, registry
from ..result_codes import ResultCode, refuse

SHOP_LIMIT_KEY = "shop_limit_state"
BONUS_FOLLOW_KEY = "bonus_follow_num_state"
BONUS_LIMITED_SALE_KEY = "bonus_limited_sale_open_num_state"
_CARAT_CATEGORY = 90
_CARD_CATEGORY = 50
_SUPPORT_CATEGORY = 51
_PIECE_CATEGORY = 102
_FOLLOW_FRAME_CATEGORY = 98

_FOLLOW_FRAME_ITEM = 132
_LIMITED_SALE_FRAME_ITEM = 296


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _row(exchange_id: int):
    return master_data.query_one(
        "SELECT * FROM item_exchange WHERE id=?", (exchange_id,))


def _unit_price(row, already_bought: int, count: int) -> int:
    gid = row["price_change_group_id"] or 0
    if not gid:
        return (row["pay_item_num"] or 0) * count
    total = 0
    for i in range(count):
        n = already_bought + i + 1
        pr = master_data.query_one(
            "SELECT pay_item_num FROM price_change "
            "WHERE group_id=? AND ?>=min_num AND ?<=max_num", (gid, n, n))
        total += (pr["pay_item_num"] if pr else (row["pay_item_num"] or 0))
    return total


def _items(full_state: dict) -> list:
    return full_state.setdefault("item_list_state", [])


def _item_count(full_state: dict, item_id: int) -> int:
    return next((i.get("number", 0) for i in _items(full_state)
                 if i.get("item_id") == item_id), 0)


@functools.lru_cache(maxsize=None)
def _item_limit(item_id: int) -> int | None:
    row = master_data.query_one("SELECT limit_num FROM item_data WHERE id=?", (item_id,))
    return row["limit_num"] if row and row["limit_num"] else None


def _add_item(full_state: dict, item_id: int, delta: int) -> int:
    cap = _item_limit(item_id)
    for i in _items(full_state):
        if i.get("item_id") == item_id:
            new_num = max(0, (i.get("number") or 0) + delta)
            i["number"] = min(new_num, cap) if cap else new_num
            return i["number"]
    if delta > 0:
        num = min(delta, cap) if cap else delta
        _items(full_state).append({"item_id": item_id, "number": num})
        return num
    return 0


item_count = _item_count
add_item = _add_item


def spend_carats(full_state: dict, amount: int) -> dict | None:
    wallet = full_state.get("coin_info_state")
    if not isinstance(wallet, dict):
        return None
    free, paid = wallet.get("fcoin", 0) or 0, wallet.get("coin", 0) or 0
    if free + paid < amount:
        return None
    take_free = min(free, amount)
    wallet["fcoin"] = free - take_free
    wallet["coin"] = paid - (amount - take_free)
    return wallet


def spend_paid_carats(full_state: dict, amount: int) -> dict | None:
    wallet = full_state.get("coin_info_state")
    if not isinstance(wallet, dict) or (wallet.get("coin", 0) or 0) < amount:
        return None
    wallet["coin"] = (wallet.get("coin", 0) or 0) - amount
    return wallet


def grant_carats(full_state: dict, amount: int, paid: bool = False) -> dict:
    wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
    field = "coin" if paid else "fcoin"
    wallet[field] = (wallet.get(field) or 0) + amount
    return wallet


def _pay(full_state: dict, row, cost: int) -> bool:
    if (row["pay_item_category"] or 0) == _CARAT_CATEGORY:
        return spend_carats(full_state, cost) is not None
    item_id = row["pay_item_id"]
    if _item_count(full_state, item_id) < cost:
        return False
    _add_item(full_state, item_id, -cost)
    return True


def _grant(full_state: dict, row, count: int, summary: dict) -> None:
    cat = row["change_item_category"] or 0
    cid = row["change_item_id"]
    num = (row["change_item_num"] or 1) * count
    if cat == _PIECE_CATEGORY:
        pieces = full_state.setdefault("piece_list_state", [])
        p = next((p for p in pieces if p.get("piece_id") == cid), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + num
        else:
            pieces.append({"piece_id": cid, "piece_num": num})
        summary["add_piece_list"].append({"piece_id": cid, "piece_num": num})
    elif cat == _SUPPORT_CATEGORY:
        cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
        owned = next((c for c in cards if c.get("support_card_id") == cid), None)
        if owned:
            owned["stock"] = (owned.get("stock") or 0) + num
        else:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            owned = {"viewer_id": "<redacted>", "support_card_id": cid, "exp": 0,
                     "limit_break_count": 0, "favorite_flag": 0, "stock": num - 1,
                     "possess_time": now, "create_time": now}
            cards.append(owned)
        collection.echo_support_card(summary, owned)
        summary["add_support_card_num_array"].append(
            {"support_card_id": cid, "number": num, "item_type": 51})
    elif cat == _CARD_CATEGORY:
        cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
        if not any(c.get("card_id") == cid for c in cards):
            rar = master_data.query_one(
                "SELECT MIN(rarity) AS r FROM card_rarity_data WHERE card_id=?", (cid,))
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            cards.append({"card_id": cid,
                          "rarity": (rar["r"] if rar and rar["r"] else 1),
                          "talent_level": 1, "skill_data_array": []})
            summary["add_card_list"].append(
                {"card_id": cid, "rarity": 3, "talent_level": 1, "create_time": now})
            bonus = _empty_summary()
            bonus["add_cloth_list"] = [{"cloth_id": cid}]
            bonus["add_total_fan"] = 1
            summary["add_card_bonus_info"] = bonus
            chara = master_data.query_one(
                "SELECT chara_id FROM card_data WHERE id=?", (cid,))
            chara_id = chara["chara_id"] if chara else cid // 100
            charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
            entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                     "fan": 1, "max_grade": 0, "dress_id": 2,
                     "mini_dress_id": 2, "love_point_pool": 0}
            if not any(c.get("chara_id") == chara_id for c in charas):
                charas.append(entry)
                summary["add_chara_list"].append({k: v for k, v in entry.items()
                                                  if k != "love_point_pool"})
    else:
        _add_item(full_state, cid, num)
        summary["add_item_list"].append({"item_id": cid, "number": num})
    extra = (row["additional_piece_num"] or 0) * count
    if extra and cat == _CARD_CATEGORY:
        pieces = full_state.setdefault("piece_list_state", [])
        p = next((p for p in pieces if p.get("piece_id") == cid), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + extra
        else:
            pieces.append({"piece_id": cid, "piece_num": extra})
        summary["add_piece_list"].append({"piece_id": cid, "piece_num": extra})


def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


def _buy(full_state: dict, exchange_id: int, count: int, summary: dict) -> bool:
    row = _row(exchange_id)
    if not row or count <= 0:
        return False
    limits = full_state.setdefault(SHOP_LIMIT_KEY, {})
    bought = limits.get(str(exchange_id), 0) or 0
    cap = row["change_item_limit_num"] or 0
    if (row["change_item_limit_type"] or 0) and cap and bought + count > cap:
        return False
    cost = _unit_price(row, bought, count)
    if not _pay(full_state, row, cost):
        return False
    _grant(full_state, row, count, summary)
    limits[str(exchange_id)] = bought + count
    return True


def _is_limited(exchange_id: int) -> bool:
    row = _row(exchange_id)
    return bool(row is not None and (row["change_item_limit_type"] or 0))


def handle_show_exchange(payload: dict) -> dict:
    from . import limited_shop

    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    limits = full_state.get(SHOP_LIMIT_KEY) or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    shop_info = limited_shop.snapshot_info(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "limit_list": [{"item_exchange_id": int(k), "exchange_count": v,
                        "update_time": now} for k, v in limits.items()
                       if _is_limited(int(k))],
        "disabled_id_array": [],
        "release_id_array": [],
        "limited_shop_info": shop_info,
        "limited_goods_info_array": limited_shop.goods_array(full_state),
        "new_aniv_shop_flag": 0,
        "check_aniv_shop_time_latest": now,
    })


def handle_exchange(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    summary = _empty_summary()
    ok = _buy(full_state, payload.get("exchange_id"), int(payload.get("count") or 1), summary)
    if not ok:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    row = _row(payload.get("exchange_id"))
    use_item = None
    if row is not None and (row["pay_item_category"] or 0) != _CARAT_CATEGORY:
        use_item = {"item_id": row["pay_item_id"],
                    "number": _item_count(full_state, row["pay_item_id"])}
    return _ok({"reward_summary_info": summary,
                "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                "use_item_info": use_item})


def _stored_user_info(full_state: dict) -> dict:
    from . import load
    blob = full_state.get(load.STATE_KEY)
    if not isinstance(blob, dict):
        return {}
    return (blob.get("data") or {}).get("user_info") or {}


def bonus_follow_num(full_state: dict) -> int:
    bought = full_state.get(BONUS_FOLLOW_KEY)
    if bought is not None:
        return int(bought)
    return int(_stored_user_info(full_state).get("bonus_follow_num") or 0)


def bonus_limited_sale_open_num(full_state: dict) -> int:
    bought = full_state.get(BONUS_LIMITED_SALE_KEY)
    if bought is not None:
        return int(bought)
    return int(_stored_user_info(full_state).get("bonus_limited_sale_open_num") or 0)


def handle_exchange_add_frame(payload: dict) -> dict:
    from . import limited_shop, load

    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    exchange_id = payload.get("exchange_id")
    count = int(payload.get("count") or 1)
    row = _row(exchange_id)
    if row is None or count <= 0:
        return _refuse()
    item_id = row["change_item_id"]
    if item_id == _FOLLOW_FRAME_ITEM:
        key, current = BONUS_FOLLOW_KEY, bonus_follow_num(full_state)
    elif item_id == _LIMITED_SALE_FRAME_ITEM:
        key, current = BONUS_LIMITED_SALE_KEY, bonus_limited_sale_open_num(full_state)
    else:
        return _refuse()
    limits = full_state.setdefault(SHOP_LIMIT_KEY, {})
    bought = limits.get(str(exchange_id), 0) or 0
    cap = row["change_item_limit_num"] or 0
    if (row["change_item_limit_type"] or 0) and cap and bought + count > cap:
        return _refuse()
    if not _pay(full_state, row, _unit_price(row, bought, count)):
        return _refuse()
    limits[str(exchange_id)] = bought + count
    full_state[key] = current + (row["change_item_num"] or 1) * count

    user_info = load.apply_derived_user_info(
        full_state, copy.deepcopy(
            load.get_or_seed_data(full_state, viewer_id).get("user_info") or {}))
    if (row["pay_item_category"] or 0) == _CARAT_CATEGORY:
        wallet = full_state.get("coin_info_state") or {}
        use_item = {"item_id": row["pay_item_id"],
                    "number": (wallet.get("fcoin") or 0) + (wallet.get("coin") or 0)}
    else:
        use_item = {"item_id": row["pay_item_id"],
                    "number": _item_count(full_state, row["pay_item_id"])}
    shop_info = limited_shop.snapshot_info(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({"updated_user_info": user_info,
                "use_item_info": use_item,
                "limited_shop_info": shop_info})


def handle_exchange_multi(payload: dict) -> dict:
    from . import limited_shop

    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    summary = _empty_summary()
    reqs = payload.get("exchange_item_info_array") or []
    bought_any = False
    paid_items: list[int] = []
    for r in reqs:
        exchange_id = r.get("exchange_id") or r.get("item_exchange_id")
        count = int(r.get("count") or 1)
        open_count = (r.get("ex_param") or {}).get("open_count")
        if open_count:
            ok = limited_shop.buy(full_state, exchange_id, int(open_count), count, summary)
        else:
            ok = _buy(full_state, exchange_id, count, summary)
        if ok:
            bought_any = True
            row = _row(exchange_id)
            if (row["pay_item_category"] or 0) != _CARAT_CATEGORY and \
                    row["pay_item_id"] not in paid_items:
                paid_items.append(row["pay_item_id"])
    if not bought_any:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary,
                "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                "use_item_info_array": [
                    {"item_id": i, "number": _item_count(full_state, i)} for i in paid_items]})


def handle_check_aniv_shop(payload: dict) -> dict:
    return _ok({})


@registry.endpoint("item/sell")
def handle_item_sell(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    item_id = payload.get("item_id")
    count = payload.get("count")
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        return _refuse()
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return _refuse()

    row = master_data.query_one(
        "SELECT sell_item_id, sell_price FROM item_data WHERE id=?", (item_id,))
    if row is None or not row["sell_price"]:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if _item_count(full_state, item_id) < count:
        return _refuse()

    remaining = _add_item(full_state, item_id, -count)
    pay_item = row["sell_item_id"]
    proceeds = row["sell_price"] * count
    summary = _empty_summary()
    before = _item_count(full_state, pay_item)
    credited = _add_item(full_state, pay_item, proceeds) - before
    summary["add_item_list"].append({"item_id": pay_item, "number": credited})

    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary,
                "use_item_info": [{"item_id": item_id, "number": remaining}]})
