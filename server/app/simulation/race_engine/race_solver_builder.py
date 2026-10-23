
from __future__ import annotations

import json
import math
import random as _random
from pathlib import Path
from typing import Callable, Optional

from .activation_conditions import ConditionsWithActivateCountsAsRandom
from .activation_sample_policy import AllCornerRandomPolicy, ImmediatePolicy
from .condition_parser import Parser, get_parser
from .course_data import CourseData, course_speed_modifier, get_course, phase_start
from .horse_types import Aptitude, HorseParameters, Strategy, parse_aptitude, parse_strategy
from .hp_policy import GameHpPolicy, NoopHpPolicy
from .race_parameters import (
    Grade, GroundCondition, RaceParameters, RaceParametersWithSkillId, RaceTime, Season, Weather,
    parse_grade, parse_ground_condition, parse_season, parse_time, parse_weather,
)
from .race_solver import Perspective, RaceSolver, SkillEffect, SkillRarity, SkillType, _bucket
from .random_gen import Rule30CARng
from ..race_exact.rng import f32
from .region import Region, RegionList

def _skills() -> dict:
    from .skill_table import skills
    return skills()


_GROUND_SPEED_MODIFIER = [
    None,
    [0, 0, 0, 0, -50],
    [0, 0, 0, 0, -50],
]
_GROUND_POWER_MODIFIER = [
    None,
    [0, 0, -50, -50, -50],
    [0, -100, -50, -100, -100],
]
_STRATEGY_PROFICIENCY_MODIFIER = [1.1, 1.0, 0.85, 0.75, 0.6, 0.4, 0.2, 0.1]


class _Asitame:
    STRATEGY_DISTANCE_COEFFICIENT = [
        [],
        [0, 1.0, 0.7, 0.75, 0.7, 1.0],
        [0, 1.0, 0.8, 0.7, 0.75, 1.0],
        [0, 1.0, 0.9, 0.875, 0.86, 1.0],
        [0, 1.0, 0.9, 1.0, 0.9, 1.0],
    ]
    BASE_MODIFIER = 0.00875

    @staticmethod
    def calc_approximate_modifier(power: float, strategy: int, distance_type: int) -> float:
        return _Asitame.BASE_MODIFIER * math.sqrt(power - 1200) * _Asitame.STRATEGY_DISTANCE_COEFFICIENT[distance_type][strategy]


class _StaminaSyoubu:
    @staticmethod
    def distance_factor(distance: float) -> float:
        if distance < 2101:
            return 0.0
        elif distance < 2201:
            return 0.5
        elif distance < 2401:
            return 1.0
        elif distance < 2601:
            return 1.2
        else:
            return 1.5

    @staticmethod
    def calc_approximate_modifier(stamina: float, distance: float) -> float:
        random_factor = 1.0
        return math.sqrt(stamina - 1200) * 0.0085 * _StaminaSyoubu.distance_factor(distance) * random_factor


def _adjust_overcap(stat: float) -> float:
    stat = f32(stat)
    return f32(1200 + f32(f32(stat - 1200) * f32(0.5))) if stat > 1200 else stat


def _base_stat(raw: float, motiv_coef: float) -> float:
    return max(1.0, min(2000.0, f32(_adjust_overcap(raw) * motiv_coef)))


def build_base_stats(horse_desc: dict, mood: int) -> HorseParameters:
    motiv_coef = f32(f32(10000 + 200 * mood) * f32(0.0001))
    return HorseParameters(
        speed=_base_stat(horse_desc["speed"], motiv_coef),
        stamina=_base_stat(horse_desc["stamina"], motiv_coef),
        power=_base_stat(horse_desc["power"], motiv_coef),
        guts=_base_stat(horse_desc["guts"], motiv_coef),
        wisdom=_base_stat(horse_desc["wisdom"], motiv_coef),
        strategy=parse_strategy(horse_desc["strategy"]),
        distance_aptitude=parse_aptitude(horse_desc["distanceAptitude"], "distance"),
        surface_aptitude=parse_aptitude(horse_desc["surfaceAptitude"], "surface"),
        strategy_aptitude=parse_aptitude(horse_desc["strategyAptitude"], "strategy"),
        raw_stamina=horse_desc["stamina"] * motiv_coef,
    )


