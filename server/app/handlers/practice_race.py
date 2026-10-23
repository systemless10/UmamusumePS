
from __future__ import annotations

import copy
import functools
import logging
import random

from .. import social
from .. import state as state_store
from ..fixtures import store as fixtures
from ..result_codes import ResultCode, refuse
from ..simulation import race_simulator
from . import trained_chara
from .load import STATE_KEY as LOAD_INDEX_STATE_KEY

log = logging.getLogger("uma-server")

ENDPOINT = "practice_race/race_start"

RACE_SKILL_CAP = 40

NPC_TYPE_TRAINER = 11

_skill_meta_cache: dict | None = None


def _skill_meta() -> dict:
    global _skill_meta_cache
    if _skill_meta_cache is None:
        from .. import master_data
        _skill_meta_cache = {
            r["id"]: (r["rarity"], r["grade_value"] or 0)
            for r in master_data.query("SELECT id, rarity, grade_value FROM skill_data")
        }
    return _skill_meta_cache


def _select_race_skills(chara: dict) -> dict:
    skills = chara.get("skill_array") or []
    if len(skills) <= RACE_SKILL_CAP:
        return chara
    meta = _skill_meta()

    def grade(s):
        return meta.get(s["skill_id"], (1, 0))[1]

    def rarity(s):
        return meta.get(s["skill_id"], (1, 0))[0]

    white = sorted((s for s in skills if rarity(s) == 1), key=grade, reverse=True)
    gold = sorted((s for s in skills if rarity(s) == 2), key=grade, reverse=True)
    unique = sorted((s for s in skills if rarity(s) in (3, 4, 5)), key=grade, reverse=True)

    n_unique = min(len(unique), max(4, RACE_SKILL_CAP // 5))
    rest = RACE_SKILL_CAP - n_unique
    n_gold = min(len(gold), rest // 2)
    n_white = min(len(white), rest - n_gold)
    selected = white[:n_white] + gold[:n_gold] + unique[:n_unique]

    if len(selected) < RACE_SKILL_CAP:
        chosen = {id(s) for s in selected}
        leftover = [s for s in white[n_white:] + gold[n_gold:] + unique[n_unique:] if id(s) not in chosen]
        selected += leftover[: RACE_SKILL_CAP - len(selected)]

    capped = dict(chara)
    capped["skill_array"] = selected
    return capped


def _frozen_servertime(viewer_id) -> int:
    full_state = state_store.get_state(viewer_id) or {}
    load_index = full_state.get(LOAD_INDEX_STATE_KEY, {})
    servertime = load_index.get("data_headers", {}).get("servertime")
    return servertime if servertime is not None else 1784325282


def _trained_chara_id(payload: dict) -> object:
    entries = payload.get("entry_chara_array") or []
    return entries[0].get("trained_chara_id") if entries else None


def _running_style(payload: dict) -> int | None:
    entries = payload.get("entry_chara_array") or []
    return entries[0].get("running_style") if entries else None


def _entry_chara_specs(payload: dict) -> list[dict]:
    entries = payload.get("entry_chara_array") or []
    return [
        {"entry_id": e.get("entry_id", i), "trained_chara_id": e.get("trained_chara_id"),
         "running_style": e.get("running_style"), "use_type": e.get("use_type"),
         "viewer_id": e.get("viewer_id")}
        for i, e in enumerate(entries)
        if e.get("trained_chara_id") is not None
    ]


def _get_roster(viewer_id) -> list[dict]:
    return trained_chara._get_or_seed_roster(viewer_id)


def resolve_entry(viewer_id, spec: dict) -> dict | None:
    owner = spec.get("viewer_id")
    if not owner or str(owner) == str(viewer_id):
        pool = _get_roster(viewer_id) or []
    else:
        from . import practice_race_lobby
        full_state = state_store.get_state(viewer_id) or {}
        pool = [p["chara"] for p in practice_race_lobby.state(full_state)["partners"]]
    found = next((c for c in pool
                  if c.get("trained_chara_id") == spec.get("trained_chara_id")), None)
    return copy.deepcopy(found) if found is not None else None


def resolve_conditions(settings: dict, rng: random.Random) -> tuple[int, int, int]:
    season = int(settings.get("season") or 0) or rng.randint(1, 5)
    weather = int(settings.get("weather") or 0) \
        or rng.choice((1, 1, 2, 3) + ((4,) if season == 4 else ()))
    ground = int(settings.get("ground_condition") or 0) \
        or (rng.choice((2, 3, 4)) if weather in (3, 4) else rng.choice((1, 1, 1, 2)))
    return season, weather, ground


def _is_real_chara(chara_id) -> bool:
    return bool(chara_id) and (chara_id == 1 or chara_id >= 1000)


@functools.lru_cache(maxsize=1)
def _mob_ids() -> tuple:
    from .. import master_data
    return tuple(r["mob_id"] for r in master_data.query(
        "SELECT mob_id FROM mob_data ORDER BY mob_id"))


def _fallback_mob_id(seed: int) -> int:
    pool = _mob_ids()
    return pool[abs(int(seed or 0)) % len(pool)] if pool else 8000


def _build_race_horse_entry(chara: dict, frame_order: int, final_grade: int, popularity: int, popularity_mark_rank_array: list[int]) -> dict:
    running_style = chara.get("running_style", 2)
    card_id = chara.get("card_id", 0)

    mob_id = chara.get("mob_id") or 0
    if mob_id:
        chara_id, card_id = 1, 0
        race_dress_id = chara.get("race_dress_id") or 1
    else:
        from .. import master_data
        row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        chara_id = (row["chara_id"] if row
                    else chara.get("chara_id")
                    or chara.get("single_mode_chara_id", 0))
        race_dress_id = (chara.get("race_dress_id")
                         or trained_chara.dress_for_card(card_id, chara_id))
        if not race_dress_id or not _is_real_chara(chara_id):
            mob_id = _fallback_mob_id(chara.get("single_mode_chara_id")
                                      or chara.get("trained_chara_id") or 0)
            chara_id, card_id, race_dress_id = 1, 0, 1

    return {
        "frame_order": frame_order,
        "viewer_id": chara.get("viewer_id", 0),
        "trainer_name": chara.get("trainer_name", "Trainer"),
        "owner_viewer_id": chara.get("owner_viewer_id", 0),
        "owner_trainer_name": chara.get("owner_trainer_name", ""),
        "single_mode_chara_id": chara.get("single_mode_chara_id", 0),
        "trained_chara_id": chara.get("trained_chara_id", 0),
        "nickname_id": chara.get("nickname_id", 0),
        "chara_id": chara_id,
        "card_id": card_id,
        "mob_id": mob_id,
        "rarity": chara.get("rarity", 1),
        "talent_level": chara.get("talent_level", 1),
        "skill_array": chara.get("skill_array", []),
        "stamina": chara["stamina"],
        "speed": chara["speed"],
        "pow": chara.get("pow", chara.get("power", 1)),
        "guts": chara["guts"],
        "wiz": chara.get("wiz", chara.get("wisdom", 1)),
        "running_style": running_style,
        "race_dress_id": race_dress_id,
        "chara_color_type": chara.get("chara_color_type", 0),
        "npc_type": chara.get("npc_type", 0),
        "final_grade": final_grade,
        "popularity": popularity,
        "popularity_mark_rank_array": popularity_mark_rank_array,
        "proper_distance_short": chara.get("proper_distance_short", 1),
        "proper_distance_mile": chara.get("proper_distance_mile", 1),
        "proper_distance_middle": chara.get("proper_distance_middle", 1),
        "proper_distance_long": chara.get("proper_distance_long", 1),
        "proper_running_style_nige": chara.get("proper_running_style_nige", 1),
        "proper_running_style_senko": chara.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": chara.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": chara.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": chara.get("proper_ground_turf", 1),
        "proper_ground_dirt": chara.get("proper_ground_dirt", 1),
        "motivation": chara.get("motivation", 3),
        "win_saddle_id_array": chara.get("win_saddle_id_array", []),
        "race_result_array": [],
        "team_id": chara.get("team_id", 0),
        "team_member_id": chara.get("team_member_id", 0),
        "team_rank": chara.get("team_rank", 0),
        "single_mode_win_count": chara.get("wins", 0),
        "fan_count": chara.get("fans", 0),
        "item_id_array": [],
        "motivation_change_flag": 0,
        "frame_order_change_flag": 0,
    }


def race_horse_rows(result: dict) -> tuple[list[dict], list[int]]:
    from . import race_mobs
    horses = result["horses"]
    sim_results = result["sim_results"]

    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}

    popularity_by_index = _rank_by(lambda i: -horses[i]["speed"])
    stamina_rank_by_index = _rank_by(lambda i: -horses[i]["stamina"])
    power_rank_by_index = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))

    gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
    gate_count = result.get("gate_count")
    if gate_count and len(horses) > gate_count:
        gate_assignment = [g % gate_count for g in gate_assignment]
    if len(horses) > 18:
        for ranks in (popularity_by_index, stamina_rank_by_index, power_rank_by_index):
            for i in ranks:
                ranks[i] = min(ranks[i], 18)
    race_horse_data_array = []
    for i, (chara, sim) in enumerate(zip(horses, sim_results)):
        race_horse_data_array.append(_build_race_horse_entry(
            chara,
            frame_order=gate_assignment[i] + 1,
            final_grade=chara.get("rank") or race_mobs.mob_rank(chara),
            popularity=popularity_by_index[i],
            popularity_mark_rank_array=[popularity_by_index[i], stamina_rank_by_index[i], power_rank_by_index[i]],
        ))
    return race_horse_data_array, gate_assignment


