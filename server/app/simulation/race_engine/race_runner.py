"""Port of tools/raceRunner.ts -- multi-horse race harness.

Builds one RaceSolver per horse (distinct-but-deterministic per-horse seed,
same `spec.seed + i*7919` derivation as the TS bridge), steps the whole
field together in lockstep at DT=1/15s (required since non-Nige horses'
`pacer` solver is stepped as a side effect inside `RaceSolver.step()`, and
frame recording needs a synchronized snapshot across the field), and
returns the exact same `{seed, frames, results, skillEvents}` shape the
Node subprocess used to produce -- this is the sole integration point with
`race_simulator.run_simulation`, so the contract must not change without
updating that caller.
"""

from __future__ import annotations

import math
from typing import Any

from .course_data import get_course
from .race_field import RaceField
from .race_solver import Perspective
from .race_solver_builder import RaceSolverBuilder

DT = 1.0 / 15.0
MAX_RACE_SECONDS = 400.0

_STRATEGY_IDS = {"Nige": 1, "Senkou": 2, "Sasi": 3, "Oikomi": 4, "Oonige": 5}


def _skill_id_and_level(entry) -> tuple[str, int]:
    """Accepts either a plain skill-id string (older/test callers) or the
    {"skillId", "level"} shape race_simulator.build_horse_spec sends."""
    if isinstance(entry, dict):
        return str(entry["skillId"]), int(entry.get("level") or 1)
    return str(entry), 1


def _new_builder(spec: dict, h: dict, i: int) -> RaceSolverBuilder:
    return (
        RaceSolverBuilder(1)
        .seed(spec["seed"] + i * 7919, 0)
        .course(spec["courseId"])
        .mood(max(-2, min(2, h.get("mood", 0))))
        .ground(spec["groundCondition"])
        .weather(spec["weather"])
        .season(spec["season"])
        .gate_index(h.get("gateIndex", i))
        .horse({
            "speed": h["speed"],
            "stamina": h["stamina"],
            "power": h["power"],
            "guts": h["guts"],
            "wisdom": h["wisdom"],
            "strategy": h["strategy"],
            "distanceAptitude": h["distanceAptitude"],
            "surfaceAptitude": h["surfaceAptitude"],
            "strategyAptitude": h["strategyAptitude"],
        })
        # Doc's "Charge Up / Conserve Power" and "Stamina Limit Break" --
        # both already ported (RaceSolverBuilder.withAsiwotameru/
        # withStaminaSyoubu in the original TS) but never enabled by the
        # upstream raceRunner.ts bridge. Real Global mechanics, so enabled
        # here. Must come after .horse()/.mood() (both read from them).
        .with_asiwotameru()
        .with_stamina_syoubu()
    )