SINGLE_MODE_MODIFIER = 400.0


def _clamp_stat(v: float) -> float:
    return max(1.0, min(2000.0, f32(v)))


def build_adjusted_stats(base_stats: HorseParameters, course: CourseData, ground: int,
                         single_mode: bool = False) -> HorseParameters:
    race_course_modifier = course_speed_modifier(course, base_stats)
    sm = SINGLE_MODE_MODIFIER if single_mode else 0.0
    return HorseParameters(
        speed=_clamp_stat(f32(f32(base_stats.speed * f32(race_course_modifier)) + _GROUND_SPEED_MODIFIER[course.surface][ground]) + sm),
        stamina=_clamp_stat(base_stats.stamina + sm),
        power=_clamp_stat(base_stats.power + _GROUND_POWER_MODIFIER[course.surface][ground] + sm),
        guts=_clamp_stat(base_stats.guts + sm),
        wisdom=_clamp_stat(f32(base_stats.wisdom * f32(_STRATEGY_PROFICIENCY_MODIFIER[base_stats.strategy_aptitude])) + sm),
        strategy=base_stats.strategy,
        distance_aptitude=base_stats.distance_aptitude,
        surface_aptitude=base_stats.surface_aptitude,
        strategy_aptitude=base_stats.strategy_aptitude,
        raw_stamina=base_stats.raw_stamina,
    )


class SkillTarget:
    SELF = 1
    ALL = 2
    IN_FOV = 4
    AHEAD_OF_POSITION = 7
    AHEAD_OF_SELF = 9
    BEHIND_SELF = 10
    ALL_ALLIES = 11
    ENEMY_STRATEGY = 18
    KAKARI_AHEAD = 19
    KAKARI_BEHIND = 20
    KAKARI_STRATEGY = 21
    UMA_ID = 22
    USED_RECOVERY = 23


_VALID_SKILL_TYPES = set(int(x) for x in SkillType)


def _is_target(self_persp: int, target_type: int) -> bool:
    return target_type == SkillTarget.ALL or self_persp == Perspective.ANY or ((self_persp == Perspective.SELF) == (target_type == SkillTarget.SELF))


class SkillData:
    __slots__ = ("skill_id", "perspective", "rarity", "wisdom_check", "sample_policy",
                 "regions", "extra_condition", "effects", "cooldown", "green")

    def __init__(self, skill_id, perspective, rarity, wisdom_check, sample_policy, regions, extra_condition, effects,
                 cooldown: float = 0.0, green: bool = False):
        self.cooldown = cooldown
        self.green = green
        self.skill_id = skill_id
        self.perspective = perspective
        self.rarity = rarity
        self.wisdom_check = wisdom_check
        self.sample_policy = sample_policy
        self.regions = regions
        self.extra_condition = extra_condition
        self.effects = effects


_LEVEL_MULTIPLIER_TARGET_SPEED = [1.00, 1.00, 1.01, 1.04, 1.07, 1.10, 1.13, 1.16, 1.19, 1.22, 1.25]
_LEVEL_MULTIPLIER_ACCEL = [1.00, 1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.125, 1.15, 1.175, 1.20]
_LEVEL_MULTIPLIER_STAT = [1.00, 1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.10]
_LEVEL_MULTIPLIER_OTHER = [1.00, 1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12, 1.14, 1.16, 1.18]

_STAT_SKILL_TYPES = frozenset([SkillType.SPEED_UP, SkillType.STAMINA_UP, SkillType.POWER_UP,
                                SkillType.GUTS_UP, SkillType.WISDOM_UP,
                                SkillType.ALL_STATUS_UP])


def _level_multiplier(skill_type: int, level: int) -> float:
    level = max(1, min(10, level))
    if skill_type == SkillType.TARGET_SPEED:
        return _LEVEL_MULTIPLIER_TARGET_SPEED[level]
    if skill_type == SkillType.ACCEL:
        return _LEVEL_MULTIPLIER_ACCEL[level]
    if skill_type in _STAT_SKILL_TYPES:
        return _LEVEL_MULTIPLIER_STAT[level]
    if skill_type == SkillType.NOOP:
        return 1.0
    return _LEVEL_MULTIPLIER_OTHER[level]


