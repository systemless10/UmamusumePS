
from __future__ import annotations


from ... import master_data

DARLEY, GODOLPHIN, BYERLEY = 9040, 9041, 9042
GODDESSES = (DARLEY, GODOLPHIN, BYERLEY)
LINK_CARD = 30137
PURE_PASSION = 102

FRAGMENT_SLOTS = tuple(range(1, 9))
CRYSTAL_SLOTS = (9, 10, 11, 12)
MERGED_SLOTS = (13, 14)
WISDOM_SLOT = 15
FUSION = {9: (1, 2), 10: (3, 4), 11: (5, 6), 12: (7, 8), 13: (9, 10), 14: (11, 12), 15: (13, 14)}

TYPE_SP = 6
TYPE_TARGET = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 30}
TYPE_FACILITY = {1: 101, 2: 105, 3: 102, 4: 103, 5: 106}

COL_FRAGMENT = 1
COL_CRYSTAL_SAME, COL_CRYSTAL_MIXED = 2, 3
COL_MERGED_SAME, COL_MERGED_MIXED = 4, 5

EFF_PARAM = 1
EFF_TRAINING = 2
EFF_HINT_RATE = 3
EFF_AFTER_TRAINING_EVENT = 4
EFF_ENERGY_DOWN = 7
EFF_SUPPORT_EVENT_GAIN = 8
EFF_CHAIN_RATE = 9
EFF_HINT_BOOST = 10
EFF_ALL_FRIENDSHIP = 11
EFF_ALL_LEVEL_UP = 12
EFF_INSTANT = 13
EFF_RACE_BONUS = 14


def _rows(sql: str, params: tuple = ()) -> list:
    try:
        return [dict(r) for r in master_data.query(sql, params)]
    except Exception:
        return []


@master_data.context_cache(maxsize=1)
def spirit_groups() -> dict:
    out = {}
    for r in _rows("SELECT * FROM single_mode_venus_spirit_group"):
        out[int(r["spirit_id"])] = (
            int(r["chara_id"]), int(r["bonus_group"]),
            tuple(int(r[f"spirit_effect_group_id_{i}"]) for i in range(1, 6)))
    return out


def spirit_id(goddess: int, type_: int) -> int:
    for sid, (g, t, _groups) in spirit_groups().items():
        if g == goddess and t == type_:
            return sid
    return (GODDESSES.index(goddess) * 8 + type_) if goddess in GODDESSES else 0


def goddess_of(sid: int) -> int:
    if sid in GODDESSES:
        return sid
    return spirit_groups().get(int(sid), (0, 0, ()))[0]


def type_of(sid: int) -> int:
    return spirit_groups().get(int(sid), (0, 0, ()))[1]


def effect_group(sid: int, column: int) -> int:
    groups = spirit_groups().get(int(sid), (0, 0, ()))[2]
    return groups[column - 1] if 0 < column <= len(groups) else 0


@master_data.context_cache(maxsize=1)
def _effects() -> dict:
    out: dict = {}
    for r in _rows("SELECT * FROM single_mode_venus_spirit_effect ORDER BY id"):
        out.setdefault(int(r["spirit_effect_group_id"]), []).append(
            (int(r["effect_type"]), int(r["effect_value_1"]), int(r["effect_value_2"]),
             int(r["effect_value_3"]), int(r["effect_value_4"])))
    return out


def effects(group: int) -> list:
    return list(_effects().get(int(group), ()))


def slot_value(group: int) -> int:
    for etype, _v1, v2, _v3, _v4 in effects(group):
        if etype == EFF_PARAM:
            return v2
    return 0


@master_data.context_cache(maxsize=1)
def crystal_groups() -> dict:
    return {(int(r["chara_id"]), int(r["venus_level"])):
            (int(r["passive_effect_group_id"]), int(r["active_effect_group_id"]))
            for r in _rows("SELECT * FROM single_mode_venus_crystal_group")}


def passive(goddess: int, level: int, effect_type: int) -> int:
    if level <= 0:
        return 0
    group = crystal_groups().get((int(goddess), min(5, int(level))), (0, 0))[0]
    return sum(v2 for et, _v1, v2, _v3, _v4 in effects(group) if et == effect_type)


def wisdom_group(goddess: int) -> int:
    return crystal_groups().get((int(goddess), 1), (0, 0))[1] or {
        DARLEY: 401, GODOLPHIN: 411, BYERLEY: 421}.get(int(goddess), 0)


@master_data.context_cache(maxsize=1)
def sprace_turns() -> dict:
    rows = _rows("SELECT race_group_id, turn_num FROM single_mode_venus_sprace_data")
    return ({int(r["turn_num"]): int(r["race_group_id"]) for r in rows}
            or {24: 50001, 48: 50002, 72: 50003})


GM_RACE_GROUP = 50004
GM_ROUTE_ROW = 990000
FINAL_TURN = 78


@master_data.context_cache(maxsize=8)
def group_programs(race_group: int) -> tuple:
    return tuple((int(r["pid"]), int(r["ground"]), int(r["distance"])) for r in _rows(
        "SELECT rg.race_program_id AS pid, cs.ground AS ground, cs.distance AS distance "
        "FROM single_mode_race_group rg JOIN single_mode_program p ON p.id=rg.race_program_id "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE rg.race_group_id=? "
        "ORDER BY rg.race_program_id", (int(race_group),)))


@master_data.context_cache(maxsize=512)
def program_course(program_id: int) -> tuple | None:
    row = master_data.query_one(
        "SELECT cs.ground AS ground, cs.distance AS distance FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE p.id=?", (int(program_id),))
    return (int(row["ground"]), int(row["distance"])) if row else None


@master_data.context_cache(maxsize=512)
def program_instance(program_id: int) -> int:
    row = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (int(program_id),))
    return int(row["race_instance_id"]) if row else 0


def is_special_program(program_id) -> bool:
    return 2601 <= int(program_id or 0) <= 2882


@master_data.context_cache(maxsize=4096)
def skill_group_rarity(skill_id: int) -> tuple:
    return master_data.skill_tip_key(int(skill_id))
