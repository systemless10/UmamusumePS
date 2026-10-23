
from __future__ import annotations

import copy
import math
import random

from .. import tuning
from . import conditions as conditions_mod

STAT_KEYS = ("speed", "stamina", "power", "guts", "wiz")

PENDING_EVENTS_KEY = "pending_event_ids"

EXTRA_EVENTS_KEY = "extra_turn_events"

EVENT_EFFECTS: dict[int, dict] = {
    3000: {"skill_point": 120},
    4000: {"skill_point": 120},
}

INTRO_CHAIN = [3000, 1001]


_SCENARIO_STORY = {
    1001: 400000400,
    1013: 400001401,
    1014: 400001402,
    1015: 400001403,
    102005: 400001422,
    1016: 400001404,
    1002: 400000009,
    1003: 400000010,
    1004: 400000011,
    1005: 400000012,
    1007: 400000031,
    1008: 400000032,
    1009: 400000033,
    1010: 400000034,
    1026: 400000005,
    202056: 400000005, 202057: 400000005, 202058: 400000005,
    202059: 400000005, 202060: 400000005,
    202061: 400000009, 202062: 400000010, 202063: 400000011, 202064: 400000012,
    202065: 400003100, 202066: 400003101, 202067: 400003102, 202068: 400003103,
    203019: 400000005, 203020: 400000005, 203021: 400000005,
    203022: 400000005, 203023: 400000005,
    203046: 400000009, 203047: 400000010, 203048: 400000011, 203049: 400000012,
    214044: 400000009, 214045: 400000010, 214046: 400000011, 214047: 400000012,
    212012: 400000009, 212013: 400000010, 212014: 400000011, 212015: 400000012,
    212063: 400012100, 212064: 400012101, 212065: 400012102, 212066: 400012103,
    214069: 400014104, 214070: 400014105, 214071: 400014106, 214072: 400014107,
    213819: 400000009, 213820: 400000010, 213821: 400000011, 213822: 400000012,
    213823: 400013100, 213824: 400013101, 213825: 400013102, 213826: 400013103,
    211062: 400000009, 211063: 400000010, 211064: 400000011, 211065: 400000012,
    211080: 400011100, 211081: 400011101, 211082: 400011102, 211083: 400011103,
    209058: 400000009, 209059: 400000010, 209060: 400000011, 209061: 400000012,
    208039: 400000009, 208040: 400000010, 208041: 400000011, 208042: 400000012,
    207108: 400000009, 207109: 400000010, 207110: 400000011, 207111: 400000012,
    207100: 400007100, 207101: 400007101, 207102: 400007102,
    205113: 400000009, 205114: 400000010, 205115: 400000011, 205116: 400000012,
}

_PLAY_TIMING = {
    1014: 6, 1015: 6, 102005: 6, 1016: 6,
    1002: 6, 1003: 6, 1004: 6, 1005: 6,
    1007: 6, 1008: 6, 1009: 6, 1010: 6,
    1026: 6,
    202056: 6, 202057: 6, 202058: 6, 202059: 6, 202060: 6,
    202061: 6, 202062: 6, 202063: 6, 202064: 6,
    202065: 6, 202066: 6, 202067: 6, 202068: 6,
    203019: 6, 203020: 6, 203021: 6, 203022: 6, 203023: 6,
    203046: 6, 203047: 6, 203048: 6, 203049: 6,
    214044: 6, 214045: 6, 214046: 6, 214047: 6,
    212012: 6, 212013: 6, 212014: 6, 212015: 6,
    212063: 6, 212064: 6, 212065: 6, 212066: 6,
    214069: 6, 214070: 6, 214071: 6, 214072: 6,
    213819: 6, 213820: 6, 213821: 6, 213822: 6,
    213823: 6, 213824: 6, 213825: 6, 213826: 6,
    211062: 6, 211063: 6, 211064: 6, 211065: 6,
    211080: 6, 211081: 6, 211082: 6, 211083: 6,
    209058: 6, 209059: 6, 209060: 6, 209061: 6,
    208039: 6, 208040: 6, 208041: 6, 208042: 6,
    207108: 6, 207109: 6, 207110: 6, 207111: 6, 207100: 6, 207101: 6, 207102: 6,
    205113: 6, 205114: 6, 205115: 6, 205116: 6,
}

SCENARIO_SCHEDULE: dict[int, list[int]] = {
    2: [1013],
    3: [1014],
    4: [1015, 102005],
}


