"""Trainee BOND (love_point) -- the per-character affection track on the home
screen, the one that unlocks chara stories, profile entries and the casual
home outfits. NOT the career support-card bond (that lives on chara_info and
dies with the run); this one is account-permanent, per CHARACTER, and is what
chara_list.love_point has always carried.

Nothing ever wrote it before this module: chara_list was served with whatever
love_point the seed account had, campaign_walking pushed its outing points
into love_point_pool only, and every consumer that was already correct --
stories.py's lock_type 6 bond gate, missions/honors' BondLevel families,
dress_data's condition_type 6 casual outfits (client-side, see below) -- sat
frozen behind it.

GROUND TRUTH (captures/, upstream=="real", plus master.mdb):

  THE LADDER. love_rank (13 rows): rank 0..12 at 0/10/22/36/60/100/200/400/
  800/1600/3200/4800/6400 cumulative points. chara_data.love_rank_limit is
  the per-character CAP rank -- 10 (3200 pts) for 65 characters, 12 (6400)
  for 31. love_point stops dead at that cap and the excess goes to
  love_point_pool: chara 1068 (limit 10) sits at love_point 3200 across
  every late capture while her pool climbs 87 -> 102 -> 295 -> 313 -> 608
  -> 623 -> 1427 -> 1477 -> 1478 -> 1493 through careers and outings alike.
  So the pool is OVERFLOW, banked against a future cap raise, not a second
  currency -- which is why campaign_walking's "always the pool" was right
  for the capture it was written from (that account's chara was capped) and
  wrong for everyone else.

  CAREER FINISH (single_mode_live/finish, 10 real completions). One grant
  for the trainee of the run, keyed to the run's final RANK:
      rank 11 (B)   -> 15   x5
      rank 13 (A)   -> 16
      rank 17 (SS)  -> 18   x3
      rank 18 (SS+) -> 18
  single_mode_rank's ids are the letter grades in pairs (1-2 G/G+, 3-4 F/F+,
  ... 17-18 SS/SS+), and those four points fit the plain letter ladder
  G=10, F=11, E=12, D=13, C=14, B=15, A=16, S=17, SS=18 exactly -- see
  career_love_points, which is that ladder. A give-up is worth NOTHING: all
  four force-delete captures (turn 1, 55, 56, 72) show love_point and pool
  both unchanged, so only a run that becomes a legacy pays.

  TEAM TRIALS (team_stadium/all_race_end, 6 real matches). ONE scalar,
  add_love_point, credited to EVERY character on the player's team:
  final_win_type 1 (match won) -> 2, anything else -> 1. Verified by
  differencing captures on the same account: the two won matches moved every
  one of the 15 team members by exactly 2, the four lost ones by 1.

  OUTINGS (campaign_walking) already had its number: campaign_walking_
  location.love_point, a flat 50. It only ever needed the cap/pool split.

  PROFILE ENTRIES (new_chara_profile_array / load's chara_profile_array).
  note_profile: 6 generic rows (chara_id 0) that apply to every trainee --
  id 1 open (lock_type 0), ids 2..6 at lock_type 1, lock_value 1..5, i.e.
  BOND RANK 1 through 5 -- plus per-character rows at lock_type 2, whose
  lock_value is a card_id you must OWN (the alt-costume profile). Confirmed
  entry by entry against a real account's 237-row chara_profile_array: a
  rank-3 chara has ids 1-4, a rank-4 chara 1-5, every rank-5-and-up chara
  1-6, and 1024 (owns card 102402) additionally has id 12, whose lock_value
  is 102402. The one captured grant -- chara 1007, 0 -> 16 points, i.e.
  rank 0 -> rank 1 -- returned exactly [{chara_id 1007, data_id 2,
  new_flag 1}], the rank-1 row.

  CASUAL OUTFITS need no server work at all. dress_data's 32 condition_type
  6 rows (ids 901xxx, use_home 1, one per character) are the casual home
  outfits, and they are NOT in cloth_list on a real account that has long
  since passed every bond rank they could want -- the client unlocks them
  off the bond rank itself. Raising love_point IS the feature.

WHAT IS EXTRAPOLATED (flagged, per this codebase's usual posture):
  * career_love_points above rank 18. No capture exists for an S+/U-rank
    finish, so the ladder CLAMPS at 18 rather than inventing a payout for
    the whole second (UG..USS) letter ladder.
  * A lock_type this module cannot evaluate leaves the profile row LOCKED,
    the same way stories._lock_ok refuses what it cannot prove.
"""

from __future__ import annotations

import functools

from .. import master_data
from . import collection

# Announced (chara_id, data_id) profile rows -- what has already been shown to
# the player with new_flag 1, so nothing is ever announced twice.
PROFILE_STATE_KEY = "chara_profile_state"

# A never-trained owned character's chara_list entry, the real account seed's
# own shape (charas 1016/1045).
CHARA_LIST_TEMPLATE = {"training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                       "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0}

_LOCK_NONE = 0
_LOCK_BOND = 1          # note_profile lock_value = required bond rank
_LOCK_OWN_CARD = 2      # note_profile lock_value = a card_id you must own

# Highest career-finish payout any capture shows (rank 17 and 18 both -> 18).
# See the module docstring: the ladder is clamped here rather than
# extrapolated into the U-rank letters.
_CAREER_LOVE_MAX = 18
_CAREER_LOVE_BASE = 9   # letter index 1 (G) -> 10


# ------------------------------------------------------------ the ladder --

