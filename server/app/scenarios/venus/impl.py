
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 5
STATE_KEY = "venus"
FIRST_OFFER_TURN = 3
SUMMER_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
LATE_LEVELUP_TURN = 73

FAVOURED_TYPES = {101: (1, 3, 6), 105: (2, 4, 6), 102: (2, 3, 6), 103: (1, 3, 4, 6), 106: (1, 5, 6)}
FAVOURED_WEIGHT = 4
WIT_BOOST_CHANCE = 12 / 21
BOOST_CHANCE = {3: 0.23, 7: 0.18, 4: 0.36}
LEFT_TYPE_CHANCE = 0.75

POINTS_TRAINING = 2
POINTS_FRAGMENT = 1
POINTS_RACE_WIN = 9
POINTS_RACE_LOSS = 5
POINTS_PER_LEVEL = 12
MAX_LEVEL = 5

RACE_FACTOR = {50001: 0.68, 50002: 1.19, 50003: 2.02, 50004: 2.17}
RACE_JITTER = 0.03
NAMED_COUNT = {50001: 6, 50002: 15, 50003: 15, 50004: 12}
RIVAL_ROWS = {50001: {2002: 2002902},
              50002: {2002: 2002904, 2003: 2003902},
              50003: {2002: 2002903, 2001: 2001903, 2003: 2003903},
              50004: {2002: 2002903, 2001: 2001904, 2003: 2003903}}
OPTIONAL_RIVALS = {50002: (2003,), 50003: (2003,), 50004: (2003,)}
BOSS_ROWS = (9040901, 9041901, 9042901)
CATEGORY_ORDER = ((1, "short"), (1, "mile"), (1, "middle"), (1, "long"),
                  (2, "short"), (2, "mile"), (2, "middle"), (2, "long"))

SPRACE_INDEX = {50001: 1, 50002: 2, 50003: 3}
WIN_BASE = {1: (10, 50), 2: (15, 60), 3: (20, 70)}
LOSS_BASE = {1: (5, 40), 2: (10, 50), 3: (15, 60)}
DARLEY_SP = {1: 20, 2: 30, 3: 45}
BYERLEY_STATS = 10
DARLEY_RACE_MULT = 1.35


