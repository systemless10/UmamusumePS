"""UNITY CUP (scenario 2) -- "Shine On, Team Spirit!". The Aoharu Cup.

This class is the whole seam between Unity Cup and the rest of the server.
Shared career code never imports anything below this package: it asks the
registry for the scenario a career is being played in and calls hooks.

WHAT LIVES WHERE
    impl.py       the mechanics -- the team, the soul gauge, the three ranks
    preview.py    the per-facility team overlay and banking it
    endpoints.py  opponent_list / team_edit / team_race_analyze /
                  team_race_start-end-out-end_out-continue /
                  save_team_edit_flag -- the endpoints URA has no
                  counterpart for
    producers.py  the scenario's own 57-beat story chain

Read impl.py's docstring first: it marks which numbers are ground truth and
which are KNOBs. docs/UNITY_CUP.md carries the full derivation.

WHY SO FEW OVERRIDES: Unity Cup does not restructure the career. Same 78 turns
(single_mode_turn turn_set 1 vs 2 differ on zero gameplay columns), same
per-character goal-race route (single_mode_route scenario_id 0, shared with
URA), and it keeps the URA Finale at turns 74/76/78
(single_mode_scenario_group 701 = scenarios 1, 2, 3). The team layer runs
ALONGSIDE an otherwise ordinary career rather than replacing it.

What it DOES replace is the CAST: the Director (102) and Happy Meek (2001) are
not in this scenario, so the three shared-content gates below are off and
producers.py supplies Unity Cup's own chain in place of URA's.
"""

from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, impl, preview, producers   # noqa: F401 -- registers producers


