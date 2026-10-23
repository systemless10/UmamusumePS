
from __future__ import annotations

import functools
import logging

from .. import master_data

log = logging.getLogger("uma-server")

CAMP_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
CAMP_BASE_BY_ID = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}
CAMP_REST_GROUP = 304


@functools.lru_cache(maxsize=1)
def _restrict_rows() -> dict:
    from .. import master_data
    out: dict = {}
    try:
        rows = master_data.query(
            "SELECT scenario_id, support_card_id FROM single_mode_restrict_support")
    except Exception:
        return {}
    for row in rows:
        out.setdefault(int(row["scenario_id"]), set()).add(int(row["support_card_id"]))
    return {k: frozenset(v) for k, v in out.items()}


def restricted_support_cards(scenario_id) -> frozenset:
    return _restrict_rows().get(int(scenario_id or 0), frozenset())


class Scenario:

    scenario_id: int = 0
    name: str = "base"

    endpoint_prefix: str = "single_mode"

    state_keys: tuple = ()

    data_set_key: str = "ura_data_set"

    data_set_on_race_start: bool = False

    intro_chain: tuple = ()

    plays_calendar_stories: bool = True

    camp_lists_rest: bool = False

    camp_rest_group: int = CAMP_REST_GROUP
    camp_rest_energy: int = 40

    sp_appraisers: frozenset = frozenset({102})

    def serves_camp_rest(self, turn) -> bool:
        return turn in CAMP_TURNS

    def base_command(self, command_id) -> int:
        cid = int(command_id or 0)
        return CAMP_BASE_BY_ID.get(cid, cid)

    def home_commands(self, full_state: dict, chara_info: dict, turn: int,
                      commands: list) -> list:
        return commands

    def specialty_bonus_by_type(self, full_state: dict, chara_info: dict,
                                type_index: int) -> int:
        return 0

    def restricted_support_cards(self) -> frozenset:
        return restricted_support_cards(self.scenario_id)

    def scenario_command(self, full_state: dict, chara_info: dict, payload: dict):
        return None

    def partner_outing(self, full_state: dict, chara_info: dict, partner, turn) -> bool:
        return False

    def extra_endpoints(self) -> dict:
        return {}

    def attach(self, response: dict, full_state: dict, chara_info: dict,
               command_info_array=None, endpoint: str = "") -> dict:
        return response

    def career_route(self, chara_info: dict):
        return None

    def placement_salt(self, full_state: dict) -> int:
        return 0

    def command_info(self, full_state: dict, chara_info: dict, home_commands):
        return None, False

    def held_turn(self, full_state: dict) -> int:
        return 0

    def friendship_bonus_pct(self, full_state: dict) -> int:
        return 0

    def specialty_bonus(self, full_state: dict) -> int:
        return 0

    def specialty_rate_pct(self, full_state: dict) -> int:
        return 0

    def training_bonus(self, full_state: dict):
        return None

    def support_event_bonus_pct(self, full_state: dict) -> int:
        return 0

    def award_training_gains(self, full_state: dict, chara_info: dict,
                             payload: dict) -> None:
        return None

    def on_training_failed(self, full_state: dict, chara_info: dict,
                           payload: dict) -> None:
        return None

    def award_claims_slot(self, full_state: dict) -> bool:
        return False

    def training_bond_bonus(self, full_state: dict, chara_info: dict) -> int:
        return 0

    def mood_change(self, full_state: dict, chara_info: dict, value: int) -> int:
        return value

    def apply_currency_gain(self, full_state: dict, chara_info: dict,
                            amount: int):
        return None

    playing_states: frozenset = frozenset({5})

    turn_end_hold_state: int = 5
    turn_end_timings: tuple = (3, 6, 10)

    def settle(self, full_state: dict, chara_info: dict,
               endpoint: str = "") -> bool:
        return False

    def interstitial_pending(self, full_state: dict, chara_info: dict,
                             turn) -> bool:
        return False

    def begin_hold(self, full_state: dict, turn: int) -> None:
        return None

    def training_failure_override(self, full_state: dict, chara_info: dict,
                                  command_id, partner_ids) -> int:
        return None

    def scale_training_preview(self, full_state: dict, chara_info: dict,
                               command_id, params: list) -> list:
        return params

    def race_field(self, chara_info, program_id, count) -> list | None:
        return None

    def race_field_for(self, viewer_id, chara_info, program_id, count) -> list | None:
        return self.race_field(chara_info, program_id, count)

    def race_player(self, viewer_id, chara_info, player: dict, program_id) -> None:
        return None

    passive_goal_types: frozenset = frozenset()

    def goal_condition(self, chara_info: dict, goal) -> bool | None:
        return None

    def group_race_program(self, full_state: dict, chara_info: dict, group_id: int,
                           turn, race_history) -> int | None:
        return None

    def goal_race_entry_events(self, full_state: dict, chara_info: dict,
                               program_id, turn) -> list | None:
        return None

    def owns_goal_race_result(self, chara_info: dict, program_id) -> bool:
        return False

    def hint_reveals(self, full_state: dict, chara_info: dict, command_id,
                     reveals: list) -> list:
        return reveals

    def facility_levelup_event(self, command_id) -> tuple:
        return (1024, 400000005)

    def appraisal_events(self):
        return None

    def levels_facilities_by_training(self) -> bool:
        return True

    def training_award_bonus(self, full_state: dict, chara_info: dict,
                             command_id) -> list:
        return []

    def training_gains(self, full_state: dict, chara_info: dict, command_id,
                       facility_level: int, camp: bool, partners: list):
        return None

    def hint_partners(self, full_state: dict, chara_info: dict, command_id,
                      partners: list, hinting: list) -> list:
        return hinting

    def partner_placements(self, full_state: dict, chara_info: dict, turn: int,
                           occupied: dict = None, standing: dict = None) -> dict:
        return {}

    def final_partners(self, full_state: dict, chara_info: dict, command_id,
                       turn: int, partners: list) -> list:
        return partners

    npc_targets: frozenset = frozenset()

    replaces_goal_race_event: bool = False

    reserve_arrivals_after_race: bool = False

    inspiration_closes_chain: bool = False

    def npc_supporters(self, full_state: dict, chara_info: dict) -> list:
        return []

    def gated_npc_charas(self) -> frozenset:
        return frozenset()

    def persist_upkeep(self, full_state: dict, chara_info: dict) -> None:
        return None

    def pal_training_roll(self, full_state: dict, chara_info: dict, card_id: int,
                          command_id) -> dict | None:
        return None

    def on_event_resolved(self, full_state: dict, chara_info: dict,
                          event_id) -> dict:
        return {}

    def on_event_choice(self, full_state: dict, chara_info: dict, event_id,
                        choice_number) -> None:
        return None

    def choice_reward_preview(self, full_state: dict, chara_info: dict, event_id):
        return None

    def holds_chain_after(self, event_id) -> bool:
        return False

    def retime_queued(self, full_state: dict, endpoint: str = "") -> None:
        return

    def chain_entry_after(self, full_state: dict, event_id):
        return None

    def race_bonus_pct(self, full_state: dict, chara_info: dict) -> int:
        return 0

    def fan_bonus_pct(self, full_state: dict, chara_info: dict) -> int:
        return 0

    def rival_race_info(self, full_state: dict, chara_info: dict, turn,
                        program_id) -> list:
        return []

    def on_race_result(self, full_state: dict, chara_info: dict,
                       race_ctx: dict, result_rank: int) -> None:
        return None

    def reset(self, full_state: dict) -> None:
        for key in self.state_keys:
            full_state.pop(key, None)

    def snapshot(self, full_state: dict) -> dict:
        return {}


    has_versus_npc: bool = True

    finals_rival_npc_ids: tuple = ()

    has_director_ending: bool = True

    has_reporter_ending: bool = True

    debut_reporter_event: int = 1016

    uses_fixed_schedule: bool = True

    has_ura_finals: bool = True

    has_ending_chain: bool = True

    race_fatigue_exempt_turns: frozenset = frozenset()

    locked_npc_rows: tuple = ()

    debut_is_goal_race: bool = True

    has_secret_events: bool = True

    has_chapter_stories: bool = True

    finals_events: dict | None = None

    post_race_sp: dict | None = None
    finals_reward_base: dict | None = None
    post_race_energy: dict | None = None

    def cap_bonus(self) -> dict:
        return cap_bonus_for_scenario(self.scenario_id)

    def __repr__(self) -> str:
        return f"<Scenario {self.scenario_id} {self.name}>"


def cap_bonus_for_scenario(scenario_id: int) -> dict:
    row = master_data.query_one(
        "SELECT max_speed, max_stamina, max_pow, max_guts, max_wiz "
        "FROM single_mode_scenario WHERE id=?", (scenario_id,))
    if not row:
        return {}
    return {"speed": row["max_speed"], "stamina": row["max_stamina"],
            "power": row["max_pow"], "guts": row["max_guts"], "wiz": row["max_wiz"]}
