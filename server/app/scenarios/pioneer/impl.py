
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import formula as RF
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 11
STATE_KEY = "pioneer"

FIRST_PLAN_TURN = 3
LAST_POINT_TURN = 60
SUMMER_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
FINALS_TURNS = frozenset(range(73, 79))
FINALS_RACE_TURNS = frozenset({74, 76, 78})
LIVE_TURN = 72
TICKET_CAP, LATE_TICKET_CAP, LATE_FREE_TICKETS = 1, 3, 2
TEAM_SIZE = 11
TEAM_GROWTH = 3
LEVEL_UP_EVALUATIONS = (2, 4)
RESULT_GREAT, RESULT_GOOD = 2, 1
LINK_CHARAS = (1031, 1040, 1027, 1069, 1021)
LINK_HINTS = ((1031, 203112, 203111), (1040, 202662, 202661), (1027, 202452, 202451),
              (1069, 203222, 203221), (1021, 201611, 201612), (M.TUCKER_CHARA, 200462, 200461))
NPC_ROWS = {101: 9001, 102: 9002, 103: 9003, 104: 9004, 106: 9006, 108: 9008, 109: 9040,
            110: 9043, 111: 9044, 112: 9047, 113: 9049, 114: 9050, 115: 1139}

RESULT_REWARD = {
    (1, 2): (10, 60), (2, 2): (15, 120), (3, 2): (20, 150), (4, 2): (25, 200), (5, 2): (35, 300),
    (1, 1): (5, 30), (2, 1): (10, 60), (3, 1): (15, 75), (4, 1): (20, 100), (5, 1): (25, 150),
}
RESULT_EVENTS = {(1, 2): (211011, 400011004), (1, 1): (211012, 400011005),
                 (2, 2): (211013, 400011008), (2, 1): (211014, 400011009),
                 (3, 2): (211015, 400011012), (3, 1): (211016, 400011013),
                 (4, 2): (211017, 400011017), (4, 1): (211018, 400011018),
                 (5, 2): (211019, 400011021), (5, 1): (211020, 400011022)}
RESULT_TIMING, RESULT_SHOW_CLEAR = 20, 1101
DIRECTOR_BOND = 5
SKILL_ZENSHIN = (200511, 200512)


def _blank() -> dict:
    return {
        "deck_id": -1, "pt": 0, "plan": [], "built": 0, "rights": 0,
        "facilities": {str(f): f * 100 for f in M.FACILITIES},
        "tickets": 0, "ticket_cap": TICKET_CAP, "gain": 0,
        "results": [], "totals": [], "snapshots": {},
        "team": [], "exec_counts": {}, "levels": {},
        "preview": {"turn": 0},
        "hold_turn": 0, "flags": {}, "due": [],
        "camp_ticket": 0, "link_steps": {}, "planned_turn": 0, "crossed": 0,
        "auto": {"is_auto_deploy": 0, "is_checked_dialog": 0, "priority_facility_array": []},
        "live": 0, "complete": 0,
    }


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    return st


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


def turn_of(chara_info: dict) -> int:
    return int((chara_info or {}).get("turn") or 1)


def served_turn(full_state: dict, chara_info: dict) -> int:
    return int(state(full_state).get("hold_turn") or 0) or turn_of(chara_info)


