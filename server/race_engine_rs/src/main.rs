
mod activation_conditions;
mod activation_sample_policy;
mod condition_parser;
mod course_data;
mod horse_types;
mod hp_policy;
mod json;
mod race_field;
mod race_parameters;
mod race_solver;
mod race_solver_builder;
mod random_gen;
mod region;

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, Write};

use course_data::{Corner, CourseData, Slope, Straight};
use horse_types::{Aptitude, Strategy};
use json::Value;
use race_field::RaceField;
use race_solver_builder::{HorseSpec, RaceSolverBuilder, SkillAlternative, SkillDef, SkillEffectDef, SkillTable};

const DT: f64 = 0.0666_f32 as f64;
const MAX_RACE_SECONDS: f64 = 400.0;

const COURSE_DATA_JSON: &str = include_str!("../data/course_data.json");
const SKILL_DATA_JSON: &str = include_str!("../data/skill_data.json");

fn opt_array(v: &Value, key: &str) -> Vec<Value> {
    v.get(key).and_then(Value::as_array).cloned().unwrap_or_default()
}

fn load_course_table(text: &str) -> Result<HashMap<i32, CourseData>, String> {
    let root = json::parse(text)?;
    let obj = root.as_object().ok_or("course_data.json root must be an object")?;
    let mut out = HashMap::new();
    for (course_id_str, raw) in obj {
        let course_id: i32 = course_id_str.parse().map_err(|_| format!("bad course id key {course_id_str}"))?;
        let mut slopes: Vec<Slope> = opt_array(raw, "slopes")
            .iter()
            .map(|s| Ok(Slope { start: s.get_f64("start")?, length: s.get_f64("length")?, slope: s.get_f64("slope")? }))
            .collect::<Result<_, String>>()?;
        if !course_data::is_sorted_by_start_f(slopes.iter().map(|s| s.start)) {
            slopes.sort_by(|a, b| a.start.partial_cmp(&b.start).unwrap());
        }
        let corners: Vec<Corner> = opt_array(raw, "corners")
            .iter()
            .map(|c| Ok(Corner { start: c.get_f64("start")?, length: c.get_f64("length")? }))
            .collect::<Result<_, String>>()?;
        let straights: Vec<Straight> = opt_array(raw, "straights")
            .iter()
            .map(|s| Ok(Straight { start: s.get_f64("start")?, end: s.get_f64("end")?, front_type: s.get_i64("frontType")? as i32 }))
            .collect::<Result<_, String>>()?;
        let course_set_status: Vec<i32> = opt_array(raw, "courseSetStatus").iter().filter_map(Value::as_i64).map(|x| x as i32).collect();
        let lane_max = raw.get("laneMax").and_then(Value::as_f64).unwrap_or(13000.0) / 10000.0;
        out.insert(
            course_id,
            CourseData {
                race_track_id: raw.get_i64("raceTrackId")? as i32,
                distance: raw.get_f64("distance")?,
                distance_type: raw.get_i64("distanceType")? as i32,
                surface: raw.get_i64("surface")? as i32,
                turn: raw.get_i64("turn")? as i32,
                course_set_status,
                corners,
                straights,
                slopes,
                max_lane: lane_max,
            },
        );
    }
    Ok(out)
}

