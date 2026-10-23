
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 13
STATE_KEY = "breeders"
LAST_PICK_KEY = "breeders_last_bc_group"

ROUTE_ID = 30000
FINAL_TURN = 69
SUMMER_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
START_DP = {M.DECK_SSR: 10, M.DECK_R: 8, M.DECK_NONE: 5}
LEVEL_COST = 5
FULL_EXP = 3
MEMBER_WEIGHT_LINKED = 4
FOLLOW_CARD_CHANCE = 0.93
GAIN_EXP_COMMANDS = (3, 4, 7, 8)

REVIEW_PRE = {1: (213010, 400013005), 2: (213012, 400013009), 3: (213014, 400013013),
              4: (213016, 400013018), 5: (213007, 400013022)}
REVIEW_EXCELLENT = {1: (213003, 400013006), 2: (213004, 400013010), 3: (213005, 400013014),
                    4: (213006, 400013019), 5: (213018, 400013023)}
REVIEW_GOOD = {1: (213011, 400013007), 2: (213013, 400013011), 3: (213015, 400013015),
               4: (213017, 400013020), 5: (213019, 400013024)}
REVIEW_TIMING = 22
REVIEW_SHOW_CLEAR = 1301
SKILL_ZENSHIN = 200511
REVIEW_REWARD = {
    (1, True): (3, 15, 10, 1, 4), (2, True): (5, 15, 20, 1, 4), (3, True): (10, 15, 35, 1, 4),
    (4, True): (15, 15, 60, 1, 4), (5, True): (30, 15, 100, 1, 4),
    (1, False): (0, 5, 5, 0, 3), (2, False): (3, 5, 10, 0, 3), (3, False): (5, 5, 15, 0, 3),
    (4, False): (5, 5, 30, 0, 3), (5, False): (15, 5, 50, 0, 3),
}
UNION_OPEN = {1: (213809, 400013210), 2: (213811, 400013218), 3: (213813, 400013220)}
UNION_REWARD = {1: (213810, 400013211), 2: (213812, 400013219), 3: (213814, 400013221)}
UNION_PAY = {1: {"speed": 10, "sp": 10},
             2: {"speed": 5, "stamina": 10, "power": 10, "guts": 10, "wiz": 10, "sp": 40},
             3: {"speed": 25, "stamina": 25, "power": 25, "guts": 25, "wiz": 25, "sp": 60}}
UNION_NEED = {1: 3, 2: 6, 3: 8}
UNION_TURN = {2: 49}
UNION3_FROM = 59
BUILDING = {1081: ((213121, 400013213), (213123, 400013216)),
            1132: ((213101, 400013212), (213102, 400013215)),
            1136: ((213127, 400013214), (213130, 400013217))}
BUILDING_NEED = (4, 6)
BUILDING_PAY = ({"all": 6, "sp": 40}, {"all": 10, "sp": 60})


