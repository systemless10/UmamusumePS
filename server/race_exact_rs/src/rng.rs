
const MBIG: i32 = 2147483647;
const MSEED: i32 = 161803398;

#[derive(Clone)]
pub struct NetRandom {
    sa: [i32; 56],
    inext: usize,
    inextp: usize,
    pub calls: u64,
    stream_calls: u64,
    skip: Vec<u64>,
    insert: Vec<u64>,
}

impl NetRandom {
    pub fn new(seed: i32) -> Self {
        let subtraction = if seed == i32::MIN { MBIG } else { seed.wrapping_abs() };
        let mut mj = MSEED.wrapping_sub(subtraction);
        let mut sa = [0i32; 56];
        sa[55] = mj;
        let mut mk: i32 = 1;
        for i in 1..55 {
            let ii = (21 * i) % 55;
            sa[ii] = mk;
            mk = mj.wrapping_sub(mk);
            if mk < 0 {
                mk = mk.wrapping_add(MBIG);
            }
            mj = sa[ii];
        }
        for _ in 0..4 {
            for i in 1..56 {
                sa[i] = sa[i].wrapping_sub(sa[1 + (i + 30) % 55]);
                if sa[i] < 0 {
                    sa[i] = sa[i].wrapping_add(MBIG);
                }
            }
        }
        let indices = |name| std::env::var(name).ok().map_or(Vec::new(), |v|
            v.split(',').filter(|s| !s.trim().is_empty()).map(|s| s.trim().parse().expect("RX_RNG indices must be unsigned integers")).collect());
        NetRandom { sa, inext: 0, inextp: 21, calls: 0, stream_calls: 0,
            skip: indices("RX_RNGSKIP"), insert: indices("RX_RNGINSERT") }
    }

    pub fn internal_sample(&mut self) -> i32 {
        let index = self.calls;
        self.calls += 1;
        for _ in 0..self.insert.iter().filter(|&&i| i == index).count() { self.raw_sample(); }
        if self.skip.contains(&index) { return self.clone().raw_sample(); }
        self.raw_sample()
    }

    fn raw_sample(&mut self) -> i32 {
        self.stream_calls += 1;
        let mut inext = self.inext + 1;
        if inext >= 56 {
            inext = 1;
        }
        let mut inextp = self.inextp + 1;
        if inextp >= 56 {
            inextp = 1;
        }
        let mut ret = self.sa[inext].wrapping_sub(self.sa[inextp]);
        if ret == MBIG {
            ret -= 1;
        }
        if ret < 0 {
            ret = ret.wrapping_add(MBIG);
        }
        self.sa[inext] = ret;
        self.inext = inext;
        self.inextp = inextp;
        ret
    }

    pub fn next_double(&mut self) -> f64 {
        self.internal_sample() as f64 * (1.0 / MBIG as f64)
    }

