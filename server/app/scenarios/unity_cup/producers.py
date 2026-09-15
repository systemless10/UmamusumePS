"""UNITY CUP (scenario 2) -- the scenario's own story chain.

Every beat is lifted from captures/bot/20260905_152744_icarus/ with its real
event_id, story_id, chara, play_timing, CHOICE COUNT and show_clear. 57
distinct 201xxx events fire across a run; this table is all of them.

WHY THIS FILE HAS TO EXIST
    Without it a Unity Cup career serves URA's chain instead -- 1013/1014/1015
    on turns 2-4 and 102005 on turn 5. Those are Director Akikawa's unlock and
    Happy Meek's unlock, and NEITHER is in this scenario: the capture's
    chara_info.evaluation_info_array carries NPC targets 101/103/104/106/108
    and never 102 or 2001. (Trackblazer's does carry 102, so this is genuinely
    per-scenario, not a Global-wide removal.) The scenario class turns those
    off via uses_fixed_schedule / has_director_ending / has_versus_npc; this
    file fills the gap they leave.

KEYED BY ctx.turn, DIRECTLY
    The table's key is the REQUEST current_turn each beat actually arrived on
    in the capture, which is exactly ctx.turn -- no offset arithmetic. An
    earlier version keyed on the response's chara_info.turn and needed a
    per-timing correction to undo that; re-deriving straight from the request
    turn removed both the corrections and the class of bug they caused.

PLAY TIMINGS
     1  the NEXT turn's opening cutscene, served on this turn's requests
     2  pre-race          3  post-race          6  after a training
     9  after a TEAM race (withheld here; team_race_out serves the head and
        check_event the rest -- team_race_end_out is never called by the real
        client: 0 requests in 24,568 bot-log calls and 0 in the wire captures)
    10  the team-race / scouting announcement screens   (Unity Cup only)
    11  the mid-run team briefing, turns 18 and 72      (Unity Cup only)
"""

from __future__ import annotations

import logging

from ... import career_events as CE
from . import impl as unity_cup

log = logging.getLogger("uma-server")

SCENARIO_ID = unity_cup.SCENARIO_ID

# ctx.turn -> [(event_id, story_id, chara_id, play_timing, choice_count, show_clear)]
#
# choice_count is NOT decorative. A pure cutscene ships choice_array: [] on the
# wire, and handing the client a one-entry array for one is what made the
# "Tutorial" beat (201103) loop: the client plays a choice-less story and
# acknowledges it WITHOUT a choice_number, so an event advertising a choice
# never receives the resolution it is waiting for and is re-served forever.
# 201121/201162 ("Growing as a Team") are NOT here: they are not on a schedule
# at all -- see unity_cup_facility_level.
BEATS: dict = {
    1:  [(201024, 400002400, 0, 1, 0, 0)],
    2:  [(201025, 400002401, 0, 6, 1, 0),
         (201114, 400002214, 0, 6, 1, 0),
         (201103, 400002041, 0, 1, 0, 0)],
    4:  [(201026, 400002402, 0, 6, 1, 0),
         (201160, 400002222, 0, 6, 1, 0)],
    5:  [(201027, 400002403, 0, 6, 0, 0),
         (201161, 400002221, 0, 6, 1, 0)],
    6:  [(201158, 400002219, 0, 6, 1, 0)],
    7:  [(201001, 400002100, 0, 6, 1, 0),
         (201159, 400002220, 0, 6, 1, 0)],
    # "A Quirky Correspondent?" -- the reporter's unlock, shared with Grand
    # Live (see that scenario's table for the master.mdb derivation). 137 of the
    # 147 scenario-2 careers in captures/bot_logs ack 101007, every one of them
    # on turn 13.
    13: [(101007, 400000100, 0, 6, 1, 0)],
    18: [(201028, 400002404, 0, 6, 1, 0)],
    20: [(201068, 400002302, 1010, 1, 1, 0)],
    21: [(201093, 400002116, 0, 10, 0, 0)],
    24: [(201029, 400002405, 0, 10, 1, 0)],
    33: [(201002, 400002101, 0, 6, 1, 0),
         (201095, 400002116, 0, 10, 0, 0)],
    36: [(201034, 400002409, 0, 10, 1, 0)],
    41: [(201039, 400002414, 0, 6, 0, 0)],
    43: [(201040, 400002415, 0, 6, 0, 0)],
    44: [(201041, 400002416, 0, 3, 0, 0)],
    45: [(201097, 400002116, 0, 10, 0, 0)],
    48: [(201042, 400002417, 0, 3, 1, 0)],
    49: [(201046, 400002421, 0, 6, 0, 0)],
    50: [(201047, 400002422, 0, 6, 1, 0)],
    54: [(201048, 400002423, 0, 3, 0, 0)],
    57: [(201003, 400002102, 0, 6, 1, 0),
         (201098, 400002116, 0, 10, 0, 0)],
    # The ORDINARY fourth-round gate. 201165 is its elite-team variant and is
    # swapped in below, exactly the way 201163 replaces the finals gate --
    # see ROUND_FOUR_GATE.
    60: [(201049, 400002424, 0, 3, 1, 0)],
    65: [(201053, 400002428, 0, 6, 0, 0),
         (201070, 400002304, 1056, 1, 1, 0)],
    68: [(201054, 400002429, 0, 3, 0, 0)],

    72: [(201055, 400002430, 0, 3, 1, 0)],
    # turn 78's two beats are NOT here: they bookend the finale's third race
    # and have to be ordered against it, not against the turn. See
    # unity_cup_finale_bookends.
}

# "The Acting Director" (turn 4, story 400002402) -- the beat that puts Riko
# Kashimoto in charge, and the gate on her showing up in training at all. See
# impl.apply_riko_gate.
ACTING_DIRECTOR_EVENT = 201026
ACTING_DIRECTOR_TURN = 4


# --------------------------------------------------- Team Power Increased --
# level reached -> (event_id, story_id). NOT a schedule: these fire when the
# team's stats have EARNED the level, which is why they land on different turns
# in every run. text_data category 181 titles them literally "Team Power
# Increased: A Sweet Gift" and so on, and single_mode_story_data confirms the
# ids; the level->event map itself is server-side (no master.mdb table), so it
# is derived from four independent capture runs that all agree on this exact
# order across 32 transitions.
POWER_UP_EVENTS = {
    2: (201010, 400002109),   # A Sweet Gift
    3: (201077, 400002118),   # A Warm Gift
    4: (201011, 400002110),   # A Happy Gift
    5: (201078, 400002119),   # A Lovely Gift
    6: (201012, 400002111),   # A Nice Gift
    7: (201013, 400002112),   # A Surprise Gift
    8: (201014, 400002113),   # The Greatest Gift
    9: (201015, 400002121),   # A Golden Gift
}
POWER_UP_TIMING = 10
POWER_UP_KEY = "unity_cup_power_up"

