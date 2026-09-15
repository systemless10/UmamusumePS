//! Port of race_parameters.py.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GroundCondition {
    Good = 1,
    Yielding = 2,
    Soft = 3,
    Heavy = 4,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Weather {
    Sunny = 1,
    Cloudy = 2,
    Rainy = 3,
    Snowy = 4,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Season {
    Spring = 1,
    Summer = 2,
    Autumn = 3,
    Winter = 4,
    Sakura = 5,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RaceTime {
    NoTime = 0,
    Morning = 1,
    Midday = 2,
    Evening = 3,
    Night = 4,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Grade {
    G1 = 100,
    G2 = 200,
    G3 = 300,
    Op = 400,
    PreOp = 700,
    Maiden = 800,
    Debut = 900,
    Daily = 999,
}

pub fn parse_ground_condition(s: &str) -> Result<GroundCondition, String> {
    match s.to_uppercase().as_str() {
        "GOOD" => Ok(GroundCondition::Good),
        "YIELDING" => Ok(GroundCondition::Yielding),
        "SOFT" => Ok(GroundCondition::Soft),
        "HEAVY" => Ok(GroundCondition::Heavy),
        other => Err(format!("Invalid ground condition {other}.")),
    }
}

pub fn parse_weather(s: &str) -> Result<Weather, String> {
    match s.to_uppercase().as_str() {
        "SUNNY" => Ok(Weather::Sunny),
        "CLOUDY" => Ok(Weather::Cloudy),
        "RAINY" => Ok(Weather::Rainy),
        "SNOWY" => Ok(Weather::Snowy),
        other => Err(format!("Invalid weather {other}.")),
    }
}

pub fn parse_season(s: &str) -> Result<Season, String> {
    match s.to_uppercase().as_str() {
        "SPRING" => Ok(Season::Spring),
        "SUMMER" => Ok(Season::Summer),
        "AUTUMN" => Ok(Season::Autumn),
        "WINTER" => Ok(Season::Winter),
        "SAKURA" => Ok(Season::Sakura),
        other => Err(format!("Invalid season {other}.")),
    }
}

pub fn parse_time(s: &str) -> Result<RaceTime, String> {
    match s.to_uppercase().as_str() {
        "NONE" | "NOTIME" | "NO_TIME" => Ok(RaceTime::NoTime),
        "MORNING" => Ok(RaceTime::Morning),
        "MIDDAY" => Ok(RaceTime::Midday),
        "EVENING" => Ok(RaceTime::Evening),
        "NIGHT" => Ok(RaceTime::Night),
        other => Err(format!("Invalid time {other}.")),
    }
}

pub fn parse_grade(s: &str) -> Result<Grade, String> {
    match s.to_uppercase().as_str() {
        "PRE-OP" | "PREOP" | "PRE_OP" => Ok(Grade::PreOp),
        "G1" => Ok(Grade::G1),
        "G2" => Ok(Grade::G2),
        "G3" => Ok(Grade::G3),
        "OP" => Ok(Grade::Op),
        "MAIDEN" => Ok(Grade::Maiden),
        "DEBUT" => Ok(Grade::Debut),
        "DAILY" => Ok(Grade::Daily),
        other => Err(format!("Invalid grade {other}.")),
    }
}

/// Mutable "partial" race parameters, mirroring RaceSolverBuilder's
/// `PartialRaceParameters` (everything except `skill_id`, filled in
/// per-skill when building skill data -- see `RaceParametersWithSkillId`).
#[derive(Clone, Debug)]
pub struct RaceParameters {
    pub mood: i32,
    pub ground_condition: GroundCondition,
    pub weather: Weather,
    pub season: Season,
    pub time: RaceTime,
    pub grade: Grade,
    pub popularity: i32,
    pub order_range: Option<(i32, i32)>,
    pub num_umas: Option<i32>,
}

impl Default for RaceParameters {
    fn default() -> Self {
        RaceParameters {
            mood: 2,
            ground_condition: GroundCondition::Good,
            weather: Weather::Sunny,
            season: Season::Spring,
            time: RaceTime::Midday,
            grade: Grade::G1,
            popularity: 1,
            order_range: None,
            num_umas: None,
        }
    }
}

/// The `extra` object passed to condition filters: RaceParameters plus the
/// currently-being-built skill's own id (self-referential conditions like
/// is_activate_other_skill_detail).
#[derive(Clone, Debug)]
pub struct RaceParametersWithSkillId {
    pub params: RaceParameters,
    pub skill_id: String,
}
