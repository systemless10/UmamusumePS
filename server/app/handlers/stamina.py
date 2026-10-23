
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
from ..result_codes import ResultCode, refuse

TP_STATE_KEY = "tp_info_state"
RP_STATE_KEY = "rp_info_state"

_DEFAULT_MAX_TP = 100
_DEFAULT_MAX_RP = 5

_TP_ITEM_CATEGORY = 20
_RP_ITEM_CATEGORY = 21

_RATIO_BASE = 10000


def tp_interval() -> int:
    return config.get_int("tp_recovery_seconds", 600)


def rp_interval() -> int:
    return config.get_int("rp_recovery_seconds", 7200)


def _base_career_tp() -> int:
    return config.get_int("career_tp_cost", 30)


def team_trials_rp_cost() -> int:
    return config.get_int("team_trials_rp_cost", 1)


class _Pool:

    def __init__(self, state_key, current_field, max_field, default_max, interval):
        self.state_key = state_key
        self.current = current_field
        self.max = max_field
        self.default_max = default_max
        self.interval = interval

    def read(self, full_state: dict, now: int | None = None) -> dict:
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
            pool["max_recovery_time"] = 0
            return pool
        if mrt <= 0:
            pool["max_recovery_time"] = now + (cap - cur) * self.interval()
            return pool
        remaining = mrt - now
        if remaining <= 0:
            pool[self.current] = cap
            pool["max_recovery_time"] = 0
            return pool
        interval = self.interval()
        if remaining > cap * interval:
            pool["max_recovery_time"] = now + (cap - cur) * interval
            return pool
        missing = math.ceil(remaining / interval)
        pool[self.current] = max(cur, cap - missing)
        return pool

    def add(self, full_state: dict, amount: int, now: int | None = None) -> dict:
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
    TP.read(full_state, now)
    RP.read(full_state, now)


def tp_info(full_state: dict) -> dict:
    return copy.deepcopy(TP.read(full_state))


def rp_info(full_state: dict) -> dict:
    return copy.deepcopy(RP.read(full_state))


def _campaign_use_tp(scenario_id: int, now: int) -> int | None:
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
        if mode == 2:
            from .training_challenge import current
            event = current(True)
            if event:
                ratio = ratio * (event["consume_tp_ratio"] or _RATIO_BASE) // _RATIO_BASE

    return max(0, base * ratio // _RATIO_BASE)


def spend_career_tp(viewer_id, full_state: dict, start_chara: dict,
                    claimed_use_tp=None) -> bool:
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


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


@functools.lru_cache(maxsize=None)
def _recovery_item(item_id: int):
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


_TP_REFILL_PATHS = ("recovery/trainer_point", "user/recovery_trainer_point")
_RP_REFILL_PATHS = ("recovery/race_point", "user/recovery_race_point")


def _buy_recovery(payload: dict, pool, carat_key: str, carat_default: int,
                  amount_key: str, amount_default: int, info_key: str) -> dict:
    from . import shop
    viewer_id = payload["viewer_id"]
    count = max(1, int(payload.get("count") or 1))
    full_state = state_store.get_state(viewer_id) or {}

    cost = config.get_int(carat_key, carat_default) * count
    if shop.spend_carats(full_state, cost) is None:
        log.info("paid %s refill refused: viewer %s cannot afford %s carats",
                 info_key, viewer_id, cost)
        return _refuse()

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
