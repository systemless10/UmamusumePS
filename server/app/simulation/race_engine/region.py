"""Port of Region.ts -- half-open interval [start, end) set algebra."""

from __future__ import annotations

from typing import Callable, Union


class Region:
    __slots__ = ("start", "end")

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end

    def intersect(self, other: "Region") -> "Region":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end <= start:
            return Region(-1, -1)
        return Region(start, end)

    def fully_contains(self, other: "Region") -> bool:
        return self.start <= other.start and self.end >= other.end

    def __repr__(self) -> str:
        return f"Region({self.start!r}, {self.end!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Region) and self.start == other.start and self.end == other.end


class RegionList(list):
    def rmap(self, f: Callable[["Region"], Union["Region", list["Region"]]]) -> "RegionList":
        out = RegionList()
        for r in self:
            newr = f(r)
            if isinstance(newr, list):
                for nr in newr:
                    if nr.start > -1:
                        out.append(nr)
            else:
                if newr.start > -1:
                    out.append(newr)
        return out

    def union(self, other: "RegionList") -> "RegionList":
        u = list(self) + list(other)
        r = RegionList()
        if len(u) == 0:
            return r
        u.sort(key=lambda x: x.start)

        acc = u[0]
        for b in u[1:]:
            a = acc
            if a.fully_contains(b):
                acc = a
            elif a.start <= b.start < a.end:
                acc = Region(a.start, b.end)
            elif a.start < b.end <= a.end:
                acc = Region(b.start, a.end)
            else:
                r.append(a)
                acc = b
        r.append(acc)
        return r
