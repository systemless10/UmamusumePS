
from __future__ import annotations

import math

from ... import training_formula as tf
from ..legend import formula as LF
from ..ramen import formula as RF
from . import master as M

TARGET_SP = RF.TARGET_SP
TARGET_ENERGY = RF.TARGET_ENERGY
STATS = RF.STATS
SUMMER_TO_BASE = RF.SUMMER_TO_BASE

BASE = {
    101: {1: {1: 10, 3: 3}, 2: {1: 11, 3: 3}, 3: {1: 12, 3: 3}, 4: {1: 13, 3: 4}, 5: {1: 14, 3: 5}},
    105: {1: {2: 9, 4: 4}, 2: {2: 10, 4: 4}, 3: {2: 11, 4: 4}, 4: {2: 12, 4: 5}, 5: {2: 13, 4: 6}},
    102: {1: {2: 5, 3: 11}, 2: {2: 5, 3: 12}, 3: {2: 5, 3: 13}, 4: {2: 6, 3: 14}, 5: {2: 7, 3: 15}},
    103: {1: {1: 3, 3: 2, 4: 10}, 2: {1: 3, 3: 2, 4: 11}, 3: {1: 3, 3: 2, 4: 12},
          4: {1: 4, 3: 2, 4: 13}, 5: {1: 4, 3: 3, 4: 14}},
    106: {1: {1: 2, 5: 9}, 2: {1: 2, 5: 10}, 3: {1: 2, 5: 11}, 4: {1: 3, 5: 12}, 5: {1: 4, 5: 13}},
}
EXPED_BASE = {1101: {1: 16, 3: 5}, 1102: {2: 15, 4: 7}, 1103: {2: 9, 3: 17},
              1104: {1: 6, 3: 5, 4: 16}, 1105: {1: 5, 5: 14}}
BASE_SP = 6
ENERGY = {101: {1: -21, 2: -22, 3: -23, 4: -25, 5: -27},
          105: {1: -19, 2: -20, 3: -21, 4: -23, 5: -25},
          102: {1: -20, 2: -21, 3: -22, 4: -24, 5: -26},
          103: {1: -21, 2: -22, 3: -23, 4: -25, 5: -27}}
EXPED_ENERGY = {1101: -21, 1102: -19, 1103: -20, 1104: -21}
WIT_ENERGY, EXPED_WIT_ENERGY = 5, 6

FRIEND_POTENTIAL = 8
ENERGY_POTENTIAL = 7
EFFECT_FLAT, EFFECT_TRAINING, EFFECT_FRIEND = 1, 2, 3


def base_of(command_id: int) -> int:
    cid = int(command_id or 0)
    return M.EXPED.get(cid, SUMMER_TO_BASE.get(cid, cid))


def is_exped(command_id: int) -> bool:
    return int(command_id or 0) in M.EXPED


def base_values(command_id: int, level: int) -> dict:
    cid = int(command_id or 0)
    if cid in EXPED_BASE:
        out = dict(EXPED_BASE[cid])
    else:
        lv = max(1, min(5, int(level or 1)))
        out = dict(BASE.get(base_of(cid), {}).get(lv, {}))
    if out:
        out[TARGET_SP] = BASE_SP
    return out


def friendship_card(chara_info: dict, command_id: int, sid: int, bond: int) -> bool:
    kind, cmd, _r, _c = RF.card_meta(sid)
    return kind == 1 and cmd == base_of(command_id) and bond >= RF.FRIENDSHIP_BOND


