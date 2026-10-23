
from __future__ import annotations

import contextvars
import functools
import random

from .. import config
from .. import tuning
from .. import master_data

NIGHT_OWL = 1
SLACKER = 2
SKIN_OUTBREAK = 3
SLOW_METABOLISM = 4
MIGRAINE = 5
PRACTICE_POOR = 6
FAST_LEARNER = 7
CHARMING = 8
HOT_TOPIC = 9
PRACTICE_PERFECT = 10
PRACTICE_PERFECT_BIG = 11
UNDER_THE_WEATHER = 12
SHINING_BRIGHTLY = 13

PROMISE_TRACKS = {
    14: frozenset({10001, 10002}),
    15: frozenset({10004, 10003, 10105}),
    16: frozenset({10005, 10104}),
    17: frozenset({10008, 10009}),
    18: frozenset({10010}),
    22: frozenset({10103}),
}
PROMISES = frozenset(PROMISE_TRACKS)


def promises_held(chara_info: dict) -> set:
    return of(chara_info) & PROMISES


def promise_fulfilled_by(chara_info: dict, track_id) -> int | None:
    if not track_id:
        return None
    for cid in promises_held(chara_info):
        if track_id in PROMISE_TRACKS[cid]:
            return cid
    return None


NEGATIVE = frozenset({NIGHT_OWL, SLACKER, SKIN_OUTBREAK, SLOW_METABOLISM,
                      MIGRAINE, PRACTICE_POOR, UNDER_THE_WEATHER})
POSITIVE = frozenset({FAST_LEARNER, CHARMING, HOT_TOPIC, PRACTICE_PERFECT,
                      PRACTICE_PERFECT_BIG, SHINING_BRIGHTLY})

_CONDITION_PROC_CHANCE = {
    NIGHT_OWL: 0.20,
    SKIN_OUTBREAK: 0.10,
    UNDER_THE_WEATHER: 0.15,
    MIGRAINE: 0.10,
}
CONDITION_PROC_ENERGY = -10
CONDITION_PROC_MOOD_CHANCE = 0.5
CONDITION_PROC_MOOD_CHANCE_BY_ID = {
    SKIN_OUTBREAK: 1.0,
}


def proc_mood_chance(condition_id: int) -> float:
    return CONDITION_PROC_MOOD_CHANCE_BY_ID.get(condition_id,
                                                CONDITION_PROC_MOOD_CHANCE)
CONDITION_STORY_SUFFIX = {
    NIGHT_OWL: 521,
    SKIN_OUTBREAK: 522,
    MIGRAINE: 722,
    SLACKER: 712,
}
SLACKER_NOSHOW_CHANCE = 0.15
_NIGHT_OWL_MOOD_CHANCE = _CONDITION_PROC_CHANCE[NIGHT_OWL]
_SKIN_OUTBREAK_MOOD_CHANCE = _CONDITION_PROC_CHANCE[SKIN_OUTBREAK]
_WEATHER_MOOD_CHANCE = _CONDITION_PROC_CHANCE[UNDER_THE_WEATHER]


PURE_PASSION_IDS = frozenset({100, 101, 102, 103, 104})
_PURE_PASSION_IMMUNE = frozenset({NIGHT_OWL, SLACKER})


def pure_passion(chara_info: dict) -> bool:
    return bool(of(chara_info) & PURE_PASSION_IDS)


def immune(chara_info: dict, condition_id: int) -> bool:
    return condition_id in _PURE_PASSION_IMMUNE and pure_passion(chara_info)


def rolled_conditions(chara_info: dict) -> list:
    if has(chara_info, SHINING_BRIGHTLY):
        return []
    conds = of(chara_info)
    return [cid for cid, p in _CONDITION_PROC_CHANCE.items()
            if cid in conds and not immune(chara_info, cid)
            and random.random() < tuning.condition_proc(p)]

CHARMING_BOND_BONUS = 2
HOT_TOPIC_FAN_MULTIPLIER = 1.2
FAST_LEARNER_SP_DISCOUNT = 0.10


def of(chara_info: dict) -> set:
    return {int(c) for c in (chara_info.get("chara_effect_id_array") or [])
            if isinstance(c, (int, float, str)) and str(c).lstrip("-").isdigit()}


