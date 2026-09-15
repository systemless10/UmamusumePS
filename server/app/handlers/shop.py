"""Shop / item exchanges -- the spend side of the economy.

Endpoints (the only shop-family ones that exist in any capture):
  item/show_exchange  -> per-viewer shop STATE (purchase counts per row, limited
                         shop info). NOT a catalog: the client builds the
                         listings itself from master.mdb item_exchange.
  item/exchange       -> buy ONE row: {exchange_id, count, ...}
  item/exchange_multi -> buy several rows in one go
  item/check_aniv_shop-> mark the anniversary shop seen

Catalog + prices are read live from master.mdb item_exchange (699 rows):
pay `pay_item_num` of (pay_item_category, pay_item_id) for `change_item_num`
of (change_item_category, change_item_id). Categories that matter:
  50  = character card      51  = support card      102 = card pieces
  90  = carats (coin_info)  everything else = a plain item in item_list.
Purchase limits: change_item_limit_type 1 = lifetime, 4 = monthly, 0 = none.

Payment is validated against OUR state, never the client's claimed balance,
and the response carries server truth so the client resyncs.
"""

from __future__ import annotations

import copy
import functools
import time

from .. import master_data
from .. import state as state_store
from . import collection, registry

SHOP_LIMIT_KEY = "shop_limit_state"   # {str(exchange_id): bought_count}
BONUS_FOLLOW_KEY = "bonus_follow_num_state"  # int: extra follow slots bought
_CARAT_CATEGORY = 90
_CARD_CATEGORY = 50
_SUPPORT_CATEGORY = 51
_PIECE_CATEGORY = 102
_FOLLOW_FRAME_CATEGORY = 98           # "Follow Slot Boost" -- not an inventory item

# coin_info_state: {fcoin: FREE carats, coin: PAID carats} -- CoinInfo's own
# two fields, shown as genuinely SEPARATE numbers on the real client (user
# screenshot, 2026-08-21: "PAID 0 / FREE 2,000,007,059" as two distinct
# labels with their own icons, not one combined total). An earlier pass here
# wrongly assumed "coin" was "Monies" (item 59/category 91, a same-named-
# adjacent but actually unrelated item-list currency) purely from two
# similarly-priced things sharing a text search, invented a THIRD
# server-only "paid_carat_reserve" key, and folded everything into fcoin --
# which is what actually produced the "it's counted together" bug the
# screenshot caught. There is no third bucket: paid carats are simply
# coin_info_state["coin"], sent to the client like any other wallet field.


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _row(exchange_id: int):
    return master_data.query_one(
        "SELECT * FROM item_exchange WHERE id=?", (exchange_id,))


def _unit_price(row, already_bought: int, count: int) -> int:
    """Total cost for `count` units. Rows with a price_change_group escalate
    by how many you've already bought (tiered pricing)."""
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
    """item_data.limit_num for this item, or None if the row has no real cap.
    Cached: master.mdb is read-only for the process lifetime."""
    row = master_data.query_one("SELECT limit_num FROM item_data WHERE id=?", (item_id,))
    return row["limit_num"] if row and row["limit_num"] else None


def _add_item(full_state: dict, item_id: int, delta: int) -> int:
    """+delta to item_id's owned count, clamped to [0, its real master.mdb
    limit_num] when that item has one.

    BUG FIXED 2026-08-24 (live-reported: a mailed Make Debut Scout ticket
    showed as 0 in-game no matter how many were actually held). Every one of
    these tickets has a real limit_num of 9999 -- a mailed quantity of 99999
    (10x the real cap) left the account holding a count the client has no
    way to represent (its own UI is built assuming nothing can ever exceed
    the declared max), and it silently rendered as 0 rather than the true,
    invalid number. Real gameplay grants are naturally far under any item's
    cap and were never at risk -- this only ever bites an admin mail/cheat
    quantity chosen without checking the real ceiling, which is exactly what
    happened here."""
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


# Public aliases. Other feature modules need the same clamped inventory
# arithmetic (stamina.py burns TP/RP recovery items through it); reaching
# across modules for the underscored names would be worse than naming them.
item_count = _item_count
add_item = _add_item


def spend_carats(full_state: dict, amount: int) -> dict | None:
    """Deduct `amount` carats, free (fcoin) first, then paid (coin) --
    matching the real client's own separately-displayed PAID/FREE numbers.
    Returns the new coin_info wallet dict, or None if the player can't
    afford it. The single shared implementation for gacha.py, _pay below,
    and daily_races.py's recovery-ticket purchase."""
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


