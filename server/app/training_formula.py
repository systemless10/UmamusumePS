
from __future__ import annotations

import functools
import json
import logging
import math
import random
from pathlib import Path

from . import config, master_data

log = logging.getLogger("uma-server")

REF_DIR = Path(__file__).resolve().parents[1] / "data" / "training_ref"

@functools.lru_cache(maxsize=None)
def trainee_growth_rates(card_id: int) -> tuple[int, ...]:
    row = master_data.query_one(
        "SELECT talent_speed, talent_stamina, talent_pow, talent_guts, talent_wiz "
        "FROM card_data WHERE id=?", (card_id,))
    return tuple(row) if row is not None else (0, 0, 0, 0, 0)

with open(REF_DIR / "cards.json", encoding="utf-8") as f:
    _CARDS = json.load(f)


class _MasterBackedCards(dict):

    def get(self, key, default=None):
        hit = dict.get(self, key)
        if hit is not None:
            return hit
        built = _card_from_master(key)
        return built if built is not None else default

    def __contains__(self, key):
        return dict.__contains__(self, key) or _card_from_master(key) is not None

    def __getitem__(self, key):
        hit = self.get(key)
        if hit is None:
            raise KeyError(key)
        return hit


CARD_BY_ID = _MasterBackedCards({c["id"]: c for c in _CARDS})

_MASTER_CARD_TYPE = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wisdom"}
_MASTER_RARITY = {1: "R", 2: "SR", 3: "SSR"}
_MASTER_EFFECT_KEYS = {
    "friendship_bonus": 1, "mood_effect": 2, "speed_bonus": 3, "stamina_bonus": 4,
    "power_bonus": 5, "guts_bonus": 6, "wisdom_bonus": 7, "training_bonus": 8,
    "initial_friendship_gauge": 14, "race_bonus": 15, "fan_bonus": 16,
    "hint_levels": 17, "hint_frequency": 18, "specialty_priority": 19,
    "skill_point_bonus": 30,
}


@master_data.context_cache(maxsize=256)
def _card_from_master(key):
    try:
        card_id = int(key)
    except (TypeError, ValueError):
        return None
    try:
        row = master_data.query_one(
            "SELECT support_card_type, command_id, rarity FROM support_card_data "
            "WHERE id=?", (card_id,))
    except Exception:
        return None
    if row is None:
        return None
    kind = int(row["support_card_type"] or 1)
    ctype = ("friend" if kind == 2 else "group" if kind == 3
             else _MASTER_CARD_TYPE.get(int(row["command_id"] or 0)))
    effects = {}
    for name, etype in _MASTER_EFFECT_KEYS.items():
        value = master_effect(card_id, etype)
        if value:
            effects[name] = int(value) if float(value).is_integer() else value
    return {"id": str(card_id), "title": "", "rarity": _MASTER_RARITY.get(row["rarity"], "SSR"),
            "type": ctype, "effects": [effects],
            "source": "master"}

with open(REF_DIR / "training_data.json", encoding="utf-8") as f:
    _TRAINING_DATA = json.load(f)
FACILITIES_BY_SCENARIO = {
    entry["scenario"]: entry["facilities"]
    for entry in _TRAINING_DATA
    if "scenario" in entry
}
LEVEL_BOOSTS = next(e["trainingLevelBoosts"] for e in _TRAINING_DATA if "trainingLevelBoosts" in e)

STAT_NAMES = ["speed", "stamina", "power", "guts", "wiz"]
CARD_TYPE_NAMES = ["speed", "stamina", "power", "guts", "wisdom"]
BASE_MOOD_MULTS = [-20, -10, 0, 10, 20]
MAX_STAT_1200_HALVING = 1200

_command_to_facility: dict[int, int] | None = None


def _facility_index_for(command_id: int) -> int | None:
    global _command_to_facility
    if _command_to_facility is None:
        rows = master_data.query(
            "SELECT DISTINCT command_id, base_command_id FROM single_mode_training"
        )
        base_to_facility = {101: 0, 105: 1, 102: 2, 103: 3, 106: 4}
        _command_to_facility = {
            row["command_id"]: base_to_facility[row["base_command_id"]]
            for row in rows
            if row["base_command_id"] in base_to_facility
        }
    return _command_to_facility.get(command_id)


_TARGET_TYPE_TO_STAT = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}
_TARGET_ENERGY = 10
_TARGET_SKILL_POINT = 30


def facility_stats(scenario_id: int, command_id: int) -> list:
    stats, _sp, _energy = _master_facility_gains(scenario_id, command_id)
    if not stats:
        return []
    return [STAT_NAMES[i] for i, v in enumerate(stats) if v > 0]


