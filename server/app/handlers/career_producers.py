
from __future__ import annotations

import logging

log = logging.getLogger("uma-server")


def story_playable(story_id: int) -> bool:
    from .. import master_data
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data "
        "WHERE story_id=? OR short_story_id=?", (story_id, story_id)))
