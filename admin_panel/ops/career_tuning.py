
from __future__ import annotations

import json
import os

from app import config, region, tuning

from . import op
from .common import OpError

STATS = ("speed", "stamina", "power", "guts", "wiz", "skill_point")

KNOBS = [
    dict(key="training_gain_multiplier", group="Training", kind="mult", label="Training gain",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="Stat and skill-point gains from every training. Stat caps still apply.",
         when="Next training screen"),
    dict(key="training_stat_tuning", group="Training", kind="perstat", label="Per-stat gains",
         help="For each stat pick Multiply (its training gain × a number, on top of Training gain) or Add (a flat amount on every training that already gives that stat).",
         when="Next training screen"),
    dict(key="failure_rate_shift", group="Training", kind="shift", label="Failure rate",
         range=[-100, 100], ticks=[-100, -50, 0, 50, 100], step=1, min=-100, max=100,
         names={-100: "Never", 0: "Normal", 100: "Always"}, luck_locked=True,
         help="Moves every training's failure chance toward never (left) or always (right). Works even where the real chance is 0%. The % on each button is the one rolled.",
         when="Next training screen"),
    dict(key="energy_cost_multiplier", group="Training", kind="mult", label="Energy use",
         range=[0, 3], ticks=[0, 1, 2, 3], step=0.05, min=0, max=10,
         help="Energy each training costs. Wit's energy recovery is unchanged.",
         when="Next training screen"),
    dict(key="bond_gain_multiplier", group="Training", kind="mult", label="Friendship gain",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="How fast support-card bonds fill from training.",
         when="Next training screen"),
    dict(key="hint_rate_multiplier", group="Training", kind="mult", label="Hint chance",
         range=[0, 15], ticks=[0, 1, 5, 10, 15], step=0.1, min=0, max=100, luck_locked=True,
         help="How often a placed card shows a skill hint (!).",
         when="Next turn"),
    dict(key="specialty_priority_bonus", group="Training", kind="int", label="Cards on their own type",
         range=[0, 500], ticks=[0, 100, 200, 300, 400, 500], step=5, min=0, max=10000, luck_locked=True,
         help="Extra pull for each support card toward its own facility. A card's real pull is about 20-100.",
         when="Next turn"),
    dict(key="skill_point_multiplier", group="Rewards", kind="mult", label="Skill points",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="Skill points from every source: training, events, races, sparks and scenario payouts. Training buttons show it; event and race popups show the normal number but your total gets the extra.",
         when="Next skill-point gain"),
    dict(key="fan_gain_multiplier", group="Rewards", kind="mult", label="Fan gain",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="Fans from every career race, after the trainee's own fan bonuses.",
         when="Next race"),
    dict(key="spark_proc_multiplier", group="Sparks", kind="mult", label="Inspiration spark chance (mid-career)",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="At the turn-31 and turn-55 inspirations, how likely each spark on the parents and grandparents is to fire. Real chances: blue ~70-90%, white skill 3-9%, pink 1-5% (higher with better compatibility). Luck also tilts it.",
         when="Next inheritance (turns 31 and 55)"),
    dict(key="legacy_spark_multiplier", group="Sparks", kind="mult", label="New sparks earned (career end)",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100,
         help="When a career ends, how likely each learned skill, G1 win and the scenario is to become a spark on the finished uma (real: ~20% each). Speed/Stamina/... (blue), aptitude (pink) and the unique spark are always passed down.",
         when="Next career end"),
    dict(key="legacy_star_shift", group="Sparks", kind="shift", label="Stars on new sparks (career end)",
         range=[-100, 100], ticks=[-100, -50, 0, 50, 100], step=1, min=-100, max=100,
         names={-100: "Always 1★", 0: "Normal", 100: "Always max ★"},
         help="Tilts the 1★/2★/3★ roll of every spark the finished uma gets. Every 25 points doubles the odds per star; the ends are absolute. A star the game can't give stays impossible (blue 3★ needs that stat at 600+).",
         when="Next career end"),
    dict(key="luck", group="Luck & events", kind="shift", label="Luck",
         range=[-100, 100], ticks=[-100, -50, 0, 50, 100], step=1, min=-100, max=100,
         names={-100: "Cursed", -50: "Unlucky", 0: "Normal", 50: "Lucky", 100: "Blessed"},
         help="Universal luck. Good rolls (hints, card placement, support and trainee events, spark procs, good event outcomes) get likelier, bad rolls (failure, bad conditions, Practice Poor, negative race skills) rarer; left is the reverse. Every 25 points doubles or halves the odds; the ends are absolute. While Luck is on it replaces Failure rate, Hint chance, Cards on their own type, Event rate and Bad-condition procs.",
         when="Next roll"),
    dict(key="event_rate_multiplier", group="Luck & events", kind="mult", label="Event rate",
         range=[0, 5], ticks=[0, 1, 2, 3, 4, 5], step=0.05, min=0, max=100, luck_locked=True,
         help="How often random events trigger: support-card events and chains, after-race events, trainee random events and Pal unlocks. Story and scripted events always play.",
         when="Next turn"),
    dict(key="condition_proc_multiplier", group="Luck & events", kind="mult", label="Bad-condition procs",
         range=[0, 3], ticks=[0, 1, 2, 3], step=0.05, min=0, max=100, luck_locked=True,
         help="How often bad conditions (Night Owl, Slacker, Migraine…) act up each turn.",
         when="Next turn"),
    dict(key="rival_strength_multiplier", group="Races", kind="mult", label="Rival strength",
         range=[0.25, 2.5], ticks=[0.25, 1, 1.5, 2, 2.5], step=0.05, min=0.1, max=5,
         help="Stats of every opponent in career races (mobs, named rivals, finals rivals), capped at 2000.",
         when="Next race"),
    dict(key="race_fatigue_multiplier", group="Races", kind="mult", label="Race fatigue",
         range=[0, 3], ticks=[0, 1, 2, 3], step=0.05, min=0, max=10,
         help="Energy a career race takes afterwards. The after-race event shows the new number.",
         when="Next race"),
    dict(key="force_training_gain", group="Exact pins (testing)", kind="int", label="Pin training gain",
         min=0, max=100000, unset="off",
         help="Every training gives exactly this much of the stats ticked below (all of them if none are ticked). Unticked stats keep the formula.",
         when="Next training screen"),
    dict(key="force_training_gain_stats", group="Exact pins (testing)", kind="stats", label="Pin applies to",
         help="None ticked = all five stats and skill points.",
         when="Next training screen"),
    dict(key="force_failure_rate", group="Exact pins (testing)", kind="int", label="Pin failure rate %",
         min=0, max=100, unset="off",
         help="Every training's failure chance is exactly this. Wins over Failure rate and Luck.",
         when="Next training screen"),
]
if region.PROCESS_REGION == "global":
    KNOBS.append(dict(key="race_simulator", group="Races", kind="select", label="Race simulator",
                      options=[["exact_test", "Default"], ["existing", "Old simulator"]],
                      help="Default is the 1:1 engine; any failure falls back to the old one.",
                      when="Next race"))
