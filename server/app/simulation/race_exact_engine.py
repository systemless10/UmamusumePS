from __future__ import annotations

import atexit
import base64
import gzip
import functools
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading

from .. import master_data, region
from .scenario_format import HorseResult, encode_result

_SERVER = Path(__file__).resolve().parents[2]
_TABLES = (
    "skill_data", "skill_level_value", "race_proper_distance_rate",
    "race_proper_ground_rate", "race_proper_runningstyle_rate", "race_motivation_rate",
    "race_course_set_status", "race_single_mode_team_status", "race_track",
    "race_course_set", "race", "race_instance",
)
_proc = None
_proc_region = None
_work_dir = None
_pid = os.getpid()
_lock = threading.Lock()


@functools.lru_cache(maxsize=None)
def _is_unique(skill_id):
    row = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (skill_id,))
    return bool(row and row["rarity"] in (3, 4, 5))


def _close_worker():
    global _proc, _work_dir
    if _pid != os.getpid():
        return
    if _proc is not None:
        if _proc.poll() is None:
            _proc.kill()
        _proc.wait(timeout=2)
        for stream in (_proc.stdin, _proc.stdout):
            if stream is not None:
                stream.close()
        _proc = None
    if _work_dir is not None:
        shutil.rmtree(_work_dir, ignore_errors=True)
        _work_dir = None


atexit.register(_close_worker)


