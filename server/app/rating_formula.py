"""
Career rank-score formula, ported from the for-reals-uma-sim-main project's
rating_formula.py (same source/pattern as training_formula.py's port of that
project's training math). Verified against real captured trained_chara
entries: get_rating([speed, stamina, power, wiz, guts]) reproduces the real
rank_score exactly for every sampled roster entry.
"""

from __future__ import annotations

MAX_STAT_VALUE = 2500

# --- Tier rate arrays (same data as the original JS STAT_SCORES IIFE) ---
_R1 = [
    5, 8, 10, 13, 16, 18, 21, 24, 26, 28, 29, 30, 31, 33, 34, 35, 39, 41, 42, 43, 52, 55, 66,
    68, 68
]

_R2 = [
    79, 80, 81, 83, 84, 85, 86, 88, 89, 90, 92, 93, 94, 96, 97, 98, 100, 101, 102, 103, 105,
    106, 107, 109, 110, 111, 113, 114, 115, 117, 118, 119, 121, 122, 123, 124, 126, 127, 128,
    130, 131, 132, 134, 135, 136, 138, 139, 140, 141, 143, 144, 145, 147, 148, 149, 151, 152,
    153, 155, 156, 157, 159, 160, 161, 162, 164, 165, 166, 168, 169, 170, 172, 173, 174, 176,
    177, 178, 179, 181, 182, 182
]


def _build_score_table():
    """Precompute score for every stat value 0..MAX_STAT_VALUE in one pass.

    The original implementation looped from 1 up to the queried stat on every
    call (O(stat) per call); this builds the same cumulative sums once.
    """
    table = [0] * (MAX_STAT_VALUE + 1)

    # Phase 1: 1..1200
    raw = 0
    idx = 0
    for c in range(1, 1201):
        if c <= 49:
            idx = 0
        elif c <= 99:
            idx = 1
        elif c % 50 == 0:
            idx += 1
        raw += _R1[idx]
        table[c] = round(raw / 10)

    # Phase 2: 1201..2000
    raw = 38413
    idx = 0
    for c in range(1201, 2001):
        if c <= 1209:
            idx = 0
        elif c <= 1219:
            idx = 1
        elif c % 10 == 0:
            idx += 1
        raw += _R2[idx]
        table[c] = round(raw / 10)

    # Phase 3: 2001..MAX_STAT_VALUE
    raw = 142796
    idx = 0
    rate = 183
    for c in range(2001, MAX_STAT_VALUE + 1):
        if idx >= 25:
            rate += 1
            idx = 0
        raw += rate
        idx += 1
        table[c] = round(raw / 10)

    return table


_SCORE_TABLE = _build_score_table()


# The table ends at MAX_STAT_VALUE. Past that it CONTINUES LINEARLY at the
# slope it finished on, instead of flattening: a stat of 2,500 and a stat of a
# million used to score identically, so nothing above the table's end was worth
# anything. Phase 3 grows by `rate/10` per point (rate creeps +1 every 25
# points), so the final step is the natural slope to carry forward.
_TOP_SLOPE = _SCORE_TABLE[MAX_STAT_VALUE] - _SCORE_TABLE[MAX_STAT_VALUE - 1]

# rank_score travels as a 32-bit signed int; past this the client can't read it.
INT32_MAX = 2_147_483_647

# Per-uma rank_score is clamped here rather than at INT32_MAX: an
# uncapped/near-int32 rank_score on an individual trained_chara is what
# caused the OverflowException crash documented in
# trained_chara.py's MAXED_VETERAN_FAN_CEILING comment (Team Stadium's own
# rank-score math multiplies a member's rank_score, and any factor at all
# overflows int32 from a starting value that high). 999,999 keeps every
# per-uma score comfortably inside the 6-digit display real veterans use
# (legitimately maxed careers top out ~900k) while leaving no headroom to
# ever revisit that crash class. Account-wide aggregates that legitimately
# sum many umas' scores (Note Archive's directory total, Team Stadium's
# best_point, the profile's own rank_score) are NOT covered by this --
# real captured accounts already exceed 999,999 there and are not the
# per-uma value this cap targets.
MAX_RANK_SCORE = 999_999


def get_stat_score(stat: int, max_stat_value: int = MAX_STAT_VALUE) -> int:
    """Rating score for a stat value. Table lookup up to max_stat_value, then a
    straight-line extension at the table's own final slope."""
    stat = int(stat)
    if stat <= 0:
        return 0
    if stat <= max_stat_value:
        return _SCORE_TABLE[stat]
    return _SCORE_TABLE[max_stat_value] + _TOP_SLOPE * (stat - max_stat_value)


def get_rating(stats) -> int:
    """Total rating for a 5-stat list (unweighted sum of stat scores).

    Clamped to MAX_RANK_SCORE: a stat in the billions extrapolates the score
    table's linear tail into numbers that overflow int32 downstream (see
    MAX_RANK_SCORE's own comment) long before the wire's own int32 ceiling
    would. Clamping here keeps every per-uma score in the safe range."""
    total = sum(get_stat_score(s) for s in stats if s and int(s) > 0)
    return min(MAX_RANK_SCORE, total)


_SKILL_GRADE: dict | None = None


def get_skill_score(skill_array) -> int:
    """Score contributed by a trainee's skills: master.mdb's
    skill_data.grade_value, summed.

    Skills used to contribute NOTHING to the career score, so buying 25 of them
    moved the number not at all (#36). Measured against 830 captured
    trained_chara entries, stats + this sum lands a median ~3% under the real
    rank_score (ratio 1.128 of the flat sum), so a per-skill multiplier we
    can't see from master is still missing -- but that is a refinement on top
    of the right quantity, where before the whole term was absent. Skills the
    master doesn't know contribute 0 rather than raising."""
    global _SKILL_GRADE
    if _SKILL_GRADE is None:
        from . import master_data
        _SKILL_GRADE = {r["id"]: r["grade_value"] or 0
                        for r in master_data.query("SELECT id, grade_value FROM skill_data")}
    total = 0
    for s in skill_array or ():
        sid = s.get("skill_id") if isinstance(s, dict) else s
        total += _SKILL_GRADE.get(sid, 0)
    return total


def get_career_score(chara_info: dict) -> int:
    """The career's rank score: stats plus the skills the player actually has.
    Clamped to MAX_RANK_SCORE for the same reason get_rating is."""
    stats = [chara_info.get(k, 0) for k in ("speed", "stamina", "power", "wiz", "guts")]
    return min(MAX_RANK_SCORE,
               get_rating(stats) + get_skill_score(chara_info.get("skill_array")))
