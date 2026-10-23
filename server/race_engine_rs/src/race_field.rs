
use std::cell::{Cell, RefCell};

use crate::horse_types::{strategy_matches, Strategy};
use crate::race_solver::{ skill_target, Perspective, RaceSolver, SkillEffect};

pub const HORSE_LANE: f64 = 1.0 / 18.0;

const PACEMAKER_GROUPS: &[&[Strategy]] = &[&[Strategy::Nige, Strategy::Oonige], &[Strategy::Senkou], &[Strategy::Sasi], &[Strategy::Oikomi]];

pub struct RaceField {
    pub solvers: Vec<RefCell<RaceSolver>>,
    pub n: usize,
    strategies: Vec<Strategy>,
    rank_of: RefCell<Vec<usize>>,
    order: RefCell<Vec<usize>>,
    prev_rank_of: RefCell<Vec<usize>>,
    pacemaker_idx: Cell<Option<usize>>,
    pacemaker_valid: Cell<bool>,
    external: RefCell<Vec<(usize, SkillEffect, f64, String)>>,
    compete_top_done: RefCell<Vec<Strategy>>,
    advantage_prev: RefCell<Vec<(usize, Vec<i32>)>>,
}

impl RaceField {
    pub fn new(mut solvers: Vec<RaceSolver>) -> RaceField {
        let n = solvers.len();
        for (i, s) in solvers.iter_mut().enumerate() {
            s.field_index = Some(i);
        }
        let strategies = solvers.iter().map(|s| s.horse.strategy).collect();
        let field = RaceField {
            solvers: solvers.into_iter().map(RefCell::new).collect(),
            n,
            strategies,
            rank_of: RefCell::new(vec![0; n]),
            order: RefCell::new((0..n).collect()),
            prev_rank_of: RefCell::new(vec![0; n]),
            pacemaker_idx: Cell::new(None),
            pacemaker_valid: Cell::new(false),
            external: RefCell::new(Vec::new()),
            compete_top_done: RefCell::new(Vec::new()),
            advantage_prev: RefCell::new(Vec::new()),
        };
        field.update();
        for i in 0..n {
            let (deferred, pos, lane, chara_id, vision, team, cancel) = {
                let mut s = field.solvers[i].borrow_mut();
                (std::mem::take(&mut s.deferred_external), s.pos, s.lane, s.chara_id, s.visible_distance(), s.context.get("teamId").and_then(crate::json::Value::as_i64).unwrap_or(0), s.debuff_cancel)
            };
            for (ef, duration, skill_id, log_index) in deferred {
                let hit = field.queue_external_effect(i, pos, lane, chara_id, vision, team, cancel, &ef, duration, &skill_id);
                let mut s = field.solvers[i].borrow_mut();
                if let Some(a) = s.activation_log.get_mut(log_index) {
                    for t in hit {
                        if !a.targets.contains(&t) {
                            a.targets.push(t);
                        }
                    }
                }
            }
        }
        let mut boosted: Vec<usize> = field.external.borrow().iter().map(|e| e.0).collect();
        boosted.sort_unstable();
        boosted.dedup();
        field.apply_external_effects();
        for idx in boosted {
            field.solvers[idx].borrow_mut().refresh_derived_stats();
        }
        field
    }

