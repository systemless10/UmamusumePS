
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from ... import career_events as CE
from ... import master_data
from .impl import SCENARIO_ID
from . import impl

log = logging.getLogger("uma-server")


def _story(event_id: int) -> int:
    row = master_data.query_one(
        "SELECT story_id, short_story_id FROM single_mode_story_data WHERE id=?",
        (int(event_id),))
    if row is None:
        return 0
    return int(row["short_story_id"] or row["story_id"] or 0)


FIXED_BEATS: dict = {
    1:  [(203026, 1, 6)],
    3:  [(203301, 0, 1)],
    13: [(203027, 0, 1)],
}

SEASONAL_GROUPS: dict = {
    27: (203601, 203602),
    29: (203552, 203553),
    42: (203575, 203576, 203577),
    60: (203559, 203560),
    68: (203585, 203586),
    72: (203597, 203598),
}
SEASONAL_TIMING = 1

_GLITTERING_STAR = 210062
_RADIANT_STAR = 210061


GOAL_BEATS: dict = {
    24: {"met": (203010, 203059), "missed": (203007, 203056)},
    48: {"met": (203011, 203060), "missed": (203008, 203057)},
    72: {"met": (203012, 203061), "missed": (203009, 203058)},
}
GOAL_TIMING = 3

ALT_ARM_SHARE = 486 / (486 + 1159)


GOAL_MET_EFFECTS: dict = {
    24: {"announce": [{"type": "all_stats", "value": "+5"},
                      {"type": "skill_points", "value": "+30"}],
         "follow": [{"type": "unique_skill_level", "value": "+1"},
                    {"type": "random_stats", "value": "+10", "stat_count": 1},
                    {"type": "skill_points", "value": "+20"}]},
    48: {"announce": [{"type": "all_stats", "value": "+6"},
                      {"type": "skill_points", "value": "+40"}],
         "follow": [{"type": "unique_skill_level", "value": "+1"},
                    {"type": "all_stats", "value": "+5"},
                    {"type": "skill_points", "value": "+30"},
                    {"type": "skill_hint", "skill_id": _GLITTERING_STAR,
                     "value": "+1"}]},
    72: {"announce": [{"type": "all_stats", "value": "+7"},
                      {"type": "skill_points", "value": "+50"}],
         "follow": [{"type": "unique_skill_level", "value": "+1"},
                    {"type": "all_stats", "value": "+10"},
                    {"type": "skill_points", "value": "+30"}]},
}
YEAR_END_BEST = {24: (15, 20, 100, 0, 0), 48: (30, 40, 450, 60000, 40000),
                 72: (50, 60, 480, 120000, 80000)}
YEAR_END_FALLBACK = {24: (15, 5000, 5000), 48: (30, 60000, 40000),
                     72: (50, 120000, 80000)}
YEAR_END_FALLBACK_EVENT = {24: 203037, 48: 203038, 72: 203039}
_UNIQUE_ONLY = [{"type": "unique_skill_level", "value": "+1"}]


def _npc_bond(chara_info: dict, target_id: int) -> int:
    for row in (chara_info or {}).get("evaluation_info_array") or ():
        if int(row.get("target_id") or 0) == target_id:
            return int(row.get("evaluation") or 0)
    return 0


def year_end_tier(full_state: dict, chara_info: dict, turn: int) -> str | None:
    akikawa = _npc_bond(chara_info, 102)
    etsuko = _npc_bond(chara_info, 103)
    fans = int((chara_info or {}).get("fans") or 0)
    gp = int(((impl.state(full_state).get("objective_points") or {})
              .get(str(turn))) or 0)
    dirt = 0 < impl.objective_target(chara_info, 48) < 300
    a, e, g, f_turf, f_dirt = YEAR_END_BEST[turn]
    if akikawa >= a and (etsuko >= e or gp >= g) and fans >= (f_dirt if dirt else f_turf):
        return "best"
    a, f_turf, f_dirt = YEAR_END_FALLBACK[turn]
    if akikawa >= a and fans >= (f_dirt if dirt else f_turf):
        return "fallback"
    return None


ENDING_BEAT = (203202, 1, 3)
ENDING_TURN = 78


def _p(stat, n):
    return {"type": stat, "value": f"+{n}"}


_MOOD_UP = {"type": "mood", "value": "+1"}


def _energy(n):
    return {"type": "energy", "value": f"+{n}"}


BEAT_EFFECTS: dict = {
    203026: [],
    203301: [],
    203027: [],
    203601: [_p("power", 10), _MOOD_UP],
    203602: [_p("wisdom", 10), _MOOD_UP],
    203552: [_p("stamina", 10), _energy(20)],
    203553: [_p("guts", 10), _energy(20)],
    203575: [_p("power", 10), _MOOD_UP],
    203576: [_p("wisdom", 10), _MOOD_UP],
    203577: [_p("guts", 10), _MOOD_UP],
    203559: [_p("speed", 10), _energy(20)],
    203560: [_p("stamina", 10), _energy(20)],
    203585: [_p("wisdom", 10), _MOOD_UP],
    203586: [_p("speed", 10), _MOOD_UP],
    203597: [_p("stamina", 10), _energy(30)],
    203598: [_p("power", 10), _energy(30)],
    203202: [{"type": "all_stats", "value": "+10"},
             {"type": "skill_points", "value": "+40"},
             {"type": "skill_hint", "skill_id": _RADIANT_STAR, "value": "+1"}],
}


