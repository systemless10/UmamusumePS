
from __future__ import annotations

import functools
import math

from ... import master_data
from ... import training_formula as tf

TRAINING_COMMANDS = (101, 105, 102, 103, 106)
SUMMER_TO_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}
BASE_TO_SUMMER = {v: k for k, v in SUMMER_TO_BASE.items()}
STATS = ("speed", "stamina", "power", "guts", "wiz")
TARGET_SP = 30
TARGET_ENERGY = 10

BASE = {
    101: {1: {1: 11, 3: 2}, 2: {1: 12, 3: 2}, 3: {1: 13, 3: 2}, 4: {1: 14, 3: 3}, 5: {1: 15, 3: 4}},
    105: {1: {2: 10, 4: 3}, 2: {2: 11, 4: 4}, 3: {2: 12, 4: 4}, 4: {2: 13, 4: 5}, 5: {2: 14, 4: 6}},
    102: {1: {2: 6, 3: 9}, 2: {2: 6, 3: 10}, 3: {2: 6, 3: 11}, 4: {2: 7, 3: 12}, 5: {2: 8, 3: 13}},
    103: {1: {1: 2, 3: 2, 4: 11}, 2: {1: 2, 3: 2, 4: 12}, 3: {1: 2, 3: 2, 4: 13},
          4: {1: 3, 3: 3, 4: 14}, 5: {1: 3, 3: 3, 4: 15}},
    106: {1: {1: 3, 5: 7}, 2: {1: 3, 5: 8}, 3: {1: 3, 5: 9}, 4: {1: 4, 5: 10}, 5: {1: 5, 5: 11}},
}
BASE_SP = 7
ENERGY = {
    101: {1: -20, 2: -21, 3: -22, 4: -24, 5: -26},
    105: {1: -20, 2: -21, 3: -22, 4: -24, 5: -26},
    102: {1: -21, 2: -22, 3: -23, 4: -25, 5: -27},
    103: {1: -21, 2: -22, 3: -23, 4: -25, 5: -27},
    106: {1: 5, 2: 5, 3: 5, 4: 5, 5: 5},
}
SUMMER_ENERGY = {601: -25, 602: -26, 603: -27, 604: -27, 605: 5}
PREVIEW_CAP = 100
MOOD = {1: -20, 2: -10, 3: 0, 4: 10, 5: 20}
NPC_BODY_MIN_ID = 1000
FRIENDSHIP_BOND = 80

_EFFECT_COLUMNS = tf._EFFECT_LEVEL_COLUMNS


@functools.lru_cache(maxsize=1024)
def card_meta(support_card_id: int) -> tuple:
    row = master_data.query_one(
        "SELECT support_card_type, command_id, rarity, chara_id FROM support_card_data "
        "WHERE id=?", (int(support_card_id or 0),))
    if row is None:
        return (0, 0, 0, 0)
    return (int(row["support_card_type"] or 0), int(row["command_id"] or 0),
            int(row["rarity"] or 1), int(row["chara_id"] or 0))


def is_normal_card(support_card_id) -> bool:
    return card_meta(support_card_id)[0] == 1


def max_level(support_card_id) -> int:
    return {1: 40, 2: 45, 3: 50}.get(card_meta(support_card_id)[2], 50)


@functools.lru_cache(maxsize=8192)
def card_effect(support_card_id: int, effect_type: int, level: int) -> float:
    return tf.master_effect(support_card_id, effect_type, level)


def unique_effect(support_card_id: int, effect_type: int, bond: int, level: int,
                  chara_info: dict | None = None) -> float:
    total = tf.unique_effect(support_card_id, effect_type, level=level, bond=bond)
    if chara_info is not None:
        skills = [x.get("skill_id") for x in chara_info.get("skill_array") or ()]
        level_sum = sum(int(x.get("level") or 1)
                        for x in chara_info.get("training_level_info_array") or ())
        total += tf.situational_unique(support_card_id, effect_type, level,
                                       skills, level_sum)
        if effect_type == tf.EFFECT_TRAINING:
            total += tf._unique_conditional_training_bonus(
                support_card_id, tf.EFFECT_TRAINING_IF_DECK_TYPE_COUNT, level,
                deck_type_count=deck_type_count(chara_info))
    return total


