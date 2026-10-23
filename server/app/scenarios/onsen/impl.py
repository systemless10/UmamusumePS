
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ... import event_engine
from ..ramen import impl as RI
from . import dig as D, formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 12
STATE_KEY = "onsen"

TICKET_CAP = 3
FIRST_PICK_TICKETS = 2
ASSISTANT_TICKETS = 1
BUFF_TURNS = 2
SUPERIOR_ENERGY_CAP = 150
LAST_DIG_TURN = 72
SUMMER_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
FIRST_TURNS = frozenset({1, 2})
SPRINGS_OPENED_BY = {3: (2, 3, 4), 24: (5, 6, 7, 8), 48: (9,), 65: (10,)}
BATH_BOND_CARDS, BATH_BOND = 3, 10
COPY_MIN_TYPES = 5
SUPERIOR_THRESHOLD = ((50, 3), (60, 16), (70, 119), (80, 10), (90, 49), (100, 27),
                      (110, 69), (120, 44), (130, 50), (140, 45), (150, 36), (160, 77),
                      (170, 67), (180, 45), (190, 49), (200, 50), (210, 42), (220, 50),
                      (230, 32), (240, 28), (250, 26), (260, 41), (270, 19), (290, 3),
                      (300, 6))
ASSISTANT_COUNT_WEIGHTS = (10, 20, 45, 20, 5)
NO_ASSISTANT_TURNS = frozenset({3})

RESULT_EVENTS = {1: {2: 212022, 1: 212023}, 2: {2: 212024, 1: 212025},
                 3: {3: 212026, 2: 212027, 1: 212028}}
RESULT_STORIES = {212022: 400012005, 212023: 400012006, 212024: 400012011,
                  212025: 400012012, 212026: 400012017, 212027: 400012018,
                  212028: 400012019}
RESULT_TIMING = 21
SKILL_KISEN = 202801
SKILL_JICHU = 202791
SKILL_HOYO = 210371
PARTY_REWARD = {
    (1, 2): (10, 150, ()),
    (1, 1): (5, 100, ()),
    (2, 2): (30, 200, ((SKILL_KISEN, 1),)),
    (2, 1): (20, 150, ()),
    (3, 3): (40, 300, ((SKILL_JICHU, 3), (SKILL_HOYO, 3))),
    (3, 2): (25, 200, ((SKILL_JICHU, 1), (SKILL_HOYO, 1))),
    (3, 1): (20, 150, ((SKILL_HOYO, 1),)),
}


def _blank() -> dict:
    return {
        "selected": M.BASE_ONSEN, "rest": {}, "dug": [M.BASE_ONSEN],
        "items": {"1": 1, "2": 1, "3": 1}, "open": [M.BASE_ONSEN],
        "enabled": False, "blue": [], "tickets": 0,
        "bath_turn": 0, "superior": 0, "spent": 0, "threshold": 0, "last_vital": None,
        "results": [], "new_result": 0,
        "assistants": {"turn": 0, "ids": []}, "copies": {"turn": 0, "map": {}},
        "preview": {"turn": 0, "bonus": {}, "dig": {}},
        "dig_due": {}, "complete_due": {}, "hold_turn": 0, "flags": {},
        "link_steps": {}, "completions": 0, "pr_turn": 0, "bath_springs": [],
        "energy_cap": 0,
    }


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    if not st["rest"]:
        st["rest"] = {str(sid): vol for sid, (_o, _t, vol) in M.stratum_info().items()}
    return st


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


def turn_of(chara_info: dict) -> int:
    return int((chara_info or {}).get("turn") or 1)


def served_turn(full_state: dict, chara_info: dict) -> int:
    return int(state(full_state).get("hold_turn") or 0) or turn_of(chara_info)


def flag(st: dict, name: str, value=None):
    if value is not None:
        st["flags"][name] = value
        return value
    return st["flags"].pop(name, None)


