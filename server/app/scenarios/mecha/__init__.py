
from __future__ import annotations

import copy

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Mecha(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Mecha"
    endpoint_prefix = "single_mode_mecha"
    data_set_key = "mecha_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    has_reporter_ending = False
    camp_lists_rest = True
    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }
    _FINALS_POST = {101002: 74, 101004: 76, 101006: 78}

    playing_states = frozenset({5, 25, 26, 27})
    turn_end_hold_state = 5
    locked_npc_rows = M.partner_rows()
    npc_targets = frozenset(t for t, _c in M.partner_rows())

    LEVELUP_EVENTS = {101: 209028, 105: 209029, 102: 209030, 103: 209031, 106: 209032}

    def facility_levelup_event(self, command_id) -> tuple:
        return (self.LEVELUP_EVENTS.get(formula.base_of(command_id), 209028), 400000005)

    def appraisal_events(self):
        return {102: [(90, 209061, 5), (70, 209060, 4), (40, 209059, 3), (0, 209058, 2)],
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)]}

    def npc_supporters(self, full_state, chara_info) -> list:
        st = impl.state(full_state)
        return list(st["members"]) if st["roster"] else []

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        started = impl.ensure_started(st, chara_info)
        before = copy.deepcopy(st["preview"])
        rolled = impl.refresh_preview(full_state, chara_info, home_commands or [],
                                      smt._support_card_levels(full_state), impl.turn_of(chara_info))
        return None, bool(started or rolled or st["flags"] or st["preview"] != before)

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
        full = self._reply_is_full(call, data, ci)
        data["mecha_data_set"] = impl.data_set(full_state, ci, commands, turn, full)
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("mecha_data_set", None)
        if not full and isinstance(data.get("home_info"), dict):
            data["home_info"] = dict(data["home_info"])
            data["home_info"]["command_info_array"] = [
                dict(c, params_inc_dec_info_array=None) if c.get("command_type") == 1 else dict(c)
                for c in data["home_info"].get("command_info_array") or ()]
        self._to_wire(data)
        for ev in data.get("unchecked_event_array") or ():
            post = self._FINALS_POST.get(int(ev.get("event_id") or 0))
            if post:
                (ev.get("event_contents_info") or {})["show_clear_sort_id"] = \
                    impl.RI.goal_sort_id(ci, post)
        return response

    @staticmethod
    def _reply_is_full(call: str, data: dict, chara_info: dict) -> bool:
        if call in ("start", "load", "overdrive"):
            return True
        if call not in ("check_event", "exec_command", "tuning", "race_out"):
            return False
        return (not data.get("unchecked_event_array")
                and int(chara_info.get("playing_state") or 1) == 1
                and not impl.career_over(chara_info))

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

    def _to_wire(self, data: dict) -> None:
        def rename(cid):
            return formula.WIRE_ID.get(int(cid), int(cid)) if isinstance(cid, int) else cid

        def fix_home(h):
            if not isinstance(h, dict):
                return h
            h = dict(h)
            h["command_info_array"] = [dict(c, command_id=rename(c.get("command_id")))
                                       for c in h.get("command_info_array") or ()]
            for key in ("disable_command_id_array",):
                if isinstance(h.get(key), list):
                    h[key] = [rename(x) for x in h[key]]
            return h

        def fix_chara(c):
            if not isinstance(c, dict) or not isinstance(c.get("training_level_info_array"), list):
                return c
            c = dict(c)
            c["training_level_info_array"] = sorted(
                (dict(e, command_id=rename(e.get("command_id"))) for e in c["training_level_info_array"]),
                key=lambda e: int(e.get("command_id") or 0))
            self._drop_carried_partners(c)
            c["evaluation_info_array"] = sorted(
                c.get("evaluation_info_array") or (),
                key=lambda e: int(e.get("target_id") or 0) if isinstance(e, dict) else 0)
            return c

        for holder in (data, data.get("single_mode_load_common"), data.get("single_mode_start_common")):
            if not isinstance(holder, dict):
                continue
            if "home_info" in holder:
                holder["home_info"] = fix_home(holder["home_info"])
            if "chara_info" in holder:
                holder["chara_info"] = fix_chara(holder["chara_info"])
        result = data.get("command_result")
        if isinstance(result, dict) and result.get("command_id") in formula.WIRE_ID:
            data["command_result"] = dict(result, command_id=rename(result["command_id"]))
        gpi = data.get("gain_parameter_info")
        if isinstance(gpi, dict) and gpi.get("effected_command_id_array"):
            data["gain_parameter_info"] = dict(
                gpi, effected_command_id_array=[rename(x) for x in gpi["effected_command_id_array"]])

    @staticmethod
    def _drop_carried_partners(chara_info: dict) -> None:
        deck_charas = {formula.RF.card_meta(sid)[3] for sid in formula.RF.deck(chara_info).values()}
        carried = {t for t, c in M.partner_rows() if c in deck_charas}
        if carried:
            chara_info["evaluation_info_array"] = [
                e for e in chara_info.get("evaluation_info_array") or ()
                if not (isinstance(e, dict) and e.get("target_id") in carried)]

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        served = formula.BASE_TO_SUMMER.get(command_id, command_id) if camp else command_id
        return formula.home_gains(chara_info, served, facility_level, partners,
                                  smt._support_card_levels(full_state))

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        row = impl.preview_row(impl.state(full_state), command_id, impl.turn_of(chara_info))
        return [dict(p) for p in row.get("line") or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        if ctype == 1:
            row = impl.preview_row(st, cmd, turn)
            capped = impl.add_research(st, [(int(s), int(v)) for s, v in row.get("points") or ()])
            impl.flag(st, "not_up", capped)
            if row.get("gear"):
                impl.add_energy(st, 1)
            if impl.overdrive_on(st, turn) and impl.has_effect(st, M.EFF_HINTS_TAKE_EFFECT):
                from ... import career_events as CE
                ev = producers.head_hint_event(full_state, chara_info, {
                    "turn": turn, "command_id": cmd, "partners": list(row.get("partners") or ())})
                if ev and CE.emit(full_state, ev):
                    impl.flag(st, "lead", True)
            return
        if ctype == 7 or cmd == 701:
            kind = "rest"
        elif ctype == 8 or cmd == 801:
            kind = "infirmary"
        elif ctype == 3:
            kind = "outing"
        else:
            return
        st["due"].append({"kind": kind, "turn": turn})

    def award_claims_slot(self, full_state) -> bool:
        return bool(impl.flag(impl.state(full_state), "lead"))

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        if race_ctx.get("finals_round"):
            return
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        st["due"].append({"kind": "race", "turn": turn, "goal": bool(race_ctx.get("is_goal"))})

    def training_bond_bonus(self, full_state, chara_info) -> int:
        st = impl.state(full_state)
        if not impl.overdrive_on(st, impl.turn_of(chara_info)):
            return 0
        return impl.effect_sum(st, M.EFF_BOND)

    def specialty_rate_pct(self, full_state) -> int:
        st = impl.state(full_state)
        return sum(v2 for t, _v1, v2, _v3, _v4 in M.chip_effect(M.CHIP_SPECIALTY,
                                                                int(st["chips"].get(str(M.CHIP_SPECIALTY), 0)))
                   if t == M.EFF_SPECIALTY)

    def hint_partners(self, full_state, chara_info, command_id, partners, hinting):
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        by_pos = formula.RF.deck(chara_info)
        normal = [p for p in partners if p in by_pos and formula.RF.is_normal_card(by_pos[p])]
        if impl.overdrive_on(st, turn) and impl.has_effect(st, M.EFF_ALL_HINTS):
            return sorted(set(hinting) | set(normal))
        rate = sum(v2 for t, _v1, v2, _v3, _v4 in M.chip_effect(
            M.CHIP_HINT_RATE, int(st["chips"].get(str(M.CHIP_HINT_RATE), 0))) if t == M.EFF_HINT_RATE)
        if not rate:
            return hinting
        from ...handlers import single_mode_team as smt
        out = list(hinting)
        for pos in normal:
            if pos in out:
                continue
            chance = smt._skill_hint_chance(by_pos[pos]) * rate / 100
            if impl.rng_for(full_state, "hint", turn, command_id, pos).random() < chance:
                out.append(pos)
        return out

    def hint_reveals(self, full_state, chara_info, command_id, reveals):
        st = impl.state(full_state)
        if impl.overdrive_on(st, impl.turn_of(chara_info)) and impl.has_effect(st, M.EFF_HINTS_TAKE_EFFECT):
            return []
        return reveals

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        turn = int(turn or 0)
        if impl.uge_pending(impl.state(full_state), turn):
            return True
        return producers.closing_pending(full_state, turn)

    def begin_hold(self, full_state, turn) -> None:
        st = impl.state(full_state)
        if impl.uge_pending(st, int(turn or 0)):
            st["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in producers.SCREEN_EVENTS

    def choice_reward_preview(self, full_state, chara_info, event_id):
        from ... import career_events as CE
        from ... import event_engine
        ev = CE.find(full_state, event_id)
        if ev is None or ev.source != "mecha" or not ev.choices:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        event_id = int(event_id or 0)
        turn = impl.turn_of(chara_info)
        if event_id == 1001 and turn <= 1:
            for ev in producers.opening_events():
                CE.emit(full_state, ev)
        elif event_id in producers.STORY_ENDING_TRIGGERS and turn >= 78:
            ev = producers.ending_event(full_state)
            if ev:
                CE.emit(full_state, ev)
        return {}

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"mecha_results": [dict(r) for r in st["results"]],
                "mecha_research": impl.levels(st)}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Mecha())
