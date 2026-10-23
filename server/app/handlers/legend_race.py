
from __future__ import annotations

import copy
import functools
import logging
import random

from .. import config, custom_support, master_data, patch
from .. import state as state_store
from . import daily_races, limited_shop, registry
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

LEGEND_KEY = "legend_race_state"
TICKET_ITEM_ID = 97
_TICKET_PER_PURCHASE = 1
_VICTORY_MONEY = 20000


@functools.lru_cache(maxsize=1)
def _billing() -> tuple:
    row = master_data.query_one(
        "SELECT frequency, pay_cost FROM legend_race_billing ORDER BY id LIMIT 1")
    if row is None:
        return (0, 0)
    return ((row["frequency"] or 0), (row["pay_cost"] or 0))


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _all_races() -> tuple:
    return _races_in(custom_support.CURRENT_PACK.get())


@functools.lru_cache(maxsize=8)
def _races_in(_pack: str) -> tuple:
    return tuple(master_data.query("SELECT * FROM legend_race ORDER BY start_date, id"))


@functools.lru_cache(maxsize=1)
def _ticket_category() -> int:
    row = master_data.query_one("SELECT item_category FROM item_data WHERE id=?",
                                (TICKET_ITEM_ID,))
    return (row["item_category"] if row else 0) or 0


def _row(legend_race_id):
    if not isinstance(legend_race_id, int) or isinstance(legend_race_id, bool):
        return None
    return next((r for r in _all_races() if r["id"] == legend_race_id), None)


def active_races() -> list:
    forced = config.get("legend_race_id")
    if forced is not None:
        row = _row(int(forced))
        return [row] if row is not None else []
    now = patch._servertime()
    return [r for r in _all_races() if (r["start_date"] or 0) <= now <= (r["end_date"] or 0)]


def _state(full_state: dict) -> dict:
    st = full_state.setdefault(LEGEND_KEY, {})
    st.setdefault("cleared", [])
    st.setdefault("played", [])
    day = daily_races._served_day()
    if st.get("day") != day:
        st["day"] = day
        st["purchase_num"] = 0
        st["entries_today"] = 0
    st.setdefault("purchase_num", 0)
    st.setdefault("entries_today", 0)
    return st


_ENTRIES_PER_DAY = 1


def _index_boss(row) -> dict:
    return daily_races.index_boss_data(row["legend_race_boss_npc_id"])


def _record(row, st: dict) -> dict:
    rid = row["id"]
    return {"legend_race_id": rid,
            "is_cleared": 1 if rid in st["cleared"] else 0,
            "is_played": 1 if rid in st["played"] else 0,
            "boss_data": _index_boss(row)}


@registry.endpoint("legend_race/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    races = active_races()
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "legend_race_record_array": [_record(r, st) for r in races],
        "purchase_num": st["purchase_num"],
        "update_item_array": [],
        "legend_race_mission_num": 0,
    })


def _preview_rewards(row) -> list:
    out = []
    for prefix in ("first_clear", "pick_up"):
        for i in (1, 2, 3):
            cat = row[f"{prefix}_item_category_{i}"] or 0
            iid = row[f"{prefix}_item_id_{i}"] or 0
            num = row[f"{prefix}_item_num_{i}"] or 0
            if not iid or num <= 0:
                continue
            if cat == daily_races._ITEM_TYPE_PIECE:
                iid = daily_races._piece_id_for_card(iid)
            out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out


@registry.endpoint("legend_race/get_reward_list")
def handle_get_reward_list(payload: dict) -> dict:
    row = _row(payload.get("legend_race_id"))
    if row is None:
        return _refuse()
    return _ok({"reward_array": _preview_rewards(row)})


@registry.endpoint("legend_race/race_entry")
def handle_race_entry(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    race_id = payload.get("legend_race_id")
    row = _row(race_id)
    if row is None or not any(r["id"] == race_id for r in active_races()):
        return _refuse(ResultCode.LEGEND_RACE_NOT_RACE_TIME)

    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    if not st.get("pending") and st["entries_today"] >= _ENTRIES_PER_DAY:
        return _refuse(ResultCode.NOT_ENOUGH_ERROR)
    player = daily_races._find_veteran(viewer_id, payload.get("trained_chara_id"))
    if player is None:
        return _refuse()
    opponents = daily_races._legend_opponents(row, viewer_id)
    if not opponents:
        return _refuse()

    horses = [copy.deepcopy(player)] + opponents
    season = (row["season"] or 1)
    weather = (row["weather"] or 1)
    ground = (row["ground"] or 1)
    course = daily_races.race_simulator.get_course_for_race_instance(row["race_instance_id"])
    entry_num = course[2] if course else len(horses)
    gates = random.sample(range(max(entry_num, len(horses))), len(horses))
    wire_horses = daily_races._race_horse_array(horses, gates)
    seed = random.randint(0, 2**31 - 1)
    st["pending"] = {
        "race_id": race_id,
        "trained_chara_id": payload.get("trained_chara_id"),
        "item_id_array": [],
        "result_rank": None,
        "season": season, "weather": weather, "ground": ground,
        "gate_assignment": gates,
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "race_horse_data_array": copy.deepcopy(wire_horses),
        "race_scenario": None,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_horse_data_array": wire_horses,
        "season": season,
        "weather": weather,
        "ground_condition": ground,
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "state": 1,
        "trained_chara_id": payload.get("trained_chara_id"),
    })


