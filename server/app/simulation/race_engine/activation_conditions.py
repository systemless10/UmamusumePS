"""Port of ActivationConditions.ts -- the skill trigger-condition table.

Two-phase system per condition: a static `filter_*` method restricts a
RegionList to where the condition could possibly hold (computed once at
build time), optionally paired with a `DynamicCondition` (a per-tick
predicate over `RaceState`, checked once `pos` enters the statically
filtered trigger region). See ConditionParser.py for how these compose via
`&`/`@` into a full skill condition expression.
"""

from __future__ import annotations

from typing import Callable, Optional

from .activation_sample_policy import (
    AllCornerRandomPolicy,
    ErlangRandomPolicy,
    ImmediatePolicy,
    LogNormalRandomPolicy,
    RandomPolicy,
    StraightRandomPolicy,
    UniformRandomPolicy,
)
from .region import Region, RegionList
from . import course_data as _course_data_mod
from .horse_types import Strategy, strategy_matches


def _is_sorted_by_start(items):
    return _course_data_mod.is_sorted_by_start(items)


def _phase_start(distance, phase):
    return _course_data_mod.phase_start(distance, phase)


def _phase_end(distance, phase):
    return _course_data_mod.phase_end(distance, phase)


DynamicCondition = Callable[["object"], bool]


def k_true(_state) -> bool:
    return True


def _with_default_cond(r):
    if isinstance(r, RegionList):
        return r, k_true
    return r


class Operator:
    sample_policy = None

    def apply(self, regions, course, horse, extra):
        raise NotImplementedError


class _CmpOperator(Operator):
    _METHOD = None

    def __init__(self, condition: "Condition", argument: float):
        self.condition = condition
        self.argument = argument
        self.sample_policy = condition.sample_policy

    def apply(self, regions, course, horse, extra):
        method = getattr(self.condition, self._METHOD)
        return _with_default_cond(method(regions, self.argument, course, horse, extra))


class EqOperator(_CmpOperator):
    _METHOD = "filter_eq"


class NeqOperator(_CmpOperator):
    _METHOD = "filter_neq"


class LtOperator(_CmpOperator):
    _METHOD = "filter_lt"


class LteOperator(_CmpOperator):
    _METHOD = "filter_lte"


class GtOperator(_CmpOperator):
    _METHOD = "filter_gt"


class GteOperator(_CmpOperator):
    _METHOD = "filter_gte"


class AndOperator(Operator):
    def __init__(self, left: Operator, right: Operator):
        self.left = left
        self.right = right
        self.sample_policy = left.sample_policy.reconcile(right.sample_policy)

    def apply(self, regions, course, horse, extra):
        leftval, leftcond = self.left.apply(regions, course, horse, extra)
        rightval, rightcond = self.right.apply(leftval, course, horse, extra)
        if leftcond is k_true and rightcond is k_true:
            return rightval, k_true
        return rightval, (lambda s: leftcond(s) and rightcond(s))


class OrOperator(Operator):
    def __init__(self, left: Operator, right: Operator):
        self.left = left
        self.right = right
        self.sample_policy = left.sample_policy.reconcile(right.sample_policy)

    def apply(self, regions, course, horse, extra):
        leftval, leftcond = self.left.apply(regions, course, horse, extra)
        rightval, rightcond = self.right.apply(regions, course, horse, extra)
        # See OrOperator.apply in ActivationConditions.ts for the caveat this
        # inherits verbatim: technically unsound if both static AND dynamic
        # conditions differ between branches. No skill in practice does that.
        return leftval.union(rightval), (lambda s: leftcond(s) or rightcond(s))


def _not_supported(regions, arg, course, horse, extra):
    raise AssertionError("unsupported comparison")


def _noop(regions, arg, course, horse, extra):
    return regions


class Condition:
    __slots__ = (
        "sample_policy", "filter_eq", "filter_neq", "filter_lt",
        "filter_lte", "filter_gt", "filter_gte",
    )

    def __init__(self, sample_policy, filter_eq=_not_supported, filter_neq=_not_supported,
                 filter_lt=_not_supported, filter_lte=_not_supported,
                 filter_gt=_not_supported, filter_gte=_not_supported):
        self.sample_policy = sample_policy
        self.filter_eq = filter_eq
        self.filter_neq = filter_neq
        self.filter_lt = filter_lt
        self.filter_lte = filter_lte
        self.filter_gt = filter_gt
        self.filter_gte = filter_gte

    def replace(self, **kw) -> "Condition":
        base = dict(
            sample_policy=self.sample_policy, filter_eq=self.filter_eq, filter_neq=self.filter_neq,
            filter_lt=self.filter_lt, filter_lte=self.filter_lte, filter_gt=self.filter_gt,
            filter_gte=self.filter_gte,
        )
        base.update(kw)
        return Condition(**base)


_NOOP_ALL = dict(filter_eq=_noop, filter_neq=_noop, filter_lt=_noop, filter_lte=_noop, filter_gt=_noop, filter_gte=_noop)


def noop_immediate() -> Condition:
    return Condition(ImmediatePolicy, **_NOOP_ALL)


def noop_random() -> Condition:
    return Condition(RandomPolicy, **_NOOP_ALL)


def immediate(**kw) -> Condition:
    return Condition(ImmediatePolicy, **kw)


def random(**kw) -> Condition:
    return Condition(RandomPolicy, **kw)


_erlang_cache: dict[tuple, ErlangRandomPolicy] = {}
_lognormal_cache: dict[tuple, LogNormalRandomPolicy] = {}
_uniform_cache: Optional[UniformRandomPolicy] = None


def erlang_random(k: int, lam: float, **kw) -> Condition:
    key = (k, lam)
    policy = _erlang_cache.get(key)
    if policy is None:
        policy = _erlang_cache[key] = ErlangRandomPolicy(k, lam)
    base = dict(filter_eq=_not_supported, filter_neq=_not_supported, filter_lt=_not_supported,
                filter_lte=_not_supported, filter_gt=_not_supported, filter_gte=_not_supported)
    base.update(kw)
    return Condition(policy, **base)