def deck_type_count(chara_info: dict) -> int:
    kinds = set()
    for sid in deck(chara_info).values():
        kind, cmd, _r, _c = card_meta(sid)
        if kind:
            kinds.add((kind, cmd if kind == 1 else 0))
    return len(kinds)


@functools.lru_cache(maxsize=512)
def growth(card_id: int) -> tuple:
    row = master_data.query_one(
        "SELECT talent_speed, talent_stamina, talent_pow, talent_guts, talent_wiz "
        "FROM card_data WHERE id=?", (int(card_id or 0),))
    return tuple(int(row[i] or 0) for i in range(5)) if row else (0, 0, 0, 0, 0)


def base_values(command_id: int, level: int, table: dict | None = None,
                sp: int | None = None) -> dict:
    base = SUMMER_TO_BASE.get(command_id, command_id)
    lv = 5 if command_id in SUMMER_TO_BASE else max(1, min(5, int(level or 1)))
    out = dict((BASE if table is None else table).get(base, {}).get(lv, {}))
    if out:
        out[TARGET_SP] = BASE_SP if sp is None else sp
    return out


def energy_base(command_id: int, level: int) -> int:
    if command_id in SUMMER_ENERGY:
        return SUMMER_ENERGY[command_id]
    return ENERGY.get(command_id, {}).get(max(1, min(5, int(level or 1))), -20)


def deck(chara_info: dict) -> dict:
    return {int(c.get("position") or 0): int(c.get("support_card_id") or 0)
            for c in chara_info.get("support_card_array") or () if c.get("position")}


def bonds(chara_info: dict) -> dict:
    return {int(e.get("target_id") or 0): int(e.get("evaluation") or 0)
            for e in chara_info.get("evaluation_info_array") or () if isinstance(e, dict)}


def facility_cards(chara_info: dict, partners) -> list:
    by_pos, bond = deck(chara_info), bonds(chara_info)
    return [(by_pos[p], bond.get(p, 0)) for p in partners or () if p in by_pos]


def npc_bodies(partners) -> int:
    return sum(1 for p in partners or () if int(p) >= NPC_BODY_MIN_ID)


def is_friendship_training(chara_info: dict, command_id: int, partners) -> bool:
    base = SUMMER_TO_BASE.get(command_id, command_id)
    for sid, bond in facility_cards(chara_info, partners):
        kind, cmd, _r, _c = card_meta(sid)
        if kind == 1 and cmd == base and bond >= FRIENDSHIP_BOND:
            return True
    return False


def home_raw(chara_info: dict, command_id: int, level: int, partners,
             card_levels: dict | None = None, table: dict | None = None,
             sp: int | None = None, friend_fn=None) -> dict:
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    base = base_values(command_id, level, table, sp)
    if not base:
        return {}
    training = mood_up = 0.0
    friendship = 1.0
    bonus = {tt: 0.0 for tt in base}
    cards = facility_cards(chara_info, partners)
    for sid, bond in cards:
        lv = int((card_levels or {}).get(sid) or max_level(sid))

        def eff(t, _sid=sid, _bond=bond, _lv=lv):
            return card_effect(_sid, t, _lv) + unique_effect(_sid, t, _bond, _lv, chara_info)

        training += eff(tf.EFFECT_TRAINING)
        mood_up += eff(tf.EFFECT_MOOD)
        for tt in bonus:
            etype = tf.EFFECT_SKILL_POINT if tt == TARGET_SP else tf.EFFECT_STAT_BONUS_BASE + tt - 1
            bonus[tt] += eff(etype)
        deck_stats, deck_sp = tf.deck_type_stat_bonus(sid, bond, list(deck(chara_info).values()), lv)
        for tt in bonus:
            bonus[tt] += deck_sp if tt == TARGET_SP else deck_stats[tt - 1]
        kind, cmd, _r, _c = card_meta(sid)
        rainbow = (friend_fn(sid, kind, cmd, bond) if friend_fn is not None
                   else kind == 1 and cmd == base_cmd and bond >= FRIENDSHIP_BOND)
        if rainbow:
            friendship *= ((1 + card_effect(sid, tf.EFFECT_FRIENDSHIP, lv) / 100)
                           * (1 + unique_effect(sid, tf.EFFECT_FRIENDSHIP, bond, lv, chara_info) / 100))
    bodies = len(cards) + npc_bodies(partners)
    motivation = int(chara_info.get("motivation") or 3)
    mood = 1 + MOOD.get(motivation, 0) * (1 + mood_up / 100) / 100
    talents = growth(chara_info.get("card_id"))
    out = {}
    for tt, value in base.items():
        if not value:
            continue
        grow = 1 + talents[tt - 1] / 100 if tt <= 5 else 1.0
        out[tt] = ((value + bonus[tt]) * friendship * mood * (1 + training / 100)
                   * (1 + 0.05 * bodies) * grow)
    return out


