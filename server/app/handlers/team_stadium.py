"""Team Stadium (Team Trials) -- the 5-distance x 3-member roster ladder,
now with real matchmaking + a real race.

  team_stadium/index              -> current roster, class, and ranking
  team_stadium/team_edit          -> set the roster (real evaluation_point)
  team_stadium/opponent_list      -> candidate opponents, drawn from REAL
                                      other accounts on this server
  team_stadium/decide_frame_order -> lock in the chosen opponent + gates
  team_stadium/start              -> run all 5 races for real, score them
  team_stadium/replay_check       -> per-round "finished watching" pacing
  team_stadium/all_race_end       -> close the match: totals, class
                                      promotion/demotion, payout
  team_stadium/user_detail        -> echo the player's own team/roster
  team_stadium/ranking            -> empty/self-only (no other real
                                      leaderboard exists on this server)

NEVER CAPTURED before this session (index/team_edit only); ground truth for
those two is captures/20260817_093008/0010_team_stadium_index.json and
0011_team_stadium_team_edit.json (real server, an established account --
team_class 5, best_point 807802). Every other endpoint here (opponent_list
onward) has ZERO real capture -- built from dump.cs's wire shapes
(TeamStadiumOpponent, TeamStadiumFrameOrder, TeamStadiumRaceStartParams,
TeamStadiumRaceResult, TeamStadiumRaceCharaResult, TeamStadiumScoreData/
ResultScoreData, TeamStadiumResultBonusData, TeamStadiumTotalScoreInfo,
TeamStadiumWinningRewardInfo/Content, TeamStadiumDefine's ProperType/TeamRank
enums) plus master.mdb's real scoring tables (team_stadium_raw_score,
team_stadium_score_bonus, team_stadium_evaluation_rate, team_stadium_class,
team_stadium_class_reward). Every inferred (as opposed to capture-confirmed)
design choice is commented at the point it's made -- this is a first real
implementation of an uncharted feature, not a capture-verified one.

team_edit request: team_data_array, one entry per (distance_type 1-5,
member_id 1-3) slot -- {distance_type, member_id, trained_chara_id,
running_style} -- plus team_evaluation_point (now IGNORED -- see
_team_evaluation_point below; kept accepted in the request for wire
compatibility but no longer trusted). distance_type here is Team Stadium's
own 5-way split, NOT the general 4-way short/mile/middle/long used
elsewhere (single_mode_team._DISTANCE_TYPE, race_simulator.py) -- the first
capture unambiguously showed 5 groups of 3, so that wider range is trusted
over the narrower one used elsewhere. A request may submit any subset of
the 15 valid (distance_type, member_id) pairs; an EMPTY slot (no horse
assigned to it yet) is sent as trained_chara_id=0 paired with
running_style=0.
team_edit response (capture): {before_rank, after_rank, reward_info_array}.

OPPONENTS come from REAL other accounts on this server (user-directed
2026-08-24, not fabricated players): every OTHER viewer_id with a submitted
team_stadium roster is a candidate, matched by distance from the requesting
player's own (freshly recomputed, never their stored claim) evaluation_point.
Falls back to synthesized mobs (single_mode_team._mob_opponents' rescale
pattern) only when too few real candidates exist.
"""

from __future__ import annotations

import functools
import logging
import random
from datetime import datetime, timezone
from datetime import time as _dt_time

log = logging.getLogger("uma-server")

from .. import master_data
from .. import patch
from .. import rating_formula
from .. import state as state_store
from ..simulation import race_simulator
from . import (bond, practice_race, presents, registry, single_mode_team, stamina,
               trained_chara)

TEAM_STADIUM_STATE_KEY = "team_stadium_state"
_VALID_DISTANCE_TYPES = range(1, 6)
_VALID_MEMBER_IDS = range(1, 4)
_VALID_RUNNING_STYLES = range(1, 6)
_STATS = ("speed", "stamina", "power", "guts", "wiz")


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _team_stadium_state(full_state: dict) -> dict:
    st = full_state.get(TEAM_STADIUM_STATE_KEY)
    if isinstance(st, dict):
        return st
    st = {
        "team_data_array": [],
        "team_class": 1, "best_team_class": 1,
        # best_point: this WEEK's Team Trial score (leaderboard/ranking.rank
        # comes from this) -- built ONLY from actual race results (skills,
        # placement, opponent's rank/rating, etc. -- see final_total in
        # handle_all_race_end), never from the roster-strength preview below.
        # User-corrected 2026-08-25: these are two completely different
        # numbers on the real client; a real account's best_point sat at
        # 1,010,001 while its roster's own evaluation was ~320,000.
        "best_point": 0,
        # team_evaluation_point: CURRENT fielded roster's live strength
        # (recomputed on every team_edit) -- what before_rank/after_rank and
        # the home "Team Rank" badge/Daily-Program gate track, confirmed by
        # real capture (team_edit's before/after moved in lockstep with THIS
        # value while best_point sat untouched the entire session).
        "team_evaluation_point": 0,
        # evaluation_peak: highest team_evaluation_point ever fielded --
        # SEPARATE from best_point, used only to gate one-time
        # team_stadium_rank tier-crossing rewards (granted the first time a
        # configured roster reaches a new tier, same team_edit response that
        # carries before_rank/after_rank).
        "evaluation_peak": 0,
        "rank": 0,
        "granted_rank": 0,   # highest team_stadium_rank.id already rewarded --
                             # id 0 exists below every real row (min id is 1),
                             # so nothing reads as pre-granted on a fresh account
        "consecutive_win_count": 0,
        "current_match": None,   # in-progress match: opponent + frame orders +
                                  # round results, cleared by all_race_end
    }
    full_state[TEAM_STADIUM_STATE_KEY] = st
    return st


def _rank_for(evaluation_point: int):
    """Highest team_stadium_rank row this evaluation point reaches, or None
    below every tier's team_min_value (real rows start at 1). This is the
    roster-strength tier (team_evaluation_point / evaluation_peak) -- NOT
    the weekly TT score (best_point), a completely different number on the
    real client (user-corrected 2026-08-25)."""
    return master_data.query_one(
        "SELECT * FROM team_stadium_rank WHERE team_min_value <= ? "
        "ORDER BY team_min_value DESC LIMIT 1", (evaluation_point,))


@functools.lru_cache(maxsize=1)
def _rank_table_ceiling() -> int:
    """Largest evaluation point the team_stadium_rank table has a band for
    (the top row's team_max_value -- 99,999,999 on the shipped master.mdb)."""
    row = master_data.query_one("SELECT MAX(team_max_value) AS m FROM team_stadium_rank")
    return int(row["m"]) if row and row["m"] else 0


def rank_display_point(st: dict) -> int:
    """The number load/index must put in user_info.best_team_evaluation_point.

    Two things this gets right that feeding it best_point straight did not:

    1. It is the ROSTER-STRENGTH peak (evaluation_peak), not the weekly Team
       Trial score (best_point) -- see _team_stadium_state for why those are
       two unrelated numbers. Only the former has a team_stadium_rank band.

    2. It is CLAMPED to the rank table's top band. TeamStadiumUtil.GetTeamRank
       (dump.cs 565066) resolves a point to a rank by scanning
       team_stadium_rank for a row with team_min_value <= point <=
       team_max_value -- a point ABOVE the last row's team_max_value matches
       nothing and comes back as TeamRank 0, i.e. BELOW rank 1. That is what
       relocked "Daily Program" (need_team_rank_play_daily_race = 2 = Team
       Rank E, PartsHomeDailyProgramButton.IsLockContentDailyProgram) on an
       account whose roster is strong enough to sit at the top of the ladder:
       225,193,828 > 99,999,999, so the client read it as no rank at all.
       _rank_for's own server-side lookup never hit this because it filters on
       team_min_value alone."""
    ceiling = _rank_table_ceiling()
    peak = int(st.get("evaluation_peak") or 0)
    return min(peak, ceiling) if ceiling else peak


def _grant_rank_rewards(full_state: dict, st: dict) -> list:
    """MAIL every team_stadium_rank tier newly reached by evaluation_peak (the
    roster-strength peak, NOT the weekly best_point score) to the present box.

    CHANGED 2026-09-03 (user request). This REVERSES the 2026-08-19 correction
    that credited these directly -- that note was about the Team Trials WIN
    boxes, which still pay out directly; only this roster-strength tier payout
    moves to mail.

    Returns [] rather than the crossed tiers so `reward_info_array` stays
    empty: the client applies that array to its own cached wallet/item counts,
    and a tier sitting unclaimed in the mailbox has not been granted yet.

    NB presents.send (not admin_send): the callers own full_state and save it."""
    granted = st.get("granted_rank") or 0
    best_row = _rank_for(st.get("evaluation_peak") or 0)
    target = best_row["id"] if best_row else 0
    if target <= granted:
        return []
    for row in master_data.query(
            "SELECT * FROM team_stadium_rank WHERE id > ? AND id <= ? ORDER BY id",
            (granted, target)):
        cat, iid, num = row["item_category"] or 0, row["item_id"] or 0, row["item_num"] or 0
        if not num:
            continue
        presents.send(full_state, cat, iid, num,
                      message=f"Team Rank {row['id']} reward")
    st["granted_rank"] = target
    return []


# ======================================================= winning rewards ===
# dump.cs TeamStadiumDefine.RewardBoxColorType: Gold=1, Red=2, White=3, and
# GetWinningRewardSprite() indexes straight off it. Team Trials only ever
# hands out Gold and Red in the live game (user-confirmed), so White is never
# emitted here.
#
# BUG FIXED 2026-08-28 (the "blue presents"): box_color_type used to be fed
# team_stadium_class_reward.class_reward_type, which runs 1-4 and is NOT a
# colour at all -- per dump.cs's DialogTeamStadiumRewardItemList.
# TeamStadiumClassRewardType it means Promoted=1 / RePromoted=2 / Residual=3 /
# Relegated=4. Rows 3 and 4 therefore asked the client for colour 3 (White)
# and colour 4 (off the end of the enum entirely), which is what rendered as
# an unknown/blue box.
_BOX_GOLD, _BOX_RED = 1, 2

# INFERRED contents. master.mdb has NO Team Trials win-box drop table (only
# team_stadium_class_reward, the end-of-term promotion payout, and
# team_stadium_rank, the one-time roster-strength tier payout) -- the real
# server rolls these itself. Kept deliberately small, in the same two
# currencies the class table already uses (item_category 90 = carats,
# item_category 103 item 98 = friend points), and scaled by team_class so
# higher classes are worth more. Tune here.
_WIN_BOX_GOLD_CHANCE = 0.15
_WIN_BOX_CONTENTS = {
    _BOX_RED:  [(90, 43, 5), (103, 98, 50)],
    _BOX_GOLD: [(90, 43, 25), (103, 98, 200)],
}


def _credit(full_state: dict, category: int, item_id: int, num: int) -> None:
    """Carats go to the wallet, everything else to the item list."""
    if not num:
        return
    if category == 90:
        wallet = full_state.get("coin_info_state")
        if not isinstance(wallet, dict):
            wallet = {"fcoin": 0, "coin": 0}
            full_state["coin_info_state"] = wallet
        wallet["fcoin"] = (wallet.get("fcoin") or 0) + num
        return
    items = full_state.setdefault("item_list_state", [])
    entry = next((i for i in items if i.get("item_id") == item_id), None)
    if entry:
        entry["number"] = (entry.get("number") or 0) + num
    else:
        items.append({"item_id": item_id, "number": num})


def _roll_win_boxes(rounds: list, rng: random.Random) -> list:
    """[{"round": n, "box_color_type": ...}] -- one box per race WON, which
    is what TeamStadiumWinningRewardInfo carries (round + box_color_type) and
    what GetWinningRewardBoxRoundCountArray(round) counts off."""
    boxes = []
    for r in rounds:
        if r.get("win_type") != 1:
            continue
        color = _BOX_GOLD if rng.random() < _WIN_BOX_GOLD_CHANCE else _BOX_RED
        boxes.append({"round": r["round"], "box_color_type": color})
    return boxes


def _open_win_boxes(full_state: dict, boxes: list, team_class: int) -> list:
    """Credit each box and return its TeamStadiumWinningRewardContent rows."""
    contents = []
    scale = max(1, team_class)
    for box in boxes:
        for category, item_id, base in _WIN_BOX_CONTENTS[box["box_color_type"]]:
            num = base * scale
            _credit(full_state, category, item_id, num)
            contents.append({"round": box["round"], "item_category": category,
                             "item_id": item_id, "item_num": num,
                             "box_color_type": box["box_color_type"]})
    return contents


