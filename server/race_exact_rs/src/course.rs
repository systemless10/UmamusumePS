use crate::data::{Assets, f32_of};
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct Event { pub kind: i32, pub distance: f32, pub values: Vec<i32> }
#[derive(Clone, Debug)]
pub struct Segment { pub start: f32, pub end: f32, pub number: i32, pub per: f32 }
#[derive(Clone, Debug)]
pub struct Course {
    pub id: i32, pub distance: f32, pub ground: i32, pub turn: i32,
    pub lane_max: f32, pub master: Value, pub events: Vec<Event>,
    pub corners: Vec<Segment>, pub straights: Vec<Segment>, pub slopes: Vec<Segment>,
    pub first_move: f32, pub empty_in_move: bool,
}
impl Course {
    pub fn load(assets: &Assets, id: i32) -> Result<Self, String> {
        let cs = assets.course_set(id);
        if cs.is_null() { return Err(format!("missing Global course {id}")); }
        let m = &cs["master"];
        let mut c = Self { id, distance: m["distance"].as_i64().ok_or("course distance")? as f32,
            ground: m["ground"].as_i64().ok_or("course ground")? as i32,
            turn: m["turn"].as_i64().ok_or("course turn")? as i32,
            lane_max: m["float_lane_max"].as_i64().ok_or("course lane maximum")? as f32 * 0.0001,
            master: m.clone(), events: vec![], corners: vec![], straights: vec![], slopes: vec![],
            first_move: 0.0, empty_in_move: false };
        let mut pending: Option<(f32, i32)> = None;
        for object in cs["event_asset"]["objects"].as_array().ok_or("course events")? {
            for p in object["fields"]["courseParams"].as_array().ok_or("courseParams")? {
                let e = Event { kind: p["_paramType"].as_i64().ok_or("event type")? as i32,
                    distance: f32_of(&p["_distance"]),
                    values: p["_values"].as_array().ok_or("event values")?.iter()
                        .map(|v| v.as_i64().unwrap() as i32).collect() };
                match (e.kind, e.values.as_slice()) {
                    (0, [number, rest @ ..]) => c.corners.push(Segment { start: e.distance,
                        end: e.distance + rest.first().copied().unwrap_or(200) as f32,
                        number: *number, per: 0.0 }),
                    (2, [1, rest @ ..]) => pending = Some((e.distance, rest.first().copied().unwrap_or(0))),
                    (2, [2, ..]) => { if let Some((start, number)) = pending.take() {
                        c.straights.push(Segment { start, end: e.distance, number, per: 0.0 });
                    } },
                    (11, [per, length]) => c.slopes.push(Segment { start: e.distance,
                        end: e.distance + *length as f32, number: per.signum(), per: *per as f32 / 10000.0 }),
                    _ => (),
                }
                c.events.push(e);
            }
        }
        if let Some(e) = c.events.iter().find(|e| e.kind == 7).filter(|e| e.values.len() == 1) {
            c.first_move = e.distance; c.empty_in_move = e.values[0] == 1;
        } else if let Some(e) = c.events.iter().find(|e| e.kind == 7 && e.values.first() == Some(&1)) {
            c.first_move = e.distance; c.empty_in_move = e.values.get(1) == Some(&1);
        } else if let Some(corner) = c.corners.first().filter(|x| x.start > 0.0) {
            c.first_move = corner.start; c.empty_in_move = true;
        }
        crate::netsort::sort(&mut c.events, &|a,b| {
            if a.distance < b.distance { -1 } else if a.distance > b.distance { 1 } else { 0 }
        });
        Ok(c)
    }
    pub fn phase(&self, distance: f32) -> usize {
        let s = self.distance / 24.0;
        if distance < s * 4.0 { 0 } else if distance < s * 16.0 { 1 }
        else if distance < s * 20.0 { 2 } else if distance < self.distance { 3 } else { 4 }
    }
    pub fn bounds(&self, phase: i32) -> (f32, f32) {
        let s = self.distance / 24.0;
        match phase { 0 => (0.0,s*4.0), 1 => (s*4.0,s*16.0), 2 => (s*16.0,s*20.0),
            3 => (s*20.0,self.distance), 4 => (self.distance,f32::MAX), _ => (0.0,0.0) }
    }
    pub fn corner(&self, n: i32) -> Option<&Segment> {
        self.corners.iter().rev().find(|s| s.number == n && n > 0)
    }
    pub fn final_corner(&self) -> Option<&Segment> {
        self.corners.iter().rev().find(|s| s.number > 3 && self.distance-s.start <= 1000.0)
    }
}
