"""GRAND LIVE (scenario 3) -- event producers and resolvers.

Moved verbatim out of handlers/career_producers.py, which used to hold every
scenario's beats side by side and open each producer with an `is_active` test.
The test is gone: `@CE.producer(..., scenario=SCENARIO_ID)` makes career_events'
own dispatch skip these everywhere else, so this file cannot leak into a URA run
and no shared module has to know these beats exist.
"""

from __future__ import annotations

import logging
import random

from ... import career_events as CE
from ...handlers.career_producers import story_playable as _story_playable
from . import impl as grand_live
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")


# ===================================================== GRAND LIVE (sc. 3) ===
# turn -> [(event_id, story_id, n_choices, play_timing)], generated from the
# UmaDumpy 20260728_183307 capture: the turn the official server first served
# each beat, its real choice count, and its own play_timing. The five "Concert
# Ends!" beats are absent on purpose -- live_start returns those itself.
#
# KEYED ON ctx.turn -- the turn the CLIENT is on (the request's current_turn),
# NOT the post-action chara_info.turn. Re-derived from the capture after a live
# report that every beat fired a turn early: the capture shows the turn-4 unlock
# produced by exec_command(current_turn=4), so beat turn == request turn.
# Using the post-advance value put the whole chain on turn N-1, which also broke
# the Lives (the concert beat landed on 23, and live_start on a non-Live turn
# does nothing).
# NOT IN THIS TABLE, and deliberately: the "Training Level Up" (202056-202060),
# "Director's Appraisal" (202061-202064) and "Event Producer's Appraisal"
# (202065-202068) families. Seven of them used to sit here on fixed turns with a
# `gl:<id>` once_key, which fired each exactly once per career and never fired
# the other five at all. They are the RECURRING families URA already has -- see
# GrandLive.facility_levelup_event / .appraisal_events for the master.mdb
# derivation and the corpus correlation -- and are produced by the shared
# level-up and appraisal paths in single_mode_team.py.
GRAND_LIVE_BEATS: dict = {
    2:  [(202001, 400003400, 0, 6)],
    4:  [(202002, 400003401, 1, 6),     # Bring Back the Grand Concert! -- unlock
         (202035, 400003228, 1, 6),     # From Training Partners to Supporters!
         (202069, 400003027, 0, 1)],    # Tutorial (same turn, chains behind)
    # "A Quirky Correspondent?" -- Reporter Otonashi's unlock. URA runs it as
    # 1016/400001404 off the fixed schedule, which this scenario disables
    # (uses_fixed_schedule = False), so it was firing NOWHERE: 42 of the 66
    # scenario-3 careers in captures/bot_logs ack 101007 and all 42 do it on
    # turn 13. master.mdb single_mode_story_data row `id 101007` carries story
    # 400000100 "A Quirky Correspondent?" with short_story_id 0, so the wire
    # value is the plain story_id; one choice, timing 6 (12 captured entries).
    # NPC_UNLOCKS maps it to the reporter the same way 1016 is mapped.
    13: [(101007, 400000100, 1, 6)],
    18: [(202004, 400003403, 0, 6)],
    24: [(202005, 400003430, 1, 6),     # The First Concert Begins! (must be LAST
                                        # of the pre-live beats: resolving it has
                                        # to leave the chain empty)
         (202009, 400003425, 1, 12), (202037, 400003230, 1, 12)],
    25: [(202076, 400003028, 0, 1)],
    30: [(202010, 400003412, 0, 6)],
    36: [(202011, 400003432, 1, 6),     # The Second Concert Begins!
         (202017, 400003426, 1, 12), (202038, 400003230, 1, 12)],
    42: [(202018, 400003404, 0, 6)],
    48: [(202019, 400003435, 1, 6),     # The Third Concert Begins!
         (202039, 400003230, 1, 12)],
    50: [(202022, 400003405, 0, 6)],
    51: [(202023, 400003406, 0, 6)],
    54: [(202024, 400003407, 0, 6)],
    57: [(202025, 400003408, 0, 6)],
    60: [(202026, 400003437, 1, 3),     # The Fourth Concert Begins!
         (202040, 400003230, 1, 12)],
    65: [(202030, 400003409, 0, 6)],
    66: [(202031, 400003410, 0, 6)],
    67: [(202032, 400003411, 0, 6)],
    69: [(202075, 400003428, 5, 6)],    # Closer Together -- the gold-skill choice
    71: [(202034, 400003413, 1, 6)],    # Our Song -- awards Girls' Legend U
    72: [(202100, 400003460, 1, 3)],    # The Grand Concert Begins!
}