def run_race(spec: dict) -> dict:
    course = get_course(spec["courseId"])
    skill_events: list[dict] = []
    kakari_start: dict[int, float] = {}  # horse_index -> activation time, until on_skill_deactivate finalizes it
    # (horse_index, skill_id) -> (activation time, isUnique), until
    # on_skill_deactivate finalizes it with the real elapsed duration --
    # same reasoning as kakari_start above, now applied to every real skill
    # too (see make_activate_hook/make_deactivate_hook).
    pending_skills: dict[tuple, tuple] = {}

    # Whether a skill id can actually be built for this race. Some skills'
    # conditions reference tokens this engine doesn't implement and those
    # throw from inside build() (not addSkill()), so a single bad skill
    # would otherwise abort the whole horse's build. Memoized across the
    # roster (most horses share most of their skills).
    skill_usable: dict[str, bool] = {}

    def is_usable(skill_id: str, level: int, h: dict, i: int) -> bool:
        cached = skill_usable.get(skill_id)
        if cached is not None:
            return cached
        ok = True
        try:
            probe = _new_builder(spec, h, i)
            probe.add_skill(skill_id, level=level)
            gen = probe.build()
            next(gen)
        except Exception:
            ok = False
        skill_usable[skill_id] = ok
        return ok

    solvers = []
    for i, h in enumerate(spec["horses"]):
        builder = _new_builder(spec, h, i)
        wanted = [_skill_id_and_level(e) for e in (h.get("skills") or [])]
        unique_ids = {
            str(e["skillId"]) for e in (h.get("skills") or [])
            if isinstance(e, dict) and e.get("isUnique")
        }

        solver = None
        try:
            for skill_id, level in wanted:
                try:
                    builder.add_skill(skill_id, level=level)
                except Exception:
                    pass
            gen = builder.build()
            solver = next(gen)
        except Exception:
            retry = _new_builder(spec, h, i)
            kept = 0
            for skill_id, level in wanted:
                if not is_usable(skill_id, level, h, i):
                    continue
                try:
                    retry.add_skill(skill_id, level=level)
                    kept += 1
                except Exception:
                    pass
            gen = retry.build()
            solver = next(gen)

        def make_activate_hook(horse_index: int, unique_ids: set):
            def hook(s, skill_id: str, perspective):
                if skill_id == "kakari":
                    # Rushing (kakari) is not a real skill id -- int(skill_id)
                    # below would raise and this event was previously just
                    # dropped, silently hiding every "Rushed" occurrence from
                    # anything downstream (team_stadium's PvP scoring needs
                    # it -- team_stadium_raw_score condition_type 5/6). Record
                    # the start time now; make_deactivate_hook finalizes the
                    # event with the REAL elapsed duration once it ends
                    # (kakari_duration can be extended mid-race by an
                    # opponent's EXTEND_KAKARI skill, so this must be measured
                    # at deactivation, not read from kakari_duration here).
                    kakari_start[horse_index] = s.accumulatetime.t
                    return
                try:
                    sid = int(skill_id)
                except (TypeError, ValueError):
                    return
                # Real captures show a genuine nonzero per-activation
                # duration matching the skill's actual buff/effect lifetime
                # (e.g. 3.6s), not 0 -- this server used to hardcode 0 for
                # every regular skill (unlike kakari just above, which
                # already measured its real span via on_skill_deactivate).
                # The solver already calls on_skill_deactivate for every
                # real skill too, not just kakari (see the plain
                # `self.on_skill_deactivate(self, s.skill_id, ...)` call
                # sites in race_solver.py) -- record the start here and
                # finalize with the real elapsed duration in
                # make_deactivate_hook below, same pattern as kakari.
                pending_skills[(horse_index, sid)] = (
                    s.accumulatetime.t, 1 if skill_id in unique_ids else 0)
            return hook

        def make_deactivate_hook(horse_index: int):
            def hook(s, skill_id: str, perspective):
                if skill_id == "kakari":
                    start_t = kakari_start.pop(horse_index, None)
                    if start_t is None:
                        return
                    skill_events.append({
                        "horseIndex": horse_index,
                        "skillId": "kakari",
                        "t": start_t,
                        "duration": s.accumulatetime.t - start_t,
                        "isUnique": 0,
                    })
                    return
                try:
                    sid = int(skill_id)
                except (TypeError, ValueError):
                    return
                pending = pending_skills.pop((horse_index, sid), None)
                if pending is None:
                    return
                start_t, is_unique = pending
                skill_events.append({
                    "horseIndex": horse_index,
                    "skillId": sid,
                    "t": start_t,
                    "duration": s.accumulatetime.t - start_t,
                    "isUnique": is_unique,
                })
            return hook

        solver.on_skill_activate = make_activate_hook(i, unique_ids)
        solver.on_skill_deactivate = make_deactivate_hook(i)
        solvers.append(solver)

    # Gives every solver live awareness of the rest of the field (order/rank,
    # gaps to the horse ahead/behind) -- not part of the original TS engine,
    # which has no notion of other horses at all. See race_field.py.
    field = RaceField(solvers)

    n = len(solvers)
    finished: list[float | None] = [None] * n
    finished_step: list[float | None] = [None] * n
    frames: list[dict[str, Any]] = []

    # BUG FIXED 2026-08-29 (live-reported: skill popups only render, and the
    # PLAYER's own horse only gets treated as the player at all -- for
    # popups AND for who the game treats as having won -- when her gate
    # happens to be gate 1; any other gate and a DIFFERENT horse gets the
    # skill/win treatment instead). Root cause found via a real captured
    # practice_race/race_start pair (proxy-captured against the actual
    # Cygames server, two separate races, gates 13 and 14): the real wire
    # format sends a race-event type this server never emitted at all --
    # type=6, exactly once per race, single param = the PLAYER's own gate
    # index (0-based) -- e.g. real capture with frame_order=13 sent
    # type=6/params=(12,); frame_order=14 sent params=(13,). Every other
    # server-side identity field (viewer_id, entry_id, frame_order) was
    # independently verified to already be correct, and this simulation's
    # own skill/result computation is gate-independent -- so this missing
    # marker is the actual bug: the client evidently uses it to learn "this
    # horse index is you" for everything downstream (skill popups, win
    # highlighting), and silently defaults to gate 1 when it never arrives.
    # Timing in both real captures lines up with the PLAYER's own last-spurt
    # start (not a fixed race-clock moment) -- tracked below the same way
    # kakari/skills are, by watching solver[0]'s own state each tick.
    player_last_spurt_t: float | None = None

    t = 0.0

    while any(f is None for f in finished) and t < MAX_RACE_SECONDS:
        active = [i for i in range(n) if finished[i] is None]
        prev_pos = {i: solvers[i].pos for i in active}
        field.step_all(DT, active)
        if player_last_spurt_t is None and n > 0 and solvers[0].is_last_spurt:
            player_last_spurt_t = t + DT
        for i in active:
            s = solvers[i]
            if s.pos >= course.distance:
                span = s.pos - prev_pos[i]
                frac = (course.distance - prev_pos[i]) / span if span > 0 else 0
                finished[i] = t + DT * frac
                finished_step[i] = t + DT
        t += DT

        # BUG FIXED 2026-08-29 (live-reported: one horse -- always whichever
        # finishes last -- "speeds up absurdly" right at the end of every
        # race). Frame recording used to stop at winner_time + a flat 5s
        # "tail grace" -- but real captured finish spreads are usually under
        # ~5s while THIS server's scaled-opponent fields routinely spread
        # 10+ seconds (verified: an 18-horse test race spread 90.7s-102.1s,
        # ~11.4s). Every horse whose real finish landed after that 5s cutoff
        # had its recorded frames truncated well short of the finish line
        # while its results[i].finishTime still correctly reported the
        # later time -- reconciling "last known position, still mid-track"
        # against "already finished" is what produced the warp/speed-burst.
        # The outer while loop already stops on its own once every horse has
        # finished (or MAX_RACE_SECONDS, a generous safety cap) -- record
        # every tick unconditionally instead of cutting the tail short.
        frames.append({
            "t": t,
            "horses": [
                {
                    "pos": s.pos,
                    "speed": s.current_speed,
                    "hp": max(0.0, s.hp.hp) if hasattr(s.hp, "hp") else 0.0,
                }
                for s in solvers
            ],
        })

    for i in range(n):
        if finished[i] is None:
            finished[i] = t
            finished_step[i] = t

    # If the player never entered her own last-spurt phase before the race
    # ended (shouldn't normally happen, but MAX_RACE_SECONDS or an
    # HP-exhaustion edge case could prevent it) -- fall back to the finish
    # time so the identity marker still gets sent rather than silently
    # dropped.
    if player_last_spurt_t is None and n > 0:
        player_last_spurt_t = finished_step[0] if finished_step[0] is not None else t

    order = sorted(range(n), key=lambda i: finished[i])
    order_index = {horse_i: rank for rank, horse_i in enumerate(order)}

    results = []
    for i, s in enumerate(solvers):
        results.append({
            "finishOrder": order_index[i],  # 0-based
            "finishTime": finished_step[i],
            "finishTimeRaw": finished[i],
            "startDelayTime": s.start_delay if isinstance(s.start_delay, (int, float)) else 0,
            "lastSpurtStartDistance": s.last_spurt_transition if (isinstance(s.last_spurt_transition, (int, float)) and s.last_spurt_transition > 0) else -1,
            "runningStyle": _STRATEGY_IDS.get(spec["horses"][i]["strategy"], 2),
        })

    # A skill still active when the race ends never gets an on_skill_deactivate
    # call (the solver simply stops ticking), which used to mean it was
    # dropped from skill_events entirely -- worse than the old duration=0
    # behavior. Flush anything still pending as active-until-the-finish.
    for (horse_index, sid), (start_t, is_unique) in pending_skills.items():
        skill_events.append({
            "horseIndex": horse_index,
            "skillId": sid,
            "t": start_t,
            "duration": t - start_t,
            "isUnique": is_unique,
        })

    return {"seed": spec["seed"], "frames": frames, "results": results, "skillEvents": skill_events,
            "playerLastSpurtTime": player_last_spurt_t}
