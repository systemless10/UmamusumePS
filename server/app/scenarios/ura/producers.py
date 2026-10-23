
from __future__ import annotations

import logging

from ... import career_events as CE

log = logging.getLogger("uma-server")

SCENARIO_ID = 1


SKILL_IRON_WILL = 200441
URA_FIXED_BEATS: dict = {
    29: (1017, 400001405, [{"type": "mood", "value": "+1"}]),
    35: (1027, 400000041, None),
    45: (1018, 400001406, [
        {"type": "wisdom", "value": "+20"},
        {"type": "skill_points", "value": "+20"},
        {"type": "skill_hint", "skill_id": SKILL_IRON_WILL, "value": 1}]),
    48: (1019, 400001407, [{"type": "skill_points", "value": "+30"}]),
    39: (102004, 400001419, [{"type": "energy", "value": "+30"}]),
    53: (1020, 400001408, None),
    59: (1028, 400000041, None),
    63: (102006, 400001419, [{"type": "energy", "value": "+30"}]),
    65: (1021, 400001409, None),
    72: (1022, 400001410, [{"type": "skill_points", "value": "+30"}]),
}


_SHARED_FIXED_STORIES = {
    story for story in [b[1] for b in URA_FIXED_BEATS.values()]
    if [b[1] for b in URA_FIXED_BEATS.values()].count(story) > 1}


def _fixed_once_key(event_id: int, story_id: int) -> str:
    if story_id in _SHARED_FIXED_STORIES:
        return f"ura_fixed:{story_id}:{event_id}"
    return f"ura_fixed:{story_id}"


@CE.producer("ura_fixed_beats", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def ura_fixed_beats(ctx: CE.Ctx) -> list:
    entry = URA_FIXED_BEATS.get(ctx.advanced_turn)
    if not entry:
        return []
    event_id, story_id, effects = entry
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=1,
        choices=[] if effects is None else [CE.Choice(effects=list(effects))],
        chara_id=0,
        once_key=_fixed_once_key(event_id, story_id),
        priority=CE.PRIO_SCENARIO,
    )]


SUMMER_PLANS_SCENARIOS = frozenset({2, 3, 4})


@CE.producer("summer_plans_shared", priority=CE.PRIO_SCENARIO)
def summer_plans_shared(ctx: CE.Ctx) -> list:
    if ctx.scenario_id not in SUMMER_PLANS_SCENARIOS:
        return []
    entry = URA_FIXED_BEATS.get(ctx.advanced_turn)
    if not entry or entry[0] not in (1027, 1028):
        return []
    event_id, story_id, _effects = entry
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=1, choices=[],
        chara_id=0, once_key=_fixed_once_key(event_id, story_id),
        priority=CE.PRIO_SCENARIO,
    )]