def _master_facility_gains(scenario_id: int, command_id: int):
    def fetch(cid, sid):
        return master_data.query(
            "SELECT target_type, effect_value FROM single_mode_training_effect "
            "WHERE command_id=? AND scenario_id=? AND result_state=2 AND sub_id=1",
            (cid, sid),
        )

    rows = fetch(command_id, scenario_id)
    if not rows:
        base = master_data.query_one(
            "SELECT base_command_id FROM single_mode_training WHERE command_id=? LIMIT 1", (command_id,))
        base_cid = base["base_command_id"] if base else command_id
        rows = fetch(base_cid, scenario_id)
        if not rows and scenario_id != 1:
            rows = fetch(base_cid, 1)
    if not rows:
        return None, 0, 0

    stats = [0, 0, 0, 0, 0]
    sp = 0
    energy = 0
    for r in rows:
        tt, v = r["target_type"], r["effect_value"]
        if tt in _TARGET_TYPE_TO_STAT:
            stats[_TARGET_TYPE_TO_STAT[tt]] = v
        elif tt == _TARGET_SKILL_POINT:
            sp = v
        elif tt == _TARGET_ENERGY:
            energy = v
    return stats, sp, energy


def _card_bond(card: dict) -> int:
    effects = card.get("effects") or []
    max_level = effects[-1] if effects else {}
    return max_level.get("initial_friendship_gauge", 0)


def _card_effect(card: dict, key: str, default=0):
    effects = card.get("effects") or []
    max_level = effects[-1] if effects else {}
    return max_level.get(key, default)


NUM_TO_BONUS_KEY = {
    0: "speed_bonus", 1: "stamina_bonus", 2: "power_bonus",
    3: "guts_bonus", 4: "wisdom_bonus",
}

@master_data.context_cache(maxsize=512)
def _camp_command_for(base_command_id: int) -> int | None:
    row = master_data.query_one(
        "SELECT command_id FROM single_mode_training WHERE base_command_id=? "
        "AND command_level=5 AND command_id<>? LIMIT 1", (base_command_id, base_command_id))
    return row["command_id"] if row else None


@master_data.context_cache(maxsize=512)
def facility_gains(scenario_id: int, command_id: int, level: int = 1):
    fac = _facility_index_for(command_id)
    row = master_data.query_one(
        "SELECT base_command_id FROM single_mode_training WHERE command_id=? LIMIT 1",
        (command_id,))
    base_cid = row["base_command_id"] if row else command_id
    if command_id != base_cid:
        return _as_tuple(_master_facility_gains(scenario_id, command_id))
    stats, sp, energy = _master_facility_gains(scenario_id, base_cid)
    if stats is None or fac is None or level <= 1:
        return _as_tuple((stats, sp, energy))
    level = min(int(level), len(LEVEL_BOOSTS))
    camp = _camp_command_for(base_cid)
    if level == 5 and camp and _has_rows(scenario_id, camp):
        return _as_tuple(_master_facility_gains(scenario_id, camp))
    measured = _URA_GUTS_LEVEL_GAINS.get(level) if (scenario_id, base_cid) == (1, 103) else None
    if measured:
        return (measured[0], sp, energy + measured[1])
    boost = LEVEL_BOOSTS[level - 1][fac]
    return (tuple(stats[i] + boost[i] for i in range(5)), sp + boost[5], energy + boost[6])


_URA_GUTS_LEVEL_GAINS = {2: ((5, 0, 5, 9, 0), -1), 3: ((5, 0, 5, 10, 0), -2),
                         4: ((5, 0, 5, 12, 0), -4)}


def _as_tuple(gains):
    stats, sp, energy = gains
    return (tuple(stats) if stats is not None else None, sp, energy)


def _has_rows(scenario_id: int, command_id: int) -> bool:
    return master_data.query_one(
        "SELECT 1 FROM single_mode_training_effect WHERE command_id=? AND scenario_id=? "
        "AND result_state=2 AND sub_id=1 LIMIT 1", (command_id, scenario_id)) is not None


def card_effect(support_card_id, key: str, default=0):
    card = CARD_BY_ID.get(str(support_card_id))
    return _card_effect(card, key, default) if card else default


_RARITY_BASE_CAP = {1: 20, 2: 25, 3: 30}

EFFECT_FRIENDSHIP = 1
EFFECT_MOOD = 2
EFFECT_STAT_BONUS_BASE = 3
EFFECT_TRAINING = 8
EFFECT_INITIAL_STAT_BASE = 9
EFFECT_INITIAL_BOND = 14
EFFECT_RACE_BONUS = 15
EFFECT_FAN_BONUS = 16
EFFECT_HINT_LEVEL = 17
EFFECT_HINT_FREQ = 18
EFFECT_SPECIALTY_PRIORITY = 19
EFFECT_EVENT_RECOVERY = 25
EFFECT_EVENT_EFFECT = 26
EFFECT_FAILURE_DOWN = 27
EFFECT_ENERGY_COST_DOWN = 28
EFFECT_SKILL_POINT = 30
EFFECT_WIT_RECOVERY = 31
EFFECT_FRIENDSHIP_ENERGY_DOWN = 113

