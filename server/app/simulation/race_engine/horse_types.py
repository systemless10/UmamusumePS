"""Port of HorseTypes.ts."""

from __future__ import annotations

from enum import IntEnum


class Strategy(IntEnum):
    NIGE = 1
    SENKOU = 2
    SASI = 3
    OIKOMI = 4
    OONIGE = 5


class Aptitude(IntEnum):
    S = 0
    A = 1
    B = 2
    C = 3
    D = 4
    E = 5
    F = 6
    G = 7


class HorseParameters:
    """Mutable: RaceSolver clones this per-solver and green (stat-boost)
    skills mutate it in place mid-race, exactly like the TS `horse` field."""

    __slots__ = (
        "speed", "stamina", "power", "guts", "wisdom", "strategy",
        "distance_aptitude", "surface_aptitude", "strategy_aptitude", "raw_stamina",
    )

    def __init__(self, speed, stamina, power, guts, wisdom, strategy,
                 distance_aptitude, surface_aptitude, strategy_aptitude, raw_stamina):
        self.speed = speed
        self.stamina = stamina
        self.power = power
        self.guts = guts
        self.wisdom = wisdom
        self.strategy = strategy
        self.distance_aptitude = distance_aptitude
        self.surface_aptitude = surface_aptitude
        self.strategy_aptitude = strategy_aptitude
        self.raw_stamina = raw_stamina

    def clone(self) -> "HorseParameters":
        return HorseParameters(
            self.speed, self.stamina, self.power, self.guts, self.wisdom, self.strategy,
            self.distance_aptitude, self.surface_aptitude, self.strategy_aptitude, self.raw_stamina,
        )


def strategy_matches(s1: int, s2: int) -> bool:
    return s1 == s2 or (s1 == Strategy.NIGE and s2 == Strategy.OONIGE) or (s1 == Strategy.OONIGE and s2 == Strategy.NIGE)


def parse_strategy(s) -> Strategy:
    if not isinstance(s, str):
        return Strategy(s)
    u = s.upper()
    if u == "NIGE":
        return Strategy.NIGE
    if u == "SENKOU":
        return Strategy.SENKOU
    if u in ("SASI", "SASHI"):
        return Strategy.SASI
    if u == "OIKOMI":
        return Strategy.OIKOMI
    if u == "OONIGE":
        return Strategy.OONIGE
    raise ValueError("Invalid running strategy.")


def parse_aptitude(a, kind: str) -> Aptitude:
    if not isinstance(a, str):
        return Aptitude(a)
    u = a.upper()
    if u in Aptitude.__members__:
        return Aptitude[u]
    raise ValueError(f"Invalid {kind} aptitude.")
