
from __future__ import annotations

import copy
import datetime
import functools
import math
import random

from ... import master_data, patch, social
from ... import state as state_store
from ...handlers import bond, collection, missions, shop, stamina, story_event
from ...handlers.stories import _grant_reward
from ...result_codes import ResultCode, refuse
from . import schedule_book

STATE_KEY = "jp_jobs"

NORMAL_GOING_JOB_MAX = 3
LIMITED_FRAME = 3
CHARA_NUM_LIMIT_TO_1_JOB = 5
MINUTES_PER_POINT = 20

CIRCLE_POINT_LIMIT = 3_000_000
_MONEY = (91, 59)
_MEDAL = (161, 134)
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
JST = datetime.timezone(datetime.timedelta(hours=9))
_COND_JOBS_COMPLETE = 600042

_MONEY_VALUES = {
    1000: (10800, 11200, 11500, 12000),
    1001: (11200, 11600, 12000, 12400),
    1010: (21600, 22400, 23000, 24000),
    1011: (31600, None, None, 34000),
}
_MONEY_TIER_WEIGHTS = (42, 63, 104, 42)
_FAN_VALUES = {
    1: (19000, 20000, 20600, 21000),
    1000: (38000, 40000, 41200, 42000),
}
_FAN_TIER_WEIGHTS = (65, 119, 43, 26)
_DROP_PRIMARY = (
    (91, 59, 2000, 64), (11, 161, 1, 37), (11, 160, 1, 37), (150, 116, 1, 28),
    (150, 119, 1, 19), (150, 120, 1, 19), (93, 44, 1, 17), (150, 117, 1, 12),
    (150, 118, 1, 10), (90, 43, 10, 10),
)
_DROP_EXTRA = ((11, 162, 1, 5), (11, 163, 1, 1), (93, 44, 50, 1))
_DROP_EXTRA_RATE = (7, 253)
_MAIN_ORDER = {
    4: (76, 77, 4), 5: (78, 84, 87, 7), 6: (88, 70, 1), 7: (85, 82, 86, 7),
    8: (83, 74, 10), 14: (179, 102, 103, 13),
}


def _ok(data) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def _now() -> int:
    return int(patch._servertime())


def _fmt(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, JST).strftime(_TIME_FMT)


def _master_time(text) -> int | None:
    try:
        return int(datetime.datetime.strptime(str(text).strip(), "%Y/%m/%d %H:%M:%S")
                   .replace(tzinfo=JST).timestamp())
    except ValueError:
        return None


@functools.lru_cache(maxsize=None)
def _reward(jobs_reward_id: int) -> dict | None:
    row = master_data.query_one("SELECT * FROM jobs_reward WHERE id=?", (int(jobs_reward_id),))
    return dict(row) if row else None


@functools.lru_cache(maxsize=None)
def _disp_order() -> dict:
    return {(r["item_category"], r["item_id"]): r["disp_order"]
            for r in master_data.query("SELECT * FROM jobs_reward_sort_disp_order")}


@functools.lru_cache(maxsize=None)
def _point_limits() -> dict:
    row = master_data.query_one("SELECT * FROM jobs_point_limit ORDER BY id LIMIT 1")
    return dict(row) if row else {"jobs_story_event_point_limit": 0,
                                  "jobs_story_event_roulette_coin_limit": 0,
                                  "jobs_training_challenge_coin_limit": 0}


def _schedule(schedule_id: int) -> dict | None:
    row = master_data.query_one("SELECT * FROM jobs_limited_schedule WHERE id=?", (int(schedule_id),))
    return dict(row) if row else None


def _current_schedule(now: int) -> dict | None:
    row = master_data.query_one(
        "SELECT * FROM jobs_limited_schedule WHERE start_date<=? AND end_date>=? "
        "ORDER BY start_date DESC LIMIT 1", (now, now))
    return dict(row) if row else None


