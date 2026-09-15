"""THE career event pipeline: one event type, one queue, one dedupe set, one
resolver, one reward applier.

WHY THIS EXISTS
---------------
Before this module, every event family had invented its own path. Measured on
the code it replaces: 44 state keys, 13 independent "has this fired" sets,
21 functions that could queue an event, 7 distinct places a reward got applied,
and a 478-line check_event with 15 early returns consulting 10 contexts.

That shape produced the same class of bug over and over, because "where does
this event's reward get applied?" had no single answer:

  * a reward hook placed next to the NPC-unlock code was UNREACHABLE, because
    events carrying a ctx returned ~200 lines earlier;
  * the commit path gated on membership of CAREER_EVENT_IDS, so serving a beat
    under its real event id silently paid nothing;
  * choice_array()'s "at least one choice" floor was right for one family and
    wrong for another, and nothing in the data said which -- a choice-less story
    got a phantom button and bootlooped the client.

None of those were logic mistakes in isolation. They were all the same
structural mistake: no single pipeline.

THE MODEL
---------
An Event is DATA, including its reward. It is not a branch.

    Event(event_id, story_id, play_timing,
          choices=[Choice(effects=[...]), ...],   # authoritative -- [] means none
          once_key="unique-per-career identity",  # the ONE dedupe key
          resolver=None)                          # or a named special flow

    PRODUCE  registered producers poll(ctx) and emit() Events
       |
    DEDUPE   emit() drops anything whose once_key already fired
       |
    QUEUE    one priority FIFO; no separate pending/extra/inline lists
       |
    SERVE    wire_entry() builds the unchecked_event_array entry, using the
             event's REAL choice count -- no floor, no guessing
       |
    RESOLVE  resolve() finds the event, applies choices[n].effects through the
             single applier, runs its named resolver if it has one, pops it

Adding an event family means writing a producer that returns Events. It cannot
forget to pay out, because the payout travels with the event; and it cannot be
served with the wrong choice count, because the count IS the choices list.

SPECIAL FLOWS keep bespoke resolvers (a duel needs opponent state, the infirmary
needs the rolled outcome, the crane game needs its minigame result) -- but they
register in RESOLVERS and travel through the same queue, so they are a labelled
extension point rather than 15 nested branches.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import random

from . import event_engine

log = logging.getLogger("uma-server")

STATE_KEY = "career_events"

# Lower serves first. Chosen so the ordering the client expects is expressed as
# data rather than as the order of 21 call sites.
PRIO_FRONT = 0         # jumps everything: the goal-cleared banner must precede
                       # the turn's other events, not trail them
PRIO_SCENARIO = 10     # scenario beats / cutscenes -- they set up everything else
PRIO_UNLOCK = 20       # NPC unlocks; must precede anything that uses the NPC
PRIO_SPECIAL = 30      # duel / training failure / infirmary -- this turn's action
PRIO_NORMAL = 50       # support, outing, random, chara stories
PRIO_TRAILING = 80     # post-race reflections, finales, "after the X" beats


@dataclasses.dataclass
class Choice:
    """One selectable option. `effects` is the event_engine effect vocabulary
    ({"type": "speed", "value": "+10"}, ...), so everything the engine already
    knows how to preview and apply works unchanged."""
    effects: list = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {"effects": list(self.effects)}

    @classmethod
    def from_dict(cls, d: dict) -> "Choice":
        return cls(effects=list((d or {}).get("effects") or []))


@dataclasses.dataclass
class Event:
    """A career event and everything needed to serve AND pay it."""
    event_id: int
    story_id: int
    play_timing: int = 1
    choices: list = dataclasses.field(default_factory=list)
    chara_id: int = 0
    support_card_id: int = 0
    once_key: str | None = None
    source: str = ""
    priority: int = PRIO_NORMAL
    resolver: str | None = None
    payload: dict = dataclasses.field(default_factory=dict)
    show_clear: int = 0
    show_clear_sort_id: int = 0
    minigame_result: dict | None = None
    # A pre-built unchecked_event_array entry, served VERBATIM. This is how the
    # not-yet-converted families ride the unified queue: their producer already
    # builds a correct entry (and pushes its own reward ctx), so wrapping it
    # keeps the wire byte-identical while still giving them one queue, one
    # dedupe set and one drop-on-resolve. They pay through their existing ctx
    # branch, which is why such an Event carries no `choices`.
    raw: dict | None = None
    # A registered HOLD GATE (see hold_gate) that must pass before this event
    # may claim a display slot. Unlike play_timing, a held event is SKIPPED
    # rather than blocking the queue -- see serve_next.
    hold_until: str | None = None
    # "Whatever timing this KIND of response carries" -- see ENDPOINT_TIMING.
    # An event that is simply part of the turn's chain has no timing of its
    # own: the real server stamps one from the response it goes out on, and the
    # same id is observed with two different timings in two different careers
    # depending only on whether that turn had a race. Such an event must never
    # be withheld by TIMING_FORBIDDEN_ENDPOINTS either -- it is servable by
    # construction, because its timing is derived from where it is served.
    timing_from_endpoint: bool = False
    # A DISAMBIGUATING id, used on the wire in place of event_id when this
    # event would otherwise be indistinguishable from another one already
    # queued. 0 means "no collision, serve the real id". See disambiguate().
    wire_event_id: int = 0

    # -- serialization (the queue lives in the per-viewer JSON state) --------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["choices"] = [Choice(**c).to_dict() if isinstance(c, dict) else c.to_dict()
                        for c in self.choices]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d or {})
        d["choices"] = [Choice.from_dict(c) for c in (d.get("choices") or [])]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def wire_entry(self) -> dict:
        """The unchecked_event_array entry.

        A `raw` entry is returned untouched -- an unconverted family already
        built it, and re-deriving it here would change the wire.

        choice_array has EXACTLY one slot per choice -- including none at all.
        The old builder floored this at 1 because a GameTora-sourced event with
        no recorded choices still needs an implicit "OK"; that floor is now the
        producer's job to express (by emitting one empty Choice), not a silent
        default that corrupts genuinely choice-less stories."""
        if self.raw is not None:
            entry = copy.deepcopy(self.raw)
            # A raw entry is served verbatim EXCEPT for a disambiguating id:
            # an unconverted family builds its own dict, so it cannot know it
            # collided with something already queued.
            if self.wire_event_id:
                entry["event_id"] = self.wire_event_id
            return entry
        return {
            "event_id": self.wire_event_id or self.event_id,
            "chara_id": self.chara_id,
            "story_id": self.story_id,
            "play_timing": self.play_timing,
            "event_contents_info": {
                "support_card_id": self.support_card_id,
                "show_clear": self.show_clear,
                "show_clear_sort_id": self.show_clear_sort_id,
                "choice_array": [
                    {"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                     "gain_select_id_index": i + 1, "select_icon": 0}
                    for i in range(len(self.choices))],
                "tips_training_partner_id": None,
            },
            "succession_event_info": None,
            "minigame_result": self.minigame_result,
        }

    def engine_event(self) -> dict:
        """The shape event_engine.apply_choice / choice_reward_array expect."""
        return {"choices": [c.to_dict() for c in self.choices]}


# ============================================================== the state ===

def _blank() -> dict:
    # `seq` makes the queue a true FIFO *within* a priority. Sorting on priority
    # alone let same-priority events reorder relative to the order they were
    # produced, which the list this replaced (a plain append-only list) never
    # did -- it silently swapped two turn-4 events in the baseline.
    return {"queue": [], "fired": [], "active": None, "seq": 0}


# A per-CAREER random salt, drawn once and persisted with the career.
#
# WHY IT EXISTS. Producers are re-polled on every exec_command until the event
# they produced is actually served, so any roll they make has to be STABLE
# across those re-polls -- a fresh roll each time would offer the player a
# different story on the same turn. Every such roll therefore seeds an RNG, and
# the seed everyone reached for was the trainee (`player_chara`,
# `single_mode_chara_id`). That is stable, and it is also the same for every
# career with that trainee: two Special Week runs get the identical "random"
# events on the identical turns, forever. This salt is the missing ingredient --
# stable within a career, different between careers.
#
# It lives in career_events' own state dict, which is already wiped with the
# career (single_mode_team._CAREER_STATE_KEYS lists STATE_KEY), so a finished
# run cannot leak its salt into the next one.
CAREER_SALT_KEY = "salt"


def career_salt(full_state: dict) -> int:
    """This career's random salt, drawn on first use."""
    st = state(full_state)
    salt = st.get(CAREER_SALT_KEY)
    if not salt:
        salt = random.getrandbits(32)
        st[CAREER_SALT_KEY] = salt
    return int(salt)


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, v)
    return st