fn load_skill_table(text: &str) -> Result<SkillTable, String> {
    let root = json::parse(text)?;
    let obj = root.as_object().ok_or("skill_data.json root must be an object")?;
    let mut out = HashMap::new();
    for (skill_id, raw) in obj {
        let alternatives: Vec<SkillAlternative> = raw
            .get_array("alternatives")?
            .iter()
            .map(|alt| {
                let precondition = alt.get("precondition").and_then(Value::as_str).filter(|s| !s.is_empty()).map(|s| s.to_string());
                let condition = alt.get_str("condition")?.to_string();
                let base_duration = alt.get_f64("baseDuration")?;
                let effects: Vec<SkillEffectDef> = alt
                    .get_array("effects")?
                    .iter()
                    .map(|ef| {
                        let opt_int = |key: &str, default: i64| ef.get(key).and_then(Value::as_i64).unwrap_or(default) as i32;
                        Ok(SkillEffectDef {
                            effect_type_raw: ef.get_i64("type")? as i32,
                            level_usage: opt_int("levelUsage", 1),
                            target: opt_int("target", 1),
                            target_value: opt_int("targetValue", 0),
                            value_usage: opt_int("valueUsage", 1),
                            additional_activate: opt_int("additionalActivate", 0),
                            modifier: ef.get_f64("modifier")?,
                        })
                    })
                    .collect::<Result<Vec<_>, String>>()?;
                let time_usage = alt.get("timeUsage").and_then(Value::as_i64).unwrap_or(1) as i32;
                let cooldown = alt.get("cooldown").and_then(Value::as_f64).unwrap_or(0.0) / 10000.0;
                Ok(SkillAlternative { precondition, condition, effects, base_duration, time_usage, cooldown })
            })
            .collect::<Result<Vec<_>, String>>()?;
        let rarity = raw.get_i64("rarity")? as i32;
        let wisdom_check = raw.get("wisdomCheck").and_then(Value::as_f64).map(|n| n != 0.0).unwrap_or(false);
        let green = opt_array(raw, "tags").iter().filter_map(Value::as_i64).any(|t| (601..=615).contains(&t));
        out.insert(skill_id.clone(), SkillDef { rarity, wisdom_check, green, alternatives });
    }
    Ok(out)
}


fn skill_id_and_level(v: &Value) -> Result<(String, i32, bool), String> {
    if let Some(obj) = v.as_object() {
        let id = if let Some(s) = obj.get("skillId").and_then(Value::as_str) {
            s.to_string()
        } else if let Some(n) = obj.get("skillId").and_then(Value::as_f64) {
            (n as i64).to_string()
        } else {
            return Err("skill entry missing skillId".to_string());
        };
        let level = obj.get("level").and_then(Value::as_f64).map(|n| n as i32).filter(|&n| n != 0).unwrap_or(1);
        let is_unique = obj.get("isUnique").and_then(Value::as_bool).unwrap_or(false);
        Ok((id, level, is_unique))
    } else if let Some(s) = v.as_str() {
        Ok((s.to_string(), 1, false))
    } else if let Some(n) = v.as_f64() {
        Ok(((n as i64).to_string(), 1, false))
    } else {
        Err("invalid skill entry".to_string())
    }
}

struct HorseInputSpec {
    speed: f64,
    stamina: f64,
    power: f64,
    guts: f64,
    wisdom: f64,
    strategy_str: String,
    distance_aptitude_str: String,
    surface_aptitude_str: String,
    strategy_aptitude_str: String,
    mood: i32,
    gate_index: i32,
    skills: Vec<(String, i32, bool)>,
    context: race_solver_builder::HorseContext,
}

struct RaceSpec {
    seed: i64,
    course_id: i32,
    ground: String,
    weather: String,
    season: String,
    single_mode: bool,
    horses: Vec<HorseInputSpec>,
    metadata: Value,
}

