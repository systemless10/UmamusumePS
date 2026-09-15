//! Port of race_solver_builder.py -- base->adjusted stat pipeline,
//! skill-data loading/wiring through the condition parser + sample-policy
//! layer.
//!
//! Scoped to exactly what `race_runner.py`'s actual usage exercises (the
//! only caller this port needs to reproduce): `nsamples` is always 1, and
//! `race_runner._new_builder` only ever calls `.seed/.course/.mood/.ground/
//! .weather/.season/.gate_index/.horse/.with_asiwotameru/
//! .with_stamina_syoubu/.add_skill/.build`. Pacer support
//! (`.pacer`/`.use_default_pacer`), `.fork`, `.with_wisdom_checks`,
//! `.with_activate_counts_as_random`, per-skill sample-policy overrides,
//! and the generator `redo`/`.send()` protocol are all unreachable from
//! that call path and are not implemented here; `build()` returns a plain
//! `Vec<RaceSolver>` of length `nsamples` instead of a generator.
//! `on_skill_activate`/`on_skill_deactivate` are likewise set directly on
//! the constructed solver by race_runner.py, not through the builder, so
//! there is no builder-level setter for them either -- see
//! `RaceSolver::set_on_skill_activate`.

use std::collections::HashMap;

use crate::activation_conditions::{self, CondResult, Condition};
use crate::condition_parser;
use crate::course_data::{self, phase_start, CourseData};
use crate::horse_types::{Aptitude, HorseParameters, Strategy};
use crate::hp_policy::{GameHpState, HpPolicy};
use crate::race_parameters::{parse_ground_condition, parse_season, parse_weather, GroundCondition, RaceParameters, RaceParametersWithSkillId};
use crate::race_solver::{Perspective, RaceSolver, SkillEffect, SkillRarity, SkillType};
use crate::random_gen::Rule30CARng;
use crate::region::{rmap_one, Region};

// ---------------------------------------------------------------------------
// Skill data (skill_data.json), loaded once by main.rs via `init_skills`.

pub struct SkillEffectDef {
    pub effect_type_raw: i32,
    pub target: i32,
    pub modifier: f64,
}

pub struct SkillAlternative {
    pub precondition: Option<String>,
    pub condition: String,
    pub effects: Vec<SkillEffectDef>,
    pub base_duration: f64,
}

pub struct SkillDef {
    pub rarity: i32,
    pub wisdom_check: bool,
    pub alternatives: Vec<SkillAlternative>,
}

pub type SkillTable = HashMap<String, SkillDef>;

static SKILLS: std::sync::OnceLock<SkillTable> = std::sync::OnceLock::new();

pub fn init_skills(table: SkillTable) {
    SKILLS.set(table).ok();
}

fn skills_table() -> &'static SkillTable {
    SKILLS.get().expect("init_skills must run before build_skill_data")
}

// ---------------------------------------------------------------------------
// Stat pipeline

const GROUND_SPEED_MODIFIER: [[f64; 5]; 3] = [[0.0; 5], [0.0, 0.0, 0.0, 0.0, -50.0], [0.0, 0.0, 0.0, 0.0, -50.0]];
const GROUND_POWER_MODIFIER: [[f64; 5]; 3] = [[0.0; 5], [0.0, 0.0, -50.0, -50.0, -50.0], [0.0, -100.0, -50.0, -100.0, -100.0]];
const STRATEGY_PROFICIENCY_MODIFIER: [f64; 8] = [1.1, 1.0, 0.85, 0.75, 0.6, 0.4, 0.2, 0.1];

const ASITAME_STRATEGY_DISTANCE_COEFFICIENT: [[f64; 6]; 5] = [
    [0.0; 6],
    [0.0, 1.0, 0.7, 0.75, 0.7, 1.0],
    [0.0, 1.0, 0.8, 0.7, 0.75, 1.0],
    [0.0, 1.0, 0.9, 0.875, 0.86, 1.0],
    [0.0, 1.0, 0.9, 1.0, 0.9, 1.0],
];
const ASITAME_BASE_MODIFIER: f64 = 0.00875;

