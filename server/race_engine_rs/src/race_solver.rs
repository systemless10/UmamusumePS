//! Port of race_solver.py -- the core per-tick physics/skill-activation
//! loop. Uses the same velocity-Verlet-style integration, the same three
//! compensated-sum (Kahan-Babuska-Neumaier) accumulators for skill
//! modifiers, and precomputes `base_target_speed`/`base_accel` arrays
//! exactly once at construction time (a subtlety worth flagging: green/
//! stat-boost skills that fire mid-race mutate `self.horse` but do NOT
//! retroactively change these precomputed arrays, matching the original
//! engine's behavior bit-for-bit).
//!
//! `field: Option<&RaceField>` is threaded as an explicit parameter through
//! every method that needs multi-horse awareness (matching Python's
//! `self.field`, which the field-coordination layer -- not part of the
//! original engine -- sets once the whole roster is built) rather than
//! stored on the struct, so this file needs no self-referential pointer or
//! unsafe code; see race_field.rs's module doc for why.

use std::cell::Cell;
use std::collections::HashSet;
use std::rc::Rc;

use crate::activation_conditions::DynCond;
use crate::course_data::CourseData;
use crate::horse_types::{strategy_matches, HorseParameters, Strategy};
use crate::hp_policy::{HpPolicy, HpTickCtx};
use crate::race_field::{RaceField, HORSE_LANE};
use crate::random_gen::Rule30CARng;
use crate::region::Region;

// ---------------------------------------------------------------------------
// Speed / acceleration tables (Object.freeze([...]) in RaceSolver.ts)

// index 0 unused (strategies are 1-indexed)
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
    [0.945, 1.0, 0.967],
    [1.17, 0.94, 0.956],
];
const ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER: [f64; 8] = [1.05, 1.0, 0.9, 0.8, 0.7, 0.5, 0.3, 0.1];
const ACCEL_DISTANCE_PROFICIENCY_MODIFIER: [f64; 8] = [1.0, 1.0, 1.0, 1.0, 1.0, 0.6, 0.5, 0.4];

const BASE_ACCEL: f64 = 0.0006;
const UPHILL_BASE_ACCEL: f64 = 0.0004;

fn calc_base_accel(base_accel_const: f64, horse: &HorseParameters, phase: usize) -> f64 {
    base_accel_const
        * (500.0 * horse.power).sqrt()
        * ACCEL_STRATEGY_PHASE_COEFFICIENT[horse.strategy as usize][phase]
        * ACCEL_GROUND_TYPE_PROFICIENCY_MODIFIER[horse.surface_aptitude as usize]
        * ACCEL_DISTANCE_PROFICIENCY_MODIFIER[horse.distance_aptitude as usize]
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
// Secure Lead's real DesirableLead formula uses a full per-strategy-pair
// coefficient matrix the doc summary this port was built from only
// captured a few example entries of -- approximated with one flat
// coefficient instead of reconstructing a guessed-at full matrix.
const SECURE_LEAD_COEF_APPROX: f64 = 3.0;
fn secure_lead_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 0.2,
        Strategy::Nige => 1.0,
        Strategy::Senkou => 1.0,
        Strategy::Sasi => 0.8,
        Strategy::Oikomi => 1.0,
    }
}
fn secure_lead_stamina_strategy_coef(s: Strategy) -> f64 {
    match s {
        Strategy::Oonige => 1.2,
        Strategy::Nige => 1.0,
        Strategy::Senkou => 0.8,
        Strategy::Sasi => 0.8,
        Strategy::Oikomi => 0.8,
    }
}

/// Shared course-length coefficient table (doc: identical brackets used by
/// both Compete Before Spurt and Secure Lead's stamina-cost formulas).
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

// JS's arrays only have indices 0-4 (Oonige=5 reads as `undefined`, silently
// NaN-poisoning threshold math that is never actually read for Oonige/Nige
// horses since position-keep is a no-op for them). Index 5 is NaN so Rust
// indexing doesn't panic where JS wouldn't have crashed either.
const POSITION_KEEP_BASE_MIN_THRESHOLD: [f64; 6] = [0.0, 0.0, 3.0, 6.5, 7.5, f64::NAN];
const POSITION_KEEP_BASE_MAX_THRESHOLD: [f64; 6] = [0.0, 0.0, 5.0, 7.0, 8.0, f64::NAN];

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

// ---------------------------------------------------------------------------

pub type Timer = Rc<Cell<f64>>;

fn new_timer_value(t: f64) -> Timer {
    Rc::new(Cell::new(t))
}

/// Kahan-Babuska-Neumaier sum: for any sequence, adding a value in any order
/// interleaved with subtracting it in any other order results in
/// acc+err == 0.0 (needed since skills add/remove modifiers at arbitrary
/// times over a long race).
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
}

pub struct PendingSkill {
    pub skill_id: String,
    pub perspective: Perspective,
    pub rarity: SkillRarity,
    pub trigger: Region,
    pub extra_condition: DynCond,
    pub effects: Vec<SkillEffect>,
}

struct ActiveSkill {
    skill_id: String,
    perspective: Perspective,
    duration_timer: Timer,
    modifier: f64,
    natural_deceleration: bool,
}

pub type SkillHook = Box<dyn FnMut(&mut RaceSolver, &str, Perspective)>;

fn noop_hook() -> SkillHook {
    Box::new(|_s, _id, _p| {})
}

pub struct RaceSolver {
    pub horse: HorseParameters,
    pub course: &'static CourseData,
    pub hp: HpPolicy,
    pub pacer: Option<Box<RaceSolver>>,
    pub rng: Rule30CARng,
    pub gate_index: i32,
    /// Set once the whole roster's `RaceField` is built. `None` during the
    /// very first ("gate skill") activation round inside `new()`, since no
    /// other horse exists yet at that point.
    pub field_index: Option<usize>,

    pub pending_skills: Vec<PendingSkill>,
    pub pending_removal: HashSet<String>,
    /// Skills sorted by trigger position, so the per-tick scan only looks at
    /// the ones the horse has actually reached instead of re-testing every
    /// unfired skill on all ~1800 frames. Only valid when `pending_removal`
    /// can never be populated -- see `armed_scan`'s construction-time check.
    armed_scan: bool,
    /// Parallel to `pending_skills`: `pending_seq[j]` is the original
    /// insertion-order sequence number of `pending_skills[j]`. Stays sorted
    /// ascending as items are removed (both shrink together), which is what
    /// lets a `seq` be mapped back to its current index via binary search.
    pending_seq: Vec<usize>,
    /// Sequence numbers of currently-armed (trigger.start already reached)
    /// skills, kept sorted ascending; scanned in descending order (nearest
    /// to insertion order last) to match the full-scan's iteration order.
    armed: Vec<usize>,
    /// (trigger_start, seq) for not-yet-armed skills, sorted descending by
    /// trigger_start so `.pop()` yields the nearest one.
    pending_queue: Vec<(f64, usize)>,

