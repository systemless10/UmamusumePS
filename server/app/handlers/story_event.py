"""Seasonal story event: the bingo/roulette board minigame.

Endpoints (registry-registered; none of these exist in main.py's HANDLERS,
none had a real capture beyond a single story_event/index snapshot --
server/fixtures/capture_06d26c/20260717_124940_340990_story_event_index_incoming.json):
  story_event/index               -> event_id, event_user_info, mission_list
  story_event/roulette             -> current board state
  story_event/roulette_exec        -> spend coins, roll, mark squares, grant
                                       bingo-line rewards, maybe advance sheet
  story_event/roulette_change_sheet-> jump to a specific sheet directly

Ground truth (verified live against master.mdb and dump.cs, NOT guessed):
  story_event_data: story_event_id -> start_date/end_date (raw unix epoch
    ints, unlike mission_data's string dates -- do NOT reuse missions.py's
    _parse_mdb_datetime here). "Active" = the row whose window contains
    servertime, same idea as missions.py's _is_active.
  story_event_roulette_bingo: per (story_event_id, sheet_num) config --
    use_item_category/id/num (the roulette-coin cost per roll, a REAL
    inventory item, not a bespoke currency), reset_line (bingo lines needed
    to clear this sheet), can_loop (the last sheet repeats forever instead
    of advancing), reward_set_id, roulette_max_num -- NOT the board's square
    count (that's a fixed 3x3, user-corrected 2026-08-20; reset_line=8 is
    the tell, since a 3x3 grid has EXACTLY 8 possible lines and a 5x5's 12
    would make 8 an arbitrary partial threshold instead of "every line").
    roulette_max_num is the PITY THRESHOLD (user-confirmed 2026-08-21): for
    the first roulette_max_num (25) spins on a card, each roll is a uniform
    pick across ALL 8 squares WITH replacement (so duplicates -- and thus
    still paying that square's roulette_reward_list prize again -- are
    normal and expected, same as any real "collect all 8" coupon-collector
    roll); only once exec_count reaches that threshold without finishing
    does selection switch to guaranteed-unmarked-square-only, so a card can
    never truly stall on bad luck.
  story_event_bingo_reward: reward_set_id -> one row per line_num (1..
    reset_line), {item_category, item_id, item_num} -- the real reward for
    the Nth bingo line completed on that sheet.
  dump.cs (client reflection, project root) gives the exact wire field
  names for the interactive endpoints -- StoryEventRouletteExecRequest
  {roulette_coin_num, continuous_setting}, StoryEventRouletteExecResponse.
  CommonResponse {order_list, reward_summary_info, roulette_reward_list,
  new_bingo_line_array, bingo_reward_list, change_sheet_num,
  change_roulette_reward_list, roulette_exec_count, roulette_stop_type},
  StoryEventRouletteReward {reward_order, item_category, item_id, item_num}
  (= story_event_bingo_reward, reward_order is line_num), StoryEventBingoData
  {sheet_num, line_num} (a completed-line marker, not a reward payload).

Real feature name (client string table, StoryEvent408001): "Prize Derby".
RouletteDefine (dump.cs) gives the exact enum values used on the wire:
  ContinuousSetting: None=0, Until1LineBingo=1, Until8LineBingo=2, Until10Play=3
  RouletteStopType: None=0, CoinEmpty=1, MaxNum=2, UntilNone=3, PlayCount10=4,
                     OneLineBingo=5, MaxLine=6
  BingoLine: HORIZON_1/2/3, VERTICAL_1/2/3, DIAGONAL_1/2 (8 lines) and
  REWARD_NUMBER_MAX=8 / ORDER_1..8 -- confirms the 3x3 board has exactly 8
  FILLABLE squares, so the 9th (center) is a pre-marked free space, standard
  bingo-card style. String StoryEvent408011 gives the real multi-spin
  interrupt rule verbatim: "All types of multi-spin will be interrupted in
  the following cases: - You run out of Prize Coins - You achieve an 8-Line
  bingo - You reach the spin quota for the current bingo card" -- so EVERY
  continuous_setting mode shares those three interrupts; only the mode's own
  target (1 line / 8 lines / 10 plays) differs.

roulette_reward_list vs bingo_reward_list are confirmed to be genuinely
SEPARATE reward pools (RouletteDerbyView has both a RouletteRewardDetailButton
and a BingoRewardDetailButton -- two distinct detail screens, user-reported
2026-08-21 after seeing the real wheel show different items than the "N-Line
Bingo" list). bingo_reward_list is real (story_event_bingo_reward, granted
per newly-completed line). roulette_reward_list -- the per-spin wheel prize,
granted on every landing regardless of bingo progress -- has NO master.mdb
source at all (checked exhaustively, EN and JP, 416 and 622 tables
respectively -- only story_event_roulette_bingo mentions "roulette" in
either). Its actual VALUES (_ROULETTE_REWARD_TABLE below) are hardcoded from
a real screenshot the user provided (2026-08-21, "Card 4" -- the
repeatable/endless sheet), matched to real item_data rows.

REAL CAPTURE (2026-08-21, captures/20260821_165708/, --upstream real proxy
against the actual Cygames server -- 0020-0025) corroborated
_ROULETTE_REWARD_TABLE exactly and additionally revealed several wire details
that were wrong in the first implementation, all fixed below:
  - user_roulette_order_list / order_list are NOT a 9-slot board snapshot --
    they're a list of ACHIEVED ORDER NUMBERS (1-8). The index endpoint's
    list is the full accumulated set for the current sheet (e.g. a real
    response showed [1,3,4,5,6,7,8], meaning only order 2 was still
    unmarked); the exec endpoint's list is only the order numbers landed on
    THAT call, in roll sequence, duplicates included (e.g. a 10-play call's
    order_list was [4,7,4,5,4,8,6,4,7,1]).
  - Sheets keep incrementing PAST the last master-data row forever (a real
    account was on sheet_num 5, then advanced to 6) reusing that last row's
    config (reward_set_id, reset_line, use_item_*, roulette_max_num) every
    time -- NOT capped/refused once past the defined range, which the
    original _sheet_row lookup did (a real bug: every card past 4 would
    have 205-refused everything).
  - bingo_reward_list / roulette_reward_list in the EXEC response use field
    name item_type (DisplayRewardInfo's real field), not item_category --
    that field name is only correct on the INDEX/change-sheet responses'
    StoryEventRouletteReward-shaped lists (reward_order/item_category).
  - new_bingo_line_array reports the LINE's true geometric BingoLine index
    (1-8: HORIZON_1-3=1-3, VERTICAL_1-3=4-6, DIAGONAL_1-2=7-8, matching
    _BOARD_LINES' own row/col/diagonal build order 1:1 once 1-indexed), in
    the order lines complete during the call -- NOT the sequential
    completion count. Reward LOOKUP is still by sequential completion count
    (story_event_bingo_reward.line_num = the Nth line ever completed this
    sheet) -- verified directly: a real call reporting new_bingo_line_array
    [8,4,3,7,2,6] (six geometrically-identified lines) paired with
    bingo_reward_list entries matching reward_set_id 7's line_num 1-6 in
    order, not line_num 8/4/3/7/2/6. Both facts came from independently
    reconstructing the same real roll sequence from its raw order_list and
    checking which lines the board geometry actually completes when.
  - bingo_info is one entry per DEFINED master sheet_num (not per-instance):
    fully-cleared one-time sheets report line_num = their reset_line; the
    repeatable last sheet reports a single persistent entry whose line_num
    keeps growing across every loop (never resets), unlike the per-loop
    reward-granting counter which does reset each clear. This part is a
    best-effort model, not fully capture-proven -- it's a display-only
    field with no observed effect on grants.
"""

