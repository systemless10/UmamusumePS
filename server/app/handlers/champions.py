from __future__ import annotations

import copy
import logging
import random
import time

from .. import champions_meeting as CM, champions_pool as POOL, opponent_sources as OS
from .. import master_data as M, patch, social
from .. import state as state_store
from ..result_codes import ResultCode, refuse
from ..simulation import race_simulator
from . import practice_race, registry, shop, trained_chara
from .presents import send as present_send

log = logging.getLogger("uma-server")

STATE_KEY = "champions_state"
ENTRY_COUNT_KEY = "champions_entry_count"

LEAGUE_OPEN, LEAGUE_GRADED = 1, 2
TIER_B, TIER_A = 1, 2
RACES_PER_ENTRY = 5
TEAM_SIZE = 3
OPEN_LEAGUE_RANK_MAX = 14
ENTRY_COST = 30
RESET_HOUR = 22
ENTRY_FREE, ENTRY_JEWEL, ENTRY_TICKET = 1, 2, 3
RACE_TYPE_CHAMPIONS = 5
NPC_TYPE_TRAINER = 11

ST_LEAGUE, ST_ENTRY, ST_RACESTART, ST_LOBBY, ST_REWARD, ST_FINALREWARD, ST_ALLEND, \
    ST_READY, ST_SIMULATED = 1, 2, 3, 4, 5, 6, 7, 8, 9

CAT_CARAT, CAT_MONEY, CAT_PIECE, CAT_EXP, CAT_GACHA, CAT_HONOR = 90, 91, 97, 30, 40, 55
ITEM_CARAT, ITEM_MONEY, ITEM_STATUE, ITEM_SUPPORT_PT = 43, 59, 115, 110
ITEM_CHARA_TICKET, ITEM_SUPPORT_TICKET = 41, 111

_QUAL = {
    1: {5: (50, 2, 8500, 4000), 4: (40, 2, 7000, 3500), 3: (30, 2, 5000, 2500),
        2: (20, 1, 5000, 2500), 1: (15, 1, 5000, 2500), 0: (10, 1, 5000, 2500)},
    3: {5: (150, 5, 11000, 5500), 4: (75, 4, 8000, 4000), 3: (50, 4, 6000, 3000),
        2: (40, 3, 6000, 3000), 1: (30, 3, 6000, 3000), 0: (20, 3, 6000, 3000)},
    2: {5: (100, 4, 8500, 4000), 4: (50, 3, 7000, 3500), 3: (40, 3, 5000, 2500),
        2: (30, 2, 5000, 2500), 1: (20, 2, 5000, 2500), 0: (15, 2, 5000, 2500)},
}
_FINAL = {
    5: {1: (5, 5, 2500, 30, 100000, 25000), 2: (4, 4, 1800, 25, 70000, 20000), 3: (3, 3, 1200, 20, 50000, 15000)},
    4: {1: (3, 3, 1200, 20, 60000, 20000), 2: (2, 2, 900, 15, 50000, 15000), 3: (1, 1, 700, 10, 40000, 10000)},
}
_FINAL_TITLES = {5: {1: (1, 2, 3, 4), 2: (2, 3, 4), 3: (3, 4)},
                 4: {1: (2, 3, 4), 2: (3, 4), 3: (4,)}}


def _ok(data=None) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data if data is not None else {}}


def _item(cat, item_id, num) -> dict:
    return {"item_type": cat, "item_id": item_id, "item_num": num}


def _qual_items(round_id, wins) -> list[dict]:
    c, s, m, p = _QUAL[round_id][max(0, min(5, wins))]
    return [_item(CAT_CARAT, ITEM_CARAT, c), _item(CAT_PIECE, ITEM_STATUE, s),
            _item(CAT_MONEY, ITEM_MONEY, m), _item(CAT_EXP, ITEM_SUPPORT_PT, p)]


def _final_items(cm, round_id, rank, owned_honors=()) -> list[dict]:
    ct, st, c, s, m, p = _FINAL[round_id][rank]
    out = [_item(CAT_GACHA, ITEM_CHARA_TICKET, ct), _item(CAT_GACHA, ITEM_SUPPORT_TICKET, st),
           _item(CAT_CARAT, ITEM_CARAT, c), _item(CAT_PIECE, ITEM_STATUE, s),
           _item(CAT_MONEY, ITEM_MONEY, m), _item(CAT_EXP, ITEM_SUPPORT_PT, p)]
    res = _resource_id(cm)
    for tier in _FINAL_TITLES[round_id][rank]:
        first = int(f"4{res:02d}{tier}01")
        hid = int(f"4{res:02d}{tier}02") if str(first) in owned_honors else first
        if M.query_one("SELECT id FROM honor_data WHERE id=?", (hid,)):
            out.append(_item(CAT_HONOR, hid, 1))
    return out


def _resource_id(cm) -> int:
    r = M.query_one("SELECT resource_id FROM champions_schedule WHERE id=?", (cm,))
    return int(r["resource_id"]) if r else 1


def _reward_info_array(cm) -> list[dict]:
    out = []
    for r in M.query("SELECT * FROM champions_reward_rate WHERE champions_id=? ORDER BY league_type, round_id, "
                     "win_count, ranking", (cm,)):
        rid = r["round_id"]
        if rid in _QUAL:
            items = _qual_items(rid, r["win_count"])
        elif rid in _FINAL and r["ranking"] in _FINAL[rid]:
            items = _final_items(cm, rid, r["ranking"])
        else:
            continue
        out.append({"league_type": r["league_type"], "round_id": rid, "win_count": r["win_count"],
                    "ranking": r["ranking"], "item_array": items})
    return out


def _empty_summary() -> dict:
    return shop._empty_summary()


