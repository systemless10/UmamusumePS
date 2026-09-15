"""GRAND LIVE (scenario 3) -- the mechanics: tokens, songs, lessons, Lives.

The scenario's implementation, not its seam. Shared career code NEVER imports
this module -- it goes through the GrandLive Scenario object in this package's
__init__.py, which is the only thing that reaches in here. `is_active()` below
survives for internal use and for the handful of places that still ask the
question directly; a new call to it from outside this package is a sign the
behaviour wants a hook in scenarios/base.py instead.

WIRE SHAPE (from UmaDumpy capture 20260728_183307, a full real Grand Live run)
---------------------------------------------------------------------------
A Grand Live response is byte-for-byte the URA response we already build, with
exactly one substitution: `ura_data_set` is replaced by `live_data_set`. Every
other field -- chara_info, home_info, not_up_parameter_info, race_*,
unchecked_event_array -- is identical, which is why the existing handlers serve
both scenarios unchanged and only `attach()` differs.

    live_data_set = {
      live_performance_info : the 5 performance tokens + their caps
      command_info_array    : per-facility TOKEN preview (parallel to home_info's
                              stat preview) -- [{command_type, command_id,
                              performance_inc_dec_info_array, params_inc_dec_info_array}]
      evaluation_info_array : the live members -- [{target_id, chara_id, member_state}]
      next_square_info_array: the THREE lesson offers, [{square_id, square_num 1..3}]
      master_live_id_array  : every song learned
      next_live_id_array    : songs learned since the last Live (bonus NOT yet active)
      effected_live_id_array: songs whose Live Bonus IS active
      live_result_array     : [{live_type, result_state}] per Live performed
      training_bonus_array  : [{target_type, effect_value}] accumulated Extra Stat Gain
      reserve_square_id     : the 予約 (reserved) lesson, 0 if none
      not_up_parameter_info : {performance_type_array} -- tokens sitting at cap
    }

ENDPOINTS the client uses that URA does not:
    single_mode_live/master_square  {square_id, current_turn}  -- take a lesson
    single_mode_live/live_start     {current_turn}             -- perform a Live

Beware when re-reading the capture: UmaDumpy's endpoint LABEL lags by one
exchange (the true endpoint of message N is the name on message N+1), on both
the filename and the `url` field. Taking the labels at face value says
live_start carries an event_id and that single_mode_live/start returns nothing
but a deck echo; both are wrong. See handoff.md.

THE LESSON BOARD
----------------
Three offers at a time. The board is EITHER three techniques OR three songs --
never mixed (verified across every next_square_info_array in the capture). Which
one you get is decided by the deterministic lesson pattern: N technique lessons
must be completed before the board flips to songs, N coming from a fixed
per-segment sequence that RESETS after every Live. See _PATTERN.

Squares come straight from master.mdb `single_mode_live_square`:
    square_type 1 (11xxx/12xxx/13xxx)  93 stat techniques (single / ranged / dual)
    square_type 2 (20xxx)             140 Group Lesson skill hints
    square_type 3 (30xxx)              15 energy recovery (3 tiers x 5 token colors)
    square_type 4 (40xxx)              21 songs
and their effects from `single_mode_live_master_bonus`:
    master_bonus_type 4 => this square is a SONG; master_bonus_type_value is its live_id
    gain_type 1 = stat      [stat, 1, value, 0]  or  [stat, 2, min, max] (ranged)
    gain_type 2 = hint      [3, tag, level, 0]
    gain_type 3 = energy    [value, 0, 0, 0]
    gain_type 5 = training bonus (Extra Stat Gain) [target_type, value, 0, 0]
with stat/target indices 1=speed 2=stamina 3=power 4=guts 5=wit 6=skill_point.

Numbers tagged KNOB below are reasoned defaults, not capture-derived ground
truth -- they are the ones to correct first if live play disagrees.
"""

from __future__ import annotations

import copy
import functools
import logging
import random

from ... import event_engine
from ... import master_data
from ..base import cap_bonus_for_scenario

log = logging.getLogger(__name__)

SCENARIO_ID = 3
STATE_KEY = "grand_live"

# ---------------------------------------------------------------- tokens ----
# performance_type on the wire.
DANCE, PASSION, VOCAL, VISUAL, MENTAL = 1, 2, 3, 4, 5
PERF_TYPES = (DANCE, PASSION, VOCAL, VISUAL, MENTAL)
PERF_NAMES = {DANCE: "dance", PASSION: "passion", VOCAL: "vocal",
              VISUAL: "visual", MENTAL: "mental"}

# EventChoiceRewardGainParam.effect_value_0 target ids for a Performance
# Points gain (dump.cs TrainingParamChangeUI.ParameterType: LivePerformanceDance
# 77 / Vocal 78 / Passion 79 / Visual 80 / Mental 81 -- the SAME numbering the
# training-preview screen uses for these categories, reused here for the event
# choice-reward display). NOT the internal 1-5 PERF_TYPES numbering above,
# which is purely our own token-state bookkeeping and never goes on the wire.
_PERF_WIRE_TARGET = {DANCE: 77, VOCAL: 78, PASSION: 79, VISUAL: 80, MENTAL: 81}


def perf_wire_target(perf_type: int) -> int:
    return _PERF_WIRE_TARGET.get(perf_type, 0)

START_TOKEN_CAP = 200
TOKEN_CAP_PER_LIVE = 50          # any Live success raises every cap by this
MAX_TOKEN_CAP = START_TOKEN_CAP + 4 * TOKEN_CAP_PER_LIVE   # 400 after all four

# THE PERFORMANCE-TOKEN FORMULA (user-supplied, exact):
#
#     tokens = floor( (S + F) * 1.15^C + 2L )
#
#       S  base by training type: Wit 5, every other facility 9
#       F  facility level (1 for level 1, 2 for level 2, ...)
#       C  support cards on the facility
#       L  scenario-link cards on the facility (these are ALSO counted in C)
#
# Checks out against both the supplied examples and the capture:
#   Speed  lvl1, C=0        -> (9+1) * 1     = 10   (capture: 10)
#   Wit    lvl1, C=0        -> (5+1) * 1     =  6   (capture: 6)
#   Speed  lvl1, C=2        -> 10 * 1.3225   = 13
#   Speed  lvl1, C=2, L=1   -> 13.225 + 2    = 15
#
# NOTE the shape: support count is EXPONENTIAL, not additive, and facility level
# feeds the base. An earlier additive model (base 10/6 plus 1 per support, 2 per
# link) reproduced the no-support case only because 9+1 and 5+1 happen to equal
# those bases at level 1 -- it drifted low as soon as a facility filled up or
# levelled.
TOKEN_BASE_WISDOM = 5
TOKEN_BASE_OTHER = 9
TOKEN_SUPPORT_BASE = 1.15      # ^C
TOKEN_PER_LINK = 2             # flat, on top

# Facility -> how its token colour is rolled (percentages, GameTora/kamigame's
# empirical 100-trial table). command_id 101 speed / 102 power / 103 guts /
# 105 stamina / 106 wisdom, matching the URA facility ids.
_TOKEN_DISTRIBUTION = {
    101: ((DANCE, 65), (PASSION, 1), (VOCAL, 3), (VISUAL, 25), (MENTAL, 6)),
    105: ((DANCE, 6), (PASSION, 56), (VOCAL, 33), (VISUAL, 2), (MENTAL, 3)),
    102: ((DANCE, 4), (PASSION, 1), (VOCAL, 66), (VISUAL, 1), (MENTAL, 28)),
    103: ((DANCE, 35), (PASSION, 6), (VOCAL, 3), (VISUAL, 55), (MENTAL, 1)),
    106: ((DANCE, 0), (PASSION, 34), (VOCAL, 2), (VISUAL, 2), (MENTAL, 62)),
}
TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)   # the capture's serve order
# Summer-camp facility ids map back onto their base facility.
CAMP_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}

# Scenario-link characters: +2 tokens each instead of +1.
#
# CAPTURE-DERIVED, not from the guide. chara_info.evaluation_info_array in the
# Grand Live run gains exactly these five as scenario members -- 1046 on turn 4
# with the first "Supporters" beat, the rest arriving with the "New Supporters!"
# beat after each Live. An earlier version of this set was transcribed from the
# guide's gold-skill lyric list (which names Mihono Bourbon and Agnes Tachyon)
# and was simply wrong for this purpose.
LINK_CHARA_IDS = frozenset({
    9008,   # Light Hello -- the scenario NPC herself
    1002,   # Silence Suzuka
    1014,   # El Condor Pasa
    1024,   # Mayano Top Gun
    1046,   # Smart Falcon
    1066,   # Twin Turbo
})