def _class_change_rewards(full_state: dict, new_class: int, class_reward_type: int) -> list:
    """team_stadium_class_reward, granted ONLY on an actual class change.

    BUG FIXED 2026-08-28: this table used to be paid out in full on EVERY
    all_race_end, picked with a bare `LIMIT 1` that ignored class_reward_type
    entirely -- so for any class above 1 that always selected the
    Promoted row (class 2: 300 carats + 1000 friend points; class 6: 1500 +
    5000) and handed it over after every single match. It is a
    promotion/relegation payout: class_reward_type is Promoted=1 /
    RePromoted=2 / Residual=3 / Relegated=4 (dump.cs
    DialogTeamStadiumRewardItemList.TeamStadiumClassRewardType), so it is
    looked up by the class you moved INTO and the reason you moved. Residual
    (3) is deliberately not paid here: this server has no weekly term
    rollover to pay a "stayed in class" reward at, and paying it per match is
    exactly the flood being fixed."""
    row = master_data.query_one(
        "SELECT * FROM team_stadium_class_reward WHERE team_class=? AND class_reward_type=? LIMIT 1",
        (new_class, class_reward_type))
    if row is None:
        return []
    out = []
    for n in range(1, 6):
        category = row[f"item_category_{n}"]
        item_id = row[f"item_id_{n}"]
        num = row[f"item_num_{n}"]
        if not num:
            continue
        _credit(full_state, category, item_id, num)
        # box_color_type stays a real RewardBoxColorType -- Gold for a
        # promotion, Red for a relegation consolation.
        out.append({"round": 0, "item_category": category, "item_id": item_id,
                    "item_num": num,
                    "box_color_type": _BOX_GOLD if class_reward_type != 4 else _BOX_RED})
    return out


def _parse_hms(value: str) -> _dt_time:
    h, m, s = (int(x) for x in value.split(":"))
    return _dt_time(h, m, s)


def _current_term_state() -> int:
    """TeamStadiumDefine.TermStatusType (dump.cs): 1=Race, 2=Interval,
    3=Calc. BUG FIXED 2026-08-24 (live-reported: "Team Trials tallying"
    shown every time the race screen was opened, permanently blocking
    entry). handle_index used to hardcode term_state=3 (Calc)
    unconditionally, so the client believed results were being tallied no
    matter the real time -- always, not intermittently, since nothing here
    ever read the clock at all.

    master.mdb's single team_stadium row only carries TIME-OF-DAY windows
    (race_start_time 15:00:00 through race_end_time 09:59:59 -- wrapping
    past midnight -- then a 5-minute interval, then ~5 hours of calc, which
    together cover the full 24 hours exactly once), with no real per-
    weekday split (only one row exists at all, every *_date column reads
    the same placeholder value 2) -- so this treats it as a DAILY cycle:
    the current time-of-day alone decides which window we're in, using the
    SAME real/frozen servertime patch.py already resolves for every other
    response (config-driven: "now" for real wall-clock time, a frozen
    value for reproducible testing)."""
    row = master_data.query_one("SELECT * FROM team_stadium LIMIT 1")
    if row is None:
        return 1   # no schedule configured -- default to open, never stuck
    now = datetime.fromtimestamp(patch._servertime(), tz=timezone.utc).time()
    interval_start, interval_end = _parse_hms(row["interval_start_time"]), _parse_hms(row["interval_end_time"])
    calc_start, calc_end = _parse_hms(row["calc_start_time"]), _parse_hms(row["calc_end_time"])
    if interval_start <= now <= interval_end:
        return 2
    if calc_start <= now <= calc_end:
        return 3
    return 1


# ============================================================ evaluation ====
# TeamStadiumDefine.ProperType (dump.cs): 1=Distance, 2=Ground, 3=RunningStyle.
# master.mdb team_stadium_evaluation_rate gives a per-mille (well, actually up
# to 10200 = 102.00%) rate for each (proper_type, proper_rank 1-8) pair --
# the SAME 1-8 aptitude scale card_data/trained_chara already store directly
# in proper_distance_*/proper_running_style_*/proper_ground_* (no letter
# conversion needed, these are already ints).
_PROPER_TYPE_DISTANCE = 1
_PROPER_TYPE_GROUND = 2
_PROPER_TYPE_STYLE = 3

# BUG FIXED 2026-08-25 (live-reported: roster-edit screen's own "Auto" button
# tanked the team's score, E 4563 -> RANK DOWN to F 2516 -- and the user's
# screenshot of that screen is the real evidence that resolves BOTH inferred
# guesses this table used to make): the 5 real Team Stadium columns are
# Sprint / Mile / Medium / Long / DIRT -- not a distance-only 5-way split
# that reused "middle" for slot 3 AND 4 while dropping "long" onto slot 5.
# distance_type 1-4 are turf races across the 4 real distance bands; 5 is a
# genuinely separate GROUND category (dirt), not a 5th distance band at all.
# The old mapping shortchanged every dirt-aptitude horse (scored on
# proper_ground_turf like everything else, see _GROUND_COLUMN below) while
# also comparing distance_type 4 against "middle" instead of "long" --
# between them, a roster that was actually well-suited to Team Stadium's
# real 5 columns could easily score LOWER under the old formula than a
# naive one, which is exactly the inverted "auto made it worse" symptom.
_DISTANCE_TYPE_COLUMN = {
    1: "proper_distance_short", 2: "proper_distance_mile",
    3: "proper_distance_middle", 4: "proper_distance_long",
    # Dirt (5) has no distance-aptitude column of its own in chara_info --
    # "middle" stands in as the closest representative race length until a
    # capture pins the real dirt course distance.
    5: "proper_distance_middle",
}
_STYLE_COLUMN = {
    1: "proper_running_style_nige", 2: "proper_running_style_senko",
    3: "proper_running_style_sashi", 4: "proper_running_style_oikomi",
    5: "proper_running_style_oikomi",   # Oonige shares Oikomi's aptitude column
}
# Ground: turf for the 4 distance columns, DIRT for column 5 -- see the
# _DISTANCE_TYPE_COLUMN comment above for the evidence this replaces (a
# constant turf-only lookup previously scored the Dirt column on the wrong
# aptitude stat entirely).
_GROUND_COLUMN = {
    1: "proper_ground_turf", 2: "proper_ground_turf", 3: "proper_ground_turf",
    4: "proper_ground_turf", 5: "proper_ground_dirt",
}


@functools.lru_cache(maxsize=None)
def _evaluation_rate(proper_type: int, proper_rank: int) -> float:
    row = master_data.query_one(
        "SELECT rate FROM team_stadium_evaluation_rate WHERE proper_type=? AND proper_rank=?",
        (proper_type, max(1, min(8, proper_rank or 1))))
    return (row["rate"] / 10000.0) if row else 1.0


def _slot_evaluation_point(chara: dict, distance_type: int, running_style: int) -> int:
    """One roster slot's contribution to team_evaluation_point.

    BUG FIXED 2026-08-25 (real capture: captures/20260825_192658/
    0033+0034_team_stadium_team_edit.json, cross-checked against
    captures/20260817_093008/0011_team_stadium_team_edit.json): this used to
    be raw stat SUM (speed+stamina+power+guts+wiz) times the aptitude rate --
    that undershoots the real client's own submitted team_evaluation_point by
    a factor of ~4.5x, and the miss isn't even a flat constant (varies
    ~4.57x-5.6x slot to slot), so it wasn't just a missing scale factor.

    Recomputing the same 15-slot roster from a real load_index capture of the
    same account through rating_formula.get_rating() (the CAREER rank_score
    curve -- a nonlinear, diminishing-returns stat->score table, not a flat
    sum) plus get_skill_score(), THEN scaled by the aptitude rate, matches
    the real submitted total within ~1.6-2.1% on both captures -- which lines
    up with rating_formula's own documented ~3% approximation gap on the
    skill-score term (the exact master.mdb column that would make it exact
    isn't known). So team_evaluation_point reuses the SAME formula as the
    single-player career rank score, just per-slot and rate-scaled, not some
    Team-Stadium-specific stat sum."""
    stats = [chara.get(s, 0) or 0 for s in ("speed", "stamina", "power", "wiz", "guts")]
    base = rating_formula.get_rating(stats) + rating_formula.get_skill_score(chara.get("skill_array"))
    rate = _evaluation_rate(_PROPER_TYPE_DISTANCE,
                            chara.get(_DISTANCE_TYPE_COLUMN.get(distance_type, 3), 1))
    rate *= _evaluation_rate(_PROPER_TYPE_GROUND,
                             chara.get(_GROUND_COLUMN.get(distance_type, "proper_ground_turf"), 1))
    rate *= _evaluation_rate(_PROPER_TYPE_STYLE,
                             chara.get(_STYLE_COLUMN.get(running_style, 2), 1))
    return int(round(base * rate))


def _team_evaluation_point(team_data_array: list, roster_by_id: dict) -> int:
    """Real team_evaluation_point: sum of every filled slot's
    _slot_evaluation_point. BUG FIXED 2026-08-24 (this was previously
    whatever the client claimed in the request, never computed -- see the
    module's old docstring). unit_max_num (team_stadium_class, 1-3 by
    current team_class) is NOT applied here: no capture confirms whether a
    lower class actually ignores extra members per distance group or
    restricts which distance groups count at all, so every filled slot
    counts for now rather than guessing which ones a real lower-class
    account would have silently dropped."""
    total = 0
    for slot in team_data_array or ():
        tcid = slot.get("trained_chara_id")
        if not tcid:
            continue
        chara = roster_by_id.get(tcid)
        if not chara:
            continue
        total += _slot_evaluation_point(chara, slot.get("distance_type"), slot.get("running_style"))
    return total


def _index_roster(roster) -> dict:
    """dict | list -> {trained_chara_id: chara}. A roster snapshot stored in
    persisted match state MUST round-trip as a plain list, never a dict --
    JSON (this project's whole state-storage format, see app/state.py) only
    has string object keys, so a dict keyed by the real INTEGER
    trained_chara_id silently comes back with STRING keys after a
    save/reload, and every `.get(trained_chara_id)` lookup against it then
    misses. BUG FIXED 2026-08-24 (caught in this feature's own smoke test,
    before ship: an opponent's runners were silently vanishing from
    chara_result_array because handle_start's roster lookup, done after a
    save/reload cycle, could never find them). Call this immediately after
    reading a roster back out of persisted state, before doing any
    trained_chara_id lookups against it."""
    if isinstance(roster, dict):
        return roster
    return {c.get("trained_chara_id"): c for c in roster or ()}


def _roster_by_id(viewer_id) -> dict:
    return {c.get("trained_chara_id"): c for c in trained_chara._get_or_seed_roster(viewer_id)}


# ============================================================== opponents ===
_OPPONENT_OFFER_SIZE = 3   # INFERRED -- no capture shows the real count or
                           # whether a reroll exists; easy to change.


# How far apart two teams' evaluation_points may be and still be offered as
# a match. INFERRED band -- the real game never says a number, but it does
# match inside a class against comparable ratings, and the Opponent Rating
# Bonus (opponent_evaluation_point / 200000) makes an unbounded spread
# actively broken: this server's accounts range from ~4,000 to ~5,800,000
# evaluation_point, so an unfiltered "closest available" match was handing a
# 4,000-rated player a 1,870,000-rated opponent and a +935% score modifier on
# every single line. Anything outside the band falls through to a mob team
# built at the player's own rating instead.
_OPPONENT_EP_BAND = 2.5