def _grant_items(vid, full: dict, items: list[dict], to_present: bool) -> dict:
    summary = _empty_summary()
    from . import user_profile
    for it in items:
        cat, iid, num = it["item_type"], it["item_id"], it["item_num"]
        if cat == CAT_HONOR:
            st = user_profile._honor_state(full, vid)
            if user_profile._grant_honor_in_state(st, iid):
                summary["add_honor_list"].append({"honor_id": iid, "step": 0,
                                                  "create_time": time.strftime("%Y-%m-%d %H:%M:%S")})
            continue
        if to_present:
            present_send(full, cat, iid, num, message_id=None)
            summary["add_present_num"] += 1
        elif cat == CAT_CARAT:
            shop.grant_carats(full, num)
            summary["add_fcoin"] += num
        else:
            shop._add_item(full, iid, num)
            summary["add_item_list"].append({"item_id": iid, "number": shop._item_count(full, iid)})
    return summary


def _vid(payload) -> str:
    return str(payload.get("viewer_id"))


def _run():
    return CM.active()


def _now() -> int:
    return patch._servertime()


def _stage(run) -> str:
    return CM.stage_at(run, _now())


def _new_state(run) -> dict:
    return {"run_id": run["run_id"], "champions_id": run["champions_id"], "league": 0,
            "signup_claimed": False, "day": None, "entry_times": 0, "free_entry_times": 0,
            "rounds": {}, "cur": None, "cum_wins": 0, "last_entry_chara": [],
            "final": None, "rank_count": {}, "race_seq": 0}


def _cm_state(full: dict, run) -> dict:
    st = full.get(STATE_KEY)
    if not isinstance(st, dict) or st.get("run_id") != run["run_id"]:
        st = _new_state(run)
        full[STATE_KEY] = st
    _roll_day(st)
    return st


def _roll_day(st: dict) -> None:
    day = (_now() - RESET_HOUR * 3600) // 86400
    if st.get("day") != day:
        st["day"] = day
        st["entry_times"] = 0
        st["free_entry_times"] = 0


def _detail(cm, league, round_id):
    return M.query_one("SELECT * FROM champions_round_detail WHERE champions_id=? AND league_type=? AND round_id=?",
                       (cm, league, round_id))


def _round_of_stage(stage) -> int:
    return {"round1": 1, "round1_tally": 1, "round2": 2, "round2_tally": 2}.get(stage, 0)


def _r2_round_id(st) -> int:
    r1 = st["rounds"].get("1")
    d = _detail(st["champions_id"], st["league"] or LEAGUE_GRADED, 1)
    need = d["breakthrough_number_1"] if d else 3
    return 3 if r1 and r1["best"] >= need else 2


def _round_id(st, stage) -> int:
    if stage == "league":
        return 0
    if stage in ("round1", "round1_tally"):
        return 1
    if stage in ("round2", "round2_tally"):
        return _r2_round_id(st)
    fin = _final_round_id(st)
    return fin or (_r2_round_id(st) if st["rounds"].get("2") else 1)


def _final_round_id(st) -> int:
    r2 = st["rounds"].get("2")
    if not r2:
        return 0
    rid = r2["round_id"]
    d = _detail(st["champions_id"], st["league"] or LEAGUE_GRADED, rid)
    b1, b2 = (d["breakthrough_number_1"], d["breakthrough_number_2"]) if d else ((3, 1) if rid == 3 else (1, 0))
    if rid == 3:
        return 5 if r2["best"] >= b1 else 4 if r2["best"] >= b2 else 0
    return 4 if r2["best"] >= b1 else 0


def _seen_best(st, key) -> int:
    r = st["rounds"][key]
    cur = st.get("cur")
    if cur and str(cur["round"]) == key and cur.get("simulated") and cur["viewed"] < len(cur["races"]):
        seen = sum(x["result"] for x in cur["races"][:cur["viewed"]])
        return max([e["wins"] for e in r["entries"][:-1]] + [seen])
    return r["best"]


def _next_tier(st, stage) -> int:
    st = dict(st, rounds={k: dict(v, best=_seen_best(st, k)) for k, v in st["rounds"].items()})
    rnd = _round_of_stage(stage)
    if rnd == 1:
        return TIER_A if _r2_round_id(st) == 3 else TIER_B
    if rnd == 2 or stage.startswith("final"):
        return {5: TIER_A, 4: TIER_B}.get(_final_round_id(st), 0)
    return 0


def _entry_claimed(r, e) -> bool:
    return e.get("claimed", r.get("claimed", False))


def _unclaimed_round(st, upto: int) -> str | None:
    for key in ("1", "2"):
        if int(key) > upto:
            break
        r = st["rounds"].get(key)
        if r and any(not _entry_claimed(r, e) for e in r["entries"]):
            return key
    return None


def _info_state(st, stage) -> int:
    if stage == "ended":
        return ST_ALLEND
    if not st["league"]:
        return ST_LEAGUE if stage in ("league", "round1", "round2") else ST_ALLEND
    if stage == "league":
        return ST_ENTRY
    rnd = _round_of_stage(stage)
    if stage in ("round1_tally", "round2_tally"):
        return ST_REWARD if _unclaimed_round(st, rnd) else ST_LEAGUE
    if stage == "round2" and _unclaimed_round(st, 1):
        return ST_REWARD
    if stage.startswith("final"):
        if _unclaimed_round(st, 2):
            return ST_REWARD
        fin = st.get("final")
        if not _final_round_id(st):
            return ST_ALLEND
        if stage == "final_entry":
            return ST_LOBBY if fin and fin.get("team") else ST_ENTRY
        if stage == "final_race":
            return ST_LOBBY
        if not (fin and fin.get("viewed")):
            return ST_RACESTART
        return ST_ALLEND if fin.get("claimed") else ST_FINALREWARD
    cur = st.get("cur")
    if cur and cur["round"] == rnd:
        if not cur.get("team"):
            return ST_ENTRY
        if cur.get("pending") is not None:
            return ST_RACESTART
        if cur.get("simulated") and cur["viewed"] < len(cur["races"]):
            return ST_SIMULATED
        if len(cur["races"]) < RACES_PER_ENTRY:
            return ST_LOBBY
        return ST_REWARD
    return ST_LEAGUE


