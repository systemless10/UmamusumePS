
from __future__ import annotations

import math

from ..ramen import formula as RF

SPORTS = tuple(g * 100 + 2000 + s for g in (1, 2, 3) for s in (1, 2, 3, 4, 5))
STATCMD = {1: 101, 2: 105, 3: 102, 4: 103, 5: 106}
TARGET_SP = 30
MAX_RANK = 100
REST_BONUS_COMMANDS = frozenset({701, 304, 390})
NPC_RANK_PARTNER = 111
LINK_MAIN_MULT = {0: 1.0, 1: 1.1, 2: 1.2, 3: 1.25, 4: 1.3}
LINK_MAIN_MULT_SUMMER = {0: 1.0, 1: 1.2, 2: 1.3, 3: 1.35, 4: 1.4}
FIGHT_STANCE_PCT = {1: 30, 2: 45, 3: 54, 4: 60}
SUMMER_TURNS = frozenset(range(37, 41)) | frozenset(range(61, 65))

BASE = {
    2101: {1: {1: 12, 3: 1}, 2: {1: 13, 3: 1}, 3: {1: 14, 3: 1}, 4: {1: 15, 3: 1}, 5: {1: 16, 3: 2}},
    2102: {1: {2: 11, 4: 2}, 2: {2: 12, 4: 2}, 3: {2: 13, 4: 2}, 4: {2: 14, 4: 2}, 5: {2: 15, 4: 3}},
    2103: {1: {2: 2, 3: 11}, 2: {2: 2, 3: 12}, 3: {2: 2, 3: 13}, 4: {2: 2, 3: 14}, 5: {2: 3, 3: 15}},
    2104: {1: {1: 1, 3: 1, 4: 12}, 2: {1: 1, 3: 1, 4: 13}, 3: {1: 1, 3: 1, 4: 14}, 4: {1: 1, 3: 1, 4: 15},
           5: {1: 2, 3: 1, 4: 16}},
    2105: {1: {1: 2, 5: 11}, 2: {1: 2, 5: 12}, 3: {1: 2, 5: 13}, 4: {1: 2, 5: 14}, 5: {1: 3, 5: 15}},
    2201: {1: {1: 8, 3: 1}, 2: {1: 9, 3: 1}, 3: {1: 10, 3: 1}, 4: {1: 11, 3: 1}, 5: {1: 12, 3: 2}},
    2202: {1: {2: 7, 4: 2}, 2: {2: 8, 4: 2}, 3: {2: 9, 4: 2}, 4: {2: 10, 4: 2}, 5: {2: 11, 4: 3}},
    2203: {1: {2: 2, 3: 7}, 2: {2: 2, 3: 8}, 3: {2: 2, 3: 9}, 4: {2: 2, 3: 10}, 5: {2: 3, 3: 11}},
    2204: {1: {1: 1, 3: 1, 4: 8}, 2: {1: 1, 3: 1, 4: 9}, 3: {1: 1, 3: 1, 4: 10}, 4: {1: 1, 3: 1, 4: 11},
           5: {1: 2, 3: 1, 4: 12}},
    2205: {1: {1: 2, 5: 7}, 2: {1: 2, 5: 8}, 3: {1: 2, 5: 9}, 4: {1: 2, 5: 10}, 5: {1: 3, 5: 11}},
    2301: {1: {1: 14, 3: 1}, 2: {1: 15, 3: 1}, 3: {1: 16, 3: 1}, 4: {1: 17, 3: 1}, 5: {1: 18, 3: 2}},
    2302: {1: {1: 1, 2: 10, 4: 2}, 2: {1: 1, 2: 11, 4: 2}, 3: {1: 1, 2: 12, 4: 2}, 4: {1: 1, 2: 13, 4: 2},
           5: {1: 2, 2: 14, 4: 2}},
    2303: {1: {1: 1, 2: 2, 3: 10}, 2: {1: 1, 2: 2, 3: 11}, 3: {1: 1, 2: 2, 3: 12}, 4: {1: 1, 2: 2, 3: 13},
           5: {1: 2, 2: 2, 3: 14}},
    2304: {1: {1: 2, 3: 1, 4: 12}, 2: {1: 2, 3: 1, 4: 13}, 3: {1: 2, 3: 1, 4: 14}, 4: {1: 2, 3: 1, 4: 15},
           5: {1: 3, 3: 1, 4: 16}},
    2305: {1: {1: 4, 5: 10}, 2: {1: 4, 5: 11}, 3: {1: 4, 5: 12}, 4: {1: 4, 5: 13}, 5: {1: 5, 5: 14}},
}
SP = {2101: 6, 2102: 6, 2103: 6, 2104: 6, 2105: 6, 2201: 10, 2202: 10, 2203: 10, 2204: 10, 2205: 10,
      2301: 4, 2302: 6, 2303: 6, 2304: 5, 2305: 5}
ENERGY = -15
ENERGY_EXTRA_WEIGHTS = ((0, 841), (1, 1696), (2, 1688), (3, 31))
SPHERE_STANCE_SP = 20
LINK_ADD = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3}


def genre(sport: int) -> int:
    return sport // 100 - 20


def main_stat(sport: int) -> int:
    return sport % 100


def level_of(rank: int) -> int:
    return 1 if rank < 20 else 2 if rank < 30 else 3 if rank < 40 else 4 if rank < 50 else 5


