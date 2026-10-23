
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import formula as RF
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = M.SCENARIO_ID
STATE_KEY = "arc"
MIRROR_KEY = "_arc"

GAUGE_MAX = 3
LINEUP_MAX = 5
ROSTER_TURN = 3
SP_TAG_BOOST_TYPE = 3
TRAINEE_SLOT_BASE = 2007900

GROWTH = (3, 8)
CO_TRAIN = (18, 27)
SHARE_SPREAD, SHARE_MAX = 7, 28
SSS_ODDS = {0: 0.03, 5: 0.42, 6: 0.42, 7: 0.42}
SSS_SURE = 8
MARK_CUTS = ((19, 1), (7, 2), (1, 3), (0, 4))
LOSS_ODDS = {1: 0.0, 2: 0.10, 3: 0.20, 4: 1.0}
EARLY_SKILL_KEEP = 0.7
FULL_SKILLS_TURN = 41
STAT_JITTER = 0.05
RIVAL_RACE_MEMBERS = (1, 3)
MIN_DISTANCE_APT = 5

THRESHOLD_EVENTS = ((200, 206126, 2, 2000, True), (400, 206127, 3, 2000, False),
                    (600, 206128, 4, 4000, True), (800, 206129, 5, 6000, False),
                    (1000, 206130, 5, 8000, True))


