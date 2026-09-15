"""TP (Training Points) and RP (Race Points) -- the two regenerating pools.

Both were previously frozen: tp_info/rp_info were seeded once from the
load/index snapshot (collection.DYNAMIC_CONTAINERS) and then never moved,
so a career start cost nothing and Team Trials was unlimited.

THE WIRE MODEL (measured, not guessed -- see the numbers below):

  {current_tp, max_tp, max_recovery_time}

`max_recovery_time` is the epoch second at which the pool will be FULL, and
is 0 when it already is. It does NOT move while the pool refills on its own
-- it is the fixed finish line -- so the sub-tick progress toward the next
point is carried in that one field and nothing else has to be stored.
Reading the pool is therefore pure arithmetic against the clock:

    missing = ceil((max_recovery_time - now) / interval)
    current = max - missing

CONFIRMED INTERVALS. 44 distinct load/index tp_info/rp_info values across the
capture corpus, each divided by (max - current) against its own capture
timestamp, land on 600s per TP (median 599.3, spread entirely explained by
capture-directory timestamps being a few minutes off the actual request) and
<=7200s per RP. Sharper still, item/use_recovery_item pins it exactly:
captures/20260904_232507 goes 26 TP -> 57 TP on one +30 carrot, and its
max_recovery_time drops by 18000s = 30 x 600 to the second. So: 10 minutes
per TP (max 100), 2 hours per RP (max 5), and a grant/spend of N simply
shifts max_recovery_time by -/+ N*interval.

CAREER COST. single_mode*/start carries the client's own `use_tp`; the server
recomputes it rather than trusting it (career_tp_cost below). Base is 30, and
an active campaign_data row (target_type=1 SingleMode, effect_type_1=4
SingleUseTP) REPLACES that base with an absolute value -- campaign 225 in this
master.mdb runs 1784757600..1787867999 with effect_value_1=15, and every
captured start inside that window sent use_tp=15 while every one outside it
sent 30. The 2x boosts (`boost_story_event_id`, `boost_factor_research_event_id`,
the training-challenge mode) are consume_tp_ratio columns on their own master
tables, all 20000 on a basis of 10000 -- which is where the 60 TP ceiling
comes from.
"""

from __future__ import annotations

import copy
import functools
import logging
import math
import time

log = logging.getLogger("uma-server")

from .. import config
from .. import master_data
from .. import state as state_store
from . import registry

TP_STATE_KEY = "tp_info_state"
RP_STATE_KEY = "rp_info_state"

# Defaults for an account whose snapshot never carried the container.
_DEFAULT_MAX_TP = 100
_DEFAULT_MAX_RP = 5

# item_data.item_category for the two recovery families (master.mdb):
#   20 -> TP items, effect_value_1 = 30 each ("Toughness 30", "Star Fruit",
#         the 68 Valentine chocolates, ...)
#   21 -> RP items, item 34 "Carrot Jelly Mini" (+1), item 35 "Carrot Jelly" (+5)
_TP_ITEM_CATEGORY = 20
_RP_ITEM_CATEGORY = 21

# consume_tp_ratio's basis (20000 = 200% = the 2x boost).
_RATIO_BASE = 10000


# --------------------------------------------------------------- knobs --
def tp_interval() -> int:
    return config.get_int("tp_recovery_seconds", 600)


def rp_interval() -> int:
    return config.get_int("rp_recovery_seconds", 7200)


def _base_career_tp() -> int:
    return config.get_int("career_tp_cost", 30)


def team_trials_rp_cost() -> int:
    return config.get_int("team_trials_rp_cost", 1)