fn parse_race_spec(v: &Value) -> Result<RaceSpec, String> {
    let seed = v.get_i64("seed")?;
    let course_id = v.get_i64("courseId")? as i32;
    let ground = v.get_str("groundCondition")?.to_string();
    let weather = v.get_str("weather")?.to_string();
    let season = v.get_str("season")?.to_string();
    let single_mode = v.get("singleMode").and_then(Value::as_bool).unwrap_or(false);
    let horses_raw = v.get_array("horses")?;
    let mut horses = Vec::with_capacity(horses_raw.len());
    for (i, h) in horses_raw.iter().enumerate() {
        let mood = h.get("mood").and_then(Value::as_f64).map(|n| n as i32).unwrap_or(0);
        let gate_index = h.get("gateIndex").and_then(Value::as_f64).map(|n| n as i32).unwrap_or(i as i32);
        let opt_num = |key: &str| h.get(key).and_then(Value::as_f64);
        let team_totals: Option<Vec<f64>> = h.get("teamStatTotals").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_f64).collect());
        let stats = [h.get_f64("speed")?, h.get_f64("stamina")?, h.get_f64("power")?, h.get_f64("guts")?, h.get_f64("wisdom")?];
        let mut data = v.get("raceContext").and_then(Value::as_object).cloned().unwrap_or_default();
        data.extend(h.as_object().cloned().unwrap_or_default());
        data.extend(h.get("raceContext").and_then(Value::as_object).cloned().unwrap_or_default());
        data.insert("roster".into(), Value::Array(horses_raw.clone()));
        let ids = opt_array(h, "skills").iter().filter_map(|e| skill_id_and_level(e).ok().map(|x| Value::String(x.0))).collect();
        data.insert("skillIds".into(), Value::Array(ids));
        let context = race_solver_builder::HorseContext {
            chara_id: opt_num("charaId").map(|n| n as i32),
            fan_count: opt_num("fanCount"),
            win_count: opt_num("winCount"),
            max_raw_stat: Some(stats.iter().cloned().fold(f64::NEG_INFINITY, f64::max)),
            team_stat_totals: team_totals.filter(|t| t.len() >= 5),
            num_skills: 0,
            data,
            is_viewer: h.get("isViewer").and_then(Value::as_bool).unwrap_or(false),
        };
        let skills_raw = opt_array(h, "skills");
        let mut skills = Vec::with_capacity(skills_raw.len());
        for entry in &skills_raw {
            skills.push(skill_id_and_level(entry)?);
        }
        horses.push(HorseInputSpec {
            speed: h.get_f64("speed")?,
            stamina: h.get_f64("stamina")?,
            power: h.get_f64("power")?,
            guts: h.get_f64("guts")?,
            wisdom: h.get_f64("wisdom")?,
            strategy_str: h.get_str("strategy")?.to_string(),
            distance_aptitude_str: h.get_str("distanceAptitude")?.to_string(),
            surface_aptitude_str: h.get_str("surfaceAptitude")?.to_string(),
            strategy_aptitude_str: h.get_str("strategyAptitude")?.to_string(),
            mood,
            gate_index,
            skills,
            context,
        });
    }
    Ok(RaceSpec { seed, course_id, ground, weather, season, single_mode, horses, metadata: v.get("raceContext").cloned().unwrap_or(Value::Null) })
}

fn strategy_id_for_output(raw: &str) -> f64 {
    match raw {
        "Nige" => 1.0,
        "Senkou" => 2.0,
        "Sasi" => 3.0,
        "Oikomi" => 4.0,
        "Oonige" => 5.0,
        _ => 2.0,
    }
}


fn new_builder(
    conditions: &'static HashMap<String, activation_conditions::Condition>,
    seed: i64,
    i: usize,
    spec: &RaceSpec,
    h: &HorseInputSpec,
) -> Result<RaceSolverBuilder, String> {
    let seed_lo = (seed + i as i64 * 7919) as u32;
    let mut b = RaceSolverBuilder::new(1, conditions, seed_lo, 0);
    b.course(spec.course_id);
    let mood = h.mood.max(-2).min(2);
    b.mood(mood);
    b.ground(&spec.ground)?;
    b.weather(&spec.weather)?;
    b.season(&spec.season)?;
    b.gate_index(h.gate_index);
    b.single_mode(spec.single_mode);
    b.chara_ids(spec.horses.iter().map(|x| x.context.chara_id.unwrap_or(0)).collect());
    b.context(h.context.clone());
    b.race_metadata(spec.metadata.get("grade").and_then(Value::as_i64).unwrap_or(100) as i32, spec.metadata.get("time").and_then(Value::as_i64).unwrap_or(2) as i32)?;
    b.num_umas(spec.horses.len() as i32);
    let desc = HorseSpec {
        speed: h.speed,
        stamina: h.stamina,
        power: h.power,
        guts: h.guts,
        wisdom: h.wisdom,
        strategy: Strategy::parse_str(&h.strategy_str)?,
        distance_aptitude: Aptitude::parse_str(&h.distance_aptitude_str, "distance")?,
        surface_aptitude: Aptitude::parse_str(&h.surface_aptitude_str, "surface")?,
        strategy_aptitude: Aptitude::parse_str(&h.strategy_aptitude_str, "strategy")?,
    };
    b.horse(desc);
    b.with_asiwotameru();
    b.with_stamina_syoubu();
    Ok(b)
}