from __future__ import annotations

import functools
import random

from .. import master_data
from .. import patch
from .. import state as state_store
from . import missions, registry, shop
from .stories import _grant_reward

EVENT_STATE_KEY = "story_event_state"

# 3x3 board (positions 0-8): 3 rows, 3 cols, 2 diagonals = 8 lines total,
# matching reset_line=8 exactly (user-corrected 2026-08-20; RouletteDefine.
# BingoLine confirms 8 named lines: HORIZON_1-3, VERTICAL_1-3, DIAGONAL_1-2).
_BOARD_SIZE = 9
_FREE_CENTER = 4   # REWARD_NUMBER_MAX=8 / ORDER_1..8 -- only 8 squares are
                   # ever landed on, so the center starts pre-marked (free).
_BOARD_LINES: tuple = tuple(
    [tuple(r * 3 + c for c in range(3)) for r in range(3)] +      # rows
    [tuple(r * 3 + c for r in range(3)) for c in range(3)] +      # cols
    [tuple(i * 3 + i for i in range(3)),                          # \ diagonal
     tuple(i * 3 + (2 - i) for i in range(3))])                   # / diagonal

# Board position (flat 0-8, row-major, 4=free center) -> the wheel's "Nth"
# order label -- read straight off the user's real screenshot of the bingo-
# card popup (2026-08-21), which lays the 8 slots out as:
#   5th  2nd  6th
#   3rd FREE  1st
#   4th  8th  7th
_POSITION_TO_ORDER = {0: 5, 1: 2, 2: 6, 3: 3, 5: 1, 6: 4, 7: 8, 8: 7}
_LANDABLE_POSITIONS = tuple(_POSITION_TO_ORDER)   # the 8 non-center squares

