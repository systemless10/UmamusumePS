
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, master as M, producers


def _partner_rows() -> tuple:
    try:
        from ... import master_data
        return tuple((int(r["partner_id"]), int(r["chara_id"])) for r in master_data.query(
            "SELECT partner_id, chara_id FROM single_mode_unique_chara WHERE scenario_id=? "
            "ORDER BY partner_id", (impl.SCENARIO_ID,)))
    except Exception:
        return ()


class Legend(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Legend"
    endpoint_prefix = "single_mode_legend"
    data_set_key = "legend_data_set"
    data_set_on_race_start = True
    state_keys = (impl.STATE_KEY,)

    uses_fixed_schedule = False
    has_versus_npc = False
    debut_reporter_event = 0
    has_director_ending = False
    has_ura_finals = False
    has_ending_chain = False
    plays_calendar_stories = False
    reserve_arrivals_after_race = True
    camp_lists_rest = True
    replaces_goal_race_event = True
    playing_states = frozenset({5, 28, 29, 30, 31, 32, 33})
    turn_end_hold_state = 5
    locked_npc_rows = _partner_rows()
    npc_targets = frozenset(t for t, _c in _partner_rows())

    def career_route(self, chara_info: dict):
        from ... import career_branches, master_data
        chara = int((chara_info or {}).get("card_id") or 0) // 100
        row = master_data.query_one(
            "SELECT id, race_set_id FROM single_mode_route WHERE scenario_id=0 AND chara_id=? "
            "ORDER BY priority DESC LIMIT 1", (chara,))
        if not row:
            return None
        ids = tuple(int(r["id"]) for r in master_data.query(
            "SELECT id FROM single_mode_route_race WHERE race_set_id=? ORDER BY id",
            (row["race_set_id"],)))
        route = career_branches.start_route(chara, row["race_set_id"], ids)
        finals = {int(r["id"]) for r in master_data.query(
            "SELECT id FROM single_mode_route_race WHERE race_set_id=? AND target_type=3",
            (row["race_set_id"],))}
        return row["id"], [i for i in route if int(i) not in finals]

    def partner_outing(self, full_state, chara_info, partner, turn) -> bool:
        return int(partner or 0) in M.LEGENDS

    def race_field(self, chara_info, program_id, count):
        if not M.is_sprace_program(program_id):
            return None
        return impl.race_field(impl.SIM_STATE.get("full_state") or {}, chara_info,
                               int(program_id or 0), count)

    def facility_levelup_event(self, command_id) -> tuple:
        return (producers.LEVEL_UP_EVENTS.get(int(command_id or 0), 210012), 400000005)

    def appraisal_events(self):
        return {102: list(producers.DIRECTOR_APPRAISAL),
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)]}

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
        live = impl.live_chara(full_state)
        live_ps = int(live.get("playing_state") or 1)
        if impl.RI.career_over(chara_info):
            for ci in (chara_info, (data.get("single_mode_load_common") or {}).get("chara_info")):
                if isinstance(ci, dict) and int(ci.get("playing_state") or 1) == 1:
                    ci["playing_state"] = 5
        if live_ps in (28, 29, 30, 31, 32, 33):
            for ci in (chara_info, (data.get("single_mode_load_common") or {}).get("chara_info")):
                if isinstance(ci, dict):
                    ci["playing_state"] = live_ps
        self._drop_carried_partners(chara_info)
        self.persist_upkeep(full_state, chara_info)
        for common in ("single_mode_load_common", "single_mode_start_common"):
            nested = (data.get(common) or {}).get("chara_info")
            if isinstance(nested, dict):
                self._drop_carried_partners(nested)
                self.persist_upkeep(full_state, nested)
        events = data.get("unchecked_event_array") or []
        appear = any(isinstance(e, dict) and int(e.get("story_id") or 0) == producers.OPENING[1][1]
                     for e in events)
        data["legend_data_set"] = impl.data_set(
            full_state, chara_info, home, impl.reply_is_full(endpoint, data), appear_legend=appear)
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("legend_data_set", None)
        return response

    @staticmethod
    def _drop_carried_partners(chara_info: dict) -> None:
        deck_charas = {formula.RF.card_meta(sid)[3] for sid in formula.RF.deck(chara_info).values()}
        carried = {t for t, c in _partner_rows() if c in deck_charas}
        if carried:
            chara_info["evaluation_info_array"] = [
                e for e in chara_info.get("evaluation_info_array") or ()
                if not (isinstance(e, dict) and e.get("target_id") in carried)]

    def persist_upkeep(self, full_state, chara_info) -> None:
        k = impl.group_card(chara_info)
        if not k:
            return
        from ...handlers import single_mode_team as smt
        entry = smt._pal_card_entry(full_state, M.GROUP_CARD)
        steps = entry.get("steps") or {}
        unlocked = 1 if entry.get("unlocked") else 0
        for row in chara_info.get("evaluation_info_array") or ():
            if not (isinstance(row, dict) and row.get("target_id") == k[0]):
                continue
            row["group_outing_info_array"] = [
                {"chara_id": g, "is_outing": unlocked, "story_step": min(1, int(steps.get(str(g), 0)))}
                for g in M.LEGENDS]
            row["story_step"] = max(0, int(steps.get(str(M.SPEED), 0)) - 1)
            row["is_outing"] = unlocked

    def training_gains(self, full_state, chara_info, command_id, facility_level, camp, partners):
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        served = formula.BASE_TO_SUMMER.get(command_id, command_id) if camp else command_id
        return formula.home_gains(chara_info, served, facility_level, partners,
                                  smt._support_card_levels(full_state), super_mood=impl.super_on(st),
                                  energy_down=impl.energy_down(full_state, chara_info))

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        return impl.preview_bonus(impl.state(full_state), impl.served_turn(full_state, chara_info),
                                  int(command_id or 0))

    def training_failure_override(self, full_state, chara_info, command_id, partners):
        return 0 if impl.zone_on(impl.state(full_state)) else None

    def training_bond_bonus(self, full_state, chara_info) -> int:
        return impl.effect_total(chara_info, impl.state(full_state), M.EFF_BOND_UP)

    def specialty_bonus(self, full_state) -> int:
        live = impl.live_chara(full_state)
        return impl.effect_total(live, impl.state(full_state), M.EFF_SPECIALTY)

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        live = impl.live_chara(full_state) or chara_info
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        group = int(payload.get("command_group_id") or 0)
        summer = turn in impl.SUMMER_TURNS
        if ctype == 1:
            partners = impl.preview_of(st, turn, "partners", cmd, []) or []
            friendship = bool(impl.preview_of(st, turn, "friendly", cmd, False))
            legend = impl.colour(full_state, turn, 1, cmd)
            impl.add_gauge(full_state, legend, int(impl.preview_of(st, turn, "gain_gauge", cmd, 0) or 0))
            supporters = len(formula.RF.facility_cards(chara_info, partners)) + formula.guests_in(partners)
            impl.after_command(full_state, live, turn, {
                "ctype": 1, "success": True, "friendship": friendship,
                "supporters": supporters, "partners": partners})
            if st["masterly"] == M.HAISEIKO:
                impl.train_friends(full_state, live, partners, friendship)
            z = st["s9047"]
            if impl.zone_on(st):
                rate = impl.zone_rate_for(st, cmd)
                if rate and impl.rng_for(full_state, "zone", turn).random() * 100 < rate:
                    impl.end_zone(full_state, 2)
                    z["end"]["turn"] = turn
            elif st["masterly"] == M.SPEED:
                z["step"] = min(impl.ZONE_STEPS, int(z["step"]) + 1)
                if int(z["step"]) >= impl.ZONE_STEPS:
                    z["start_due"] = True
            k = impl.group_card(chara_info)
            entry = _pal_entry(full_state)
            if k and k[0] in partners and entry.get("met") and \
                    impl.rng_for(full_state, "card003", turn).random() < producers.CARD_TRAINING_CHANCE:
                st["card003_due"] = turn
            return
        is_rest = ctype == 7 or cmd == 701
        is_outing = ctype == 3 and group != 390
        if not (is_rest or is_outing or ctype == 8):
            return
        impl.after_command(full_state, live, turn, {"ctype": 8 if ctype == 8 else (7 if is_rest else 3),
                                                     "success": True, "friendship": False,
                                                     "supporters": 0, "partners": []})
        if impl.zone_on(st):
            impl.end_zone(full_state, 1)
            st["s9047"]["end"]["turn"] = turn
        if is_outing:
            camp = group == 304 or cmd == 304 or summer
            up = 1 if camp else max(0, int(live.get("motivation") or 3)
                                    - int(chara_info.get("motivation") or 3))
            if up:
                impl.count_mood_up(full_state, up)
        if is_rest or is_outing:
            row = (3, 0) if is_outing else (7, 701)
            st["due"].append({"legend": impl.colour(full_state, turn, *row), "turn": turn,
                              "summer": summer, "kind": "rest" if is_rest else "outing"})

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        from ...handlers import single_mode_team as smt
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        program = int(race_ctx.get("program_id") or 0)
        rank = int(result_rank or 99)
        impl.snapshot_race(full_state, race_ctx, chara_info, rank)
        st["due"].append({"legend": impl.colour(full_state, turn, 4, 401), "turn": turn,
                          "summer": turn in impl.SUMMER_TURNS, "kind": "race"})
        rows = smt._route_goal_rows(tuple(chara_info.get("route_race_id_array") or ()))
        goal = next((r for r in rows if r[0] == turn), None)
        if goal is None:
            return
        need = int(goal[3] or 0)
        if need and rank > need:
            return
        st["goal_due"] = {"rank": rank, "turn": turn, "program_id": program,
                          "sort": impl.RI.goal_sort_id(chara_info, turn)}
        if 25 <= turn <= 48 and 1 not in st["pop_done"]:
            st["pop_due"].append(1)
        if 49 <= turn <= 72 and 3 not in st["pop_done"]:
            st["pop_due"].append(3)

    def hint_partners(self, full_state, chara_info, command_id, partners, hinting):
        st = impl.state(full_state)
        rate = impl.effect_total(chara_info, st, M.EFF_HINT)
        fixed = impl.effect_total(chara_info, st, M.EFF_HINT_FIXED)
        by_pos = formula.RF.deck(chara_info)
        normal = [p for p in partners if p in by_pos and formula.RF.is_normal_card(by_pos[p])]
        out = list(hinting)
        if rate:
            from ...handlers import single_mode_team as smt
            turn = impl.turn_of(chara_info)
            for pos in normal:
                if pos in out:
                    continue
                chance = smt._skill_hint_chance(by_pos[pos]) * rate / 100
                if impl.rng_for(full_state, "hint", turn, command_id, pos).random() < chance:
                    out.append(pos)
        if fixed and normal and not any(p in out for p in normal):
            out.append(normal[0])
        return out

    def partner_placements(self, full_state, chara_info, turn, occupied=None, standing=None) -> dict:
        st = impl.state(full_state)
        out: dict = {}
        occupied = dict(occupied or {})
        rng = impl.rng_for(full_state, "place", turn)
        cmds = list(formula.TRAINING_COMMANDS)

        def put(cmd, pid):
            if int(occupied.get(cmd, 0)) >= 5:
                return
            out.setdefault(cmd, []).append(pid)
            occupied[cmd] = int(occupied.get(cmd, 0)) + 1

        for cmd_s, chara in sorted(st["s9048"]["guests"].items()):
            chara = int(chara)
            if impl.is_full(st, chara):
                put(int(cmd_s), chara)
            elif rng.random() < impl.GUEST_PRESENT_CHANCE:
                put(rng.choice(cmds), chara)
        extra = impl.effect_total(chara_info, st, M.EFF_ADD_SUPPORT)
        if extra:
            placed = {p for ps in (standing or {}).values() for p in ps}
            idle = [p for p in sorted(formula.RF.deck(chara_info)) if p not in placed]
            rng.shuffle(idle)
            for pos in idle[:extra]:
                put(rng.choice(cmds), pos)
        return out

    def final_partners(self, full_state, chara_info, command_id, turn, partners) -> list:
        st = impl.state(full_state)
        forced = impl.forced_cards(chara_info, st)
        if not forced:
            return partners
        base = formula.SUMMER_TO_BASE.get(int(command_id or 0), int(command_id or 0))
        out = [p for p in partners if forced.get(p, base) == base]
        for pos, cmd in sorted(forced.items()):
            if cmd == base and pos not in out and len(out) < 5:
                out.append(pos)
        return out

    def mood_change(self, full_state, chara_info, value: int) -> int:
        st = impl.state(full_state)
        if st["masterly"] != M.ST_LITE:
            return value
        if value < 0:
            return 0 if impl.super_on(st) else value
        return impl.count_mood_up(full_state, value) if value > 0 else value

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        return impl.sprace_pending(impl.state(full_state), int(turn or 0))

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
        if ev is None or ev.source != "legend":
            return None
        if int(event_id or 0) == producers.LINK_RACE[0]:
            out = []
            for g in sorted(producers.LINK_SKILLS):
                arr = event_engine.choice_reward_array(
                    {"choices": [{"effects": producers.link_effects(chara_info, g)}]}, chara_info or {})
                for row in arr:
                    out.append({"select_index": g, "gain_param_array": row.get("gain_param_array") or []})
            return out
        if not ev.choices or len(ev.choices) < 2:
            return None
        return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        from ... import career_events as CE
        from ...handlers import single_mode_team as smt
        event_id = int(event_id or 0)
        st = impl.state(full_state)
        if event_id == 1001 and impl.turn_of(chara_info) <= 1:
            for ev in producers.opening_events():
                CE.emit(full_state, ev)
            return {}
        if not impl.group_card(chara_info):
            return {}
        seen = st["card_seen"]
        kind = producers.CARD_DATE_EVENTS.get(event_id)
        turn = impl.turn_of(chara_info)
        if kind and not seen.get(f"{event_id}:{turn}"):
            seen[f"{event_id}:{turn}"] = 1
            impl.add_all_gauges(full_state, 3)
            if kind == "date":
                smt._grant_pure_passion(full_state, chara_info, M.GROUP_CARD, turn)
        entry = smt._pal_card_entry(full_state, M.GROUP_CARD)
        if entry.get("unlocked") and not seen.get("unlock"):
            seen["unlock"] = 1
            impl.add_all_gauges(full_state, 3)
        return {}

    def on_event_choice(self, full_state, chara_info, event_id, choice_number) -> None:
        if int(event_id or 0) != producers.CARD_DATE4 or not impl.group_card(chara_info):
            return
        st = impl.state(full_state)
        key = f"date4:{impl.turn_of(chara_info)}"
        if st["card_seen"].get(key):
            return
        st["card_seen"][key] = 1
        idx = max(1, min(3, int(choice_number or 1)))
        impl.add_gauge(full_state, M.LEGENDS[idx - 1], 0, set_to=M.GAUGE_MAX)
        from ...handlers import single_mode_team as smt
        smt._grant_pure_passion(full_state, chara_info, M.GROUP_CARD, impl.turn_of(chara_info))

    def snapshot(self, full_state) -> dict:
        st = impl.state(full_state)
        return {"legend_races": [dict(r) for r in st["races"]], "legend_buffs": impl.held(st),
                "legend_guidance": int(st["masterly"])}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


def _pal_entry(full_state: dict) -> dict:
    from ...handlers import single_mode_team as smt
    return smt._pal_card_entry(full_state, M.GROUP_CARD)


register(Legend())
