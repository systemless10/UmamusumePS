"""Multi-horse field coordination -- NOT part of the original TS engine
(which runs every RaceSolver in isolation, oblivious to other horses). This
is the foundation for Global-accuracy mechanics that genuinely depend on
where other horses are: live order/rank, gap-to-ahead/behind (bashin_diff_*,
distance_diff_top, order/order_rate), position-keep relative to a real
pacemaker, blocking, and lane selection.

`RaceSolver.field` is set to a `RaceField` once the whole roster is built
(race_runner.py does this after constructing every solver, before the tick
loop starts). It is `None` during the very first "gate skill" activation
round inside `RaceSolver.__init__` -- at that point no other horse exists
yet -- so every dynamic condition that reads `s.field` must handle `None`
by treating the condition as not-yet-determinable (return False).
"""

from __future__ import annotations

from typing import Optional

from .horse_types import Strategy, strategy_matches


class RaceField:
    def __init__(self, solvers: list):
        self.solvers = solvers
        self.n = len(solvers)
        # index -> 1-based rank (1 = leader), updated after every full tick
        self.rank_of: list[int] = [0] * self.n
        # ranks sorted ascending -> solver index, i.e. order[0] is the leader
        self.order: list[int] = list(range(self.n))
        # previous tick's rank, for overtake/order-improvement detection
        self._prev_rank_of: list[int] = list(self.rank_of)
        for s in solvers:
            s.field = self
        self.update()

    def update(self) -> None:
        """Recompute live order/rank from each solver's current `pos`. Call
        once after every horse has been stepped for this tick."""
        self._prev_rank_of = self.rank_of
        idx = sorted(range(self.n), key=lambda i: -self.solvers[i].pos)
        self.order = idx
        rank_of = [0] * self.n
        for rank, i in enumerate(idx):
            rank_of[i] = rank + 1
        self.rank_of = rank_of

    def _index_of(self, solver) -> int:
        # n is at most 18 (a full gate); linear scan is cheap and avoids
        # solvers needing to know their own field-assigned index.
        for i, s in enumerate(self.solvers):
            if s is solver:
                return i
        raise ValueError("solver not in this field")

    def order_of(self, solver) -> int:
        """1-based live finishing-position rank (1 = leader)."""
        return self.rank_of[self._index_of(solver)]

    def previous_order_of(self, solver) -> int:
        return self._prev_rank_of[self._index_of(solver)]

    def leader_pos(self) -> float:
        return self.solvers[self.order[0]].pos

    def distance_to_leader(self, solver) -> float:
        """Meters behind the current leader (0 if this solver IS the leader)."""
        return max(0.0, self.leader_pos() - solver.pos)

    def distance_to_ahead(self, solver) -> Optional[float]:
        """Meters behind the horse immediately ahead, or None if leading."""
        i = self._index_of(solver)
        r = self.rank_of[i]
        if r <= 1:
            return None
        ahead = self.order[r - 2]
        return max(0.0, self.solvers[ahead].pos - solver.pos)

    def distance_to_behind(self, solver) -> Optional[float]:
        """Meters ahead of the horse immediately behind, or None if last."""
        i = self._index_of(solver)
        r = self.rank_of[i]
        if r >= self.n:
            return None
        behind = self.order[r]
        return max(0.0, solver.pos - self.solvers[behind].pos)

    def lane_gap_to_ahead(self, solver) -> Optional[float]:
        i = self._index_of(solver)
        r = self.rank_of[i]
        if r <= 1:
            return None
        ahead = self.order[r - 2]
        return self.solvers[ahead].lane - solver.lane

    def lane_gap_to_behind(self, solver) -> Optional[float]:
        i = self._index_of(solver)
        r = self.rank_of[i]
        if r >= self.n:
            return None
        behind = self.order[r]
        return solver.lane - self.solvers[behind].lane

    def did_overtake(self, solver) -> bool:
        """True on the tick this solver's rank improved (took over at least
        one other horse) versus the previous tick."""
        i = self._index_of(solver)
        return self.rank_of[i] < self._prev_rank_of[i]

    # -- lane / blocking / vision (doc's Lane System + Blocking sections) ---
    # 1 horse-lane = 1/18 course-width (doc). `solver.lane` is stored in
    # course-width units throughout, matching the doc's primary unit; this
    # constant converts a horse-lane count into that unit where the doc
    # states a threshold in horse-lanes.
    HORSE_LANE = 1.0 / 18.0

    def _others(self, solver):
        for s in self.solvers:
            if s is not solver:
                yield s

    def front_blocker(self, solver):
        """Doc: front block = 0 < distGap < 2m AND |laneGap| <= a tolerance
        that shrinks from 0.75 horse-lanes at 0m to 0.3 at 2m. Lowest
        distance-gap qualifying horse wins. Returns (blocker, dist_gap) or
        None."""
        best = None
        for other in self._others(solver):
            dist_gap = other.pos - solver.pos
            if not (0 < dist_gap < 2.0):
                continue
            lane_gap = abs(other.lane - solver.lane)
            tolerance = (1.0 - 0.6 * dist_gap / 2.0) * 0.75 * self.HORSE_LANE
            if lane_gap <= tolerance and (best is None or dist_gap < best[1]):
                best = (other, dist_gap)
        return best

    def side_room(self, solver):
        """Doc: side block = |distGap| < 1.05m AND |laneGap| < 2 horse-lanes.
        Returns (inner_blocked, outer_blocked) -- inner = lower lane (toward
        the rail), outer = higher lane."""
        inner_blocked = outer_blocked = False
        for other in self._others(solver):
            if abs(other.pos - solver.pos) >= 1.05:
                continue
            lane_gap = other.lane - solver.lane
            if abs(lane_gap) >= 2.0 * self.HORSE_LANE:
                continue
            if lane_gap < 0:
                inner_blocked = True
            elif lane_gap > 0:
                outer_blocked = True
        return inner_blocked, outer_blocked

    def is_surrounded(self, solver) -> bool:
        """Doc: true if ALL THREE of Out/Front/Behind each find another
        horse (the same horse may satisfy more than one direction)."""
        out = front = behind = False
        for other in self._others(solver):
            dist_gap = other.pos - solver.pos
            lane_gap = other.lane - solver.lane
            if abs(dist_gap) < 1.5 and 0 < lane_gap < 3.0 * self.HORSE_LANE:
                out = True
            if 0 < dist_gap < 3.0 and abs(lane_gap) < 1.5 * self.HORSE_LANE:
                front = True
            if -3.0 < dist_gap < 0 and abs(lane_gap) < 1.5 * self.HORSE_LANE:
                behind = True
        return out and front and behind

    def is_solo_front_runner(self, solver) -> bool:
        return not any(strategy_matches(other.horse.strategy, Strategy.NIGE) for other in self._others(solver))

    def pacemaker(self):
        """Doc's pre-1.5-anniversary pacemaker rule (the current, post-1.5,
        mechanism is explicitly flagged as unclear even in the real game --
        using the last confirmed one): the 1st-place horse among the
        field's most-forward strategy group that's actually present."""
        groups = (
            lambda strat: strat in (Strategy.NIGE, Strategy.OONIGE),
            lambda strat: strat == Strategy.SENKOU,
            lambda strat: strat == Strategy.SASI,
            lambda strat: strat == Strategy.OIKOMI,
        )
        for match in groups:
            for i in self.order:  # self.order is sorted leader-first
                if match(self.solvers[i].horse.strategy):
                    return self.solvers[i]
        return None

    def gap_to_pacemaker(self, solver) -> float:
        pm = self.pacemaker()
        if pm is None or pm is solver:
            return 0.0
        return pm.pos - solver.pos

    def near_count(self, solver) -> int:
        """Doc: near = |distGap| < 3m AND |laneGap| < 3 horse-lanes
        (post-1st-anniversary thresholds; the doc notes these were tighter,
        1.5m/1.5 horse-lanes, before that)."""
        count = 0
        for other in self._others(solver):
            if abs(other.pos - solver.pos) < 3.0 and abs(other.lane - solver.lane) < 3.0 * self.HORSE_LANE:
                count += 1
        return count

    def near_infront_count(self, solver) -> int:
        """Doc: near_infront_count -- same "near" definition as near_count
        (|distGap|<3m, |laneGap|<3 horse-lanes) but restricted to horses
        AHEAD (distGap>0)."""
        count = 0
        for other in self._others(solver):
            dist_gap = other.pos - solver.pos
            if 0 < dist_gap < 3.0 and abs(other.lane - solver.lane) < 3.0 * self.HORSE_LANE:
                count += 1
        return count

    def temptation_count_behind(self, solver) -> int:
        """Doc: temptation_count_behind -- the number of horses ranked
        behind `solver` (by finishing-position rank, not physical distance)
        that are currently rushing (kakari)."""
        r = self.rank_of[self._index_of(solver)]
        return sum(1 for i in self.order[r:] if self.solvers[i].is_kakari)

    def temptation_count_infront(self, solver) -> int:
        """Doc: temptation_count_infront -- the number of horses ranked
        ahead of `solver` that are currently rushing (kakari)."""
        r = self.rank_of[self._index_of(solver)]
        return sum(1 for i in self.order[:r - 1] if self.solvers[i].is_kakari)

    def running_style_temptation_count(self, strategy) -> int:
        """Doc: running_style_temptation_count_{nige,senko,sashi,oikomi} --
        the number of horses in the field (the querying horse counts too)
        with the given running strategy that are currently rushing."""
        return sum(1 for s in self.solvers if strategy_matches(s.horse.strategy, strategy) and s.is_kakari)

    def step_all(self, dt: float, active_indices: Optional[list[int]] = None) -> None:
        """Step every (or only the still-racing `active_indices`) solver by
        `dt`, then refresh the shared order/rank snapshot. Solvers must all
        be stepped before `update()` runs so the snapshot reflects a single
        consistent instant, matching how the original per-horse lockstep
        loop in race_runner.py already worked for frame recording."""
        indices = active_indices if active_indices is not None else range(self.n)
        for i in indices:
            self.solvers[i].step(dt)
        self.update()