# The real per-square "roulette" prize table (item + quantity per wheel
# order 1st-8th), user-confirmed 2026-08-21 from a live screenshot of
# "Card 4" -- the repeatable/endless sheet (can_loop=1): its OWN bingo-
# MILESTONE table (reward_set_id 7) pays out in these same three
# currencies, corroborating this is that card. No master.mdb source exists
# for this table at all (checked exhaustively, EN 416 + JP 622 tables) --
# genuinely hardcoded from a real capture, not guessed. item_id/category
# resolved against text_data/item_data (Support Points id=110 cat=30,
# Friend Points id=98 cat=103, Monies id=59 cat=91, Medium/Dirt Racing
# Shoes id=7/13 cat=11). NOT YET CONFIRMED to be the same pool on the
# non-endless sheets (1-3) -- reused here as the best available guess until
# a capture of one of those proves it right or wrong.
_ROULETTE_REWARD_TABLE = [
    {"item_category": 30, "item_id": 110, "item_num": 100},   # 1st: Support Points
    {"item_category": 103, "item_id": 98, "item_num": 50},    # 2nd: Friend Points
    {"item_category": 91, "item_id": 59, "item_num": 200},    # 3rd: Monies
    {"item_category": 30, "item_id": 110, "item_num": 50},    # 4th: Support Points
    {"item_category": 103, "item_id": 98, "item_num": 20},    # 5th: Friend Points
    {"item_category": 91, "item_id": 59, "item_num": 100},    # 6th: Monies
    {"item_category": 11, "item_id": 13, "item_num": 1},      # 7th: Dirt Racing Shoes
    {"item_category": 11, "item_id": 7, "item_num": 1},       # 8th: Medium Racing Shoes
]

# RouletteDefine.ContinuousSetting (dump.cs) -- real wire values.
_CONTINUOUS_NONE = 0
_CONTINUOUS_UNTIL_1_LINE = 1
_CONTINUOUS_UNTIL_8_LINE = 2
_CONTINUOUS_UNTIL_10_PLAY = 3

# RouletteDefine.RouletteStopType (dump.cs) -- real wire values. MaxNum (2)
# is "the current run started below roulette_max_num and reached it without
# finishing" -- the "spin quota for the current bingo card" StoryEvent408011
# names as a universal interrupt; see handle_roulette_exec's started_pre_pity
# check (user-corrected 2026-08-21: hitting the threshold mid-run must STOP
# there, not seamlessly switch to guaranteed picks and keep going).
_STOP_NONE = 0
_STOP_COIN_EMPTY = 1
_STOP_MAX_NUM = 2
_STOP_UNTIL_NONE = 3
_STOP_PLAY_COUNT_10 = 4
_STOP_ONE_LINE_BINGO = 5
_STOP_MAX_LINE = 6


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