def _blank() -> dict:
    return {
        "started": False, "roster": False, "members": [], "trainee": 0,
        "tpoints": 0, "gexp": 0, "pot": {}, "ss_wins": 0, "sss_wins": 0, "since_sss": 0,
        "race_cond": {}, "trust_seen": False, "expedition_started": False,
        "lineup": {}, "races": [], "thresholds": [], "with": 0, "with_options": [],
        "classic_won": False, "senior_won": None, "unique_ups": 0,
        "preview": {"turn": 0, "rows": {}}, "rival_races": {"turn": 0, "rows": []},
        "sprace": {}, "hold_turn": 0, "upkeep_turn": 0, "co_train": {},
        "due": [], "flags": {}, "done_exhibitions": [],
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


def flag(st: dict, name: str, value=None):
    if value is not None:
        st["flags"][name] = value
        return value
    return st["flags"].pop(name, None)


def rng_for(full_state: dict, *parts) -> random.Random:
    return random.Random(":".join(["arc", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def live_chara(full_state: dict) -> dict:
    from ...handlers import single_mode_team as smt
    return ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("chara_info") or {}


def mirror(full_state: dict, chara_info: dict | None = None) -> None:
    st = state(full_state)
    for ci in (chara_info, live_chara(full_state)):
        if isinstance(ci, dict):
            ci[MIRROR_KEY] = {"pt": int(st["tpoints"]), "pot": {str(k): int(v) for k, v in st["pot"].items()}}


def mirrored_levels(chara_info: dict) -> dict:
    return {int(k): int(v) for k, v in ((chara_info or {}).get(MIRROR_KEY) or {}).get("pot", {}).items()}


def strip_mirror(chara_info) -> None:
    if isinstance(chara_info, dict):
        chara_info.pop(MIRROR_KEY, None)


def member(st: dict, chara_id: int) -> dict | None:
    for m in st["members"]:
        if int(m["chara"]) == int(chara_id):
            return m
    return None


def is_expedition(turn: int) -> bool:
    return int(turn) in M.EXPED_TURNS


def deck_charas(chara_info: dict) -> dict:
    out = {}
    for pos, sid in RF.deck(chara_info).items():
        kind, _cmd, _r, chara = RF.card_meta(sid)
        if kind == 1 and chara and chara not in out:
            out[chara] = (pos, sid)
    return out


def deck_all_charas(chara_info: dict) -> set:
    return {RF.card_meta(sid)[3] for sid in RF.deck(chara_info).values()}


def _initial_points(rng: random.Random) -> list:
    pts = [lo + 4 * rng.randint(0, (hi - lo) // 4) for lo, hi in M.INITIAL_POINTS]
    diff = M.INITIAL_POINTS_TOTAL - sum(pts)
    order = list(range(len(pts)))
    while diff:
        rng.shuffle(order)
        moved = False
        for i in order:
            lo, hi = M.INITIAL_POINTS[i]
            step = 4 if diff > 0 else -4
            if lo <= pts[i] + step <= hi:
                pts[i] += step
                diff -= step
                moved = True
                if not diff:
                    break
        if not moved:
            pts[0] += diff
            break
    return pts


def _peff_block(rng: random.Random, x_groups) -> list:
    block = [1, 11, rng.choice(x_groups) if isinstance(x_groups, tuple) else x_groups]
    rng.shuffle(block)
    return block


def _x_choice(chara: int, rng: random.Random):
    if chara in M.LINK_X:
        return tuple(M.LINK_X[chara])
    groups = list(M.OTHER_X_WEIGHTS)
    return rng.choices(groups, weights=[M.OTHER_X_WEIGHTS[g] for g in groups])[0]


def _extend_peff(m: dict, rng: random.Random) -> None:
    while len(m["peff"]) < 3:
        for g in _peff_block(rng, m["x"]):
            m["peff_num"] = int(m.get("peff_num") or 0) + 1
            value = (15 if m["deck_link"] else 10) if g == 11 else 0
            m["peff"].append([m["peff_num"], g, value])


def create_roster(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    if st["roster"]:
        return st["members"]
    rng = rng_for(full_state, "roster")
    trainee = turn_trainee = int(chara_info.get("card_id") or 0) // 100
    st["trainee"] = turn_trainee
    deck = deck_charas(chara_info)
    deck_rows = []
    for chara, (pos, sid) in sorted(deck.items(), key=lambda kv: kv[1][0]):
        row = M.scout_for_card(sid) or M.scout_r_card(chara)
        if row and chara != trainee:
            deck_rows.append((chara, row, pos, sid))
    links = [c for c in M.link_charas() if c != trainee and c not in deck]
    rng.shuffle(links)
    room = max(0, M.MEMBERS - len(deck_rows))
    links = links[:room]
    link_rows = [(c, M.scout_r_card(c), 0, 0) for c in links if M.scout_r_card(c)]
    taken = {trainee, *deck, *links}
    fill = []
    pool = [c for c in M.fill_pool() if c not in taken and c not in M.link_charas()]
    rng.shuffle(pool)
    for c in pool:
        if len(deck_rows) + len(link_rows) + len(fill) >= M.MEMBERS:
            break
        fill.append((c, M.scout_r_card(c), 0, 0))
    rest = deck_rows + fill
    rng.shuffle(rest)
    ordered = (link_rows + rest)[:M.MEMBERS]
    points = _initial_points(rng)
    members = []
    for slot, (chara, row, pos, sid) in enumerate(ordered, start=1):
        group, cmd = M.SLOTS[slot - 1]
        ratio = M.INITIAL_STAT_RATIO[slot - 1]
        stats = {}
        for key, col in zip(M.STAT_KEYS, ("speed", "stamina", "pow", "guts", "wiz")):
            lim = int(row.get(f"{col}_limit") or 2000)
            stats[key] = max(1, min(lim, int(round(int(row[col]) * ratio
                                                   * rng.uniform(1 - STAT_JITTER, 1 + STAT_JITTER)))))
        is_link = chara in M.link_charas()
        m = {"chara": chara, "scout": int(row["id"]), "slot": slot, "group": group, "cmd": cmd,
             **stats, "gauge": 0, "star": 0, "points": points[slot - 1], "peff": [], "peff_num": 0,
             "pot": {}, "link": is_link, "deck_link": bool(is_link and pos), "pos": int(pos),
             "card": int(sid or row["support_card_id"]), "x": _x_choice(chara, rng)}
        _extend_peff(m, rng)
        members.append(m)
    st["members"] = members
    st["roster"] = True
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for m in members:
        if m["chara"] not in have:
            arr.append({"target_id": m["chara"], "training_partner_id": m["chara"], "evaluation": 0,
                        "is_outing": 0, "story_step": 0, "is_appear": 1,
                        "group_outing_info_array": []})
    log.info("arc: roster %s", [(m["slot"], m["chara"], m["cmd"]) for m in members])
    return members


def npc_members(st: dict, chara_info: dict) -> list:
    if not st["roster"]:
        return []
    return [m["chara"] for m in st["members"] if not m["pos"]]


def partner_member(st: dict, chara_info: dict, partner: int) -> dict | None:
    p = int(partner)
    if p >= RF.NPC_BODY_MIN_ID:
        return member(st, p)
    for m in st["members"]:
        if m["pos"] and int(m["pos"]) == p:
            return m
    return None


def all_rows(st: dict) -> list:
    rows = [(m["chara"], int(m["points"])) for m in st["members"]]
    rows.append((int(st["trainee"]), int(st["tpoints"])))
    return rows


def wire_ranks(st: dict) -> dict:
    rows = all_rows(st)
    return {c: 1 + sum(1 for _o, q in rows if q > p) for c, p in rows}


def strict_ranks(st: dict) -> dict:
    order = sorted(all_rows(st), key=lambda r: (-r[1], r[0]))
    return {c: i + 1 for i, (c, _p) in enumerate(order)}


def approval_rate(st: dict) -> int:
    if not st["roster"]:
        return 1
    return int(sum(p for _c, p in all_rows(st)) / 170 + 0.5)


def add_trainee_points(full_state: dict, n: int) -> None:
    st = state(full_state)
    st["tpoints"] = max(0, int(st["tpoints"]) + int(n))
    mirror(full_state)


def pool_for(turn: int) -> int:
    for last, pool in M.POOL:
        if int(turn) <= last:
            return pool
    return M.POOL[-1][1]


def _grow_points(full_state: dict, st: dict, turn: int) -> None:
    rng = rng_for(full_state, "pool", turn)
    ranks = strict_ranks(st)
    raw = []
    for m in st["members"]:
        mean = M.POOL_SHARE[min(len(M.POOL_SHARE), ranks[m["chara"]]) - 1]
        raw.append(max(0.0, min(SHARE_MAX, rng.gauss(mean, SHARE_SPREAD / 2))))
    total = sum(raw) or 1.0
    units = [int(round(r * 200 / total)) for r in raw]
    drift = 200 - sum(units)
    order = sorted(range(len(units)), key=lambda i: -raw[i])
    i = 0
    while drift and order:
        j = order[i % len(order)]
        if drift > 0 or units[j] > 0:
            units[j] += 1 if drift > 0 else -1
            drift += -1 if drift > 0 else 1
        i += 1
    step = pool_for(turn) // 200
    for m, u in zip(st["members"], units):
        m["points"] = int(m["points"]) + u * step


def _grow_stats(full_state: dict, st: dict, turn: int) -> None:
    rng = rng_for(full_state, "growth", turn)
    co = st.get("co_train") or {}
    co_charas = {int(c) for c in co.get("charas") or ()} if int(co.get("turn") or 0) == turn - 1 else set()
    co_stat = M.STAT_OF.get(int(co.get("command_id") or 0))
    for m in st["members"]:
        row = M_scout(m)
        for key, col in zip(M.STAT_KEYS, ("speed", "stamina", "pow", "guts", "wiz")):
            lim = int((row or {}).get(f"{col}_limit") or 2000)
            add = rng.randint(*GROWTH)
            if m["chara"] in co_charas and key == co_stat:
                add += rng.randint(*CO_TRAIN)
            m[key] = min(lim, int(m[key]) + add)


def M_scout(m: dict) -> dict | None:
    from ... import master_data
    r = master_data.query_one("SELECT * FROM single_mode_scout_chara WHERE id=?", (int(m["scout"]),))
    return dict(r) if r else None


def _member_potentials(st: dict, turn: int) -> None:
    for m in st["members"]:
        for pid, lv in M.MEMBER_SCHEDULE[m["group"]].get(int(turn), ()):
            m["pot"][str(pid)] = max(int(m["pot"].get(str(pid), 0)), lv)


def upkeep(full_state: dict, chara_info: dict, target: int | None = None) -> bool:
    st = state(full_state)
    if not st["roster"]:
        return False
    target = int(target or served_turn(full_state, chara_info))
    done = int(st["upkeep_turn"] or ROSTER_TURN)
    if target <= done:
        return False
    for t in range(done + 1, target + 1):
        _grow_stats(full_state, st, t)
        _member_potentials(st, t)
        _grow_points(full_state, st, t)
        st["upkeep_turn"] = t
    build_lineup(full_state, chara_info, target)
    schedule_rival_races(full_state, chara_info, target)
    return True


def start_roster_turn(full_state: dict, chara_info: dict) -> None:
    st = state(full_state)
    create_roster(full_state, chara_info)
    st["upkeep_turn"] = ROSTER_TURN
    _member_potentials(st, ROSTER_TURN)
    st["lineup"] = {}
    mirror(full_state, chara_info)


def on_training(full_state: dict, chara_info: dict, command_id: int, partners, turn: int,
                failed: bool) -> None:
    st = state(full_state)
    if not st["roster"]:
        return
    exped = formula.is_exped(command_id)
    rainbows = formula.rainbow_count(chara_info, command_id, partners)
    full, on = [], []
    for p in partners or ():
        m = partner_member(st, chara_info, p)
        if not m:
            continue
        on.append(m["chara"])
        if exped or failed:
            continue
        if int(m["gauge"]) >= GAUGE_MAX:
            full.append(m["chara"])
            continue
        m["gauge"] = min(GAUGE_MAX, int(m["gauge"]) + 1 + rainbows)
    flag(st, "not_up_boost", full)
    st["co_train"] = {"turn": int(turn), "command_id": formula.base_of(command_id), "charas": on}


def raise_gauges(st: dict, charas, n: int = 1) -> list:
    full = []
    for c in charas:
        m = member(st, c)
        if not m:
            continue
        if int(m["gauge"]) >= GAUGE_MAX:
            full.append(m["chara"])
        m["gauge"] = min(GAUGE_MAX, int(m["gauge"]) + n)
    return full


def mei_roll(full_state: dict, chara_info: dict, card_id: int, command_id, shared_chance: float):
    if int(card_id) not in (M.MEI_SSR, M.MEI_R):
        return None
    exped = formula.is_exped(int(command_id or 0))
    story = M.MEI_EXPED_STORY if exped else M.MEI_JAPAN_STORY
    wire = M.MEI_WIRE.get((int(card_id), story))
    if wire is None:
        return {"chance": 0, "story": story}
    from ...handlers import single_mode_events
    bond = 7 + (2 if single_mode_events.has_condition(chara_info, 8) else 0)
    chance = M.MEI_CHANCE.get((int(card_id), story), shared_chance)
    name = "労いのエール" if exped else "お疲れ様！！"
    effects = [{"type": "guts", "value": "+3"}, {"type": "skill_points", "value": "+3"},
               {"type": "bond", "value": f"+{bond}", "char_id": M.MEI}]
    return {"chance": chance, "story": story, "wire": wire,
            "event": {"name": name, "choices": [{"name": "", "effects": effects}]}}


def mei_resolved(full_state: dict, story_id: int) -> None:
    st = state(full_state)
    if story_id == M.MEI_EXPED_STORY:
        st["gexp"] = int(st["gexp"]) + M.MEI_EXPED_GEXP
        return
    if not st["roster"]:
        return
    co = [c for c in (st.get("co_train") or {}).get("charas") or ()
          if (member(st, c) or {}).get("gauge", GAUGE_MAX) < GAUGE_MAX]
    pick = co[:M.MEI_GAUGE_MEMBERS]
    rest = [m["chara"] for m in st["members"]
            if int(m["gauge"]) < GAUGE_MAX and m["chara"] not in pick]
    rng = rng_for(full_state, "mei", (st.get("co_train") or {}).get("turn"))
    rng.shuffle(rest)
    pick += rest[:M.MEI_GAUGE_MEMBERS - len(pick)]
    raise_gauges(st, pick)


def mark_for(vital: int) -> int:
    for floor, mark in MARK_CUTS:
        if int(vital) >= floor:
            return mark
    return 4


def lineup_open(chara_info: dict, turn: int) -> bool:
    from ...handlers import single_mode_team as smt
    if turn < ROSTER_TURN or is_expedition(turn):
        return False
    return not smt._race_for_turn(chara_info, turn)


def build_lineup(full_state: dict, chara_info: dict, turn: int) -> dict:
    st = state(full_state)
    if not lineup_open(chara_info, turn):
        st["lineup"] = {"turn": int(turn), "members": [], "special": 0}
        return st["lineup"]
    rng = rng_for(full_state, "lineup", turn)
    full = [m["chara"] for m in st["members"] if int(m["gauge"]) >= GAUGE_MAX]
    if len(full) > LINEUP_MAX:
        full = rng.sample(full, LINEUP_MAX)
    order = {m["chara"]: i for i, m in enumerate(sorted(st["members"], key=lambda m: (-int(m["points"]), m["chara"])))}
    full.sort(key=lambda c: order[c])
    special = 0
    if len(full) == LINEUP_MAX:
        since = int(st["since_sss"])
        if since >= SSS_SURE:
            special = 1
        elif since in SSS_ODDS and rng.random() < SSS_ODDS[since]:
            special = 1
    st["lineup"] = {"turn": int(turn), "members": full, "special": special,
                    "preview": ss_preview(st, chara_info, full, special),
                    "price": price_lineup(st, full, special)}
    return st["lineup"]


def price_lineup(st: dict, charas, special: int) -> dict:
    ranks = strict_ranks(st)
    me = ranks[int(st["trainee"])]
    out = {}
    for c in charas:
        s = me + ranks[c]
        win = 995 - 15 * s if s <= 13 else 930 - 10 * s
        out[str(c)] = [win * (2 if special else 1), int(0.6 * win - 96)]
    return out


def ss_preview(st: dict, chara_info: dict, charas, special: int) -> list:
    out = [0] * 5
    for c in charas:
        m = member(st, c)
        for i, key in enumerate(M.STAT_KEYS):
            out[i] += 1 + max(0, int(m[key]) // 150 - 1)
        spec = M.TARGET_OF[m["cmd"]] - 1
        out[spec] += 6 if m["deck_link"] else 4
    if special:
        out = [v + 15 for v in out]
    params = [{"target_type": i + 1, "value": RF.displayed(chara_info, i + 1, v)}
              for i, v in enumerate(out) if v]
    params.append({"target_type": formula.TARGET_SP, "value": (10 if special else 5) * len(charas)})
    return params


def current_peff(m: dict) -> list:
    return min(m["peff"], key=lambda e: e[0]) if m["peff"] else [0, 0, 0]


def bonus_line(st: dict, chara_info: dict, charas) -> list:
    add = {}
    for c in charas:
        m = member(st, c)
        _num, grp, value = current_peff(m)
        if grp == 11:
            tt = M.TARGET_OF[m["cmd"]]
            add[tt] = add.get(tt, 0) + int(value)
        elif grp == 12:
            add[formula.TARGET_SP] = add.get(formula.TARGET_SP, 0) + 20
    return [{"target_type": tt, "value": RF.displayed(chara_info, tt, v) if tt <= 5 else v}
            for tt, v in sorted(add.items())]


def selection_info(st: dict, chara_info: dict, turn: int) -> dict:
    lu = st.get("lineup") or {}
    charas = [int(c) for c in lu.get("members") or ()] if int(lu.get("turn") or 0) == int(turn) else []
    if not charas:
        return {"all_win_approval_point": 0, "is_special_match": 0, "params_inc_dec_info_array": [],
                "bonus_params_inc_dec_info_array": [], "selection_rival_info_array": []}
    spent = bool(lu.get("spent"))
    special = int(lu.get("special") or 0)
    price = price_lineup(st, charas, special) if spent else lu.get("price") or {}
    vital = int(chara_info.get("vital") or 0)
    rows = []
    for c in charas:
        win, lose = price.get(str(c), [0, 0])
        mark = 4 if spent else (1 if special else mark_for(vital))
        rows.append({"chara_id": c, "mark": mark, "win_approval_point": int(win),
                     "lose_approval_point": int(lose)})
    return {"all_win_approval_point": sum(r["win_approval_point"] for r in rows),
            "is_special_match": special,
            "params_inc_dec_info_array": [dict(p) for p in lu.get("preview") or ()],
            "bonus_params_inc_dec_info_array": bonus_line(st, chara_info, charas),
            "selection_rival_info_array": rows}


class Refused(Exception):
    pass


def run_match(full_state: dict, chara_info: dict, turn: int) -> dict:
    st = state(full_state)
    lu = st.get("lineup") or {}
    charas = [int(c) for c in lu.get("members") or ()]
    if int(lu.get("turn") or 0) != int(turn) or not charas or lu.get("spent"):
        raise Refused("no lineup")
    rng = rng_for(full_state, "match", turn)
    special = int(lu.get("special") or 0)
    vital = int(chara_info.get("vital") or 0)
    price = lu.get("price") or {}
    results, beaten, not_up = [], [], []
    for c in charas:
        m = member(st, c)
        win, lose = price.get(str(c), [0, 0])
        mark = 1 if special else mark_for(vital)
        won = rng.random() >= LOSS_ODDS.get(mark, 0.0)
        m["star"] = int(m["star"]) + 1
        if won:
            st["tpoints"] = int(st["tpoints"]) + int(win)
            m["points"] = int(m["points"]) + int(lose)
            beaten.append(c)
        else:
            st["tpoints"] = int(st["tpoints"]) + int(lose)
            m["points"] = int(m["points"]) + int(win)
        results.append({"chara_id": c, "result": 1 if won else 2})
    won_bonus = bonus_line(st, chara_info, beaten)
    kept = set()
    for c in beaten:
        if _apply_peff(full_state, chara_info, member(st, c), turn, not_up) == 6:
            kept.add(c)
    for c in charas:
        if c not in kept:
            member(st, c)["gauge"] = 0
    st["ss_wins"] = int(st["ss_wins"]) + len(beaten)
    if special:
        st["sss_wins"] = int(st["sss_wins"]) + len(beaten)
        st["since_sss"] = 0
    else:
        st["since_sss"] = int(st["since_sss"]) + len(beaten)
    lu["spent"] = True
    lu["in_match"] = len(charas)
    _bond_from_match(chara_info, st, beaten, turn)
    flag(st, "ss_end", {"turn": int(turn), "n": len(charas)})
    mirror(full_state, chara_info)
    log.info("arc: SS match T%s %s -> %s (SSS %s), trainee %s pts", turn, charas,
             [r["result"] for r in results], special, st["tpoints"])
    return {"results": results, "bonus": won_bonus, "not_up": sorted(set(not_up))}


def _apply_peff(full_state: dict, chara_info: dict, m: dict, turn: int, not_up: list) -> int:
    from ... import event_engine
    st = state(full_state)
    num, grp, _value = current_peff(m)
    m["peff"] = [e for e in m["peff"] if e[0] != num]
    rng = rng_for(full_state, "peff", turn, m["chara"])
    if grp == 1:
        event_engine._apply_effects(chara_info, member_hint(chara_info, m, rng))
    elif grp in (4, 5):
        vital, cap = int(chara_info.get("vital") or 0), int(chara_info.get("max_vital") or 100)
        if grp == 4:
            chara_info["max_vital"] = cap + 4
            cap += 4
        if vital >= cap:
            not_up.append(6)
        chara_info["vital"] = min(cap, vital + 20)
        if grp == 5:
            mood = int(chara_info.get("motivation") or 3)
            if mood >= 5:
                not_up.append(20)
            chara_info["motivation"] = min(5, mood + 1)
    elif grp == 7:
        st["gexp"] = int(st["gexp"]) + 50
    elif grp in (8, 9):
        from ...handlers import single_mode_events
        single_mode_events.add_condition(chara_info, 8 if grp == 8 else 10)
    _extend_peff(m, rng_for(full_state, "peff-next", m["chara"], m.get("peff_num")))
    return grp


def member_hint(chara_info: dict, m: dict, rng: random.Random) -> list:
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or ()}
    if m["pos"]:
        sid = int(m["card"])
        level = 1 + int(round(RF.card_effect(sid, 17, RF.max_level(sid)) or 0))
    else:
        sid, level = int(m["card"]), 1
    pool = [s for s in M.hint_skills(sid) if s not in learned] or list(M.hint_skills(sid))
    if not pool:
        return []
    return [{"type": "skill_hint", "skill_id": rng.choice(pool), "value": f"+{max(1, level)}"}]


def _bond_from_match(chara_info: dict, st: dict, beaten, turn: int) -> None:
    gain = 7 if int(turn) < 25 else 9
    for c in beaten:
        m = member(st, c)
        if not m["pos"]:
            continue
        for row in chara_info.get("evaluation_info_array") or ():
            if isinstance(row, dict) and int(row.get("target_id") or 0) == int(m["pos"]):
                row["evaluation"] = min(100, int(row.get("evaluation") or 0) + gain)


def cond_count(st: dict, cond: tuple, chara_info: dict, race_history) -> int:
    cid, _num, ctype, v1, v2, v3, total = cond
    if ctype == 142:
        return min(total, int(st["ss_wins"]))
    if ctype == 24:
        if cid in (6, 11):
            return 1 if st["expedition_started"] else 0
        return 1 if st["trust_seen"] else 0
    if ctype == 60:
        n = sum(1 for h in race_history or ()
                if int(h.get("result_rank") or 99) == 1 and M.program_grade(h.get("program_id") or 0) == v1)
        return min(total, n)
    if ctype == 144:
        return 1 if any(int(h.get("program_id") or 0) == v2 for h in race_history or ()) else 0
    if ctype == 57:
        return 1 if any(int(h.get("program_id") or 0) == v2 and int(h.get("result_rank") or 99) <= v3
                        for h in race_history or ()) else 0
    return 0


def _history(full_state: dict) -> list:
    from ...handlers import single_mode_team as smt
    return ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("race_history") or []


def unlockable(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    hist = _history(full_state)
    out = []
    for pid, conds in sorted(M.potential_conditions().items()):
        if int(st["pot"].get(str(pid), 0)):
            continue
        if all(cond_count(st, c, chara_info, hist) >= c[6] for c in conds):
            out.append(pid)
    return out


def unlock(full_state: dict, chara_info: dict, pids) -> None:
    from ... import event_engine
    st = state(full_state)
    for pid in pids:
        if int(st["pot"].get(str(pid), 0)):
            continue
        st["pot"][str(pid)] = 1
        if int(pid) == 10:
            event_engine._apply_effects(chara_info, [
                {"type": "skill_hint", "skill_id": s, "value": "+1"} for s in M.ARC_DREAM_HINTS])
    mirror(full_state, chara_info)


def level_up(full_state: dict, chara_info: dict, requests) -> None:
    st = state(full_state)
    plan, cost = {}, 0
    for r in requests or ():
        pid, new = int(r.get("potential_id") or 0), int(r.get("level") or 0)
        have = int(plan.get(pid, st["pot"].get(str(pid), 0)))
        if new > M.max_level(pid):
            raise Invalid(f"potential {pid} has no level {new}")
        if not have or new <= have:
            raise Refused(f"potential {pid} at {have} -> {new}")
        for lv in range(have + 1, new + 1):
            c = M.potential_costs().get((pid, lv))
            if c is None:
                raise Invalid(f"potential {pid} has no level {lv}")
            cost += c
        plan[pid] = new
    if cost > int(st["gexp"]):
        raise Refused(f"{cost} points over {st['gexp']}")
    st["gexp"] = int(st["gexp"]) - cost
    for pid, new in plan.items():
        st["pot"][str(pid)] = new
    mirror(full_state, chara_info)


class Invalid(Exception):
    pass


def potential_array(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    hist = _history(full_state)
    out = []
    for pid, conds in sorted(M.potential_conditions().items()):
        lv = int(st["pot"].get(str(pid), 0))
        progress = None if lv else [
            {"condition_id": c[0], "current_count": cond_count(st, c, chara_info, hist), "total_count": c[6]}
            for c in conds]
        out.append({"level": lv, "potential_id": pid, "progress_array": progress})
    return out


def level_sum(st: dict) -> int:
    return sum(int(v) for v in st["pot"].values())


def refresh_preview(full_state: dict, chara_info: dict, commands, card_levels=None,
                    turn: int | None = None) -> bool:
    st = state(full_state)
    turn = int(turn or turn_of(chara_info))
    rate = approval_rate(st)
    lv = {int(k): int(v) for k, v in st["pot"].items()}
    rows = {}
    for c in commands or ():
        if c.get("command_type") != 1:
            continue
        cmd = int(c["command_id"])
        partners = list(c.get("training_partner_array") or ())
        level = int(c.get("level") or 1)
        home_energy = next((int(x.get("value") or 0) for x in c.get("params_inc_dec_info_array") or ()
                            if int(x.get("target_type") or 0) == formula.TARGET_ENERGY), 0)
        line = formula.scenario_line(chara_info, cmd, level, partners, card_levels, rate, lv, home_energy)
        rows[str(cmd)] = {"line": line, "gexp": formula.global_exp(chara_info, cmd, partners),
                          "partners": partners}
    before = st["preview"]
    st["preview"] = {"turn": turn, "rows": rows}
    return before != st["preview"]


def preview_row(st: dict, command_id, turn: int) -> dict:
    pv = st["preview"]
    if int(pv.get("turn") or 0) != int(turn):
        return {}
    return pv["rows"].get(str(int(command_id or 0))) or {}


def cap_bonus(slot: int) -> int:
    return 110 - 10 * slot if slot <= 6 else 80 - 5 * slot


def race_stats(stats: list, levels: dict, race_instance_id: int, caps: list):
    add = [0] * 5
    turf = mid = None
    for (pid, plv), bv in M.potential_buff().items():
        if int(levels.get(pid, 0)) >= plv:
            add = [a + b for a, b in zip(add, bv[:5])]
            turf = 7 if bv[5] else turf
            mid = 7 if bv[6] else mid
    for did in M.race_debuffs().get(int(race_instance_id), ()):
        pid, plv, *v = M.debuff()[did]
        if int(levels.get(pid, 0)) < plv:
            add = [a + b for a, b in zip(add, v[:5])]
            turf = 1 if v[5] else turf
            mid = 1 if v[6] else mid
    return [max(1, min(c, s + a)) for s, a, c in zip(stats, add, caps)], turf, mid


def apply_player_race_stats(chara_info: dict, player: dict, program_id: int) -> None:
    levels = mirrored_levels(chara_info)
    inst = M.program_instance(program_id)
    keys = ("speed", "stamina", "power", "guts", "wiz")
    caps = [int(chara_info.get(f"max_{k}") or 2000) for k in keys]
    base = [int(player.get(k if k != "power" else ("power" if "power" in player else "pow")) or 0)
            for k in keys]
    stats, turf, mid = race_stats(base, levels, inst, caps)
    for k, v in zip(keys, stats):
        player[k] = v
        if k == "power" and "pow" in player:
            player["pow"] = v
    if turf is not None:
        player["proper_ground_turf"] = max(int(player.get("proper_ground_turf") or 1), 7) if turf == 7 else 1
    if mid is not None:
        player["proper_distance_middle"] = (max(int(player.get("proper_distance_middle") or 1), 7)
                                            if mid == 7 else 1)


def member_runner(full_state: dict, m: dict, program_id: int, turn: int) -> dict:
    from ...handlers import single_mode_team as smt
    row = M_scout(m) or {}
    inst = M.program_instance(program_id)
    caps = [int(row.get(f"{c}_limit") or 2000) + cap_bonus(int(m["slot"]))
            for c in ("speed", "stamina", "pow", "guts", "wiz")]
    levels = {int(k): int(v) for k, v in m["pot"].items()}
    stats, turf, mid = race_stats([int(m[k]) for k in M.STAT_KEYS], levels, inst, caps)
    skills = list(M.hint_skills(int(row.get("support_card_id") or m["card"])))
    if int(turn) < FULL_SKILLS_TURN:
        rng = rng_for(full_state, "mskills", turn, m["chara"])
        skills = [s for s in skills if rng.random() < EARLY_SKILL_KEEP]
    out = {"viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
           "trained_chara_id": TRAINEE_SLOT_BASE + int(m["slot"]),
           "single_mode_chara_id": TRAINEE_SLOT_BASE + int(m["slot"]),
           "nickname_id": 0, "card_id": int(m["chara"]) * 100 + 1, "mob_id": 0, "chara_id": int(m["chara"]),
           "race_dress_id": int(m["chara"]) * 100 + 1, "rarity": 3, "talent_level": 1,
           "skill_array": [{"skill_id": s, "level": 1} for s in skills],
           "speed": stats[0], "stamina": stats[1], "power": stats[2], "pow": stats[2],
           "guts": stats[3], "wiz": stats[4],
           "running_style": smt._npc_running_style(row) if row else 2, "motivation": 5,
           "_arc_member": int(m["chara"])}
    for k in ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
              "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
              "proper_running_style_sashi", "proper_running_style_oikomi", "proper_ground_turf",
              "proper_ground_dirt"):
        out[k] = int(row.get(k) or 1)
    if turf is not None:
        out["proper_ground_turf"] = max(out["proper_ground_turf"], 7) if turf == 7 else 1
    if mid is not None:
        out["proper_distance_middle"] = max(out["proper_distance_middle"], 7) if mid == 7 else 1
    return out


def npc_runner(full_state: dict, row: dict, program_id: int, index: int) -> dict:
    from ...handlers import race_mobs
    from ...handlers import single_mode_team as smt
    bands = M.NPC_BANDS.get(int(program_id)) or ((1.0, 1.0),) * 5
    rng = rng_for(full_state, "npc", program_id, row["id"], index)
    stats = [max(1, int(round(int(row[c]) * rng.uniform(lo, hi))))
             for c, (lo, hi) in zip(("speed", "stamina", "pow", "guts", "wiz"), bands)]
    mob = int(row.get("mob_id") or 0)
    out = {"viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
           "trained_chara_id": int(row["id"]), "single_mode_chara_id": int(row["id"]),
           "nickname_id": 0, "card_id": 0, "mob_id": mob, "chara_id": 1 if mob else int(row["chara_id"]),
           "race_dress_id": race_mobs.mob_dress(mob) if mob else int(row["race_dress_id"]),
           "rarity": 1 if mob else 3, "talent_level": 1, "skill_array": smt._npc_skills(row),
           "speed": stats[0], "stamina": stats[1], "power": stats[2], "pow": stats[2],
           "guts": stats[3], "wiz": stats[4], "running_style": smt._npc_running_style(row),
           "motivation": rng.randint(int(row.get("motivation_min") or 4) or 4,
                                     max(int(row.get("motivation_min") or 4) or 4,
                                         int(row.get("motivation_max") or 5)))}
    for k in ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
              "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
              "proper_running_style_sashi", "proper_running_style_oikomi", "proper_ground_turf",
              "proper_ground_dirt"):
        out[k] = int(row.get(k) or 1)
    out["proper_ground_turf"] = max(out["proper_ground_turf"], 7)
    out["proper_distance_middle"] = max(out["proper_distance_middle"], 7)
    return out


def top_members(st: dict, n: int) -> list:
    ranks = strict_ranks(st)
    return sorted(st["members"], key=lambda m: ranks[m["chara"]])[:n]


def overseas_field(full_state: dict, chara_info: dict, program_id: int, count: int) -> list:
    st = state(full_state)
    turn = served_turn(full_state, chara_info)
    program_id = int(program_id)
    if program_id in M.EXHIBITION_FIELD:
        n, park, _total = M.EXHIBITION_FIELD[program_id]
        npcs = [park] if park else []
    elif program_id in (M.NIEL, M.FOY):
        n, npcs = 3, []
    elif program_id == M.ARC_CLASSIC:
        n, npcs = 3, [M.PARK_CLASSIC]
    else:
        n, npcs = 3, [M.PARK_SENIOR, M.RIGANTONA] + ([M.MONTJEU] if st["classic_won"] else [])
    out = []
    for i, row_id in enumerate(npcs):
        row = M.npc_row(row_id)
        if row:
            out.append(npc_runner(full_state, row, program_id, i))
    for m in top_members(st, n):
        out.append(member_runner(full_state, m, program_id, turn))
    mobs = list(M.overseas_mobs())
    rng = rng_for(full_state, "mobs", program_id, turn)
    rng.shuffle(mobs)
    i = 0
    while len(out) < count and mobs:
        out.append(npc_runner(full_state, mobs[i % len(mobs)], program_id, 100 + i))
        i += 1
    return out[:count]


def scheduled_members(st: dict, program_id: int, turn: int) -> list:
    rr = st.get("rival_races") or {}
    if int(rr.get("turn") or 0) != int(turn):
        return []
    return [int(r["chara_id"]) for r in rr.get("rows") or () if int(r["program_id"]) == int(program_id)]


def schedule_rival_races(full_state: dict, chara_info: dict, turn: int) -> None:
    from ...handlers import single_mode_team as smt
    st = state(full_state)
    rows = []
    turn = int(turn)
    if turn in M.EXHIBITIONS:
        prog = M.EXHIBITIONS[turn]
        rows += [{"program_id": prog, "chara_id": m["chara"]}
                 for m in top_members(st, M.EXHIBITION_FIELD[prog][0])]
    goal = smt._race_for_turn(chara_info, turn)
    goal_prog = int((goal or {}).get("program_id") or 0)
    if goal_prog in M.LONGCHAMP or goal_prog in (M.DERBY, M.TAKARAZUKA):
        rows += [{"program_id": goal_prog, "chara_id": m["chara"]} for m in top_members(st, 3)]
    rng = rng_for(full_state, "rival-races", turn)
    taken = {r["chara_id"] for r in rows}
    for prog in _graded_turf_programs(turn):
        if prog in (goal_prog,) or prog in M.LONGCHAMP:
            continue
        eligible = [m for m in st["members"] if m["chara"] not in taken and _distance_ok(m, prog)]
        if not eligible:
            continue
        k = min(len(eligible), rng.randint(*RIVAL_RACE_MEMBERS))
        for m in rng.sample(eligible, k):
            rows.append({"program_id": prog, "chara_id": m["chara"]})
            taken.add(m["chara"])
    st["rival_races"] = {"turn": turn, "rows": rows}


def _graded_turf_programs(turn: int) -> list:
    from ... import master_data
    row = master_data.query_one("SELECT year, month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
                                (M.SCENARIO_ID, int(turn)))
    if not row:
        return []
    year = int(row["year"])
    perms = (1,) if year == 1 else (year, 4)
    marks = ",".join("?" * len(perms))
    rows = master_data.query(
        "SELECT p.id AS id FROM single_mode_program p JOIN race_instance ri ON ri.id=p.race_instance_id "
        "JOIN race r ON r.id=ri.race_id JOIN race_course_set cs ON cs.id=r.course_set "
        f"WHERE p.month=? AND p.half=? AND r.grade IN (100, 200, 300) AND cs.ground=1 "
        f"AND p.race_permission IN ({marks}) AND p.id < 2900",
        (int(row["month"]), int(row["half"]), *perms))
    return sorted({int(r["id"]) for r in rows})


def _distance_ok(m: dict, program_id: int) -> bool:
    from ... import master_data
    r = master_data.query_one(
        "SELECT cs.distance AS d FROM single_mode_program p JOIN race_instance ri ON ri.id=p.race_instance_id "
        "JOIN race r ON r.id=ri.race_id JOIN race_course_set cs ON cs.id=r.course_set WHERE p.id=?",
        (int(program_id),))
    if not r:
        return False
    d = int(r["d"])
    col = ("proper_distance_short" if d <= 1400 else "proper_distance_mile" if d <= 1800
           else "proper_distance_middle" if d <= 2400 else "proper_distance_long")
    row = M_scout(m) or {}
    return int(row.get(col) or 1) >= MIN_DISTANCE_APT


def race_points(program_id: int, place: int) -> int:
    rows = M.win_points()
    inst = M.program_instance(program_id)
    table = rows.get((inst, 0)) or rows.get((0, M.program_grade(program_id))) or []
    return next((pt for lo, hi, pt in table if lo <= int(place) <= hi), 0)


def pay_race_points(full_state: dict, program_id: int, trainee_place: int, member_places: dict) -> None:
    st = state(full_state)
    st["tpoints"] = int(st["tpoints"]) + race_points(program_id, trainee_place)
    for c, place in (member_places or {}).items():
        m = member(st, int(c))
        if m:
            m["points"] = int(m["points"]) + race_points(program_id, int(place))
    mirror(full_state)


def rival_array(st: dict) -> list:
    ranks = wire_ranks(st)
    trainee = int(st["trainee"])
    rows = [(int(m["points"]), int(m["chara"]), m) for m in st["members"]]
    rows.append((int(st["tpoints"]), trainee, None))
    out = []
    for points, chara, m in sorted(rows, key=lambda r: (-r[0], r[1])):
        if m is None:
            out.append({"chara_id": trainee, "rank": ranks.get(trainee, 1), "approval_point": points})
            continue
        out.append({
            "chara_id": m["chara"], "scout_chara_id": m["scout"],
            "speed": int(m["speed"]), "stamina": int(m["stamina"]), "power": int(m["power"]),
            "guts": int(m["guts"]), "wiz": int(m["wiz"]), "command_id": m["cmd"],
            "rival_boost": int(m["gauge"]), "star_lv": int(m["star"]), "rank": ranks[m["chara"]],
            "approval_point": points,
            "potential_array": [{"level": int(v), "potential_id": int(k)}
                                for k, v in sorted(m["pot"].items(), key=lambda kv: int(kv[0]))],
            "selection_peff_array": [{"effect_group_id": g, "effect_num": n, "effect_value": v}
                                     for n, g, v in sorted(m["peff"])[:3]]})
    return out


def arc_info(full_state: dict, chara_info: dict) -> dict:
    st = state(full_state)
    return {"approval_rate": approval_rate(st), "global_exp": int(st["gexp"]),
            "sp_tag_boost_type": SP_TAG_BOOST_TYPE,
            "ss_match_win_count": int(st["ss_wins"]),
            "special_ss_match_win_count": int(st["sss_wins"]),
            "potential_array": potential_array(full_state, chara_info)}


def command_rows(st: dict, commands, turn: int) -> list:
    out = []
    for c in commands or ():
        cid, ctype = int(c.get("command_id") or 0), int(c.get("command_type") or 0)
        row = preview_row(st, cid, turn) if ctype == 1 else {}
        out.append({"command_type": ctype, "command_id": cid,
                    "add_global_exp": int(row.get("gexp") or 0),
                    "params_inc_dec_info_array": [dict(p) for p in row.get("line") or ()]})
    return out


def evaluation_array(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    by_pos = RF.deck(chara_info)
    npc = dict(M.npc_partners())
    out = []
    for e in sorted((e for e in chara_info.get("evaluation_info_array") or () if isinstance(e, dict)),
                    key=lambda e: int(e.get("target_id") or 0)):
        t = int(e.get("target_id") or 0)
        if t in by_pos:
            chara = RF.card_meta(by_pos[t])[3]
        elif t in npc:
            chara = npc[t]
        elif t >= RF.NPC_BODY_MIN_ID:
            chara = t
        else:
            continue
        out.append({"chara_id": chara, "target_id": t})
    return out


def data_set(full_state: dict, chara_info: dict, commands, turn: int, full: bool) -> dict:
    st = state(full_state)
    not_up = flag(st, "not_up_boost") or []
    races = [{"race_num": int(r["race_num"]), "turn": int(r["turn"]), "result_rank": int(r["result_rank"])}
             for r in st["races"]]
    rr = st.get("rival_races") or {}
    rival_races = [dict(r) for r in rr.get("rows") or ()] if int(rr.get("turn") or 0) == int(turn) else []
    return {
        "arc_info": arc_info(full_state, chara_info),
        "arc_rival_array": rival_array(st) if st["roster"] else [
            {"chara_id": int(st["trainee"] or (int(chara_info.get("card_id") or 0) // 100)),
             "rank": 1, "approval_point": int(st["tpoints"])}],
        "rival_race_info_array": rival_races,
        "selection_info": selection_info(st, chara_info, turn),
        "race_history_array": races,
        "command_info_array": command_rows(st, commands, turn) if full else None,
        "evaluation_info_array": evaluation_array(full_state, chara_info),
        "not_up_arc_parameter_info": {"rival_boost_chara_id_array": list(not_up),
                                      "all_rival_boost_flag": False},
    }


def career_over(chara_info: dict) -> bool:
    return RI.career_over(chara_info)
