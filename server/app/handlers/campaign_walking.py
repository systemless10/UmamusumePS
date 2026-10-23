
from __future__ import annotations

import functools
import time

from .. import master_data
from .. import state as state_store
from . import bond, daily_races, registry, shop
from ..result_codes import ResultCode, refuse

STATE_KEY = "campaign_walking_state"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@functools.lru_cache(maxsize=1)
def _all_data_rows() -> tuple:
    return tuple(master_data.query("SELECT * FROM campaign_walking_data"))


def _active_row(now_ts: int):
    for row in _all_data_rows():
        camp = master_data.query_one(
            "SELECT start_time, end_time FROM campaign_data WHERE campaign_id=?",
            (row["campaign_id"],))
        if camp and camp["start_time"] <= now_ts <= camp["end_time"]:
            return row
    return None


@functools.lru_cache(maxsize=256)
def _chara_row(campaign_id: int, chara_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_chara WHERE campaign_id=? AND chara_id=?",
        (campaign_id, chara_id))


@functools.lru_cache(maxsize=256)
def _location_row(location_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_location WHERE id=?", (location_id,))


@functools.lru_cache(maxsize=256)
def _reward_row(reward_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_reward_set WHERE id=?", (reward_id,))


def _served_day() -> int:
    return daily_races._served_day()


def _state(full_state: dict, row) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    if st.get("campaign_id") != row["campaign_id"]:
        st.clear()
        st["campaign_id"] = row["campaign_id"]
        st["gauge"] = 0
        st["day"] = _served_day()
        st["today_walking_num"] = 0
        st["claimed_reward_ids"] = []
        st["acts"] = []
        st["pending"] = None
    day = _served_day()
    if st.get("day") != day:
        st["day"] = day
        st["today_walking_num"] = 0
    st.setdefault("claimed_reward_ids", [])
    st.setdefault("acts", [])
    return st


def add_gauge(full_state: dict, kind: str) -> tuple[int, int] | None:
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return None
    st = _state(full_state, row)
    before = st["gauge"]
    amount = row[f"gauge_up_{kind}"] or 0
    st["gauge"] = min(row["gauge_max"], before + amount)
    return (before, st["gauge"])


def gauge_info(full_state: dict, kind: str) -> dict | None:
    result = add_gauge(full_state, kind)
    if result is None:
        return None
    before, after = result
    return {"before_gauge": before, "after_gauge": after}


@registry.endpoint("campaign_walking/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _ok({"walking_gauge": 0, "today_walking_num": 0,
                     "walking_reward_info": [], "chara_walking_act_array": []})
    st = _state(full_state, row)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "walking_gauge": st["gauge"],
        "today_walking_num": st["today_walking_num"],
        "walking_reward_info": [{"reward_id": rid, "exchange_count": 1}
                                for rid in st["claimed_reward_ids"]],
        "chara_walking_act_array": [
            {"chara_id": a["chara_id"], "data_id": a["data_id"],
             "create_time": a["create_time"], "new_flag": 0}
            for a in st["acts"]],
    })


def load_info(viewer_id) -> dict:
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return {"walking_gauge": 0, "today_walking_num": 0,
                "show_login_bonus": False, "show_tips": False,
                "resume_state": 0, "resume_chara_id": 0, "resume_location_id": 0}
    st = _state(full_state, row)
    pending = st.get("pending")
    return {
        "walking_gauge": st["gauge"],
        "today_walking_num": st["today_walking_num"],
        "show_login_bonus": False,
        "show_tips": False,
        "resume_state": 1 if pending else 0,
        "resume_chara_id": (pending or {}).get("chara_id", 0),
        "resume_location_id": (pending or {}).get("location_id", 0),
    }


@registry.endpoint("campaign_walking/go_walking")
def handle_go_walking(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _refuse(ResultCode.CAMPAIGN_WALKING_NOT_IN_TERM)
    st = _state(full_state, row)

    chara_id = payload.get("chara_id")
    location_id = payload.get("location_id")
    reward_id = payload.get("reward_id")

    chara = _chara_row(row["campaign_id"], chara_id)
    if chara is None or location_id not in (
            chara["location_1"], chara["location_2"], chara["location_3"]):
        return _refuse()
    loc = _location_row(location_id)
    reward = _reward_row(reward_id)
    if loc is None or reward is None or reward["reward_set_id"] != loc["reward_set_id"]:
        return _refuse()
    if st["gauge"] < row["gauge_max"] or st["today_walking_num"] >= row["walking_limit"]:
        return _refuse()

    st["gauge"] = 0
    st["today_walking_num"] += 1
    st["pending"] = {"chara_id": chara_id, "location_id": location_id,
                     "reward_id": reward_id}
    state_store.save_state(viewer_id, full_state)
    return _ok({"today_walking_num": st["today_walking_num"]})


def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


_CATEGORY_CARAT = 90
_CATEGORY_MONEY = 91


def _grant_reward(full_state: dict, reward, summary: dict) -> dict:
    category, item_id, num = reward["item_category"], reward["item_id"], reward["item_num"]
    if category == _CATEGORY_CARAT:
        shop.grant_carats(full_state, num)
        summary["add_fcoin"] += num
    else:
        shop._add_item(full_state, item_id, num)
        summary["add_item_list"].append({"item_id": item_id, "number": num})
    return {"item_type": category, "item_id": item_id, "item_num": num}


@registry.endpoint("campaign_walking/check_result")
def handle_check_result(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _refuse(ResultCode.CAMPAIGN_WALKING_NOT_IN_TERM)
    st = _state(full_state, row)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    st["pending"] = None

    chara_id = pending["chara_id"]
    location_id = pending["location_id"]
    reward_id = pending["reward_id"]
    loc = _location_row(location_id)
    love_gain = (loc["love_point"] if loc else 0) or 0

    love_info, new_profiles = bond.grant(full_state, chara_id, love_gain)

    summary = _empty_summary()
    reward_list = []
    if reward_id not in st["claimed_reward_ids"]:
        reward = _reward_row(reward_id)
        if reward is not None:
            reward_list.append(_grant_reward(full_state, reward, summary))
            st["claimed_reward_ids"].append(reward_id)

    now = _now()
    already = any(a["chara_id"] == chara_id and a["data_id"] == location_id
                  for a in st["acts"])
    if not already:
        st["acts"].append({"chara_id": chara_id, "data_id": location_id, "create_time": now})
    new_walking_act = {"chara_id": chara_id, "data_id": location_id,
                       "create_time": now, "new_flag": 1}

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "result": {
            "chara_id": chara_id,
            "love_point_info": love_info,
            "reward_list": reward_list,
            "new_walking_act": new_walking_act,
            "new_chara_profile_array": new_profiles,
        },
        "reward_summary_info": summary,
    })
