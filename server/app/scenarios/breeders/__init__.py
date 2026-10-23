
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Breeders(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Breeders"
    endpoint_prefix = "single_mode_breeders"
    data_set_key = "breeders_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    has_ura_finals = False
    has_ending_chain = False
    replaces_goal_race_event = True
    reserve_arrivals_after_race = True
    inspiration_closes_chain = True
    plays_calendar_stories = False
    camp_lists_rest = True
    playing_states = frozenset({5, 40, 41, 43, 44, 47})
    turn_end_hold_state = 5
    npc_targets = frozenset({M.ADVISOR_TARGET})

    LEVELUP_EVENTS = {101: 213801, 102: 213802, 103: 213803, 105: 213804, 106: 213805}

    def race_field(self, chara_info, program_id, count):
        return impl.bc_field(chara_info, program_id, count)

    def facility_levelup_event(self, command_id) -> tuple:
        base = formula.SUMMER_TO_BASE.get(int(command_id or 0), int(command_id or 0))
        return (self.LEVELUP_EVENTS.get(base, 213801), 400000005)

    def gated_npc_charas(self) -> frozenset:
        return frozenset({M.CASINO_CHARA})

    def appraisal_events(self):
        return {102: [(90, 213822, 5), (70, 213821, 4), (40, 213820, 3), (0, 213819, 2)],
                M.ADVISOR_TARGET: [(90, 213826, 5), (70, 213825, 4), (40, 213824, 3), (0, 213823, 2)]}

    def career_route(self, chara_info: dict):
        chara = int((chara_info or {}).get("card_id") or 0) // 100
        return impl.ROUTE_ID, [impl.debut_route_row(chara)]

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        changed = impl.refresh_preview(full_state, chara_info,
                                       {"command_info_array": home_commands or []},
                                       smt._support_card_levels(full_state))
        return None, bool(changed)

    def attach(self, response, full_state, chara_info, command_info_array=None, endpoint=""):
        data = (response or {}).get("data")
        if not isinstance(data, dict) or not isinstance(chara_info, dict):
            return response
        from ...handlers import single_mode_team as smt
        home = data.get("home_info")
        if not isinstance(home, dict):
            home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
        if impl.RI.career_over(chara_info):
            for ci in (chara_info, (data.get("single_mode_load_common") or {}).get("chara_info")):
                if isinstance(ci, dict) and int(ci.get("playing_state") or 1) == 1:
                    ci["playing_state"] = 5
        served_ci = data.get("chara_info")
        if (isinstance(served_ci, dict) and impl.state(full_state).get("hold_turn")
                and data.get("unchecked_event_array")
                and int(served_ci.get("playing_state") or 1) == 1):
            served_ci["playing_state"] = 5
        if endpoint.rsplit("/", 1)[-1] == "start":
            impl.roll_track(full_state)
            impl.state(full_state)["deck_id"] = impl.deck_id_of(chara_info)
        data["breeders_data_set"] = impl.data_set(full_state, chara_info, home,
                                                  impl.reply_is_full(endpoint, data))
        for holder, key in ((data.get("single_mode_start_common"), "breeders_data_set_start"),
                            (data.get("single_mode_load_common"), "breeders_data_set_load")):
            if isinstance(holder, dict):
                holder.pop("breeders_data_set", None)
                data[key] = impl.data_set_start(full_state)
        return response

    def settle(self, full_state, chara_info, endpoint=""):
        if not isinstance(chara_info, dict):
            return False
        k = impl.casino_card(chara_info)
        if not k:
            return False
        st = impl.state(full_state)
        row = next((e for e in chara_info.get("evaluation_info_array") or ()
                    if isinstance(e, dict) and int(e.get("target_id") or 0) == k[0]), None)
        step = int((row or {}).get("story_step") or 0)
        if step != int(st.get("outings") or 0):
            st["outings"] = step
            return True
        return False

    def final_partners(self, full_state, chara_info, command_id, turn, partners) -> list:
        st = impl.state(full_state or {})
        if full_state and impl.dreams_active(st, int(turn or 0)):
            return impl.dreams_partners(chara_info)
        return partners

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        turn = impl.served_turn(full_state, chara_info)
        served = formula.BASE_TO_SUMMER.get(command_id, command_id) if camp else command_id
        caps = impl.dreams_caps(st, chara_info, turn)
        return formula.home_gains(chara_info, served, facility_level, partners,
                                  smt._support_card_levels(full_state),
                                  sub_pct=impl.sub_pct(st, chara_info, turn),
                                  main_cap=caps[0], sp_cap=caps[1])

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        return impl.preview_bonus(impl.state(full_state), impl.turn_of(chara_info), int(command_id or 0))

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        if not st["formed"]:
            return
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        if ctype == 1:
            impl.train_members(full_state, st, impl.preview_members(st, turn, cmd))
            return
        casino = (ctype == 3 and int(payload.get("command_group_id") or 0) == 390
                  and int(payload.get("select_id") or 0) == M.CASINO_CHARA)
        if casino:
            st["outing_due"] = {str(c): n for c, n in impl.outing_split(
                st, impl.rng_for(full_state, "outing", turn)).items()}
        elif ctype in (3, 7, 8):
            st["plus1_due"].append({"turn": turn, "kind": ctype})

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        from ...handlers import single_mode_team as smt
        rows = smt._route_goal_rows(tuple(chara_info.get("route_race_id_array") or ()))
        goal = next((r for r in rows if r[0] == turn), None)
        if goal is None:
            return
        need = int(goal[3] or 0)
        rank = int(result_rank or 99)
        if need and rank > need:
            return
        sort = impl.RI.goal_sort_id(chara_info, turn)
        if turn == impl.FINAL_TURN:
            chara_info["state"] = 3
            st["goal_due"] = {"bc": True, "turn": turn}
            return
        st["goal_due"] = {"sort": sort, "rank": rank, "turn": turn}
        st["plus1_due"].append({"turn": turn, "kind": "race", "race": True})

    def specialty_bonus(self, full_state) -> int:
        return M.team_rank_effect(impl.team_rank(impl.state(full_state)), M.TEAM_SPECIALTY)

    def hint_partners(self, full_state, chara_info, command_id, partners, hinting):
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        if impl.dreams_active(st, turn):
            return [p for p in st["dreams"].get("tips") or () if p in partners]
        rate = M.team_rank_effect(impl.team_rank(st), M.TEAM_HINT_RATE)
        if not rate:
            return hinting
        from ...handlers import single_mode_team as smt
        by_pos = impl.deck(chara_info)
        out = list(hinting)
        for pos in partners:
            if pos in out or pos not in by_pos or not formula.RF.is_normal_card(by_pos[pos]):
                continue
            chance = smt._skill_hint_chance(by_pos[pos]) * rate / 100
            if impl.rng_for(full_state, "hint", turn, command_id, pos).random() < chance:
                out.append(pos)
        return out

    def training_failure_override(self, full_state, chara_info, command_id, partners):
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        if not impl.dreams_active(st, turn):
            return None
        down = int(impl.effect_sums(st, chara_info).get(M.EFF_FAIL, 0))
        if not down:
            return None
        from ... import training_formula as tf
        from ...handlers import single_mode_team as smt
        base = formula.SUMMER_TO_BASE.get(command_id, command_id)
        level = 5 if command_id in formula.SUMMER_TO_BASE else next(
            (int(e.get("level") or 1) for e in chara_info.get("training_level_info_array") or ()
             if e.get("command_id") == base), 1)
        rate = tf.failure_rate(chara_info.get("vital", 100), command_id=command_id,
                               facility_level=level,
                               conditions=chara_info.get("chara_effect_id_array"),
                               facility_cards=smt._facility_failure_cards(chara_info, partners))
        return max(0, rate * (100 - min(100, down)) // 100)

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        return impl.review_pending(impl.state(full_state), int(turn or 0))

    def begin_hold(self, full_state, turn) -> None:
        impl.state(full_state)["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in producers.SCREEN_EVENTS

    def choice_reward_preview(self, full_state, chara_info, event_id):
        from ... import career_events as CE
        from ... import event_engine
        ev = CE.find(full_state, event_id)
        if ev is None or ev.source != "breeders" or not ev.choices or len(ev.choices) < 2:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        event_id = int(event_id or 0)
        turn = impl.turn_of(chara_info)
        if event_id == 1001 and turn <= 1:
            ev = producers.invite_event()
            if ev:
                CE.emit(full_state, ev)
        elif event_id in producers.CASINO_OUTINGS:
            st = impl.state(full_state)
            due = st.pop("outing_due", None)
            split = ({int(c): int(n) for c, n in due.items()} if isinstance(due, dict)
                     else impl.outing_split(st, impl.rng_for(full_state, "outing", turn)))
            for c, n in split.items():
                m = impl.member(st, c)
                if m:
                    impl.add_exp(full_state, st, m, n)
        return {}

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"breeders_reviews": [dict(r) for r in st["reviews"]],
                "breeders_team_rank": impl.team_rank(st)}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Breeders())
