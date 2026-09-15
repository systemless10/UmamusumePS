"""idle_single_mode/* -- "Independent Career Training" (jishu-tore), the
AFK/idle career variant where the client hands the server a trainee + deck +
race agenda and comes back later to collect a finished run, instead of
training turn by turn like single_mode_team/single_mode_live.

REAL WIRE SHAPES, cited from captures/20260818_122351/ (this account's own
real session against the real Cygames server, captured via the MITM proxy):
    0027_idle_single_mode_pre_start.json   -- deck-agenda list + last settings
    0028_idle_single_mode_start.json       -- start_chara/start_info in, a
                                              turn-1 progress_info out (same
                                              chara_info shape single_mode_team/
                                              start builds, real end_time ~50
                                              real minutes after start_time)
    0010_idle_single_mode_end.json         -- an EARLIER real completion in the
                                              same session: progress_log_info
                                              (gain summary) + end_info (the
                                              finished chara_info, playing_state
                                              1, state 3)
    0011_idle_single_mode_check_progress_log.json -- a poll with NO "data" key
                                              at all in the response (this is
                                              the only ground truth we have for
                                              this endpoint's shape)
    0012_single_mode_live_factor_select.json,
    0013_single_mode_live_finish.json      -- the SAME two calls a normal
                                              interactive run ends with. They
                                              are NOT reimplemented here --
                                              single_mode_live/* already maps
                                              onto single_mode_team.handle_
                                              factor_select / handle_finish
                                              (see main.py's scenario
                                              dict), and this module leaves the
                                              finished career sitting in
                                              single_mode_team.STATE_KEY for
                                              those SAME real handlers to
                                              finalize into a trained_chara --
                                              exactly the sequence the capture
                                              shows (end -> check_progress_log
                                              -> factor_select -> finish).

WHAT'S REAL vs. THE CHEAT
--------------------------
Real, reused: the turn-1 trainee build (single_mode_team.handle_start's own
_seed_dynamic_career -- same aptitude/support-deck/inheritance math a normal
career gets), the finish machinery (single_mode_live/factor_select+finish,
untouched), the reserved-race deck-agenda list (the ONE real capture's static
preset content, replayed verbatim -- it's generic Agenda-N content, not
per-player), and the learnable-skill set (master.mdb card_data.
available_skill_set_id -> available_skill_set, plus every is_general_skill
row, both filtered on disable_singlemode=0 the same way the real single-mode
skill shop would gate them).

Deliberately cheated (private-server instant-complete, NOT from any capture):
  * idle_single_mode/start immediately fast-forwards the SAME persisted
    career (single_mode_team.STATE_KEY) to turn 78 / state 2 (goal met) with
    every stat at 9999, skill_point at 999999, and a hint at max level for
    every skill this trainee can ever learn -- while the /start RESPONSE
    itself still honestly shows the turn-1 starting chara_info, matching the
    real capture's own shape.
  * idle_single_mode/end only "unlocks" _IDLE_READY_SECONDS (10s) after
    start instead of the real ~50 minutes -- gated for real (wall-clock),
    not merely cosmetic: calling /end before that returns the same
    no-"data" envelope real check_progress_log(0011) showed for a poll, so
    the client's own polling loop naturally waits those 10s out.
  * progress_log_info's gain breakdown and end_info.reward_summary_info are
    served genuinely ZEROED/EMPTY rather than inventing plausible-looking
    per-stat gain numbers or item drops -- nothing was actually simulated
    turn by turn, so nothing is fabricated to look like it was.

Skill-hint field shapes (verified in master.mdb, NOT assumed from the task
prompt's own working title): card_talent_hint_upgrade is genuinely the real
per-(rarity, level) COST table for card/skill_upgrade (see cards.py's
handle_skill_upgrade) -- but it only has rows for talent_level 1..3 in this
server's master.mdb (rarity 1 and 2 only; cards.py's own DATA GAP note), so
that's the ceiling used for the persistent per-owned-card hint
(collection.py's card_list[].skill_data_array, {skill_id, level}) below --
raising it further would claim a level this server's own upgrade-cost table
can't back up. The separate PER-CAREER hint ladder (chara_info.
skill_tips_array, {group_id, rarity, level}) is a different system that
single_mode_team.py already established goes to level 5
(_MAX_HINT_LEVEL, corroborated by real level-5 entries in captured finished
careers) -- reused here unchanged.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from .. import master_data
from .. import state as state_store
from . import cards, collection, registry, single_mode_team, stamina, trained_chara

log = logging.getLogger("uma-server")

# Persisted while an idle run is "in flight" (park/resume-safe -- see the
# _CAREER_STATE_KEYS() entry added in single_mode_team.py). Cleared for free
# whenever the career itself is cleared (finish/give-up/new start).
IDLE_RUN_KEY = "idle_single_mode_run"

# NOT a per-career key -- this is a standing "remember my last idle settings"
# preference, like a real account would show on the next pre_start screen.
IDLE_LAST_START_INFO_KEY = "idle_single_mode_last_start_info"

_DEFAULT_START_INFO = {
    "training_policy_ground_type": 1,
    "training_policy_param_rate_set_id": 3,
    "priority_skill_array": [],
}

# Real ~50 minutes -> a private-server-cheat ~10 seconds. Configurable via
# client_config.json's "idle_single_mode_seconds" for anyone who wants the
# genuine wait back.
_IDLE_READY_SECONDS_DEFAULT = 10
_READY_TOLERANCE_SECONDS = 2.0      # see _build_result's comment

_MAX_STAT = 999_999
_MAX_SP = 999_999
_MAX_GRADE = 10                     # admin.py's set-grade ceiling; real captured
                                    # finished careers show chara_grade 10 too.
_FINISHED_TURN = single_mode_team._CAREER_END_TURN   # 78 -- handle_finish's
                                                     # "reached the end" gate.
_CARD_HINT_LEVEL_CAP = 3            # see module docstring: card_talent_hint_
                                    # upgrade's real ceiling on THIS server.

_STATS = ("speed", "stamina", "power", "guts", "wiz")

# Copied out of the (now gitignored) captures/ corpus into server/fixtures/ so
# this stays available on a fresh checkout. Original path:
# captures/20260818_122351/0027_idle_single_mode_pre_start.json
_PRE_START_CAPTURE = (Path(__file__).resolve().parents[2] / "fixtures"
                     / "captures" / "20260818_122351"
                     / "0027_idle_single_mode_pre_start.json")


def _idle_ready_seconds() -> float:
    return float(config.get("idle_single_mode_seconds", _IDLE_READY_SECONDS_DEFAULT) or 0)


@functools.lru_cache(maxsize=1)
def _default_reserved_race_info() -> dict:
    """The deck-agenda picker (deck_num 1..8, 'Agenda N') -- static preset
    race schedules, not derived from anything player-specific (verified: the
    same 8 agendas, unrelated to this account's own deck, in the one real
    capture we have). Replayed verbatim from that capture rather than
    reimplementing a preset-generation system neither this server nor its
    master.mdb currently has a source for. Falls back to an empty single
    deck if the capture isn't present on this checkout."""
    try:
        doc = json.loads(_PRE_START_CAPTURE.read_text(encoding="utf-8"))
        return copy.deepcopy(doc["response"]["data"]["reserved_race_info"])
    except Exception:
        log.warning("idle_single_mode: pre_start capture missing at %s -- "
                    "serving an empty deck agenda", _PRE_START_CAPTURE)
        return {"default_deck_num": 0, "needs_default_confirm": 0,
                "reserved_race_array": []}


