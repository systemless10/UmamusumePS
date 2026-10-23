
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


class Ramen(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Ramen"
    endpoint_prefix = "single_mode_ramen"
    data_set_key = "ramen_data_set"
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

    playing_states = frozenset({5, 45, 46, 48})

    turn_end_hold_state = 5

    LEVELUP_EVENTS = {101: 214039, 102: 214040, 103: 214041, 105: 214042, 106: 214043}

    def facility_levelup_event(self, command_id) -> tuple:
        base = formula.SUMMER_TO_BASE.get(int(command_id or 0), int(command_id or 0))
        return (self.LEVELUP_EVENTS.get(base, 214039), 400000005)

    sp_appraisers = frozenset({102, 101})

    def appraisal_events(self):
        return {102: [(90, 214047, 5), (70, 214046, 4), (40, 214045, 3), (0, 214044, 2)],
                101: [(90, 214072, 5), (70, 214071, 4), (40, 214070, 3), (0, 214069, 2)]}

    def gated_npc_charas(self) -> frozenset:
        return frozenset({9001})

    def npc_supporters(self, full_state, chara_info) -> list:
        return list(impl.state(full_state)["npcs"])

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        before = st["preview"]
        rolled = impl.refresh_preview(full_state, chara_info,
                                      {"command_info_array": home_commands or []},
                                      smt._support_card_levels(full_state))
        return None, bool(rolled or st["flags"] or st["preview"] != before)

    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        data = (response or {}).get("data")
        if not isinstance(data, dict) or not isinstance(chara_info, dict):
            return response
        call = endpoint.rsplit("/", 1)[-1]
        if call == "region_select_check":
            return response
        from ...handlers import single_mode_team as smt
        home = data.get("home_info")
        if not isinstance(home, dict):
            home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
        st = impl.state(full_state)
        if impl.career_over(chara_info):
            for ci in (chara_info, (data.get("single_mode_load_common") or {}).get("chara_info")):
                if isinstance(ci, dict) and int(ci.get("playing_state") or 1) == 1:
                    ci["playing_state"] = 5
        data["ramen_data_set"] = impl.ramen_data_set(
            full_state, chara_info, home, endpoint, impl._is_full(endpoint, data),
            start=(call == "start"))
        if call == "start":
            data["ramen_data_set_start"] = {"auto_select_info": [], "auto_select_set_info": [],
                                            "is_checked_uraf_event": 1}
        elif call == "load":
            turn = impl.turn_of(chara_info)
            nested = (data.get("single_mode_load_common") or {}).get("chara_info")
            if st.get("hold_turn") and isinstance(nested, dict):
                nested["turn"] = int(st["hold_turn"])
            last = st["last_tasting"]
            data["ramen_data_set_load"] = {
                "auto_select_info": [], "auto_select_set_info": [],
                "selected_region_id_array": list(st["regions"]),
                "reduce_base_turn_info_array": impl.reduce_base_array(
                    {int(f): v for f, v in st["base"].items()}) if st["base"] else [],
                "check_point_info_array": impl.check_point_info(st),
                "last_tasting_info": (last["info"] if last.get("turn") == turn and last.get("info")
                                      else {"feeling_id_1_num": 0, "feeling_id_2_num": 0,
                                            "feeling_id_3_num": 0, "special_feeling_num": 0,
                                            "region_id": 0}),
                "check_point_pt": int(st["pt"]),
                "expected_check_point_pt": impl.expected_pt(st, turn),
                "used_twinkle_text_id_array": list(st["twinkle"]),
                "is_checked_uraf_event": 1,
            }
        elif call == "check_event":
            data["ramen_data_set_check_event"] = {
                "is_gauge_gained": bool(impl.flag(st, "gauge_gained"))}
        for ev in data.get("unchecked_event_array") or ():
            turn = self._FINALS_POST.get(int(ev.get("event_id") or 0))
            if turn:
                (ev.get("event_contents_info") or {})["show_clear_sort_id"] = \
                    impl.goal_sort_id(chara_info, turn)
        return response

    def settle(self, full_state, chara_info, endpoint=""):
        if not isinstance(chara_info, dict):
            return False
        st = impl.state(full_state)
        by_pos = formula.deck(chara_info)
        changed = False
        for row in chara_info.get("evaluation_info_array") or ():
            pos = int(row.get("target_id") or 0)
            sid = by_pos.get(pos)
            if not sid or sid not in M.deck_link_types():
                continue
            step = int(row.get("story_step") or 0)
            seen = int(st["link_steps"].get(str(sid)) or 0)
            for s in range(seen + 1, step + 1):
                impl.add_secrets(st, M.outing_secrets().get((sid, s), 0))
            if step != seen:
                st["link_steps"][str(sid)] = step
                changed = True
        return changed

    def training_gains(self, full_state, chara_info, command_id, facility_level,
                       camp, partners):
        from ...handlers import single_mode_team as smt
        served = formula.BASE_TO_SUMMER.get(command_id, command_id) if camp else command_id
        return formula.home_gains(chara_info, served, facility_level, partners,
                                  smt._support_card_levels(full_state))

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        st = impl.state(full_state)
        preview = st["preview"]
        if preview.get("turn") != impl.turn_of(chara_info):
            return []
        return [dict(p) for p in preview["bonus"].get(str(command_id)) or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        if ctype == 1:
            impl.train_gauge(full_state, chara_info, cmd, turn)
            impl.count_training(st, cmd)
            st["marks"]["trained"] = {"turn": turn, "command_id": cmd}
        elif not impl.gauge_active(st, turn):
            return
        elif ctype == 7 or cmd == 701:
            st["gauge_due"] = "rest"
        elif ctype == 3:
            st["gauge_due"] = "outing"
        elif ctype == 8 or cmd == 801:
            st["gauge_due"] = "infirmary"

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        if impl.gauge_active(st, turn) and not race_ctx.get("finals_round"):
            st["gauge_due"] = "race"

    def specialty_bonus(self, full_state) -> int:
        return impl.specialty_bonus(full_state)

    def hint_partners(self, full_state, chara_info, command_id, partners, hinting):
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        by_pos = formula.deck(chara_info)
        normal = [p for p in partners if p in by_pos and formula.is_normal_card(by_pos[p])]
        if impl.flagged_turn(st, "all_tips", turn):
            return sorted(set(hinting) | set(normal))
        rate = impl.hint_rate(full_state)
        if not rate:
            return hinting
        import random
        from ...handlers import single_mode_team as smt
        out = list(hinting)
        for pos in normal:
            if pos in out:
                continue
            chance = smt._skill_hint_chance(by_pos[pos]) * rate / 100
            seed = f"ramen:hint:{int(chara_info.get('card_id') or 0)}:{turn}:{pos}"
            if random.Random(seed).random() < chance:
                out.append(pos)
        return out

    def partner_placements(self, full_state, chara_info, turn, occupied=None, standing=None) -> dict:
        return impl.extra_partners(full_state, int(turn or 0))

    def training_failure_override(self, full_state, chara_info, command_id, partners):
        turn = impl.turn_of(chara_info)
        down = impl.failure_down(full_state, turn)
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
        return max(0, rate * (100 - down) // 100)

    def race_bonus_pct(self, full_state, chara_info) -> int:
        st = impl.state(full_state)
        turn = impl.turn_of(chara_info)
        return impl.effect_sum(st, turn, M.EFFECT_RACE) if impl.uraf_active(st, turn) else 0

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        cp = M.check_point_for_turn(turn)
        return bool(cp) and not any(r["check_point_type"] == cp
                                    for r in impl.state(full_state)["results"])

    def begin_hold(self, full_state, turn) -> None:
        impl.state(full_state)["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def choice_reward_preview(self, full_state, chara_info, event_id):
        from ... import career_events as CE
        from ... import event_engine
        ev = CE.find(full_state, event_id)
        if ev is None or ev.source != "ramen" or not ev.choices:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in (producers.JAMBOREE_OPEN_EVENTS | producers.RESULT_EVENTS)

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        event_id = int(event_id or 0)
        if event_id == 1001 and impl.turn_of(chara_info) <= 1:
            ev = producers.opening_event()
            if ev:
                CE.emit(full_state, ev)
        elif event_id == producers.STORY_ENDING_TRIGGER and impl.turn_of(chara_info) >= 78:
            ev = producers.ending_event()
            if ev:
                CE.emit(full_state, ev)
        return {}

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"ramen_results": [dict(r) for r in st["results"]],
                "ramen_uraf_type": int(st["uraf"].get("type") or 0)}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Ramen())