@functools.lru_cache(maxsize=1)
def _all_events() -> tuple:
    return tuple(master_data.query("SELECT * FROM story_event_data"))


def _active_event_id(now_ts: int) -> int | None:
    for row in _all_events():
        if row["start_date"] <= now_ts <= row["end_date"]:
            return row["story_event_id"]
    return None


@functools.lru_cache(maxsize=32)
def _sheets(story_event_id: int) -> tuple:
    return tuple(master_data.query(
        "SELECT * FROM story_event_roulette_bingo WHERE story_event_id=? "
        "ORDER BY sheet_num", (story_event_id,)))


def _sheet_row(story_event_id: int, sheet_num: int):
    """The CONFIG row to use for `sheet_num` -- an exact match if one is
    defined, else the last (can_loop) row once sheet_num has run past the
    defined range, since sheet_num itself keeps incrementing forever while
    master data only ever defines a handful of rows (real capture: a
    genuine account was on sheet_num 5/6, reusing sheet 4's row both
    times -- capping/refusing past the last row, as an earlier version did,
    made every card past the story ones 205-refuse everything)."""
    sheets = _sheets(story_event_id)
    exact = next((s for s in sheets if s["sheet_num"] == sheet_num), None)
    if exact is not None:
        return exact
    last = sheets[-1] if sheets else None
    return last if last is not None and sheet_num > last["sheet_num"] and last["can_loop"] else None


def _last_defined_sheet_num(story_event_id: int) -> int:
    sheets = _sheets(story_event_id)
    return sheets[-1]["sheet_num"] if sheets else 0


@functools.lru_cache(maxsize=64)
def _bingo_rewards(reward_set_id: int) -> tuple:
    return tuple(master_data.query(
        "SELECT * FROM story_event_bingo_reward WHERE reward_set_id=? "
        "ORDER BY line_num", (reward_set_id,)))


def _reward_preview(reward_set_id: int) -> list:
    return [{"reward_order": r["line_num"], "item_category": r["item_category"],
             "item_id": r["item_id"], "item_num": r["item_num"]}
            for r in _bingo_rewards(reward_set_id)]


def _roulette_reward_preview() -> list:
    """The wheel's own preview list (StoryEventRouletteReward-shaped:
    reward_order/item_category/item_id/item_num), sourced from
    _ROULETTE_REWARD_TABLE -- NOT _reward_preview/story_event_bingo_reward,
    which is the separate bingo-milestone table (user-corrected 2026-08-21
    after the wheel graphic regressed back to showing bingo rewards; this
    is the board-state endpoint's own reward_list, distinct from the exec
    endpoint's per-spin grant, which already used the right table)."""
    return [{"reward_order": i + 1, **_ROULETTE_REWARD_TABLE[i]}
            for i in range(len(_ROULETTE_REWARD_TABLE))]


def _fresh_board() -> list:
    board = [0] * _BOARD_SIZE
    board[_FREE_CENTER] = 1
    return board


def _event_state(full_state: dict, story_event_id: int) -> tuple[dict, bool]:
    """(state, first_access) -- state is seeded once per event id and mutated
    in place; caller saves full_state when done."""
    store = full_state.setdefault(EVENT_STATE_KEY, {})
    key = str(story_event_id)
    first_access = key not in store
    st = store.setdefault(key, {
        "current_sheet": 1, "board": _fresh_board(),
        "lines_done": [], "roulette_exec_count": 0,
        "bingo_history": {},   # {str(sheet_num): line_num} -- see module docstring
    })
    return st, first_access


def _completed_lines(board: list) -> set:
    return {i for i, line in enumerate(_BOARD_LINES) if all(board[p] for p in line)}


def _order_numbers(board: list) -> list:
    """The board's marked squares as ORDER NUMBERS (1-8), sorted -- this is
    the real wire shape for user_roulette_order_list (real capture:
    [1,3,4,5,6,7,8], NOT a 9-slot 0/1 snapshot)."""
    return sorted(_POSITION_TO_ORDER[p] for p in _LANDABLE_POSITIONS if board[p])