def _envelope(data: dict | None = None) -> dict:
    base = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        base["data"] = data
    return base


def _refuse() -> dict:
    """result_code 205 (PARAM_ERROR) -- what /end and /check_progress_log
    genuinely answer while the timer is still running, confirmed against
    AutoIndependentCareer's own aic/independent.py (an independent client
    of this SAME real endpoint, written against the actual Cygames server):
    its check_progress_log() docstring states outright "while the timer is
    still running there is no progress log yet and the server answers 205".
    Previously this module answered a bare response_code=1 success with no
    'data' key instead -- this project's OWN main.py has already hit this
    exact class of bug once before (see _REFUSE's comment there): serving a
    success envelope where the real server would refuse corrupts client
    state instead of being cleanly retried, which is exactly what user-
    reported 2026-08-19 "whenever it hits 0, it just softlocks me" looks
    like -- the eventual real /end call, once ready, was being preceded by
    however many polls the client made in between that each got a
    malformed 'success', which downstream state the finish flow choked on."""
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


# ------------------------------------------------------------ skill grants --

def _learnable_skill_rows(card_id):
    """Every skill_data row this card/character could ever have: its own
    available_skill_set (master.mdb card_data.available_skill_set_id ->
    available_skill_set) plus every general (is_general_skill=1) skill --
    gold variants (rarity=2) included, and deliberately NOT filtered on
    disable_singlemode: that flag gates real in-career PURCHASES (a real
    run genuinely can't buy some gold upgrades of a character's own
    skills -- see single_mode_team.py's own real skill-purchase flow,
    which does filter on it), but this whole endpoint is already an
    explicit private-server cheat (see module docstring's WHAT'S REAL vs
    THE CHEAT section), not a simulation of what a real run could acquire.
    User-corrected 2026-08-19: 'reward every skill already instead of
    skill hints, and make it so it does gold and unique skills too'."""
    ids = set()
    if card_id:
        row = master_data.query_one(
            "SELECT available_skill_set_id FROM card_data WHERE id=?", (card_id,))
        set_id = row["available_skill_set_id"] if row else None
        if set_id:
            for r in master_data.query(
                    "SELECT skill_id FROM available_skill_set WHERE available_skill_set_id=?",
                    (set_id,)):
                ids.add(r["skill_id"])
    for r in master_data.query("SELECT id FROM skill_data WHERE is_general_skill=1"):
        ids.add(r["id"])
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return master_data.query(
        f"SELECT id, group_id, rarity FROM skill_data WHERE id IN ({placeholders})",
        tuple(ids))


