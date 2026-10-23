
from __future__ import annotations

import struct

_MBIG = 2147483647
_MSEED = 161803398
_F = struct.Struct('<f')


def f32(x: float) -> float:
    return _F.unpack(_F.pack(x))[0]


def _i32(x: int) -> int:
    return (x + 2**31) % 2**32 - 2**31


class NetRandom:

    __slots__ = ('_sa', '_inext', '_inextp', 'calls')

    def __init__(self, seed: int):
        seed = _i32(int(seed))
        subtraction = _MBIG if seed == -2147483648 else abs(seed)
        mj = _MSEED - subtraction
        sa = [0] * 56
        sa[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            sa[ii] = mk
            mk = mj - mk
            if mk < 0:
                mk += _MBIG
            mj = sa[ii]
        for _ in range(4):
            for i in range(1, 56):
                sa[i] = _i32(sa[i] - sa[1 + (i + 30) % 55])
                if sa[i] < 0:
                    sa[i] += _MBIG
        self._sa = sa
        self._inext = 0
        self._inextp = 21
        self.calls = 0

    def _internal_sample(self) -> int:
        self.calls += 1
        inext = self._inext + 1
        if inext >= 56:
            inext = 1
        inextp = self._inextp + 1
        if inextp >= 56:
            inextp = 1
        ret = self._sa[inext] - self._sa[inextp]
        if ret == _MBIG:
            ret -= 1
        if ret < 0:
            ret += _MBIG
        self._sa[inext] = ret
        self._inext = inext
        self._inextp = inextp
        return ret

    def next_double(self) -> float:
        return self._internal_sample() * (1.0 / _MBIG)

    def next_range(self, lo: int, hi: int) -> int:
        if type(lo) is not int or type(hi) is not int:
            raise TypeError('Random.Next bounds must be integers')
        if not -2147483648 <= lo <= 2147483647 or not -2147483648 <= hi <= 2147483647:
            raise ValueError('Random.Next bounds must fit int32')
        if lo > hi:
            raise ValueError('Random.Next minimum exceeds maximum')
        rng = hi - lo
        if rng <= _MBIG:
            return int(self.next_double() * rng) + lo
        value = self._internal_sample()
        if self._internal_sample() % 2 == 0:
            value = -value
        sample = (float(value) + (_MBIG - 1)) / (2 * _MBIG - 1)
        return int(sample * rng) + lo


class RaceRandom:

    def __init__(self, seed: int, trace: list | None = None):
        self._r = NetRandom(seed)
        self.trace = trace

    @property
    def calls(self) -> int:
        return self._r.calls

    def _log(self, kind, args, value, tag, first_call=None):
        if self.trace is not None:
            self.trace.append((self._r.calls - 1 if first_call is None else first_call,
                               kind, args, value, tag))
        return value

    def int_max(self, mx: int, tag: str = '') -> int:
        first = self.calls
        return self._log('int', (mx,), self._r.next_range(0, mx), tag, first)

    def int_range(self, lo: int, hi: int, tag: str = '') -> int:
        first = self.calls
        return self._log('int', (lo, hi), self._r.next_range(lo, hi), tag, first)

    def float_max(self, mx: float, tag: str = '') -> float:
        mx = f32(mx)
        v = f32(f32(f32(self._r.next_double()) * f32(abs(mx))) + 0.0)
        v = 0.0 if v < 0.0 else (mx if mx < v else v)
        return self._log('float', (mx,), v, tag)

    def float_range(self, lo: float, hi: float, tag: str = '') -> float:
        lo, hi = f32(lo), f32(hi)
        v = f32(f32(f32(self._r.next_double()) * f32(abs(f32(hi - lo)))) + lo)
        v = lo if v < lo else (hi if hi < v else v)
        return self._log('float', (lo, hi), v, tag)