GL_UNLOCK_EVENT = 202002        # "Bring Back the Grand Concert!" -- unlocks lessons
GL_GIRLS_LEGEND_EVENT = 202034  # "Our Song" -- awards Girls' Legend U

# play_timing 12 = AFTER THE LIVE. These beats must NOT go into the turn's normal
# chain; live_start emits them once the concert has been performed.
#
# This is what opens the backstage screen. The capture's turn 24 reads:
#     check_event 202057 -> serves 202005 (Concert Begins)
#     check_event 202005 -> serves NOTHING          <-- backstage screen opens
#     master_square x3                              <-- the pre-live lessons
#     live_start          -> 202006 (Concert Ends, pt 12)
#     check_event 202006 -> 202009 (pt 12) -> 202037 (pt 12) -> ...
# Resolving "Concert Begins" has to leave the chain EMPTY. Queueing the pt-12
# beats alongside it meant something always followed, so the client never got
# the gap it needs and the screen never appeared (live-reported: "there's a
# special screen ... it doesnt appear in our PS").
POST_LIVE_TIMING = 12

# SCENARIO SUPPORTERS -- the umas the "From Training Partners to Supporters!" and
# "New Supporters!" beats recruit onto the Live stage. On the wire each gets its
# own evaluation_info_array row with target_id == chara_id and member_state 1,
# and the Live's performers are drawn from the member_state-1 rows. We emitted
# none of them, so the official's 13-row array was 11 for us.
#
# WHICH BEAT recruits WHICH supporters, read straight off the capture -- where a
# reward appears in the message AFTER the event that granted it:
#
#   msg 98  serves 202006 -> msg 99  pays the concert (SP / stats / fans)
#   msg 99  serves 202009 -> msg 100 adds Silence Suzuka, and +10 SP
#   msg 100 serves 202037 -> msg 101 adds the other three, and raises the caps
#
# So it is ONE supporter on "After the Nth Concert" and THREE on "New
# Supporters!" -- not an even split, and emphatically not all four on the
# concert's own card, which is where they were landing (live-reported).
SUPPORTER_GRANTS: dict = {
    202009: [1002],                 # After the First Concert
    202037: [1066, 1024, 1014],     # New Supporters!
    202017: [1026],                 # After the Second Concert
    202038: [1072, 1019],
    202039: [1059, 1023, 1027],     # after the Third -- all three land together
    202040: [1071],                 # after the Fourth
}

# "After the Nth Concert" carries a small SP reward of its own, on top of the
# concert payout: capture msg 100 shows +10.
AFTER_CONCERT_SP = 10

SUPPORTER_FIRST = 1046              # Smart Falcon -- recruited by GL_UNLOCK_EVENT below

# "From Training Partners to Supporters!" -- the OTHER turn-4 beat. Recruits
# from the player's own EQUIPPED deck ("training partners"), not the fixed
# scenario-link roster SUPPORTER_GRANTS draws from. No stat reward of its own.
# The roster is derived from the deck -- see _recruit_training_partners.
TRAINING_PARTNERS_EVENT = 202035

