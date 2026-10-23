
pub const TICK: f32 = 0.0666;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Style { Other, Nige, Oonige }

#[derive(Clone, Copy, Debug)]
pub struct Runner {
    pub style: Style,
    pub distance: f32,
    pub lane: f32,
    pub speed: f32,
    pub hp: f32,
    pub max_hp: f32,
    pub guts: f32,
    pub strategy_proficiency: f32,
    pub rank: usize,
    pub final_straight: bool,
    pub tempted: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct Params {
    pub start_distance: f32, pub check_end_section: u32, pub end_section: u32,
    pub top_distance: f32, pub top_lane: f32,
    pub top_exit_distance: f32, pub top_exit_lane: f32,
    pub top_time: [f32; 3], pub top_speed: [f32; 3],
    pub fight_distance: f32, pub fight_lane: f32, pub fight_time: f32,
    pub fight_speed_gap: f32, pub fight_order_percent: usize,
    pub fight_exit_distance: f32, pub fight_entry_hp_percent: f32,
    pub fight_exit_hp_percent: f32,
    pub fight_exit_inclusive: bool,
    pub fight_select_before_history: bool,
    pub fight_speed: [f32; 3], pub fight_accel: [f32; 3],
    pub top_hp: [f32; 4],
    pub top_requires_lead: u8,
    pub top_cohort: bool,
    pub top_end_exit: bool,
}

impl Default for Params {
    fn default() -> Self {
        Self {
            start_distance: 150.0, check_end_section: 6, end_section: 9,
            top_distance: 3.75, top_lane: 0.165,
            top_exit_distance: 5.0, top_exit_lane: 0.416,
            top_time: [700.0, 0.5, 0.012], top_speed: [500.0, 0.6, 0.0001],
            fight_distance: 3.0, fight_lane: 0.25, fight_time: 2.0,
            fight_speed_gap: 0.6, fight_order_percent: 50,
            fight_exit_distance: 5.0, fight_entry_hp_percent: 15.0,
            fight_exit_hp_percent: 5.0, fight_exit_inclusive: true, fight_select_before_history: true,
            fight_speed: [200.0, 0.708, 0.0001],
            fight_accel: [160.0, 0.59, 0.0001], top_hp: [1.4, 3.6, 3.5, 7.7],
            top_requires_lead: 1, top_cohort: false, top_end_exit: false,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Mode {
    pub top: bool, pub top_remaining: f32, pub top_group: Option<usize>,
    pub top_distance_exit: bool,
    pub fight: bool, pub fight_count: u32,
    pub fight_group: Option<usize>,
}

#[derive(Clone, Debug)]
pub struct State {
    pub modes: Vec<Mode>,
    pub top_fired: [bool; 2],
    pub near: Vec<Vec<Option<f32>>>,
    pub enter_seq: Vec<usize>,
}
impl State {
    pub fn new(count: usize) -> Self {
        Self { modes: vec![Mode::default(); count], top_fired: [false; 2],
            near: vec![vec![None; count]; count], enter_seq: vec![] }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Change { TopEnter(usize), TopExit(usize), FightEnter(usize), FightExit(usize) }

fn formula(guts: f32, c: [f32; 3]) -> f32 { (c[0] * guts.max(0.0)).powf(c[1]) * c[2] }
pub fn top_duration(r: &Runner, p: &Params) -> f32 {
    formula(r.guts, p.top_time) * r.strategy_proficiency
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Effects {
    pub target_speed_add: f32, pub acceleration_add: f32,
    pub hp_base_rate: Option<f32>,
}
pub fn effects(r: &Runner, m: &Mode, p: &Params) -> Effects {
    let hp_base_rate = if m.top {
        Some(p.top_hp[match (r.style, r.tempted) {
            (Style::Oonige, false) => 2, (Style::Oonige, true) => 3,
            (_, false) => 0, (_, true) => 1,
        }])
    } else { None };
    Effects {
        target_speed_add: if m.top { formula(r.guts, p.top_speed) } else { 0.0 }
            + if m.fight { formula(r.guts, p.fight_speed) } else { 0.0 },
        acceleration_add: if m.fight { formula(r.guts, p.fight_accel) } else { 0.0 },
        hp_base_rate,
    }
}

pub fn step(old: &State, runners: &[Runner], course_distance: f32, dt: f32,
            p: &Params) -> (State, Vec<Change>) {
    let n = runners.len();
    assert_eq!(old.modes.len(), n);
    assert!(course_distance > 0.0 && dt >= 0.0 && dt.is_finite());
    let mut next = old.clone();
    next.enter_seq.clear();
    let mut changes = Vec::new();
    let one = |n: &str| std::env::var(n).is_ok_and(|v| v == "1");
    let end = course_distance / 24.0 * (p.end_section - std::env::var("RX_CTEND1").map_or(true, |v| v != "0") as u32) as f32;
    for i in 0..n {
        let r = &runners[i]; let m = &old.modes[i];
        if m.top {
            next.modes[i].top_remaining -= dt;
            if next.modes[i].top_remaining <= 0.0 || r.distance >= end {
                next.modes[i].top = false;
                if p.top_end_exit && r.distance >= end { next.modes[i].top_distance_exit = true; }
            } else {
                let peers: Vec<usize> = (0..n).filter(|&j| j != i &&
                    (p.top_cohort || old.modes[j].top) && old.modes[j].top_group == m.top_group).collect();
                if !peers.is_empty() && peers.iter().all(|&j|
                    (if std::env::var("RX_CTABS").map_or(true, |v| v != "0") {(runners[j].distance - r.distance).abs()} else {runners[j].distance - r.distance}) >= p.top_exit_distance ||
                    (runners[j].lane - r.lane).abs() >= p.top_exit_lane) {
                    next.modes[i].top = false;
                    next.modes[i].top_distance_exit = true;
                }
            }
        }
        let group = std::env::var("RX_CFGROUP").map_or(true, |v| v != "0");
        if m.fight && (r.hp < r.max_hp * p.fight_exit_hp_percent / 100.0 ||
            !(0..n).any(|j| j != i && old.modes[j].fight_count > 0 &&
                (!group || old.modes[j].fight_group == m.fight_group) &&
                (if p.fight_exit_inclusive {
                    (runners[j].distance - r.distance).abs() <= p.fight_exit_distance
                } else {
                    (runners[j].distance - r.distance).abs() < p.fight_exit_distance
                }))) {
            next.modes[i].fight = false;
        }
        for j in 0..n {
            let q = &runners[j];
            let close = i != j && (r.final_straight || q.final_straight) &&
                (r.distance - q.distance).abs() <= p.fight_distance &&
                (r.lane - q.lane).abs() <= p.fight_lane;
            next.near[i][j] = if close {
                Some(old.near[i][j].map_or(0.0, |t| t + dt))
            } else { None };
        }
    }
    for group in 0..2 {
        let members: Vec<usize> = (0..n).filter(|&i| old.modes[i].top_group == Some(group)).collect();
        let live: Vec<usize> = members.iter().copied().filter(|&i| next.modes[i].top).collect();
        if live.len() == 1 && members.len() > 1 && members.iter().all(|&i|
            i == live[0] || next.modes[i].top_distance_exit) {
            next.modes[live[0]].top = false;
            next.modes[live[0]].top_distance_exit = true;
        }
    }
    if runners.iter().any(|r| r.distance >= p.start_distance) {
        for (group, style) in [Style::Nige, Style::Oonige].into_iter().enumerate() {
            if next.top_fired[group] { continue; }
            let leader = if std::env::var("RX_CTRANKLEAD").map_or(true, |v| v != "0") {
                (0..n).filter(|&i| runners[i].style == style).min_by_key(|&i| (runners[i].rank, i))
            } else {
                (0..n).filter(|&i| runners[i].style == style)
                    .max_by(|&a, &b| runners[a].distance.total_cmp(&runners[b].distance)
                        .then_with(|| b.cmp(&a)))
            };
            if let Some(lead) = leader {
                let leads = match p.top_requires_lead {
                    1 => runners[lead].rank == 1,
                    2 => runners.iter().all(|q| q.distance <= runners[lead].distance),
                    _ => true,
                };
                if !leads { continue; }
                let members: Vec<usize> = (0..n).filter(|&i| {
                    let r = &runners[i]; let q = &runners[lead];
                    r.style == style && q.distance - r.distance < p.top_distance &&
                        (q.lane - r.lane).abs() <= p.top_lane
                }).collect();
                if members.len() > 1 && members.iter().any(|&i| runners[i].distance <
                    course_distance / 24.0 * (p.check_end_section - one("RX_CTCHK1") as u32) as f32) {
                    next.top_fired[group] = true;
                    for i in members {
                        next.modes[i].top = true;
                        next.modes[i].top_group = Some(group);
                        next.modes[i].top_remaining = top_duration(&runners[i], p) - dt;
                    }
                }
            }
        }
    }
    let top_rank = (n * p.fight_order_percent).div_ceil(100);
    let model: Vec<char> = std::env::var("RX_CFMODEL").unwrap_or_else(|_| "nx-".into()).chars().collect();
    let in_rank = |j: usize| runners[j].rank <= top_rank;
    let hp_ok = |i: usize| runners[i].hp >= runners[i].max_hp * p.fight_entry_hp_percent / 100.0;
    let target_ok: Vec<bool> = (0..n).map(|j| hp_ok(j) &&
        (old.modes[j].fight || old.modes[j].fight_count == 0) &&
        (!old.modes[j].fight || next.modes[j].fight)).collect();
    let can_enter = |i: usize| old.modes[i].fight_count == 0 && hp_ok(i);
    let rank_ok = |i: usize, j: usize| match model[1] {'t' => in_rank(j), 'e' => in_rank(i), 'b' => in_rank(i) && in_rank(j), _ => in_rank(i) || in_rank(j)};
    let candidate = |i: usize, j: usize| i != j && can_enter(i) && hp_ok(j) && rank_ok(i, j) &&
        next.near[i][j].is_some_and(|t| t >= p.fight_time) &&
        (runners[i].speed - runners[j].speed).abs() <= p.fight_speed_gap;
    let q = |i: usize, j: usize| candidate(i,j) && target_ok[j];
    for i in 0..n {
        if !can_enter(i) { continue; }
        let qualified: Vec<usize> = (0..n).filter(|&j|
            if p.fight_select_before_history && model[0] == 'n' && model[2] != 's' {candidate(i,j)}
            else {q(i, j) || (model[2] == 's' && q(j, i))}).collect();
        let targets: Vec<usize> = match model[0] {
            'a' => qualified.clone(),
            'g' => qualified.first().copied().into_iter().collect(),
            'n' => qualified.iter().copied().min_by(|&a, &b| next.near[i][b].unwrap_or(0.0).total_cmp(&next.near[i][a].unwrap_or(0.0)).then(a.cmp(&b))).into_iter().collect(),
            _ => qualified.iter().copied().min_by_key(|&j| (runners[j].rank, j)).into_iter().collect(),
        };
        if std::env::var("RX_CFPAIR").is_ok() && !targets.is_empty() {eprintln!("CFPAIR {} {:?} {:?} {} | {:?}", i, targets, qualified, runners[i].distance,
            qualified.iter().map(|&j|(j,runners[j].rank,next.modes[j].fight_group,next.modes[j].fight,runners[j].distance-runners[i].distance,runners[j].lane-runners[i].lane,next.near[i][j],next.near[j][i])).collect::<Vec<_>>());}
        let grp_partner = if model.get(3) == Some(&'r') {qualified.iter().copied().min_by_key(|&j| (runners[j].rank, j))} else {None};
        for j in targets {
            if !target_ok[j] {continue;}
            let gj = grp_partner.unwrap_or(j);
            let g = next.modes[gj].fight_group.or(next.modes[i].fight_group).unwrap_or(i.min(gj));
            for k in [i, j] {
                if next.modes[k].fight_group.is_none() { next.modes[k].fight_group = Some(g); }
                if !next.modes[k].fight {
                    next.modes[k].fight = true;
                    next.modes[k].fight_count += 1;
                    next.enter_seq.push(k);
                }
            }
        }
    }
    for i in 0..n {
        if old.modes[i].top != next.modes[i].top {
            changes.push(if next.modes[i].top { Change::TopEnter(i) } else { Change::TopExit(i) });
        }
        if old.modes[i].fight != next.modes[i].fight {
            changes.push(if next.modes[i].fight { Change::FightEnter(i) } else { Change::FightExit(i) });
        }
    }
    (next, changes)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn runner(distance: f32, rank: usize) -> Runner {
        Runner { style: Style::Nige, distance, lane: 0.0, speed: 20.0,
            hp: 1000.0, max_hp: 1000.0, guts: 1040.0, strategy_proficiency: 1.0,
            rank, final_straight: false, tempted: false }
    }
    #[test]
    fn controlled_oracle_top_tick_124_to_277() {
        let mut state = State::new(3); let mut t = 0.0f32;
        let p = Params::default(); let mut boundaries = Vec::new();
        for tick in 1..=278 {
            t += TICK;
            let d = if tick < 124 { 149.0 } else { 150.0 };
            let r = [runner(d,1),runner(d-1.0,2),runner(d-2.0,3)];
            let (s,c) = step(&state,&r,2500.0,TICK,&p); state=s;
            if !c.is_empty() { boundaries.push((tick,t,c)); }
        }
        assert_eq!(boundaries.len(),2);
        assert_eq!((boundaries[0].0,boundaries[0].1),(124,8.258392333984375f32));
        assert_eq!((boundaries[1].0,boundaries[1].1),(277,18.448204040527344f32));
        assert_eq!(boundaries[0].2.len(),3); assert_eq!(boundaries[1].2.len(),3);
    }
    #[test]
    fn controlled_oracle_fight_tick_1706_to_1755() {
        let p=Params::default(); let mut s=State::new(9); let mut t=0.0f32;
        let mut start=None; let mut end=None;
        for tick in 1..=1756 {
            t+=TICK;
            let mut r:Vec<_>=(0..9).map(|i|runner(i as f32*20.0,i+1)).collect();
            for q in &mut r { q.style=Style::Other; }
            r[1].distance=2200.0; r[1].rank=7;
            r[5].distance=if tick<1755 {2202.0} else {f32::from_bits(2205.0f32.to_bits()+1)};
            r[5].rank=if tick<1706 {6} else {5};
            r[1].final_straight=tick>=1650; r[5].final_straight=tick>=1650;
            let (ns,c)=step(&s,&r,2500.0,TICK,&p);s=ns;
            if c.contains(&Change::FightEnter(1)) {start=Some((tick,t));}
            if c.contains(&Change::FightExit(1)) {end=Some((tick,t));}
        }
        assert_eq!(start,Some((1706,113.61790466308594f32)));
        assert_eq!(end,Some((1755,116.88115692138672f32)));
    }
    #[test]
    fn natural_top_expiry_does_not_collapse_peer() {
        let p=Params::default();let mut s=State::new(2);s.top_fired[0]=true;
        for m in &mut s.modes {m.top=true;m.top_group=Some(0);m.top_remaining=10.0;}
        s.modes[0].top_remaining=TICK;
        let r=[runner(200.0,1),runner(201.0,2)];
        let (s,_)=step(&s,&r,2500.0,TICK,&p);
        let (s,_)=step(&s,&r,2500.0,TICK,&p);
        assert!(!s.modes[0].top);assert!(s.modes[1].top);
    }
    #[test]
    fn former_dueler_prevents_distance_exit_but_cannot_be_target() {
        let p=Params::default();let mut s=State::new(3);
        s.modes[0].fight=true;s.modes[0].fight_count=1;s.modes[1].fight_count=1;
        let mut r=[runner(200.0,1),runner(204.9,2),runner(203.0,3)];
        for q in &mut r {q.style=Style::Other;q.final_straight=true;}
        let (ns,_)=step(&s,&r,2500.0,TICK,&p);assert!(ns.modes[0].fight);
        s=ns;s.modes[0].fight=false;
        for _ in 0..35 {s=step(&s,&r,2500.0,TICK,&p).0;}
        assert!(!s.modes[2].fight);
        r[1].distance=205.0;s.modes[0].fight=true;
        assert!(step(&s,&r,2500.0,TICK,&p).0.modes[0].fight);
        r[1].distance=f32::from_bits(205.0f32.to_bits()+1);
        assert!(!step(&s,&r,2500.0,TICK,&p).0.modes[0].fight);
    }
    #[test]
    fn fight_continuation_distance_is_inclusive_in_both_directions() {
        let p=Params::default();let mut s=State::new(2);
        s.modes[0].fight=true;s.modes[0].fight_count=1;s.modes[0].fight_group=Some(0);
        s.modes[1].fight_count=1;s.modes[1].fight_group=Some(0);
        for distance in [195.0,205.0] {
            let mut r=[runner(200.0,1),runner(distance,2)];
            for q in &mut r {q.style=Style::Other;}
            assert!(step(&s,&r,2500.0,TICK,&p).0.modes[0].fight);
            let strict=Params {fight_exit_inclusive:false,..p};
            assert!(!step(&s,&r,2500.0,TICK,&strict).0.modes[0].fight);
            r[1].distance=f32::from_bits(if distance>200.0 {distance.to_bits()+1} else {distance.to_bits()-1});
            assert!(!step(&s,&r,2500.0,TICK,&p).0.modes[0].fight);
        }
    }
    #[test]
    fn oldest_former_dueler_blocks_fallback_but_low_hp_does_not() {
        let mut p=Params::default();let mut s=State::new(3);
        let mut r=[runner(200.0,1),runner(201.0,2),runner(202.0,3)];
        for q in &mut r {q.style=Style::Other;q.final_straight=true;}
        s.modes[1].fight_count=1;
        s.near[0][1]=Some(4.0);s.near[0][2]=Some(2.0);s.near[2][0]=Some(2.0);
        let next=step(&s,&r,2500.0,TICK,&p).0;
        assert_eq!(next.enter_seq,vec![2,0]);
        assert!(!next.modes[1].fight);
        r[1].hp=149.0;
        assert_eq!(step(&s,&r,2500.0,TICK,&p).0.enter_seq,vec![0,2]);
        r[1].hp=1000.0;p.fight_select_before_history=false;
        assert_eq!(step(&s,&r,2500.0,TICK,&p).0.enter_seq,vec![0,2]);
    }
    #[test]
    fn effects_match_controlled_speed_and_additive_acceleration() {
        let p=Params::default();let r=runner(200.0,1);let mut m=Mode::default();
        m.top=true;
        assert!((effects(&r,&m,&p).target_speed_add-0.26890755).abs()<1e-6);
        assert_eq!(effects(&r,&m,&p).hp_base_rate,Some(1.4));
        m.top=false;m.fight=true;
        let e=effects(&r,&m,&p);
        assert!((e.target_speed_add-0.5823555).abs()<2e-6);
        assert!((e.acceleration_add-0.1203595).abs()<1e-6);
        assert_eq!(e.hp_base_rate,None);
    }
    #[test]
    fn fight_timer_ignores_hp_speed_rank_until_activation_and_resets_on_gap() {
        let p=Params::default();let mut s=State::new(2);
        let mut r=[runner(200.0,2),runner(202.0,2)];
        for q in &mut r {q.style=Style::Other;q.final_straight=true;q.hp=100.0;}
        r[1].speed=25.0;
        for _ in 0..32 {s=step(&s,&r,2500.0,TICK,&p).0;}
        assert!(!s.modes[0].fight);
        r[0].hp=150.0;r[1].hp=150.0;r[1].speed=20.0;r[1].rank=1;
        s=step(&s,&r,2500.0,TICK,&p).0;
        assert!(s.modes[0].fight && s.modes[1].fight);
        r[0].hp=50.0;s=step(&s,&r,2500.0,TICK,&p).0;
        assert!(s.modes[0].fight);
        r[0].hp=49.9;s=step(&s,&r,2500.0,TICK,&p).0;
        assert!(!s.modes[0].fight && s.modes[1].fight);
        r[1].lane=0.251;s=step(&s,&r,2500.0,TICK,&p).0;
        assert_eq!(s.near[0][1],None);
    }
    #[test]
    fn top_distance_cascade_and_style_partition() {
        let p=Params::default();let mut s=State::new(3);
        let mut r=[runner(200.0,1),runner(199.0,2),runner(198.0,3)];
        s=step(&s,&r,2500.0,TICK,&p).0;
        r[2].distance=193.9;s=step(&s,&r,2500.0,TICK,&p).0;
        assert!(s.modes[0].top && s.modes[1].top && !s.modes[2].top);
        r[1].distance=195.0;s=step(&s,&r,2500.0,TICK,&p).0;
        assert!(!s.modes[0].top && !s.modes[1].top);
        let mut r=[runner(200.0,1),runner(199.0,2)];r[1].style=Style::Oonige;
        assert!(!step(&State::new(2),&r,2500.0,TICK,&p).0.modes[0].top);
    }
}
