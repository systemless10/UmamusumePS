
from __future__ import annotations

import base64
import copy
import functools
import gzip
import logging
import random
import time

import numpy as np

from .. import config, custom_support, master_data, patch
from .. import state as state_store
from ..result_codes import ResultCode, refuse
from ..simulation.scenario_format import decode_scenario
from . import daily_races, heroes, race_mobs, registry, stamina, trained_chara

log = logging.getLogger("uma-server")

KEY = "challenge_match_state"
POINT_ITEM_ID = 159
POINT_ITEM_CATEGORY = 166
GAUGE_MAX = 100
GAUGE_PER_HARD = 50
EXTREME_AFTER_VH = 3
CHALLENGE_SECONDS = 600
RETRY_BONUS_PCT = (0, 25, 50, 75, 100, 150)
TYPE_HARD, TYPE_CHALLENGE = 0, 1
DIFF_EXTREME = 5
ST_NONE, ST_SELECTED, ST_ITEMS, ST_RUN, ST_RESULT = 0, 2, 3, 4, 5
OPEN_CLOSE, OPEN_CAN, OPEN_OPENED = 0, 1, 2


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@functools.lru_cache(maxsize=8)
def _events_in(_pack: str) -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM challenge_match_data ORDER BY start_date"))


@functools.lru_cache(maxsize=8)
def _races_in(_pack: str) -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM challenge_match_race ORDER BY challenge_match_id, disp_order"))


def _races(cm_id: int) -> list:
    return [r for r in _races_in(custom_support.CURRENT_PACK.get()) if r["challenge_match_id"] == cm_id]


def _race(race_id):
    if not isinstance(race_id, int) or isinstance(race_id, bool):
        return None
    return next((r for r in _races_in(custom_support.CURRENT_PACK.get()) if r["id"] == race_id), None)


@functools.lru_cache(maxsize=64)
def _boss(boss_id: int):
    row = master_data.query_one("SELECT * FROM challenge_match_boss_npc WHERE id=?", (boss_id,))
    return dict(row) if row else None


@functools.lru_cache(maxsize=1)
def _raw_points() -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM challenge_match_raw_point ORDER BY id"))


@functools.lru_cache(maxsize=64)
def _distance(race_instance_id: int) -> int:
    row = master_data.query_one(
        "SELECT rcs.distance AS d FROM race_instance ri JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set rcs ON rcs.id=r.course_set WHERE ri.id=?", (race_instance_id,))
    return row["d"] if row else 2000


@functools.lru_cache(maxsize=512)
def _skill_meta(skill_id: int) -> tuple:
    row = master_data.query_one("SELECT rarity, group_rate FROM skill_data WHERE id=?", (skill_id,))
    return (row["rarity"], row["group_rate"]) if row else (0, -1)


def active_event():
    events = _events_in(custom_support.CURRENT_PACK.get())
    forced = config.get("challenge_match_id")
    if forced is not None:
        return next((e for e in events if e["challenge_match_id"] == int(forced)), None)
    now = patch._servertime()
    return next((e for e in events if (e["start_date"] or 0) <= now <= (e["end_date"] or 0)), None)


def _racing_open(event) -> bool:
    if event is None:
        return False
    if config.get("challenge_match_id") is not None:
        return True
    return patch._servertime() <= (event["ending_date"] or 0)


def _state(full_state: dict, cm_id: int) -> dict:
    st = full_state.setdefault(KEY, {})
    if st.get("cm_id") != cm_id:
        st.clear()
        st.update(cm_id=cm_id, match_point=0, cleared=[], gauge=0,
                  open_type=OPEN_CLOSE, challenge_race_id=0, end_date=0,
                  extreme_count=0, retry=0)
    _expire(st)
    return st


BONUS_SPARK_TOP = 5
BONUS_MAX_LEVEL = 5


def _bonus_factor(event) -> int:
    return int((event or {}).get("bonus_factor_id") or 0)


def _bonus_level(full_state: dict, player: dict, event) -> int:
    fid = _bonus_factor(event)
    if not fid:
        return 0
    roster = {r.get("trained_chara_id"): r for r in full_state.get(trained_chara.ROSTER_KEY) or []}
    best = 0
    for k in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        parent = roster.get(player.get(k))
        for f in (parent or {}).get("factor_info_array") or []:
            if f.get("factor_id") == fid:
                best = max(best, int(f.get("level") or 1))
    return min(best, BONUS_MAX_LEVEL)


