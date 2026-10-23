
from __future__ import annotations

import math

import numpy as np

F = np.float32
ZERO = F(0.0)
ONE = F(1.0)


def powf(x, y) -> np.float32:
    return F(math.pow(float(x), float(y)))


def log10f(x) -> np.float32:
    return F(math.log10(float(x)))


def sqrtf(x) -> np.float32:
    return np.sqrt(F(x))


def clampf(v, lo, hi) -> np.float32:
    if v < lo:
        return F(lo)
    if v > hi:
        return F(hi)
    return F(v)


_EPS8 = F(1.401298464324817e-45) * F(8.0)


def approximately(a, b) -> bool:
    a = F(a)
    b = F(b)
    return max(F(max(abs(a), abs(b)) * F(1e-06)), _EPS8) > abs(F(b - a))


def floor_to_int(x) -> int:
    return int(math.floor(float(x)))
