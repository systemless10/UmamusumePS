from __future__ import annotations

import copy
import functools
import logging
import random

from .. import champions_pool as POOL, config, master_data as M, patch, race_conditions, rating_formula, social
from .. import state as state_store
from ..result_codes import ResultCode, refuse
from ..simulation import race_simulator
from . import daily_races, practice_race, registry, shop, trained_chara

log = logging.getLogger("uma-server")

KEY = "team_building_state"
TEAM_SIZE = 7
CAPTAIN = 1
RACE_TYPE = 11
RACE_GROUP = 5
SWITCH_EVERY = 3
NATURAL_MAX = 5
START_NPC_GROUP = 1
CLOCK_ITEM = 95
JELLY_ITEMS = {34}
NPC_TCID_BASE = 700_000
SCOUT_TCID_BASE = 900_000_000

ST_NONE, ST_SELECTED, ST_RUN, ST_WIN_SHOWN, ST_SKIPPED, ST_FIRST = 0, 1, 2, 3, 4, 5

def WIN_BASIC(rank: int) -> int:
    if rank <= 5:
        return 100
    if rank <= 29:
        group = (rank - 2) // 4
        if rank >= 24:
            return {24: 600, 25: 700, 26: 700, 27: 700, 28: 800, 29: 900}[rank]
        return 100 * (group + 1)
    return 1000
LOSS_RATE = 50
TEAM_SKILL_SCORE = 3556
ROSTER_SCORE = 105
BAND = 0.12
OFFER_SHIFTS = (1.10, 1.00, 0.90)


def _ok(data=None) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data if data is not None else {}}


def _now() -> int:
    return patch._servertime()


def _vid(payload) -> str:
    return str(payload.get("viewer_id"))


def active(now: int | None = None) -> dict | None:
    if not M.has_table("team_building_data"):
        return None
    forced = config.get("team_building_id")
    if forced:
        r = M.query_one("SELECT * FROM team_building_data WHERE team_building_event_id=?", (int(forced),))
        return dict(r) if r else None
    now = _now() if now is None else now
    r = M.query_one("SELECT * FROM team_building_data WHERE notice_date<=? AND end_date>=? "
                    "ORDER BY team_building_event_id DESC LIMIT 1", (now, now))
    return dict(r) if r else None


def _racing(ev) -> bool:
    return bool(ev) and (bool(config.get("team_building_id")) or ev["start_date"] <= _now() <= ev["ending_date"])


def _ticket_id(ev) -> int:
    return ev.get("ticket_item_id") or 2000 + ev["team_building_event_id"] - 1000


def _pt_id(ev) -> int:
    return ev.get("scout_pt_item_id") or 1000 + ev["team_building_event_id"] - 1000


def _rows(sql, args=()) -> list[dict]:
    return [dict(r) for r in M.query(sql, args)]


@M.context_cache(maxsize=4)
def _ranks() -> list:
    return _rows("SELECT * FROM team_building_rank ORDER BY team_rank")


def team_rank(score: int) -> int:
    for r in _ranks():
        if r["team_min_value"] <= score <= r["team_max_value"]:
            return r["team_rank"]
    return _ranks()[-1]["team_rank"] if score > 0 else 1


def _pct_row(table: str, lo: str, hi: str | None, value: int) -> int:
    rows = _rows(f"SELECT * FROM {table} ORDER BY {lo}")
    for r in rows:
        if r[lo] <= value and (hi is None or value <= r[hi]):
            return r["bonus_value"] // 100
    return 0


def roster_pct(count: int) -> int:
    return _pct_row("team_building_chara_count", "collection_chara_min", "collection_chara_max", count)


def streak_pct(streak: int) -> int:
    rows = _rows("SELECT * FROM team_building_consecutive_wins ORDER BY consecutive_wins_num")
    if not rows or streak < rows[0]["consecutive_wins_num"]:
        return 0
    row = rows[-1] if streak > rows[-1]["consecutive_wins_num"] else \
        next((r for r in rows if r["consecutive_wins_num"] == streak), None)
    return row["bonus_value"] // 100 if row else 0


def scout_cost(chara_rank: int, best_score: int) -> int:
    r = M.query_one("SELECT scout_value FROM team_building_scout_point WHERE chara_rank<=? "
                    "ORDER BY chara_rank DESC LIMIT 1", (chara_rank,))
    if r is None:
        return 0
    value = r["scout_value"]
    d = M.query_one("SELECT discount_value FROM team_building_discount_point WHERE team_rank=?",
                    (team_rank(best_score),))
    pct = (d["discount_value"] if d else 0) // 100
    return value * (100 - pct) // 100


def _collection(eid) -> dict:
    return {r["chara_id"]: r for r in _rows(
        "SELECT * FROM team_building_collection_chara WHERE team_building_event_id=?", (eid,))}


def _sets(eid) -> list[dict]:
    out = []
    for s in _rows("SELECT * FROM team_building_collection_set WHERE team_building_event_id=?", (eid,)):
        s["charas"] = [r["chara_id"] for r in _rows(
            "SELECT chara_id FROM team_building_chara_group WHERE chara_group_id=?", (s["chara_group_id"],))]
        out.append(s)
    return out