def friend_fn(sport: int):
    stat_cmd = STATCMD[main_stat(sport)]
    return lambda sid, kind, cmd, bond: kind == 1 and cmd == stat_cmd and bond >= RF.FRIENDSHIP_BOND


def self_rank_gain(chara_info: dict, sport: int, partners, after_rest: bool) -> int:
    by_pos, bond = RF.deck(chara_info), RF.bonds(chara_info)
    k = 0
    friendship = False
    stat_cmd = STATCMD[main_stat(sport)]
    for p in partners or ():
        if p in by_pos:
            k += 1
            kind, cmd, _r, _c = RF.card_meta(by_pos[p])
            if kind == 1 and cmd == stat_cmd and bond.get(p, 0) >= RF.FRIENDSHIP_BOND:
                friendship = True
        elif p == NPC_RANK_PARTNER:
            k += 1
    base = 3 + (k // 2 + 1 if k else 0)
    return base * (2 if friendship else 1) + (3 if after_rest else 0)


def gain_sport_rank_array(chara_info: dict, sport: int, offered: dict, ranks: dict, after_rest: bool) -> list:
    out = []
    for s in [sport] + [o for o in offered if o != sport and genre(o) == genre(sport)]:
        g = self_rank_gain(chara_info, s, offered[s], after_rest)
        out.append({"command_id": s, "gain_rank": max(0, min(g, MAX_RANK - int(ranks.get(s, 0))))})
    return out


def home_raw(chara_info: dict, sport: int, level: int, partners, card_levels=None) -> dict:
    return RF.home_raw(chara_info, sport, level, partners, card_levels, table=BASE, sp=SP[sport],
                       friend_fn=friend_fn(sport))


def roll_energy_extra(rng) -> int:
    total = sum(w for _v, w in ENERGY_EXTRA_WEIGHTS)
    x = rng.randrange(total)
    for v, w in ENERGY_EXTRA_WEIGHTS:
        if x < w:
            return v
        x -= w
    return 0


def home_params(chara_info: dict, sport: int, level: int, partners, card_levels=None,
                energy: int = ENERGY) -> list:
    raw = home_raw(chara_info, sport, level, partners, card_levels)
    out = []
    for tt in (1, 2, 3, 4, 5, TARGET_SP):
        if tt in raw:
            v = RF.displayed(chara_info, tt, RF.capped(raw[tt]))
            if v:
                out.append({"target_type": tt, "value": v})
    out.append({"target_type": 10, "value": RF.energy_after_cost_down(chara_info, partners, energy, card_levels)
                if hasattr(RF, "energy_after_cost_down") else energy})
    return out


def link_raw(chara_info: dict, sport: int, level: int, partners, linked, card_levels=None) -> dict:
    if not linked:
        return home_raw(chara_info, sport, level, partners, card_levels)
    adds: dict = {}
    for s, lv in linked:
        adds[main_stat(s)] = adds.get(main_stat(s), 0) + LINK_ADD[max(1, min(5, int(lv or 1)))]
    base = dict(BASE[sport][level])
    for tt, n in adds.items():
        if tt in base:
            base[tt] += n
    out = RF.home_raw(chara_info, sport, level, partners, card_levels, table={sport: {level: base}},
                      sp=SP[sport] + len(linked), friend_fn=friend_fn(sport))
    talents = RF.growth(chara_info.get("card_id"))
    for tt, n in adds.items():
        if tt not in BASE[sport][level]:
            out[tt] = n * _per_point(chara_info, sport, level, partners, tt, card_levels) / (1 + talents[tt - 1] / 100)
    return out


def _per_point(chara_info, sport, level, partners, tt, card_levels=None) -> float:
    def raw(v):
        return RF.home_raw(chara_info, sport, level, partners, card_levels, table={sport: {level: {tt: v}}},
                           sp=0, friend_fn=friend_fn(sport)).get(tt, 0.0)
    return raw(2) - raw(1)


def bonus_params(chara_info: dict, sport: int, level: int, partners, linked, compe_pct: int,
                 card_levels=None, summer: bool | None = None, stances=()) -> list:
    home_pre_raw = home_raw(chara_info, sport, level, partners, card_levels)
    raw = link_raw(chara_info, sport, level, partners, linked, card_levels)
    if summer is None:
        summer = int(chara_info.get("turn") or 0) in SUMMER_TURNS
    k = min(4, len(linked))
    mult = (LINK_MAIN_MULT_SUMMER if summer else LINK_MAIN_MULT)[k]
    if k and any(st // 100 == 2 for st in stances):
        mult += FIGHT_STANCE_PCT[k] / 100
    sphere = any(st // 100 == 1 for st in stances)
    own = BASE[sport][level]
    added = {main_stat(s) for s, _lv in linked} - set(own)
    order = sorted(t for t in raw if t not in added and t != TARGET_SP) + \
        ([TARGET_SP] if TARGET_SP in raw else []) + sorted(t for t in raw if t in added)
    out = []
    for tt in order:
        v = raw[tt] * (mult if tt == main_stat(sport) else 1.0) * (1 + compe_pct / 100)
        if tt == TARGET_SP and sphere:
            v += SPHERE_STANCE_SP
        home_pre = RF.capped(home_pre_raw.get(tt, 0))
        delta = min(RF.PREVIEW_CAP, int(math.floor(v + 1e-9))) - home_pre
        d = RF._shown_bonus(chara_info, tt, home_pre, delta) if delta > 0 else 0
        if d or tt in added:
            out.append({"target_type": tt, "value": d})
    return out