def rng_for(full_state: dict, *parts) -> random.Random:
    return random.Random(":".join(["pioneer", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def facilities(st: dict) -> dict:
    return {int(f): int(s) for f, s in st["facilities"].items()}


def island_turn(turn: int) -> bool:
    return turn in SUMMER_TURNS or turn in FINALS_TURNS


def period(turn: int) -> int:
    return M.PERIOD_FINALS if turn in FINALS_TURNS else M.PERIOD_CAMP


def deck(chara_info: dict) -> dict:
    return RF.deck(chara_info)


def tucker_card(chara_info: dict):
    for pos, sid in deck(chara_info).items():
        _k, _c, rarity, chara = RF.card_meta(sid)
        if chara == M.TUCKER_CHARA:
            return pos, sid, rarity
    return None


def compute_deck_id(chara_info: dict, card_levels=None) -> int:
    t = tucker_card(chara_info)
    if not t:
        return 0
    _pos, sid, rarity = t
    if rarity < 3:
        return 1
    return 3 if formula.card_level(chara_info, sid, card_levels) >= 41 else 2


def deck_id(full_state: dict, chara_info: dict) -> int:
    st = state(full_state)
    if int(st["deck_id"]) < 0:
        from ...handlers import single_mode_team as smt
        st["deck_id"] = compute_deck_id(chara_info, smt._support_card_levels(full_state))
    return int(st["deck_id"])


def linked_charas(chara_info: dict) -> set:
    return RI.linked_charas(chara_info)


def normal_deck_charas(chara_info: dict) -> list:
    out = []
    for _pos, sid in sorted(deck(chara_info).items()):
        kind, _cmd, _r, chara = RF.card_meta(sid)
        if kind == 1 and chara and chara not in out:
            out.append(chara)
    return out


def current_plan(st: dict) -> int:
    n = len(st["results"]) + 1
    return n if n <= 5 else 0


def planning_cost(full_state: dict, chara_info: dict) -> int:
    st = state(full_state)
    cp = current_plan(st)
    if not cp:
        return 0
    return M.planning().get((deck_id(full_state, chara_info), cp), (0, 0, 0))[2]


def thresholds(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    cp = current_plan(st)
    if not cp:
        return [0, 0]
    d = deck_id(full_state, chara_info)
    rows = sorted((tmin, tmax, v) for (dd, tmin, tmax), v in M.ticket_thresholds().items() if dd == d)
    _p, cp_turn, _t = M.planning().get((d, cp), (0, 0, 0))
    for tmin, tmax, v in rows:
        if tmax == cp_turn:
            return list(v)
    return [0, 0]


def latest(st: dict):
    if not st["results"]:
        return None
    last = max(st["results"], key=lambda r: r["check_point_type"])
    return last["check_point_type"], last["result_state"]


def passive(st: dict, effect_type: int) -> int:
    got = latest(st)
    return M.check_point_effect(got[0], got[1], effect_type) if got else 0


def cp_pct(st: dict) -> int:
    return passive(st, M.CP_POINTS)


def te_pct(st: dict) -> int:
    return passive(st, M.CP_TRAINING)


def hint_pct(st: dict) -> int:
    return passive(st, M.CP_HINT_RATE)


def points_active(st: dict, turn: int) -> bool:
    return FIRST_PLAN_TURN <= turn <= LAST_POINT_TURN and len(st["results"]) < 5


def great_count(st: dict, among=None) -> int:
    return sum(1 for r in st["results"] if r["result_state"] == RESULT_GREAT
               and (among is None or r["check_point_type"] in among))


def all_great(st: dict) -> bool:
    return len(st["results"]) == 5 and great_count(st) == 5


def next_step(facilities_now: dict, facility: int, target: int):
    for tgt, cost in M.lv_up().get(int(facilities_now.get(facility, facility * 100)), ()):
        if tgt == int(target):
            return cost
    return None


def available_steps(facilities_now: dict) -> dict:
    return {f: list(M.lv_up().get(int(facilities_now.get(f, f * 100)), ()))
            for f in M.FACILITIES if M.lv_up().get(int(facilities_now.get(f, f * 100)))}


def validate_plan(full_state: dict, chara_info: dict, rows) -> list | None:
    st = state(full_state)
    facs = facilities(st)
    cost = planning_cost(full_state, chara_info)
    out, seen, total = [], set(), 0
    for row in sorted(rows or (), key=lambda r: int(r.get("priority") or 0)):
        fac, tgt = int(row.get("facility_id") or 0), int(row.get("target_state_id") or 0)
        if fac in seen or fac not in M.FACILITIES:
            return None
        step = next_step(facs, fac, tgt)
        if step is None:
            return None
        seen.add(fac)
        total += step
        out.append({"priority": len(out) + 1, "facility_id": fac,
                    "prev_state_id": facs.get(fac, fac * 100), "target_state_id": tgt})
    if total == cost:
        return out
    most = sum(min(c for _t, c in steps) for steps in available_steps(facs).values())
    if total < cost and most <= cost and set(available_steps(facs)) <= seen:
        return out
    return None


def commit_plan(full_state: dict, chara_info: dict, plan: list, turn: int) -> None:
    st = state(full_state)
    st["plan"] = plan
    st["built"] = 0
    st["planned_turn"] = int(turn)


def plan_batches(full_state: dict, chara_info: dict) -> tuple:
    st = state(full_state)
    first_pt = thresholds(full_state, chara_info)[0]
    running, first, second = 0, [], []
    for row in st["plan"]:
        running += next_step({int(row["facility_id"]): int(row["prev_state_id"])},
                             int(row["facility_id"]), int(row["target_state_id"])) or 0
        (first if running <= first_pt and not second else second).append(row)
    return first, second


def build(full_state: dict, chara_info: dict, batch: int) -> list:
    st = state(full_state)
    if int(st["built"]) >= batch:
        return []
    first, second = plan_batches(full_state, chara_info)
    rows = (first if batch == 1 else []) + (second if batch == 2 else [])
    if batch == 2 and int(st["built"]) < 1:
        rows = first + second
    for row in rows:
        st["facilities"][str(int(row["facility_id"]))] = int(row["target_state_id"])
    st["built"] = batch
    if rows:
        log.info("pioneer: built %s", [r["target_state_id"] for r in rows])
    return rows


def add_points(full_state: dict, chara_info: dict, amount: int, how: str, turn: int) -> list:
    st = state(full_state)
    if amount <= 0 or not points_active(st, turn):
        return []
    before = int(st["pt"])
    st["pt"] = before + int(amount)
    crossed = []
    for i, thr in enumerate(thresholds(full_state, chara_info), start=1):
        if thr and before < thr <= st["pt"] and int(st["crossed"]) < i:
            crossed.append(i)
    for i in crossed:
        st["crossed"] = i
        if turn in SUMMER_TURNS:
            build(full_state, chara_info, i)
            st["rights"] = i
            st["camp_ticket"] = int(st["camp_ticket"]) + 1
        else:
            st["due"].append({"kind": "ticket", "how": how, "batch": i, "turn": int(turn),
                              "cp": current_plan(st)})
    return crossed


def grant_ticket(st: dict, n: int = 1) -> None:
    st["tickets"] = min(int(st["ticket_cap"]), int(st["tickets"]) + n)
    st["gain"] = n


def ticket_reached(full_state: dict, chara_info: dict, batch: int) -> None:
    st = state(full_state)
    build(full_state, chara_info, batch)
    st["rights"] = batch
    grant_ticket(st)


def form_team(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    if st["team"]:
        return st["team"]
    trainee = int(chara_info.get("card_id") or 0) // 100
    team = [c for c in normal_deck_charas(chara_info) if c != trainee]
    taken = linked_charas(chara_info)
    team += [c for c in LINK_CHARAS if c not in team and c not in taken]
    team += _draw_extras(full_state, chara_info, team, TEAM_SIZE - len(team), "team")
    st["team"] = team
    _ensure_rows(chara_info, islanders(full_state, chara_info))
    log.info("pioneer: island team %s", team)
    return team


def grow_team(full_state: dict, chara_info: dict, check_point_type: int) -> list:
    st = state(full_state)
    new = _draw_extras(full_state, chara_info, st["team"], TEAM_GROWTH, f"grow{check_point_type}")
    st["team"] = st["team"] + new
    _ensure_rows(chara_info, new)
    return new


def _draw_extras(full_state: dict, chara_info: dict, team: list, n: int, salt: str) -> list:
    if n <= 0:
        return []
    from ... import master_data
    taken = linked_charas(chara_info) | set(team) | set(NPC_ROWS.values())
    pool = sorted(c for c in master_data.drawable_charas()
                  if 1000 <= int(c) < 2000 and c not in taken)
    rng = rng_for(full_state, "islanders", salt)
    rng.shuffle(pool)
    return pool[:n]


def islanders(full_state: dict, chara_info: dict) -> list:
    brought = {RF.card_meta(sid)[3] for sid in deck(chara_info).values()}
    return [c for c in state(full_state)["team"] if c not in brought]


def _ensure_rows(chara_info: dict, charas) -> None:
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for chara in charas:
        if chara not in have:
            arr.append({"target_id": chara, "training_partner_id": chara,
                        "evaluation": 0, "is_outing": 0, "story_step": 0,
                        "is_appear": 1, "group_outing_info_array": []})


def on_team(full_state: dict, chara_info: dict, target_id: int) -> bool:
    st = state(full_state)
    if not st["team"]:
        return False
    if target_id >= 1000:
        return target_id in st["team"]
    sid = deck(chara_info).get(int(target_id))
    return bool(sid) and RF.card_meta(sid)[3] in st["team"]


def count_training(st: dict, command_id: int) -> None:
    base = M.ISLAND_TO_TRAINING.get(int(command_id), int(command_id))
    if base in RI.EXEC_INFO_ORDER:
        st["exec_counts"][str(base)] = int(st["exec_counts"].get(str(base), 0)) + 1


def level_for(st: dict, base_cmd: int) -> int:
    exec_count = int(st["exec_counts"].get(str(base_cmd), 0))
    return min(5, 1 + exec_count // 4 + great_count(st, LEVEL_UP_EVALUATIONS))


def sync_levels(full_state: dict, chara_info: dict) -> list:
    from ...handlers import single_mode_team as smt
    st = state(full_state)
    career = (full_state.get(smt.STATE_KEY) or {}).get("data") or {}
    levels = smt._facility_levels(career)
    tla = chara_info.setdefault("training_level_info_array", [])
    rose = []
    for cmd in formula.TRAINING_COMMANDS:
        new = level_for(st, cmd)
        if int(levels.get(str(cmd), 1)) < new:
            rose.append(cmd)
        levels[str(cmd)] = new
        entry = next((e for e in tla if e.get("command_id") == cmd), None)
        if entry:
            entry["level"] = new
        else:
            tla.append({"command_id": cmd, "level": new})
    return rose


def training_exec_info(st: dict) -> list:
    return [{"base_command_id": c, "exec_count": int(st["exec_counts"].get(str(c), 0))}
            for c in RI.EXEC_INFO_ORDER]


def check_point_due(st: dict, turn: int) -> bool:
    cp = M.check_point_for_turn(turn)
    return bool(cp) and not any(r["check_point_type"] == cp for r in st["results"])


def commit_check_point(full_state: dict, chara_info: dict, turn: int):
    st = state(full_state)
    cp = M.check_point_for_turn(turn)
    cost = planning_cost(full_state, chara_info)
    pt = int(st["pt"])
    result = RESULT_GREAT if pt >= cost else RESULT_GOOD
    if result == RESULT_GREAT and int(st["built"]) < 2:
        build(full_state, chara_info, 2)
    rate, cap = M.pt_bonus().get(cp, (100, 0))
    overflow = max(0, pt - cost)
    energy = min(cap, overflow * rate // 1000) if result == RESULT_GREAT else 0
    st["results"] = [r for r in st["results"] if r["check_point_type"] != cp] + [
        {"check_point_type": cp, "result_state": result}]
    st["totals"] = st["totals"] + [pt]
    if cp in (1, 3, 5):
        st["snapshots"][str(cp)] = dict(st["facilities"])
    event_id, story_id = RESULT_EVENTS[(cp, result)]
    ev = CE.Event(
        event_id=event_id, story_id=story_id, play_timing=RESULT_TIMING,
        choices=[CE.Choice(effects=result_rewards(full_state, chara_info, cp, result, energy))],
        chara_id=0, show_clear=RESULT_SHOW_CLEAR, show_clear_sort_id=RI.goal_sort_id(chara_info, turn) or 1,
        once_key=f"pioneer:result:{cp}", priority=CE.PRIO_FRONT, resolver="pioneer_result",
        source="pioneer", payload={"check_point_type": cp, "result_state": result, "turn": int(turn)})
    log.info("pioneer: evaluation %s -> %s (%s/%s pt, overflow energy %s)", cp, result, pt, cost, energy)
    return result, ev


def result_rewards(full_state: dict, chara_info: dict, cp: int, result: int, energy: int) -> list:
    stats, sp = RESULT_REWARD[(cp, result)]
    effects = [{"type": "all_stats", "value": f"+{stats}"},
               {"type": "skill_point", "value": f"+{sp}"}]
    if energy:
        effects.append({"type": "energy", "value": f"+{energy}"})
    if cp == 5:
        gold, white = SKILL_ZENSHIN
        effects.append({"type": "skill_hint", "skill_id": gold if result == RESULT_GREAT else white,
                        "value": "+1" if result == RESULT_GREAT else "+3"})
    else:
        effects += random_hint(full_state, chara_info, f"cp{cp}")
    return effects


def random_hint(full_state: dict, chara_info: dict, salt: str) -> list:
    pool = [sid for sid in deck(chara_info).values() if RF.is_normal_card(sid)]
    if not pool:
        return []
    rng = rng_for(full_state, "hint", salt)
    return RI.card_hint_effects(chara_info, rng.choice(pool), rng)


def refresh_preview(full_state: dict, chara_info: dict, home_commands, card_levels=None) -> bool:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    facs = facilities(st)
    te = te_pct(st)
    cp = cp_pct(st)
    bonus, points = {}, {}
    island_on = island_turn(turn)
    for c in home_commands or ():
        if c.get("command_type") != 1:
            continue
        cmd = int(c["command_id"])
        partners = list(c.get("training_partner_array") or ())
        if cmd == M.ISLAND_TRAINING:
            continue
        if cmd in M.ISLAND_TO_TRAINING:
            bonus[str(cmd)] = formula.island_bonus(chara_info, cmd, partners, facs,
                                                   period(turn), card_levels)
        else:
            bonus[str(cmd)] = formula.training_bonus(chara_info, cmd, int(c.get("level") or 1),
                                                     partners, te, card_levels)
        points[str(cmd)] = formula.training_points(chara_info, partners, cmd, cp)
    island = ({"map": {}, "params": [], "bonus": [], "points": 0} if island_on else
              island_preview(full_state, chara_info, home_commands, turn, card_levels))
    new = {"turn": turn, "bonus": bonus, "points": points, "island": island}
    changed = new != st["preview"]
    st["preview"] = new
    return changed


def island_preview(full_state: dict, chara_info: dict, home_commands, turn: int,
                   card_levels=None) -> dict:
    st = state(full_state)
    facs = facilities(st)
    pmap = island_map(full_state, chara_info, home_commands, turn) if int(st["tickets"]) > 0 else {}
    gains = formula.island_training_gains(chara_info, pmap, facs, card_levels)
    return {"map": {str(f): ids for f, ids in pmap.items()},
            "params": formula.island_training_params(gains),
            "bonus": formula.island_training_bonus(chara_info, pmap, facs, card_levels) if pmap else [],
            "points": formula.island_training_points(chara_info, pmap, cp_pct(st))}


def island_map(full_state: dict, chara_info: dict, home_commands, turn: int) -> dict:
    st = state(full_state)
    return formula.island_partners(chara_info, home_commands, facilities(st),
                                   islanders(full_state, chara_info),
                                   rng_for(full_state, "beach", turn))


def stored(st: dict, turn: int) -> dict:
    pv = st["preview"]
    return pv if pv.get("turn") == turn else {"bonus": {}, "points": {}, "island": {}}


def island_partner_map(st: dict, turn: int) -> dict:
    return {int(f): list(ids) for f, ids in (stored(st, turn).get("island") or {}).get("map", {}).items()}


def evaluation_array(full_state: dict, chara_info: dict) -> list:
    by_pos = deck(chara_info)
    out = []
    for e in chara_info.get("evaluation_info_array") or ():
        if not isinstance(e, dict):
            continue
        tid = int(e.get("target_id") or 0)
        if tid in by_pos:
            chara = RF.card_meta(by_pos[tid])[3]
        elif tid in NPC_ROWS:
            chara = NPC_ROWS[tid]
        elif tid >= 1000:
            chara = tid
        else:
            continue
        out.append({"target_id": tid, "chara_id": chara,
                    "is_pioneer_team": 1 if on_team(full_state, chara_info, tid) else 0})
    order = {t: i for i, t in enumerate(sorted(by_pos))}
    out.sort(key=lambda r: (0 if r["target_id"] in by_pos else 1 if r["target_id"] < 1000 else 2,
                            order.get(r["target_id"], r["target_id"])))
    return out


def npc_rows(chara_info: dict) -> list:
    brought = {RF.card_meta(sid)[3] for sid in deck(chara_info).values()}
    return [(t, c) for t, c in sorted(NPC_ROWS.items()) if c not in brought]


def point_rows(full_state: dict, chara_info: dict, home_commands, turn: int) -> list:
    st = state(full_state)
    if not points_active(st, turn) or turn < FIRST_PLAN_TURN:
        return []
    pv = stored(st, turn)
    cp = cp_pct(st)
    rows, extra = [], {}
    for c in home_commands or ():
        if not c.get("is_enable"):
            continue
        ctype, cmd = int(c.get("command_type") or 0), int(c.get("command_id") or 0)
        if ctype == 1:
            if cmd == M.ISLAND_TRAINING:
                gain = int((pv.get("island") or {}).get("points") or 0)
            else:
                gain = int(pv["points"].get(str(cmd), 0))
            rows.append({"command_type": 1, "command_id": cmd, "gain_num": gain})
        elif ctype == 3:
            extra.setdefault(3, {"command_type": 3, "command_id": 0, "gain_num": 0})
        elif ctype == 4:
            extra.setdefault(4, {"command_type": 4, "command_id": cmd,
                                 "gain_num": race_points_for(full_state, chara_info, turn, cp)})
        elif ctype == 7:
            extra.setdefault(7, {"command_type": 7, "command_id": cmd, "gain_num": 0})
    return rows + [extra[k] for k in sorted(extra)]


def goal_race_turn(chara_info: dict, turn: int) -> bool:
    from ...handlers import single_mode_team as smt
    try:
        forced = smt._forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    except Exception:
        forced = frozenset()
    return turn in forced


def beach_race_pct(st: dict) -> int:
    return sum(v2 for _t, _v1, v2, _v3, _v4 in formula.facility_rows(
        facilities(st), M.EFFECT_RACE_PT))


def race_points_for(full_state: dict, chara_info: dict, turn: int, cp=None, goal=None) -> int:
    st = state(full_state)
    cp = cp_pct(st) if cp is None else cp
    goal = goal_race_turn(chara_info, turn) if goal is None else goal
    return formula.race_points(cp, goal, beach_race_pct(st))


def data_set(full_state: dict, chara_info: dict, home_info: dict, start: bool = False) -> dict:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    commands = list((home_info or {}).get("command_info_array") or ())
    pv = stored(st, turn)
    done = len(st["results"]) >= 5
    trainings = [c for c in commands if c.get("command_type") == 1]
    bonus_rows = []
    for c in trainings:
        cmd = int(c["command_id"])
        params = (pv.get("island") or {}).get("bonus") if cmd == M.ISLAND_TRAINING else pv["bonus"].get(str(cmd))
        bonus_rows.append({"command_type": 1, "command_id": cmd,
                           "params_inc_dec_info_array": [] if start else list(params or [])})
    if start or not bonus_rows:
        bonus_rows = [{"command_type": 1, "command_id": c, "params_inc_dec_info_array": []}
                      for c in (*formula.TRAINING_COMMANDS, M.ISLAND_TRAINING)]
    island_on = island_turn(turn) or (st.get("complete") and turn >= LIVE_TURN)
    pmap = island_partner_map(st, turn)
    if island_on:
        shima_info = []
    else:
        shima_info = {"remain_num": int(st["tickets"]),
                      "shima_training_partner_info_array": [
                          {"facility_id": f, "training_partner_id_array": ids}
                          for f, ids in pmap.items()] if int(st["tickets"]) > 0 else []}
    cost = 0 if done else planning_cost(full_state, chara_info)
    return {
        "pioneer_info": {
            "pioneer_pt": 0 if done else int(st["pt"]),
            "planning_cost": cost,
            "gain_shima_training_pt_array": [0, 0] if done else thresholds(full_state, chara_info),
            "deck_id": deck_id(full_state, chara_info),
            "last_gain_rights_num": int(st["rights"]),
            "target_race_gain_pioneer_point": (0 if goal_race_turn(chara_info, turn)
                                               else race_points_for(full_state, chara_info, turn,
                                                                    goal=True)),
        },
        "command_info_array": bonus_rows,
        "evaluation_info_array": evaluation_array(full_state, chara_info),
        "shima_training_info": shima_info,
        "shima_training_gain_info": {"gain_num": int(st["gain"])},
        "facility_info_array": [{"facility_id": f, "state_id": int(st["facilities"][str(f)])}
                                for f in M.FACILITIES],
        "planning_info_array": [dict(r) for r in st["plan"]],
        "pioneer_point_gain_info_array": point_rows(full_state, chara_info, commands, turn),
        "check_point_info_array": check_point_info(st),
        "training_exec_info_array": training_exec_info(st),
    }


def check_point_info(st: dict, current: int = 0) -> list:
    rows = sorted(st["results"], key=lambda r: r["check_point_type"])
    if current:
        rows = ([r for r in rows if r["check_point_type"] == current]
                + [r for r in rows if r["check_point_type"] != current])
    return [{"check_point_type": r["check_point_type"], "result_state": r["result_state"]}
            for r in rows]


def auto_planning_info(st: dict) -> dict:
    a = st["auto"]
    return {"is_auto_deploy": int(a.get("is_auto_deploy") or 0),
            "is_checked_dialog": int(a.get("is_checked_dialog") or 0),
            "priority_facility_array": [dict(p) for p in a.get("priority_facility_array") or ()]}


def facility_snapshots(st: dict) -> list:
    out = []
    for cp in (1, 3, 5):
        snap = st["snapshots"].get(str(cp))
        if snap:
            out.append({"check_point_type": cp,
                        "facility_info_array": [{"facility_id": int(f), "state_id": int(s)}
                                                for f, s in sorted(snap.items(), key=lambda x: int(x[0]))]})
    return out


def total_points(st: dict) -> int:
    return sum(int(t) for t in st["totals"])