def training_report_season(now: int | None = None) -> dict | None:
    now = _now() if now is None else now
    if not master_data.has_table("training_report_pass"):
        return None
    for row in master_data.query("SELECT * FROM training_report_pass ORDER BY id DESC"):
        start = _master_time(row["start_date"])
        end = _master_time(row["end_date"])
        if start is not None and end is not None and start <= now <= end:
            return dict(row)
    return None


def _main_rewards(row: dict) -> list:
    mains = [(row[f"main_reward_{k}_item_category"], row[f"main_reward_{k}_item_id"],
              row[f"main_reward_{k}_item_num"])
             for k in range(1, 10)
             if row[f"main_reward_{k}_item_disp_type"] == 0 and row[f"main_reward_{k}_item_category"]]
    pinned = _MAIN_ORDER.get(row["id"])
    if pinned and sorted(pinned) == sorted(m[1] for m in mains):
        mains.sort(key=lambda m: pinned.index(m[1]))
    order = _disp_order()
    return sorted(mains, key=lambda m: order.get((m[0], m[1]), 0))


def _roll_tier(values, weights) -> int | None:
    picks = [(v, w) for v, w in zip(values, weights) if v is not None]
    return random.choices([v for v, _ in picks], [w for _, w in picks])[0]


def _roll_money(row: dict) -> int:
    values = _MONEY_VALUES.get(row["money_reward_item_odds_group_id"])
    return _roll_tier(values, _MONEY_TIER_WEIGHTS) if values else row["money_min"]


def _roll_fans(row: dict) -> int:
    values = _FAN_VALUES.get(row["fan_num_odds_group_id"])
    return _roll_tier(values, _FAN_TIER_WEIGHTS) if values else row["fan_num_min"]


def _roll_drops() -> list:
    def pick(table):
        t = random.choices(table, [e[3] for e in table])[0]
        return (t[0], t[1], t[2])
    drops = [pick(_DROP_PRIMARY)]
    if random.randrange(_DROP_EXTRA_RATE[1]) < _DROP_EXTRA_RATE[0]:
        drops.append(pick(_DROP_EXTRA))
    return drops


def _state(full_state: dict) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    st.setdefault("going", [])
    st.setdefault("log", {})
    st.setdefault("limited", {})
    st.setdefault("circle", {})
    st.setdefault("training_report", {})
    st.setdefault("story_event", {})
    st.setdefault("settings", [])
    st.setdefault("complete_total", 0)
    return st


def _going_wire(job: dict) -> dict:
    return {"jobs_reward_id": job["jobs_reward_id"],
            "attend_card_info_array": copy.deepcopy(job["cards"]),
            "end_time": _fmt(job["end_time"]),
            "local_push_type_index": job["frame"]}


def _limited_info(st: dict, now: int) -> list:
    sched = _current_schedule(now)
    counts = st["limited"].get(str(sched["id"]), {}) if sched else {}
    return [{"jobs_reward_id": int(rid), "complete_count": n}
            for rid, n in sorted(counts.items(), key=lambda kv: int(kv[0]))]


def _card_setting(full_state: dict, st: dict) -> list:
    stored = schedule_book.stored(full_state)
    if stored is not None and isinstance(stored.get("schedule_book_jobs_card_setting"), list):
        return copy.deepcopy(stored["schedule_book_jobs_card_setting"])
    return copy.deepcopy(st["settings"])


def _write_card_setting(full_state: dict, st: dict, started: list) -> list:
    rows = {r["jobs_frame_order"]: r for r in _card_setting(full_state, st)}
    for job in started:
        if job["frame"] >= NORMAL_GOING_JOB_MAX:
            continue
        row = {"jobs_frame_order": job["frame"], "jobs_reward_id": job["jobs_reward_id"]}
        for i, card in enumerate(job["cards"], 1):
            row[f"card_id_{i}"] = card["card_id"]
        cards = {c["card_id"] for c in job["cards"]}
        for frame, other in list(rows.items()):
            other_cards = {other.get(f"card_id_{i}") for i in range(1, CHARA_NUM_LIMIT_TO_1_JOB + 1)}
            if frame != job["frame"] and (other["jobs_reward_id"] == job["jobs_reward_id"]
                                          or cards & other_cards):
                del rows[frame]
        rows[job["frame"]] = row
    setting = [rows[k] for k in sorted(rows)]
    st["settings"] = copy.deepcopy(setting)
    stored = schedule_book.stored(full_state)
    if stored is not None:
        stored["schedule_book_jobs_card_setting"] = copy.deepcopy(setting)
    return setting


