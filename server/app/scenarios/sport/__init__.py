
from __future__ import annotations

import copy

from ..base import Scenario
from ..registry import register
from . import endpoints, formula, impl, producers


def _npc_rows() -> tuple:
    from ... import master_data
    return tuple((int(r["partner_id"]), int(r["chara_id"])) for r in master_data.query(
        "SELECT partner_id, chara_id FROM single_mode_unique_chara WHERE scenario_id=7 "
        "ORDER BY partner_id"))


class Sport(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Sport"
    endpoint_prefix = "single_mode_sport"
    data_set_key = "sport_data_set"
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

    playing_states = frozenset({5, impl.PS_COMPETITION, impl.PS_COMPETITION_LIVE})
    turn_end_hold_state = 5
    locked_npc_rows = _npc_rows()
    npc_targets = frozenset(t for t, _c in locked_npc_rows)

    sp_appraisers = frozenset({102, 111})

    def appraisal_events(self):
        return {102: [(90, 207111, 5), (70, 207110, 4), (40, 207109, 3), (0, 207108, 2)],
                103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
                111: [(70, 207102, 4), (40, 207101, 3), (0, 207100, 2)]}

    def base_command(self, command_id) -> int:
        cid = int(command_id or 0)
        if cid in formula.SPORTS:
            return formula.STATCMD[formula.main_stat(cid)]
        return super().base_command(cid)

    def levels_facilities_by_training(self) -> bool:
        return False

    def extra_endpoints(self) -> dict:
        return dict(endpoints.HANDLERS)

    def home_commands(self, full_state, chara_info, turn, commands) -> list:
        from ...handlers import single_mode_team as smt
        return impl.convert_commands(full_state, chara_info, turn, commands,
                                     smt._support_card_levels(full_state))

    def command_info(self, full_state, chara_info, home_commands):
        if not isinstance(chara_info, dict):
            return None, False
        st = impl.state(full_state)
        started = impl.ensure_started(st, chara_info)
        return None, bool(started or st["flags"])

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
        if call == "start" and int(st["preview"].get("turn") or 0) != impl.turn_of(ci):
            impl.rebuild(full_state)
        if st["flags"].pop("rebuilt", None) and isinstance(data.get("home_info"), dict):
            home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info")
            if isinstance(home, dict):
                data["home_info"] = copy.deepcopy(home)
        full = self._reply_is_full(call, data, ci)
        self._sport_levels(data, impl.ranks(st))
        data["sport_data_set"] = impl.data_set(full_state)
        for holder in (data.get("single_mode_start_common"), data.get("single_mode_load_common")):
            if isinstance(holder, dict):
                holder.pop("sport_data_set", None)
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
    def _sport_levels(data: dict, ranks: dict) -> None:
        for ci in (data.get("chara_info"),
                   (data.get("single_mode_load_common") or {}).get("chara_info"),
                   (data.get("single_mode_start_common") or {}).get("chara_info")):
            if not isinstance(ci, dict) or not isinstance(ci.get("training_level_info_array"), list):
                continue
            camp = [dict(e) for e in ci["training_level_info_array"]
                    if isinstance(e, dict) and 600 <= int(e.get("command_id") or 0) < 700]
            ci["training_level_info_array"] = sorted(camp, key=lambda e: int(e["command_id"])) + [
                {"command_id": s, "level": formula.level_of(ranks.get(s, 1))} for s in formula.SPORTS]

    @staticmethod
    def _reply_is_full(call: str, data: dict, chara_info: dict) -> bool:
        if impl.career_over(chara_info) and call != "load":
            return False
        if call in ("start", "load", "use_item", "competition_live"):
            return True
        if call not in ("check_event", "exec_command"):
            return False
        return (not data.get("unchecked_event_array")
                and int(chara_info.get("playing_state") or 1) == 1)

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        row = impl.training_row(impl.state(full_state), command_id, impl.turn_of(chara_info))
        return [dict(p) for p in row.get("bonus") or ()]

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        ctype = int(payload.get("command_type") or 0)
        cmd = int(payload.get("command_id") or 0)
        turn = int(payload.get("current_turn") or impl.turn_of(chara_info))
        if ctype == 1 and cmd in formula.SPORTS:
            impl.after_training(st, cmd, impl.training_row(st, cmd, turn).get("gains") or [])
        else:
            impl.after_other(st, cmd or int(payload.get("command_group_id") or 0))
        impl.rebuild(full_state)

    def on_training_failed(self, full_state, chara_info, payload) -> None:
        st = impl.state(full_state)
        impl.after_training(st, int(payload.get("command_id") or 0), [])
        impl.rebuild(full_state)

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank) -> None:
        st = impl.state(full_state)
        st["raced_turn"] = int(race_ctx.get("turn") or impl.turn_of(chara_info))
        impl.after_other(st, 401)

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        turn = int(turn or 0)
        if impl.compe_pending(impl.state(full_state), turn):
            return True
        return producers.closing_pending(full_state, chara_info, turn)

    def begin_hold(self, full_state, turn) -> None:
        st = impl.state(full_state)
        if impl.compe_pending(st, int(turn or 0)):
            st["hold_turn"] = int(turn or 0)

    def held_turn(self, full_state) -> int:
        return int(impl.state(full_state).get("hold_turn") or 0)

    def holds_chain_after(self, event_id) -> bool:
        return int(event_id or 0) in producers.SCREEN_EVENTS

    def choice_reward_preview(self, full_state, chara_info, event_id):
        from ... import career_events as CE
        from ... import event_engine
        ev = CE.find(full_state, event_id)
        if ev is None or ev.source != "sport" or not ev.choices:
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
        return {"sport_results": [dict(r) for r in st["results"]],
                "sport_ranks": impl.ranks(st)}

    def reset(self, full_state) -> None:
        impl.reset(full_state)


register(Sport())
