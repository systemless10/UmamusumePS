
from __future__ import annotations

import math
from enum import IntEnum
from typing import Callable, Optional

from .course_data import CourseData, phase_start
from .horse_types import HorseParameters, Strategy, strategy_matches
from .random_gen import Rule30CARng
from ..race_exact.rng import f32
from .region import Region


_SPEED_STRATEGY_PHASE_COEFFICIENT = [
    [],
    [1.0, 0.98, 0.962],
    [0.978, 0.991, 0.975],
    [0.93, 0.998, 0.994],
    [0.931, 1.0, 1.02],
    [1.063, 0.962, 0.95],
]
_SPEED_DISTANCE_PROFICIENCY_MODIFIER = [1.05, 1.0, 0.9, 0.8, 0.6, 0.4, 0.2, 0.1]


def _base_speed(course: CourseData) -> float:
    return 20.0 - (course.distance - 2000) / 1000.0


def _base_target_speed(horse: HorseParameters, course: CourseData, phase: int) -> float:
    return (_base_speed(course) * _SPEED_STRATEGY_PHASE_COEFFICIENT[horse.strategy][phase]
            + (1 if phase == 2 else 0) * math.sqrt(500.0 * horse.speed)
            * _SPEED_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude] * 0.002)


def _last_spurt_speed(horse: HorseParameters, course: CourseData) -> float:
    return (
        (_base_target_speed(horse, course, 2) + 0.01 * _base_speed(course)) * 1.05
        + math.sqrt(500.0 * horse.speed) * _SPEED_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude] * 0.002
        + math.pow(450.0 * horse.guts, 0.597) * 0.0001
    )


_ACCEL_STRATEGY_PHASE_COEFFICIENT = [
    [],
    [1.0, 1.0, 0.996],
    [0.985, 1.0, 0.996],
    [0.975, 1.0, 1.0],
    [0.945, 1.0, 0.997],
    [1.17, 0.94, 0.956],
]
_ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER = [1.05, 1.0, 0.9, 0.8, 0.7, 0.5, 0.3, 0.1]
_ACCEL_DISTANCE_PROFICIENCY_MODIFIER = [1.0, 1.0, 1.0, 1.0, 1.0, 0.6, 0.5, 0.4]

_BASE_ACCEL = 0.0006
_UPHILL_BASE_ACCEL = 0.0004


def _base_accel(base_accel_const: float, horse: HorseParameters, phase: int) -> float:
    value = f32(math.sqrt(f32(f32(500.0) * f32(horse.power))))
    value = f32(value * f32(base_accel_const))
    value = f32(value * f32(_ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER[horse.surface_aptitude]))
    value = f32(value * f32(_ACCEL_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude]))
    return f32(value * f32(_ACCEL_STRATEGY_PHASE_COEFFICIENT[horse.strategy][phase]))


_PHASE_DECELERATION = [-1.2, -0.8, -1.0]

_STRATEGY_PROFICIENCY_MODIFIER_APPROX = [1.1, 1.0, 0.85, 0.75, 0.6, 0.4, 0.2, 0.1]

_COMPETE_DIST_THRESHOLD = {
    Strategy.OONIGE: 0.0, Strategy.NIGE: 0.0, Strategy.SENKOU: 2.5, Strategy.SASI: 5.0, Strategy.OIKOMI: 10.0,
}
_COMPETE_STRATEGY_COEF = {
    Strategy.OONIGE: 0.2, Strategy.NIGE: 0.8, Strategy.SENKOU: 1.0, Strategy.SASI: 1.0, Strategy.OIKOMI: 1.0,
}
_COMPETE_STAMINA_STRATEGY_COEF = {
    Strategy.OONIGE: 1.5, Strategy.NIGE: 1.2, Strategy.SENKOU: 1.0, Strategy.SASI: 1.0, Strategy.OIKOMI: 1.0,
}
_SECURE_LEAD_COEF = {
    Strategy.OONIGE: {Strategy.NIGE: 2.0, Strategy.SENKOU: 7.0, Strategy.SASI: 8.0, Strategy.OIKOMI: 8.0},
    Strategy.NIGE: {Strategy.SENKOU: 4.0, Strategy.SASI: 8.0, Strategy.OIKOMI: 8.0},
    Strategy.SENKOU: {Strategy.SASI: 5.0, Strategy.OIKOMI: 6.0},
    Strategy.SASI: {Strategy.OIKOMI: 3.0},
}
_SECURE_LEAD_STRATEGY_COEF = {Strategy.OONIGE: 0.2, Strategy.NIGE: 1.0, Strategy.SENKOU: 1.0, Strategy.SASI: 0.8}
_SECURE_LEAD_STAMINA_STRATEGY_COEF = {Strategy.OONIGE: 1.2, Strategy.NIGE: 1.0, Strategy.SENKOU: 0.8, Strategy.SASI: 0.8}

_FORWARDNESS = {Strategy.OONIGE: 0, Strategy.NIGE: 1, Strategy.SENKOU: 2, Strategy.SASI: 3, Strategy.OIKOMI: 4}

_FORCE_IN_STRATEGY = {Strategy.NIGE: 0.02, Strategy.OONIGE: 0.02, Strategy.SENKOU: 0.01,
                      Strategy.SASI: 0.01, Strategy.OIKOMI: 0.03}

HORSE_LANE = 1.0 / 18.0
_VISIBLE_DISTANCE = 20.0


def _competition_distance_coefficient(distance: float) -> float:
    if distance < 1801:
        return 0.3
    if distance < 2101:
        return 0.5
    if distance < 2201:
        return 0.8
    if distance < 2401:
        return 1.0
    if distance < 2601:
        return 1.1
    return 1.2


class _PositionKeep:
    BASE_MIN_THRESHOLD = [0, 0, 3.0, 6.5, 7.5, 0]
    BASE_MAX_THRESHOLD = [0, 0, 5.0, 7.0, 8.0, 0]

    @staticmethod
    def course_factor(distance: float) -> float:
        return 0.0008 * (distance - 1000) + 1.0

    @staticmethod
    def min_threshold(strategy: int, distance: float) -> float:
        factor = 1.0 if strategy == Strategy.SENKOU else _PositionKeep.course_factor(distance)
        return _PositionKeep.BASE_MIN_THRESHOLD[strategy] * factor

    @staticmethod
    def max_threshold(strategy: int, distance: float) -> float:
        return _PositionKeep.BASE_MAX_THRESHOLD[strategy] * _PositionKeep.course_factor(distance)


class TemptationMode(IntEnum):
    NULL = 0
    POSITION_SASHI = 1
    POSITION_SENKO = 2
    POSITION_NIGE = 3
    BOOST = 4


_TEMPTATION_STRATEGY = {
    TemptationMode.POSITION_SASHI: Strategy.SASI,
    TemptationMode.POSITION_SENKO: Strategy.SENKOU,
    TemptationMode.POSITION_NIGE: Strategy.NIGE,
}


class RaceEventType(IntEnum):
    SCORE = 0
    SKILL = 3
    COMPETE_TOP = 4
    COMPETE_FIGHT = 5
    RELEASE_CONSERVE_POWER = 6


class Timer:
    __slots__ = ("t",)

    def __init__(self, t: float = 0):
        self.t = t


class CompensatedAccumulator:

    __slots__ = ("acc", "err")

    def __init__(self, acc: float, err: float = 0.0):
        self.acc = acc
        self.err = err

    def add(self, n: float) -> None:
        t = self.acc + n
        if abs(self.acc) >= abs(n):
            self.err += (self.acc - t) + n
        else:
            self.err += (n - t) + self.acc
        self.acc = t


class Perspective(IntEnum):
    SELF = 1
    OTHER = 2
    ANY = 3


class SkillType(IntEnum):
    NOOP = 0
    SPEED_UP = 1
    STAMINA_UP = 2
    POWER_UP = 3
    GUTS_UP = 4
    WISDOM_UP = 5
    OONIGE = 6
    VISIBLE_DISTANCE = 8
    TARGET_LANE = 35
    DEBUFF_CANCEL = 38
    RECOVERY = 9
    MULTIPLY_START_DELAY = 10
    EXTEND_KAKARI = 13
    SET_START_DELAY = 14
    CURRENT_SPEED = 21
    CURRENT_SPEED_WITH_NATURAL_DECELERATION = 22
    TARGET_SPEED = 27
    MODIFY_KAKARI_CHANCE = 29
    ACCEL = 31
    ALL_STATUS_UP = 32
    LANE_MOVE_SPEED_UP = 28
    ACTIVATE_RANDOM_GOLD = 37
    EXTEND_EVOLVED_DURATION = 42


