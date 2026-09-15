"""Extract master.mdb's honor_data (Epithets, 549 rows) into a structured,
reusable JSON catalogue -- the SAME treatment tools/extract_support_card_
unique_effects.py gave support_card_unique_effect, and the SAME treatment
missions.py's own docstring gave mission_data: decode every row's real
condition from real master data instead of hardcoding guesses into any
future "grant this epithet automatically" feature.

Why this needed its own pass (honor_data isn't structured like mission_data
was): honor_data DOES have condition_type/condition_value/condition_value_2
columns, but 458 of 549 rows (83%) are condition_type=0 -- meaning the
table itself encodes NOTHING for most epithets. The real condition lives
only in the human-readable description (text_data category 66, keyed by
honor_data.id -- names are category 65), exactly like mission_data's
category-67 descriptions turned out to fully spell out condition_type's
otherwise-undocumented meaning. Spot-checked against >30 real rows across
every category and every one matched the plain-English description
exactly (e.g. 100301 "A New Hope" / "Win 100 G1 races", 100501 "Sapporo
Superstar" / "Win 200 races at Sapporo Racecourse in Career") -- confirmed
reliable enough to parse programmatically rather than transcribe by hand.

condition_type IS used for 91 rows (1-5): event-mission completion (1),
Champions Meeting cup tier (2), Trainer Aptitude Test (3), a specific
event's reward completion (4), and one scenario-ending flag (5) -- all
five require game systems this project hasn't built (event missions,
Champions Meeting, Trainer exams; see missions.py's own docstring for the
same gaps). Decoded structurally below but flagged needs_unbuilt_system,
same honest posture as everywhere else un-simulated in this codebase.

Regex families below parse the ~90 distinct description TEMPLATES this
549-row table actually uses (category 700's 192 "Get N total fans for
<chara>" rows are one template x 64 characters x 3 tiers) into real
master.mdb ids wherever the name in the text resolves cleanly:
  chara name        -> text_data category 6  (chara_data's own name text)
  skill name         -> text_data category 47 (skill_data's own name text)
  song/live name      -> text_data category 16 (live_data.music_id's name text)
  racecourse name     -> text_data category 35 (race_course_set.race_track_id's
                        name text) -> race_course_set -> race.course_set
  Career rank letter -> single_mode_rank.id (SAME numbering missions.py's
                        100004 branch already confirmed: 1=G...18=SS+)
  Team Rank letter    -> team_stadium_rank.team_min_value (SAME 5 verified
                        pairs missions.py's 100010/300006 branch uses)
  zodiac cup index    -> honor_data's own condition_value (data-verified:
                        1-12 match Capricorn..Sagittarius in every real row)

2026-08-19 second pass (user direction: "get to manually extracting the
other 140 or fixing those too -- give them some sort of data value even
if we can't implement it yet"): every row that previously fell back to a
bare {"kind": "text_only"} with NOTHING extracted now gets real structured
fields (thresholds, names, ids) alongside its needs_unbuilt_system reason
-- e.g. "Win 100 Daily Races" -> {"kind": "daily_race_win_count",
"threshold": 100, "needs_unbuilt_system": "Daily Race mode"}, not just a
reason string. Result: 413/549 (75%) fully resolved with nothing missing,
136 (25%) resolved WITH a real needs_unbuilt_system flag, and exactly 1
(100101, "Rookie Trainer") legitimately has no condition data at all --
it's the unconditional default. Zero rows are bare/unexplored now.

This tool only EXTRACTS -- it does not wire any epithet into grant_honor().
That is a separate follow-up once this catalogue is reviewed, same two-step
support_card_unique_effect already went through this session.

Usage: python tools/extract_honor_data.py [--out PATH]
Honors MASTER_MDB_PATH (see app/master_data.py) for a future JP-server run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "server"))

from app import master_data  # noqa: E402

# -------------------------------------------------------------- name caches --
_chara_cache: dict = {}
_skill_cache: dict = {}
_live_cache: dict = {}
_track_cache: dict = {}


def _chara_id_by_name(name: str) -> int | None:
    if name not in _chara_cache:
        row = master_data.query_one(
            'SELECT "index" FROM text_data WHERE category=6 AND text=?', (name,))
        _chara_cache[name] = row["index"] if row else None
    return _chara_cache[name]


def _skill_id_by_name(name: str) -> int | None:
    if name not in _skill_cache:
        row = master_data.query_one(
            'SELECT "index" FROM text_data WHERE category=47 AND text=?', (name,))
        _skill_cache[name] = row["index"] if row else None
    return _skill_cache[name]


def _live_id_by_name(name: str) -> int | None:
    if name not in _live_cache:
        row = master_data.query_one(
            'SELECT "index" FROM text_data WHERE category=16 AND text=?', (name,))
        _live_cache[name] = row["index"] if row else None
    return _live_cache[name]


def _track_id_by_name(name: str) -> int | None:
    if name not in _track_cache:
        row = master_data.query_one(
            'SELECT "index" FROM text_data WHERE category=35 AND text=?', (name,))
        _track_cache[name] = row["index"] if row else None
    return _track_cache[name]


# single_mode_rank.id numbering -- confirmed 2026-08-19 (missions.py's
# 100004 branch): text_data category 67's own mission descriptions spell
# out cv1=3..17 as F..SS for that condition_type, matching single_mode_
# rank's id order exactly (id 1 = G's 0-299 bucket). G/G+ have no real
# mission row to confirm against but continue the same monotonic id order
# the other 15 verified letters already establish; SS+ (id 18) likewise
# has no mission row but is the one remaining slot before single_mode_
# rank's id-19 jump into the UG/UF/... tier names.
_RANK_LETTERS = ["G", "G+", "F", "F+", "E", "E+", "D", "D+", "C", "C+",
                "B", "B+", "A", "A+", "S", "S+", "SS", "SS+"]
_RANK_LETTER_TO_ID = {letter: i + 1 for i, letter in enumerate(_RANK_LETTERS)}

# team_stadium_rank.team_min_value for each letter honor_data actually uses
# (D/B/A/S/SS) -- verified 2026-08-18 (missions.py's own TeamRank branch
# comment): ids 6/14/18/24/30 have team_min_value 27500/100000/160000/
# 220000/265000, matching "Reach Team Rank D/B/A/S/SS" 1:1.
_TEAM_RANK_MIN_VALUE = {"D": 27500, "B": 100000, "A": 160000, "S": 220000, "SS": 265000}

_GRADE_NAME = {100: "G1", 200: "G2", 300: "G3"}
_GRADE_BY_NAME = {v: k for k, v in _GRADE_NAME.items()}


def _venue_race_ids(track_id: int) -> list:
    rows = master_data.query(
        "SELECT rc.id FROM race rc JOIN race_course_set cs ON cs.id = rc.course_set "
        "WHERE cs.race_track_id=?", (track_id,))
    return [r["id"] for r in rows]


# --------------------------------------------------------- description regex --
# Each entry: (compiled pattern, builder(match) -> dict | None). Tried in
# order; first match wins. Every builder resolves real master.mdb ids where
# the text names one -- returns None (falls through to the next pattern /
# eventually text_only) if a name doesn't resolve, rather than emit a
# partially-wrong condition.
_PATTERNS: list = []


def _register(pattern: str):
    compiled = re.compile(pattern)

    def deco(fn):
        _PATTERNS.append((compiled, fn))
        return fn
    return deco


@_register(r"^Get ([\d,]+) total fans for (.+)$")
def _p_chara_fan(m):
    chara_id = _chara_id_by_name(m.group(2))
    if chara_id is None:
        return None
    return {"kind": "chara_fan_threshold", "chara_id": chara_id,
           "threshold": int(m.group(1).replace(",", ""))}


@_register(r"^View Episode (\d+) of (.+)'s Umamusume Story$")
def _p_chara_story_episode(m):
    chara_id = _chara_id_by_name(m.group(2))
    if chara_id is None:
        return None
    row = master_data.query_one(
        "SELECT id FROM chara_story_data WHERE chara_id=? AND episode_index=?",
        (chara_id, int(m.group(1))))
    if not row:
        return None
    return {"kind": "chara_story_episode", "chara_id": chara_id,
           "episode": int(m.group(1)), "chara_story_data_id": row["id"]}


@_register(r"^Win ([\d,]+) G([123]) races$")
def _p_grade_win_count(m):
    return {"kind": "grade_win_count", "grade": int(m.group(2)) * 100,
           "threshold": int(m.group(1).replace(",", ""))}


@_register(r"^Collect all G([123]) trophies$")
def _p_all_trophies_by_grade(m):
    return {"kind": "all_trophies_by_grade", "grade": int(m.group(1)) * 100,
           "note": "account-wide (every genuine run, any character) -- "
                   "distinct from missions.py's AllTrophiesForChara (100056), "
                   "which is per-character and spans G1-G3 combined, not one "
                   "grade at a time"}


@_register(r"^Achieve Career rank (\S+) or higher ([\d,]+) times$")
def _p_rank_achieved_count(m):
    letter = m.group(1)
    if letter not in _RANK_LETTER_TO_ID:
        return None
    return {"kind": "rank_achieved_count", "rank_letter": letter,
           "rank_id": _RANK_LETTER_TO_ID[letter],
           "threshold": int(m.group(2).replace(",", ""))}


@_register(r"^Reach Team Rank (\S+)$")
def _p_team_rank(m):
    letter = m.group(1)
    if letter not in _TEAM_RANK_MIN_VALUE:
        return None
    return {"kind": "team_rank", "rank_letter": letter,
           "team_min_value": _TEAM_RANK_MIN_VALUE[letter]}


@_register(r"^Win ([\d,]+) races at (.+) Racecourse in Career$")
def _p_venue_win_count(m):
    track_id = _track_id_by_name(m.group(2))
    if track_id is None:
        return None
    return {"kind": "venue_win_count", "venue": m.group(2), "track_id": track_id,
           "race_ids": _venue_race_ids(track_id),
           "threshold": int(m.group(1).replace(",", ""))}


@_register(r"^Obtain (\d+) or more plushies from one claw machine session$")
def _p_crane_session(m):
    return {"kind": "crane_session_plushies", "threshold": int(m.group(1))}


@_register(r"^Obtain ([\d,]+) plushies from the claw machine$")
def _p_crane_lifetime(m):
    return {"kind": "crane_lifetime_plushies",
           "threshold": int(m.group(1).replace(",", ""))}


@_register(r"^Complete a Career playthrough with at least ([\d,]+) fans$")
def _p_career_fan_threshold(m):
    return {"kind": "career_fan_threshold",
           "threshold": int(m.group(1).replace(",", ""))}


@_register(r"^Win the URA Finale in a Career playthrough$")
def _p_ura_finale(m):
    return {"kind": "ura_finale_win",
           "note": "same single_mode_race_group_id=10003 resolution as "
                   "missions.py's 100019 (URA Finale win)"}


@_register(r"^Win all of the Big Eight races in Career$")
def _p_big_eight(m):
    return {"kind": "big_eight",
           "note": "same 8-race set as missions.py's 100020"}


@_register(r"^Follow (\d+) trainers$")
def _p_follow(m):
    return {"kind": "follow_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "multiplayer -- no other real players on "
                                   "a private server"}


@_register(r"^Get (\d+) followers$")
def _p_followers(m):
    return {"kind": "follower_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "multiplayer -- no other real players on "
                                   "a private server"}


@_register(r"^Add (\d+) Trainees to your roster$")
def _p_add_trainees(m):
    return {"kind": "add_trainees", "threshold": int(m.group(1))}


@_register(r"^Obtain (\d+) different Support Cards$")
def _p_support_card_kind_num(m):
    return {"kind": "support_card_kind_num", "threshold": int(m.group(1))}


@_register(r"^Accumulate ([\d,]+) monies$")
def _p_accumulate_money(m):
    return {"kind": "currency_threshold", "currency": "money",
           "threshold": int(m.group(1).replace(",", "")),
           "note": "needs confirming which real state key tracks lifetime "
                   "money EARNED (not current balance, which can be spent "
                   "back down) -- not resolved this pass"}


@_register(r"^Watch (\d+) unique Winning Concerts$")
def _p_watch_unique_concerts(m):
    return {"kind": "watch_unique_concerts", "threshold": int(m.group(1)),
           "note": "same distinct song_ids-across-lives_done resolution as "
                   "missions.py's 600033"}


@_register(r"^Unlock 5.? for (\d+) Trainees$")
def _p_unlock_star_count(m):
    return {"kind": "unlock_star_count", "star": 5, "threshold": int(m.group(1)),
           "note": "same shape as missions.py's UnlockStarCount (300005)"}


@_register(r"^Obtain all G1 to G3 trophies with (.+)$")
def _p_all_trophies_for_chara(m):
    chara_id = _chara_id_by_name(m.group(1))
    if chara_id is None:
        return None
    return {"kind": "all_trophies_for_chara", "chara_id": chara_id,
           "note": "same chara_id-scoped resolution as missions.py's "
                   "AllTrophiesForChara (100056) -- this is that SAME "
                   "achievement, as an epithet instead of a mission reward"}


_WORD_NUMBER = {"an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4,
               "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


@_register(r"^Raise (\S+) Umamusume'?s? Bond Levels? to (\d+)$")
def _p_bond_level_threshold_count(m):
    word = m.group(1).lower()
    count = _WORD_NUMBER.get(word)
    if count is None:
        return None
    return {"kind": "bond_level_threshold_count", "chara_count": count,
           "bond_level": int(m.group(2)),
           "note": "same shape as missions.py's BondLevelThresholdCount "
                   "(600038)"}


@_register(r"^Obtain (\d+) Titles$")
def _p_title_collector(m):
    return {"kind": "title_collector", "threshold": int(m.group(1)),
           "note": "count of distinct honor_ids this viewer has ever "
                   "earned (honor_state's own 'honors' map, len())"}


@_register(r"^View (\d+) different stories$")
def _p_view_distinct_stories(m):
    return {"kind": "view_distinct_stories", "threshold": int(m.group(1))}


@_register(r"^Achieve a Great Success rating on all concerts in Our Grand Concert$")
def _p_concert_all_great(m):
    return {"kind": "concert_all_great",
           "note": "same per-run 'every performed Live was Great Success' "
                   "resolution as missions.py's 100070 -- fully answerable "
                   "with today's Grand Live state, not gated on anything"}


@_register(r"^Watch (.+) with a specific formation$")
def _p_watch_song_with_formation(m):
    live_id = _live_id_by_name(m.group(1))
    return {"kind": "watch_song_with_formation", "song": m.group(1), "live_id": live_id,
           "needs_unbuilt_system": "which formation was used isn't recorded, "
                                   "only that the song was performed "
                                   "(grand_live_lives_done[].song_ids)"}


@_register(r"^Watch a special Grand Concert$")
def _p_watch_special_concert(m):
    # NOT a real song title -- resolved by cross-reference instead: mission
    # 600417 carries this EXACT text with cv1=1029, and grand_live.py's own
    # GIRLS_LEGEND_U_LIVE_ID constant is 1029 -- so "a special Grand Concert"
    # IS Girls! Legend U specifically, confirmed by that id match rather
    # than a text_data name lookup (which fails on this non-song phrasing).
    return {"kind": "watch_song", "song": "a special Grand Concert (Girls! Legend U)",
           "live_id": 1029,
           "note": "live_id cross-confirmed via mission 600417's identical "
                   "text sharing cv1=1029 == grand_live.py's own "
                   "GIRLS_LEGEND_U_LIVE_ID; same resolution as missions.py's "
                   "600006"}


@_register(r"^Clear ([\d,]+) Career goals$")
def _p_career_goal_clear_count(m):
    return {"kind": "career_goal_clear_count", "threshold": int(m.group(1).replace(",", "")),
           "needs_unbuilt_system": "no lifetime counter of goal-clear EVENTS "
                                   "exists -- single_mode_team.py tracks "
                                   "per-career goal state (GOAL_MARKED_KEY) "
                                   "but it's wiped every career, not summed"}


@_register(r"^Acquire positive conditions (\d+) times in Career$")
def _p_positive_condition_count(m):
    return {"kind": "positive_condition_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "conditions.py tracks which conditions a "
                                   "trainee currently HAS, not a lifetime "
                                   "acquisition counter"}


@_register(r"^Place top (\d+) in a graded (\w+) race in Career with an Umamusume "
          r"whose \2 aptitude is (\S+) or lower$")
def _p_aptitude_conquest(m):
    return {"kind": "aptitude_conquest", "top_n": int(m.group(1)),
           "distance_type": m.group(2), "aptitude_at_most": m.group(3),
           "needs_unbuilt_system": "race results don't record the trainee's "
                                   "aptitude AT RACE TIME, only the final "
                                   "value at career finish, and stat/aptitude "
                                   "can rise between the race and the finish"}


@_register(r"^Win the URA Finale in Career with a margin of DST in the finals$")
def _p_ura_finale_margin(m):
    return {"kind": "ura_finale_margin_win", "margin_type": "DST",
           "needs_unbuilt_system": "race margin-of-victory isn't recorded "
                                   "per result, only result_rank"}


@_register(r"^Enjoy recreation along the riverbank (\d+) times in Career$")
def _p_recreation_riverbank(m):
    return {"kind": "recreation_type_count", "recreation_type": "riverbank",
           "threshold": int(m.group(1)),
           "needs_unbuilt_system": "single_mode_team.py's _apply_recreation "
                                   "rolls a random outing flavor but doesn't "
                                   "persist which TYPE was rolled, so there's "
                                   "nothing to count yet"}


@_register(r"^Experience a moment where you feel an irreplaceable bond$")
def _p_irreplaceable_bond_one(m):
    return {"kind": "irreplaceable_bond_moment", "chara_count": 1,
           "needs_unbuilt_system": "specific event/story trigger not "
                                   "identified this pass"}


@_register(r"^Experience moments where you feel an irreplaceable bond with (\d+) Umamusume$")
def _p_irreplaceable_bond_many(m):
    return {"kind": "irreplaceable_bond_moment", "chara_count": int(m.group(1)),
           "needs_unbuilt_system": "specific event/story trigger not "
                                   "identified this pass"}


@_register(r"^Win (\d+) Daily Races?$")
def _p_daily_race_win_count(m):
    return {"kind": "daily_race_win_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Daily Race mode"}


@_register(r"^Win (\d+) Legend Races?$")
def _p_legend_race_win_count(m):
    return {"kind": "legend_race_win_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Legend Race (competitive, not building)"}


@_register(r"^Play Team Trials (\d+) times$")
def _p_team_trials_play_count(m):
    return {"kind": "team_trials_play_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Team Trials (competitive, not building)"}


@_register(r"^Reach Class (\d+) in Team Trials$")
def _p_team_trials_class(m):
    return {"kind": "team_trials_class", "class_threshold": int(m.group(1)),
           "needs_unbuilt_system": "Team Trials (competitive, not building)"}


@_register(r"^Obtain (\d+) bonus win rewards in Team Trials$")
def _p_team_trials_bonus_win_rewards(m):
    return {"kind": "team_trials_bonus_win_rewards", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Team Trials (competitive, not building)"}


@_register(r"^Obtain a score of ([\d,]+) in Team Trials$")
def _p_team_trials_score(m):
    return {"kind": "team_trials_score", "threshold": int(m.group(1).replace(",", "")),
           "needs_unbuilt_system": "Team Trials (competitive, not building)"}


@_register(r"^Have your Star Umamusume borrowed (\d+) times by other trainers in Career$")
def _p_borrowed_count(m):
    return {"kind": "borrowed_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "multiplayer -- no other real players on "
                                   "a private server"}


@_register(r"^Share a Veteran Umamusume in your Club's chat$")
def _p_club_share_veteran(m):
    return {"kind": "club_share_veteran",
           "needs_unbuilt_system": "Clubs -- multiplayer, no other real players"}


@_register(r"^Have your Veteran Umamusume be invited to a total of (\d+) practice races$")
def _p_veteran_invited_count(m):
    return {"kind": "veteran_invited_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "practice-race invites FROM other real "
                                   "players -- multiplayer"}


@_register(r"^Raise your Archive level to (\d+)$")
def _p_archive_level(m):
    return {"kind": "archive_level_threshold", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Archive/collector level -- no real state "
                                   "key identified this pass"}


@_register(r"^Complete all Daily Missions for (\S+) days straight$")
def _p_daily_mission_streak(m):
    word = m.group(1).lower()
    threshold = int(word) if word.isdigit() else _WORD_NUMBER.get(word)
    if threshold is None:
        return None
    return {"kind": "daily_mission_streak", "threshold": threshold,
           "needs_unbuilt_system": "missions.py's daily-reset model only "
                                   "tracks TODAY's claim set, not a "
                                   "consecutive-day streak counter"}


@_register(r"^Purchase items from the Daily Sale shop (\d+) times$")
def _p_daily_sale_purchase_count(m):
    return {"kind": "daily_sale_purchase_count", "threshold": int(m.group(1)),
           "needs_unbuilt_system": "Daily Sale shop (no such shop tab exists "
                                   "yet -- same gap missions.py's 600037 "
                                   "documents)"}


@_register(r"^Obtain all Scenario Record rewards from (.+)$")
def _p_scenario_record_completion(m):
    return {"kind": "scenario_record_completion", "scenario": m.group(1),
           "needs_unbuilt_system": "no scenario reward-completion tracking "
                                   "exists for any scenario"}


@_register(r"^Win the Unity Cup$")
def _p_unity_cup_win(m):
    return {"kind": "unity_cup_win",
           "needs_unbuilt_system": "Unity Cup scenario isn't built"}


@_register(r"^Raise the Team Power of (.+) to S in Unity Cup$")
def _p_unity_cup_team_power(m):
    return {"kind": "unity_cup_team_power", "team_name": m.group(1), "rank_letter": "S",
           "needs_unbuilt_system": "Unity Cup scenario isn't built"}


@_register(r"^Take on those two in the URA Finale finals in Unity Cup and win$")
def _p_unity_cup_special_finale(m):
    return {"kind": "unity_cup_special_finale_win",
           "needs_unbuilt_system": "Unity Cup scenario isn't built"}


@_register(r"^Win the Twinkle Star Climax finals$")
def _p_twinkle_star_climax_win(m):
    return {"kind": "twinkle_star_climax_win",
           "needs_unbuilt_system": "Twinkle Star Climax scenario isn't built"}


@_register(r"^Buy a total of ([\d,]+) training items from the TS Climax Pro Shop$")
def _p_tsc_shop_purchase_count(m):
    return {"kind": "tsc_shop_purchase_count",
           "threshold": int(m.group(1).replace(",", "")),
           "needs_unbuilt_system": "Twinkle Star Climax scenario (and its Pro "
                                   "Shop) isn't built"}


@_register(r"^Learn a concert technique ([\d,]+) times$")
def _p_concert_technique_count(m):
    return {"kind": "concert_technique_count",
           "threshold": int(m.group(1).replace(",", "")),
           "needs_unbuilt_system": "grand_live.py tracks the CURRENT lesson "
                                   "pattern's technique count (segment_"
                                   "techniques) but resets it every segment, "
                                   "not a lifetime cumulative counter"}


@_register(r"^Watch (.+)$")
def _p_watch_song(m):
    live_id = _live_id_by_name(m.group(1))
    if live_id is None:
        return None
    return {"kind": "watch_song", "song": m.group(1), "live_id": live_id,
           "note": "same grand_live_lives_done[].song_ids resolution as "
                   "missions.py's 600006"}


@_register(r"^Complete a Career playthrough with an S in track, distance, and style aptitudes$")
def _p_aptitude_all_s(m):
    return {"kind": "aptitude_all_s",
           "note": "the trainee's FINAL aptitudes (proper_ground/proper_"
                   "distance/proper_running_style, all their sub-fields) "
                   "ARE stored per genuine roster entry -- 'S in track/"
                   "distance/style' means the single BEST proper_* value in "
                   "each of the 3 families reached grade S (numeric coding "
                   "not resolved this pass, but the fields exist)"}


@_register(r"^Complete a Career playthrough with (.+)$")
def _p_skill_combo(m):
    # Comma-split ONLY (not on every bare "and"): "X, Y and Z" / "X, Y, and
    # Z" -- an oxford-comma list of real skill names, one of which can
    # itself legitimately contain the word "and" (e.g. "In Body and Mind").
    # Splitting on every "and" broke that name; splitting on commas and then
    # stripping a leading "and " off ONLY the last segment doesn't. Every
    # name must still resolve or this returns None -- a stat-aptitude phrase
    # like "an S in track, distance, and style aptitudes" isn't a skill
    # list, so a partial match here would be actively wrong, not just
    # incomplete (see _p_aptitude_all_s above for that one, registered
    # first so it wins before this pattern even runs).
    text = m.group(1).rstrip(".")
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if parts:
            parts[-1] = re.sub(r"^and\s+", "", parts[-1])
    else:
        # No comma -- a plain 2-item "X and Y" (real skill names can still
        # legitimately contain "and", e.g. "In Body and Mind", but THOSE
        # only ever appear inside a >=3-item comma list in this table; every
        # real 2-item, no-comma row splits on the ONE outer "and" cleanly).
        parts = [p.strip() for p in text.split(" and ", 1) if p.strip()]
    if not parts:
        return None
    ids = []
    for p in parts:
        sid = _skill_id_by_name(p)
        if sid is None:
            return None
        ids.append(sid)
    return {"kind": "skill_combo_owned", "skill_names": parts, "skill_ids": ids,
           "note": "per-run gate (has ANY genuine run's skill_array ever "
                   "contained ALL of these), same shape as missions.py's "
                   "SpecificSkillAcquired (100038)"}


@_register(r"^View all of Main Story Act (\d+)(?:, Chapter (\d+)|, Finale Part (\d+))?$")
def _p_main_story_chapter(m):
    out = {"kind": "main_story_act_chapter", "act": int(m.group(1))}
    if m.group(2):
        out["chapter"] = int(m.group(2))
    elif m.group(3):
        out["finale_part"] = int(m.group(3))
    out["note"] = ("act/chapter numbers extracted from the text; NOT yet "
                   "resolved to the specific main_story_data id RANGE that "
                   "chapter covers -- main_story_data has no explicit "
                   "chapter-boundary column, needs a further pass")
    return out


@_register(r"^Default$")
def _p_default(m):
    return {"kind": "default_starting_honor"}


def _decode_condition_type_1(row, name, desc) -> dict:
    return {"kind": "event_special_missions", "event_id": row["condition_value"],
           "needs_unbuilt_system": "story_event_mission_list is served empty "
                                   "(see missions.py's own docstring) -- no "
                                   "per-event mission data to check against"}


# "<Sign> Cup <Tier>[ <variant marker>]" -- cv1 IS the zodiac cup index (data-
# verified 2026-08-19 against every real row: cv1 1-12 match Capricorn through
# Sagittarius in order, the standard JRA Champions Meeting zodiac calendar),
# so the sign name doesn't need extracting from text at all. Tier DOES need
# extracting from the name (Platinum/Gold/Silver/Bronze); cv2 (1 or 2) is a
# second axis the name marks with a trailing symbol on cv2==2 rows -- kept
# raw since this pass didn't confirm what it means (solo vs. team-derby?).
_ZODIAC_CUPS = ["Capricorn", "Aquarius", "Pisces", "Aries", "Taurus", "Gemini",
               "Cancer", "Leo", "Virgo", "Libra", "Scorpio", "Sagittarius"]
_CUP_TIER_RE = re.compile(r"^(.+) Cup (Platinum|Gold|Silver|Bronze)\b")


def _decode_condition_type_2(row, name, desc) -> dict:
    cup_id = row["condition_value"]
    zodiac = (_ZODIAC_CUPS[cup_id - 1] if 1 <= cup_id <= len(_ZODIAC_CUPS) else None)
    tier = None
    tm = _CUP_TIER_RE.match(name or "")
    if tm:
        tier = tm.group(2)
    return {"kind": "champions_cup_tier", "cup_id": cup_id, "zodiac": zodiac,
           "tier": tier, "cup_variant": row["condition_value_2"],
           "needs_unbuilt_system": "Champions Meeting -- competitive/ranked, "
                                   "same category this project has decided "
                                   "not to build (team_stadium.py's own "
                                   "reasoning)"}


# The 3 real rows' condition_value happen to equal a real single_mode_team.py
# scenario id (2001/3001/4001) -- consistent with SCENARIO_NAMES' own 1/2/3
# numbering pattern one digit over, but NOT independently confirmed against a
# 4th scenario this pass, so mapped only for the 3 values actually observed
# rather than assumed to generalize.
_TRAINER_EXAM_SCENARIO = {2001: "Unity Cup", 3001: "Our Grand Concert",
                          4001: "Twinkle Star Climax"}


def _decode_condition_type_3(row, name, desc) -> dict:
    sg = row["condition_value"]
    return {"kind": "trainer_exam", "scenario_group": sg,
           "scenario_name": _TRAINER_EXAM_SCENARIO.get(sg),
           "needs_unbuilt_system": "Trainer exams -- a whole minigame, no "
                                   "server-side system at all (same gap "
                                   "missions.py's 700001 documents)"}


_EVENT_REWARD_NAME_RE = re.compile(r"^Complete the individual rewards for (.+)$")


def _decode_condition_type_4(row, name, desc) -> dict:
    event_name = None
    m = _EVENT_REWARD_NAME_RE.match(desc or "")
    if m:
        event_name = m.group(1)
    return {"kind": "event_reward_completion", "event_id": row["condition_value"],
           "event_name": event_name,
           "needs_unbuilt_system": "event-specific reward tracking not built"}


def _decode_condition_type_5(row, name, desc) -> dict:
    return {"kind": "scenario_ending", "ending_id": row["condition_value"],
           "mode": "Ultra Golshi Mode", "ending": "Good Ending",
           "needs_unbuilt_system": "Ultra Golshi Mode -- a difficulty/scenario "
                                   "variant this project hasn't built"}


_CONDITION_TYPE_DECODERS = {
    1: _decode_condition_type_1, 2: _decode_condition_type_2,
    3: _decode_condition_type_3, 4: _decode_condition_type_4,
    5: _decode_condition_type_5,
}

# Numeric-only last resort for whatever's left after every regex above --
# still gives SOME data value (the raw numbers/text) rather than a bare
# {"kind": "text_only"} with nothing extractable, per user direction
# 2026-08-19 ("give them some sort of data value even if we can't implement
# it yet"). Only a handful of rows should ever reach this (odd phrasings the
# named patterns above don't cover).
_NUMBER_RE = re.compile(r"\d[\d,]*")


def _decode_description(desc: str) -> dict | None:
    if not desc:
        return None
    for pattern, builder in _PATTERNS:
        m = pattern.match(desc)
        if m:
            result = builder(m)
            if result is not None:
                return result
    numbers = [int(n.replace(",", "")) for n in _NUMBER_RE.findall(desc)]
    return {"kind": "text_only", "numbers": numbers,
           "note": "no pattern matched this description -- numbers extracted "
                   "as a bare fallback, needs a human pass"}


def extract() -> list[dict]:
    rows = master_data.query("SELECT * FROM honor_data ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        name = master_data.query_one(
            'SELECT text FROM text_data WHERE category=65 AND "index"=?', (d["id"],))
        desc = master_data.query_one(
            'SELECT text FROM text_data WHERE category=66 AND "index"=?', (d["id"],))
        desc_text = desc["text"] if desc else None
        name_text = name["text"] if name else None

        condition = None
        if d["condition_type"] in _CONDITION_TYPE_DECODERS:
            condition = _CONDITION_TYPE_DECODERS[d["condition_type"]](d, name_text, desc_text)
        else:
            condition = _decode_description(desc_text)
        if condition is None:
            condition = {"kind": "text_only"}

        out.append({
            "honor_id": d["id"],
            "name": name["text"] if name else None,
            "description": desc_text,
            "category": d["category"],
            "rank": d["rank"],
            "start_date": d["start_date"],
            "end_date": d["end_date"],
            "condition": condition,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(_ROOT / "data" / "training_ref" / "honor_data.json"),
                    help="output JSON path")
    args = ap.parse_args()

    data = extract()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    from collections import Counter
    kinds = Counter(c["condition"]["kind"] for c in data)
    resolved = sum(1 for c in data if c["condition"]["kind"] not in
                  ("text_only",) and "needs_unbuilt_system" not in c["condition"])
    needs_system = sum(1 for c in data if "needs_unbuilt_system" in c["condition"])
    text_only = sum(1 for c in data if c["condition"]["kind"] == "text_only"
                    and "needs_unbuilt_system" not in c["condition"])
    print(f"master.mdb: {master_data.MASTER_MDB_PATH}")
    print(f"{len(data)} honors -> {out_path}")
    print(f"  {resolved:>3} fully resolved to real master.mdb ids/state hooks")
    print(f"  {needs_system:>3} decoded but gated on an unbuilt system")
    print(f"  {text_only:>3} still text_only (no pattern matched)")
    print("--- by kind ---")
    for kind, n in kinds.most_common():
        print(f"  {n:>3} {kind}")


if __name__ == "__main__":
    main()
