
from __future__ import annotations

import math

from ... import training_formula as tf
from ..ramen import formula as RF
from . import master as M

TRAINING_COMMANDS = RF.TRAINING_COMMANDS
SUMMER_TO_BASE = RF.SUMMER_TO_BASE
BASE_TO_SUMMER = RF.BASE_TO_SUMMER
STATS = RF.STATS
TARGET_SP = RF.TARGET_SP
TARGET_ENERGY = RF.TARGET_ENERGY

BASE = {
    101: {1: {1: 10, 3: 3}, 2: {1: 11, 3: 3}, 3: {1: 12, 3: 3}, 4: {1: 13, 3: 4},
          5: {1: 14, 3: 5}, 6: {1: 15, 3: 6}},
    105: {1: {2: 8, 4: 6}, 2: {2: 9, 4: 6}, 3: {2: 10, 4: 6}, 4: {2: 11, 4: 7},
          5: {2: 12, 4: 8}, 6: {2: 13, 4: 9}},
    102: {1: {2: 4, 3: 9}, 2: {2: 4, 3: 10}, 3: {2: 4, 3: 11}, 4: {2: 5, 3: 12},
          5: {2: 6, 3: 13}, 6: {2: 7, 3: 14}},
    103: {1: {1: 2, 3: 3, 4: 9}, 2: {1: 2, 3: 3, 4: 10}, 3: {1: 2, 3: 3, 4: 11},
          4: {1: 3, 3: 3, 4: 12}, 5: {1: 3, 3: 4, 4: 13}, 6: {1: 4, 3: 5, 4: 14}},
    106: {1: {1: 2, 5: 8}, 2: {1: 2, 5: 9}, 3: {1: 2, 5: 10}, 4: {1: 3, 5: 11},
          5: {1: 4, 5: 12}, 6: {1: 5, 5: 13}},
}
LV6 = {cmd: {lv: dict(rows[6]) for lv in range(1, 6)} for cmd, rows in BASE.items()}
BASE_SP = 5
ENERGY = {101: {1: -19, 2: -20, 3: -21, 4: -23, 5: -25}}
ENERGY_DEFAULT = {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}
SUMMER_ENERGY = {601: -25, 602: -26, 603: -26, 604: -26}
WIT_ENERGY = 5
FRIENDSHIP_BOND = RF.FRIENDSHIP_BOND
GROUP_TYPE = 3


def level_of(command_id: int, level: int) -> int:
    return 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))


def friend_fn(chara_info: dict, command_id: int, all_friend: bool = False):
    base = SUMMER_TO_BASE.get(command_id, command_id)
    passion = M.PURE_PASSION in set(chara_info.get("chara_effect_id_array") or ())

    def fn(sid, kind, cmd, bond):
        if all_friend:
            return True
        if kind == 1:
            return cmd == base and bond >= FRIENDSHIP_BOND
        return kind == GROUP_TYPE and passion and bond >= FRIENDSHIP_BOND
    return fn


def is_friendship_training(chara_info: dict, command_id: int, partners,
                           all_friend: bool = False) -> bool:
    fn = friend_fn(chara_info, command_id, all_friend)
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if fn(sid, kind, cmd, bond):
            return True
    return False


def home_raw(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
             *, all_friend: bool = False, level6: bool = False) -> dict:
    return RF.home_raw(chara_info, command_id, level, partners, card_levels,
                       table=LV6 if level6 else BASE, sp=BASE_SP,
                       friend_fn=friend_fn(chara_info, command_id, all_friend))


def home_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
              **kw) -> dict:
    raw = home_raw(chara_info, command_id, level, partners, card_levels, **kw)
    return {tt: RF.capped(v) for tt, v in raw.items()}


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


def base_energy(command_id: int, level: int, level6: bool = False) -> int:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    if base_cmd == 106:
        return WIT_ENERGY
    if command_id in SUMMER_ENERGY and not level6:
        return SUMMER_ENERGY[command_id]
    lv = 5 if level6 else level_of(command_id, level)
    return ENERGY.get(base_cmd, ENERGY_DEFAULT).get(lv, -20)


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
           level6: bool = False, all_friend: bool = False) -> int:
    base = base_energy(command_id, level, level6)
    if base < 0:
        mult = 1.0
        for f in energy_down_factors(chara_info, partners, card_levels):
            mult *= f
        return -int(math.floor(-base * mult + 1e-9))
    fn = friend_fn(chara_info, command_id, all_friend)
    total = 0.0
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if fn(sid, kind, cmd, bond):
            lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
            total += (max(0.0, RF.card_effect(sid, tf.EFFECT_WIT_RECOVERY, lv))
                      + tf.bond_gated_effect(sid, tf.EFFECT_WIT_RECOVERY, bond, lv))
    return base + int(round(total))


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
               *, all_friend: bool = False, level6: bool = False) -> dict | None:
    line = home_line(chara_info, command_id, level, partners, card_levels,
                     all_friend=all_friend, level6=level6)
    if not line:
        return None
    out = {}
    for tt, pre in line.items():
        shown = RF.displayed(chara_info, tt, pre)
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = shown
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels, level6, all_friend)
    return out


def bonus_params(chara_info: dict, command_id: int, level: int, partners, card_levels=None, *,
                 slot_bonus: dict, training_pct: int, energy_down: int,
                 all_friend: bool = False, level6: bool = False) -> list:
    line = home_line(chara_info, command_id, level, partners, card_levels,
                     all_friend=all_friend, level6=level6)
    out = []
    for tt in sorted(line):
        home = line[tt]
        f = int(slot_bonus.get(M.TYPE_SP if tt == TARGET_SP else tt, 0))
        if home <= 0 and not f:
            continue
        total = int(math.floor((home + f) * (1 + training_pct / 100) + 1e-9))
        lift = total - home
        if lift <= 0:
            continue
        shown = RF._shown_bonus(chara_info, tt, home, lift)
        if shown > 0:
            out.append({"target_type": tt, "value": shown})
    if energy_down:
        cost = energy(chara_info, command_id, level, partners, card_levels, level6, all_friend)
        if cost < 0:
            refund = int(math.ceil(-cost * energy_down / 100 - 1e-9))
            if refund > 0:
                out.append({"target_type": TARGET_ENERGY, "value": refund})
    return out