def _free_left(st, stage) -> int:
    return st["free_entry_times"]


def _champions_info(st, stage) -> dict:
    cur = st.get("cur")
    team = (cur or {}).get("team") or (st.get("final") or {}).get("team") or []
    return {"champions_id": st["champions_id"], "league_type": st["league"],
            "entry_times": st["entry_times"], "free_entry_times": _free_left(st, stage),
            "round_id": _round_id(st, stage), "next_tier": _next_tier(st, stage),
            "state": _info_state(st, stage),
            "entry_trained_chara_id_array": [c["trained_chara_id"] for c in team]}


def load_info(viewer_id, full: dict) -> dict | None:
    run = _run()
    if not run:
        return None
    st = _cm_state(full, run)
    stage = _stage(run)
    _tally(st, stage)
    return _champions_info(st, stage)


def _race_condition(cm, round_id):
    r = M.query_one("SELECT race_instance_id, race_condition_id FROM champions_race_condition "
                    "WHERE champions_id=? AND round_id=?", (cm, round_id))
    if not r:
        r = M.query_one("SELECT race_instance_id, race_condition_id FROM champions_race_condition "
                        "WHERE champions_id=? ORDER BY round_id LIMIT 1", (cm,))
    cond = M.query_one("SELECT season, weather, ground FROM race_condition WHERE id=?", (r["race_condition_id"],))
    season, weather, ground = (cond["season"], cond["weather"], cond["ground"]) if cond else (1, 1, 1)
    return r["race_instance_id"], season, weather, ground


def _user_head(vid) -> dict:
    from . import room_match
    return room_match._user_head(vid)


def _band(st, kind: str) -> list:
    cfg = CM.pool_config()
    lo, hi = cfg["bands"].get(kind, [0, 100])
    if kind.startswith("final"):
        frac = min(1.0, st["cum_wins"] / 80)
        lo = lo + (hi - lo) * float(cfg.get("final_win_pressure", 0.7)) * frac
    return [lo, hi]


def _pick_opponents(st, kind: str, rng: random.Random) -> list[dict]:
    teams = OS.pick("cm", _band(st, kind), rng, 2, {"champions_id": st["champions_id"]})
    if not teams:
        raise RuntimeError("CM opponent mix is empty (tools/opp_ctl.py mix cm)")
    return [dict(t, _snapshot=t.get("_pool")) for t in teams]


def _mood(rng, final: bool) -> int:
    if final:
        return 5
    m = CM.pool_config().get("mood", "random")
    return int(m) if str(m).isdigit() and 1 <= int(m) <= 5 else rng.randint(1, 5)


def _simulate(vid, st, team: list[dict], round_id: int, kind: str, race_num: int) -> dict:
    cm = st["champions_id"]
    st["race_seq"] += 1
    seed = random.Random(f"{st['run_id']}:{vid}:{st['race_seq']}").getrandbits(31)
    rng = random.Random(seed)
    ri, season, weather, ground = _race_condition(cm, round_id)
    final = round_id in (4, 5)
    opps = _pick_opponents(st, kind, rng)

    me = _user_head(vid)
    users, horses, trained = [], [], []
    room_id = int(f"{st['race_seq']:04d}{seed % 100000:05d}")
    for team_id, owner in enumerate([None] + opps, start=1):
        if owner is None:
            charas = [copy.deepcopy(c) for c in team]
            viewer, name, honor = social._wire_id(vid), me["name"], me["honor_data"]
        else:
            charas = [POOL.member_chara(owner, i, ri) for i in range(TEAM_SIZE)]
            viewer, name = POOL.trainer_viewer_id(owner["id"]), POOL.trainer_name(owner)
            honor = {"honor_id": 0}
        users.append({"room_id": room_id, "viewer_id": viewer, "name": name,
                      "honor_data": dict({"step": 0, "create_time": ""}, **honor), "team_id": team_id,
                      "entry_chara_array": [{"chara_id": c.get("chara_id") or c["card_id"] // 100,
                                             "race_cloth_id": c.get("race_cloth_id") or 0,
                                             "nick_name_id": c.get("nickname_id") or 0,
                                             "team_member_id": m} for m, c in enumerate(charas, start=1)],
                      "_team": owner["id"] if owner else None})
        for m, c in enumerate(charas, start=1):
            trained.append(copy.deepcopy(c))
            h = copy.deepcopy(c)
            h.update({"viewer_id": viewer, "trainer_name": name, "npc_type": NPC_TYPE_TRAINER,
                      "team_id": team_id, "team_member_id": m, "motivation": _mood(rng, final)})
            horses.append(h)

    n = len(horses)
    result = race_simulator.simulate_race(
        horses[0], horses[TEAM_SIZE:], ri, ground_condition=ground, weather=weather, season=season,
        seed=seed, entry_num=n, gate_assignment=rng.sample(range(n), n),
        extra_player_charas=horses[1:TEAM_SIZE], race_context={"race_type": RACE_TYPE_CHAMPIONS})
    if result is None:
        raise RuntimeError(f"CM race instance {ri} could not be simulated")
    rows, _ = practice_race.race_horse_rows(result)
    order = {i: r["finishOrder"] + 1 for i, r in enumerate(result["sim_results"])}
    places = [{"team_id": h["team_id"], "team_member_id": h["team_member_id"],
               "viewer_id": h["viewer_id"], "trained_chara_id": h.get("trained_chara_id"),
               "rank": order[i], "frame_order": rows[i]["frame_order"], "card_id": h["card_id"],
               "race_cloth_id": h.get("race_cloth_id") or 0, "rarity": h.get("rarity") or 3,
               "talent_level": h.get("talent_level") or 1, "final_grade": rows[i]["final_grade"],
               "popularity": rows[i]["popularity"]}
              for i, h in enumerate(horses)]
    team_best = {}
    for p in places:
        team_best[p["team_id"]] = min(team_best.get(p["team_id"], 99), p["rank"])
    team_order = sorted(team_best, key=team_best.get)
    return {
        "race_num": race_num, "round_id": round_id,
        "room_info": {"room_id": room_id, "user_entry_num": TEAM_SIZE,
                      "race_start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_now())),
                      "race_instance_id": ri, "season": season, "weather": weather,
                      "ground_condition": ground, "random_seed": seed,
                      "race_scenario": result["race_scenario"]},
        "room_user_array": users, "race_horse_data_array": rows, "trained_chara_array": trained,
        "result": 1 if team_order[0] == 1 else 0, "user_rank": team_best[1],
        "team_rank": {str(t): i + 1 for i, t in enumerate(team_order)}, "places": places,
    }


