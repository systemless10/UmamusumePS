"""
Dynamic career-event engine.

The real URA career fires hundreds of events -- character story events, support
card events (one per card in the deck, firing "by coincidence" across the run),
and outing/date events. Each has one to three choices, and each choice applies a
set of effects (stats / energy / mood / skill points / skill hints / bond /
conditions). Capturing every one is infeasible, so this module serves them all
DYNAMICALLY:

  * STRUCTURE (which events exist, their story ids and titles) comes from
    master.mdb (text_data category 181 for titles; the story_id encodes the
    source -- 8<card><nnn> for a support card, 50<chara><nnn> for a character).
  * OUTCOMES (the choices and their effects) come from the datamined event
    database under server/data/events/*.json. That data was verified to match
    the real captures EXACTLY -- every captured event (Chasing Brilliance,
    Status-Boring?, Leave It to the Roomie, the chara outings, ...) resolves to
    the same title and the same effects the live server sent. master.mdb does
    NOT store choice outcomes, so the datamined DB is the authoritative source
    for them.

An event is looked up by (source id, normalized title): a support-card event by
its card_id, a character event by the trainee's card_id. The choices are turned
into the wire structures the client expects -- an unchecked_event_array entry
(one choice_array slot per choice), a get_choice_reward preview
(choice_reward_array: every choice's gains, both branches for a random one), and
an apply step that mutates chara_info on commit.

Effect -> display encoding (gain_param_array, decoded from real captures):
  display 1  param up     ev0 target (1-5 stat, 10 energy, 20 mood, 30 SP,
                          51-55 caps), ev1 amount
  display 2  param down   same targets
  display 4  bond up      ev0 char_id, ev1 amount
  display 6  skill hint   ev0 skill_id, ev1 level
  display 9  good status  ev0 condition id
  display 14 all stats up ev1 amount
  display 35 bond down    ev0 char_id
  display 37 bad status   ev0 condition id
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import re

from . import gametora
from . import master_data
from .handlers import conditions
from .handlers import single_mode_events

log = logging.getLogger("uma-server")

# Conditions (chara_effect ids) that are GOOD -> shown with the good-status icon
# (display 9); everything else is a debuff (display 37). Event-granted conditions
# are usually good (Practice Perfect, Charming, Fast Learner, Hot Topic, ...).
_GOOD_CONDITIONS = frozenset({7, 8, 9, 10, 11, 13})

# Wire event ids (from the real captures): the client accepts these generic ids
# for any dynamically-served choice event of that kind.
SUPPORT_EVENT_ID = 10002        # support-card CHAIN/story events (story 8<card>nnn)
SUPPORT_RANDOM_EVENT_ID = 20000  # support-card RANDOM events (story 80<chara>nnn)
CHARA_EVENT_ID = 6000            # trainee outing / date events (story 50<chara>nnn)
CAREER_EVENT_IDS = frozenset({SUPPORT_EVENT_ID, SUPPORT_RANDOM_EVENT_ID, CHARA_EVENT_ID})


# THE SHORT STORY ID -- what actually goes on the wire.
#
# single_mode_story_data carries a story TWICE on the same row: `story_id` (a
# static reference id) and, when non-zero, `short_story_id` -- the copy that
# sits in the trainee's own CHRONOLOGICAL chain. text_data has a title under
# both, so a beat looks duplicated: Kitasan Black's "Summer Camp (Year 2)
# Begins!" is 501068103 AND 501068409, Special Week's is 501001103 and
# 501001406. Only the suffix of the short one is per-trainee.
#
# The real server serves the SHORT id, with no exceptions: across the three
# 20260905 sessions it served 249 distinct stories and NOT ONE of them was a
# long id that has a short. _find_race_story_ids already reached the same
# conclusion the long way round for race beats ("ALWAYS has the higher
# story_id"); this is the same rule read straight off master instead.
#
# Idempotent by construction: the lookup is on `story_id` only, so handing it
# an id that is already short leaves it alone.
@functools.lru_cache(maxsize=8192)
def wire_story_id(story_id) -> int:
    """The id the client is served for this story -- its short id when it has
    one, otherwise the id itself."""
    try:
        sid = int(story_id or 0)
    except (TypeError, ValueError):
        return story_id
    if sid <= 0:
        return story_id
    row = master_data.query_one(
        "SELECT short_story_id FROM single_mode_story_data WHERE story_id=?", (sid,))
    short = (row["short_story_id"] if row else 0) or 0
    return short or sid


# THE PER-TRAINEE EVENT BAND (the 11xxx/12xxx ids) -- STILL UNSOLVED, and the
# 2026-09-08 attempt is recorded here so it is not retried the same way.
#
# These ids appear NOWHERE in master.mdb (every integer column of every table
# was searched for 11051 / 11626 / 12526), so they are server-assigned. What
# they are assigned OVER is now known, though:
#
#   Q = single_mode_story_data rows with event_category=2, short_story_id>0,
#       in the 5<chara>nnn band, ordered by `id`
#
# Within one trainee the event_id is exactly `rank_in_Q + K`, and it holds
# across her whole id span with no exceptions: Oguri Cap's 12 captured pairs
# all sit at K=10928 (including a jump of 74 ranks between master rows 2291 and
# 2413 that the events match to the number), and Kitasan Black's 27 all sit at
# K=10955. The ordering is by master `id`, NOT by short_story_id and not by
# story_id -- both were tested and contradicted by the captures.
#
# What is NOT solved is K. It is per-trainee (10928 Oguri, 10932 for the third
# captured trainee, 10955 Kitasan) rather than global, so Q must be missing
# ~27 rows scattered through the id space -- but the drift is not monotonic in
# `id`, which rules out simply adding a category of rows to Q. Fitting K per
# trainee against the bot logs recovers it with 22/22, 25/25, 26/26 and 31/31
# exact matches for the trainees with the most data, but the fitted value moves
# when the input set is widened, so those fits are not trustworthy enough to
# serve. A capture of any ONE chain beat for a few more trainees would pin K
# for them directly and very likely expose the pattern.
#
# Until then these beats keep the generic CHARA_EVENT_ID rather than a
# fabricated id: a wrong id here is a wrong presentation, and a guessed formula
# that is right for three trainees and unverified for the other eighty is not
# better than an honest fallback.


# THE 20xxx BAND -- a support card's random event #3 does NOT use the generic id.
#
# Measured over 34 distinct real 20xxx ids and 19,545 acks in the bot logs, with
# zero unexplained: random events #1 and #2 (stories 80<chara>001 / 002) go out
# as the generic 20000, but #3 ("A Hint for Growth", 80<chara>003) carries a
# UNIQUE id of its own --
#
#     event_id = 20001 + rank
#
# where rank is the 0-based position of that story among all 80<chara>003
# stories ordered by single_mode_story_data.id, EXCLUDING the 809* NPC/scenario
# support characters. master.mdb yields exactly 79 such rows, giving the
# observed range 20001..20079.
#
# Serving 20000 for all three collapsed the whole family onto one id: the client
# keys its presentation off event_id, and the server could not tell two
# simultaneously-queued reveals apart.
_RANDOM_EVENT_IDS: dict | None = None


def support_random_event_id(story_id) -> int:
    """The wire event_id for a support-card RANDOM event with this story --
    its own 20001..20079 id for a #3, else the generic 20000."""
    global _RANDOM_EVENT_IDS
    if _RANDOM_EVENT_IDS is None:
        _RANDOM_EVENT_IDS = {
            int(r["story_id"]): SUPPORT_RANDOM_EVENT_ID + 1 + i
            for i, r in enumerate(master_data.query(
                "SELECT story_id FROM single_mode_story_data "
                "WHERE story_id BETWEEN 800000000 AND 808999999 "
                "AND story_id % 1000 = 3 ORDER BY id"))}
    try:
        return _RANDOM_EVENT_IDS.get(int(story_id), SUPPORT_RANDOM_EVENT_ID)
    except (TypeError, ValueError):
        return SUPPORT_RANDOM_EVENT_ID

# full_state keys for the dynamic-event flow.
CAREER_EVENT_CTX_KEY = "active_career_event"   # {event_id, story_id, trainee_card_id}
FIRED_EVENTS_KEY = "fired_career_events"        # [story_id, ...] already fired this run
# ["support:30078", "chara:105101", ...] -- the chains dead-ended this run by a
# `chain_end` outcome. Owner tokens rather than bare card ids because BOTH
# kinds of chain carry `ee`: a support card's chain and a trainee's own
# outing/date chain, whose ids overlap in no useful way. Their RANDOM events
# still fire -- it is the chain that closes, not the card. Per-career, so it
# belongs in _CAREER_STATE_KEYS.
CHAIN_ENDED_KEY = "ended_support_chains"

# data-type -> stat key on chara_info. The datamined DB uses "wisdom"; the wire
# / chara_info uses "wiz".
_STAT_TYPE_KEY = {"speed": "speed", "stamina": "stamina", "power": "power",
                  "guts": "guts", "wisdom": "wiz", "wiz": "wiz"}
# "Fast Learner" (text_data cat 142 idx 7) -- a CHANCE reward on the events
# that offer it, and mechanically an SP DISCOUNT on skill purchases (see
# single_mode_team._skill_purchase_cost).
_FAST_LEARNER_CONDITION = 7
_FAST_LEARNER_CHANCE = 0.10
# stat key -> display target index (1-5).
_STAT_KEY_IDX = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events")

# lazily-built indexes: card_id(str) -> {normalized_title: event}
_SUPPORT_INDEX: dict[str, dict[str, dict]] | None = None
_CHARA_INDEX: dict[str, dict[str, dict]] | None = None


def _norm(s: str | None) -> str:
    # master.mdb wraps long event titles with a LITERAL backslash-n, two
    # characters, not a newline: 'After the Kikuka Sho: Still No Scheduled\n
    # Landing'. The \w strip below keeps word characters, so that escape used to
    # survive as a stray 'n' glued between the two words -- and every one of
    # those titles then matched NOTHING on the GameTora side, which spells its
    # titles out with a space. 236 of the 546 secret events (43%) were
    # unservable for exactly this reason, along with every other long-titled
    # event: 'After the ...' post-race beats are the worst hit.
    s = re.sub(r"\\+[nrt]", " ", s or "")
    # \w keeps unicode word chars: JP-titled events (e.g. Light Hello's card)
    # must NOT all collapse to "" and overwrite each other in the index.
    return re.sub(r"[^\w]", "", s.lower(), flags=re.UNICODE)


def _iter_events(card: dict):
    """Yield every event dict in a datamined card entry (both the newer
    'sections' layout and the flat chain/random/... lists)."""
    secs = card.get("sections")
    if isinstance(secs, dict):
        for evs in secs.values():
            for e in evs or ():
                yield e
    for key in ("chain_events", "events", "random_events", "special_events",
                "date_events", "costume_events"):
        for e in card.get(key) or ():
            yield e


