
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;

pub struct Tree<'a>(pub &'a Value);

pub fn f32_of(v: &Value) -> f32 {
    match v {
        Value::Object(m) if m.contains_key("float32_hex") => {
            let h = m["float32_hex"].as_str().unwrap();
            f32::from_bits(u32::from_str_radix(h.trim_start_matches("0x"), 16).unwrap())
        }
        Value::Number(n) => n.as_f64().unwrap() as f32,
        _ => panic!("not a float: {v}"),
    }
}

impl<'a> Tree<'a> {
    pub fn get(&self, path: &str) -> &'a Value {
        let mut v = self.0;
        for part in path.split('.') {
            if let Some((name, idx)) = part.split_once('[') {
                v = &v[name];
                let i: usize = idx.trim_end_matches(']').parse().unwrap();
                v = &v[i];
            } else {
                v = &v[part];
            }
            if v.is_null() {
                panic!("missing param {path}");
            }
        }
        v
    }
    pub fn f(&self, path: &str) -> f32 {
        thread_local!(static CACHE: std::cell::RefCell<HashMap<usize, HashMap<String, f32>>> = Default::default());
        let root = self.0 as *const Value as usize;
        if let Some(v) = CACHE.with(|c| c.borrow().get(&root).and_then(|m| m.get(path)).copied()) { return v; }
        let v = f32_of(self.get(path));
        CACHE.with(|c| c.borrow_mut().entry(root).or_default().insert(path.to_string(), v));
        v
    }
    pub fn i(&self, path: &str) -> i32 {
        self.get(path).as_i64().unwrap_or_else(|| panic!("not int: {path}")) as i32
    }
    pub fn arr(&self, path: &str) -> &'a Vec<Value> {
        self.get(path).as_array().unwrap_or_else(|| panic!("not array: {path}"))
    }
}

pub struct Assets {
    pub root: Value,
}

impl Assets {
    pub fn load(path: &str) -> Result<Self, String> {
        let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        let root: Value = serde_json::from_slice(&bytes).map_err(|e| format!("{path}: {e}"))?;
        if root["source_region"] != "Global" && root["source_region"] != "JP" {
            return Err(format!("{path}: race assets require source_region Global or JP"));
        }
        Ok(Assets { root })
    }
    pub fn params(&self) -> Tree<'_> {
        Tree(&self.root["race_param_define"]["objects"][0]["fields"])
    }
    pub fn course_set(&self, id: i32) -> &Value {
        &self.root["course_sets"][id.to_string()]
    }
    pub fn lane_asset(&self, name: &str) -> &Value {
        &self.root["lane_assets"][name]["objects"][0]["fields"]
    }
}

pub struct Master {
    pub tables: HashMap<String, Vec<HashMap<String, Value>>>,
}

impl Master {
    pub fn load(path: &str) -> Result<Self, String> {
        let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        let tables = serde_json::from_slice(&bytes).map_err(|e| format!("{path}: {e}"))?;
        Ok(Master { tables })
    }
    pub fn rows(&self, t: &str) -> &Vec<HashMap<String, Value>> {
        self.tables.get(t).unwrap_or_else(|| panic!("missing table {t}"))
    }
    pub fn find(&self, t: &str, key: &str, val: i64) -> Option<&HashMap<String, Value>> {
        self.rows(t).iter().find(|r| r.get(key).and_then(|v| v.as_i64()) == Some(val))
    }
}

pub fn ival(r: &HashMap<String, Value>, k: &str) -> i64 {
    r.get(k).and_then(|v| v.as_i64()).unwrap_or_else(|| panic!("missing int column {k}"))
}

pub fn sval<'a>(r: &'a HashMap<String, Value>, k: &str) -> &'a str {
    r.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

#[derive(Deserialize, Clone, Debug)]
pub struct SkillRef {
    pub skill_id: i32,
    pub level: i32,
}

#[derive(Deserialize, Clone, Debug)]
pub struct HorseIn {
    pub frame_order: i32,
    #[serde(default, deserialize_with = "viewer_marker")]
    pub viewer_id: i64,
    #[serde(default)]
    pub chara_id: i32,
    #[serde(default, deserialize_with = "null_i32")]
    pub card_id: i32,
    #[serde(default)]
    pub mob_id: i32,
    #[serde(default)]
    pub npc_type: i32,
    #[serde(default)]
    pub skill_array: Vec<SkillRef>,
    pub speed: i32,
    pub stamina: i32,
    pub pow: i32,
    pub guts: i32,
    pub wiz: i32,
    pub running_style: i32,
    pub motivation: i32,
    pub proper_distance_short: i32,
    pub proper_distance_mile: i32,
    pub proper_distance_middle: i32,
    pub proper_distance_long: i32,
    pub proper_running_style_nige: i32,
    pub proper_running_style_senko: i32,
    pub proper_running_style_sashi: i32,
    pub proper_running_style_oikomi: i32,
    pub proper_ground_turf: i32,
    pub proper_ground_dirt: i32,
    #[serde(default)]
    pub popularity: i32,
    #[serde(default)]
    pub team_id: i32,
    #[serde(default)]
    pub team_member_id: i32,
    #[serde(default)]
    pub team_rank: i32,
    #[serde(default)]
    pub single_mode_win_count: i32,
    #[serde(default)]
    pub fan_count: i64,
    #[serde(default)]
    pub item_id_array: Vec<i32>,
    #[serde(default)]
    pub final_grade: i32,
    #[serde(default)]
    pub rarity: i32,
    #[serde(default)]
    pub talent_level: i32,
    #[serde(default, deserialize_with = "null_vec")]
    pub scenario_data_array: Vec<ScenarioData>,
}

#[derive(Deserialize, Clone, Debug)]
pub struct ScenarioData {
    #[serde(rename = "type")]
    pub kind: i32,
    pub value: i64,
}

fn null_vec<'de, D: serde::Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<Vec<T>, D::Error> {
    Ok(Option::<Vec<T>>::deserialize(d)?.unwrap_or_default())
}

fn null_i32<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i32, D::Error> {
    Ok(Option::<i32>::deserialize(d)?.unwrap_or(0))
}

fn viewer_marker<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    let v = Value::deserialize(d)?;
    if let Some(id) = v.as_i64() { return Ok(id); }
    if v.as_str().is_some_and(|s| s.starts_with("<REDACTED:viewer_id:") && s.ends_with('>')) {
        return Ok(1);
    }
    Err(serde::de::Error::custom("viewer_id requires an integer or a recognized redacted player marker"))
}

#[derive(Deserialize, Clone, Debug)]
pub struct RaceIn {
    pub replay_sha: String,
    pub logged_seed: i64,
    pub seed: i64,
    pub seed_mode: String,
    #[serde(default)]
    pub input_sha256: Option<String>,
    pub race_type: i32,
    pub race_instance_id: i32,
    pub race_id: i32,
    pub course_set: i32,
    pub season: i32,
    pub weather: i32,
    pub ground_condition: i32,
    pub horses: Vec<HorseIn>,
    #[serde(default)]
    pub region: Option<String>,
}

impl RaceIn {
    pub fn load(path: &str) -> Result<Self, String> {
        let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        serde_json::from_slice(&bytes).map_err(|e| format!("{path}: {e}"))
    }
}
