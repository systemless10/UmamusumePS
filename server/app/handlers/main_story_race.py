
from __future__ import annotations

import logging
import random

from .. import master_data
from .. import state as state_store
from ..simulation import race_simulator
from . import registry, shop, trained_chara
from .practice_race import _build_race_horse_entry, _get_roster
from .stories import clear_main_story
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

MAIN_STORY_RACE_CTX_KEY = "main_story_race_ctx"
_RACE_STORY_TYPE = 4
_DEFAULT_ENTRY_RANK = 3


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _race_group_id_for_episode(episode_id) -> tuple | None:
    story = master_data.query_one(
        "SELECT * FROM main_story_data WHERE id=?", (episode_id,))
    if story is None:
        return None
    for slot in (1, 2, 3, 4, 5):
        if story[f"story_type_{slot}"] == _RACE_STORY_TYPE:
            race_row = master_data.query_one(
                "SELECT * FROM main_story_race_data WHERE id=?",
                (story[f"story_id_{slot}"],))
            if race_row is not None:
                return race_row
    return None


def _roster_rows(group_id: int) -> list:
    return master_data.query(
        "SELECT * FROM main_story_race_chara_data WHERE group_id=? "
        "ORDER BY bracket_number", (group_id,))


def _skill_array_for(skill_set_id: int) -> list:
    if not skill_set_id:
        return []
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    if row is None:
        return []
    out = []
    for i in range(1, 21):
        sid = row[f"skill_id{i}"]
        if sid:
            out.append({"skill_id": sid, "level": row[f"skill_level{i}"] or 1})
    return out


def _mob_chara_dict(row) -> dict:
    return {
        "chara_id": row["chara_id"], "mob_id": row["mob_id"],
        "rarity": 1, "talent_level": 1,
        "skill_array": _skill_array_for(row["skill_set_id"]),
        "stamina": row["stamina"], "speed": row["speed"],
        "pow": row["pow"], "guts": row["guts"], "wiz": row["wiz"],
        "running_style": row["running_style"],
        "race_dress_id": row["dress_id"], "chara_color_type": row["chara_color_type"],
        "proper_distance_short": row["proper_distance_short"],
        "proper_distance_mile": row["proper_distance_mile"],
        "proper_distance_middle": row["proper_distance_middle"],
        "proper_distance_long": row["proper_distance_long"],
        "proper_running_style_nige": row["proper_running_style_nige"],
        "proper_running_style_senko": row["proper_running_style_senko"],
        "proper_running_style_sashi": row["proper_running_style_sashi"],
        "proper_running_style_oikomi": row["proper_running_style_oikomi"],
        "proper_ground_turf": row["proper_ground_turf"],
        "proper_ground_dirt": row["proper_ground_dirt"],
        "motivation": row["motivation"],
    }


@registry.endpoint("main_story_race/get_entry_list")
def handle_get_entry_list(payload: dict) -> dict:
    race_row = _race_group_id_for_episode(payload.get("episode_id"))
    if race_row is None:
        return _refuse()
    entries = [
        {"frame_order": r["bracket_number"], "chara_id": r["chara_id"],
         "mob_id": r["mob_id"], "is_player": r["is_player"],
         "dress_id": r["dress_id"], "rank": _DEFAULT_ENTRY_RANK,
         "chara_color_type": r["chara_color_type"]}
        for r in _roster_rows(race_row["group_id"])]
    return _ok({"entry_chara_array": entries})


@registry.endpoint("main_story_race/get_race_table")
def handle_get_race_table(payload: dict) -> dict:
    race_row = _race_group_id_for_episode(payload.get("episode_id"))
    if race_row is None:
        return _refuse()
    viewer_id = payload["viewer_id"]
    wanted_chara = payload.get("trained_chara_id")
    roster = _get_roster(viewer_id)
    player = next((c for c in roster if c.get("trained_chara_id") == wanted_chara), None)
    if player is None:
        return _refuse()

    rows = _roster_rows(race_row["group_id"])
    horses = [player if r["is_player"] else _mob_chara_dict(r) for r in rows]

    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}

    pop_by = _rank_by(lambda i: -horses[i]["speed"])
    sta_by = _rank_by(lambda i: -horses[i]["stamina"])
    pow_by = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))

    table = [
        _build_race_horse_entry(
            chara, frame_order=i + 1, final_grade=chara.get("rank", 1),
            popularity=pop_by[i],
            popularity_mark_rank_array=[pop_by[i], sta_by[i], pow_by[i]])
        for i, chara in enumerate(horses)]
    return _ok({"race_horse_data": table})