def _real_opponent_candidates(viewer_id, my_evaluation_point: int, my_team_class: int = 1,
                              limit: int = _OPPONENT_OFFER_SIZE) -> list:
    """Real other accounts on this server that have submitted a team_stadium
    roster, ranked by closeness to the requester's own evaluation_point.
    User-directed 2026-08-24: opponents come from REAL accounts, not
    fabricated ones. Every candidate's evaluation_point is RECOMPUTED here
    (never their stored claim -- several accounts on this server carry
    leftover test-script values in the tens of millions from before this
    formula existed)."""
    me = str(viewer_id)
    scored = []
    # ONE query for the one key this needs. This used to call get_state per
    # viewer, which parsed every account's ENTIRE state (the big ones are
    # ~340 KB each) just to read team_stadium_state off it: 180 ms across 200
    # accounts, against ~25 ms here for a verified-identical opponent set.
    # Sorted so the tie-break below is deterministic -- the old iteration
    # order was whatever the table happened to return.
    candidates = state_store.all_states_for_key(TEAM_STADIUM_STATE_KEY)
    eligible = []
    for other_id in sorted(candidates):
        st = candidates[other_id]
        if other_id == me:
            continue
        if not isinstance(st, dict) or not st.get("team_data_array"):
            continue
        if (st.get("team_class") or 1) != my_team_class:
            continue          # real matchmaking never crosses a class boundary
        eligible.append((other_id, st))

    # Two more targeted queries for the surviving candidates' rosters, instead
    # of a full get_state each inside the loop. Same fast-path rule as
    # missions._genuine_roster: only trust a roster already at the current
    # version, and otherwise fall through to _roster_by_id, which is what
    # (re)seeds and persists one.
    ids = [oid for oid, _ in eligible]
    _rosters = state_store.all_states_for_key(trained_chara.ROSTER_KEY, ids)
    _versions = state_store.all_states_for_key(trained_chara.ROSTER_VERSION_KEY, ids)

    for other_id, st in eligible:
        if _versions.get(other_id) == trained_chara.ROSTER_VERSION:
            roster = _index_roster(_rosters.get(other_id))
        else:
            roster = _roster_by_id(other_id)
        ep = _team_evaluation_point(st["team_data_array"], roster)
        lo, hi = my_evaluation_point / _OPPONENT_EP_BAND, my_evaluation_point * _OPPONENT_EP_BAND
        if my_evaluation_point > 0 and not (lo <= ep <= hi):
            continue
        scored.append((abs(ep - my_evaluation_point), other_id, st, roster, ep))

    # Real lists are one offer ABOVE the player's rating, one level with it and
    # one BELOW -- not simply the three nearest (see _build_opponent for the
    # four captures). With a large population "three nearest" happens to come
    # out that way, but on a small or lopsided pool it can hand back three
    # opponents all on the same side of the player, so the split is explicit.
    above = sorted((c for c in scored if c[4] > my_evaluation_point), key=lambda c: c[0])
    below = sorted((c for c in scored if c[4] <= my_evaluation_point), key=lambda c: c[0])
    picked, seen = [], set()

    def take(pool):
        for cand in pool:
            if cand[1] not in seen:
                seen.add(cand[1])
                picked.append(cand)
                return True
        return False

    take(above)                                    # the strong pick
    take(sorted(scored, key=lambda c: c[0]))       # the even one
    take(below)                                    # the weak one
    # Short pool: backfill with whatever is nearest and still unused, so a
    # server with only one or two eligible accounts still offers them.
    for cand in sorted(scored, key=lambda c: c[0]):
        if len(picked) >= limit:
            break
        if cand[1] not in seen:
            seen.add(cand[1])
            picked.append(cand)
    picked.sort(key=lambda c: c[4], reverse=True)  # descending rating
    return picked[:limit]


def _trainer_name_for(viewer_id) -> str:
    """Cheap, consistent display name for a real other account, matching
    load.py's own _neutralize_identity convention (f"Trainer{last 4 digits}")
    rather than reaching into their full load_index profile for a cosmetic
    field."""
    return f"Trainer{str(viewer_id)[-4:]}"


def _wire_item_info(full_state: dict, used_item_ids: list) -> list:
    """UserItem[] for whatever the match just consumed -- the client uses this
    to correct its own cached stock rather than re-reading load/index."""
    from . import shop
    return [{"item_id": item_id, "number": shop.item_count(full_state, item_id)}
            for item_id in dict.fromkeys(used_item_ids or ())]


def _wire_team_data_array(team_data_array: list) -> list:
    return [{"distance_type": e.get("distance_type"), "member_id": e.get("member_id"),
             "trained_chara_id": e.get("trained_chara_id") or 0,
             "running_style": e.get("running_style") or 0}
            for e in team_data_array or ()]


def _wire_trained_chara_array(team_data_array: list, roster) -> list:
    """roster may be a dict OR a list (see _index_roster) -- accepts either
    so callers never need to remember which form a given roster is
    currently in."""
    roster = _index_roster(roster)
    seen = set()
    out = []
    for slot in team_data_array or ():
        tcid = slot.get("trained_chara_id")
        if not tcid or tcid in seen:
            continue
        seen.add(tcid)
        chara = roster.get(tcid)
        if chara:
            out.append(chara)
    return out


def _build_opponent(opponent_viewer_id, st: dict, roster: dict, evaluation_point: int,
                    strength: int = 2) -> dict:
    """strength is the offer's POSITION in the list (1 = the strong pick,
    2 = the even one, 3 = the weak one), NOT a rating bucket.

    BUG FIXED 2026-08-28: this used to be computed as
    `1 + evaluation_point // 200000` clamped to 1-5, a rating scale invented
    here. Four real opponent_list captures all show exactly `strength` 1, 2,
    3 -- one per offer, in descending evaluation_point order -- and never any
    other value:
      captures/20260825_192658/0010: 338672 / 333452 / 328455
      captures/20260825_192658/0015: 338383 / 332317 / 329631
      captures/20260825_094909/0049: 339037 / 334721 / 328490
      captures/20260816_202505/0045:   4171 /   2516 /   1152
    In all four the MIDDLE offer sits on the player's own team_evaluation_point
    (the same session's team_edit capture 0034 puts it at 333531 against that
    first list's 333452; the 0816 session's own roster was the 2516 the second
    offer matches exactly), which is what makes the list read as
    "+about 5k / yours / -about 5k"."""
    team_data_array = st["team_data_array"]
    return {
        "strength": strength,
        "opponent_viewer_id": int(opponent_viewer_id),
        "evaluation_point": evaluation_point,
        "user_info": {"viewer_id": int(opponent_viewer_id),
                      "name": _trainer_name_for(opponent_viewer_id),
                      "team_class": st.get("team_class", 1)},
        "team_data_array": _wire_team_data_array(team_data_array),
        "trained_chara_array": _wire_trained_chara_array(team_data_array, roster),
        "winning_reward_guarantee_status": 0,
    }


# Mob opponents' trained_chara_ids. They used to be NEGATIVE (-1, -2, ...) and
# -- worse -- the SAME -1..-15 in all three offers, so the three opponent cards
# on screen collided with each other on a field the client keys charas by. A
# high positive band, one 100-wide slice per offer, is unique both against the
# player's own roster (ids run to ~100k, and trained_chara.HOUSE_ID_BASE is
# 200k) and across offers.
_MOB_CHARA_ID_BASE = 700_000
_MOB_CHARA_ID_STRIDE = 100
# Real opponents in the capture are fully limit-broken; rarity itself comes off
# the card (card_data.default_rarity) since that is real data.
_MOB_TALENT_LEVEL = 5


@functools.lru_cache(maxsize=512)
def _card_for_chara(chara_id: int) -> int:
    """A real card_id for this character, or 0 if she has none.

    0 is what made the opponent-select screen draw RED BLOCKS (live-reported
    2026-09-03): every mob chara went out with card_id 0, and the client builds
    a trained-chara card from card_id -- with nothing to resolve it drew the
    missing-asset placeholder. single_mode_npc is mostly the ANONYMOUS mob band
    (chara_id 1, the 8xxx mob_ids), which has no card_data row at all and so can
    never render as an uma; those rows drop out of the pool by way of this
    returning 0 for them.
    """
    if not chara_id:
        return 0
    row = master_data.query_one(
        "SELECT id FROM card_data WHERE chara_id=? ORDER BY default_rarity DESC, id",
        (int(chara_id),))
    return int(row["id"]) if row else 0


@functools.lru_cache(maxsize=1)
def _npc_pool() -> tuple:
    """single_mode_npc rows that are a REAL character (one with a card), each
    already carrying the card_id and rarity the wire needs."""
    out = []
    for r in master_data.query(
            "SELECT id, chara_id, mob_id, speed, stamina, pow, guts, wiz, skill_set_id, "
            "proper_distance_short, proper_distance_mile, proper_distance_middle, "
            "proper_distance_long, proper_running_style_nige, proper_running_style_senko, "
            "proper_running_style_sashi, proper_running_style_oikomi, "
            "proper_ground_turf, proper_ground_dirt FROM single_mode_npc"):
        card_id = _card_for_chara(r["chara_id"])
        if not card_id:
            continue
        row = dict(r)
        row["card_id"] = card_id
        card = master_data.query_one(
            "SELECT default_rarity FROM card_data WHERE id=?", (card_id,))
        row["rarity"] = int((dict(card) if card else {}).get("default_rarity") or 3)
        out.append(row)
    return tuple(out)


def _mob_opponent_team(chara_info_like: dict, evaluation_point: int,
                       viewer_id: int = 0, offer_index: int = 0) -> tuple:
    """Fallback when too few real accounts have an eligible roster: build a
    synthetic 15-slot roster from single_mode_npc, rescaled to land near the
    target evaluation_point -- same rescale-to-target-total idea
    single_mode_team._mob_opponents already uses (there for "weaker than the
    trainee"; here for "comparable strength"). Returns (team_data_array,
    roster_by_id) in the SAME shape a real account's would be.

    Each member is a FULL trained-chara record, not the 23-field stub this used
    to emit. The opponent-select screen renders these exactly like a real
    player's team, so everything it draws -- card art, rarity stars, rank badge,
    fan count, racing silks -- has to be present and resolvable; see
    _card_for_chara for the red-block bug that came of leaving card_id at 0.
    The stat/aptitude/skill fields the race sim reads are unchanged."""
    rows = _npc_pool()
    if not rows:
        return [], {}
    rng = random.Random(evaluation_point)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    team_data_array = []
    roster = {}
    next_id = _MOB_CHARA_ID_BASE + offer_index * _MOB_CHARA_ID_STRIDE
    for distance_type in _VALID_DISTANCE_TYPES:
        for member_id in _VALID_MEMBER_IDS:
            row = dict(rng.choice(rows))
            total = row["speed"] + row["stamina"] + row["pow"] + row["guts"] + row["wiz"]
            # Target: this slot alone should roughly reproduce evaluation_point
            # when run back through _slot_evaluation_point -- close enough for a
            # fallback pool, not claimed to be exact.
            target = max(250, evaluation_point / 15)
            scale = target / max(total, 1)
            stats = {"speed": int(row["speed"] * scale),
                     "stamina": int(row["stamina"] * scale),
                     "power": int(row["pow"] * scale),
                     "guts": int(row["guts"] * scale),
                     "wiz": int(row["wiz"] * scale)}
            rank_score = rating_formula.get_rating(
                [stats["speed"], stats["stamina"], stats["power"],
                 stats["guts"], stats["wiz"]])
            fans = rng.randint(10_000, 300_000)
            card_id = row["card_id"]
            chara = {
                "trained_chara_id": next_id,
                "viewer_id": int(viewer_id),
                "owner_viewer_id": 0, "owner_trained_chara_id": 0,
                "single_mode_chara_id": next_id,
                "card_id": card_id,
                "mob_id": row["mob_id"],
                "trainer_name": "Trainer",
                "running_style": rng.randint(1, 4),
                "speed": stats["speed"], "stamina": stats["stamina"],
                "power": stats["power"], "guts": stats["guts"], "wiz": stats["wiz"],
                "proper_distance_short": row["proper_distance_short"],
                "proper_distance_mile": row["proper_distance_mile"],
                "proper_distance_middle": row["proper_distance_middle"],
                "proper_distance_long": row["proper_distance_long"],
                "proper_running_style_nige": row["proper_running_style_nige"],
                "proper_running_style_senko": row["proper_running_style_senko"],
                "proper_running_style_sashi": row["proper_running_style_sashi"],
                "proper_running_style_oikomi": row["proper_running_style_oikomi"],
                "proper_ground_turf": row["proper_ground_turf"],
                "proper_ground_dirt": row["proper_ground_dirt"],
                # Real skills off single_mode_npc.skill_set_id. BUG FIXED
                # 2026-08-28: mob opponents used to carry no skill_array at all,
                # so they ran every race with zero skills activating -- no
                # accelerations, no recoveries, and (via team_stadium_raw_score
                # condition_type 8) not a single point of skill score, handing
                # the player the whole category for free on top of a physically
                # weaker field.
                "skill_array": single_mode_team._npc_skills(row),
                # --- everything below is what the CARD itself is drawn from ---
                "rarity": row["rarity"],
                "talent_level": _MOB_TALENT_LEVEL,
                "rank_score": rank_score,
                "rank": trained_chara._rank_for_score(rank_score),
                "fans": fans,
                "chara_grade": trained_chara._chara_grade_for(fans),
                "race_cloth_id": trained_chara.dress_for_card(card_id, row["chara_id"]),
                "scenario_id": 1,
                "succession_num": 0,
                "succession_trained_chara_id_1": 0,
                "succession_trained_chara_id_2": 0,
                "is_saved": 1, "is_locked": 0,
                "nickname_id": 0, "wins": 0,
                "register_time": now, "create_time": now,
                # Present-and-empty, not absent: the capture carries all of
                # these on every opponent chara, and a missing array is a null
                # the client would have to defend against.
                "support_card_list": [], "race_result_list": [],
                "win_saddle_id_array": [], "nickname_id_array": [],
                "factor_info_array": [], "factor_extend_array": [],
                "succession_chara_array": [],
            }
            roster[next_id] = chara
            team_data_array.append({"distance_type": distance_type,
                                    "member_id": member_id,
                                    "trained_chara_id": next_id,
                                    "running_style": chara["running_style"]})
            next_id += 1
    return team_data_array, roster