def _blank() -> dict:
    return {
        "slots": {},
        "levels": {str(g): 0 for g in M.GODDESSES},
        "active": {},
        "uses": [],
        "offers": {"turn": 0, "rows": []},
        "points": {str(c): 0 for c in formula.TRAINING_COMMANDS},
        "shown_levels": {},
        "sprace": {},
        "races": [],
        "done_races": [],
        "due": [],
        "wisdom_seen": False,
        "godolphin": {},
        "hold_turn": 0,
        "preview": {"turn": 0, "bonus": {}},
        "cleared_turn": 0,
        "fragments_total": 0,
        "wisdom_uses": 0,
        "final": {},
        "card003_due": 0,
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
    return random.Random(":".join(["venus", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def level(st: dict, goddess: int) -> int:
    return int(st["levels"].get(str(goddess), 0))


def training_pct(st: dict) -> int:
    return sum(M.passive(g, level(st, g), M.EFF_TRAINING) for g in M.GODDESSES)


def energy_down(st: dict) -> int:
    return M.passive(M.DARLEY, level(st, M.DARLEY), M.EFF_ENERGY_DOWN)


def hint_rate(st: dict) -> int:
    return M.passive(M.GODOLPHIN, level(st, M.GODOLPHIN), M.EFF_HINT_RATE)


def support_event_gain(st: dict) -> int:
    return M.passive(M.BYERLEY, level(st, M.BYERLEY), M.EFF_SUPPORT_EVENT_GAIN)


def chain_rate(st: dict) -> int:
    return M.passive(M.BYERLEY, level(st, M.BYERLEY), M.EFF_CHAIN_RATE)


def active_goddess(st: dict, turn: int) -> int:
    a = st.get("active") or {}
    return int(a.get("chara_id") or 0) if int(a.get("turn") or 0) == int(turn) else 0


def wisdom_on(st: dict, turn: int, goddess: int) -> bool:
    return active_goddess(st, turn) == goddess


def slot(st: dict, n: int):
    v = st["slots"].get(str(n))
    return (int(v[0]), int(v[1])) if v else None


def fragments_held(st: dict) -> int:
    return sum(1 for n in M.FRAGMENT_SLOTS if slot(st, n))


def wisdom_ready(st: dict) -> bool:
    return slot(st, M.WISDOM_SLOT) is not None


def offers_blocked(st: dict, turn: int) -> bool:
    return (int(turn) < FIRST_OFFER_TURN or wisdom_ready(st)
            or int((st.get("active") or {}).get("turn") or 0) == int(turn))


def slot_bonus(st: dict) -> dict:
    out: dict = {}
    for n in range(1, M.WISDOM_SLOT):
        s = slot(st, n)
        if not s:
            continue
        t = M.type_of(s[0])
        out[t] = out.get(t, 0) + M.slot_value(s[1])
    return out


def _fuse(full_state: dict, st: dict, target: int) -> None:
    left, right = slot(st, M.FUSION[target][0]), slot(st, M.FUSION[target][1])
    if not left or not right or slot(st, target):
        return
    rng = rng_for(full_state, "fuse", st["fragments_total"], target)
    if target == M.WISDOM_SLOT:
        a, b = M.goddess_of(left[0]), M.goddess_of(right[0])
        if a == b:
            goddess = a
        else:
            count: dict = {}
            for n in M.FRAGMENT_SLOTS:
                s = slot(st, n)
                if s:
                    g = M.goddess_of(s[0])
                    count[g] = count.get(g, 0) + 1
            best = max(count.values(), default=0)
            tied = sorted(g for g, c in count.items() if c == best)
            goddess = tied[0] if len(tied) == 1 else rng.choice(tied)
        st["slots"][str(target)] = [goddess, 0]
        return
    goddess = M.goddess_of(left[0])
    same = goddess == M.goddess_of(right[0])
    type_ = M.type_of(left[0]) if rng.random() < LEFT_TYPE_CHANCE else M.type_of(right[0])
    merged = target in M.MERGED_SLOTS
    column = ((M.COL_MERGED_SAME if same else M.COL_MERGED_MIXED) if merged
              else (M.COL_CRYSTAL_SAME if same else M.COL_CRYSTAL_MIXED))
    sid = M.spirit_id(goddess, type_)
    st["slots"][str(target)] = [sid, M.effect_group(sid, column)]


def add_fragment(full_state: dict, sid: int, count: int = 1) -> int:
    st = state(full_state)
    landed = 0
    for _ in range(int(count)):
        free = next((n for n in M.FRAGMENT_SLOTS if not slot(st, n)), None)
        if free is None:
            break
        st["slots"][str(free)] = [int(sid), M.effect_group(sid, M.COL_FRAGMENT)]
        st["fragments_total"] = int(st["fragments_total"]) + 1
        landed += 1
        fac = M.TYPE_FACILITY.get(M.type_of(sid))
        if fac:
            st["points"][str(fac)] = int(st["points"].get(str(fac), 0)) + POINTS_FRAGMENT
        for target in (9, 10, 11, 12, 13, 14, 15):
            _fuse(full_state, st, target)
    if landed:
        log.info("venus: +%s fragment %s -> %s held%s", landed, sid, fragments_held(st),
                 " (wisdom ready)" if wisdom_ready(st) else "")
    return landed


def spirit_info_array(st: dict) -> list:
    out = []
    for n in range(1, M.WISDOM_SLOT + 1):
        s = slot(st, n)
        if s:
            out.append({"spirit_num": n, "spirit_id": s[0], "effect_group_id": s[1]})
    return out


def _roll_type(rng: random.Random, command_id: int, ctype: int) -> int:
    types = list(range(1, 7))
    if ctype != 1:
        return rng.choice(types)
    fav = FAVOURED_TYPES.get(command_id, ())
    return rng.choices(types, weights=[FAVOURED_WEIGHT if t in fav else 1 for t in types])[0]


def roll_offers(full_state: dict, chara_info: dict, turn: int, home_commands) -> list:
    st = state(full_state)
    if int(st["offers"].get("turn") or 0) == int(turn) and st["offers"].get("rows") is not None:
        return st["offers"]["rows"]
    rows: list = []
    if not offers_blocked(st, turn):
        rng = rng_for(full_state, "offer", turn)
        seven = fragments_held(st) >= 7
        camp = int(turn) in SUMMER_TURNS
        goal = _is_goal_turn(chara_info, turn)
        all_friend = wisdom_on(st, turn, M.BYERLEY)
        trainings = {formula.SUMMER_TO_BASE.get(int(c.get("command_id") or 0), int(c.get("command_id") or 0)): c
                     for c in home_commands or () if int(c.get("command_type") or 0) == 1}
        for cid in (101, 102, 103, 105, 106):
            if cid not in trainings:
                continue
            goddess = rng.choice(M.GODDESSES)
            type_ = _roll_type(rng, cid, 1)
            partners = trainings[cid].get("training_partner_array") or ()
            served = int(trainings[cid].get("command_id") or cid)
            boost = formula.is_friendship_training(chara_info, served, partners, all_friend)
            if boost and cid == 106:
                boost = rng.random() < WIT_BOOST_CHANCE
            rows.append([1, served, M.spirit_id(goddess, type_), int(boost and not seven)])
        for ctype, cid in ((3, 0), (4, 401), (7, 701)):
            if camp and ctype == 7:
                continue
            goddess = rng.choice(M.GODDESSES)
            type_ = _roll_type(rng, cid, ctype)
            boost = rng.random() < BOOST_CHANCE.get(ctype, 0)
            if ctype == 4 and goal:
                boost = True
            rows.append([ctype, cid, M.spirit_id(goddess, type_), int(boost and not seven)])
    st["offers"] = {"turn": int(turn), "rows": rows}
    return rows


def _is_goal_turn(chara_info: dict, turn: int) -> bool:
    try:
        return int(turn) in _smt()._forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    except Exception:
        return False


def offer_for(st: dict, turn: int, ctype: int, command_id: int):
    if int(st["offers"].get("turn") or 0) != int(turn):
        return None
    cid = formula.SUMMER_TO_BASE.get(int(command_id or 0), int(command_id or 0))
    if ctype == 3:
        cid = 0
    for row in st["offers"].get("rows") or ():
        row_cid = formula.SUMMER_TO_BASE.get(int(row[1]), int(row[1]))
        if int(row[0]) == int(ctype) and row_cid == cid:
            return int(row[2]), 2 if int(row[3]) else 1
    return None


def command_info_rows(st: dict, turn: int) -> list:
    if int(st["offers"].get("turn") or 0) != int(turn):
        return []
    return [{"command_type": r[0], "command_id": r[1], "spirit_id": r[2], "is_boost": r[3]}
            for r in st["offers"].get("rows") or ()]


class Refused(Exception):
    pass


def use_wisdom(full_state: dict, chara_info: dict, turn: int) -> int:
    st = state(full_state)
    if not wisdom_ready(st):
        raise Refused("no wisdom to use")
    goddess = slot(st, M.WISDOM_SLOT)[0]
    st["levels"][str(goddess)] = min(5, level(st, goddess) + 1)
    group = M.wisdom_group(goddess)
    st["active"] = {"chara_id": goddess, "group": group, "turn": int(turn)}
    st["uses"].append({"set_id": len(st["uses"]) + 1, "spirit_info_array": spirit_info_array(st),
                       "venus_spirit_active_effect_info_array": active_rows(st, turn)})
    st["wisdom_uses"] = int(st["wisdom_uses"]) + 1
    st["cleared_turn"] = int(turn)
    st["offers"] = {"turn": int(turn), "rows": []}
    for etype, v1, v2, _v3, _v4 in M.effects(group):
        if etype != M.EFF_INSTANT:
            continue
        if v1 == 10:
            mv = max(int(chara_info.get("max_vital") or 100), int(chara_info.get("vital") or 0))
            chara_info["vital"] = min(mv, int(chara_info.get("vital") or 0) + v2)
        elif v1 == 20:
            chara_info["motivation"] = max(1, min(5, int(chara_info.get("motivation") or 3) + v2))
    log.info("venus: wisdom %s used on turn %s -> levels %s", goddess, turn, st["levels"])
    return goddess


def clear_after_use(st: dict, turn: int) -> None:
    used = int(st.get("cleared_turn") or 0)
    if used and int(turn) >= used and wisdom_ready(st):
        st["slots"] = {}
        st["cleared_turn"] = 0


def active_rows(st: dict, turn: int) -> list:
    a = st.get("active") or {}
    if int(a.get("turn") or 0) != int(turn):
        return []
    return [{"chara_id": int(a["chara_id"]), "effect_group_id": int(a["group"]),
             "begin_turn": int(turn), "end_turn": int(turn)}]


def race_mult(st: dict, turn: int) -> float:
    return DARLEY_RACE_MULT if wisdom_on(st, turn, M.DARLEY) else 1.0


def facility_level(full_state: dict, command_id: int) -> int:
    smt = _smt()
    career = (full_state.get(smt.STATE_KEY) or {}).get("data") or {}
    return int(smt._facility_levels(career).get(str(command_id), 1))


def owed_levels(full_state: dict) -> list:
    st = state(full_state)
    out = []
    for cmd in formula.TRAINING_COMMANDS:
        target = min(MAX_LEVEL, 1 + int(st["points"].get(str(cmd), 0)) // POINTS_PER_LEVEL)
        have = max(facility_level(full_state, cmd), int(st["shown_levels"].get(str(cmd), 1)))
        for lv in range(have + 1, target + 1):
            out.append((cmd, lv))
    return out


def raise_facility(full_state: dict, chara_info: dict, command_id: int, lv: int) -> None:
    smt = _smt()
    career = (full_state.get(smt.STATE_KEY) or {}).get("data") or {}
    levels = smt._facility_levels(career)
    new = max(int(levels.get(str(command_id), 1)), min(MAX_LEVEL, int(lv)))
    levels[str(command_id)] = new
    state(full_state)["shown_levels"][str(command_id)] = new
    tla = chara_info.setdefault("training_level_info_array", [])
    entry = next((e for e in tla if e.get("command_id") == command_id), None)
    if entry:
        entry["level"] = new
    else:
        tla.append({"command_id": command_id, "level": new})


def add_race_points(full_state: dict, won: bool) -> None:
    st = state(full_state)
    for cmd in formula.TRAINING_COMMANDS:
        st["points"][str(cmd)] = int(st["points"].get(str(cmd), 0)) + (
            POINTS_RACE_WIN if won else POINTS_RACE_LOSS)


def preview_flags(st: dict, turn: int) -> dict:
    return {"all_friend": wisdom_on(st, turn, M.BYERLEY), "level6": wisdom_on(st, turn, M.DARLEY)}


def refresh_preview(full_state: dict, chara_info: dict, home_info: dict, card_levels=None) -> bool:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    commands = [c for c in (home_info or {}).get("command_info_array") or ()]
    before = (copy.deepcopy(st["preview"]), copy.deepcopy(st["offers"]))
    flags = preview_flags(st, turn)
    fb = slot_bonus(st)
    g = training_pct(st)
    ed = energy_down(st)
    bonus, partners = {}, {}
    for c in commands:
        if int(c.get("command_type") or 0) != 1:
            continue
        cmd = int(c["command_id"])
        partners[str(cmd)] = list(c.get("training_partner_array") or ())
        bonus[str(cmd)] = formula.bonus_params(
            chara_info, cmd, int(c.get("level") or 1), c.get("training_partner_array") or (),
            card_levels, slot_bonus=fb, training_pct=g, energy_down=ed, **flags)
    st["preview"] = {"turn": turn, "bonus": bonus, "partners": partners}
    roll_offers(full_state, chara_info, turn, commands)
    return (st["preview"], st["offers"]) != before


def preview_partners(st: dict, turn: int, command_id: int) -> list:
    pv = st["preview"]
    if int(pv.get("turn") or 0) != int(turn):
        return []
    return list((pv.get("partners") or {}).get(str(command_id)) or ())


def preview_bonus(st: dict, turn: int, command_id: int) -> list:
    pv = st["preview"]
    if int(pv.get("turn") or 0) != int(turn):
        return []
    return [dict(p) for p in pv["bonus"].get(str(command_id)) or ()]


def sprace_group(turn: int) -> int:
    return M.sprace_turns().get(int(turn or 0), 0)


def sprace_pending(st: dict, turn: int) -> bool:
    return bool(sprace_group(turn)) and int(turn) not in [int(t) for t in st["done_races"]]


def _category(distance: int) -> str:
    return "short" if distance <= 1400 else "mile" if distance <= 1800 else "middle" if distance <= 2400 else "long"


def best_category(race_history) -> tuple:
    tally: dict = {}
    for h in race_history or ():
        pid = int(h.get("program_id") or 0)
        if M.is_special_program(pid):
            continue
        course = M.program_course(pid)
        if course:
            key = (course[0], _category(course[1]))
            tally[key] = tally.get(key, 0) + 1
    if not tally:
        return (1, "middle")
    return max(CATEGORY_ORDER, key=lambda k: (tally.get(k, 0), -CATEGORY_ORDER.index(k)))


def pick_program(race_group: int, race_history, rng: random.Random) -> int:
    programs = M.group_programs(race_group)
    ground, cat = best_category(race_history)
    pool = [p for p, g, d in programs if g == ground and _category(d) == cat]
    pool = pool or [p for p, _g, d in programs if _category(d) == cat] or [p for p, _g, _d in programs]
    return rng.choice(pool) if pool else 0


def race_num(race_group: int, program_id: int) -> int:
    course = M.program_course(program_id) or (1, 2000)
    k = 5 if course[0] == 2 else ("short", "mile", "middle", "long").index(_category(course[1])) + 1
    return (SPRACE_INDEX.get(race_group, 1) - 1) * 5 + k


def announce(full_state: dict, chara_info: dict, turn: int) -> dict:
    st = state(full_state)
    sp = st["sprace"]
    if int(sp.get("turn") or 0) == int(turn) and sp.get("program_id"):
        return sp
    group = sprace_group(turn)
    smt = _smt()
    history = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("race_history") or []
    rng = rng_for(full_state, "sprace", turn)
    program = pick_program(group, history, rng)
    cond = smt._career_race_conditions(program, chara_info)
    st["sprace"] = {"turn": int(turn), "group": group, "program_id": program,
                    "weather": int(cond["weather"]), "ground_condition": int(cond["ground_condition"]),
                    "stage": "announced"}
    log.info("venus: special race %s on turn %s -> program %s", group, turn, program)
    return st["sprace"]


def race_condition(st: dict, turn: int):
    sp = st["sprace"]
    if int(sp.get("turn") or 0) == int(turn) and sp.get("program_id") and sp.get("stage") != "out":
        return {"program_id": int(sp["program_id"]), "weather": int(sp["weather"]),
                "ground_condition": int(sp["ground_condition"])}
    return []


def _named_rows(chara_ids, suffix: int = 900) -> list:
    from ... import master_data
    if not chara_ids:
        return []
    marks = ",".join("?" * len(chara_ids))
    return [dict(r) for r in master_data.query(
        f"SELECT * FROM single_mode_npc WHERE id IN ({marks})",
        tuple(int(c) * 1000 + suffix for c in chara_ids))]


def _npc_row(row_id: int) -> dict | None:
    from ... import master_data
    r = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?", (int(row_id),))
    return dict(r) if r else None


def _named_opponent(row: dict, factor: float, rng: random.Random, index: int) -> dict:
    smt = _smt()
    chara = int(row["chara_id"])
    out = {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": int(row["id"]), "single_mode_chara_id": int(row["id"]),
        "nickname_id": 0, "card_id": 0, "mob_id": 0, "chara_id": chara,
        "rarity": 3, "talent_level": 1, "skill_array": smt._npc_skills(row),
        "running_style": smt._npc_running_style(row),
        "race_dress_id": int(row.get("race_dress_id") or chara * 100 + 1),
    }
    for stat, col in (("speed", "speed"), ("stamina", "stamina"), ("power", "pow"),
                      ("guts", "guts"), ("wiz", "wiz")):
        out[stat] = max(1, round(int(row[col]) * factor * rng.uniform(1 - RACE_JITTER, 1 + RACE_JITTER)))
    for k in ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
              "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
              "proper_running_style_sashi", "proper_running_style_oikomi",
              "proper_ground_turf", "proper_ground_dirt"):
        out[k] = int(row.get(k) or 1)
    lo, hi = int(row.get("motivation_min") or 4), int(row.get("motivation_max") or 5)
    out["motivation"] = rng.randint(min(lo, hi), max(lo, hi))
    return out


def race_field(full_state: dict, chara_info: dict, program_id: int, count: int) -> list | None:
    group = next((g for g in (50001, 50002, 50003, 50004)
                  if int(program_id) in {p for p, _g, _d in M.group_programs(g)}), 0)
    if not group or count <= 0:
        return None
    smt = _smt()
    rng = random.Random(f"venus-field:{CE.career_salt(full_state)}:{program_id}:{turn_of(chara_info)}")
    factor = RACE_FACTOR[group]
    trainee = int(chara_info.get("card_id") or 0) // 100
    out: list = []
    if group == 50004:
        for row_id in BOSS_ROWS:
            row = _npc_row(row_id)
            if row:
                out.append(_named_opponent(row, factor, rng, len(out)))
    for chara, row_id in RIVAL_ROWS[group].items():
        if chara in OPTIONAL_RIVALS.get(group, ()) and rng.random() < 0.5:
            continue
        row = _npc_row(row_id)
        if row and chara != trainee:
            out.append(_named_opponent(row, factor, rng, len(out)))
    from ... import master_data
    pool = [int(r["chara_id"]) for r in master_data.query(
        "SELECT chara_id FROM single_mode_npc WHERE id % 1000 = 900 AND chara_id BETWEEN 1000 AND 1999")]
    pool = sorted(c for c in set(pool) if c != trainee)
    rng.shuffle(pool)
    want = max(0, min(count - len(out), NAMED_COUNT[group]))
    for row in _named_rows(pool[:want]):
        out.append(_named_opponent(row, factor, rng, len(out)))
    rest = count - len(out)
    if rest > 0:
        out += smt._mob_opponents(chara_info, rest)
    return out[:count]


def reward_effects(full_state: dict, chara_info: dict, race_group: int, won: bool,
                   running_style: int, turn: int) -> list:
    smt = _smt()
    st = state(full_state)
    idx = SPRACE_INDEX.get(race_group, 1)
    rb = smt._race_bonus_pct(chara_info, full_state)
    mult = (1 + rb / 100) * race_mult(st, turn)

    def pay(v):
        return int(v * mult + 1e-9)

    stats, sp = (WIN_BASE if won else LOSS_BASE)[idx]
    effects = [{"type": "all_stats", "value": f"+{pay(stats)}"},
               {"type": "skill_points", "value": f"+{pay(sp)}"}]
    if not won:
        return effects
    if level(st, M.DARLEY) >= idx:
        effects.append({"type": "skill_points", "value": f"+{pay(DARLEY_SP[idx])}"})
    if level(st, M.BYERLEY) >= idx:
        effects.append({"type": "random_stats", "value": f"+{pay(BYERLEY_STATS)}", "stat_count": idx})
    if level(st, M.GODOLPHIN) >= idx:
        rng = rng_for(full_state, "strategy", turn)
        for skill in style_hints(chara_info, running_style, idx, rng):
            effects.append({"type": "skill_hint", "skill_id": skill, "value": "+1"})
    effects.append({"type": "npc_bond", "target_id": 102, "value": "+5"})
    return effects


_STYLE_TAG = {1: "逃げ", 2: "先行", 3: "差し", 4: "追込"}


def style_hints(chara_info: dict, running_style: int, n: int, rng: random.Random) -> list:
    from ... import master_data
    style = int(running_style or 3)
    rows = master_data.query(
        "SELECT id FROM skill_data WHERE rarity=1 AND id BETWEEN 200000 AND 209999 "
        "AND (condition_1 LIKE ? OR condition_2 LIKE ?)",
        (f"%running_style=={style}%", f"%running_style=={style}%"))
    pool = sorted(int(r["id"]) for r in rows)
    rng.shuffle(pool)
    return pool[:n]


def gm_program(full_state: dict, chara_info: dict, race_history) -> int:
    rng = rng_for(full_state, "gm")
    return pick_program(M.GM_RACE_GROUP, race_history, rng)


def evaluation_array(chara_info: dict) -> list:
    out = [{"target_id": pos, "chara_id": 0, "member_state": 0}
           for pos in sorted(formula.RF.deck(chara_info))]
    for target, chara in npc_partners(chara_info):
        out.append({"target_id": target, "chara_id": chara, "member_state": 1})
    return out


def npc_partners(chara_info: dict) -> list:
    from ... import master_data
    deck_charas = {formula.RF.card_meta(sid)[3] for sid in formula.RF.deck(chara_info).values()}
    rows = master_data.query(
        "SELECT partner_id, chara_id FROM single_mode_unique_chara WHERE scenario_id=? "
        "ORDER BY partner_id", (SCENARIO_ID,))
    return [(int(r["partner_id"]), int(r["chara_id"])) for r in rows
            if int(r["chara_id"]) not in deck_charas]


_FULL_ENDPOINTS = frozenset({"start", "load", "spirit_use"})


def reply_is_full(endpoint: str, data: dict) -> bool:
    call = (endpoint or "").rsplit("/", 1)[-1]
    ci = data.get("chara_info") or {}
    if RI.career_over(ci) and call != "load":
        return False
    if call in _FULL_ENDPOINTS:
        return True
    return (not data.get("unchecked_event_array") and int(ci.get("playing_state") or 1) == 1
            and call in ("check_event", "exec_command", "race_out", "gain_skills",
                         "change_short_cut", "venus_race_out"))


def command_info_array(st: dict, home_info: dict, turn: int) -> list:
    rows = []
    for c in (home_info or {}).get("command_info_array") or ():
        ctype, cid = int(c.get("command_type") or 0), int(c.get("command_id") or 0)
        params = preview_bonus(st, turn, cid) if ctype == 1 else []
        rows.append({"command_type": ctype, "command_id": cid, "params_inc_dec_info_array": params})
    return rows


def data_set(full_state: dict, chara_info: dict, home_info: dict, full: bool, *,
             race_start_info=None, race_scenario=None, race_reward_info=None) -> dict:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    return {
        "race_start_info": race_start_info,
        "race_scenario": race_scenario,
        "command_info_array": command_info_array(st, home_info, turn) if full else None,
        "evaluation_info_array": evaluation_array(chara_info),
        "spirit_info_array": spirit_info_array(st),
        "venus_spirit_active_effect_info_array": active_rows(st, turn),
        "venus_chara_info_array": [{"chara_id": g, "venus_level": level(st, g)} for g in M.GODDESSES],
        "venus_chara_command_info_array": command_info_rows(st, turn),
        "venus_race_condition": race_condition(st, turn),
        "venus_race_history_array": [dict(r) for r in st["races"]],
        "race_reward_info": race_reward_info,
        "live_item_id": 0,
    }


def link_card(chara_info: dict):
    for pos, sid in formula.RF.deck(chara_info).items():
        if sid == M.LINK_CARD:
            return pos, sid
    return None