fn is_usable(
    cache: &mut HashMap<(usize, String, i32), bool>,
    conditions: &'static HashMap<String, activation_conditions::Condition>,
    spec: &RaceSpec,
    h: &HorseInputSpec,
    i: usize,
    skill_id: &str,
    level: i32,
) -> bool {
    let key = (i, skill_id.to_string(), level);
    if let Some(&ok) = cache.get(&key) {
        return ok;
    }
    let ok = (|| -> Result<(), String> {
        let mut probe = new_builder(conditions, spec.seed, i, spec, h)?;
        probe.add_skill(skill_id, level);
        probe.validate_skills()
    })()
    .is_ok();
    cache.insert(key, ok);
    ok
}

fn wire_duration(a: &race_solver::Activation) -> f64 {
    if let Some(d) = a.timed_duration {
        return d;
    }
    if a.t <= 0.0 || a.has_stat_effect {
        -1.0
    } else {
        0.0
    }
}

fn run_race(spec: &RaceSpec, conditions: &'static HashMap<String, activation_conditions::Condition>) -> Result<Value, String> {
    let course = course_data::get_course(spec.course_id);

    let mut skill_usable: HashMap<(usize, String, i32), bool> = HashMap::new();
    let mut skipped_skills = Vec::new();

    let mut solvers = Vec::with_capacity(spec.horses.len());
    let mut unique_ids: Vec<HashSet<String>> = Vec::with_capacity(spec.horses.len());
    for (i, h) in spec.horses.iter().enumerate() {
        let mut builder = new_builder(conditions, spec.seed, i, spec, h)?;
        for (skill_id, level, _is_unique) in &h.skills {
            builder.add_skill(skill_id, *level);
        }
        let solver = match builder.build() {
            Ok(mut v) => v.pop().unwrap(),
            Err(_) => {
                let mut retry = new_builder(conditions, spec.seed, i, spec, h)?;
                for (skill_id, level, _is_unique) in &h.skills {
                    if !is_usable(&mut skill_usable, conditions, spec, h, i, skill_id, *level) {
                        skipped_skills.push(json::obj(vec![("gateIndex", json::num(h.gate_index as f64)), ("skillId", Value::String(skill_id.clone()))]));
                        continue;
                    }
                    retry.add_skill(skill_id, *level);
                }
                retry.build()?.pop().unwrap()
            }
        };
        unique_ids.push(h.skills.iter().filter(|(_, _, u)| *u).map(|(id, _, _)| id.clone()).collect());
        solvers.push(solver);
    }

    let field = RaceField::new(solvers);

    let n = field.n;
    let mut finished: Vec<Option<f64>> = vec![None; n];
    let mut finished_step: Vec<Option<f64>> = vec![None; n];
    let mut hp_out = vec![false; n];
    let mut blocked_front_time = vec![0.0f64; n];
    let mut frames: Vec<Value> = Vec::new();
    let gate_horses = (0..n).map(|i| {
        let s = field.solvers[i].borrow();
        json::obj(vec![
            ("pos", json::num(s.pos)), ("speed", json::num(s.current_speed)),
            ("hp", json::num(s.hp.hp_value().unwrap_or(0.0).max(0.0))),
            ("lane", json::num(s.lane)), ("tempt", json::num(0.0)),
            ("block", json::num(-1.0)),
        ])
    }).collect();
    frames.push(json::obj(vec![("t", json::num(0.0)), ("horses", json::arr(gate_horses))]));
    let mut distance_diff_max = 0.0f64;

    let mut t = 0.0f64;
    while finished.iter().any(|f| f.is_none()) && t < MAX_RACE_SECONDS {
        let active: Vec<usize> = (0..n).filter(|&i| finished[i].is_none()).collect();
        let prev_pos: HashMap<usize, f64> = active.iter().map(|&i| (i, field.solvers[i].borrow().pos)).collect();
        field.step_all(DT, &active);
        for &i in &active {
            let mut s = field.solvers[i].borrow_mut();
            if !s.hp.has_remaining_hp() {
                hp_out[i] = true;
            }
            if s.front_blocker_index >= 0 {
                blocked_front_time[i] += DT;
            }
            if s.pos >= course.distance {
                let span = s.pos - prev_pos[&i];
                let frac = if span > 0.0 { (course.distance - prev_pos[&i]) / span } else { 0.0 };
                finished[i] = Some(t + DT * frac);
                finished_step[i] = Some(t + DT);
                s.finished = true;
            }
        }
        t = ((t as f32) + (DT as f32)) as f64;
        if finished.iter().all(|f| f.is_none()) {
            let positions: Vec<f64> = (0..n).map(|i| field.solvers[i].borrow().pos).collect();
            let hi = positions.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let lo = positions.iter().cloned().fold(f64::INFINITY, f64::min);
            distance_diff_max = distance_diff_max.max(hi - lo);
        }

        let horses: Vec<Value> = (0..n)
            .map(|i| {
                let s = field.solvers[i].borrow();
                let hp_val = s.hp.hp_value().map(|v| v.max(0.0)).unwrap_or(0.0);
                let tempt = if s.is_kakari { s.temptation_mode as i32 } else { 0 };
                json::obj(vec![
                    ("pos", json::num(s.pos)),
                    ("speed", json::num(s.current_speed)),
                    ("hp", json::num(hp_val)),
                    ("lane", json::num(s.lane)),
                    ("tempt", json::num(tempt as f64)),
                    ("block", json::num(s.front_blocker_index as f64)),
                ])
            })
            .collect();
        frames.push(json::obj(vec![("t", json::num(t)), ("horses", json::arr(horses))]));
    }

    for i in 0..n {
        if finished[i].is_none() {
            finished[i] = Some(t);
            finished_step[i] = Some(t);
        }
    }

    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| finished[a].unwrap().partial_cmp(&finished[b].unwrap()).unwrap());
    let mut order_index = vec![0usize; n];
    for (rank, &horse_i) in order.iter().enumerate() {
        order_index[horse_i] = rank;
    }

    let mut results = Vec::with_capacity(n);
    for i in 0..n {
        let s = field.solvers[i].borrow();
        let lst = s.last_spurt_start_pos;
        let spurted = 0.0 < lst && lst < course.distance - 60.0 - 1e-6;
        results.push(json::obj(vec![
            ("finishOrder", json::num(order_index[i] as f64)),
            ("finishTime", json::num(finished_step[i].unwrap())),
            ("finishTimeRaw", json::num(finished[i].unwrap())),
            ("startDelayTime", json::num(s.start_delay)),
            ("lastSpurtStartDistance", json::num(if spurted { lst } else { -1.0 })),
            ("runningStyle", json::num(strategy_id_for_output(&spec.horses[i].strategy_str))),
            ("rushed", Value::Bool(s.temptation_count > 0 && s.kakari_start <= course.distance)),
            ("hpOut", Value::Bool(hp_out[i])),
            ("blockedFrontTime", json::num(blocked_front_time[i])),
        ]));
    }

    let mut skill_events: Vec<Value> = Vec::new();
    let mut race_events: Vec<Value> = Vec::new();
    for i in 0..n {
        let s = field.solvers[i].borrow();
        for a in &s.activation_log {
            let sid: i64 = match a.skill_id.parse() {
                Ok(v) => v,
                Err(_) => continue,
            };
            let targets: Vec<Value> = if a.targets.is_empty() { vec![json::num(i as f64)] } else { a.targets.iter().map(|&x| json::num(x as f64)).collect() };
            skill_events.push(json::obj(vec![
                ("horseIndex", json::num(i as f64)),
                ("skillId", json::num(sid as f64)),
                ("t", json::num(a.t)),
                ("duration", json::num(wire_duration(a))),
                ("isUnique", json::num(if unique_ids[i].contains(&a.skill_id) { 1.0 } else { 0.0 })),
                ("targets", json::arr(targets)),
            ]));
        }
        let mut spans = s.kakari_log.clone();
        if s.is_kakari {
            spans.push((s.kakari_started_at, t));
        }
        for (start_t, end_t) in spans {
            skill_events.push(json::obj(vec![
                ("horseIndex", json::num(i as f64)),
                ("skillId", Value::String("kakari".to_string())),
                ("t", json::num(start_t)),
                ("duration", json::num(end_t - start_t)),
                ("isUnique", json::num(0.0)),
            ]));
        }
        for &(at, type_id) in &s.event_log {
            race_events.push(json::obj(vec![
                ("horseIndex", json::num(i as f64)),
                ("type", json::num(type_id as f64)),
                ("t", json::num(at)),
            ]));
        }
    }

    Ok(json::obj(vec![
        ("seed", json::num(spec.seed as f64)),
        ("frames", json::arr(frames)),
        ("results", json::arr(results)),
        ("skillEvents", json::arr(skill_events)),
        ("skippedSkills", json::arr(skipped_skills)),
        ("raceEvents", json::arr(race_events)),
        ("distanceDiffMax", json::num(distance_diff_max)),
    ]))
}