def has(chara_info: dict, condition_id: int) -> bool:
    return condition_id in of(chara_info)


def any_negative(chara_info: dict) -> bool:
    return bool(of(chara_info) & NEGATIVE)


def add(chara_info: dict, condition_id: int) -> bool:
    arr = chara_info.setdefault("chara_effect_id_array", [])
    if condition_id in arr:
        return False
    if blocks_bad_condition(chara_info, condition_id):
        return False
    arr.append(condition_id)
    return True


def remove(chara_info: dict, condition_id: int) -> bool:
    arr = chara_info.get("chara_effect_id_array") or []
    if condition_id not in arr:
        return False
    chara_info["chara_effect_id_array"] = [c for c in arr if c != condition_id]
    return True


def cure_one_negative(chara_info: dict) -> int | None:
    bad = sorted(of(chara_info) & NEGATIVE)
    if not bad:
        return None
    remove(chara_info, bad[0])
    return bad[0]


def cure_negatives_by_chance(chara_info: dict, chance: float, rng=None) -> list:
    import random as _random
    r = rng or _random
    cured = []
    for cid in sorted(of(chara_info) & NEGATIVE):
        if r.random() < chance:
            remove(chara_info, cid)
            cured.append(cid)
    return cured


def bond_gain(chara_info: dict, base: int) -> int:
    gain = base + CHARMING_BOND_BONUS if has(chara_info, CHARMING) else base
    mult = config.get_float("bond_gain_multiplier")
    return gain if mult is None or gain <= 0 else max(0, int(round(gain * mult)))


def fan_gain(chara_info: dict, base: int) -> int:
    if has(chara_info, HOT_TOPIC):
        return int(base * HOT_TOPIC_FAN_MULTIPLIER)
    return base


def sp_discount(chara_info: dict) -> float:
    return FAST_LEARNER_SP_DISCOUNT if has(chara_info, FAST_LEARNER) else 0.0


def blocks_stat(chara_info: dict, stat: str) -> bool:
    return stat == "speed" and has(chara_info, SLOW_METABOLISM)


def blocks_mood_gain(chara_info: dict) -> bool:
    return has(chara_info, MIGRAINE)


def skips_training(chara_info: dict) -> bool:
    return (has(chara_info, SLACKER) and not immune(chara_info, SLACKER)
            and random.random() < tuning.bad(SLACKER_NOSHOW_CHANCE))


def turn_mood_penalty(chara_info: dict) -> int:
    if has(chara_info, SHINING_BRIGHTLY):
        return 0
    drop = 0
    conds = of(chara_info)
    if (NIGHT_OWL in conds and not immune(chara_info, NIGHT_OWL)
            and random.random() < tuning.bad(_NIGHT_OWL_MOOD_CHANCE)):
        drop += 1
    if SKIN_OUTBREAK in conds and random.random() < tuning.bad(_SKIN_OUTBREAK_MOOD_CHANCE):
        drop += 1
    if UNDER_THE_WEATHER in conds and random.random() < tuning.bad(_WEATHER_MOOD_CHANCE):
        drop += 1
    return drop


def apply_mood(chara_info: dict, delta: int) -> int:
    if delta > 0 and blocks_mood_gain(chara_info):
        return 0
    if delta < 0:
        return -drop_mood(chara_info, -delta)
    cur = chara_info.get("motivation", 3) or 3
    new = max(1, min(5, cur + delta))
    chara_info["motivation"] = new
    return new - cur


MOOD_GUARDS = (25, 41, 42)
BAD_CONDITION_GUARDS = (26, 43)
REFRESH_MIND = 32

_GUARD_LOG: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "condition_guard_log", default=None)


def begin_request() -> None:
    _GUARD_LOG.set([])


def _log_guard(kind: str, entry: dict) -> None:
    log = _GUARD_LOG.get()
    if log is not None:
        log.append((kind, entry))


def guard_report() -> dict | None:
    log = _GUARD_LOG.get()
    if not log:
        return None
    return {"evaluation_chara_id_array": [],
            "param_guard_array": [e for k, e in log if k == "param"],
            "bad_condition_guard_array": [e for k, e in log if k == "bad"]}


def _held_guard(chara_info: dict, guards: tuple) -> int | None:
    held = of(chara_info)
    return next((g for g in guards if g in held), None)