# ...AND IT PAYS THE TRAINEE. Tazuna's gift is not just a cutscene: the level
# it awards is worth that many points in ALL FIVE stats, to the trainee only
# (the teammates' own numbers do not move on these responses). Served with no
# effects the panel read "Team Power went up!" and nothing happened, which is
# what the player saw (user-reported 2026-09-06, screenshot of "A Sweet Gift").
#
# level reached -> gain in every stat. 24 transitions across three capture runs
# agree exactly, e.g. 0147->0148 in 20260905_152744_icarus: team_power 1 -> 2
# with the trainee going 217/197/168/129/157 -> 221/201/172/133/161.
#
# NO SKILL POINTS. Every one of those 24 transitions pays 0 SP and 0 energy --
# the skill points come from the team RACES instead (TEAM_RACE_REWARDS: 17 SP
# for a rank-3 set through 102 for Boss+), which is the pay-out that lands with
# the ladder move.
POWER_UP_GAIN = {2: 4, 3: 4, 4: 5, 5: 5, 6: 6, 7: 7, 8: 8, 9: 10}

# ...AND THE TOP TWO LEVELS ALSO TEACH "It's On!". Levels 8 and 9 are the S and
# S+ team ranks (impl.team_power's ladder tops out at 9 = SPlus), and GameTora's
# "S and S+ Team Ranks" section says reaching each awards a hint for It's On!
# -- normally level 1, but level 3 when the trainee is one of the scenario's own
# story characters, "so you can max out the hint by reaching S+ rank with one of
# the scenario-linked characters" (1 + 1 = 2 is not a max; 3 + 3 = 6, clamped to
# 5, is).
ITS_ON_SKILL = 200461
ITS_ON_LEVELS = (8, 9)
ITS_ON_HINT = 1
ITS_ON_HINT_LINKED = 3

# ------------------------------------------------------- "The Word Spreads" --
# The first scouted teammate's arrival. NOT in BEATS, because the beat alone is
# not enough: the client resolves the <support> token in "<support> has joined
# the team!" from event_contents_info.support_card_id, and a table of fixed
# tuples has no way to name the teammate who actually joined. Serving 0 there
# rendered the line literally, with a blank portrait (user-reported
# 2026-09-05).
#
# Capture 0138 has event 201020 / story 400002205 / chara 1010 / support card
# 10008 on turn 3, and 10008 is single_mode_scout_chara row 8's
# support_card_id -- i.e. the pool row the teammate joined from already carries
# it. So the event is built from the roster instead of from a table.
#
# ONCE per career. The later scout waves are announced by the timing-10
# scouting screens (201093/201095/201097/201098, turns 21/33/45/57, already in
# BEATS); "The Word Spreads" itself fires exactly once, and 201157 -- the
# second event master.mdb gives that same title -- appears in no capture at all.
# ------------------------------------------------------- the race gate --
# "Before the Nth Round of the Unity Cup" -- the beat that OPENS a team race.
# text_data category 181 names all five literally, and they are the last event
# served on each of the five race turns (24/36/48/60/72).
#
# THE CLIENT OPENS THE RACE SCREEN ON AN EMPTY CHAIN, not on this event. The
# capture is the same shape at all five races:
#
#     check_event   -> [(201029, 10)]      the announcement
#     check_event   -> []                  <-- THE SIGNAL
#     team_edit / opponent_list / team_race_start ...
#
# So resolving one of these must leave unchecked_event_array EMPTY. Without
# that hold, _drain_pending happily serves whatever else is queued in the SAME
# response -- a support-card event, a Team Power award, anything -- the client
# never sees the empty array, and the turn simply advances with no race
# (user-reported 2026-09-05: "never sent to the unity race select screen, just
# went to next turn").
#
# This is the identical failure Grand Live's backstage screen had, and is why
# base.Scenario carries holds_chain_after at all -- Unity Cup just never
# overrode it.
RACE_GATE_EVENTS = frozenset({
    201029,   # Before the First Round        turn 24
    201034,   # Before the Second Round       turn 36
    201042,   # Before the Third Round        turn 48
    201049,   # Before the Fourth Round       turn 60
    201165,   # ... and its ELITE variant
    201055,   # Before the Fifth Round        turn 72
    201163,   # ... and its ELITE variant, which is a gate exactly like 201055
})
# 201163 belongs here for the same reason it exists: unity_cup_beats swaps it in
# for 201055 whenever the run beat the elite team, so on those runs it IS the
# finals gate. Leaving it out meant the finals cutscene played and then nothing
# -- no playing_state 7, no empty chain -- so the race never opened, the turn-72
# hold never lifted, and the client re-offered turn 72's goal race forever
# (user-reported 2026-09-06: "softlocked on the Arima Kinen, winning it just
# sends me back to the Arima Kinen"). Any future gate variant must be added
# here as well as produced.

# ------------------------------------------- the team race RESULT beats --
# THE ROUND, NOT THE TURN. These used to live in BEATS keyed on the scheduled
# race turn (24/36/48/60/72), and _take_post_race_events looked them up by the
# request's current_turn -- so a player who reached the race screen a turn late
# raced on turn 25, the lookup missed, and the ENTIRE result sequence was
# skipped: no "Round One: Victory!", no "New Members Join!", nothing. Worse, it
# is that check_event which settles the race (see UnityCup.settle -- the
# race-flow endpoints deliberately do not), so with no event to serve, the
# ladder move and the trainee's stat/SP pay-out were left to land silently on
# the NEXT request -- which was the next training, and the client duly added
# them to the training result panel. That is the "+5 speed/stamina/power/guts
# from a WIT training" the user reported on 2026-09-06; the race award had
# leaked into the training's own numbers.
#
# EVERY ROUND HAS THREE OUTCOMES, and master.mdb names them unambiguously --
# round three is "We All Won Together!" / "Just a Little More!" / "We'll Win
# Next Time", and the other four rounds follow the same win / draw / lose
# wording. Only the win beat was ever served; a lost or drawn round played the
# victory cutscene.
#
# The story ids are single_mode_story_data.SHORT_story_id, which is what the
# wire actually carries (story_id 400002006 is served as 400002406 -- verified
# against the capture for all 24 beats already in BEATS above). That column is
# why none of these had to be guessed.
#
# The Update-2 variants are deliberately NOT here: 201166 ("Round Four: Beating
# the Elite Team!") and 201163/201164 (the strengthened team Zenith finals)
# belong to the elite-team path, which this scenario does not offer yet.
TEAM_RACE_RESULT_EVENTS = {
    #     WIN                      DRAW                     LOSE
    1: ((201030, 400002406), (201031, 400002407), (201032, 400002408)),
    2: ((201035, 400002410), (201036, 400002411), (201037, 400002412)),
    3: ((201043, 400002418), (201044, 400002419), (201045, 400002420)),
    4: ((201050, 400002425), (201051, 400002426), (201052, 400002427)),
    5: ((201056, 400002431), (201057, 400002432), (201058, 400002433)),
}

# "After the Race: New Members Join!" -- rounds 1-3 only; the corpus has no
# fourth or fifth (201146 n=137, 201147 n=135, 201148 n=131 over 147 runs, and
# nothing of the kind on turns 60 or 72).
TEAM_RACE_JOIN_EVENTS = {
    1: (201146, 400002216),
    2: (201147, 400002216),
    3: (201148, 400002216),
}

