
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 10
STATE_KEY = "legend"
SUMMER_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
GUIDANCE_TURN = 37
FINAL_TURN = 72
LEGEND_ORDER = M.LEGENDS
SIM_STATE: dict = {}

RANK_WEIGHTS = {
    (1, False): {1: 1}, (1, True): {1: 1},
    (2, False): {1: 64, 2: 36}, (2, True): {1: 25, 2: 75},
    (3, False): {1: 28, 2: 46, 3: 26}, (3, True): {1: 12, 2: 29, 3: 58},
}
HINTS_PER_LOTTERY = 2
GAUGE_TRAINING, GAUGE_FRIENDSHIP = 1, 3

SUPER_TURNS = 3
SUPER_EXTENSIONS = 2
SUPER_STEPS = 3
ZONE_STEPS = 4
FRIEND_GAUGE_MAX = 20
FRIEND_LEVEL_MAX = 9
FRIEND_GAIN = 7
GUEST_PRESENT_CHANCE = 0.8

RACE_GROUPS = (100001, 100002, 100003)
STELLA, PRIDE, LEGEND_RACE = RACE_GROUPS
RACE_FACTOR = {100001: (0.68, 0.73, 0.68, 0.68, 0.67),
               100002: (1.21, 1.21, 1.21, 1.14, 1.17),
               100003: (3.68, 3.03, 3.39, 2.68, 3.01)}
RACE_JITTER = 0.015
NAMED_COUNT = {100001: 11, 100002: 13, 100003: 14}
FIELD_SIZE = 16
LINK_CHARAS = (1115, 1086, 1116, 1070, 1046)
TURF_REGULARS = {1115: 1.0, 1116: 0.6, 1086: 0.6, 1070: 0.6}
DIRT_REGULARS = {1046: 1.0}
LEGEND_ROW = {M.ST_LITE: 9046901, M.SPEED: 9047901, M.HAISEIKO: 9048901}
LEGEND_WEIGHTS = {M.ST_LITE: 1, M.SPEED: 2, M.HAISEIKO: 4}
ALMOND_EYE = 1129
RACE_REWARD = {(100001, True): (30, 125, 25), (100001, False): (20, 75, 15),
               (100002, True): (45, 210, 35), (100002, False): (30, 125, 25),
               (100003, True): (55, 300, 0), (100003, False): (45, 210, 0)}
ERA_CHANGER = 210352
NEW_LEGEND = 210351
TIMELY = 202791
CATEGORIES = ("sprint", "mile", "classic", "long", "dirt")
CATEGORY_REWARD_SET = {"sprint": 910001, "mile": 910002, "classic": 910003,
                       "long": 910004, "dirt": 910005}
POPULARITY_REWARD = {1: ([{"type": "speed", "value": "+20"}], 20),
                     2: ([{"type": "all_stats", "value": "+5"}], 50),
                     3: ([{"type": "all_stats", "value": "+10"}], 100),
                     4: ([{"type": "all_stats", "value": "+25"}], 125)}
TRIPLE_CROWN = (163, 166, 168)