const DENSE_LEAD_FRAMES: usize = 17;
const SPARSE_STRIDE: usize = 16;
const GOAL_WINDOW: f64 = 25.0;

fn downsample_result(result: Value, distance: f64) -> Value {
    let mut map = match result {
        Value::Object(m) => m,
        other => return other,
    };
    let frames = match map.remove("frames") {
        Some(Value::Array(f)) => f,
        Some(other) => {
            map.insert("frames".to_string(), other);
            return Value::Object(map);
        }
        None => return Value::Object(map),
    };

    let frame_t = |f: &Value| f.get("t").and_then(Value::as_f64).unwrap_or(0.0);
    let gate_needed = frames.first().map_or(false, |f| frame_t(f) > 0.0);
    let off = if gate_needed { 1 } else { 0 };
    let eff_len = frames.len() + off;

    let mut finish: Vec<Option<(f64, f64, f64, f64)>> = Vec::new();
    let mut finish_frames: Vec<Value> = Vec::new();
    if let Some(results) = map.get("results").and_then(Value::as_array) {
        for (hi, r) in results.iter().enumerate() {
            let finish_t = r.get("finishTimeRaw").and_then(Value::as_f64).unwrap_or(f64::INFINITY);
            let mut best: Option<&Value> = None;
            for f in frames.iter() {
                if frame_t(f) <= finish_t {
                    best = Some(f);
                } else {
                    break;
                }
            }
            let h = best.and_then(|f| f.get("horses")).and_then(Value::as_array).and_then(|hs| hs.get(hi));
            match (best, h) {
                (Some(f), Some(h)) => {
                    let g = |k: &str| h.get(k).and_then(Value::as_f64).unwrap_or(0.0);
                    finish.push(Some((g("pos"), g("speed"), frame_t(f), g("lane"))));
                    finish_frames.push(json::obj(vec![
                        ("pos", h.get("pos").cloned().unwrap_or(Value::Null)),
                        ("speed", h.get("speed").cloned().unwrap_or(Value::Null)),
                        ("t", f.get("t").cloned().unwrap_or(Value::Null)),
                        ("lane", h.get("lane").cloned().unwrap_or(Value::Null)),
                    ]));
                }
                _ => {
                    finish.push(None);
                    finish_frames.push(Value::Null);
                }
            }
        }
    }

    let near_goal = |f: &Value| -> bool {
        let t = frame_t(f);
        f.get("horses").and_then(Value::as_array).map_or(false, |hs| {
            hs.iter().enumerate().any(|(hi, h)| {
                let mut pos = h.get("pos").and_then(Value::as_f64).unwrap_or(0.0);
                if let Some(Some((fp, fs, ft, _))) = finish.get(hi) {
                    if t > *ft {
                        pos = fp + fs * (t - ft);
                    }
                }
                (distance - pos).abs() < GOAL_WINDOW
            })
        })
    };

    let mut keep = vec![false; eff_len];
    if eff_len <= DENSE_LEAD_FRAMES {
        keep.iter_mut().for_each(|k| *k = true);
    } else {
        keep.iter_mut().take(DENSE_LEAD_FRAMES).for_each(|k| *k = true);
        let mut last = DENSE_LEAD_FRAMES - 1;
        let mut dense = false;
        for k in DENSE_LEAD_FRAMES..eff_len {
            let near = near_goal(&frames[k - off]);
            if k - last >= SPARSE_STRIDE || (dense && near) {
                keep[k] = true;
                last = k;
                dense = near;
            }
        }
        keep[eff_len - 1] = true;
    }

    let raw_frame_count = frames.len();
    let kept: Vec<Value> = frames.into_iter().enumerate().filter(|(i, _)| keep[i + off]).map(|(_, f)| f).collect();

    map.insert("frames".to_string(), Value::Array(kept));
    map.insert("finishFrames".to_string(), Value::Array(finish_frames));
    map.insert("rawFrameCount".to_string(), json::num(raw_frame_count as f64));
    map.insert("framesDownsampled".to_string(), Value::Bool(true));
    map.insert("gateFrameNeeded".to_string(), Value::Bool(gate_needed));
    Value::Object(map)
}