def _load():
    global _SUPPORT_INDEX, _CHARA_INDEX
    if _SUPPORT_INDEX is not None:
        return
    _SUPPORT_INDEX, _CHARA_INDEX = {}, {}

    def add(index, cid, event):
        index.setdefault(str(cid), {})[_norm(event.get("name"))] = event

    for fname in ("jp_events.json", "events.json"):
        path = os.path.join(_DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            db = json.load(fh)
        for cid, card in db.items():
            for e in _iter_events(card):
                add(_SUPPORT_INDEX, cid, e)

    path = os.path.join(_DATA_DIR, "character_event_data.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            db = json.load(fh)
        for cid, card in db.items():
            for e in _iter_events(card):
                add(_CHARA_INDEX, cid, e)


# ---- story_id decoding ------------------------------------------------------
# support-card event: story_id = 800000000 + card_id*1000 + index
# character event:    story_id = 500000000 + chara_id*1000 + index

def _support_card_of(story_id: int):
    if 800000000 <= story_id < 900000000:
        return (story_id - 800000000) // 1000
    return None


def _chara_of(story_id: int):
    if 500000000 <= story_id < 600000000:
        return (story_id - 500000000) // 1000
    return None


def resolve(story_id: int, title: str | None, trainee_card_id=None) -> dict | None:
    """Find the event for a story_id, by (source id, normalized title).

    Preference order: GameTora dump (the richer, user-preferred source) first,
    then the datamined JSON as a fallback. Support events resolve by card_id;
    character events by the trainee's card_id (a chara's events are keyed under
    her card). Both sources normalize to the same schema (choices w/ effects or
    random-branch outcomes), so the caller is source-agnostic. Returns the event
    dict or None."""
    _load()
    key = _norm(title)
    if not key:
        return None

    card = _support_card_of(story_id)
    if card is not None:
        gt = gametora.load_cached("support", card)
        if gt and key in gt:
            return gt[key]
        return (_SUPPORT_INDEX.get(str(card)) or {}).get(key)

    chara = _chara_of(story_id)
    if chara is not None:
        if trainee_card_id:
            gt = gametora.load_cached("chara", trainee_card_id)
            if gt and key in gt:
                return gt[key]
            ev = (_CHARA_INDEX.get(str(trainee_card_id)) or {}).get(key)
            if ev:
                return ev
        # fall back to any card belonging to that chara (cards are 100<chara>NN)
        for cid, evs in _CHARA_INDEX.items():
            if cid.startswith(str(chara)) and key in evs:
                return evs[key]
    return None


def title_key(text: str) -> str:
    """The key an event title is indexed under (see _norm) -- public so callers
    can ask 'does this card's event data claim this master.mdb title?'."""
    return _norm(text)


def card_event_title_keys(trainee_card_id: int) -> frozenset:
    """Normalized titles of the events listed for THIS trainee card. Each outfit
    card lists only its OWN costume events (Special Week's base card has 'How
    Should I Pose?', her swimsuit card has 'Swimsuit Power Unlocked!'), which is
    what makes outfit-aware filtering possible.

    BOTH sources are merged, and GameTora is the bigger one: it has 260 trainee
    cards to character_event_data.json's 80. Reading only the datamined file
    left every card it omits looking like it had no events at all -- McQueen's
    third costume (101303) has 26 events on GameTora and zero there -- which
    sent it down the 'no ownership basis, don't filter' path and handed it its
    siblings' costume events."""
    _load()
    keys = set(_CHARA_INDEX.get(str(trainee_card_id)) or ())
    keys |= set(gametora.load_cached("chara", trainee_card_id) or ())
    return frozenset(k for k in keys if k and k not in _RESERVED_EVENT_KEYS)


# Cache keys that are NOT events: per-chara metadata and the shared-event stat
# codes normalize() stores alongside them.
_RESERVED_EVENT_KEYS = frozenset({"__meta__", gametora._SPECIAL_KEY})


def card_events_for_trainee(trainee_card_id: int) -> dict:
    """A trainee card's {key: event}, GameTora merged OVER the datamined index.
    GameTora is the bigger source (260 cards vs 80) AND the only one carrying
    `section`/`conditions`, so it wins on conflicts."""
    _load()
    merged = dict(_CHARA_INDEX.get(str(trainee_card_id)) or {})
    merged.update(gametora.load_cached("chara", trainee_card_id) or {})
    return {k: v for k, v in merged.items() if k not in _RESERVED_EVENT_KEYS}


def special_event_stats(trainee_card_id: int) -> dict:
    """Per-trainee stat codes for the SHARED 'Special Events', straight off the
    trainee's GameTora page: {'nyear': 'st', 'dance': ['st', 'in']}.
    Codes are GameTora's short forms -- see SPECIAL_STAT_NAMES."""
    page = gametora.load_cached("chara", trainee_card_id) or {}
    val = page.get(gametora._SPECIAL_KEY)
    return val if isinstance(val, dict) else {}


# GameTora short stat codes -> the effect type names our engine applies.
# VERIFIED, not guessed: cross-checked against character_event_data's
# new_years_resolution_stat on the 142 cards carrying both, which produced
# exactly these five pairs and no others. Two guesses it overturned:
# `in` is WISDOM (intelligence), not bond -- reading it as bond served 53
# trainees "+25 bond" for their New Year resolution -- and power is `po`, not
# `pw`. Neither `pw` nor `wi` occurs anywhere in the data.
SPECIAL_STAT_NAMES = {"sp": "speed", "st": "stamina", "po": "power",
                      "gu": "guts", "in": "wisdom", "pt": "skill_points"}


def sibling_card_ids(trainee_card_id: int) -> tuple:
    """The character's OTHER outfit cards (100101 <-> 100102 <-> 100103).
    Union of both sources for the same reason card_event_title_keys merges
    them -- a sibling missing from the datamined file is still a sibling."""
    _load()
    chara = trainee_card_id // 100
    ids = set(_CHARA_INDEX) | set(_cached_chara_ids())
    return tuple(sorted(int(c) for c in ids
                        if str(c).isdigit() and int(c) // 100 == chara
                        and int(c) != trainee_card_id))


_cached_chara_ids_cache: tuple | None = None


def _cached_chara_ids() -> tuple:
    """Trainee card ids with a dumped GameTora page (see gametora._DATA_DIR)."""
    global _cached_chara_ids_cache
    if _cached_chara_ids_cache is None:
        try:
            names = os.listdir(gametora._DATA_DIR)
        except OSError:
            names = []
        _cached_chara_ids_cache = tuple(
            n[len("chara_"):-len(".json")] for n in names
            if n.startswith("chara_") and n.endswith(".json")
            and n[len("chara_"):-len(".json")].isdigit())
    return _cached_chara_ids_cache


# ---- effect encode / apply --------------------------------------------------

def _parse_value(v, default: int = 0) -> int:
    """First signed integer in a value string. A GameTora "+5/+10" range still
    yields its LOW half here; the range itself is expanded into two outcome
    branches upstream by _choice_effect_groups, so nothing should reach this
    with a range still attached except through a caller that wants one number."""
    if v is None:
        return default
    m = re.search(r"-?\d+", str(v))
    return int(m.group()) if m else default


# "+5/+10", "+1/+3", "-10/-20" -- GameTora writes a choice's two possible
# outcomes as one slash-separated value per effect.
_RANGE_RE = re.compile(r"^\s*([+-]?\d+)\s*/\s*([+-]?\d+)\s*$")


def _split_range_effects(effects):
    """(low_branch, high_branch) when any effect carries a "+5/+10" range, else
    None.

    These are a 50/50 GAMBLE, not a bond-scaled amount (user-corrected
    2026-07-28). 'Paying It Forward' is the reported case: its second choice is
    {speed "+5/+10", skill_hint "+1/+3"}, i.e. either (+5 speed, hint +1) OR
    (+10 speed, hint +3) -- and the branches move TOGETHER, so they have to be
    split per-branch rather than rolled per-effect. We were serving only the low
    half and the client showed a single outcome.

    Effects without a range are shared by both branches unchanged."""
    if not effects:
        return None
    if not any(_RANGE_RE.match(str(e.get("value") or "")) for e in effects):
        return None
    low, high = [], []
    for e in effects:
        m = _RANGE_RE.match(str(e.get("value") or ""))
        if m:
            low.append(dict(e, value=m.group(1)))
            high.append(dict(e, value=m.group(2)))
        else:
            low.append(e)
            high.append(e)
    return low, high


def _G(did, e0, e1=0, e2=0):
    return {"display_id": did, "effect_value_0": int(e0),
            "effect_value_1": int(e1), "effect_value_2": int(e2)}


# gain_param_array display_id -> the string the client renders, VERBATIM from
# master (text_data category 394, 36 rows). Found 2026-07-28 while fixing the
# post-race preview; this is the authoritative table and it independently
# CONFIRMS every display id we had previously derived from captures alone
# (1/2 stat, 4/5 bond, 6 hint, 9/37 condition, 11 unlock, 14 random, 35 bond
# down). Note the numbers here are display ids, NOT the `effect_value_0`
# targets -- energy/mood/SP all render through display 1 with targets 10/20/30.
#
#    1 '{0} +{1}'                          2 '{0} -{1}'
#    3 '+{0} Fans'                         6 'hint lvl +{0}'
#    4 'Friendship with {0} +{1}'          5 ... '(cannot change ... not in your deck)'
#    7 'Gain'                              8 'Chance to gain a random skill'
#    9 'Become {0}'                       10 'Cures {0}'
#   11 'Unlock recreation with {0}'       12 "End this Support Card's chain event"
#   13 'All attributes +{0}'              14 'Random {0} attribute(s) +{1}'
#   15 'Cures all bad conditions'         16 'Randomly cures {0} bad condition(s)'
#   17 'Restrict {0} training'            18 'Restrict race entry'
#   19 'Previously trained attribute +{0}' 20 ... '-{0}'
#   21 'Stat gains based on race grade'   22 'Stat gains based on race grade and result'
#   31/32 'Receive the following skill hints in chain event {0}:'
#   34 'Random {0} attribute(s) -{1}'     38/39/40 '... (Prevented by {n})'
#
# Still unused and worth a look: 17/18 (training / race-entry restrictions).
DISPLAY_FANS = 3
DISPLAY_RACE_GRADE_STATS = 21    # 22 is the '... and result' variant
DISPLAY_RANDOM_SKILL_CHANCE = 8
DISPLAY_CHAIN_END = 12           # "End this Support Card's chain event" -- see
                                 # the chain_end effect (gametora's `ee`).

# stat display index (1-5) -> stat key, for the capped-stat log line.
_STAT_IDX_KEY = {idx: key for key, idx in _STAT_KEY_IDX.items()}

# THE "NOTHING HAPPENED" LINES on an event's outcome screen -- "<stat> is in
# superb form", "Energy is full.", "Mood remains Great.", "Friendship with {0}
# is maxed out." The client prints one per reward that landed on something
# ALREADY at its ceiling (user-reported 2026-09-03, for the stat one). Note it
# is the ALREADY-capped case only: a stat that merely REACHES its cap on this
# event still gains, and the client just cuts the number off by itself.
#
# None of these strings are in master.mdb -- they are client literals
# (TextId.SingleMode0363 "{0} is in <color=#F16D27>superb form</color>.",
# 0364 energy, 0362 mood, 0355 friendship), which is why an earlier hunt
# through text_data category 394 for a display id that renders them found
# nothing: gain_param_array is not the mechanism at all. The client builds the
# lines from WorkSingleModeChangeParameterInfo's LimitStatusTypeList /
# LimitEvaluationCharaIdList, which SetLimitParameter fills straight from the
# response's `not_up_parameter_info` (dump.cs: NotUpParameterInfo,
# SingleModeCheckEventResponse.CommonResponse) -- so the SERVER names what
# didn't move and the client prints one line each.
#
#   status_type_array         SingleModeDefine.ParameterGainLimitType, the same
#                             numbering as our gain display targets: 1-5 stats,
#                             6 energy, 20 mood, 30 skill points.
#   evaluation_chara_id_array CHARA ids (capture-confirmed: [1026], the chara of
#                             the deck card whose bond was already 100), not
#                             deck positions.
#
# Deliberately NOT filled: skill_id_array / skill_tips_array / skill_lv_id_array
# ("Already acquired {0}", "The hint level for {0} is at its max"). Two int[]
# fields and one SkillTips[] field could each be the hint-at-max one and no
# capture disambiguates them; guessing risks handing the client's MessagePack
# formatter the wrong shape. A hint on a maxed skill stays silent until a
# capture settles it.
_MOOD_MAX = 5
_BOND_MAX = 100

# Scenario NPCs whose evaluation row is keyed by target id, not by a deck
# position: target_id -> chara_id, derived from the unlock table so a new NPC
# only has to be registered there (102 Director, 103 Reporter, 2001 Meek, ...).
_NPC_BOND_CHARAS = {target_id: chara_id
                    for pairs in single_mode_events.NPC_UNLOCKS.values()
                    for target_id, chara_id in pairs}


def capped_stat_indexes(chara_info: dict | None) -> frozenset:
    """The display indexes (1-5, _STAT_KEY_IDX) of the five stats that are
    ALREADY sitting at their cap.

    Read BEFORE a choice is applied -- afterwards a stat that this very event
    pushed up to its cap looks identical to one that was capped all along, and
    only the latter is "at its peak"."""
    if not chara_info:
        return frozenset()
    capped = set()
    for key, idx in _STAT_KEY_IDX.items():
        cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}")
        if cap and (chara_info.get(key) or 0) >= cap:
            capped.add(idx)
    return frozenset(capped)


def capped_stat_codes(before: dict | None, stats) -> list:
    """not_up_parameter_info status codes (1-5) for every stat named in
    `stats` that was ALREADY at its cap in `before`.

    The reporting half of a DIRECT stat write -- the pay-outs that add stats
    themselves instead of going through apply_choice (the end-of-run sendoffs,
    Grand Live's concert pay-outs, the race and team-race rewards). Those never
    build a gains list, so not_up_info has nothing to read; they name the stats
    they touched instead and get the same "<stat> is in superb form" line.

    `before` MUST be the pre-application picture -- afterwards "was already
    capped" and "just hit the cap" are indistinguishable, and only the former
    earns a line (see not_up_snapshot).
    """
    if not before:
        return []
    capped = capped_stat_indexes(before)
    return sorted({_STAT_KEY_IDX[s] for s in stats or ()
                   if s in _STAT_KEY_IDX and _STAT_KEY_IDX[s] in capped})


# Codes owed by a pay-out that ran too far from the response to put them on it
# itself -- a scenario's on_event_resolved hook, whose return value check_event
# discards. Drained onto the response right after that hook runs, so a stash
# made inside it lands on the card that granted the stats rather than the next
# one. See handlers.single_mode_team._note_not_up.
PENDING_NOT_UP_KEY = "pending_not_up"


def stash_not_up(full_state: dict | None, codes) -> None:
    """Owe `codes` to whichever response comes out of this event resolution."""
    if not isinstance(full_state, dict) or not codes:
        return
    merged = set(full_state.get(PENDING_NOT_UP_KEY) or ()) | set(codes)
    full_state[PENDING_NOT_UP_KEY] = sorted(merged)
    log.info("pay-out: already at ceiling -> not_up_parameter_info %s (stats %s)",
             sorted(codes), [_STAT_IDX_KEY.get(c) for c in sorted(codes)])


def not_up_snapshot(chara_info: dict | None) -> dict | None:
    """The BEFORE picture not_up_info reads. Taken before a reward applies,
    because afterwards "was already full" and "just became full" are
    indistinguishable -- and only the former earns a line.

    The evaluation rows are copied because _apply_bond mutates them in place;
    everything else not_up_info reads is replaced wholesale, not mutated."""
    if not chara_info:
        return None
    snap = dict(chara_info)
    snap["evaluation_info_array"] = [dict(e) if isinstance(e, dict) else e
                                     for e in chara_info.get("evaluation_info_array") or ()]
    return snap


def bond_with_chara(chara_info: dict, char_id) -> int | None:
    """Current bond (evaluation) with a character named by CHARA id, or None if
    there is no row for her.

    An evaluation row can be keyed three ways: a deck card's position (an
    ordinary support card), a scenario NPC's target id (the Director/Reporter/
    Meek), or the chara id itself (Grand Live's cardless supporters). Bond
    rewards name the CHARA, so all three have to be tried."""
    if not char_id:
        return None
    targets = {char_id} if char_id > 100 else set()
    pos = deck_position_for_chara(chara_info, char_id)
    if pos is not None:
        targets.add(pos)
    for target_id, npc_chara in _NPC_BOND_CHARAS.items():
        if npc_chara == char_id:
            targets.add(target_id)
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") in targets:
            return e.get("evaluation", 0)
    return None


def not_up_info(gains: list, before: dict | None) -> dict:
    """The not_up_parameter_info fields this reward earns: everything it tried
    to RAISE that was already at its ceiling in `before`.

    Only the "up" displays count -- a LOSS (display 2/35) still lands on a
    capped stat or a maxed bond -- and only what the reward actually touches:
    a stat the choice never mentions isn't "in superb form", it simply wasn't
    part of the event."""
    if not before:
        return {}
    capped = capped_stat_indexes(before)
    energy_full = (before.get("vital") or 0) >= before.get("max_vital", 100)
    mood_max = (before.get("motivation") or 0) >= _MOOD_MAX
    status: set = set()
    charas: set = set()
    for g in gains or ():
        did, target = g.get("display_id"), g.get("effect_value_0")
        if did == 1:                                  # '{0} +{1}'
            if target in capped:                      # 1-5, a stat at its cap
                status.add(target)
            elif target == 10 and energy_full:        # energy -> "Energy is full."
                status.add(6)
            elif target == 20 and mood_max:           # mood -> "Mood remains Great."
                status.add(20)
        elif did == 4 and (bond_with_chara(before, target) or 0) >= _BOND_MAX:
            charas.add(target)                        # "Friendship with {0} is maxed out."
    info = {}
    if status:
        info["status_type_array"] = sorted(status)
    if charas:
        info["evaluation_chara_id_array"] = sorted(charas)
    if info:
        log.info("event reward: already at ceiling -> not_up_parameter_info %s "
                 "(stats %s)", info,
                 [_STAT_IDX_KEY.get(t) for t in sorted(status & set(_STAT_IDX_KEY))])
    return info


def source_default_chara(source, source_id):
    """The character a bond defaults to when the effect names none -- a support
    card's `this-card-bond` (`bo` with no char_id) is friendship with the card's
    own featured character."""
    if source == "support":
        return card_chara_id(source_id)
    return None


def _encode_gains(effects, chara_info=None, default_char_id=None) -> list:
    """Encode a list of effects into gain_param_array entries, flattening the
    multi-entry cases (all_stats -> five per-stat lines)."""
    out = []
    for e in effects or ():
        g = _encode_effect(e, chara_info, default_char_id)
        if g is None:
            continue
        out.extend(g if isinstance(g, list) else [g])
    return out


def _encode_effect(eff: dict, chara_info: dict | None = None, default_char_id=None):
    """One effect -> a gain_param_array entry, a LIST of entries (all_stats),
    or None to omit from the preview.
    When chara_info is given, a bond for a character not in the deck uses the
    "can't gain friendship" variant (display 5). A bond with no char_id (a support
    card's this-card-bond) falls back to default_char_id so the name shows.

    Two overrides let an effect APPLY a real number while DISPLAYING something
    else, which is what the race events need -- the real client never shows the
    figures there, it shows 'Stat gains based on race grade' and 'Chance to gain
    a random skill' (live-reported: we were leaking the actual numbers and the
    named skill). `hidden` drops the effect from the preview entirely; the
    `display_*` keys emit an arbitrary display row instead of the type's own.
    See _DISPLAY (text_data category 394) for the vocabulary."""
    if eff.get("hidden"):
        return None
    if eff.get("display_id") is not None:
        return _G(eff["display_id"], eff.get("display_target", 0),
                  eff.get("display_value", 0))
    t = eff.get("type")
    if t in _STAT_TYPE_KEY:
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, _STAT_KEY_IDX[_STAT_TYPE_KEY[t]], abs(v))
    if t == "energy":
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, 10, abs(v))
    if t == "mood":
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, 20, abs(v))
    if t in ("skill_points", "skill_point"):
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, 30, abs(v))
    if t == "all_stats":
        # FIVE per-stat entries, not the generic 'Random N attribute(s)' icon
        # (display 14) -- that icon made 'Mystery Fortune Ritual!' preview as
        # 'Random 1 attribute(s) +7' when the real screen lists all five
        # (live-reported; the APPLY path was always correct).
        # display 1 is '{0} +{1}' and 2 is '{0} -{1}': a LOSS must use 2, or the
        # preview contradicts the outcome. This hardcoded 1, so the
        # acupuncturist's bad branch advertised '+15 to everything' and then
        # took 15 off each (live-reported, with the result screen correct).
        v = _parse_value(eff.get("value"), 1)
        return [_G(1 if v >= 0 else 2, idx, abs(v)) for idx in (1, 2, 3, 4, 5)]
    if t == "max_energy":
        # Target 11 = MAX energy, not 10 (plain energy) -- live-reported as
        # "max-energy gains display as plain 'Energy'". Read off the real
        # capture of event 102001: across its five choices the same slot holds
        # a stat CAP (51/53/54/55 at +4) for four of them and target 11 at +4
        # for the energy one, alongside target 10 for ordinary energy.
        return _G(1, 11, abs(_parse_value(eff.get("value"))))
    if t == "full_energy":
        return _G(1, 10, 100)
    if t == "bond":
        char_id = eff.get("char_id") or default_char_id or 0
        v = _parse_value(eff.get("value"), 5)
        # A non-deck character is shown with display 5 -- the "can't gain
        # friendship" bond variant (confirmed from a capture where a chara not in
        # the deck rendered its +5 bond as display 5, not 4). No bond is applied.
        if chara_info is not None and char_id and deck_position_for_chara(chara_info, char_id) is None:
            return _G(5, char_id, abs(v))
        return _G(4 if v >= 0 else 35, char_id, abs(v))
    if t == "npc_bond":
        # A SCENARIO NPC's bond (the Reporter, the Director, Meek). They have no
        # support card, so they can't go through the deck lookup above -- their
        # evaluation row is keyed by target_id, and only chara_id is displayed.
        v = _parse_value(eff.get("value"), 5)
        return _G(4 if v >= 0 else 35, eff.get("chara_id") or 0, abs(v))
    if t == "fans":
        return _G(DISPLAY_FANS, abs(_parse_value(eff.get("value"))))
    if t in ("skill_hint", "obtain_skill"):
        return _G(6, eff.get("skill_id") or 0, abs(_parse_value(eff.get("value"), 1)))
    if t == "skill_hint_or":
        # PREVIEW ONLY (the commit path resolves the set to one alternative
        # first -- resolve_or_effects). One hint row per alternative, which is
        # how GameTora renders an `sr` too ("X or Y"): there is no display id
        # for "one of these", and dropping the extras would hide from the
        # player which skills are even on the table.
        return [_G(6, o.get("skill_id") or 0, abs(_parse_value(o.get("value"), 1)))
                for o in (eff.get("options") or []) if isinstance(o, dict)]
    if t == "chain_end":
        # display 12 "End this Support Card's chain event" -- the real client's
        # own warning line for a dead-end outcome, so the player can see which
        # branch costs them the rest of the chain (the whole point of the
        # mechanic). Takes no arguments.
        return _G(DISPLAY_CHAIN_END, 0, 0)
    if t == "condition":
        cid = eff.get("condition_id") or 0
        return _G(9 if cid in _GOOD_CONDITIONS else 37, cid, 0)
    if t in ("highest_stat", "lowest_stat", "last_stat", "random_stats"):
        # rendered on a stat the client resolves; show a generic stat icon.
        # 14 is 'Random {0} attribute(s) +{1}' and 34 is the '-{1}' variant --
        # same gain/loss split as all_stats above.
        v = _parse_value(eff.get("value"))
        return _G(14 if v >= 0 else 34, int(eff.get("stat_count") or 1), abs(v))
    if t == "heal_status":
        # display 16 'Randomly cures {0} bad condition(s)' (text_data 394) --
        # user-reported 2026-08-25: Extra Training's top option applies its
        # 20% debuff-cure roll (see _EXTRA_TRAINING_CURE_CHANCE / the
        # heal_status apply branch above) but never showed a line for it on
        # the choice preview, since this fell through to the no-icon default
        # below. The display line itself doesn't reveal the odds -- same as
        # display 8's "Chance to gain a random skill" for skill_hint -- so
        # this applies whether or not the effect carries a `chance` (the
        # acupuncturist's unconditional heal_status gets the same line).
        # `effect_value_0`=1 matches the apply-time behavior of curing
        # exactly one condition when several are present.
        return _G(16, 1)
    # fans / mt / race_rewards / date_* : applied but no reward icon here
    return None


