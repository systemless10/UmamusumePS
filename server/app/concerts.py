
from __future__ import annotations

import functools
import json
import logging
import os
import time

from . import master_data, region

log = logging.getLogger("uma-server")

MUSIC_LIST_KEY = "music_list_state"
DEFAULTS_MAILED_KEY = "concerts_default_mailed"
_MUSIC_REWARD_TYPE = 80
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "concert_unlocks.json")


@functools.lru_cache(maxsize=1)
def table() -> tuple:
    try:
        with open(_DATA, encoding="utf-8") as fh:
            return tuple(json.load(fh).get("songs") or ())
    except Exception:
        log.exception("concert unlock table unreadable at %s", _DATA)
        return ()


def _songs(*kinds) -> list:
    return [s for s in table() if s["kind"] in kinds]


@functools.lru_cache(maxsize=None)
def _release(music_id: int):
    row = master_data.query_one(
        "SELECT start_date FROM live_data WHERE music_id=?", (music_id,))
    return int(row["start_date"]) if row else None


def _available(music_id: int) -> bool:
    start = _release(int(music_id))
    return start is not None and start <= time.time()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def owned(full_state: dict) -> set:
    return {m.get("music_id") for m in (full_state.get(MUSIC_LIST_KEY) or ())
            if isinstance(m, dict)}


def grant(full_state: dict, music_id: int):
    music_id = int(music_id)
    if music_id in owned(full_state) or not _available(music_id):
        return None
    entry = {"music_id": music_id, "acquisition_time": _now()}
    full_state.setdefault(MUSIC_LIST_KEY, []).append(entry)
    log.info("concerts: unlocked %s (%s)", music_id, _title(music_id))
    return dict(entry)


def _grant_first(full_state: dict, music_ids) -> dict | None:
    first = None
    for mid in music_ids:
        got = grant(full_state, mid)
        if got and first is None:
            first = got
    return first


@functools.lru_cache(maxsize=None)
def _program_race(program_id: int):
    row = master_data.query_one(
        "SELECT p.race_instance_id, r.grade FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE p.id=?", (program_id,))
    return (int(row["race_instance_id"]), int(row["grade"])) if row else (None, None)


def race_won_songs(chara_info: dict, program_id, result_rank: int) -> list:
    if int(result_rank or 0) != 1 or not program_id:
        return []
    instance, grade = _program_race(int(program_id))
    if instance is None:
        return []
    scenario_id = int((chara_info or {}).get("scenario_id") or 0)
    out = []
    for song in _songs("win_race", "win_grade_at_most"):
        p = song.get("params") or {}
        if p.get("scenario_id") and int(p["scenario_id"]) != scenario_id:
            continue
        if song["kind"] == "win_race":
            if instance in (p.get("race_instance_ids") or ()):
                out.append(song["music_id"])
        elif grade >= int(p.get("grade") or 0):
            out.append(song["music_id"])
    return out


def on_race_end(full_state: dict, chara_info: dict, race_ctx: dict,
                result_rank: int):
    ids = race_won_songs(chara_info, race_ctx.get("program_id"), result_rank)
    if int((chara_info or {}).get("scenario_id") or 0) == 4 and _won_climax(full_state):
        ids += [s["music_id"] for s in _songs("ts_climax")]
    return _grant_first(full_state, ids)


def _won_climax(full_state: dict) -> bool:
    try:
        from .scenarios.trackblazer import impl as tb
    except Exception:
        return False
    climax = (tb.state(full_state).get("climax") or {})
    return len(climax.get("results") or ()) >= 3 and tb.climax_ranking(full_state) == 1


def on_scenario(full_state: dict, kind: str):
    return _grant_first(full_state, [s["music_id"] for s in _songs(kind)])


@functools.lru_cache(maxsize=1)
def _story_triggers() -> dict:
    return {int(sid): s["music_id"] for s in _songs("story_seen")
            for sid in (s.get("params") or {}).get("story_ids") or ()}


def attach_story_songs(viewer_id, endpoint: str, response: dict) -> dict:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or data.get("add_music"):
        return response
    events = data.get("unchecked_event_array")
    if not events or not isinstance(events, list):
        return response
    triggers = _story_triggers()
    hits = [triggers[int(e.get("story_id") or 0)] for e in events
            if isinstance(e, dict) and int(e.get("story_id") or 0) in triggers]
    if not hits:
        return response
    from . import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    got = _grant_first(full_state, hits)
    if got:
        state_store.save_state(viewer_id, full_state)
        data["add_music"] = got
    return response


def _story_songs(kind: str, episode_id) -> list:
    reg = region.CURRENT_REGION.get()
    return [s["music_id"] for s in _songs(kind)
            if ((s.get("params") or {}).get("episode_ids") or {}).get(reg) == episode_id]


def on_story_clear(full_state: dict, kind: str, episode_id) -> list:
    out = []
    for mid in _story_songs(kind, episode_id):
        got = grant(full_state, mid)
        if got:
            out.append(got)
    return out


def on_login(full_state: dict) -> bool:
    changed = False
    mailed = full_state.setdefault(DEFAULTS_MAILED_KEY, [])
    have = owned(full_state)
    from .handlers import presents
    for song in _songs("default"):
        mid = song["music_id"]
        if mid in mailed or not _available(mid):
            continue
        mailed.append(mid)
        changed = True
        if mid not in have:
            presents.send(full_state, _MUSIC_REWARD_TYPE, mid, 1)

    from .handlers import stories
    st = full_state.get(stories.STORY_STATE_KEY) or {}
    cleared = {"main_story": st.get("main_cleared"),
               "extra_story": st.get("extra_cleared"),
               "story_event": st.get("story_event_cleared")}
    for kind, episodes in cleared.items():
        for episode_id in episodes or ():
            if on_story_clear(full_state, kind, episode_id):
                changed = True
    return changed


def _title(music_id: int):
    row = master_data.query_one(
        "SELECT text FROM text_data WHERE category=16 AND [index]=?", (music_id,))
    return row["text"] if row else None