@functools.lru_cache(maxsize=8)
def _bonus_pct(level: int) -> int:
    if not level:
        return 0
    r = master_data.query_one("SELECT float_ability_value_coef FROM skill_level_value "
                              "WHERE ability_type=501 AND level=?", (level,))
    return int(r[0]) // 100 if r else 0


def _grant_bonus_spark(full_state: dict, trained_chara_id, event, bonus_level: int) -> None:
    fid = _bonus_factor(event)
    rec = next((r for r in full_state.get(trained_chara.ROSTER_KEY) or []
                if r.get("trained_chara_id") == trained_chara_id), None)
    if not fid or rec is None:
        return
    factors = rec.setdefault("factor_info_array", [])
    if any(f.get("factor_id") == fid for f in factors):
        return
    factors.append({"factor_id": fid, "level": min(BONUS_MAX_LEVEL, bonus_level + 1)})
    log.info("challenge_match: Carnival Bonus spark Lv%d -> %s", min(BONUS_MAX_LEVEL, bonus_level + 1), trained_chara_id)


def runs_in(instances: set, start: int | None, end: int | None, full_state: dict) -> int:
    runs = (full_state.get(KEY) or {}).get("runs") or []
    return sum(1 for ts, inst in runs if inst in instances
               and (start is None or ts >= start) and (end is None or ts <= end))


def _expire(st: dict) -> None:
    if st.get("open_type") == OPEN_OPENED and st.get("end_date", 0) < patch._servertime():
        pending = st.get("pending") or {}
        if pending.get("race_id") != st.get("challenge_race_id"):
            _close_challenge(st)


def _close_challenge(st: dict) -> None:
    st.update(open_type=OPEN_CLOSE, challenge_race_id=0, end_date=0)


def _end_str(stamp: int) -> str:
    return time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(stamp)) if stamp else ""


def _retry_pct(st: dict) -> int:
    return RETRY_BONUS_PCT[min(st.get("retry", 0), len(RETRY_BONUS_PCT) - 1)]


def _boss_data(row) -> dict:
    boss = _boss(row["challenge_match_race_boss_npc_id"])
    return daily_races.index_boss_data(0, boss=boss) if boss else {}


def _records(event, st: dict) -> list:
    out = []
    for r in _races(event["challenge_match_id"]):
        out.append({"challenge_match_race_id": r["id"],
                    "is_cleared": 1 if r["id"] in st["cleared"] else 0,
                    "boss_data": _boss_data(r),
                    "race_reward_limit_array": []})
    return out


def load_info(full_state: dict) -> dict:
    event = active_event()
    if event is None:
        return {"challenge_match_id": 0, "match_point": 0,
                "resume_state": 0, "resume_trained_chara_id": 0}
    st = _state(full_state, event["challenge_match_id"])
    pending = st.get("pending") or {}
    return {"challenge_match_id": event["challenge_match_id"],
            "match_point": st["match_point"],
            "resume_state": pending.get("state", 0),
            "resume_trained_chara_id": pending.get("trained_chara_id", 0)}


def _load(payload):
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    event = active_event()
    if event is None:
        return viewer_id, full_state, None, None
    return viewer_id, full_state, event, _state(full_state, event["challenge_match_id"])


@registry.endpoint("challenge_match/index")
def handle_index(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    if event is None:
        return refuse(ResultCode.CHALLENGE_MATCH_NOT_IN_TERM)
    pending = st.get("pending") or {}
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "challenge_match_id": event["challenge_match_id"],
        "match_point": st["match_point"],
        "challenge_match_race_record_array": _records(event, st),
        "challenge_race_gauge_value": st["gauge"],
        "challenge_race_extreme_count": st["extreme_count"],
        "challenge_race_open_type": st["open_type"],
        "challenge_race_challenge_match_race_id": st["challenge_race_id"],
        "challenge_race_end_date": _end_str(st["end_date"]),
        "challenge_race_retry_bonus": _retry_pct(st),
        "resume_challenge_match_race_id": pending.get("race_id", 0),
        "resume_state": pending.get("state", 0),
        "resume_trained_chara_id": pending.get("trained_chara_id", 0),
        "mission_badge_num": 0,
        "notify_gauge_max": 1 if st["open_type"] == OPEN_CAN else 0,
    })


