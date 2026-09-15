"""URA FINALE (scenario 1) -- the base career, and every other scenario's
starting point.

Almost nothing lives here, and that is the point: base.Scenario's defaults ARE
URA's behaviour, so this package overrides nothing. What it does own is URA's
own fixed story chain, which used to sit in handlers/career_producers.py behind
an `if grand_live.is_active(...)` test -- a guard that would have needed a new
clause for every scenario added.
"""

from __future__ import annotations

import logging

from ... import career_events as CE

log = logging.getLogger("uma-server")

SCENARIO_ID = 1


# ============================================== URA SCENARIO FIXED (sc. 1) ==
# URA's own six-beat scenario chain, CORPUS-VERIFIED against the 20 scenario-1
# careers in captures/bot_logs (event_id @ current_turn, careers that fired it /
# careers that reached the turn):
#
#   1017 @ 29  15/15   Holding an Event!
#   1018 @ 45  13/13   A Three-Legged Race
#   1019 @ 48  13/13   At the Carrot Farm
#   1020 @ 53  13/13   The Finale is in Danger?!
#   1021 @ 65   6/6    Hand in Hand
#   1022 @ 72   4/4    Going to the URA Finale!
#
# THE IDS. These used to go out as the generic event_engine.CHARA_EVENT_ID
# (6000, the outings id), which the client keys its presentation off -- so six
# distinct scenario cutscenes were all announced as an outing. The mapping is
# forced: master.mdb groups exactly six short stories, 400001405-400001410, in
# this order, and single_mode_events.SCENARIO_STORIES already runs the same
# sequence one step earlier (1013->400001401 ... 1016->400001404).
#
# THE TURNS. 'Holding an Event!' is EARLY March of the Classic year (turn 29),
# not Late (30) -- 15/15 careers ack it on 29. 53 and 65 are new here: those two
# beats did not exist at all, so a third of URA's scenario chain never played.
# Keyed on the POST-action turn (ctx.advanced_turn), which is the turn number
# the client is on when it acks a play_timing 1 beat -- the acked current_turn
# above is that same number.
#
# NO FAN GATES. The wiki's 50,000 / 100,000 / 240,000 requirements are not what
# the server does: career_log_20260711_223106 fires 1018 on turn 45 with 31,154
# fans, and every one of the six fires in every career that reaches its turn,
# goal-failed runs included. The gates were silently skipping 3 of the 4 beats
# we had.
#
# THE CHOICE COUNT is now wire-measured, not assumed. captures/bot/URA Finale
# carries the whole chain with its event_contents_info intact -- 20 careers,
# zero variation -- and the count is NOT uniform:
#
#   1017 1 choice   1018 1   1019 1   1020 0   1021 0   1022 1
#
# `effects=None` below means "no choice_array at all", which career_events
# already supports (Event.choices == [] serves an empty array; see the note
# over wire_entry). 1020 and 1021 were going out with an invented acknowledge
# slot, and a choice count is not cosmetic -- see this module's own history and
# career_events' docstring: an event served with the wrong count is exactly the
# failure that file was restructured to prevent.
SKILL_IRON_WILL = 200441           # 'Iron Will' (text_data cat 47)
URA_FIXED_BEATS: dict = {
    # turn: (event_id, short_story_id, effects | None for a choiceless cutscene)
    29: (1017, 400001405, [{"type": "mood", "value": "+1"}]),
    # 'Summer Plans' (story 400000041) -- the camp announcement, two turns
    # before each summer camp (37-40 and 61-64). Two master rows, 2729 and
    # 2730, which is why one story has two ids: 1027 for the Classic camp and
    # 1028 for the Senior one. 20/20 careers at these turns, choiceless.
    35: (1027, 400000041, None),
    45: (1018, 400001406, [
        {"type": "wisdom", "value": "+20"},
        {"type": "skill_points", "value": "+20"},
        # "+3 levels if scenario link is active" -- scenario link is not modelled
        # anywhere yet, so the base 1 level is granted.
        {"type": "skill_hint", "skill_id": SKILL_IRON_WILL, "value": 1}]),
    48: (1019, 400001407, [{"type": "skill_points", "value": "+30"}]),
    # 'Hot Duo, Cool Snack' (story 400001419, master row id 102004/102006 --
    # the 102xxx band is the one place event_id IS single_mode_story_data.id,
    # confirmed on all six rows of the family). A Happy Meek beat that plays
    # INSIDE each camp, and the same two-rows-one-story shape as Summer Plans.
    # 20/20 careers, one acknowledge choice, no payout on the wire.
    39: (102004, 400001419, []),
    # 1020 and 1021 have no recorded payout anywhere -- master.mdb has the
    # stories but no reward table for this class (same as the race rewards and
    # the Director's sendoff), and the bot logs record acks, not effects. Served
    # with none rather than an invented amount; fill them in when a capture or
    # the user supplies real values.
    53: (1020, 400001408, None),
    59: (1028, 400000041, None),
    63: (102006, 400001419, []),
    65: (1021, 400001409, None),
    72: (1022, 400001410, [{"type": "skill_points", "value": "+30"}]),
}


# TWO BEATS CAN SHARE ONE STORY, so the story alone is no longer a unique key:
# 'Summer Plans' is 1027 then 1028 and 'Hot Duo, Cool Snack' is 102004 then
# 102006, both the same scene played twice a career. Keyed on the story alone
# the second firing would look like a repeat of the first and be dropped, which
# is the once_key trap held_turn_freezes_dedupe_keys was written about.
#
# The key is only WIDENED for the stories that are actually duplicated, so
# every beat that already had a stable key keeps the exact same string -- a
# career saved before this change must not re-fire the beats it has seen.
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
    # URA's own chain. Every other scenario has its own and none of these
    # appear in their captures -- serving them in Grand Live put a "URA
    # Finale" announcement in the middle of a Grand Concert run. The
    # scenario= filter above is what keeps them here now.
    # The turn key stays ctx.advanced_turn (the POST-action turn): the corpus
    # confirms that IS the turn number the client acks these on. Grand Live's
    # table keys on ctx.turn instead, because its beats are timed off a
    # different response kind.
    entry = URA_FIXED_BEATS.get(ctx.advanced_turn)
    if not entry:
        return []
    event_id, story_id, effects = entry
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=1,
        # None = a choiceless cutscene (1020/1021/1027/1028); [] = one
        # acknowledge slot with no payout. Both are wire-measured, see above.
        choices=[] if effects is None else [CE.Choice(effects=list(effects))],
        # chara_id 0 -- a scenario cutscene, not a character event. All 79 real
        # acks of 1013-1022 come back with chara_id 0; sending the trainee's own
        # made the client attribute the beat to her.
        chara_id=0,
        once_key=_fixed_once_key(event_id, story_id),
        priority=CE.PRIO_SCENARIO,
    )]
