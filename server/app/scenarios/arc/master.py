
from __future__ import annotations

import collections
import functools

from ... import master_data

SCENARIO_ID = 6
ROUTE_ID = 20000
TURNS = 67

SPEED, STAMINA, POWER, GUTS, WIZ = 101, 105, 102, 103, 106
TRAININGS = (SPEED, STAMINA, POWER, GUTS, WIZ)
STAT_OF = {SPEED: "speed", STAMINA: "stamina", POWER: "power", GUTS: "guts", WIZ: "wiz"}
STAT_KEYS = ("speed", "stamina", "power", "guts", "wiz")
TARGET_OF = {SPEED: 1, STAMINA: 2, POWER: 3, GUTS: 4, WIZ: 5}

EXPED = {1101: SPEED, 1102: STAMINA, 1103: POWER, 1104: GUTS, 1105: WIZ}
EXPED_OF = {v: k for k, v in EXPED.items()}
EXPED_REST = 1106
EXPED_TURNS = frozenset(range(37, 44)) | frozenset(range(61, 68))

PARK_CLASSIC, PARK_SENIOR, RIGANTONA, MONTJEU = 2005901, 2005902, 2006901, 2004901
MEI = 9043
MEI_SSR, MEI_R = 30160, 10094
MEI_TARGET = 110
MEI_JAPAN_STORY, MEI_EXPED_STORY = 809043003, 400006115
MEI_WIRE = {(MEI_SSR, MEI_JAPAN_STORY): (21086, 6), (MEI_R, MEI_JAPAN_STORY): (21199, 6),
            (MEI_SSR, MEI_EXPED_STORY): (21105, 6)}
MEI_CHANCE = {(MEI_SSR, MEI_JAPAN_STORY): 15 / 31, (MEI_SSR, MEI_EXPED_STORY): 2 / 9}
MEI_GAUGE_MEMBERS = 5
MEI_EXPED_GEXP = 50
MOB_GROUP = 51

SLOTS = (("B", SPEED), ("A", STAMINA), ("C", POWER), ("B", GUTS), ("A", WIZ),
         ("C", SPEED), ("B", STAMINA), ("A", POWER), ("C", GUTS), ("B", WIZ),
         ("A", SPEED), ("C", STAMINA), ("B", POWER), ("A", GUTS), ("C", WIZ))
MEMBERS = len(SLOTS)

MEMBER_SCHEDULE = {
    "A": {7: [(1, 1)], 8: [(2, 1)], 16: [(5, 1)], 20: [(2, 2)], 21: [(3, 1)], 22: [(5, 2)],
          25: [(4, 1)], 27: [(6, 1)], 35: [(1, 2)], 37: [(3, 2)], 39: [(6, 2)],
          41: [(7, 1), (8, 1)], 42: [(4, 2), (7, 2), (8, 2)], 61: [(9, 1)], 66: [(9, 2)]},
    "B": {8: [(1, 1)], 10: [(2, 1)], 18: [(5, 1)], 21: [(2, 2)], 22: [(3, 1)], 23: [(5, 2)],
          28: [(6, 1)], 29: [(4, 1)], 36: [(3, 2)], 38: [(1, 2)], 40: [(6, 2)],
          41: [(7, 1), (8, 1)], 42: [(4, 2), (7, 2), (8, 2)], 61: [(9, 1)], 66: [(9, 2)]},
    "C": {10: [(1, 1)], 12: [(2, 1)], 18: [(3, 1)], 20: [(5, 1)], 23: [(2, 2)], 24: [(5, 2)],
          25: [(6, 1)], 29: [(4, 1)], 35: [(3, 2)], 37: [(1, 2)],
          41: [(7, 1), (8, 1)], 42: [(4, 2), (6, 2), (7, 2), (8, 2)], 61: [(9, 1)], 66: [(9, 2)]},
}

INITIAL_POINTS = ((1556, 1608), (1452, 1508), (1348, 1404), (1144, 1196), (1036, 1088),
                  (932, 988), (732, 788), (728, 780), (620, 676), (520, 572), (512, 564),
                  (408, 464), (404, 456), (300, 352), (300, 356))
INITIAL_POINTS_TOTAL = 12400
INITIAL_STAT_RATIO = (1.068, 0.994, 0.926, 0.856, 0.785, 0.711, 0.675, 0.644, 0.604,
                      0.564, 0.530, 0.495, 0.481, 0.468, 0.452)

