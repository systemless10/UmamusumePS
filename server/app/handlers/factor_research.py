from __future__ import annotations

import copy
import datetime as dt
import random

from .. import master_data as M, patch, region, state as S
from ..result_codes import ResultCode, refuse
from . import daily_races, shop, trained_chara

STATE_KEY = "factor_research_state"
GAUGE_MAX = 100
REPORT_CATEGORIES = (174, 194, 195)
JST = dt.timezone(dt.timedelta(hours=9))


def _jp():
    return region.CURRENT_REGION.get() == "jp"


def _tz():
    return JST if _jp() else dt.timezone.utc


def freeze_parents(full, viewer_id, start_chara):
    from . import single_mode_team
    roster = single_mode_team._roster_by_trained_id(viewer_id)
    ids = [start_chara.get(k) for k in ("succession_trained_chara_id_1", "succession_trained_chara_id_2")]
    full["career_parent_snapshot"] = [copy.deepcopy(roster[i]) for i in ids if i in roster]


def _ok(data):
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def current(earning=False):
    if not M.has_table("factor_research_data"):
        return None
    now = int(patch._servertime())
    end = "ending_date" if earning else "end_date"
    row = M.query_one(f"SELECT * FROM factor_research_data WHERE start_date<=? AND {end}>=? "
                      "ORDER BY start_date DESC, factor_research_event_id DESC LIMIT 1", (now, now))
    return dict(row) if row else None


def validate_start(start_chara):
    selected = start_chara.get("boost_factor_research_event_id")
    if not selected:
        return None
    event = current(earning=True)
    if type(selected) is not int or event is None or selected != event["factor_research_event_id"]:
        return refuse()
    return None


def event_state(full, event):
    return full.setdefault(STATE_KEY, {}).setdefault(str(event["factor_research_event_id"]),
        {"gauge": 0, "claimed": {}, "seen": False, "careers": {},
         "total_reward_point": 0, "cleared": [], "presented": []})


def _boxes(event):
    return [dict(r) for r in M.query("SELECT * FROM factor_research_box WHERE factor_research_event_id=? "
                                   "ORDER BY box_step, box_id", (event["factor_research_event_id"],))]


def _rewards(box):
    return [dict(r) for r in M.query("SELECT * FROM factor_research_box_reward WHERE box_id=? ORDER BY id",
                                   (box["box_id"],))]


def _remaining(st, reward):
    return max(0, reward["box_num"] - st["claimed"].get(str(reward["id"]), 0))


def _remaining_total(st, event):
    return sum(_remaining(st, r) for b in _boxes(event) for r in _rewards(b))


def _gauge(before, after, remaining):
    return {"before_gauge": before % GAUGE_MAX, "after_gauge": after % GAUGE_MAX,
            "before_gauge_stock_num": before // GAUGE_MAX,
            "after_gauge_stock_num": after // GAUGE_MAX,
            "is_gauge_stock_limit": int(remaining > 0 and after >= remaining * GAUGE_MAX)}


def add_gauge(full, amount):
    event = current(earning=True)
    if event is None or amount <= 0:
        return None
    st = event_state(full, event)
    remaining = _remaining_total(st, event)
    before = st["gauge"]
    st["gauge"] = min(remaining * GAUGE_MAX, before + int(amount))
    return _gauge(before, st["gauge"], remaining)


def settle_career(full, record, start_chara, good_ending):
    event = current(earning=True)
    if event is None:
        return None
    st = event_state(full, event)
    key = str(record["trained_chara_id"])
    if key in st["careers"]:
        return copy.deepcopy(st["careers"][key])
    boosted = (start_chara or {}).get("boost_factor_research_event_id") == event["factor_research_event_id"]
    amount = 400 if good_ending else 200
    if boosted:
        amount = amount * event["bonus_reward_ratio"] // 10000
    result = {"factor_research_gauge_info": add_gauge(full, amount), "is_boost": int(boosted)}
    st["careers"][key] = result
    return copy.deepcopy(result)


