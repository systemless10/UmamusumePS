
use std::cell::Cell;
use std::collections::HashSet;

use std::rc::Rc;

use crate::course_data::CourseData;
use crate::horse_types::{strategy_matches, HorseParameters, Strategy};
use crate::hp_policy::{HpPolicy, HpTickCtx};
use crate::race_field::{RaceField, HORSE_LANE};
use crate::random_gen::Rule30CARng;
use crate::region::Region;


const SPEED_STRATEGY_PHASE_COEFFICIENT: [[f64; 3]; 6] = [
    [0.0, 0.0, 0.0],
    [1.0, 0.98, 0.962],
    [0.978, 0.991, 0.975],
    [0.93, 0.998, 0.994],
    [0.931, 1.0, 1.02],
    [1.063, 0.962, 0.95],
];
const SPEED_DISTANCE_PROFICIENCY_MODIFIER: [f64; 8] = [1.05, 1.0, 0.9, 0.8, 0.6, 0.4, 0.2, 0.1];

fn base_speed(course: &CourseData) -> f64 {
    20.0 - (course.distance - 2000.0) / 1000.0
}

fn base_target_speed(horse: &HorseParameters, course: &CourseData, phase: usize) -> f64 {
    base_speed(course) * SPEED_STRATEGY_PHASE_COEFFICIENT[horse.strategy as usize][phase]
        + (if phase == 2 { 1.0 } else { 0.0 }) * (500.0 * horse.speed).sqrt() * SPEED_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude as usize] * 0.002
}

fn last_spurt_speed(horse: &HorseParameters, course: &CourseData) -> f64 {
    (base_target_speed(horse, course, 2) + 0.01 * base_speed(course)) * 1.05
        + (500.0 * horse.speed).sqrt() * SPEED_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude as usize] * 0.002
        + (450.0 * horse.guts).powf(0.597) * 0.0001
}

const ACCEL_STRATEGY_PHASE_COEFFICIENT: [[f64; 3]; 6] = [
    [0.0, 0.0, 0.0],
    [1.0, 1.0, 0.996],
    [0.985, 1.0, 0.996],
    [0.975, 1.0, 1.0],
    [0.945, 1.0, 0.997],
    [1.17, 0.94, 0.956],
];
const ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER: [f64; 8] = [1.05, 1.0, 0.9, 0.8, 0.7, 0.5, 0.3, 0.1];
const ACCEL_DISTANCE_PROFICIENCY_MODIFIER: [f64; 8] = [1.0, 1.0, 1.0, 1.0, 1.0, 0.6, 0.5, 0.4];

const BASE_ACCEL: f64 = 0.0006;
const UPHILL_BASE_ACCEL: f64 = 0.0004;

fn calc_base_accel(base_accel_const: f64, horse: &HorseParameters, phase: usize) -> f64 {
    let value = (500.0_f32 * horse.power as f32).sqrt() * base_accel_const as f32;
    let value = value * ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER[horse.surface_aptitude as usize] as f32;
    let value = value * ACCEL_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude as usize] as f32;
    (value * ACCEL_STRATEGY_PHASE_COEFFICIENT[horse.strategy as usize][phase] as f32) as f64
}

const PHASE_DECELERATION: [f64; 3] = [-1.2, -0.8, -1.0];

const STRATEGY_PROFICIENCY_MODIFIER_APPROX: [f64; 8] = [1.1, 1.0, 0.85, 0.75, 0.6, 0.4, 0.2, 0.1];

fn compete_dist_threshold(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige | Strategy::Nige => 0.0,
        Strategy::Senkou => 2.5,
        Strategy::Sasi => 5.0,
        Strategy::Oikomi => 10.0,
    }
}
fn compete_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 0.2,
        Strategy::Nige => 0.8,
        _ => 1.0,
    }
}
fn compete_stamina_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 1.5,
        Strategy::Nige => 1.2,
        _ => 1.0,
    }
}
fn secure_lead_coef(own: Strategy, rival: Strategy) -> Option<f64> {
    use Strategy::*;
    match (own, rival) {
        (Oonige, Nige) => Some(2.0),
        (Oonige, Senkou) => Some(7.0),
        (Oonige, Sasi) | (Oonige, Oikomi) => Some(8.0),
        (Nige, Senkou) => Some(4.0),
        (Nige, Sasi) | (Nige, Oikomi) => Some(8.0),
        (Senkou, Sasi) => Some(5.0),
        (Senkou, Oikomi) => Some(6.0),
        (Sasi, Oikomi) => Some(3.0),
        _ => None,
    }
}
fn has_secure_lead_row(own: Strategy) -> bool {
    own != Strategy::Oikomi
}
fn secure_lead_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 0.2,
        Strategy::Sasi => 0.8,
        _ => 1.0,
    }
}
fn secure_lead_stamina_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 1.2,
        Strategy::Nige => 1.0,
        _ => 0.8,
    }
}

fn forwardness(s: Strategy) -> i32 {
    match s {
        Strategy::Oonige => 0,
        Strategy::Nige => 1,
        Strategy::Senkou => 2,
        Strategy::Sasi => 3,
        Strategy::Oikomi => 4,
    }
}

fn force_in_strategy(s: Strategy) -> f64 {
    match s {
        Strategy::Nige | Strategy::Oonige => 0.02,
        Strategy::Senkou | Strategy::Sasi => 0.01,
        Strategy::Oikomi => 0.03,
    }
}

const VISIBLE_DISTANCE: f64 = 20.0;

fn competition_distance_coefficient(distance: f64) -> f64 {
    if distance < 1801.0 {
        0.3
    } else if distance < 2101.0 {
        0.5
    } else if distance < 2201.0 {
        0.8
    } else if distance < 2401.0 {
        1.0
    } else if distance < 2601.0 {
        1.1
    } else {
        1.2
    }
}

const POSITION_KEEP_BASE_MIN_THRESHOLD: [f64; 6] = [0.0, 0.0, 3.0, 6.5, 7.5, 0.0];
const POSITION_KEEP_BASE_MAX_THRESHOLD: [f64; 6] = [0.0, 0.0, 5.0, 7.0, 8.0, 0.0];

