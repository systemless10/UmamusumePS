
from __future__ import annotations

import functools

from .. import master_data


@functools.lru_cache(maxsize=8192)
def full_story_id(story_id: int) -> int:
    if master_data.query_one("SELECT 1 FROM single_mode_story_data WHERE story_id=?",
                             (story_id,)):
        return story_id
    row = master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE short_story_id=? ORDER BY id",
        (story_id,))
    return int(row["story_id"]) if row else story_id


@functools.lru_cache(maxsize=8192)
def short_story_id(story_id: int) -> int:
    row = master_data.query_one(
        "SELECT short_story_id FROM single_mode_story_data WHERE story_id=?", (story_id,))
    return int(row["short_story_id"] or 0) or story_id if row else story_id


def _fix(events, short: bool = False) -> None:
    for ev in events or ():
        if not isinstance(ev, dict):
            continue
        try:
            sid = int(ev.get("story_id") or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            sid = full_story_id(sid)
            ev["story_id"] = short_story_id(sid) if short else sid


def apply(resp: dict) -> None:
    data = resp.get("data")
    short = isinstance(data, dict) and any(
        int((blk.get("chara_info") or {}).get("short_cut_state") or 0) == 1
        for blk in [data, *(v for v in data.values() if isinstance(v, dict))]
        if isinstance(blk, dict))

    def walk(node, depth=0):
        if depth > 3 or not isinstance(node, dict):
            return
        for key, val in node.items():
            if key == "unchecked_event_array" and isinstance(val, list):
                _fix(val, short)
            elif isinstance(val, dict):
                walk(val, depth + 1)
    walk(resp.get("data"))