def _spend_guard(chara_info: dict, guards: tuple) -> int | None:
    g = _held_guard(chara_info, guards)
    if g is not None:
        remove(chara_info, g)
    return g


def mood_guard_preview(chara_info: dict | None) -> int | None:
    if not chara_info or (chara_info.get("motivation", 3) or 3) <= 1:
        return None
    return _held_guard(chara_info, MOOD_GUARDS)


def bad_guard_preview(chara_info: dict | None, condition_id: int) -> int | None:
    if (not chara_info or int(condition_id) not in NEGATIVE
            or has(chara_info, condition_id)):
        return None
    return _held_guard(chara_info, BAD_CONDITION_GUARDS)


def drop_mood(chara_info: dict, n: int) -> int:
    cur = chara_info.get("motivation", 3) or 3
    new = max(1, cur - max(0, n))
    if new == cur:
        return 0
    g = _spend_guard(chara_info, MOOD_GUARDS)
    if g is not None:
        _log_guard("param", {"chara_effect_id": g, "status_type": 20})
        return 0
    chara_info["motivation"] = new
    return cur - new


def blocks_bad_condition(chara_info: dict, condition_id: int) -> bool:
    if int(condition_id) not in NEGATIVE or has(chara_info, condition_id):
        return False
    g = _spend_guard(chara_info, BAD_CONDITION_GUARDS)
    if g is None:
        return False
    _log_guard("bad", {"chara_effect_id": g, "target_chara_effect_id": int(condition_id)})
    return True


REFRESH_EVENT_ID = 21148
REFRESH_STORY_ID = 800001002
REFRESH_ENERGY = 5
REFRESH_MIN_FIRES = 3
REFRESH_MAX_FIRES = 6
REFRESH_END_CHANCE = 0.5


def refresh_ends(fires: int, rng=random) -> bool:
    if fires < REFRESH_MIN_FIRES:
        return False
    return fires >= REFRESH_MAX_FIRES or rng.random() < REFRESH_END_CHANCE


_BUFF_STATS = {"speed": "speed_add", "stamina": "stamina_add", "power": "power_add",
               "guts": "guts_add", "wiz": "wiz_add"}
_BUFF_APTITUDES = {
    "proper_ground_turf": "turf_add", "proper_ground_dirt": "dirt_add",
    "proper_distance_short": "short_add", "proper_distance_mile": "mile_add",
    "proper_distance_middle": "middle_add", "proper_distance_long": "long_add",
    "proper_running_style_nige": "nige_add", "proper_running_style_senko": "senko_add",
    "proper_running_style_sashi": "sashi_add", "proper_running_style_oikomi": "oikomi_add",
}


@functools.lru_cache(maxsize=1)
def _scheduled_buffs() -> tuple:
    if not master_data.has_table("single_mode_chara_effect_buff"):
        return ()
    return tuple(dict(r) for r in master_data.query(
        "SELECT * FROM single_mode_chara_effect_buff ORDER BY id"))


def _active_buffs(chara_info: dict) -> list:
    chara_id = (chara_info.get("card_id") or 0) // 100
    turn = chara_info.get("turn")
    if not chara_id or turn is None:
        return []
    return [r for r in _scheduled_buffs()
            if r["chara_id"] == chara_id and r["start_turn"] <= turn <= r["end_turn"]]


def sync_scheduled(chara_info: dict) -> None:
    chara_id = (chara_info.get("card_id") or 0) // 100
    rows = [r for r in _scheduled_buffs() if r["chara_id"] == chara_id]
    if not rows:
        return
    active = {r["effect_id"] for r in _active_buffs(chara_info)}
    for r in rows:
        if r["effect_id"] in active:
            add(chara_info, r["effect_id"])
        else:
            remove(chara_info, r["effect_id"])


def apply_race_buffs(race_chara: dict, program_id: int | None) -> None:
    for r in _active_buffs(race_chara):
        if r["program_id"] and r["program_id"] != program_id:
            continue
        for key, col in _BUFF_STATS.items():
            if r[col]:
                race_chara[key] = max(1, (race_chara.get(key) or 0) + r[col])
        for key, col in _BUFF_APTITUDES.items():
            if r[col]:
                race_chara[key] = max(1, min(8, (race_chara.get(key) or 1) + r[col]))