@registry.endpoint("main_story_race/race_start")
def handle_race_start(payload: dict) -> dict:
    episode_id = payload.get("episode_id")
    race_row = _race_group_id_for_episode(episode_id)
    if race_row is None:
        return _refuse()
    viewer_id = payload["viewer_id"]
    wanted_chara = payload.get("trained_chara_id")
    roster = _get_roster(viewer_id)
    player = next((c for c in roster if c.get("trained_chara_id") == wanted_chara), None)
    if player is None:
        return _refuse()

    running_style = payload.get("running_style")
    if running_style:
        player = dict(player)
        player["running_style"] = running_style

    rows = _roster_rows(race_row["group_id"])
    opponents = [_mob_chara_dict(r) for r in rows if not r["is_player"]]

    result = race_simulator.simulate_race(
        player, opponents, race_row["race_instance_id"],
        entry_num=len(opponents) + 1, race_context={"race_type": 3})
    if result is None:
        return _refuse()

    horses = result["horses"]
    sim_results = result["sim_results"]

    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}

    pop_by = _rank_by(lambda i: -horses[i]["speed"])
    sta_by = _rank_by(lambda i: -horses[i]["stamina"])
    pow_by = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))
    gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
    table = [
        _build_race_horse_entry(
            chara, frame_order=gate_assignment[i] + 1, final_grade=chara.get("rank", 1),
            popularity=pop_by[i],
            popularity_mark_rank_array=[pop_by[i], sta_by[i], pow_by[i]])
        for i, chara in enumerate(horses)]

    finish_order = sorted(range(len(horses)), key=lambda i: sim_results[i]["finishTime"])
    player_finish_rank = finish_order.index(0) + 1

    full_state = state_store.get_state(viewer_id) or {}
    full_state[MAIN_STORY_RACE_CTX_KEY] = {
        "episode_id": episode_id,
        "cleared": player_finish_rank <= (race_row["clear_rank"] or 1),
    }
    state_store.save_state(viewer_id, full_state)

    gimmick_type = race_row["gimmick_type"]
    is_match_gimmick = 0
    if gimmick_type:
        trigger = race_row["gimmick_trigger_skill"]
        owned = {s.get("skill_id") for s in (player.get("skill_array") or [])}
        is_match_gimmick = 1 if (trigger and trigger in owned) else 0

    return _ok({
        "race_result_info": {
            "race_instance_id": race_row["race_instance_id"],
            "race_horse_data_array": table,
            "season": payload.get("season") or 1,
            "weather": payload.get("weather") or 1,
            "ground_condition": payload.get("ground_condition") or 1,
            "random_seed": result["seed"],
            "race_scenario": result["race_scenario"],
        },
        "running_style": player.get("running_style", 2),
        "is_match_gimmick": is_match_gimmick,
    })


@registry.endpoint("main_story_race/race_end")
def handle_race_end(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.pop(MAIN_STORY_RACE_CTX_KEY, None) or {}
    if ctx.get("episode_id") != episode_id:
        return _refuse()

    if not ctx.get("cleared"):
        state_store.save_state(viewer_id, full_state)
        return _refuse()

    summary = shop._empty_summary()
    ok, reward_array = clear_main_story(full_state, episode_id, summary, viewer_id)
    if not ok:
        state_store.save_state(viewer_id, full_state)
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "main_story_data_list": [{"episode_id": episode_id, "state": 1}],
        "reward_array": reward_array,
        "reward_summary_info": summary,
    })