def expire_reports(full):
    if not M.has_table("factor_research_data"):
        return False
    removed = False
    for row in M.query("SELECT i.id FROM item_data i JOIN factor_research_data e "
                       "ON i.effect_type_1=e.factor_research_event_id "
                       "WHERE i.item_category IN (174,194,195) AND e.end_date<?", (int(patch._servertime()),)):
        count = shop._item_count(full, row["id"])
        if count:
            shop._add_item(full, row["id"], -count)
            removed = True
    return removed


def _item(event, category):
    row = M.query_one("SELECT * FROM item_data WHERE item_category=? AND effect_type_1=? ORDER BY id LIMIT 1",
                      (category, event["factor_research_event_id"]))
    return dict(row) if row else None


def _item_wire(full, item):
    return ({"item_type": item["item_category"], "item_id": item["id"],
             "item_num": shop._item_count(full, item["id"])} if item else None)


def _task_info(st):
    return {"total_reward_point": st["total_reward_point"],
            "cleared_factor_research_task_list_id_array": list(st["cleared"]),
            "presented_trained_chara_id_array": list(st["presented"])}


def load_info(full):
    expire_reports(full)
    event = current()
    if event is None:
        return None
    st = event_state(full, event)
    info = {"factor_research_event_id": event["factor_research_event_id"],
            "factor_research_event_first_flag": not st["seen"],
            "factor_research_gauge_info": _gauge(st["gauge"], st["gauge"], _remaining_total(st, event))}
    if _jp():
        info.update({"factor_research_report_usable_flag": any(
                         (item := _item(event, cat)) and shop._item_count(full, item["id"]) > 0
                         for cat in REPORT_CATEGORIES),
                     "factor_research_task_info": _task_info(st) if event.get("special_event_type") == 2 else None})
    return info


JP_INDEX_FIELDS = ("all_factor_update_item", "white_factor_add_item", "special_event_type",
                   "factor_research_task_info", "factor_research_all_completed_reward_flag")


def _index_reply(data):
    return _ok(data if _jp() else {k: v for k, v in data.items() if k not in JP_INDEX_FIELDS})


def _display(row, count=1):
    return {"item_type": row["item_category"], "item_id": row["item_id"], "item_num": row["item_num"] * count}


def handle_index(payload):
    full = S.get_state(payload["viewer_id"]) or {}
    expire_reports(full)
    event = current()
    if event is None:
        S.save_state(payload["viewer_id"], full)
        return _index_reply({"factor_research_event_id": 0, "box_id": 0, "box_item_num": 0,
                    "box_info_array": [], "factor_update_item_array": None,
                    "all_factor_update_item": None, "white_factor_add_item": None,
                    "factor_research_gauge_info": None, "factor_research_reward_info": None,
                    "factor_research_display_reward": None, "special_event_type": 0,
                    "factor_research_task_info": None, "factor_research_all_completed_reward_flag": 0})
    st = event_state(full, event)
    st["seen"] = True
    summary, display = shop._empty_summary(), []
    before = st["gauge"]
    boxes = _boxes(event)
    active_box = None
    for box in boxes:
        rewards = _rewards(box)
        while st["gauge"] >= GAUGE_MAX:
            available = [r for r in rewards if _remaining(st, r) and r["rate"] > 0]
            if not available:
                break
            weights = [r["rate"] * _remaining(st, r) / r["box_num"] for r in available]
            reward = random.choices(available, weights=weights, k=1)[0]
            key = str(reward["id"])
            st["claimed"][key] = st["claimed"].get(key, 0) + 1
            st["gauge"] -= GAUGE_MAX
            daily_races._grant(full, reward["item_category"], reward["item_id"], reward["item_num"], summary)
            display.append(_display(reward))
        if any(_remaining(st, r) for r in rewards):
            active_box = box
            break
    box = active_box or (boxes[-1] if boxes else None)
    rewards = _rewards(box) if box else []
    remaining = _remaining_total(st, event)
    data = {"factor_research_event_id": event["factor_research_event_id"],
            "special_event_type": event.get("special_event_type", 0),
            "box_id": box["box_id"] if box else 0,
            "box_item_num": sum(st["claimed"].get(str(r["id"]), 0) for r in rewards),
            "box_info_array": [{"factor_research_box_reward_id": r["id"], "remain_num": _remaining(st, r)} for r in rewards],
            "factor_research_gauge_info": _gauge(before, st["gauge"], remaining),
            "factor_research_reward_info": summary if display else None,
            "factor_research_display_reward": {"box_id": box["box_id"], "reward_array": display} if display and box else None,
            "factor_update_item_array": _item_wire(full, _item(event, 174)),
            "all_factor_update_item": _item_wire(full, _item(event, 194)),
            "white_factor_add_item": _item_wire(full, _item(event, 195)),
            "factor_research_task_info": _task_info(st) if event.get("special_event_type") == 2 else None,
            "factor_research_all_completed_reward_flag": int(remaining == 0 and _tasks_complete(st, event))}
    S.save_state(payload["viewer_id"], full)
    return _index_reply(data)