# ------------------------------------------------------------------ lives ----
# turn -> live_type, straight from master single_mode_live_live_data.
# chara_info.playing_state THE CLIENT USES TO OPEN THE BACKSTAGE SCREEN.
#
# This is the whole trigger, and nothing else is: 1 = home/training, 2 = racing,
# 5 = mid-turn event chain, and 10 = the Live screen (the Anticipation gauge, the
# Lessons button and the concert button). The capture's turn 24 sets 10 the
# moment "Concert Begins" resolves, holds it across all three backstage
# master_square calls, and drops it when live_start answers.
#
# An earlier theory -- that an EMPTY event chain after Concert Begins was the
# cue -- was wrong: the live log showed the chain running dry exactly as
# intended and the client going straight back to exec_command, because
# playing_state was still 1.
PLAYING_STATE_BACKSTAGE = 10
PLAYING_STATE_NORMAL = 1
# 5 = "this turn's event chain is still running". The capture's live_start
# response carries 5, and every post-live check_event on that turn keeps it,
# dropping to 1 only once the turn actually changes. Answering the concert with
# 1 says "the turn is over", which is not true yet -- the Concert Ends beat and
# the after-the-live beats are still to come.
PLAYING_STATE_EVENT_CHAIN = 5

LIVE_TURNS = {24: 1, 36: 2, 48: 3, 60: 4, 72: 10}
GRAND_LIVE_TYPE = 10
GREAT_SUCCESS_SONGS = 3          # master's great_success_num, all five rows
RESULT_GREAT, RESULT_NORMAL = 2, 1

SP_PER_SONG = 25
SP_PER_TECHNIQUE = 5
LIVE_STATS_GREAT = 10            # 3+ songs in the segment
LIVE_STATS_NORMAL = 3            # 0-2 songs
LIVE_FANS = 1000                 # every concert, user-specified

# The Grand Concert's own reward tier -- user-supplied 2026-08-16. It does NOT
# use the segment-song great/normal split above: its "special version" is
# gated on TOTAL songs learned across the whole run (countable_songs), not
# songs learned since the last concert, and pays out far more than any of the
# first four concerts.
GRAND_CONCERT_SONG_GATE = 18
GRAND_CONCERT_STATS_SPECIAL = 15
GRAND_CONCERT_STATS_NORMAL = 12
GRAND_CONCERT_FANS_SPECIAL = 9000
GRAND_CONCERT_FANS_NORMAL = 2000
# Gold/white pair, confirmed off skill_data (group_id 21007): 210071 "I Wanna
# Win with You" is rarity=2, 210072 "On the Way to Our Dream" is rarity=1 --
# same shape as Closer Together's pairs, so the grant looks its rarity up via
# skill_rarity() rather than assuming it from this name, too.
GRAND_CONCERT_SKILL_SPECIAL = 210071
GRAND_CONCERT_SKILL_NORMAL = 210072

# Free songs nothing charges you for. Make Debut! arrives with the scenario's
# lesson-unlock event; Girls' Legend U is awarded by "Our Song" in late Senior
# and pointedly does NOT count toward the 18-song gate that event itself gates.
MAKE_DEBUT_LIVE_ID = 1006
GIRLS_LEGEND_U_LIVE_ID = 1029
FREE_LIVE_IDS = (MAKE_DEBUT_LIVE_ID, GIRLS_LEGEND_U_LIVE_ID)

# Song gates (Senior). Checked at the END of the named turn's command.
SONG_GATE_GOLD_SKILL = (65, 16)     # early Nov, >=16 songs -> "Closer Together"
SONG_GATE_SPECIAL_LIVE = (71, 18)   # early Dec, >=18 songs -> special Grand Live

# ---------------------------------------------------------------- lessons ----
# How many TECHNIQUE lessons must be completed before the board flips to songs.
# Index = songs already taken this segment; once the initial run is exhausted
# the loop repeats. The counter resets completely after every Live -- that reset
# is the mechanic the whole scenario is scheduled around.
_PATTERN = {
    1: ((1, 2, 3), (4, 4, 2, 2)),      # Junior, before the 1st Promotional Live
    2: ((2, 2, 2), (4, 5, 2, 2)),      # before the 2nd
    3: ((2, 2, 2), (4, 5, 2, 2)),      # before the 3rd
    4: ((2, 2, 2), (4, 5, 2, 2)),      # before the 4th
    5: ((2, 2, 2), (4, 3, 2, 2)),      # before the Grand Live
}
BOARD_SIZE = 3
LESSON_UNLOCK_TURN = 5

# Which songs may be offered, keyed by how many Promotional Lives are done.
# Not derivable from master (every lesson song is level 1 there); transcribed
# from the scenario's song table and cross-checked against every song square
# the capture was actually offered -- no song appeared before its group.
_SONG_UNLOCK = {
    0: (1038, 1044, 1040, 1003, 1057, 1042, 1047, 1046),
    1: (1023, 1032, 1011),
    2: (1012, 1043, 1045, 1034),
    3: (1024, 1020, 1039, 1021, 1014, 1041),
}

# master_bonus stat index (single_mode_live_master_bonus's own numbering).
STAT_SPEED, STAT_STAMINA, STAT_POWER, STAT_GUTS, STAT_WIT, STAT_SP = 1, 2, 3, 4, 5, 6
_STAT_BY_INDEX = {STAT_SPEED: "speed", STAT_STAMINA: "stamina", STAT_POWER: "power",
                  STAT_GUTS: "guts", STAT_WIT: "wiz", STAT_SP: "skill_point"}

# CORRECTION 2026-08-16: training_bonus_array's target_type is NOT translated
# to the params_inc_dec_info_array convention -- a real capture
# (UmaDumpy dumps/20260728_183307, master_square response #179) shows the
# wire array itself as [{target_type:1,...}, {target_type:4,...},
# {target_type:6, effect_value:5}], i.e. skill points genuinely goes out as
# target_type 6, master_bonus's own native index, unconverted. A prior fix
# here assumed (without checking a real capture first) that this array must
# follow the same 1-5/30 convention every OTHER wire array uses, and
# "corrected" 6 to 30 -- which was wrong, and broke a field that was already
# correct. Left as a straight passthrough of the master_bonus index; see
# build_live_data_set below.

# master_bonus_gain_type
GAIN_STAT, GAIN_HINT, GAIN_ENERGY, GAIN_TRAINING_BONUS = 1, 2, 3, 5
# The stat-gain "mode" in master_bonus_gain_value_N_2.
STAT_MODE_FLAT, STAT_MODE_RANGE = 1, 2
# master_bonus_type
MB_SONG = 4

SQUARE_TECHNIQUE_TYPES = (1, 2, 3)
SQUARE_SONG_TYPE = 4


# ============================================================= master data ==

def is_active(chara_info) -> bool:
    """The single discriminator. Everything in this module is gated on it, so a
    URA career never touches Grand Live code."""
    return bool(chara_info) and int(chara_info.get("scenario_id") or 0) == SCENARIO_ID


@functools.lru_cache(maxsize=1)
def _squares() -> dict:
    """square_id -> {type, cost{perf_type: value}, bonus}. Read once."""
    out = {}
    for r in master_data.query("SELECT * FROM single_mode_live_square"):
        d = dict(r)
        cost = {}
        for i in range(1, 6):
            t, v = d[f"perf_type_{i}"], d[f"perf_value_{i}"]
            if t and v:
                cost[int(t)] = cost.get(int(t), 0) + int(v)
        out[int(d["id"])] = {
            "id": int(d["id"]), "type": int(d["square_type"]), "cost": cost,
            "master_bonus_id": int(d["master_bonus_id"]),
        }
    return out


@functools.lru_cache(maxsize=1)
def _master_bonuses() -> dict:
    """master_bonus_id -> {song_live_id | None, gains[(type, values)]}."""
    out = {}
    for r in master_data.query("SELECT * FROM single_mode_live_master_bonus"):
        d = dict(r)
        gains = []
        for k in (1, 2, 3):
            t = d[f"master_bonus_gain_type_{k}"]
            if t:
                gains.append((int(t), [int(d[f"master_bonus_gain_value_{k}_{j}"])
                                       for j in range(1, 5)]))
        out[int(d["id"])] = {
            "song_live_id": (int(d["master_bonus_type_value"])
                             if int(d["master_bonus_type"]) == MB_SONG else None),
            "gains": gains,
        }
    return out