def handle_index(payload: dict) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    st = _state(full_state)
    now = _now()
    return _ok({
        "jobs_going_info_array": [_going_wire(j) for j in st["going"]] or None,
        "jobs_limited_info_array": _limited_info(st, now),
        "jobs_job_log_array": [{"top_list_index": i,
                                "card_id_array": list(st["log"].get(str(i)) or [0] * CHARA_NUM_LIMIT_TO_1_JOB)}
                               for i in range(NORMAL_GOING_JOB_MAX + 1)],
        "jobs_limited_top_new": 0,
        "schedule_book_setting": _card_setting(full_state, st),
    })


def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    infos = payload.get("jobs_start_job_info_array")
    if not isinstance(infos, list) or not infos:
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    now = _now()
    owned = {c.get("card_id") for c in full_state.get(collection.CARD_LIST_KEY) or []
             if isinstance(c, dict)}

    busy_frames = {j["frame"] for j in st["going"]}
    busy_rewards = {j["jobs_reward_id"] for j in st["going"]}
    busy_cards = {c["card_id"] for j in st["going"] for c in j["cards"]}
    started = []
    for info in infos:
        if not isinstance(info, dict):
            return refuse()
        rid = int(info.get("jobs_reward_id") or 0)
        frame = int(info.get("local_push_type_index", -1))
        row = _reward(rid)
        cards = [{"card_id": int(c.get("card_id") or 0), "dress_id": int(c.get("dress_id") or 0)}
                 for c in info.get("attend_card_info_array") or [] if isinstance(c, dict)]
        if row is None or not 0 <= frame <= LIMITED_FRAME or frame in busy_frames:
            return refuse()
        if (frame == LIMITED_FRAME) != bool(row["limited_schedule_id"]) or rid in busy_rewards:
            return refuse()
        card_ids = [c["card_id"] for c in cards]
        charas = {bond.chara_id_for_card(cid) for cid in card_ids}
        if (len(cards) != CHARA_NUM_LIMIT_TO_1_JOB or len(charas) != len(cards)
                or not set(card_ids) <= owned or busy_cards & set(card_ids)):
            return refuse()
        if row["limited_schedule_id"]:
            sched = _schedule(row["limited_schedule_id"])
            done = st["limited"].get(str(row["limited_schedule_id"]), {}).get(str(rid), 0)
            if (sched is None or not sched["start_date"] <= now <= sched["ending_date"]
                    or done >= (row["limited_max_count"] or 0)):
                return refuse()
        busy_frames.add(frame)
        busy_rewards.add(rid)
        busy_cards |= set(card_ids)
        started.append({"jobs_reward_id": rid, "frame": frame, "cards": cards,
                        "end_time": now + int(row["minutes_to_job_complete"]) * 60})

    st["going"] += started
    for job in started:
        st["log"][str(job["frame"])] = [c["card_id"] for c in job["cards"]]
    setting = _write_card_setting(full_state, st, started)
    state_store.save_state(viewer_id, full_state)
    return _ok({"jobs_going_info_array": [_going_wire(j) for j in started],
                "schedule_book_setting": setting})


