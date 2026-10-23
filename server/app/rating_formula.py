
from __future__ import annotations

MAX_STAT_VALUE = 2500

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
    table = [0] * (MAX_STAT_VALUE + 1)

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


_TOP_SLOPE = _SCORE_TABLE[MAX_STAT_VALUE] - _SCORE_TABLE[MAX_STAT_VALUE - 1]

INT32_MAX = 2_147_483_647

MAX_RANK_SCORE = 999_999


def get_stat_score(stat: int, max_stat_value: int = MAX_STAT_VALUE) -> int:
    stat = int(stat)
    if stat <= 0:
        return 0
    if stat <= max_stat_value:
        return _SCORE_TABLE[stat]
    return _SCORE_TABLE[max_stat_value] + _TOP_SLOPE * (stat - max_stat_value)


def get_rating(stats) -> int:
    total = sum(get_stat_score(s) for s in stats if s and int(s) > 0)
    return min(MAX_RANK_SCORE, total)


_SKILL_GRADE: dict | None = None


def get_skill_score(skill_array) -> int:
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
    stats = [chara_info.get(k, 0) for k in ("speed", "stamina", "power", "wiz", "guts")]
    return min(MAX_RANK_SCORE,
               get_rating(stats) + get_skill_score(chara_info.get("skill_array")))