def _grant_all_skills(chara_info: dict) -> list[int]:
    """Directly teach every learnable skill (gold variants included) into
    chara_info['skill_array'] at level 1 -- an actual grant, not a hint --
    and separately maxes the trainee's own UNIQUE skill (single_mode_team.
    py's _grant_unique_skill/_level_unique_skill, the same real mechanism
    a normal career's unique-skill level-up events use) to its real cap.
    Previously this only set skill_tips_array (a hint the player would
    still have to spend SP to actually learn) and never touched the
    unique skill at all. Returns the flat skill_id list for
    _persist_card_hints below (unchanged -- still worth raising the
    OWNED CARD's permanent hint level for future careers, on top of this
    run's own direct grant)."""
    rows = _learnable_skill_rows(chara_info.get("card_id"))
    ids = sorted({r["id"] for r in rows})
    skills = chara_info.setdefault("skill_array", [])
    by_id = {s.get("skill_id"): s for s in skills if isinstance(s, dict)}
    for sid in ids:
        if sid not in by_id:
            skills.append({"skill_id": sid, "level": 1})
    single_mode_team._grant_unique_skill(chara_info)
    single_mode_team._level_unique_skill(chara_info, by=single_mode_team._UNIQUE_SKILL_MAX_LEVEL)
    return ids


def _persist_card_hints(full_state: dict, card_id, skill_ids: list[int]) -> None:
    """Also raises the OWNED CARD's persistent hint levels (collection.py's
    card_list[].skill_data_array, {skill_id, level}) -- the account-level
    progress card/skill_upgrade would normally buy one level at a time, and
    what future careers with this same card inherit from. Only touches a
    card that's actually in the collection (a fresh/borrowed trainee with no
    card_list row yet is left alone rather than fabricating one)."""
    if not card_id or not skill_ids:
        return
    entry = cards._card_entry(full_state, card_id)
    if entry is None:
        return
    skills = entry.setdefault("skill_data_array", [])
    by_id = {s.get("skill_id"): s for s in skills if isinstance(s, dict)}
    for sid in skill_ids:
        existing = by_id.get(sid)
        if existing is None:
            skills.append({"skill_id": sid, "level": _CARD_HINT_LEVEL_CAP})
        else:
            existing["level"] = max(existing.get("level", 0) or 0, _CARD_HINT_LEVEL_CAP)


