
from __future__ import annotations

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _clz32(x: int) -> int:
    x &= _MASK32
    if x == 0:
        return 32
    return 32 - x.bit_length()


class Rule30CARng:
    __slots__ = ("hi", "lo")

    def __init__(self, seed_lo: int, seed_hi: int = 0):
        self.hi = seed_hi & _MASK32
        self.lo = seed_lo & _MASK32

    @classmethod
    def seeded(cls, seed: int) -> "Rule30CARng":
        z = (seed + 0x9E3779B97F4A7C15) & _MASK64
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        z ^= z >> 31
        return cls(z & _MASK32, z >> 32)

    def step(self) -> None:
        hi, lo = self.hi, self.lo
        rot = hi >> 31
        rolhi = ((hi << 1) & _MASK32) | (lo >> 31)
        rollo = ((lo << 1) & _MASK32) | rot
        rot = (hi << 31) & _MASK32
        rorhi = (hi >> 1) | ((lo << 31) & _MASK32)
        rorlo = (lo >> 1) | rot

        self.hi = (rorhi ^ (hi | rolhi)) & _MASK32
        self.lo = (rorlo ^ (lo | rollo)) & _MASK32

    def pair(self) -> tuple[int, int]:
        x = 0
        y = 0
        for _ in range(16):
            x = ((x << 2) & _MASK32) | ((self.hi & 0x10000) >> 15) | (self.hi & 1)
            y = ((y << 2) & _MASK32) | ((self.hi & 0x1000000) >> 23) | ((self.hi & 0x100) >> 8)
            self.step()
        return x, y

    def int32(self) -> int:
        x, _y = self.pair()
        return x

    def random(self) -> float:
        mask_hi = 0x03FFFFFF
        mask_lo = 0x07FFFFFF
        exp = 0x8000000
        mant = 0x20000000000000
        hi, lo = self.pair()
        return ((hi & mask_hi) * exp + (lo & mask_lo)) / mant

    def uniform(self, upper) -> int:
        mask = _MASK32 >> _clz32(int(upper - 1) | 1)
        while True:
            n = self.int32() & mask
            if n >= 0x80000000:
                n -= 0x100000000
            if n < upper:
                return n
