"""The Scenario interface -- one object per career scenario.

WHY THIS EXISTS
---------------
Grand Live was added as a module that shared career code imported BY NAME:
`grand_live.is_active(chara_info)` guarded 22 branches across single_mode_team.py,
career_producers.py, event_engine.py and trained_chara.py, and 90-odd call sites
reached into it directly. That works for two scenarios. At ten it is a branch per
scenario per hook point, spread through an 11k-line file, where forgetting one is
silent -- exactly the failure shape career_events.py was built to remove from
events.

THE MODEL
---------
A Scenario is DATA + HOOKS, looked up by scenario_id. It is not a branch.

    scen = scenarios.for_chara(chara_info)      # never None
    return scen.friendship_bonus_pct(full_state)

Every hook here has a NEUTRAL default that reproduces URA's behaviour exactly, so

  * shared code calls hooks unconditionally -- no `is_active` test survives;
  * a scenario that does not care about a hook does not mention it;
  * adding a scenario is a package under scenarios/ plus one line in
    registry._SCENARIO_MODULES. Nothing in single_mode_team.py changes.

Hooks are grouped below by the question they answer. When a new scenario needs a
decision shared code currently hardcodes, ADD A HOOK HERE with the current
behaviour as its default -- that keeps every existing scenario byte-identical
while making the new one expressible.
"""

from __future__ import annotations

import functools
import logging

from .. import master_data

log = logging.getLogger("uma-server")



@functools.lru_cache(maxsize=1)
def _restrict_rows() -> dict:
    """single_mode_restrict_support -> {scenario_id: frozenset(support_card_id)}.

    THE DECK BAN IS MASTER DATA, not a rule anyone has to maintain here. The
    table is tiny and says exactly what the in-game deck rules say: card 30081
    ("Passing the Dream On" Special Week, the Team Sirius group card) is barred
    from scenario 2 (Unity Cup) and scenario 3 (Grand Live), and nothing else
    is barred anywhere. Reading it means a future ban needs no code change.
    """
    from .. import master_data
    out: dict = {}
    try:
        rows = master_data.query(
            "SELECT scenario_id, support_card_id FROM single_mode_restrict_support")
    except Exception:
        return {}
    for row in rows:
        out.setdefault(int(row["scenario_id"]), set()).add(int(row["support_card_id"]))
    return {k: frozenset(v) for k, v in out.items()}


def restricted_support_cards(scenario_id) -> frozenset:
    """Support cards this scenario refuses to start with."""
    return _restrict_rows().get(int(scenario_id or 0), frozenset())


