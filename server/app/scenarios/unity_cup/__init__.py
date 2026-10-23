
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, impl, preview, producers


class UnityCup(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Unity Cup"

    endpoint_prefix = "single_mode_team"
    data_set_key = "team_data_set"

    state_keys = (impl.STATE_KEY, preview.PREVIEW_KEY)

    playing_states = Scenario.playing_states | {
        impl.PLAYING_STATE_TEAM_RACE,
        impl.PLAYING_STATE_TEAM_RACE_RUNNING,
        impl.PLAYING_STATE_TEAM_RACE_RESULT,
    }


    uses_fixed_schedule = False

    debut_reporter_event = 0

    has_director_ending = False

    has_versus_npc = False

    finals_rival_npc_ids = (2002101, 2003101)

    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }

    def extra_endpoints(self) -> dict:
        return {
            "opponent_list": endpoints.handle_opponent_list,
            "team_edit": endpoints.handle_team_edit,
            "team_race_analyze": endpoints.handle_team_race_analyze,
            "team_race_start": endpoints.handle_team_race_start,
            "team_race_end": endpoints.handle_team_race_end,
            "team_race_out": endpoints.handle_team_race_out,
            "team_race_end_out": endpoints.handle_team_race_end_out,
            "team_race_continue": endpoints.handle_team_race_continue,
            "save_team_edit_flag": endpoints.handle_save_team_edit_flag,
        }

    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def command_info(self, full_state, chara_info, home_commands):
        return preview.command_info(full_state, chara_info, home_commands)

    def partner_placements(self, full_state, chara_info, turn, occupied=None, standing=None):
        return preview.scout_placements(full_state, chara_info, turn, occupied)

    def training_failure_override(self, full_state, chara_info, command_id,
                                  partner_ids):
        return preview.training_failure_override(full_state, chara_info,
                                                 command_id, partner_ids)

    def levels_facilities_by_training(self) -> bool:
        return False

    def training_award_bonus(self, full_state, chara_info, command_id) -> list:
        return preview.training_award_bonus(full_state, chara_info, command_id)

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        preview.award_training_gains(full_state, chara_info, payload)

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        return impl.race_turn_pending(impl.state(full_state), turn)

    def begin_hold(self, full_state: dict, turn: int) -> None:
        impl.begin_race_hold(impl.state(full_state), turn)

    def held_turn(self, full_state: dict) -> int:
        from ...handlers import single_mode_team as smt
        career = full_state.get(smt.STATE_KEY) or {}
        chara = ((career.get("data") or {}).get("chara_info") or {})
        return impl.race_hold(impl.state(full_state), chara.get("turn"))

    def retime_queued(self, full_state: dict, endpoint: str = "") -> None:
        producers.retime_race_gate(full_state, endpoint)

    def chain_entry_after(self, full_state: dict, event_id):
        return impl.next_post_race_beat(impl.state(full_state), event_id)

    def gated_npc_charas(self) -> frozenset:
        return frozenset({impl.RIKO_CHARA_ID})

    def persist_upkeep(self, full_state: dict, chara_info: dict) -> None:
        impl.apply_riko_gate(impl.state(full_state), chara_info)

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        st = impl.state(full_state)
        if event_id and event_id == st.get(impl.POST_RACE_LAST_KEY):
            impl.release_race_hold(st)
        if event_id == producers.ACTING_DIRECTOR_EVENT:
            impl.acting_director_resolved(st)
            impl.apply_riko_gate(st, chara_info)
        if event_id in (producers.FACILITY_LEVEL_EVENT[0],
                        producers.FACILITY_LEVEL_EVENT_POST_CUP[0]):
            impl.facility_level_up_resolved(st)
        level = producers.power_up_level(event_id)
        if level:
            impl.power_up_resolved(st, level)
        elif event_id == producers.SCENARIO_INTRO_EVENT:
            impl.scenario_intro_resolved(st)
        elif event_id == producers.TEAM_SUPPORT_EVENT:
            impl.team_support_resolved(st, chara_info)
        elif producers.team_race_join_round(event_id):
            impl.join_wave_resolved(st, producers.team_race_join_round(event_id))
        elif event_id in producers.RACE_GATE_EVENTS:
            chara_info["playing_state"] = impl.PLAYING_STATE_TEAM_RACE
            return {"playing_state": impl.PLAYING_STATE_TEAM_RACE}
        else:
            impl.scout_join_resolved(st, event_id, producers.scout_join_events(st))
        return {}

    _UNSETTLED_ENDPOINTS = ("team_race_start", "team_race_end",
                            "team_race_out", "team_race_end_out",
                            "team_race_continue")

    def settle(self, full_state, chara_info, endpoint: str = "") -> bool:
        if endpoint.rsplit("/", 1)[-1] in self._UNSETTLED_ENDPOINTS:
            return False
        st = impl.state(full_state)
        from ...handlers import single_mode_team as smt
        career = full_state.get(smt.STATE_KEY)
        saved = ((career or {}).get("data") or {}).get("chara_info")
        target = saved if isinstance(saved, dict) else chara_info
        stale = impl.discard_stale_pending(st, target)
        stale = impl.recover_stuck_screen(st, target, endpoint) or stale
        races = st.get("races") or ()
        if races and int(target.get("playing_state") or 0) == impl.PLAYING_STATE_EVENT_CHAIN \
                and int(target.get("turn") or 0) > int(races[-1].get("turn") or 0):
            target["playing_state"] = impl.PLAYING_STATE_TURN_START
            stale = True
        if stale and isinstance(chara_info, dict) and target is not chara_info:
            chara_info["playing_state"] = target.get("playing_state")
        if not st.get("pending_award"):
            return stale
        if not impl.settle_race_award(st, target):
            return stale
        if isinstance(chara_info, dict) and target is not chara_info:
            for key in impl.STATS + ("skill_point",):
                chara_info[key] = target[key]
        return True

    def holds_chain_after(self, event_id) -> bool:
        return event_id in producers.RACE_GATE_EVENTS

    def snapshot(self, full_state: dict) -> dict:
        st = impl.state(full_state)
        title_id = impl.team_title(st)
        races = list(st.get("races") or ())
        return {
            "unity_cup_team_rank": int(st.get("team_rank") or 30),
            "unity_cup_races": [dict(r) for r in races],
            "unity_cup_final_stat_bonus":
                impl.final_rank_stat_bonus(int(st.get("team_rank") or 30)),
            "team_titles": [impl.team_title_name(i)
                            for i in range(1, title_id + 1)],
            "unity_cup_team_title": title_id,
            "spirit_bursts": int(st.get("bursts") or 0),
            "unity_cup_extreme_bursts": int(st.get("extreme_bursts") or 0),
            "unity_training_count": int(st.get("unity_trainings") or 0),
            "unity_training_max_chars": int(st.get("unity_training_max_chars") or 0),
            "unity_cup_won": bool(
                len(races) >= len(impl.TEAM_RACE_TURNS)
                and int((races[-1] or {}).get("result_state") or 0) == 1),
        }

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)
        full_state.pop(preview.PREVIEW_KEY, None)


register(UnityCup())