def _simulate(payload: dict, resolve=resolve_entry) -> dict | None:
    try:
        return _simulate_inner(payload, resolve)
    except Exception:
        log.exception("race simulation failed, falling back to fixture replay")
        return None


def _simulate_inner(payload: dict, resolve=resolve_entry) -> dict | None:
    entry_specs = _entry_chara_specs(payload)
    race_instance_id = payload.get("race_instance_id")
    if not entry_specs or race_instance_id is None:
        return None

    from . import directory
    viewer_id = payload.get("viewer_id")
    rng = random.Random()
    motivation = int(payload.get("motivation") or 0)
    players, served, player_entry_ids = [], [], []
    for spec in entry_specs:
        chara = resolve(viewer_id, spec)
        if chara is None:
            log.warning("practice_race/race_start: %s has no trained chara %s (use_type %s)",
                        viewer_id, spec["trained_chara_id"], spec.get("use_type"))
            return refuse(ResultCode.PRACTICE_RACE_START_NO_TRAINED_CHARA_ERROR)
        if spec.get("running_style"):
            chara["running_style"] = spec["running_style"]
        owner = str(chara.get("viewer_id") or viewer_id)
        chara["viewer_id"] = social._wire_id(owner)
        served.append(copy.deepcopy(chara))
        card = directory.cards_for([owner]).get(owner) or {}
        chara.update({
            "trainer_name": card.get("name") or f"Trainer{owner[-4:]}",
            "npc_type": NPC_TYPE_TRAINER,
            "race_dress_id": chara.get("race_cloth_id") or chara.get("race_dress_id"),
            "motivation": motivation or rng.randint(1, 5),
        })
        players.append(chara)
        player_entry_ids.append(spec["entry_id"])

    from . import race_mobs
    opponents_needed = max(0, 18 - len(players))
    opponents = race_mobs.preset_mobs(
        opponents_needed, rng,
        avoid_ids={p.get("trained_chara_id") for p in players})
    if len(opponents) < opponents_needed:
        return None
    for mob in opponents:
        mob.update({"trainer_name": None, "motivation": motivation or rng.randint(1, 5)})
    season, weather, ground = resolve_conditions(payload, rng)


    result = race_simulator.simulate_race(
        players[0], opponents, race_instance_id,
        ground_condition=ground,
        weather=weather,
        season=season,
        entry_num=payload.get("entry_num"),
        extra_player_charas=players[1:],
    )
    if result is None:
        return None

    race_horse_data_array, gate_assignment = race_horse_rows(result)
    for row, chara in zip(race_horse_data_array, players):
        row["race_result_array"] = copy.deepcopy(chara.get("race_result_list") or [])

    envelope = {
        "response_code": 1,
        "data_headers": {
            "result_code": 1,
            "notifications": {},
            "servertime": _frozen_servertime(payload.get("viewer_id")),
        },
        "data": {
            "trained_chara_array": served,
            "entry_info_array": [
                {"entry_id": eid, "frame_order": gate_assignment[i] + 1}
                for i, eid in enumerate(player_entry_ids)
            ],
            "practice_partner_owner_info_array": [],
            "practice_race_id": 0,
            "state": 1,
            "race_result_info": {
                "race_instance_id": race_instance_id,
                "race_horse_data_array": race_horse_data_array,
                "season": season,
                "weather": weather,
                "ground_condition": ground,
                "random_seed": result["seed"],
                "race_scenario": result["race_scenario"],
            },
        },
    }
    log.info(
        "practice_race/race_start: simulated race_instance_id=%s players=%s scenario_b64_len=%d "
        "frame_count=%d (raw=%d) duration=%.1fs",
        race_instance_id, [p.get("trained_chara_id") for p in players], len(result["race_scenario"]),
        result["frame_count"], result["raw_frame_count"],
        max((s["finishTime"] for s in result["sim_results"]), default=0),
    )
    return envelope


