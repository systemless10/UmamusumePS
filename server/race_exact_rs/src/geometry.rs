use crate::data::{Assets, f32_of};
use crate::course::Course;
fn gon(n:&str)->bool {std::env::var(n).map_or(true,|v|v!="0")}
fn gflag(n:&str)->bool {
    thread_local!(static FLAGS:std::cell::RefCell<std::collections::HashMap<String,bool>>=Default::default());
    if let Some(v)=FLAGS.with(|f|f.borrow().get(n).copied()) {return v;}
    let v=std::env::var(n).map_or(false,|v|v=="1");FLAGS.with(|f|f.borrow_mut().insert(n.to_string(),v));v
}
type V3 = [f32; 3];
type Q = [f32; 4];
fn qmul(a: Q, b: Q) -> Q {
    let [ax,ay,az,aw]=a; let [bx,by,bz,bw]=b;
    if gflag("RX_Q4") {
        let [ax,ay,az,aw,bx,by,bz,bw]=[ax,ay,az,aw,bx,by,bz,bw].map(|v|v as f64);
        return [(aw*bx+ax*bw+ay*bz-az*by) as f32,(aw*by+ay*bw+az*bx-ax*bz) as f32,(aw*bz+az*bw+ax*by-ay*bx) as f32,(aw*bw-ax*bx-ay*by-az*bz) as f32];
    }
    [aw*bx+ax*bw+ay*bz-az*by, aw*by+ay*bw+az*bx-ax*bz,
     aw*bz+az*bw+ax*by-ay*bx, aw*bw-ax*bx-ay*by-az*bz]
}
fn qlerp(a: Q, b: Q, t: f32) -> Q {
    let t=t.clamp(0.0,1.0);
    let q1=gflag("RX_Q1");let q2=gflag("RX_Q2");
    let dot=if q2 {(0..4).map(|i|a[i] as f64*b[i] as f64).sum::<f64>() as f32} else {a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+a[3]*b[3]};
    let sign=if dot<0.0 {-1.0} else {1.0};
    let mut q=std::array::from_fn::<_,4,_>(|i|if q1 {(a[i] as f64+t as f64*(sign as f64*b[i] as f64-a[i] as f64)) as f32} else {a[i]+t*(sign*b[i]-a[i])});
    let n=if q2 {((q[0] as f64*q[0] as f64+q[1] as f64*q[1] as f64+q[2] as f64*q[2] as f64+q[3] as f64*q[3] as f64) as f32).sqrt()}
        else if gflag("RX_Q3") {((q[0] as f64*q[0] as f64+q[1] as f64*q[1] as f64+q[2] as f64*q[2] as f64+q[3] as f64*q[3] as f64).sqrt()) as f32}
        else {(q[0]*q[0]+q[1]*q[1]+q[2]*q[2]+q[3]*q[3]).sqrt()};
    if gflag("RX_Q5") {let inv=1.0/n;for v in &mut q { *v*=inv; }} else {for v in &mut q { *v/=n; }} q
}
fn qlerp_jp(a: Q, b: Q, t: f32) -> Q {
    let t=t.clamp(0.0,1.0);let t1=1.0-t;
    let dot=a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+a[3]*b[3];
    let q:Q=std::array::from_fn(|i|if dot>=0.0 {t1*a[i]+t*b[i]} else {t1*a[i]-t*b[i]});
    let inv=1.0/(q[0]*q[0]+q[1]*q[1]+q[2]*q[2]+q[3]*q[3]).sqrt();
    q.map(|v|v*inv)
}
fn qmul_jp(a: Q, b: Q) -> Q {
    let [ax,ay,az,aw]=a; let [bx,by,bz,bw]=b;
    let (cx,cy,cz)=(ay*bz-az*by,az*bx-ax*bz,ax*by-ay*bx);
    let dot=ax*bx+ay*by+az*bz;
    [ax*bw+bx*aw+cx,ay*bw+by*aw+cy,az*bw+bz*aw+cz,aw*bw-dot]
}
fn qlerp64(a: Q, b: Q, t: f64) -> Q {
    let t=t.clamp(0.0,1.0);
    let dot:f64=(0..4).map(|i|a[i] as f64*b[i] as f64).sum();
    let sign=if dot<0.0 {-1.0} else {1.0};
    let q:[f64;4]=std::array::from_fn(|i|a[i] as f64+t*(sign*b[i] as f64-a[i] as f64));
    let n=(q[0]*q[0]+q[1]*q[1]+q[2]*q[2]+q[3]*q[3]).sqrt();
    std::array::from_fn(|i|(q[i]/n) as f32)
}
pub struct Geometry {
    keys: Vec<V3>, rotations: Vec<Q>, distance: f32, fix: Q,
    pub jp: bool,
    goal: V3, goal_rot: Q, goal_dir: V3, pub distance_to_world: f32,
    overrun: Option<(Vec<V3>,Vec<Q>,f32)>,
}
type Keys=(Vec<V3>,Vec<Q>);
fn lane_keys(a:&Assets,name:&str)->Result<Keys,String> {
    let k=&a.lane_asset(name)["key"];
    let x=k["valueX"].as_array().ok_or("lane x keys")?;
    let z=k["valueZ"].as_array().ok_or("lane z keys")?;
    let y=k["valueY"].as_array().filter(|v| !v.is_empty());
    if x.is_empty() || z.len()!=x.len() || y.is_some_and(|y|y.len()!=x.len()) {
        return Err("inconsistent lane position keys".into());
    }
    let keys=x.iter().enumerate().map(|(i,x)|[f32_of(x),y.map_or(0.0,|y|f32_of(&y[i])),f32_of(&z[i])]).collect();
    let rotations=k["rotation"].as_array().ok_or("rotation keys")?.iter()
        .map(|r|[f32_of(&r["x"]),f32_of(&r["y"]),f32_of(&r["z"]),f32_of(&r["w"])]).collect::<Vec<_>>();
    if rotations.is_empty() { return Err("empty rotation keys".into()); }
    Ok((keys,rotations))
}
impl Geometry {
    pub fn load(a: &Assets, c: &Course, jp_region: bool) -> Result<Self,String> {
        let name=a.course_set(c.id)["normal_lane_asset"].as_str().ok_or("normal lane asset")?;
        let (keys,rotations)=lane_keys(a,name)?;
        let overrun=(|| {
            let (dir,file)=name.rsplit_once('/')?;let p=file.split('_').collect::<Vec<_>>();
            if p.len()!=8 {return None;}
            let o=format!("{dir}/{}_{}_{}_01_0000_{}_0_{}",p[0],p[1],p[2],p[5],p[7]);
            if a.lane_asset(&o).is_null() {return None;}
            let d=a.lane_asset(&o)["Distance"].as_f64()? as f32;
            lane_keys(a,&o).ok().map(|(k,r)|(k,r,d))
        })().filter(|_|gon("RX_OVERRUN"));
        let h=(if c.turn==1 {180.0f32} else {0.0}) * 0.01745329238474369 * 0.5;
        let mut g=Self { keys, rotations, distance:c.distance, fix:[0.0,h.sin(),0.0,h.cos()], jp:jp_region && gon("RX_JP_GEOM"),
            goal:[0.0;3],goal_rot:[0.0;4],goal_dir:[0.0;3],distance_to_world:0.0,overrun:None };
        let (p,_) = g.transform(c.distance-10.0);
        (g.goal,g.goal_rot)=g.transform(c.distance);
        g.goal_dir=std::array::from_fn(|i|(g.goal[i]-p[i])*0.1);
        g.overrun=overrun;
        let a=g.world(0.0,0.0);let b=g.world(1.0,0.0);
        let [dx,dy,dz]=std::array::from_fn(|i|a[i]-b[i]);
        g.distance_to_world=if g.jp {(dy*dy+dx*dx+dz*dz).sqrt()}
            else if gon("RX_K1") {((dy as f64)*(dy as f64)+(dx as f64)*(dx as f64)+(dz as f64)*(dz as f64)).sqrt() as f32}
            else if gflag("RX_K2") {((dx as f64)*(dx as f64)+(dz as f64)*(dz as f64)).sqrt() as f32}
            else {((dy*dy+dx*dx+dz*dz) as f64).sqrt() as f32};
        if let Some(s)=std::env::var("RX_D2WSPAN").ok().and_then(|v|v.parse::<f32>().ok()) {
            let b=g.world(s,0.0);let d:[f32;3]=std::array::from_fn(|i|a[i]-b[i]);
            g.distance_to_world=((d[0]*d[0]+d[1]*d[1]+d[2]*d[2]) as f64).sqrt() as f32/s;
        }
        if let Some(m)=std::env::var("RX_D2WMUL").ok().and_then(|v|v.parse::<f64>().ok()) {g.distance_to_world=(g.distance_to_world as f64*m) as f32;}
        if let Some(k)=std::env::var("RX_D2WULP").ok().and_then(|v|v.parse::<i32>().ok()) {g.distance_to_world=f32::from_bits((g.distance_to_world.to_bits() as i32+k) as u32);}
        if std::env::var("RX_D2WDBG").is_ok() {eprintln!("D2W {:.9} dxz {:.9} dy {:.9}",g.distance_to_world,((dx*dx+dz*dz) as f64).sqrt(),dy);}
        Ok(g)
    }
    fn transform(&self,d:f32)->(V3,Q) {
        if d>self.distance && let Some((keys,rots,od))=&self.overrun {
            let r=(d-self.distance)%od;
            let t=if !self.jp && gon("RX_OVERRUN64") {((r as f64)/(*od as f64)*1000.0) as f32} else {r/od*1000.0};
            return self.sample(keys,rots,t);
        }
        if d>self.distance {
            return (std::array::from_fn(|i|self.goal_dir[i]*(d-self.distance)+self.goal[i]), self.goal_rot);
        }
        if self.jp {return self.sample(&self.keys,&self.rotations,self.distance_jp_t(d));}
        if gflag("RX_G4") {
            let t=(d as f64)/(self.distance as f64)*1000.0;let i=t as usize;
            let f=(t-i as f64).clamp(0.0,1.0);
            let p=if i<self.keys.len()-1 {std::array::from_fn(|j|(((self.keys[i+1][j] as f64)-(self.keys[i][j] as f64))*f+self.keys[i][j] as f64) as f32)}
                else {self.keys[self.keys.len()-1]};
            let r=if i<self.rotations.len()-1 {
                if gflag("RX_G5") {qlerp64(self.rotations[i],self.rotations[i+1],f)} else {qlerp(self.rotations[i],self.rotations[i+1],f as f32)}
            } else {self.rotations[self.rotations.len()-1]};
            return (p,qmul(r,self.fix));
        }
        if gflag("RX_G9") {
            let t64=(d as f64)/(self.distance as f64)*1000.0;let t=t64 as f32;
            let i=t64 as usize;let f=((t64-i as f64) as f32).clamp(0.0,1.0);let keys=&self.keys;let rotations=&self.rotations;
            let p=if i<keys.len()-1 {std::array::from_fn(|j|(((keys[i+1][j] as f64)-(keys[i][j] as f64))*(f as f64)+(keys[i][j] as f64)) as f32)} else {keys[keys.len()-1]};
            let _=t;
            let r=if i<rotations.len()-1 {qlerp(rotations[i],rotations[i+1],f)} else {rotations[rotations.len()-1]};
            return (p,qmul(r,self.fix));
        }
        if gflag("RX_G15") {
            let t64=(d as f64)/(self.distance as f64)*1000.0;let t=t64 as f32;
            let i=(t64 as usize).min(t as usize);
            if std::env::var("RX_G15DBG").is_ok() && i!=t as usize {eprintln!("G15 d={d} t={t} t64={t64}");}
            return self.sample_i(&self.keys,&self.rotations,t,i);
        }
        let t=if gflag("RX_G6") {((d as f64)*1000.0/(self.distance as f64)) as f32}
            else if gon("RX_G1") {((d as f64)/(self.distance as f64)*1000.0) as f32} else {d/self.distance*1000.0};
        self.sample(&self.keys,&self.rotations,t)
    }
    fn distance_jp_t(&self,d:f32)->f32 {d/self.distance*1000.0}
    fn sample(&self,keys:&[V3],rotations:&[Q],t:f32)->(V3,Q) {
        self.sample_i(keys,rotations,t,t as usize)
    }
    fn sample_i(&self,keys:&[V3],rotations:&[Q],t:f32,i:usize)->(V3,Q) {
        if self.jp {
            let p=if i<keys.len()-1 {let f=(t-i as f32).clamp(0.0,1.0);std::array::from_fn(|j|keys[i][j]*(1.0-f)+keys[i+1][j]*f)} else {keys[keys.len()-1]};
            let r=if i<rotations.len()-1 {qlerp_jp(rotations[i],rotations[i+1],t-i as f32)} else {rotations[rotations.len()-1]};
            return (p,qmul_jp(r,self.fix));
        }
        let p=if i<keys.len()-1 {
            let f=(t-i as f32).clamp(0.0,1.0);
            if gon("RX_G2") {std::array::from_fn(|j|(((keys[i+1][j] as f64)-(keys[i][j] as f64))*(f as f64)+(keys[i][j] as f64)) as f32)}
            else {std::array::from_fn(|j|(keys[i+1][j]-keys[i][j])*f+keys[i][j])}
        } else {keys[keys.len()-1]};
        let r=if i<rotations.len()-1 {
                if gflag("RX_G5") {qlerp64(rotations[i],rotations[i+1],(t-i as f32) as f64)} else {qlerp(rotations[i],rotations[i+1],t-i as f32)}}
            else {rotations[rotations.len()-1]};
        (p,qmul(r,self.fix))
    }
    pub fn world64(&self,d:f32,lane:f32,mode:i32)->[f64;3] {
        let (o,[x,y,z,w])=self.transform(d);let l=lane*11.25;
        let [x2,y2,z2]=[x+x,y+y,z+z];let yy=y2*y;let zz=z2*z;let l64=l as f64;
        let f=|a:f32,b:f32,sgn:f64|(a as f64+sgn*b as f64);
        let t=[(1.0-f(yy,zz,1.0))*l64,f(x2*y,z2*w,1.0)*l64,f(x2*z,y2*w,-1.0)*l64];
        let t=if mode==2 {t.map(|v|v as f32 as f64)} else {t};
        [t[0]+o[0] as f64,t[1]+o[1] as f64,t[2]+o[2] as f64]
    }
    fn origin64(&self,d:f32)->Option<[f64;3]> {
        if d>self.distance {return None;}
        let t=((d as f64)/(self.distance as f64)*1000.0) as f32;let i=t as usize;let keys=&self.keys;
        if i>=keys.len()-1 {return None;}
        let f=(t-i as f32).clamp(0.0,1.0) as f64;
        Some(std::array::from_fn(|j|((keys[i+1][j] as f64)-(keys[i][j] as f64))*f+(keys[i][j] as f64)))
    }
fn mul_f32_exact(a:f64,b:f64)->f32 {
    let p=a*b;let e=a.mul_add(b,-p);
    let r=p as f32;let lo=f32::from_bits(r.to_bits()-1);let hi=f32::from_bits(r.to_bits()+1);
    for n in [lo,hi] { if (r as f64+n as f64)*0.5==p { let up=(n as f64>p)==(e>0.0); return if e==0.0 {r} else if up {n} else {r}; } }
    r
}
    pub fn world_variants(&self,d:f32,lane:f32)->Vec<V3> {
        let (o,[x,y,z,w])=self.transform(d);let l=lane*11.25;let l64=l as f64;
        let [x2,y2,z2]=[x+x,y+y,z+z];let yy=y2*y;let zz=z2*z;
        let f=|a:f32,b:f32,sgn:f64|((a as f64+sgn*b as f64));
        let g7=[((1.0-f(yy,zz,1.0))*l64) as f32+o[0],(f(x2*y,z2*w,1.0)*l64) as f32+o[1],(f(x2*z,y2*w,-1.0)*l64) as f32+o[2]];
        let fl=[(1.0-(yy+zz))*l+o[0],(x2*y+z2*w)*l+o[1],(x2*z-y2*w)*l+o[2]];
        let g10=[((1.0-f(yy,zz,1.0))*l64+o[0] as f64) as f32,(f(x2*y,z2*w,1.0)*l64+o[1] as f64) as f32,(f(x2*z,y2*w,-1.0)*l64+o[2] as f64) as f32];
        let c=[(1.0-f(yy,zz,1.0)) as f32,f(x2*y,z2*w,1.0) as f32,f(x2*z,y2*w,-1.0) as f32];
        let g12=[c[0]*l+o[0],c[1]*l+o[1],c[2]*l+o[2]];
        let (xd,yd,zd,wd)=(x as f64,y as f64,z as f64,w as f64);
        let g3=[((1.0-(2.0*yd*yd+2.0*zd*zd))*l64+o[0] as f64) as f32,((2.0*xd*yd+2.0*zd*wd)*l64+o[1] as f64) as f32,((2.0*xd*zd-2.0*yd*wd)*l64+o[2] as f64) as f32];
        let g3b=[((1.0-(2.0*yd*yd+2.0*zd*zd))*l64) as f32+o[0],((2.0*xd*yd+2.0*zd*wd)*l64) as f32+o[1],((2.0*xd*zd-2.0*yd*wd)*l64) as f32+o[2]];
        vec![g7,fl,g10,g12,g3,g3b]
    }
    pub fn world(&self,d:f32,lane:f32)->V3 {
        let (o,[x,y,z,w])=self.transform(d);
        static LW:std::sync::OnceLock<f32>=std::sync::OnceLock::new();
        let l=lane*LW.get_or_init(||std::env::var("RX_LANEW").ok().and_then(|v|v.parse().ok()).unwrap_or(11.25));
        if gflag("RX_G3") {
            let (x,y,z,w,l)=(x as f64,y as f64,z as f64,w as f64,l as f64);
            let [x2,y2,z2]=[x+x,y+y,z+z];let yy=y2*y;let zz=z2*z;
            return [((1.0-(yy+zz))*l+o[0] as f64) as f32,((x2*y+z2*w)*l+o[1] as f64) as f32,((x2*z-y2*w)*l+o[2] as f64) as f32];
        }
        if self.jp {
            let [x2,y2,z2]=[x+x,y+y,z+z];
            let v=[1.0-y*y2-z*z2,x*y2+w*z2,x*z2-w*y2];
            return std::array::from_fn(|k|o[k]+l*v[k]);
        }
        let [x2,y2,z2]=[x+x,y+y,z+z];let yy=y2*y;let zz=z2*z;
        if gon("RX_G7") {
            let (l64,f)=(l as f64,|a:f32,b:f32,sgn:f64|((a as f64+sgn*b as f64)));
            if gflag("RX_G12") {let c=[(1.0-f(yy,zz,1.0)) as f32,f(x2*y,z2*w,1.0) as f32,f(x2*z,y2*w,-1.0) as f32];return [c[0]*l+o[0],c[1]*l+o[1],c[2]*l+o[2]];}
            if gflag("RX_G13") {let c=[(1.0-f(yy,zz,1.0)) as f32,f(x2*y,z2*w,1.0) as f32,f(x2*z,y2*w,-1.0) as f32];return [((c[0] as f64*l64) as f32)+o[0],((c[1] as f64*l64) as f32)+o[1],((c[2] as f64*l64+o[2] as f64) as f32)];}
            if gflag("RX_G14") {let c=[(1.0-f(yy,zz,1.0)) as f32,f(x2*y,z2*w,1.0) as f32,f(x2*z,y2*w,-1.0) as f32];return [(c[0] as f64*l64+o[0] as f64) as f32,(c[1] as f64*l64+o[1] as f64) as f32,(c[2] as f64*l64+o[2] as f64) as f32];}
            if gflag("RX_G16") && let Some(o6)=self.origin64(d) {
                let t=[((1.0-f(yy,zz,1.0))*l64),(f(x2*y,z2*w,1.0)*l64),(f(x2*z,y2*w,-1.0)*l64)];
                let t=if gflag("RX_G16B") {t.map(|v|v as f32 as f64)} else {t};
                return [(t[0]+o6[0]) as f32,(t[1]+o6[1]) as f32,(t[2]+o6[2]) as f32];
            }
            if gflag("RX_G17") {
                return [Self::mul_f32_exact(1.0-f(yy,zz,1.0),l64)+o[0],Self::mul_f32_exact(f(x2*y,z2*w,1.0),l64)+o[1],Self::mul_f32_exact(f(x2*z,y2*w,-1.0),l64)+o[2]];
            }
            if gflag("RX_G10") {return [((1.0-f(yy,zz,1.0))*l64+o[0] as f64) as f32,(f(x2*y,z2*w,1.0)*l64+o[1] as f64) as f32,(f(x2*z,y2*w,-1.0)*l64+o[2] as f64) as f32];}
            if gflag("RX_G11") {return [((1.0-f(yy,zz,1.0))*l64) as f32+o[0],(f(x2*y,z2*w,1.0)*l64) as f32+o[1],(f(x2*z,y2*w,-1.0)*l64+o[2] as f64) as f32];}
            return [((1.0-f(yy,zz,1.0))*l64) as f32+o[0],(f(x2*y,z2*w,1.0)*l64) as f32+o[1],(f(x2*z,y2*w,-1.0)*l64) as f32+o[2]];
        }
        [(1.0-(yy+zz))*l+o[0],(x2*y+z2*w)*l+o[1],(x2*z-y2*w)*l+o[2]]
    }
}
