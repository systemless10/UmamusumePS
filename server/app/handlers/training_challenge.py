from __future__ import annotations

import copy
from collections import Counter

from .. import master_data as M, patch, state as S
from ..result_codes import ResultCode, refuse
from . import registry, shop

STATE_KEY = "training_challenge_state"
RUN_KEY = "training_challenge_run"
APTITUDES = {101: "proper_distance_short", 102: "proper_distance_mile",
             103: "proper_distance_middle", 104: "proper_distance_long",
             105: "proper_ground_turf", 106: "proper_ground_dirt"}
STATS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}


def _ok(data=None):
    response = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        response["data"] = data
    return response


def current(main_only=False):
    if not M.has_table("training_challenge_master"):
        return None
    now = patch._servertime()
    end = "end_date" if main_only else "end_result_date"
    row = M.query_one(f"SELECT * FROM training_challenge_master WHERE start_date<=? AND {end}>=? ORDER BY start_date DESC LIMIT 1", (now, now))
    return dict(row) if row else None


def ranking_cutoff(event):
    now = patch._servertime()
    cutoff = ((now - 22 * 3600) // 86400) * 86400 + 22 * 3600
    return cutoff if cutoff > event["start_date"] else None


def exam_ids(event):
    return [event[k] for k in ("exam_id_1", "exam_id_2", "exam_id_3", "exam_id_4", "exam_id_5", "ex_exam_id", "free_exam_id") if event[k]]


def event_state(full, event):
    return full.setdefault(STATE_KEY, {}).setdefault(str(event["id"]),
        {"select_exam_id": 0, "exams": {}, "seen": False})


def wallet_item(event):
    r = M.query_one("SELECT pay_item_id FROM item_exchange WHERE item_exchange_top_id=? LIMIT 1", (event["shop_id"],))
    return int(r[0]) if r else 156


def user_info(full, event):
    return {"event_coin": shop._item_count(full, wallet_item(event)),
            "select_exam_id": event_state(full, event)["select_exam_id"]}


def exam_infos(full, event):
    exams = event_state(full, event)["exams"]
    return [copy.deepcopy(exams.get(str(eid), {"exam_id": eid, "high_score_value": 0,
            "result_type": 1, "high_score_trained_chara": None, "is_challenged": False})) for eid in available_exams(full, event)]


def available_exams(full, event):
    st = event_state(full, event)["exams"]
    available = [event[f"exam_id_{i}"] for i in range(1, 6) if event[f"exam_id_{i}"]]
    if not available:
        return [event["free_exam_id"]] if event["free_exam_id"] else []
    if all(st.get(str(eid), {}).get("result_type", 1) >= 2 for eid in available):
        if event["ex_exam_id"]:
            available.append(event["ex_exam_id"])
        if not event["ex_exam_id"] or st.get(str(event["ex_exam_id"]), {}).get("result_type", 1) >= 2:
            if event["free_exam_id"]:
                available.append(event["free_exam_id"])
    return available


def overlay_login(data, full):
    event = current()
    if not event or not event_state(full, event)["select_exam_id"]:
        data["training_challenge_user_info"] = None
        data["training_challenge_exam_infos"] = None
        return
    data["training_challenge_user_info"] = user_info(full, event)
    data["training_challenge_exam_infos"] = exam_infos(full, event)


@registry.endpoint("training_challenge/index")
def handle_index(payload):
    event = current()
    if not event:
        return refuse(ResultCode.TRAINING_CHALLENGE_NOT_IN_TERM)
    full = S.get_state(payload["viewer_id"]) or {}
    st = event_state(full, event)
    first = not st["seen"] or not st["select_exam_id"]
    st["seen"] = True
    ranking_state = 1 if ranking_cutoff(event) is not None else 2
    response = _ok({"event_id": event["id"], "event_user_info": user_info(full, event),
                    "exam_infos": exam_infos(full, event), "ranking_state": ranking_state,
                    "support_card_ranking_state": ranking_state, "first_access_flag": first})
    S.save_state(payload["viewer_id"], full)
    return response


@registry.endpoint("training_challenge/exam_select")
def handle_exam_select(payload):
    event = current(True)
    if not event:
        return refuse(ResultCode.TRAINING_CHALLENGE_NOT_IN_TERM)
    full = S.get_state(payload["viewer_id"]) or {}
    if payload.get("exam_id") not in available_exams(full, event):
        return refuse()
    event_state(full, event)["select_exam_id"] = payload["exam_id"]
    S.save_state(payload["viewer_id"], full)
    return _ok()


def validate_start(full, start):
    if not start.get("is_play_training_challenge"):
        return None
    event = current(True)
    if not event or event["target_main_scenario"] != start.get("scenario_id"):
        return refuse(ResultCode.TRAINING_CHALLENGE_NOT_IN_TERM_SINGLE_MODE)
    if start.get("training_challenge_mode", 1) not in (1, 2):
        return refuse()
    if start.get("training_challenge_mode", 1) == 2 and not event["bonus_reward_ratio"]:
        return refuse()
    selected = event_state(full, event)["select_exam_id"]
    if selected not in available_exams(full, event):
        return refuse()
    return None


def bind_start(full, start):
    full.pop(RUN_KEY, None)
    if start.get("is_play_training_challenge"):
        event = current(True)
        full[RUN_KEY] = {"event": event, "exam_id": event_state(full, event)["select_exam_id"],
                         "mode": start.get("training_challenge_mode", 1)}


def _race_counts(record):
    grades = Counter()
    for race in record.get("race_result_list") or []:
        if race.get("result_rank") != 1:
            continue
        r = M.query_one("SELECT r.grade FROM single_mode_program p JOIN race_instance i ON i.id=p.race_instance_id JOIN race r ON r.id=i.race_id WHERE p.id=?", (race["program_id"],))
        if r:
            grades[int(r[0])] += 1
    return grades


def scenario_values(full, record):
    gl = full.get("grand_live") or {}
    facts = record.get("_scenario_facts") or {}
    legs = facts.get("trackblazer_climax_legs") or []
    return {2000: int((full.get("unity_cup") or {}).get("bursts") or 0),
            3000: sum(i.get("result_state") == 2 for i in gl.get("lives_done", []) if i.get("live_type", 0) <= 4),
            3001: len([i for i in gl.get("songs", []) if i != 1029]),
            4000: sum(r.get("player_rank") == 1 for r in legs),
            4001: int(facts.get("trackblazer_climax_rank") == 1)}


def score(record, exam, full=None):
    full = full or {}
    rows = [dict(r) for r in M.query("SELECT * FROM training_challenge_score WHERE score_group_id=? ORDER BY score_type,disp_order,data,id", (exam["score_group_id"],))]
    skills = {i["skill_id"] if isinstance(i, dict) else i for i in record.get("skill_array", record.get("skill_data_array", []))}
    grades = _race_counts(record) if any(r["score_type"] == 7 for r in rows) else Counter()
    rarity_counts = Counter()
    for sid in skills:
        r = M.query_one("SELECT rarity FROM skill_data WHERE id=?", (sid,))
        if r:
            rarity_counts[int(r[0])] += 1
    values = scenario_values(full, record)
    picked = []
    rank_ratio = 100
    for r in rows:
        kind, threshold = r["score_type"], r["data"]
        if kind in APTITUDES:
            if record.get(APTITUDES[kind], 0) == threshold:
                picked.append((r, 1))
                rank_ratio = r["bonus_score"]
            continue
        if kind in STATS:
            count = int(record.get(STATS[kind], 0) >= threshold)
        elif kind == 6:
            count = int(threshold in skills)
        elif kind == 7:
            count = grades[threshold]
        elif kind == 8:
            count = rarity_counts[threshold]
        elif kind in values:
            count = int(values[kind] >= threshold)
        else:
            raise ValueError(f"Unresearched Training Challenge score type {kind}")
        if count:
            picked.append((r, count))
    base = int(record["rank_score"])
    rating = base * rank_ratio // 100
    total = rating + sum(r["bonus_score"] * n for r, n in picked if r["score_type"] not in APTITUDES)
    result = result_type(total, exam)
    grade_coin = {1: 0, 2: exam["good_score_reward_coin"], 3: exam["great_score_reward_coin"], 4: exam["exellent_score_reward_coin"]}[result]
    coins = (total // 1000) * 20 + sum(r["reward_coin"] * n for r, n in picked) + grade_coin
    return {"total": total, "rank_score": rating, "base_rank_score": base,
            "score_ids": [r["id"] for r, _ in picked], "add_event_coin": coins}


def result_type(total, exam):
    for threshold, result in (("exellent_score", 4), ("great_score", 3), ("good_score", 2)):
        if total >= exam[threshold]:
            return result
    return 1


def settle(full, record, viewer_id=None):
    run = full.get(RUN_KEY)
    if not run:
        return None
    if "result" in run:
        return copy.deepcopy(run["result"])
    event, eid = run["event"], run["exam_id"]
    exam = dict(M.query_one("SELECT * FROM training_challenge_exam WHERE id=?", (eid,)))
    summary = score(record, exam, full)
    if run["mode"] == 2:
        summary["add_event_coin"] = summary["add_event_coin"] * event["bonus_reward_ratio"] // 10000
    shop._add_item(full, wallet_item(event), summary["add_event_coin"])
    st = event_state(full, event)
    old = st["exams"].get(str(eid), {})
    result = result_type(summary["total"], exam)
    reward_summary, rewards = shop._empty_summary(), []
    if result >= 2 and old.get("result_type", 1) < 2:
        from .stories import _grant_reward
        for i in range(1, 6):
            cat, item, num = (exam[f"item_{k}_{i}"] for k in ("category", "id", "num"))
            if cat and item and num:
                rewards += _grant_reward(full, cat, item, num, reward_summary, viewer_id)
    if not old or summary["total"] > old.get("high_score_value", 0):
        st["exams"][str(eid)] = {"exam_id": eid, "high_score_value": summary["total"],
            "result_type": result, "high_score_trained_chara": copy.deepcopy(record), "is_challenged": True}
    st.setdefault("support_usage", {}).setdefault(str(eid), {})
    usage = st["support_usage"][str(eid)]
    for card in record.get("support_card_list", []):
        sid = str(card["support_card_id"])
        usage[sid] = usage.get(sid, 0) + 1
    ids = exam_ids(event)
    next_id = 0
    if result >= 2 and old.get("result_type", 1) < 2 and eid != ids[-1]:
        available = available_exams(full, event)
        next_id = next((i for i in ids[ids.index(eid) + 1:] if i in available), 0)
    result_data = {"event_id": event["id"], "exam_id": eid, "next_exam_id": next_id,
            "score_summary": summary, "reward_summary_info": reward_summary,
            "reward_list": rewards, "event_user_info": user_info(full, event),
            "training_challenge_mode": run["mode"], "exam_infos": exam_infos(full, event)}
    st.setdefault("history", []).append({"time": patch._servertime(), "exam_id": eid,
        "score": summary["total"], "result_type": result, "trained_chara": copy.deepcopy(record)})
    run["result"] = copy.deepcopy(result_data)
    return result_data


@registry.endpoint("training_challenge/get_ranking")
def handle_ranking(payload):
    event = current()
    if not event:
        return refuse(ResultCode.TRAINING_CHALLENGE_RANKING_NOT_IN_TERM)
    rows = []
    cutoff = ranking_cutoff(event)
    if cutoff is None:
        return _ok({"ranking_user_list": []})
    for viewer, all_events in S.all_states_for_key(STATE_KEY).items():
        st = all_events.get(str(event["id"]), {})
        history = st.get("history", [{"time": 0, "exam_id": i["exam_id"],
            "score": i["high_score_value"], "result_type": i["result_type"],
            "trained_chara": i["high_score_trained_chara"]} for i in st.get("exams", {}).values()])
        exams = [i for i in history if i["result_type"] >= 2 and i["time"] < cutoff]
        if not exams:
            continue
        best = max(exams, key=lambda i: i["score"])
        full = S.get_state(viewer) or {}
        rows.append({"viewer_id": int(viewer), "name": (full.get("user_info_state") or {}).get("name", "Trainer"),
                     "score": best["score"], "exam_id": best["exam_id"],
                     "trained_chara": copy.deepcopy(best["trained_chara"])})
    rows.sort(key=lambda r: (-r["score"], r["viewer_id"]))
    return _ok({"ranking_user_list": rows[:100]})


def support_ranking(payload):
    event = current()
    if not event:
        return _ok({"groups": [], "is_counting": False, "first_access_flag": False})
    totals = {eid: Counter() for eid in exam_ids(event)}
    cutoff = ranking_cutoff(event)
    full = S.get_state(payload["viewer_id"]) or {}
    st = event_state(full, event)
    first = not st.get("support_ranking_seen", False)
    st["support_ranking_seen"] = True
    S.save_state(payload["viewer_id"], full)
    for all_events in S.all_states_for_key(STATE_KEY).values():
        saved = all_events.get(str(event["id"]), {})
        if "history" in saved:
            for run in saved["history"]:
                if cutoff is not None and run["time"] < cutoff and run["exam_id"] in totals:
                    totals[run["exam_id"]].update(str(c["support_card_id"]) for c in run["trained_chara"].get("support_card_list", []))
            continue
        for eid, usage in saved.get("support_usage", {}).items():
            if int(eid) in totals:
                if cutoff is not None:
                    totals[int(eid)].update(usage)
    groups = []
    for index, eid in enumerate(exam_ids(event), 1):
        counts = totals[eid]
        total = sum(counts.values())
        groups.append({"exam_index": index, "items": [{"support_card_id": int(sid),
            "percentage": n * 10000 // total, "rank": rank} for rank, (sid, n) in enumerate(counts.most_common(10), 1)]})
    return _ok({"groups": groups, "is_counting": cutoff is None, "first_access_flag": first})