def _record(full, payload):
    ident = payload.get("trained_chara_id")
    return next((r for r in full.get(trained_chara.ROSTER_KEY) or [] if r.get("trained_chara_id") == ident), None)


def _factor(ident):
    row = M.query_one("SELECT * FROM succession_factor WHERE factor_id=?", (ident,))
    return dict(row) if row else None


def _upgrade_plan(before_id, after_id):
    before, after = _factor(before_id), _factor(after_id)
    if not before or not after or before["factor_group_id"] != after["factor_group_id"]:
        return None
    if before["factor_type"] != after["factor_type"] or not 1 <= before["rarity"] < after["rarity"] <= 3:
        return None
    cost = 0
    for rarity in range(before["rarity"] + 1, after["rarity"] + 1):
        rule = M.query_one("SELECT * FROM factor_research_factor_upgrade WHERE factor_type=? AND factor_rarity=?",
                           (after["factor_type"], rarity))
        if not rule or not rule["is_upgradable"]:
            return None
        cost += rule["use_item_num"]
    return cost


def _replace_factor(record, entry, after_id):
    before_id = entry["factor_id"]
    history = record.setdefault("factor_extend_array", [])
    prior = next((h for h in reversed(history) if h.get("position_id") == 0 and h.get("factor_id") == before_id), None)
    history.append({"position_id": 0, "base_factor_id": prior["base_factor_id"] if prior else before_id,
                    "factor_id": after_id, "register_time": dt.datetime.fromtimestamp(
                        patch._servertime(), _tz()).strftime("%Y-%m-%d %H:%M:%S")})
    entry["factor_id"] = after_id


def _upgrade(payload, paid=False):
    if paid and not _jp():
        return refuse()
    full = S.get_state(payload["viewer_id"]) or {}
    record = _record(full, payload)
    before_id, after_id = payload.get("before_factor_id"), payload.get("after_factor_id")
    entry = next((f for f in (record or {}).get("factor_info_array") or [] if f["factor_id"] == before_id), None)
    cost = _upgrade_plan(before_id, after_id) if entry else None
    if cost is None:
        return refuse()
    if paid:
        cat = payload.get("item_category")
        if cat not in (185, 196):
            return refuse()
        items = [dict(r) for r in M.query("SELECT * FROM item_data WHERE item_category=? ORDER BY end_date,id", (cat,))]
        now = int(patch._servertime())
        items = [i for i in items if _item_in_period(i, now) and shop._item_count(full, i["id"]) > 0]
    else:
        event = current()
        if event is None:
            return refuse()
        item = _item(event, 174)
        items = [item] if item else []
    if sum(shop._item_count(full, i["id"]) for i in items) < cost:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)
    left, used = cost, None
    for item in items:
        take = min(left, shop._item_count(full, item["id"]))
        if take:
            shop._add_item(full, item["id"], -take)
            used = item
            left -= take
        if not left:
            break
    _replace_factor(record, entry, after_id)
    S.save_state(payload["viewer_id"], full)
    return _ok({"trained_chara": copy.deepcopy(record), "factor_update_item_array": _item_wire(full, used)})


