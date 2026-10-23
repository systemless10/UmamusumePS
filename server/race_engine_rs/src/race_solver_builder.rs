
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


pub struct SkillEffectDef {
    pub effect_type_raw: i32,
    pub level_usage: i32,
    pub target: i32,
    pub target_value: i32,
    pub value_usage: i32,
    pub additional_activate: i32,
    pub modifier: f64,
}

pub struct SkillAlternative {
    pub precondition: Option<String>,
    pub condition: String,
    pub effects: Vec<SkillEffectDef>,
    pub base_duration: f64,
    pub time_usage: i32,
    pub cooldown: f64,
}

pub struct SkillDef {
    pub rarity: i32,
    pub wisdom_check: bool,
    pub green: bool,
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
    let random_factor = 1.0;
    (stamina - 1200.0).sqrt() * 0.0085 * stamina_syoubu_distance_factor(distance) * random_factor
}

fn adjust_overcap(stat: f64) -> f64 {
    if stat > 1200.0 {
        (1200.0_f32 + ((stat as f32 - 1200.0_f32) * 0.5_f32)) as f64
    } else {
        stat
    }
}

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

fn base_stat(raw: f64, motiv_coef: f64) -> f64 {
    ((adjust_overcap(raw) as f32 * motiv_coef as f32) as f64).max(1.0).min(2000.0)
}