def _users_wire(race) -> list[dict]:
    return [{k: v for k, v in u.items() if not k.startswith("_")} for u in race["room_user_array"]]


def _race_start_payload(race) -> dict:
    return {"room_info": copy.deepcopy(race["room_info"]), "room_user_array": _users_wire(race),
            "race_horse_data_array": copy.deepcopy(race["race_horse_data_array"]),
            "trained_chara_array": copy.deepcopy(race["trained_chara_array"])}


def _race_result(race) -> dict:
    return {"race_num": race["race_num"], "result": race["result"], "user_rank": race["user_rank"]}


def _count_ranks(st, race) -> None:
    for p in race["places"]:
        if p["team_id"] != 1:
            continue
        rc = st["rank_count"].setdefault(str(p["trained_chara_id"]), [0, 0, 0, 0])
        rc[min(p["rank"], 4) - 1] += 1


def _finish_race(st, cur, race) -> None:
    cur["races"].append(race)
    _count_ranks(st, race)
    if race["result"]:
        st["cum_wins"] = min(80, st["cum_wins"] + 1)
    _sync_entry(st, cur)


def _seen_races(cur) -> list:
    races = cur.get("races") or []
    return races[:cur["viewed"]] if cur.get("simulated") else races


def _sync_entry(st, cur) -> None:
    rnd = st["rounds"][str(cur["round"])]
    entry = rnd["entries"][-1]
    seen = _seen_races(cur)
    entry["wins"] = sum(r["result"] for r in seen)
    entry["results"] = [_race_result(r) for r in seen]
    rnd["best"] = max(e["wins"] for e in rnd["entries"])


def _kind(st, round_id) -> str:
    return {1: "round1", 2: "round2_b", 3: "round2_a", 4: "final_b", 5: "final_a"}[round_id]


def _run_remaining(vid, st) -> None:
    cur = st["cur"]
    cur["pending"] = None
    while len(cur["races"]) < RACES_PER_ENTRY:
        race = _simulate(vid, st, cur["team"], cur["round_id"], _kind(st, cur["round_id"]), len(cur["races"]) + 1)
        _finish_race(st, cur, race)


def _tally(st, stage) -> None:
    cur = st.get("cur")
    if not cur or not cur.get("team"):
        return
    if cur["round"] < _round_of_stage(stage) or (stage.endswith("_tally") or stage.startswith("final")
                                                 or stage == "ended"):
        cur["pending"] = None
        cur["races"] = _seen_races(cur)
        cur["viewed"] = len(cur["races"])
        cur["simulated"] = True
        _sync_entry(st, cur)