# "Closer Together" -- turn 69's gold-skill choice. User-supplied reference
# (2026-08-16): gated on 16+ songs learned by this point (girlsLegendGate below
# via countable_songs, which already excludes Girls' Legend U itself); below
# the gate the event is story-only, matching every other GL beat with no known
# reward. gain_select_id_index order matches the reference table's own row
# order (Smart Falcon, Mihono Bourbon, Agnes Tachyon, Silence Suzuka, None).
# Rarity (gold/white) if the trainee IS that character or runs one of their
# support cards; the other tier otherwise. "None" has no character-linked
# choice, so it always grants its single skill regardless of "linkage" --
# rarity is looked up from skill_data (grand_live.skill_rarity), never assumed
# from which "slot" a caller filed the id under (see skill_rarity's docstring:
# 200501 "Lane Legerdemain" is rarity=2/gold with no white counterpart at all,
# not the "white-only" skill this table originally assumed).
CLOSER_TOGETHER_EVENT = 202075
CLOSER_TOGETHER_SONG_GATE = 16
# choice_number -> (chara_id or None, gold_skill_id or None, white_skill_id)
CLOSER_TOGETHER_CHOICES = {
    1: (1046, 202281, 202282),   # Smart Falcon: Full Speed! / Full Tilt
    2: (1026, 200431, 200432),   # Mihono Bourbon: Concentration / Focus
    3: (1032, 201701, 201702),   # Agnes Tachyon: Come What May / All I've Got
    4: (1002, 200711, 200712),   # Silence Suzuka: Trackblazer / Rosy Outlook
    5: (None, 200501, None),     # None: Lane Legerdemain (only variant)
}


def supporters_for(full_state) -> list:
    """Every supporter recruited so far, in the order they joined. Purely a
    read of the seated list -- recruitment itself happens at event RESOLUTION
    (see _resolve_grand_live_beat), never inferred from the turn number. A
    turn-based shortcut here was the bug: turn 4 queues THREE beats, so
    `turn >= 4` was true from the first turn-4 response, before the player had
    even seen "Bring Back the Grand Concert!" -- the join then landed on
    whatever event happened to be served first, not on its own card, and
    carried no reward (live-reported: Smart Falcon's "joined your cause!"
    line and the beat's Guts/SP payout were both missing)."""
    return list(grand_live.state(full_state).get("supporters") or ())

# The "Nth Concert Begins!" beat of each Live. Resolving one of these is what
# puts the trainee into the backstage state -- see the resolver below.
CONCERT_BEGINS_EVENTS = frozenset({202005, 202011, 202019, 202026, 202100})


def post_live_beats(turn: int) -> list:
    """The play_timing-12 beats for this Live turn, in order."""
    return [b for b in GRAND_LIVE_BEATS.get(turn, ())
            if b[3] == POST_LIVE_TIMING]

# Payouts are deliberately EMPTY. The capture records WHICH beats fired and WHEN,
# not what each granted; inventing stat numbers for two dozen story beats would
# be fabrication dressed as data. The beats that genuinely grant something do it
# through the named resolver below, which is capture- or guide-backed.
_GL_STORY_ONLY = {"effects": []}


