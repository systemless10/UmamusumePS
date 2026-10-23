
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
FRIENDSHIP_BOND = RF.FRIENDSHIP_BOND
GROUP_TYPE = 3
NPC_MIN = RF.NPC_BODY_MIN_ID

BASE = {
    101: {1: {1: 11, 3: 2}, 2: {1: 12, 3: 2}, 3: {1: 13, 3: 2}, 4: {1: 14, 3: 3}, 5: {1: 15, 3: 4}},
    105: {1: {2: 8, 4: 6}, 2: {2: 9, 4: 6}, 3: {2: 10, 4: 6}, 4: {2: 11, 4: 7}, 5: {2: 12, 4: 8}},
    102: {1: {2: 4, 3: 10}, 2: {2: 4, 3: 11}, 3: {2: 4, 3: 12}, 4: {2: 5, 3: 13}, 5: {2: 6, 3: 14}},
    103: {1: {1: 2, 3: 2, 4: 10}, 2: {1: 2, 3: 2, 4: 11}, 3: {1: 2, 3: 2, 4: 12},
          4: {1: 3, 3: 2, 4: 13}, 5: {1: 3, 3: 3, 4: 14}},
    106: {1: {1: 3, 5: 7}, 2: {1: 3, 5: 8}, 3: {1: 3, 5: 9}, 4: {1: 4, 5: 10}, 5: {1: 5, 5: 11}},
}
ENERGY = {101: {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}}
ENERGY_DEFAULT = {1: -21, 2: -22, 3: -23, 4: -25, 5: -27}
SUMMER_ENERGY = {601: -26, 602: -27, 603: -27, 604: -27}
WIT_ENERGY = 5
SUPER_MOOD = 55
ZONE_TRAINING = 30
ZONE_ENERGY_DOWN = 50
ZONE_END_STEP = {5: 30, 6: 60}
PRACTICE_PERFECT = {10: 2, 11: 4}


def sp_of(command_id: int) -> int:
    return 5 if SUMMER_TO_BASE.get(command_id, command_id) == 106 else 7


def level_of(command_id: int, level: int) -> int:
    return 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))


def friend_fn(chara_info: dict, command_id: int):
    base = SUMMER_TO_BASE.get(command_id, command_id)
    passion = M.PASSION in set(chara_info.get("chara_effect_id_array") or ())

    def fn(sid, kind, cmd, bond):
        if kind == 1:
            return cmd == base and bond >= FRIENDSHIP_BOND
        return kind == GROUP_TYPE and passion
    return fn


