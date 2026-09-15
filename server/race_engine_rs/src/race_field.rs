//! Port of race_field.py -- multi-horse field coordination. Not part of the
//! original TS engine (which runs every RaceSolver in isolation, oblivious
//! to other horses).
//!
//! `RaceField` owns every solver in `RefCell`s so one can be borrowed
//! mutably (to step it) while its own methods borrow the others
//! immutably to answer blocking/lane/order queries -- the same access
//! pattern Python gets for free via shared references. Solvers do not hold
//! a reference back to the field: instead `field: Option<&RaceField>` is
//! threaded as an explicit parameter through `RaceSolver::step` and every
//! condition-checking closure (see `activation_conditions::DynCond`), so
//! nothing here needs a self-referential pointer or unsafe code.
//!
//! Ported already carrying the perf fixes validated on the Python engine:
//! each solver's own roster index is stored on it directly (`field_index`)
//! rather than found by a linear scan, `side_room`/`is_surrounded` early-out
//! once both/all flags are set, and `pacemaker()` is computed once per tick
//! and cached rather than recomputed per asking horse.

use std::cell::{Cell, RefCell};

use crate::horse_types::{strategy_matches, Strategy};
use crate::race_solver::RaceSolver;

/// 1 horse-lane = 1/18 course-width (doc). `lane` fields elsewhere are
/// stored in course-width units; this converts a horse-lane count into
/// that unit wherever the doc states a threshold in horse-lanes.
pub const HORSE_LANE: f64 = 1.0 / 18.0;

/// Doc's pacemaker strategy groups, most-forward first.
const PACEMAKER_GROUPS: &[&[Strategy]] = &[&[Strategy::Nige, Strategy::Oonige], &[Strategy::Senkou], &[Strategy::Sasi], &[Strategy::Oikomi]];