# --------------------------------------------------------------- max-out ----

def _max_out_career(chara_info: dict) -> None:
    """The cheat itself: every stat to 9999 (and its cap raised to at least
    9999 so the training-screen bar doesn't show an over-100% gain off a
    lower cap), skill_point to 999999, and the run marked COMPLETE the same
    way single_mode_team.handle_finish recognizes one (turn >= 78 and/or
    state==2) so the real finish machinery treats it as a genuine graduate,
    not a give-up.

    BUG FIXED 2026-08-24 (live-reported: an idle-trained uma's grade/
    fan-based bonus was still tiny despite _populate_full_race_history now
    giving her every race in the game as a win). chara_info["chara_grade"]
    set below is the CAREER's own in-progress display value -- it is NOT
    what the finished trained_chara ends up with. trained_chara.py's own
    build (_chara_grade_for(fans), around line 1043) recomputes chara_grade
    FRESH from chara_info["fans"] at finish time, ignoring whatever this
    function set here entirely. Since nothing in this cheat path ever
    touched fans, a fresh turn-1 career's starting fans (1) survived
    untouched all the way to the finished record -- a maxed win record
    sitting on a single-digit fan count, the single lowest grade tier
    every single_mode_chara_grade row is satisfied by. Reusing
    trained_chara.MAXED_VETERAN_FAN_CEILING (99,999,999) rather than
    inventing a new constant: that value was deliberately chosen there to
    sit far above any real ceiling (real legitimately-maxed careers top out
    ~900k) while staying safely under the int32 wire ceiling headroom
    Team Stadium's own rank-score math needs (see that constant's own
    comment) -- the exact same overflow class of risk applies to any
    fans-driven multiplication downstream of this trained_chara too."""
    for stat in _STATS:
        chara_info[stat] = _MAX_STAT
        cap_key = f"max_{stat}"
        if (chara_info.get(cap_key) or 0) < _MAX_STAT:
            chara_info[cap_key] = _MAX_STAT
    chara_info["skill_point"] = _MAX_SP
    chara_info["vital"] = chara_info.get("max_vital", 100)
    chara_info["motivation"] = 5
    chara_info["turn"] = _FINISHED_TURN
    chara_info["state"] = 2           # goal met (handle_finish's other legacy gate)
    chara_info["playing_state"] = 1   # matches real end_info.chara_info.playing_state
    chara_info["chara_grade"] = _MAX_GRADE
    chara_info["fans"] = trained_chara.MAXED_VETERAN_FAN_CEILING


@functools.lru_cache(maxsize=1)
def _all_program_ids() -> tuple:
    """Every real single_mode_program row -- the full set of races a career
    could ever run. Cached: master.mdb is read-only for the process lifetime,
    and this is 776 rows re-read on every idle start otherwise."""
    return tuple(r["id"] for r in master_data.query("SELECT id FROM single_mode_program"))


def _populate_full_race_history(career_data: dict) -> None:
    """User-requested 2026-08-24: the idle/independent-training cheat should
    leave the finished trainee having raced (and won) EVERY race in the game,
    not just whatever _debut_race_result used to fabricate (a single Junior
    Make Debut). trained_chara._career_race_results treats career_data's
    race_history as the trusted log for the finished uma's displayed race
    record (see its own docstring) -- appending one won (result_rank=1) entry
    per single_mode_program row here is the exact same mechanism a normal
    career's own _append_race_history uses turn by turn, just populated in
    bulk since idle training has no real per-turn race_ctx to log from.
    turn/weather/ground/running_style are cosmetic placeholders (idle
    training never really ran these), same spirit as _max_out_career's own
    cheat values elsewhere in this module."""
    history = career_data.setdefault("race_history", [])
    already = {h.get("program_id") for h in history}
    for program_id in _all_program_ids():
        if program_id in already:
            continue
        history.append({
            "turn": _FINISHED_TURN, "program_id": program_id,
            "weather": 1, "ground_condition": 1, "running_style": 2,
            "result_rank": 1, "frame_order": 1, "npc_count": 0,
        })


# ------------------------------------------------------------- endpoints ----

