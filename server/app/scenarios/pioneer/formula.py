
from __future__ import annotations

import math
import random

from ... import training_formula as tf
from ..onsen import formula as OF
from ..ramen import formula as RF
from . import master as M

TRAINING_COMMANDS = RF.TRAINING_COMMANDS
STATS = RF.STATS
TARGET_SP = RF.TARGET_SP
TARGET_ENERGY = RF.TARGET_ENERGY
PREVIEW_CAP = RF.PREVIEW_CAP
_SUMMER_ID = RF.BASE_TO_SUMMER

BASE = {
    101: {1: {1: 12, 3: 1}, 2: {1: 13, 3: 1}, 3: {1: 14, 3: 1}, 4: {1: 15, 3: 2}, 5: {1: 16, 3: 3}},
    105: {1: {2: 9, 4: 5}, 2: {2: 10, 4: 5}, 3: {2: 11, 4: 5}, 4: {2: 12, 4: 6}, 5: {2: 13, 4: 7}},
    102: {1: {2: 3, 3: 11}, 2: {2: 3, 3: 12}, 3: {2: 4, 3: 13}, 4: {2: 4, 3: 14}, 5: {2: 5, 3: 15}},
    103: {1: {1: 2, 3: 2, 4: 10}, 2: {1: 2, 3: 2, 4: 11}, 3: {1: 2, 3: 2, 4: 12},
          4: {1: 3, 3: 2, 4: 13}, 5: {1: 4, 3: 3, 4: 14}},
    106: {1: {1: 5, 5: 8}, 2: {1: 5, 5: 9}, 3: {1: 5, 5: 10}, 4: {1: 6, 5: 11}, 5: {1: 7, 5: 12}},
}
BASE_SP = 6
ENERGY = {1: -20, 2: -21, 3: -22, 4: -24, 5: -26}
SPEED_LV5_ENERGY = -25
WIT_ENERGY = 5

W = {
    101: {1: 0.60, 3: 0.40, 30: 0.485},
    102: {2: 0.48, 4: 0.32, 30: 0.695},
    103: {2: 0.28, 3: 0.595, 30: 0.685},
    104: {1: 0.30, 3: 0.195, 4: 0.50, 30: 0.69},
    105: {1: 0.43, 5: 1.00, 30: 0.695},
    106: {1: 0.19, 2: 0.14, 3: 0.20, 4: 0.11, 5: 0.12, 30: 0.68},
}
CARD_BODY, ISLANDER_BODY, BEACH_BODY = 0.05, 0.02, 0.01
FRIEND_PARTICIPANT = 0.05
ISLAND_STATS = (1, 2, 3, 4, 5, 30)
BEACH_CAP = 5

PT_BASE, PT_PER_TRAINING, PT_PER_ISLAND = 60, 6, 3
PT_RAINBOW = 20
RACE_PT_BASE, GOAL_RACE_EXTRA = 20, 40


def displayed(chara_info: dict, target_type: int, gain: int) -> int:
    return RF.displayed(chara_info, target_type, gain)


def card_level(chara_info: dict, sid: int, card_levels: dict | None) -> int:
    lv = (card_levels or {}).get(sid)
    if lv:
        return int(lv)
    for c in chara_info.get("support_card_array") or ():
        if int(c.get("support_card_id") or 0) == int(sid):
            try:
                got = tf.card_level(sid, c.get("exp") or 0, c.get("limit_break_count"))
                if got:
                    return int(got)
            except Exception:
                pass
    return RF.max_level(sid)


def is_rainbow(chara_info: dict, position: int, facility_command: int) -> bool:
    sid = RF.deck(chara_info).get(int(position))
    if not sid:
        return False
    kind, cmd, _r, _c = RF.card_meta(sid)
    return (kind == 1 and cmd == facility_command
            and RF.bonds(chara_info).get(int(position), 0) >= RF.FRIENDSHIP_BOND)


def bodies(chara_info: dict, partners) -> int:
    by_pos = RF.deck(chara_info)
    return sum(1 for p in partners or () if int(p) in by_pos or int(p) >= RF.NPC_BODY_MIN_ID)