@functools.lru_cache(maxsize=1)
def _thresholds() -> tuple:
    """[(total_point, rank)] descending -- love_rank is 13 static rows."""
    rows = master_data.query(
        "SELECT rank, total_point FROM love_rank ORDER BY total_point DESC")
    return tuple((r["total_point"], r["rank"]) for r in rows)


def love_rank(love_point: int) -> int:
    """Bond LEVEL for a raw love_point total."""
    for threshold, rank in _thresholds():
        if (love_point or 0) >= threshold:
            return rank
    return 0


@functools.lru_cache(maxsize=1024)
def love_cap(chara_id: int) -> int:
    """The point total this character's bond stops at: the love_rank
    threshold of her chara_data.love_rank_limit (10 -> 3200, 12 -> 6400).
    An unknown character gets the top of the ladder rather than 0, so a
    master-data gap can never silently freeze someone's bond at zero."""
    row = master_data.query_one(
        "SELECT love_rank_limit FROM chara_data WHERE id=?", (int(chara_id),))
    limit = (row["love_rank_limit"] if row else None) or max(r for _, r in _thresholds())
    cap = master_data.query_one(
        "SELECT total_point FROM love_rank WHERE rank=?", (int(limit),))
    return int(cap["total_point"]) if cap else max(t for t, _ in _thresholds())


def career_love_points(rank_id: int) -> int:
    """Bond points a completed career pays out, from its final rank id (a
    single_mode_rank row). The letter ladder: G=10 .. B=15, A=16, S=17,
    SS=18, clamped there -- see the module docstring for the captured fits
    and why the clamp."""
    letter_index = (int(rank_id or 0) + 1) // 2          # 17,18 -> 9 (SS)
    if letter_index <= 0:
        return 0
    return min(_CAREER_LOVE_MAX, _CAREER_LOVE_BASE + letter_index)


# ------------------------------------------------------------- the state --

def chara_entry(full_state: dict, chara_id: int) -> dict:
    """This character's chara_list row, created if she has none yet."""
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
    """Credit `points` of bond to one character and report it the way every
    real grant does.

    Returns love_point_info's wire shape {"chara_id", "love_point_before",
    "love_point_after", "love_point_pool_before", "love_point_pool_after"}
    with the cap/overflow split applied: points fill love_point up to
    love_cap(chara_id), the remainder banks in love_point_pool. The caller
    saves the state."""
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


# --------------------------------------------------------- note profiles --

@functools.lru_cache(maxsize=1)
def _profile_rows() -> tuple:
    return tuple({"id": r["id"], "chara_id": r["chara_id"], "lock_type": r["lock_type"],
                  "lock_value": r["lock_value"]}
                 for r in master_data.query(
                     "SELECT id, chara_id, lock_type, lock_value "
                     "FROM note_profile ORDER BY id"))


@functools.lru_cache(maxsize=1024)
def _rows_for(chara_id: int) -> tuple:
    """The note_profile rows that describe THIS character: her own rows, plus
    -- for a TRAINEE only -- the generic chara_id 0 template every trainee
    shares. The 9xxx characters are not trainees and carry exactly one row of
    their own: the real account's array has Tazuna (9001) at data_id 7 and
    nothing else, never the generic 1..6."""
    own = tuple(r for r in _profile_rows() if r["chara_id"] == chara_id)
    if not master_data.is_trainee_chara(chara_id):
        return own
    return tuple(r for r in _profile_rows() if r["chara_id"] == 0) + own


@functools.lru_cache(maxsize=1)
def _profile_chara_ids() -> tuple:
    """Every character the profile screen has rows for: every trainee in
    chara_data -- owned or not, released or not; a real account's array
    carries 1002/1005/... at data_id 1 with no chara_list row at all, and
    unreleased 1043/1047/... too -- plus the non-trainees note_profile names
    outright (Tazuna 9001, the Director 9004, ...)."""
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
    """One note_profile lock. Anything we cannot evaluate stays LOCKED, the
    same posture as stories._lock_ok."""
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
    """The NoteDataForDisplay rows this character has just unlocked and never
    been shown -- new_chara_profile_array. Call AFTER the bond moved; the
    rows returned are recorded as announced, so a second call is empty."""
    entry = chara_entry(full_state, chara_id)
    rank = love_rank(entry.get("love_point") or 0)
    announced = _announced(full_state)
    fresh = [r for r in _unlocked_rows(full_state, chara_id, rank, _owned_card_ids(full_state))
             if (chara_id, r["id"]) not in announced]
    _remember(full_state, [(chara_id, r["id"]) for r in fresh])
    return [{"chara_id": chara_id, "data_id": r["id"], "new_flag": 1} for r in fresh]


def chara_profile_array(full_state: dict) -> list:
    """The whole account's unlocked profile rows, for load/index. Derived
    live from bond ranks and owned cards rather than stored, like every other
    computed container here; only the "already announced" set is persisted,
    so a row the player has seen keeps new_flag 0.

    The first call on an account announces everything already unlocked -- a
    player who has been at bond rank 8 for weeks should not log in to a wall
    of New! badges on profile text she has been reading all along."""
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


# ------------------------------------------------------------- one grant --

def grant(full_state: dict, chara_id: int, points: int) -> tuple:
    """add_love_point + the profile rows it unlocked, in one call. Returns
    (love_point_info, new_chara_profile_array). The caller saves the state."""
    info = add_love_point(full_state, chara_id, points)
    return info, new_profile_entries(full_state, chara_id)


def chara_id_for_card(card_id: int) -> int:
    """card_data.chara_id, with the id-arithmetic fallback trainee cards (and
    only those -- never a support card, see master_data.support_card_chara)
    allow."""
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (int(card_id),))
    return int(row["chara_id"]) if row else int(card_id) // 100