pub struct RaceField {
    pub solvers: Vec<RefCell<RaceSolver>>,
    pub n: usize,
    /// Each solver's fixed strategy, mirrored outside its `RefCell` so
    /// `pacemaker()` can scan every horse's strategy -- including the one
    /// currently mid-`step()` and thus already mutably borrowed -- without
    /// re-entering that `RefCell`.
    strategies: Vec<Strategy>,
    rank_of: RefCell<Vec<usize>>,
    order: RefCell<Vec<usize>>,
    prev_rank_of: RefCell<Vec<usize>>,
    pacemaker_idx: Cell<Option<usize>>,
    pacemaker_valid: Cell<bool>,
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
        };
        field.update();
        field
    }

    /// Recompute live order/rank from each solver's current `pos`. Call
    /// once after every horse has been stepped for this tick.
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

    /// Step every (or only the still-racing `active_indices`) solver by
    /// `dt`, then refresh the shared order/rank snapshot. Solvers must all
    /// be stepped before `update()` runs so the snapshot reflects a single
    /// consistent instant.
    pub fn step_all(&self, dt: f64, active_indices: &[usize]) {
        for &i in active_indices {
            self.solvers[i].borrow_mut().step(dt, Some(self));
        }
        self.update();
    }

    pub fn order_of(&self, i: usize) -> usize {
        self.rank_of.borrow()[i]
    }

    pub fn previous_order_of(&self, i: usize) -> usize {
        self.prev_rank_of.borrow()[i]
    }

    /// `my_pos`/`my_lane` below are the querying horse `i`'s own current
    /// values, passed in by the caller instead of re-borrowed here -- `i`
    /// may be the solver currently mid-`step()` and thus already mutably
    /// borrowed via its `RefCell`, so this must never index `self.solvers[i]`.
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

    /// True on the tick this solver's rank improved (took over at least one
    /// other horse) versus the previous tick.
    pub fn did_overtake(&self, i: usize) -> bool {
        self.rank_of.borrow()[i] < self.prev_rank_of.borrow()[i]
    }

    /// Doc: front block = 0 < distGap < 2m AND |laneGap| <= a tolerance that
    /// shrinks from 0.75 horse-lanes at 0m to 0.3 at 2m. Lowest
    /// distance-gap qualifying horse wins. Returns (other_index, dist_gap).
    pub fn front_blocker(&self, i: usize, pos: f64, lane: f64) -> Option<(usize, f64)> {
        let mut best: Option<(usize, f64)> = None;
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let other = self.solvers[j].borrow();
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

    /// Doc: side block = |distGap| < 1.05m AND |laneGap| < 2 horse-lanes.
    /// Returns (inner_blocked, outer_blocked) -- inner = lower lane (toward
    /// the rail), outer = higher lane.
    pub fn side_room(&self, i: usize, pos: f64, lane: f64) -> (bool, bool) {
        let mut inner_blocked = false;
        let mut outer_blocked = false;
        let lane_tol = 2.0 * HORSE_LANE;
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let other = self.solvers[j].borrow();
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

    /// Doc: true if ALL THREE of Out/Front/Behind each find another horse
    /// (the same horse may satisfy more than one direction).
    pub fn is_surrounded(&self, i: usize, pos: f64, lane: f64) -> bool {
        let mut out = false;
        let mut front = false;
        let mut behind = false;
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let other = self.solvers[j].borrow();
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
        for j in 0..self.n {
            if j == i {
                continue;
            }
            if strategy_matches(self.strategies[j], Strategy::Nige) {
                return false;
            }
        }
        true
    }

    /// Doc's pre-1.5-anniversary pacemaker rule: the 1st-place horse among
    /// the field's most-forward strategy group that's actually present.
    /// Depends only on `order` (refreshed once per tick by `update`) and
    /// solvers' fixed strategies, so it is computed once per tick and
    /// cached rather than recomputed per asking horse.
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

    /// Doc: near = |distGap| < 3m AND |laneGap| < 3 horse-lanes.
    pub fn near_count(&self, i: usize, pos: f64, lane: f64) -> i32 {
        let lane_tol = 3.0 * HORSE_LANE;
        let mut count = 0;
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let other = self.solvers[j].borrow();
            if (other.pos - pos).abs() < 3.0 && (other.lane - lane).abs() < lane_tol {
                count += 1;
            }
        }
        count
    }

    /// Doc: near_infront_count -- same "near" definition as near_count
    /// (|distGap|<3m, |laneGap|<3 horse-lanes) but restricted to horses AHEAD.
    pub fn near_infront_count(&self, i: usize, pos: f64, lane: f64) -> i32 {
        let lane_tol = 3.0 * HORSE_LANE;
        let mut count = 0;
        for j in 0..self.n {
            if j == i {
                continue;
            }
            let other = self.solvers[j].borrow();
            let dist_gap = other.pos - pos;
            if dist_gap > 0.0 && dist_gap < 3.0 && (other.lane - lane).abs() < lane_tol {
                count += 1;
            }
        }
        count
    }

    /// Doc: temptation_count_behind -- the number of horses ranked behind
    /// `i` (by finishing-position rank, not physical distance) that are
    /// currently rushing (kakari).
    pub fn temptation_count_behind(&self, i: usize) -> i32 {
        let r = self.rank_of.borrow()[i];
        let order = self.order.borrow();
        order[r..].iter().filter(|&&j| self.solvers[j].borrow().is_kakari).count() as i32
    }

    /// Doc: temptation_count_infront -- the number of horses ranked ahead
    /// of `i` that are currently rushing (kakari).
    pub fn temptation_count_infront(&self, i: usize) -> i32 {
        let r = self.rank_of.borrow()[i];
        let order = self.order.borrow();
        order[..r - 1].iter().filter(|&&j| self.solvers[j].borrow().is_kakari).count() as i32
    }

    /// Doc: running_style_temptation_count_{nige,senko,sashi,oikomi} -- the
    /// number of horses in the field (the querying horse counts too) with
    /// the given running strategy that are currently rushing. `self_idx`/
    /// `self_is_kakari` come from the caller directly (rather than
    /// re-borrowing `self.solvers[self_idx]`) since this is called from a
    /// condition check while that very solver is already mutably borrowed
    /// mid-`step()`.
    pub fn running_style_temptation_count(&self, strategy: Strategy, self_idx: usize, self_is_kakari: bool) -> i32 {
        let others = (0..self.n)
            .filter(|&j| j != self_idx && strategy_matches(self.strategies[j], strategy) && self.solvers[j].borrow().is_kakari)
            .count() as i32;
        let self_counts = strategy_matches(self.strategies[self_idx], strategy) && self_is_kakari;
        others + self_counts as i32
    }
}