@functools.lru_cache(maxsize=1)
def _song_squares() -> dict:
    """live_id -> square_id, for every song that can be BOUGHT in a lesson."""
    out = {}
    for sid, sq in _squares().items():
        if sq["type"] != SQUARE_SONG_TYPE:
            continue
        live_id = _master_bonuses().get(sq["master_bonus_id"], {}).get("song_live_id")
        if live_id:
            out[live_id] = sid
    return out


@functools.lru_cache(maxsize=1)
def _song_bonuses() -> dict:
    """live_id -> (live_bonus_type, live_bonus_value). 1=Specialty Rate,
    2=Support Event Chance, 3=Friendship Bonus."""
    return {int(r["live_id"]): (int(r["live_bonus_type"]), int(r["live_bonus_value"]))
            for r in master_data.query("SELECT * FROM single_mode_live_song_list")}


def _is_offerable_square(sq: dict) -> bool:
    """False for two master-data rows that exist but the real game apparently
    never offers (user-confirmed 2026-08-16, cross-checked against a Technique
    reference table that lists only flat stat gains and never a bare, aptitude-
    less hint):

      - GAIN_HINT whose tag matches no real skill. Every REAL 'Group Lesson
        Basics' variant carries a specific aptitude tag (201/202/203/204/502,
        ...) that _hint_skill_ids resolves to real skills; the generic
        'Group Lesson' rows (square_id 20000-20004) carry tag 1, which
        matches nothing -- confirmed by directly querying every tag this
        table uses. Buying one silently granted no hint at all (live-
        reported: 'sometimes when buying some skills nothing gets given').
      - GAIN_STAT in STAT_MODE_RANGE ('Speed +3 to 7' style). The reference
        table's Specific Stat tier is a flat +5/+8/+12 at cost 10/16/24 --
        exactly the FLAT-mode squares already in this table under a
        different id -- and never mentions a displayed range. The ranged
        squares share the same stat/cost SHAPE at different numbers (cost
        8/12/18) with no match anywhere in the reference, and _ranged_gain's
        own history (see its docstring) already established the displayed
        range is not what the flat squares pay -- this excludes the range
        squares from the offer pool entirely rather than reconciling two
        conflicting cost tables."""
    bonus = _master_bonuses().get(sq["master_bonus_id"])
    if not bonus:
        return True
    for gain_type, vals in bonus["gains"]:
        if gain_type == GAIN_HINT and not _hint_skill_ids(vals[1]):
            return False
        if gain_type == GAIN_STAT and vals[1] == STAT_MODE_RANGE:
            return False
    return True


# TECHNIQUE AVAILABILITY -- user-supplied reference table (2026-09-03):
#
#   Before First Concert   SP +5,  Stat +5
#   Before Fourth Concert  SP +8,  Stat +8,  Stat +4 & SP +4,  2 Stats +4
#   Before Grand Concert   SP +12, Stat +12, Stat +6 & SP +6,  2 Stats +6
#   Skill Hints and Energy Techniques are ALWAYS available.
#
# Keyed off the GAIN VALUE rather than a square-id range, so the tiers are
# derived from master data instead of transcribed: a single-stat square (one
# GAIN_STAT -- the "Specific Stat" and "Skill Points" rows, which are the same
# square shape with stat index 6) pays 5/8/12, a pair square (two GAIN_STAT --
# both "Stat & SP" and "2 Stats") pays 4/6.
#
# The tiers REPLACE each other, they do not accumulate: the table lists a
# complete set per stage, and the whole point of the mechanic is that concerts
# upgrade what the board offers.
#
# Note what this drops: master carries a THIRD pair tier at +8 (square ids
# 13201-13215, 16+16 tokens) that appears nowhere in the reference table, so it
# is now never offered. Before this, every tier was in one flat pool from turn
# 5 -- a +12 square was reachable before the first concert (live-reported).
_TIER_SINGLE = {1: 5, 2: 8, 3: 12}
_TIER_PAIR = {2: 4, 3: 6}


def _tier_for_segment(segment: int) -> int:
    """Concerts performed -> which technique tier the board offers. _segment is
    1 before the first concert and 5 heading into the Grand Concert, so the
    middle three segments (before the 2nd, 3rd and 4th) share tier 2 -- which
    is what the table's "Before Fourth Concert" row spans."""
    if segment <= 1:
        return 1
    return 2 if segment <= 4 else 3


@functools.lru_cache(maxsize=1)
def _stat_squares_by_tier() -> dict:
    """tier -> the stat/SP technique square ids offered at that tier."""
    out = {1: [], 2: [], 3: []}
    for sid, sq in _squares().items():
        if sq["type"] not in SQUARE_TECHNIQUE_TYPES or not _is_offerable_square(sq):
            continue
        gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
        stat_gains = [v for t, v in gains if t == GAIN_STAT]
        if not stat_gains or len(stat_gains) != len(gains):
            continue                      # hint / energy squares, handled below
        values = {v[2] for v in stat_gains}
        if len(values) != 1:
            continue                      # no such row today; never guess a tier
        value = values.pop()
        table = _TIER_SINGLE if len(stat_gains) == 1 else _TIER_PAIR
        tier = next((t for t, val in table.items() if val == value), None)
        if tier:
            out[tier].append(sid)
    return {t: tuple(sorted(v)) for t, v in out.items()}


def _square_gain_types(sq: dict) -> set:
    gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
    return {t for t, _v in gains}


@functools.lru_cache(maxsize=1)
def _energy_squares() -> tuple:
    return tuple(sorted(sid for sid, sq in _squares().items()
                        if sq["type"] in SQUARE_TECHNIQUE_TYPES
                        and _is_offerable_square(sq)
                        and GAIN_ENERGY in _square_gain_types(sq)))


@functools.lru_cache(maxsize=1)
def _hint_squares() -> tuple:
    return tuple(sorted(sid for sid, sq in _squares().items()
                        if sq["type"] in SQUARE_TECHNIQUE_TYPES
                        and _is_offerable_square(sq)
                        and GAIN_HINT in _square_gain_types(sq)))


def _hint_square_suits(sq: dict, chara_info: dict | None) -> bool:
    """Whether a Group Lesson square may be OFFERED to this trainee.

    The aptitude rule already existed but only at GRANT time (_grant_hint
    returns None for a tag the trainee has no A in) -- so the square still
    appeared on the board and the player could spend 15-30 tokens on it and be
    given nothing (live-reported 2026-09-03: hints must only appear for
    aptitudes the trainee has an A in). Screening the OFFER is the fix; the
    grant-time check stays as a backstop for a board rolled before this.

    chara_info None (an event-granted song refreshing the board with no trainee
    to hand) keeps every hint square rather than silently emptying the pool."""
    if chara_info is None:
        return True
    gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
    for gain_type, vals in gains:
        if gain_type != GAIN_HINT:
            continue
        field = _hint_apt_field(vals[1])
        if field and int(chara_info.get(field) or 0) < _HIGH_APTITUDE_GRADE:
            return False
    return True


def _offered_squares(st, chara_info: dict | None) -> list:
    """The technique pool the board draws from: this segment's stat tier, plus
    the always-available energy and (aptitude-suited) hint squares."""
    tier = _tier_for_segment(_segment(st))
    pool = list(_stat_squares_by_tier().get(tier, ()))
    pool += list(_energy_squares())
    pool += [sid for sid in _hint_squares()
             if _hint_square_suits(_squares()[sid], chara_info)]
    return pool


@functools.lru_cache(maxsize=1)
def scenario_cap_bonus() -> dict:
    """Grand Live's per-stat career cap boost, straight from
    single_mode_scenario. Row 3 is (speed 400, stamina 100, power 100, guts 300,
    wiz 100) -- i.e. the 1600/1300/1300/1500/1300 ceiling the scenario is known
    for. URA's row is a flat 200 across the board, which is exactly the constant
    the code used to hardcode."""
    return cap_bonus_for_scenario(SCENARIO_ID)


# ================================================================== state ===