def _blank() -> dict:
    return {
        "gauges": {str(g): 0 for g in M.LEGENDS},
        "colours": {"turn": 0, "rows": {}},
        "buffs": [],
        "offers": [], "offer_turn": 0, "offer_bands": {},
        "chosen": {},
        "masterly": 0,
        "s9046": {"remain": 0, "step": 0, "extend": 0, "end_due": False, "start_due": False},
        "s9047": {"zone": 0, "step": 0, "start_due": False, "end": {}},
        "s9048": {"gauges": {}, "guests": {}},
        "races": [],
        "sprace": {}, "done_races": [], "hold_turn": 0,
        "due": [],
        "preview": {"turn": 0},
        "activated": [], "activated_turn": 0,
        "popularity": {}, "pop_done": [], "pop_due": [],
        "cm": {}, "route_id": 0,
        "snapshots": {},
        "advanced_to": 1,
        "goal_due": {},
        "race_legend": 0, "boss": False,
        "card003_due": 0, "card_seen": {},
        "ending_due": False,
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
    return random.Random(":".join(["legend", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def live_chara(full_state: dict) -> dict:
    smt = _smt()
    return ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("chara_info") or {}


def gauge(st: dict, legend: int) -> int:
    return int(st["gauges"].get(str(legend), 0))


def add_gauge(full_state: dict, legend: int, n: int, *, set_to: int | None = None) -> int:
    st = state(full_state)
    before = gauge(st, legend)
    after = min(M.GAUGE_MAX, max(0, set_to if set_to is not None else before + int(n)))
    st["gauges"][str(legend)] = after
    if after != before and st.get("offer_turn"):
        _reroll(full_state, st, legend, before, after)
    return after - before


def add_all_gauges(full_state: dict, n: int) -> None:
    for g in M.LEGENDS:
        add_gauge(full_state, g, n)


def row_key(ctype: int, cid: int) -> str:
    if ctype == 3:
        return "3:0"
    return f"{ctype}:{formula.SUMMER_TO_BASE.get(cid, cid) if ctype == 1 else cid}"


def roll_colours(full_state: dict, turn: int) -> dict:
    st = state(full_state)
    if int(st["colours"].get("turn") or 0) == int(turn) and st["colours"].get("rows"):
        return st["colours"]["rows"]
    rng = rng_for(full_state, "colour", turn)
    rows = {}
    for key in ("1:101", "1:105", "1:102", "1:103", "1:106", "3:0", "4:401", "7:701"):
        rows[key] = rng.choice(M.LEGENDS)
    st["colours"] = {"turn": int(turn), "rows": rows}
    return rows


def colour(full_state: dict, turn: int, ctype: int, cid: int) -> int:
    return int(roll_colours(full_state, turn).get(row_key(ctype, cid)) or M.HAISEIKO)


def held(st: dict) -> list:
    return [int(b["buff_id"]) for b in st["buffs"]]


def _buff_entry(st: dict, buff_id: int) -> dict | None:
    return next((b for b in st["buffs"] if int(b["buff_id"]) == int(buff_id)), None)


def guidance_live(st: dict, legend: int) -> bool:
    if legend == M.ST_LITE:
        return int(st["s9046"]["remain"]) > 0
    if legend == M.SPEED:
        return int(st["s9047"]["zone"]) > 0
    return int(st["masterly"]) == M.HAISEIKO


def super_on(st: dict) -> bool:
    return int(st["s9046"]["remain"]) > 0


def zone_on(st: dict) -> bool:
    return int(st["s9047"]["zone"]) > 0


def cool_time(entry: dict, turn: int) -> int:
    ct = int(M.buff(entry["buff_id"]).get("buff_ct") or 0)
    used = int(entry.get("consumed_turn") or 0)
    if not ct or not used:
        return 0
    return max(0, ct - max(0, int(turn) - used - 1))


def _condition_motivation(chara_info: dict, st: dict, group: int) -> bool | None:
    for ctype, v1, *_ in M.conditions(group):
        if ctype == M.COND_MOTIVATION:
            return super_on(st) or int(chara_info.get("motivation") or 3) >= v1
    return None


def is_active(chara_info: dict, st: dict, entry: dict) -> bool:
    row = M.buff(entry["buff_id"])
    if int(row.get("buff_trigger") or 0) == 1:
        return bool(entry.get("pending"))
    mot = _condition_motivation(chara_info, st, int(row.get("condition_group_id") or 0))
    return True if mot is None else mot


def active_effects(chara_info: dict, st: dict) -> list:
    out = []
    for b in st["buffs"]:
        if not is_active(chara_info, st, b):
            continue
        for et, v1, v2, lg, bv in M.buff_effects(b["buff_id"]):
            if lg and guidance_live(st, lg):
                v1 = bv
            out.append((et, v1, v2, int(b["buff_id"])))
    return out


def effect_total(chara_info: dict, st: dict, effect_type: int) -> int:
    return sum(v1 for et, v1, _v2, _b in active_effects(chara_info, st) if et == effect_type)


def _fires(row: dict, chara_info: dict, st: dict, ev: dict) -> bool:
    for ctype, v1, v2, v3, v4 in M.conditions(int(row.get("condition_group_id") or 0)):
        if ctype == M.COND_COMMAND:
            if int(ev["ctype"]) not in {v for v in (v1, v2, v3, v4) if v}:
                return False
            if int(ev["ctype"]) == 1 and not ev["success"]:
                return False
        elif ctype == M.COND_FRIENDSHIP:
            if not (ev["ctype"] == 1 and ev["success"] and ev["friendship"]):
                return False
        elif ctype == M.COND_SUPPORTERS:
            if not (ev["ctype"] == 1 and ev["success"] and ev["supporters"] >= v2):
                return False
        elif ctype == M.COND_MOTIVATION:
            if not (super_on(st) or int(chara_info.get("motivation") or 3) >= v1):
                return False
        else:
            return False
    return True


def after_command(full_state: dict, chara_info: dict, turn: int, ev: dict) -> list:
    st = state(full_state)
    fired: list = []
    if ev["ctype"] == 1:
        for b in st["buffs"]:
            if b.get("pending"):
                b["pending"] = 0
                b["consumed_turn"] = int(turn)
    for b in st["buffs"]:
        row = M.buff(b["buff_id"])
        if int(row.get("buff_trigger") or 0) != 1 or b.get("pending"):
            continue
        if cool_time(b, turn) > 0 or (int(b.get("consumed_turn") or 0) == int(turn)
                                      and int(row.get("buff_ct") or 0)):
            continue
        if not _fires(row, chara_info, st, ev):
            continue
        fired.append(int(b["buff_id"]))
        immediate = int(row.get("buff_turn_type") or 0) == 4
        for et, v1, v2, _lg, _bv in M.buff_effects(b["buff_id"]):
            if et == M.EFF_IMMEDIATE and v1 == 20:
                mood_up(full_state, chara_info, v2)
            elif et == M.EFF_BOND_NOW:
                _bond_partners(chara_info, ev.get("partners") or (), v1)
        if immediate:
            b["consumed_turn"] = int(turn)
        else:
            b["pending"] = 1
    st["activated"] = fired
    st["activated_turn"] = int(turn)
    return fired


def _bond_partners(chara_info: dict, partners, n: int) -> None:
    by_pos = formula.RF.deck(chara_info)
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") in partners and e.get("target_id") in by_pos:
            e["evaluation"] = min(100, int(e.get("evaluation") or 0) + int(n))


def buff_info_array(chara_info: dict, st: dict, turn: int) -> list:
    rows = sorted(st["buffs"], key=lambda b: int(b["buff_id"]))
    fresh = [b for b in rows if int(b.get("obtained_turn") or 0) == int(turn)
             and st.get("fresh_buff") == int(b["buff_id"])]
    rows = fresh + [b for b in rows if b not in fresh]
    return [{"buff_id": int(b["buff_id"]), "is_active": int(is_active(chara_info, st, b)),
             "cool_time": cool_time(b, turn), "obtained_turn": int(b.get("obtained_turn") or 0)}
            for b in rows]


def top_rank(st: dict) -> int:
    n = len(st["buffs"])
    return 3 if n >= M.RANK3_HELD else 2 if n >= M.RANK2_HELD else 1


def _draw(rng: random.Random, st: dict, legend: int, band3: bool, guaranteed: bool,
          taken: set) -> int:
    top = top_rank(st)
    pool = {r: [b for b, row in M.buffs().items()
                if int(row["legend_id"]) == legend and int(row["buff_rank"]) == r
                and b not in taken and b not in held(st)] for r in (1, 2, 3)}
    if guaranteed:
        rank = top
    else:
        weights = RANK_WEIGHTS[(top, band3)]
        ranks = [r for r in weights if pool.get(r)]
        if not ranks:
            ranks = [r for r in (1, 2, 3) if r <= top and pool.get(r)]
            if not ranks:
                return 0
            rank = ranks[-1]
        else:
            rank = rng.choices(ranks, weights=[weights[r] for r in ranks])[0]
    if not pool.get(rank):
        alt = [r for r in (top, 2, 1, 3) if r <= top and pool.get(r)]
        if not alt:
            return 0
        rank = alt[0]
    return rng.choice(sorted(pool[rank]))


def _legend_offers(full_state: dict, st: dict, legend: int, keep: list, tag) -> list:
    g = gauge(st, legend)
    num, highest = M.offers_for(legend, g)
    rng = rng_for(full_state, "offer", tag, legend, g)
    out = list(keep)
    taken = set(st["offers"]) | set(out)
    while len(out) < num:
        guaranteed = highest > 0 and not any(M.rank_of(b) == top_rank(st) for b in out)
        b = _draw(rng, st, legend, num >= 3, guaranteed, taken)
        if not b:
            break
        out.append(b)
        taken.add(b)
    return out


def roll_offers(full_state: dict, turn: int) -> list:
    st = state(full_state)
    st["offers"] = []
    by_legend = {}
    for legend in LEGEND_ORDER:
        by_legend[legend] = _legend_offers(full_state, st, legend, [], turn)
        st["offers"] += by_legend[legend]
    st["offer_turn"] = int(turn)
    st["offer_bands"] = {str(g): M.offers_for(g, gauge(st, g))[0] for g in M.LEGENDS}
    log.info("legend: lottery T%s gauges %s -> offers %s", turn, st["gauges"], st["offers"])
    return st["offers"]


def _reroll(full_state: dict, st: dict, legend: int, before: int, after: int) -> None:
    old = [b for b in st["offers"] if M.legend_of(b) == legend]
    others = [b for b in st["offers"] if M.legend_of(b) != legend]
    tag = f"{st['offer_turn']}:re{after}"
    if after >= M.GAUGE_MAX and before < M.GAUGE_MAX:
        st["offers"] = [b for b in others]
        new = _legend_offers(full_state, st, legend, [], tag)
    else:
        st["offers"] = [b for b in others]
        num, _h = M.offers_for(legend, after)
        new = _legend_offers(full_state, st, legend, old[:num], tag)
    merged = []
    for lg in LEGEND_ORDER:
        merged += new if lg == legend else [b for b in others if M.legend_of(b) == lg]
    st["offers"] = merged


def choose(full_state: dict, buff_id: int) -> None:
    st = state(full_state)
    if int(buff_id) not in st["offers"]:
        raise Refused(f"buff {buff_id} not offered")
    if len(st["buffs"]) >= M.BUFF_LIMIT:
        raise Refused("buff list full; exchange_buff")
    st["chosen"] = {"buff_id": int(buff_id)}


def exchange(full_state: dict, before: int, after: int) -> None:
    st = state(full_state)
    if int(after) not in st["offers"] or int(before) not in held(st):
        raise Refused(f"exchange {before} -> {after} not possible")
    st["chosen"] = {"buff_id": int(after), "before": int(before)}


def answer_lottery(full_state: dict, chara_info: dict, turn: int) -> int:
    st = state(full_state)
    chosen = st.get("chosen") or {}
    got = int(chosen.get("buff_id") or 0)
    if not got and st["offers"]:
        got = st["offers"][0] if len(st["buffs"]) < M.BUFF_LIMIT else 0
    if got:
        if chosen.get("before"):
            st["buffs"] = [b for b in st["buffs"] if int(b["buff_id"]) != int(chosen["before"])]
        if len(st["buffs"]) < M.BUFF_LIMIT and got not in held(st):
            st["buffs"].append({"buff_id": got, "obtained_turn": int(turn), "pending": 0,
                                "consumed_turn": 0})
            st["fresh_buff"] = got
    st["chosen"] = {}
    st["offers"] = []
    st["offer_turn"] = 0
    st["gauges"] = {str(g): 0 for g in M.LEGENDS}
    grant_support_hints(full_state, chara_info, turn)
    log.info("legend: lottery answered T%s -> %s (held %s)", turn, got, held(st))
    return got


def grant_support_hints(full_state: dict, chara_info: dict, turn: int) -> list:
    from ... import event_engine
    rng = rng_for(full_state, "hints", turn)
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or ()}
    pools = []
    for sid in formula.RF.deck(chara_info).values():
        if not formula.RF.is_normal_card(sid):
            continue
        skills = [g[1] for g in event_engine.card_hint_groups(sid) if g[0] == "skill"
                  and event_engine.card_hint_open(chara_info, g[1], learned)]
        if skills:
            pools.append(skills)
    effects = []
    for _ in range(HINTS_PER_LOTTERY):
        if not pools:
            break
        effects.append({"type": "skill_hint", "skill_id": rng.choice(rng.choice(pools)), "value": "+1"})
    if effects:
        event_engine.apply_choice(chara_info, {"choices": [{"effects": effects}]}, 1,
                                  full_state=full_state)
    return effects


class Refused(Exception):
    pass


def pick_guidance(full_state: dict) -> int:
    st = state(full_state)
    count = {g: sum(1 for b in held(st) if M.legend_of(b) == g) for g in M.LEGENDS}
    best = max(count.values())
    tied = [g for g in M.LEGENDS if count[g] == best]
    return tied[0] if len(tied) == 1 else rng_for(full_state, "guidance").choice(tied)


def grant_guidance(full_state: dict, chara_info: dict, legend: int) -> None:
    st = state(full_state)
    st["masterly"] = int(legend)
    if legend == M.ST_LITE:
        st["s9046"].update(step=SUPER_STEPS)
    elif legend == M.SPEED:
        st["s9047"].update(step=ZONE_STEPS)


def start_super(full_state: dict, chara_info: dict) -> None:
    st = state(full_state)
    st["s9046"].update(remain=SUPER_TURNS, step=0, extend=SUPER_EXTENSIONS, start_due=False)
    chara_info["motivation"] = min(5, int(chara_info.get("motivation") or 3) + 1)


def end_super(full_state: dict) -> None:
    st = state(full_state)
    st["s9046"].update(remain=0, extend=0, end_due=False)


def count_mood_up(full_state: dict, n: int) -> int:
    st = state(full_state)
    if st["masterly"] != M.ST_LITE or n <= 0:
        return n
    s = st["s9046"]
    if super_on(st):
        for _ in range(n):
            if int(s["extend"]) > 0:
                s["extend"] = int(s["extend"]) - 1
                s["remain"] = int(s["remain"]) + 1
        return 0
    s["step"] = min(SUPER_STEPS, int(s["step"]) + n)
    if int(s["step"]) >= SUPER_STEPS:
        s["start_due"] = True
    return n


def mood_up(full_state: dict, chara_info: dict, n: int) -> int:
    amount = count_mood_up(full_state, n)
    if amount > 0:
        chara_info["motivation"] = min(5, int(chara_info.get("motivation") or 3) + amount)
    return amount


def start_zone(full_state: dict) -> None:
    st = state(full_state)
    st["s9047"].update(zone=1, step=0, start_due=False)


def end_zone(full_state: dict, end_type: int) -> None:
    st = state(full_state)
    z = st["s9047"]
    n = int(z["zone"])
    if not n:
        return
    z["end"] = {"n": n, "end_type": int(end_type)}
    z["zone"] = 0


def zone_rates(full_state: dict, chara_info: dict, home_commands) -> list:
    st = state(full_state)
    n = int(st["s9047"]["zone"])
    out = []
    for c in home_commands or ():
        if int(c.get("command_type") or 0) != 1:
            continue
        cid = int(c["command_id"])
        out.append({"command_type": 1, "command_id": cid,
                    "zone_end_rate": formula.zone_end_rate(
                        chara_info, cid, int(c.get("level") or 1),
                        c.get("training_partner_array") or (), n) if n else 0})
    return out


def recruit_guests(full_state: dict, chara_info: dict) -> dict:
    from ... import master_data
    st = state(full_state)
    s = st["s9048"]
    if s["gauges"]:
        return s
    deck = formula.RF.deck(chara_info)
    for pos, sid in sorted(deck.items()):
        if formula.RF.card_meta(sid)[0] == formula.GROUP_TYPE:
            continue
        s["gauges"][str(pos)] = [1, FRIEND_GAUGE_MAX]
    taken = {formula.RF.card_meta(sid)[3] for sid in deck.values()}
    taken.add(int(chara_info.get("card_id") or 0) // 100)
    rng = rng_for(full_state, "guests")
    pool = sorted(c for c in master_data.drawable_charas() if 1000 <= int(c) < 2000 and c not in taken)
    rng.shuffle(pool)
    for i, cmd in enumerate(formula.TRAINING_COMMANDS):
        if i < len(pool):
            s["guests"][str(cmd)] = pool[i]
            s["gauges"][str(pool[i])] = [1, FRIEND_GAUGE_MAX]
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for chara in s["guests"].values():
        if chara not in have:
            arr.append({"target_id": chara, "training_partner_id": chara, "evaluation": 0,
                        "is_outing": 0, "story_step": 0, "is_appear": 1,
                        "group_outing_info_array": []})
    log.info("legend: Haiseiko guests %s", s["guests"])
    return s


def friend(st: dict, pid: int):
    v = st["s9048"]["gauges"].get(str(int(pid)))
    return (int(v[0]), int(v[1])) if v else None


def is_full(st: dict, pid: int) -> bool:
    f = friend(st, pid)
    return bool(f) and f[1] >= FRIEND_GAUGE_MAX


def guest_facility(st: dict, chara: int) -> int:
    return next((int(c) for c, g in st["s9048"]["guests"].items() if int(g) == int(chara)), 0)


def friend_gain(chara_info: dict, st: dict) -> int:
    from ...handlers import conditions
    gain = FRIEND_GAIN + effect_total(chara_info, st, M.EFF_BOND_UP)
    return conditions.bond_gain(chara_info, gain)


def forced_cards(chara_info: dict, st: dict) -> dict:
    out = {}
    deck = formula.RF.deck(chara_info)
    for pid in st["s9048"]["gauges"]:
        pos = int(pid)
        if pos < 100 and pos in deck and is_full(st, pos):
            kind, cmd, _r, _c = formula.RF.card_meta(deck[pos])
            if kind == 1 and cmd in formula.TRAINING_COMMANDS:
                out[pos] = cmd
    return out


def train_friends(full_state: dict, chara_info: dict, partners, friendship: bool) -> None:
    st = state(full_state)
    gain = friend_gain(chara_info, st)
    for pid in partners or ():
        f = friend(st, pid)
        if not f:
            continue
        lv, g = f
        if g >= FRIEND_GAUGE_MAX and friendship and lv < FRIEND_LEVEL_MAX:
            st["s9048"]["gauges"][str(int(pid))] = [lv + 1, 0]
        elif g < FRIEND_GAUGE_MAX:
            st["s9048"]["gauges"][str(int(pid))] = [lv, min(FRIEND_GAUGE_MAX, g + gain)]


def friend_gauge_array(st: dict) -> list:
    rows = []
    for pid, (lv, g) in sorted(((int(k), v) for k, v in st["s9048"]["gauges"].items())):
        rows.append({"training_partner_id": pid, "level": int(lv), "gauge_value": int(g)})
    return rows


def full_guests_in(st: dict, partners) -> int:
    return sum(1 for p in partners or () if int(p) >= formula.NPC_MIN and is_full(st, p))


def mods_for(full_state: dict, chara_info: dict, partners) -> dict:
    st = state(full_state)
    out = {"tu": 0.0, "tu_per_sup": 0.0, "mu": 0.0, "mu_per_sup": 0.0, "fb": 0.0,
           "fb_per_friend": 0.0, "nfriend_extra": 0, "super": super_on(st)}
    for et, v1, v2, _b in active_effects(chara_info, st):
        if et == M.EFF_TRAINING:
            out["tu"] += v1
            out["tu_per_sup"] += v2
        elif et == M.EFF_MOOD:
            out["mu"] += v1
            out["mu_per_sup"] += v2
        elif et == M.EFF_FRIENDSHIP:
            out["fb"] += v1
            out["fb_per_friend"] += v2
    if zone_on(st):
        out["tu"] += formula.ZONE_TRAINING
    for pid in partners or ():
        f = friend(st, pid)
        if not f:
            continue
        lv, g = f
        if int(pid) < formula.NPC_MIN:
            out["tu"] += M.card_level_bonus(lv)
        else:
            t1, t2 = M.guest_level_bonus(lv)
            out["tu"] += t1
            if g >= FRIEND_GAUGE_MAX:
                out["nfriend_extra"] += 1
                out["fb"] += t2
    return out


def energy_down(full_state: dict, chara_info: dict) -> int:
    st = state(full_state)
    return (formula.ZONE_ENERGY_DOWN if zone_on(st) else 0) + effect_total(chara_info, st, M.EFF_HP_DOWN)


def refresh_preview(full_state: dict, chara_info: dict, home_info: dict, card_levels=None) -> bool:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    commands = list((home_info or {}).get("command_info_array") or ())
    before = copy.deepcopy(st["preview"])
    roll_colours(full_state, turn)
    bonus, partners, gain_gauge, fgain, friendly = {}, {}, {}, {}, {}
    fg = friend_gain(chara_info, st)
    for c in commands:
        if int(c.get("command_type") or 0) != 1:
            continue
        cmd = int(c["command_id"])
        ps = list(c.get("training_partner_array") or ())
        partners[str(cmd)] = ps
        bonus[str(cmd)] = formula.bonus_params(
            chara_info, cmd, int(c.get("level") or 1), ps, card_levels,
            mods=mods_for(full_state, chara_info, ps))
        rainbow = formula.is_friendship_training(chara_info, cmd, ps, full_guests_in(st, ps))
        friendly[str(cmd)] = rainbow
        legend = colour(full_state, turn, 1, cmd)
        gain_gauge[str(cmd)] = max(0, min(GAUGE_FRIENDSHIP if rainbow else GAUGE_TRAINING,
                                          M.GAUGE_MAX - gauge(st, legend)))
        if st["masterly"] == M.HAISEIKO:
            fgain[str(cmd)] = [{"training_partner_id": int(p),
                                "gain_value": max(0, min(fg, FRIEND_GAUGE_MAX - friend(st, p)[1]))}
                               for p in ps if friend(st, p)]
    st["preview"] = {"turn": turn, "bonus": bonus, "partners": partners, "gain_gauge": gain_gauge,
                     "fgain": fgain, "friendly": friendly,
                     "zone_rates": zone_rates(full_state, chara_info, commands)}
    return st["preview"] != before


def preview_of(st: dict, turn: int, key: str, cmd: int, default=None):
    pv = st["preview"]
    if int(pv.get("turn") or 0) != int(turn):
        return default
    return (pv.get(key) or {}).get(str(cmd), default)


def preview_bonus(st: dict, turn: int, command_id: int) -> list:
    return [dict(p) for p in preview_of(st, turn, "bonus", command_id, []) or ()]


def zone_rate_for(st: dict, command_id: int) -> int:
    for r in st["preview"].get("zone_rates") or ():
        if int(r["command_id"]) == int(command_id):
            return int(r["zone_end_rate"])
    return 0


def raise_all_facilities(full_state: dict, chara_info: dict) -> None:
    RI.raise_all_facilities(full_state, chara_info)
    RI.refresh_home(full_state, chara_info)


def sprace_group(turn: int) -> int:
    return M.sprace_turns().get(int(turn or 0), 0)


def sprace_pending(st: dict, turn: int) -> bool:
    return bool(sprace_group(turn)) and int(turn) not in [int(t) for t in st["done_races"]]


def category(ground: int, distance: int) -> str:
    if ground == 2:
        return "dirt"
    return ("sprint" if distance <= 1400 else "mile" if distance <= 1800
            else "classic" if distance <= 2400 else "long")


def race_categories(race_history) -> dict:
    tally: dict = {}
    for h in race_history or ():
        pid = int(h.get("program_id") or 0)
        if M.is_sprace_program(pid):
            continue
        course = M.program_course(pid)
        if course:
            k = category(*course)
            tally[k] = tally.get(k, 0) + 1
    return tally


def best_category(race_history, available) -> str:
    tally = race_categories(race_history)
    order = sorted(CATEGORIES, key=lambda k: (-tally.get(k, 0), CATEGORIES.index(k)))
    for k in order:
        if k in available:
            return k
    return "classic"


def pick_program(race_group: int, race_history, rng: random.Random) -> int:
    programs = M.group_programs(race_group)
    by_cat: dict = {}
    for p, g, d in programs:
        by_cat.setdefault(category(g, d), []).append(p)
    cat = best_category(race_history, set(by_cat))
    pool = by_cat.get(cat) or [p for p, _g, _d in programs]
    return rng.choice(pool) if pool else 0


def g1_wins(race_history) -> int:
    from ... import master_data
    n = 0
    for h in race_history or ():
        if int(h.get("result_rank") or 0) != 1:
            continue
        row = master_data.query_one("SELECT grade FROM single_mode_program p JOIN race_instance ri "
                                    "ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
                                    "WHERE p.id=?", (int(h.get("program_id") or 0),))
        if row and int(row["grade"]) == 100 and not M.is_sprace_program(h.get("program_id")):
            n += 1
    return n


def boss_due(full_state: dict, chara_info: dict, program_id: int, race_history) -> bool:
    st = state(full_state)
    if int(chara_info.get("card_id") or 0) // 100 == ALMOND_EYE:
        return False
    course = M.program_course(program_id)
    if not course or category(*course) not in ("mile", "classic"):
        return False
    won = {int(r["race_type"]) for r in st["races"] if int(r["result_rank"]) == 1}
    if not {1, 2} <= won:
        return False
    wins = {int(h.get("program_id") or 0) for h in race_history or () if int(h.get("result_rank") or 0) == 1}
    tiara = (164, 167, 169)
    return set(TRIPLE_CROWN) <= wins or set(tiara) <= wins or g1_wins(race_history) >= 9


def race_legend(full_state: dict) -> int:
    st = state(full_state)
    if not st.get("race_legend"):
        rng = rng_for(full_state, "race_legend")
        legends = list(LEGEND_WEIGHTS)
        st["race_legend"] = rng.choices(legends, weights=[LEGEND_WEIGHTS[g] for g in legends])[0]
    return int(st["race_legend"])


def announce(full_state: dict, chara_info: dict, turn: int) -> dict:
    st = state(full_state)
    sp = st["sprace"]
    if int(sp.get("turn") or 0) == int(turn) and sp.get("program_id"):
        return sp
    group = sprace_group(turn)
    smt = _smt()
    history = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("race_history") or []
    program = pick_program(group, history, rng_for(full_state, "sprace", turn))
    cond = smt._career_race_conditions(program, chara_info)
    boss = group == LEGEND_RACE and boss_due(full_state, chara_info, program, history)
    st["boss"] = bool(boss)
    st["sprace"] = {"turn": int(turn), "group": group, "program_id": program,
                    "weather": int(cond["weather"]), "ground_condition": int(cond["ground_condition"]),
                    "stage": "announced", "boss": bool(boss)}
    log.info("legend: Dream Fest %s on turn %s -> program %s%s", group, turn, program,
             " (Almond Eye)" if boss else "")
    return st["sprace"]


def _npc_row(row_id: int) -> dict | None:
    from ... import master_data
    r = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?", (int(row_id),))
    return dict(r) if r else None


def _opponent(row: dict, factors: tuple, rng: random.Random) -> dict:
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
    for (stat, col), f in zip((("speed", "speed"), ("stamina", "stamina"), ("power", "pow"),
                               ("guts", "guts"), ("wiz", "wiz")), factors):
        out[stat] = max(1, round(int(row[col]) * f * rng.uniform(1 - RACE_JITTER, 1 + RACE_JITTER)))
    for k in ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
              "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
              "proper_running_style_sashi", "proper_running_style_oikomi",
              "proper_ground_turf", "proper_ground_dirt"):
        out[k] = int(row.get(k) or 1)
    lo, hi = int(row.get("motivation_min") or 4), int(row.get("motivation_max") or 5)
    out["motivation"] = rng.randint(min(lo, hi), max(lo, hi))
    return out


def race_field(full_state: dict, chara_info: dict, program_id: int, count: int) -> list | None:
    from ... import master_data
    group = next((g for g in RACE_GROUPS
                  if int(program_id) in {p for p, _g, _d in M.group_programs(g)}), 0)
    if not group or count <= 0:
        return None
    smt = _smt()
    seed = f"legend-field:{chara_info.get('single_mode_chara_id')}:{chara_info.get('card_id')}:{program_id}"
    rng = random.Random(seed)
    factors = RACE_FACTOR[group]
    trainee = int(chara_info.get("card_id") or 0) // 100
    out: list = []
    st = state(full_state) if full_state else {}
    if group == LEGEND_RACE:
        boss = bool((st.get("sprace") or {}).get("boss")) if st else False
        if boss:
            course = M.program_course(program_id) or (1, 2000)
            row = _npc_row(M.ALMOND_EYE_ROWS[0 if category(*course) == "mile" else 1])
        else:
            legend = race_legend(full_state) if full_state else M.HAISEIKO
            row = _npc_row(LEGEND_ROW[legend])
        if row:
            out.append(_opponent(row, factors, rng))
    course = M.program_course(program_id) or (1, 2000)
    regulars = DIRT_REGULARS if course[0] == 2 else TURF_REGULARS
    named: list = []
    for chara, p in regulars.items():
        if chara != trainee and rng.random() < p:
            named.append(chara)
    pool = sorted(int(r["chara_id"]) for r in master_data.query(
        "SELECT chara_id FROM single_mode_npc WHERE npc_group_id=0 AND id = chara_id * 10 "
        "AND chara_id BETWEEN 1000 AND 1999"))
    pool = [c for c in pool if c != trainee and c not in named and c != ALMOND_EYE]
    rng.shuffle(pool)
    want = min(count - len(out), NAMED_COUNT[group])
    named += pool[:max(0, want - len(named))]
    for chara in named[:want]:
        row = _npc_row(chara * 10)
        if row:
            out.append(_opponent(row, factors, rng))
    rest = count - len(out)
    if rest > 0:
        out += smt._mob_opponents(chara_info, rest)
    return out[:count]


def reward_effects(full_state: dict, chara_info: dict, group: int, won: bool) -> list:
    smt = _smt()
    stats, sp, energy = RACE_REWARD[(group, won)]
    eff = [{"type": "all_stats", "value": f"+{smt._scale_race_reward(stats, chara_info, full_state)}"},
           {"type": "skill_points", "value": f"+{smt._scale_race_reward(sp, chara_info, full_state)}"}]
    if energy:
        eff.append({"type": "energy", "value": f"+{energy}"})
    if won and group == PRIDE:
        eff.append({"type": "skill_hint", "skill_id": ERA_CHANGER, "value": "+1"})
    if won and group == LEGEND_RACE:
        eff.append({"type": "skill_hint", "skill_id": TIMELY, "value": "+1"})
    return eff


def all_won(st: dict) -> bool:
    return len(st["races"]) == 3 and all(int(r["result_rank"]) == 1 for r in st["races"])


def stella_won(st: dict) -> bool:
    return any(int(r["race_type"]) == 1 and int(r["result_rank"]) == 1 for r in st["races"])


def snapshot_race(full_state: dict, race_ctx: dict, chara_info: dict, rank: int) -> None:
    if int(rank or 0) != 1:
        return
    rsi = race_ctx.get("race_start_info") or {}
    if not rsi or not race_ctx.get("race_scenario"):
        return
    from ... import master_data
    pid = int(race_ctx.get("program_id") or 0)
    row = master_data.query_one("SELECT r.grade AS grade FROM single_mode_program p JOIN race_instance ri "
                                "ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
                                "WHERE p.id=?", (pid,))
    if not row or int(row["grade"]) != 100:
        return
    turn = int(race_ctx.get("turn") or 0)
    year = "classic" if 25 <= turn <= 48 else "senior" if turn >= 49 else "junior"
    snap = race_result_info(rsi, race_ctx.get("race_scenario"), int(race_ctx.get("race_instance_id") or
                                                                     M.program_instance(pid)))
    snap["_program_id"] = pid
    snap["_turn"] = turn
    state(full_state)["snapshots"][year] = snap


_HORSE_DEFAULTS = {"fan_count": 0, "final_grade": 0, "frame_order_change_flag": 0,
                   "item_id_array": [], "motivation_change_flag": 0, "npc_type": 0,
                   "owner_trainer_name": "", "popularity_mark_rank_array": [1, 1, 1],
                   "race_result_array": [], "scenario_data_array": [], "single_mode_win_count": 0,
                   "team_id": 0, "team_member_id": 0, "team_rank": 0, "win_saddle_id_array": [],
                   "chara_color_type": 0}


def race_result_info(rsi: dict, scenario, instance: int) -> dict:
    horses = []
    for h in rsi.get("race_horse_data") or ():
        row = dict(_HORSE_DEFAULTS)
        row.update(copy.deepcopy(h))
        horses.append(row)
    return {"ground_condition": int(rsi.get("ground_condition") or 1),
            "race_horse_data_array": horses, "race_instance_id": int(instance or 0),
            "race_scenario": scenario or "", "random_seed": int(rsi.get("random_seed") or 0),
            "season": int(rsi.get("season") or 1), "weather": int(rsi.get("weather") or 1)}


def _public(snap) -> dict | list:
    if not snap:
        return []
    return {k: v for k, v in snap.items() if not k.startswith("_")}


def start_popularity(full_state: dict, ptype: int) -> dict:
    st = state(full_state)
    info = {"popularity_type": int(ptype), "poster_catchphrase_id": 0, "poster_race_result_info": [],
            "advertisement_legend_id": 0}
    rng = rng_for(full_state, "catch", ptype)
    if ptype >= 2:
        info["poster_catchphrase_id"] = st.get("catchphrase") or rng.randint(1, 18)
        st["catchphrase"] = info["poster_catchphrase_id"]
    if ptype == 2:
        info["poster_race_result_info"] = _public(st["snapshots"].get("classic")
                                                  or st["snapshots"].get("junior"))
    if ptype == 4:
        info["advertisement_legend_id"] = race_legend(full_state)
    st["popularity"] = info
    return info


def cm_route(race_history) -> int:
    wins = {int(h.get("program_id") or 0) for h in race_history or () if int(h.get("result_rank") or 0) == 1}
    if wins & set(TRIPLE_CROWN):
        return 1
    if wins & {164, 167, 169}:
        return 2
    tally = race_categories(race_history)
    if tally.get("dirt", 0) and tally["dirt"] >= max(tally.values()):
        return 3
    if tally.get("sprint", 0) + tally.get("mile", 0) > tally.get("classic", 0) + tally.get("long", 0):
        return 4
    return 5


def pick_cm(full_state: dict, race_history) -> dict:
    st = state(full_state)
    route = int(st.get("route_id") or 5)
    rows = [r for r in M.cm_details() if int(r["route_id"]) == route]
    wins = {int(h.get("program_id") or 0) for h in race_history or () if int(h.get("result_rank") or 0) == 1}
    best = None
    for r in rows:
        ct, cv = int(r["condition_type"]), int(r["condition_value"])
        ok = (ct == 2 and cv in wins) or (ct == 1 and cv in wins)
        if ok and (best is None or int(r["priority"]) > int(best["priority"])):
            best = r
    if best is None:
        best = next((r for r in rows if int(r["condition_type"]) == 4), rows[0] if rows else None)
    snap = (st["sprace"].get("snapshot") if isinstance(st.get("sprace"), dict) else None) \
        or st["snapshots"].get("senior") or st["snapshots"].get("classic")
    st["cm"] = {"cm_detail_id": int(best["id"]) if best else 0, "race_result_info": _public(snap)}
    return st["cm"]


def evaluation_array(chara_info: dict) -> list:
    out = []
    deck = formula.RF.deck(chara_info)
    for pos in sorted(deck):
        out.append({"target_id": pos, "chara_id": formula.RF.card_meta(deck[pos])[3]})
    for e in sorted((e for e in chara_info.get("evaluation_info_array") or ()
                     if isinstance(e, dict) and int(e.get("target_id") or 0) > 100),
                    key=lambda e: int(e["target_id"])):
        t = int(e["target_id"])
        chara = t if t >= formula.NPC_MIN else _npc_chara(t)
        if chara:
            out.append({"target_id": t, "chara_id": chara})
    return out


def _npc_chara(target: int) -> int:
    from ... import master_data
    row = master_data.query_one("SELECT chara_id FROM single_mode_unique_chara WHERE scenario_id=? "
                                "AND partner_id=?", (SCENARIO_ID, int(target)))
    return int(row["chara_id"]) if row else 0


_FULL_ENDPOINTS = frozenset({"start", "load"})


def reply_is_full(endpoint: str, data: dict) -> bool:
    call = (endpoint or "").rsplit("/", 1)[-1]
    ci = data.get("chara_info") or {}
    if RI.career_over(ci) and call != "load":
        return False
    if call in _FULL_ENDPOINTS:
        return True
    return call in ("check_event", "exec_command", "race_out", "gain_skills", "change_short_cut",
                    "legend_race_out", "popularity_end", "cm_end")


def command_info_array(full_state: dict, chara_info: dict, home_info: dict, turn: int) -> list:
    st = state(full_state)
    rows = []
    for c in (home_info or {}).get("command_info_array") or ():
        ctype, cid = int(c.get("command_type") or 0), int(c.get("command_id") or 0)
        if ctype != 1 and cid in (401, 701) and not int(c.get("is_enable", 1) or 0):
            continue
        if ctype == 1:
            fg = preview_of(st, turn, "fgain", cid) if st["masterly"] == M.HAISEIKO else None
            rows.append({"command_type": 1, "command_id": cid,
                         "legend_id": colour(full_state, turn, 1, cid),
                         "gain_gauge": int(preview_of(st, turn, "gain_gauge", cid, 0) or 0),
                         "params_inc_dec_info_array": preview_bonus(st, turn, cid),
                         "friend_gauge_gain_array": fg})
        elif ctype == 3 or cid in (301, 390, 304):
            rows.append({"command_type": 3, "command_id": 0, "legend_id": colour(full_state, turn, 3, 0),
                         "gain_gauge": 0, "params_inc_dec_info_array": [], "friend_gauge_gain_array": []})
        elif cid == 401 or ctype == 4:
            rows.append({"command_type": 4, "command_id": 401, "legend_id": colour(full_state, turn, 4, 401),
                         "gain_gauge": 0, "params_inc_dec_info_array": [], "friend_gauge_gain_array": []})
        elif cid == 701:
            rows.append({"command_type": 7, "command_id": 701, "legend_id": colour(full_state, turn, 7, 701),
                         "gain_gauge": 0, "params_inc_dec_info_array": [], "friend_gauge_gain_array": []})
    return rows


def masterly_info(st: dict) -> dict:
    m = int(st["masterly"])
    s46, s47 = st["s9046"], st["s9047"]
    return {
        "info_9046": ({"remain_count": int(s46["remain"]), "current_step_count": int(s46["step"]),
                       "can_extend_count": int(s46["extend"])} if m == M.ST_LITE else None),
        "info_9047": ({"continuation_zone_count": int(s47["zone"]), "current_step_count": int(s47["step"]),
                       "is_zone_end": bool(s47.get("end")),
                       "zone_end_rate_array": list(st["preview"].get("zone_rates") or [])}
                      if m == M.SPEED else None),
        "info_9048": ({"friend_gauge_array": friend_gauge_array(st), "all_friend_gauge_gain_info": 0}
                      if m == M.HAISEIKO else None),
    }


def data_set(full_state: dict, chara_info: dict, home_info: dict, full: bool, *,
             appear_legend: bool = False) -> dict:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    activated = list(st["activated"]) if int(st.get("activated_turn") or 0) == turn - 1 or \
        int(st.get("activated_turn") or 0) == turn else []
    return {
        "command_info_array": command_info_array(full_state, chara_info, home_info, turn),
        "evaluation_info_array": evaluation_array(chara_info),
        "gauge_count_array": [{"legend_id": g, "count": gauge(st, g)} for g in M.LEGENDS],
        "buff_info_array": buff_info_array(chara_info, st, turn),
        "obtainable_buff_id_array": list(st["offers"]) if st.get("offer_turn") else [],
        "masterly_bonus_info": masterly_info(st),
        "race_history_array": [dict(r) for r in st["races"]],
        "not_up_parameter_info": {"legend_id_array": [], "friend_partner_id_array": [],
                                  "friend_partner_id_at_random_array": [],
                                  "friend_lv_partner_id_array": [], "not_up_9046_remain_count": False},
        "not_up_buff_parameter_info": {"status_type_array": [], "chara_effect_id_array": []},
        "not_down_parameter_info": {"friend_partner_id_array": [], "param_guard_array": [],
                                    "bad_condition_guard_array": []},
        "activated_buff_id_array": activated,
        "route_id": int(st.get("route_id") or 0),
        "cm_info": {"cm_detail_id": int((st["cm"] or {}).get("cm_detail_id") or 0),
                    "race_result_info": (st["cm"] or {}).get("race_result_info") or []
                    if int(live_chara(full_state).get("playing_state") or 0) == 32 else []},
        "popularity_info": dict(st["popularity"]) if st.get("popularity") else [],
        "is_appear_legend": bool(appear_legend),
    }


def group_card(chara_info: dict):
    for pos, sid in formula.RF.deck(chara_info).items():
        if sid == M.GROUP_CARD:
            return pos, sid
    return None
