"""TRACKBLAZER (scenario 4) -- "Start of the Climax".

This class is the whole seam between Trackblazer and the rest of the server.
Shared career code never imports anything below this package: it asks the
registry for the scenario a career is being played in and calls hooks.

WHAT LIVES WHERE
    impl.py       the mechanics -- the two currencies, the route, the Climax,
                  Rival Races, and the free_data_set envelope
    shop.py       the Pro Shop -- the reset windows, stock generation, buying,
                  using, and the nine item effect types
    endpoints.py  multi_item_exchange / multi_item_use, the two endpoints URA
                  has no counterpart for
    producers.py  the scenario's own fixed story chain

Read impl.py's docstring first: it carries the wire shape, the capture it was
derived from, and which numbers are KNOBs rather than ground truth.
docs/URA_VS_TRACKBLAZER.md carries the full comparison with URA and
docs/TRACKBLAZER_SHOP.md the shop's derivation.

WHAT TRACKBLAZER ACTUALLY REPLACES. Not the calendar -- single_mode_turn's
turn_set 4 differs from URA's turn_set 1 on exactly one gameplay column
(`unique_command`, the shop button, on turns 13-78) -- and not the finale's
three race slots, which stay on turns 74/76/78. What it replaces is the
OBJECTIVE and the ECONOMY: per-uma goal races become four shared Grade Point
targets on a route chosen by aptitude, the finale becomes a cumulative Victory
Point standings over a 16-runner field, and a whole shop economy runs alongside
the career that URA has no trace of.
"""

from __future__ import annotations

from ... import career_events as CE
from ..base import Scenario
from ..registry import register
from . import endpoints, impl, producers, shop      # noqa: F401 -- registers producers


