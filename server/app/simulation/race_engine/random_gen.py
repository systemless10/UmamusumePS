"""Bit-exact port of uma-skill-tools' Random.ts (Rule30CARng).

A custom 64-bit two-lane elementary-cellular-automaton PRNG (Wolfram Rule
30), NOT a standard algorithm (not Mersenne Twister / xorshift / PCG). Every
race result downstream of this class is a deterministic function of its
exact bit manipulation, so this must reproduce the JS implementation
bit-for-bit, not just "equivalent quality" -- see Random.ts's own comments
for why this specific (unusual) construction was chosen.

All JS bitwise operators (``<<`` ``>>>`` ``|`` ``&`` ``^``) implicitly
truncate to 32 bits; since Python ints don't do that, every left-shift
result is masked with ``& _MASK32`` immediately, and hi/lo are always kept
in the range [0, 0xFFFFFFFF] so plain Python ``>>`` behaves like JS ``>>>``.
"""

from __future__ import annotations

_MASK32 = 0xFFFFFFFF


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
        # `upper` is sometimes a non-integer float here (e.g. a region
        # length derived from course distances); JS's `|` operator
        # implicitly ToInt32-truncates (toward zero) before the bitwise op,
        # so replicate that explicitly -- Python raises on `float | int`.
        mask = _MASK32 >> _clz32(int(upper - 1) | 1)
        while True:
            n = self.int32() & mask
            # JS's `&` always yields a SIGNED int32, and `while (n >= upper)`
            # compares that signed value. This is only observable when `mask`
            # includes bit 31 -- the degenerate upper<=0 case (a region
            # exactly 10 units long feeding `uniform(length-10)==uniform(0)`
            # in ActivationSamplePolicy's AllCornerRandomPolicy, confirmed to
            # occur in real skill/course combinations). There, roughly half
            # of all draws are negative and exit the loop immediately; an
            # unsigned interpretation here would retry forever instead. For
            # every legitimate (upper>=1, small) call `mask` never reaches
            # bit 31, so this reinterpretation is a no-op there.
            if n >= 0x80000000:
                n -= 0x100000000
            if n < upper:
                return n