def reset(full_state: dict) -> None:
    """Career start. ONE key to clear -- the 13 parallel fired-sets this
    replaces were each their own chance to leak into the next career, and one
    (FIRED_EVENTS_KEY) actually did: it accumulated 354 ids across careers, so
    later runs never saw their own deck's events again."""
    full_state[STATE_KEY] = _blank()


# ================================================================ dedupe ===

def already_fired(full_state: dict, once_key) -> bool:
    if not once_key:
        return False
    return str(once_key) in set(state(full_state).get("fired") or ())


def mark_fired(full_state: dict, once_key) -> None:
    if not once_key:
        return
    st = state(full_state)
    fired = set(st.get("fired") or ())
    fired.add(str(once_key))
    st["fired"] = sorted(fired)


def unmark_fired(full_state: dict, once_key) -> None:
    """Let a once_key fire again.

    For an offer that is owed until something in the world CHANGES rather than
    until a turn passes: the producer re-arms its own key while the thing is
    still owed, and emit still refuses a duplicate because the key is matched
    against the live queue and the active row as well as against `fired`.
    """
    if not once_key:
        return
    st = state(full_state)
    fired = set(st.get("fired") or ())
    fired.discard(str(once_key))
    st["fired"] = sorted(fired)


# ================================================================= queue ===
#
# WHY QUEUED EVENTS NEED DISTINCT IDS.
#
# event_id is the only handle the client hands back. It acknowledges an event
# with check_event {event_id, choice_number} and asks for a choice preview with
# get_choice_reward {event_id} -- so find() and drop() can only look an event up
# by that id. When two queued events share one, the server is guessing which one
# the player answered.
#
# That is not hypothetical, and drop()'s own docstring records the bug it caused:
# resolving the event on screen also deleted its still-queued siblings, and "an
# outing queued behind another vanished from its own turn and only resurfaced 17
# turns later, taking its +20 Wit with it". The fix at the time made drop remove
# only ONE match, which narrows the window without closing it -- the remaining
# guess is "the active one, else the first queued match", and a client resolving
# out of order (it re-sends an id after a reload -- see find) can still be paid
# the wrong event's rewards.
#
# Duplicates are the norm, not the exception. Whole families travel under one
# generic id: every outing is 6000, every support-card event 10002. A driven URA
# career serves 6000 for 18 distinct stories where the real server uses it for 4,
# because the per-trainee ids the real server would use are not derivable from
# master.mdb (see the write-up above event_engine.wire_story_id).
#
# THE CLIENT DOES NOT CARE WHAT THE NUMBER IS. Three independent checks say
# event_id is an opaque handle to it, not a key:
#   * the client builds its own event entries from storyId alone --
#     TutorialSingleMode.AddEventInfo(list, storyId, timing, supportCardId,
#     showClear) takes no event_id and renders through the same pipeline;
#   * none of the real ids appear anywhere in master.mdb, the client's own
#     database (all 416 tables scanned), so it has nothing to resolve them
#     against;
#   * no career event id is hardcoded in the client binary.
# Presentation comes from story_id, show_clear, show_clear_sort_id and
# play_timing, all of which are served correctly either way.
#
# So a colliding event is given a private id that is unique within the career.
# FIRST COME, FIRST SERVED: the first event to claim an id keeps the real one,
# and only later duplicates are renumbered -- so an event that is already unique
# (the crane intro, the finals pair, anything a caller special-cases by id) is
# never touched. Nothing outside this module needs to know: resolve/find/drop
# accept either id, and the real id stays on the row for logging and for
# once_key identity.
#
# WHEN REAL IDS TURN UP: nothing here has to be undone. A producer that learns
# the true per-trainee id simply emits it, the collision stops happening, and
# the synthetic id stops being assigned.
#
# NOT CLOSED BY THIS -- the CROSS-QUEUE case. Uniqueness is guaranteed within
# this queue only. single_mode_events.EXTRA_EVENTS_KEY is a second, legacy queue
# of pre-built wire entries, drained by _drain_pending after this one, and
# handle_ura_check_event calls resolve() FIRST on every acknowledgement. So a
# legacy entry served under 6000 while a held or timing-blocked 6000 row is
# still sitting in this queue would have its acknowledgement captured here: the
# queued row is resolved and dropped, and the legacy event never pays.
#
# Reachable (serve_next returning nothing because the head is held is exactly
# when the legacy queue gets its turn), but not fixed here, because renumbering
# a legacy entry is not safe from this side -- those entries are consumed by
# branches further down handle_ura_check_event that key on the id itself, and
# nothing in the corpus pins down which. The fix belongs with whichever family
# still uses EXTRA_EVENTS_KEY, as it migrates onto this queue.
_SYNTHETIC_ID_BASE = 990000


