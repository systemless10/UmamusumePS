
pub struct Rule30CARng {
    pub hi: u32,
    pub lo: u32,
}

impl Rule30CARng {
    pub fn new(seed_lo: u32, seed_hi: u32) -> Self {
        Rule30CARng { hi: seed_hi, lo: seed_lo }
    }

    pub fn seeded(seed: u64) -> Self {
        let mut z = seed.wrapping_add(0x9E3779B97F4A7C15);
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^= z >> 31;
        Rule30CARng { lo: z as u32, hi: (z >> 32) as u32 }
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
        const MANT: f64 = 9_007_199_254_740_992.0;
        let (hi, lo) = self.pair();
        (((hi as u64) & MASK_HI) * EXP + ((lo as u64) & MASK_LO)) as f64 / MANT
    }

    pub fn uniform(&mut self, upper: f64) -> i32 {
        let v = (upper - 1.0) as i64;
        let ored = (v | 1) as u32;
        let clz = ored.leading_zeros();
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
