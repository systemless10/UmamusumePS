
from __future__ import annotations

import contextvars
import logging

from . import config

log = logging.getLogger("uma-server")

LUCK_OVERRIDES = ("failure_rate_shift", "hint_rate_multiplier", "event_rate_multiplier",
                  "condition_proc_multiplier", "specialty_priority_bonus")


def _num(key: str) -> float | None:
    return config.get_float(key)


def luck() -> float:
    v = _num("luck")
    return 0.0 if v is None else max(-1.0, min(1.0, v / 100.0))


def _locked(key: str) -> bool:
    return key in LUCK_OVERRIDES and luck() != 0


def mult(key: str) -> float:
    if _locked(key):
        return 1.0
    v = _num(key)
    return 1.0 if v is None else max(0.0, v)


def specialty_bonus() -> int:
    if luck() > 0:
        return int(round(400 * luck()))
    if _locked("specialty_priority_bonus"):
        return 0
    return max(0, config.get_int("specialty_priority_bonus", 0))


def off_type_scale() -> float:
    return 1.0 + luck() if luck() < 0 else 1.0


def _odds(p: float, steps: float) -> float:
    if p <= 0 or p >= 1:
        return p
    o = p / (1 - p) * (2.0 ** steps)
    return o / (1 + o)


def good(p: float) -> float:
    p = max(0.0, min(1.0, float(p)))
    L = luck()
    if L >= 1:
        return 1.0 if p > 0 else 0.0
    if L <= -1:
        return 0.0
    return _odds(p, 4 * L)


def bad(p: float) -> float:
    p = max(0.0, min(1.0, float(p)))
    L = luck()
    if L >= 1:
        return 0.0
    if L <= -1:
        return 1.0 if p > 0 else 0.0
    return _odds(p, -4 * L)


def event(p: float) -> float:
    return good(min(1.0, float(p) * mult("event_rate_multiplier")))


def condition_proc(p: float) -> float:
    return bad(min(1.0, float(p) * mult("condition_proc_multiplier")))


def hint(p: float) -> float:
    return good(min(1.0, float(p) * mult("hint_rate_multiplier")))


def branch_weights(weights: list) -> list:
    if luck() == 0 or len(weights) < 2:
        return weights
    total = sum(weights) or 1.0
    p0 = good(weights[0] / total)
    rest = sum(weights[1:]) or 1.0
    return [p0 * total] + [w / rest * (1 - p0) * total for w in weights[1:]]


def failure_pct(rate: int) -> int:
    rate = max(0, min(100, int(rate)))
    if config.get("force_failure_rate") is not None:
        return rate
    if luck() != 0:
        return int(round(100 * bad(rate / 100)))
    s = _num("failure_rate_shift")
    if not s:
        return rate
    s = max(-1.0, min(1.0, s / 100.0))
    return int(round(rate * (1 + s) if s < 0 else rate + (100 - rate) * s))


def pinned_gain() -> int | None:
    v = config.get("force_training_gain")
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def pinned_stats() -> set:
    v = config.get("force_training_gain_stats") or []
    return {str(s) for s in v} if isinstance(v, list) else set()


def spark_proc(p: float) -> float:
    return good(min(1.0, float(p) * mult("spark_proc_multiplier")))


def legacy_chance(p: float) -> float:
    return max(0.0, min(1.0, float(p) * mult("legacy_spark_multiplier")))


def legacy_star(stars, weights) -> int:
    import random
    v = _num("legacy_star_shift") or 0.0
    L = max(-1.0, min(1.0, v / 100.0))
    pairs = [(s, w) for s, w in zip(stars, weights) if w > 0]
    if L >= 1:
        return max(pairs)[0]
    if L <= -1:
        return min(pairs)[0]
    lo = min(s for s, _ in pairs)
    return random.choices([s for s, _ in pairs],
                          [w * 2.0 ** (4 * L * (s - lo)) for s, w in pairs])[0]


STAT_KEYS = ("speed", "stamina", "power", "guts", "wiz", "skill_point")


def stat_rule(stat: str) -> tuple[float, int]:
    rules = config.get("training_stat_tuning") or {}
    r = rules.get(stat) if isinstance(rules, dict) else None
    if not isinstance(r, dict):
        return 1.0, 0
    try:
        return max(0.0, float(r.get("x", 1))), int(r.get("add", 0))
    except (TypeError, ValueError):
        return 1.0, 0


TRAINING_SP = contextvars.ContextVar("tuning_training_sp", default=0)


def note_training_sp(n: int) -> None:
    TRAINING_SP.set(TRAINING_SP.get() + max(0, int(n)))


def career_sp(viewer_id) -> int | None:
    from . import state as state_store
    try:
        v = state_store.extract_json_path(viewer_id, "single_mode_team", "$.data.chara_info.skill_point")
    except Exception:
        return None
    return None if v is None else int(v)


def settle_skill_points(viewer_id, before: int | None, response: dict) -> None:
    m = mult("skill_point_multiplier")
    if m == 1.0 or before is None:
        return
    from . import state as state_store
    full = state_store.get_state(viewer_id)
    career = (full or {}).get("single_mode_team") or {}
    ci = (career.get("data") or {}).get("chara_info")
    if not ci:
        return
    gained = int(ci.get("skill_point") or 0) - before - TRAINING_SP.get()
    if gained <= 0:
        return
    extra = int(round(gained * (m - 1)))
    if not extra:
        return
    ci["skill_point"] = max(0, int(ci.get("skill_point") or 0) + extra)
    full["single_mode_team"] = career
    state_store.save_state(viewer_id, full)
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict) and isinstance(data.get("chara_info"), dict):
        data["chara_info"]["skill_point"] = ci["skill_point"]
    log.info("skill_point_multiplier %.2f: +%d SP on top of %d", m, extra, gained)


_RIVAL_STATS = ("speed", "stamina", "pow", "power", "guts", "wiz")


def scale_rivals(opponents: list) -> None:
    k = mult("rival_strength_multiplier")
    if k == 1.0:
        return
    for o in opponents or ():
        for s in _RIVAL_STATS:
            if isinstance(o.get(s), (int, float)):
                o[s] = max(1, min(2000, int(round(o[s] * k))))