_CHARA_EVENT_STORY_SUFFIX = {3000: 100, 4000: 900}


def _story_id_for(event_id: int, chara_id: int) -> int:
    if event_id in _SCENARIO_STORY:
        return _SCENARIO_STORY[event_id]
    if event_id in _CHARA_EVENT_STORY_SUFFIX and chara_id:
        return int(f"50{chara_id}{_CHARA_EVENT_STORY_SUFFIX[event_id]:03d}")
    return 0


def _wire_story_id(story_id: int) -> int:
    from .. import event_engine
    return event_engine.wire_story_id(story_id)


def is_scenario_event(event_id: int) -> bool:
    return event_id in _SCENARIO_STORY


def event_entry(event_id: int, chara_id: int) -> dict:
    is_scenario = event_id in _SCENARIO_STORY
    return {
        "event_id": event_id,
        "chara_id": 0 if is_scenario else chara_id,
        "story_id": _wire_story_id(_story_id_for(event_id, chara_id)),
        "play_timing": _PLAY_TIMING.get(event_id, 1),
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [{
                "select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                "gain_select_id_index": 1, "select_icon": 0,
            }],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None,
        "minigame_result": None,
    }


_STAT_STATUS_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}


def apply_effect(chara_info: dict, event_id: int) -> list:
    eff = EVENT_EFFECTS.get(event_id)
    if not eff:
        return []
    not_up = []
    for key, val in eff.items():
        if key == "skill_point":
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + val
        elif key in STAT_KEYS:
            cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}", 9999)
            if val > 0 and chara_info.get(key, 0) >= cap:
                not_up.append(_STAT_STATUS_TYPE[key])
            chara_info[key] = min(chara_info.get(key, 0) + val, cap)
        elif key == "vital":
            max_vital = chara_info.get("max_vital", 100)
            if val > 0 and chara_info.get("vital", 100) >= max_vital:
                not_up.append(6)
            chara_info["vital"] = max(0, min(max_vital,
                                             chara_info.get("vital", 100) + val))
    return sorted(not_up)


NPC_UNLOCKS: dict[int, list[tuple[int, int]]] = {
    1014: [(102, 9002)],
    102005: [(2001, 2001)],
    1016: [(103, 9003)],
    101007: [(103, 9003)],
    202002: [(105, 9008), (102, 9002)],
    214001: [(102, 9002)],
    214002: [(101, 9001)],
    214048: [(103, 9003)],
    212002: [(102, 9002)],
    212003: [(114, 9050)],
    212016: [(103, 9003)],
    213001: [(102, 9002)],
    213816: [(103, 9003)],
    211006: [(102, 9002)],
    211079: [(113, 9049)],
    211022: [(103, 9003)],
    205001: [(102, 9002)],
    205034: [(103, 9003)],
    209026: [(102, 9002)],
    209035: [(103, 9003)],
    208043: [(102, 9002)],
    208053: [(103, 9003)],
    207019: [(102, 9002), (111, 9044)],
    203026: [(102, 9002), (103, 9003)],
    206002: [(102, 9002), (110, 9043)],
    206100: [(103, 9003)],
}

APPRAISAL_EVENTS: dict[int, list[tuple[int, int, int]]] = {
    102: [(90, 1005, 5), (70, 1004, 4), (40, 1003, 3), (0, 1002, 2)],
    103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
}
APPRAISAL_BOND_GAIN = 7
APPRAISAL_BOND_GAIN_BY_NPC = {102: 5, 103: 7, 111: 5, 101: 5, 114: 5}


def appraisal_bond_gain(target_id) -> int:
    return APPRAISAL_BOND_GAIN_BY_NPC.get(int(target_id or 0), APPRAISAL_BOND_GAIN)


def appraisal_for(target_id: int, bond: int, table: dict | None = None):
    for min_bond, event_id, amount in (table or APPRAISAL_EVENTS).get(target_id, ()):
        if bond >= min_bond:
            return event_id, amount
    return None
VERSUS_NPCS = {2001}
UNLOCKED_NPCS_KEY = "unlocked_versus_npcs"

_URA_FACILITY_COMMANDS = [101, 105, 102, 103, 106]
_REAL_NPC_TARGETS = {102, 103, 2001}


def npcs_unlocked_by(event_id: int) -> list:
    return NPC_UNLOCKS.get(event_id, [])