def _event_user_info(full_state: dict, story_event_id: int, sheet_num: int) -> dict:
    return {"event_point": full_state.get("story_event_point", {}).get(str(story_event_id), 0),
            "roulette_coin_num": _coin_count(full_state, story_event_id, sheet_num),
            "roulette_sheet_num": sheet_num}


def _coin_count(full_state: dict, story_event_id: int, sheet_num: int) -> int:
    row = _sheet_row(story_event_id, sheet_num)
    if not row:
        return 0
    return shop._item_count(full_state, row["use_item_id"])


def _bingo_info_list(st: dict) -> list:
    return [{"sheet_num": int(k), "line_num": v}
            for k, v in sorted(st["bingo_history"].items(), key=lambda kv: int(kv[0]))]


def _advance_if_cleared(full_state, story_event_id: int, st: dict, row) -> tuple:
    """If the FULL board (all 8 lines -- see handle_roulette_exec's own
    comment on why this ignores row["reset_line"]) has been reached, move to
    the next sheet (or loop the current one if it's the can_loop/last sheet
    -- sheet_num keeps incrementing forever past it; see _sheet_row).
    Returns (change_sheet_num, change_roulette_reward_list) -- (None, []) if
    nothing changed."""
    if len(st["lines_done"]) < len(_BOARD_LINES):
        return None, []
    last_num = _last_defined_sheet_num(story_event_id)
    key = str(row["sheet_num"])
    if row["can_loop"] and row["sheet_num"] >= last_num:
        # The repeatable sheet's bingo_info entry stays keyed at ITS OWN
        # fixed master sheet_num and accumulates across every loop --
        # real capture showed bingo_info pinned at sheet_num 4 while the
        # DISPLAYED roulette_sheet_num climbed to 5, then 6.
        st["bingo_history"][key] = st["bingo_history"].get(key, 0) + len(st["lines_done"])
    else:
        st["bingo_history"][key] = len(st["lines_done"])
    st["current_sheet"] = row["sheet_num"] + 1
    st["board"] = _fresh_board()
    st["lines_done"] = []
    st["roulette_exec_count"] = 0   # "Spin 1" on a new card, not a running total
    # change_roulette_reward_list is the WHEEL's preview for the new card
    # (same field family as roulette_reward_list -- user-corrected
    # 2026-08-21), not the bingo-milestone one.
    return st["current_sheet"], _roulette_reward_preview()


