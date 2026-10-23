#[derive(Clone,Copy,Debug)]
pub enum Operation {Add,Set}
#[derive(Clone,Debug)]
pub struct Modifier {pub index:usize,pub value:f32,pub operation:Operation,pub owner:u64,pub once:bool,pub remaining:Option<f32>}
#[derive(Clone,Default,Debug)]
pub struct Modifiers {pub entries:Vec<Modifier>}
impl Modifiers {
    pub fn add(&mut self,index:usize,value:f32,operation:Operation,owner:u64,once:bool) {
        self.entries.push(Modifier{index,value,operation,owner,once,remaining:None});
    }
    pub fn add_timed(&mut self,index:usize,value:f32,operation:Operation,owner:u64,once:bool,remaining:f32) {
        self.entries.push(Modifier{index,value,operation,owner,once,remaining:Some(remaining)});
    }
    pub fn update_timed(&mut self,dt:f32) {
        self.entries.retain_mut(|m|match &mut m.remaining {
            Some(left) if *left>=0.0=>{*left-=dt;*left>0.0},
            _=>true,
        });
    }
    pub fn apply(&self,index:usize,mut value:f32)->f32 {
        for m in self.entries.iter().filter(|m|m.index==index && !m.once) {
            value=match m.operation {Operation::Add=>value+m.value,Operation::Set=>m.value};
        } value
    }
    pub fn consume(&mut self,index:usize,mut value:f32)->f32 {
        for m in self.entries.iter().filter(|m|m.index==index && m.once) {
            value=match m.operation {Operation::Add=>value+m.value,Operation::Set=>m.value};
        }
        self.entries.retain(|m|!(m.index==index && m.once));value
    }
    pub fn stop(&mut self,owner:u64) {self.entries.retain(|m|m.owner!=owner || m.remaining.is_some());}
    pub fn has(&self,index:usize)->bool {self.entries.iter().any(|m|m.index==index)}
}