def log_normal_random(mu: float, sigma: float, **kw) -> Condition:
    key = (mu, sigma)
    policy = _lognormal_cache.get(key)
    if policy is None:
        policy = _lognormal_cache[key] = LogNormalRandomPolicy(mu, sigma)
    base = dict(filter_eq=_not_supported, filter_neq=_not_supported, filter_lt=_not_supported,
                filter_lte=_not_supported, filter_gt=_not_supported, filter_gte=_not_supported)
    base.update(kw)
    return Condition(policy, **base)


def uniform_random(**kw) -> Condition:
    global _uniform_cache
    if _uniform_cache is None:
        _uniform_cache = UniformRandomPolicy()
    base = dict(filter_eq=_not_supported, filter_neq=_not_supported, filter_lt=_not_supported,
                filter_lte=_not_supported, filter_gt=_not_supported, filter_gte=_not_supported)
    base.update(kw)
    return Condition(_uniform_cache, **base)


def noop_erlang_random(k: int, lam: float) -> Condition:
    return erlang_random(k, lam, **_NOOP_ALL)


def noop_log_normal_random(mu: float, sigma: float) -> Condition:
    return log_normal_random(mu, sigma, **_NOOP_ALL)


def _noop_section_random(start: float, end: float) -> Condition:
    def section_random(regions, _arg, course, _horse, _extra):
        bounds = Region(start * (course.distance / 24), end * (course.distance / 24))
        return regions.rmap(lambda r: r.intersect(bounds))

    return random(filter_eq=section_random, filter_neq=section_random, filter_lt=section_random,
                  filter_lte=section_random, filter_gt=section_random, filter_gte=section_random)


def _value_filter(get_value: Callable) -> Condition:
    def feq(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) == value else RegionList()

    def fneq(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) != value else RegionList()

    def flt(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) < value else RegionList()

    def flte(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) <= value else RegionList()

    def fgt(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) > value else RegionList()

    def fgte(regions, value, course, horse, extra):
        return regions if get_value(course, horse, extra) >= value else RegionList()

    return immediate(filter_eq=feq, filter_neq=fneq, filter_lt=flt, filter_lte=flte, filter_gt=fgt, filter_gte=fgte)


# ---------------------------------------------------------------------------
# Live-field-aware conditions (order, gaps, overtakes). NOT part of the
# original TS engine: that library has no notion of other horses at all, so
# these all used to be either static "assumed order-range" checks (which our
# race harness never sets, making them permanent pass-throughs) or, for the
# gap/overtake ones, distance-based RNG approximations. Now that
# `race_field.RaceField` gives every solver a live per-tick snapshot of the
# rest of the field, these become genuinely dynamic per-tick checks -- the
# real mechanic, not an approximation of it.

_CMP_OPS = {
    "eq": lambda v, a: v == a, "neq": lambda v, a: v != a,
    "lt": lambda v, a: v < a, "lte": lambda v, a: v <= a,
    "gt": lambda v, a: v > a, "gte": lambda v, a: v >= a,
}


def _live_condition(get_value, transform_arg=None, static_filter=None) -> Condition:
    """Build a full six-comparator Condition whose runtime value comes from
    `get_value(state)` each tick (state.field is None -> condition is
    "not yet determinable", i.e. false, matching the pre-race gate-skill
    activation round where no field exists yet). `transform_arg(arg, state)`
    lets e.g. order_rate convert its percentage argument into a live
    absolute-order threshold using the field's actual horse count.
    `static_filter(regions, arg, course, horse, extra)` optionally still
    restricts the static trigger region (e.g. "only in the final corner
    onward"); defaults to no restriction."""
    if transform_arg is None:
        transform_arg = lambda arg, state: arg
    if static_filter is None:
        static_filter = lambda regions, arg, course, horse, extra: regions

    def build(op_name):
        cmp = _CMP_OPS[op_name]

        def filt(regions, arg, course, horse, extra):
            restricted = static_filter(regions, arg, course, horse, extra)

            def check(s):
                if s.field is None:
                    return False
                return cmp(get_value(s), transform_arg(arg, s))
            return restricted, check
        return filt

    return immediate(**{f"filter_{name}": build(name) for name in _CMP_OPS})


def _order_filter(get_threshold: Callable[[float, int], float]) -> Condition:
    return _live_condition(lambda s: s.field.order_of(s), transform_arg=lambda arg, s: get_threshold(arg, s.field.n))


def _continuous_band_check(state_key: str, in_band: Callable):
    """`order_rate_inXX_continue` / `_outXX_continue`: true once the horse
    has been continuously in/out of the band since at least accumulatetime
    t=5s (the doc: "the first 5 seconds of the race don't count toward this
    continuity"). Tracks a per-solver "in-streak-since" timestamp under
    `state_key` (unique per band definition so multiple such conditions on
    the same horse don't collide), reset to None the instant the horse
    leaves the band."""
    def check(s):
        if s.field is None:
            return False
        since = getattr(s, state_key, None)
        if not in_band(s) or s.accumulatetime.t < 5.0:
            since = None
        elif since is None:
            since = max(s.accumulatetime.t, 5.0)
        setattr(s, state_key, since)
        return since is not None
    return check


def _order_in_filter(rate: float) -> Condition:
    def feq(regions, one, course, horse, extra):
        assert one == 1
        check = _continuous_band_check(
            f"_order_band_in_{int(rate * 1000)}",
            lambda s: s.field.order_of(s) <= round(rate * s.field.n),
        )
        return regions, check

    return immediate(filter_eq=feq)


def _order_out_filter(rate: float) -> Condition:
    def feq(regions, one, course, horse, extra):
        assert one == 1
        check = _continuous_band_check(
            f"_order_band_out_{int(rate * 1000)}",
            lambda s: s.field.order_of(s) > round(rate * s.field.n),
        )
        return regions, check

    return immediate(filter_eq=feq)