def _blank() -> dict:
    return {
        "tokens": {str(t): 0 for t in PERF_TYPES},
        "token_cap": START_TOKEN_CAP,
        "songs": [],                 # live_ids owned, in acquisition order
        "pending_songs": [],         # learned since the last Live (bonus dormant)
        "active_songs": [],          # Live Bonus currently in effect
        "training_bonus": {},        # str(target_type) -> accumulated +N
        "board": [],                 # the three offered square_ids
        "reserve_square_id": 0,
        "lives_done": [],            # [{"live_type": n, "result_state": 1|2}]
        "segment_songs": 0,          # songs taken since the last Live
        "segment_techniques": 0,     # techniques taken since the last Live
        "techniques_since_song": 0,  # the lesson-pattern counter
        "unlocked": False,           # lessons available ("Bring Back the Grand Concert!")
        "unlocked_turn": 0,          # the turn that event fired on
        # The Live's setlist stays in `pending_songs` for the rest of the Live's
        # turn (it IS next_live_id_array); this records the turn after which it
        # moves into active_songs and the Live Bonuses switch on.
        "activate_after_turn": 0,
        # Same deferral for the +50 token cap: the official live_start response
        # still reports the pre-Live cap.
        "raise_cap_after_turn": 0,
    }


def state(full_state: dict) -> dict:
    """The Grand Live sub-state, created on demand. Callers must save_state."""
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():          # forward-compat for older careers
        st.setdefault(k, v)
    return st


def reset(full_state: dict) -> None:
    """Called on career start. Also clears the state for a URA start, so a
    Grand Live run never leaks its songs/tokens into the next career."""
    full_state[STATE_KEY] = _blank()


def _tokens(st) -> dict:
    return {int(k): int(v) for k, v in (st.get("tokens") or {}).items()}


def _set_tokens(st, tokens: dict) -> None:
    cap = st.get("token_cap", START_TOKEN_CAP)
    st["tokens"] = {str(t): max(0, min(cap, int(tokens.get(t, 0)))) for t in PERF_TYPES}


# =============================================================== the board ==

def _segment(st) -> int:
    """Which of the five lesson segments we're in: 1 before the first Live,
    5 heading into the Grand Live."""
    return min(len(st.get("lives_done") or ()) + 1, 5)


def _songs_needed(st) -> int:
    """Techniques required before the board flips to songs, for the NEXT song
    of this segment."""
    initial, loop = _PATTERN[_segment(st)]
    n = st.get("segment_songs", 0)
    if n < len(initial):
        return initial[n]
    return loop[(n - len(initial)) % len(loop)]


def _available_songs(st) -> list:
    """Songs unlocked by the Lives done so far and not already learned."""
    done = len(st.get("lives_done") or ())
    owned = set(st.get("songs") or ())
    out = []
    for tier in range(0, min(done, 3) + 1):
        out += [s for s in _SONG_UNLOCK.get(tier, ()) if s not in owned]
    return [s for s in out if s in _song_squares()]


def _roll_board(st, rng=None, chara_info: dict | None = None) -> list:
    """Three offers: all songs when the pattern counter is satisfied, otherwise
    all techniques. Never mixed -- verified against every next_square_info_array
    in the capture.

    chara_info gates the technique pool -- see _offered_squares (this segment's
    tier plus the always-available energy/hint squares, hints screened by the
    trainee's own aptitudes)."""
    r = rng or random
    if st.get("techniques_since_song", 0) >= _songs_needed(st):
        songs = _available_songs(st)
        if songs:
            picks = r.sample(songs, min(BOARD_SIZE, len(songs)))
            return [_song_squares()[s] for s in picks]
        # Every unlocked song already learned -- fall through to techniques so
        # the board is never empty.
    pool = _offered_squares(st, chara_info)
    return list(r.sample(pool, min(BOARD_SIZE, len(pool))))


def ensure_board(st, rng=None, chara_info: dict | None = None) -> list:
    if not st.get("board"):
        st["board"] = _roll_board(st, rng, chara_info)
    return st["board"]


def reroll_board(st, chara_info: dict | None = None, keep: int = 0,
                 rng=None) -> list:
    """A fresh board that KEEPS the reserved offer (`keep`), for
    single_mode_live/lottery_square. _roll_board picks all three at once and has
    no notion of holding one back, so re-roll and then splice the kept square
    into its original slot, dropping whichever new pick collided with it.

    A `keep` of 0, or one the re-roll happens to return anyway, degrades to a
    plain _roll_board."""
    fresh = _roll_board(st, rng, chara_info)
    keep = int(keep or 0)
    if not keep or keep in fresh:
        return fresh
    old = [int(s) for s in (st.get("board") or [])]
    slot = old.index(keep) if keep in old else 0
    slot = min(slot, len(fresh) - 1) if fresh else 0
    if not fresh:
        return [keep]
    fresh[slot] = keep
    return fresh


def can_afford(st, square_id: int) -> bool:
    sq = _squares().get(int(square_id))
    if not sq:
        return False
    have = _tokens(st)
    return all(have.get(t, 0) >= v for t, v in sq["cost"].items())


# ============================================================ applying one ==

def _hint_skill_ids(tag: int) -> tuple:
    """Skills carrying a Group Lesson's tag (103 = Late Surger, 203 = Medium,
    ...). skill_data.tag_id is a SLASH-separated token list ('201/303/403'),
    so match on tokens rather than substring -- '103' must not match '1103'."""
    return _hint_skill_ids_cached(int(tag))


@functools.lru_cache(maxsize=64)
def _hint_skill_ids_cached(tag: int) -> tuple:
    want = str(tag)
    out = []
    for r in master_data.query(
            "SELECT id, tag_id FROM skill_data "
            "WHERE disable_singlemode=0 AND rarity=1 AND tag_id LIKE ?",
            (f"%{want}%",)):
        # tag_id's delimiter is '/', not whitespace -- str.split() with no
        # argument splits on whitespace, so it never actually broke the
        # string apart ('201/303/403'.split() == ['201/303/403'], one token,
        # never equal to `want`). Every tag lookup silently returned zero
        # matches, so EVERY Group Lesson's skill hint -- not just the unused
        # "Group Lesson" (tag 1) squares -- granted nothing (live-reported:
        # "sometimes when buying some skills nothing gets given").
        if want in str(r["tag_id"] or "").split("/"):
            out.append(int(r["id"]))
    return tuple(sorted(out))


def skill_rarity(skill_id: int) -> int:
    """The skill's REAL rarity from skill_data, not a caller's assumption.

    Closer Together's "None" (no linked character) choice grants skill 200501
    ("Lane Legerdemain") with rarity hardcoded to 1 (the "white" slot it was
    filed under) -- but skill_data actually has it at rarity=2 (gold); its
    group-mate 200502 ("Go with the Flow") is the real rarity=1 skill. Tagging
    it rarity=1 on the wire made the client render the WRONG skill's name for
    the group (live-reported: picking Lane Legerdemain showed "Go with the
    Flow" instead). Gold/white pairs share a group_id, and the client keys off
    (group_id, rarity) to pick which of the two to display -- so the rarity
    sent must be the skill's own, always looked up here rather than assumed
    from which "slot" (gold/white) a caller thinks it fills."""
    row = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (int(skill_id),))
    return int(row["rarity"]) if row else 1


def add_skill_tip(chara_info: dict, skill_id: int, level: int, rarity: int = 1) -> None:
    """Raise a skill hint by `level`, in the REAL wire shape -- confirmed off a
    real capture (chara_info.skill_tips_array: {group_id, rarity, level}, e.g.
    group_id 20127 appearing TWICE with rarity 1 and 2 for the same skill's
    white/gold pair). The previous shape here, {skill_id, level}, was wrong on
    every field (wrong key, no rarity, and not group-scoped -- skill_id 200711
    and 200712, gold/white of the same skill, group to 20071 by //10 and must
    be tracked as separate entries by rarity, not merged as one). Every
    lesson-hint grant was silently malformed on the wire because of this."""
    group_id = int(skill_id) // 10
    tips = chara_info.setdefault("skill_tips_array", [])
    for t in tips:
        if t.get("group_id") == group_id and t.get("rarity") == rarity:
            t["level"] = min(5, t.get("level", 0) + int(level))
            return
    tips.append({"group_id": group_id, "rarity": rarity, "level": max(1, int(level))})



# Group Lesson square tags ARE aptitude categories (confirmed exhaustively --
# every real GAIN_HINT tag on the board is one of these, plus dummy tag 1
# which _offer already excludes): 101-104 running style, 201-204 distance,
# 502 dirt ground (501/turf exists in the scheme but no real square uses it).
# Maps a tag straight to the matching chara_info aptitude field.
_STYLE_APT_FIELD = {101: "proper_running_style_nige", 102: "proper_running_style_senko",
                    103: "proper_running_style_sashi", 104: "proper_running_style_oikomi"}
