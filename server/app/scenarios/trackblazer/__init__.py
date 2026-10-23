
from __future__ import annotations

from ... import career_events as CE
from ..base import Scenario
from ..registry import register
from . import endpoints, impl, producers, shop


class Trackblazer(Scenario):
    scenario_id = impl.SCENARIO_ID
    name = "Trackblazer"

    endpoint_prefix = "single_mode_free"
    data_set_key = "free_data_set"

    state_keys = (impl.STATE_KEY,)

    intro_chain = (4000, 1001)

    uses_fixed_schedule = False

    debut_reporter_event = 0

    has_versus_npc = False

    has_director_ending = False

    has_ending_chain = False

    has_secret_events = False

    has_chapter_stories = False

    debut_is_goal_race = False

    race_fatigue_exempt_turns = frozenset({24, 48, 72})

    has_ura_finals = True

    finals_events = {
        1: {"pre": (203101, 400004050, False), "post": (203102, 400004051, 5, 5)},
        2: {"pre": (203103, 400004060, True), "post": (203104, 400004061, 6, 5)},
        3: {"pre": (203105, 400004070, True), "post": (203106, 400004071, 7, 5)},
    }

    post_race_sp = {"win": (35, 25, 20), "solid": (35, 25, 20)}

    post_race_energy = {
        "win":    (-20, (-10, -25)),
        "solid":  (-25, (-15, -35)),
        "defeat": (-30, (-20, -40)),
    }

    finals_reward_base = {1: (10, 30), 2: (10, 30), 3: (10, 30)}

    def extra_endpoints(self) -> dict:
        return {"multi_item_exchange": endpoints.handle_multi_item_exchange,
                "multi_item_exchange2": endpoints.handle_multi_item_exchange2,
                "multi_item_exchange_2": endpoints.handle_multi_item_exchange2,
                "multi_item_use": endpoints.handle_multi_item_use}

    def attach(self, response, full_state, chara_info, command_info_array=None,
               endpoint=""):
        return impl.attach(response, full_state, chara_info, command_info_array,
                           endpoint=endpoint)

    def settle(self, full_state, chara_info, endpoint=""):
        changed = impl.promote_ranking(full_state, endpoint)
        tail = (endpoint or "").rsplit("/", 1)[-1]
        if tail not in ("race_end", "race_out", "race_start", "race_entry",
                        "get_choice_reward", "change_running_style"):
            changed = shop.publish_pending_offers(full_state) or changed
        return changed

    def held_turn(self, full_state):
        return impl.held_turn(full_state)

    def command_info(self, full_state, chara_info, home_commands):
        commands = [{"command_type": 1, "command_id": cid,
                     "params_inc_dec_info_array":
                         impl.take_bonus_preview(full_state, cid)}
                    for cid in impl.TRAINING_COMMAND_IDS]
        return commands, impl.tick(full_state, chara_info)

    def career_route(self, chara_info):
        return impl.career_route(chara_info)

    def scale_training_preview(self, full_state, chara_info, command_id, params):
        st = impl.state(full_state)
        turn = int(chara_info.get("turn") or 1)
        gain = shop.training_bonus_pct(st, turn, command_id)
        cost = shop.energy_cost_pct(st, turn, command_id)
        if not gain and not cost:
            impl.stash_bonus_preview(full_state, command_id, [])
            return params
        out = []
        bonus = []
        for entry in params:
            target = int(entry.get("target_type") or 0)
            value = int(entry.get("value") or 0)
            if target == 10:
                if cost and value < 0:
                    scaled = -((-value) * (100 + cost) // 100)
                    bonus.append({"target_type": target, "value": scaled - value})
                    value = scaled
            elif gain and 1 <= target <= 5:
                scaled = value * (100 + gain) // 100
                delta = scaled - value
                if delta:
                    bonus.append({"target_type": target, "value": delta})
                value = scaled
            out.append({**entry, "value": value})
        impl.stash_bonus_preview(full_state, command_id, bonus)
        return out

    def training_failure_override(self, full_state, chara_info, command_id,
                                  partner_ids):
        turn = int(chara_info.get("turn") or 1)
        if shop.zero_failure(impl.state(full_state), turn):
            return 0
        return None

    def placement_salt(self, full_state) -> int:
        return int(impl.state(full_state).get("placement_salt") or 0)

    def race_bonus_pct(self, full_state, chara_info) -> int:
        return shop.race_bonus_pct(impl.state(full_state),
                                   int(chara_info.get("turn") or 1))

    def fan_bonus_pct(self, full_state, chara_info) -> int:
        return shop.fan_bonus_pct(impl.state(full_state),
                                  int(chara_info.get("turn") or 1))

    def on_race_result(self, full_state, chara_info, race_ctx, result_rank):
        st = impl.state(full_state)
        turn = int(race_ctx.get("turn") or chara_info.get("turn") or 1)
        program_id = race_ctx.get("program_id")
        finals_round = race_ctx.get("finals_round")
        if finals_round:
            impl.record_climax_leg(full_state, chara_info, turn, program_id,
                                   int(result_rank))
            return
        grade = impl.grade_of(program_id)
        impl.add_win_points(st, impl.win_points_for(grade, int(result_rank)))
        impl.add_coins(st, impl.coins_for(grade, int(result_rank)))
        rival_win = (int(result_rank) == 1
                     and impl.is_rival_race(full_state, program_id, turn))
        if rival_win:
            reward = impl.rival_win_reward(full_state, chara_info, program_id)
            for stat in reward["stats"]:
                cap = "max_wiz" if stat == "wiz" else f"max_{stat}"
                chara_info[stat] = min(int(chara_info.get(stat) or 0)
                                       + impl.RIVAL_WIN_STAT_BONUS,
                                       int(chara_info.get(cap) or 9999))
            if reward["skill_id"]:
                from ... import event_engine
                event_engine._apply_skill_hint(chara_info, reward["skill_id"], 1)
            CE.emit(full_state, CE.Event(
                event_id=self.RIVAL_WIN_EVENT, story_id=self.RIVAL_WIN_STORY,
                play_timing=3, choices=[CE.Choice(effects=[])], chara_id=0,
                once_key=f"tb:rival_win:{turn}",
                priority=CE.PRIO_TRAILING, source="trackblazer_rival_win"))
        shop.maybe_add_limited_offer(
            full_state, turn, int(result_rank),
            is_debut=(grade in (800, 900)), is_rival_win=rival_win)

    RIVAL_WIN_EVENT = 203025
    RIVAL_WIN_STORY = 400004013

    LEVELUP_EVENTS = {101: 203019, 102: 203020, 103: 203021,
                      105: 203022, 106: 203023}

    def facility_levelup_event(self, command_id) -> tuple:
        return (self.LEVELUP_EVENTS.get(int(command_id or 0), 203019), 400000005)

    def appraisal_events(self):
        return {
            102: [(90, 203049, 5), (70, 203048, 4), (40, 203047, 3), (0, 203046, 2)],
            103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
        }

    def rival_race_info(self, full_state, chara_info, turn, program_id):
        return impl.rival_for_turn(full_state, chara_info, turn, program_id)

    def snapshot(self, full_state: dict) -> dict:
        st = impl.state(full_state)
        legs = (st.get("climax") or {}).get("results") or []
        return {
            "trackblazer_win_points": int(st.get("win_points") or 0),
            "trackblazer_prev_win_points": int(st.get("prev_win_points") or 0),
            "trackblazer_coins": int(st.get("coins") or 0),
            "trackblazer_climax_rank": impl.climax_ranking(full_state),
            "trackblazer_objectives": dict(st.get("objectives") or {}),
            "result_points": int(st.get("win_points_total") or 0),
            "pro_shop_coins": int(st.get("coins_total") or 0),
            "pro_shop_items": int(st.get("items_bought") or 0),
            "ts_climax_all_won": (
                len(legs) >= len(impl.CLIMAX_TURNS)
                and all(int(r.get("player_rank") or 0) == 1 for r in legs)),
            "trackblazer_climax_legs": [
                {"turn": int(r.get("turn") or 0),
                 "player_rank": int(r.get("player_rank") or 0)} for r in legs],
        }

    def reset(self, full_state: dict) -> None:
        impl.reset(full_state)


register(Trackblazer())
