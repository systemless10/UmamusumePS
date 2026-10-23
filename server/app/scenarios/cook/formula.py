
from __future__ import annotations

import math

from ... import training_formula as tf
from ..ramen import formula as RF

TRAINING_COMMANDS = RF.TRAINING_COMMANDS
SUMMER_TO_BASE = RF.SUMMER_TO_BASE
BASE_TO_SUMMER = RF.BASE_TO_SUMMER
STATS = RF.STATS
TARGET_SP = RF.TARGET_SP
PREVIEW_CAP = RF.PREVIEW_CAP
BONUS_CAP = 100
NPC_MIN = RF.NPC_BODY_MIN_ID
DECK_MAX = 100

BASE = {
    101: {1: {1: 11, 3: 2}, 2: {1: 12, 3: 2}, 3: {1: 13, 3: 2}, 4: {1: 14, 3: 3}, 5: {1: 15, 3: 4}},
    105: {1: {2: 8, 4: 5}, 2: {2: 9, 4: 5}, 3: {2: 10, 4: 6}, 4: {2: 11, 4: 7}, 5: {2: 12, 4: 8}},
    102: {1: {2: 4, 3: 9}, 2: {2: 4, 3: 10}, 3: {2: 4, 3: 11}, 4: {2: 5, 3: 12}, 5: {2: 6, 3: 13}},
    103: {1: {1: 2, 3: 2, 4: 10}, 2: {1: 2, 3: 2, 4: 11}, 3: {1: 2, 3: 2, 4: 12},
          4: {1: 3, 3: 3, 4: 13}, 5: {1: 3, 3: 3, 4: 14}},
    106: {1: {1: 2, 5: 8}, 2: {1: 2, 5: 9}, 3: {1: 2, 5: 10}, 4: {1: 3, 5: 11}, 5: {1: 4, 5: 12}},
}
BASE_SP = 5
ENERGY = {101: {1: -19, 2: -20, 3: -21, 4: -23, 5: -25}}
ENERGY_DEFAULT = {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}
SUMMER_ENERGY = {601: -25, 602: -26, 603: -26, 604: -26, 605: 5}
WIT_ENERGY = 5
EPSILON = 5e-4

POWER_EFFECT_TYPES = (2, 206, 207, 208)
POWER_EFFECT_VALUE_1 = {207: 30}
POWER_BANDS = (
    (10000, {2: 30, 206: 20, 207: 60, 208: 45}),
    (7000, {2: 28, 206: 17, 207: 51, 208: 43}),
    (5000, {2: 25, 206: 14, 207: 42, 208: 39}),
    (2500, {2: 21, 206: 11, 207: 33, 208: 34}),
    (1500, {2: 16, 206: 8, 207: 24, 208: 28}),
    (300, {2: 10, 206: 5, 207: 15, 208: 20}),
)


def power_effect(power: int, effect_type: int) -> int:
    for lo, values in POWER_BANDS:
        if int(power) >= lo:
            return values[effect_type]
    return 0


def power_effect_rows(power: int) -> list:
    return [{"effect_type": t, "effect_value_1": POWER_EFFECT_VALUE_1.get(t, 0),
             "effect_value_2": power_effect(power, t), "effect_value_3": 0, "effect_value_4": 0}
            for t in POWER_EFFECT_TYPES]


def base_of(command_id) -> int:
    cid = int(command_id or 0)
    return SUMMER_TO_BASE.get(cid, cid)


def energy_base(command_id: int, level: int) -> int:
    if command_id in SUMMER_ENERGY:
        return SUMMER_ENERGY[command_id]
    if command_id == 106:
        return WIT_ENERGY
    lv = max(1, min(5, int(level or 1)))
    return ENERGY.get(command_id, ENERGY_DEFAULT).get(lv, ENERGY_DEFAULT[lv])


def home_raw(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    return RF.home_raw(chara_info, command_id, level, partners, card_levels, table=BASE, sp=BASE_SP)


def capped(raw: float) -> int:
    return min(PREVIEW_CAP, int(math.floor(raw + EPSILON)))


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict | None:
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    if not raw:
        return None
    out = {}
    for tt, value in raw.items():
        gain = RF.displayed(chara_info, tt, capped(value))
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = gain
    cards = [(sid, bond, None) for sid, bond in RF.facility_cards(chara_info, partners)]
    base_energy = energy_base(command_id, level)
    if base_energy < 0:
        out["energy_cost"] = RF.facility_energy(base_energy, command_id, cards, card_levels)
    else:
        out["energy_cost"] = tf.training_energy(base_energy, base_of(command_id), cards)
    return out


def dish_pct(dish_effects, command_id) -> int:
    base = base_of(command_id)
    total = 0
    for e in dish_effects or ():
        if int(e["effect_type"]) != 2:
            continue
        cmds = (e["effect_value_2"], e["effect_value_3"], e["effect_value_4"])
        if int(e["effect_value_2"]) == 0 or base in cmds:
            total += int(e["effect_value_1"])
    return total


def bonus_line(chara_info: dict, command_id: int, level: int, partners, power_pct: int,
               dish: int, sp_pct: int, card_levels=None) -> list:
    out = []
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    for tt in sorted(raw):
        g0 = capped(raw[tt])
        m = (100 + power_pct + dish) / 100 * ((100 + sp_pct) / 100 if tt == TARGET_SP else 1)
        delta = min(BONUS_CAP, int(g0 * m + 1e-9) - g0)
        if delta > 0:
            out.append({"target_type": tt, "value": RF._shown_bonus(chara_info, tt, g0, delta)})
    return out
