use crate::{course::Course,rng::RaceRandom};
#[derive(Clone,Debug)]
pub struct Trigger {
    pub name:String, pub op:String, pub rhs:i32, pub windows:Vec<(f32,f32)>,
    pub randomized:bool, pub lot:Option<bool>, pub previous:bool,
    pub prev_count:i32, pub this_count:i32,
}
pub type Condition=Vec<Vec<Trigger>>;
fn opt(name:&str)->bool {std::env::var(name).is_ok_and(|v|v=="1")}
fn lot(r:&mut RaceRandom,lo:f32,hi:f32,tag:&str)->(f32,f32) {
    let x=r.float_range(lo,(hi-10.0).max(lo),&||tag.into());if std::env::var("RX_LOTDBG").is_ok() {eprintln!("LOT {tag} {lo} {hi} {x}");}(x,x+10.0)
}
pub fn build(text:&str,c:&Course,r:&mut RaceRandom,tag:&str)->Condition {
    if text.is_empty() {return vec![];}
    text.split('@').map(|g|g.split('&').map(|token| {
        let parsed=["==","!=",">=","<=",">","<"].iter().find_map(|op|token.split_once(op).map(|(n,v)|(n,*op,v)));
        let (n,op,rhs)=parsed.unwrap_or(("invalid","==","0"));
        let v=rhs.parse().unwrap_or(0);
        let mut t=Trigger {name:n.into(),op:op.into(),rhs:v,windows:vec![],randomized:false,lot:None,previous:false,prev_count:0,this_count:0};
        match n {
            "corner_random"=>{t.randomized=true;if let Some(s)=c.corner(v) {t.windows.push(lot(r,s.start,s.end,tag));}},
            "is_finalcorner_random"=>{t.randomized=true;if let Some(s)=c.final_corner() {t.windows.push(lot(r,s.start,s.end,tag));}},
            "straight_random"|"while_in_straight_random"=>{
                t.randomized=true;
                if !c.straights.is_empty() {
                    let i=r.int_max(c.straights.len() as i32,&||tag.into()) as usize;
                    let s=&c.straights[i];t.windows.push(if n=="while_in_straight_random" {(s.start,s.end)} else {lot(r,s.start,s.end,tag)});
                }
            },
            "last_straight_random"=>{
                t.randomized=true;
                if let Some(fc)=c.final_corner() {
                    for s in c.straights.iter().rev().filter(|s|s.start>=fc.start) {
                        t.windows=vec![lot(r,s.start,s.end,tag)];
                    }
                }
            },
            "phase_random"|"phase_firsthalf_random"|"phase_laterhalf_random"=>{
                t.randomized=true;let (mut lo,mut hi)=c.bounds(v);let half=(hi-lo)*0.5;
                if n=="phase_firsthalf_random" {hi=lo+half;}
                if n=="phase_laterhalf_random" {lo+=half;}
                t.windows.push(lot(r,lo,hi,tag));
            },
            "distance_rate_after_random"=>{t.randomized=true;t.windows.push(lot(r,v as f32/100.0*c.distance,c.distance,tag));},
            "up_slope_random"|"down_slope_random"=>{
                t.randomized=true;let sign=if n.starts_with("up") {1} else {-1};
                let mut slopes=c.slopes.iter().filter(|s|s.number==sign).collect::<Vec<_>>();
                if std::env::var("RX_SLOPE_RANDOM_ORDER").map_or(true,|v|v!="0") {slopes.sort_by(|a,b|a.start.total_cmp(&b.start));}
                if !slopes.is_empty() {
                    let i=r.int_max(slopes.len() as i32,&||tag.into()) as usize;
                    t.windows.push(lot(r,slopes[i].start,slopes[i].end,tag));
                }
            },
            "all_corner_random"=>{
                t.randomized=true;let mut candidates=c.corners.iter().map(|s|(s.start,s.end)).collect::<Vec<_>>();
                for _ in 0..4 {
                    if candidates.is_empty() {break;}
                    let i=r.int_range(0,candidates.len() as i32,&||tag.into()) as usize;
                    let pick=candidates[i];let w=lot(r,pick.0,pick.1,tag);t.windows.push(w);
                    let threshold=if pick.1-w.1>=10.0 {candidates[i].0=w.1;w.1}
                        else {candidates.remove(i);pick.0};
                    candidates.retain(|s|s.0>=threshold);
                }
                t.windows.sort_by(|a,b|a.0.total_cmp(&b.0));
            },
            "random_lot"=>t.lot=Some(r.int_max(100,&||tag.into())<v),
            "phase_firstquarter_random"=>{
                t.randomized=true;let (lo,hi)=c.bounds(v);
                t.windows.push(lot(r,lo,lo+(hi-lo)*0.25,tag));
            },
            "phase_corner_random"|"phase_straight_random"|"phase_first_half_straight_random"|"phase_latter_half_straight_random"=>{
                t.randomized=true;let (mut lo,mut hi)=c.bounds(v);let half=(hi-lo)*0.5;
                if n=="phase_first_half_straight_random" {hi=lo+half;}
                if n=="phase_latter_half_straight_random" {lo+=half;}
                let segs:Vec<(f32,f32)>=if n=="phase_corner_random" {c.corners.iter().map(|s|(s.start,s.end)).collect()}
                    else {c.straights.iter().map(|s|(s.start,s.end)).collect()};
                let inside:Vec<(f32,f32)>=segs.into_iter().filter(|s|s.1>lo && s.0<hi).map(|s|(s.0.max(lo),s.1.min(hi))).collect();
                if !inside.is_empty() {
                    if std::env::var("RX_JP_PCR").map_or(true,|v|v!="old") {
                        let total:f32=inside.iter().map(|s|s.1-s.0).sum();
                        let mut x=r.float_max(total,&||tag.into());let _=r.raw(&||tag.into());
                        let mut start=inside[inside.len()-1].1;
                        for s in &inside {if x<=s.1-s.0 {start=s.0+x;break;} x-=s.1-s.0;}
                        t.windows.push((start,start+10.0));
                    } else {
                        let i=r.int_max(inside.len() as i32,&||tag.into()) as usize;
                        t.windows.push(lot(r,inside[i].0,inside[i].1,tag));
                    }
                }
            },
            _=>(),
        } t
    }).collect()).collect()
}
pub fn compare(lhs:f32,op:&str,rhs:f32)->bool {
    match op {"=="=>lhs==rhs,"!="=>lhs!=rhs,">="=>lhs>=rhs,"<="=>lhs<=rhs,">"=>lhs>rhs,"<"=>lhs<rhs,_=>false}
}
pub fn evaluate(c:&mut Condition,distance:f32,query:&impl Fn(&str,i32)->Option<f32>,unsupported:&mut std::collections::BTreeSet<String>)->bool {
    let mut any=false;
    for group in c {
        let mut all=true;
        for t in group {
            let ok=if t.randomized {t.windows.iter().any(|(a,b)|*a<=distance && distance<=*b)}
                else if let Some(lot)=t.lot {lot}
                else if let Some(v)=query(&t.name,t.rhs) {
                    if t.name=="is_activate_any_skill" {
                        let adjusted=v as i32-t.this_count;let prev=t.prev_count;t.prev_count=adjusted;
                        if prev<adjusted {t.this_count+=1;}
                        compare(i32::from(prev<adjusted) as f32,&t.op,t.rhs as f32)
                    } else if t.name=="is_last_straight_onetime" {
                        let ok=!t.previous && v!=0.0;
                        t.previous|=v!=0.0;ok
                    } else {compare(v,&t.op,t.rhs as f32)}
                }
                else {unsupported.insert(format!("trigger:{}",t.name));false};
            all &= ok;
        } any |= all;
    } any
}