def rng_for(full_state: dict, *parts) -> random.Random:
    return random.Random(":".join(["onsen", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def deck(chara_info: dict) -> dict:
    return formula.RF.deck(chara_info)


def linked_charas(chara_info: dict) -> set:
    return RI.linked_charas(chara_info)


def kiyoko_card(chara_info: dict):
    for pos, sid in deck(chara_info).items():
        _kind, _cmd, rarity, chara = formula.RF.card_meta(sid)
        if chara == M.KIYOKO_CHARA:
            return pos, sid, rarity
    return None


def deck_type_count(chara_info: dict) -> int:
    return RI.deck_type_count(chara_info)


def spring_state(st: dict, onsen_id: int) -> int:
    if int(onsen_id) == int(st["selected"]):
        return 2
    return 1 if int(onsen_id) in st["open"] else 0


def layers(st: dict, onsen_id: int) -> list:
    return [(sid, kind, int(st["rest"].get(str(sid), vol)))
            for sid, kind, vol in M.strata().get(int(onsen_id), ())]


def spring_done(st: dict, onsen_id: int) -> bool:
    return all(rest <= 0 for _sid, _k, rest in layers(st, onsen_id))


def rank(st: dict) -> int:
    dug = set(int(x) for x in st["dug"])
    best = 1
    for r, need, required, _group in M.ryokan_ranks():
        if len(dug) >= need and (not required or required in dug):
            best = max(best, r)
    return best


def rank_effects(st: dict) -> list:
    r = rank(st)
    group = next((g for rr, _n, _req, g in M.ryokan_ranks() if rr == r), 0)
    return M.effects().get(group, []) if r > 1 else []


def rank_effect_sum(st: dict, effect_type: int) -> int:
    return sum(v2 for t, _v1, v2, _v3, _v4 in rank_effects(st) if t == effect_type)


def open_springs(st: dict, onsen_ids) -> None:
    for o in onsen_ids:
        if int(o) not in st["open"]:
            st["open"].append(int(o))


def dig_active(st: dict, turn: int) -> bool:
    return (bool(st["enabled"]) and turn <= LAST_DIG_TURN and turn not in SUMMER_TURNS
            and int(st["selected"]) != M.BASE_ONSEN and not spring_done(st, st["selected"]))


def tools(st: dict, chara_info: dict, level_up: bool = False) -> list:
    out = []
    for kind in (D.SAND, D.SOIL, D.ROCK):
        item = M.item_for_stratum().get(kind, kind)
        level = int(st["items"].get(str(item), 1))
        if level_up:
            level = min(M.max_item_level(), level + 1)
        out.append({"stratum_type": kind, "item_id": item, "item_level": level,
                    "dig_effect_value": D.tool_value(chara_info, kind, level, st["blue"], st["enabled"]),
                    "is_enable": bool(st["enabled"])})
    return out


def dig_layers(st: dict, chara_info: dict) -> list:
    linked = linked_charas(chara_info)
    tool = {t["stratum_type"]: t["dig_effect_value"] for t in tools(st, chara_info)}
    return [(sid, rest, D.multiplier(tool[kind], D.link_power(linked, kind)))
            for sid, kind, rest in layers(st, st["selected"]) if rest > 0]


def dig_plan(full_state: dict, chara_info: dict, base: int, turn: int = 0) -> list:
    st = state(full_state)
    turn = turn or served_turn(full_state, chara_info)
    if not dig_active(st, turn) or base <= 0:
        return []
    return D.plan(base, dig_layers(st, chara_info))


def training_base(partners, chara_info: dict) -> int:
    by_pos = deck(chara_info)
    return D.BASE_TRAINING + sum(1 for p in partners or () if p in by_pos)


def plan_bonus(st: dict, digs: list) -> dict:
    return D.plan_bonus(digs, st["rest"])


def apply_dig(full_state: dict, chara_info: dict, digs: list, pay: bool) -> dict:
    st = state(full_state)
    bonus = plan_bonus(st, digs)
    for sid, amount in digs:
        st["rest"][str(sid)] = max(0, int(st["rest"].get(str(sid), 0)) - int(amount))
    if pay and bonus:
        effects = [{"type": {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}[tt],
                    "value": f"+{v}"} for tt, v in sorted(bonus.items()) if 1 <= tt <= 5 and v]
        event_engine._apply_effects(chara_info, effects)
        energy = int(bonus.get(D.THRESHOLD_ENERGY, 0))
        if energy:
            chara_info["vital"] = max(0, int(chara_info.get("vital") or 0) + energy)
    return bonus


def completed_now(st: dict) -> bool:
    sel = int(st["selected"])
    return sel != M.BASE_ONSEN and sel not in st["dug"] and spring_done(st, sel)


def ticket_num(chara_info: dict, type_id: int) -> int:
    k = kiyoko_card(chara_info)
    if k:
        n = M.spa_tickets().get((type_id, M.KIYOKO_CHARA, k[2]), 0)
        if n:
            return n
    return M.spa_tickets().get((type_id, 0, 0), 0)


def add_tickets(st: dict, n: int) -> None:
    if n <= 0:
        return
    have = int(st["tickets"])
    if have >= TICKET_CAP:
        st["flags"]["not_gain_ticket"] = True
    st["tickets"] = min(TICKET_CAP, have + n)


def remain(st: dict, turn: int) -> int:
    bt = int(st["bath_turn"] or 0)
    if not bt or turn < bt or turn >= bt + BUFF_TURNS:
        return 0
    return bt + BUFF_TURNS - turn


def buff_active(st: dict, turn: int) -> bool:
    return remain(st, turn) > 0


def effected(st: dict, turn: int) -> list:
    if not buff_active(st, turn):
        return []
    return [int(o) for o in (st.get("bath_springs") or [M.BASE_ONSEN])]


def buff_rows(st: dict, turn: int) -> list:
    out = []
    for g in effected(st, turn):
        out += M.effects().get(g, [])
    return out


def buff_sum(st: dict, turn: int, effect_type: int, col: int = 2) -> int:
    return sum(row[col] for row in buff_rows(st, turn) if row[0] == effect_type)


def friendship_pct(st: dict, turn: int, command_id: int) -> int:
    base = formula.SUMMER_TO_BASE.get(command_id, command_id)
    return sum(v2 for t, v1, v2, _v3, _v4 in buff_rows(st, turn)
               if t == M.EFFECT_FRIENDSHIP and v1 == base)


def training_pct(st: dict, turn: int) -> int:
    return buff_sum(st, turn, M.EFFECT_TRAINING)


def failure_down(st: dict, turn: int) -> int:
    return -buff_sum(st, turn, M.EFFECT_FAILURE)


def energy_down(st: dict, turn: int) -> int:
    return -buff_sum(st, turn, M.EFFECT_ENERGY_COST)


def hint_rate(st: dict, turn: int) -> int:
    return buff_sum(st, turn, M.EFFECT_HINT_RATE)


def specialty(st: dict) -> int:
    return rank_effect_sum(st, M.EFFECT_AFFINITY)


def copies_due(st: dict, chara_info: dict, turn: int) -> int:
    for t, _v1, v2, v3, _v4 in buff_rows(st, turn):
        if t == M.EFFECT_SUB_PARTNER and deck_type_count(chara_info) >= (v3 or COPY_MIN_TYPES):
            return v2
    return 0


def place_copies(full_state: dict, chara_info: dict, turn: int, occupied=None,
                 standing=None) -> dict:
    st = state(full_state)
    n = copies_due(st, chara_info, turn)
    if not n:
        return {}
    if st["copies"].get("turn") != turn:
        rng = rng_for(full_state, "copies", turn)
        normal = [p for p, sid in sorted(deck(chara_info).items()) if formula.RF.is_normal_card(sid)]
        picks = rng.sample(normal, min(n, len(normal)))
        placed: dict = {}
        for pos in picks:
            cmds = []
            for c in formula.TRAINING_COMMANDS:
                here = occupied.get(c, 0) if isinstance(occupied, dict) else 0
                count = here if isinstance(here, int) else len(here or ())
                here_ids = (standing.get(c) or () if isinstance(standing, dict)
                            else RI._placed_positions(full_state, chara_info, c))
                if pos not in here_ids and count + len(placed.get(str(c), ())) < 5:
                    cmds.append(c)
            if cmds:
                placed.setdefault(str(rng.choice(cmds)), []).append(pos)
        st["copies"] = {"turn": turn, "map": placed}
    return {int(k): list(v) for k, v in st["copies"].get("map", {}).items()}


class Refused(Exception):
    pass


def bathe(full_state: dict, chara_info: dict, turn: int) -> list:
    st = state(full_state)
    if int(st["tickets"]) < 1:
        raise Refused("no ticket")
    if buff_active(st, turn):
        raise Refused("the buff is still running")
    st["tickets"] = int(st["tickets"]) - 1
    st["bath_turn"] = turn
    st["bath_springs"] = [M.BASE_ONSEN] + [int(o) for o in st["dug"] if int(o) != M.BASE_ONSEN]
    superior = bool(st["superior"]) or any(
        t == M.EFFECT_ALWAYS_SUPERIOR for t, *_ in rank_effects(st))
    not_up = []
    vital = int(chara_info.get("vital") or 0)
    max_vital = int(chara_info.get("max_vital") or 100)
    energy = mood = 0
    for t, v1, v2, _v3, _v4 in buff_rows(st, turn):
        if t == M.EFFECT_PARAM and v1 == 10:
            energy += v2
        elif t == M.EFFECT_PARAM and v1 == 20:
            mood += v2
    if superior:
        for t, v1, v2, _v3, _v4 in M.effects().get(M.SUPERIOR_GROUP, []):
            if t == M.EFFECT_PARAM and v1 == 10:
                energy += v2
        cap = SUPERIOR_ENERGY_CAP
        st["energy_cap"] = SUPERIOR_ENERGY_CAP
    else:
        cap = max(max_vital, vital)
    if vital >= cap:
        not_up.append(6)
    chara_info["vital"] = min(cap, vital + energy)
    if mood:
        if int(chara_info.get("motivation") or 3) >= 5:
            not_up.append(20)
        event_engine._apply_effects(chara_info, [{"type": "mood", "value": f"+{mood}"}])
    _cure(chara_info)
    _bath_bonds(full_state, chara_info, turn)
    if superior:
        _superior(full_state, chara_info, turn)
    st["superior"] = 0
    st["last_vital"] = int(chara_info.get("vital") or 0)
    log.info("onsen: bath on turn %s (superior %s), %s tickets left", turn, superior, st["tickets"])
    return sorted(set(not_up))


def _cure(chara_info: dict) -> None:
    cured = set()
    for t, *vals in M.effects().get(M.BASE_ONSEN, []):
        if t == M.EFFECT_CURE:
            cured |= {v for v in vals if v}
    arr = chara_info.get("chara_effect_id_array") or []
    chara_info["chara_effect_id_array"] = [c for c in arr if c not in cured]


def _bath_bonds(full_state: dict, chara_info: dict, turn: int) -> None:
    by_pos = deck(chara_info)
    rows = [e for e in chara_info.get("evaluation_info_array") or ()
            if isinstance(e, dict) and e.get("target_id") in by_pos
            and int(e.get("evaluation") or 0) < 100]
    rng = rng_for(full_state, "bathbond", turn)
    rows.sort(key=lambda e: (int(e.get("evaluation") or 0), rng.random()))
    for e in rows[:BATH_BOND_CARDS]:
        e["evaluation"] = min(100, int(e.get("evaluation") or 0) + BATH_BOND)


def _superior(full_state: dict, chara_info: dict, turn: int) -> None:
    sp = hints = 0
    for t, v1, v2, _v3, _v4 in M.effects().get(M.SUPERIOR_GROUP, []):
        if t == M.EFFECT_PARAM and v1 == 30:
            sp += v2
        elif t == M.EFFECT_SKILL_HINT:
            hints += v2
    hints += rank_effect_sum(state(full_state), M.EFFECT_HINT_COUNT)
    if sp:
        event_engine._apply_effects(chara_info, [{"type": "skill_point", "value": f"+{sp}"}])
    pool = [sid for sid in deck(chara_info).values() if event_engine.card_hint_groups(sid)]
    rng = rng_for(full_state, "superior", turn)
    for _ in range(hints):
        if pool:
            RI.grant_card_hint(chara_info, rng.choice(pool), rng)


def superior_threshold(full_state: dict, *key) -> int:
    rng = rng_for(full_state, "superior-threshold", *key)
    floor = rng.choices([f for f, _w in SUPERIOR_THRESHOLD],
                        weights=[w for _f, w in SUPERIOR_THRESHOLD])[0]
    return floor + rng.randint(1, 10)


def track_energy(full_state: dict, chara_info: dict) -> bool:
    st = state(full_state)
    vital = int(chara_info.get("vital") or 0)
    if st.get("energy_cap") and vital <= int(chara_info.get("max_vital") or 100):
        st["energy_cap"] = 0
    last = st.get("last_vital")
    st["last_vital"] = vital
    if last is None or vital >= last or int(st["superior"]) or not st["enabled"]:
        return last != vital
    st["spent"] = int(st["spent"]) + (last - vital)
    if not st.get("threshold"):
        st["threshold"] = superior_threshold(full_state, turn_of(chara_info), st["spent"])
    if st["spent"] >= int(st["threshold"]):
        st["superior"] = 1
        st["spent"] = 0
        st["threshold"] = 0
        log.info("onsen: super recovery ready (turn %s)", turn_of(chara_info))
    return True


def pr_enabled(turn: int) -> bool:
    return turn not in FIRST_TURNS and turn not in SUMMER_TURNS


def link_targets(chara_info: dict) -> list:
    by_chara = {}
    for pos, sid in deck(chara_info).items():
        by_chara.setdefault(formula.RF.card_meta(sid)[3], pos)
    return [(by_chara.get(c, c), c) for c in D.LINK_CHARAS]


def assistants(full_state: dict, chara_info: dict, turn: int) -> list:
    st = state(full_state)
    if not pr_enabled(turn) or turn in NO_ASSISTANT_TURNS:
        return []
    if st["assistants"].get("turn") != turn:
        rng = rng_for(full_state, "assistants", turn)
        n = rng.choices(range(len(ASSISTANT_COUNT_WEIGHTS)), weights=ASSISTANT_COUNT_WEIGHTS)[0]
        ids = [t for t, _c in link_targets(chara_info)]
        st["assistants"] = {"turn": turn, "ids": rng.sample(ids, min(n, len(ids)))}
    return list(st["assistants"]["ids"])


def assistant_preview(full_state: dict, chara_info: dict, turn: int) -> tuple:
    st = state(full_state)
    digs = dig_plan(full_state, chara_info, D.BASE_ASSISTANT + len(assistants(full_state, chara_info, turn)), turn)
    bonus = plan_bonus(st, digs)
    params = []
    for tt, v in M.assistant_params():
        if tt == formula.TARGET_ENERGY:
            v += int(bonus.get(D.THRESHOLD_ENERGY, 0))
        elif 1 <= tt <= 5:
            v = formula.displayed(chara_info, tt, v)
        params.append({"target_type": tt, "value": v})
    bonus_params = formula.flat_params(chara_info, {tt: v for tt, v in bonus.items()
                                                    if tt != D.THRESHOLD_ENERGY})
    return params, bonus_params, digs


def refresh_preview(full_state: dict, chara_info: dict, home_info: dict, card_levels=None) -> bool:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    bonus, digs = {}, {}
    s = training_pct(st, turn)
    for c in (home_info or {}).get("command_info_array") or ():
        if c.get("command_type") != 1:
            continue
        cmd = int(c["command_id"])
        partners = list(c.get("training_partner_array") or ())
        plan = dig_plan(full_state, chara_info, training_base(partners, chara_info), turn)
        digs[str(cmd)] = plan
        f = (friendship_pct(st, turn, cmd)
             if formula.RF.is_friendship_training(chara_info, cmd, partners) else 0)
        bonus[str(cmd)] = formula.bonus_params(chara_info, cmd, int(c.get("level") or 1), partners,
                                               s, f, plan_bonus(st, plan), card_levels)
    changed = st["preview"] != {"turn": turn, "bonus": bonus, "dig": digs}
    st["preview"] = {"turn": turn, "bonus": bonus, "dig": {k: [list(x) for x in v] for k, v in digs.items()}}
    return changed


def stored_dig(st: dict, command_id: int, turn: int) -> list:
    preview = st["preview"]
    if preview.get("turn") != turn:
        return []
    for key in (command_id, formula.BASE_TO_SUMMER.get(command_id), formula.SUMMER_TO_BASE.get(command_id)):
        if key is not None and str(key) in preview["dig"]:
            return [tuple(x) for x in preview["dig"][str(key)]]
    return []


def party_pending(st: dict, turn: int) -> bool:
    sched = M.schedules().get(int(turn or 0))
    return bool(sched) and not any(r["schedule_id"] == sched for r in st["results"])


def commit_party(full_state: dict, chara_info: dict, turn: int):
    st = state(full_state)
    sched = M.schedules()[int(turn)]
    result = M.dug_result(sched, rank(st))
    st["results"] = [r for r in st["results"] if r["schedule_id"] != sched] + [
        {"schedule_id": sched, "result_type": result}]
    st["new_result"] = sched
    event_id = RESULT_EVENTS[sched][result]
    stats, sp, hints = PARTY_REWARD[(sched, result)]
    effects = [{"type": "all_stats", "value": f"+{stats}"},
               {"type": "skill_point", "value": f"+{sp}"}]
    effects += [{"type": "skill_hint", "skill_id": s, "value": f"+{n}"} for s, n in hints]
    ev = CE.Event(event_id=event_id, story_id=RESULT_STORIES[event_id], play_timing=RESULT_TIMING,
                  choices=[CE.Choice(effects=effects)], chara_id=0,
                  once_key=f"onsen:result:{sched}", priority=CE.PRIO_FRONT,
                  resolver="onsen_result", source="onsen",
                  payload={"schedule_id": sched, "result_type": result, "turn": int(turn)})
    log.info("onsen: bathing party %s -> result %s (rank %s)", sched, result, rank(st))
    return result, ev


def check_dug_result_array(st: dict) -> list:
    rows = sorted(st["results"], key=lambda r: r["schedule_id"])
    new = int(flag(st, "new_result_first") or 0)
    if new:
        rows = [r for r in rows if r["schedule_id"] == new] + [r for r in rows if r["schedule_id"] != new]
    return [{"schedule_id": r["schedule_id"], "result_type": r["result_type"]} for r in rows]


def last_result(st: dict, schedule_id: int) -> int:
    return next((r["result_type"] for r in st["results"] if r["schedule_id"] == schedule_id), 0)


def outing_dig(full_state: dict, chara_info: dict, turn: int, kiyoko: bool) -> list:
    return dig_plan(full_state, chara_info, D.BASE_KIYOKO_OUTING if kiyoko else D.BASE_REST, turn)


def race_base(chara_info: dict, turn: int) -> int:
    from ...handlers import single_mode_team as smt
    try:
        forced = smt._forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    except Exception:
        forced = frozenset()
    return D.BASE_GOAL_RACE if turn in forced else D.BASE_RACE


def kiyoko_outing_open(chara_info: dict) -> bool:
    k = kiyoko_card(chara_info)
    if not k:
        return False
    return any(int(e.get("target_id") or 0) == k[0] and int(e.get("is_outing") or 0)
               for e in chara_info.get("evaluation_info_array") or () if isinstance(e, dict))


def _dig_rows(digs) -> list:
    return [{"stratum_id": int(s), "dig_value": int(v)} for s, v in digs]


def data_set(full_state: dict, chara_info: dict, home_info: dict) -> dict:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    commands = [c for c in (home_info or {}).get("command_info_array") or ()
                if c.get("command_type") == 1]
    preview = st["preview"] if st["preview"].get("turn") == turn else {"bonus": {}, "dig": {}}
    rows = []
    for c in commands:
        cmd = int(c["command_id"])
        rows.append({"command_type": 1, "command_id": cmd,
                     "params_inc_dec_info_array": list(preview["bonus"].get(str(cmd)) or []),
                     "dig_info_array": _dig_rows(preview["dig"].get(str(cmd)) or [])})
    active = dig_active(st, turn)
    race_only = race_base(chara_info, turn) == D.BASE_GOAL_RACE
    if race_only:
        for row in rows:
            row["dig_info_array"] = []
    rest_dig = _dig_rows(outing_dig(full_state, chara_info, turn, False)) if active and not race_only else []
    kiyo_dig = _dig_rows(outing_dig(full_state, chara_info, turn, True)) if active else []
    race_dig = _dig_rows(dig_plan(full_state, chara_info, race_base(chara_info, turn), turn)) if active else []
    rows += [
        {"command_type": 3, "command_id": 301, "params_inc_dec_info_array": [], "dig_info_array": rest_dig},
        {"command_type": 3, "command_id": 390, "params_inc_dec_info_array": [], "dig_info_array": rest_dig},
        {"command_type": 4, "command_id": 401, "params_inc_dec_info_array": [], "dig_info_array": race_dig},
        {"command_type": 7, "command_id": 701, "params_inc_dec_info_array": [], "dig_info_array": rest_dig},
        {"command_type": 8, "command_id": 801, "params_inc_dec_info_array": [], "dig_info_array": []},
    ]
    outing = []
    if active:
        outing.append({"chara_id": 0, "dig_info_array": rest_dig})
        if kiyoko_outing_open(chara_info):
            outing.append({"chara_id": M.KIYOKO_CHARA, "dig_info_array": kiyo_dig})
    pr_on = pr_enabled(turn)
    if pr_on:
        params, bonus_params, pr_digs = assistant_preview(full_state, chara_info, turn)
    else:
        params = [{"target_type": tt, "value": v} for tt, v in M.assistant_params()]
        bonus_params, pr_digs = [], []
    evaluation = ([{"target_id": t, "chara_id": c} for t, c in link_targets(chara_info)]
                  if len(st["open"]) > 1 else [])
    return {
        "command_info_array": rows,
        "bathing_info": {"ticket_num": int(st["tickets"]),
                         "onsen_effect_remain_count": remain(st, turn),
                         "superior_state": int(st["superior"])},
        "onsen_info_array": [
            {"onsen_id": o, "state": spring_state(st, o),
             "stratum_info_array": [{"stratum_id": sid, "rest_volume": rest}
                                    for sid, _k, rest in layers(st, o)]}
            for o in sorted(M.onsens())],
        "dig_effect_info_array": tools(st, chara_info),
        "dug_onsen_id_array": [int(o) for o in st["dug"]],
        "effected_onsen_id_array": effected(st, turn),
        "level_up_dig_effect_info_array": tools(st, chara_info, level_up=True),
        "evaluation_info_array": evaluation,
        "assistant_command_info": {
            "command_type": 12, "is_enable": 1 if pr_on else 0,
            "assistant_partner_id_array": assistants(full_state, chara_info, turn) if pr_on else [],
            "params_inc_dec_info_array": params,
            "bonus_params_inc_dec_info_array": bonus_params,
            "dig_info_array": _dig_rows(pr_digs)},
        "ryokan_rank": rank(st),
        "ryokan_rank_clear_state": 2,
        "check_dug_result_array": check_dug_result_array(st),
        "not_up_parameter_info": {"not_gain_ticket": bool(st["flags"].get("not_gain_ticket"))},
        "outing_effect_array": outing,
    }
