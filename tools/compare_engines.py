r"""Differential harness: the pure-Python race engine vs the Rust port.

Run from `server/` with the project venv:

    .\.venv\Scripts\python.exe ..\tools\compare_engines.py [N]
    .\.venv\Scripts\python.exe ..\tools\compare_engines.py diagnose [i]

Generates N random race specs (fixed master seed, so the corpus is the same
every run), simulates each through `race_engine.race_runner.run_race()`
in-process AND through the `race_engine_rs` binary over stdin/stdout, and
reports how many came back identical.

Compare by `==`, never by a text digest of the JSON: the Rust binary emits
`5.0` where Python emits `5` for the same number, which is equal as a value
and different as text.

Expected result: high-80s to mid-90s percent exact. The rest is the known,
documented residual -- a threshold skill condition that reads live field state
(order_rate/near_count) flipping on sub-epsilon float noise. Those show up as a
skillEvents-only difference with identical `frames` and `results`, i.e. no
effect on the race. A drop well below that band, or a difference with a nonzero
finish-time delta, means something actually regressed.

`diagnose i` re-runs spec i alone and prints which top-level key differs and
the first differing element, which is the fast way to tell "same class of
noise" from "real bug".
"""
import json
import os
import random
import subprocess
import sys

_SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_SERVER, "server"))

from app.simulation.race_simulator import RACE_ENGINE_RS_EXE, _course_data  # noqa: E402
from app.simulation.race_engine.race_runner import run_race                # noqa: E402

_SKILL_DATA = os.path.join(_SERVER, "server", "app", "simulation", "race_engine",
                           "data", "skill_data.json")
SKILLS = json.load(open(_SKILL_DATA, encoding="utf-8"))
SKILL_IDS = list(SKILLS)

STRATS = ["Nige", "Senkou", "Sasi", "Oikomi"]
APT = ["S", "A", "B", "C", "D"]
GROUND = ["good", "yielding", "soft", "heavy"]
WEATHER = ["sunny", "cloudy", "rainy", "snowy"]
SEASON = ["spring", "summer", "autumn", "winter"]


def with_effect_type(type_id: int) -> list:
    """Skill ids carrying an effect of this type -- for aiming the corpus at a
    newly implemented effect instead of hoping a random draw picks one up."""
    return [sid for sid, s in SKILLS.items()
            for alt in s["alternatives"] for e in alt["effects"]
            if e["type"] == type_id]


def spec(rng, course_ids, force_skills=()):
    cid = rng.choice(course_ids)
    n = rng.choice([9, 12, 18])
    horses = []
    for i in range(n):
        sk = rng.sample(SKILL_IDS, rng.randint(0, 6))
        if force_skills and i < 3:
            sk = sk + [rng.choice(list(force_skills))]
        horses.append({
            "speed": rng.randint(300, 1200), "stamina": rng.randint(300, 1200),
            "power": rng.randint(300, 1200), "guts": rng.randint(300, 1200),
            "wisdom": rng.randint(300, 1200),
            "strategy": rng.choice(STRATS),
            "distanceAptitude": rng.choice(APT), "surfaceAptitude": rng.choice(APT[:3]),
            "strategyAptitude": rng.choice(APT), "mood": rng.randint(-2, 2),
            "skills": [{"skillId": s, "level": rng.randint(1, 10), "isUnique": False}
                       for s in sk],
            "gateIndex": i,
        })
    return {"courseId": int(cid), "groundCondition": rng.choice(GROUND),
            "weather": rng.choice(WEATHER), "season": rng.choice(SEASON),
            "seed": rng.randint(0, 2 ** 31 - 1), "horses": horses}


def _corpus(n, force_skills=()):
    rng = random.Random(20260903)
    course_ids = list(_course_data())
    for i in range(n):
        yield i, spec(rng, course_ids, force_skills if i % 2 == 0 else ())


def _rust(sp):
    p = subprocess.run([str(RACE_ENGINE_RS_EXE)], input=json.dumps(sp),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:400])
    return json.loads(p.stdout)


def main(n=40, force_skills=()):
    exact = 0
    for i, sp in _corpus(n, force_skills):
        py = run_race(json.loads(json.dumps(sp)))
        try:
            rs = _rust(sp)
        except RuntimeError as e:
            print(f"{i}: RUST FAILED {e}")
            continue
        if py == rs:
            exact += 1
            continue
        worst = max(abs((a.get("finishTime") or 0) - (b.get("finishTime") or 0))
                    for a, b in zip(py["results"], rs["results"]))
        print(f"{i}: DIFF  max finish-time delta {worst:.4f}s  "
              f"skillEvents {len(py['skillEvents'])} vs {len(rs['skillEvents'])}")
    print(f"exact: {exact}/{n}")


def diagnose(idx, force_skills=()):
    sp = None
    for i, s in _corpus(idx + 1, force_skills):
        sp = s
    py = run_race(json.loads(json.dumps(sp)))
    rs = _rust(sp)
    for k in sorted(set(py) | set(rs)):
        a, b = py.get(k), rs.get(k)
        size = (len(a), len(b)) if isinstance(a, list) and isinstance(b, list) else ""
        print(k, "equal" if a == b else "DIFFERS", size)
    for k in ("results", "skillEvents"):
        for j, (a, b) in enumerate(zip(py[k], rs[k])):
            if a != b:
                print(f"  {k}[{j}] py={a}")
                print(f"  {k}[{j}] rs={b}")
                break
    for j, (a, b) in enumerate(zip(py["frames"], rs["frames"])):
        if a != b:
            print("first differing frame", j)
            print("  py", json.dumps(a)[:400])
            print("  rs", json.dumps(b)[:400])
            break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "diagnose":
        diagnose(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