_DISTANCE_APT_FIELD = {201: "proper_distance_short", 202: "proper_distance_mile",
                       203: "proper_distance_middle", 204: "proper_distance_long"}
_GROUND_APT_FIELD = {501: "proper_ground_turf", 502: "proper_ground_dirt"}
_HIGH_APTITUDE_GRADE = 7  # A (7) or S (8) on the 1-8 G..S scale


def _hint_apt_field(tag: int):
    """The chara_info aptitude field a Group Lesson tag is gated on, or None
    for a tag that is not an aptitude category."""
    return (_STYLE_APT_FIELD.get(tag) or _DISTANCE_APT_FIELD.get(tag)
            or _GROUND_APT_FIELD.get(tag))


def _grant_hint(chara_info: dict, tag: int, level: int, rng=None) -> int | None:
    """Raise the hint level of a random (white, rarity=1) skill in this tag
    group that the trainee doesn't already own. Returns the skill id, or None
    if nothing applied.

    User-directed 2026-08-20: a Group Lesson square only teaches something
    the trainee has HIGH aptitude for (A/S grade) -- landing a Nige
    specialist on a Late Surger lesson, or a turf runner on the Dirt one,
    now grants nothing, same no-op as the existing 'no real skill matches
    this tag' case _offer already screens most of (dummy tag 1) -- this
    covers the rest (tags 101-104/201-204/502, real squares an unsuited
    trainee can still land on since _offer only filters by whether the tag
    resolves to real skills at all, not by whose aptitude it suits)."""
    apt_field = _hint_apt_field(tag)
    if apt_field and int(chara_info.get(apt_field) or 0) < _HIGH_APTITUDE_GRADE:
        return None
    r = rng or random
    candidates = _hint_skill_ids(tag)
    if not candidates:
        return None
    owned = {int(s.get("skill_id")) for s in (chara_info.get("skill_array") or [])
             if s.get("skill_id")}
    pool = [s for s in candidates if s not in owned]
    if not pool:
        return None
    skill_id = r.choice(pool)
    add_skill_tip(chara_info, skill_id, level, rarity=1)
    return skill_id


def _ranged_gain(vals, token_cost: int) -> int:
    """The real gain of a "Speed +5 to 9"-style square.

    It is NOT a roll. A technique always pays HALF its token cost in stats
    (user-reported and true of the whole table): the 60 flat squares satisfy
    gain == cost/2 exactly, and for all 18 ranged ones -- every one of which
    costs a single token -- cost/2 lands inside the printed range and is the
    value the game actually grants: Da8 -> +4, Da12 -> +6, Da18 -> +9.

    The min..max in master is what the CLIENT prints, not what it pays; rolling
    it uniformly (the old behaviour) overpaid or underpaid nearly every time.
    Clamped to the printed range so an unexpected cost can't produce a value the
    player was never shown."""
    half = int(token_cost) // 2
    return max(vals[2], min(vals[3], half))


def apply_square(st, chara_info: dict, square_id: int, rng=None) -> dict:
    """Spend the tokens and apply the square's effect. Returns a summary of what
    happened (for logging/tests). Raises ValueError if unaffordable/unknown, so
    the handler can refuse rather than silently give it away."""
    r = rng or random
    square_id = int(square_id)
    sq = _squares().get(square_id)
    if sq is None:
        raise ValueError(f"unknown square {square_id}")
    if not can_afford(st, square_id):
        raise ValueError(f"cannot afford square {square_id}")

    have = _tokens(st)
    for t, v in sq["cost"].items():
        have[t] = have.get(t, 0) - v
    _set_tokens(st, have)

    bonus = _master_bonuses().get(sq["master_bonus_id"], {"gains": [], "song_live_id": None})
    result = {"square_id": square_id, "song": None, "stats": {}, "energy": 0,
              "hint": None, "training_bonus": {}}

    spent = sum(sq["cost"].values())
    for gain_type, vals in bonus["gains"]:
        if gain_type == GAIN_STAT:
            stat = _STAT_BY_INDEX.get(vals[0])
            if not stat:
                continue
            amount = vals[2] if vals[1] == STAT_MODE_FLAT else _ranged_gain(vals, spent)
            result["stats"][stat] = result["stats"].get(stat, 0) + amount
        elif gain_type == GAIN_HINT:
            result["hint"] = _grant_hint(chara_info, vals[1], vals[2], r)
        elif gain_type == GAIN_ENERGY:
            result["energy"] += vals[0]
        elif gain_type == GAIN_TRAINING_BONUS:
            key = str(vals[0])
            st["training_bonus"][key] = st["training_bonus"].get(key, 0) + vals[1]
            result["training_bonus"][key] = vals[1]

    _apply_stats(chara_info, result["stats"])
    if result["energy"]:
        chara_info["vital"] = min(chara_info.get("max_vital", 100),
                                  chara_info.get("vital", 0) + result["energy"])

    song = bonus["song_live_id"]
    if song:
        _learn_song(st, song)
        result["song"] = song
        st["segment_songs"] = st.get("segment_songs", 0) + 1
        st["techniques_since_song"] = 0
    else:
        st["segment_techniques"] = st.get("segment_techniques", 0) + 1
        st["techniques_since_song"] = st.get("techniques_since_song", 0) + 1

    if st.get("reserve_square_id") == square_id:
        st["reserve_square_id"] = 0
    st["board"] = _roll_board(st, r, chara_info)   # refreshes after every lesson
    return result


def _apply_stats(chara_info: dict, stats: dict) -> list:
    """Stat gains, honouring the trainee's caps and the universal SOFT_CAP
    halving above 1200. Skill points are uncapped and never halved.

    Used to reimplement its own cap-clamped add here instead of calling
    event_engine.add_stat -- which does the exact same clamp AND the 1200
    halving every other gain path (events, hints, appraisals, duels, races,
    inspirations) already goes through. That left Grand Live's own gains (the
    concert payouts, Make Debut!/Girls' Legend U's mastery bonuses) as the one
    path where a stat past 1200 kept gaining at full value instead of half
    (live-reported: "the +15 is bypassing the half rule past 1200").

    Returns the not_up_parameter_info codes it owes -- the stats it tried to
    raise that were ALREADY capped. None of these gains goes through a choice,
    so event_engine.not_up_info never sees them and the card stayed silent
    instead of saying "<stat> is in superb form"; the callers hand them to the
    response with event_engine.stash_not_up."""
    owed = event_engine.capped_stat_codes(
        chara_info, [s for s, a in stats.items()
                     if s != "skill_point" and (a or 0) > 0])
    for stat, amount in stats.items():
        if not amount:
            continue
        if stat == "skill_point":
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + amount
            continue
        event_engine.add_stat(chara_info, stat, amount)
    return owed


def _learn_song(st, live_id: int) -> None:
    live_id = int(live_id)
    if live_id in (st.get("songs") or ()):
        return
    st.setdefault("songs", []).append(live_id)
    # A song's Live Bonus is DORMANT until the next Live fires it. This is the
    # single most misunderstood thing in the scenario, and the reason a bonus
    # bought in the final segment is worth nothing.
    st.setdefault("pending_songs", []).append(live_id)


def grant_free_song(full_state: dict, live_id: int, turn: int = 0,
                    chara_info: dict = None) -> bool:
    """Make Debut! / Girls' Legend U -- awarded by an event, no token cost.
    Each carries its own Mastery Bonus (user-supplied 2026-08-16 reference
    table): Make Debut! is All Performance Points +10 (a token grant, no
    chara_info needed); Girls' Legend U is All Stats +10 (a real stat grant,
    which is why chara_info is now a parameter here)."""
    st = state(full_state)
    if int(live_id) in (st.get("songs") or ()):
        return False
    _learn_song(st, live_id)
    if int(live_id) == MAKE_DEBUT_LIVE_ID:
        st["unlocked"] = True
        st["unlocked_turn"] = int(turn or 0)
        # Make Debut!'s Mastery Bonus is "All Performance Points +10".
        have = _tokens(st)
        _set_tokens(st, {t: have.get(t, 0) + 10 for t in PERF_TYPES})
        ensure_board(st, chara_info=chara_info)
    elif int(live_id) == GIRLS_LEGEND_U_LIVE_ID and chara_info is not None:
        # Girls' Legend U's Mastery Bonus is "All Stats +10" -- was granting
        # only the song itself (live-reported 2026-08-16). apply_square's own
        # GAIN_STAT path never runs for a free/event-granted song at all, so
        # this needed its own grant here, same as Make Debut!'s tokens above.
        event_engine.stash_not_up(full_state, _apply_stats(
            chara_info, {s: 10 for s in
                         ("speed", "stamina", "power", "guts", "wiz")}))
    return True


