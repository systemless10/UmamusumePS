//! Port of activation_sample_policy.py -- turns a RegionList (where a
//! skill's condition can statically hold) into one concrete trigger Region
//! per race sample, plus the double-dispatch `reconcile*` logic used when an
//! `&` (AndOperator) combines two conditions with different sample
//! policies.
//!
//! Python's caching of Uniform/LogNormal/Erlang policy *objects*
//! (`_erlang_cache` etc.) existed only to avoid allocating many identical
//! Python objects; since these are plain Rust value types here, no
//! interning is needed for correctness -- only Python's `reconcile*`
//! *dispatch table* has to be reproduced exactly (see `reconcile` below),
//! since which of the two operand policies survives an `&` combination is
//! behaviorally significant (a right-hand LogNormal(3,1) beats a left-hand
//! Erlang(2,4), for example, and that choice changes trigger placement).

use crate::region::{union, Region};

#[derive(Clone, Debug)]
pub enum SamplePolicy {
    Immediate,
    Random,
    Uniform,
    LogNormal(f64, f64),
    Erlang(i32, f64),
    StraightRandom,
    AllCornerRandom,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Immediate,
    Random,
    Distribution,
    Straight,
    AllCorner,
}

impl SamplePolicy {
    fn kind(&self) -> Kind {
        match self {
            SamplePolicy::Immediate => Kind::Immediate,
            SamplePolicy::Random => Kind::Random,
            SamplePolicy::Uniform | SamplePolicy::LogNormal(..) | SamplePolicy::Erlang(..) => Kind::Distribution,
            SamplePolicy::StraightRandom => Kind::Straight,
            SamplePolicy::AllCornerRandom => Kind::AllCorner,
        }
    }

    /// Transcribed from the double-dispatch `reconcile`/`reconcile_*`
    /// methods on each concrete Python policy class. Traced out by hand
    /// into a direct table below (see the module the port was written
    /// against for the derivation) -- `self` is the left operand of `&`,
    /// `other` the right. Returns Err on the one combination Python raises
    /// ValueError for (Straight combined with AllCorner, in either order).
    pub fn reconcile(&self, other: &SamplePolicy) -> Result<SamplePolicy, String> {
        use Kind::*;
        let a = self.kind();
        match other.kind() {
            Immediate => Ok(self.clone()),
            Random => Ok(other.clone()),
            Distribution => match a {
                Immediate | Distribution => Ok(other.clone()),
                Random | Straight | AllCorner => Ok(self.clone()),
            },
            Straight => match a {
                Immediate | Distribution | Random => Ok(other.clone()),
                Straight => Ok(self.clone()),
                AllCorner => Err("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy".to_string()),
            },
            AllCorner => match a {
                Immediate | Distribution | Random | AllCorner => Ok(other.clone()),
                Straight => Err("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy".to_string()),
            },
        }
    }