# ------------------------------------------------------- the pool math --
class _Pool:
    """One regenerating pool, described by the three wire field names it
    uses. TP and RP differ ONLY in those names and their interval."""

    def __init__(self, state_key, current_field, max_field, default_max, interval):
        self.state_key = state_key
        self.current = current_field
        self.max = max_field
        self.default_max = default_max
        self.interval = interval

    def read(self, full_state: dict, now: int | None = None) -> dict:
        """The pool, advanced to `now`. Mutates (and returns) the stored dict
        so a caller that saves full_state persists the tick."""
        now = int(now if now is not None else time.time())
        pool = full_state.get(self.state_key)
        if not isinstance(pool, dict):
            pool = {self.current: self.default_max, self.max: self.default_max,
                    "max_recovery_time": 0}
            full_state[self.state_key] = pool
        pool.setdefault(self.max, self.default_max)
        pool.setdefault(self.current, pool[self.max])
        pool.setdefault("max_recovery_time", 0)

        cap = int(pool[self.max] or self.default_max)
        cur = int(pool[self.current] or 0)
        mrt = int(pool["max_recovery_time"] or 0)
        if cur >= cap:
            # At or over the cap nothing accrues and the finish line is
            # cleared -- exactly what the real server sends (every full
            # tp_info in the corpus carries max_recovery_time 0). Over the
            # cap is a real state, not an error: a carrot is allowed to
            # overfill, and the surplus then simply sits there.
            pool["max_recovery_time"] = 0
            return pool
        if mrt <= 0:
            # BELOW the cap with no finish line is a state the real server
            # never sends, but our own does: load_index_fresh.json's snapshot
            # carries {current_tp: 0, max_tp: 100, max_recovery_time: 0}, and
            # so do two other captured accounts. Read literally that is a pool
            # that can never refill -- a fresh account permanently stuck at 0
            # TP. Heal it by starting the clock now instead.
            pool["max_recovery_time"] = now + (cap - cur) * self.interval()
            return pool
        remaining = mrt - now
        if remaining <= 0:
            pool[self.current] = cap
            pool["max_recovery_time"] = 0
            return pool
        interval = self.interval()
        # A finish line further out than a full pool takes to refill cannot be
        # right under the CURRENT interval -- it was written under a different
        # one (tp_recovery_seconds is a live config knob, re-read on mtime, so
        # this happens the moment it is retuned) or against a larger cap. Read
        # literally it says "cap - missing" points are owed with `missing`
        # absurdly large, which drives the figure negative; the max() below
        # then pins current at whatever stale value was last stored and the
        # pool NEVER refills again. Observed 2026-09-11: tp_recovery_seconds
        # 600 -> 1 froze an account at 3 TP while its client, computing off
        # its own hardcoded 600s, went on displaying 67. Re-anchor instead --
        # keep the points actually held, restart the clock under the interval
        # in force now.
        if remaining > cap * interval:
            pool["max_recovery_time"] = now + (cap - cur) * interval
            return pool
        # Points still owed at `now`. ceil, because a partially-elapsed tick
        # has not been credited yet.
        missing = math.ceil(remaining / interval)
        pool[self.current] = max(cur, cap - missing)
        return pool

    def add(self, full_state: dict, amount: int, now: int | None = None) -> dict:
        """Grant `amount` (may exceed the cap -- the real game lets a carrot
        overfill, it just warns first), pulling the finish line closer by
        amount*interval so the partial tick in flight is preserved."""
        now = int(now if now is not None else time.time())
        pool = self.read(full_state, now)
        if amount <= 0:
            return pool
        cap = int(pool[self.max] or self.default_max)
        cur = int(pool[self.current] or 0)
        pool[self.current] = cur + amount
        if pool[self.current] >= cap:
            pool["max_recovery_time"] = 0
        else:
            pool["max_recovery_time"] = int(pool["max_recovery_time"]) - amount * self.interval()
        return pool

    def spend(self, full_state: dict, amount: int, now: int | None = None) -> dict | None:
        """Deduct `amount`, or None (and no mutation) if the pool can't cover
        it. Pushes the finish line out by amount*interval; from a FULL pool
        there is no finish line yet, so one is started at `now`."""
        now = int(now if now is not None else time.time())
        pool = self.read(full_state, now)
        if amount <= 0:
            return pool
        cap = int(pool[self.max] or self.default_max)
        cur = int(pool[self.current] or 0)
        if cur < amount:
            return None
        was_full = cur >= cap
        pool[self.current] = cur - amount
        if was_full:
            pool["max_recovery_time"] = now + (cap - pool[self.current]) * self.interval()
        else:
            pool["max_recovery_time"] = int(pool["max_recovery_time"]) + amount * self.interval()
        return pool


TP = _Pool(TP_STATE_KEY, "current_tp", "max_tp", _DEFAULT_MAX_TP, tp_interval)
RP = _Pool(RP_STATE_KEY, "current_rp", "max_rp", _DEFAULT_MAX_RP, rp_interval)


def refresh(full_state: dict, now: int | None = None) -> None:
    """Tick both pools. The caller owns saving."""
    TP.read(full_state, now)
    RP.read(full_state, now)


def tp_info(full_state: dict) -> dict:
    return copy.deepcopy(TP.read(full_state))


def rp_info(full_state: dict) -> dict:
    return copy.deepcopy(RP.read(full_state))


