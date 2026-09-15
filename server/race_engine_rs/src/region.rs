//! Port of region.py -- half-open interval [start, end) set algebra.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Region {
    pub start: f64,
    pub end: f64,
}

impl Region {
    pub fn new(start: f64, end: f64) -> Self {
        Region { start, end }
    }

    pub fn intersect(&self, other: &Region) -> Region {
        let start = self.start.max(other.start);
        let end = self.end.min(other.end);
        if end <= start {
            Region::new(-1.0, -1.0)
        } else {
            Region::new(start, end)
        }
    }

    pub fn fully_contains(&self, other: &Region) -> bool {
        self.start <= other.start && self.end >= other.end
    }
}

/// Port of RegionList.rmap/union. Plain `Vec<Region>` elsewhere; these two
/// operations get free functions since Rust has no subclassing to hang them
/// off of.
pub fn rmap_one(regions: &[Region], f: impl Fn(&Region) -> Region) -> Vec<Region> {
    let mut out = Vec::new();
    for r in regions {
        let newr = f(r);
        if newr.start > -1.0 {
            out.push(newr);
        }
    }
    out
}

/// `rmap` when `f` maps one input region to several output regions (e.g.
/// intersecting against a list of corners/slopes/straights).
pub fn rmap_multi(regions: &[Region], f: impl Fn(&Region) -> Vec<Region>) -> Vec<Region> {
    let mut out = Vec::new();
    for r in regions {
        for nr in f(r) {
            if nr.start > -1.0 {
                out.push(nr);
            }
        }
    }
    out
}

pub fn union(a: &[Region], b: &[Region]) -> Vec<Region> {
    let mut u: Vec<Region> = a.iter().chain(b.iter()).copied().collect();
    let mut r = Vec::new();
    if u.is_empty() {
        return r;
    }
    // Python's list.sort is stable; f64 has a total order here since region
    // bounds are never NaN, so partial_cmp().unwrap() matches sorted(key=...).
    u.sort_by(|x, y| x.start.partial_cmp(&y.start).unwrap());

    let mut acc = u[0];
    for &b in &u[1..] {
        let a = acc;
        if a.fully_contains(&b) {
            acc = a;
        } else if a.start <= b.start && b.start < a.end {
            acc = Region::new(a.start, b.end);
        } else if a.start < b.end && b.end <= a.end {
            acc = Region::new(b.start, a.end);
        } else {
            r.push(a);
            acc = b;
        }
    }
    r.push(acc);
    r
}