# Guard kinds we can actually decide from chara_info. Anything else (a scripted
# `sc` condition, a `ct` count label, skill/condition possession) falls back to
# the choice's default segment rather than guessing -- a wrong guess here would
# silently hand out the wrong branch of a career storyline.
def _guard_holds(guard: dict, chara_info: dict) -> bool | None:
    kind = guard.get("kind")
    if kind == "otherwise":
        return None            # the else arm; only taken when nothing else did
    if kind in ("mood_min", "mood_max"):
        mood = chara_info.get("motivation")
        if mood is None:
            return None
        want = guard.get("value")
        if not isinstance(want, int):
            return None
        return mood >= want if kind == "mood_min" else mood <= want
    if kind == "season":
        season = _season_of(chara_info)
        return None if season is None else season == guard.get("value")
    return None


_SEASONS = {1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall",
            11: "fall", 12: "winter"}


def _season_of(chara_info: dict):
    """The in-career season, from the turn's calendar month."""
    from .handlers import single_mode_team
    row = single_mode_team._turn_slot(chara_info.get("turn"))
    return _SEASONS.get(row["month"]) if row else None


def resolve_segments(choice: dict, chara_info: dict | None) -> dict:
    """A choice with conditional `segments` -> the ONE segment that applies,
    shaped as a plain choice. Returns the choice untouched when it has no
    segments, so every ordinary event takes the same path it always did.

    Picks the first segment whose guards all hold; if none does (or nothing is
    decidable), the `otherwise` arm, then the unguarded arm, then the first --
    the same order GameTora renders them in."""
    segments = choice.get("segments")
    if not segments:
        return choice
    chosen = segments[_segment_index(segments, chara_info)]
    out = {k: v for k, v in choice.items() if k not in ("segments", "effects",
                                                        "outcomes", "probs",
                                                        "random_either")}
    out.update({k: v for k, v in chosen.items() if k != "when"})
    return out


def chain_owner_token(source, source_id) -> str | None:
    """"support:30078" / "chara:105101" for a career event ctx, else None.

    Only the two sources that HAVE a chain get a token; a 'seasonal' or
    'inline' event has nothing to dead-end, and answering None for those keeps
    a stray `ee` from closing some unrelated card's chain."""
    if source not in ("support", "chara") or not source_id:
        return None
    return f"{source}:{int(source_id)}"


def end_chain(full_state: dict | None, owner) -> None:
    """Dead-end one chain (support card's or trainee's) for the rest of this career.

    Recorded as an owner token rather than by bulk-marking the chain's
    remaining story ids as fired: the fired set is also what stops an event
    repeating, so burning ids into it would be indistinguishable from "already
    seen" and would make a re-derived chain (a cache refresh mid-career)
    silently swallow events the player never got. It also keeps the card's
    RANDOM events firing, which is correct -- `ee` closes the chain, not the
    card."""
    # `is None`, not a truth test: an empty dict is a perfectly good (and, at
    # the very start of a career, normal) full_state, and `not {}` would drop
    # the very first chain_end of the run on the floor.
    if full_state is None or not owner:
        return
    ended = full_state.setdefault(CHAIN_ENDED_KEY, [])
    if owner not in ended:
        ended.append(owner)


def chain_ended(full_state: dict | None, owner) -> bool:
    if full_state is None or not owner:
        return False
    return owner in (full_state.get(CHAIN_ENDED_KEY) or ())