def grant_carats(full_state: dict, amount: int, paid: bool = False) -> dict:
    """Add `amount` carats -- to the PAID bucket (coin) if paid=True, else
    the FREE bucket (fcoin). These are genuinely separate wire fields the
    client displays on their own, not one combined total."""
    wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
    field = "coin" if paid else "fcoin"
    wallet[field] = (wallet.get(field) or 0) + amount
    return wallet


def _pay(full_state: dict, row, cost: int) -> bool:
    """Deduct the price. Carats spend free-first (the client shows the sum);
    every other currency is just an item count. Never trusts the request."""
    if (row["pay_item_category"] or 0) == _CARAT_CATEGORY:
        return spend_carats(full_state, cost) is not None
    item_id = row["pay_item_id"]
    if _item_count(full_state, item_id) < cost:
        return False
    _add_item(full_state, item_id, -cost)
    return True


def _grant(full_state: dict, row, count: int, summary: dict) -> None:
    """Give what was bought, and record it in reward_summary_info."""
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
            cards.append({"viewer_id": "<redacted>", "support_card_id": cid, "exp": 0,
                          "limit_break_count": 0, "favorite_flag": 0, "stock": num - 1})
            summary["add_support_card_list"].append(
                {"support_card_id": cid, "exp": 0, "limit_break_count": 0,
                 "favorite_flag": 0, "stock": 0})
        summary["add_support_card_num_array"].append(
            {"support_card_id": cid, "number": num, "item_type": 51})
    elif cat == _CARD_CATEGORY:
        cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
        if not any(c.get("card_id") == cid for c in cards):
            rar = master_data.query_one(
                "SELECT MIN(rarity) AS r FROM card_rarity_data WHERE card_id=?", (cid,))
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            # BUG FIXED 2026-09-10: this read the rarity as (rar or {}).get("r"),
            # but query_one returns a sqlite3.Row, which supports subscripting
            # and NOT .get -- so granting a trainee card the account did not
            # already own raised AttributeError and 500'd the whole call. Only
            # this branch was affected, and only on the not-already-owned path,
            # which is why it survived: a card exchange for a dupe never
            # reaches it. Found by card/unlock, which grants through here.
            cards.append({"card_id": cid,
                          "rarity": (rar["r"] if rar and rar["r"] else 1),
                          "talent_level": 1, "skill_data_array": []})
            # create_time + add_card_bonus_info(cloth) + add_chara_list are how
            # the REAL response tells the client to apply this live; without
            # them the purchase only appears after a full client restart.
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
    """True when master.mdb caps how often this row may be bought
    (change_item_limit_type 1 = lifetime, 4 = monthly; 0 = no limit). An
    exchange_id with no master row at all is treated as unlimited -- it cannot
    be bought, so advertising a cap for it would be meaningless."""
    row = _row(exchange_id)
    return bool(row is not None and (row["change_item_limit_type"] or 0))