BY_KEY = {k["key"]: k for k in KNOBS}


def _read() -> dict:
    try:
        with open(config.CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except FileNotFoundError:
        return {}
    except ValueError as ex:
        raise OpError(f"{config.CONFIG_PATH} isn't valid JSON: {ex}") from ex


@op("tuning.get")
def get(viewer, args):
    data = _read()
    return {"path": config.CONFIG_PATH, "stats": list(STATS), "luck_overrides": list(tuning.LUCK_OVERRIDES),
            "knobs": [dict(k, value=data.get(k["key"])) for k in KNOBS]}


def _clean(k: dict, v):
    if v is None or v == "" or v == []:
        return None
    kind, label = k["kind"], k["label"]
    if kind == "select":
        if v not in [o[0] for o in k["options"]]:
            raise OpError(f"{label}: unknown option {v}.")
        return v
    if kind == "perstat":
        if not isinstance(v, dict):
            raise OpError(f"{label}: expected a table of stats.")
        out = {}
        for st, r in v.items():
            if st not in STATS or not isinstance(r, dict):
                raise OpError(f"{label}: unknown stat {st}.")
            try:
                x, add = float(r.get("x", 1)), int(float(r.get("add", 0)))
            except (TypeError, ValueError) as ex:
                raise OpError(f"{label}: {st} needs numbers.") from ex
            if not (0 <= x <= 100 and -1000 <= add <= 10000):
                raise OpError(f"{label}: {st} × must be 0-100 and + between -1000 and 10000.")
            if x != 1 or add:
                out[st] = {"x": x, "add": add}
        return out or None
    if kind == "stats":
        bad = [s for s in v if s not in STATS] if isinstance(v, list) else [v]
        if bad:
            raise OpError(f"{label}: unknown stat {bad[0]}.")
        return [s for s in STATS if s in v]
    try:
        v = float(v) if kind == "mult" else int(float(v))
    except (TypeError, ValueError) as ex:
        raise OpError(f"{label} must be a number.") from ex
    if not k["min"] <= v <= k["max"]:
        raise OpError(f"{label} must be between {k['min']} and {k['max']}.")
    if (kind == "mult" and v == 1) or (kind == "shift" and v == 0) or (kind == "int" and v == 0 and "unset" not in k):
        return None
    return v


@op("tuning.set", label="Career tuning")
def set_(viewer, args):
    data = _read()
    for key, v in (args.get("values") or {}).items():
        if key not in BY_KEY:
            raise OpError(f"Unknown setting {key}.")
        v = _clean(BY_KEY[key], v)
        if v is None:
            data.pop(key, None)
        else:
            data[key] = v
    tmp = config.CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, config.CONFIG_PATH)
    return get(viewer, {})
