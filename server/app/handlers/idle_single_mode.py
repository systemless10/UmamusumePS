
from __future__ import annotations

import copy
import functools
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from .. import master_data
from .. import region
from .. import state as state_store
from . import cards, collection, registry, single_mode_team, stamina, trained_chara
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

IDLE_RUN_KEY = "idle_single_mode_run"

IDLE_LAST_START_INFO_KEY = "idle_single_mode_last_start_info"

_CLIENT_TZ = (timezone(timedelta(hours=9)) if region.PROCESS_REGION == "jp"
              else timezone.utc)
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _time_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, _CLIENT_TZ).strftime(_TIME_FMT)


_DEFAULT_START_INFO = {
    "training_policy_ground_type": 1,
    "training_policy_param_rate_set_id": 3,
    "priority_skill_array": [],
}

_IDLE_READY_SECONDS_DEFAULT = 10
_READY_TOLERANCE_SECONDS = 2.0

_MAX_STAT = 999_999
_MAX_SP = 999_999
_MAX_GRADE = 10
_FINISHED_TURN = single_mode_team._CAREER_END_TURN
_CARD_HINT_LEVEL_CAP = 3

_STATS = ("speed", "stamina", "power", "guts", "wiz")

_PRE_START_CAPTURE = (Path(__file__).resolve().parents[2] / "fixtures"
                     / "captures" / "20260818_122351"
                     / "0027_idle_single_mode_pre_start.json")


def _idle_ready_seconds() -> float:
    return float(config.get("idle_single_mode_seconds", _IDLE_READY_SECONDS_DEFAULT) or 0)


@functools.lru_cache(maxsize=1)
def _default_reserved_race_info() -> dict:
    try:
        doc = json.loads(_PRE_START_CAPTURE.read_text(encoding="utf-8"))
        return copy.deepcopy(doc["response"]["data"]["reserved_race_info"])
    except Exception:
        log.warning("idle_single_mode: pre_start capture missing at %s -- "
                    "serving an empty deck agenda", _PRE_START_CAPTURE)
        return {"default_deck_num": 0, "needs_default_confirm": 0,
                "reserved_race_array": []}


def _envelope(data: dict | None = None) -> dict:
    base = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        base["data"] = data
    return base


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _learnable_skill_rows(card_id):
    ids = set()
    if card_id:
        row = master_data.query_one(
            "SELECT available_skill_set_id FROM card_data WHERE id=?", (card_id,))
        set_id = row["available_skill_set_id"] if row else None
        if set_id:
            for r in master_data.query(
                    "SELECT skill_id FROM available_skill_set WHERE available_skill_set_id=?",
                    (set_id,)):
                ids.add(r["skill_id"])
    for r in master_data.query("SELECT id FROM skill_data WHERE is_general_skill=1"):
        ids.add(r["id"])
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return master_data.query(
        f"SELECT id, group_id, rarity FROM skill_data WHERE id IN ({placeholders})",
        tuple(ids))


def _grant_all_skills(chara_info: dict) -> list[int]:
    rows = _learnable_skill_rows(chara_info.get("card_id"))
    ids = sorted({r["id"] for r in rows})
    skills = chara_info.setdefault("skill_array", [])
    by_id = {s.get("skill_id"): s for s in skills if isinstance(s, dict)}
    for sid in ids:
        if sid not in by_id:
            skills.append({"skill_id": sid, "level": 1})
    single_mode_team._grant_unique_skill(chara_info)
    single_mode_team._level_unique_skill(chara_info, by=single_mode_team._UNIQUE_SKILL_MAX_LEVEL)
    return ids


def _persist_card_hints(full_state: dict, card_id, skill_ids: list[int]) -> None:
    if not card_id or not skill_ids:
        return
    entry = cards._card_entry(full_state, card_id)
    if entry is None:
        return
    skills = entry.setdefault("skill_data_array", [])
    by_id = {s.get("skill_id"): s for s in skills if isinstance(s, dict)}
    for sid in skill_ids:
        existing = by_id.get(sid)
        if existing is None:
            skills.append({"skill_id": sid, "level": _CARD_HINT_LEVEL_CAP})
        else:
            existing["level"] = max(existing.get("level", 0) or 0, _CARD_HINT_LEVEL_CAP)


def _max_out_career(chara_info: dict) -> None:
    for stat in _STATS:
        chara_info[stat] = _MAX_STAT
        cap_key = f"max_{stat}"
        if (chara_info.get(cap_key) or 0) < _MAX_STAT:
            chara_info[cap_key] = _MAX_STAT
    chara_info["skill_point"] = _MAX_SP
    chara_info["vital"] = chara_info.get("max_vital", 100)
    chara_info["motivation"] = 5
    chara_info["turn"] = _FINISHED_TURN
    chara_info["state"] = 2
    chara_info["playing_state"] = 1
    chara_info["chara_grade"] = _MAX_GRADE
    chara_info["fans"] = trained_chara.MAXED_VETERAN_FAN_CEILING