def capped(raw: float) -> int:
    return min(PREVIEW_CAP, int(math.floor(raw + 1e-9)))


def displayed(chara_info: dict, target_type: int, gain: int) -> int:
    if target_type > 5:
        return gain
    stat = STATS[target_type - 1]
    cap_key = "max_wiz" if stat == "wiz" else f"max_{stat}"
    return tf._apply_stat_cap(gain, int(chara_info.get(stat) or 0),
                              int(chara_info.get(cap_key) or 9999))


def facility_energy(base_energy: int, command_id: int, cards, card_levels=None) -> int:
    levels = {sid: int((card_levels or {}).get(sid) or max_level(sid)) for sid, _b, _n in cards}
    cost = tf.energy_cost(base_energy, [sid for sid, _b, _n in cards],
                          {sid: b for sid, b, _n in cards}, levels)
    base_cmd = SUMMER_TO_BASE.get(command_id, command_id)
    if any(card_meta(sid)[0] == 1 and card_meta(sid)[1] == base_cmd and b >= FRIENDSHIP_BOND
           for sid, b, _n in cards):
        pct = sum(tf.friendship_energy_down(sid, levels[sid]) for sid, _b, _n in cards)
        cost = -int(math.floor(-cost * max(0.0, 1 - pct / 100) + 1e-9))
    return cost


def home_gains(chara_info: dict, command_id: int, level: int, partners,
               card_levels: dict | None = None) -> dict | None:
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    if not raw:
        return None
    out = {}
    for tt, value in raw.items():
        gain = displayed(chara_info, tt, capped(value))
        if tt == TARGET_SP:
            out["skill_point"] = gain
        else:
            out[STATS[tt - 1]] = gain
    cards = [(sid, bond, None) for sid, bond in facility_cards(chara_info, partners)]
    base_energy = energy_base(command_id, level)
    if base_energy < 0:
        out["energy_cost"] = facility_energy(base_energy, command_id, cards, card_levels)
    else:
        out["energy_cost"] = tf.training_energy(
            base_energy, SUMMER_TO_BASE.get(command_id, command_id), cards)
    return out


def bonus_params(chara_info: dict, command_id: int, level: int, partners,
                 s_pct: float, s_sp_pct: float, f_pct: float, limits: dict,
                 card_levels: dict | None = None) -> list:
    out = []
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    for tt in sorted(raw):
        s = s_sp_pct if tt == TARGET_SP else s_pct
        if not s and not f_pct:
            continue
        home_pre = capped(raw[tt])
        total_pre = int(math.floor(home_pre * (1 + s / 100) * (1 + f_pct / 100) + 1e-9))
        if total_pre <= home_pre:
            continue
        delta = min(total_pre - home_pre, PREVIEW_CAP + int(limits.get(tt, 0)))
        out.append({"target_type": tt,
                    "value": _shown_bonus(chara_info, tt, home_pre, delta)})
    return out


def _shown_bonus(chara_info: dict, target_type: int, home_pre: int, delta: int) -> int:
    if target_type > 5:
        return delta
    stat = STATS[target_type - 1]
    current = int(chara_info.get(stat) or 0)
    room_1200 = tf.MAX_STAT_1200_HALVING - current
    if room_1200 <= 0:
        shown = max(1, delta // 2)
    else:
        home_below = min(home_pre, room_1200)
        total_below = min(home_pre + delta, room_1200)
        whole = total_below - home_below
        rest = delta - whole
        shown = max(1, whole + (max(1, rest // 2) if rest > 0 else 0))
    cap_key = "max_wiz" if stat == "wiz" else f"max_{stat}"
    room = (int(chara_info.get(cap_key) or 9999) - current
            - displayed(chara_info, target_type, home_pre))
    return max(0, min(shown, room))
