"""Port of HpPolicy.ts -- HP/stamina drain and the last-spurt decision.

`get_last_spurt_pair` is the most safety-critical formula in the whole
engine: it decides both whether/where a horse commits to max effort for
the remainder of the race, and (if HP is insufficient to sprint the whole
remaining distance) which throttled-down speed to settle for instead, via
a candidate search + stochastic "subpar acceptance" roll. Transcribed
verbatim from HpPolicy.ts, including the two-equation solve in the
candidate loop.
"""

from __future__ import annotations

import math

from .course_data import CourseData, phase_start
from .random_gen import Rule30CARng

_HP_STRATEGY_COEFFICIENT = [0, 0.95, 0.89, 1.0, 0.995, 0.86]
_HP_CONSUMPTION_GROUND_MODIFIER = [
    [],
    [0, 1.0, 1.0, 1.02, 1.02],
    [0, 1.0, 1.0, 1.01, 1.02],
]


class NoopHpPolicy:
    """Used only for pacer solvers -- infinite HP."""

    def init(self, horse) -> None:
        pass

    def tick(self, state, dt: float) -> None:
        pass

    def has_remaining_hp(self) -> bool:
        return True

    def hp_ratio_remaining(self) -> float:
        return 1.0

    def recover(self, modifier: float) -> None:
        pass

    def get_last_spurt_pair(self, state, max_speed: float, base_target_speed2: float):
        return -1, max_speed


class GameHpPolicy:
    def __init__(self, course: CourseData, ground: int, rng: Rule30CARng):
        self.distance = course.distance
        self.base_speed = 20.0 - (course.distance - 2000) / 1000.0
        self.ground_modifier = _HP_CONSUMPTION_GROUND_MODIFIER[course.surface][ground]
        self.rng = rng
        # placeholder until init() -- the first round of skill activations
        # happens before init() is called, but some conditions access HP
        # methods during that round (e.g. is_hp_empty_onetime), so this must
        # be "initialized enough" for them not to blow up.
        self.max_hp = 1.0
        self.hp = 1.0
        self.guts_modifier = 1.0
        self.subpar_accept_chance = 0

    def init(self, horse) -> None:
        self.max_hp = 0.8 * _HP_STRATEGY_COEFFICIENT[horse.strategy] * horse.stamina + self.distance
        self.hp = self.max_hp
        self.guts_modifier = 1.0 + 200.0 / math.sqrt(600.0 * horse.guts)
        self.subpar_accept_chance = round((15.0 + 0.05 * horse.wisdom) * 1000)

    def get_status_modifier(self, state) -> float:
        modifier = 1.0
        if state.is_pace_down:
            modifier *= 0.6
        if state.is_downhill_mode:
            modifier *= 0.4
        if state.is_kakari:
            modifier *= 1.6
        return modifier

    def hp_per_second(self, state, velocity: float) -> float:
        guts_modifier = self.guts_modifier if state.phase >= 2 else 1.0
        return (20.0 * math.pow(velocity - self.base_speed + 12.0, 2) / 144.0
                * self.get_status_modifier(state) * self.ground_modifier * guts_modifier)

    def tick(self, state, dt: float) -> None:
        # NOTE unsure whether hp is consumed by `amount*dt` per frame or
        # `amount` once every second; believed to be the former (a rate).
        self.hp -= self.hp_per_second(state, state.current_speed) * dt

    def has_remaining_hp(self) -> bool:
        return self.hp > 0.0

    def hp_ratio_remaining(self) -> float:
        return max(0.0, self.hp / self.max_hp)

    def recover(self, modifier: float) -> None:
        self.hp = min(self.max_hp, self.hp + self.max_hp * modifier)

    def get_last_spurt_pair(self, state, max_speed: float, base_target_speed2: float):
        max_dist = self.distance - phase_start(self.distance, 2)
        s = (max_dist - 60) / max_speed
        lastleg = _LastLegState(phase=2, is_pace_down=False, is_downhill_mode=False, is_kakari=False)
        if self.hp >= self.hp_per_second(lastleg, max_speed) * s:
            return -1, max_speed

        candidates: list[tuple[float, float]] = []
        remain_distance = self.distance - 60 - state.pos
        speed = max_speed - 0.1
        while speed >= base_target_speed2:
            hp_at_base = self.hp_per_second(lastleg, base_target_speed2)
            hp_at_speed = self.hp_per_second(lastleg, speed)
            denom = base_target_speed2 * hp_at_speed - hp_at_base * speed
            numer = base_target_speed2 * self.hp - hp_at_base * remain_distance
            spurt_duration = min(remain_distance / speed, max(0.0, numer / denom))
            spurt_distance = spurt_duration * speed + 60
            candidates.append((self.distance - spurt_distance, speed))
            speed -= 0.1

        if len(candidates) == 0:
            # maxSpeed - 0.1 < baseTargetSpeed2 (can happen with very low
            # speed, e.g. 1). Not clear what's "correct"; opt for never
            # starting spurt, matching the TS engine.
            return self.distance, max_speed

        def finish_time(cand):
            pos, spd = cand
            return (pos - state.pos) / base_target_speed2 + (self.distance - pos) / spd

        candidates.sort(key=finish_time)
        for cand in candidates:
            if self.rng.uniform(100000) <= self.subpar_accept_chance:
                return cand
        return candidates[-1]


class _LastLegState:
    __slots__ = ("phase", "is_pace_down", "is_downhill_mode", "is_kakari")

    def __init__(self, phase, is_pace_down, is_downhill_mode, is_kakari):
        self.phase = phase
        self.is_pace_down = is_pace_down
        self.is_downhill_mode = is_downhill_mode
        self.is_kakari = is_kakari
