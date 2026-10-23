
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Pioneer(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Pioneer"
    endpoint_prefix = "single_mode_pioneer"
    data_set_key = "pioneer_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }
    _FINALS_POST = {101002: 74, 101004: 76, 101006: 78}
    camp_lists_rest = True
    camp_rest_group = M.ISLAND_REST
    npc_targets = frozenset({113})
    sp_appraisers = frozenset({102, 113})

    playing_states = frozenset({5, 34, 35, 38, 39})
    turn_end_hold_state = 5

    def facility_levelup_event(self, command_id) -> tuple:
        base = self.base_command(command_id)
        return (producers.LEVELUP_EVENTS.get(base, 211024), 400000005)

    def appraisal_events(self):
        return {102: [(90, 211065, 5), (70, 211064, 4), (40, 211063, 3), (0, 211062, 2)],
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
                113: [(80, 211083, 5), (60, 211082, 4), (40, 211081, 3), (0, 211080, 2)]}

    def levels_facilities_by_training(self) -> bool:
        return False

    def npc_supporters(self, full_state, chara_info) -> list:
        return impl.islanders(full_state, chara_info)

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def serves_camp_rest(self, turn) -> bool:
        return impl.island_turn(int(turn or 0))

    def base_command(self, command_id) -> int:
        cid = int(command_id or 0)
        return M.ISLAND_TO_TRAINING.get(cid, super().base_command(cid))

    def scenario_command(self, full_state, chara_info, payload):
        if int(payload.get("command_id") or 0) != M.ISLAND_TRAINING:
            return None
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        pv = impl.stored(st, turn).get("island") or {}
        st["tickets"] = max(0, int(st["tickets"]) - 1)
        st["gain"] = 0
        capped = []
        for pos, sid in sorted(impl.deck(chara_info).items()):
            gain = 10 if formula.RF.is_normal_card(sid) else 7
            for row in chara_info.get("evaluation_info_array") or ():
                if isinstance(row, dict) and row.get("target_id") == pos:
                    if int(row.get("evaluation") or 0) >= 100:
                        capped.append(formula.RF.card_meta(sid)[3])
                    row["evaluation"] = min(100, int(row.get("evaluation") or 0) + gain)
        st["flags"]["not_up_bond"] = capped
        merged: dict = {}
        for p in list(pv.get("params") or ()) + list(pv.get("bonus") or ()):
            merged[p["target_type"]] = merged.get(p["target_type"], 0) + int(p["value"])
        return [{"target_type": tt, "value": v} for tt, v in sorted(merged.items())]

    def partner_outing(self, full_state, chara_info, partner, turn) -> bool:
        turn = int(turn or impl.turn_of(chara_info))
        st = impl.state(full_state)
        if int(partner or 0) != M.TUCKER_CHARA or not impl.tucker_card(chara_info) \
                or not impl.points_active(st, turn):
            return False
        st["tucker_outings"] = int(st.get("tucker_outings") or 0) + 1
        st["due"].append({"kind": "tucker", "step": st["tucker_outings"], "turn": turn})
        return True

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        changed = impl.refresh_preview(full_state, chara_info, home_commands or [],
                                       smt._support_card_levels(full_state))
        return None, bool(changed or impl.state(full_state).get("gain"))

    def home_commands(self, full_state, chara_info, turn, commands) -> list:
        turn = int(turn or 0)
        st = impl.state(full_state)
        out = [dict(c) for c in commands]
        if impl.island_turn(turn):
            race_turn = turn in impl.FINALS_RACE_TURNS
            facs = impl.facilities(st)
            for c in out:
                base = self.base_command(c.get("command_id"))
                if c.get("command_type") == 1 and base in M.ISLAND_VERSIONS:
                    c["command_id"] = M.ISLAND_VERSIONS[base]
                    c["level"] = 5
                    gains = {}
                    for p in c.get("params_inc_dec_info_array") or ():
                        tt = int(p.get("target_type") or 0)
                        key = ("skill_point" if tt == formula.TARGET_SP else "energy_cost"
                               if tt == formula.TARGET_ENERGY else formula.STATS[tt - 1])
                        gains[key] = int(p.get("value") or 0)
                    c["params_inc_dec_info_array"] = formula.island_params(
                        chara_info, c["command_id"], gains, facs)
            trainings = [c for c in out if c.get("command_type") == 1]

            def entry(ctype, cid, enable):
                return {"command_type": ctype, "command_id": cid, "is_enable": enable,
                        "training_partner_array": [], "tips_event_partner_array": [],
                        "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0}
            if race_turn:
                rest = [entry(3, 301, 0), entry(3, 390, 0), entry(4, 401, 1),
                        entry(7, 701, 0), entry(8, 801, 0)]
            else:
                race_open = next((c.get("is_enable", 1) for c in out if c.get("command_id") == 401), 1)
                rest = [entry(3, M.ISLAND_REST, 1),
                        entry(4, 401, 0 if turn in impl.FINALS_TURNS else race_open),
                        entry(7, 701, 0), entry(8, 801, 0)]
            return trainings + rest
        trainings = [c for c in out if c.get("command_type") == 1]
        others = [c for c in out if c.get("command_type") != 1]
        trainings.append(self._island_training_entry(full_state, chara_info, turn, trainings))
        return trainings + others

    def _island_training_entry(self, full_state, chara_info, turn, trainings) -> dict:
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        pv = impl.island_preview(full_state, chara_info, trainings, turn,
                                 smt._support_card_levels(full_state))
        pmap = {int(f): ids for f, ids in pv["map"].items()}
        forced = impl.goal_race_turn(chara_info, turn)
        enable = int(st["tickets"]) > 0 and impl.FIRST_PLAN_TURN <= turn <= impl.LIVE_TURN
        partners = sorted({p for ids in pmap.values() for p in ids})
        level = max((int(c.get("level") or 1) for c in trainings), default=1)
        return {"command_type": 1, "command_id": M.ISLAND_TRAINING,
                "is_enable": 1 if enable else 0,
                "training_partner_array": [] if forced and not enable else partners,
                "tips_event_partner_array": [],
                "params_inc_dec_info_array": list(pv.get("params") or []),
                "failure_rate": 0, "level": level}

    def attach(self, response, full_state, chara_info, command_info_array=None, endpoint=""):
        data = (response or {}).get("data")
        if not isinstance(data, dict) or not isinstance(chara_info, dict):
            return response
        from ...handlers import single_mode_team as smt
        call = endpoint.rsplit("/", 1)[-1]
        home = data.get("home_info")
        if not isinstance(home, dict):
            home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
        if impl.RI.career_over(chara_info):
            for ci in (chara_info, (data.get("single_mode_load_common") or {}).get("chara_info")):
                if isinstance(ci, dict) and int(ci.get("playing_state") or 1) == 1:
                    ci["playing_state"] = 5
        for ci in (data.get("chara_info"), (data.get("single_mode_load_common") or {}).get("chara_info"),
                   (data.get("single_mode_start_common") or {}).get("chara_info")):
            if isinstance(ci, dict):
                self._carry_npc_rows(ci)
        source_ci = data.get("chara_info") if isinstance(data.get("chara_info"), dict) else chara_info
        data["pioneer_data_set"] = impl.data_set(full_state, source_ci, home, start=(call == "start"))
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("pioneer_data_set", None)
        if call in ("start", "load"):
            data["auto_planning_info"] = impl.auto_planning_info(impl.state(full_state))
        impl.state(full_state)["gain"] = 0
        for ev in data.get("unchecked_event_array") or ():
            post = self._FINALS_POST.get(int(ev.get("event_id") or 0))
            if post:
                (ev.get("event_contents_info") or {})["show_clear_sort_id"] = \
                    impl.RI.goal_sort_id(chara_info, post)
        return response

    @staticmethod
    def _carry_npc_rows(chara_info: dict) -> None:
        arr = chara_info.setdefault("evaluation_info_array", [])
        present = {e.get("target_id") for e in arr if isinstance(e, dict)}
        for target_id, _chara in impl.npc_rows(chara_info):
            if target_id not in present:
                arr.append({"target_id": target_id, "training_partner_id": target_id,
                            "evaluation": 0, "is_outing": 0, "story_step": 0,
                            "is_appear": 0, "group_outing_info_array": []})

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        levels = smt._support_card_levels(full_state)
        turn = impl.served_turn(full_state, chara_info)
        base = self.base_command(command_id)
        if impl.island_turn(turn):
            return formula.island_gains(chara_info, M.ISLAND_VERSIONS[base], partners,
                                        impl.facilities(impl.state(full_state)), levels)
        return formula.home_gains(chara_info, base, facility_level, partners, levels)

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        cid = int(command_id or 0)
        if cid == M.ISLAND_TRAINING:
            return []
        st = impl.state(full_state)
        pv = impl.stored(st, impl.turn_of(chara_info))
        return [dict(p) for p in pv["bonus"].get(str(cid)) or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        if int(payload.get("command_type") or 0) != 1:
            return
        cmd = int(payload.get("command_id") or 0)
        pv = impl.stored(st, turn)
        if cmd == M.ISLAND_TRAINING:
            points = int((pv.get("island") or {}).get("points") or 0)
        else:
            points = int(pv["points"].get(str(cmd), 0))
            impl.count_training(st, cmd)
            for base in impl.sync_levels(full_state, chara_info):
                if not impl.island_turn(turn):
                    st["due"].append({"kind": "levelup", "command_id": base,
                                      "level": impl.level_for(st, base)})
        impl.add_points(full_state, chara_info, points, "train", turn)

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        if race_ctx.get("finals_round") or not impl.points_active(st, turn):
            return
        goal = bool(race_ctx.get("is_goal")) or impl.goal_race_turn(chara_info, turn)
        st["due"].append({"kind": "race", "turn": turn,
                          "points": impl.race_points_for(full_state, chara_info, turn, goal=goal)})

    def specialty_bonus_by_type(self, full_state, chara_info, type_index) -> int:
        cmd = {0: 101, 1: 105, 2: 102, 3: 103, 4: 106}.get(int(type_index))
        if cmd is None:
            return 0
        turn = impl.served_turn(full_state, chara_info)
        periods = impl.period(turn) if impl.island_turn(turn) else M.PERIOD_ISLAND
        return sum(v2 for _t, _v1, v2, _v3, _v4 in formula.facility_rows(
            impl.facilities(impl.state(full_state)), M.EFFECT_SPECIALTY, period=periods, v3=cmd))

    def hint_partners(self, full_state, chara_info, command_id, partners, hinting):
        st = impl.state(full_state)
        turn = impl.served_turn(full_state, chara_info)
        facs = impl.facilities(st)
        by_pos = impl.deck(chara_info)
        normal = [p for p in partners if p in by_pos and formula.RF.is_normal_card(by_pos[p])]
        base = self.base_command(command_id)
        island_cmd = M.ISLAND_VERSIONS.get(base) if impl.island_turn(turn) else None
        if island_cmd and formula.facility_rows(facs, M.EFFECT_ALL_HINTS,
                                                period=impl.period(turn), v3=island_cmd):
            return sorted(set(hinting) | set(normal))
        rate = impl.hint_pct(st) + sum(v2 for _t, _v1, v2, _v3, _v4 in formula.facility_rows(
            facs, M.EFFECT_HINT_RATE, period=M.PERIOD_ALWAYS))
        if island_cmd:
            rate += sum(v2 for _t, _v1, v2, _v3, _v4 in formula.facility_rows(
                facs, M.EFFECT_HINT_RATE, period=impl.period(turn), v3=island_cmd))
        if not rate:
            return hinting
        from ...handlers import single_mode_team as smt
        out = list(hinting)
        for pos in normal:
            if pos in out:
                continue
            p = smt._skill_hint_chance(by_pos[pos])
            chance = 1.0 if p >= 1 else p * rate / 100 / (1 - p)
            if impl.rng_for(full_state, "hint", turn, pos).random() < chance:
                out.append(pos)
        return out

    def training_failure_override(self, full_state, chara_info, command_id, partners):
        cid = int(command_id or 0)
        if cid == M.ISLAND_TRAINING:
            return 0
        turn = impl.served_turn(full_state, chara_info)
        base = self.base_command(cid)
        if impl.island_turn(turn) and base in M.ISLAND_VERSIONS:
            from ... import training_formula as tf
            from ...handlers import single_mode_team as smt
            return tf.failure_rate(chara_info.get("vital", 100), command_id=M.ISLAND_VERSIONS[base],
                                   facility_level=5,
                                   conditions=chara_info.get("chara_effect_id_array"),
                                   facility_cards=smt._facility_failure_cards(chara_info, partners))
        return None

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        st = impl.state(full_state)
        turn = int(turn or 0)
        if impl.check_point_due(st, turn):
            return True
        return turn == impl.LIVE_TURN and not st["complete"]

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
        if ev is None or ev.source != "pioneer" or not ev.choices or len(ev.choices) < 2:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        event_id = int(event_id or 0)
        turn = impl.turn_of(chara_info)
        if event_id == 1001 and turn <= 1:
            ev = producers.opening_event()
            if ev:
                CE.emit(full_state, ev)
        elif event_id == producers.STORY_ENDING_TRIGGER and turn >= 78:
            ev = producers.ending_event(full_state)
            if ev:
                CE.emit(full_state, ev)
        self._tucker_dates(full_state, chara_info)
        return {}

    def _tucker_dates(self, full_state, chara_info) -> None:
        from ... import career_events as CE
        t = impl.tucker_card(chara_info)
        if not t:
            return
        st = impl.state(full_state)
        pos = t[0]
        row = next((e for e in chara_info.get("evaluation_info_array") or ()
                    if isinstance(e, dict) and int(e.get("target_id") or 0) == pos), None)
        step = int((row or {}).get("story_step") or 0)
        seen = int(st["link_steps"].get(str(pos)) or 0)
        if step <= seen:
            return
        st["link_steps"][str(pos)] = step
        turn = impl.turn_of(chara_info)
        if impl.points_active(st, turn):
            ev = producers.tucker_reward_event(chara_info, step, turn)
            if ev:
                CE.emit(full_state, ev)

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"pioneer_results": [dict(r) for r in st["results"]],
                "pioneer_total_points": impl.total_points(st),
                "scenario_data_array": [{"type": 11001, "value": impl.total_points(st)}]}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Pioneer())