def grant_lowest_performance(full_state: dict, chara_info: dict, amount: int) -> int | None:
    """+amount Performance Points to whichever of the 5 categories currently
    sits lowest (ties broken by PERF_TYPES order) -- user-confirmed: Light
    Hello's support card event "Another Day's Hard Work!" grants +20 to the
    lowest category, unlike Make Debut!'s flat all-categories +10 above.
    GameTora's own effect code for this is "mt" -- absent from gametora.
    _CODE_TYPE (no other event carries it), so event_engine.py handles the
    raw code directly rather than through the usual named-type mapping.
    Returns the category granted, or None if this run isn't even Grand Live
    (state() would otherwise silently seed a Grand Live sub-state onto a URA
    career that will never read it -- an event from a pal/friend card can be
    present in a URA deck too, so this can't assume the scenario)."""
    if not is_active(chara_info):
        return None
    st = state(full_state)
    tokens = _tokens(st)
    target = min(PERF_TYPES, key=lambda t: tokens.get(t, 0))
    tokens[target] = tokens.get(target, 0) + int(amount)
    _set_tokens(st, tokens)
    return target


def tokens_visible(full_state: dict, turn) -> bool:
    """Whether the training screen shows token icons yet.

    Not simply "unlocked": the capture has 0 of 5 facilities showing tokens on
    turn 4 -- the very turn the unlock event fires and Make Debut! lands -- and
    5 of 5 from turn 5. So they appear the turn AFTER the unlock, not on it."""
    st = state(full_state)
    if not st.get("unlocked"):
        return False
    unlocked_turn = int(st.get("unlocked_turn") or 0)
    return not unlocked_turn or int(turn or 0) > unlocked_turn


# ================================================================= tokens ===

def token_gain(command_id: int, support_chara_ids=(), facility_level: int = 1) -> int:
    """floor((S + F) * 1.15^C + 2L) -- see the constants above.

    `support_chara_ids` must be ONLY real support cards standing in the facility;
    scenario NPCs (Tazuna, the Director, the Reporter) are not support cards and
    must not inflate C."""
    is_wit = CAMP_BASE.get(command_id, command_id) == 106
    s = TOKEN_BASE_WISDOM if is_wit else TOKEN_BASE_OTHER
    f = max(1, int(facility_level or 1))
    ids = list(support_chara_ids or ())
    c = len(ids)
    l = sum(1 for chara_id in ids if chara_id in LINK_CHARA_IDS)
    return int((s + f) * (TOKEN_SUPPORT_BASE ** c) + TOKEN_PER_LINK * l)


def _roll_token_types(command_id: int, rainbow: bool, rng=None) -> list:
    """Which colour(s) this facility pays out. Friendship (rainbow) training
    yields TWO types instead of one -- roughly double throughput, and the reason
    the whole scenario reduces to 'unlock rainbows early, then never stop'."""
    r = rng or random
    dist = _TOKEN_DISTRIBUTION.get(CAMP_BASE.get(command_id, command_id))
    if not dist:
        return []
    types = [t for t, _ in dist]
    weights = [w for _, w in dist]
    if not any(weights):
        return []
    first = r.choices(types, weights=weights)[0]
    if not rainbow:
        return [first]
    rest = [(t, w) for t, w in dist if t != first and w]
    if not rest:
        return [first]
    second = r.choices([t for t, _ in rest], weights=[w for _, w in rest])[0]
    return [first, second]


def token_preview(command_id: int, support_chara_ids=(), rainbow: bool = False,
                  rng=None, facility_level: int = 1) -> list:
    """performance_inc_dec_info_array for one facility. Both colours of a
    rainbow training carry the SAME value (capture-verified)."""
    value = token_gain(command_id, support_chara_ids, facility_level)
    return [{"performance_type": t, "value": value}
            for t in _roll_token_types(command_id, rainbow, rng)]


def award_tokens(full_state: dict, preview: list) -> dict:
    """Bank the tokens the client was shown for the facility just trained. Reads
    the SAME preview the training screen rendered, so what you see is what you
    get -- the identical rule the stat gains already follow."""
    st = state(full_state)
    have = _tokens(st)
    for entry in preview or ():
        t = int(entry.get("performance_type") or 0)
        if t in PERF_TYPES:
            have[t] = have.get(t, 0) + int(entry.get("value") or 0)
    _set_tokens(st, have)
    return st["tokens"]


# live_bonus_type in single_mode_live_song_list.
LIVE_BONUS_SPECIALTY = 1      # 得意率アップ  -- raises the specialty (rainbow) rate
LIVE_BONUS_SUPPORT_EVENT = 2  # サポート連続イベント率 -- support chain event rate
LIVE_BONUS_FRIENDSHIP = 3     # 友情ボーナス -- the big one


def live_bonuses(full_state: dict) -> dict:
    """The Live Bonuses currently IN EFFECT, summed over every activated song.

    Returns {"friendship": pct, "specialty": pts, "support_event": pts}.

    These were tracked and reported on the wire but never actually applied --
    `_song_bonuses()` existed and nothing called it, so buying a Friendship
    Bonus song changed the number on the info screen and nothing else
    (live-reported: "the actual buffs dont seem to apply").

    Only songs in `active_songs` count: a song's bonus is dormant until the Live
    after the one it was bought in, which is why a bonus bought in the final
    segment is worth nothing."""
    st = state(full_state)
    bonuses = _song_bonuses()
    out = {"friendship": 0, "specialty": 0, "support_event": 0}
    for live_id in (st.get("active_songs") or ()):
        kind, value = bonuses.get(int(live_id), (0, 0))
        if kind == LIVE_BONUS_FRIENDSHIP:
            out["friendship"] += value
        elif kind == LIVE_BONUS_SPECIALTY:
            out["specialty"] += value
        elif kind == LIVE_BONUS_SUPPORT_EVENT:
            out["support_event"] += value
    return out


def friendship_bonus_pct(full_state: dict) -> int:
    """Total Friendship Bonus % in effect. Caps at +65 in a perfect run (7 songs
    at 5% plus 3 at 10%, Girls' Legend U included)."""
    return live_bonuses(full_state)["friendship"]


def training_bonus_for(full_state: dict) -> dict:
    """Extra Stat Gain from songs, as {stat_name: +N}. It behaves as a
    `statBonus` term -- added to the facility base BEFORE every multiplier --
    which is why a +2 is worth far more than 2 and why these songs are early
    buys."""
    st = state(full_state)
    out = {}
    for key, val in (st.get("training_bonus") or {}).items():
        stat = _STAT_BY_INDEX.get(int(key))
        if stat and val:
            out[stat] = out.get(stat, 0) + int(val)
    return out


# ================================================================== lives ===

def live_turn_type(turn: int):
    return LIVE_TURNS.get(int(turn or 0))


def pending_live_for(full_state: dict, turn) -> int | None:
    """The live_type live_start should perform for this reported turn, or None.

    Tolerant of a ONE-TURN overshoot on purpose. Our exec_command advances
    chara_info.turn as soon as the command runs (24 -> 25), while the official
    server holds it at 24 until the turn's whole event chain finishes -- so by
    the time the client asks for the concert we may already be reporting 25.
    A strict `LIVE_TURNS[turn]` lookup then found nothing, silently performed no
    Live, paid nothing, and returned no Concert Ends event: live-reported as
    "the concert was a success instead of a great success" (no result at all,
    so the client showed its default) and "no event plays after".

    The window is deliberately just {turn, turn - 1}: enough to absorb that
    off-by-one, narrow enough that it can never reach forward to a Live the
    player has not got to yet."""
    done = {r.get("live_type") for r in (state(full_state).get("lives_done") or ())}
    for candidate in (int(turn or 0), int(turn or 0) - 1):
        live_type = LIVE_TURNS.get(candidate)
        if live_type is not None and live_type not in done:
            return live_type
    return None