fn asitame_calc_approximate_modifier(power: f64, strategy: Strategy, distance_type: i32) -> f64 {
    ASITAME_BASE_MODIFIER * (power - 1200.0).sqrt() * ASITAME_STRATEGY_DISTANCE_COEFFICIENT[distance_type as usize][strategy as usize]
}

fn stamina_syoubu_distance_factor(distance: f64) -> f64 {
    if distance < 2101.0 {
        0.0
    } else if distance < 2201.0 {
        0.5
    } else if distance < 2401.0 {
        1.0
    } else if distance < 2601.0 {
        1.2
    } else {
        1.5
    }
}

fn stamina_syoubu_calc_approximate_modifier(stamina: f64, distance: f64) -> f64 {
    let random_factor = 1.0; // TODO (matches Python): unclear how the real random-factor scaling works
    (stamina - 1200.0).sqrt() * 0.0085 * stamina_syoubu_distance_factor(distance) * random_factor
}

fn adjust_overcap(stat: f64) -> f64 {
    if stat > 1200.0 {
        1200.0 + ((stat - 1200.0) / 2.0).floor()
    } else {
        stat
    }
}

/// Raw per-horse input, matching the dict shape `race_runner._new_builder`
/// passes to `.horse({...})`.
#[derive(Clone)]
pub struct HorseSpec {
    pub speed: f64,
    pub stamina: f64,
    pub power: f64,
    pub guts: f64,
    pub wisdom: f64,
    pub strategy: Strategy,
    pub distance_aptitude: Aptitude,
    pub surface_aptitude: Aptitude,
    pub strategy_aptitude: Aptitude,
}

/// RawStat -> BaseStat: halve anything past 1200 (adjust_overcap), apply
/// mood, then hard-clamp to [1, 2000] -- the wiki's documented final bound
/// on Base Stats (matches Python's `_base_stat`).
fn base_stat(raw: f64, motiv_coef: f64) -> f64 {
    (adjust_overcap(raw) * motiv_coef).max(1.0).min(2000.0)
}

pub fn build_base_stats(desc: &HorseSpec, mood: i32) -> HorseParameters {
    let motiv_coef = 1.0 + 0.02 * mood as f64;
    HorseParameters {
        speed: base_stat(desc.speed, motiv_coef),
        stamina: base_stat(desc.stamina, motiv_coef),
        power: base_stat(desc.power, motiv_coef),
        guts: base_stat(desc.guts, motiv_coef),
        wisdom: base_stat(desc.wisdom, motiv_coef),
        strategy: desc.strategy,
        distance_aptitude: desc.distance_aptitude,
        surface_aptitude: desc.surface_aptitude,
        strategy_aptitude: desc.strategy_aptitude,
        raw_stamina: desc.stamina * motiv_coef,
    }
}

pub fn build_adjusted_stats(base: &HorseParameters, course: &CourseData, ground: GroundCondition) -> HorseParameters {
    let race_course_modifier = course_data::course_speed_modifier(course, base.speed, base.stamina, base.power, base.guts, base.wisdom);
    let ground = ground as usize;
    HorseParameters {
        speed: (base.speed * race_course_modifier + GROUND_SPEED_MODIFIER[course.surface as usize][ground]).max(1.0),
        stamina: base.stamina,
        power: (base.power + GROUND_POWER_MODIFIER[course.surface as usize][ground]).max(1.0),
        guts: base.guts,
        wisdom: base.wisdom * STRATEGY_PROFICIENCY_MODIFIER[base.strategy_aptitude as usize],
        strategy: base.strategy,
        distance_aptitude: base.distance_aptitude,
        surface_aptitude: base.surface_aptitude,
        strategy_aptitude: base.strategy_aptitude,
        raw_stamina: base.raw_stamina,
    }
}

// ---------------------------------------------------------------------------
// Skill target / effect building

const SKILL_TARGET_SELF: i32 = 1;
const SKILL_TARGET_ALL: i32 = 2;

fn is_target(self_persp: Perspective, target_type: i32) -> bool {
    target_type == SKILL_TARGET_ALL || self_persp == Perspective::Any || ((self_persp == Perspective::Self_) == (target_type == SKILL_TARGET_SELF))
}

