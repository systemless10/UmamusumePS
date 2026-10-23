
from __future__ import annotations

import functools

from ... import master_data

EFFECT_FLAT = 1
EFFECT_TRAINING = 2
EFFECT_ALL_HINTS = 101
EFFECT_HINTS_TAKE = 102
EFFECT_FRIEND_PARTICIPANT = 103
EFFECT_MULTI_RAINBOW = 104
EFFECT_SPECIALTY = 201
EFFECT_HINT_RATE = 204
EFFECT_APPEAR = 205
EFFECT_RACE_PT = 206

CP_TRAINING = 200
CP_POINTS = 202
CP_HINT_RATE = 204

PERIOD_ISLAND = 0
PERIOD_CAMP = 2
PERIOD_FINALS = 3
PERIOD_ALWAYS = 9

FACILITIES = (101, 102, 103, 104, 105, 106)
BEACH_HOUSE = 106
TRAINING_TO_FACILITY = {101: 101, 105: 102, 102: 103, 103: 104, 106: 105}
FACILITY_TO_TRAINING = {v: k for k, v in TRAINING_TO_FACILITY.items()}
ISLAND_VERSIONS = {101: 3601, 105: 3602, 102: 3603, 103: 3604, 106: 3605}
ISLAND_TO_TRAINING = {v: k for k, v in ISLAND_VERSIONS.items()}
ISLAND_TRAINING = 3101
ISLAND_REST = 3606
FACILITY_STATS = {101: (1, 3, 30), 102: (2, 4, 30), 103: (2, 3, 30),
                  104: (1, 3, 4, 30), 105: (1, 5, 30)}

TUCKER_CHARA = 9049


def _rows(sql: str, params: tuple = ()) -> list:
    try:
        return [dict(r) for r in master_data.query(sql, params)]
    except Exception:
        return []


@functools.lru_cache(maxsize=1)
def facility_effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT state_id, effect_type, effect_value_1, effect_value_2, effect_value_3, "
                   "effect_value_4 FROM single_mode_11_facility_effect ORDER BY id"):
        out.setdefault(int(r["state_id"]), []).append(
            (int(r["effect_type"]), int(r["effect_value_1"]), int(r["effect_value_2"]),
             int(r["effect_value_3"]), int(r["effect_value_4"])))
    return out


@functools.lru_cache(maxsize=1)
def lv_up() -> dict:
    out: dict = {}
    for r in _rows("SELECT current_state_id, target_state_id, cost "
                   "FROM single_mode_11_facility_lv_up ORDER BY id"):
        out.setdefault(int(r["current_state_id"]), []).append(
            (int(r["target_state_id"]), int(r["cost"])))
    return out


@functools.lru_cache(maxsize=1)
def state_levels() -> dict:
    return {int(r["state_id"]): int(r["facility_lv"])
            for r in _rows("SELECT state_id, facility_lv FROM single_mode_11_facility_state")}


def level_of(state_id: int) -> int:
    return state_levels().get(int(state_id or 0), 0)


@functools.lru_cache(maxsize=1)
def planning() -> dict:
    return {(int(r["deck_id"]), int(r["check_point_type"])):
            (int(r["planning_turn"]), int(r["check_point_turn"]), int(r["target_pt"]))
            for r in _rows("SELECT deck_id, check_point_type, planning_turn, check_point_turn, "
                           "target_pt FROM single_mode_11_planning")}


def check_point_turns() -> dict:
    return {cp: turn for (deck, cp), (_p, turn, _t) in planning().items() if deck == 0}


def check_point_for_turn(turn: int) -> int:
    return next((cp for cp, t in check_point_turns().items() if t == int(turn or 0)), 0)


def plan_for_turn(turn: int) -> int:
    for cp, t in sorted(check_point_turns().items()):
        if turn <= t:
            return cp
    return 0


@functools.lru_cache(maxsize=1)
def ticket_thresholds() -> dict:
    out: dict = {}
    for r in _rows("SELECT deck_id, turn_min, turn_max, gain_shima_training_pt "
                   "FROM single_mode_11_gain_training_pt ORDER BY id"):
        out.setdefault((int(r["deck_id"]), int(r["turn_min"]), int(r["turn_max"])), []).append(
            int(r["gain_shima_training_pt"]))
    return {k: sorted(v) for k, v in out.items()}


@functools.lru_cache(maxsize=1)
def pt_bonus() -> dict:
    return {int(r["check_point_type"]): (int(r["pt_rate"]), int(r["add_value_max"]))
            for r in _rows("SELECT check_point_type, pt_rate, add_value_max "
                           "FROM single_mode_11_pioneer_pt_bonus")}


@functools.lru_cache(maxsize=1)
def check_point_effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT check_point_type, result_state, effect_type, effect_value_1, "
                   "effect_value_2 FROM single_mode_11_check_point_eff ORDER BY id"):
        out.setdefault((int(r["check_point_type"]), int(r["result_state"])), []).append(
            (int(r["effect_type"]), int(r["effect_value_1"]), int(r["effect_value_2"])))
    return out


def check_point_effect(check_point_type: int, result_state: int, effect_type: int) -> int:
    return next((v2 for t, _v1, v2 in check_point_effects().get(
        (int(check_point_type), int(result_state)), ()) if t == effect_type), 0)


@functools.lru_cache(maxsize=1)
def shima_effect() -> dict:
    out: dict = {}
    for r in _rows("SELECT command_id, result_state, target_type, effect_value, required_facility_id "
                   "FROM single_mode_11_shima_effect ORDER BY id"):
        out.setdefault((int(r["command_id"]), int(r["result_state"]),
                        int(r["required_facility_id"])), []).append(
            (int(r["target_type"]), int(r["effect_value"])))
    return out


@functools.lru_cache(maxsize=1)
def outing_effect() -> list:
    return [(int(r["chara_id"]), int(r["min_lv"]), int(r["value"]))
            for r in _rows("SELECT chara_id, min_lv, value FROM single_mode_11_outing_effect ORDER BY id")]


def tucker_reward(card_level: int) -> int:
    best = (-1, 0)
    for chara, min_lv, value in outing_effect():
        if chara == TUCKER_CHARA and value and card_level >= min_lv and min_lv > best[0]:
            best = (min_lv, value)
    return best[1]