_MULTIPLY_RANDOM_USAGES = (8, 9)


def _multiply_random(roll: float) -> float:
    if roll < 0.6:
        return 0.0
    return 0.02 if roll < 0.9 else 0.04


_TEAM_TOTAL_MULT = ((1200, 0.8), (1800, 0.9), (2600, 1.0), (3600, 1.1), (None, 1.2))
_WIN_COUNT_MULT = ((6, 0.8), (14, 0.9), (18, 1.0), (25, 1.1), (None, 1.2))
_MAX_RAW_STAT_MULT = ((600, 0.8), (800, 0.9), (1000, 1.0), (1100, 1.1), (None, 1.2))
_FAN_COUNT_MULT = ((100000, 0.8), (200000, 0.9), (300000, 1.0), (400000, 1.1), (None, 1.2))


def _static_value_multiplier(value_usage, ctx: dict) -> float:
    if value_usage == 2:
        return min(1.0 + 0.01 * (ctx.get("numSkills") or 0), 1.2)
    if value_usage in (3, 4, 5, 6, 7):
        totals = ctx.get("teamStatTotals")
        return _bucket(totals[value_usage - 3], _TEAM_TOTAL_MULT) if totals else 1.0
    if value_usage == 10 and ctx.get("winCount") is not None:
        return _bucket(ctx["winCount"], _WIN_COUNT_MULT)
    if value_usage == 12 and ctx.get("fanCount") is not None:
        return _bucket(ctx["fanCount"], _FAN_COUNT_MULT)
    if value_usage == 13 and ctx.get("maxRawStat") is not None:
        return _bucket(ctx["maxRawStat"], _MAX_RAW_STAT_MULT)
    return 1.0


def _build_skill_effects(skill: dict, perspective: int, level: int, rng=None, ctx: Optional[dict] = None) -> list[SkillEffect]:
    out = []
    for ef in skill["effects"]:
        t = ef["type"]
        target = ef.get("target", SkillTarget.SELF)
        effective_type = t if t in _VALID_SKILL_TYPES else SkillType.NOOP
        if perspective != Perspective.SELF and not _is_target(perspective, target):
            effective_type = SkillType.NOOP
        level_factor = _level_multiplier(effective_type, level)
        usage = ef.get("levelUsage", 1)
        if usage == 2:
            level_factor = 1.0
        elif usage == 3:
            level_factor = 1.0 / level_factor
        modifier = ef["modifier"] / 10000 * level_factor
        value_usage = ef.get("valueUsage", 1)
        if value_usage in _MULTIPLY_RANDOM_USAGES:
            modifier *= _multiply_random(rng.random()) if rng is not None else 0.0
        else:
            modifier *= _static_value_multiplier(value_usage, ctx or {})
        out.append(SkillEffect(effective_type, skill["baseDuration"] / 10000, modifier,
                               target=target if perspective == Perspective.SELF else SkillTarget.SELF,
                               target_value=ef.get("targetValue", 0),
                               time_usage=skill.get("timeUsage", 1),
                               value_usage=value_usage,
                               additional_activate=ef.get("additionalActivate", 0)))
    return out


