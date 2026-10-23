
from __future__ import annotations

import random
import time

from .. import master_data
from .. import social
from .. import state as state_store
from . import registry
from ..result_codes import ResultCode, refuse

JUKEBOX_STATE_KEY = "jukebox_state"

_HISTORY_LIMIT = 100
_RANDOM_COOLDOWN_SEC = 15 * 60
_REQUEST_VALUE_SUFFIX = 103
_FALLBACK_REQUESTER = 1001


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _jukebox_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(JUKEBOX_STATE_KEY)
    if isinstance(st, dict):
        return st
    hist = state_store.extract_json_path(
        viewer_id, "load_index", "$.data.jukebox_request_history")
    if hist is None:
        from .load import get_or_seed_blob
        hist = ((get_or_seed_blob(full_state, viewer_id).get("data") or {})
                .get("jukebox_request_history") or {})
    if not isinstance(hist, dict):
        hist = {}
    st = {"next_request_id": (hist.get("request_id") or 0) + 1,
          "history": [], "random_count": 0}
    full_state[JUKEBOX_STATE_KEY] = st
    return st


def _owned_music_ids(full_state: dict, viewer_id) -> set:
    music = full_state.get("music_list_state")
    if music is None:
        music = state_store.extract_json_path(
            viewer_id, "load_index", "$.data.music_list")
        if music is None:
            from .load import get_or_seed_blob
            music = ((get_or_seed_blob(full_state, viewer_id).get("data") or {})
                     .get("music_list") or [])
        if not isinstance(music, list):
            music = []
    return {m.get("music_id") for m in music if isinstance(m, dict)}


@registry.endpoint("jukebox/play_user_request")
def handle_play_user_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    music_id = payload.get("music_id")
    music_size = payload.get("music_size", 1)
    if not isinstance(music_id, int) or not isinstance(music_size, int) \
            or music_size < 0:
        return _refuse()
    row = master_data.query_one(
        "SELECT music_id, condition_type FROM jukebox_music_data "
        "WHERE music_id=?", (music_id,))
    if row is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if row["condition_type"] and music_id not in _owned_music_ids(full_state, viewer_id):
        return _refuse()

    st = _jukebox_state(full_state, viewer_id)
    request_id = st["next_request_id"]
    st["next_request_id"] = request_id + 1
    st["history"].append({
        "request_id": request_id, "request_type": 0, "request_value": 0,
        "requester_id": viewer_id, "requester_request_id": request_id,
        "music_id": music_id, "request_time": _now(),
    })
    del st["history"][:-_HISTORY_LIMIT]
    from . import missions
    missions.mark_achieved(full_state, missions.FLAG_JUKEBOX_REQUESTED)
    state_store.save_state(viewer_id, full_state)
    return _ok({"request_id": request_id})


@registry.endpoint("jukebox/draw_random_request")
def handle_draw_random_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)

    owned = _owned_music_ids(full_state, viewer_id)
    pool = [r["music_id"] for r in master_data.query(
        "SELECT music_id, condition_type FROM jukebox_music_data "
        "WHERE is_hidden=0 AND request_type=0")
        if not r["condition_type"] or r["music_id"] in owned]
    if not pool:
        return _refuse()

    charas = full_state.get("chara_collection") or []
    chara_ids = [c.get("chara_id") for c in charas
                 if isinstance(c, dict) and c.get("chara_id")]
    requester = random.choice(chara_ids) if chara_ids else _FALLBACK_REQUESTER

    st["random_count"] = (st.get("random_count") or 0) + 1
    entry = {
        "request_type": 1,
        "request_value": requester * 1000 + _REQUEST_VALUE_SUFFIX,
        "music_id": random.choice(pool),
        "requester_id": requester,
        "request_id": st["random_count"],
        "requester_request_id": None,
    }
    next_time = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() + _RANDOM_COOLDOWN_SEC))
    st["last_random"] = entry
    st["next_random_request_time"] = next_time
    st["history"].append(dict(entry, request_time=_now()))
    del st["history"][:-_HISTORY_LIMIT]
    state_store.save_state(viewer_id, full_state)
    return _ok({"request_history": entry,
                "next_random_request_time": next_time,
                "add_music_array": []})


_SETTING_DEFAULTS = {"play_music_flag": 1,
                     "random_request_flag": 1,
                     "user_request_flag": 1}