@M.context_cache(maxsize=4)
def _race_instances() -> list:
    return [r["id"] for r in M.query(
        "SELECT ri.id FROM race_instance ri JOIN race r ON r.id=ri.race_id WHERE r.\"group\"=? ORDER BY ri.id",
        (RACE_GROUP,))]


def _new(eid) -> dict:
    return {"event_id": eid, "team": None, "team_name": "", "total_race": 0, "total_win": 0, "streak": 0,
            "best_score": 0, "ranks_got": [], "collection": {}, "groups": [], "new_groups": [],
            "ticket_day": -1, "buy_day": -1, "bought": 0, "cont_day": -1, "daily_continue": 0,
            "offers": None, "race": None, "seq": 0, "runs": [], "scouts": [], "scouted_from": [],
            "rot_seed": random.getrandbits(31), "first": True, "state": ST_FIRST}


def _state(full, ev) -> dict:
    st = full.get(KEY)
    if not isinstance(st, dict) or st.get("event_id") != ev["team_building_event_id"]:
        st = _new(ev["team_building_event_id"])
        full[KEY] = st
    _refill(full, st, ev)
    return st


def _day(ev) -> int:
    h, m, s = (int(x) for x in (ev.get("stamina_add_time") or "22:00:00").split(":"))
    return (_now() - (h * 3600 + m * 60 + s)) // 86400


def _refill(full, st, ev) -> None:
    day = _day(ev)
    if st["ticket_day"] != day:
        tid = _ticket_id(ev)
        have = shop._item_count(full, tid)
        first = st["ticket_day"] < 0
        want = ev["stamina_default"] if first else max(have, min(NATURAL_MAX, ev["stamina_max"]))
        if want > have:
            shop._add_item(full, tid, want - have)
        st["ticket_day"] = day
    if st["buy_day"] != day:
        st["buy_day"], st["bought"] = day, 0
    if st["cont_day"] != day:
        st["cont_day"], st["daily_continue"] = day, 0


def _need(payload, racing=True):
    ev = active()
    if not ev or (racing and not _racing(ev)):
        return None, None, None, refuse(ResultCode.TEAM_BUILDING_NOT_IN_TERM)
    full = state_store.get_state(_vid(payload)) or {}
    return ev, full, _state(full, ev), None


def _save(payload, full) -> None:
    state_store.save_state(_vid(payload), full)


def _stat(c, k):
    return c.get(k, c.get({"pow": "power", "power": "pow"}.get(k, k), 0)) or 0


def rank_score_of(c: dict) -> int:
    if c.get("rank_score"):
        return int(c["rank_score"])
    stats = [_stat(c, k) for k in ("speed", "stamina", "pow", "guts", "wiz")]
    return min(rating_formula.MAX_RANK_SCORE,
               rating_formula.get_rating(stats) + rating_formula.get_skill_score(c.get("skill_array")))


@M.context_cache(maxsize=4)
def _npc_rows() -> dict:
    return {r["id"]: r for r in _rows("SELECT * FROM team_building_race_npc")}


def _npc_member(row_id: int, member_id: int, style: int | None = None) -> dict:
    row = _npc_rows()[row_id]
    c = daily_races._npc_chara(row)
    c["trained_chara_id"] = c["single_mode_chara_id"] = NPC_TCID_BASE + row_id
    score = rank_score_of(c)
    return {"member_id": member_id, "kind": "npc", "npc_id": row_id, "chara_id": c["chara_id"],
            "rank_score": score, "rank": trained_chara._rank_for_score(score),
            "running_style": style or c["running_style"], "chara": c}


def _tc_member(tc: dict, member_id: int, style: int | None = None) -> dict:
    tc = copy.deepcopy(tc)
    score = rank_score_of(tc)
    tc["rank_score"] = score
    tc.setdefault("rank", trained_chara._rank_for_score(score))
    chara_id = tc.get("chara_id") or (tc.get("card_id") or 0) // 100
    return {"member_id": member_id, "kind": "tc", "chara_id": chara_id,
            "rank_score": score, "rank": tc["rank"],
            "running_style": style or tc.get("running_style") or 1, "chara": tc}


def _entry_wire(m: dict) -> dict:
    if m["kind"] == "npc":
        return {"member_id": m["member_id"], "trained_chara": None,
                "mob_chara": {"npc_id": m["npc_id"], "rank": m["rank"], "rank_score": m["rank_score"],
                              "running_style": m["running_style"]}}
    tc = {k: v for k, v in m["chara"].items() if not k.startswith("_")}
    tc["running_style"] = m["running_style"]
    return {"member_id": m["member_id"], "trained_chara": tc,
            "mob_chara": {"npc_id": 0, "rank": 0, "rank_score": 0, "running_style": 0}}


def _team_score(team: list[dict], st: dict | None) -> int:
    score = sum(m["rank_score"] for m in team)
    if st is not None:
        score += ROSTER_SCORE * len(st["collection"]) + TEAM_SKILL_SCORE * len(st["groups"])
    return score


def _team_info(team_id, vid, name, honor, team_name, score, members) -> dict:
    return {"team_id": team_id, "viewer_id": vid, "user_name": name, "honor_data": {"honor_id": honor or 0},
            "team_name": team_name, "team_score": score,
            "entry_chara_array": [_entry_wire(m) for m in members]}