@registry.endpoint("story_event/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = patch._servertime()
    event_id = _active_event_id(now_ts)
    if event_id is None:
        return _ok({"event_id": 0, "event_user_info": {}, "mission_list": []})
    st, _ = _event_state(full_state, event_id)
    data = {
        "event_id": event_id,
        "event_user_info": _event_user_info(full_state, event_id, st["current_sheet"]),
        "mission_list": missions.build_mission_list(viewer_id, full_state),
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(data)


@registry.endpoint("story_event/roulette")
def handle_roulette(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse()
    st, first_access = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None:
        return _refuse()
    data = {
        "event_user_info": _event_user_info(full_state, event_id, st["current_sheet"]),
        "user_roulette_order_list": _order_numbers(st["board"]),
        "roulette_reward_list": _roulette_reward_preview(),
        "bingo_info": _bingo_info_list(st),
        "first_access_flag": first_access,
        "roulette_exec_count": st["roulette_exec_count"],
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(data)


# Until1LineBingo/Until8LineBingo have no fixed quota of their own -- they
# run until their own condition (or a universal interrupt) fires. With
# duplicates allowed pre-pity, finishing a card can genuinely take well
# past 8 rolls (coupon-collector odds), so the safety cap has to clear the
# pity threshold plus room for the guaranteed tail, not just the square
# count -- this is a backstop that should never actually be the reason a
# run stops.
_SAFETY_ROLL_CAP = 64
_CONTINUOUS_MAX_ROLLS = {_CONTINUOUS_NONE: 1, _CONTINUOUS_UNTIL_10_PLAY: 10}


@registry.endpoint("story_event/roulette_exec")
def handle_roulette_exec(payload: dict) -> dict:
    """Real interrupt rule (StoryEvent408011, verbatim): a multi-spin run
    stops on whichever comes first -- out of coins, the FULL 8-line bingo
    completes, or the mode's own spin quota is hit. continuous_setting picks
    the mode/quota (RouletteDefine.ContinuousSetting); Until1LineBingo adds
    ITS OWN extra stop the moment any single new line completes, on top of
    those three universal ones."""
    viewer_id = payload["viewer_id"]
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st, _ = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None:
        return _refuse()

    mode = int(payload.get("continuous_setting") or _CONTINUOUS_NONE)
    # A card always needs the FULL board (all 8 lines) to clear -- NOT
    # row["reset_line"] literally. Sheets 1-3 do say reset_line=8 (matches),
    # but the repeatable sheet's row says reset_line=1 despite its own
    # reward_set_id having all 8 line_num tiers defined, and the real
    # capture showed 3-6 NEW lines completing per call on that exact sheet
    # before it stopped/advanced -- never just 1 (user-reported 2026-08-21:
    # "board past 4+ only draw until 1 bingo" instead of the full board like
    # 1-3). reset_line is still used for master-data bookkeeping elsewhere;
    # only the "is this card cleared" threshold ignores it.
    reset_line = len(_BOARD_LINES)
    cost_each = row["use_item_num"] or 1
    pity_threshold = row["roulette_max_num"] or 25
    max_rolls = _CONTINUOUS_MAX_ROLLS.get(mode, _SAFETY_ROLL_CAP)
    if shop._item_count(full_state, row["use_item_id"]) < cost_each:
        return _refuse()  # can't even afford a single spin

    # Whether THIS call starts below the pity threshold. If so, crossing it
    # mid-call is its own hard stop (RouletteStopType.MaxNum) -- user-
    # corrected 2026-08-21: reaching roulette_max_num without finishing ends
    # the run right there, it does NOT seamlessly switch to guaranteed
    # picks and keep going within the same call. A call that already starts
    # at/past the threshold (a later, separate spin) has no such stop --
    # pity is already active for its whole duration.
    started_pre_pity = st["roulette_exec_count"] < pity_threshold

    summary = shop._empty_summary()
    order_list = []
    roulette_reward_list = []
    new_bingo_line_array = []
    bingo_reward_list = []
    rolled = 0
    stop_type = _STOP_NONE
    while rolled < max_rolls:
        unmarked = [i for i in _LANDABLE_POSITIONS if not st["board"][i]]
        if not unmarked:
            stop_type = _STOP_MAX_LINE  # board already full from a prior roll
            break
        if shop._item_count(full_state, row["use_item_id"]) < cost_each:
            stop_type = _STOP_COIN_EMPTY
            break
        shop._add_item(full_state, row["use_item_id"], -cost_each)
        # Pre-pity: any of the 8 squares, duplicates allowed (coupon-collector
        # odds -- see module docstring). Once exec_count reaches the card's
        # own pity threshold, force an unmarked one so the card can't stall.
        if st["roulette_exec_count"] >= pity_threshold:
            landed = random.choice(unmarked)
        else:
            landed = random.choice(_LANDABLE_POSITIONS)
        st["board"][landed] = 1
        st["roulette_exec_count"] += 1
        rolled += 1
        order_list.append(_POSITION_TO_ORDER[landed])

        # The per-square wheel prize -- granted immediately on landing,
        # every spin, independent of bingo progress (user-confirmed
        # mechanic; see _ROULETTE_REWARD_TABLE). DisplayRewardInfo's real
        # field is item_type, not item_category (real capture, 2026-08-21).
        prize = _ROULETTE_REWARD_TABLE[_POSITION_TO_ORDER[landed] - 1]
        _grant_reward(full_state, prize["item_category"], prize["item_id"],
                      prize["item_num"], summary, viewer_id)
        roulette_reward_list.append({"item_type": prize["item_category"],
                                     "item_id": prize["item_id"], "item_num": prize["item_num"]})

        completed_line_indexes = sorted(_completed_lines(st["board"]) - set(st["lines_done"]))
        completed_a_new_line = bool(completed_line_indexes)
        for line_idx in completed_line_indexes:
            if len(st["lines_done"]) >= reset_line:
                break
            st["lines_done"].append(line_idx)
            # Reward lookup is by SEQUENTIAL completion count (the Nth line
            # this sheet has ever completed); new_bingo_line_array reports
            # the line's true geometric BingoLine id (1-8) instead -- these
            # are DIFFERENT numbers, both real-capture-verified (see module
            # docstring).
            line_num = len(st["lines_done"])
            reward_row = next((r for r in _bingo_rewards(row["reward_set_id"])
                               if r["line_num"] == line_num), None)
            new_bingo_line_array.append(line_idx + 1)
            if reward_row is None:
                continue
            _grant_reward(
                full_state, reward_row["item_category"], reward_row["item_id"],
                reward_row["item_num"], summary, viewer_id)
            bingo_reward_list.append({"item_type": reward_row["item_category"],
                                      "item_id": reward_row["item_id"],
                                      "item_num": reward_row["item_num"]})

        if len(st["lines_done"]) >= reset_line:
            stop_type = _STOP_MAX_LINE
            break
        if mode == _CONTINUOUS_UNTIL_1_LINE and completed_a_new_line:
            stop_type = _STOP_ONE_LINE_BINGO
            break
        if started_pre_pity and st["roulette_exec_count"] >= pity_threshold:
            stop_type = _STOP_MAX_NUM
            break
    else:
        # Reached max_rolls without an early interrupt.
        stop_type = {_CONTINUOUS_UNTIL_10_PLAY: _STOP_PLAY_COUNT_10,
                    _CONTINUOUS_NONE: _STOP_UNTIL_NONE}.get(mode, _STOP_NONE)

    change_sheet_num, change_reward_list = _advance_if_cleared(
        full_state, event_id, st, row)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "order_list": order_list,
        "reward_summary_info": summary,
        "roulette_reward_list": roulette_reward_list,
        "new_bingo_line_array": new_bingo_line_array,
        "bingo_reward_list": bingo_reward_list,
        "change_sheet_num": change_sheet_num,
        "change_roulette_reward_list": change_reward_list,
        "roulette_exec_count": st["roulette_exec_count"],
        "roulette_stop_type": stop_type,
    })


@registry.endpoint("story_event/roulette_change_sheet")
def handle_roulette_change_sheet(payload: dict) -> dict:
    """The "Exchange" button (user-confirmed 2026-08-21): only usable once
    you're ALREADY on the repeatable/endless card (can_loop) -- the story
    cards (1-3) must be played through, not skipped. Rerolls the SAME
    repeatable card fresh; there's nowhere else valid to "exchange" to."""
    viewer_id = payload["viewer_id"]
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st, _ = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None or not row["can_loop"]:
        return _refuse()
    st["board"] = _fresh_board()
    st["lines_done"] = []
    st["roulette_exec_count"] = 0
    state_store.save_state(viewer_id, full_state)
    return _ok({
        # st["current_sheet"], NOT row["sheet_num"] -- row is the capped
        # CONFIG row (e.g. always 4), but the DISPLAYED sheet number keeps
        # incrementing past it (real capture: 5, then 6, ...).
        "change_sheet_num": st["current_sheet"],
        "change_roulette_reward_list": _roulette_reward_preview(),
    })


# --------------------------------------------------------------------------
# dump.cs shapes:
#   StoryEventAnnounceRequest       {}  -> {content}
#   StoryEventReceiveMissionRequest {mission_id_array}
#     -> {reward_summary_info, updated_mission_array, add_event_point,
#         add_roulette_coin_num, new_story_id_list, get_all_point_reward}


@functools.lru_cache(maxsize=None)
def _max_point_threshold(story_event_id: int) -> int:
    """Top rung of this event's point-reward ladder (story_event_point_reward),
    or 0 when the event has no ladder at all. Cached -- master.mdb is read-only
    for the process lifetime, same as the other lookups in this module."""
    row = master_data.query_one(
        "SELECT MAX(point) AS p FROM story_event_point_reward WHERE story_event_id=?",
        (story_event_id,))
    return (row["p"] if row and row["p"] else 0)


def _all_point_rewards_reached(story_event_id: int, event_point: int) -> bool:
    top = _max_point_threshold(story_event_id)
    return bool(top) and event_point >= top


@registry.endpoint("story_event/announce")
def handle_announce(payload: dict) -> dict:
    """story_event/announce -- the event's in-game announcement text.

    Serves an EMPTY string, and that is the honest answer rather than a
    shortfall: `content` is authored announcement prose, and this build's
    master.mdb has no per-story_event text to draw it from. text_data has
    exactly one announcement-shaped family (categories 279-289) and its rows
    are one-off holiday-concert copy keyed to a single id, not the 19
    story_event_data rows (1001-1019) -- so there is nothing to look up, and
    writing a plausible-sounding banner would be inventing game content.

    Still worth implementing over main.py's NOOP_SUCCESS: that fallback has no
    `data` key at all, so `content` reaches the client as a NULL string rather
    than an empty one. This project has already been bitten by exactly that
    distinction -- see cards.handle_get_release_card_array, where a missing
    key deserialized to null and NullRef'd the scout screen.

    Refuses when no event is running: there is no announcement to show."""
    if _active_event_id(patch._servertime()) is None:
        return _refuse()
    return _ok({"content": ""})


@registry.endpoint("story_event/receive_mission")
def handle_receive_mission(payload: dict) -> dict:
    """story_event/receive_mission -- claim the event's own mission rewards.

    Claiming runs through missions.claim_missions, the SAME gate and grant
    path mission/receive uses, because an event mission is an ordinary
    mission_data row -- it is only the response that differs. The ids are not
    additionally filtered to "this event's" missions: mission_data.event_id
    (values 3..149 here) does not join to story_event_data.story_event_id
    (1001..1019) by any relation this project has established, so a filter
    would have to guess and would reject real event missions. The per-mission
    condition gate is what actually decides whether a claim is legitimate, and
    it is unchanged.

    The four extra response fields are MEASURED across the grant rather than
    derived from a reward-category map this project has not reverse-
    engineered: event points and roulette coins are both read before and after
    claiming and reported as the delta, so they are exact whatever category
    the reward used. new_story_id_list comes from the summary's own
    add_story_id_array (shop._grant fills it when a reward unlocks a story).

    get_all_point_reward is real: story_event_point_reward is the cumulative
    "reach N points" ladder (keyed story_event_id -> point threshold, 107 rows
    for the currently-live event), so "collected everything on the track" is
    simply the account's event_point having reached the HIGHEST threshold for
    this event. An event with no ladder rows at all reports False rather than
    vacuously True -- there is nothing to have finished."""
    viewer_id = payload["viewer_id"]
    ids = payload.get("mission_id_array")
    if not isinstance(ids, list) or not ids:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse()
    st, _ = _event_state(full_state, event_id)
    sheet_num = st["current_sheet"]

    points_before = (full_state.get("story_event_point") or {}).get(str(event_id), 0)
    coins_before = _coin_count(full_state, event_id, sheet_num)

    summary = shop._empty_summary()
    updated = missions.claim_missions(viewer_id, full_state, ids, summary)
    if updated is None:
        return _refuse()

    points_after = (full_state.get("story_event_point") or {}).get(str(event_id), 0)
    coins_after = _coin_count(full_state, event_id, sheet_num)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "updated_mission_array": updated,
        "add_event_point": points_after - points_before,
        "add_roulette_coin_num": coins_after - coins_before,
        "new_story_id_list": list(summary.get("add_story_id_array") or []),
        "get_all_point_reward": _all_point_rewards_reached(event_id, points_after),
    })