@functools.lru_cache(maxsize=1)
def _all_program_ids() -> tuple:
    return tuple(r["id"] for r in master_data.query("SELECT id FROM single_mode_program"))


def _populate_full_race_history(career_data: dict) -> None:
    history = career_data.setdefault("race_history", [])
    already = {h.get("program_id") for h in history}
    for program_id in _all_program_ids():
        if program_id in already:
            continue
        history.append({
            "turn": _FINISHED_TURN, "program_id": program_id,
            "weather": 1, "ground_condition": 1, "running_style": 2,
            "result_rank": 1, "frame_order": 1, "npc_count": 0,
        })


@registry.endpoint("idle_single_mode/pre_start")
def handle_pre_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    last = full_state.get(IDLE_LAST_START_INFO_KEY) or _DEFAULT_START_INFO
    return _envelope({
        "reserved_race_info": _reserved_race_info(full_state),
        "last_idle_single_mode_start_info": copy.deepcopy(last),
    })


@registry.endpoint("idle_single_mode/start")
def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    common = payload.get("single_mode_start_request_common") or {}
    start_chara = common.get("start_chara") or {}
    start_info = payload.get("start_info") or {}

    single_mode_team.handle_start({"viewer_id": viewer_id, "start_chara": start_chara})

    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(single_mode_team.STATE_KEY)
    if not isinstance(career, dict):
        return _envelope({})

    turn1_chara_info = copy.deepcopy(career["data"]["chara_info"])

    final_ci = career["data"]["chara_info"]
    _max_out_career(final_ci)
    _populate_full_race_history(career["data"])
    skill_ids = _grant_all_skills(final_ci)
    _persist_card_hints(full_state, final_ci.get("card_id"), skill_ids)

    now = time.time()
    ready_at = now + _idle_ready_seconds()
    start_time, end_time = _time_str(now), _time_str(ready_at)
    full_state[IDLE_RUN_KEY] = {"ready_at": ready_at,
                                "start_time": start_time,
                                "end_time": end_time}
    full_state[IDLE_LAST_START_INFO_KEY] = {
        "training_policy_ground_type": start_info.get(
            "training_policy_ground_type", _DEFAULT_START_INFO["training_policy_ground_type"]),
        "training_policy_param_rate_set_id": start_info.get(
            "training_policy_param_rate_set_id",
            _DEFAULT_START_INFO["training_policy_param_rate_set_id"]),
        "priority_skill_array": start_info.get("priority_skill_array", []),
    }

    stamina.spend_career_tp(viewer_id, full_state, start_chara,
                            common.get("use_tp"))
    tp_state = stamina.TP.read(full_state)

    state_store.save_state(viewer_id, full_state)

    return _envelope({
        "progress_info": {
            "chara_info": turn1_chara_info,
            "start_time": start_time,
            "end_time": end_time,
            "dress_id": 0,
        },
        "tp_info": dict(tp_state),
        "user_item_array": [],
        "mission_list": [],
        "story_event_mission_list": [],
        "story_event_chara_bonus_list": [],
        "add_trained_chara_array": [],
    })


