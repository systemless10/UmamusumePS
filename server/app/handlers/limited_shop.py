
from __future__ import annotations

import functools
import json
import random

from .. import master_data
from .. import patch
from . import registry, shop

STATE_KEY = "limited_shop_state"


@functools.lru_cache(maxsize=1)
def _all_rows() -> tuple:
    if not master_data.has_table("limited_exchange"):
        return ()
    return tuple(master_data.query("SELECT * FROM limited_exchange"))


def _active_row(now_ts: int):
    for row in _all_rows():
        if row["start_date"] <= now_ts <= row["end_date"]:
            return row
    return None


def _slot_group_id(odds_id: int, slot: int) -> int:
    return (10 + (odds_id - 1000)) * 10 + slot


@functools.lru_cache(maxsize=64)
def _slot_rewards(group_id: int) -> tuple:
    return tuple(master_data.query(
        "SELECT * FROM limited_exchange_reward WHERE group_id=?", (group_id,)))


def _reward_row(reward_id: int):
    return master_data.query_one(
        "SELECT * FROM limited_exchange_reward WHERE id=?", (reward_id,))


_GLOBAL_DISP_ORDER_7 = {5: 1, 6: 2, 1: 3, 2: 4, 3: 5, 4: 6, 7: 7}

_NO_CEILING = 1 << 30


@functools.lru_cache(maxsize=1)
def _jp_pool() -> dict | None:
    from .. import region
    path = region.seeds_dir() / "limited_shop_pool.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _campaign(now_ts: int) -> dict | None:
    row = _active_row(now_ts)
    if row is not None:
        lineup = row["item_lineup_value"] or 0
        slots = []
        for slot in range(1, lineup + 1):
            rewards = [(r["id"], r["item_exchange_id"], r["ribbon_value"] or 0, r["odds"])
                       for r in _slot_rewards(_slot_group_id(row["odds_id"], slot))]
            disp = _GLOBAL_DISP_ORDER_7.get(slot, slot) if lineup == 7 else slot
            slots.append((disp, rewards))
        return {"id": row["id"], "end_date": row["end_date"],
                "open_value": row["open_value"] or 0,
                "odds": {ctx: (row[o] or 0, row[c] or 0)
                         for ctx, (o, c) in _CONTEXT_ODDS_COLUMN.items()},
                "slots": slots}
    if master_data.has_table("limited_exchange"):
        return None
    pool = _jp_pool()
    if not pool:
        return None
    return {"id": pool["limited_exchange_id"], "end_date": pool["close_time"],
            "open_value": 0,
            "odds": {ctx: (ppm, _NO_CEILING) for ctx, ppm in pool["open_odds_ppm"].items()},
            "slots": [(s["disp_order"],
                       [(r["reward_id"], r["item_exchange_id"], r["ribbon_value"], r["weight"])
                        for r in s["rewards"]])
                      for s in pool["slots"]]}


def _roll_slot(rewards: list):
    total = sum(w for *_x, w in rewards)
    if total <= 0:
        return None
    threshold = random.randint(1, total)
    acc = 0
    for entry in rewards:
        acc += entry[3]
        if threshold <= acc:
            return entry
    return rewards[-1]


def _reset_open_time() -> int:
    return 32400 if master_data.has_table("limited_exchange") else 0


def _state(full_state: dict, active_id: int) -> dict:
    from .daily_races import _served_day
    st = full_state.setdefault(STATE_KEY, {})
    day = _served_day()
    if st.get("limited_exchange_id") != active_id or st.get("day") != day:
        st.clear()
        st["limited_exchange_id"] = active_id
        st["day"] = day
        st["open_count"] = 0
        st["last_open_time"] = _reset_open_time()
        st["goods"] = []
    return st


_BASE_MAX_OPEN = 3


def max_open_count(full_state: dict, camp: dict | None = None) -> int:
    base = ((camp or {}).get("open_value") or 0) or _BASE_MAX_OPEN
    return base + shop.bonus_limited_sale_open_num(full_state)


