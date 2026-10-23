
from __future__ import annotations

import functools

from ... import master_data

EFFECT_PARAM = 1
EFFECT_TRAINING = 2
EFFECT_FRIENDSHIP = 4
EFFECT_RACE = 7
EFFECT_LIMIT = 10
EFFECT_SPECIALTY = 13
EFFECT_HINT_RATE = 14
EFFECT_BOND = 15
EFFECT_ALL_TIPS = 16
EFFECT_TRAINING_TIPS = 17
EFFECT_EXTRA_CARDS = 18
EFFECT_TYPE_HINTS = 19
EFFECT_EXTRA_CARD = 20
EFFECT_SP_TRAINING = 21
EFFECT_FAILURE_DOWN = 22

COND_PARAM = 2
COND_COMMAND = 3
COND_CARD_TYPE = 5
COND_DECK_TYPES = 6

CATEGORY_REGION = 1
CATEGORY_BASIC = 2
CATEGORY_SPECIAL_FEELING = 3
CATEGORY_URAF = 4


def _rows(sql: str, params: tuple = ()) -> list:
    try:
        return [dict(r) for r in master_data.query(sql, params)]
    except Exception:
        return []


def conditions(row: dict) -> list:
    out = []
    for i in (1, 2, 3):
        ctype = int(row.get(f"condition_type_{i}") or 0)
        if ctype:
            out.append((ctype, int(row.get(f"condition_value_{i}") or 0)))
    return out


@functools.lru_cache(maxsize=1)
def region_select_turns() -> dict:
    return {int(r["turn"]): int(r["region_select_type"])
            for r in _rows("SELECT region_select_type, turn FROM single_mode_14_region_select")}


@functools.lru_cache(maxsize=1)
def region_feelings() -> dict:
    out: dict = {}
    for r in _rows("SELECT region_id, feeling_id, feeling_num FROM single_mode_14_region_feeling"):
        out.setdefault(int(r["region_id"]), {})[int(r["feeling_id"])] = int(r["feeling_num"])
    return out


@functools.lru_cache(maxsize=1)
def region_types() -> dict:
    return {int(r["region_id"]): int(r["region_select_type"])
            for r in _rows("SELECT DISTINCT region_id, region_select_type "
                           "FROM single_mode_14_region_feeling")}


@functools.lru_cache(maxsize=1)
def region_effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_14_region_effect ORDER BY id"):
        out.setdefault(int(r["region_id"]), []).append(r)
    return out


@functools.lru_cache(maxsize=1)
def region_effect_by_id() -> dict:
    return {int(r["id"]): r for rows in region_effects().values() for r in rows}


@functools.lru_cache(maxsize=1)
def _region_bonus_rows() -> tuple:
    return tuple(_rows("SELECT region_id, effect_type, min_pt, max_pt, add_value "
                       "FROM single_mode_14_region_effect_bonus"))


def region_effect_bonus(region_id: int, effect_type: int, pt: int) -> int:
    for r in _region_bonus_rows():
        if (int(r["region_id"]) == region_id and int(r["effect_type"]) == effect_type
                and int(r["min_pt"]) <= pt <= int(r["max_pt"])):
            return int(r["add_value"])
    return 0


@functools.lru_cache(maxsize=1)
def basic_effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_14_basic_effect ORDER BY id"):
        out.setdefault(int(r["check_point_type"]), []).append(r)
    return out


@functools.lru_cache(maxsize=1)
def basic_effect_by_id() -> dict:
    return {int(r["id"]): r for rows in basic_effects().values() for r in rows}


@functools.lru_cache(maxsize=1)
def check_points() -> dict:
    return {int(r["check_point_type"]): {"turn": int(r["turn"]),
                                         "success_pt": int(r["success_pt"]),
                                         "great_success_pt": int(r["great_success_pt"])}
            for r in _rows("SELECT * FROM single_mode_14_check_point")}


def check_point_for_turn(turn) -> int:
    for cp_type, row in check_points().items():
        if row["turn"] == int(turn or 0):
            return cp_type
    return 0


def period_of(turn) -> int:
    turn = int(turn or 0)
    for cp_type in sorted(check_points()):
        if turn <= check_points()[cp_type]["turn"]:
            return cp_type
    return max(check_points() or {3: 0})


@functools.lru_cache(maxsize=1)
def tasting_gain_pt() -> dict:
    return {int(r["check_point_type"]): int(r["gain_pt"])
            for r in _rows("SELECT check_point_type, gain_pt FROM single_mode_14_check_point_pt")}


@functools.lru_cache(maxsize=1)
def _pt_effect_rows() -> tuple:
    return tuple(_rows("SELECT pt_min, pt_max, effect_type, effect_value "
                       "FROM single_mode_14_check_point_pt_effect"))


def pt_effect(pt: int, effect_type: int) -> int:
    return sum(int(r["effect_value"]) for r in _pt_effect_rows()
               if int(r["effect_type"]) == effect_type
               and int(r["pt_min"]) <= int(pt) <= int(r["pt_max"]))


@functools.lru_cache(maxsize=1)
def _result_effect_rows() -> tuple:
    return tuple(_rows("SELECT check_point_type, result_state, effect_type, effect_value "
                       "FROM single_mode_14_check_point_effect"))


def result_effect(check_point_type: int, result_state: int, effect_type: int) -> int:
    return sum(int(r["effect_value"]) for r in _result_effect_rows()
               if int(r["check_point_type"]) == check_point_type
               and int(r["result_state"]) == result_state
               and int(r["effect_type"]) == effect_type)


@functools.lru_cache(maxsize=1)
def _feeling_bonus_rows() -> tuple:
    return tuple(_rows("SELECT feeling_num_min, feeling_num_max, add_value "
                       "FROM single_mode_14_feeling_bonus"))


def feeling_bonus(tricks_held: int) -> int:
    for r in _feeling_bonus_rows():
        if int(r["feeling_num_min"]) <= tricks_held <= int(r["feeling_num_max"]):
            return int(r["add_value"])
    return 0


@functools.lru_cache(maxsize=1)
def deck_link_types() -> dict:
    return {int(r["support_card_id"]): int(r["deck_type"])
            for r in _rows("SELECT deck_type, support_card_id FROM single_mode_14_deck_info")}


@functools.lru_cache(maxsize=1)
def outing_secrets() -> dict:
    return {(int(r["support_card_id"]), int(r["story_step"])): int(r["special_feeling_num"])
            for r in _rows("SELECT support_card_id, story_step, special_feeling_num "
                           "FROM single_mode_14_outing_effect")}


@functools.lru_cache(maxsize=1)
def finals_effects() -> list:
    return _rows("SELECT * FROM single_mode_14_finals_effect ORDER BY id")


@functools.lru_cache(maxsize=1)
def finals_effect_by_id() -> dict:
    return {int(r["id"]): r for r in finals_effects()}


@functools.lru_cache(maxsize=1)
def finals_skills() -> dict:
    return {int(r["select_type"]): r for r in _rows("SELECT * FROM single_mode_14_finals_gain_skill")}


def effect_row(category: int, effect_id: int) -> dict | None:
    table = {CATEGORY_REGION: region_effect_by_id, CATEGORY_BASIC: basic_effect_by_id,
             CATEGORY_URAF: finals_effect_by_id}.get(int(category))
    return table().get(int(effect_id)) if table else None