def deck_levels(chara_info: dict, card_levels=None) -> dict:
    return {sid: card_level(chara_info, sid, card_levels) for sid in RF.deck(chara_info).values()}


def _energy(chara_info: dict, base_cmd: int, base_energy: int, partners, card_levels=None) -> int:
    if base_energy < 0:
        mult = 1.0
        for f in OF.energy_down_factors(chara_info, partners, deck_levels(chara_info, card_levels)):
            mult *= f
        return -int(math.floor(-base_energy * mult + 1e-9))
    if base_cmd != 106:
        return base_energy
    levels = deck_levels(chara_info, card_levels)
    extra = 0.0
    for sid, bond in RF.facility_cards(chara_info, partners):
        kind, cmd, _r, _c = RF.card_meta(sid)
        if kind == 1 and cmd == 106 and bond >= RF.FRIENDSHIP_BOND:
            extra += (max(0.0, RF.card_effect(sid, tf.EFFECT_WIT_RECOVERY, levels.get(sid)))
                      + tf.bond_gated_effect(sid, tf.EFFECT_WIT_RECOVERY, bond))
    return base_energy + int(round(extra))


def _gains_from_raw(chara_info: dict, raw: dict) -> dict:
    out = {}
    for tt, value in raw.items():
        if tt == TARGET_ENERGY:
            continue
        gain = displayed(chara_info, tt, RF.capped(value))
        out["skill_point" if tt == TARGET_SP else STATS[tt - 1]] = gain
    return out


def bonus_line(chara_info: dict, raw: dict, pct_for) -> list:
    out = []
    for tt in raw:
        if tt == TARGET_ENERGY:
            continue
        pct = pct_for(tt)
        if not pct:
            continue
        home_pre = RF.capped(raw[tt])
        total = int(math.floor(home_pre * (1 + pct / 100) + 1e-9))
        if total <= home_pre:
            continue
        delta = min(total - home_pre, PREVIEW_CAP)
        out.append({"target_type": tt, "value": RF._shown_bonus(chara_info, tt, home_pre, delta)})
    return out


def home_raw(chara_info: dict, command_id: int, level: int, partners, card_levels=None) -> dict:
    return RF.home_raw(chara_info, command_id, level, partners, card_levels,
                       table=BASE, sp=BASE_SP)


def home_gains(chara_info: dict, command_id: int, level: int, partners,
               card_levels=None) -> dict | None:
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    if not raw:
        return None
    out = _gains_from_raw(chara_info, raw)
    lv = max(1, min(5, int(level or 1)))
    base_energy = (WIT_ENERGY if command_id == 106 else
                   SPEED_LV5_ENERGY if command_id == 101 and lv == 5 else ENERGY[lv])
    out["energy_cost"] = _energy(chara_info, command_id, base_energy, partners, card_levels)
    return out


def training_bonus(chara_info: dict, command_id: int, level: int, partners, te: int,
                   card_levels=None) -> list:
    if not te:
        return []
    raw = home_raw(chara_info, command_id, level, partners, card_levels)
    return bonus_line(chara_info, raw, lambda tt: te)


def island_base(island_cmd: int, facilities: dict) -> list:
    base_cmd = M.ISLAND_TO_TRAINING[island_cmd]
    fac = M.TRAINING_TO_FACILITY[base_cmd]
    built = M.level_of(facilities.get(fac, 0)) > 0
    rows = M.shima_effect().get((island_cmd, 2, fac if built else 0), [])
    flat = flat_bonuses(facilities)
    return [(tt, v + (flat.get(tt, 0) if tt != TARGET_ENERGY else 0)) for tt, v in rows]


def flat_bonuses(facilities: dict) -> dict:
    out: dict = {}
    for state in facilities.values():
        for t, _v1, v2, v3, _v4 in M.facility_effects().get(int(state), ()):
            if t == M.EFFECT_FLAT:
                out[v3] = out.get(v3, 0) + v2
    return out


