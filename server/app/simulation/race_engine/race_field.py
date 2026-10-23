
from __future__ import annotations

from typing import Optional

from .horse_types import Strategy, strategy_matches


class RaceField:
    def __init__(self, solvers: list):
        self.solvers = solvers
        self.n = len(solvers)
        self.rank_of: list[int] = [0] * self.n
        self.order: list[int] = list(range(self.n))
        self._prev_rank_of: list[int] = list(self.rank_of)
        self._external: list[tuple] = []
        self._compete_top_done: set[int] = set()
        self._advantage_prev: dict[int, set] = {}
        for i, s in enumerate(solvers):
            s.field = self
            s.field_index = i
        self.update()
        for s in solvers:
            for ef, duration, skill_id, targets in s.deferred_external:
                for t in self.queue_external_effect(s, ef, duration, skill_id):
                    if t not in targets:
                        targets.append(t)
            s.deferred_external = []
        boosted = {idx for idx, *_ in self._external}
        self._apply_external_effects()
        for idx in sorted(boosted):
            self.solvers[idx].refresh_derived_stats()

    def update(self) -> None:
        self._prev_rank_of = self.rank_of
        idx = sorted(range(self.n), key=lambda i: -self.solvers[i].pos)
        self.order = idx
        rank_of = [0] * self.n
        for rank, i in enumerate(idx):
            rank_of[i] = rank + 1
        self.rank_of = rank_of

    def _index_of(self, solver) -> int:
        return solver.field_index

    def order_of(self, solver) -> int:
        return self.rank_of[self._index_of(solver)]

    def previous_order_of(self, solver) -> int:
        return self._prev_rank_of[self._index_of(solver)]

    def leader_pos(self) -> float:
        return self.solvers[self.order[0]].pos

    def distance_to_leader(self, solver) -> float:
        return max(0.0, self.leader_pos() - solver.pos)

    def distance_to_ahead(self, solver) -> Optional[float]:
        i = self._index_of(solver)
        r = self.rank_of[i]
        if r <= 1:
            return None
        ahead = self.order[r - 2]
        return max(0.0, self.solvers[ahead].pos - solver.pos)

    def distance_to_behind(self, solver) -> Optional[float]:
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

    def viewer_remain_distance(self) -> float:
        for s in self.solvers:
            if s.is_viewer:
                return s.course.distance - s.pos
        return float("nan")

    def other_activated_advantage(self, index: int, ability_type: int) -> bool:
        return any(ability_type in types for i, types in self._advantage_prev.items() if i != index)

    def did_overtake(self, solver) -> bool:
        i = self._index_of(solver)
        return self.rank_of[i] < self._prev_rank_of[i]

    HORSE_LANE = 1.0 / 18.0

    def _others(self, solver):
        for s in self.solvers:
            if s is not solver and not s.finished:
                yield s

    def front_blocker(self, solver):
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
        groups = (
            lambda strat: strat in (Strategy.NIGE, Strategy.OONIGE),
            lambda strat: strat == Strategy.SENKOU,
            lambda strat: strat == Strategy.SASI,
            lambda strat: strat == Strategy.OIKOMI,
        )
        for match in groups:
            for i in self.order:
                if match(self.solvers[i].horse.strategy):
                    return self.solvers[i]
        return None

    def gap_to_pacemaker(self, solver) -> float:
        pm = self.pacemaker()
        if pm is None or pm is solver:
            return 0.0
        return pm.pos - solver.pos

    def near_count(self, solver) -> int:
        count = 0
        for other in self._others(solver):
            if abs(other.pos - solver.pos) < 3.0 and abs(other.lane - solver.lane) < 3.0 * self.HORSE_LANE:
                count += 1
        return count

    def near_infront_count(self, solver) -> int:
        count = 0
        for other in self._others(solver):
            dist_gap = other.pos - solver.pos
            if 0 < dist_gap < 3.0 and abs(other.lane - solver.lane) < 3.0 * self.HORSE_LANE:
                count += 1
        return count

    def temptation_count_behind(self, solver) -> int:
        r = self.rank_of[self._index_of(solver)]
        return sum(1 for i in self.order[r:] if self.solvers[i].is_kakari)

    def temptation_count_infront(self, solver) -> int:
        r = self.rank_of[self._index_of(solver)]
        return sum(1 for i in self.order[:r - 1] if self.solvers[i].is_kakari)

    def running_style_temptation_count(self, strategy, source=None) -> int:
        return sum(1 for s in self.solvers if strategy_matches(s.horse.strategy, strategy) and s.is_kakari
                   and (source is None or self.is_opponent(source, s)))


    @staticmethod
    def is_opponent(source, other):
        if source is other:
            return False
        team = source.context.get("teamId") or 0
        return not team or team != other.context.get("teamId")

    def select_targets(self, source, target_type: int, target_value: int) -> list:
        from .race_solver import SkillTarget, SkillType
        others = [s for s in self._others(source)]
        if target_type == SkillTarget.ALL:
            return [source] + others
        if target_type == SkillTarget.ALL_OTHER:
            return others
        if target_type == SkillTarget.ALL_ALLIES:
            team = source.context.get("teamId") or 0
            return [s for s in others if team and s.context.get("teamId") == team]
        if target_type == SkillTarget.IN_FOV:
            seen = sorted((s for s in others if source._is_visible(s)), key=lambda s: s.pos - source.pos)
            return seen[:target_value]
        if target_type == SkillTarget.AHEAD_OF_SELF:
            ahead = sorted((s for s in others if s.pos > source.pos), key=lambda s: s.pos - source.pos)
            return ahead[:target_value]
        if target_type == SkillTarget.BEHIND_SELF:
            behind = sorted((s for s in others if s.pos < source.pos), key=lambda s: source.pos - s.pos)
            return behind[:target_value]
        if target_type == SkillTarget.ENEMY_STRATEGY:
            return [s for s in others if strategy_matches(s.horse.strategy, target_value)]
        if target_type == SkillTarget.KAKARI_AHEAD:
            ahead = sorted((s for s in others if s.is_kakari and s.pos > source.pos), key=lambda s: s.pos - source.pos)
            return ahead[:target_value]
        if target_type == SkillTarget.KAKARI_BEHIND:
            behind = sorted((s for s in others if s.is_kakari and s.pos < source.pos), key=lambda s: source.pos - s.pos)
            return behind[:target_value]
        if target_type == SkillTarget.KAKARI_STRATEGY:
            return [s for s in others if s.is_kakari and s.horse.strategy == target_value]
        if target_type == SkillTarget.AHEAD_OF_POSITION:
            ahead = sorted((s for s in others if s.pos > source.pos), key=lambda s: s.pos - source.pos)
            return ahead[:target_value]
        if target_type == SkillTarget.UMA_ID:
            return [s for s in self.solvers if not s.finished and s.chara_id == target_value]
        if target_type == SkillTarget.USED_RECOVERY:
            healers = [s for s in others if SkillType.RECOVERY in self._advantage_prev.get(s.field_index, ())]
            return healers[:target_value]
        return []

    def queue_external_effect(self, source, effect, duration: float, skill_id: str) -> list[int]:
        targets = [i for i in self.select_targets(source, effect.target, effect.target_value)
                   if not (i.debuff_cancel and effect.modifier < 0)]
        for t in targets:
            self._external.append((t.field_index, effect, duration, skill_id))
        return [t.field_index for t in targets]

    def _apply_external_effects(self) -> None:
        from .race_solver import Perspective
        pending, self._external = self._external, []
        for idx, effect, duration, skill_id in pending:
            target = self.solvers[idx]
            if not target.finished and not (target.debuff_cancel and effect.modifier < 0):
                target.apply_effect(effect, duration, skill_id, Perspective.OTHER)


    def _update_compete_top(self) -> None:
        for group in (Strategy.NIGE, Strategy.OONIGE):
            active = [s for s in self.solvers if s.compete_top_active and s.horse.strategy == group]
            for s in active:
                if s.finished:
                    s.end_compete_top()
                    continue
                if not any(o is not s and o.compete_top_active and o.pos - s.pos <= 5.0
                           and abs(o.lane - s.lane) <= 0.416 for o in active):
                    s.end_compete_top()
        if not self.n or self.solvers[self.order[0]].pos < 150.0:
            return
        for group in (Strategy.NIGE, Strategy.OONIGE):
            if group in self._compete_top_done:
                continue
            members = [s for s in self.solvers if s.horse.strategy == group and not s.finished]
            if len(members) < 2:
                continue
            front = members[0]
            for m in members:
                if m.pos > front.pos:
                    front = m
            cands = [m for m in members
                     if front.pos - m.pos < 3.75 and abs(front.lane - m.lane) < 0.165]
            if len(cands) < 2 or not any(m.pos < m.section_length * 5.0 for m in cands):
                continue
            self._compete_top_done.add(group)
            for m in cands:
                m.start_compete_top()


    def _resolve_overlaps(self) -> None:
        running = [s for s in self.solvers if not s.finished]
        for a_i in range(len(running)):
            a = running[a_i]
            for b in running[a_i + 1:]:
                if abs(a.pos - b.pos) < 0.4 and abs(a.lane - b.lane) < 0.4 * self.HORSE_LANE:
                    outer = b if (b.lane > a.lane or (b.lane == a.lane and b.field_index > a.field_index)) else a
                    outer.lane = min(outer.course.max_lane, outer.lane + 0.4 * self.HORSE_LANE)
                    outer._lane_retarget = True

    def step_all(self, dt: float, active_indices: Optional[list[int]] = None) -> None:
        indices = active_indices if active_indices is not None else range(self.n)
        for i in indices:
            self.solvers[i].step(dt)
        self._apply_external_effects()
        self._resolve_overlaps()
        self.update()
        self._update_compete_top()
        self._advantage_prev = {}
        for s in self.solvers:
            if s.advantage_types:
                self._advantage_prev[s.field_index] = s.advantage_types
                s.advantage_types = set()