def handle_abort(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    rid = int(payload.get("jobs_reward_id") or 0)
    job = next((j for j in st["going"] if j["jobs_reward_id"] == rid), None)
    if job is None:
        return refuse()
    st["going"].remove(job)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


def _merge_items(summary: dict) -> None:
    merged: dict = {}
    for e in summary["add_item_list"]:
        merged[e["item_id"]] = merged.get(e["item_id"], 0) + e["number"]
    summary["add_item_list"] = [{"item_id": k, "number": v} for k, v in merged.items()]


def _summary() -> dict:
    s = shop._empty_summary()
    s["add_support_card_limit_break_array"] = []
    return s


def handle_check_complete(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    now = _now()

    item = payload.get("jobs_use_item_info")
    item_id = use_num = tp_per_item = 0
    if isinstance(item, dict) and int(item.get("use_num") or 0) > 0:
        item_id, use_num = int(item.get("item_id") or 0), int(item["use_num"])
        found = stamina._recovery_item(item_id)
        if not found or found[0] is not stamina.TP:
            return refuse()
        if shop.item_count(full_state, item_id) < use_num:
            return refuse(ResultCode.NOT_ENOUGH_ERROR)
        tp_per_item = found[1]
    reductions = []
    listed = set()
    for e in payload.get("jobs_reduce_tp_array") or []:
        listed.add(int((e or {}).get("jobs_reward_id") or 0))
        use = int((e or {}).get("use_tp_value") or 0)
        if use <= 0:
            continue
        job = next((j for j in st["going"] if j["jobs_reward_id"] == int(e.get("jobs_reward_id") or 0)), None)
        if job is None:
            return refuse()
        left = max(0, job["end_time"] - now)
        need = math.ceil(left / (MINUTES_PER_POINT * 60))
        if use > need + 1:
            return refuse()
        reductions.append((job, use))
    tp_cost = sum(use for _, use in reductions)
    tp_have = int(stamina.TP.read(full_state, now).get("current_tp") or 0) + tp_per_item * use_num
    if tp_have < tp_cost:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)

    if use_num:
        shop.add_item(full_state, item_id, -use_num)
        stamina.TP.add(full_state, tp_per_item * use_num, now)
    if tp_cost:
        stamina.TP.spend(full_state, tp_cost, now)
    for job, use in reductions:
        job["end_time"] = max(now, job["end_time"] - use * MINUTES_PER_POINT * 60)

    done = [j for j in st["going"] if j["end_time"] <= now
            and (not listed or j["jobs_reward_id"] in listed)]
    st["going"] = [j for j in st["going"] if j not in done]

    results, fan_total = [], 0
    report_pt = event_pt = event_coin = research_gauge_up = 0
    for job in done:
        row = _reward(job["jobs_reward_id"])
        if row is None:
            continue
        result, fans = _collect(viewer_id, full_state, st, job, row)
        results.append(result)
        fan_total += fans
        report_pt += row["training_report_pt"]
        event_pt += row["story_event_event_pt"]
        event_coin += row["story_event_roulette_coin"]
        research_gauge_up += row["factor_research_gauge_up"]
        st["complete_total"] += 1

    limits = _point_limits()
    circle = _circle_points(viewer_id, st, fan_total)
    report = _training_report_points(st, report_pt, now)
    event = _story_event(viewer_id, full_state, st, event_pt, event_coin, limits, now)
    from ...handlers import factor_research
    research_gauge = factor_research.add_gauge(full_state, research_gauge_up)
    for r in results:
        r["before_circle_point"], r["after_circle_point"] = circle["before"], circle["after"]
        r["before_jobs_training_report_point_in_season"] = report["before"]
        r["after_jobs_training_report_point_in_season"] = report["after"]

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "tp_info": stamina.tp_info(full_state),
        "jobs_result_info_array": results,
        "jobs_result_limit_info": {
            "jobs_training_report_point_limit_in_season": report["limit"],
            "jobs_story_event_point_limit": limits["jobs_story_event_point_limit"],
            "before_jobs_story_event_point": event["point_before"],
            "after_jobs_story_event_point": event["point_after"],
            "jobs_story_event_roulette_coin_limit": limits["jobs_story_event_roulette_coin_limit"],
            "before_jobs_story_event_roulette_coin": event["coin_before"],
            "after_jobs_story_event_roulette_coin": event["coin_after"],
            "jobs_training_challenge_coin_limit": limits["jobs_training_challenge_coin_limit"],
            "before_jobs_training_challenge_coin": 0,
            "after_jobs_training_challenge_coin": 0,
            "after_jobs_circle_point": circle["jobs_after"],
            "jobs_circle_point_limit": CIRCLE_POINT_LIMIT,
        },
        "jobs_going_info_array": [_going_wire(j) for j in st["going"]],
        "story_event_info": event["info"],
        "factor_research_gauge_info": research_gauge,
        "training_challenge_result": None,
        "jobs_limited_info_array": _limited_info(st, now),
    })