def team_race_join_round(event_id):
    """The round whose join beat this is, or 0."""
    for round_no, (ev, _story) in TEAM_RACE_JOIN_EVENTS.items():
        if ev == event_id:
            return round_no
    return 0


# show_clear on the result beat: 4 in the capture for all five rounds (the
# panel that lists what the round paid). The join beat carries 0.
RESULT_SHOW_CLEAR = 4


# BEATING THE ELITE TEAM HAS ITS OWN BEAT, and so does the finals against the
# strengthened Zenith. Corpus (147 scenario-2 runs in captures/bot_logs):
#
#   201165  turn 60, 110 runs   the elite team appears
#   201166  turn 60,  50 runs   ...and is beaten
#   201055  turn 72,  78 runs   the ordinary finals gate
#   201163  turn 72,  49 runs   the STRENGTHENED finals gate
#   201056  turn 72,  74 runs   the ordinary finals win
#   201164  turn 72,  47 runs   the strengthened finals win
#
# and every one of the 49 runs carrying 201163 also carries 201165 -- the two
# branches never mix, which is what says 201163/201164 REPLACE 201055/201056
# rather than adding to them.
ELITE_RACE_ROUND = 4          # the fourth team race, turn 60
# Turn 60 has TWO gates and they never mix. Across the 129 scenario-2 careers
# in captures/bot_logs that reached it, 201049 fires in 19 and 201165 in 110,
# and no career carries both. The chains behind them are just as clean:
#
#     201049 -> 201050          the ordinary round-four win
#     201165 -> 201166          the elite team, beaten
#     201165 -> 201052          the elite team, lost to
#
# So 201165 is the gate for a board that HAS an elite card on it -- which is
# what impl.elite_available decides -- and 201166 REPLACES the ordinary win
# beat rather than following it (201050 appears in 18 careers, 201166 in 50,
# and never together).
ROUND_FOUR_GATE = (201049, 400002424)
ELITE_ROUND_FOUR_GATE = (201165, 400002446)
ELITE_WIN_EVENT = (201166, 400002447)
FINALS_GATE = (201055, 400002430)
ZENITH_STRONG_GATE = (201163, 400002448)
ZENITH_STRONG_WIN = (201164, 400002449)


def post_race_beats_for_round(round_no, result_state, beat_elite=False) -> list:
    """The play_timing 9 beats for the round that just finished.

    result_state is the wire's AoharuRaceResultState: 1 win, 2 lose, 3 draw."""
    outcomes = TEAM_RACE_RESULT_EVENTS.get(int(round_no or 0))
    if not outcomes:
        return []
    index = {1: 0, 3: 1, 2: 2}.get(int(result_state or 1), 0)
    event_id, story_id = outcomes[index]
    if int(round_no or 0) == 5 and beat_elite and index == 0:
        event_id, story_id = ZENITH_STRONG_WIN
    if int(round_no or 0) == ELITE_RACE_ROUND and beat_elite and index == 0:
        # REPLACES 201050, exactly the way ZENITH_STRONG_WIN replaces the
        # ordinary finals win above. Appending it served both, and the corpus
        # is unambiguous that they are alternatives: 201050 in 18 careers,
        # 201166 in 50, never one career with both.
        event_id, story_id = ELITE_WIN_EVENT
    out = [(event_id, story_id, 0, POST_RACE_TIMING, 1, RESULT_SHOW_CLEAR)]
    join = TEAM_RACE_JOIN_EVENTS.get(int(round_no or 0))
    if join:
        out.append((join[0], join[1], 0, POST_RACE_TIMING, 1, 0))
    if int(round_no or 0) == len(unity_cup.TEAM_RACE_TURNS):
        # ...AND THE FIFTH ROUND HANDS OFF TO THE URA FINALE IN THE SAME CHAIN.
        # 101008 is the last thing that plays on the finals turn, not the first
        # thing on the next one -- capture, all four runs:
        #
        #     team_race_out  [(201056|201164, 9)]
        #     check_event    [(101008, 9)]
        #     check_event    []
        #
        # It used to be produced by unity_cup_ura_invite instead, and a producer
        # cannot reach the client here: the response that resolves the last
        # result beat returns early on holds_chain_after with an EMPTY array
        # (that empty array is the client's cue), so nothing is polled and the
        # invitation could not be served until the NEXT turn's first request --
        # exactly one turn late, which is where the player saw it
        # (user-reported 2026-09-07). In the chain it is the beat the turn hold
        # waits on, so it lands as the last event of turn 72.
        out.append((URA_INVITE_EVENT, URA_INVITE_STORY, 0, POST_RACE_TIMING, 0, 0))
    return out


# ------------------------------------------------ "Team Zenith Declares War" --
# Turn 69, and it pays. The screenshot the player sent has the banner "Main
# Scenario Event / Team Zenith Declares War" over a counter reading "3 turn(s)
# Until the Unity Cup" -- the finals are turn 72, so the event is turn 69, and
# turn 69 carries exactly one scenario event in the corpus: 201099, in 127 of
# 147 runs. (GameTora dates it "Senior Year, late November", which is turn 70;
# the announcement is served the turn before, the same way the four scouting
# screens sit three turns ahead of their races.)
#
# WHAT IT GIVES, from GameTora's "Unity Cup Special Skills": a hint for the
# Burning/Ignited Spirit skill of the team's HIGHEST stat rank -- the player's
# own screenshot says "The team's unbelievably passionate focus on Wit has had
# a positive effect" -- plus that stat and skill points, all four scaled by how
# many Spirit Bursts the career has fired. The white (Ignited) halves are
# rarity 1 and the gold (Burning) halves rarity 2 of skill groups 21001-21005.
ZENITH_WAR_EVENT = 201099
ZENITH_WAR_STORY = 400002117
ZENITH_WAR_TURN = 69
ZENITH_WAR_TIMING = 10
ZENITH_WAR_KEY = "unity_cup_zenith_war"

# stat -> (Burning/gold, Ignited/white)
SPIRIT_SKILLS = {
    "speed":   (210011, 210012),
    "stamina": (210021, 210022),
    "power":   (210031, 210032),
    "guts":    (210041, 210042),
    "wiz":     (210051, 210052),
}

# lowest burst count in the band -> (gold?, hint level, stat and skill points).
# GameTora gives four bands and says the values below four bursts are unknown,
# so under four this stays a plain cutscene rather than an invented reward.
ZENITH_WAR_BANDS = (
    (13, True, 3, 40),
    (10, True, 1, 30),
    (7, False, 3, 20),
    (4, False, 1, 10),
)


def zenith_war_reward(bursts: int, stat: str) -> list:
    """The event's effects for a career that fired `bursts` Spirit Bursts."""
    for floor, gold, level, amount in ZENITH_WAR_BANDS:
        if int(bursts or 0) >= floor:
            skill_id = SPIRIT_SKILLS[stat][0 if gold else 1]
            return [{"type": stat, "value": "+%d" % amount},
                    {"type": "skill_point", "value": "+%d" % amount},
                    {"type": "skill_hint", "skill_id": skill_id,
                     "value": "+%d" % level}]
    return []


