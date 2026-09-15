"""The consecutive-race penalty: 'Race Fatigue' and 'After Repeated Races...'.

Running races back to back on consecutive turns costs the trainee mood, and
past a point stats and her skin. Two events carry it, both in the shared 5xx
story band every trainee has (master.mdb text_data category 181 lists suffix
508/509 for all 83 of them):

    508 'Race Fatigue'            -> event_id 7003
    509 'After Repeated Races...' -> event_id 7004

They are NOT scenario content -- URA, Grand Live, Unity Cup and Trackblazer all
serve them -- but they matter most in Trackblazer, where a career averages 37.9
races against URA's 24.4 and half the turns are spent racing.

Neither event was reachable before this module: 508/509 sat in the id table in
single_mode_team (_SHARED_BEAT_EVENT_IDS) with no producer, so the only way
'Race Fatigue' could ever appear was the 7% per-turn random chara-story roll
that scans the whole 5xx band -- untied to racing, and once per career. Both
suffixes are excluded from that pool now (_chara_random_stories), the same way
scheduled and secret stories are: an event with a real trigger must not also be
reachable from the lottery.


THE MODEL
---------
`streak` is the number of CONSECUTIVE TURNS ending with this one on which the
trainee raced. Any turn spent not racing resets it. `energy` is the energy she
had going INTO the race (the turn's starting vital), not what the race left her
with.

USER-SUPPLIED TABLE, per race, unconditional:

                        1 race  2 races  3 races  4+ races
    Mood Down    1+ En.     0%       0%      60%     100%
                 0 En.     15%      33%      90%     100%
    Skin Outb.   1+ En.     0%       0%      15%      33%
                 0 En.      4%       8%      25%      33%
    3 stats -10  1+ En.     0%       0%       0%      40%
                 0 En.      0%       0%       0%      40%

CORPUS CHECK (1,741 real Trackblazer careers in captures/bot_logs, streak and
post-race event read off each turn's api_calls; turns 24/48/72 excluded, see
below). The measurement agrees with the table wherever it can see it:

    streak  energy   n       7003     7004    either
      1     any      27,601   0.0%     0.0%     0.0%
      2     any      14,628   0.0%     0.0%     0.0%
      3     1+        4,388  58.8%     0.0%    58.8%
      3     0           690  60.1%     0.0%    60.1%
      4     1+          779  60.8%    39.0%    99.9%
      4     0           492  62.0%    38.0%   100.0%
      5+    either     1,271  54-62%  38-46%   100.0%

  * 'Mood Down' IS the event: 7003 lands mood -1 in 99.5% of 2,626 fires and
    7004 in 98.1% of 377 (the remainder is mood already at the floor of 1), so
    the table's Mood Down row and the event's own fire rate are the same
    number. 60% at 3 races and 100% at 4+ match exactly.
  * The '3 stats -10' row IS 7004's share of the 4+ column: 7004 applies
    exactly three -10s in 1,005 of 1,005 fires, and fires on 38.6-39.0% of
    4+ races against the table's 40%. So the two events are not independent
    rolls -- 7004 is what happens when the stat penalty lands.
  * Streak 1 and 2 fire NOTHING, in 42,229 races. The table's 0-energy cells
    for 1-2 races are therefore not carried by either event, and are left
    unimplemented (see ONE KNOWN DISAGREEMENT).
  * Energy makes no measurable difference to the fire rate (60.1% vs 58.8% at
    streak 3, n=5,078). The table says it should (90% vs 60%). The table wins
    here -- it is the spec -- but see below.

ONE KNOWN DISAGREEMENT. The table's 0-energy column is the only part the
corpus contradicts, and the only part of it we act on is the streak-3 Mood Down
cell (90% vs a measured 60.1%, n=690). The 1-2 race 0-energy cells (15%/33%
mood, 4%/8% skin) would need a penalty with no event behind it, and no such
event exists in 42,229 observed races, so nothing is implemented for them.
Flip _MOOD_DOWN[3] to (0.60, 0.60) to follow the corpus instead.

'Skin Troubles' (suffix 522 -> event 7002) is a DIFFERENT event and lives
elsewhere (single_mode_team._maybe_fire_condition_event): it is what procs
while the trainee already HAS Skin Outbreak, which is the condition these two
events hand out. Its ~10% per-turn rate is measured in that function's comment.
"""

import random

from . import conditions