fn position_keep_course_factor(distance: f64) -> f64 {
    0.0008 * (distance - 1000.0) + 1.0
}
fn position_keep_min_threshold(strategy: Strategy, distance: f64) -> f64 {
    let factor = if strategy == Strategy::Senkou { 1.0 } else { position_keep_course_factor(distance) };
    POSITION_KEEP_BASE_MIN_THRESHOLD[strategy as usize] * factor
}
fn position_keep_max_threshold(strategy: Strategy, distance: f64) -> f64 {
    POSITION_KEEP_BASE_MAX_THRESHOLD[strategy as usize] * position_keep_course_factor(distance)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TemptationMode {
    Null = 0,
    PositionSashi = 1,
    PositionSenko = 2,
    PositionNige = 3,
    Boost = 4,
}

pub const EVENT_COMPETE_TOP: i32 = 4;
pub const EVENT_COMPETE_FIGHT: i32 = 5;
pub const EVENT_RELEASE_CONSERVE_POWER: i32 = 6;


pub type Timer = Rc<Cell<f64>>;

fn new_timer_value(t: f64) -> Timer {
    Rc::new(Cell::new(t))
}

#[derive(Clone, Copy)]
pub struct CompensatedAccumulator {
    pub acc: f64,
    pub err: f64,
}

impl CompensatedAccumulator {
    fn new(acc: f64) -> Self {
        CompensatedAccumulator { acc, err: 0.0 }
    }

    pub fn add(&mut self, n: f64) {
        let t = self.acc + n;
        if self.acc.abs() >= n.abs() {
            self.err += (self.acc - t) + n;
        } else {
            self.err += (n - t) + self.acc;
        }
        self.acc = t;
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Perspective {
    Self_ = 1,
    Other = 2,
    Any = 3,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum SkillType {
    Noop = 0,
    SpeedUp = 1,
    StaminaUp = 2,
    PowerUp = 3,
    GutsUp = 4,
    WisdomUp = 5,
    Oonige = 6,
    VisibleDistance = 8,
    TargetLane = 35,
    DebuffCancel = 38,
    Recovery = 9,
    MultiplyStartDelay = 10,
    ExtendKakari = 13,
    SetStartDelay = 14,
    CurrentSpeed = 21,
    CurrentSpeedWithNaturalDeceleration = 22,
    TargetSpeed = 27,
    LaneMoveSpeedUp = 28,
    ModifyKakariChance = 29,
    Accel = 31,
    AllStatusUp = 32,
    ActivateRandomGold = 37,
    ExtendEvolvedDuration = 42,
}

impl SkillType {
    fn is_stat(self) -> bool {
        matches!(self, SkillType::SpeedUp | SkillType::StaminaUp | SkillType::PowerUp | SkillType::GutsUp | SkillType::WisdomUp | SkillType::AllStatusUp)
    }
    fn is_timed(self) -> bool {
        matches!(self, SkillType::CurrentSpeed | SkillType::CurrentSpeedWithNaturalDeceleration | SkillType::TargetSpeed | SkillType::Accel | SkillType::LaneMoveSpeedUp | SkillType::VisibleDistance | SkillType::TargetLane)
    }
}

pub mod skill_target {
    pub const SELF: i32 = 1;
    pub const ALL: i32 = 2;
    pub const ALL_ALLIES: i32 = 11;
    pub const IN_FOV: i32 = 4;
    pub const AHEAD_OF_SELF: i32 = 9;
    pub const BEHIND_SELF: i32 = 10;
    pub const ENEMY_STRATEGY: i32 = 18;
    pub const KAKARI_AHEAD: i32 = 19;
    pub const KAKARI_BEHIND: i32 = 20;
    pub const KAKARI_STRATEGY: i32 = 21;
    pub const AHEAD_OF_POSITION: i32 = 7;
    pub const UMA_ID: i32 = 22;
    pub const USED_RECOVERY: i32 = 23;
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub enum SkillRarity {
    White = 1,
    Gold = 2,
    Unique = 3,
    Evolution = 6,
}

#[derive(Clone, Copy)]
pub struct SkillEffect {
    pub effect_type: SkillType,
    pub base_duration: f64,
    pub modifier: f64,
    pub target: i32,
    pub target_value: i32,
    pub time_usage: i32,
    pub value_usage: i32,
    pub additional_activate: i32,
}

impl SkillEffect {
    pub fn on_self(effect_type: SkillType, base_duration: f64, modifier: f64) -> SkillEffect {
        SkillEffect { effect_type, base_duration, modifier, target: skill_target::SELF, target_value: 0, time_usage: 1, value_usage: 1, additional_activate: 0 }
    }
}

pub type SharedCond = Rc<dyn Fn(&RaceSolver, Option<&RaceField>) -> bool>;

#[derive(Clone)]
pub struct PendingSkill {
    pub skill_id: String,
    pub perspective: Perspective,
    pub rarity: SkillRarity,
    pub trigger: Region,
    pub extra_condition: SharedCond,
    pub effects: Vec<SkillEffect>,
    pub cooldown: f64,
    pub followups: Vec<Region>,
    pub green: bool,
}

impl PendingSkill {
    fn rearmed(&self, pos: f64) -> Option<PendingSkill> {
        let mut again = self.clone();
        if !self.followups.is_empty() {
            let rest: Vec<Region> = self.followups.iter().copied().filter(|r| r.end > pos).collect();
            if rest.is_empty() {
                return None;
            }
            again.trigger = rest[0];
            again.followups = rest[1..].to_vec();
        }
        Some(again)
    }
}

struct Watcher {
    effect: SkillEffect,
    skill_id: String,
    perspective: Perspective,
    kind: i32,
    left: i32,
    timer: Timer,
    group: u64,
}

fn bucket(value: f64, table: &[(Option<f64>, f64)]) -> f64 {
    crate::race_solver_builder::bucket(value, table)
}

const GREEN_COUNT_MULT: [(Option<f64>, f64); 4] = [(Some(3.0), 0.0), (Some(5.0), 1.0), (Some(6.0), 2.0), (None, 3.0)];
const SPEED1_MULT: [(Option<f64>, f64); 5] = [(Some(1700.0), 0.0), (Some(1800.0), 1.0), (Some(1900.0), 2.0), (Some(2000.0), 3.0), (None, 4.0)];
const SPEED2_MULT: [(Option<f64>, f64); 3] = [(Some(1400.0), 1.0), (Some(1600.0), 2.0), (None, 3.0)];
const CORNER_LATE_ORDER_UP_MULT: [(Option<f64>, f64); 5] = [(Some(1.0), 1.0), (Some(2.0), 1.1), (Some(3.0), 1.2), (Some(4.0), 1.3), (None, 1.4)];
const BLOCKED_SIDE_MULT: [(Option<f64>, f64); 4] = [(Some(2.0), 1.0), (Some(4.0), 2.0), (Some(6.0), 3.0), (None, 4.0)];

fn additional_activate_limit(kind: i32) -> i32 {
    match kind {
        1 | 2 => 3,
        3 => 2,
        _ => 0,
    }
}

struct ActiveSkill {
    skill_id: String,
    perspective: Perspective,
    duration_timer: Timer,
    modifier: f64,
    natural_deceleration: bool,
}

pub struct Activation {
    pub t: f64,
    pub skill_id: String,
    pub has_stat_effect: bool,
    pub targets: Vec<usize>,
    pub timed_duration: Option<f64>,
}

pub type SkillHook = Box<dyn FnMut(&mut RaceSolver, &str, Perspective)>;

fn noop_hook() -> SkillHook {
    Box::new(|_s, _id, _p| {})
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PosKeepMode {
    PaceDown,
    PaceUp,
    PaceUpEx,
    SpeedUp,
    Overtake,
}

#[derive(Clone, Copy)]
struct OtherView {
    idx: usize,
    pos: f64,
    lane: f64,
}

pub struct RaceSolver {
    pub horse: HorseParameters,
    pub course: &'static CourseData,
    pub hp: HpPolicy,
    pub pacer: Option<Box<RaceSolver>>,
    pub rng: Rule30CARng,
    pub gate_index: i32,
    pub field_index: Option<usize>,
    pub finished: bool,

    pub pending_skills: Vec<PendingSkill>,
    pub pending_removal: HashSet<String>,
    armed_scan: bool,
    pending_seq: Vec<usize>,
    armed: Vec<usize>,
    pending_queue: Vec<(f64, usize)>,

    pub used_skills: HashSet<String>,
    gorosi_rng: Rule30CARng,
    pace_effect_rng: Rule30CARng,

    pub timers: Vec<Timer>,
    pub accumulatetime: Timer,

    pub activation_log: Vec<Activation>,
    pub event_log: Vec<(f64, i32)>,
    pub kakari_log: Vec<(f64, f64)>,
    pub kakari_started_at: f64,

    cooling: Vec<(f64, PendingSkill)>,
    next_seq: usize,
    watchers: Vec<Watcher>,
    order_up_groups: Vec<(Vec<Timer>, i32)>,
    activation_group: u64,
    green_activations: i32,
    corner_late_order_up: i32,
    mid_side_block: f64,
    mid_side_block_max: f64,
    pub advantage_types: Vec<i32>,
    pub deferred_external: Vec<(SkillEffect, f64, String, usize)>,
    pub chara_id: i32,
    pub is_viewer: bool,
    pub context: std::collections::BTreeMap<String, crate::json::Value>,
    pub vision_modifiers: Vec<(Option<Timer>,f64)>,
    pub lane_modifiers: Vec<(Option<Timer>,f64)>,
    pub debuff_cancel: bool,
    pub overtake_target_timer: f64,
    pub overtake_no_order_up_timer: f64,

    pub gate_roll: i64,
    pub random_lot: i64,
    pub phase: i32,
    next_phase_transition: f64,

    active_target_speed_skills: Vec<ActiveSkill>,
    active_current_speed_skills: Vec<ActiveSkill>,
    active_accel_skills: Vec<ActiveSkill>,
    active_lane_speed_skills: Vec<ActiveSkill>,
    pub activate_count: [i32; 3],
    pub activate_count_heal: i32,
    pub activate_count_later_half: i32,
    pub activate_count_last_frame: i32,

    on_skill_activate: SkillHook,
    on_skill_deactivate: SkillHook,

    section_length: f64,
    pub is_pace_down: bool,
    pos_keep_end: f64,
    pos_keep_speed_coef: f64,
    pos_keep_mode: Option<PosKeepMode>,
    pos_keep_effect_exit_distance: f64,
    pos_keep_effect_start: f64,
    pos_keep_timer: Timer,
    pos_keep_continue_distance: f64,

    modifiers_target_speed: CompensatedAccumulator,
    modifiers_current_speed: CompensatedAccumulator,
    modifiers_accel: CompensatedAccumulator,
    modifiers_one_frame_accel: f64,
    modifiers_special_skill_duration_scaling: f64,
    modifiers_kakari_chance: f64,
    lane_speed_skill_modifier: f64,

    pub start_delay: f64,
    pub pos: f64,
    accel: f64,
    pub current_speed: f64,
    pub target_speed: f64,
    is_downhill_mode: bool,
    pub is_last_spurt: bool,
    pub last_spurt_start_pos: f64,

    pub lane: f64,
    lane_target: f64,
    lane_change_speed: f64,
    pub lane_retarget: bool,
    extra_move_lane: Option<f64>,
    pub front_blocker_index: i32,
    pub blocked_front_timer: Cell<f64>,
    pub blocked_side_timer: Cell<f64>,
    pub blocked_all_timer: Cell<f64>,
    pub infront_near_lane_timer: Cell<f64>,
    pub behind_near_lane_timer: Cell<f64>,
    pub moved_lane_this_tick: bool,

    pub compete_top_active: bool,
    compete_top_timer: Timer,
    compete_top_modifier: f64,
    compete_spurt_active: bool,
    compete_spurt_timer: Timer,
    compete_spurt_check: Timer,
    compete_spurt_modifier: f64,
    secure_lead_active: bool,
    secure_lead_timer: Timer,
    secure_lead_check: Timer,
    secure_lead_modifier: f64,
    stamina_keep_active: bool,
    stamina_keep_check: Timer,
    duel_target: i64,
    duel_continuous_timer: f64,
    duel_active: bool,
    duel_target_speed_modifier: f64,
    duel_accel_modifier: f64,

    min_speed: f64,
    start_dash: bool,

    n_hills: usize,
    hill_start: Vec<f64>,
    hill_end: Vec<f64>,
    hill_idx: i64,
    hill_rng: Vec<Rule30CARng>,
    downhill_timer: Timer,
    slope_per: f64,

    base_target_speed: [f64; 3],
    pub last_spurt_speed: f64,
    pub last_spurt_transition: f64,

    pub kakari_start: f64,
    kakari_duration: f64,
    kakari_timer: Timer,
    pub is_kakari: bool,
    pub temptation_mode: TemptationMode,
    pub temptation_count: i32,

    section_modifier: Vec<f64>,
    base_accel: [f64; 6],

    temptation_roll: f64,
    force_in_random: f64,
    pos_keep_rng: Rule30CARng,
    compete_rng: Rule30CARng,
    stamina_keep_factor: f64,

    pub order_band_in_200: Cell<Option<f64>>,
    pub order_band_in_400: Cell<Option<f64>>,
    pub order_band_in_500: Cell<Option<f64>>,
    pub order_band_in_800: Cell<Option<f64>>,
    pub order_band_out_200: Cell<Option<f64>>,
    pub order_band_out_400: Cell<Option<f64>>,
    pub order_band_out_500: Cell<Option<f64>>,
    pub order_band_out_700: Cell<Option<f64>>,
}

impl RaceSolver {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        horse: HorseParameters,
        course: &'static CourseData,
        mut rng: Rule30CARng,
        skills: Vec<PendingSkill>,
        hp: HpPolicy,
        pacer: Option<Box<RaceSolver>>,
        on_skill_activate: Option<SkillHook>,
        on_skill_deactivate: Option<SkillHook>,
        gate_index: i32,
    ) -> RaceSolver {
        let armed_scan = !skills.iter().any(|sk| sk.effects.iter().any(|ef| ef.effect_type == SkillType::ActivateRandomGold));
        let pending_seq: Vec<usize> = (0..skills.len()).collect();
        let next_seq = skills.len();
        let mut pending_queue: Vec<(f64, usize)> = skills.iter().enumerate().map(|(seq, sk)| (sk.trigger.start, seq)).collect();
        pending_queue.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());

        let gorosi_rng = Rule30CARng::seeded(rng.int32() as u64);
        let pace_effect_rng = Rule30CARng::seeded(rng.int32() as u64);

        let strategy = horse.strategy;
        let distance = course.distance;
        let section_length = distance / 24.0;

        let mut solver = RaceSolver {
            horse,
            course,
            hp,
            pacer,
            rng,
            gate_index,
            field_index: None,
            finished: false,

            pending_skills: skills,
            pending_removal: HashSet::new(),
            armed_scan,
            pending_seq,
            armed: Vec::new(),
            pending_queue,

            used_skills: HashSet::new(),
            gorosi_rng,
            pace_effect_rng,

            timers: Vec::new(),
            accumulatetime: new_timer_value(0.0),

            activation_log: Vec::new(),
            event_log: Vec::new(),
            kakari_log: Vec::new(),
            kakari_started_at: 0.0,

            cooling: Vec::new(),
            next_seq: 0,
            watchers: Vec::new(),
            order_up_groups: Vec::new(),
            activation_group: 0,
            green_activations: 0,
            corner_late_order_up: 0,
            mid_side_block: 0.0,
            mid_side_block_max: 0.0,
            advantage_types: Vec::new(),
            deferred_external: Vec::new(),
            chara_id: 0,
            is_viewer: false,
            context: std::collections::BTreeMap::new(),
            vision_modifiers: Vec::new(),
            lane_modifiers: Vec::new(),
            debuff_cancel: false,
            overtake_target_timer: 0.0,
            overtake_no_order_up_timer: 0.0,

            gate_roll: 0,
            random_lot: 0,
            phase: 0,
            next_phase_transition: crate::course_data::phase_start(distance, 1),

            active_target_speed_skills: Vec::new(),
            active_current_speed_skills: Vec::new(),
            active_accel_skills: Vec::new(),
            active_lane_speed_skills: Vec::new(),
            activate_count: [0, 0, 0],
            activate_count_heal: 0,
            activate_count_later_half: 0,
            activate_count_last_frame: 0,

            on_skill_activate: on_skill_activate.unwrap_or_else(noop_hook),
            on_skill_deactivate: on_skill_deactivate.unwrap_or_else(noop_hook),

            section_length,
            is_pace_down: false,
            pos_keep_end: section_length * 10.0,
            pos_keep_speed_coef: 1.0,
            pos_keep_mode: None,
            pos_keep_effect_exit_distance: 0.0,
            pos_keep_effect_start: 0.0,
            pos_keep_timer: new_timer_value(0.0),
            pos_keep_continue_distance: section_length.floor() * if strategy == Strategy::Oonige { 3.0 } else { 1.0 },

            modifiers_target_speed: CompensatedAccumulator::new(0.0),
            modifiers_current_speed: CompensatedAccumulator::new(0.0),
            modifiers_accel: CompensatedAccumulator::new(0.0),
            modifiers_one_frame_accel: 0.0,
            modifiers_special_skill_duration_scaling: 1.0,
            modifiers_kakari_chance: 0.0,
            lane_speed_skill_modifier: 0.0,

            start_delay: 0.0,
            pos: 0.0,
            accel: 0.0,
            current_speed: 3.0,
            target_speed: 0.85 * base_speed(course),
            is_downhill_mode: false,
            is_last_spurt: false,
            last_spurt_start_pos: -1.0,

            lane: 0.0,
            lane_target: 0.0,
            lane_change_speed: 0.0,
            lane_retarget: true,
            extra_move_lane: None,
            front_blocker_index: -1,
            blocked_front_timer: Cell::new(0.0),
            blocked_side_timer: Cell::new(0.0),
            blocked_all_timer: Cell::new(0.0),
            infront_near_lane_timer: Cell::new(0.0),
            behind_near_lane_timer: Cell::new(0.0),
            moved_lane_this_tick: false,

            compete_top_active: false,
            compete_top_timer: new_timer_value(0.0),
            compete_top_modifier: 0.0,
            compete_spurt_active: false,
            compete_spurt_timer: new_timer_value(0.0),
            compete_spurt_check: new_timer_value(0.0),
            compete_spurt_modifier: 0.0,
            secure_lead_active: false,
            secure_lead_timer: new_timer_value(0.0),
            secure_lead_check: new_timer_value(0.0),
            secure_lead_modifier: 0.0,
            stamina_keep_active: false,
            stamina_keep_check: new_timer_value(0.0),
            duel_target: -1,
            duel_continuous_timer: 0.0,
            duel_active: false,
            duel_target_speed_modifier: 0.0,
            duel_accel_modifier: 0.0,

            min_speed: 0.0,
            start_dash: true,

            n_hills: 0,
            hill_start: Vec::new(),
            hill_end: Vec::new(),
            hill_idx: -1,
            hill_rng: Vec::new(),
            downhill_timer: new_timer_value(0.0),
            slope_per: 0.0,

            base_target_speed: [0.0; 3],
            last_spurt_speed: 0.0,
            last_spurt_transition: -1.0,

            kakari_start: 0.0,
            kakari_duration: 0.0,
            kakari_timer: new_timer_value(0.0),
            is_kakari: false,
            temptation_mode: TemptationMode::Null,
            temptation_count: 0,

            section_modifier: Vec::new(),
            base_accel: [0.0; 6],

            temptation_roll: 0.0,
            force_in_random: 0.0,
            pos_keep_rng: Rule30CARng::new(0, 0),
            compete_rng: Rule30CARng::new(0, 0),
            stamina_keep_factor: 1.0,

            order_band_in_200: Cell::new(None),
            order_band_in_400: Cell::new(None),
            order_band_in_500: Cell::new(None),
            order_band_in_800: Cell::new(None),
            order_band_out_200: Cell::new(None),
            order_band_out_400: Cell::new(None),
            order_band_out_500: Cell::new(None),
            order_band_out_700: Cell::new(None),
        };

        solver.next_seq = next_seq;
        solver.accumulatetime = solver.new_timer(0.0);
        solver.pos_keep_timer = solver.new_timer(0.0);
        solver.compete_top_timer = solver.new_timer(0.0);
        solver.compete_spurt_timer = solver.new_timer(0.0);
        solver.compete_spurt_check = solver.new_timer(0.0);
        solver.secure_lead_timer = solver.new_timer(0.0);
        solver.secure_lead_check = solver.new_timer(0.0);
        solver.stamina_keep_check = solver.new_timer(0.0);

        solver.gate_roll = solver.rng_uniform(12252240.0) as i64;
        solver.random_lot = solver.rng_uniform(100.0) as i64;

        solver.start_delay = 0.1 * solver.rng.random();
        if let Some(p) = solver.pacer.as_mut() {
            p.start_delay = 0.0;
        }

        solver.lane = (solver.gate_index as f64 + solver.gate_adjustor()) * HORSE_LANE;
        solver.lane_target = solver.lane;

        solver.process_skill_activations(None);
        solver.min_speed = 0.85 * base_speed(solver.course) + (200.0 * solver.horse.guts).sqrt() * 0.001;
        solver.start_dash = true;
        solver.modifiers_accel.add(24.0);

        solver.init_hills();

        solver.base_target_speed = [base_target_speed(&solver.horse, solver.course, 0), base_target_speed(&solver.horse, solver.course, 1), base_target_speed(&solver.horse, solver.course, 2)];
        solver.last_spurt_speed = last_spurt_speed(&solver.horse, solver.course);
        solver.last_spurt_transition = -1.0;

        solver.kakari_start = (1.0 + solver.rng_uniform(8.0)) * solver.section_length;
        let kakari_pct = ((6.5 / (0.1 * solver.horse.wisdom + 1.0).log10()).powf(2.0) - 1e-9).ceil();
        if solver.rng.random() * 100.0 >= kakari_pct + solver.modifiers_kakari_chance * 100.0 {
            solver.kakari_start = solver.course.distance + 9999.0;
        }
        let r1 = solver.rng.random();
        let r2 = solver.rng.random();
        let r3 = solver.rng.random();
        let seq = [0.0, r1, r2, r3, 1.0];
        let idx = seq.iter().position(|&x| x > 0.45).unwrap();
        solver.kakari_duration = 3.0 * idx as f64;
        solver.kakari_timer = solver.new_timer(0.0);
        solver.temptation_count = 0;

        solver.section_modifier = Vec::with_capacity(25);
        for _ in 0..24 {
            let max_v = solver.horse.wisdom / 5500.0 * (solver.horse.wisdom * 0.1).log10();
            let factor = (max_v - 0.65 + solver.rng.random() * 0.65) / 100.0;
            solver.section_modifier.push(base_speed(solver.course) * factor);
        }
        solver.section_modifier.push(0.0);

        solver.temptation_roll = solver.rng.random();
        solver.force_in_random = solver.rng.random() * 0.1;
        solver.pos_keep_rng = Rule30CARng::seeded(solver.rng.int32() as u64);
        solver.compete_rng = Rule30CARng::seeded(solver.rng.int32() as u64);
        solver.stamina_keep_factor = 1.035 + 0.005 * solver.rng.random();

        solver.hp.init(&solver.horse);

        solver.base_accel = [0, 1, 2, 0, 1, 2].iter().enumerate().map(|(i, &p)| {
            calc_base_accel(if i > 2 { UPHILL_BASE_ACCEL } else { BASE_ACCEL }, &solver.horse, p)
        }).collect::<Vec<f64>>().try_into().unwrap();

        solver
    }

    fn rng_uniform(&mut self, upper: f64) -> f64 {
        self.rng.uniform(upper) as f64
    }

    fn call_activate_hook(&mut self, skill_id: &str, perspective: Perspective) {
        let mut hook = std::mem::replace(&mut self.on_skill_activate, noop_hook());
        hook(self, skill_id, perspective);
        self.on_skill_activate = hook;
    }

    fn call_deactivate_hook(&mut self, skill_id: &str, perspective: Perspective) {
        let mut hook = std::mem::replace(&mut self.on_skill_deactivate, noop_hook());
        hook(self, skill_id, perspective);
        self.on_skill_deactivate = hook;
    }


    fn new_timer(&mut self, t: f64) -> Timer {
        let tm = new_timer_value(t);
        self.timers.push(tm.clone());
        tm
    }

    fn release_timer(&mut self, tm: &Timer) {
        if let Some(pos) = self.timers.iter().position(|t| Rc::ptr_eq(t, tm)) {
            self.timers.remove(pos);
        }
    }

    pub fn refresh_derived_stats(&mut self) {
        self.min_speed = 0.85 * base_speed(self.course) + (200.0 * self.horse.guts).sqrt() * 0.001;
        self.base_target_speed = [base_target_speed(&self.horse, self.course, 0), base_target_speed(&self.horse, self.course, 1), base_target_speed(&self.horse, self.course, 2)];
        self.last_spurt_speed = last_spurt_speed(&self.horse, self.course);
        self.hp.init(&self.horse);
        self.base_accel = [0, 1, 2, 0, 1, 2].iter().enumerate().map(|(i, &p)| {
            calc_base_accel(if i > 2 { UPHILL_BASE_ACCEL } else { BASE_ACCEL }, &self.horse, p)
        }).collect::<Vec<f64>>().try_into().unwrap();
    }

    fn gate_adjustor(&self) -> f64 {
        let gate = self.gate_index + 1;
        match self.course.race_track_id {
            10101 => 0.0,
            10201 => if gate >= 14 { 1.86 } else { 0.0 },
            _ => if gate >= 10 { 0.6 } else { 0.0 },
        }
    }

    fn init_hills(&mut self) {
        self.n_hills = self.course.slopes.len();
        self.hill_start = self.course.slopes.iter().map(|s| s.start).rev().collect();
        self.hill_end = self.course.slopes.iter().map(|s| s.start + s.length).rev().collect();
        self.hill_idx = -1;

        let mut hill_rng = Vec::with_capacity(self.n_hills);
        for _ in 0..self.n_hills {
            let hi = self.rng.int32() as u64;
            let lo = self.rng.int32() as u64;
            hill_rng.push(Rule30CARng::seeded((hi << 32) | lo));
        }
        self.hill_rng = hill_rng;
        self.downhill_timer = self.new_timer(0.0);

        if self.hill_start.last() == Some(&0.0) {
            self.hill_idx = 0;
            self.slope_per = self.course.slopes[0].slope;
            self.downhill_timer.set(0.0);
            let roll = self.hill_rng[0].random();
            self.downhill_check(roll);
            self.hill_start.pop();
        } else {
            self.slope_per = 0.0;
        }
    }


    fn get_max_speed(&self) -> f64 {
        if self.start_dash {
            self.target_speed.min(0.85 * base_speed(self.course))
        } else if self.current_speed + self.modifiers_one_frame_accel > self.target_speed {
            9999.0
        } else {
            self.target_speed
        }
    }

    pub fn compete_top_hp_coef(&self) -> f64 {
        if !self.compete_top_active {
            return 0.0;
        }
        if self.horse.strategy == Strategy::Oonige {
            if self.is_kakari { 7.7 } else { 3.5 }
        } else if self.is_kakari {
            3.6
        } else {
            1.4
        }
    }

    pub fn step(&mut self, dt: f64, field: Option<&RaceField>) {
        let mut motion_fraction = 1.0;
        if self.accumulatetime.get() < self.start_delay {
            let remaining = (self.start_delay as f32 - self.accumulatetime.get() as f32) - dt as f32;
            if remaining > 0.0 {
                for tm in &self.timers { tm.set(tm.get() + dt); }
                return;
            }
            if remaining < 0.0 { motion_fraction = (remaining.abs() / dt as f32) as f64; }
        }

        if self.pos < self.pos_keep_end {
            if let Some(pacer) = self.pacer.as_mut() {
                pacer.step(dt, field);
            }
        }

        let was_start_dash = self.start_dash;
        let halfv;
        if was_start_dash {
            self.apply_forces();
            self.accel = self.accel as f32 as f64;
            halfv = ((self.current_speed as f32 + self.accel as f32 * dt as f32) as f64).min(self.get_max_speed());
            self.current_speed = halfv;
            let displacement = (halfv as f32 + self.modifiers_current_speed.acc as f32) + self.modifiers_current_speed.err as f32;
            let movement = (displacement * dt as f32) * motion_fraction as f32;
            self.pos = (self.pos as f32 + movement) as f64;
        } else {
            halfv = (self.current_speed + 0.5 * dt * self.accel).min(self.get_max_speed());
            let displacement = halfv + self.modifiers_current_speed.acc + self.modifiers_current_speed.err;
            self.pos += displacement * dt * motion_fraction;
        }
        let hp_ctx = HpTickCtx {
            is_pace_down: self.is_pace_down,
            is_downhill_mode: self.is_downhill_mode,
            is_kakari: self.is_kakari,
            compete_top_hp_coef: self.compete_top_hp_coef(),
            phase: self.phase,
            current_speed: self.current_speed,
            start_dash: was_start_dash,
        };
        self.hp.tick(hp_ctx, dt);
        for tm in &self.timers {
            tm.set(tm.get() + dt);
        }
        self.update_hills();
        self.update_phase();
        self.process_skill_activations(field);
        self.update_overtake_effects(field);
        self.update_kakari();
        self.update_position_keep(field);
        self.update_last_spurt_state();
        self.update_target_speed(field);
        self.apply_forces();
        if !was_start_dash {
            self.current_speed = (halfv + 0.5 * dt * self.accel + self.modifiers_one_frame_accel).min(self.get_max_speed());
        }
        if !self.start_dash && self.current_speed < self.min_speed {
            self.current_speed = self.min_speed;
        } else if self.start_dash && self.current_speed >= 0.85 * base_speed(self.course) {
            self.start_dash = false;
            self.modifiers_accel.add(-24.0);
        }
        self.modifiers_one_frame_accel = 0.0;
        self.update_blocking(dt, field);
        self.update_lane(dt, field);
        self.update_near_lane_timers(dt, field);
        if let Some(f) = field {
            let targets = !self.overtake_targets(f).is_empty();
            self.overtake_target_timer = if targets {self.overtake_target_timer + dt} else {0.0};
            self.overtake_no_order_up_timer = if targets && !f.did_overtake(self.field_index.unwrap()) {self.overtake_no_order_up_timer + dt} else {0.0};
        }
        self.update_competition_mechanics(dt, field);
    }

    fn update_blocking(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => {
                self.front_blocker_index = -1;
                self.blocked_front_timer.set(0.0);
                self.blocked_side_timer.set(0.0);
                self.blocked_all_timer.set(0.0);
                return;
            }
        };
        let i = self.field_index.unwrap();
        let blocker = field.front_blocker(i, self.pos, self.lane);
        if let Some((other_idx, dist_gap)) = blocker {
            let other_speed = field.solvers[other_idx].borrow().current_speed;
            let cap = (0.988 + 0.012 * (dist_gap / 2.0)) * other_speed;
            self.current_speed = self.current_speed.min(cap);
            self.blocked_front_timer.set(self.blocked_front_timer.get() + dt);
            self.front_blocker_index = other_idx as i32;
        } else {
            self.blocked_front_timer.set(0.0);
            self.front_blocker_index = -1;
        }
        let (inner_blocked, outer_blocked) = field.side_room(i, self.pos, self.lane);
        let side_blocked = inner_blocked || outer_blocked;
        self.blocked_side_timer.set(if side_blocked { self.blocked_side_timer.get() + dt } else { 0.0 });
        if self.phase == 1 {
            self.mid_side_block = if side_blocked { self.mid_side_block + dt } else { 0.0 };
            self.mid_side_block_max = self.mid_side_block_max.max(self.mid_side_block);
        }
        let all_blocked = blocker.is_some() && side_blocked;
        self.blocked_all_timer.set(if all_blocked { self.blocked_all_timer.get() + dt } else { 0.0 });
    }


    fn move_lane_point(&self) -> f64 {
        self.course.corners.first().map_or(self.course.distance, |c| c.start)
    }

    pub fn visible_distance(&self)->f64 {
        (20.0+self.vision_modifiers.iter().filter(|(tm,_)|tm.as_ref().map(|t|t.get()<0.0).unwrap_or(true)).map(|(_,m)|m).sum::<f64>()).max(0.0)
    }

    pub fn is_visible(&self, other_pos: f64, other_lane: f64) -> bool {
        let distance=self.visible_distance();
        let gap=other_pos-self.pos;
        0.0<=gap && gap<=distance && (other_lane-self.lane).abs() <= ((gap/distance.max(0.001))*11.5*HORSE_LANE+2.0*HORSE_LANE)/2.0
    }

    fn overtake_targets(&self, field: &RaceField) -> Vec<OtherView> {
        let i = self.field_index.unwrap();
        let mut targets: Vec<OtherView> = Vec::new();
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            if o.finished {
                continue;
            }
            let dist_gap = o.pos - self.pos;
            if !(1.0..=20.0).contains(&dist_gap) || !self.is_visible(o.pos, o.lane) {
                continue;
            }
            let speed_gap = self.current_speed - o.current_speed;
            if speed_gap <= 0.0 || dist_gap / speed_gap >= 15.0 {
                continue;
            }
            if o.target_speed < self.target_speed || (o.front_blocker_index >= 0 && o.current_speed < self.target_speed) {
                targets.push(OtherView { idx: j, pos: o.pos, lane: o.lane });
            }
        }
        if self.front_blocker_index >= 0 {
            let b = self.front_blocker_index as usize;
            if !targets.iter().any(|t| t.idx == b) {
                let o = field.solvers[b].borrow();
                targets.push(OtherView { idx: b, pos: o.pos, lane: o.lane });
            }
        }
        targets
    }

    fn lane_clear(&self, field: &RaceField, cand: f64, dist_lo: f64, dist_hi: f64) -> bool {
        let i = self.field_index.unwrap();
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            if o.finished {
                continue;
            }
            if !(dist_lo <= o.pos && o.pos <= dist_hi) || !self.is_visible(o.pos, o.lane) {
                continue;
            }
            if cand - 0.8 * HORSE_LANE <= o.lane && o.lane <= cand + 0.8 * HORSE_LANE {
                return false;
            }
        }
        true
    }

    fn in_final_straight(&self) -> bool {
        match self.course.straights.last() {
            Some(last) => last.start <= self.pos && self.pos < last.end,
            None => false,
        }
    }

    fn update_lane_target(&mut self, field: &RaceField) {
        let i = self.field_index.unwrap();
        let (inner_blocked, outer_blocked) = field.side_room(i, self.pos, self.lane);
        if let Some(last_corner) = self.course.corners.last() {
            if self.extra_move_lane.is_none() && self.pos >= last_corner.start {
                self.extra_move_lane = Some((self.lane / 0.1).max(0.0).min(1.0) * 0.5 + self.rng.random() * 0.1);
            }
        }

        let moving_in = self.lane_target < self.lane;
        let blocked_moving = (if moving_in { inner_blocked } else { outer_blocked }) && (self.lane_target - self.lane).abs() > 1e-9;
        if !(self.lane_retarget || blocked_moving || (self.lane - self.lane_target).abs() < 0.5 * HORSE_LANE) {
            return;
        }
        self.lane_retarget = false;

        let targets = self.overtake_targets(field);
        let lane_modifier:f64=self.lane_modifiers.iter().filter(|(tm,_)|tm.as_ref().map(|t|t.get()<0.0).unwrap_or(true)).map(|(_,m)|m).sum();
        if lane_modifier!=0.0 {
            self.lane_target=(self.lane+lane_modifier).max(0.0).min(self.course.max_lane); return;
        }
        let mlp = self.move_lane_point();
        let forced_normal = self.phase <= 1 && mlp - 200.0 <= self.pos && self.pos < mlp;
        self.lane_target = if targets.is_empty() || forced_normal {
            self.normal_mode_lane(field, inner_blocked, outer_blocked)
        } else {
            self.overtake_mode_lane(field, &targets, inner_blocked, outer_blocked)
        };
        self.lane_target = self.lane_target.min(self.course.max_lane).max(0.0);
    }

    fn inside_neighbor_lane(&self, field: &RaceField) -> Option<f64> {
        let i = self.field_index.unwrap();
        let mut best: Option<f64> = None;
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            if o.finished || (o.pos - self.pos).abs() >= 1.05 {
                continue;
            }
            if o.lane < self.lane && best.map_or(true, |b| o.lane > b) {
                best = Some(o.lane);
            }
        }
        best
    }

    fn normal_mode_lane(&self, field: &RaceField, inner_blocked: bool, outer_blocked: bool) -> f64 {
        if !self.hp.has_remaining_hp() {
            return self.lane;
        }
        if self.is_pace_down {
            return 0.18;
        }
        if let Some(extra) = self.extra_move_lane {
            if self.in_final_straight() && extra > self.lane && !outer_blocked {
                return extra;
            }
        }
        if self.phase <= 1 && !inner_blocked {
            return (self.lane - 0.05).max(0.0);
        }
        if self.phase == 1 {
            if let Some(inside) = self.inside_neighbor_lane(field) {
                if self.lane - inside < 1.75 * HORSE_LANE {
                    return inside + 2.0 * HORSE_LANE;
                }
            }
        }
        self.lane
    }

    fn overtake_mode_lane(&self, field: &RaceField, targets: &[OtherView], inner_blocked: bool, outer_blocked: bool) -> f64 {
        let i = self.field_index.unwrap();
        let mut candidates: Vec<f64> = Vec::new();
        for t in targets {
            let mut inner = *t;
            let mut outer = *t;
            for j in 0..field.n {
                if j == i {
                    continue;
                }
                let c = field.solvers[j].borrow();
                if c.finished {
                    continue;
                }
                let dp = c.pos - t.pos;
                let dl = (c.lane - t.lane).abs();
                if (0.0..=3.0).contains(&dp) && 0.0 < dl && dl < 2.0 * HORSE_LANE {
                    if c.lane < inner.lane {
                        inner = OtherView { idx: j, pos: c.pos, lane: c.lane };
                    }
                    if c.lane > outer.lane {
                        outer = OtherView { idx: j, pos: c.pos, lane: c.lane };
                    }
                }
            }
            for (edge, cand) in [(inner, inner.lane - HORSE_LANE), (outer, outer.lane + HORSE_LANE)] {
                if 0.0 <= cand && cand <= self.course.max_lane && self.lane_clear(field, cand, self.pos, edge.pos + 0.5) {
                    candidates.push(cand);
                }
            }
        }
        let furthest = targets.iter().map(|t| t.pos).fold(f64::NEG_INFINITY, f64::max);
        let extra = if self.phase <= 1 && self.lane >= HORSE_LANE { self.lane - HORSE_LANE } else { self.lane };
        if self.lane_clear(field, extra, self.pos, furthest + 3.0) {
            candidates.push(extra);
        }
        let (in_coef, out_coef) = match self.phase {
            0 => (1.0, 100.0),
            1 => (1.0, 1.0),
            _ => (1.0, 1.15),
        };
        let mut best: Option<f64> = None;
        let mut best_score = 0.0;
        for &cand in &candidates {
            if cand < self.lane && inner_blocked {
                continue;
            }
            if cand > self.lane && outer_blocked {
                continue;
            }
            let score = (cand - self.lane).abs() * if cand < self.lane { in_coef } else { out_coef };
            if best.is_none() || score < best_score {
                best = Some(cand);
                best_score = score;
            }
        }
        let best = best.unwrap_or(self.lane);
        if let Some(extra) = self.extra_move_lane {
            if best < extra && !outer_blocked {
                return extra;
            }
        }
        best
    }

    fn update_lane(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => return,
        };
        let i = self.field_index.unwrap();
        self.update_lane_target(field);
        let lane_distance = (self.lane_target - self.lane).abs();
        if lane_distance < 1e-9 {
            self.lane_change_speed = 0.0;
            self.moved_lane_this_tick = false;
            return;
        }
        let before_mlp = self.phase <= 1 && self.pos < self.move_lane_point();
        let first_move_modifier = if before_mlp { 1.0 + self.lane / self.course.max_lane.max(1e-9) * 0.05 } else { 1.0 };
        let order_modifier = if self.phase >= 2 { 1.0 + field.order_of(i) as f64 * 0.01 } else { 1.0 };
        let target_lane_change_speed = 0.02 * (0.3 + 0.001 * self.horse.power) * first_move_modifier * order_modifier;
        if self.lane_change_speed < target_lane_change_speed {
            self.lane_change_speed = target_lane_change_speed.min(self.lane_change_speed + 0.03 * dt);
        } else {
            self.lane_change_speed = target_lane_change_speed.max(self.lane_change_speed - 0.03 * dt);
        }
        let moving_in = self.lane_target < self.lane;
        let mut actual_speed = (self.lane_change_speed + self.lane_speed_skill_modifier).min(0.6).max(0.0);
        if moving_in {
            actual_speed *= 1.0 + self.lane;
        }
        let (inner_blocked, outer_blocked) = field.side_room(i, self.pos, self.lane);
        if if moving_in { inner_blocked } else { outer_blocked } {
            self.moved_lane_this_tick = false;
            return;
        }
        let step = actual_speed * dt * if moving_in { -1.0 } else { 1.0 };
        if step.abs() >= lane_distance {
            self.lane = self.lane_target;
            self.lane_change_speed = 0.0;
        } else {
            self.lane += step;
        }
        self.lane = self.lane.min(self.course.max_lane).max(0.0);
        self.moved_lane_this_tick = step.abs() > 1e-9;
    }

    fn update_near_lane_timers(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => {
                self.infront_near_lane_timer.set(0.0);
                self.behind_near_lane_timer.set(0.0);
                return;
            }
        };
        let i = self.field_index.unwrap();
        let ahead_gap = field.distance_to_ahead(i, self.pos);
        let ahead_lane_gap = field.lane_gap_to_ahead(i, self.lane);
        let near_ahead = ahead_gap.is_some() && ahead_gap.unwrap().abs() < 2.5 && ahead_lane_gap.unwrap().abs() < HORSE_LANE;
        self.infront_near_lane_timer.set(if near_ahead { self.infront_near_lane_timer.get() + dt } else { 0.0 });

        let behind_gap = field.distance_to_behind(i, self.pos);
        let behind_lane_gap = field.lane_gap_to_behind(i, self.lane);
        let near_behind = behind_gap.is_some() && behind_gap.unwrap().abs() < 2.5 && behind_lane_gap.unwrap().abs() < HORSE_LANE;
        self.behind_near_lane_timer.set(if near_behind { self.behind_near_lane_timer.get() + dt } else { 0.0 });
    }


    fn roll_temptation_mode(&self) -> TemptationMode {
        let r = self.temptation_roll;
        match self.horse.strategy {
            Strategy::Nige | Strategy::Oonige => TemptationMode::Boost,
            Strategy::Senkou => TemptationMode::PositionNige,
            Strategy::Sasi => if r < 0.75 { TemptationMode::PositionNige } else { TemptationMode::PositionSenko },
            Strategy::Oikomi => {
                if r < 0.7 {
                    TemptationMode::PositionNige
                } else if r < 0.9 {
                    TemptationMode::PositionSenko
                } else {
                    TemptationMode::PositionSashi
                }
            }
        }
    }

    fn update_kakari(&mut self) {
        if self.temptation_count == 0 && self.pos >= self.kakari_start {
            self.is_kakari = true;
            self.temptation_count = 1;
            self.temptation_mode = self.roll_temptation_mode();
            self.kakari_timer.set(-self.kakari_duration);
            self.kakari_started_at = self.accumulatetime.get();
            self.exit_position_keep(false);
            self.pos_keep_timer.set(2.0);
            self.call_activate_hook("kakari", Perspective::Self_);
        } else if self.is_kakari && self.kakari_timer.get() >= 0.0 {
            self.is_kakari = false;
            self.temptation_mode = TemptationMode::Null;
            self.kakari_log.push((self.kakari_started_at, self.accumulatetime.get()));
            self.call_deactivate_hook("kakari", Perspective::Self_);
        }
    }


    pub fn ai_strategy(&self) -> Strategy {
        if self.is_kakari {
            match self.temptation_mode {
                TemptationMode::PositionSashi => return Strategy::Sasi,
                TemptationMode::PositionSenko => return Strategy::Senkou,
                TemptationMode::PositionNige => return Strategy::Nige,
                _ => {}
            }
        }
        self.horse.strategy
    }

    fn pos_keep_wit_check(&mut self, coef: f64) -> bool {
        if self.is_kakari {
            return true;
        }
        let pct = coef * (self.horse.wisdom.max(1.0) * 0.1).log10();
        self.pos_keep_rng.random() * 100.0 < pct
    }

    fn exit_position_keep(&mut self, cooldown: bool) {
        self.pos_keep_mode = None;
        self.is_pace_down = false;
        self.pos_keep_speed_coef = 1.0;
        if cooldown {
            self.pos_keep_timer.set(-1.0);
        }
    }

    fn enter_position_keep(&mut self, mode: PosKeepMode, coef: f64) {
        self.pos_keep_mode = Some(mode);
        self.pos_keep_speed_coef = coef;
        self.is_pace_down = mode == PosKeepMode::PaceDown;
        self.pos_keep_effect_start = self.pos;
    }

    fn speed_up_threshold(&self, field: &RaceField, strategy: Strategy) -> f64 {
        if field.is_solo_front_runner(self.field_index.unwrap()) {
            12.5
        } else if strategy == Strategy::Oonige {
            17.5
        } else {
            4.5
        }
    }

    fn lead_in_group(&self, field: &RaceField, strategy: Strategy) -> Option<f64> {
        let i = self.field_index.unwrap();
        let mut best_other: Option<f64> = None;
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            if !strategy_matches(o.ai_strategy(), strategy) {
                continue;
            }
            if o.pos > self.pos {
                return None;
            }
            if best_other.map_or(true, |b| o.pos > b) {
                best_other = Some(o.pos);
            }
        }
        Some(match best_other {
            None => f64::INFINITY,
            Some(b) => self.pos - b,
        })
    }

    fn pace_up_ex_wanted(&self, field: &RaceField, strategy: Strategy) -> bool {
        let i = self.field_index.unwrap();
        let mine = forwardness(strategy);
        if strategy_matches(strategy, Strategy::Nige) {
            for j in 0..field.n {
                if j == i {
                    continue;
                }
                let o = field.solvers[j].borrow();
                if !o.finished && o.pos > self.pos && forwardness(o.ai_strategy()) > mine {
                    return true;
                }
            }
            return false;
        }
        match field.pacemaker() {
            Some(pm) if pm != i => forwardness(field.solvers[pm].borrow().ai_strategy()) > mine,
            _ => false,
        }
    }

    fn update_position_keep(&mut self, field: Option<&RaceField>) {
        let field = match field {
            Some(f) if self.pos < self.pos_keep_end => f,
            _ => {
                if self.pos_keep_mode.is_some() {
                    self.exit_position_keep(false);
                }
                return;
            }
        };
        let i = self.field_index.unwrap();
        let strategy = self.ai_strategy();
        let front_runner = strategy_matches(strategy, Strategy::Nige);

        if let Some(mode) = self.pos_keep_mode {
            if self.pos - self.pos_keep_effect_start >= self.pos_keep_continue_distance {
                self.exit_position_keep(true);
                return;
            }
            let exit = match mode {
                PosKeepMode::PaceUpEx => !self.pace_up_ex_wanted(field, strategy),
                PosKeepMode::SpeedUp => {
                    let gap = if field.order_of(i) == 1 { field.distance_to_behind(i, self.pos) } else { None };
                    match gap {
                        None => true,
                        Some(g) => g >= self.speed_up_threshold(field, strategy),
                    }
                }
                PosKeepMode::Overtake => match self.lead_in_group(field, strategy) {
                    Some(lead) => lead >= if strategy == Strategy::Oonige { 27.5 } else { 10.0 },
                    None => false,
                },
                PosKeepMode::PaceUp => field.gap_to_pacemaker(i, self.pos) < self.pos_keep_effect_exit_distance,
                PosKeepMode::PaceDown => {
                    let speed_skill = !self.active_target_speed_skills.is_empty() || !self.active_current_speed_skills.is_empty();
                    field.gap_to_pacemaker(i, self.pos) > self.pos_keep_effect_exit_distance || speed_skill
                }
            };
            if exit {
                self.exit_position_keep(true);
            }
            return;
        }

        if self.pos_keep_timer.get() < 2.0 {
            return;
        }
        self.pos_keep_timer.set(0.0);

        if self.pace_up_ex_wanted(field, strategy) {
            self.enter_position_keep(PosKeepMode::PaceUpEx, 2.0);
            return;
        }
        if front_runner {
            if self.is_kakari && self.temptation_mode == TemptationMode::Boost {
                self.enter_position_keep(PosKeepMode::SpeedUp, 1.04);
                return;
            }
            if field.order_of(i) == 1 {
                if let Some(gap) = field.distance_to_behind(i, self.pos) {
                    if gap < self.speed_up_threshold(field, strategy) && self.pos_keep_wit_check(20.0) {
                        self.enter_position_keep(PosKeepMode::SpeedUp, 1.04);
                    }
                }
            } else if self.lead_in_group(field, strategy).is_none() && self.pos_keep_wit_check(20.0) {
                self.enter_position_keep(PosKeepMode::Overtake, 1.05);
            }
            return;
        }

        let pm = match field.pacemaker() {
            Some(pm) if pm != i => pm,
            _ => return,
        };
        let gap = field.solvers[pm].borrow().pos - self.pos;
        let lo = position_keep_min_threshold(strategy, self.course.distance);
        let hi = position_keep_max_threshold(strategy, self.course.distance);
        if gap > hi {
            if self.pos_keep_wit_check(15.0) {
                self.enter_position_keep(PosKeepMode::PaceUp, 1.04);
                self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo);
            }
        } else if gap < lo && !self.is_kakari && self.active_target_speed_skills.is_empty() && self.active_current_speed_skills.is_empty() {
            self.enter_position_keep(PosKeepMode::PaceDown, if self.phase == 1 { 0.945 } else { 0.915 });
            let exit_hi = if self.phase == 1 { lo + 0.5 * (hi - lo) } else { hi };
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (exit_hi - lo);
        }
    }

    fn update_last_spurt_state(&mut self) {
        if self.is_last_spurt || self.phase < 2 {
            return;
        }
        if self.last_spurt_transition == -1.0 {
            let (transition, speed) = self.hp.get_last_spurt_pair(self.pos, self.last_spurt_speed, self.base_target_speed[2]);
            self.last_spurt_transition = transition;
            self.last_spurt_speed = speed;
        }
        if self.pos >= self.last_spurt_transition {
            self.is_last_spurt = true;
            self.last_spurt_start_pos = self.pos;
        }
    }

    fn update_target_speed(&mut self, field: Option<&RaceField>) {
        if !self.hp.has_remaining_hp() {
            self.target_speed = self.min_speed;
        } else if self.is_last_spurt {
            self.target_speed = self.last_spurt_speed;
        } else {
            self.target_speed = self.base_target_speed[self.phase as usize] * self.pos_keep_speed_coef;
            let idx = (self.pos / self.section_length) as usize;
            self.target_speed += self.section_modifier[idx];
        }
        self.target_speed += self.modifiers_target_speed.acc + self.modifiers_target_speed.err;

        if self.phase == 0 && self.lane > 0.12 {
            if let Some(f) = field {
                let (inner_blocked, _) = f.side_room(self.field_index.unwrap(), self.pos, self.lane);
                if !inner_blocked {
                    self.target_speed += self.force_in_random + force_in_strategy(self.horse.strategy);
                }
            }
        }

        if self.is_downhill_mode {
            self.target_speed += 0.3 + self.slope_per / 100000.0;
        } else if self.hill_idx != -1 && self.slope_per > 0.0 {
            self.target_speed -= self.slope_per / 10000.0 * 200.0 / self.horse.power;
            self.target_speed = self.target_speed.max(self.min_speed);
        }

        if !self.active_lane_speed_skills.is_empty() && self.moved_lane_this_tick {
            self.target_speed += (0.0002 * self.horse.power).sqrt();
        }

        self.target_speed = self.target_speed.min(30.0);
    }

    fn apply_forces(&mut self) {
        if !self.hp.has_remaining_hp() {
            self.accel = -1.2;
            return;
        }
        if self.current_speed > self.target_speed {
            self.accel = if self.is_pace_down { -0.5 } else { PHASE_DECELERATION[self.phase as usize] };
            return;
        }
        self.accel = self.base_accel[(self.slope_per > 0.0) as usize * 3 + self.phase as usize];
        self.accel += self.modifiers_accel.acc + self.modifiers_accel.err;
    }

    fn downhill_check(&mut self, roll: f64) {
        if self.slope_per < 0.0 && roll < self.horse.wisdom * 0.0004 {
            self.call_activate_hook("downhill", Perspective::Self_);
            self.is_downhill_mode = true;
        }
    }

    fn update_hills(&mut self) {
        if self.hill_idx == -1 && self.hill_start.last().map_or(false, |&s| self.pos >= s) {
            self.hill_idx = (self.n_hills - self.hill_start.len()) as i64;
            self.slope_per = self.course.slopes[self.hill_idx as usize].slope;
            self.downhill_timer.set(0.0);
            let roll = self.hill_rng[self.hill_idx as usize].random();
            self.downhill_check(roll);
            self.hill_start.pop();
        } else if self.hill_idx != -1 && self.hill_end.last().map_or(false, |&e| self.pos > e) {
            self.hill_idx = -1;
            self.slope_per = 0.0;
            self.hill_end.pop();
            if self.is_downhill_mode {
                self.call_deactivate_hook("downhill", Perspective::Self_);
            }
            self.is_downhill_mode = false;
        }
        if self.downhill_timer.get() >= 1.0 && self.hill_idx != -1 {
            let roll = self.hill_rng[self.hill_idx as usize].random();
            if self.is_downhill_mode && roll > 0.8 {
                self.call_deactivate_hook("downhill", Perspective::Self_);
                self.is_downhill_mode = false;
            } else if !self.is_downhill_mode {
                self.downhill_check(roll);
            }
            self.downhill_timer.set(0.0);
        }
    }

    fn update_phase(&mut self) {
        if self.pos >= self.next_phase_transition && self.phase < 2 {
            self.phase += 1;
            self.next_phase_transition = crate::course_data::phase_start(self.course.distance, self.phase + 1);
        }
    }


    fn process_skill_activations(&mut self, field: Option<&RaceField>) {
        for i in (0..self.active_target_speed_skills.len()).rev() {
            if self.active_target_speed_skills[i].duration_timer.get() >= 0.0 {
                let s = self.active_target_speed_skills.remove(i);
                self.release_timer(&s.duration_timer);
                self.modifiers_target_speed.add(-s.modifier);
                self.call_deactivate_hook(&s.skill_id, s.perspective);
            }
        }
        for i in (0..self.active_current_speed_skills.len()).rev() {
            if self.active_current_speed_skills[i].duration_timer.get() >= 0.0 {
                let s = self.active_current_speed_skills.remove(i);
                self.release_timer(&s.duration_timer);
                self.modifiers_current_speed.add(-s.modifier);
                if s.natural_deceleration {
                    self.modifiers_one_frame_accel += s.modifier;
                }
                self.call_deactivate_hook(&s.skill_id, s.perspective);
            }
        }
        for i in (0..self.active_accel_skills.len()).rev() {
            if self.active_accel_skills[i].duration_timer.get() >= 0.0 {
                let s = self.active_accel_skills.remove(i);
                self.release_timer(&s.duration_timer);
                self.modifiers_accel.add(-s.modifier);
                self.call_deactivate_hook(&s.skill_id, s.perspective);
            }
        }
        for i in (0..self.active_lane_speed_skills.len()).rev() {
            if self.active_lane_speed_skills[i].duration_timer.get() >= 0.0 {
                let s = self.active_lane_speed_skills.remove(i);
                self.release_timer(&s.duration_timer);
                self.lane_speed_skill_modifier -= s.modifier;
                self.call_deactivate_hook(&s.skill_id, s.perspective);
            }
        }

        for i in (0..self.watchers.len()).rev() {
            if self.watchers[i].timer.get() >= 0.0 {
                let w = self.watchers.remove(i);
                self.release_timer(&w.timer);
            }
        }
        self.order_up_groups.retain(|g| !g.0.iter().all(|tm| tm.get() >= 0.0));

        if !self.cooling.is_empty() {
            let now = self.accumulatetime.get();
            let cooling = std::mem::take(&mut self.cooling);
            for (ready, sk) in cooling {
                if now >= ready {
                    if let Some(again) = sk.rearmed(self.pos) {
                        self.push_pending(again);
                    }
                } else {
                    self.cooling.push((ready, sk));
                }
            }
        }

        if self.armed_scan {
            for sk in &self.pending_skills {
                if self.pos < sk.trigger.start { (sk.extra_condition)(self, field); }
            }
            self.process_pending_armed(self.pos, field);
        } else {
            self.process_pending_full(self.pos, field);
        }
    }

    fn push_pending(&mut self, sk: PendingSkill) {
        let seq = self.next_seq;
        self.next_seq += 1;
        let start = sk.trigger.start;
        self.pending_skills.push(sk);
        self.pending_seq.push(seq);
        let at = self.pending_queue.partition_point(|&(st, _)| st > start);
        self.pending_queue.insert(at, (start, seq));
    }

    fn process_pending_armed(&mut self, pos: f64, field: Option<&RaceField>) {
        while let Some(&(start, _seq)) = self.pending_queue.last() {
            if start > pos {
                break;
            }
            let (_, seq) = self.pending_queue.pop().unwrap();
            let at = self.armed.partition_point(|&s| s < seq);
            self.armed.insert(at, seq);
        }

        let mut activate_count_this_frame = 0;
        let mut i = self.armed.len();
        while i > 0 {
            i -= 1;
            let seq = self.armed[i];
            let j = self.pending_seq.binary_search(&seq).expect("armed seq must be present in pending_seq");
            let trigger_end = self.pending_skills[j].trigger.end;
            if pos >= trigger_end {
                self.armed.remove(i);
                self.pending_skills.remove(j);
                self.pending_seq.remove(j);
                continue;
            }
            let cond = self.pending_skills[j].extra_condition.clone();
            let fires = cond(self, field);
            if fires {
                self.armed.remove(i);
                let sk = self.pending_skills.remove(j);
                self.pending_seq.remove(j);
                let is_special = sk.skill_id == "asitame" || sk.skill_id == "staminasyoubu";
                self.activate_skill(&sk.skill_id, sk.perspective, sk.rarity, &sk.effects, sk.green, field);
                if sk.cooldown > 0.0 {
                    self.cooling.push((self.accumulatetime.get() + sk.cooldown, sk));
                }
                if !is_special {
                    activate_count_this_frame += 1;
                }
            }
        }
        self.activate_count_last_frame = activate_count_this_frame;
    }

    fn process_pending_full(&mut self, pos: f64, field: Option<&RaceField>) {
        let mut activate_count_this_frame = 0;
        let mut i = self.pending_skills.len();
        while i > 0 {
            i -= 1;
            if i >= self.pending_skills.len() {
                continue;
            }
            let skill_id_matches_removal = self.pending_removal.contains(&self.pending_skills[i].skill_id);
            if pos >= self.pending_skills[i].trigger.end || skill_id_matches_removal {
                let removed = self.pending_skills.remove(i);
                self.pending_removal.remove(&removed.skill_id);
                continue;
            }
            let cond = self.pending_skills[i].extra_condition.clone();
            let fires = cond(self, field) && pos >= self.pending_skills[i].trigger.start;
            if fires {
                let (skill_id, perspective, rarity, effects, green) = {
                    let sk = &self.pending_skills[i];
                    (sk.skill_id.clone(), sk.perspective, sk.rarity, sk.effects.clone(), sk.green)
                };
                self.activate_skill(&skill_id, perspective, rarity, &effects, green, field);
                let sk = self.pending_skills.remove(i);
                if sk.cooldown > 0.0 {
                    self.cooling.push((self.accumulatetime.get() + sk.cooldown, sk));
                }
                if skill_id != "asitame" && skill_id != "staminasyoubu" {
                    activate_count_this_frame += 1;
                }
            }
        }
        self.activate_count_last_frame = activate_count_this_frame;
    }

    fn scaled_duration(&self, ef: &SkillEffect, rarity: SkillRarity, field: Option<&RaceField>) -> f64 {
        let mut base = ef.base_duration;
        if ef.time_usage == 2 {
            if let Some(f) = field {
                base *= (0.8 + f.distance_to_leader(self.field_index.unwrap(), self.pos) / 62.5).min(1.6);
            }
        } else if ef.time_usage == 3 || ef.time_usage == 7 {
            let hp = self.hp.hp_value().unwrap_or(0.0);
            let steps: &[(f64, f64)] = if ef.time_usage == 3 {
                &[(3500.0, 4.0), (3200.0, 3.5), (3000.0, 3.0), (2800.0, 2.5), (2600.0, 2.2), (2400.0, 2.0), (2000.0, 1.5)]
            } else {
                &[(2100.0, 3.0), (2000.0, 2.5), (1800.0, 2.0), (1500.0, 1.5)]
            };
            for &(bound, mult) in steps {
                if hp >= bound {
                    base *= mult;
                    break;
                }
            }
        } else if ef.time_usage == 5 {
            base *= bucket(self.mid_side_block_max, &BLOCKED_SIDE_MULT);
        }
        base * (self.course.distance / 1000.0) * if rarity == SkillRarity::Evolution { self.modifiers_special_skill_duration_scaling } else { 1.0 }
    }

    fn dynamic_value_multiplier(&self, ef: &SkillEffect) -> f64 {
        match ef.value_usage {
            20 => bucket(self.mid_side_block_max, &BLOCKED_SIDE_MULT),
            11 => bucket(self.corner_late_order_up as f64, &CORNER_LATE_ORDER_UP_MULT),
            14 => bucket(self.green_activations as f64, &GREEN_COUNT_MULT),
            22 => bucket(self.horse.speed, &SPEED1_MULT),
            23 => bucket(self.horse.speed, &SPEED2_MULT),
            _ => 1.0,
        }
    }

    fn in_corner(&self) -> bool {
        self.course.corners.iter().any(|c| c.start <= self.pos && self.pos < c.start + c.length)
    }

    fn update_overtake_effects(&mut self, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => return,
        };
        let i = self.field_index.unwrap();
        let gained = field.previous_order_of(i) as i64 - field.order_of(i) as i64;
        if gained <= 0 {
            return;
        }
        if self.phase >= 2 && self.in_corner() {
            self.corner_late_order_up += gained as i32;
        }
        let extend = self.course.distance / 1000.0;
        for _ in 0..gained {
            let mut fire: Vec<(SkillEffect, f64, String, Perspective)> = Vec::new();
            for w in self.watchers.iter_mut() {
                if w.kind == 1 && w.left > 0 && w.timer.get() < 0.0 {
                    w.left -= 1;
                    fire.push((w.effect, -w.timer.get(), w.skill_id.clone(), w.perspective));
                }
            }
            for (ef, remaining, sid, persp) in fire {
                self.apply_effect(&ef, remaining, &sid, persp, Some(field));
            }
            for g in self.order_up_groups.iter_mut() {
                if g.1 > 0 {
                    g.1 -= 1;
                    for tm in &g.0 {
                        if tm.get() < 0.0 {
                            tm.set(tm.get() - extend);
                        }
                    }
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn activate_skill(&mut self, skill_id: &str, perspective: Perspective, rarity: SkillRarity, effects: &[SkillEffect], green: bool, field: Option<&RaceField>) {
        let mut effects = effects.to_vec();
        effects.sort_by_key(|ef| (ef.effect_type == SkillType::ExtendEvolvedDuration) as u8);
        let special = skill_id == "asitame" || skill_id == "staminasyoubu";
        self.activation_group += 1;
        let group = self.activation_group;
        if !special {
            let mut fire: Vec<(SkillEffect, f64, String, Perspective)> = Vec::new();
            for w in self.watchers.iter_mut() {
                if (w.kind == 2 || w.kind == 3) && w.left > 0 && w.timer.get() < 0.0 && w.group != group {
                    w.left -= 1;
                    fire.push((w.effect, -w.timer.get(), w.skill_id.clone(), w.perspective));
                }
            }
            for (ef, remaining, sid, persp) in fire {
                self.apply_effect(&ef, remaining, &sid, persp, field);
            }
        }

        let log_index = self.activation_log.len();
        let mut targets: Vec<usize> = Vec::new();
        let mut timed_duration: Option<f64> = None;
        let mut group_timers: Vec<Timer> = Vec::new();
        let mut advantage: Vec<i32> = Vec::new();
        for ef in &effects {
            let mut ef = *ef;
            let mult = self.dynamic_value_multiplier(&ef);
            if mult != 1.0 {
                ef.modifier *= mult;
            }
            let scaled_duration = self.scaled_duration(&ef, rarity, field);
            if ef.effect_type.is_timed() {
                timed_duration = Some(timed_duration.unwrap_or(0.0).max(scaled_duration));
            }
            if ef.additional_activate != 0 {
                let tm = self.new_timer(-scaled_duration);
                group_timers.push(tm.clone());
                self.watchers.push(Watcher {
                    effect: ef,
                    skill_id: skill_id.to_string(),
                    perspective,
                    kind: ef.additional_activate,
                    left: additional_activate_limit(ef.additional_activate),
                    timer: tm,
                    group,
                });
                continue;
            }
            if ef.target != skill_target::SELF && !special {
                if let Some(f) = field {
                    for t in f.queue_external_effect(self.field_index.unwrap(), self.pos, self.lane, self.chara_id, self.visible_distance(), self.context.get("teamId").and_then(crate::json::Value::as_i64).unwrap_or(0), self.debuff_cancel, &ef, scaled_duration, skill_id) {
                        if !targets.contains(&t) {
                            targets.push(t);
                        }
                    }
                } else {
                    self.deferred_external.push((ef, scaled_duration, skill_id.to_string(), log_index));
                }
                continue;
            }
            if ef.modifier > 0.0 && ef.effect_type != SkillType::Noop {
                let t = ef.effect_type as i32;
                if !advantage.contains(&t) {
                    advantage.push(t);
                }
            }
            if let Some(tm) = self.apply_effect(&ef, scaled_duration, skill_id, perspective, field) {
                group_timers.push(tm);
            }
        }
        if !group_timers.is_empty() && effects.iter().any(|ef| ef.time_usage == 4) {
            self.order_up_groups.push((group_timers, 3));
        }
        if !special {
            for t in advantage {
                if !self.advantage_types.contains(&t) {
                    self.advantage_types.push(t);
                }
            }
        }
        if green {
            self.green_activations += 1;
        }

        if perspective == Perspective::Self_ {
            self.activate_count[self.phase as usize] += 1;
            if self.pos >= self.course.distance / 2.0 {
                self.activate_count_later_half += 1;
            }
        }
        self.used_skills.insert(skill_id.to_string());
        if skill_id == "asitame" {
            self.event_log.push((self.accumulatetime.get(), EVENT_RELEASE_CONSERVE_POWER));
        } else if skill_id != "staminasyoubu" {
            self.activation_log.push(Activation {
                t: self.accumulatetime.get(),
                skill_id: skill_id.to_string(),
                has_stat_effect: effects.iter().any(|ef| ef.effect_type.is_stat()),
                targets,
                timed_duration,
            });
        }
        self.call_activate_hook(skill_id, perspective);
    }

    pub fn apply_effect(&mut self, ef: &SkillEffect, scaled_duration: f64, skill_id: &str, perspective: Perspective, field: Option<&RaceField>) -> Option<Timer> {
        match ef.effect_type {
            SkillType::Noop => {}
            SkillType::Oonige => {if self.horse.strategy==Strategy::Nige {self.horse.strategy=Strategy::Oonige; self.pos_keep_continue_distance=self.section_length.floor()*3.0;}}
            SkillType::VisibleDistance => {
                let timer=if scaled_duration>0.0 {Some(self.new_timer(-scaled_duration))} else {None};
                self.vision_modifiers.push((timer.clone(),ef.modifier)); return timer;
            }
            SkillType::TargetLane => {
                let timer=if scaled_duration>0.0 {Some(self.new_timer(-scaled_duration))} else {None};
                self.lane_modifiers.push((timer.clone(),ef.modifier)); self.lane_retarget=true; return timer;
            }
            SkillType::DebuffCancel => self.debuff_cancel=true,
            SkillType::SpeedUp => self.horse.speed = (self.horse.speed + ef.modifier).max(1.0),
            SkillType::StaminaUp => {
                self.horse.stamina = (self.horse.stamina + ef.modifier).max(1.0);
                self.horse.raw_stamina = (self.horse.raw_stamina + ef.modifier).max(1.0);
            }
            SkillType::PowerUp => self.horse.power = (self.horse.power + ef.modifier).max(1.0),
            SkillType::GutsUp => self.horse.guts = (self.horse.guts + ef.modifier).max(1.0),
            SkillType::WisdomUp => self.horse.wisdom = (self.horse.wisdom + ef.modifier).max(1.0),
            SkillType::AllStatusUp => {
                self.horse.speed = (self.horse.speed + ef.modifier).max(1.0);
                self.horse.stamina = (self.horse.stamina + ef.modifier).max(1.0);
                self.horse.raw_stamina = (self.horse.raw_stamina + ef.modifier).max(1.0);
                self.horse.power = (self.horse.power + ef.modifier).max(1.0);
                self.horse.guts = (self.horse.guts + ef.modifier).max(1.0);
                self.horse.wisdom = (self.horse.wisdom + ef.modifier).max(1.0);
            }
            SkillType::MultiplyStartDelay => self.start_delay *= ef.modifier,
            SkillType::ExtendKakari => {
                if self.is_kakari {
                    self.kakari_timer.set(self.kakari_timer.get() - ef.modifier);
                }
            }
            SkillType::SetStartDelay => self.start_delay = ef.modifier,
            SkillType::TargetSpeed => {
                self.modifiers_target_speed.add(ef.modifier);
                let timer = self.new_timer(-scaled_duration);
                self.active_target_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer.clone(), modifier: ef.modifier, natural_deceleration: false });
                return Some(timer);
            }
            SkillType::ModifyKakariChance => self.modifiers_kakari_chance += ef.modifier / 100.0,
            SkillType::Accel => {
                self.modifiers_accel.add(ef.modifier);
                let timer = self.new_timer(-scaled_duration);
                self.active_accel_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer.clone(), modifier: ef.modifier, natural_deceleration: false });
                return Some(timer);
            }
            SkillType::CurrentSpeed | SkillType::CurrentSpeedWithNaturalDeceleration => {
                self.modifiers_current_speed.add(ef.modifier);
                let timer = self.new_timer(-scaled_duration);
                let natural_deceleration = ef.effect_type == SkillType::CurrentSpeedWithNaturalDeceleration;
                self.active_current_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer.clone(), modifier: ef.modifier, natural_deceleration });
                return Some(timer);
            }
            SkillType::Recovery => {
                self.hp.recover(ef.modifier);
                if ef.modifier > 0.0 {
                    if perspective == Perspective::Self_ {
                        self.activate_count_heal += 1;
                    }
                    self.stamina_keep_active = false;
                    if self.phase >= 2 && !self.is_last_spurt {
                        self.last_spurt_transition = -1.0;
                        self.update_last_spurt_state();
                    }
                }
            }
            SkillType::LaneMoveSpeedUp => {
                self.lane_speed_skill_modifier += ef.modifier;
                let timer = self.new_timer(-scaled_duration);
                self.active_lane_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer.clone(), modifier: ef.modifier, natural_deceleration: false });
                return Some(timer);
            }
            SkillType::ActivateRandomGold => self.do_activate_random_gold(ef.modifier, field),
            SkillType::ExtendEvolvedDuration => self.modifiers_special_skill_duration_scaling = ef.modifier,
        }
        None
    }

    fn do_activate_random_gold(&mut self, ngolds: f64, field: Option<&RaceField>) {
        let mut gold_indices: Vec<usize> = self
            .pending_skills
            .iter()
            .enumerate()
            .filter(|(_, sk)| matches!(sk.rarity, SkillRarity::Gold | SkillRarity::Evolution) && sk.effects.iter().all(|ef| ef.effect_type as i32 > SkillType::WisdomUp as i32
                && ef.effect_type != SkillType::AllStatusUp))
            .map(|(i, _)| i)
            .collect();
        for i in (0..gold_indices.len()).rev() {
            let j = self.gorosi_rng.uniform((i + 1) as f64) as usize;
            gold_indices.swap(i, j);
        }
        let take = (ngolds as usize).min(gold_indices.len());
        for &idx in gold_indices.iter().take(take) {
            let (skill_id, perspective, rarity, effects) = {
                let sk = &self.pending_skills[idx];
                (sk.skill_id.clone(), sk.perspective, sk.rarity, sk.effects.clone())
            };
            let green = self.pending_skills[idx].green;
            self.activate_skill(&skill_id, perspective, rarity, &effects, green, field);
            self.pending_removal.insert(skill_id);
        }
    }


    fn update_competition_mechanics(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => return,
        };
        self.update_stamina_keep();
        self.update_compete_top_exit();
        self.update_compete_before_spurt(field);
        self.update_secure_lead(field);
        self.update_dueling(dt, field);
    }

    fn update_stamina_keep(&mut self) {
        if self.stamina_keep_active || self.stamina_keep_check.get() < 2.0 || self.is_last_spurt {
            return;
        }
        self.stamina_keep_check.set(0.0);
        let (hp_now, need) = match &self.hp {
            HpPolicy::Game(g) => (g.hp, g.hp_needed_to_finish(self.pos, self.base_target_speed[1], self.last_spurt_speed)),
            HpPolicy::Noop => return,
        };
        if hp_now >= need * self.stamina_keep_factor {
            return;
        }
        let wisdom = self.horse.wisdom.max(1.0);
        let pct = 30.0 * (wisdom / 1000.0 + wisdom.powf(0.03));
        if self.compete_rng.random() * 100.0 < pct {
            self.stamina_keep_active = true;
        }
    }

    pub fn start_compete_top(&mut self) {
        let guts = self.horse.guts.max(1.0);
        let strat_prof = STRATEGY_PROFICIENCY_MODIFIER_APPROX[self.horse.strategy_aptitude as usize];
        self.compete_top_modifier = (500.0 * guts).powf(0.6) * 0.0001;
        self.compete_top_timer.set(-(700.0 * guts).powf(0.5) * 0.012 * strat_prof);
        self.compete_top_active = true;
        self.modifiers_target_speed.add(self.compete_top_modifier);
        self.event_log.push((self.accumulatetime.get(), EVENT_COMPETE_TOP));
    }

    pub fn end_compete_top(&mut self) {
        if self.compete_top_active {
            self.compete_top_active = false;
            self.modifiers_target_speed.add(-self.compete_top_modifier);
        }
    }

    fn update_compete_top_exit(&mut self) {
        if self.compete_top_active && (self.compete_top_timer.get() >= 0.0 || self.pos >= self.section_length * 8.0) {
            self.end_compete_top();
        }
    }

    fn update_compete_before_spurt(&mut self, field: &RaceField) {
        if self.compete_spurt_active {
            if self.compete_spurt_timer.get() >= 0.0 {
                self.compete_spurt_active = false;
                self.compete_spurt_check.set(-1.0);
                self.modifiers_target_speed.add(-self.compete_spurt_modifier);
            }
            return;
        }
        if !(self.section_length * 10.0 <= self.pos && self.pos < self.section_length * 15.0) {
            return;
        }
        if self.compete_spurt_check.get() < 2.0 {
            return;
        }
        self.compete_spurt_check.set(0.0);
        if self.stamina_keep_active {
            return;
        }
        let i = self.field_index.unwrap();
        let strategy = self.horse.strategy;
        let far_from_lead = field.distance_to_leader(i, self.pos) > compete_dist_threshold(strategy);
        let near = field.near_count(i, self.pos, self.lane) > 0;
        if !(far_from_lead || near) {
            return;
        }
        let pct = 15.0 * (self.horse.wisdom.max(1.0) * 0.1).log10();
        if !(self.compete_rng.random() * 100.0 < pct) {
            return;
        }
        let mut solo_bonus = 1.0;
        if (strategy == Strategy::Nige || strategy == Strategy::Oonige) && field.is_solo_front_runner(i) {
            solo_bonus = if strategy == Strategy::Oonige { 2.0 } else { 1.1 };
        }
        let modifier = ((self.horse.power.max(0.0) / 1500.0).powf(0.5) * 2.0 + (self.horse.guts.max(0.0) / 3000.0).powf(0.2)) * 0.1 * compete_strategy_coef(strategy) * solo_bonus;
        let near_factor = if near && !far_from_lead { 0.5 } else { 0.0 };
        let stamina_cost = 20.0 * (compete_stamina_strategy_coef(strategy) * competition_distance_coefficient(self.course.distance) + near_factor);
        self.compete_spurt_active = true;
        self.compete_spurt_modifier = modifier;
        self.compete_spurt_timer.set(-2.0);
        self.modifiers_target_speed.add(modifier);
        if let Some(hp) = self.hp.hp_mut() {
            *hp -= stamina_cost;
        }
    }

    fn update_secure_lead(&mut self, field: &RaceField) {
        if self.secure_lead_active {
            if self.secure_lead_timer.get() >= 0.0 {
                self.secure_lead_active = false;
                self.secure_lead_check.set(-1.0);
                self.modifiers_target_speed.add(-self.secure_lead_modifier);
            }
            return;
        }
        if !(self.section_length * 10.0 <= self.pos && self.pos < self.section_length * 15.0) {
            return;
        }
        if self.secure_lead_check.get() < 2.0 {
            return;
        }
        self.secure_lead_check.set(0.0);
        let strategy = self.horse.strategy;
        if self.stamina_keep_active || !has_secure_lead_row(strategy) {
            return;
        }
        let i = self.field_index.unwrap();
        let mut threatened = false;
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            let coef = match secure_lead_coef(strategy, o.horse.strategy) {
                Some(c) => c,
                None => continue,
            };
            if o.pos > self.pos {
                continue;
            }
            if self.pos - o.pos < coef + 0.0003 * (self.course.distance + 1000.0) {
                threatened = true;
                break;
            }
        }
        if !threatened || self.compete_rng.random() >= 0.20 {
            return;
        }
        let mut solo_bonus = 1.0;
        if (strategy == Strategy::Nige || strategy == Strategy::Oonige) && field.is_solo_front_runner(i) {
            solo_bonus = if strategy == Strategy::Oonige { 7.0 } else { 2.0 };
        }
        let modifier = (self.horse.guts.max(0.0) / 2000.0).powf(0.5) * 0.3 * secure_lead_strategy_coef(strategy) * solo_bonus;
        let stamina_cost = 20.0 * secure_lead_stamina_strategy_coef(strategy) * competition_distance_coefficient(self.course.distance);
        self.secure_lead_active = true;
        self.secure_lead_modifier = modifier;
        self.secure_lead_timer.set(-2.0);
        self.modifiers_target_speed.add(modifier);
        if let Some(hp) = self.hp.hp_mut() {
            *hp -= stamina_cost;
        }
    }

    fn update_dueling(&mut self, dt: f64, field: &RaceField) {
        if self.course.straights.is_empty() {
            return;
        }
        let in_final_straight = self.in_final_straight();
        if self.duel_active {
            if self.hp.hp_ratio_remaining() < 0.05 {
                self.duel_active = false;
                self.modifiers_target_speed.add(-self.duel_target_speed_modifier);
                self.modifiers_accel.add(-self.duel_accel_modifier);
            }
            return;
        }
        if !in_final_straight || self.hp.hp_ratio_remaining() < 0.15 {
            self.duel_continuous_timer = 0.0;
            self.duel_target = -1;
            return;
        }
        let i = self.field_index.unwrap();
        let mut target: Option<(usize, f64)> = None;
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let o = field.solvers[j].borrow();
            if o.finished {
                continue;
            }
            if (o.pos - self.pos).abs() < 3.0 && (o.lane - self.lane).abs() < 0.25 && (target.is_none() || j as i64 == self.duel_target) {
                target = Some((j, o.current_speed));
                if j as i64 == self.duel_target {
                    break;
                }
            }
        }
        let (target_idx, target_speed) = match target {
            Some(t) => t,
            None => {
                self.duel_continuous_timer = 0.0;
                self.duel_target = -1;
                return;
            }
        };
        if target_idx as i64 != self.duel_target {
            self.duel_target = target_idx as i64;
            self.duel_continuous_timer = 0.0;
        }
        self.duel_continuous_timer += dt;
        if self.duel_continuous_timer < 2.0 {
            return;
        }
        let top_half = field.order_of(i) <= (field.n / 2).max(1);
        if !(top_half && (target_speed - self.current_speed).abs() < 0.6) {
            return;
        }
        let guts = self.horse.guts.max(1.0);
        self.duel_target_speed_modifier = (200.0 * guts).powf(0.708) * 0.0001;
        self.duel_accel_modifier = (160.0 * guts).powf(0.59) * 0.0001;
        self.duel_active = true;
        self.modifiers_target_speed.add(self.duel_target_speed_modifier);
        self.modifiers_accel.add(self.duel_accel_modifier);
        self.event_log.push((self.accumulatetime.get(), EVENT_COMPETE_FIGHT));
    }

    pub fn set_on_skill_activate(&mut self, hook: SkillHook) {
        self.on_skill_activate = hook;
    }

    pub fn set_on_skill_deactivate(&mut self, hook: SkillHook) {
        self.on_skill_deactivate = hook;
    }

    pub fn cleanup(&mut self) {
        let pending: Vec<(String, Perspective)> = self
            .active_target_speed_skills
            .iter()
            .chain(self.active_current_speed_skills.iter())
            .chain(self.active_accel_skills.iter())
            .chain(self.active_lane_speed_skills.iter())
            .map(|s| (s.skill_id.clone(), s.perspective))
            .collect();
        for (skill_id, perspective) in pending {
            self.call_deactivate_hook(&skill_id, perspective);
        }
        if self.is_downhill_mode {
            self.call_deactivate_hook("downhill", Perspective::Self_);
        }
    }
}

pub fn is_visible_from(pos: f64, lane: f64, other_pos: f64, other_lane: f64) -> bool {
    let dist_gap = other_pos - pos;
    if !(0.0..=VISIBLE_DISTANCE).contains(&dist_gap) {
        return false;
    }
    let half_width = ((dist_gap / VISIBLE_DISTANCE) * 11.5 * HORSE_LANE + 2.0 * HORSE_LANE) / 2.0;
    (other_lane - lane).abs() <= half_width
}
