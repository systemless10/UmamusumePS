
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
MAIN_TARGET = {101: 1, 105: 2, 102: 3, 103: 4, 106: 5}
LINE_CAP = 100

BASE = {
    101: {1: {1: 11, 3: 2}, 2: {1: 12, 3: 2}, 3: {1: 13, 3: 2}, 4: {1: 14, 3: 3}, 5: {1: 15, 3: 4}},
    105: {1: {2: 8, 4: 6}, 2: {2: 9, 4: 6}, 3: {2: 10, 4: 6}, 4: {2: 11, 4: 7}, 5: {2: 12, 4: 8}},
    102: {1: {2: 4, 3: 10}, 2: {2: 4, 3: 11}, 3: {2: 4, 3: 12}, 4: {2: 5, 3: 13}, 5: {2: 6, 3: 14}},
    103: {1: {1: 2, 3: 2, 4: 9}, 2: {1: 2, 3: 2, 4: 10}, 3: {1: 2, 3: 2, 4: 11},
          4: {1: 3, 3: 2, 4: 12}, 5: {1: 4, 3: 3, 4: 13}},
    106: {1: {1: 2, 5: 8}, 2: {1: 2, 5: 9}, 3: {1: 2, 5: 10}, 4: {1: 3, 5: 11}, 5: {1: 4, 5: 12}},
}
BASE_SP = 7
ENERGY = {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}
SUMMER_ENERGY = {601: -25, 602: -26, 603: -26, 604: -26}
WIT_ENERGY = 5


def level_of(command_id: int, level: int) -> int:
    return 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))


def home_raw(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    return RF.home_raw(chara_info, command_id, level, partners, card_levels, table=BASE, sp=BASE_SP)


def home_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
              sub_pct: int = 100, main_cap: int = LINE_CAP, sp_cap: int = LINE_CAP) -> dict:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    out = {}
    for tt, value in raw.items():
        cap = sp_cap if tt == TARGET_SP else main_cap
        pre = min(cap, int(math.floor(value + 1e-9)))
        if sub_pct != 100 and tt != TARGET_SP and tt != MAIN_TARGET.get(base_cmd):
            pre = max(1, pre * sub_pct // 100) if pre > 0 else 0
        out[tt] = pre
    return out


def energy_down_factors(chara_info: dict, partners, card_levels=None) -> list:
    out = []
    for sid, bond in RF.facility_cards(chara_info, partners):
        lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
        base = RF.card_effect(sid, tf.EFFECT_ENERGY_COST_DOWN, lv)
        if base > 0:
            out.append(1 - base / 100)
        gated = tf.bond_gated_effect(sid, tf.EFFECT_ENERGY_COST_DOWN, bond, lv)
        if gated > 0:
            out.append(1 - gated / 100)
    return out


def base_energy(command_id: int, level: int) -> int:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    if base_cmd == 106:
        return WIT_ENERGY
    if command_id in SUMMER_ENERGY:
        return SUMMER_ENERGY[command_id]
    return ENERGY.get(level_of(command_id, level), -20)


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> int:
    base = base_energy(command_id, level)
    if base < 0:
        mult = 1.0
        for f in energy_down_factors(chara_info, partners, card_levels):
            mult *= f
        return -int(math.floor(-base * mult + 1e-9))
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    cards = [(sid, bond, None) for sid, bond in RF.facility_cards(chara_info, partners)]
    return tf.training_energy(base, base_cmd, cards)


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
               sub_pct: int = 100, main_cap: int = LINE_CAP, sp_cap: int = LINE_CAP) -> dict | None:
    line = home_line(chara_info, command_id, level, partners, card_levels, sub_pct, main_cap, sp_cap)
    if not line:
        return None
    out = {}
    for tt, pre in line.items():
        shown = RF.displayed(chara_info, tt, pre)
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = shown
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels)
    return out


def bonus_params(chara_info: dict, command_id: int, level: int, partners, card_levels=None, *,
                 member_pct: int = 0, sp_pct: int = 0, team_f: int = 0, dreams_f: int = 0,
                 sub_pct: int = 100, main_cap: int = LINE_CAP, sp_cap: int = LINE_CAP,
                 add_main_cap: int = LINE_CAP, add_sp_cap: int = LINE_CAP,
                 energy_down: int = 0) -> list:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    full = home_line(chara_info, command_id, level, partners, card_levels, 100, main_cap, sp_cap)
    out = []
    for tt in sorted(full):
        pre = full[tt]
        add = member_pct + (sp_pct if tt == TARGET_SP else 0)
        if pre <= 0 or not (add or team_f or dreams_f):
            continue
        total = int(math.floor(pre * (1 + add / 100) * (1 + team_f / 100) * (1 + dreams_f / 100) + 1e-9))
        lift = min(total - pre, add_sp_cap if tt == TARGET_SP else add_main_cap)
        if lift <= 0:
            continue
        is_sub = tt != TARGET_SP and tt != MAIN_TARGET.get(base_cmd)
        home_shown_pre = pre
        if is_sub and sub_pct != 100:
            lift = max(1, lift * sub_pct // 100)
            home_shown_pre = max(1, pre * sub_pct // 100)
        shown = RF._shown_bonus(chara_info, tt, home_shown_pre, lift)
        if shown > 0:
            out.append({"target_type": tt, "value": shown})
    if energy_down:
        cost = energy(chara_info, command_id, level, partners, card_levels)
        if cost < 0:
            left = int(math.floor(-cost * (100 - energy_down) / 100 + 1e-9))
            refund = -cost - left
            if refund > 0:
                out.append({"target_type": TARGET_ENERGY, "value": refund})
    return out
