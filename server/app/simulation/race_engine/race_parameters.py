
from __future__ import annotations

from enum import IntEnum
from typing import Optional


class GroundCondition(IntEnum):
    GOOD = 1
    YIELDING = 2
    SOFT = 3
    HEAVY = 4


class Weather(IntEnum):
    SUNNY = 1
    CLOUDY = 2
    RAINY = 3
    SNOWY = 4


class Season(IntEnum):
    SPRING = 1
    SUMMER = 2
    AUTUMN = 3
    WINTER = 4
    SAKURA = 5


class RaceTime(IntEnum):
    NO_TIME = 0
    MORNING = 1
    MIDDAY = 2
    EVENING = 3
    NIGHT = 4


class Grade(IntEnum):
    NONE = 0
    SPECIAL = 1000
    G1 = 100
    G2 = 200
    G3 = 300
    OP = 400
    PRE_OP = 700
    MAIDEN = 800
    DEBUT = 900
    DAILY = 999


def parse_ground_condition(g) -> GroundCondition:
    if not isinstance(g, str):
        return GroundCondition(g)
    return GroundCondition[g.upper()]


def parse_weather(w) -> Weather:
    if not isinstance(w, str):
        return Weather(w)
    return Weather[w.upper()]


def parse_season(s) -> Season:
    if not isinstance(s, str):
        return Season(s)
    return Season[s.upper()]


def parse_time(t) -> RaceTime:
    if not isinstance(t, str):
        return RaceTime(t)
    u = t.upper()
    if u in ("NONE", "NOTIME"):
        return RaceTime.NO_TIME
    return RaceTime[u]


def parse_grade(g) -> Grade:
    if not isinstance(g, str):
        return Grade(g)
    u = g.upper()
    if u in ("PRE-OP", "PREOP"):
        return Grade.PRE_OP
    return Grade[u]


class RaceParameters:

    __slots__ = (
        "mood", "ground_condition", "weather", "season", "time", "grade",
        "popularity", "order_range", "num_umas", "chara_ids", "context",
    )

    def __init__(self):
        self.mood: int = 2
        self.ground_condition: GroundCondition = GroundCondition.GOOD
        self.weather: Weather = Weather.SUNNY
        self.season: Season = Season.SPRING
        self.time: RaceTime = RaceTime.MIDDAY
        self.grade: Grade = Grade.G1
        self.popularity: int = 1
        self.order_range: Optional[tuple[int, int]] = None
        self.num_umas: Optional[int] = None
        self.chara_ids: tuple = ()
        self.context: dict = {}


class RaceParametersWithSkillId:

    __slots__ = (
        "mood", "ground_condition", "weather", "season", "time", "grade",
        "popularity", "order_range", "num_umas", "chara_ids", "context", "skill_id",
    )

    def __init__(self, params: RaceParameters, skill_id: str):
        self.mood = params.mood
        self.ground_condition = params.ground_condition
        self.weather = params.weather
        self.season = params.season
        self.time = params.time
        self.grade = params.grade
        self.popularity = params.popularity
        self.order_range = params.order_range
        self.num_umas = params.num_umas
        self.chara_ids = params.chara_ids
        self.context = params.context
        self.skill_id = skill_id