fn valid_skill_type(t: i32) -> Option<SkillType> {
    match t {
        0 => Some(SkillType::Noop),
        1 => Some(SkillType::SpeedUp),
        2 => Some(SkillType::StaminaUp),
        3 => Some(SkillType::PowerUp),
        4 => Some(SkillType::GutsUp),
        5 => Some(SkillType::WisdomUp),
        9 => Some(SkillType::Recovery),
        10 => Some(SkillType::MultiplyStartDelay),
        13 => Some(SkillType::ExtendKakari),
        14 => Some(SkillType::SetStartDelay),
        21 => Some(SkillType::CurrentSpeed),
        22 => Some(SkillType::CurrentSpeedWithNaturalDeceleration),
        27 => Some(SkillType::TargetSpeed),
        29 => Some(SkillType::ModifyKakariChance),
        31 => Some(SkillType::Accel),
        32 => Some(SkillType::AllStatusUp),
        28 => Some(SkillType::LaneMoveSpeedUp),
        37 => Some(SkillType::ActivateRandomGold),
        42 => Some(SkillType::ExtendEvolvedDuration),
        _ => None,
    }
}

// Doc's skill-level multiplier tables (levels 1-10), by effect category.
// Real Global mechanic, not present in the vendored TS engine (which never
// passes skill level at all -- everything fires at its flat stored value).
// Index 0 unused (levels are 1-indexed).
const LEVEL_MULTIPLIER_TARGET_SPEED: [f64; 11] = [1.00, 1.00, 1.01, 1.04, 1.07, 1.10, 1.13, 1.16, 1.19, 1.22, 1.25];
const LEVEL_MULTIPLIER_ACCEL: [f64; 11] = [1.00, 1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.125, 1.15, 1.175, 1.20];
const LEVEL_MULTIPLIER_STAT: [f64; 11] = [1.00, 1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.10];
const LEVEL_MULTIPLIER_OTHER: [f64; 11] = [1.00, 1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12, 1.14, 1.16, 1.18];

fn level_multiplier(skill_type: SkillType, level: i32) -> f64 {
    let level = (level.max(1).min(10)) as usize;
    match skill_type {
        SkillType::TargetSpeed => LEVEL_MULTIPLIER_TARGET_SPEED[level],
        SkillType::Accel => LEVEL_MULTIPLIER_ACCEL[level],
        SkillType::SpeedUp | SkillType::StaminaUp | SkillType::PowerUp | SkillType::GutsUp | SkillType::WisdomUp
        | SkillType::AllStatusUp => LEVEL_MULTIPLIER_STAT[level],
        SkillType::Noop => 1.0,
        _ => LEVEL_MULTIPLIER_OTHER[level],
    }
}

fn build_skill_effects(alt: &SkillAlternative, perspective: Perspective, level: i32) -> Vec<SkillEffect> {
    alt.effects
        .iter()
        .map(|ef| {
            let effective_type = match valid_skill_type(ef.effect_type_raw) {
                Some(t) if is_target(perspective, ef.target) => t,
                _ => SkillType::Noop,
            };
            let modifier = ef.modifier / 10000.0 * level_multiplier(effective_type, level);
            SkillEffect { effect_type: effective_type, base_duration: alt.base_duration / 10000.0, modifier }
        })
        .collect()
}

fn skill_rarity_from_i32(v: i32) -> CondResult<SkillRarity> {
    match v {
        1 => Ok(SkillRarity::White),
        2 => Ok(SkillRarity::Gold),
        3 => Ok(SkillRarity::Unique),
        6 => Ok(SkillRarity::Evolution),
        other => Err(format!("unexpected skill rarity {other}")),
    }
}

pub struct SkillData {
    pub skill_id: String,
    pub perspective: Perspective,
    pub rarity: SkillRarity,
    pub wisdom_check: bool,
    pub sample_policy: crate::activation_sample_policy::SamplePolicy,
    pub regions: Vec<Region>,
    pub extra_condition: activation_conditions::OptCond,
    pub effects: Vec<SkillEffect>,
}