_MOB_OPPONENT_VIEWER_BASE = 900_000_600_000  # sentinel range, mirrors
                                              # single_mode_team's own
                                              # _FRIEND_SENTINEL_VIEWER_BASE
                                              # convention for fabricated ids


# The three offers' target ratings, as a fraction of the player's own. The
# real list is "a bit above / level / a bit below" (see _build_opponent's
# captures: +1.5% / -0.02% / -1.5% at a 333k rating). On the live game that
# spread is emergent -- it is just how far apart the nearest real players
# happen to sit in a huge population, which is why the same shape appears as
# +1655 / 0 / -1364 at a 2.5k rating instead. Mob fill-ins have no population
# to be spaced by, so they reproduce the shape directly: a percentage, with a
# small absolute floor so two offers never collapse onto the same number at
# very low ratings.
_MOB_OFFER_SPREAD = (0.015, 0.0, -0.015)
_MOB_OFFER_MIN_DELTA = 250


def _mob_offer_targets(my_evaluation_point: int, count: int, start_index: int) -> list:
    """Target evaluation_points for `count` mob offers filling positions
    start_index.. of the list, strongest first."""
    targets = []
    for i in range(start_index, start_index + count):
        frac = _MOB_OFFER_SPREAD[min(i, len(_MOB_OFFER_SPREAD) - 1)]
        delta = my_evaluation_point * frac
        if frac and abs(delta) < _MOB_OFFER_MIN_DELTA:
            delta = _MOB_OFFER_MIN_DELTA if frac > 0 else -_MOB_OFFER_MIN_DELTA
        targets.append(max(1, int(round(my_evaluation_point + delta))))
    return targets


def _opponent_offers(viewer_id, my_evaluation_point: int, my_team_class: int = 1) -> list:
    """[(key, wire_opponent_dict, team_data_array, roster_list, team_class,
    evaluation_point)] -- key is what decide_frame_order's submitted
    opponent_info is matched back against. roster is always a LIST here
    (never a dict) -- see _index_roster's docstring for why: this return
    value gets persisted into current_match.candidates and must survive a
    JSON round-trip."""
    offers = []
    for _dist, other_id, st, roster, ep in _real_opponent_candidates(
            viewer_id, my_evaluation_point, my_team_class):
        offers.append((str(other_id), st["team_data_array"], list(roster.values()),
                       st.get("team_class", 1), ep, roster, st))
    missing = max(0, _OPPONENT_OFFER_SIZE - len(offers))
    # Mobs fill the tail of the list, so a pool with one real opponent still
    # offers a stronger and a weaker mob around it rather than three clones of
    # the player's own rating (which is what this used to build -- every mob
    # was targeted at my_evaluation_point exactly, so all three cards showed
    # the same number and the same Opponent Rating Bonus).
    for i, target in enumerate(_mob_offer_targets(
            my_evaluation_point, missing, _OPPONENT_OFFER_SIZE - missing)):
        mob_viewer = _MOB_OPPONENT_VIEWER_BASE + i
        team_data_array, roster = _mob_opponent_team(
            {}, target, viewer_id=mob_viewer, offer_index=i)
        offers.append((str(mob_viewer), team_data_array, list(roster.values()),
                       my_team_class, target,
                       roster, {"team_data_array": team_data_array,
                                "team_class": my_team_class}))
    # Descending rating, strength = position (1 strong / 2 even / 3 weak).
    offers.sort(key=lambda o: o[4], reverse=True)
    return [(key, _build_opponent(int(key), st, roster, ep, strength=pos),
             tda, roster_list, team_class, ep)
            for pos, (key, tda, roster_list, team_class, ep, roster, st)
            in enumerate(offers, start=1)]


# ============================================================== courses ====
@functools.lru_cache(maxsize=1)
def _course_pools_by_distance_type() -> dict:
    """{race distanceType (1-4, course_data.json's own field) -> [race_instance_id, ...]}
    for picking a real course per Team Stadium distance_type. Cached: master
    data + course_data.json are both read-only for the process lifetime."""
    course_data = race_simulator._course_data()
    pools: dict = {1: [], 2: [], 3: [], 4: []}
    for row in master_data.query(
            "SELECT ri.id AS instance_id, r.course_set AS course_set "
            "FROM race_instance ri JOIN race r ON r.id = ri.race_id"):
        course = course_data.get(str(row["course_set"]))
        if not course:
            continue
        dt = course.get("distanceType")
        if dt in pools:
            pools[dt].append(row["instance_id"])
    return pools


# Team Stadium distance_type (1-5) -> real course distanceType (1-4). Same
# "double up middle" inference as _DISTANCE_TYPE_COLUMN above, for the same
# reason (no capture/master data resolves the 5th bucket).
_TEAM_DISTANCE_TO_COURSE_TYPE = {1: 1, 2: 2, 3: 3, 4: 3, 5: 4}


def _pick_course(distance_type: int, seed_rng: random.Random) -> int | None:
    pool = _course_pools_by_distance_type().get(_TEAM_DISTANCE_TO_COURSE_TYPE.get(distance_type, 3))
    return seed_rng.choice(pool) if pool else None


# =========================================================== raw scoring ====
# 2026-08-24: read GameTora's "Team Trials (PvP) Scoring System" article
# (community-verified real mechanics) and cross-checked EVERY one of its
# named bonuses against team_stadium_raw_score's condition_type/value rows --
# every single value in the article (10000/8000/.../2000 placement, 5000
# Trio, 4000 All-Placed, 3000 Quinella, 4000 Dark Horse, -500/-100 Rushed,
# 100-per-0.1s/2000-cap Beat Target Time, 500/1200 skill activation, the
# exact 1500-2500/2000-3000 unique-skill-by-level tables, and every discrete
# score in the Nose..Distance margin table) has an EXACT match in this
# table. This is no longer a guess -- condition_type is now fully decoded:
#   1  = 1st-2nd place margin (value1 = index 0..20 into the Nose/Head/Neck/
#        1/2..10-length/Distance ladder below -- ONLY the physical length
#        breakpoints separating those 21 buckets are inferred, the article
#        gives named categories, not thresholds; the SCORES themselves are
#        exact)
#   2  = Dark Horse (value1=3 [finish<=3rd], value2=8 [8th favorite+])
#   3  = final position (value1 = place 1-12) -- unchanged, already exact
#   4  = Beat Target Time (value1 = deciseconds beaten, 1-20, score=value1*100)
#   5  = Rushed flat penalty (-500)
#   6  = Rushed per-second penalty (-100/s)
#   7  = Strong Start (flat 1000)
#   8  = skill activation (value1: 1=white/500, 2=gold/1200, 3=base unique,
#        4&5=evolved unique -- value1 3/4/5 map DIRECTLY to skill_data.rarity
#        for the fired skill id; value2 = that skill's level 1-10)
#   9  = Good Positioning (value1: 1=mid-phase leader-of-strategy, 2=final-
#        phase leader-of-strategy; both flat 1000)
#   10 = match Victory (10000, awarded once at all_race_end for winning 3+
#        of 5 rounds)
#   11 = team result (value1/value2: (3,3)=Trio/5000 prio1, (3,5)=All-Placed/
#        4000 prio2 [mutually exclusive via priority, matching the article's
#        "can't get both"], (2,2)=Quinella/3000 -- team_stadium_class's own
#        unit_max_num (1/2/3/3/3/3 by team_class 1-6) is what actually gates
#        whether a distance group fields 2 or 3, tying Quinella/Trio directly
#        to class rather than being a free choice)
#
# team_stadium_score_bonus (the score MODIFIERS, applied additively to a raw
# subtotal per the article's own explicit rule) is equally exact:
#   condition_type 1 (value1=1, rate 1000/10000=10%)      = Ace Bonus
#   condition_type 3 (value1=0, rate 200000)               = Opponent Rating
#        Bonus = opponent_evaluation_point / 200000, as a fraction -- checks
#        out EXACTLY against the article's own "~50% at 100,000 rating"
#        anchor (100000/200000 = 50%); its "~80% at 150,000" is a stated
#        ballpark and lands at 75% here, well inside that hedge
#   condition_type 4 (value1=2..5, rate 200..500)          = Consecutive Win
#        Bonus = 1% per streak count, STARTING AT COUNT 2 (no value1=1 row --
#        the first win of a streak gets 0%, contradicting the article's own
#        looser "every character in the third victory gets +3%" prose, which
#        rounds off that detail). Caps at 5 (the table's own top row), which
#        neatly matches "5 races per match" -- so a streak is a per-MATCH
#        concept; consecutive_win_count is reset to 0 at the start of every
#        new match (opponent_list) rather than carried across matches
#   condition_type 5 (value1=0, rate 0)                    = Support Bonus,
#        a placeholder 0% in the master row itself -- this feature has no
#        per-slot support-card deck data to compute a real value from, and
#        the table agrees 0 is a legitimate base case, not a guess
# Their priority order (1,2,3,4) matches the article's own section order
# (Ace, Opponent, Support, Consecutive) exactly.


# raw_score_id on the wire is team_stadium_raw_score's PRIMARY KEY, NOT its
# condition_type. dump.cs is unambiguous: ScoreData.RawScoreId is fed to
# MasterTeamStadiumRawScore.Get(int id) -- its only int-keyed accessor, the
# condition_type ones are all named GetWithConditionType* -- and the sibling
# HorseChallengeMatchPointCalculator does the same with `int rawPointId`.
# BUG FIXED 2026-08-28: every score line used to send the condition_type
# (1-11) there, which the client resolved against ids 1-11 -- eleven unrelated
# rows out of the middle of the 1st-2nd-margin ladder -- so the score-details
# screen named and grouped the wrong things.
NO_SCORE = (0, 0)


@functools.lru_cache(maxsize=None)
def _raw_score_row(condition_type: int, value1: int, value2: int = 0) -> tuple:
    """(team_stadium_raw_score.id, score) for one condition, or NO_SCORE."""
    row = master_data.query_one(
        "SELECT id, score FROM team_stadium_raw_score WHERE condition_type=? "
        "AND condition_value_1=? AND condition_value_2=? ORDER BY priority LIMIT 1",
        (condition_type, value1, value2))
    return (row["id"], row["score"]) if row else NO_SCORE


@functools.lru_cache(maxsize=1)
def _placement_scores() -> dict:
    """{finish place (1-based) -> (row id, score)} -- condition_type=3."""
    return {r["condition_value_1"]: (r["id"], r["score"]) for r in master_data.query(
        "SELECT id, condition_value_1, score FROM team_stadium_raw_score WHERE condition_type=3")}


def _place_score(place: int) -> tuple:
    return _placement_scores().get(place, NO_SCORE)


def _raw_score(condition_type: int, value1: int, value2: int = 0) -> int:
    return _raw_score_row(condition_type, value1, value2)[1]


# Margin ladder: (length value, condition_value_1 index) for the 17 numeric
# entries; Nose/Head/Neck (indices 0/1/2) and Distance (index 20) are handled
# separately in _margin_index. 1 length = 2.5m -- confirmed via uma-tools'
# own basinnhyou.ts skill-gain tool, which measures skill position gain in
# lengths as `(pos_gain_meters) / 2.5`.
LENGTH_METERS = 2.5
_MARGIN_LADDER = [
    (0.5, 3), (0.75, 4), (1, 5), (1.25, 6), (1.5, 7), (1.75, 8), (2, 9),
    (2.5, 10), (3, 11), (3.5, 12), (4, 13), (5, 14), (6, 15), (7, 16),
    (8, 17), (9, 18), (10, 19),
]