@CE.producer("grand_live_beats", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def grand_live_beats(ctx: CE.Ctx) -> list:
    out = []
    for event_id, story_id, n_choices, play_timing in GRAND_LIVE_BEATS.get(ctx.turn, ()):
        if play_timing == POST_LIVE_TIMING:
            continue          # live_start emits these -- see post_live_beats
        if not _story_playable(story_id):
            log.warning("grand live: story %s missing from master; skipping %s",
                        story_id, event_id)
            continue
        out.append(CE.Event(
            event_id=event_id, story_id=story_id, play_timing=play_timing,
            # 3 and 6 in the table above are not properties of the beat -- they
            # are whichever response kind happened to carry it in the ONE
            # capture the table was read off. Real stamps the timing from the
            # response (CE.ENDPOINT_TIMING), and the same id appears with both
            # in different careers: 202005 {3:2, 6:2}, 202024 {6:3, 3:1},
            # 202025 {6:1, 3:1} -- the difference is only whether that turn had
            # a race. Pinning 3 was a softlock: TIMING_FORBIDDEN_ENDPOINTS
            # refuses to serve a timing-3 event on exec_command, so "The Fourth
            # Concert Begins!" (turn 60, a training turn in 16 of 39 real
            # careers) sat at the head of the queue with the whole chain behind
            # it until the player happened to race. Timings 1 and 12 are left
            # alone: 12 is post-live (live_start emits it) and 1 is the
            # start/load tutorial beats, neither of which is a turn chain.
            timing_from_endpoint=play_timing in (3, 6),
            choices=[CE.Choice(effects=[]) for _ in range(n_choices)],
            # chara_id 0, NOT the trainee. Every 202xxx beat in the capture is
            # chara_id 0 -- they are scenario cutscenes, not character events,
            # and naming a character makes the client stage one in a scene that
            # does not have her.
            chara_id=0,
            once_key=f"gl:{event_id}",
            priority=CE.PRIO_SCENARIO,
            resolver="grand_live_beat",
            payload={"turn": ctx.turn},
        ))
    return out


# ------------------------------------------------- the RANDOM story pool --
# Five one-shot story events that are NOT on a schedule. master.mdb has them as
# one consecutive family -- single_mode_story_data 202051-202055, stories
# 400003300-400003304, event_category 0, no short_story_id:
#
#   202051 "We Are..."                  10 of 66 careers, turns  8..68
#   202052 "A \"Good Singer\""            16 of 66, turns 10..69
#   202053 "Proposal! A New Single"     13 of 66, turns  8..68
#   202054 "Tracen Idol"                13 of 66, turns  7..65
#   202055 "Every Bit of Effort Counts"  8 of 66, turns  6..70
#
# Each fires AT MOST ONCE per career and never on a fixed turn. 202054 was
# modelled as a fixed turn-46 beat (it fires on 46 in exactly zero real
# careers) and the other four were not modelled at all -- 60 servings across 66
# careers, i.e. ~0.9 per career, all missing.
#
# CHOICE COUNT: 202053, 202054 and 202055 are captured with exactly 2 choices,
# play_timing 1, chara_id 0. 202051 and 202052 have no wire capture; they are
# consecutive rows of the same family and are served the same way. If either
# turns out to be choice-LESS the client will re-serve it forever (the
# 201103 "Tutorial" failure mode -- see unity_cup.producers), so that is the
# first thing to check if one of these ever loops.
RANDOM_POOL = (
    (202051, 400003300), (202052, 400003301), (202053, 400003302),
    (202054, 400003303), (202055, 400003304),
)
RANDOM_POOL_CHOICES = 2
RANDOM_POOL_TIMING = 1
RANDOM_POOL_TURNS = (6, 70)      # observed span, inclusive
# 60 firings over 66 careers across ~65 eligible turns.
RANDOM_POOL_CHANCE = 0.9 / (RANDOM_POOL_TURNS[1] - RANDOM_POOL_TURNS[0] + 1)


@CE.producer("grand_live_random_pool", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def grand_live_random_pool(ctx: CE.Ctx) -> list:
    """At most one un-fired pool story per turn, drawn uniformly.

    Seeded on (trainee, turn) rather than the global RNG so polling the same
    turn twice cannot produce two different answers -- the same rule the token
    preview uses. once_key keeps each one to a single firing per career; emit()
    drops a repeat, so the roll only has to be stable within its own turn."""
    turn = int(ctx.turn or 0)
    if not (RANDOM_POOL_TURNS[0] <= turn <= RANDOM_POOL_TURNS[1]):
        return []
    # Draw from what is LEFT, not from all five. Rolling the whole pool and
    # letting emit() drop an already-fired draw silently costs firings the
    # further into a career you get (measured: 0.77 per career instead of the
    # real 0.91), and the loss is not uniform across the five.
    remaining = [(e, sid) for e, sid in RANDOM_POOL
                 if not CE.already_fired(ctx.full_state, f"gl:{e}")]
    if not remaining:
        return []
    rng = random.Random(f"glpool:{CE.career_salt(ctx.full_state)}:{turn}")
    if rng.random() >= RANDOM_POOL_CHANCE:
        return []
    event_id, story_id = rng.choice(remaining)
    if not _story_playable(story_id):
        return []
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=RANDOM_POOL_TIMING,
        choices=[CE.Choice(effects=[]) for _ in range(RANDOM_POOL_CHOICES)],
        chara_id=0,
        # SAME once_key namespace as the scheduled beats: 202054 used to be one
        # of those, and a career that already played it there must not get it
        # again from here.
        once_key=f"gl:{event_id}",
        priority=CE.PRIO_SCENARIO,
        resolver="grand_live_beat",
        payload={"turn": turn},
    )]


