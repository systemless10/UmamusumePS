"""
Orchestrates a real practice-race simulation: maps real trained_chara data
(the player's selected uma + 17 real opponents pulled from the account's
roster) into uma-tools' HorseParameters format, runs the actual race physics
natively via `app.simulation.race_engine` (a Python port of uma-skill-tools'
RaceSolver, validated bit-for-bit against the original TypeScript engine --
see `race_engine/__init__.py`), and encodes the result into the real
`race_scenario` wire format (see `scenario_format.py`).

This replaces fixture replay for practice_race/race_start: instead of only
being able to show one of the ~4 real races we happened to capture, this
produces a genuine result for any uma/course/condition combination.

Physics run through the native Rust port (race_engine_rs) by default, with
app.simulation.race_engine (pure Python, formula/RNG-matched against Rust)
as the automatic fallback if the Rust binary is missing or a run of it
raises -- see run_simulation() below. The original Node.js/TypeScript engine
(uma-skill-tools, shelling out to a pre-built raceRunner.bundle.js) has been
fully retired from this path: it ran every horse in isolation with no notion
of the other horses on track, so it can't reproduce the blocking/position-
keep/competition mechanics both native engines implement (race_field.py).
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
from pathlib import Path

from .. import master_data
from .scenario_format import HorseFrame, HorseResult, RaceEvent, SkillEvent, encode_scenario

# course_data.json used to be resolved from the vendored uma-tools/ checkout
# (uma-skill-tools/data/course_data.json) -- when that failed to resolve,
# simulate_race() raised and every race silently fell back to STATIC FIXTURE
# REPLAY, which serves horses belonging to some other race and makes the real
# client NullRef in PaddockViewControllerBase (observed 2026-08-11: a Grand
# Live debut softlock; the traceback in the server log was FileNotFoundError
# on course_data.json). Now that uma-tools/ has been deleted (2026-08-30,
# Node/TS fully retired -- see race_simulator's module docstring), this reads
# the same file's contents from race_engine/data/course_data.json instead --
# already vendored in-repo there for the native engines, and confirmed
# byte-identical to the old uma-tools copy before that copy was removed.
# UMA_COURSE_DATA_JSON still overrides the path entirely if ever needed.
COURSE_DATA_JSON = Path(
    os.environ.get("UMA_COURSE_DATA_JSON")
    or Path(__file__).resolve().parent / "race_engine" / "data" / "course_data.json"
)

_course_data_cache: dict | None = None

APTITUDE_LETTERS = "SABCDEFG"  # index 0=S (best) .. 7=G (worst)

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
    """race_instance_id -> race_instance.race_id -> race.course_set/entry_num
    -> course_data.json entry. entry_num is the real number of starting
    stalls for this specific race (varies 5-18 across master.mdb, not
    always a full 18-horse field -- practice races were previously always
    simulated with 18 regardless, which is why more horses would show up
    running than the race's own gate could hold)."""
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
    """Floor a RAW stat (chara_info's speed/stamina/power/guts/wiz, or a
    skill's level) at 1 before it reaches the physics engine. Zero or
    NEGATIVE raw stats (a deliberately-debuffed trainee) need this: sqrt of
    a negative puts NaN into the simulation, and JSON.stringify(NaN) is
    null, which detonated build_race_scenario and silently degraded EVERY
    career race to fixture replay. Clamped for the physics only; the served
    chara_info keeps its real (possibly negative) value.

    Deliberately NOT doing anything to the upper end here (previously it
    briefly also halved raw stats past 1200, matching the real client's own
    RawStat -> BaseStat formula -- but race_solver_builder.py's
    _adjust_overcap() already does exactly that step downstream, so doing
    it here too silently halved every stat TWICE. Left as a raw passthrough
    on the high end; see _adjust_overcap/build_base_stats for the real
    >1200 halving and the [1,2000] final clamp)."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


_unique_skill_ids_cache = None


def _unique_skill_ids() -> set:
    """skill_id (str) -> chara-unique tier (rarity 3/4/5). race_engine's
    skill-activate hook used to hardcode SkillEvent.isUnique to 0 for every
    single event regardless of the skill's real rarity -- this feeds the
    real value through instead, via the "isUnique" flag on each skill spec
    below."""
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
    }


_RACE_ENGINE_RS_RELATIVE = Path("race_engine_rs") / "target" / "release" / (
    "race_engine_rs.exe" if os.name == "nt" else "race_engine_rs")


def _resolve_race_engine_rs_exe() -> Path:
    override = os.environ.get("UMA_RACE_ENGINE_RS_EXE")
    if override:
        return Path(override)
    # server/app/simulation/race_simulator.py -> server/race_engine_rs/...
    return Path(__file__).resolve().parents[2] / _RACE_ENGINE_RS_RELATIVE


RACE_ENGINE_RS_EXE = _resolve_race_engine_rs_exe()


# The race engine is now a PERSISTENT worker rather than a process per race.
# Spawning it cost ~20 ms, and it re-parsed course_data.json + skill_data.json
# (~600 KB) on every start for another ~21 ms -- 41 ms of fixed overhead on a
# ~119 ms race, none of it simulation. The binary loops over newline-delimited
# JSON (race_engine_rs/src/main.rs), so one process serves every race.
#
# Requests are serialized through _RS_LOCK: the worker handles one race at a
# time. That is not a throughput loss worth worrying about here (main.py
# already serializes a viewer's requests, and this is a private server), and
# it is what keeps the single stdin/stdout pipe coherent.
_RS_TIMEOUT_SECONDS = 8
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
    """The live worker, started on first use and restarted if it ever dies."""
    global _rs_proc
    if _rs_proc is not None and _rs_proc.poll() is None:
        return _rs_proc
    _rs_proc = subprocess.Popen(
        [str(RACE_ENGINE_RS_EXE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,   # errors come back as a JSON "error" reply
        text=True,
        encoding="utf-8",
    )
    return _rs_proc


def _run_simulation_rust(spec: dict) -> dict:
    """Primary path: the native Rust port of app.simulation.race_engine (see
    that package's README/history -- formula-for-formula and RNG-bit-for-bit
    matched against the Python engine, which remains the automatic fallback
    below if this ever raises). Same JSON contract documented in
    race_engine_rs/src/main.rs, now one line in and one line out.

    A dead or wedged worker is killed and retried once; if that also fails the
    caller falls back to the Python engine for this race, exactly as before."""
    payload = json.dumps(spec)
    last_error = "unknown"
    with _RS_LOCK:
        for attempt in (1, 2):
            try:
                proc = _rs_worker()
            except (FileNotFoundError, OSError) as e:
                raise RuntimeError(f"race_engine_rs failed to run: {e}") from e
            # readline on a pipe has no timeout of its own; killing the worker
            # makes it return "" immediately, which the retry below handles.
            watchdog = threading.Timer(_RS_TIMEOUT_SECONDS, proc.kill)
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


def run_simulation(
    horses: list[dict],
    course_set_id: int,
    ground_condition: int,
    weather: int,
    season: int,
    seed: int | None = None,
    gate_count: int | None = None,
    gate_assignment: list[int] | None = None,
) -> dict:
    """horses: list of real trained_chara dicts (18 of them, index 0 = player's horse).
    Returns {seed, frames, results, skillEvents}. Primary engine is the
    native Rust port (race_engine_rs, a formula-for-formula and RNG-bit-for-
    bit match of the Python engine below, run as a subprocess for its speed);
    any failure to run it (missing/unbuilt binary, a crash, a timeout) falls
    back automatically to app.simulation.race_engine's pure-Python
    implementation, so a Rust bug degrades a single race's engine rather than
    the request. Set UMA_DISABLE_RACE_ENGINE_RS=1 to force the Python engine
    always (e.g. while debugging a suspected Rust-specific issue) --
    UMA_RACE_ENGINE_RS_EXE overrides the resolved binary path. The original
    Node.js/TypeScript engine (uma-skill-tools) has been fully retired from
    this path -- it has no notion of the multi-horse field mechanics
    (blocking, position-keep, competition) both native engines implement, so
    it is no longer a suitable fallback; see race_engine/race_field.py.

    gate_count: the course's full physical gate count (defaults to the field
    size when unset). Real races randomize which starting gate each entrant
    -- including the player -- draws; horses used to always be built in
    array order with the player permanently at gate_index 0 (gate 1). A
    random sample of `len(horses)` distinct gates out of `gate_count` is
    drawn instead, keeping array order (and therefore results[i]/sim_results[i]
    -> horse identity) unchanged -- only the physical starting gate moves.

    gate_assignment: use THIS exact permutation instead of rolling a fresh
    one. Needed wherever the gate lineup is shown to the player in one
    request (e.g. daily_races.py's race_entry paddock screen, built from
    race_horse_data_array[].frame_order) before the race actually runs in a
    LATER, separate request (race_start) -- rolling independently in each
    would show one random gate at the paddock and animate a DIFFERENT one
    once the race starts, on top of the plain-array-index bug this whole
    parameter exists to fix in the first place."""
    course = _course_data()[str(course_set_id)]
    gates = list(gate_assignment) if gate_assignment else random.sample(
        range(gate_count or len(horses)), len(horses))
    spec = {
        "courseId": course_set_id,
        "groundCondition": GROUND_CONDITION_NAMES.get(ground_condition, "good"),
        "weather": WEATHER_NAMES.get(weather, "sunny"),
        "season": SEASON_NAMES.get(season, "spring"),
        "seed": seed if seed is not None else random.randint(0, 2**31 - 1),
        "horses": [build_horse_spec(h, course, gate_index=gates[i]) for i, h in enumerate(horses)],
    }
    result = None
    if not os.environ.get("UMA_DISABLE_RACE_ENGINE_RS"):
        try:
            result = _run_simulation_rust(spec)
        except Exception:
            log.warning("race_engine_rs failed; falling back to the Python engine", exc_info=True)
    if result is None:
        from .race_engine.race_runner import run_race
        result = run_race(spec)
    result["gate_assignment"] = gates
    # DERIVED HERE, not in either engine: both return the same frames+results,
    # and the Rust port is the PRIMARY path -- computing this inside
    # race_runner.run_race would have left it empty on every real race and
    # present only when the Python fallback happened to run.
    result["finalStretchOvertakes"] = _final_stretch_overtakes(
        course, result.get("frames") or [], result.get("results") or [])
    return result


def _final_stretch_overtakes(course, frames: list, results: list) -> list:
    """How many runners each horse passed over the FINAL STRETCH, per horse,
    indexed like the `horses` array.

    The final stretch is the last entry in course.straights -- the same region
    the is_last_straight skill conditions use, so "in the final stretch" means
    here exactly what it means to a skill. Each horse's place is counted the
    moment it ENTERS that straight and again at the finish, and the improvement
    between the two is how many runners it passed. Being passed clamps to 0: the
    only question ever asked of this is "overtook at least N" (epithets 85/160).

    Reads the frames the simulation already produced, so it costs one pass and
    never a second simulation."""
    n = len(results)
    # `course` is the raw course_data JSON dict here (run_simulation's own
    # _course_data()), but the same helper is useful against the parsed Course
    # object, so accept either shape.
    straights = (course.get("straights") if isinstance(course, dict)
                 else getattr(course, "straights", None)) or []
    if not frames or not straights or not n:
        return [0] * n
    last = straights[-1]
    entry = last["start"] if isinstance(last, dict) else last.start

    # First frame in which each horse is at or past the start of the final
    # straight. One that never reaches it is scored from the last frame instead
    # of being dropped.
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
    # finishOrder is 0-based; it also breaks ties below.
    final_place = [int(r["finishOrder"]) for r in results]
    out = []
    for i in range(n):
        f = entry_frame[i] or frames[-1]
        horses = f["horses"]
        # Place on entry = rank by position, ties broken by the FINAL order.
        # The tiebreak matters: horses can share a position to the last decimal
        # (a tightly packed field, or two runners the solver has moving
        # identically), and counting only strictly-ahead runners then reports
        # every one of them as 2nd -- which turns into a fake overtake for
        # everyone behind. Ordering a tied group the way it finished means a tie
        # contributes no movement, which is the honest reading: nobody passed.
        order = sorted(range(n), key=lambda j: (-horses[j]["pos"], final_place[j]))
        place_then = order.index(i)
        out.append(max(0, place_then - final_place[i]))
    return out


log = logging.getLogger("uma-server")


def _simulate_job(job: dict) -> dict:
    """One race, simulated AND encoded. Module-level (never a closure or a
    lambda) so it stays picklable for run_races' process pool."""
    sim = run_simulation(
        job["horses"], job["course_set_id"], job["ground"], job["weather"],
        job["season"], job["seed"], gate_count=job["gate_count"],
        gate_assignment=job["gate_assignment"])
    scenario_b64, frame_count, _raw = build_race_scenario(
        sim, job["n"], job["gate_count"])
    return {"sim": sim, "scenario": scenario_b64, "frame_count": frame_count}


# Races are only worth shipping to other processes when there is real work to
# spread; below this many skills in the field, spawning costs more than it saves.
_PARALLEL_SKILL_THRESHOLD = 400


def run_races(jobs: list, max_workers: int = 5) -> list:
    """Simulate several INDEPENDENT races, in parallel where it pays off.

    Each race is a pure function of (horses, course, conditions, seed) and the
    engine is seeded per race, so distributing them changes nothing about the
    results -- only the wall clock.

    Why this exists: team_stadium/start has to simulate a whole five-race
    match inside ONE request. The per-tick cost scales with the number of
    skills in the field, and a roster carrying several hundred skills per
    horse pushed a match to ~21s -- long enough that the client gave up and
    showed a network error, then retried, re-running the same five races
    again. Measured on such a roster: 21.6s sequential vs 8.2s across five
    workers, pool startup included.

    Any failure at all (a spawn that won't start, an unpicklable job, a
    worker that dies) falls back to running the jobs sequentially in-process,
    so the worst case is today's behaviour rather than a broken request."""
    if not jobs:
        return []
    # jobs carry raw trained_chara dicts (run_simulation builds the horse
    # specs itself), so the skill list is `skill_array` here -- `skills` is
    # only the name it has after build_horse_spec. Accept either.
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


# Real captured race_scenario blobs do NOT sample uniformly. Both engines step
# at 1/15s and sending every tick is suspected of making the client hang while
# loading the race, so frames must be thinned -- but thinning them EVENLY is
# what caused the reported gate-open flicker.
#
# BUG FIXED 2026-09-03 (live-reported: "right when the gates open there's a
# tiny flicker and then they are in their correct positions"). This kept a flat
# ~1.25 frames/second, derived from the AVERAGE density of a real blob. The
# average is real; the uniformity is not. Decoding the frame timestamps of 20
# real Cygames scenarios (every captures/*/*race_start.json carrying a
# race_scenario -- practice, daily, daily legend, main story, career live; 5- to
# 18-horse fields) shows one pattern with zero exceptions:
#
#   * the first 17 frames are EVERY tick, t = 0, 0.0666, ... 1.0656
#   * everything after that is every 16th tick (dt = 1.0656 = 16 * 0.0666)
#
# That leading block is exactly the start dash: a horse leaves the gate at
# 3 m/s and is past 11 m/s within a third of a second, so at a flat 0.8s sample
# the whole acceleration curve was two frames with a straight line between them
# for the client to interpolate through. That line is visibly wrong mid-dash and
# snaps as soon as the next frame lands -- the flicker, and why "then they are
# in their correct positions": nothing was wrong with the positions, only with
# what the client had to interpolate through to reach them.
#
# Real blobs sometimes ALSO carry a dense block at the very end (0 frames in 15
# of the 20, 20-39 in the other 5). Not reproduced: not consistent enough to
# copy, and no finish-line artifact is reported -- unlike the start, where all
# 20 agree exactly.
_DENSE_LEAD_FRAMES = 17   # ticks kept at full rate from the gate
_SPARSE_STRIDE = 16       # every Nth tick thereafter (1.0656s at 1/15s ticks)

# Both engines record a frame AFTER each step, so their first frame is t=1/15
# with the horses already a fifth of a metre out at ~3.8 m/s. Every real
# scenario instead opens with a t=0 frame: distance 0.0 and speed exactly 3.0
# for every horse, which is the solver's own starting current_speed. Without it
# the client has no gate-time anchor and extrapolates the opening moment
# backwards -- the other half of the flicker. Synthesized here rather than in
# the engines so their outputs stay identical to each other; this is a
# rendering concern, not physics.
_GATE_FRAME_SPEED = 3.0


def _downsample_frames(frames: list) -> list:
    """Real-server frame cadence: every tick through the start dash, every 16th
    tick after it. The final frame is always kept so the blob still covers the
    whole race -- real blobs show the same odd short interval at their tail."""
    if len(frames) <= 2:
        return frames
    last_idx = len(frames) - 1
    keep = {i for i in range(min(_DENSE_LEAD_FRAMES, len(frames)))}
    keep.update(range(0, len(frames), _SPARSE_STRIDE))
    keep.add(last_idx)
    return [frames[i] for i in sorted(keep)]


# Real scenarios seat horses in an even lateral spread across the gate, then
# converge toward the inside rail for the body of the race. The physics sim's
# own `lane` output does NOT model this (clustered, can exceed 0..1), so at
# gate-open the client -- which places horses by the FIRST frame's per-index
# lanes -- would snap them from the gate layout to those values. Synthesize the
# real gate->rail pattern instead. Distance/speed/finish are untouched.
#
# Decoded from EVERY real captured scenario (fields of 5, 9, 10, 11, 12, 13,
# 14, 15, 16, 17 and 18 horses -- captures/**/*race_start.json etc):
#  * gate spread = slot * (1/18), PLUS an extra 0.6-slot gap once the field
#    reaches slot 9, the center divider between the two physical gate blocks
#    (a full 18-gate start is two structures of 9; without this gap the right
#    block sits ~0.03 off and the horses "snap" closer when the animation
#    takes over). Frame 0 of every one of those captures is the same sequence
#    -- 0, 555, 1111 ... 4444, then the 889 jump to 5333 -- at EVERY field
#    size, because a starting stall is a fixed-width physical structure: a
#    16-gate start occupies less of the track width, it does not use wider
#    stalls or move the divider.
#  * the full spread is HELD ~3s at the gate, then converges over ~40s to a
#    near-rail floor (~0.06). (Real also fans back out in the final ~15s; not
#    reproduced here -- cosmetic, and only affects the finish, not the start.)
#
# BUG FIXED 2026-09-07 (live-reported: "right when the racecourse opens the
# umamusume teleport because their offsets apart from each other are wrong",
# on Teio Sho and other dirt races). The spacing above used to be 1/gate_count
# with the divider at gate_count//2, derived from the single 18-horse capture
# available at the time -- where 1/gate_count IS 1/18 and gate_count//2 IS 9,
# so it was exact there and wrong at every other field size. No dirt race in
# master.mdb has 18 gates (dirt is 9/12/14/15/16 only; 202 dirt races are
# 16-gate, Teio Sho among them), so dirt hit the wrong branch every single
# time: at 16 gates the field was stretched ~12% too wide (gate 16 at 9750
# instead of 8666) and the divider gap fired a slot early. The client seats
# the models at the real physical gate positions and then takes lanes from
# frame 0, so the whole field snapped sideways the instant the scenario took
# over. 12- and 16-gate TURF races (632 of them) had the same artifact; the
# 18-gate G1s most careers run did not, which is why it read as dirt-only.
_LANE_GATE_HOLD_S = 3.0
_LANE_CONVERGE_S = 40.0
_LANE_RAIL_FLOOR = 0.06
_LANE_CENTER_GAP = 0.6  # extra gate spacing (in base-spacing units) at the divider
_LANE_GATE_SLOTS = 18   # stall width is 1/18 of track width on EVERY course
_LANE_DIVIDER_SLOT = 9  # first slot of the second gate block


def _gate_spread(index: int) -> float:
    """Lateral gate position for wire slot `index`, at the real fixed stall
    spacing (1/18 of track width), with the center-divider gap from slot 9 on.
    Independent of the course's gate count and of the field size -- see the
    2026-09-07 note above."""
    s = 1.0 / _LANE_GATE_SLOTS
    extra = _LANE_CENTER_GAP * s if index >= _LANE_DIVIDER_SLOT else 0.0
    return index * s + extra


def _gate_lane(index: int, t: float) -> float:
    spread = _gate_spread(index)
    if t <= _LANE_GATE_HOLD_S:
        factor = 1.0
    elif t >= _LANE_CONVERGE_S:
        factor = _LANE_RAIL_FLOOR
    else:
        p = (t - _LANE_GATE_HOLD_S) / (_LANE_CONVERGE_S - _LANE_GATE_HOLD_S)
        factor = 1.0 - (1.0 - _LANE_RAIL_FLOOR) * p
    return spread * factor


def build_race_scenario(sim_result: dict, horse_num: int, gate_count: int | None = None) -> tuple[str, int, int]:
    """sim_result: output of run_simulation(). Returns (gzip+base64
    race_scenario, downsampled frame count, original frame count). gate_count
    (the course's full physical gate count) is accepted for call-site
    compatibility but no longer affects anything: lane spacing is a fixed
    1/18 of track width on every course (see _gate_spread).

    sim_result["gate_assignment"] (set by run_simulation), if present, maps
    horse array-index -> the actual randomized physical gate it was built
    with in the physics sim -- the visual gate-open spread below must use
    the SAME mapping, or the replay would show every horse lined up in
    array order while the simulated race (post_number-based skills,
    blocking, etc) ran them from their real randomized gates instead."""
    import base64
    import gzip

    gate_assignment = sim_result.get("gate_assignment") or list(range(horse_num))
    # BUG FIXED 2026-08-30 (live-reported: team_stadium/start 500ing every
    # time -- "IndexError: list assignment index out of range" in this
    # function). gate_assignment holds RAW physical gate numbers, which can
    # go up to gate_count-1 (the course's full gate count, e.g. 18) even
    # when far fewer horses are actually racing -- Team Stadium heats are
    # typically 2-6 horses on an 18-gate course, and decide_frame_order's own
    # gate roll floors at 8 slots regardless of field size. But every
    # fixed-size wire structure below (horse_frames, results, skill/race
    # event horse indices) has exactly `horse_num` slots -- encode_scenario
    # asserts this -- so indexing them with the raw gate number overruns the
    # array whenever gate_count/the roll's slot count exceeds horse_num,
    # which is the ordinary case here, not an edge case. Only the RELATIVE
    # gate order carries any wire meaning (confirmed by the one capture that
    # validated this: an 18-horse FULL field, where raw gate number and
    # compacted rank are indistinguishable) -- so rank_of maps each horse's
    # array index to its dense 0..horse_num-1 rank by ascending gate number,
    # and every wire-array index below uses that instead of the raw gate
    # number.
    #
    # BUG FIXED 2026-09-07 (same report as the _gate_lane spacing fix above):
    # _gate_lane used to be fed the RAW gate number, on the theory that a
    # smaller field should sit in its real scattered subset of the course's
    # gates. Real captures say otherwise -- frame 0 of every one of them is a
    # contiguous run from slot 0, i.e. a field of n always fills gates 1..n
    # with no holes, exactly as real racing does. Feeding the raw gate left
    # empty stalls scattered through the lineup whenever the field was
    # smaller than the course's gate count (e.g. 12 horses on a 16-gate dirt
    # course). The lane now uses rank_of too, so the visual lineup and the
    # wire slot agree; the raw gate still drives the physics sim, which is
    # where it actually carries meaning (post_number skills, blocking).
    rank_of = {orig_i: rank for rank, orig_i in
               enumerate(sorted(range(horse_num), key=lambda i: gate_assignment[i]))}
    sim_frames = sim_result["frames"]
    # The Rust engine thins the frame list itself and flags that it did, because
    # 92% of what it used to send was parsed here purely to be thrown away (see
    # downsample_result in race_engine_rs/src/main.rs). It also reports whether
    # the gate frame is needed, since once the list is thinned we can no longer
    # infer that from frames[0]["t"]. The Python fallback engine sets neither
    # flag and still goes through the full path below.
    pre_thinned = bool(sim_result.get("framesDownsampled"))
    needs_gate = sim_result.get("gateFrameNeeded")
    if needs_gate is None:
        needs_gate = bool(sim_frames and sim_frames[0]["t"] > 0)
    if needs_gate:
        sim_frames = [{
            "t": 0.0,
            "horses": [{"pos": 0.0, "speed": _GATE_FRAME_SPEED, "hp": h.get("hp")}
                       for h in sim_frames[0]["horses"]],
        }] + sim_frames
    raw_frames = sim_frames if pre_thinned else _downsample_frames(sim_frames)

    # DEFENSE: a NaN/Infinity anywhere in the JS sim arrives here as None
    # (JSON.stringify(NaN) === 'null'). One None used to TypeError below and
    # silently degrade the whole race to fixture replay. Carry the last good
    # value forward instead -- and fail loudly if a horse NEVER reports.
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

    # The sim FREEZES a horse's position the instant it finishes; real scenarios
    # instead have finishers COAST past the line (keep moving, gently slowing).
    # Freezing makes a runaway winner (e.g. the maxed uma, which finishes many
    # seconds ahead) appear to slam to a dead stop at the wire during the
    # post-finish grace frames. Detect each horse's finish and coast it
    # forward at its crossing speed for the remaining frames. Purely visual
    # -- finish order/time come from sim_result["results"] and are untouched.
    #
    # BUG FIXED 2026-08-29 (live-reported: umas slow to a near-stop right
    # before the finish line, then visibly speed back up crossing it).
    # Detection used to be a heuristic on the DOWNSAMPLED position data
    # (~1.25 samples/sec): "position barely moved between two consecutive
    # kept frames" was treated as "she must have finished". That's
    # ambiguous with a horse that's just genuinely moving SLOWLY for one
    # sampled interval (e.g. HP exhaustion forces min_speed near the end of
    # a race) -- a false-positive trigger there falsely freezes/coasts her
    # from that low crossing speed for the REST of the race, which looks
    # exactly like "slows to a stop", and produces a visible discontinuity
    # once her real (authoritative, already-computed) finish time is
    # reached and something else reconciles against it. Use
    # sim_result["results"][hi]["finishTimeRaw"] -- the physics engine's
    # own precise finish timestamp, already computed with no ambiguity --
    # directly instead of re-detecting it from lossy downsampled position
    # deltas. Position/speed at that exact moment come from the FULL
    # (non-downsampled) sim_result["frames"], not raw_frames.
    finish = {}  # horse idx -> (finish_pos, finish_speed, finish_t)
    # When the engine thinned the frames it also sent this lookup precomputed
    # at full resolution (finishFrames), because the search below needs every
    # tick and the thinned list no longer has them. Same values, found the
    # same way -- see downsample_result in race_engine_rs/src/main.rs.
    engine_finish = sim_result.get("finishFrames") if pre_thinned else None
    if engine_finish:
        for hi in range(horse_num):
            entry = engine_finish[hi] if hi < len(engine_finish) else None
            if entry:
                finish[hi] = (entry["pos"], entry["speed"], entry["t"])
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
                finish[hi] = (h["pos"], h["speed"], last_at_or_before["t"])

    # BUG FIXED 2026-08-29 (live-reported: skill popups, win credit, and
    # even huge unexplained speed bursts all showed up on whichever horse
    # was AT GATE 1, never on the player unless her own randomized gate
    # happened to BE 1). Root cause, found by decoding two real
    # proxy-captured practice_race/race_start races against the genuine
    # Cygames server: the per-tick horse-frame array (and, below, the
    # results array) is wire-ordered by PHYSICAL GATE, not by the
    # array/build order used everywhere else in this codebase (the same
    # order race_horse_data_array/skill/entry data use). Confirmed
    # unambiguously: a real capture's frame0 lane_q values are EXACTLY
    # 0, 555, 1111, ... 9777 in wire order -- gate 1's spread, then gate
    # 2's, then gate 3's, etc, strictly by wire slot -- while the SAME
    # capture's race_horse_data_array (identity data) lists the player
    # FIRST despite her real gate being 13. This server was building
    # frames/results in array order (matching race_horse_data_array),
    # so a horse's real movement/finish data landed in whatever wire
    # slot her ARRAY INDEX happened to equal, which the client reads as
    # a GATE NUMBER -- only ever correct by coincidence, when a horse's
    # array index and gate happened to match. Every per-tick horse slot
    # (and the results slot below) must be written at its WIRE rank
    # (`rank_of[idx]`, see above) -- not idx, and not the raw gate number
    # either: that capture was an 18-horse FULL field, where rank and raw
    # gate number are identical and so can't be told apart, but a smaller
    # field's raw gate number can exceed horse_num-1 (BUG FIXED
    # 2026-08-30, see rank_of's own comment above) while these arrays are
    # always exactly horse_num slots.
    frames = []
    for f in raw_frames:
        t = f["t"]
        horse_frames: list[HorseFrame | None] = [None] * horse_num
        for idx, h in enumerate(f["horses"]):
            pos, sp = h["pos"], h["speed"]
            fin = finish.get(idx)
            if fin is not None and t > fin[2]:
                fpos, fsp, ft = fin
                pos = fpos + fsp * (t - ft)  # coast past the line instead of freezing
                sp = fsp
            horse_frames[rank_of[idx]] = HorseFrame(
                distance=pos,
                lane_position=_gate_lane(rank_of[idx], t),
                speed=sp,
                hp=int(round(h["hp"])),
            )
        frames.append((t, horse_frames))
    # finish_diff_time = the gap to the horse that finished immediately ahead
    # (order N vs order N-1), as in real captures. Left at 0 the client shows
    # every margin as "Nose". Computed from finishTimeRaw (the precise crossing
    # time) NOT finishTime -- the latter is frame-quantized to 1/15s, which
    # collapses close finishers to identical times and reintroduces "Nose" ties.
    rawtime_by_order = {r["finishOrder"]: r["finishTimeRaw"] for r in sim_result["results"]}
    results: list[HorseResult | None] = [None] * horse_num
    for idx, r in enumerate(sim_result["results"]):
        prev_raw = rawtime_by_order.get(r["finishOrder"] - 1)
        diff_time = 0.0 if prev_raw is None else max(0.0, r["finishTimeRaw"] - prev_raw)
        results[rank_of[idx]] = HorseResult(
            finish_order=r["finishOrder"],
            finish_time=r["finishTime"],
            finish_diff_time=diff_time,
            start_delay_time=r["startDelayTime"],
            guts_order=0,
            wiz_order=0,
            last_spurt_start_distance=r["lastSpurtStartDistance"] if r["lastSpurtStartDistance"] >= 0 else -1.0,
            running_style=r["runningStyle"],
            defeat=0,
            finish_time_raw=r["finishTimeRaw"],
        )
    skill_events = [
        SkillEvent(
            horse_index=rank_of[e["horseIndex"]],
            skill_id=e["skillId"],
            t=e["t"],
            duration=e["duration"],
            is_unique=e["isUnique"],
        )
        for e in sim_result.get("skillEvents", [])
        # "kakari" (rushing) isn't a real skill id -- run_simulation emits it
        # as a non-numeric sentinel so team_stadium's PvP scoring can see
        # Rushed events (team_stadium_raw_score condition_type 5/6). It was
        # briefly ALSO sent to the client as its own RaceEvent type=4, then
        # dropped again (see the race_events comment below) -- the wire
        # scenario format's SKILL record specifically packs skill_id as an
        # integer (struct.error otherwise) and has no slot for a status
        # effect like this anyway, so it's simply excluded here.
        if isinstance(e["skillId"], int)
    ]
    # type=5 (Finished): one per horse, always -- verified real and kept
    # since it was added. type=4 (Kakari/Rushing start) was ALSO added
    # alongside type=5 at first, then reverted: this server's opponent field
    # (all scaled to a near-identical, extreme stat level against a maxed
    # player) made kakari over-trigger massively (17 of 18 horses
    # simultaneously in one test, vs 2-3 in every real capture) landing
    # right around the gate-lineup animation's timing window. The stat-cap
    # fix (race_solver_builder.py's _base_stat, added later the same
    # session) should have normalized kakari's real trigger rate along with
    # everything else it was distorting -- worth re-testing before
    # re-adding type=4, not done yet.
    #
    # type=6 -- THE ACTUAL FIX for "skill popups / win credit only work when
    # the player's gate happens to be 1". Confirmed via two real
    # proxy-captured practice_race/race_start races against the genuine
    # Cygames server (gates 13 and 14): both carry exactly ONE type=6 event,
    # paramCount=1, whose single param is the PLAYER's own gate index
    # (0-based) -- e.g. frame_order=13 -> params=(12,), frame_order=14 ->
    # params=(13,). This server never sent this event type at all. The
    # client evidently uses it to learn "this horse index is you" for
    # everything identity-dependent downstream (skill popups, win
    # highlighting) -- silently defaulting to gate 1 when it never arrives
    # would exactly explain the reported symptom, including why it "worked"
    # whenever the player's real gate happened to already be 1. Timing in
    # both real captures lines up with the PLAYER's own last-spurt start
    # (not a fixed race-clock moment), which run_race already tracks
    # per-tick and returns as playerLastSpurtTime.
    race_events = [
        RaceEvent(t=r["finishTime"], type_id=5, params=(rank_of[i],))
        for i, r in enumerate(sim_result["results"])
    ]
    player_spurt_t = sim_result.get("playerLastSpurtTime")
    if player_spurt_t is not None and horse_num > 0:
        race_events.append(RaceEvent(t=player_spurt_t, type_id=6, params=(rank_of[0],)))
    blob = encode_scenario(horse_num, frames, results, skill_events=skill_events, race_events=race_events)
    scenario_b64 = base64.b64encode(gzip.compress(blob)).decode("ascii")
    # rawFrameCount: the engine's ORIGINAL tick count, which sim_result["frames"]
    # no longer reports once the engine has thinned the list itself.
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
) -> dict | None:
    """Runs a full practice race for player_chara + real opponents. Field size =
    the player-requested `entry_num` (practice races let you pick fewer than a
    full field), capped by the course's physical gate count and the opponent
    pool; falls back to the course's own entry_num when unset. Returns
    {"race_scenario": ..., "results": [...], ...} or None if the course can't
    be resolved.

    gate_assignment: see run_simulation -- pass the SAME permutation shown at
    an earlier paddock/entry screen so the animation starts each horse where
    that screen already said it would.

    extra_player_charas: additional player-owned entries (practice races let
    the player enter more than one of their own umas at once -- confirmed
    real via a proxy-captured request/response against the genuine Cygames
    server, up to 5 seen in one race). Placed in the field right after
    player_chara, before any opponent, so callers can rely on
    horses[0:1+len(extra_player_charas)] being every player entry in
    submission order. Every existing caller omits this (defaults to None),
    so single-player-entry behaviour is completely unchanged."""
    course_info = get_course_for_race_instance(race_instance_id)
    if course_info is None:
        return None
    course_set_id, _course, course_entry_num = course_info

    primary = [player_chara] + list(extra_player_charas or [])
    field = entry_num or course_entry_num
    field = max(len(primary), min(field, course_entry_num, len(primary) + len(opponents)))
    horses = primary + opponents[: field - len(primary)]
    sim_result = run_simulation(horses, course_set_id, ground_condition, weather, season, seed,
                                 gate_count=course_entry_num, gate_assignment=gate_assignment)
    # gate_count no longer affects lane spacing (fixed 1/18 stalls, see
    # _gate_spread); still passed for call-site compatibility.
    scenario_b64, frame_count, raw_frame_count = build_race_scenario(sim_result, len(horses), course_entry_num)
    return {
        "race_scenario": scenario_b64,
        "frame_count": frame_count,
        "raw_frame_count": raw_frame_count,
        "sim_results": sim_result["results"],
        "seed": sim_result["seed"],
        "course_set_id": course_set_id,
        "horses": horses,
        # The SAME randomized gate permutation build_race_scenario just used
        # for the in-race lane_position frames -- callers need it too, for
        # race_horse_data_array[].frame_order (the PRE-race gate lineup).
        # Without this, frame_order defaults to plain array index (player
        # always index 0 -> always "gate 1" before the race starts), while
        # the animation itself correctly starts her in the random gate --
        # live-reported as "always in gate 1, then randomly teleports to her
        # actual gate" the moment the race animation kicks in.
        "gate_assignment": sim_result.get("gate_assignment"),
        # THE RAW EVENT STREAM, passed straight through. Not used to build the
        # wire scenario (the SkillEvent list above is the filtered, re-indexed
        # version that goes to the client) -- this is for callers that need to
        # know what actually HAPPENED in the race: which skills each horse fired
        # and when it was Rushed. team_stadium.py already reads the same stream
        # off run_simulation directly for its PvP scoring; career races get it
        # here, where two epithets need it ("Use at least 13 skills in a single
        # race", and the win-while-Rushed family).
        #
        # Entries are {horseIndex, skillId, t, duration, isUnique}, indexed by
        # the horses array above, and skillId is the non-numeric sentinel
        # "kakari" for a Rushed episode rather than a real skill.
        "skill_events": sim_result.get("skillEvents") or [],
        # Runners each horse passed over the final straight, indexed like
        # `horses` (see race_runner._final_stretch_overtakes).
        "final_stretch_overtakes": sim_result.get("finalStretchOvertakes") or [],
    }
