from __future__ import annotations

import base64
import copy
import gzip
import json
import logging
import random
import time
from pathlib import Path

from .. import champions_pool as POOL, green_skills, master_data as M, opponent_sources as OS, patch, race_conditions, social
from .. import state as state_store
from ..result_codes import ResultCode, refuse
from ..simulation import race_simulator
from ..simulation.scenario_format import decode_scenario
from . import champions, practice_race, race_mobs, registry, shop, trained_chara

log = logging.getLogger("uma-server")

STATE_KEY = "heroes_state"
TEAM_SIZE, MOBS, ROUNDS, FIELD = 3, 6, 5, 12
TICKET_BASE = 3000
MAIN_DAYS = 6
NPC_TRAINER, NPC_OPPONENT = 11, 20
RACE_TYPE = 5
ITEM_PARFAIT, ITEM_SUN, ITEM_RAIN, ITEM_INNER, ITEM_OUTER = 116, 117, 118, 119, 120

HORSE_LENGTH_BASHIN = [800, 1600, 3200, 5000, 7500, 10000, 12500, 15000, 17500, 20000, 25000,
                       30000, 35000, 40000, 50000, 60000, 70000, 80000, 90000, 100000]
NECK = 2

MOOD_WEIGHTS = {2: 586, 3: 495, 4: 440, 5: 354}
MOB_SCALE, MOB_JITTER = 3.47, 0.04
DROP_IF_UNPLACED = 0.6
DROPS = [((91, 59, 200), 36), ((91, 59, 600), 36), ((91, 59, 400), 28), ((91, 59, 800), 26),
         ((20, 32, 1), 4), ((34, 95, 1), 3), ((90, 43, 10), 2), ((150, 116, 1), 1),
         ((150, 117, 1), 1), ((150, 118, 1), 1), ((150, 120, 1), 1), ((34, 95, 2), 1)]
BANDS = {1: (0, 45), 2: (20, 65), 3: (40, 85), 4: (60, 100)}
FINAL_BAND = (85, 100)
CAT_CARAT, CAT_HONOR = 90, 55

FINALISTS = Path(__file__).resolve().parents[2] / "data" / "heroes_finalists.json"


def _ok(data=None) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data if data is not None else {}}


def _now() -> int:
    return patch._servertime()


def _vid(payload) -> str:
    return str(payload.get("viewer_id"))


def active(now: int | None = None) -> dict | None:
    now = _now() if now is None else now
    if not M.has_table("heroes_data"):
        return None
    r = M.query_one("SELECT * FROM heroes_data WHERE start_date<=? AND end_date>=? ORDER BY heroes_id DESC LIMIT 1",
                    (now, now))
    return dict(r) if r else None


def _schedule(hid) -> list[dict]:
    return [dict(r) for r in M.query("SELECT * FROM heroes_stage_schedule WHERE heroes_id=? ORDER BY start_date",
                                     (hid,))]


def stage_at(hid, now: int | None = None) -> tuple[int, int]:
    now = _now() if now is None else now
    cur = (0, 0)
    for r in _schedule(hid):
        if r["start_date"] <= now:
            cur = (r["stage"], r["stage_step"])
    return cur


def _main_window(hid) -> tuple[int, int]:
    r = M.query_one("SELECT start_date, end_date FROM heroes_stage_schedule WHERE heroes_id=? AND stage=1 "
                    "AND stage_step=1", (hid,))
    return (r["start_date"], r["end_date"]) if r else (0, 0)


def _course(hid, stage) -> tuple[int, int]:
    r = M.query_one("SELECT race_instance_id, season FROM heroes_race_condition WHERE heroes_id=? AND stage=?",
                    (hid, stage))
    return (r["race_instance_id"], r["season"]) if r else (0, 1)


def _ranks() -> list[dict]:
    return [dict(r) for r in M.query("SELECT * FROM heroes_league_rank ORDER BY league_min_value")]


def _rank_row(score) -> dict:
    rows = _ranks()
    return next((r for r in reversed(rows) if score >= r["league_min_value"]), rows[0])


def perfect_score(hd) -> int:
    free = min((r["league_min_value"] for r in _ranks() if r["ticket_num"]), default=0)
    best = 100 + 60 + 40
    return free + hd["ticket_add"] * MAIN_DAYS * best * ROUNDS


def _new(hid) -> dict:
    return {"heroes_id": hid, "team": [], "team_name": "", "edit_rewarded": False, "option_deny": 0,
            "score": 0, "gauge": 0, "skill": 0, "resume": 0, "plan": None, "set": None, "seq": 0,
            "stats": {}, "chara_score": {}, "ranks_got": [], "honor_chara": 0, "ticket_day": -1,
            "seen": [], "final": None}


def _state(full, hd) -> dict:
    st = full.get(STATE_KEY)
    if not isinstance(st, dict) or st.get("heroes_id") != hd["heroes_id"]:
        st = _new(hd["heroes_id"])
        full[STATE_KEY] = st
    _refill(full, st, hd)
    return st


def _ticket(hid) -> int:
    return TICKET_BASE + hid