# The Grand Finale's ends-beat is any of TEN ids -- master.mdb
# single_mode_story_data 202041-202045 (short_story_id 400003417) and
# 202046-202050 (400003418). Real careers land all over them (202041 x32,
# 202046 x3, 202044/202045/202049 x1 each over 38 careers), and what selects
# the variant is not known. Accept every one, so which id we happen to serve
# can never be the thing that decides whether the payout fires.
GRAND_CONCERT_ENDS_EVENTS = frozenset(range(202041, 202051))
CONCERT_ENDS_EVENTS = frozenset({202006, 202012, 202020, 202027}) | GRAND_CONCERT_ENDS_EVENTS
NEW_SUPPORTERS_EVENTS = frozenset({202037, 202038, 202039, 202040})
AFTER_CONCERT_EVENTS = frozenset({202009, 202017, 202025, 202032})
# The Grand Concert's own "Ends" beat -- unlike the first four concerts, it has
# no After-the-Nth/New-Supporters! follow-up to release concert_turn (see
# NEW_SUPPORTERS_EVENTS handling below), so nothing else in its chain ever
# resets the held turn back to 0. Left alone, EVERY response for the rest of
# the run keeps reporting turn 72 (attach()'s held-turn override has no
# release condition of its own) -- live-reported as a permanent softlock right
# after the Grand Finale's rewards. Keyed on the whole variant SET above: this
# guard used to name 202046 alone, the rarest of the ten.


def apply_beat_rewards(full_state, chara_info, event_id) -> dict:
    """Pay the post-live beats at RESOLUTION, each on its own card.

    The client renders a card by diffing served state against the previous
    response, so WHICH card a reward appears on is decided entirely by WHEN we
    mutate. Applying the payout inside perform_live (during live_start) spent it
    before the first card existed -- invisible -- while the caps settled on a
    turn tick and landed on "The First Concert Ends!", which is the New
    Supporters! card's content.

    Called from check_event for EVERY event id, because "Concert Ends" is pushed
    inline by live_start rather than through the queue and so never reaches the
    resolver registry. Both appliers are idempotent (one pops, one clears its
    flag), so the queued beats resolving through both paths pay exactly once.

    Reached as the scenario's on_event_resolved hook, so it runs only for a
    Grand Live career -- the is_active guard this used to open with is now the
    registry lookup that found this scenario at all."""
    eid = int(event_id or 0)
    if eid in CONCERT_ENDS_EVENTS:
        paid = grand_live.apply_live_payout(full_state, chara_info)
        if paid:
            log.info("grand live: concert payout on beat %s -- +%s all stats, "
                     "+%s SP, +%s fans%s",
                     eid, paid["stat"], paid["sp"], paid["fans"],
                     f", skill {paid['skill']}" if paid.get("skill") else "")
        if eid in GRAND_CONCERT_ENDS_EVENTS:
            grand_live.state(full_state)["concert_turn"] = 0
        return paid
    granted = []
    for chara_id in SUPPORTER_GRANTS.get(eid, ()):
        if grand_live.add_supporter(full_state, chara_id):
            granted.append(chara_id)
    if eid in AFTER_CONCERT_EVENTS:
        chara_info["skill_point"] = chara_info.get("skill_point", 0) + AFTER_CONCERT_SP
    if eid in NEW_SUPPORTERS_EVENTS:
        if grand_live.settle_lives(full_state):
            log.info("grand live: token caps raised on beat %s", eid)
        # Last beat of the concert chain -- release the held turn, which is
        # exactly where the capture lets it move (message 100 -> 101).
        grand_live.state(full_state)["concert_turn"] = 0
    if granted:
        log.info("grand live: supporters %s joined on beat %s", granted, eid)
    return {"supporters": granted} if granted else {}


