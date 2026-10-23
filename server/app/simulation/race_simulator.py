
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
from pathlib import Path

from .. import master_data, region
from .scenario_format import HorseFrame, HorseResult, RaceEvent, SkillEvent, encode_scenario

COURSE_DATA_JSON = Path(
    os.environ.get("UMA_COURSE_DATA_JSON")
    or Path(__file__).resolve().parent / "race_engine" / "data" / "course_data.json"
)

_course_data_cache: dict | None = None

APTITUDE_LETTERS = "SABCDEFG"

STRATEGY_NAMES = {1: "Nige", 2: "Senkou", 3: "Sasi", 4: "Oikomi", 5: "Oonige"}

GROUND_CONDITION_NAMES = {1: "good", 2: "yielding", 3: "soft", 4: "heavy"}
WEATHER_NAMES = {1: "sunny", 2: "cloudy", 3: "rainy", 4: "snowy"}
SEASON_NAMES = {1: "spring", 2: "summer", 3: "autumn", 4: "winter", 5: "sakura"}


def _course_data() -> dict:
    global _course_data_cache
    if _course_data_cache is None:
        _course_data_cache = json.loads(COURSE_DATA_JSON.read_text(encoding="utf-8"))
    return _course_data_cache


def get_course_for_race_instance(race_instance_id: int) -> tuple[int, dict, int] | None:
    ri = master_data.query_one("SELECT race_id FROM race_instance WHERE id=?", (race_instance_id,))
    if ri is None:
        return None
    race = master_data.query_one("SELECT course_set, entry_num FROM race WHERE id=?", (ri["race_id"],))
    if race is None:
        return None
    course_set_id = race["course_set"]
    course = _course_data().get(str(course_set_id))
    if course is None:
        return None
    return course_set_id, course, race["entry_num"]


def _aptitude_letter(value: int) -> str:
    idx = max(0, min(7, 8 - value))
    return APTITUDE_LETTERS[idx]


def _distance_aptitude(chara: dict, distance_type: int) -> str:
    field = {1: "proper_distance_short", 2: "proper_distance_mile",
             3: "proper_distance_middle", 4: "proper_distance_long"}[distance_type]
    return _aptitude_letter(chara.get(field, 1))


def _surface_aptitude(chara: dict, surface: int) -> str:
    field = "proper_ground_turf" if surface == 1 else "proper_ground_dirt"
    return _aptitude_letter(chara.get(field, 1))


def _strategy_aptitude(chara: dict) -> str:
    field = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
              3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}.get(
        chara.get("running_style", 2), "proper_running_style_senko"
    )
    return _aptitude_letter(chara.get(field, 1))