def settings(full_state: dict) -> dict:
    st = full_state.get(JUKEBOX_STATE_KEY)
    stored = (st.get("settings") or {}) if isinstance(st, dict) else {}
    return {name: int(stored.get(name, default))
            for name, default in _SETTING_DEFAULTS.items()}


def _change_setting(payload: dict, field: str) -> dict:
    viewer_id = payload["viewer_id"]
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)
    st.setdefault("settings", {})[field] = 1 if value else 0
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("jukebox/change_play_music")
def handle_change_play_music(payload: dict) -> dict:
    return _change_setting(payload, "play_music_flag")


@registry.endpoint("jukebox/change_random_request")
def handle_change_random_request(payload: dict) -> dict:
    return _change_setting(payload, "random_request_flag")


@registry.endpoint("jukebox/change_user_request")
def handle_change_user_request(payload: dict) -> dict:
    return _change_setting(payload, "user_request_flag")


_UMA_LIKES_MIN = 7
_UMA_LIKES_MAX = 10


def _request_key(entry: dict) -> str:
    return f"{entry.get('requester_id') or 0}:{entry.get('request_id') or 0}"


def _uma_like_pool(full_state: dict) -> list:
    return sorted(master_data.drawable_charas())


def _roll_uma_likes(st: dict, history, full_state: dict) -> None:
    from . import daily_races

    likes = st.setdefault("likes", [])
    live = {_request_key(h) for h in history}
    likes = [l for l in likes if l.get("request_key") in live]
    st.pop("like_rolled", None)

    today = daily_races._served_day()
    if st.get("like_day") != today:
        st["like_day"] = today
        likes = [l for l in likes if not l.get("like_chara_id")]
        pool = _uma_like_pool(full_state)
        entries = list(history)
        if pool and entries:
            count = min(len(pool), random.randint(_UMA_LIKES_MIN, _UMA_LIKES_MAX))
            for chara_id in random.sample(pool, count):
                entry = random.choice(entries)
                likes.append({"request_key": _request_key(entry),
                              "request_id": entry.get("request_id"),
                              "like_chara_id": chara_id,
                              "like_viewer_id": 0})
    st["likes"] = likes


@registry.endpoint("jukebox/exec_like")
def handle_exec_like(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    request_id = payload.get("request_id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        return _refuse()
    target = payload.get("request_viewer_id")
    if target is not None and (not isinstance(target, int) or isinstance(target, bool)):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)
    history = st.get("history") or ()
    entry = None
    if target:
        entry = next((h for h in history
                      if h.get("request_id") == request_id
                      and h.get("requester_id") == target), None)
    if entry is None:
        entry = next((h for h in history
                      if h.get("request_id") == request_id), None)
    if entry is None:
        return _refuse()

    key = _request_key(entry)
    likes = st.setdefault("likes", [])
    mine = social._wire_id(viewer_id)
    if not any(l.get("request_key") == key and l.get("like_viewer_id") == mine
               for l in likes):
        likes.append({"request_key": key,
                      "request_id": entry.get("request_id"),
                      "like_chara_id": 0,
                      "like_viewer_id": mine})
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("jukebox/history")
def handle_history(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)

    history = list(st.get("history") or ())
    _roll_uma_likes(st, history, full_state)

    rows = [{"request_id": h.get("request_id"),
             "request_type": h.get("request_type") or 0,
             "request_value": h.get("request_value") or 0,
             "requester_id": social._wire_id(h.get("requester_id") or 0),
             "music_id": h.get("music_id"),
             "request_time": h.get("request_time") or _now()}
            for h in reversed(history)]

    order = {_request_key(h): i for i, h in enumerate(reversed(history))}
    stored = st.get("likes") or []
    stored = sorted(stored, key=lambda l: (order.get(l.get("request_key"), 1 << 30),
                                           0 if l.get("like_viewer_id") else 1,
                                           l.get("like_chara_id") or 0))
    likes = [{"request_id": l.get("request_id"),
              "sort_id": i,
              "like_chara_id": l.get("like_chara_id") or 0,
              "like_viewer_id": l.get("like_viewer_id") or 0}
             for i, l in enumerate(stored, start=1)]

    from . import directory
    card = directory.build_card(viewer_id, full_state)
    own = directory.summary(viewer_id, card) if card else None
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "like_array": likes,
        "latest_request_history": rows[0] if rows else None,
        "request_history_array": rows,
        "summary_user_info_array": [own] if own else [],
    })
