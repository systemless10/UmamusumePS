
from __future__ import annotations

import math

from ... import training_formula as tf
from ..legend import formula as LF
from ..ramen import formula as RF
from . import master as M

TRAINING_COMMANDS = RF.TRAINING_COMMANDS
SUMMER_TO_BASE = RF.SUMMER_TO_BASE
BASE_TO_SUMMER = RF.BASE_TO_SUMMER
STATS = RF.STATS
TARGET_SP = RF.TARGET_SP
TARGET_ENERGY = RF.TARGET_ENERGY
PREVIEW_CAP = RF.PREVIEW_CAP

WIRE_ID = {101: 901, 102: 902, 106: 906}
SERVER_ID = {v: k for k, v in WIRE_ID.items()}

BASE = {
    101: {1: {1: 11, 3: 2}, 2: {1: 12, 3: 2}, 3: {1: 13, 3: 2}, 4: {1: 14, 3: 3}, 5: {1: 15, 3: 4}},
    105: {1: {2: 10, 4: 4}, 2: {2: 11, 4: 4}, 3: {2: 12, 4: 5}, 4: {2: 13, 4: 5}, 5: {2: 14, 4: 6}},
    102: {1: {2: 4, 3: 10}, 2: {2: 4, 3: 11}, 3: {2: 5, 3: 12}, 4: {2: 5, 3: 13}, 5: {2: 6, 3: 14}},
    103: {1: {1: 2, 3: 2, 4: 9}, 2: {1: 2, 3: 2, 4: 10}, 3: {1: 2, 3: 2, 4: 11},
          4: {1: 3, 3: 3, 4: 12}, 5: {1: 4, 3: 3, 4: 13}},
    106: {1: {1: 2, 5: 8}, 2: {1: 2, 5: 9}, 3: {1: 2, 5: 10}, 4: {1: 3, 5: 11}, 5: {1: 4, 5: 12}},
}
BASE_SP = 5
ENERGY = {101: {1: -19, 2: -20, 3: -21, 4: -23, 5: -25}}
ENERGY_DEFAULT = {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}
SUMMER_ENERGY = {601: -25, 602: -26, 603: -26, 604: -26}
WIT_ENERGY = 5

GEAR_BY_HALF_YEAR = {1: 3, 2: 6, 3: 10, 4: 16, 5: 20, 6: 25, 7: 30}
OVERDRIVE_TRAINING = 25

RESEARCH_SPLIT = {101: (M.SPEED, M.POWER, M.STAMINA), 105: (M.STAMINA, M.WIZ, M.GUTS),
                  102: (M.POWER, M.STAMINA, M.WIZ), 103: (M.GUTS, M.SPEED, M.POWER),
                  106: (M.WIZ, M.GUTS, M.SPEED)}
FACILITY_STATUS = {101: M.SPEED, 105: M.STAMINA, 102: M.POWER, 103: M.GUTS, 106: M.WIZ}
NPC_MIN = RF.NPC_BODY_MIN_ID
DECK_MAX = 100


def server_id(command_id: int) -> int:
    cid = int(command_id or 0)
    return SERVER_ID.get(cid, cid)


def base_of(command_id: int) -> int:
    cid = server_id(command_id)
    return SUMMER_TO_BASE.get(cid, cid)


def level_of(command_id: int, level: int) -> int:
    return 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))


