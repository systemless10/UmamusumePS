"""Daily/Legend-race "limited shop": running a race has a chance to unlock
a limited-time shop stocked with randomly-rolled purchasable items.

Ground truth (verified live against master.mdb and dump.cs, plus two real
captures -- NOT guessed):

  limited_exchange: exactly one row is ever "active" (start_date <=
    servertime <= end_date, both raw unix epoch ints -- same idea as
    story_event.py's _active_event_id, NOT missions.py's string-date
    _parse_mdb_datetime). The live row (id=5, odds_id=1004,
    item_lineup_value=7) carries PER-CONTEXT odds/ceiling as parts-per-
    million: daily_race_odds=350000/ceiling=3, legend_race_odds=400000/
    ceiling=2, team_stadium_odds=300000/ceiling=4, single_mode_odds=0
    (disabled). "35% per daily race run, capped at 3 opens" is the real
    behavior behind the user's "50%-ish, can trigger more than once".

  limited_exchange_reward: the random item pool, split into
    item_lineup_value slots. VERIFIED formula (checked against all 5
    historical limited_exchange rows, zero exceptions):
        group_id = (10 + (odds_id - 1000)) * 10 + slot   for slot in 1..item_lineup_value
    e.g. odds_id=1004 (7 slots) -> groups 141-147; odds_id=1000 (6 slots)
    -> groups 101-106. Each slot's rows are weighted by `odds` (parts-per-
    million WITHIN that slot; verified summing to ~1,000,000 per slot).
    `item_exchange_id` on each reward row is an ordinary item_exchange row
    (top_id 9000) -- the existing generic shop.handle_exchange/_buy already
    buys these correctly; nothing new needed on the purchase side.

  Real capture captures/20260819_173442/0033_daily_race_skip_race_skip.json:
    a race_skip_count=3 call rolled 2 opens in ONE response
    (limited_shop_info: open_count=2, open_flag=1, appear_flag=1) -- exactly
    "running several at once can trigger it more than once". The following
    0034_item_show_exchange.json shows 14 limited_goods_info_array entries:
    TWO full 7-slot sets, tagged open_count=1 and open_count=2 respectively
    -- each trigger rolls and ACCUMULATES its own fresh offer set, it
    doesn't replace the previous one.

  appear_flag is 1 ONLY in the exact response of the call that produced a
  new open; every other read (a later item/show_exchange, load/index) shows
  0 while open_flag/open_count/last_open_time stay persisted -- a one-shot
  "just now" flag, not shop-open state. snapshot_info() below always
  returns 0; roll() returns 1 only when it just produced a fresh open.

  dump.cs confirms LimitedShopInfo {limited_exchange_id, open_flag,
  appear_flag, close_time, open_count, limited_sale_all_change_flag,
  last_open_time} and LimitedGoodsInfo {disp_order, open_count, reward_id
  (-> limited_exchange_reward.id), exchange_count} exactly (already matched
  by daily_races.py's now-removed hardcoded stub). item/manual_close is a
  real endpoint with an EMPTY request body -- no capture of it exists
  anywhere (checked debug_responses/ and captures/ exhaustively), so its
  effect below is an inference, flagged at the function.

Scope (user-confirmed): wired up for daily races and legend races only
(single-run via replay_check, multi-run via race_skip) -- "running in
races". team_stadium/single_mode share the same master row/odds columns so
roll() is written generically by `context`, but those two call sites are
not wired up this pass.

Known design gap (no real evidence either way): the wire format has ONE
global open_count/ceiling shared across every context, but master data
gives daily/legend/team-stadium their OWN ceiling. No capture exercises two
different contexts against the same campaign to show how that should
interact. Implemented as: roll() only ever compares the shared open_count
against *that call's own* context ceiling -- so once any context's ceiling
is reached, a context with an equal-or-lower ceiling simply never fires
again (the highest-ceiling context in practice becomes the effective cap).
"""

from __future__ import annotations

import functools
import random
import time

from .. import master_data
from . import registry, shop

STATE_KEY = "limited_shop_state"


@functools.lru_cache(maxsize=1)
def _all_rows() -> tuple:
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