def _worker():
    global _proc, _proc_region, _work_dir
    current = region.CURRENT_REGION.get()
    if _proc is not None and _proc.poll() is None and _proc_region == current:
        return _proc
    _close_worker()
    _proc_region = current
    executable = Path(os.environ.get("UMA_RACE_EXACT_RS_EXE") or
                      _SERVER / "race_exact_rs/target/release" /
                      ("race_exact_rs.exe" if os.name == "nt" else "race_exact_rs"))
    assets = Path(os.environ.get("UMA_RACE_EXACT_ASSETS") or
                  _SERVER / ("data/race_param_define.json" if current == "jp"
                            else "data/race_param_define_global.json"))
    _work_dir = tempfile.mkdtemp(prefix=f"uma-{current}-race-")
    master = Path(_work_dir) / "master.json"
    master.write_text(json.dumps({table: [dict(row) for row in master_data.query(
        f'SELECT * FROM "{table}"')] for table in _TABLES}), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("RX_")}
    _proc = subprocess.Popen(
        [str(executable), "serve", "--master", str(master), "--assets", str(assets)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", env=env,
    )
    return _proc


def run_input(race_input: dict) -> dict:
    global _pid, _proc, _work_dir, _lock
    if _pid != os.getpid():
        for stream in ((_proc.stdin, _proc.stdout) if _proc is not None else ()):
            stream.close()
        _pid, _proc, _work_dir, _lock = os.getpid(), None, None, threading.Lock()
    payload = json.dumps(race_input, allow_nan=False)
    with _lock:
        for _ in range(2):
            proc = _worker()
            timeout = max(30.0, 0.002 * len(race_input["horses"]) ** 2)
            watchdog = threading.Timer(timeout, proc.kill)
            watchdog.start()
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (BrokenPipeError, OSError, ValueError):
                line = ""
            finally:
                watchdog.cancel()
            if not line:
                _close_worker()
                continue
            result = json.loads(line)
            if "error" in result:
                raise RuntimeError(f"race_exact_rs: {result['error']}")
            return result
    raise RuntimeError("race_exact_rs worker failed twice")


def build_input(horses, course_set_id, ground, weather, season, seed, gates,
                single_mode=False, race_context=None):
    context = dict(race_context or {})
    instance_id = context.get("race_instance_id")
    if instance_id:
        instance = master_data.query_one("SELECT * FROM race_instance WHERE id=?", (instance_id,))
        race = master_data.query_one("SELECT * FROM race WHERE id=?", (instance["race_id"],)) if instance else None
    else:
        race = master_data.query_one("SELECT * FROM race WHERE course_set=? ORDER BY id LIMIT 1", (course_set_id,))
        instance = master_data.query_one("SELECT * FROM race_instance WHERE race_id=? ORDER BY id LIMIT 1", (race["id"],)) if race else None
    if race is None or instance is None or race["course_set"] != course_set_id:
        raise ValueError(f"Cannot resolve exact race metadata for course {course_set_id}")
    gate_to_original = sorted(range(len(horses)), key=lambda i: gates[i])
    popularity = sorted(range(len(horses)), key=lambda i: (-horses[i].get("speed", 0), i))
    prepared = []
    for rank, i in enumerate(gate_to_original):
        h = dict(horses[i])
        h["frame_order"] = rank + 1
        for name, alias in (("speed", "speed"), ("stamina", "stamina"), ("pow", "power"), ("guts", "guts"), ("wiz", "wisdom")):
            h[name] = int(h.get(name, h.get(alias, 1)) or 1)
        for name in ("proper_distance_short", "proper_distance_mile", "proper_distance_middle", "proper_distance_long",
                     "proper_running_style_nige", "proper_running_style_senko", "proper_running_style_sashi", "proper_running_style_oikomi",
                     "proper_ground_turf", "proper_ground_dirt"):
            h[name] = int(h.get(name, 7) or 7)
        h["running_style"] = int(h.get("running_style") or 2)
        h["motivation"] = int(h.get("motivation") or 3)
        h["chara_id"] = int(h.get("chara_id") or (h.get("card_id") or 0) // 100)
        v = h.get("viewer_id")
        if isinstance(v, int) and not isinstance(v, bool):
            h["viewer_id"] = v
        elif isinstance(v, str) and v.strip().isdigit():
            h["viewer_id"] = int(v)
        elif v:
            h["viewer_id"] = 1
        else:
            h["viewer_id"] = 0 if "viewer_id" in h else int(h.get("is_viewer", i == 0))
        h["popularity"] = int(h.get("popularity") or popularity.index(i) + 1)
        h["skill_array"] = [{"skill_id": int(sk["skill_id"]), "level": int(sk.get("level") or 1)} for sk in h.get("skill_array") or []]
        prepared.append(h)
    return {
        "replay_sha": "live", "logged_seed": seed, "seed": seed, "seed_mode": "logged",
        "region": "jp" if region.CURRENT_REGION.get() == "jp" else "global", "race_type": int(context.get("race_type", 6 if single_mode else 9)),
        "race_instance_id": instance["id"], "race_id": race["id"], "course_set": course_set_id,
        "season": season, "weather": weather, "ground_condition": ground, "horses": prepared,
    }, gate_to_original


def _horse_distance_at(frames, horse, t):
    if horse < 0 or not frames or t <= 0:
        return 0.0
    best = min(frames, key=lambda f: abs(f["t"] - t))
    hs = best["horses"]
    return float(hs[horse]["distance"]) if horse < len(hs) else 0.0


def encode_snapshot(snapshot):
    n = snapshot["runner_count"]
    jp = snapshot["version"] >= 100000005
    if jp:
        out = bytearray(struct.pack("<iifiiiii", 4, snapshot["version"],
            snapshot["distance_diff_max"], n, 12, 0, len(snapshot["frames"]), 4 + 12 * n))
    else:
        out = bytearray(struct.pack("<iifiiiiii", 4, snapshot["version"],
            snapshot["distance_diff_max"], n, 12, 31, 0, len(snapshot["frames"]), 4 + 12 * n))
    for frame in snapshot["frames"]:
        out.extend(struct.pack("<f", frame["t"]))
        for h in frame["horses"]:
            out.extend(struct.pack("<fhhhbb", h["distance"], *(max(-32768, min(32767, h[k])) for k in ("lane", "speed", "hp")),
                                   max(-128, min(127, h["tempt"])), max(-128, min(127, h["block"]))))
    out.extend(struct.pack("<i", 0))
    if jp:
        out.extend(struct.pack(f"<{n}i", *[39] * n))
    for r in snapshot["results"]:
        out.extend(encode_result(HorseResult(r["order"], r["time"], r["diff"], r["delay"],
            r["guts_order"], r["wiz_order"], r["last_spurt"], r["style"], r["defeat"], r["raw_time"])))
        if jp:
            out.extend(struct.pack("<fi", 0.0, 0))
    out.extend(struct.pack("<ii", 0, len(snapshot["events"])))
    for e in snapshot["events"]:
        params = [(int(v) + 2**31) % 2**32 - 2**31 for v in e["params"]]
        if not jp:
            out.extend(struct.pack(f"<hfBB{len(params)}i", 6 + 4 * len(params), e["t"], e["type"], len(params), *params))
            continue
        if e["type"] == 3:
            params = (params + [0] * 9)[:9]
        dist = _horse_distance_at(snapshot["frames"], params[0] if params else -1, e["t"])
        out.extend(struct.pack(f"<hfBB{len(params)}iBff", 15 + 4 * len(params), e["t"], e["type"], len(params),
                               *params, 1, dist, dist))
    return base64.b64encode(gzip.compress(out)).decode("ascii")


def adapt_result(snapshot, seed, course_set_id, gates, gate_to_original, horses):
    n = len(horses)
    frames = []
    for frame in snapshot["frames"]:
        runners = [None] * n
        for wire, h in enumerate(frame["horses"]):
            runners[gate_to_original[wire]] = {"pos": h["distance"], "speed": h["speed"] / 100,
                "lane": h["lane"] / 10000, "hp": h["hp"], "tempt": h["tempt"],
                "block": gate_to_original[h["block"]] if h["block"] >= 0 else -1}
        frames.append({"t": frame["t"], "horses": runners})
    results = [None] * n
    for wire, r in enumerate(snapshot["results"]):
        results[gate_to_original[wire]] = {"finishOrder": r["order"], "finishTime": r["time"],
            "finishTimeRaw": r["raw_time"], "finishTimeDisplay": r["time"], "finishDiffDisplay": r["diff"],
            "startDelayTime": r["delay"], "gutsOrder": r["guts_order"], "wizOrder": r["wiz_order"],
            "lastSpurtStartDistance": r["last_spurt"], "runningStyle": r["style"], "defeat": r["defeat"]}
    skills, events = [], []
    for e in snapshot["events"]:
        if e["type"] == 3:
            wire, skill, duration, _, mask, _ = e["params"]
            skills.append({"horseIndex": gate_to_original[wire], "skillId": skill, "t": e["t"],
                "duration": duration / 10000 if duration >= 0 else -1,
                "targets": [gate_to_original[j] for j in range(n) if mask & (1 << j)],
                "isUnique": _is_unique(skill)})
        elif e["params"]:
            events.append({"horseIndex": gate_to_original[e["params"][0]], "type": e["type"], "t": e["t"]})
    for e in snapshot.get("rushing_events", []):
        skills.append({"horseIndex": gate_to_original[e["horse"]], "skillId": "kakari",
            "t": e["t"], "duration": e["duration"], "isUnique": False})
    skills.sort(key=lambda e: e["t"])
    return {"seed": seed, "engine": "race_exact_rs", "frames": frames, "results": results,
        "skillEvents": skills, "raceEvents": events, "distanceDiffMax": snapshot["distance_diff_max"],
        "diagnostics": snapshot["diagnostics"], "gate_assignment": gates, "course_set_id": course_set_id,
        "race_scenario": encode_snapshot(snapshot), "framesDownsampled": True, "gateFrameNeeded": False}


def run_simulation(horses, course_set_id, ground, weather, season, seed, gates, single_mode=False, race_context=None):
    race_input, order = build_input(horses, course_set_id, ground, weather, season, seed, gates, single_mode, race_context)
    return adapt_result(run_input(race_input), seed, course_set_id, gates, order, horses)