def wire_id(row) -> int:
    """The id THIS event goes out under -- its disambiguating id if it has one,
    else its real one. The only id the client will ever quote back."""
    if not isinstance(row, dict):
        return 0
    return int(row.get("wire_event_id") or row.get("event_id") or 0)


def _disambiguate(st: dict, row: dict) -> None:
    """Give `row` a private id if its real one is already spoken for."""
    taken = {wire_id(e) for e in (st.get("queue") or ())}
    if isinstance(st.get("active"), dict):
        taken.add(wire_id(st["active"]))
    if int(row.get("event_id") or 0) not in taken:
        return
    row["wire_event_id"] = _SYNTHETIC_ID_BASE + int(st.get("seq") or 0)
    log.info("event %s (story %s) collides with a queued event; serving it as "
             "%s so the acknowledgement is unambiguous",
             row.get("event_id"), row.get("story_id"), row["wire_event_id"])


def emit(full_state: dict, event: Event) -> bool:
    """Enqueue an event unless its once_key already fired or it is already
    queued. Returns whether it was accepted.

    Marking happens HERE, at emit, only for the dedupe identity -- the REWARD
    still applies at resolution. Those two were conflated before, which is how
    Grand Live's lesson unlock ended up granting its tokens the instant the turn
    ticked whether or not the cutscene ever played."""
    if already_fired(full_state, event.once_key):
        return False
    st = state(full_state)

    # "Is this already queued?" is answered by once_key when the event HAS one,
    # and only falls back to event_id when it doesn't.
    #
    # Several families share one generic event_id -- all four URA fixed beats are
    # event_engine.CHARA_EVENT_ID, differing only by story_id. Comparing on
    # event_id alone therefore rejected every beat after the first, which is a
    # dedupe bug rather than a real collision: they are distinct events that
    # happen to travel under a shared id.
    # Without a once_key the identity includes story_id, NOT event_id alone.
    # Whole families share one generic event_id and differ only by story: every
    # support-card event is 10002, every outing 6000. Comparing on event_id
    # alone would let the first one queued block every other event of its kind
    # for that turn.
    def _identity(e: dict):
        return e.get("once_key") or ("id", e.get("event_id"), e.get("story_id"))

    mine = _identity(event.to_dict())
    known = {_identity(e) for e in st["queue"]}
    active_row = st.get("active")
    if active_row:
        known.add(_identity(active_row))
    if mine in known:
        return False
    st["seq"] = int(st.get("seq") or 0) + 1
    row = event.to_dict()
    row["_seq"] = st["seq"]
    _disambiguate(st, row)
    st["queue"].append(row)
    st["queue"].sort(key=lambda e: (e.get("priority", PRIO_NORMAL),
                                    e.get("_seq", 0)))
    mark_fired(full_state, event.once_key)
    log.debug("event queued: %s story=%s prio=%s src=%s",
              event.event_id, event.story_id, event.priority, event.source)
    return True


