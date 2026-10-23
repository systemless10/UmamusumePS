
use crate::course_data::{phase_start, CourseData};
use crate::horse_types::HorseParameters;
use crate::random_gen::Rule30CARng;

const HP_STRATEGY_COEFFICIENT: [f64; 6] = [0.0, 0.95, 0.89, 1.0, 0.995, 0.86];
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
    pub compete_top_hp_coef: f64,
    pub phase: i32,
    pub current_speed: f64,
    pub start_dash: bool,
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
            max_hp: 1.0,
            hp: 1.0,
            guts_modifier: 1.0,
            subpar_accept_chance: 0,
        }
    }

    pub fn init(&mut self, horse: &HorseParameters) {
        self.max_hp = ((horse.stamina as f32 * 0.8_f32) * HP_STRATEGY_COEFFICIENT[horse.strategy as usize] as f32 + self.distance as f32) as f64;
        self.hp = self.max_hp;
        self.guts_modifier = 1.0 + 200.0 / (600.0 * horse.guts).sqrt();
        self.subpar_accept_chance = ((15.0 + 0.05 * horse.wisdom) * 1000.0).round_ties_even() as i64;
    }

    fn get_status_modifier(ctx: HpTickCtx) -> f64 {
        let mut modifier = 1.0;
        if ctx.is_pace_down {
            modifier *= 0.6;
        }
        if ctx.is_downhill_mode {
            modifier *= 0.4;
        }
        if ctx.compete_top_hp_coef != 0.0 {
            modifier *= ctx.compete_top_hp_coef;
        } else if ctx.is_kakari {
            modifier *= 1.6;
        }
        modifier
    }

    fn hp_per_second(&self, ctx: HpTickCtx, velocity: f64) -> f64 {
        let guts_modifier = if ctx.phase >= 2 { self.guts_modifier } else { 1.0 };
        20.0 * (velocity - if ctx.start_dash { velocity } else { self.base_speed } + 12.0).powi(2) / 144.0
            * Self::get_status_modifier(ctx)
            * self.ground_modifier
            * guts_modifier
    }

    pub fn tick(&mut self, ctx: HpTickCtx, dt: f64) {
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

    pub fn get_last_spurt_pair(&mut self, pos: f64, max_speed: f64, base_target_speed2: f64) -> (f64, f64) {
        let lastleg = HpTickCtx { is_pace_down: false, is_downhill_mode: false, is_kakari: false, compete_top_hp_coef: 0.0, phase: 2, current_speed: 0.0, start_dash: false };
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

    pub fn hp_needed_to_finish(&self, pos: f64, mid_speed: f64, spurt_speed: f64) -> f64 {
        let spurt_start = phase_start(self.distance, 2);
        let mid = HpTickCtx { is_pace_down: false, is_downhill_mode: false, is_kakari: false, compete_top_hp_coef: 0.0, phase: 1, current_speed: 0.0, start_dash: false };
        let late = HpTickCtx { phase: 2, ..mid };
        let mut need = 0.0;
        if pos < spurt_start {
            need += self.hp_per_second(mid, mid_speed) * (spurt_start - pos) / mid_speed;
        }
        let late_dist = self.distance - pos.max(spurt_start);
        need += self.hp_per_second(late, spurt_speed) * late_dist / spurt_speed;
        need
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

    pub fn hp_mut(&mut self) -> Option<&mut f64> {
        match self {
            HpPolicy::Noop => None,
            HpPolicy::Game(g) => Some(&mut g.hp),
        }
    }

    pub fn hp_value(&self) -> Option<f64> {
        match self {
            HpPolicy::Noop => None,
            HpPolicy::Game(g) => Some(g.hp),
        }
    }
}