@registry.endpoint("challenge_match/challenge_race_open")
@registry.endpoint("challenge_match/race_open")
def handle_race_open(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    if not _racing_open(event):
        return refuse(ResultCode.CHALLENGE_MATCH_NOT_IN_TERM)
    if st["open_type"] != OPEN_CAN:
        return refuse()
    pool = [r for r in _races(event["challenge_match_id"]) if r["race_type"] == TYPE_CHALLENGE]
    if not pool:
        return refuse()
    extreme = [r for r in pool if r["difficulty"] >= DIFF_EXTREME]
    if extreme and st["extreme_count"] >= EXTREME_AFTER_VH:
        pick = extreme[0]
    else:
        pick = random.choices(pool, weights=[max(0, r["race_appearance_rate"] or 0) for r in pool])[0]
    st["extreme_count"] = 0 if pick["difficulty"] >= DIFF_EXTREME else st["extreme_count"] + 1
    st.update(gauge=0, open_type=OPEN_OPENED, challenge_race_id=pick["id"],
              end_date=patch._servertime() + CHALLENGE_SECONDS)
    state_store.save_state(viewer_id, full_state)
    return _ok({"challenge_match_race_id": pick["id"],
                "end_date": _end_str(st["end_date"]),
                "challenge_race_extreme_count": st["extreme_count"]})


def _playable(st: dict, row) -> int | None:
    if row["race_type"] != TYPE_CHALLENGE:
        return None
    if st["open_type"] != OPEN_OPENED or st["challenge_race_id"] != row["id"]:
        return ResultCode.CHALLENGE_MATCH_CHALLENGE_RACE_TIME_OUT
    return None


def _opponents(row, viewer_id, player_tcid) -> list:
    boss = _boss(row["challenge_match_race_boss_npc_id"])
    if boss is None:
        return []
    rng = random.Random(daily_races._field_seed(viewer_id, row["id"]))
    want = daily_races._entry_num(row["race_instance_id"]) - 2
    mobs = race_mobs.preset_mobs(max(0, want), rng, avoid_ids=(player_tcid, boss["id"]))
    return [daily_races._boss_chara(boss)] + mobs


@registry.endpoint("challenge_match/race_entry")
def handle_race_entry(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    row = _race(payload.get("challenge_match_race_id"))
    if not _racing_open(event) or row is None or row["challenge_match_id"] != event["challenge_match_id"]:
        return refuse(ResultCode.CHALLENGE_MATCH_NOT_IN_TERM)
    code = _playable(st, row)
    if code is not None:
        return refuse(code)
    tcid = payload.get("trained_chara_id")
    player = daily_races._find_veteran(viewer_id, tcid)
    if player is None:
        return refuse()
    opponents = _opponents(row, viewer_id, tcid)
    if not opponents:
        return refuse()
    horses = [copy.deepcopy(player)] + opponents
    entry_num = daily_races._entry_num(row["race_instance_id"])
    gates = random.sample(range(max(entry_num, len(horses))), len(horses))
    wire = daily_races._race_horse_array(horses, gates)
    seed = random.randint(0, 2**31 - 1)
    season, weather, ground = row["season"] or 1, row["weather"] or 1, row["ground"] or 1
    st["pending"] = {
        "race_id": row["id"], "trained_chara_id": tcid, "state": ST_SELECTED,
        "item_id_array": [], "result_rank": None,
        "season": season, "weather": weather, "ground": ground,
        "gate_assignment": gates, "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "race_horse_data_array": copy.deepcopy(wire), "race_scenario": None,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok({"race_horse_data_array": wire, "season": season, "weather": weather,
                "ground_condition": ground, "random_seed": seed,
                "race_instance_id": row["race_instance_id"], "state": ST_SELECTED,
                "trained_chara_id": tcid})


@registry.endpoint("challenge_match/reflect_item_effect")
def handle_reflect_item_effect(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    pending = (st or {}).get("pending")
    if not pending or pending["race_id"] != payload.get("challenge_match_race_id"):
        return refuse()
    row = _race(pending["race_id"])
    if not pending.get("paid"):
        if stamina.RP.spend(full_state, row["cost_rp"] or 0) is None:
            return refuse(ResultCode.NOT_ENOUGH_ERROR)
        pending["paid"] = True
    used = daily_races.apply_race_items(full_state, pending, payload.get("item_id_array"))
    pending["state"] = ST_ITEMS
    state_store.save_state(viewer_id, full_state)
    reply = daily_races.reflect_reply(full_state, pending, 0, used)
    reply["item_info_array"] = [i for i in reply["item_info_array"] if i["item_id"]]
    reply["rp_info"] = stamina.rp_info(full_state)
    reply["state"] = ST_ITEMS
    return _ok(reply)


_STATS = ("speed", "stamina", "power", "pow", "guts", "wiz")


@registry.endpoint("challenge_match/race_start")
def handle_race_start(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    pending = (st or {}).get("pending")
    if not pending or not pending.get("paid"):
        return refuse()
    row = _race(pending["race_id"])
    player = daily_races._find_veteran(viewer_id, pending["trained_chara_id"])
    if row is None or player is None:
        return refuse()
    player = copy.deepcopy(player)
    if pending.get("motivation"):
        player["motivation"] = pending["motivation"]
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    pct = _retry_pct(st) if row["race_type"] == TYPE_CHALLENGE else 0
    bonus_lvl = _bonus_level(full_state, player, event)
    pending["bonus_level"] = bonus_lvl
    pct += _bonus_pct(bonus_lvl)
    if pct:
        for k in _STATS:
            if isinstance(player.get(k), (int, float)):
                player[k] = int(player[k] * (100 + pct) / 100)
    outcome = daily_races._run_race(
        player, _opponents(row, viewer_id, pending["trained_chara_id"]),
        row["race_instance_id"], pending["season"], pending["weather"], pending["ground"],
        gate_assignment=pending.get("gate_assignment"), race_type=12)
    pending.update(result_rank=outcome["rank"], race_scenario=outcome["race_scenario"],
                   state=ST_RUN)
    state_store.save_state(viewer_id, full_state)
    return _ok({"race_scenario": outcome["race_scenario"], "state": ST_RUN,
                "running_style": payload.get("running_style") or player.get("running_style") or 1})


def border_time(distance: int) -> float:
    f = np.float32
    d = f(distance)
    base = f(20) - (d - f(2000)) / f(1000)
    last = d - f(16) * d / f(24)
    return float((last / f(21) + (d - last) / base) * f(1.18))


def _phase(dist: float, d: int) -> int:
    if dist < d / 6:
        return 0
    if dist < d * 2 / 3:
        return 1
    if dist < d * 5 / 6:
        return 2
    return 3


def _score(scenario_b64: str, player: dict, popularity: int, difficulty: int,
           distance: int, slot: int = 0) -> list:
    rows = _raw_points()
    details: list[dict] = []

    def add(match):
        row = next((r for r in rows if match(r)), None)
        if row is None:
            return
        e = next((e for e in details if e["raw_point_id"] == row["id"]), None)
        if e is None:
            e = {"raw_point_id": row["id"], "num": 0, "point": 0, "point_bonus": 0}
            details.append(e)
        e["point"] += row["point"]
        e["num"] += 1

    sc = decode_scenario(gzip.decompress(base64.b64decode(scenario_b64)))
    results, frames = sc["results"], sc["frames"]
    me = results[slot]
    order = me.finish_order

    if me.start_delay_time < 0.02:
        add(lambda r: r["condition_type"] == 7 and r["condition_value_1"] == 1)

    levels = {s.get("skill_id"): s.get("level") or 1 for s in player.get("skill_array") or []}
    timed = []
    for ev in sc["events"]:
        if ev["type"] == 3 and ev["params"] and ev["params"][0] == slot:
            timed.append((ev["t"], "skill", ev["params"][1]))
    prev = 0
    for t, horses in frames:
        if not horses:
            continue
        ph = _phase(horses[slot].distance, distance)
        if ph != prev:
            style = me.running_style
            same = [i for i, h in enumerate(horses) if results[i].running_style == style]
            if max(same, key=lambda i: horses[i].distance) == slot:
                timed.append((t, "keep", ph))
            prev = ph
    timed.sort(key=lambda x: x[0])
    for _, kind, v in timed:
        if kind == "skill":
            rarity, group_rate = _skill_meta(v)
            if group_rate is None or group_rate < 0:
                continue
            lv = levels.get(v, 1)
            add(lambda r: r["condition_type"] == 8 and r["condition_value_1"] == rarity
                and r["condition_value_2"] in (0, lv))
        else:
            add(lambda r: r["condition_type"] == 9 and r["condition_value_1"] == v)

    diff = int(np.floor(border_time(distance) * 10)) - int(np.floor(me.finish_time * 10))
    if diff >= 0:
        add(lambda r: r["condition_type"] == 4 and r["condition_value_1"] <= diff < r["condition_value_2"])
    add(lambda r: r["condition_type"] == 3 and r["condition_value_1"] == order + 1)
    add(lambda r: r["condition_type"] == 2 and r["condition_value_1"] >= order + 1
        and r["condition_value_2"] <= popularity)
    add(lambda r: r["condition_type"] == 12 and r["condition_value_1"] == difficulty
        and r["condition_value_2"] >= order + 1)
    if order == 0:
        second = next((r for r in results if r.finish_order == 1), None)
        if second is not None:
            cat = heroes.horse_length(second.finish_diff_time)
            add(lambda r: r["condition_type"] == 1 and r["condition_value_1"] == cat)
    wins = 1 if order == 0 else 0
    add(lambda r: r["condition_type"] == 10
        and (wins if r["condition_value_1"] == 1 else 1 - wins) >= r["condition_value_2"])
    return details


def _slot(gates) -> int:
    if not gates:
        return 0
    return sorted(range(len(gates)), key=lambda i: gates[i]).index(0)


def _grant_list(full_state, row, prefix: str, n: int, summary: dict) -> list:
    out = []
    for i in range(1, n + 1):
        cat = row.get(f"{prefix}_item_category_{i}") or 0
        iid = row.get(f"{prefix}_item_id_{i}") or 0
        num = row.get(f"{prefix}_item_num_{i}") or 0
        if not iid or num <= 0:
            continue
        if cat == daily_races._ITEM_TYPE_PIECE:
            iid = daily_races._piece_id_for_card(iid)
        daily_races._grant(full_state, cat, iid, num, summary)
        out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out


@registry.endpoint("challenge_match/race_end")
def handle_race_end(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    pending = (st or {}).get("pending")
    if not pending or pending.get("result_rank") is None:
        return refuse()
    row = _race(pending["race_id"])
    rank = pending["result_rank"]
    player = daily_races._find_veteran(viewer_id, pending["trained_chara_id"]) or {}
    wire0 = (pending.get("race_horse_data_array") or [{}])[0]
    details = []
    if pending.get("race_scenario"):
        try:
            details = _score(pending["race_scenario"], player, wire0.get("popularity") or 1,
                             row["difficulty"], _distance(row["race_instance_id"]),
                             slot=_slot(pending.get("gate_assignment")))
        except Exception:
            log.exception("challenge_match: scoring failed")
    total = max(0, sum(d["point"] for d in details))

    summary = daily_races._empty_summary()
    st["match_point"] += total
    daily_races._grant(full_state, POINT_ITEM_CATEGORY, POINT_ITEM_ID, total, summary)
    first = []
    if rank == 1 and row["id"] not in st["cleared"]:
        first = _grant_list(full_state, row, "first_clear", 3, summary)
        st["cleared"].append(row["id"])
    drop = _grant_list(full_state, row, "pick_up", 5, summary)

    if rank <= BONUS_SPARK_TOP:
        _grant_bonus_spark(full_state, pending["trained_chara_id"], event, pending.get("bonus_level") or 0)
    st.setdefault("runs", []).append([patch._servertime(), row["race_instance_id"]])
    if row["race_type"] == TYPE_HARD:
        if st["open_type"] == OPEN_CLOSE:
            st["gauge"] = min(GAUGE_MAX, st["gauge"] + GAUGE_PER_HARD)
            if st["gauge"] >= GAUGE_MAX:
                st["open_type"] = OPEN_CAN
    else:
        st["retry"] = 0 if rank == 1 else st.get("retry", 0) + 1
        _close_challenge(st)
    st.pop("pending", None)
    from .legend_race import _merge_summary
    _merge_summary(summary)
    state_store.save_state(viewer_id, full_state)
    log.info("challenge_match/race_end: race=%s rank=%d points=%d", row["id"], rank, total)
    return _ok({
        "rank": rank,
        "match_point": st["match_point"],
        "add_match_point_array": {
            "add_match_point_total": total, "add_match_point_base": total,
            "add_match_point_match_bonus_skill": 0, "point_detail_array": details},
        "first_clear_reward_array": first,
        "drop_reward_array": drop,
        "victory_reward_array": [],
        "reward_summary_info": summary,
        "trained_chara_id": pending["trained_chara_id"],
        "trained_chara_array": [],
        "state": ST_NONE,
        "mission_badge_num": 0,
    })


@registry.endpoint("challenge_match/reset")
def handle_reset(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    if st is not None:
        st.pop("pending", None)
        _expire(st)
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": ST_NONE})


@registry.endpoint("challenge_match/resume")
def handle_resume(payload: dict) -> dict:
    viewer_id, full_state, event, st = _load(payload)
    pending = (st or {}).get("pending") or {}
    return _ok({
        "state": pending.get("state", ST_NONE),
        "race_horse_data_array": copy.deepcopy(pending.get("race_horse_data_array") or []),
        "season": pending.get("season") or 0,
        "weather": pending.get("weather") or 0,
        "ground_condition": pending.get("ground") or 0,
        "random_seed": pending.get("random_seed") or 0,
        "race_instance_id": pending.get("race_instance_id") or 0,
        "race_scenario": pending.get("race_scenario"),
    })