def pending(full_state: dict) -> list:
    return [Event.from_dict(e) for e in state(full_state).get("queue") or ()]


def peek(full_state: dict) -> Event | None:
    q = state(full_state).get("queue") or ()
    return Event.from_dict(q[0]) if q else None


# play_timing NAMES THE KIND OF RESPONSE THAT MAY CARRY THE EVENT, and the
# client silently ignores one that arrives on the wrong kind. Measured over
# every captured response that served an event:
#
#     timing 2  -> race_entry
#     timing 3  -> race_out (56) and the check_event chain behind it (43)
#                  ...and exec_command NOT ONCE, in 99 servings
#     timing 9  -> team_race_out and its check_event chain
#     timing 6/10/11 -> exec_command and check_event
#
# Timing 3 is "after the turn's race", which is why the Unity Cup gate for
# rounds 3-5 (turns 48/60/72 -- all race turns) carries it while rounds 1-2
# (training turns) carry timing 10. Serving a timing-3 event on an
# exec_command response therefore does nothing at all: the client drops it,
# never resolves it, and it stays ACTIVE for ever. Every later response
# re-offers that same dead event, the turn-hold behind it never lifts, and the
# career loops on its turn (user-reported 2026-09-06: trained instead of
# racing on turn 72, and the Unity Cup finals never started).
#
# Only the pairs that never occur are listed; anything unmeasured stays
# allowed, so this can only withhold an event from a response kind the real
# server has never used for it.
TIMING_FORBIDDEN_ENDPOINTS = {
    3: ("exec_command",),
}


