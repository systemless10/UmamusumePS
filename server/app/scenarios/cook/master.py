
from __future__ import annotations

import collections
import functools

from ... import master_data

MATERIALS = (100, 200, 300, 400, 500)
COMMAND_MATERIAL = {101: 100, 105: 200, 102: 300, 103: 400, 106: 500}

GE_DISH_TRAINING = 100
GE_STOCK_CAP = 110
GE_HARVEST = 130
GE_DISH_ENERGY = 140
GE_DISH_RACE = 150

EFF_TRAINING = 2
EFF_RACE = 21
EFF_ENERGY = 201
EFF_MOTIVATION = 202
EFF_BOND = 203
EFF_EXTRA_CARD = 204
EFF_MAX_ENERGY = 205
LASTING = (EFF_TRAINING, EFF_RACE)


def _rows(table: str) -> list:
    if not master_data.has_table(table):
        return []
    return [dict(r) for r in master_data.query(f"SELECT * FROM {table} ORDER BY id")]


@functools.lru_cache(maxsize=1)
def dishes() -> dict:
    return {int(r["id"]): {k: int(v or 0) for k, v in r.items()}
            for r in _rows("single_mode_cook_dish")}


@functools.lru_cache(maxsize=1)
def dish_costs() -> dict:
    out = collections.defaultdict(dict)
    for r in _rows("single_mode_cook_dish_material"):
        out[int(r["material_group_id"])][int(r["material_id"])] = int(r["material_num"])
    return dict(out)


@functools.lru_cache(maxsize=1)
def dish_effects() -> dict:
    out = collections.defaultdict(list)
    for r in _rows("single_mode_cook_dish_effect"):
        out[int(r["effect_group_id"])].append(tuple(int(r[k] or 0) for k in (
            "effect_type", "effect_value_1", "effect_value_2", "effect_value_3", "effect_value_4")))
    return dict(out)


@functools.lru_cache(maxsize=1)
def success_odds() -> list:
    return [(int(r["power_min"]), int(r["power_max"]), int(r["success_rate"]))
            for r in _rows("single_mode_cook_success_odds")]


def success_rate_band(power: int) -> int:
    return next((rate for lo, hi, rate in success_odds() if lo <= int(power) <= hi), 0)


@functools.lru_cache(maxsize=1)
def success_effects() -> dict:
    out = collections.defaultdict(list)
    for r in _rows("single_mode_cook_success_effect"):
        out[int(r["effect_group_id"])].append({k: int(v or 0) for k, v in r.items()})
    return dict(out)


@functools.lru_cache(maxsize=1)
def power_data() -> dict:
    return {int(r["cook_type"]): {k: int(v or 0) for k, v in r.items()}
            for r in _rows("single_mode_cook_power_data")}


def tasting_for_turn(turn) -> int:
    return next((k for k, r in power_data().items() if r["turn_num"] == int(turn or 0)), 0)


@functools.lru_cache(maxsize=1)
def garden_levels() -> dict:
    return {(int(r["facility_id"]), int(r["facility_lv"])):
            {"garden_lv": int(r["garden_lv"]), "effect_group_id": int(r["effect_group_id"]),
             "coin_num": int(r["coin_num"])}
            for r in _rows("single_mode_cook_garden_level")}


@functools.lru_cache(maxsize=1)
def garden_effects() -> dict:
    out = collections.defaultdict(list)
    for r in _rows("single_mode_cook_garden_effect"):
        out[int(r["effect_group_id"])].append(tuple(int(r[k] or 0) for k in (
            "effect_type", "effect_value_1", "effect_value_2", "effect_value_3", "effect_value_4")))
    return dict(out)


def facility_effects(facility_id: int, level: int) -> list:
    row = garden_levels().get((int(facility_id), int(level)))
    return garden_effects().get(row["effect_group_id"], []) if row else []


def stock_cap(facility_id: int, level: int) -> int:
    return next((v2 for t, _v1, v2, _v3, _v4 in facility_effects(facility_id, level)
                 if t == GE_STOCK_CAP), 999)


def harvest_amount(facility_id: int, level: int, kind: int) -> int:
    return next((v3 for t, v1, _v2, v3, _v4 in facility_effects(facility_id, level)
                 if t == GE_HARVEST and v1 == kind), 0)


@functools.lru_cache(maxsize=1)
def material_rates() -> dict:
    return {(int(r["boost_type"]), int(r["material_count"])): int(r["material_quantity_rate"])
            for r in _rows("single_mode_cook_material_rate")}


@functools.lru_cache(maxsize=1)
def coin_rates() -> dict:
    return {(int(r["period"]), int(r["material_count"])): int(r["coin_quantity_rate"])
            for r in _rows("single_mode_cook_coin_rate")}


@functools.lru_cache(maxsize=1)
def dish_names() -> list:
    return [{k: int(v or 0) for k, v in r.items()} for r in _rows("single_mode_cook_dish_name")]


def dish_ids_of_set(dish_set_id: int) -> list:
    return [i for i, d in sorted(dishes().items()) if d["dish_set_id"] == int(dish_set_id)]