def npc_placements(turn: int, target_ids) -> dict:
    import random
    placement = {}
    for tid in target_ids or ():
        pick = random.Random((int(turn) << 12) ^ int(tid)).choices(
            range(6), weights=[100, 100, 100, 100, 100, 50], k=1)[0]
        if pick == 5:
            continue
        fac = _URA_FACILITY_COMMANDS[pick]
        if fac not in placement:
            placement[fac] = tid
    return placement


MAX_DUELS = 5
DUEL_LAST_TURN_KEY = "duel_last_turn"


def _has_meek(unlocked) -> bool:
    return any((n[0] if isinstance(n, (list, tuple)) else n) == 2001 for n in (unlocked or ()))


def meek_facility(turn) -> int:
    return _URA_FACILITY_COMMANDS[int(turn) % len(_URA_FACILITY_COMMANDS)]


def meek_duel_offered(turn, unlocked, versus_level: int = 1, last_duel_turn=None) -> bool:
    if turn is None or not _has_meek(unlocked):
        return False
    if versus_level > MAX_DUELS:
        return False
    if last_duel_turn is not None and int(turn) <= int(last_duel_turn):
        return False
    return (int(turn) * 7 + 3) % 5 < 2


def happy_meek_facility(turn, unlocked, versus_level: int = 1, last_duel_turn=None):
    if meek_duel_offered(turn, unlocked, versus_level, last_duel_turn):
        return meek_facility(turn)
    return None


def _reconcile_npc_appearance(data: dict, unlocked=()) -> None:
    ci = data.get("chara_info")
    hi = data.get("home_info")
    if not (isinstance(ci, dict) and isinstance(hi, dict)):
        return
    unlocked_ids = {(n[0] if isinstance(n, (list, tuple)) else n) for n in unlocked or ()}
    from .. import scenarios
    real = _REAL_NPC_TARGETS | set(scenarios.for_chara(ci).npc_targets)
    ev = [e for e in ci.get("evaluation_info_array") or []
          if not (isinstance(e, dict) and isinstance(e.get("target_id"), int)
                  and e["target_id"] >= 100 and e["target_id"] not in real)]
    ci["evaluation_info_array"] = ev
    seen = set()
    for e in ev:
        if isinstance(e, dict) and e.get("target_id") in real:
            e["is_appear"] = 1 if e["target_id"] in unlocked_ids else 0
            seen.add(e["target_id"])
    for pid in (unlocked_ids & real) - seen:
        ev.append({"target_id": pid, "training_partner_id": pid, "evaluation": 0,
                   "is_outing": 0, "story_step": 0, "is_appear": 1,
                   "group_outing_info_array": []})


def apply_versus_state(data: dict, turn, unlocked, versus_level: int = 1, last_duel_turn=None):
    uds0 = data.get("ura_data_set")
    if isinstance(uds0, dict):
        ci0 = data.get("chara_info") or {}
        rows = []
        from .. import master_data as _md
        for c in ci0.get("support_card_array") or []:
            pos, sid = c.get("position"), c.get("support_card_id")
            if not pos:
                continue
            row = _md.query_one(
                "SELECT chara_id FROM support_card_data WHERE id=?", (sid,))
            rows.append({"target_id": pos,
                         "chara_id": row["chara_id"] if row else 0,
                         "member_state": 0})
        for n in unlocked or ():
            tid, cid = (n[0], n[1]) if isinstance(n, (list, tuple)) else (n, n)
            rows.append({"target_id": tid, "chara_id": cid,
                         "member_state": 1 if tid == 2001 else 0})
        uds0["evaluation_info_array"] = rows
    if not _has_meek(unlocked):
        _reconcile_npc_appearance(data, unlocked)
        return None

    uds = data.get("ura_data_set")
    if isinstance(uds, dict):
        uev = uds.setdefault("evaluation_info_array", [])
        if not any(isinstance(e, dict) and e.get("target_id") == 2001 for e in uev):
            uev.append({"target_id": 2001, "chara_id": 2001, "member_state": 1})
    ci = data.get("chara_info")
    if isinstance(ci, dict):
        cev = ci.setdefault("evaluation_info_array", [])
        if not any(isinstance(e, dict) and e.get("target_id") == 2001 for e in cev):
            cev.append({"target_id": 2001, "training_partner_id": 2001, "evaluation": 0,
                        "is_outing": 0, "story_step": 0, "is_appear": 0,
                        "group_outing_info_array": []})

    duel_fac = None
    if turn is not None:
        fac = meek_facility(turn)
        hi = data.get("home_info")
        if isinstance(hi, dict):
            for c in hi.get("command_info_array") or []:
                if isinstance(c, dict) and c.get("command_id") == fac:
                    tp = c.setdefault("training_partner_array", [])
                    if 2001 not in tp:
                        tp.append(2001)
        if isinstance(uds, dict) and meek_duel_offered(turn, unlocked, versus_level, last_duel_turn):
            for c in uds.get("command_info_array") or []:
                if isinstance(c, dict) and c.get("command_id") == fac:
                    c["versus_event_partner_id"] = 2001
            duel_fac = fac

    _reconcile_npc_appearance(data, unlocked)
    return duel_fac


