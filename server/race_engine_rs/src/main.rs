//! Port of race_runner.py -- reads one race spec as JSON on stdin, writes
//! `{seed, frames, results, skillEvents}` as JSON on stdout. Drop-in
//! replacement for the hot path only: `race_simulator.py`'s existing
//! `_run_simulation_node` already shells out to a Node subprocess with
//! exactly this contract, so swapping the subprocess target is the whole
//! integration (see that function and `race_runner.run_race`).

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

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, Write};
use std::rc::Rc;

use course_data::{Corner, CourseData, Slope, Straight};
use horse_types::{Aptitude, Strategy};
use json::Value;
use race_field::RaceField;
use race_solver_builder::{HorseSpec, RaceSolverBuilder, SkillAlternative, SkillDef, SkillEffectDef, SkillTable};

const DT: f64 = 1.0 / 15.0;
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
                        Ok(SkillEffectDef { effect_type_raw: ef.get_i64("type")? as i32, target: ef.get_i64("target")? as i32, modifier: ef.get_f64("modifier")? })
                    })
                    .collect::<Result<Vec<_>, String>>()?;
                Ok(SkillAlternative { precondition, condition, effects, base_duration })
            })
            .collect::<Result<Vec<_>, String>>()?;
        let rarity = raw.get_i64("rarity")? as i32;
        let wisdom_check = raw.get("wisdomCheck").and_then(Value::as_f64).map(|n| n != 0.0).unwrap_or(false);
        out.insert(skill_id.clone(), SkillDef { rarity, wisdom_check, alternatives });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Race spec (stdin JSON) parsing

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
}

struct RaceSpec {
    seed: i64,
    course_id: i32,
    ground: String,
    weather: String,
    season: String,
    horses: Vec<HorseInputSpec>,
}

fn parse_race_spec(v: &Value) -> Result<RaceSpec, String> {
    let seed = v.get_i64("seed")?;
    let course_id = v.get_i64("courseId")? as i32;
    let ground = v.get_str("groundCondition")?.to_string();
    let weather = v.get_str("weather")?.to_string();
    let season = v.get_str("season")?.to_string();
    let horses_raw = v.get_array("horses")?;
    let mut horses = Vec::with_capacity(horses_raw.len());
    for (i, h) in horses_raw.iter().enumerate() {
        let mood = h.get("mood").and_then(Value::as_f64).map(|n| n as i32).unwrap_or(0);
        let gate_index = h.get("gateIndex").and_then(Value::as_f64).map(|n| n as i32).unwrap_or(i as i32);
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
        });
    }
    Ok(RaceSpec { seed, course_id, ground, weather, season, horses })
}

/// `_STRATEGY_IDS.get(spec["horses"][i]["strategy"], 2)` in Python: an
/// *exact*, case-sensitive string match against the raw input (not the
/// parsed `Strategy` enum, which is case-insensitive and accepts "SASHI").
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

// ---------------------------------------------------------------------------
// Builder construction, mirroring race_runner._new_builder

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
    // Doc's "Charge Up / Conserve Power" and "Stamina Limit Break" -- both
    // already ported but never enabled by the upstream raceRunner.ts
    // bridge. Real Global mechanics, so enabled here. Must come after
    // .horse()/.mood() (both read from them).
    b.with_asiwotameru();
    b.with_stamina_syoubu();
    Ok(b)
}

fn is_usable(
    cache: &mut HashMap<String, bool>,
    conditions: &'static HashMap<String, activation_conditions::Condition>,
    spec: &RaceSpec,
    h: &HorseInputSpec,
    i: usize,
    skill_id: &str,
    level: i32,
) -> bool {
    if let Some(&ok) = cache.get(skill_id) {
        return ok;
    }
    let ok = (|| -> Result<(), String> {
        let mut probe = new_builder(conditions, spec.seed, i, spec, h)?;
        probe.add_skill(skill_id, level);
        probe.validate_skills()
    })()
    .is_ok();
    cache.insert(skill_id.to_string(), ok);
    ok
}