def facility_rows(facilities: dict, effect_type: int, period=None, v3=None) -> list:
    out = []
    for state in facilities.values():
        for t, v1, v2, e3, v4 in M.facility_effects().get(int(state), ()):
            if t != effect_type:
                continue
            if period is not None and v1 not in (period if isinstance(period, tuple) else (period,)):
                continue
            if v3 is not None and e3 != v3:
                continue
            out.append((t, v1, v2, e3, v4))
    return out


def beach_level(facilities: dict) -> int:
    return M.level_of(facilities.get(M.BEACH_HOUSE, 0))


def island_raw(chara_info: dict, island_cmd: int, partners, facilities: dict,
               card_levels=None) -> tuple:
    base_cmd = M.ISLAND_TO_TRAINING[island_cmd]
    rows = island_base(island_cmd, facilities)
    order = [tt for tt, _v in rows if tt != TARGET_ENERGY]
    energy = next((v for tt, v in rows if tt == TARGET_ENERGY), -15)
    stats = {tt: v for tt, v in rows if tt not in (TARGET_ENERGY, TARGET_SP)}
    sp = next((v for tt, v in rows if tt == TARGET_SP), 0)
    raw = RF.home_raw(chara_info, _SUMMER_ID[base_cmd], 5, partners, card_levels,
                      table={base_cmd: {5: stats}}, sp=sp)
    if beach_level(facilities) >= 1:
        rainbows = sum(1 for p in partners or () if is_rainbow(chara_info, p, base_cmd))
        raw = {tt: v * (1 + FRIEND_PARTICIPANT * rainbows) for tt, v in raw.items()}
    return raw, order, energy


def island_gains(chara_info: dict, island_cmd: int, partners, facilities: dict,
                 card_levels=None) -> dict:
    raw, _order, energy = island_raw(chara_info, island_cmd, partners, facilities, card_levels)
    out = _gains_from_raw(chara_info, raw)
    out["energy_cost"] = _energy(chara_info, M.ISLAND_TO_TRAINING[island_cmd], energy, partners,
                                 card_levels)
    return out


def island_params(chara_info: dict, island_cmd: int, gains: dict, facilities: dict) -> list:
    _rows = island_base(island_cmd, facilities)
    out = []
    for tt, _v in _rows:
        if tt == TARGET_ENERGY:
            continue
        key = "skill_point" if tt == TARGET_SP else STATS[tt - 1]
        out.append({"target_type": tt, "value": int(gains.get(key, 0))})
    out.append({"target_type": TARGET_ENERGY, "value": int(gains.get("energy_cost", -15))})
    return out


def island_bonus(chara_info: dict, island_cmd: int, partners, facilities: dict, period: int,
                 card_levels=None) -> list:
    raw, order, _e = island_raw(chara_info, island_cmd, partners, facilities, card_levels)
    pct = sum(v2 for _t, _v1, v2, _v3, _v4 in facility_rows(
        facilities, M.EFFECT_TRAINING, period=period, v3=island_cmd))
    return bonus_line(chara_info, {tt: raw[tt] for tt in order if tt in raw}, lambda tt: pct)


def _card(chara_info: dict, position: int, card_levels) -> dict:
    by_pos = RF.deck(chara_info)
    sid = by_pos[position]
    lv = card_level(chara_info, sid, card_levels)
    bond = RF.bonds(chara_info).get(position, 0)
    kind, cmd, _r, _c = RF.card_meta(sid)

    def eff(t):
        return RF.card_effect(sid, t, lv) + RF.unique_effect(sid, t, bond, lv, chara_info)

    sb = {st: eff(tf.EFFECT_SKILL_POINT if st == TARGET_SP else tf.EFFECT_STAT_BONUS_BASE + st - 1)
          for st in ISLAND_STATS}
    deck_stats, deck_sp = tf.deck_type_stat_bonus(sid, bond, list(by_pos.values()), lv)
    for i in range(5):
        sb[i + 1] += deck_stats[i]
    sb[TARGET_SP] += deck_sp
    friendship = ((1 + RF.card_effect(sid, tf.EFFECT_FRIENDSHIP, lv) / 100)
                  * (1 + RF.unique_effect(sid, tf.EFFECT_FRIENDSHIP, bond, lv, chara_info) / 100) - 1) * 100
    return {"kind": kind, "cmd": cmd, "bond": bond, "train": eff(tf.EFFECT_TRAINING),
            "mood": eff(tf.EFFECT_MOOD), "friendship": friendship, "sb": sb}


