
from __future__ import annotations

import time

from .. import master_data
from .. import state as state_store
from . import registry, shop
from ..result_codes import refuse

STATE_KEY = "map_event_state"
ENDING_LOCKED, ENDING_UNREAD, ENDING_READ = 1, 2, 3
LAST_DISPLAYED_CHECKPOINT = 9


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _active(now_ts: int | None = None):
    now_ts = now_ts or int(time.time())
    for row in master_data.query("SELECT * FROM map_event_data"):
        if row["start_date"] <= now_ts <= row["end_date"]:
            return row
    return None


def _points(event_id: int) -> list:
    return master_data.query(
        "SELECT * FROM map_event_map_point WHERE event_id=? ORDER BY map_point_id", (event_id,))


def _open_areas(event_id: int, now_ts: int) -> list[int]:
    return [r["area_id"] for r in master_data.query(
        "SELECT * FROM map_event_area_data WHERE event_id=? ORDER BY area_id", (event_id,))
        if r["start_date"] <= now_ts]


def _state(full_state: dict, row) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    if st.get("event_id") != row["event_id"]:
        st.clear()
        st.update(event_id=row["event_id"], gauge=0, arrival=0, ending=ENDING_LOCKED,
                  read_flavor=[], read_episode=[], seen_areas=[], first_access=1,
                  login_update=0)
    return st


def add_gauge(full_state: dict, kind: str) -> tuple[int, int] | None:
    row = _active()
    if row is None:
        return None
    st = _state(full_state, row)
    before = st["gauge"]
    st["gauge"] = min(row["gauge_max"], before + (row[f"gauge_up_{kind}"] or 0))
    if kind == "login":
        st["login_update"] = 1
    return before, st["gauge"]


def gauge_info(full_state: dict, kind: str) -> dict | None:
    result = add_gauge(full_state, kind)
    return None if result is None else {"before_gauge": result[0], "after_gauge": result[1]}


def _next_point(row, st, now_ts):
    areas = _open_areas(row["event_id"], now_ts)
    for p in _points(row["event_id"]):
        if p["map_point_id"] > st["arrival"]:
            return p if p["area_id"] in areas and p["start_date"] <= now_ts else None
    return None


def _current_area(row, st) -> int:
    pts = {p["map_point_id"]: p for p in _points(row["event_id"])}
    return pts[st["arrival"]]["area_id"] if st["arrival"] in pts else 1


def _area_flag(row, st, now_ts, consume: bool) -> int:
    new = [a for a in _open_areas(row["event_id"], now_ts) if a not in st["seen_areas"]]
    if consume and new:
        st["seen_areas"] += new
    return int(any(a != 1 for a in new))


def _summary(row, st, now_ts, consume: bool) -> dict:
    nxt = _next_point(row, st, now_ts)
    return {"gauge": st["gauge"], "arrival_map_point_id": st["arrival"],
            "current_area_id": _current_area(row, st),
            "is_area_open": _area_flag(row, st, now_ts, consume),
            "login_update_flag": st["login_update"],
            "ending_read_type": st["ending"],
            "read_next_episode_id_array": list(st["read_episode"]),
            "_enable": [nxt["map_point_id"]] if nxt else []}


def load_info(viewer_id) -> dict | None:
    row = _active()
    if row is None:
        return None
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state, row)
    s = _summary(row, st, int(time.time()), consume=False)
    s.pop("_enable")
    return {"event_id": row["event_id"], **s, "first_access_flag": st["first_access"],
            "map_point_exec_flag": int(st["gauge"] >= row["gauge_max"] and
                                       _next_point(row, st, int(time.time())) is not None)}


@registry.endpoint("map_event/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    row = _active()
    if row is None:
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state, row)
    s = _summary(row, st, int(time.time()), consume=True)
    st["first_access"] = st["login_update"] = 0
    state_store.save_state(viewer_id, full_state)
    return _ok({**{k: v for k, v in s.items() if k != "_enable"},
                "enable_map_point_id_array": s["_enable"],
                "read_flavor_text_id_array": list(st["read_flavor"]),
                "playlist_link": ""})


@registry.endpoint("map_event/point_exec")
def handle_point_exec(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    row = _active()
    if row is None:
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state, row)
    now_ts = int(time.time())
    nxt = _next_point(row, st, now_ts)
    if nxt is None or nxt["map_point_id"] != payload.get("map_point_id") or st["gauge"] < row["gauge_max"]:
        return refuse()
    st["gauge"] = 0
    st["arrival"] = nxt["map_point_id"]
    rewards = []
    summary = {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
               "add_support_card_list": [], "add_cloth_list": [], "add_music_list": [],
               "add_fcoin": 0, "add_chara_profile_list": []}
    if nxt["item_id_1"]:
        shop._add_item(full_state, nxt["item_id_1"], nxt["item_num_1"])
        summary["add_item_list"].append({"item_id": nxt["item_id_1"], "number": nxt["item_num_1"]})
        rewards.append({"item_type": nxt["item_category_1"], "item_id": nxt["item_id_1"],
                        "item_num": nxt["item_num_1"]})
    if nxt["map_point_id"] >= LAST_DISPLAYED_CHECKPOINT and st["ending"] == ENDING_LOCKED:
        st["ending"] = ENDING_UNREAD
    movie = nxt["movie_start_date"]
    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_array": rewards, "reward_summary_info": summary,
                "story_id_array": [s for s in (nxt["story_id_1"], nxt["story_id_2"]) if s],
                "ending_read_type": st["ending"],
                "movie_release_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(movie))
                if isinstance(movie, int) and movie > now_ts else ""})


def _mark(payload: dict, fn) -> dict:
    viewer_id = payload["viewer_id"]
    row = _active()
    if row is None:
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    fn(_state(full_state, row))
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("map_event/flavor_text_read")
def handle_flavor_text_read(payload: dict) -> dict:
    fid = payload.get("flavor_text_id")
    return _mark(payload, lambda st: fid in st["read_flavor"] or st["read_flavor"].append(fid))


@registry.endpoint("map_event/movie_trailer_dialog_read")
def handle_trailer_read(payload: dict) -> dict:
    eid = payload.get("next_episode_id")
    return _mark(payload, lambda st: eid in st["read_episode"] or st["read_episode"].append(eid))


@registry.endpoint("map_event/ending_read")
def handle_ending_read(payload: dict) -> dict:
    def read(st):
        if st["ending"] == ENDING_UNREAD:
            st["ending"] = ENDING_READ
    return _mark(payload, read)
