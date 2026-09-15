"""main_story_race/* -- the mandatory races embedded in main-story episodes
(e.g. episode 104's Debut race).

Endpoints:
  get_entry_list -> the paddock's opponent list (frame_order, chara/mob id)
  get_race_table -> full per-horse stats+skills for the paddock screen
  race_start     -> runs the race, returns the scenario the client replays
  race_end       -> grants the episode's reward if the race was cleared

Capture ground truth (2026-08-16, real server, ops 0016-0019, episode 104):
  get_entry_list : request {episode_id}; response {entry_chara_array:
                   [{frame_order, chara_id, mob_id, is_player, dress_id,
                   rank, chara_color_type}, ...]}.
  get_race_table : request {episode_id, trained_chara_id}; response
                   {race_horse_data: [<full per-horse entry>, ...]} -- same
                   shape as practice_race's race_horse_data_array entries.
  race_start     : request {episode_id, trained_chara_id, running_style};
                   response {race_result_info: {race_instance_id,
                   race_horse_data_array, season, weather, ground_condition,
                   random_seed, race_scenario}, running_style,
                   is_match_gimmick}.
  race_end       : request {episode_id}; response {main_story_data_list:
                   [{episode_id, state:1}], reward_array,
                   reward_summary_info} -- THE REWARD IS GRANTED HERE, not
                   through a separate main_story/first_clear call, unlike
                   non-race episodes (101-103 in the same capture session
                   went through first_clear; 104, a race episode, did not).
                   race_end's request carries no result data at all, so the
                   server must independently know whether the race was
                   cleared -- from its own record of what race_start actually
                   simulated (see MAIN_STORY_RACE_CTX_KEY below), not
                   anything the client reports.

[mdb] The full chain, verified field-for-field against the capture:
  main_story_data.story_type_N == 4 (a "race" story slot) -> story_id_N is
    the id into main_story_race_data (NOT the episode_id itself -- episode
    104's story_type_1=4, story_id_1=4, and main_story_race_data id=4 is a
    DIFFERENT row with race_instance_id=700401, group_id=12).
  main_story_race_data: race_instance_id (the course), group_id (the
    opponent roster key), clear_rank (the finish position needed to clear),
    bonus_group_id/bonus_chara_1-3 (character-linked bonus conditions, not
    yet modeled here), gimmick_type/gimmick_trigger_skill (course gimmicks,
    not yet modeled -- 0 for the one captured episode, so is_match_gimmick
    is always False until a real gimmick episode is captured to verify
    against).
  main_story_race_chara_data WHERE group_id=X ORDER BY bracket_number: the
    complete opponent roster PLUS the player's own slot (is_player=1,
    stats zeroed -- filled from the real trained_chara at request time).
    Checked bracket 1 (mob_id 8009) against the capture directly: speed 100,
    stamina 112, pow 132, guts 141, wiz 116 -- exact match.
  skill_set.id == chara_data.skill_set_id resolves to the mob's actual
    skill_array. Checked id 8064 against the capture: [{200282,1},
    {200532,1}] -- exact match.

NOT capture-verified (single data point, or none at all):
  * entry_chara_array's `rank` field -- all 4 opponents in the one capture
    showed rank=3; no schema column backs it and no second episode exists to
    tell a real formula from a coincidence, so it is a flat default matching
    that one observation, not a derived value.
  * A LOST race (finish worse than clear_rank): only a WIN was captured.
    What race_end actually sends for a miss is unverified; this refuses the
    episode (no reward, state left uncleared) as the least-wrong guess --
    replace this the day a losing capture exists.
  * bonus_group_id / bonus_chara_1-3 (character-linked race bonuses) and
    gimmick handling are read from master but not yet applied to anything.

Simulation reuses app/simulation/race_simulator.py wholesale -- the same
uma-tools race physics engine practice_race.py and daily_races.py already
run. build_horse_spec()'s field names (pow/guts/wiz/proper_distance_*/
proper_running_style_*/proper_ground_*) already match
main_story_race_chara_data's columns directly; only skill_array needs
adding via the skill_set lookup above.
"""

from __future__ import annotations

import logging
import random

from .. import master_data
from .. import state as state_store
from ..simulation import race_simulator
from . import registry, shop, trained_chara
from .practice_race import _build_race_horse_entry, _get_roster
from .stories import clear_main_story

log = logging.getLogger("uma-server")

MAIN_STORY_RACE_CTX_KEY = "main_story_race_ctx"
_RACE_STORY_TYPE = 4
_DEFAULT_ENTRY_RANK = 3    # see module docstring: single-data-point default


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _race_group_id_for_episode(episode_id) -> tuple | None:
    """(main_story_race_data row, group_id) for this episode's embedded race,
    or None if this episode has no race story slot."""
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
    """A main_story_race_chara_data row, shaped for build_horse_spec /
    _build_race_horse_entry (same field names as a roster chara dict)."""
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
        entry_num=len(opponents) + 1)
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
    # See practice_race.py's identical fix (2026-08-24): frame_order drives
    # the PRE-race gate lineup and must match the animation's randomized
    # gate, not the plain array index (player always index 0 -> always
    # "gate 1" before the race starts, then a visible jump once it runs).
    gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
    table = [
        _build_race_horse_entry(
            chara, frame_order=gate_assignment[i] + 1, final_grade=chara.get("rank", 1),
            popularity=pop_by[i],
            popularity_mark_rank_array=[pop_by[i], sta_by[i], pow_by[i]])
        for i, chara in enumerate(horses)]

    # The player is index 0 (simulate_race puts player_chara first). Their
    # finish rank -- by finishTime, the actual simulated result, not the
    # display-only popularity ranking above -- decides clear_rank.
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
        # is_match_gimmick is a plain int client-side (dump.cs:
        # MainStoryRaceRaceStartResponse.CommonResponse), not a bool -- a
        # Python bool here msgpack-encodes as the boolean wire format, which
        # the client's int deserializer rejects outright ("Deserialize error
        # at field: code is invalid. code:194 format:false", 194 = 0xC2 =
        # msgpack's false).
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
        # UNVERIFIED (see module docstring): no capture of a losing race_end
        # exists. Refusing is the least-wrong default -- no reward, episode
        # stays uncleared, matching every other "gate unmet" refusal on this
        # server -- rather than inventing a specific loss response shape.
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
