
from __future__ import annotations

import functools

from ... import master_data

EFFECT_TRAINING = 1
EFFECT_HINT_RATE = 2
EFFECT_ENERGY_COST = 4
EFFECT_SUB_PARTNER = 5
EFFECT_AFFINITY = 7
EFFECT_GOAL_RACE = 8
EFFECT_FAILURE = 12
EFFECT_FRIENDSHIP = 14
EFFECT_ALWAYS_SUPERIOR = 15
EFFECT_HINT_COUNT = 16
EFFECT_PARAM = 101
EFFECT_EVALUATION = 102
EFFECT_CURE = 104
EFFECT_SKILL_HINT = 107
EFFECT_MAX_VITAL = 108

BASE_ONSEN = 1
LEGEND_ONSEN = 10
SUPERIOR_GROUP = 99
RANK_GROUP_BASE = 100

TICKET_DIG_COMPLETE = 1
TICKET_SUMMER_CAMP = 2
TICKET_URA_FINAL_BEFORE = 3

KIYOKO_CHARA = 9050


def _rows(sql: str, params: tuple = ()) -> list:
    try:
        return [dict(r) for r in master_data.query(sql, params)]
    except Exception:
        return []


@functools.lru_cache(maxsize=1)
def onsens() -> dict:
    return {int(r["id"]): int(r["open_turn"])
            for r in _rows("SELECT id, open_turn FROM single_mode_12_onsen")}


@functools.lru_cache(maxsize=1)
def strata() -> dict:
    out: dict = {}
    for r in _rows("SELECT id, onsen_id, stratum_order, stratum_type, stratum_volume "
                   "FROM single_mode_12_stratum ORDER BY onsen_id, stratum_order"):
        out.setdefault(int(r["onsen_id"]), []).append(
            (int(r["id"]), int(r["stratum_type"]), int(r["stratum_volume"])))
    return out


@functools.lru_cache(maxsize=1)
def stratum_info() -> dict:
    return {sid: (onsen, kind, vol)
            for onsen, rows in strata().items() for sid, kind, vol in rows}


@functools.lru_cache(maxsize=1)
def effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT group_id, effect_type, effect_value_1, effect_value_2, "
                   "effect_value_3, effect_value_4 FROM single_mode_12_onsen_effect ORDER BY id"):
        out.setdefault(int(r["group_id"]), []).append(
            (int(r["effect_type"]), int(r["effect_value_1"]), int(r["effect_value_2"]),
             int(r["effect_value_3"]), int(r["effect_value_4"])))
    return out


@functools.lru_cache(maxsize=1)
def dig_item_values() -> dict:
    return {(int(r["item_id"]), int(r["item_level"])): int(r["dig_value"])
            for r in _rows("SELECT item_id, item_level, dig_value FROM single_mode_12_dig_item")}


@functools.lru_cache(maxsize=1)
def item_for_stratum() -> dict:
    return {int(r["stratum_type"]): int(r["item_id"])
            for r in _rows("SELECT DISTINCT item_id, stratum_type FROM single_mode_12_dig_item")}


def max_item_level() -> int:
    return max((lv for _i, lv in dig_item_values()), default=6)


@functools.lru_cache(maxsize=1)
def dig_power() -> dict:
    out: dict = {}
    for r in _rows("SELECT stratum_type, param_type, param_rank_min, param_rank_max, "
                   "bonus_value FROM single_mode_12_dig_power ORDER BY id"):
        out.setdefault((int(r["stratum_type"]), int(r["param_type"])), []).append(
            (int(r["param_rank_min"]), int(r["param_rank_max"]), int(r["bonus_value"])))
    return out


@functools.lru_cache(maxsize=1)
def stratum_params() -> dict:
    out: dict = {}
    for r in _rows("SELECT stratum_type, param_type FROM single_mode_12_dig_power ORDER BY id"):
        lst = out.setdefault(int(r["stratum_type"]), [])
        if int(r["param_type"]) not in lst:
            lst.append(int(r["param_type"]))
    return {k: tuple(v) for k, v in out.items()}


@functools.lru_cache(maxsize=1)
def factor_bonus() -> dict:
    return {(int(r["stratum_type"]), int(r["factor_group_id"])): int(r["bonus_value"])
            for r in _rows("SELECT stratum_type, factor_group_id, bonus_value "
                           "FROM single_mode_12_factor_bonus")}


@functools.lru_cache(maxsize=1)
def dig_bonus() -> dict:
    out: dict = {}
    for r in _rows("SELECT stratum_id, dug_value, effect_value_1, effect_value_2 "
                   "FROM single_mode_12_dig_bonus ORDER BY stratum_id, dug_value, id"):
        rows = out.setdefault(int(r["stratum_id"]), [])
        dv = int(r["dug_value"])
        if not rows or rows[-1][0] != dv:
            rows.append((dv, []))
        rows[-1][1].append((int(r["effect_value_1"]), int(r["effect_value_2"])))
    return out


@functools.lru_cache(maxsize=1)
def ryokan_ranks() -> list:
    return [(int(r["id"]), int(r["dug_onsen_num"]), int(r["dug_onsen_id"]), int(r["group_id"]))
            for r in _rows("SELECT id, group_id, dug_onsen_num, dug_onsen_id "
                           "FROM single_mode_12_ryokan_rank ORDER BY id")]


@functools.lru_cache(maxsize=1)
def schedules() -> dict:
    return {int(r["turn_num"]): int(r["id"])
            for r in _rows("SELECT id, turn_num FROM single_mode_12_schedule")}


@functools.lru_cache(maxsize=1)
def dug_results() -> list:
    return [(int(r["schedule_id"]), int(r["ryokan_rank_min"]), int(r["ryokan_rank_max"]),
             int(r["result_type"]))
            for r in _rows("SELECT schedule_id, ryokan_rank_min, ryokan_rank_max, result_type "
                           "FROM single_mode_12_check_dug_result")]


def dug_result(schedule_id: int, rank: int) -> int:
    for sched, lo, hi, result in dug_results():
        if sched == schedule_id and lo <= rank <= hi:
            return result
    return 1


@functools.lru_cache(maxsize=1)
def spa_tickets() -> dict:
    return {(int(r["type_id"]), int(r["chara_id"]), int(r["rarity"])): int(r["ticket_num"])
            for r in _rows("SELECT type_id, chara_id, rarity, ticket_num FROM single_mode_12_spa_ticket")}


@functools.lru_cache(maxsize=1)
def outing_tickets() -> dict:
    return {(int(r["chara_id"]), int(r["rarity"]), int(r["story_step"])): int(r["ticket_num"])
            for r in _rows("SELECT chara_id, rarity, story_step, ticket_num "
                           "FROM single_mode_12_outing_effect")}


@functools.lru_cache(maxsize=1)
def assistant_params() -> list:
    return [(int(r["target_type"]), int(r["effect_value"]))
            for r in _rows("SELECT target_type, effect_value FROM single_mode_12_assistant_effect "
                           "ORDER BY id")]