# ------------------------------------------------- career (TP) pricing --
def _campaign_use_tp(scenario_id: int, now: int) -> int | None:
    """An active SingleUseTP campaign's ABSOLUTE cost, or None.

    target_type 1 = MasterCampaignData.TargetCategory.SingleMode,
    effect_type_1 4 = EffectType.SingleUseTP. target_id 0 means every
    scenario; a scenario-specific row wins over the blanket one."""
    try:
        row = master_data.query_one(
            "SELECT effect_value_1 FROM campaign_data "
            "WHERE target_type=1 AND effect_type_1=4 "
            "  AND (target_id=0 OR target_id=?) "
            "  AND start_time<=? AND end_time>=? "
            "ORDER BY target_id DESC, campaign_id DESC LIMIT 1",
            (int(scenario_id or 0), now, now))
    except Exception:
        log.exception("SingleUseTP campaign lookup failed; using the base cost")
        return None
    if not row or not row["effect_value_1"]:
        return None
    return int(row["effect_value_1"])


@functools.lru_cache(maxsize=None)
def _ratio(table: str, key_column: str, key: int) -> int:
    """A consume_tp_ratio off one of the three tables that carry one, on a
    basis of 10000. Cached -- master.mdb is read-only for the process."""
    try:
        row = master_data.query_one(
            f"SELECT consume_tp_ratio FROM {table} WHERE {key_column}=?", (key,))
    except Exception:
        log.exception("consume_tp_ratio lookup failed for %s.%s=%s", table, key_column, key)
        return _RATIO_BASE
    if not row or not row["consume_tp_ratio"]:
        return _RATIO_BASE
    return int(row["consume_tp_ratio"])