def _event(event_id: int, choices: int, timing: int, ctx, effects=None) -> CE.Event:
    return CE.Event(
        event_id=int(event_id),
        story_id=_story(event_id),
        play_timing=int(timing),
        choices=[CE.Choice(effects=list(effects or []))
                 for _ in range(max(1, choices))] if choices else [],
        chara_id=ctx.player_chara,
        once_key=f"trackblazer:{event_id}",
        priority=CE.PRIO_SCENARIO,
    )


def _due(ctx, turn: int) -> bool:
    return int(turn) in (int(ctx.advanced_turn or 0), int(ctx.turn or 0))


@CE.producer("trackblazer_fixed_beats", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def trackblazer_fixed_beats(ctx: CE.Ctx) -> list:
    out = []
    for turn, beats in FIXED_BEATS.items():
        if not _due(ctx, turn):
            continue
        for event_id, choices, timing in beats:
            out.append(_event(event_id, choices, timing, ctx,
                              effects=BEAT_EFFECTS.get(event_id)))
    for turn, group in SEASONAL_GROUPS.items():
        if not _due(ctx, turn):
            continue
        rng = random.Random((CE.career_salt(ctx.full_state) << 8) ^ int(turn))
        arm = rng.choice(group)
        out.append(_event(arm, 1, SEASONAL_TIMING, ctx,
                          effects=BEAT_EFFECTS.get(arm)))
    if _ending_due(ctx):
        event_id, choices, timing = ENDING_BEAT
        out.append(_event(event_id, choices, timing, ctx,
                          effects=BEAT_EFFECTS.get(event_id)))
    return out


_POOL_PATH = Path(__file__).resolve().parents[3] / "data" / "trackblazer_event_pool.json"
_pool_cache: dict | None = None


def _pool() -> dict:
    global _pool_cache
    if _pool_cache is None:
        try:
            with open(_POOL_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError:
            log.warning("trackblazer: no event pool at %s; pool events disabled",
                        _POOL_PATH)
            raw = {}
        events, weights = {}, {}
        for key, ent in (raw.get("events") or {}).items():
            eid = int(key)
            events[eid] = {"story_id": int(ent["story_id"]),
                           "choices": [list(c) for c in ent.get("choices") or [[]]]}
            for t, hits in (ent.get("turn_hits") or {}).items():
                weights.setdefault(int(t), []).append((eid, int(hits)))
        reach = {int(t): int(c) for t, c in (raw.get("turn_reach") or {}).items()}
        turn_p = {t: min(1.0, sum(h for _e, h in ws) / reach[t])
                  for t, ws in weights.items() if reach.get(t)}
        _pool_cache = {"events": events, "turn_p": turn_p,
                       "weights": {t: sorted(ws) for t, ws in weights.items()}}
    return _pool_cache


def _pool_schedule(salt: int) -> dict:
    pool = _pool()
    rng = random.Random(f"tbpool:{salt}")
    fired, out = set(), {}
    for turn in sorted(pool["weights"]):
        if rng.random() >= pool["turn_p"].get(turn, 0.0):
            continue
        cands = [(e, h) for e, h in pool["weights"][turn] if e not in fired]
        if not cands:
            continue
        pick = rng.choices([e for e, _ in cands], weights=[h for _, h in cands])[0]
        fired.add(pick)
        out[turn] = pick
    return out


@CE.producer("trackblazer_random_pool", priority=CE.PRIO_NORMAL,
             scenario=SCENARIO_ID)
def trackblazer_random_pool(ctx: CE.Ctx) -> list:
    turn = int(ctx.turn or 0)
    if turn <= 0:
        return []
    event_id = _pool_schedule(CE.career_salt(ctx.full_state)).get(turn)
    if event_id is None or CE.already_fired(ctx.full_state, f"tb:pool:{event_id}"):
        return []
    ent = _pool()["events"][event_id]
    return [CE.Event(
        event_id=event_id, story_id=ent["story_id"],
        play_timing=6, timing_from_endpoint=True,
        choices=[CE.Choice(effects=list(c)) for c in ent["choices"]],
        chara_id=0,
        once_key=f"tb:pool:{event_id}",
        priority=CE.PRIO_NORMAL,
        source="trackblazer_pool")]


def _ending_due(ctx) -> bool:
    if int(ctx.turn or 0) != ENDING_TURN:
        return False
    climax = impl.state(ctx.full_state).get("climax") or {}
    return any(int(r.get("turn") or 0) == ENDING_TURN
               for r in climax.get("results") or [])


@CE.producer("trackblazer_goal_beats", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def trackblazer_goal_beats(ctx: CE.Ctx) -> list:
    out = []
    for turn, arms in GOAL_BEATS.items():
        if int(ctx.turn or 0) != turn:
            continue
        met = impl.objective_met(ctx.full_state, ctx.chara_info, turn)
        announce, follow = arms["met"] if met else arms["missed"]
        if met and random.Random("tbgoal:%s:%s" % (CE.career_salt(ctx.full_state), turn)
                                 ).random() < ALT_ARM_SHARE:
            announce, follow = arms["missed"]
        pay = GOAL_MET_EFFECTS.get(turn, {}) if met else {}
        out.append(_event(announce, 1, GOAL_TIMING, ctx,
                          effects=pay.get("announce")))
        if not met:
            out.append(_event(follow, 1, GOAL_TIMING, ctx))
            continue
        tier = year_end_tier(ctx.full_state, ctx.chara_info, turn)
        if tier == "best":
            out.append(_event(follow, 1, GOAL_TIMING, ctx, effects=pay.get("follow")))
        elif tier == "fallback":
            out.append(_event(YEAR_END_FALLBACK_EVENT[turn], 1, GOAL_TIMING, ctx,
                              effects=_UNIQUE_ONLY))
    return out