def _collect(viewer_id, full_state: dict, st: dict, job: dict, row: dict) -> tuple:
    summary = _summary()
    display = [{"item_type": _MONEY[0], "item_id": _MONEY[1], "item_num": _roll_money(row)},
               {"item_type": _MEDAL[0], "item_id": _MEDAL[1], "item_num": row["trainer_medal"]}]
    display += [{"item_type": c, "item_id": i, "item_num": n} for c, i, n in _main_rewards(row)]
    additional = [{"item_type": c, "item_id": i, "item_num": n} for c, i, n in _roll_drops()]
    for e in display + additional:
        _grant_reward(full_state, e["item_type"], e["item_id"], e["item_num"], summary, viewer_id)
    _merge_items(summary)

    fans = _roll_fans(row)
    per_chara = fans // len(job["cards"])
    charas, profiles, pool_map = [], [], {}
    for card in job["cards"]:
        chara_id = bond.chara_id_for_card(card["card_id"])
        entry = bond.chara_entry(full_state, chara_id)
        fan_before = int(entry.get("fan") or 0)
        entry["fan"] = fan_before + per_chara
        love = bond.add_love_point(full_state, chara_id, row["love_point"])
        if love["love_point_pool_after"] > love["love_point_pool_before"]:
            pool_map[chara_id] = {"viewer_id": int(viewer_id), "chara_id": chara_id,
                                  "pool_point": love["love_point_pool_before"]}
        profiles += bond.new_profile_entries(full_state, chara_id)
        charas.append({"chara_id": chara_id, "dress_id": card["dress_id"],
                       "before_fan_num": fan_before, "after_fan_num": entry["fan"],
                       "love_point_info": love})

    limited = []
    if row["limited_schedule_id"]:
        counts = st["limited"].setdefault(str(row["limited_schedule_id"]), {})
        counts[str(row["id"])] = counts.get(str(row["id"]), 0) + 1
        limited = [row["id"], row["limited_schedule_id"]]

    return {
        "jobs_reward_id": row["id"],
        "card_id": job["cards"][0]["card_id"],
        "local_push_type_index": job["frame"],
        "display_reward_list": display,
        "additional_display_reward_list": additional,
        "love_point_pool_map": pool_map or [],
        "add_love_point": row["love_point"],
        "reward_fan_num": per_chara,
        "campaign_id_array": [],
        "jobs_limited_info": limited,
        "reward_summary_info": summary,
        "jobs_result_character_info_array": charas,
        "new_chara_profile_array": profiles,
    }, per_chara * len(job["cards"])


def _circle_points(viewer_id, st: dict, fans: int) -> dict:
    month = int(datetime.datetime.fromtimestamp(_now(), JST).strftime("%Y%m"))
    record = st["circle"]
    if record.get("month") != month:
        record.update(month=month, point=0)
    member = social.member_row(viewer_id)
    if member is None:
        return {"before": 0, "after": 0, "jobs_after": record["point"]}
    circle_id = member["circle_id"]
    before = social.circle_rank(circle_id)[1] or 0
    add = max(0, min(fans, CIRCLE_POINT_LIMIT - record["point"]))
    record["point"] += add
    after = social.add_circle_point(circle_id, add) if add else before
    return {"before": before, "after": after, "jobs_after": record["point"]}