def island_base_values(facilities: dict) -> dict:
    base = {tt: v for tt, v in M.shima_effect().get((M.ISLAND_TRAINING, 2, 0), ())
            if tt != TARGET_ENERGY}
    for tt, v in flat_bonuses(facilities).items():
        base[tt] = base.get(tt, 0) + v
    return base


def island_training_raw(chara_info: dict, partner_map: dict, facilities: dict,
                        card_levels=None) -> dict:
    by_pos = RF.deck(chara_info)
    base = island_base_values(facilities)
    m = RF.MOOD.get(int(chara_info.get("motivation") or 3), 0) / 100
    talents = RF.growth(chara_info.get("card_id"))
    body = 1.0
    sb = {st: 0.0 for st in ISLAND_STATS}
    terms = []
    rainbows = 0
    for fac, ids in partner_map.items():
        cards = [_card(chara_info, p, card_levels) for p in ids if p in by_pos]
        islanders = sum(1 for p in ids if int(p) >= RF.NPC_BODY_MIN_ID)
        for c in cards:
            for st in ISLAND_STATS:
                sb[st] += c["sb"][st]
        if fac == M.BEACH_HOUSE:
            body += BEACH_BODY * len(cards)
        else:
            body += CARD_BODY * len(cards) + ISLANDER_BODY * islanders
        train = sum(c["train"] for c in cards) / 100
        mood = (1 + m * (1 + sum(c["mood"] for c in cards) / 100)) / (1 + m) - 1
        fr = 1.0
        if fac != M.BEACH_HOUSE:
            own = M.FACILITY_TO_TRAINING.get(fac)
            for c in cards:
                if c["kind"] == 1 and c["cmd"] == own and c["bond"] >= RF.FRIENDSHIP_BOND:
                    fr *= 1 + c["friendship"] / 100
                    rainbows += 1
        terms.append((fac, train, mood, fr))
    beach = 1 + FRIEND_PARTICIPANT * rainbows if beach_level(facilities) >= 1 else 1.0
    out = {}
    for st in ISLAND_STATS:
        s_train = s_mood = 0.0
        p_fr = 1.0
        for fac, train, mood, fr in terms:
            w = W.get(fac, {}).get(st, 0.0)
            s_train += w * train
            s_mood += w * mood
            p_fr *= 1 + w * (fr - 1)
        grow = 1 + talents[st - 1] / 100 if st <= 5 else 1.0
        out[st] = ((base.get(st, 0) + sb[st]) * (1 + m) * grow * body
                   * (1 + s_train) * (1 + s_mood) * p_fr * beach)
    return out


def island_training_gains(chara_info: dict, partner_map: dict, facilities: dict,
                          card_levels=None) -> dict:
    raw = island_training_raw(chara_info, partner_map, facilities, card_levels)
    out = _gains_from_raw(chara_info, raw)
    out["energy_cost"] = 0
    return out


def island_training_params(gains: dict) -> list:
    out = [{"target_type": tt, "value": int(gains.get("skill_point" if tt == TARGET_SP
                                                      else STATS[tt - 1], 0))}
           for tt in ISLAND_STATS]
    out.append({"target_type": TARGET_ENERGY, "value": 0})
    return out