@registry.endpoint("idle_single_mode/pre_start")
def handle_pre_start(payload: dict) -> dict:
    """idle_single_mode/pre_start -- the deck-agenda + 'last settings' screen
    shown before starting a run. See _default_reserved_race_info's docstring
    for the agenda list; last_idle_single_mode_start_info is this viewer's
    own last real idle start (genuinely remembered, not the fixture's frozen
    values) once they've started at least one."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    last = full_state.get(IDLE_LAST_START_INFO_KEY) or _DEFAULT_START_INFO
    return _envelope({
        # _reserved_race_info, not the raw capture: an agenda the player saved
        # through idle_single_mode/multi_race_reserve_deck has to show up here,
        # or the save is invisible on the very screen that made it.
        "reserved_race_info": _reserved_race_info(full_state),
        "last_idle_single_mode_start_info": copy.deepcopy(last),
    })


@registry.endpoint("idle_single_mode/start")
def handle_start(payload: dict) -> dict:
    """idle_single_mode/start -- {single_mode_start_request_common: {start_
    chara, tp_info, current_money, use_tp, current_succession_rank_point},
    start_info: {training_policy_ground_type, training_policy_param_rate_
    set_id, priority_skill_array, race_array}} -> {progress_info: {chara_
    info (turn 1), start_time, end_time, dress_id}, tp_info, ...}.

    Builds the turn-1 trainee through single_mode_team.handle_start's REAL
    dynamic-career path (same card_rarity_data stats/aptitudes, support
    deck, inheritance sparks a normal career gets) so the /start RESPONSE is
    honest -- then immediately fast-forwards the SAME persisted career
    (single_mode_team.STATE_KEY) to the maxed, finished state described in
    the module docstring, ready for idle_single_mode/end once
    _idle_ready_seconds() has elapsed."""
    viewer_id = payload["viewer_id"]
    common = payload.get("single_mode_start_request_common") or {}
    start_chara = common.get("start_chara") or {}
    start_info = payload.get("start_info") or {}

    single_mode_team.handle_start({"viewer_id": viewer_id, "start_chara": start_chara})

    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(single_mode_team.STATE_KEY)
    if not isinstance(career, dict):
        return _envelope({})   # start_chara had no usable card_id -- nothing built

    turn1_chara_info = copy.deepcopy(career["data"]["chara_info"])

    final_ci = career["data"]["chara_info"]
    _max_out_career(final_ci)
    _populate_full_race_history(career["data"])
    skill_ids = _grant_all_skills(final_ci)
    _persist_card_hints(full_state, final_ci.get("card_id"), skill_ids)

    # UTC, not local time -- datetime.now() (naive, local) sent a start_time/
    # end_time 5 hours behind this machine's real UTC (server runs UTC-5),
    # so the client's own UTC-based clock read end_time as already elapsed
    # and showed "0 seconds remaining" the instant the screen opened
    # (user-reported 2026-08-19). Every other real timestamp field in this
    # project (trained_chara.py, single_mode_team.py's friend injection,
    # etc.) already uses datetime.now(timezone.utc) for exactly this reason.
    now = datetime.now(timezone.utc)
    # end_time is stored, not just ready_at, so idle_single_mode/status can
    # re-serve the EXACT string this response hands the client rather than
    # recomputing one against a clock that has since moved.
    _end_time = (now + timedelta(seconds=_idle_ready_seconds())
                 ).strftime("%Y-%m-%d %H:%M:%S")
    full_state[IDLE_RUN_KEY] = {"ready_at": time.time() + _idle_ready_seconds(),
                                "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                                "end_time": _end_time}
    full_state[IDLE_LAST_START_INFO_KEY] = {
        "training_policy_ground_type": start_info.get(
            "training_policy_ground_type", _DEFAULT_START_INFO["training_policy_ground_type"]),
        "training_policy_param_rate_set_id": start_info.get(
            "training_policy_param_rate_set_id",
            _DEFAULT_START_INFO["training_policy_param_rate_set_id"]),
        "priority_skill_array": start_info.get("priority_skill_array", []),
    }

    # Real TP spend, off OUR authoritative tp_info_state (never the client's
    # claimed snapshot) -- same convention cards.py/shop.py use for money.
    # Now goes through the shared pool (stamina.py) so an auto-career costs
    # exactly what a hand-played one does and, crucially, PUSHES THE REFILL
    # CLOCK OUT: the old arithmetic here only ever decremented current_tp and
    # left max_recovery_time alone, so the next load/index recomputed the pool
    # against an unmoved finish line and handed the spent TP straight back.
    stamina.spend_career_tp(viewer_id, full_state, start_chara,
                            common.get("use_tp"))
    tp_state = stamina.TP.read(full_state)

    state_store.save_state(viewer_id, full_state)

    end_time = now + timedelta(seconds=_idle_ready_seconds())
    return _envelope({
        "progress_info": {
            "chara_info": turn1_chara_info,
            "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "dress_id": 0,
        },
        "tp_info": dict(tp_state),
        "user_item_array": [],
        "mission_list": [],
        "story_event_mission_list": [],
        "story_event_chara_bonus_list": [],
        "add_trained_chara_array": [],
    })


def _build_result(viewer_id) -> dict | None:
    """Shared by check_progress_log and end: None while the run isn't ready
    yet, else the {progress_log_info, end_info} payload (see module
    docstring for the real vs. cheated shape of each). Idempotent -- doesn't
    touch IDLE_RUN_KEY/STATE_KEY, so either endpoint can be polled
    repeatedly once ready without side effects; the client's own next
    calls, single_mode_live/factor_select then single_mode_live/finish
    (both real, unmodified handlers; see main._install_scenario_routes),
    are what actually consume/clear the career."""
    full_state = state_store.get_state(viewer_id) or {}
    run = full_state.get(IDLE_RUN_KEY)
    career = full_state.get(single_mode_team.STATE_KEY)
    if not run or not isinstance(career, dict):
        return None
    # A few seconds of slack: the client's own local countdown (driven by
    # the end_time string /start handed it) and our ready_at (an epoch
    # computed a moment earlier/independently) can drift apart by a couple
    # seconds -- request latency, clock granularity, whatever -- and a call
    # landing a hair before ready_at got hard-refused (205) instead. On the
    # real ~50-minute timer that's noise; on this server's 10s cheat timer
    # it's a meaningfully large fraction of the whole wait, and user-
    # reported 2026-08-19 ("Why am I getting a 205 connection error when
    # timer ends?") is consistent with exactly that gap being hit right as
    # the client's own countdown reached zero.
    if time.time() < float(run.get("ready_at") or 0) - _READY_TOLERANCE_SECONDS:
        return None

    ci = copy.deepcopy(career["data"]["chara_info"])

    def _zero_gain_block() -> dict:
        z = {"sign": 0, "value": 0}
        return {
            "speed": dict(z), "stamina": dict(z), "power": dict(z),
            "wiz": dict(z), "guts": dict(z),
            "max_speed": 0, "max_stamina": 0, "max_power": 0, "max_wiz": 0, "max_guts": 0,
            "proper_distance_short": 0, "proper_distance_mile": 0,
            "proper_distance_middle": 0, "proper_distance_long": 0,
            "proper_running_style_nige": 0, "proper_running_style_senko": 0,
            "proper_running_style_sashi": 0, "proper_running_style_oikomi": 0,
            "proper_ground_turf": 0, "proper_ground_dirt": 0,
            "skill_point": 0, "skill_tips_array": [],
        }

    return {
        "progress_log_info": {
            "chara_effect_log_array": [],
            "support_card_gain_info_array": [],
            "event_gain_info": _zero_gain_block(),
            "succession_gain_info": _zero_gain_block(),
            "succession_factor_gain_array": [],
            "race_history_array": [],
            "gain_skill_id_array": [],
            "total_skill_point": ci.get("skill_point", 0),
        },
        "end_info": {
            "chara_info": ci,
            "race_condition_array": [],
            "unchecked_event_array": [],
            "home_info": None,
            "win_saddle_id_array": [],
            "effected_factor_array": [],
            "race_start_info": None,
            "race_scenario": None,
            "add_trophy_info": None,
            "trophy_reward_info": None,
            "prev_chara_grade": None,
            "race_add_reward_info": [],
            "reserved_race_info": None,
            "mission_list": [],
            "story_event_mission_list": [],
            "start_dress_info": [],
            "story_event_chara_bonus_list": [],
            "resume_factor_select": None,
            "race_random_program_array": [],
            "race_reward_limit_more_list": [],
            "skill_filter_setting_array": [],
            "reward_summary_info": {
                "add_item_list": [], "add_piece_list": [], "add_card_list": [],
                "add_card_bonus_info": None, "add_support_card_list": [],
                "add_support_card_num_array": [], "add_honor_list": [],
                "add_chara_list": [], "add_cloth_list": [], "add_music_list": [],
                "add_story_id_array": [], "add_fcoin": 0, "add_present_num": 0,
                "add_total_fan": 0, "new_chara_profile_array": [],
                "force_update_honor_id": 0,
            },
            "is_umaplan": False,
        },
    }


@registry.endpoint("idle_single_mode/check_progress_log")
def handle_check_progress_log(payload: dict) -> dict:
    """idle_single_mode/check_progress_log -- the finished run's log, valid
    ONLY after /end. AutoIndependentCareer's aic/independent.py (an
    independent real-server client) states this outright: "while the timer
    is still running there is no progress log yet and the server answers
    205" -- confirmed real ground truth, not a guess. See _refuse()."""
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