def _pick_or_option(eff: dict) -> dict | None:
    """Roll one alternative out of a `skill_hint_or` set.

    UNIFORM, because GameTora publishes no odds for an `sr` set -- the same
    default _roll_branch already uses for a random branch with no "~90"
    divider. If a capture ever pins the real split (the gold half of an SSR
    chain finale is the one that matters), weight it here."""
    opts = [o for o in (eff.get("options") or []) if isinstance(o, dict)]
    if not opts:
        return None
    return opts[_roll_branch(None, len(opts))]


def resolve_or_effects(choice: dict) -> dict:
    """Collapse every `skill_hint_or` in a choice down to the ONE alternative
    that landed, returning a shallow copy (the cached event dict is shared
    across careers and must never be mutated).

    Rolled HERE, once, on the commit path only -- before _choice_effect_groups
    -- for two reasons. The applied hint and the hint on the outcome screen
    then come from a single roll and cannot disagree (apply_choice runs
    _apply_effects and _encode_gains over the same list). And resolving first
    lets an alternative's own "+1/+3" range go through the normal branch split
    with everything else on the choice, so Fine Motion's gold hint still moves
    together with its Wit "+5/+10" instead of rolling independently.

    The PREVIEW deliberately does not call this: choice_reward_array shows a
    row per alternative, i.e. "one of these", which is the same contract it
    already uses for a two-branch random choice."""
    def resolve(effects):
        if not any(e.get("type") == "skill_hint_or" for e in effects or ()):
            return effects
        out = []
        for e in effects:
            if e.get("type") != "skill_hint_or":
                out.append(e)
                continue
            opt = _pick_or_option(e)
            if opt:
                out.append({"type": "skill_hint", "skill_id": opt.get("skill_id"),
                            "value": opt.get("value")})
        return out

    if not isinstance(choice, dict):
        return choice
    out = dict(choice)
    if isinstance(out.get("outcomes"), list):
        out["outcomes"] = [resolve(g) for g in out["outcomes"]]
    if out.get("effects"):
        out["effects"] = resolve(out["effects"])
    return out


def _choice_effect_groups(choice: dict):
    """The effect-list(s) for a choice: a random_either choice has two
    (outcomes[0]=lesser, outcomes[1]=greater); a normal one has a single list.

    A choice whose effects carry GameTora "+5/+10" ranges is ALSO two-branched
    and is expanded here -- the single chokepoint both the preview
    (choice_reward_array) and the commit (apply_choice) run through, so they can
    never disagree about how many outcomes a choice has."""
    if choice.get("random_either") and isinstance(choice.get("outcomes"), list):
        return [g or [] for g in choice["outcomes"]]
    effects = choice.get("effects") or []
    pair = _split_range_effects(effects)
    if pair:
        return [pair[0], pair[1]]
    return [effects]


def choice_array(event: dict, allow_empty: bool = False, chara_info: dict | None = None) -> list:
    """unchecked_event_array choice_array -- one slot per choice. Matches the real
    support/outing event shape exactly: the five scalar keys of the client's
    `ChoiceArray` (dump.cs:762564) and nothing else. gain_select_id_index (1..N)
    is what the client returns as choice_number on commit; select_index is the
    story BRANCH to play -- see _choice_branch_index.

    The `or 1` floor exists because a GameTora-sourced event with no recorded
    choices still has one implicit "OK" the client needs. That is wrong for a
    genuinely choice-LESS story: the real server sends an EMPTY choice_array for
    those, and manufacturing a choice makes the client render a button the story
    has no branch for -- which bootlooped Grand Live's turn-5 Tutorial. Pass
    allow_empty=True when the count is known to be authoritative (a capture)."""
    choices = event.get("choices") or []
    n = len(choices)
    if not (allow_empty and isinstance(event.get("choices"), list)):
        n = n or 1
    return [
        {"select_index": (_choice_branch_index(choices[i], chara_info)
                          if i < len(choices) else 1),
         "receive_item_id": 0,
         "target_race_id": (_choice_target_race(choices[i], chara_info)
                            if i < len(choices) else 0),
         "gain_select_id_index": i + 1, "select_icon": 0}
        for i in range(n)
    ]


def _choice_branch_index(choice: dict, chara_info: dict | None) -> int:
    """Which BRANCH of the story the client should play for this choice, 1-based.

    The real server decides this and sends it as the SCALAR `select_index` on
    each choice_array entry. Across every capture in the repo, 1554/1554 real
    entries carry it as a plain int -- 1395 are 1 and the rest run 2..7, i.e.
    only the branching choices carry an index other than 1. Agnes Tachyon's
    mood-gated event arrives as select_index 2 at Normal mood: branch 2 is the
    arm whose text swaps the objective. So the SERVER picks the story arm, the
    same way it picks the effects -- see resolve_segments, which this reuses so
    the two can never disagree.

    (An earlier version of this wrapped the value in an invented
    `select_index_info_array`. No capture and no dump.cs type has ever had such
    a member, so the client dropped it and read select_index as 0 for every
    choice this path served.)

    THIS IS ALSO WHERE A RANDOM OUTCOME IS DECIDED, and it has to be. The
    client plays the story branch named here (StoryViewController hands the
    chosen choice's select_index to StoryTimelineController.SetParameterSelectIndex,
    and every text clip tagged ParameterBranch/ParameterSelection --
    TextDifferenceType 6/5, dump.cs:655530 -- shows only the arm matching it).
    The commit response carries NOTHING the client could branch on: no capture
    of check_event's choice_number reply has ever contained a
    choice_reward_array. So the server must have already rolled the gamble by
    the time it SERVES the event, and the captures show it doing exactly that --
    the same story_id arrives with different select_index values on different
    serves, and only for choices that gamble:

        830107001 'A Seaside Resort, Love is in the Air'  (1,) and (2,)
        830028003 'We Walk Together'                      (1,) and (2,)
        501006516 'Bottomless Pit'                        (1,1) and (1,2)
            -- choice 1 is flat and is always 1; choice 2 is the random_either
        501006302 'Raffle Time!'                          (3,), (4,), (5,)

    Rolling it at commit instead (which is what apply_choice used to do alone)
    left the two halves independent: the text played branch 1 while the stats
    came from a fresh roll, so the story said the gamble paid off and the
    player got the losing numbers, or the reverse.

    The index is FLAT over everything the choice can resolve to, in wire order:
    segment 1's outcomes, then segment 2's, ... For the ordinary one-outcome-
    per-segment choice this is identical to the segment number this used to
    return (which is the value every segmented capture pins), and 'Raffle
    Time!''s 3/4/5 is what a choice with more than two arms looks like.
    """
    resolved = resolve_segments(choice, chara_info)
    groups = _choice_effect_groups(resolved)
    branch = (_roll_branch(resolved.get("probs"), len(groups))
              if len(groups) > 1 else 0)
    return _branch_offset(choice, chara_info) + branch + 1


def _branch_offset(choice: dict, chara_info: dict | None) -> int:
    """How many outcomes the segments BEFORE the applying one contribute -- the
    base the rolled branch is added to, and the base subtracted back off when a
    served branch is turned into an outcome again. 0 for a choice with no
    segments, which is almost all of them."""
    segments = choice.get("segments")
    if not segments:
        return 0
    i = _segment_index(segments, chara_info)
    return sum(len(_choice_effect_groups(seg)) for seg in segments[:i])


def _segment_index(segments: list, chara_info: dict | None) -> int:
    """0-based index of the segment that applies -- the ONE place that decision
    is made, so the branch number we serve, the reward preview and the committed
    effects can never come from different segments. Matching on segment CONTENT
    instead would mis-index arms that are deliberately identical (Narita Brian's
    same-race branches, Fuji Kiseki's two 'Toward Greater Heights')."""
    if chara_info is not None:
        for i, seg in enumerate(segments):
            guards = seg.get("when") or []
            if guards and all(_guard_holds(g, chara_info) for g in guards):
                return i
    for i, seg in enumerate(segments):
        if any(g.get("kind") == "otherwise" for g in seg.get("when") or []):
            return i
    for i, seg in enumerate(segments):
        if not seg.get("when"):
            return i
    return 0


def _choice_target_race(choice: dict, chara_info: dict | None = None) -> int:
    """The race_instance id this choice switches the career objective to, or 0.

    `ChoiceArray.target_race_id` (dump.cs:762569) is what feeds the client's
    `EventInfoBase.TargetRaceIdArray`, and it is what makes the goal-change
    production actually play -- `show_clear` 4/5 says WHICH production, this says
    WHICH RACE. We were sending a hardcoded 0, so the branch fired, the goal
    changed, and the client had no race to animate to (live-reported
    2026-09-04, twice).

    One value per choice, which is exactly the shape the branch events have:
    Fine Motion's four choices each name a different race, while Agnes Tachyon's
    single choice hides its swap inside a mood-guarded segment. Segments are
    searched too, and since at most one segment of a choice carries a
    race_change, no chara_info is needed to pick between them here."""
    # Only the segment that ACTUALLY applies may advertise a target race: at
    # mood Good, Tachyon's event keeps the Derby, and naming the NHK Mile arm
    # there would point the client's goal-change production at a race the career
    # is not going to run. With no chara_info to decide with, fall back to
    # scanning every segment (the preview path, where the value is unused).
    groups = list(choice.get("segments") or [])
    if groups and chara_info is not None:
        groups = [groups[_segment_index(groups, chara_info)]]
    for group in ([choice] + groups):
        lists = [group.get("effects") or []] + list(group.get("outcomes") or [])
        for effects in lists:
            for eff in effects:
                if eff.get("type") == "race_change" and eff.get("race_instance_id"):
                    if chara_info is None:
                        return 0
                    from .handlers import single_mode_team
                    return single_mode_team.route_race_id_for_instance(
                        chara_info, int(eff["race_instance_id"]))
    return 0


