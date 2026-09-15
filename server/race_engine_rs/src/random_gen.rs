//! Bit-exact port of random_gen.py's Rule30CARng (itself a port of
//! uma-skill-tools' Random.ts): a 64-bit two-lane elementary-cellular-
//! automaton PRNG (Wolfram Rule 30). Every race result downstream depends
//! on this reproducing the Python/JS bit manipulation exactly, so this file
//! must not "clean up" any operation -- only translate it.
//!
//! Python needs explicit `& 0xFFFFFFFF` masks after every shift because its
//! ints are arbitrary-precision; here every lane is a native `u32`, whose
//! fixed-width `<<`/`>>` already truncate to 32 bits the same way, so those
//! masks simply disappear rather than needing an explicit equivalent.

pub struct Rule30CARng {
    pub hi: u32,
    pub lo: u32,
}

impl Rule30CARng {
    pub fn new(seed_lo: u32, seed_hi: u32) -> Self {
        Rule30CARng { hi: seed_hi, lo: seed_lo }
    }

    pub fn step(&mut self) {
        let hi = self.hi;
        let lo = self.lo;
        let rot = hi >> 31;
        let rolhi = (hi << 1) | (lo >> 31);
        let rollo = (lo << 1) | rot;
        let rot2 = hi << 31;
        let rorhi = (hi >> 1) | (lo << 31);
        let rorlo = (lo >> 1) | rot2;

        self.hi = rorhi ^ (hi | rolhi);
        self.lo = rorlo ^ (lo | rollo);
    }

    pub fn pair(&mut self) -> (u32, u32) {
        let mut x: u32 = 0;
        let mut y: u32 = 0;
        for _ in 0..16 {
            x = (x << 2) | ((self.hi & 0x10000) >> 15) | (self.hi & 1);
            y = (y << 2) | ((self.hi & 0x1000000) >> 23) | ((self.hi & 0x100) >> 8);
            self.step();
        }
        (x, y)
    }

    pub fn int32(&mut self) -> u32 {
        self.pair().0
    }

    pub fn random(&mut self) -> f64 {
        const MASK_HI: u64 = 0x03FF_FFFF;
        const MASK_LO: u64 = 0x07FF_FFFF;
        const EXP: u64 = 0x0800_0000;
        const MANT: f64 = 9_007_199_254_740_992.0; // 0x20000000000000 == 2^53
        let (hi, lo) = self.pair();
        (((hi as u64) & MASK_HI) * EXP + ((lo as u64) & MASK_LO)) as f64 / MANT
    }

    /// Port of `uniform()`. `upper` is sometimes non-integer (e.g. a region
    /// length derived from a course distance); JS's `|` operator
    /// ToInt32-truncates before the bitwise op, which is why the Python
    /// port (and this one) truncates `upper - 1` toward zero explicitly.
    ///
    /// Returns a signed value on purpose: JS's `&` yields a signed int32
    /// and the retry condition compares against that signed value. This
    /// only matters in the degenerate `upper <= 0` case (confirmed to occur
    /// via `AllCornerRandomPolicy` on a region exactly 10 units long); for
    /// every ordinary call `mask` never reaches bit 31 and the sign
    /// reinterpretation is a no-op, exactly as in the Python port.
    pub fn uniform(&mut self, upper: f64) -> i32 {
        let v = (upper - 1.0) as i64;
        let ored = (v | 1) as u32; // low 32 bits of the (possibly negative) two's-complement value
        let clz = ored.leading_zeros(); // matches _clz32: 32 for ored == 0
        let mask: u32 = 0xFFFF_FFFFu32 >> clz;
        loop {
            let n_u32 = self.int32() & mask;
            let n: i64 = if n_u32 >= 0x8000_0000 {
                n_u32 as i64 - 0x1_0000_0000
            } else {
                n_u32 as i64
            };
            if (n as f64) < upper {
                return n as i32;
            }
        }
    }
}