def _item_in_period(item, now):
    for key, lower in (("start_date", True), ("end_date", False)):
        value = item.get(key)
        if not value:
            continue
        stamp = int(dt.datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=_tz()).timestamp())
        if (lower and now < stamp) or (not lower and now > stamp):
            return False
    return True


def handle_factor_update(payload):
    return _upgrade(payload)


def handle_factor_update_paid_item(payload):
    return _upgrade(payload, paid=True)


def _task_rows(event):
    if not M.has_table("factor_research_task_list"):
        return []
    return [dict(r) for r in M.query("SELECT * FROM factor_research_task_list WHERE factor_research_event_id=? ORDER BY id",
                                    (event["factor_research_event_id"],))]


def _tasks_complete(st, event):
    return all(r["id"] in st["cleared"] for r in _task_rows(event))


def _condition(row, number, record, factors, normal_clears):
    prefix = f"condition{number}_"
    kind = row[prefix + "type"]
    values = [row[prefix + f"value{i}"] for i in range(1, 8)]
    if kind == 1:
        return record.get("scenario_id") == values[0]
    if kind == 2:
        return _g1_wins(record) >= values[0]
    if kind == 3:
        return any(f["factor_group_id"] in values[1:] for f in factors)
    if kind == 4:
        return any(f["factor_type"] == 2 and f["factor_group_id"] == values[1] and f["rarity"] >= values[0] for f in factors)
    if kind == 5:
        return sum(f["factor_type"] in (4, 5, 6, 7) for f in factors) >= values[0]
    if kind == 6:
        return any(f["factor_group_id"] == values[0] for f in factors)
    if kind == 100:
        return normal_clears >= values[0]
    return False


def _g1_wins(record):
    results = record.get("race_result_list")
    if results is None:
        return trained_chara.g1_win_count(record)
    count = 0
    for result in results:
        if result.get("result_rank") != 1:
            continue
        race = M.query_one("SELECT r.grade FROM single_mode_program p JOIN race_instance i "
                           "ON i.id=p.race_instance_id JOIN race r ON r.id=i.race_id WHERE p.id=?",
                           (result.get("program_id"),))
        count += int(race is not None and race["grade"] == 100)
    return count


def _matching_tasks(event, record):
    factors = [row for f in record.get("factor_info_array") or [] if (row := _factor(f["factor_id"]))]
    tasks = _task_rows(event)
    matched = []
    for task_type in (2, 1):
        for row in tasks:
            if row["task_type"] != task_type:
                continue
            tests = [_condition(row, n, record, factors, len(matched)) for n in range(1, 6) if row[f"condition{n}_type"]]
            if tests and (all(tests) if row["logic_type"] == 1 else any(tests)):
                matched.append(row["id"])
    return matched


def _prediction(full, event, record):
    st = event_state(full, event)
    if record["trained_chara_id"] in st["presented"]:
        return [], 0, []
    ids = [i for i in _matching_tasks(event, record) if i not in st["cleared"]]
    points = sum(r["research_point"] for r in _task_rows(event) if r["id"] in ids)
    rewards = [dict(r) for r in M.query("SELECT * FROM factor_research_task_point_reward WHERE factor_research_event_id=? "
                                       "AND reward_point>? AND reward_point<=? ORDER BY reward_point,id",
                                       (event["factor_research_event_id"], st["total_reward_point"], st["total_reward_point"] + points))]
    return ids, points, rewards