POOL = ((24, 800), (48, 2000), (TURNS, 2400))
POOL_SHARE = (21.8, 20.7, 19.9, 18.6, 16.7, 16.3, 15.5, 13.7, 11.4, 11.6, 10.9, 10.0, 9.6,
              7.6, 7.2, 7.6)

LINK_X = {1133: (7,), 1137: (12,), 1128: (12,), 1049: (12,), 1115: (4,), 1025: (4,),
          1107: (5,), 1067: (5,), 1070: (6,), 1143: (7,), 1007: (8,), 1014: (9, 7)}
OTHER_X_WEIGHTS = {7: 56, 6: 28, 8: 5, 12: 5, 4: 2, 9: 2, 5: 1, 11: 1}

WITH_INDEX = {MEI: 1, 1070: 2, 1014: 3, 1025: 4, 1049: 5, 1007: 6, 1067: 7, 1107: 8, 1115: 9,
              1128: 10, 1133: 11, 1143: 12, 1137: 13}
WITH_CHARA = {v: k for k, v in WITH_INDEX.items()}
WITH_FIXED = {1007: 1049, 1014: 1070, 1008: 1107, 1009: 1107, 1018: 1070, 1106: 1107,
              1011: 1014, 1006: 1070, 1024: 1070, 1091: 1067, 1032: 1014}

LINK_CHAIN = {
    MEI: (206311, 206321, 206331), 1007: (206312, 206322, 206332),
    1014: (206313, 206323, 206333), 1025: (206314, 206324, 206334),
    1049: (206315, 206325, 206335), 1067: (206316, 206326, 206336),
    1070: (206317, 206327, 206337), 1107: (206318, 206328, 206338),
    1115: (206319, 206329, 206345), 1128: (206320, 206330, 206346),
    1133: (206350, 206360, 206347), 1143: (206351, 206361, 206348),
    1137: (206352, 206362, 206349),
}

EXHIBITIONS = {24: 2901, 36: 2902, 54: 2903, 60: 2904}
EXHIBITION_NUM = {2901: 1, 2902: 2, 2903: 3, 2904: 4}
NIEL, ARC_CLASSIC, FOY, ARC_SENIOR = 2905, 2906, 2907, 2908
LONGCHAMP = (NIEL, ARC_CLASSIC, FOY, ARC_SENIOR)
LONGCHAMP_TURN = {NIEL: 41, ARC_CLASSIC: 43, FOY: 65, ARC_SENIOR: 67}
DERBY, TAKARAZUKA = 166, 74
EXHIBITION_FIELD = {2901: (5, 0, 12), 2902: (5, 0, 12), 2903: (6, PARK_CLASSIC, 14),
                    2904: (7, PARK_CLASSIC, 15)}

NPC_BANDS = {
    2901: ((0.25, 0.30), (0.28, 0.32), (0.25, 0.30), (0.25, 0.30), (0.25, 0.30)),
    2902: ((0.45, 0.50),) * 5,
    2903: ((0.78, 0.83), (0.80, 0.85), (0.80, 0.84), (0.80, 0.85), (0.80, 0.85)),
    2904: ((1.00, 1.05), (0.95, 1.00), (0.95, 1.00), (0.90, 0.95), (0.95, 1.00)),
    NIEL: ((0.59, 0.64), (0.64, 0.69), (0.55, 0.60), (0.65, 0.70), (0.67, 0.71)),
    ARC_CLASSIC: ((0.61, 0.66), (0.64, 0.69), (0.60, 0.65), (0.65, 0.70), (0.67, 0.72)),
    FOY: ((1.37, 1.41), (1.08, 1.13), (1.28, 1.33), (1.03, 1.07), (1.12, 1.16)),
    ARC_SENIOR: ((1.41, 1.46), (1.15, 1.20), (1.30, 1.35), (1.03, 1.08), (1.15, 1.20)),
}