EFFECT_TRAINING_IF_NOT_PREFERRED = 102
EFFECT_TRAINING_IF_DECK_TYPE_COUNT = 103
EFFECT_TRAINING_IF_FANS_GAINED = 104

EFFECT_TRAINING_IF_MAX_ENERGY = 108
EFFECT_TRAINING_IF_DECK_BOND_SUM = 109
EFFECT_TRAINING_IF_CARDS_IN_FACILITY = 110
EFFECT_TRAINING_IF_FACILITY_LEVEL = 111
EFFECT_TRAINING_IF_CURRENT_ENERGY = 114

EFFECT_FRIENDSHIP_STACKING = 106

EFFECT_FAILURE_RATE_ZERO_CHANCE = 112

EFFECT_INITIAL_STAT_DECK_COMPOSITION = 105

EFFECT_FRIENDSHIP_IF_LOW_ENERGY = 107
EFFECT_IF_BOND = 101
EFFECT_HINT_COUNT = 33
EFFECT_ALL_STATS = 41
EFFECT_INITIAL_SKILL_POINT = 32

EFFECT_DECK_INITIAL_BOND = 115
EFFECT_PER_OWNED_SKILL = 116
EFFECT_FACILITY_LEVEL_SUM = 117
EFFECT_TWO_FACILITIES = 118
EFFECT_DECK_APPEAR_UP = 119
EFFECT_DECK_TYPE_STAT_BONUS = 120
EFFECT_TRAINING_BOND_UP = 121
EFFECT_SHARED_NEXT_TURN = 122
_SKILL_CATEGORY_ABILITY = {1: 27, 2: 31, 3: 9}

_EFFECT_LEVEL_COLUMNS = [
    (0, "init"), (5, "limit_lv5"), (10, "limit_lv10"), (15, "limit_lv15"),
    (20, "limit_lv20"), (25, "limit_lv25"), (30, "limit_lv30"),
    (35, "limit_lv35"), (40, "limit_lv40"), (45, "limit_lv45"),
    (50, "limit_lv50"),
]


@master_data.context_cache(maxsize=4096)
def _effect_rows(effect_table_id: int) -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM support_card_effect_table WHERE id=?", (effect_table_id,)))


@master_data.context_cache(maxsize=4096)
def _card_meta(support_card_id: int) -> tuple:
    row = master_data.query_one(
        "SELECT effect_table_id, rarity FROM support_card_data WHERE id=?",
        (support_card_id,))
    if row is None:
        return (None, 1)
    return (row["effect_table_id"], row["rarity"] or 1)


def master_effect(support_card_id, effect_type: int, level: int | None = None) -> float:
    table_id, rarity = _card_meta(support_card_id)
    if table_id is None:
        return 0.0
    if level is None:
        level = _RARITY_BASE_CAP.get(rarity, 30) + 20
    for row in _effect_rows(table_id):
        if row.get("type") != effect_type:
            continue
        prev = None
        for lv, col in _EFFECT_LEVEL_COLUMNS:
            val = row.get(col)
            if val is None or val == -1:
                continue
            if lv == level:
                return float(val)
            if lv > level:
                if prev is None:
                    return 0.0
                lv0, val0 = prev
                return float(math.floor(val0 + (val - val0) * (level - lv0) / (lv - lv0)))
            prev = (lv, float(val))
        return prev[1] if prev else 0.0
    return 0.0


@master_data.context_cache(maxsize=4096)
def _unique_rows(support_card_id: int) -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT lv, type_0, value_0, value_0_1, value_0_2, value_0_3, value_0_4, "
        "type_1, value_1, value_1_1, value_1_2, value_1_3, value_1_4 "
        "FROM support_card_unique_effect WHERE id=?", (support_card_id,)))


def unique_effect(support_card_id, effect_type: int, level: int | None = None,
                  bond: int | None = None) -> float:
    table_id, rarity = _card_meta(support_card_id)
    if level is None:
        level = _RARITY_BASE_CAP.get(rarity, 30) + 20
    total = 0.0
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > level:
            continue
        if row.get("type_0") == effect_type:
            total += float(row.get("value_0") or 0)
        if row.get("type_1") == effect_type:
            total += float(row.get("value_1") or 0)
    if bond is not None:
        total += bond_gated_effect(support_card_id, effect_type, bond, level)
        if EFFECT_STAT_BONUS_BASE <= effect_type <= EFFECT_STAT_BONUS_BASE + 4:
            total += bond_gated_effect(support_card_id, EFFECT_ALL_STATS, bond, level)
    return total