def _refill(full, st, hd) -> None:
    a, b = _main_window(hd["heroes_id"])
    now = _now()
    if not a or now < a:
        return
    day_len = max(1, (b - a + 1) / MAIN_DAYS)
    day = min(MAIN_DAYS - 1, int((min(now, b) - a) // day_len))
    tid = _ticket(hd["heroes_id"])
    while st["ticket_day"] < day:
        st["ticket_day"] += 1
        have = shop._item_count(full, tid)
        shop._add_item(full, tid, max(0, min(hd["ticket_max"], have + hd["ticket_add"]) - have))


def _need(payload):
    hd = active()
    if not hd:
        return None, None, None, refuse(ResultCode.PARAM_ERROR)
    full = state_store.get_state(_vid(payload)) or {}
    return hd, full, _state(full, hd), None


def _save(payload, full) -> None:
    state_store.save_state(_vid(payload), full)


def _head(vid) -> dict:
    from . import room_match
    return room_match._user_head(vid)


def _honor(h) -> dict:
    return {"honor_id": (h or {}).get("honor_id", 0)}


def _stats_wire(st, tcid) -> dict:
    s = st["stats"].get(str(tcid)) or {"hist": [], "c": [0, 0, 0, 0]}
    return {"trained_chara_id": int(tcid), "race_history_array": copy.deepcopy(s["hist"][-5:][::-1]),
            "rank_count_1": s["c"][0], "rank_count_2": s["c"][1], "rank_count_3": s["c"][2],
            "rank_count_unplaced": s["c"][3]}


def _empty_stats(tcid) -> dict:
    return {"trained_chara_id": tcid, "race_history_array": [], "rank_count_1": 0, "rank_count_2": 0,
            "rank_count_3": 0, "rank_count_unplaced": 0}


def _own_team(vid, st) -> dict:
    head = _head(vid)
    team = st["team"]
    return {"team_id": 1, "viewer_id": social._wire_id(vid), "user_name": head["name"],
            "honor_data": _honor(head["honor_data"]), "team_name": st["team_name"] or head["name"],
            "league_score": st["score"],
            "member_array": [{"trained_chara": _tc_wire(c), "team_member_id": c["team_member_id"]} for c in team]
            if team else None,
            "chara_race_stats_array": [_stats_wire(st, c["trained_chara_id"]) for c in team]}


def _tc_wire(c) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_") and k != "team_member_id"}


def _opp_team(t, idx_team, ri, season, score) -> dict:
    members = _opp_members(t, ri, season)
    return {"team_id": idx_team, "viewer_id": POOL.trainer_viewer_id(t["id"]), "user_name": POOL.trainer_name(t),
            "honor_data": {"honor_id": 0}, "team_name": POOL.trainer_name(t), "league_score": score,
            "member_array": [{"trained_chara": m, "team_member_id": i + 1} for i, m in enumerate(members)],
            "chara_race_stats_array": [_empty_stats(m["trained_chara_id"]) for m in members]}


def _opp_members(t, ri, season) -> list[dict]:
    out = []
    for i in range(TEAM_SIZE):
        c = POOL.member_chara(t, i, ri)
        c["skill_array"] = green_skills.adapt(c["skill_array"], ri, season)
        out.append(c)
    return out


def _snapshot(snap, tid):
    if snap == "mix":
        key, _, tid = tid.partition("|")
    else:
        key = f"loh:{snap[3:]}" if snap.startswith("loh") else f"real:{snap}"
    t = OS.team(key, tid)
    if t is None:
        raise RuntimeError(f"opponent team {tid} vanished from {key}")
    return t


def _pick(hid, band, rng, n) -> list[str]:
    teams = OS.pick("loh", band, rng, n, {"heroes_id": hid})
    if not teams:
        raise RuntimeError("LoH opponent mix is empty (tools/opp_ctl.py mix loh)")
    return [f"{t['_pool']}|{t['id']}" for t in teams]


def _mood(rng) -> int:
    return rng.choices(list(MOOD_WEIGHTS), weights=list(MOOD_WEIGHTS.values()))[0]


def _mob_rows(ri) -> list[dict]:
    g = M.query_one("SELECT npc_group_id FROM race_instance WHERE id=?", (ri,))
    return [dict(r) for r in M.query("SELECT * FROM heroes_race_mob_npc WHERE npc_group_id=? ORDER BY id",
                                     ((g["npc_group_id"] if g else 0),))]


def _mob_chara(row, face, jitter, slot) -> dict:
    c = race_mobs._base_mob(slot, face, race_mobs.mob_dress(face))
    for k in ("speed", "stamina", "pow", "guts", "wiz"):
        c[k] = max(1, round(row[k] * MOB_SCALE * jitter[k]))
    for k in daily_keys():
        c[k] = row[k]
    c["running_style"] = race_mobs._running_style_from_aptitudes(row)
    skills = race_mobs._skill_set(row["skill_set_id_1"] or 0) + race_mobs._skill_set(row["skill_set_id_2"] or 0)
    c["skill_array"] = [{"skill_id": s, "level": lv} for s, lv in dict(skills).items()]
    c["rank"] = race_mobs.mob_rank(c)
    return c


def daily_keys():
    return ("proper_distance_short", "proper_distance_mile", "proper_distance_middle", "proper_distance_long",
            "proper_running_style_nige", "proper_running_style_senko", "proper_running_style_sashi",
            "proper_running_style_oikomi", "proper_ground_turf", "proper_ground_dirt")


def _plan(vid, st, hd) -> dict:
    st["seq"] += 1
    seed = random.Random(f"heroes:{hd['heroes_id']}:{vid}:{st['seq']}").getrandbits(31)
    rng = random.Random(seed)
    ri, season = _course(hd["heroes_id"], 1)
    cond = race_conditions.roll(ri, rng, season)
    rtype = _rank_row(st["score"])["league_rank_type"]
    snap = "mix"
    opps = _pick(hd["heroes_id"], BANDS.get(rtype, (0, 100)), rng, ROUNDS)
    mobs = _mob_rows(ri)
    faces = list(race_mobs._room_faces())
    rounds = []
    for n in range(ROUNDS):
        picks = rng.sample(range(len(mobs)), min(MOBS, len(mobs)))
        rounds.append({
            "opp": opps[n % len(opps)],
            "opp_score": max(0, int(st["score"] * rng.uniform(0.85, 1.15)) // 10 * 10),
            "mobs": [{"row": mobs[i]["id"], "face": rng.choice(faces),
                      "jitter": {k: rng.uniform(1 - MOB_JITTER, 1 + MOB_JITTER)
                                 for k in ("speed", "stamina", "pow", "guts", "wiz")}} for i in picks],
            "moods": [_mood(rng) for _ in range(FIELD)],
            "gates": rng.sample(range(FIELD), FIELD),
            "seed": rng.getrandbits(31),
        })
    return {"seed": seed, "snap": snap, "ri": ri, "season": season, "weather": cond["weather"],
            "ground": cond["ground_condition"], "rounds": rounds}


def _field(vid, st, plan, rnd, items, skill) -> list[dict]:
    me = _head(vid)
    out = []
    for i, c in enumerate(st["team"]):
        h = copy.deepcopy(c)
        if skill:
            h["skill_array"] = list(h.get("skill_array") or []) + [{"skill_id": skill, "level": 1}]
        h.update({"viewer_id": social._wire_id(vid), "trainer_name": me["name"], "npc_type": NPC_TRAINER,
                  "team_id": 1, "team_member_id": c["team_member_id"], "_items": list(items)})
        out.append(h)
    t = _snapshot(plan["snap"], rnd["opp"])
    for i, c in enumerate(_opp_members(t, plan["ri"], plan["season"])):
        c.update({"viewer_id": POOL.trainer_viewer_id(t["id"]), "trainer_name": POOL.trainer_name(t),
                  "npc_type": NPC_OPPONENT, "team_id": 2, "team_member_id": i + 1})
        out.append(c)
    rows = {r["id"]: r for r in _mob_rows(plan["ri"])}
    for j, m in enumerate(rnd["mobs"]):
        c = _mob_chara(rows[m["row"]], m["face"], m["jitter"], 1000 + j)
        c.update({"team_id": 0, "team_member_id": 0})
        out.append(c)
    for h, mood in zip(out, rnd["moods"]):
        h["motivation"] = mood
    return out


def _gates(rnd, items) -> tuple[list[int], bool]:
    gates = list(rnd["gates"])
    want = None
    if ITEM_INNER in items:
        want = [0, 1, 2]
    elif ITEM_OUTER in items:
        rng = random.Random(rnd["seed"] ^ 0x5EED)
        want = sorted(rng.sample(range(6, FIELD), TEAM_SIZE))
    if want is None:
        return gates, False
    rest = [g for g in sorted(gates[TEAM_SIZE:], key=lambda g: gates.index(g)) if g not in want]
    free = [g for g in gates[:TEAM_SIZE] if g not in want]
    others = [g if g not in want else None for g in gates[TEAM_SIZE:]]
    pool = iter(free)
    others = [g if g is not None else next(pool) for g in others]
    del rest
    random.Random(rnd["seed"]).shuffle(want)
    return want + others, True


def horse_length(diff_time: float) -> int:
    v = int(diff_time * 15 / 2.5 * 10000)
    return next((i for i, t in enumerate(HORSE_LENGTH_BASHIN) if t >= v), len(HORSE_LENGTH_BASHIN))


@M.context_cache(maxsize=4)
def _scores() -> dict:
    return {r["order"]: r["league_score"] for r in M.query("SELECT * FROM heroes_league_score")}


@M.context_cache(maxsize=4)
def _gauge_rows() -> list:
    return [dict(r) for r in M.query("SELECT * FROM heroes_gauge ORDER BY priority DESC, id")]


def round_gauge(best: int, margin_cat: int) -> int:
    g = 0
    for r in _gauge_rows():
        if r["add_timing"] != 1:
            continue
        t, lo, hi = r["condition_type"], r["condition_value_1"], r["condition_value_2"]
        if t == 1 or (t == 2 and lo <= best <= hi) or (t == 3 and best == lo and margin_cat <= NECK):
            g += r["gauge_up"]
    return g


def set_gauge(bests: list[int]) -> int:
    n = sum(1 for b in bests if b >= 2)
    hits = [r for r in _gauge_rows() if r["add_timing"] == 2 and r["condition_type"] == 4
            and n >= r["condition_value_2"]]
    return max((r["gauge_up"] for r in hits), default=0)


def _race(field, gates, plan, rnd, weather, ground, ri=None, season=None) -> dict:
    ri = ri or plan["ri"]
    result = race_simulator.simulate_race(
        field[0], field[TEAM_SIZE:], ri, ground_condition=ground, weather=weather,
        season=season or plan["season"], seed=rnd["seed"], entry_num=len(field), gate_assignment=gates,
        extra_player_charas=field[1:TEAM_SIZE], race_context={"race_type": RACE_TYPE})
    if result is None:
        raise RuntimeError(f"LoH race instance {ri} could not be simulated")
    return result


def _horse_rows(field, result, flags) -> list[dict]:
    rows, _ = practice_race.race_horse_rows(result)
    for h, row in zip(field, rows):
        row.update({"motivation": h["motivation"], "team_id": h["team_id"], "team_member_id": h["team_member_id"],
                    "team_rank": 0, "item_id_array": list(h.get("_items") or []),
                    "motivation_change_flag": flags.get("mood", 0) if h["team_id"] == 1 else 0,
                    "frame_order_change_flag": flags.get("gate", 0),
                    "scenario_data_array": copy.deepcopy(h.get("scenario_data_array") or []),
                    "single_mode_win_count": h.get("wins") or 0, "fan_count": h.get("fans") or 0})
        if h.get("mob_id"):
            row["race_dress_id"] = h["race_dress_id"]
    return rows


def _decoded(result) -> list:
    return decode_scenario(gzip.decompress(base64.b64decode(result["race_scenario"])))["results"]


def _hidden(row) -> dict:
    keep = {"viewer_id", "owner_viewer_id", "trainer_name", "owner_trainer_name", "card_id", "chara_id",
            "rarity", "frame_order", "race_dress_id", "final_grade", "motivation", "mob_id", "team_id",
            "team_member_id", "team_rank"}
    out = {k: (v if k in keep else None) for k, v in row.items()}
    out.update(skill_array=[], win_saddle_id_array=[], race_result_array=[], item_id_array=[], scenario_data_array=[])
    return out


@registry.endpoint("heroes/index")
def handle_index(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    vid = _vid(payload)
    stage, step = stage_at(hd["heroes_id"])
    first = 0 if st["seen"] else 1
    key = f"{stage}:{step}"
    first_step = 0 if key in st["seen"] else 1
    if first_step:
        st["seen"].append(key)
    tid = _ticket(hd["heroes_id"])
    used = {str(c) for c in st["stats"]}
    data = {
        "heroes_id": hd["heroes_id"], "own_team_info": _own_team(vid, st),
        "own_chara_result_array": [_stats_wire(st, c) for c in sorted(used, key=int)],
        "stage": stage, "stage_step": step,
        "item_info_array": [{"item_id": tid, "number": shop._item_count(full, tid)}] if stage >= 1 else [],
        "stage1_resume_state": st["resume"], "stage1_resume_round_num": (st.get("set") or {}).get("round_num", 1),
        "honor_chara_id": st["honor_chara"], "first_access_flags": first, "first_access_stage_step_flag": first_step,
        "first_access_final_stage1_result_flag": 0, "first_access_final_stage1_ranking_flag": 0,
        "first_access_final_get_honor_flag": 0, "first_access_final_get_nickname_flag": 0,
        "hero_gauge": st["gauge"], "hero_skill_id": st["skill"], "option_deny_final_stage_flag": st["option_deny"],
        "final_info": _final_index(vid, full, st, hd, stage, step) if stage == 2 else None,
    }
    _save(payload, full)
    return _ok(data)


def load_info(viewer_id, full) -> dict | None:
    hd = active()
    if not hd:
        return None
    st = _state(full, hd)
    stage, step = stage_at(hd["heroes_id"])
    ids = [c["trained_chara_id"] for c in st["team"]]
    fin = None
    if stage == 2 and st.get("final"):
        f = st["final"]
        fin = {"first_access_final_stage1_ranking_flag": 0, "next_play_info": _next_play(f),
               "received_reward_group_array": list(f["umaine_groups"]), "race_group_id": f["group"],
               "race_num": f["race"], "team_edit_change_flag": 0}
    return {"heroes_id": hd["heroes_id"], "stage": stage, "stage_step": step, "resume_state": st["resume"],
            "team_trained_chara_id_array": ids, "entry_trained_chara_id_array": ids if stage == 1 else [],
            "league_score": st["score"], "final_load_index_info": fin}


def _grant(vid, full, items) -> dict:
    return champions._grant_items(vid, full, items, False)


def _item(cat, iid, num) -> dict:
    return {"item_type": cat, "item_id": iid, "item_num": num}


@registry.endpoint("heroes/stage1_edit_team")
def handle_edit_team(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    stage, step = stage_at(hd["heroes_id"])
    if not (stage == 0 or (stage == 1 and step == 1)) or st["set"]:
        return refuse(ResultCode.PARAM_ERROR)
    team = champions._snapshot_team(_vid(payload), payload.get("entry_chara_array") or [], None)
    if team is None:
        return refuse(ResultCode.PARAM_ERROR)
    st["team"] = team
    st["plan"] = None
    for c in team:
        c["chara_id"] = c.get("chara_id") or c["card_id"] // 100
        st["stats"].setdefault(str(c["trained_chara_id"]), {"hist": [], "c": [0, 0, 0, 0], "chara": c["chara_id"]})
    items = []
    if not st["edit_rewarded"]:
        st["edit_rewarded"] = True
        items = [_item(r["item_category"], r["item_id"], r["item_num"])
                 for r in M.query("SELECT * FROM heroes_special_reward WHERE condition_type=1 ORDER BY id")]
    summary = _grant(_vid(payload), full, items)
    _save(payload, full)
    return _ok({"team_edit_reward_array": items, "reward_summary_info": summary})


@registry.endpoint("heroes/stage1_edit_team_name")
def handle_edit_team_name(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    st["team_name"] = str(payload.get("name") or "")[:20]
    _save(payload, full)
    return _ok({"name": st["team_name"]})


@registry.endpoint("heroes/stage1_set_option")
def handle_set_option(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    st["option_deny"] = 1 if payload.get("option_deny_final_stage_flag") else 0
    _save(payload, full)
    return _ok({})


def _can_race(hd, st) -> bool:
    return stage_at(hd["heroes_id"]) == (1, 1) and len(st["team"]) == TEAM_SIZE


def _opponent_info(vid, full, st, hd) -> dict:
    plan = st["plan"]
    rounds = []
    for n, rnd in enumerate(plan["rounds"], start=1):
        field = _field(vid, st, plan, rnd, (), 0)
        rows = []
        for i, h in enumerate(field):
            row = practice_race._build_race_horse_entry(h, frame_order=rnd["gates"][i] + 1,
                                                        final_grade=h.get("rank") or race_mobs.mob_rank(h),
                                                        popularity=1, popularity_mark_rank_array=[1, 1, 1])
            row.update({"motivation": h["motivation"], "team_id": h["team_id"],
                        "team_member_id": h["team_member_id"], "team_rank": 0})
            rows.append(_hidden(row))
        t = _snapshot(plan["snap"], rnd["opp"])
        rounds.append({"round": n, "race_horse_data_array": rows,
                       "opponent_team_info": _opp_team(t, 2, plan["ri"], plan["season"], rnd["opp_score"])})
    race_items = [{"item_id": i, "number": shop._item_count(full, i)}
                  for i in (ITEM_PARFAIT, ITEM_SUN, ITEM_RAIN, ITEM_INNER, ITEM_OUTER)]
    return {"set_info": {"season": plan["season"], "weather": plan["weather"], "ground_condition": plan["ground"],
                         "race_instance_id": plan["ri"]},
            "round_info_array": rounds, "race_item_info_array": race_items}


@registry.endpoint("heroes/stage1_get_opponent_teams")
def handle_get_opponent_teams(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    if not _can_race(hd, st) or st["set"]:
        return refuse(ResultCode.PARAM_ERROR)
    vid = _vid(payload)
    if not st["plan"]:
        st["plan"] = _plan(vid, st, hd)
    st["resume"] = 10
    info = _opponent_info(vid, full, st, hd)
    _save(payload, full)
    return _ok({"get_opponent_team_info": info})


@registry.endpoint("heroes/stage1_check_opponent_teams")
def handle_check_opponent_teams(payload):
    return _ok({})


@registry.endpoint("heroes/stage1_check_hero_skill")
def handle_check_hero_skill(payload):
    return _ok({})


def _skill_usable(skill, items) -> bool:
    if not skill:
        return False
    r = M.query_one("SELECT item_id FROM heroes_skill_item WHERE skill_id=?", (skill,))
    return not r or not r["item_id"] or r["item_id"] in items


@registry.endpoint("heroes/stage1_start_set")
def handle_start_set(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    vid = _vid(payload)
    plan = st["plan"]
    items = [int(i) for i in payload.get("item_id_array") or []]
    if st["set"] and st["set"].get("start_info") and not st["set"].get("grand"):
        return _ok({"start_set_info": st["set"]["start_info"]})
    if not _can_race(hd, st) or st["set"] or not plan:
        return refuse(ResultCode.PARAM_ERROR)
    if len(items) > hd["race_item_limit"] or len(set(items)) != len(items) \
            or any(shop._item_count(full, i) < 1 for i in items):
        return refuse(ResultCode.PARAM_ERROR)
    cost = _rank_row(st["score"])["ticket_num"]
    tid = _ticket(hd["heroes_id"])
    if cost and shop._item_count(full, tid) < cost:
        return refuse(ResultCode.PARAM_ERROR)
    for i in items:
        shop._add_item(full, i, -1)
    if cost:
        shop._add_item(full, tid, -cost)
    skill = st["skill"] if _skill_usable(st["skill"], items) else 0
    weather, ground = plan["weather"], plan["ground"]
    if ITEM_SUN in items:
        weather, ground = 1, 1
    elif ITEM_RAIN in items:
        weather, ground = 3, 4
    league_rank = _rank_row(st["score"])["league_rank"]
    scores = _scores()
    out, bests, gains, chara_gain, hist = [], [], [], {}, []
    for n, rnd in enumerate(plan["rounds"], start=1):
        field = _field(vid, st, plan, rnd, items, skill)
        if ITEM_PARFAIT in items:
            for h in field[:TEAM_SIZE]:
                h["motivation"] = 5
        gates, moved = _gates(rnd, items)
        result = _race(field, gates, plan, rnd, weather, ground)
        rows = _horse_rows(field, result, {"mood": 1 if ITEM_PARFAIT in items else 0, "gate": 1 if moved else 0})
        dec = _decoded(result)
        order = [dec[r["frame_order"] - 1].finish_order + 1 for r in rows]
        own = sorted(range(TEAM_SIZE), key=lambda i: order[i])
        best_i = own[0]
        best = order[best_i]
        margin = horse_length(dec[rows[best_i]["frame_order"] - 1].finish_diff_time) if best == 2 else 99
        score = sum(scores.get(order[i], 0) for i in range(TEAM_SIZE))
        g = round_gauge(best, margin)
        bests.append(best)
        gains.append(g)
        for i in range(TEAM_SIZE):
            c = field[i]
            chara_gain[str(c["chara_id"])] = chara_gain.get(str(c["chara_id"]), 0) + scores.get(order[i], 0)
            hist.append((c["trained_chara_id"], {
                "league_rank": league_rank, "race_num": n, "rank": order[i],
                "finish_time": int(round(dec[rows[i]["frame_order"] - 1].finish_time * 10000)),
                "frame_order": rows[i]["frame_order"], "popularity": rows[i]["popularity"], "race_horse_num": FIELD}))
        t = _snapshot(plan["snap"], rnd["opp"])
        out.append({"round_info": {"round": n, "race_horse_data_array": rows,
                                   "opponent_team_info": _opp_team(t, 2, plan["ri"], plan["season"], rnd["opp_score"])},
                    "race_scenario": result["race_scenario"], "random_seed": rnd["seed"],
                    "gained_league_score": score, "gained_hero_gauge": g})
    for tcid, h in hist:
        s = st["stats"].setdefault(str(tcid), {"hist": [], "c": [0, 0, 0, 0]})
        s["hist"].append(h)
        s["c"][min(h["rank"], 4) - 1] += 1
    info_items = [{"item_id": i, "number": shop._item_count(full, i)} for i in items]
    if cost:
        info_items.append({"item_id": tid, "number": shop._item_count(full, tid)})
    start_info = {"set_info": {"season": plan["season"], "weather": weather, "ground_condition": ground,
                               "race_instance_id": plan["ri"]},
                  "round_result_array": out, "item_info_array": info_items}
    st["set"] = {"ticket": bool(cost), "skill": skill, "bests": bests, "gains": gains,
                 "score": sum(r["gained_league_score"] for r in out), "chara_gain": chara_gain,
                 "round_num": 1, "start_info": start_info, "grand": None, "finish": None}
    st["plan"] = None
    st["resume"] = 20
    _save(payload, full)
    return _ok({"start_set_info": start_info})


@registry.endpoint("heroes/stage1_check_round")
def handle_check_round(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    n = int(payload.get("stage1_resume_round_num") or 1)
    if st["set"]:
        st["set"]["round_num"] = n
        st["resume"] = 30
    _save(payload, full)
    return _ok({"stage1_resume_state": st["resume"], "stage1_resume_round_num": n})


def _draw_skill(st, seed) -> int:
    rank = _rank_row(st["score"])["league_rank"]
    rows = [r for r in M.query("SELECT * FROM heroes_skill WHERE league_rank_min<=? AND league_rank_max>=?",
                               (rank, rank))]
    if not rows:
        return 0
    return random.Random(seed).choices([r["skill_id"] for r in rows], weights=[r["rate"] for r in rows])[0]


def _finish_block(st, drawn) -> dict:
    return {"reward_array": [], "reward_summary_info": [], "hero_gauge": st["gauge"], "hero_skill_id": drawn}


@registry.endpoint("heroes/stage1_grand_result")
def handle_grand_result(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    cur = st["set"]
    if not cur:
        return refuse(ResultCode.PARAM_ERROR)
    if cur["grand"] is None:
        vid = _vid(payload)
        before_score, after_score = st["score"], st["score"] + cur["score"]
        items = []
        for r in _ranks():
            if before_score < r["league_min_value"] <= after_score and r["league_rank"] not in st["ranks_got"] \
                    and r["reward_group_id"]:
                st["ranks_got"].append(r["league_rank"])
                items += [_item(g["item_category"], g["item_id"], g["item_num"]) for g in M.query(
                    "SELECT * FROM heroes_league_rank_reward_group WHERE reward_group_id=? ORDER BY id",
                    (r["reward_group_id"],))]
        summary = _grant(vid, full, items) if items else None
        st["score"] = after_score
        for k, v in cur["chara_gain"].items():
            st["chara_score"][k] = st["chara_score"].get(k, 0) + v
        before_g = min(100, st["gauge"] + sum(cur["gains"]))
        drawn = 0
        if cur["skill"] and cur["ticket"]:
            after_g = 0
        else:
            after_g = before_g + set_gauge(cur["bests"])
        if after_g >= 100:
            drawn = _draw_skill(st, f"{vid}:{st['seq']}:skill")
        st["gauge"], st["skill"] = after_g, drawn
        grand = {"before_league_score": before_score, "after_league_score": after_score,
                 "reward_summary_info": summary, "before_hero_gauge": before_g, "after_hero_gauge": after_g,
                 "own_chara_race_stats_array": [_stats_wire(st, c["trained_chara_id"]) for c in st["team"]]}
        cur["grand"] = grand
        cur["drawn"] = drawn
        if not cur["ticket"]:
            cur["finish"] = _finish_block(st, drawn)
    grand = cur["grand"]
    finish = cur["finish"]
    if finish is not None:
        st["set"] = None
        st["resume"] = 0
    else:
        st["resume"] = 40
    _save(payload, full)
    return _ok({"stage1_grand_result": grand, "stage1_finish_set": finish})


def _drops(cur, seed) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for best in cur["bests"]:
        if best <= 3 or rng.random() < DROP_IF_UNPLACED:
            (cat, iid, num), = rng.choices([d for d, _ in DROPS], weights=[w for _, w in DROPS])
            out.append(_item(cat, iid, num))
    return out


@registry.endpoint("heroes/stage1_finish_set")
def handle_finish_set(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    cur = st["set"]
    if not cur or cur["grand"] is None:
        return refuse(ResultCode.PARAM_ERROR)
    vid = _vid(payload)
    items = _drops(cur, f"{vid}:{st['seq']}:drops")
    summary = _grant(vid, full, items)
    finish = {"reward_array": items, "reward_summary_info": summary, "hero_gauge": st["gauge"],
              "hero_skill_id": cur.get("drawn", 0)}
    st["set"] = None
    st["resume"] = 0
    _save(payload, full)
    return _ok({"stage1_finish_set": finish})


@registry.endpoint("heroes/stage1_resume")
def handle_resume(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    vid = _vid(payload)
    cur = st["set"] or {}
    opp = _opponent_info(vid, full, st, hd) if st["plan"] and not cur else None
    return _ok({"stage1_resume_state": st["resume"], "stage1_resume_round_num": cur.get("round_num", 1),
                "get_opponent_team_info": opp, "start_set_info": cur.get("start_info"),
                "stage1_grand_result": cur.get("grand")})


@registry.endpoint("heroes/stage1_honor_select_chara_list")
def handle_honor_select_chara_list(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    rows = [{"chara_id": int(k), "score": v} for k, v in sorted(st["chara_score"].items(), key=lambda kv: int(kv[0]))]
    return _ok({"selectable_chara_list": rows, "unselectable_chara_list": [], "select_honor_chara_id": st["honor_chara"]})


@registry.endpoint("heroes/stage1_honor_select_chara")
def handle_honor_select_chara(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    cid = int(payload.get("chara_id") or 0)
    if str(cid) not in st["chara_score"]:
        return refuse(ResultCode.PARAM_ERROR)
    st["honor_chara"] = cid
    st["resume"] = 50
    _save(payload, full)
    return _ok({"stage1_resume_state": 50})


def _finalist_scores(hid) -> list[int]:
    try:
        own = json.loads((OS.LOH_DIR / "heroes_meta.json").read_text())["heroes"][str(hid)]["final_scores"]
        if own:
            return list(own)
    except (OSError, ValueError, KeyError):
        pass
    try:
        return list(json.loads(FINALISTS.read_text())["scores"])
    except (OSError, ValueError, KeyError):
        return []


def player_rank(st) -> int:
    scores = _finalist_scores(st["heroes_id"])
    r = 1 + sum(1 for s in scores if s > st["score"])
    return r if scores and r <= len(scores) and not st["option_deny"] else 0


def _ranking_record(rank, score, viewer, name, members, snapshot) -> dict:
    lead = members[0] if members else {}
    return {"rank": rank, "league_score": score, "viewer_id": viewer, "snapshot_id": snapshot,
            "team_name": name, "honor_data": {"honor_id": 0},
            "team_member_array": [{"team_member_id": i + 1, "trained_chara_id": m["trained_chara_id"],
                                   "card_id": m["card_id"], "final_grade": m.get("rank") or 1,
                                   "rarity": m.get("rarity") or 3, "talent_level": m.get("talent_level") or 1,
                                   "race_cloth_id": m.get("race_cloth_id") or 0,
                                   "running_style": m.get("running_style") or 1} for i, m in enumerate(members)],
            "leader_chara_id": lead.get("chara_id") or 0, "leader_chara_dress_id": lead.get("race_cloth_id") or 0}


@registry.endpoint("heroes/get_ranking")
def handle_get_ranking(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    vid = _vid(payload)
    f = _bracket(vid, st, hd)
    top = []
    for slot in f["slots"]:
        if slot["team"] == "me":
            top.append(_ranking_record(slot["rank"], st["score"], social._wire_id(vid), _own_team(vid, st)["team_name"],
                                       st["team"], 0))
        else:
            t = _snapshot(f["snap"], slot["team"])
            ms = _opp_members(t, f["ri"], f["season"])
            top.append(_ranking_record(slot["rank"], slot["score"], POOL.trainer_viewer_id(t["id"]),
                                       POOL.trainer_name(t), ms, slot["rank"]))
    me = _ranking_record(player_rank(st), st["score"], social._wire_id(vid), _own_team(vid, st)["team_name"],
                         st["team"], 0)
    return _ok({"ranking_array1": top, "ranking_array2": [me], "ranking_array3": []})


@registry.endpoint("heroes/get_ranking_chara_info")
def handle_get_ranking_chara_info(payload):
    hd, full, st, err = _need(payload)
    if err:
        return err
    vid = _vid(payload)
    target = str(payload.get("target_viewer_id"))
    idx = int(payload.get("team_member_id") or 1) - 1
    if target == str(social._wire_id(vid)):
        c = st["team"][idx] if idx < len(st["team"]) else None
        if not c:
            return refuse(ResultCode.PARAM_ERROR)
        return _ok({"user_name": _head(vid)["name"], "trained_chara": _tc_wire(c),
                    "chara_race_stats": _stats_wire(st, c["trained_chara_id"])})
    f = _bracket(vid, st, hd)
    for slot in f["slots"]:
        if slot["team"] != "me":
            t = _snapshot(f["snap"], slot["team"])
            if str(POOL.trainer_viewer_id(t["id"])) == target:
                m = _opp_members(t, f["ri"], f["season"])[idx]
                return _ok({"user_name": POOL.trainer_name(t), "trained_chara": m,
                            "chara_race_stats": _empty_stats(m["trained_chara_id"])})
    return refuse(ResultCode.PARAM_ERROR)


@registry.endpoint("heroes/get_past_results")
def handle_get_past_results(payload):
    full = state_store.get_state(_vid(payload)) or {}
    past = full.get("heroes_past") or []
    return _ok({"past_results": past})


@registry.endpoint("heroes/get_past_race_replay")
def handle_get_past_race_replay(payload):
    return refuse(ResultCode.PARAM_ERROR)


def _bracket(vid, st, hd) -> dict:
    f = st.get("final")
    if f and f.get("slots"):
        return f
    scores = _finalist_scores(st["heroes_id"])
    me = player_rank(st)
    rng = random.Random(f"heroes:{hd['heroes_id']}:{vid}:bracket")
    snap = "mix"
    teams = [_snapshot(snap, ref) for ref in _pick(hd["heroes_id"], FINAL_BAND, rng, len(scores))]
    teams.sort(key=lambda t: -t["_percentile"])
    slots, it = [], iter(teams)
    for rank in range(1, len(scores) + 1):
        if rank == me:
            slots.append({"rank": rank, "score": st["score"], "team": "me"})
        else:
            s = scores[rank - 2] if me and rank > me else scores[rank - 1]
            t = next(it)
            slots.append({"rank": rank, "score": s, "team": f"{t['_pool']}|{t['id']}"})
    ri, season = _course(hd["heroes_id"], 2)
    group, race = 0, 0
    for r in M.query("SELECT * FROM heroes_final_stage_race"):
        if me and r["ranking_min"] <= me <= r["ranking_max"]:
            group, race = r["group_id"], r["race_number"]
    f = {"slots": slots, "snap": snap, "ri": ri, "season": season, "me": me, "group": group, "race": race,
         "umaine": {}, "umaine_groups": [], "played": [], "rewarded": [], "conds": {}}
    st["final"] = f
    return f


def _final_races() -> list[dict]:
    return [dict(r) for r in M.query("SELECT * FROM heroes_final_stage_race ORDER BY group_id, race_number")]


def _next_play(f):
    plays = sorted((r for r in _final_races() if r["race_play"]), key=lambda r: r["race_play"])
    for r in plays:
        if [r["group_id"], r["race_number"]] not in f["played"]:
            return {"group_id": r["group_id"], "race_num": r["race_number"]}
    return None


def _final_teams(vid, st, f, row) -> list:
    return [s for s in f["slots"] if row["ranking_min"] <= s["rank"] <= row["ranking_max"]]


_final_cache: dict = {}


def _final_race(vid, st, hd, f, group, race, with_result: bool) -> dict | None:
    row = next((r for r in _final_races() if r["group_id"] == group and r["race_number"] == race), None)
    if row is None:
        return None
    slots = _final_teams(vid, st, f, row)
    key = (vid, hd["heroes_id"], group, race, with_result)
    seed = random.Random(f"heroes:{hd['heroes_id']}:{vid}:final:{group}:{race}").getrandbits(31)
    rng = random.Random(seed)
    cond = race_conditions.roll(f["ri"], rng, f["season"])
    field, teams = [], []
    me = _head(vid)
    for tno, slot in enumerate(slots, start=1):
        if slot["team"] == "me":
            info = _own_team(vid, st)
            info["team_id"] = tno
            for c in st["team"]:
                h = copy.deepcopy(c)
                h.update({"viewer_id": social._wire_id(vid), "trainer_name": me["name"], "npc_type": NPC_TRAINER,
                          "team_id": tno, "team_member_id": c["team_member_id"], "_rank": slot["rank"]})
                field.append(h)
        else:
            t = _snapshot(f["snap"], slot["team"])
            info = _opp_team(t, tno, f["ri"], f["season"], slot["score"])
            for m in info["member_array"]:
                h = copy.deepcopy(m["trained_chara"])
                h.update({"viewer_id": info["viewer_id"], "trainer_name": info["user_name"], "npc_type": NPC_OPPONENT,
                          "team_id": tno, "team_member_id": m["team_member_id"], "_rank": slot["rank"]})
                field.append(h)
        info["league_score"] = slot["score"]
        teams.append(info)
    for h in field:
        h["motivation"] = 5
    set_info = {"season": f["season"], "weather": cond["weather"], "ground_condition": cond["ground_condition"],
                "race_instance_id": f["ri"]}
    out = {"race_num": race, "set_info": set_info, "team_info_array": teams,
           "umaine_array": list(f["umaine"].get(f"{group}:{race}", [])),
           "is_played": 1 if [group, race] in f["played"] else 0, "chara_result_array": None}
    if not with_result:
        out["race_horse_info_array"] = [{"race_horse_data": _hidden_final(h, i), "stage1_rank": h["_rank"]}
                                        for i, h in enumerate(field)]
        return out
    if key in _final_cache:
        return copy.deepcopy(_final_cache[key])
    gates = rng.sample(range(len(field)), len(field))
    if len(field) < 2:
        return out
    rnd = {"seed": seed}
    result = _race(field, gates, None, rnd, cond["weather"], cond["ground_condition"], f["ri"], f["season"])
    rows = _horse_rows(field, result, {})
    dec = _decoded(result)
    out["race_horse_info_array"] = [{"race_horse_data": r, "stage1_rank": h["_rank"]} for h, r in zip(field, rows)]
    out["chara_result_array"] = [
        {"viewer_id": r["viewer_id"], "trained_chara_id": r["trained_chara_id"], "frame_order": r["frame_order"],
         "rank": dec[r["frame_order"] - 1].finish_order + 1,
         "finish_time": int(round(dec[r["frame_order"] - 1].finish_time * 10000)),
         "horse_length": horse_length(dec[r["frame_order"] - 1].finish_diff_time)} for r in rows]
    out["_scenario"], out["_seed"] = result["race_scenario"], seed
    _final_cache[key] = copy.deepcopy(out)
    return out


def _hidden_final(h, i) -> dict:
    row = practice_race._build_race_horse_entry(h, frame_order=i + 1, final_grade=h.get("rank") or 1,
                                                popularity=i + 1, popularity_mark_rank_array=[i + 1] * 3)
    row.update({"motivation": 5, "team_id": h["team_id"], "team_member_id": h["team_member_id"], "team_rank": 0})
    return row


def _wire_race(r) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _race_lists(vid, st, hd, f, with_result) -> dict:
    out = {"race_info_array_1": [], "race_info_array_2": []}
    for row in _final_races():
        r = _final_race(vid, st, hd, f, row["group_id"], row["race_number"], with_result)
        if r:
            out[f"race_info_array_{row['group_id']}"].append(_wire_race(r))
    for k in out:
        out[k].sort(key=lambda r: r["race_num"])
    return out


def _final_index(vid, full, st, hd, stage, step) -> dict:
    f = _bracket(vid, st, hd)
    chars = []
    for slot in f["slots"]:
        if slot["team"] == "me":
            ms, name, viewer = st["team"], _own_team(vid, st)["team_name"], social._wire_id(vid)
        else:
            t = _snapshot(f["snap"], slot["team"])
            ms, name, viewer = _opp_members(t, f["ri"], f["season"]), POOL.trainer_name(t), POOL.trainer_viewer_id(t["id"])
        row = next((r for r in _final_races() if r["ranking_min"] <= slot["rank"] <= r["ranking_max"]), None)
        for m in ms:
            chars.append({"viewer_id": viewer, "card_id": m["card_id"], "rarity": m.get("rarity") or 3,
                          "rank": m.get("rank") or 1, "nickname_id": m.get("nickname_id") or 0, "team_name": name,
                          "group_id": row["group_id"] if row else 0, "race_num": row["race_number"] if row else 0})
    return {"stage1_rank": f["me"], "race_group_id": f["group"], "race_num": f["race"],
            "next_play_info": _next_play(f) if step >= 4 else {"group_id": 1, "race_num": 11},
            "honor_data": None, "team_edit_change_flag": 0, "index_chara_array": chars,
            "received_reward_group_array": list(f["umaine_groups"])}


def _need_final(payload, min_step):
    hd, full, st, err = _need(payload)
    if err:
        return hd, full, st, None, err
    stage, step = stage_at(hd["heroes_id"])
    if stage != 2 or step < min_step:
        return hd, full, st, None, refuse(ResultCode.PARAM_ERROR)
    return hd, full, st, _bracket(_vid(payload), st, hd), None


@registry.endpoint("heroes/final_edit_team")
def handle_final_edit_team(payload):
    hd, full, st, f, err = _need_final(payload, 1)
    if err:
        return err
    if stage_at(hd["heroes_id"]) != (2, 1) or not f["me"]:
        return refuse(ResultCode.PARAM_ERROR)
    team = champions._snapshot_team(_vid(payload), payload.get("entry_chara_array") or [], None)
    if team is None:
        return refuse(ResultCode.PARAM_ERROR)
    st["team"] = team
    _final_cache.clear()
    _save(payload, full)
    return _ok({})


@registry.endpoint("heroes/final_get_umaine")
def handle_final_get_umaine(payload):
    hd, full, st, f, err = _need_final(payload, 2)
    if err:
        return err
    data = _race_lists(_vid(payload), st, hd, f, False)
    first = 0 if f["conds"].get("umaine_tips") else 1
    f["conds"]["umaine_tips"] = 1
    data.update(received_reward_group_array=list(f["umaine_groups"]), first_access_umaine_tips_flag=first)
    _save(payload, full)
    return _ok(data)


def _special(cond) -> list[dict]:
    return [_item(r["item_category"], r["item_id"], r["item_num"])
            for r in M.query("SELECT * FROM heroes_special_reward WHERE condition_type=? ORDER BY id", (cond,))]


@registry.endpoint("heroes/final_set_umaine")
def handle_final_set_umaine(payload):
    hd, full, st, f, err = _need_final(payload, 3)
    if err:
        return err
    group, race = int(payload.get("group_id") or 0), int(payload.get("race_num") or 0)
    liked = [int(s["frame_num"]) for s in payload.get("setting_array") or [] if s.get("enable")]
    f["umaine"][f"{group}:{race}"] = liked
    items = []
    if group and group not in f["umaine_groups"]:
        f["umaine_groups"].append(group)
        items = _special(3)
    summary = _grant(_vid(payload), full, items)
    _save(payload, full)
    return _ok({"reward_summary_info": summary, "received_reward_group_array": list(f["umaine_groups"])})


@registry.endpoint("heroes/final_get_race_list")
def handle_final_get_race_list(payload):
    hd, full, st, f, err = _need_final(payload, 2)
    if err:
        return err
    step = stage_at(hd["heroes_id"])[1]
    data = _race_lists(_vid(payload), st, hd, f, step >= 4)
    _save(payload, full)
    return _ok(data)


@registry.endpoint("heroes/final_get_race_replay")
def handle_final_get_race_replay(payload):
    hd, full, st, f, err = _need_final(payload, 4)
    if err:
        return err
    nxt = payload.get("next_play_info") or _next_play(f) or {}
    r = _final_race(_vid(payload), st, hd, f, int(nxt.get("group_id") or 0), int(nxt.get("race_num") or 0), True)
    if not r or "_scenario" not in r:
        return refuse(ResultCode.PARAM_ERROR)
    first = 0 if f["conds"].get("race_tips") else 1
    f["conds"]["race_tips"] = 1
    _save(payload, full)
    return _ok({"race_result_info": {"race_info": _wire_race(r), "race_scenario": r["_scenario"],
                                     "random_seed": r["_seed"]},
                "first_access_race_tips_flag": first})


@registry.endpoint("heroes/final_check_race")
def handle_final_check_race(payload):
    hd, full, st, f, err = _need_final(payload, 4)
    if err:
        return err
    group, race = int(payload.get("group_id") or 0), int(payload.get("race_num") or 0)
    items = []
    if [group, race] not in f["played"]:
        f["played"].append([group, race])
        row = next((r for r in _final_races() if r["group_id"] == group and r["race_number"] == race), None)
        if row and row["race_play"] and row["heroes_special_reward"]:
            items = _special(row["heroes_special_reward"])
    summary = _grant(_vid(payload), full, items)
    _final_cache.clear()
    _save(payload, full)
    return _ok({"reward_summary_info": summary, "next_play_info": _next_play(f)})