    pub fn viewer_remain_distance(&self, i: usize) -> f64 {
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let s = self.solvers[j].borrow();
            if s.is_viewer {
                return s.course.distance - s.pos;
            }
        }
        f64::NAN
    }

    pub fn other_activated_advantage(&self, i: usize, ability_type: i32) -> bool {
        self.advantage_prev.borrow().iter().any(|(j, types)| *j != i && types.contains(&ability_type))
    }

    pub fn update(&self) {
        let mut prev = self.prev_rank_of.borrow_mut();
        let rank = self.rank_of.borrow();
        prev.copy_from_slice(&rank);
        drop(rank);
        self.pacemaker_valid.set(false);

        let positions: Vec<f64> = self.solvers.iter().map(|s| s.borrow().pos).collect();
        let mut idx: Vec<usize> = (0..self.n).collect();
        idx.sort_by(|&a, &b| positions[b].partial_cmp(&positions[a]).unwrap());
        let mut rank_of = vec![0usize; self.n];
        for (rank, &i) in idx.iter().enumerate() {
            rank_of[i] = rank + 1;
        }
        *self.rank_of.borrow_mut() = rank_of;
        *self.order.borrow_mut() = idx;
    }

    pub fn step_all(&self, dt: f64, active_indices: &[usize]) {
        for &i in active_indices {
            self.solvers[i].borrow_mut().step(dt, Some(self));
        }
        self.apply_external_effects();
        self.resolve_overlaps();
        self.update();
        self.update_compete_top();
        let mut prev = Vec::new();
        for j in 0..self.n {
            let mut s = self.solvers[j].borrow_mut();
            if !s.advantage_types.is_empty() {
                prev.push((j, std::mem::take(&mut s.advantage_types)));
            }
        }
        *self.advantage_prev.borrow_mut() = prev;
    }

    fn running_others(&self, i: usize) -> impl Iterator<Item = (usize, std::cell::Ref<'_, RaceSolver>)> + '_ {
        (0..self.n).filter(move |&j| j != i).map(move |j| (j, self.solvers[j].borrow())).filter(|(_, s)| !s.finished)
    }


    pub fn select_targets(&self, i: usize, pos: f64, lane: f64, chara_id: i32, vision: f64, team: i64, target_type: i32, target_value: i32) -> Vec<usize> {
        if target_type == skill_target::UMA_ID {
            return (0..self.n)
                .filter(|&j| if j == i { chara_id == target_value } else {
                    let s = self.solvers[j].borrow();
                    !s.finished && s.chara_id == target_value
                })
                .collect();
        }
        let others: Vec<(usize, f64, f64, bool, Strategy)> = self
            .running_others(i)
            .map(|(j, s)| (j, s.pos, s.lane, s.is_kakari, s.horse.strategy))
            .collect();
        let take = target_value.max(0) as usize;
        let nearest = |mut v: Vec<(usize, f64)>| -> Vec<usize> {
            v.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap());
            v.into_iter().take(take).map(|(j, _)| j).collect()
        };
        match target_type {
            skill_target::ALL => std::iter::once(i).chain(others.iter().map(|o| o.0)).collect(),
            3 => others.iter().map(|o| o.0).collect(),
            skill_target::ALL_ALLIES => others.iter().filter(|o| team != 0 && self.solvers[o.0].borrow().context.get("teamId").and_then(crate::json::Value::as_i64).unwrap_or(0) == team).map(|o| o.0).collect(),
            skill_target::IN_FOV => nearest(others.iter().filter(|o| o.1 >= pos && o.1 - pos <= vision && (o.2 - lane).abs() <= (((o.1 - pos)/vision.max(0.001))*11.5*HORSE_LANE+2.0*HORSE_LANE)/2.0).map(|o| (o.0, o.1 - pos)).collect()),
            skill_target::AHEAD_OF_SELF => nearest(others.iter().filter(|o| o.1 > pos).map(|o| (o.0, o.1 - pos)).collect()),
            skill_target::BEHIND_SELF => nearest(others.iter().filter(|o| o.1 < pos).map(|o| (o.0, pos - o.1)).collect()),
            skill_target::ENEMY_STRATEGY => match Strategy::from_i32(target_value) {
                Ok(st) => others.iter().filter(|o| strategy_matches(o.4, st)).map(|o| o.0).collect(),
                Err(_) => Vec::new(),
            },
            skill_target::KAKARI_AHEAD => nearest(others.iter().filter(|o| o.3 && o.1 > pos).map(|o| (o.0, o.1 - pos)).collect()),
            skill_target::KAKARI_BEHIND => nearest(others.iter().filter(|o| o.3 && o.1 < pos).map(|o| (o.0, pos - o.1)).collect()),
            skill_target::KAKARI_STRATEGY => others.iter().filter(|o| o.3 && o.4 as i32 == target_value).map(|o| o.0).collect(),
            skill_target::AHEAD_OF_POSITION => nearest(others.iter().filter(|o| o.1 > pos).map(|o| (o.0, o.1 - pos)).collect()),
            skill_target::USED_RECOVERY => {
                let prev = self.advantage_prev.borrow();
                others
                    .iter()
                    .filter(|o| prev.iter().any(|(j, types)| *j == o.0 && types.contains(&(crate::race_solver::SkillType::Recovery as i32))))
                    .map(|o| o.0)
                    .take(take)
                    .collect()
            }
            _ => Vec::new(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn queue_external_effect(&self, i: usize, pos: f64, lane: f64, chara_id: i32, vision: f64, team: i64, cancel: bool, effect: &SkillEffect, duration: f64, skill_id: &str) -> Vec<usize> {
        let targets: Vec<usize> = self.select_targets(i, pos, lane, chara_id, vision, team, effect.target, effect.target_value).into_iter().filter(|&t| !(effect.modifier < 0.0 && if t == i { cancel } else { self.solvers[t].borrow().debuff_cancel })).collect();
        let mut ext = self.external.borrow_mut();
        for &t in &targets {
            ext.push((t, *effect, duration, skill_id.to_string()));
        }
        targets
    }

    fn apply_external_effects(&self) {
        let pending = std::mem::take(&mut *self.external.borrow_mut());
        for (idx, effect, duration, skill_id) in pending {
            let mut target = self.solvers[idx].borrow_mut();
            if !target.finished && !(target.debuff_cancel && effect.modifier < 0.0) {
                target.apply_effect(&effect, duration, &skill_id, Perspective::Other, Some(self));
            }
        }
    }


    fn update_compete_top(&self) {
        for group in [Strategy::Nige, Strategy::Oonige] {
            let active: Vec<usize> = (0..self.n)
                .filter(|&j| {
                    let s = self.solvers[j].borrow();
                    s.compete_top_active && s.horse.strategy == group
                })
                .collect();
            for &a in &active {
                let (finished, pos, lane) = {
                    let s = self.solvers[a].borrow();
                    (s.finished, s.pos, s.lane)
                };
                if finished {
                    self.solvers[a].borrow_mut().end_compete_top();
                    continue;
                }
                let keep = active.iter().any(|&o| {
                    if o == a {
                        return false;
                    }
                    let s = self.solvers[o].borrow();
                    s.compete_top_active && s.pos - pos <= 5.0 && (s.lane - lane).abs() <= 0.416
                });
                if !keep {
                    self.solvers[a].borrow_mut().end_compete_top();
                }
            }
        }
        if self.n == 0 || self.solvers[self.order.borrow()[0]].borrow().pos < 150.0 {
            return;
        }
        for group in [Strategy::Nige, Strategy::Oonige] {
            if self.compete_top_done.borrow().contains(&group) {
                continue;
            }
            let members: Vec<(usize, f64, f64, f64)> = (0..self.n)
                .filter_map(|j| {
                    let s = self.solvers[j].borrow();
                    (s.horse.strategy == group && !s.finished).then(|| (j, s.pos, s.lane, s.course.distance / 24.0))
                })
                .collect();
            if members.len() < 2 {
                continue;
            }
            let mut front = members[0];
            for m in &members {
                if m.1 > front.1 {
                    front = *m;
                }
            }
            let cands: Vec<(usize, f64, f64, f64)> = members.iter().copied().filter(|m| front.1 - m.1 < 3.75 && (front.2 - m.2).abs() < 0.165).collect();
            if cands.len() < 2 || !cands.iter().any(|m| m.1 < m.3 * 5.0) {
                continue;
            }
            self.compete_top_done.borrow_mut().push(group);
            for m in &cands {
                self.solvers[m.0].borrow_mut().start_compete_top();
            }
        }
    }


    fn resolve_overlaps(&self) {
        let running: Vec<usize> = (0..self.n).filter(|&j| !self.solvers[j].borrow().finished).collect();
        for ai in 0..running.len() {
            for &b in &running[ai + 1..] {
                let a = running[ai];
                let (a_pos, a_lane) = {
                    let s = self.solvers[a].borrow();
                    (s.pos, s.lane)
                };
                let (b_pos, b_lane) = {
                    let s = self.solvers[b].borrow();
                    (s.pos, s.lane)
                };
                if (a_pos - b_pos).abs() < 0.4 && (a_lane - b_lane).abs() < 0.4 * HORSE_LANE {
                    let outer = if b_lane > a_lane || (b_lane == a_lane && b > a) { b } else { a };
                    let mut o = self.solvers[outer].borrow_mut();
                    o.lane = o.course.max_lane.min(o.lane + 0.4 * HORSE_LANE);
                    o.lane_retarget = true;
                }
            }
        }
    }

    pub fn order_of(&self, i: usize) -> usize {
        self.rank_of.borrow()[i]
    }

    pub fn previous_order_of(&self, i: usize) -> usize {
        self.prev_rank_of.borrow()[i]
    }

    pub fn distance_to_leader(&self, i: usize, my_pos: f64) -> f64 {
        let leader = self.order.borrow()[0];
        let leader_pos = if leader == i { my_pos } else { self.solvers[leader].borrow().pos };
        (leader_pos - my_pos).max(0.0)
    }

    pub fn distance_to_ahead(&self, i: usize, my_pos: f64) -> Option<f64> {
        let r = self.rank_of.borrow()[i];
        if r <= 1 {
            return None;
        }
        let ahead = self.order.borrow()[r - 2];
        Some((self.solvers[ahead].borrow().pos - my_pos).max(0.0))
    }

    pub fn distance_to_behind(&self, i: usize, my_pos: f64) -> Option<f64> {
        let r = self.rank_of.borrow()[i];
        if r >= self.n {
            return None;
        }
        let behind = self.order.borrow()[r];
        Some((my_pos - self.solvers[behind].borrow().pos).max(0.0))
    }

    pub fn lane_gap_to_ahead(&self, i: usize, my_lane: f64) -> Option<f64> {
        let r = self.rank_of.borrow()[i];
        if r <= 1 {
            return None;
        }
        let ahead = self.order.borrow()[r - 2];
        Some(self.solvers[ahead].borrow().lane - my_lane)
    }

    pub fn lane_gap_to_behind(&self, i: usize, my_lane: f64) -> Option<f64> {
        let r = self.rank_of.borrow()[i];
        if r >= self.n {
            return None;
        }
        let behind = self.order.borrow()[r];
        Some(my_lane - self.solvers[behind].borrow().lane)
    }

    pub fn did_overtake(&self, i: usize) -> bool {
        self.rank_of.borrow()[i] < self.prev_rank_of.borrow()[i]
    }

    pub fn front_blocker(&self, i: usize, pos: f64, lane: f64) -> Option<(usize, f64)> {
        let mut best: Option<(usize, f64)> = None;
        for (j, other) in self.running_others(i) {
            let dist_gap = other.pos - pos;
            if !(dist_gap > 0.0 && dist_gap < 2.0) {
                continue;
            }
            let lane_gap = (other.lane - lane).abs();
            let tolerance = (1.0 - 0.6 * dist_gap / 2.0) * 0.75 * HORSE_LANE;
            if lane_gap <= tolerance && best.map_or(true, |(_, bd)| dist_gap < bd) {
                best = Some((j, dist_gap));
            }
        }
        best
    }

    pub fn side_room(&self, i: usize, pos: f64, lane: f64) -> (bool, bool) {
        let mut inner_blocked = false;
        let mut outer_blocked = false;
        let lane_tol = 2.0 * HORSE_LANE;
        for (_, other) in self.running_others(i) {
            let d = other.pos - pos;
            if !(-1.05 < d && d < 1.05) {
                continue;
            }
            let lane_gap = other.lane - lane;
            if !(-lane_tol < lane_gap && lane_gap < lane_tol) {
                continue;
            }
            if lane_gap < 0.0 {
                inner_blocked = true;
                if outer_blocked {
                    break;
                }
            } else if lane_gap > 0.0 {
                outer_blocked = true;
                if inner_blocked {
                    break;
                }
            }
        }
        (inner_blocked, outer_blocked)
    }

    pub fn is_surrounded(&self, i: usize, pos: f64, lane: f64) -> bool {
        let mut out = false;
        let mut front = false;
        let mut behind = false;
        for (_, other) in self.running_others(i) {
            let dist_gap = other.pos - pos;
            let lane_gap = other.lane - lane;
            if dist_gap.abs() < 1.5 && lane_gap > 0.0 && lane_gap < 3.0 * HORSE_LANE {
                out = true;
            }
            if dist_gap > 0.0 && dist_gap < 3.0 && lane_gap.abs() < 1.5 * HORSE_LANE {
                front = true;
            }
            if dist_gap > -3.0 && dist_gap < 0.0 && lane_gap.abs() < 1.5 * HORSE_LANE {
                behind = true;
            }
            if out && front && behind {
                break;
            }
        }
        out && front && behind
    }

    pub fn is_solo_front_runner(&self, i: usize) -> bool {
        !self.running_others(i).any(|(j, _)| strategy_matches(self.strategies[j], Strategy::Nige))
    }

    pub fn pacemaker(&self) -> Option<usize> {
        if self.pacemaker_valid.get() {
            return self.pacemaker_idx.get();
        }
        let order = self.order.borrow();
        let mut result = None;
        for group in PACEMAKER_GROUPS {
            for &i in order.iter() {
                if group.contains(&self.strategies[i]) {
                    result = Some(i);
                    break;
                }
            }
            if result.is_some() {
                break;
            }
        }
        self.pacemaker_idx.set(result);
        self.pacemaker_valid.set(true);
        result
    }

    pub fn gap_to_pacemaker(&self, i: usize, my_pos: f64) -> f64 {
        match self.pacemaker() {
            Some(pm) if pm != i => self.solvers[pm].borrow().pos - my_pos,
            _ => 0.0,
        }
    }

    pub fn near_count(&self, i: usize, pos: f64, lane: f64) -> i32 {
        let lane_tol = 3.0 * HORSE_LANE;
        let mut count = 0;
        for (_, other) in self.running_others(i) {
            if (other.pos - pos).abs() < 3.0 && (other.lane - lane).abs() < lane_tol {
                count += 1;
            }
        }
        count
    }

    pub fn near_infront_count(&self, i: usize, pos: f64, lane: f64) -> i32 {
        let lane_tol = 3.0 * HORSE_LANE;
        let mut count = 0;
        for (_, other) in self.running_others(i) {
            let dist_gap = other.pos - pos;
            if dist_gap > 0.0 && dist_gap < 3.0 && (other.lane - lane).abs() < lane_tol {
                count += 1;
            }
        }
        count
    }

    pub fn temptation_count_behind(&self, i: usize) -> i32 {
        let r = self.rank_of.borrow()[i];
        let order = self.order.borrow();
        order[r..].iter().filter(|&&j| self.solvers[j].borrow().is_kakari).count() as i32
    }

    pub fn temptation_count_infront(&self, i: usize) -> i32 {
        let r = self.rank_of.borrow()[i];
        let order = self.order.borrow();
        order[..r - 1].iter().filter(|&&j| self.solvers[j].borrow().is_kakari).count() as i32
    }

    pub fn activated_any_advantage(&self, index: usize) -> bool {
        self.advantage_prev.borrow().iter().any(|(i,types)|*i==index && !types.is_empty())
    }

    pub fn running_style_temptation_count(&self, strategy: Strategy, self_idx: usize, self_is_kakari: bool) -> i32 {
        let others = (0..self.n)
            .filter(|&j| j != self_idx && strategy_matches(self.strategies[j], strategy) && self.solvers[j].borrow().is_kakari)
            .count() as i32;
        let self_counts = strategy_matches(self.strategies[self_idx], strategy) && self_is_kakari;
        others + self_counts as i32
    }
}
