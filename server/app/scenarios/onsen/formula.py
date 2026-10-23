
from __future__ import annotations

import math

from ... import training_formula as tf
from ..ramen import formula as RF

TRAINING_COMMANDS = RF.TRAINING_COMMANDS
SUMMER_TO_BASE = RF.SUMMER_TO_BASE
BASE_TO_SUMMER = RF.BASE_TO_SUMMER
STATS = RF.STATS
TARGET_SP = RF.TARGET_SP
TARGET_ENERGY = RF.TARGET_ENERGY

BASE = {
    101: {1: {1: 12, 3: 2}, 2: {1: 14, 3: 3}, 3: {1: 17, 3: 4}, 4: {1: 20, 3: 5}, 5: {1: 23, 3: 6}},
    105: {1: {2: 11, 4: 3}, 2: {2: 14, 4: 4}, 3: {2: 17, 4: 5}, 4: {2: 20, 4: 6}, 5: {2: 23, 4: 8}},
    102: {1: {2: 3, 3: 11}, 2: {2: 4, 3: 14}, 3: {2: 5, 3: 17}, 4: {2: 6, 3: 20}, 5: {2: 8, 3: 23}},
    103: {1: {1: 2, 3: 2, 4: 12}, 2: {1: 2, 3: 2, 4: 15}, 3: {1: 2, 3: 2, 4: 18},
          4: {1: 3, 3: 3, 4: 21}, 5: {1: 4, 3: 3, 4: 24}},
    106: {1: {1: 2, 5: 11}, 2: {1: 2, 5: 12}, 3: {1: 2, 5: 13}, 4: {1: 3, 5: 14}, 5: {1: 3, 5: 15}},
}
BASE_SP = 7
ENERGY = {
    101: {1: -20, 2: -25, 3: -30, 4: -35, 5: -40},
    105: {1: -22, 2: -27, 3: -32, 4: -37, 5: -42},
    102: {1: -22, 2: -27, 3: -32, 4: -37, 5: -42},
    103: {1: -22, 2: -27, 3: -32, 4: -37, 5: -42},
    106: {1: 5, 2: 5, 3: 5, 4: 5, 5: 5},
}
PREVIEW_CAP = RF.PREVIEW_CAP


def level_of(command_id: int, level: int) -> int:
    return 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))


def home_raw(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    return RF.home_raw(chara_info, command_id, level, partners, card_levels, table=BASE, sp=BASE_SP)


def displayed(chara_info: dict, target_type: int, gain: int) -> int:
    shown = RF.displayed(chara_info, target_type, gain)
    if target_type <= 5 and shown == 0 and gain > 0:
        stat = STATS[target_type - 1]
        cap_key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        if int(chara_info.get(stat) or 0) < int(chara_info.get(cap_key) or 9999):
            return 1
    return shown


def energy_down_factors(chara_info: dict, partners, card_levels=None,
                        extra_pct: int = 0) -> list:
    out = []
    for sid, bond in RF.facility_cards(chara_info, partners):
        lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
        base = RF.card_effect(sid, tf.EFFECT_ENERGY_COST_DOWN, lv)
        if base > 0:
            out.append(1 - base / 100)
        gated = tf.bond_gated_effect(sid, tf.EFFECT_ENERGY_COST_DOWN, bond, lv)
        if gated > 0:
            out.append(1 - gated / 100)
    if extra_pct:
        out.append(1 - extra_pct / 100)
    return out


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
           dig_energy: int = 0, extra_down_pct: int = 0, cap: int | None = None) -> int:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    base = ENERGY.get(base_cmd, {}).get(level_of(command_id, level), -20)
    if base < 0:
        mult = 1.0
        for f in energy_down_factors(chara_info, partners, card_levels, extra_down_pct):
            mult *= f
        return -int(math.floor(-base * mult + 1e-9)) + int(dig_energy)
    cards = [(sid, bond, None) for sid, bond in RF.facility_cards(chara_info, partners)]
    gain = tf.training_energy(base, base_cmd, cards) + int(dig_energy)
    vital = int(chara_info.get("vital") or 0)
    room = int(cap or chara_info.get("max_vital") or 100) - vital
    if gain > 0:
        gain = min(gain, max(0, room))
    return gain


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
               dig_energy: int = 0, extra_down_pct: int = 0, cap: int | None = None) -> dict | None:
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    if not raw:
        return None
    out = {}
    for tt, value in raw.items():
        gain = displayed(chara_info, tt, RF.capped(value))
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = gain
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels,
                                dig_energy, extra_down_pct, cap)
    return out


def bonus_params(chara_info: dict, command_id: int, level: int, partners,
                 s_pct: int, f_pct: int, dig_stats: dict, card_levels=None) -> list:
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    out = []
    for tt in sorted(set(raw) | {t for t in dig_stats if t != TARGET_ENERGY}):
        home_pre = RF.capped(raw.get(tt, 0))
        delta = 0
        if tt in raw and (s_pct or f_pct):
            total = int(math.floor(home_pre * (1 + s_pct / 100) * (1 + f_pct / 100) + 1e-9))
            if total > home_pre:
                delta = min(total - home_pre, PREVIEW_CAP)
        delta += int(dig_stats.get(tt, 0))
        if delta <= 0:
            continue
        shown = RF._shown_bonus(chara_info, tt, home_pre, delta)
        if shown > 0:
            out.append({"target_type": tt, "value": shown})
    return out


def flat_params(chara_info: dict, gains: dict) -> list:
    out = []
    for tt in sorted(gains):
        v = int(gains[tt])
        if not v:
            continue
        if 1 <= tt <= 5 and v > 0:
            v = displayed(chara_info, tt, v)
        out.append({"target_type": tt, "value": v})
    return out