    pub used_skills: HashSet<String>,
    gorosi_rng: Rule30CARng,
    pace_effect_rng: Rule30CARng,

    pub timers: Vec<Timer>,
    pub accumulatetime: Timer,

    pub gate_roll: i64,
    pub random_lot: i64,
    pub phase: i32,
    next_phase_transition: f64,

    active_target_speed_skills: Vec<ActiveSkill>,
    active_current_speed_skills: Vec<ActiveSkill>,
    active_accel_skills: Vec<ActiveSkill>,
    /// Doc: Lane Move Speed Up (ability type 28) -- not a modifier added on
    /// activation; while active AND the horse moved lane on the PREVIOUS
    /// frame, MoveLaneModifier = sqrt(0.0002*Power) is added to target
    /// speed each such tick (see update_target_speed).
    active_lane_speed_skills: Vec<ActiveSkill>,
    pub activate_count: [i32; 3],
    pub activate_count_heal: i32,
    // Activations past the halfway mark of the course -- the DISTANCE half,
    // not a phase; see activation_conditions' activate_count_later_half.
    pub activate_count_later_half: i32,
    pub activate_count_last_frame: i32,

    on_skill_activate: SkillHook,
    on_skill_deactivate: SkillHook,

    section_length: f64,
    pub is_pace_down: bool,
    pos_keep_min_threshold: f64,
    pos_keep_max_threshold: f64,
    pos_keep_cooldown: Timer,
    pos_keep_end: f64,
    pos_keep_speed_coef: f64,
    pos_keep_mode: Option<PosKeepMode>,
    pos_keep_effect_exit_distance: f64,
    pos_keep_effect_start: f64,

    modifiers_target_speed: CompensatedAccumulator,
    modifiers_current_speed: CompensatedAccumulator,
    modifiers_accel: CompensatedAccumulator,
    modifiers_one_frame_accel: f64,
    modifiers_special_skill_duration_scaling: f64,
    modifiers_kakari_chance: f64,

    pub start_delay: f64,
    pub pos: f64,
    accel: f64,
    pub current_speed: f64,
    target_speed: f64,
    is_downhill_mode: bool,
    pub is_last_spurt: bool,

    pub lane: f64,
    lane_target: f64,
    lane_change_speed: f64,
    extra_move_lane: Option<f64>,
    pub blocked_front_timer: Cell<f64>,
    pub blocked_side_timer: Cell<f64>,
    pub blocked_all_timer: Cell<f64>,
    pub infront_near_lane_timer: Cell<f64>,
    pub behind_near_lane_timer: Cell<f64>,
    pub moved_lane_this_tick: bool,

    spot_struggle_active: bool,
    spot_struggle_timer: Timer,
    spot_struggle_modifier: f64,
    compete_spurt_active: bool,
    compete_spurt_timer: Timer,
    compete_spurt_cooldown: Timer,
    compete_spurt_modifier: f64,
    secure_lead_active: bool,
    secure_lead_timer: Timer,
    secure_lead_cooldown: Timer,
    secure_lead_modifier: f64,
    stamina_keep_active: bool,
    duel_continuous_timer: f64,
    duel_active: bool,
    duel_timer: Timer,
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
    pub temptation_count: i32,

    section_modifier: Vec<f64>,
    base_accel: [f64; 6],


    pub order_band_in_200: Cell<Option<f64>>,
    pub order_band_in_400: Cell<Option<f64>>,
    pub order_band_in_500: Cell<Option<f64>>,
    pub order_band_in_800: Cell<Option<f64>>,
    pub order_band_out_200: Cell<Option<f64>>,
    pub order_band_out_400: Cell<Option<f64>>,
    pub order_band_out_500: Cell<Option<f64>>,
    pub order_band_out_700: Cell<Option<f64>>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PosKeepMode {
    PaceDown,
    PaceUp,
    SpeedUp,
    Overtake,
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
        let mut pending_queue: Vec<(f64, usize)> = skills.iter().enumerate().map(|(seq, sk)| (sk.trigger.start, seq)).collect();
        // descending: pop() yields the nearest
        pending_queue.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());

        let gorosi_rng = Rule30CARng::new(rng.int32(), 0);
        let pace_effect_rng = Rule30CARng::new(rng.int32(), 0);

        let strategy = horse.strategy;
        let distance = course.distance;

        let mut solver = RaceSolver {
            horse,
            course,
            hp,
            pacer,
            rng,
            gate_index,
            field_index: None,

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
            accumulatetime: new_timer_value(0.0), // placeholder, replaced below

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

            section_length: distance / 24.0,
            is_pace_down: false,
            pos_keep_min_threshold: position_keep_min_threshold(strategy, distance),
            pos_keep_max_threshold: position_keep_max_threshold(strategy, distance),
            pos_keep_cooldown: new_timer_value(0.0),
            pos_keep_end: distance / 24.0 * 10.0,
            pos_keep_speed_coef: 1.0,
            pos_keep_mode: None,
            pos_keep_effect_exit_distance: 0.0,
            pos_keep_effect_start: 0.0,

            modifiers_target_speed: CompensatedAccumulator::new(0.0),
            modifiers_current_speed: CompensatedAccumulator::new(0.0),
            modifiers_accel: CompensatedAccumulator::new(0.0),
            modifiers_one_frame_accel: 0.0,
            modifiers_special_skill_duration_scaling: 1.0,
            modifiers_kakari_chance: 0.0,

            start_delay: 0.0,
            pos: 0.0,
            accel: 0.0,
            current_speed: 3.0,
            target_speed: 0.85 * base_speed(course),
            is_downhill_mode: false,
            is_last_spurt: false,

            lane: gate_index as f64 * (1.0 / 18.0),
            lane_target: 0.0,
            lane_change_speed: 0.0,
            extra_move_lane: None,
            blocked_front_timer: Cell::new(0.0),
            blocked_side_timer: Cell::new(0.0),
            blocked_all_timer: Cell::new(0.0),
            infront_near_lane_timer: Cell::new(0.0),
            behind_near_lane_timer: Cell::new(0.0),
            moved_lane_this_tick: false,

            spot_struggle_active: false,
            spot_struggle_timer: new_timer_value(0.0),
            spot_struggle_modifier: 0.0,
            compete_spurt_active: false,
            compete_spurt_timer: new_timer_value(0.0),
            compete_spurt_cooldown: new_timer_value(0.0),
            compete_spurt_modifier: 0.0,
            secure_lead_active: false,
            secure_lead_timer: new_timer_value(0.0),
            secure_lead_cooldown: new_timer_value(0.0),
            secure_lead_modifier: 0.0,
            stamina_keep_active: false,
            duel_continuous_timer: 0.0,
            duel_active: false,
            duel_timer: new_timer_value(0.0),
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
            temptation_count: 0,

            section_modifier: Vec::new(),
            base_accel: [0.0; 6],


            order_band_in_200: Cell::new(None),
            order_band_in_400: Cell::new(None),
            order_band_in_500: Cell::new(None),
            order_band_in_800: Cell::new(None),
            order_band_out_200: Cell::new(None),
            order_band_out_400: Cell::new(None),
            order_band_out_500: Cell::new(None),
            order_band_out_700: Cell::new(None),
        };

