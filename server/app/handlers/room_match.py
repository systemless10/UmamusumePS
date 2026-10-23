
from __future__ import annotations

import copy
import json
import logging
import random
import sqlite3
import time
from contextlib import contextmanager
from threading import RLock

from .. import config, patch, region, social
from .. import state as state_store
from ..result_codes import ResultCode, refuse
from ..simulation import race_simulator
from . import directory, practice_race, practice_race_lobby, race_mobs, registry, trained_chara

log = logging.getLogger("uma-server")

POLLING_INTERVAL = 3
MAX_CHARAS_PER_USER = 3
SAVED_RESULT_KEEP = 365 * 86400

SAVED_RESULT_LIMIT = 20
AUDIENCE_LIMIT = 100
ROOM_LIST_SIZE = 10
ROOM_LINGER = 7 * 86400

_START_DELAY = {1: 30 * 60, 2: 3600, 3: 3 * 3600, 4: 6 * 3600, 5: 12 * 3600, 6: 86400}
START_TIME_TYPE_SIMPLE = 1

USER_ENTRY, USER_AUDIENCE = 1, 2
ENTRY_NORMAL, ENTRY_PRIVATE = 1, 2
PRIVATE_NONE, PRIVATE_ENABLE, PRIVATE_DISABLE = 0, 1, 2
PRIVATE_FRIEND, PRIVATE_CIRCLE = 1, 2
RESTRICT_LOWER, RESTRICT_UPPER = 1, 2

NPC_TYPE_ENTRANT = 11

STATUS_WAITING = 0


_lock = RLock()
_conns: dict[str, sqlite3.Connection] = {}


def _db_path():
    return region.data_dir() / "room_match.sqlite3"


