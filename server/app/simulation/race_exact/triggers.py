
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .course import SLOPE_DOWN, SLOPE_UP, Course
from .rng import RaceRandom, f32

FLOAT_MAX = 3.4028234663852886e+38
_TOKEN = re.compile(r'^(\w+)(==|!=|>=|<=|>|<)(-?\d+)$')


@dataclass
class Phases:
    middle: float
    end: float
    last: float
    course: float

    @classmethod
    def for_distance(cls, distance: int) -> 'Phases':
        section = f32(distance / 24.0)
        return cls(f32(section * 4.0), f32(section * 16.0), f32(section * 20.0), float(distance))

    def bounds(self, phase: int) -> tuple[float, float]:
        if phase == 0:
            return 0.0, self.middle
        if phase == 1:
            return self.middle, self.end
        if phase == 2:
            return self.end, self.last
        if phase == 3:
            return self.last, self.course
        if phase == 4:
            return self.course, FLOAT_MAX
        return 0.0, 0.0


@dataclass
class Trigger:
    name: str
    op: str
    value: int
    distance_min: float | None = None
    distance_max: float | None = None
    distance_set: list = field(default_factory=list)
    lot_ok: bool | None = None
    random_start_time: int | None = None


def _lot_start(rng: RaceRandom, lo: float, hi: float, tag: str) -> tuple[float, float]:
    upper = f32(hi - 10.0)
    if upper < lo:
        upper = lo
    v = rng.float_range(lo, upper, tag)
    return v, f32(v + 10.0)


def _lot_and_add(rng: RaceRandom, t: Trigger, lo: float, hi: float, tag: str) -> tuple[float, float]:
    upper = f32(hi - 10.0)
    if upper < lo:
        upper = lo
    v = rng.float_range(lo, upper, tag)
    w = (v, f32(v + 10.0))
    t.distance_set.append(w)
    t.distance_set.sort(key=lambda x: x[0])
    return w


def build_trigger(token: str, rng: RaceRandom, course: Course, phases: Phases, tag: str) -> Trigger | None:
    m = _TOKEN.match(token.strip())
    if not m:
        return None
    t = Trigger(m.group(1), m.group(2), int(m.group(3)))
    n, v = t.name, t.value

    if n == 'corner_random':
        cd = course.corner_distance(v)
        if cd:
            t.distance_min, t.distance_max = _lot_start(rng, cd[0], cd[1], tag)
    elif n == 'is_finalcorner_random':
        fc = course.final_corner_distance()
        if fc:
            t.distance_min, t.distance_max = _lot_start(rng, fc[0], fc[1], tag)
    elif n == 'straight_random':
        if course.straights:
            i = rng.int_max(len(course.straights), tag)
            s = course.straights[i]
            t.distance_min, t.distance_max = _lot_start(rng, s.start, s.end, tag)
    elif n == 'last_straight_random':
        fc = course.final_corner_distance()
        if fc and course.straights:
            for s in reversed(course.straights):
                if fc[0] <= s.start:
                    t.distance_min, t.distance_max = _lot_start(rng, s.start, s.end, tag)
    elif n in ('phase_random', 'phase_firsthalf_random', 'phase_laterhalf_random'):
        lo, hi = phases.bounds(v)
        if n == 'phase_random':
            t.distance_min, t.distance_max = _lot_start(rng, lo, hi, tag)
        else:
            half = f32(f32(hi - lo) * 0.5)
            if n == 'phase_firsthalf_random':
                upper = f32(f32(half + lo) - 10.0)
                if upper < lo:
                    upper = lo
                x = rng.float_range(lo, upper, tag)
            else:
                start = f32(lo + half)
                upper = f32(hi - 10.0)
                if upper < start:
                    upper = start
                x = rng.float_range(start, upper, tag)
            t.distance_min, t.distance_max = x, f32(x + 10.0)
    elif n == 'distance_rate_after_random':
        lo = f32(f32(v / 100.0) * float(course.distance))
        t.distance_min, t.distance_max = _lot_start(rng, lo, float(course.distance), tag)
    elif n in ('up_slope_random', 'down_slope_random'):
        want = SLOPE_UP if n.startswith('up') else SLOPE_DOWN
        slopes = [s for s in course.slopes if s.type == want]
        if slopes:
            i = rng.int_max(len(slopes), tag)
            s = slopes[i]
            t.distance_min, t.distance_max = _lot_start(rng, s.start, s.end, tag)
    elif n == 'all_corner_random':
        cands = [[c.start, c.end] for c in course.corners]
        for _ in range(4):
            if not cands:
                break
            i = rng.int_range(0, len(cands), tag)
            pick = cands[i]
            lo, hi = _lot_and_add(rng, t, pick[0], pick[1], tag)
            if 10.0 <= f32(pick[1] - hi):
                pick[0] = hi
            else:
                cands.remove(pick)
            threshold = pick[0]
            cands = [c for c in cands if not (c[0] < threshold)]
    elif n == 'random_lot':
        t.lot_ok = rng.int_max(100, tag) < v
    elif n == 'phase_firstquarter_random':
        lo, hi = phases.bounds(v)
        quarter = f32(f32(hi - lo) * 0.25)
        t.distance_min, t.distance_max = _lot_start(rng, lo, f32(lo + quarter), tag)
    elif n in ('phase_corner_random', 'phase_straight_random'):
        lo, hi = phases.bounds(v)
        segs = course.corners if n == 'phase_corner_random' else course.straights
        cands = [(max(s.start, lo), min(s.end, hi)) for s in segs if s.start < hi and lo < s.end]
        if cands:
            i = rng.int_max(len(cands), tag)
            t.distance_min, t.distance_max = _lot_start(rng, cands[i][0], cands[i][1], tag)
    elif n in ('phase_first_half_straight_random',
               'phase_latter_half_straight_random', 'up_slope_random_later_half', 'random_lot_shared',
               'run_at_full_speed_random'):
        t.lot_ok = None
    return t


def build_condition(cond: str, rng: RaceRandom, course: Course, phases: Phases, tag: str) -> list[list[Trigger]]:
    groups = []
    if not cond:
        return groups
    for grp in cond.split('@'):
        groups.append([t for t in (build_trigger(tok, rng, course, phases, tag) for tok in grp.split('&')) if t])
    return groups
