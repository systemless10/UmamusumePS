"""Port of ActivationSamplePolicy.ts -- turns a RegionList (where a skill's
condition can statically hold) into one concrete trigger Region per race
sample, plus the double-dispatch `reconcile*` methods used when an
AndOperator combines two conditions with different sample policies."""

from __future__ import annotations

import math
from typing import Protocol

from .random_gen import Rule30CARng
from .region import Region, RegionList


class ActivationSamplePolicy(Protocol):
    def sample(self, regions: RegionList, nsamples: int, rng: Rule30CARng) -> list[Region]: ...
    def reconcile(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...
    def reconcile_immediate(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...
    def reconcile_distribution_random(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...
    def reconcile_random(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...
    def reconcile_straight_random(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...
    def reconcile_all_corner_random(self, other: "ActivationSamplePolicy") -> "ActivationSamplePolicy": ...


class _ImmediatePolicy:
    def sample(self, regions, nsamples, rng):
        return list(regions[:1])

    def reconcile(self, other):
        return other.reconcile_immediate(self)

    def reconcile_immediate(self, other):
        return other

    def reconcile_distribution_random(self, other):
        return other

    def reconcile_random(self, other):
        return other

    def reconcile_straight_random(self, other):
        return other

    def reconcile_all_corner_random(self, other):
        return other


ImmediatePolicy = _ImmediatePolicy()


class _RandomPolicy:
    def sample(self, regions, nsamples, rng):
        if len(regions) == 0:
            return []
        acc = 0
        weights = []
        for r in regions:
            acc += r.end - r.start
            weights.append(acc)
        samples = []
        for _ in range(nsamples):
            threshold = rng.uniform(acc)
            region = next(r for i, r in enumerate(regions) if weights[i] > threshold)
            samples.append(region.start + rng.uniform(region.end - region.start - 10))
        return [Region(pos, pos + 10) for pos in samples]

    def reconcile(self, other):
        return other.reconcile_random(self)

    def reconcile_immediate(self, other):
        return self

    def reconcile_distribution_random(self, other):
        return self

    def reconcile_random(self, other):
        return other

    def reconcile_straight_random(self, other):
        return other

    def reconcile_all_corner_random(self, other):
        return other


RandomPolicy = _RandomPolicy()


class DistributionRandomPolicy:
    def distribution(self, upper: float, nsamples: int, rng: Rule30CARng) -> list[float]:
        raise NotImplementedError

    def sample(self, regions, nsamples, rng):
        if len(regions) == 0:
            return []
        rng_total = sum(r.end - r.start for r in regions)
        rs = sorted(regions, key=lambda r: r.start)
        randoms = self.distribution(rng_total, nsamples, rng)
        samples = []
        for i in range(nsamples):
            pos = randoms[i]
            j = 0
            while True:
                pos += rs[j].start
                if pos > rs[j].end:
                    pos -= rs[j].end
                    j += 1
                else:
                    samples.append(Region(pos, rs[j].end))
                    break
        return samples

    def reconcile(self, other):
        return other.reconcile_distribution_random(self)

    def reconcile_immediate(self, other):
        return self

    def reconcile_distribution_random(self, other):
        return self

    def reconcile_random(self, other):
        return other

    def reconcile_straight_random(self, other):
        return other

    def reconcile_all_corner_random(self, other):
        return other


class UniformRandomPolicy(DistributionRandomPolicy):
    def distribution(self, upper, nsamples, rng):
        return [rng.uniform(upper) for _ in range(nsamples)]


class LogNormalRandomPolicy(DistributionRandomPolicy):
    def __init__(self, mu: float, sigma: float):
        self.mu = mu
        self.sigma = sigma

    def distribution(self, upper, nsamples, rng):
        nums: list[float] = []
        halfn = math.ceil(nsamples / 2)
        for _ in range(halfn):
            while True:
                x = rng.random() * 2.0 - 1.0
                y = rng.random() * 2.0 - 1.0
                r2 = x * x + y * y
                if r2 != 0.0 and r2 < 1.0:
                    break
            m = math.sqrt(-2.0 * math.log(r2) / r2) * self.sigma
            a = math.exp(x * m + self.mu)
            b = math.exp(y * m + self.mu)
            nums.append(a)
            nums.append(b)
        min_v = math.exp(self.mu + self.sigma * -3.09023)
        max_v = math.exp(self.mu + self.sigma * 3.09023)
        rng_range = max_v - min_v
        return [math.floor(upper * min(max(n - min_v, 0) / rng_range, 1.0)) for n in nums]


class ErlangRandomPolicy(DistributionRandomPolicy):
    def __init__(self, k: int, lam: float):
        self.k = k
        self.lam = lam

    def distribution(self, upper, nsamples, rng):
        nums = []
        for _ in range(nsamples):
            u = 1.0
            for _ in range(self.k):
                u *= rng.random()
            n = -math.log(u) / self.lam
            nums.append(n)
        min_v = self.k * math.pow(1 - 1 / (9 * self.k) + -3.09023 * math.sqrt(1 / (9 * self.k)), 3) / self.lam
        max_v = self.k * math.pow(1 - 1 / (9 * self.k) + 3.09023 * math.sqrt(1 / (9 * self.k)), 3) / self.lam
        rng_range = max_v - min_v
        return [math.floor(upper * min(max(n - min_v, 0) / rng_range, 1.0)) for n in nums]


class _StraightRandomPolicy:
    def sample(self, regions, nsamples, rng):
        if len(regions) == 0:
            return []
        samples = []
        for _ in range(nsamples):
            r = regions[rng.uniform(len(regions))]
            samples.append(r.start + rng.uniform(r.end - r.start - 10))
        return [Region(pos, pos + 10) for pos in samples]

    def reconcile(self, other):
        return other.reconcile_straight_random(self)

    def reconcile_immediate(self, other):
        return self

    def reconcile_distribution_random(self, other):
        return self

    def reconcile_random(self, other):
        return self

    def reconcile_straight_random(self, other):
        return other

    def reconcile_all_corner_random(self, other):
        raise ValueError("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy")


StraightRandomPolicy = _StraightRandomPolicy()


class _AllCornerRandomPolicy:
    def place_triggers(self, regions: RegionList, rng: Rule30CARng) -> Region:
        triggers: list[float] = []
        candidates = sorted(regions, key=lambda r: r.start)
        while len(triggers) < 4 and len(candidates) > 0:
            ci = rng.uniform(len(candidates))
            c = candidates[ci]
            start = c.start + rng.uniform(c.end - c.start - 10)
            if start + 20 <= c.end:
                candidates[ci:ci + 1] = [Region(start + 10, c.end)]
            else:
                del candidates[ci]
            del candidates[0:ci]
            triggers.append(start)
        return Region(triggers[0], triggers[0] + 10)

    def sample(self, regions, nsamples, rng):
        return [self.place_triggers(regions, rng) for _ in range(nsamples)]

    def reconcile(self, other):
        return other.reconcile_all_corner_random(self)

    def reconcile_immediate(self, other):
        return self

    def reconcile_distribution_random(self, other):
        return self

    def reconcile_random(self, other):
        return self

    def reconcile_straight_random(self, other):
        raise ValueError("cannot reconcile StraightRandomPolicy with AllCornerRandomPolicy")

    def reconcile_all_corner_random(self, other):
        return self


AllCornerRandomPolicy = _AllCornerRandomPolicy()
