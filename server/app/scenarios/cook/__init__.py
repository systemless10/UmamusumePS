
from __future__ import annotations

import copy

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Cook(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Cook"
    endpoint_prefix = "single_mode_cook"
    data_set_key = "cook_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    has_reporter_ending = False
    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }
    _FINALS_POST = {101002: 74, 101004: 76, 101006: 78}

    playing_states = frozenset({5, 23, 24})
    turn_end_hold_state = 5
    locked_npc_rows = impl.npc_rows()
    npc_targets = frozenset(t for t, _c in impl.npc_rows())

    LEVELUP_EVENTS = {101: 208033, 105: 208034, 102: 208035, 103: 208036, 106: 208037}

    def facility_levelup_event(self, command_id) -> tuple:
        return (self.LEVELUP_EVENTS.get(formula.base_of(command_id), 208033), 400000005)

    def appraisal_events(self):
        return {102: [(90, 208042, 5), (70, 208041, 4), (40, 208040, 3), (0, 208039, 2)],
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)]}

    def npc_supporters(self, full_state, chara_info) -> list:
        st = impl.state(full_state)
        return list(st["friends"]) if st["roster"] else []

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        started = impl.ensure_started(st, chara_info)
        before = copy.deepcopy(st["preview"])
        impl.refresh_preview(full_state, chara_info, home_commands or [],
                             smt._support_card_levels(full_state), impl.turn_of(chara_info))
        return None, bool(started or st["flags"] or st["preview"] != before
                          or st["cook_flags"]["garden_level_up"])

    def attach(self, response, full_state, chara_info, command_info_array=None, endpoint=""):
        data = (response or {}).get("data")
        if not isinstance(data, dict):
            return response
        load_ci = (data.get("single_mode_load_common") or {}).get("chara_info")
        start_ci = (data.get("single_mode_start_common") or {}).get("chara_info")
        ci = chara_info if isinstance(chara_info, dict) else (load_ci or start_ci)
        if not isinstance(ci, dict):
            return response
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        impl.ensure_started(st, ci)
        call = (endpoint or "").rsplit("/", 1)[-1]
        if impl.career_over(ci):
            for c in (chara_info, load_ci):
                if isinstance(c, dict) and int(c.get("playing_state") or 1) == 1:
                    c["playing_state"] = 5
        home = data.get("home_info")
        for holder in (data.get("single_mode_load_common"), data.get("single_mode_start_common")):
            if not isinstance(home, dict) and isinstance(holder, dict):
                home = holder.get("home_info")
        if not isinstance(home, dict):
            home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
        commands = list(home.get("command_info_array") or ())
        turn = self._served_turn(full_state, ci, data)
        if call == "check_event" and st["cook_flags"]["garden_level_up"]:
            if st["flags"].get("glu_seen"):
                st["cook_flags"]["garden_level_up"] = 0
                st["flags"].pop("glu_seen", None)
            else:
                st["flags"]["glu_seen"] = True
        full = self._reply_is_full(call, data, ci)
        data["cook_data_set"] = impl.data_set(full_state, ci, commands, turn, full,
                                              start=(call == "start"))
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("cook_data_set", None)
        if not full and isinstance(data.get("home_info"), dict):
            data["home_info"] = dict(data["home_info"])
            data["home_info"]["command_info_array"] = [
                dict(c, params_inc_dec_info_array=None) if c.get("command_type") == 1 else dict(c)
                for c in data["home_info"].get("command_info_array") or ()]
        for ev in data.get("unchecked_event_array") or ():
            post = self._FINALS_POST.get(int(ev.get("event_id") or 0))
            if post:
                (ev.get("event_contents_info") or {})["show_clear_sort_id"] = \
                    impl.RI.goal_sort_id(ci, post)
        return response

    @staticmethod
    def _reply_is_full(call: str, data: dict, chara_info: dict) -> bool:
        if impl.career_over(chara_info) and call != "load":
            return False
        if call in ("start", "load", "cooking", "facility_level_up", "tasting_live"):
            return True
        if call not in ("check_event", "exec_command"):
            return False
        return (not data.get("unchecked_event_array")
                and int(chara_info.get("playing_state") or 1) == 1)

    def _served_turn(self, full_state: dict, chara_info: dict, data: dict) -> int:
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        if int(st.get("hold_turn") or 0):
            return int(st["hold_turn"])
        turn = impl.turn_of(chara_info)
        events = data.get("unchecked_event_array") or []
        head = events[0] if events and isinstance(events[0], dict) else {}
        if (turn > 1 and int(head.get("play_timing") or 0) in self.turn_end_timings
                and int(full_state.get(smt._TURN_HOLD_RELEASED_KEY) or 0) != turn):
            return turn - 1
        return turn

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        served = formula.BASE_TO_SUMMER.get(command_id, command_id) if camp else command_id
        return formula.home_gains(chara_info, served, facility_level, partners,
                                  smt._support_card_levels(full_state))

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        row = impl.training_row(impl.state(full_state), command_id, impl.turn_of(chara_info))
        return [dict(p) for p in row.get("line") or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        if ctype == 1:
            care = impl.training_row(st, cmd, turn).get("care")
            if care:
                impl.apply_care(st, care, turn)
            impl.set_last_command(st, turn, 1, cmd, 2)
            return
        if ctype == 7 or cmd == 701:
            st["due"].append({"kind": "rest", "turn": turn, "spec": impl.care_spec(st, "rest", turn)})
            impl.set_last_command(st, turn, 7, 701, 2)
        elif ctype == 3:
            yayoi = int(payload.get("select_id") or 0) == impl.YAYOI
            st["due"].append({"kind": "yayoi" if yayoi else "outing", "turn": turn,
                              "spec": impl.care_spec(st, "yayoi" if yayoi else "rest", turn)})
            st["yayoi_due"] = yayoi
            group = int(payload.get("command_group_id") or 0) or cmd
            impl.set_last_command(st, turn, 3, group, 2)

    def on_training_failed(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        cmd = int(payload.get("command_id") or 0)
        st["due"].append({"kind": "fail", "turn": turn, "spec": impl.care_spec(st, "fail", turn, cmd)})
        impl.set_last_command(st, turn, 1, cmd, 1)

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        impl.set_last_command(st, turn, 4, 401, 0)
        if turn < 78:
            st["due"].append({"kind": "race", "turn": turn, "spec": impl.care_spec(st, "race", turn)})

    def specialty_rate_pct(self, full_state) -> int:
        return formula.power_effect(int(impl.state(full_state)["power"]), 206)

    def fan_bonus_pct(self, full_state, chara_info) -> int:
        return formula.power_effect(int(impl.state(full_state)["power"]), 208)

    def race_bonus_pct(self, full_state, chara_info) -> int:
        return impl.dish_race_pct(impl.state(full_state), impl.turn_of(chara_info))

    def partner_placements(self, full_state, chara_info, turn, occupied=None, standing=None) -> dict:
        return impl.extra_partners(impl.state(full_state), int(turn or 0))

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        turn = int(turn or 0)
        if impl.tasting_pending(impl.state(full_state), turn):
            return True
        return producers.closing_pending(full_state, chara_info, turn)

    def begin_hold(self, full_state, turn) -> None:
        st = impl.state(full_state)
        if impl.tasting_pending(st, int(turn or 0)):
            st["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in producers.SCREEN_EVENTS

    def choice_reward_preview(self, full_state, chara_info, event_id):
        from ... import career_events as CE
        from ... import event_engine
        ev = CE.find(full_state, event_id)
        if ev is None or ev.source != "cook" or not ev.choices:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        event_id = int(event_id or 0)
        turn = impl.turn_of(chara_info)
        st = impl.state(full_state)
        if event_id == 1001 and turn <= 1:
            for ev in producers.opening_events():
                CE.emit(full_state, ev)
        elif event_id in producers.STORY_ENDING_TRIGGERS and turn >= 78:
            ev = producers.ending_event(full_state)
            if ev:
                CE.emit(full_state, ev)
        elif st.get("yayoi_due") and event_id != producers.CARE_YAYOI[0]:
            st["yayoi_due"] = False
            impl.flag(st, "event_gain", impl.add_materials(st, impl.YAYOI_MATERIALS))
        return {}

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"cook_results": [dict(r) for r in st["results"]],
                "cook_friends_power": int(st["power"])}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Cook())