def _mark_practice_race_participated(payload: dict) -> None:
    viewer_id = payload.get("viewer_id")
    if not viewer_id:
        return
    from . import missions
    from .. import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    missions.mark_achieved(full_state, missions.FLAG_PRACTICE_RACE_RUN)
    state_store.save_state(viewer_id, full_state)


def handle_get_follow_user_data(payload: dict) -> dict:
    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {
            "follow_user_chara_array": [],
            "practice_partner_owner_info_array": [],
        },
    }


def handle_race_start(payload: dict, resolve=resolve_entry) -> dict:
    import time
    t0 = time.time()
    log.info("practice_race/race_start: handling request")
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    queued = full_state.pop("admin_practice_race_queue", None)
    if queued is not None:
        from . import practice_race_lobby
        state_store.save_state(payload.get("viewer_id"), full_state)
        data = copy.deepcopy(queued["payload"])
        race_id = practice_race_lobby.remember_race(
            payload.get("viewer_id"), queued["request"], data)
        data.update(practice_race_id=race_id, state=1)
        _mark_practice_race_participated(payload)
        return {"response_code": 1, "data_headers": {"result_code": 1,
                "notifications": {}, "servertime": _frozen_servertime(payload.get("viewer_id"))},
                "data": data}
    _mark_practice_race_participated(payload)
    simulated = _simulate(payload, resolve)
    log.info("practice_race/race_start: _simulate took %.2fs, result=%s", time.time() - t0, "ok" if simulated else "None")
    if simulated is not None and simulated["data_headers"]["result_code"] != 1:
        return simulated
    if simulated is not None:
        log.info("practice_race/race_start: real simulation")
        try:
            from . import practice_race_lobby
            simulated["data"]["practice_race_id"] = practice_race_lobby.remember_race(
                payload.get("viewer_id"), payload, simulated["data"])
        except Exception:
            log.exception("practice_race: could not file the race for replay; "
                          "serving it unsaved")
        return simulated

    log.info("practice_race/race_start: falling back to fixture replay")
    pairs = fixtures.all_for(ENDPOINT)
    if not pairs:
        return {
            "response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {},
        }

    wanted_chara = _trained_chara_id(payload)
    wanted_course = payload.get("race_instance_id")

    def score(pair):
        chara_match = wanted_chara is not None and _trained_chara_id(pair.request) == wanted_chara
        course_match = wanted_course is not None and pair.request.get("race_instance_id") == wanted_course
        return (chara_match, course_match)

    best = max(pairs, key=score)
    response = copy.deepcopy(best.response)
    if response.get("data", {}).get("race_result_info", {}).get("race_scenario"):
        from . import practice_race_lobby
        response["data"]["practice_race_id"] = practice_race_lobby.remember_race(
            payload.get("viewer_id"), payload, response["data"])
    return response