def parts(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    base = RF.base_values(command_id, level, BASE, sp_of(command_id))
    training = mood_up = 0.0
    friendship = 1.0
    bonus = {tt: 0.0 for tt in base}
    nfriend = 0
    fn = friend_fn(chara_info, command_id)
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
        if fn(sid, kind, cmd, bond):
            nfriend += 1
            friendship *= ((1 + RF.card_effect(sid, tf.EFFECT_FRIENDSHIP, lv) / 100)
                           * (1 + RF.unique_effect(sid, tf.EFFECT_FRIENDSHIP, bond, lv, chara_info) / 100))
    return {"base": base, "bonus": bonus, "training": training, "mood_up": mood_up,
            "friendship": friendship, "bodies": len(cards) + RF.npc_bodies(partners),
            "ncards": len(cards), "nfriend": nfriend, "growth": RF.growth(chara_info.get("card_id")),
            "base_cmd": base_cmd}


def product(chara_info: dict, p: dict, *, mood_val=None, tu: float = 0.0, mu: float = 0.0,
            fb: float = 0.0) -> dict:
    mot = int(chara_info.get("motivation") or 3)
    mv = RF.MOOD.get(mot, 0) if mood_val is None else mood_val
    mood = 1 + mv * (1 + (p["mood_up"] + mu) / 100) / 100
    fr = p["friendship"] * (1 + fb / 100)
    out = {}
    for tt, v in p["base"].items():
        if not v:
            continue
        g = 1 + p["growth"][tt - 1] / 100 if tt <= 5 else 1
        out[tt] = ((v + p["bonus"][tt]) * fr * mood * (1 + (p["training"] + tu) / 100)
                   * (1 + 0.05 * p["bodies"]) * g)
    return out


def home_line(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
              *, super_mood: bool = False) -> dict:
    p = parts(chara_info, command_id, level, partners, card_levels)
    raw = product(chara_info, p, mood_val=SUPER_MOOD if super_mood else None)
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


def base_energy(command_id: int, level: int) -> int:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    if base_cmd == 106:
        return WIT_ENERGY
    if command_id in SUMMER_ENERGY:
        return SUMMER_ENERGY[command_id]
    return ENERGY.get(base_cmd, ENERGY_DEFAULT).get(level_of(command_id, level), -21)


def energy(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
           down: int = 0) -> int:
    base = base_energy(command_id, level)
    if base < 0:
        mult = 1.0
        for f in energy_down_factors(chara_info, partners, card_levels):
            mult *= f
        return -int(math.floor(-base * mult * max(0.0, 1 - down / 100) + 1e-9))
    fn = friend_fn(chara_info, command_id)
    total = 0.0
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if fn(sid, kind, cmd, bond):
            lv = int((card_levels or {}).get(sid) or RF.max_level(sid))
            total += (max(0.0, RF.card_effect(sid, tf.EFFECT_WIT_RECOVERY, lv))
                      + tf.bond_gated_effect(sid, tf.EFFECT_WIT_RECOVERY, bond, lv))
    return base + int(round(total))


def home_gains(chara_info: dict, command_id: int, level: int, partners, card_levels=None,
               *, super_mood: bool = False, energy_down: int = 0) -> dict | None:
    line = home_line(chara_info, command_id, level, partners, card_levels, super_mood=super_mood)
    if not line:
        return None
    out = {}
    for tt, pre in line.items():
        shown = RF.displayed(chara_info, tt, pre)
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = shown
    out["energy_cost"] = energy(chara_info, command_id, level, partners, card_levels, energy_down)
    return out


def bonus_params(chara_info: dict, command_id: int, level: int, partners, card_levels=None, *,
                 mods: dict) -> list:
    p = parts(chara_info, command_id, level, partners, card_levels)
    super_mood = bool(mods.get("super"))
    home = product(chara_info, p, mood_val=SUPER_MOOD if super_mood else None)
    nfriend = p["nfriend"] + int(mods.get("nfriend_extra") or 0)
    fb = float(mods.get("fb") or 0) + float(mods.get("fb_per_friend") or 0) * p["nfriend"]
    total = product(chara_info, p, mood_val=SUPER_MOOD if super_mood else None,
                    tu=float(mods.get("tu") or 0)
                    + float(mods.get("tu_per_sup") or 0) * (p["ncards"] + guests_in(partners)),
                    mu=float(mods.get("mu") or 0)
                    + float(mods.get("mu_per_sup") or 0) * (p["ncards"] + guests_in(partners)),
                    fb=fb if nfriend else 0.0)
    out = []
    for tt in sorted(home):
        hp = RF.capped(home[tt])
        tp = int(math.floor(total[tt] + 1e-9))
        if tp <= hp:
            continue
        shown = RF._shown_bonus(chara_info, tt, hp, min(RF.PREVIEW_CAP, tp - hp))
        if shown > 0:
            out.append({"target_type": tt, "value": shown})
    return out


def guests_in(partners) -> int:
    return sum(1 for q in partners or () if int(q) >= NPC_MIN)


def is_friendship_training(chara_info: dict, command_id: int, partners,
                           full_guests: int = 0) -> bool:
    if full_guests:
        return True
    fn = friend_fn(chara_info, command_id)
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if fn(sid, kind, cmd, bond):
            return True
    return False


def plain_failure_rate(chara_info: dict, command_id: int, level: int, partners) -> int:
    from ...handlers import single_mode_team as smt
    conds = [c for c in chara_info.get("chara_effect_id_array") or () if c not in PRACTICE_PERFECT]
    return tf.failure_rate(int(chara_info.get("vital") or 0), command_id=command_id,
                           facility_level=level_of(command_id, level), conditions=conds,
                           facility_cards=smt._facility_failure_cards(chara_info, partners))


def zone_end_rate(chara_info: dict, command_id: int, level: int, partners, n: int) -> int:
    step = 100 if n >= 7 else ZONE_END_STEP.get(n, 0)
    return min(100, plain_failure_rate(chara_info, command_id, level, partners) + step)