def choice_reward(event_id: int, choice_number: int) -> dict:
    return {"choice_reward_array": []}


NORMAL_FAIL_EVENT = 7014
WORST_FAIL_EVENT = 7015
_FAIL_STORY_SUFFIX = {7014: 713, 7015: 714}
FAIL_CTX_KEY = "active_failure"


def failure_event_entry(event_id: int, chara_id: int, worst: bool) -> dict:
    return {
        "event_id": event_id,
        "chara_id": chara_id,
        "story_id": int(f"50{chara_id}{_FAIL_STORY_SUFFIX[event_id]:03d}"),
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [
                {"select_index": 2 if worst else 1, "receive_item_id": 0,
                 "target_race_id": 0, "gain_select_id_index": 1, "select_icon": 0},
                {"select_index": 1, "receive_item_id": 0,
                 "target_race_id": 0, "gain_select_id_index": 2, "select_icon": 0},
            ],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def resolve_failure(chara_info: dict, worst: bool, choice_number: int, trained_stat: str) -> dict:
    import random
    is_bottom = (choice_number == 2)
    others = [s for s in STAT_KEYS if s != trained_stat]
    mv = chara_info.get("max_vital", 100)

    def dock(stat, amt):
        chara_info[stat] = max(0, chara_info.get(stat, 0) - amt)

    def drop_mood(n):
        conditions_mod.drop_mood(chara_info, n)

    if worst:
        if is_bottom and random.random() < tuning.good(0.03):
            chara_info["vital"] = min(mv, chara_info.get("vital", 0) + 10)
            add_condition(chara_info, COND_PRACTICE_PERFECT)
            return {"outcome": "worst_bottom_perfect"}
        drop_mood(3)
        if not is_bottom:
            chara_info["vital"] = min(mv, chara_info.get("vital", 0) + 10)
        dock(trained_stat, 10)
        for s in random.sample(others, 2):
            dock(s, 10)
        if is_bottom or random.random() < tuning.bad(0.40):
            add_condition(chara_info, COND_PRACTICE_POOR)
        return {"outcome": "worst_bottom" if is_bottom else "worst_top"}

    if is_bottom and random.random() < tuning.good(0.20):
        add_condition(chara_info, COND_PRACTICE_PERFECT)
        return {"outcome": "normal_bottom_perfect"}
    drop_mood(1)
    if not is_bottom:
        dock(trained_stat, 5)
        if random.random() < tuning.bad(0.08):
            add_condition(chara_info, COND_PRACTICE_POOR)
        return {"outcome": "normal_top"}
    dock(trained_stat, 10)
    if random.random() < tuning.bad(0.65):
        add_condition(chara_info, COND_PRACTICE_POOR)
    return {"outcome": "normal_bottom"}


_G = lambda did, e0, e1: {"display_id": did, "effect_value_0": e0, "effect_value_1": e1, "effect_value_2": 0}
_NORMAL_FAIL_PREVIEW = [
    {"select_index": 1, "gain_param_array": [_G(2, 20, 1), _G(20, 5, 0)]},
    {"select_index": 1, "gain_param_array": [_G(2, 20, 1), _G(37, 6, 0), _G(20, 5, 0)]},
    {"select_index": 2, "gain_param_array": [_G(2, 20, 1), _G(20, 10, 0)]},
    {"select_index": 2, "gain_param_array": [_G(20, 10, 0), _G(37, 6, 0), _G(2, 20, 1)]},
    {"select_index": 2, "gain_param_array": [_G(9, 10, 0)]},
]
_WORST_FAIL_PREVIEW = [
    {"select_index": 1, "gain_param_array": [_G(1, 10, 10), _G(20, 10, 0), _G(34, 2, 10), _G(2, 20, 3)]},
    {"select_index": 1, "gain_param_array": [_G(1, 10, 10), _G(2, 20, 3), _G(20, 10, 0), _G(34, 2, 10), _G(37, 6, 0)]},
    {"select_index": 2, "gain_param_array": [_G(2, 20, 3), _G(20, 10, 0), _G(34, 2, 10), _G(37, 6, 0)]},
    {"select_index": 2, "gain_param_array": [_G(9, 10, 0), _G(1, 10, 10)]},
]


def failure_preview(worst: bool, trained_stat: str = "speed",
                    chara_info: dict | None = None) -> list:
    preview = copy.deepcopy(_WORST_FAIL_PREVIEW if worst else _NORMAL_FAIL_PREVIEW)
    poor_guard = conditions_mod.bad_guard_preview(chara_info, COND_PRACTICE_POOR)
    mood_guard = conditions_mod.mood_guard_preview(chara_info)
    for choice in preview:
        for g in choice["gain_param_array"]:
            if poor_guard and g["display_id"] == 37 and g["effect_value_0"] == COND_PRACTICE_POOR:
                g.update(display_id=40, effect_value_1=poor_guard)
            elif mood_guard and g["display_id"] == 2 and g["effect_value_0"] == 20:
                g.update(display_id=38, effect_value_2=mood_guard)
    return preview


CONDITION_NAMES = {
    1: "Night Owl", 2: "Slacker", 3: "Skin Outbreak", 4: "Slow Metabolism",
    5: "Migraine", 6: "Practice Poor", 7: "Fast Learner", 8: "Charming",
    9: "Hot Topic", 10: "Practice Perfect", 11: "Practice Perfect (◎)",
    12: "Under the Weather", 13: "Shining Brightly", 19: "Not Ready",
    20: "Legs of Glass", 21: "Ominous Portent",
}
COND_PRACTICE_POOR = 6
COND_PRACTICE_PERFECT = 10
_COND_EXCLUSIVE_GROUP = {6: "practice", 10: "practice", 11: "practice",
                         7: "learner", 40: "learner"}


def conditions(chara_info: dict) -> list:
    return chara_info.setdefault("chara_effect_id_array", [])


def has_condition(chara_info: dict, cond_id: int) -> bool:
    return cond_id in (chara_info.get("chara_effect_id_array") or [])


def add_condition(chara_info: dict, cond_id: int) -> None:
    arr = conditions(chara_info)
    if cond_id not in arr and conditions_mod.blocks_bad_condition(chara_info, cond_id):
        return
    grp = _COND_EXCLUSIVE_GROUP.get(cond_id)
    if grp:
        arr[:] = [c for c in arr if _COND_EXCLUSIVE_GROUP.get(c) != grp]
    if cond_id not in arr:
        arr.append(cond_id)


def remove_condition(chara_info: dict, cond_id: int) -> None:
    arr = chara_info.get("chara_effect_id_array")
    if arr and cond_id in arr:
        arr.remove(cond_id)


DUEL_EVENT_ID = 102001
_DUEL_STORY_ID = 400001418
DUEL_CTX_KEY = "active_duel"
VERSUS_LEVEL_KEY = "versus_level"

_STAT_IDX_TO_KEY = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
_RACING_SPIRIT = {1: (210091, 21009), 2: (210101, 21010), 3: (210111, 21011),
                  4: (210121, 21012), 5: (210131, 21013)}
_HAPPY_MEEK_STATS = {
    1: {"speed": 252, "stamina": 231, "power": 249, "guts": 220, "wiz": 228},
    2: {"speed": 403, "stamina": 354, "power": 398, "guts": 352, "wiz": 364},
}
for _lvl in (3, 4, 5):
    _HAPPY_MEEK_STATS[_lvl] = {
        _s: _HAPPY_MEEK_STATS[2][_s] + (_lvl - 2) * (_HAPPY_MEEK_STATS[2][_s] - _HAPPY_MEEK_STATS[1][_s])
        for _s in STAT_KEYS
    }


def happy_meek_stats(level: int) -> dict:
    return dict(_HAPPY_MEEK_STATS.get(
        level, _HAPPY_MEEK_STATS[max(_HAPPY_MEEK_STATS)]))


def duel_stat_options(versus_level: int) -> list:
    order = [3, 1, 4, 2, 5]
    start = (max(1, versus_level) - 1) % 5
    return [order[(start + i) % 5] for i in range(3)]


def duel_event_entry(stat_options: list, chara_info: dict | None = None,
                     versus_level: int = 1) -> dict:
    def icon(s):
        return duel_icon(chara_info, s, versus_level) if chara_info else 2

    return {
        "event_id": DUEL_EVENT_ID, "chara_id": 0, "story_id": _DUEL_STORY_ID,
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [
                {"select_index": s, "receive_item_id": 0, "target_race_id": 0,
                 "gain_select_id_index": s, "select_icon": icon(s)}
                for s in stat_options
            ],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


DUEL_ICON_X, DUEL_ICON_CIRCLE, DUEL_ICON_DOUBLE = 1, 3, 4


_DUEL_SCALE = 100.0


def duel_win_probability(chara_info: dict, stat_index: int, versus_level: int) -> float:
    key = _STAT_IDX_TO_KEY.get(stat_index)
    if not key:
        return 0.0
    npc = _HAPPY_MEEK_STATS.get(versus_level, _HAPPY_MEEK_STATS[max(_HAPPY_MEEK_STATS)])
    gap = (chara_info.get(key, 0) or 0) - (npc.get(key, 0) or 0)
    return min(0.90, max(0.15, 1.0 / (1.0 + math.exp(-gap / _DUEL_SCALE))))


def duel_icon(chara_info: dict, stat_index: int, versus_level: int) -> int:
    p = duel_win_probability(chara_info, stat_index, versus_level)
    if p < 0.25:
        return DUEL_ICON_X
    if p < 0.75:
        return DUEL_ICON_CIRCLE
    return DUEL_ICON_DOUBLE


def _duel_won(chara_info: dict, stat_index: int, versus_level: int) -> bool:
    return random.random() < duel_win_probability(chara_info, stat_index, versus_level)


_DUEL_WIN_STAT_RANGE = (10, 25)
_DUEL_LOSE_STAT_RANGE = (5, 15)
_DUEL_WIN_SP = 30
_DUEL_LOSE_SP = 15


def _duel_gains(stat_index: int, won: bool, stat_up: int | None = None) -> list:
    if won:
        skill_id, _g = _RACING_SPIRIT[stat_index]
        if stat_up is None:
            stat_up = sum(_DUEL_WIN_STAT_RANGE) // 2
        return [
            {"display_id": 1, "effect_value_0": 50 + stat_index, "effect_value_1": 4, "effect_value_2": 0},
            {"display_id": 1, "effect_value_0": stat_index, "effect_value_1": stat_up, "effect_value_2": 0},
            {"display_id": 1, "effect_value_0": 30, "effect_value_1": _DUEL_WIN_SP, "effect_value_2": 0},
            {"display_id": 6, "effect_value_0": skill_id, "effect_value_1": 1, "effect_value_2": 0},
        ]
    if stat_up is None:
        stat_up = sum(_DUEL_LOSE_STAT_RANGE) // 2
    return [
        {"display_id": 1, "effect_value_0": stat_index, "effect_value_1": stat_up, "effect_value_2": 0},
        {"display_id": 1, "effect_value_0": 30, "effect_value_1": _DUEL_LOSE_SP, "effect_value_2": 0},
    ]


def duel_preview(stat_options: list) -> list:
    arr = []
    for s in stat_options:
        arr.append({"select_index": s, "gain_param_array": _duel_gains(s, won=True)})
        arr.append({"select_index": s, "gain_param_array": _duel_gains(s, won=False)})
    return arr


def resolve_duel(chara_info: dict, stat_index: int, versus_level: int) -> dict:
    if stat_index not in _STAT_IDX_TO_KEY:
        stat_index = 1
    key = _STAT_IDX_TO_KEY[stat_index]
    won = _duel_won(chara_info, stat_index, versus_level)
    stat_up = random.randint(*(_DUEL_WIN_STAT_RANGE if won else _DUEL_LOSE_STAT_RANGE))
    sp_up = _DUEL_WIN_SP if won else _DUEL_LOSE_SP
    gains = _duel_gains(stat_index, won, stat_up)
    cap_key = "max_wiz" if key == "wiz" else f"max_{key}"
    if won:
        chara_info[cap_key] = chara_info.get(cap_key, 1200) + 4
        _skill_id, group_id = _RACING_SPIRIT[stat_index]
        tips = chara_info.setdefault("skill_tips_array", [])
        if not any(t.get("group_id") == group_id for t in tips):
            tips.append({"group_id": group_id, "rarity": 1, "level": 1})
    chara_info[key] = min(chara_info.get(key, 0) + stat_up, chara_info.get(cap_key, 9999))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + sp_up
    return {"won": won, "choice_reward_array": [{"select_index": stat_index, "gain_param_array": gains}]}