        solver.accumulatetime = solver.new_timer(0.0);

        // Competition-mechanics timers (see each `update_*` method below):
        // must be registered via `new_timer()` -- not left as the bare
        // placeholder `Cell` the struct literal above gives them -- so
        // `step()`'s per-frame `for tm in &self.timers` loop actually
        // advances them. Without this they never tick, so a timer-based
        // exit condition (e.g. `spot_struggle_timer.get() >= 0.0`) never
        // fires and the mechanic never turns back off once triggered.
        solver.spot_struggle_timer = solver.new_timer(0.0);
        solver.compete_spurt_timer = solver.new_timer(0.0);
        solver.compete_spurt_cooldown = solver.new_timer(0.0);
        solver.secure_lead_timer = solver.new_timer(0.0);
        solver.secure_lead_cooldown = solver.new_timer(0.0);
        solver.duel_timer = solver.new_timer(0.0);

        // Fields set from `rng`, matching Python's exact draw order.
        solver.pos_keep_cooldown = solver.new_timer(0.0);
        // bit of a hack, see RaceSolver.ts's comment on gateRoll: n%k is
        // uniformly distributed for a random n only when n_max === k-1 (mod
        // k) for every k; the smallest such n_max for k in [1,18] is
        // lcm(1..18)-1, so draw up to lcm(1..18).
        solver.gate_roll = solver.rng_uniform(12252240.0) as i64;
        solver.random_lot = solver.rng_uniform(100.0) as i64;

        // must come before the first round of skill activations so concen
        // etc can modify it
        solver.start_delay = 0.1 * solver.rng.random();
        if let Some(p) = solver.pacer.as_mut() {
            p.start_delay = 0.0;
        }

        // gate skills (before minSpeed: green skills can modify guts).
        // Must run -- and `init_hills()` below must run -- before the
        // kakari/section_modifier rng draws further down: this is the exact
        // rng-consumption order race_solver.py's `__init__` uses, and this
        // whole block only exists to keep the two engines' rng streams in
        // lockstep (init_hills() draws 2 * len(course.slopes) rng.int32()
        // calls to seed hill_rng, plus a downhill_check() roll per hill
        // starting at the gate).
        solver.process_skill_activations(None);
        solver.min_speed = 0.85 * base_speed(solver.course) + (200.0 * solver.horse.guts).sqrt() * 0.001;
        solver.start_dash = true;
        solver.modifiers_accel.add(24.0); // start dash accel

        // after skill activations, since greens can affect downhill check if
        // starting on a hill
        solver.init_hills();

        // also must come after the first round of skill activations
        solver.base_target_speed = [base_target_speed(&solver.horse, solver.course, 0), base_target_speed(&solver.horse, solver.course, 1), base_target_speed(&solver.horse, solver.course, 2)];
        solver.last_spurt_speed = last_spurt_speed(&solver.horse, solver.course);
        solver.last_spurt_transition = -1.0;

        // roll for section first, then check, so rng always advances twice
        // regardless of outcome (avoids data-dependent rng advancement)
        solver.kakari_start = (2.0 + solver.rng_uniform(7.0)) * solver.section_length;
        if solver.rng.random() > (0.65 / (0.1 * solver.horse.wisdom + 1.0).log10()).powi(2) + solver.modifiers_kakari_chance {
            solver.kakari_start = solver.course.distance + 9999.0;
        }
        let r1 = solver.rng.random();
        let r2 = solver.rng.random();
        let r3 = solver.rng.random();
        let seq = [0.0, r1, r2, r3, 1.0];
        let idx = seq.iter().position(|&x| x > 0.45).unwrap();
        solver.kakari_duration = 3.0 * idx as f64;
        solver.kakari_timer = solver.new_timer(0.0);
        solver.is_kakari = false;
        solver.temptation_count = 0;

        solver.section_modifier = Vec::with_capacity(25);
        for _ in 0..24 {
            let max_v = solver.horse.wisdom / 5500.0 * (solver.horse.wisdom * 0.1).log10();
            let factor = (max_v - 0.65 + solver.rng.random() * 0.65) / 100.0;
            solver.section_modifier.push(base_speed(solver.course) * factor);
        }
        solver.section_modifier.push(0.0); // tick after race is done / one uma runs off the end


        solver.hp.init(&solver.horse);

        solver.base_accel = [0, 1, 2, 0, 1, 2].iter().enumerate().map(|(i, &p)| {
            calc_base_accel(if i > 2 { UPHILL_BASE_ACCEL } else { BASE_ACCEL }, &solver.horse, p)
        }).collect::<Vec<f64>>().try_into().unwrap();