@CE.resolver("grand_live_beat")
def _resolve_grand_live_beat(full_state, chara_info, event, choice_number, **kw):
    """The two Grand Live beats that grant something, applied at RESOLUTION.

    At queue time the lesson unlock flipped the tokens on the instant the turn
    ticked, whether or not the cutscene was ever shown -- live-reported as "the
    event just doesn't play, I unlock them when the turn ticks"."""
    turn = (event.payload or {}).get("turn") or chara_info.get("turn") or 0

    # THE BACKSTAGE SCREEN. Resolving "Concert Begins" flips playing_state to 10,
    # which is the client's cue to open the Live screen (Anticipation gauge,
    # Lessons, and the concert button). It stays 10 through the backstage
    # master_square calls and is cleared by live_start.
    if event.event_id in CONCERT_BEGINS_EVENTS:
        chara_info["playing_state"] = grand_live.PLAYING_STATE_BACKSTAGE
        # HOLD THE TURN for the concert's whole chain. The official keeps
        # chara_info.turn at the Live's turn from here until the chain empties
        # (capture: turn 24 on messages 97-100, 25 only on 101). Our
        # exec_command already advanced to 25, so the client read "24 is behind
        # us, this must be the SECOND concert" and played the wrong animation --
        # every concert announced as the next one (live-reported).
        grand_live.state(full_state)["concert_turn"] = int(turn or 0)
        log.info("grand live: backstage screen open (event %s, turn %s)",
                 event.event_id, turn)
        return {"playing_state": grand_live.PLAYING_STATE_BACKSTAGE}

    if event.event_id == GL_UNLOCK_EVENT:
        result = {}
        # chara_info: Make Debut! refreshes the lesson board, and the board's
        # hint squares are screened against the trainee's own aptitudes
        # (grand_live._offered_squares) -- without her, that first board could
        # offer a Group Lesson she can never be taught anything by.
        if grand_live.grant_free_song(full_state, grand_live.MAKE_DEBUT_LIVE_ID,
                                      turn=turn, chara_info=chara_info):
            log.info("grand live: lessons unlocked on turn %s, Make Debut! granted",
                     turn)
            result["unlocked"] = True
            result["song"] = grand_live.MAKE_DEBUT_LIVE_ID
        # User-specified 2026-08-12 (live-observed; no capture exists for this
        # beat's payload -- see the module docstring's stance on fabricating
        # numbers, which this is NOT: it's the player's own reported client
        # behaviour, the same standing as a capture). "Bring Back the Grand
        # Concert!" also grants Guts +10, Skill Pts +10, and seats Smart
        # Falcon (1046) as the run's first supporter -- at RESOLUTION, so the
        # "joined your cause!" line lands on THIS card (see supporters_for's
        # docstring for why turn-based seating put it on the wrong one).
        result["not_up"] = grand_live._apply_stats(
            chara_info, {"guts": 10, "skill_point": 10}) or None
        # Capture, turn 4: the trainee's own row (target_id 0) flips to
        # member_state 1 on this beat alongside Smart Falcon's new row. Two
        # "joined your cause!" lines, which is exactly what was reported
        # missing -- "<Our Trainee> joined your cause! Smart Falcon joined
        # your cause!". The trainee is seated like any other supporter; she
        # keeps her target_id 0 row rather than gaining a second one.
        joined = []
        trainee = int(chara_info.get("card_id") or 0) // 100
        if trainee and grand_live.add_supporter(full_state, trainee):
            joined.append(trainee)
        if grand_live.add_supporter(full_state, SUPPORTER_FIRST):
            joined.append(SUPPORTER_FIRST)
        if joined:
            result["supporters"] = joined
        return result
    elif event.event_id == GL_GIRLS_LEGEND_EVENT:
        if grand_live.grant_free_song(full_state, grand_live.GIRLS_LEGEND_U_LIVE_ID,
                                      chara_info=chara_info):
            log.info("grand live: Girls' Legend U granted (not counted toward 18)")
            return {"song": grand_live.GIRLS_LEGEND_U_LIVE_ID}
    elif event.event_id == TRAINING_PARTNERS_EVENT:
        return _recruit_training_partners(full_state, chara_info)
    elif event.event_id == CLOSER_TOGETHER_EVENT:
        return _resolve_closer_together(full_state, chara_info, choice_number)
    return {}


