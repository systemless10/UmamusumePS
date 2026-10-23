use crate::{data::Tree, math, rng::RaceRandom};

#[derive(Clone, Copy)]
pub struct Runner {
    pub distance: f32,
    pub order: usize,
    pub style: i32,
    pub ex: bool,
    pub phase: usize,
    pub wiz: f32,
    pub temptation: i32,
    pub speed_skill: bool,
}

#[derive(Clone, Default)]
pub struct Keep {
    pub mode: i32,
    pub count: i32,
    pub multiply: f32,
    pub checked: i32,
    pub wanted: i32,
    pub cooldown: f32,
    interval: f32,
    pub end_distance: f32,
    pub target_gap: f32,
    was_tempted: bool,
}

fn offset(p: &Tree<'_>, distance: i32, style: i32) -> (f32, f32) {
    let scale = ((distance - p.i("PositionKeepPaseBaseDistance")) as f32
        * p.f("PositionKeepPaseBaseDistanceCoef") + 1.0).max(1.0);
    let name = match style { 2 => "Senko", 3 => "Sashi", 4 => "Oikomi", _ => return (0.0, 0.0) };
    let min = p.f(&format!("PositionKeepPaseUpDownDistanceDiffMin{name}"));
    let max = p.f(&format!("PositionKeepPaseUpDownDistanceDiffMax{name}"));
    (if style == 2 { min } else { scale * min }, scale * max)
}

impl Keep {
    pub fn rng_probe_state(&self) -> [f32; 5] {
        [self.checked as f32, self.cooldown, self.interval, self.end_distance, self.target_gap]
    }

    pub fn clear(&mut self, p: &Tree<'_>) {
        self.mode = 0;
        self.multiply = 1.0;
        self.cooldown = p.i("PositionKeepCoolDownSec") as f32;
    }

    pub fn in_section(p: &Tree<'_>, distance: i32, sections: i32, position: f32) -> bool {
        let section = (distance / sections) as f32;
        position >= (p.i("PositionKeepStartSection") - 1) as f32 * section
            && position < p.i("PositionKeepEndSection") as f32 * section
    }

    pub fn update(&mut self, p: &Tree<'_>, distance: i32, sections: i32,
                  runners: &[Runner], owner: usize, pacemaker: usize,
                  random: &mut RaceRandom, dt: f32, pacemaker_ready: bool) {
        let h = runners[owner];
        let pace = runners[pacemaker];
        let tempted = h.temptation != 0;
        if self.was_tempted != tempted {
            self.clear(p);
            self.cooldown = 0.0;
            self.interval = 0.0;
            self.was_tempted = tempted;
        }
        let style = if tempted {
            match h.temptation { 1 => 3, 2 => 2, _ => 1 }
        } else { h.style };
        let backward_style_ahead = runners.iter().any(|other| other.order < h.order && other.style > h.style);
        let behind = runners.iter().find(|other| other.order == h.order + 1);
        let ex_suffix = if h.ex { "Oonige" } else { "" };
        let speed_up_end = if h.ex {
            p.f("PositionKeepSpeedUpEndDistanceDiffOonige")
        } else if !(pacemaker == owner && h.style != 1)
            && runners.iter().filter(|other| other.style == 1 && !other.ex).count() == 1 {
            p.f("PositionKeepSpeedUpEndDistanceDiffOne")
        } else { p.f("PositionKeepSpeedUpEndDistanceDiff") };
        let force_rush = tempted && std::env::var("RX_TEMPT_FORCEKEEP").map_or(true, |v| v != "0");
        let far_behind = behind.map_or(!force_rush, |other| h.distance - other.distance >= speed_up_end);
        let gap = pace.distance - h.distance;
        self.checked = -1;
        self.wanted = -1;
        if self.mode == 0 && Self::in_section(p, distance, sections, h.distance) {
            if self.cooldown > 0.0 { self.cooldown -= dt; }
            if self.cooldown > 0.0 { return; }
            if self.interval > 0.0 { self.interval -= dt; }
            if self.interval > 0.0 { return; }
            self.interval = p.i("PositionKeepCheckIntervalSec") as f32;
            self.checked = 0;
            let mode = if style == 1 {
                if tempted { 1 } else if backward_style_ahead { 5 }
                else if h.order == 0 { 1 } else { 2 }
            } else if !pacemaker_ready { 0 }
            else if pace.order < h.order && h.style < pace.style { 5 }
            else {
                let (min, max) = offset(p, distance, style);
                let max = if std::env::var("RX_PK_MAX_FLOOR").is_ok_and(|v| v == "1")
                    || style == 2 && std::env::var("RX_PK_MAX_FLOOR_SENKO").is_ok_and(|v| v == "1") {
                    max.floor()
                } else { max };
                if gap < min { if pacemaker == owner { 0 } else { 4 } }
                else if gap > max { 3 } else { 0 }
            };
            self.wanted = mode;
            if mode == 0 || mode == 1 && far_behind || mode == 4 && h.speed_skill { return; }
            if !tempted && matches!(mode, 1 | 2 | 3) {
                let key = match mode {
                    1 => format!("PositionKeepSpeedUpStartPerVal1{ex_suffix}"),
                    2 => format!("PositionKeepOvertakeStartPerVal1{ex_suffix}"),
                    _ => "PositionKeepPaseUpStartPerVal1".into(),
                };
                let chance = math::log10(h.wiz * 0.1) * p.f(&key);
                if chance <= random.float_max(100.0, &|| format!("keep{owner}:lot{mode}")) { return; }
            }
            self.mode = mode;
            self.checked = mode;
            self.count += 1;
            let continue_sections = if h.ex {
                p.arr("PositionKeepContinueSectionExArray")[0].as_i64().unwrap() as i32
            } else { p.i("PositionKeepContinueSection") };
            self.end_distance = h.distance + (distance / sections * continue_sections) as f32;
            self.multiply = match mode {
                1 => p.f(&format!("PositionKeepSpeedUpBaseTargetSpeedMultiply{ex_suffix}")),
                2 => p.f(&format!("PositionKeepOvertakeBaseTargetSpeedMultiply{ex_suffix}")),
                3 => p.f("PositionKeepPaseUpBaseTargetSpeedMultiply"),
                4 => p.f(if h.phase == 1 { "PositionKeepPaseDownBaseTargetSpeedMultiplyPhaseMiddle" }
                         else { "PositionKeepPaseDownBaseTargetSpeedMultiply" }),
                5 => p.f("PositionKeepPaseUpExBaseTargetSpeedMultiply"),
                _ => 1.0,
            };
            if matches!(mode, 3 | 4) {
                let (min, mut max) = offset(p, distance, style);
                if mode == 4 && h.phase == 1 {
                    max = (max - min) * p.f("PositionKeepPaseUpDownDistanceDiffMaxCoef").clamp(0.0, 1.0) + min;
                }
                self.target_gap = random.float_range(min, max, &|| format!("keep{owner}:gap{mode}"));
            }
        }
        let end = h.distance >= self.end_distance || match self.mode {
            1 => far_behind,
            2 if h.order == 0 => behind.is_none_or(|other| h.distance - other.distance
                >= p.f(&format!("PositionKeepOvertakeEndDistanceDiff{ex_suffix}"))),
            3 => gap <= self.target_gap,
            4 => pacemaker == owner || h.speed_skill || gap >= self.target_gap,
            5 => !backward_style_ahead,
            _ => false,
        };
        if self.mode != 0 && end { self.clear(p); }
    }
}