_MAX_INT32 = 2_000_000_000


def _info(camp: dict, st: dict, appear_flag: int, full_state: dict) -> dict:
    return {
        "limited_exchange_id": camp["id"],
        "open_flag": 1 if st["open_count"] > 0 else 0,
        "appear_flag": appear_flag,
        "close_time": min(camp["end_date"], _MAX_INT32),
        "max_open_count": max_open_count(full_state, camp),
        "open_count": st["open_count"],
        "limited_sale_all_change_flag": 0,
        "last_open_time": st["last_open_time"],
    }


_CONTEXT_ODDS_COLUMN = {
    "daily_race": ("daily_race_odds", "daily_race_ceiling"),
    "legend_race": ("legend_race_odds", "legend_race_ceiling"),
    "team_stadium": ("team_stadium_odds", "team_stadium_ceiling"),
    "single_mode": ("single_mode_odds", "single_mode_ceiling"),
}


def roll(full_state: dict, context: str) -> dict | None:
    now_ts = patch._servertime()
    camp = _campaign(now_ts)
    if camp is None:
        return None
    st = _state(full_state, camp["id"])

    odds, ceiling = camp["odds"].get(context, (0, 0))
    fresh_open = False
    if (odds > 0 and ceiling > 0 and st["open_count"] < ceiling
            and st["open_count"] < max_open_count(full_state, camp)):
        if random.randint(1, 1_000_000) <= odds:
            st["open_count"] += 1
            st["last_open_time"] = now_ts
            for disp, rewards in camp["slots"]:
                reward = _roll_slot(rewards)
                if reward is not None:
                    st["goods"].append({
                        "disp_order": disp,
                        "open_count": st["open_count"],
                        "reward_id": reward[0],
                        "item_exchange_id": reward[1],
                        "ribbon_value": reward[2],
                        "exchange_count": 0,
                    })
            fresh_open = True

    return _info(camp, st, 1 if fresh_open else 0, full_state)


def snapshot_info(full_state: dict) -> dict | None:
    camp = _campaign(patch._servertime())
    if camp is None:
        return None
    st = _state(full_state, camp["id"])
    return _info(camp, st, 0, full_state)


def _goods_exchange_id(g: dict):
    if g.get("item_exchange_id"):
        return g["item_exchange_id"]
    reward = _reward_row(g["reward_id"]) if master_data.has_table("limited_exchange_reward") else None
    return reward["item_exchange_id"] if reward else 0


def goods_array(full_state: dict) -> list:
    camp = _campaign(patch._servertime())
    if camp is None:
        return []
    st = _state(full_state, camp["id"])
    return [{
        "disp_order": g["disp_order"],
        "open_count": g["open_count"],
        "reward_id": g["reward_id"],
        "exchange_count": g.get("exchange_count") or 0,
        "item_exchange_id": _goods_exchange_id(g),
        "ribbon_value": g.get("ribbon_value") or 0,
    } for g in st["goods"]]


def buy(full_state: dict, exchange_id: int, open_count: int, count: int,
        summary: dict) -> bool:
    camp = _campaign(patch._servertime())
    if camp is None or count <= 0:
        return False
    st = _state(full_state, camp["id"])
    good = next((g for g in st["goods"]
                 if g["open_count"] == open_count and _goods_exchange_id(g) == exchange_id), None)
    row = shop._row(exchange_id)
    if good is None or row is None:
        return False
    cap = row["change_item_limit_num"] or 1
    bought = good.get("exchange_count") or 0
    if bought + count > cap:
        return False
    if not shop._pay(full_state, row, shop._unit_price(row, 0, count)):
        return False
    shop._grant(full_state, row, count, summary)
    good["exchange_count"] = bought + count
    return True


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@registry.endpoint("item/manual_close")
def handle_manual_close(payload: dict) -> dict:
    return _ok({})
