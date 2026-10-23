
from __future__ import annotations

import copy
import time

from .. import concerts
from .. import master_data
from .. import patch
from .. import state as state_store
from . import bond, collection, registry, shop
from ..result_codes import ResultCode, refuse

STORY_STATE_KEY = "story_state"
_JEWEL_CATEGORY = 90
_HONOR_CATEGORY = 55
_LOCK_NONE = 0
_LOCK_BOND = 6

_READ_INFO_FAMILIES = {
    "add_home_story_data_array": "home_story_data_array",
    "add_short_episode_data_array": "short_episode_data_array",
    "add_home_poster_data_array": "home_poster_data_array",
    "add_tutorial_guide_data_array": "tutorial_guide_data_array",
    "add_released_episode_data_array": "released_episode_data_array",
    "add_talk_gallery_data_array": "talk_gallery_data_array",
    "add_home_banner_data_array": "home_banner_data_array",
}
_RESPONSE_ORDER = [
    "home_story_data_array", "short_episode_data_array",
    "home_poster_data_array", "tutorial_guide_data_array",
    "released_episode_data_array", "talk_gallery_data_array",
    "home_banner_data_array",
]
_SEED_KEYS = {k: k for k in _RESPONSE_ORDER}
_SEED_KEYS["talk_gallery_data_array"] = "talk_gallery_list"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _seed_data() -> dict:
    from .load import _load_seed
    return (_load_seed() or {}).get("data") or {}


def _extract_ids(entries) -> list:
    ids = []
    for e in entries or []:
        if isinstance(e, dict):
            v = e.get("id", e.get("home_story_trigger_id", e.get("episode_id")))
        else:
            v = e
        if isinstance(v, int):
            ids.append(v)
    return ids


def _story_state(full_state: dict) -> dict:
    st = full_state.get(STORY_STATE_KEY)
    if isinstance(st, dict):
        _mark_all_guides_read(st)
        return st
    seed = _seed_data()
    st = {
        "chara_cleared": [e.get("episode_id") for e in seed.get("character_story_data_list") or []
                          if e.get("state")],
        "main_cleared": [e.get("episode_id") for e in seed.get("main_story_data_list") or []
                         if e.get("state")],
        "read_info": {key: sorted(set(_extract_ids(seed.get(seed_key) or [])))
                      for key, seed_key in _SEED_KEYS.items()},
    }
    _mark_all_guides_read(st)
    full_state[STORY_STATE_KEY] = st
    return st


def _all_guide_groups() -> list:
    if not master_data.has_table("tutorial_guide_data"):
        return []
    return [r["group_id"] for r in master_data.query(
        "SELECT DISTINCT group_id FROM tutorial_guide_data")]


def _mark_all_guides_read(st: dict) -> None:
    read = st.setdefault("read_info", {})
    have = read.get("tutorial_guide_data_array") or []
    merged = sorted(set(have) | set(_all_guide_groups()))
    if merged != have:
        read["tutorial_guide_data_array"] = merged


def _love_rank(full_state: dict, chara_id: int) -> int:
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if charas is None:
        charas = _seed_data().get("chara_list") or []
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    return bond.love_rank((entry or {}).get("love_point") or 0)


def _lock_ok(full_state: dict, lock_type: int, value_1: int, value_2: int) -> bool:
    lock_type = lock_type or _LOCK_NONE
    if lock_type == _LOCK_NONE:
        return True
    if lock_type == _LOCK_BOND:
        return _love_rank(full_state, value_1) >= (value_2 or 0)
    return False


