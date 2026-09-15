//! Port of horse_types.py.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Strategy {
    Nige = 1,
    Senkou = 2,
    Sasi = 3,
    Oikomi = 4,
    Oonige = 5,
}

impl Strategy {
    pub fn from_i32(v: i32) -> Result<Strategy, String> {
        match v {
            1 => Ok(Strategy::Nige),
            2 => Ok(Strategy::Senkou),
            3 => Ok(Strategy::Sasi),
            4 => Ok(Strategy::Oikomi),
            5 => Ok(Strategy::Oonige),
            _ => Err(format!("Invalid running strategy int {v}.")),
        }
    }

    pub fn parse_str(s: &str) -> Result<Strategy, String> {
        match s.to_uppercase().as_str() {
            "NIGE" => Ok(Strategy::Nige),
            "SENKOU" => Ok(Strategy::Senkou),
            "SASI" | "SASHI" => Ok(Strategy::Sasi),
            "OIKOMI" => Ok(Strategy::Oikomi),
            "OONIGE" => Ok(Strategy::Oonige),
            _ => Err("Invalid running strategy.".to_string()),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Aptitude {
    S = 0,
    A = 1,
    B = 2,
    C = 3,
    D = 4,
    E = 5,
    F = 6,
    G = 7,
}

impl Aptitude {
    pub fn from_i32(v: i32) -> Result<Aptitude, String> {
        match v {
            0 => Ok(Aptitude::S),
            1 => Ok(Aptitude::A),
            2 => Ok(Aptitude::B),
            3 => Ok(Aptitude::C),
            4 => Ok(Aptitude::D),
            5 => Ok(Aptitude::E),
            6 => Ok(Aptitude::F),
            7 => Ok(Aptitude::G),
            _ => Err(format!("Invalid aptitude int {v}.")),
        }
    }

    pub fn parse_str(s: &str, kind: &str) -> Result<Aptitude, String> {
        match s.to_uppercase().as_str() {
            "S" => Ok(Aptitude::S),
            "A" => Ok(Aptitude::A),
            "B" => Ok(Aptitude::B),
            "C" => Ok(Aptitude::C),
            "D" => Ok(Aptitude::D),
            "E" => Ok(Aptitude::E),
            "F" => Ok(Aptitude::F),
            "G" => Ok(Aptitude::G),
            _ => Err(format!("Invalid {kind} aptitude.")),
        }
    }
}

/// Mutable: RaceSolver clones this per-solver and green (stat-boost) skills
/// mutate it in place mid-race, exactly like the Python/TS `horse` field.
#[derive(Clone, Debug)]
pub struct HorseParameters {
    pub speed: f64,
    pub stamina: f64,
    pub power: f64,
    pub guts: f64,
    pub wisdom: f64,
    pub strategy: Strategy,
    pub distance_aptitude: Aptitude,
    pub surface_aptitude: Aptitude,
    pub strategy_aptitude: Aptitude,
    pub raw_stamina: f64,
}

pub fn strategy_matches(s1: Strategy, s2: Strategy) -> bool {
    s1 == s2
        || (s1 == Strategy::Nige && s2 == Strategy::Oonige)
        || (s1 == Strategy::Oonige && s2 == Strategy::Nige)
}
