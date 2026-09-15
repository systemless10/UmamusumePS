"""SECRET career events (#21) and their preconditions.

Every trainee's GameTora page carries a `secret` section: events that fire only
when a career-long condition holds -- winning a specific race in a specific
year, taking the Triple Crown, beating a rival. 546 of them across the cached
trainee pages, each with real choices and effects.

TWO WAYS TO GET THIS WRONG, and we have had both:

  * never serving them (the scraper used to discard the section label and the
    `conditions` field, so a secret event was indistinguishable from an
    ordinary one); and
  * serving them at RANDOM. A secret event's story id sits in the same
    master.mdb band as the trainee's ordinary random beats -- Mayano Top Gun's
    'The Fruits of My Labor' is 501024118, right next to her personal chain --
    so the random-event roll happily offered it on turn 9 to a player who had
    won nothing (live-reported 2026-09-06). single_mode_team's random pool now
    subtracts these story ids explicitly; this module owns the question of
    when one is actually DUE.

THE CONDITION LANGUAGE
----------------------
    conditions = [clause, clause, ...]        # ALL must hold
    clause     = [verb, arg, ...]

The verb vocabulary and every argument's meaning are GameTora's own, decoded
from the condition renderer + English locale table in its page bundle
(docs/'Mejiro McQueen (Summer) ... GameTora_files'/953-*.js -- the
`secret_condition` locale block and the `render` entry per verb). That is what
turned a pile of guessed tokens into a specification:

    grade code   100 G1 | 200 G2 | 300 G3 | 400 OP | 700 Pre-OP | "g" graded
    length code  0 Sprint | 1 Mile | 2 Medium | 3 Long | 4 Dirt
    strategy     1 Front Runner | 2 Pace Chaser | 3 Late Surger | 4 End Closer
    dist/terrain ("trn"|0, 1 turf / 2 dirt) or ("dist"|1, length code)
    race target  "101901|2" = race instance 101901 run in career year 2;
                 a bare 101901 means any year
    date         (year, month, half) -- half 1 Early, 2 Late

STILL DELIBERATELY CONSERVATIVE: `evaluate` returns None for any verb it does
not fully understand OR that the career data cannot answer (no simulation ran,
so no popularity/rival placements were recorded), and an event with even one
unknown clause NEVER fires. Showing a secret event the player did not earn is
exactly the bug this module exists to prevent.

NOTE CLAUSES (`obj`, `nc`, `gold_city_race`, `dist_wins_branch`,
`racetrack_wins_branch`, `3_crown_route`) are annotations GameTora renders
alongside the real conditions -- "the rewards depend on how many Long races you
won" is not a gate. They neither block nor satisfy: an event whose clauses are
ALL notes has no gate we can place it by, and stays unfired.
"""

from __future__ import annotations

import dataclasses
import logging
import re

log = logging.getLogger("uma-server")

# turn -> career year. 24 turns per year (Junior / Classic / Senior).
_TURNS_PER_YEAR = 24

# Sentinel for a clause that is an annotation rather than a gate (see above).
NOTE = "note"

# Fixed race sets, taken from GameTora's own renderer rather than guessed.
_TRIPLE_CROWN = (100501, 101001, 101501)          # Satsuki / Derby / Kikuka
_TRIPLE_TIARA = (100401, 100901, 101401)          # Oka Sho / Oaks / Shuka
_SPRING_TRIPLE_CROWN = ((100301, None), (100601, None), (101201, 3))
_AUTUMN_TRIPLE_CROWN = (101601, 101901, 102301)   # Tenno Sho (Autumn) / JC / Arima

_GRADED = (100, 200, 300)
_STRATEGIES = (1, 2, 3, 4)


