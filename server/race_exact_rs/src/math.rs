#[path = "math_tables.rs"]
mod tables;
use tables::*;

pub fn approximately(a: f32, b: f32) -> bool {
    (b - a).abs() < (0.000001 * a.abs().max(b.abs())).max(f32::from_bits(8))
}

pub fn gate_movement(movement: f32, remaining_delay: f32, dt: f32) -> f32 {
    (movement as f64 * (remaining_delay.abs() as f64 / dt as f64)) as f32
}

pub fn log10(x: f32) -> f32 {
    if x == 0.0 { return f32::NEG_INFINITY; }
    if x < 0.0 { return f32::NAN; }
    if !x.is_finite() { return x; }
    let mut bits = x.to_bits();
    let mut exponent = ((bits >> 23) as i32 - 127) as f32;
    let mut mantissa = bits & 0x7fffff;
    if exponent == -127.0 {
        let normalized = f32::from_bits(mantissa | 0x3f800000) - 1.0;
        bits = normalized.to_bits() & 0x7fffff;
        exponent = ((normalized.to_bits() >> 23) as i32 - 253) as f32;
        mantissa = bits;
    }
    if (x - 1.0).abs() < 0.0625 {
        let r = x - 1.0;
        let u = r / (2.0 + r);
        let correction = r * u;
        let u = u + u;
        let u2 = u * u;
        let u3 = u * u2;
        let low = u2.mul_add(0.012500000186264515, 0.0833333358168602)
            .mul_add(u3, -correction);
        let high = f32::from_bits(r.to_bits() & 0xffff0000);
        let low = low + (r - high);
        let a = low * 0.43359375;
        let b = high * 0.0007007318781688809;
        let c = high * 0.43359375;
        return (low.mul_add(0.0007007318781688809, b) + a) + c;
    }
    let rounded = (bits & 0x7f0000) + ((bits & 0x8000) << 1);
    let i = (rounded >> 16) as usize;
    let r = (f32::from_bits(rounded | 0x3f000000)
        - f32::from_bits(mantissa | 0x3f000000)) * LOG_INV[i];
    let polynomial = r.mul_add(0.3333333432674408, 0.5);
    let r = polynomial.mul_add(r * r, r) * 0.4342944920063019;
    let low = 0.0002487456367816776f32.mul_add(exponent, -r) + LOG_LO[i];
    0.30078125f32.mul_add(exponent, LOG_HI[i]) + low
}

pub fn pow(x: f32, y: f32) -> f32 {
    if y == 0.0 || x == 1.0 { return 1.0; }
    if y == 1.0 { return x; }
    if x <= 0.0 || !x.is_finite() || !y.is_finite() || x.is_subnormal() {
        return x.powf(y);
    }
    let x = x as f64;
    let y = y as f64;
    let near = (x - 1.0).abs() < D_6732194;
    let log = if near {
        let r = x - 1.0;
        let u = r / (r + D_67321A4);
        let correction = u * r;
        let u = u + u;
        let u2 = u * u;
        let a = u2 * D_67321C0 + D_67321D0;
        let b = u2 * D_67321C8 + D_67321D8;
        let u3 = u * u2;
        let u7 = (u2 * u2) * u3;
        r + ((u7 * b + u3 * a) - correction)
    } else {
        let bits = x.to_bits();
        let mantissa = bits & 0x000fffffffffffff;
        let i = ((mantissa >> 44) + ((mantissa >> 43) & 1)) as usize;
        let rounded = f64::from_bits(((i as u64) | 0x3fe00) << 44);
        let m = f64::from_bits(mantissa | 0x3fe0000000000000);
        let r = (rounded - m) * POW_INV[i];
        let polynomial = D_6732164.mul_add(r, 0.5).mul_add(r, D_673219C);
        let e = ((bits >> 52) as i32 - 1023) as f64;
        (e * D_673216C + POW_LOG[i]) - r * polynomial
    };
    let z = y * log;
    if z > 88.72283935546875 { return f32::INFINITY; }
    if z <= -103.2789306640625 { return 0.0; }
    let n = (z * D_67321F0).round_ties_even() as i32;
    let r = if near { z - n as f64 * D_6732184 }
        else { (-(n as f64)).mul_add(D_6732184, z) };
    let p = if near {
        (r * r) * (D_673218C * r + 0.5) + r
    } else {
        r * D_673218C.mul_add(r, 0.5).mul_add(r, D_673219C)
    };
    let t = POW_EXP[(n & 63) as usize];
    let value = if near { p * t + t } else { p.mul_add(t, t) };
    f64::from_bits(value.to_bits().wrapping_add(((n >> 6) as i64 as u64) << 52)) as f32
}