def _task_request(payload, present):
    full = S.get_state(payload["viewer_id"]) or {}
    event = current(earning=True)
    record = _record(full, payload)
    if event is None or event.get("special_event_type") != 2 or record is None:
        return refuse()
    st = event_state(full, event)
    ids, points, rewards = _prediction(full, event, record)
    if not present:
        return _ok({"clear_factor_research_task_list_id_array": ids, "add_reward_point": points,
                    "add_reward_item_array": [_display(r) for r in rewards]})
    if record["trained_chara_id"] in st["presented"]:
        return refuse()
    st["presented"].append(record["trained_chara_id"])
    st["cleared"] = sorted(set(st["cleared"]) | set(ids))
    st["total_reward_point"] += points
    summary = shop._empty_summary()
    for reward in rewards:
        daily_races._grant(full, reward["item_category"], reward["item_id"], reward["item_num"], summary)
    S.save_state(payload["viewer_id"], full)
    return _ok({"add_reward_point": points, "add_reward_item_array": [_display(r) for r in rewards],
                "reward_summary_info": summary, "factor_research_task_info": _task_info(st),
                "factor_research_all_completed_reward_flag": int(_remaining_total(st, event) == 0 and _tasks_complete(st, event)),
                "factor_update_item_array": [_item_wire(full, i) for cat in REPORT_CATEGORIES if (i := _item(event, cat))]})


def handle_task_predict(payload):
    return _task_request(payload, False)


def handle_task_presentation(payload):
    return _task_request(payload, True)


def _special_update(payload, white):
    full = S.get_state(payload["viewer_id"]) or {}
    event = current()
    record = _record(full, payload)
    if event is None or event.get("special_event_type") != 2 or record is None:
        return refuse()
    category = 195 if white else 194
    item = _item(event, category)
    if item is None or shop._item_count(full, item["id"]) < 1:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)
    factors = record.setdefault("factor_info_array", [])
    if white:
        after_id = payload.get("factor_id")
        allowed = M.query_one("SELECT * FROM factor_research_white_factor_item WHERE item_id=? AND factor_id=?",
                              (item["id"], after_id))
        after = _factor(after_id) if allowed else None
        if not after or after["rarity"] != 3 or any(
                (f := _factor(e["factor_id"])) and f["factor_group_id"] == after["factor_group_id"] for e in factors):
            return refuse()
        entry = {"factor_id": 0, "level": 0}
        _replace_factor(record, entry, after_id)
        factors.append(entry)
    else:
        plan = []
        for entry in factors:
            before = _factor(entry["factor_id"])
            if before and before["factor_type"] in (1, 2, 3) and before["rarity"] < 3:
                after = M.query_one("SELECT factor_id FROM succession_factor WHERE factor_group_id=? AND rarity=3",
                                    (before["factor_group_id"],))
                if after:
                    plan.append((entry, after["factor_id"]))
        if not plan:
            return refuse()
        for entry, after_id in plan:
            _replace_factor(record, entry, after_id)
    shop._add_item(full, item["id"], -1)
    S.save_state(payload["viewer_id"], full)
    return _ok({"trained_chara": copy.deepcopy(record), "factor_update_item": _item_wire(full, item)})


def handle_all_factor_update(payload):
    return _special_update(payload, False)


def handle_white_factor_update(payload):
    return _special_update(payload, True)


COMMON_HANDLERS = {
    "factor_research/index": handle_index,
    "factor_research/factor_update_index": lambda p: _ok({}),
    "factor_research/factor_update": handle_factor_update,
}

HANDLERS = {
    "factor_research/factor_update_paid_item": handle_factor_update_paid_item,
    "factor_research/task_predict": handle_task_predict,
    "factor_research/task_presentation": handle_task_presentation,
    "factor_research/all_factor_update": handle_all_factor_update,
    "factor_research/white_factor_update": handle_white_factor_update,
}