@registry.endpoint("idle_single_mode/end")
def handle_end(payload: dict) -> dict:
    """idle_single_mode/end -- {} -> {progress_log_info, end_info} once the
    run is ready (see _build_result). Before then, refuses with result_code
    205 -- user-reported 2026-08-19, "whenever it hits 0, it just softlocks
    me": this endpoint was answering a bare response_code=1 SUCCESS with no
    'data' key while not-yet-ready, which is exactly the failure mode this
    project's own main.py._REFUSE comment already documents once before
    (serving success where the real server refuses corrupts client state
    rather than being cleanly retried). See _refuse()'s docstring for the
    real-server confirmation this should be 205, not a fake success."""
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


@registry.endpoint("idle_single_mode/result")
def handle_result(payload: dict) -> dict:
    """idle_single_mode/result -- dump.cs's IdleSingleModeResultRequest ->
    IdleSingleModeResultResponse.CommonResponse{progress_log_info, end_info},
    byte-identical field shape to /end's response, but a genuinely separate
    endpoint this module previously never implemented at all (fell through
    to main.py's NOOP_SUCCESS, a bare no-'data' envelope -- the same wrong-
    success-instead-of-refuse shape /end and check_progress_log had). Wired
    up as the likely resume-path call for "close the game... return... if
    it's ended you just go to the career finished screen"; refuses (205)
    the same way the other two do while not ready."""
    result = _build_result(payload["viewer_id"])
    return _envelope(result) if result is not None else _refuse()