    pub fn next_range(&mut self, lo: i32, hi: i32) -> i32 {
        assert!(lo <= hi, "Random.Next minimum exceeds maximum");
        let range = hi as i64 - lo as i64;
        if range <= MBIG as i64 {
            return (self.next_double() * range as f64) as i64 as i32 + lo;
        }
        let mut value = self.internal_sample() as i64;
        if self.internal_sample() % 2 == 0 {
            value = -value;
        }
        let sample = (value as f64 + (MBIG as f64 - 1.0)) / (2.0 * MBIG as f64 - 1.0);
        ((sample * range as f64) as i64 + lo as i64) as i32
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct Draw {
    pub index: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_calls_after: Option<u64>,
    pub kind: &'static str,
    pub value: f64,
    pub tag: String,
    pub t: f32,
}

#[derive(Clone)]
pub struct RaceRandom {
    r: NetRandom,
    pub trace: Option<Vec<Draw>>,
    pub now: f32,
    pub wide: bool,
}

impl RaceRandom {
    pub fn new(seed: i32, trace: bool) -> Self {
        RaceRandom { r: NetRandom::new(seed), trace: if trace { Some(Vec::new()) } else { None }, now: 0.0, wide: false }
    }

    pub fn calls(&self) -> u64 {
        self.r.calls
    }

    fn log(&mut self, first: u64, kind: &'static str, value: f64, tag: &dyn Fn() -> String) {
        if let Some(t) = self.trace.as_mut() {
            let stream_calls_after = if self.r.skip.is_empty() && self.r.insert.is_empty() {
                None
            } else { Some(self.r.stream_calls) };
            t.push(Draw { index: first, stream_calls_after, kind, value, tag: tag(), t: self.now });
        }
    }

    pub fn raw(&mut self, tag: &dyn Fn() -> String) -> i32 {
        let first = self.r.calls;
        let v = self.r.internal_sample();
        self.log(first, "raw", v as f64, tag);
        v
    }

    pub fn peek_double(&self) -> f64 { self.r.clone().next_double() }
    pub fn peek_doubles(&self, n: usize) -> Vec<f64> { let mut r = self.r.clone(); (0..n).map(|_| r.next_double()).collect() }

    pub fn int_max(&mut self, max: i32, tag: &dyn Fn() -> String) -> i32 {
        let first = self.r.calls;
        let v = self.r.next_range(0, max);
        self.log(first, "int", v as f64, tag);
        v
    }

    pub fn int_range(&mut self, lo: i32, hi: i32, tag: &dyn Fn() -> String) -> i32 {
        let first = self.r.calls;
        let v = self.r.next_range(lo, hi);
        self.log(first, "int", v as f64, tag);
        v
    }

    pub fn float_max(&mut self, max: f32, tag: &dyn Fn() -> String) -> f32 {
        let first = self.r.calls;
        let d = self.r.next_double() as f32;
        let mut v = d * max.abs() + 0.0f32;
        if v < 0.0 {
            v = 0.0;
        } else if max < v {
            v = max;
        }
        self.log(first, "float", v as f64, tag);
        v
    }

    pub fn float_range(&mut self, lo: f32, hi: f32, tag: &dyn Fn() -> String) -> f32 {
        let mode = if self.wide { Wide::Double } else { Wide::Float };
        self.float_range_with(lo, hi, mode, tag)
    }

    pub fn float_range_with(&mut self, lo: f32, hi: f32, mode: Wide, tag: &dyn Fn() -> String) -> f32 {
        let first = self.r.calls;
        let dd = self.r.next_double();
        let d = dd as f32;
        let mut v = match mode {
            Wide::Float => d * (hi - lo).abs() + lo,
            Wide::Double => (dd * (hi - lo).abs() as f64 + lo as f64) as f32,
            Wide::FloatDraw => (d as f64 * (hi - lo).abs() as f64 + lo as f64) as f32,
        };
        if v < lo {
            v = lo;
        } else if hi < v {
            v = hi;
        }
        self.log(first, "float", v as f64, tag);
        v
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Wide { Float, Double, FloatDraw }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_known_dotnet_values() {
        let mut r = NetRandom::new(0);
        assert_eq!(r.internal_sample(), 1559595546);
        assert_eq!(r.internal_sample(), 1755192844);
        assert_eq!(r.internal_sample(), 1649316166);
    }

    #[test]
    fn probe_indices_are_logical_and_skip_peeks() {
        let mut r = NetRandom::new(0);
        r.skip = vec![1];
        r.insert = vec![3];
        assert_eq!(r.internal_sample(), 1559595546);
        assert_eq!(r.internal_sample(), 1755192844);
        assert_eq!(r.internal_sample(), 1755192844);
        assert_eq!(r.stream_calls, 2);
        assert_eq!(r.internal_sample(), 1198642031);
        assert_eq!(r.calls, 4);
        assert_eq!(r.stream_calls, 4);
    }
}