# The other half of the same measurement: WHICH timing each kind of response
# stamps on the events it carries. Over 1541 real event entries in every full
# wire capture, only 5 of 316 event_ids are ever seen with more than one
# timing -- so the timing belongs to the RESPONSE, not to the event.
#
#   race_entry -> always 2      race_out  -> always 3
#   live_start -> always 12     team_race_out -> always 9
#   start/load -> always 1      exec_command -> 6 (+10/11 Unity Cup)
#
# check_event is deliberately absent: it CONTINUES whatever chain is already
# running (real shows 1, 3, 6, 9, 10, 11, 12 on it), so it inherits the timing
# of the response that opened the chain -- CHAIN_TIMING_KEY below.
ENDPOINT_TIMING = {
    "race_entry": 2,
    "race_out": 3,
    "live_start": 12,
    "team_race_out": 9,
    "exec_command": 6,
    "start": 1,
    "load": 1,
}
CHAIN_TIMING_KEY = "chain_timing"

# The subset of ENDPOINT_TIMING that is UNANIMOUS in the real corpus and is
# therefore stamped onto whatever a response of that kind happens to carry,
# rather than merely being the default for events that opted in:
#
#   race_entry     70/70 entries timing 2
#   race_out       97/97 entries timing 3
#   team_race_out  20/20 entries timing 9
#
# The client keys off the RESPONSE kind, so an event that reaches one of these
# carrying anything else is silently dropped -- and it reaches them from a
# dozen producers that each hardcoded a timing suited to the exec_command path
# they were written for (URA's finals-loss reaction at timing 6, the
# career-over event at timing 1, an outing at 6 that a race turn dragged along).
# Stamping at the point of service is what the real server does and is the only
# fix that covers producers not yet written.
#
# /load is here for the same reason, and its case is the sharpest of the four.
# It is equally unanimous in the corpus (start/load, 18/18 at timing 1) and it
# is the ONE response that re-serves an event it did not itself produce: what
# was on screen when the player quit, stamped with the timing of whatever
# response first served it. The client buckets incoming events by play_timing
# (WorkSingleModeData._storyInfoListDic) and plays a bucket only when it
# reaches that beat -- so a training event remembered at timing 6 (CommandStart)
# came back on the load response into a bucket the resumed turn-start screen
# never plays, and the event was simply gone. That is the user-reported "exiting
# at an event choice skips the event entirely", and it applies to every timing a
# resumed event can carry (3 RaceEnd, 6 CommandStart, 10/11 Unity Cup), which is
# exactly why the real server stamps the response's own timing here too.
#
# An earlier note argued the opposite -- that re-timing a resumed event was "a
# behaviour change with no defect behind it". The defect is the resume itself.
#
# exec_command and check_event genuinely carry several timings each and must
# never be stamped.
ENDPOINT_FORCED_TIMING = {
    "race_entry": 2,
    "race_out": 3,
    "team_race_out": 9,
    "load": 1,
}


def note_endpoint(full_state: dict, endpoint: str) -> int:
    """Record that a response of THIS kind is being built, and return the
    timing it carries.

    The chain timing used to be recorded only as a side effect of serve_next
    actually serving something. That is the wrong trigger: the response kind
    opens the chain whether or not it had a free display slot at that moment,
    and the common case is that it did NOT (the turn's own beats were already
    on screen). Every check_event behind such a response then inherited a chain
    timing that was never set, i.e. 1, and a timing_from_endpoint event served
    from the drain came out as a start-of-turn intro instead of the
    post-training beat it is."""
    return _endpoint_timing(full_state, endpoint)


def stamp_response_timing(data: dict, endpoint: str) -> None:
    """Force every event this response carries to the timing its KIND uses.
    No-op on any endpoint whose timing is not unanimous in the corpus."""
    timing = ENDPOINT_FORCED_TIMING.get((endpoint or "").rsplit("/", 1)[-1])
    if timing is None or not isinstance(data, dict):
        return
    for entry in _response_events(data):
        if entry.get("play_timing") != timing:
            entry["play_timing"] = timing


def _response_events(data: dict):
    """Every event entry on a response, including the two nested copies."""
    for holder in (data, data.get("single_mode_load_common"),
                   data.get("single_mode_start_common")):
        if not isinstance(holder, dict):
            continue
        for entry in holder.get("unchecked_event_array") or ():
            if isinstance(entry, dict):
                yield entry


