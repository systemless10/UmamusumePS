"""Port of CourseData.ts."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NamedTuple

# Phase = 0 | 1 | 2 | 3


class Surface:
    TURF = 1
    DIRT = 2


class DistanceType:
    SHORT = 1
    MILE = 2
    MID = 3
    LONG = 4


class Orientation:
    CLOCKWISE = 1
    COUNTERCLOCKWISE = 2
    UNUSED = 3
    NO_TURNS = 4


class ThresholdStat:
    SPEED = 1
    STAMINA = 2
    POWER = 3
    GUTS = 4
    INT = 5


class Corner(NamedTuple):
    start: float
    length: float


class Straight(NamedTuple):
    start: float
    end: float
    front_type: int


class Slope(NamedTuple):
    start: float
    length: float
    slope: float


class CourseData:
    __slots__ = (
        "race_track_id", "distance", "distance_type", "surface", "turn",
        "course_set_status", "corners", "straights", "slopes", "max_lane",
    )

    def __init__(self, race_track_id, distance, distance_type, surface, turn,
                 course_set_status, corners, straights, slopes, max_lane):
        self.race_track_id = race_track_id
        self.distance = distance
        self.distance_type = distance_type
        self.surface = surface
        self.turn = turn
        self.course_set_status = course_set_status
        self.corners = corners
        self.straights = straights
        self.slopes = slopes
        self.max_lane = max_lane


_DATA_PATH = Path(__file__).resolve().parent / "data" / "course_data.json"
_raw_courses: dict | None = None
_course_cache: dict[int, CourseData] = {}


def _raw() -> dict:
    global _raw_courses
    if _raw_courses is None:
        _raw_courses = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _raw_courses


def is_sorted_by_start(items) -> bool:
    last = -1
    for item in items:
        if item.start <= last:
            return False
        last = item.start
    return True


def phase_start(distance: float, phase: int) -> float:
    if phase == 0:
        return 0
    if phase == 1:
        return distance * 1 / 6
    if phase == 2:
        return distance * 2 / 3
    if phase == 3:
        return distance * 5 / 6
    raise ValueError(phase)


def phase_end(distance: float, phase: int) -> float:
    if phase == 0:
        return distance * 1 / 6
    if phase == 1:
        return distance * 2 / 3
    if phase == 2:
        return distance * 5 / 6
    if phase == 3:
        return distance
    raise ValueError(phase)


def course_speed_modifier(course: CourseData, stats) -> float:
    """`stats` needs .speed/.stamina/.power/.guts/.wisdom attributes."""
    statvalues = [0, min(stats.speed, 901), min(stats.stamina, 901),
                  min(stats.power, 901), min(stats.guts, 901), min(stats.wisdom, 901)]
    total = sum((1 + math.floor(statvalues[stat] / 300.01)) * 0.05 for stat in course.course_set_status)
    return 1 + total / max(len(course.course_set_status), 1)


def get_course(course_id: int) -> CourseData:
    cached = _course_cache.get(course_id)
    if cached is not None:
        return cached
    raw = _raw()[str(course_id)]
    slopes = [Slope(s["start"], s["length"], s["slope"]) for s in raw.get("slopes", [])]
    if not is_sorted_by_start(slopes):
        slopes = sorted(slopes, key=lambda s: s.start)
    corners = [Corner(c["start"], c["length"]) for c in raw.get("corners", [])]
    straights = [Straight(s["start"], s["end"], s["frontType"]) for s in raw.get("straights", [])]
    course = CourseData(
        race_track_id=raw["raceTrackId"],
        distance=raw["distance"],
        distance_type=raw["distanceType"],
        surface=raw["surface"],
        turn=raw["turn"],
        course_set_status=list(raw.get("courseSetStatus", [])),
        corners=corners,
        straights=straights,
        slopes=slopes,
        # raw laneMax/10000 = max lane in course-widths (confirmed against
        # real track data: Tokyo turf, the doc's stated widest course, comes
        # out to exactly 1.5; narrow NAR dirt tracks come out around 1.1-1.2,
        # matching the doc's stated range).
        max_lane=raw.get("laneMax", 13000) / 10000.0,
    )
    _course_cache[course_id] = course
    return course