@contextmanager
def _db():
    with _lock:
        key = region.CURRENT_REGION.get()
        conn = _conns.get(key)
        if conn is None:
            conn = sqlite3.connect(_db_path(), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS room (
                room_id INTEGER PRIMARY KEY, host TEXT NOT NULL,
                start_ts REAL NOT NULL, data TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS saved_result (
                viewer_id TEXT NOT NULL, saved_room_id INTEGER NOT NULL,
                register_id INTEGER NOT NULL, save_ts REAL NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (viewer_id, saved_room_id, register_id))""")
            _conns[key] = conn
        with conn:
            yield conn


def _load_room(conn, room_id) -> dict | None:
    row = conn.execute("SELECT data FROM room WHERE room_id=?",
                       (int(room_id or 0),)).fetchone()
    return json.loads(row["data"]) if row else None


def _save_room(conn, room: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO room (room_id, host, start_ts, data) "
                 "VALUES (?,?,?,?)",
                 (room["room_id"], room["host"], room["start_ts"], json.dumps(room)))


def _drop_room(conn, room_id) -> None:
    conn.execute("DELETE FROM room WHERE room_id=?", (int(room_id),))


def _all_rooms(conn) -> list[dict]:
    conn.execute("DELETE FROM room WHERE start_ts < ?", (time.time() - ROOM_LINGER,))
    return [json.loads(r["data"]) for r in
            conn.execute("SELECT data FROM room ORDER BY start_ts")]


def _new_room_id(conn) -> int:
    while True:
        rid = random.randint(10_000_000, 99_999_999)
        if not conn.execute("SELECT 1 FROM room WHERE room_id=?", (rid,)).fetchone():
            return rid


def _shift() -> float:
    return patch._servertime() - time.time()


def _stamp(real_ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(real_ts + _shift()))


def _started(room: dict) -> bool:
    return time.time() >= room["start_ts"]


def _ok(data: dict | None = None) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": data if data is not None else {}}


def _user_card(vid) -> dict:
    return directory.cards_for([str(vid)]).get(str(vid)) or {}


def _user_head(vid) -> dict:
    card = _user_card(vid)
    honor_id = card.get("honor_id") or 0
    return {"viewer_id": social._wire_id(vid),
            "name": card.get("name") or f"Trainer{str(vid)[-4:]}",
            "leader_chara_id": card.get("leader_chara_id") or 0,
            "leader_chara_dress_id": card.get("leader_chara_dress_id") or 0,
            "honor_id": honor_id, "honor_data": {"honor_id": honor_id}}


def _room_user(room: dict, vid, join_type: int) -> dict:
    head = _user_head(vid)
    return {"viewer_id": head["viewer_id"], "room_id": room["room_id"],
            "name": head["name"], "leader_chara_id": head["leader_chara_id"],
            "leader_chara_dress_id": head["leader_chara_dress_id"],
            "honor_id": head["honor_id"], "honor_data": head["honor_data"],
            "join_type": join_type}


def _entrant_count(room: dict, private: bool | None = None) -> int:
    return sum(1 for m in room["members"].values() for c in m["charas"]
               if private is None or (c["join_type"] == ENTRY_PRIVATE) == private)


def _qualifies_private(room: dict, vid) -> bool:
    kind = room["private_entry_type"]
    if kind == PRIVATE_FRIEND:
        return social.friend_data(room["host"], vid)["state"] == social.STATE_MUTUAL
    if kind == PRIVATE_CIRCLE:
        mine, theirs = social.circle_of(str(vid)), social.circle_of(room["host"])
        return bool(mine and theirs and mine["circle_id"] == theirs["circle_id"])
    return False


def _private_status(room: dict, vid) -> int:
    if _join_type(room, vid) or room["host"] == str(vid):
        return PRIVATE_NONE
    if not room["private_entry_type"]:
        return PRIVATE_DISABLE
    open_private = _entrant_count(room, private=True) < room["private_entry_num"]
    return PRIVATE_ENABLE if open_private and _qualifies_private(room, vid) \
        else PRIVATE_DISABLE


def _room_info(room: dict, vid) -> dict:
    raced = room.get("race")
    return {
        "room_id": room["room_id"],
        "host_viewer_id": social._wire_id(room["host"]),
        "race_instance_id": room["race_instance_id"],
        "room_name": room["room_name"], "message": room["message"],
        "season": room["season"], "weather": room["weather"],
        "ground_condition": room["ground_condition"], "motivation": room["motivation"],
        "entry_num": room["entry_num"],
        "current_entry_num": _entrant_count(room),
        "private_entry_type": room["private_entry_type"],
        "private_entry_num": room["private_entry_num"],
        "private_current_entry_num": _entrant_count(room, private=True),
        "is_allow_watching": room["is_allow_watching"],
        "trained_chara_restriction": room["trained_chara_restriction"],
        "restriction_type": room["restriction_type"],
        "start_time": _stamp(room["start_ts"]),
        "random_seed": room["random_seed"],
        "status": STATUS_WAITING,
        "is_allow_display": room["is_allow_display"],
        "simulate_app_version": raced["app_version"] if raced else None,
        "simulate_resource_version": raced["resource_version"] if raced else None,
        "register_time": _stamp(room["register_ts"]),
        "restrict_chara_info_array": copy.deepcopy(room["restrict_chara_info_array"]),
        "private_entry_status": _private_status(room, vid),
    }


def _user_details(room: dict, vid) -> list[dict]:
    out = []
    for member_vid, m in room["members"].items():
        head = _user_head(member_vid)
        out.append({
            **head,
            "user_friend_state": social.friend_data(vid, member_vid)["state"]
            if str(member_vid) != str(vid) else 0,
            "trained_chara_array": [copy.deepcopy(c["chara"]) for c in m["charas"]],
            "trained_chara_join_type_array": [
                {"trained_chara_id": c["chara"]["trained_chara_id"],
                 "join_type": c["join_type"]} for c in m["charas"]],
            "join_type": USER_ENTRY,
            "register_time": _stamp(m["register_ts"]),
        })
    return out


def _lobby(room: dict, vid, polling: bool = True) -> dict:
    data = {"room_info": _room_info(room, vid),
            "room_user_detail_array": _user_details(room, vid)}
    if polling:
        data["polling_interval"] = POLLING_INTERVAL
    return data


def _join_type(room: dict, vid) -> int:
    vid = str(vid)
    if vid in room["members"]:
        return USER_ENTRY
    if vid in room["audience"]:
        return USER_AUDIENCE
    return 0


def _unchecked(room: dict, vid) -> bool:
    return _join_type(room, vid) and str(vid) not in room["checked"]


def _my_rooms(conn, vid) -> list[dict]:
    return [r for r in _all_rooms(conn) if _unchecked(r, vid)]


def _entry_charas(vid, entries, room: dict | None) -> list[dict] | int:
    entries = [e for e in (entries or []) if e.get("trained_chara_id")]
    if not entries or len(entries) > MAX_CHARAS_PER_USER:
        return ResultCode.PARAM_ERROR
    roster = {c.get("trained_chara_id"): c
              for c in trained_chara._get_or_seed_roster(vid) or []}
    out = []
    for i, e in enumerate(entries):
        base = roster.get(e["trained_chara_id"])
        if base is None:
            log.warning("room_match: %s has no trained chara %s", vid, e["trained_chara_id"])
            return ResultCode.PARAM_ERROR
        if room is not None and not _allowed(room, base):
            return ResultCode.PARAM_ERROR
        chara = copy.deepcopy(base)
        chara["viewer_id"] = social._wire_id(vid)
        if e.get("running_style"):
            chara["running_style"] = e["running_style"]
        chara["member_id"] = e.get("member_id") or i + 1
        out.append(chara)
    return out


def _allowed(room: dict, chara: dict) -> bool:
    limit, kind = room["trained_chara_restriction"], room["restriction_type"]
    rank = chara.get("rank") or 1
    if limit and kind == RESTRICT_LOWER and rank < limit:
        return False
    if limit and kind == RESTRICT_UPPER and rank > limit:
        return False
    allow = room["restrict_chara_info_array"]
    if allow:
        chara_id = (chara.get("card_id") or 0) // 100
        dress = chara.get("race_cloth_id") or 0
        return any(a.get("chara_id") == chara_id
                   and (not a.get("dress_id_array") or dress in a["dress_id_array"])
                   for a in allow)
    return True


def _course_entry_num(race_instance_id) -> int | None:
    info = race_simulator.get_course_for_race_instance(int(race_instance_id or 0))
    return info[2] if info else None


def _create(payload: dict, simple: bool) -> dict:
    vid = str(payload.get("viewer_id"))
    race_instance_id = int(payload.get("race_instance_id") or 0)
    course_max = _course_entry_num(race_instance_id)
    if not course_max:
        return refuse(ResultCode.PARAM_ERROR)

    if simple:
        settings = {"entry_num": course_max, "start_time_type": START_TIME_TYPE_SIMPLE,
                    "is_allow_watching": 1, "private_entry_type": 0,
                    "private_entry_num": 0, "season": 5, "weather": 0,
                    "ground_condition": 0, "motivation": 0, "restriction": 0,
                    "restriction_type": 0, "restrict_chara_info_array": [],
                    "is_allow_display_flag": 0}
    else:
        settings = payload
    entry_num = max(1, min(int(settings.get("entry_num") or course_max), course_max))
    private_num = int(settings.get("private_entry_num") or 0) \
        if settings.get("private_entry_type") else 0

    now = time.time()
    room = {
        "host": vid, "race_instance_id": race_instance_id,
        "room_name": str(payload.get("room_name") or ""),
        "message": str(payload.get("message") or ""),
        "season": int(settings.get("season") or 0),
        "weather": int(settings.get("weather") or 0),
        "ground_condition": int(settings.get("ground_condition") or 0),
        "motivation": int(settings.get("motivation") or 0),
        "entry_num": entry_num,
        "private_entry_type": int(settings.get("private_entry_type") or 0),
        "private_entry_num": max(0, min(private_num, entry_num)),
        "is_allow_watching": 1 if settings.get("is_allow_watching") else 0,
        "trained_chara_restriction": int(settings.get("restriction") or 0),
        "restriction_type": int(settings.get("restriction_type") or 0),
        "restrict_chara_info_array": list(settings.get("restrict_chara_info_array") or []),
        "is_allow_display": 1 if settings.get("is_allow_display_flag") else 0,
        "start_time_type": int(settings.get("start_time_type") or START_TIME_TYPE_SIMPLE),
        "random_seed": random.randint(1, 2**31 - 1),
        "register_ts": now, "members": {}, "audience": {}, "checked": [],
        "race": None,
    }
    room["start_ts"] = now + _START_DELAY.get(room["start_time_type"], _START_DELAY[1])

    charas = _entry_charas(vid, payload.get("entry_chara_array"), room)
    if isinstance(charas, int):
        return refuse(charas)
    if len(charas) > entry_num:
        return refuse(ResultCode.ROOM_MATCH_ENTRY_NUM_OVER_ERROR)
    room["members"][vid] = {"register_ts": now, "charas": [
        {"chara": c, "join_type": ENTRY_NORMAL} for c in charas]}

    with _db() as conn:
        room["room_id"] = _new_room_id(conn)
        _save_room(conn, room)
    log.info("room_match: %s opened room %s (race %s, %s gates, starts in %ds)",
             vid, room["room_id"], race_instance_id, entry_num, room["start_ts"] - now)
    return _ok(_lobby(room, vid))


@registry.endpoint("room_match/create_room")
def handle_create_room(payload: dict) -> dict:
    return _create(payload, simple=False)


@registry.endpoint("room_match/create_room_simple")
def handle_create_room_simple(payload: dict) -> dict:
    return _create(payload, simple=True)


@registry.endpoint("room_match/index")
def handle_index(payload: dict) -> dict:
    with _db() as conn:
        return _ok({"join_watch_room_num": len(_my_rooms(conn, payload.get("viewer_id")))})


@registry.endpoint("room_match/get_entry_room_list")
def handle_get_entry_room_list(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        rooms = _my_rooms(conn, vid)
    users = []
    for room in rooms:
        users.append(_room_user(room, vid, _join_type(room, vid)))
        if room["host"] != vid:
            users.append(_room_user(room, room["host"], USER_ENTRY))
    return _ok({"room_info_array": [_room_info(r, vid) for r in rooms],
                "room_user_array": users})


@registry.endpoint("room_match/room_list")
def handle_room_list(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        rooms = _all_rooms(conn)
        mine = len([r for r in rooms if _unchecked(r, vid)])
    pool = [r for r in rooms if r["is_allow_display"] and not _started(r)
            and r["host"] != vid and not _join_type(r, vid)
            and _entrant_count(r) < r["entry_num"]]
    random.shuffle(pool)
    pool = pool[:ROOM_LIST_SIZE]
    return _ok({"room_info_array": [_room_info(r, vid) for r in pool],
                "room_user_array": [_room_user(r, r["host"], USER_ENTRY) for r in pool],
                "join_watch_room_num": mine})


@registry.endpoint("room_match/room_search")
def handle_room_search(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
        mine = len(_my_rooms(conn, vid))
    if room is None:
        return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    return _ok({"room_info": _room_info(room, vid),
                "room_user_array": [_room_user(room, room["host"], USER_ENTRY)],
                "join_watch_room_num": mine})


@registry.endpoint("room_match/room_detail")
def handle_room_detail(payload: dict) -> dict:
    vid = payload.get("viewer_id")
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
    if room is None:
        return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    return _ok(_lobby(room, vid, polling=False))


@registry.endpoint("room_match/get_friend_list")
def handle_get_friend_list(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    friends = social.friend_list(vid)
    ids = [str(f["friend_viewer_id"]) for f in friends]
    cards = directory.cards_for(ids)
    return _ok({"summary_user_info_array": [
        directory.summary(f_id, cards[f_id], f["state"])
        for f_id, f in zip(ids, friends) if f_id in cards]})


def _lobby_endpoint(payload: dict) -> dict:
    vid = payload.get("viewer_id")
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
    if room is None:
        return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    return _ok(_lobby(room, vid))


@registry.endpoint("room_match/enter_waiting_room")
def handle_enter_waiting_room(payload: dict) -> dict:
    return _lobby_endpoint(payload)


@registry.endpoint("room_match/polling")
def handle_polling(payload: dict) -> dict:
    return _lobby_endpoint(payload)


@registry.endpoint("room_match/entry_room")
def handle_entry_room(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
        if room is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        if _started(room):
            return refuse(ResultCode.ROOM_MATCH_ENTRY_OVERTIME_ERROR)
        charas = _entry_charas(vid, payload.get("entry_chara_array"), room)
        if isinstance(charas, int):
            return refuse(charas)
        room["members"].pop(vid, None)
        normal_cap = room["entry_num"] - room["private_entry_num"]
        normal_free = normal_cap - _entrant_count(room, private=False)
        private_free = (room["private_entry_num"] - _entrant_count(room, private=True)
                        if _qualifies_private(room, vid) else 0)
        if len(charas) > max(0, normal_free) + max(0, private_free):
            return refuse(ResultCode.ROOM_MATCH_ENTRY_NUM_OVER_ERROR)
        placed = []
        for c in charas:
            kind = ENTRY_NORMAL if normal_free > 0 else ENTRY_PRIVATE
            normal_free -= kind == ENTRY_NORMAL
            placed.append({"chara": c, "join_type": kind})
        room["members"][vid] = {"register_ts": time.time(), "charas": placed}
        room["audience"].pop(vid, None)
        _save_room(conn, room)
    return _ok({"room_info": _room_info(room, vid),
                "room_user_detail_array": [_room_user(room, v, USER_ENTRY)
                                           for v in room["members"]]})


@registry.endpoint("room_match/leave_room")
def handle_leave_room(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
        if room is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        if room["host"] == vid:
            return refuse(ResultCode.PARAM_ERROR)
        if _started(room):
            return refuse(ResultCode.ROOM_MATCH_CANCLE_OVERTIME_ERROR)
        room["members"].pop(vid, None)
        _save_room(conn, room)
    return _ok()


@registry.endpoint("room_match/watch_room")
def handle_watch_room(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
        if room is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        if not room["is_allow_watching"] or _started(room):
            return refuse(ResultCode.ROOM_MATCH_WATCH_RACE_START_TIME_ERROR)
        if vid not in room["members"] and vid not in room["audience"]:
            if len(room["audience"]) >= AUDIENCE_LIMIT:
                return refuse(ResultCode.ROOM_MATCH_AUDIENCE_NUM_OVER)
            room["audience"][vid] = {"register_ts": time.time()}
            _save_room(conn, room)
    return _ok({"room_info": _room_info(room, vid)})


@registry.endpoint("room_match/leave_watching")
def handle_leave_watching(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room = _load_room(conn, payload.get("room_id"))
        if room is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        room["audience"].pop(vid, None)
        _save_room(conn, room)
    return _ok()


def _host_room(conn, payload: dict, overtime_code: int):
    room = _load_room(conn, payload.get("room_id"))
    if room is None:
        return None, refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    if room["host"] != str(payload.get("viewer_id")):
        return None, refuse(ResultCode.PARAM_ERROR)
    if _started(room):
        return None, refuse(overtime_code)
    return room, None


@registry.endpoint("room_match/edit_room")
def handle_edit_room(payload: dict) -> dict:
    with _db() as conn:
        room, err = _host_room(conn, payload, ResultCode.ROOM_MATCH_EDIT_OVERTIME_ERROR)
        if err:
            return err
        room["room_name"] = str(payload.get("room_name") or "")
        room["message"] = str(payload.get("message") or "")
        _save_room(conn, room)
    return _ok({"room_info": _room_info(room, payload.get("viewer_id"))})


@registry.endpoint("room_match/change_allow_display")
def handle_change_allow_display(payload: dict) -> dict:
    with _db() as conn:
        room, err = _host_room(conn, payload, ResultCode.ROOM_MATCH_EDIT_OVERTIME_ERROR)
        if err:
            return err
        room["is_allow_display"] = 1 if payload.get("is_allow_display") else 0
        _save_room(conn, room)
    return _ok({"room_info": _room_info(room, payload.get("viewer_id"))})


@registry.endpoint("room_match/force_race_start")
def handle_force_race_start(payload: dict) -> dict:
    with _db() as conn:
        room, err = _host_room(conn, payload,
                               ResultCode.ROOM_MATCH_FORCE_START_OVERTIME_ERROR)
        if err:
            return err
        room["start_ts"] = time.time()
        _save_room(conn, room)
    return _ok({"room_info": _room_info(room, payload.get("viewer_id"))})


@registry.endpoint("room_match/destroy_room")
def handle_destroy_room(payload: dict) -> dict:
    with _db() as conn:
        room, err = _host_room(conn, payload, ResultCode.ROOM_MATCH_DELETE_ROOM_ERROR)
        if err:
            return err
        _drop_room(conn, room["room_id"])
    log.info("room_match: room %s destroyed by its host", room["room_id"])
    return _ok()


def _resolve_conditions(room: dict, rng: random.Random) -> tuple[int, int, int]:
    return practice_race.resolve_conditions(room, rng)


def _run_race(room: dict) -> dict | None:
    rng = random.Random(room["random_seed"])
    season, weather, ground = _resolve_conditions(room, rng)

    entrants, trained = [], []
    for vid, m in room["members"].items():
        name = _user_head(vid)["name"]
        for c in m["charas"]:
            horse = copy.deepcopy(c["chara"])
            trained.append(copy.deepcopy(c["chara"]))
            horse.update({"trainer_name": name, "npc_type": NPC_TYPE_ENTRANT})
            if room["motivation"]:
                horse["motivation"] = room["motivation"]
            else:
                horse["motivation"] = rng.randint(1, 5)
            entrants.append(horse)
    if not entrants:
        return None

    course_max = _course_entry_num(room["race_instance_id"]) or room["entry_num"]
    field = min(room["entry_num"], course_max)
    mobs = race_mobs.room_mobs(entrants, max(0, field - len(entrants)),
                               room["race_instance_id"], rng, room["motivation"])
    runners = max(len(entrants), min(field, len(entrants) + len(mobs)))
    result = race_simulator.simulate_race(
        entrants[0], mobs, room["race_instance_id"],
        ground_condition=ground, weather=weather, season=season,
        seed=room["random_seed"], entry_num=field,
        gate_assignment=rng.sample(range(runners), runners),
        extra_player_charas=entrants[1:], race_context={"race_type": 8})
    if result is None:
        return None
    rows, _ = practice_race.race_horse_rows(result)
    return {"race_scenario": result["race_scenario"],
            "race_horse_data_array": rows,
            "trained_chara_array": trained,
            "season": season, "weather": weather, "ground_condition": ground,
            "app_version": str(config.get("app_version", "1.35.2")),
            "resource_version": str(config.get("resource_version", "10007900"))}


def _raced_room(conn, room_id) -> tuple[dict | None, dict | None]:
    room = _load_room(conn, room_id)
    if room is None:
        return None, refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    if not _started(room):
        return None, refuse(ResultCode.ROOM_MATCH_WATCH_RACE_START_TIME_ERROR)
    if room.get("race") is None:
        race = _run_race(room)
        if race is None:
            log.error("room_match: room %s could not be simulated", room["room_id"])
            return None, refuse(ResultCode.PARAM_ERROR)
        room["race"] = race
        _save_room(conn, room)
    return room, None


@registry.endpoint("room_match/race_start")
def handle_race_start(payload: dict) -> dict:
    with _db() as conn:
        room, err = _raced_room(conn, payload.get("room_id"))
    if err:
        return err
    race = room["race"]
    return _ok({"race_scenario": race["race_scenario"],
                "race_horse_data_array": copy.deepcopy(race["race_horse_data_array"]),
                "trained_chara_array": copy.deepcopy(race["trained_chara_array"]),
                "random_seed": room["random_seed"],
                "season": race["season"], "weather": race["weather"],
                "ground_condition": race["ground_condition"],
                "start_time_type": room["start_time_type"]})


def _saved_info(room: dict, own_join_type: int, register_id: int,
                favorite: int = 0) -> dict:
    info = _room_info(room, room["host"])
    out = {"saved_room_id": room["room_id"], "room_id": room["room_id"],
           "register_id": register_id}
    for k in ("host_viewer_id", "room_name", "message", "season", "weather",
              "ground_condition", "motivation", "entry_num", "current_entry_num",
              "private_entry_type", "private_entry_num", "private_current_entry_num",
              "is_allow_watching", "trained_chara_restriction", "restriction_type",
              "start_time", "random_seed", "race_instance_id"):
        out[k] = info[k]
    out.update({"favorite_flag": favorite, "own_join_type": own_join_type,
                "simulate_app_version": info["simulate_app_version"],
                "simulate_resource_version": info["simulate_resource_version"],
                "expiration_time": _stamp(room["start_ts"] + SAVED_RESULT_KEEP),
                "restrict_chara_info_array": info["restrict_chara_info_array"]})
    return out


@registry.endpoint("room_match/race_end_result")
def handle_race_end_result(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        room, err = _raced_room(conn, payload.get("room_id"))
        if err:
            return err
        join = _join_type(room, vid)
        if not join:
            return refuse(ResultCode.PARAM_ERROR)
        if int(payload.get("is_save_result") or 0):
            overwrite = (int(payload.get("saved_room_id") or 0),
                         int(payload.get("register_id") or 0))
            if all(overwrite):
                conn.execute("DELETE FROM saved_result WHERE viewer_id=? AND "
                             "saved_room_id=? AND register_id=?", (vid, *overwrite))
            _save_result(conn, vid, room, join)
        if vid not in room["checked"]:
            room["checked"].append(vid)
        everyone = set(room["members"]) | set(room["audience"])
        if everyone <= set(room["checked"]):
            _drop_room(conn, room["room_id"])
        else:
            _save_room(conn, room)
    return _ok()


def _save_result(conn, vid: str, room: dict, join: int) -> None:
    register_id = int(room["start_ts"] + _shift())
    entry = {"room": room, "own_join_type": join, "favorite_flag": 0}
    conn.execute("INSERT OR REPLACE INTO saved_result VALUES (?,?,?,?,?)",
                 (vid, room["room_id"], register_id, time.time(), json.dumps(entry)))
    rows = conn.execute("SELECT saved_room_id, register_id, data FROM saved_result "
                        "WHERE viewer_id=? ORDER BY save_ts", (vid,)).fetchall()
    excess = len(rows) - SAVED_RESULT_LIMIT
    for r in rows:
        if excess <= 0:
            break
        if json.loads(r["data"]).get("favorite_flag"):
            continue
        conn.execute("DELETE FROM saved_result WHERE viewer_id=? AND saved_room_id=? "
                     "AND register_id=?", (vid, r["saved_room_id"], r["register_id"]))
        excess -= 1


def _saved_rows(conn, vid: str):
    return conn.execute("SELECT saved_room_id, register_id, data FROM saved_result "
                        "WHERE viewer_id=? ORDER BY save_ts", (vid,)).fetchall()


def _saved_one(conn, vid: str, payload: dict):
    return conn.execute(
        "SELECT saved_room_id, register_id, data FROM saved_result WHERE viewer_id=? "
        "AND saved_room_id=? AND register_id=?",
        (vid, int(payload.get("saved_room_id") or 0),
         int(payload.get("register_id") or 0))).fetchone()


@registry.endpoint("room_match/get_saved_race_result_list")
def handle_get_saved_race_result_list(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        rows = _saved_rows(conn, vid)
    infos, users = [], []
    for r in rows:
        entry = json.loads(r["data"])
        infos.append(_saved_info(entry["room"], entry["own_join_type"],
                                 r["register_id"], entry["favorite_flag"]))
        users.append(_room_user(entry["room"], vid, entry["own_join_type"]))
    return _ok({"race_result_array": infos, "room_user_array": users})


@registry.endpoint("room_match/get_saved_race_result")
def handle_get_saved_race_result(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        row = _saved_one(conn, vid, payload)
    if row is None:
        return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
    entry = json.loads(row["data"])
    room, race = entry["room"], entry["room"]["race"]
    return _ok({"race_result": _saved_info(room, entry["own_join_type"],
                                           row["register_id"], entry["favorite_flag"]),
                "race_scenario": race["race_scenario"],
                "race_horse_data_array": copy.deepcopy(race["race_horse_data_array"]),
                "trained_chara_array": copy.deepcopy(race["trained_chara_array"]),
                "season": race["season"], "weather": race["weather"],
                "ground_condition": race["ground_condition"]})


@registry.endpoint("room_match/delete_race_result")
def handle_delete_race_result(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        if _saved_one(conn, vid, payload) is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        conn.execute("DELETE FROM saved_result WHERE viewer_id=? AND saved_room_id=? "
                     "AND register_id=?", (vid, int(payload.get("saved_room_id") or 0),
                                           int(payload.get("register_id") or 0)))
    return _ok()


@registry.endpoint("room_match/change_lock_race_result")
def handle_change_lock_race_result(payload: dict) -> dict:
    vid = str(payload.get("viewer_id"))
    with _db() as conn:
        row = _saved_one(conn, vid, payload)
        if row is None:
            return refuse(ResultCode.ROOM_MATCH_NO_ROOM_ERROR)
        entry = json.loads(row["data"])
        entry["favorite_flag"] = 1 if int(payload.get("lock_flag") or 0) else 0
        conn.execute("UPDATE saved_result SET data=? WHERE viewer_id=? AND "
                     "saved_room_id=? AND register_id=?",
                     (json.dumps(entry), vid, row["saved_room_id"], row["register_id"]))
    return _ok({"saved_room_id": row["saved_room_id"], "register_id": row["register_id"],
                "is_locked": entry["favorite_flag"]})


PRESET_KEY = "room_match_presets"


def _presets(full_state: dict) -> dict:
    st = full_state.get(PRESET_KEY)
    if not isinstance(st, dict):
        st = full_state[PRESET_KEY] = {"next_preset_id": 1, "presets": []}
    practice_race_lobby.seed_presets(st, "room_match")
    return st


def _preset_wire(p: dict) -> dict:
    return {"preset_id": p["preset_id"], "name": p.get("name") or "",
            "preset_chara_array": p.get("preset_chara_array") or []}


@registry.endpoint("room_match/get_preset_array")
def handle_get_preset_array(payload: dict) -> dict:
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    return _ok({"preset_info_array": [_preset_wire(p)
                                      for p in _presets(full_state)["presets"]]})


@registry.endpoint("room_match/save_preset")
def handle_save_preset(payload: dict) -> dict:
    vid = payload.get("viewer_id")
    full_state = state_store.get_state(vid) or {}
    st = _presets(full_state)
    for incoming in payload.get("preset_info_array") or []:
        pid = int(incoming.get("preset_id") or 0)
        preset = next((p for p in st["presets"] if p["preset_id"] == pid), None) \
            if pid else None
        if preset is None:
            preset = {"preset_id": st["next_preset_id"], "name": ""}
            st["next_preset_id"] += 1
            st["presets"].append(preset)
        preset["preset_chara_array"] = incoming.get("preset_chara_array") or []
        if incoming.get("name"):
            preset["name"] = str(incoming["name"])[:32]
    state_store.save_state(vid, full_state)
    return _ok({"preset_info_array": [_preset_wire(p) for p in st["presets"]]})


@registry.endpoint("room_match/change_preset_name")
def handle_change_preset_name(payload: dict) -> dict:
    vid = payload.get("viewer_id")
    full_state = state_store.get_state(vid) or {}
    st = _presets(full_state)
    pid = int(payload.get("preset_id") or 0)
    preset = next((p for p in st["presets"] if p["preset_id"] == pid), None)
    if preset is None:
        return refuse(ResultCode.PARAM_ERROR)
    preset["name"] = str(payload.get("name") or "")[:32]
    state_store.save_state(vid, full_state)
    return _ok({"preset_info": _preset_wire(preset)})


def entered_chara_ids(viewer_id) -> list[int]:
    vid = str(viewer_id)
    try:
        with _db() as conn:
            rooms = _all_rooms(conn)
    except Exception:
        log.exception("room_match: could not read rooms for %s", vid)
        return []
    return [c["chara"]["trained_chara_id"] for r in rooms if not _started(r)
            for c in (r["members"].get(vid) or {}).get("charas", [])]