# --------------------------------------------------------------------------
# dump.cs shapes:
#   IdleSingleModeStatusRequest {} -> {progress_info}
#     IdleSingleModeProgressInfo {chara_info, start_time, end_time}
#   IdleSingleModeMultiRaceReserveDeckRequest
#     {scenario_id, multi_race_reserve_deck} -> {reserved_race_info}


@registry.endpoint("idle_single_mode/status")
def handle_status(payload: dict) -> dict:
    """idle_single_mode/status -- "is a run going, and when does it land".

    The resume/poll read: the SAME progress_info /start returned, rebuilt from
    the stored run rather than remembered, so closing the game and coming back
    shows the real remaining time instead of a countdown that restarts.

    Refuses (205) when no run is in flight. That matches how this module's
    other three read endpoints behave when there is nothing to report, and is
    the shape this project has already established as correct for a
    not-yet/nothing-here idle response -- see handle_end's docstring for the
    live softlock that answering a bare success instead caused.

    start_time/end_time are the ones /start committed (UTC strings, same
    format and the same reason -- see handle_start), not recomputed here: a
    second computation against a drifting clock is exactly what would make
    the resumed countdown disagree with the one already on screen.

    NOTE this reports a run in flight OR finished-but-not-yet-collected; it
    deliberately does not gate on ready_at. "Done, go collect it" is a status,
    and /end is the endpoint that decides whether the reward is unlockable."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    run = full_state.get(IDLE_RUN_KEY)
    career = full_state.get(single_mode_team.STATE_KEY)
    if not run or not isinstance(career, dict):
        return _refuse()

    start_time = run.get("start_time") or ""
    end_time = run.get("end_time")
    if not end_time:
        # Runs started before end_time was stored alongside start_time still
        # have ready_at, which is the same instant as an epoch -- derive from
        # that rather than refusing a legitimately live run.
        ready_at = float(run.get("ready_at") or 0)
        end_time = (datetime.fromtimestamp(ready_at, timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S") if ready_at else start_time)

    return _envelope({"progress_info": {
        "chara_info": copy.deepcopy(career["data"]["chara_info"]),
        "start_time": start_time,
        "end_time": end_time,
        "dress_id": 0,
    }})


def _reserved_race_info(full_state: dict) -> dict:
    """The deck-agenda picker, with this account's own saved agendas overlaid
    on the captured defaults.

    _default_reserved_race_info() replays the one real capture verbatim, which
    is right for the CONTENT of Agenda 1..8 but wrong the moment the player
    saves over a slot -- the save would be invisible on the very next
    pre_start. Saves live account-level in single_mode_team's
    RESERVED_DECKS_KEY (the same store the interactive career's own
    multi_race_reserve writes), so the overlay here is what makes the two
    screens agree.

    Deck 0 is deliberately absent: it is the CURRENT career's live schedule,
    per-career state that an idle deck picker has no business showing."""
    info = copy.deepcopy(_default_reserved_race_info())
    saved = full_state.get(single_mode_team.RESERVED_DECKS_KEY) or {}
    if not isinstance(saved, dict) or not saved:
        return info

    decks = info.get("reserved_race_array") or []
    by_num = {d.get("deck_num"): d for d in decks}
    for key, row in saved.items():
        if not isinstance(row, dict):
            continue
        num = int(key) if str(key).isdigit() else 0
        if num <= 0:
            continue
        deck = by_num.get(num)
        if deck is None:
            deck = {"deck_num": num, "deck_name": f"Agenda {num}", "race_array": []}
            decks.append(deck)
            by_num[num] = deck
        deck["deck_name"] = row.get("deck_name") or deck.get("deck_name")
        deck["race_array"] = copy.deepcopy(row.get("race_array") or [])
    decks.sort(key=lambda d: d.get("deck_num") or 0)
    info["reserved_race_array"] = decks
    return info


@registry.endpoint("idle_single_mode/multi_race_reserve_deck")
def handle_multi_race_reserve_deck(payload: dict) -> dict:
    """idle_single_mode/multi_race_reserve_deck -- save an agenda from the
    idle run's own deck picker.

    The nested `multi_race_reserve_deck` block is the same
    {deck_num, deck_name, add_race_array, cancel_race_array} payload the
    interactive career's single_mode/multi_race_reserve takes, so the write
    goes through THAT handler -- one implementation of the add/cancel delta
    and the account-level preset store, not a second one that drifts. Its own
    response is discarded; this endpoint's declared field is
    reserved_race_info (the whole picker), not reserved_race_array.

    `scenario_id` is accepted and ignored: agendas are account-level presets
    shared across scenarios (see single_mode_team._reserved_decks), not
    per-scenario state.

    Deck 0 is refused. Through this endpoint it would write the live career's
    schedule from a screen that is picking a preset for a run that has not
    started, and it is not what the picker offers."""
    viewer_id = payload["viewer_id"]
    req = payload.get("multi_race_reserve_deck")
    if not isinstance(req, dict):
        return _refuse()
    try:
        deck_num = int(req.get("deck_num") or 0)
    except (TypeError, ValueError):
        return _refuse()
    if deck_num <= 0:
        return _refuse()

    result = single_mode_team.handle_multi_race_reserve(
        {"viewer_id": viewer_id, "multi_race_reserve_deck": req})
    if (result.get("data_headers") or {}).get("result_code") != 1:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    return _envelope({"reserved_race_info": _reserved_race_info(full_state)})
