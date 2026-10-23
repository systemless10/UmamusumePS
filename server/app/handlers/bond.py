
from __future__ import annotations

import functools

from .. import master_data
from . import collection

PROFILE_STATE_KEY = "chara_profile_state"

CHARA_LIST_TEMPLATE = {"training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                       "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0}

_LOCK_NONE = 0
_LOCK_BOND = 1
_LOCK_OWN_CARD = 2

_CAREER_LOVE_MAX = 18
_CAREER_LOVE_BASE = 9


@functools.lru_cache(maxsize=1)
def _thresholds() -> tuple:
    rows = master_data.query(
        "SELECT rank, total_point FROM love_rank ORDER BY total_point DESC")
    return tuple((r["total_point"], r["rank"]) for r in rows)


def love_rank(love_point: int) -> int:
    for threshold, rank in _thresholds():
        if (love_point or 0) >= threshold:
            return rank
    return 0


@functools.lru_cache(maxsize=1024)
def love_cap(chara_id: int) -> int:
    row = master_data.query_one(
        "SELECT love_rank_limit FROM chara_data WHERE id=?", (int(chara_id),))
    limit = (row["love_rank_limit"] if row else None) or max(r for _, r in _thresholds())
    cap = master_data.query_one(
        "SELECT total_point FROM love_rank WHERE rank=?", (int(limit),))
    return int(cap["total_point"]) if cap else max(t for t, _ in _thresholds())


def career_love_points(rank_id: int) -> int:
    letter_index = (int(rank_id or 0) + 1) // 2
    if letter_index <= 0:
        return 0
    return min(_CAREER_LOVE_MAX, _CAREER_LOVE_BASE + letter_index)


def chara_entry(full_state: dict, chara_id: int) -> dict:
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if not isinstance(charas, list):
        charas = []
        full_state[collection.CHARA_LIST_KEY] = charas
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    if entry is None:
        entry = dict(CHARA_LIST_TEMPLATE, chara_id=chara_id)
        charas.append(entry)
    return entry


def add_love_point(full_state: dict, chara_id: int, points: int) -> dict:
    entry = chara_entry(full_state, chara_id)
    before = entry.get("love_point") or 0
    pool_before = entry.get("love_point_pool") or 0
    points = max(0, int(points or 0))

    room = max(0, love_cap(chara_id) - before)
    applied = min(points, room)
    entry["love_point"] = before + applied
    entry["love_point_pool"] = pool_before + (points - applied)
    return {"chara_id": chara_id,
            "love_point_before": before, "love_point_after": entry["love_point"],
            "love_point_pool_before": pool_before,
            "love_point_pool_after": entry["love_point_pool"]}


@functools.lru_cache(maxsize=1)
def _profile_rows() -> tuple:
    return tuple({"id": r["id"], "chara_id": r["chara_id"], "lock_type": r["lock_type"],
                  "lock_value": r["lock_value"]}
                 for r in master_data.query(
                     "SELECT id, chara_id, lock_type, lock_value "
                     "FROM note_profile ORDER BY id"))


@functools.lru_cache(maxsize=1024)
def _rows_for(chara_id: int) -> tuple:
    own = tuple(r for r in _profile_rows() if r["chara_id"] == chara_id)
    if not master_data.is_trainee_chara(chara_id):
        return own
    return tuple(r for r in _profile_rows() if r["chara_id"] == 0) + own


@functools.lru_cache(maxsize=1)
def _profile_chara_ids() -> tuple:
    ids = {int(r["id"]) for r in master_data.query("SELECT id FROM chara_data")
           if master_data.is_trainee_chara(r["id"])}
    ids |= {r["chara_id"] for r in _profile_rows() if r["chara_id"]}
    return tuple(sorted(ids))


def _owned_card_ids(full_state: dict) -> set:
    cards = full_state.get(collection.CARD_LIST_KEY)
    if not isinstance(cards, list):
        return set()
    return {c.get("card_id") for c in cards if isinstance(c, dict)}


def _row_unlocked(row: dict, rank: int, owned_cards: set) -> bool:
    lock = row["lock_type"] or _LOCK_NONE
    if lock == _LOCK_NONE:
        return True
    if lock == _LOCK_BOND:
        return rank >= (row["lock_value"] or 0)
    if lock == _LOCK_OWN_CARD:
        return row["lock_value"] in owned_cards
    return False


def _announced(full_state: dict) -> set:
    st = full_state.get(PROFILE_STATE_KEY)
    if not isinstance(st, dict) or not isinstance(st.get("announced"), list):
        return set()
    return {(p[0], p[1]) for p in st["announced"] if isinstance(p, (list, tuple)) and len(p) == 2}


def _remember(full_state: dict, pairs) -> None:
    st = full_state.get(PROFILE_STATE_KEY)
    if not isinstance(st, dict):
        st = {}
        full_state[PROFILE_STATE_KEY] = st
    known = _announced(full_state)
    st["announced"] = sorted([list(p) for p in known | {(c, d) for c, d in pairs}])


def _unlocked_rows(full_state: dict, chara_id: int, rank: int, owned: set) -> list:
    return [r for r in _rows_for(chara_id) if _row_unlocked(r, rank, owned)]


def new_profile_entries(full_state: dict, chara_id: int) -> list:
    entry = chara_entry(full_state, chara_id)
    rank = love_rank(entry.get("love_point") or 0)
    announced = _announced(full_state)
    fresh = [r for r in _unlocked_rows(full_state, chara_id, rank, _owned_card_ids(full_state))
             if (chara_id, r["id"]) not in announced]
    _remember(full_state, [(chara_id, r["id"]) for r in fresh])
    return [{"chara_id": chara_id, "data_id": r["id"], "new_flag": 1} for r in fresh]


def chara_profile_array(full_state: dict) -> list:
    charas = full_state.get(collection.CHARA_LIST_KEY)
    love = {c["chara_id"]: (c.get("love_point") or 0)
            for c in (charas if isinstance(charas, list) else [])
            if isinstance(c, dict) and "chara_id" in c}
    owned = _owned_card_ids(full_state)
    seeding = PROFILE_STATE_KEY not in full_state
    announced = _announced(full_state)

    out = []
    fresh = []
    for chara_id in _profile_chara_ids():
        rank = love_rank(love.get(chara_id, 0))
        for row in _unlocked_rows(full_state, chara_id, rank, owned):
            key = (chara_id, row["id"])
            is_new = 0 if (seeding or key in announced) else 1
            if is_new or seeding:
                fresh.append(key)
            out.append({"chara_id": chara_id, "data_id": row["id"], "new_flag": is_new})
    _remember(full_state, fresh)
    return out


def grant(full_state: dict, chara_id: int, points: int) -> tuple:
    info = add_love_point(full_state, chara_id, points)
    return info, new_profile_entries(full_state, chara_id)


def chara_id_for_card(card_id: int) -> int:
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (int(card_id),))
    return int(row["chara_id"]) if row else int(card_id) // 100
