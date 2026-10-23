
from __future__ import annotations

import logging
import random

from ... import epithets
from ... import master_data

log = logging.getLogger("uma-server")

SCENARIO_ID = 4
STATE_KEY = "trackblazer"

CLIMAX_TURNS = (74, 76, 78)
CLIMAX_RACE_GROUPS = {74: 40001, 76: 40002, 78: 40003}

OBJECTIVE_TURNS = (24, 48, 72)

APT_B, APT_C = 6, 5

STATS = ("speed", "stamina", "power", "guts", "wiz")

TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)

BONUS_PREVIEW_KEY = "trackblazer_bonus_preview"


def stash_bonus_preview(full_state: dict, command_id, bonus: list) -> None:
    full_state.setdefault(BONUS_PREVIEW_KEY, {})[int(command_id)] = bonus


def take_bonus_preview(full_state: dict, command_id) -> list:
    return list((full_state.get(BONUS_PREVIEW_KEY) or {}).get(int(command_id)) or [])


def is_active(chara_info) -> bool:
    return int((chara_info or {}).get("scenario_id") or 0) == SCENARIO_ID


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = {}
        full_state[STATE_KEY] = st
    st.setdefault("coins", 0)
    st.setdefault("coins_total", 0)
    st.setdefault("win_points", 0)
    st.setdefault("win_points_total", 0)
    st.setdefault("prev_win_points", 0)
    st.setdefault("items_bought", 0)
    st.setdefault("gained_coin_num", 0)
    st.setdefault("items", {})
    st.setdefault("effects", [])
    st.setdefault("next_use_id", 1)
    st.setdefault("shop_id", 0)
    st.setdefault("offers", [])
    st.setdefault("next_shop_item_id", 1)
    st.setdefault("rival", {})
    st.setdefault("climax", {})
    st.setdefault("objectives", {})
    return st


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


_GROUND_APT = {1: "proper_ground_turf", 2: "proper_ground_dirt"}
_DISTANCE_APT = {3: "proper_distance_short", 4: "proper_distance_mile",
                 5: "proper_distance_middle", 6: "proper_distance_long"}


def _condition_met(cond, chara_info: dict) -> bool:
    kind = int(cond["condition_type_1"])
    field = (_GROUND_APT if kind == 1 else _DISTANCE_APT).get(
        int(cond["condition_value_1"]))
    if not field:
        return False
    return int(chara_info.get(field) or 0) >= int(cond["condition_value_2"] or APT_B)


def career_route(chara_info: dict):
    rows = master_data.query(
        "SELECT id, race_set_id, condition_set_id FROM single_mode_route "
        "WHERE scenario_id=? AND chara_id=0 ORDER BY priority DESC", (SCENARIO_ID,))
    for row in rows:
        cset = int(row["condition_set_id"] or 0)
        if cset:
            conds = master_data.query(
                "SELECT condition_type_1, condition_value_1, condition_value_2 "
                "FROM single_mode_route_condition WHERE condition_set_id=?", (cset,))
            if not conds or not all(_condition_met(c, chara_info) for c in conds):
                continue
        races = master_data.query(
            "SELECT id, condition_type, condition_value_1 FROM single_mode_route_race "
            "WHERE race_set_id=? ORDER BY sort_id", (row["race_set_id"],))
        chara_id = int(chara_info.get("card_id") or 0) // 100
        out = []
        for r in races:
            if int(r["condition_type"] or 0) == _CT_PER_CHARA_RACE:
                sub = _per_chara_route_race(chara_id, int(r["condition_value_1"] or 0))
                if sub:
                    out.append(sub)
                    continue
                log.warning("trackblazer: no per-chara route race for chara %s "
                            "group %s -- keeping row %s unresolved",
                            chara_id, r["condition_value_1"], r["id"])
            out.append(int(r["id"]))
        return int(row["id"]), out
    return None, []


_CT_PER_CHARA_RACE = 8
_URA_SCENARIO_ID = 1


