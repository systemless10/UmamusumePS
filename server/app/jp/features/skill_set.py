
from __future__ import annotations

import copy
import datetime
import json
import random
import sqlite3
import threading
import time

from ... import region
from ... import state as state_store
from ...result_codes import ResultCode, refuse

STATE_KEY = "jp_skill_set"
DECKS = 10
SKILL_COUNT_MAX = 200
SHARES_PER_DAY = 100
SHARE_DAYS = 120
TRAINING_ROADMAP_DECK_ID = 101
JST = datetime.timezone(datetime.timedelta(hours=9))
_RESET_HOUR_JST = 5
NO_TIME = "0000-00-00 00:00:00"

_DEFAULT_POLICY = {
    "skill_point_allocate_priority_type": 1,
    "super_priority_allocate_priority_type": 1,
    "super_priority_min_hint_lv_required_to_skill_get": 0,
    "high_priority_allocate_priority_type": 1,
    "high_priority_min_hint_lv_required_to_skill_get": 0,
    "normal_priority_allocate_priority_type": 1,
    "normal_priority_min_hint_lv_required_to_skill_get": 0,
    "excess_point_allocate_setting_flag_not_include_skill_set": False,
}


def _ok(data=None) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data if data is not None else {}}


def _jst(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S")


def _game_day(ts: float) -> str:
    return (datetime.datetime.fromtimestamp(ts, JST)
            - datetime.timedelta(hours=_RESET_HOUR_JST)).strftime("%Y-%m-%d")


def default_info() -> dict:
    return {
        "skill_set_deck_array": [
            {"skill_set_deck_id": i, "deck_name": f"スキルセット{i}", "share_id": 0,
             "expiration_time": NO_TIME, "skill_set_skill_array": []}
            for i in range(1, DECKS + 1)],
        "skill_set_policy_get_skill": copy.deepcopy(_DEFAULT_POLICY),
        "auto_call_skill_set_deck_id": 0,
        "num_of_share_on_the_day": SHARES_PER_DAY,
    }


def info(full_state: dict) -> dict:
    blob = full_state.setdefault(STATE_KEY, default_info())
    for k, v in _DEFAULT_POLICY.items():
        blob.setdefault("skill_set_policy_get_skill", {}).setdefault(k, v)
    today = _game_day(time.time())
    if blob.get("share_day") != today:
        blob["share_day"] = today
        blob["num_of_share_on_the_day"] = SHARES_PER_DAY
    return blob


def public(blob: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in blob.items() if k != "share_day"}


def _deck(blob: dict, deck_id) -> dict | None:
    return next((d for d in blob["skill_set_deck_array"]
                 if d["skill_set_deck_id"] == deck_id), None)


def _clean_skills(raw) -> list | None:
    if not isinstance(raw, list) or len(raw) > SKILL_COUNT_MAX:
        return None
    out, seen = [], set()
    for s in raw:
        if not isinstance(s, dict):
            return None
        try:
            row = {"priority_type": int(s["priority_type"]),
                   "priority_value": int(s["priority_value"]),
                   "skill_id": int(s["skill_id"])}
        except (KeyError, TypeError, ValueError):
            return None
        if row["priority_type"] not in (1, 2, 3) or row["skill_id"] in seen:
            return None
        seen.add(row["skill_id"])
        out.append(row)
    return out


def _deck_reply(deck: dict) -> dict:
    return {"skill_set_deck": {
        "skill_set_deck_id": deck["skill_set_deck_id"], "deck_name": deck["deck_name"],
        "share_id": deck["share_id"], "expiration_time": deck["expiration_time"],
        "skill_set_skill_array": [dict(skill_set_deck_id=deck["skill_set_deck_id"], **s)
                                  for s in deck["skill_set_skill_array"]]}}


def _stored(deck: dict) -> dict:
    return _deck_reply(deck)["skill_set_deck"]


_lock = threading.Lock()
_conns: dict = {}


def _db() -> sqlite3.Connection:
    key = region.CURRENT_REGION.get()
    conn = _conns.get(key)
    if conn is None:
        conn = sqlite3.connect(region.data_dir() / "skill_set_share.sqlite3", check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS skill_set_share (
                            share_id    INTEGER PRIMARY KEY,
                            viewer_id   TEXT NOT NULL,
                            deck_name   TEXT NOT NULL,
                            skills      TEXT NOT NULL,
                            expire_ts   INTEGER NOT NULL)""")
        _conns[key] = conn
    return conn


def _new_share_id(conn) -> int:
    while True:
        sid = random.randint(100_000_000, 999_999_999)
        if not conn.execute("SELECT 1 FROM skill_set_share WHERE share_id=?", (sid,)).fetchone():
            return sid


def _live_share(conn, share_id):
    row = conn.execute("SELECT viewer_id, deck_name, skills, expire_ts FROM skill_set_share "
                       "WHERE share_id=?", (share_id,)).fetchone()
    if not row or row[3] < time.time():
        return None
    return row


def handle_index(payload: dict) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    blob = info(full_state)
    state_store.save_state(payload["viewer_id"], full_state)
    out = public(blob)
    out["skill_set_deck_array"] = [_stored(d) for d in blob["skill_set_deck_array"]]
    from . import roadmap
    return _ok({"skill_set_info": out,
                "training_roadmap_selected_info": roadmap.selected_info(full_state)})


def _change(payload: dict, *, skills: bool, name: bool) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    blob = info(full_state)
    deck = _deck(blob, payload.get("skill_set_deck_id"))
    if deck is None:
        return refuse()
    if skills:
        cleaned = _clean_skills(payload.get("skill_set_skill_array"))
        if cleaned is None:
            return refuse()
        deck["skill_set_skill_array"] = cleaned
        deck["share_id"], deck["expiration_time"] = 0, NO_TIME
    if name:
        new_name = payload.get("deck_name")
        if not isinstance(new_name, str) or not new_name.strip():
            return refuse()
        deck["deck_name"] = new_name
    state_store.save_state(payload["viewer_id"], full_state)
    return _ok(_deck_reply(deck))


def handle_change_deck_skill(payload: dict) -> dict:
    return _change(payload, skills=True, name=False)


def handle_change_deck_name(payload: dict) -> dict:
    return _change(payload, skills=False, name=True)


def handle_change_deck_skill_and_name(payload: dict) -> dict:
    return _change(payload, skills=True, name=True)


def handle_change_policy_get_skill(payload: dict) -> dict:
    policy = payload.get("skill_set_policy_get_skill")
    if not isinstance(policy, dict):
        return refuse()
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    blob = info(full_state)
    cur = blob["skill_set_policy_get_skill"]
    for k in _DEFAULT_POLICY:
        if k in policy:
            cur[k] = bool(policy[k]) if isinstance(_DEFAULT_POLICY[k], bool) else int(policy[k])
    state_store.save_state(payload["viewer_id"], full_state)
    return _ok({})


def handle_change_auto_call_deck_id(payload: dict) -> dict:
    deck_id = int(payload.get("skill_set_deck_id") or 0)
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    blob = info(full_state)
    if deck_id not in (0, TRAINING_ROADMAP_DECK_ID) and _deck(blob, deck_id) is None:
        return refuse()
    blob["auto_call_skill_set_deck_id"] = deck_id
    state_store.save_state(payload["viewer_id"], full_state)
    return _ok({})


def handle_share(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    blob = info(full_state)
    deck = _deck(blob, payload.get("skill_set_deck_id"))
    if deck is None or blob["num_of_share_on_the_day"] <= 0:
        return refuse()
    now = time.time()
    expire = int(now) + SHARE_DAYS * 86400
    with _lock:
        conn = _db()
        with conn:
            sid = _new_share_id(conn)
            conn.execute("INSERT INTO skill_set_share VALUES (?,?,?,?,?)",
                         (sid, str(viewer_id), deck["deck_name"],
                          json.dumps(deck["skill_set_skill_array"]), expire))
    deck["share_id"], deck["expiration_time"] = sid, _jst(expire)
    blob["num_of_share_on_the_day"] -= 1
    state_store.save_state(viewer_id, full_state)
    return _ok({"share_id": sid, "expiration_time": deck["expiration_time"],
                "num_of_share_on_the_day": blob["num_of_share_on_the_day"]})


def handle_get_share_info(payload: dict) -> dict:
    with _lock:
        row = _live_share(_db(), payload.get("share_id"))
    if row is None:
        return refuse(ResultCode.SKILL_SET_SHARE_ID_NOT_EXIST)
    return _ok({"deck_name": row[1], "skill_set_skill_array": json.loads(row[2])})


def handle_get_extension_period(payload: dict) -> dict:
    with _lock:
        row = _live_share(_db(), payload.get("share_id"))
    if row is None or row[0] != str(payload["viewer_id"]):
        return refuse(ResultCode.SKILL_SET_SHARE_ID_NOT_EXIST)
    return _ok({"expiration_time": _jst(int(time.time()) + SHARE_DAYS * 86400)})


def handle_extension_share_period(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    share_id = payload.get("share_id")
    full_state = state_store.get_state(viewer_id) or {}
    blob = info(full_state)
    with _lock:
        conn = _db()
        row = _live_share(conn, share_id)
        if row is None or row[0] != str(viewer_id):
            return refuse(ResultCode.SKILL_SET_SHARE_ID_NOT_EXIST)
        if blob["num_of_share_on_the_day"] <= 0:
            return refuse(ResultCode.SKILL_SET_SHARE_ID_FAILED_EXTEND_EXPIRATION)
        expire = int(time.time()) + SHARE_DAYS * 86400
        with conn:
            conn.execute("UPDATE skill_set_share SET expire_ts=? WHERE share_id=?", (expire, share_id))
    for d in blob["skill_set_deck_array"]:
        if d["share_id"] == share_id:
            d["expiration_time"] = _jst(expire)
    blob["num_of_share_on_the_day"] -= 1
    state_store.save_state(viewer_id, full_state)
    return _ok({"num_of_share_on_the_day": blob["num_of_share_on_the_day"]})


HANDLERS = {
    "skill_set/index": handle_index,
    "skill_set/change_deck_skill": handle_change_deck_skill,
    "skill_set/change_deck_name": handle_change_deck_name,
    "skill_set/change_deck_skill_and_name": handle_change_deck_skill_and_name,
    "skill_set/change_policy_get_skill": handle_change_policy_get_skill,
    "skill_set/change_auto_call_deck_id": handle_change_auto_call_deck_id,
    "skill_set/share": handle_share,
    "skill_set/get_share_info": handle_get_share_info,
    "skill_set/get_extension_period": handle_get_extension_period,
    "skill_set/extension_share_period": handle_extension_share_period,
}