_STAT_TYPES = frozenset([SkillType.SPEED_UP, SkillType.STAMINA_UP, SkillType.POWER_UP,
                         SkillType.GUTS_UP, SkillType.WISDOM_UP, SkillType.ALL_STATUS_UP])
_TIMED_TYPES = frozenset([SkillType.CURRENT_SPEED, SkillType.CURRENT_SPEED_WITH_NATURAL_DECELERATION,
                          SkillType.TARGET_SPEED, SkillType.ACCEL, SkillType.LANE_MOVE_SPEED_UP,
                          SkillType.VISIBLE_DISTANCE, SkillType.TARGET_LANE])


def _bucket(value: float, table) -> float:
    for bound, mult in table:
        if bound is None or value < bound:
            return mult
    return table[-1][1]


_GREEN_COUNT_MULT = ((3, 0.0), (5, 1.0), (6, 2.0), (None, 3.0))
_SPEED1_MULT = ((1700, 0.0), (1800, 1.0), (1900, 2.0), (2000, 3.0), (None, 4.0))
_SPEED2_MULT = ((1400, 1.0), (1600, 2.0), (None, 3.0))
_CORNER_LATE_ORDER_UP_MULT = ((1, 1.0), (2, 1.1), (3, 1.2), (4, 1.3), (None, 1.4))
_BLOCKED_SIDE_MULT = ((2, 1.0), (4, 2.0), (6, 3.0), (None, 4.0))
_ADDITIONAL_ACTIVATE_LIMIT = {1: 3, 2: 3, 3: 2}


class SkillTarget:
    SELF = 1
    ALL = 2
    ALL_OTHER = 3
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


class SkillRarity(IntEnum):
    WHITE = 1
    GOLD = 2
    UNIQUE = 3
    EVOLUTION = 6


class SkillEffect:
    __slots__ = ("type", "base_duration", "modifier", "target", "target_value", "time_usage",
                 "value_usage", "additional_activate")

    def __init__(self, type_: int, base_duration: float, modifier: float,
                 target: int = SkillTarget.SELF, target_value: int = 0, time_usage: int = 1,
                 value_usage: int = 1, additional_activate: int = 0):
        self.type = type_
        self.base_duration = base_duration
        self.modifier = modifier
        self.target = target
        self.target_value = target_value
        self.time_usage = time_usage
        self.value_usage = value_usage
        self.additional_activate = additional_activate

    def scaled(self, mult: float) -> "SkillEffect":
        return SkillEffect(self.type, self.base_duration, self.modifier * mult, self.target,
                           self.target_value, self.time_usage, self.value_usage,
                           self.additional_activate)


class PendingSkill:
    __slots__ = ("skill_id", "perspective", "rarity", "trigger", "extra_condition", "effects",
                 "cooldown", "followups", "green")

    def __init__(self, skill_id: str, perspective: Optional[int], rarity: int, trigger: Region,
                 extra_condition: Callable, effects: list[SkillEffect], cooldown: float = 0.0,
                 followups: tuple = (), green: bool = False):
        self.skill_id = skill_id
        self.perspective = perspective
        self.rarity = rarity
        self.trigger = trigger
        self.extra_condition = extra_condition
        self.effects = effects
        self.cooldown = cooldown
        self.followups = followups
        self.green = green

    def rearmed(self, pos: float) -> Optional["PendingSkill"]:
        trigger, followups = self.trigger, self.followups
        if followups:
            rest = [r for r in followups if r.end > pos]
            if not rest:
                return None
            trigger, followups = rest[0], tuple(rest[1:])
        return PendingSkill(self.skill_id, self.perspective, self.rarity, trigger,
                            self.extra_condition, self.effects, self.cooldown, followups, self.green)


class _ActiveSkill:
    __slots__ = ("skill_id", "perspective", "duration_timer", "modifier", "natural_deceleration")

    def __init__(self, skill_id, perspective, duration_timer, modifier, natural_deceleration=False):
        self.skill_id = skill_id
        self.perspective = perspective
        self.duration_timer = duration_timer
        self.modifier = modifier
        self.natural_deceleration = natural_deceleration


def _noop_hook(*_args, **_kwargs):
    pass


