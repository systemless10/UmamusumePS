"""GRAND LIVE (scenario 3) -- "Brighter Together: Our Grand Concert".

This class is the whole seam between Grand Live and the rest of the server.
Shared career code never imports anything below this package: it asks the
registry for the scenario a career is being played in and calls hooks. What used
to be 22 `grand_live.is_active(...)` branches scattered through
single_mode_team.py, career_producers.py, event_engine.py and trained_chara.py
is now the lookup that returns this object.

WHAT LIVES WHERE
    impl.py       the mechanics -- tokens, songs, the lesson board, the Lives
    producers.py  the story beats and their resolvers
    endpoints.py  master_square / live_start, the two endpoints URA has no
                  counterpart for
    preview.py    the per-facility token preview and banking it

Read impl.py's docstring first: it carries the wire shape, the capture it was
derived from, and which numbers are KNOBs rather than ground truth.
"""

from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, impl, preview, producers


class GrandLive(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Grand Live"

    # Grand Live posts its career to single_mode_live/*. Every shared endpoint
    # takes the SAME request and returns the same response URA's does -- the
    # only wire difference is the data_set swap attach() performs -- so main.py
    # aliases the whole family onto these same handlers.
    endpoint_prefix = "single_mode_live"
    data_set_key = "live_data_set"

    # Tokens/songs/lesson board, plus the cached per-turn token preview.
    state_keys = (impl.STATE_KEY, preview.PREVIEW_KEY)

    # 10 = backstage, the Live screen (Anticipation gauge, Lessons, the concert
    # button). Added to the base set, which already protects 5 ("event chain
    # still running") -- here that is the concert's own result chain. A
    # check_event arriving in either must not stomp playing_state back to 1: it
    # exits the Live screen with the concert still pending and the career stuck
    # on that turn (live-reported softlock, 2026-08-26).
    playing_states = Scenario.playing_states | {impl.PLAYING_STATE_BACKSTAGE}

    # Happy Meek is not in this scenario at all -- no evaluation row anywhere in
    # the capture, and her unlock event never fires -- so no marker, no duels,
    # no Finals round-3 slot.
    has_versus_npc = False

    # The NPC band Grand Live shows as locked from turn 1. Without these rows
    # present at is_appear 0 the client has no 0 -> 1 diff to announce, so the
    # Director (202002, with Light Hello) and the Reporter (101007, "A Quirky
    # Correspondent?") unlocked in total silence -- both user-reported. Copied
    # from the capture exactly, 105 deliberately absent (Light Hello is
    # revealed through her deck position): see base.Scenario.locked_npc_rows.
    locked_npc_rows = ((101, 9001), (102, 9002), (103, 9003),
                       (104, 9004), (106, 9006))

    # The Director's sendoff and his "A Super Successful Event!" are URA
    # exclusive (user-supplied 2026-08-16: they played here and should not
    # have). The Reporter's equivalent is confirmed fine in both.
    has_director_ending = False

    # single_mode_events.SCENARIO_SCHEDULE (1013/1014/1015/1016 + Happy Meek's
    # unlock) never fires in the capture; Grand Live's own chain replaces it.
    uses_fixed_schedule = False

    # The URA Finale itself DOES run here -- 101001-101006 on turns 74/76/78,
    # exactly like URA.
    has_ura_finals = True

    # ...BUT NOT WITH URA'S CUTSCENES. This was a comment with no code behind
    # it: finals_events stayed None, so single_mode_team._finals_events_for fell
    # back to URA's 11000-11005 table and a Grand Live run played URA's finale
    # story. The corpus is unambiguous -- across the 66 scenario-3 careers in
    # captures/bot_logs, 101001/101002 ack on turn 74 in 38 of 38 careers that
    # reached it, 101003/101004 on 76 in 38, 101005 on 78 in 36 and 101006 in
    # 32, and URA's 11000-11005 appear in ZERO of them.
    #
    # The pairing is Unity Cup's, verbatim: the same six ids carry the same six
    # stories with the same timings in every capture that records both
    # (400000101/104 t2/t3, 400000102/105, 400000103/106 -- 8 records each).
    # Kept as its own table rather than importing the scenario-2 one, because
    # nothing guarantees the two stay equal and a shared literal would hide it
    # if they ever diverge; Trackblazer already proves the family is per
    # scenario (203101-203106).
    # Shapes are the captured ones, not URA's: all three pres carry ZERO
    # choices and show_clear 0, and the posts carry one choice, show_clear 1
    # and sort ids 12/13/14 (see the same table in scenarios/unity_cup).
    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }

    # THE RECURRING FAMILIES ARE URA'S MECHANICS WITH GRAND LIVE'S IDS.
    # master.mdb single_mode_story_data (whose `id` IS the event id for these)
    # lays them out unmistakably:
    #
    #   202056-202060  "Training Level Up"          story 400000005 (URA: id 11)
    #   202061-202064  "Director's Appraisal"       stories 400000009-012
    #                                               (URA: ids 7/8/9/10)
    #   202065-202068  "Event Producer's Appraisal" stories 400003100-103
    #                                               (URA's Reporter: 12/13/14/15)
    #
    # They were modelled as one-shot fixed-turn scenario beats in
    # GRAND_LIVE_BEATS instead, each with a `gl:<id>` once_key, so seven of them
    # fired exactly once per career and 202058/202059/202065-068 never fired at
    # all. Real fires them repeatedly: over the 66 scenario-3 careers in
    # captures/bot_logs, 202056 alone is 111 acks across 46 careers.
    LEVELUP_EVENTS = {101: 202056, 102: 202057, 103: 202058,
                      105: 202059, 106: 202060}

    def facility_levelup_event(self, command_id) -> tuple:
        """One id PER FACILITY here, unlike URA's single 1024.

        Correlating every level-up ack against the exec_command of the same
        turn is exact with no exceptions: 202056 x111 all on command 101,
        202057 x53 all on 102, 202058 x22 all on 103, 202059 x49 all on 105,
        202060 x90 all on 106. The story is 400000005 for all five, same as
        URA's."""
        return (self.LEVELUP_EVENTS.get(int(command_id or 0), 202056), 400000005)

    def appraisal_events(self):
        """Same bond tiers as URA (0/40/70/90 from single_mode_evaluation) and
        the same pay-outs, under this scenario's ids. The same correlation run
        shows 202061-202068 spread across ALL five facilities -- they are bond
        tiers, not per-facility, exactly like URA's."""
        return {
            102: [(90, 202064, 5), (70, 202063, 4), (40, 202062, 3), (0, 202061, 2)],
            103: [(90, 202068, 5), (70, 202067, 4), (40, 202066, 3), (0, 202065, 2)],
        }

    # ------------------------------------------------------------- endpoints --
    def extra_endpoints(self) -> dict:
        return {"master_square": endpoints.handle_master_square,
                "live_start": endpoints.handle_live_start,
                "reserve_square": endpoints.handle_reserve_square,
                "lottery_square": endpoints.handle_lottery_square}

    # -------------------------------------------------------------- response --
    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def command_info(self, full_state, chara_info, home_commands):
        return preview.command_info(full_state, chara_info, home_commands)

    def held_turn(self, full_state: dict) -> int:
        # Set by "The Nth Concert Begins!", released by "New Supporters!" -- the
        # exact window the official holds chara_info.turn across.
        return int(impl.state(full_state).get("concert_turn") or 0)

    # -------------------------------------------------------------- training --
    def friendship_bonus_pct(self, full_state: dict) -> int:
        return impl.friendship_bonus_pct(full_state)

    def specialty_bonus(self, full_state: dict) -> int:
        return impl.live_bonuses(full_state).get("specialty", 0)

    def training_bonus(self, full_state: dict):
        return impl.training_bonus_for(full_state) or None

    def support_event_bonus_pct(self, full_state: dict) -> int:
        # live_bonus_type 2 (サポート連続イベント率) raises the CHAIN share.
        return impl.live_bonuses(full_state).get("support_event", 0)

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        preview.award_tokens(full_state, chara_info, payload)

    def apply_currency_gain(self, full_state, chara_info, amount):
        target = impl.grant_lowest_performance(full_state, chara_info, amount)
        if target is None:
            return None
        return impl.perf_wire_target(target), amount

    # ------------------------------------------------------------------ flow --
    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        """A concert turn whose Live has not been performed yet."""
        live_type = impl.live_turn_type(int(turn or 0))
        if live_type is None:
            return False
        done = {r.get("live_type")
                for r in (impl.state(full_state).get("lives_done") or ())}
        return live_type not in done

    def begin_hold(self, full_state: dict, turn: int) -> None:
        # Held from the moment exec_command lands on the concert turn, NOT from
        # when "Concert Begins" resolves several responses later: the client was
        # told 24 -> 25 by that very response and then snapped back to 24
        # mid-chain, having already drawn turn 25's home screen -- the reported
        # "ghost turn" before the padlock.
        impl.state(full_state)["concert_turn"] = int(turn or 0)

    def npc_supporters(self, full_state, chara_info) -> list:
        """Supporters who joined our cause and own NO equipped support card.

        One who IS in the deck is already on stage as her card and must not
        appear again as a second, cardless copy of herself. Same for the
        trainee, who is on stage as herself."""
        trainee = int(chara_info.get("card_id") or 0) // 100
        equipped = {impl.support_card_chara(c.get("support_card_id") or 0)
                    for c in chara_info.get("support_card_array", []) or []}
        return [int(c) for c in producers.supporters_for(full_state)
                if int(c) != trainee and int(c) not in equipped]

    def gated_npc_charas(self) -> frozenset:
        """Light Hello (9008) -- "Bring Back the Grand Concert!" (202002, turn
        4) is what puts her on the training screen, card or no card."""
        return frozenset({9008})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        return producers.apply_beat_rewards(full_state, chara_info, event_id)

    def holds_chain_after(self, event_id) -> bool:
        return event_id in producers.CONCERT_BEGINS_EVENTS

    # The epithet word for each performance token. Four match PERF_NAMES, but
    # the prose says "Composure" where the token is MENTAL and pluralises Vocal/
    # Visual -- and the epithet parser keys the fact off the prose word
    # ("concert_score_" + the word it matched, lowercased), so the wording is
    # what these keys have to spell.
    _SCORE_WORD = {impl.DANCE: "dance", impl.PASSION: "passion",
                   impl.VOCAL: "vocals", impl.VISUAL: "visuals",
                   impl.MENTAL: "composure"}

    def snapshot(self, full_state: dict) -> dict:
        """Songs learned + each concert's result_state, frozen into the finished
        veteran record at graduation -- before the NEXT career's reset() clears
        it. missions.py's SongCountThreshold / SpecificSongObtained /
        ConcertSuccessCount conditions read these back, and epithets 233-241
        read the performance scores and the Grand Concert's rating.

        The five scores are the FINAL token values, not a running total of every
        point ever granted. The cap settles it: tokens start capped at 200 and
        reach 400 after all four Lives, and the epithets ask for 300 in one
        category (237-241) and 1,500 across all five (234) -- exactly 5x300,
        against a 5x400 ceiling. A cumulative reading would make both trivial."""
        import copy
        st = impl.state(full_state)
        tokens = {int(k): int(v) for k, v in (st.get("tokens") or {}).items()}
        lives = st.get("lives_done") or []
        # The Grand Concert (live_type 10) is the last Live and the only one
        # epithets 235/236 are about. Its Great Success and its "special"
        # version are ONE state in this scenario, not two: perform_live gates
        # both on the same >=18-song count (GRAND_CONCERT_SONG_GATE), so the
        # two epithets resolve from the same result_state rather than one of
        # them being guessed at.
        grand = next((r for r in lives
                      if int(r.get("live_type") or 0) == impl.GRAND_LIVE_TYPE), None)
        grand_great = bool(grand) and             int(grand.get("result_state") or 0) == impl.RESULT_GREAT
        out = {"grand_live_songs": list(st.get("songs") or []),
               "grand_live_lives_done": copy.deepcopy(lives),
               "performance_points": sum(tokens.get(t, 0) for t in impl.PERF_TYPES),
               "grand_concert": bool(grand),
               "grand_concert_great_success": grand_great,
               "special_grand_concert": grand_great,
               "songs_obtained": len(st.get("songs") or ())}
        for t, word in self._SCORE_WORD.items():
            out["concert_score_" + word] = tokens.get(t, 0)
        return out

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)
        full_state.pop(preview.PREVIEW_KEY, None)


register(GrandLive())