fn run_race(spec: &RaceSpec, conditions: &'static HashMap<String, activation_conditions::Condition>) -> Result<Value, String> {
    let course = course_data::get_course(spec.course_id);

    let skill_events: Rc<RefCell<Vec<Value>>> = Rc::new(RefCell::new(Vec::new()));
    let kakari_start: Rc<RefCell<HashMap<usize, f64>>> = Rc::new(RefCell::new(HashMap::new()));
    // (horse_index, skill_id) -> (activation time, isUnique), until the
    // deactivate hook finalizes it with the real elapsed duration -- same
    // reasoning as kakari_start, applied to every real skill too (matches
    // race_runner.py's pending_skills / make_activate_hook/make_deactivate_hook).
    let pending_skills: Rc<RefCell<HashMap<(usize, i64), (f64, bool)>>> = Rc::new(RefCell::new(HashMap::new()));
    // Whether a skill id can actually be built for this race. Some skills'
    // conditions reference tokens this engine doesn't implement and those
    // fail from inside build() (not add_skill()), so a single bad skill
    // would otherwise abort the whole horse's build. Memoized across the
    // roster (most horses share most of their skills).
    let mut skill_usable: HashMap<String, bool> = HashMap::new();

    let mut solvers = Vec::with_capacity(spec.horses.len());
    for (i, h) in spec.horses.iter().enumerate() {
        let mut builder = new_builder(conditions, spec.seed, i, spec, h)?;
        for (skill_id, level, _is_unique) in &h.skills {
            builder.add_skill(skill_id, *level);
        }
        let mut solver = match builder.build() {
            Ok(mut v) => v.pop().unwrap(),
            Err(_) => {
                let mut retry = new_builder(conditions, spec.seed, i, spec, h)?;
                for (skill_id, level, _is_unique) in &h.skills {
                    if !is_usable(&mut skill_usable, conditions, spec, h, i, skill_id, *level) {
                        continue;
                    }
                    retry.add_skill(skill_id, *level);
                }
                retry.build()?.pop().unwrap()
            }
        };

        // skillId (as sent on the wire, e.g. "100011") -> whether the input
        // spec flagged it isUnique. Consulted by the activate hook below;
        // matches race_runner.py's per-horse `unique_ids` set.
        let unique_ids: HashSet<String> = h.skills.iter()
            .filter(|(_, _, is_unique)| *is_unique)
            .map(|(id, _, _)| id.clone())
            .collect();

        let horse_index = i;
        let events = skill_events.clone();
        let kstart = kakari_start.clone();
        let pending = pending_skills.clone();
        solver.set_on_skill_activate(Box::new(move |s, skill_id, _perspective| {
            if skill_id == "kakari" {
                // Rushing (kakari) is not a real skill id -- record the
                // start time now; the deactivate hook finalizes the event
                // with the REAL elapsed duration once it ends (kakari
                // duration can be extended mid-race by an opponent's
                // EXTEND_KAKARI skill, so this must be measured at
                // deactivation, not read from a stored duration here).
                kstart.borrow_mut().insert(horse_index, s.accumulatetime.get());
                return;
            }
            let sid: i64 = match skill_id.parse() {
                Ok(v) => v,
                Err(_) => return,
            };
            // Deferred to the deactivate hook, same as kakari above: the
            // event's real duration is only known once the skill ends
            // (matches race_runner.py's pending_skills/make_activate_hook).
            pending.borrow_mut().insert(
                (horse_index, sid),
                (s.accumulatetime.get(), unique_ids.contains(skill_id)),
            );
        }));

        let events2 = skill_events.clone();
        let kstart2 = kakari_start.clone();
        let pending2 = pending_skills.clone();
        solver.set_on_skill_deactivate(Box::new(move |s, skill_id, _perspective| {
            if skill_id == "kakari" {
                let start_t = match kstart2.borrow_mut().remove(&horse_index) {
                    Some(t) => t,
                    None => return,
                };
                events2.borrow_mut().push(json::obj(vec![
                    ("horseIndex", json::num(horse_index as f64)),
                    ("skillId", Value::String("kakari".to_string())),
                    ("t", json::num(start_t)),
                    ("duration", json::num(s.accumulatetime.get() - start_t)),
                    ("isUnique", json::num(0.0)),
                ]));
                return;
            }
            let sid: i64 = match skill_id.parse() {
                Ok(v) => v,
                Err(_) => return,
            };
            let (start_t, is_unique) = match pending2.borrow_mut().remove(&(horse_index, sid)) {
                Some(v) => v,
                None => return,
            };
            events2.borrow_mut().push(json::obj(vec![
                ("horseIndex", json::num(horse_index as f64)),
                ("skillId", json::num(sid as f64)),
                ("t", json::num(start_t)),
                ("duration", json::num(s.accumulatetime.get() - start_t)),
                ("isUnique", json::num(if is_unique { 1.0 } else { 0.0 })),
            ]));
        }));

        solvers.push(solver);
    }

    // Gives every solver live awareness of the rest of the field (order/
    // rank, gaps to the horse ahead/behind) -- not part of the original TS
    // engine, which has no notion of other horses at all.
    let field = RaceField::new(solvers);

    let n = field.n;
    let mut finished: Vec<Option<f64>> = vec![None; n];
    let mut finished_step: Vec<Option<f64>> = vec![None; n];
    let mut frames: Vec<Value> = Vec::new();

    // The PLAYER's (index 0) own last-spurt start time -- the client uses a
    // dedicated race-event marker carrying this to learn "this horse index
    // is you" (skill popups, win highlighting). Tracked the same way
    // kakari/skills are, by watching solver[0]'s own state each tick
    // (matches race_runner.py's player_last_spurt_t).
    let mut player_last_spurt_t: Option<f64> = None;

    let mut t = 0.0f64;

    while finished.iter().any(|f| f.is_none()) && t < MAX_RACE_SECONDS {
        let active: Vec<usize> = (0..n).filter(|&i| finished[i].is_none()).collect();
        let prev_pos: HashMap<usize, f64> = active.iter().map(|&i| (i, field.solvers[i].borrow().pos)).collect();
        field.step_all(DT, &active);
        if player_last_spurt_t.is_none() && n > 0 && field.solvers[0].borrow().is_last_spurt {
            player_last_spurt_t = Some(t + DT);
        }
        for &i in &active {
            let pos = field.solvers[i].borrow().pos;
            if pos >= course.distance {
                let span = pos - prev_pos[&i];
                let frac = if span > 0.0 { (course.distance - prev_pos[&i]) / span } else { 0.0 };
                finished[i] = Some(t + DT * frac);
                finished_step[i] = Some(t + DT);
            }
        }
        t += DT;

        // Record every tick unconditionally -- real captured finish spreads
        // can exceed a flat "winner + grace seconds" cutoff (an 18-horse
        // field with a wide stat spread can spread 10+ seconds), and the
        // outer while loop already stops on its own once every horse has
        // finished (or MAX_RACE_SECONDS, a generous safety cap). Truncating
        // here left late finishers' frames stuck mid-track while their
        // results still reported the later time, producing a warp/speed-
        // burst right at the line (matches race_runner.py's fix).
        let horses: Vec<Value> = (0..n)
            .map(|i| {
                let s = field.solvers[i].borrow();
                let hp_val = s.hp.hp_value().map(|v| v.max(0.0)).unwrap_or(0.0);
                json::obj(vec![("pos", json::num(s.pos)), ("speed", json::num(s.current_speed)), ("hp", json::num(hp_val))])
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

    // If the player never entered her own last-spurt phase before the race
    // ended (MAX_RACE_SECONDS or an HP-exhaustion edge case) -- fall back to
    // the finish time so the identity marker still gets sent.
    if player_last_spurt_t.is_none() && n > 0 {
        player_last_spurt_t = Some(finished_step[0].unwrap_or(t));
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
        let last_spurt = if s.last_spurt_transition > 0.0 { s.last_spurt_transition } else { -1.0 };
        results.push(json::obj(vec![
            ("finishOrder", json::num(order_index[i] as f64)),
            ("finishTime", json::num(finished_step[i].unwrap())),
            ("finishTimeRaw", json::num(finished[i].unwrap())),
            ("startDelayTime", json::num(s.start_delay)),
            ("lastSpurtStartDistance", json::num(last_spurt)),
            ("runningStyle", json::num(strategy_id_for_output(&spec.horses[i].strategy_str))),
        ]));
    }

    // A skill still active when the race ends never gets an on_skill_deactivate
    // call (the solver simply stops ticking) -- flush anything still pending
    // as active-until-the-finish, matching race_runner.py.
    for ((horse_index, sid), (start_t, is_unique)) in pending_skills.borrow().iter() {
        skill_events.borrow_mut().push(json::obj(vec![
            ("horseIndex", json::num(*horse_index as f64)),
            ("skillId", json::num(*sid as f64)),
            ("t", json::num(*start_t)),
            ("duration", json::num(t - start_t)),
            ("isUnique", json::num(if *is_unique { 1.0 } else { 0.0 })),
        ]));
    }

    let skill_events_out = skill_events.borrow().clone();
    Ok(json::obj(vec![
        ("seed", json::num(spec.seed as f64)),
        ("frames", json::arr(frames)),
        ("results", json::arr(results)),
        ("skillEvents", json::arr(skill_events_out)),
        ("playerLastSpurtTime", json::num(player_last_spurt_t.unwrap_or(t))),
    ]))
}

// Frame cadence -- MUST stay identical to race_simulator.py's
// _DENSE_LEAD_FRAMES / _SPARSE_STRIDE / _downsample_frames. See the comment
// block above those constants for why real scenarios are dense through the
// start dash and sparse afterwards.
const DENSE_LEAD_FRAMES: usize = 17;
const SPARSE_STRIDE: usize = 16;

/// Thin `frames` down to exactly the set build_race_scenario would have kept.
///
/// The sim produces ~1,170 frames (1.5 MB of JSON) and Python keeps 89 of
/// them, so 92% of that blob was serialized here, pushed through a pipe and
/// parsed on the other side purely to be discarded. Doing the cut here is
/// worth ~36 ms per race on the Python side alone.
///
/// Python prepends a synthetic t=0 "gate" frame when the first simulated
/// frame starts after t=0, and it downsamples AFTER that prepend -- so the
/// index math below works in that post-prepend space and maps back, even
/// though this side never emits the gate frame itself. `gateFrameNeeded`
/// tells Python whether to add it; `framesDownsampled` tells Python the list
/// is already thinned and must not be thinned again.
fn downsample_result(result: Value) -> Value {
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

    let gate_needed = frames
        .first()
        .and_then(|f| f.get("t"))
        .and_then(Value::as_f64)
        .map_or(false, |t| t > 0.0);
    let off = if gate_needed { 1 } else { 0 };
    let eff_len = frames.len() + off;

    let mut keep = vec![false; eff_len];
    if eff_len <= 2 {
        for k in keep.iter_mut() {
            *k = true;
        }
    } else {
        for k in keep.iter_mut().take(DENSE_LEAD_FRAMES.min(eff_len)) {
            *k = true;
        }
        let mut i = 0;
        while i < eff_len {
            keep[i] = true;
            i += SPARSE_STRIDE;
        }
        keep[eff_len - 1] = true;
    }

    // build_race_scenario needs each horse's position/speed at the last frame
    // at or before its OWN finishTimeRaw, and it needs that at full
    // resolution -- re-deriving it from thinned frames would move the finish
    // sample to a nearby kept tick and change the encoded blob. Compute it
    // here, while the full list still exists, and send it alongside.
    let mut finish_frames: Vec<Value> = Vec::new();
    if let Some(results) = map.get("results").and_then(Value::as_array) {
        for (hi, r) in results.iter().enumerate() {
            let finish_t = r
                .get("finishTimeRaw")
                .and_then(Value::as_f64)
                .unwrap_or(f64::INFINITY);
            let mut best: Option<&Value> = None;
            for f in frames.iter() {
                let t = f.get("t").and_then(Value::as_f64).unwrap_or(0.0);
                if t <= finish_t {
                    best = Some(f);
                } else {
                    break;
                }
            }
            let entry = match best
                .and_then(|f| f.get("horses"))
                .and_then(Value::as_array)
                .and_then(|hs| hs.get(hi))
            {
                Some(h) => json::obj(vec![
                    ("pos", h.get("pos").cloned().unwrap_or(Value::Null)),
                    ("speed", h.get("speed").cloned().unwrap_or(Value::Null)),
                    (
                        "t",
                        best.and_then(|f| f.get("t")).cloned().unwrap_or(Value::Null),
                    ),
                ]),
                None => Value::Null,
            };
            finish_frames.push(entry);
        }
    }

    let raw_frame_count = frames.len();
    let kept: Vec<Value> = frames
        .into_iter()
        .enumerate()
        .filter(|(i, _)| keep[i + off])
        .map(|(_, f)| f)
        .collect();

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
    let skills = load_skill_table(SKILL_DATA_JSON)?;
    race_solver_builder::init_skills(skills);

    let conditions: &'static HashMap<String, activation_conditions::Condition> = Box::leak(Box::new(activation_conditions::build_conditions()));

    // Persistent-worker loop: one JSON spec per line in, one JSON result per
    // line out, flushed each iteration.
    //
    // The two tables loaded above are ~600 KB of JSON and cost ~21 ms to parse
    // -- that used to be paid on EVERY race, because each race was a fresh
    // process. Paying it once per process instead is the entire point. The
    // per-request work below is stateless (the RNG is seeded from the spec),
    // so races cannot leak into each other across iterations.
    //
    // A caller that writes one spec and closes stdin still behaves exactly as
    // before, so this stays compatible with the old one-shot invocation.
    let stdin = std::io::stdin();
    let mut reader = stdin.lock();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut line = String::new();
    loop {
        line.clear();
        if reader.read_line(&mut line).map_err(|e| e.to_string())? == 0 {
            break; // EOF -- parent closed the pipe
        }
        if line.trim().is_empty() {
            continue;
        }
        // A bad spec must not take the worker down with it: report the error
        // as a JSON reply and keep serving. The caller treats an "error" key
        // as a failed run and falls back to the Python engine for that race.
        let outcome = (|| -> Result<Value, String> {
            let spec_json = json::parse(&line)?;
            let spec = parse_race_spec(&spec_json)?;
            Ok(downsample_result(run_race(&spec, conditions)?))
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