def career_year(turn) -> int:
    try:
        turn = int(turn)
    except (TypeError, ValueError):
        return 0
    return max(1, min(3, (turn - 1) // _TURNS_PER_YEAR + 1))


def date_turn(year, month, half) -> int | None:
    """(Senior, Dec, Late) -> turn 72. The calendar the whole URA scenario uses:
    24 turns a year, two per month."""
    try:
        year, month, half = int(year), int(month), int(half)
    except (TypeError, ValueError):
        return None
    if not (1 <= year <= 3 and 1 <= month <= 12 and 1 <= half <= 2):
        return None
    return (year - 1) * _TURNS_PER_YEAR + (month - 1) * 2 + half


# ================================================================== facts ===

@dataclasses.dataclass
class Race:
    """One race the trainee actually ran. Everything a condition can ask about
    a race, resolved once by the caller (which owns the master.mdb joins)."""
    program_id: int = 0
    race_id: int = 0              # race_instance id -- what conditions name
    turn: int = 0
    rank: int = 99
    running_style: int = 0
    grade: int = 0                # 100 G1 / 200 G2 / 300 G3 / 400 OP / 700
    distance: int = 0
    ground: int = 0               # 1 turf, 2 dirt
    track_id: int = 0             # racetrack (race_course_set.race_track_id)
    objective: bool = False       # was this race one of the route's goals?
    popularity: int | None = None       # None when no simulation recorded one
    rival_ranks: dict | None = None     # {chara_id: finish order}, None = unknown

    @property
    def year(self) -> int:
        return career_year(self.turn)

    @property
    def won(self) -> bool:
        return self.rank == 1

    @property
    def graded(self) -> bool:
        return self.grade in _GRADED

    @property
    def length_types(self) -> set:
        """The length codes this race matches. A dirt race matches 4 AND its own
        distance bucket -- GameTora's list mixes the two axes (Smart Falcon's
        'win a Dirt G1 11 times' is code 4), and a 1200m dirt race genuinely is
        a sprint as well."""
        out = {4} if self.ground == 2 else set()
        d = self.distance or 0
        out.add(0 if d <= 1400 else 1 if d <= 1800 else 2 if d <= 2400 else 3)
        return out


@dataclasses.dataclass
class Context:
    """The career as the conditions see it."""
    races: list = dataclasses.field(default_factory=list)   # in run order
    turn: int = 0
    fans: int = 0
    goals_cleared: int = 0
    fired_story_ids: set = dataclasses.field(default_factory=set)
    # Fan Promises this career has FULFILLED (condition ids -- see
    # handlers.conditions.PROMISE_TRACKS). A promise is fulfilled by winning at
    # one of its racetracks while holding it, which is what `win_connect_live`
    # asks about.
    promises_fulfilled: set = dataclasses.field(default_factory=set)
    # The event currently being evaluated. Set by _satisfied, never by the
    # caller: `racetrack_wins_branch` reads its threshold out of the event's own
    # reward arms, which is the only clause that needs to see them.
    event: dict | None = None
    # (race_id, year) -> the last turn that race can still be entered. Supplied
    # by the caller (it needs master.mdb); without it the NEGATIVE conditions
    # below cannot be decided and their events stay unfired.
    race_deadline: object = None

    def missed(self, race_id, year) -> bool | None:
        """True once the race can no longer be run, False while it still can,
        None when we cannot tell."""
        if self.race_deadline is None:
            return None
        try:
            last = self.race_deadline(race_id, year)
        except Exception:                                    # noqa: BLE001
            log.exception("race deadline lookup failed for %s", race_id)
            return None
        return None if last is None else (self.turn or 0) > last

    # -- small derived helpers -------------------------------------------
    def matching(self, race_id, year=None, *, won=None):
        for r in self.races:
            if r.race_id != race_id:
                continue
            if year is not None and r.year != year:
                continue
            if won is not None and r.won != won:
                continue
            yield r

    def ran(self, race_id, year=None) -> bool:
        return any(True for _ in self.matching(race_id, year))

    def win(self, race_id, year=None) -> bool:
        return any(True for _ in self.matching(race_id, year, won=True))

    @property
    def wins(self) -> list:
        return [r for r in self.races if r.won]

    def g1_wins(self, year=None) -> list:
        return [r for r in self.wins
                if r.grade == 100 and (year is None or r.year == year)]

    @property
    def win_runs(self) -> list:
        """Maximal runs of consecutive won races -- what a 'win streak' is."""
        runs, cur = [], []
        for r in self.races:
            if r.won:
                cur.append(r)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        return runs


# =============================================================== matchers ===

def _target(token) -> tuple:
    """"101901|2" -> (101901, 2); 101901 -> (101901, None)."""
    if isinstance(token, bool):
        return None, None
    if isinstance(token, int):
        return token, None
    text = str(token)
    if "|" in text:
        left, _, right = text.partition("|")
        if left.isdigit() and right.isdigit():
            return int(left), int(right)
        return None, None
    return (int(text), None) if text.isdigit() else (None, None)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _grade_match(code, race: Race) -> bool | None:
    """GameTora's grade token: a number is race.grade, "g" means any graded."""
    if isinstance(code, str) and code.lower() == "g":
        return race.graded
    n = _int(code)
    return None if n is None else race.grade == n


def _dt_match(kind, value, race: Race) -> bool | None:
    """The `distOrTerrain` pair: ("trn"|0, turf/dirt) or ("dist"|1, length)."""
    v = _int(value)
    if v is None:
        return None
    if kind in ("trn", 0, "0"):
        return race.ground == v
    if kind in ("dist", 1, "1"):
        return v in race.length_types
    if kind in ("strat", 2, "2"):
        return race.running_style == v
    return None


def _count_ok(n, want) -> bool | None:
    """`count` args are 'at least', and a [lo, hi] pair is an inclusive band
    (Symboli Rudolf's three graduated variants are [0,6] / 7 / 8)."""
    if isinstance(want, (list, tuple)):
        if len(want) != 2:
            return None
        lo, hi = _int(want[0]), _int(want[1])
        return None if lo is None or hi is None else lo <= n <= hi
    c = _int(want)
    return None if c is None else n >= c


# ============================================================== the verbs ===
# Each returns True / False / None (cannot be evaluated) / NOTE.
_VERBS: dict = {}


def _verb(*names):
    def deco(fn):
        for n in names:
            _VERBS[n] = fn
        return fn
    return deco


# -- one named race ---------------------------------------------------------

@_verb("win")
def _v_win(c, ctx, _all):
    rid, yr = _target(c[1])
    return None if rid is None else ctx.win(rid, yr)


@_verb("lose")
def _v_lose(c, ctx, _all):
    rid, yr = _target(c[1])
    if rid is None:
        return None
    return any(True for _ in ctx.matching(rid, yr, won=False))


@_verb("do_not_win")
def _v_do_not_win(c, ctx, _all):
    """A negative is only EARNED once the race can no longer be run -- see
    Context.missed."""
    rid, yr = _target(c[1])
    if rid is None:
        return None
    if ctx.win(rid, yr):
        return False
    return ctx.missed(rid, yr)


@_verb("participate")
def _v_participate(c, ctx, _all):
    rid, yr = _target(c[1])
    return None if rid is None else ctx.ran(rid, yr)


@_verb("do_not_participate")
def _v_do_not_participate(c, ctx, _all):
    rid, yr = _target(c[1])
    if rid is None:
        return None
    if ctx.ran(rid, yr):
        return False
    return ctx.missed(rid, yr)


@_verb("race_w2")
def _v_race_w2(c, ctx, _all):
    """Win the race TWICE -- e.g. the Arima Kinen in both Classic and Senior."""
    rid, yr = _target(c[1])
    return None if rid is None else len(list(ctx.matching(rid, yr, won=True))) >= 2


@_verb("race_pn")
def _v_race_pn(c, ctx, _all):
    rid, yr = _target(c[1])
    pos = _int(c[2]) if len(c) > 2 else None
    if rid is None or pos is None:
        return None
    return any(r.rank <= pos for r in ctx.matching(rid, yr))


@_verb("win_or")
def _v_win_or(c, ctx, _all):
    if len(c) < 3:
        return None
    targets = [_target(t) for t in c[1:]]
    if any(rid is None for rid, _ in targets):
        return None
    return any(ctx.win(rid, yr) for rid, yr in targets)


@_verb("win_all")
def _v_win_all(c, ctx, _all):
    targets = [_target(t) for t in (c[1] if isinstance(c[1], list) else c[1:])]
    if not targets or any(rid is None for rid, _ in targets):
        return None
    return all(ctx.win(rid, yr) for rid, yr in targets)


@_verb("win_n_of")
def _v_win_n_of(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    if n is None or len(c) < 3 or not isinstance(c[2], list):
        return None
    targets = [_target(t) for t in c[2]]
    if any(rid is None for rid, _ in targets):
        return None
    return sum(1 for rid, yr in targets if ctx.win(rid, yr)) >= n


@_verb("pick_and_win")
def _v_pick_and_win(c, ctx, _all):
    """Pick the race as an OBJECTIVE and win it."""
    rid, yr = _target(c[1])
    if rid is None:
        return None
    return any(r.objective for r in ctx.matching(rid, yr, won=True))


@_verb("dont_pick_and_win")
def _v_dont_pick_and_win(c, ctx, _all):
    """Enter it as an OPTIONAL race and win it."""
    rid, yr = _target(c[1])
    if rid is None:
        return None
    return any(not r.objective for r in ctx.matching(rid, yr, won=True))


@_verb("win_as_strat")
def _v_win_as_strat(c, ctx, _all):
    rid, yr = _target(c[1])
    strat = _int(c[2]) if len(c) > 2 else None
    if rid is None or strat not in _STRATEGIES:
        return None
    return any(r.running_style == strat for r in ctx.matching(rid, yr, won=True))


@_verb("win_as_not_strat")
def _v_win_as_not_strat(c, ctx, _all):
    rid, yr = _target(c[1])
    strat = _int(c[2]) if len(c) > 2 else None
    if rid is None or strat not in _STRATEGIES:
        return None
    return any(r.running_style and r.running_style != strat
               for r in ctx.matching(rid, yr, won=True))


# -- tallies across the whole career ----------------------------------------

@_verb("win_g1")
def _v_win_g1(c, ctx, _all):
    return _count_ok(len(ctx.g1_wins()), c[1] if len(c) > 1 else 1)


@_verb("never_won_g1_before")
def _v_never_won_g1(c, ctx, _all):
    return not ctx.g1_wins()


@_verb("won_g1_before")
def _v_won_g1_before(c, ctx, _all):
    return bool(ctx.g1_wins())


@_verb("win_g1_year")
def _v_win_g1_year(c, ctx, _all):
    year = _int(c[1]) if len(c) > 1 else None
    if year is None:
        return None
    return _count_ok(len(ctx.g1_wins(year)), c[2] if len(c) > 2 else 1)


@_verb("win_g1_strat")
def _v_win_g1_strat(c, ctx, _all):
    strat = _int(c[1]) if len(c) > 1 else None
    if strat not in _STRATEGIES:
        return None
    n = sum(1 for r in ctx.g1_wins() if r.running_style == strat)
    return _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("win_g1_track")
def _v_win_g1_track(c, ctx, _all):
    track = _int(c[1]) if len(c) > 1 else None
    if track is None:
        return None
    n = sum(1 for r in ctx.g1_wins() if r.track_id == track)
    return _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("win_g1_length")
def _v_win_g1_length(c, ctx, _all):
    codes = c[1] if isinstance(c[1], list) else [c[1]]
    codes = [_int(x) for x in codes]
    if any(x is None for x in codes):
        return None
    n = sum(1 for r in ctx.g1_wins() if r.length_types & set(codes))
    return _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("win_g1_cnt_class_distance")
def _v_win_g1_ccd(c, ctx, _all):
    """"In the {class}, win at least {amount} {lengthType} G1 races" --
    cond[1] amount, cond[2] class/year, cond[3] length code(s)."""
    amount, year = _int(c[1]) if len(c) > 1 else None, _int(c[2]) if len(c) > 2 else None
    if amount is None or year is None or len(c) < 4:
        return None
    codes = c[3] if isinstance(c[3], list) else [c[3]]
    codes = [_int(x) for x in codes]
    if any(x is None for x in codes):
        return None
    n = sum(1 for r in ctx.g1_wins(year) if r.length_types & set(codes))
    return n >= amount


@_verb("gn_race_w")
def _v_gn_race_w(c, ctx, _all):
    hits = [_grade_match(c[1], r) for r in ctx.wins] if len(c) > 1 else [None]
    if any(h is None for h in hits):
        return None
    return _count_ok(sum(1 for h in hits if h), c[2] if len(c) > 2 else 1)


@_verb("y_gn_race_w")
def _v_y_gn_race_w(c, ctx, _all):
    year = _int(c[1]) if len(c) > 1 else None
    if year is None or len(c) < 3:
        return None
    wins = [r for r in ctx.wins if r.year == year]
    hits = [_grade_match(c[2], r) for r in wins]
    if any(h is None for h in hits):
        return None
    return _count_ok(sum(1 for h in hits if h), c[3] if len(c) > 3 else 1)


@_verb("gn_race_no_w")
def _v_gn_race_no_w(c, ctx, _all):
    """Win a NON-OBJECTIVE race of the given grade."""
    wins = [r for r in ctx.wins if not r.objective]
    hits = [_grade_match(c[1], r) for r in wins] if len(c) > 1 else [None]
    if any(h is None for h in hits):
        return None
    return _count_ok(sum(1 for h in hits if h), c[2] if len(c) > 2 else 1)


@_verb("dt_gn_race_w")
def _v_dt_gn_race_w(c, ctx, _all):
    if len(c) < 5:
        return None
    n = 0
    for r in ctx.wins:
        dt, g = _dt_match(c[1], c[2], r), _grade_match(c[3], r)
        if dt is None or g is None:
            return None
        n += bool(dt and g)
    return _count_ok(n, c[4])


@_verb("dt_gn_race_no_w")
def _v_dt_gn_race_no_w(c, ctx, _all):
    if len(c) < 5:
        return None
    n = 0
    for r in ctx.wins:
        if r.objective:
            continue
        dt, g = _dt_match(c[1], c[2], r), _grade_match(c[3], r)
        if dt is None or g is None:
            return None
        n += bool(dt and g)
    return _count_ok(n, c[4])


@_verb("y_dt_gn_race_no_w")
def _v_y_dt_gn_race_no_w(c, ctx, _all):
    year = _int(c[1]) if len(c) > 1 else None
    if year is None or len(c) < 6:
        return None
    n = 0
    for r in ctx.wins:
        if r.objective or r.year != year:
            continue
        dt, g = _dt_match(c[2], c[3], r), _grade_match(c[4], r)
        if dt is None or g is None:
            return None
        n += bool(dt and g)
    return _count_ok(n, c[5])


@_verb("dt_race_w")
def _v_dt_race_w(c, ctx, _all):
    if len(c) < 4:
        return None
    n = 0
    for r in ctx.wins:
        dt = _dt_match(c[1], c[2], r)
        if dt is None:
            return None
        n += bool(dt)
    return _count_ok(n, c[3])


@_verb("rt_race_w")
def _v_rt_race_w(c, ctx, _all):
    track = _int(c[1]) if len(c) > 1 else None
    if track is None:
        return None
    n = sum(1 for r in ctx.wins if r.track_id == track)
    return _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("gn_race_pn")
def _v_gn_race_pn(c, ctx, _all):
    pos = _int(c[2]) if len(c) > 2 else None
    if pos is None or len(c) < 2:
        return None
    n = 0
    for r in ctx.races:
        g = _grade_match(c[1], r)
        if g is None:
            return None
        n += bool(g and r.rank <= pos)
    return _count_ok(n, c[3] if len(c) > 3 else 1)


@_verb("any_race_pn")
def _v_any_race_pn(c, ctx, _all):
    pos = _int(c[1]) if len(c) > 1 else None
    if pos is None:
        return None
    return _count_ok(sum(1 for r in ctx.races if r.rank == pos),
                     c[2] if len(c) > 2 else 1)


@_verb("third_any_non_objective")
def _v_third_non_obj(c, ctx, _all):
    return any(r.rank == 3 and not r.objective for r in ctx.races)


@_verb("win_streak_graded")
def _v_win_streak_graded(c, ctx, _all):
    """"A win streak of N+ graded races." A streak is an unbroken run of wins;
    the requirement counts the GRADED races inside one such run."""
    n = _int(c[1]) if len(c) > 1 else None
    if n is None:
        return None
    return any(sum(1 for r in run if r.graded) >= n for run in ctx.win_runs)


@_verb("win_on_streak")
def _v_win_on_streak(c, ctx, all_clauses):
    """"After the win streak, WITHOUT BREAKING IT, also win X." Evaluated
    against the sibling win_streak_graded clause so the two must describe the
    SAME unbroken run -- a streak, then a loss, then this race is not it."""
    rid, yr = _target(c[1])
    if rid is None:
        return None
    need = 1
    for other in all_clauses or ():
        if isinstance(other, list) and other and str(other[0]) == "win_streak_graded":
            need = _int(other[1] if len(other) > 1 else 1, 1)
    for run in ctx.win_runs:
        if sum(1 for r in run if r.graded) < need:
            continue
        if any(r.race_id == rid and (yr is None or r.year == yr) for r in run):
            return True
    return False


# -- the fixed crowns -------------------------------------------------------

def _won_set(ctx, targets) -> bool:
    return all(ctx.win(rid, yr) for rid, yr in targets)


@_verb("triple_crown")
def _v_triple_crown(c, ctx, _all):
    return _won_set(ctx, [(r, None) for r in _TRIPLE_CROWN])


@_verb("triple_tiara")
def _v_triple_tiara(c, ctx, _all):
    return _won_set(ctx, [(r, None) for r in _TRIPLE_TIARA])


@_verb("spring_triple_crown")
def _v_spring_triple_crown(c, ctx, _all):
    return _won_set(ctx, _SPRING_TRIPLE_CROWN)


@_verb("autumn_triple_crown_senior")
def _v_autumn_senior(c, ctx, _all):
    return _won_set(ctx, [(r, 3) for r in _AUTUMN_TRIPLE_CROWN])


@_verb("autumn_triple_crown_same_year")
def _v_autumn_same_year(c, ctx, _all):
    return any(_won_set(ctx, [(r, y) for r in _AUTUMN_TRIPLE_CROWN])
               for y in (2, 3))


@_verb("brian_five")
def _v_brian_five(c, ctx, _all):
    """"OR win at least 5 G1 races before the third year" -- an ALTERNATIVE to
    the clause it accompanies, not another AND term. is_eligible() gives it
    that meaning; here it is just the alternative's own test."""
    return len([r for r in ctx.g1_wins() if r.year < 3]) >= 5


# -- calendar, fans, objectives, other events -------------------------------

@_verb("date")
def _v_date(c, ctx, _all):
    """"Triggers in {class}, {date}". Satisfied from that turn ON rather than
    exactly on it: the event is checked per command, and a check that happened
    to be missed on the one turn would otherwise lose the event for good."""
    if len(c) < 4:
        return None
    t = date_turn(c[1], c[2], c[3])
    return None if t is None else (ctx.turn or 0) >= t


@_verb("do_not_race")
def _v_do_not_race(c, ctx, _all):
    """"Don't race in {class}, {date}" -- only decidable once that turn is
    behind us."""
    if len(c) < 4:
        return None
    t = date_turn(c[1], c[2], c[3])
    if t is None:
        return None
    if (ctx.turn or 0) <= t:
        return False
    return not any(r.turn == t for r in ctx.races)


@_verb("fan")
def _v_fan(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    return None if n is None else (ctx.fans or 0) >= n


@_verb("fans_before_finals")
def _v_fans_before_finals(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    if n is None:
        return None
    # The finals start after the last calendar turn (Senior, Late December).
    return (ctx.fans or 0) >= n and (ctx.turn or 0) <= 3 * _TURNS_PER_YEAR


@_verb("clear_objective")
def _v_clear_objective(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    return None if n is None else (ctx.goals_cleared or 0) >= n


@_verb("ev")
def _v_ev(c, ctx, _all):
    """"Trigger the 「X」 training event" -- cond[1] is the story id."""
    sid = _int(c[1]) if len(c) > 1 else None
    return None if sid is None else sid in (ctx.fired_story_ids or ())


@_verb("pop")
def _v_pop(c, ctx, _all):
    """"Be the #N favorite for the CURRENT race" -- the race just run."""
    n = _int(c[1]) if len(c) > 1 else None
    if n is None or not ctx.races:
        return None if n is None else False
    pop = ctx.races[-1].popularity
    return None if pop is None else pop == n


@_verb("use_strategy")
def _v_use_strategy(c, ctx, _all):
    """"Use the {strategy} strategy" -- no race is named, so it reads against
    the current race, the same way `pop` does."""
    strat = _int(c[1]) if len(c) > 1 else None
    if strat not in _STRATEGIES:
        return None
    return bool(ctx.races) and ctx.races[-1].running_style == strat


# -- rivals (needs the simulated field's placements) ------------------------

def _rival_result(ctx, rid, yr, chara):
    """(player_rank, rival_rank) for the named race, or None when we cannot
    tell -- the race was not run, or it ran without a simulation so no rival
    placements were recorded."""
    for r in ctx.matching(rid, yr):
        if r.rival_ranks is None:
            return None
        rank = r.rival_ranks.get(chara, r.rival_ranks.get(str(chara)))
        if rank is None:
            continue
        return r.rank, int(rank)
    return None


@_verb("beat_rival")
def _v_beat_rival(c, ctx, _all):
    rid, yr = _target(c[1])
    chara = _int(c[2]) if len(c) > 2 else None
    if rid is None or chara is None:
        return None
    if not ctx.ran(rid, yr):
        return False
    res = _rival_result(ctx, rid, yr, chara)
    return None if res is None else res[0] < res[1]


@_verb("lose_to_rival")
def _v_lose_to_rival(c, ctx, _all):
    rid, yr = _target(c[1])
    chara = _int(c[2]) if len(c) > 2 else None
    if rid is None or chara is None:
        return None
    if not ctx.ran(rid, yr):
        return False
    res = _rival_result(ctx, rid, yr, chara)
    return None if res is None else res[0] > res[1]


@_verb("rival_draw")
def _v_rival_draw(c, ctx, _all):
    """"Both you and {char} LOSE the race" -- neither finished first."""
    rid, yr = _target(c[1])
    chara = _int(c[2]) if len(c) > 2 else None
    if rid is None or chara is None:
        return None
    if not ctx.ran(rid, yr):
        return False
    res = _rival_result(ctx, rid, yr, chara)
    return None if res is None else (res[0] != 1 and res[1] != 1)


def _beaten(ctx, charas, grade=None):
    """How many of `charas` the trainee has finished ahead of, counted once per
    character. None when any race in the career has no recorded field."""
    beaten = set()
    for r in ctx.races:
        if grade is not None:
            g = _grade_match(grade, r)
            if g is None:
                return None
            if not g:
                continue
        if r.rival_ranks is None:
            return None
        for chara in charas:
            rank = r.rival_ranks.get(chara, r.rival_ranks.get(str(chara)))
            if rank is not None and r.rank < int(rank):
                beaten.add(chara)
    return len(beaten)


@_verb("r_race_w")
def _v_r_race_w(c, ctx, _all):
    chara = _int(c[1]) if len(c) > 1 else None
    if chara is None:
        return None
    n = _beaten(ctx, [chara])
    return None if n is None else _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("rn_race_w")
def _v_rn_race_w(c, ctx, _all):
    if len(c) < 2 or not isinstance(c[1], list):
        return None
    charas = [_int(x) for x in c[1]]
    if any(x is None for x in charas):
        return None
    n = _beaten(ctx, charas)
    return None if n is None else _count_ok(n, c[2] if len(c) > 2 else 1)


@_verb("r_gn_race_w")
def _v_r_gn_race_w(c, ctx, _all):
    chara = _int(c[1]) if len(c) > 1 else None
    if chara is None or len(c) < 3:
        return None
    n = _beaten(ctx, [chara], grade=c[2])
    return None if n is None else _count_ok(n, c[3] if len(c) > 3 else 1)


@_verb("rn_gn_race_w")
def _v_rn_gn_race_w(c, ctx, _all):
    if len(c) < 3 or not isinstance(c[1], list):
        return None
    charas = [_int(x) for x in c[1]]
    if any(x is None for x in charas):
        return None
    n = _beaten(ctx, charas, grade=c[2])
    return None if n is None else _count_ok(n, c[3] if len(c) > 3 else 1)


@_verb("win_all_g1_rival")
def _v_win_all_g1_rival(c, ctx, _all):
    """Win every G1 the named rival also ran in (and have run at least one)."""
    chara = _int(c[1]) if len(c) > 1 else None
    if chara is None:
        return None
    seen = 0
    for r in ctx.races:
        if r.grade != 100:
            continue
        if r.rival_ranks is None:
            return None
        if r.rival_ranks.get(chara, r.rival_ranks.get(str(chara))) is None:
            continue
        seen += 1
        if not r.won:
            return False
    return seen > 0


# -- annotations, not gates -------------------------------------------------

@_verb("obj", "nc", "gold_city_race", "3_crown_route", "mile_route", "turf_only")
def _v_note(c, ctx, _all):
    return NOTE


# -- reward-branch events ---------------------------------------------------
# "The rewards will depend on the amount of Long/Kyoto wins" reads like a note,
# but it IS the gate: the reward table starts at a nonzero win count (Rice
# Shower's lowest arm is 2 Kyoto wins), so fewer than that and the event has no
# arm to pay. The threshold is not hardcoded -- it is the lowest band in the
# event's OWN segments, so a data refresh moves it.
#
# TIMING: these are career SUMMARIES, and the two other events of this family
# (Tokai Teio's 'The Tireless Wonder', Mejiro McQueen's 'Peerless Stayer') both
# carry an explicit `date 3,12,2` alongside the branch clause. Rice Shower's
# omits it, so the final calendar turn is applied here for all three -- firing
# the moment the count crosses 2 would pay the WEAKEST arm to a player still on
# their way to 5.

def _bands(event: dict) -> list:
    """The (low, high) win bands of the event's reward arms, from their `when`
    guards: "※ 2" -> (2, 2), "※ 3-4" -> (3, 4), "※ 5+" -> (5, None)."""
    out = []
    for choice in event.get("choices") or ():
        for seg in choice.get("segments") or ():
            for guard in seg.get("when") or ():
                if guard.get("kind") != "count":
                    continue
                band = _parse_band(guard.get("value"))
                if band:
                    out.append(band)
    return out


def _parse_band(text):
    m = re.search(r"(\d+)\s*(?:-\s*(\d+)|(\+))?", str(text or ""))
    if not m:
        return None
    low = int(m.group(1))
    if m.group(2):
        return low, int(m.group(2))
    return (low, None) if m.group(3) else (low, low)


def branch_count(event: dict, ctx: Context):
    """How many wins this event's reward branch is counted over, or None when
    the event has no branch clause."""
    for c in event.get("conditions") or ():
        if not (isinstance(c, list) and c):
            continue
        verb = str(c[0])
        if verb == "racetrack_wins_branch":
            track = _int(c[1]) if len(c) > 1 else None
            if track is None:
                return None
            return sum(1 for r in ctx.wins if r.track_id == track)
        if verb == "dist_wins_branch":
            code = _int(c[1]) if len(c) > 1 else None
            if code is None:
                return None
            return sum(1 for r in ctx.wins if code in r.length_types)
    return None


def branch_segment_index(event: dict, ctx: Context, choice: dict):
    """Which reward arm of `choice` this career has earned, or None. The arms
    are matched by their BAND, never by position -- Rice Shower lists hers
    5+/3-4/2 and the other two list theirs 2-3/4-5/6+."""
    n = branch_count(event, ctx)
    if n is None:
        return None
    for i, seg in enumerate(choice.get("segments") or ()):
        for guard in seg.get("when") or ():
            if guard.get("kind") != "count":
                continue
            band = _parse_band(guard.get("value"))
            if band and band[0] <= n and (band[1] is None or n <= band[1]):
                return i
    return None


def _v_wins_branch(c, ctx, all_clauses, count):
    low = min((b[0] for b in _bands(ctx.event or {})), default=None)
    if low is None:
        low = _int(c[2]) if len(c) > 2 else None      # e.g. dist_wins_branch 3 "2+"
        if low is None:
            return None
    return count >= low and (ctx.turn or 0) >= FINAL_TURN


@_verb("racetrack_wins_branch")
def _v_racetrack_wins_branch(c, ctx, all_clauses):
    track = _int(c[1]) if len(c) > 1 else None
    if track is None:
        return None
    return _v_wins_branch(c, ctx, all_clauses,
                          sum(1 for r in ctx.wins if r.track_id == track))


@_verb("dist_wins_branch")
def _v_dist_wins_branch(c, ctx, all_clauses):
    code = _int(c[1]) if len(c) > 1 else None
    if code is None:
        return None
    return _v_wins_branch(c, ctx, all_clauses,
                          sum(1 for r in ctx.wins if code in r.length_types))


@_verb("win_connect_live")
def _v_win_connect_live(c, ctx, _all):
    """"Finish your Fan Promise from the previous event." The promise is a
    real condition on the trainee (handlers.conditions.PROMISE_TRACKS) and is
    fulfilled by winning at one of its racetracks."""
    return bool(ctx.promises_fulfilled)


# `ct` is GameTora's escape hatch: a condition it never formalized, carried as
# an English sentence. Each entry here TRANSLATES one such sentence into real
# clauses -- it does not interpret it at runtime, so an unlisted sentence is
# still unknown and its event still never fires. Only add a sentence whose
# reading is unambiguous given the data we record.
#
#   Winning Ticket's 'Temporary Truce': the other two thirds of the BNW trio,
#   Biwa Hayahide (1023) and Narita Taishin (1050). Winning a race they ran in
#   IS finishing ahead of them, which is what rn_race_w tests. The count is the
#   one judgement call -- the sentence's plural 'races' gives no number, so it
#   takes the one the rest of the vocabulary defaults to.
_CT_TRANSLATIONS = {
    "win races where biwa hayahide or narita taishin run":
        [["rn_race_w", [1023, 1050], 1]],
}


@_verb("ct")
def _v_ct(c, ctx, all_clauses):
    text = str(c[1]).strip().lower() if len(c) > 1 else ""
    clauses = _CT_TRANSLATIONS.get(text)
    if not clauses:
        return None
    values = [evaluate_in(cl, ctx, all_clauses) for cl in clauses]
    if any(v is None for v in values):
        return None
    return all(v is NOTE or v for v in values)


# Clauses that are an OR-ALTERNATIVE to the rest of the list rather than
# another AND term ("Or win at least 5 G1 races before the third year").
_ALTERNATIVES = frozenset({"brian_five"})


# ============================================================ the verdict ===

def evaluate(clause, ctx: Context):
    """True / False / NOTE / None (verb unknown or unanswerable -- never fire)."""
    if not isinstance(clause, list) or not clause:
        return None
    return evaluate_in(clause, ctx, [clause])


def evaluate_in(clause, ctx: Context, all_clauses):
    if not isinstance(clause, list) or not clause:
        return None
    fn = _VERBS.get(str(clause[0]))
    if fn is None:
        return None
    try:
        return fn(clause, ctx, all_clauses)
    except Exception:                                        # noqa: BLE001
        log.exception("secret condition %r failed to evaluate", clause)
        return None


FINAL_TURN = 3 * _TURNS_PER_YEAR      # Senior, Late December -- the last turn


def is_eligible(event: dict, ctx: Context) -> bool:
    """True only when every gating clause is understood AND satisfied.

    An event with no conditions, or with nothing but note clauses, has no gate
    we can place it by and is never fired -- that is the whole difference
    between a secret event and a random one."""
    if not _satisfied(event, ctx):
        return False
    # A gate an UNTOUCHED career already satisfies is not an achievement gate:
    # Symboli Rudolf's 'Challenge Met' asks for 0-6 G1 wins, which is already
    # true before she has raced at all. Those are career-SUMMARY variants --
    # one event with several outcomes -- and belong on the last calendar turn,
    # not on turn 1. Everything with a real gate is unaffected, the negatives
    # included: 'do not run the Tenno Sho' is not satisfied until the Tenno
    # Sho has gone by (Context.missed).
    if _vacuous(event, ctx) and (ctx.turn or 0) < FINAL_TURN:
        return False
    return True


def _satisfied(event: dict, ctx: Context) -> bool:
    clauses = event.get("conditions") or []
    if not clauses:
        return False
    ctx = dataclasses.replace(ctx, event=event)
    required, alternatives, gates = [], [], 0
    for clause in clauses:
        verb = str(clause[0]) if isinstance(clause, list) and clause else None
        value = evaluate_in(clause, ctx, clauses)
        if value is NOTE:
            continue
        if value is None:
            return False                     # not understood -> never fire
        gates += 1
        (alternatives if verb in _ALTERNATIVES else required).append(value)
    if not gates:
        return False
    if alternatives:
        return (bool(required) and all(required)) or any(alternatives)
    return all(required)


def _vacuous(event: dict, ctx: Context) -> bool:
    """Would a career that has done NOTHING satisfy this gate? Evaluated on
    turn 1 with an empty history, keeping the caller's own lookups so the two
    verdicts are comparable."""
    blank = dataclasses.replace(ctx, races=[], turn=1, fans=0, goals_cleared=0,
                                fired_story_ids=set())
    return _satisfied(event, blank)


def unsupported_verbs(event: dict) -> list:
    """The verbs in this event we cannot evaluate at all -- for coverage
    reporting, which must not be confused with 'the player hasn't earned it'."""
    return sorted({str(c[0]) for c in (event.get("conditions") or [])
                   if isinstance(c, list) and c and str(c[0]) not in _VERBS})


def understood(event: dict) -> bool:
    """Whether the event has at least one gating clause and no unknown verb."""
    clauses = event.get("conditions") or []
    if not clauses or unsupported_verbs(event):
        return False
    return any(isinstance(c, list) and c and _VERBS.get(str(c[0])) is not _v_note
               for c in clauses)


def win_g1_threshold(event: dict):
    """The `win_g1` count this event is the variant for, or None.

    Trainees with graduated win_g1 variants (Symboli Rudolf: 0-6 / 7 / 8 G1
    wins) have several that are simultaneously true once the top one is,
    because a count is 'at least'. The caller uses this to serve only the
    strongest."""
    for c in event.get("conditions") or ():
        if isinstance(c, list) and c and str(c[0]) == "win_g1" and len(c) > 1:
            arg = c[1]
            if isinstance(arg, (list, tuple)) and len(arg) == 2:
                return _int(arg[0])
            return _int(arg)
    return None