@CE.producer(ZENITH_WAR_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_zenith_war(ctx: CE.Ctx) -> list:
    if int(ctx.turn or 0) != ZENITH_WAR_TURN:
        return []
    st = unity_cup.state(ctx.full_state)
    stat = unity_cup.best_stat(st)
    effects = zenith_war_reward(st.get("bursts"), stat)
    return [CE.Event(
        event_id=ZENITH_WAR_EVENT,
        story_id=ZENITH_WAR_STORY,
        play_timing=ZENITH_WAR_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key=ZENITH_WAR_KEY,
        priority=CE.PRIO_SCENARIO,
    )]


# ----------------------------------------------------------- "A Team at Last" --
# The team NAMING beat, Junior Class late September -- single_mode_turn puts
# that at turn 18 exactly, and the corpus agrees: one of these fires on turn 18
# in 137 of 147 runs and never on any other turn.
#
# TWO VARIANTS, and the difference is the deck. 201154 (103 careers) ships a
# choice per uma name the career may pick; 201019 (28 careers) ships exactly ONE
# choice and is what plays when none is available -- the capture's team_name_id
# goes straight from 0 ("Name Pending") to 5 ("Team Carrot") when it resolves.
# That single choice is the whole reason the choice list is built from the
# ELIGIBLE names rather than always five: if the client rendered all five and
# greyed the rest out, 201019 would have shipped five too.
TEAM_NAME_TURN = 18
TEAM_NAME_EVENT_PICK = (201154, 400002445)
TEAM_NAME_EVENT_FIXED = (201019, 400002444)
TEAM_NAME_TIMING = 10
TEAM_NAME_RESOLVER = "unity_cup_team_name"

# ------------------------------------------- "A Present from Director Akikawa!" --
# The last beat of the run: the gold skill the team's NAME earned. Six stories
# (400002434 + team_name_id) and two event-id families -- 201104 + name_id when
# the URA finals were won, 201059/201060 + name_id when they were lost. name_id
# 0 is "Three Years of Hard Work!", the no-prize version, which is what the 26
# corpus runs that lost the Unity Cup finals were served.
FINALS_PRESENT_STORY = 400002434
FINALS_PRESENT_WON = 201104
FINALS_PRESENT_LOST = 201060      # 201059 for name_id 0; see below
FINALS_PRESENT_LOST_NONE = 201059
FINALS_PRESENT_TIMING = 3
FINALS_PRESENT_TURN = 78


def finals_present_beat(name_id: int, won_ura: bool) -> tuple:
    """(event_id, story_id) for the endgame present."""
    name_id = int(name_id or 0)
    if won_ura:
        event_id = FINALS_PRESENT_WON + name_id
    else:
        event_id = (FINALS_PRESENT_LOST + name_id if name_id
                    else FINALS_PRESENT_LOST_NONE)
    return event_id, FINALS_PRESENT_STORY + name_id


SCOUT_JOIN_EVENT = 201020
SCOUT_JOIN_STORY = 400002205
SCOUT_JOIN_TIMING = 6

TURN_START_TIMING = 1        # the NEXT turn's opening cutscene
AFTER_TRAINING_TIMING = 6    # literally "after a training" -- gated on one
POST_RACE_TIMING = 9         # withheld -- team_race_end_out serves these

# "Team Support" -- the beat where the player's six support cards join the
# team. The roster intake hangs off THIS event resolving rather than off a bare
# turn number: the event IS the in-fiction moment they join, so growing the
# roster on a turn check instead put six teammates on the team screen before
# the player had been told there was a team.
TEAM_SUPPORT_EVENT = 201114

# "What's the Unity Cup?" -- the beat that explains the scenario and unlocks
# the scouting UI (team_info.is_scout_enable). Like the roster intake this
# hangs off the event resolving rather than off a turn number, because the
# capture flips the flag exactly between 0133 (still False) and 0134 (True) --
# the two responses either side of 201025 being acknowledged, both on turn 2.
SCENARIO_INTRO_EVENT = 201025


def beats_for(turn) -> list:
    return BEATS.get(int(turn or 0), [])


def post_race_beats(turn) -> list:
    """Any play_timing 9 beat still keyed on a literal turn. BEATS no longer
    carries one -- see TEAM_RACE_RESULT_EVENTS -- so this is the empty
    fallback that keeps a stale caller honest."""
    return [b for b in beats_for(turn) if b[3] == POST_RACE_TIMING]


def _is_training_poll(ctx) -> bool:
    """Whether this poll came from a TRAINING exec_command rather than from the
    turn's opening check_event.

    play_timing 6 means "after a training", and the capture holds to it: on
    turn 2 the chain starts at exec_command(current_turn=2) with 201025, never
    at that turn's opening check_event. Emitting them on the open instead is
    what made "What's the Unity Cup?" and "Team Support" play at the START of
    turn 2 -- they drained together with the previous turn's boundary chain
    (live-reported)."""
    payload = getattr(ctx, "payload", None) or {}
    return payload.get("command_type") is not None


@CE.producer("unity_cup_beats", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_beats(ctx: CE.Ctx) -> list:
    """This scenario's fixed chain.

    The scenario= filter is what keeps these out of every other career, the
    same way ura/producers.py keeps URA's 1013/1014/1015 chain out of this one.
    """
    training = _is_training_poll(ctx)
    out = []
    for event_id, story_id, chara_id, timing, choice_count, show_clear in beats_for(ctx.turn):
        if timing == POST_RACE_TIMING:
            continue                     # served by the team race's own chain
        if (event_id, story_id) == FINALS_GATE and unity_cup.beat_elite(
                unity_cup.state(ctx.full_state)):
            # Blue flames, not red: a team that beat an elite team meets the
            # STRENGTHENED Zenith, and the finals gate is its own beat.
            event_id, story_id = ZENITH_STRONG_GATE
        if (event_id, story_id) == ROUND_FOUR_GATE and unity_cup.elite_available(
                unity_cup.state(ctx.full_state), unity_cup.ELITE_RACE_INDEX):
            # The same swap one round earlier: the elite team is ON THE BOARD,
            # so the gate that opens the round is its own beat. Whether the
            # player then picks it and wins decides 201166 vs 201052 -- see
            # post_race_beats_for_round.
            event_id, story_id = ELITE_ROUND_FOUR_GATE
        if event_id in RACE_GATE_EVENTS:
            # ...and the gate's timing is the RESPONSE's, not the round's.
            timing = gate_timing(training)
        if timing == AFTER_TRAINING_TIMING and not training:
            # ONLY timing 6 is gated. Timings 10/11/2/3 fire on any poll --
            # gating them too would strand the team-race announcements on a
            # turn the player races instead of trains, which is exactly what
            # turn 24 is.
            continue
        out.append(CE.Event(
            event_id=event_id,
            story_id=story_id,
            play_timing=timing,
            # A choice-less cutscene must ship an EMPTY choice_array.
            choices=[CE.Choice(effects=[])] if choice_count else [],
            chara_id=chara_id or None,
            once_key=f"unity_cup_beat:{event_id}",
            priority=CE.PRIO_SCENARIO,
        ))
    # THE ONE BEAT THAT IS ALLOWED TO ARRIVE LATE. Every other timing-6 beat
    # is cosmetic if the player spends turn 4 resting or racing and it never
    # comes back, but this one is a GATE: Riko Kashimoto is off the training
    # screen until it resolves (impl.apply_riko_gate), so losing it would lock
    # her out of the whole run. Re-offered on every later training poll --
    # career_events.emit drops it the moment its once_key has fired, so the
    # normal turn-4 showing is unaffected.
    if training and int(ctx.turn or 0) > ACTING_DIRECTOR_TURN:
        late = next((b for b in beats_for(ACTING_DIRECTOR_TURN)
                     if b[0] == ACTING_DIRECTOR_EVENT), None)
        if late:
            out.append(CE.Event(
                event_id=late[0], story_id=late[1],
                play_timing=AFTER_TRAINING_TIMING,
                choices=[CE.Choice(effects=[])] if late[4] else [],
                chara_id=late[2] or None,
                once_key=f"unity_cup_beat:{late[0]}",
                priority=CE.PRIO_SCENARIO,
            ))
    # A turn-start beat opens the NEXT turn, so it trails the rest of the
    # chain: the capture's turn 2 runs 201025 -> 201114 -> 201103, with the
    # timing-1 Tutorial after the two timing-6 beats.
    out.sort(key=lambda e: e.play_timing == TURN_START_TIMING)
    return out


# The gate beat for each of the five rounds, in schedule order. RACE_GATE_EVENTS
# is a set (it is asked "is this a gate?"); this is the same five as a sequence,
# because re-offering a missed round has to pick the RIGHT one.
# Round four and round five each list their ORDINARY gate; the elite variant
# is a swap made at production time (ROUND_FOUR_GATE / FINALS_GATE), and a
# re-offer that named the elite id unconditionally would gate an ordinary run
# on a beat it never earned.
RACE_GATE_BY_ROUND = (201029, 201034, 201042, 201049, 201055)

# ------------------------------------------- what timing a race gate carries --
# PLAY_TIMING FOLLOWS THE RESPONSE, NOT THE ROUND. BEATS lists 10 for rounds
# one and two and 3 for rounds three, four and five, and that looked like a
# property of each round -- it is not. Lining every captured gate up against
# what the player did on that turn (both runs in captures/bot, nineteen gates,
# no exceptions):
#
#   turn 24, 36            training, then the gate     timing 10   (8 gates)
#   turn 48, 60, 72        race_entry -> race_start ->
#                          race_end -> race_out -> gate timing  3  (11 gates)
#
# The bot ALWAYS raced on 48/60/72 because those turns carry a scheduled race,
# which is the only reason the round and the timing appeared to be tied. A
# player who trains on turn 72 instead is a case the corpus simply never
# contains -- and with the gate stamped 3, servable_on refuses to put it on an
# exec_command, so it sat queued while the held turn re-served 72 forever
# (user-reported 2026-09-07, the third time on this turn; the previous fix
# turned "the client drops it" into "the server never offers it").
#
# 10 is what the same gate carries on turns 24 and 36 after a training, so it
# is not a guess: it is the timing the real server uses for exactly this
# situation one and two rounds earlier.
GATE_TIMING_TRAINING = 10
GATE_TIMING_RACE = 3


def gate_timing(training: bool) -> int:
    return GATE_TIMING_TRAINING if training else GATE_TIMING_RACE


def retime_race_gate(full_state: dict, endpoint: str = "") -> None:
    """Re-stamp a queued race gate that THIS kind of response cannot carry.

    The gate is produced on one response and can be served on a later one, so
    the timing chosen at production time can be wrong by the time it is
    offered -- and a gate the queue will not release is a frozen turn, not a
    missed cutscene. Only ever fires when the alternative is "never delivered":
    a timing already servable here is left exactly as it is.

    Repairs a save that is ALREADY stuck, too -- the stranded gate is in the
    queue, not in any producer's hands.
    """
    if not endpoint:
        return
    for row in CE.queued_rows(full_state):
        if int(row.get("event_id") or 0) not in RACE_GATE_EVENTS:
            continue
        if CE.servable_on(row, endpoint):
            continue
        log.info("unity cup: gate %s cannot ride a %s (play_timing %s) -- "
                 "re-stamping it %s", row.get("event_id"),
                 endpoint.rsplit("/", 1)[-1], row.get("play_timing"),
                 GATE_TIMING_TRAINING)
        row["play_timing"] = GATE_TIMING_TRAINING


def _gate_spent(ctx, index: int) -> bool:
    """Has this round's gate beat already PLAYED without the race happening?

    BEATS produces each gate once, keyed on its own event id, so a gate that
    reaches the client but never opens the race screen strands the round for
    good: the round is unrun, the key is spent, and the ordinary re-offer below
    is still waiting for the turn to be PAST the race turn -- which on turn 72
    it never is, because the hold freezes the served turn there. That is the
    finals softlock (2026-09-06): 201163 played, the race never opened, and the
    client re-offered the turn's goal race for ever.

    Spent-but-unrun is the one case where the re-offer must fire ON the race
    turn as well, so it is asked as a question about the KEY rather than about
    the turn."""
    gates = [RACE_GATE_BY_ROUND[index]]
    # ... and the elite finals plays under its own id (see unity_cup_beats).
    if gates[0] == FINALS_GATE[0]:
        gates.append(ZENITH_STRONG_GATE[0])
    for gate in gates:
        # STILL IN HAND IS NOT SPENT. On the race turn itself BEATS produces
        # the gate in this very poll and marks its key on the way out, so
        # asking the key alone would have this producer emit a SECOND gate
        # behind the first, and the round would play its cutscene twice.
        if CE.find(ctx.full_state, gate) is not None:
            return False
        if CE.already_fired(ctx.full_state, "unity_cup_beat:%d" % gate):
            return True
    return False


@CE.producer("unity_cup_missed_race", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def unity_cup_missed_race(ctx: CE.Ctx) -> list:
    """Re-offer a team race the career is past but never actually ran.

    The gate beats are turn-keyed (BEATS), so a round lost to a client crash on
    turn 36 could never come back: turn 37 has no gate, and the run reached
    graduation having raced fewer than five times. Its ladder then never moved
    and the graduation bonus was computed from a rank the team never had a
    chance to earn.

    One round at a time, earliest first, and only once the turn is genuinely
    past it -- ON the race turn the ordinary beat is already doing this job."""
    from . import impl
    st = impl.state(ctx.full_state)
    if not st.get("members"):
        return []
    run = len(st.get("races") or ())
    for index, race_turn in enumerate(impl.TEAM_RACE_TURNS):
        if index < run:
            continue                      # already raced
        if ctx.turn <= race_turn and not _gate_spent(ctx, index):
            break                         # not owed yet -- BEATS still has it
        event_id = RACE_GATE_BY_ROUND[index]
        story_id = next((b[1] for b in beats_for(race_turn)
                         if b[0] == event_id), 0)
        if (event_id, story_id) == FINALS_GATE and unity_cup.beat_elite(st):
            # The same swap unity_cup_beats makes -- a re-offered finals must
            # be the strengthened Zenith one for a run that earned it.
            event_id, story_id = ZENITH_STRONG_GATE
        # OWED UNTIL THE RACE IS RUN, NOT UNTIL A TURN PASSES.
        #
        # This key used to carry a turn number, so that a re-offer whose own
        # cutscene failed would come back on the next turn rather than strand
        # the round. On a HELD turn it cannot: the hold freezes the served
        # turn, the client echoes that frozen number back as current_turn, and
        # every turn this producer can see -- ctx.turn and ctx.advanced_turn
        # alike -- is pinned with it. The key never changes, so it is spent
        # after a single offer and the round is stranded for good. That is the
        # turn-72 infinite loop: the finals gate was offered once, resolved
        # without the race ever being banked, and no later poll could re-offer
        # it, so the hold held forever and every exec_command came back on
        # turn 72 (user-reported 2026-09-06).
        #
        # So the key is per round, and re-armed here for as long as the round
        # is genuinely still owed. Re-arming cannot pile the gate up: emit()
        # matches a once_key against the live queue and the active row too, so
        # an offer still on screen is refused whatever `fired` says.
        once_key = "unity_cup_missed_race:%d" % index
        # ...but NOT while the player is in the middle of racing it. The round
        # is not banked until team_race_end, so between opening the race screen
        # and finishing it the round still reads as owed -- offering the gate
        # there would interrupt the very race it is trying to start.
        if bool(st.get("pending")) or int(
                (ctx.chara_info or {}).get("playing_state") or 0) in (
                    impl.PLAYING_STATE_TEAM_RACE,
                    impl.PLAYING_STATE_TEAM_RACE_RUNNING,
                    impl.PLAYING_STATE_TEAM_RACE_RESULT):
            return []
        CE.unmark_fired(ctx.full_state, once_key)
        return [CE.Event(
            event_id=event_id,
            story_id=story_id,
            play_timing=TURN_START_TIMING,
            choices=[CE.Choice(effects=[])],
            once_key=once_key,
            priority=CE.PRIO_SCENARIO,
        )]
    return []


@CE.producer("unity_cup_power_up", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_power_up(ctx: CE.Ctx) -> list:
    """Award the next team_power level once the roster has earned it.

    ONE LEVEL AT A TIME. If a big training turn earns two levels at once the
    next one is emitted on the following poll, because each level has its own
    cutscene and the player is meant to see each -- and because team_power on
    the wire only moves when one is acknowledged, skipping a level would strand
    its event forever.

    The event is what moves team_power; impl.refresh_power deliberately does
    not. See UnityCup.on_event_resolved."""
    st = unity_cup.state(ctx.full_state)
    shown = int(st.get("power") or 1)
    level = shown + 1
    if unity_cup.earned_power(st) < level:
        return []
    beat = POWER_UP_EVENTS.get(level)
    if not beat:
        return []
    event_id, story_id = beat
    gain = POWER_UP_GAIN.get(level, 0)
    effects = [{"type": "all_stats", "value": "+%d" % gain}] if gain else []
    if level in ITS_ON_LEVELS:
        hint = (ITS_ON_HINT_LINKED
                if unity_cup.trainee_is_scenario_chara(ctx.chara_info)
                else ITS_ON_HINT)
        effects.append({"type": "skill_hint", "skill_id": ITS_ON_SKILL,
                        "value": "+%d" % hint})
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=POWER_UP_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key=f"{POWER_UP_KEY}:{level}",
        priority=CE.PRIO_SCENARIO,
    )]


# ------------------------------------------------------ "Growing as a Team" --
# THE FACILITY LEVEL-UP CUTSCENE, and it is on no schedule whatsoever. Both ids
# carry text_data 400002120 "Growing as a Team" and both used to sit in BEATS,
# on turns 18 and 72, which is where the player saw the second one play "right
# after winning the Unity Cup" with no facilities levelling and no animation
# (user-reported 2026-09-07).
#
# The corpus says what they really are. Across the two full runs in
# captures/bot, 201121 is served thirteen times on thirteen DIFFERENT turns
# (15,16,17,18,19,20,21,22,23,24,25,27,28,30,31,32,33,35,36,41,49,...,72 -- no
# two runs agree), and every single serving is followed, on the very next
# response, by chara_info.training_level_info_array gaining a level. Thirteen
# level-up moments, thirteen events, 1:1 with no exceptions either way. That is
# the whole rule: the team earns a facility level (impl.facility_levels, a pure
# function of the team's stat ranks), this beat announces it, and the number on
# the facility button moves when the beat is acknowledged.
#
# 201162 IS THE SAME BEAT AFTER THE UNITY CUP. It appears exactly once per run,
# on turn 73 in both, and it is the first level-up once the fifth team race is
# banked; 201121 never appears past turn 72. So the finals swap the id, the way
# beat_elite swaps the finals gate.
FACILITY_LEVEL_EVENT = (201121, 400002120)
FACILITY_LEVEL_EVENT_POST_CUP = (201162, 400002120)
FACILITY_LEVEL_TIMING = 11
FACILITY_LEVEL_KEY = "unity_cup_facility_level"


@CE.producer(FACILITY_LEVEL_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_facility_level(ctx: CE.Ctx) -> list:
    """Announce the facility levels the team has earned but not been shown.

    ONE BEAT PER BATCH, not per facility: the capture levels two and even three
    facilities behind a single serving (turn 49: power, guts and wit together),
    which is why the once_key is keyed on the level set rather than on a
    facility or a turn. A fresh set of levels re-arms it on its own.
    """
    st = unity_cup.state(ctx.full_state)
    if st.get("pending") or st.get("pending_award"):
        return []                       # a race is still on screen
    owed = unity_cup.facility_level_up_pending(st)
    if not owed:
        return []
    post_cup = len(st.get("races") or ()) >= len(unity_cup.TEAM_RACE_TURNS)
    event_id, story_id = (FACILITY_LEVEL_EVENT_POST_CUP if post_cup
                          else FACILITY_LEVEL_EVENT)
    signature = ",".join("%s:%s" % (cmd, owed[cmd]) for cmd in sorted(owed))
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=FACILITY_LEVEL_TIMING,
        choices=[CE.Choice(effects=[])],
        once_key="%s:%s" % (FACILITY_LEVEL_KEY, signature),
        priority=CE.PRIO_SCENARIO,
    )]


@CE.producer("unity_cup_team_name", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_team_name(ctx: CE.Ctx) -> list:
    """"A Team at Last" -- name the team.

    Not in BEATS because the choice list is the deck's, not a constant: the
    player picks from the scenario umas they actually brought, and Team Carrot
    is always the last option. The name is what the client shows on the team
    screen (it reads "Name Pending" until this resolves -- user-reported
    2026-09-06) AND what decides the gold skill the finals pay."""
    st = unity_cup.state(ctx.full_state)
    if unity_cup.team_named(st) or int(ctx.turn or 0) < TEAM_NAME_TURN:
        return []
    options = unity_cup.team_name_options(ctx.chara_info)
    event_id, story_id = (TEAM_NAME_EVENT_PICK if options
                          else TEAM_NAME_EVENT_FIXED)
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=TEAM_NAME_TIMING,
        # One per selectable name, Team Carrot last. The effects are empty --
        # the name itself is banked by the resolver, which is the only thing
        # that gets to see which choice was taken.
        choices=[CE.Choice(effects=[])
                 for _ in options + [unity_cup.TEAM_NAME_DEFAULT]],
        once_key="unity_cup_team_name",
        resolver=TEAM_NAME_RESOLVER,
        priority=CE.PRIO_SCENARIO,
    )]