def unique_params(support_card_id, effect_type: int, level: int | None = None) -> list:
    table_id, rarity = _card_meta(support_card_id)
    if level is None:
        level = _RARITY_BASE_CAP.get(rarity, 30) + 20
    out = []
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > level:
            continue
        for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
            if t == effect_type:
                out.append(tuple(int(row.get(k) or 0) for k in
                                 (prefix, f"{prefix}_1", f"{prefix}_2",
                                  f"{prefix}_3", f"{prefix}_4")))
    return out


@master_data.context_cache(maxsize=4096)
def skill_categories(skill_id) -> frozenset:
    row = master_data.query_one("SELECT * FROM skill_data WHERE id=?", (int(skill_id),))
    if row is None:
        return frozenset()
    row = dict(row)
    found = set()
    for i in (1, 2):
        for j in (1, 2, 3):
            ability = row.get(f"ability_type_{i}_{j}")
            if (ability and (row.get(f"float_ability_value_{i}_{j}") or 0) > 0
                    and row.get(f"target_type_{i}_{j}") == 1):
                found.update(c for c, a in _SKILL_CATEGORY_ABILITY.items() if a == ability)
    return frozenset(found)


def situational_unique(support_card_id, effect_type: int, level: int | None = None,
                       owned_skill_ids=(), facility_level_sum: int = 0) -> float:
    total = 0.0
    for category, etype, per, max_n, _ in unique_params(
            support_card_id, EFFECT_PER_OWNED_SKILL, level):
        if etype == effect_type and per:
            n = sum(1 for s in owned_skill_ids or () if category in skill_categories(s))
            total += per * min(n, max_n)
    for etype, full_at, max_v, _, _ in unique_params(
            support_card_id, EFFECT_FACILITY_LEVEL_SUM, level):
        if etype == effect_type and full_at:
            total += min(max_v, max_v * facility_level_sum / full_at)
    return total


def deck_type_stat_bonus(support_card_id, bond: int, deck_card_ids,
                         level: int | None = None) -> tuple:
    stats, sp = [0, 0, 0, 0, 0], 0
    for _, min_bond, per, max_v, _ in unique_params(
            support_card_id, EFFECT_DECK_TYPE_STAT_BONUS, level):
        if bond < min_bond:
            continue
        counts = [0] * 5
        pals = 0
        for sid in deck_card_ids or ():
            t = card_type_index(sid)
            if t is None:
                pals += 1
            else:
                counts[t] += 1
        for y in range(5):
            stats[y] += min(max_v, per * counts[y])
        sp += min(max_v, per * pals)
    return stats, sp


def bond_gated_effect(support_card_id, effect_type: int, bond: int,
                      level: int | None = None) -> float:
    table_id, rarity = _card_meta(support_card_id)
    if level is None:
        level = _RARITY_BASE_CAP.get(rarity, 30) + 20
    total = 0.0
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > level:
            continue
        for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
            if t != EFFECT_IF_BOND or bond < (row.get(prefix) or 0):
                continue
            for sub, val in ((f"{prefix}_1", f"{prefix}_2"), (f"{prefix}_3", f"{prefix}_4")):
                if row.get(sub) == effect_type:
                    total += float(row.get(val) or 0)
    return total


def _deck_card_type_count(deck_card_ids) -> int:
    types = set()
    for sid in deck_card_ids or ():
        card = CARD_BY_ID.get(str(sid))
        t = card.get("type") if card else None
        if t:
            types.add(t)
    return len(types)


def deck_composition_initial_stat_bonus(deck_card_ids, support_card_levels: dict | None) -> dict:
    ids = list(deck_card_ids or ())
    levels = support_card_levels or {}
    totals: dict[str, int] = {}
    granted = False
    for sid in ids:
        unique_lv = levels.get(sid)
        if not unique_lv:
            continue
        for row in _unique_rows(sid):
            if (row.get("lv") or 0) > unique_lv:
                continue
            for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
                if t != EFFECT_INITIAL_STAT_DECK_COMPOSITION:
                    continue
                per_match = row.get(prefix) or 0
                per_friend_group = row.get(f"{prefix}_1") or 0
                granted = True
                for deck_sid in ids:
                    card = CARD_BY_ID.get(str(deck_sid))
                    ctype = card.get("type") if card else None
                    if ctype in ("friend", "group"):
                        for stat in STAT_NAMES:
                            totals[stat] = totals.get(stat, 0) + per_friend_group
                    elif ctype in CARD_TYPE_NAMES:
                        stat = STAT_NAMES[CARD_TYPE_NAMES.index(ctype)]
                        totals[stat] = totals.get(stat, 0) + per_match
    if not granted:
        return {}
    return totals