def _training_report_points(st: dict, points: int, now: int) -> dict:
    season = training_report_season(now)
    if season is None:
        return {"before": 0, "after": 0, "limit": 0}
    key = str(season["season_id"])
    limit = int(season["jobs_training_report_point_limit"] or 0)
    before = st["training_report"].get(key, 0)
    after = min(limit, before + points)
    st["training_report"][key] = after
    return {"before": before, "after": after, "limit": limit}


def season_points(full_state: dict, season_id: int) -> int:
    return int(((full_state.get(STATE_KEY) or {}).get("training_report") or {}).get(str(season_id), 0))


def _earning_event(now: int) -> int | None:
    row = master_data.query_one(
        "SELECT story_event_id FROM story_event_data WHERE start_date<=? AND ending_date>=? "
        "ORDER BY start_date DESC LIMIT 1", (now, now))
    return int(row["story_event_id"]) if row else None


def _story_event(viewer_id, full_state: dict, st: dict, points: int, coins: int,
                 limits: dict, now: int) -> dict:
    event_id = _earning_event(now)
    if event_id is None:
        return {"info": None, "point_before": 0, "point_after": 0, "coin_before": 0, "coin_after": 0}
    record = st["story_event"].setdefault(str(event_id), {"point": 0, "coin": 0})
    point_before, coin_before = record["point"], record["coin"]
    record["point"] = min(limits["jobs_story_event_point_limit"], point_before + points)
    record["coin"] = min(limits["jobs_story_event_roulette_coin_limit"], coin_before + coins)
    add_point = record["point"] - point_before
    add_coin = record["coin"] - coin_before

    totals = full_state.setdefault("story_event_point", {})
    total_before = int(totals.get(str(event_id), 0))
    total_after = total_before + add_point
    totals[str(event_id)] = total_after

    ev_state, _ = story_event._event_state(full_state, event_id)
    sheet = story_event._sheet_row(event_id, ev_state["current_sheet"])
    coin_type, coin_item = ((sheet["use_item_category"], sheet["use_item_id"]) if sheet else (94, 45))
    if add_coin:
        shop.add_item(full_state, coin_item, add_coin)

    summary = _summary()
    reward_ids, reward_list = [], []
    for r in master_data.query(
            "SELECT * FROM story_event_point_reward WHERE story_event_id=? AND point>? AND point<=? "
            "ORDER BY point, id", (event_id, total_before, total_after)):
        reward_ids.append(r["id"])
        reward_list += _grant_reward(full_state, r["item_category"], r["item_id"], r["item_num"],
                                     summary, viewer_id)
    _merge_items(summary)

    return {
        "point_before": point_before, "point_after": record["point"],
        "coin_before": coin_before, "coin_after": record["coin"],
        "info": {
            "add_point_info": {"total": add_point, "rank_score": 0, "nickname_bonus": 0,
                               "card_bonus": 0, "support_card_bonus": 0, "win_num_bonus": 0},
            "event_id": event_id,
            "event_user_info": story_event._event_user_info(full_state, event_id,
                                                            ev_state["current_sheet"]),
            "point_reward_id_list": reward_ids,
            "reward_list": reward_list,
            "reward_summary_info": summary,
            "training_reward_list": [{"item_type": coin_type, "item_id": coin_item,
                                      "item_num": add_coin}],
            "additional_training_reward_list": [],
            "is_boost": 0,
            "new_story_id_list": [],
            "story_data_list": [],
            "get_all_point_reward": total_before < story_event._max_point_threshold(event_id) <= total_after,
        },
    }


def _complete_total(viewer_id, full_state: dict, row) -> int:
    return int((full_state.get(STATE_KEY) or {}).get("complete_total") or 0)


missions.CONDITION_HOOKS[_COND_JOBS_COMPLETE] = _complete_total


HANDLERS = {
    "jobs/index": handle_index,
    "jobs/start": handle_start,
    "jobs/check_complete": handle_check_complete,
    "jobs/abort": handle_abort,
}