@CE.resolver(TEAM_NAME_RESOLVER)
def _resolve_team_name(full_state, chara_info, ev, choice_number, **kwargs):
    """Bank the picked name.

    The options are re-derived from the deck rather than carried on the event:
    the deck cannot change mid-career, so the list the player was shown is the
    list this rebuilds, and the event survives a server restart with no extra
    state to persist."""
    options = unity_cup.team_name_options(chara_info) + [unity_cup.TEAM_NAME_DEFAULT]
    index = max(1, int(choice_number or 1)) - 1
    name_id = options[index] if index < len(options) else unity_cup.TEAM_NAME_DEFAULT
    return {"team_name_id": unity_cup.name_team(unity_cup.state(full_state), name_id)}


FINALS_PRESENT_HOLD = "unity_cup_finals_present"


@CE.hold_gate(FINALS_PRESENT_HOLD)
def _ending_chain_reached_present(full_state: dict) -> bool:
    """Is it this beat's turn in the career-ending chain yet?

    IT IS THE FOURTH BEAT, NOT THE SECOND. The capture's tail, all four runs
    (bot/20260905_152744 #0500-0505):

        race_out     [(101006, 3)]   "After the URA Finale Finals"
        check_event  [(201111, 3)]   "...: Victory!"
        check_event  [(10, 400000091, 3)]  "Twinkle Monthly Special Issue"
        check_event  [(201109, 3)]   "A Present from Director Akikawa!"
        check_event  [(6, 3)]        the trainee's own final event
        check_event  []

    Turn 78 alone is not enough to place it: this producer and
    unity_cup_finale_bookends both fire on the race_out poll, the queue is
    FIFO within a priority, and this one is registered first -- so the present
    was served immediately after 101006, two beats early, ahead of both the
    Victory beat and Twinkle Monthly (user-reported 2026-09-07).

    The two beats ahead of it live in DIFFERENT queues -- 201111 is a producer
    event, Twinkle is queued into single_mode_team's own ending chain -- so
    ordering them means waiting on both by name rather than sorting one list.
    The career-events queue wins the display slot over that chain, which is
    why holding this back until Twinkle is out of it lands the present between
    Twinkle and the trainee's ending, exactly where the capture has it.

    IT IS A SERVE-TIME HOLD, NOT A PRODUCER CONDITION, and that distinction is
    the whole bug this carries a note about. Producers are polled on race_out
    and exec_command only; every beat above plays on a check_event, which polls
    nothing. Asking this question inside the producer therefore asked it
    exactly once -- on the race_out that BUILDS the ending chain, where it is
    false by construction -- and the present was never produced at all
    (user-reported 2026-09-07, after the ordering fix above). As a hold gate it
    is re-checked on every drain, so the event is produced when it always was
    and simply waits its turn.
    """
    from ...handlers import single_mode_team as smt
    from ...handlers import single_mode_events
    if smt.ENDING_PAYOUT_KEY not in full_state:
        return False                  # the ending chain has not been built yet
    if not CE.already_fired(full_state, "unity_cup_beat:%d" % FINALE_VICTORY[0]):
        return False                  # "...: Victory!" still owed
    twinkle = smt._ENDING_CHAIN[0][0]
    return not any(
        int((e or {}).get("event_id") or 0) == twinkle
        for e in (full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or ()))


