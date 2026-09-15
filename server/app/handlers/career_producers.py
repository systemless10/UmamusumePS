"""Event PRODUCERS shared by every scenario, plus the helpers their
scenario-specific siblings need.

Each family here answers one question -- "what should fire on this turn?" -- and
returns Events whose rewards travel with them. Compare with what this replaces:
a `_maybe_queue_*` function that queued into one of three lists, marked its own
private fired-set, and pushed a separate reward ctx that a matching branch in
check_event had to remember to consult.

A producer here CANNOT:
  * forget to pay out          -- the reward is on the Event
  * be served the wrong count  -- choice_array is len(choices)
  * leak across careers        -- one fired-set, cleared by career_events.reset
  * be unreachable at resolve  -- resolution runs before any branch

Adding a family means adding a function to this file. Nothing in check_event or
exec_command needs to change.

SCENARIO-SPECIFIC producers do NOT live here. They live with their scenario, in
app/scenarios/<name>/producers.py, and declare themselves with
`@CE.producer(..., scenario=<id>)` so career_events' own dispatch skips them
elsewhere. This file used to hold URA's and Grand Live's beats side by side,
each opening with an `is_active` test -- one more clause per scenario, in a file
no scenario owned.
"""

from __future__ import annotations

import logging

log = logging.getLogger("uma-server")

# No shared producer families exist yet -- every one written so far turned out
# to belong to a scenario. `from .. import career_events as CE` is the import a
# shared one would need; it is left out rather than sitting unused.


# ================================================================ helpers ===

def story_playable(story_id: int) -> bool:
    """A title in text_data is not enough -- single_mode_story_data must have the
    row, matched on EITHER column. The official server serves the short id, and
    19 of Grand Live's 22 beats exist only as a short_story_id, so a story_id-only
    check calls them all missing."""
    from .. import master_data
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data "
        "WHERE story_id=? OR short_story_id=?", (story_id, story_id)))