def _per_chara_route_race(chara_id: int, group_id: int) -> int:
    if not chara_id or not group_id:
        return 0
    row = master_data.query_one(
        "SELECT route_race_id FROM single_mode_change_chara_route "
        "WHERE route_race_group_id=? AND chara_id=?", (int(group_id), int(chara_id)))
    if row:
        return int(row["route_race_id"] or 0)
    row = master_data.query_one(
        "SELECT rr.id FROM single_mode_route r "
        "JOIN single_mode_route_race rr ON rr.race_set_id = r.race_set_id "
        "WHERE r.scenario_id=? AND r.chara_id=? AND rr.condition_type=1 "
        "ORDER BY rr.sort_id LIMIT 1", (_URA_SCENARIO_ID, int(chara_id)))
    return int(row["id"]) if row else 0


def objective_target(chara_info: dict, turn: int) -> int:
    _route_id, race_ids = career_route(chara_info)
    if not race_ids:
        return 0
    marks = ",".join("?" * len(race_ids))
    row = master_data.query_one(
        f"SELECT condition_value_1 FROM single_mode_route_race WHERE id IN ({marks}) "
        "AND turn=? AND condition_type=7", (*race_ids, int(turn)))
    return int(row["condition_value_1"]) if row else 0


def grade_of(program_id):
    row = master_data.query_one(
        "SELECT r.grade FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE p.id=?", (program_id,))
    return int(row["grade"]) if row else None


def win_points_for(grade, result_rank: int, race_group_id: int = 0) -> int:
    row = master_data.query_one(
        "SELECT point_num FROM single_mode_free_win_point "
        "WHERE race_group_id=? AND grade=? AND order_min<=? AND order_max>=?",
        (int(race_group_id), int(grade or 0), int(result_rank), int(result_rank)))
    return int(row["point_num"]) if row else 0


def coins_for(grade, result_rank: int) -> int:
    row = master_data.query_one(
        "SELECT coin_num FROM single_mode_free_coin_race "
        "WHERE grade=? AND order_min<=? AND order_max>=?",
        (int(grade or 0), int(result_rank), int(result_rank)))
    return int(row["coin_num"]) if row else 0


def add_coins(st: dict, amount: int) -> None:
    amount = int(amount or 0)
    if amount <= 0:
        return
    st["coins"] = int(st.get("coins") or 0) + amount
    st["gained_coin_num"] = int(st.get("gained_coin_num") or 0) + amount
    st["coins_total"] = int(st.get("coins_total") or 0) + amount


def spend_coins(st: dict, amount: int) -> bool:
    amount = int(amount or 0)
    if amount > int(st.get("coins") or 0):
        return False
    st["coins"] = int(st.get("coins") or 0) - amount
    return True


def add_win_points(st: dict, amount: int) -> None:
    amount = int(amount or 0)
    st["prev_win_points"] = int(st.get("win_points") or 0)
    st["win_points"] = st["prev_win_points"] + amount
    if amount > 0:
        st["win_points_total"] = int(st.get("win_points_total") or 0) + amount


def consume_win_points(st: dict) -> int:
    had = int(st.get("win_points") or 0)
    st["prev_win_points"] = had
    st["win_points"] = 0
    return had


def objective_met(full_state: dict, chara_info: dict, turn: int) -> bool:
    st = state(full_state)
    key = str(int(turn))
    done = st.get("objectives") or {}
    if key in done:
        return bool(done[key])
    target = objective_target(chara_info, turn)
    got = consume_win_points(st)
    met = got >= target if target else True
    done[key] = met
    st["objectives"] = done
    st.setdefault("objective_points", {})[key] = got
    return met


_NPC_STAT_SCALE = 1.83

_FIXED_CLIMAX_NPCS = (2001901, 2002901, 2003901)
_CLIMAX_FIELD_SIZE = 15


def _skill_set(skill_set_id) -> list:
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    if row is None:
        return []
    out = []
    for i in range(1, 21):
        sid = row[f"skill_id{i}"]
        if sid:
            out.append({"skill_id": int(sid), "level": int(row[f"skill_level{i}"] or 1)})
    return out