fn run() -> Result<(), String> {
    let courses = load_course_table(COURSE_DATA_JSON)?;
    course_data::init_courses(courses);
    let skills = match std::env::var("UMA_RACE_SKILL_TABLE").ok().and_then(|p| std::fs::read_to_string(p).ok()) {
        Some(text) => load_skill_table(&text)?,
        None => load_skill_table(SKILL_DATA_JSON)?,
    };
    race_solver_builder::init_skills(skills);

    let conditions: &'static HashMap<String, activation_conditions::Condition> = Box::leak(Box::new(activation_conditions::build_conditions()));

    let stdin = std::io::stdin();
    let mut reader = stdin.lock();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut line = String::new();
    loop {
        line.clear();
        if reader.read_line(&mut line).map_err(|e| e.to_string())? == 0 {
            break;
        }
        if line.trim().is_empty() {
            continue;
        }
        let outcome = (|| -> Result<Value, String> {
            let spec_json = json::parse(&line)?;
            let spec = parse_race_spec(&spec_json)?;
            let distance = course_data::get_course(spec.course_id).distance;
            let result = run_race(&spec, conditions)?;
            if spec_json.get("allFrames").and_then(Value::as_bool).unwrap_or(false) {
                return Ok(result);
            }
            Ok(downsample_result(result, distance))
        })();
        let reply = match outcome {
            Ok(v) => v,
            Err(e) => json::obj(vec![("error", Value::String(e))]),
        };
        writeln!(out, "{}", json::to_string(&reply)).map_err(|e| e.to_string())?;
        out.flush().map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("race runner error: {e}");
        std::process::exit(1);
    }
}