pub fn build_base_stats(desc: &HorseSpec, mood: i32) -> HorseParameters {
    let motiv_coef = ((10000 + 200 * mood) as f32 * 0.0001_f32) as f64;
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

const SINGLE_MODE_MODIFIER: f64 = 400.0;

fn clamp_stat(v: f64) -> f64 {
    (v as f32 as f64).max(1.0).min(2000.0)
}

pub fn build_adjusted_stats(base: &HorseParameters, course: &CourseData, ground: GroundCondition, single_mode: bool) -> HorseParameters {
    let race_course_modifier = course_data::course_speed_modifier(course, base.speed, base.stamina, base.power, base.guts, base.wisdom);
    let ground = ground as usize;
    let sm = if single_mode { SINGLE_MODE_MODIFIER } else { 0.0 };
    HorseParameters {
        speed: clamp_stat(((base.speed as f32 * race_course_modifier as f32) + GROUND_SPEED_MODIFIER[course.surface as usize][ground] as f32) as f64 + sm),
        stamina: clamp_stat(base.stamina + sm),
        power: clamp_stat(base.power + GROUND_POWER_MODIFIER[course.surface as usize][ground] + sm),
        guts: clamp_stat(base.guts + sm),
        wisdom: clamp_stat((base.wisdom as f32 * STRATEGY_PROFICIENCY_MODIFIER[base.strategy_aptitude as usize] as f32) as f64 + sm),
        strategy: base.strategy,
        distance_aptitude: base.distance_aptitude,
        surface_aptitude: base.surface_aptitude,
        strategy_aptitude: base.strategy_aptitude,
        raw_stamina: base.raw_stamina,
    }
}


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
        6 => Some(SkillType::Oonige),
        8 => Some(SkillType::VisibleDistance),
        35 => Some(SkillType::TargetLane),
        38 => Some(SkillType::DebuffCancel),
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

fn multiply_random(roll: f64) -> f64 {
    if roll < 0.6 {
        0.0
    } else if roll < 0.9 {
        0.02
    } else {
        0.04
    }
}

#[derive(Clone, Default)]
pub struct HorseContext {
    pub chara_id: Option<i32>,
    pub fan_count: Option<f64>,
    pub win_count: Option<f64>,
    pub max_raw_stat: Option<f64>,
    pub team_stat_totals: Option<Vec<f64>>,
    pub num_skills: usize,
    pub is_viewer: bool,
    pub data: std::collections::BTreeMap<String, crate::json::Value>,
}

pub fn bucket(value: f64, table: &[(Option<f64>, f64)]) -> f64 {
    for &(bound, mult) in table {
        match bound {
            Some(b) if value >= b => continue,
            _ => return mult,
        }
    }
    table[table.len() - 1].1
}

const TEAM_TOTAL_MULT: [(Option<f64>, f64); 5] = [(Some(1200.0), 0.8), (Some(1800.0), 0.9), (Some(2600.0), 1.0), (Some(3600.0), 1.1), (None, 1.2)];
const WIN_COUNT_MULT: [(Option<f64>, f64); 5] = [(Some(6.0), 0.8), (Some(14.0), 0.9), (Some(18.0), 1.0), (Some(25.0), 1.1), (None, 1.2)];
const MAX_RAW_STAT_MULT: [(Option<f64>, f64); 5] = [(Some(600.0), 0.8), (Some(800.0), 0.9), (Some(1000.0), 1.0), (Some(1100.0), 1.1), (None, 1.2)];
const FAN_COUNT_MULT: [(Option<f64>, f64); 5] = [(Some(100000.0), 0.8), (Some(200000.0), 0.9), (Some(300000.0), 1.0), (Some(400000.0), 1.1), (None, 1.2)];

fn static_value_multiplier(value_usage: i32, ctx: &HorseContext) -> f64 {
    match value_usage {
        2 => (1.0 + 0.01 * ctx.num_skills as f64).min(1.2),
        3..=7 => ctx.team_stat_totals.as_ref().map_or(1.0, |t| bucket(t[(value_usage - 3) as usize], &TEAM_TOTAL_MULT)),
        10 => ctx.win_count.map_or(1.0, |v| bucket(v, &WIN_COUNT_MULT)),
        12 => ctx.fan_count.map_or(1.0, |v| bucket(v, &FAN_COUNT_MULT)),
        13 => ctx.max_raw_stat.map_or(1.0, |v| bucket(v, &MAX_RAW_STAT_MULT)),
        _ => 1.0,
    }
}

fn build_skill_effects(alt: &SkillAlternative, perspective: Perspective, level: i32, rng: &mut Rule30CARng, ctx: &HorseContext) -> Vec<SkillEffect> {
    alt.effects
        .iter()
        .map(|ef| {
            let mut effective_type = valid_skill_type(ef.effect_type_raw).unwrap_or(SkillType::Noop);
            if perspective != Perspective::Self_ && !is_target(perspective, ef.target) {
                effective_type = SkillType::Noop;
            }
            let mut modifier = ef.modifier / 10000.0 * match ef.level_usage {2 => 1.0, 3 => 1.0 / level_multiplier(effective_type,level), _ => level_multiplier(effective_type,level)};
            if ef.value_usage == 8 || ef.value_usage == 9 {
                modifier *= multiply_random(rng.random());
            } else {
                modifier *= static_value_multiplier(ef.value_usage, ctx);
            }
            SkillEffect {
                effect_type: effective_type,
                base_duration: alt.base_duration / 10000.0,
                modifier,
                target: if perspective == Perspective::Self_ { ef.target } else { SKILL_TARGET_SELF },
                target_value: ef.target_value,
                time_usage: alt.time_usage,
                value_usage: ef.value_usage,
                additional_activate: ef.additional_activate,
            }
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
    pub cooldown: f64,
    pub green: bool,
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
    rng: &mut Rule30CARng,
    ctx: &HorseContext,
) -> CondResult<Vec<SkillData>> {
    let skills = skills_table();
    let def = skills.get(skill_id).ok_or_else(|| format!("bad skill ID {skill_id}"))?;
    let extra = RaceParametersWithSkillId { params: race_params.clone(), skill_id: skill_id.to_string() };

    let mut triggers: Vec<SkillData> = Vec::new();
    for alt in &def.alternatives {
        let mut pre_check: activation_conditions::OptCond = None;
        let mut pre_bounds = Vec::new();
        let mut full: Vec<Region> = whole_course.to_vec();
        if let Some(precondition) = alt.precondition.as_deref() {
            if !precondition.is_empty() {
                let pre = condition_parser::parse(conditions, precondition)?;
                let (pre_regions, cond) = pre.apply(whole_course, course, horse, &extra)?;
                pre_check = cond;
                pre_bounds = pre_regions.clone();
                if pre_regions.is_empty() {
                    continue;
                }
                let bounds = Region::new(pre_regions[0].start, whole_course.last().unwrap().end);
                full = rmap_one(&full, |r| r.intersect(&bounds));
            }
        }

        let op = condition_parser::parse(conditions, &alt.condition)?;
        let (regions, mut extra_condition) = op.apply(&full, course, horse, &extra)?;
        if let Some(pre) = pre_check {
            let active = extra_condition;
            let seen = std::cell::Cell::new(false);
            extra_condition = Some(Box::new(move |s,f| {
                if pre_bounds.iter().any(|r| r.start <= s.pos && s.pos < r.end) && pre(s,f) { seen.set(true); }
                seen.get() && active.as_ref().map(|c|c(s,f)).unwrap_or(true)
            }));
        }
        if regions.is_empty() {
            continue;
        }
        if !triggers.is_empty() && !alt.condition.contains("is_activate_other_skill_detail") && !alt.condition.contains("is_used_skill_id") {
            continue;
        }
        let effects = build_skill_effects(alt, perspective, level, rng, ctx);
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
                cooldown: alt.cooldown,
                green: def.green,
            });
        }
    }
    if !triggers.is_empty() {
        return Ok(triggers);
    }
    let effects = build_skill_effects(&def.alternatives[0], perspective, level, rng, ctx);
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
        cooldown: 0.0,
        green: false,
    }])
}