def _chara_episode_unlocked(full_state: dict, row, cleared: list) -> bool:
    if not _lock_ok(full_state, row["lock_type_1"],
                    row["lock_value_1_1"], row["lock_value_1_2"]):
        return False
    if (row["episode_index"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM chara_story_data WHERE chara_id=? AND episode_index=?",
            (row["chara_id"], row["episode_index"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


def _wallet(full_state: dict) -> dict:
    wallet = full_state.get("coin_info_state")
    if not isinstance(wallet, dict):
        wallet = copy.deepcopy(_seed_data().get("coin_info") or {"fcoin": 0, "coin": 0})
        full_state["coin_info_state"] = wallet
    return wallet


def _grant_reward(full_state: dict, category: int, reward_id: int, num: int,
                  summary: dict, viewer_id=None) -> list:
    category = category or 0
    if not category or not num or num <= 0:
        return []
    if category == _JEWEL_CATEGORY:
        wallet = _wallet(full_state)
        wallet["fcoin"] = (wallet.get("fcoin") or 0) + num
        summary["add_fcoin"] += num
    elif category == _HONOR_CATEGORY:
        from . import user_profile
        if viewer_id and user_profile.grant_honor(viewer_id, reward_id):
            summary.setdefault("add_honor_list", []).append(
                {"honor_id": reward_id, "create_time": time.strftime("%Y-%m-%d %H:%M:%S")})
    else:
        shop._grant(full_state,
                    {"change_item_category": category, "change_item_id": reward_id,
                     "change_item_num": num, "additional_piece_num": 0},
                    1, summary)
    return [{"item_type": category, "item_id": reward_id, "item_num": num}]


@registry.endpoint("character_story/first_clear")
def handle_first_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM chara_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("chara_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        if not _chara_episode_unlocked(full_state, row, cleared):
            return _refuse()
        reward_array = _grant_reward(
            full_state, row["add_reward_category_1"], row["add_reward_id_1"],
            row["add_reward_num_1"], summary, viewer_id)
        cleared.append(episode_id)
        read = st.setdefault("read_info", {})
        released = set(read.get("released_episode_data_array") or [])
        released.add(row["story_id"])
        read["released_episode_data_array"] = sorted(released)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "character_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "release_item_flag": 0,
    })


def add_home_stories(full_state: dict, ids) -> None:
    ids = {i for i in ids if isinstance(i, int)}
    if not ids:
        return
    read = _story_state(full_state).setdefault("read_info", {})
    for key in ("home_story_data_array", "talk_gallery_data_array"):
        read[key] = sorted(set(read.get(key) or []) | ids)


@registry.endpoint("read_info/index")
def handle_read_info_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    read = st.setdefault("read_info", {})
    for req_key, resp_key in _READ_INFO_FAMILIES.items():
        ids = _extract_ids(payload.get(req_key))
        if not ids:
            continue
        read[resp_key] = sorted(set(read.get(resp_key) or []) | set(ids))
        if resp_key == "home_story_data_array":
            read["talk_gallery_data_array"] = sorted(
                set(read.get("talk_gallery_data_array") or []) | set(ids))
    viewed = _extract_ids(payload.get("add_viewed_story_array"))
    if viewed:
        read["viewed_story_array"] = sorted(
            set(read.get("viewed_story_array") or []) | set(viewed))
    state_store.save_state(viewer_id, full_state)
    data = {}
    for resp_key in _RESPONSE_ORDER:
        ids = read.get(resp_key) or []
        if resp_key == "talk_gallery_data_array":
            data[resp_key] = [{"home_story_trigger_id": i, "new_flag": 0} for i in ids]
        else:
            data[resp_key] = [{"id": i} for i in ids]
    return _ok(data)


def _main_story_unlocked(full_state: dict, row, cleared: list) -> bool:
    prev_index = row["prev_episode_index"] or 0
    if prev_index:
        prev = master_data.query_one(
            "SELECT id FROM main_story_data WHERE part_id=? AND episode_index=?",
            (row["part_id"], prev_index))
        if prev and prev["id"] not in cleared:
            return False
    for slot in (1, 2, 3):
        if not _lock_ok(full_state, row[f"lock_type_{slot}"],
                        row[f"lock_value_{slot}_1"], row[f"lock_value_{slot}_2"]):
            return False
    return True


def clear_main_story(full_state: dict, episode_id: int, summary: dict, viewer_id=None):
    row = master_data.query_one(
        "SELECT * FROM main_story_data WHERE id=?", (episode_id,))
    if row is None:
        return False, []
    st = _story_state(full_state)
    cleared = st.setdefault("main_cleared", [])
    if episode_id in cleared:
        return True, []
    if not _main_story_unlocked(full_state, row, cleared):
        return False, []
    reward_array = _grant_reward(
        full_state, row["add_reward_category_1"], row["add_reward_id_1"],
        row["add_reward_num_1"], summary, viewer_id)
    cleared.append(episode_id)
    return True, reward_array


@registry.endpoint("main_story/first_clear")
def handle_main_story_first_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    full_state = state_store.get_state(viewer_id) or {}
    summary = shop._empty_summary()
    ok, reward_array = clear_main_story(full_state, episode_id, summary, viewer_id)
    if not ok:
        return _refuse()
    songs = concerts.on_story_clear(full_state, "main_story", episode_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "main_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "add_music_array": songs,
        "release_item_flag": 0,
    })


def _story_event_unlocked(row, cleared: list) -> bool:
    if (row["episode_index_id"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM story_event_story_data "
            "WHERE story_event_id=? AND episode_index_id=?",
            (row["story_event_id"], row["episode_index_id"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


@registry.endpoint("story_event/story_clear")
def handle_story_event_story_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM story_event_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("story_event_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        points = full_state.get("story_event_point", {}).get(str(row["story_event_id"]), 0)
        if (row["start_date"] > patch._servertime() or points < row["need_point"]
                or not _story_event_unlocked(row, cleared)):
            return _refuse()
        reward_array = _grant_reward(
            full_state, row["add_reward_category_1"], row["add_reward_id_1"],
            row["add_reward_num_1"], summary, viewer_id)
        reward_array += _grant_reward(
            full_state, row["add_reward_category_2"], row["add_reward_id_2"],
            row["add_reward_num_2"], summary, viewer_id)
        cleared.append(episode_id)
    songs = concerts.on_story_clear(full_state, "story_event", episode_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "new_story_id_list": [],
        "add_music_array": songs,
    })


def _extra_story_unlocked(row, cleared: list) -> bool:
    if (row["episode_index_id"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM story_extra_story_data "
            "WHERE story_extra_id=? AND episode_index_id=?",
            (row["story_extra_id"], row["episode_index_id"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


@registry.endpoint("extra_story/first_clear")
def handle_extra_story_first_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM story_extra_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("extra_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        now_ts = patch._servertime()
        if not (row["start_date"] <= now_ts <= row["end_date"]):
            return _refuse()
        if not _extra_story_unlocked(row, cleared):
            return _refuse()
        for slot in (1, 2):
            if not row[f"add_reward_category_{slot}"]:
                continue
            reward_array += _grant_reward(
                full_state, row[f"add_reward_category_{slot}"],
                row[f"add_reward_id_{slot}"], row[f"add_reward_num_{slot}"],
                summary, viewer_id)
        cleared.append(episode_id)
        read = st.setdefault("read_info", {})
        released = set(read.get("released_episode_data_array") or [])
        for i in range(1, 6):
            sid = row[f"story_id_{i}"]
            if sid:
                released.add(sid)
        read["released_episode_data_array"] = sorted(released)

    songs = concerts.on_story_clear(full_state, "extra_story", episode_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "extra_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "add_music_array": songs,
    })