# ---------------------------------------------------------------------------
# accumulatetime: skill ids for which the "estimate where it can't activate"
# static-region optimization is skipped (transcribed verbatim from
# ActivationConditions.ts; see that file's comment for why).
_ACCUMULATETIME_FULL_COURSE_SKILLS = frozenset([
    '100302211', '100403111', '100501111', '101001211', '101002111', '101021', '101802111', '101901111', '102002211', '102302111',
    '103103211', '103203211', '103301111', '103501111', '103801211', '103802111', '103802121', '104002111', '104201211', '105201111',
    '105202211', '106003111', '106401111', '109302211', '110001111', '110001121', '110001211', '110602111', '110651', '112402111',
    '120681', '200401', '200441', '200442', '200521', '200831', '200861', '200891', '200921', '201011', '201012', '201021', '201022',
    '201071', '201072', '201091', '201092', '201141', '201142', '201201', '201202', '201271', '201272', '201302', '201401', '201402',
    '201491', '201492', '201591', '201592', '201651', '201652', '201661', '201662', '202301', '202302', '202303', '202351', '202352',
    '203601', '203602', '203861', '203862', '203871', '203872', '204001', '204002', '204041', '204042', '408011', '409051', '412011',
    '412031',
])

_CORNER_RANDOM_QUAD_SKILLS = frozenset([
    '200331', '200332', '200333', '200341', '200342', '200343', '200351', '200352', '200353',
    '200971', '200972', '201041', '201042', '201111', '201112', '201181', '201182',
    '201251', '201252', '201321', '201322', '201391', '201392', '201461', '201462',
])

_PHASE_FUDGE_SKILLS = frozenset(['100591', '900591', '110261', '910261', '110191', '910191', '120451', '920451', '101502121'])


def _accumulatetime_filter_gte(regions, t, course, horse, extra):
    if extra.skill_id in _ACCUMULATETIME_FULL_COURSE_SKILLS:
        allowed_region = Region(0, course.distance)
    else:
        base_speed = 20.0 - (course.distance - 2000) / 1000.0
        allowed_region = Region(0.85 * base_speed * t, course.distance)
    return regions.rmap(lambda r: r.intersect(allowed_region)), (lambda s: s.accumulatetime.t >= t)


def _all_corner_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    corners = [Region(c.start, c.start + c.length) for c in course.corners]
    return regions.rmap(lambda r: [r.intersect(c) for c in corners])


def _corner_filter_eq(regions, corner_num, course, horse, extra):
    assert _is_sorted_by_start(course.corners)
    if corner_num == 0:
        last_end = 0
        non_corners = []
        for c in course.corners:
            non_corners.append(Region(last_end, c.start))
            last_end = c.start + c.length
        if last_end != course.distance:
            non_corners.append(Region(last_end, course.distance))
        return regions.rmap(lambda r: [r.intersect(s) for s in non_corners])
    elif len(course.corners) + corner_num >= 5:
        corners = []
        idx = len(course.corners) + corner_num - 5
        while idx >= 0:
            corner = course.corners[idx]
            corners.append(Region(corner.start, corner.start + corner.length))
            idx -= 4
        corners.reverse()
        return regions.rmap(lambda r: [r.intersect(c) for c in corners])
    else:
        return RegionList()


def _corner_filter_neq(regions, corner_num, course, horse, extra):
    assert corner_num == 0
    corners = [Region(c.start, c.start + c.length) for c in course.corners]
    return regions.rmap(lambda r: [r.intersect(c) for c in corners])


def _corner_random_filter_eq(regions, corner_num, course, horse, extra):
    assert _is_sorted_by_start(course.corners)
    if extra.skill_id in _CORNER_RANDOM_QUAD_SKILLS:
        if corner_num == 1:
            corner = course.corners[max(len(course.corners) - 4, 0)]
            bounds = Region(corner.start, corner.start + corner.length)
            return regions.rmap(lambda r: r.intersect(bounds))
        return RegionList()
    if len(course.corners) + corner_num >= 5:
        corner = course.corners[len(course.corners) + corner_num - 5]
        bounds = Region(corner.start, corner.start + corner.length)
        return regions.rmap(lambda r: r.intersect(bounds))
    return RegionList()


def _distance_rate_filter_lte(regions, rate, course, horse, extra):
    bounds = Region(0, course.distance * rate / 100)
    return regions.rmap(lambda r: r.intersect(bounds))