def _resolve_closer_together(full_state, chara_info, choice_number) -> dict:
    """Turn 69's gold-skill choice -- see CLOSER_TOGETHER_CHOICES."""
    if grand_live.countable_songs(full_state) < CLOSER_TOGETHER_SONG_GATE:
        return {}
    pick = CLOSER_TOGETHER_CHOICES.get(int(choice_number or 0))
    if not pick:
        return {}
    linked_chara, gold_id, white_id = pick

    is_linked = False
    if linked_chara:
        trainee = int(chara_info.get("card_id") or 0) // 100
        is_linked = trainee == linked_chara or any(
            grand_live.support_card_chara(c.get("support_card_id") or 0) == linked_chara
            for c in chara_info.get("support_card_array") or ())

    # gold_id wins when linked, or when it's the only variant this row has
    # (white_id is None -- the "None" row's Lane Legerdemain).
    skill_id = gold_id if (gold_id and (is_linked or white_id is None)) else white_id
    if not skill_id:
        return {}
    rarity = grand_live.skill_rarity(skill_id)
    grand_live.add_skill_tip(chara_info, skill_id, 1, rarity=rarity)
    log.info("grand live: Closer Together granted skill %s (rarity %s)", skill_id, rarity)
    return {"hint": skill_id}


def _recruit_training_partners(full_state, chara_info) -> dict:
    """'From Training Partners to Supporters!' -- recruits from the player's own
    EQUIPPED deck, distinct from SUPPORTER_GRANTS' fixed scenario-link roster.

    WHICH deck cards, read off the capture's turn-4 -> turn-5 transition:

        pos 1  card 30028  chara 1068  type 1  owner 0             -> JOINS
        pos 2  card 30107  chara 1004  type 1  owner 0             -> JOINS
        pos 3  card 30052  chara 9008  type 2  owner 0             -- not an uma
        pos 4  card 30100  chara 1030  type 1  owner 0             -> JOINS
        pos 5  card 20002  chara 1009  type 1  owner 0             -> JOINS
        pos 6  card 30101  chara 1032  type 1  owner 466240681561  -- BORROWED

    So: every deck card whose chara is a trainee AND that the player actually
    owns. Slot 3 is excluded because 9008 (Light Hello) is not an uma; slot 6
    because a rented friend's card is not your training partner -- her chara
    joins later, on the turn-24 beat, not here.

    The count therefore falls out of the deck (four in the capture, three for a
    deck carrying one non-uma and one borrowed card) rather than being fixed.
    An earlier pass picked a hardcoded 3 at random from the deck, which put
    different umas on stage than the ones the player had been training with."""
    deck = chara_info.get("support_card_array") or []
    seated = set(grand_live.state(full_state).get("supporters") or ())
    candidates, seen = [], set()
    for card in sorted(deck, key=lambda c: int(c.get("position") or 0)):
        if int(card.get("owner_viewer_id") or 0):
            continue                       # borrowed from a friend, not ours
        chara_id = grand_live.support_card_chara(card.get("support_card_id") or 0)
        if not grand_live.is_trainee_chara(chara_id):
            continue                       # pal/group NPC card, never on stage
        if chara_id not in seen and chara_id not in seated:
            seen.add(chara_id)
            candidates.append(chara_id)

    if not candidates:
        log.warning("grand live: training-partners beat found no eligible "
                    "deck characters to recruit (empty/exhausted deck)")
        return {}

    granted = [c for c in candidates if grand_live.add_supporter(full_state, c)]
    if granted:
        log.info("grand live: training partners %s joined as supporters", granted)
    return {"supporters": granted} if granted else {}