def _blank() -> dict:
    return {
        "members": [], "formed": False,
        "bc_program": 0, "bc_group": 0, "track": 0,
        "deck_id": M.DECK_NONE, "dp": 0, "pending": 0,
        "levels": {"1": 1, "2": 1, "3": 1},
        "stock": 0, "stock_max": M.STOCK_NORMAL, "limit": 0,
        "dreams": {"turn": 0, "tips": []},
        "reviews": [], "new_review": 0,
        "union_event": 0, "union_done": [], "progress": 0, "building_done": [],
        "bc_results": [], "placement": {"turn": 0, "map": {}},
        "preview": {"turn": 0, "bonus": {}, "members": {}},
        "plus1_due": [], "goal_due": {}, "hold_turn": 0, "flags": {},
        "dress": 0, "outings": 0, "route": [],
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
    return random.Random(":".join(["breeders", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def deck(chara_info: dict) -> dict:
    return formula.RF.deck(chara_info)


def deck_id_of(chara_info: dict) -> int:
    ids = set(deck(chara_info).values())
    if M.CASINO_SSR in ids:
        return M.DECK_SSR
    if M.CASINO_R in ids:
        return M.DECK_R
    return M.DECK_NONE


def casino_card(chara_info: dict):
    for pos, sid in deck(chara_info).items():
        if formula.RF.card_meta(sid)[3] == M.CASINO_CHARA:
            return pos, sid
    return None


def deck_type_count(chara_info: dict) -> int:
    return RI.deck_type_count(chara_info)


def linked_charas(chara_info: dict) -> set:
    return RI.linked_charas(chara_info)


def normal_positions(chara_info: dict) -> list:
    return [p for p, sid in sorted(deck(chara_info).items()) if formula.RF.is_normal_card(sid)]


def members(st: dict) -> list:
    return st["members"]


def member(st: dict, chara_id: int) -> dict | None:
    return next((m for m in st["members"] if int(m["chara_id"]) == int(chara_id)), None)


def team_rank(st: dict) -> int:
    return min((int(m["rank"]) for m in st["members"]), default=0)


def is_full(m: dict) -> bool:
    return int(m["exp"]) >= FULL_EXP and int(m["rank"]) < M.max_rank()


def can_gain(m: dict) -> bool:
    return int(m["rank"]) < M.max_rank() and int(m["exp"]) < FULL_EXP


def form_team(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    if st["members"]:
        return st["members"]
    trainee = int(chara_info.get("card_id") or 0) // 100
    in_deck = {formula.RF.card_meta(sid)[3] for sid in deck(chara_info).values()}
    pool = [c for c in M.members() if c != trainee]
    rng = rng_for(full_state, "team")
    picks = []
    while pool and len(picks) < 3:
        weights = [MEMBER_WEIGHT_LINKED if c in in_deck else 1 for c in pool]
        c = rng.choices(pool, weights=weights)[0]
        picks.append(c)
        pool.remove(c)
    st["members"] = [{"member_id": i + 1, "chara_id": c, "rank": 1, "exp": 0}
                     for i, c in enumerate(picks)]
    st["formed"] = True
    st["deck_id"] = deck_id_of(chara_info)
    log.info("breeders: team %s", picks)
    return st["members"]


def formation_story(st: dict) -> tuple:
    from itertools import combinations
    trio = tuple(sorted(int(m["chara_id"]) for m in st["members"]))
    combos = list(combinations(sorted(M.members()), 3))
    k = combos.index(trio) if trio in combos else 0
    return 213111 + k, 400013200 + k


def lowest(st: dict, rng: random.Random) -> dict | None:
    cands = [m for m in st["members"] if can_gain(m)]
    if not cands:
        return None
    key = lambda m: (int(m["exp"]), int(m["rank"]))
    best = min(key(m) for m in cands)
    return rng.choice([m for m in cands if key(m) == best])


def add_exp(full_state: dict, st: dict, m: dict, n: int = 1) -> int:
    got = 0
    for _ in range(int(n)):
        if not can_gain(m):
            break
        m["exp"] = int(m["exp"]) + 1
        got += 1
    return got


def rank_up(st: dict, m: dict) -> None:
    m["rank"] = min(M.max_rank(), int(m["rank"]) + 1)
    m["exp"] = 0
    st["pending"] = int(st["pending"]) + M.dp_rank_up(st["deck_id"])


def train_members(full_state: dict, st: dict, chara_ids) -> None:
    for c in chara_ids:
        m = member(st, c)
        if not m:
            continue
        if is_full(m):
            rank_up(st, m)
        else:
            add_exp(full_state, st, m, 1)


def plus_one(full_state: dict, tag) -> dict | None:
    st = state(full_state)
    m = lowest(st, rng_for(full_state, "plus1", tag))
    if m:
        add_exp(full_state, st, m, 1)
    return m


def outing_split(st: dict, rng: random.Random | None = None) -> dict:
    sim = {int(m["chara_id"]): [int(m["rank"]), int(m["exp"])] for m in st["members"]}
    rng = rng or random.Random(0)
    out: dict = {}
    for _ in range(3):
        cands = [c for c, (r, e) in sim.items() if e < FULL_EXP and r < M.max_rank()]
        if not cands:
            break
        best = min((sim[c][1], sim[c][0]) for c in cands)
        c = rng.choice([c for c in cands if (sim[c][1], sim[c][0]) == best])
        sim[c][1] += 1
        out[c] = out.get(c, 0) + 1
    return out


def upcoming_schedule(st: dict, turn: int):
    done = {int(r["schedule_id"]) for r in st["reviews"]}
    for t, sid in sorted(M.review_turns().items()):
        if t >= int(turn) and sid not in done:
            return sid
    return None


def need_for(schedule_id) -> int:
    row = M.schedule(schedule_id) if schedule_id else None
    return int(row[4]) if row else 0


def predict(st: dict, turn: int) -> int:
    out = int(st["pending"])
    sid = upcoming_schedule(st, turn)
    if sid and st["members"] and team_rank(st) >= need_for(sid):
        out += M.dp_review(st["deck_id"], sid)
    return out


def review_pending(st: dict, turn: int) -> bool:
    sid = M.review_turns().get(int(turn or 0))
    return bool(sid) and st["formed"] and not any(int(r["schedule_id"]) == sid for r in st["reviews"])


UNIQUE_LEVEL_REVIEWS = (3, 5)


def commit_review(full_state: dict, chara_info: dict, turn: int):
    st = state(full_state)
    sid = M.review_turns()[int(turn)]
    excellent = team_rank(st) >= need_for(sid)
    result = 2 if excellent else 1
    st["reviews"] = [r for r in st["reviews"] if r["schedule_id"] != sid] + [
        {"schedule_id": sid, "result_type": result}]
    st["new_review"] = sid
    if sid == 5:
        st["stock_max"] = M.STOCK_LIMIT_UP
    event_id, story = (REVIEW_EXCELLENT if excellent else REVIEW_GOOD)[sid]
    stats, energy, sp, mood, _hints = REVIEW_REWARD[(sid, excellent)]
    effects = [{"type": "all_stats", "value": f"+{stats}"}] if stats else []
    effects += [{"type": "energy", "value": f"+{energy}"},
                {"type": "skill_points", "value": f"+{sp}"}]
    if mood:
        effects.append({"type": "mood", "value": f"+{mood}"})
    if excellent and sid == 4:
        effects.append({"type": "skill_hint", "skill_id": SKILL_ZENSHIN, "value": "+1"})
    if excellent and sid in UNIQUE_LEVEL_REVIEWS:
        effects.append({"type": "unique_skill_level", "value": "+1"})
    ev = CE.Event(event_id=event_id, story_id=story, play_timing=REVIEW_TIMING,
                  choices=[CE.Choice(effects=effects)], chara_id=0,
                  once_key=f"breeders:review:{sid}", priority=CE.PRIO_FRONT,
                  resolver="breeders_review_result", source="breeders",
                  show_clear=REVIEW_SHOW_CLEAR,
                  show_clear_sort_id=RI.goal_sort_id(chara_info, int(turn)),
                  payload={"schedule_id": sid, "excellent": excellent, "turn": int(turn)})
    log.info("breeders: Reflection %s -> %s (team rank %s, need %s)", sid,
             "Excellent" if excellent else "Good", team_rank(st), need_for(sid))
    return result, ev


def review_payout(full_state: dict, chara_info: dict, schedule_id: int, excellent: bool) -> None:
    st = state(full_state)
    turn = [t for t, s in M.review_turns().items() if s == schedule_id][0]
    gain = int(st["pending"]) + (M.dp_review(st["deck_id"], schedule_id) if excellent else 0)
    st["dp"] = int(st["dp"]) + gain
    st["pending"] = 0
    st["stock"] = int(st["stock_max"])
    st["limit"] = M.level_limit_at(turn)
    hints = REVIEW_REWARD[(schedule_id, excellent)][4]
    pool = [sid for sid in deck(chara_info).values() if formula.RF.is_normal_card(sid)]
    rng = rng_for(full_state, "review-hints", schedule_id)
    for _ in range(hints):
        if pool:
            RI.grant_card_hint(chara_info, rng.choice(pool), rng)


def review_result_array(st: dict) -> list:
    rows = sorted(st["reviews"], key=lambda r: r["schedule_id"])
    new = int(st.get("new_review") or 0)
    if new:
        rows = [r for r in rows if r["schedule_id"] == new] + [r for r in rows if r["schedule_id"] != new]
    return [{"schedule_id": r["schedule_id"], "result_type": r["result_type"]} for r in rows]


class Refused(Exception):
    pass


def level_limit(st: dict, turn: int) -> int:
    return int(st.get("limit") or 0) or M.level_limit_at(turn)


def apply_meeting(st: dict, turn: int, levels: list) -> int:
    want = {int(e.get("group_type") or 0): int(e.get("level") or 0) for e in levels or ()}
    limit = level_limit(st, turn)
    cost = 0
    for g in M.GROUPS:
        have = int(st["levels"].get(str(g), 1))
        new = want.get(g, have)
        if new < have or new > limit:
            raise Refused(f"group {g}: {have} -> {new} (limit {limit})")
        cost += (new - have) * LEVEL_COST
    if cost > int(st["dp"]):
        raise Refused(f"cost {cost} > {st['dp']} DP")
    for g in M.GROUPS:
        st["levels"][str(g)] = want.get(g, int(st["levels"].get(str(g), 1)))
    st["dp"] = int(st["dp"]) - cost
    return cost


def enhance_groups(st: dict) -> list:
    return [{"group_type": g, "level": int(st["levels"].get(str(g), 1))} for g in M.GROUPS]


def active_effect_ids(st: dict, chara_info: dict) -> list:
    types = deck_type_count(chara_info)
    rows = []
    for g in (M.GROUP_COMMON,) + M.GROUPS:
        level = 0 if g == M.GROUP_COMMON else int(st["levels"].get(str(g), 1))
        _need, group = M.sp_levels().get((g, level), (0, 0))
        rows += [r for r in M.sp_effects().get(group, []) if r[2] <= types]
    if any(r[1] == M.EFF_SUB_PARAM and r[0] >= 10000 for r in rows):
        rows = [r for r in rows if not (r[1] == M.EFF_SUB_PARAM and r[0] < 10000)]
    five = sorted(r[0] for r in rows if r[2])
    rest = sorted(r[0] for r in rows if not r[2])
    return five + rest


def effect_sums(st: dict, chara_info: dict) -> dict:
    by_id = {r[0]: r for rows in M.sp_effects().values() for r in rows}
    out: dict = {}
    for eid in active_effect_ids(st, chara_info):
        r = by_id.get(eid)
        if r:
            out[r[1]] = out.get(r[1], 0) + r[3]
    return out


def dreams_active(st: dict, turn: int) -> bool:
    return int(st["dreams"].get("turn") or 0) == int(turn)


def dreams_caps(st: dict, chara_info: dict, turn: int) -> tuple:
    if not dreams_active(st, turn):
        return (formula.LINE_CAP,) * 4
    e = effect_sums(st, chara_info)
    return (formula.LINE_CAP + e.get(M.EFF_BASE_MAIN_LIMIT, 0),
            formula.LINE_CAP + e.get(M.EFF_BASE_SP_LIMIT, 0),
            formula.LINE_CAP + e.get(M.EFF_ADD_MAIN_LIMIT, 0),
            formula.LINE_CAP + e.get(M.EFF_ADD_SP_LIMIT, 0))


def sub_pct(st: dict, chara_info: dict, turn: int) -> int:
    if not dreams_active(st, turn):
        return 100
    return int(effect_sums(st, chara_info).get(M.EFF_SUB_PARAM, 10))


def activate_dreams(full_state: dict, chara_info: dict, turn: int, board_tips) -> None:
    st = state(full_state)
    if int(st["stock"]) < 1:
        raise Refused("no DREAMS stock")
    if dreams_active(st, turn):
        raise Refused("DREAMS already active")
    e = effect_sums(st, chara_info)
    normal = normal_positions(chara_info)
    tips = [p for p in board_tips if p in normal]
    rng = rng_for(full_state, "dreams-tips", turn)
    guarantee = int(e.get(M.EFF_GUARANTEE_TIPS, 0))
    extra = [p for p in normal if p not in tips]
    rng.shuffle(extra)
    tips += extra[:guarantee]
    if M.EFF_ALL_TIPS in e:
        tips = list(normal)
    st["stock"] = int(st["stock"]) - 1
    st["dreams"] = {"turn": int(turn), "tips": sorted(set(tips))}
    log.info("breeders: DREAMS on turn %s (stock %s left)", turn, st["stock"])


def dreams_partners(chara_info: dict) -> list:
    return normal_positions(chara_info)


def place_members(full_state: dict, chara_info: dict, turn: int, board: dict) -> dict:
    st = state(full_state)
    if st["placement"].get("turn") == turn and st["placement"].get("map"):
        return {int(k): int(v) for k, v in st["placement"]["map"].items()}
    rng = rng_for(full_state, "place", turn)
    by_pos = deck(chara_info)
    cards_at = {cmd: [p for p in ids if p in by_pos] for cmd, ids in board.items()}
    placed = {}
    commands = list(board)
    for m in st["members"]:
        c = int(m["chara_id"])
        own = [p for p, sid in by_pos.items() if formula.RF.card_meta(sid)[3] == c]
        home = next((cmd for cmd, ids in cards_at.items() if own and own[0] in ids), None)
        if home is None:
            with_cards = [cmd for cmd, ids in cards_at.items() if ids]
            if with_cards and rng.random() < FOLLOW_CARD_CHANCE:
                weights = [len(cards_at[cmd]) for cmd in with_cards]
                home = rng.choices(with_cards, weights=weights)[0]
            else:
                home = rng.choice(commands) if commands else 0
        placed[c] = int(home)
    st["placement"] = {"turn": turn, "map": {str(k): v for k, v in placed.items()}}
    return placed


def members_at(full_state: dict, chara_info: dict, turn: int, command_id: int, board: dict) -> list:
    st = state(full_state)
    if dreams_active(st, turn):
        return [int(m["chara_id"]) for m in st["members"]]
    placed = place_members(full_state, chara_info, turn, board)
    return [c for c, cmd in placed.items() if cmd == int(command_id)]


def refresh_preview(full_state: dict, chara_info: dict, home_info: dict, card_levels=None) -> bool:
    st = state(full_state)
    if not st["formed"]:
        return False
    turn = served_turn(full_state, chara_info)
    commands = [c for c in (home_info or {}).get("command_info_array") or ()
                if c.get("command_type") == 1]
    board = {int(c["command_id"]): list(c.get("training_partner_array") or ()) for c in commands}
    active = dreams_active(st, turn)
    e = effect_sums(st, chara_info) if active else {}
    caps = dreams_caps(st, chara_info, turn)
    sub = sub_pct(st, chara_info, turn)
    tr = team_rank(st)
    bonus, at = {}, {}
    for c in commands:
        cmd = int(c["command_id"])
        partners = board[cmd]
        here = members_at(full_state, chara_info, turn, cmd, board)
        at[str(cmd)] = here
        mpct = sum(M.member_bonus(int(member(st, h)["rank"]), is_full(member(st, h))) for h in here)
        rainbow = formula.RF.is_friendship_training(chara_info, cmd, partners)
        bonus[str(cmd)] = formula.bonus_params(
            chara_info, cmd, int(c.get("level") or 1), partners, card_levels,
            member_pct=mpct, sp_pct=int(e.get(M.EFF_SKILL_PT, 0)),
            team_f=M.team_rank_effect(tr, M.TEAM_TAG_TRAINING) if rainbow else 0,
            dreams_f=int(e.get(M.EFF_TAG_TRAINING, 0)) if rainbow else 0,
            sub_pct=sub, main_cap=caps[0], sp_cap=caps[1], add_main_cap=caps[2],
            add_sp_cap=caps[3], energy_down=-int(e.get(M.EFF_CONSUME_HP, 0)))
    new = {"turn": turn, "bonus": bonus, "members": at}
    changed = st["preview"] != new
    st["preview"] = new
    return changed


def preview_members(st: dict, turn: int, command_id: int) -> list:
    pv = st["preview"]
    if pv.get("turn") != turn:
        return []
    for key in (command_id, formula.BASE_TO_SUMMER.get(command_id), formula.SUMMER_TO_BASE.get(command_id)):
        if key is not None and str(key) in pv["members"]:
            return list(pv["members"][str(key)])
    return []


def preview_bonus(st: dict, turn: int, command_id: int) -> list:
    pv = st["preview"]
    if pv.get("turn") != turn:
        return []
    return [dict(p) for p in pv["bonus"].get(str(command_id)) or ()]


def _rank_up_predict(st: dict, here: list) -> int:
    ms = [member(st, c) for c in here if member(st, c)]
    if not ms:
        return 0
    if all(int(m["rank"]) >= M.max_rank() for m in ms):
        return 3
    return 2 if any(is_full(m) for m in ms) else 1


def casino_outing_open(chara_info: dict) -> bool:
    k = casino_card(chara_info)
    if not k:
        return False
    return any(int(e.get("target_id") or 0) == k[0] and int(e.get("is_outing") or 0)
               for e in chara_info.get("evaluation_info_array") or () if isinstance(e, dict))


def is_goal_turn(chara_info: dict, turn: int) -> bool:
    from ...handlers import single_mode_team as smt
    try:
        return int(turn) in smt._forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    except Exception:
        return False


BC_STAT_FACTORS = {"speed": 1.778, "stamina": 1.468, "power": 1.668, "guts": 1.468, "wiz": 1.418}
BC_STAT_JITTER = 0.01


def bc_field(chara_info: dict, program_id, count: int) -> list | None:
    from ...handlers import race_mobs, single_mode_team as smt
    rows = M.bc_npc_rows(int(program_id or 0)) if program_id else ()
    if not rows:
        return None
    rng = random.Random(f"breeders-bc:{chara_info.get('card_id')}:{program_id}:"
                        f"{chara_info.get('speed')}:{chara_info.get('skill_point')}")
    picks = rng.sample(list(rows), min(count, len(rows)))
    faces = list(smt._mob_id_pool())
    rng.shuffle(faces)
    out = []
    for i, row in enumerate(picks):
        opp = smt._mob_opponent_from_npc(row, {}, i)
        wire = {"speed": "speed", "stamina": "stamina", "power": "pow", "guts": "guts", "wiz": "wiz"}
        for stat, col in wire.items():
            opp[stat] = max(1, round(row[col] * BC_STAT_FACTORS[stat]
                                     * rng.uniform(1 - BC_STAT_JITTER, 1 + BC_STAT_JITTER)))
        lo, hi = int(row.get("motivation_min") or 3), int(row.get("motivation_max") or 5)
        opp["motivation"] = rng.randint(min(lo, hi), max(lo, hi))
        if int(opp.get("mob_id") or 0) > race_mobs.MAX_RACE_MOB_ID and faces:
            opp["mob_id"] = faces[i % len(faces)]
        out.append(opp)
    return out


_FULL_ENDPOINTS = frozenset({"start", "load", "team_sp_training"})


def reply_is_full(endpoint: str, data: dict) -> bool:
    call = (endpoint or "").rsplit("/", 1)[-1]
    ci = data.get("chara_info") or {}
    if call in _FULL_ENDPOINTS:
        return True
    if call not in ("check_event", "exec_command", "team_meeting", "race_out"):
        return False
    return (not data.get("unchecked_event_array") and int(ci.get("playing_state") or 1) == 1
            and int(ci.get("state") or 0) != 3)


def data_set(full_state: dict, chara_info: dict, home_info: dict, full: bool = True) -> dict:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    commands = [c for c in (home_info or {}).get("command_info_array") or ()
                if c.get("command_type") == 1]
    pv = st["preview"] if st["preview"].get("turn") == turn else {"bonus": {}, "members": {}}
    rows = []
    for c in commands:
        cmd = int(c["command_id"])
        here = list(pv["members"].get(str(cmd)) or [])
        rows.append({"command_type": 1, "command_id": cmd,
                     "params_inc_dec_info_array": list(pv["bonus"].get(str(cmd)) or []),
                     "team_member_info_array": [
                         {"chara_id": h, "gain_exp": 1 if can_gain(member(st, h)) else 0}
                         for h in here if member(st, h)],
                     "rank_up_predict": _rank_up_predict(st, here)})
    gainable = turn < FINAL_TURN and any(can_gain(m) for m in st["members"])
    goal_turn = is_goal_turn(chara_info, turn)
    gain_rows = []
    for ctype in GAIN_EXP_COMMANDS:
        on = gainable and (ctype in (3, 7) or (ctype == 4 and goal_turn))
        gain_rows.append({"command_type": ctype, "gain_exp": 1 if on else 0})
    lfo = []
    if st["members"] and casino_outing_open(chara_info):
        split = outing_split(st, rng_for(full_state, "outing", turn))
        lfo = [{"chara_id": c, "gain_exp": n} for c, n in split.items()]
    return {
        "command_info_array": rows if full else None,
        "team_member_info_array": [{"member_id": int(m["member_id"]), "chara_id": int(m["chara_id"]),
                                    "rank": int(m["rank"]), "exp": int(m["exp"])}
                                   for m in st["members"]],
        "team_sp_training_info": {"stock_num": int(st["stock"]), "stock_max": int(st["stock_max"]),
                                  "activated_state": 1 if dreams_active(st, turn) else 0},
        "not_up_parameter_info": {"not_up_exp_chara_id_array": [],
                                  "is_overflow_stock": bool(st["flags"].get("overflow"))},
        "bc_race_result_array": [dict(r) for r in st["bc_results"]],
        "team_union_progress": int(st["progress"]),
        "bc_race_track_id": int(st["track"] or M.TRACK_SANTA_ANITA),
        "team_rank": team_rank(st),
        "having_enhance_point": int(st["dp"]),
        "predict_enhance_point": predict(st, turn) if st["members"] else 0,
        "team_union_event": int(st["union_event"]),
        "command_gain_exp_array": gain_rows,
        "scenario_dress_setting": int(st["dress"]),
        "team_sp_level_limit": level_limit(st, turn),
        "link_friend_outing_member_info_array": lfo,
    }


def data_set_start(full_state: dict) -> dict:
    st = state(full_state)
    return {"last_select_bc_group_id": int(full_state.get(LAST_PICK_KEY) or 0),
            "deck_id": int(st["deck_id"]),
            "team_review_result_array": review_result_array(st),
            "enhance_group_array": enhance_groups(st)}


def debut_route_row(chara_id: int) -> int:
    from ... import master_data
    row = master_data.query_one(
        "SELECT rr.id FROM single_mode_route r JOIN single_mode_route_race rr "
        "ON rr.race_set_id=r.race_set_id WHERE r.scenario_id=0 AND r.chara_id=? "
        "AND rr.turn=12 AND rr.condition_type=1 ORDER BY r.priority DESC, rr.sort_id LIMIT 1",
        (int(chara_id),))
    return int(row["id"]) if row else 130001


def bc_route(flag: int) -> list:
    return [131000 + (int(flag) - 100) * 10 + sort for sort in range(2, 7)]


def roll_track(full_state: dict) -> int:
    st = state(full_state)
    if not st["track"]:
        st["track"] = (M.TRACK_DEL_MAR if rng_for(full_state, "track").random() < 0.4
                       else M.TRACK_SANTA_ANITA)
    return int(st["track"])
