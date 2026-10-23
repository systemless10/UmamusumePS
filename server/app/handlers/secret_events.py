
from __future__ import annotations

import dataclasses
import logging
import re

log = logging.getLogger("uma-server")

_TURNS_PER_YEAR = 24

NOTE = "note"

_TRIPLE_CROWN = (100501, 101001, 101501)
_TRIPLE_TIARA = (100401, 100901, 101401)
_SPRING_TRIPLE_CROWN = ((100301, None), (100601, None), (101201, 3))
_AUTUMN_TRIPLE_CROWN = (101601, 101901, 102301)

_GRADED = (100, 200, 300)
_STRATEGIES = (1, 2, 3, 4)


def career_year(turn) -> int:
    try:
        turn = int(turn)
    except (TypeError, ValueError):
        return 0
    return max(1, min(3, (turn - 1) // _TURNS_PER_YEAR + 1))


def date_turn(year, month, half) -> int | None:
    try:
        year, month, half = int(year), int(month), int(half)
    except (TypeError, ValueError):
        return None
    if not (1 <= year <= 3 and 1 <= month <= 12 and 1 <= half <= 2):
        return None
    return (year - 1) * _TURNS_PER_YEAR + (month - 1) * 2 + half


@dataclasses.dataclass
class Race:
    program_id: int = 0
    race_id: int = 0
    turn: int = 0
    rank: int = 99
    running_style: int = 0
    grade: int = 0
    distance: int = 0
    ground: int = 0
    track_id: int = 0
    objective: bool = False
    popularity: int | None = None
    rival_ranks: dict | None = None

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
        out = {4} if self.ground == 2 else set()
        d = self.distance or 0
        out.add(0 if d <= 1400 else 1 if d <= 1800 else 2 if d <= 2400 else 3)
        return out


@dataclasses.dataclass
class Context:
    races: list = dataclasses.field(default_factory=list)
    turn: int = 0
    fans: int = 0
    goals_cleared: int = 0
    fired_story_ids: set = dataclasses.field(default_factory=set)
    promises_fulfilled: set = dataclasses.field(default_factory=set)
    event: dict | None = None
    race_deadline: object = None

    def missed(self, race_id, year) -> bool | None:
        if self.race_deadline is None:
            return None
        try:
            last = self.race_deadline(race_id, year)
        except Exception:
            log.exception("race deadline lookup failed for %s", race_id)
            return None
        return None if last is None else (self.turn or 0) > last

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


def _target(token) -> tuple:
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
    if isinstance(code, str) and code.lower() == "g":
        return race.graded
    n = _int(code)
    return None if n is None else race.grade == n


def _dt_match(kind, value, race: Race) -> bool | None:
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
    if isinstance(want, (list, tuple)):
        if len(want) != 2:
            return None
        lo, hi = _int(want[0]), _int(want[1])
        return None if lo is None or hi is None else lo <= n <= hi
    c = _int(want)
    return None if c is None else n >= c


_VERBS: dict = {}


def _verb(*names):
    def deco(fn):
        for n in names:
            _VERBS[n] = fn
        return fn
    return deco


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
    rid, yr = _target(c[1])
    if rid is None:
        return None
    return any(r.objective for r in ctx.matching(rid, yr, won=True))


@_verb("dont_pick_and_win")
def _v_dont_pick_and_win(c, ctx, _all):
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
    n = _int(c[1]) if len(c) > 1 else None
    if n is None:
        return None
    return any(sum(1 for r in run if r.graded) >= n for run in ctx.win_runs)


@_verb("win_on_streak")
def _v_win_on_streak(c, ctx, all_clauses):
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
    return len([r for r in ctx.g1_wins() if r.year < 3]) >= 5


@_verb("date")
def _v_date(c, ctx, _all):
    if len(c) < 4:
        return None
    t = date_turn(c[1], c[2], c[3])
    return None if t is None else (ctx.turn or 0) >= t


@_verb("do_not_race")
def _v_do_not_race(c, ctx, _all):
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
    return (ctx.fans or 0) >= n and (ctx.turn or 0) <= 3 * _TURNS_PER_YEAR


@_verb("clear_objective")
def _v_clear_objective(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    return None if n is None else (ctx.goals_cleared or 0) >= n


@_verb("ev")
def _v_ev(c, ctx, _all):
    sid = _int(c[1]) if len(c) > 1 else None
    return None if sid is None else sid in (ctx.fired_story_ids or ())


@_verb("pop")
def _v_pop(c, ctx, _all):
    n = _int(c[1]) if len(c) > 1 else None
    if n is None or not ctx.races:
        return None if n is None else False
    pop = ctx.races[-1].popularity
    return None if pop is None else pop == n


@_verb("use_strategy")
def _v_use_strategy(c, ctx, _all):
    strat = _int(c[1]) if len(c) > 1 else None
    if strat not in _STRATEGIES:
        return None
    return bool(ctx.races) and ctx.races[-1].running_style == strat


def _rival_result(ctx, rid, yr, chara):
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
    rid, yr = _target(c[1])
    chara = _int(c[2]) if len(c) > 2 else None
    if rid is None or chara is None:
        return None
    if not ctx.ran(rid, yr):
        return False
    res = _rival_result(ctx, rid, yr, chara)
    return None if res is None else (res[0] != 1 and res[1] != 1)


def _beaten(ctx, charas, grade=None):
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


@_verb("obj", "nc", "gold_city_race", "3_crown_route", "mile_route", "turf_only")
def _v_note(c, ctx, _all):
    return NOTE


def _bands(event: dict) -> list:
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
        low = _int(c[2]) if len(c) > 2 else None
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
    return bool(ctx.promises_fulfilled)


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


_ALTERNATIVES = frozenset({"brian_five"})


def evaluate(clause, ctx: Context):
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
    except Exception:
        log.exception("secret condition %r failed to evaluate", clause)
        return None


FINAL_TURN = 3 * _TURNS_PER_YEAR


def is_eligible(event: dict, ctx: Context) -> bool:
    if not _satisfied(event, ctx):
        return False
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
            return False
        gates += 1
        (alternatives if verb in _ALTERNATIVES else required).append(value)
    if not gates:
        return False
    if alternatives:
        return (bool(required) and all(required)) or any(alternatives)
    return all(required)


def _vacuous(event: dict, ctx: Context) -> bool:
    blank = dataclasses.replace(ctx, races=[], turn=1, fans=0, goals_cleared=0,
                                fired_story_ids=set())
    return _satisfied(event, blank)


def unsupported_verbs(event: dict) -> list:
    return sorted({str(c[0]) for c in (event.get("conditions") or [])
                   if isinstance(c, list) and c and str(c[0]) not in _VERBS})


def understood(event: dict) -> bool:
    clauses = event.get("conditions") or []
    if not clauses or unsupported_verbs(event):
        return False
    return any(isinstance(c, list) and c and _VERBS.get(str(c[0])) is not _v_note
               for c in clauses)


def win_g1_threshold(event: dict):
    for c in event.get("conditions") or ():
        if isinstance(c, list) and c and str(c[0]) == "win_g1" and len(c) > 1:
            arg = c[1]
            if isinstance(arg, (list, tuple)) and len(arg) == 2:
                return _int(arg[0])
            return _int(arg)
    return None