#[allow(clippy::too_many_arguments)]
pub fn build_skill_data(
    horse: &HorseParameters,
    race_params: &RaceParameters,
    course: &'static CourseData,
    whole_course: &[Region],
    conditions: &'static HashMap<String, Condition>,
    skill_id: &str,
    perspective: Perspective,
    level: i32,
    ignore_null_effects: bool,
) -> CondResult<Vec<SkillData>> {
    let skills = skills_table();
    let def = skills.get(skill_id).ok_or_else(|| format!("bad skill ID {skill_id}"))?;
    let extra = RaceParametersWithSkillId { params: race_params.clone(), skill_id: skill_id.to_string() };

    let mut triggers: Vec<SkillData> = Vec::new();
    for alt in &def.alternatives {
        let mut full: Vec<Region> = whole_course.to_vec();
        if let Some(precondition) = alt.precondition.as_deref() {
            if !precondition.is_empty() {
                let pre = condition_parser::parse(conditions, precondition)?;
                let (pre_regions, _cond) = pre.apply(whole_course, course, horse, &extra)?;
                if pre_regions.is_empty() {
                    continue;
                }
                let bounds = Region::new(pre_regions[0].start, whole_course.last().unwrap().end);
                full = rmap_one(&full, |r| r.intersect(&bounds));
            }
        }

        let op = condition_parser::parse(conditions, &alt.condition)?;
        let (regions, extra_condition) = op.apply(&full, course, horse, &extra)?;
        if regions.is_empty() {
            continue;
        }
        // Some two-trigger skills need both triggers placed (e.g. all the
        // is_activate_other_skill_detail ones); others should only ever
        // place one even with non-mutually-exclusive conditions. Only place
        // a second trigger for the two known cases that need it.
        if !triggers.is_empty() && !alt.condition.contains("is_activate_other_skill_detail") && !alt.condition.contains("is_used_skill_id") {
            continue;
        }
        let effects = build_skill_effects(alt, perspective, level);
        if !effects.is_empty() || ignore_null_effects {
            let rarity_raw = def.rarity;
            let rarity = if (3..=5).contains(&rarity_raw) { 3 } else { rarity_raw };
            triggers.push(SkillData {
                skill_id: skill_id.to_string(),
                perspective,
                rarity: skill_rarity_from_i32(rarity)?,
                wisdom_check: def.wisdom_check,
                sample_policy: op.sample_policy().clone(),
                regions,
                extra_condition,
                effects,
            });
        }
    }
    if !triggers.is_empty() {
        return Ok(triggers);
    }
    // No alternative's condition is satisfiable for this course/horse. Still
    // add a placeholder (for Adventure of 564-style ActivateRandomGold
    // interactions) at a location after the course ends, with a constantly
    // false dynamic condition so it never activates normally.
    let effects = build_skill_effects(&def.alternatives[0], perspective, level);
    if effects.is_empty() && !ignore_null_effects {
        return Ok(Vec::new());
    }
    let rarity_raw = def.rarity;
    let rarity = if (3..=5).contains(&rarity_raw) { 3 } else { rarity_raw };
    let after_end = vec![Region::new(9999.0, 9999.0)];
    Ok(vec![SkillData {
        skill_id: skill_id.to_string(),
        perspective,
        rarity: skill_rarity_from_i32(rarity)?,
        wisdom_check: def.wisdom_check,
        sample_policy: crate::activation_sample_policy::SamplePolicy::Immediate,
        regions: after_end,
        extra_condition: Some(Box::new(|_s, _f| false)),
        effects,
    }])
}

// ---------------------------------------------------------------------------
// Builder

type ExtraSkillHook = Box<dyn Fn(&mut Vec<SkillData>, &HorseParameters, &CourseData)>;

pub struct RaceSolverBuilder {
    nsamples: usize,
    course: Option<&'static CourseData>,
    race_params: RaceParameters,
    horse_desc: Option<HorseSpec>,
    gate_index: i32,
    rng: Rule30CARng,
    conditions: &'static HashMap<String, Condition>,
    skills: Vec<(String, Perspective, i32)>,
    extra_skill_hooks: Vec<ExtraSkillHook>,
}

impl RaceSolverBuilder {
    pub fn new(nsamples: usize, conditions: &'static HashMap<String, Condition>, seed_lo: u32, seed_hi: u32) -> Self {
        RaceSolverBuilder {
            nsamples,
            course: None,
            race_params: RaceParameters::default(),
            horse_desc: None,
            gate_index: 0,
            rng: Rule30CARng::new(seed_lo, seed_hi),
            conditions,
            skills: Vec::new(),
            extra_skill_hooks: Vec::new(),
        }
    }