def _npc_wire(row) -> dict:
    scale = _NPC_STAT_SCALE
    return {
        "npc_id": int(row["id"]),
        "chara_id": int(row["chara_id"]),
        "dress_id": int(row["race_dress_id"] or 0),
        "talent_level": 1,
        "win_points": 0,
        "speed": int(row["speed"] * scale),
        "stamina": int(row["stamina"] * scale),
        "power": int(row["pow"] * scale),
        "guts": int(row["guts"] * scale),
        "wiz": int(row["wiz"] * scale),
        "proper_distance_short": row["proper_distance_short"],
        "proper_distance_mile": row["proper_distance_mile"],
        "proper_distance_middle": row["proper_distance_middle"],
        "proper_distance_long": row["proper_distance_long"],
        "proper_running_style_nige": row["proper_running_style_nige"],
        "proper_running_style_senko": row["proper_running_style_senko"],
        "proper_running_style_sashi": row["proper_running_style_sashi"],
        "proper_running_style_oikomi": row["proper_running_style_oikomi"],
        "proper_ground_turf": row["proper_ground_turf"],
        "proper_ground_dirt": row["proper_ground_dirt"],
        "skill_array": _skill_set(row["skill_set_id"]),
    }


def ensure_climax_field(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    climax = st.get("climax") or {}
    if climax.get("npcs"):
        return climax["npcs"]
    player = int(chara_info.get("card_id") or 0) // 100
    pool = [int(r["id"]) for r in master_data.query(
        "SELECT id, chara_id FROM single_mode_npc WHERE id % 1000 = 900 ORDER BY id")
        if int(r["chara_id"]) != player and master_data.has_portrait_art(r["chara_id"])]
    rng = random.Random((player << 8) ^ 0x7B7B)
    want = _CLIMAX_FIELD_SIZE - len(_FIXED_CLIMAX_NPCS)
    draw = rng.sample(pool, min(want, len(pool)))
    npcs = []
    for npc_id in list(draw) + list(_FIXED_CLIMAX_NPCS):
        row = master_data.query_one(
            "SELECT * FROM single_mode_npc WHERE id=?", (npc_id,))
        if row is not None:
            npcs.append(_npc_wire(row))
    climax["npcs"] = npcs
    climax.setdefault("results", [])
    climax.setdefault("ranking", 0)
    st["climax"] = climax
    return npcs


def _npc_strength(npc: dict) -> float:
    return sum(int(npc.get(s) or 0) for s in STATS)


def record_climax_leg(full_state: dict, chara_info: dict, turn: int,
                      program_id, player_rank: int) -> None:
    st = state(full_state)
    npcs = ensure_climax_field(full_state, chara_info)
    climax = st["climax"]
    if any(int(r.get("turn") or 0) == int(turn) for r in climax.get("results") or []):
        return
    rng = random.Random((int(turn) << 12) ^ int(chara_info.get("card_id") or 0))
    ordered = sorted(npcs, key=lambda n: -(_npc_strength(n) + rng.uniform(-400, 400)))
    places = [p for p in range(1, len(npcs) + 2) if p != int(player_rank)]
    group = CLIMAX_RACE_GROUPS.get(int(turn), 0)
    result_array = []
    for npc, place in zip(ordered, places):
        npc["win_points"] = int(npc.get("win_points") or 0) + win_points_for(
            0, place, race_group_id=group)
        result_array.append({"npc_id": npc["npc_id"], "result_rank": place})
    add_win_points(st, win_points_for(0, int(player_rank), race_group_id=group))
    result_array.sort(key=lambda r: r["npc_id"])
    climax.setdefault("results", []).append(
        {"turn": int(turn), "program_id": int(program_id or 0),
         "player_rank": int(player_rank),
         "race_result_array": result_array})
    better = sum(1 for n in npcs
                 if int(n.get("win_points") or 0) > int(st.get("win_points") or 0))
    climax["pending_ranking"] = better + 1
    climax["pending_ranking_turn"] = int(turn)


_RANKING_HELD_ENDPOINTS = ("race_end", "race_out")


def promote_ranking(full_state: dict, endpoint: str = "") -> bool:
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" not in climax:
        return False
    if (endpoint or "").rsplit("/", 1)[-1] in _RANKING_HELD_ENDPOINTS:
        return False
    climax["ranking"] = int(climax.pop("pending_ranking") or 0)
    climax.pop("pending_ranking_turn", None)
    return True


def held_turn(full_state: dict) -> int:
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" not in climax:
        return 0
    return int(climax.get("pending_ranking_turn") or 0)


def publish_ranking(st: dict, endpoint: str = "") -> int:
    return int((st.get("climax") or {}).get("ranking") or 0)


def climax_ranking(full_state: dict) -> int:
    climax = (state(full_state).get("climax") or {})
    return int(climax.get("pending_ranking") or climax.get("ranking") or 0)


RIVAL_FIRST_TURN = 15
RIVAL_CHANCE = 0.80
RIVAL_WIN_STAT_BONUS = 5
RIVAL_WIN_STAT_COUNT = 2

_GRADED = (100, 200, 300)


def _program_course(program_id):
    return master_data.query_one(
        "SELECT cs.distance AS distance, cs.ground AS ground "
        "FROM single_mode_program p JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id "
        "JOIN race_course_set cs ON cs.id = r.course_set WHERE p.id=?", (program_id,))


def distance_field(metres) -> str:
    metres = int(metres or 0)
    if metres <= 1400:
        return "proper_distance_short"
    if metres <= 1800:
        return "proper_distance_mile"
    if metres <= 2400:
        return "proper_distance_middle"
    return "proper_distance_long"


def _drawable_chara_pool(player: int) -> list:
    return [int(r["chara_id"]) for r in master_data.query(
        "SELECT DISTINCT chara_id FROM single_mode_npc WHERE id % 1000 = 900 "
        "ORDER BY chara_id")
        if int(r["chara_id"]) != player and master_data.has_portrait_art(r["chara_id"])]


def tick(full_state: dict, chara_info: dict) -> bool:
    if not is_active(chara_info):
        return False
    from . import shop
    st = state(full_state)
    turn = int(chara_info.get("turn") or 1)
    changed = shop.refresh(full_state, turn)
    rival = st.get("rival") or {}
    if int(rival.get("turn") or 0) != turn:
        rival_for_turn(full_state, chara_info, turn,
                       _turn_race_program(chara_info, turn))
        changed = True
    if int(st.get("gained_coin_num") or 0):
        changed = True
    return changed


def _turn_race_programs(chara_info: dict, turn) -> list:
    try:
        from ...handlers import single_mode_team as smt
        rows = smt._build_race_condition_array(chara_info, turn) or []
    except Exception:
        return []
    return [r.get("program_id") for r in rows if isinstance(r, dict) and r.get("program_id")]


def rival_for_turn(full_state: dict, chara_info: dict, turn, program_id=None) -> list:
    st = state(full_state)
    turn = int(turn or 0)
    if turn < RIVAL_FIRST_TURN:
        return []
    rival = st.get("rival") or {}
    if int(rival.get("turn") or 0) == turn:
        return list(rival.get("array") or [])
    programs = _turn_race_programs(chara_info, turn)
    if not programs and program_id:
        programs = [program_id]
    candidates = []
    for pid in programs:
        course = _program_course(pid)
        if grade_of(pid) not in _GRADED or course is None:
            continue
        ground_field = ("proper_ground_turf" if int(course["ground"]) == 1
                        else "proper_ground_dirt")
        if (int(chara_info.get(ground_field) or 0) >= APT_C
                and int(chara_info.get(distance_field(course["distance"])) or 0) >= APT_C):
            candidates.append(int(pid))
    array = []
    if candidates:
        from ... import career_events as _ce
        rng = random.Random("tbrival:%d:%d" % (_ce.career_salt(full_state), turn))
        if rng.random() < RIVAL_CHANCE:
            pool = _drawable_chara_pool(int(chara_info.get("card_id") or 0) // 100)
            if pool:
                array = [{"program_id": rng.choice(candidates),
                          "chara_id": rng.choice(pool)}]
    st["rival"] = {"turn": turn, "array": array}
    return list(array)


def is_rival_race(full_state: dict, program_id, turn) -> bool:
    rival = state(full_state).get("rival") or {}
    if int(rival.get("turn") or 0) != int(turn or 0):
        return False
    return any(int(e.get("program_id") or 0) == int(program_id or 0)
               for e in rival.get("array") or [])


def rival_win_reward(full_state: dict, chara_info: dict, program_id) -> dict:
    rng = random.Random((int(program_id or 0) << 4) ^ 0x5A5A)
    stats = rng.sample(list(STATS), RIVAL_WIN_STAT_COUNT)
    course = _program_course(program_id)
    skill_id = None
    if course is not None:
        rows = master_data.query(
            "SELECT id FROM skill_data WHERE tag_id IN (401, 402) AND rarity=1 "
            "AND grade_value > 0 AND disable_singlemode=0 ORDER BY id")
        if rows:
            skill_id = int(rng.choice(rows)["id"])
    return {"stats": stats, "skill_id": skill_id}


def _turn_race_program(chara_info: dict, turn):
    try:
        from ...handlers import single_mode_team as smt
        info = smt._voluntary_race_for_turn(chara_info, turn)
    except Exception:
        return None
    return (info or {}).get("program_id")


def build_free_data_set(full_state: dict, chara_info: dict,
                        command_info_array=None, endpoint: str = "") -> dict:
    from . import shop
    st = state(full_state)
    turn = int(chara_info.get("turn") or 1)
    shop.refresh(full_state, turn)
    rival_for_turn(full_state, chara_info, turn, _turn_race_program(chara_info, turn))
    gained = int(st.get("gained_coin_num") or 0)
    st["gained_coin_num"] = 0
    climax = st.get("climax") or {}
    items = [{"item_id": int(k), "num": int(v)}
             for k, v in sorted((st.get("items") or {}).items(),
                                key=lambda kv: int(kv[0])) if int(v) > 0]
    return {
        "shop_id": int(st.get("shop_id") or 0),
        "sale_value": 0,
        "win_points": int(st.get("win_points") or 0),
        "prev_win_points": int(st.get("prev_win_points") or 0),
        "gained_coin_num": gained,
        "coin_num": int(st.get("coins") or 0),
        "twinkle_race_ranking": publish_ranking(st, endpoint),
        "user_item_info_array": items or None,
        "pick_up_item_info_array": list(st.get("offers") or []) or None,
        "twinkle_race_npc_info_array": list(climax.get("npcs") or []),
        "item_effect_array": shop.active_effects(st, turn) or None,
        "twinkle_race_npc_result_array": list(climax.get("results") or []),
        "command_info_array": _command_info(command_info_array),
        "rival_race_info_array": list((st.get("rival") or {}).get("array") or []),
        "unchecked_event_achievement_id": None,
    }


def _stamp_achievement_id(data: dict) -> None:
    fds = data.get("free_data_set")
    if not isinstance(fds, dict):
        return
    head = (data.get("unchecked_event_array") or [None])[0]
    if not isinstance(head, dict):
        return
    ach = epithets.trackblazer_achievement_id(head.get("event_id"))
    if ach:
        fds["unchecked_event_achievement_id"] = ach


def _command_info(command_info_array) -> list:
    if command_info_array:
        return [{"command_type": 1, "command_id": c.get("command_id"),
                 "params_inc_dec_info_array":
                     list(c.get("params_inc_dec_info_array") or [])}
                for c in command_info_array if c.get("command_type") == 1]
    return [{"command_type": 1, "command_id": cid, "params_inc_dec_info_array": []}
            for cid in TRAINING_COMMAND_IDS]


def attach(response: dict, full_state: dict, chara_info: dict,
           command_info_array=None, endpoint: str = "") -> dict:
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    data.pop("ura_data_set", None)
    data.pop("team_data_set", None)
    data.pop("live_data_set", None)
    data["free_data_set"] = build_free_data_set(full_state, chara_info,
                                                command_info_array, endpoint)
    _stamp_achievement_id(data)
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" in climax:
        chara_info["playing_state"] = 5
        data["chara_info"] = chara_info
    return response