def _margin_index(length: float) -> int:
    """length (in body lengths) -> team_stadium_raw_score condition_type=1's
    condition_value_1 (0=Nose..20=Distance). INFERRED bucketing -- the
    article names Nose/Head/Neck/1/2..10-length/Distance as categories but
    never gives the meter/length thresholds that separate them, and no
    other source here does either. Sub-1/2-length space is split into three
    equal thirds for Nose/Head/Neck; the numeric ladder above is matched to
    its nearest entry by midpoint; anything past the last defined length (10)
    reads as Distance. The SCORE for whichever bucket this lands in is exact
    (team_stadium_raw_score) even though the boundary itself is a guess."""
    if length < 0:
        length = 0.0
    if length < 0.5:
        third = 0.5 / 3
        return 0 if length < third else (1 if length < 2 * third else 2)
    if length > 10:
        return 20
    best_idx, best_diff = 19, abs(length - 10)
    for val, idx in _MARGIN_LADDER:
        diff = abs(length - val)
        if diff < best_diff:
            best_idx, best_diff = idx, diff
    return best_idx


def _margin_score(length: float) -> tuple:
    return _raw_score_row(1, _margin_index(length))


def _dark_horse_score(favorite_rank: int, place: int) -> tuple:
    return _raw_score_row(2, 3, 8) if place <= 3 and favorite_rank >= 8 else NO_SCORE


def _beat_target_time_score(beat_seconds: float) -> tuple:
    deciseconds = min(20, int(beat_seconds * 10))
    return _raw_score_row(4, deciseconds) if deciseconds >= 1 else NO_SCORE


def _rush_penalty(duration: float) -> tuple:
    """(row id of the flat -500 "Rushed" entry, total penalty) -- the flat
    penalty and the per-second one share a single displayed line."""
    if duration <= 0:
        return NO_SCORE
    flat_id, flat = _raw_score_row(5, 0)
    _per_id, per = _raw_score_row(6, 0)
    return flat_id, flat + per * round(duration)


_STRONG_START_THRESHOLD = 0.02   # bottom 20% of the engine's [0, 0.1] start-
                                  # delay range (race_solver.py: start_delay =
                                  # 0.1 * random()) -- matches the article's
                                  # own "probability of strong start is 20%"
                                  # claim exactly, since that's a uniform
                                  # distribution's bottom quintile.


def _strong_start_score(start_delay: float) -> tuple:
    return _raw_score_row(7, 1) if start_delay < _STRONG_START_THRESHOLD else NO_SCORE


@functools.lru_cache(maxsize=None)
def _skill_rarity(skill_id: int) -> int | None:
    row = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (skill_id,))
    return row["rarity"] if row else None


def _skill_activation_score(skill_id: int, level: int) -> tuple:
    rarity = _skill_rarity(skill_id)
    if rarity == 1:
        return _raw_score_row(8, 1)
    if rarity == 2:
        return _raw_score_row(8, 2)
    if rarity in (3, 4, 5):
        return _raw_score_row(8, rarity, max(1, min(10, level or 1)))
    return NO_SCORE


def _positioning_score(is_mid_phase: bool) -> tuple:
    return _raw_score_row(9, 1 if is_mid_phase else 2)


def _team_result_score(places: list, team_size: int) -> tuple:
    """places: this team's fielded members' finish positions in ONE race.
    Trio (5000, all 3 top-3) beats All-Placed (4000, all 3 top-5) beats
    nothing -- team_stadium_raw_score's own `priority` column encodes that
    exclusivity (checked highest-priority-first), matching the article's
    explicit "you can get Top 3 or Top 5, not both". Quinella (3000, both
    top-2) only exists for a 2-member team, itself gated by
    team_stadium_class.unit_max_num."""
    if not places:
        return NO_SCORE
    if team_size >= 3 and all(p <= 3 for p in places):
        return _raw_score_row(11, 3, 3)      # Trio
    if team_size >= 3 and all(p <= 5 for p in places):
        return _raw_score_row(11, 3, 5)      # All Members Placed
    if team_size == 2 and all(p <= 2 for p in places):
        return _raw_score_row(11, 2, 2)      # Quinella
    return NO_SCORE


# ==================================================== score modifiers ====
@functools.lru_cache(maxsize=None)
def _score_bonus_rate_raw(condition_type: int, value1: int) -> int:
    row = master_data.query_one(
        "SELECT score_rate FROM team_stadium_score_bonus WHERE condition_type=? AND condition_value_1=?",
        (condition_type, value1))
    return row["score_rate"] if row else 0


def _ace_bonus_rate() -> float:
    return _score_bonus_rate_raw(1, 1) / 10000.0


def _opponent_bonus_rate(opponent_evaluation_point: int) -> float:
    divisor = _score_bonus_rate_raw(3, 0)
    return (opponent_evaluation_point / divisor) if divisor else 0.0


def _consecutive_bonus_rate(count: int) -> float:
    if count < 2:
        return 0.0
    return _score_bonus_rate_raw(4, min(count, 5)) / 10000.0


def _support_bonus_rate() -> float:
    return _score_bonus_rate_raw(5, 0) / 10000.0   # always 0 -- see module note above


@functools.lru_cache(maxsize=None)
def _score_bonus_id(condition_type: int, value1: int) -> int:
    """team_stadium_score_bonus.id -- what TeamStadiumBonusData.score_bonus_id
    carries (ScoreBonusData.BonusName resolves the row's display name from
    it), same primary-key convention as raw_score_id above."""
    row = master_data.query_one(
        "SELECT id FROM team_stadium_score_bonus WHERE condition_type=? AND condition_value_1=?",
        (condition_type, value1))
    return row["id"] if row else 0


def _apply_line_bonuses(lines: list, specs: list) -> int:
    """Fold the score MODIFIERS into each score line and return the horse's
    post-bonus total.

    specs: [(team_stadium_score_bonus.id, rate as a fraction)].

    The client does NOT apply these itself -- it re-derives them. dump.cs's
    ScoreData.ScoreBonusDetail pairs ApplyBonus(rawScore) with
    CalcRawScore(score) around SumAllBonus(), i.e. displayed line score =
    raw + sum(bonus_score) and raw = score - sum(bonus_score). So
    `bonus_score` is an ABSOLUTE point amount and the line's own `score`
    must already include it.

    BUG FIXED 2026-08-28: every line used to ship its RAW score with an
    empty bonus_array while the modifiers were folded only into
    team_total_score. The race screen showed the server's (bonused) team
    total, then the score-details tab re-totalled the un-bonused lines and
    came out wildly smaller -- exactly the mismatch GameTora's article
    warns about from the other direction ("on the score details page you
    might notice that you actually got much more than [the base score]").
    Modifiers are ADDITIVE, not multiplicative, per that same article."""
    total = 0
    for line in lines:
        raw = line["score"]
        if raw <= 0:            # the Rushed penalty is not scaled by bonuses
            line["bonus_array"] = []
            total += raw
            continue
        bonus_array = []
        added = 0
        for bonus_id, rate in specs:
            amount = round(raw * rate)
            if amount:
                bonus_array.append({"score_bonus_id": bonus_id, "bonus_score": int(amount)})
                added += int(amount)
        line["bonus_array"] = bonus_array
        line["score"] = raw + added
        total += line["score"]
    return total


# ================================================= race result analysis ====
# Good Positioning and the 1st-2nd margin bonus both need to look INSIDE the
# race, not just at final results -- both read run_simulation()'s raw (pre-
# downsample) frames directly.

def _position_at(frames: list, horse_idx: int, t: float) -> float:
    """Linear-interpolated position of horse_idx at time t, from run_simulation's
    raw {"t":..., "horses":[{"pos":...}]} frames."""
    prev = frames[0]
    for f in frames:
        if f["t"] >= t:
            if f is prev:
                return f["horses"][horse_idx]["pos"]
            t0, t1 = prev["t"], f["t"]
            p0, p1 = prev["horses"][horse_idx]["pos"], f["horses"][horse_idx]["pos"]
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return p0 + (p1 - p0) * frac
        prev = f
    return frames[-1]["horses"][horse_idx]["pos"]


def _phase_leaders(frames: list, running_styles: list, boundary: float) -> set:
    """Horse indices that are ranked 1st among same-running_style horses at
    the moment THEY cross `boundary` distance -- the real "Good Positioning"
    condition (dump.cs/master.mdb give no more detail than the article's own
    plain-language description; phase_start()'s 1/6 and 2/3 course-distance
    boundaries are the SAME ones race_solver.py's own phase transitions use,
    so at least the boundary itself is not a guess)."""
    n = len(running_styles)
    crossing_t = [None] * n
    for i in range(n):
        for f in frames:
            if f["horses"][i]["pos"] >= boundary:
                crossing_t[i] = f["t"]
                break
    leaders = set()
    for i in range(n):
        t = crossing_t[i]
        if t is None:
            continue
        pos_i = _position_at(frames, i, t)
        style = running_styles[i]
        if all(_position_at(frames, j, t) <= pos_i
               for j in range(n) if j != i and running_styles[j] == style):
            leaders.add(i)
    return leaders


def _favorite_ranks(horses: list) -> list:
    """1 = strongest (most favored), matching Dark Horse's "Nth favorite"
    condition. No real popularity/odds system exists on this server -- ranks
    by raw stat total, the same proxy _mob_opponent_team/_slot_evaluation_point
    already use elsewhere in this module."""
    totals = [sum(h.get(s, 0) or 0 for s in _STATS) for h in horses]
    order = sorted(range(len(horses)), key=lambda i: -totals[i])
    ranks = [0] * len(horses)
    for rank, idx in enumerate(order, start=1):
        ranks[idx] = rank
    return ranks


def _standard_course_time(course: dict) -> float:
    """"Standard Course Time" / reference time the article describes --
    course_data.json gives finishTimeMin/finishTimeMax (in 1e-4s units,
    matching this codebase's own race_horse wire encoding elsewhere) for the
    course but no single named "standard" value; the midpoint is used as a
    working proxy. INFERRED -- no capture confirms the real reference time."""
    lo, hi = course.get("finishTimeMin"), course.get("finishTimeMax")
    if not lo or not hi:
        return 0.0
    return (lo + hi) / 2 / 10000.0


def _skill_level_for(chara: dict, skill_id: int) -> int:
    for sk in chara.get("skill_array") or ():
        if sk.get("skill_id") == skill_id:
            return sk.get("level") or 1
    return 1


def _team_race_horse_array(horses: list, ris: list) -> list:
    """race_horse_data_array entries -- the SAME rich RaceHorseData wire
    struct every other race type here uses (practice_race._build_race_horse_
    entry), NOT the bare {frame_order, trained_chara_id} pair this used to
    send. BUG FIXED 2026-08-24 (live-reported softlock after a full 5-round
    match, confirmed via Player.log: 'DialogTeamStadiumRaceResultList.
    GetHorseDataList' NullReferenceException). The real dump.cs RaceHorseData
    has ~20 fields including viewer_id, stats, dress, aptitudes -- the
    client's post-match history/result screens read those directly AND use
    viewer_id (together with trained_chara_id) as the accessor lookup key
    for TeamStadiumTrainedCharaDataAccessor, so a bare 2-field stub left
    viewer_id at its implicit 0 for every horse, which resolves to nobody on
    either side and NullRefs downstream once the match tries to save its
    history. `chara["viewer_id"]` is stamped from `ri` here (mine or the
    opponent's real account id) since a roster snapshot for another account
    doesn't otherwise carry a self-referential viewer_id field."""
    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}
    pop = _rank_by(lambda i: -(horses[i].get("speed") or 0))
    sta = _rank_by(lambda i: -(horses[i].get("stamina") or 0))
    pw = _rank_by(lambda i: -(horses[i].get("pow", horses[i].get("power", 0)) or 0))
    out = []
    for i, chara in enumerate(horses):
        chara = dict(chara)
        chara["viewer_id"] = ris[i]["viewer_id"]
        out.append(practice_race._build_race_horse_entry(
            chara, frame_order=ris[i]["frame_order"], final_grade=chara.get("rank", 1),
            popularity=pop[i], popularity_mark_rank_array=[pop[i], sta[i], pw[i]]))
    return out