RACE_RESULT = {
    2901: {True: (206009, 400006007, 3, 10, 50), False: (206010, 400006008, 0, 10, 30)},
    2902: {True: (206014, 400006012, 5, 20, 50), False: (206015, 400006013, 0, 20, 30)},
    2903: {True: (206019, 400006027, 7, 30, 80), False: (206020, 400006028, 0, 30, 50)},
    2904: {True: (206023, 400006030, 10, 40, 100), False: (206024, 400006031, 0, 40, 70)},
    NIEL: {True: (206046, 400006016, 5, 40, 50), False: (206047, 400006017, 3, 40, 30)},
    ARC_CLASSIC: {True: (206049, 400006019, 7, 50, 80), False: (206050, 400006020, 5, 50, 50)},
    FOY: {True: (206027, 400006033, 7, 40, 100), False: None},
    DERBY: {True: (206098, 400006072, 5, 45, 0), False: (206099, 400006073, 3, 45, 0)},
    TAKARAZUKA: {True: (206342, 400006075, 5, 45, 0), False: (206343, 400006076, 3, 45, 0)},
}
ARC_SENIOR_WON = (206031, 400006037, 10, 60)
ARC_SENIOR_LOST = (206032, 400006038, 5, 40)
ARC_SENIOR_WON_DOUBLE = (206096, 400006070, 30, 140)
ARC_SENIOR_WON_DOUBLE_NO_LV2 = (206093, 400006040, 10, 60)
ARC_SENIOR_LOST_AFTER_WIN = (206094, 400006041, 5, 40)
PRE_RACE = {NIEL: (206045, 400006015), ARC_CLASSIC: (206048, 400006018),
            FOY: (206026, 400006032), ARC_SENIOR: (206030, 400006036),
            DERBY: (206097, 400006071), TAKARAZUKA: (206341, 400006074)}
PRE_ARC_AFTER_WIN = (206092, 400006039)
PRE_ARC_LINK = {1014: (206138, 400006300), 1007: (206142, 400006304),
                1067: (206144, 400006306), 1107: (206150, 400006312)}
EXHIBITION_OPEN = {2901: (206008, 400006006), 2902: (206013, 400006011),
                   2903: (206018, 400006026), 2904: (206022, 400006029)}

SKILL_ARC_GOLD, SKILL_ARC_WHITE = 210291, 210292
ARC_DREAM_HINTS = (20013, 20273, 20019)


@functools.lru_cache(maxsize=1)
def potential_conditions() -> dict:
    out = collections.defaultdict(list)
    for r in master_data.query(
            "SELECT id, potential_id, num, condition_type_1, condition_value_1, condition_value_2, "
            "condition_value_3, display_total_count FROM single_mode_arc_potential_cond ORDER BY potential_id, num"):
        out[int(r["potential_id"])].append(
            (int(r["id"]), int(r["num"]), int(r["condition_type_1"]), int(r["condition_value_1"]),
             int(r["condition_value_2"]), int(r["condition_value_3"]), int(r["display_total_count"])))
    return dict(out)


@functools.lru_cache(maxsize=1)
def potential_costs() -> dict:
    return {(int(r["potential_id"]), int(r["potential_lv"])): int(r["global_exp"])
            for r in master_data.query("SELECT potential_id, potential_lv, global_exp "
                                       "FROM single_mode_arc_potential_level")}


def max_level(potential_id: int) -> int:
    return max([lv for (p, lv) in potential_costs() if p == int(potential_id)] or [1])


@functools.lru_cache(maxsize=1)
def potential_bonus() -> list:
    return [(int(r["potential_id"]), int(r["potential_lv"]), int(r["effect_type"]),
             int(r["effect_value_1"]), int(r["effect_value_2"]), int(r["effect_value_3"]),
             int(r["effect_value_4"]))
            for r in master_data.query("SELECT * FROM single_mode_arc_potential_bonus ORDER BY id")]


@functools.lru_cache(maxsize=1)
def potential_buff() -> dict:
    return {(int(r["potential_id"]), int(r["potential_lv"])):
            tuple(int(r[k]) for k in ("speed", "stamina", "power", "guts", "wiz", "turf", "middle"))
            for r in master_data.query("SELECT * FROM single_mode_arc_potential_buff")}


@functools.lru_cache(maxsize=1)
def debuff() -> dict:
    return {int(r["id"]): (int(r["potential_id"]), int(r["potential_lv"]),
                           *(int(r[k]) for k in ("speed", "stamina", "power", "guts", "wiz",
                                                 "turf", "middle")))
            for r in master_data.query("SELECT * FROM single_mode_arc_debuff")}