def _distance_rate_filter_gte(regions, rate, course, horse, extra):
    bounds = Region(course.distance * rate / 100, course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _distance_rate_after_random_filter_eq(regions, rate, course, horse, extra):
    bounds = Region(course.distance * rate / 100, course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _distance_type_filter_eq(regions, distance_type, course, horse, extra):
    return regions if course.distance_type == distance_type else RegionList()


def _distance_type_filter_neq(regions, distance_type, course, horse, extra):
    return regions if course.distance_type != distance_type else RegionList()


def _down_slope_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    slopes = [Region(s.start, s.start + s.length) for s in course.slopes if s.slope < 0]
    return regions.rmap(lambda r: [r.intersect(s) for s in slopes])


def _hp_per_filter_lte(regions, hp_per, course, horse, extra):
    hp_per /= 100
    return regions, (lambda s: s.hp.hp_ratio_remaining() <= hp_per)


def _hp_per_filter_gte(regions, hp_per, course, horse, extra):
    hp_per /= 100
    return regions, (lambda s: s.hp.hp_ratio_remaining() >= hp_per)


def _is_activate_any_skill_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    return regions, (lambda s: s.activate_count_last_frame > 0)


def _is_activate_heal_skill_filter_eq(regions, flag, course, horse, extra):
    """Has THIS horse activated a recovery (effect type 9) skill yet?

    ADDED 2026-09-03 -- was unregistered (a silent always-true no-op). The
    solver already keeps the count this needs (activate_count_heal, bumped on
    every SELF recovery activation); nothing was reading it for the boolean
    form."""
    return regions, (lambda s: (s.activate_count_heal > 0) == (flag == 1))


def _is_activate_other_skill_detail_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    return regions, (lambda s: extra.skill_id in s.used_skills)


def _is_basis_distance_filter_eq(regions, flag, course, horse, extra):
    assert flag in (0, 1)
    return regions if min(course.distance % 400, 1) != flag else RegionList()


def _is_badstart_filter_eq(regions, flag, course, horse, extra):
    assert flag in (0, 1)
    f = (lambda s: s.start_delay > 0.08) if flag else (lambda s: s.start_delay <= 0.08)
    return regions, f


_DIRT_GRADE_TRACK_IDS = (10101, 10103, 10104, 10105)


def _is_dirtgrade_filter_eq(regions, flag, course, horse, extra):
    assert flag == 1
    return regions if course.race_track_id in _DIRT_GRADE_TRACK_IDS else RegionList()


def _is_dirtgrade_filter_neq(regions, flag, course, horse, extra):
    assert flag == 1
    return regions if course.race_track_id not in _DIRT_GRADE_TRACK_IDS else RegionList()


# Doc: tight-corner tracks -- Sapporo, Hakodate, Fukushima, Kokura, Kawasaki,
# Funabashi (track_id, not course_set_id).
_TIGHT_TRACK_IDS = (10001, 10002, 10004, 10010, 10103, 10104)


def _is_tight_track_filter_eq(regions, flag, course, horse, extra):
    assert flag in (0, 1)
    is_tight = course.race_track_id in _TIGHT_TRACK_IDS
    return regions if bool(flag) == is_tight else RegionList()


def _is_finalcorner_filter_eq(regions, flag, course, horse, extra):
    assert flag in (0, 1)
    assert _is_sorted_by_start(course.corners)
    if not course.corners:
        return RegionList()
    final_start = course.corners[-1].start
    bounds = Region(final_start, course.distance) if flag else Region(0, final_start)
    return regions.rmap(lambda r: r.intersect(bounds))


def _is_finalcorner_laterhalf_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    assert _is_sorted_by_start(course.corners)
    if not course.corners:
        return RegionList()
    fc = course.corners[-1]
    bounds = Region((fc.start + fc.start + fc.length) / 2, fc.start + fc.length)
    return regions.rmap(lambda r: r.intersect(bounds))


def _is_finalcorner_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    assert _is_sorted_by_start(course.corners)
    if not course.corners:
        return RegionList()
    fc = course.corners[-1]
    bounds = Region(fc.start, fc.start + fc.length)
    return regions.rmap(lambda r: r.intersect(bounds))


def _is_hp_empty_onetime_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    return regions, (lambda s: not s.hp.has_remaining_hp())


def _is_lastspurt_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    bounds = Region(_phase_start(course.distance, 2), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds)), (lambda s: s.is_last_spurt)


def _is_last_straight_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    assert _is_sorted_by_start(course.straights)
    last_straight = course.straights[-1]
    bounds = Region(last_straight.start, last_straight.end)
    return regions.rmap(lambda r: r.intersect(bounds))


def _is_last_straight_onetime_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    assert _is_sorted_by_start(course.straights)
    last_straight_start = course.straights[-1].start
    trigger = Region(last_straight_start, last_straight_start + 10)
    return regions.rmap(lambda r: r.intersect(trigger))


def _is_temptation_filter_eq(regions, b, course, horse, extra):
    return regions, (lambda s: int(s.is_kakari) == b)


def _is_used_skill_id_filter_eq(regions, skill_id, course, horse, extra):
    return regions, (lambda s: str(skill_id) in s.used_skills)


def _lastspurt_filter_eq(regions, case_, course, horse, extra):
    if case_ == 1:
        f = lambda s: s.is_last_spurt and s.last_spurt_transition != -1
    elif case_ == 2:
        f = lambda s: s.is_last_spurt and s.last_spurt_transition == -1
    elif case_ == 3:
        f = lambda s: not s.is_last_spurt
    else:
        raise AssertionError("lastspurt case must be 1-3")
    bounds = Region(_phase_start(course.distance, 2), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds)), f


def _order_get_pos(arg, _n):
    return arg


def _order_rate_get_pos(rate, num_umas):
    return round(num_umas * (rate / 100.0))