def build_skill_data(horse: HorseParameters, race_params: RaceParameters, course: CourseData,
                      whole_course: RegionList, parser: Parser, skill_id: str, perspective: int,
                      level: int = 1, ignore_null_effects: bool = False, rng=None,
                      ctx: Optional[dict] = None) -> list[SkillData]:
    skills = _skills()
    if skill_id not in skills:
        raise ValueError(f"bad skill ID {skill_id}")
    extra = RaceParametersWithSkillId(race_params, skill_id)
    alternatives = skills[skill_id]["alternatives"]
    triggers: list[SkillData] = []
    for skill in alternatives:
        full = RegionList(whole_course)
        pre_check = None
        precondition = skill.get("precondition")
        if precondition:
            pre = parser.parse(parser.tokenize(precondition))
            pre_regions, pre_check = pre.apply(whole_course, course, horse, extra)
            if len(pre_regions) == 0:
                continue
            bounds = Region(pre_regions[0].start, whole_course[-1].end)
            full = full.rmap(lambda r: r.intersect(bounds))

        op = parser.parse(parser.tokenize(skill["condition"]))
        regions, extra_condition = op.apply(full, course, horse, extra)
        if pre_check is not None:
            activation_check = extra_condition
            seen = [False]
            def check(state, pre=pre_check, active=activation_check, bounds=pre_regions, seen=seen):
                if any(r.start <= state.pos < r.end for r in bounds) and pre(state):
                    seen[0] = True
                return seen[0] and (active is None or active(state))
            check.observe_precondition = True
            extra_condition = check
        if len(regions) == 0:
            continue
        if triggers and "is_activate_other_skill_detail" not in skill["condition"] and "is_used_skill_id" not in skill["condition"]:
            continue
        effects = _build_skill_effects(skill, perspective, level, rng, ctx)
        if effects or ignore_null_effects:
            rarity = skills[skill_id]["rarity"]
            triggers.append(SkillData(
                skill_id=skill_id,
                perspective=perspective,
                rarity=3 if 3 <= rarity <= 5 else rarity,
                wisdom_check=bool(skills[skill_id]["wisdomCheck"]),
                sample_policy=op.sample_policy,
                regions=regions,
                extra_condition=extra_condition,
                effects=effects,
                cooldown=skill.get("cooldown", 0.0) / 10000,
                green=any(601 <= t <= 615 for t in skills[skill_id].get("tags", ())),
            ))
    if triggers:
        return triggers
    effects = _build_skill_effects(alternatives[0], perspective, level, rng, ctx)
    if not effects and not ignore_null_effects:
        return []
    rarity = skills[skill_id]["rarity"]
    after_end = RegionList([Region(9999, 9999)])
    return [SkillData(
        skill_id=skill_id,
        perspective=perspective,
        rarity=3 if 3 <= rarity <= 5 else rarity,
        wisdom_check=bool(skills[skill_id]["wisdomCheck"]),
        sample_policy=ImmediatePolicy,
        regions=after_end,
        extra_condition=(lambda _s: False),
        effects=effects,
    )]


_default_parser = get_parser()
_acr_parser = get_parser(ConditionsWithActivateCountsAsRandom)


