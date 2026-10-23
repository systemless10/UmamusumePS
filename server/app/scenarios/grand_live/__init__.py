
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import endpoints, impl, preview, producers


class GrandLive(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Grand Live"

    endpoint_prefix = "single_mode_live"
    data_set_key = "live_data_set"

    state_keys = (impl.STATE_KEY, preview.PREVIEW_KEY)

    playing_states = Scenario.playing_states | {impl.PLAYING_STATE_BACKSTAGE}

    has_versus_npc = False

    locked_npc_rows = ((101, 9001), (102, 9002), (103, 9003),
                       (104, 9004), (106, 9006))

    has_director_ending = False

    uses_fixed_schedule = False

    debut_reporter_event = 0

    has_ura_finals = True

    finals_events = {
        1: {"pre": (101001, 400000101, False), "post": (101002, 400000104, 12)},
        2: {"pre": (101003, 400000102, False), "post": (101004, 400000105, 13)},
        3: {"pre": (101005, 400000103, False), "post": (101006, 400000106, 14)},
    }

    LEVELUP_EVENTS = {101: 202056, 102: 202057, 103: 202058,
                      105: 202059, 106: 202060}

    def facility_levelup_event(self, command_id) -> tuple:
        return (self.LEVELUP_EVENTS.get(int(command_id or 0), 202056), 400000005)

    def appraisal_events(self):
        return {
            102: [(90, 202064, 5), (70, 202063, 4), (40, 202062, 3), (0, 202061, 2)],
            103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
        }

    def extra_endpoints(self) -> dict:
        return {"master_square": endpoints.handle_master_square,
                "live_start": endpoints.handle_live_start,
                "reserve_square": endpoints.handle_reserve_square,
                "lottery_square": endpoints.handle_lottery_square}

    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def command_info(self, full_state, chara_info, home_commands):
        return preview.command_info(full_state, chara_info, home_commands)

    def held_turn(self, full_state: dict) -> int:
        return int(impl.state(full_state).get("concert_turn") or 0)

    def friendship_bonus_pct(self, full_state: dict) -> int:
        return impl.friendship_bonus_pct(full_state)

    def specialty_bonus(self, full_state: dict) -> int:
        return impl.live_bonuses(full_state).get("specialty", 0)

    def training_bonus(self, full_state: dict):
        return impl.training_bonus_for(full_state) or None

    def support_event_bonus_pct(self, full_state: dict) -> int:
        return impl.live_bonuses(full_state).get("support_event", 0)

    def award_training_gains(self, full_state, chara_info, payload) -> None:
        preview.award_tokens(full_state, chara_info, payload)

    def apply_currency_gain(self, full_state, chara_info, amount):
        target = impl.grant_lowest_performance(full_state, chara_info, amount)
        if target is None:
            return None
        return impl.perf_wire_target(target), amount

    def interstitial_pending(self, full_state, chara_info, turn) -> bool:
        live_type = impl.live_turn_type(int(turn or 0))
        if live_type is None:
            return False
        done = {r.get("live_type")
                for r in (impl.state(full_state).get("lives_done") or ())}
        return live_type not in done

    def begin_hold(self, full_state: dict, turn: int) -> None:
        impl.state(full_state)["concert_turn"] = int(turn or 0)

    def npc_supporters(self, full_state, chara_info) -> list:
        trainee = int(chara_info.get("card_id") or 0) // 100
        equipped = {impl.support_card_chara(c.get("support_card_id") or 0)
                    for c in chara_info.get("support_card_array", []) or []}
        return [int(c) for c in producers.supporters_for(full_state)
                if int(c) != trainee and int(c) not in equipped]

    def gated_npc_charas(self) -> frozenset:
        return frozenset({9008})

    def on_event_resolved(self, full_state, chara_info, event_id) -> dict:
        return producers.apply_beat_rewards(full_state, chara_info, event_id)

    def holds_chain_after(self, event_id) -> bool:
        return event_id in producers.CONCERT_BEGINS_EVENTS

    _SCORE_WORD = {impl.DANCE: "dance", impl.PASSION: "passion",
                   impl.VOCAL: "vocals", impl.VISUAL: "visuals",
                   impl.MENTAL: "composure"}

    def snapshot(self, full_state: dict) -> dict:
        import copy
        st = impl.state(full_state)
        tokens = {int(k): int(v) for k, v in (st.get("tokens") or {}).items()}
        lives = st.get("lives_done") or []
        grand = next((r for r in lives
                      if int(r.get("live_type") or 0) == impl.GRAND_LIVE_TYPE), None)
        grand_great = bool(grand) and             int(grand.get("result_state") or 0) == impl.RESULT_GREAT
        out = {"grand_live_songs": list(st.get("songs") or []),
               "grand_live_lives_done": copy.deepcopy(lives),
               "performance_points": sum(tokens.get(t, 0) for t in impl.PERF_TYPES),
               "grand_concert": bool(grand),
               "grand_concert_great_success": grand_great,
               "special_grand_concert": grand_great,
               "songs_obtained": len(st.get("songs") or ())}
        for t, word in self._SCORE_WORD.items():
            out["concert_score_" + word] = tokens.get(t, 0)
        return out

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)
        full_state.pop(preview.PREVIEW_KEY, None)


register(GrandLive())