class Scenario:
    """Base = URA Finale's behaviour. Subclasses override only what differs."""

    # ------------------------------------------------------------ identity --
    scenario_id: int = 0
    name: str = "base"

    # The endpoint family the client posts this scenario's career to, without
    # the trailing slash. URA uses single_mode/*, Grand Live single_mode_live/*.
    # main.py aliases every shared career endpoint onto this prefix.
    endpoint_prefix: str = "single_mode"

    # Keys this scenario owns in full_state, cleared at the start of EVERY
    # career (not just its own) so a finished run cannot leak into the next.
    state_keys: tuple = ()

    # The data_set field this scenario's responses carry in place of URA's.
    # Informational -- attach() is what actually performs the swap.
    data_set_key: str = "ura_data_set"

    # The career-start event chain, as event_ids. Empty means "the shared
    # default" (single_mode_events.INTRO_CHAIN: the trainee's "Introducing
    # <trainee>!" beat, then Tazuna's tips). Only Trackblazer differs -- it
    # opens on the trainee's "Self-Introduction" beat instead. Declared here
    # rather than imported so this module keeps knowing nothing about handlers.
    intro_chain: tuple = ()

    def restricted_support_cards(self) -> frozenset:
        """Support cards that may not be brought into this scenario.

        Straight off single_mode_restrict_support -- see the module function.
        Enforced at career start (single_mode/start), which is the only moment
        a deck is chosen."""
        return restricted_support_cards(self.scenario_id)

    def extra_endpoints(self) -> dict:
        """Endpoints this scenario has that the shared family does not, as
        {suffix: handler}. The handler is a plain (payload) -> response
        callable, the same contract as main.HANDLERS."""
        return {}

    # ------------------------------------------------------------- response --
    def attach(self, response: dict, full_state: dict, chara_info: dict,
               command_info_array=None, endpoint: str = "") -> dict:
        """Rewrite a served response into this scenario's envelope.

        Called from the ONE chokepoint every career response passes through
        (single_mode_team._sync_chara_info), so no handler knows scenarios
        exist. URA's envelope is what the handlers already build, so the
        default is a no-op."""
        return response

    def career_route(self, chara_info: dict):
        """(route_id, [route_race_id, ...]) -- this career's goal races.

        Default None means "the shared per-uma historical route", which is what
        URA, Unity Cup and Grand Live all read: single_mode_route under
        scenario_id 0, keyed by chara_id. Trackblazer is the one scenario whose
        routes are chara_id 0 and selected by APTITUDE instead, which is why the
        hook takes the whole chara_info rather than an id."""
        return None

    def placement_salt(self, full_state: dict) -> int:
        """Extra seed material for this turn's support-card placement roll.

        The roll is seeded by (card_id, turn) so a turn's layout is stable when
        the preview is rebuilt -- which is right, and is exactly why anything
        that RE-ROLLS it (Trackblazer's Reset Whistle) needs a number it can
        move. 0 is "never re-rolled", which is every scenario but that one."""
        return 0

    def command_info(self, full_state: dict, chara_info: dict, home_commands):
        """This scenario's per-facility preview overlay, as (commands, rolled).

        `rolled` says a fresh random roll was made and therefore needs
        persisting. (None, False) means "this scenario adds no overlay", which
        is URA."""
        return None, False

    def held_turn(self, full_state: dict) -> int:
        """A turn number to SERVE in place of chara_info.turn, or 0 for none.

        Scenarios that hold the client on a turn across a cutscene chain (Grand
        Live's concerts) report it here. Served only -- the persisted turn is
        never touched."""
        return 0

    # ------------------------------------------------------------- training --
    def friendship_bonus_pct(self, full_state: dict) -> int:
        """Extra friendship-bonus percentage points from scenario mechanics."""
        return 0

    def specialty_bonus(self, full_state: dict) -> int:
        """Weight points added to a support card's own-type facility in the
        placement roll, on top of its specialty_priority."""
        return 0

    def training_bonus(self, full_state: dict):
        """{stat: +N} flat training bonus for the preview, or None."""
        return None

    def support_event_bonus_pct(self, full_state: dict) -> int:
        """Percentage points added to the support CHAIN event share."""
        return 0

    def award_training_gains(self, full_state: dict, chara_info: dict,
                             payload: dict) -> None:
        """Bank this turn's scenario currency after a real training action.

        Pays against the preview the client was actually SHOWN (scenarios cache
        their own roll -- see command_info), never a fresh one."""
        return None

    def apply_currency_gain(self, full_state: dict, chara_info: dict,
                            amount: int):
        """An event choice granting this scenario's own currency, as
        (wire_target_id, amount) for the outcome screen, or None if the
        scenario has no such currency. event_engine's `mt` /
        performance_tokens effect."""
        return None

    # ----------------------------------------------------------------- flow --
    # playing_state values an ordinary check_event must NOT stomp back to 1.
    # Doing so exits whatever screen the client is on with its flow still
    # pending, which softlocks the career on that turn.
    #
    # 5 ("event chain still running") is in the BASE set, not a scenario's: the
    # crane-game minigame sets it on any career, URA included, and resetting it
    # there would drop the outcome event the client is waiting on. A scenario
    # ADDS to this set (Grand Live adds 10, backstage; Unity Cup adds 7/8/9,
    # the team-race screen), it does not replace it.
    playing_states: frozenset = frozenset({5})

    def settle(self, full_state: dict, chara_info: dict,
               endpoint: str = "") -> bool:
        """Apply a pay-out this scenario deferred to the request AFTER the
        set-piece that earned it. True if persistent state changed.

        Some awards cannot land on the response that produces them: Unity Cup's
        team race hands the client the OLD ladder rank plus the new one as
        tmp_team_rank so the rank-up screen has something to animate, and only
        the request after that carries the committed rank, the teammates' new
        stats and the reward panel. Called from the same response chokepoint as
        attach(), before it, so the envelope attach builds is the settled one.
        """
        return False

    def interstitial_pending(self, full_state: dict, chara_info: dict,
                             turn) -> bool:
        """Whether the turn just played owes a scenario set-piece that has not
        happened yet -- the display slot is held open for its event chain."""
        return False

    def begin_hold(self, full_state: dict, turn: int) -> None:
        """Start holding the SERVED turn at `turn`, for the set-piece chain
        interstitial_pending just reported. Paired with held_turn(), which is
        what actually reports it, and released by the scenario's own chain."""
        return None

    def training_failure_override(self, full_state: dict, chara_info: dict,
                                  command_id, partner_ids) -> int:
        """A failure rate this scenario forces for this facility, or None to
        leave the formula's own number alone.

        Unity Cup zeroes it whenever a teammate standing there is about to
        SpExplode -- the purple burst. Capture-confirmed 157/157 across two
        runs (39 + 118 facility-showings, min = max = 0), and specific to
        purple: the blue burst leaves the rate untouched (max 54 and 97 in the
        same two runs)."""
        return None

    def scale_training_preview(self, full_state: dict, chara_info: dict,
                               command_id, params: list) -> list:
        """Last-pass adjustment of ONE facility's params_inc_dec_info_array.

        `params` is the finished preview the client will render -- target_type
        1-5 stats, 10 energy (a NEGATIVE cost for a training), 30 skill points.
        Returning it unchanged is URA, which has nothing that scales a facility.

        This is deliberately the LAST word rather than an input to the formula:
        exec_command applies exactly what the preview shows (see
        _preview_gains_for), so a scenario that adjusts the number here gets the
        award for free and the button and the pay-out cannot disagree. It is
        also the only place both halves of Trackblazer's Ankle Weights can be
        expressed together -- +50% gain and +20% energy cost are one item.
        """
        return params

    def facility_levelup_event(self, command_id) -> tuple:
        """(event_id, story_id) for the 'Training Level Up!' banner after this
        facility levelled. URA uses ONE id for all five (1024/400000005, from a
        real capture); Grand Live has one per facility. Story 400000005 is the
        same everywhere -- only the event_id, which is what the client keys its
        presentation off, differs."""
        return (1024, 400000005)

    def appraisal_events(self):
        """target_id -> [(min_bond, event_id, amount)] for the NPC Appraisal
        series, or None for URA's (single_mode_events.APPRAISAL_EVENTS).

        The MECHANIC is shared -- train the facility the NPC is standing in and
        her appraisal sometimes fires, paying by bond tier -- but the event ids
        are per scenario, and the client keys the presentation off them."""
        return None

    def levels_facilities_by_training(self) -> bool:
        """Whether four successful trainings in a facility level it up.

        URA's rule, and the default. Unity Cup replaces it wholesale: there the
        facility level is a function of the team's rank in that stat (see
        unity_cup.impl.facility_levels), so the counter must not also fire --
        it would clobber the derived level and play a "Training Level Up!"
        cutscene the scenario has no such thing as."""
        return True

    def training_award_bonus(self, full_state: dict, chara_info: dict,
                             command_id) -> list:
        """Extra params_inc_dec_info_array entries to APPLY for this training,
        on top of the ones home_info displays.

        Unity Cup's teammate bonus is a SECOND array on the wire
        (team_data_set.command_info_array), and the client renders it as its
        own line and adds it to the total: the training button reads "+12 +12"
        and the player expects 24. home_info's array -- the only one the server
        was applying -- carries the first 12 alone, so half of every trained
        stat silently went missing (user-reported 2026-09-05).

        Display and award are deliberately separate: these entries must NOT be
        folded into the home_info preview, or the client would show the bonus
        twice."""
        return []

    def partner_placements(self, full_state: dict, chara_info: dict, turn: int,
                           occupied: dict = None) -> dict:
        """{facility command_id: [training_partner_id, ...]} -- scenario
        teammates standing in a facility this turn.

        They ride the SAME home_info training_partner_array as support cards
        and scenario NPCs, which is the only array the client draws facility
        portraits from. A scenario that shadows those facilities with an
        overlay of its own (Unity Cup's team_data_set.command_info_array) MUST
        report its bodies here too, or the overlay promises a bonus for a
        facility the player can see is empty -- exactly the desync
        user-reported on 2026-09-05.

        `occupied` is {command_id: bodies already standing there}, so the
        five-slot cap counts everyone.

        Distinct from npc_supporters(), which is for recruits who own no card
        and are placed by the shared roll: these are placed by the SCENARIO,
        because its own preview has to agree with them body for body."""
        return {}

    def npc_supporters(self, full_state: dict, chara_info: dict) -> list:
        """chara_ids this scenario places in training who own no equipped
        support card this run, in join order."""
        return []

    def gated_npc_charas(self) -> frozenset:
        """chara_ids this scenario keeps OFF the training screen until its own
        story beat introduces them -- even when the player brought their real
        support card.

        Per scenario, not global: the same character is an ordinary card
        everywhere else, and a gate derived from every scenario's unlock table
        at once hid her for the scenarios that never fire the beat that would
        release her."""
        return frozenset()

    def persist_upkeep(self, full_state: dict, chara_info: dict) -> None:
        """Scenario state that has to be written into the PERSISTED chara_info,
        not just into the served copy.

        Scenario.attach runs in the response dispatcher, long after the handler
        saved the career: anything it changes is serve-only. A flag the rest of
        the career READS BACK -- is_appear, which the facility placement roll
        consults on the next exec_command -- has to be reasserted here instead,
        on the career's own chara_info, or the scenario and the persisted state
        drift apart and only the wire ever agrees with the scenario."""
        return None

    def on_event_resolved(self, full_state: dict, chara_info: dict,
                          event_id) -> dict:
        """Scenario payouts that must land when an event RESOLVES rather than
        when it was queued -- the client renders a reward card by diffing served
        state, so WHICH card a reward appears on is decided by when we mutate.

        Called from check_event for EVERY event id, so it must be idempotent."""
        return {}

    def holds_chain_after(self, event_id) -> bool:
        """Whether resolving this event must leave the served event chain EMPTY.

        That gap is a signal in its own right: it is what makes the client open
        Grand Live's backstage screen. Serving anything else in the same
        response softlocks the career on that turn."""
        return False

    def retime_queued(self, full_state: dict, endpoint: str = "") -> None:
        """Last chance to re-stamp a QUEUED event's play_timing for the kind of
        response about to carry it.

        Called immediately before the career-events queue is offered a display
        slot. Default: do nothing. A scenario overrides it when one of its
        beats can legitimately arrive on more than one kind of response and the
        timing has to follow the response rather than the beat -- see
        UnityCup.retime_queued, where a race gate that reaches a training turn
        would otherwise be withheld forever."""
        return

    def chain_entry_after(self, full_state: dict, event_id):
        """The ONE event this scenario owes now that `event_id` resolved, as an
        unchecked_event_array entry, or None.

        For a cutscene sequence the scenario serves itself rather than through
        the event queues (Unity Cup's post-race result beats): the response
        that continues such a chain must carry that entry and nothing else, or
        the queued events of the turn take the slot and the rest of the chain
        is lost."""
        return None

    def race_bonus_pct(self, full_state: dict, chara_info: dict) -> int:
        """Percentage points added to the deck's own Race Bonus. Trackblazer's
        Cleat Hammers are a +20%/+35% race bonus for one turn."""
        return 0

    def fan_bonus_pct(self, full_state: dict, chara_info: dict) -> int:
        """Percentage points added to the deck's own Fan Bonus. Trackblazer's
        Glow Sticks are +50% fans for one turn."""
        return 0

    def rival_race_info(self, full_state: dict, chara_info: dict, turn,
                        program_id) -> list:
        """[{program_id, chara_id}] -- races this turn that carry a scenario
        rival. Trackblazer's Rival Races; nothing else has them."""
        return []

    def on_race_result(self, full_state: dict, chara_info: dict,
                       race_ctx: dict, result_rank: int) -> None:
        """A race the trainee just ran has been committed.

        Called from race_end's one commit block, next to the fan credit and the
        race_history append, and exactly once per race (the block is guarded by
        race_ctx['end_committed'], so a client retry does not double-pay).

        URA has nothing to bank here. Trackblazer has two currencies that come
        off nothing but grade and placement -- Grade Points and shop Coins --
        plus the Twinkle Star Climax standings and the post-race chance of a
        limited shop offer, all of which need the finish and none of which any
        existing hook sees."""
        return None

    def reset(self, full_state: dict) -> None:
        """Clear this scenario's state. Called for every career start."""
        for key in self.state_keys:
            full_state.pop(key, None)

    def snapshot(self, full_state: dict) -> dict:
        """Per-run facts worth freezing into the finished veteran record (read
        back by missions/epithets long after the career state is gone)."""
        return {}

    # ------------------------------------------------- shared-content gates --
    # Each answers "does the base game's X exist in this scenario?". Defaults
    # are URA's, i.e. yes.

    # Happy Meek -- her marker, her duels and her URA Finals round-3 slot.
    has_versus_npc: bool = True

    # THIS SCENARIO'S OWN final challengers in the URA Finals round-3 race, as
    # single_mode_npc row ids. The same slot Happy Meek occupies for URA (see
    # has_versus_npc) -- each one replaces a mob, so the field size, and with
    # it the frame count the client renders, does not change.
    #
    # Empty for URA itself: Meek is not expressed here because she alone scales
    # with duels won, which none of these do.
    finals_rival_npc_ids: tuple = ()

    # The Director's bond-scaled sendoff and his "Super Successful Event!".
    has_director_ending: bool = True

    # single_mode_events.SCENARIO_SCHEDULE, the fixed turn-2/3/4 opening chain.
    uses_fixed_schedule: bool = True

    # The URA Finale itself (turns 74/76/78). Grand Live has it too.
    has_ura_finals: bool = True

    # URA's CAREER-ENDING CHAIN behind the turn-78 finals race_out: 'Twinkle
    # Monthly Special Issue', the Director's 'A Super Successful Event!', the
    # bond-scaled sendoffs and the trainee's own 5000xxx111 ending story, plus
    # their end-of-run payouts. Distinct from has_director_ending, which drops
    # only the Director's share of it: a scenario can keep the chain and lose
    # him (Grand Live), or have an ending of its own and want none of it --
    # Trackblazer's capture runs 203106 -> 203202 -> finish and nothing else.
    has_ending_chain: bool = True

    # TURNS ON WHICH RACING CANNOT TIRE THE TRAINEE OUT -- no 'Race Fatigue'
    # / 'After Repeated Races...' no matter how long the streak is, and the
    # streak keeps counting through them for the races either side.
    #
    # Empty for URA and everything else: the exemption is Trackblazer's alone
    # (user-confirmed), and the corpus shows it exactly -- across 1,741 real
    # Trackblazer careers the event fires on 55-100% of streak-3+ races on
    # every turn it can, and on 0 of 450 / 0 of 93 / 0 of 314 on turns 24, 48
    # and 72. Those are the three round-final turns, the races the scenario
    # forces on the player, which is presumably why they are free.
    race_fatigue_exempt_turns: frozenset = frozenset()

    # NPC ROWS THIS SCENARIO CARRIES FROM TURN 1 AT is_appear 0, as
    # (target_id, chara_id) -- the placeholders whose 0 -> 1 flip IS the
    # client's "X will now appear in training" announcement.
    #
    # The client renders that line by DIFFING the served
    # chara_info.evaluation_info_array between responses. A row that does not
    # exist until the unlock response, and arrives on it already at
    # is_appear 1, is not a diff it can see: the NPC unlocks silently. That is
    # exactly what happened to Grand Live's Director Akikawa (event 202002,
    # alongside Light Hello) and to its Reporter (101007, "A Quirky
    # Correspondent?") -- both user-reported as unlocking with no message.
    #
    # Real Grand Live carries 101/102/103/104/106 from the very first response
    # and never 105 (Light Hello's reveal rides her deck position instead) --
    # capture 20260907_204921, every response from 0004 on, with 102 flipping
    # at 0046 (202002) and 103 at 0084 (101007).
    #
    # Empty for URA, whose rows already ride its captured /start template, and
    # for every scenario with no corpus to copy: inventing rows the real wire
    # does not carry is its own bug (see single_mode_events._REAL_NPC_TARGETS).
    locked_npc_rows: tuple = ()

    # THE DEBUT RACE IS A GOAL RACE: its post-race beat is the trainee's own
    # 'After the ...' story -- ONE acknowledge, the fixed all-five + SP payout,
    # and ZERO energy -- not the ordinary placement event with its two
    # differently-priced options.
    #
    # Trackblazer is the exception, and the exception is where the old rule
    # came from: single_mode_team._is_debut_race was derived from three real
    # DEBUT captures that are all scenario 4 (captures/bot/20260905_152744_icarus
    # and _144604_icarus, both single_mode_free/*, both program 851 turn 12),
    # and then applied to every scenario. It has no career goals at all --
    # see has_secret_events -- so its debut really is a voluntary race that
    # costs -25 energy and offers the choice.
    #
    # Everywhere else it does not: the Grand Live capture runs the debut at
    # turn 12 (20260907_204921/0075-0081, program 1070) with vital 53 before
    # race_entry and 53 still on turn 13, and serves 11059 -> 11125 with ONE
    # choice each. User-reported: outside Trackblazer the debut must not
    # prompt for, or take, any energy.
    debut_is_goal_race: bool = True

    # The per-trainee SECRET events (#21) -- the ones that unlock from what the
    # career has done rather than from a schedule. MANT, on Trackblazer: "An
    # Uma's Career Goals and Secret Events are disabled in Trackblazer", which
    # is the same statement master.mdb makes by giving scenario 4 shared
    # chara_id=0 routes instead of a route per trainee.
    has_secret_events: bool = True

    # THE FINALE'S CUTSCENES ARE PER-SCENARIO, even though the three races are
    # not. Every scenario runs the same qualifier/semifinal/final programs, and
    # each wraps them in ITS OWN pre-race and post-race story: URA's are
    # 11000/11003 ... 11002/11005, Unity Cup's are 101001-101006 and
    # Trackblazer's are 203101-203106 (all three families are in the capture
    # corpus, and no two overlap). None means "URA's", which is what
    # single_mode_team._FINALS_EVENTS holds.
    finals_events: dict | None = None

    # ---------------------------------------------------------- master data --
    def cap_bonus(self) -> dict:
        """Per-scenario stat-cap boost over the card's base, from master.mdb's
        single_mode_scenario row. Generic: every scenario has one, URA's is a
        flat 200 across the board."""
        return cap_bonus_for_scenario(self.scenario_id)

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return f"<Scenario {self.scenario_id} {self.name}>"


def cap_bonus_for_scenario(scenario_id: int) -> dict:
    """single_mode_scenario's stat-cap boost for one scenario id. A free
    function as well as a hook, because career start reads it for the scenario
    named on the START REQUEST, before any Scenario object is in hand."""
    row = master_data.query_one(
        "SELECT max_speed, max_stamina, max_pow, max_guts, max_wiz "
        "FROM single_mode_scenario WHERE id=?", (scenario_id,))
    if not row:
        return {}
    return {"speed": row["max_speed"], "stamina": row["max_stamina"],
            "power": row["max_pow"], "guts": row["max_guts"], "wiz": row["max_wiz"]}