@functools.lru_cache(maxsize=1)
def race_debuffs() -> dict:
    out = collections.defaultdict(list)
    for r in master_data.query("SELECT race_instance_id, debuff_id FROM single_mode_arc_race_debuff ORDER BY id"):
        out[int(r["race_instance_id"])].append(int(r["debuff_id"]))
    return dict(out)


@functools.lru_cache(maxsize=1)
def training_eff_rows() -> tuple:
    return tuple((int(r["require_pt_min"]), int(r["require_pt_max"]), int(r["training_eff"]))
                 for r in master_data.query("SELECT * FROM single_mode_arc_training_eff ORDER BY id"))


@functools.lru_cache(maxsize=1)
def win_points() -> dict:
    out = collections.defaultdict(list)
    for r in master_data.query("SELECT * FROM single_mode_arc_win_pt"):
        out[(int(r["race_instance_id"]), int(r["grade"]))].append(
            (int(r["order_min"]), int(r["order_max"]), int(r["point_num"])))
    return dict(out)


@functools.lru_cache(maxsize=1)
def peff_groups() -> dict:
    return {int(r["id"]): (int(r["icon_id"]), int(r["sub_id"]), int(r["effect_id_1"]), int(r["effect_id_2"]))
            for r in master_data.query("SELECT * FROM single_mode_arc_taiman_peff_grp")}


@functools.lru_cache(maxsize=1)
def link_charas() -> tuple:
    return tuple(sorted(int(r["chara_id"]) for r in master_data.query(
        "SELECT chara_id FROM single_mode_special_chara WHERE scenario_id=?", (SCENARIO_ID,))
        if int(r["chara_id"]) != MEI))


@functools.lru_cache(maxsize=1)
def npc_partners() -> tuple:
    return tuple((int(r["partner_id"]), int(r["chara_id"])) for r in master_data.query(
        "SELECT partner_id, chara_id FROM single_mode_unique_chara WHERE scenario_id=? ORDER BY partner_id",
        (SCENARIO_ID,)))


@functools.lru_cache(maxsize=4096)
def scout_for_card(support_card_id: int) -> dict | None:
    r = master_data.query_one("SELECT * FROM single_mode_scout_chara WHERE support_card_id=?",
                              (int(support_card_id),))
    return dict(r) if r else None


@functools.lru_cache(maxsize=512)
def scout_r_card(chara_id: int) -> dict | None:
    r = master_data.query_one(
        "SELECT s.* FROM single_mode_scout_chara s JOIN support_card_data c ON c.id=s.support_card_id "
        "WHERE s.chara_id=? AND c.rarity=1 ORDER BY s.support_card_id LIMIT 1", (int(chara_id),))
    return dict(r) if r else None


@functools.lru_cache(maxsize=1)
def fill_pool() -> tuple:
    return tuple(sorted({int(r["chara_id"]) for r in master_data.query(
        "SELECT DISTINCT s.chara_id FROM single_mode_scout_chara s JOIN support_card_data c "
        "ON c.id=s.support_card_id WHERE c.rarity=1 AND s.chara_id BETWEEN 1000 AND 1999")}))


@functools.lru_cache(maxsize=4096)
def hint_skills(support_card_id: int) -> tuple:
    return tuple(int(r["hint_value_1"]) for r in master_data.query(
        "SELECT hint_value_1 FROM single_mode_hint_gain WHERE support_card_id=? AND hint_gain_type=0 "
        "ORDER BY hint_group", (int(support_card_id),)))


@functools.lru_cache(maxsize=16)
def npc_row(row_id: int) -> dict | None:
    r = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?", (int(row_id),))
    return dict(r) if r else None


@functools.lru_cache(maxsize=1)
def overseas_mobs() -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM single_mode_npc WHERE npc_group_id=? ORDER BY id", (MOB_GROUP,)))


@functools.lru_cache(maxsize=64)
def program_instance(program_id: int) -> int:
    r = master_data.query_one("SELECT race_instance_id FROM single_mode_program WHERE id=?",
                              (int(program_id),))
    return int(r["race_instance_id"]) if r else 0


@functools.lru_cache(maxsize=512)
def program_grade(program_id: int) -> int:
    r = master_data.query_one(
        "SELECT r.grade AS g FROM single_mode_program p JOIN race_instance ri ON ri.id=p.race_instance_id "
        "JOIN race r ON r.id=ri.race_id WHERE p.id=?", (int(program_id),))
    return int(r["g"]) if r and r["g"] is not None else 0
