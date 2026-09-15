//! Port of hp_policy.py.
//!
//! Python's `RaceSolver.hp` is a duck-typed field holding either policy and
//! `self.hp.tick(self, dt)` passes the *whole solver* in. That aliases
//! `self` (immutable read) with `self.hp` (mutable) at the same time, which
//! Rust's borrow checker forbids when `hp` is itself a field of the solver.
//! Every method here that needs live solver state takes a small `Copy`
//! context struct of just the fields it reads instead of a solver
//! reference, which sidesteps the aliasing entirely and needs no unsafe.

use crate::course_data::{phase_start, CourseData};
use crate::horse_types::HorseParameters;
use crate::random_gen::Rule30CARng;

const HP_STRATEGY_COEFFICIENT: [f64; 6] = [0.0, 0.95, 0.89, 1.0, 0.995, 0.86];
// [surface][ground]; index 0 unused (surfaces are 1-indexed), mirroring the
// Python port's ragged nested list.
const HP_CONSUMPTION_GROUND_MODIFIER: [[f64; 5]; 3] = [
    [0.0, 0.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 1.0, 1.02, 1.02],
    [0.0, 1.0, 1.0, 1.01, 1.02],
];

#[derive(Clone, Copy)]
pub struct HpTickCtx {
    pub is_pace_down: bool,
    pub is_downhill_mode: bool,
    pub is_kakari: bool,
    pub phase: i32,
    pub current_speed: f64,
}

pub struct GameHpState {
    distance: f64,
    base_speed: f64,
    ground_modifier: f64,
    rng: Rule30CARng,
    pub max_hp: f64,
    pub hp: f64,
    guts_modifier: f64,
    subpar_accept_chance: i64,
}

impl GameHpState {
    pub fn new(course: &CourseData, ground: i32, rng: Rule30CARng) -> Self {
        GameHpState {
            distance: course.distance,
            base_speed: 20.0 - (course.distance - 2000.0) / 1000.0,
            ground_modifier: HP_CONSUMPTION_GROUND_MODIFIER[course.surface as usize][ground as usize],
            rng,
            // Placeholder until init(): the first round of skill activations
            // happens before init() is called, but some conditions access HP
            // methods during that round (e.g. is_hp_empty_onetime), so this
            // must be "initialized enough" not to blow up.
            max_hp: 1.0,
            hp: 1.0,
            guts_modifier: 1.0,
            subpar_accept_chance: 0,
        }
    }

    pub fn init(&mut self, horse: &HorseParameters) {
        self.max_hp = 0.8 * HP_STRATEGY_COEFFICIENT[horse.strategy as usize] * horse.stamina + self.distance;
        self.hp = self.max_hp;
        self.guts_modifier = 1.0 + 200.0 / (600.0 * horse.guts).sqrt();
        self.subpar_accept_chance = ((15.0 + 0.05 * horse.wisdom) * 1000.0).round() as i64;
    }

    fn get_status_modifier(ctx: HpTickCtx) -> f64 {
        let mut modifier = 1.0;
        if ctx.is_pace_down {
            modifier *= 0.6;
        }
        if ctx.is_downhill_mode {
            modifier *= 0.4;
        }
        if ctx.is_kakari {
            modifier *= 1.6;
        }
        modifier
    }

    fn hp_per_second(&self, ctx: HpTickCtx, velocity: f64) -> f64 {
        let guts_modifier = if ctx.phase >= 2 { self.guts_modifier } else { 1.0 };
        20.0 * (velocity - self.base_speed + 12.0).powi(2) / 144.0
            * Self::get_status_modifier(ctx)
            * self.ground_modifier
            * guts_modifier
    }

    pub fn tick(&mut self, ctx: HpTickCtx, dt: f64) {
        // NOTE unsure whether hp is consumed by `amount*dt` per frame or
        // `amount` once every second; believed to be the former (a rate).
        self.hp -= self.hp_per_second(ctx, ctx.current_speed) * dt;
    }

    pub fn has_remaining_hp(&self) -> bool {
        self.hp > 0.0
    }

    pub fn hp_ratio_remaining(&self) -> f64 {
        (self.hp / self.max_hp).max(0.0)
    }

    pub fn recover(&mut self, modifier: f64) {
        self.hp = self.max_hp.min(self.hp + self.max_hp * modifier);
    }