def _phase_filter_eq(regions, phase, course, horse, extra):
    fudge = 10 if extra.skill_id in _PHASE_FUDGE_SKILLS else 0
    bounds = Region(_phase_start(course.distance, phase), _phase_end(course.distance, phase) + fudge)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_filter_lt(regions, phase, course, horse, extra):
    assert phase > 0
    bounds = Region(0, _phase_start(course.distance, phase))
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_filter_lte(regions, phase, course, horse, extra):
    bounds = Region(0, _phase_end(course.distance, phase))
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_filter_gt(regions, phase, course, horse, extra):
    assert phase < 3
    bounds = Region(_phase_start(course.distance, phase + 1), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_filter_gte(regions, phase, course, horse, extra):
    bounds = Region(_phase_start(course.distance, phase), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_corner_random_filter_eq(regions, phase, course, horse, extra):
    phase_start = _phase_start(course.distance, phase)
    phase_end = _phase_end(course.distance, phase)
    corners = []
    for c in course.corners:
        c_end = c.start + c.length
        if (phase_start <= c.start < phase_end) or (phase_start <= c_end < phase_end):
            corners.append(Region(max(c.start, phase_start), min(c_end, phase_end)))
    return regions.rmap(lambda r: [r.intersect(c) for c in corners])


def _phase_firsthalf_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    bounds = Region(start, start + (end - start) / 2)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_firstquarter_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    bounds = Region(start, start + (end - start) / 4)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_laterhalf_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    bounds = Region((start + end) / 2, end)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_laterhalf_random_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    bounds = Region((start + end) / 2, end)
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_random_filter_eq(regions, phase, course, horse, extra):
    bounds = Region(_phase_start(course.distance, phase), _phase_end(course.distance, phase))
    return regions.rmap(lambda r: r.intersect(bounds))


def _phase_straight_random_filter_eq(regions, phase, course, horse, extra):
    phase_bounds = Region(_phase_start(course.distance, phase), _phase_end(course.distance, phase))
    straights = [Region(s.start, s.end) for s in course.straights]
    mapped = regions.rmap(lambda r: [r.intersect(s) for s in straights])
    return mapped.rmap(lambda r: r.intersect(phase_bounds))


def _phase_first_half_straight_random_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    half_bounds = Region(start, start + (end - start) / 2)
    straights = [Region(s.start, s.end) for s in course.straights]
    mapped = regions.rmap(lambda r: [r.intersect(s) for s in straights])
    return mapped.rmap(lambda r: r.intersect(half_bounds))


def _phase_latter_half_straight_random_filter_eq(regions, phase, course, horse, extra):
    start = _phase_start(course.distance, phase)
    end = _phase_end(course.distance, phase)
    half_bounds = Region(start + (end - start) / 2, end)
    straights = [Region(s.start, s.end) for s in course.straights]
    mapped = regions.rmap(lambda r: [r.intersect(s) for s in straights])
    return mapped.rmap(lambda r: r.intersect(half_bounds))


def _gate_block(state, num_umas: int) -> int:
    gate_number = state.gate_roll % num_umas
    if gate_number < 9:
        return gate_number
    return 1 + (24 - gate_number) % 8


def _post_number_filter_eq(regions, post, course, horse, extra):
    num_umas = extra.num_umas or 9
    return regions, (lambda s: _gate_block(s, num_umas) == post)


def _post_number_filter_lte(regions, post, course, horse, extra):
    num_umas = extra.num_umas or 9
    return regions, (lambda s: _gate_block(s, num_umas) <= post)


def _post_number_filter_gte(regions, post, course, horse, extra):
    num_umas = extra.num_umas or 9
    return regions, (lambda s: _gate_block(s, num_umas) >= post)


def _random_lot_filter_eq(regions, lot, course, horse, extra):
    return regions, (lambda s: s.random_lot < lot)


def _remain_distance_filter_eq(regions, remain, course, horse, extra):
    bounds = Region(course.distance - remain, course.distance - remain + 1)
    return regions.rmap(lambda r: r.intersect(bounds))


def _remain_distance_filter_lte(regions, remain, course, horse, extra):
    bounds = Region(course.distance - remain, course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _remain_distance_filter_gte(regions, remain, course, horse, extra):
    bounds = Region(0, course.distance - remain)
    return regions.rmap(lambda r: r.intersect(bounds))


_FURLONG_METERS = 201.168


def _furlong_filter_eq(regions, n, course, horse, extra):
    bounds = Region((n - 1) * _FURLONG_METERS, n * _FURLONG_METERS)
    return regions.rmap(lambda r: r.intersect(bounds))


def _run_at_full_speed_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    bounds = Region(_phase_start(course.distance, 3), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _running_style_filter_eq(regions, strategy, course, horse, extra):
    return regions if strategy_matches(horse.strategy, strategy) else RegionList()


def _slope_filter_eq(regions, slope_type, course, horse, extra):
    assert slope_type in (0, 1, 2)
    assert _is_sorted_by_start(course.slopes)
    last_end = 0
    slopes = [s for s in course.slopes if (slope_type != 2 and s.slope > 0) or (slope_type != 1 and s.slope < 0)]
    if slope_type == 0:
        slope_r = []
        for s in slopes:
            slope_r.append(Region(last_end, s.start))
            last_end = s.start + s.length
        if last_end != course.distance:
            slope_r.append(Region(last_end, course.distance))
    else:
        slope_r = [Region(s.start, s.start + s.length) for s in slopes]
    return regions.rmap(lambda r: [r.intersect(s) for s in slope_r])


def _straight_front_type_filter_eq(regions, front_type, course, horse, extra):
    assert front_type in (1, 2)
    straights = [Region(s.start, s.end) for s in course.straights if s.front_type == front_type]
    return regions.rmap(lambda r: [r.intersect(s) for s in straights])


def _straight_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    straights = [Region(s.start, s.end) for s in course.straights]
    return regions.rmap(lambda r: [r.intersect(s) for s in straights])


def _last_straight_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    assert _is_sorted_by_start(course.straights)
    if not course.straights:
        return RegionList()
    s = course.straights[-1]
    bounds = Region(s.start, s.end)
    return regions.rmap(lambda r: r.intersect(bounds))


def _temptation_count_filter_eq(regions, n, course, horse, extra):
    return regions, (lambda s: s.temptation_count == n)


def _up_slope_random_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    slopes = [Region(s.start, s.start + s.length) for s in course.slopes if s.slope > 0]
    return regions.rmap(lambda r: [r.intersect(s) for s in slopes])


def _up_slope_random_later_half_filter_eq(regions, one, course, horse, extra):
    assert one == 1
    half = Region(course.distance / 2, course.distance)
    slopes = [Region(s.start, s.start + s.length) for s in course.slopes if s.slope > 0]
    mapped = regions.rmap(lambda r: [r.intersect(s) for s in slopes])
    return mapped.rmap(lambda r: r.intersect(half))


def _compete_fight_count_filter_gt(regions, arg, course, horse, extra):
    assert _is_sorted_by_start(course.straights)
    last_straight = course.straights[-1]
    bounds = Region(last_straight.start, last_straight.end)
    return regions.rmap(lambda r: r.intersect(bounds))


def _did_overtake_value(s) -> int:
    return int(s.field.did_overtake(s))


def _change_order_up_end_after_static(regions, arg, course, horse, extra):
    bounds = Region(_phase_start(course.distance, 2), course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _change_order_up_finalcorner_after_static(regions, arg, course, horse, extra):
    assert _is_sorted_by_start(course.corners)
    if not course.corners:
        return RegionList()
    final_corner_start = course.corners[-1].start
    bounds = Region(final_corner_start, course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _change_order_up_middle_static(regions, arg, course, horse, extra):
    bounds = Region(_phase_start(course.distance, 1), _phase_end(course.distance, 1))
    return regions.rmap(lambda r: r.intersect(bounds))


def _activate_count_all_filter_lte(regions, n, course, horse, extra):
    return regions, (lambda s: sum(s.activate_count) <= n)


def _activate_count_all_filter_gte(regions, n, course, horse, extra):
    return regions, (lambda s: sum(s.activate_count) >= n)


def _activate_count_end_after_filter_gte(regions, n, course, horse, extra):
    return regions, (lambda s: s.activate_count[2] >= n)


def _activate_count_heal_filter_gte(regions, n, course, horse, extra):
    return regions, (lambda s: s.activate_count_heal >= n)


def _activate_count_later_half_filter_gte(regions, n, course, horse, extra):
    """Skills THIS horse has activated past the halfway mark of the course.

    ADDED 2026-09-03 -- was unregistered (a silent always-true no-op). Its one
    real user is 210131 "Racing Spirit: Wit", whose Global text is "Slightly
    increase velocity upon activating 2 skills during the second half of the
    race", so "later half" here is the DISTANCE half (the same 50% split
    distance_rate>=50 uses elsewhere in this data), not a race phase -- the
    per-phase counters are the separate activate_count_start/middle/end_after
    family right above."""
    return regions, (lambda s: s.activate_count_later_half >= n)


def _activate_count_middle_filter_gte(regions, n, course, horse, extra):
    return regions, (lambda s: s.activate_count[1] >= n)


def _activate_count_start_filter_gte(regions, n, course, horse, extra):
    return regions, (lambda s: s.activate_count[0] >= n)


def _running_style_count_helper(strategy: int):
    def fn(course, horse, extra):
        return int(strategy_matches(horse.strategy, strategy))
    return fn


# temptation_count_behind/infront and running_style_temptation_count_* used
# to be _noop_section_random(2, 9) stubs (always-true at a random point in
# sections 2-9, ignoring the actual argument and any real rushing state) --
# a leftover from before race_field.RaceField existed. Real per-tick checks
# now that every solver has live field awareness, same reasoning as
# near_count/is_surrounded/etc above. "Opponent" variants: this engine has
# no notion of team affiliation anywhere (every horse in the field is just
# another horse), so they're implemented identically to the plain (self-
# inclusive-count) versions -- correct for the vast majority of races (no
# teammates present); in a genuine team race this over-counts teammates as
# opponents rather than silently dropping the skill.
def _temptation_count_behind_live() -> Condition:
    return _live_condition(lambda s: s.field.temptation_count_behind(s))


def _temptation_count_infront_live() -> Condition:
    return _live_condition(lambda s: s.field.temptation_count_infront(s))


def _running_style_temptation_count_live(strategy: int) -> Condition:
    return _live_condition(lambda s: s.field.running_style_temptation_count(strategy))


def _build_conditions() -> dict:
    c: dict[str, Condition] = {}

    c["accumulatetime"] = immediate(filter_gte=_accumulatetime_filter_gte)
    c["activate_count_all"] = immediate(filter_lte=_activate_count_all_filter_lte, filter_gte=_activate_count_all_filter_gte)
    c["activate_count_end_after"] = immediate(filter_gte=_activate_count_end_after_filter_gte)
    c["activate_count_heal"] = immediate(filter_gte=_activate_count_heal_filter_gte)
    c["activate_count_later_half"] = immediate(filter_gte=_activate_count_later_half_filter_gte)
    c["activate_count_middle"] = immediate(filter_gte=_activate_count_middle_filter_gte)
    c["activate_count_start"] = immediate(filter_gte=_activate_count_start_filter_gte)
    c["all_corner_random"] = Condition(AllCornerRandomPolicy, filter_eq=_all_corner_random_filter_eq)
    c["always"] = noop_immediate()
    c["base_power"] = _value_filter(lambda course, horse, extra: horse.power)
    c["base_speed"] = _value_filter(lambda course, horse, extra: horse.speed)
    c["base_stamina"] = _value_filter(lambda course, horse, extra: horse.stamina)
    c["base_guts"] = _value_filter(lambda course, horse, extra: horse.guts)
    c["base_wiz"] = _value_filter(lambda course, horse, extra: horse.wisdom)
    # 1 Bashin = 2.5m (doc). Now that RaceField gives real per-tick gaps,
    # these are genuine live checks rather than the original engine's
    # Erlang-distribution timing approximation (which existed only because
    # real position data wasn't available). No horse behind/ahead (last /
    # leading) is treated as an infinite gap.
    c["bashin_diff_behind"] = _live_condition(
        lambda s: (lambda d: float("inf") if d is None else d / 2.5)(s.field.distance_to_behind(s)))
    c["bashin_diff_infront"] = _live_condition(
        lambda s: (lambda d: float("inf") if d is None else d / 2.5)(s.field.distance_to_ahead(s)))
    # Blocking/near-lane timers are now real (RaceSolver._update_blocking /
    # _update_near_lane_timers, backed by RaceField -- see Phase 4 of the
    # race-engine accuracy pass). Not part of the original TS engine, which
    # approximated all of these with a plain Erlang-distributed trigger
    # placement since it had no notion of other horses' positions at all.
    c["behind_near_lane_time"] = _live_condition(lambda s: s._behind_near_lane_timer)
    c["behind_near_lane_time_set1"] = _live_condition(lambda s: s._behind_near_lane_timer)
    c["blocked_all_continuetime"] = _live_condition(lambda s: s._blocked_all_timer)
    c["blocked_front"] = _live_condition(lambda s: int(s.field.front_blocker(s) is not None))
    c["blocked_front_continuetime"] = _live_condition(lambda s: s._blocked_front_timer)
    c["blocked_side_continuetime"] = _live_condition(lambda s: s._blocked_side_timer)
    # "did this horse's order improve this tick" -- inferred trigger
    # definition (the doc doesn't spell these out precisely by name), but
    # matches the plain reading of "change order up" against real live rank.
    c["change_order_onetime"] = _live_condition(_did_overtake_value)
    c["change_order_up_end_after"] = _live_condition(_did_overtake_value, static_filter=_change_order_up_end_after_static)
    c["change_order_up_finalcorner_after"] = _live_condition(_did_overtake_value, static_filter=_change_order_up_finalcorner_after_static)
    c["change_order_up_middle"] = _live_condition(_did_overtake_value, static_filter=_change_order_up_middle_static)
    c["compete_fight_count"] = uniform_random(filter_gt=_compete_fight_count_filter_gt)
    c["corner"] = immediate(filter_eq=_corner_filter_eq, filter_neq=_corner_filter_neq)
    c["corner_count"] = _value_filter(lambda course, horse, extra: len(course.corners))
    c["corner_random"] = random(filter_eq=_corner_random_filter_eq)
    c["course_distance"] = _value_filter(lambda course, horse, extra: course.distance)
    # distance_diff_rate has no explicit formula in the doc; inferred here as
    # gap-to-leader expressed as a percentage of course distance (consistent
    # with how every other "_rate" condition in this table is a percentage).
    c["distance_diff_rate"] = _live_condition(
        lambda s: (s.field.distance_to_leader(s) / s.course.distance * 100) if s.course.distance else 0.0)
    c["distance_diff_top"] = _live_condition(lambda s: round(s.field.distance_to_leader(s)))
    c["distance_diff_top_float"] = _live_condition(lambda s: s.field.distance_to_leader(s))
    c["distance_rate"] = immediate(filter_lte=_distance_rate_filter_lte, filter_gte=_distance_rate_filter_gte)
    c["distance_rate_after_random"] = random(filter_eq=_distance_rate_after_random_filter_eq)
    c["distance_type"] = immediate(filter_eq=_distance_type_filter_eq, filter_neq=_distance_type_filter_neq)
    c["down_slope_random"] = random(filter_eq=_down_slope_random_filter_eq)
    c["furlong"] = immediate(filter_eq=_furlong_filter_eq)
    c["grade"] = _value_filter(lambda course, horse, extra: extra.grade)
    c["ground_condition"] = _value_filter(lambda course, horse, extra: extra.ground_condition)
    c["ground_type"] = _value_filter(lambda course, horse, extra: course.surface)
    c["hp_per"] = immediate(filter_lte=_hp_per_filter_lte, filter_gte=_hp_per_filter_gte)
    c["infront_near_lane_time"] = _live_condition(lambda s: s._infront_near_lane_timer)
    c["is_activate_any_skill"] = immediate(filter_eq=_is_activate_any_skill_filter_eq)
    c["is_activate_heal_skill"] = immediate(filter_eq=_is_activate_heal_skill_filter_eq)
    c["is_activate_other_skill_detail"] = immediate(filter_eq=_is_activate_other_skill_detail_filter_eq)
    c["is_basis_distance"] = immediate(filter_eq=_is_basis_distance_filter_eq)
    c["is_badstart"] = immediate(filter_eq=_is_badstart_filter_eq)
    c["is_behind_in"] = noop_immediate()
    c["is_dirtgrade"] = immediate(filter_eq=_is_dirtgrade_filter_eq, filter_neq=_is_dirtgrade_filter_neq)
    c["is_exist_skill_id"] = noop_immediate()
    c["is_finalcorner"] = immediate(filter_eq=_is_finalcorner_filter_eq)
    c["is_finalcorner_laterhalf"] = immediate(filter_eq=_is_finalcorner_laterhalf_filter_eq)
    c["is_finalcorner_random"] = random(filter_eq=_is_finalcorner_random_filter_eq)
    c["is_hp_empty_onetime"] = immediate(filter_eq=_is_hp_empty_onetime_filter_eq)
    c["is_lastspurt"] = immediate(filter_eq=_is_lastspurt_filter_eq)
    c["is_last_straight"] = immediate(filter_eq=_is_last_straight_filter_eq)
    c["is_last_straight_onetime"] = immediate(filter_eq=_is_last_straight_onetime_filter_eq)
    c["is_move_lane"] = _live_condition(lambda s: int(s._moved_lane_this_tick))
    c["is_overtake"] = _live_condition(_did_overtake_value)
    c["is_surrounded"] = _live_condition(lambda s: int(s.field.is_surrounded(s)))
    c["is_temptation"] = immediate(filter_eq=_is_temptation_filter_eq)
    c["is_used_skill_id"] = immediate(filter_eq=_is_used_skill_id_filter_eq)
    c["lane_type"] = noop_immediate()
    c["lastspurt"] = immediate(filter_eq=_lastspurt_filter_eq)
    c["motivation"] = _value_filter(lambda course, horse, extra: extra.mood + 3)
    c["near_count"] = _live_condition(lambda s: s.field.near_count(s))
    c["near_infront_count"] = _live_condition(lambda s: s.field.near_infront_count(s))
    c["order"] = _order_filter(_order_get_pos)
    c["order_rate"] = _order_filter(_order_rate_get_pos)
    c["order_rate_in20_continue"] = _order_in_filter(0.2)
    c["order_rate_in40_continue"] = _order_in_filter(0.4)
    c["order_rate_in50_continue"] = _order_in_filter(0.5)
    c["order_rate_in80_continue"] = _order_in_filter(0.8)
    c["order_rate_out20_continue"] = _order_out_filter(0.2)
    c["order_rate_out40_continue"] = _order_out_filter(0.4)
    c["order_rate_out50_continue"] = _order_out_filter(0.5)
    c["order_rate_out70_continue"] = _order_out_filter(0.7)
    # Left as the original engine's distribution approximation: these need
    # the doc's full Overtake-mode target-selection (which horse this one is
    # actively chasing), which this pass's simplified lane-selection doesn't
    # model -- see RaceSolver._update_lane_target's docstring.
    c["overtake_target_no_order_up_time"] = noop_erlang_random(3, 2.0)
    c["overtake_target_time"] = noop_erlang_random(3, 2.0)
    c["phase"] = Condition(
        ImmediatePolicy, filter_eq=_phase_filter_eq, filter_neq=_not_supported, filter_lt=_phase_filter_lt,
        filter_lte=_phase_filter_lte, filter_gt=_phase_filter_gt, filter_gte=_phase_filter_gte,
    )
    c["phase_corner_random"] = random(filter_eq=_phase_corner_random_filter_eq)
    c["phase_firsthalf"] = immediate(filter_eq=_phase_firsthalf_filter_eq)
    c["phase_firsthalf_random"] = random(filter_eq=_phase_firsthalf_filter_eq)
    c["phase_first_half_straight_random"] = Condition(StraightRandomPolicy, filter_eq=_phase_first_half_straight_random_filter_eq)
    c["phase_firstquarter"] = immediate(filter_eq=_phase_firstquarter_filter_eq)
    c["phase_firstquarter_random"] = random(filter_eq=_phase_firstquarter_filter_eq)
    c["phase_laterhalf"] = immediate(filter_eq=_phase_laterhalf_filter_eq)
    c["phase_laterhalf_random"] = random(filter_eq=_phase_laterhalf_random_filter_eq)
    c["phase_latter_half_straight_random"] = Condition(StraightRandomPolicy, filter_eq=_phase_latter_half_straight_random_filter_eq)
    c["phase_random"] = random(filter_eq=_phase_random_filter_eq)
    c["phase_straight_random"] = Condition(StraightRandomPolicy, filter_eq=_phase_straight_random_filter_eq)
    c["popularity"] = _value_filter(lambda course, horse, extra: extra.popularity)
    c["post_number"] = immediate(filter_eq=_post_number_filter_eq, filter_lte=_post_number_filter_lte, filter_gte=_post_number_filter_gte)
    c["random_lot"] = immediate(filter_eq=_random_lot_filter_eq)
    c["remain_distance"] = immediate(filter_eq=_remain_distance_filter_eq, filter_lte=_remain_distance_filter_lte, filter_gte=_remain_distance_filter_gte)
    c["rotation"] = _value_filter(lambda course, horse, extra: course.turn)
    c["run_at_full_speed_random"] = random(filter_eq=_run_at_full_speed_random_filter_eq)
    c["running_style"] = immediate(filter_eq=_running_style_filter_eq)
    c["running_style_count_same"] = noop_immediate()
    c["running_style_count_same_rate"] = noop_immediate()
    c["running_style_count_nige_otherself"] = _value_filter(lambda course, horse, extra: int(strategy_matches(horse.strategy, Strategy.NIGE)))
    c["running_style_count_senko_otherself"] = _value_filter(lambda course, horse, extra: int(strategy_matches(horse.strategy, Strategy.SENKOU)))
    c["running_style_count_sashi_otherself"] = _value_filter(lambda course, horse, extra: int(strategy_matches(horse.strategy, Strategy.SASI)))
    c["running_style_count_oikomi_otherself"] = _value_filter(lambda course, horse, extra: int(strategy_matches(horse.strategy, Strategy.OIKOMI)))
    c["running_style_equal_popularity_one"] = noop_immediate()
    c["running_style_temptation_count_nige"] = _running_style_temptation_count_live(Strategy.NIGE)
    c["running_style_temptation_count_senko"] = _running_style_temptation_count_live(Strategy.SENKOU)
    c["running_style_temptation_count_sashi"] = _running_style_temptation_count_live(Strategy.SASI)
    c["running_style_temptation_count_oikomi"] = _running_style_temptation_count_live(Strategy.OIKOMI)
    # "opponent" variants: no team concept exists in this engine -- see the
    # comment on _temptation_count_behind_live above.
    c["running_style_temptation_opponent_count_nige"] = _running_style_temptation_count_live(Strategy.NIGE)
    c["running_style_temptation_opponent_count_senko"] = _running_style_temptation_count_live(Strategy.SENKOU)
    c["running_style_temptation_opponent_count_sashi"] = _running_style_temptation_count_live(Strategy.SASI)
    c["running_style_temptation_opponent_count_oikomi"] = _running_style_temptation_count_live(Strategy.OIKOMI)
    c["same_skill_horse_count"] = noop_immediate()
    c["season"] = _value_filter(lambda course, horse, extra: extra.season)
    c["slope"] = immediate(filter_eq=_slope_filter_eq)
    c["straight_front_type"] = immediate(filter_eq=_straight_front_type_filter_eq)
    c["straight_random"] = Condition(StraightRandomPolicy, filter_eq=_straight_random_filter_eq)
    c["last_straight_random"] = Condition(StraightRandomPolicy, filter_eq=_last_straight_random_filter_eq)
    c["temptation_count"] = immediate(filter_eq=_temptation_count_filter_eq)
    c["temptation_count_behind"] = _temptation_count_behind_live()
    c["temptation_count_infront"] = _temptation_count_infront_live()
    c["temptation_opponent_count_behind"] = _temptation_count_behind_live()
    c["temptation_opponent_count_infront"] = _temptation_count_infront_live()
    c["time"] = _value_filter(lambda course, horse, extra: extra.time)
    c["track_id"] = _value_filter(lambda course, horse, extra: course.race_track_id)
    c["is_tight_track"] = immediate(filter_eq=_is_tight_track_filter_eq)
    c["up_slope_random"] = random(filter_eq=_up_slope_random_filter_eq)
    c["up_slope_random_later_half"] = random(filter_eq=_up_slope_random_later_half_filter_eq)
    c["visiblehorse"] = noop_immediate()
    c["weather"] = _value_filter(lambda course, horse, extra: extra.weather)
    return c


Conditions: dict[str, Condition] = _build_conditions()


def activate_count_all_random_filter_gte(regions, n, course, horse, extra):
    if n == 7:
        rl = RegionList()
        for r in regions:
            rl.append(Region(r.start, r.start + 11))
        return rl
    lo = min(n / 23.0 - 0.2, 0.6) * course.distance
    hi = min(n / 23.0 + 0.2, 1.0) * course.distance
    bounds = Region(lo, hi)
    return regions.rmap(lambda r: r.intersect(bounds))


def activate_count_all_random_filter_lte(regions, n, course, horse, extra):
    return RegionList()


def _acr_activate_count_end_after_filter_gte(regions, n, course, horse, extra):
    bounds = Region(_phase_start(course.distance, 2), _phase_end(course.distance, 3))
    return regions.rmap(lambda r: r.intersect(bounds))


def _acr_activate_count_later_half_filter_gte(regions, n, course, horse, extra):
    bounds = Region(course.distance / 2, course.distance)
    return regions.rmap(lambda r: r.intersect(bounds))


def _acr_activate_count_middle_filter_gte(regions, n, course, horse, extra):
    start = _phase_start(course.distance, 1)
    end = _phase_end(course.distance, 1)
    bounds = Region(start, start + n / 10 * (end - start))
    return regions.rmap(lambda r: r.intersect(bounds))


def _acr_activate_count_start_filter_gte(regions, n, course, horse, extra):
    bounds = Region(_phase_start(course.distance, 0), _phase_end(course.distance, 0))
    return regions.rmap(lambda r: r.intersect(bounds))


def _build_conditions_with_activate_counts_as_random() -> dict:
    """Port of RaceSolverBuilder.ts's `conditionsWithActivateCountsAsRandom`
    (used via `.withActivateCountsAsRandom()`, an alternate condition table
    used for probabilistic activation-count skills)."""
    c = dict(Conditions)
    c["activate_count_all"] = random(filter_gte=activate_count_all_random_filter_gte, filter_lte=activate_count_all_random_filter_lte)
    c["activate_count_end_after"] = random(filter_gte=_acr_activate_count_end_after_filter_gte)
    c["activate_count_heal"] = noop_random()
    c["activate_count_later_half"] = random(filter_gte=_acr_activate_count_later_half_filter_gte)
    c["activate_count_middle"] = random(filter_gte=_acr_activate_count_middle_filter_gte)
    c["activate_count_start"] = immediate(filter_gte=_acr_activate_count_start_filter_gte)
    return c


ConditionsWithActivateCountsAsRandom: dict[str, Condition] = _build_conditions_with_activate_counts_as_random()
