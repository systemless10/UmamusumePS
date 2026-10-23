
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


def _fix(events) -> None:
    for ev in events or ():
        if not isinstance(ev, dict):
            continue
        try:
            sid = int(ev.get("story_id") or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            ev["story_id"] = full_story_id(sid)


def apply(resp: dict) -> None:
    def walk(node, depth=0):
        if depth > 3 or not isinstance(node, dict):
            return
        for key, val in node.items():
            if key == "unchecked_event_array" and isinstance(val, list):
                _fix(val)
            elif isinstance(val, dict):
                walk(val, depth + 1)
    walk(resp.get("data"))