@CE.producer("unity_cup_finals_present", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_finals_present(ctx: CE.Ctx) -> list:
    """The endgame present: a gold hint for the team name's own skill.

    Paid only when the Unity Cup FINALS (round five) were won -- a career that
    lost them is served "Three Years of Hard Work!" with nothing attached, and
    that is exactly the shape of the 26 corpus runs that finished with no
    rarity-2 team-name tip at all."""
    if int(ctx.turn or 0) < FINALS_PRESENT_TURN:
        return []
    st = unity_cup.state(ctx.full_state)
    skill_id, level = unity_cup.team_name_reward(st)
    name_id = int(st.get("team_name_id") or 0) if skill_id else 0
    won_ura = _won_ura_finals(ctx)
    event_id, story_id = finals_present_beat(name_id, won_ura)
    effects = ([{"type": "skill_hint", "skill_id": skill_id, "value": level}]
               if skill_id else [])
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=FINALS_PRESENT_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key="unity_cup_finals_present",
        hold_until=FINALS_PRESENT_HOLD,
        priority=CE.PRIO_SCENARIO,
    )]


# ------------------------------------------- "Invitation to the URA Finale" --
# THE UNITY CUP HANDS OFF TO THE URA FINALE, and it does so unconditionally.
# Event 101008 / story 400000110 is URA's own invitation beat, and this
# scenario serves it the moment the fifth team race is settled -- the request
# right after team_race_out, which is where the finals pay-out lands.
#
# Corpus, all four captured finals (bot/20260905_152744 #0473-0474,
# bot/20260905_183334 #0365-0366, #0755-0756, #1141-1142), no exceptions:
#
#     ....._team_race_out    unchecked_event_array [(201056|201164, 9)]
#     ....._check_event      unchecked_event_array [(101008, 9)]
#     ....._check_event      []
#     ....._race_entry       [(101001, 2)]        <- the URA finale itself
#
# play_timing 9, choice_array [], show_clear 0, and story_id 400000110 is the
# FULL id -- single_mode_story_data.short_story_id is 0 for this row, unlike
# every 201xxx beat in BEATS, so there is no short form to send.
#
# It is not conditional on the result. All four captures won their finals, but
# the beat belongs to the URA Finale's own chain (which every scenario but
# Trackblazer runs), and the finals result is already told by 201056/201057/
# 201058. Withholding it left a Unity Cup career walking into the URA finale
# with no invitation at all (user-reported 2026-09-06).
URA_INVITE_EVENT = 101008
URA_INVITE_STORY = 400000110
URA_INVITE_KEY = "unity_cup_ura_invite"