def parts(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    base = base_values(command_id, level)
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
        if friendship_card(chara_info, command_id, sid, bond):
            nfriend += 1
            friendship *= ((1 + RF.card_effect(sid, tf.EFFECT_FRIENDSHIP, lv) / 100)
                           * (1 + RF.unique_effect(sid, tf.EFFECT_FRIENDSHIP, bond, lv, chara_info) / 100))
    return {"base": base, "bonus": bonus, "training": training, "mood_up": mood_up,
            "friendship": friendship, "bodies": len(cards) + RF.npc_bodies(partners),
            "ncards": len(cards), "nfriend": nfriend, "growth": RF.growth(chara_info.get("card_id"))}


def home_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    p = parts(chara_info, command_id, level, partners, card_levels)
    return {tt: RF.capped(v) for tt, v in LF.product(chara_info, p).items()}


def base_energy(command_id: int, level: int) -> int:
    cid = int(command_id or 0)
    if cid == 1105:
        return EXPED_WIT_ENERGY
    if cid in EXPED_ENERGY:
        return EXPED_ENERGY[cid]
    base = base_of(cid)
    if base == 106:
        return WIT_ENERGY
    return ENERGY.get(base, ENERGY[101]).get(max(1, min(5, int(level or 1))), -21)


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> int:
    base = base_energy(command_id, level)
    if base < 0:
        mult = 1.0
        for f in LF.energy_down_factors(chara_info, partners, card_levels):
            mult *= f
        return -int(math.floor(-base * mult + 1e-9))
    total = 0.0
    for sid, bond in RF.facility_cards(chara_info, partners):
        if friendship_card(chara_info, command_id, sid, bond):
            lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
            total += (max(0.0, RF.card_effect(sid, tf.EFFECT_WIT_RECOVERY, lv))
                      + tf.bond_gated_effect(sid, tf.EFFECT_WIT_RECOVERY, bond, lv))
    return base + int(round(total))


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict | None:
    line = home_line(chara_info, command_id, level, partners, card_levels)
    if not line:
        return None
    out = {}
    for tt, pre in line.items():
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = RF.displayed(chara_info, tt, pre)
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels)
    return out


def training_eff(approval_rate: int) -> int:
    pct = int(approval_rate) // 10
    for lo, hi, e in M.training_eff_rows():
        if lo <= pct <= hi:
            return e
    return 0


def flat_bonus(levels: dict, target: int) -> int:
    return sum(v2 for pid, plv, et, v1, v2, _v3, _v4 in M.potential_bonus()
               if et == EFFECT_FLAT and v1 == target and int(levels.get(pid, 0)) >= plv)


def extra_pct(levels: dict, command_id: int) -> int:
    base, exped = base_of(command_id), is_exped(command_id)
    return sum(v2 for pid, plv, et, v1, v2, v3, _v4 in M.potential_bonus()
               if et == EFFECT_TRAINING and int(levels.get(pid, 0)) >= plv
               and v1 in (0, base) and (not v3 or exped))


def scenario_delta(home_pre: int, target: int, approval_rate: int, levels: dict,
                   command_id: int, friendship: bool) -> int:
    e = training_eff(approval_rate) + extra_pct(levels, command_id)
    f = 120 if (friendship and int(levels.get(FRIEND_POTENTIAL, 0)) >= 3) else 100
    fl = flat_bonus(levels, target)
    return min(RF.PREVIEW_CAP, (home_pre + fl) * (100 + e) * f // 10000 - home_pre)


def energy_refund(home_energy: int, levels: dict, command_id: int) -> int:
    if is_exped(command_id) and int(levels.get(ENERGY_POTENTIAL, 0)) >= 3 and home_energy < 0:
        return -(home_energy // 5)
    return 0


def scenario_line(chara_info: dict, command_id: int, level: int, partners, card_levels,
                  approval_rate: int, levels: dict, home_energy: int) -> list:
    p = parts(chara_info, command_id, level, partners, card_levels)
    home = {tt: RF.capped(v) for tt, v in LF.product(chara_info, p).items()}
    out = []
    for tt in sorted(home):
        pre = home[tt]
        d = scenario_delta(pre, tt, approval_rate, levels, command_id, p["nfriend"] > 0)
        if d > 0 and tt <= 5:
            d = RF._shown_bonus(chara_info, tt, pre, d)
        if d > 0:
            out.append({"target_type": tt, "value": d})
    refund = energy_refund(home_energy, levels, command_id)
    if refund:
        out.append({"target_type": TARGET_ENERGY, "value": refund})
    return out


def global_exp(chara_info: dict, command_id: int, partners) -> int:
    if not is_exped(command_id):
        return 0
    rainbow = sum(1 for sid, bond in RF.facility_cards(chara_info, partners)
                  if friendship_card(chara_info, command_id, sid, bond))
    return (30 if int(command_id) == 1105 else 50) + 20 * len(list(partners or ())) + 20 * rainbow


def rainbow_count(chara_info: dict, command_id: int, partners) -> int:
    return sum(1 for sid, bond in RF.facility_cards(chara_info, partners)
               if friendship_card(chara_info, command_id, sid, bond))