def half_year(turn: int) -> int:
    return min(7, (max(1, int(turn or 1)) - 1) // 12 + 1)


def parts(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    command_id = server_id(command_id)
    base_cmd = base_of(command_id)
    base = RF.base_values(command_id, level, BASE, BASE_SP)
    training = mood_up = 0.0
    friendship = 1.0
    bonus = {tt: 0.0 for tt in base}
    nfriend = 0
    cards = RF.facility_cards(chara_info, partners)
    deck_ids = list(RF.deck(chara_info).values())
    for sid, bond in cards:
        lv = int((card_levels or {}).get(sid) or RF.max_level(sid))

        def eff(t, _s=sid, _b=bond, _l=lv):
            return RF.card_effect(_s, t, _l) + RF.unique_effect(_s, t, _b, _l, chara_info)

        training += eff(tf.EFFECT_TRAINING)
        mood_up += eff(tf.EFFECT_MOOD)
        for tt in bonus:
            et = tf.EFFECT_SKILL_POINT if tt == TARGET_SP else tf.EFFECT_STAT_BONUS_BASE + tt - 1
            bonus[tt] += eff(et)
        ds, dsp = tf.deck_type_stat_bonus(sid, bond, deck_ids, lv)
        for tt in bonus:
            bonus[tt] += dsp if tt == TARGET_SP else ds[tt - 1]
        kind, cmd, _r, _c = RF.card_meta(sid)
        if kind == 1 and cmd == base_cmd and bond >= RF.FRIENDSHIP_BOND:
            nfriend += 1
            friendship *= ((1 + RF.card_effect(sid, tf.EFFECT_FRIENDSHIP, lv) / 100)
                           * (1 + RF.unique_effect(sid, tf.EFFECT_FRIENDSHIP, bond, lv, chara_info) / 100))
    return {"base": base, "bonus": bonus, "training": training, "mood_up": mood_up,
            "friendship": friendship, "bodies": len(cards) + RF.npc_bodies(partners),
            "ncards": len(cards), "nfriend": nfriend, "growth": RF.growth(chara_info.get("card_id")),
            "base_cmd": base_cmd}


def home_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    p = parts(chara_info, command_id, level, partners, card_levels)
    return {tt: RF.capped(v) for tt, v in LF.product(chara_info, p).items()}


def base_energy(command_id: int, level: int) -> int:
    command_id = server_id(command_id)
    base_cmd = base_of(command_id)
    if base_cmd == 106:
        return WIT_ENERGY
    if command_id in SUMMER_ENERGY:
        return SUMMER_ENERGY[command_id]
    return ENERGY.get(base_cmd, ENERGY_DEFAULT).get(level_of(command_id, level), -20)


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> int:
    command_id = server_id(command_id)
    base = base_energy(command_id, level)
    if base < 0:
        mult = 1.0
        for f in LF.energy_down_factors(chara_info, partners, card_levels):
            mult *= f
        return -int(math.floor(-base * mult + 1e-9))
    base_cmd = base_of(command_id)
    total = 0.0
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if kind == 1 and cmd == base_cmd and bond >= RF.FRIENDSHIP_BOND:
            lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
            total += (max(0.0, RF.card_effect(sid, tf.EFFECT_WIT_RECOVERY, lv))
                      + tf.bond_gated_effect(sid, tf.EFFECT_WIT_RECOVERY, bond, lv))
    return base + int(round(total))


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict | None:
    command_id = server_id(command_id)
    line = home_line(chara_info, command_id, level, partners, card_levels)
    if not line:
        return None
    out = {}
    for tt, pre in line.items():
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = RF.displayed(chara_info, tt, pre)
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels)
    return out


def learning_boost(level: int, bonus_extra: int = 0) -> float:
    for lo, hi, base, bonus in M.learning_bonus():
        if lo <= int(level) <= hi:
            if not base:
                return 0.0
            return (bonus + bonus_extra) * (base + int(level)) / base
    return 0.0


def shown_bonus(chara_info: dict, target_type: int, home_pre: int, delta: int) -> int:
    return RF._shown_bonus(chara_info, target_type, home_pre, delta)


def scenario_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None, *,
                  mods: dict) -> list:
    p = parts(chara_info, command_id, level, partners, card_levels)
    home = {tt: RF.capped(v) for tt, v in LF.product(chara_info, p).items()}
    chips = mods.get("chips") or {}
    lb = learning_boost(int(mods.get("research_level") or 1), int(mods.get("lb_extra") or 0))
    gear = GEAR_BY_HALF_YEAR[half_year(mods.get("turn") or 1)] if mods.get("gear") else 0
    mult = (1 + lb / 100) * (1 + gear / 100)
    if p["nfriend"]:
        mult *= 1 + _chip_value(chips, M.CHIP_FRIENDSHIP, M.EFF_FRIENDSHIP) / 100
    od = bool(mods.get("overdrive"))
    effects = mods.get("core_effects") or ()
    total = int(mods.get("total_research") or 0)
    member = sum(v2 for t, _v1, v2, _v3, _v4 in effects if t == M.EFF_MEMBER) if od else 0
    out = []
    for tt in sorted(home):
        hp = home[tt]
        m = mult
        if tt == TARGET_SP:
            m *= 1 + _chip_value(chips, M.CHIP_SP, M.EFF_SP) / 100
        if od:
            stat = sum(M.boost(v4, total) for t, _v1, _v2, v3, v4 in effects
                       if t == M.EFF_STAT_BOOST and v3 == tt)
            m *= ((1 + OVERDRIVE_TRAINING / 100) * (1 + stat / 100)
                  * (1 + member * int(mods.get("members") or 0) / 100))
        value = min(PREVIEW_CAP, int(math.floor(round(hp * m, 3) + 1e-9)) - hp)
        if value > 0 and tt <= 5:
            value = shown_bonus(chara_info, tt, hp, value)
        if value > 0:
            out.append({"target_type": tt, "value": value})
    home_energy = int(mods.get("home_energy") or 0)
    down = -sum(v2 for t, _v1, v2, _v3, _v4 in effects if t == M.EFF_ENERGY_DOWN) if od else 0
    if down and home_energy < 0:
        out.append({"target_type": TARGET_ENERGY, "value": int(math.ceil(-home_energy * down / 100))})
    return out


def _chip_value(chips: dict, chip_id: int, effect_type: int) -> int:
    return sum(v2 for t, _v1, v2, _v3, _v4 in M.chip_effect(chip_id, int(chips.get(chip_id, 0)))
               if t == effect_type)


def research_points(command_id: int, members: int, *, gear: bool, friendship: bool, chips: dict,
                    head_pct: int, levels: dict, limit: int) -> list:
    base_cmd = base_of(command_id)
    base = (20 if command_id in SUMMER_TO_BASE else 10) + 5 * int(members)
    t0 = int(math.floor(base * (1.2 if gear else 1) + 1e-6))
    if friendship:
        main = int(math.floor(t0 * 0.7 + 0.5 + 1e-9)) + 4
        sub2 = int(math.floor(t0 * 0.1 + 0.5 + 1e-9))
        split = [main, t0 + 5 - main - sub2, sub2]
    else:
        sub1 = int(math.floor(t0 * 0.2 + 0.5 + 1e-9))
        sub2 = int(math.floor(t0 * 0.1 + 1e-9))
        split = [t0 - sub1 - sub2, sub1, sub2]
    out = []
    for status, v in zip(RESEARCH_SPLIT[base_cmd], split):
        pct = _chip_value(chips, M.RESEARCH_CHIP_OF[status], M.EFF_RESEARCH) + int(head_pct)
        if pct > 0:
            v = v + max(1, int(math.floor(v * pct / 100 + 1e-9)))
        out.append((status, max(0, min(v, int(limit) - int(levels.get(status, 1))))))
    return out