class UnityCup(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Unity Cup"

    # Unity Cup posts its career to single_mode_team/*. Every shared endpoint
    # takes the SAME request and returns the same response URA's does -- the
    # only wire difference is the data_set swap attach() performs -- so main.py
    # aliases the whole family onto the same handler objects.
    endpoint_prefix = "single_mode_team"
    data_set_key = "team_data_set"

    # The team roster, gauges, ladder and race history, plus the cached
    # per-turn facility placement roll.
    state_keys = (impl.STATE_KEY, preview.PREVIEW_KEY)

    # THE TEAM-RACE SCREEN. 7 (select), 8 (racing) and 9 (result) must survive
    # a check_event arriving mid-flow, exactly as Grand Live's backstage 10
    # must -- stomping any of them back to 1 drops the client out of the race
    # with the race still pending.
    playing_states = Scenario.playing_states | {
        impl.PLAYING_STATE_TEAM_RACE,
        impl.PLAYING_STATE_TEAM_RACE_RUNNING,
        impl.PLAYING_STATE_TEAM_RACE_RESULT,
    }

    # ------------------------------------------------- shared-content gates --
    # All three are OFF because the capture says these NPCs are not in this
    # scenario at all. chara_info.evaluation_info_array in a real Unity Cup run
    # carries NPC targets 101/103/104/106/108 and NEVER 102 (Director Akikawa)
    # or 2001 (Happy Meek) -- while a Trackblazer run's DOES carry 102, so this
    # is genuinely per-scenario rather than a Global-wide removal. Leaving them
    # on served URA's 1013/1014/1015 chain on turns 2-4 and Happy Meek's 102005
    # unlock on turn 5 inside a Unity Cup career, announcing two characters who
    # then have no evaluation row to appear in.

    # single_mode_events.SCENARIO_SCHEDULE (1013/1014/1015 + 102005). Unity
    # Cup's own chain replaces it -- producers.BEATS turns 2-7.
    uses_fixed_schedule = False

    # The Director's bond-scaled sendoff and his "Super Successful Event!":
    # he is target 102, which this scenario never has.
    has_director_ending = False

    # Happy Meek's marker, her duels and her URA Finals round-3 slot. She is
    # chara 2001 and likewise absent.
    has_versus_npc = False

    # ...but this scenario has its OWN pair in that slot: Bitter Glasse (chara
    # 2002) and Little Cocon (chara 2003), the Unity Cup's final challenge.
    #
    # master.mdb gives each the same three-row shape it gives Meek -- a weak
    # 9xx row and two stronger ones -- and 2002101/2003101 are the strongest,
    # exactly as 2001902 is hers. (Their 3004xxx/3006xxx rows are a different
    # job: those are the two NAMED runners inside Team Zenith's fifteen, and
    # the team race already seats them -- see impl.boss_npc_ids.)
    #
    # They have no card_data row, so the client can only render them through
    # their own npc race_dress_id (200201 / 200301), the same constraint that
    # governs Meek.
    finals_rival_npc_ids = (2002101, 2003101)

    # THE URA FINALE'S OWN CUTSCENES, and they are NOT URA's. This scenario
    # runs the same three finale programs but wraps each in the 101001-101006
    # family, with URA's 4000001xx stories rather than URA-the-scenario's
    # 4000010xx ones. All four captured runs agree, request for request:
    #
    #   turn 74  race_entry (101001, 400000101, t2)  race_out (101002, 400000104, t3)
    #   turn 76  race_entry (101003, 400000102, t2)  race_out (101004, 400000105, t3)
    #   turn 78  race_entry (101005, 400000103, t2)  race_out (101006, 400000106, t3)
    #
    # Serving URA's 11000/11003/11001/11004/11002/11005 here played the wrong
    # three cutscenes with the right rewards -- the player saw URA's finale
    # story in a Unity Cup run (user-reported 2026-09-06). Trackblazer proves
    # the same point from the other side: its capture runs 203101-203106.
    #
    # The turn-78 pair is BOOKENDED by two more beats that belong to this
    # scenario alone -- 201110 "...: Reunion" (timing 2, served in the
    # check_event right after race_entry) and 201111 "...: Victory!" (timing 3,
    # right after race_out). Those live in producers.BEATS.
    #
    # The sort ids and the round-3 choice used to be carried over from URA's
    # table on the assumption that the shape matched. It does not. Every
    # captured entry of these six agrees (8 records each):
    #
    #   101001/101003/101005  ZERO choices, show_clear 0, timing 2
    #   101002/101004/101006  one choice, show_clear 1, sort ids 12 / 13 / 14
    #
    # So round 3's pre has no acknowledge choice (it was False for rounds 1-2
    # and True here purely by inheritance), and the sort ids are 12/13/14, not
    # URA's 9/10/11.
    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }

    # ------------------------------------------------------------- endpoints --
    def extra_endpoints(self) -> dict:
        return {
            "opponent_list": endpoints.handle_opponent_list,
            "team_edit": endpoints.handle_team_edit,
            "team_race_analyze": endpoints.handle_team_race_analyze,
            "team_race_start": endpoints.handle_team_race_start,
            "team_race_end": endpoints.handle_team_race_end,
            "team_race_out": endpoints.handle_team_race_out,
            "team_race_end_out": endpoints.handle_team_race_end_out,
            "team_race_continue": endpoints.handle_team_race_continue,
            "save_team_edit_flag": endpoints.handle_save_team_edit_flag,
        }

    # -------------------------------------------------------------- response --
    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def command_info(self, full_state, chara_info, home_commands):
        return preview.command_info(full_state, chara_info, home_commands)

    def partner_placements(self, full_state, chara_info, turn, occupied=None):
        """The scouted teammates' facility portraits.

        command_info() then reads them back OUT of home_info rather than
        rolling its own, so the overlay and the training screen can never
        disagree about who is standing where."""
        return preview.scout_placements(full_state, chara_info, turn, occupied)

    # -------------------------------------------------------------- training --
    def training_failure_override(self, full_state, chara_info, command_id,
                                  partner_ids):
        return preview.training_failure_override(full_state, chara_info,
                                                 command_id, partner_ids)

    def levels_facilities_by_training(self) -> bool:
        return False

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        return preview.training_award_bonus(full_state, chara_info, command_id)

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        preview.award_training_gains(full_state, chara_info, payload)

    # ------------------------------------------------------------------ flow --
    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        """A team-race turn whose race has not been run yet -- the display slot
        is held open for the gate, and the served turn stops advancing."""
        return impl.race_turn_pending(impl.state(full_state), turn)

    def begin_hold(self, full_state: dict, turn: int) -> None:
        impl.begin_race_hold(impl.state(full_state), turn)

    def held_turn(self, full_state: dict) -> int:
        # The run's OWN turn goes with it: race_hold uses it as the brick
        # guard, since a hold whose last beat never comes back is otherwise
        # unreleasable and the career is stuck on that turn forever.
        from ...handlers import single_mode_team as smt
        career = full_state.get(smt.STATE_KEY) or {}
        chara = ((career.get("data") or {}).get("chara_info") or {})
        return impl.race_hold(impl.state(full_state), chara.get("turn"))

    def retime_queued(self, full_state: dict, endpoint: str = "") -> None:
        """A race gate carries the timing of the response that delivers it --
        10 after a training, 3 after a race. See producers.retime_race_gate."""
        producers.retime_race_gate(full_state, endpoint)

    def chain_entry_after(self, full_state: dict, event_id):
        """The team race's result beats, one per response -- see
        impl.next_post_race_beat."""
        return impl.next_post_race_beat(impl.state(full_state), event_id)

    def gated_npc_charas(self) -> frozenset:
        """Riko Kashimoto (9006) is this scenario's own unintroduced NPC, the
        same shape as Light Hello in Grand Live: until "The Acting Director"
        she is not on the training screen at all, so her card cannot be
        trained with, cannot take bond, and cannot meet anybody."""
        return frozenset({impl.RIKO_CHARA_ID})

    def persist_upkeep(self, full_state: dict, chara_info: dict) -> None:
        """Riko's presence flag, written where the game reads it back.

        apply_riko_gate also runs in attach, but attach only edits the copy
        going out on the wire -- the facility placement roll
        (single_mode_team._roll_distribution) reads is_appear off the SAVED
        career on the next exec_command, and there her row was still whatever
        career start left it: 0. So "The Acting Director" fired, the response
        said she appears, and she was still never placed in a facility
        (user-reported 2026-09-08: "why does Riko not appear at all in training
        even after she was unlocked")."""
        impl.apply_riko_gate(impl.state(full_state), chara_info)

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        """Two story beats move team state, and both are hung off the EVENT
        rather than off a turn number so the roster and the cutscenes can never
        disagree about when the team exists.

        "What's the Unity Cup?" unlocks scouting; "Team Support" promotes the
        player's six support cards from SemiMember to full member. Neither
        creates anybody -- ensure_roster seeds the whole opening roster on turn
        1 so the names are on the wire before the cutscene needs them.

        Called for EVERY event id, so both handlers stay idempotent: they only
        set a flag and let ensure_roster reconcile."""
        st = impl.state(full_state)
        if event_id and event_id == st.get(impl.POST_RACE_LAST_KEY):
            # The last post-race beat has been acknowledged, so the team-race
            # turn is over: let the served turn move on. The capture ticks
            # 24 -> 25 on exactly this response (0227).
            impl.release_race_hold(st)
        if event_id == producers.ACTING_DIRECTOR_EVENT:
            # "The Acting Director" -- Riko Kashimoto's pal-card outing opens
            # here. A standalone branch, not part of the chain below: 201026 is
            # also an ordinary story beat and still falls through to the
            # scout-join reconciler like every other one.
            impl.acting_director_resolved(st)
            # ...and onto the SAVED chara_info, on this very response: this is
            # the frame the capture flips 106 on (0142 -> 0143), and the flip
            # has to survive into the state the next turn's placement roll
            # reads. `chara_info` here is the career's own dict.
            impl.apply_riko_gate(st, chara_info)
        if event_id in (producers.FACILITY_LEVEL_EVENT[0],
                        producers.FACILITY_LEVEL_EVENT_POST_CUP[0]):
            # "Growing as a Team" IS the facility level-up: the capture moves
            # training_level_info_array on the response right after this ack,
            # never on the one that carries the event.
            impl.facility_level_up_resolved(st)
        level = producers.power_up_level(event_id)
        if level:
            # "Team Power Increased" -- the cutscene IS the award. team_power
            # moves here and nowhere else; see impl.refresh_power.
            impl.power_up_resolved(st, level)
        elif event_id == producers.SCENARIO_INTRO_EVENT:
            impl.scenario_intro_resolved(st)
        elif event_id == producers.TEAM_SUPPORT_EVENT:
            impl.team_support_resolved(st, chara_info)
        elif producers.team_race_join_round(event_id):
            # "After the Race: New Members Join!" -- THE INTAKE HAPPENS HERE,
            # not at team_race_out, so the roster grows in the same response
            # that acknowledges the beat and the client has a delta to render
            # its join list from. Same rule as the deck six above.
            impl.join_wave_resolved(st, producers.team_race_join_round(event_id))
        elif event_id in producers.RACE_GATE_EVENTS:
            # "Before the Nth Round" -- OPEN THE RACE SCREEN. The empty chain
            # holds_chain_after leaves behind is only half the cue; the other
            # half is playing_state 7, and without it the client acknowledges
            # the cutscene and goes straight back to training (user-reported
            # twice, 2026-09-05: 'the Unity Cup opened but the team select and
            # the race never happen'). Capture 0110 -> 0111 is the whole
            # difference: playing_state 5 -> 7 on the response that empties the
            # chain, and team_edit / opponent_list / team_race_start follow.
            chara_info["playing_state"] = impl.PLAYING_STATE_TEAM_RACE
            return {"playing_state": impl.PLAYING_STATE_TEAM_RACE}
        else:
            # "The Word Spreads" -- mark the teammate it announced as
            # introduced, so the next wave's join gets its own showing instead
            # of the same one replaying.
            impl.scout_join_resolved(st, event_id, producers.scout_join_events(st))
        return {}

    # Endpoints that must NOT settle a banked race award. The three race-flow
    # ones still belong to the race the award came from -- team_race_out in
    # particular has to serve the OLD rank with the new one as tmp_team_rank,
    # or the rank-up screen animates a number onto itself.
    _UNSETTLED_ENDPOINTS = ("team_race_start", "team_race_end",
                            "team_race_out", "team_race_end_out",
                            "team_race_continue")

    def settle(self, full_state, chara_info, endpoint: str = "") -> bool:
        """Pay a finished team race: the ladder move, the teammates' stats, the
        reward panel and the trainee's own stats and skill points.

        chara_info here is the RESPONSE's copy; the career's persisted one is
        the source of truth, so the gains are applied there and mirrored out.
        """
        if endpoint.rsplit("/", 1)[-1] in self._UNSETTLED_ENDPOINTS:
            return False
        st = impl.state(full_state)
        from ...handlers import single_mode_team as smt
        career = full_state.get(smt.STATE_KEY)
        saved = ((career or {}).get("data") or {}).get("chara_info")
        target = saved if isinstance(saved, dict) else chara_info
        # A race block from an older build crashes the client on every load;
        # clearing it is the only way such a career opens at all. The reset
        # playing_state has to land on the PERSISTED chara_info as well, or the
        # next load resumes into the same dead screen.
        stale = impl.discard_stale_pending(st, target)
        # ... and a career already parked on that screen with nothing to resume
        # into cannot be opened at all until it is moved off it.
        stale = impl.recover_stuck_screen(st, target, endpoint) or stale
        if stale and isinstance(chara_info, dict) and target is not chara_info:
            chara_info["playing_state"] = target.get("playing_state")
        if not st.get("pending_award"):
            return stale
        if not impl.settle_race_award(st, target):
            return stale
        if isinstance(chara_info, dict) and target is not chara_info:
            for key in impl.STATS + ("skill_point",):
                chara_info[key] = target[key]
        return True

    def holds_chain_after(self, event_id) -> bool:
        """The five "Before the Nth Round" beats must leave the chain EMPTY.

        That empty unchecked_event_array is what makes the client open the
        team-race screen (team_edit -> opponent_list -> team_race_start). Serve
        anything else in the same response and the race never opens; the turn
        just advances. See producers.RACE_GATE_EVENTS."""
        return event_id in producers.RACE_GATE_EVENTS

    def snapshot(self, full_state: dict) -> dict:
        """Per-run facts frozen into the finished veteran record at graduation,
        before the NEXT career's reset() clears them.

        The final team_rank is the one that matters: race_single_mode_team_status
        turns it into the trainee's graduation stat bonus, and it is the number
        any future mission/epithet condition about Unity Cup would ask for."""
        st = impl.state(full_state)
        title_id = impl.team_title(st)
        races = list(st.get("races") or ())
        return {
            "unity_cup_team_rank": int(st.get("team_rank") or 30),
            "unity_cup_races": [dict(r) for r in races],
            "unity_cup_final_stat_bonus":
                impl.final_rank_stat_bonus(int(st.get("team_rank") or 30)),
            # ---- epithet facts (152-155, 163; 162 comes from race_history) ----
            # team_titles is a LIST of every title the team held, not just the
            # last: the title is 1 + races done (impl.team_title), so it only
            # ever climbs, and epithet 154 asks whether "Superstardom" was ever
            # OBTAINED. Serving the whole ladder up to where it stopped is what
            # makes that an honest yes.
            "team_titles": [impl.team_title_name(i)
                            for i in range(1, title_id + 1)],
            "unity_cup_team_title": title_id,
            "spirit_bursts": int(st.get("bursts") or 0),
            "unity_cup_extreme_bursts": int(st.get("extreme_bursts") or 0),
            "unity_training_count": int(st.get("unity_trainings") or 0),
            "unity_training_max_chars": int(st.get("unity_training_max_chars") or 0),
            # "Win the Unity Cup" (mission condition_type 100033): the LAST of
            # the five team races won. The final race is the Cup itself -- the
            # earlier four are the league climb -- so the league rank is not the
            # question and neither is the win/loss tally. result_state 1 is a
            # win (endpoints._RESULT_WIN); 2 is a loss and 3 a draw.
            "unity_cup_won": bool(
                len(races) >= len(impl.TEAM_RACE_TURNS)
                and int((races[-1] or {}).get("result_state") or 0) == 1),
        }

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)
        full_state.pop(preview.PREVIEW_KEY, None)


register(UnityCup())