# event_contents_info.is_effected_multi_chara is a PER-SCENARIO wire field, not
# a per-event one, and the endpoint family separates it perfectly: across 1528
# real event entries it is present on 757 of 757 single_mode_team (Unity Cup)
# entries -- always False -- and absent from 282 of 282 single_mode_free
# (Trackblazer) and 489 of 489 single_mode_live (Grand Live) ones. No entry
# anywhere carries True.
#
# An earlier reading counted the same 757/282 split and concluded the field was
# simply optional and could be left alone; it is not optional, it is decided by
# which scenario is running. We emitted it from exactly one Unity Cup builder,
# so most Unity Cup events went out without it.
#
# URA has no real event capture in this repo at all (docs: zero bare
# single_mode/* wire captures), so it stays with the majority and omits it --
# which is also what it did before, so no URA behaviour changes here.
_MULTI_CHARA_FLAG_PREFIXES = ("single_mode_team",)


def stamp_multi_chara_flag(data: dict, endpoint: str) -> None:
    """Add or remove is_effected_multi_chara to match the scenario family."""
    if not isinstance(data, dict):
        return
    wants = (endpoint or "").split("/")[0] in _MULTI_CHARA_FLAG_PREFIXES
    for entry in _response_events(data):
        info = entry.get("event_contents_info")
        if not isinstance(info, dict):
            continue
        if wants:
            info["is_effected_multi_chara"] = False
        else:
            info.pop("is_effected_multi_chara", None)


def _endpoint_timing(full_state: dict, endpoint: str) -> int:
    """The timing a `timing_from_endpoint` event takes on THIS response.

    A response kind with a known timing also OPENS a chain: the check_events
    the client fires to work through that chain carry the same timing, and are
    served here with no endpoint of their own to go on."""
    st = state(full_state)
    name = (endpoint or "").rsplit("/", 1)[-1]
    timing = ENDPOINT_TIMING.get(name)
    if timing is None:
        return int(st.get(CHAIN_TIMING_KEY) or 1)
    st[CHAIN_TIMING_KEY] = timing
    return timing


# ============================================================ hold gates ===
#
# A producer is asked what fires exactly ONCE per response, and only two kinds
# of response poll at all (race_out and exec_command -- see
# single_mode_team._poll_career_events' call sites). An event whose correct
# PLACE in a chain only arrives later, on some check_event, therefore cannot
# express that as a producer-side condition: by the time the condition is true
# nothing will ever ask again, and the event is simply never produced
# (user-reported 2026-09-07: "A Present from Director Akikawa!" vanished
# outright after its ordering was fixed this way).
#
# A hold gate moves that condition to SERVE time, where it is re-checked on
# every drain. The producer emits the event as it always did; the gate decides
# when it is allowed onto the wire.
_HOLD_GATES = {}


def hold_gate(name: str):
    """Register `name` as a serve-time gate: fn(full_state) -> ready?"""
    def wrap(fn):
        _HOLD_GATES[name] = fn
        return fn
    return wrap


def _held(row: dict, full_state: dict) -> bool:
    """Whether this queued row is still waiting for its gate to open.

    An UNKNOWN gate name never holds. Gate functions run against saved state
    on every drain, so one that raises must not take the queue down with it --
    it is treated as open, which degrades to the old always-serve behaviour
    rather than to a stuck career."""
    name = (row or {}).get("hold_until")
    if not name:
        return False
    fn = _HOLD_GATES.get(name)
    if fn is None:
        return False
    try:
        return not fn(full_state)
    except Exception:
        log.exception("hold gate %r failed; serving the event", name)
        return False


def servable_on(row: dict, endpoint: str) -> bool:
    """Whether an event may claim the display slot on THIS kind of response."""
    if not endpoint or not isinstance(row, dict):
        return True
    if row.get("timing_from_endpoint"):
        return True          # its timing IS this response's -- see Event
    blocked = TIMING_FORBIDDEN_ENDPOINTS.get(int(row.get("play_timing") or 0))
    return not (blocked and endpoint.rsplit("/", 1)[-1] in blocked)


def queued_rows(full_state: dict) -> list:
    """Every event row still waiting for a display slot, ACTIVE ONE INCLUDED.

    Returned as the live dicts, so a caller that adjusts one (see
    Scenario.retime_queued) is editing the queue itself. `active` is in here
    because an event already offered but never acted on is re-offered from
    exactly the same row."""
    st = state(full_state)
    rows = [r for r in (st.get("queue") or ()) if isinstance(r, dict)]
    if isinstance(st.get("active"), dict):
        rows.append(st["active"])
    return rows