def career_tp_cost(start_chara: dict, now: int | None = None) -> int:
    """What one career run costs THIS account right now.

    Base 30, replaced outright by an active SingleUseTP campaign (15 during
    the half-TP window), then scaled by whichever 2x boost the player opted
    into on the setup screen. 15 at the floor, 60 at the ceiling."""
    now = int(now if now is not None else time.time())
    start_chara = start_chara if isinstance(start_chara, dict) else {}
    scenario_id = start_chara.get("scenario_id") or 0

    override = config.get("career_tp_cost")
    base = int(override) if override is not None else (
        _campaign_use_tp(scenario_id, now) or _base_career_tp())

    ratio = _RATIO_BASE
    story_id = int(start_chara.get("boost_story_event_id") or 0)
    if story_id:
        ratio = _ratio("story_event_data", "story_event_id", story_id)
    else:
        research_id = int(start_chara.get("boost_factor_research_event_id") or 0)
        if research_id:
            ratio = _ratio("factor_research_data", "factor_research_event_id", research_id)
    if start_chara.get("is_play_training_challenge"):
        mode = int(start_chara.get("training_challenge_mode") or 0)
        if mode:
            ratio = ratio * _ratio("training_challenge_master", "id", mode) // _RATIO_BASE

    return max(0, base * ratio // _RATIO_BASE)


def spend_career_tp(viewer_id, full_state: dict, start_chara: dict,
                    claimed_use_tp=None) -> bool:
    """Charge a career start. False (and no mutation) if the account cannot
    afford it -- the caller must then refuse the start rather than hand out
    a free run.

    The client sends its own `use_tp` and shows that number to the player;
    a disagreement means our master-data reading of the campaign/boost state
    has drifted from the client's, so it is logged loudly even though the
    server's own figure is what gets charged."""
    cost = career_tp_cost(start_chara)
    if claimed_use_tp is not None and int(claimed_use_tp or 0) != cost:
        log.warning("career TP cost disagreement for viewer %s (scenario %s): "
                    "client says %s, server charges %s",
                    viewer_id, (start_chara or {}).get("scenario_id"),
                    claimed_use_tp, cost)
    if TP.spend(full_state, cost) is None:
        log.info("career start refused for viewer %s: %s TP needed, %s held",
                 viewer_id, cost, TP.read(full_state).get("current_tp"))
        return False
    return True


# ------------------------------------------------------------ handlers --
def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


@functools.lru_cache(maxsize=None)
def _recovery_item(item_id: int):
    """(pool, amount-per-unit) for a recovery item, or None if item_id is not
    one. Read straight off item_data rather than hardcoded: the TP family
    alone is 70 rows and grows with every seasonal chocolate."""
    row = master_data.query_one(
        "SELECT item_category, effect_value_1 FROM item_data WHERE id=?", (item_id,))
    if not row:
        return None
    if row["item_category"] == _TP_ITEM_CATEGORY:
        return TP, int(row["effect_value_1"] or 0)
    if row["item_category"] == _RP_ITEM_CATEGORY:
        return RP, int(row["effect_value_1"] or 0)
    return None


def consume_recovery_items(full_state: dict, item_ids) -> list:
    """Burn one each of `item_ids` (Team Trials' start request passes RP
    jellies this way) and return the ids actually consumed."""
    from . import shop
    used = []
    for raw in item_ids or []:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        found = _recovery_item(item_id)
        if not found or shop.item_count(full_state, item_id) < 1:
            continue
        pool, amount = found
        shop.add_item(full_state, item_id, -1)
        pool.add(full_state, amount)
        used.append(item_id)
    return used


@registry.endpoint("item/use_recovery_item")
def handle_use_recovery_item(payload: dict) -> dict:
    """item/use_recovery_item {item_id, client_own_num, item_num} ->
    {tp_info, rp_info}.

    Capture-confirmed shape (4 in the corpus, all item 32 x1): BOTH keys are
    present and the pool the item does not touch is sent as null, not
    omitted. client_own_num is the client's own belief about its stock and
    is deliberately ignored -- item_list_state is the authority."""
    from . import shop
    viewer_id = payload["viewer_id"]
    item_id = int(payload.get("item_id") or 0)
    item_num = max(1, int(payload.get("item_num") or 1))

    found = _recovery_item(item_id)
    if not found:
        log.warning("item/use_recovery_item: item %s is not a TP/RP recovery "
                    "item (viewer %s)", item_id, viewer_id)
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    held = shop.item_count(full_state, item_id)
    if held < item_num:
        log.info("item/use_recovery_item refused: viewer %s asked for %s x%s, holds %s",
                 viewer_id, item_id, item_num, held)
        return _refuse()

    pool, amount = found
    shop.add_item(full_state, item_id, -item_num)
    pool.add(full_state, amount * item_num)
    refresh(full_state)
    state_store.save_state(viewer_id, full_state)

    return _ok({"tp_info": tp_info(full_state) if pool is TP else None,
                "rp_info": rp_info(full_state) if pool is RP else None})


# PATH UNCONFIRMED. RecoveryTrainerPointRequest/RecoveryRacePointRequest
# {count, client_own_num} -> {coin_info, tp_info/rp_info} are read exactly
# off dump.cs, but dump.cs holds no URL strings and neither endpoint has ever
# been captured, so the path itself is inferred from the PascalCase ->
# snake_case convention every known pair follows. Both plausible spellings are
# registered; if the real client uses a third, pressing the refill button once
# prints it as main.py's "no handler/fixture for ..." warning and it can be
# added to the tuples below.
_TP_REFILL_PATHS = ("recovery/trainer_point", "user/recovery_trainer_point")
_RP_REFILL_PATHS = ("recovery/race_point", "user/recovery_race_point")


def _buy_recovery(payload: dict, pool, carat_key: str, carat_default: int,
                  amount_key: str, amount_default: int, info_key: str) -> dict:
    """The shared body of the two paid refills: N carats -> a pool grant."""
    from . import shop
    viewer_id = payload["viewer_id"]
    count = max(1, int(payload.get("count") or 1))
    full_state = state_store.get_state(viewer_id) or {}

    cost = config.get_int(carat_key, carat_default) * count
    if shop.spend_carats(full_state, cost) is None:
        log.info("paid %s refill refused: viewer %s cannot afford %s carats",
                 info_key, viewer_id, cost)
        return _refuse()

    # amount 0 means "fill the pool" -- the natural reading for RP, whose
    # real-game refill tops the whole 5 up rather than granting one point.
    per = config.get_int(amount_key, amount_default)
    if per <= 0:
        current = pool.read(full_state)
        per = max(0, int(current[pool.max] or 0) - int(current[pool.current] or 0))
    pool.add(full_state, per * count)
    refresh(full_state)
    state_store.save_state(viewer_id, full_state)

    return _ok({"coin_info": copy.deepcopy(full_state.get("coin_info_state")),
                info_key: tp_info(full_state) if pool is TP else rp_info(full_state)})


def _register_refills() -> None:
    """Both spellings of both refills, bound to the same body."""
    def make(pool, carat_key, carat_default, amount_key, amount_default, info_key):
        def handler(payload: dict) -> dict:
            return _buy_recovery(payload, pool, carat_key, carat_default,
                                 amount_key, amount_default, info_key)
        return handler

    for path in _TP_REFILL_PATHS:
        registry.endpoint(path)(make(TP, "tp_refill_carat_cost", 10,
                                     "tp_refill_amount", 30, "tp_info"))
    for path in _RP_REFILL_PATHS:
        registry.endpoint(path)(make(RP, "rp_refill_carat_cost", 50,
                                     "rp_refill_amount", 0, "rp_info"))


_register_refills()