        solver
    }

    fn rng_uniform(&mut self, upper: f64) -> f64 {
        self.rng.uniform(upper) as f64
    }

    // Calling `self.on_skill_activate(self, ...)` directly would borrow
    // `self.on_skill_activate` mutably (required for `FnMut`) while also
    // passing `self` as `&mut RaceSolver` -- two overlapping mutable
    // borrows of `self`. Swapping the hook out for a no-op placeholder for
    // the duration of the call sidesteps this with no behavioral
    // difference (the hook itself never re-enters the solver it was called
    // from, so it never observes the temporary swap).
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

    // -- setup helpers --------------------------------------------------

    fn new_timer(&mut self, t: f64) -> Timer {
        let tm = new_timer_value(t);
        self.timers.push(tm.clone());
        tm
    }

    /// Stop ticking a finished skill's duration timer. `step()` advances
    /// every timer in `self.timers` each frame, and nothing reads a timer
    /// again once its skill has been dropped from the active-skill lists,
    /// so this is purely bookkeeping.
    fn release_timer(&mut self, tm: &Timer) {
        if let Some(pos) = self.timers.iter().position(|t| Rc::ptr_eq(t, tm)) {
            self.timers.remove(pos);
        }
    }

    fn init_hills(&mut self) {
        // slopes must be sorted by start for the sequential-consumption
        // logic below
        self.n_hills = self.course.slopes.len();
        self.hill_start = self.course.slopes.iter().map(|s| s.start).rev().collect();
        self.hill_end = self.course.slopes.iter().map(|s| s.start + s.length).rev().collect();
        self.hill_idx = -1;

        // separate rng per hill so a downhill proc on one hill doesn't
        // affect how many times the rng is rolled on a later hill
        self.hill_rng = self.course.slopes.iter().map(|_| Rule30CARng::new(self.rng.int32(), self.rng.int32())).collect();
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

    // -- per-tick ---------------------------------------------------------

    fn get_max_speed(&self) -> f64 {
        if self.start_dash {
            self.target_speed.min(0.85 * base_speed(self.course))
        } else if self.current_speed + self.modifiers_one_frame_accel > self.target_speed {
            9999.0 // allow decelerating if targetSpeed drops
        } else {
            self.target_speed
        }
    }

    pub fn step(&mut self, mut dt: f64, field: Option<&RaceField>) {
        if self.accumulatetime.get() < self.start_delay {
            let partial_frame = self.start_delay - self.accumulatetime.get();
            if partial_frame < dt {
                for tm in &self.timers {
                    tm.set(tm.get() + partial_frame);
                }
                dt -= partial_frame;
            } else {
                for tm in &self.timers {
                    tm.set(tm.get() + dt);
                }
                return;
            }
        }

        if self.pos < self.pos_keep_end {
            if let Some(pacer) = self.pacer.as_mut() {
                pacer.step(dt, field);
            }
        }

        let halfv = (self.current_speed + 0.5 * dt * self.accel).min(self.get_max_speed());
        let displacement = halfv + self.modifiers_current_speed.acc + self.modifiers_current_speed.err;
        self.pos += displacement * dt;
        let hp_ctx = HpTickCtx { is_pace_down: self.is_pace_down, is_downhill_mode: self.is_downhill_mode, is_kakari: self.is_kakari, phase: self.phase, current_speed: self.current_speed };
        self.hp.tick(hp_ctx, dt);
        for tm in &self.timers {
            tm.set(tm.get() + dt);
        }
        self.update_hills();
        self.update_phase();
        self.process_skill_activations(field);
        self.update_kakari();
        self.update_position_keep(dt, field);
        self.update_last_spurt_state();
        self.update_target_speed();
        self.apply_forces();
        self.current_speed = (halfv + 0.5 * dt * self.accel + self.modifiers_one_frame_accel).min(self.get_max_speed());
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
        self.update_competition_mechanics(dt, field);
    }

    fn update_blocking(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => {
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
        } else {
            self.blocked_front_timer.set(0.0);
        }
        let (inner_blocked, outer_blocked) = field.side_room(i, self.pos, self.lane);
        let side_blocked = inner_blocked || outer_blocked;
        self.blocked_side_timer.set(if side_blocked { self.blocked_side_timer.get() + dt } else { 0.0 });
        let all_blocked = blocker.is_some() && side_blocked;
        self.blocked_all_timer.set(if all_blocked { self.blocked_all_timer.get() + dt } else { 0.0 });
    }

    fn update_lane_target(&mut self, field: &RaceField) {
        let i = self.field_index.unwrap();
        if !self.hp.has_remaining_hp() {
            self.lane_target = self.lane;
            return;
        }
        if self.is_pace_down {
            self.lane_target = 0.18;
            return;
        }
        if let Some(last_corner) = self.course.corners.last() {
            if self.extra_move_lane.is_none() && self.pos >= last_corner.start {
                let lane_distance = self.lane;
                self.extra_move_lane = Some((lane_distance / 0.1).max(0.0).min(1.0) * 0.5 + self.rng.random() * 0.1);
            }
        }
        if let Some(extra) = self.extra_move_lane {
            self.lane_target = extra;
            return;
        }
        let (inner_blocked, outer_blocked) = field.side_room(i, self.pos, self.lane);
        if field.front_blocker(i, self.pos, self.lane).is_some() {
            if !outer_blocked {
                self.lane_target = self.course.max_lane.min(self.lane + 1.0 / 18.0);
            } else if !inner_blocked {
                self.lane_target = (self.lane - 1.0 / 18.0).max(0.0);
            }
            // else boxed in on both sides -- stay put
        } else {
            self.lane_target = (self.lane - 0.05).max(0.0);
        }
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
        let first_move_modifier = 1.0 + lane_distance / self.course.max_lane.max(1e-9) * 0.05;
        let order_modifier = if self.phase >= 2 { 1.0 + field.order_of(i) as f64 * 0.01 } else { 1.0 };
        let target_lane_change_speed = 0.02 * (0.3 + 0.001 * self.horse.power) * first_move_modifier * order_modifier;
        let lane_change_accel = 0.02 * 1.5;
        if self.lane_change_speed < target_lane_change_speed {
            self.lane_change_speed = target_lane_change_speed.min(self.lane_change_speed + lane_change_accel * dt);
        }
        let actual_speed = self.lane_change_speed.max(0.0).min(0.6);
        let direction = if self.lane_target > self.lane { 1.0 } else { -1.0 };
        let step = actual_speed * dt * direction;
        if step.abs() >= lane_distance {
            self.lane = self.lane_target;
            self.lane_change_speed = 0.0;
        } else {
            self.lane += step;
        }
        self.lane = self.lane.max(0.0).min(self.course.max_lane);
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

    fn update_kakari(&mut self) {
        if self.temptation_count == 0 && self.pos >= self.kakari_start {
            self.is_kakari = true;
            self.temptation_count = 1;
            self.kakari_timer.set(-self.kakari_duration);
            self.call_activate_hook("kakari", Perspective::Self_);
        } else if self.is_kakari && self.kakari_timer.get() >= 0.0 {
            self.is_kakari = false;
            self.call_deactivate_hook("kakari", Perspective::Self_);
        }
    }

    fn update_position_keep(&mut self, dt: f64, field: Option<&RaceField>) {
        if strategy_matches(self.horse.strategy, Strategy::Nige) {
            self.update_position_keep_front_runner(dt, field);
        } else {
            self.update_position_keep_non_nige(dt, field);
        }
    }

    /// Doc: several position-keep entry checks roll a wisdom-scaled chance
    /// "every 2 seconds". Converted here to an equivalent continuous
    /// per-tick hazard rate rather than literally gating on a 2s interval
    /// timer, so entry can happen on whichever tick the distance condition
    /// first holds -- a smoothing of the doc's discrete-interval model, not
    /// a literal implementation of it.
    fn roll_position_keep_wisdom(&mut self, coef: f64, dt: f64) -> bool {
        let pct = (coef * (self.horse.wisdom.max(1.0) * 0.1).log10()).max(0.0);
        let per_tick_chance = 1.0 - (1.0 - pct.min(100.0) / 100.0).powf(dt / 2.0);
        self.rng.random() < per_tick_chance
    }

    fn update_position_keep_non_nige(&mut self, dt: f64, field: Option<&RaceField>) {
        if self.pos >= self.pos_keep_end {
            self.is_pace_down = false;
            self.pos_keep_mode = None;
            self.pos_keep_speed_coef = 1.0;
            return;
        }
        let field = match field {
            None => {
                self.pos_keep_mode = None;
                self.is_pace_down = false;
                self.pos_keep_speed_coef = 1.0;
                return;
            }
            Some(f) => f,
        };
        let i = self.field_index.unwrap();
        if field.pacemaker() == Some(i) {
            self.pos_keep_mode = None;
            self.is_pace_down = false;
            self.pos_keep_speed_coef = 1.0;
            return;
        }
        let gap = field.gap_to_pacemaker(i, self.pos);
        let no_speed_skills = self.active_target_speed_skills.is_empty() && self.active_current_speed_skills.is_empty();

        if self.pos_keep_mode == Some(PosKeepMode::PaceDown) {
            if gap > self.pos_keep_effect_exit_distance || self.pos - self.pos_keep_effect_start > self.section_length || !no_speed_skills || self.is_kakari {
                self.pos_keep_mode = None;
                self.is_pace_down = false;
                self.pos_keep_cooldown.set(-3.0);
                self.pos_keep_speed_coef = 1.0;
            }
            return;
        }
        if self.pos_keep_mode == Some(PosKeepMode::PaceUp) {
            if gap < self.pos_keep_effect_exit_distance {
                self.pos_keep_mode = None;
                self.pos_keep_speed_coef = 1.0;
            }
            return;
        }

        if gap < self.pos_keep_min_threshold && no_speed_skills && !self.is_kakari && self.pos_keep_cooldown.get() >= 0.0 {
            self.pos_keep_mode = Some(PosKeepMode::PaceDown);
            self.is_pace_down = true;
            self.pos_keep_effect_start = self.pos;
            let lo = self.pos_keep_min_threshold;
            let hi = if self.phase == 1 { lo + 0.5 * (self.pos_keep_max_threshold - lo) } else { self.pos_keep_max_threshold };
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo);
            self.pos_keep_speed_coef = if self.phase == 1 { 0.945 } else { 0.915 };
        } else if gap > self.pos_keep_max_threshold && self.roll_position_keep_wisdom(15.0, dt) {
            self.pos_keep_mode = Some(PosKeepMode::PaceUp);
            let (lo, hi) = (self.pos_keep_min_threshold, self.pos_keep_max_threshold);
            self.pos_keep_effect_exit_distance = lo + self.pace_effect_rng.random() * (hi - lo);
            self.pos_keep_speed_coef = 1.04;
        }
    }

    fn update_position_keep_front_runner(&mut self, dt: f64, field: Option<&RaceField>) {
        if self.pos >= self.pos_keep_end {
            self.pos_keep_mode = None;
            self.pos_keep_speed_coef = 1.0;
            return;
        }
        let field = match field {
            Some(f) => f,
            None => return,
        };
        let i = self.field_index.unwrap();
        let is_leader = field.order_of(i) == 1;
        let gap_2nd = if is_leader { field.distance_to_behind(i, self.pos) } else { None };
        let solo = field.is_solo_front_runner(i);
        let is_oonige = self.horse.strategy == Strategy::Oonige;
        let speed_up_threshold = if solo { 12.5 } else if is_oonige { 17.5 } else { 4.5 };
        let overtake_exit_threshold = if is_oonige { 27.5 } else { 10.0 };

        if self.pos_keep_mode == Some(PosKeepMode::SpeedUp) {
            if !is_leader || gap_2nd.is_none() || gap_2nd.unwrap() >= speed_up_threshold {
                self.pos_keep_mode = None;
                self.pos_keep_speed_coef = 1.0;
            }
            return;
        }
        if self.pos_keep_mode == Some(PosKeepMode::Overtake) {
            if is_leader && gap_2nd.is_some() && gap_2nd.unwrap() >= overtake_exit_threshold {
                self.pos_keep_mode = None;
                self.pos_keep_speed_coef = 1.0;
            }
            return;
        }

        // roll_position_keep_wisdom needs &mut self, but is_leader/gap_2nd
        // were already computed above from `field` (now unused past this
        // point), so this doesn't conflict.
        if is_leader && gap_2nd.is_some() && gap_2nd.unwrap() < speed_up_threshold {
            if self.roll_position_keep_wisdom(20.0, dt) {
                self.pos_keep_mode = Some(PosKeepMode::SpeedUp);
                self.pos_keep_speed_coef = 1.04;
            }
        } else if !is_leader && self.roll_position_keep_wisdom(20.0, dt) {
            self.pos_keep_mode = Some(PosKeepMode::Overtake);
            self.pos_keep_speed_coef = 1.05;
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
        }
    }

    fn update_target_speed(&mut self) {
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

        if self.is_downhill_mode {
            self.target_speed += 0.3 + self.slope_per / 100000.0;
        } else if self.hill_idx != -1 && self.slope_per > 0.0 {
            self.target_speed -= self.slope_per / 10000.0 * 200.0 / self.horse.power;
            self.target_speed = self.target_speed.max(self.min_speed);
        }

        // Doc: MoveLaneModifier -- applied when affected by a skill that
        // modifies lane move speed (ability type 28) AND lane movement
        // happened on the PREVIOUS frame. This runs before update_lane(dt)
        // in step(), so moved_lane_this_tick still holds last tick's value
        // here -- exactly "the previous frame", no extra state needed.
        if !self.active_lane_speed_skills.is_empty() && self.moved_lane_this_tick {
            self.target_speed += (0.0002 * self.horse.power).sqrt();
        }

        // Target speed cannot exceed 30 m/s (Game:Mechanics, Target Speed
        // section). Applied after every additive/uphill/downhill modifier.
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
        // Re-roll once per second while still on a hill: a chance to drop
        // out of downhill mode, or (if not yet in it) another chance to
        // enter it -- matches race_solver.py's periodic re-check, which the
        // single entry-time roll above does not capture on its own.
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
        // Phase is deliberately capped at 2 for speed-modifier purposes --
        // phase 3 (from 5/6 distance) is treated the same as phase 2 and is
        // never reached via this transition (matches race_solver.py).
        if self.pos >= self.next_phase_transition && self.phase < 2 {
            self.phase += 1;
            self.next_phase_transition = crate::course_data::phase_start(self.course.distance, self.phase + 1);
        }
    }

    // -- skill activation -------------------------------------------------

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
                self.call_deactivate_hook(&s.skill_id, s.perspective);
            }
        }

        if self.armed_scan {
            self.process_pending_armed(self.pos, field);
        } else {
            self.process_pending_full(self.pos, field);
        }
    }

    /// Position-ordered equivalent of the full scan below. Arms skills as
    /// `pos` reaches their trigger.start, then tests only the armed ones. A
    /// skill with trigger.start > pos is a guaranteed no-op in the full scan
    /// (trigger.end >= trigger.start, so neither branch can fire) and
    /// `pending_removal` is provably empty here (see `armed_scan`), so the
    /// skipped entries cannot matter. Armed skills are visited in
    /// descending sequence-number order, the same relative order the full
    /// scan visits them in, so activations within a frame keep their
    /// original order.
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
            let fires = (self.pending_skills[j].extra_condition)(self, field);
            if fires {
                self.armed.remove(i);
                let sk = self.pending_skills.remove(j);
                self.pending_seq.remove(j);
                let is_special = sk.skill_id == "asitame" || sk.skill_id == "staminasyoubu";
                self.activate_skill(&sk.skill_id, sk.perspective, sk.rarity, &sk.effects);
                if !is_special {
                    activate_count_this_frame += 1;
                }
            }
        }
        self.activate_count_last_frame = activate_count_this_frame;
    }

    /// Plain full-list scan, used whenever any pending skill carries an
    /// ACTIVATE_RANDOM_GOLD effect (so `pending_removal` can become
    /// non-empty -- see `_do_activate_random_gold`, which can re-activate
    /// another still-pending skill out of order).
    fn process_pending_full(&mut self, pos: f64, field: Option<&RaceField>) {
        let mut activate_count_this_frame = 0;
        let mut i = self.pending_skills.len();
        while i > 0 {
            i -= 1;
            let skill_id_matches_removal = self.pending_removal.contains(&self.pending_skills[i].skill_id);
            if pos >= self.pending_skills[i].trigger.end || skill_id_matches_removal {
                let removed = self.pending_skills.remove(i);
                self.pending_removal.remove(&removed.skill_id);
                continue;
            }
            let fires = pos >= self.pending_skills[i].trigger.start && (self.pending_skills[i].extra_condition)(self, field);
            if fires {
                let (skill_id, perspective, rarity, effects) = {
                    let sk = &self.pending_skills[i];
                    (sk.skill_id.clone(), sk.perspective, sk.rarity, sk.effects.clone())
                };
                self.activate_skill(&skill_id, perspective, rarity, &effects);
                self.pending_skills.remove(i);
                if skill_id != "asitame" && skill_id != "staminasyoubu" {
                    activate_count_this_frame += 1;
                }
            }
        }
        self.activate_count_last_frame = activate_count_this_frame;
    }

    fn activate_skill(&mut self, skill_id: &str, perspective: Perspective, rarity: SkillRarity, effects: &[SkillEffect]) {
        // ExtendEvolvedDuration must apply after other effects on the same
        // skill, so it doesn't extend its own siblings' durations.
        let mut effects = effects.to_vec();
        effects.sort_by_key(|ef| (ef.effect_type == SkillType::ExtendEvolvedDuration) as u8);

        for ef in &effects {
            let scaled_duration = ef.base_duration * (self.course.distance / 1000.0) * (if rarity == SkillRarity::Evolution { self.modifiers_special_skill_duration_scaling } else { 1.0 });
            match ef.effect_type {
                SkillType::Noop => {}
                SkillType::SpeedUp => self.horse.speed = (self.horse.speed + ef.modifier).max(1.0),
                SkillType::StaminaUp => {
                    self.horse.stamina = (self.horse.stamina + ef.modifier).max(1.0);
                    self.horse.raw_stamina = (self.horse.raw_stamina + ef.modifier).max(1.0);
                }
                SkillType::PowerUp => self.horse.power = (self.horse.power + ef.modifier).max(1.0),
                SkillType::GutsUp => self.horse.guts = (self.horse.guts + ef.modifier).max(1.0),
                SkillType::WisdomUp => self.horse.wisdom = (self.horse.wisdom + ef.modifier).max(1.0),
                // Effect type 32 -- see race_solver.py's ALL_STATUS_UP branch
                // for how it was identified and why the modifier is on the
                // stat-point scale.
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
                    self.active_target_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer, modifier: ef.modifier, natural_deceleration: false });
                }
                SkillType::LaneMoveSpeedUp => {
                    let timer = self.new_timer(-scaled_duration);
                    self.active_lane_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer, modifier: ef.modifier, natural_deceleration: false });
                }
                SkillType::ModifyKakariChance => self.modifiers_kakari_chance += ef.modifier / 100.0,
                SkillType::Accel => {
                    self.modifiers_accel.add(ef.modifier);
                    let timer = self.new_timer(-scaled_duration);
                    self.active_accel_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer, modifier: ef.modifier, natural_deceleration: false });
                }
                SkillType::CurrentSpeed | SkillType::CurrentSpeedWithNaturalDeceleration => {
                    self.modifiers_current_speed.add(ef.modifier);
                    let timer = self.new_timer(-scaled_duration);
                    let natural_deceleration = ef.effect_type == SkillType::CurrentSpeedWithNaturalDeceleration;
                    self.active_current_speed_skills.push(ActiveSkill { skill_id: skill_id.to_string(), perspective, duration_timer: timer, modifier: ef.modifier, natural_deceleration });
                }
                SkillType::Recovery => {
                    if perspective == Perspective::Self_ {
                        self.activate_count_heal += 1;
                    }
                    self.hp.recover(ef.modifier);
                    self.stamina_keep_active = false; // doc: HP-recovery resets Stamina Keep
                    if self.phase >= 2 && !self.is_last_spurt {
                        self.last_spurt_transition = -1.0;
                        self.update_last_spurt_state();
                    }
                }
                SkillType::ActivateRandomGold => self.do_activate_random_gold(ef.modifier),
                SkillType::ExtendEvolvedDuration => self.modifiers_special_skill_duration_scaling = ef.modifier,
            }
        }

        if perspective == Perspective::Self_ {
            self.activate_count[self.phase as usize] += 1;
            if self.pos >= self.course.distance / 2.0 {
                self.activate_count_later_half += 1;
            }
        }
        self.used_skills.insert(skill_id.to_string());
        self.call_activate_hook(skill_id, perspective);
    }

    fn do_activate_random_gold(&mut self, ngolds: f64) {
        let mut gold_indices: Vec<usize> = self
            .pending_skills
            .iter()
            .enumerate()
            .filter(|(_, sk)| matches!(sk.rarity, SkillRarity::Gold | SkillRarity::Evolution) && sk.effects.iter().all(|ef| ef.effect_type as i32 > SkillType::WisdomUp as i32
                && ef.effect_type != SkillType::AllStatusUp))
            .map(|(i, _)| i)
            .collect();
        // Python's range(len-1, -1, -1) includes i==0: even that draw's
        // self-swap (j is always 0 when i==0) still consumes a gorosi_rng
        // roll, so it must not be skipped even though it never changes
        // gold_indices' contents -- otherwise gorosi_rng's state would
        // desync from Python's after this call.
        if !gold_indices.is_empty() {
            for i in (0..gold_indices.len()).rev() {
                let j = self.gorosi_rng.uniform((i + 1) as f64) as usize;
                gold_indices.swap(i, j);
            }
        }
        let take = (ngolds as usize).min(gold_indices.len());
        for &idx in gold_indices.iter().take(take) {
            let (skill_id, perspective, rarity, effects) = {
                let sk = &self.pending_skills[idx];
                (sk.skill_id.clone(), sk.perspective, sk.rarity, sk.effects.clone())
            };
            self.activate_skill(&skill_id, perspective, rarity, &effects);
            self.pending_removal.insert(skill_id);
        }
    }

    // -- competition mechanics (doc: Spot Struggle, Dueling, Compete Before
    // Spurt, Secure Lead, Stamina Keep) -- per the doc itself, the
    // least-confident section of the whole engine: the author explicitly
    // flags these as "inferred from parameter file" or "under
    // investigation" rather than confirmed reverse-engineering. Treat this
    // as the first place to look if race behavior ever looks wrong.

    fn update_competition_mechanics(&mut self, dt: f64, field: Option<&RaceField>) {
        let field = match field {
            Some(f) => f,
            None => return,
        };
        self.update_stamina_keep(dt);
        self.update_spot_struggle(field);
        self.update_compete_before_spurt(dt, field);
        self.update_secure_lead(dt, field);
        self.update_dueling(dt, field);
    }

    /// Doc: Stamina Keep -- conserve roughly 1.035-1.04x the HP needed to
    /// finish. The doc's own author flags this activation-chance formula as
    /// "highly speculative", explicitly contradicted by their own
    /// packet-capture testing. "HP needed to finish" is approximated here
    /// as remaining-course-fraction (a coarse proxy). While active,
    /// suppresses Compete Before Spurt / Secure Lead entry, per doc; reset
    /// when an HP-recovery skill fires.
    fn update_stamina_keep(&mut self, dt: f64) {
        if self.stamina_keep_active || self.course.distance <= 0.0 {
            return;
        }
        let remaining_frac = ((self.course.distance - self.pos) / self.course.distance).max(0.0);
        if self.hp.hp_ratio_remaining() >= remaining_frac * 1.04 {
            return;
        }
        let wisdom = self.horse.wisdom.max(1.0);
        let pct = 30.0 * (wisdom / 1000.0 + wisdom.powf(0.03));
        let per_tick_chance = 1.0 - (1.0 - pct.min(100.0) / 100.0).powf(dt / 2.0);
        if self.rng.random() < per_tick_chance {
            self.stamina_keep_active = true;
        }
    }

    /// Doc: Spot Struggle / Lead Competition. Only Nige/Oonige-strategy
    /// horses compete, and only within their own strategy -- note this is
    /// an *exact* strategy match (`s.horse.strategy == strategy`), not
    /// `strategy_matches` (which treats Nige/Oonige as equivalent
    /// elsewhere): a Nige horse's group here excludes Oonige horses.
    fn update_spot_struggle(&mut self, field: &RaceField) {
        if self.spot_struggle_active {
            if self.spot_struggle_timer.get() >= 0.0 || self.pos >= self.section_length * 9.0 {
                self.spot_struggle_active = false;
                self.modifiers_target_speed.add(-self.spot_struggle_modifier);
            }
            return;
        }
        if self.pos < 150.0 || self.pos >= self.section_length * 9.0 {
            return;
        }
        let strategy = self.horse.strategy;
        if strategy != Strategy::Nige && strategy != Strategy::Oonige {
            return;
        }
        let i = self.field_index.unwrap();
        // Python's `max(group, key=...)` returns the *first* maximal
        // element on ties; `Iterator::max_by` would return the *last*, so
        // this scans manually with a strict `>` to match Python exactly.
        let mut group_count = 0;
        let mut front_idx: Option<usize> = None;
        let mut front_pos = f64::NEG_INFINITY;
        for j in 0..field.n {
            // `j == i` is `self`, already mutably borrowed for the duration
            // of this `step()` call -- read its own fields directly rather
            // than re-entering its `RefCell` via `field.solvers[j]`.
            let (j_strategy, j_pos) = if j == i { (self.horse.strategy, self.pos) } else { let s = field.solvers[j].borrow(); (s.horse.strategy, s.pos) };
            if j_strategy == strategy {
                group_count += 1;
                if j_pos > front_pos {
                    front_pos = j_pos;
                    front_idx = Some(j);
                }
            }
        }
        if group_count < 2 {
            return;
        }
        let front_idx = front_idx.unwrap();
        if front_idx == i {
            return;
        }
        let (front_pos, front_lane) = {
            let f = field.solvers[front_idx].borrow();
            (f.pos, f.lane)
        };
        let dist_gap = front_pos - self.pos;
        let lane_gap = (front_lane - self.lane).abs();
        if !(dist_gap < 3.75 && lane_gap < 0.165 * 11.25) {
            return;
        }
        let guts = self.horse.guts.max(1.0);
        let strat_prof = STRATEGY_PROFICIENCY_MODIFIER_APPROX[self.horse.strategy_aptitude as usize];
        self.spot_struggle_modifier = (500.0 * guts).powf(0.6) * 0.0001;
        let duration = (700.0 * guts).powf(0.5) * 0.012 * strat_prof;
        self.spot_struggle_active = true;
        self.spot_struggle_timer.set(-duration);
        self.modifiers_target_speed.add(self.spot_struggle_modifier);
    }

    fn update_compete_before_spurt(&mut self, dt: f64, field: &RaceField) {
        if self.compete_spurt_active {
            if self.compete_spurt_timer.get() >= 0.0 {
                self.compete_spurt_active = false;
                self.compete_spurt_cooldown.set(-1.0);
                self.modifiers_target_speed.add(-self.compete_spurt_modifier);
            }
            return;
        }
        let (section_lo, section_hi) = (self.section_length * 10.0, self.section_length * 15.0);
        if !(section_lo <= self.pos && self.pos < section_hi) || self.compete_spurt_cooldown.get() < 0.0 {
            return;
        }
        if self.stamina_keep_active {
            return;
        }
        let i = self.field_index.unwrap();
        let strategy = self.horse.strategy;
        let dist_threshold = compete_dist_threshold(strategy);
        let far_from_lead = field.distance_to_leader(i, self.pos) > dist_threshold;
        let near = field.near_count(i, self.pos, self.lane) > 0;
        if !(far_from_lead || near) {
            return;
        }
        if !self.roll_position_keep_wisdom(15.0, dt) {
            return;
        }
        let strat_coef = compete_strategy_coef(strategy);
        let mut solo_bonus = 1.0;
        if (strategy == Strategy::Nige || strategy == Strategy::Oonige) && field.is_solo_front_runner(i) {
            solo_bonus = if strategy == Strategy::Oonige { 2.0 } else { 1.1 };
        }
        let modifier = ((self.horse.power.max(0.0) / 1500.0).powf(0.5) * 2.0 + (self.horse.guts.max(0.0) / 3000.0).powf(0.2)) * 0.1 * strat_coef * solo_bonus;
        let stamina_strat_coef = compete_stamina_strategy_coef(strategy);
        let dist_coef = competition_distance_coefficient(self.course.distance);
        let near_factor = if near && !far_from_lead { 0.5 } else { 0.0 };
        let stamina_cost = 20.0 * (stamina_strat_coef * dist_coef + near_factor) * 2.0; // 2s window
        self.compete_spurt_active = true;
        self.compete_spurt_modifier = modifier;
        self.compete_spurt_timer.set(-2.0);
        self.modifiers_target_speed.add(modifier);
        if let Some(hp) = self.hp.hp_mut() {
            *hp -= stamina_cost;
        }
    }

    /// Doc: Secure Lead (section 11-15). The doc's real DesirableLead
    /// formula uses a full per-strategy-pair coefficient matrix that wasn't
    /// fully captured in this port's reference notes -- approximated below
    /// with a single flat coefficient instead of the real matrix.
    fn update_secure_lead(&mut self, dt: f64, field: &RaceField) {
        if self.secure_lead_active {
            if self.secure_lead_timer.get() >= 0.0 {
                self.secure_lead_active = false;
                self.secure_lead_cooldown.set(-1.0);
                self.modifiers_target_speed.add(-self.secure_lead_modifier);
            }
            return;
        }
        let (section_lo, section_hi) = (self.section_length * 10.0, self.section_length * 15.0);
        if !(section_lo <= self.pos && self.pos < section_hi) || self.secure_lead_cooldown.get() < 0.0 {
            return;
        }
        if self.stamina_keep_active {
            return;
        }
        let i = self.field_index.unwrap();
        let gap_behind = match field.distance_to_behind(i, self.pos) {
            Some(g) => g,
            None => return,
        };
        let desirable_lead = SECURE_LEAD_COEF_APPROX + 0.0003 * (self.course.distance + 1000.0);
        if gap_behind >= desirable_lead {
            return;
        }
        if self.rng.random() >= 0.20 * (dt / 2.0) {
            return;
        }
        let strategy = self.horse.strategy;
        let strat_coef = secure_lead_strategy_coef(strategy);
        let mut solo_bonus = 1.0;
        if (strategy == Strategy::Nige || strategy == Strategy::Oonige) && field.is_solo_front_runner(i) {
            solo_bonus = if strategy == Strategy::Oonige { 7.0 } else { 2.0 };
        }
        let modifier = (self.horse.guts.max(0.0) / 2000.0).powf(0.5) * 0.3 * strat_coef * solo_bonus;
        let stamina_strat_coef = secure_lead_stamina_strategy_coef(strategy);
        let dist_coef = competition_distance_coefficient(self.course.distance);
        let stamina_cost = 20.0 * stamina_strat_coef * dist_coef * 2.0;
        self.secure_lead_active = true;
        self.secure_lead_modifier = modifier;
        self.secure_lead_timer.set(-2.0);
        self.modifiers_target_speed.add(modifier);
        if let Some(hp) = self.hp.hp_mut() {
            *hp -= stamina_cost;
        }
    }

    /// Doc: Dueling / Compete Fight, final straight only.
    fn update_dueling(&mut self, dt: f64, field: &RaceField) {
        if self.course.straights.is_empty() {
            return;
        }
        let last_straight = self.course.straights.last().unwrap();
        let in_final_straight = last_straight.start <= self.pos && self.pos < last_straight.end;
        if self.duel_active {
            if !in_final_straight || self.hp.hp_ratio_remaining() < 0.05 {
                self.duel_active = false;
                self.modifiers_target_speed.add(-self.duel_target_speed_modifier);
                self.modifiers_accel.add(-self.duel_accel_modifier);
            }
            return;
        }
        if !in_final_straight || self.hp.hp_ratio_remaining() < 0.15 {
            self.duel_continuous_timer = 0.0;
            return;
        }
        let i = self.field_index.unwrap();
        let (pos, lane) = (self.pos, self.lane);
        let mut target: Option<(f64, f64)> = None; // (pos, current_speed) of the found target
        for j in 0..field.n {
            if j == i {
                continue;
            }
            let other = field.solvers[j].borrow();
            let dist_gap = other.pos - pos;
            let lane_gap = other.lane - lane;
            if dist_gap.abs() < 3.0 && lane_gap.abs() < 0.25 * 11.25 {
                target = Some((other.pos, other.current_speed));
                break;
            }
        }
        let target = match target {
            Some(t) => t,
            None => {
                self.duel_continuous_timer = 0.0;
                return;
            }
        };
        self.duel_continuous_timer += dt;
        if self.duel_continuous_timer < 2.0 {
            return;
        }
        let top_half = field.order_of(i) <= (field.n / 2).max(1);
        if !(top_half && (target.1 - self.current_speed).abs() < 0.6) {
            return;
        }
        let guts = self.horse.guts.max(1.0);
        self.duel_target_speed_modifier = (200.0 * guts).powf(0.708) * 0.0001;
        self.duel_accel_modifier = (160.0 * guts).powf(0.59) * 0.0001;
        self.duel_active = true;
        self.modifiers_target_speed.add(self.duel_target_speed_modifier);
        self.modifiers_accel.add(self.duel_accel_modifier);
    }

    /// Deactivate any skills that haven't finished their durations yet
    /// (call at the end of a simulation, in case a skill activated near the
    /// end and the race finished before its duration expired).
    /// Python's race_runner.py sets these as plain attributes on the
    /// already-constructed solver (`solver.on_skill_activate = ...`)
    /// rather than through the builder, so the orchestration layer needs
    /// to reach them post-construction too.
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
