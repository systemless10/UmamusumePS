
from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import json
import os
import random
import zlib
from datetime import datetime, timezone
from pathlib import Path

import logging

from .. import career_branches
from .. import race_conditions
from .. import career_events
from .. import tuning
from .. import region
from .. import concerts
from .. import epithets
from .. import config
from .. import event_engine
from .. import master_data
from .. import patch
from .. import paid_features
from .. import state as state_store
from .. import training_formula
from ..fixtures import templates as fixtures
from ..simulation import race_simulator
from . import (archive_directory, bond, collection, conditions, missions, pal_cards,
               practice_race, presents, race_fatigue, race_mobs, secret_events,
               single_mode_events, stamina, trained_chara, transfer)
from .. import scenarios
from ..result_codes import refuse

log = logging.getLogger("uma-server")

STATE_KEY = "single_mode_team"
START_CHARA_STATE_KEY = "single_mode_team_start_chara"
FRIEND_SUPPORT_INJECT_KEY = "injected_friend_support_cards"

SCENARIO_NAMES = {1: "URA Finale", 2: "Unity Cup", 3: "Grand Live"}

SM_CHARA_ID_BASE = 9_000_000

_STAT_TO_RARITY_COL = {
    "speed": "speed", "stamina": "stamina", "power": "pow",
    "guts": "guts", "wiz": "wiz",
}
_APTITUDE_FIELDS = (
    "proper_distance_short", "proper_distance_mile", "proper_distance_middle",
    "proper_distance_long", "proper_running_style_nige",
    "proper_running_style_senko", "proper_running_style_sashi",
    "proper_running_style_oikomi", "proper_ground_turf", "proper_ground_dirt",
)


def _card_rarity_row(card_id: int, star_level: int | None = None) -> dict | None:
    rows = master_data.query(
        "SELECT * FROM card_rarity_data WHERE card_id=? ORDER BY rarity", (card_id,))
    if not rows:
        return None
    by_rarity = {r["rarity"]: dict(r) for r in rows}
    if star_level:
        target = max(min(by_rarity), min(max(by_rarity), int(star_level)))
        return by_rarity[target]
    dr_row = master_data.query_one("SELECT default_rarity FROM card_data WHERE id=?", (card_id,))
    dr = dr_row["default_rarity"] if dr_row else None
    return by_rarity.get(dr) or by_rarity[max(by_rarity)]


def _owned_support(viewer_id) -> dict:
    full = state_store.get_state(viewer_id) or {}
    return {s.get("support_card_id"): s for s in (full.get(collection.SUPPORT_CARD_KEY) or [])}


def _career_support_array(viewer_id, start_chara: dict) -> list[dict]:
    owned = _owned_support(viewer_id)
    out = []
    friend = (start_chara.get("friend_support_card_info") or {}).get("support_card_id")
    for sid in [s for s in (start_chara.get("support_card_ids") or []) if s][:5 if friend else 6]:
        o = owned.get(sid, {})
        out.append({
            "position": len(out) + 1,
            "support_card_id": sid,
            "limit_break_count": o.get("limit_break_count", 0),
            "exp": o.get("exp", 0),
            "owner_viewer_id": 0,
        })
    if friend:
        o = owned.get(friend, {})
        out.append({
            "position": len(out) + 1,
            "support_card_id": friend,
            "limit_break_count": o.get("limit_break_count", 0),
            "exp": o.get("exp", 0),
            "owner_viewer_id": 0,
        })
    return out


def _owned_card(viewer_id, card_id: int) -> dict:
    full = state_store.get_state(viewer_id) or {}
    for c in (full.get(collection.CARD_LIST_KEY) or []):
        if c.get("card_id") == card_id:
            return c
    return {}


def _career_route(scenario_id, chara_id, chara_info=None):
    own = scenarios.get(scenario_id).career_route(chara_info or {})
    if own is not None:
        return own
    rows = master_data.query(
        "SELECT id, race_set_id FROM single_mode_route "
        "WHERE scenario_id=0 AND chara_id=? ORDER BY priority DESC LIMIT 1", (chara_id,))
    row = rows[0] if rows else None
    if not row:
        return None, []
    races = master_data.query(
        "SELECT id FROM single_mode_route_race WHERE race_set_id=? ORDER BY id",
        (row["race_set_id"],))
    ids = tuple(r["id"] for r in races)
    return row["id"], career_branches.start_route(chara_id, row["race_set_id"], ids)


def _build_start_chara_info(viewer_id, start_chara: dict, template_ci: dict) -> dict:
    ci = copy.deepcopy(template_ci)
    card_id = start_chara.get("card_id")
    owned = _owned_card(viewer_id, card_id) if card_id else {}
    star_level = start_chara.get("rarity") or owned.get("rarity")
    talent_level = start_chara.get("talent_level") or owned.get("talent_level") or 1
    rd = _card_rarity_row(card_id, star_level) if card_id else None

    ci["card_id"] = card_id
    ci["single_mode_chara_id"] = SM_CHARA_ID_BASE + (card_id or 0) % 1_000_000
    ci["scenario_id"] = start_chara.get("scenario_id", ci.get("scenario_id"))
    ci["talent_level"] = talent_level

    cap_bonus = scenarios.cap_bonus_for_scenario(ci.get("scenario_id") or 1)
    if rd:
        ci["rarity"] = rd.get("rarity", ci.get("rarity"))
        for stat, col in _STAT_TO_RARITY_COL.items():
            ci[stat] = rd.get(col, 0)
            base_cap = rd.get(f"max_{col}", ci.get(f"max_{stat}", 1200))
            ci[f"max_{stat}"] = base_cap + cap_bonus.get(stat, _CAREER_BASE_CAP_BONUS)
            ci[f"default_max_{stat}"] = base_cap
        for apt in _APTITUDE_FIELDS:
            if apt in rd:
                ci[apt] = rd[apt]

    route_id, route_races = _career_route(ci["scenario_id"], (card_id or 0) // 100, ci)
    if route_id is not None:
        ci["route_id"] = route_id
        ci["route_race_id_array"] = route_races

    ci["succession_trained_chara_id_1"] = start_chara.get("succession_trained_chara_id_1", 0) or 0
    ci["succession_trained_chara_id_2"] = start_chara.get("succession_trained_chara_id_2", 0) or 0
    ci["support_card_array"] = _career_support_array(viewer_id, start_chara)

    ci["turn"] = 1
    ci["playing_state"] = 1
    ci["state"] = 0
    ci["vital"] = ci.get("max_vital", 100)
    ci["motivation"] = 3
    ci["fans"] = 1
    ci["skill_point"] = 0
    ci["skill_array"] = []
    ci["skill_tips_array"] = []
    card_hints = {s.get("skill_id"): s.get("level") or 0
                  for s in owned.get("skill_data_array") or () if isinstance(s, dict)}
    innate: dict[tuple[int, int], int] = {}
    for row in master_data.query(
            "SELECT skill_id, need_rank FROM available_skill_set WHERE available_skill_set_id=?",
            (ci.get("card_id"),)):
        level = card_hints.get(row["skill_id"], 0)
        if row["need_rank"] <= talent_level and level > 0:
            key = master_data.skill_tip_key(row["skill_id"])
            innate[key] = max(innate.get(key, 0), level)
    for (gid, rarity), level in sorted(innate.items()):
        ci["skill_tips_array"].append({"group_id": gid, "rarity": rarity, "level": min(5, level)})
    _grant_unique_skill(ci)
    _init_bonds(ci)
    _apply_initial_stats(ci)
    _apply_inheritance(ci, viewer_id, start_chara)
    return ci


CURRENT_ENDPOINT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "career_endpoint", default="")


def _endpoint_scenario_id():
    prefix = (CURRENT_ENDPOINT.get() or "").split("/", 1)[0]
    if not prefix:
        return None
    for scen in scenarios.all_scenarios():
        if scen.endpoint_prefix == prefix:
            return scen.scenario_id
    return None


def _force_seeded_scenario(viewer_id, career_state: dict, scenario_id) -> None:
    if not scenario_id or not isinstance(career_state, dict):
        return
    data = career_state.get("data")
    if not isinstance(data, dict):
        return
    changed = False
    for holder in (data, data.get("single_mode_start_common"),
                   data.get("single_mode_load_common")):
        if isinstance(holder, dict) and isinstance(holder.get("chara_info"), dict):
            if holder["chara_info"].get("scenario_id") != scenario_id:
                holder["chara_info"]["scenario_id"] = scenario_id
                changed = True
    if not changed:
        return
    intro = (list(scenarios.get(scenario_id).intro_chain)
             or single_mode_events.INTRO_CHAIN)
    if intro:
        ci = data.get("chara_info") or {}
        head = single_mode_events.event_entry(
            intro[0], (ci.get("card_id") or 0) // 100)
        for holder in (data, data.get("single_mode_start_common"),
                       data.get("single_mode_load_common")):
            if isinstance(holder, dict) and holder.get("unchecked_event_array"):
                holder["unchecked_event_array"] = [copy.deepcopy(head)]
    _install_own_data_set(data, data.get("chara_info") or {})
    full_state = state_store.get_state(viewer_id) or {}
    full_state[STATE_KEY] = career_state
    pending = full_state.get(single_mode_events.PENDING_EVENTS_KEY)
    if intro and isinstance(pending, list) and pending:
        full_state[single_mode_events.PENDING_EVENTS_KEY] = list(intro)
    state_store.save_state(viewer_id, full_state)


def _install_own_data_set(career_data: dict, chara_info: dict) -> None:
    scen = scenarios.for_chara(chara_info)
    keep = scen.data_set_key
    for key in _SCENARIO_DATA_SET_KEYS:
        if key != keep and key in career_data:
            del career_data[key]
    if keep == "ura_data_set" and keep not in career_data:
        try:
            seed = _load_ura_race_seed("train_check_event")["data"]["ura_data_set"]
        except (OSError, KeyError):
            return
        career_data[keep] = copy.deepcopy(seed)


def _seed_fresh_career(viewer_id, start_chara: dict | None) -> dict:
    pair = fixtures.find("single_mode_team/start", **{"start_chara": start_chara})
    if pair is None:
        pair = fixtures.first("single_mode_team/start")
    if pair is None:
        raise LookupError("No captured single_mode_team/start fixture available")

    career_state = pair.response_copy()
    career_state.setdefault("data", {}).setdefault("race_history", [])
    _install_own_data_set(career_state["data"],
                          career_state["data"].get("chara_info") or {})

    full_state = state_store.get_state(viewer_id) or {}
    from . import factor_research
    if region.CURRENT_REGION.get() == "jp" or factor_research.current() is not None:
        factor_research.freeze_parents(full_state, viewer_id, start_chara or {})
    full_state[STATE_KEY] = career_state
    full_state[START_CHARA_STATE_KEY] = start_chara or {}
    state_store.save_state(viewer_id, full_state)
    return career_state


def _seed_dynamic_career(viewer_id, start_chara: dict) -> dict:
    pair = fixtures.first("single_mode/start") or fixtures.first("single_mode_team/start")
    if pair is None:
        raise LookupError("No captured single_mode start fixture available")

    career_state = pair.response_copy()
    data = career_state.get("data", {})

    template_ci = data.get("chara_info", {})
    chara_info = _build_start_chara_info(viewer_id, start_chara, template_ci)

    data["chara_info"] = chara_info
    _install_own_data_set(data, chara_info)
    data.setdefault("race_history", [])
    player_chara = (chara_info.get("card_id") or 0) // 100
    intro = (list(scenarios.for_chara(chara_info).intro_chain)
             or single_mode_events.INTRO_CHAIN)
    data["unchecked_event_array"] = [single_mode_events.event_entry(intro[0], player_chara)] if intro else []
    home = data.get("home_info")
    if isinstance(home, dict):
        _refresh_command_info(chara_info, home, turn=chara_info.get("turn", 1),
                              facility_levels=_facility_levels(data),
                              race_history=data.get("race_history", []),
                              support_card_levels=_support_card_levels(
                                  state_store.get_state(viewer_id) or {}),
                              full_state=state_store.get_state(viewer_id) or {})
    smsc = data.get("single_mode_start_common")
    if isinstance(smsc, dict):
        smsc["chara_info"] = copy.deepcopy(chara_info)
        smsc["unchecked_event_array"] = copy.deepcopy(data["unchecked_event_array"])
        if isinstance(smsc.get("home_info"), dict) and isinstance(home, dict):
            smsc["home_info"]["command_info_array"] = copy.deepcopy(home["command_info_array"])

    full_state = state_store.get_state(viewer_id) or {}
    full_state[STATE_KEY] = career_state
    full_state[START_CHARA_STATE_KEY] = start_chara or {}
    from . import factor_research
    if region.CURRENT_REGION.get() == "jp" or factor_research.current() is not None:
        factor_research.freeze_parents(full_state, viewer_id, start_chara or {})
    full_state[single_mode_events.PENDING_EVENTS_KEY] = list(intro)
    _remember_display(full_state, career_state)
    state_store.save_state(viewer_id, full_state)
    return career_state


def career_state_keys() -> tuple:
    return _CAREER_STATE_KEYS()


def clear_active_career(viewer_id) -> list[str]:
    full_state = state_store.get_state(viewer_id) or {}
    keys = career_state_keys()
    removed = [k for k in keys if k in full_state]
    for k in removed:
        del full_state[k]
    if removed:
        state_store.save_state(viewer_id, full_state)
    return removed


def _CAREER_STATE_KEYS() -> tuple:
    return (STATE_KEY, START_CHARA_STATE_KEY, "career_parent_snapshot", "training_challenge_run", "_custom_support_pack", "_custom_support_fired", RACE_CTX_KEY, STYLE_CHOICE_KEY,
            RACE_REWARD_KEY, APPRAISAL_CTX_KEY, CAREER_CTX_QUEUE_KEY,
            PAL_STATE_KEY, PAL_EVENT_CTX_KEY, PURE_PASSION_KEY,
            GOAL_MARKED_KEY, GOAL_ANNOUNCED_KEY, GOAL_STORIES_FIRED_KEY,
            GOAL_EVENT_KEY, HOT_SPRING_KEY,
            SCENARIO_FIXED_FIRED_KEY,
            UNIQUE_LEVEL_FIRED_KEY, DIRECTOR_FAN_FIRED_KEY,
            EPITHET_RUN_FIRED_KEY,
            SCENARIO_TURNS_FIRED_KEY, SCENARIO_EVENTS_DONE_KEY,
            POST_RACE_FIRED_KEY, RAFFLE_CTX_KEY, INFIRMARY_CTX_KEY,
            CRANE_PLAYED_KEY, ENDING_PAYOUT_KEY, DISPLAY_EVENT_KEY,
            "career_factor_roll",
            _TURN_SERVED_KEY, _TURN_HOLD_RELEASED_KEY,
            SKILL_UPGRADE_KEY,
            INSPIRATION_CTX_KEY, CRANE_CTX_KEY,
            event_engine.FIRED_EVENTS_KEY,
            event_engine.LAST_TRAINED_KEY,
            event_engine.CHAIN_ENDED_KEY,
            event_engine.CAREER_EVENT_CTX_KEY,
            HINT_REVEAL_CTX_KEY, REST_CTX_KEY,
            single_mode_events.FAIL_CTX_KEY,
            CAREER_FAILED_FLAG_KEY,
            single_mode_events.EXTRA_EVENTS_KEY,
            single_mode_events.PENDING_EVENTS_KEY,
            single_mode_events.UNLOCKED_NPCS_KEY,
            single_mode_events.VERSUS_LEVEL_KEY,
            single_mode_events.DUEL_CTX_KEY,
            single_mode_events.DUEL_LAST_TURN_KEY,
            *scenarios.state_keys(),
            GRAND_LIVE_EVENTS_FIRED_KEY,
            career_events.STATE_KEY,
            "idle_single_mode_run",
            RESERVED_RACES_KEY,
            FRIENDSHIP_STACK_KEY, CRANE_SESSION_KEY)


def handle_pre_single_mode_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    _ensure_default_friend(viewer_id, full_state)
    full_state = state_store.get_state(viewer_id) or full_state
    pair = fixtures.first("pre_single_mode/index")
    base = pair.response_copy() if pair is not None else {
        "response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
        "data": {}}
    data = base.setdefault("data", {})
    data["default_running_style_array"] = []

    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    for key in ("succession_trained_chara_data", "event_succession_trained_chara_data"):
        block = data.setdefault(key, {})
        block["succession_trained_chara_array"] = [
            {**trained_chara._load_starter_trained_chara(e["viewer_id"])[0]}
            for e in injected]
        block["summary_user_info_array"] = copy.deepcopy(injected)
    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    fsc = data.setdefault("friend_support_card_data", {})
    fsc["summary_user_info_array"] = copy.deepcopy(injected)
    fsc["support_card_data_array"] = [copy.deepcopy(e["user_support_card"]) for e in injected]

    genuine = [c for c in trained_chara._get_or_seed_roster(viewer_id)
              if trained_chara.PLAYER_CAREER_ID_BASE
                 <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]
    best_by_scenario: dict = {}
    for c in genuine:
        sid = c.get("scenario_id")
        score = c.get("rank_score") or 0
        if sid and score > best_by_scenario.get(sid, 0):
            best_by_scenario[sid] = score
    data["scenario_record_highest_score_array"] = [
        {"scenario_id": sid, "highest_rank_score": score}
        for sid, score in sorted(best_by_scenario.items())]
    if _grant_scenario_record_rewards(full_state, best_by_scenario):
        state_store.save_state(viewer_id, full_state)
    return base


def handle_friend_support_card_reload(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    _ensure_default_friend(viewer_id, full_state)
    full_state = state_store.get_state(viewer_id) or full_state

    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    excluded = {str(v) for v in (payload.get("exclude_viewer_id_array") or [])}
    remaining = [e for e in injected if str(e.get("viewer_id")) not in excluded]
    if not remaining and injected:
        log.info("friend_support_card_reload: every lender (%s) already shown -- "
                 "re-offering the whole pool rather than an empty Borrow screen",
                 len(injected))
        remaining = injected

    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "summary_user_info_array": copy.deepcopy(remaining),
                "support_card_data_array": [copy.deepcopy(e["user_support_card"])
                                            for e in remaining
                                            if e.get("user_support_card")],
            }}


SCENARIO_RECORD_GRANTED_KEY = "scenario_record_granted"


def _grant_scenario_record_rewards(full_state: dict, best_by_scenario: dict) -> bool:
    granted = full_state.setdefault(SCENARIO_RECORD_GRANTED_KEY, {})
    mailed = False
    for scenario_id, best in best_by_scenario.items():
        watermark = granted.get(str(scenario_id)) or 0
        rows = master_data.query(
            "SELECT need_record_min, reward_item_category, reward_item_id, reward_num "
            "FROM single_mode_scenario_record WHERE scenario_id=? AND need_record_min<=? "
            "AND need_record_min>? ORDER BY need_record_min",
            (scenario_id, best, watermark))
        for row in rows:
            if row["reward_num"]:
                presents.send(full_state, row["reward_item_category"],
                              row["reward_item_id"], row["reward_num"],
                              message_id=50)
                mailed = True
            granted[str(scenario_id)] = row["need_record_min"]
    return mailed


_FRIEND_TEMPLATE_TRAINED_CHARA = {
    "card_id": 103201, "rank_score": 15351, "rank": 15,
    "proper_ground_turf": 7, "proper_ground_dirt": 2,
    "proper_running_style_nige": 3, "proper_running_style_senko": 7,
    "proper_running_style_sashi": 7, "proper_running_style_oikomi": 2,
    "proper_distance_short": 1, "proper_distance_mile": 4,
    "proper_distance_middle": 7, "proper_distance_long": 6,
    "rarity": 4, "talent_level": 5, "factor_info_array": [], "factor_extend_array": [],
    "skill_count": 23,
}
_FRIEND_SENTINEL_VIEWER_BASE = 900_000_500_000
_FRIEND_CIRCLE_ID = 900_000_500

_DEFAULT_FRIEND_CARD_ID = 30052
_DEFAULT_FRIEND_LEVEL = 50
_DEFAULT_FRIEND_LIMIT_BREAK = 4

_DEFAULT_FRIEND_CARDS = [
    (_DEFAULT_FRIEND_CARD_ID, "Light Hello"),
    (30010, "Fine Motion"),
    (30036, "Riko Kashimoto"),
    (30028, "Kitasan Black"),
    (30067, "Heirs to the Throne"),
    (30081, "Team Sirius"),
]


def _ensure_default_friend(viewer_id, full_state: dict) -> None:
    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    have = {e.get("support_card_id") for e in injected}
    for card_id, name in _DEFAULT_FRIEND_CARDS:
        if card_id in have:
            continue
        inject_friend_support_card(viewer_id, card_id,
                                   level=_DEFAULT_FRIEND_LEVEL,
                                   limit_break_count=_DEFAULT_FRIEND_LIMIT_BREAK,
                                   name=name)


def inject_friend_support_card(viewer_id, support_card_id: int, level: int = None,
                               limit_break_count: int = None, name: str = "Friend") -> dict:
    row = master_data.query_one("SELECT rarity FROM support_card_data WHERE id=?",
                                (support_card_id,))
    if row is None:
        raise ValueError(f"no support_card_data row for id {support_card_id}")
    rarity = row["rarity"]
    if level is None:
        top = master_data.query_one(
            "SELECT MAX(level) AS lv FROM support_card_level WHERE rarity=?", (rarity,))
        level = (top["lv"] if top else None) or 1
    exp_row = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (rarity, level))
    if exp_row is None:
        raise ValueError(f"no support_card_level row for rarity {rarity} level {level}")
    limit_break_count = 4 if limit_break_count is None else limit_break_count

    _TEMPLATE_CHARA_ID = 1032
    _TEMPLATE_DRESS_ID = 103201
    _FRIEND_TRAINED_CHARA_ID_BASE = 9001

    full_state = state_store.get_state(viewer_id) or {}
    injected = full_state.setdefault(FRIEND_SUPPORT_INJECT_KEY, [])
    friend_viewer_id = _FRIEND_SENTINEL_VIEWER_BASE + len(injected)
    friend_trained_chara_id = _FRIEND_TRAINED_CHARA_ID_BASE + len(injected)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "viewer_id": friend_viewer_id, "name": name,
        "honor_id": 100101, "honor_data": {"honor_id": 100101},
        "last_login_time": now,
        "leader_chara_id": _TEMPLATE_CHARA_ID, "leader_chara_dress_id": _TEMPLATE_DRESS_ID,
        "support_card_id": support_card_id, "partner_chara_id": _TEMPLATE_CHARA_ID,
        "comment": "", "fan": 1, "directory_level": 1, "rank_score": 1,
        "team_stadium_win_count": 0, "single_mode_play_count": 1,
        "team_evaluation_point": 0, "best_team_evaluation_point": 0,
        "friend_state": 1, "state": 1,
        "circle_info": {"circle_id": _FRIEND_CIRCLE_ID, "name": "Friends"},
        "circle_user": {"viewer_id": friend_viewer_id, "circle_id": _FRIEND_CIRCLE_ID,
                        "membership": 1, "join_time": now, "penalty_end_time": now,
                        "item_request_end_time": now, "last_check_post_id": 0,
                        "ranking_result_check_time": now},
        "user_support_card": {"viewer_id": friend_viewer_id, "support_card_id": support_card_id,
                              "exp": exp_row["total_exp"], "limit_break_count": limit_break_count,
                              "favorite_flag": 0, "stock": 0,
                              "possess_time": now, "create_time": now},
        "user_trained_chara": {**_FRIEND_TEMPLATE_TRAINED_CHARA,
                               "viewer_id": friend_viewer_id,
                               "trained_chara_id": friend_trained_chara_id,
                               "register_time": now},
    }
    injected.append(entry)
    state_store.save_state(viewer_id, full_state)
    return entry


def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    start_chara = payload.get("start_chara")
    if STATE_KEY in full_state:
        persisted_start = full_state.get(START_CHARA_STATE_KEY) or {}
        same_selection = (not start_chara) or (
            start_chara.get("card_id") == persisted_start.get("card_id"))
        if same_selection:
            return full_state[STATE_KEY]
        log.info("single_mode/start: viewer %s sent a new start_chara (card %s) "
                 "over a stale persisted career (card %s, turn %s) -- clearing "
                 "and reseeding instead of serving the stale one back",
                 viewer_id, start_chara.get("card_id"),
                 persisted_start.get("card_id"),
                 full_state[STATE_KEY].get("data", {}).get("chara_info", {}).get("turn"))
        clear_active_career(viewer_id)
        full_state = state_store.get_state(viewer_id) or {}

    if start_chara and start_chara.get("card_id"):
        from . import factor_research, training_challenge
        research_error = factor_research.validate_start(start_chara)
        if research_error is not None:
            return research_error
        event_error = training_challenge.validate_start(full_state, start_chara)
        if event_error is not None:
            return event_error
        roster = _roster_by_trained_id(viewer_id)
        for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
            pid = start_chara.get(key)
            if pid and pid not in roster:
                log.warning("single_mode/start refused: viewer %s selected parent "
                           "%s=%s, not in this account's roster (stale client cache?)",
                           viewer_id, key, pid)
                return refuse()
        banned = _banned_deck_cards(start_chara)
        if banned:
            log.warning("single_mode/start refused: viewer %s brought %s into "
                        "scenario %s, barred by single_mode_restrict_support",
                        viewer_id, sorted(banned), start_chara.get("scenario_id"))
            return refuse()
        if "use_tp" in payload:
            full_state = state_store.get_state(viewer_id) or {}
            if not stamina.spend_career_tp(viewer_id, full_state, start_chara,
                                           payload.get("use_tp")):
                return refuse()
            state_store.save_state(viewer_id, full_state)
        full_state = state_store.get_state(viewer_id) or {}
        if any(full_state.pop(k, None) is not None for k in (_TURN_SERVED_KEY, _TURN_HOLD_RELEASED_KEY)):
            state_store.save_state(viewer_id, full_state)
        response = _seed_dynamic_career(viewer_id, start_chara)
        full_state = state_store.get_state(viewer_id) or {}
        training_challenge.bind_start(full_state, start_chara)
        from . import story_event
        if master_data.has_table("story_event_data"):
            story_id = story_event._active_event_id(patch._servertime())
            if story_id is not None:
                story_event.event_missions(viewer_id, full_state, story_id)
        state_store.save_state(viewer_id, full_state)
        return response
    return _seed_fresh_career(viewer_id, start_chara)


def _banned_deck_cards(start_chara: dict) -> set:
    if not isinstance(start_chara, dict):
        return set()
    barred = scenarios.restricted_support_cards(start_chara.get("scenario_id"))
    if not barred:
        return set()
    chosen = {int(c) for c in (start_chara.get("support_card_ids") or ()) if c}
    friend = (start_chara.get("friend_support_card_info") or {}).get("support_card_id")
    if friend:
        chosen.add(int(friend))
    return chosen & set(barred)


def sync_chara_info(viewer_id, endpoint: str, response: dict) -> dict:
    if isinstance(endpoint, str) and endpoint.startswith(_career_prefixes()):
        try:
            _repair_unique_skill(viewer_id)
        except Exception:
            log.exception("unique skill repair failed; serving as-is")
        try:
            _repair_storyless_events(viewer_id, response)
        except Exception:
            log.exception("storyless event repair failed; serving as-is")
    response = _sync_chara_info(viewer_id, endpoint, response)
    if isinstance(endpoint, str) and endpoint.startswith(_career_prefixes()):
        try:
            _apply_short_cut_state(viewer_id, response)
        except Exception:
            log.exception("short_cut_state apply failed; serving as-is")
        try:
            _hold_facility_levels(viewer_id, endpoint, response)
        except Exception:
            log.exception("facility level hold failed; serving new levels")
        try:
            _prune_roster(viewer_id, response)
        except Exception:
            log.exception("roster prune failed; serving unpruned")
        try:
            _carry_locked_npc_rows(response)
        except Exception:
            log.exception("locked NPC row carry failed; serving without them")
        try:
            _attach_scenario_data_set(viewer_id, response, endpoint)
        except Exception:
            log.exception("scenario data_set attach failed; serving without it")
        _warn_foreign_chara(viewer_id, endpoint, response)
        try:
            record_gate_stories(viewer_id, response)
        except Exception:
            log.exception("gate story record failed; continuing")
        try:
            _stamp_special_home(response, viewer_id)
        except Exception:
            log.exception("special home stamp failed; serving unstamped")
        try:
            _stamp_scheduled_conditions(response)
        except Exception:
            log.exception("scheduled condition stamp failed; serving unstamped")
        try:
            career_events.stamp_response_timing(
                (response or {}).get("data"), endpoint)
        except Exception:
            log.exception("response timing stamp failed; serving unstamped")
        try:
            career_events.stamp_multi_chara_flag(
                (response or {}).get("data"), endpoint)
        except Exception:
            log.exception("multi-chara flag stamp failed; serving unstamped")
        try:
            _trim_start_envelope(endpoint, response)
        except Exception:
            log.exception("start envelope trim failed; serving untrimmed")
        try:
            _trim_opponent_envelope(endpoint, response)
        except Exception:
            log.exception("opponent envelope trim failed; serving untrimmed")
    return response


def _carry_locked_npc_rows(response: dict) -> None:
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    chara_info = data.get("chara_info")
    if not isinstance(chara_info, dict):
        return
    rows = scenarios.for_chara(chara_info).locked_npc_rows
    if not rows:
        return
    arr = chara_info.setdefault("evaluation_info_array", [])
    present = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for target_id, _chara_id in rows:
        if target_id in present:
            continue
        arr.append({"target_id": target_id, "training_partner_id": target_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 0, "group_outing_info_array": []})


def _trim_opponent_envelope(endpoint: str, response: dict) -> None:
    if not isinstance(endpoint, str) or not endpoint.endswith("/opponent_list"):
        return
    data = (response or {}).get("data")
    if isinstance(data, dict) and "team_data_set" in data:
        data.pop("chara_info", None)


_START_NESTED_ONLY_FIELDS = (
    "chara_info", "home_info", "tp_info", "unchecked_event_array",
    "race_condition_array", "user_item_array", "add_trained_chara_array",
    "mission_list", "story_event_chara_bonus_list", "story_event_mission_list",
    "race_history", "race_random_program_array", "reserved_race_array",
    "facility_levels",
)


def _trim_start_envelope(endpoint: str, response: dict) -> None:
    if not isinstance(endpoint, str) or not endpoint.endswith("/start"):
        return
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    smsc = data.get("single_mode_start_common")
    if not isinstance(smsc, dict):
        return
    for key in _START_NESTED_ONLY_FIELDS:
        if key in data:
            if key in smsc:
                smsc[key] = data[key]
            data.pop(key, None)


def _warn_foreign_chara(viewer_id, endpoint: str, response: dict) -> None:
    try:
        card = ((response or {}).get("data") or {}).get("chara_info", {}).get("card_id")
        career = (state_store.get_state(viewer_id) or {}).get(STATE_KEY)
        if not card or not isinstance(career, dict):
            return
        mine = (career.get("data") or {}).get("chara_info", {}).get("card_id")
        if mine and card != mine:
            log.error("FOREIGN CAREER on %s: serving card_id %s but this viewer's "
                      "career is card_id %s -- unpatched capture leaking through",
                      endpoint, card, mine)
    except Exception:
        log.exception("foreign-chara tripwire failed")


@functools.lru_cache(maxsize=1)
def _career_prefixes() -> tuple:
    return tuple(sorted({"single_mode/", "single_mode_team/"} | {
        f"{scen.endpoint_prefix}/" for scen in scenarios.all_scenarios()}))


_SCENARIO_DATA_SET_KEYS = ("ura_data_set", "team_data_set", "live_data_set",
                           "free_data_set", "ramen_data_set", "onsen_data_set")
_NESTED_COMMON_KEYS = ("single_mode_start_common", "single_mode_load_common")


def _scrub_foreign_data_sets(data: dict, keep: str) -> None:
    for holder in (data, *(data.get(k) for k in _NESTED_COMMON_KEYS)):
        if not isinstance(holder, dict):
            continue
        for key in _SCENARIO_DATA_SET_KEYS:
            if key != keep and key in holder:
                del holder[key]


def _attach_scenario_data_set(viewer_id, response: dict, endpoint: str = "") -> None:
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    chara_info = data.get("chara_info")
    scen = scenarios.for_chara(chara_info)
    full_state = state_store.get_state(viewer_id) or {}
    if not isinstance(chara_info, dict):
        career_ci = ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("chara_info")
        career_scen = scenarios.for_chara(career_ci)
        if (isinstance(career_ci, dict) and career_scen.data_set_on_race_start
                and endpoint == f"{career_scen.endpoint_prefix}/race_start"):
            career_scen.attach(response, full_state, copy.deepcopy(career_ci), None,
                               endpoint=endpoint)
            _scrub_foreign_data_sets(data, career_scen.data_set_key)
            return
    home = data.get("home_info")
    if not isinstance(home, dict):
        home = ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
    settled = scen.settle(full_state, chara_info, endpoint)
    command_info, rolled = scen.command_info(full_state, chara_info,
                                             home.get("command_info_array"))
    scen.attach(response, full_state, chara_info, command_info, endpoint=endpoint)
    _scrub_foreign_data_sets(data, scen.data_set_key)
    held = scen.held_turn(full_state)
    if not held:
        before = full_state.get(_TURN_HOLD_RELEASED_KEY)
        held = _turn_end_hold(data, full_state, scen.turn_end_timings)
        if full_state.get(_TURN_HOLD_RELEASED_KEY) != before:
            state_store.save_state(viewer_id, full_state)
    ci_hold = data.get("chara_info")
    events_now = data.get("unchecked_event_array") or []
    head_now = events_now[0] if events_now and isinstance(events_now[0], dict) else None
    if (scen.turn_end_hold_state and isinstance(ci_hold, dict) and head_now is not None
            and int(ci_hold.get("playing_state") or 1) == 1
            and int(head_now.get("play_timing") or 0) not in _TURN_START_TIMINGS):
        ci_hold["playing_state"] = scen.turn_end_hold_state
    if isinstance(data.get("chara_info"), dict):
        ci_turn = data["chara_info"]
        run_turn = int(ci_turn.get("turn") or 0)
        served, moved = _served_turn(full_state, ci_turn, held or run_turn)
        ci_turn["turn"] = served
        if moved:
            state_store.save_state(viewer_id, full_state)
        if "race_condition_array" in data:
            data["race_condition_array"] = _build_race_condition_array(
                ci_turn, served,
                ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("race_history", []))
            smlc = data.get("single_mode_load_common")
            if isinstance(smlc, dict) and "race_condition_array" in smlc:
                smlc["race_condition_array"] = copy.deepcopy(data["race_condition_array"])
    if rolled or settled:
        state_store.save_state(viewer_id, full_state)


_TURN_END_TIMINGS = (3, 6, 10)
_TURN_START_TIMINGS = (1, 11)
_TURN_HOLD_RELEASED_KEY = "turn_end_hold_released"


_TURN_SERVED_KEY = "turn_served_high"
_TURN_REWIND_SLACK = 2


def _served_turn(full_state: dict, chara_info: dict, served: int) -> tuple:
    served = int(served or 0)
    if served <= 0:
        return served, False
    career = str(chara_info.get("start_time") or "")
    mark = full_state.get(_TURN_SERVED_KEY)
    if not isinstance(mark, dict) or mark.get("career") != career:
        mark = {"career": career, "turn": 0}
    high = int(mark.get("turn") or 0)
    if served < high:
        if high - served <= _TURN_REWIND_SLACK:
            return high, False
    elif served == high:
        return served, False
    mark["turn"] = served
    full_state[_TURN_SERVED_KEY] = mark
    return served, True


def _turn_end_hold(data: dict, full_state: dict, timings=_TURN_END_TIMINGS) -> int:
    chara_info = data.get("chara_info")
    if not isinstance(chara_info, dict):
        return 0
    turn = int(chara_info.get("turn") or 0)
    if turn <= 1:
        return 0
    events = data.get("unchecked_event_array") or []
    head = events[0] if events and isinstance(events[0], dict) else {}
    if int(head.get("play_timing") or 0) in timings:
        if int(full_state.get(_TURN_HOLD_RELEASED_KEY) or 0) == turn:
            return 0
        return turn - 1
    if int(full_state.get(_TURN_HOLD_RELEASED_KEY) or 0) != turn:
        full_state[_TURN_HOLD_RELEASED_KEY] = turn
    return 0


def _client_turn_conflict(payload: dict, full_state: dict, chara_info: dict,
                          allow_played_turn: bool = True):
    claimed = payload.get("current_turn")
    if claimed is None or not isinstance(chara_info, dict):
        return None
    try:
        claimed = int(claimed)
        turn = int(chara_info.get("turn") or 0)
    except (TypeError, ValueError):
        return None
    if claimed == turn or (allow_played_turn and claimed == turn - 1):
        return None
    try:
        if claimed == int(scenarios.for_chara(chara_info).held_turn(full_state) or 0):
            return None
    except Exception:
        log.exception("held_turn check failed -- treating client turn %s as a "
                      "conflict against run turn %s", claimed, turn)
    return claimed


def _stale_turn_refusal(viewer_id, where: str, claimed, chara_info: dict) -> dict:
    log.warning("%s: viewer %s claims turn %s but the run is on turn %s -- "
                "REFUSING (stale client career cache; reload the career). "
                "Persisted turn left untouched.",
                where, viewer_id, claimed, (chara_info or {}).get("turn"))
    return refuse()


def _friendship_bonus(full_state: dict, chara_info: dict) -> int:
    return scenarios.for_chara(chara_info).friendship_bonus_pct(full_state)


def _specialty_bonus(full_state: dict, chara_info: dict) -> int:
    return scenarios.for_chara(chara_info).specialty_bonus(full_state)


def _training_bonus(full_state: dict, chara_info: dict):
    return scenarios.for_chara(chara_info).training_bonus(full_state)


def _sync_chara_info(viewer_id, endpoint: str, response: dict) -> dict:
    if not endpoint.startswith(_career_prefixes()):
        return response

    if endpoint.rsplit("/", 1)[-1] in ("race_entry", "race_start", "race_end", "race_out",
                                       "check_event", "minigame_end", "continue",
                                       "master_square", "live_start"):
        return response

    full_state = state_store.get_state(viewer_id)
    if not full_state or STATE_KEY not in full_state:
        return response

    persisted_data = full_state[STATE_KEY].get("data", {})
    data = response.get("data")
    if isinstance(data, dict):
        for key in data:
            if key == "unchecked_event_array":
                continue
            if key in persisted_data:
                data[key] = copy.deepcopy(persisted_data[key])

        if "home_info" in data and "ura_data_set" not in data:
            persisted_uds = persisted_data.get("ura_data_set")
            if persisted_uds is not None:
                data["ura_data_set"] = copy.deepcopy(persisted_uds)

        if isinstance(data.get("home_info"), dict):
            counts = _continue_counts(full_state, full_state.get(RACE_CTX_KEY))
            data["home_info"].update(counts)
            if isinstance(persisted_data.get("home_info"), dict):
                persisted_data["home_info"].update(counts)

        _apply_versus(data, full_state, (data.get("chara_info") or {}).get("turn"))

        if "race_condition_array" in data and "chara_info" in data:
            data["race_condition_array"] = _build_race_condition_array(
                data["chara_info"], data["chara_info"].get("turn"),
                persisted_data.get("race_history", []))

        if "mission_list" in data:
            data["mission_list"] = missions.career_mission_list(viewer_id, full_state)

        if "story_event_mission_list" in data:
            data["story_event_mission_list"] = []

        if "race_history" in data:
            data["race_history"] = _wire_race_history(data["race_history"])

        if "reserved_race_array" in data:
            data["reserved_race_array"] = _build_reserved_race_array(
                data["reserved_race_array"], full_state)
        for holder in (data, data.get("single_mode_start_common"),
                       data.get("single_mode_load_common")):
            if isinstance(holder, dict) and isinstance(holder.get("reserved_race_info"), dict):
                holder["reserved_race_info"] = _build_reserved_race_info(
                    holder["reserved_race_info"], full_state)

        smlc = data.get("single_mode_load_common")
        if isinstance(smlc, dict):
            if "chara_info" in smlc and "chara_info" in data:
                smlc["chara_info"] = copy.deepcopy(data["chara_info"])
            if "home_info" in smlc and "home_info" in data:
                smlc["home_info"] = copy.deepcopy(data["home_info"])
            if "race_history" in smlc and "race_history" in data:
                smlc["race_history"] = copy.deepcopy(data["race_history"])
            if "race_condition_array" in smlc and "race_condition_array" in data:
                smlc["race_condition_array"] = copy.deepcopy(data["race_condition_array"])
            if "reserved_race_array" in smlc and "reserved_race_array" in data:
                smlc["reserved_race_array"] = copy.deepcopy(data["reserved_race_array"])
            if "mission_list" in smlc and "mission_list" in data:
                smlc["mission_list"] = copy.deepcopy(data["mission_list"])
            if "story_event_mission_list" in smlc:
                smlc["story_event_mission_list"] = []

        _resume_race_on_load(endpoint, data, full_state)

    return response


_RACE_PLAYING_STATES = (2, 3, 4)


def _resume_race_on_load(endpoint: str, data: dict, full_state: dict) -> None:
    if (endpoint or "").rsplit("/", 1)[-1] != "load":
        return
    ci = data.get("chara_info")
    if not isinstance(ci, dict):
        return
    ctx = full_state.get(RACE_CTX_KEY) or {}
    rsi = ctx.get("race_start_info")
    resumable = bool(rsi) and ctx.get("turn") == ci.get("turn")
    stranded = int(ci.get("playing_state") or 0) in _RACE_PLAYING_STATES
    if not resumable and not stranded:
        return

    smlc = data.get("single_mode_load_common")
    holders = [h for h in (data, smlc) if isinstance(h, dict)]
    for holder in holders:
        hci = holder.get("chara_info")
        if not isinstance(hci, dict):
            continue
        if resumable:
            hci["playing_state"] = 2
            hci["state"] = 0
            hci["race_program_id"] = ctx.get("program_id")
        else:
            hci["playing_state"] = 1
            hci["race_program_id"] = 0
    if not resumable:
        log.info("load: career carried race playing_state with no race context "
                 "to resume (turn %s) -- reset to the training screen",
                 ci.get("turn"))
        return

    for holder in holders:
        if holder is smlc or "race_start_info" in holder:
            holder["race_start_info"] = copy.deepcopy(rsi)
        if ctx.get("race_scenario") and (holder is smlc or "race_scenario" in holder):
            holder["race_scenario"] = ctx["race_scenario"]
        if "unchecked_event_array" in holder:
            holder["unchecked_event_array"] = []
    log.info("load: resuming in-flight race program %s (turn %s) instead of "
             "the training screen", ctx.get("program_id"), ctx.get("turn"))


def _prune_roster(viewer_id, response) -> None:
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    chara_info = career.get("data", {}).get("chara_info") or {}
    deck = {}
    for c in chara_info.get("support_card_array") or ():
        pos, sid = c.get("position"), c.get("support_card_id")
        if pos:
            deck[pos] = event_engine.card_chara_id(sid) or 0
    allowed = set(deck)
    for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []) or ():
        allowed.add(n[0] if isinstance(n, (list, tuple)) else n)
    allowed.update(_npc_supporters(full_state, chara_info))
    if not allowed:
        return

    def prune(container):
        arr = container.get("evaluation_info_array")
        if not isinstance(arr, list):
            return
        kept = []
        for e in arr:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            tid = e.get("target_id")
            if tid not in allowed:
                continue
            if tid in deck and "chara_id" in e and e["chara_id"] != deck[tid]:
                e = dict(e, chara_id=deck[tid])
            kept.append(e)
        container["evaluation_info_array"] = kept

    for key in ("ura_data_set", "chara_info"):
        blob = data.get(key)
        if isinstance(blob, dict):
            prune(blob)
    smlc = data.get("single_mode_load_common")
    if isinstance(smlc, dict):
        for key in ("ura_data_set", "chara_info"):
            blob = smlc.get(key)
            if isinstance(blob, dict):
                prune(blob)


def handle_load(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id)
    if full_state is None or STATE_KEY not in full_state:
        career_state = _seed_fresh_career(viewer_id, None)
        _force_seeded_scenario(viewer_id, career_state, _endpoint_scenario_id())

    pair = fixtures.first("single_mode/load") or fixtures.first("single_mode_team/load")
    if pair is None:
        raise LookupError("No captured single_mode/load fixture available")
    response = pair.response_copy()
    pending_display = (state_store.get_state(viewer_id) or {}).get(DISPLAY_EVENT_KEY) or []
    response["data"]["unchecked_event_array"] = copy.deepcopy(pending_display)
    smlc = response["data"].get("single_mode_load_common")
    if isinstance(smlc, dict) and "unchecked_event_array" in smlc:
        smlc["unchecked_event_array"] = copy.deepcopy(pending_display)
    return response


DISPLAY_EVENT_KEY = "on_screen_event"
GOAL_EVENT_KEY = "pending_goal_event"


def _served_branch(full_state: dict, event_id, choice_number):
    for entry in full_state.get(DISPLAY_EVENT_KEY) or ():
        if not isinstance(entry, dict) or entry.get("event_id") != event_id:
            continue
        arr = (entry.get("event_contents_info") or {}).get("choice_array") or []
        i = max(1, int(choice_number or 1)) - 1
        if i >= len(arr):
            i = 0
        if i < len(arr) and isinstance(arr[i], dict):
            return arr[i].get("select_index")
    return None


def _remember_display(full_state: dict, response: dict) -> None:
    try:
        arr = (response.get("data") or {}).get("unchecked_event_array")
    except AttributeError:
        return
    if arr:
        full_state[DISPLAY_EVENT_KEY] = copy.deepcopy(arr)
    else:
        full_state.pop(DISPLAY_EVENT_KEY, None)


RACE_CTX_KEY = "active_race_ctx"
STYLE_CHOICE_KEY = "career_style_choice"


def _best_running_style(chara_info: dict) -> int:
    apts = {1: chara_info.get("proper_running_style_nige", 1),
            2: chara_info.get("proper_running_style_senko", 1),
            3: chara_info.get("proper_running_style_sashi", 1),
            4: chara_info.get("proper_running_style_oikomi", 1)}
    return max(apts, key=lambda k: (apts[k], -k))


def _player_running_style(full_state: dict, chara_info: dict) -> int:
    choice = (full_state or {}).get(STYLE_CHOICE_KEY)
    return choice if choice in (1, 2, 3, 4) else _best_running_style(chara_info)


def _career_chara_info(viewer_id) -> dict | None:
    full = state_store.get_state(viewer_id) or {}
    career = full.get(STATE_KEY)
    return career.get("data", {}).get("chara_info") if isinstance(career, dict) else None


def _patch_player_horse(race_start_info: dict, chara_info: dict,
                        running_style: int | None = None) -> None:
    if not chara_info:
        return
    rhd = (race_start_info or {}).get("race_horse_data")
    if not isinstance(rhd, list) or not rhd or not isinstance(rhd[0], dict):
        return
    h = rhd[0]
    card_id = chara_info.get("card_id")
    h["single_mode_chara_id"] = chara_info.get("single_mode_chara_id", h.get("single_mode_chara_id"))
    h["trained_chara_id"] = 0
    if card_id:
        h["card_id"] = card_id
        h["chara_id"] = card_id // 100
        h["race_dress_id"] = trained_chara.dress_for_card(card_id, card_id // 100)
    h["rarity"] = chara_info.get("rarity", h.get("rarity"))
    h["talent_level"] = chara_info.get("talent_level", h.get("talent_level"))
    h["skill_array"] = copy.deepcopy(chara_info.get("skill_array", []))
    h["speed"] = chara_info.get("speed", 0)
    h["stamina"] = chara_info.get("stamina", 0)
    h["pow"] = chara_info.get("power", 0)
    h["guts"] = chara_info.get("guts", 0)
    h["wiz"] = chara_info.get("wiz", 0)
    h["motivation"] = chara_info.get("motivation", h.get("motivation"))
    h["fan_count"] = chara_info.get("fans", h.get("fan_count"))
    h["running_style"] = running_style or _best_running_style(chara_info)
    for apt in (
        "proper_distance_short", "proper_distance_mile", "proper_distance_middle",
        "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
        "proper_running_style_sashi", "proper_running_style_oikomi",
        "proper_ground_turf", "proper_ground_dirt",
    ):
        if apt in chara_info:
            h[apt] = chara_info[apt]


_DEBUT_GRADE = 900


def _race_info_for_program(program_id: int) -> dict | None:
    program = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (program_id,))
    if not program:
        return None
    instance_id = program["race_instance_id"]
    inst = master_data.query_one("SELECT race_id FROM race_instance WHERE id=?", (instance_id,))
    if not inst:
        return None
    race_id = inst["race_id"]
    race_row = master_data.query_one("SELECT grade FROM race WHERE id=?", (race_id,))
    if race_row and race_row["grade"] == _DEBUT_GRADE:
        story_name = "Debut"
    else:
        name_row = master_data.query_one(
            "SELECT text FROM text_data WHERE category=28 AND [index]=?", (instance_id,))
        story_name = name_row["text"] if name_row else None
    return {"program_id": program_id, "race_instance_id": instance_id,
            "race_id": race_id, "story_name": story_name}


_RACE_CATEGORIES = ("short", "mile", "middle", "long")


def _race_category(distance: int) -> str:
    if distance <= 1400:
        return "short"
    if distance <= 1800:
        return "mile"
    if distance <= 2400:
        return "middle"
    return "long"


@functools.lru_cache(maxsize=4096)
def _program_course(program_id) -> tuple | None:
    row = master_data.query_one(
        "SELECT cs.distance AS distance, cs.ground AS ground FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE p.id=?", (program_id,))
    return (row["ground"], _race_category(row["distance"])) if row else None


@functools.lru_cache(maxsize=64)
def _branch_arms(race_set_id: int) -> tuple:
    return tuple(
        {"id": r["id"], "sort_id": r["sort_id"], "inst": r["inst"]}
        for r in master_data.query(
            "SELECT rr.id AS id, rr.sort_id AS sort_id, p.race_instance_id AS inst "
            "FROM single_mode_route_race rr "
            "JOIN single_mode_program p ON p.id = rr.condition_id "
            "WHERE rr.race_set_id=? AND rr.determine_race != 0", (race_set_id,)))


def _race_set_of(route_race_id_array) -> int | None:
    ids = [int(i) for i in (route_race_id_array or ())]
    if not ids:
        return None
    row = master_data.query_one(
        f"SELECT race_set_id FROM single_mode_route_race "
        f"WHERE id IN ({','.join(map(str, ids))}) LIMIT 1")
    return row["race_set_id"] if row else None


def route_race_id_for_instance(chara_info: dict, race_instance_id: int) -> int:
    rs = _race_set_of(chara_info.get("route_race_id_array"))
    if rs is None or not race_instance_id:
        return 0
    arm = next((a for a in _branch_arms(rs) if a["inst"] == race_instance_id), None)
    return arm["id"] if arm else 0


def refresh_route_screen(full_state: dict | None, chara_info: dict) -> bool:
    if not isinstance(full_state, dict):
        return False
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return False
    data = career.get("data") or {}
    home = data.get("home_info")
    if not isinstance(home, dict):
        return False
    ci = data.get("chara_info")
    if isinstance(ci, dict) and ci is not chara_info:
        ci["route_race_id_array"] = list(chara_info.get("route_race_id_array") or ())
        if chara_info.get(RACE_RESTRICT_UNTIL_KEY):
            ci[RACE_RESTRICT_UNTIL_KEY] = chara_info[RACE_RESTRICT_UNTIL_KEY]
    else:
        ci = chara_info
    _refresh_command_info(
        ci, home, turn=ci.get("turn", 1),
        unlocked_npcs=[n[0] for n in (full_state.get(
            single_mode_events.UNLOCKED_NPCS_KEY) or [])],
        facility_levels=_facility_levels(data),
        race_history=data.get("race_history", []),
        training_bonus=_training_bonus(full_state, ci),
        friendship_bonus=_friendship_bonus(full_state, ci),
        specialty_bonus=_specialty_bonus(full_state, ci),
        support_card_levels=_support_card_levels(full_state),
        friendship_stacks=_friendship_stacks(full_state),
        full_state=full_state)
    return True


def cancel_route_race(chara_info: dict, race_instance_id: int) -> bool:
    ids = list(chara_info.get("route_race_id_array") or ())
    race_set_id = _race_set_of(ids)
    if race_set_id is None or not race_instance_id:
        return False
    target = next((a for a in _branch_arms(race_set_id)
                   if a["inst"] == race_instance_id), None)
    if target is None or target["id"] not in ids:
        return False
    chara_info["route_race_id_array"] = [i for i in ids if i != target["id"]]
    log.info("route goal cancelled: sort_id %s -> route_race %s (race_instance %s)",
             target["sort_id"], target["id"], race_instance_id)
    return True


def restrict_racing(chara_info: dict, turns: int) -> bool:
    try:
        turns = int(turns)
    except (TypeError, ValueError):
        return False
    if turns <= 0:
        return False
    turn = chara_info.get("turn") or 0
    until = max(int(chara_info.get(RACE_RESTRICT_UNTIL_KEY) or 0), turn + turns)
    chara_info[RACE_RESTRICT_UNTIL_KEY] = until
    log.info("racing restricted for %s turn(s), reopens on turn %s", turns, until)
    return True


def switch_route_race(chara_info: dict, race_instance_id: int,
                      route_race_id: int | None = None) -> bool:
    ids = list(chara_info.get("route_race_id_array") or ())
    if not ids or not (race_instance_id or route_race_id):
        return False
    race_set_id = _race_set_of(ids)
    if race_set_id is None:
        return False
    row = {"race_set_id": race_set_id}
    arms = _branch_arms(row["race_set_id"])
    target = (next((r for r in arms if r["id"] == route_race_id), None) if route_race_id
              else next((r for r in arms if r["inst"] == race_instance_id), None))
    if target is None:
        log.warning("route branch: race_instance %s is not an arm of any branch "
                    "group on race set %s", race_instance_id, row["race_set_id"])
        return False
    if target["id"] in ids:
        return False
    siblings = {r["id"] for r in arms if r["sort_id"] == target["sort_id"]}
    ids = [i for i in ids if i not in siblings]
    ids.append(target["id"])
    chara_info["route_race_id_array"] = sorted(ids)
    log.info("route branch taken: sort_id %s -> route_race %s (race_instance %s)",
             target["sort_id"], target["id"], race_instance_id)
    return True


def _turn_slot(turn):
    if turn is None:
        return None
    return master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))


@functools.lru_cache(maxsize=64)
def _route_goal_rows(route_race_id_array: tuple) -> tuple:
    if not route_race_id_array:
        return ()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, turn, sort_id, condition_id, condition_value_1, determine_race "
        f"FROM single_mode_route_race "
        f"WHERE id IN ({ids}) AND condition_type IN (1, 9) AND target_type IN (1, 3) "
        f"ORDER BY id")
    dropped = _dropped_alternative_ids(route_race_id_array)
    out = []
    for r in rows:
        cid = r["condition_id"]
        if not cid:
            continue
        if r["id"] in dropped:
            continue
        if master_data.query_one("SELECT id FROM single_mode_program WHERE id=?", (cid,)):
            kind = "program"
        elif master_data.query_one(
                "SELECT id FROM single_mode_race_group WHERE race_group_id=?", (cid,)):
            kind = "group"
        else:
            continue
        out.append((r["turn"], cid, kind, r["condition_value_1"]))
    return tuple(out)


def _best_ground_category(chara_info: dict, race_history) -> tuple:
    apt = {"short": chara_info.get("proper_distance_short", 1),
           "mile": chara_info.get("proper_distance_mile", 1),
           "middle": chara_info.get("proper_distance_middle", 1),
           "long": chara_info.get("proper_distance_long", 1)}
    ground_apt = {1: chara_info.get("proper_ground_turf", 1),
                  2: chara_info.get("proper_ground_dirt", 1)}
    tally = {}
    for h in race_history or ():
        gc = _program_course(h.get("program_id"))
        if gc:
            tally[gc] = tally.get(gc, 0) + 1
    if tally:
        return max(tally, key=lambda gc: (tally[gc], apt.get(gc[1], 0), ground_apt.get(gc[0], 0)))
    ground = 1 if ground_apt[1] >= ground_apt[2] else 2
    return (ground, max(_RACE_CATEGORIES, key=lambda c: apt[c]))


def _group_race_for_turn(chara_info: dict, group_id: int, turn, race_history=()) -> dict | None:
    members = master_data.query(
        "SELECT rg.race_program_id AS pid, cs.distance AS distance, cs.ground AS ground "
        "FROM single_mode_race_group rg JOIN single_mode_program p ON p.id=rg.race_program_id "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE rg.race_group_id=?", (group_id,))
    if not members:
        return None
    own = scenarios.for_chara(chara_info).group_race_program(
        None, chara_info, group_id, turn, race_history)
    if own:
        return _race_info_for_program(own)
    pool = list(members)
    if len(pool) > 1:
        ground, category = _best_ground_category(chara_info, race_history)
        exact = [m for m in pool if m["ground"] == ground and _race_category(m["distance"]) == category]
        by_cat = exact or [m for m in pool if _race_category(m["distance"]) == category]
        pool = by_cat or pool
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = random.Random((int(turn or 0) << 16) ^ chara_id).choice(pool)
    return _race_info_for_program(pick["pid"])


def _race_for_turn(chara_info: dict, turn, race_history=()) -> dict | None:
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    if not route_ids or turn is None:
        return None
    for t, cid, kind, _cv1 in _route_goal_rows(route_ids):
        if t == turn:
            if kind == "program":
                return _race_info_for_program(cid)
            return _group_race_for_turn(chara_info, cid, turn, race_history)
    return None


@functools.lru_cache(maxsize=64)
def _dropped_alternative_ids(route_race_id_array: tuple) -> frozenset:
    if not route_race_id_array:
        return frozenset()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, sort_id, determine_race AS dr, determine_race_for_generate AS gen "
        f"FROM single_mode_route_race WHERE id IN ({ids}) AND determine_race != 0")
    by_sort: dict = {}
    for r in rows:
        by_sort.setdefault(r["sort_id"], []).append(r)
    drop = set()
    for rs in by_sort.values():
        ordered = sorted(rs, key=lambda x: x["id"])
        chosen = (next((r for r in ordered if r["dr"] == career_branches.DR_DEFAULT), None)
                  or next((r for r in ordered if r["gen"] == 0), ordered[0]))
        drop |= {r["id"] for r in rs if r["id"] != chosen["id"]}
    return frozenset(drop)


@functools.lru_cache(maxsize=64)
def _route_all_goals(route_race_id_array: tuple) -> tuple:
    if not route_race_id_array:
        return ()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, turn, sort_id, condition_type, condition_id, condition_value_1, "
        f"condition_value_2 FROM single_mode_route_race WHERE id IN ({ids}) "
        f"AND target_type=1 ORDER BY turn, sort_id")
    dropped = _dropped_alternative_ids(route_race_id_array)
    return tuple((r["turn"], r["sort_id"], r["condition_type"], r["condition_id"],
                  r["condition_value_1"], r["condition_value_2"])
                 for r in rows if r["id"] not in dropped)


def _goal_window(goal, all_goals) -> tuple:
    turn = goal[0]
    earlier = [g[0] for g in all_goals if g[0] < turn]
    return (max(earlier) if earlier else 0), turn


def _grade_tally(goal, race_history, all_goals=()) -> int:
    _turn, _sort, _ctype, cid, cv1, _cv2 = goal
    after, by = _goal_window(goal, all_goals) if all_goals else (None, None)
    tally = 0
    for h in race_history or ():
        if after is not None:
            ht = h.get("turn")
            if ht is not None and not (after < ht <= by):
                continue
        if (h.get("result_rank") or 99) > (cv1 or 99):
            continue
        row = master_data.query_one(
            "SELECT r.grade AS g FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id=p.race_instance_id "
            "JOIN race r ON r.id=ri.race_id WHERE p.id=?", (h.get("program_id"),))
        if row and row["g"] is not None and cid and row["g"] <= cid:
            tally += 1
    return tally


def _goal_is_cleared(goal, chara_info: dict, race_history, all_goals=()) -> bool:
    _turn, _sort, ctype, cid, cv1, cv2 = goal
    own = scenarios.for_chara(chara_info).goal_condition(chara_info, goal)
    if own is not None:
        return bool(own)
    if ctype == 3:
        return (chara_info.get("fans", 0) or 0) >= (cv1 or 0)
    if ctype == 2:
        return _grade_tally(goal, race_history, all_goals) >= (cv2 or 1)
    after, by = _goal_window(goal, all_goals) if all_goals else (None, None)
    for h in race_history or ():
        if h.get("program_id") != cid:
            continue
        if after is not None:
            ht = h.get("turn")
            if ht is not None and not (after < ht <= by):
                continue
        return not cv1 or (h.get("result_rank") or 99) <= cv1
    return False


def _goals_cleared_count(chara_info: dict, race_history, turn=None) -> int:
    goals = _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
    if turn is None:
        turn = chara_info.get("turn")
    n = 0
    for g in goals:
        if not _goal_is_cleared(g, chara_info, race_history, goals):
            continue
        if (g[2] in (2, 3) or g[2] in scenarios.for_chara(chara_info).passive_goal_types) \
                and turn is not None and g[0] > turn:
            continue
        n += 1
    return n


GOAL_MARKED_KEY = "goal_clear_marked"
GOAL_ANNOUNCED_KEY = "goals_announced"
SCENARIO_TURNS_FIRED_KEY = "scenario_turns_fired"
SCENARIO_EVENTS_DONE_KEY = "scenario_events_done"


def _goal_announced(full_state: dict, sort_id) -> bool:
    return sort_id in set(full_state.get(GOAL_ANNOUNCED_KEY) or ())


def _record_goal_announced(full_state: dict, sort_id) -> None:
    done = set(full_state.get(GOAL_ANNOUNCED_KEY) or ())
    done.add(sort_id)
    full_state[GOAL_ANNOUNCED_KEY] = sorted(done)


def _mark_goal_progress(response: dict, chara_info: dict, race_history, sort_id=None,
                        full_state: dict | None = None) -> None:
    events = response.get("data", {}).get("unchecked_event_array") or []
    if not events or sort_id is None:
        return
    if full_state is not None:
        if _goal_announced(full_state, sort_id):
            return
        _record_goal_announced(full_state, sort_id)
        full_state[GOAL_MARKED_KEY] = _goals_cleared_count(chara_info, race_history)
    info = events[0].setdefault("event_contents_info", {})
    info["show_clear"] = 1
    info["show_clear_sort_id"] = sort_id


_GOAL_ACHIEVED_PLAY_TIMING = 6
_GOAL_CLEAR_STATS = 3
_GOAL_CLEAR_SP = 24
GOAL_STORIES_FIRED_KEY = "goal_achieved_stories_fired"
_GOAL_ACHIEVED_EVENT_BASE = 10800


@functools.lru_cache(maxsize=256)
def _goal_achieved_stories(chara_id: int) -> tuple:
    base = 500000000 + chara_id * 1000
    return tuple((r["story_id"], r["short_story_id"] or r["story_id"])
                 for r in master_data.query(
                     "SELECT sd.story_id, sd.short_story_id FROM single_mode_story_data sd "
                     "JOIN text_data t ON t.category=181 AND t.[index]=sd.story_id "
                     "WHERE (t.text LIKE 'Goal %' OR t.text LIKE '目標達成%') AND sd.story_id>=? "
                     "AND sd.story_id<? ORDER BY sd.story_id", (base, base + 1000)))


def _goal_achieved_event(full_state: dict, chara_info: dict, sort_id) -> dict | None:
    chara_id = (chara_info.get("card_id") or 0) // 100
    stories = _goal_achieved_stories(chara_id) if chara_id else ()
    if not stories:
        return None
    fired = set(full_state.get(GOAL_STORIES_FIRED_KEY) or ())
    nxt = next(((sid, short) for sid, short in stories if sid not in fired), None)
    if not nxt:
        return None
    story_id, short_id = nxt
    full_state[GOAL_STORIES_FIRED_KEY] = sorted(fired | {story_id})
    return {
        "event_id": _real_chapter_event_ids().get(story_id) or _GOAL_ACHIEVED_EVENT_BASE + story_id % 1000,
        "chara_id": chara_id, "story_id": short_id,
        "play_timing": _GOAL_ACHIEVED_PLAY_TIMING,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 1,
            "show_clear_sort_id": sort_id if sort_id is not None else 1,
            "choice_array": [{"select_index": 1, "receive_item_id": 0,
                              "target_race_id": 0, "gain_select_id_index": 1,
                              "select_icon": 0}],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def _newly_cleared_goals(full_state: dict, chara_info: dict, race_history) -> int:
    cleared = _goals_cleared_count(chara_info, race_history)
    return cleared if cleared > (full_state.get(GOAL_MARKED_KEY) or 0) else 0


_CAREER_FAIL_REACTION_SUFFIX = 601


def _purge_queued_events(response: dict, full_state: dict) -> None:
    response["data"]["unchecked_event_array"] = []
    full_state[CAREER_FAILED_FLAG_KEY] = True
    full_state.pop(SCENARIO_SLOT_RESERVED_KEY, None)
    for key in (single_mode_events.EXTRA_EVENTS_KEY,
                single_mode_events.PENDING_EVENTS_KEY,
                event_engine.CAREER_EVENT_CTX_KEY,
                single_mode_events.FAIL_CTX_KEY,
                HINT_REVEAL_CTX_KEY, REST_CTX_KEY,
                single_mode_events.DUEL_CTX_KEY):
        full_state.pop(key, None)


def _queue_fail_reaction(response, full_state, chara_info) -> None:
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return
    story_id = 500000000 + chara * 1000 + _CAREER_FAIL_REACTION_SUFFIX
    if not master_data.query_one(
            "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)):
        return
    _queue_turn_event(response, full_state, _scenario_event_entry(
        _shared_beat_event_id(_CAREER_FAIL_REACTION_SUFFIX), story_id,
        play_timing=6, choices=[_ack_choice()], chara_id=chara), fail_chain=True)


def _career_fail_event() -> dict:
    e = _scenario_event_entry(1, 400000090, play_timing=1, choices=[])
    e["event_contents_info"]["show_clear"] = 2
    return e


def _maybe_fail_goal(response: dict, full_state: dict, career: dict, new_turn) -> bool:
    chara_info = career["data"]["chara_info"]
    if chara_info.get("state") == 2:
        return True
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    fans = chara_info.get("fans", 0)
    history = career["data"].get("race_history") or []
    _all_goals = _route_all_goals(route_ids)
    cleared = {(g[0], g[2], g[3]) for g in _all_goals
               if _goal_is_cleared(g, chara_info, history, _all_goals)}
    failed = False
    for t, cid, kind, _cv1 in _route_goal_rows(route_ids):
        if t == new_turn and kind == "program" and (t, 1, cid) not in cleared:
            prog = master_data.query_one(
                "SELECT need_fan_count FROM single_mode_program WHERE id=?", (cid,))
            if prog and fans < prog["need_fan_count"]:
                failed = True
    for g in _all_goals:
        passive = (2, 3) + tuple(scenarios.for_chara(chara_info).passive_goal_types)
        if g[2] not in passive or g[0] + 1 != new_turn:
            continue
        if (g[0], g[2], g[3]) not in cleared:
            failed = True
    if failed:
        chara_info["state"] = 2
        _purge_queued_events(response, full_state)
        _queue_fail_reaction(response, full_state, chara_info)
        _queue_turn_event(response, full_state, _career_fail_event(), fail_chain=True)
        log.info("career goal FAILED at turn %s (state=2, fail chain queued)", new_turn)
    return failed


_URA_TURN_SET_ID = 1


def _voluntary_race_for_turn(chara_info: dict, turn) -> dict | None:
    if turn is None:
        return None
    trow = master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not trow or trow["period"] == 3:
        return None
    fans = chara_info.get("fans", 0)
    grade = chara_info.get("chara_grade", 1)
    rows = master_data.query(
        "SELECT id, race_instance_id, recommend_class_id FROM single_mode_program "
        f"WHERE month=? AND half=? AND need_fan_count<=? AND race_permission IN {_year_permissions(turn)} "
        "AND id NOT IN (SELECT DISTINCT condition_id FROM single_mode_route_race "
        "WHERE condition_type=1 AND condition_id IS NOT NULL)",
        (trow["month"], trow["half"], fans))
    if not rows:
        return None
    for tolerance in (0, 1, 2, 99):
        cands = [r for r in rows if abs(r["recommend_class_id"] - grade) <= tolerance]
        if cands:
            break
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = random.Random((int(turn) << 16) ^ chara_id).choice(cands)
    return _race_info_for_program(pick["id"])


def _year_permissions(turn) -> str:
    year = (int(turn) - 1) // 24 + 1 if turn else 1
    if year <= 1:
        return "(1)"
    if year == 2:
        return "(2, 3)"
    return "(3, 4)"


def _season_for_date(date: int) -> int:
    return race_conditions.season_for_date(date)


def _career_race_conditions(program_id, chara_info: dict | None = None) -> dict:
    return race_conditions.career_conditions(chara_info, int(program_id or 0))


def _race_condition_entry(program_id: int, chara_info: dict | None = None) -> dict:
    cond = race_conditions.career_conditions(chara_info, int(program_id))
    return {"program_id": program_id, "weather": cond["weather"],
            "ground_condition": cond["ground_condition"]}


_MAIDEN_GRADE = 800


@functools.lru_cache(maxsize=1)
def _debut_program_ids() -> frozenset:
    rows = master_data.query(
        "SELECT sp.id FROM single_mode_program sp JOIN race_instance ri ON ri.id = sp.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE r.grade = 900")
    return frozenset(r["id"] for r in rows)


@functools.lru_cache(maxsize=1)
def _maiden_program_ids() -> frozenset:
    rows = master_data.query(
        "SELECT sp.id FROM single_mode_program sp JOIN race_instance ri ON ri.id = sp.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE r.grade = ?", (_MAIDEN_GRADE,))
    return frozenset(r["id"] for r in rows)


def _debut_cleared(race_history) -> bool:
    ids = _debut_program_ids() | _maiden_program_ids()
    return any(h.get("program_id") in ids and h.get("result_rank") == 1 for h in (race_history or []))


def _debut_retry_active(chara_info: dict, turn, race_history) -> bool:
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    forced_turns = _forced_race_turns(route_ids)
    if not forced_turns:
        return False
    debut_turn = min(forced_turns)
    if turn is None or turn <= debut_turn:
        return False
    return not _debut_cleared(race_history)


@functools.lru_cache(maxsize=128)
def _maiden_races_for_turn(turn) -> tuple:
    pool = sorted(_maiden_program_ids())
    if not pool or turn is None:
        return ()
    slot = master_data.query_one(
        "SELECT month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not slot:
        return ()
    ids = ",".join(str(int(i)) for i in pool)
    return tuple(r["id"] for r in master_data.query(
        f"SELECT id FROM single_mode_program WHERE id IN ({ids}) "
        f"AND month=? AND half=? ORDER BY id", (slot["month"], slot["half"])))


def _debut_retry_race(chara_info: dict, turn) -> dict | None:
    pool = sorted(_maiden_program_ids())
    if not pool:
        return None
    slot = master_data.query_one(
        "SELECT month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if slot:
        ids = ",".join(str(int(i)) for i in pool)
        same_slot = [r["id"] for r in master_data.query(
            f"SELECT id FROM single_mode_program WHERE id IN ({ids}) "
            f"AND month=? AND half=?", (slot["month"], slot["half"]))]
        pool = same_slot or pool
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = pool[(int(turn or 0) + chara_id) % len(pool)]
    return _race_info_for_program(pick)


def _build_race_condition_array(chara_info: dict, turn, race_history=()) -> list:
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    forced_turns = _forced_race_turns(route_ids)
    if turn is None:
        return []
    if turn in forced_turns:
        race = _race_for_turn(chara_info, turn, race_history)
        return [_race_condition_entry(race["program_id"], chara_info)] if race else []
    if _debut_retry_active(chara_info, turn, race_history):
        maidens = _maiden_races_for_turn(turn)
        if maidens:
            return [_race_condition_entry(pid, chara_info) for pid in maidens]
        race = _debut_retry_race(chara_info, turn)
        return [_race_condition_entry(race["program_id"], chara_info)] if race else []

    trow = master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not trow:
        return []
    if trow["period"] == 3:
        rows = master_data.query(
            "SELECT id FROM single_mode_program WHERE month=? AND half=? "
            "AND race_permission=5 AND base_program_id=0 ORDER BY id",
            (trow["month"], trow["half"]))
        return [_race_condition_entry(r["id"], chara_info) for r in rows]
    fans = chara_info.get("fans", 0)
    rows = master_data.query(
        "SELECT id FROM single_mode_program WHERE month=? AND half=? AND need_fan_count<=? "
        f"AND race_permission IN {_year_permissions(turn)} AND base_program_id=0",
        (trow["month"], trow["half"], fans))
    return [_race_condition_entry(r["id"], chara_info) for r in rows]


RESERVED_RACES_KEY = "reserved_races"
RESERVED_DECKS_KEY = "reserved_race_decks"
RESERVED_DEFAULT_DECK_KEY = "reserved_race_default_deck"

_RESERVED_DECK_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "capture_06d26c"

_RESERVED_DECK_PROFILES = {
    "global": {"count": 10, "name": "Agenda {n}", "fixture_dir": _RESERVED_DECK_FIXTURE_DIR,
               "info_reply": False},
    "jp": {"count": 8, "name": "", "fixture_dir": None, "info_reply": True},
}


def _reserved_deck_profile() -> dict:
    return _RESERVED_DECK_PROFILES[region.CURRENT_REGION.get()]


def _default_reserved_decks() -> tuple:
    return _default_reserved_decks_for(region.CURRENT_REGION.get())


@functools.lru_cache(maxsize=None)
def _default_reserved_decks_for(region_name: str) -> tuple:
    profile = _RESERVED_DECK_PROFILES[region_name]
    fixture_dir = profile["fixture_dir"]
    found: dict = {}
    for path in sorted(fixture_dir.glob("*single_mode_team_start_incoming.json")) if fixture_dir else ():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for deck in ((doc.get("data") or {}).get("data") or {}).get("reserved_race_array") or []:
            num = deck.get("deck_num")
            if isinstance(num, int) and num > 0 and num not in found:
                found[num] = [{"year": r.get("year"), "program_id": r.get("program_id")}
                              for r in deck.get("race_array") or []]
        if found:
            break
    if fixture_dir and not found:
        log.warning("single_mode: no reserved-race deck fixture under %s -- "
                    "serving empty agenda presets", fixture_dir)
    return tuple({"deck_num": n, "deck_name": profile["name"].format(n=n),
                  "race_array": found.get(n, [])}
                 for n in range(1, profile["count"] + 1))


def _reserved_key(race: dict) -> tuple:
    return (race.get("year"), race.get("program_id"))


def _apply_reserve_delta(races: list, add_array, cancel_array) -> list:
    cancel = {_reserved_key(r) for r in (cancel_array or [])}
    out = [r for r in races if _reserved_key(r) not in cancel]
    have = {_reserved_key(r) for r in out}
    for r in (add_array or []):
        key = _reserved_key(r)
        if r.get("program_id") is not None and key not in have:
            out.append({"year": r.get("year"), "program_id": r.get("program_id")})
            have.add(key)
    return out


def _reserved_decks(full_state: dict) -> list:
    saved = full_state.get(RESERVED_DECKS_KEY) or {}
    out = []
    for deck in _default_reserved_decks():
        row = saved.get(str(deck["deck_num"]))
        if not isinstance(row, dict):
            out.append(copy.deepcopy(deck))
            continue
        out.append({"deck_num": deck["deck_num"],
                    "deck_name": row.get("deck_name") or deck["deck_name"],
                    "race_array": copy.deepcopy(row.get("race_array") or [])})
    known = {d["deck_num"] for d in out}
    for key in sorted(saved, key=lambda k: int(k) if str(k).isdigit() else 0):
        row = saved[key]
        num = int(key) if str(key).isdigit() else 0
        if num > 0 and num not in known and isinstance(row, dict):
            out.append({"deck_num": num, "deck_name": row.get("deck_name") or f"Agenda {num}",
                        "race_array": copy.deepcopy(row.get("race_array") or [])})
    return out


def _reserved_race_array(full_state: dict, deck0_name: str = "") -> list:
    deck0 = {"deck_num": 0, "deck_name": deck0_name,
             "race_array": copy.deepcopy(list(full_state.get(RESERVED_RACES_KEY) or []))}
    return [deck0] + _reserved_decks(full_state)


def _reserved_race_info(full_state: dict, deck0_name: str = "") -> dict:
    try:
        default_deck = int(full_state.get(RESERVED_DEFAULT_DECK_KEY) or 0)
    except (TypeError, ValueError):
        default_deck = 0
    return {"default_deck_num": default_deck, "needs_default_confirm": 0,
            "reserved_race_array": _reserved_race_array(full_state, deck0_name)}


def _build_reserved_race_info(template: dict, full_state: dict) -> dict:
    deck0_name = next((d.get("deck_name", "") for d in template.get("reserved_race_array") or []
                       if isinstance(d, dict) and d.get("deck_num") == 0), "")
    return _reserved_race_info(full_state, deck0_name)


def handle_multi_race_reserve(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    req = payload.get("multi_race_reserve_deck")
    if not isinstance(req, dict):
        req = payload
    try:
        deck_num = int(req.get("deck_num") or 0)
    except (TypeError, ValueError):
        deck_num = 0
    full_state = state_store.get_state(viewer_id) or {}

    if deck_num <= 0:
        full_state[RESERVED_RACES_KEY] = _apply_reserve_delta(
            list(full_state.get(RESERVED_RACES_KEY) or []),
            req.get("add_race_array"), req.get("cancel_race_array"))
    else:
        current = next((d for d in _reserved_decks(full_state) if d["deck_num"] == deck_num),
                       {"deck_name": f"Agenda {deck_num}", "race_array": []})
        name = req.get("deck_name")
        saved = dict(full_state.get(RESERVED_DECKS_KEY) or {})
        saved[str(deck_num)] = {
            "deck_name": (name.strip() if isinstance(name, str) and name.strip()
                          else current["deck_name"]),
            "race_array": _apply_reserve_delta(current["race_array"],
                                               req.get("add_race_array"),
                                               req.get("cancel_race_array")),
        }
        full_state[RESERVED_DECKS_KEY] = saved
        if req.get("is_default"):
            full_state[RESERVED_DEFAULT_DECK_KEY] = deck_num

    state_store.save_state(viewer_id, full_state)
    data = ({"reserved_race_info": _reserved_race_info(full_state)}
            if _reserved_deck_profile()["info_reply"]
            else {"reserved_race_array": _reserved_race_array(full_state)})
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def _build_reserved_race_array(template: list, full_state: dict) -> list:
    if not template:
        return template
    deck0_name = next((d.get("deck_name", "") for d in template
                       if isinstance(d, dict) and d.get("deck_num") == 0), "")
    return _reserved_race_array(full_state, deck0_name)


_RACE_TITLE_FILLER = frozenset({"race"})


def _race_name_tokens(name: str | None) -> list:
    if not name:
        return []
    s = str(name).replace("\\n", " ").replace("\n", " ")
    return ["".join(ch for ch in w if ch.isalnum())
            for w in s.lower().replace(".", " ").replace("(", " ").replace(")", " ").split()
            if any(ch.isalnum() for ch in w)]


def _race_name_variants(name: str | None) -> list:
    if not name:
        return []
    s = str(name)
    out = [s]
    if "(" in s and ")" in s:
        inside = s[s.index("(") + 1:s.rindex(")")]
        outside = (s[:s.index("(")] + s[s.rindex(")") + 1:]).strip()
        out += [inside, outside, f"{inside} {outside}"]
    return [v for v in out if v.strip()]


def _race_title_matches(story_part: str | None, race_name: str | None) -> bool:
    st = _race_name_tokens(story_part)
    if not st:
        return False
    if len(st) > 1 and st[-1] in _RACE_TITLE_FILLER:
        st = st[:-1]
    for variant in _race_name_variants(race_name):
        rt = _race_name_tokens(variant)
        if len(st) == len(rt) and all(r.startswith(s) or s.startswith(r)
                                      for s, r in zip(st, rt)):
            return True
    return False


def _find_race_story_ids(chara_id: int, prefix: str, race_name: str,
                         occurrence: int = 0, occurrences: int = 1) -> list:
    base = 500000000 + chara_id * 1000
    if not race_name:
        return []
    rows = master_data.query(
        "SELECT [index], text FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<? AND text LIKE ? ORDER BY [index]",
        (base, base + 1000, f"{prefix}%"))
    ids = [r["index"] for r in rows
           if _race_title_matches((r["text"] or "")[len(prefix):].split(":")[0], race_name)]
    if not ids:
        ids = _native_race_story_ids(base, prefix, race_name)
    ids = [i for i in ids if _story_exists(i)]
    runs = []
    for i in ids:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    if not runs:
        return []
    if len(runs) == 1 and occurrences > 1 and len(runs[0]) == occurrences:
        return [runs[0][min(occurrence, occurrences - 1)]]
    return runs[min(occurrence, len(runs) - 1)]


_NATIVE_RACE_TITLE = {"Before the ": ("{}にむけて", True), "After the ": ("{}の後に", False)}
_NATIVE_DEBUT = ("デビュー戦", "メイクデビュー")
_RACE_ID_NAME_CATEGORIES = (32, 33, 36, 38)
_INSTANCE_NAME_CATEGORIES = (28, 29)


@functools.lru_cache(maxsize=512)
def _native_race_names(race_name: str) -> tuple:
    if race_name == "Debut":
        return _NATIVE_DEBUT
    rid_cats = ",".join(map(str, _RACE_ID_NAME_CATEGORIES))
    inst_cats = ",".join(map(str, _INSTANCE_NAME_CATEGORIES))
    try:
        race_ids = {r["i"] for r in master_data.query(
            f"SELECT [index] AS i FROM text_data WHERE category IN ({rid_cats}) AND text=?",
            (race_name,))}
        race_ids |= {r["race_id"] for r in master_data.query(
            f"SELECT ri.race_id FROM text_data v JOIN race_instance ri ON ri.id = v.[index] "
            f"WHERE v.category IN ({inst_cats}) AND v.text=?", (race_name,))}
        if not race_ids:
            return ()
        ids = ",".join(str(int(i)) for i in race_ids)
        rows = master_data.query(
            f"SELECT text FROM main.text_data WHERE category IN ({rid_cats}) AND [index] IN ({ids}) "
            f"UNION SELECT m.text FROM main.text_data m JOIN race_instance ri ON ri.id = m.[index] "
            f"WHERE m.category IN ({inst_cats}) AND ri.race_id IN ({ids})")
    except Exception:
        return ()
    names = {(r["text"] or "").replace("\\n", "") for r in rows} - {"", race_name}
    return tuple(sorted(names, key=len, reverse=True))


def _native_race_story_ids(base: int, prefix: str, race_name: str) -> list:
    form = _NATIVE_RACE_TITLE.get(prefix)
    names = _native_race_names(race_name) if form else ()
    if not names:
        return []
    pattern, whole = form
    wanted = [pattern.format(n) for n in names]
    out = []
    for r in master_data.query(
            "SELECT [index], text FROM text_data WHERE category=181 "
            "AND [index]>=? AND [index]<? ORDER BY [index]", (base, base + 1000)):
        title = (r["text"] or "").replace("\\n", "")
        if any(title == w if whole else title.startswith(w) for w in wanted):
            out.append(r["index"])
    return out


def _race_outcome_variant(variants: list, rank) -> int | None:
    if not variants:
        return None
    rank = rank or 1
    n = len(variants)
    if n == 1 or rank == 1:
        return variants[0]
    if n == 2:
        return variants[1]
    if n == 3:
        return variants[1] if rank <= 3 else variants[2]
    return variants[min(rank, n) - 1]


def _race_occurrence(chara_info: dict, turn, race_name: str | None) -> tuple:
    if not race_name or not chara_info:
        return 0, 1
    goals = _route_goal_rows(tuple(chara_info.get("route_race_id_array") or ()))
    turns = []
    for t, cid, kind, _cv1 in goals:
        if kind != "program":
            continue
        info = _race_info_for_program(cid)
        if info and info.get("story_name") == race_name:
            turns.append(t)
    if turn is None:
        return 0, max(1, len(turns))
    return sum(1 for t in turns if t < turn), max(1, len(turns))


@functools.lru_cache(maxsize=4096)
def _story_exists(story_id: int) -> bool:
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)))


@functools.lru_cache(maxsize=1024)
def _story_or_short_exists(story_id: int) -> bool:
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data "
        "WHERE story_id=? OR short_story_id=?", (story_id, story_id)))


_CHALLENGE_EVENT_ID = 3016
_CHALLENGE_STORY_SUFFIX = 600
_CHALLENGE_STAT_GAIN = 5
_CHALLENGE_CHANCE_BY_MOOD = {5: 0.386, 4: 0.369, 3: 0.181, 2: 0.146, 1: 0.166}
_CHALLENGE_STAT_KEY = "challenge_stat"


def _roll_challenge_event(chara_info: dict) -> tuple:
    chara_id = (chara_info.get("card_id") or 0) // 100
    story_id = 500000000 + chara_id * 1000 + _CHALLENGE_STORY_SUFFIX
    chance = _CHALLENGE_CHANCE_BY_MOOD.get(int(chara_info.get("motivation") or 3), 0.181)
    if not chara_id or not _story_exists(story_id) or random.random() >= chance:
        return [], None
    return [{
        "event_id": _CHALLENGE_EVENT_ID, "chara_id": chara_id,
        "story_id": event_engine.wire_story_id(story_id), "play_timing": 2,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [{"select_index": 1, "receive_item_id": 0,
                              "target_race_id": 0, "gain_select_id_index": 1,
                              "select_icon": 0}],
            "tips_training_partner_id": None},
        "succession_event_info": None, "minigame_result": None,
    }], random.choice(event_engine._STAT_KEYS_ALL)


def _pay_challenge_stat(full_state: dict, chara_info: dict, data: dict) -> None:
    ctx = full_state.get(RACE_CTX_KEY)
    stat = ctx.pop(_CHALLENGE_STAT_KEY, None) if isinstance(ctx, dict) else None
    if not stat:
        return
    cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
    if (chara_info.get(stat) or 0) >= cap:
        _note_not_up(data, [event_engine._STAT_KEY_IDX[stat]])
    event_engine.add_stat(chara_info, stat, _CHALLENGE_STAT_GAIN)


def _retarget_race_prep_event(unchecked_event_array, chara_id: int, race_name: str | None = None,
                              chara_info: dict | None = None, turn=None) -> None:
    if not unchecked_event_array or not chara_id:
        return
    ev = unchecked_event_array[0]
    old_story = ev.get("story_id")
    if not old_story:
        return
    if race_name:
        occ, total = _race_occurrence(chara_info, turn, race_name)
        ids = _find_race_story_ids(chara_id, "Before the ", race_name, occ, total)
        story_id = ids[0] if ids else None
    else:
        title_row = master_data.query_one(
            "SELECT text FROM text_data WHERE category=181 AND [index]=?", (old_story,))
        story_id = None
        if title_row:
            base = 500000000 + chara_id * 1000
            rows = master_data.query(
                "SELECT [index] FROM text_data WHERE category=181 AND [index]>=? AND [index]<? "
                "AND text=? ORDER BY ([index] % 1000 = 200), [index]",
                (base, base + 1000, title_row["text"]))
            story_id = next((r["index"] for r in rows if _story_exists(r["index"])), None)
    if not story_id:
        base = 500000000 + chara_id * 1000
        for suffix in (600, 601):
            if master_data.query_one(
                    "SELECT story_id FROM single_mode_story_data WHERE story_id=?",
                    (base + suffix,)):
                story_id = base + suffix
                break
    if story_id and _story_exists(story_id):
        ev["chara_id"] = chara_id
        ev["story_id"] = event_engine.wire_story_id(story_id)


def _post_race_event_entry(chara_info: dict, race_name: str, won: bool = True,
                           choices: int = 2, rank=None, turn=None) -> dict | None:
    chara_id = (chara_info.get("card_id") or 0) // 100
    if not chara_id:
        return None
    occ, total = _race_occurrence(chara_info, turn, race_name)
    variants = _find_race_story_ids(chara_id, "After the ", race_name, occ, total)
    story = _race_outcome_variant(variants, rank if rank else (1 if won else 99))
    if not story:
        return None
    return {
        "event_id": 11168, "chara_id": chara_id,
        "story_id": event_engine.wire_story_id(story), "play_timing": 3,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [
                {"select_index": i, "receive_item_id": 0, "target_race_id": 0,
                 "gain_select_id_index": i, "select_icon": 0}
                for i in range(1, max(1, choices) + 1)],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


_POST_RACE_GRADE_COL = {100: 0, 200: 1, 300: 1}
_POST_RACE_TABLE = {
    "win":    ((10, 8, 5), (45, 35, 30)),
    "solid":  ((8, 5, 3),  (45, 35, 30)),
    "defeat": ((4, 3, 0),  (25, 20, 10)),
}
_POST_RACE_ENERGY = {
    "win":    (-15, (-5, -20)),
    "solid":  (-20, (-10, -30)),
    "defeat": (-25, (-15, -35)),
}
_POST_RACE_STORY_SUFFIX = {"win": 708, "solid": 709, "defeat": 710}
_POST_RACE_EVENT_ID = 11169
_ETSUKO_EVENT_ID = 11170
_ETSUKO_COVERAGE_CHANCE = 0.20
_ETSUKO_STORY_WIN = 400000035
_ETSUKO_STORY_LOSE = 400000036
_ETSUKO_TARGET, _ETSUKO_CHARA = 103, 9003
_ETSUKO_WIN_FANS = 500
_STAT_CHOICES = ("speed", "stamina", "power", "guts", "wisdom")


def _placement_bucket(rank) -> str:
    rank = int(rank or 1)
    if rank <= 1:
        return "win"
    return "solid" if rank <= 5 else "defeat"


_DEBUT_STAT_COUNT = 3
_DEBUT_STAT_BY_RANK = {1: 3, 2: 2, 3: 1, 4: 1, 5: 1}


def _post_race_amounts(chara_info: dict, program_id, bucket: str,
                       full_state: dict = None, rank=None) -> tuple:
    grade = _race_grade_of(program_id) if program_id else None
    col = _POST_RACE_GRADE_COL.get(grade, 2)
    stats, sps = _POST_RACE_TABLE[bucket]
    override = scenarios.for_chara(chara_info).post_race_sp
    if override and bucket in override:
        sps = override[bucket]
    stat = stats[col]
    if grade == _DEBUT_GRADE:
        r = int(rank) if rank else {"win": 1, "solid": 2, "defeat": 6}[bucket]
        stat = _DEBUT_STAT_BY_RANK.get(r, 1)
    return (_scale_race_reward(stat, chara_info, full_state),
            _scale_race_reward(sps[col], chara_info, full_state))


_DISTANCE_TYPE = {"short": 1, "mile": 2, "middle": 3, "long": 4}


@functools.lru_cache(maxsize=64)
def _race_related_skills(ground: int, category: str) -> tuple:
    dt = _DISTANCE_TYPE.get(category)
    if not dt:
        return ()
    d, g = f"%distance_type=={dt}%", f"%ground_type=={ground}%"
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM skill_data WHERE is_general_skill=1 AND disable_singlemode=0 "
        "AND (condition_1 LIKE ? OR condition_2 LIKE ? "
        "     OR condition_1 LIKE ? OR condition_2 LIKE ?)", (d, d, g, g)))


_RACE_HINT_CHANCE = 0.35


_GOLD_CITY_CHARA = 1040
_GOLD_CITY_BUFF_TURN = 57
_GOLD_CITY_STAT_SPREAD = {
    "win":    ("all_stats", 1),
    "solid":  ("random_stats", 2),
    "defeat": ("random_stats", 2),
}


def _post_race_stat_effect(chara_info: dict, bucket: str, stat: int,
                           program_id=None) -> dict | None:
    if not stat:
        return None
    if program_id and _race_grade_of(program_id) == _DEBUT_GRADE:
        return {"type": "random_stats", "value": f"+{stat}",
                "stat_count": _DEBUT_STAT_COUNT}
    chara = (chara_info.get("card_id") or 0) // 100
    if (chara == _GOLD_CITY_CHARA
            and (chara_info.get("turn") or 0) >= _GOLD_CITY_BUFF_TURN):
        kind, count = _GOLD_CITY_STAT_SPREAD[bucket]
        eff = {"type": kind, "value": f"+{stat}"}
        if kind == "random_stats":
            eff["stat_count"] = count
        return eff
    return {"type": random.choice(_STAT_CHOICES), "value": f"+{stat}"}


def _race_reward_effects(stat_effect: dict | None, sp: int,
                         display_id: int = event_engine.DISPLAY_RACE_GRADE_STATS) -> list:
    out = []
    if stat_effect:
        out.append(dict(stat_effect))
    if sp:
        out.append({"type": "skill_points", "value": f"+{sp}"})
    if out:
        out[0]["display_id"] = display_id
        for e in out[1:]:
            e["hidden"] = True
    return out


def _race_skill_hint(chara_info: dict, program_id) -> dict | None:
    course = _program_course(program_id) if program_id else None
    if not course:
        return None
    pool = _race_related_skills(*course)
    if not pool:
        return None
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or ()}
    candidates = [s for s in pool if s not in learned]
    if not candidates:
        return None
    return {"type": "skill_hint", "skill_id": random.choice(candidates),
            "value": 1, "chance": _RACE_HINT_CHANCE,
            "display_id": event_engine.DISPLAY_RANDOM_SKILL_CHANCE}


def _post_race_choices(chara_info: dict, program_id, rank,
                       full_state: dict = None) -> dict:
    bucket = _placement_bucket(rank)
    stat, sp = _post_race_amounts(chara_info, program_id, bucket, full_state, rank)
    shared = _race_reward_effects(
        _post_race_stat_effect(chara_info, bucket, stat, program_id), sp,
        event_engine.DISPLAY_RACE_GRADE_STATS + (bucket == "solid"))
    hint = _race_skill_hint(chara_info, program_id)
    if hint:
        shared.append(hint)
    if bucket == "defeat":
        from .. import negative_race_skills
        penalty = negative_race_skills.effect(chara_info, program_id, hidden=bool(hint), full_state=full_state)
        if penalty:
            shared.append(penalty)
    top, bottom = (scenarios.for_chara(chara_info).post_race_energy
                   or _POST_RACE_ENERGY)[bucket]
    k = tuning.mult("race_fatigue_multiplier")
    if k != 1.0:
        top, bottom = int(round(top * k)), tuple(int(round(e * k)) for e in bottom)
    return {"choices": [
        {"effects": shared + [{"type": "energy", "value": str(top)}]},
        {"outcomes": [shared + [{"type": "energy", "value": str(e)}] for e in bottom],
         "random_either": True},
    ]}


def _etsuko_bond(value: int) -> dict:
    return {"type": "npc_bond", "target_id": _ETSUKO_TARGET,
            "chara_id": _ETSUKO_CHARA, "value": f"{value:+d}"}


def _etsuko_coverage(chara_info: dict, program_id, rank,
                     full_state: dict = None) -> tuple:
    won = int(rank or 1) <= 1
    bucket = "win" if won else "defeat"
    stat, sp = _post_race_amounts(chara_info, program_id, bucket, full_state,
                                  rank if won else 6)
    gains = _race_reward_effects(
        _post_race_stat_effect(chara_info, bucket, stat, program_id), sp)
    hint = _race_skill_hint(chara_info, program_id)
    if hint:
        gains.append(hint)
    if not won:
        from .. import negative_race_skills
        penalty = negative_race_skills.effect(chara_info, program_id, hidden=bool(hint), full_state=full_state)
        if penalty:
            gains.append(penalty)
    if won:
        return (_ETSUKO_STORY_WIN, {"choices": [{"effects": gains + [
            {"type": "energy", "value": "-15"}, {"type": "mood", "value": "+1"},
            {"type": "fans", "value": f"+{_ETSUKO_WIN_FANS}"}, _etsuko_bond(15)]}]})
    return (_ETSUKO_STORY_LOSE, {"choices": [
        {"outcomes": [
            gains + [{"type": "energy", "value": "-25"},
                     {"type": "mood", "value": "-1"}, _etsuko_bond(-10)],
            gains + [{"type": "energy", "value": "-15"},
                     {"type": "mood", "value": "+1"}, _etsuko_bond(15)]],
         "random_either": True},
        {"effects": gains + [{"type": "energy", "value": "-20"}, _etsuko_bond(10)]},
    ]})


_GOAL_RACE_STAT_COUNT = {100: 5, 200: 4, 300: 4}
_GOAL_RACE_STAT_COUNT_DEFAULT = 3
_JP_GOAL_RACE_SP = 45


def _goal_race_reward(chara_info: dict, program_id, finals_round, rank) -> dict:
    reward = _race_reward_for(chara_info, program_id, finals_round, int(rank or 1))
    count = 5 if finals_round or region.CURRENT_REGION.get() == "jp" else _GOAL_RACE_STAT_COUNT.get(
        _race_grade_of(program_id) if program_id else None,
        _GOAL_RACE_STAT_COUNT_DEFAULT)
    if count >= 5:
        effects = [{"type": "all_stats", "value": f"+{reward['stats']}"}]
    else:
        effects = [{"type": "random_stats", "value": f"+{reward['stats']}",
                    "stat_count": count}]
    if reward.get("sp"):
        effects.append({"type": "skill_points", "value": f"+{reward['sp']}"})
    return {"choices": [{"effects": effects}]}


def _is_goal_race(chara_info: dict, program_id, turn, race_history=()) -> bool:
    if not program_id:
        return False
    if _finals_round_for_program(program_id):
        return True
    if _race_grade_of(program_id) == _DEBUT_GRADE:
        return True
    goal = _race_for_turn(chara_info, turn, race_history) if chara_info else None
    return bool(goal and goal.get("program_id") == program_id)


def _voluntary_race_event(full_state: dict, chara_info: dict, program_id, rank):
    chara = (chara_info.get("card_id") or 0) // 100
    unlocked = [list(x) for x in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
    if ([_ETSUKO_TARGET, _ETSUKO_CHARA] in unlocked
            and random.random() < _ETSUKO_COVERAGE_CHANCE):
        story_id, ev = _etsuko_coverage(chara_info, program_id, rank, full_state)
        if _story_exists(story_id):
            return (event_engine.career_event_entry(
                ev, _ETSUKO_EVENT_ID, story_id, chara_id=_ETSUKO_CHARA,
                support_card_id=0, play_timing=3),
                ev, event_engine.event_title(story_id))
    ev = _post_race_choices(chara_info, program_id, rank, full_state)
    suffix = _POST_RACE_STORY_SUFFIX[_placement_bucket(rank)]
    story_id = 500000000 + chara * 1000 + suffix
    if not chara or not _story_exists(story_id):
        return None, ev, None
    return (event_engine.career_event_entry(
        ev, _shared_beat_event_id(suffix) if suffix in _SHARED_BEAT_EVENT_IDS
        else _POST_RACE_EVENT_ID,
        story_id, chara_id=chara, support_card_id=0, play_timing=3),
        ev, event_engine.event_title(story_id))


_FINALS_EVENTS = {
    1: {"pre": (11000, 400001031, False), "post": (11003, 400001032, 9)},
    2: {"pre": (11001, 400001033, False), "post": (11004, 400001034, 10)},
    3: {"pre": (11002, 400001035, False), "post": (11005, 400001036, 11)},
}
_ENDING_CHAIN = [
    (10, 400000091, 3, 0),
    (4, 400001412, 2, 2),
]
_ENDING_CHARA_EVENT_ID = 6
_REPORTER_ISSUE_IDS = (2, 10)
_REPORTER_ISSUE_HIGH_BOND = 70


def reporter_issue_event_id(chara_info: dict) -> int:
    return 2 if _bond_of(chara_info, 103) >= _REPORTER_ISSUE_HIGH_BOND else 10


_REPORTER_ISSUE_PAY = ((90, 5, 20), (70, 3, 10), (40, 0, 10), (0, 0, 5))


def reporter_issue_pay(chara_info: dict) -> tuple:
    bond = _bond_of(chara_info, 103)
    return next((st, sp) for need, st, sp in _REPORTER_ISSUE_PAY if bond >= need)


def reporter_issue_effects(chara_info: dict) -> list:
    stats, sp = reporter_issue_pay(chara_info)
    out = [{"type": "all_stats", "value": f"+{stats}"}] if stats else []
    return out + [{"type": "skill_points", "value": f"+{sp}"}]
_ENDING_CHARA_LOST_EVENT_ID = 5

_TRAINEE_ENDING_STATS = 5
_TRAINEE_ENDING_SP = 20
ENDING_PAYOUT_KEY = "ending_payouts"

_SUPER_SUCCESS_EVENT_ID = 4
_SUPER_SUCCESS_MAX = (25, 100)
_SUPER_SUCCESS_BASE = (20, 80)
_DIRECTOR_TARGET = 102
_DIRECTOR_MAX_BOND = 100


_FRIEND_ENDING = {
    809001009: (11, (("speed", 9), ("stamina", 9), ("power", 9), ("guts", 9),
                     ("wiz", 9), ("skill_point", 40))),
    809001122: (43, (("speed", 12), ("stamina", 12), ("wiz", 12), ("skill_point", 40))),
    809001123: (42, (("speed", 5), ("stamina", 5), ("wiz", 5), ("skill_point", 25))),
    809002009: (32, (("speed", 20), ("guts", 20), ("skill_point", 56))),
    809008009: (20, (("speed", 15), ("guts", 15), ("skill_point", 30))),
    809043010: (25, (("power", 12), ("guts", 18), ("skill_point", 20))),
    809049022: (36, (("speed", 15), ("stamina", 15), ("guts", 15), ("skill_point", 50))),
}
_FRIEND_ENDING_SCENARIO_CHARAS = {9050, 1139}


def friend_endings(full_state: dict, chara_info: dict) -> list:
    out = []
    cards = sorted(chara_info.get("support_card_array") or (),
                   key=lambda c: c.get("position") or 0)
    for card in cards:
        card_id = card.get("support_card_id") or 0
        if not pal_cards.is_pal(card_id):
            continue
        if pal_cards.card_row(card_id)[1] in _FRIEND_ENDING_SCENARIO_CHARAS:
            continue
        entry = _pal_state(full_state).get(str(card_id)) or {}
        done = (entry.get("step") or 0) >= pal_cards.outing_chain_len(card_id)
        story = pal_cards.ending_story(card_id, done)
        if not story:
            continue
        event_id, base = _FRIEND_ENDING.get(story, (story % 100000, ()))
        boosted = event_engine.boost_effects(
            [{"type": t, "value": f"+{v}"} for t, v in base], card_id, chara_info)
        gains = {t: int(e["value"]) for (t, _v), e in zip(base, boosted)}
        log.info("friend ending: card %s %s -> %s %s", card_id,
                 "complete" if done else "incomplete", story, gains or "(story only)")
        out.append((event_id, story, card_id, gains))
    return out


def friend_ending_ce_events(full_state: dict, chara_info: dict, priority: int,
                            source: str) -> list:
    out = []
    for event_id, story, card_id, gains in friend_endings(full_state, chara_info):
        out.append(career_events.Event(
            event_id=event_id, story_id=story, play_timing=3,
            choices=[career_events.Choice(effects=[
                {"type": t, "value": f"+{v}"} for t, v in gains.items()])],
            support_card_id=card_id, once_key=f"friend_ending:{story}",
            priority=priority, source=source))
    return out


def _friend_ending_events(full_state: dict, chara_info: dict) -> list:
    out = []
    for event_id, story, card_id, gains in friend_endings(full_state, chara_info):
        ev = _scenario_event_entry(event_id, story, play_timing=3, choices=[_ack_choice()])
        ev["event_contents_info"]["support_card_id"] = card_id
        out.append((ev, {"gains": gains}))
    return out


def _ending_payout_events(full_state: dict, chara_info: dict, won_final: bool = True) -> list:
    out = []
    chara_id = (chara_info.get("card_id") or 0) // 100
    if chara_id:
        hot_spring = 500000000 + chara_id * 1000 + _HOT_SPRING_SUFFIX
        if full_state.get(HOT_SPRING_KEY) and won_final and _story_exists(hot_spring):
            out.append((_scenario_event_entry(
                _HOT_SPRING_EVENT_ID, hot_spring, play_timing=3,
                choices=[_ack_choice()], chara_id=chara_id), 0, 0))
        if won_final:
            out.append((_scenario_event_entry(
                _ENDING_CHARA_EVENT_ID, 500000000 + chara_id * 1000 + 111,
                play_timing=3, choices=[_ack_choice()], chara_id=chara_id),
                _TRAINEE_ENDING_STATS, _TRAINEE_ENDING_SP))
        else:
            out.append((_scenario_event_entry(
                _ENDING_CHARA_LOST_EVENT_ID, 500000000 + chara_id * 1000 + 110,
                play_timing=3, choices=[_ack_choice()], chara_id=chara_id), 0, 0))
    return out


def _ack_choice(select_index=1, receive_item_id=0):
    return {"select_index": select_index, "receive_item_id": receive_item_id,
            "target_race_id": 0, "gain_select_id_index": 1, "select_icon": 0}


def _scenario_event_entry(event_id, story_id, play_timing, choices, show_clear=0,
                          show_clear_sort_id=0, chara_id=0) -> dict:
    return {
        "event_id": event_id, "chara_id": chara_id,
        "story_id": event_engine.wire_story_id(story_id),
        "play_timing": play_timing,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": show_clear,
            "show_clear_sort_id": show_clear_sort_id, "choice_array": choices,
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def _finals_round_for_program(program_id) -> int | None:
    if program_id is None:
        return None
    if 2001 <= program_id <= 2041 or 2301 <= program_id <= 2341:
        return 1
    if 2101 <= program_id <= 2141 or 2401 <= program_id <= 2441:
        return 2
    if 2201 <= program_id <= 2241 or 2501 <= program_id <= 2541:
        return 3
    return None


def _finals_events_for(chara_info) -> dict:
    return scenarios.for_chara(chara_info).finals_events or _FINALS_EVENTS


def _finals_event_entry(round_no: int, which: str, chara_info=None) -> dict:
    table = _finals_events_for(chara_info)
    if which == "pre":
        event_id, story_id, has_choice = table[round_no]["pre"]
        return _scenario_event_entry(event_id, story_id, play_timing=2,
                                     choices=[_ack_choice()] if has_choice else [])
    event_id, story_id, sort_id, *rest = table[round_no]["post"]
    return _scenario_event_entry(event_id, story_id, play_timing=3,
                                 choices=[_ack_choice()],
                                 show_clear=rest[0] if rest else 1,
                                 show_clear_sort_id=sort_id)


def _career_player_race_chara(chara_info: dict, viewer_id,
                              running_style: int | None = None) -> dict:
    chara = dict(chara_info)
    chara["running_style"] = running_style or _best_running_style(chara_info)
    chara.setdefault("viewer_id", viewer_id)
    chara.setdefault("trainer_name", "Trainer")
    return chara


_MOB_ID_BASE = 3_000_000


def _npc_running_style(row: dict) -> int:
    apts = {1: row.get("proper_running_style_nige", 1), 2: row.get("proper_running_style_senko", 1),
            3: row.get("proper_running_style_sashi", 1), 4: row.get("proper_running_style_oikomi", 1)}
    return max(apts, key=apts.get)


@functools.lru_cache(maxsize=None)
def _npc_skill_array(skill_set_id: int) -> tuple:
    if not skill_set_id:
        return ()
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    if row is None:
        return ()
    out = []
    for n in range(1, 21):
        skill_id = row[f"skill_id{n}"]
        if skill_id:
            out.append({"skill_id": skill_id, "level": row[f"skill_level{n}"] or 1})
    return tuple(out)


def _npc_skills(row: dict) -> list:
    return [dict(sk) for sk in _npc_skill_array(row.get("skill_set_id") or 0)]


@functools.lru_cache(maxsize=1)
def _mob_id_pool() -> tuple:
    return tuple(r["mob_id"] for r in master_data.query(
        "SELECT DISTINCT m.mob_id FROM mob_data m "
        "JOIN single_mode_npc n ON n.mob_id = m.mob_id WHERE m.mob_id <= ? "
        "ORDER BY m.mob_id", (race_mobs.MAX_RACE_MOB_ID,)))


def _mob_shape_from_npc(row: dict, index: int, scale: float = 1.0) -> dict:
    pool = _mob_id_pool()
    mob_id = row.get("mob_id") or (pool[(row.get("id", 0) + index) % len(pool)]
                                   if pool else 8000)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": row["id"], "single_mode_chara_id": row["id"],
        "nickname_id": 0, "card_id": 0, "mob_id": mob_id, "chara_id": 1,
        "race_dress_id": 1, "rarity": 1, "talent_level": 1,
        "skill_array": _npc_skills(row),
        "speed": max(1, round(row["speed"] * scale)),
        "stamina": max(1, round(row["stamina"] * scale)),
        "power": max(1, round(row["pow"] * scale)),
        "guts": max(1, round(row["guts"] * scale)),
        "wiz": max(1, round(row["wiz"] * scale)),
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 1),
        "proper_distance_mile": row.get("proper_distance_mile", 1),
        "proper_distance_middle": row.get("proper_distance_middle", 1),
        "proper_distance_long": row.get("proper_distance_long", 1),
        "proper_running_style_nige": row.get("proper_running_style_nige", 1),
        "proper_running_style_senko": row.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": row.get("proper_ground_turf", 1),
        "proper_ground_dirt": row.get("proper_ground_dirt", 1),
        "motivation": random.randint(row.get("motivation_min", 1) or 1,
                                     row.get("motivation_max", 4) or 4),
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


def _mob_opponent_from_npc(row: dict, uma: dict, index: int, scale: float = 1.0) -> dict:
    mob_id = row.get("mob_id") or 0
    if mob_id:
        return {
            "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
            "trained_chara_id": row["id"],
            "single_mode_chara_id": row["id"],
            "nickname_id": 0,
            "card_id": 0,
            "mob_id": mob_id,
            "chara_id": 1,
            "rarity": 1,
            "talent_level": 1,
            "skill_array": _npc_skills(row),
            "speed": max(1, round(row["speed"] * scale)),
            "stamina": max(1, round(row["stamina"] * scale)),
            "power": max(1, round(row["pow"] * scale)),
            "guts": max(1, round(row["guts"] * scale)),
            "wiz": max(1, round(row["wiz"] * scale)),
            "running_style": _npc_running_style(row),
            "race_dress_id": 1,
            "proper_distance_short": row.get("proper_distance_short", 1),
            "proper_distance_mile": row.get("proper_distance_mile", 1),
            "proper_distance_middle": row.get("proper_distance_middle", 1),
            "proper_distance_long": row.get("proper_distance_long", 1),
            "proper_running_style_nige": row.get("proper_running_style_nige", 1),
            "proper_running_style_senko": row.get("proper_running_style_senko", 1),
            "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
            "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
            "proper_ground_turf": row.get("proper_ground_turf", 1),
            "proper_ground_dirt": row.get("proper_ground_dirt", 1),
            "motivation": row.get("motivation_max", 3) or 3,
        }
    card_id = uma["cardId"]
    chara_id = uma.get("charaId", 0)
    dress = row.get("race_dress_id") or trained_chara.dress_for_card(card_id, chara_id)
    if not dress or not chara_id:
        return _mob_shape_from_npc(row, index, scale)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": _MOB_ID_BASE + row["id"] * 100 + index,
        "single_mode_chara_id": row.get("chara_id", 0),
        "nickname_id": 0,
        "card_id": card_id,
        "mob_id": 0,
        "rarity": 1,
        "talent_level": 1,
        "skill_array": _npc_skills(row),
        "speed": max(1, round(row["speed"] * scale)),
        "stamina": max(1, round(row["stamina"] * scale)),
        "power": max(1, round(row["pow"] * scale)),
        "guts": max(1, round(row["guts"] * scale)),
        "wiz": max(1, round(row["wiz"] * scale)),
        "running_style": _npc_running_style(row),
        "race_dress_id": dress,
        "proper_distance_short": row.get("proper_distance_short", 1),
        "proper_distance_mile": row.get("proper_distance_mile", 1),
        "proper_distance_middle": row.get("proper_distance_middle", 1),
        "proper_distance_long": row.get("proper_distance_long", 1),
        "proper_running_style_nige": row.get("proper_running_style_nige", 1),
        "proper_running_style_senko": row.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": row.get("proper_ground_turf", 1),
        "proper_ground_dirt": row.get("proper_ground_dirt", 1),
        "motivation": random.randint(row.get("motivation_min", 1) or 1, row.get("motivation_max", 4) or 4),
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


_HAPPY_MEEK_CHARA = 2001
_HAPPY_MEEK_NPC_ID = 2001902
_HAPPY_MEEK_MAX_LEVEL = 5


def _meek_in_finals(full_state: dict, chara_info, finals_round) -> bool:
    return finals_round == 3 and scenarios.for_chara(chara_info).has_versus_npc


def _happy_meek_finals_level(full_state: dict) -> int:
    return max(1, min(_HAPPY_MEEK_MAX_LEVEL,
                      int(full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1) or 1)))


def _happy_meek_opponent(level: int = _HAPPY_MEEK_MAX_LEVEL) -> dict | None:
    raw = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?",
                                (_HAPPY_MEEK_NPC_ID,))
    if not raw:
        return None
    row = {k: raw[k] for k in raw.keys()}
    stats = single_mode_events.happy_meek_stats(level)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": _HAPPY_MEEK_NPC_ID,
        "single_mode_chara_id": _HAPPY_MEEK_CHARA,
        "nickname_id": 0, "card_id": 0, "mob_id": 0,
        "chara_id": _HAPPY_MEEK_CHARA,
        "race_dress_id": row["race_dress_id"] or 200101,
        "rarity": 5, "talent_level": 5, "skill_array": _npc_skills(row),
        "speed": stats["speed"], "stamina": stats["stamina"],
        "power": stats["power"], "guts": stats["guts"], "wiz": stats["wiz"],
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 7),
        "proper_distance_mile": row.get("proper_distance_mile", 7),
        "proper_distance_middle": row.get("proper_distance_middle", 7),
        "proper_distance_long": row.get("proper_distance_long", 7),
        "proper_running_style_nige": row.get("proper_running_style_nige", 2),
        "proper_running_style_senko": row.get("proper_running_style_senko", 7),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 7),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 2),
        "proper_ground_turf": row.get("proper_ground_turf", 7),
        "proper_ground_dirt": row.get("proper_ground_dirt", 7),
        "motivation": 5,
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


def _scenario_finals_rival(npc_id: int) -> dict | None:
    raw = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?",
                                (int(npc_id),))
    if not raw:
        return None
    row = {k: raw[k] for k in raw.keys()}
    chara_id = int(row.get("chara_id") or 0)
    if not master_data.has_portrait_art(chara_id):
        mob = _mob_shape_from_npc(row, 0)
        mob["motivation"] = 5
        return mob
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": int(npc_id),
        "single_mode_chara_id": chara_id,
        "nickname_id": 0, "card_id": 0, "mob_id": 0,
        "chara_id": chara_id,
        "race_dress_id": row.get("race_dress_id") or 1,
        "rarity": 5, "talent_level": 5, "skill_array": _npc_skills(row),
        "speed": row.get("speed") or 100, "stamina": row.get("stamina") or 100,
        "power": row.get("pow") or 100, "guts": row.get("guts") or 100,
        "wiz": row.get("wiz") or 100,
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 7),
        "proper_distance_mile": row.get("proper_distance_mile", 7),
        "proper_distance_middle": row.get("proper_distance_middle", 7),
        "proper_distance_long": row.get("proper_distance_long", 7),
        "proper_running_style_nige": row.get("proper_running_style_nige", 2),
        "proper_running_style_senko": row.get("proper_running_style_senko", 7),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 7),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 2),
        "proper_ground_turf": row.get("proper_ground_turf", 7),
        "proper_ground_dirt": row.get("proper_ground_dirt", 7),
        "motivation": 5,
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


def _scenario_finals_rivals(chara_info, finals_round) -> list[dict]:
    if finals_round != 3:
        return []
    ids = scenarios.for_chara(chara_info).finals_rival_npc_ids or ()
    return [o for o in (_scenario_finals_rival(i) for i in ids) if o]


_FIELD_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "career_field_rules.json"
_NAMED_BY_GRADE = {100: 7, 200: 5, 300: 3, 400: 1}
_FACTOR_ROLL = 0.05


@functools.lru_cache(maxsize=1)
def _field_rule_tables() -> dict:
    try:
        with open(_FIELD_RULES_PATH) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        log.exception("career_field_rules.json unreadable; career fields fall back")
        raw = {}
    out = {}
    for name in ("factor", "named", "skill_drop"):
        out[name] = {tuple(int(x) for x in k.split(",")): v
                     for k, v in (raw.get(name) or {}).items()}
    out["mob_groups"] = {int(k): {int(g): w for g, w in v.items()}
                         for k, v in (raw.get("mob_groups") or {}).items()}
    out["field_drop"] = {int(k): v for k, v in (raw.get("field_drop") or {}).items()}
    return out


@functools.lru_cache(maxsize=512)
def _program_race(program_id) -> dict:
    if not program_id:
        return {}
    row = master_data.query_one(
        "SELECT r.grade, r.entry_num, p.entry_decrease, ri.npc_group_id, "
        "cs.ground, cs.distance FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id "
        "JOIN race_course_set cs ON cs.id = r.course_set WHERE p.id = ?", (int(program_id),))
    return dict(row) if row else {}


def _nearest(table: dict, program_id, turn: int):
    if (program_id, turn) in table:
        return table[(program_id, turn)]
    same = [k for k in table if k[0] == program_id]
    pool = same or list(table)
    if not pool:
        return None
    return table[min(pool, key=lambda k: (abs(k[1] - turn), k))]


def _field_rules(program_id, turn: int) -> dict:
    t = _field_rule_tables()
    pid = int(program_id or 0)
    named = t["named"].get((pid, turn))
    if named is None:
        named = _NAMED_BY_GRADE.get(_program_race(pid).get("grade"), 0)
    return {"factor": _nearest(t["factor"], pid, turn) or [1.0] * 5,
            "named": named,
            "skill_drop": _nearest(t["skill_drop"], pid, turn) or [0, 0.0]}


def _field_size(program_id, entry_num: int, rng: random.Random) -> int:
    dec = int(_program_race(program_id).get("entry_decrease") or 0)
    if dec <= 0:
        return entry_num
    weights = _field_rule_tables()["field_drop"].get(dec) or [1] * (dec + 1)
    return max(2, entry_num - rng.choices(range(len(weights)), weights=weights)[0])


def _apply_field_rules(opp: dict, row: dict, rules: dict, rng: random.Random,
                       trim_skills: bool) -> dict:
    for key, col, base in zip(("speed", "stamina", "power", "guts", "wiz"),
                              ("speed", "stamina", "pow", "guts", "wiz"), rules["factor"]):
        opp[key] = max(1, round(row[col] * (base + rng.uniform(0, _FACTOR_ROLL))))
    if trim_skills:
        k, p = rules["skill_drop"]
        drop = k + (1 if rng.random() < p else 0)
        skills = list(opp.get("skill_array") or [])
        keep = max(0, len(skills) - drop)
        opp["skill_array"] = [skills[i] for i in sorted(rng.sample(range(len(skills)), keep))]
    lo = row.get("motivation_min") or 1
    opp["motivation"] = rng.randint(lo, max(lo, row.get("motivation_max") or lo))
    return opp


def _field_rng(chara_info: dict, program_id, salt: str) -> random.Random:
    key = (f"{chara_info.get('start_time') or ''}|{chara_info.get('card_id') or 0}|"
           f"{chara_info.get('turn') or 0}|{program_id or 0}|{salt}")
    return random.Random(zlib.crc32(key.encode()))


def _surface_ok(row: dict, race: dict, floor: int) -> bool:
    col = "proper_ground_dirt" if race.get("ground") == 2 else "proper_ground_turf"
    return (row.get(col) or 0) >= floor


def _distance_col(distance: int) -> str:
    return ("proper_distance_short" if distance <= 1400 else
            "proper_distance_mile" if distance <= 1800 else
            "proper_distance_middle" if distance <= 2400 else "proper_distance_long")


def _mob_opponents(chara_info: dict, count: int, exclude_charas=(), program_id=None,
                   named_taken: int = 0) -> list[dict]:
    turn = int(chara_info.get("turn") or 0)
    rules = _field_rules(program_id, turn)
    race = _program_race(program_id)
    rows = [dict(r) for r in master_data.query(
        "SELECT * FROM single_mode_npc WHERE mob_id <= ?", (race_mobs.MAX_RACE_MOB_ID,))]
    if not rows:
        return []
    exclude = set(exclude_charas or ()) | {(chara_info.get("card_id") or 0) // 100} - {0}
    rng = _field_rng(chara_info, program_id, "field")
    used, opponents = set(), []

    n_named = max(0, min(count, rules["named"] - named_taken))
    if n_named:
        dist = _distance_col(race.get("distance") or 2000)
        uma_pool = trained_chara._UMA_DATA
        cands = [r for r in rows
                 if not r["mob_id"] and r["npc_group_id"] == 0 and r["chara_id"] > 1
                 and r["id"] == r["chara_id"] * 10 and r["chara_id"] not in exclude
                 and _surface_ok(r, race, 7) and (r[dist] or 0) >= 7]
        rng.shuffle(cands)
        for row in cands:
            if len(opponents) >= n_named:
                break
            uma = _uma_for_chara(uma_pool, row)
            if uma is None:
                continue
            opp = _mob_opponent_from_npc(row, uma, len(opponents))
            opponents.append(_apply_field_rules(opp, row, rules, rng, trim_skills=True))
            exclude.add(row["chara_id"])

    mob_rows = [r for r in rows if (r["mob_id"] or 0) > 0 and 0 < r["npc_group_id"] < 50]
    mob_dist = _distance_col(race.get("distance") or 2000)
    by_group = {}
    for r in mob_rows:
        if not race or (_surface_ok(r, race, 5) and (r[mob_dist] or 0) >= 4):
            by_group.setdefault(r["npc_group_id"], []).append(r)
    weights = (_field_rule_tables()["mob_groups"].get(race.get("npc_group_id"))
               or {g: 1 for g in by_group})
    groups = [g for g in weights if by_group.get(g)]
    while len(opponents) < count and groups:
        g = rng.choices(groups, weights=[weights[x] for x in groups])[0]
        free = [r for r in by_group[g] if r["id"] not in used]
        if not free:
            groups.remove(g)
            continue
        row = rng.choice(free)
        used.add(row["id"])
        opp = _mob_opponent_from_npc(row, None, len(opponents))
        opponents.append(_apply_field_rules(opp, row, rules, rng, trim_skills=True))

    seen_ids = set()
    for o in opponents:
        while o["trained_chara_id"] in seen_ids:
            o["trained_chara_id"] += 1000
        seen_ids.add(o["trained_chara_id"])
        o["single_mode_chara_id"] = o["trained_chara_id"] if o.get("mob_id") \
            else o["single_mode_chara_id"]
    return opponents


_UNKNOWN_RACE_RANK = 999


_RIVAL_CONDITION_UNCONDITIONAL = 1
_RIVAL_CONDITION_BRANCH_FLAG = 3


@functools.lru_cache(maxsize=64)
def _active_branch_flags(route_race_id_array: tuple) -> frozenset:
    if not route_race_id_array:
        return frozenset()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    dropped = _dropped_alternative_ids(route_race_id_array)
    rows = master_data.query(
        f"SELECT id, determine_race_flag AS flag FROM single_mode_route_race "
        f"WHERE id IN ({ids}) AND determine_race_flag != 0")
    return frozenset(r["flag"] for r in rows if r["id"] not in dropped)


def _uma_for_chara(uma_pool, npc):
    chara_id = npc.get("chara_id")
    if not chara_id:
        return None
    for u in uma_pool:
        if (u.get("charaId") or (u.get("cardId") or 0) // 100) == chara_id:
            return u
    return None


def _rivals_for_turn(rows, turn: int) -> list:
    best = {}
    for row in rows:
        chara = row["rival_chara_id"]
        row_turn = row["rival_turn"] or 0
        rank = (0, abs(row_turn - int(turn or 0))) if row_turn else (1, 0)
        if chara not in best or rank < best[chara][0]:
            best[chara] = (rank, row)
    return [row for _rank, row in best.values()]


def _scripted_rivals(chara_info: dict, program_id, limit: int) -> list:
    player_chara = (chara_info.get("card_id") or 0) // 100
    if not player_chara or not program_id or limit <= 0:
        return []
    rows = master_data.query(
        "SELECT r.rival_chara_id, r.frame_order, r.condition_type, r.rival_flag_id, "
        "r.turn AS rival_turn, n.* "
        "FROM single_mode_rival r JOIN single_mode_npc n ON n.id = r.single_mode_npc_id "
        "WHERE r.chara_id=? AND r.race_program_id=? AND r.condition_type IN (?, ?) "
        "ORDER BY r.id",
        (player_chara, program_id,
         _RIVAL_CONDITION_UNCONDITIONAL, _RIVAL_CONDITION_BRANCH_FLAG))
    if rows:
        flags = _active_branch_flags(tuple(chara_info.get("route_race_id_array") or ()))
        rows = [r for r in rows
                if r["condition_type"] != _RIVAL_CONDITION_BRANCH_FLAG
                or r["rival_flag_id"] in flags]
        branched = {r["rival_chara_id"] for r in rows
                    if r["condition_type"] == _RIVAL_CONDITION_BRANCH_FLAG}
        if branched:
            rows = [r for r in rows
                    if r["condition_type"] == _RIVAL_CONDITION_BRANCH_FLAG
                    or r["rival_chara_id"] not in branched]
    rows = _rivals_for_turn(rows, chara_info.get("turn") or 0)
    if not rows:
        return []
    turn = int(chara_info.get("turn") or 0)
    rules = _field_rules(program_id, turn)
    rng = _field_rng(chara_info, program_id, "rivals")
    uma_pool = trained_chara._UMA_DATA
    out = []
    for i, row in enumerate(rows[:limit]):
        npc = dict(row)
        if not npc.get("rival_chara_id"):
            continue
        if npc["rival_chara_id"] == player_chara:
            continue
        uma = _uma_for_chara(uma_pool, npc)
        if uma is None:
            continue
        out.append(_apply_field_rules(_mob_opponent_from_npc(npc, uma, i), npc, rules, rng,
                                      trim_skills=False))
    return out


def _dedupe_field_faces(horses) -> int:
    pool = trained_chara._UMA_DATA

    def chara_of(h):
        cid = int(h.get("chara_id") or 0)
        return cid if cid > 1 else (int(h.get("card_id") or 0)) // 100

    def refaceable(h):
        return not h.get("viewer_id") and not h.get("mob_id") and h.get("card_id")

    seen, fixed = set(), 0
    for horse in horses:
        chara = chara_of(horse)
        if chara and not refaceable(horse):
            if chara in seen:
                log.error("duplicate uma in one field: chara %s appears twice "
                          "and neither one is a re-faceable filler", chara)
            seen.add(chara)
    for horse in horses:
        chara = chara_of(horse)
        if not chara or not refaceable(horse):
            continue
        if chara not in seen:
            seen.add(chara)
            continue
        for uma in pool:
            new_chara = uma.get("charaId") or (uma.get("cardId") or 0) // 100
            if not new_chara or new_chara in seen:
                continue
            dress = trained_chara.dress_for_card(uma["cardId"], new_chara)
            if not dress:
                continue
            horse["card_id"] = uma["cardId"]
            horse["race_dress_id"] = dress
            seen.add(new_chara)
            fixed += 1
            break
        else:
            log.error("duplicate uma in one field: chara %s repeats and the "
                      "face pool is exhausted", chara)
    return fixed


def _simulate_career_race(chara_info: dict, viewer_id, race_instance_id: int,
                          running_style: int | None = None,
                          with_happy_meek: bool = False,
                          happy_meek_level: int = _HAPPY_MEEK_MAX_LEVEL,
                          finals_rivals: list | None = None,
                          program_id=None) -> dict | None:
    try:
        player = _career_player_race_chara(chara_info, viewer_id, running_style)
        conditions.apply_race_buffs(player, program_id)
        scenario = scenarios.for_chara(chara_info)
        scenario.race_player(viewer_id, chara_info, player, program_id)
        facts = scenario.snapshot(state_store.get_state(viewer_id) or {})
        if facts.get("scenario_data_array"):
            player["scenario_data_array"] = facts["scenario_data_array"]
        player["race_context"] = {**(player.get("race_context") or {}), "scenarioId": chara_info.get("scenario_id"), "scenarioFacts": facts}
        course_info = race_simulator.get_course_for_race_instance(race_instance_id)
        if course_info is None:
            return None
        _course_set_id, _course, entry_num = course_info
        field_size = max(0, _field_size(program_id, entry_num,
                                        _field_rng(chara_info, program_id, "size")) - 1)
        rivals = _scripted_rivals(chara_info, program_id, field_size)
        scripted_charas = {(o.get("card_id") or 0) // 100 for o in rivals if o.get("card_id")}
        own_field = scenarios.for_chara(chara_info).race_field_for(
            viewer_id, chara_info, program_id, max(0, field_size - len(rivals)))
        opponents = rivals + (own_field if own_field is not None else _mob_opponents(
            chara_info, max(0, field_size - len(rivals)), exclude_charas=scripted_charas,
            program_id=program_id, named_taken=len(rivals)))
        if not opponents:
            return None
        _dedupe_field_faces([player] + opponents)
        meek_index = None
        if with_happy_meek:
            meek = _happy_meek_opponent(happy_meek_level)
            if meek:
                opponents[0] = meek
                meek_index = 1
        for offset, rival in enumerate(finals_rivals or ()):
            slot = (1 if meek_index else 0) + offset
            if slot >= len(opponents):
                break
            opponents[slot] = rival
        cond = _career_race_conditions(program_id, chara_info)
        forced_gates = None
        if cond.get("frame_order"):
            stalls = max(entry_num or 0, 1 + len(opponents), int(cond["frame_order"]))
            mine = int(cond["frame_order"]) - 1
            others = [g for g in range(stalls) if g != mine]
            forced_gates = [mine] + _field_rng(chara_info, program_id, "gate").sample(others, len(opponents))
        tuning.scale_rivals(opponents)
        result = race_simulator.simulate_race(
            player, opponents, race_instance_id, ground_condition=cond["ground_condition"],
            weather=cond["weather"], season=cond["season"], single_mode=True, gate_assignment=forced_gates,
            race_context={"subWeather": cond.get("sub_weather", 0), "viewer_id": viewer_id})
        if result is None:
            return None

        horses = result["horses"]
        sim_results = result["sim_results"]

        def _rank_by(key):
            order = sorted(range(len(horses)), key=key)
            return {idx: rank + 1 for rank, idx in enumerate(order)}

        popularity_by_index = _rank_by(lambda i: -horses[i]["speed"])
        stamina_rank_by_index = _rank_by(lambda i: -horses[i]["stamina"])
        power_rank_by_index = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))

        gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
        race_horse_data = []
        for i, h in enumerate(horses):
            rank = chara_info.get("chara_grade", 1) if i == 0 else h.get("rank", 1)
            race_horse_data.append(practice_race._build_race_horse_entry(
                h, frame_order=gate_assignment[i] + 1, final_grade=rank,
                popularity=popularity_by_index[i],
                popularity_mark_rank_array=[popularity_by_index[i], stamina_rank_by_index[i],
                                            power_rank_by_index[i]],
            ))

        player_order = sim_results[0]["finishOrder"] + 1
        meek_order = (sim_results[meek_index]["finishOrder"] + 1
                      if meek_index is not None and meek_index < len(sim_results)
                      else None)
        rival_finish_orders = {}
        for i, h in enumerate(horses):
            if not i:
                continue
            chara = (h.get("card_id") or 0) // 100
            if chara and i < len(sim_results):
                rival_finish_orders.setdefault(chara, sim_results[i]["finishOrder"] + 1)
        return {
            "race_horse_data": race_horse_data,
            "random_seed": result["seed"],
            "race_scenario": result["race_scenario"],
            "player_finish_order": player_order,
            "player_finish_time": int(round(
                (sim_results[0].get("finishTimeDisplay")
                 or sim_results[0].get("finishTimeRaw") or 0.0) * 10000)),
            "player_popularity": popularity_by_index[0],
            "rival_finish_orders": rival_finish_orders,
            "meek_finish_order": meek_order,
            "beat_happy_meek": (meek_order is not None and player_order < meek_order),
            "player_rushed_count": sum(
                1 for e in (result.get("skill_events") or ())
                if e.get("horseIndex") == 0 and e.get("skillId") == "kakari"),
            "player_skills_used": len({
                e.get("skillId") for e in (result.get("skill_events") or ())
                if e.get("horseIndex") == 0 and isinstance(e.get("skillId"), int)}),
            "player_overtakes": ((result.get("final_stretch_overtakes") or [0])[0]
                                 if result.get("final_stretch_overtakes") else 0),
        }
    except Exception:
        log.exception("career race simulation failed, falling back to static fixture replay")
        return None


def _resolve_and_enter_race(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    pair = fixtures.first("single_mode/race_entry") or fixtures.first("single_mode_team/race_entry")
    if pair is None:
        raise LookupError("No captured race_entry fixture available")
    response = pair.response_copy()
    data = response.get("data", {})
    rsi = data.get("race_start_info", {}) or {}
    program_id = payload.get("program_id") or rsi.get("program_id")

    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    chara_info = career["data"]["chara_info"] if isinstance(career, dict) else None
    career_home = career["data"].get("home_info") if isinstance(career, dict) else None
    if chara_info is None:
        log.warning("race entry for viewer %s with NO active career state -- "
                    "serving raw fixture replay (race will not match the run)", viewer_id)
    conflict = _client_turn_conflict(payload, full_state, chara_info)
    if conflict is not None:
        return _stale_turn_refusal(viewer_id, "single_mode/race_entry", conflict, chara_info)
    player_style = _player_running_style(full_state, chara_info or {})
    _patch_player_horse(rsi, chara_info, running_style=player_style)
    current_turn = payload.get("current_turn")
    if current_turn is None and chara_info is not None:
        current_turn = chara_info.get("turn")
    race_history = career["data"].get("race_history", []) if isinstance(career, dict) else []
    race_info = _race_info_for_program(payload["program_id"]) if payload.get("program_id") else None
    if (race_info and chara_info
            and _debut_retry_active(chara_info, current_turn, race_history)
            and _race_grade_of(race_info["program_id"]) not in
            (_MAIDEN_GRADE, _DEBUT_GRADE)):
        log.info("race %s refused: maiden not yet won (debut retry active)",
                 race_info["program_id"])
        race_info = None
    if not race_info and chara_info:
        race_info = _race_for_turn(chara_info, current_turn, race_history)
    if not race_info and chara_info:
        if _debut_retry_active(chara_info, current_turn, race_history):
            race_info = _debut_retry_race(chara_info, current_turn)
    if not race_info and chara_info:
        race_info = _voluntary_race_for_turn(chara_info, current_turn)
    chara_id = (chara_info or {}).get("card_id", 0) // 100
    finals_round = _finals_round_for_program((race_info or {}).get("program_id"))
    is_goal = _is_goal_race(chara_info or {}, (race_info or {}).get("program_id") or program_id,
                            current_turn, race_history)
    challenge_stat = None
    prep_chain = []
    if finals_round:
        data["unchecked_event_array"] = [
            _finals_event_entry(finals_round, "pre", chara_info)]
    elif not is_goal and race_info and chara_info is not None:
        data["unchecked_event_array"], challenge_stat = _roll_challenge_event(chara_info)
    elif (is_goal and chara_info is not None and race_info
          and scenarios.for_chara(chara_info).goal_race_entry_events(
              full_state, chara_info, race_info.get("program_id"), current_turn) is not None):
        entries = scenarios.for_chara(chara_info).goal_race_entry_events(
            full_state, chara_info, race_info.get("program_id"), current_turn)
        data["unchecked_event_array"], prep_chain = entries[:1], entries[1:]
    elif (is_goal and chara_info is not None
          and scenarios.for_chara(chara_info).replaces_goal_race_event):
        data["unchecked_event_array"] = []
    else:
        _retarget_race_prep_event(data.get("unchecked_event_array"), chara_id,
                                  race_name=(race_info or {}).get("story_name"),
                                  chara_info=chara_info, turn=current_turn)
    if race_info:
        program_id = race_info["program_id"]
        rsi["program_id"] = program_id
        rsi.update({k: v for k, v in _career_race_conditions(program_id, chara_info).items()
                    if k != "frame_order"})

    sim = None
    if chara_info is not None and race_info and race_info.get("race_instance_id"):
        sim = _simulate_career_race(chara_info, viewer_id, race_info["race_instance_id"],
                                    running_style=player_style,
                                    program_id=race_info.get("program_id"),
                                    with_happy_meek=_meek_in_finals(
                                        full_state, chara_info, finals_round),
                                    happy_meek_level=_happy_meek_finals_level(full_state),
                                    finals_rivals=_scenario_finals_rivals(
                                        chara_info, finals_round))
        if sim:
            rsi["race_horse_data"] = sim["race_horse_data"]
            rsi["random_seed"] = sim["random_seed"]
        else:
            log.warning("race entry for viewer %s (program %s): simulation unavailable -- "
                        "falling back to static fixture replay, and the reward path below "
                        "won't know the real finish order", viewer_id, program_id)

    if chara_info is not None:
        race_ci = copy.deepcopy(chara_info)
        race_ci["playing_state"] = 2
        race_ci["state"] = 0
        race_ci["race_program_id"] = program_id
        data["chara_info"] = race_ci

    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)

    full_state[RACE_CTX_KEY] = {
        "program_id": program_id,
        "turn": current_turn,
        "race_start_info": copy.deepcopy(rsi),
        "race_story_name": (race_info or {}).get("story_name"),
        "prep_chain": prep_chain,
        "prep_served": (data.get("unchecked_event_array") or [{}])[0].get("event_id"),
        "is_goal": is_goal,
        _CHALLENGE_STAT_KEY: challenge_stat,
        "race_scenario": (sim or {}).get("race_scenario"),
        "player_finish_order": (sim or {}).get("player_finish_order", _UNKNOWN_RACE_RANK),
        "player_finish_time": (sim or {}).get("player_finish_time"),
        "player_popularity": (sim or {}).get("player_popularity"),
        "rival_finish_orders": (sim or {}).get("rival_finish_orders"),
        "player_rushed_count": (sim or {}).get("player_rushed_count"),
        "player_skills_used": (sim or {}).get("player_skills_used"),
        "player_overtakes": (sim or {}).get("player_overtakes"),
        "beat_happy_meek": (sim or {}).get("beat_happy_meek", False),
        "finals_round": finals_round,
        "running_style": player_style,
        "race_instance_id": (race_info or {}).get("race_instance_id"),
        "vital_at_entry": (chara_info or {}).get("vital"),
    }
    if isinstance(career_home, dict):
        career_home.update(_continue_counts(full_state, full_state[RACE_CTX_KEY]))
        data["home_info"] = copy.deepcopy(career_home)
    _apply_versus(data, full_state, current_turn)
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def handle_ura_race_entry(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    chara_info = _career_chara_info(viewer_id)
    current_turn = payload.get("current_turn") or (chara_info or {}).get("turn")
    stale = (ctx.get("turn") is not None and current_turn is not None
             and ctx.get("turn") != current_turn)
    mismatched = (payload.get("program_id") and ctx.get("program_id")
                  and payload.get("program_id") != ctx.get("program_id"))
    if not ctx.get("race_start_info") or chara_info is None or stale or mismatched:
        if stale or mismatched:
            log.warning("race_entry: discarding stale race ctx (ctx turn %s prog %s "
                        "vs current turn %s prog %s) -- resolving fresh",
                        ctx.get("turn"), ctx.get("program_id"),
                        current_turn, payload.get("program_id"))
            full_state.pop(RACE_CTX_KEY, None)
            state_store.save_state(viewer_id, full_state)
        return _resolve_and_enter_race(payload)

    pair = fixtures.first("single_mode/race_entry")
    response = pair.response_copy() if pair is not None else {
        "response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": {}}
    data = response.setdefault("data", {})
    career = full_state.get(STATE_KEY)
    if ctx.get(_CHALLENGE_STAT_KEY) and payload.get("event_id") and isinstance(career, dict):
        chara_info = career["data"]["chara_info"]
        _pay_challenge_stat(full_state, chara_info, data)
    race_ci = copy.deepcopy(chara_info)
    race_ci["playing_state"] = 2
    race_ci["state"] = 0
    race_ci["race_program_id"] = ctx.get("program_id")
    data["chara_info"] = race_ci
    data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
    career_home = career["data"].get("home_info") if isinstance(career, dict) else None
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def handle_change_running_style(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    style = payload.get("running_style")
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if style in (1, 2, 3, 4) and isinstance(career, dict):
        full_state[STYLE_CHOICE_KEY] = style
        ci = career["data"]["chara_info"]
        ci["race_running_style"] = style
        ctx = full_state.get(RACE_CTX_KEY) or {}
        cur = payload.get("current_turn") or ci.get("turn")
        if ctx.get("race_start_info") and ctx.get("turn") == cur:
            rid = ctx.get("race_instance_id")
            if not rid and ctx.get("program_id"):
                rid = (_race_info_for_program(ctx["program_id"]) or {}).get("race_instance_id")
            sim = (_simulate_career_race(
                ci, viewer_id, rid, running_style=style,
                program_id=ctx.get("program_id"),
                with_happy_meek=_meek_in_finals(full_state, ci,
                                                ctx.get("finals_round")),
                happy_meek_level=_happy_meek_finals_level(full_state),
                finals_rivals=_scenario_finals_rivals(
                    ci, ctx.get("finals_round"))) if rid else None)
            if sim:
                rsi = ctx["race_start_info"]
                rsi["race_horse_data"] = sim["race_horse_data"]
                rsi["random_seed"] = sim["random_seed"]
                ctx["race_scenario"] = sim["race_scenario"]
                ctx["player_finish_order"] = sim["player_finish_order"]
                ctx["player_finish_time"] = sim.get("player_finish_time")
                ctx["player_popularity"] = sim.get("player_popularity")
                ctx["rival_finish_orders"] = sim.get("rival_finish_orders")
                ctx["beat_happy_meek"] = sim.get("beat_happy_meek", False)
            else:
                rhd = (ctx.get("race_start_info") or {}).get("race_horse_data") or []
                if rhd and isinstance(rhd[0], dict):
                    rhd[0]["running_style"] = style
            ctx["running_style"] = style
            full_state[RACE_CTX_KEY] = ctx
        state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}


def _drain_pending(full_state: dict, resolved_event_id, player_chara: int) -> list:
    pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
    if resolved_event_id in pending:
        pending.remove(resolved_event_id)
    full_state[single_mode_events.PENDING_EVENTS_KEY] = pending
    if pending and single_mode_events.npcs_unlocked_by(pending[0]):
        nxt = pending[0]
        full_state[single_mode_events.PENDING_EVENTS_KEY] = pending[1:]
        return [single_mode_events.event_entry(nxt, player_chara)]
    already_active = career_events.active(full_state)
    if already_active is not None:
        return [already_active.wire_entry()]
    goal_entry = full_state.pop(GOAL_EVENT_KEY, None)
    if goal_entry:
        return [goal_entry]
    wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
    if wire is not None:
        return [wire]
    extra = list(full_state.get(single_mode_events.EXTRA_EVENTS_KEY, []))
    if extra:
        nxt = extra.pop(0)
        full_state[single_mode_events.EXTRA_EVENTS_KEY] = extra
        return [nxt]
    if pending:
        return [single_mode_events.event_entry(pending[0], player_chara)]
    return []


SCENARIO_SLOT_RESERVED_KEY = "_scenario_slot_reserved"


def _scenario_interstitial_pending(full_state: dict, career: dict,
                                   payload: dict, turn=None) -> bool:
    chara_info = career["data"]["chara_info"]
    if turn is None:
        turn = payload.get("current_turn")
    if turn is None:
        turn = (chara_info.get("turn") or 1) - 1
    return scenarios.for_chara(chara_info).interstitial_pending(
        full_state, chara_info, turn)


@contextlib.contextmanager
def _scenario_slot_reserved(full_state: dict, active: bool):
    if not active:
        yield
        return
    full_state[SCENARIO_SLOT_RESERVED_KEY] = True
    try:
        yield
    finally:
        full_state.pop(SCENARIO_SLOT_RESERVED_KEY, None)


CAREER_FAILED_FLAG_KEY = "_career_failed_this_response"


def _career_has_failed(full_state: dict) -> bool:
    if (full_state or {}).get(CAREER_FAILED_FLAG_KEY):
        return True
    career = (full_state or {}).get(STATE_KEY)
    if not isinstance(career, dict):
        return False
    return (career.get("data", {}).get("chara_info") or {}).get("state") == 2


def _queue_turn_event(response: dict, full_state: dict, entry: dict,
                      front: bool = False, fail_chain: bool = False) -> None:
    if not fail_chain and _career_has_failed(full_state):
        return
    shown = response["data"].get("unchecked_event_array") or []
    if not shown and not full_state.get(SCENARIO_SLOT_RESERVED_KEY):
        response["data"]["unchecked_event_array"] = [entry]
        return
    queue = full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, [])
    if not shown:
        if front:
            queue.insert(0, entry)
        else:
            queue.append(entry)
        return
    if front:
        full_state[single_mode_events.EXTRA_EVENTS_KEY] = list(shown) + list(queue)
        response["data"]["unchecked_event_array"] = [entry]
    else:
        queue.append(entry)


def _note_not_up(data: dict, not_up) -> None:
    if not not_up:
        return
    if not isinstance(not_up, dict):
        not_up = {"status_type_array": list(not_up)}
    info = data.setdefault("not_up_parameter_info", {})
    for field, ids in not_up.items():
        if ids:
            info[field] = sorted(set(info.get(field) or []) | set(ids))


def _emit_turn_event(response: dict, full_state: dict, event: career_events.Event) -> bool:
    accepted = career_events.emit(full_state, event)
    if accepted:
        shown = response["data"].get("unchecked_event_array") or []
        if not shown and not full_state.get(SCENARIO_SLOT_RESERVED_KEY):
            wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
            if wire is not None:
                response["data"]["unchecked_event_array"] = [wire]
    return accepted


def _handle_ura_check_event(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    career = full_state.get(STATE_KEY)
    chara_info = career["data"]["chara_info"] if isinstance(career, dict) else None

    if not payload.get("program_id"):
        reward = full_state.pop(RACE_REWARD_KEY, None)
        if reward and chara_info is not None:
            owed = _apply_race_reward(chara_info, reward)
            if owed:
                full_state[RACE_REWARD_NOT_UP_KEY] = owed
            state_store.save_state(viewer_id, full_state)

    if payload.get("program_id") and not payload.get("event_id"):
        return _resolve_and_enter_race(payload)

    if ctx.get("race_start_info") and chara_info is not None:
        response = _load_ura_race_seed("race_check_event")
        data = response["data"]
        if ctx.get(_CHALLENGE_STAT_KEY):
            _pay_challenge_stat(full_state, chara_info, data)
            state_store.save_state(viewer_id, full_state)
        race_ci = copy.deepcopy(chara_info)
        race_ci["playing_state"] = 2
        race_ci["state"] = 0
        race_ci["race_program_id"] = ctx.get("program_id")
        data["chara_info"] = race_ci
        data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
        if data.get("race_condition_array"):
            data["race_condition_array"][0]["program_id"] = ctx.get("program_id")
        for key in ("home_info", "ura_data_set"):
            persisted = career["data"].get(key)
            if key in data and isinstance(persisted, dict):
                data[key] = copy.deepcopy(persisted)
        data["unchecked_event_array"] = []
        chain = ctx.get("prep_chain") or []
        if chain and payload.get("event_id") == ctx.get("prep_served"):
            nxt = chain.pop(0)
            ctx["prep_chain"], ctx["prep_served"] = chain, nxt.get("event_id")
            state_store.save_state(viewer_id, full_state)
            data["unchecked_event_array"] = [copy.deepcopy(nxt)]
        return response

    try:
        if _record_gate_choice(full_state, payload.get("event_id"),
                               payload.get("choice_number")):
            state_store.save_state(viewer_id, full_state)
    except Exception:
        log.exception("gate choice record failed; continuing")

    if chara_info is None:
        return _no_career_refusal(viewer_id, "single_mode/check_event", full_state)
    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    data["unchecked_event_array"] = []

    updated = copy.deepcopy(chara_info)
    if updated.get("playing_state") not in scenarios.for_chara(updated).playing_states:
        updated["playing_state"] = 1
    career_home = career["data"].get("home_info")
    unlocked = full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])
    turn = payload.get("current_turn", updated.get("turn"))
    event_id = payload.get("event_id")
    ce_resolution = career_events.resolve(full_state, updated, event_id, payload.get("choice_number"),
                                          career=career, current_turn=turn)
    _note_not_up(data, full_state.pop(RACE_REWARD_NOT_UP_KEY, None))
    if ce_resolution is not None:
        _note_not_up(data, ce_resolution.extra.get("not_up"))
        _note_not_up(data, (ce_resolution.applied or {}).get("not_up"))
    if ce_resolution is not None and "factors" in ce_resolution.extra:
        data["event_effected_factor_array"] = ce_resolution.extra["factors"]
    if isinstance(career_home, dict):
        _sync_infirmary_lock(updated, career_home, turn)
    scenarios.for_chara(updated).on_event_resolved(full_state, updated, event_id)
    scenarios.for_chara(updated).on_event_choice(full_state, updated, event_id,
                                                 payload.get("choice_number"))
    _note_not_up(data, full_state.pop(event_engine.PENDING_NOT_UP_KEY, None))
    if "race_condition_array" in data:
        data["race_condition_array"] = _build_race_condition_array(
            updated, turn, career["data"].get("race_history", []))


    cev_ctx = _career_ctx_for(full_state, event_id)
    if cev_ctx:
        ev, _ = _career_event_from_ctx(full_state, event_id)
        served_branch = _served_branch(full_state, event_id,
                                       payload.get("choice_number", 1))
        if ev:
            dcid = event_engine.source_default_chara(cev_ctx.get("source"), cev_ctx.get("source_id"))
            applied = event_engine.apply_choice(
                updated, ev, payload.get("choice_number", 1), dcid,
                source_card_id=_event_source_card(cev_ctx),
                full_state=full_state,
                chain_owner=event_engine.chain_owner_token(
                    cev_ctx.get("source"), cev_ctx.get("source_id")),
                branch=served_branch) or {}
            _note_not_up(data, applied.get("not_up"))
        meeting_ctx = _pal_ctx_for(full_state, cev_ctx.get("story_id"))
        meeting_card = (meeting_ctx.get("card_id")
                        if meeting_ctx and meeting_ctx.get("kind") == "first" else None)
        try:
            _commit_pal_event(full_state, updated, payload.get("choice_number", 1),
                              ev, served_branch, story_id=cev_ctx.get("story_id"))
        except Exception:
            log.exception("pal/group outing commit failed; event effects kept")
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            _sync_infirmary_lock(updated, career_home, turn)
            data["home_info"] = copy.deepcopy(career_home)
        _apply_versus(data, full_state, turn)
        player_chara = (updated.get("card_id") or 0) // 100
        data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
        career["data"]["chara_info"] = updated
        if meeting_card is not None:
            _maybe_unlock_after_first_meeting(
                response, full_state, career, meeting_card)
        if cev_ctx.get("queued_turn") is not None:
            log.info("SUPPORT_FIRE_RATE resolved event_id=%s queued_turn=%s "
                     "resolved_turn=%s pending_span=%s", event_id,
                     cev_ctx.get("queued_turn"), turn,
                     (turn - cev_ctx.get("queued_turn")) if turn is not None else None)
        _drop_career_ctx(full_state, cev_ctx)
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response


    if event_id == _CRANE_INTRO_EVENT and ce_resolution is not None:
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = []
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response


    payouts = full_state.get(ENDING_PAYOUT_KEY) or {}
    if event_id is not None and str(event_id) in payouts:
        pay = payouts.pop(str(event_id))
        gains = pay.get("gains")
        if gains is not None:
            named = [k for k in _ALL_STATS if gains.get(k, 0) > 0]
            owed = event_engine.capped_stat_codes(updated if named else None, named)
            for stat in named:
                event_engine.add_stat(updated, stat, gains[stat])
            updated["skill_point"] = updated.get("skill_point", 0) + gains.get("skill_point", 0)
        else:
            owed = event_engine.capped_stat_codes(
                updated if pay.get("stats", 0) > 0 else None, _ALL_STATS)
            for stat in _ALL_STATS:
                event_engine.add_stat(updated, stat, pay.get("stats", 0))
            updated["skill_point"] = updated.get("skill_point", 0) + pay.get("sp", 0)
        full_state[ENDING_PAYOUT_KEY] = payouts
        log.info("ending payout on event %s: +%s all stats, +%s SP%s",
                 event_id, pay.get("stats"), pay.get("sp"),
                 " (at ceiling -> not_up_parameter_info %s)" % owed if owed else "")
        _note_not_up(data, owed)
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        player_chara = (updated.get("card_id") or 0) // 100
        data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response


    chain_next = scenarios.for_chara(updated).chain_entry_after(full_state, event_id)
    if chain_next:
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = [chain_next]
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    if scenarios.for_chara(updated).holds_chain_after(event_id):
        _register_npc_unlocks(full_state, updated, event_id)
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = []
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    if event_id and ce_resolution is None:
        _note_not_up(data, single_mode_events.apply_effect(updated, event_id))
        if any(event_id in evs for evs in
               single_mode_events.SCENARIO_SCHEDULE.values()):
            done = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
            if event_id not in done:
                full_state[SCENARIO_EVENTS_DONE_KEY] = sorted(done | {event_id})
    if event_id:
        _register_npc_unlocks(full_state, updated, event_id)
    player_chara = (updated.get("card_id") or 0) // 100
    data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
    data["chara_info"] = updated
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)

    _apply_versus(data, full_state, updated.get("turn", turn))
    for npc in single_mode_events.npcs_unlocked_by(event_id):
        deck_position = _deck_position_for_chara(updated, npc[1])
        if deck_position is not None:
            for row in updated.get("evaluation_info_array") or []:
                if row.get("target_id") == deck_position:
                    row["is_appear"] = 1
            continue
        for row in updated.get("evaluation_info_array") or []:
            if row.get("target_id") == npc[0]:
                row["is_appear"] = 1
        uds = data.get("ura_data_set")
        if isinstance(uds, dict):
            uev = uds.setdefault("evaluation_info_array", [])
            if not any(e.get("target_id") == npc[0] for e in uev
                       if isinstance(e, dict)):
                uev.append({"target_id": npc[0], "chara_id": npc[1],
                            "member_state": 0})

    career["data"]["chara_info"] = updated
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def _failure_inputs(viewer_id):
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return None
    ci = (career.get("data") or {}).get("chara_info") or {}
    return (ci.get("vital"), tuple(ci.get("chara_effect_id_array") or ()))


def handle_ura_check_event(payload: dict) -> dict:
    viewer_id = payload.get("viewer_id")
    before = _failure_inputs(viewer_id)
    response = _handle_ura_check_event(payload)
    try:
        _refresh_failure_after_event(viewer_id, before, response)
    except Exception:
        log.exception("failure-rate refresh after check_event failed; skipping")
    return response


def _refresh_failure_after_event(viewer_id, before, response) -> None:
    after = _failure_inputs(viewer_id)
    if before is None or after is None or after == before:
        return
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    data = career.get("data") or {}
    home = data.get("home_info")
    if not isinstance(home, dict):
        return
    _refresh_failure_rates(data.get("chara_info") or {}, home, full_state,
                           _facility_levels(data))
    state_store.save_state(viewer_id, full_state)
    rates = {c.get("command_id"): c.get("failure_rate")
             for c in home.get("command_info_array") or () if c.get("command_type") == 1}
    served = ((response or {}).get("data") or {}).get("home_info")
    if isinstance(served, dict):
        for c in served.get("command_info_array") or ():
            if c.get("command_type") == 1 and c.get("command_id") in rates:
                c["failure_rate"] = rates[c["command_id"]]


def _refresh_failure_after_gains(career_state, response, full_state) -> None:
    data = (career_state or {}).get("data") or {}
    home = data.get("home_info")
    if not isinstance(home, dict):
        return
    _refresh_failure_rates(data.get("chara_info") or {}, home, full_state,
                           _facility_levels(data))
    rates = {c.get("command_id"): c.get("failure_rate")
             for c in home.get("command_info_array") or () if c.get("command_type") == 1}
    served = ((response or {}).get("data") or {}).get("home_info")
    if isinstance(served, dict) and served is not home:
        for c in served.get("command_info_array") or ():
            if c.get("command_type") == 1 and c.get("command_id") in rates:
                c["failure_rate"] = rates[c["command_id"]]


def _apply_versus(data, full_state, turn):
    if not scenarios.for_chara((data or {}).get("chara_info")).has_versus_npc:
        return None
    return single_mode_events.apply_versus_state(
        data, turn,
        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []),
        full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1),
        full_state.get(single_mode_events.DUEL_LAST_TURN_KEY))


def _happy_meek_facility(turn, full_state, chara_info=None):
    if not scenarios.for_chara(chara_info).has_versus_npc:
        return None
    return single_mode_events.happy_meek_facility(
        turn,
        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []),
        full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1),
        full_state.get(single_mode_events.DUEL_LAST_TURN_KEY))


def _duel_choice_to_stat(choice_number, options):
    opts = options or []
    if choice_number in (1, 2, 3, 4, 5):
        return choice_number
    if opts and isinstance(choice_number, int) and 1 <= choice_number <= len(opts):
        return opts[choice_number - 1]
    return opts[0] if opts else 1


@career_events.resolver("duel")
def _resolve_duel(full_state, chara_info, event, choice_number, current_turn=None, **kw):
    ctx = event.payload or {}
    vlevel = ctx.get("versus_level", full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1))
    stat_index = _duel_choice_to_stat(choice_number, ctx.get("stat_options"))
    duel_result = single_mode_events.resolve_duel(chara_info, stat_index, vlevel)
    if duel_result.get("won"):
        full_state[single_mode_events.VERSUS_LEVEL_KEY] = vlevel + 1
    full_state[single_mode_events.DUEL_LAST_TURN_KEY] = current_turn if current_turn is not None else chara_info.get("turn")
    return {}


@career_events.resolver("training_failure")
def _resolve_training_failure(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    single_mode_events.resolve_failure(
        chara_info, payload.get("worst", False), choice_number or 1,
        payload.get("stat") or "speed")
    full_state.pop(single_mode_events.FAIL_CTX_KEY, None)
    return {}


def handle_get_choice_reward(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event_id = payload.get("event_id")
    full_state = state_store.get_state(viewer_id) or {}

    career_ci = ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("chara_info")
    own = scenarios.for_chara(career_ci).choice_reward_preview(full_state, career_ci, event_id)
    if own is not None:
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": own}}

    if event_id == single_mode_events.DUEL_EVENT_ID:
        vlevel = full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1)
        ctx = full_state.get(single_mode_events.DUEL_CTX_KEY) or {}
        options = ctx.get("stat_options") or single_mode_events.duel_stat_options(vlevel)
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": single_mode_events.duel_preview(options)}}

    ctx = full_state.get(single_mode_events.FAIL_CTX_KEY) or {}
    if event_id in (single_mode_events.NORMAL_FAIL_EVENT, single_mode_events.WORST_FAIL_EVENT):
        preview = single_mode_events.failure_preview(
            ctx.get("worst", event_id == single_mode_events.WORST_FAIL_EVENT),
            ctx.get("stat") or "speed", career_ci)
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": preview}}

    if event_id in event_engine.CAREER_EVENT_IDS or _career_ctx_for(full_state, event_id):
        ev, ctx = _career_event_from_ctx(full_state, event_id)
        if ev and ctx.get("event_id") == event_id:
            ci = (full_state.get(STATE_KEY) or {}).get("data", {}).get("chara_info")
            dcid = event_engine.source_default_chara(ctx.get("source"), ctx.get("source_id"))
            partner, chances = _pal_unlock_preview_args(full_state, ctx.get("story_id"))
            return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, dcid, partner, chances,
                        source_card_id=_event_source_card(ctx))}}

    story_id = _pending_event_story(full_state, event_id)
    if story_id:
        career = full_state.get(STATE_KEY) or {}
        ci = career.get("data", {}).get("chara_info") or {}
        card_id = ci.get("card_id") or 0
        try:
            ev = event_engine.resolve(story_id, event_engine.event_title(story_id),
                                      card_id)
        except Exception:
            log.exception("choice-reward preview resolve failed for story %s", story_id)
            ev = None
        if ev and ev.get("choices"):
            return {"response_code": 1,
                    "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, (card_id // 100) or None)}}

    data = single_mode_events.choice_reward(event_id, payload.get("choice_number", 0))
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _pending_event_story(full_state: dict, event_id):
    if event_id is None:
        return None
    found = career_events.find(full_state, event_id)
    if found is not None:
        raw = getattr(found, "raw", None) or {}
        if raw.get("story_id"):
            return raw["story_id"]
        if getattr(found, "story_id", None):
            return found.story_id
    for entry in full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or ():
        if isinstance(entry, dict) and entry.get("event_id") == event_id:
            return entry.get("story_id")
    return None


_HINT_SP_DISCOUNT = {1: 10, 2: 20, 3: 30, 4: 35, 5: 40}


_FAST_LEARNER_SP_DISCOUNT = 10
_STUDIOUS_CONDITION = 40
_STUDIOUS_SP_DISCOUNT = 4


def _skill_purchase_cost(skill_id, tips_by_key: dict, conditions=()) -> int:
    row = master_data.query_one(
        "SELECT need_skill_point FROM single_mode_skill_need_point WHERE id=?", (skill_id,))
    base = row["need_skill_point"] if row else 0
    level = tips_by_key.get(master_data.skill_tip_key(skill_id), 0)
    pct = _HINT_SP_DISCOUNT.get(level, 0)
    if event_engine._FAST_LEARNER_CONDITION in (conditions or ()):
        pct += _FAST_LEARNER_SP_DISCOUNT
    elif _STUDIOUS_CONDITION in (conditions or ()):
        pct += _STUDIOUS_SP_DISCOUNT
    return base * (100 - pct) // 100


def handle_gain_skills(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": {}}
    chara_info = career["data"]["chara_info"]

    purchased = None
    for field in ("gain_skill_info_array", "skill_info_array", "gain_skill_array",
                  "skill_array", "skill_id_array"):
        if payload.get(field):
            purchased = payload[field]
            break

    entries = []
    for p in purchased or []:
        if isinstance(p, dict) and p.get("skill_id"):
            entries.append({"skill_id": int(p["skill_id"]), "level": int(p.get("level") or 1)})
        elif isinstance(p, int):
            entries.append({"skill_id": p, "level": 1})

    tips_by_key = {(t.get("group_id"), t.get("rarity")): t.get("level", 0)
                   for t in chara_info.get("skill_tips_array", [])}
    skills = chara_info.setdefault("skill_array", [])
    by_id = {s["skill_id"]: s for s in skills}
    total_cost = 0
    for e in entries:
        existing = by_id.get(e["skill_id"])
        if existing and existing.get("level", 1) >= e["level"]:
            continue
        total_cost += _skill_purchase_cost(e["skill_id"], tips_by_key,
                                           chara_info.get("chara_effect_id_array") or ())
        if existing:
            existing["level"] = e["level"]
        else:
            new = {"skill_id": e["skill_id"], "level": e["level"]}
            skills.append(new)
            by_id[e["skill_id"]] = new

    chara_info["skill_point"] = max(0, chara_info.get("skill_point", 0) - total_cost)
    state_store.save_state(viewer_id, full_state)
    if entries:
        log.info("gain_skills: +%d skills, -%d SP (now %d) for viewer %s",
                 len(entries), total_cost, chara_info["skill_point"], viewer_id)

    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    updated = copy.deepcopy(chara_info)
    updated["playing_state"] = 1
    data["chara_info"] = updated
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    turn = payload.get("current_turn", updated.get("turn"))
    _apply_versus(data, full_state, turn)
    keep = ["chara_info", "home_info"] + [k for k in data if k.endswith("_data_set")]
    response["data"] = {k: data[k] for k in keep if k in data}
    return response


def handle_ura_race_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    pair = fixtures.first("single_mode/race_start") or fixtures.first("single_mode_team/race_start")
    if pair is None:
        raise LookupError("No captured race_start fixture available")
    response = pair.response_copy()
    data = response.get("data", {})

    full_state = state_store.get_state(viewer_id) or {}
    race_ctx = full_state.get(RACE_CTX_KEY) or {}
    if race_ctx.get("race_scenario") and race_ctx.get("race_start_info"):
        data["race_scenario"] = race_ctx["race_scenario"]
        data["race_start_info"] = copy.deepcopy(race_ctx["race_start_info"])
    else:
        _patch_player_horse(data.get("race_start_info", {}), _career_chara_info(viewer_id))
    return response


_URA_RACE_SEED_DIR = region.seeds_dir() / "ura_race"
_ura_race_seed_cache: dict = {}


def _no_career_refusal(viewer_id, where: str, full_state: dict) -> dict:
    log.error("%s: no career state for viewer %s -- refusing rather than serving "
              "the raw Maruzensky seed. full_state keys=%s",
              where, viewer_id, sorted(full_state or {}))
    return refuse()


def _load_ura_race_seed(name: str) -> dict:
    if name not in _ura_race_seed_cache:
        with open(_URA_RACE_SEED_DIR / f"{name}.json", encoding="utf-8") as f:
            _ura_race_seed_cache[name] = json.load(f)
    return copy.deepcopy(_ura_race_seed_cache[name])


def _career_win_streak(race_history, result_rank: int) -> int:
    if result_rank != 1:
        return 0
    streak = 1
    for entry in reversed(list(race_history or ())):
        if entry.get("result_rank") == 1:
            streak += 1
        else:
            break
    return max(1, min(5, streak))


def _build_race_reward_info(program_id, result_rank: int, gained_fans: int,
                            race_history=None, rng=None) -> dict:
    rng = rng or random
    info = {
        "result_rank": int(result_rank),
        "result_time": 0,
        "race_reward": [],
        "race_reward_bonus": [],
        "race_reward_plus_bonus": [],
        "race_reward_bonus_win": [],
        "gained_fans": int(gained_fans),
        "campaign_id_array": [],
    }
    if not program_id:
        return info
    program = master_data.query_one(
        "SELECT reward_set_id FROM single_mode_program WHERE id=?", (program_id,))
    if not program or not program["reward_set_id"]:
        return info
    rows = master_data.query(
        "SELECT reward_type, bonus, odds, item_category, item_id, item_num "
        "FROM single_mode_reward_set WHERE reward_set_id=? "
        "AND order_min<=? AND order_max>=? ORDER BY id",
        (program["reward_set_id"], result_rank, result_rank))
    tier = _career_win_streak(race_history, result_rank)
    for row in rows:
        if row["reward_type"] == 2 and row["bonus"] != tier:
            continue
        if rng.randrange(1000000) >= (row["odds"] or 0):
            continue
        item = {"item_type": row["item_category"], "item_id": row["item_id"],
                "item_num": row["item_num"]}
        key = "race_reward" if row["reward_type"] == 1 else "race_reward_bonus"
        info[key].append(item)
    return info


RACE_TROPHY_KEY = "trophy_award"


def _race_trophy_award(full_state: dict, viewer_id, race_ctx: dict, chara_info: dict,
                       result_rank: int) -> tuple:
    cached = race_ctx.get(RACE_TROPHY_KEY)
    if isinstance(cached, list) and len(cached) == 2:
        return copy.deepcopy(cached[0]), copy.deepcopy(cached[1])
    award = ([], None)
    if int(result_rank or 0) == 1 and race_ctx.get("program_id"):
        row = master_data.query_one(
            "SELECT race_instance_id FROM single_mode_program WHERE id=?",
            (int(race_ctx["program_id"]),))
        if row and row["race_instance_id"]:
            from . import user_profile
            award = user_profile.award_race_trophy(
                full_state, viewer_id, int(row["race_instance_id"]),
                int(chara_info.get("card_id") or 0) // 100)
    race_ctx[RACE_TROPHY_KEY] = [award[0], award[1]]
    return copy.deepcopy(award[0]), copy.deepcopy(award[1])


RACE_REWARD_CREDITED_KEY = "reward_credited"


def _credit_race_reward(full_state: dict, race_ctx: dict) -> dict:
    credited = race_ctx.get(RACE_REWARD_CREDITED_KEY)
    if isinstance(credited, dict):
        return credited
    reward = race_ctx.get("reward_info") or {}
    summary = {"add_item_list": [], "add_fcoin": 0}
    for it in (reward.get("race_reward") or []) + (reward.get("race_reward_bonus") or []):
        item_id, num = int(it.get("item_id") or 0), int(it.get("item_num") or 0)
        if not item_id or num <= 0:
            continue
        if it.get("item_type") == presents._TYPE_CARAT:
            wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
            before = wallet.get("fcoin") or 0
            wallet["fcoin"] = presents._clamp_int32(before + num)
            summary["add_fcoin"] += wallet["fcoin"] - presents._clamp_int32(before)
            continue
        applied = presents._add_item(full_state, item_id, num)
        if applied:
            presents._summary_add_item(summary, item_id, applied)
    race_ctx[RACE_REWARD_CREDITED_KEY] = summary
    return summary


def _fan_gain_for_race(chara_info: dict, program_id, result_rank: int,
                       full_state: dict = None) -> int:
    base = 1000
    if program_id:
        program = master_data.query_one(
            "SELECT fan_set_id FROM single_mode_program WHERE id=?", (program_id,))
        if program:
            row = master_data.query_one(
                "SELECT fan_count FROM single_mode_fan_count WHERE fan_set_id=? AND [order]=?",
                (program["fan_set_id"], result_rank))
            if row:
                base = row["fan_count"]
    bonus_pct = _deck_effect_at_level(chara_info, training_formula.EFFECT_FAN_BONUS)
    if full_state is not None:
        bonus_pct += scenarios.for_chara(chara_info).fan_bonus_pct(full_state, chara_info)
    fans = conditions.fan_gain(chara_info, int(round(base * (1 + bonus_pct / 100))))
    return int(round(fans * tuning.mult("fan_gain_multiplier")))


_RACE_REWARD_BASE = {
    100: (3, 45),
    200: (3, 35), 300: (3, 35),
    400: (3, 30), 500: (3, 30), 600: (3, 30), 700: (3, 30),
    800: (3, 30), 900: (3, 30),
}
_FINALS_REWARD_BASE = {1: (10, 40), 2: (10, 60), 3: (10, 80)}
_FINALS_MEEK_REWARD = (20, 150)
_SKILL_PAST_MY_LIMITS = 210081
_FINALS_MEEK_STORY = None
_FINALS_MEEK_EVENT_ID = None
_DEFAULT_STAT_CAP = 1200
_POST_RACE_SUFFIXES = (513, 514, 515)
POST_RACE_FIRED_KEY = "post_race_stories_fired"

RACE_REWARD_NOT_UP_KEY = "pending_race_reward_not_up"
RACE_REWARD_KEY = "pending_race_reward"


def _deck_effect_at_level(chara_info: dict, effect_type: int) -> float:
    levels = _deck_support_card_levels(chara_info)
    total = 0.0
    for card in chara_info.get("support_card_array") or []:
        sid = card.get("support_card_id")
        if not sid:
            continue
        lv = levels.get(sid) if card.get("exp") else \
            training_formula.level_cap_for(sid, card.get("limit_break_count"))
        total += training_formula.master_effect(sid, effect_type, level=lv)
        if lv:
            total += training_formula.unique_effect(sid, effect_type, level=lv)
    return total


def _race_bonus_pct(chara_info: dict, full_state: dict = None) -> int:
    return _deck_effect_at_level(chara_info, training_formula.EFFECT_RACE_BONUS)


def _scale_race_reward(value: int, chara_info: dict, full_state: dict = None) -> int:
    deck = int(_race_bonus_pct(chara_info))
    out = int(value) * (100 + deck) // 100
    if full_state is not None:
        extra = int(scenarios.for_chara(chara_info).race_bonus_pct(full_state, chara_info) or 0)
        out = out * (100 + extra) // 100
    return out


def _race_grade_of(program_id) -> int | None:
    row = master_data.query_one(
        "SELECT r.grade FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE p.id=?", (program_id,))
    return row["grade"] if row else None


def _is_debut_race(program_id) -> bool:
    return _race_grade_of(program_id) == _DEBUT_GRADE


def _debut_runs_voluntary(chara_info: dict, program_id) -> bool:
    if not _is_debut_race(program_id):
        return False
    return not scenarios.for_chara(chara_info).debut_is_goal_race


def _race_reward_for(chara_info: dict, program_id, finals_round, result_rank: int,
                     full_state: dict = None) -> dict:
    if finals_round:
        table = scenarios.for_chara(chara_info).finals_reward_base or _FINALS_REWARD_BASE
        base_stat, base_sp = table.get(finals_round, (10, 40))
    else:
        grade = _race_grade_of(program_id) if program_id else None
        base_stat, base_sp = _RACE_REWARD_BASE.get(grade, (3, 30))
        if region.CURRENT_REGION.get() == "jp":
            base_sp = _JP_GOAL_RACE_SP
    if not finals_round:
        if result_rank >= 4:
            base_stat, base_sp = 1, max(10, base_sp - 10)
        elif result_rank >= 2:
            base_stat = max(1, base_stat - (result_rank - 1))
    elif result_rank >= 6:
        base_stat, base_sp = 1, max(10, base_sp - 10)
    elif result_rank >= 4:
        base_stat, base_sp = max(1, base_stat // 2), max(10, base_sp - 5)
    return {"stats": _scale_race_reward(base_stat, chara_info, full_state),
            "sp": _scale_race_reward(base_sp, chara_info, full_state)}


def _apply_race_reward(chara_info: dict, reward: dict) -> list:
    per_stat = reward.get("stats", 0)
    not_up = []
    for s in ("speed", "stamina", "power", "guts", "wiz"):
        cap_key = "max_wiz" if s == "wiz" else f"max_{s}"
        soft_key = "default_max_wiz" if s == "wiz" else f"default_max_{s}"
        soft = chara_info.get(soft_key) or _DEFAULT_STAT_CAP
        gain = per_stat // 2 if chara_info.get(s, 0) > soft else per_stat
        if gain > 0 and chara_info.get(s, 0) >= chara_info.get(cap_key, 9999):
            not_up.append(event_engine._STAT_KEY_IDX[s])
        chara_info[s] = min(chara_info.get(s, 0) + gain, chara_info.get(cap_key, 9999))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + reward.get("sp", 0)
    hint = reward.get("skill_hint")
    if hint:
        event_engine._apply_skill_hint(chara_info, hint, 1)
    if not_up:
        log.info("race reward: already at ceiling -> not_up_parameter_info %s "
                 "(stats %s)", sorted(not_up),
                 [event_engine._STAT_IDX_KEY.get(c) for c in sorted(not_up)])
    return sorted(set(not_up))


def _queue_post_race_choice(response: dict, full_state: dict, career_state: dict) -> bool:
    return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    fired = set(full_state.get(POST_RACE_FIRED_KEY) or [])
    for suffix in _POST_RACE_SUFFIXES:
        sid = 500000000 + chara * 1000 + suffix
        if sid in fired:
            continue
        title = event_engine.event_title(sid)
        ev = event_engine.resolve(sid, title, card_id) if title else None
        if not ev or not ev.get("choices"):
            continue
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara,
            support_card_id=0, play_timing=3))
        _push_career_ctx(full_state, {
            "event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
            "source": "chara", "source_id": card_id, "title": title,
            "trainee_card_id": card_id})
        full_state[POST_RACE_FIRED_KEY] = sorted(fired | {sid})
        return True
    return False


def _append_race_history(career_data: dict, race_ctx: dict, chara_info: dict, result_rank: int) -> None:
    history = career_data.setdefault("race_history", [])
    rsi = race_ctx.get("race_start_info") or {}
    horses = rsi.get("race_horse_data") or []
    history.append({
        "turn": race_ctx.get("turn"),
        "program_id": race_ctx.get("program_id"),
        "weather": rsi.get("weather", 1),
        "ground_condition": rsi.get("ground_condition", 1),
        "running_style": race_ctx.get("running_style")
                         or chara_info.get("race_running_style", 2),
        "result_rank": result_rank,
        "frame_order": (horses[0].get("frame_order", 1) if horses else 1),
        "npc_count": max(0, len(horses) - 1),
        "popularity": race_ctx.get("player_popularity"),
        "rival_ranks": race_ctx.get("rival_finish_orders"),
        "rushed_count": race_ctx.get("player_rushed_count"),
        "skills_used": race_ctx.get("player_skills_used"),
        "overtakes": race_ctx.get("player_overtakes"),
        "fans_after": int((chara_info or {}).get("fans") or 0),
    })


_WIRE_RACE_HISTORY_FIELDS = (
    "turn", "program_id", "weather", "ground_condition", "running_style",
    "result_rank", "frame_order", "npc_count")


def _wire_race_history(history) -> list:
    if not isinstance(history, list):
        return history
    return [{k: e[k] for k in _WIRE_RACE_HISTORY_FIELDS if k in e}
            if isinstance(e, dict) else e
            for e in history]


FAN_PROMISES_DONE_KEY = "fan_promises_fulfilled"


def _fulfil_fan_promise(career_data: dict, chara_info: dict, program_id) -> int | None:
    track = (_race_facts_for_program(program_id) or {}).get("track_id")
    cid = conditions.promise_fulfilled_by(chara_info, track)
    if cid is None:
        return None
    conditions.remove(chara_info, cid)
    done = career_data.setdefault(FAN_PROMISES_DONE_KEY, [])
    if cid not in done:
        done.append(cid)
    log.info("fan promise %s fulfilled by a win at racetrack %s", cid, track)
    return cid


def _career_chara_grade(fans: int, runs: int, wins: int) -> int:
    grade = 1
    for r in master_data.query(
            "SELECT id, win_num, run_num, need_fan_count FROM single_mode_chara_grade ORDER BY id"):
        if wins >= r["win_num"] and runs >= r["run_num"] and fans >= r["need_fan_count"]:
            grade = r["id"]
    return grade


def handle_ura_race_end(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    race_ctx = full_state.get(RACE_CTX_KEY) or {}

    if not career:
        return _no_career_refusal(viewer_id, "single_mode/race_end", full_state)

    response = _load_ura_race_seed("race_end")
    data = response["data"]
    finish_order = race_ctx.get("player_finish_order")
    result_rank = finish_order if finish_order else 1
    won = result_rank == 1
    gained_fans = 1000

    if career:
        chara_info = copy.deepcopy(career["data"]["chara_info"])
        gained_fans = _fan_gain_for_race(chara_info, race_ctx.get("program_id"),
                                         result_rank, full_state=full_state)
        already_committed = race_ctx.get("end_committed", False)
        if already_committed and race_ctx.get("gained_fans_committed") is not None:
            gained_fans = race_ctx["gained_fans_committed"]
        if not already_committed:
            chara_info["fans"] = chara_info.get("fans", 0) + gained_fans
            _append_race_history(career["data"], race_ctx, chara_info, result_rank)
            if won:
                _fulfil_fan_promise(career["data"], chara_info,
                                    race_ctx.get("program_id"))
            try:
                scenarios.for_chara(chara_info).on_race_result(
                    full_state, chara_info, race_ctx, result_rank)
            except Exception:
                log.exception("scenario race pay-out failed; race still committed")
            try:
                race_ctx["add_music"] = concerts.on_race_end(
                    full_state, chara_info, race_ctx, result_rank)
            except Exception:
                log.exception("concert unlock on race_end failed")
        history = career["data"].get("race_history") or []
        chara_info["chara_grade"] = _career_chara_grade(
            chara_info.get("fans", 0), len(history),
            sum(1 for h in history if h.get("result_rank") == 1))
        chara_info["playing_state"] = 4
        fr = race_ctx.get("finals_round")
        if fr == 3 or (fr and not won
                       and scenarios.for_chara(chara_info).has_ending_chain):
            chara_info["state"] = 3
        data["chara_info"] = chara_info
        career["data"]["chara_info"] = chara_info
        if "race_history" in data:
            data["race_history"] = _wire_race_history(history)
        if "race_condition_array" in data:
            data["race_condition_array"] = _build_race_condition_array(
                chara_info, chara_info.get("turn"), history)
        race_ctx["won"] = won
        if not already_committed:
            race_ctx["gained_fans_committed"] = gained_fans
        race_ctx["end_committed"] = True
        full_state[RACE_CTX_KEY] = race_ctx
        state_store.save_state(viewer_id, full_state)

    reward_info = race_ctx.get("reward_info")
    if not isinstance(reward_info, dict):
        reward_info = _build_race_reward_info(
            race_ctx.get("program_id"), result_rank, gained_fans,
            (career["data"].get("race_history") if career else None))
        race_ctx["reward_info"] = reward_info
        if career:
            full_state[RACE_CTX_KEY] = race_ctx
            state_store.save_state(viewer_id, full_state)
    reward_info = copy.deepcopy(reward_info)
    reward_info["gained_fans"] = gained_fans
    reward_info["result_rank"] = result_rank
    reward_info["result_time"] = int(race_ctx.get("player_finish_time") or 0)
    data["race_reward_info"] = reward_info
    data["race_add_reward_info"] = []
    data["add_music"] = race_ctx.get("add_music")
    credited = {"add_item_list": [], "add_fcoin": 0}
    trophy, trophy_reward = [], None
    if career:
        credited = _credit_race_reward(full_state, race_ctx)
        trophy, trophy_reward = _race_trophy_award(
            full_state, viewer_id, race_ctx, career["data"]["chara_info"], result_rank)
        full_state[RACE_CTX_KEY] = race_ctx
        state_store.save_state(viewer_id, full_state)
    data["add_trophy_info"] = trophy
    data["trophy_reward_info"] = trophy_reward
    if isinstance(data.get("reward_summary_info"), dict):
        data["reward_summary_info"]["add_total_fan"] = gained_fans
        data["reward_summary_info"]["add_item_list"] = copy.deepcopy(credited["add_item_list"])
        data["reward_summary_info"]["add_fcoin"] = credited["add_fcoin"]
    return response


def handle_ura_race_out(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    race_ctx = full_state.get(RACE_CTX_KEY) or {}

    if not career:
        return _no_career_refusal(viewer_id, "single_mode/race_out", full_state)

    response = _load_ura_race_seed("race_out")
    data = response["data"]
    data["unchecked_event_array"] = []

    if career:
        chara_info = copy.deepcopy(career["data"]["chara_info"])
        finals_round = race_ctx.get("finals_round")
        if (finals_round and not race_ctx.get("won", True)
                and scenarios.for_chara(chara_info).has_ending_chain):
            chara_info["state"] = 3
            chara_info["playing_state"] = 5
            chain = [_scenario_event_entry(reporter_issue_event_id(chara_info),
                                           _ENDING_CHAIN[0][1], play_timing=3,
                                           choices=[_ack_choice(*_ENDING_CHAIN[0][2:])])]
            issue_stats, issue_sp = reporter_issue_pay(chara_info)
            payouts = {str(reporter_issue_event_id(chara_info)):
                       {"stats": issue_stats, "sp": issue_sp}}
            for entry, pay in _friend_ending_events(full_state, chara_info):
                chain.append(entry)
                payouts[str(entry["event_id"])] = pay
            chara = (chara_info.get("card_id") or 0) // 100
            if chara:
                chain.append(_scenario_event_entry(
                    _ENDING_CHARA_LOST_EVENT_ID, 500000000 + chara * 1000 + 110,
                    play_timing=3, choices=[_ack_choice()], chara_id=chara))
            full_state[ENDING_PAYOUT_KEY] = payouts
            for entry in chain:
                _queue_turn_event(response, full_state, entry)
            log.info("URA finals round %s LOST -- career completed (110 ending)", finals_round)
            data["chara_info"] = chara_info
            career["data"]["chara_info"] = chara_info
            full_state.pop(RACE_CTX_KEY, None)
            _remember_display(full_state, response)
            state_store.save_state(viewer_id, full_state)
            return response
        if finals_round:
            beat_meek = bool(race_ctx.get("beat_happy_meek")) and finals_round == 3
            data["unchecked_event_array"] = [
                _finals_event_entry(finals_round, "post", chara_info)]
            try:
                reward = _race_reward_for(
                    chara_info, race_ctx.get("program_id"), finals_round,
                    int(race_ctx.get("player_finish_order") or 1),
                    full_state=full_state)
                if beat_meek:
                    stats, sp = _FINALS_MEEK_REWARD
                    reward = {"stats": _scale_race_reward(stats, chara_info, full_state),
                              "sp": _scale_race_reward(sp, chara_info, full_state),
                              "skill_hint": _SKILL_PAST_MY_LIMITS}
                    log.info("URA final: beat Happy Meek -> %s", reward)
                full_state[RACE_REWARD_KEY] = reward
            except Exception:
                log.exception("finals reward failed; skipping")
            if finals_round == 3 and scenarios.for_chara(chara_info).has_ending_chain:
                scen = scenarios.for_chara(chara_info)
                has_director = scen.has_director_ending
                chain = [_scenario_event_entry(reporter_issue_event_id(chara_info)
                                               if sid == _ENDING_CHAIN[0][1] else eid,
                                               sid, play_timing=3,
                                               choices=[_ack_choice(sel, item)])
                         for eid, sid, sel, item in _ENDING_CHAIN
                         if has_director or eid != _SUPER_SUCCESS_EVENT_ID]
                issue_stats, issue_sp = reporter_issue_pay(chara_info)
                payouts = {str(reporter_issue_event_id(chara_info)):
                           {"stats": issue_stats, "sp": issue_sp}}
                ending = _ending_payout_events(full_state, chara_info,
                                               won_final=race_ctx.get("won", True))
                friends = _friend_ending_events(full_state, chara_info)
                for entry, stats, sp in ending[:-1]:
                    chain.append(entry)
                    payouts[str(entry["event_id"])] = {"stats": stats, "sp": sp}
                for entry, pay in friends:
                    chain.append(entry)
                    payouts[str(entry["event_id"])] = pay
                for entry, stats, sp in ending[-1:]:
                    chain.append(entry)
                    payouts[str(entry["event_id"])] = {"stats": stats, "sp": sp}
                if has_director:
                    d_bond = _bond_of(chara_info, _DIRECTOR_TARGET)
                    s_stats, s_sp = (_SUPER_SUCCESS_MAX
                                     if d_bond >= _DIRECTOR_MAX_BOND
                                     else _SUPER_SUCCESS_BASE)
                    payouts[str(_SUPER_SUCCESS_EVENT_ID)] = {"stats": s_stats, "sp": s_sp}
                full_state[ENDING_PAYOUT_KEY] = payouts
                full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, []).extend(chain)
        else:
            story_name = race_ctx.get("race_story_name")
            won_flag = race_ctx.get("won", True)
            rank = race_ctx.get("player_finish_order") or 1
            is_goal = race_ctx.get("is_goal")
            if is_goal is None:
                is_goal = _is_goal_race(chara_info, race_ctx.get("program_id"),
                                        race_ctx.get("turn"),
                                        career["data"].get("race_history") or [])
            route_ids_now = tuple(chara_info.get("route_race_id_array") or ())
            placement_failed = is_goal and not scenarios.for_chara(chara_info).owns_goal_race_result(
                chara_info, race_ctx.get("program_id")) and any(
                t == race_ctx.get("turn") and cv1 and rank > cv1
                for t, _cid, _kind, cv1 in _route_goal_rows(route_ids_now))
            try:
                if placement_failed:
                    chara_info["state"] = 2
                    _purge_queued_events(response, full_state)
                    _queue_fail_reaction(response, full_state, chara_info)
                    _queue_turn_event(response, full_state, _career_fail_event(),
                                      fail_chain=True)
                    log.info("goal race at turn %s lost (rank %s) -- career failed, "
                             "skipped goal-complete celebration/reward",
                             race_ctx.get("turn"), rank)
                elif is_goal and (scenarios.for_chara(chara_info).replaces_goal_race_event
                                  or scenarios.for_chara(chara_info).owns_goal_race_result(
                                      chara_info, race_ctx.get("program_id"))):
                    pass
                elif is_goal and not _debut_runs_voluntary(chara_info,
                                                             race_ctx.get("program_id")):
                    ev = _goal_race_reward(chara_info, race_ctx.get("program_id"),
                                           race_ctx.get("finals_round"), rank)
                    entry = (_post_race_event_entry(chara_info, story_name,
                                                    won=won_flag, choices=1,
                                                    rank=rank,
                                                    turn=race_ctx.get("turn"))
                             if story_name else None)
                    title = story_name
                    if entry:
                        try:
                            ev = _branch_post_race(full_state, chara_info, entry, ev)
                        except Exception:
                            log.exception("post-race branch failed; serving it plain")
                        data["unchecked_event_array"] = [entry]
                        _push_career_ctx(full_state, {
                            "event_id": entry["event_id"],
                            "story_id": entry["story_id"],
                            "source": "inline", "source_id": 0, "title": title,
                            "trainee_card_id": chara_info.get("card_id"),
                            "event": ev})
                    else:
                        event_engine.apply_choice(chara_info, ev, 1, full_state=full_state)
                else:
                    entry, ev, title = _voluntary_race_event(
                        full_state, chara_info, race_ctx.get("program_id"), rank)
                    if entry:
                        data["unchecked_event_array"] = [entry]
                        _push_career_ctx(full_state, {
                            "event_id": entry["event_id"],
                            "story_id": entry["story_id"],
                            "source": "inline", "source_id": 0, "title": title,
                            "trainee_card_id": chara_info.get("card_id"),
                            "event": ev})
                    else:
                        event_engine.apply_choice(chara_info, ev, 1, full_state=full_state)
            except Exception:
                log.exception("post-race reward event failed; skipping")
            try:
                _maybe_fire_epithet_events(full_state, career, response)
            except Exception:
                log.exception("epithet announcement failed; skipping")
            try:
                _maybe_fire_secret_event(full_state, career, response)
            except Exception:
                log.exception("post-race secret event failed; skipping")
            try:
                _maybe_fire_race_fatigue(full_state, career, response, race_ctx)
            except Exception:
                log.exception("race fatigue roll failed; skipping")
            try:
                unlocked = [list(x) for x in full_state.get(
                    single_mode_events.UNLOCKED_NPCS_KEY, [])]
                done = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
                reporter_event = scenarios.for_chara(chara_info).debut_reporter_event
                if (reporter_event and race_ctx.get("won", True)
                        and [103, 9003] not in unlocked
                        and 1016 not in done
                        and _race_grade_of(race_ctx.get("program_id")) == _DEBUT_GRADE):
                    pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
                    if 1016 not in pending:
                        pending.append(1016)
                    full_state[single_mode_events.PENDING_EVENTS_KEY] = pending
            except Exception:
                log.exception("reporter unlock failed; skipping")
        if finals_round != 3 and chara_info.get("state") != 3:
            chara_info["turn"] = chara_info.get("turn", 1) + 1
        chara_info["playing_state"] = 1
        career["data"]["chara_info"] = chara_info
        race_pending = False
        scen_race = scenarios.for_chara(chara_info)
        if scen_race.reserve_arrivals_after_race and race_ctx.get("turn") is not None:
            try:
                race_pending = _scenario_interstitial_pending(
                    full_state, career, payload, turn=race_ctx.get("turn"))
                if race_pending:
                    scen_race.begin_hold(full_state, int(race_ctx.get("turn")))
            except Exception:
                log.exception("scenario turn-hold failed post-race; skipping")
        with _scenario_slot_reserved(full_state, race_pending):
            try:
                _maybe_fire_refresh_event(full_state, career, response)
            except Exception:
                log.exception("refresh event failed post-race; skipping")
            try:
                _maybe_queue_inspiration(response, full_state, career, viewer_id,
                                         chara_info.get("turn"))
            except Exception:
                log.exception("inspiration roll failed post-race; skipping")
        try:
            if chara_info.get("state") != 2:
                _maybe_fail_goal(response, full_state, career, chara_info.get("turn"))
                with _scenario_slot_reserved(full_state, race_pending):
                    _maybe_queue_chara_story(response, full_state, career, chara_info.get("turn"),
                                             after_race=race_ctx.get("turn") if race_ctx else None)
                    try:
                        _maybe_queue_unique_level(response, full_state, career,
                                                  chara_info.get("turn"))
                    except Exception:
                        log.exception("unique skill level event failed post-race; skipping")
            sort_id = next((s for t, s, ct, cid, _c1, _c2
                            in _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
                            if ct == 1 and cid == race_ctx.get("program_id")), None)
            if sort_id is not None:
                _mark_goal_progress(response, chara_info,
                                    career["data"].get("race_history", []), sort_id,
                                    full_state)
            if chara_info.get("state") != 2:
                _announce_due_passive_goals(response, full_state, career,
                                            race_ctx.get("turn"))
        except Exception:
            log.exception("goal evaluation failed post-race; not failing the career")
        played_turn = race_ctx.get("turn")
        try:
            if played_turn is not None and _scenario_interstitial_pending(
                    full_state, career, payload, turn=played_turn):
                scenarios.for_chara(chara_info).begin_hold(full_state, int(played_turn))
        except Exception:
            log.exception("scenario turn-hold failed post-race; skipping")
        try:
            _poll_career_events(response, full_state, career, payload,
                                turn=played_turn)
        except Exception:
            log.exception("career event poll failed post-race; skipping")
        try:
            if random.random() < tuning.event(_POST_RACE_SUPPORT_CHANCE):
                _maybe_fire_support_event(full_state, career, response, force=True)
        except Exception:
            log.exception("post-race support event failed; skipping")
        _claim_inspiration_slot(response, full_state, chara_info)

        reward = race_ctx.get("pending_reward")
        if reward:
            if data.get("unchecked_event_array"):
                full_state[RACE_REWARD_KEY] = reward
            else:
                _apply_race_reward(chara_info, reward)
        career_home = career["data"].get("home_info")
        if isinstance(career_home, dict):
            unlocked = [n[0] for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
            _refresh_command_info(chara_info, career_home, turn=chara_info["turn"], unlocked_npcs=unlocked,
                                  facility_levels=_facility_levels(career["data"]),
                                  race_history=career["data"].get("race_history", []),
                                  training_bonus=_training_bonus(full_state, chara_info),
                              friendship_bonus=_friendship_bonus(full_state, chara_info),
                              specialty_bonus=_specialty_bonus(full_state, chara_info),
                              support_card_levels=_support_card_levels(full_state),
                              friendship_stacks=_friendship_stacks(full_state),
                              full_state=full_state)
            data["home_info"] = copy.deepcopy(career_home)
        data["chara_info"] = chara_info
        career["data"]["chara_info"] = chara_info
        full_state.pop(RACE_CTX_KEY, None)

    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


_TRAINING_COMMAND_IDS_SET = {101, 105, 102, 103, 106}
_SUPPORT_EVENT_CHANCE = 0.75
_CHAIN_STEP_CHANCE = 0.065
_RANDOM_SUPPORT_EVENT_CHANCE = _SUPPORT_EVENT_CHANCE * 0.20
_POST_RACE_SUPPORT_CHANCE = 0.20
_SUPPORT_FROM_DECK = 0.60
_SUPPORT_CHAIN_VS_RANDOM = 0.80
_CHAIN_STOP_TURN = 71
_SUPPORT_STOP_TURN = 71


def _career_deck(chara_info: dict) -> list:
    return [c.get("support_card_id") for c in chara_info.get("support_card_array") or []
            if c.get("support_card_id")]


def _deck_support_card_levels(chara_info: dict) -> dict:
    return {c["support_card_id"]: training_formula.card_level(
                c["support_card_id"], c.get("exp", 0), c.get("limit_break_count"))
           for c in chara_info.get("support_card_array") or []
           if c.get("support_card_id")}


def _pick_support_event(pool: list, fired: set, want_random: bool,
                        full_state: dict | None = None):
    cards = list(pool)
    random.shuffle(cards)
    for card in cards:
        from .. import custom_support
        card_fired = set((full_state or {}).get("_custom_support_fired", {}).get(str(card), [])) if custom_support.card(card) else fired
        chain, rand = event_engine.support_events(card)
        if want_random:
            avail = [(sid, t, ev) for sid, t, ev in rand if sid not in card_fired]
            if avail:
                sid, t, ev = random.choice(avail)
                return card, sid, t, ev, True
        elif not event_engine.chain_ended(full_state, f"support:{int(card)}"):
            for sid, t, ev in chain:
                if sid not in card_fired:
                    return card, sid, t, ev, False
    return None


def _maybe_fire_support_event(full_state: dict, career_state: dict, response: dict,
                              force: bool = False) -> bool:
    chara_info = career_state["data"]["chara_info"]
    turn_now = chara_info.get("turn")
    if turn_now is not None and turn_now > _SUPPORT_STOP_TURN:
        return False
    if _is_camp_turn(turn_now):
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    own_chara = int(chara_info.get("card_id") or 0) // 100

    def _not_own(pool):
        return [c for c in pool if master_data.support_card_chara(c) != own_chara]
    chains_allowed = not (turn_now is not None and turn_now > _CHAIN_STOP_TURN)
    pick = None
    if not force:
        if chains_allowed:
            winners = [c for c in _career_deck(chara_info)
                       if random.random() < tuning.event(_CHAIN_STEP_CHANCE)]
            if winners:
                pick = _pick_support_event(winners, fired, False, full_state)
        if not pick:
            if random.random() >= tuning.event(_RANDOM_SUPPORT_EVENT_CHANCE):
                return False
            for pool in ([_career_deck(chara_info), event_engine.all_support_card_ids()]
                         if random.random() < _SUPPORT_FROM_DECK
                         else [event_engine.all_support_card_ids()]):
                pool = _not_own(pool)
                pick = pool and _pick_support_event(pool, fired, True, full_state)
                if pick:
                    break
            if not pick:
                return False
    if pick:
        return _serve_support_pick(full_state, chara_info, response, pick)

    if random.random() < _SUPPORT_FROM_DECK:
        pools = [_career_deck(chara_info), event_engine.all_support_card_ids()]
    else:
        pools = [event_engine.all_support_card_ids()]
    chain_share = _SUPPORT_CHAIN_VS_RANDOM
    chain_share = min(1.0, chain_share
                      + scenarios.for_chara(chara_info)
                      .support_event_bonus_pct(full_state) / 100.0)
    want_random = random.random() >= chain_share
    if not chains_allowed:
        want_random = True
    for pool in pools:
        pool = _not_own(pool)
        if not pool:
            continue
        pick = _pick_support_event(pool, fired, want_random, full_state)
        if not pick and chains_allowed:
            pick = _pick_support_event(pool, fired, not want_random, full_state)
        if pick:
            break
    if not pick:
        return False
    return _serve_support_pick(full_state, chara_info, response, pick)


def _serve_support_pick(full_state: dict, chara_info: dict, response: dict, pick) -> bool:
    card, sid, title, ev, is_random = pick

    event_id = (event_engine.support_random_event_id(sid) if is_random
                else event_engine.SUPPORT_EVENT_ID)
    if _career_ctx_pending(full_state, event_id):
        log.info("SUPPORT_FIRE_RATE pending_skip event_id=%s turn=%s",
                 event_id, chara_info.get("turn"))
        return False
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=0, support_card_id=card, play_timing=1))
    from .. import custom_support
    if custom_support.card(card):
        full_state.setdefault("_custom_support_fired", {}).setdefault(str(card), []).append(sid)
    else:
        full_state[event_engine.FIRED_EVENTS_KEY] = list(
            full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_id, "story_id": sid, "source": "support",
        "source_id": card, "title": title,
        "queued_turn": chara_info.get("turn"),
        "trainee_card_id": chara_info.get("card_id")})
    return True


_UNIQUE_SKILL_MAX_LEVEL = 6
_GAUGE_GREEN = 60


def _unique_skill_for(card_id, rarity) -> tuple:
    if card_id and (rarity or 0) >= 3:
        row = master_data.query_one(
            "SELECT s.skill_id1, s.skill_level1 FROM card_rarity_data r "
            "JOIN skill_set s ON s.id = r.skill_set "
            "WHERE r.card_id = ? AND r.rarity = ?", (int(card_id), int(rarity)))
        if row and row["skill_id1"]:
            return row["skill_id1"], max(1, min(3, row["skill_level1"] or 1))
    chara = (card_id or 0) // 100
    if not chara:
        return None, 0
    offset = (chara - 1000) * 10 + 1
    if (rarity or 0) < 3:
        return 900000 + offset, 1
    return 100000 + offset, max(1, min(3, int(rarity) - 2))


def _grant_unique_skill(chara_info: dict) -> None:
    skill_id, level = _unique_skill_for(chara_info.get("card_id"),
                                        chara_info.get("rarity"))
    if not skill_id:
        return
    arr = chara_info.setdefault("skill_array", [])
    for s in arr:
        if s.get("skill_id") == skill_id:
            return
    arr.append({"skill_id": skill_id, "level": level})


def _repair_unique_skill(viewer_id) -> None:
    full_state = state_store.get_state(viewer_id) or {}
    ci = ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("chara_info")
    if not isinstance(ci, dict) or not ci.get("card_id"):
        return
    right, _ = _unique_skill_for(ci["card_id"], ci.get("rarity"))
    wrong = 100000 + ((int(ci["card_id"]) // 100) - 1000) * 10 + 1
    if not right or right == wrong:
        return
    arr = ci.get("skill_array") or []
    stale = next((s for s in arr if s.get("skill_id") == wrong), None)
    if stale is None:
        return
    if any(s.get("skill_id") == right for s in arr):
        arr.remove(stale)
    else:
        stale["skill_id"] = right
    state_store.save_state(viewer_id, full_state)


def _repair_storyless_events(viewer_id, response=None) -> None:
    full_state = state_store.get_state(viewer_id) or {}
    changed = False

    def heal(node):
        nonlocal changed
        if isinstance(node, dict):
            eid = node.get("event_id")
            if (node.get("story_id") == 0 and isinstance(eid, int)
                    and single_mode_events.is_scenario_event(eid)):
                fixed = single_mode_events.event_entry(eid, 0)
                node["story_id"] = fixed["story_id"]
                if "chara_id" in node:
                    node["chara_id"] = 0
                if "play_timing" in node:
                    node["play_timing"] = fixed["play_timing"]
                changed = True
            for v in node.values():
                heal(v)
        elif isinstance(node, list):
            for v in node:
                heal(v)

    heal(full_state.get(career_events.STATE_KEY))
    heal(full_state.get(DISPLAY_EVENT_KEY))
    if changed:
        state_store.save_state(viewer_id, full_state)
    heal((response or {}).get("data"))


def _level_unique_skill(chara_info: dict, by: int = 1) -> bool:
    skill_id, start = _unique_skill_for(chara_info.get("card_id"),
                                        chara_info.get("rarity"))
    if not skill_id:
        return False
    arr = chara_info.setdefault("skill_array", [])
    entry = next((s for s in arr if s.get("skill_id") == skill_id), None)
    if entry is None:
        entry = {"skill_id": skill_id, "level": start}
        arr.append(entry)
    new = min(_UNIQUE_SKILL_MAX_LEVEL, (entry.get("level") or start) + by)
    if new == entry.get("level"):
        return False
    entry["level"] = new
    return True


_UNIQUE_LEVEL_EVENTS = {
    51: (107, 3007, 60_000, 40_000, False),
    55: (108, 3008, 70_000, 60_000, True),
    72: (109, 3009, 120_000, 80_000, False),
}
UNIQUE_LEVEL_FIRED_KEY = "unique_skill_events_fired"


def _is_dirt_trainee(chara_info: dict) -> bool:
    return (chara_info.get("proper_ground_dirt") or 0) > (
        chara_info.get("proper_ground_turf") or 0)


SCENARIO_FIXED_FIRED_KEY = "scenario_fixed_fired"
GRAND_LIVE_EVENTS_FIRED_KEY = "grand_live_events_fired"


_DIRECTOR_FAN_EVENTS = (
    (400001028, [{"type": "speed", "value": "+10"}]),
    (400001029, [{"type": "power", "value": "+10"}]),
    (400001026, [{"type": "wisdom", "value": "+10"}]),
    (400001023, [{"type": "mood", "value": "+1"}]),
)
_DIRECTOR_FAN_CHANCE = 0.10
DIRECTOR_FAN_FIRED_KEY = "director_fan_fired"


def _maybe_fire_condition_event(full_state: dict, career: dict, response: dict) -> bool:
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    for cid in conditions.rolled_conditions(chara_info):
        suffix = conditions.CONDITION_STORY_SUFFIX.get(cid)
        if not suffix:
            continue
        story_id = 500000000 + chara * 1000 + suffix
        if not _story_exists(story_id):
            continue
        effects = [{"type": "energy",
                    "value": str(conditions.CONDITION_PROC_ENERGY)}]
        if random.random() < conditions.proc_mood_chance(cid):
            effects.append({"type": "mood", "value": "-1"})
        ev = {"choices": [{"effects": effects}]}
        entry = event_engine.career_event_entry(
            ev, _shared_beat_event_id(suffix, chara_info.get("turn")),
            story_id, chara_id=chara,
            support_card_id=0, play_timing=1)
        _queue_turn_event(response, full_state, entry)
        _push_career_ctx(full_state, {
            "event_id": entry["event_id"], "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": chara_info.get("card_id"), "event": ev})
        return True
    return False


REFRESH_FIRES_KEY = "refresh_mind_fires"


def _maybe_fire_refresh_event(full_state: dict, career: dict, response: dict) -> bool:
    chara_info = career["data"]["chara_info"]
    if not conditions.has(chara_info, conditions.REFRESH_MIND):
        full_state.pop(REFRESH_FIRES_KEY, None)
        return False
    fires = int(full_state.get(REFRESH_FIRES_KEY) or 0) + 1
    effects = [{"type": "energy", "value": f"+{conditions.REFRESH_ENERGY}"}]
    if conditions.refresh_ends(fires):
        effects.append({"type": "condition_end", "condition_id": conditions.REFRESH_MIND})
        full_state.pop(REFRESH_FIRES_KEY, None)
    else:
        full_state[REFRESH_FIRES_KEY] = fires
    ev = {"choices": [{"effects": effects}]}
    entry = event_engine.career_event_entry(
        ev, conditions.REFRESH_EVENT_ID, conditions.REFRESH_STORY_ID,
        chara_id=0, support_card_id=0, play_timing=1)
    _queue_turn_event(response, full_state, entry)
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": conditions.REFRESH_STORY_ID,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(conditions.REFRESH_STORY_ID) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    return True


EPITHET_RUN_FIRED_KEY = "epithets_announced_in_run"


def _maybe_fire_epithet_events(full_state: dict, career: dict,
                               response: dict) -> int:
    chara_info = career["data"]["chara_info"]
    if (chara_info.get("scenario_id") or 0) != scenarios.trackblazer.impl.SCENARIO_ID:
        return 0
    fired = [int(x) for x in (full_state.get(EPITHET_RUN_FIRED_KEY) or ())]
    new = epithets.newly_completed_in_run(career["data"], chara_info, fired)
    if not new:
        return 0
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    for nickname_id, event_id, story_id in new:
        rng = random.Random("%s:epithet:%s" % (card_id, nickname_id))
        ev = {"choices": [{"effects":
                           epithets.trackblazer_reward_effects(event_id, rng)}]}
        entry = event_engine.career_event_entry(
            ev, event_id, story_id, chara_id=chara, support_card_id=0,
            play_timing=3)
        _queue_turn_event(response, full_state, entry)
        _push_career_ctx(full_state, {
            "event_id": entry["event_id"], "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": card_id, "event": ev})
        fired.append(nickname_id)
    full_state[EPITHET_RUN_FIRED_KEY] = fired
    log.info("trackblazer epithets completed: %s",
             [(n, e) for n, e, _s in new])
    return len(new)


def _maybe_fire_race_fatigue(full_state: dict, career: dict, response: dict,
                             race_ctx: dict) -> bool:
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    turn = race_ctx.get("turn")
    if turn in scenarios.for_chara(chara_info).race_fatigue_exempt_turns:
        return False
    streak = race_fatigue.consecutive_races(
        career["data"].get("race_history") or [], turn)
    outcome = race_fatigue.roll(streak, race_ctx.get("vital_at_entry"))
    if not outcome:
        return False
    story_id = 500000000 + chara * 1000 + outcome["suffix"]
    if not _story_exists(story_id):
        return False
    ev = {"choices": [{"effects": outcome["effects"]}]}
    entry = event_engine.career_event_entry(
        ev, _shared_beat_event_id(outcome["suffix"], turn), story_id,
        chara_id=chara, support_card_id=0, play_timing=3)
    _queue_turn_event(response, full_state, entry)
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    log.info("race fatigue: %s consecutive race turns at turn %s (energy %s) "
             "-> story %s%s", streak, turn, race_ctx.get("vital_at_entry"),
             story_id, " + Skin Outbreak" if outcome["skin_outbreak"] else "")
    return True


def _maybe_fire_director_fan(full_state: dict, career: dict, response: dict) -> bool:
    chara_info = career["data"]["chara_info"]
    if _bond_of(chara_info, _DIRECTOR_TARGET) < _DIRECTOR_MAX_BOND:
        return False
    fired = set(full_state.get(DIRECTOR_FAN_FIRED_KEY) or ())
    pool = [(sid, eff) for sid, eff in _DIRECTOR_FAN_EVENTS if sid not in fired]
    if not pool or random.random() >= _DIRECTOR_FAN_CHANCE:
        return False
    story_id, effects = random.choice(pool)
    if not _story_exists(story_id):
        return False
    ev = {"choices": [{"effects": effects}]}
    entry = event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, story_id,
        chara_id=(chara_info.get("card_id") or 0) // 100,
        support_card_id=0, play_timing=1)
    _queue_turn_event(response, full_state, entry)
    full_state[DIRECTOR_FAN_FIRED_KEY] = sorted(fired | {story_id})
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    return True


def _maybe_queue_unique_level(response: dict, full_state: dict, career: dict,
                              turn) -> bool:
    spec = _UNIQUE_LEVEL_EVENTS.get(turn)
    if not spec:
        return False
    chara_info = career["data"]["chara_info"]
    if not scenarios.for_chara(chara_info).plays_calendar_stories:
        return False
    suffix, event_id, need_turf, need_dirt, needs_green = spec
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    story_id = 500000000 + chara * 1000 + suffix
    fired = set(full_state.get(UNIQUE_LEVEL_FIRED_KEY) or ())
    if story_id in fired or not _story_exists(story_id):
        return False
    need = need_dirt if _is_dirt_trainee(chara_info) else need_turf
    met = (chara_info.get("fans") or 0) >= need
    if met and needs_green:
        met = _bond_of(chara_info, _DIRECTOR_TARGET) >= _GAUGE_GREEN
    effects = []
    if met:
        effects.append({"type": "unique_skill_level", "value": "+1"})
        if needs_green:
            effects.append({"type": "mood", "value": "+1"})
    elif needs_green:
        effects.append({"type": "energy", "value": "-5"})
    ev = {"choices": [{"effects": effects}]}
    entry = event_engine.career_event_entry(
        ev, event_id, story_id, chara_id=chara,
        support_card_id=0, play_timing=1)
    _queue_turn_event(response, full_state, entry)
    full_state[UNIQUE_LEVEL_FIRED_KEY] = sorted(fired | {story_id})
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    return True


_CHARA_FIXED_STORY_EVENTS = {
    25: [(101, 3001, 1)],
    37: [(103, 3003, 1)],
    40: [(104, 3004, 1)],
    41: [(300, 3010, 6)],
    49: [(102, 3002, 1)],
    50: [(302, 6001, 6)],
}

RAFFLE_CTX_KEY = "raffle_ctx"
HOT_SPRING_KEY = "hot_spring_ticket"
_HOT_SPRING_ITEM_ID = 1
_HOT_SPRING_SUFFIX = 112
_HOT_SPRING_EVENT_ID = 7
_RAFFLE_PRIZES = (
    (5, 10, [{"type": "mood", "value": "-1"}]),
    (4, 50, [{"type": "energy", "value": "+20"}]),
    (3, 30, [{"type": "energy", "value": "+20"}, {"type": "mood", "value": "+1"},
             {"type": "all_stats", "value": "+5"}]),
    (2, 5, [{"type": "energy", "value": "+30"}, {"type": "mood", "value": "+1"},
            {"type": "all_stats", "value": "+10"}]),
    (1, 5, [{"type": "energy", "value": "+30"}, {"type": "mood", "value": "+1"},
            {"type": "all_stats", "value": "+10"}]),
)
_RAFFLE_EVENT_ID = 6001


def _roll_raffle() -> tuple:
    total = sum(w for _i, w, _e in _RAFFLE_PRIZES)
    r = random.uniform(0, total)
    upto = 0.0
    for item_id, w, effects in _RAFFLE_PRIZES:
        upto += w
        if r <= upto:
            return item_id, effects
    return _RAFFLE_PRIZES[-1][0], _RAFFLE_PRIZES[-1][2]


_CHARA_FIXED_STORY_EVENTS.update({
    61: [(105, 3005, 1)],
    65: [(301, 3011, 6)],
})

_CHAPTER_SLOT_BASE = 11179 - 113
_CHAPTER_GRACE_TURNS = 2
CHAPTER_FIRED_KEY = "chapter_stories_fired"
PLAYED_STORIES_KEY = "gate_stories_played"
STORY_CHOICES_KEY = "gate_story_choices"


_FIXED_UNSCHEDULED_STORIES = {501097119: 67}
_CLIENT_SWAPPED_STORIES = frozenset({501097120})


def _chapter_excluded(card_id: int) -> frozenset:
    chara = card_id // 100
    return (_branch_story_ids(chara) | _secret_story_ids(card_id)
            | frozenset(_race_restrict_stories().values())
            | frozenset(s for (c, _t), ids in _FORCED_REST_TURNS.items() if c == chara
                        for s in ids))


_AFTER_RACE_CHAPTERS = {501103114: 13}


def _maybe_queue_chapters(full_state: dict, career: dict, response: dict, new_turn,
                          after_race=None) -> None:
    chara_info = career["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara or new_turn is None or not scenarios.for_chara(chara_info).has_chapter_stories:
        return
    fired = career["data"].setdefault(CHAPTER_FIRED_KEY, [])
    played = set(career["data"].get(PLAYED_STORIES_KEY) or ())
    choices = {int(k): v for k, v in (career["data"].get(STORY_CHOICES_KEY) or {}).items()}
    excluded = _chapter_excluded(card_id)
    lo = 500000000 + chara * 1000
    extra = tuple((s, t) for s, t in _FIXED_UNSCHEDULED_STORIES.items() if lo <= s < lo + 1000)
    for story, turn in career_branches.chapter_stories(chara) + extra:
        if story in fired or story in excluded:
            continue
        if story in _AFTER_RACE_CHAPTERS:
            if after_race is None or int(after_race) < _AFTER_RACE_CHAPTERS[story]:
                continue
        elif not turn <= new_turn <= turn + _CHAPTER_GRACE_TURNS:
            continue
        if not career_branches.gate_met(story, played, choices):
            continue
        fired.append(story)
        _queue_chapter(full_state, response, chara_info, story)


@functools.lru_cache(maxsize=1)
def _real_chapter_event_ids() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "events",
                        "real_chapter_event_ids.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {int(k): int(v) for k, v in json.load(fh)["ids"].items()}
    except (OSError, ValueError, KeyError):
        log.exception("real chapter event ids unreadable; using the slot band")
        return {}


def _chapter_event_id(story: int) -> int:
    return _real_chapter_event_ids().get(story) or _CHAPTER_SLOT_BASE + story % 1000


def _queue_chapter(full_state: dict, response: dict, chara_info: dict, story: int) -> None:
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    slot = _chapter_event_id(story)
    title = event_engine.event_title(story)
    ev = event_engine.resolve(story, title, card_id)
    if not ev or not ev.get("choices"):
        _queue_turn_event(response, full_state, _scenario_event_entry(
            slot, story, play_timing=1, choices=[_ack_choice()], chara_id=chara))
        return
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, slot, story, chara_id=chara, support_card_id=0, play_timing=1,
        chara_info=chara_info))
    _push_career_ctx(full_state, {
        "event_id": slot, "story_id": story, "source": "chara", "source_id": card_id,
        "title": title, "trainee_card_id": card_id, "event": ev})


_SPECIAL_HOME_SCHEDULE = {
    1097: ((46, 53, 3), (55, 60, 4), (61, 67, 5), (68, 70, 6), (71, 78, 5)),
    1143: ((54, 54, 7),),
}
_FORCED_REST_TURNS = {(1143, 54): (501143116, 501143117)}


def _forced_rest_stories(chara_info: dict) -> tuple:
    return _FORCED_REST_TURNS.get(((chara_info.get("card_id") or 0) // 100,
                                   chara_info.get("turn")), ())
_SPECIAL_HOME_HOLD_STORIES = {(1097, 55): 501097108, (1097, 68): 501097117}


def _special_home_id(chara_info: dict, pending=(), fired=None) -> int:
    turn = chara_info.get("turn")
    chara = (chara_info.get("card_id") or 0) // 100
    rows = _SPECIAL_HOME_SCHEDULE.get(chara)
    if not rows or turn is None:
        return 0
    hold = _SPECIAL_HOME_HOLD_STORIES.get((chara, turn))
    if hold and (hold in pending or (fired is not None and hold not in fired)):
        return 0
    return next((sid for lo, hi, sid in rows if lo <= turn <= hi), 0)


def _stamp_special_home(response: dict, viewer_id=None) -> None:
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    fired = None
    for holder in (data, data.get("single_mode_load_common"),
                   data.get("single_mode_start_common")):
        if not isinstance(holder, dict):
            continue
        ci, home = holder.get("chara_info") or data.get("chara_info"), holder.get("home_info")
        if not isinstance(ci, dict) or not isinstance(home, dict):
            continue
        if (ci.get("card_id") or 0) // 100 in _SPECIAL_HOME_SCHEDULE:
            pending = [e.get("story_id") for e in (holder.get("unchecked_event_array")
                       or data.get("unchecked_event_array") or []) if isinstance(e, dict)]
            if fired is None and viewer_id is not None:
                career = (state_store.get_state(viewer_id) or {}).get(STATE_KEY) or {}
                fired = set((career.get("data") or {}).get(CHAPTER_FIRED_KEY) or ())
            home["special_home_id"] = _special_home_id(ci, pending, fired)


def _stamp_scheduled_conditions(response: dict) -> None:
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    for holder in (data, data.get("single_mode_load_common"),
                   data.get("single_mode_start_common")):
        ci = holder.get("chara_info") if isinstance(holder, dict) else None
        if isinstance(ci, dict):
            conditions.sync_scheduled(ci)


def record_gate_stories(viewer_id, response: dict) -> None:
    data = (response or {}).get("data") or {}
    parents = career_branches.gate_parents()
    served = set()
    for holder in (data, data.get("single_mode_load_common")):
        for entry in (holder or {}).get("unchecked_event_array") or ():
            sid = _reference_story_id(entry.get("story_id") or 0)
            if sid in parents:
                served.add(sid)
    if not served:
        return
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY) or {}
    if not career.get("data"):
        return
    played = career["data"].setdefault(PLAYED_STORIES_KEY, [])
    new = sorted(served - set(played))
    if new:
        played.extend(new)
        state_store.save_state(viewer_id, full_state)


def _record_gate_choice(full_state: dict, event_id, choice_number) -> bool:
    if not choice_number:
        return False
    story = _pending_event_story(full_state, event_id)
    story = _reference_story_id(story) if story else None
    if story not in career_branches.gate_parents():
        return False
    career = full_state.get(STATE_KEY) or {}
    if not career.get("data"):
        return False
    career["data"].setdefault(STORY_CHOICES_KEY, {})[str(story)] = int(choice_number)
    return True
CHARA_STORY_FIRED_KEY = "chara_story_turns_fired"
RACE_RESTRICT_UNTIL_KEY = "race_restrict_until_turn"


@functools.lru_cache(maxsize=1)
def _race_restrict_stories() -> dict:
    out = {}
    for r in master_data.query(
            "SELECT chara_id, turn, gain_id FROM single_mode_race_restrict_turn"):
        gain = r["gain_id"] or 0
        chara, suffix = gain // 100000, (gain // 100) % 1000
        if chara != r["chara_id"] or not suffix:
            continue
        out[(r["chara_id"], r["turn"])] = 500000000 + chara * 1000 + suffix
    return out


def _maybe_fire_race_restrict_story(full_state: dict, career: dict, response: dict,
                                    new_turn) -> bool:
    chara_info = career["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    sid = _race_restrict_stories().get((chara, new_turn))
    if not sid:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if sid in fired:
        return False
    title = master_data.query_one(
        'SELECT text FROM text_data WHERE category=181 AND "index"=?', (sid,))
    ev = event_engine.resolve(sid, title["text"] if title else None, card_id)
    synthesized = False
    if not ev or not ev.get("choices"):
        ev = {"name": title["text"] if title else None,
              "choices": [{"effects": [{"type": "race_restrict", "turns": 1}]}]}
        synthesized = True
    ctx = {"event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
           "source": "chara", "source_id": card_id,
           "title": title["text"] if title else None, "trainee_card_id": card_id}
    rs, stories = _live_branches(chara_info)
    bs = stories.get(sid)
    if bs is not None and not bs.by_select_index:
        ev = _branch_event(ev, ctx["title"], rs, bs)
        ctx["event"] = ev
    elif synthesized:
        ctx["event"] = ev
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara, support_card_id=0,
        play_timing=1, chara_info=chara_info))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
    _push_career_ctx(full_state, ctx)
    return True


_BRANCH_EVENT_ID = 11440


@functools.lru_cache(maxsize=512)
def _personal_race_set(chara_id: int) -> int | None:
    row = master_data.query_one(
        "SELECT race_set_id FROM single_mode_route WHERE scenario_id=0 AND chara_id=? "
        "ORDER BY priority DESC LIMIT 1", (chara_id,))
    return row["race_set_id"] if row else None


@functools.lru_cache(maxsize=512)
def _branch_story_ids(chara_id: int) -> frozenset:
    rs = _personal_race_set(chara_id)
    return frozenset(career_branches.branch_stories(chara_id, rs)) if rs else frozenset()


def _live_branches(chara_info: dict) -> tuple:
    chara = (chara_info.get("card_id") or 0) // 100
    rs = _race_set_of(chara_info.get("route_race_id_array"))
    if not chara or rs is None:
        return None, {}
    return rs, career_branches.branch_stories(chara, rs)


@functools.lru_cache(maxsize=2048)
def _reference_story_id(story_id: int) -> int:
    row = master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE short_story_id=? LIMIT 1",
        (story_id,))
    return row["story_id"] if row else story_id


def _apply_branch_now(full_state: dict, chara_info: dict, story_id: int) -> bool:
    rs, stories = _live_branches(chara_info)
    bs = stories.get(story_id)
    if bs is None or len(bs.choices) != 1 or bs.by_select_index:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if story_id in fired:
        return False
    changed = False
    for eff in career_branches.effects_for_choice(rs, bs, 0):
        changed |= switch_route_race(chara_info, eff["race_instance_id"], eff["route_race_id"])
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [story_id]
    if changed:
        refresh_route_screen(full_state, chara_info)
    return changed


def _branch_event(ev: dict | None, title: str | None, rs: int, bs) -> dict:
    base = [c for c in ((ev or {}).get("choices") or ())]
    out = copy.deepcopy(ev) if ev else {"name": title}
    choices = []
    for i in range(len(bs.choices)):
        src = copy.deepcopy(base[i] if len(base) == len(bs.choices) else (base[0] if base else {}))
        src["effects"] = [e for e in (src.get("effects") or ())
                          if e.get("type") != "race_change"] + \
            career_branches.effects_for_choice(rs, bs, i)
        for seg in src.get("segments") or ():
            seg["effects"] = [e for e in (seg.get("effects") or ())
                              if e.get("type") != "race_change"] + \
                career_branches.effects_for_choice(rs, bs, i)
        choices.append(src)
    out["choices"] = choices
    return out


def _maybe_fire_branch_story(full_state: dict, career: dict, response: dict, new_turn) -> bool:
    chara_info = career["data"]["chara_info"]
    if new_turn is None:
        return False
    rs, stories = _live_branches(chara_info)
    if not stories:
        return False
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    secret = _secret_story_ids(card_id) if card_id else frozenset()
    scheduled = set(_race_restrict_stories().values())
    for sid, bs in sorted(stories.items()):
        if (sid in fired or sid in secret or sid in scheduled or bs.by_select_index
                or career_branches.anchor_turn(chara, rs, sid)):
            continue
        if sid % 1000 >= 300 and len(bs.choices) == 1:
            continue
        if not career_branches.fire_turn(rs, bs) <= new_turn < bs.first_turn:
            continue
        if _career_ctx_pending(full_state, _BRANCH_EVENT_ID):
            return False
        title = event_engine.event_title(sid)
        ev = _branch_event(event_engine.resolve(sid, title, card_id), title, rs, bs)
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, _BRANCH_EVENT_ID, sid, chara_id=chara, support_card_id=0,
            play_timing=1, chara_info=chara_info))
        full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
        _push_career_ctx(full_state, {
            "event_id": _BRANCH_EVENT_ID, "story_id": sid,
            "source": "chara", "source_id": card_id, "title": title,
            "trainee_card_id": card_id, "event": ev})
        log.info("branch story %s fired on turn %s (branch from turn %s)",
                 sid, new_turn, bs.first_turn)
        return True
    return False


def _branch_post_race(full_state: dict, chara_info: dict, entry: dict, ev: dict) -> dict:
    ref = _reference_story_id(entry.get("story_id") or 0)
    rs, stories = _live_branches(chara_info)
    bs = stories.get(ref)
    card_id = chara_info.get("card_id") or 0
    if bs is None or bs.by_select_index or ref in (_secret_story_ids(card_id) if card_id else ()):
        return ev
    if len(bs.choices) == 1:
        _apply_branch_now(full_state, chara_info, ref)
        return ev
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if ref in fired:
        return ev
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [ref]
    branched = _branch_event(ev, None, rs, bs)
    entry["event_contents_info"]["choice_array"] = [
        {"select_index": i, "receive_item_id": 0,
         "target_race_id": (bs.choices[i - 1] or (0,))[0],
         "gain_select_id_index": i, "select_icon": 0}
        for i in range(1, len(bs.choices) + 1)]
    return branched


def _maybe_queue_chara_story(response: dict, full_state: dict, career: dict, new_turn,
                             after_race=None) -> None:
    chara_info_ = career["data"]["chara_info"]
    try:
        _maybe_fire_race_restrict_story(full_state, career, response, new_turn)
    except Exception:
        log.exception("race-restrict story failed; skipping")
    try:
        _maybe_fire_branch_story(full_state, career, response, new_turn)
    except Exception:
        log.exception("branch story failed; skipping")
    try:
        _maybe_queue_chapters(full_state, career, response, new_turn, after_race)
    except Exception:
        log.exception("chapter stories failed; skipping")
    if not scenarios.for_chara(chara_info_).plays_calendar_stories:
        return
    entries = list(_CHARA_FIXED_STORY_EVENTS.get(new_turn) or ())
    if not entries:
        return
    fired = career["data"].setdefault(CHARA_STORY_FIRED_KEY, [])
    if new_turn in fired:
        return
    chara = (career["data"]["chara_info"].get("card_id") or 0) // 100
    if not chara:
        return
    fired.append(new_turn)
    card_id = career["data"]["chara_info"].get("card_id") or 0
    for suffix, slot, timing in entries:
        story = 500000000 + chara * 1000 + suffix
        if not master_data.query_one(
                "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story,)):
            continue
        kind = {101: "new_year", 102: "new_year_2", 104: "summer_camp"}.get(suffix)
        if kind:
            ev = event_engine.seasonal_event(kind, card_id)
            if ev:
                _queue_turn_event(response, full_state, event_engine.career_event_entry(
                    ev, event_engine.CHARA_EVENT_ID, story, chara_id=chara,
                    support_card_id=0, play_timing=timing))
                _push_career_ctx(full_state, {
                    "event_id": event_engine.CHARA_EVENT_ID, "story_id": story,
                    "source": "seasonal", "source_id": card_id, "title": kind,
                    "trainee_card_id": card_id})
                if kind == "new_year":
                    _queue_pal_new_year(response, full_state, career)
                continue
        if suffix == 302:
            item_id, effects = _roll_raffle()
            if item_id == _HOT_SPRING_ITEM_ID:
                full_state[HOT_SPRING_KEY] = 1
                log.info("raffle: Hot Spring Ticket drawn -- the end-of-career "
                         "Hot Spring Getaway is now unlocked")
            entry = _scenario_event_entry(_RAFFLE_EVENT_ID, story,
                                          play_timing=timing,
                                          choices=[_ack_choice(item_id, item_id)],
                                          chara_id=chara)
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=_RAFFLE_EVENT_ID, story_id=story, raw=entry,
                choices=[career_events.Choice(effects=effects)],
                priority=career_events.PRIO_SPECIAL, source="raffle"))
            continue
        try:
            _apply_branch_now(full_state, chara_info_, story)
        except Exception:
            log.exception("branch on story %s failed; skipping", story)
        _queue_turn_event(response, full_state, _scenario_event_entry(
            slot, story, play_timing=timing, choices=[_ack_choice()], chara_id=chara))


_CHARA_RANDOM_CHANCE = 0.07


@functools.lru_cache(maxsize=256)
def _chara_random_stories(chara: int, trainee_card_id: int = 0) -> tuple:
    base = 500000000 + chara * 1000
    rows = master_data.query(
        "SELECT [index], text FROM text_data WHERE category=181 "
        "AND ([index] BETWEEN ? AND ? OR [index] BETWEEN ? AND ? "
        "     OR [index] BETWEEN ? AND ?) ORDER BY [index]",
        (base + 500, base + 599, base + 800, base + 899,
         base + 113, base + 199))
    scheduled = set(_race_restrict_stories().values())
    rows = [r for r in rows if r["index"] not in scheduled]
    branching = _branch_story_ids(chara)
    rows = [r for r in rows if r["index"] not in branching]
    chapters = (career_branches.chapter_band_ids(chara) | set(_FIXED_UNSCHEDULED_STORIES)
                | _CLIENT_SWAPPED_STORIES)
    rows = [r for r in rows if r["index"] not in chapters]
    secret = _secret_story_ids(trainee_card_id) if trainee_card_id else frozenset()
    rows = [r for r in rows if r["index"] not in secret]
    rows = [r for r in rows if r["index"] - base not in
            (race_fatigue.FATIGUE_SUFFIX, race_fatigue.REPEATED_SUFFIX)]
    mine = event_engine.card_event_title_keys(trainee_card_id) if trainee_card_id else None
    if not mine:
        return tuple((r["index"], r["text"]) for r in rows)
    theirs = set()
    for sib in event_engine.sibling_card_ids(trainee_card_id):
        theirs |= event_engine.card_event_title_keys(sib)
    theirs -= mine
    return tuple((r["index"], r["text"]) for r in rows
                 if event_engine.title_key(r["text"]) not in theirs)


def _maybe_fire_chara_random_event(full_state: dict, career_state: dict, response: dict,
                                   force: bool = False) -> bool:
    if not force and random.random() >= tuning.event(_CHARA_RANDOM_CHANCE):
        return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    candidates = [(sid, title) for sid, title in _chara_random_stories(chara, card_id)
                  if sid not in fired]
    random.shuffle(candidates)
    for sid, title in candidates:
        ev = event_engine.resolve(sid, title, card_id)
        synthesized = False
        if not ev or not ev.get("choices"):
            ev = event_engine.shared_event(sid % 1000, card_id)
            synthesized = True
        if not ev or not ev.get("choices"):
            continue
        event_id = _shared_beat_event_id(sid % 1000, chara_info.get("turn"))
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_id, sid, chara_id=chara,
            support_card_id=0, play_timing=6))
        full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
        ctx = {"event_id": event_id, "story_id": sid,
               "source": "chara", "source_id": card_id, "title": title,
               "trainee_card_id": card_id}
        if synthesized:
            ctx["event"] = ev
        _push_career_ctx(full_state, ctx)
        return True
    return False


@functools.lru_cache(maxsize=2048)
def _race_instance_for_program(program_id) -> int | None:
    if not program_id:
        return None
    row = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (program_id,))
    return row["race_instance_id"] if row else None


@functools.lru_cache(maxsize=2048)
def _race_facts_for_program(program_id) -> dict | None:
    if not program_id:
        return None
    row = master_data.query_one(
        "SELECT ri.id AS race_id, r.grade AS grade, cs.distance AS distance, "
        "       cs.ground AS ground, cs.race_track_id AS track_id "
        "FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id "
        "LEFT JOIN race_course_set cs ON cs.id = r.course_set "
        "WHERE p.id=?", (program_id,))
    return dict(row) if row else None


_RACE_PERMISSION_YEARS = {1: (1,), 2: (2,), 3: (3,), 4: (2, 3)}


@functools.lru_cache(maxsize=4096)
def _race_entry_turns(race_id) -> tuple:
    out = []
    for r in master_data.query(
            "SELECT month, half, race_permission FROM single_mode_program "
            "WHERE race_instance_id=?", (race_id,)):
        for year in _RACE_PERMISSION_YEARS.get(r["race_permission"], ()):
            out.append(((year - 1) * 24 + ((r["month"] or 1) - 1) * 2 + (r["half"] or 1),
                        year))
    return tuple(sorted(set(out)))


def _race_deadline(race_id, year=None):
    turns = [t for t, y in _race_entry_turns(race_id) if year is None or y == year]
    return max(turns) if turns else None


def _secret_context(full_state: dict, career_data: dict) -> secret_events.Context:
    chara_info = career_data.get("chara_info") or {}
    history = career_data.get("race_history") or []
    goals = _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
    objective_programs = {g[3] for g in goals if g[2] == 1}
    races = []
    for h in history:
        facts = _race_facts_for_program(h.get("program_id")) or {}
        if not facts.get("race_id"):
            continue
        ranks = h.get("rival_ranks")
        races.append(secret_events.Race(
            program_id=h.get("program_id") or 0,
            race_id=facts["race_id"],
            turn=h.get("turn") or 0,
            rank=h.get("result_rank") or 99,
            running_style=h.get("running_style") or 0,
            grade=facts.get("grade") or 0,
            distance=facts.get("distance") or 0,
            ground=facts.get("ground") or 0,
            track_id=facts.get("track_id") or 0,
            objective=h.get("program_id") in objective_programs,
            popularity=h.get("popularity"),
            rival_ranks=({int(k): int(v) for k, v in ranks.items()}
                         if isinstance(ranks, dict) else None),
        ))
    return secret_events.Context(
        races=races,
        turn=chara_info.get("turn") or 0,
        fans=chara_info.get("fans") or 0,
        goals_cleared=_goals_cleared_count(chara_info, history),
        fired_story_ids=set(full_state.get(event_engine.FIRED_EVENTS_KEY) or ()),
        promises_fulfilled=set(career_data.get(FAN_PROMISES_DONE_KEY) or ()),
        race_deadline=_race_deadline,
    )


def _secret_events_for(card_id: int) -> list:
    out = []
    chara = card_id // 100
    if not chara:
        return out
    base = 500000000 + chara * 1000
    titles = {}
    for table in ("text_data", "main.text_data"):
        for r in master_data.query(
                f'SELECT "index" i, text FROM {table} WHERE category=181 '
                'AND "index" BETWEEN ? AND ?', (base, base + 999)):
            if r["text"]:
                titles.setdefault(event_engine.title_key(r["text"]), r["i"])
    for ev in (event_engine.card_events_for_trainee(card_id) or {}).values():
        if not isinstance(ev, dict) or ev.get("section") != "secret":
            continue
        sid = titles.get(event_engine.title_key(ev.get("name") or ""))
        if sid and ev.get("choices"):
            out.append((sid, ev["name"], ev))
    out.sort(key=lambda t: t[0])
    return out


@functools.lru_cache(maxsize=256)
def _secret_story_ids(card_id: int) -> frozenset:
    try:
        return frozenset(sid for sid, _title, ev in _secret_events_for(card_id)
                         if ev.get("conditions"))
    except Exception:
        log.exception("secret story id lookup failed for card %s", card_id)
        return frozenset()


_SECRET_EVENT_IDS = {501032114: 11440}


def _maybe_fire_secret_event(full_state: dict, career_state: dict, response: dict) -> bool:
    career_data = career_state["data"]
    chara_info = career_data["chara_info"]
    if not scenarios.for_chara(chara_info).has_secret_events:
        return False
    card_id = chara_info.get("card_id") or 0
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    ctx = _secret_context(full_state, career_data)
    due = [(sid, title, ev) for sid, title, ev in _secret_events_for(card_id)
           if sid not in fired and secret_events.is_eligible(ev, ctx)]
    thresholds = [t for t in (secret_events.win_g1_threshold(e) for _s, _t, e in due)
                  if t is not None]
    if thresholds:
        best = max(thresholds)
        due = [(sid, title, ev) for sid, title, ev in due
               if secret_events.win_g1_threshold(ev) in (None, best)]
    for sid, title, ev in due:
        ev = _resolve_reward_branch(ev, ctx)
        event_id = _SECRET_EVENT_IDS.get(sid, event_engine.CHARA_EVENT_ID)
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_id, sid, chara_id=card_id // 100, support_card_id=0,
            play_timing=1, chara_info=chara_info))
        full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
        event_ctx = {
            "event_id": event_id, "story_id": sid,
            "source": "chara", "source_id": card_id, "title": title,
            "trainee_card_id": card_id}
        if ev.get("_branch_resolved"):
            event_ctx["event"] = ev
        _push_career_ctx(full_state, event_ctx)
        return True
    return False


def _resolve_reward_branch(ev: dict, ctx) -> dict:
    if secret_events.branch_count(ev, ctx) is None:
        return ev
    out = copy.deepcopy(ev)
    changed = False
    for choice in out.get("choices") or ():
        idx = secret_events.branch_segment_index(ev, ctx, choice)
        if idx is None:
            continue
        seg = (choice.get("segments") or [])[idx]
        for key in ("effects", "outcomes", "probs", "random_either"):
            choice.pop(key, None)
        choice.update({k: v for k, v in seg.items() if k != "when"})
        choice.pop("segments", None)
        changed = True
    if not changed:
        return ev
    out["_branch_resolved"] = True
    return out


_EXTRA_TRAINING_CHANCE = 0.06
_ACUPUNCTURE_CHANCE = 1 / 400
_EXTRA_TRAINING_SUFFIX = 715
_ACUPUNCTURE_SUFFIX = 720


_SHARED_BEAT_EVENT_IDS = {
    500: 10000,
    506: 10000,
    510: 10000,
    511: 10000,
    520: 10000,
    519: 10012,
    517: 10012,
    518: 10012,
    524: 10000,
    516: 7019,
    523: 7000,
    507: 10001,
    101: 3001,
    102: 3002,
    104: 3004,
    107: 3007,
    108: 3008,
    109: 3009,
    111: 6,
    300: 3010,
    301: 3011,
    302: 6001,
    100: 3000, 400: 3000,
    103: 3003,
    105: 3005,
    600: 3016,
    601: 3017,
    800: 3061,
    900: 4000,
    700: 7009,
    701: 7011,
    705: None,
    708: 7005,
    709: 7006,
    710: 7007,
    713: 7014,
    714: 7015,
    715: 7017,
    717: 7016,
    718: 7010,
    719: 7022,
    720: 7020,
    722: 7023,
    726: 6002,
    731: 6007,
    732: 7044, 733: 7045,
    508: 7003,
    509: 7004,
    522: 7002,
    702: 6000, 703: 6000, 704: 6000,
}
_SHARED_BEAT_EVENT_IDS = {k: v for k, v in _SHARED_BEAT_EVENT_IDS.items()
                          if v is not None}
_SHARED_BEAT_EVENT_IDS_INFERRED: dict = {}


_SHARED_BEAT_EVENT_IDS_YEAR3 = {732: 7046, 733: 7047}
_YEAR3_FIRST_TURN = 49


def _shared_beat_event_id(suffix: int, turn=None) -> int:
    if suffix in _SHARED_BEAT_EVENT_IDS:
        try:
            late = turn is not None and int(turn) >= _YEAR3_FIRST_TURN
        except (TypeError, ValueError):
            late = False
        if late and suffix in _SHARED_BEAT_EVENT_IDS_YEAR3:
            return _SHARED_BEAT_EVENT_IDS_YEAR3[suffix]
        return _SHARED_BEAT_EVENT_IDS[suffix]
    return _SHARED_BEAT_EVENT_IDS_INFERRED.get(suffix, event_engine.CHARA_EVENT_ID)


_EXTRA_TRAINING_LAST_TURN = 72


def _maybe_fire_shared_beat(full_state: dict, career_state: dict, response: dict,
                            suffix: int, chance: float, context: dict | None = None,
                            once_per_career: bool = True) -> bool:
    if random.random() >= chance:
        return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    sid = 500000000 + chara * 1000 + suffix
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if once_per_career and sid in fired:
        return False
    if not master_data.query_one(
            "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (sid,)):
        return False
    context = dict(context or {}, scenario_id=chara_info.get("scenario_id"))
    ev = event_engine.shared_event(suffix, card_id, context)
    if not ev or not ev.get("choices"):
        return False
    row = master_data.query_one(
        'SELECT text FROM text_data WHERE category=181 AND "index"=?', (sid,))
    event_id = _shared_beat_event_id(suffix)
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=chara,
        support_card_id=0, play_timing=1))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_id, "story_id": sid,
        "source": "chara", "source_id": card_id,
        "title": (row["text"] if row else ""), "trainee_card_id": card_id,
        "event": ev})
    return True


def _next_outing_event(full_state: dict, chara_info: dict):
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    card_id = chara_info.get("card_id") or 0
    if event_engine.chain_ended(full_state, f"chara:{int(card_id)}"):
        return None
    chara = card_id // 100
    for sid, title in event_engine.chara_outing_chain(card_id, chara):
        if sid not in fired:
            ev = event_engine.resolve(sid, title, card_id)
            if ev:
                return ev, sid, chara, title
    return None


_OUTING_CHANCE = 0.70


def _maybe_fire_outing_event(full_state: dict, career_state: dict, response: dict) -> bool:
    if random.random() >= _OUTING_CHANCE:
        return False
    chara_info = career_state["data"]["chara_info"]
    pick = _next_outing_event(full_state, chara_info)
    if not pick:
        return False
    ev, sid, chara, title = pick
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara,
        support_card_id=0, play_timing=6))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(
        full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
        "source": "chara", "source_id": chara_info.get("card_id"), "title": title,
        "trainee_card_id": chara_info.get("card_id")})
    return True


CAREER_CTX_QUEUE_KEY = "career_event_ctx_queue"


def _event_source_card(ctx: dict | None):
    if not ctx or ctx.get("source") != "support":
        return None
    return ctx.get("source_id")


def _push_career_ctx(full_state: dict, ctx: dict) -> None:
    queue = full_state.setdefault(CAREER_CTX_QUEUE_KEY, [])
    queue.append(ctx)
    full_state[event_engine.CAREER_EVENT_CTX_KEY] = ctx


def _career_ctx_for(full_state: dict, event_id) -> dict:
    for ctx in full_state.get(CAREER_CTX_QUEUE_KEY) or ():
        if ctx.get("event_id") == event_id:
            return ctx
    legacy = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    return legacy if legacy.get("event_id") == event_id else {}


def _career_ctx_pending(full_state: dict, event_id) -> bool:
    return any(c.get("event_id") == event_id
              for c in full_state.get(CAREER_CTX_QUEUE_KEY) or ())


def _drop_career_ctx(full_state: dict, ctx: dict) -> None:
    identity = (ctx.get("event_id"), ctx.get("story_id"))
    queue = [c for c in (full_state.get(CAREER_CTX_QUEUE_KEY) or ())
             if (c.get("event_id"), c.get("story_id")) != identity]
    if queue:
        full_state[CAREER_CTX_QUEUE_KEY] = queue
    else:
        full_state.pop(CAREER_CTX_QUEUE_KEY, None)
    legacy = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    if (legacy.get("event_id"), legacy.get("story_id")) == identity:
        full_state.pop(event_engine.CAREER_EVENT_CTX_KEY, None)


def _career_event_from_ctx(full_state: dict, event_id=None):
    if event_id is not None:
        ctx = _career_ctx_for(full_state, event_id)
    else:
        ctx = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    if not ctx:
        return None, ctx
    if ctx.get("event"):
        return ctx["event"], ctx
    if ctx.get("source") == "inline":
        return ctx.get("event"), ctx
    if ctx.get("source"):
        return event_engine.resolve_source(ctx["source"], ctx.get("source_id"), ctx.get("title")), ctx
    sid = ctx.get("story_id")
    if not sid:
        return None, ctx
    return event_engine.resolve(sid, event_engine.event_title(sid), ctx.get("trainee_card_id")), ctx


def _announce_due_passive_goals(response: dict, full_state: dict, career_state: dict,
                                acted_turn=None) -> None:
    try:
        ci_goal = career_state["data"]["chara_info"]
        history = career_state["data"].get("race_history", [])
        turn_acted = acted_turn
        if turn_acted is None:
            turn_acted = (ci_goal.get("turn") or 1) - 1
        goals_all = _route_all_goals(tuple(ci_goal.get("route_race_id_array") or ()))
        due = [g for g in goals_all
               if g[2] in (2, 3) and g[0] == turn_acted
               and _goal_is_cleared(g, ci_goal, history, goals_all)
               and not _goal_announced(full_state, g[1])]
        if due:
            entry = _goal_achieved_event(full_state, ci_goal, due[-1][1])
            if entry:
                _record_goal_announced(full_state, due[-1][1])
                _push_career_ctx(full_state, {
                    "event_id": entry["event_id"], "story_id": entry["story_id"],
                    "source": "inline", "source_id": 0, "title": "Goal Achieved",
                    "trainee_card_id": ci_goal.get("card_id"),
                    "event": {"choices": [{"effects": [
                        {"type": "all_stats", "value": f"+{_GOAL_CLEAR_STATS}"},
                        {"type": "skill_points", "value": f"+{_GOAL_CLEAR_SP}"}]}]}})
                shown_now = response["data"].get("unchecked_event_array") or []
                shown_id = shown_now[0].get("event_id") if shown_now else None
                is_scenario_shown = shown_id is not None and any(
                    shown_id in evs for evs in single_mode_events.SCENARIO_SCHEDULE.values())
                if not shown_now or is_scenario_shown:
                    _queue_turn_event(response, full_state, entry, front=True)
                else:
                    full_state[GOAL_EVENT_KEY] = entry
            elif response["data"].get("unchecked_event_array"):
                _mark_goal_progress(response, ci_goal, history, due[-1][1], full_state)
    except Exception:
        log.exception("passive-goal announcement failed; skipping")


def _command_disabled(home_info, payload: dict) -> bool:
    if not isinstance(home_info, dict):
        return False
    cid = int(payload.get("command_id") or 0) or int(payload.get("command_group_id") or 0)
    if not cid:
        return False
    rows = [c for c in home_info.get("command_info_array") or ()
            if isinstance(c, dict) and c.get("command_id") == cid]
    return bool(rows) and not any(int(c.get("is_enable", 1) or 0) for c in rows)


def handle_exec_command(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id)
    if full_state is None or STATE_KEY not in full_state:
        raise LookupError("No active career for this viewer -- call start first")

    career_state = full_state[STATE_KEY]

    if payload.get("event_id") in _REST_EVENT_IDS and payload.get("command_id") is None:
        return handle_ura_check_event(payload)

    if payload.get("event_id") == single_mode_events.DUEL_EVENT_ID:
        ctx = full_state.get(single_mode_events.DUEL_CTX_KEY) or {}
        options = ctx.get("stat_options") or single_mode_events.duel_stat_options(
            full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1))
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": single_mode_events.duel_preview(options)}}

    if (payload.get("event_id") in event_engine.CAREER_EVENT_IDS
            or _career_ctx_for(full_state, payload.get("event_id"))):
        ev, ctx = _career_event_from_ctx(full_state, payload.get("event_id"))
        if ev and ctx.get("event_id") == payload.get("event_id"):
            ci = career_state["data"].get("chara_info")
            dcid = event_engine.source_default_chara(ctx.get("source"), ctx.get("source_id"))
            partner, chances = _pal_unlock_preview_args(full_state, ctx.get("story_id"))
            return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, dcid, partner, chances,
                        source_card_id=_event_source_card(ctx))}}

    conflict = _client_turn_conflict(payload, full_state,
                                     career_state["data"].get("chara_info"),
                                     allow_played_turn=False)
    if conflict is not None:
        return _stale_turn_refusal(viewer_id, "single_mode/exec_command", conflict,
                                   career_state["data"].get("chara_info"))

    if _command_disabled(career_state["data"].get("home_info"), payload):
        log.warning("exec_command refused: command %s/%s is disabled this turn",
                    payload.get("command_id"), payload.get("command_group_id"))
        return refuse()


    resolved = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
    in_flight = set(full_state.get(single_mode_events.PENDING_EVENTS_KEY) or [])
    in_flight |= {e.get("event_id") for e in
                  (full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or [])}
    cur_turn = payload.get("current_turn")
    if not scenarios.for_chara(career_state["data"].get("chara_info")).uses_fixed_schedule:
        scheduled = []
    else:
        scheduled = [e for e in single_mode_events.SCENARIO_SCHEDULE.get(cur_turn, [])
                     if e not in resolved and e not in in_flight]
    unlocked = [list(x) for x in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
    unlocked_target_ids = [n[0] for n in unlocked]

    chara_before = career_state["data"].get("chara_info") or {}
    response = _simulate_exec_command(
        career_state, payload, unlocked_npcs=unlocked_target_ids,
        training_bonus=_training_bonus(full_state, chara_before),
        friendship_bonus=_friendship_bonus(full_state, chara_before),
        specialty_bonus=_specialty_bonus(full_state, chara_before),
        full_state=full_state)
    full_state[single_mode_events.UNLOCKED_NPCS_KEY] = unlocked

    if not (response.get("data") or {}).get("_training_failed"):
        scenarios.for_chara(chara_before).award_training_gains(
            full_state, chara_before, payload)
        if scenarios.for_chara(chara_before).award_claims_slot(full_state):
            data_now = response.get("data") or {}
            if not data_now.get("unchecked_event_array"):
                wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
                if wire is not None:
                    data_now["unchecked_event_array"] = [wire]
        _refresh_failure_after_gains(career_state, response, full_state)
    else:
        scenarios.for_chara(chara_before).on_training_failed(
            full_state, chara_before, payload)

    cmd = payload.get("command_id")
    if _COMMAND_TRAINED_STAT.get(cmd):
        full_state[event_engine.LAST_TRAINED_KEY] = _COMMAND_TRAINED_STAT[cmd]
    meek_fac = _happy_meek_facility(
        payload.get("current_turn"), full_state,
        career_state["data"].get("chara_info"))
    if not scheduled and cmd is not None and meek_fac is not None and cmd == meek_fac:
        vlevel = full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1)
        options = single_mode_events.duel_stat_options(vlevel)
        duel_ci = career_state["data"]["chara_info"]
        entry = single_mode_events.duel_event_entry(options, duel_ci, vlevel)
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=single_mode_events.DUEL_EVENT_ID, story_id=entry.get("story_id"),
            raw=entry, once_key=f"duel:{duel_ci.get('turn')}",
            priority=career_events.PRIO_SPECIAL, resolver="duel", source="duel",
            payload={"stat_options": options, "versus_level": vlevel}))

    rdata = response.get("data", {})
    training_failed = bool(rdata.get("_training_failed"))
    if rdata.pop("_training_failed", False):
        worst = rdata.pop("_training_worst", False)
        fail_ci = career_state["data"]["chara_info"]
        player_chara = (fail_ci.get("card_id") or 0) // 100
        eid = single_mode_events.WORST_FAIL_EVENT if worst else single_mode_events.NORMAL_FAIL_EVENT
        stat = _COMMAND_TRAINED_STAT.get(cmd)
        full_state[single_mode_events.FAIL_CTX_KEY] = {
            "event_id": eid, "worst": worst, "stat": stat}
        entry = single_mode_events.failure_event_entry(eid, player_chara, worst)
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=eid, story_id=entry.get("story_id"), raw=entry,
            once_key=f"training_failure:{fail_ci.get('turn')}:{eid}",
            priority=career_events.PRIO_SPECIAL, resolver="training_failure",
            source="training_failure", payload={"worst": worst, "stat": stat}))
    else:
        rdata.pop("_training_worst", None)

    if rdata.pop("_slacked", False):
        ci_slack = career_state["data"]["chara_info"]
        chara_slack = (ci_slack.get("card_id") or 0) // 100
        suffix = conditions.CONDITION_STORY_SUFFIX[conditions.SLACKER]
        story = 500000000 + chara_slack * 1000 + suffix
        if chara_slack and _story_exists(story):
            _queue_turn_event(response, full_state, _scenario_event_entry(
                _shared_beat_event_id(suffix, ci_slack.get("turn")), story,
                play_timing=6, choices=[_ack_choice()], chara_id=chara_slack),
                front=True)

    rest_outcome = rdata.pop("_rest_outcome", None)
    for story in rdata.pop("_trip_stories", None) or ():
        _queue_chapter(full_state, response, career_state["data"]["chara_info"], story)
    if rest_outcome:
        rest_ci = career_state["data"]["chara_info"]
        player_chara = (rest_ci.get("card_id") or 0) // 100
        full_state[REST_CTX_KEY] = rest_outcome
        rest_eid = rest_outcome["event_id"]
        entry = _rest_event_entry(rest_eid, player_chara)
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=rest_eid, story_id=entry.get("story_id"), raw=entry,
            once_key=f"rest:{rest_ci.get('turn')}:{rest_eid}",
            priority=career_events.PRIO_SPECIAL, resolver="rest", source="rest"))

    hint_reveals = rdata.pop("_hint_reveals", None) or []
    if hint_reveals:
        hr_turn = career_state["data"]["chara_info"].get("turn")
        for i, (hint_entry, hint_effects) in enumerate(hint_reveals):
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=hint_entry.get("event_id"),
                story_id=hint_entry.get("story_id"),
                raw=hint_entry, choices=[career_events.Choice(effects=hint_effects)],
                once_key=f"hint_reveal:{hr_turn}:{i}",
                priority=career_events.PRIO_SPECIAL, source="hint_reveal"))

    leveled_command = rdata.pop("_facility_leveled", False)
    if leveled_command:
        lvl_event, lvl_story = scenarios.for_chara(
            career_state["data"]["chara_info"]).facility_levelup_event(leveled_command)
        entry = _scenario_event_entry(lvl_event, lvl_story, play_timing=6,
                                      choices=[_ack_choice()])
        lvlup_turn = career_state["data"]["chara_info"].get("turn")
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=lvl_event, story_id=lvl_story, raw=entry,
            once_key=f"facility_levelup:{lvlup_turn}",
            priority=career_events.PRIO_SPECIAL, source="facility_levelup"))

    npc_partners = rdata.pop("_npc_partners", None) or []
    trained_cmd = rdata.pop("_trained_command", None)
    pal_partners = rdata.pop("_pal_partners", None) or []
    pal_command = rdata.pop("_pal_command", None)
    if rdata.pop("_infirmary", False):
        ci_inf = career_state["data"]["chara_info"]
        chara_inf = (ci_inf.get("card_id") or 0) // 100
        if chara_inf:
            story = 500000000 + chara_inf * 1000 + _INFIRMARY_STORY_SUFFIX
            entry = _scenario_event_entry(
                _INFIRMARY_EVENT_ID, story, play_timing=6,
                choices=[_ack_choice()], chara_id=chara_inf)
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=_INFIRMARY_EVENT_ID, story_id=story, raw=entry,
                priority=career_events.PRIO_SPECIAL, resolver="infirmary",
                source="infirmary"))
    tid = random.choice(npc_partners) if npc_partners else None
    if tid is not None and random.random() < _appraisal_chance(
            career_state["data"]["chara_info"].get("scenario_id"), tid):
        try:
            ci = career_state["data"]["chara_info"]
            bond = _bond_of(ci, tid)
            hit = single_mode_events.appraisal_for(
                tid, bond, scenarios.for_chara(ci).appraisal_events())
            if hit:
                a_event, amount = hit
                stat = (None if tid in scenarios.for_chara(ci).sp_appraisers
                        else _COMMAND_TRAINED_STAT.get(trained_cmd, "speed"))
                player_chara = (ci.get("card_id") or 0) // 100
                entry = single_mode_events.event_entry(a_event, player_chara)
                _emit_turn_event(response, full_state, career_events.Event(
                    event_id=a_event, story_id=entry.get("story_id"), raw=entry,
                    once_key=f"appraisal:{ci.get('turn')}:{a_event}",
                    priority=career_events.PRIO_SPECIAL, resolver="appraisal",
                    source="appraisal",
                    payload={"target_id": tid, "amount": amount, "stat": stat}))
        except Exception:
            log.exception("appraisal roll failed; skipping")

    scenario_pending = False
    try:
        scenario_pending = _scenario_interstitial_pending(full_state, career_state,
                                                          payload)
        if scenario_pending:
            held = payload.get("current_turn")
            if held is None:
                held = (career_state["data"]["chara_info"].get("turn") or 1) - 1
            scenarios.for_chara(career_state["data"]["chara_info"]).begin_hold(
                full_state, int(held or 0))
    except Exception:
        log.exception("scenario turn-hold failed; skipping")

    with _scenario_slot_reserved(full_state, scenario_pending):
        try:
            _maybe_fire_refresh_event(full_state, career_state, response)
        except Exception:
            log.exception("refresh event failed; skipping")
        try:
            _maybe_queue_inspiration(response, full_state, career_state, viewer_id,
                                     career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("inspiration roll failed; skipping this turn's event")

    career_failed = False
    try:
        career_failed = _maybe_fail_goal(
            response, full_state, career_state,
            career_state["data"]["chara_info"].get("turn"))
    except Exception:
        log.exception("goal evaluation failed; not failing the career")
    with _scenario_slot_reserved(full_state, scenario_pending):
        try:
            _maybe_queue_chara_story(response, full_state, career_state,
                                     career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("chara story queue failed; skipping")
        try:
            _maybe_queue_unique_level(response, full_state, career_state,
                                      career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("unique skill level event queue failed; skipping")
    try:
        _maybe_fire_director_fan(full_state, career_state, response)
    except Exception:
        log.exception("director's fan event failed; skipping")
    try:
        _maybe_fire_condition_event(full_state, career_state, response)
    except Exception:
        log.exception("condition event failed; skipping")

    try:
        ci_now = career_state["data"]["chara_info"]
        scenarios.for_chara(ci_now).persist_upkeep(full_state, ci_now)
        passion_ended = _pure_passion_effects(full_state, ci_now, ci_now.get("turn"))
        _sync_pal_evaluation(full_state, ci_now)
        for cond in passion_ended:
            end_card = pal_cards.pure_passion_card(cond)
            sid = pal_cards.pure_passion_end_story(end_card) if end_card else None
            if sid and _queue_pal_event(response, full_state, career_state,
                                        end_card, sid, "passion_end"):
                log.info("pure passion: condition %s expired, serving %s's "
                         "end story %s", cond, end_card, sid)
                break
        if not (response["data"].get("unchecked_event_array") or []):
            for pos, card_id in _deck_pal_cards(ci_now):
                entry = _pal_card_entry(full_state, card_id)
                if not entry.get("met") or entry.get("unlocked") \
                        or full_state.get(PAL_EVENT_CTX_KEY) \
                        or _pal_chain_ended(full_state, card_id):
                    continue
                pre = pal_cards.pre_unlock_story(card_id)
                if pre and entry.get("pre_unlock"):
                    sid = pal_cards.unlock_story(card_id)
                    if sid and _queue_pal_event(response, full_state, career_state,
                                                card_id, sid, "unlock"):
                        break
                    continue
                bond = _bond_of(ci_now, pos)
                chance = (_PAL_UNLOCK_CHANCE_HIGH if bond >= 60
                          else _PAL_UNLOCK_CHANCE_LOW)
                if random.random() < tuning.event(chance):
                    if pre:
                        entry["pre_unlock"] = 1
                        if _queue_pal_event(response, full_state, career_state,
                                            card_id, pre, "pre_unlock"):
                            break
                        continue
                    sid = pal_cards.unlock_story(card_id)
                    if sid and _queue_pal_event(response, full_state, career_state,
                                                card_id, sid, "unlock"):
                        break
        response["data"]["chara_info"] = copy.deepcopy(ci_now)
    except Exception:
        log.exception("pal/group upkeep failed; skipping")

    try:
        if _maybe_fire_secret_event(full_state, career_state, response):
            pass
        elif payload.get("command_group_id") == _OUTING_COMMAND_GROUP:
            sel = payload.get("select_id")
            ci_out = career_state["data"]["chara_info"]
            scenario_beat = scenarios.for_chara(ci_out).partner_outing(
                full_state, ci_out, sel, payload.get("current_turn"))
            card_id = _card_for_partner(full_state, ci_out, sel)
            if (card_id is None or not _queue_outing_event(
                    response, full_state, career_state, card_id, sel,
                    payload.get("current_turn"))) and not scenario_beat:
                _maybe_fire_support_event(full_state, career_state, response)
        elif payload.get("command_group_id") in _RECREATION_GROUPS or payload.get("command_type") == 3:
            rec_turn = payload.get("current_turn")
            if (random.random() < _CRANE_CHANCE and not _is_camp_turn(rec_turn)
                    and (rec_turn or 0) >= _CRANE_FIRST_TURN
                    and _crane_available(full_state)):
                _queue_crane_game(response, full_state, career_state)
            else:
                fired = _maybe_fire_outing_event(full_state, career_state, response)
                if not fired:
                    _maybe_fire_support_event(full_state, career_state, response)
        elif cmd is not None and cmd != _RACE_COMMAND_ID:
            trained_stat = _COMMAND_TRAINED_STAT.get(cmd)
            if _maybe_fire_shared_beat(full_state, career_state, response,
                                       _ACUPUNCTURE_SUFFIX, _ACUPUNCTURE_CHANCE):
                pass
            elif (trained_stat and not training_failed
                  and not _is_camp_turn(payload.get("current_turn"))
                  and (payload.get("current_turn") or 0) <= _EXTRA_TRAINING_LAST_TURN
                  and _maybe_fire_shared_beat(
                      full_state, career_state, response, _EXTRA_TRAINING_SUFFIX,
                      _EXTRA_TRAINING_CHANCE, {"stat": trained_stat},
                      once_per_career=False)):
                pass
            elif not _maybe_fire_pal_training_event(full_state, career_state, response,
                                                    pal_partners, pal_command):
                if not _maybe_fire_chara_random_event(full_state, career_state, response):
                    _maybe_fire_support_event(full_state, career_state, response)
    except Exception:
        log.exception("dynamic event firing failed (skipped)")

    try:
        _maybe_queue_pal_finale(response, full_state, career_state)
    except Exception:
        log.exception("pal/group finale queue failed; skipping")

    if scheduled:
        pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
        pending += [e for e in scheduled if e not in pending]
        player_chara = (career_state["data"]["chara_info"].get("card_id") or 0) // 100
        shown = response["data"].get("unchecked_event_array")
        unlock = next((e for e in pending if single_mode_events.npcs_unlocked_by(e)), None)
        if unlock is not None:
            lead = pending.pop(0)
            if shown:
                full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, [])
                full_state[single_mode_events.EXTRA_EVENTS_KEY][:0] = shown
            response["data"]["unchecked_event_array"] = [
                single_mode_events.event_entry(lead, player_chara)]
        elif not shown:
            response["data"]["unchecked_event_array"] = [
                single_mode_events.event_entry(pending[0], player_chara)]
        full_state[single_mode_events.PENDING_EVENTS_KEY] = pending

    dead_ctx = full_state.get(RACE_CTX_KEY)
    if dead_ctx:
        now_turn = career_state["data"]["chara_info"].get("turn")
        if (dead_ctx.get("turn") is not None and now_turn is not None
                and dead_ctx["turn"] < now_turn):
            full_state.pop(RACE_CTX_KEY, None)

    _announce_due_passive_goals(response, full_state, career_state,
                                payload.get("current_turn"))

    try:
        _poll_career_events(response, full_state, career_state, payload,
                            endpoint="exec_command")
    except Exception:
        log.exception("career event poll failed; skipping")
    _claim_inspiration_slot(response, full_state, career_state["data"]["chara_info"])

    if not response["data"].get("unchecked_event_array"):
        backlog = list(full_state.get(single_mode_events.EXTRA_EVENTS_KEY, []))
        if backlog:
            response["data"]["unchecked_event_array"] = [backlog.pop(0)]
            full_state[single_mode_events.EXTRA_EVENTS_KEY] = backlog

    _remember_display(full_state, response)
    from . import campaign_walking
    campaign_walking.add_gauge(full_state, "singlemode")
    state_store.save_state(viewer_id, full_state)
    return response


_PREVIEW_TARGET_TO_STAT = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}


def _preview_gains_for(career_state: dict, command_id) -> list | None:
    hi = career_state.get("data", {}).get("home_info", {})
    for c in hi.get("command_info_array", []) or []:
        if c.get("command_id") == command_id:
            return c.get("params_inc_dec_info_array") or []
    return None


def _merge_preview_params(base: list, extra: list) -> list:
    merged = [dict(p) for p in base]
    by_type = {p.get("target_type"): p for p in merged}
    for p in extra or ():
        tt = p.get("target_type")
        if tt in by_type:
            by_type[tt]["value"] = by_type[tt].get("value", 0) + p.get("value", 0)
        else:
            entry = dict(p)
            merged.append(entry)
            by_type[tt] = entry
    return merged


def _note_capped_facility_stats(chara_info: dict, command_id, not_up: list) -> None:
    if not_up is None:
        return
    try:
        stats = training_formula.facility_stats(
            chara_info.get("scenario_id") or 1, int(command_id or 0))
    except Exception:
        log.exception("facility stat profile lookup failed; "
                      "skipping the capped-stat notices")
        return
    for stat in stats:
        code = _STAT_TO_TARGET_TYPE.get(stat)
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}")
        if not code or code in not_up or not cap:
            continue
        if (chara_info.get(stat) or 0) >= cap:
            not_up.append(code)
            log.info("training reward: already at ceiling -> "
                     "not_up_parameter_info %s (stat %s, facility %s)",
                     code, stat, command_id)


def _apply_preview_gains(chara_info: dict, preview: list | None, payload: dict,
                         not_up: list | None = None) -> bool:
    if not preview:
        return False
    vital = payload.get("current_vital", chara_info.get("vital", 100))
    for p in preview:
        tt, val = p.get("target_type"), p.get("value", 0)
        stat = _PREVIEW_TARGET_TO_STAT.get(tt)
        if stat is not None:
            if conditions.blocks_stat(chara_info, stat):
                continue
            cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
            if not_up is not None and val > 0 and chara_info.get(stat, 0) >= cap:
                not_up.append(tt)
                log.info("training reward: already at ceiling -> "
                         "not_up_parameter_info %s (stat %s)", tt, stat)
            chara_info[stat] = max(0, min(cap, chara_info.get(stat, 0) + val))
        elif tt == 30:
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + val
            tuning.note_training_sp(val)
        elif tt == 10:
            vital = vital + val
    before = payload.get("current_vital", chara_info.get("vital", 100))
    chara_info["vital"] = max(0, min(max(chara_info.get("max_vital", 100), before), vital))
    return True


TRAINING_COMMAND_IDS = [101, 105, 102, 103, 106]
_COMMAND_TO_TYPE = {101: 0, 105: 1, 102: 2, 103: 3, 106: 4}
_STAT_TO_TARGET_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

_BOND_GAIN_PER_TRAINING = 7
_BOND_MAX = 100
_NPC_TARGET_IDS = {102, 103, 2001}
_NPC_BOND_GAIN = 2
APPRAISAL_CTX_KEY = "appraisal_ctx"
_APPRAISAL_CHANCE = 0.25
_APPRAISAL_CHANCE_BY_NPC = {102: 34 / 46, 103: 46 / 56, 111: 1.0, 101: 1.0, 114: 1.0}
_APPRAISAL_CHANCE_BY_SCENARIO_NPC = {(12, 102): 1.0, (14, 102): 1.0}


def _appraisal_chance(scenario_id, target_id) -> float:
    return _APPRAISAL_CHANCE_BY_SCENARIO_NPC.get(
        (scenario_id, target_id),
        _APPRAISAL_CHANCE_BY_NPC.get(target_id, _APPRAISAL_CHANCE))


@career_events.resolver("appraisal")
def _resolve_appraisal(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    amount = payload.get("amount") or 0
    stat = payload.get("stat")
    not_up = []
    if stat:
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        if amount > 0 and chara_info.get(stat, 0) >= cap:
            not_up.append(event_engine._STAT_KEY_IDX.get(stat))
        chara_info[stat] = min(chara_info.get(stat, 0) + amount, cap)
    else:
        chara_info["skill_point"] = chara_info.get("skill_point", 0) + amount
    tid = payload.get("target_id")
    if tid:
        _ensure_npc_eval(chara_info, tid)
        _set_bond(chara_info, tid, min(_BOND_MAX, _bond_of(chara_info, tid)
                                       + single_mode_events.appraisal_bond_gain(tid)))
    not_up = [c for c in not_up if c]
    return {"not_up": not_up} if not_up else {}

_GATED_NPC_CHARA_IDS = frozenset(
    chara_id for unlocks in single_mode_events.NPC_UNLOCKS.values()
    for _, chara_id in unlocks)


def _deck_position_for_chara(chara_info: dict, chara_id: int):
    for c in chara_info.get("support_card_array", []) or []:
        if master_data.support_card_chara(c.get("support_card_id") or 0) == chara_id:
            return c.get("position")
    return None


def _hide_ungated_support_card_rows(chara_info: dict) -> None:
    gated = scenarios.for_chara(chara_info).gated_npc_charas()
    for c in chara_info.get("support_card_array", []) or []:
        if master_data.support_card_chara(c.get("support_card_id") or 0) not in gated:
            continue
        for row in chara_info.get("evaluation_info_array") or []:
            if row.get("target_id") == c.get("position"):
                row["is_appear"] = 0


def _register_npc_unlocks(full_state: dict, updated: dict, event_id) -> None:
    for npc in single_mode_events.npcs_unlocked_by(event_id):
        deck_position = _deck_position_for_chara(updated, npc[1])
        if deck_position is not None:
            for row in updated.get("evaluation_info_array") or []:
                if row.get("target_id") == deck_position:
                    row["is_appear"] = 1
            continue
        unlocked_now = [list(x) for x in
                        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
        newly_unlocked = list(npc) not in unlocked_now
        if newly_unlocked:
            unlocked_now.append(list(npc))
            full_state[single_mode_events.UNLOCKED_NPCS_KEY] = unlocked_now
        _ensure_npc_eval(updated, npc[0])
        if not newly_unlocked:
            continue
        for row in updated.get("evaluation_info_array") or []:
            if row.get("target_id") == npc[0]:
                row["is_appear"] = 1


def _ensure_npc_eval(chara_info: dict, target_id: int) -> None:
    arr = chara_info.setdefault("evaluation_info_array", [])
    if not any(e.get("target_id") == target_id for e in arr):
        arr.append({"target_id": target_id, "training_partner_id": target_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 0, "group_outing_info_array": []})
_AWAY_WEIGHT = 50
_BASE_FACILITY_WEIGHT = 100

_HINT_BASE_CHANCE = 0.07


def _card_id_at_position(chara_info: dict, position) -> int | None:
    for c in chara_info.get("support_card_array", []) or []:
        if c.get("position") == position:
            return c.get("support_card_id")
    return None


def _skill_hint_chance(support_card_id) -> float:
    card = training_formula.CARD_BY_ID.get(str(support_card_id))
    if not card or card.get("type") not in training_formula.CARD_TYPE_NAMES:
        return 0.0
    if not event_engine.card_chara_id(support_card_id):
        return 0.0
    hint_pct = training_formula.card_effect(support_card_id, "hint_frequency", 0) or 0
    hint_pct += training_formula.unique_effect(support_card_id, training_formula.EFFECT_HINT_FREQ)
    chance = _HINT_BASE_CHANCE * (1 + hint_pct / 100)
    return tuning.hint(chance)


def _hinting_partners(chara_info: dict, turn, partners) -> list:
    card_id = int(chara_info.get("card_id") or 0)
    hinting = []
    for pos in partners:
        sid = _card_id_at_position(chara_info, pos)
        if sid is None:
            continue
        chance = _skill_hint_chance(sid)
        if chance <= 0:
            continue
        seed = (card_id << 20) ^ (int(turn or 0) << 8) ^ int(pos)
        if random.Random(seed).random() < chance:
            hinting.append(pos)
    return hinting


HINT_REVEAL_CTX_KEY = "active_hint_reveal"
_HINT_LEVEL_WEIGHTS = ([1], [100])
_MAX_HINT_LEVEL = 5
_HINT_STAT_VARIANT_CHANCE = 0.25
_HINT_STAT_VARIANT = {
    "speed": [("speed", 6), ("power", 2)],
    "stamina": [("stamina", 6), ("guts", 2)],
    "power": [("power", 6), ("stamina", 2)],
    "guts": [("guts", 6), ("speed", 1), ("power", 1)],
    "wiz": [("wiz", 6), ("speed", 2)],
}
_HINT_BOND_GAIN = 5


_GENERIC_HINT_EVENT = 20099
_GENERIC_HINT_STORY = 801000003
_FALLBACK_HINT_STORY = 801001003


@functools.lru_cache(maxsize=1)
def _generic_hint_story() -> bool:
    return _story_exists(_GENERIC_HINT_STORY)


def _build_hint_reveals(chara_info: dict, career_state: dict, command_id, turn) -> list:
    partners = _partners_in_command(career_state, command_id)
    hinting_positions = _hinting_partners(chara_info, turn, partners)
    if not hinting_positions:
        return []
    hinting_positions = [random.choice(list(hinting_positions))]
    learned = {s.get("skill_id") for s in chara_info.get("skill_array", []) or []}
    reveals = []
    for pos in hinting_positions:
        sid = _card_id_at_position(chara_info, pos)
        if sid is None:
            continue
        chara = event_engine.card_chara_id(sid)
        if not chara:
            continue
        groups = event_engine.card_hint_groups(sid)
        candidates = [g for g in groups if g[0] == "skill"
                      and event_engine.card_hint_open(chara_info, g[1], learned)]
        pending_levels = {}
        card_type = training_formula.card_type_index(sid)
        trained_stat = (training_formula.STAT_NAMES[card_type]
                        if card_type is not None else None)
        stat_groups = [g for g in groups if g[0] == "stat"]
        draws = 1 + int(training_formula.bond_gated_effect(
            sid, training_formula.EFFECT_HINT_COUNT, _bond_of(chara_info, pos)))
        effects = []
        for _ in range(draws):
            pool = candidates + stat_groups
            pick = random.choice(pool) if pool else None
            if pick is not None and pick[0] == "stat":
                effects += [dict(e) for e in pick[1]]
            elif pick is None and trained_stat and not effects:
                effects = [{"type": stat, "value": amount}
                           for stat, amount in _HINT_STAT_VARIANT.get(
                               trained_stat, [(trained_stat, 6)])]
            elif pick is not None:
                skill_id = pick[1]
                level = random.choices(_HINT_LEVEL_WEIGHTS[0], weights=_HINT_LEVEL_WEIGHTS[1], k=1)[0]
                level += int(round(training_formula.master_effect(
                    sid, training_formula.EFFECT_HINT_LEVEL)))
                current = (event_engine.skill_hint_level(chara_info, skill_id)
                           + pending_levels.get(skill_id, 0))
                level = max(0, min(level, _MAX_HINT_LEVEL - current))
                pending_levels[skill_id] = pending_levels.get(skill_id, 0) + level
                if current + level >= _MAX_HINT_LEVEL:
                    candidates.remove(pick)
                if level:
                    effects.append({"type": "skill_hint", "skill_id": skill_id, "value": level})
        if not effects:
            continue
        effects = effects + [{"type": "bond", "value": f"+{_HINT_BOND_GAIN}",
                              "char_id": chara}]
        if _generic_hint_story():
            entry = event_engine.career_event_entry(
                {"choices": [{"effects": effects}]}, _GENERIC_HINT_EVENT,
                _GENERIC_HINT_STORY, chara_id=0, support_card_id=0, play_timing=6)
            entry["event_contents_info"]["tips_training_partner_id"] = pos
            reveals.append((entry, effects))
            continue
        story_id = 800000000 + chara * 1000 + 3
        if not _story_exists(story_id):
            story_id = _FALLBACK_HINT_STORY
        entry = event_engine.career_event_entry(
            {"choices": [{"effects": effects}]},
            event_engine.support_random_event_id(story_id), story_id,
            chara_id=0, support_card_id=sid, play_timing=6)
        reveals.append((entry, effects))
    return reveals


def _support_card_levels(full_state: dict) -> dict:
    return {
        c["support_card_id"]: training_formula.card_level(
            c["support_card_id"], c.get("exp") or 0, c.get("limit_break_count"))
        for c in full_state.get(collection.SUPPORT_CARD_KEY) or []
        if isinstance(c, dict) and c.get("support_card_id")
    }


def _friendship_stacks(full_state: dict) -> dict:
    raw = (full_state or {}).get(FRIENDSHIP_STACK_KEY) or {}
    return {int(k): v for k, v in raw.items()}


def _bond_of(chara_info: dict, position) -> int:
    for e in chara_info.get("evaluation_info_array", []) or []:
        if e.get("target_id") == position:
            return e.get("evaluation", 0) or 0
    return 0


def _set_bond(chara_info: dict, position, value: int) -> None:
    for e in chara_info.get("evaluation_info_array", []) or []:
        if e.get("target_id") == position:
            e["evaluation"] = value
            return


def _init_bonds(chara_info: dict) -> None:
    positions = set()
    levels = _deck_support_card_levels(chara_info)
    deck_wide = sum(amount for c in chara_info.get("support_card_array", [])
                    for etype, amount, *_ in training_formula.unique_params(
                        c.get("support_card_id"), training_formula.EFFECT_DECK_INITIAL_BOND,
                        levels.get(c.get("support_card_id")) or 0)
                    if etype == training_formula.EFFECT_INITIAL_BOND)
    for c in chara_info.get("support_card_array", []):
        sid = c.get("support_card_id")
        init = training_formula.card_effect(sid, "initial_friendship_gauge", 0) or 0
        if levels.get(sid):
            init += training_formula.unique_effect(
                sid, training_formula.EFFECT_INITIAL_BOND, level=levels[sid])
        _set_bond(chara_info, c.get("position"), min(_BOND_MAX, int(init + deck_wide)))
        positions.add(c.get("position"))
    for row in chara_info.get("evaluation_info_array") or []:
        if row.get("target_id") in positions:
            row["is_appear"] = 1
    for tid in sorted(_NPC_TARGET_IDS):
        _ensure_npc_eval(chara_info, tid)
    _hide_ungated_support_card_rows(chara_info)


def _apply_initial_stats(chara_info: dict) -> dict:
    totals: dict[str, int] = {}
    for c in chara_info.get("support_card_array", []):
        bonuses = training_formula.initial_stat_bonuses(
            c.get("support_card_id"), c.get("limit_break_count", 0))
        for stat, val in bonuses.items():
            totals[stat] = totals.get(stat, 0) + val
    deck_ids = [c.get("support_card_id") for c in chara_info.get("support_card_array", [])
               if c.get("support_card_id")]
    deck_levels = {c.get("support_card_id"): training_formula.card_level(
                      c.get("support_card_id"), c.get("exp", 0), c.get("limit_break_count"))
                   for c in chara_info.get("support_card_array", []) if c.get("support_card_id")}
    for stat, val in training_formula.deck_composition_initial_stat_bonus(
            deck_ids, deck_levels).items():
        totals[stat] = totals.get(stat, 0) + val
    for sid in deck_ids:
        if not deck_levels.get(sid):
            continue
        for i, stat in enumerate(training_formula.STAT_NAMES):
            val = int(training_formula.unique_effect(
                sid, training_formula.EFFECT_INITIAL_STAT_BASE + i, level=deck_levels[sid]))
            if val:
                totals[stat] = totals.get(stat, 0) + val
    sp = 0
    for c in chara_info.get("support_card_array", []):
        sid = c.get("support_card_id")
        if not sid:
            continue
        sp += training_formula.master_effect(
            sid, training_formula.EFFECT_INITIAL_SKILL_POINT,
            level=training_formula.level_cap_for(sid, c.get("limit_break_count", 0)))
        if deck_levels.get(sid):
            sp += training_formula.unique_effect(
                sid, training_formula.EFFECT_INITIAL_SKILL_POINT, level=deck_levels[sid])
    if sp:
        chara_info["skill_point"] = int(chara_info.get("skill_point") or 0) + int(sp)
    for stat, val in totals.items():
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        chara_info[stat] = min(chara_info.get(stat, 0) + val, cap)
    return totals


_CAREER_BASE_CAP_BONUS = 200

_INHERIT_BLUE = {1: 5, 2: 12, 3: 21}

_CAP_INCREASE_BY_STAR = {1: 4, 2: 9, 3: 16}


def _pink_steps(total_stars: int) -> int:
    if total_stars >= 10:
        return 4
    if total_stars >= 7:
        return 3
    if total_stars >= 4:
        return 2
    if total_stars >= 1:
        return 1
    return 0

_RED_GROUP_TO_APT = {
    11: "proper_ground_turf", 12: "proper_ground_dirt",
    21: "proper_running_style_nige", 22: "proper_running_style_senko",
    23: "proper_running_style_sashi", 24: "proper_running_style_oikomi",
    31: "proper_distance_short", 32: "proper_distance_mile",
    33: "proper_distance_middle", 34: "proper_distance_long",
}
_APT_INHERIT_CAP = 7
_APT_MIDRUN_CAP = 8


def _red_rank_up(star: int) -> int:
    return 2 if star >= 3 else 1


def _raise_aptitude(chara_info: dict, field: str, ranks: int,
                    cap: int = _APT_INHERIT_CAP) -> None:
    cur = chara_info.get(field, 1) or 1
    if cur < cap:
        chara_info[field] = min(cap, cur + ranks)
_BLUE_GROUP_TO_STAT = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
_CAP_TARGET_TO_STAT = {61: "speed", 62: "stamina", 63: "power", 64: "guts", 65: "wiz"}


def _spark_cap_bonuses(factor_group_id, star: int) -> dict:
    out: dict[str, int] = {}
    rows = master_data.query(
        "SELECT target_type, effect_id, value_1 FROM succession_factor_effect "
        "WHERE factor_group_id=? AND target_type IN (61,62,63,64,65)", (factor_group_id,))
    by_tt: dict = {}
    for r in rows:
        by_tt.setdefault(r["target_type"], []).append(r)
    for tt, rs in by_tt.items():
        stat = _CAP_TARGET_TO_STAT.get(tt)
        if not stat:
            continue
        rs.sort(key=lambda r: r["effect_id"])
        pick = None
        for r in rs:
            if r["effect_id"] <= star:
                pick = r
        pick = pick or rs[0]
        if pick["value_1"]:
            out[stat] = out.get(stat, 0) + pick["value_1"]
    return out


def _roster_by_trained_id(viewer_id) -> dict:
    full = state_store.get_state(viewer_id) or {}
    out: dict = {}
    sources = []
    from .load import get_or_seed_blob
    li = get_or_seed_blob(full, viewer_id)
    if isinstance(li, dict):
        tc = (li.get("data") or {}).get("trained_chara")
        if isinstance(tc, list):
            sources.append(tc)
    for key in ("legacy_roster_pre_house", "legacy_roster"):
        if isinstance(full.get(key), list):
            sources.append(full[key])
    for lst in sources:
        for u in lst:
            if isinstance(u, dict):
                tid = u.get("trained_chara_id")
                if tid is not None and tid not in out:
                    out[tid] = u
    if region.CURRENT_REGION.get() == "jp":
        out.update({u["trained_chara_id"]: u for u in full.get("legacy_roster") or []})
        if full.get(STATE_KEY):
            out.update({u["trained_chara_id"]: u for u in full.get("career_parent_snapshot") or []})
    return out


def _apply_inheritance(chara_info: dict, viewer_id, start_chara: dict) -> dict:
    roster = _roster_by_trained_id(viewer_id)
    blue: dict[str, int] = {}
    caps: dict[str, int] = {}
    hints: dict[tuple[int, int], int] = {}
    red_stars: dict[str, int] = {}
    parents = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        pid = start_chara.get(key)
        parent = roster.get(pid) if pid else None
        if parent:
            parents.append(parent)

    members = []
    for i, parent in enumerate(parents):
        parent_chara = (parent.get("card_id") or 0) // 100
        own_skill_ids = set()
        if parent_chara:
            offset = (parent_chara - 1000) * 10 + 1
            own_skill_ids = {900000 + offset, 100000 + offset}
        members.append((parent.get("factor_info_array") or [], True, own_skill_ids))
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        gps = [by_pos[p] for p in (10, 20) if p in by_pos]
        for a in gps:
            members.append((a.get("factor_info_array") or [], False, set()))

    for factor_list, is_parent, own_skill_ids in members:
        for f in factor_list:
            sf = master_data.query_one(
                "SELECT factor_type, factor_group_id, rarity FROM succession_factor WHERE factor_id=?",
                (f.get("factor_id"),))
            if not sf:
                continue
            star = sf["rarity"]
            if sf["factor_type"] == 3:
                if not is_parent:
                    continue
                eff = master_data.query_one(
                    "SELECT value_1 FROM succession_factor_effect "
                    "WHERE factor_group_id=? AND target_type=41 ORDER BY effect_id LIMIT 1",
                    (sf["factor_group_id"],))
                if not eff or eff["value_1"] not in own_skill_ids:
                    continue
                key = master_data.skill_tip_key(eff["value_1"])
                hints[key] = max(hints.get(key, 0), min(3, star))
            if sf["factor_type"] == 1:
                stat = _BLUE_GROUP_TO_STAT.get(sf["factor_group_id"])
                if stat:
                    blue[stat] = blue.get(stat, 0) + _INHERIT_BLUE.get(star, 0)
                    caps[stat] = caps.get(stat, 0) + _CAP_INCREASE_BY_STAR.get(star, 0)
            elif sf["factor_type"] == 2:
                field = _RED_GROUP_TO_APT.get(sf["factor_group_id"])
                if field:
                    red_stars[field] = red_stars.get(field, 0) + star

    for stat, cbonus in caps.items():
        key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[key] = chara_info.get(key, 1200) + cbonus
    for stat, val in blue.items():
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        chara_info[stat] = min(chara_info.get(stat, 0) + val, cap)
    red = {field: _pink_steps(total) for field, total in red_stars.items()}
    for field, ranks in red.items():
        if ranks:
            _raise_aptitude(chara_info, field, ranks)
    tips = chara_info.setdefault("skill_tips_array", [])
    for (gid, rarity), lvl in hints.items():
        existing = next((t for t in tips if t.get("group_id") == gid
                         and t.get("rarity") == rarity), None)
        if existing:
            existing["level"] = min(5, max(existing.get("level", 0), lvl))
        else:
            tips.append({"group_id": gid, "rarity": rarity, "level": min(5, lvl)})
    return {"blue": blue, "caps": caps, "hints": hints, "red": red}


_INSPIRATION_EVENTS = {31: (7044, 7040, 7045), 55: (7046, 7041, 7047)}
_INSPIRATION_MAIN_IDS = (7040, 7041)
_INSPIRATION_STORY = 400000040
INSPIRATION_CTX_KEY = "active_inspiration"
INSPIRATION_FIRED_KEY = "inspiration_turns_fired"


@career_events.resolver("inspiration_main")
def _resolve_inspiration_main(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    lotteries = payload.get("lotteries")
    if lotteries:
        pick = int(payload.get("selected") or 1)
        payload = lotteries[pick - 1 if 0 < pick <= len(lotteries) else 0]
    _apply_inspiration(chara_info, payload, full_state)
    return {"factors": payload.get("factors") or []}


def _apply_inspiration(chara_info: dict, payload: dict, full_state=None) -> None:
    for stat, cbonus in (payload.get("caps") or {}).items():
        key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[key] = chara_info.get(key, 1200) + cbonus
    event_engine.apply_choice(chara_info, {"choices": [{"effects": payload.get("effects") or []}]}, 1,
                              full_state=full_state)
    for field, ranks in (payload.get("aptitudes") or {}).items():
        _raise_aptitude(chara_info, field, ranks, _APT_MIDRUN_CAP)


_GAIN_INFO_FIELDS = (
    "speed", "stamina", "power", "guts", "wiz",
    "max_speed", "max_stamina", "max_power", "max_guts", "max_wiz",
    "proper_distance_short", "proper_distance_mile", "proper_distance_middle",
    "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi",
    "proper_ground_turf", "proper_ground_dirt", "skill_point")


@functools.lru_cache(maxsize=1)
def _succession_lottery() -> bool:
    from . import load as _load
    try:
        with open(_load.SEED_PATH, encoding="utf-8") as fh:
            seed = json.load(fh)
    except (OSError, ValueError):
        return False
    while isinstance(seed, dict) and "common_define" not in seed and isinstance(seed.get("data"), dict):
        seed = seed["data"]
    common = (seed or {}).get("common_define") or {}
    return bool(common.get("select_succession_open_date"))


def _succession_gain_info(lottery_id: int, chara_info: dict, payload: dict) -> dict:
    after = copy.deepcopy(chara_info)
    _apply_inspiration(after, payload)
    info = {"lottery_id": lottery_id}
    info.update({k: after.get(k, 0) for k in _GAIN_INFO_FIELDS})
    info["skill_tips_array"] = [dict(t, effected_trainer_skill_id_array=t.get("effected_trainer_skill_id_array") or [])
                                for t in after.get("skill_tips_array") or []]
    info["effected_factor_array"] = copy.deepcopy(payload.get("factors") or [])
    return info


def _jp_factor_shape(factors: list) -> list:
    return [dict(pos, factor_info_array=[
                dict(f, effected_trainer_skill_id_array=f.get("effected_trainer_skill_id_array") or [],
                     before_factor_id=f.get("before_factor_id") or 0)
                for f in pos.get("factor_info_array") or []])
            for pos in factors]


def handle_select_succession(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    pick = int(payload.get("selected_lottery_id") or 1)
    for row in career_events.queued_rows(full_state):
        if row.get("resolver") == "inspiration_main" and (row.get("payload") or {}).get("lotteries"):
            row["payload"]["selected"] = pick
            state_store.save_state(viewer_id, full_state)
            break
    else:
        log.warning("select_succession %s: no inspiration waiting", pick)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


_INSPIRE_BASE_RATE = {
    1: (0.70, 0.80, 0.90),
    2: (0.01, 0.03, 0.05),
    3: (0.05, 0.10, 0.15),
    4: (0.03, 0.06, 0.09),
    5: (0.01, 0.02, 0.03),
    6: (0.03, 0.06, 0.09),
}

_STEP_WEIGHTS = {
    11: (30, 25, 20, 12, 6, 3, 2, 1, 0.5, 0.5),
    12: (15, 18, 20, 17, 12, 8, 5, 3, 1, 1),
    13: (5, 8, 10, 12, 13, 13, 12, 11, 9, 7),
    21: (90, 10), 22: (70, 30), 23: (50, 50),
    31: (60, 25, 10, 5),
    32: (30, 35, 25, 10),
    33: (10, 25, 35, 30),
    41: (50, 30, 12, 5, 3),
    42: (30, 30, 20, 12, 8),
    43: (15, 25, 25, 20, 15),
    51: (70, 25, 5), 52: (40, 40, 20), 53: (20, 40, 40),
}

_STAT_TARGETS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
_SKILL_POINT_TARGET = 6
_HINT_TARGET = 41

_GOLD_MIN_NON_BLUE = 3


@functools.lru_cache(maxsize=4096)
def _relation_points(*charas: int) -> int:
    if not charas or not all(charas):
        return 0
    cond = " AND ".join(
        "EXISTS (SELECT 1 FROM succession_relation_member m "
        "WHERE m.relation_type=r.relation_type AND m.chara_id=?)" for _ in charas)
    row = master_data.query_one(
        f"SELECT COALESCE(SUM(r.relation_point), 0) AS pts FROM succession_relation r WHERE {cond}",
        tuple(charas))
    return row["pts"] if row else 0


def _pair_compat_points(chara_a: int, chara_b: int) -> int:
    return _relation_points(chara_a, chara_b)


def _shared_g1_bonus(a: dict, b: dict) -> int:
    from . import trained_chara
    g1 = {sid for sid, _c, t, _r in trained_chara._saddle_rows()
          if t == trained_chara._SADDLE_TYPE_G1}
    shared = set(a.get("win_saddle_id_array") or ()) & set(b.get("win_saddle_id_array") or ())
    return 3 * len(shared & g1)


def _chara_of(entry: dict) -> int:
    return (entry.get("card_id") or 0) // 100


def _lineage_members(trainee_chara: int, parents: list) -> list:
    charas = [_chara_of(p) for p in parents]
    both = len(parents) == 2
    shared = (_relation_points(charas[0], charas[1]) + _shared_g1_bonus(parents[0], parents[1])
              if both else 0)
    parent_rows, gp_rows = [], []
    for i, parent in enumerate(parents):
        pos = 10 * (i + 1)
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or ()
                  if isinstance(a, dict)}
        score = _relation_points(trainee_chara, charas[i]) + shared
        for offset, src in ((1, 10), (2, 20)):
            gp = by_pos.get(src)
            if not gp:
                continue
            link = (_relation_points(trainee_chara, charas[i], _chara_of(gp))
                    + _shared_g1_bonus(parent, gp))
            score += link
            gp_rows.append((pos + offset, gp.get("factor_info_array") or [], link))
        parent_rows.append((pos, parent.get("factor_info_array") or [], score))
    return parent_rows + gp_rows


def _factor_steps(factor_group_id: int) -> dict:
    steps: dict = {}
    for r in master_data.query(
            "SELECT effect_id, target_type, value_1, value_2 FROM succession_factor_effect "
            "WHERE factor_group_id=? ORDER BY effect_id, id", (factor_group_id,)):
        steps.setdefault(r["effect_id"], []).append(dict(r))
    return steps


def _draw_step(steps: dict, effect_group_id: int):
    ids = sorted(steps)
    if not ids:
        return None
    weights = list(_STEP_WEIGHTS.get(effect_group_id) or ())[:len(ids)]
    weights += [weights[-1] if weights else 1] * (len(ids) - len(weights))
    return random.choices(ids, weights=weights)[0]


INSPIRATION_COMPAT_HOOKS: list = []
SENIOR_UNIQUE_HOOKS: list = []
SENIOR_INSPIRATION_TURN = 55


def _roll_inspiration(chara_info: dict, viewer_id, start_chara: dict,
                      turn: int | None = None) -> dict | None:
    roster = _roster_by_trained_id(viewer_id)
    parents = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        pid = (start_chara or {}).get(key)
        if pid and roster.get(pid):
            parents.append(roster[pid])
    if not parents:
        return None
    compat_bonus = sum(h(viewer_id) for h in INSPIRATION_COMPAT_HOOKS)
    forced_uniques = (sum(h(viewer_id) for h in SENIOR_UNIQUE_HOOKS)
                      if turn == SENIOR_INSPIRATION_TURN else 0)

    acc = _InspirationTotals(chara_info)
    unproced_uniques: list = []
    for pos, factor_list, score in _lineage_members((chara_info.get("card_id") or 0) // 100,
                                                    parents):
        for f in factor_list:
            sf = master_data.query_one(
                "SELECT factor_type, factor_group_id, rarity, effect_group_id "
                "FROM succession_factor WHERE factor_id=?", (f.get("factor_id"),))
            if not sf or sf["factor_type"] not in _INSPIRE_BASE_RATE:
                continue
            star = max(1, min(3, sf["rarity"]))
            chance = (_INSPIRE_BASE_RATE[sf["factor_type"]][star - 1]
                      * (1 + (score + compat_bonus) / 100))
            if random.random() >= tuning.spark_proc(chance):
                if sf["factor_type"] == 3:
                    unproced_uniques.append((pos, f, sf))
                continue
            acc.proc(pos, f, sf)
    random.shuffle(unproced_uniques)
    for pos, f, sf in unproced_uniques[:forced_uniques]:
        acc.proc(pos, f, sf, trainer_skill=7)
    return acc.result()


class _InspirationTotals:

    def __init__(self, chara_info: dict):
        self.chara_info = chara_info
        self.learned = {s.get("skill_id") for s in chara_info.get("skill_array") or []}
        self.stats: dict[str, int] = {}
        self.skill_points = 0
        self.caps: dict[str, int] = {}
        self.aptitudes: dict[str, int] = {}
        self.hints: list = []
        self.factors: dict[int, list] = {}
        self.non_blue = 0

    def proc(self, pos, f, sf, trainer_skill: int = 0) -> None:
        rows = _factor_steps(sf["factor_group_id"]).get(
            _draw_step(_factor_steps(sf["factor_group_id"]), sf["effect_group_id"])) or []
        paid = False
        for r in rows:
            tt, v1, v2 = r["target_type"], r["value_1"], r["value_2"]
            if tt in _STAT_TARGETS:
                self.stats[_STAT_TARGETS[tt]] = self.stats.get(_STAT_TARGETS[tt], 0) + v1
            elif tt == _SKILL_POINT_TARGET:
                self.skill_points += v1
            elif tt in _CAP_TARGET_TO_STAT:
                stat = _CAP_TARGET_TO_STAT[tt]
                self.caps[stat] = self.caps.get(stat, 0) + v1
            elif tt in _RED_GROUP_TO_APT:
                field = _RED_GROUP_TO_APT[tt]
                if (self.chara_info.get(field, 1) or 1) >= _APT_MIDRUN_CAP:
                    continue
                self.aptitudes[field] = max(self.aptitudes.get(field, 0), v1)
            elif tt == _HINT_TARGET and v1:
                if v1 in self.learned:
                    continue
                self.hints.append({"type": "skill_hint", "skill_id": v1, "value": v2 or 1})
            else:
                continue
            paid = True
        if not paid:
            return
        entry = {"factor_id": f.get("factor_id"), "level": 0}
        if trainer_skill:
            entry["effected_trainer_skill_id_array"] = [trainer_skill]
        self.factors.setdefault(pos, []).append(entry)
        if sf["factor_type"] != 1:
            self.non_blue += 1

    def result(self) -> dict:
        effects = [{"type": s, "value": v} for s, v in self.stats.items()]
        if self.skill_points:
            effects.append({"type": "skill_point", "value": self.skill_points})
        return {
            "effects": effects + self.hints,
            "aptitudes": self.aptitudes,
            "caps": self.caps,
            "factors": [{"position": p, "factor_info_array": fl} for p, fl in self.factors.items()],
            "gold": self.non_blue >= _GOLD_MIN_NON_BLUE,
        }


def _maybe_queue_inspiration(response: dict, full_state: dict, career: dict,
                             viewer_id, new_turn) -> None:
    if new_turn not in _INSPIRATION_EVENTS:
        return
    fired = career["data"].setdefault(INSPIRATION_FIRED_KEY, [])
    if new_turn in fired:
        return
    chara_info = career["data"]["chara_info"]
    roll = _roll_inspiration(chara_info, viewer_id,
                             full_state.get(START_CHARA_STATE_KEY) or {}, new_turn)
    if roll is None:
        return
    fired.append(new_turn)
    pre_id, main_id, post_id = _INSPIRATION_EVENTS[new_turn]
    chara = (chara_info.get("card_id") or 0) // 100
    pre = _scenario_event_entry(pre_id, 500000000 + chara * 1000 + 732,
                                play_timing=1, choices=[], chara_id=chara)
    main = _scenario_event_entry(main_id, _INSPIRATION_STORY, play_timing=1, choices=[])
    main["succession_event_info"] = {"effect_type": 2 if roll["gold"] else 1}
    main_payload = {"effects": roll["effects"], "aptitudes": roll.get("aptitudes") or {},
                    "caps": roll.get("caps") or {}, "factors": roll["factors"]}
    if _succession_lottery() and not paid_features.active(full_state, "umaplan"):
        main["succession_event_info"]["succession_gain_info_array"] = None
    elif _succession_lottery():
        second = _roll_inspiration(chara_info, viewer_id,
                                   full_state.get(START_CHARA_STATE_KEY) or {}, new_turn)
        rolls = [roll, second]
        lotteries = [{"effects": r["effects"], "aptitudes": r.get("aptitudes") or {},
                      "caps": r.get("caps") or {}, "factors": _jp_factor_shape(r["factors"])}
                     for r in rolls]
        main["succession_event_info"] = {
            "effect_type": 2 if any(r["gold"] for r in rolls) else 1,
            "succession_gain_info_array": [
                _succession_gain_info(i + 1, chara_info, lot) for i, lot in enumerate(lotteries)]}
        main_payload = {"lotteries": lotteries, "selected": 1}
    post = _scenario_event_entry(post_id, 500000000 + chara * 1000 + 733,
                                 play_timing=1, choices=[], chara_id=chara)
    closes_chain = scenarios.for_chara(chara_info).inspiration_closes_chain
    prio = _INSPIRATION_CLOSING_PRIO if closes_chain else career_events.PRIO_SCENARIO
    emit = ((lambda _r, fs, ev: career_events.emit(fs, ev)) if closes_chain
            else _emit_turn_event)
    emit(response, full_state, career_events.Event(
        event_id=pre_id, story_id=pre.get("story_id"), raw=pre,
        priority=prio, source="inspiration"))
    emit(response, full_state, career_events.Event(
        event_id=main_id, story_id=main.get("story_id"), raw=main,
        priority=prio, resolver="inspiration_main",
        source="inspiration", payload=main_payload))
    emit(response, full_state, career_events.Event(
        event_id=post_id, story_id=post.get("story_id"), raw=post,
        priority=prio, source="inspiration"))


_INSPIRATION_CLOSING_PRIO = career_events.PRIO_TRAILING + 5


def _claim_inspiration_slot(response: dict, full_state: dict, chara_info: dict) -> None:
    if not scenarios.for_chara(chara_info).inspiration_closes_chain:
        return
    data = response.setdefault("data", {})
    if data.get("unchecked_event_array") or full_state.get(SCENARIO_SLOT_RESERVED_KEY):
        return
    wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
    if wire is not None:
        data["unchecked_event_array"] = [wire]


SHARED_NEXT_TURN_KEY = "unique_shared_next_turn"


def _roll_distribution(chara_info: dict, turn: int, specialty_bonus: int = 0,
                       salt: int = 0, full_state: dict | None = None) -> dict:
    rng = random.Random((int(chara_info.get("card_id", 0)) << 8) ^ int(turn)
                       ^ (int(salt or 0) << 24))
    dist = {0: [], 1: [], 2: [], 3: [], 4: []}
    levels = _deck_support_card_levels(chara_info)
    appear_by_position = {e.get("target_id"): e.get("is_appear", 1)
                          for e in chara_info.get("evaluation_info_array") or []}
    deck = [(c.get("support_card_id"), c.get("position"))
            for c in chara_info.get("support_card_array", [])]
    away = float(_AWAY_WEIGHT)
    for sid, pos in deck:
        for v0, _v1, min_bond, _, _ in training_formula.unique_params(
                sid, training_formula.EFFECT_DECK_APPEAR_UP, levels.get(sid) or 0):
            if _bond_of(chara_info, pos) >= min_bond:
                away *= max(0.0, 1 - v0 / 100)
    shared = ((full_state or {}).get(SHARED_NEXT_TURN_KEY) or {})
    shared_bonus = ({int(p): v for p, v in (shared.get("positions") or {}).items()}
                    if shared.get("turn") == int(turn) else {})
    rate_pct = (scenarios.for_chara(chara_info).specialty_rate_pct(full_state)
                if full_state is not None else 0)
    for c in chara_info.get("support_card_array", []):
        sid, pos = c.get("support_card_id"), c.get("position")
        if not appear_by_position.get(pos, 1):
            continue
        bond = _bond_of(chara_info, pos)
        weights = [_BASE_FACILITY_WEIGHT] * 5 + [away]
        t = training_formula.card_type_index(sid)
        if t is not None:
            weights[t] += training_formula.card_effect(sid, "specialty_priority", 0) or 0
            weights[t] += int(specialty_bonus or 0)
            weights[t] += tuning.specialty_bonus()
            weights[t] = int(weights[t] * tuning.off_type_scale())
            weights[t] += shared_bonus.get(pos, 0)
            if full_state is not None:
                weights[t] += scenarios.for_chara(chara_info).specialty_bonus_by_type(
                    full_state, chara_info, t)
            unique_lv = levels.get(sid)
            if unique_lv:
                weights[t] += training_formula.unique_effect(
                    sid, training_formula.EFFECT_SPECIALTY_PRIORITY, level=unique_lv,
                    bond=bond)
            if rate_pct:
                weights[t] *= 1 + rate_pct / 100
        if tuning.luck():
            weights[-1] = max(0, int(round(weights[-1] * (1 - tuning.luck()))))
        pick = rng.choices(range(6), weights=weights, k=1)[0]
        if pick != 5:
            dist[pick].append(pos)
            extra = sum(v0 for v0, min_bond, *_ in training_formula.unique_params(
                sid, training_formula.EFFECT_TWO_FACILITIES, levels.get(sid) or 0)
                if bond >= min_bond)
            others = [i for i in range(6) if i != pick]
            for _ in range(int(extra)):
                again = rng.choices(others, weights=[weights[i] for i in others], k=1)[0]
                if again == 5:
                    break
                dist[again].append(pos)
                others.remove(again)
    return dist


def _place_unlocked_npcs(unlocked_npcs, turn: int) -> dict:
    plain = [t for t in (unlocked_npcs or ()) if t not in single_mode_events.VERSUS_NPCS]
    out = {cmd: [] for cmd in TRAINING_COMMAND_IDS}
    for fac, tid in single_mode_events.npc_placements(turn, plain).items():
        if fac in out:
            out[fac].append(tid)
    return out


_MAX_FACILITY_PARTNERS = 5


def _npc_supporters(full_state: dict, chara_info: dict) -> list:
    return scenarios.for_chara(chara_info).npc_supporters(full_state, chara_info)


def _npc_supporter_placements(full_state: dict, chara_info: dict, turn: int,
                              occupied: dict = None) -> dict:
    supporters = _npc_supporters(full_state or {}, chara_info)
    if not supporters:
        return {}
    taken = dict(occupied or {})
    weights = [_BASE_FACILITY_WEIGHT] * 5 + [_AWAY_WEIGHT]
    out: dict = {}
    for chara_id in supporters:
        pick = random.Random((int(turn) << 16) ^ int(chara_id)).choices(
            range(6), weights=weights, k=1)[0]
        if pick == 5:
            continue
        fac = TRAINING_COMMAND_IDS[pick]
        if taken.get(fac, 0) >= _MAX_FACILITY_PARTNERS:
            continue
        taken[fac] = taken.get(fac, 0) + 1
        out.setdefault(fac, []).append(chara_id)
    return out


def _ensure_supporter_eval_rows(chara_info: dict, supporter_ids) -> None:
    if not supporter_ids:
        return
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for chara_id in supporter_ids:
        if chara_id in have:
            continue
        have.add(chara_id)
        arr.append({"target_id": chara_id, "training_partner_id": chara_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 1, "group_outing_info_array": []})


_CAMP_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
_CAMP_ID_BY_BASE = {101: 601, 105: 602, 102: 603, 103: 604, 106: 605}
_CAMP_BASE_BY_ID = {v: k for k, v in _CAMP_ID_BY_BASE.items()}
_CAMP_LEVEL = 5
_CAMP_OUTING_GROUP = 304
_CAMP_REST_ENERGY = 40
_CAMP_REST_MOOD = 1

FACILITY_LEVELS_KEY = "facility_levels"
FACILITY_TRAINS_KEY = "facility_trains"
_LEVEL_UP_TRAINS = 4
_MAX_FACILITY_LEVEL = 5
FACILITY_HOLD_KEY = "_facility_level_hold"

FRIENDSHIP_STACK_KEY = "friendship_stack_counts"


def _is_camp_turn(turn) -> bool:
    return turn in _CAMP_TURNS


def _facility_levels(career_data: dict) -> dict:
    lv = career_data.get(FACILITY_LEVELS_KEY)
    if not isinstance(lv, dict):
        lv = {str(cmd): 1 for cmd in TRAINING_COMMAND_IDS}
        career_data[FACILITY_LEVELS_KEY] = lv
    return lv


def _facility_level(career_data: dict, base_cmd: int) -> int:
    return _facility_levels(career_data).get(str(base_cmd), 1)


def _hold_facility_levels(viewer_id, endpoint: str, response: dict) -> None:
    if not endpoint.endswith("exec_command"):
        return
    data = (response or {}).get("data")
    if not isinstance(data, dict) or not isinstance(data.get("chara_info"), dict):
        return
    full_state = state_store.get_state(viewer_id) or {}
    career_data = (full_state.get(STATE_KEY) or {}).get("data") or {}
    hold = career_data.get(FACILITY_HOLD_KEY)
    chara_info = data["chara_info"]
    if not isinstance(hold, dict) or hold.get("turn") != chara_info.get("turn"):
        return
    old = {int(k): int(v) for k, v in (hold.get("levels") or {}).items()}
    for e in chara_info.get("training_level_info_array") or ():
        if e.get("command_id") in old:
            e["level"] = old[e["command_id"]]
    scen = scenarios.for_chara(chara_info)
    for c in (data.get("home_info") or {}).get("command_info_array") or ():
        base = scen.base_command(c.get("command_id"))
        if base in old and "level" in c:
            c["level"] = old[base]


def _record_facility_train(career_data: dict, base_cmd: int,
                           chara_info: dict | None = None) -> bool:
    if base_cmd not in _COMMAND_TO_TYPE:
        return False
    levels = _facility_levels(career_data)
    trains = career_data.setdefault(FACILITY_TRAINS_KEY, {})
    key = str(base_cmd)
    if levels.get(key, 1) >= _MAX_FACILITY_LEVEL:
        return False
    n = trains.get(key, 0) + 1
    leveled = n >= _LEVEL_UP_TRAINS
    if leveled:
        n = 0
        target_turn = (chara_info if isinstance(chara_info, dict) else {}).get("turn")
        career_data[FACILITY_HOLD_KEY] = {"turn": target_turn,
                                          "levels": {key: levels.get(key, 1)}}
        levels[key] = min(_MAX_FACILITY_LEVEL, levels.get(key, 1) + 1)
    trains[key] = n
    if leveled:
        target = chara_info if isinstance(chara_info, dict) else career_data.get("chara_info")
        tla = (target if isinstance(target, dict) else {}).setdefault(
            "training_level_info_array", [])
        entry = next((e for e in tla if e.get("command_id") == base_cmd), None)
        if entry:
            entry["level"] = levels[key]
        else:
            tla.append({"command_id": base_cmd, "level": levels[key]})
    return leveled


_RACE_COMMAND_ID = 401
_FORCEABLE_NON_TRAINING_IDS = {301, 390, 701, 304}


@functools.lru_cache(maxsize=64)
def _forced_race_turns(route_race_id_array: tuple) -> frozenset:
    return frozenset(row[0] for row in _route_goal_rows(route_race_id_array))


def _poll_career_events(response: dict, full_state: dict, career: dict,
                        payload: dict, turn=None, endpoint: str = "") -> list:
    chara_info = career["data"]["chara_info"]
    scenarios.for_chara(chara_info).retime_queued(full_state, endpoint)
    shown = response.get("data", {}).get("unchecked_event_array") or []
    if not shown:
        already_active = career_events.active(full_state)
        if already_active is not None and not career_events.servable_on(
                already_active.to_dict(), endpoint):
            already_active = None
        if already_active is not None:
            response.setdefault("data", {})["unchecked_event_array"] = [already_active.wire_entry()]
            return [already_active]
    client_turn = turn if turn is not None else payload.get("current_turn")
    if client_turn is None:
        client_turn = max(1, (chara_info.get("turn") or 1) - 1)
    ctx = career_events.Ctx(
        viewer_id=payload.get("viewer_id"), full_state=full_state, career=career,
        chara_info=chara_info, turn=int(client_turn),
        advanced_turn=chara_info.get("turn") or (int(client_turn) + 1),
        payload=payload)
    career_events.note_endpoint(full_state, endpoint)
    accepted = career_events.poll(ctx)
    if not accepted:
        return []
    shown = response.get("data", {}).get("unchecked_event_array") or []
    if not shown:
        wire, _ev = career_events.serve_next(full_state, endpoint)
        if wire is not None:
            response.setdefault("data", {})["unchecked_event_array"] = [wire]
    return accepted


def _forced_training_gain():
    value = config.get("force_training_gain")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("client_config.json force_training_gain is not an integer: %r",
                    value)
        return None


def _training_gain_multiplier():
    value = config.get("training_gain_multiplier")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        log.warning("client_config.json training_gain_multiplier is not a number: %r",
                    value)
        return None
    return None if value == 1 else max(0.0, value)


def _build_training_command_info(chara_info: dict, template_command_info, distribution: dict,
                                 unlocked_npcs=(), turn: int = 1,
                                 facility_levels: dict = None, camp: bool = False,
                                 race_history=(), training_bonus: dict = None,
                                 friendship_bonus: int = 0,
                                 support_card_levels: dict = None,
                                 friendship_stacks: dict = None,
                                 full_state: dict = None) -> list:
    current = {s: chara_info.get(s, 0) for s in training_formula.STAT_NAMES}
    maxs = {s: chara_info.get("max_wiz" if s == "wiz" else f"max_{s}", 9999)
            for s in training_formula.STAT_NAMES}
    motivation = max(0, min(4, chara_info.get("motivation", 2)))
    scenario_id = chara_info.get("scenario_id", 1)
    pos_to_sid = {c["position"]: c["support_card_id"] for c in chara_info.get("support_card_array", [])}
    deck_card_ids = list(pos_to_sid.values())
    total_fan = chara_info.get("fans", 0) or 0
    current_vital = chara_info.get("vital", 0) or 0
    max_vital = chara_info.get("max_vital", 0) or 0
    deck_bond_sum = sum(_bond_of(chara_info, p) for p in pos_to_sid)
    pure_passion = _pure_passion_cards(chara_info)
    npc_placement = _place_unlocked_npcs(unlocked_npcs, turn)
    occupied = {cmd: len(distribution.get(_COMMAND_TO_TYPE[cmd], []))
                     + len(npc_placement.get(cmd, []))
                for cmd in TRAINING_COMMAND_IDS}
    supporter_placement = (_npc_supporter_placements(full_state, chara_info, turn, occupied)
                           if full_state is not None else {})
    _ensure_supporter_eval_rows(
        chara_info, [c for ids in supporter_placement.values() for c in ids])
    for cmd, ids in supporter_placement.items():
        occupied[cmd] = occupied.get(cmd, 0) + len(ids)
    standing = {cmd: [*distribution.get(_COMMAND_TO_TYPE[cmd], []), *npc_placement.get(cmd, []),
                      *supporter_placement.get(cmd, [])]
                for cmd in TRAINING_COMMAND_IDS}
    team_placement = (scenarios.for_chara(chara_info).partner_placements(
                          full_state, chara_info, turn, occupied, standing=standing)
                      if full_state is not None else {})

    owned_skill_ids = [s.get("skill_id") for s in chara_info.get("skill_array") or ()
                       if s.get("skill_id")]
    facility_level_sum = sum(
        _CAMP_LEVEL if camp else int((facility_levels or {}).get(str(c), 1) or 1)
        for c in TRAINING_COMMAND_IDS)
    training = []
    for cmd in TRAINING_COMMAND_IDS:
        level = _CAMP_LEVEL if camp else (facility_levels or {}).get(str(cmd), 1)
        served_cmd = _CAMP_ID_BY_BASE[cmd] if camp else cmd
        partners = list(distribution.get(_COMMAND_TO_TYPE[cmd], []))
        in_facility = [(pos_to_sid.get(p), _bond_of(chara_info, p)) for p in partners]
        hinting = _hinting_partners(chara_info, turn, partners)
        partners += npc_placement.get(cmd, [])
        supporters_here = supporter_placement.get(cmd, [])
        partners += supporters_here
        partners += [p for p in team_placement.get(cmd, []) if p not in partners]
        while len(partners) > _MAX_FACILITY_PARTNERS and supporters_here:
            partners.remove(supporters_here[-1])
            supporters_here = supporters_here[:-1]
        if full_state is not None:
            partners = list(scenarios.for_chara(chara_info).final_partners(
                full_state, chara_info, cmd, turn, list(partners)))
        hinting = scenarios.for_chara(chara_info).hint_partners(
            full_state or {}, chara_info, cmd, list(partners), hinting)
        gains = (scenarios.for_chara(chara_info).training_gains(
                     full_state, chara_info, cmd, level, camp, list(partners))
                 if full_state is not None else None)
        if gains is None:
            gains = training_formula.calculate_training_gain(
                card_id=chara_info.get("card_id"), in_facility_cards=in_facility,
                command_id=cmd, scenario_id=scenario_id, facility_level=level,
                motivation=motivation, current_stats=current, max_stats=maxs,
                npc_supporter_count=len(supporters_here),
                extra_stat_bonus=training_bonus,
                scenario_friendship_bonus=friendship_bonus,
                support_card_levels=support_card_levels,
                deck_card_ids=deck_card_ids, total_fan=total_fan,
                current_vital=current_vital, max_vital=max_vital,
                deck_bond_sum=deck_bond_sum, friendship_stacks=friendship_stacks,
                pure_passion_cards=pure_passion, owned_skill_ids=owned_skill_ids,
                facility_level_sum=facility_level_sum)
        if not gains:
            continue
        for s in training_formula.STAT_NAMES:
            if gains.get(s) and conditions.blocks_stat(chara_info, s):
                gains[s] = 0
        forced = _forced_training_gain()
        pin_only = tuning.pinned_stats() if forced is not None else set()
        if pin_only:
            gains = dict(gains)
            for s in pin_only:
                gains[s] = forced
            forced = None
        mult = _training_gain_multiplier()
        sp_mult = tuning.mult("skill_point_multiplier")
        rules = {s: tuning.stat_rule(s) for s in tuning.STAT_KEYS}
        if mult is not None or sp_mult != 1.0 or any(r != (1.0, 0) for r in rules.values()):
            gains = dict(gains)
            for s in (*training_formula.STAT_NAMES, "skill_point"):
                if gains.get(s) and s not in pin_only:
                    x, add = rules[s]
                    gains[s] = max(0, int(round(gains[s] * (mult if mult is not None else 1.0)
                                                * (sp_mult if s == "skill_point" else 1.0) * x)) + add)
        if forced is None:
            params = [{"target_type": _STAT_TO_TARGET_TYPE[s], "value": gains[s]}
                      for s in training_formula.STAT_NAMES if gains.get(s, 0)]
            if gains.get("skill_point"):
                params.append({"target_type": 30, "value": gains["skill_point"]})
        else:
            params = [{"target_type": _STAT_TO_TARGET_TYPE[s], "value": forced}
                      for s in training_formula.STAT_NAMES]
            params.append({"target_type": 30, "value": forced})
        params.append({"target_type": 10, "value": gains.get("energy_cost", -20)})
        params = scenarios.for_chara(chara_info).scale_training_preview(
            full_state or {}, chara_info, cmd, params)
        fail = training_formula.failure_rate(
            chara_info.get("vital", 100), command_id=cmd, facility_level=level,
            conditions=chara_info.get("chara_effect_id_array"),
            facility_cards=_facility_failure_cards(chara_info, partners))
        override = scenarios.for_chara(chara_info).training_failure_override(
            full_state or {}, chara_info, cmd, partners)
        if override is not None:
            fail = override
        fail = tuning.failure_pct(fail)
        training.append({
            "command_type": 1, "command_id": served_cmd, "is_enable": 1,
            "training_partner_array": list(partners),
            "tips_event_partner_array": hinting, "params_inc_dec_info_array": params,
            "failure_rate": fail, "level": level,
        })
    def _non_training_entry(ctype, cid, enable=1):
        return {"command_type": ctype, "command_id": cid, "is_enable": enable,
                "training_partner_array": [], "tips_event_partner_array": [],
                "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0}

    if camp:
        non_training = [_non_training_entry(3, _CAMP_OUTING_GROUP),
                        _non_training_entry(4, _RACE_COMMAND_ID)]
        if scenarios.for_chara(chara_info).camp_lists_rest:
            non_training += [_non_training_entry(7, 701, enable=0),
                             _non_training_entry(8, 801, enable=0)]
    else:
        non_training = [_non_training_entry(3, 301), _non_training_entry(3, 390),
                        _non_training_entry(4, _RACE_COMMAND_ID),
                        _non_training_entry(7, 701),
                        _non_training_entry(8, 801, enable=0)]

    forced_turns = _forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    unlock_turn = min(forced_turns) if forced_turns else None
    is_forced_turn = turn in forced_turns
    if is_forced_turn:
        for c in training:
            c["is_enable"] = 0
            c["training_partner_array"] = []
            c["tips_event_partner_array"] = []
            c["failure_rate"] = 0
    for c in non_training:
        cid = c.get("command_id")
        if cid == _RACE_COMMAND_ID:
            c["is_enable"] = 1 if (unlock_turn is not None and turn >= unlock_turn) else 0
        elif cid in _FORCEABLE_NON_TRAINING_IDS and not (camp and cid == 701):
            c["is_enable"] = 0 if is_forced_turn else 1

    commands = training + non_training
    if full_state is not None:
        commands = scenarios.for_chara(chara_info).home_commands(
            full_state, chara_info, turn, commands)
    if _forced_rest_stories({**chara_info, "turn": turn}):
        for c in commands:
            c["is_enable"] = 1 if c.get("command_id") in _REST_COMMANDS else 0
    return commands


def _refresh_failure_rates(chara_info: dict, home_info: dict,
                           full_state: dict = None,
                           facility_levels: dict = None) -> None:
    conds = chara_info.get("chara_effect_id_array")
    vital = chara_info.get("vital", 100)
    scenario = scenarios.for_chara(chara_info)
    for c in home_info.get("command_info_array") or []:
        if c.get("command_type") == 1 and c.get("command_id") is not None and c.get("is_enable"):
            cmd = c["command_id"]
            base = scenario.base_command(cmd)
            level = int(c.get("level") or (facility_levels or {}).get(str(base), 1) or 1)
            rate = training_formula.failure_rate(
                vital, command_id=cmd, facility_level=level, conditions=conds,
                facility_cards=_facility_failure_cards(
                    chara_info, c.get("training_partner_array") or []))
            override = scenario.training_failure_override(
                full_state or {}, chara_info, base,
                c.get("training_partner_array") or [])
            c["failure_rate"] = tuning.failure_pct(rate if override is None else override)


def _refresh_command_info(chara_info: dict, home_info: dict, turn: int, unlocked_npcs=(),
                          facility_levels: dict = None, race_history=(),
                          training_bonus: dict = None,
                          friendship_bonus: int = 0,
                          specialty_bonus: int = 0,
                          support_card_levels: dict = None,
                          friendship_stacks: dict = None,
                          full_state: dict = None) -> list:
    distribution = _roll_distribution(
        chara_info, turn, specialty_bonus,
        salt=scenarios.for_chara(chara_info).placement_salt(full_state or {}),
        full_state=full_state)
    home_info["command_info_array"] = _build_training_command_info(
        chara_info, home_info.get("command_info_array"), distribution,
        unlocked_npcs=unlocked_npcs, turn=turn,
        facility_levels=facility_levels, camp=_is_camp_turn(turn),
        race_history=race_history, training_bonus=training_bonus,
        friendship_bonus=friendship_bonus, support_card_levels=support_card_levels,
        friendship_stacks=friendship_stacks, full_state=full_state)
    forced_turns = _forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    unlock_turn = min(forced_turns) if forced_turns else None
    restricted = (unlock_turn is None or turn is None or turn < unlock_turn)
    until = chara_info.get(RACE_RESTRICT_UNTIL_KEY) or 0
    if turn is not None and until and turn < until:
        restricted = True
    home_info["race_entry_restriction"] = 1 if restricted else 0

    _sync_infirmary_lock(chara_info, home_info, turn)
    return home_info["command_info_array"]


def _sync_infirmary_lock(chara_info: dict, home_info: dict, turn: int | None = None) -> None:
    cmds = home_info.get("command_info_array")
    if cmds is None:
        return
    treatable = conditions.any_negative(chara_info)
    found = next((c for c in cmds if c.get("command_id") == _INFIRMARY_COMMAND_ID), None)
    if found is not None:
        found["is_enable"] = 1 if treatable else 0
    elif turn is not None and not _is_camp_turn(turn):
        cmds.append({"command_type": _INFIRMARY_COMMAND_TYPE,
                     "command_id": _INFIRMARY_COMMAND_ID,
                     "is_enable": 1 if treatable else 0,
                     "training_partner_array": [], "tips_event_partner_array": [],
                     "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0})


def _partners_in_command(career_state: dict, command_id) -> list:
    hi = career_state.get("data", {}).get("home_info", {})
    for c in hi.get("command_info_array", []) or []:
        if c.get("command_id") == command_id:
            return c.get("training_partner_array", []) or []
    return []


_COMMAND_TRAINED_STAT = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz",
                         601: "speed", 602: "stamina", 603: "power", 604: "guts", 605: "wiz"}
_ALL_STATS = ["speed", "stamina", "power", "guts", "wiz"]

TRAINING_TALLY_KEY = "training_tally"


def _record_training_tally(career_data: dict, command_id, failed: bool) -> None:
    stat = _COMMAND_TRAINED_STAT.get(command_id)
    if stat is None:
        return
    tally = career_data.setdefault(TRAINING_TALLY_KEY, {})
    tally[stat] = int(tally.get(stat) or 0) + 1
    if failed:
        tally["failures"] = int(tally.get("failures") or 0) + 1

_REST_COMMANDS = {701}
REST_CTX_KEY = "active_rest"
_REST_EVENT_IDS = frozenset({7009, 7010, 7011})
_REST_STORY_SUFFIX = {7011: 701, 7009: 700, 7010: 718}


@career_events.resolver("rest")
def _resolve_rest(full_state, chara_info, event, choice_number, career=None, **kw):
    rest_ctx = full_state.get(REST_CTX_KEY)
    if not rest_ctx:
        return {}
    was_capped = (chara_info.get("vital", 0) or 0) >= chara_info.get("max_vital", 100)
    _apply_rest_outcome(chara_info, rest_ctx)
    career_data = (career or {}).get("data") if isinstance(career, dict) else None
    career_home = career_data.get("home_info") if isinstance(career_data, dict) else None
    if isinstance(career_home, dict):
        _refresh_failure_rates(chara_info, career_home, full_state,
                               _facility_levels(career_data))
    full_state.pop(REST_CTX_KEY, None)
    return {"not_up": [6]} if was_capped else {}
_REST_EVENT_BY_ENERGY = {70: 7011, 50: 7010, 30: 7009}

_INFIRMARY_COMMAND_ID = 801
_INFIRMARY_COMMAND_TYPE = 8
_INFIRMARY_EVENT_ID = 7016
_INFIRMARY_STORY_SUFFIX = 717
_INFIRMARY_ENERGY = 20
_INFIRMARY_CURE_CHANCE = 0.60
INFIRMARY_CTX_KEY = "infirmary_ctx"


@career_events.resolver("infirmary")
def _resolve_infirmary(full_state, chara_info, event, choice_number, career=None, **kw):
    was_capped = (chara_info.get("vital", 0) or 0) >= chara_info.get("max_vital", 100)
    chara_info["vital"] = min(chara_info.get("max_vital", 100),
                              (chara_info.get("vital", 0) or 0) + _INFIRMARY_ENERGY)
    cured = conditions.cure_negatives_by_chance(chara_info, _INFIRMARY_CURE_CHANCE)
    log.info("infirmary: +%s energy, cured=%s", _INFIRMARY_ENERGY, cured)
    career_data = (career or {}).get("data") if isinstance(career, dict) else None
    career_home = career_data.get("home_info") if isinstance(career_data, dict) else None
    if isinstance(career_home, dict) and isinstance(full_state, dict):
        _refresh_command_info(
            chara_info, career_home, turn=chara_info.get("turn"),
            unlocked_npcs=[n[0] for n in
                           full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])],
            facility_levels=_facility_levels(career_data),
            race_history=career_data.get("race_history", []),
            training_bonus=_training_bonus(full_state, chara_info),
            friendship_bonus=_friendship_bonus(full_state, chara_info),
            specialty_bonus=_specialty_bonus(full_state, chara_info),
            support_card_levels=_support_card_levels(full_state),
            friendship_stacks=_friendship_stacks(full_state),
            full_state=full_state)
    return {"not_up": [6]} if was_capped else {}


def _roll_rest_outcome() -> dict:
    r = random.random()
    night_owl = False
    if r < 0.247:
        energy = 70
    elif r < 0.852:
        energy = 50
    elif r < 0.975:
        energy = 30
    else:
        energy = 30
        night_owl = True
    slow_metab = False
    return {"event_id": _REST_EVENT_BY_ENERGY[energy], "energy": energy,
            "night_owl": night_owl, "slow_metab": slow_metab}


def _apply_rest_outcome(chara_info: dict, outcome: dict) -> None:
    mv = chara_info.get("max_vital", 100)
    chara_info["vital"] = max(0, min(mv, chara_info.get("vital", 0) + outcome.get("energy", 0)))
    if outcome.get("night_owl"):
        single_mode_events.add_condition(chara_info, _COND_NIGHT_OWL)
    if outcome.get("slow_metab"):
        single_mode_events.add_condition(chara_info, _COND_SLOW_METABOLISM)


def _rest_event_entry(event_id: int, chara_id: int) -> dict:
    return {
        "event_id": event_id, "chara_id": chara_id,
        "story_id": int(f"50{chara_id}{_REST_STORY_SUFFIX[event_id]:03d}"),
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [{"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                              "gain_select_id_index": 1, "select_icon": 0}],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }
_RECREATION_GROUPS = {301, 390, 304}
_MOTIVATION_MAX = 5
_COND_NIGHT_OWL = 1
_COND_SLOW_METABOLISM = 4


_OUTING_RIVERBANK = 301
_OUTING_KARAOKE = 302
_OUTING_SHRINE = 303


PAL_STATE_KEY = "pal_card_state"
PAL_EVENT_CTX_KEY = "pal_event_ctx"
_PAL_UNLOCK_CHANCE_LOW = 0.15
_PAL_UNLOCK_CHANCE_HIGH = 0.68
_PAL_RANDOM_CHANCE = 0.10
_PAL_RANDOM_CHANCE_BY_CARD = {30052: 0.40}
_GROUP_BUFF_TRAIN_CHANCE = 0.07
_OUTING_COMMAND_GROUP = 390
_PAL_FIRST_EVENT_ID = 10002
_PAL_OUTING_EVENT_ID = 10002
_PAL_NEW_YEAR_TURN = 25
PURE_PASSION_KEY = "pure_passion_until"


def _pal_state(full_state: dict) -> dict:
    return full_state.setdefault(PAL_STATE_KEY, {})


def _pal_card_entry(full_state: dict, card_id: int) -> dict:
    return _pal_state(full_state).setdefault(str(card_id), {})


def _deck_pal_cards(chara_info: dict) -> list:
    return [(c.get("position"), c.get("support_card_id"))
            for c in (chara_info.get("support_card_array") or [])
            if pal_cards.is_pal_or_group(c.get("support_card_id"))]


def _sync_pal_evaluation(full_state: dict, chara_info: dict) -> None:
    state = _pal_state(full_state)
    for pos, card_id in _deck_pal_cards(chara_info):
        entry = state.get(str(card_id)) or {}
        for e in chara_info.get("evaluation_info_array") or []:
            if e.get("target_id") == pos:
                e["is_outing"] = 1 if entry.get("unlocked") else 0
                e["story_step"] = entry.get("step", 0)
                break


def _pure_passion_effects(full_state: dict, chara_info: dict, turn) -> list:
    active = full_state.get(PURE_PASSION_KEY) or {}
    if turn is None:
        return []
    live = {cid: until for cid, until in active.items() if int(until) >= int(turn)}
    expired = []
    if live != active:
        expired = [int(cid) for cid in active if cid not in live]
        full_state[PURE_PASSION_KEY] = live
    known = pal_cards.pure_passion_conditions()
    effects = [e for e in (chara_info.get("chara_effect_id_array") or [])
               if e not in known]
    effects += [int(cid) for cid in live]
    chara_info["chara_effect_id_array"] = effects
    return expired


def _grant_pure_passion(full_state: dict, chara_info: dict, card_id: int, turn) -> bool:
    cond = pal_cards.pure_passion_condition(card_id)
    if not cond or turn is None:
        return False
    active = full_state.setdefault(PURE_PASSION_KEY, {})
    if int(active.get(str(cond)) or 0) >= int(turn):
        return False
    turns = pal_cards.roll_passion_turns(card_id)
    until = int(turn) + turns - 1
    active[str(cond)] = until
    log.info("pure passion: card %s condition %s for %s turns (until turn %s)",
             card_id, cond, turns, active[str(cond)])
    _pure_passion_effects(full_state, chara_info, turn)
    return True


def _pure_passion_cards(chara_info: dict) -> frozenset:
    live = set(chara_info.get("chara_effect_id_array") or ())
    if not live:
        return frozenset()
    return frozenset(
        card_id for _pos, card_id in _deck_pal_cards(chara_info)
        if pal_cards.is_group(card_id)
        and pal_cards.pure_passion_condition(card_id) in live)


def _pal_event_entry(card_id: int, sid: int, chara_info: dict, event_id: int,
                     play_timing: int = 1):
    title, ev = event_engine.event_by_story_id(card_id, sid)
    if ev is None:
        ev = {"choices": [{"name": "Continue", "effects": []}]}
        title = master_data.query_one(
            "SELECT text FROM text_data WHERE category=181 AND [index]=?", (sid,))
        title = title["text"] if title else ""
    return title, ev, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=0, support_card_id=card_id, play_timing=play_timing)


def _pal_ctxs(full_state: dict) -> list:
    v = full_state.get(PAL_EVENT_CTX_KEY)
    return [v] if isinstance(v, dict) else list(v or [])


def _pal_ctx_for(full_state: dict, story_id) -> dict | None:
    return next((c for c in _pal_ctxs(full_state) if c.get("story_id") == story_id), None)


def _queue_pal_event(response, full_state, career_state, card_id, sid, kind,
                     partner=None, front: bool = False, override=None) -> bool:
    chara_info = career_state["data"]["chara_info"]
    if override:
        event_id, timing = override["wire"]
        ev, title = override["event"], override["event"].get("name") or ""
        entry = event_engine.career_event_entry(
            ev, event_id, sid, chara_id=0, support_card_id=card_id, play_timing=timing)
    else:
        event_id, timing = pal_cards.wire_ids(card_id, sid) or (_PAL_FIRST_EVENT_ID, 1)
        title, ev, entry = _pal_event_entry(card_id, sid, chara_info, event_id, timing)
    _queue_turn_event(response, full_state, entry, front=front)
    ctx = {"event_id": event_id, "story_id": sid, "source": "support",
           "source_id": card_id, "title": title,
           "trainee_card_id": chara_info.get("card_id")}
    if override:
        ctx["event"] = ev
    _push_career_ctx(full_state, ctx)
    full_state[PAL_EVENT_CTX_KEY] = [c for c in _pal_ctxs(full_state)
                                     if c.get("story_id") != sid] + [
        {"card_id": card_id, "story_id": sid, "kind": kind, "partner": partner}]
    full_state[event_engine.FIRED_EVENTS_KEY] = list(
        full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    return True


def _maybe_fire_pal_training_event(full_state, career_state, response, pal_partners,
                                   command_id=None) -> bool:
    chara_info = career_state["data"]["chara_info"]
    for pos, card_id in pal_partners or ():
        entry = _pal_card_entry(full_state, card_id)
        if not entry.get("met"):
            sid = pal_cards.first_meeting_story(card_id)
            entry["met"] = 1
            if sid:
                return _queue_pal_event(response, full_state, career_state, card_id,
                                        sid, "first")
            continue
        if (pal_cards.is_group(card_id) and pal_cards.random_passion(card_id)
                and random.random() < _GROUP_BUFF_TRAIN_CHANCE):
            _grant_pure_passion(full_state, chara_info, card_id, chara_info.get("turn"))
        roll = scenarios.for_chara(chara_info).pal_training_roll(
            full_state, chara_info, card_id, command_id)
        if roll is not None:
            if roll["chance"] and random.random() < roll["chance"]:
                return _queue_pal_event(response, full_state, career_state, card_id,
                                        roll["story"], "random", override=roll)
            continue
        chance = _PAL_RANDOM_CHANCE_BY_CARD.get(card_id, _PAL_RANDOM_CHANCE)
        if random.random() < chance:
            sid = pal_cards.random_story(card_id)
            if sid:
                return _queue_pal_event(response, full_state, career_state, card_id,
                                        sid, "random")
    return False


def _member_step(entry: dict, partner) -> int:
    return int((entry.get("steps") or {}).get(str(partner), 0))


def _new_year_outing_ready(full_state: dict, chara_info: dict, card_id: int,
                           turn=None) -> bool:
    entry = _pal_card_entry(full_state, card_id)
    if not entry.get("unlocked") or entry.get("new_year_fired"):
        return False
    if int(turn if turn is not None else chara_info.get("turn") or 0) < _PAL_NEW_YEAR_TURN:
        return False
    return pal_cards.new_year_story(card_id) is not None


def _queue_pal_new_year(response, full_state, career_state) -> bool:
    chara_info = career_state["data"]["chara_info"]
    cards = {c for _p, c in _deck_pal_cards(chara_info)}
    cards |= {int(cid) for cid, e in _pal_state(full_state).items()
              if e.get("unlocked") and pal_cards.is_pal_or_group(int(cid))}
    for card_id in sorted(cards):
        if not _new_year_outing_ready(full_state, chara_info, card_id):
            continue
        sid = pal_cards.new_year_story(card_id)
        if not sid:
            continue
        partner = next(iter(pal_cards.partners(card_id) or ()), None)
        if _queue_pal_event(response, full_state, career_state, card_id, sid,
                            "new_year", partner=partner):
            log.info("new year: pal/group card %s date %s queued behind the "
                     "trainee's own beat", card_id, sid)
            return True
    return False


def _outing_partners(full_state: dict, chara_info: dict) -> list:
    out = []
    cards = {c for _p, c in _deck_pal_cards(chara_info)}
    cards |= {int(cid) for cid, e in _pal_state(full_state).items()
              if e.get("unlocked") and pal_cards.is_pal(int(cid))}
    for card_id in sorted(cards):
        entry = _pal_card_entry(full_state, card_id)
        if not entry.get("unlocked"):
            continue
        for chara in pal_cards.partners(card_id):
            step = (entry.get("step", 0) if pal_cards.is_pal(card_id)
                    else _member_step(entry, chara))
            exhausted = step >= pal_cards.outing_chain_len(
                card_id, None if pal_cards.is_pal(card_id) else chara)
            if (not exhausted and step >= 1 and pal_cards.group_kit(card_id)
                    and any(_member_step(entry, m) < 1 for m in pal_cards.group_members(card_id))):
                exhausted = True
            if exhausted and not _new_year_outing_ready(full_state, chara_info, card_id):
                continue
            out.append((chara, card_id))
    return out


def _card_for_partner(full_state: dict, chara_info: dict, select_id) -> int | None:
    for chara, card_id in _outing_partners(full_state, chara_info):
        if chara == select_id:
            return card_id
    return None


def _queue_outing_event(response, full_state, career_state, card_id, partner,
                        turn=None) -> bool:
    chara_info = career_state["data"]["chara_info"]
    if _new_year_outing_ready(full_state, chara_info, card_id, turn):
        sid = pal_cards.new_year_story(card_id)
        if sid:
            return _queue_pal_event(response, full_state, career_state, card_id, sid,
                                    "new_year", partner=partner, front=True)
    entry = _pal_card_entry(full_state, card_id)
    step = (entry.get("step", 0) if pal_cards.is_pal(card_id)
            else _member_step(entry, partner))
    sid = pal_cards.outing_story(card_id, partner, step)
    if not sid:
        return False
    return _queue_pal_event(response, full_state, career_state, card_id, sid,
                            "outing", partner=partner, front=True)


_PAL_UNLOCK_DECLINE = {30052: (2,)}


def _served_branch_ends_chain(event, choice_number, chara_info, branch) -> bool:
    if event is None:
        return False
    i, count = event_engine.served_outcome(event, choice_number, chara_info, branch)
    if i is None:
        return False
    choices = event.get("choices") or []
    idx = max(1, int(choice_number or 1)) - 1
    idx = idx if idx < len(choices) else 0
    groups = event_engine._choice_effect_groups(
        event_engine.resolve_segments(choices[idx], chara_info))
    return i < len(groups) and "chain_end" in repr(groups[i])


def _pal_chain_ended(full_state: dict, card_id) -> bool:
    return bool(_pal_card_entry(full_state, card_id).get("chain_ended")
                or event_engine.chain_ended(full_state, f"support:{int(card_id)}"))


def _maybe_unlock_after_first_meeting(response, full_state, career_state, card_id):
    ci = career_state["data"]["chara_info"]
    entry = _pal_card_entry(full_state, card_id)
    if (not entry.get("met") or entry.get("unlocked")
            or full_state.get(PAL_EVENT_CTX_KEY)
            or _pal_chain_ended(full_state, card_id)
            or response["data"].get("unchecked_event_array")):
        return False
    if entry.get("meeting_unlock_roll_done"):
        return False
    entry["meeting_unlock_roll_done"] = True
    pos = next((p for p, cid in _deck_pal_cards(ci) if cid == card_id), None)
    bond = _bond_of(ci, pos) if pos is not None else 0
    chance = _PAL_UNLOCK_CHANCE_HIGH if bond >= 60 else _PAL_UNLOCK_CHANCE_LOW
    if random.random() >= tuning.event(chance):
        return False
    pre = pal_cards.pre_unlock_story(card_id)
    sid = pre or pal_cards.unlock_story(card_id)
    if not sid:
        return False
    if pre:
        entry["pre_unlock"] = 1
    return _queue_pal_event(response, full_state, career_state, card_id, sid,
                            "pre_unlock" if pre else "unlock")


def _pal_unlock_hit(card_id, choice_number, event, chara_info, branch) -> bool:
    if event is not None:
        outcome, count = event_engine.served_outcome(
            event, choice_number, chara_info, branch)
        if count > 1 and outcome is not None:
            return outcome == event_engine.richest_outcome(
                event, choice_number, chara_info)
    return random.random() < pal_cards.unlock_chance(card_id, choice_number or 1)


def _commit_pal_event(full_state: dict, chara_info: dict, choice_number: int,
                      event: dict | None = None, branch=None, story_id=None) -> None:
    ctx = _pal_ctx_for(full_state, story_id)
    if not ctx:
        return
    card_id, kind = ctx.get("card_id"), ctx.get("kind")
    entry = _pal_card_entry(full_state, card_id)
    turn = chara_info.get("turn")
    if kind == "unlock":
        if _pal_unlock_hit(card_id, choice_number, event, chara_info, branch):
            entry["unlocked"] = 1
            _grant_pure_passion(full_state, chara_info, card_id, turn)
        elif (_served_branch_ends_chain(event, choice_number, chara_info, branch)
              or int(choice_number or 0) in _PAL_UNLOCK_DECLINE.get(int(card_id), ())):
            entry["chain_ended"] = 1
    elif kind == "outing":
        entry["step"] = (entry.get("step") or 0) + 1
        partner = ctx.get("partner")
        if partner is not None:
            entry.setdefault("outed", [])
            if partner not in entry["outed"]:
                entry["outed"].append(partner)
            steps = entry.setdefault("steps", {})
            steps[str(partner)] = steps.get(str(partner), 0) + 1
        if pal_cards.is_group(card_id):
            done = set(entry.get("outed") or [])
            complete = all(m in done for m in pal_cards.group_members(card_id))
        else:
            complete = entry["step"] >= pal_cards.outing_chain_len(card_id)
        if complete and not entry.get("finale"):
            entry["finale"] = 1
            if not pal_cards.is_pal(card_id):
                entry["finale_pending"] = pal_cards.finale_story(card_id) or 0
            _grant_pure_passion(full_state, chara_info, card_id, turn)
    elif kind == "new_year":
        entry["new_year_fired"] = 1
        if pal_cards.is_pal(card_id):
            entry["finale"] = 1
    rest = [c for c in _pal_ctxs(full_state) if c is not ctx]
    if rest:
        full_state[PAL_EVENT_CTX_KEY] = rest
    else:
        full_state.pop(PAL_EVENT_CTX_KEY, None)
    _sync_pal_evaluation(full_state, chara_info)


def _pal_unlock_preview_args(full_state: dict, story_id=None) -> tuple:
    ctx = _pal_ctx_for(full_state, story_id) or {}
    if ctx.get("kind") != "unlock":
        return None, None
    card_id = ctx.get("card_id")
    partners = pal_cards.partners(card_id)
    if not partners:
        return None, None
    return partners[0], pal_cards.unlock_chances(card_id)


def _maybe_queue_pal_finale(response, full_state, career_state) -> bool:
    for card_id, entry in list(_pal_state(full_state).items()):
        sid = entry.get("finale_pending")
        if sid:
            entry["finale_pending"] = 0
            return _queue_pal_event(response, full_state, career_state, int(card_id),
                                    sid, "finale")
    return False


_CRANE_REWARDS = {1: (5, 0, 0), 2: (10, 1, 1), 3: (15, 2, 2)}
_CRANE_HINT_GROUP = 20038
_CRANE_CHANCE = 0.235
_CRANE_FIRST_TURN = 25
CRANE_CTX_KEY = "active_crane_game"
CRANE_PLAYED_KEY = "crane_game_played"
CRANE_LIFETIME_KEY = "crane_game_lifetime"
CRANE_SESSION_KEY = "crane_session_plushies"
_CRANE_INTRO_EVENT = 6002


@career_events.resolver("crane_outcome")
def _resolve_crane_outcome(full_state, chara_info, event, choice_number, **kw):
    tier = (event.payload or {}).get("tier") or 1
    vital_add, mood_add, hint_add = _CRANE_REWARDS.get(tier, _CRANE_REWARDS[1])
    not_up = []
    max_vital = chara_info.get("max_vital", 100)
    if chara_info.get("vital", 0) >= max_vital:
        not_up.append(6)
    chara_info["vital"] = min(max_vital, chara_info.get("vital", 0) + vital_add)
    if tier == 1:
        cap = chara_info.get("max_guts", 9999)
        chara_info["guts"] = min(chara_info.get("guts", 0) + 3, cap)
    if mood_add:
        if chara_info.get("motivation", 3) >= 5:
            not_up.append(20)
        chara_info["motivation"] = min(5, chara_info.get("motivation", 3) + mood_add)
    if hint_add:
        tips = chara_info.setdefault("skill_tips_array", [])
        tip = next((t for t in tips if t.get("group_id") == _CRANE_HINT_GROUP
                    and t.get("rarity") == 1), None)
        if tip:
            tip["level"] = min(5, (tip.get("level") or 0) + hint_add)
        else:
            tips.append({"group_id": _CRANE_HINT_GROUP, "rarity": 1,
                         "level": min(5, hint_add)})
    return {"not_up": not_up} if not_up else {}


@career_events.resolver("crane_launch")
def _resolve_crane_launch(full_state, chara_info, event, choice_number, **kw):
    chara_info["playing_state"] = 6
    return {}


def _crane_available(full_state: dict) -> bool:
    return not full_state.get(CRANE_PLAYED_KEY)


def _queue_crane_game(response: dict, full_state: dict, career_state: dict) -> None:
    chara = (career_state["data"]["chara_info"].get("card_id") or 0) // 100
    if not chara or not _crane_available(full_state):
        return
    entry = _scenario_event_entry(_CRANE_INTRO_EVENT, 500000000 + chara * 1000 + 726,
                                  play_timing=6, choices=[_ack_choice()], chara_id=chara)
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=_CRANE_INTRO_EVENT, story_id=entry.get("story_id"), raw=entry,
        priority=career_events.PRIO_SPECIAL, resolver="crane_launch", source="crane"))
    full_state[CRANE_PLAYED_KEY] = 1


def handle_minigame_end(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.pop(CRANE_CTX_KEY, None) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/minigame_end", full_state)
    chara = ctx.get("chara") or ((career["data"]["chara_info"].get("card_id") or 0) // 100)
    result = payload.get("result") or {}
    lifetime = full_state.setdefault(CRANE_LIFETIME_KEY, {"play_count": 0, "plushie_count": 0})
    lifetime["play_count"] = (lifetime.get("play_count") or 0) + 1
    session_plushies = int(result.get("result_value") or 0)
    lifetime["plushie_count"] = (lifetime.get("plushie_count") or 0) + session_plushies
    full_state[CRANE_SESSION_KEY] = session_plushies
    tier = result.get("result_state")
    tier = tier if tier in _CRANE_REWARDS else 1
    base = 500000000 + chara * 1000
    suffix = 731
    rows = master_data.query(
        "SELECT story_id, mini_game_result FROM single_mode_story_data "
        "WHERE story_id BETWEEN ? AND ?", (base + 727, base + 731))
    matches = [r["story_id"] - base for r in rows if r["mini_game_result"] == tier]
    if matches:
        suffix = max(matches)
    outcome_event = _CRANE_INTRO_EVENT + (suffix - 726)
    entry = _scenario_event_entry(outcome_event, base + suffix,
                                  play_timing=8, choices=[_ack_choice()], chara_id=chara)
    detail = [dict(d, viewer_id=payload.get("viewer_id"),
                   single_mode_chara_id=(career["data"]["chara_info"].get("single_mode_chara_id")
                                         if isinstance(career, dict) else 0),
                   turn=payload.get("current_turn"), minigame_id=1)
              for d in (result.get("result_detail_array") or [])]
    entry["minigame_result"] = {"result_state": result.get("result_state"),
                                "result_value": result.get("result_value"),
                                "result_detail_array": detail}

    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    if isinstance(career, dict):
        updated = copy.deepcopy(career["data"]["chara_info"])
        updated["playing_state"] = 5
        data["chara_info"] = updated
        career_home = career["data"].get("home_info")
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=outcome_event, story_id=base + suffix, raw=entry,
        priority=career_events.PRIO_SPECIAL, resolver="crane_outcome",
        source="crane", payload={"tier": tier}))
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def _lineage_factor_groups(viewer_id, full_state: dict) -> dict:
    counts: dict[int, int] = {}
    start_chara = full_state.get(START_CHARA_STATE_KEY) or {}
    roster = _roster_by_trained_id(viewer_id)
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        parent = roster.get(start_chara.get(key)) if start_chara.get(key) else None
        if not parent:
            continue
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        members = [parent] + [by_pos[p] for p in (10, 20) if p in by_pos]
        for m in members:
            for f in m.get("factor_info_array") or []:
                g = (f.get("factor_id") or 0) // 100
                counts[g] = counts.get(g, 0) + 1
    return counts


def _roll_career_factors(chara_info: dict, race_history, lineage_groups=None,
                         missed: list | None = None) -> list:
    rolled = []
    stat_groups = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}
    stat = random.choice(list(stat_groups))
    v = chara_info.get(stat, 0)
    if v < 600:
        star = tuning.legacy_star((1, 2), (90, 10))
    elif v < 1100:
        star = tuning.legacy_star((1, 2, 3), (48, 46, 6))
    else:
        star = tuning.legacy_star((1, 2, 3), (21, 68, 11))
    rolled.append(stat_groups[stat] * 100 + star)

    apt_groups = {"proper_ground_turf": 11, "proper_ground_dirt": 12,
                  "proper_running_style_nige": 21, "proper_running_style_senko": 22,
                  "proper_running_style_sashi": 23, "proper_running_style_oikomi": 24,
                  "proper_distance_short": 31, "proper_distance_mile": 32,
                  "proper_distance_middle": 33, "proper_distance_long": 34}
    eligible = [g for k, g in apt_groups.items() if (chara_info.get(k) or 1) >= 7]
    if eligible:
        rolled.append(random.choice(eligible) * 100
                      + tuning.legacy_star((1, 2, 3), (20, 70, 10)))

    from .. import rating_formula
    score = rating_formula.get_career_score(chara_info)

    def band_star():
        if score < 6500:
            return tuning.legacy_star((1, 2), (90, 10))
        if score < 17500:
            return tuning.legacy_star((1, 2, 3), (50, 45, 5))
        return tuning.legacy_star((1, 2, 3), (20, 70, 10))

    card = chara_info.get("card_id") or 0
    if master_data.query_one(
            "SELECT factor_id FROM succession_factor WHERE factor_group_id=? AND factor_type=3 "
            "LIMIT 1", (card,)):
        rolled.append(card * 100 + band_star())

    lineage = lineage_groups or {}
    for s in chara_info.get("skill_array") or []:
        sid = s.get("skill_id")
        row = master_data.query_one(
            "SELECT sfe.factor_group_id AS g FROM succession_factor_effect sfe "
            "JOIN succession_factor sf ON sf.factor_group_id=sfe.factor_group_id "
            "AND sf.factor_type=4 WHERE sfe.target_type=41 AND sfe.value_1=? "
            "ORDER BY sfe.factor_group_id LIMIT 1", (sid,))
        g = row["g"] if row else None
        if g is None and master_data.query_one(
                "SELECT factor_id FROM succession_factor WHERE factor_group_id=? "
                "AND factor_type=4 LIMIT 1", (sid // 10,)):
            g = sid // 10
        if not g:
            continue
        rar = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (sid,))
        base, cap = (0.40, 0.70) if (rar and rar["rarity"] == 2) else (0.20, 0.35)
        chance = min(cap, base * (1.1 ** lineage.get(g, 0)))
        if random.random() < tuning.legacy_chance(chance):
            rolled.append(g * 100 + band_star())
        elif missed is not None:
            missed.append(g * 100 + band_star())

    seen_races = set()
    for h in race_history or []:
        if h.get("result_rank") != 1:
            continue
        row = master_data.query_one(
            "SELECT ri.race_id AS rid, r.grade AS grade FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id=p.race_instance_id "
            "JOIN race r ON r.id=ri.race_id WHERE p.id=?", (h.get("program_id"),))
        if (row and row["grade"] == 100 and 1001 <= row["rid"] <= 1024
                and row["rid"] not in seen_races):
            seen_races.add(row["rid"])
            if random.random() < tuning.legacy_chance(0.20):
                rolled.append((9000 + row["rid"]) * 100 + band_star())
            elif missed is not None:
                missed.append((9000 + row["rid"]) * 100 + band_star())

    if chara_info.get("state") == 3:
        if random.random() < tuning.legacy_chance(0.20):
            rolled.append(3000100 + band_star())
        elif missed is not None:
            missed.append(3000100 + band_star())
    if missed is not None:
        got = {fid // 100 for fid in rolled}
        missed[:] = [f for f in _valid_factor_ids(missed) if f // 100 not in got]
    return [{"factor_id": fid, "level": 0} for fid in _valid_factor_ids(rolled)]


FACTOR_ROLL_HOOKS: list = []


def _career_roll(viewer_id, full_state: dict, chara_info: dict, race_history) -> list:
    missed: list = []
    factors = _roll_career_factors(chara_info, race_history or [],
                                   _lineage_factor_groups(viewer_id, full_state), missed)
    for hook in FACTOR_ROLL_HOOKS:
        factors = hook(full_state, factors, missed)
    return factors


def _valid_factor_ids(rolled: list) -> list:
    best: dict[int, int] = {}
    for fid in rolled:
        g, star = divmod(fid, 100)
        stars = [r["rarity"] for r in master_data.query(
            "SELECT rarity FROM succession_factor WHERE factor_group_id=?", (g,))]
        if not stars:
            continue
        if star not in stars:
            lower = [x for x in stars if x <= star]
            star = max(lower) if lower else min(stars)
        best[g] = max(best.get(g, 0), star)
    return [g * 100 + star for g, star in best.items()]


def handle_factor_select(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    stashed = _ensure_factor_roll(viewer_id, full_state)
    picked = payload.get("factor_lottery_id")
    if picked and any(r["lottery_id"] == int(picked) for r in stashed["rolls"]):
        stashed["selected_lottery_id"] = int(picked)
    if "is_bulk_lottery" in payload:
        return _jp_factor_select(viewer_id, full_state, stashed, payload)
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_select_common": {
                "rank_score": stashed["rank_score"], "rank": stashed["rank"],
                "lottery_remain_num": _FACTOR_RELOTTERY_LIMIT - (len(stashed["rolls"]) - 1),
                "lottery_count": len(stashed["rolls"]),
                "factor_select_info_array": [
                    {"lottery_id": r["lottery_id"],
                     "factor_info_array": r["factors"]} for r in stashed["rolls"]],
                "factor_relottery_info_array": []}}}


_FACTOR_RELOTTERY_LIMIT = 2

SHORT_CUT_KEY = "career_short_cut_state"


def _ensure_factor_roll(viewer_id, full_state: dict) -> dict:
    stashed = full_state.get("career_factor_roll")
    career = full_state.get(STATE_KEY)
    ci = career["data"]["chara_info"] if isinstance(career, dict) else {}
    if not stashed:
        from .. import rating_formula
        history = career["data"].get("race_history") if isinstance(career, dict) else []
        factors = _career_roll(viewer_id, full_state, ci, history)
        score = rating_formula.get_career_score(ci)
        stashed = {"factors": factors, "rank_score": score,
                   "rank": trained_chara._rank_for_score(score)}
        full_state["career_factor_roll"] = stashed
    if not stashed.get("rolls"):
        stashed["rolls"] = [{"lottery_id": 1, "factors": stashed["factors"]}]
    stashed.setdefault("selected_lottery_id", stashed["rolls"][0]["lottery_id"])
    return stashed


def _selected_factors(stashed):
    if not isinstance(stashed, dict):
        return None
    want = stashed.get("selected_lottery_id")
    for roll in stashed.get("rolls") or ():
        if roll.get("lottery_id") == want:
            return roll.get("factors")
    return stashed.get("factors")


def handle_factor_lottery(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/factor_lottery", full_state)
    stashed = _ensure_factor_roll(viewer_id, full_state)
    if "request_lottery_type" in payload:
        return _jp_factor_lottery(viewer_id, full_state, career, stashed, payload)
    if len(stashed["rolls"]) > _FACTOR_RELOTTERY_LIMIT:
        log.warning("factor_lottery refused: %s rolls already used (limit %s+1)",
                    len(stashed["rolls"]), _FACTOR_RELOTTERY_LIMIT)
        return refuse()

    use_tp = int(payload.get("use_tp") or 0)
    pool = stamina.TP.spend(full_state, use_tp)
    if pool is None:
        log.warning("factor_lottery refused: %s TP requested, pool has %s",
                    use_tp, stamina.tp_info(full_state).get("current_tp"))
        return refuse()

    ci = career["data"]["chara_info"]
    factors = _career_roll(viewer_id, full_state, ci, career["data"].get("race_history"))
    lottery_id = max(r["lottery_id"] for r in stashed["rolls"]) + 1
    stashed["rolls"].append({"lottery_id": lottery_id, "factors": factors})
    log.info("factor_lottery: roll %s for %s TP (%s rolls now)",
             lottery_id, use_tp, len(stashed["rolls"]))
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_lottery_common": {
                "tp_info": stamina.tp_info(full_state),
                "lottery_remain_num": _FACTOR_RELOTTERY_LIMIT - (len(stashed["rolls"]) - 1),
                "lottery_count": len(stashed["rolls"]),
                "select_lottery_id": stashed["selected_lottery_id"],
                "factor_select_info_array": [
                    {"lottery_id": r["lottery_id"],
                     "factor_info_array": r["factors"]} for r in stashed["rolls"]]}}}


_JP_FACTOR_REROLLS = 3
_JP_REROLL_TP, _JP_REROLL_COIN = 30, 30
_JP_ORDER_COIN = 100
_ORDER_JEWEL, _ORDER_PASS, _ORDER_TICKET = 1, 2, 3
_LOT_TP, _LOT_CAMPAIGN, _LOT_COIN, _LOT_PASS, _LOT_TICKET = 1, 2, 3, 4, 5
LAST_ORDER_KEY = "last_factor_order_group"
SKILL_UPGRADE_KEY = "career_skill_upgrade_request"


def _jp_rerolls_left(stashed: dict) -> int:
    return max(0, _JP_FACTOR_REROLLS - (len(stashed["rolls"]) - 1))


def _jp_next_lottery(full_state: dict, stashed: dict) -> dict:
    left = _jp_rerolls_left(stashed)
    if paid_features.active(full_state, "reroll_pass"):
        return {"request_lottery_type": _LOT_PASS, "need_tp": 0, "need_coin": 0,
                "need_item": 0, "pass_remain_num": left}
    if len(stashed["rolls"]) == 1:
        return {"request_lottery_type": _LOT_TP, "need_tp": _JP_REROLL_TP, "need_coin": 0,
                "need_item": 0, "pass_remain_num": 0}
    if paid_features.item_count(full_state, paid_features.REROLL_TICKET_ITEM) > 0:
        return {"request_lottery_type": _LOT_TICKET, "need_tp": 0, "need_coin": 0,
                "need_item": 1, "pass_remain_num": 0}
    return {"request_lottery_type": _LOT_COIN, "need_tp": 0, "need_coin": _JP_REROLL_COIN,
            "need_item": 0, "pass_remain_num": 0}


def _jp_factor_rolls(stashed: dict) -> list:
    rolls = sorted(stashed["rolls"], key=lambda r: r["lottery_id"])
    ordered = rolls[-1:] + rolls[:-1] if len(rolls) > 1 else rolls
    return [{"lottery_id": r["lottery_id"], "factor_info_array": r["factors"]} for r in ordered]


def _jp_order_info(full_state: dict, stashed: dict | None) -> dict:
    ordered = int((stashed or {}).get("order_group") or 0)
    if paid_features.active(full_state, "order_pass"):
        kind = _ORDER_PASS
    elif paid_features.item_count(full_state, paid_features.ORDER_TICKET_ITEM) > 0:
        kind = _ORDER_TICKET
    else:
        kind = _ORDER_JEWEL
    closed = bool(stashed and stashed.get("order_closed"))
    return {"is_order_enable": not closed, "factor_group_id": ordered,
            "last_factor_group_id": int(full_state.get(LAST_ORDER_KEY) or 0),
            "request_type": kind, "need_coin": _JP_ORDER_COIN, "need_item": 1}


def _jp_order_common(full_state: dict, item_id: int) -> dict:
    return {"coin_info": copy.deepcopy(full_state.get("coin_info_state") or {"fcoin": 0, "coin": 0}),
            "user_item": {"item_id": item_id,
                          "number": paid_features.item_count(full_state, item_id)}}


def _orderable_group(group: int) -> bool:
    return bool(group) and master_data.query_one(
        "SELECT factor_id FROM succession_factor WHERE factor_group_id=? LIMIT 1",
        (group,)) is not None


def _force_factor_group(factors: list, group: int, chara_info: dict) -> list:
    if not group or any(f["factor_id"] // 100 == group for f in factors):
        return factors
    from .. import rating_formula
    score = rating_formula.get_career_score(chara_info)
    if score < 6500:
        star = random.choices((1, 2), (90, 10))[0]
    elif score < 17500:
        star = random.choices((1, 2, 3), (50, 45, 5))[0]
    else:
        star = random.choices((1, 2, 3), (20, 70, 10))[0]
    kinds = (range(1, 6), range(11, 35))
    same = next((k for k in kinds if group in k), None)
    kept = [f for f in factors if not (same and f["factor_id"] // 100 in same)]
    fid = _valid_factor_ids([group * 100 + star])
    return kept + [{"factor_id": x, "level": 0} for x in fid]


def _jp_place_order(full_state: dict, stashed: dict, chara_info: dict, request) -> bool:
    if not isinstance(request, dict) or not int(request.get("factor_group_id") or 0):
        return True
    if stashed.get("order_closed"):
        return True
    group = int(request["factor_group_id"])
    kind = int(request.get("request_type") or _ORDER_JEWEL)
    if not _orderable_group(group):
        log.warning("factor order refused: unknown factor group %s", group)
        return False
    if kind == _ORDER_PASS:
        if not paid_features.active(full_state, "order_pass"):
            log.warning("factor order refused: no 因子指定パス")
            return False
    elif kind == _ORDER_TICKET:
        if not paid_features.spend_item(full_state, paid_features.ORDER_TICKET_ITEM):
            log.warning("factor order refused: no 因子指定チケット")
            return False
    elif not paid_features.spend_coin(full_state, max(_JP_ORDER_COIN, int(request.get("use_coin") or 0))):
        log.warning("factor order refused: %s carats short", _JP_ORDER_COIN)
        return False
    stashed["order_group"] = group
    full_state[LAST_ORDER_KEY] = group
    for roll in stashed["rolls"]:
        roll["factors"] = _force_factor_group(roll["factors"], group, chara_info)
    log.info("factor order: group %s guaranteed (request_type %s)", group, kind)
    return True


def _jp_add_roll(viewer_id, full_state: dict, career: dict, stashed: dict) -> int:
    ci = career["data"]["chara_info"]
    factors = _career_roll(viewer_id, full_state, ci, career["data"].get("race_history"))
    factors = _force_factor_group(factors, int(stashed.get("order_group") or 0), ci)
    lottery_id = max(r["lottery_id"] for r in stashed["rolls"]) + 1
    stashed["rolls"].append({"lottery_id": lottery_id, "factors": factors})
    return lottery_id


def _jp_factor_select(viewer_id, full_state: dict, stashed: dict, payload: dict) -> dict:
    full_state[SKILL_UPGRADE_KEY] = {
        "skill_id_array": list(payload.get("skill_id_array") or []),
        "gain_skill_upgrade_info_array": list(payload.get("gain_skill_upgrade_info_array") or [])}
    stashed["jp_flow"] = True
    career = full_state.get(STATE_KEY)
    ci = career["data"]["chara_info"] if isinstance(career, dict) else {}
    if not _jp_place_order(full_state, stashed, ci, payload.get("factor_order_request")):
        return refuse()
    stashed["order_closed"] = True
    if (payload.get("is_bulk_lottery") and isinstance(career, dict)
            and paid_features.active(full_state, "reroll_pass")):
        while _jp_rerolls_left(stashed) > 0:
            _jp_add_roll(viewer_id, full_state, career, stashed)
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "single_mode_factor_select_common": {
                    "rank_score": stashed["rank_score"], "rank": stashed["rank"],
                    "factor_order_info": _jp_order_info(full_state, stashed),
                    "lottery_remain_num": _jp_rerolls_left(stashed),
                    "lottery_count": len(stashed["rolls"]),
                    "factor_select_info_array": _jp_factor_rolls(stashed),
                    "factor_relottery_info_array": [],
                    "next_lottery_info": _jp_next_lottery(full_state, stashed)},
                "single_mode_factor_order_common": _jp_order_common(
                    full_state, paid_features.ORDER_TICKET_ITEM)}}


def _jp_factor_lottery(viewer_id, full_state: dict, career: dict, stashed: dict,
                       payload: dict) -> dict:
    kind = int(payload.get("request_lottery_type") or 0)
    if _jp_rerolls_left(stashed) <= 0:
        log.warning("factor_lottery refused: no re-rolls left")
        return refuse()
    if kind == _LOT_TP:
        if stamina.TP.spend(full_state, int(payload.get("use_tp") or 0)) is None:
            log.warning("factor_lottery refused: %s TP requested", payload.get("use_tp"))
            return refuse()
    elif kind == _LOT_COIN:
        cost = max(_JP_REROLL_COIN, int(payload.get("use_coin") or 0))
        if not paid_features.spend_coin(full_state, cost):
            log.warning("factor_lottery refused: %s carats requested", cost)
            return refuse()
    elif kind == _LOT_PASS:
        if not paid_features.active(full_state, "reroll_pass"):
            log.warning("factor_lottery refused: no 因子再獲得パス")
            return refuse()
    elif kind == _LOT_TICKET:
        if not paid_features.spend_item(full_state, paid_features.REROLL_TICKET_ITEM):
            log.warning("factor_lottery refused: no 因子再獲得チケット")
            return refuse()
    lottery_id = _jp_add_roll(viewer_id, full_state, career, stashed)
    stashed["jp_flow"] = True
    log.info("factor_lottery (JP type %s): roll %s", kind, lottery_id)
    state_store.save_state(viewer_id, full_state)
    return jp_factor_lottery_reply(full_state, stashed)


def jp_factor_lottery_reply(full_state: dict, stashed: dict) -> dict:
    common = _jp_order_common(full_state, paid_features.REROLL_TICKET_ITEM)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_lottery_common": {
                "tp_info": stamina.tp_info(full_state),
                "coin_info": common["coin_info"],
                "user_item": common["user_item"],
                "lottery_remain_num": _jp_rerolls_left(stashed),
                "lottery_count": len(stashed["rolls"]),
                "factor_select_info_array": _jp_factor_rolls(stashed),
                "factor_relottery_info_array": [],
                "next_lottery_info": _jp_next_lottery(full_state, stashed)}}}


def handle_factor_order_load(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    stashed = full_state.get("career_factor_roll")
    left = _jp_rerolls_left(stashed) + 1 if stashed and stashed.get("rolls") else _JP_FACTOR_REROLLS + 1
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_order_load_common": {
                "factor_order_info": _jp_order_info(full_state, stashed), "lottery_remain_num": left}}}


def handle_factor_relottery_end(payload: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


def _apply_short_cut_state(viewer_id, response: dict) -> None:
    full_state = state_store.get_state(viewer_id) or {}
    if SHORT_CUT_KEY not in full_state:
        return
    value = int(full_state[SHORT_CUT_KEY] or 0)
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    for holder in (data, data.get("single_mode_load_common")):
        if isinstance(holder, dict) and isinstance(holder.get("chara_info"), dict):
            holder["chara_info"]["short_cut_state"] = value


def handle_change_short_cut(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/change_short_cut", full_state)

    full_state[SHORT_CUT_KEY] = int(payload.get("short_cut_state") or 0)
    chara_info = career["data"]["chara_info"]
    chara_info["short_cut_state"] = full_state[SHORT_CUT_KEY]
    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    for key in ("race_condition_array", "race_start_info", "race_running_style",
                "event_effected_factor_array", "not_down_parameter_info"):
        data.pop(key, None)
    state_store.save_state(viewer_id, full_state)
    return response


_ALARM_CLOCK_ITEM = 95
_FREE_CONTINUE_ALLOWANCE = 1
_PAID_CONTINUE_ALLOWANCE = 3
CONTINUE_BONUS_HOOKS: list = []


def _paid_continue_allowance(full_state: dict) -> int:
    return _PAID_CONTINUE_ALLOWANCE + sum(h(full_state) for h in CONTINUE_BONUS_HOOKS)


def _alarm_clock_stock(full_state: dict) -> int:
    for it in full_state.get("item_list_state") or []:
        if it.get("item_id") == _ALARM_CLOCK_ITEM:
            return int(it.get("number") or 0)
    return 0


def _spend_alarm_clock(full_state: dict) -> bool:
    items = full_state.setdefault("item_list_state", [])
    for it in items:
        if it.get("item_id") == _ALARM_CLOCK_ITEM and int(it.get("number") or 0) > 0:
            it["number"] = int(it["number"]) - 1
            return True
    return False


def _continue_counts(full_state: dict, ctx: dict | None) -> dict:
    ctx = ctx or {}
    free_left = max(0, _FREE_CONTINUE_ALLOWANCE - int(ctx.get("continue_free_used") or 0))
    paid_left = max(0, _paid_continue_allowance(full_state) - int(ctx.get("continue_paid_used") or 0))
    return {
        "available_free_continue_num": free_left,
        "available_continue_num": min(paid_left, _alarm_clock_stock(full_state)),
        "free_continue_num": _FREE_CONTINUE_ALLOWANCE,
    }


def handle_continue(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY)
    chara_info = _career_chara_info(viewer_id)
    if not ctx or chara_info is None or ctx.get("won"):
        return refuse()
    continue_type = payload.get("continue_type")
    counts = _continue_counts(full_state, ctx)
    if continue_type == 2:
        if counts["available_continue_num"] <= 0 or not _spend_alarm_clock(full_state):
            return refuse()
        ctx["continue_paid_used"] = int(ctx.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return refuse()
        ctx["continue_free_used"] = int(ctx.get("continue_free_used") or 0) + 1
    career = full_state.get(STATE_KEY)
    if ctx.get("end_committed") and isinstance(career, dict):
        cd = career["data"]
        hist = cd.get("race_history") or []
        if hist and hist[-1].get("program_id") == ctx.get("program_id"):
            hist.pop()
        ci_live = cd["chara_info"]
        ci_live["fans"] = max(0, (ci_live.get("fans") or 0)
                              - (ctx.get("gained_fans_committed") or 0))
        ci_live["playing_state"] = 2
        ctx.pop("pending_reward", None)
        ctx.pop("gained_fans_committed", None)
        ctx["end_committed"] = False
        chara_info = copy.deepcopy(ci_live)
    race_info = _race_info_for_program(ctx.get("program_id")) or {}
    sim = None
    if race_info.get("race_instance_id"):
        sim = _simulate_career_race(
            chara_info, viewer_id, race_info["race_instance_id"],
            program_id=race_info.get("program_id"),
            with_happy_meek=_meek_in_finals(full_state, chara_info,
                                            ctx.get("finals_round")),
            happy_meek_level=_happy_meek_finals_level(full_state),
            finals_rivals=_scenario_finals_rivals(
                chara_info, ctx.get("finals_round")))
    prev_finish = ctx.get("player_finish_order")
    if sim:
        rsi = ctx.get("race_start_info") or {}
        rsi["race_horse_data"] = sim["race_horse_data"]
        rsi["random_seed"] = sim["random_seed"]
        ctx.update(race_start_info=rsi, race_scenario=sim["race_scenario"],
                   player_finish_order=sim["player_finish_order"],
                   player_finish_time=sim.get("player_finish_time"),
                   player_popularity=sim.get("player_popularity"),
                   rival_finish_orders=sim.get("rival_finish_orders"),
                   beat_happy_meek=sim.get("beat_happy_meek", False))
    log.info("continue: type %s, race %s -> %s (seed %s, was finish %s)",
             continue_type, ctx.get("program_id"),
             "re-simulated -> finish %s" % ctx.get("player_finish_order") if sim
             else "NOT re-simulated (no simulation available)",
             (sim or {}).get("random_seed"), prev_finish)
    ctx["continue_used"] = ctx.get("continue_used", 0) + 1
    full_state[RACE_CTX_KEY] = ctx
    career = full_state.get(STATE_KEY)
    data = {}
    race_ci = copy.deepcopy(chara_info)
    race_ci["playing_state"] = 2
    race_ci["state"] = 0
    race_ci["race_program_id"] = ctx.get("program_id")
    data["chara_info"] = race_ci
    if ctx.get("race_start_info"):
        data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
    data["unchecked_event_array"] = []
    if isinstance(career, dict) and isinstance(career["data"].get("home_info"), dict):
        career["data"]["home_info"].update(_continue_counts(full_state, ctx))
        data["home_info"] = copy.deepcopy(career["data"]["home_info"])
    if continue_type == 2:
        data["user_item"] = {"item_id": _ALARM_CLOCK_ITEM,
                             "number": _alarm_clock_stock(full_state)}
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def _apply_camp_rest(chara_info: dict, current_vital: int, group: int = _CAMP_OUTING_GROUP,
                     energy: int = _CAMP_REST_ENERGY):
    chara_info["vital"] = min(chara_info.get("max_vital", 100), current_vital + energy)
    chara_info["motivation"] = min(_MOTIVATION_MAX,
                                   chara_info.get("motivation", 3) + _CAMP_REST_MOOD)
    return group


def _apply_recreation(chara_info: dict, current_vital: int):
    mv = chara_info.get("max_vital", 100)
    capped = False

    def add_energy(n):
        nonlocal capped
        capped = current_vital >= mv
        chara_info["vital"] = min(mv, current_vital + n)

    def add_mood(n):
        chara_info["motivation"] = min(_MOTIVATION_MAX, chara_info.get("motivation", 3) + n)

    r = random.random()
    if r < 0.35:
        add_mood(2)
        return _OUTING_KARAOKE, capped
    if r < 0.65:
        add_mood(1); add_energy(10)
        return _OUTING_RIVERBANK, capped
    if r < 0.85:
        add_mood(1); add_energy(10)
    elif r < 0.95:
        add_mood(1); add_energy(20)
    else:
        add_mood(1); add_energy(30)
    return _OUTING_SHRINE, capped


def _facility_failure_cards(chara_info: dict, partners) -> list:
    pos_to_sid = {c.get("position"): c.get("support_card_id")
                  for c in chara_info.get("support_card_array") or []}
    levels = _deck_support_card_levels(chara_info)
    return [(pos_to_sid[p], levels.get(pos_to_sid[p])) for p in partners or ()
            if pos_to_sid.get(p)]


def _roll_training_failure(command_id, current_vital: int, conditions=None, deck=(),
                           in_facility_ids=(), support_card_levels: dict = None,
                           shown_rate=None):
    stat = _COMMAND_TRAINED_STAT.get(command_id)
    if stat is None:
        return False, False
    if training_formula.training_failure_zero_proc(in_facility_ids, support_card_levels):
        return False, False
    fail_pct = shown_rate if shown_rate is not None else training_formula.failure_rate(
        current_vital, command_id=command_id, conditions=conditions, deck=deck)
    if shown_rate is None:
        fail_pct = tuning.failure_pct(fail_pct)
    if training_formula.forced_failure_rate() is not None:
        fail_pct = training_formula.forced_failure_rate()
    if fail_pct <= 0 or random.random() * 100 >= fail_pct:
        return False, False
    return True, fail_pct >= 80


def _simulate_exec_command(career_state: dict, payload: dict, unlocked_npcs=(),
                           training_bonus: dict = None,
                           friendship_bonus: int = 0,
                           specialty_bonus: int = 0,
                           full_state: dict = None) -> dict:
    template_pair = fixtures.first("single_mode/exec_command") or fixtures.first("single_mode_team/exec_command")
    response = template_pair.response_copy()
    chara_info = copy.deepcopy(career_state["data"]["chara_info"])
    if chara_info.get("scenario_id") != 2 and "team_data_set" in response.get("data", {}):
        del response["data"]["team_data_set"]
    support_card_levels = _support_card_levels(full_state) if full_state else {}

    command_id = payload.get("command_id")
    command_group_id = payload.get("command_group_id")
    command_type = payload.get("command_type")

    preview = _preview_gains_for(career_state, command_id)
    if preview and full_state is not None and command_type == 1:
        bonus = scenarios.for_chara(
            career_state.get("data", {}).get("chara_info")).training_award_bonus(
                full_state, career_state.get("data", {}).get("chara_info"), command_id)
        if bonus:
            preview = _merge_preview_params(preview, bonus)
    current_vital = payload.get("current_vital", chara_info.get("vital", 100))

    base_command = scenarios.for_chara(chara_info).base_command(command_id)
    pos_to_sid = {c["position"]: c["support_card_id"] for c in chara_info.get("support_card_array", [])}
    in_facility_ids = [pos_to_sid[p] for p in _partners_in_command(career_state, command_id)
                       if p in pos_to_sid]
    shown = next((c.get("failure_rate") for c in
                   (career_state.get("data", {}).get("home_info", {}).get("command_info_array") or [])
                   if c.get("command_id") == command_id and c.get("command_type") == 1), None)
    failed, worst = _roll_training_failure(
        base_command, current_vital, conditions=chara_info.get("chara_effect_id_array"),
        deck=_career_deck(chara_info), in_facility_ids=in_facility_ids,
        support_card_levels=support_card_levels,
        shown_rate=int(shown) if shown is not None and current_vital == chara_info.get("vital", current_vital) else None)

    outing_group = None
    data_infirmary = False
    slacked = False
    rest_outcome = None
    trip_stories = ()
    hint_reveals = []
    npc_partners = []
    pal_partners = []
    is_rest = command_id in _REST_COMMANDS or command_type == 7
    is_recreation = command_group_id in _RECREATION_GROUPS or command_type == 3
    is_partner_outing = command_group_id == _OUTING_COMMAND_GROUP
    scen_rest = scenarios.for_chara(chara_info)
    rest_group = scen_rest.camp_rest_group
    is_camp_rest = (command_group_id in (_CAMP_OUTING_GROUP, rest_group)
                    or command_id in (_CAMP_OUTING_GROUP, rest_group)
                    or (is_recreation and scen_rest.serves_camp_rest(payload.get("current_turn"))))
    not_up = []
    scenario_params = (scenarios.for_chara(chara_info).scenario_command(
        full_state, chara_info, payload) if full_state is not None else None)
    if scenario_params is not None:
        _apply_preview_gains(chara_info, scenario_params, payload, not_up)
    elif failed and base_command == 106:
        if current_vital >= chara_info.get("max_vital", 100):
            not_up.append(6)
        chara_info["vital"] = min(chara_info.get("max_vital", 100), current_vital + 5)
    elif failed:
        pass
    elif is_camp_rest:
        if current_vital >= chara_info.get("max_vital", 100):
            not_up.append(6)
        outing_group = _apply_camp_rest(chara_info, current_vital, rest_group,
                                        scen_rest.camp_rest_energy)
    elif is_partner_outing:
        outing_group = _OUTING_COMMAND_GROUP
    elif is_rest and _forced_rest_stories(chara_info):
        outing_group = 701
        trip_stories = _forced_rest_stories(chara_info)
    elif is_rest:
        rest_outcome = _roll_rest_outcome()
        outing_group = 701
    elif is_recreation:
        outing_group, energy_capped = _apply_recreation(chara_info, current_vital)
        if energy_capped:
            not_up.append(6)
    elif command_id == _INFIRMARY_COMMAND_ID or command_type == _INFIRMARY_COMMAND_TYPE:
        data_infirmary = True
        outing_group = _INFIRMARY_COMMAND_ID
    elif conditions.skips_training(chara_info):
        chara_info["vital"] = max(0, current_vital - 5)
        applied = False
        slacked = True
        log.info("slacker: trainee skipped training this turn")
    else:
        applied = _apply_preview_gains(chara_info, preview, payload, not_up)
        if applied:
            _note_capped_facility_stats(chara_info, command_id, not_up)
        if applied:
            fac_index = training_formula._facility_index_for(command_id)
            gl_supporters = set(_npc_supporters(full_state or {}, chara_info))
            trained_here = _partners_in_command(career_state, command_id)
            deck_levels = _deck_support_card_levels(chara_info)
            training_bond_up = 0
            for p, card_sid in pos_to_sid.items():
                for v0, v1, *_ in training_formula.unique_params(
                        card_sid, training_formula.EFFECT_TRAINING_BOND_UP,
                        deck_levels.get(card_sid) or 0):
                    training_bond_up = max(training_bond_up,
                                           v1 if p in trained_here else v0)
            if full_state is not None:
                shared = {}
                for p in trained_here:
                    card_sid = pos_to_sid.get(p)
                    for etype, amount, *_ in training_formula.unique_params(
                            card_sid, training_formula.EFFECT_SHARED_NEXT_TURN,
                            deck_levels.get(card_sid) or 0) if card_sid else ():
                        if etype != training_formula.EFFECT_SPECIALTY_PRIORITY:
                            continue
                        for other in trained_here:
                            if other != p and other in pos_to_sid:
                                shared[str(other)] = shared.get(str(other), 0) + amount
                full_state[SHARED_NEXT_TURN_KEY] = {
                    "turn": int(chara_info.get("turn") or 0) + 1, "positions": shared}
            for pos in trained_here:
                if pos in gl_supporters:
                    continue
                if pos in _NPC_TARGET_IDS:
                    _ensure_npc_eval(chara_info, pos)
                    gain = _NPC_BOND_GAIN
                else:
                    gain = _BOND_GAIN_PER_TRAINING + training_bond_up + (
                        scenarios.for_chara(chara_info).training_bond_bonus(full_state, chara_info)
                        if full_state is not None else 0)
                gain = conditions.bond_gain(chara_info, gain)
                bond_before = _bond_of(chara_info, pos)
                _set_bond(chara_info, pos, min(_BOND_MAX, bond_before + gain))
                sid = pos_to_sid.get(pos)
                if (full_state is not None and sid and bond_before >= 80
                        and fac_index is not None):
                    card = training_formula.CARD_BY_ID.get(str(sid))
                    ctype = card.get("type") if card else None
                    card_type_num = (training_formula.CARD_TYPE_NAMES.index(ctype)
                                     if ctype in training_formula.CARD_TYPE_NAMES else -1)
                    unique_lv = support_card_levels.get(sid)
                    if (card_type_num == fac_index and unique_lv
                            and training_formula.unique_effect(
                                sid, training_formula.EFFECT_FRIENDSHIP_STACKING,
                                level=unique_lv)):
                        stacks = full_state.setdefault(FRIENDSHIP_STACK_KEY, {})
                        stacks[str(sid)] = stacks.get(str(sid), 0) + 1
            appraisers = set(single_mode_events.APPRAISAL_EVENTS) | set(
                scenarios.for_chara(chara_info).appraisal_events() or ())
            npc_partners = [p for p in _partners_in_command(career_state, command_id)
                            if p in appraisers]
            trained_positions = set(_partners_in_command(career_state, command_id))
            pal_partners = [(p, c) for p, c in _deck_pal_cards(chara_info)
                            if p in trained_positions]
            hint_reveals = _build_hint_reveals(chara_info, career_state, command_id, payload.get("current_turn"))
            if full_state is not None:
                hint_reveals = scenarios.for_chara(chara_info).hint_reveals(
                    full_state, chara_info, command_id, hint_reveals)
        else:
            chara_info["vital"] = max(0, current_vital - 20)


    chara_info["turn"] = int(chara_info.get("turn") or 0) + 1

    data = response["data"]
    data["chara_info"] = chara_info
    data["unchecked_event_array"] = []
    _note_not_up(data, not_up)
    data["command_result"] = {"command_id": outing_group or command_id or command_group_id or 0,
                              "sub_id": (int(payload.get("select_id") or 1)
                                         if outing_group == _OUTING_COMMAND_GROUP else 1),
                              "result_state": 1 if failed else 2}
    data["_training_failed"] = failed and base_command != 106
    data["_training_worst"] = worst
    data["_rest_outcome"] = rest_outcome
    data["_trip_stories"] = trip_stories
    data["_hint_reveals"] = hint_reveals
    data["_npc_partners"] = npc_partners
    data["_trained_command"] = base_command if npc_partners else None
    data["_pal_partners"] = pal_partners
    data["_pal_command"] = command_id
    data["_infirmary"] = data_infirmary
    data["_slacked"] = slacked
    career_data = career_state["data"]
    _record_training_tally(career_data, base_command, failed)
    at_camp = (command_id in _CAMP_BASE_BY_ID or _is_camp_turn(chara_info["turn"] - 1)
               or scenarios.for_chara(chara_info).base_command(command_id) != command_id)
    if (not failed and not at_camp
            and scenarios.for_chara(chara_info).levels_facilities_by_training()):
        data["_facility_leveled"] = (
            base_command if _record_facility_train(career_data, base_command, chara_info)
            else False)
    career_home = career_data.get("home_info")
    if isinstance(career_home, dict):
        _refresh_command_info(chara_info, career_home, turn=chara_info["turn"],
                              unlocked_npcs=unlocked_npcs, facility_levels=_facility_levels(career_data),
                              race_history=career_data.get("race_history", []),
                              training_bonus=training_bonus,
                              friendship_bonus=friendship_bonus,
                              specialty_bonus=specialty_bonus,
                              support_card_levels=support_card_levels,
                              friendship_stacks=_friendship_stacks(full_state),
                              full_state=full_state)
        if isinstance(data.get("home_info"), dict):
            data["home_info"]["command_info_array"] = copy.deepcopy(career_home["command_info_array"])
    career_data["chara_info"] = chara_info
    return response


_CAREER_END_TURN = 78

_CHARA_LIST_TEMPLATE = {"training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                        "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0}


def _upsert_chara_collection(full_state: dict, final_chara_info: dict) -> None:
    card_id = (final_chara_info or {}).get("card_id")
    if not card_id:
        return
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    chara_id = row["chara_id"] if row else card_id // 100
    charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
    if not isinstance(charas, list):
        return
    fans = final_chara_info.get("fans", 0) or 0
    grade = final_chara_info.get("chara_grade", 0) or 0
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    if entry:
        entry["training_num"] = (entry.get("training_num", 0) or 0) + 1
        entry["fan"] = (entry.get("fan", 0) or 0) + fans
        entry["max_grade"] = max(entry.get("max_grade", 0) or 0, grade)
    else:
        charas.append(dict(_CHARA_LIST_TEMPLATE, chara_id=chara_id,
                           training_num=1, fan=max(1, fans), max_grade=grade))


def handle_finish(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career_state = full_state.get(STATE_KEY)
    picked = payload.get("factor_lottery_id")

    roster = None
    new_record = None
    final_chara_info = None
    love_point_info = None
    new_chara_profile_array = []
    scenario_record_rewards = []
    training_challenge_result = None
    story_event_info = None
    factor_research_data_set = None
    if isinstance(career_state, dict):
        final_chara_info = career_state.get("data", {}).get("chara_info")
        turn = (final_chara_info or {}).get("turn", 0) or 0
        if turn >= _CAREER_END_TURN or (final_chara_info or {}).get("state") in (2, 3):
            try:
                start_chara = full_state.get(START_CHARA_STATE_KEY) or {}
                roster = trained_chara._get_or_seed_roster(viewer_id)
                full_state = state_store.get_state(viewer_id)
                career_state = full_state[STATE_KEY]
                stashed_roll = full_state.pop("career_factor_roll", None)
                if (isinstance(stashed_roll, dict) and stashed_roll.get("jp_flow") and picked
                        and any(r.get("lottery_id") == int(picked)
                                for r in stashed_roll.get("rolls") or ())):
                    stashed_roll["selected_lottery_id"] = int(picked)
                new_record = trained_chara.build_trained_chara_from_career(
                    viewer_id, career_state, start_chara, roster,
                    factor_info_array=_selected_factors(stashed_roll),
                    full_state=full_state)
                directory_before = archive_directory.pool(full_state, roster)
                roster = roster + [new_record]
                cap = int(config.get("veteran_capacity",
                                     trained_chara.VETERAN_CAPACITY) or trained_chara.VETERAN_CAPACITY)
                while len(roster) > cap:
                    victim = next((i for i, r in enumerate(roster)
                                   if r is not new_record and not r.get("is_locked")), None)
                    if victim is None:
                        break
                    archive_directory.retire(full_state, roster,
                                             [roster[victim].get("trained_chara_id")])
                    dropped = roster.pop(victim)
                    log.info("veteran box over capacity (%s): dropped oldest "
                             "unlocked trained_chara %s", cap,
                             dropped.get("trained_chara_id"))
                full_state[trained_chara.ROSTER_KEY] = roster
                archive_directory.prune_hidden(full_state, roster)
                scenario_record_rewards = archive_directory.settle_scenario_record(
                    full_state, directory_before, new_record)
                _upsert_chara_collection(full_state, final_chara_info)
                love_chara_id = bond.chara_id_for_card(
                    (final_chara_info or {}).get("card_id") or 0)
                rank_id = (new_record or {}).get("rank") or 0
                if love_chara_id and rank_id:
                    love_point_info, new_chara_profile_array = bond.grant(
                        full_state, love_chara_id, bond.career_love_points(rank_id))
                state_store.save_state(viewer_id, full_state)
            except Exception:
                log.exception("finish: building the legacy trained_chara failed; "
                              "no legacy added for viewer %s", viewer_id)
                roster = new_record = None

            try:
                full_state = state_store.get_state(viewer_id) or {}
                career_data = (career_state or {}).get("data") or {}
                facts = epithets.build_facts(career_data, final_chara_info, full_state)
                earned = epithets.award_for_career(
                    full_state, career_data, final_chara_info, facts=facts)
                if earned:
                    if isinstance(new_record, dict):
                        existing = list(new_record.get("nickname_id_array") or ())
                        new_record["nickname_id_array"] = existing + [
                            i for i in earned if i not in existing]
                        roster_now = full_state.get(trained_chara.ROSTER_KEY)
                        if isinstance(roster_now, list):
                            for rec in roster_now:
                                if rec.get("trained_chara_id") == new_record.get("trained_chara_id"):
                                    rec["nickname_id_array"] = new_record["nickname_id_array"]
                                    break
                    state_store.save_state(viewer_id, full_state)
            except Exception:
                log.exception("finish: epithet award failed for viewer %s", viewer_id)

            if isinstance(new_record, dict):
                try:
                    from . import training_challenge
                    full_state = state_store.get_state(viewer_id) or {}
                    training_challenge_result = training_challenge.settle(full_state, new_record, viewer_id)
                    state_store.save_state(viewer_id, full_state)
                except Exception:
                    log.exception("finish: training challenge settlement failed for viewer %s", viewer_id)
                try:
                    from . import story_event
                    full_state = state_store.get_state(viewer_id) or {}
                    story_event_info = story_event.settle_career(full_state, new_record, viewer_id, start_chara)
                    state_store.save_state(viewer_id, full_state)
                except Exception:
                    log.exception("finish: story event settlement failed for viewer %s", viewer_id)
                try:
                    from . import factor_research
                    full_state = state_store.get_state(viewer_id) or {}
                    factor_research_data_set = factor_research.settle_career(
                        full_state, new_record, start_chara,
                        (final_chara_info or {}).get("state") != 2)
                    state_store.save_state(viewer_id, full_state)
                except Exception:
                    log.exception("finish: factor research settlement failed for viewer %s", viewer_id)

    clear_active_career(viewer_id)

    template = fixtures.first("single_mode_team/finish")
    response = template.response_copy() if template is not None else {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {"single_mode_finish_common": {}},
    }
    if isinstance(response.get("data_headers"), dict):
        response["data_headers"]["notifications"] = {}
    smfc = response.get("data", {}).get("single_mode_finish_common")
    if isinstance(smfc, dict):
        smfc["trained_chara"] = copy.deepcopy(
            roster if roster is not None
            else trained_chara._get_or_seed_roster(viewer_id))
        smfc["trained_chara_id"] = (new_record["trained_chara_id"]
                                    if new_record is not None else 0)
        if final_chara_info is not None:
            smfc["chara_info"] = copy.deepcopy(final_chara_info)
        saved = state_store.get_state(viewer_id) or {}
        smfc["love_point_info"] = love_point_info or _unchanged_love_point_info(
            saved, final_chara_info)
        love_chara = smfc["love_point_info"].get("chara_id") or 0
        if not love_chara and final_chara_info is not None:
            love_chara = (final_chara_info.get("card_id") or 0) // 100
        smfc["update_user_chara_info"] = _love_chara_entry(saved, love_chara)
        smfc["new_chara_profile_array"] = new_chara_profile_array
        horses = archive_directory.pool(saved, smfc["trained_chara"])
        card_id = ((new_record or {}).get("card_id")
                   or (final_chara_info or {}).get("card_id") or 0)
        smfc["directory_card_array"] = (
            archive_directory.card_array(viewer_id, horses, card_id) if card_id else [])
        smfc["directory_ranking"] = (
            archive_directory.ranking_of(horses, new_record["trained_chara_id"])
            if new_record is not None else 0)
        smfc["directory_card_num"] = archive_directory.entry_count(horses)
        records = []
        if new_record is not None:
            records = archive_directory.scenario_top(
                viewer_id, horses, new_record.get("scenario_id"))
            if not any(r["trained_chara"].get("trained_chara_id")
                       == new_record["trained_chara_id"] for r in records):
                records = []
        smfc["scenario_record_array"] = records
        smfc["scenario_record_reward_array"] = scenario_record_rewards
        smfc["transfer_event_info"] = transfer.transfer_event_info(viewer_id)
        smfc["training_challenge_result"] = training_challenge_result
        smfc["story_event_info"] = story_event_info
        from . import map_event
        map_state = state_store.get_state(viewer_id) or {}
        map_gauge = map_event.gauge_info(map_state, "singlemode")
        if map_gauge is not None:
            smfc["map_event_gauge_info"] = map_gauge
            state_store.save_state(viewer_id, map_state)
        if region.PROCESS_REGION == "jp":
            smfc["factor_research_data_set"] = factor_research_data_set
    return response


def _love_chara_entry(full_state: dict, chara_id: int):
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if not chara_id or not isinstance(charas, list):
        return None
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    return dict(entry) if entry else None


def _unchanged_love_point_info(full_state: dict, final_chara_info) -> dict:
    chara_id = (bond.chara_id_for_card((final_chara_info or {}).get("card_id") or 0)
                if final_chara_info else 0)
    entry = _love_chara_entry(full_state, chara_id) or {}
    love = entry.get("love_point") or 0
    pool = entry.get("love_point_pool") or 0
    return {"chara_id": chara_id, "love_point_before": love, "love_point_after": love,
            "love_point_pool_before": pool, "love_point_pool_after": pool}


def handle_race_analyze(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    program_id = payload.get("program_id")
    horses = []
    if ctx and (program_id in (None, 0) or ctx.get("program_id") == program_id):
        horses = copy.deepcopy((ctx.get("race_start_info") or {}).get("race_horse_data") or [])
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"race_horse_data_array": horses}}