def serve_next(full_state: dict, endpoint: str = ""):
    """Pop the front of the queue and make it the ACTIVE event (the one the
    client is showing). Returns (wire_entry, Event) or (None, None).

    A head this response kind cannot carry is LEFT WHERE IT IS rather than
    skipped over: the queue is ordered, and serving the event behind it would
    play the turn's cutscenes out of order. It simply waits for a response that
    can carry it -- for timing 3, the race the turn is about.

    A HELD head is the opposite case and IS skipped over. play_timing says
    "not on this kind of response", so waiting in place costs nothing; a hold
    gate says "not until something later in THIS chain has played", so waiting
    in place is a deadlock -- the events it is waiting for are exactly the ones
    queued behind it (or in the legacy EXTRA queue, which _drain_pending only
    reaches once this returns nothing). Skipping keeps the held event in the
    queue for the next drain, which is when its gate is re-checked.
    """
    st = state(full_state)
    if not st["queue"]:
        st["active"] = None
        return None, None
    index = 0
    while index < len(st["queue"]) and _held(st["queue"][index], full_state):
        index += 1
    if index >= len(st["queue"]):
        return None, None            # everything left is still waiting
    if not servable_on(st["queue"][index], endpoint):
        return None, None
    ev = Event.from_dict(st["queue"].pop(index))
    if ev.timing_from_endpoint:
        ev.play_timing = _endpoint_timing(full_state, endpoint)
    elif endpoint:
        # Even a fixed-timing event opens the chain the check_events behind it
        # will inherit from.
        _endpoint_timing(full_state, endpoint)
    st["active"] = ev.to_dict()
    return ev.wire_entry(), ev


def active(full_state: dict) -> Event | None:
    a = state(full_state).get("active")
    return Event.from_dict(a) if a else None


def find(full_state: dict, event_id) -> Event | None:
    """The event this id refers to, whether it is active or still queued. The
    client can resolve out of order (it re-sends an id after a reload), so both
    have to be searched -- the old code only knew about whichever list the
    producing family happened to use."""
    st = state(full_state)
    a = st.get("active")
    if a and _matches(a, event_id):
        return Event.from_dict(a)
    for e in st.get("queue") or ():
        if _matches(e, event_id):
            return Event.from_dict(e)
    return None


def _matches(row: dict, event_id) -> bool:
    """Whether the client quoting `event_id` means THIS row.

    Matched against the id that ACTUALLY WENT OUT and nothing else. A row with a
    disambiguating id answers only to that id -- accepting its real id as well
    would hand back the very ambiguity the id exists to remove, because the row
    that legitimately owns the real id is still queued alongside it. A row
    without one answers to its real id, which was unique across the queue at the
    moment it was queued."""
    try:
        return int(event_id) == wire_id(row)
    except (TypeError, ValueError):
        return False


def drop(full_state: dict, event_id) -> None:
    """Remove the ONE event that went out under this id.

    Removing every match was a real bug: whole families share a generic
    event_id (all outings are 6000, all support events 10002), so resolving the
    one on screen also deleted its still-queued siblings. In the baseline an
    outing queued behind another vanished from its own turn and only resurfaced
    17 turns later, taking its +20 Wit with it.

    Removing only one narrowed that window without closing it -- which of the
    siblings was removed was still a guess. It is no longer a guess: ids are
    made unique across the queue at emit time (see _disambiguate), so at most
    one row can answer to any id and "the active one, else the first queued
    match" now picks the only candidate there is."""
    st = state(full_state)
    a = st.get("active")
    if a and _matches(a, event_id):
        st["active"] = None
        return
    queue = list(st.get("queue") or ())
    for i, e in enumerate(queue):
        if _matches(e, event_id):
            del queue[i]
            break
    st["queue"] = queue


# ============================================================== resolvers ===
# Special flows that need more than "apply these effects": they get a named
# function here and set resolver=<name>. A labelled extension point instead of
# another branch in check_event.
RESOLVERS: dict = {}


def resolver(name: str):
    def deco(fn):
        RESOLVERS[name] = fn
        return fn
    return deco


@dataclasses.dataclass
class Resolution:
    """What resolving an event did -- for logging and for the caller to report."""
    event: Event
    choice_number: int
    applied: dict = dataclasses.field(default_factory=dict)
    extra: dict = dataclasses.field(default_factory=dict)
    handled: bool = True


