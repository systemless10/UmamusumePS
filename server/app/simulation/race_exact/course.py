
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from . import params
from .rng import f32

SLOPE_NONE, SLOPE_UP, SLOPE_DOWN = 0, 1, 2


@dataclass
class Corner:
    start: float
    end: float
    number: int
    is_final: bool


@dataclass
class Straight:
    start: float
    end: float
    front_type: int
    front_type_number: int


@dataclass
class Slope:
    start: float
    end: float
    type: int
    per: float


@dataclass
class Course:
    id: int
    distance: int
    race_track_id: int
    ground: int
    turn: int
    lane_max: float
    master: object
    corner_events: list = field(default_factory=list)
    corners: list = field(default_factory=list)
    straights: list = field(default_factory=list)
    slopes: list = field(default_factory=list)
    lane_max_changes: list = field(default_factory=list)
    move_lane_point: float | None = None

    def corner_distance(self, number: int):
        if number <= 0:
            return None
        for start, values in reversed(self.corner_events):
            if len(values) > 1 and values[0] == number:
                length = values[1] if len(values) > 1 else 200
                return start, f32(float(length) + start)
        return None

    def final_corner_distance(self):
        for start, values in reversed(self.corner_events):
            if len(values) > 1 and f32(float(self.distance) - start) <= 1000.0 and values[0] > 3:
                length = values[1] if len(values) > 1 else 200
                return start, f32(float(length) + start)
        return None


@lru_cache(maxsize=None)
def get_course(course_id: int) -> Course:
    cs = params.course_set(course_id)
    m = cs.master
    c = Course(id=course_id, distance=m.distance, race_track_id=m.race_track_id, ground=m.ground,
               turn=m.turn, lane_max=f32(m.float_lane_max / 10000.0), master=m)
    pending = None
    for typ, dist, _off, values in cs.events:
        if typ == params.EVENT_CORNER:
            c.corner_events.append((dist, values))
            if values:
                length = values[1] if len(values) > 1 else 200
                end = f32(float(length) + dist)
                is_final = not (1000.0 < f32(float(c.distance) - end) or values[0] < 4)
                c.corners.append(Corner(dist, end, values[0], is_final))
        elif typ == params.EVENT_STRAIGHT:
            if values and values[0] == 1:
                pending = (dist, values)
            elif values and values[0] == 2 and pending is not None:
                st, sv = pending
                c.straights.append(Straight(st, dist, sv[1] if len(sv) > 1 else 0,
                                            sv[2] if len(sv) > 2 else -1))
                pending = None
        elif typ == params.EVENT_SLOPE and len(values) == 2:
            per = f32(values[0] / 10000.0)
            typ_s = SLOPE_UP if per > 0.0 else (SLOPE_DOWN if per < 0.0 else SLOPE_NONE)
            c.slopes.append(Slope(dist, f32(float(values[1]) + dist), typ_s, per))
        elif typ == params.EVENT_LANE_MAX:
            c.lane_max_changes.append((dist, values))
        elif typ == params.EVENT_MOVE_LANE_POINT:
            c.move_lane_point = dist
    return c