def _unique_conditional_training_bonus(support_card_id, effect_type: int, level, *,
                                       deck_type_count: int = 0, total_fan: int = 0,
                                       not_preferred: bool = False, bond: int = 0,
                                       current_vital: int = 0, max_vital: int = 0,
                                       deck_bond_sum: int = 0,
                                       cards_in_facility: int = 0,
                                       facility_level: int = 0) -> float:
    total = 0.0
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > (level or 0):
            continue
        for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
            if t != effect_type:
                continue
            v0 = row.get(prefix) or 0
            v1 = row.get(f"{prefix}_1") or 0
            v2 = row.get(f"{prefix}_2") or 0
            v3 = row.get(f"{prefix}_3") or 0
            v4 = row.get(f"{prefix}_4") or 0
            if effect_type == EFFECT_TRAINING_IF_NOT_PREFERRED:
                if not_preferred and bond >= v0:
                    total += v1
            elif effect_type == EFFECT_TRAINING_IF_DECK_TYPE_COUNT:
                if deck_type_count >= v0:
                    total += v1
            elif effect_type == EFFECT_TRAINING_IF_FANS_GAINED:
                if v0:
                    total += min(v1, total_fan // v0)
            elif effect_type == EFFECT_TRAINING_IF_MAX_ENERGY:
                if max_vital > v1:
                    total += min(v4, v3 + (max_vital - v1) * (v2 / 100))
                elif max_vital:
                    total += v3
            elif effect_type == EFFECT_TRAINING_IF_DECK_BOND_SUM:
                if v1:
                    total += deck_bond_sum // v1
            elif effect_type == EFFECT_TRAINING_IF_CARDS_IN_FACILITY:
                total += cards_in_facility * v1
            elif effect_type == EFFECT_TRAINING_IF_FACILITY_LEVEL:
                total += facility_level * v1
            elif effect_type == EFFECT_TRAINING_IF_CURRENT_ENERGY:
                total += min(v2, v1 + (min(current_vital, 100) / 100) * (v2 - v1))
    return total


def _friendship_stack_bonus(support_card_id, level, stacks: int) -> float:
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > (level or 0):
            continue
        for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
            if t != EFFECT_FRIENDSHIP_STACKING:
                continue
            max_stacks = row.get(prefix) or 0
            per_stack = row.get(f"{prefix}_2") or 0
            return min(stacks, max_stacks) * per_stack
    return 0.0


def _low_energy_friendship_bonus(support_card_id, level, current_vital: int) -> float:
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > (level or 0):
            continue
        for prefix, t in (("value_0", row.get("type_0")), ("value_1", row.get("type_1"))):
            if t != EFFECT_FRIENDSHIP_IF_LOW_ENERGY:
                continue
            low_vital = row.get(f"{prefix}_2") or 0
            ceiling = row.get(f"{prefix}_3") or 0
            floor = row.get(f"{prefix}_4") or 0
            vital = min(max(current_vital or 0, low_vital), 100)
            if low_vital >= 100:
                return float(ceiling)
            return floor + (100 - vital) / (100 - low_vital) * (ceiling - floor)
    return 0.0


def training_failure_zero_proc(in_facility_card_ids, support_card_levels: dict | None) -> bool:
    levels = support_card_levels or {}
    for sid in in_facility_card_ids or ():
        unique_lv = levels.get(sid)
        if not unique_lv:
            continue
        for row in _unique_rows(sid):
            if (row.get("lv") or 0) > unique_lv:
                continue
            for t, v in ((row.get("type_0"), row.get("value_0")),
                        (row.get("type_1"), row.get("value_1"))):
                if t == EFFECT_FAILURE_RATE_ZERO_CHANCE and v and random.random() * 100 < v:
                    return True
    return False


def friendship_energy_down(support_card_id, level) -> float:
    total = 0.0
    for row in _unique_rows(support_card_id):
        if (row.get("lv") or 0) > (level or 0):
            continue
        for prefix in ("value_0", "value_1"):
            t = row.get("type_0" if prefix == "value_0" else "type_1")
            if t == EFFECT_FRIENDSHIP_ENERGY_DOWN and row.get(prefix) == EFFECT_ENERGY_COST_DOWN:
                total += float(row.get(f"{prefix}_1") or 0)
    return total


def deck_effect_total(support_card_ids, effect_type: int) -> float:
    return sum(master_effect(sid, effect_type) + unique_effect(sid, effect_type)
               for sid in support_card_ids or ())


def card_level(support_card_id, exp: int = 0, limit_break_count=None) -> int:
    from . import region
    if region.CURRENT_REGION.get() == "jp":
        return level_cap_for(support_card_id, limit_break_count)
    return support_card_level_from_exp(support_card_id, exp or 0)


def level_cap_for(support_card_id, limit_break_count) -> int:
    _table_id, rarity = _card_meta(support_card_id)
    lb = 4 if limit_break_count is None else int(limit_break_count)
    return _RARITY_BASE_CAP.get(rarity, 30) + 5 * max(0, min(4, lb))


@master_data.context_cache(maxsize=512)
def support_card_level_from_exp(support_card_id, exp: int) -> int:
    row = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
    if row is None:
        return 1
    best = master_data.query_one(
        "SELECT MAX(level) AS lv FROM support_card_level WHERE rarity=? AND total_exp<=?",
        (row["rarity"], exp))
    return (best["lv"] if best and best["lv"] is not None else 1)


def _facility_base_failure(command_id, facility_level: int = 1) -> int:
    row = master_data.query_one(
        "SELECT failure_rate FROM single_mode_training WHERE command_id=? AND command_level=?",
        (command_id, facility_level))
    return row["failure_rate"] if row else 520


def failure_rate(vital: int, command_id=None, facility_level: int = 1,
                 conditions=None, deck=(), extra_down_pct: int = 0,
                 facility_cards=None) -> int:
    base = _facility_base_failure(command_id, facility_level) if command_id else 520
    x0 = 0.1 * base
    f = (100 - vital) * (x0 - vital) / 40.0 if vital < x0 else 0.0
    f = max(0.0, min(99.0, f))
    if facility_cards is not None:
        down = sum(max(0.0, master_effect(sid, EFFECT_FAILURE_DOWN, level))
                   for sid, level in facility_cards if sid)
        f *= max(0.0, 1 - down / 100)
    elif deck:
        f *= max(0.0, 1 - deck_effect_total(deck, EFFECT_FAILURE_DOWN) / 100)
    if extra_down_pct:
        f = round(f * max(0.0, 1 - extra_down_pct / 100), 9)
    fr = math.ceil(round(f, 9))
    conds = conditions or ()
    if 6 in conds:
        fr += 2
    if 11 in conds:
        fr -= 4
    elif 10 in conds:
        fr -= 2
    fr = max(0, min(99, fr))
    override = forced_failure_rate()
    return fr if override is None else override


def forced_failure_rate():
    value = config.get("force_failure_rate")
    if value is None:
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        log.warning("client_config.json force_failure_rate is not an integer: %r",
                    value)
        return None


def training_energy(base_delta: int, command_id, in_facility_cards=(),
                    levels: dict | None = None) -> int:
    entries = [(c, bond) for c, bond, *_ in in_facility_cards or () if c]
    if base_delta < 0:
        cost = energy_cost(base_delta, deck=[c for c, _ in entries], bonds=dict(entries),
                           levels=levels)
        mult = config.get_float("energy_cost_multiplier")
        return cost if mult is None else min(0, int(round(cost * max(0.0, mult))))
    if _facility_index_for(command_id) == 4:
        return base_delta + int(round(sum(
            master_effect(c, EFFECT_WIT_RECOVERY)
            + bond_gated_effect(c, EFFECT_WIT_RECOVERY, bond) for c, bond in entries
            if bond >= 80 and (CARD_BY_ID.get(str(c)) or {}).get("type") == "wisdom")))
    return base_delta


def energy_cost(base_cost: int, deck=(), bonds: dict | None = None,
                levels: dict | None = None) -> int:
    if not deck or base_cost >= 0:
        return base_cost
    levels = levels or {}
    down = sum(master_effect(sid, EFFECT_ENERGY_COST_DOWN, levels.get(sid))
               + unique_effect(sid, EFFECT_ENERGY_COST_DOWN, level=levels.get(sid))
               for sid in deck)
    down += sum(bond_gated_effect(c, EFFECT_ENERGY_COST_DOWN, b)
                for c, b in (bonds or {}).items())
    factor = max(0.0, 1 - down / 100)
    return -math.floor(abs(base_cost) * factor + 1e-9)


def card_type_index(support_card_id):
    card = CARD_BY_ID.get(str(support_card_id))
    if not card or card.get("type") not in CARD_TYPE_NAMES:
        return None
    return CARD_TYPE_NAMES.index(card["type"])


_INITIAL_STAT_TYPES = {9: "speed", 10: "stamina", 11: "power", 12: "guts", 13: "wiz"}
_RARITY_BASE_CAP = {1: 20, 2: 25, 3: 30}
_EFFECT_LEVEL_COLS = [
    ("init", 1), ("limit_lv5", 5), ("limit_lv10", 10), ("limit_lv15", 15),
    ("limit_lv20", 20), ("limit_lv25", 25), ("limit_lv30", 30), ("limit_lv35", 35),
    ("limit_lv40", 40), ("limit_lv45", 45), ("limit_lv50", 50),
]


def initial_stat_bonuses(support_card_id, limit_break_count: int = 4) -> dict:
    rows = master_data.query(
        "SELECT rarity, effect_table_id FROM support_card_data WHERE id = ?",
        (support_card_id,))
    if not rows:
        return {}
    rarity = rows[0]["rarity"]
    etid = rows[0]["effect_table_id"]
    level = _RARITY_BASE_CAP.get(rarity, 30) + 5 * max(0, min(4, limit_break_count or 0))
    out: dict[str, int] = {}
    for eff in master_data.query(
            "SELECT * FROM support_card_effect_table WHERE id = ? AND type IN (9,10,11,12,13)",
            (etid,)):
        stat = _INITIAL_STAT_TYPES.get(eff["type"])
        if stat is None:
            continue
        val = 0
        for col, bp in _EFFECT_LEVEL_COLS:
            if bp > level:
                break
            v = eff[col]
            if v is not None and v != -1:
                val = v
        if val:
            out[stat] = val
    return out


def _pure_passion_friendship(support_card_id, pure_passion_cards=()) -> bool:
    return bool(pure_passion_cards) and support_card_id in pure_passion_cards


def friendship_multiplier(in_facility_cards: list[tuple[int, int]], command_id: int,
                          scenario_friendship_bonus: int = 0,
                          pure_passion_cards=()) -> float:
    fac = _facility_index_for(command_id)
    if fac is None:
        return 1.0
    final_friendship_bonus = 1.0
    any_friendship = False
    for sid, bond in in_facility_cards:
        card = CARD_BY_ID.get(str(sid))
        if card is None:
            continue
        if card.get("type") in ("friend", "group"):
            if _pure_passion_friendship(sid, pure_passion_cards):
                final_friendship_bonus *= 1 + (_card_effect(card, "friendship_bonus") / 100)
                any_friendship = True
            continue
        card_type_num = CARD_TYPE_NAMES.index(card["type"]) if card.get("type") in CARD_TYPE_NAMES else -1
        if card_type_num == fac and bond >= 80:
            final_friendship_bonus *= 1 + (_card_effect(card, "friendship_bonus") / 100)
            any_friendship = True
    if any_friendship and scenario_friendship_bonus:
        final_friendship_bonus *= 1 + (int(scenario_friendship_bonus) / 100)
    return final_friendship_bonus


def calculate_training_gain(
    *,
    card_id: int,
    in_facility_cards: list[tuple[int, int]],
    command_id: int,
    scenario_id: int,
    facility_level: int,
    motivation: int,
    current_stats: dict[str, int],
    max_stats: dict[str, int],
    extra_stat_bonus: dict | None = None,
    scenario_friendship_bonus: int = 0,
    support_card_levels: dict[int, int] | None = None,
    deck_card_ids: list[int] | None = None,
    total_fan: int = 0,
    current_vital: int = 0,
    max_vital: int = 0,
    deck_bond_sum: int = 0,
    friendship_stacks: dict[int, int] | None = None,
    pure_passion_cards=(),
    npc_supporter_count: int = 0,
    owned_skill_ids=(),
    facility_level_sum: int = 0,
) -> dict | None:
    fac = _facility_index_for(command_id)
    if fac is None:
        return None

    base_stats, base_sp, energy_delta = facility_gains(scenario_id, command_id, facility_level or 1)
    if base_stats is None:
        return None

    growth = trainee_growth_rates(card_id)

    total_mood_effect = 0
    total_training_effect = 0
    final_friendship_bonus = 1.0
    any_friendship = False
    stat_bonus = [0, 0, 0, 0, 0]
    count = 0
    sp_bonus = 0
    friendship_energy_down_pct = 0.0
    deck_type_count = _deck_card_type_count(deck_card_ids)

    for sid, bond in in_facility_cards:
        card = CARD_BY_ID.get(str(sid))
        if card is None:
            continue
        pal = card.get("type") in ("friend", "group")
        card_type_num = CARD_TYPE_NAMES.index(card["type"]) if card.get("type") in CARD_TYPE_NAMES else -1
        unique_lv = (support_card_levels or {}).get(sid)
        unique = ((lambda t: unique_effect(sid, t, level=unique_lv, bond=bond)
                   + situational_unique(sid, t, unique_lv, owned_skill_ids,
                                        facility_level_sum))
                  if unique_lv else (lambda t: 0.0))

        total_mood_effect += _card_effect(card, "mood_effect") + unique(EFFECT_MOOD)
        total_training_effect += _card_effect(card, "training_bonus") + unique(EFFECT_TRAINING)
        if unique_lv:
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_NOT_PREFERRED, unique_lv,
                not_preferred=(card_type_num != fac), bond=bond)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_DECK_TYPE_COUNT, unique_lv,
                deck_type_count=deck_type_count)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_FANS_GAINED, unique_lv, total_fan=total_fan)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_MAX_ENERGY, unique_lv, max_vital=max_vital)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_DECK_BOND_SUM, unique_lv, deck_bond_sum=deck_bond_sum)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_FACILITY_LEVEL, unique_lv, facility_level=facility_level)
            total_training_effect += _unique_conditional_training_bonus(
                sid, EFFECT_TRAINING_IF_CURRENT_ENERGY, unique_lv, current_vital=current_vital)

        if unique_lv:
            friendship_energy_down_pct += friendship_energy_down(sid, unique_lv)

        is_friendship = (_pure_passion_friendship(sid, pure_passion_cards) if pal
                         else (card_type_num == fac and bond >= 80))
        if is_friendship:
            friendship_pct = ((1 + _card_effect(card, "friendship_bonus") / 100)
                              * (1 + unique(EFFECT_FRIENDSHIP) / 100) - 1) * 100
            if unique_lv and friendship_stacks:
                friendship_pct += _friendship_stack_bonus(
                    sid, unique_lv, friendship_stacks.get(sid, 0))
            if unique_lv:
                friendship_pct += _low_energy_friendship_bonus(sid, unique_lv, current_vital)
            final_friendship_bonus *= 1 + (friendship_pct / 100)
            any_friendship = True

        for y in range(5):
            if base_stats[y] != 0:
                base_bonus = (master_effect(sid, EFFECT_STAT_BONUS_BASE + 4) if y == 4
                             else _card_effect(card, NUM_TO_BONUS_KEY[y]))
                stat_bonus[y] += base_bonus + unique(EFFECT_STAT_BONUS_BASE + y)
        if unique_lv:
            deck_stats, deck_sp = deck_type_stat_bonus(sid, bond, deck_card_ids, unique_lv)
            for y in range(5):
                if base_stats[y] != 0:
                    stat_bonus[y] += deck_stats[y]
            sp_bonus += deck_sp
        count += 1
        sp_bonus += _card_effect(card, "skill_point_bonus") + unique(EFFECT_SKILL_POINT)

    if support_card_levels:
        for sid, _bond in in_facility_cards:
            unique_lv = support_card_levels.get(sid)
            if unique_lv:
                total_training_effect += _unique_conditional_training_bonus(
                    sid, EFFECT_TRAINING_IF_CARDS_IN_FACILITY, unique_lv,
                    cards_in_facility=count + npc_supporter_count)

    song_stat_bonus = [int((extra_stat_bonus or {}).get(stat_name, 0) or 0)
                       for stat_name in STAT_NAMES]
    song_sp_bonus = int((extra_stat_bonus or {}).get("skill_point", 0) or 0)

    if any_friendship and scenario_friendship_bonus:
        final_friendship_bonus *= 1 + (int(scenario_friendship_bonus) / 100)

    final_mood_effect = 1 + (BASE_MOOD_MULTS[motivation] * ((total_mood_effect / 100) + 1) / 100)
    final_training_effect = 1 + (total_training_effect / 100)
    final_char_count = 1 + ((count + npc_supporter_count) / 20)

    result = {}
    for i, stat_name in enumerate(STAT_NAMES):
        if base_stats[i] == 0:
            continue
        base_val = base_stats[i] + stat_bonus[i]
        raw_val = math.floor(
            base_val * final_friendship_bonus * final_mood_effect
            * final_training_effect * final_char_count
            * ((growth[i] / 100) + 1)
            + song_stat_bonus[i] * final_friendship_bonus
        )
        result[stat_name] = _apply_stat_cap(raw_val, current_stats.get(stat_name, 0), max_stats.get(stat_name, 9999))

    sp_raw = base_sp + sp_bonus
    result["skill_point"] = math.floor(
        final_friendship_bonus * final_mood_effect * final_training_effect * final_char_count * sp_raw
        + song_sp_bonus * final_friendship_bonus
    )
    result["energy_cost"] = training_energy(energy_delta, command_id, in_facility_cards,
                                            levels=support_card_levels)
    if any_friendship and friendship_energy_down_pct and result["energy_cost"] < 0:
        result["energy_cost"] = -math.floor(
            abs(result["energy_cost"]) * max(0.0, 1 - friendship_energy_down_pct / 100) + 1e-9)

    return result


def _apply_stat_cap(stat_inc: int, current: int, max_stat: int) -> int:
    if stat_inc <= 0:
        return math.floor(stat_inc)

    if current >= MAX_STAT_1200_HALVING:
        actual = stat_inc / 2
    elif current + stat_inc > MAX_STAT_1200_HALVING:
        over = (current + stat_inc) - MAX_STAT_1200_HALVING
        actual = (MAX_STAT_1200_HALVING - current) + (over / 2)
    else:
        actual = stat_inc

    actual = max(1, math.floor(actual))
    if current + actual > max_stat:
        actual = max_stat - current
    return actual