def resolve(full_state: dict, chara_info: dict, event_id, choice_number=0,
            **kwargs) -> Resolution | None:
    """THE resolution path. Applies the chosen effects through the single
    applier, runs the event's named resolver if it has one, then drops it.

    Returns None when the id is unknown to the pipeline, so a caller can fall
    back for anything not migrated yet."""
    ev = find(full_state, event_id)
    if ev is None:
        return None

    res = Resolution(event=ev, choice_number=choice_number or 0)

    # ONE reward applier. Every family's payout goes through event_engine's
    # effect vocabulary, so caps, the 1200 halving, mood clamps and hint grants
    # behave identically no matter which producer made the event.
    if ev.choices:
        index = max(1, int(choice_number or 1))
        index = min(index, len(ev.choices))
        try:
            res.applied = event_engine.apply_choice(
                chara_info, ev.engine_event(), index, full_state=full_state) or {}
        except Exception:                              # noqa: BLE001
            log.exception("event %s: reward application failed", event_id)

    fn = RESOLVERS.get(ev.resolver) if ev.resolver else None
    if fn is not None:
        try:
            res.extra = fn(full_state, chara_info, ev, choice_number, **kwargs) or {}
        except Exception:                              # noqa: BLE001
            log.exception("event %s: resolver %r failed", event_id, ev.resolver)

    drop(full_state, event_id)
    log.info("event %s resolved (choice %s, src=%s) applied=%s%s",
             event_id, choice_number, ev.source, res.applied,
             f" extra={res.extra}" if res.extra else "")
    return res


# ============================================================== producers ===
# A producer looks at the career and returns the events that should fire now.
# Registered in one ordered list, so "what can fire this turn" is answerable by
# reading a single registry instead of tracing 21 call sites through
# exec_command.
PRODUCERS: list = []


@dataclasses.dataclass
class Ctx:
    """What a producer gets to look at.

    TWO turn numbers, and picking the wrong one shifts a whole schedule by one:

      turn          the turn the CLIENT is on -- the request's current_turn.
                    This is the real "what turn is it", and the one a schedule
                    derived from a capture should use: the capture shows
                    exec_command(current_turn=4) producing the turn-4 beat.

      advanced_turn chara_info.turn AFTER the action, i.e. turn + 1. Some older
                    tables were authored against this value; they read one turn
                    early if switched to `turn` without re-deriving them.

    Live-reported symptom of getting this wrong: "the event for unlocking tokens
    played one turn early", and -- same cause -- the Lives never fired, because
    the concert beat landed on turn 23 and live_start then saw a turn that isn't
    in the Live schedule and silently did nothing."""
    viewer_id: object
    full_state: dict
    career: dict
    chara_info: dict
    turn: int
    advanced_turn: int | None = None
    payload: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.advanced_turn is None:
            self.advanced_turn = (self.turn or 0) + 1

    @property
    def career_data(self) -> dict:
        return self.career.get("data", {}) if isinstance(self.career, dict) else {}

    @property
    def race_history(self) -> list:
        return self.career_data.get("race_history") or []

    @property
    def player_chara(self) -> int:
        return (self.chara_info.get("card_id") or 0) // 100

    @property
    def scenario_id(self) -> int:
        return int(self.chara_info.get("scenario_id") or 1)


def producer(name: str, priority: int = PRIO_NORMAL, scenario: int | None = None):
    """Register a producer. `priority` is the DEFAULT for events it emits that
    don't set their own.

    `scenario` restricts the producer to one scenario_id -- poll() skips it
    everywhere else. That test lives HERE, in dispatch, so a scenario's own
    producer module never has to open with `if not <scenario>.is_active(...)`:
    the filter cannot be forgotten, and a producer that omits it is by
    construction scenario-agnostic rather than accidentally global."""
    def deco(fn):
        PRODUCERS.append((name, priority, fn, scenario))
        return fn
    return deco


def poll(ctx: Ctx) -> list:
    """Run every producer and enqueue what they return. Returns the events
    actually accepted (post-dedupe), in serve order."""
    accepted = []
    for name, prio, fn, scenario in PRODUCERS:
        if scenario is not None and ctx.scenario_id != scenario:
            continue
        try:
            produced = fn(ctx) or ()
        except Exception:                              # noqa: BLE001
            log.exception("producer %r failed; skipping it this turn", name)
            continue
        for ev in produced:
            if ev is None:
                continue
            if not ev.source:
                ev.source = name
            if ev.priority == PRIO_NORMAL and prio != PRIO_NORMAL:
                ev.priority = prio
            if emit(ctx.full_state, ev):
                accepted.append(ev)
    accepted.sort(key=lambda e: e.priority)
    return accepted