    pub fn seed(&mut self, lo: u32, hi: u32) -> &mut Self {
        self.rng = Rule30CARng::new(lo, hi);
        self
    }

    pub fn course(&mut self, course_id: i32) -> &mut Self {
        self.course = Some(course_data::get_course(course_id));
        self
    }

    pub fn mood(&mut self, mood: i32) -> &mut Self {
        self.race_params.mood = mood;
        self
    }

    pub fn ground(&mut self, ground: &str) -> CondResult<&mut Self> {
        self.race_params.ground_condition = parse_ground_condition(ground)?;
        Ok(self)
    }

    pub fn weather(&mut self, weather: &str) -> CondResult<&mut Self> {
        self.race_params.weather = parse_weather(weather)?;
        Ok(self)
    }

    pub fn season(&mut self, season: &str) -> CondResult<&mut Self> {
        self.race_params.season = parse_season(season)?;
        Ok(self)
    }

    pub fn horse(&mut self, desc: HorseSpec) -> &mut Self {
        self.horse_desc = Some(desc);
        self
    }

    pub fn gate_index(&mut self, index: i32) -> &mut Self {
        self.gate_index = index;
        self
    }

    pub fn add_skill(&mut self, skill_id: &str, level: i32) -> &mut Self {
        self.skills.push((skill_id.to_string(), Perspective::Self_, level));
        self
    }

    /// Must be called after `.horse()` and `.mood()`.
    pub fn with_asiwotameru(&mut self) -> &mut Self {
        let base_displayed_power = self.horse_desc.as_ref().expect("with_asiwotameru requires .horse() first").power * (1.0 + 0.02 * self.race_params.mood as f64);
        let hook: ExtraSkillHook = Box::new(move |skilldata, horse, course| {
            let mut power = base_displayed_power;
            for sd in skilldata.iter() {
                if let Some(power_up) = sd.effects.iter().find(|ef| ef.effect_type == SkillType::PowerUp) {
                    if !sd.regions.is_empty() && sd.regions[0].start < 9999.0 {
                        power += power_up.modifier;
                    }
                }
            }
            if power > 1200.0 {
                let spurt_start = vec![Region::new(phase_start(course.distance, 2), course.distance)];
                skilldata.push(SkillData {
                    skill_id: "asitame".to_string(),
                    perspective: Perspective::Self_,
                    rarity: SkillRarity::White,
                    wisdom_check: false,
                    sample_policy: crate::activation_sample_policy::SamplePolicy::Immediate,
                    regions: spurt_start,
                    extra_condition: Some(Box::new(|_s, _f| true)),
                    effects: vec![SkillEffect {
                        effect_type: SkillType::Accel,
                        base_duration: 3.0 / (course.distance / 1000.0),
                        modifier: asitame_calc_approximate_modifier(power, horse.strategy, course.distance_type),
                    }],
                });
            }
        });
        self.extra_skill_hooks.push(hook);
        self
    }

    pub fn with_stamina_syoubu(&mut self) -> &mut Self {
        let hook: ExtraSkillHook = Box::new(move |skilldata, horse, course| {
            let mut stamina = horse.raw_stamina;
            for sd in skilldata.iter() {
                if let Some(stamina_up) = sd.effects.iter().find(|ef| ef.effect_type == SkillType::StaminaUp) {
                    if !sd.regions.is_empty() && sd.regions[0].start < 9999.0 {
                        stamina += stamina_up.modifier;
                    }
                }
            }
            if stamina > 1200.0 {
                let spurt_start = vec![Region::new(phase_start(course.distance, 2), course.distance)];
                skilldata.push(SkillData {
                    skill_id: "staminasyoubu".to_string(),
                    perspective: Perspective::Self_,
                    rarity: SkillRarity::White,
                    wisdom_check: false,
                    sample_policy: crate::activation_sample_policy::SamplePolicy::Immediate,
                    regions: spurt_start,
                    extra_condition: Some(Box::new(|s, _f| s.current_speed >= s.last_spurt_speed)),
                    effects: vec![SkillEffect {
                        effect_type: SkillType::TargetSpeed,
                        base_duration: 9999.0,
                        modifier: stamina_syoubu_calc_approximate_modifier(stamina, course.distance),
                    }],
                });
            }
        });
        self.extra_skill_hooks.push(hook);
        self
    }

