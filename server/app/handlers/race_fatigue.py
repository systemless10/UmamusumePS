
import random

from . import conditions

FATIGUE_SUFFIX = 508
REPEATED_SUFFIX = 509

_MOOD_DOWN = {3: (0.60, 0.60), 4: (1.00, 1.00)}
_SKIN_OUTBREAK = {3: (0.15, 0.25), 4: (0.33, 0.33)}
_STAT_PENALTY = {3: (0.00, 0.00), 4: (0.40, 0.40)}

MIN_STREAK = min(_MOOD_DOWN)

STAT_PENALTY_VALUE = -10
STAT_PENALTY_COUNT = 3
_STATS = ("speed", "stamina", "power", "guts", "wisdom")


def consecutive_races(race_history, turn) -> int:
    try:
        turn = int(turn)
    except (TypeError, ValueError):
        return 0
    turns = set()
    for h in race_history or ():
        try:
            turns.add(int(h.get("turn")))
        except (TypeError, ValueError, AttributeError):
            continue
    turns.add(turn)
    streak = 0
    while turn - streak in turns:
        streak += 1
    return streak


def _row(table: dict, streak: int, energy) -> float:
    key = min(streak, max(table))
    if key not in table:
        return 0.0
    has_energy, no_energy = table[key]
    try:
        broke = int(energy) <= 0
    except (TypeError, ValueError):
        broke = False
    return no_energy if broke else has_energy


def roll(streak: int, energy, rng=None) -> dict | None:
    rng = rng or random
    if streak < MIN_STREAK:
        return None
    fire = _row(_MOOD_DOWN, streak, energy)
    if fire <= 0 or rng.random() >= fire:
        return None
    repeated = rng.random() < _row(_STAT_PENALTY, streak, energy)
    effects = [{"type": "mood", "value": "-1"}]
    if repeated:
        for stat in rng.sample(_STATS, STAT_PENALTY_COUNT):
            effects.append({"type": stat, "value": str(STAT_PENALTY_VALUE)})
    skin = rng.random() < min(1.0, _row(_SKIN_OUTBREAK, streak, energy) / fire)
    if skin:
        effects.append({"type": "condition",
                        "condition_id": conditions.SKIN_OUTBREAK})
    return {"suffix": REPEATED_SUFFIX if repeated else FATIGUE_SUFFIX,
            "effects": effects, "skin_outbreak": skin}