def career_event_entry(event: dict, event_id: int, story_id: int,
                       chara_id: int = 0, support_card_id: int = 0,
                       play_timing: int = 1, allow_empty_choices: bool = False,
                       chara_info: dict | None = None) -> dict:
    """An unchecked_event_array entry for a dynamically-served career event.
    Matches the real support(10002)/outing(6000) envelope: support events use
    chara_id 0 + event_contents_info.support_card_id; outings use the chara id.
    choice_array has one slot per choice -- see choice_array() for when a story
    legitimately has none."""
    return {
        # wire_story_id, not the id the caller tracks it by: bookkeeping keys
        # (fired-event sets, dedupe keys, master lookups) stay on the reference
        # id, and only the wire carries the short one. See wire_story_id.
        "event_id": event_id, "chara_id": chara_id,
        "story_id": wire_story_id(story_id),
        "play_timing": play_timing,
        "event_contents_info": {
            "support_card_id": support_card_id, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": choice_array(event, allow_empty_choices, chara_info),
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


# NOTE (2026-09-04): do NOT echo master's `single_mode_story_data.show_clear`
# into this response field. They are DIFFERENT enums that happen to share a name.
# The response one is SingleModeDefine.EventContentsShowClear (dump.cs:489864):
#     0 None  1 Clear  2 Failed  3 MemberLack
#     4 AoharuRaceList  5 TSCRaceRanking  6 VenusRaceList
# so passing master's 4 ("target race changed") through here asked the client to
# open the AOHARU RACE LIST in a Grand Live career. Only 1 and 2 belong here, and
# _mark_goal_progress is the one place that sets them.
#
# Master's own show_clear IS the goal-change production flag -- dump.cs exposes
# it as MasterSingleModeStoryData.SingleModeStoryData.ShowTargetRaceChange (4,
# 2 rows: Tachyon 501032114 and Daiwa Scarlet 501009115) and .ShowTargetRaceDecide
# (5, 6 rows) -- but the client reads that from its OWN master.mdb by story_id.
# It needs nothing from us.

UNLOCK_OUTING_DISPLAY_ID = 11   # capture: gain_param_array marker for
                                # "unlocks recreation with <effect_value_0>"


def choice_reward_array(event: dict, chara_info: dict | None = None,
                        default_char_id=None, unlock_partner=None,
                        unlock_chances=None, source_card_id=None) -> list:
    """get_choice_reward preview -- every choice's gains. A random_either choice
    contributes two entries (both branches) under the same select_index, exactly
    like the official server shows the two possible outcomes. chara_info (when
    given) marks bond for non-deck characters; default_char_id names a this-card
    bond.

    For a pal/group card's outing-UNLOCK event, pass unlock_partner (the chara
    the outing unlocks with) and unlock_chances ({select_index: probability}).
    The unlock is advertised with display_id 11 -- without it the player sees
    the stat gains but no "unlocks recreation" line and can't tell which choice
    is the one (live-reported for Tazuna, true of every such card). A choice
    that only MIGHT unlock emits both outcomes under the same select_index --
    one entry carrying the marker, one not -- which is exactly how the real
    server presents a gamble (capture: Riko's top choice)."""
    out = []
    chances = unlock_chances or {}
    for i, choice in enumerate(event.get("choices") or []):
        idx = i + 1
        chance = chances.get(idx, 0.0) if unlock_partner else 0.0
        marker = _G(UNLOCK_OUTING_DISPLAY_ID, unlock_partner) if chance > 0 else None
        entries = []
        for group in _choice_effect_groups(choice):
            gains = _encode_gains(boost_effects(group, source_card_id),
                                  chara_info, default_char_id)
            entries.append({"select_index": idx, "gain_param_array": gains})
        if marker is not None:
            if chance >= 1.0:
                for e in entries:
                    e["gain_param_array"] = e["gain_param_array"] + [dict(marker)]
            elif len(entries) > 1:
                # random_either: the DATA already carries both outcome branches
                # -- mark the richer (unlocking) one. Appending a synthetic
                # third entry here rendered a phantom 'Branch 3' on Riko's
                # unlock preview (live-reported).
                target = max(entries, key=lambda e: len(e["gain_param_array"]))
                target["gain_param_array"] = target["gain_param_array"] + [dict(marker)]
            else:
                # single-branch source: show with- and without-unlock outcomes
                entries.append({"select_index": idx,
                                "gain_param_array": list(entries[0]["gain_param_array"])
                                + [dict(marker)]})
        out.extend(entries)
    return out


def served_outcome(event: dict, choice_number, chara_info: dict | None,
                   branch) -> tuple:
    """(0-based outcome the served `branch` names, how many outcomes the choice
    has). The index is None when there is no served branch or it doesn't land
    inside this choice -- see apply_choice, which treats both the same way.

    For anything that has to agree with the gamble the player is WATCHING (the
    pal/group outing unlock, whose text says outright whether the outing
    opened), this is the question to ask; rolling again is what made the story
    and the state disagree."""
    choices = event.get("choices") or []
    if not choices:
        return None, 0
    idx = max(1, int(choice_number or 1)) - 1
    if idx >= len(choices):
        idx = 0
    groups = _choice_effect_groups(resolve_segments(choices[idx], chara_info))
    if not branch:
        return None, len(groups)
    i = int(branch) - 1 - _branch_offset(choices[idx], chara_info)
    return (i if 0 <= i < len(groups) else None), len(groups)


def richest_outcome(event: dict, choice_number, chara_info: dict | None = None) -> int:
    """Which outcome of a choice is the GOOD one -- the arm with the most
    gains. Deliberately the same rule choice_reward_array uses to decide which
    branch of a gamble gets the "unlocks recreation" marker, so the preview,
    the story branch and the unlock itself all name the same arm."""
    choices = event.get("choices") or []
    if not choices:
        return 0
    idx = max(1, int(choice_number or 1)) - 1
    if idx >= len(choices):
        idx = 0
    groups = _choice_effect_groups(resolve_segments(choices[idx], chara_info))
    if not groups:
        return 0
    # Measured on the ENCODED gains, not the raw effect lists, because that is
    # what choice_reward_array measures -- an effect that encodes to no reward
    # icon (fans, race rewards) must not tip the comparison one way there and
    # the other way here.
    sizes = [len(_encode_gains(g, chara_info)) for g in groups]
    return max(range(len(groups)), key=lambda i: sizes[i])


SOFT_CAP = 1200   # every point of stat gain ABOVE this is worth half


def add_stat(chara_info: dict, key: str, amount: int) -> int:
    """Add `amount` to a stat, HALVING the portion above SOFT_CAP (rounded
    down), clamped to the stat's max. Returns the value actually added.

    Universal rule (user-supplied): a gain that straddles 1200 counts the part
    below it in full and halves the rest, e.g. 1190 +20 -> 1190+10+5 = 1205.
    Previews still show the full number -- only the applied value halves.
    Every gain path must go through here (events, hints, appraisals, duels,
    races, inspirations); race rewards had their own copy of this rule."""
    cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}", 9999)
    cur = chara_info.get(key, 0) or 0
    if amount <= 0:
        new = max(0, min(cap, cur + amount))
        chara_info[key] = new
        return new - cur
    below = max(0, min(amount, SOFT_CAP - cur))
    above = amount - below
    new = min(cap, cur + below + above // 2)
    chara_info[key] = new
    return new - cur


_EVENT_BOOST_CACHE: dict = {}


def event_boost(source_card_id) -> tuple:
    """(energy_multiplier, effect_multiplier) for events belonging to a support
    card, from its own 'event recovery amount up' (effect type 25) and 'event
    effect up' (type 26).

    Only ~11 cards carry these and they are all the pal/friend cards -- which
    matches how the community sim models it (Game.cpp keeps a
    `friend_vitalBonus = 1 + 0.01 * eventRecoveryAmountUp` and spends it only in
    `addVitalFriend`). So the boost is scoped to the OWNING card's own events,
    not every event in the run. Both the preview and the commit go through
    this, so the outcome screen can't disagree with what's applied."""
    if not source_card_id:
        return (1.0, 1.0)
    if source_card_id not in _EVENT_BOOST_CACHE:
        from . import training_formula
        _EVENT_BOOST_CACHE[source_card_id] = (
            1.0 + training_formula.master_effect(
                source_card_id, training_formula.EFFECT_EVENT_RECOVERY) / 100.0,
            1.0 + training_formula.master_effect(
                source_card_id, training_formula.EFFECT_EVENT_EFFECT) / 100.0,
        )
    return _EVENT_BOOST_CACHE[source_card_id]


_BOOSTED_ENERGY = ("energy", "max_energy")
_BOOSTED_EFFECT = frozenset(("all_stats", "skill_points", "skill_point",
                             "highest_stat", "lowest_stat", "last_stat",
                             "random_stats"))


def boost_effects(effects: list, source_card_id) -> list:
    """`effects` with the owning card's event bonuses folded into the numbers.
    Only GAINS are scaled -- a card that makes its events better must never
    make their costs worse."""
    energy_mult, effect_mult = event_boost(source_card_id)
    if energy_mult == 1.0 and effect_mult == 1.0:
        return effects
    out = []
    for eff in effects or ():
        t = eff.get("type")
        mult = (energy_mult if t in _BOOSTED_ENERGY else
                effect_mult if (t in _STAT_TYPE_KEY or t in _BOOSTED_EFFECT) else 1.0)
        v = _parse_value(eff.get("value"), 0)
        if mult == 1.0 or v <= 0:
            out.append(eff)
            continue
        out.append(dict(eff, value=f"+{int(v * mult)}"))
    return out


def _apply_effects(chara_info: dict, effects: list, default_char_id=None,
                   full_state: dict | None = None, source_card_id=None,
                   chain_owner=None) -> list:
    """Applies every effect to chara_info in place. Returns extra
    gain_param_array-shaped entries for effects whose actual display can't be
    known statically from the effect dict alone (currently just "mt" -- which
    category ends up lowest is state-dependent, decided here at apply time,
    not something _encode_effect could ever derive from the static effect)."""
    extra_gains: list = []
    for eff in effects or ():
        t = eff.get("type")
        if t in _STAT_TYPE_KEY:
            add_stat(chara_info, _STAT_TYPE_KEY[t], _parse_value(eff.get("value")))
        elif t == "all_stats":
            v = _parse_value(eff.get("value"))
            for key in ("speed", "stamina", "power", "guts", "wiz"):
                add_stat(chara_info, key, v)
        elif t == "energy":
            mv = chara_info.get("max_vital", 100)
            chara_info["vital"] = max(0, min(mv, chara_info.get("vital", 0) + _parse_value(eff.get("value"))))
        elif t == "max_energy":
            chara_info["max_vital"] = chara_info.get("max_vital", 100) + _parse_value(eff.get("value"))
        elif t == "full_energy":
            chara_info["vital"] = chara_info.get("max_vital", 100)
        elif t == "mood":
            chara_info["motivation"] = max(1, min(5, chara_info.get("motivation", 3) + _parse_value(eff.get("value"))))
        elif t in ("skill_points", "skill_point"):
            chara_info["skill_point"] = max(0, chara_info.get("skill_point", 0) + _parse_value(eff.get("value")))
        elif t == "bond":
            _apply_bond(chara_info, eff.get("char_id") or default_char_id, _parse_value(eff.get("value"), 5))
        elif t == "npc_bond":
            _apply_npc_bond(chara_info, eff.get("target_id"), _parse_value(eff.get("value"), 5))
        elif t == "race_change":
            # A branching career storyline: the trainee's objective race for
            # this branch group changes. Delegate to single_mode_team, which
            # owns route_race_id_array -- see switch_route_race.
            from .handlers import single_mode_team
            if single_mode_team.switch_route_race(chara_info, eff.get("race_instance_id")):
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "race_cancel":
            # An objective race this event takes away (GameTora `ra`).
            from .handlers import single_mode_team
            if single_mode_team.cancel_route_race(chara_info, eff.get("race_instance_id")):
                # This turn may have been a FORCED goal-race turn; the screen
                # has to be rebuilt or the player stays locked into a race that
                # no longer exists.
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "race_restrict":
            # "Cannot race for N turns" (GameTora `rl`).
            from .handlers import single_mode_team
            if single_mode_team.restrict_racing(chara_info, eff.get("turns")):
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "branch_flag":
            # A named branch that swaps the STORY but not the race (only
            # Narita Brian's "Public Appearance"); recorded so the flag-gated
            # rival rows and the story picker can see it.
            flags = chara_info.setdefault("branch_flags", [])
            if eff.get("flag") and eff["flag"] not in flags:
                flags.append(eff["flag"])
        elif t == "unique_skill_level":
            # Raising the trainee's own unique skill needs the star-rating rules
            # in single_mode_team, so delegate rather than duplicate them.
            from .handlers import single_mode_team
            single_mode_team._level_unique_skill(
                chara_info, _parse_value(eff.get("value"), 1))
        elif t == "fans":
            # No reward icon for fans (no display id is known from any capture),
            # but the number itself is real and must land -- the Reporter's
            # winning coverage pays 500.
            chara_info["fans"] = max(0, chara_info.get("fans", 0)
                                     + _parse_value(eff.get("value")))
        elif t in ("skill_hint", "obtain_skill"):
            # `chance` present -> the hint is a ROLL, not a guarantee (the race
            # events advertise "Chance to gain a random skill"); absent ->
            # always, as before.
            chance = eff.get("chance")
            if chance is not None and random.random() >= float(chance):
                continue
            _apply_skill_hint(chara_info, eff.get("skill_id"), _parse_value(eff.get("value"), 1))
        elif t == "skill_hint_or":
            # Defensive only: apply_choice resolves these to a single
            # skill_hint before we get here (resolve_or_effects), so that the
            # applied hint and the displayed one are the SAME roll. Anything
            # reaching this branch came in through some other path -- roll it
            # here rather than silently granting nothing.
            opt = _pick_or_option(eff)
            if opt:
                _apply_skill_hint(chara_info, opt.get("skill_id"),
                                  _parse_value(opt.get("value"), 1))
        elif t == "chain_end":
            # chain_owner, NOT source_card_id: `ee` appears on trainee outing
            # chains too (105101/105102 'catchingtheeveningblooms'), and
            # _event_source_card deliberately answers None for source 'chara'
            # because a trainee card id is not a support card id and must never
            # be looked up in the support tables for event_boost.
            end_chain(full_state, chain_owner)
        elif t == "condition":
            cond = eff.get("condition_id")
            # Fast Learner is a CHANCE outcome, not a guaranteed one: the
            # events that offer it hand out their stats/mood/SP every time but
            # only sometimes grant the condition itself (live-reported ~10%).
            # Modeled here rather than per-event so it holds wherever the
            # condition is offered.
            if cond == _FAST_LEARNER_CONDITION and random.random() >= _FAST_LEARNER_CHANCE:
                continue
            # Practice Perfect is NOT a single global chance -- it varies per
            # event (user-confirmed 2026-08-16): a support card's non-random
            # choice branch ("the only possible outcome") grants it every
            # time, same as any other effect on that branch, while some
            # trainee events genuinely roll for it. A blanket 20% here (mis-
            # generalized from ONE user-reported trainee event to EVERY
            # condition-10 grant, support cards included) silently threw the
            # condition away on most support-card events that actually
            # guarantee it -- exactly the "shown in the outcome, never
            # actually granted" bug reported live. GameTora's own data has no
            # `chance` key on any of these (support or trainee), so the
            # correct default is the same one every OTHER effect type already
            # uses: guaranteed unless this choice is a random_either
            # (handled by _choice_effect_groups/apply_choice) or the effect
            # carries an explicit `chance`, per skill_hint/heal_status below.
            chance = eff.get("chance")
            if cond == _COND_PRACTICE_PERFECT and chance is not None \
                    and random.random() >= float(chance):
                continue
            if cond:
                single_mode_events.add_condition(chara_info, cond)
        elif t == "heal_status":
            # `chance` present -> a roll; absent -> always, as before. BUG
            # FIXED 2026-08-25: this called a local _heal_conditions() that
            # cleared EVERY bad condition at once, contradicting the display
            # line it's paired with (display 16 "Randomly cures 1 bad
            # condition(s)", effect_value_0=1 -- see _encode_effect). Reusing
            # conditions.cure_one_negative (the same "cure exactly one" model
            # already established for this exact wording) instead.
            chance = eff.get("chance")
            if chance is not None and random.random() >= float(chance):
                continue
            conditions.cure_one_negative(chara_info)
        elif t in ("highest_stat", "lowest_stat", "last_stat", "random_stats"):
            _apply_dynamic_stat(chara_info, t, _parse_value(eff.get("value")),
                                 int(eff.get("stat_count") or 1))
        elif t in ("mt", "performance_tokens"):
            # "mt" is the raw GameTora code, unmapped in gametora._CODE_TYPE --
            # Grand Live "Performance Points to the lowest category" (Light
            # Hello's "Another Day's Hard Work!", user-confirmed 2026-08-20).
            # "performance_tokens" is the SAME real effect under a different
            # name: event_engine._card_events merges the datamined events.json
            # index UNDER the GameTora per-card cache, but the two sources key
            # this event by DIFFERENT titles (events.json has it under the raw
            # JP title, GameTora under the English translation) -- so both
            # survive the merge as separate dict entries, and event_by_story_id
            # returns whichever is iterated first. That is frequently the
            # datamined entry (inserted first in _load()), which hand-names
            # this same code "performance_tokens" instead of "mt". Root-caused
            # 2026-08-26 after the +20 kept silently vanishing: guts/skill_
            # points/bond on the same choice applied fine (recognized types),
            # only this one was dropped, because nothing matched "performance_
            # tokens" below. Aliasing it here (rather than fixing merge order,
            # which risks reshuffling every other card's event resolution) is
            # the minimal fix -- whichever source wins now applies the same.
            #
            # full_state is optional here (career_events.py's apply_choice
            # call doesn't have one to give): silently skipped rather than
            # raising when absent, same as every other effect that can't
            # apply without more context than chara_info alone carries.
            if full_state is not None:
                from . import scenarios
                amount = _parse_value(eff.get("value"), 20)
                gain = scenarios.for_chara(chara_info).apply_currency_gain(
                    full_state, chara_info, amount)
                # BUG FIXED 2026-08-24 (live-reported: the token WAS granted --
                # confirmed correct by the user -- but the outcome screen never
                # showed it, unlike every other effect on the same choice).
                # _encode_gains only ever sees the static effect dict, which
                # can't name a category that's resolved live right above --
                # so the display entry has to be built HERE, once we actually
                # know which category won, not in _encode_effect.
                if gain is not None:
                    wire_target, gained = gain
                    extra_gains.append(_G(1, wire_target, gained))
        # race_rewards / date_*: ignored
    return extra_gains


def deck_position_for_chara(chara_info: dict, char_id) -> int | None:
    """The deck slot (evaluation_info_array target_id) of the support card that
    features char_id, or None if that character isn't in the deck. Bond
    (friendship) can only be gained with deck members -- an event featuring a
    non-deck character grants no bond."""
    if not char_id:
        return None
    for c in chara_info.get("support_card_array") or ():
        if card_chara_id(c.get("support_card_id")) == char_id:
            return c.get("position")
    return None


def _apply_bond(chara_info: dict, char_id, amount: int) -> None:
    """Raise the featured deck member's bond (evaluation) toward its 100 cap.
    No-op if the character isn't in the deck (you can't befriend them)."""
    pos = deck_position_for_chara(chara_info, char_id)
    if pos is None:
        return
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") == pos:
            e["evaluation"] = max(0, min(100, e.get("evaluation", 0) + amount))
            return


def _apply_npc_bond(chara_info: dict, target_id, amount: int) -> None:
    """Raise a scenario NPC's bond. Their evaluation row is keyed by target_id
    directly (102 Director / 103 Reporter / 2001 Meek) rather than by a deck
    position, so _apply_bond's card lookup can't reach them. No-op when the row
    is absent -- that means the NPC hasn't been unlocked yet."""
    if not target_id:
        return
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") == target_id:
            e["evaluation"] = max(0, min(100, e.get("evaluation", 0) + amount))
            return


_STAT_KEYS_ALL = ("speed", "stamina", "power", "guts", "wiz")


def _apply_dynamic_stat(chara_info: dict, kind: str, value: int, count: int) -> None:
    """Apply a stat gain whose target the client resolves at runtime: the
    highest/lowest stat, the last-trained stat (approximated by the highest), or
    N random stats."""
    def cur(k):
        return chara_info.get(k, 0)

    def bump(k):
        cap = chara_info.get("max_wiz" if k == "wiz" else f"max_{k}", 9999)
        chara_info[k] = max(0, min(cap, cur(k) + value))

    keys = list(_STAT_KEYS_ALL)
    if kind == "random_stats":
        targets = random.sample(keys, min(max(1, count), len(keys)))
    elif kind == "lowest_stat":
        targets = [min(keys, key=cur)]
    else:  # highest_stat / last_stat
        targets = [max(keys, key=cur)]
    for k in targets:
        bump(k)


# ------------------------------------------------- support card hint pools --
# single_mode_hint_gain IS the '!' pool. One row per (support_card_id,
# hint_group): hint_gain_type 0 is a skill hint (hint_value_1 = skill_id) and
# hint_gain_type 1 is the stat variant (hint_value_1 = a target_type, 1-5 for
# the stats and 30 for skill points; hint_value_2 = the amount), which is why a
# stat group has two or three rows sharing a hint_group.
#
# THIS REPLACES available_skill_set. That table is the TRAINEE card's learnable
# pool -- every skill the character can ever buy, golds included -- and reading
# a support card's hints out of it is what gave Tokai Teio's card a gold hint
# for a skill that is not in her pool at all (user-reported 2026-09-06). Her
# real pool is nine skills, 200432/200532/200542/200552/200712/201242/201252/
# 201272/201522, and every one of them is rarity 1. 238 of the game's support
# cards have rows here; the ones that do not are pals and group cards, which
# genuinely have no hints (GameTora says so too).
HINT_STAT_TARGETS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz",
                     30: "skill_point"}


@functools.lru_cache(maxsize=None)
def card_hint_groups(support_card_id) -> tuple:
    """This support card's hint pool, one entry per hint_group.

    Each entry is ("skill", skill_id) or ("stat", ((effect, ...))) -- the
    effects already in this module's own vocabulary, so a caller can hand them
    straight to apply_choice."""
    if not support_card_id:
        return ()
    rows = master_data.query(
        "SELECT hint_group, hint_gain_type, hint_value_1, hint_value_2 "
        "FROM single_mode_hint_gain WHERE support_card_id=? "
        "ORDER BY hint_group, id", (int(support_card_id),))
    groups, order = {}, []
    for row in rows:
        key = int(row["hint_group"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    for key in order:
        rows = groups[key]
        if int(rows[0]["hint_gain_type"] or 0) == 0:
            out.append(("skill", int(rows[0]["hint_value_1"])))
            continue
        effects = []
        for row in rows:
            field = HINT_STAT_TARGETS.get(int(row["hint_value_1"] or 0))
            if field:
                effects.append({"type": field,
                                "value": "+%d" % int(row["hint_value_2"] or 0)})
        if effects:
            out.append(("stat", tuple(effects)))
    return tuple(out)


def card_hint_skills(support_card_id) -> tuple:
    """Just the skill ids in this card's hint pool."""
    return tuple(v for kind, v in card_hint_groups(support_card_id)
                 if kind == "skill")


def _apply_skill_hint(chara_info: dict, skill_id, level: int) -> None:
    """Raise a hint keyed by the skill's REAL (group_id, rarity).

    Both were previously wrong for gold skills: the rarity was hardcoded to 1
    and the match ignored it, so a gold hint landed on -- and then kept
    levelling -- its WHITE group-mate's entry. Every SSR chain finale that
    hands out the card's gold skill (Fine Motion SSR's Speed Star, 200581)
    granted the white one instead (Prepared to Pass, 200582); a card whose
    branch can hand out either of the pair could never give the gold half.
    See master_data.skill_tip_key."""
    if not skill_id:
        return
    group_id, rarity = master_data.skill_tip_key(skill_id)
    tips = chara_info.setdefault("skill_tips_array", [])
    for t in tips:
        if t.get("group_id") == group_id and t.get("rarity") == rarity:
            t["level"] = min(5, t.get("level", 1) + level)
            return
    tips.append({"group_id": group_id, "rarity": rarity, "level": max(1, level)})


def apply_choice(chara_info: dict, event: dict, choice_number: int,
                 default_char_id=None, source_card_id=None,
                 full_state: dict | None = None, chain_owner=None,
                 branch=None) -> dict:
    """Commit a choice: apply its effects to chara_info in place and return the
    resulting {choice_reward_array} for that choice.

    `branch` is the 1-based story branch this choice was SERVED with -- the
    select_index off the choice_array entry the client is looking at right now
    (see _choice_branch_index for why that value, not a roll here, is the
    gamble's result). Pass it and the numbers the player gets are the ones
    belonging to the text the client is playing. It is optional only because a
    handful of internal callers apply an event they never served (the
    end-of-run sendoffs, scenario pay-outs, _resolve_* helpers); those still
    roll here, which is correct since no story is watching."""
    choices = event.get("choices") or []
    idx = max(1, choice_number) - 1
    if idx >= len(choices):
        idx = 0
    if not choices:
        return {"choice_reward_array": []}
    choice = resolve_or_effects(resolve_segments(choices[idx], chara_info))
    groups = _choice_effect_groups(choice)
    if len(groups) > 1:
        picked = None
        if branch:
            # Back out the flat served index to an outcome of the segment that
            # applies. Out of range means the branch belongs to some other
            # shape of this choice (a cache refreshed mid-career, a guard that
            # now evaluates differently) -- roll rather than pay the wrong arm.
            picked = int(branch) - 1 - _branch_offset(choices[idx], chara_info)
            if not 0 <= picked < len(groups):
                log.warning("event choice %s: served branch %s out of range for "
                            "%s outcomes; re-rolling", choice_number, branch, len(groups))
                picked = None
        if picked is None:
            picked = _roll_branch(choice.get("probs"), len(groups))
        effects = groups[picked]
    else:
        effects = groups[0]
    effects = boost_effects(effects, source_card_id)
    # Snapshot what is already at its ceiling BEFORE applying -- see not_up_info.
    before = not_up_snapshot(chara_info)
    extra_gains = _apply_effects(chara_info, effects, default_char_id, full_state,
                                 source_card_id, chain_owner)
    gains = _encode_gains(effects, chara_info, default_char_id) + extra_gains
    # `not_up` rides along with the reward for the caller to put on the response
    # (not_up_parameter_info) -- that is what makes the client say "<stat> is in
    # superb form" / "Energy is full." instead of silently showing nothing.
    return {"choice_reward_array": [{"select_index": idx + 1, "gain_param_array": gains}],
            "not_up": not_up_info(gains, before)}


# The two New Year beats. USER-SUPPLIED real values (2026-07-26) -- these are
# ground truth from the live game, not derived, because the events appear in no
# datamined/GameTora cache. An earlier invented set (stat+10 & SP+15 /
# energy+20 / mood+1 & SP+10) was live-reported wrong; do not re-guess these.
#
#   'new_year'   story suffix 101, turn 25 (Junior -> Classic)
#       1. <the trainee's own type stat> +25   2. Energy +20   3. SP +20
#   'new_year_2' story suffix 102, turn 49 (Classic -> Senior)
#       1. Energy +30   2. ALL five stats +8   3. SP +35
#
# The per-trainee stat in the first event is character_event_data's
# special_event_meta.new_years_resolution_stat (present for all 80 charas).
#
# 'summer_camp' story suffix 104, turn 40 -- USER-SUPPLIED (2026-07-27, with a
# screenshot): TWO choices, Power +10 / Guts +10. Year 2 ONLY: master has
# suffix 104 'At Summer Camp (Year 2)' for 83 trainees, 105 is 'Summer Camp
# (Year 3) *Begins!*', and no "At Summer Camp (Year 3)" exists anywhere.
# UNIVERSAL, not per-trainee (user-confirmed): GameTora's character pages carry
# per-trainee stat codes for exactly two of the ten Special Events -- `nyear`
# (one code, the New Year stat above) and `dance` (two codes, Dance Lesson) --
# and camp has no such field, which is what "same for everyone" looks like in
# that data. If a trainee ever shows a different pair, this becomes a per-chara
# table like new_years_resolution_stat.
#
# 'dance_lesson' story suffix 506 -- USER-SUPPLIED (2026-07-27): same shape as
# the camp event, TWO options at +10 each, but the two STATS are per trainee
# and come from GameTora's `dance` codes (present on all 260 pages).
#
# 'fan_letter' story suffix 507 -- USER-SUPPLIED: Mood +1, SP +30 (one option).
# 'extra_training' suffix 715 -- USER-SUPPLIED: two options. Top = +5 to the
#   stat of the FACILITY JUST TRAINED and -5 energy, with a 20% chance to cure
#   a debuff; bottom = +5 energy. Needs the facility, hence `context`.
# 'acupuncture' suffix 720 -- USER-SUPPLIED: five options, four of them a
#   weighted coin-flip between a good and a bad outcome.
_SEASONAL_KINDS = ("new_year", "new_year_2", "summer_camp", "dance_lesson",
                   "fan_letter", "extra_training", "acupuncture")

# Shared "Special Event" story suffix -> seasonal kind, for the ones whose
# effects we actually know. These events are NOT in any event cache (no shared
# event id appears on a character page), so without an entry here they resolve
# to nothing and are silently skipped -- which is why Dance Lesson never fired.
# Master Trainer (500) is deliberately absent: it IS on the pages, with the
# per-trainee stat and its Practice Perfect condition already in the data.
SHARED_EVENT_KINDS = {506: "dance_lesson", 507: "fan_letter",
                      715: "extra_training", 720: "acupuncture"}

# Skills the acupuncturist can hand out (text_data category 47).
_SKILL_CORNER_RECOVERY = 200352
_SKILL_STRAIGHT_RECOVERY = 200382


def shared_event(story_suffix: int, card_id, context: dict | None = None) -> dict | None:
    """The synthesized event for a SHARED beat that lives in the trainee's
    random band but has no cached event data. None when we have no values for
    it, so the caller skips rather than serving an empty event."""
    kind = SHARED_EVENT_KINDS.get(story_suffix)
    return seasonal_event(kind, card_id, context) if kind else None


def seasonal_event(kind: str, card_id, context: dict | None = None) -> dict | None:
    """A SEASONAL career event's choices. These are real choice events in game
    but appear in NO datamined/GameTora event cache (verified: no shared-event
    id is present on any character page), so their effects come from the table
    above. Returns None for any kind we have no values for, so the caller falls
    back to narration-only rather than inventing rewards."""
    if kind not in _SEASONAL_KINDS:
        return None
    if kind == "summer_camp":
        return {"choices": [
            {"effects": [{"type": "power", "value": "+10"}]},
            {"effects": [{"type": "guts", "value": "+10"}]},
        ]}
    if kind == "dance_lesson":
        # Two options at +10, same as camp, but the STATS are per trainee --
        # GameTora's `dance` pair, e.g. Special Week ["gu","st"].
        codes = (special_event_stats(card_id) or {}).get("dance") or ()
        stats = [SPECIAL_STAT_NAMES.get(c) for c in codes]
        stats = [s for s in stats if s]
        if len(stats) < 2:
            return None      # no codes for this trainee -> don't invent a pair
        return {"choices": [{"effects": [{"type": s, "value": "+10"}]}
                            for s in stats[:2]]}
    if kind == "fan_letter":
        return {"choices": [{"effects": [{"type": "mood", "value": "+1"},
                                         {"type": "skill_points", "value": "+30"}]}]}
    if kind == "extra_training":
        stat = (context or {}).get("stat")
        if not stat:
            return None          # without the facility we'd guess the stat
        # display_id 19 overrides the shown line to text_data category 394's
        # "Previously trained attribute +{0}" (live-reported 2026-08-24: this
        # was showing the literal stat name/icon, e.g. "Speed +5", instead of
        # the real client's generic phrasing for a facility-relative gain --
        # same override mechanism the race events already use for "Stat
        # gains based on race grade"). APPLY is unchanged: `type: stat` still
        # adds +5 to the real facility-specific stat; only the display line
        # differs.
        base_effects = [{"type": stat, "value": "+5",
                         "display_id": 19, "display_target": 5},
                        {"type": "energy", "value": "-5"}]
        # User-reported 2026-08-25 (screenshot, real GameTora page): the top
        # choice is a random_either with TWO full outcome branches -- both
        # carry a friendship gain with the scenario's companion NPC (the
        # base/URA scenario's Reporter, Otonashi -- single_mode_events.py's
        # NPC_UNLOCKS target 103/chara_id 9003) plus the stat/energy above;
        # only the SECOND branch (20%) additionally cures a bad condition
        # ("Randomly cures 1 bad condition(s)", display 16). This was
        # previously modeled as ONE choice with an invisible ad hoc `chance`
        # tag on heal_status, which can't reproduce the real client's two
        # separate branch previews. Other scenarios' equivalent companion (if
        # any) isn't verified -- only scenario_id 1 gets the bond, everyone
        # else just gets stat+energy until a capture pins the rest.
        if (context or {}).get("scenario_id") == 1:
            # target_id 103 / chara_id 9003 -- both needed: APPLY
            # (_apply_npc_bond) keys off target_id, the preview icon
            # (_encode_effect's npc_bond branch) keys off chara_id.
            base_effects = [{"type": "npc_bond", "target_id": 103, "chara_id": 9003,
                             "value": "+5"}] + base_effects
        no_cure = list(base_effects)
        with_cure = base_effects + [{"type": "heal_status"}]
        return {"choices": [
            {"outcomes": [no_cure, with_cure],
             "probs": [round(100 * (1 - _EXTRA_TRAINING_CURE_CHANCE)),
                       round(100 * _EXTRA_TRAINING_CURE_CHANCE)],
             "random_either": True},
            {"effects": [{"type": "energy", "value": "+5"}]},
        ]}
    if kind == "acupuncture":
        return _acupuncture_event()
    if kind == "new_year_2":
        return {"choices": [
            {"effects": [{"type": "energy", "value": "+30"}]},
            {"effects": [{"type": "all_stats", "value": "+8"}]},
            {"effects": [{"type": "skill_points", "value": "+35"}]},
        ]}
    _load()
    # GameTora's per-trainee `nyear` code FIRST: it covers all 260 trainee
    # cards, where character_event_data's special_event_meta covers 142 -- the
    # other 118 were silently served a hardcoded 'speed', i.e. 45% of trainees
    # got the wrong stat on this event.
    stat = SPECIAL_STAT_NAMES.get((special_event_stats(card_id) or {}).get("nyear"))
    if not stat:
        meta = ((_CHARA_INDEX.get(str(card_id)) or {}).get("__meta__")
                or _chara_meta(card_id) or {})
        stat = meta.get("new_years_resolution_stat") or "speed"
    return {"choices": [
        {"effects": [{"type": stat, "value": "+25"}]},
        {"effects": [{"type": "energy", "value": "+20"}]},
        {"effects": [{"type": "skill_points", "value": "+20"}]},
    ]}


_EXTRA_TRAINING_CURE_CHANCE = 0.20   # user-supplied: top option's debuff cure


def _acupuncture_event() -> dict:
    """The acupuncturist (story suffix 720). USER-SUPPLIED odds and effects.

    Four of the five options are a weighted gamble between a good and a bad
    outcome, which is exactly what `random_either` + `probs` models -- the
    client shows BOTH branches on the outcome screen, as the real one does.
    Option 5 is the flat, safe one."""
    def gamble(good, good_pct, bad):
        return {"outcomes": [good, bad], "probs": [good_pct, 100 - good_pct],
                "random_either": True}

    return {"choices": [
        gamble([{"type": "all_stats", "value": "+20"}], 30,
               [{"type": "mood", "value": "-2"},
                {"type": "all_stats", "value": "-15"},
                {"type": "condition", "condition_id": _COND_NIGHT_OWL}]),
        gamble([{"type": "obtain_skill", "skill_id": _SKILL_CORNER_RECOVERY, "value": 1},
                {"type": "obtain_skill", "skill_id": _SKILL_STRAIGHT_RECOVERY, "value": 1}], 45,
               [{"type": "energy", "value": "-20"}, {"type": "mood", "value": "-2"}]),
        gamble([{"type": "max_energy", "value": "+12"},
                {"type": "energy", "value": "+40"},
                {"type": "heal_status"}], 70,
               [{"type": "energy", "value": "-20"}, {"type": "mood", "value": "-2"},
                {"type": "condition", "condition_id": _COND_PRACTICE_POOR}]),
        gamble([{"type": "energy", "value": "+20"}, {"type": "mood", "value": "+1"},
                {"type": "condition", "condition_id": _COND_CHARMING}], 85,
               [{"type": "energy", "value": "-10"}, {"type": "mood", "value": "-1"},
                {"type": "condition", "condition_id": _COND_PRACTICE_POOR}]),
        {"effects": [{"type": "energy", "value": "+10"}]},
    ]}


# Condition ids used by the synthesized shared events (see handlers.conditions).
_COND_NIGHT_OWL = 1
_COND_PRACTICE_POOR = 6
_COND_CHARMING = 8
_COND_PRACTICE_PERFECT = 10


def _chara_meta(card_id) -> dict | None:
    """special_event_meta for a trainee card (per-chara seasonal parameters)."""
    _load()
    path = os.path.join(_DATA_DIR, "character_event_data.json")
    global _CHARA_META
    if _CHARA_META is None:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            _CHARA_META = {k: (v or {}).get("special_event_meta") or {}
                           for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            _CHARA_META = {}
    meta = _CHARA_META.get(str(card_id))
    if meta:
        return meta
    # Alt cards (summer/NY variants) aren't all in the cache -- fall back to
    # any card of the SAME character, same as the event lookup does.
    prefix = str((int(card_id) // 100))
    for cid, m in _CHARA_META.items():
        if cid.startswith(prefix) and m:
            return m
    return None


_CHARA_META: dict | None = None


def resolve_source(source: str, source_id, title: str | None) -> dict | None:
    """Resolve an event directly by its data source (unambiguous), used by the
    preview/commit path which stores (source, source_id, title) in context.
    source is 'support' (source_id = card_id) or 'chara' (source_id = trainee
    card_id), or 'seasonal' (source_id = trainee card_id, title = the seasonal
    kind, e.g. 'new_year'). GameTora first, datamined fallback."""
    _load()
    if source == "seasonal":
        return seasonal_event(title, source_id)
    key = _norm(title)
    if not key:
        return None
    if source == "support":
        gt = gametora.load_cached("support", source_id)
        if gt and key in gt:
            return gt[key]
        return (_SUPPORT_INDEX.get(str(source_id)) or {}).get(key)
    gt = gametora.load_cached("chara", source_id)
    if gt and key in gt:
        return gt[key]
    ev = (_CHARA_INDEX.get(str(source_id)) or {}).get(key)
    if ev:
        return ev
    for cid, evs in _CHARA_INDEX.items():
        if key in evs:
            return evs[key]
    return None


_card_chara_cache: dict = {}


def card_chara_id(card_id: int) -> int | None:
    """The character a support card features (support_card_data.chara_id) -- its
    RANDOM events live under story 80<chara>nnn."""
    if card_id not in _card_chara_cache:
        rows = master_data.query("SELECT chara_id FROM support_card_data WHERE id=?", (card_id,))
        chara = next((r["chara_id"] for r in rows), None)
        # Cards released after this master.mdb have NO support_card_data row, so
        # chara came back None and their RANDOM events (story 80<chara>nnn) were
        # looked up under base 800000000 -- an empty window, so every one of them
        # was silently dropped. 57 otherwise-fine cards resolved zero events that
        # way. Their bond effects name the character directly, so recover it from
        # there rather than losing the card entirely.
        if chara is None:
            chara = _chara_id_from_events(card_id)
        _card_chara_cache[card_id] = chara
    return _card_chara_cache[card_id]


def _chara_id_from_events(card_id: int) -> int | None:
    """The character a card features, read off its own events' bond effects
    ({"type": "bond", "char_id": 1068}). Fallback for cards missing from
    support_card_data; the most-named character wins."""
    counts: dict = {}
    for ev in (_card_events(card_id) or {}).values():
        for choice in ev.get("choices") or []:
            for eff in choice.get("effects") or []:
                cid = eff.get("char_id")
                if cid:
                    counts[cid] = counts.get(cid, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _title_key(text: str) -> str:
    """Normalized title for matching. master.mdb wraps long story titles with
    literal newlines ('Emulation Is the Sincerest Form of\\nFlattery') and uses
    typographic apostrophes/quotes where GameTora uses ASCII, so an exact string
    compare silently dropped those events (they then fell back to a
    choice-less acknowledge event, losing their real rewards)."""
    for a, b in (("\\n", " "), ("\\r", " "), ("\r", " "), ("\n", " "),
                 ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("♪", ""), ("☆", ""), ("★", ""),
                 # fullwidth punctuation (JP source text) vs ASCII (GameTora) --
                 # Light Hello's 'Embrace Those Emotions！' failed to resolve
                 ("！", "!"), ("？", "?"), ("：", ":"), ("；", ";"),
                 ("（", "("), ("）", ")"), ("～", "~"), ("〜", "~"),
                 ("　", " "), ("・", " ")):
        text = text.replace(a, b)
    return " ".join(text.split()).casefold()


@functools.lru_cache(maxsize=4096)
def _titles_in_window(base: int, span: int) -> tuple:
    return tuple((r["idx"], _title_key(r["text"] or "")) for r in master_data.query(
        "SELECT [index] AS idx, text FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<?", (base, base + span)))


# JP title -> the EN title master.mdb (an EN client) uses. Every data source we
# have for Light Hello's pal card (30052 / chara 9008) is JP-named -- the only
# such card -- and there is no JP master to join through, so the bridge is this
# hand-verified table. Keys/values are matched through _title_key.
_TITLE_ALIASES = {
    "ちゃっかりリサーチ♪": "Some Crafty Research ♪",
    "イベント企画のライトハロー": "Light Hello, the Event Planner",
    "お疲れ様です……！": "Another Day's Hard Work!",
    "想いの力、受け止めて……！": "Embrace Those Emotions!",
    "嵐の大洋で、まどろみ": "Repose in the Lunar Mare",
    "ティコの輝き": "Tycho's Radiance",
    "レゴリスで隠して": "Hidden Beneath the Regolith",
    "ホイヘンス山、越えし君": "Scaling Mons Huygens",
    "有明を共に歩む": "Dawn Under the Waning Moon",
    "満ちる時まで": "Until It Shines in Full",
    "ハローとの絆・きょうはぶれいこー": "Bonding with Light Hello: Letting Loose",
}
_ALIAS_KEYS: dict | None = None


def _alias_key(key: str) -> str | None:
    global _ALIAS_KEYS
    if _ALIAS_KEYS is None:
        _ALIAS_KEYS = {_title_key(jp): _title_key(en)
                       for jp, en in _TITLE_ALIASES.items()}
    return _ALIAS_KEYS.get(key)


def _untranslated_ids(base: int, span: int = 1000) -> tuple:
    """Story ids in a window whose master title is BLANK. Cards released after
    this master.mdb was pulled have their story ids but no localized titles, so
    nothing can ever match them by name -- see _pair_untranslated."""
    return tuple(r["idx"] for r in master_data.query(
        "SELECT [index] AS idx FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<? AND TRIM(COALESCE(text,''))='' "
        "ORDER BY [index]", (base, base + span)))


def _pair_untranslated(card_id: int, chara: int | None, leftovers: list) -> dict:
    """{gametora_event: story_id} for events no title lookup could place.

    Newer cards carry JP titles on every source we have while master.mdb has the
    story ids with EMPTY titles, so name matching is structurally impossible and
    ~60 real cards resolved to zero events -- own a fresh SSR and it stayed
    silent for the whole run. Both sides are ordered, so pair them by position:
    master's ids ascending against GameTora's `id` ascending.

    The one thing position alone can't tell us is which leftovers are chain
    (story 8<card>nnn) and which are random (80<chara>nnn). GameTora's `id`
    settles it: across all 152 cards where both buckets resolve by title, every
    chain id outranks every random id, with no interleaving -- so the highest
    ids take the chain slots. Applied ONLY when the counts line up exactly on
    both sides; a partial match means an assumption broke, and mispairing would
    hand an event the wrong scene's rewards, so we drop it instead."""
    leftovers = [ev for ev in leftovers if ev.get("id") is not None]
    if not leftovers:
        return {}
    chain_ids = _untranslated_ids(800000000 + card_id * 1000)
    rand_ids = _untranslated_ids(800000000 + chara * 1000) if chara else ()
    if len(leftovers) != len(chain_ids) + len(rand_ids) or not (chain_ids or rand_ids):
        return {}
    ordered = sorted(leftovers, key=lambda e: e["id"])
    paired = {}
    for ev, sid in zip(ordered[len(ordered) - len(chain_ids):], chain_ids):
        paired[id(ev)] = sid
    for ev, sid in zip(ordered[:len(rand_ids)], rand_ids):
        paired[id(ev)] = sid
    # NOTE: pairing deliberately runs over the FULL id lists, including ids with
    # no playable story -- the positional alignment depends on it, and dropping
    # them here would shift every later event onto the wrong scene. Unplayable
    # ids are filtered at the END of support_events instead (see the asset
    # guard there), which also catches the ones plain title matching produces.
    return paired


@functools.lru_cache(maxsize=8192)
def _story_row_exists(story_id: int) -> bool:
    """Whether master has a real single_mode_story_data row for this id (the
    same check single_mode_team._story_exists makes; duplicated here to keep
    the engine free of a handler import)."""
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)))


def _story_id_by_title(title: str, base: int, span: int = 1000) -> int | None:
    """The story id for an event title within a [base, base+span) window (used to
    map a GameTora event to its real story id). GameTora's English names equal
    master.mdb's text_data (category 181) up to whitespace/punctuation styling,
    so matching is done on a normalized key (see _title_key); JP-only sources go
    through _TITLE_ALIASES."""
    key = _title_key(title or "")
    if not key:
        return None
    hit = next((idx for idx, t in _titles_in_window(base, span) if t == key), None)
    if hit is None:
        alias = _alias_key(key)
        if alias:
            hit = next((idx for idx, t in _titles_in_window(base, span) if t == alias), None)
    return hit


_support_events_cache: dict = {}


def _card_events(card_id: int) -> dict:
    """A support card's {key: event} map: the GameTora per-card cache merged
    OVER the bulk events.json fallback index. The merge (rather than or-else)
    matters: old per-card caches of JP-titled pages collapsed all events onto
    the key '' (see gametora._norm_title), so the cache alone can be a
    one-event husk while the fallback still has the full set."""
    _load()
    merged = dict(_SUPPORT_INDEX.get(str(card_id)) or {})
    for k, v in (gametora.load_cached("support", card_id) or {}).items():
        merged[k or f"__cache_{len(merged)}"] = v
    return merged


def support_events(card_id: int):
    """(chain, random) event lists for a support card, each [(story_id, title,
    event)]. Ground truth for chain-vs-random is the story-id range: chain events
    sit at 8<card>nnn, random events at 80<chara>nnn. Events with no resolvable
    story id (unfireable) are dropped. Chain is ordered by story id.

    PAL/GROUP cards' outing material is EXCLUDED here (pal_cards.is_reserved):
    their unlock event, the outing chain and the finale must only ever reach the
    player through the outing system, never the generic coincidence pull
    (live-reported: outing-chain events were being served at random). Note pal
    cards' stories live at 80<chara>nnn, so they land in the RANDOM bucket,
    while group cards' live at 8<card>nnn and land in the CHAIN bucket -- both
    have to be filtered, which is why the check is applied to each branch."""
    if card_id in _support_events_cache:
        return _support_events_cache[card_id]
    from .handlers import pal_cards
    gt = _card_events(card_id)
    chara = card_chara_id(card_id)
    # The same event can reach us twice (once keyed by title from the bulk
    # index, once as a '__cache_N' husk from a collapsed per-card cache), which
    # used to put duplicate entries in the pools.
    seen_titles, uniq = set(), []
    for ev in gt.values():
        key = _title_key(ev.get("name") or "")
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        uniq.append(ev)

    chain, rand, unplaced = [], [], []
    for ev in uniq:
        title = ev["name"]
        sid = _story_id_by_title(title, 800000000 + card_id * 1000)
        if sid:
            if not pal_cards.is_reserved(card_id, sid):
                chain.append((sid, title, ev))
            continue
        sid = _story_id_by_title(title, 800000000 + chara * 1000) if chara else None
        if sid:
            if not pal_cards.is_reserved(card_id, sid):
                rand.append((sid, title, ev))
            continue
        unplaced.append(ev)

    # Untranslated cards: nothing matched by name, so place what's left by
    # position (see _pair_untranslated).
    placed = _pair_untranslated(card_id, chara, unplaced)
    own_band = (800000000 + card_id * 1000) // 1000
    for ev in unplaced:
        sid = placed.get(id(ev))
        if not sid or pal_cards.is_reserved(card_id, sid):
            continue
        (chain if sid // 1000 == own_band else rand).append((sid, ev["name"], ev))

    # ASSET GUARD, applied to EVERY resolution path (2026-07-28). A story id
    # existing in text_data does not mean the story is playable -- master keeps
    # title rows for content this .mdb predates, and serving one tells the
    # client to play an asset that isn't there, which softlocks it mid-cutscene
    # (it stuck a live career on card 30130; the same class of bug softlocked
    # the debut twice). Both the positional pairing AND plain title matching can
    # produce these: card 30135 resolves 830135001 by title with no story row.
    # Cost of the guard, measured: 966 -> 774 events over the Global pool and 2
    # -> 19 cards serving nothing. Those 192 events were never playable. The fix
    # for THAT is a newer master.mdb, not a looser guard here.
    chain = [t for t in chain if _story_row_exists(t[0])]
    rand = [t for t in rand if _story_row_exists(t[0])]
    chain.sort(key=lambda t: t[0])
    rand.sort(key=lambda t: t[0])
    _support_events_cache[card_id] = (chain, rand)
    return chain, rand


def event_by_story_id(card_id: int, sid: int):
    """(title, event) for one of a support card's events by story id -- the
    lookup the outing system needs, since those events are deliberately absent
    from support_events()."""
    gt = _card_events(card_id)
    for ev in gt.values():
        title = ev.get("name")
        if not title:
            continue
        for base in (800000000 + card_id * 1000, 800000000 + (card_chara_id(card_id) or 0) * 1000):
            if _story_id_by_title(title, base) == sid:
                return title, ev
    return None, None


_all_support_ids_cache: list | None = None


def all_support_card_ids() -> list:
    """Every support card we have a GameTora dump for AND that's actually
    backed by a real master.mdb support_card_data row (the pool for random,
    not-necessarily-in-deck support events).

    GameTora's dump directory carries some ids (confirmed: 30135 'Katsuragi
    Ace', 30296) with no matching support_card_data row at all -- likely a
    card not yet in this game's data (future/other-region release the scrape
    picked up regardless). Picking one of those for a random event queues an
    event_contents_info.support_card_id the client can't find any real card
    data for, which froze the client on a black screen mid-career (confirmed
    live: card 30135, story 830135001 'By the People', was the exact event on
    screen when it happened) -- so these must be filtered out here, at the
    source every caller (support_events' random branch, _maybe_fire_support_event)
    draws from, rather than patched at each call site.

    BUG FIXED 2026-09-03 (live-reported: an event's portrait/rarity badge
    sometimes rendering broken/yellow instead of freezing outright -- a
    SOFTER version of the exact failure above). This used to filter on
    card_chara_id(cid), which has its OWN fallback (_chara_id_from_events)
    that recovers a chara id straight from a card's GameTora bond effects
    when support_card_data has no row for it -- i.e. it happily returns
    truthy for precisely the no-master.mdb-row cards this function's
    docstring says it excludes. 16 such ids confirmed live (10145, 20099,
    20100, 30297-30309, a contiguous just-past-the-newest-known-id block --
    every appearance of the same "future/other-region release" pattern as
    30135/30296 above, just with event data complete enough for the
    fallback to name a character and slip past the old check). Queries
    support_card_data directly instead, with no fallback, matching what the
    docstring always claimed this did."""
    global _all_support_ids_cache
    if _all_support_ids_cache is None:
        ids = []
        d = os.path.join(gametora._DATA_DIR)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.startswith("support_") and fn.endswith(".json"):
                    try:
                        ids.append(int(fn[len("support_"):-len(".json")]))
                    except ValueError:
                        pass
        with_row = {r["id"] for r in master_data.query(
            "SELECT id FROM support_card_data WHERE id IN ({})".format(
                ",".join("?" * len(ids))), tuple(ids))} if ids else set()
        _all_support_ids_cache = [cid for cid in ids if cid in with_row]
    return _all_support_ids_cache


def event_title(story_id: int) -> str | None:
    """The event's display title (master.mdb text_data category 181)."""
    rows = master_data.query(
        "SELECT text FROM text_data WHERE category=181 AND [index]=?", (story_id,))
    for r in rows:
        return r["text"]
    return None


def support_event_chain(card_id: int, max_events: int = 25) -> list:
    """The ordered [(story_id, title)] of a support card's story events that we
    can actually serve (title exists AND resolves to choices/effects). Story ids
    are 8<card>001, 002, ... -- the game fires them in that order across a run."""
    chain = []
    for idx in range(1, max_events + 1):
        sid = 800000000 + card_id * 1000 + idx
        title = event_title(sid)
        if not title:
            continue
        if resolve(sid, title):
            chain.append((sid, title))
    return chain


# Trainee story-index ranges worth firing as choice events: the 700-block is
# outings/dates; low indices are the character's own story chain. (The fixed
# system events -- race results, infirmary, mood swings -- are handled elsewhere,
# so we only keep story ids that resolve to a real choice event here.)
# Recreation/date events only. The surrounding 7xx band is CONDITION-triggered
# content that must fire from its own trigger, never from an outing:
#   700/701 rest outcomes, 708-711 post-race reactions, 712 slacking off,
#   713/714 sickness ("Get Well Soon!" -- live-reported firing off a
#   recreation, which is why this range is now narrow), 715/716 extra
#   training, 717 infirmary, 718 all-refreshed, 719/720/722 conditions,
#   726-731 crane game, 732/733 inspiration wrappers.
_OUTING_SUFFIXES = range(702, 708)


def chara_outing_chain(trainee_card_id: int, chara_id: int, max_events: int = 40) -> list:
    chain = []
    for idx in list(_OUTING_SUFFIXES):
        sid = 500000000 + chara_id * 1000 + idx
        title = event_title(sid)
        if not title:
            continue
        if resolve(sid, title, trainee_card_id):
            chain.append((sid, title))
    return chain


def _roll_branch(probs, n: int) -> int:
    """Pick a random branch index. Uses GameTora odds when given (a "~90" divider
    -> 0.9); any missing weights share the leftover probability equally; with no
    odds at all it's a uniform pick."""
    weights = list(probs) if probs and len(probs) == n else [None] * n
    known = sum(w for w in weights if isinstance(w, (int, float)))
    unknown = [i for i, w in enumerate(weights) if not isinstance(w, (int, float))]
    if unknown:
        share = max(0.0, 1.0 - known) / len(unknown)
        for i in unknown:
            weights[i] = share
    total = sum(weights) or 1.0
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return n - 1


def choice_count(event: dict) -> int:
    return len(event.get("choices") or []) or 1