def live_turn_of(live_type: int):
    """The scheduled turn for a live_type -- the inverse of LIVE_TURNS."""
    for turn, lt in LIVE_TURNS.items():
        if lt == live_type:
            return turn
    return None


def songs_learned(full_state: dict) -> int:
    return len(state(full_state).get("songs") or ())


def countable_songs(full_state: dict) -> int:
    """The count the 16/18-song gates use. Girls' Legend U is excluded -- it is
    awarded by the very event the 18-count gates."""
    return len([s for s in (state(full_state).get("songs") or ())
                if s != GIRLS_LEGEND_U_LIVE_ID])


def perform_live(full_state: dict, chara_info: dict, turn: int) -> dict:
    """Run the Live scheduled for this turn: pay out, activate every dormant
    Live Bonus, raise the token caps, and reset the lesson pattern.

    Returns the result summary (also used to build the event payload)."""
    st = state(full_state)
    live_type = live_turn_type(turn)
    if live_type is None:
        return {}

    # The segment's song count is len(pending_songs), NOT the segment_songs
    # counter. pending_songs holds every song learned since the last Live --
    # including the FREE ones, and "automatically-gained songs count toward the
    # gauge". The counter was only bumped by apply_square, so a run whose third
    # song was Make Debut! showed a MAXED gauge and still scored a plain success
    # (live-reported). Two sources of truth, one of them wrong.
    songs = len(st.get("pending_songs") or ())
    techniques = st.get("segment_techniques", 0)
    skill = None
    if live_type == GRAND_LIVE_TYPE:
        great = countable_songs(full_state) >= GRAND_CONCERT_SONG_GATE
        stat_gain = GRAND_CONCERT_STATS_SPECIAL if great else GRAND_CONCERT_STATS_NORMAL
        fans = GRAND_CONCERT_FANS_SPECIAL if great else GRAND_CONCERT_FANS_NORMAL
        skill = GRAND_CONCERT_SKILL_SPECIAL if great else GRAND_CONCERT_SKILL_NORMAL
    else:
        great = songs >= GREAT_SUCCESS_SONGS
        stat_gain = LIVE_STATS_GREAT if great else LIVE_STATS_NORMAL
        fans = LIVE_FANS
    result_state = RESULT_GREAT if great else RESULT_NORMAL

    sp = songs * SP_PER_SONG + techniques * SP_PER_TECHNIQUE
    # RECORDED, not applied. The client renders each event card by diffing the
    # served state against the previous response, so anything spent here -- during
    # live_start, before the first card exists -- is invisible: the payout had
    # already landed by the time "The First Concert Ends!" was drawn. It is paid
    # when THAT beat resolves instead (career_producers._resolve_grand_live_beat).
    st["pending_payout"] = {"stat": stat_gain, "sp": sp, "fans": fans, "skill": skill}

    # The segment's songs STAY in pending (next_live_id_array) for now -- that
    # array is the Live's SETLIST, and the capture still shows all four in it on
    # the live_start response and on every response for the rest of the turn,
    # emptying only once the turn changes. Clearing it here left the concert with
    # nothing to perform.
    #
    # They are marked to activate instead: their Live Bonus comes online from the
    # next turn, which is also when the wire moves them into effected_live_id_array.
    activated = list(st.get("pending_songs") or ())
    st["activate_after_turn"] = int(turn or 0)

    # song_ids/songs_owned: NOT used by the concert itself -- read back out at
    # career-finish time (trained_chara.py's grand_live_lives_done) by
    # missions.py's WatchSpecificSong (600006, cv1=live_id -- is THIS song in
    # any performed setlist), WatchUniqueConcerts (600033, count of distinct
    # live_ids across every setlist ever performed) and SongsBeforeFirst
    # Concert (100075 -- lives_done[0]'s songs_owned, since `songs` already
    # includes everything _learn_song has added up through this segment).
    st.setdefault("lives_done", []).append(
        {"live_type": live_type, "result_state": result_state,
         "song_ids": sorted(activated), "songs_owned": len(st.get("songs") or ())})
    # ANY success raises every token cap; a Great Success additionally raises
    # the trainee's stat caps.
    # The cap rise is DEFERRED to the end of the Live's turn, like the song
    # activation: the official live_start response still reports the old cap
    # (200, not 250). Raising it during the concert made every token bar read
    # against a ceiling the client was not showing yet.
    if live_type != GRAND_LIVE_TYPE:
        st["raise_cap_after_turn"] = int(turn or 0)
    if great:
        _raise_stat_caps(chara_info)

    # The pattern resets completely: whatever progress was made toward the next
    # song is destroyed, which is why entering a Live mid-pattern is a real loss.
    st["segment_songs"] = 0
    st["segment_techniques"] = 0
    st["techniques_since_song"] = 0
    st["board"] = _roll_board(st, chara_info=chara_info)

    return {"live_type": live_type, "result_state": result_state, "great": great,
            "songs": songs, "techniques": techniques, "skill_point": sp,
            "stat_gain": stat_gain, "activated": activated}


# KNOB: Great Success on a Promotional Live raises stat caps. The scenario is
# documented as doing this but not by how much; +10 per stat per Great Success
# is a conservative reading (max +40 across the four promotional Lives).
GREAT_SUCCESS_CAP_GAIN = 10


def _raise_stat_caps(chara_info: dict) -> None:
    for stat in ("speed", "stamina", "power", "guts", "wiz"):
        key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        if key in chara_info:
            chara_info[key] = int(chara_info[key]) + GREAT_SUCCESS_CAP_GAIN


# ====================================================== the wire structure ==

# Moved to master_data -- they are plain master.mdb reads that several unrelated
# features need, not Grand Live mechanics. Re-exported here so this module reads
# the same as before.
support_card_chara = master_data.support_card_chara
is_trainee_chara = master_data.is_trainee_chara


# member_state 1 = "has joined your cause" -- NOT "is a trainee character".
#
# This was originally read off a single late-career capture, where every uma in
# the array happened to be a 1 and every NPC a 0, and was implemented as a
# chara_id band test. Sweeping all 199 captured evaluation_info_arrays by turn
# disproves that flat: on turn 1 EVERY row is 0, including the trainee herself.
# Rows flip to 1 as the scenario recruits them --
#
#   turn 1   all 12 rows member_state 0
#   turn 4   trainee (0, 1056) -> 1, and Smart Falcon arrives as a NEW row
#   turn 5   the owned uma deck cards (1, 2, 4, 5) -> 1
#   turn 24+ the remaining deck card and the scripted scenario supporters
#
# The band test lit every uma up from turn 1, so the array never CHANGED across
# responses -- and the client renders "<name> joined your cause!" by diffing the
# served state between responses. That is why no join line ever appeared
# (live-reported). Recruitment state is the only input here now.
def _member_state(chara_id, seated: set) -> int:
    return int(int(chara_id or 0) in seated)


def apply_live_payout(full_state: dict, chara_info: dict) -> dict:
    """Pay the Live recorded by perform_live. Called when the "Concert Ends!"
    beat RESOLVES, so the client sees the stats/SP/fans arrive on that card."""
    st = state(full_state)
    payout = st.pop("pending_payout", None)
    if not payout:
        return {}
    event_engine.stash_not_up(full_state, _apply_stats(
        chara_info, {s: payout["stat"] for s in
                     ("speed", "stamina", "power", "guts", "wiz")}))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + payout["sp"]
    chara_info["fans"] = chara_info.get("fans", 0) + payout["fans"]
    skill_id = payout.get("skill")
    if skill_id:
        add_skill_tip(chara_info, skill_id, 1, rarity=skill_rarity(skill_id))
    return payout


def add_supporter(full_state: dict, chara_id: int) -> bool:
    """Seat one scenario supporter. Returns False if already seated, so a beat
    replayed through both the queue and the blanket check_event hook recruits
    her exactly once."""
    st = state(full_state)
    seated = st.setdefault("supporters", [])
    if int(chara_id) in seated:
        return False
    seated.append(int(chara_id))
    return True


def settle_lives(full_state: dict) -> bool:
    """Raise every token cap and seat the Live's supporters. Called when the
    "New Supporters!" beat RESOLVES -- both belong on THAT card, not on the
    concert's own. Returns whether anything was pending."""
    st = state(full_state)
    if not st.get("raise_cap_after_turn"):
        return False
    st["token_cap"] = min(MAX_TOKEN_CAP,
                          st.get("token_cap", START_TOKEN_CAP) + TOKEN_CAP_PER_LIVE)
    st["raise_cap_after_turn"] = 0
    _set_tokens(st, _tokens(st))           # re-clamp (the cap only ever grows)
    st["lives_settled"] = int(st.get("lives_settled") or 0) + 1
    return True