class Trackblazer(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Trackblazer"

    # Trackblazer posts its career to single_mode_free/*. Every shared endpoint
    # takes the SAME request and returns the same response URA's does -- the
    # only wire difference is the data_set swap attach() performs -- so main.py
    # aliases the whole family onto the same handler objects.
    endpoint_prefix = "single_mode_free"
    data_set_key = "free_data_set"

    # Coins, Grade Points, inventory, active item effects, the shop lineup, the
    # Climax standings and the per-turn Rival Race roll all live in one key.
    state_keys = (impl.STATE_KEY,)

    # This scenario's career opens on a DIFFERENT chara beat. Real
    # single_mode_free/start serves event_id 4000 / story 501068900
    # ("Self-Introduction", story suffix 900) where the other three scenarios
    # serve 3000 / suffix 400 ("Introducing <trainee>!"); Tazuna's tips (1001)
    # follow unchanged in both. Both real 20260905 single_mode_free starts
    # carry exactly that entry, and the bot logs agree with no exceptions:
    # across 1741 scenario-4 careers turn 1 opens on 4000 every time and on
    # 3000 never, while scenarios 1/2/3 (20/147/66 careers) open on 3000 and
    # never on 4000. Story suffix 900 exists for all 83 trainees that have a
    # suffix 400, so no trainee is left without one.
    intro_chain = (4000, 1001)

    # ------------------------------------------------- shared-content gates --
    # single_mode_events.SCENARIO_SCHEDULE (URA's 1013/1014/1015 opening chain
    # plus Happy Meek's 102005 unlock) never fires in the capture -- turns 1-5
    # of a real Trackblazer run serve 4000, 1001, 203026, 10002, 203301, 20039
    # and nothing else. This scenario's own chain replaces it.
    uses_fixed_schedule = False

    # Happy Meek's marker, her duels and her URA-Finals round-3 slot. She IS in
    # this scenario, but only as one of the Twinkle Star Climax runners
    # (npc 2001901, alongside Bitter Glasse and Little Cocon) -- there is no
    # duel chain and no round-3 rival slot, because the Climax has no round-3
    # rival: it has a 16-runner field and a points table.
    has_versus_npc = False

    # The Director's bond-scaled sendoff and his "A Super Successful Event!".
    # The capture's turn 78 is 203106 -> 203202 -> finish, with neither of them
    # in it.
    has_director_ending = False

    # ...and neither is the rest of URA's ending chain: no "Twinkle Monthly
    # Special Issue", no per-trainee 5000xxx111 ending story, no end-of-run
    # payouts. 203202 IS Trackblazer's ending, and it is the last event of the
    # run (capture 0506 -> 0507 -> 0508, then gain_skills/factor_select/finish).
    has_ending_chain = False

    # The per-trainee SECRET events are off -- MANT: "An Uma's Career Goals and
    # Secret Events are disabled in Trackblazer". The goal half of that sentence
    # is already true by construction: the route is chosen by aptitude and has
    # chara_id 0, so there is no per-uma goal race to fire one from.
    has_secret_events = False

    # ...and with no goals, the DEBUT is an ordinary voluntary race: the
    # placement event ('Victory!' / 'Solid Showing' / 'Defeat'), two options
    # with different energy costs, -25 or so off the bar. Both real debut
    # captures this behaviour was read off are scenario 4 (see
    # scenarios/base.py's debut_is_goal_race, which every other scenario keeps
    # at True).
    debut_is_goal_race = False

    # RACING IS FREE ON THE THREE ROUND-FINAL TURNS: no Race Fatigue there,
    # however many races in a row led up to them (user-confirmed, and the only
    # scenario it holds for). Straight out of the corpus -- of streak-3+ races
    # on these turns across 1,741 careers, 0 of 450 on turn 24, 0 of 93 on 48
    # and 0 of 314 on 72 produced a fatigue event, against 55-100% on every
    # other turn that can carry one.
    race_fatigue_exempt_turns = frozenset({24, 48, 72})

    # The three finale races still run, on the same turns.
    has_ura_finals = True

    # ...WRAPPED IN TRACKBLAZER'S OWN CUTSCENES, which are not URA's. Read off
    # the capture, request for request:
    #
    #   turn 74  race_entry (203101, 400004050, t2, no choice)
    #            race_out   (203102, 400004051, t3)
    #   turn 76  race_entry (203103, 400004060, t2)  race_out (203104, 400004061, t3)
    #   turn 78  race_entry (203105, 400004070, t2)  race_out (203106, 400004071, t3)
    #
    # Only the first leg's pre-race beat is choice-less; the other two carry the
    # acknowledge choice, which is the opposite of URA's shape (there only the
    # turn-78 one does) and is why this cannot be inferred from URA's table.
    # sort ids 9/10/11 on the posts are carried over from URA's -- the capture
    # shows the same show_clear shape and nothing in it contradicts them.
    # Post tuples are (event_id, story_id, show_clear_sort_id, show_clear).
    # The sort ids were URA's 9/10/11 by inheritance; the capture says 5/6/7,
    # and this family's show_clear is 5, not 1 -- both confirmed on every
    # captured entry. The pres are right as they stand: 203101 carries no
    # choice, 203103 and 203105 carry one.
    finals_events = {
        1: {"pre": (203101, 400004050, False), "post": (203102, 400004051, 5, 5)},
        2: {"pre": (203103, 400004060, True), "post": (203104, 400004061, 6, 5)},
        3: {"pre": (203105, 400004070, True), "post": (203106, 400004071, 7, 5)},
    }

    # ------------------------------------------------------------- endpoints --
    def extra_endpoints(self) -> dict:
        return {"multi_item_exchange": endpoints.handle_multi_item_exchange,
                "multi_item_exchange2": endpoints.handle_multi_item_exchange2,
                "multi_item_use": endpoints.handle_multi_item_use}

    # -------------------------------------------------------------- response --
    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def settle(self, full_state, chara_info, endpoint=""):
        """Publish a Climax leg's new standing, one response after the pair that
        animates it. See impl.promote_ranking."""
        return impl.promote_ranking(full_state, endpoint)

    def held_turn(self, full_state):
        """Freeze the served turn for the same one response promote_ranking
        holds the standing on, or the ranking screen softlocks. See
        impl.held_turn."""
        return impl.held_turn(full_state)

    def command_info(self, full_state, chara_info, home_commands):
        """The per-facility overlay -- empty apart from an active shop item's
        OWN SHARE of the button's number (see scale_training_preview, which
        runs earlier in the same response and leaves it in
        impl.BONUS_PREVIEW_KEY for this to collect). Every capture with no
        item active shows five entries with an EMPTY params array, which is
        what an idle facility still gets here.

        `rolled` is also this hook's OTHER job: the chokepoint's "persist
        this" signal, for the shop lineup and Rival Race rolls that must
        survive the response that made them. See impl.tick."""
        commands = [{"command_type": 1, "command_id": cid,
                     "params_inc_dec_info_array":
                         impl.take_bonus_preview(full_state, cid)}
                    for cid in impl.TRAINING_COMMAND_IDS]
        return commands, impl.tick(full_state, chara_info)

    def career_route(self, chara_info):
        """Trackblazer's routes are shared (chara_id 0) and chosen by APTITUDE,
        so the lookup needs the trainee's aptitudes rather than her id."""
        return impl.career_route(chara_info)

    # -------------------------------------------------------------- training --
    def scale_training_preview(self, full_state, chara_info, command_id, params):
        """Fold the active shop buffs into the button the player reads.

        Megaphones raise every facility, ankle weights raise ONE facility and
        raise its energy cost with it -- the two halves of an ankle weight are
        one item and are applied together here, which is the whole reason this
        hook takes the finished preview rather than feeding the formula.

        Also stashes the ITEM'S OWN SHARE of each changed number into
        impl.BONUS_PREVIEW_KEY, keyed by this facility's command_id -- Grand
        Live and Unity Cup both show this same bonus-only breakdown as its own
        badge on the facility, layered on top of the (already-scaled) number
        here rather than folded invisibly into it (their preview.py modules'
        own params_inc_dec_info_array; see impl.held_turn's neighbour,
        impl.take_bonus_preview, for the read side). Home_info's own number
        stays the combined total -- this hook's actual job -- so the two
        cannot disagree."""
        st = impl.state(full_state)
        turn = int(chara_info.get("turn") or 1)
        gain = shop.training_bonus_pct(st, turn, command_id)
        cost = shop.energy_cost_pct(st, turn, command_id)
        if not gain and not cost:
            # Stashed even when empty: an item that just expired must clear
            # its OLD badge here too, not leave the last turn's bonus cached.
            impl.stash_bonus_preview(full_state, command_id, [])
            return params
        out = []
        bonus = []
        for entry in params:
            target = int(entry.get("target_type") or 0)
            value = int(entry.get("value") or 0)
            if target == 10:
                # Energy: a training's is NEGATIVE, so a cost increase makes it
                # more negative. A positive one (rest, recreation) is left alone
                # -- an ankle weight does not scale a nap.
                if cost and value < 0:
                    scaled = -((-value) * (100 + cost) // 100)
                    bonus.append({"target_type": target, "value": scaled - value})
                    value = scaled
            elif gain and 1 <= target <= 5:
                scaled = value * (100 + gain) // 100
                delta = scaled - value
                if delta:
                    bonus.append({"target_type": target, "value": delta})
                value = scaled
            out.append({**entry, "value": value})
        impl.stash_bonus_preview(full_state, command_id, bonus)
        return out

    def training_failure_override(self, full_state, chara_info, command_id,
                                  partner_ids):
        """The Good-Luck Charm (item 10001): failure rate 0 for one turn.

        Applied to the SERVED rate, so the button the player reads and the roll
        the server makes cannot disagree."""
        turn = int(chara_info.get("turn") or 1)
        if shop.zero_failure(impl.state(full_state), turn):
            return 0
        return None

    def placement_salt(self, full_state) -> int:
        """The Reset Whistle (item 7001) re-rolls this turn's support placement.
        The roll is seeded by (card_id, turn) so a turn's layout is stable when
        rebuilt -- correct, and exactly why a re-roll needs a salt to move."""
        return int(impl.state(full_state).get("placement_salt") or 0)

    # ---------------------------------------------------------------- races --
    def race_bonus_pct(self, full_state, chara_info) -> int:
        """Cleat Hammers, +20% / +35% for the turn they are used on."""
        return shop.race_bonus_pct(impl.state(full_state),
                                   int(chara_info.get("turn") or 1))

    def fan_bonus_pct(self, full_state, chara_info) -> int:
        """Glow Sticks, +50% fans for the turn they are used on."""
        return shop.fan_bonus_pct(impl.state(full_state),
                                  int(chara_info.get("turn") or 1))

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank):
        """Bank a finished race: both currencies, the Climax leg if this was
        one, the Rival Race win reward, and the post-race shop offer roll."""
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or chara_info.get("turn") or 1)
        program_id = race_ctx.get("program_id")
        finals_round = race_ctx.get("finals_round")
        if finals_round:
            # A Climax leg pays Victory Points on its own grade-independent
            # ladder and NO coins -- those rows are in single_mode_free_win_point
            # under race_group 40001-40003 and are absent from _coin_race.
            impl.record_climax_leg(full_state, chara_info, turn, program_id,
                                   int(result_rank))
            return
        grade = impl.grade_of(program_id)
        impl.add_win_points(st, impl.win_points_for(grade, int(result_rank)))
        impl.add_coins(st, impl.coins_for(grade, int(result_rank)))
        rival_win = (int(result_rank) == 1
                     and impl.is_rival_race(full_state, program_id, turn))
        if rival_win:
            reward = impl.rival_win_reward(full_state, chara_info, program_id)
            for stat in reward["stats"]:
                cap = "max_wiz" if stat == "wiz" else f"max_{stat}"
                chara_info[stat] = min(int(chara_info.get(stat) or 0)
                                       + impl.RIVAL_WIN_STAT_BONUS,
                                       int(chara_info.get(cap) or 9999))
            if reward["skill_id"]:
                from ... import event_engine
                event_engine._apply_skill_hint(chara_info, reward["skill_id"], 1)
            # ...AND SHOW THE PLAYER WHY. 203025 "Rival Bested!" is the Rival
            # Race victory cutscene and was the single largest omission in this
            # scenario: 96.2% of real careers ack it, 32,743 times, ~19.6 per
            # career, and we served it never -- the reward above landed with
            # nothing on screen to explain it.
            #
            # CUTSCENE ONLY, no effects: the reward is paid directly above, and
            # putting it on the choice as well would pay it twice. Wire shape is
            # the captured one, 30 identical records -- story 400004013, ONE
            # choice, play_timing 3, chara_id 0, show_clear 0, served in the
            # check_event chain behind race_out.
            CE.emit(full_state, CE.Event(
                event_id=self.RIVAL_WIN_EVENT, story_id=self.RIVAL_WIN_STORY,
                play_timing=3, choices=[CE.Choice(effects=[])], chara_id=0,
                # Fires on every Rival Race win, so the key has to vary per
                # occurrence or every win after the first is deduped away.
                once_key=f"tb:rival_win:{turn}",
                priority=CE.PRIO_TRAILING, source="trackblazer_rival_win"))
        shop.maybe_add_limited_offer(
            full_state, turn, int(result_rank),
            is_debut=(grade in (800, 900)), is_rival_win=rival_win)

    RIVAL_WIN_EVENT = 203025
    RIVAL_WIN_STORY = 400004013

    # THE RECURRING FAMILIES, exactly as in Grand Live -- URA's mechanics under
    # this scenario's ids. master.mdb single_mode_story_data (whose `id` IS the
    # event id across the whole 203xxx band -- 58/58 captured wire pairs agree,
    # zero mismatches) names them:
    #
    #   203019-203023  "Training Level Up"     story 400000005 (URA: id 11)
    #   203046-203049  "Director's Appraisal"  stories 400000009-012
    #
    # Both were filed as "pool events, to be served by shared random-event
    # machinery" -- machinery that does not exist -- so nine ids and ~9,900 acks
    # were served by nothing at all.
    LEVELUP_EVENTS = {101: 203019, 102: 203020, 103: 203021,
                      105: 203022, 106: 203023}

    def facility_levelup_event(self, command_id) -> tuple:
        """One id per facility. Correlating every ack against the exec_command
        of the same turn is exact with no exceptions across the 1,741 real
        scenario-4 careers: 203019 x2369 all on command 101, 203020 x879 all on
        102, 203021 x762 all on 103, 203022 x529 all on 105, 203023 x2886 all
        on 106."""
        return (self.LEVELUP_EVENTS.get(int(command_id or 0), 203019), 400000005)

    def appraisal_events(self):
        """URA's bond tiers and pay-outs under this scenario's ids. The same
        correlation run shows 203046-203049 spread across all five facilities,
        i.e. bond tiers rather than per-facility."""
        return {
            102: [(90, 203049, 5), (70, 203048, 4), (40, 203047, 3), (0, 203046, 2)],
        }

    def rival_race_info(self, full_state, chara_info, turn, program_id):
        """This turn's Rival Race flag, for the free_data_set the response is
        about to carry. Decided once per turn and remembered."""
        return impl.rival_for_turn(full_state, chara_info, turn, program_id)

    # ----------------------------------------------------------------- run ----
    def snapshot(self, full_state: dict) -> dict:
        """Per-run facts frozen into the finished veteran record at graduation,
        before the NEXT career's reset() clears them. Four of the scenario's own
        epithets read these back (180 Result Pts, 181 items bought, 182 the
        Climax sweep, 184 coins earned).

        Each of the three counters is the LIFETIME total, not the live one. That
        distinction is the whole reason they exist: `win_points` is consumed by
        every year-end objective, `coins` is a balance the Pro Shop spends down,
        and `offers` is regenerated each reset window -- so at graduation none of
        the three can say what the run actually earned or bought, and all four
        epithets were unreachable while the snapshot reported them."""
        st = impl.state(full_state)
        legs = (st.get("climax") or {}).get("results") or []
        return {
            # live values, kept for anything that wants the end-state
            "trackblazer_win_points": int(st.get("win_points") or 0),
            "trackblazer_prev_win_points": int(st.get("prev_win_points") or 0),
            "trackblazer_coins": int(st.get("coins") or 0),
            "trackblazer_climax_rank": impl.climax_ranking(full_state),
            "trackblazer_objectives": dict(st.get("objectives") or {}),
            # ------------------------------ epithet facts ------------------
            "result_points": int(st.get("win_points_total") or 0),
            "pro_shop_coins": int(st.get("coins_total") or 0),
            "pro_shop_items": int(st.get("items_bought") or 0),
            # All three legs present AND each one won. `len(CLIMAX_TURNS)` rather
            # than a literal 3 so the check follows the schedule it is about.
            "ts_climax_all_won": (
                len(legs) >= len(impl.CLIMAX_TURNS)
                and all(int(r.get("player_rank") or 0) == 1 for r in legs)),
            "trackblazer_climax_legs": [
                {"turn": int(r.get("turn") or 0),
                 "player_rank": int(r.get("player_rank") or 0)} for r in legs],
        }

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)


register(Trackblazer())