def _build_result(viewer_id) -> dict | None:
    full_state = state_store.get_state(viewer_id) or {}
    run = full_state.get(IDLE_RUN_KEY)
    career = full_state.get(single_mode_team.STATE_KEY)
    if not run or not isinstance(career, dict):
        return None
    if time.time() < float(run.get("ready_at") or 0) - _READY_TOLERANCE_SECONDS:
        return None

    ci = copy.deepcopy(career["data"]["chara_info"])

    def _zero_gain_block() -> dict:
        z = {"sign": 0, "value": 0}
        return {
            "speed": dict(z), "stamina": dict(z), "power": dict(z),
            "wiz": dict(z), "guts": dict(z),
            "max_speed": 0, "max_stamina": 0, "max_power": 0, "max_wiz": 0, "max_guts": 0,
            "proper_distance_short": 0, "proper_distance_mile": 0,
            "proper_distance_middle": 0, "proper_distance_long": 0,
            "proper_running_style_nige": 0, "proper_running_style_senko": 0,
            "proper_running_style_sashi": 0, "proper_running_style_oikomi": 0,
            "proper_ground_turf": 0, "proper_ground_dirt": 0,
            "skill_point": 0, "skill_tips_array": [],
        }

    return {
        "progress_log_info": {
            "chara_effect_log_array": [],
            "support_card_gain_info_array": [],
            "event_gain_info": _zero_gain_block(),
            "succession_gain_info": _zero_gain_block(),
            "succession_factor_gain_array": [],
            "race_history_array": [],
            "gain_skill_id_array": [],
            "total_skill_point": ci.get("skill_point", 0),
        },
        "end_info": {
            "chara_info": ci,
            "race_condition_array": [],
            "unchecked_event_array": [],
            "home_info": None,
            "win_saddle_id_array": [],
            "effected_factor_array": [],
            "race_start_info": None,
            "race_scenario": None,
            "add_trophy_info": None,
            "trophy_reward_info": None,
            "prev_chara_grade": None,
            "race_add_reward_info": [],
            "reserved_race_info": None,
            "mission_list": [],
            "story_event_mission_list": [],
            "start_dress_info": [],
            "story_event_chara_bonus_list": [],
            "resume_factor_select": None,
            "race_random_program_array": [],
            "race_reward_limit_more_list": [],
            "skill_filter_setting_array": [],
            "reward_summary_info": {
                "add_item_list": [], "add_piece_list": [], "add_card_list": [],
                "add_card_bonus_info": None, "add_support_card_list": [],
                "add_support_card_num_array": [], "add_honor_list": [],
                "add_chara_list": [], "add_cloth_list": [], "add_music_list": [],
                "add_story_id_array": [], "add_fcoin": 0, "add_present_num": 0,
                "add_total_fan": 0, "new_chara_profile_array": [],
                "force_update_honor_id": 0,
            },
            "is_umaplan": False,
        },
    }


@registry.endpoint("idle_single_mode/check_progress_log")
def handle_check_progress_log(payload: dict) -> dict:
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


@registry.endpoint("idle_single_mode/end")
def handle_end(payload: dict) -> dict:
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


@registry.endpoint("idle_single_mode/result")
def handle_result(payload: dict) -> dict:
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


@registry.endpoint("idle_single_mode/status")
def handle_status(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    run = full_state.get(IDLE_RUN_KEY)
    career = full_state.get(single_mode_team.STATE_KEY)
    if not run or not isinstance(career, dict):
        return _refuse()

    start_time = run.get("start_time") or ""
    end_time = run.get("end_time")
    if not end_time:
        ready_at = float(run.get("ready_at") or 0)
        end_time = _time_str(ready_at) if ready_at else start_time

    return _envelope({"progress_info": {
        "chara_info": copy.deepcopy(career["data"]["chara_info"]),
        "start_time": start_time,
        "end_time": end_time,
        "dress_id": 0,
    }})


def _reserved_race_info(full_state: dict) -> dict:
    if single_mode_team._reserved_deck_profile()["fixture_dir"] is None:
        info = single_mode_team._reserved_race_info(full_state)
        info["reserved_race_array"] = [d for d in info["reserved_race_array"]
                                       if d.get("deck_num")]
        return info
    info = copy.deepcopy(_default_reserved_race_info())
    saved = full_state.get(single_mode_team.RESERVED_DECKS_KEY) or {}
    if not isinstance(saved, dict) or not saved:
        return info

    decks = info.get("reserved_race_array") or []
    by_num = {d.get("deck_num"): d for d in decks}
    for key, row in saved.items():
        if not isinstance(row, dict):
            continue
        num = int(key) if str(key).isdigit() else 0
        if num <= 0:
            continue
        deck = by_num.get(num)
        if deck is None:
            deck = {"deck_num": num, "deck_name": f"Agenda {num}", "race_array": []}
            decks.append(deck)
            by_num[num] = deck
        deck["deck_name"] = row.get("deck_name") or deck.get("deck_name")
        deck["race_array"] = copy.deepcopy(row.get("race_array") or [])
    decks.sort(key=lambda d: d.get("deck_num") or 0)
    info["reserved_race_array"] = decks
    return info


@registry.endpoint("idle_single_mode/multi_race_reserve_deck")
def handle_multi_race_reserve_deck(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    req = payload.get("multi_race_reserve_deck")
    if not isinstance(req, dict):
        return _refuse()
    try:
        deck_num = int(req.get("deck_num") or 0)
    except (TypeError, ValueError):
        return _refuse()
    if deck_num <= 0:
        return _refuse()

    result = single_mode_team.handle_multi_race_reserve(
        {"viewer_id": viewer_id, "multi_race_reserve_deck": req})
    if (result.get("data_headers") or {}).get("result_code") != 1:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    return _envelope({"reserved_race_info": _reserved_race_info(full_state)})
