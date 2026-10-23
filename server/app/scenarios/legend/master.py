
from __future__ import annotations

import functools

from ... import master_data
from ..venus import master as VM

ST_LITE, SPEED, HAISEIKO = 9046, 9047, 9048
LEGENDS = (ST_LITE, SPEED, HAISEIKO)
GROUP_CARD = 30241
PASSION = 104
GAUGE_MAX = 8
BUFF_LIMIT = 10
RANK2_HELD, RANK3_HELD = 2, 4
ALMOND_EYE_ROWS = (91129901, 91129902)

EFF_TRAINING = 1
EFF_HINT = 2
EFF_SPECIALTY = 3
EFF_FAILURE_DOWN = 5
EFF_HP_DOWN = 6
EFF_GUEST_LEVEL = 7
EFF_SUPER_MOOD = 8
EFF_ADD_SUPPORT = 10
EFF_BOND_UP = 15
EFF_FRIENDSHIP = 17
EFF_APPEAR = 24
EFF_HINT_COUNT = 25
EFF_HINT_FIXED = 26
EFF_MOOD = 28
EFF_IMMEDIATE = 29
EFF_BOND_NOW = 30

COND_COMMAND = 2
COND_FRIENDSHIP = 8
COND_SUPPORTERS = 13
COND_MOTIVATION = 14


def _rows(sql: str, params: tuple = ()) -> list:
    try:
        return [dict(r) for r in master_data.query(sql, params)]
    except Exception:
        return []


@functools.lru_cache(maxsize=1)
def buffs() -> dict:
    return {int(r["buff_id"]): r for r in _rows("SELECT * FROM single_mode_10_buff ORDER BY buff_id")}


def buff(buff_id) -> dict:
    return buffs().get(int(buff_id or 0)) or {}


def legend_of(buff_id) -> int:
    return int(buff(buff_id).get("legend_id") or 0)


def rank_of(buff_id) -> int:
    return int(buff(buff_id).get("buff_rank") or 0)


@functools.lru_cache(maxsize=1)
def _conditions() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_10_buff_condition ORDER BY id"):
        out.setdefault(int(r["condition_group_id"]), []).append(
            (int(r["condition_type"]), int(r["condition_value_1"]), int(r["condition_value_2"]),
             int(r["condition_value_3"]), int(r["condition_value_4"])))
    return out


def conditions(group: int) -> list:
    return list(_conditions().get(int(group or 0), ()))


@functools.lru_cache(maxsize=1)
def _effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_10_buff_effect ORDER BY id"):
        out.setdefault(int(r["effect_group_id"]), []).append(
            (int(r["effect_type"]), int(r["effect_value_1"]), int(r["effect_value_2"]),
             int(r["legend_id"]), int(r["bonus_value"])))
    return out


def effects(group: int) -> list:
    return list(_effects().get(int(group or 0), ()))


def buff_effects(buff_id) -> list:
    return effects(int(buff(buff_id).get("effect_group_id") or 0))


@functools.lru_cache(maxsize=1)
def buff_num() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_10_buff_num ORDER BY id"):
        out.setdefault(int(r["legend_id"]), []).append(
            (int(r["gauge_min"]), int(r["gauge_max"]), int(r["num"]),
             int(r["highest_num"]), int(r["link_num"])))
    return out


def offers_for(legend: int, gauge: int) -> tuple:
    for lo, hi, num, highest, _link in buff_num().get(int(legend), ()):
        if lo <= int(gauge) <= hi:
            return num, highest
    return 0, 0


@functools.lru_cache(maxsize=1)
def lottery_turns() -> frozenset:
    rows = _rows("SELECT turn FROM single_mode_10_lottery_turn")
    return frozenset(int(r["turn"]) for r in rows) or frozenset(range(6, 67, 6))


@functools.lru_cache(maxsize=1)
def masterly() -> dict:
    rows = _rows("SELECT legend_id, effect_group_id FROM single_mode_10_masterly")
    return {int(r["legend_id"]): int(r["effect_group_id"]) for r in rows} or {
        g: g for g in LEGENDS}


def masterly_value(legend: int, effect_type: int) -> int:
    return sum(v1 for et, v1, _v2, _l, _b in effects(masterly().get(int(legend), 0))
               if et == effect_type)


@functools.lru_cache(maxsize=1)
def zone_rewards() -> list:
    return [(int(r["min_turn"]), int(r["max_turn"]), int(r["end_type"]), int(r["gain_type"]),
             int(r["gain_value"])) for r in _rows("SELECT * FROM single_mode_10_9047_reward ORDER BY id")]


def zone_reward(n: int, end_type: int) -> tuple:
    stats = sp = 0
    for lo, hi, et, gt, v in zone_rewards():
        if et == end_type and lo <= n <= (hi or 99):
            if gt == 1:
                stats += v
            elif gt == 2:
                sp += v
    return stats, sp


@functools.lru_cache(maxsize=1)
def _card_levels() -> list:
    return [(int(r["min_level"]), int(r["max_level"]), int(r["level_bonus"]))
            for r in _rows("SELECT * FROM single_mode_10_9048_bonus ORDER BY id")]


def card_level_bonus(level: int) -> int:
    for lo, hi, v in _card_levels():
        if lo <= level <= hi:
            return v
    return 10


@functools.lru_cache(maxsize=1)
def _guest_levels() -> list:
    return [(int(r["min_level"]), int(r["max_level"]), int(r["level_bonus_1"]),
             int(r["level_bonus_2"])) for r in _rows("SELECT * FROM single_mode_10_9048_guest ORDER BY id")]


def guest_level_bonus(level: int) -> tuple:
    for lo, hi, a, b in _guest_levels():
        if lo <= level <= hi:
            return a, b
    return 25, 20


@functools.lru_cache(maxsize=1)
def sprace_turns() -> dict:
    rows = _rows("SELECT race_group_id, turn_num FROM single_mode_10_sprace")
    return ({int(r["turn_num"]): int(r["race_group_id"]) for r in rows}
            or {24: 100001, 48: 100002, 72: 100003})


RACE_TYPE = {100001: 1, 100002: 2, 100003: 3}


def is_sprace_program(program_id) -> bool:
    return 2910 <= int(program_id or 0) <= 3025


@functools.lru_cache(maxsize=1)
def cm_details() -> list:
    return _rows("SELECT * FROM single_mode_10_cm_detail ORDER BY id")


@functools.lru_cache(maxsize=1)
def outing_effects() -> dict:
    return {(int(r["chara_id"]), int(r["step"])): (int(r["effect_type"]), int(r["effect_value"]))
            for r in _rows("SELECT * FROM single_mode_10_group_outing_eff")}


group_programs = VM.group_programs
program_course = VM.program_course
program_instance = VM.program_instance