class RaceSolverBuilder:
    def __init__(self, nsamples: int):
        self.nsamples = nsamples
        self._course: Optional[CourseData] = None
        self._race_params = RaceParameters()
        self._horse: Optional[dict] = None
        self._gate_index: int = 0
        self._pacer: Optional[dict] = None
        self._pacer_skills: list = []
        self._rng = Rule30CARng.seeded(_random.getrandbits(32))
        self._parser: Parser = _default_parser
        self._skills: list[tuple[str, int, int]] = []
        self._wisdom_seeds: dict[str, tuple[int, int]] = {}
        self._use_wisdom_checks = False
        self._other_raw_wisdom = 2000
        self._other_mood = 2
        self._hp_policy_factory: Callable = lambda course, params, rng: GameHpPolicy(course, params.ground_condition, rng)
        self._sample_policy_override: list[Optional[dict]] = [None, {}, {}, {}]
        self._extra_skill_hooks: list[Callable] = []
        self._on_skill_activate: Optional[Callable] = None
        self._on_skill_deactivate: Optional[Callable] = None
        self._single_mode = False
        self._context: dict = {}

    def seed(self, lo: int, hi: int = 0) -> "RaceSolverBuilder":
        self._rng = Rule30CARng.seeded(((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF))
        return self

    def course(self, course) -> "RaceSolverBuilder":
        self._course = get_course(course) if isinstance(course, int) else course
        return self

    def mood(self, mood: int) -> "RaceSolverBuilder":
        self._race_params.mood = mood
        return self

    def ground(self, ground) -> "RaceSolverBuilder":
        self._race_params.ground_condition = parse_ground_condition(ground)
        return self

    def weather(self, weather) -> "RaceSolverBuilder":
        self._race_params.weather = parse_weather(weather)
        return self

    def season(self, season) -> "RaceSolverBuilder":
        self._race_params.season = parse_season(season)
        return self

    def time(self, time) -> "RaceSolverBuilder":
        self._race_params.time = parse_time(time)
        return self

    def grade(self, grade) -> "RaceSolverBuilder":
        self._race_params.grade = parse_grade(grade)
        return self

    def popularity(self, popularity: int) -> "RaceSolverBuilder":
        self._race_params.popularity = popularity
        return self

    def order(self, start: int, end: int) -> "RaceSolverBuilder":
        self._race_params.order_range = (start, end)
        return self

    def num_umas(self, n: int) -> "RaceSolverBuilder":
        self._race_params.num_umas = n
        return self

    def horse(self, horse: dict) -> "RaceSolverBuilder":
        self._horse = horse
        return self

    def single_mode(self, flag: bool = True) -> "RaceSolverBuilder":
        self._single_mode = bool(flag)
        return self

    def context(self, ctx: dict) -> "RaceSolverBuilder":
        self._context = dict(ctx or {})
        self._race_params.context = self._context
        return self

    def chara_ids(self, ids) -> "RaceSolverBuilder":
        self._race_params.chara_ids = tuple(ids)
        return self

    def gate_index(self, index: int) -> "RaceSolverBuilder":
        self._gate_index = index
        return self

    def pacer(self, horse: dict) -> "RaceSolverBuilder":
        self._pacer = horse
        return self

    def _is_nige(self) -> bool:
        s = self._horse["strategy"]
        if isinstance(s, str):
            return s.upper() in ("NIGE", "OONIGE")
        return s in (Strategy.NIGE, Strategy.OONIGE)

    def use_default_pacer(self, opening_leg_accel: bool = True) -> "RaceSolverBuilder":
        if self._is_nige():
            return self
        self._pacer = dict(self._horse)
        self._pacer["strategy"] = "Nige"
        if opening_leg_accel:
            self._pacer_skills = [
                _PendingSkillLiteral('201601', Perspective.SELF, SkillRarity.WHITE, Region(0, 100),
                                     (lambda _s: True), [SkillEffect(SkillType.ACCEL, 3.0, 0.2)]),
                _PendingSkillLiteral('200532', Perspective.SELF, SkillRarity.WHITE, Region(0, 100),
                                     (lambda _s: True), [SkillEffect(SkillType.ACCEL, 1.2, 0.2)]),
            ]
        return self

    def hp_policy_factory(self, fn: Callable) -> "RaceSolverBuilder":
        self._hp_policy_factory = fn
        return self

    def with_activate_counts_as_random(self) -> "RaceSolverBuilder":
        self._parser = _acr_parser
        return self

    def with_asiwotameru(self) -> "RaceSolverBuilder":
        base_displayed_power = self._horse["power"] * (1 + 0.02 * self._race_params.mood)

        def hook(skilldata: list[SkillData], horse: HorseParameters, course: CourseData):
            power = base_displayed_power
            for sd in skilldata:
                power_up = next((ef for ef in sd.effects if ef.type == SkillType.POWER_UP), None)
                if power_up and len(sd.regions) > 0 and sd.regions[0].start < 9999:
                    power += power_up.modifier
            if power > 1200:
                spurt_start = RegionList([Region(phase_start(course.distance, 2), course.distance)])
                skilldata.append(SkillData(
                    skill_id='asitame', perspective=Perspective.SELF, rarity=SkillRarity.WHITE,
                    wisdom_check=False, regions=spurt_start, sample_policy=ImmediatePolicy,
                    extra_condition=(lambda s: s.is_last_spurt),
                    effects=[SkillEffect(
                        SkillType.ACCEL, 3.0 / (course.distance / 1000.0),
                        _Asitame.calc_approximate_modifier(power, horse.strategy, course.distance_type),
                    )],
                ))

        self._extra_skill_hooks.append(hook)
        return self

    def with_stamina_syoubu(self) -> "RaceSolverBuilder":
        def hook(skilldata: list[SkillData], horse: HorseParameters, course: CourseData):
            stamina = horse.raw_stamina
            for sd in skilldata:
                stamina_up = next((ef for ef in sd.effects if ef.type == SkillType.STAMINA_UP), None)
                if stamina_up and len(sd.regions) > 0 and sd.regions[0].start < 9999:
                    stamina += stamina_up.modifier
            if stamina > 1200:
                spurt_start = RegionList([Region(phase_start(course.distance, 2), course.distance)])
                skilldata.append(SkillData(
                    skill_id='staminasyoubu', perspective=Perspective.SELF, rarity=SkillRarity.WHITE,
                    wisdom_check=False, regions=spurt_start, sample_policy=ImmediatePolicy,
                    extra_condition=(lambda s: s.current_speed >= s.last_spurt_speed),
                    effects=[SkillEffect(
                        SkillType.TARGET_SPEED, 9999.0,
                        _StaminaSyoubu.calc_approximate_modifier(stamina, course.distance),
                    )],
                ))

        self._extra_skill_hooks.append(hook)
        return self

    def with_wisdom_checks(self, seeds: dict[str, tuple[int, int]]) -> "RaceSolverBuilder":
        self._use_wisdom_checks = True
        self._wisdom_seeds.update(seeds)
        return self

    def other_raw_wisdom(self, wisdom: float, mood: Optional[int] = None) -> "RaceSolverBuilder":
        self._other_raw_wisdom = wisdom
        self._other_mood = mood if mood is not None else self._race_params.mood
        return self

    def add_skill(self, skill_id: str, perspective: int = Perspective.SELF, sample_policy=None,
                  level: int = 1) -> "RaceSolverBuilder":
        self._skills.append((skill_id, perspective, level))
        if sample_policy is not None:
            self._sample_policy_override[perspective][skill_id] = sample_policy
        return self

    def on_skill_activate(self, cb: Callable) -> "RaceSolverBuilder":
        self._on_skill_activate = cb
        return self

    def on_skill_deactivate(self, cb: Callable) -> "RaceSolverBuilder":
        self._on_skill_deactivate = cb
        return self

    def fork(self) -> "RaceSolverBuilder":
        clone = RaceSolverBuilder(self.nsamples)
        clone._course = self._course
        clone._race_params = RaceParameters()
        for slot in RaceParameters.__slots__:
            setattr(clone._race_params, slot, getattr(self._race_params, slot))
        clone._horse = self._horse
        clone._gate_index = self._gate_index
        clone._single_mode = self._single_mode
        clone._context = dict(self._context)
        clone._pacer = self._pacer
        clone._pacer_skills = list(self._pacer_skills)
        clone._rng = Rule30CARng(self._rng.lo, self._rng.hi)
        clone._parser = self._parser
        clone._skills = list(self._skills)
        clone._use_wisdom_checks = self._use_wisdom_checks
        clone._wisdom_seeds = dict(self._wisdom_seeds)
        clone._other_raw_wisdom = self._other_raw_wisdom
        clone._other_mood = self._other_mood
        clone._hp_policy_factory = self._hp_policy_factory
        clone._sample_policy_override = [None if m is None else dict(m) for m in self._sample_policy_override]
        clone._on_skill_activate = self._on_skill_activate
        clone._on_skill_deactivate = self._on_skill_deactivate
        clone._extra_skill_hooks = list(self._extra_skill_hooks)
        return clone

    def build(self):
        horse = build_base_stats(self._horse, self._race_params.mood)
        solver_rng = Rule30CARng.seeded(self._rng.int32())
        pacer_rng = Rule30CARng.seeded(self._rng.int32())
        skill_rng = Rule30CARng.seeded(self._rng.int32())

        pacer_horse = None
        if self._pacer:
            pacer_horse = build_adjusted_stats(
                build_base_stats(self._pacer, self._race_params.mood), self._course, self._race_params.ground_condition)

        whole_course = RegionList([Region(0, self._course.distance)])

        other_base_wisdom = _adjust_overcap(self._other_raw_wisdom) * (1 + 0.02 * self._other_mood)
        skill_activation_chance = [
            0.0,
            max(1 - 90 / horse.wisdom, 0.2),
            max(1 - 90 / other_base_wisdom, 0.2),
            1.0,
        ]

        skilldata: list[SkillData] = []
        for skill_id, p, level in self._skills:
            skilldata.extend(build_skill_data(horse, self._race_params, self._course, whole_course, self._parser,
                                              skill_id, p, level, rng=skill_rng,
                                              ctx=dict(self._context, numSkills=len(self._skills))))
        for hook in self._extra_skill_hooks:
            hook(skilldata, horse, self._course)

        triggers = []
        followups = []
        for sd in skilldata:
            override = self._sample_policy_override[sd.perspective]
            sp = (override.get(sd.skill_id) if override else None) or sd.sample_policy
            if sp is AllCornerRandomPolicy:
                sets = [sp.place_all_triggers(sd.regions, self._rng) for _ in range(self.nsamples)]
                triggers.append([ts[0] for ts in sets])
                followups.append([tuple(ts[1:]) for ts in sets])
            else:
                triggers.append(sp.sample(sd.regions, self.nsamples, self._rng))
                followups.append(None)

        wisdom_rngs = {sid: Rule30CARng(*seed) for sid, seed in self._wisdom_seeds.items()}

        own_chance = max(1 - 90 / horse.wisdom, 0.2)
        live = []
        passed: dict[str, bool] = {}
        for sdi, sd in enumerate(skilldata):
            if sd.wisdom_check and sd.perspective == Perspective.SELF:
                if sd.skill_id not in passed:
                    passed[sd.skill_id] = skill_rng.random() < own_chance
                if not passed[sd.skill_id]:
                    continue
            live.append(sdi)
        skilldata = [skilldata[i] for i in live]
        triggers = [triggers[i] for i in live]
        followups = [followups[i] for i in live]
        cooldown_scale = self._course.distance / 1000.0

        horse = build_adjusted_stats(horse, self._course, self._race_params.ground_condition,
                                     single_mode=self._single_mode)

        lastskills = None
        i = 0
        while i < self.nsamples:
            if lastskills is not None:
                skills = lastskills
                lastskills = None
            else:
                skills = []
                for sdi, sd in enumerate(skilldata):
                    if self._use_wisdom_checks and sd.wisdom_check:
                        if not (wisdom_rngs[sd.skill_id].random() < skill_activation_chance[sd.perspective]):
                            continue
                    trig = triggers[sdi][i % len(triggers[sdi])]
                    more = followups[sdi][i % len(followups[sdi])] if followups[sdi] else ()
                    skills.append(_PendingSkillLiteral(sd.skill_id, sd.perspective, sd.rarity, trig, sd.extra_condition, sd.effects,
                                                       cooldown=sd.cooldown * cooldown_scale, followups=more, green=sd.green))

            backup_pacer_rng = Rule30CARng(pacer_rng.lo, pacer_rng.hi)
            backup_solver_rng = Rule30CARng(solver_rng.lo, solver_rng.hi)

            pacer = None
            if pacer_horse is not None:
                pacer = RaceSolver(
                    horse=pacer_horse, course=self._course, hp=NoopHpPolicy(),
                    skills=self._pacer_skills, rng=pacer_rng,
                )

            solver = RaceSolver(
                horse=horse, course=self._course, skills=skills, pacer=pacer,
                hp=self._hp_policy_factory(self._course, self._race_params, Rule30CARng.seeded(solver_rng.int32())),
                rng=solver_rng, on_skill_activate=self._on_skill_activate, on_skill_deactivate=self._on_skill_deactivate,
                gate_index=self._gate_index,
            )
            solver.chara_id = int(self._context.get("charaId") or 0)
            solver.is_viewer = bool(self._context.get("isViewer"))
            solver.context = dict(self._context)

            redo = yield solver

            if redo:
                pacer_rng = backup_pacer_rng
                solver_rng = backup_solver_rng
                lastskills = skills
                continue
            i += 1


def _PendingSkillLiteral(skill_id, perspective, rarity, trigger, extra_condition, effects,
                         cooldown=0.0, followups=(), green=False):
    from .race_solver import PendingSkill
    return PendingSkill(skill_id, perspective, rarity, trigger, extra_condition, effects,
                        cooldown, followups, green)