def handle_show_exchange(payload: dict) -> dict:
    # Deferred import: limited_shop.py imports THIS module (for
    # SHOP_LIMIT_KEY), so importing it back at shop.py's top level would be
    # circular. Safe here since it's only ever needed once main.py has
    # finished loading every handler module.
    from . import limited_shop

    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    limits = full_state.get(SHOP_LIMIT_KEY) or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    shop_info = limited_shop.snapshot_info(full_state)
    state_store.save_state(viewer_id, full_state)
    # limit_list carries ONLY rows that actually have a purchase limit.
    #
    # BUG FIXED 2026-09-11 (live-reported: "when exchanging a voucher the GUI
    # for the exchange is completely broken, but it did go through" -- the
    # Exchange Complete dialog drew a "Limit  <n> -> 1" line, in untranslated
    # JP, for item_exchange 2000261, whose master row is
    # change_item_limit_type 0 / change_item_limit_num 0, i.e. UNLIMITED).
    # This used to emit an entry for every exchange_id the account had ever
    # bought, so an unlimited row got a limit entry, and the dialog rendered a
    # limit counter against a cap of zero.
    #
    # Capture-proven: the real server's 194-entry limit_list
    # (20260821_165708 tx 16) is 174 rows of change_item_limit_type 1
    # (lifetime) + 20 of type 4 (monthly) and NOT ONE of type 0. Rows with a
    # limit stay in the list even at exchange_count 0 -- that is how the
    # client learns the row is capped at all -- so the filter is on the
    # master's limit_type, never on the count.
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
    # Response shape copied EXACTLY from the real capture: reward_summary_info
    # + coin_info + use_item_info (the spent currency). An earlier `item_list`
    # key was invented and the client ignored it, so purchases only showed
    # after a restart.
    row = _row(payload.get("exchange_id"))
    use_item = None
    if row is not None and (row["pay_item_category"] or 0) != _CARAT_CATEGORY:
        use_item = {"item_id": row["pay_item_id"],
                    "number": _item_count(full_state, row["pay_item_id"])}
    return _ok({"reward_summary_info": summary,
                "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                "use_item_info": use_item})


def handle_exchange_add_frame(payload: dict) -> dict:
    """item/exchangeAddFrame -- the Friend-Points shop row "Follow Slot Boost"
    (item_exchange id 27: 1000x Friend Points -> +1 max follow, lifetime cap
    40). Its reward is NOT an inventory item: it raises the account's
    bonus_follow_num (load/index user_info). Previously unhandled -- the no-op
    fallback made it a phantom purchase (client kicked to title, points
    resynced back, slots never granted). Live request shape (raw-logged):
    {exchange_id, count, current_num, get_list_time} -- same as item/exchange.
    Response mirrors the item/exchange family (no real capture; raw-logged for
    the day one appears) plus the new bonus_follow_num."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    exchange_id = payload.get("exchange_id")
    count = int(payload.get("count") or 1)
    row = _row(exchange_id)
    if row is None or count <= 0:
        return _refuse()
    limits = full_state.setdefault(SHOP_LIMIT_KEY, {})
    bought = limits.get(str(exchange_id), 0) or 0
    cap = row["change_item_limit_num"] or 0
    if (row["change_item_limit_type"] or 0) and cap and bought + count > cap:
        return _refuse()
    if not _pay(full_state, row, _unit_price(row, bought, count)):
        return _refuse()
    limits[str(exchange_id)] = bought + count
    total = (full_state.get(BONUS_FOLLOW_KEY) or 0) + (row["change_item_num"] or 1) * count
    full_state[BONUS_FOLLOW_KEY] = total
    state_store.save_state(viewer_id, full_state)
    use_item = None
    if (row["pay_item_category"] or 0) != _CARAT_CATEGORY:
        use_item = {"item_id": row["pay_item_id"],
                    "number": _item_count(full_state, row["pay_item_id"])}
    return _ok({"reward_summary_info": _empty_summary(),
                "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                "use_item_info": use_item,
                "bonus_follow_num": total})


def handle_exchange_multi(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    summary = _empty_summary()
    reqs = payload.get("exchange_item_info_array") or []
    bought_any = False
    for r in reqs:
        if _buy(full_state, r.get("exchange_id") or r.get("item_exchange_id"),
                int(r.get("count") or 1), summary):
            bought_any = True
    if not bought_any:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary,
                "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                "use_item_info_array": [
                    {"item_id": r.get("exchange_id") or r.get("item_exchange_id"),
                     "number": int(r.get("count") or 1)} for r in reqs]})


def handle_check_aniv_shop(payload: dict) -> dict:
    return _ok({})


# --------------------------------------------------------------------------
# item/sell -- convert spare inventory into Monies.
#
# dump.cs: ItemSellRequest {item_id, count, client_own_num}
#          ItemSellResponse.CommonResponse {reward_summary_info, use_item_info}
#
# Fully master-driven, no invented numbers: item_data carries sell_item_id and
# sell_price per item. On this build 43 of the 191 items are sellable and every
# one of them pays item 59 (Monies) at 10-20 each; sell_price 0 means the item
# simply cannot be sold, which is what the refusal below is. Reading the rate
# out of master rather than hardcoding "money" keeps a future item that pays
# something else working with no edit here.
#
# `client_own_num` is the client's claimed balance -- accepted and NOT trusted,
# the same convention every other current_*/claimed field in this project gets
# (see cards.py's module docstring). Our own item_list_state decides.


@registry.endpoint("item/sell")
def handle_item_sell(payload: dict) -> dict:
    """item/sell: {item_id, count, client_own_num} -> {reward_summary_info,
    use_item_info}. use_item_info is the UserItem row for what was SOLD, at
    its new balance -- the client applies it to replace the stack it just
    spent from; what was RECEIVED rides in reward_summary_info like every
    other payout in this module."""
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
        return _refuse()        # unknown, or not a sellable item

    full_state = state_store.get_state(viewer_id) or {}
    if _item_count(full_state, item_id) < count:
        return _refuse()

    remaining = _add_item(full_state, item_id, -count)
    pay_item = row["sell_item_id"]
    proceeds = row["sell_price"] * count
    summary = _empty_summary()
    # Report what was actually credited, not what was owed: _add_item clamps
    # to the item's real limit_num, so at the ceiling those two differ and the
    # client would show a gain that never landed.
    before = _item_count(full_state, pay_item)
    credited = _add_item(full_state, pay_item, proceeds) - before
    summary["add_item_list"].append({"item_id": pay_item, "number": credited})

    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary,
                "use_item_info": [{"item_id": item_id, "number": remaining}]})