type ExtraSkillHook = Box<dyn Fn(&mut Vec<SkillData>, &HorseParameters, &CourseData)>;

pub struct RaceSolverBuilder {
    nsamples: usize,
    course: Option<&'static CourseData>,
    race_params: RaceParameters,
    horse_desc: Option<HorseSpec>,
    gate_index: i32,
    single_mode: bool,
    context: HorseContext,
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
            single_mode: false,
            context: HorseContext::default(),
            rng: Rule30CARng::seeded(((seed_hi as u64) << 32) | seed_lo as u64),
            conditions,
            skills: Vec::new(),
            extra_skill_hooks: Vec::new(),
        }
    }

    pub fn seed(&mut self, lo: u32, hi: u32) -> &mut Self {
        self.rng = Rule30CARng::seeded(((hi as u64) << 32) | lo as u64);
        self
    }

    pub fn context(&mut self, ctx: HorseContext) -> &mut Self {
        self.race_params.context = ctx.data.clone();
        self.race_params.popularity = ctx.data.get("popularity").and_then(crate::json::Value::as_i64).unwrap_or(1) as i32;
        self.context = ctx;
        self
    }

    pub fn chara_ids(&mut self, ids: Vec<i32>) -> &mut Self {
        self.race_params.chara_ids = ids;
        self
    }

    pub fn single_mode(&mut self, flag: bool) -> &mut Self {
        self.single_mode = flag;
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

    pub fn num_umas(&mut self, n: i32) { self.race_params.num_umas = Some(n); }

    pub fn race_metadata(&mut self, grade: i32, time: i32) -> CondResult<&mut Self> {
        use crate::race_parameters::{Grade, RaceTime};
        self.race_params.grade = match grade {
            0 => Grade::None, 1000 => Grade::Special, 100 => Grade::G1, 200 => Grade::G2, 300 => Grade::G3, 400 => Grade::Op,
            700 => Grade::PreOp, 800 => Grade::Maiden, 900 => Grade::Debut, 999 => Grade::Daily,
            _ => return Err(format!("unknown race grade {grade}")),
        };
        self.race_params.time = match time {
            0 => RaceTime::NoTime, 1 => RaceTime::Morning, 2 => RaceTime::Midday,
            3 => RaceTime::Evening, 4 => RaceTime::Night,
            _ => return Err(format!("unknown race time {time}")),
        };
        Ok(self)
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
                    extra_condition: Some(Box::new(|s, _f| s.is_last_spurt)),
                    effects: vec![SkillEffect::on_self(
                        SkillType::Accel,
                        3.0 / (course.distance / 1000.0),
                        asitame_calc_approximate_modifier(power, horse.strategy, course.distance_type),
                    )],
                    cooldown: 0.0,
                    green: false,
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
                    effects: vec![SkillEffect::on_self(
                        SkillType::TargetSpeed,
                        9999.0,
                        stamina_syoubu_calc_approximate_modifier(stamina, course.distance),
                    )],
                    cooldown: 0.0,
                    green: false,
                });
            }
        });
        self.extra_skill_hooks.push(hook);
        self
    }

    #[allow(clippy::type_complexity)]
    fn prepare(&mut self) -> CondResult<(HorseParameters, Vec<SkillData>, Vec<Vec<Region>>, Vec<Vec<Region>>, Rule30CARng)> {
        let horse_desc = self.horse_desc.as_ref().expect("horse() must be called before build()");
        let mut horse = build_base_stats(horse_desc, self.race_params.mood);
        let solver_rng = Rule30CARng::seeded(self.rng.int32() as u64);
        let _pacer_rng = Rule30CARng::seeded(self.rng.int32() as u64);
        let mut skill_rng = Rule30CARng::seeded(self.rng.int32() as u64);

        let course = self.course.expect("course() must be called before build()");
        let whole_course = vec![Region::new(0.0, course.distance)];

        let mut ctx = self.context.clone();
        ctx.num_skills = self.skills.len();
        let mut skilldata: Vec<SkillData> = Vec::new();
        for (skill_id, perspective, level) in &self.skills {
            skilldata.extend(build_skill_data(&horse, &self.race_params, course, &whole_course, self.conditions, skill_id, *perspective, *level, false, &mut skill_rng, &ctx)?);
        }
        for hook in &self.extra_skill_hooks {
            hook(&mut skilldata, &horse, course);
        }

        let mut triggers: Vec<Vec<Region>> = Vec::with_capacity(skilldata.len());
        let mut followups: Vec<Vec<Region>> = Vec::with_capacity(skilldata.len());
        for sd in &skilldata {
            if matches!(sd.sample_policy, crate::activation_sample_policy::SamplePolicy::AllCornerRandom) {
                let all = crate::activation_sample_policy::place_all_corner_trigger_list(&sd.regions, &mut self.rng);
                triggers.push(vec![all[0]]);
                followups.push(all[1..].to_vec());
            } else {
                triggers.push(sd.sample_policy.sample(&sd.regions, self.nsamples, &mut self.rng));
                followups.push(Vec::new());
            }
        }

        let own_chance = (1.0 - 90.0 / horse.wisdom).max(0.2);
        let mut passed: HashMap<String, bool> = HashMap::new();
        let mut keep: Vec<bool> = Vec::with_capacity(skilldata.len());
        for sd in &skilldata {
            let mut live = true;
            if sd.wisdom_check && sd.perspective == Perspective::Self_ {
                let ok = *passed.entry(sd.skill_id.clone()).or_insert_with(|| skill_rng.random() < own_chance);
                live = ok;
            }
            keep.push(live);
        }
        let mut k = keep.iter();
        skilldata.retain(|_| *k.next().unwrap());
        let mut k = keep.iter();
        triggers.retain(|_| *k.next().unwrap());
        let mut k = keep.iter();
        followups.retain(|_| *k.next().unwrap());

        horse = build_adjusted_stats(&horse, course, self.race_params.ground_condition, self.single_mode);

        Ok((horse, skilldata, triggers, followups, solver_rng))
    }

    pub fn validate_skills(&mut self) -> CondResult<()> {
        self.prepare()?;
        Ok(())
    }

    pub fn build(&mut self) -> CondResult<Vec<RaceSolver>> {
        if self.nsamples != 1 {
            return Err("RaceSolverBuilder::build only supports nsamples == 1 on this port".to_string());
        }
        let (horse, skilldata, triggers, followups, solver_rng) = self.prepare()?;
        let cooldown_scale = self.course.unwrap().distance / 1000.0;
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
                extra_condition: std::rc::Rc::from(sd.extra_condition.unwrap_or_else(|| Box::new(|_s, _f| true))),
                effects: sd.effects,
                cooldown: sd.cooldown * cooldown_scale,
                followups: followups[sdi].clone(),
                green: sd.green,
            });
        }

        let mut solver_rng = solver_rng;
        let hp = HpPolicy::Game(GameHpState::new(course, ground as i32, Rule30CARng::seeded(solver_rng.int32() as u64)));
        let mut solver = RaceSolver::new(horse, course, solver_rng, skills, hp, None, None, None, self.gate_index);
        solver.chara_id = self.context.chara_id.unwrap_or(0);
        solver.is_viewer = self.context.is_viewer;
        solver.context = self.context.data.clone();
        Ok(vec![solver])
    }
}
