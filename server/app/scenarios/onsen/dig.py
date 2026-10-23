
from __future__ import annotations

import bisect
import math

from . import master as M

SAND, SOIL, ROCK = 1, 2, 3
STAT_KEYS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}

RANK_EDGES = (50, 100, 150, 200, 250, 300, 350, 400, 500, 600, 700, 800, 900,
              1000, 1050, 1100, 1150, 1201)

LINK_CHARAS = {1003: SAND, 1026: SOIL, 1080: SOIL, 1099: ROCK, 1100: ROCK}
LINK_POWER = 10

BASE_TRAINING = 25
BASE_ASSISTANT = 10
BASE_REST = 15
BASE_KIYOKO_OUTING = 25
BASE_RACE = 15
BASE_GOAL_RACE = 25

THRESHOLD_ENERGY = 10


def stat_rank(value: int) -> int:
    return bisect.bisect_right(RANK_EDGES, int(value or 0)) + 1


def tool_value(chara_info: dict, stratum_type: int, level: int, blue_groups=None,
               enabled: bool = True) -> int:
    item = M.item_for_stratum().get(stratum_type, stratum_type)
    total = M.dig_item_values().get((item, int(level)), 0)
    for param in M.stratum_params().get(stratum_type, ()):
        rank = stat_rank(chara_info.get(STAT_KEYS[param]) or 0)
        for lo, hi, bonus in M.dig_power().get((stratum_type, param), ()):
            if lo <= rank <= hi:
                total += bonus
                break
    if enabled:
        for group in blue_groups or ():
            total += M.factor_bonus().get((stratum_type, int(group)), 0)
    return total


def link_power(linked: set, stratum_type: int) -> int:
    return LINK_POWER * sum(1 for c, t in LINK_CHARAS.items() if t == stratum_type and c in linked)


def multiplier(tool: int, link: int) -> float:
    return 1 + (tool + link) / 100


def plan(base: int, layers: list) -> list:
    out = []
    budget = int(base)
    for sid, rest, m in layers:
        if budget <= 0 or rest <= 0:
            continue
        full = math.floor(budget * m + 1e-9)
        if full >= rest:
            out.append((sid, rest))
            budget -= math.ceil(rest / m - 1e-9)
        else:
            if full > 0:
                out.append((sid, full))
            budget = 0
    return out


def bonus_for(stratum_id: int, before: int, after: int) -> dict:
    out: dict = {}
    for dv, rows in M.dig_bonus().get(int(stratum_id), ()):
        if before < dv <= after:
            for tt, v in rows:
                out[tt] = out.get(tt, 0) + v
    return out


def plan_bonus(digs: list, rests: dict) -> dict:
    out: dict = {}
    info = M.stratum_info()
    for sid, amount in digs:
        vol = info[sid][2]
        before = vol - int(rests.get(str(sid), rests.get(sid, vol)))
        for tt, v in bonus_for(sid, before, before + amount).items():
            out[tt] = out.get(tt, 0) + v
    return out
