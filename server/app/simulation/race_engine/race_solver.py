"""Port of RaceSolver.ts -- the core per-tick physics/skill-activation loop.

Uses the same velocity-Verlet-style integration, the same three
compensated-sum (Kahan-Babuska-Neumaier) accumulators for skill modifiers,
and precomputes `base_target_speed`/`base_accel` arrays exactly once at
construction time (a subtlety worth flagging: green/stat-boost skills that
fire mid-race mutate `self.horse` but do NOT retroactively change these
precomputed arrays, matching the original engine's behavior bit-for-bit).
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Callable, Optional

from .course_data import CourseData, phase_start
from .horse_types import HorseParameters, Strategy, strategy_matches
from .random_gen import Rule30CARng
from .region import Region

# ---------------------------------------------------------------------------
# Speed / acceleration tables (Object.freeze([...]) in RaceSolver.ts)

# BUG FIXED 2026-08-29: two wrong values found cross-checking the Global
# wiki's Strategy Phase Coefficient table for Target Speed. Sashi (Late
# Surger, row 3) opening-leg was 0.938, should be 0.93. Oikomi (End Closer,
# row 4) final-leg/last-spurt was 1.0, should be 1.02 -- a real 2% deficit
# specifically in the phase that determines a closer's finishing kick.
_SPEED_STRATEGY_PHASE_COEFFICIENT = [
    [],  # strategies start numbered at 1
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


# BUG FIXED 2026-08-29: Oikomi (End Closer, row 4) final-leg/last-spurt
# acceleration was 0.997 per the wiki's Strategy Phase Coefficient table for
# Base Acceleration, should be 0.967 -- compounds the Target Speed fix just
# above (same phase, same strategy): a closer both targets a lower spurt
# speed AND accelerates into it more slowly than the real game.
_ACCEL_STRATEGY_PHASE_COEFFICIENT = [
    [],
    [1.0, 1.0, 0.996],
    [0.985, 1.0, 0.996],
    [0.975, 1.0, 1.0],
    [0.945, 1.0, 0.967],
    [1.17, 0.94, 0.956],
]
_ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER = [1.05, 1.0, 0.9, 0.8, 0.7, 0.5, 0.3, 0.1]
_ACCEL_DISTANCE_PROFICIENCY_MODIFIER = [1.0, 1.0, 1.0, 1.0, 1.0, 0.6, 0.5, 0.4]

_BASE_ACCEL = 0.0006
_UPHILL_BASE_ACCEL = 0.0004


def _base_accel(base_accel_const: float, horse: HorseParameters, phase: int) -> float:
    return (base_accel_const * math.sqrt(500.0 * horse.power)
            * _ACCEL_STRATEGY_PHASE_COEFFICIENT[horse.strategy][phase]
            * _ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER[horse.surface_aptitude]
            * _ACCEL_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude])


_PHASE_DECELERATION = [-1.2, -0.8, -1.0]

# -- competition-mechanics constant tables (doc, see _update_* methods) -----
# Strategy Proficiency Modifier (S..G), same table used for Wiz scaling
# elsewhere in the pipeline -- reused here for Spot Struggle's duration term.
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
# Secure Lead's real DesirableLead formula uses a full per-strategy-pair
# coefficient matrix the doc summary this port was built from only captured
# a few example entries of (Oonige-vs-Nige=2.0, Oonige-vs-Sashi=8.0,
# Nige-vs-Senkou=4.0, ...) -- approximated with one flat coefficient instead
# of reconstructing a guessed-at full matrix.
_SECURE_LEAD_COEF_APPROX = 3.0
_SECURE_LEAD_STRATEGY_COEF = {Strategy.OONIGE: 0.2, Strategy.NIGE: 1.0, Strategy.SENKOU: 1.0, Strategy.SASI: 0.8}
_SECURE_LEAD_STAMINA_STRATEGY_COEF = {Strategy.OONIGE: 1.2, Strategy.NIGE: 1.0, Strategy.SENKOU: 0.8, Strategy.SASI: 0.8}


def _competition_distance_coefficient(distance: float) -> float:
    """Shared course-length coefficient table (doc: identical brackets used
    by both Compete Before Spurt and Secure Lead's stamina-cost formulas)."""
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
    # JS's arrays only have indices 0-4 (Oonige=5 reads as `undefined`,
    # silently NaN-poisoning threshold math that is never actually read for
    # Oonige/Nige horses since updatePositionKeep is a noop for them). Pad
    # with NaN at index 5 so Python indexing doesn't crash where JS wouldn't.
    BASE_MIN_THRESHOLD = [0, 0, 3.0, 6.5, 7.5, float("nan")]
    BASE_MAX_THRESHOLD = [0, 0, 5.0, 7.0, 8.0, float("nan")]

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


class Timer:
    __slots__ = ("t",)

    def __init__(self, t: float = 0):
        self.t = t


class CompensatedAccumulator:
    """Kahan-Babuska-Neumaier sum: for any sequence, adding a value in any
    order interleaved with subtracting it in any other order results in
    acc+err == 0.0 (needed since skills add/remove modifiers at arbitrary
    times over a long race)."""

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


class SkillRarity(IntEnum):
    WHITE = 1
    GOLD = 2
    UNIQUE = 3
    EVOLUTION = 6


class SkillEffect:
    __slots__ = ("type", "base_duration", "modifier")

    def __init__(self, type_: int, base_duration: float, modifier: float):
        self.type = type_
        self.base_duration = base_duration
        self.modifier = modifier


class PendingSkill:
    __slots__ = ("skill_id", "perspective", "rarity", "trigger", "extra_condition", "effects")

    def __init__(self, skill_id: str, perspective: Optional[int], rarity: int, trigger: Region,
                 extra_condition: Callable, effects: list[SkillEffect]):
        self.skill_id = skill_id
        self.perspective = perspective
        self.rarity = rarity
        self.trigger = trigger
        self.extra_condition = extra_condition
        self.effects = effects


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
        # clone since green skills may modify the stat values
        self.horse = horse.clone()
        self.course = course
        self.hp = hp
        self.pacer = pacer
        self.rng = rng
        self.gate_index = gate_index
        # Set by race_field.RaceField once the whole roster is built (not
        # part of the original TS engine, which has no multi-horse
        # awareness). None during the very first ("gate skill") activation
        # round below, since no other horse exists yet at that point --
        # field-dependent dynamic conditions must treat that as "unknown".
        self.field = None
        self.pending_skills: list[PendingSkill] = list(skills)
        self.pending_removal: set[str] = set()
        self.used_skills: set[str] = set()
        self.gorosi_rng = Rule30CARng(self.rng.int32())
        self.pace_effect_rng = Rule30CARng(self.rng.int32())
        self.timers: list[Timer] = []
        self.accumulatetime = self._new_timer()

        # bit of a hack, see RaceSolver.ts's comment on gateRoll: n%k is
        # uniformly distributed for a random n only when n_max ≡ k-1 (mod k)
        # for every k; the smallest such n_max for k in [1,18] is
        # lcm(1..18)-1, so draw up to lcm(1..18).
        self.gate_roll = self.rng.uniform(12252240)
        self.random_lot = self.rng.uniform(100)
        self.phase = 0
        self.next_phase_transition = phase_start(self.course.distance, 1)
        self.active_target_speed_skills: list[_ActiveSkill] = []
        self.active_current_speed_skills: list[_ActiveSkill] = []
        self.active_accel_skills: list[_ActiveSkill] = []
        # Doc: Lane Move Speed Up (ability type 28) -- not a modifier that
        # gets added on activation; while active AND the horse moved lane on
        # the PREVIOUS frame, MoveLaneModifier = sqrt(0.0002*Power) is added
        # to target speed each such tick (see _update_target_speed).
        self.active_lane_speed_skills: list[_ActiveSkill] = []
        self.activate_count = [0, 0, 0]
        self.activate_count_heal = 0
        # Activations past the halfway mark of the course -- the DISTANCE half,
        # not a phase; see activation_conditions._activate_count_later_half_filter_gte.
        self.activate_count_later_half = 0
        self.activate_count_last_frame = 0
        self.on_skill_activate = on_skill_activate or _noop_hook
        self.on_skill_deactivate = on_skill_deactivate or _noop_hook
        self.section_length = self.course.distance / 24.0
        self.is_pace_down = False
        self.pos_keep_min_threshold = _PositionKeep.min_threshold(self.horse.strategy, self.course.distance)
        self.pos_keep_max_threshold = _PositionKeep.max_threshold(self.horse.strategy, self.course.distance)
        self.pos_keep_cooldown = self._new_timer()
        # Doc: position-keep runs the first 10 sections of the race.
        self.pos_keep_end = self.section_length * 10.0
        self.pos_keep_speed_coef = 1.0
        self._pos_keep_mode: Optional[str] = None  # 'speed_up' | 'overtake' (front-runner modes)
        # Real per-tick field-relative position keep (doc's full mode set)
        # replaces the original engine's synthetic-pacer-only approximation,
        # which our race harness never even enabled (raceRunner.ts never
        # calls useDefaultPacer()) -- position keep was previously always a
        # complete no-op for every horse in every race this server has ever
        # simulated. `field` is attached after construction, so both impls
        # early-return until it's set.
        if strategy_matches(self.horse.strategy, Strategy.NIGE):
            self._update_position_keep_impl = self._update_position_keep_front_runner
        else:
            self._update_position_keep_impl = self._update_position_keep_non_nige

        self.modifiers_target_speed = CompensatedAccumulator(0.0)
        self.modifiers_current_speed = CompensatedAccumulator(0.0)
        self.modifiers_accel = CompensatedAccumulator(0.0)
        self.modifiers_one_frame_accel = 0.0
        self.modifiers_special_skill_duration_scaling = 1.0
        self.modifiers_kakari_chance = 0.0

        # must come before the first round of skill activations so concen
        # etc can modify it
        self.start_delay = 0.1 * self.rng.random()
        if self.pacer:
            self.pacer.start_delay = 0.0

        self.pos = 0.0
        self.accel = 0.0
        self.current_speed = 3.0
        self.target_speed = 0.85 * _base_speed(self.course)
        self.is_downhill_mode = False
        self.is_last_spurt = False

        # Lane system (doc's Lane System section) -- not part of the
        # original TS engine. Initial lane: gate_index * one horse-lane
        # (0.625 course-widths / 18); the doc's Longchamp/gate>=10 adjustor
        # special cases are not modeled. Stored in course-width units.
        self.lane = self.gate_index * (1.0 / 18.0)
        self.lane_target = self.lane
        self.lane_change_speed = 0.0
        self._extra_move_lane: Optional[float] = None
        self._blocked_front_timer = 0.0
        self._blocked_side_timer = 0.0
        self._blocked_all_timer = 0.0
        self._infront_near_lane_timer = 0.0
        self._behind_near_lane_timer = 0.0
        self._moved_lane_this_tick = False

        # Competition mechanics (doc: Spot Struggle, Dueling, Compete Before
        # Spurt, Secure Lead, Stamina Keep) -- NOT part of the original TS
        # engine (no multi-horse awareness at all) and, per the doc itself,
        # the least-confident section of the whole reference: the author
        # explicitly flags these as "inferred from parameter file" or
        # "under investigation" rather than confirmed reverse-engineering.
        # Implemented anyway since the user asked for full Global accuracy,
        # but treat these as the first place to look if race behavior ever
        # looks wrong -- see each method's docstring for its specific caveat.
        self._spot_struggle_active = False
        self._spot_struggle_timer = self._new_timer()
        self._compete_spurt_active = False
        self._compete_spurt_timer = self._new_timer()
        self._compete_spurt_cooldown = self._new_timer()
        self._compete_spurt_modifier = 0.0
        self._secure_lead_active = False
        self._secure_lead_timer = self._new_timer()
        self._secure_lead_cooldown = self._new_timer()
        self._secure_lead_modifier = 0.0
        self._stamina_keep_active = False
        self._duel_continuous_timer = 0.0
        self._duel_active = False
        self._duel_timer = self._new_timer()
        self._duel_target_speed_modifier = 0.0
        self._duel_accel_modifier = 0.0
        self.process_skill_activations()  # activate gate skills (before minSpeed: green skills can modify guts)
        self.min_speed = 0.85 * _base_speed(self.course) + math.sqrt(200.0 * self.horse.guts) * 0.001
        self.start_dash = True
        self.modifiers_accel.add(24.0)  # start dash accel

        # after skill activations, since greens can affect downhill check if starting on a hill
        self._init_hills()

        # also must come after the first round of skill activations
        self.base_target_speed = [_base_target_speed(self.horse, self.course, p) for p in (0, 1, 2)]
        self.last_spurt_speed = _last_spurt_speed(self.horse, self.course)
        self.last_spurt_transition = -1

        # roll for section first, then check, so rng always advances twice
        # regardless of outcome (avoids data-dependent rng advancement)
        self.kakari_start = (2 + self.rng.uniform(7)) * self.section_length
        if self.rng.random() > math.pow(0.65 / math.log10(0.1 * self.horse.wisdom + 1), 2) + self.modifiers_kakari_chance:
            self.kakari_start = self.course.distance + 9999
        r1, r2, r3 = self.rng.random(), self.rng.random(), self.rng.random()
        seq = [0.0, r1, r2, r3, 1.0]
        idx = next(i for i, x in enumerate(seq) if x > 0.45)
        self.kakari_duration = 3.0 * idx
        self.kakari_timer = self._new_timer()
        self.is_kakari = False
        self.temptation_count = 0

        self.section_modifier: list[float] = []
        for _ in range(24):
            max_v = self.horse.wisdom / 5500.0 * math.log10(self.horse.wisdom * 0.1)
            factor = (max_v - 0.65 + self.rng.random() * 0.65) / 100.0
            self.section_modifier.append(_base_speed(self.course) * factor)
        self.section_modifier.append(0.0)  # tick after race is done / one uma runs off the end

        self.hp.init(self.horse)

        self.base_accel = [
            _base_accel(_UPHILL_BASE_ACCEL if i > 2 else _BASE_ACCEL, self.horse, p)
            for i, p in enumerate([0, 1, 2, 0, 1, 2])
        ]

    # -- setup helpers --------------------------------------------------

    def _new_timer(self, t: float = 0) -> Timer:
        tm = Timer(t)
        self.timers.append(tm)
        return tm

    def _init_hills(self) -> None:
        # slopes must be sorted by start for the sequential-consumption logic below
        self.n_hills = len(self.course.slopes)
        self.hill_start = [s.start for s in self.course.slopes][::-1]
        self.hill_end = [s.start + s.length for s in self.course.slopes][::-1]
        self.hill_idx = -1

        # separate rng per hill so a downhill proc on one hill doesn't affect
        # how many times the rng is rolled on a later hill
        self.hill_rng = [Rule30CARng(self.rng.int32(), self.rng.int32()) for _ in self.course.slopes]
        self.downhill_timer = self._new_timer()

        if self.hill_start and self.hill_start[-1] == 0:
            self.hill_idx = 0
            self.slope_per = self.course.slopes[0].slope
            self.downhill_timer.t = 0
            self._downhill_check(self.hill_rng[0].random())
            self.hill_start.pop()
        else:
            self.slope_per = 0

    # -- per-tick ---------------------------------------------------------

    def get_max_speed(self) -> float:
        if self.start_dash:
            return min(self.target_speed, 0.85 * _base_speed(self.course))
        elif self.current_speed + self.modifiers_one_frame_accel > self.target_speed:
            return 9999.0  # allow decelerating if targetSpeed drops
        else:
            return self.target_speed

    def step(self, dt: float) -> None:
        if self.accumulatetime.t < self.start_delay:
            partial_frame = self.start_delay - self.accumulatetime.t
            if partial_frame < dt:
                for tm in self.timers:
                    tm.t += partial_frame
                dt -= partial_frame
            else:
                for tm in self.timers:
                    tm.t += dt
                return

        if self.pos < self.pos_keep_end and self.pacer is not None:
            self.pacer.step(dt)

        halfv = min(self.current_speed + 0.5 * dt * self.accel, self.get_max_speed())
        displacement = halfv + self.modifiers_current_speed.acc + self.modifiers_current_speed.err
        self.pos += displacement * dt
        self.hp.tick(self, dt)
        for tm in self.timers:
            tm.t += dt
        self._update_hills()
        self._update_phase()
        self.process_skill_activations()
        self._update_kakari()
        self._update_position_keep(dt)
        self._update_last_spurt_state()
        self._update_target_speed()
        self._apply_forces()
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
        self._update_competition_mechanics(dt)

    def _update_blocking(self, dt: float) -> None:
        """Doc's Blocking section -- not part of the original TS engine
        (single-horse-only, no notion of other horses). Front block caps
        actual speed to a fraction of the blocking horse's speed; side/all
        just accumulate continuous-duration timers the skill-condition table
        reads (blocked_front_continuetime etc)."""
        if self.field is None:
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
        else:
            self._blocked_front_timer = 0.0
        inner_blocked, outer_blocked = self.field.side_room(self)
        side_blocked = inner_blocked or outer_blocked
        self._blocked_side_timer = self._blocked_side_timer + dt if side_blocked else 0.0
        all_blocked = blocker is not None and side_blocked
        self._blocked_all_timer = self._blocked_all_timer + dt if all_blocked else 0.0

    def _update_lane_target(self) -> None:
        """Simplified version of the doc's Target Lane Selection (Normal
        mode's rail-drift rule, plus a basic "move to whichever side has
        room" escape when blocked in front). NOT the full documented
        algorithm -- the doc's Overtake-mode candidate lane scoring against
        every nearby horse's crowd, and Fixed mode (skill-driven lane
        targets), are not modeled. This still gives horses a real reason to
        change lanes (drift to the rail when clear, swing out to pass when
        blocked) rather than lanes being purely cosmetic."""
        if self.field is None:
            return
        if not self.hp.has_remaining_hp():
            self.lane_target = self.lane
            return
        if self.is_pace_down:
            self.lane_target = 0.18
            return
        if self.course.corners and self._extra_move_lane is None and self.pos >= self.course.corners[-1].start:
            lane_distance = self.lane
            self._extra_move_lane = min(1.0, max(0.0, lane_distance / 0.1)) * 0.5 + self.rng.random() * 0.1
        if self._extra_move_lane is not None:
            self.lane_target = self._extra_move_lane
            return
        inner_blocked, outer_blocked = self.field.side_room(self)
        if self.field.front_blocker(self) is not None:
            if not outer_blocked:
                self.lane_target = min(self.course.max_lane, self.lane + 1.0 / 18.0)
            elif not inner_blocked:
                self.lane_target = max(0.0, self.lane - 1.0 / 18.0)
            # else boxed in on both sides -- stay put
        else:
            self.lane_target = max(0.0, self.lane - 0.05)

    def _update_lane(self, dt: float) -> None:
        """Doc's Lane Change Speed formulas (target/current/actual speed),
        applied toward `lane_target` from `_update_lane_target`."""
        if self.field is None:
            return
        self._update_lane_target()
        lane_distance = abs(self.lane_target - self.lane)
        if lane_distance < 1e-9:
            self.lane_change_speed = 0.0
            self._moved_lane_this_tick = False
            return
        first_move_modifier = 1.0 + lane_distance / max(self.course.max_lane, 1e-9) * 0.05
        order_modifier = 1.0 + self.field.order_of(self) * 0.01 if self.phase >= 2 else 1.0
        target_lane_change_speed = 0.02 * (0.3 + 0.001 * self.horse.power) * first_move_modifier * order_modifier
        lane_change_accel = 0.02 * 1.5
        if self.lane_change_speed < target_lane_change_speed:
            self.lane_change_speed = min(target_lane_change_speed, self.lane_change_speed + lane_change_accel * dt)
        actual_speed = max(0.0, min(0.6, self.lane_change_speed))
        direction = 1.0 if self.lane_target > self.lane else -1.0
        step = actual_speed * dt * direction
        if abs(step) >= lane_distance:
            self.lane = self.lane_target
            self.lane_change_speed = 0.0
        else:
            self.lane += step
        self.lane = max(0.0, min(self.course.max_lane, self.lane))
        self._moved_lane_this_tick = abs(step) > 1e-9

    def _update_near_lane_timers(self, dt: float) -> None:
        """Doc: behind/infront_near_lane_time -- near = |distGap|<2.5m AND
        |laneGap|<1 horse-lane, checked only against the horse immediately
        ahead/behind by live order; accumulates while continuously true.
        Simplification: resets whenever the gap stops qualifying, rather
        than specifically tracking "did the adjacent horse's identity
        change" (the doc's stated reset trigger) -- in practice these
        coincide almost always, since an order swap between two horses that
        were near enough to trigger this rarely leaves the new neighbor
        still within the near window."""
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

    def _update_kakari(self) -> None:
        if self.temptation_count == 0 and self.pos >= self.kakari_start:
            self.is_kakari = True
            self.temptation_count = 1
            self.kakari_timer.t = -self.kakari_duration
            self.on_skill_activate(self, 'kakari', Perspective.SELF)
        elif self.is_kakari and self.kakari_timer.t >= 0:
            self.is_kakari = False
            self.on_skill_deactivate(self, 'kakari', Perspective.SELF)

    def _update_position_keep(self, dt: float) -> None:
        if self._update_position_keep_impl is not None:
            self._update_position_keep_impl(dt)

    def _roll_position_keep_wisdom(self, coef: float, dt: float) -> bool:
        """Doc: several position-keep entry checks roll a wisdom-scaled
        chance "every 2 seconds" (e.g. 20*log10(Wiz*0.1)%). Converted here to
        an equivalent continuous per-tick hazard rate rather than literally
        gating on a 2s interval timer, so entry can happen on whichever tick
        the distance condition first holds -- a smoothing of the doc's
        discrete-interval model, not a literal implementation of it."""
        pct = max(0.0, coef * math.log10(max(self.horse.wisdom, 1) * 0.1))
        per_tick_chance = 1.0 - (1.0 - min(pct, 100.0) / 100.0) ** (dt / 2.0)
        return self.rng.random() < per_tick_chance

    def _update_position_keep_non_nige(self, dt: float) -> None:
        """Doc's Non-Front-Runner position-keep modes (Pace-up / Pace-down),
        now measured against the real field's live pacemaker
        (RaceField.pacemaker) instead of the original engine's synthetic
        one-off pacer solver, which our race harness never even
        constructed -- position keep was previously a complete no-op for
        every non-front-runner in every race this server has simulated."""
        if self.pos >= self.pos_keep_end:
            self.is_pace_down = False
            self._pos_keep_mode = None
            self.pos_keep_speed_coef = 1.0
            return
        if self.field is None or self.field.pacemaker() is self:
            # No meaningful pacemaker to keep pace against (this horse IS
            # the pacemaker, e.g. the only Nige/Oonige-tier horse present,
            # or a solo/no-other-strategy field) -- without this guard,
            # gap_to_pacemaker() reads as 0 for one's own self and would
            # spuriously trigger pace-down forever. Clear cleanly in case
            # this horse held the pacemaker role away and just took it over.
            self._pos_keep_mode = None
            self.is_pace_down = False
            self.pos_keep_speed_coef = 1.0
            return
        gap = self.field.gap_to_pacemaker(self)
        no_speed_skills = len(self.active_target_speed_skills) == 0 and len(self.active_current_speed_skills) == 0

        if self._pos_keep_mode == 'pace_down':
            if (gap > self.pos_keep_effect_exit_distance
                    or self.pos - self.pos_keep_effect_start > self.section_length
                    or not no_speed_skills
                    or self.is_kakari):
                self._pos_keep_mode = None
                self.is_pace_down = False
                self.pos_keep_cooldown.t = -3.0
                self.pos_keep_speed_coef = 1.0
            return
        if self._pos_keep_mode == 'pace_up':
            if gap < self.pos_keep_effect_exit_distance:
                self._pos_keep_mode = None
                self.pos_keep_speed_coef = 1.0
            return

        if (gap < self.pos_keep_min_threshold and no_speed_skills
                and not self.is_kakari and self.pos_keep_cooldown.t >= 0):
            self._pos_keep_mode = 'pace_down'
            self.is_pace_down = True
            self.pos_keep_effect_start = self.pos
            lo = self.pos_keep_min_threshold
            hi = lo + 0.5 * (self.pos_keep_max_threshold - lo) if self.phase == 1 else self.pos_keep_max_threshold
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo)
            self.pos_keep_speed_coef = 0.945 if self.phase == 1 else 0.915
        elif gap > self.pos_keep_max_threshold and self._roll_position_keep_wisdom(15.0, dt):
            self._pos_keep_mode = 'pace_up'
            lo, hi = self.pos_keep_min_threshold, self.pos_keep_max_threshold
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo)
            self.pos_keep_speed_coef = 1.04

    def _update_position_keep_front_runner(self, dt: float) -> None:
        """Doc's Front-Runner position-keep modes (Speed-up / Overtake),
        for Nige/Oonige strategy only. Not part of the original engine at
        all (it only ever modeled the non-front-runner pace-down case)."""
        if self.pos >= self.pos_keep_end:
            self._pos_keep_mode = None
            self.pos_keep_speed_coef = 1.0
            return
        if self.field is None:
            return
        is_leader = self.field.order_of(self) == 1
        gap_2nd = self.field.distance_to_behind(self) if is_leader else None
        solo = self.field.is_solo_front_runner(self)
        is_oonige = self.horse.strategy == Strategy.OONIGE
        speed_up_threshold = 12.5 if solo else (17.5 if is_oonige else 4.5)
        overtake_exit_threshold = 27.5 if is_oonige else 10.0

        if self._pos_keep_mode == 'speed_up':
            if not is_leader or gap_2nd is None or gap_2nd >= speed_up_threshold:
                self._pos_keep_mode = None
                self.pos_keep_speed_coef = 1.0
            return
        if self._pos_keep_mode == 'overtake':
            if is_leader and gap_2nd is not None and gap_2nd >= overtake_exit_threshold:
                self._pos_keep_mode = None
                self.pos_keep_speed_coef = 1.0
            return

        if is_leader and gap_2nd is not None and gap_2nd < speed_up_threshold:
            if self._roll_position_keep_wisdom(20.0, dt):
                self._pos_keep_mode = 'speed_up'
                self.pos_keep_speed_coef = 1.04
        elif not is_leader:
            if self._roll_position_keep_wisdom(20.0, dt):
                self._pos_keep_mode = 'overtake'
                self.pos_keep_speed_coef = 1.05

    def _update_last_spurt_state(self) -> None:
        if self.is_last_spurt or self.phase < 2:
            return
        if self.last_spurt_transition == -1:
            transition, speed = self.hp.get_last_spurt_pair(self, self.last_spurt_speed, self.base_target_speed[2])
            self.last_spurt_transition = transition
            self.last_spurt_speed = speed
        if self.pos >= self.last_spurt_transition:
            self.is_last_spurt = True

    def _update_target_speed(self) -> None:
        if not self.hp.has_remaining_hp():
            self.target_speed = self.min_speed
        elif self.is_last_spurt:
            self.target_speed = self.last_spurt_speed
        else:
            self.target_speed = self.base_target_speed[self.phase] * self.pos_keep_speed_coef
            self.target_speed += self.section_modifier[int(self.pos // self.section_length)]
        self.target_speed += self.modifiers_target_speed.acc + self.modifiers_target_speed.err

        if self.is_downhill_mode:
            self.target_speed += 0.3 + self.slope_per / 100000.0
        elif self.hill_idx != -1 and self.slope_per > 0:
            self.target_speed -= self.slope_per / 10000.0 * 200.0 / self.horse.power
            self.target_speed = max(self.target_speed, self.min_speed)

        # Doc: MoveLaneModifier -- applied when affected by a skill that
        # modifies lane move speed (ability type 28) AND lane movement
        # happened on the PREVIOUS frame. This runs before _update_lane(dt)
        # in step(), so _moved_lane_this_tick still holds last tick's value
        # here -- exactly "the previous frame", no extra state needed.
        if self.active_lane_speed_skills and self._moved_lane_this_tick:
            self.target_speed += math.sqrt(0.0002 * self.horse.power)

        # BUG FIXED 2026-08-29 (live-reported: the player's horse -- and only
        # the player's, tested and confirmed independent of gate/starting
        # position -- accelerates to an absurd speed, 39-51 m/s observed,
        # around 130-180% over any real racehorse). Wiki (Game:Mechanics,
        # Target Speed section) states explicitly: "Target speed cannot go
        # below minimum speed or exceed 30 m/s." Nothing enforced that
        # ceiling anywhere -- get_max_speed() just returns target_speed
        # unbounded, and modifiers_target_speed.acc accumulates every
        # active skill's additive speed bonus with no cap of its own. An
        # admin-injected roster entry running with every skill in the game
        # simultaneously (100+ concurrently active, confirmed by direct
        # measurement) stacks enough additive bonuses to blow well past 30
        # m/s -- a real account could never reach this ceiling through
        # normal stacking, so it was never hit until now. Cap here, after
        # every additive/uphill/downhill modifier has already applied, so
        # the fix applies uniformly regardless of source.
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
        # phase 3 (from 5/6 distance) is treated the same as phase 2 for
        # speed-modifier purposes -- capped at 2 here deliberately.
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
                self.on_skill_deactivate(self, s.skill_id, s.perspective)

        activate_count_this_frame = 0
        for i in range(len(self.pending_skills) - 1, -1, -1):
            s = self.pending_skills[i]
            if self.pos >= s.trigger.end or s.skill_id in self.pending_removal:
                del self.pending_skills[i]
                self.pending_removal.discard(s.skill_id)
            elif self.pos >= s.trigger.start and s.extra_condition(self):
                self.activate_skill(s)
                del self.pending_skills[i]
                if s.skill_id != 'asitame' and s.skill_id != 'staminasyoubu':
                    activate_count_this_frame += 1
        self.activate_count_last_frame = activate_count_this_frame

    def activate_skill(self, s: PendingSkill) -> None:
        # ExtendEvolvedDuration must apply after other effects on the same
        # skill, so it doesn't extend its own siblings' durations.
        effects = sorted(s.effects, key=lambda ef: 1 if ef.type == SkillType.EXTEND_EVOLVED_DURATION else 0)
        for ef in effects:
            scaled_duration = ef.base_duration * (self.course.distance / 1000) * (
                self.modifiers_special_skill_duration_scaling if s.rarity == SkillRarity.EVOLUTION else 1
            )
            t = ef.type
            if t == SkillType.NOOP:
                pass
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
                # IDENTIFIED 2026-09-03. Neither the wiki, the race docs PDF,
                # nor the old TS engine names type 32, so it used to be a
                # silent no-op. The Global skill text does name it: 102602211
                # "Perfect Boost" / 103102111 "Perfect (music note) Order Up!"
                # are literally "Decrease time lost to slow starts and very
                # slightly increase all attributes" -- the start-delay half is
                # their type 10, so type 32 is the "all attributes" half. Its
                # modifier is on the same 1/10000 stat-point scale as types 1-5
                # (100000 -> +10 "very slightly"; 200000/600000 on the bigger
                # passives; -200000 on the one debuff, 203691), and 104301221
                # confirms it stacks ON TOP of separate type 1/type 3 grants in
                # the same alternative rather than replacing them.
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
                self.active_target_speed_skills.append(_ActiveSkill(
                    s.skill_id, s.perspective, self._new_timer(-scaled_duration), ef.modifier))
            elif t == SkillType.MODIFY_KAKARI_CHANCE:
                self.modifiers_kakari_chance += ef.modifier / 100.0
            elif t == SkillType.ACCEL:
                self.modifiers_accel.add(ef.modifier)
                self.active_accel_skills.append(_ActiveSkill(
                    s.skill_id, s.perspective, self._new_timer(-scaled_duration), ef.modifier))
            elif t in (SkillType.CURRENT_SPEED, SkillType.CURRENT_SPEED_WITH_NATURAL_DECELERATION):
                self.modifiers_current_speed.add(ef.modifier)
                self.active_current_speed_skills.append(_ActiveSkill(
                    s.skill_id, s.perspective, self._new_timer(-scaled_duration), ef.modifier,
                    natural_deceleration=(t == SkillType.CURRENT_SPEED_WITH_NATURAL_DECELERATION)))
            elif t == SkillType.RECOVERY:
                if s.perspective == Perspective.SELF:
                    self.activate_count_heal += 1
                self.hp.recover(ef.modifier)
                self._stamina_keep_active = False  # doc: HP-recovery resets Stamina Keep
                if self.phase >= 2 and not self.is_last_spurt:
                    self.last_spurt_transition = -1
                    self._update_last_spurt_state()
            elif t == SkillType.LANE_MOVE_SPEED_UP:
                self.active_lane_speed_skills.append(_ActiveSkill(
                    s.skill_id, s.perspective, self._new_timer(-scaled_duration), ef.modifier))
            elif t == SkillType.ACTIVATE_RANDOM_GOLD:
                self._do_activate_random_gold(ef.modifier)
            elif t == SkillType.EXTEND_EVOLVED_DURATION:
                self.modifiers_special_skill_duration_scaling = ef.modifier

        if s.perspective == Perspective.SELF:
            self.activate_count[self.phase] += 1
            if self.pos >= self.course.distance / 2:
                self.activate_count_later_half += 1
        self.used_skills.add(s.skill_id)
        self.on_skill_activate(self, s.skill_id, s.perspective)

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

    # -- competition mechanics (doc: Spot Struggle, Dueling, Compete Before
    # Spurt, Secure Lead, Stamina Keep) -- see the __init__ comment above
    # these fields' declarations for the overall confidence caveat.

    def _update_competition_mechanics(self, dt: float) -> None:
        if self.field is None:
            return
        self._update_stamina_keep(dt)
        self._update_spot_struggle(dt)
        self._update_compete_before_spurt(dt)
        self._update_secure_lead(dt)
        self._update_dueling(dt)

    def _update_stamina_keep(self, dt: float) -> None:
        """Doc: Stamina Keep -- conserve roughly 1.035-1.04x the HP needed to
        finish. The doc's own author flags this activation-chance formula as
        "highly speculative", explicitly contradicted by their own
        packet-capture testing (93/100 immediate activations observed at
        1000 Wiz, far higher than this formula predicts). Implemented anyway
        since it's the only documented version -- treat this specific
        mechanic as the least trustworthy in the whole engine. "HP needed to
        finish" is approximated here as remaining-course-fraction (a coarse
        proxy; the doc doesn't specify how the real game estimates this
        either). While active, suppresses Compete Before Spurt / Secure
        Lead entry, per doc; reset when an HP-recovery skill fires (see
        SkillType.RECOVERY handling in activate_skill)."""
        if self._stamina_keep_active or self.course.distance <= 0:
            return
        remaining_frac = max(0.0, (self.course.distance - self.pos) / self.course.distance)
        if self.hp.hp_ratio_remaining() >= remaining_frac * 1.04:
            return
        wisdom = max(self.horse.wisdom, 1)
        pct = 30.0 * (wisdom / 1000.0 + math.pow(wisdom, 0.03))
        per_tick_chance = 1.0 - (1.0 - min(pct, 100.0) / 100.0) ** (dt / 2.0)
        if self.rng.random() < per_tick_chance:
            self._stamina_keep_active = True

    def _update_spot_struggle(self, dt: float) -> None:
        """Doc: Spot Struggle / Lead Competition -- flagged by the doc as
        "inferred from parameter file, less concretely confirmed". Only
        Nige/Oonige-strategy horses compete, and only within their own
        strategy (Nige-vs-Nige, Oonige-vs-Oonige, never cross-group)."""
        if self._spot_struggle_active:
            if self._spot_struggle_timer.t >= 0 or self.pos >= self.section_length * 9.0:
                self._spot_struggle_active = False
                self.modifiers_target_speed.add(-self._spot_struggle_modifier)
            return
        if self.pos < 150.0 or self.pos >= self.section_length * 9.0:
            return
        strategy = self.horse.strategy
        if strategy not in (Strategy.NIGE, Strategy.OONIGE):
            return
        # frontmost horse of this same strategy group
        group = [s for s in self.field.solvers if s.horse.strategy == strategy]
        if len(group) < 2:
            return
        front = max(group, key=lambda s: s.pos)
        if front is self:
            return
        dist_gap = front.pos - self.pos
        lane_gap = abs(front.lane - self.lane)
        # 1 course-width = 11.25m (doc); 0.165 course-width bound below
        if not (dist_gap < 3.75 and lane_gap < 0.165 * 11.25):
            return
        guts = max(self.horse.guts, 1)
        strat_prof = _STRATEGY_PROFICIENCY_MODIFIER_APPROX[self.horse.strategy_aptitude]
        self._spot_struggle_modifier = math.pow(500.0 * guts, 0.6) * 0.0001
        duration = math.pow(700.0 * guts, 0.5) * 0.012 * strat_prof
        self._spot_struggle_active = True
        self._spot_struggle_timer.t = -duration
        self.modifiers_target_speed.add(self._spot_struggle_modifier)

    def _update_compete_before_spurt(self, dt: float) -> None:
        """Doc: Compete Before Spurt (位置取り調整), sections 11-15. The doc
        gives concrete EFFECT formulas but not an exact entry-chance formula
        (just "wisdom-scaled" / "guts-scaled", no expression given) -- the
        entry roll below reuses the same wisdom-log curve as the documented
        position-keep rolls elsewhere in this doc, which is an inference by
        analogy, not a transcribed formula."""
        if self._compete_spurt_active:
            if self._compete_spurt_timer.t >= 0:
                self._compete_spurt_active = False
                self._compete_spurt_cooldown.t = -1.0
                self.modifiers_target_speed.add(-self._compete_spurt_modifier)
            return
        section_lo, section_hi = self.section_length * 10.0, self.section_length * 15.0
        if not (section_lo <= self.pos < section_hi) or self._compete_spurt_cooldown.t < 0:
            return
        if self._stamina_keep_active:
            return
        strategy = self.horse.strategy
        dist_threshold = _COMPETE_DIST_THRESHOLD[strategy]
        far_from_lead = self.field.distance_to_leader(self) > dist_threshold
        near = self.field.near_count(self) > 0
        if not (far_from_lead or near):
            return
        if not self._roll_position_keep_wisdom(15.0, dt):
            return
        strat_coef = _COMPETE_STRATEGY_COEF[strategy]
        solo_bonus = 1.0
        if strategy in (Strategy.NIGE, Strategy.OONIGE) and self.field.is_solo_front_runner(self):
            solo_bonus = 2.0 if strategy == Strategy.OONIGE else 1.1
        modifier = ((math.pow(max(self.horse.power, 0) / 1500.0, 0.5) * 2.0
                     + math.pow(max(self.horse.guts, 0) / 3000.0, 0.2)) * 0.1 * strat_coef * solo_bonus)
        stamina_strat_coef = _COMPETE_STAMINA_STRATEGY_COEF[strategy]
        dist_coef = _competition_distance_coefficient(self.course.distance)
        near_factor = 0.5 if near and not far_from_lead else 0.0
        stamina_cost = 20.0 * (stamina_strat_coef * dist_coef + near_factor) * 2.0  # 2s window
        self._compete_spurt_active = True
        self._compete_spurt_modifier = modifier
        self._compete_spurt_timer.t = -2.0
        self.modifiers_target_speed.add(modifier)
        if hasattr(self.hp, "hp"):
            self.hp.hp -= stamina_cost

    def _update_secure_lead(self, dt: float) -> None:
        """Doc: Secure Lead (リード確保), sections 11-15. The doc's real
        DesirableLead formula uses a full per-strategy-pair coefficient
        matrix that wasn't fully captured in this port's reference notes
        (only a handful of example entries were) -- approximated below with
        a single flat coefficient instead of the real matrix. Directionally
        correct (leaders occasionally surge to hold off a chaser) but the
        exact threshold distance is a guess, not the documented value."""
        if self._secure_lead_active:
            if self._secure_lead_timer.t >= 0:
                self._secure_lead_active = False
                self._secure_lead_cooldown.t = -1.0
                self.modifiers_target_speed.add(-self._secure_lead_modifier)
            return
        section_lo, section_hi = self.section_length * 10.0, self.section_length * 15.0
        if not (section_lo <= self.pos < section_hi) or self._secure_lead_cooldown.t < 0:
            return
        if self._stamina_keep_active:
            return
        gap_behind = self.field.distance_to_behind(self)
        if gap_behind is None:
            return
        desirable_lead = _SECURE_LEAD_COEF_APPROX + 0.0003 * (self.course.distance + 1000)
        if gap_behind >= desirable_lead:
            return
        if self.rng.random() >= 0.20 * (dt / 2.0):
            return
        strategy = self.horse.strategy
        strat_coef = _SECURE_LEAD_STRATEGY_COEF.get(strategy, 1.0)
        solo_bonus = 1.0
        if strategy in (Strategy.NIGE, Strategy.OONIGE) and self.field.is_solo_front_runner(self):
            solo_bonus = 7.0 if strategy == Strategy.OONIGE else 2.0
        modifier = math.pow(max(self.horse.guts, 0) / 2000.0, 0.5) * 0.3 * strat_coef * solo_bonus
        stamina_strat_coef = _SECURE_LEAD_STAMINA_STRATEGY_COEF.get(strategy, 0.8)
        dist_coef = _competition_distance_coefficient(self.course.distance)
        stamina_cost = 20.0 * stamina_strat_coef * dist_coef * 2.0
        self._secure_lead_active = True
        self._secure_lead_modifier = modifier
        self._secure_lead_timer.t = -2.0
        self.modifiers_target_speed.add(modifier)
        if hasattr(self.hp, "hp"):
            self.hp.hp -= stamina_cost

    def _update_dueling(self, dt: float) -> None:
        """Doc: Dueling / Compete Fight (追い比べ), final straight only.
        Flagged "inferred from parameter file" by the doc."""
        if not self.course.straights:
            return
        last_straight = self.course.straights[-1]
        in_final_straight = last_straight.start <= self.pos < last_straight.end
        if self._duel_active:
            if not in_final_straight or self.hp.hp_ratio_remaining() < 0.05:
                self._duel_active = False
                self.modifiers_target_speed.add(-self._duel_target_speed_modifier)
                self.modifiers_accel.add(-self._duel_accel_modifier)
            return
        if not in_final_straight or self.hp.hp_ratio_remaining() < 0.15:
            self._duel_continuous_timer = 0.0
            return
        target = None
        for other in self.field.solvers:
            if other is self:
                continue
            dist_gap = other.pos - self.pos
            lane_gap = other.lane - self.lane
            if abs(dist_gap) < 3.0 and abs(lane_gap) < 0.25 * 11.25:
                target = other
                break
        if target is None:
            self._duel_continuous_timer = 0.0
            return
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

    def cleanup(self) -> None:
        """Deactivate any skills that haven't finished their durations yet
        (call at the end of a simulation, in case a skill activated near the
        end and the race finished before its duration expired)."""
        for s in self.active_target_speed_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for s in self.active_current_speed_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        for s in self.active_accel_skills:
            self.on_skill_deactivate(self, s.skill_id, s.perspective)
        if self.is_downhill_mode:
            self.on_skill_deactivate(self, 'downhill', Perspective.SELF)
