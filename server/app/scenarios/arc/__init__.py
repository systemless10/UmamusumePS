
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Arc(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Arc"
    endpoint_prefix = "single_mode_arc"
    data_set_key = "arc_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    has_reporter_ending = False
    has_ura_finals = False
    has_ending_chain = False
    plays_calendar_stories = False
    has_chapter_stories = False
    camp_lists_rest = True
    camp_rest_group = M.EXPED_REST
    camp_rest_energy = 50
    sp_appraisers = frozenset({102, M.MEI_TARGET})
    passive_goal_types = frozenset({11})

    playing_states = frozenset({5, 16, 17, 18, 19})
    turn_end_hold_state = 5
    locked_npc_rows = M.npc_partners()
    npc_targets = frozenset(t for t, _c in M.npc_partners())

    LEVELUP_EVENTS = {101: 206102, 105: 206105, 102: 206103, 103: 206104, 106: 206106}
    OWN_RESULTS = frozenset({M.DERBY, M.TAKARAZUKA, *M.LONGCHAMP})

    def facility_levelup_event(self, command_id) -> tuple:
        return (self.LEVELUP_EVENTS.get(formula.base_of(command_id), 206102), 400000005)

    def appraisal_events(self):
        return {102: [(90, 206136, 5), (70, 206135, 4), (40, 206134, 3), (0, 206133, 2)],
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
                M.MEI_TARGET: [(90, 206154, 5), (70, 206153, 4), (40, 206152, 3), (0, 206124, 2)]}

    def career_route(self, chara_info: dict):
        from ... import master_data
        from ..breeders import impl as BI
        chara = int((chara_info or {}).get("card_id") or 0) // 100
        rows = [int(r["id"]) for r in master_data.query(
            "SELECT id FROM single_mode_route_race WHERE race_set_id=? AND condition_type != 8 ORDER BY sort_id",
            (M.ROUTE_ID,))]
        debut = BI.debut_route_row(chara)
        return M.ROUTE_ID, ([debut] if debut else []) + rows

    def goal_condition(self, chara_info: dict, goal):
        if int(goal[2]) != 11:
            return None
        pt = int(((chara_info or {}).get(impl.MIRROR_KEY) or {}).get("pt") or 0)
        return pt >= int(goal[4] or 0)

    def npc_supporters(self, full_state, chara_info) -> list:
        return impl.npc_members(impl.state(full_state), chara_info)

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def serves_camp_rest(self, turn) -> bool:
        return impl.is_expedition(int(turn or 0))

    def base_command(self, command_id) -> int:
        cid = int(command_id or 0)
        return M.EXPED.get(cid, super().base_command(cid))

    def scenario_command(self, full_state, chara_info, payload):
        if int(payload.get("command_type") or 0) != endpoints.SS_COMMAND_TYPE:
            return None
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        st = impl.state(full_state)
        try:
            result = impl.run_match(full_state, chara_info, turn)
        except impl.Refused:
            return []
        impl.flag(st, "match_result", result)
        merged: dict = {}
        for p in list(st["lineup"].get("preview") or ()) + list(result["bonus"]):
            merged[int(p["target_type"])] = merged.get(int(p["target_type"]), 0) + int(p["value"])
        return [{"target_type": tt, "value": v} for tt, v in sorted(merged.items())]

    def home_commands(self, full_state, chara_info, turn, commands) -> list:
        turn = int(turn or 0)
        if not impl.is_expedition(turn):
            return commands
        out = []
        race_only = any(c.get("command_type") == 4 and c.get("is_enable") for c in commands) and not any(
            c.get("command_type") == 1 and c.get("is_enable") for c in commands)
        for c in commands:
            if c.get("command_type") != 1:
                continue
            base = self.base_command(c.get("command_id"))
            if base in M.EXPED_OF:
                c = dict(c, command_id=M.EXPED_OF[base], level=5)
                out.append(c)

        def entry(ctype, cid, enable):
            return {"command_type": ctype, "command_id": cid, "is_enable": enable,
                    "training_partner_array": [], "tips_event_partner_array": [],
                    "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0}
        race_open = next((c.get("is_enable", 1) for c in commands if c.get("command_id") == 401), 1)
        out += [entry(3, M.EXPED_REST, 0 if race_only else 1), entry(4, 401, race_open),
                entry(7, 701, 0), entry(8, 801, 0)]
        return out

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        started = not st["started"]
        if started:
            st["started"] = True
            st["trainee"] = int(chara_info.get("card_id") or 0) // 100
            impl.mirror(full_state, chara_info)
        changed = impl.refresh_preview(full_state, chara_info, home_commands or [],
                                       smt._support_card_levels(full_state), impl.turn_of(chara_info))
        live = impl.live_chara(full_state) or chara_info
        pending = bool(st["roster"]) and impl.turn_of(live) > int(st["upkeep_turn"] or 0)
        return None, bool(started or changed or pending or st["flags"])

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
        if impl.upkeep(full_state, impl.live_chara(full_state) or ci, target=turn):
            impl.refresh_preview(full_state, ci, commands, smt._support_card_levels(full_state),
                                 impl.turn_of(ci))
        full = self._reply_is_full(call, data, ci)
        data["arc_data_set"] = impl.data_set(full_state, ci, commands, turn, full)
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("arc_data_set", None)
        for c in (data.get("chara_info"), load_ci, start_ci):
            if isinstance(c, dict):
                impl.strip_mirror(c)
                self._drop_carried_partners(c)
        lc = data.get("single_mode_load_common")
        if call == "load" and isinstance(lc, dict) and isinstance(load_ci, dict) \
                and int(load_ci.get("playing_state") or 1) in (17, 18, 19):
            sp = st.get("sprace") or {}
            if sp.get("race_start_info"):
                import copy as _copy
                lc["race_start_info"] = _copy.deepcopy(sp["race_start_info"])
                lc["race_scenario"] = sp.get("race_scenario")
            endpoints.patch_members(lc.get("race_start_info"))
        endpoints.patch_members(data.get("race_start_info"))
        for arr in (data.get("race_horse_data_array"),):
            endpoints.patch_members({"race_horse_data": arr or []})
        return response

    def _served_turn(self, full_state: dict, chara_info: dict, data: dict) -> int:
        from ...handlers import single_mode_team as smt
        held = int(impl.state(full_state).get("hold_turn") or 0)
        if held:
            return held
        turn = impl.turn_of(chara_info)
        events = data.get("unchecked_event_array") or []
        head = events[0] if events and isinstance(events[0], dict) else {}
        if (turn > 1 and int(head.get("play_timing") or 0) in self.turn_end_timings
                and int(full_state.get(smt._TURN_HOLD_RELEASED_KEY) or 0) != turn):
            return turn - 1
        return turn

    @staticmethod
    def _reply_is_full(call: str, data: dict, chara_info: dict) -> bool:
        if call in ("start", "load"):
            return True
        if call not in ("check_event", "exec_command", "race_out", "potential_level_up",
                        "arc_race_out", "selection_exec"):
            return False
        return (not data.get("unchecked_event_array")
                and int(chara_info.get("playing_state") or 1) == 1
                and not impl.career_over(chara_info))

    @staticmethod
    def _drop_carried_partners(chara_info: dict) -> None:
        deck = impl.deck_all_charas(chara_info)
        carried = {t for t, c in M.npc_partners() if c in deck}
        if carried:
            chara_info["evaluation_info_array"] = [
                e for e in chara_info.get("evaluation_info_array") or ()
                if not (isinstance(e, dict) and e.get("target_id") in carried)]

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        base = self.base_command(command_id)
        cmd = M.EXPED_OF[base] if impl.is_expedition(impl.turn_of(chara_info)) and base in M.EXPED_OF \
            else base
        return formula.home_gains(chara_info, cmd, facility_level, partners,
                                  smt._support_card_levels(full_state))

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        row = impl.preview_row(st, command_id, turn)
        return [dict(p) for p in row.get("line") or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        if int(payload.get("command_type") or 0) == endpoints.SS_COMMAND_TYPE:
            from ... import career_events as CE
            due = impl.flag(st, "ss_end") or {}
            ev = producers.ss_end_event(due)
            if ev and CE.emit(full_state, ev):
                impl.flag(st, "lead", True)
            return
        if int(payload.get("command_type") or 0) != 1:
            return
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        cmd = int(payload.get("command_id") or 0)
        row = impl.preview_row(st, cmd, turn)
        partners = row.get("partners") or []
        impl.on_training(full_state, chara_info, cmd, partners, turn, failed=False)
        st["gexp"] = int(st["gexp"]) + int(row.get("gexp") or 0)

    def award_claims_slot(self, full_state) -> bool:
        return bool(impl.flag(impl.state(full_state), "lead"))

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        program = int(race_ctx.get("program_id") or 0)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        rank = int(result_rank or 99)
        places = {}
        for chara, place in (race_ctx.get("rival_finish_orders") or {}).items():
            if impl.member(st, int(chara)):
                places[int(chara)] = int(place)
        impl.pay_race_points(full_state, program, rank, places)
        if program == M.ARC_CLASSIC and rank == 1:
            st["classic_won"] = True
        need = self._goal_need(chara_info, program, turn)
        ok = need is None or not need or rank <= need
        mirror = chara_info.setdefault(impl.MIRROR_KEY, {})
        mirror["own_result"] = bool(program in self.OWN_RESULTS and ok)
        if program in self.OWN_RESULTS and ok:
            st["due"].append({"kind": "race_result", "program_id": program, "rank": rank, "turn": turn})
        if places and program not in M.LONGCHAMP:
            st["due"].append({"kind": "member_win", "turn": turn, "charas": sorted(places)})
        if program == M.ARC_SENIOR and ok:
            chara_info["state"] = 3
        impl.mirror(full_state, chara_info)
        chara_info[impl.MIRROR_KEY]["own_result"] = mirror["own_result"]

    @staticmethod
    def _goal_need(chara_info: dict, program: int, turn: int):
        from ...handlers import single_mode_team as smt
        for t, _cid, _kind, cv1 in smt._route_goal_rows(tuple(chara_info.get("route_race_id_array") or ())):
            if t == turn:
                return int(cv1 or 0)
        return None

    def owns_goal_race_result(self, chara_info: dict, program_id) -> bool:
        return bool(int(program_id or 0) in self.OWN_RESULTS
                    and ((chara_info or {}).get(impl.MIRROR_KEY) or {}).get("own_result"))

    def goal_race_entry_events(self, full_state, chara_info, program_id, turn):
        return producers.goal_race_entry_events(full_state, chara_info, program_id)

    def race_field_for(self, viewer_id, chara_info, program_id, count):
        from ...handlers import single_mode_team as smt
        program_id = int(program_id or 0)
        full_state = smt.state_store.get_state(viewer_id) or {}
        st = impl.state(full_state)
        if not st["roster"]:
            return None
        if program_id in M.EXHIBITION_FIELD or program_id in M.LONGCHAMP:
            return impl.overseas_field(full_state, chara_info, program_id, count)
        turn = impl.served_turn(full_state, chara_info)
        charas = impl.scheduled_members(st, program_id, turn)
        if not charas:
            return None
        out = [impl.member_runner(full_state, impl.member(st, c), program_id, turn) for c in charas]
        out = out[:count]
        if len(out) < count:
            out += smt._mob_opponents(chara_info, count - len(out), exclude_charas=set(charas))
        return out

    def race_player(self, viewer_id, chara_info, player, program_id) -> None:
        impl.apply_player_race_stats(chara_info, player, int(program_id or 0))

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        turn = int(turn or 0)
        st = impl.state(full_state)
        return turn in M.EXHIBITIONS and turn not in [int(t) for t in st["done_exhibitions"]] \
            and int((chara_info or {}).get("state") or 0) != 2

    def begin_hold(self, full_state, turn) -> None:
        impl.state(full_state)["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in producers.SCREEN_EVENTS

    def choice_reward_preview(self, full_state, chara_info, event_id):
        return producers.choice_reward_preview(full_state, chara_info, event_id)

    def pal_training_roll(self, full_state, chara_info, card_id, command_id):
        from ...handlers import single_mode_team as smt
        return impl.mei_roll(full_state, chara_info, card_id, command_id, smt._PAL_RANDOM_CHANCE)

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ...handlers import single_mode_team as smt
        ctx = smt._career_ctx_for(full_state, event_id)
        if ctx.get("source") == "support" and ctx.get("event") and not ctx.get("arc_paid") \
                and ctx.get("story_id") in (M.MEI_JAPAN_STORY, M.MEI_EXPED_STORY):
            ctx["arc_paid"] = 1
            impl.mei_resolved(full_state, int(ctx["story_id"]))
        return {}

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"arc_races": [dict(r) for r in st["races"]],
                "arc_potentials": dict(st["pot"]), "arc_ss_wins": int(st["ss_wins"]),
                "arc_classic_won": bool(st["classic_won"]),
                "arc_rate": impl.approval_rate(st), "arc_level_sum": impl.level_sum(st)}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Arc())