def _roll_slot(group_id: int):
    rows = _slot_rewards(group_id)
    if not rows:
        return None
    total = sum(r["odds"] for r in rows)
    if total <= 0:
        return None
    threshold = random.randint(1, total)
    acc = 0
    for row in rows:
        acc += row["odds"]
        if threshold <= acc:
            return row
    return rows[-1]


def _reward_row(reward_id: int):
    return master_data.query_one(
        "SELECT * FROM limited_exchange_reward WHERE id=?", (reward_id,))


def _state(full_state: dict, active_id: int) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    if st.get("limited_exchange_id") != active_id:
        # New campaign period (or first-ever access) -- reset accumulated
        # opens/goods. Buying history (shop.SHOP_LIMIT_KEY) is untouched;
        # item_exchange rows track their own lifetime limits independently.
        st.clear()
        st["limited_exchange_id"] = active_id
        st["open_count"] = 0
        st["last_open_time"] = 0
        st["goods"] = []
    return st


# LimitedShopInfo.close_time is a real (signed) C# int32 client-side --
# master.mdb's end_date for the live "never really expires" campaign row is
# 2556143999, which OVERFLOWS int32 (max 2147483647). Sending it verbatim
# broke the client's msgpack deserialization entirely ("could not receive
# parameters from server" right after replay_check -- confirmed the actual
# cause of that report). Clamp to the same safe "far future" sentinel the
# original hardcoded stub used before this feature replaced it.
_MAX_INT32 = 2_000_000_000


def _info(row, st: dict, appear_flag: int) -> dict:
    return {
        "limited_exchange_id": row["id"],
        "open_flag": 1 if st["open_count"] > 0 else 0,
        "appear_flag": appear_flag,
        "close_time": min(row["end_date"], _MAX_INT32),
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
    """Call once per race run. Returns the current limited_shop_info dict
    (appear_flag=1 iff THIS call produced at least one fresh open), or None
    if there's no active campaign right now."""
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return None
    st = _state(full_state, row["id"])

    odds_col, ceiling_col = _CONTEXT_ODDS_COLUMN[context]
    odds, ceiling = row[odds_col] or 0, row[ceiling_col] or 0
    fresh_open = False
    if odds > 0 and ceiling > 0 and st["open_count"] < ceiling:
        if random.randint(1, 1_000_000) <= odds:
            st["open_count"] += 1
            st["last_open_time"] = now_ts
            for slot in range(1, (row["item_lineup_value"] or 0) + 1):
                reward = _roll_slot(_slot_group_id(row["odds_id"], slot))
                if reward is not None:
                    st["goods"].append({
                        "disp_order": slot,
                        "open_count": st["open_count"],
                        "reward_id": reward["id"],
                    })
            fresh_open = True

    return _info(row, st, 1 if fresh_open else 0)


def snapshot_info(full_state: dict) -> dict | None:
    """Read-only echo (item/show_exchange) -- appear_flag always 0."""
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return None
    st = _state(full_state, row["id"])
    return _info(row, st, 0)


def goods_array(full_state: dict) -> list:
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return []
    st = _state(full_state, row["id"])
    limits = full_state.get(shop.SHOP_LIMIT_KEY) or {}
    out = []
    for g in st["goods"]:
        reward = _reward_row(g["reward_id"])
        bought = limits.get(str(reward["item_exchange_id"]), 0) if reward else 0
        out.append({
            "disp_order": g["disp_order"],
            "open_count": g["open_count"],
            "reward_id": g["reward_id"],
            "exchange_count": bought,
        })
    return out


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@registry.endpoint("item/manual_close")
def handle_manual_close(payload: dict) -> dict:
    """No capture exists for this endpoint anywhere (request body is empty
    per dump.cs), so its real server-side effect is unknown. Deliberately a
    no-op beyond acknowledging: resetting open_count to let the shop
    re-trigger would let a player bypass the real per-campaign ceiling by
    repeatedly closing and re-rolling, which is a worse guess to get wrong
    than "the close button doesn't do anything server-side" (plausible if
    it's purely a client-side UI dismissal). Revisit if a real capture ever
    surfaces."""
    return _ok({})