@registry.endpoint("legend_race/reflect_item_effect")
def handle_reflect_item_effect(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    row = _row(pending["race_id"])
    if row is None:
        return _refuse()

    cost = row["cost_num"] or 1
    if daily_races._item_count(full_state, TICKET_ITEM_ID) < cost:
        return _refuse()
    daily_races._add_item(full_state, TICKET_ITEM_ID, -cost)
    if not pending.get("counted"):
        pending["counted"] = True
        st["entries_today"] = st.get("entries_today", 0) + 1
    used = daily_races.apply_race_items(full_state, pending, payload.get("item_id_array"))
    state_store.save_state(viewer_id, full_state)
    return _ok(daily_races.reflect_reply(full_state, pending, TICKET_ITEM_ID, used))


@registry.endpoint("legend_race/race_start")
def handle_race_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    row = _row(pending["race_id"])
    player = daily_races._find_veteran(viewer_id, pending["trained_chara_id"])
    if row is None or player is None:
        return _refuse()

    player = dict(player)
    if pending.get("motivation"):
        player["motivation"] = pending["motivation"]
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    outcome = daily_races._run_race(
        player, daily_races._legend_opponents(row, viewer_id),
        row["race_instance_id"], pending["season"], pending["weather"],
        pending["ground"], gate_assignment=pending.get("gate_assignment"), race_type=12)
    pending["result_rank"] = outcome["rank"]
    pending["race_scenario"] = outcome["race_scenario"]
    state_store.save_state(viewer_id, full_state)
    log.info("legend_race/race_start: race=%s player=%s rank=%d simulated=%s",
             pending["race_id"], pending["trained_chara_id"], outcome["rank"],
             outcome["sim_results"] is not None)
    return _ok({
        "race_scenario": outcome["race_scenario"],
        "state": 4,
        "running_style": payload.get("running_style") or player.get("running_style") or 1,
    })


def _merge_summary(summary: dict) -> None:
    for key, id_field, num_field in (("add_item_list", "item_id", "number"),
                                     ("add_piece_list", "piece_id", "piece_num")):
        merged = []
        for entry in summary.get(key) or []:
            prev = next((e for e in merged if e[id_field] == entry[id_field]), None)
            if prev is None:
                merged.append(entry)
            else:
                prev[num_field] += entry[num_field]
        summary[key] = merged


def _award(full_state: dict, st: dict, row, rank: int) -> dict:
    summary = daily_races._empty_summary()
    rid = row["id"]
    first_clear, victory = [], []

    drop = daily_races._grant_row_items(full_state, row, "pick_up", summary)
    if rank == 1:
        if rid not in st["cleared"]:
            first_clear = daily_races._grant_row_items(
                full_state, row, "first_clear", summary)
            st["cleared"].append(rid)
        daily_races._grant(full_state, daily_races._ITEM_TYPE_MONEY, 59,
                           _VICTORY_MONEY, summary)
        victory = [{"item_type": daily_races._ITEM_TYPE_MONEY, "item_id": 59,
                    "item_num": _VICTORY_MONEY}]
    if rid not in st["played"]:
        st["played"].append(rid)
    _merge_summary(summary)
    return {"first_clear_reward_array": first_clear,
            "drop_reward_array": drop,
            "victory_reward_array": victory,
            "reward_summary_info": summary}


@registry.endpoint("legend_race/replay_check")
def handle_replay_check(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending or pending.get("result_rank") is None:
        return _refuse()
    row = _row(pending["race_id"])
    if row is None:
        return _refuse()

    rank = pending["result_rank"]
    reward = _award(full_state, st, row, rank)
    st.pop("pending", None)
    shop_info = limited_shop.roll(full_state, "legend_race")
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "rank": rank,
        "first_clear_reward_array": reward["first_clear_reward_array"],
        "drop_reward_array": reward["drop_reward_array"],
        "victory_reward_array": reward["victory_reward_array"],
        "reward_summary_info": reward["reward_summary_info"],
        "state": 0,
        "add_trophy_info": None,
        "trophy_reward_info": None,
        "limited_shop_info": shop_info,
    })


@registry.endpoint("legend_race/recovery_ticket")
def handle_recovery_ticket(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    frequency, cost = _billing()
    if frequency <= 0 or st["purchase_num"] >= frequency:
        return _refuse()

    from . import shop
    if cost and shop.spend_carats(full_state, cost) is None:
        return _refuse()
    summary = daily_races._empty_summary()
    daily_races._grant(full_state, _ticket_category(), TICKET_ITEM_ID,
                       _TICKET_PER_PURCHASE, summary)
    st["purchase_num"] += 1
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
        "reward_summary_info": summary,
        "purchase_num": st["purchase_num"],
    })


@registry.endpoint("legend_race/reset")
def handle_reset(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    st.pop("pending", None)
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": 0})


@registry.endpoint("legend_race/resume")
def handle_resume(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending") or {}
    if not pending:
        state = 0
    elif pending.get("result_rank") is not None:
        state = 4
    else:
        state = 2
    race_id = pending.get("race_id")
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_horse_data_array": copy.deepcopy(
            pending.get("race_horse_data_array") or []),
        "season": pending.get("season") or 0,
        "weather": pending.get("weather") or 0,
        "ground_condition": pending.get("ground") or 0,
        "random_seed": pending.get("random_seed") or 0,
        "race_instance_id": pending.get("race_instance_id") or 0,
        "race_scenario": pending.get("race_scenario"),
        "state": state,
        "purchase_num": st["purchase_num"],
        "is_cleared": 1 if race_id in st["cleared"] else 0,
    })