    pub fn sample(&self, regions: &[Region], nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<Region> {
        match self {
            SamplePolicy::Immediate => regions.iter().take(1).copied().collect(),
            SamplePolicy::Random => sample_random(regions, nsamples, rng),
            SamplePolicy::Uniform => sample_distribution(regions, nsamples, rng, &uniform_distribution),
            SamplePolicy::LogNormal(mu, sigma) => {
                sample_distribution(regions, nsamples, rng, &|upper, n, rng| log_normal_distribution(*mu, *sigma, upper, n, rng))
            }
            SamplePolicy::Erlang(k, lam) => {
                sample_distribution(regions, nsamples, rng, &|upper, n, rng| erlang_distribution(*k, *lam, upper, n, rng))
            }
            SamplePolicy::StraightRandom => sample_straight_random(regions, nsamples, rng),
            SamplePolicy::AllCornerRandom => (0..nsamples).map(|_| place_all_corner_triggers(regions, rng)).collect(),
        }
    }
}

fn sample_random(regions: &[Region], nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<Region> {
    if regions.is_empty() {
        return Vec::new();
    }
    let mut acc = 0.0;
    let mut weights = Vec::with_capacity(regions.len());
    for r in regions {
        acc += r.end - r.start;
        weights.push(acc);
    }
    let mut samples = Vec::with_capacity(nsamples);
    for _ in 0..nsamples {
        let threshold = rng.uniform(acc) as f64;
        let region = regions.iter().enumerate().find(|(i, _)| weights[*i] > threshold).unwrap().1;
        let pos = region.start + rng.uniform(region.end - region.start - 10.0) as f64;
        samples.push(pos);
    }
    samples.into_iter().map(|pos| Region::new(pos, pos + 10.0)).collect()
}

fn sample_straight_random(regions: &[Region], nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<Region> {
    if regions.is_empty() {
        return Vec::new();
    }
    let mut samples = Vec::with_capacity(nsamples);
    for _ in 0..nsamples {
        let r = regions[rng.uniform(regions.len() as f64) as usize];
        let pos = r.start + rng.uniform(r.end - r.start - 10.0) as f64;
        samples.push(Region::new(pos, pos + 10.0));
    }
    samples
}

fn sample_distribution(
    regions: &[Region],
    nsamples: usize,
    rng: &mut crate::random_gen::Rule30CARng,
    distribution: &dyn Fn(f64, usize, &mut crate::random_gen::Rule30CARng) -> Vec<f64>,
) -> Vec<Region> {
    if regions.is_empty() {
        return Vec::new();
    }
    let rng_total: f64 = regions.iter().map(|r| r.end - r.start).sum();
    let mut rs: Vec<Region> = regions.to_vec();
    rs.sort_by(|a, b| a.start.partial_cmp(&b.start).unwrap());
    let randoms = distribution(rng_total, nsamples, rng);
    let mut samples = Vec::with_capacity(nsamples);
    for &r0 in randoms.iter().take(nsamples) {
        let mut pos = r0;
        let mut j = 0usize;
        loop {
            pos += rs[j].start;
            if pos > rs[j].end {
                pos -= rs[j].end;
                j += 1;
            } else {
                samples.push(Region::new(pos, rs[j].end));
                break;
            }
        }
    }
    samples
}

fn uniform_distribution(upper: f64, nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<f64> {
    (0..nsamples).map(|_| rng.uniform(upper) as f64).collect()
}

fn log_normal_distribution(mu: f64, sigma: f64, upper: f64, nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<f64> {
    let mut nums: Vec<f64> = Vec::new();
    let halfn = (nsamples as f64 / 2.0).ceil() as usize;
    for _ in 0..halfn {
        let (x, y, r2);
        loop {
            let xx = rng.random() * 2.0 - 1.0;
            let yy = rng.random() * 2.0 - 1.0;
            let rr2 = xx * xx + yy * yy;
            if rr2 != 0.0 && rr2 < 1.0 {
                x = xx;
                y = yy;
                r2 = rr2;
                break;
            }
        }
        let m = (-2.0 * r2.ln() / r2).sqrt() * sigma;
        nums.push((x * m + mu).exp());
        nums.push((y * m + mu).exp());
    }
    let min_v = (mu + sigma * -3.09023).exp();
    let max_v = (mu + sigma * 3.09023).exp();
    let rng_range = max_v - min_v;
    nums.into_iter().map(|n| (upper * (((n - min_v).max(0.0) / rng_range).min(1.0))).floor()).collect()
}

fn erlang_distribution(k: i32, lam: f64, upper: f64, nsamples: usize, rng: &mut crate::random_gen::Rule30CARng) -> Vec<f64> {
    let mut nums = Vec::with_capacity(nsamples);
    for _ in 0..nsamples {
        let mut u = 1.0;
        for _ in 0..k {
            u *= rng.random();
        }
        nums.push(-u.ln() / lam);
    }
    let kf = k as f64;
    let min_v = kf * (1.0 - 1.0 / (9.0 * kf) + -3.09023 * (1.0 / (9.0 * kf)).sqrt()).powi(3) / lam;
    let max_v = kf * (1.0 - 1.0 / (9.0 * kf) + 3.09023 * (1.0 / (9.0 * kf)).sqrt()).powi(3) / lam;
    let rng_range = max_v - min_v;
    nums.into_iter().map(|n| (upper * (((n - min_v).max(0.0) / rng_range).min(1.0))).floor()).collect()
}

fn place_all_corner_triggers(regions: &[Region], rng: &mut crate::random_gen::Rule30CARng) -> Region {
    let mut triggers: Vec<f64> = Vec::new();
    let mut candidates: Vec<Region> = regions.to_vec();
    candidates.sort_by(|a, b| a.start.partial_cmp(&b.start).unwrap());
    while triggers.len() < 4 && !candidates.is_empty() {
        let ci = rng.uniform(candidates.len() as f64) as usize;
        let c = candidates[ci];
        let start = c.start + rng.uniform(c.end - c.start - 10.0) as f64;
        if start + 20.0 <= c.end {
            candidates.splice(ci..ci + 1, [Region::new(start + 10.0, c.end)]);
        } else {
            candidates.remove(ci);
        }
        candidates.drain(0..ci);
        triggers.push(start);
    }
    Region::new(triggers[0], triggers[0] + 10.0)
}

/// Exposed for callers (e.g. AndOperator/OrOperator over static regions)
/// that need plain region-set union outside the sample-policy machinery.
pub fn union_regions(a: &[Region], b: &[Region]) -> Vec<Region> {
    union(a, b)
}