class RaceSolver:
    def __init__(self, horse: HorseParameters, course: CourseData, rng: Rule30CARng,
                 skills: list[PendingSkill], hp, pacer: Optional["RaceSolver"] = None,
                 on_skill_activate: Optional[Callable] = None,
                 on_skill_deactivate: Optional[Callable] = None,
                 gate_index: int = 0):
        self.horse = horse.clone()
        self.course = course
        self.hp = hp
        self.pacer = pacer
        self.rng = rng
        self.gate_index = gate_index
        self.field = None
        self.field_index = 0
        self.finished = False
        self.pending_skills: list[PendingSkill] = list(skills)
        self.pending_removal: set[str] = set()
        self.used_skills: set[str] = set()
        self.gorosi_rng = Rule30CARng.seeded(self.rng.int32())
        self.pace_effect_rng = Rule30CARng.seeded(self.rng.int32())
        self.timers: list[Timer] = []
        self.accumulatetime = self._new_timer()

        self.activation_log: list[tuple] = []
        self.event_log: list[tuple] = []
        self.kakari_log: list[tuple] = []
        self._cooling: list[tuple] = []
        self._watchers: list[list] = []
        self._order_up_groups: list[list] = []
        self._activation_group = 0
        self._green_activations = 0
        self._corner_late_order_up = 0
        self._mid_side_block = 0.0
        self._mid_side_block_max = 0.0
        self.advantage_types: set = set()
        self.deferred_external: list[tuple] = []
        self.chara_id = 0
        self.is_viewer = False
        self.context: dict = {}
        self.vision_modifiers = []
        self.lane_modifiers = []
        self.debuff_cancel = False
        self.overtake_target_timer = 0.0
        self.overtake_no_order_up_timer = 0.0

        self.gate_roll = self.rng.uniform(12252240)
        self.random_lot = self.rng.uniform(100)
        self.phase = 0
        self.next_phase_transition = phase_start(self.course.distance, 1)
        self.active_target_speed_skills: list[_ActiveSkill] = []
        self.active_current_speed_skills: list[_ActiveSkill] = []
        self.active_accel_skills: list[_ActiveSkill] = []
        self.active_lane_speed_skills: list[_ActiveSkill] = []
        self.activate_count = [0, 0, 0]
        self.activate_count_heal = 0
        self.activate_count_later_half = 0
        self.activate_count_last_frame = 0
        self.on_skill_activate = on_skill_activate or _noop_hook
        self.on_skill_deactivate = on_skill_deactivate or _noop_hook
        self.section_length = self.course.distance / 24.0

        self.is_pace_down = False
        self.pos_keep_end = self.section_length * 10.0
        self.pos_keep_speed_coef = 1.0
        self._pos_keep_mode: Optional[str] = None
        self.pos_keep_effect_exit_distance = 0.0
        self.pos_keep_effect_start = 0.0
        self.pos_keep_timer = self._new_timer()
        self.pos_keep_continue_distance = math.floor(self.section_length) * (
            3 if self.horse.strategy == Strategy.OONIGE else 1)

        self.modifiers_target_speed = CompensatedAccumulator(0.0)
        self.modifiers_current_speed = CompensatedAccumulator(0.0)
        self.modifiers_accel = CompensatedAccumulator(0.0)
        self.modifiers_one_frame_accel = 0.0
        self.modifiers_special_skill_duration_scaling = 1.0
        self.modifiers_kakari_chance = 0.0
        self.lane_speed_skill_modifier = 0.0

        self.start_delay = 0.1 * self.rng.random()
        if self.pacer:
            self.pacer.start_delay = 0.0

        self.pos = 0.0
        self.accel = 0.0
        self.current_speed = 3.0
        self.target_speed = 0.85 * _base_speed(self.course)
        self.is_downhill_mode = False
        self.is_last_spurt = False
        self.last_spurt_start_pos = -1.0

        self.lane = (self.gate_index + self._gate_adjustor()) * HORSE_LANE
        self.lane_target = self.lane
        self.lane_change_speed = 0.0
        self._lane_retarget = True
        self._extra_move_lane: Optional[float] = None
        self._front_blocker_index = -1
        self._blocked_front_timer = 0.0
        self._blocked_side_timer = 0.0
        self._blocked_all_timer = 0.0
        self._infront_near_lane_timer = 0.0
        self._behind_near_lane_timer = 0.0
        self._moved_lane_this_tick = False

        self.compete_top_active = False
        self._compete_top_timer = self._new_timer()
        self._compete_top_modifier = 0.0
        self._compete_spurt_active = False
        self._compete_spurt_timer = self._new_timer()
        self._compete_spurt_check = self._new_timer()
        self._compete_spurt_modifier = 0.0
        self._secure_lead_active = False
        self._secure_lead_timer = self._new_timer()
        self._secure_lead_check = self._new_timer()
        self._secure_lead_modifier = 0.0
        self._stamina_keep_active = False
        self._stamina_keep_check = self._new_timer()
        self._duel_target = -1
        self._duel_continuous_timer = 0.0
        self._duel_active = False
        self._duel_target_speed_modifier = 0.0
        self._duel_accel_modifier = 0.0

        self.is_kakari = False
        self.temptation_mode = TemptationMode.NULL
        self.temptation_count = 0
        self.process_skill_activations()
        self.min_speed = 0.85 * _base_speed(self.course) + math.sqrt(200.0 * self.horse.guts) * 0.001
        self.start_dash = True
        self.modifiers_accel.add(24.0)

        self._init_hills()

        self.base_target_speed = [_base_target_speed(self.horse, self.course, p) for p in (0, 1, 2)]
        self.last_spurt_speed = _last_spurt_speed(self.horse, self.course)
        self.last_spurt_transition = -1

        self.kakari_start = (1 + self.rng.uniform(8)) * self.section_length
        kakari_pct = math.ceil(math.pow(6.5 / math.log10(0.1 * self.horse.wisdom + 1), 2) - 1e-9)
        if self.rng.random() * 100.0 >= kakari_pct + self.modifiers_kakari_chance * 100.0:
            self.kakari_start = self.course.distance + 9999
        r1, r2, r3 = self.rng.random(), self.rng.random(), self.rng.random()
        seq = [0.0, r1, r2, r3, 1.0]
        idx = next(i for i, x in enumerate(seq) if x > 0.45)
        self.kakari_duration = 3.0 * idx
        self.kakari_timer = self._new_timer()
        self.temptation_count = 0

        self.section_modifier: list[float] = []
        for _ in range(24):
            max_v = self.horse.wisdom / 5500.0 * math.log10(self.horse.wisdom * 0.1)
            factor = (max_v - 0.65 + self.rng.random() * 0.65) / 100.0
            self.section_modifier.append(_base_speed(self.course) * factor)
        self.section_modifier.append(0.0)

        self._temptation_roll = self.rng.random()
        self._force_in_random = self.rng.random() * 0.1
        self.pos_keep_rng = Rule30CARng.seeded(self.rng.int32())
        self.compete_rng = Rule30CARng.seeded(self.rng.int32())
        self._stamina_keep_factor = 1.035 + 0.005 * self.rng.random()

        self.hp.init(self.horse)

        self.base_accel = [
            _base_accel(_UPHILL_BASE_ACCEL if i > 2 else _BASE_ACCEL, self.horse, p)
            for i, p in enumerate([0, 1, 2, 0, 1, 2])
        ]


    def refresh_derived_stats(self) -> None:
        self.min_speed = 0.85 * _base_speed(self.course) + math.sqrt(200.0 * self.horse.guts) * 0.001
        self.base_target_speed = [_base_target_speed(self.horse, self.course, p) for p in (0, 1, 2)]
        self.last_spurt_speed = _last_spurt_speed(self.horse, self.course)
        self.hp.init(self.horse)
        self.base_accel = [
            _base_accel(_UPHILL_BASE_ACCEL if i > 2 else _BASE_ACCEL, self.horse, p)
            for i, p in enumerate([0, 1, 2, 0, 1, 2])
        ]

    def _new_timer(self, t: float = 0) -> Timer:
        tm = Timer(t)
        self.timers.append(tm)
        return tm

    def _gate_adjustor(self) -> float:
        gate = self.gate_index + 1
        track = self.course.race_track_id
        if track == 10101:
            return 0.0
        if track == 10201:
            return 1.86 if gate >= 14 else 0.0
        return 0.6 if gate >= 10 else 0.0

    def _init_hills(self) -> None:
        self.n_hills = len(self.course.slopes)
        self.hill_start = [s.start for s in self.course.slopes][::-1]
        self.hill_end = [s.start + s.length for s in self.course.slopes][::-1]
        self.hill_idx = -1

        self.hill_rng = [Rule30CARng.seeded((self.rng.int32() << 32) | self.rng.int32()) for _ in self.course.slopes]
        self.downhill_timer = self._new_timer()

        if self.hill_start and self.hill_start[-1] == 0:
            self.hill_idx = 0
            self.slope_per = self.course.slopes[0].slope
            self.downhill_timer.t = 0
            self._downhill_check(self.hill_rng[0].random())
            self.hill_start.pop()
        else:
            self.slope_per = 0


    def get_max_speed(self) -> float:
        if self.start_dash:
            return min(self.target_speed, 0.85 * _base_speed(self.course))
        elif self.current_speed + self.modifiers_one_frame_accel > self.target_speed:
            return 9999.0
        else:
            return self.target_speed

    def step(self, dt: float) -> None:
        motion_fraction = 1.0
        if self.accumulatetime.t < self.start_delay:
            remaining = f32(f32(self.start_delay - f32(self.accumulatetime.t)) - f32(dt))
            if remaining > 0:
                for tm in self.timers:
                    tm.t += dt
                return
            if remaining < 0:
                motion_fraction = f32(abs(remaining) / f32(dt))

        if self.pos < self.pos_keep_end and self.pacer is not None:
            self.pacer.step(dt)

        was_start_dash = self.start_dash
        if was_start_dash:
            self._apply_forces()
            self.accel = f32(self.accel)
            halfv = min(f32(f32(self.current_speed) + f32(self.accel * f32(dt))), self.get_max_speed())
            self.current_speed = halfv
            displacement = f32(f32(halfv + self.modifiers_current_speed.acc) + self.modifiers_current_speed.err)
            move = f32(f32(displacement * f32(dt)) * f32(motion_fraction))
            self.pos = f32(f32(self.pos) + move)
        else:
            halfv = min(self.current_speed + 0.5 * dt * self.accel, self.get_max_speed())
            displacement = halfv + self.modifiers_current_speed.acc + self.modifiers_current_speed.err
            self.pos += displacement * dt * motion_fraction
        self.hp.tick(self, dt)
        for tm in self.timers:
            tm.t += dt
        self._update_hills()
        self._update_phase()
        self.process_skill_activations()
        self._update_overtake_effects()
        self._update_kakari()
        self._update_position_keep(dt)
        self._update_last_spurt_state()
        self._update_target_speed()
        self._apply_forces()
        if not was_start_dash:
            self.current_speed = min(halfv + 0.5 * dt * self.accel + self.modifiers_one_frame_accel, self.get_max_speed())
        if not self.start_dash and self.current_speed < self.min_speed:
            self.current_speed = self.min_speed
        elif self.start_dash and self.current_speed >= 0.85 * _base_speed(self.course):
            self.start_dash = False
            self.modifiers_accel.add(-24.0)
        self.modifiers_one_frame_accel = 0.0
        self._update_blocking(dt)
        self._update_lane(dt)
        self._update_near_lane_timers(dt)
        self._update_overtake_timers(dt)
        self._update_competition_mechanics(dt)

    def _update_blocking(self, dt: float) -> None:
        if self.field is None:
            self._front_blocker_index = -1
            self._blocked_front_timer = 0.0
            self._blocked_side_timer = 0.0
            self._blocked_all_timer = 0.0
            return
        blocker = self.field.front_blocker(self)
        if blocker is not None:
            other, dist_gap = blocker
            cap = (0.988 + 0.012 * (dist_gap / 2.0)) * other.current_speed
            self.current_speed = min(self.current_speed, cap)
            self._blocked_front_timer += dt
            self._front_blocker_index = other.field_index
        else:
            self._blocked_front_timer = 0.0
            self._front_blocker_index = -1
        inner_blocked, outer_blocked = self.field.side_room(self)
        side_blocked = inner_blocked or outer_blocked
        self._blocked_side_timer = self._blocked_side_timer + dt if side_blocked else 0.0
        if self.phase == 1:
            self._mid_side_block = self._mid_side_block + dt if side_blocked else 0.0
            self._mid_side_block_max = max(self._mid_side_block_max, self._mid_side_block)
        all_blocked = blocker is not None and side_blocked
        self._blocked_all_timer = self._blocked_all_timer + dt if all_blocked else 0.0


    def _move_lane_point(self) -> float:
        return self.course.corners[0].start if self.course.corners else self.course.distance

    @property
    def visible_distance(self):
        return max(0.0, _VISIBLE_DISTANCE + sum(mod for timer,mod in self.vision_modifiers if timer is None or timer.t < 0))

    def _is_visible(self, other) -> bool:
        dist_gap = other.pos - self.pos
        distance = self.visible_distance
        if not (0.0 <= dist_gap <= distance):
            return False
        half_width = ((dist_gap / max(distance, 0.001)) * 11.5 * HORSE_LANE + 2.0 * HORSE_LANE) / 2.0
        return abs(other.lane - self.lane) <= half_width

    def _overtake_targets(self) -> list:
        targets = []
        for other in self.field.solvers:
            if other is self or other.finished:
                continue
            dist_gap = other.pos - self.pos
            if not (1.0 <= dist_gap <= 20.0) or not self._is_visible(other):
                continue
            speed_gap = self.current_speed - other.current_speed
            if speed_gap <= 0.0 or dist_gap / speed_gap >= 15.0:
                continue
            if (other.target_speed < self.target_speed
                    or (other._front_blocker_index >= 0 and other.current_speed < self.target_speed)):
                targets.append(other)
        if self._front_blocker_index >= 0:
            blocker = self.field.solvers[self._front_blocker_index]
            if blocker not in targets:
                targets.append(blocker)
        return targets

    def _lane_clear(self, cand: float, dist_lo: float, dist_hi: float) -> bool:
        for other in self.field.solvers:
            if other is self or other.finished:
                continue
            if not (dist_lo <= other.pos <= dist_hi) or not self._is_visible(other):
                continue
            if cand - 0.8 * HORSE_LANE <= other.lane <= cand + 0.8 * HORSE_LANE:
                return False
        return True

    def _in_final_straight(self) -> bool:
        if not self.course.straights:
            return False
        last = self.course.straights[-1]
        return last.start <= self.pos < last.end

    def _update_overtake_timers(self, dt):
        targets = self._overtake_targets()
        self.overtake_target_timer = self.overtake_target_timer + dt if targets else 0.0
        self.overtake_no_order_up_timer = (self.overtake_no_order_up_timer + dt
            if targets and not self.field.did_overtake(self) else 0.0)

    def _update_lane_target(self) -> None:
        field = self.field
        inner_blocked, outer_blocked = field.side_room(self)
        if self.course.corners and self._extra_move_lane is None and self.pos >= self.course.corners[-1].start:
            self._extra_move_lane = min(1.0, max(0.0, self.lane / 0.1)) * 0.5 + self.rng.random() * 0.1

        moving_in = self.lane_target < self.lane
        blocked_moving = (inner_blocked if moving_in else outer_blocked) and abs(self.lane_target - self.lane) > 1e-9
        if not (self._lane_retarget or blocked_moving or abs(self.lane - self.lane_target) < 0.5 * HORSE_LANE):
            return
        self._lane_retarget = False

        targets = self._overtake_targets()
        lane_modifier = sum(mod for timer,mod in self.lane_modifiers if timer is None or timer.t < 0)
        if lane_modifier:
            self.lane_target = min(self.course.max_lane, max(0.0, self.lane + lane_modifier))
            return
        mlp = self._move_lane_point()
        forced_normal = self.phase <= 1 and mlp - 200.0 <= self.pos < mlp
        if not targets or forced_normal:
            self.lane_target = self._normal_mode_lane(inner_blocked, outer_blocked)
        else:
            self.lane_target = self._overtake_mode_lane(targets, inner_blocked, outer_blocked)
        self.lane_target = max(0.0, min(self.course.max_lane, self.lane_target))

    def _inside_neighbor(self):
        best = None
        for other in self.field.solvers:
            if other is self or other.finished or abs(other.pos - self.pos) >= 1.05:
                continue
            if other.lane < self.lane and (best is None or other.lane > best.lane):
                best = other
        return best

    def _normal_mode_lane(self, inner_blocked: bool, outer_blocked: bool) -> float:
        if not self.hp.has_remaining_hp():
            return self.lane
        if self.is_pace_down:
            return 0.18
        if (self._in_final_straight() and self._extra_move_lane is not None
                and self._extra_move_lane > self.lane and not outer_blocked):
            return self._extra_move_lane
        if self.phase <= 1 and not inner_blocked:
            return max(0.0, self.lane - 0.05)
        if self.phase == 1:
            inside = self._inside_neighbor()
            if inside is not None and self.lane - inside.lane < 1.75 * HORSE_LANE:
                return inside.lane + 2.0 * HORSE_LANE
        return self.lane

    def _overtake_mode_lane(self, targets: list, inner_blocked: bool, outer_blocked: bool) -> float:
        field = self.field
        candidates = []
        for t in targets:
            inner = outer = t
            for c in field.solvers:
                if c is self or c.finished:
                    continue
                if 0.0 <= c.pos - t.pos <= 3.0 and 0.0 < abs(c.lane - t.lane) < 2.0 * HORSE_LANE:
                    if c.lane < inner.lane:
                        inner = c
                    if c.lane > outer.lane:
                        outer = c
            for edge, cand in ((inner, inner.lane - HORSE_LANE), (outer, outer.lane + HORSE_LANE)):
                if 0.0 <= cand <= self.course.max_lane and self._lane_clear(cand, self.pos, edge.pos + 0.5):
                    candidates.append(cand)
        furthest = max(t.pos for t in targets)
        extra = self.lane - HORSE_LANE if (self.phase <= 1 and self.lane >= HORSE_LANE) else self.lane
        if self._lane_clear(extra, self.pos, furthest + 3.0):
            candidates.append(extra)
        in_coef, out_coef = ((1.0, 100.0) if self.phase == 0 else (1.0, 1.0) if self.phase == 1 else (1.0, 1.15))
        best = None
        best_score = 0.0
        for cand in candidates:
            if cand < self.lane and inner_blocked:
                continue
            if cand > self.lane and outer_blocked:
                continue
            score = abs(cand - self.lane) * (in_coef if cand < self.lane else out_coef)
            if best is None or score < best_score:
                best, best_score = cand, score
        if best is None:
            best = self.lane
        if self._extra_move_lane is not None and best < self._extra_move_lane and not outer_blocked:
            return self._extra_move_lane
        return best

    def _update_lane(self, dt: float) -> None:
        if self.field is None:
            return
        self._update_lane_target()
        lane_distance = abs(self.lane_target - self.lane)
        if lane_distance < 1e-9:
            self.lane_change_speed = 0.0
            self._moved_lane_this_tick = False
            return
        before_mlp = self.phase <= 1 and self.pos < self._move_lane_point()
        first_move_modifier = (1.0 + self.lane / max(self.course.max_lane, 1e-9) * 0.05) if before_mlp else 1.0
        order_modifier = 1.0 + self.field.order_of(self) * 0.01 if self.phase >= 2 else 1.0
        target_lane_change_speed = 0.02 * (0.3 + 0.001 * self.horse.power) * first_move_modifier * order_modifier
        if self.lane_change_speed < target_lane_change_speed:
            self.lane_change_speed = min(target_lane_change_speed, self.lane_change_speed + 0.03 * dt)
        else:
            self.lane_change_speed = max(target_lane_change_speed, self.lane_change_speed - 0.03 * dt)
        moving_in = self.lane_target < self.lane
        actual_speed = max(0.0, min(0.6, self.lane_change_speed + self.lane_speed_skill_modifier))
        if moving_in:
            actual_speed *= 1.0 + self.lane
        inner_blocked, outer_blocked = self.field.side_room(self)
        if (inner_blocked if moving_in else outer_blocked):
            self._moved_lane_this_tick = False
            return
        step = actual_speed * dt * (-1.0 if moving_in else 1.0)
        if abs(step) >= lane_distance:
            self.lane = self.lane_target
            self.lane_change_speed = 0.0
        else:
            self.lane += step
        self.lane = max(0.0, min(self.course.max_lane, self.lane))
        self._moved_lane_this_tick = abs(step) > 1e-9

    def _update_near_lane_timers(self, dt: float) -> None:
        if self.field is None:
            self._infront_near_lane_timer = 0.0
            self._behind_near_lane_timer = 0.0
            return
        ahead_gap = self.field.distance_to_ahead(self)
        ahead_lane_gap = self.field.lane_gap_to_ahead(self)
        near_ahead = (ahead_gap is not None and abs(ahead_gap) < 2.5
                      and abs(ahead_lane_gap) < self.field.HORSE_LANE)
        self._infront_near_lane_timer = self._infront_near_lane_timer + dt if near_ahead else 0.0

        behind_gap = self.field.distance_to_behind(self)
        behind_lane_gap = self.field.lane_gap_to_behind(self)
        near_behind = (behind_gap is not None and abs(behind_gap) < 2.5
                       and abs(behind_lane_gap) < self.field.HORSE_LANE)
        self._behind_near_lane_timer = self._behind_near_lane_timer + dt if near_behind else 0.0


    def _roll_temptation_mode(self) -> TemptationMode:
        s = self.horse.strategy
        r = self._temptation_roll
        if s in (Strategy.NIGE, Strategy.OONIGE):
            return TemptationMode.BOOST
        if s == Strategy.SENKOU:
            return TemptationMode.POSITION_NIGE
        if s == Strategy.SASI:
            return TemptationMode.POSITION_NIGE if r < 0.75 else TemptationMode.POSITION_SENKO
        if r < 0.7:
            return TemptationMode.POSITION_NIGE
        return TemptationMode.POSITION_SENKO if r < 0.9 else TemptationMode.POSITION_SASHI

    def _update_kakari(self) -> None:
        if self.temptation_count == 0 and self.pos >= self.kakari_start:
            self.is_kakari = True
            self.temptation_count = 1
            self.temptation_mode = self._roll_temptation_mode()
            self.kakari_timer.t = -self.kakari_duration
            self._kakari_started_at = self.accumulatetime.t
            self._exit_position_keep(cooldown=False)
            self.pos_keep_timer.t = 2.0
            self.on_skill_activate(self, 'kakari', Perspective.SELF)
        elif self.is_kakari and self.kakari_timer.t >= 0:
            self.is_kakari = False
            self.temptation_mode = TemptationMode.NULL
            self.kakari_log.append((self._kakari_started_at, self.accumulatetime.t))
            self.on_skill_deactivate(self, 'kakari', Perspective.SELF)


    def ai_strategy(self) -> int:
        mode = self.temptation_mode
        if self.is_kakari and mode in _TEMPTATION_STRATEGY:
            return _TEMPTATION_STRATEGY[mode]
        return self.horse.strategy

    def _pos_keep_wit_check(self, coef: float) -> bool:
        if self.is_kakari:
            return True
        pct = coef * math.log10(max(self.horse.wisdom, 1) * 0.1)
        return self.pos_keep_rng.random() * 100.0 < pct

    def _exit_position_keep(self, cooldown: bool = True) -> None:
        self._pos_keep_mode = None
        self.is_pace_down = False
        self.pos_keep_speed_coef = 1.0
        if cooldown:
            self.pos_keep_timer.t = -1.0

    def _enter_position_keep(self, mode: str, coef: float) -> None:
        self._pos_keep_mode = mode
        self.pos_keep_speed_coef = coef
        self.is_pace_down = mode == 'pace_down'
        self.pos_keep_effect_start = self.pos

    def _speed_up_threshold(self, strategy: int) -> float:
        if self.field.is_solo_front_runner(self):
            return 12.5
        return 17.5 if strategy == Strategy.OONIGE else 4.5

    def _lead_in_group(self, strategy: int) -> Optional[float]:
        best_other = None
        for other in self.field.solvers:
            if other is self or not strategy_matches(other.ai_strategy(), strategy):
                continue
            if other.pos > self.pos:
                return None
            if best_other is None or other.pos > best_other:
                best_other = other.pos
        return float("inf") if best_other is None else self.pos - best_other

    def _pace_up_ex_wanted(self, strategy: int) -> bool:
        mine = _FORWARDNESS[strategy]
        if strategy_matches(strategy, Strategy.NIGE):
            return any(o is not self and not o.finished and o.pos > self.pos
                       and _FORWARDNESS[o.ai_strategy()] > mine for o in self.field.solvers)
        pm = self.field.pacemaker()
        return pm is not None and pm is not self and _FORWARDNESS[pm.ai_strategy()] > mine

    def _update_position_keep(self, dt: float) -> None:
        if self.pos >= self.pos_keep_end or self.field is None:
            if self._pos_keep_mode is not None:
                self._exit_position_keep(cooldown=False)
            return
        strategy = self.ai_strategy()
        front_runner = strategy_matches(strategy, Strategy.NIGE)
        mode = self._pos_keep_mode

        if mode is not None:
            if self.pos - self.pos_keep_effect_start >= self.pos_keep_continue_distance:
                self._exit_position_keep()
                return
            if mode == 'pace_up_ex':
                if not self._pace_up_ex_wanted(strategy):
                    self._exit_position_keep()
            elif mode == 'speed_up':
                gap = self.field.distance_to_behind(self) if self.field.order_of(self) == 1 else None
                if gap is None or gap >= self._speed_up_threshold(strategy):
                    self._exit_position_keep()
            elif mode == 'overtake':
                lead = self._lead_in_group(strategy)
                if lead is not None and lead >= (27.5 if strategy == Strategy.OONIGE else 10.0):
                    self._exit_position_keep()
            elif mode == 'pace_up':
                if self.field.gap_to_pacemaker(self) < self.pos_keep_effect_exit_distance:
                    self._exit_position_keep()
            elif mode == 'pace_down':
                speed_skill = bool(self.active_target_speed_skills or self.active_current_speed_skills)
                if self.field.gap_to_pacemaker(self) > self.pos_keep_effect_exit_distance or speed_skill:
                    self._exit_position_keep()
            return

        if self.pos_keep_timer.t < 2.0:
            return
        self.pos_keep_timer.t = 0.0

        if self._pace_up_ex_wanted(strategy):
            self._enter_position_keep('pace_up_ex', 2.0)
            return
        if front_runner:
            if self.is_kakari and self.temptation_mode == TemptationMode.BOOST:
                self._enter_position_keep('speed_up', 1.04)
                return
            if self.field.order_of(self) == 1:
                gap = self.field.distance_to_behind(self)
                if gap is not None and gap < self._speed_up_threshold(strategy) and self._pos_keep_wit_check(20.0):
                    self._enter_position_keep('speed_up', 1.04)
            elif self._lead_in_group(strategy) is None and self._pos_keep_wit_check(20.0):
                self._enter_position_keep('overtake', 1.05)
            return

        pm = self.field.pacemaker()
        if pm is None or pm is self:
            return
        gap = pm.pos - self.pos
        lo = _PositionKeep.min_threshold(strategy, self.course.distance)
        hi = _PositionKeep.max_threshold(strategy, self.course.distance)
        if gap > hi:
            if self._pos_keep_wit_check(15.0):
                self._enter_position_keep('pace_up', 1.04)
                self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo)
        elif gap < lo and not self.is_kakari and not (self.active_target_speed_skills or self.active_current_speed_skills):
            self._enter_position_keep('pace_down', 0.945 if self.phase == 1 else 0.915)
            exit_hi = lo + 0.5 * (hi - lo) if self.phase == 1 else hi
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (exit_hi - lo)

    def _update_last_spurt_state(self) -> None:
        if self.is_last_spurt or self.phase < 2:
            return
        if self.last_spurt_transition == -1:
            transition, speed = self.hp.get_last_spurt_pair(self, self.last_spurt_speed, self.base_target_speed[2])
            self.last_spurt_transition = transition
            self.last_spurt_speed = speed
        if self.pos >= self.last_spurt_transition:
            self.is_last_spurt = True
            self.last_spurt_start_pos = self.pos

    def _update_target_speed(self) -> None:
        if not self.hp.has_remaining_hp():
            self.target_speed = self.min_speed
        elif self.is_last_spurt:
            self.target_speed = self.last_spurt_speed
        else:
            self.target_speed = self.base_target_speed[self.phase] * self.pos_keep_speed_coef
            self.target_speed += self.section_modifier[int(self.pos // self.section_length)]
        self.target_speed += self.modifiers_target_speed.acc + self.modifiers_target_speed.err

        if self.phase == 0 and self.field is not None and self.lane > 0.12:
            inner_blocked, _ = self.field.side_room(self)
            if not inner_blocked:
                self.target_speed += self._force_in_random + _FORCE_IN_STRATEGY[self.horse.strategy]

        if self.is_downhill_mode:
            self.target_speed += 0.3 + self.slope_per / 100000.0
        elif self.hill_idx != -1 and self.slope_per > 0:
            self.target_speed -= self.slope_per / 10000.0 * 200.0 / self.horse.power
            self.target_speed = max(self.target_speed, self.min_speed)

        if self.active_lane_speed_skills and self._moved_lane_this_tick:
            self.target_speed += math.sqrt(0.0002 * self.horse.power)

        self.target_speed = min(self.target_speed, 30.0)

    def _apply_forces(self) -> None:
        if not self.hp.has_remaining_hp():
            self.accel = -1.2
            return
        if self.current_speed > self.target_speed:
            self.accel = -0.5 if self.is_pace_down else _PHASE_DECELERATION[self.phase]
            return
        self.accel = self.base_accel[int(self.slope_per > 0) * 3 + self.phase]
        self.accel += self.modifiers_accel.acc + self.modifiers_accel.err

    def _downhill_check(self, roll: float) -> None:
        if self.slope_per < 0 and roll < self.horse.wisdom * 0.0004:
            self.on_skill_activate(self, 'downhill', Perspective.SELF)
            self.is_downhill_mode = True

    def _update_hills(self) -> None:
        if self.hill_idx == -1 and self.hill_start and self.pos >= self.hill_start[-1]:
            self.hill_idx = self.n_hills - len(self.hill_start)
            self.slope_per = self.course.slopes[self.hill_idx].slope
            self.downhill_timer.t = 0
            self._downhill_check(self.hill_rng[self.hill_idx].random())
            self.hill_start.pop()
        elif self.hill_idx != -1 and self.hill_end and self.pos > self.hill_end[-1]:
            self.hill_idx = -1
            self.slope_per = 0
            self.hill_end.pop()
            if self.is_downhill_mode:
                self.on_skill_deactivate(self, 'downhill', Perspective.SELF)
            self.is_downhill_mode = False
        if self.downhill_timer.t >= 1.0 and self.hill_idx != -1:
            roll = self.hill_rng[self.hill_idx].random()
            if self.is_downhill_mode and roll > 0.8:
                self.on_skill_deactivate(self, 'downhill', Perspective.SELF)
                self.is_downhill_mode = False
            elif not self.is_downhill_mode:
                self._downhill_check(roll)
            self.downhill_timer.t = 0.0

    def _update_phase(self) -> None:
        if self.pos >= self.next_phase_transition and self.phase < 2:
            self.phase += 1
            self.next_phase_transition = phase_start(self.course.distance, self.phase + 1)


    def process_skill_activations(self) -> None:
        for i in range(len(self.active_target_speed_skills) - 1, -1, -1):
            s = self.active_target_speed_skills[i]
            if s.duration_timer.t >= 0:
                del self.active_target_speed_skills[i]
                self.modifiers_target_speed.add(-s.modifier)
                self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for i in range(len(self.active_current_speed_skills) - 1, -1, -1):
            s = self.active_current_speed_skills[i]
            if s.duration_timer.t >= 0:
                del self.active_current_speed_skills[i]
                self.modifiers_current_speed.add(-s.modifier)
                if s.natural_deceleration:
                    self.modifiers_one_frame_accel += s.modifier
                self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for i in range(len(self.active_accel_skills) - 1, -1, -1):
            s = self.active_accel_skills[i]
            if s.duration_timer.t >= 0:
                del self.active_accel_skills[i]
                self.modifiers_accel.add(-s.modifier)
                self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for i in range(len(self.active_lane_speed_skills) - 1, -1, -1):
            s = self.active_lane_speed_skills[i]
            if s.duration_timer.t >= 0:
                del self.active_lane_speed_skills[i]
                self.lane_speed_skill_modifier -= s.modifier
                self.on_skill_deactivate(self, s.skill_id, s.perspective)

        for i in range(len(self._watchers) - 1, -1, -1):
            if self._watchers[i][5].t >= 0:
                del self._watchers[i]
        for i in range(len(self._order_up_groups) - 1, -1, -1):
            if all(tm.t >= 0 for tm in self._order_up_groups[i][0]):
                del self._order_up_groups[i]

        if self._cooling:
            still = []
            for ready, sk in self._cooling:
                if self.accumulatetime.t >= ready:
                    again = sk.rearmed(self.pos)
                    if again is not None:
                        self.pending_skills.append(again)
                else:
                    still.append((ready, sk))
            self._cooling = still

        activate_count_this_frame = 0
        for i in range(len(self.pending_skills) - 1, -1, -1):
            if i >= len(self.pending_skills):
                continue
            s = self.pending_skills[i]
            if self.pos >= s.trigger.end or s.skill_id in self.pending_removal:
                del self.pending_skills[i]
                self.pending_removal.discard(s.skill_id)
            elif (self.pos >= s.trigger.start or getattr(s.extra_condition, "observe_precondition", False)) and s.extra_condition(self) and self.pos >= s.trigger.start:
                self.activate_skill(s)
                del self.pending_skills[i]
                if s.cooldown > 0:
                    self._cooling.append((self.accumulatetime.t + s.cooldown, s))
                if s.skill_id != 'asitame' and s.skill_id != 'staminasyoubu':
                    activate_count_this_frame += 1
        self.activate_count_last_frame = activate_count_this_frame

    def _dynamic_value_multiplier(self, ef: SkillEffect) -> float:
        vu = ef.value_usage
        if vu == 20:
            return _bucket(self._mid_side_block_max, _BLOCKED_SIDE_MULT)
        if vu == 11:
            return _bucket(self._corner_late_order_up, _CORNER_LATE_ORDER_UP_MULT)
        if vu == 14:
            return _bucket(self._green_activations, _GREEN_COUNT_MULT)
        if vu == 22:
            return _bucket(self.horse.speed, _SPEED1_MULT)
        if vu == 23:
            return _bucket(self.horse.speed, _SPEED2_MULT)
        return 1.0

    def _in_corner(self) -> bool:
        return any(c.start <= self.pos < c.start + c.length for c in self.course.corners)

    def _update_overtake_effects(self) -> None:
        if self.field is None:
            return
        gained = self.field.previous_order_of(self) - self.field.order_of(self)
        if gained <= 0:
            return
        if self.phase >= 2 and self._in_corner():
            self._corner_late_order_up += gained
        for _ in range(gained):
            for w in self._watchers:
                if w[3] == 1 and w[4] > 0 and w[5].t < 0:
                    w[4] -= 1
                    self.apply_effect(w[0], -w[5].t, w[1], w[2])
            extend = 1.0 * self.course.distance / 1000.0
            for g in self._order_up_groups:
                if g[1] > 0:
                    g[1] -= 1
                    for tm in g[0]:
                        if tm.t < 0:
                            tm.t -= extend

    def _scaled_duration(self, ef: SkillEffect, rarity: int) -> float:
        base = ef.base_duration
        if ef.time_usage == 2 and self.field is not None:
            base *= min(0.8 + self.field.distance_to_leader(self) / 62.5, 1.6)
        elif ef.time_usage in (3, 7):
            hp = getattr(self.hp, "hp", 0.0)
            steps = ((3500, 4.0), (3200, 3.5), (3000, 3.0), (2800, 2.5), (2600, 2.2), (2400, 2.0),
                     (2000, 1.5)) if ef.time_usage == 3 else \
                ((2100, 3.0), (2000, 2.5), (1800, 2.0), (1500, 1.5))
            for bound, mult in steps:
                if hp >= bound:
                    base *= mult
                    break
        elif ef.time_usage == 5:
            base *= _bucket(self._mid_side_block_max, _BLOCKED_SIDE_MULT)
        return base * (self.course.distance / 1000) * (
            self.modifiers_special_skill_duration_scaling if rarity == SkillRarity.EVOLUTION else 1)

    def activate_skill(self, s: PendingSkill) -> None:
        effects = sorted(s.effects, key=lambda ef: 1 if ef.type == SkillType.EXTEND_EVOLVED_DURATION else 0)
        targets: list[int] = []
        timed_duration = None
        special = s.skill_id in ('asitame', 'staminasyoubu')
        self._activation_group += 1
        group = self._activation_group
        if not special:
            for w in self._watchers:
                if w[3] in (2, 3) and w[4] > 0 and w[5].t < 0 and w[6] != group:
                    w[4] -= 1
                    self.apply_effect(w[0], -w[5].t, w[1], w[2])
        group_timers = []
        advantage = set()
        for ef in effects:
            mult = self._dynamic_value_multiplier(ef)
            if mult != 1.0:
                ef = ef.scaled(mult)
            scaled_duration = self._scaled_duration(ef, s.rarity)
            if ef.type in _TIMED_TYPES:
                timed_duration = max(timed_duration or 0.0, scaled_duration)
            if ef.additional_activate:
                tm = self._new_timer(-scaled_duration)
                group_timers.append(tm)
                self._watchers.append([ef, s.skill_id, s.perspective, ef.additional_activate,
                                       _ADDITIONAL_ACTIVATE_LIMIT.get(ef.additional_activate, 0), tm, group])
                continue
            if ef.target != SkillTarget.SELF and not special:
                if self.field is not None:
                    for t in self.field.queue_external_effect(self, ef, scaled_duration, s.skill_id):
                        if t not in targets:
                            targets.append(t)
                else:
                    self.deferred_external.append((ef, scaled_duration, s.skill_id, targets))
                continue
            if ef.modifier > 0 and ef.type != SkillType.NOOP:
                advantage.add(int(ef.type))
            tm = self.apply_effect(ef, scaled_duration, s.skill_id, s.perspective)
            if tm is not None:
                group_timers.append(tm)
        if group_timers and any(ef.time_usage == 4 for ef in effects):
            self._order_up_groups.append([group_timers, 3])
        if advantage and not special:
            self.advantage_types |= advantage
        if getattr(s, "green", False):
            self._green_activations += 1

        if s.perspective == Perspective.SELF:
            self.activate_count[self.phase] += 1
            if self.pos >= self.course.distance / 2:
                self.activate_count_later_half += 1
        self.used_skills.add(s.skill_id)
        if s.skill_id == 'asitame':
            self.event_log.append((self.accumulatetime.t, RaceEventType.RELEASE_CONSERVE_POWER))
        elif s.skill_id != 'staminasyoubu':
            self.activation_log.append((self.accumulatetime.t, s.skill_id, effects, targets, timed_duration))
        self.on_skill_activate(self, s.skill_id, s.perspective)

    def apply_effect(self, ef: SkillEffect, scaled_duration: float, skill_id: str, perspective):
        t = ef.type
        if t == SkillType.NOOP:
            pass
        elif t == SkillType.OONIGE:
            if self.horse.strategy == Strategy.NIGE:
                self.horse.strategy = Strategy.OONIGE
                self.pos_keep_continue_distance = math.floor(self.section_length) * 3
        elif t == SkillType.VISIBLE_DISTANCE:
            timer = self._new_timer(-scaled_duration) if scaled_duration > 0 else None
            self.vision_modifiers.append((timer, ef.modifier))
            return timer
        elif t == SkillType.TARGET_LANE:
            timer = self._new_timer(-scaled_duration) if scaled_duration > 0 else None
            self.lane_modifiers.append((timer, ef.modifier))
            self._lane_retarget = True
            return timer
        elif t == SkillType.DEBUFF_CANCEL:
            self.debuff_cancel = True
        elif t == SkillType.SPEED_UP:
            self.horse.speed = max(self.horse.speed + ef.modifier, 1)
        elif t == SkillType.STAMINA_UP:
            self.horse.stamina = max(self.horse.stamina + ef.modifier, 1)
            self.horse.raw_stamina = max(self.horse.raw_stamina + ef.modifier, 1)
        elif t == SkillType.POWER_UP:
            self.horse.power = max(self.horse.power + ef.modifier, 1)
        elif t == SkillType.GUTS_UP:
            self.horse.guts = max(self.horse.guts + ef.modifier, 1)
        elif t == SkillType.WISDOM_UP:
            self.horse.wisdom = max(self.horse.wisdom + ef.modifier, 1)
        elif t == SkillType.ALL_STATUS_UP:
            self.horse.speed = max(self.horse.speed + ef.modifier, 1)
            self.horse.stamina = max(self.horse.stamina + ef.modifier, 1)
            self.horse.raw_stamina = max(self.horse.raw_stamina + ef.modifier, 1)
            self.horse.power = max(self.horse.power + ef.modifier, 1)
            self.horse.guts = max(self.horse.guts + ef.modifier, 1)
            self.horse.wisdom = max(self.horse.wisdom + ef.modifier, 1)
        elif t == SkillType.MULTIPLY_START_DELAY:
            self.start_delay *= ef.modifier
        elif t == SkillType.EXTEND_KAKARI:
            if self.is_kakari:
                self.kakari_timer.t -= ef.modifier
        elif t == SkillType.SET_START_DELAY:
            self.start_delay = ef.modifier
        elif t == SkillType.TARGET_SPEED:
            self.modifiers_target_speed.add(ef.modifier)
            tm = self._new_timer(-scaled_duration)
            self.active_target_speed_skills.append(_ActiveSkill(
                skill_id, perspective, tm, ef.modifier))
            return tm
        elif t == SkillType.MODIFY_KAKARI_CHANCE:
            self.modifiers_kakari_chance += ef.modifier / 100.0
        elif t == SkillType.ACCEL:
            self.modifiers_accel.add(ef.modifier)
            tm = self._new_timer(-scaled_duration)
            self.active_accel_skills.append(_ActiveSkill(
                skill_id, perspective, tm, ef.modifier))
            return tm
        elif t in (SkillType.CURRENT_SPEED, SkillType.CURRENT_SPEED_WITH_NATURAL_DECELERATION):
            self.modifiers_current_speed.add(ef.modifier)
            tm = self._new_timer(-scaled_duration)
            self.active_current_speed_skills.append(_ActiveSkill(
                skill_id, perspective, tm, ef.modifier,
                natural_deceleration=(t == SkillType.CURRENT_SPEED_WITH_NATURAL_DECELERATION)))
            return tm
        elif t == SkillType.RECOVERY:
            self.hp.recover(ef.modifier)
            if ef.modifier > 0:
                if perspective == Perspective.SELF:
                    self.activate_count_heal += 1
                self._stamina_keep_active = False
                if self.phase >= 2 and not self.is_last_spurt:
                    self.last_spurt_transition = -1
                    self._update_last_spurt_state()
        elif t == SkillType.LANE_MOVE_SPEED_UP:
            self.lane_speed_skill_modifier += ef.modifier
            tm = self._new_timer(-scaled_duration)
            self.active_lane_speed_skills.append(_ActiveSkill(
                skill_id, perspective, tm, ef.modifier))
            return tm
        elif t == SkillType.ACTIVATE_RANDOM_GOLD:
            self._do_activate_random_gold(ef.modifier)
        elif t == SkillType.EXTEND_EVOLVED_DURATION:
            self.modifiers_special_skill_duration_scaling = ef.modifier
        return None

    def _do_activate_random_gold(self, ngolds: float) -> None:
        gold_indices = [
            i for i, skill in enumerate(self.pending_skills)
            if skill.rarity in (SkillRarity.GOLD, SkillRarity.EVOLUTION)
            and all(ef.type > SkillType.WISDOM_UP and ef.type != SkillType.ALL_STATUS_UP
                    for ef in skill.effects)
        ]
        for i in range(len(gold_indices) - 1, -1, -1):
            j = self.gorosi_rng.uniform(i + 1)
            gold_indices[i], gold_indices[j] = gold_indices[j], gold_indices[i]
        for i in range(min(int(ngolds), len(gold_indices))):
            s = self.pending_skills[gold_indices[i]]
            self.activate_skill(s)
            self.pending_removal.add(s.skill_id)


    def _update_competition_mechanics(self, dt: float) -> None:
        if self.field is None:
            return
        self._update_stamina_keep()
        self._update_compete_top_exit()
        self._update_compete_before_spurt()
        self._update_secure_lead()
        self._update_dueling(dt)

    def _update_stamina_keep(self) -> None:
        if self._stamina_keep_active or self._stamina_keep_check.t < 2.0 or self.is_last_spurt:
            return
        self._stamina_keep_check.t = 0.0
        if not hasattr(self.hp, "hp_needed_to_finish"):
            return
        need = self.hp.hp_needed_to_finish(self.pos, self.base_target_speed[1], self.last_spurt_speed)
        if self.hp.hp >= need * self._stamina_keep_factor:
            return
        wisdom = max(self.horse.wisdom, 1)
        pct = 30.0 * (wisdom / 1000.0 + math.pow(wisdom, 0.03))
        if self.compete_rng.random() * 100.0 < pct:
            self._stamina_keep_active = True

    def start_compete_top(self) -> None:
        guts = max(self.horse.guts, 1)
        strat_prof = _STRATEGY_PROFICIENCY_MODIFIER_APPROX[self.horse.strategy_aptitude]
        self._compete_top_modifier = math.pow(500.0 * guts, 0.6) * 0.0001
        self._compete_top_timer.t = -math.pow(700.0 * guts, 0.5) * 0.012 * strat_prof
        self.compete_top_active = True
        self.modifiers_target_speed.add(self._compete_top_modifier)
        self.event_log.append((self.accumulatetime.t, RaceEventType.COMPETE_TOP))

    @property
    def compete_top_hp_coef(self) -> float:
        if not self.compete_top_active:
            return 0.0
        if self.horse.strategy == Strategy.OONIGE:
            return 7.7 if self.is_kakari else 3.5
        return 3.6 if self.is_kakari else 1.4

    def end_compete_top(self) -> None:
        if self.compete_top_active:
            self.compete_top_active = False
            self.modifiers_target_speed.add(-self._compete_top_modifier)

    def _update_compete_top_exit(self) -> None:
        if self.compete_top_active and (self._compete_top_timer.t >= 0 or self.pos >= self.section_length * 8.0):
            self.end_compete_top()

    def _update_compete_before_spurt(self) -> None:
        if self._compete_spurt_active:
            if self._compete_spurt_timer.t >= 0:
                self._compete_spurt_active = False
                self._compete_spurt_check.t = -1.0
                self.modifiers_target_speed.add(-self._compete_spurt_modifier)
            return
        if not (self.section_length * 10.0 <= self.pos < self.section_length * 15.0):
            return
        if self._compete_spurt_check.t < 2.0:
            return
        self._compete_spurt_check.t = 0.0
        if self._stamina_keep_active:
            return
        strategy = self.horse.strategy
        far_from_lead = self.field.distance_to_leader(self) > _COMPETE_DIST_THRESHOLD[strategy]
        near = self.field.near_count(self) > 0
        if not (far_from_lead or near):
            return
        pct = 15.0 * math.log10(max(self.horse.wisdom, 1) * 0.1)
        if not self.compete_rng.random() * 100.0 < pct:
            return
        solo_bonus = 1.0
        if strategy in (Strategy.NIGE, Strategy.OONIGE) and self.field.is_solo_front_runner(self):
            solo_bonus = 2.0 if strategy == Strategy.OONIGE else 1.1
        modifier = ((math.pow(max(self.horse.power, 0) / 1500.0, 0.5) * 2.0
                     + math.pow(max(self.horse.guts, 0) / 3000.0, 0.2)) * 0.1
                    * _COMPETE_STRATEGY_COEF[strategy] * solo_bonus)
        near_factor = 0.5 if near and not far_from_lead else 0.0
        stamina_cost = 20.0 * (_COMPETE_STAMINA_STRATEGY_COEF[strategy]
                               * _competition_distance_coefficient(self.course.distance) + near_factor)
        self._compete_spurt_active = True
        self._compete_spurt_modifier = modifier
        self._compete_spurt_timer.t = -2.0
        self.modifiers_target_speed.add(modifier)
        if hasattr(self.hp, "hp"):
            self.hp.hp -= stamina_cost

    def _update_secure_lead(self) -> None:
        if self._secure_lead_active:
            if self._secure_lead_timer.t >= 0:
                self._secure_lead_active = False
                self._secure_lead_check.t = -1.0
                self.modifiers_target_speed.add(-self._secure_lead_modifier)
            return
        if not (self.section_length * 10.0 <= self.pos < self.section_length * 15.0):
            return
        if self._secure_lead_check.t < 2.0:
            return
        self._secure_lead_check.t = 0.0
        strategy = self.horse.strategy
        coefs = _SECURE_LEAD_COEF.get(strategy)
        if self._stamina_keep_active or not coefs:
            return
        threatened = False
        for other in self.field.solvers:
            coef = coefs.get(other.horse.strategy)
            if other is self or coef is None or other.pos > self.pos:
                continue
            if self.pos - other.pos < coef + 0.0003 * (self.course.distance + 1000):
                threatened = True
                break
        if not threatened or self.compete_rng.random() >= 0.20:
            return
        solo_bonus = 1.0
        if strategy in (Strategy.NIGE, Strategy.OONIGE) and self.field.is_solo_front_runner(self):
            solo_bonus = 7.0 if strategy == Strategy.OONIGE else 2.0
        modifier = math.pow(max(self.horse.guts, 0) / 2000.0, 0.5) * 0.3 * _SECURE_LEAD_STRATEGY_COEF[strategy] * solo_bonus
        stamina_cost = (20.0 * _SECURE_LEAD_STAMINA_STRATEGY_COEF[strategy]
                        * _competition_distance_coefficient(self.course.distance))
        self._secure_lead_active = True
        self._secure_lead_modifier = modifier
        self._secure_lead_timer.t = -2.0
        self.modifiers_target_speed.add(modifier)
        if hasattr(self.hp, "hp"):
            self.hp.hp -= stamina_cost

    def _update_dueling(self, dt: float) -> None:
        if not self.course.straights:
            return
        in_final_straight = self._in_final_straight()
        if self._duel_active:
            if self.hp.hp_ratio_remaining() < 0.05:
                self._duel_active = False
                self.modifiers_target_speed.add(-self._duel_target_speed_modifier)
                self.modifiers_accel.add(-self._duel_accel_modifier)
            return
        if not in_final_straight or self.hp.hp_ratio_remaining() < 0.15:
            self._duel_continuous_timer = 0.0
            self._duel_target = -1
            return
        target = None
        for other in self.field.solvers:
            if other is self or other.finished:
                continue
            if abs(other.pos - self.pos) < 3.0 and abs(other.lane - self.lane) < 0.25:
                if target is None or other.field_index == self._duel_target:
                    target = other
                    if other.field_index == self._duel_target:
                        break
        if target is None:
            self._duel_continuous_timer = 0.0
            self._duel_target = -1
            return
        if target.field_index != self._duel_target:
            self._duel_target = target.field_index
            self._duel_continuous_timer = 0.0
        self._duel_continuous_timer += dt
        if self._duel_continuous_timer < 2.0:
            return
        top_half = self.field.order_of(self) <= max(1, self.field.n // 2)
        if not (top_half and abs(target.current_speed - self.current_speed) < 0.6):
            return
        guts = max(self.horse.guts, 1)
        self._duel_target_speed_modifier = math.pow(200.0 * guts, 0.708) * 0.0001
        self._duel_accel_modifier = math.pow(160.0 * guts, 0.59) * 0.0001
        self._duel_active = True
        self.modifiers_target_speed.add(self._duel_target_speed_modifier)
        self.modifiers_accel.add(self._duel_accel_modifier)
        self.event_log.append((self.accumulatetime.t, RaceEventType.COMPETE_FIGHT))

    def cleanup(self) -> None:
        for s in self.active_target_speed_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for s in self.active_current_speed_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for s in self.active_accel_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        if self.is_downhill_mode:
            self.on_skill_deactivate(self, 'downhill', Perspective.SELF)