def _head(vid) -> dict:
    from . import room_match
    return room_match._user_head(vid)


def _own_info(vid, st) -> dict | None:
    if not st["team"]:
        return None
    head = _head(vid)
    return _team_info(0, social._wire_id(vid), head["name"], head["honor_id"],
                      st["team_name"] or head["name"], _team_score(st["team"], st), st["team"])


DIRT_SLOTS = (6, 7)
DIRT_MIN = 6


@functools.lru_cache(maxsize=4096)
def _card_dirt(card_id: int) -> int:
    return int(POOL._card_proper(card_id).get("proper_ground_dirt") or 0)


def _dirt_ok(m) -> bool:
    try:
        return _card_dirt(int(m["card"])) >= DIRT_MIN
    except (KeyError, TypeError, ValueError):
        return False


@functools.lru_cache(maxsize=1)
def _db() -> tuple:
    out = []
    try:
        from .. import opponent_sources as OS
        for hid in (1, 2):
            for t in OS.teams(f"loh:{hid}"):
                    for i, m in enumerate(t["members"]):
                        out.append((("loh", hid, t["id"]), i, int(m.get("score") or 0), int(m.get("chara") or 0),
                                    _dirt_ok(m)))
    except Exception:
        log.exception("team_building: LoH pool")
    try:
        snap = POOL.resolve_snapshot(0, "auto")
        if snap:
            for t in POOL.teams(snap):
                for i, m in enumerate(t["members"]):
                    out.append((("cm", snap, t["id"]), i, int(m.get("score") or 0), int(m.get("chara") or 0),
                                _dirt_ok(m)))
    except Exception:
        log.exception("team_building: CM team pool")
    return tuple(sorted(out, key=lambda x: x[2]))


def _db_team(src):
    if src[0] == "loh":
        from .. import opponent_sources as OS
        return OS.team(f"loh:{src[1]}", src[2])
    return POOL.team(src[1], src[2])


def _db_member(src, idx, member_id, ri) -> dict:
    t = _db_team(src)
    c = POOL.member_chara(t, idx, ri)
    c["_db"] = [list(src), idx]
    c["trainer_name"] = POOL.trainer_name(t)
    return _tc_member(c, member_id)


def _candidates(target: float, rng, used_chara: set, want_tc: bool, dirt: bool = False) -> list:
    lo, hi = target * 0.85, target * 1.15
    db = [("db", (x[0], x[1]), x[2]) for x in _db()
          if lo <= x[2] <= hi and x[3] not in used_chara and (x[4] or not dirt)]
    want_tc = want_tc or dirt
    if not db and want_tc:
        pool = [x for x in _db() if x[3] not in used_chara and (x[4] or not dirt)]
        if pool:
            near = sorted(pool, key=lambda x: abs(x[2] - target))[:40]
            db = [("db", (b[0], b[1]), b[2]) for b in near]
    if want_tc:
        return db
    npc = []
    for rid, row in _npc_rows().items():
        ch = row["chara_id"] if row["chara_id"] > 1 else -rid
        if ch in used_chara:
            continue
        sc = rank_score_of(daily_races._npc_chara(row))
        if lo <= sc <= hi:
            npc.append(("npc", rid, sc))
    out = db + npc
    if not out:
        rows = [(rid, rank_score_of(daily_races._npc_chara(r))) for rid, r in _npc_rows().items()]
        rid, sc = min(rows, key=lambda x: abs(x[1] - target))
        out = [("npc", rid, sc)]
    return out


def _assemble(target: int, rng, ri) -> list[dict]:
    members, used, total = [], set(), 0
    for slot in range(1, TEAM_SIZE + 1):
        per = max(1.0, (target - total) / (TEAM_SIZE - slot + 1))
        kind, ref, sc = rng.choice(_candidates(per, rng, used, slot == CAPTAIN, slot in DIRT_SLOTS))
        if kind == "db":
            (src, idx) = ref
            m = _db_member(src, idx, slot, ri)
        else:
            m = _npc_member(ref, slot)
        used.add(m["chara_id"] if m["chara_id"] > 1 else -(m.get("npc_id") or 0))
        members.append(m)
        total += m["rank_score"]
    return members


def _real_teams(vid, ev) -> list:
    if config.get("team_building_real_players") is False:
        return []
    out = []
    for other, st in (state_store.all_states_for_key(KEY) or {}).items():
        if str(other) == str(vid) or not isinstance(st, dict):
            continue
        if st.get("event_id") != ev["team_building_event_id"] or not st.get("team"):
            continue
        out.append((other, st))
    return out


LEAGUE_SIZE = 48
LEAGUE_TIERS = 6
LEAGUE_WINS = 10
LEAGUE_WARMUP = 168


@functools.lru_cache(maxsize=4)
def _league_roster(eid: int) -> tuple:
    rng = random.Random(f"tbleague:{eid}")
    scores = sorted(x[2] for x in _db()) or [8_000, 12_000]
    top = scores[-1]
    start = 3_000
    out = []
    for i in range(LEAGUE_SIZE):
        u = min(1.0, max(0.0, (i + rng.random()) / LEAGUE_SIZE))
        ceil = int(start + (top - start) * u ** 1.3)
        tiers = [int(TEAM_SIZE * (start + (ceil - start) * k / (LEAGUE_TIERS - 1)))
                 for k in range(LEAGUE_TIERS)]
        out.append({"idx": i, "seed": rng.getrandbits(31), "tiers": tiers})
    return tuple(out)