@CE.producer(URA_INVITE_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_ura_invite(ctx: CE.Ctx) -> list:
    """URA's invitation beat, once the Unity Cup finals are behind us.

    Gated on the RACE HISTORY, not on the turn: a round lost to a client crash
    can be re-offered a turn or two late (see unity_cup_missed_race), and the
    invitation follows the fifth race wherever it actually lands. The pending
    block is checked too, so it cannot cut in front of a race the client is
    still watching."""
    st = unity_cup.state(ctx.full_state)
    if len(st.get("races") or ()) < len(unity_cup.TEAM_RACE_TURNS):
        return []
    if st.get("pending") or st.get("pending_award"):
        return []                       # the finals result has not settled yet
    return [CE.Event(
        event_id=URA_INVITE_EVENT,
        story_id=URA_INVITE_STORY,
        play_timing=POST_RACE_TIMING,
        choices=[],                     # a choice-less cutscene
        once_key=URA_INVITE_KEY,
        priority=CE.PRIO_SCENARIO,
    )]


# ------------------------------------- the URA finale's turn-78 bookends --
# TWO BEFORE AND TWO AFTER, and only the outer pair is shared with URA. The
# finale's third race is wrapped by this scenario's own finals_events pair
# (101005 pre / 101006 post, see UnityCup.finals_events) and then by two beats
# that belong to the Unity Cup alone. Capture order, all four runs:
#
#   race_entry   [(101005, 400000103, 2)]   "Before the URA Finale Finals"
#   check_event  [(201110, 400000107, 2)]   "...: Reunion"
#   ... the race ...
#   race_out     [(101006, 400000106, 3)]   "After the URA Finale Finals"
#   check_event  [(201111, 400000108, 3)]   "...: Victory!"
#   check_event  [(10|2, 400000091, 3)]     the shared ending chain
#
# KEYED ON THE RACE, NOT ON THE TURN. These used to sit in BEATS under turn 78,
# where both were produced by the turn's first poll -- so the Reunion could
# play before "Before the URA Finale Finals" and the Victory beat before the
# race that decides it. The pre beat is now held until the client is actually
# standing in finals round three (RACE_CTX_KEY, set at race_entry and cleared
# at race_out), and the post beat until that race is in the history.
FINALE_REUNION = (201110, 400000107)
FINALE_VICTORY = (201111, 400000108)
FINALE_ROUND = 3

# WHAT THE VICTORY BEAT PAYS: skill points, no stats, keyed on where the team
# finished on the Unity Cup ladder.
#
#   final team_rank 9 -> 85 SP   (bot/20260905_152744 #0501-0502,
#                                 bot/20260905_183334 #0392-0393)
#   final team_rank 2 -> 136 SP  (#0782-0783, #1166-1167)
#
# All four runs WON the finale race (result_rank 1) and all four carried the
# same 70% deck race bonus, so neither placement nor the bonus can be what
# separates 85 from 136 -- the ladder position is the only thing that differs,
# which is also how the player described it. Flat values, not scaled: 201109
# two beats later pays 10 stats / 50 SP in all four runs despite that same 70%,
# so career-event pay-outs do not take the race multiplier.
#
# THE BOUNDARY IS THE ONE GUESS. Only ranks 9 and 2 are in the corpus, and the
# two rank-2 runs are also the two that beat the elite team, so "top of the
# ladder" and "faced the strengthened Zenith" are indistinguishable here; the
# ladder reading is the one implemented. Anything from 4th down pays the
# observed 85.
FINALE_VICTORY_SP = ((3, 136), (30, 85))


def finale_victory_sp(team_rank: int) -> int:
    for ceiling, sp in FINALE_VICTORY_SP:
        if int(team_rank or 30) <= ceiling:
            return sp
    return FINALE_VICTORY_SP[-1][1]


def _in_finals_round_three(ctx: CE.Ctx) -> bool:
    """Whether the client is standing in the finale's third race right now."""
    from ...handlers import single_mode_team as smt
    race_ctx = ctx.full_state.get(smt.RACE_CTX_KEY) or {}
    return smt._finals_round_for_program(race_ctx.get("program_id")) == FINALE_ROUND


def _finals_round_three_raced(ctx: CE.Ctx) -> bool:
    from ...handlers import single_mode_team as smt
    for race in ctx.race_history or ():
        if isinstance(race, dict) and smt._finals_round_for_program(
                race.get("program_id")) == FINALE_ROUND:
            return True
    return False


@CE.producer("unity_cup_finale_bookends", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def unity_cup_finale_bookends(ctx: CE.Ctx) -> list:
    """"...: Reunion" before the finale's last race, "...: Victory!" after."""
    if _finals_round_three_raced(ctx):
        st = unity_cup.state(ctx.full_state)
        sp = finale_victory_sp(st.get("team_rank"))
        return [CE.Event(
            event_id=FINALE_VICTORY[0], story_id=FINALE_VICTORY[1],
            play_timing=3,
            choices=[CE.Choice(effects=[{"type": "skill_point",
                                         "value": "+%d" % sp}])],
            once_key="unity_cup_beat:%d" % FINALE_VICTORY[0],
            priority=CE.PRIO_SCENARIO,
        )]
    if not _in_finals_round_three(ctx):
        return []
    return [CE.Event(
        event_id=FINALE_REUNION[0], story_id=FINALE_REUNION[1],
        play_timing=2,
        choices=[CE.Choice(effects=[])],
        once_key="unity_cup_beat:%d" % FINALE_REUNION[0],
        priority=CE.PRIO_SCENARIO,
    )]


def _won_ura_finals(ctx: CE.Ctx) -> bool:
    """Whether the last race on record was won -- the URA finale on turn 78."""
    for race in reversed(ctx.race_history or ()):
        if isinstance(race, dict):
            return int(race.get("result_rank") or race.get("rank") or 0) == 1
    return False


@CE.producer("unity_cup_scout_join", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_scout_join(ctx: CE.Ctx) -> list:
    """"The Word Spreads" -- the first scouted teammate introduces herself.

    play_timing 6, so it is gated on a training poll exactly like the BEATS
    entry it replaces (the capture serves it from exec_command on turn 3, not
    from that turn's opening check_event)."""
    if not _is_training_poll(ctx):
        return []
    st = unity_cup.state(ctx.full_state)
    pending = unity_cup.unannounced_scouts(st)
    if not pending:
        return []
    member = pending[0]
    return [CE.Event(
        event_id=SCOUT_JOIN_EVENT,
        story_id=SCOUT_JOIN_STORY,
        play_timing=SCOUT_JOIN_TIMING,
        chara_id=member["chara_id"],
        support_card_id=unity_cup.scout_support_card(member.get("scout_id")),
        choices=[CE.Choice(effects=[])],
        once_key="unity_cup_scout_join",
        priority=CE.PRIO_SCENARIO,
    )]


def scout_join_events(st: dict) -> dict:
    """{event_id: target_id} for the join announcement currently on offer, so
    on_event_resolved can bank the teammate it introduced."""
    pending = unity_cup.unannounced_scouts(st)
    return {SCOUT_JOIN_EVENT: pending[0]["target_id"]} if pending else {}


def power_up_level(event_id) -> int:
    """The team_power level a "Team Power Increased" event awards, or 0."""
    for level, (eid, _story) in POWER_UP_EVENTS.items():
        if eid == event_id:
            return level
    return 0
