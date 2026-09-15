//! Port of course_data.py.

use std::collections::HashMap;
use std::sync::OnceLock;

pub const SURFACE_TURF: i32 = 1;
pub const SURFACE_DIRT: i32 = 2;

#[derive(Clone, Copy, Debug)]
pub struct Corner {
    pub start: f64,
    pub length: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Straight {
    pub start: f64,
    pub end: f64,
    pub front_type: i32,
}

#[derive(Clone, Copy, Debug)]
pub struct Slope {
    pub start: f64,
    pub length: f64,
    pub slope: f64,
}

#[derive(Clone, Debug)]
pub struct CourseData {
    pub race_track_id: i32,
    pub distance: f64,
    pub distance_type: i32,
    pub surface: i32,
    pub turn: i32,
    pub course_set_status: Vec<i32>,
    pub corners: Vec<Corner>,
    pub straights: Vec<Straight>,
    pub slopes: Vec<Slope>,
    pub max_lane: f64,
}

pub fn is_sorted_by_start_f(starts: impl Iterator<Item = f64>) -> bool {
    let mut last = -1.0;
    for start in starts {
        if start <= last {
            return false;
        }
        last = start;
    }
    true
}

pub fn phase_start(distance: f64, phase: i32) -> f64 {
    match phase {
        0 => 0.0,
        1 => distance * 1.0 / 6.0,
        2 => distance * 2.0 / 3.0,
        3 => distance * 5.0 / 6.0,
        _ => panic!("invalid phase {phase}"),
    }
}

pub fn phase_end(distance: f64, phase: i32) -> f64 {
    match phase {
        0 => distance * 1.0 / 6.0,
        1 => distance * 2.0 / 3.0,
        2 => distance * 5.0 / 6.0,
        3 => distance,
        _ => panic!("invalid phase {phase}"),
    }
}

/// `speed`/`stamina`/`power`/`guts`/`wisdom` accessed positionally exactly
/// like the Python port's `[0, stats.speed, stats.stamina, ...]` array
/// (index 0 unused, ThresholdStat is 1-indexed in course_set_status).
pub fn course_speed_modifier(course: &CourseData, speed: f64, stamina: f64, power: f64, guts: f64, wisdom: f64) -> f64 {
    let statvalues = [0.0, speed.min(901.0), stamina.min(901.0), power.min(901.0), guts.min(901.0), wisdom.min(901.0)];
    let total: f64 = course
        .course_set_status
        .iter()
        .map(|&stat| (1.0 + (statvalues[stat as usize] / 300.01).floor()) * 0.05)
        .sum();
    1.0 + total / (course.course_set_status.len().max(1) as f64)
}

// -- data loading -------------------------------------------------------
// Populated by main.rs at startup from data/course_data.json (parsed with
// serde_json), via `init_courses`. Kept separate from this module's pure
// logic so the latter needed no JSON dependency while the linker was
// unavailable during development.

static COURSES: OnceLock<HashMap<i32, CourseData>> = OnceLock::new();

pub fn init_courses(courses: HashMap<i32, CourseData>) {
    COURSES.set(courses).ok();
}

pub fn get_course(course_id: i32) -> &'static CourseData {
    COURSES
        .get()
        .expect("init_courses must run before get_course")
        .get(&course_id)
        .unwrap_or_else(|| panic!("unknown course id {course_id}"))
}