    /// Returns (transition_pos, speed). `pos` is the live solver's current
    /// position; everything else needed is captured in `self` or the fixed
    /// synthetic "last leg" context below (phase 2, no pace-down/downhill/
    /// kakari), matching the Python port's `_LastLegState` literal exactly.
    pub fn get_last_spurt_pair(&mut self, pos: f64, max_speed: f64, base_target_speed2: f64) -> (f64, f64) {
        let lastleg = HpTickCtx { is_pace_down: false, is_downhill_mode: false, is_kakari: false, phase: 2, current_speed: 0.0 };
        let max_dist = self.distance - phase_start(self.distance, 2);
        let s = (max_dist - 60.0) / max_speed;
        if self.hp >= self.hp_per_second(lastleg, max_speed) * s {
            return (-1.0, max_speed);
        }

        let mut candidates: Vec<(f64, f64)> = Vec::new();
        let remain_distance = self.distance - 60.0 - pos;
        let mut speed = max_speed - 0.1;
        while speed >= base_target_speed2 {
            let hp_at_base = self.hp_per_second(lastleg, base_target_speed2);
            let hp_at_speed = self.hp_per_second(lastleg, speed);
            let denom = base_target_speed2 * hp_at_speed - hp_at_base * speed;
            let numer = base_target_speed2 * self.hp - hp_at_base * remain_distance;
            let spurt_duration = (remain_distance / speed).min((numer / denom).max(0.0));
            let spurt_distance = spurt_duration * speed + 60.0;
            candidates.push((self.distance - spurt_distance, speed));
            speed -= 0.1;
        }

        if candidates.is_empty() {
            // maxSpeed - 0.1 < baseTargetSpeed2 (can happen with very low
            // speed, e.g. 1). Not clear what's "correct"; opt for never
            // starting spurt, matching the TS engine.
            return (self.distance, max_speed);
        }

        let finish_time = |cand: &(f64, f64)| (cand.0 - pos) / base_target_speed2 + (self.distance - cand.0) / cand.1;
        candidates.sort_by(|a, b| finish_time(a).partial_cmp(&finish_time(b)).unwrap());
        for &cand in &candidates {
            if self.rng.uniform(100_000.0) as i64 <= self.subpar_accept_chance {
                return cand;
            }
        }
        candidates[candidates.len() - 1]
    }
}

pub enum HpPolicy {
    Noop,
    Game(GameHpState),
}

impl HpPolicy {
    pub fn init(&mut self, horse: &HorseParameters) {
        if let HpPolicy::Game(g) = self {
            g.init(horse);
        }
    }

    pub fn tick(&mut self, ctx: HpTickCtx, dt: f64) {
        if let HpPolicy::Game(g) = self {
            g.tick(ctx, dt);
        }
    }

    pub fn has_remaining_hp(&self) -> bool {
        match self {
            HpPolicy::Noop => true,
            HpPolicy::Game(g) => g.has_remaining_hp(),
        }
    }

    pub fn hp_ratio_remaining(&self) -> f64 {
        match self {
            HpPolicy::Noop => 1.0,
            HpPolicy::Game(g) => g.hp_ratio_remaining(),
        }
    }

    pub fn recover(&mut self, modifier: f64) {
        if let HpPolicy::Game(g) = self {
            g.recover(modifier);
        }
    }

    pub fn get_last_spurt_pair(&mut self, pos: f64, max_speed: f64, base_target_speed2: f64) -> (f64, f64) {
        match self {
            HpPolicy::Noop => (-1.0, max_speed),
            HpPolicy::Game(g) => g.get_last_spurt_pair(pos, max_speed, base_target_speed2),
        }
    }

    /// Mirrors Python's `hasattr(self.hp, "hp")` direct-field pokes in
    /// RaceSolver's competition-mechanics methods (a no-op for the pacer's
    /// NoopHpPolicy, which has no `hp` field at all).
    pub fn hp_mut(&mut self) -> Option<&mut f64> {
        match self {
            HpPolicy::Noop => None,
            HpPolicy::Game(g) => Some(&mut g.hp),
        }
    }

    /// Read-only counterpart of `hp_mut`, for frame recording (`max(0.0,
    /// s.hp.hp) if hasattr(s.hp, "hp") else 0.0` in Python).
    pub fn hp_value(&self) -> Option<f64> {
        match self {
            HpPolicy::Noop => None,
            HpPolicy::Game(g) => Some(g.hp),
        }
    }
}