def island_training_bonus(chara_info: dict, partner_map: dict, facilities: dict,
                          card_levels=None) -> list:
    raw = island_training_raw(chara_info, partner_map, facilities, card_levels)
    fac_pct = {st: 0 for st in ISLAND_STATS}
    for fac, stats in M.FACILITY_STATS.items():
        pct = sum(v2 for _t, v1, v2, v3, _v4 in M.facility_effects().get(
            int(facilities.get(fac, 0)), ()) if _t == M.EFFECT_TRAINING and v1 == M.PERIOD_ISLAND)
        for st in stats:
            fac_pct[st] += pct
    beach = sum(v2 for _t, v1, v2, _v3, _v4 in M.facility_effects().get(
        int(facilities.get(M.BEACH_HOUSE, 0)), ()) if _t == M.EFFECT_TRAINING and v1 == 0)
    rb_facilities = sum(1 for fac, ids in partner_map.items() if fac != M.BEACH_HOUSE and any(
        is_rainbow(chara_info, p, M.FACILITY_TO_TRAINING[fac]) for p in ids))
    multi = max((v2 for _t, v1, v2, v3, _v4 in M.facility_effects().get(
        int(facilities.get(M.BEACH_HOUSE, 0)), ())
        if _t == M.EFFECT_MULTI_RAINBOW and v1 == 0 and v3 <= rb_facilities), default=0)
    return bonus_line(chara_info, {st: raw[st] for st in ISLAND_STATS},
                      lambda st: fac_pct.get(st, 0) + beach + multi)


def island_partners(chara_info: dict, home_commands, facilities: dict, team, rng: random.Random) -> dict:
    by_pos = RF.deck(chara_info)
    standing: dict = {}
    for c in home_commands or ():
        cmd = int(c.get("command_id") or 0)
        if c.get("command_type") != 1 or cmd not in M.TRAINING_TO_FACILITY:
            continue
        for p in c.get("training_partner_array") or ():
            p = int(p)
            if p in by_pos or p >= RF.NPC_BODY_MIN_ID:
                standing.setdefault(p, M.TRAINING_TO_FACILITY[cmd])
    placed: dict = {}
    homeless_cards, homeless_islanders = [], []
    for p in sorted(standing):
        fac = standing[p]
        if M.level_of(facilities.get(fac, 0)) > 0:
            placed.setdefault(fac, []).append(p)
        elif p in by_pos:
            homeless_cards.append(p)
        else:
            homeless_islanders.append(p)
    if M.level_of(facilities.get(M.BEACH_HOUSE, 0)) > 0:
        homeless_cards += [p for p in sorted(by_pos) if p not in standing]
        away = [c for c in team or () if int(c) not in standing]
        beach = []
        if len(homeless_cards) > BEACH_CAP:
            keep = [p for p in homeless_cards if p >= 6 or RF.card_meta(by_pos[p])[0] != 1]
            rest = [p for p in homeless_cards if p not in keep]
            rng.shuffle(rest)
            homeless_cards = sorted((keep + rest)[:BEACH_CAP])
        beach += homeless_cards
        for pool in (homeless_islanders, away):
            pool = list(pool)
            rng.shuffle(pool)
            beach += pool[:max(0, BEACH_CAP - len(beach))]
        if beach:
            placed[M.BEACH_HOUSE] = sorted(beach)
    order = sorted(placed, key=lambda f: min(placed[f]))
    return {f: placed[f] for f in order}


def training_points(chara_info: dict, partners, command_id: int, cp_pct: int) -> int:
    n = bodies(chara_info, partners)
    base_cmd = M.ISLAND_TO_TRAINING.get(command_id, command_id)
    rainbow = any(is_rainbow(chara_info, p, base_cmd) for p in partners or ())
    return (PT_BASE + PT_PER_TRAINING * n) * (100 + cp_pct + (PT_RAINBOW if rainbow else 0)) // 100


def island_training_points(chara_info: dict, partner_map: dict, cp_pct: int) -> int:
    n = sum(bodies(chara_info, ids) for ids in partner_map.values())
    rainbow = any(is_rainbow(chara_info, p, M.FACILITY_TO_TRAINING[fac])
                  for fac, ids in partner_map.items() if fac != M.BEACH_HOUSE
                  for p in ids)
    return (PT_BASE + PT_PER_ISLAND * n) * (100 + cp_pct + (PT_RAINBOW if rainbow else 0)) // 100


def race_points(cp_pct: int, goal: bool, beach_race_pct: int = 0) -> int:
    pts = RACE_PT_BASE * (100 + cp_pct) // 100
    if goal:
        return GOAL_RACE_EXTRA + pts
    return pts + RACE_PT_BASE * beach_race_pct // 100