def _settle_activation(st: dict, turn) -> None:
    """Once the Live's turn is behind us, move its setlist from pending into
    active -- the songs leave next_live_id_array, land in effected_live_id_array,
    and their Live Bonuses switch on.

    Deferred rather than done inside perform_live because next_live_id_array is
    the setlist the concert performs; emptying it at performance time left the
    Live with no songs."""
    # The cap rise and the supporters are NOT settled here. They are paid by the
    # "New Supporters!" beat's resolver, because the client attributes a change
    # to whichever card it first sees it on -- and this ran a full turn earlier,
    # putting all five cap raises on "The First Concert Ends!" instead.
    #
    # FALLBACK ONLY: if that beat never played (a chain the player skipped, or a
    # Live with no post-live beats at all), settle a full turn late so the caps
    # are never silently lost.
    cap_after = int(st.get("raise_cap_after_turn") or 0)
    if cap_after and int(turn or 0) > cap_after + 1:
        st["token_cap"] = min(MAX_TOKEN_CAP,
                              st.get("token_cap", START_TOKEN_CAP) + TOKEN_CAP_PER_LIVE)
        st["raise_cap_after_turn"] = 0
        _set_tokens(st, _tokens(st))       # re-clamp (the cap only ever grows)
        st["lives_settled"] = int(st.get("lives_settled") or 0) + 1

    after = int(st.get("activate_after_turn") or 0)
    if not after or int(turn or 0) <= after:
        return
    moving = list(st.get("pending_songs") or ())
    if moving:
        st["active_songs"] = list(st.get("active_songs") or ()) + moving
    st["pending_songs"] = []
    st["activate_after_turn"] = 0
    if moving:
        log.info("grand live: live bonuses now active for %s", moving)


def _live_members(chara_info: dict, full_state: dict = None) -> list:
    """evaluation_info_array: the TRAINEE at target_id 0, then one row per
    support card (target_id = deck position), then the fixed scenario NPCs,
    then any recruited supporter who is not already one of those rows.

    Every row is present from turn 1; what changes over the run is member_state.
    A supporter already on the board (the trainee, a deck uma) is recruited by
    FLIPPING her existing row -- she must not also gain a target_id == chara_id
    row, or the client would stage her twice. Only supporters with no row yet
    (Smart Falcon, the scripted scenario umas) get one appended."""
    from . import producers as career_producers
    seated = {int(c) for c in career_producers.supporters_for(full_state or {})}

    out = []
    trainee = int(chara_info.get("card_id") or 0) // 100      # trainee card IS /100
    if trainee:
        out.append({"target_id": 0, "chara_id": trainee,
                    "member_state": _member_state(trainee, seated)})
    for card in (chara_info.get("support_card_array") or []):
        chara_id = support_card_chara(card.get("support_card_id") or 0)
        if chara_id:
            out.append({"target_id": int(card.get("position") or 0),
                        "chara_id": chara_id,
                        "member_state": _member_state(chara_id, seated)})
    # Light Hello (9008) fills the one gap in this band (105) -- present
    # unconditionally from turn 1, same as the other five: this row is just
    # her IDENTITY registration (member_state stays 0 forever, like the rest,
    # so it never fires "X joined your cause"). WHEN she's actually allowed to
    # show up training is a completely different flag on a completely
    # different array -- see single_mode_events.NPC_UNLOCKS's 202002 entry,
    # which flips chara_info.evaluation_info_array[105].is_appear (the real
    # "X can now appear in training" mechanism already used for Director/
    # Reporter). An earlier pass gated THIS row on the turn-4 unlock instead,
    # which wired her through the wrong system entirely and fired "joined your
    # cause" instead of "can now appear in training" (user-corrected
    # 2026-08-20).
    for target_id, chara_id in ((101, 9001), (102, 9002), (103, 9003),
                                (104, 9004), (105, 9008), (106, 9006)):
        out.append({"target_id": target_id, "chara_id": chara_id, "member_state": 0})

    seen = {r["chara_id"] for r in out}
    for chara_id in career_producers.supporters_for(full_state or {}):
        if chara_id not in seen:
            seen.add(chara_id)
            out.append({"target_id": chara_id, "chara_id": chara_id,
                        "member_state": 1})
    return out


def build_live_data_set(full_state: dict, chara_info: dict,
                        command_info_array=None) -> dict:
    """The live_data_set block. `command_info_array` is the per-facility TOKEN
    preview; pass None on responses that have no training screen (races, the
    Live itself) and it degrades to empty previews rather than lying."""
    st = state(full_state)
    _settle_activation(st, chara_info.get("turn"))
    ensure_board(st, chara_info=chara_info)
    tokens = _tokens(st)
    cap = st.get("token_cap", START_TOKEN_CAP)

    perf = {PERF_NAMES[t]: tokens.get(t, 0) for t in PERF_TYPES}
    perf.update({f"max_{PERF_NAMES[t]}": cap for t in PERF_TYPES})

    if command_info_array is None:
        # RE-SERVE THE LAST PREVIEW, don't invent an empty one. Real carries five
        # POPULATED rows on every response kind, races and the Live included
        # (21/25 race_entry, 21/25 race_out, 11/11 live_start, all with a
        # non-empty performance_inc_dec_info_array); five rows of empty arrays
        # tell the client every facility pays zero tokens.
        #
        # The cached roll is also the only thing here that knows the right
        # COMMAND IDS: summer camp serves 601-605, and the hardcoded fallback
        # below is wrong for the eight camp turns. Whatever the training screen
        # last showed is right by construction.
        from . import preview as _preview
        cached = (full_state.get(_preview.PREVIEW_KEY) or {}).get("commands")
        command_info_array = copy.deepcopy(cached) if cached else [
            {"command_type": 1, "command_id": cmd,
             "performance_inc_dec_info_array": [], "params_inc_dec_info_array": []}
            for cmd in TRAINING_COMMAND_IDS]

    return {
        "live_performance_info": perf,
        "command_info_array": list(command_info_array),
        "evaluation_info_array": _live_members(chara_info, full_state),
        "next_square_info_array": [{"square_id": sid, "square_num": i + 1}
                                   for i, sid in enumerate(st.get("board") or ())],
        "master_live_id_array": sorted(st.get("songs") or ()),
        "next_live_id_array": sorted(st.get("pending_songs") or ()),
        "effected_live_id_array": sorted(st.get("active_songs") or ()),
        "not_up_parameter_info": {
            "performance_type_array": [t for t in PERF_TYPES if tokens.get(t, 0) >= cap]},
        # Wire shape is exactly {live_type, result_state} -- the song_ids/
        # songs_owned we also keep on each lives_done entry are internal
        # bookkeeping for the mission readers documented at _perform_live, and
        # real responses never carry them. Project, don't deepcopy.
        "live_result_array": [{"live_type": int(r.get("live_type") or 0),
                               "result_state": int(r.get("result_state") or 0)}
                              for r in (st.get("lives_done") or ())],
        "reserve_square_id": int(st.get("reserve_square_id") or 0),
        "training_bonus_array": [
            {"target_type": int(k), "effect_value": int(v)}
            for k, v in sorted((st.get("training_bonus") or {}).items(),
                               key=lambda kv: int(kv[0])) if v],
    }


def attach(response: dict, full_state: dict, chara_info: dict,
           command_info_array=None, endpoint: str = "") -> dict:
    """Swap URA's ura_data_set for Grand Live's live_data_set on a response.

    Reached as the scenario's attach hook, from the ONE place every career
    response passes through, so no handler needs to know the scenario exists."""
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    data.pop("ura_data_set", None)
    data.pop("team_data_set", None)
    data["live_data_set"] = build_live_data_set(full_state, chara_info,
                                                command_info_array)
    if endpoint.endswith("/live_start"):
        # On the Live's OWN response the just-performed Live is listed FIRST,
        # with the earlier ones after it in order -- the client reads index 0 as
        # "the Live you just did" for its result screen. Every other response
        # lists them chronologically. Both verified across all five Lives.
        results = data["live_data_set"].get("live_result_array") or []
        if len(results) > 1:
            data["live_data_set"]["live_result_array"] = results[-1:] + results[:-1]
    return response