# Story suffixes, both present for all 83 trainees.
FATIGUE_SUFFIX = 508          # 'Race Fatigue'          -> event_id 7003
REPEATED_SUFFIX = 509         # 'After Repeated Races...' -> event_id 7004

# The table above, as (energy > 0, energy == 0) pairs keyed by streak. Streaks
# past the last key use the last key's row -- the table's own "4+".
#
# These are the table's UNCONDITIONAL per-race probabilities, kept in that form
# so this stays a literal transcription of the source. Skin Outbreak is rolled
# only when an event actually fires, so its chance has to be divided by the
# fire rate on the way (see roll); at 4+ the fire rate is 1.0 and the two are
# the same number.
_MOOD_DOWN = {3: (0.60, 0.90), 4: (1.00, 1.00)}
_SKIN_OUTBREAK = {3: (0.15, 0.25), 4: (0.33, 0.33)}
_STAT_PENALTY = {3: (0.00, 0.00), 4: (0.40, 0.40)}

MIN_STREAK = min(_MOOD_DOWN)  # below this nothing can fire, at all, ever

# What 'After Repeated Races...' costs: -10 to three of the five, chosen at
# random without replacement. Corpus: exactly three, exactly -10, in 1,005 of
# 1,005 fires.
STAT_PENALTY_VALUE = -10
STAT_PENALTY_COUNT = 3
_STATS = ("speed", "stamina", "power", "guts", "wisdom")


def consecutive_races(race_history, turn) -> int:
    """How many turns in a row ending at `turn` the trainee has raced on,
    counting this one.

    Read off the career's own persisted race_history (turn numbers) rather than
    a counter of our own: history is already appended for every real race in
    race_end's commit block, already survives /load, and already backfills for
    careers that predate this feature. A separate counter would be a second
    source of truth to keep in sync with it.

    Several races on ONE turn (a retried race, the corpus's repeated race_start
    entries) count once -- the turn is the unit, not the race."""
    try:
        turn = int(turn)
    except (TypeError, ValueError):
        return 0
    turns = set()
    for h in race_history or ():
        try:
            turns.add(int(h.get("turn")))
        except (TypeError, ValueError, AttributeError):
            continue
    turns.add(turn)                 # this race may not be committed yet
    streak = 0
    while turn - streak in turns:
        streak += 1
    return streak


def _row(table: dict, streak: int, energy) -> float:
    """The table cell for this streak and energy, with streaks past the last
    row taking the last row ('4+')."""
    key = min(streak, max(table))
    if key not in table:
        return 0.0
    has_energy, no_energy = table[key]
    try:
        broke = int(energy) <= 0
    except (TypeError, ValueError):
        broke = False               # unknown energy reads as 'she had some'
    return no_energy if broke else has_energy


def roll(streak: int, energy, rng=None) -> dict | None:
    """Roll this race's fatigue outcome, or None if nothing happens.

    Returns {"suffix", "effects", "skin_outbreak"} -- `effects` in the event
    engine's own effect-list shape, ready to hang off a one-acknowledge choice
    so the normal preview/commit path applies it.

    The three rows of the table are NOT three independent rolls. Mood Down is
    the event firing at all, the stat penalty is which of the two events fires,
    and Skin Outbreak is the only genuinely separate roll -- conditional on an
    event, so the table's unconditional number is divided by the fire rate."""
    rng = rng or random
    if streak < MIN_STREAK:
        return None
    fire = _row(_MOOD_DOWN, streak, energy)
    if fire <= 0 or rng.random() >= fire:
        return None
    # Which event: 'After Repeated Races...' is exactly the branch that carries
    # the stat penalty, so the table's stat cell IS its share.
    repeated = rng.random() < _row(_STAT_PENALTY, streak, energy)
    # Mood -1 rides on both. The engine floors motivation at 1, so a trainee
    # already at Awful takes the event and no mood loss -- which is what the
    # corpus's residual 0.5%/1.9% of mood+0 fires are.
    effects = [{"type": "mood", "value": "-1"}]
    if repeated:
        for stat in rng.sample(_STATS, STAT_PENALTY_COUNT):
            effects.append({"type": stat, "value": str(STAT_PENALTY_VALUE)})
    skin = rng.random() < min(1.0, _row(_SKIN_OUTBREAK, streak, energy) / fire)
    if skin:
        effects.append({"type": "condition",
                        "condition_id": conditions.SKIN_OUTBREAK})
    return {"suffix": REPEATED_SUFFIX if repeated else FATIGUE_SUFFIX,
            "effects": effects, "skin_outbreak": skin}
