
from __future__ import annotations

import functools
import time

from . import master_data
from . import state as state_store

STATE_KEY = "event_gallery"
_COMMON_CATEGORIES = (0, 1)


@master_data.context_cache(maxsize=4)
def _stories() -> tuple[dict, dict]:
    rows, by_short = {}, {}
    for r in master_data.query(
            "SELECT story_id, short_story_id, card_chara_id, support_chara_id, "
            "support_card_id, event_category FROM single_mode_story_data"):
        rows[r["story_id"]] = (r["card_chara_id"], r["support_chara_id"],
                               r["support_card_id"], r["event_category"])
        if r["short_story_id"]:
            by_short.setdefault(r["short_story_id"], []).append(r["story_id"])
    return rows, by_short


@functools.lru_cache(maxsize=1)
def _support_charas() -> dict:
    return {r["id"]: r["chara_id"]
            for r in master_data.query("SELECT id, chara_id FROM support_card_data")}


@functools.lru_cache(maxsize=1)
def _card_charas() -> dict:
    return {r["id"]: r["chara_id"]
            for r in master_data.query("SELECT id, chara_id FROM card_data")}


def targets(story_id) -> list:
    rows, by_short = _stories()
    if story_id in rows:
        return [story_id]
    return list(by_short.get(story_id) or ())


def chara_for(story_id, trainee_chara_id: int) -> int:
    card_chara, support_chara, support_card, category = _stories()[0][story_id]
    if card_chara:
        return card_chara
    if support_chara:
        return support_chara
    if support_card:
        return _support_charas().get(support_card) or 0
    return 0 if category in _COMMON_CATEGORIES else (trainee_chara_id or 0)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _store(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = full_state[STATE_KEY] = {}
    return st


def record(full_state: dict, story_id, trainee_chara_id: int = 0) -> list:
    st = _store(full_state)
    added = []
    for sid in targets(story_id):
        if str(sid) in st:
            continue
        chara = chara_for(sid, trainee_chara_id)
        stamp = _now()
        st[str(sid)] = [chara, stamp, 1]
        added.append({"chara_id": chara, "data_id": sid, "create_time": stamp,
                      "new_flag": 1})
    return added


def record_served(viewer_id, response: dict) -> None:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    events = data.get("unchecked_event_array")
    if not isinstance(events, list) or not events:
        return
    ids = [e.get("story_id") for e in events if isinstance(e, dict) and e.get("story_id")]
    if not any(targets(s) for s in ids):
        return
    card_id = ((data.get("chara_info") or {}).get("card_id")) or 0
    trainee = _card_charas().get(card_id) or 0
    full_state = state_store.get_state(viewer_id) or {}
    added = []
    for sid in ids:
        added += record(full_state, sid, trainee)
    if added:
        state_store.save_state(viewer_id, full_state)


def wire_array(full_state: dict) -> list:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        return []
    rows = [{"chara_id": v[0], "data_id": int(k), "create_time": v[1], "new_flag": v[2]}
            for k, v in st.items()]
    rows.sort(key=lambda r: (r["chara_id"], r["data_id"]))
    return rows


def mark_viewed(full_state: dict) -> bool:
    st = full_state.get(STATE_KEY)
    changed = False
    if isinstance(st, dict):
        for v in st.values():
            if v[2]:
                v[2] = 0
                changed = True
    return changed