_LEAGUE: dict = {}


def _league_step(eid: int, hour: int, tiers: list, wins: list) -> None:
    roster = _league_roster(eid)
    rng = random.Random(f"tbleague:{eid}:{hour}")
    score = [r["tiers"][t] for r, t in zip(roster, tiers)]
    order = sorted(range(len(roster)), key=lambda i: (score[i], rng.random()))
    for a, b in zip(order[::2], order[1::2]):
        pa = 1 / (1 + 10 ** ((score[b] - score[a]) / max(1, 0.06 * score[b])))
        w = a if rng.random() < pa else b
        wins[w] += 1
        if wins[w] >= LEAGUE_WINS and tiers[w] < LEAGUE_TIERS - 1:
            tiers[w], wins[w] = tiers[w] + 1, 0


def _league_at(eid: int, hours: int) -> tuple:
    hist = _LEAGUE.setdefault(eid, [([0] * LEAGUE_SIZE, [0] * LEAGUE_SIZE)])
    while len(hist) <= hours:
        tiers, wins = list(hist[-1][0]), list(hist[-1][1])
        _league_step(eid, len(hist), tiers, wins)
        hist.append((tiers, wins))
    return tuple(hist[hours][0])


def _league_hours(st, ev) -> int:
    now = _now()
    t0 = ev["start_date"] if ev.get("start_date") and ev["start_date"] <= now else st.setdefault("league_t0", now)
    return LEAGUE_WARMUP + max(0, (now - t0) // 3600)


@functools.lru_cache(maxsize=256)
def _league_team(eid: int, idx: int, tier: int, ri: int) -> tuple:
    r = _league_roster(eid)[idx]
    rng = random.Random(f"{r['seed']}:{tier}")
    return tuple(_assemble(r["tiers"][tier], rng, ri))


def _league(st, ev, ri) -> list[dict]:
    eid = ev["team_building_event_id"]
    tiers = _league_at(eid, _league_hours(st, ev))
    out = []
    for r, t in zip(_league_roster(eid), tiers):
        members = copy.deepcopy(list(_league_team(eid, r["idx"], t, ri)))
        cap = members[0]["chara"]
        out.append({"viewer_id": cap.get("viewer_id") or 0, "user_name": cap.get("trainer_name") or "Trainer",
                    "honor": 0, "team_name": cap.get("trainer_name") or "Team",
                    "score": _team_score(members, None), "members": members, "real": False})
    return out


def _offers(vid, st, ev, rng) -> list[dict]:
    own = max(1, _team_score(st["team"], st))
    ri = _rotation(st)[0]["race_instance_id"]
    real = _real_teams(vid, ev)
    rng.shuffle(real)
    taken, offers, league = set(), [], None
    for n, shift in enumerate(OFFER_SHIFTS):
        target = int(own * shift)
        st["seq"] += 1
        tid = st["seq"]
        pick = next(((o, s) for o, s in real if o not in taken
                     and abs(_team_score(s["team"], s) - target) <= target * BAND), None)
        if pick:
            o, s = pick
            taken.add(o)
            head = _head(o)
            members = copy.deepcopy(s["team"])
            for m in members:
                m["_owner"] = str(o)
            offers.append({"team_id": tid, "viewer_id": social._wire_id(o), "user_name": head["name"],
                           "honor": head["honor_id"], "team_name": s.get("team_name") or head["name"],
                           "score": _team_score(s["team"], s), "members": members, "real": True})
            continue
        if league is None:
            league = _league(st, ev, ri)
        pool = [o for o in league if id(o) not in taken] or league
        o = rng.choice(sorted(pool, key=lambda x: abs(x["score"] - target))[:6])
        taken.add(id(o))
        offers.append(dict(o, team_id=tid))
    coll = _collection(ev["team_building_event_id"])
    offers.sort(key=lambda o: -sum(1 for m in o["members"]
                                   if m["chara_id"] in coll and str(m["chara_id"]) not in st["collection"]))
    return offers


def _offer_wire(o) -> dict:
    return _team_info(o["team_id"], o["viewer_id"], o["user_name"], o["honor"], o["team_name"],
                      o["score"], o["members"])


def _scout_points(st, opp_score: int, win: bool) -> tuple[int, int]:
    basic = WIN_BASIC(team_rank(opp_score))
    if not win:
        basic = basic * LOSS_RATE // 100
    streak = streak_pct(st["streak"] + 1) if win else 0
    return basic, basic * (100 + roster_pct(len(st["collection"])) + streak) // 100


def _rotation(st) -> list[dict]:
    out, base = [], st["total_race"] // SWITCH_EVERY
    insts = _race_instances()
    for k in range(3):
        rng = random.Random(f"tb:{st['rot_seed']}:{base + k}")
        ri = rng.choice(insts)
        c = race_conditions.roll(ri, rng)
        out.append({"order": k + 1, "race_instance_id": ri, "season": c["season"], "weather": c["weather"],
                    "ground_condition": c["ground_condition"]})
    return out


def _rotation_info(st) -> dict:
    return {"current_race_num": st["total_race"] % SWITCH_EVERY, "race_condition_array": _rotation(st)}


def _buffs(st, ev) -> dict:
    keys = {"all": [0] * 5, "captain": [0] * 5, "member": [0] * 5}
    coll = _collection(ev["team_building_event_id"])
    for ch in st["collection"]:
        row = coll.get(int(ch))
        if not row:
            continue
        for i in (1, 2):
            cond, bt, val = row[f"condition_type_{i}"], row[f"bonus_type_{i}"], row[f"bonus_value_{i}"]
            if cond and 1 <= bt <= 5:
                keys[{1: "all", 2: "captain", 3: "member"}.get(cond, "all")][bt - 1] += val
    return keys


_STAT_KEYS = ("speed", "stamina", "pow", "guts", "wiz")


def _race_chara(m, add=None, pct=0, skills=()) -> dict:
    c = copy.deepcopy(m["chara"])
    if "power" in c and "pow" not in c:
        c["pow"] = c["power"]
    for i, k in enumerate(_STAT_KEYS):
        v = (c.get(k) or 0) + (add[i] if add else 0)
        c[k] = int(v * (100 + pct) / 100)
        if k == "pow":
            c["power"] = c[k]
    c["running_style"] = m["running_style"]
    c["motivation"] = 3
    if skills:
        c["skill_array"] = list(c.get("skill_array") or []) + [{"skill_id": s, "level": 1} for s in skills]
    return c


def _field(vid, st, ev, offer, pct) -> list[dict]:
    buffs = _buffs(st, ev)
    skills = [s["skill_id"] for s in _sets(ev["team_building_event_id"]) if s["chara_group_id"] in st["groups"]]
    head = _head(vid)
    out = []
    for m in st["team"]:
        add = [a + b for a, b in zip(buffs["all"], buffs["captain" if m["member_id"] == CAPTAIN else "member"])]
        c = _race_chara(m, add, pct, skills)
        if m["kind"] == "tc":
            c.update(viewer_id=social._wire_id(vid), trainer_name=head["name"])
        c.update(team_id=1, team_member_id=m["member_id"])
        out.append(c)
    for m in offer["members"]:
        c = _race_chara(m)
        c.update(team_id=2, team_member_id=m["member_id"])
        out.append(c)
    return out


def _simulate(field, cond, seed, gates) -> dict:
    res = race_simulator.simulate_race(
        field[0], field[TEAM_SIZE:], cond["race_instance_id"], ground_condition=cond["ground_condition"],
        weather=cond["weather"], season=cond["season"], seed=seed, entry_num=len(field),
        gate_assignment=gates, extra_player_charas=field[1:TEAM_SIZE], race_context={"race_type": RACE_TYPE})
    if res is None:
        raise RuntimeError(f"team_building: race instance {cond['race_instance_id']} could not be simulated")
    return res


def _finish(res, rows) -> list[int]:
    from . import heroes
    dec = heroes._decoded(res)
    return [dec[r["frame_order"] - 1].finish_order + 1 for r in rows]


def _winner_is_own(order) -> bool:
    return min(order[:TEAM_SIZE]) == 1


def _run(vid, st, ev, race) -> None:
    offer, cond = race["offer"], race["cond"]
    n = race["continue_num"]
    pct, sims = 0, 1
    if n:
        row = M.query_one("SELECT * FROM team_building_retry_bonus WHERE retry_min<=? AND retry_max>=?", (n, n))
        if row:
            pct, sims = row["bonus_rate"], max(1, row["resimulate_num"])
    field = _field(vid, st, ev, offer, pct)
    rng = random.Random(race["seed"] + 7919 * n)
    best = None
    for _ in range(sims):
        res = _simulate(field, cond, rng.getrandbits(31), race["gates"])
        rows, _g = practice_race.race_horse_rows(res)
        order = _finish(res, rows)
        if best is None or (_winner_is_own(order) and not _winner_is_own(best[2])):
            best = (res, rows, order)
        if _winner_is_own(best[2]):
            break
    res, rows, order = best
    for h, row in zip(field, rows):
        row.update(team_id=h["team_id"], team_member_id=h["team_member_id"], motivation=3)
        if h.get("mob_id") and h.get("race_dress_id"):
            row["race_dress_id"] = h["race_dress_id"]
    race.update(win=_winner_is_own(order), order=order, scenario=res["race_scenario"],
                seed_used=res["seed"], rows=rows)


def _race_result_info(race) -> dict:
    c = race["cond"]
    return {"race_instance_id": c["race_instance_id"], "race_horse_data_array": copy.deepcopy(race["rows"]),
            "season": c["season"], "weather": c["weather"], "ground_condition": c["ground_condition"],
            "random_seed": race.get("seed_used") or 0, "race_scenario": race.get("scenario")}


def _grant(full, cat, iid, num, summary, out) -> None:
    daily_races._grant(full, cat, iid, num, summary)
    out.append({"item_type": cat, "item_id": iid, "item_num": num})


def _rank_rewards(full, st, summary) -> list:
    out = []
    reached = team_rank(st["best_score"])
    for r in _ranks():
        if r["team_rank"] <= reached and r["team_rank"] not in st["ranks_got"]:
            st["ranks_got"].append(r["team_rank"])
            for g in _rows("SELECT * FROM team_building_rank_reward_group WHERE reward_group_id=?",
                           (r["reward_group_id"],)):
                _grant(full, g["item_category"], g["item_id"], g["item_num"], summary, out)
    return out


def _rescore(full, st, summary) -> tuple[int, list]:
    score = _team_score(st["team"], st)
    st["best_score"] = max(st["best_score"], score)
    return score, _rank_rewards(full, st, summary)


def _items_wire(full, ev) -> list:
    return [{"item_id": i, "number": shop._item_count(full, i)} for i in (_ticket_id(ev), _pt_id(ev))]


def load_info(viewer_id, full) -> dict | None:
    ev = active()
    if not ev:
        return None
    st = _state(full, ev)
    cap = next((m for m in st["team"] or [] if m["member_id"] == CAPTAIN), None)
    return {"team_building_id": ev["team_building_event_id"], "state": _client_state(st),
            "captain_id": (cap["chara"].get("trained_chara_id") or 0) if cap else 0}


def _client_state(st) -> int:
    if st["team"] is None:
        return ST_FIRST
    return st["state"]


def _mission_badge(vid, full) -> int:
    from . import missions
    return missions.badge_counts(vid, full)["team_building_mission_num"]


def _index_data(vid, full, st, ev) -> dict:
    coll_ids = set(_collection(ev["team_building_event_id"]))
    new_groups = list(st["new_groups"])
    return {
        "own_team_info": _own_info(vid, st),
        "race_rotation_info": _rotation_info(st),
        "item_info_array": _items_wire(full, ev),
        "total_race_count": st["total_race"], "total_win_count": st["total_win"],
        "straight_win_count": st["streak"], "best_team_score": st["best_score"],
        "state": _client_state(st), "purchase_ticket_num": st["bought"],
        "collection_chara_array": [{"chara_id": int(c), "used_flag": 1 if v.get("used") else 0,
                                    "new_flag": 1 if v.get("new") else 0}
                                   for c, v in st["collection"].items() if int(c) in coll_ids],
        "collection_group_id_array": list(st["groups"]),
        "new_collection_group_id_array": new_groups,
        "is_season_change": 0, "mission_badge_num": _mission_badge(vid, full),
        "daily_continue_num": st["daily_continue"], "scout_ranking_state": 1,
        "special_race_info": {"state": 0, "watch_state": 0, "fixed_count": 0},
    }


def _summary():
    return daily_races._empty_summary()


@registry.endpoint("team_building/index")
def handle_index(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    data = _index_data(_vid(payload), full, st, ev)
    _save(payload, full)
    return _ok(data)


def _veteran(vid, tcid):
    tc = daily_races._find_veteran(vid, tcid)
    return copy.deepcopy(tc) if tc else None


@registry.endpoint("team_building/make_team")
def handle_make_team(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    vid = _vid(payload)
    if st["team"]:
        return refuse()
    tc = _veteran(vid, payload.get("trained_chara_id"))
    if tc is None:
        return refuse()
    npcs = sorted(r for r, row in _npc_rows().items() if row["npc_group_id"] == START_NPC_GROUP)
    st["team"] = [_tc_member(tc, CAPTAIN, payload.get("running_style"))] + \
        [_npc_member(r, i + 2) for i, r in enumerate(npcs[:TEAM_SIZE - 1])]
    st["team_name"] = (payload.get("name") or "")[:16]
    st["state"], st["first"] = ST_NONE, False
    summary, part = _summary(), []
    shop._add_item(full, _pt_id(ev), ev["start_add_scout_point"])
    part.append({"item_type": 1, "item_id": _pt_id(ev), "item_num": ev["start_add_scout_point"]})
    score, rewards = _rescore(full, st, summary)
    _save(payload, full)
    return _ok({"reward_array": rewards, "participate_reward_array": part, "reward_summary_info": summary,
                "team_score": score, "state": st["state"]})


@registry.endpoint("team_building/change_captain")
def handle_change_captain(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    if not st["team"] or st["state"] in (ST_SELECTED, ST_RUN):
        return refuse()
    tc = _veteran(_vid(payload), payload.get("trained_chara_id"))
    if tc is None:
        return refuse()
    st["team"][0] = _tc_member(tc, CAPTAIN, payload.get("running_style"))
    summary = _summary()
    score, rewards = _rescore(full, st, summary)
    _save(payload, full)
    return _ok({"reward_array": rewards, "reward_summary_info": summary, "team_score": score})


@registry.endpoint("team_building/change_running_style")
def handle_change_running_style(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    if not st["team"]:
        return refuse()
    by_id = {m["member_id"]: m for m in st["team"]}
    for r in payload.get("running_style_info_array") or []:
        m = by_id.get(r.get("member_id"))
        if m and 1 <= (r.get("running_style") or 0) <= 4:
            m["running_style"] = r["running_style"]
    _save(payload, full)
    return _ok({})


@registry.endpoint("team_building/change_team_name")
def handle_change_team_name(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    st["team_name"] = (payload.get("name") or "")[:16]
    _save(payload, full)
    return _ok({"name": st["team_name"]})


@registry.endpoint("team_building/opponent_array")
def handle_opponent_array(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    if not st["team"]:
        return refuse()
    vid = _vid(payload)
    st["offers"] = _offers(vid, st, ev, random.Random())
    _save(payload, full)
    return _ok({"opponent_array": [_offer_wire(o) for o in st["offers"]],
                "expectation_scout_point_array": [
                    {"team_id": o["team_id"], "expectation_scout_point": _scout_points(st, o["score"], True)[1]}
                    for o in st["offers"]]})


@registry.endpoint("team_building/opponent_array_special")
def handle_opponent_array_special(payload: dict) -> dict:
    return _ok({"opponent_array": [], "expectation_scout_point_array": []})


@registry.endpoint("team_building/race_entry")
def handle_race_entry(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    offer = next((o for o in st["offers"] or [] if o["team_id"] == payload.get("team_id")), None)
    if offer is None or not st["team"]:
        return refuse()
    if shop._item_count(full, _ticket_id(ev)) < 1:
        return refuse(ResultCode.TEAM_BUILDING_TICKET_AUTO_RECOVERY)
    shop._add_item(full, _ticket_id(ev), -1)
    gates = list(range(2 * TEAM_SIZE))
    rng = random.Random()
    rng.shuffle(gates)
    race = {"offer": offer, "cond": _rotation(st)[0], "seed": rng.getrandbits(31), "gates": gates,
            "continue_num": 0}
    _run(_vid(payload), st, ev, race)
    st["race"], st["state"] = race, ST_SELECTED
    st["runs"].append(_now())
    _save(payload, full)
    return _ok({"race_horse_data_array": copy.deepcopy(race["rows"]), "item_info_array": _items_wire(full, ev),
                "random_seed": race["seed_used"], "state": st["state"]})


@registry.endpoint("team_building/race_start")
def handle_race_start(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    race = st.get("race")
    if not race:
        return refuse()
    st["state"] = ST_SKIPPED if payload.get("is_short") else ST_RUN
    _save(payload, full)
    return _ok({"race_scenario": race["scenario"], "state": st["state"]})


@registry.endpoint("team_building/race_continue")
def handle_race_continue(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    race = st.get("race")
    if not race or race.get("win"):
        return refuse()
    if st["daily_continue"] < config.get("team_building_free_continue_num", 5):
        st["daily_continue"] += 1
    elif shop._item_count(full, CLOCK_ITEM) >= 1:
        shop._add_item(full, CLOCK_ITEM, -1)
    else:
        return refuse(ResultCode.TEAM_BUILDING_FREE_CONTINUE_AUTO_RECOVERY)
    race["continue_num"] += 1
    _run(_vid(payload), st, ev, race)
    st["state"] = ST_SELECTED
    _save(payload, full)
    return _ok({"race_horse_data_array": copy.deepcopy(race["rows"]), "random_seed": race["seed_used"],
                "state": st["state"], "item_info_array": _items_wire(full, ev) +
                [{"item_id": CLOCK_ITEM, "number": shop._item_count(full, CLOCK_ITEM)}],
                "continue_num": race["continue_num"], "daily_continue_num": st["daily_continue"]})


def _register(st, ev, race) -> tuple[list, list]:
    coll = _collection(ev["team_building_event_id"])
    add_c, add_g = [], []
    for m in st["team"]:
        ch = m["chara_id"]
        if ch in coll and str(ch) not in st["collection"]:
            st["collection"][str(ch)] = {"new": True, "used": False}
            add_c.append(ch)
    have = {int(c) for c in st["collection"]}
    for s in _sets(ev["team_building_event_id"]):
        if s["chara_group_id"] not in st["groups"] and s["charas"] and set(s["charas"]) <= have:
            st["groups"].append(s["chara_group_id"])
            st["new_groups"].append(s["chara_group_id"])
            add_g.append(s["chara_group_id"])
    return add_c, add_g


@registry.endpoint("team_building/race_result")
def handle_race_result(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    race = st.get("race")
    if not race or race.get("done"):
        return refuse()
    win = race["win"]
    summary, basic_r, win_r = _summary(), [], []
    basic, total = _scout_points(st, race["offer"]["score"], win)
    shop._add_item(full, _pt_id(ev), total)
    basic_r.append({"item_type": 1, "item_id": _pt_id(ev), "item_num": total})
    st["total_race"] += 1
    add_c, add_g = [], []
    if win:
        st["total_win"] += 1
        st["streak"] += 1
        add_c, add_g = _register(st, ev, race)
        for r in _rows("SELECT * FROM team_building_win_reward"):
            if r["win_loop_num"] and st["total_win"] % r["win_loop_num"] == 0:
                _grant(full, r["item_category"], r["item_id"], r["item_num"], summary, win_r)
    else:
        st["streak"] = 0
    score, rank_r = _rescore(full, st, summary)
    race["done"] = True
    st["state"] = ST_WIN_SHOWN if win else ST_NONE
    if not win:
        st["race"] = None
    _save(payload, full)
    return _ok({"race_rotation_info": _rotation_info(st), "team_score": score,
                "scout_point_info": {"item_id": _pt_id(ev), "basic_num": basic, "total_num": total,
                                     "straight_win_count": st["streak"]},
                "basic_reward_array": basic_r, "win_reward_array": win_r, "team_score_reward_array": rank_r,
                "reward_summary_info": summary,
                "add_collection_chara_array": [{"chara_id": c, "used_flag": 0, "new_flag": 1} for c in add_c],
                "add_collection_group_id_array": add_g, "state": st["state"]})


@registry.endpoint("team_building/scout")
def handle_scout(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    race = st.get("race")
    if st["state"] != ST_WIN_SHOWN or not race:
        return refuse()
    summary, rewards = _summary(), []
    own_id, opp_id = payload.get("own_team_member_id") or 0, payload.get("opponent_team_member_id") or 0
    if own_id:
        if not 2 <= own_id <= TEAM_SIZE:
            return refuse()
        src = next((m for m in race["offer"]["members"] if m["member_id"] == opp_id), None)
        if src is None:
            return refuse()
        cost = scout_cost(src["rank"], st["best_score"])
        if shop._item_count(full, _pt_id(ev)) < cost:
            return refuse()
        shop._add_item(full, _pt_id(ev), -cost)
        new = copy.deepcopy(src)
        new["member_id"] = own_id
        new["running_style"] = payload.get("running_style") or new["running_style"]
        if new["kind"] == "tc":
            st["seq"] += 1
            new["chara"]["trained_chara_id"] = SCOUT_TCID_BASE + st["seq"]
            st["scouted_from"].append([_now(), new.pop("_owner", None), src["chara"].get("trained_chara_id"),
                                       src["chara_id"]])
        st["team"][own_id - 1] = new
        st["scouts"].append(_now())
    score, rewards = _rescore(full, st, summary)
    st["state"], st["race"] = ST_NONE, None
    _save(payload, full)
    return _ok({"reward_array": rewards, "reward_summary_info": summary, "team_score": score,
                "item_data_array": _items_wire(full, ev), "state": st["state"]})


@registry.endpoint("team_building/resume")
def handle_resume(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    race = st.get("race")
    _save(payload, full)
    if not race:
        return _ok({"opponent_team_info": None, "race_result_info": None, "state": _client_state(st),
                    "continue_num": 0})
    return _ok({"opponent_team_info": _offer_wire(race["offer"]), "race_result_info": _race_result_info(race),
                "state": st["state"], "continue_num": race["continue_num"]})


@registry.endpoint("team_building/save_collection")
def handle_save_collection(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    if payload.get("check_chara_flag"):
        for v in st["collection"].values():
            v["new"] = False
    if payload.get("check_skill_flag"):
        st["new_groups"] = []
    _save(payload, full)
    return _ok({})


@registry.endpoint("team_building/recovery_ticket")
def handle_recovery_ticket(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    n = int(payload.get("count") or 1)
    limit = config.get("team_building_purchase_limit_num", 5)
    cost = config.get("team_building_coin_pay_cost", 10) * n
    if n < 1 or st["bought"] + n > limit or shop._item_count(full, _ticket_id(ev)) + n > ev["stamina_max"]:
        return refuse()
    coin = shop.spend_carats(full, cost)
    if coin is None:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)
    shop._add_item(full, _ticket_id(ev), n)
    st["bought"] += n
    summary = _summary()
    _save(payload, full)
    return _ok({"coin_info": coin, "reward_summary_info": summary, "purchase_ticket_num": st["bought"]})


@registry.endpoint("team_building/use_recovery_item")
def handle_use_recovery_item(payload: dict) -> dict:
    ev, full, st, bad = _need(payload)
    if bad:
        return bad
    iid, n = payload.get("item_id"), int(payload.get("item_num") or 1)
    if iid not in JELLY_ITEMS or n < 1 or shop._item_count(full, iid) < n \
            or shop._item_count(full, _ticket_id(ev)) + n > ev["stamina_max"]:
        return refuse()
    shop._add_item(full, iid, -n)
    shop._add_item(full, _ticket_id(ev), n)
    _save(payload, full)
    return _ok({"item_info_array": _items_wire(full, ev) + [{"item_id": iid, "number": shop._item_count(full, iid)}],
                "reward_summary_info": _summary(), "purchase_ticket_num": st["bought"]})


@registry.endpoint("team_building/get_ranking")
def handle_get_ranking(payload: dict) -> dict:
    ev, full, st, bad = _need(payload, racing=False)
    if bad:
        return bad
    counts = {}
    for other, s in (state_store.all_states_for_key(KEY) or {}).items():
        if not isinstance(s, dict) or s.get("event_id") != ev["team_building_event_id"]:
            continue
        for _t, owner, tcid, _ch in s.get("scouted_from") or []:
            if owner and tcid:
                counts[(owner, tcid)] = counts.get((owner, tcid), 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:100]
    out = []
    for n, ((owner, tcid), cnt) in enumerate(top, start=1):
        tc = daily_races._find_veteran(owner, tcid)
        if tc:
            out.append({"viewer_id": social._wire_id(owner), "name": _head(owner)["name"], "rank": n,
                        "count": cnt, "trained_chara": tc})
    return _ok({"scout_ranking_user_list": out})


def counts_in(full_state: dict, kind: str, lo: int, hi: int) -> int:
    st = full_state.get(KEY) or {}
    return sum(1 for t in st.get(kind) or [] if lo <= t <= hi)