def _need_run(payload):
    run = _run()
    if not run:
        return None, None, None, refuse(ResultCode.PARAM_ERROR)
    vid = _vid(payload)
    full = state_store.get_state(vid) or {}
    st = _cm_state(full, run)
    for team in ((st.get("cur") or {}).get("team"), (st.get("final") or {}).get("team"),
                 (st.get("reg_team") or {}).get("team")):
        for c in team or []:
            c["race_cloth_id"] = trained_chara.dress_for_card(c["card_id"], c.get("chara_id") or c["card_id"] // 100)
    stage = _stage(run)
    _tally(st, stage)
    return run, full, st, None


def _slim(st) -> None:
    last = st.get("last_round_entry")
    if isinstance(last, dict):
        if (st.get("final") or {}).get("team"):
            st.pop("last_round_entry")
        else:
            last.pop("races", None)


def _save(payload, full) -> None:
    if isinstance(full.get(STATE_KEY), dict):
        _slim(full[STATE_KEY])
    state_store.save_state(_vid(payload), full)


def _chara_top(c) -> dict:
    return {"card_id": c["card_id"], "chara_id": c.get("chara_id") or c["card_id"] // 100,
            "race_cloth_id": c.get("race_cloth_id") or 0, "nick_name_id": c.get("nickname_id") or 0,
            "team_member_id": c.get("team_member_id") or 0}


def _missions(payload, full, st) -> list:
    from . import missions
    ids = {r["id"] for r in M.query("SELECT id FROM mission_data WHERE condition_type=200012 "
                                     "AND condition_value_1=?", (st["champions_id"],))}
    return [m for m in missions.build_mission_list(_vid(payload), full) if m["mission_id"] in ids]


@registry.endpoint("champions/index")
def handle_index(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return _ok({"champions_info": None, "race_result_array": [], "entry_chara_array": [],
                    "last_entry_chara_array": [], "champions_mission_array": [], "reward_array": [],
                    "reward_summary_info": None, "first_check_round_flag": 0, "last_checked_time": 0,
                    "news_rank_count_info_array": []})
    stage = _stage(run)
    if not _active_entry(st, stage):
        _auto_free_entry(payload, full, st, stage)
    cur = st.get("cur") or {}
    team = cur.get("team") or (st.get("final") or {}).get("team") or []
    races = _seen_races(cur) if cur else []
    if stage.startswith("final") and st.get("final") and st["final"].get("race"):
        races = [st["final"]["race"]]
    first = 0 if st.get("_seen_round") == _round_id(st, stage) else 1
    st["_seen_round"] = _round_id(st, stage)
    data = {
        "champions_info": _champions_info(st, stage),
        "race_result_array": [_race_result(r) for r in races],
        "entry_chara_array": [_chara_top(c) for c in team],
        "last_entry_chara_array": copy.deepcopy(st["last_entry_chara"]),
        "champions_mission_array": _missions(payload, full, st),
        "reward_array": _reward_info_array(st["champions_id"]),
        "reward_summary_info": None,
        "first_check_round_flag": first,
        "last_checked_time": _now(),
        "news_rank_count_info_array": [
            {"trained_chara_id": int(k), "rank_count_1": v[0], "rank_count_2": v[1],
             "rank_count_3": v[2], "rank_count_unplaced": v[3]} for k, v in st["rank_count"].items()],
    }
    _save(payload, full)
    return _ok(data)


@registry.endpoint("champions/get_reward_array")
def handle_get_reward_array(payload):
    run = _run()
    return _ok({"reward_array": _reward_info_array(run["champions_id"]) if run else []})


@registry.endpoint("champions/select_league")
def handle_select_league(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    league = int(payload.get("league_type") or 0)
    if league not in (LEAGUE_OPEN, LEAGUE_GRADED) or st["league"]:
        return refuse(ResultCode.PARAM_ERROR)
    st["league"] = league
    items = []
    if not st["signup_claimed"]:
        st["signup_claimed"] = True
        rows = M.query("SELECT * FROM champions_entry_reward WHERE champions_id IN (0, ?)", (st["champions_id"],))
        items = [_item(r["item_category"], r["item_id"], r["item_num"]) for r in rows]
    summary = _grant_items(_vid(payload), full, items, False)
    stage = _stage(run)
    _save(payload, full)
    return _ok({"state": _info_state(st, stage), "league_type": league,
                "reward_array": items, "reward_summary_info": summary})


def _ticket_item(cm) -> int | None:
    r = M.query_one("SELECT id FROM item_data WHERE item_category=163 AND effect_type_1=?", (cm,))
    return r["id"] if r else None


def _open_entry(payload, full, st, stage, rnd):
    round_id = _round_id(st, stage)
    rk = str(rnd)
    st["rounds"].setdefault(rk, {"round_id": round_id, "entries": [], "best": -1, "claimed": False})
    st["rounds"][rk]["round_id"] = round_id
    st["rounds"][rk]["entries"].append({"wins": 0, "results": [], "team": [], "claimed": False})
    reg = st.get("reg_team") or {}
    team = copy.deepcopy(reg["team"]) if reg.get("round") == rnd else []
    st["cur"] = {"round": rnd, "round_id": round_id, "team": team, "races": [], "viewed": 0,
                 "pending": None, "simulated": False, "_vid": _vid(payload)}
    if team:
        st["rounds"][rk]["entries"][-1]["team"] = [c["trained_chara_id"] for c in team]
    counts = full.setdefault(ENTRY_COUNT_KEY, {})
    counts[str(st["champions_id"])] = counts.get(str(st["champions_id"]), 0) + 1


@registry.endpoint("champions/entry")
def handle_entry(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    rnd = _round_of_stage(stage)
    if stage not in ("round1", "round2") or not st["league"]:
        return refuse(ResultCode.PARAM_ERROR)
    if _unclaimed_round(st, rnd - 1):
        return refuse(ResultCode.PARAM_ERROR)
    round_id = _round_id(st, stage)
    d = _detail(st["champions_id"], st["league"], round_id)
    free_cap, paid_cap = (d["free_entry_number"], d["entry_number"]) if d else (1, 3)
    unlimited = bool(CM.options().get("unlimited_entries"))
    etype = int(payload.get("entry_type") or ENTRY_FREE)
    coin_info, items = None, []
    if etype == ENTRY_FREE:
        if st["free_entry_times"] >= free_cap and not unlimited:
            return refuse(ResultCode.PARAM_ERROR)
        st["free_entry_times"] += 1
    else:
        if st["entry_times"] >= paid_cap and not unlimited:
            return refuse(ResultCode.PARAM_ERROR)
        if etype == ENTRY_JEWEL:
            coin_info = shop.spend_carats(full, ENTRY_COST)
            if coin_info is None:
                return refuse(ResultCode.PARAM_ERROR)
        elif etype == ENTRY_TICKET:
            tid = _ticket_item(st["champions_id"])
            if not tid or shop._item_count(full, tid) < 1:
                return refuse(ResultCode.PARAM_ERROR)
            shop._add_item(full, tid, -1)
            items.append({"item_id": tid, "number": shop._item_count(full, tid)})
        else:
            return refuse(ResultCode.PARAM_ERROR)
        st["entry_times"] += 1
    _open_entry(payload, full, st, stage, rnd)
    if coin_info is None:
        wallet = full.get("coin_info_state") or {}
        coin_info = {"fcoin": wallet.get("fcoin", 0), "coin": wallet.get("coin", 0)}
    _save(payload, full)
    return _ok({"entry_times": st["entry_times"], "free_entry_times": _free_left(st, stage),
                "coin_info": coin_info, "item_info_array": items, "state": _info_state(st, stage)})


def _snapshot_team(vid, entries, league) -> list[dict] | None:
    roster = {c.get("trained_chara_id"): c for c in trained_chara._get_or_seed_roster(vid) or []}
    team = []
    for i, e in enumerate(entries or []):
        base = roster.get(e.get("trained_chara_id"))
        if base is None:
            return None
        if league == LEAGUE_OPEN and (base.get("rank") or 1) > OPEN_LEAGUE_RANK_MAX:
            return None
        c = copy.deepcopy(base)
        c["race_cloth_id"] = trained_chara.dress_for_card(c["card_id"], c.get("chara_id") or c["card_id"] // 100)
        if e.get("running_style"):
            c["running_style"] = e["running_style"]
        c["team_member_id"] = e.get("team_member_id") or i + 1
        c["viewer_id"] = "<redacted>"
        team.append(c)
    if len(team) != TEAM_SIZE or len({c["trained_chara_id"] for c in team}) != TEAM_SIZE:
        return None
    return sorted(team, key=lambda c: c["team_member_id"])


@registry.endpoint("champions/set_entry_chara")
def handle_set_entry_chara(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    vid = _vid(payload)
    entries = payload.get("entry_chara_array") or []
    team = _snapshot_team(vid, entries, st["league"])
    if team is None:
        return refuse(ResultCode.PARAM_ERROR)
    st["last_entry_chara"] = [{"trained_chara_id": e["trained_chara_id"], "running_style": e.get("running_style") or 0,
                               "team_member_id": e.get("team_member_id") or i + 1} for i, e in enumerate(entries)]
    if stage == "final_entry" and _final_round_id(st):
        st["final"] = dict(st.get("final") or {}, team=team)
    else:
        cur = st.get("cur")
        rnd = _round_of_stage(stage)
        if stage not in ("round1", "round2"):
            return refuse(ResultCode.PARAM_ERROR)
        st["reg_team"] = {"round": rnd, "team": team}
        if not cur or cur["round"] != rnd or len(cur["races"]) >= RACES_PER_ENTRY:
            if not _auto_free_entry(payload, full, st, stage) and not _auto_paid_entry(payload, full, st, stage):
                _save(payload, full)
                return _ok({"state": _info_state(st, stage)})
            cur = st["cur"]
        cur["team"] = team
        cur["_vid"] = vid
        st["rounds"][str(cur["round"])]["entries"][-1]["team"] = [c["trained_chara_id"] for c in team]
    _save(payload, full)
    return _ok({"state": _info_state(st, stage)})


@registry.endpoint("champions/lobby")
def handle_lobby(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    cur = st.get("cur") or {}
    return _ok({"race_result_array": [_race_result(r) for r in (_seen_races(cur) if cur else [])]})


def _auto_free_entry(payload, full, st, stage) -> bool:
    rnd = _round_of_stage(stage)
    cur = st.get("cur")
    if stage not in ("round1", "round2") or (st.get("reg_team") or {}).get("round") != rnd:
        return False
    if cur and cur["round"] == rnd and not len(cur["races"]) >= RACES_PER_ENTRY:
        return False
    if _unclaimed_round(st, rnd - 1):
        return False
    d = _detail(st["champions_id"], st["league"], _round_id(st, stage))
    free_cap = d["free_entry_number"] if d else 1
    if st["free_entry_times"] >= free_cap and not CM.options().get("unlimited_entries"):
        return False
    st["free_entry_times"] += 1
    _open_entry(payload, full, st, stage, rnd)
    return True


def _auto_paid_entry(payload, full, st, stage) -> bool:
    rnd = _round_of_stage(stage)
    if stage not in ("round1", "round2") or (st.get("reg_team") or {}).get("round") != rnd:
        return False
    if _unclaimed_round(st, rnd - 1):
        return False
    d = _detail(st["champions_id"], st["league"], _round_id(st, stage))
    paid_cap = d["entry_number"] if d else 3
    if st["entry_times"] >= paid_cap and not CM.options().get("unlimited_entries"):
        return False
    tid = _ticket_item(st["champions_id"])
    if tid and shop._item_count(full, tid) >= 1:
        shop._add_item(full, tid, -1)
    elif shop.spend_carats(full, ENTRY_COST) is None:
        return False
    st["entry_times"] += 1
    _open_entry(payload, full, st, stage, rnd)
    return True


def _active_entry(st, stage):
    cur = st.get("cur")
    if not cur or not cur.get("team") or cur["round"] != _round_of_stage(stage) or stage.endswith("_tally"):
        return None
    return cur


@registry.endpoint("champions/race_entry")
def handle_race_entry(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    if not _active_entry(st, stage) and _auto_free_entry(payload, full, st, stage):
        _save(payload, full)
    cur = _active_entry(st, stage)
    if cur is None or len(cur["races"]) >= RACES_PER_ENTRY:
        return refuse(ResultCode.PARAM_ERROR)
    if cur.get("pending") is None:
        race = _simulate(_vid(payload), st, cur["team"], cur["round_id"], _kind(st, cur["round_id"]),
                         len(cur["races"]) + 1)
        cur["pending"] = race
    race = cur["pending"]
    _save(payload, full)
    info = dict(race["room_info"], race_scenario="")
    return _ok({"room_info_array": [info], "room_user_array": _users_wire(race), "state": ST_READY})


@registry.endpoint("champions/poll")
def handle_poll(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    cur = st.get("cur") or {}
    race = cur.get("pending")
    return _ok({"room_user_array": _users_wire(race) if race else [],
                "state": ST_READY if race else _info_state(st, _stage(run))})


@registry.endpoint("champions/cancel")
def handle_cancel(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    if st.get("cur"):
        st["cur"]["pending"] = None
    _save(payload, full)
    return _ok({"is_success": 1})


@registry.endpoint("champions/paddock_index")
def handle_paddock_index(payload):
    run = _run()
    if not run:
        return _ok({"news_title_id": 0})
    r = M.query_one("SELECT id FROM champions_news_title WHERE round_id IN (0, 1) ORDER BY RANDOM() LIMIT 1")
    return _ok({"news_title_id": r["id"] if r else 0})


def _replay_target(st):
    cur = st.get("cur") or {}
    if cur.get("pending") is not None:
        return cur["pending"], "pending"
    if cur.get("simulated") and cur["viewed"] < len(cur["races"]):
        return cur["races"][cur["viewed"]], "replay"
    if cur.get("races"):
        return cur["races"][-1], "last"
    return None, None


@registry.endpoint("champions/race_start")
def handle_race_start(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    race, _ = _replay_target(st)
    if race is None:
        return refuse(ResultCode.PARAM_ERROR)
    return _ok(_race_start_payload(race))


@registry.endpoint("champions/race_end")
def handle_race_end(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    cur = st.get("cur")
    race, kind = _replay_target(st)
    if cur and kind == "pending":
        cur["pending"] = None
        _finish_race(st, cur, race)
        cur["viewed"] = len(cur["races"])
    elif cur and kind == "replay":
        cur["viewed"] += 1
        _sync_entry(st, cur)
    _save(payload, full)
    return _ok({"next_tier": _next_tier(st, stage), "state": _info_state(st, stage)})


@registry.endpoint("champions/all_race_exec")
def handle_all_race_exec(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    if not _active_entry(st, stage) and _auto_free_entry(payload, full, st, stage):
        _save(payload, full)
    cur = _active_entry(st, stage)
    if cur is None:
        return refuse(ResultCode.PARAM_ERROR)
    start_viewed = len(cur["races"]) if not cur.get("simulated") else cur["viewed"]
    if cur.get("pending") is not None:
        race = cur["pending"]
        cur["pending"] = None
        _finish_race(st, cur, race)
    _run_remaining(_vid(payload), st)
    cur["simulated"] = True
    cur["viewed"] = min(start_viewed, len(cur["races"]))
    _sync_entry(st, cur)
    _save(payload, full)
    return _ok({"state": ST_SIMULATED})


@registry.endpoint("champions/round_end")
def handle_round_end(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    cur = st.get("cur")
    if stage in ("round1", "round2") and cur and cur["round"] == _round_of_stage(stage):
        cur["pending"] = None
        cur["races"] = _seen_races(cur)
        cur["viewed"] = len(cur["races"])
        cur["simulated"] = True
        _sync_entry(st, cur)
        st["last_round_entry"] = cur
        st["cur"] = None
    key = _unclaimed_round(st, 2 if not stage.startswith("round1") else 1)
    if key is None:
        return _ok({"reward_array": [], "reward_summary_info": _empty_summary(),
                    "state": _info_state(st, stage)})
    rnd = st["rounds"][key]
    items = []
    for e in rnd["entries"]:
        if not _entry_claimed(rnd, e):
            e["claimed"] = True
            items += _qual_items(rnd["round_id"], max(0, e["wins"]))
    rnd["claimed"] = True
    summary = _grant_items(_vid(payload), full, items, bool(payload.get("is_present")))
    if st.get("cur") and str(st["cur"]["round"]) == key:
        st["last_round_entry"] = st["cur"]
        st["cur"] = None
    _save(payload, full)
    return _ok({"reward_array": items, "reward_summary_info": summary, "state": _info_state(st, stage)})


def _final_team(st):
    fin = st.get("final") or {}
    if fin.get("team"):
        return fin["team"]
    for key in ("2", "1"):
        last = (st.get("last_round_entry") or {}) if key == "2" else {}
        if last.get("team"):
            return last["team"]
    cur = st.get("cur") or {}
    return cur.get("team") or []


def _ensure_final_race(vid, st) -> dict | None:
    fin_round = _final_round_id(st)
    if not fin_round:
        return None
    fin = st.setdefault("final", {}) or {}
    st["final"] = fin
    if fin.get("race"):
        return fin
    team = _final_team(st)
    if len(team) != TEAM_SIZE:
        return None
    fin["team"] = team
    race = _simulate(vid, st, team, fin_round, _kind(st, fin_round), 1)
    fin.update(race=race, round_id=fin_round, rank=race["team_rank"]["1"], viewed=False, claimed=False)
    rng = random.Random(race["room_info"]["random_seed"])
    total = st["cum_wins"] + sum(max(0, min(80, round(st["cum_wins"] * rng.uniform(0.8, 1.2)))) for _ in range(2))
    fin["total_entry_num"] = max(3, 100 * total + rng.randint(-60, 60))
    return fin


@registry.endpoint("champions/final_lobby")
def handle_final_lobby(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    results, own = [], []
    if stage in ("final_race", "final_result", "ended"):
        fin = _ensure_final_race(_vid(payload), st)
        if fin and fin.get("race") and fin.get("viewed"):
            race = fin["race"]
            results = [_race_result(race)]
            own = [{"viewer_id": p["viewer_id"], "team_member_id": p["team_member_id"], "rank": p["rank"]}
                   for p in race["places"] if p["team_id"] == 1]
    _save(payload, full)
    return _ok({"race_result_array": results, "own_chara_result_array": own})


@registry.endpoint("champions/final_race_start")
def handle_final_race_start(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    if stage not in ("final_result", "ended"):
        return refuse(ResultCode.PARAM_ERROR)
    fin = _ensure_final_race(_vid(payload), st)
    if not fin:
        return refuse(ResultCode.PARAM_ERROR)
    _save(payload, full)
    data = _race_start_payload(fin["race"])
    data.update(state=_info_state(st, stage), played_live_flag=int(bool(fin.get("live"))))
    return _ok(data)


@registry.endpoint("champions/final_race_end")
def handle_final_race_end(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    fin = _ensure_final_race(_vid(payload), st)
    if not fin:
        return refuse(ResultCode.PARAM_ERROR)
    if not fin.get("viewed"):
        fin["viewed"] = True
        _count_ranks(st, fin["race"])
    _save(payload, full)
    return _ok({"state": _info_state(st, stage), "total_entry_num": fin["total_entry_num"]})


@registry.endpoint("champions/final_race_ranking")
def handle_final_race_ranking(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    fin = _ensure_final_race(_vid(payload), st)
    if not fin:
        return _ok({"total_num": 0, "ranking_info_array": [], "ranking_user_info_array": [],
                    "ranking_entry_chara_info_array": []})
    race = fin["race"]
    ranking, users, charas = [], [], []
    for u in race["room_user_array"]:
        ranking.append({"viewer_id": u["viewer_id"], "last_round_id": fin["round_id"],
                        "rank": race["team_rank"][str(u["team_id"])], "is_same_final": 1})
        users.append({"viewer_id": u["viewer_id"], "name": u["name"], "honor_data": u["honor_data"],
                      "leader_chara_id": u["entry_chara_array"][0]["chara_id"],
                      "leader_chara_dress_id": u["entry_chara_array"][0]["race_cloth_id"]})
    for p in race["places"]:
        charas.append({"viewer_id": p["viewer_id"], "trained_chara_id": p["trained_chara_id"],
                       "team_member_id": p["team_member_id"], "card_id": p["card_id"],
                       "final_grade": p["final_grade"], "rarity": p["rarity"], "talent_level": p["talent_level"],
                       "race_cloth_id": p["race_cloth_id"],
                       "running_style": next((h.get("running_style", 1) for h in race["trained_chara_array"]
                                              if h.get("trained_chara_id") == p["trained_chara_id"]), 1)})
    _save(payload, full)
    return _ok({"total_num": fin["total_entry_num"], "ranking_info_array": sorted(ranking, key=lambda r: r["rank"]),
                "ranking_user_info_array": users, "ranking_entry_chara_info_array": charas})


@registry.endpoint("champions/final_round_end")
def handle_final_round_end(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    stage = _stage(run)
    vid = _vid(payload)
    fin = _ensure_final_race(vid, st)
    if not fin or fin.get("claimed") or not fin.get("viewed") or stage not in ("final_result", "ended"):
        return _ok({"reward_array": [], "reward_summary_info": _empty_summary(), "state": _info_state(st, stage)})
    from . import user_profile
    owned = set(user_profile._honor_state(full, vid).get("honors") or {})
    items = _final_items(st["champions_id"], fin["round_id"], fin["rank"], owned)
    summary = _grant_items(vid, full, items, bool(payload.get("is_present")))
    fin["claimed"] = True
    _save(payload, full)
    return _ok({"reward_array": items, "reward_summary_info": summary, "state": _info_state(st, stage)})


def _all_races(st) -> list[dict]:
    out = list((st.get("cur") or {}).get("races") or [])
    if (st.get("final") or {}).get("race"):
        out.append(st["final"]["race"])
    return out


def _race_by_num(st, num) -> dict | None:
    races = _all_races(st)
    return next((r for r in races if r["race_num"] == int(num or 0)), races[-1] if races else None)


@registry.endpoint("champions/get_race_result_chart")
def handle_get_race_result_chart(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    race = _race_by_num(st, payload.get("race_num"))
    if race is None:
        return _ok({"room_user_array": [], "race_horse_data_array": [], "trained_chara_array": []})
    p = _race_start_payload(race)
    return _ok({"room_user_array": p["room_user_array"], "race_horse_data_array": p["race_horse_data_array"],
                "trained_chara_array": p["trained_chara_array"]})


@registry.endpoint("champions/get_race_history_info")
def handle_get_race_history_info(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    hist, own = [], []
    for race in _all_races(st):
        info = dict(race["room_info"], race_scenario="")
        hist.append({"race_num": race["race_num"], "room_info": info, "race_history_chara_result_array": [
            {k: p[k] for k in ("viewer_id", "team_member_id", "card_id", "rarity", "final_grade",
                               "talent_level", "race_cloth_id", "rank")} | {"team_id": p["team_id"]}
            for p in race["places"]]})
    for race in _all_races(st)[-1:]:
        own = [{k: p[k] for k in ("viewer_id", "team_member_id", "card_id", "rarity", "final_grade",
                                  "talent_level", "race_cloth_id", "rank")} | {"team_id": p["team_id"]}
               for p in race["places"] if p["team_id"] == 1]
    return _ok({"race_history_info_array": hist, "own_race_history_chara_array": own})


@registry.endpoint("champions/get_ranking_chara_info")
def handle_get_ranking_chara_info(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    target, member = int(payload.get("target_viewer_id") or 0), int(payload.get("team_member_id") or 0)
    for race in reversed(_all_races(st)):
        for p, tc in zip(race["places"], race["trained_chara_array"]):
            if p["team_member_id"] == member and (p["viewer_id"] == target or (p["team_id"] == 1 and not target)):
                return _ok({"trained_chara": copy.deepcopy(tc)})
        for p, tc in zip(race["places"], race["trained_chara_array"]):
            if p["team_member_id"] == member and p["team_id"] == 1:
                return _ok({"trained_chara": copy.deepcopy(tc)})
    return refuse(ResultCode.PARAM_ERROR)


@registry.endpoint("champions/get_news_info")
def handle_get_news_info(payload):
    run, full, st, err = _need_run(payload)
    if err:
        return err
    race = _race_by_num(st, payload.get("race_num"))
    vid = social._wire_id(_vid(payload))
    total = sum(e["wins"] for r in st["rounds"].values() for e in r["entries"])
    races = sum(len(e["results"]) for r in st["rounds"].values() for e in r["entries"])
    chara_results = []
    if race:
        for p in race["places"]:
            rc = st["rank_count"].get(str(p["trained_chara_id"]), [0, 0, 0, 0]) if p["team_id"] == 1 else [0, 0, 0, 0]
            chara_results.append({"viewer_id": p["viewer_id"], "team_id": p["team_id"],
                                  "team_member_id": p["team_member_id"], "rank_count_1": rc[0],
                                  "rank_count_2": rc[1], "rank_count_3": rc[2], "rank_count_unplaced": rc[3],
                                  "race_win_5": 0, "news_chara_race_history_array": []})
    return _ok({"news_race_id_array": [],
                "news_trainer_info": {"viewer_id": vid, "total_win": total, "total_race_count": races,
                                      "race_win_5": 0, "news_trainer_id_array": []},
                "news_chara_info_array": [], "news_chara_result_info_array": chara_results})


@registry.endpoint("champions/get_news_win_info")
def handle_get_news_win_info(payload):
    return _ok({"news_win_info": {"news_win_title_id": 0, "news_win_comment_id": 0, "news_win_id_array": []},
                "news_chara_result_info": None})


@registry.endpoint("champions_stamp/poll")
def handle_stamp_poll(payload):
    return _ok({"stamp_array": []})


@registry.endpoint("champions_stamp/send_stamp")
def handle_stamp_send(payload):
    return _ok({})