    /// Everything `build()` does before it starts constructing solvers:
    /// stat pipeline, skill-data loading, sample-policy trigger placement.
    /// Split out so `validate_skills()` can run exactly this much without
    /// paying for a throwaway `RaceSolver`.
    #[allow(clippy::type_complexity)]
    fn prepare(&mut self) -> CondResult<(HorseParameters, Vec<SkillData>, Vec<Vec<Region>>, Rule30CARng)> {
        let horse_desc = self.horse_desc.as_ref().expect("horse() must be called before build()");
        let mut horse = build_base_stats(horse_desc, self.race_params.mood);
        let solver_rng = Rule30CARng::new(self.rng.int32(), 0);
        let _pacer_rng = Rule30CARng::new(self.rng.int32(), 0); // drawn for parity with Python's rng advancement even though no pacer is ever configured on this path

        let course = self.course.expect("course() must be called before build()");
        let whole_course = vec![Region::new(0.0, course.distance)];

        let mut skilldata: Vec<SkillData> = Vec::new();
        for (skill_id, perspective, level) in &self.skills {
            skilldata.extend(build_skill_data(&horse, &self.race_params, course, &whole_course, self.conditions, skill_id, *perspective, *level, false)?);
        }
        for hook in &self.extra_skill_hooks {
            hook(&mut skilldata, &horse, course);
        }

        let mut triggers: Vec<Vec<Region>> = Vec::with_capacity(skilldata.len());
        for sd in &skilldata {
            triggers.push(sd.sample_policy.sample(&sd.regions, self.nsamples, &mut self.rng));
        }

        // must come after skill activations are decided: conditions like
        // base_power depend on BASE stats
        horse = build_adjusted_stats(&horse, course, self.race_params.ground_condition);

        Ok((horse, skilldata, triggers, solver_rng))
    }

    /// Raises whatever `prepare()` would raise for this builder's skill
    /// set, without constructing a `RaceSolver` -- used to probe whether a
    /// single skill can be built at all (see main.rs's per-skill fallback,
    /// mirroring `race_runner.is_usable`).
    pub fn validate_skills(&mut self) -> CondResult<()> {
        self.prepare()?;
        Ok(())
    }

    /// Returns one solver per sample. `nsamples` is always 1 on the path
    /// this port implements (see module doc) -- enforced here rather than
    /// silently mis-behaving, since `Box<dyn Fn>` extra_conditions aren't
    /// `Clone` and Python's generator otherwise shares the same closure
    /// object by reference across samples, which this port has no
    /// equivalent for. Unlike Python's generator, there is no `redo`/
    /// `.send()` protocol either -- also unreachable from race_runner.py's
    /// actual usage.
    pub fn build(&mut self) -> CondResult<Vec<RaceSolver>> {
        if self.nsamples != 1 {
            return Err("RaceSolverBuilder::build only supports nsamples == 1 on this port".to_string());
        }
        let (horse, skilldata, triggers, solver_rng) = self.prepare()?;
        let course = self.course.unwrap();
        let ground = self.race_params.ground_condition;

        let mut skills = Vec::with_capacity(skilldata.len());
        for (sdi, sd) in skilldata.into_iter().enumerate() {
            let trig = triggers[sdi][0];
            skills.push(crate::race_solver::PendingSkill {
                skill_id: sd.skill_id,
                perspective: sd.perspective,
                rarity: sd.rarity,
                trigger: trig,
                // `None` here plays the role Python's `k_true` sentinel
                // played: a condition whose static filter alone determines
                // whether it holds, with no further per-tick check needed.
                extra_condition: sd.extra_condition.unwrap_or_else(|| Box::new(|_s, _f| true)),
                effects: sd.effects,
            });
        }

        let mut solver_rng = solver_rng;
        let hp = HpPolicy::Game(GameHpState::new(course, ground as i32, Rule30CARng::new(solver_rng.int32(), 0)));
        let solver = RaceSolver::new(horse, course, solver_rng, skills, hp, None, None, None, self.gate_index);
        Ok(vec![solver])
    }
}