@functools.lru_cache(maxsize=None)
def _team_class_row(team_class: int) -> dict | None:
    return master_data.query_one("SELECT * FROM team_stadium_class WHERE team_class=?", (team_class,))


def _unit_max_num(team_class: int) -> int:
    row = _team_class_row(team_class)
    return row["unit_max_num"] if row else 3


# ============================================================= endpoints ====

@registry.endpoint("team_stadium/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    rewards = _grant_rank_rewards(full_state, st)
    state_store.save_state(viewer_id, full_state)
    match = st.get("current_match")
    # RaceStatusType (dump.cs TeamStadiumDefine): 0=None, 1=Decide, 2=Race.
    race_status = 0
    if match:
        race_status = 2 if match.get("rounds") else 1
    return _ok({
        "team_stadium_id": 1,
        "team_stadium_user": {
            "team_class": st["team_class"], "best_team_class": st["best_team_class"],
            "team_class_state": 0, "best_point": st["best_point"],
        },
        "ranking": {
            "term_id": 0, "viewer_id": viewer_id, "team_class": st["team_class"],
            "best_point": st["best_point"], "rank": st["rank"],
        },
        "border_line": [],
        "team_class_change_state": 0,
        "reward_info_array": rewards,
        "race_status": race_status,
        "term_state": _current_term_state(),
    })


@registry.endpoint("team_stadium/team_edit")
def handle_team_edit(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    team_data_array = payload.get("team_data_array")
    if not isinstance(team_data_array, list) or not team_data_array:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    owned = {c.get("trained_chara_id")
             for c in trained_chara._get_or_seed_roster(viewer_id)}

    cleaned = []
    seen_slots = set()
    for slot in team_data_array:
        if not isinstance(slot, dict):
            return _refuse()
        dtype, member = slot.get("distance_type"), slot.get("member_id")
        tcid, style = slot.get("trained_chara_id"), slot.get("running_style")
        if dtype not in _VALID_DISTANCE_TYPES or member not in _VALID_MEMBER_IDS:
            return _refuse()
        if (dtype, member) in seen_slots:
            return _refuse()             # duplicate slot in one request
        seen_slots.add((dtype, member))
        if tcid:
            if not isinstance(style, int) or style not in _VALID_RUNNING_STYLES:
                return _refuse()
            if tcid not in owned:
                return _refuse()         # can only field a horse you actually own
        elif style != 0:
            return _refuse()             # empty slot must pair tcid=0 with style=0
        cleaned.append({"distance_type": dtype, "member_id": member,
                        "trained_chara_id": tcid or 0, "running_style": style})

    st = _team_stadium_state(full_state)
    by_slot = {(e["distance_type"], e["member_id"]): e
              for e in st.get("team_data_array") or []}
    for e in cleaned:
        by_slot[(e["distance_type"], e["member_id"])] = e
    st["team_data_array"] = sorted(by_slot.values(),
                                   key=lambda e: (e["distance_type"], e["member_id"]))

    # REAL evaluation_point (BUG FIXED 2026-08-24): the client's own claimed
    # team_evaluation_point in the request is no longer trusted -- see
    # _team_evaluation_point's docstring. Still accepted in the payload for
    # wire compatibility, just ignored.
    old_ep = st.get("team_evaluation_point") or 0
    st["team_evaluation_point"] = _team_evaluation_point(
        st["team_data_array"], _roster_by_id(viewer_id))
    # evaluation_peak (NOT best_point -- see _team_stadium_state's docstring,
    # user-corrected 2026-08-25: best_point is the weekly TT score, a
    # completely separate number from roster strength) only ever grows, so a
    # rank tier reward -- once earned by fielding a strong-enough roster --
    # isn't clawed back just because a later edit swaps in something weaker.
    st["evaluation_peak"] = max(st.get("evaluation_peak") or 0, st["team_evaluation_point"])

    # before_rank/after_rank are TeamStadiumDefine.TeamRank (dump.cs enum:
    # None=0, F=1, E=2, E1=3, E2=4, E3=5, D=6, ... -- identical to
    # team_stadium_rank.id, whose team_min_value bands line up exactly: id 1
    # = 1-2999 = F, id 2 = 3000-6999 = E, ...), NOT the same thing as
    # `ranking.rank` elsewhere in this file (a real capture,
    # captures/20260817_093008/0010_team_stadium_index.json, shows that as
    # 40876 -- a leaderboard placement out of thousands of real players,
    # which this single-player server has no real population to compute).
    #
    # BUG FIXED 2026-08-25 #2 (real capture, same session as the formula fix
    # above: captures/20260825_192658/0033+0034_team_stadium_team_edit.json --
    # two edits back-to-back, swapping ONE horse in ONE slot):
    #   0033: team_evaluation_point=316797 (tier 39) -> response before=41 after=39
    #   0034: team_evaluation_point=333531 (tier 41) -> response before=39 after=41
    # i.e. before_rank/after_rank bracket the CURRENT (raw, non-monotonic)
    # team_evaluation_point of THIS edit against whatever it was on the
    # PREVIOUS edit -- not against best_point (the session's
    # team_stadium_user.best_point sat fixed at 1,010,001 across this entire
    # capture, untouched by any of these edits or the 3 races run in between,
    # confirming it tracks something else -- a season/term peak -- and isn't
    # what before_rank/after_rank compare against). This is what makes
    # "RANK DOWN" a real, correct thing the client shows: your current
    # fielded roster's rank can legitimately drop if you swap in something
    # worse, same as the original bug report's screenshot (4,563/E ->
    # RANK DOWN to 2,516/F). The 2026-08-25 #1 fix above (comparing against
    # best_point so after_rank could only ever climb) was itself wrong --
    # it made the badge/tile-unlock cache one-way-latching instead of
    # tracking the roster you actually have equipped right now.
    before_row, after_row = _rank_for(old_ep), _rank_for(st["team_evaluation_point"])
    before_rank = before_row["id"] if before_row else 0
    after_rank = after_row["id"] if after_row else 0
    rewards = _grant_rank_rewards(full_state, st)
    state_store.save_state(viewer_id, full_state)
    return _ok({"before_rank": before_rank, "after_rank": after_rank, "reward_info_array": rewards})


@registry.endpoint("team_stadium/opponent_list")
def handle_opponent_list(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    if not st.get("team_data_array"):
        return _refuse()   # nothing to matchmake with -- must team_edit first

    my_ep = _team_evaluation_point(st["team_data_array"], _roster_by_id(viewer_id))
    offers = _opponent_offers(viewer_id, my_ep, st.get("team_class", 1))
    st["current_match"] = {
        "candidates": {key: {"team_data_array": tda, "roster": roster, "team_class": team_class,
                             "evaluation_point": ep}
                       for key, _wire, tda, roster, team_class, ep in offers},
        "chosen": None, "frame_orders": None, "rounds": [], "last_checked_round": 0,
    }
    # Consecutive Win Bonus caps at streak=5 (team_stadium_score_bonus's own
    # top row) -- exactly the number of races in one match, so a streak is a
    # per-MATCH concept. Reset here, at the start of a new match, rather than
    # carrying a win streak across matches indefinitely.
    st["consecutive_win_count"] = 0
    state_store.save_state(viewer_id, full_state)
    return _ok({"opponent_info_array": [wire for _key, wire, _tda, _roster, _cls, _ep in offers]})


@registry.endpoint("team_stadium/decide_frame_order")
def handle_decide_frame_order(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match")
    candidates = (match or {}).get("candidates") or {}
    submitted = payload.get("opponent_info") or {}
    key = str(submitted.get("opponent_viewer_id") or "")
    if key not in candidates:
        # Live-reported 2026-08-24 ("connection error 205" reopening a
        # scheduled match): the real client does NOT always echo back a
        # populated opponent_info here -- reopening an in-progress match
        # sends an all-zero placeholder (strength=0, opponent_viewer_id=0,
        # user_info/team_data_array/trained_chara_array all None), confirmed
        # via a live payload dump, rather than resubmitting whichever
        # candidate the player actually tapped. This endpoint has never had
        # a real capture to verify its true request shape against, so
        # rather than treat that as invalid, auto-select the first
        # candidate -- _real_opponent_candidates already sorts candidates by
        # closeness to the player's own evaluation_point, so "first" is the
        # same "best available match" the client would have shown first.
        if candidates:
            key = next(iter(candidates))
        else:
            log.warning("team_stadium/decide_frame_order REFUSED (no candidates): "
                        "match_present=%s submitted=%s", bool(match), submitted)
            return _refuse()

    chosen = match["candidates"][key]
    opp_roster = _index_roster(chosen["roster"])   # see _index_roster's docstring
    opp_team_class = chosen.get("team_class", 1)
    opp_evaluation_point = chosen.get("evaluation_point") or 0
    # Rebuilt server-side from the stored candidate -- NOT the client's
    # submitted opponent_info, which (see above) may just be an empty
    # placeholder. This is also what gets echoed back as opponent_info_copy
    # below and reused by handle_start, so it must be real either way.
    opponent_info = _build_opponent(int(key), chosen, chosen["roster"], opp_evaluation_point)
    match["chosen"] = {"key": key, "opponent_info": opponent_info,
                       "team_data_array": chosen["team_data_array"],
                       "roster": chosen["roster"], "team_class": opp_team_class,
                       "evaluation_point": opp_evaluation_point}

    my_roster = _roster_by_id(viewer_id)
    # Same runner count on both sides -- see handle_start's own note. Sizing
    # the opponent from THEIR class let a higher-class player field more
    # horses than the opponent could and win every race on headcount.
    my_unit_max = opp_unit_max = _unit_max_num(st.get("team_class", 1))
    frame_orders = []
    per_distance_seed = random.Random((int(viewer_id) << 8) ^ int(key.lstrip("-") or 0))
    for distance_type in _VALID_DISTANCE_TYPES:
        # team_stadium_class.unit_max_num (1/2/3 by team_class) caps how many
        # of a side's 3 roster slots actually race per distance group --
        # this is what makes Quinella (2-member) vs Trio (3-member) team
        # results possible at all (team_stadium_raw_score condition_type=11).
        mine = [e for e in st["team_data_array"]
               if e["distance_type"] == distance_type and e["trained_chara_id"]][:my_unit_max]
        theirs = [e for e in chosen["team_data_array"]
                 if e["distance_type"] == distance_type and e["trained_chara_id"]][:opp_unit_max]
        # (slot, is_mine) pairs, NOT slot-value membership -- two different
        # accounts' slots are plain {distance_type, member_id,
        # trained_chara_id, running_style} dicts with no owner tag, and
        # small per-account trained_chara_id spaces make an exact value
        # collision between MY slot and the OPPONENT's entirely plausible
        # (confirmed live 2026-08-24: two real accounts both fielding their
        # own local id 3 at member_id=1/running_style=2). `slot in mine`
        # matches on dict VALUE equality, so a coincidental collision
        # silently mislabeled the opponent's horse as mine (and vice versa,
        # skipping the real opponent's entry entirely) -- corrupting both
        # the race field and every score attributed to it downstream.
        field = [(e, True) for e in mine] + [(e, False) for e in theirs]
        if not field:
            continue
        gates = per_distance_seed.sample(range(max(8, len(field))), len(field))
        random_info = []
        for i, (slot, is_mine) in enumerate(field):
            chara = (my_roster if is_mine else opp_roster).get(slot["trained_chara_id"]) or {}
            random_info.append({
                "viewer_id": int(viewer_id) if is_mine else int(opponent_info.get("opponent_viewer_id") or 0),
                "member_id": slot["member_id"], "trained_chara_id": slot["trained_chara_id"],
                "running_style": slot["running_style"], "frame_order": gates[i] + 1,
                "motivation": chara.get("motivation", 3),
            })
        frame_orders.append({"distance_type": distance_type, "race_order": distance_type,
                             "random_info_array": random_info})
    match["frame_orders"] = frame_orders
    state_store.save_state(viewer_id, full_state)

    return _ok({
        "frame_order_info_array": frame_orders,
        "user_team_data_array_copy": _wire_team_data_array(st["team_data_array"]),
        "user_trained_chara_array_copy": _wire_trained_chara_array(st["team_data_array"], my_roster),
        "opponent_info_copy": opponent_info,
        "opponent_chara_info_array_latest_copy": _wire_trained_chara_array(
            chosen["team_data_array"], chosen["roster"]),
        "winning_reward_guarantee_status": 0,
    })


def _score_horse(i, horse, ri, place, sim, course, mid_leaders, final_leaders,
                  favorite_ranks, standard_time, winner_idx, margin_len, n,
                  skill_events_by_horse, kakari_by_horse) -> tuple[list, int]:
    """One horse's raw (pre-modifier) score_array line items + their sum.
    Every component here is a real team_stadium_raw_score row (see the
    module-level note above _placement_scores) except the margin/positioning/
    dark-horse/beat-time TRIGGER conditions, which are reconstructed from the
    race sim itself (see _margin_index, _phase_leaders, _favorite_ranks,
    _standard_course_time's own docstrings for exactly what's inferred vs
    real)."""
    result = sim["results"][i]
    lines = []

    def add(row, num):
        row_id, score = row
        if row_id and score:
            lines.append({"raw_score_id": row_id, "num": int(num),
                          "score": int(score), "bonus_array": []})

    add(_place_score(place), place)

    if i == winner_idx and n > 1:
        add(_margin_score(margin_len), _margin_index(margin_len))

    add(_dark_horse_score(favorite_ranks[i], place), favorite_ranks[i])

    if standard_time:
        beat = standard_time - result["finishTimeRaw"]
        if beat > 0:
            add(_beat_target_time_score(beat), min(20, int(beat * 10)))

    add(_strong_start_score(result.get("startDelayTime") or 0.0), 1)

    if i in mid_leaders:
        add(_positioning_score(True), 1)
    if i in final_leaders:
        add(_positioning_score(False), 2)

    for ev in skill_events_by_horse.get(i, []):
        lvl = _skill_level_for(horse, ev["skillId"])
        add(_skill_activation_score(ev["skillId"], lvl), ev["skillId"])

    return lines, sum(l["score"] for l in lines)


@registry.endpoint("team_stadium/start")
def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    chosen = match.get("chosen")
    frame_orders = match.get("frame_orders")
    if not chosen or not frame_orders:
        return _refuse()   # must decide_frame_order first

    # RP IS SPENT HERE, at the top, before any of the five races is simulated.
    # team_stadium/start is the only endpoint in the family whose response
    # carries rp_info (TeamStadiumStartResponse.CommonResponse, dump.cs), and
    # its request's item_id_array is how the client hands over Carrot Jelly to
    # cover a match it cannot otherwise afford -- so the items are burned
    # first, then the match price is taken from the topped-up pool. Neither
    # this endpoint nor any other in team_stadium/* has ever been captured, so
    # the ORDER (items then price) is inferred; it is the only order in which
    # spending a jelly to afford a match can work.
    used_item_ids = stamina.consume_recovery_items(
        full_state, payload.get("item_id_array"))
    rp_cost = stamina.team_trials_rp_cost()
    if stamina.RP.spend(full_state, rp_cost) is None:
        log.info("team_stadium/start refused: viewer %s has %s RP, needs %s",
                 viewer_id, stamina.RP.read(full_state).get("current_rp"), rp_cost)
        state_store.save_state(viewer_id, full_state)   # keep any jelly spent
        return _refuse()

    my_roster = _roster_by_id(viewer_id)
    opp_roster = _index_roster(chosen["roster"])   # see _index_roster's docstring
    course_rng = random.Random((int(viewer_id) << 16) ^ int(chosen["key"].lstrip("-") or 0))

    # Ace Bonus (+10% to member_id==1's own scores) and Opponent Rating Bonus
    # (opponent_evaluation_point/200000, see the module note above
    # _score_bonus_rate_raw) both come straight from team_stadium_score_bonus.
    # Applied SYMMETRICALLY: my horses get a bonus keyed to the OPPONENT's
    # rating, and (since the opponent is a real fielded side too, not just a
    # backdrop) their horses get one keyed to MY rating -- otherwise a strong
    # player would systematically under-score weak opponents relative to how
    # the real game would have scored that same matchup from the other side,
    # skewing win/loss unfairly in the player's favor every time.
    ace_ids = {e["trained_chara_id"] for e in st["team_data_array"] if e.get("member_id") == 1}
    opp_ace_ids = {e["trained_chara_id"] for e in chosen["team_data_array"] if e.get("member_id") == 1}
    my_ep = _team_evaluation_point(st["team_data_array"], my_roster)
    opponent_ep = chosen.get("evaluation_point") or 0
    ace_rate = _ace_bonus_rate()
    support_rate = _support_bonus_rate()
    opponent_bonus_for_me = _opponent_bonus_rate(opponent_ep)
    opponent_bonus_for_them = _opponent_bonus_rate(my_ep)
    # Both sides field the SAME number of runners. BUG FIXED 2026-08-28:
    # this used to size each side from its OWN team_class, so a class-2
    # player (unit_max 2) fielding two horses against a class-1 opponent
    # (unit_max 1) fielding one took 2 of the 3 finishing places in every
    # race and banked two horses' worth of score against one -- an
    # unloseable match that had nothing to do with how the horses actually
    # ran. Real matchmaking pairs you inside your own class, which is what
    # _real_opponent_candidates now enforces; this is the belt-and-braces
    # half of that fix, for a fallback/mob opponent.
    my_unit_max = opp_unit_max = _unit_max_num(st.get("team_class", 1))

    race_start_params_array = []
    race_result_array = []
    consecutive = st.get("consecutive_win_count") or 0

    # PLAN every round first, then simulate them all, then score. The five
    # races of a match are independent (each one is a pure, separately seeded
    # function of its own field and course), so they can run concurrently --
    # see race_simulator.run_races. Scoring stays strictly sequential below
    # because the Consecutive Win Bonus depends on the previous round's
    # result. Splitting the loop this way is what lets a heavy roster finish
    # inside the client's request timeout instead of returning a response it
    # has already given up waiting for.
    plans = []
    for fo in frame_orders:
        distance_type = fo["distance_type"]
        course_instance = _pick_course(distance_type, course_rng)
        if course_instance is None:
            continue
        course_info = race_simulator.get_course_for_race_instance(course_instance)
        if course_info is None:
            continue
        course_set_id, course, gate_count = course_info

        horses, team_ids, ris = [], [], []
        for ri in fo["random_info_array"]:
            is_mine = ri["viewer_id"] == int(viewer_id)
            chara = (my_roster if is_mine else opp_roster).get(ri["trained_chara_id"])
            if not chara:
                continue
            chara = dict(chara)
            chara["running_style"] = ri["running_style"]
            horses.append(chara)
            team_ids.append(1 if is_mine else 2)
            ris.append(ri)
        if not horses:
            continue
        n = len(horses)

        gate_assignment = [ri["frame_order"] - 1 for ri in ris]
        weather, ground, season = 1, 1, 1   # INFERRED fixed fair-weather race --
                                            # no capture confirms whether Team
                                            # Stadium rolls real weather/season.
        # Drawn here, in frame_order order, exactly as before -- the course
        # rng must be consumed in the same sequence whether or not the races
        # are later run in parallel.
        seed = course_rng.randint(0, 2**31 - 1)
        plans.append({
            "distance_type": distance_type, "course_instance": course_instance,
            "course": course, "gate_count": gate_count, "n": n,
            "horses": horses, "team_ids": team_ids, "ris": ris,
            "weather": weather, "ground": ground, "season": season, "seed": seed,
            "job": {"horses": horses, "course_set_id": course_set_id,
                    "ground": ground, "weather": weather, "season": season,
                    "seed": seed, "gate_count": gate_count,
                    "gate_assignment": gate_assignment, "n": n},
        })

    outcomes = race_simulator.run_races([pl["job"] for pl in plans])

    for plan, outcome in zip(plans, outcomes):
        distance_type = plan["distance_type"]
        course_instance = plan["course_instance"]
        course, gate_count, n = plan["course"], plan["gate_count"], plan["n"]
        horses, team_ids, ris = plan["horses"], plan["team_ids"], plan["ris"]
        weather, ground, season = plan["weather"], plan["ground"], plan["season"]
        seed = plan["seed"]
        sim, scenario_b64 = outcome["sim"], outcome["scenario"]

        finish_order = sorted(range(n), key=lambda i: sim["results"][i]["finishTime"])
        place_of = {horse_i: rank + 1 for rank, horse_i in enumerate(finish_order)}
        running_styles = [h.get("running_style", 2) for h in horses]
        favorite_ranks = _favorite_ranks(horses)
        mid_leaders = _phase_leaders(sim["frames"], running_styles, course["distance"] * (1 / 6))
        final_leaders = _phase_leaders(sim["frames"], running_styles, course["distance"] * (2 / 3))
        standard_time = _standard_course_time(course)

        winner_idx = finish_order[0]
        margin_len = 0.0
        if n > 1:
            second_idx = finish_order[1]
            winner_finish_t = sim["results"][winner_idx]["finishTimeRaw"]
            second_pos = _position_at(sim["frames"], second_idx, winner_finish_t)
            margin_len = max(0.0, (course["distance"] - second_pos) / LENGTH_METERS)

        skill_events_by_horse: dict = {}
        kakari_by_horse: dict = {}
        for ev in sim.get("skillEvents", []):
            hi = ev["horseIndex"]
            if ev["skillId"] == "kakari":
                kakari_by_horse[hi] = kakari_by_horse.get(hi, 0.0) + (ev.get("duration") or 0.0)
            else:
                skill_events_by_horse.setdefault(hi, []).append(ev)

        # Pass 1: raw score + Ace/Opponent/Support modifiers (NOT Consecutive
        # -- that depends on THIS round's own win/loss, decided below).
        provisional = []   # per-horse: [lines, raw_subtotal, bonus_specs, rush_row]
        for i, (horse, team_id, ri) in enumerate(zip(horses, team_ids, ris)):
            place = place_of[i]
            lines, raw_subtotal = _score_horse(
                i, horse, ri, place, sim, course, mid_leaders, final_leaders,
                favorite_ranks, standard_time, winner_idx, margin_len, n,
                skill_events_by_horse, kakari_by_horse)
            is_mine = team_id == 1
            ace_set = ace_ids if is_mine else opp_ace_ids
            # (score_bonus_id, rate) pairs, so each modifier can be itemised
            # into every line's own bonus_array -- see _apply_line_bonuses.
            specs = []
            if support_rate:
                specs.append((_score_bonus_id(5, 0), support_rate))
            if ri["trained_chara_id"] in ace_set and ace_rate:
                specs.append((_score_bonus_id(1, 1), ace_rate))
            opp_rate = opponent_bonus_for_me if is_mine else opponent_bonus_for_them
            if opp_rate:
                specs.append((_score_bonus_id(3, 0), opp_rate))
            rush_row = _rush_penalty(kakari_by_horse.get(i, 0.0))
            provisional.append([lines, raw_subtotal, specs, rush_row])

        def _total(idx):
            _lines, raw_subtotal, specs, rush_row = provisional[idx]
            rate = sum(r for _bid, r in specs)
            return round(raw_subtotal * (1 + rate)) + rush_row[1]

        team_scores = {1: 0, 2: 0}
        for i in range(n):
            team_scores[team_ids[i]] += _total(i)
        my_score, opp_score = team_scores[1], team_scores[2]
        win_type = 1 if my_score > opp_score else (2 if opp_score > my_score else 3)

        # Pass 2: Consecutive Win Bonus -- only the winning side, only once
        # win/loss for THIS round is known.
        if win_type == 1:
            consecutive += 1
            bonus_rate = _consecutive_bonus_rate(consecutive)
            if bonus_rate:
                streak_id = _score_bonus_id(4, min(consecutive, 5))
                for i in range(n):
                    if team_ids[i] == 1:
                        provisional[i][2].append((streak_id, bonus_rate))
        elif win_type == 2:
            consecutive = 0

        team_scores = {1: 0, 2: 0}
        chara_result_array = []
        places_by_team = {1: [], 2: []}
        for i, (horse, team_id, ri) in enumerate(zip(horses, team_ids, ris)):
            lines, _raw_subtotal, specs, rush_row = provisional[i]
            rush_id, rush_penalty = rush_row
            if rush_penalty:
                lines.append({"raw_score_id": rush_id, "num": 0,
                              "score": rush_penalty, "bonus_array": []})
            # The line scores ARE the total now -- the client re-derives the
            # same number from score_array, so nothing may be added on top of
            # them here or the two views diverge again.
            total = _apply_line_bonuses(lines, specs)
            team_scores[team_id] += total
            places_by_team[team_id].append(place_of[i])
            chara_result_array.append({
                "viewer_id": ri["viewer_id"], "frame_order": ri["frame_order"],
                "trained_chara_id": ri["trained_chara_id"], "team_id": team_id,
                "finish_order": place_of[i], "finish_time": int(sim["results"][i]["finishTimeRaw"] * 10000),
                "score_array": lines,
            })

        my_tr_id, my_team_result = _team_result_score(
            places_by_team[1], min(my_unit_max, len(places_by_team[1])))
        _opp_tr_id, opp_team_result = _team_result_score(
            places_by_team[2], min(opp_unit_max, len(places_by_team[2])))
        my_score, opp_score = team_scores[1] + my_team_result, team_scores[2] + opp_team_result
        win_type = 1 if my_score > opp_score else (2 if opp_score > my_score else 3)

        # Only the team-WIDE lines belong in team_score_array. It used to
        # re-list the per-horse sum under raw_score_id 3 ("final position"),
        # which the score-details tab then counted a second time on top of
        # the same horses' own score_array entries. Team Results
        # (Trio/All-Placed/Quinella) is the one real entry, and it carries no
        # modifiers (GameTora names the Opponent bonus as applying to the
        # match Victory award only).
        team_score_array = []
        if my_team_result:
            team_score_array.append({"raw_score_id": my_tr_id, "num": len(places_by_team[1]),
                                     "score": my_team_result, "bonus_array": []})

        race_start_params_array.append({
            "round": distance_type, "race_instance_id": course_instance,
            "season": season, "weather": weather, "ground_condition": ground,
            "random_seed": seed,
            "race_horse_data_array": _team_race_horse_array(horses, ris),
            "self_evaluate": my_score, "opponent_evaluate": opp_score,
        })
        race_result_array.append({
            "distance_type": distance_type, "race_scenario": scenario_b64,
            "round": distance_type,
            "team_score_array": team_score_array,
            "team_total_score": my_score, "win_type": win_type,
            "current_consecutive_win_count": consecutive,
            # Wire fields carry the master table's own raw integer scale
            # (score_rate, e.g. 300 = 3%) -- NOT the /10000.0 float fraction
            # _consecutive_bonus_rate returns for internal score math. A raw
            # Python float here msgpack-encodes as float64, which the real
            # client's typed deserializer rejects outright (live-reported:
            # "Deserialize error ... code:203 format:float 64" on this exact
            # response -- every int-typed field in this module must stay an
            # int all the way to the wire, never a bare rate/fraction).
            "bonus_rate_by_next_win": (_score_bonus_rate_raw(4, min(consecutive + 1, 5))
                                       if consecutive + 1 >= 2 else 0),
            "chara_result_array": chara_result_array,
        })

    match["rounds"] = race_result_array
    # Rolled here, not at all_race_end: TeamStadiumWinningRewardInfo is part
    # of THIS response (winning_reward_info_array) so the client can show the
    # right box on each race's result card, and all_race_end must then open
    # exactly those boxes rather than roll a second, different set.
    match["win_boxes"] = _roll_win_boxes(
        race_result_array, random.Random(course_rng.randint(0, 2**31 - 1)))
    st["consecutive_win_count"] = consecutive
    state_store.save_state(viewer_id, full_state)

    return _ok({
        "use_item_id_array": used_item_ids,
        "rp_info": stamina.rp_info(full_state),
        "race_start_params_array": race_start_params_array,
        "race_result_array": race_result_array,
        "item_info_array": _wire_item_info(full_state, used_item_ids),
        "is_include_unsupported_race": False,
        "winning_reward_info_array": match["win_boxes"],
        "winning_reward_guarantee_status": 0,
        "last_checked_round": match.get("last_checked_round", 0),
        "support_card_bonus": 0,
        "user_team_data_array_copy": _wire_team_data_array(st["team_data_array"]),
        "user_trained_chara_array_copy": _wire_trained_chara_array(st["team_data_array"], my_roster),
        "opponent_info_copy": chosen["opponent_info"],
        "opponent_chara_info_array_latest_copy": _wire_trained_chara_array(
            chosen["team_data_array"], opp_roster),
    })


@registry.endpoint("team_stadium/replay_check")
def handle_replay_check(payload: dict) -> dict:
    """Mirrors daily_races.py's own replay_check pattern: a per-round
    "finished watching this animation" pacing echo, not a gate we enforce
    (nothing here blocks all_race_end on it -- no capture shows the real
    server ever refusing an early all_race_end call)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    round_num = payload.get("round") or 0
    match["last_checked_round"] = max(match.get("last_checked_round", 0), round_num)
    state_store.save_state(viewer_id, full_state)
    return _ok({"last_checked_round": match["last_checked_round"]})


@registry.endpoint("team_stadium/all_race_end")
def handle_all_race_end(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    rounds = match.get("rounds") or []
    if not rounds:
        return _refuse()   # must start (and actually run the races) first

    wins = sum(1 for r in rounds if r["win_type"] == 1)
    all_won = wins == len(rounds) and len(rounds) > 0
    # Victory (team_stadium_raw_score condition_type=10): +10000 for winning
    # 3+ of the match's 5 races -- NOT all 5 (that's a stricter bar this
    # code used to conflate with "all_won", which is still used below for
    # class promotion since no better population-percentile signal exists
    # here). Only the Opponent Rating Bonus modifies it, per the article.
    match_won = wins >= 3
    bonus = 0
    if match_won:
        chosen = match.get("chosen") or {}
        opponent_bonus = _opponent_bonus_rate(chosen.get("evaluation_point") or 0)
        bonus = round(_raw_score(10, 1, 1) * (1 + opponent_bonus))
    final_total = sum(r["team_total_score"] for r in rounds) + bonus

    # NOT feeding team_evaluation_point into best_point here (BUG FIXED
    # 2026-08-25, user-corrected: best_point is this week's Team Trial
    # SCORE -- built from actual race results, skills, opponent rank/rating,
    # etc. -- a completely different number from the roster-strength
    # evaluation_point; a real account had these sit at 1,010,001 and
    # ~320,000 simultaneously). Recomputed here only to keep
    # team_evaluation_point itself fresh in case the roster changed since
    # decide_frame_order; it also still separately updates evaluation_peak
    # for tier-reward purposes, same as team_edit.
    st["team_evaluation_point"] = _team_evaluation_point(
        st["team_data_array"], _roster_by_id(viewer_id))
    st["evaluation_peak"] = max(st.get("evaluation_peak") or 0, st["team_evaluation_point"])
    old_best_point = st.get("best_point") or 0
    st["best_point"] = max(old_best_point, final_total)
    is_update_high_score = final_total > old_best_point

    # Class promotion/demotion via team_stadium_class's percentile bands.
    # INFERRED comparison: this server has no real opponent pool to rank
    # the player's percentile against, so "win rate this match" (all_won ->
    # promote, all_won False and a losing majority -> demote, otherwise
    # keep) stands in for the real population-percentile mechanic
    # class_up_range/class_down_range describe. Flagged as the roughest
    # approximation in this feature.
    classes = {r["team_class"]: r for r in master_data.query(
        "SELECT * FROM team_stadium_class ORDER BY team_class")}
    cur_class = st.get("team_class", 1)
    if all_won and cur_class < max(classes):
        st["team_class"] = cur_class + 1
    elif wins == 0 and cur_class > min(classes):
        st["team_class"] = cur_class - 1

    # The per-match payout is the win boxes -- one per race won, rolled in
    # handle_start so the client already knows their colours.
    winning_reward_content_array = _open_win_boxes(
        full_state, match.get("win_boxes") or [], cur_class)
    # ...plus team_stadium_class_reward, but ONLY when the class actually
    # moved (see _class_change_rewards for what this table really is).
    if st["team_class"] > cur_class:
        winning_reward_content_array += _class_change_rewards(
            full_state, st["team_class"],
            2 if st["team_class"] <= (st.get("best_team_class") or 1) else 1)
    elif st["team_class"] < cur_class:
        winning_reward_content_array += _class_change_rewards(
            full_state, st["team_class"], 4)

    st["best_team_class"] = max(st.get("best_team_class", 1), st["team_class"])

    rank = st.get("rank", 1)
    # Roster-strength tier rewards are credited here if a tier was newly
    # crossed, but they are NOT winning-reward box contents -- index/team_edit
    # already report them through reward_info_array, and the dicts this
    # returns ({item_type, item_id, item_num}) are not the
    # TeamStadiumWinningRewardContent shape the client deserializes that
    # array as. They used to be concatenated straight onto it.
    _grant_rank_rewards(full_state, st)

    mvp_chara_id = 0
    best_score = -1
    for r in rounds:
        for cr in r["chara_result_array"]:
            if cr["team_id"] == 1:
                total_line = sum(s["score"] for s in cr["score_array"])
                if total_line > best_score:
                    best_score, mvp_chara_id = total_line, cr["trained_chara_id"]

    st["current_match"] = None   # match closed -- opponent_list must run again

    # Bond: every character on the player's team gains add_love_point --
    # 2 when the match was won, 1 otherwise (VERIFIED against six real
    # all_race_end captures, differenced across the same account's
    # chara_list: the two final_win_type 1 matches moved all 15 members by
    # exactly 2, the four losses by 1). One grant per CHARACTER, not per
    # slot: a team can field the same uma only once, but a roster entry is
    # a trained_chara and two of them can share a chara_id, and the real
    # update_user_chara_array carries one row per character.
    add_love_point = 2 if match_won else 1
    my_roster = _roster_by_id(viewer_id)
    love_charas = []
    for slot in st.get("team_data_array") or ():
        record = my_roster.get(slot.get("trained_chara_id"))
        chara_id = bond.chara_id_for_card(record.get("card_id") or 0) if record else 0
        if chara_id and chara_id not in love_charas:
            love_charas.append(chara_id)
    new_chara_profile_array = []
    for chara_id in love_charas:
        bond.add_love_point(full_state, chara_id, add_love_point)
        new_chara_profile_array += bond.new_profile_entries(full_state, chara_id)
    update_user_chara_array = [dict(bond.chara_entry(full_state, c)) for c in love_charas]

    from . import campaign_walking
    walk_gauge_info = campaign_walking.gauge_info(full_state, "teamstadium")
    state_store.save_state(viewer_id, full_state)

    return _ok({
        "add_friend_point": 0, "add_fan_info_array": [],
        "add_love_point": add_love_point,
        "update_user_chara_array": update_user_chara_array,
        "reward_summary_info": None,
        "campaign_walking_gauge_info": walk_gauge_info,
        "winning_reward_content_array": winning_reward_content_array,
        "total_score_info": {"final_total_score": final_total, "all_race_result_score_bonus": bonus},
        "final_win_type": 1 if match_won else (2 if wins < len(rounds) - wins else 3),
        "is_update_high_score": int(is_update_high_score),
        "ranking_rank": rank,
        "mvp_chara_id": mvp_chara_id,
        "circle_point": 0,
        "new_chara_profile_array": new_chara_profile_array,
        "ranking": {"term_id": 0, "viewer_id": viewer_id, "team_class": st["team_class"],
                   "best_point": st["best_point"], "rank": rank},
        "border_line": [],
        "campaign_id_array": [],
    })


@registry.endpoint("team_stadium/user_detail")
def handle_user_detail(payload: dict) -> dict:
    """Echoes the player's OWN team/roster -- dump.cs names no request
    field, and no capture exists, so this just serves the same shape
    team_edit/index already build rather than guessing an "other player"
    detail view this server has no real second-account context to show
    from this particular call (opponent_list already carries the real
    other-account roster data when that matters)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    return _ok({
        "team_stadium_user": {
            "team_class": st["team_class"], "best_team_class": st["best_team_class"],
            "best_point": st["best_point"],
        },
        "team_data_array": _wire_team_data_array(st.get("team_data_array") or []),
        "trained_chara_array": _wire_trained_chara_array(
            st.get("team_data_array") or [], _roster_by_id(viewer_id)),
    })


@registry.endpoint("team_stadium/ranking")
def handle_ranking(payload: dict) -> dict:
    """No real cross-account leaderboard exists on this server (matches
    user_profile.py's friend/index convention: empty/self-only rather than
    fabricating other players' ranking positions)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    return _ok({
        "ranking_array": [{"term_id": 0, "viewer_id": viewer_id, "team_class": st["team_class"],
                           "best_point": st["best_point"], "rank": st["rank"]}],
        "summary_user_info_array": [], "user_friend_array": [],
    })