def _sim_stat(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


_unique_skill_ids_cache = None


def _unique_skill_ids() -> set:
    global _unique_skill_ids_cache
    if _unique_skill_ids_cache is None:
        from .. import master_data
        _unique_skill_ids_cache = {
            str(r["id"]) for r in master_data.query(
                "SELECT id FROM skill_data WHERE rarity >= 3")
        }
    return _unique_skill_ids_cache


def build_horse_spec(chara: dict, course: dict, gate_index: int = 0) -> dict:
    running_style = chara.get("running_style", 2)
    unique_ids = _unique_skill_ids()
    return {
        "speed": _sim_stat(chara["speed"]),
        "stamina": _sim_stat(chara["stamina"]),
        "power": _sim_stat(chara.get("pow", chara.get("power", 1))),
        "guts": _sim_stat(chara["guts"]),
        "wisdom": _sim_stat(chara.get("wiz", chara.get("wisdom", 1))),
        "strategy": STRATEGY_NAMES.get(running_style, "Senkou"),
        "distanceAptitude": _distance_aptitude(chara, course["distanceType"]),
        "surfaceAptitude": _surface_aptitude(chara, course["surface"]),
        "strategyAptitude": _strategy_aptitude(chara),
        "mood": chara.get("motivation", 3) - 3,
        "skills": [
            {"skillId": str(sk["skill_id"]), "level": _sim_stat(sk.get("level") or 1),
             "isUnique": str(sk["skill_id"]) in unique_ids}
            for sk in (chara.get("skill_array") or [])
        ],
        "gateIndex": gate_index,
        "charaId": _opt_int(chara.get("chara_id") or ((chara.get("card_id") or 0) // 100)),
        "fanCount": _opt_int(chara.get("fan_count", chara.get("fans"))),
        "winCount": _opt_int(chara.get("single_mode_win_count", chara.get("wins"))),
        "teamStatTotals": chara.get("team_stat_totals"),
        "teamId": _opt_int(chara.get("team_id")) or 0,
        "teamMemberId": _opt_int(chara.get("team_member_id")) or 0,
        "popularity": _opt_int(chara.get("popularity")),
        "scenarioData": list(chara.get("scenario_data_array") or []),
        "successionSkillCount": _opt_int(chara.get("succession_skill_count")) if chara.get("succession_skill_count") is not None else sum(
            900000 <= int(sk["skill_id"]) < 1000000 for sk in chara.get("skill_array") or []),
        "raceContext": dict(chara.get("race_context") or {}),
    }


def _opt_int(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


_RACE_ENGINE_RS_RELATIVE = Path("race_engine_rs") / "target" / "release" / (
    "race_engine_rs.exe" if os.name == "nt" else "race_engine_rs")


def _resolve_race_engine_rs_exe() -> Path:
    override = os.environ.get("UMA_RACE_ENGINE_RS_EXE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / _RACE_ENGINE_RS_RELATIVE


RACE_ENGINE_RS_EXE = _resolve_race_engine_rs_exe()


_RS_TIMEOUT_SECONDS = 8
_RS_TIMEOUT_PER_RUNNER_SQ = 0.0006


def _rs_timeout(spec: dict) -> float:
    n = len(spec.get("horses") or ())
    return max(_RS_TIMEOUT_SECONDS, _RS_TIMEOUT_PER_RUNNER_SQ * n * n)
_rs_proc: subprocess.Popen | None = None
_RS_LOCK = threading.Lock()


def _rs_kill_worker() -> None:
    global _rs_proc
    proc, _rs_proc = _rs_proc, None
    if proc is None:
        return
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def _rs_worker() -> subprocess.Popen:
    global _rs_proc
    if _rs_proc is not None and _rs_proc.poll() is None:
        return _rs_proc
    from .race_engine.skill_table import ensure_file
    env = dict(os.environ)
    try:
        env["UMA_RACE_SKILL_TABLE"] = str(ensure_file())
    except Exception:
        log.warning("could not write the race skill table for race_engine_rs", exc_info=True)
    _rs_proc = subprocess.Popen(
        [str(RACE_ENGINE_RS_EXE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return _rs_proc


def _run_simulation_rust(spec: dict) -> dict:
    payload = json.dumps(spec)
    last_error = "unknown"
    with _RS_LOCK:
        for attempt in (1, 2):
            try:
                proc = _rs_worker()
            except (FileNotFoundError, OSError) as e:
                raise RuntimeError(f"race_engine_rs failed to run: {e}") from e
            watchdog = threading.Timer(_rs_timeout(spec), proc.kill)
            watchdog.start()
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (BrokenPipeError, OSError, ValueError) as e:
                last_error = str(e)
                line = ""
            finally:
                watchdog.cancel()
            if not line:
                last_error = f"worker produced no output ({last_error})"
                _rs_kill_worker()
                continue
            result = json.loads(line)
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(f"race_engine_rs failed: {result['error']}")
            return result
    raise RuntimeError(f"race_engine_rs failed: {last_error}")


def _dense_ranks(gates: list[int]) -> list[int]:
    order = sorted(range(len(gates)), key=lambda i: gates[i])
    ranks = [0] * len(gates)
    for rank, i in enumerate(order):
        ranks[i] = rank
    return ranks


def race_context_for_instance(race_instance_id: int) -> dict:
    row = master_data.query_one(
        "SELECT r.grade, ri.time, r.is_dirtgrade FROM race_instance ri JOIN race r ON r.id=ri.race_id WHERE ri.id=?",
        (race_instance_id,))
    return {**(dict(row) if row else {}), "race_instance_id": race_instance_id}


def build_spec(horses: list[dict], course_set_id: int, ground_condition: int, weather: int,
               season: int, seed: int, gates: list[int], single_mode: bool = False,
               race_context: dict | None = None, stalls: int | None = None) -> dict:
    course = _course_data()[str(course_set_id)]
    ranks = _dense_ranks(gates)
    if stalls and len(ranks) > stalls:
        ranks = [r % stalls for r in ranks]
    context = dict(race_context or {})
    if "sub_weather" in context:
        context.setdefault("subWeather", context["sub_weather"])
    context.setdefault("isAbroad", course["raceTrackId"] >= 10200)
    context.setdefault("region", region.PROCESS_REGION)
    context.setdefault("sharedRandomLot", random.Random(seed ^ 0x53484152).randrange(100))
    specs = [build_horse_spec(h, course, gate_index=ranks[i]) for i, h in enumerate(horses)]
    popularity = sorted(range(len(horses)), key=lambda i: (-horses[i].get("speed", 0), i))
    viewer_id = context.get("viewer_id") or (horses[0].get("viewer_id") if horses else None)
    for i, (horse, spec) in enumerate(zip(horses, specs)):
        spec["popularity"] = spec["popularity"] or popularity.index(i) + 1
        spec["isViewer"] = bool(horse.get("is_viewer", horse.get("isViewer",
            bool(viewer_id and horse.get("viewer_id") == viewer_id) if viewer_id else i == 0)))
        if not spec.get("teamStatTotals") and spec["teamId"]:
            teammates = [h for h, sp in zip(horses, specs) if sp["teamId"] == spec["teamId"]]
            spec["teamStatTotals"] = [sum(_sim_stat(h.get(k, h.get(alias, 1))) for h in teammates)
                for k, alias in (("speed", "speed"), ("stamina", "stamina"), ("pow", "power"), ("guts", "guts"), ("wiz", "wisdom"))]
    return {
        "courseId": course_set_id,
        "groundCondition": GROUND_CONDITION_NAMES.get(ground_condition, "good"),
        "weather": WEATHER_NAMES.get(weather, "sunny"),
        "season": SEASON_NAMES.get(season, "spring"),
        "seed": seed, "singleMode": bool(single_mode), "raceContext": context, "horses": specs,
    }


def skill_diagnostics(spec: dict) -> list[dict]:
    import re
    from .race_engine.skill_table import skills
    from .race_engine.activation_conditions import Conditions
    from .race_engine.race_solver_builder import _VALID_SKILL_TYPES
    table = skills()
    supported_targets = {1, 2, 3, 4, 7, 9, 10, 11, 18, 19, 20, 21, 22, 23}
    supported_values = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 20, 22, 23}
    out = []
    for h in spec["horses"]:
        for entry in h.get("skills") or []:
            sid = str(entry["skillId"]) if isinstance(entry, dict) else str(entry)
            definition = table.get(sid)
            issues = set()
            if definition is None:
                issues.add("missing_skill_definition")
            else:
                for alt in definition["alternatives"]:
                    for expression in (alt.get("condition"), alt.get("precondition")):
                        for token in re.findall(r"([a-z_][a-z_0-9]*)\s*(?:==|!=|>=|<=|>|<)", expression or ""):
                            if token not in Conditions:
                                issues.add("unsupported_condition:" + token)
                            if token in {"lane_type", "is_behind_in", "random_lot_shared", "is_goodstart"}:
                                issues.add("approximate_condition:" + token)
                    for ef in alt["effects"]:
                        if ef["type"] not in _VALID_SKILL_TYPES:
                            issues.add("unsupported_ability:" + str(ef["type"]))
                        if ef.get("target", 1) not in supported_targets:
                            issues.add("unsupported_target:" + str(ef["target"]))
                        vu = ef.get("valueUsage", 1)
                        if vu not in supported_values:
                            issues.add("unsupported_value_scaling:" + str(vu))
                        if vu in {11, 12}:
                            issues.add("approximate_value_scaling:" + str(vu))
                        if ef["type"] == 35:
                            issues.add("approximate_lane_target")
                    if alt.get("timeUsage", 1) not in {0, 1, 2, 3, 4, 5, 6, 7}:
                        issues.add("unsupported_duration_scaling:" + str(alt["timeUsage"]))
            if issues:
                out.append({"gateIndex": h.get("gateIndex"), "skillId": sid, "issues": sorted(issues)})
    return out


def run_simulation(
    horses: list[dict],
    course_set_id: int,
    ground_condition: int,
    weather: int,
    season: int,
    seed: int | None = None,
    gate_count: int | None = None,
    gate_assignment: list[int] | None = None,
    single_mode: bool = False,
    race_context: dict | None = None,
) -> dict:
    course = _course_data()[str(course_set_id)]
    stalls = gate_count
    gate_count = max(gate_count or 0, len(horses))
    gates = list(gate_assignment) if gate_assignment and len(gate_assignment) == len(horses) \
        else random.sample(range(gate_count), len(horses))
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    current_region = region.CURRENT_REGION.get()
    if current_region == "global" or not os.environ.get("UMA_JP_LEGACY_RACE"):
        from . import race_exact_engine
        try:
            result = race_exact_engine.run_simulation(
                horses, course_set_id, ground_condition, weather, season, seed, gates,
                single_mode=single_mode, race_context=race_context)
        except Exception:
            if current_region == "global":
                raise
            log.warning("JP race_exact_rs failed; falling back to race_engine_rs", exc_info=True)
            result = None
        if result is not None:
            result["finalStretchOvertakes"] = _final_stretch_overtakes(
                course, result["frames"], result["results"])
            return result
    spec = build_spec(horses, course_set_id, ground_condition, weather, season, seed, gates,
                      single_mode=single_mode, race_context=race_context, stalls=stalls)
    result = None
    if not os.environ.get("UMA_DISABLE_RACE_ENGINE_RS"):
        try:
            result = _run_simulation_rust(spec)
        except Exception:
            log.warning("race_engine_rs failed; falling back to the Python engine", exc_info=True)
    if result is None:
        from .race_engine.race_runner import run_race
        result = run_race(spec)
        result["engine"] = "python"
    else:
        result["engine"] = "rust"
    result["diagnostics"] = skill_diagnostics(spec)
    result["diagnostics"].extend({**entry, "issues": ["skill_build_failed"]}
                                 for entry in result.get("skippedSkills") or [])
    if result["diagnostics"]:
        log.warning("Race skill limitations: %s", result["diagnostics"])
    result["gate_assignment"] = gates
    result["course_set_id"] = course_set_id
    _add_result_details(result, horses, course_set_id, seed)
    result["finalStretchOvertakes"] = _final_stretch_overtakes(
        course, result.get("frames") or [], result.get("results") or [])
    return result


def _display_times(raw_times: list[float], course_set_id: int, seed: int) -> list[float]:
    if not raw_times:
        return []
    scaled = [t * 1.18 for t in raw_times]
    winner = min(scaled)
    offset = 0.0
    try:
        row = master_data.query_one(
            "SELECT finish_time_min, finish_time_min_random_range, finish_time_max, "
            "finish_time_max_random_range FROM race_course_set WHERE id=?", (course_set_id,))
    except Exception:
        row = None
    if row:
        rng = random.Random(seed)
        lo, hi = row["finish_time_min"] / 10000.0, row["finish_time_max"] / 10000.0
        if winner < lo:
            spread = row["finish_time_min_random_range"] / 10000.0
            offset = lo + rng.uniform(-spread, spread) - winner
        elif winner > hi:
            spread = row["finish_time_max_random_range"] / 10000.0
            offset = hi + rng.uniform(-spread, spread) - winner
    return [t + offset for t in scaled]


def _base_stat(value, motivation) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 1.0
    if v > 1200:
        v = 1200 + (v - 1200) // 2
    return v * (1 + 0.02 * ((motivation or 3) - 3))


def _rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda i: (-values[i], i))
    ranks = [0] * len(values)
    for rank, i in enumerate(order):
        ranks[i] = rank
    return ranks


def _defeat_type(horse: dict, result: dict, course: dict, field_size: int) -> int:
    order = result["finishOrder"]
    if order == 0:
        return 1
    if order == 1:
        return 2
    if result.get("rushed"):
        return 4
    if result.get("hpOut"):
        return 6 if result.get("gutsOrder", 0) < max(1, field_size // 3) else 5
    distance_field = {1: "proper_distance_short", 2: "proper_distance_mile",
                      3: "proper_distance_middle", 4: "proper_distance_long"}[course["distanceType"]]
    if (horse.get(distance_field) or 1) <= 5:
        return 12
    ground_field = "proper_ground_turf" if course["surface"] == 1 else "proper_ground_dirt"
    if (horse.get(ground_field) or 1) <= 5:
        return 13
    if (horse.get("motivation") or 3) <= 2:
        return 14
    if result.get("blockedFrontTime", 0.0) >= 10.0:
        return 10
    if result.get("lastSpurtStartDistance", -1) < 0:
        return 7
    return 9


def _add_result_details(result: dict, horses: list[dict], course_set_id: int, seed: int) -> None:
    results = result.get("results") or []
    course = _course_data()[str(course_set_id)]
    display = _display_times([r["finishTimeRaw"] for r in results], course_set_id, seed)
    by_order = {r["finishOrder"]: i for i, r in enumerate(results)}
    guts = _rank_desc([_base_stat(h.get("guts"), h.get("motivation")) for h in horses])
    wiz = _rank_desc([_base_stat(h.get("wiz", h.get("wisdom")), h.get("motivation")) for h in horses])
    for i, r in enumerate(results):
        r["finishTimeDisplay"] = display[i]
        ahead = by_order.get(r["finishOrder"] - 1)
        r["finishDiffDisplay"] = 0.0 if ahead is None else max(0.0, display[i] - display[ahead])
        r["gutsOrder"] = guts[i] if i < len(guts) else 0
        r["wizOrder"] = wiz[i] if i < len(wiz) else 0
        r["defeat"] = _defeat_type(horses[i], r, course, len(results)) if i < len(horses) else 0


def _final_stretch_overtakes(course, frames: list, results: list) -> list:
    n = len(results)
    straights = (course.get("straights") if isinstance(course, dict)
                 else getattr(course, "straights", None)) or []
    if not frames or not straights or not n:
        return [0] * n
    last = straights[-1]
    entry = last["start"] if isinstance(last, dict) else last.start

    entry_frame = [None] * n
    for f in frames:
        horses = f["horses"]
        if len(horses) < n:
            continue
        for i in range(n):
            if entry_frame[i] is None and horses[i]["pos"] >= entry:
                entry_frame[i] = f
        if all(e is not None for e in entry_frame):
            break
    final_place = [int(r["finishOrder"]) for r in results]
    out = []
    for i in range(n):
        f = entry_frame[i] or frames[-1]
        horses = f["horses"]
        order = sorted(range(n), key=lambda j: (-horses[j]["pos"], final_place[j]))
        place_then = order.index(i)
        out.append(max(0, place_then - final_place[i]))
    return out


log = logging.getLogger("uma-server")


def _simulate_job(job: dict) -> dict:
    sim = run_simulation(
        job["horses"], job["course_set_id"], job["ground"], job["weather"],
        job["season"], job["seed"], gate_count=job["gate_count"],
        gate_assignment=job["gate_assignment"], single_mode=bool(job.get("single_mode")), race_context=job.get("race_context"))
    scenario_b64, frame_count, _raw = build_race_scenario(
        sim, job["n"], job["gate_count"])
    return {"sim": sim, "scenario": scenario_b64, "frame_count": frame_count}


_PARALLEL_SKILL_THRESHOLD = 400


def run_races(jobs: list, max_workers: int = 5) -> list:
    if not jobs:
        return []
    total_skills = sum(
        len(h.get("skill_array") or h.get("skills") or [])
        for j in jobs for h in (j.get("horses") or []) if isinstance(h, dict))
    if len(jobs) > 1 and total_skills >= _PARALLEL_SKILL_THRESHOLD:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
                return list(pool.map(_simulate_job, jobs))
        except Exception:
            log.warning("parallel race execution failed; falling back to sequential",
                        exc_info=True)
    return [_simulate_job(j) for j in jobs]


_DENSE_LEAD_FRAMES = 17
_SPARSE_STRIDE = 16
_GOAL_WINDOW = 25.0

_GATE_FRAME_SPEED = 3.0


def _coast_pos(pos: float, t: float, fin) -> float:
    if fin is not None and t > fin[2]:
        return fin[0] + fin[1] * (t - fin[2])
    return pos


def _select_frames(frames: list, finish: dict, distance: float) -> list[int]:
    n = len(frames)
    if n <= _DENSE_LEAD_FRAMES:
        return list(range(n))
    keep = list(range(_DENSE_LEAD_FRAMES))
    last = _DENSE_LEAD_FRAMES - 1
    dense = False
    for k in range(_DENSE_LEAD_FRAMES, n):
        f = frames[k]
        near = any(abs(distance - _coast_pos(h["pos"], f["t"], finish.get(hi))) < _GOAL_WINDOW
                   for hi, h in enumerate(f["horses"]))
        if k - last >= _SPARSE_STRIDE or (dense and near):
            keep.append(k)
            last = k
            dense = near
    if keep[-1] != n - 1:
        keep.append(n - 1)
    return keep


def build_race_scenario(sim_result: dict, horse_num: int, gate_count: int | None = None) -> tuple[str, int, int]:
    import base64
    import gzip

    if sim_result.get("engine") == "race_exact_rs":
        return sim_result["race_scenario"], len(sim_result["frames"]), len(sim_result["frames"])

    gate_assignment = sim_result.get("gate_assignment") or list(range(horse_num))
    rank_of = {orig_i: rank for rank, orig_i in
               enumerate(sorted(range(horse_num), key=lambda i: gate_assignment[i]))}
    distance = _course_data()[str(sim_result["course_set_id"])]["distance"] \
        if sim_result.get("course_set_id") is not None else None
    sim_frames = sim_result["frames"]
    pre_thinned = bool(sim_result.get("framesDownsampled"))
    needs_gate = sim_result.get("gateFrameNeeded")
    if needs_gate is None:
        needs_gate = bool(sim_frames and sim_frames[0]["t"] > 0)
    if needs_gate:
        sim_frames = [{
            "t": 0.0,
            "horses": [{"pos": 0.0, "speed": _GATE_FRAME_SPEED, "hp": h.get("hp"),
                        "lane": h.get("lane", 0.0), "tempt": 0, "block": -1}
                       for h in sim_frames[0]["horses"]],
        }] + sim_frames

    finish = {}
    engine_finish = sim_result.get("finishFrames") if pre_thinned else None
    if engine_finish:
        for hi in range(horse_num):
            entry = engine_finish[hi] if hi < len(engine_finish) else None
            if entry:
                finish[hi] = (entry["pos"], entry["speed"], entry["t"], entry.get("lane", 0.0))
    else:
        full_frames = sim_result["frames"]
        for hi in range(horse_num):
            finish_t = sim_result["results"][hi]["finishTimeRaw"]
            last_at_or_before = None
            for f in full_frames:
                if f["t"] <= finish_t:
                    last_at_or_before = f
                else:
                    break
            if last_at_or_before is not None:
                h = last_at_or_before["horses"][hi]
                finish[hi] = (h["pos"], h["speed"], last_at_or_before["t"], h.get("lane", 0.0))

    if pre_thinned or distance is None:
        raw_frames = sim_frames
    else:
        raw_frames = [sim_frames[k] for k in _select_frames(sim_frames, finish, distance)]

    last_good = {}
    for f in raw_frames:
        for hi, h in enumerate(f["horses"]):
            if h.get("pos") is None or h.get("speed") is None:
                good = last_good.get(hi)
                if good is None:
                    raise RuntimeError(
                        f"race sim produced no valid position for horse {hi}; "
                        "check its input stats")
                h["pos"], h["speed"], h["hp"] = good
            else:
                last_good[hi] = (h["pos"], h["speed"], h.get("hp") or 0.0)
                if h.get("hp") is None:
                    h["hp"] = 0.0

    frames = []
    for f in raw_frames:
        t = f["t"]
        horse_frames: list[HorseFrame | None] = [None] * horse_num
        for idx, h in enumerate(f["horses"]):
            pos, sp, lane = h["pos"], h["speed"], h.get("lane", 0.0)
            fin = finish.get(idx)
            if fin is not None and t > fin[2]:
                pos = _coast_pos(pos, t, fin)
                sp = fin[1]
                lane = fin[3]
            block = h.get("block", -1)
            horse_frames[rank_of[idx]] = HorseFrame(
                distance=pos,
                lane_position=lane,
                speed=sp,
                hp=h["hp"],
                temptation_mode=int(h.get("tempt") or 0),
                block_front_horse_index=rank_of[block] if block is not None and block >= 0 else -1,
            )
        frames.append((t, horse_frames))

    results: list[HorseResult | None] = [None] * horse_num
    for idx, r in enumerate(sim_result["results"]):
        results[rank_of[idx]] = HorseResult(
            finish_order=r["finishOrder"],
            finish_time=r.get("finishTimeDisplay", r["finishTime"]),
            finish_diff_time=r.get("finishDiffDisplay", 0.0),
            start_delay_time=r["startDelayTime"],
            guts_order=r.get("gutsOrder", 0),
            wiz_order=r.get("wizOrder", 0),
            last_spurt_start_distance=r["lastSpurtStartDistance"] if r["lastSpurtStartDistance"] >= 0 else -1.0,
            running_style=r["runningStyle"],
            defeat=r.get("defeat", 0),
            finish_time_raw=r["finishTimeRaw"],
        )
    skill_events = [
        SkillEvent(
            horse_index=rank_of[e["horseIndex"]],
            skill_id=e["skillId"],
            t=e["t"],
            duration=e["duration"],
            target_mask=sum(1 << rank_of[t] for t in (e.get("targets") or [e["horseIndex"]])),
        )
        for e in sim_result.get("skillEvents", [])
        if isinstance(e["skillId"], int)
    ]
    race_events = [
        RaceEvent(t=e["t"], type_id=e["type"], params=(rank_of[e["horseIndex"]],))
        for e in sim_result.get("raceEvents", [])
    ]
    blob = encode_scenario(horse_num, frames, results, skill_events=skill_events, race_events=race_events,
                           distance_diff_max=float(sim_result.get("distanceDiffMax") or 0.0))
    scenario_b64 = base64.b64encode(gzip.compress(blob)).decode("ascii")
    return (scenario_b64, len(raw_frames),
            int(sim_result.get("rawFrameCount", len(sim_result["frames"]))))


def simulate_race(
    player_chara: dict,
    opponents: list[dict],
    race_instance_id: int,
    ground_condition: int = 1,
    weather: int = 1,
    season: int = 1,
    seed: int | None = None,
    entry_num: int | None = None,
    gate_assignment: list[int] | None = None,
    extra_player_charas: list[dict] | None = None,
    single_mode: bool = False,
    race_context: dict | None = None,
) -> dict | None:
    course_info = get_course_for_race_instance(race_instance_id)
    if course_info is None:
        return None
    course_set_id, _course, course_entry_num = course_info

    primary = [player_chara] + list(extra_player_charas or [])
    field = entry_num or course_entry_num
    field = max(len(primary), min(field, course_entry_num, len(primary) + len(opponents)))
    horses = [dict(h, is_viewer=True) for h in primary] + [dict(h, is_viewer=False) for h in opponents[: field - len(primary)]]
    metadata = race_context_for_instance(race_instance_id)
    metadata.update(race_context or {})
    sim_result = run_simulation(horses, course_set_id, ground_condition, weather, season, seed,
                                 gate_count=course_entry_num, gate_assignment=gate_assignment,
                                 single_mode=single_mode, race_context=metadata)
    scenario_b64, frame_count, raw_frame_count = build_race_scenario(sim_result, len(horses), course_entry_num)
    return {
        "race_scenario": scenario_b64,
        "frame_count": frame_count,
        "raw_frame_count": raw_frame_count,
        "sim_results": sim_result["results"],
        "seed": sim_result["seed"],
        "course_set_id": course_set_id,
        "horses": horses,
        "gate_assignment": sim_result.get("gate_assignment"),
        "gate_count": course_entry_num,
        "skill_events": sim_result.get("skillEvents") or [],
        "final_stretch_overtakes": sim_result.get("finalStretchOvertakes") or [],
    }
