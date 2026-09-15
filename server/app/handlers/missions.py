"""Mission system -- mission/index, mission/receive.

Previously entirely unimplemented: honor/index (user_profile.py) served
mission_list / story_event_mission_list as permanently-empty arrays, so
the mission tab could never show, track, or pay out anything, no matter
what you actually did in-game.

The endpoint PATH "mission/index" is live-confirmed (2026-08-18 server
log: the real client calls mission/index, not mission/load -- dump.cs's
class names (MissionLoadRequest/MissionLoadTask) turned out to be a false
lead; there's no MissionIndex class anywhere in the dump, so path strings
apparently aren't always derivable from a request class's name the way
other endpoints in this codebase suggested. Caught immediately because the
first live test after deploying mission/load showed "no handler/fixture
for mission/index" in the log while mission/load sat unreachable).

The RESPONSE shape also turned out to need correcting once a REAL capture
existed (2026-08-18, captures/20260818_122351/0020_mission_index.json and
0022_mission_receive.json, real server, real account): dump.cs's UserMission
class (3 fields) was evidently reflecting an OLDER client build than the
one actually connecting -- the real wire entries carry MORE fields, and
the two response arrays don't even share one shape:
  mission_list (mission/index, honor/index): {mission_id, exec_count,
    mission_status, mission_type, event_id} -- the extra two come straight
    off the mission_data row (mission_type, event_id columns), no lookup.
  updated_mission_array (mission/receive): {mission_id, exec_count,
    mission_status, condition_type} -- a DIFFERENT extra field, not the
    same two. Getting this wrong would matter: MessagePack C# formatters
    are array-positional (field COUNT and order, not names), so shipping
    the dump.cs-only 3-field version wasn't just missing information, it
    was a wire-format mismatch the client's deserializer would choke on.
MissionState still matches GameDefine.MissionState (0 NotClear, 1 Clear,
2 GotReward); MissionReceiveRequest = {mission_id_array}, unchanged.

The UserMission SHAPE is captured, though -- honor/index really does carry
a live mission_list (captures/20260816_202505/0052_honor_index.json: 215
entries, e.g. {mission_id: 600301, exec_count: 33, mission_status: 0}),
which is what confirmed the field names above and is why honor/index
(user_profile.py's handle_honor_index) now calls build_mission_list()
here instead of hardcoding [].

master.mdb's mission_data is a big table (3279 rows) with 137 distinct
condition_type codes; the game's own MasterMissionData.ConditionType enum
only names FOUR of them (SingleModeRaceWin=100003, SingleModeRaceArrival=
100025, Champions=200012, FanNum=600013). The other 133 have no name in
any compiled class, BUT master.mdb's text_data table (category 67) turns
out to carry the real, human-readable mission description for every
mission_data.id (join on text_data."index" = mission_data.id) -- e.g. id
100000's text is literally "Win a race in Career", confirming condition_
type 100003. That single join decoded the real intent of every condition_
type in the table (see WORKLOG.md-style session notes: 2026-08-18), which
is FAR more than what's evaluated below -- _exec_count_for only covers the
subset that's ALSO (a) unambiguous from the text + condition_value columns
and (b) computable from state this server already maintains truthfully,
with NO new event-hook needed (so there's no risk of quietly drifting from
what really happened). Rather than guess and fabricate progress for
condition types this session didn't nail down precisely (this project's
standing rule -- see team_stadium.py's "honestly NOT simulated", single_
mode_team.py's INFERRED/NOT-captured notes, etc.), everything NOT listed
here is deliberately left at exec_count 0:

  FanNum (600013): condition_value_1 is a chara_id (0 = account-wide).
    Sum of collection.py's chara_list[].fan.

  SingleModeRaceWin (100003): condition_value_1 is a card_id (0 = no
    restriction). Sum of _genuine_roster's trained_chara.wins, optionally
    filtered to that card_id -- `wins` is the real graduated-career win
    count trained_chara already writes on career completion, not invented
    for this feature.

  CompleteCareerPlayCount (100004), cv1 0/1 ONLY: text_data revealed this
    code is really two families sharing one id -- cv1 0/1 = "Complete N
    Career playthroughs" (plain count, len(_genuine_roster)); cv1 3-17 =
    "Achieve Career rank X or higher" (a per-run minimum-rank gate against
    an F/F+/E/.../SS letter scale this session found no verified id
    mapping for -- left at 0, not guessed).

  AddTrainees (400001): len(_genuine_roster) -- same "genuine" definition
    as SingleModeRaceWin (excludes house-roster padding / cheat injects).

  SupportCardKindNum (400002): len(support_card_collection) -- distinct
    owned support cards.

  SupportCardLimitBreakCount (300002): sum of support_card_collection's
    own limit_break_count -- cumulative uncaps performed, not invented.

  UnlockStar (300003) / PotentialLevelUpCount (300004): the SAME stat
    under two condition_types (collection.py: "talent_level = potential",
    and this game calls raising it "unlocking a star" interchangeably) --
    sum of (talent_level - 1) across card_collection (starts at 1 on
    acquisition per shop.py/presents.py's own card grant).

  TeamRank (300006): condition_value_1 IS team_stadium_rank.id (verified:
    ids 6/14/18/24/30 have team_min_value 27500/100000/160000/220000/265000,
    matching "Reach Team Rank D/B/A/S/SS" exactly) -- compares team_stadium.
    py's real best_point against that row's team_min_value. condition_num is
    always 1 for this family, so exec_count is a plain 0/1.

  BondLevel, one chara (600014): condition_value_1 is a chara_id; the
    chara's current love_rank level (see _love_rank_level) from their real
    chara_list love_point -- love_rank is small (13 rows) master data
    mapping raw bond points to a 0-12 level, same table stories.py's own
    docstring already referenced for this exact purpose.

  BondLevel, any (600020): sum of EVERY owned character's current love_rank
    level -- since love_point only ever increases (training, never spent),
    that sum IS the real cumulative count of level-up events, no separate
    counter needed.

  FanThresholdCount (600021): count of chara_list entries whose fan has
    reached condition_value_1 (always 1,000,000 in every real row, but read
    live rather than hardcoded).

  G1Wins (100007): sum of _genuine_roster's win_saddle_id_array lengths --
    trained_chara.py already records G1 (grade 100) wins per finished run
    at career-finish time, so this reuses that real record rather than
    re-deriving grade from race_result_list here.

  CareerWinThreshold (100012): condition_value_1 is a per-RUN win
    threshold (5/10/15/.../ condition_num always 1) -- count of
    _genuine_roster entries whose own `wins` meets it, compared against
    the generic count>=condition_num check like every other family here.

  CareerFanThreshold (100028), cv1==0 OR a chara_id: another two-family
    code sharing one id (see CompleteCareerPlayCount above for the same
    pattern) -- cv1==0 is a plain per-run "at least condition_value_2 fans"
    gate; cv1==<chara_id> is the same gate restricted to runs on that
    specific trainee (card_data.chara_id, resolved from _genuine_roster's
    card_id). Per-run, not a lifetime sum, same as CareerWinThreshold.

  UnlockStarCount (300005): condition_value_1 is always 5 (star level);
    condition_num is how many DIFFERENT trainees need it -- count of
    card_collection entries at that talent_level, unlike UnlockStar/
    PotentialLevel above which sum how many times ANY card was raised.

  ViewCharaStory (500001) / ViewMainStory (500002): "have you read episode
    X" gates. cv1 for the chara-story family is chara_story_data.story_id
    (an 8-digit id, e.g. 41001004) -- NOT the row's own PK, which is what
    stories.py's chara_cleared list actually stores, so it's resolved via
    one extra lookup. The main-story family's cv1 IS main_story_data.id
    directly (verified against real rows). Both check membership in
    stories._story_state's real chara_cleared/main_cleared lists.

  LoginDaysTotal (600001) / LoginToday (600022): login_bonus.py's new
    total_login_days()/logged_in_today() -- a plain distinct-day counter
    this project never tracked before (login_bonus_state is per-CAMPAIGN,
    not a global day count), recorded once per real load/index call using
    the same daily_races._served_day() reset clock the campaigns use.

  SkillCountThreshold (100041) / SpecificSkillAcquired (100038): per-run
    (100041, cv2 is the count) / lifetime-ever (100038, cv2 is the exact
    skill_id) checks against _genuine_roster's own real skill_array --
    single_mode_team.py copies the finished run's ACTUAL learned skills
    there verbatim (unique skill included), not a fabricated set.

  EpithetAndCareerComplete (100037): cv1 is the honor_id; owning it (user_
    profile._honor_state) AND having at least one genuine finished run.

  G1TrophyCount (100005): the BEST single genuine run's own distinct G1-win
    count (win_saddle_id_array, the same real per-run record G1Wins/100007
    already reuses) -- "in Career" (singular run), not a lifetime sum.

  RaiseSkillHint (300007): existence check against card_collection's own
    skill_data_array hint levels -- the SAME persistent state idle_single_
    mode.py's _persist_card_hints and any real card/skill_upgrade purchase
    already write to.

  ViewDistinctStories (500003): stories._story_state's chara_cleared +
    main_cleared + story_event_cleared, summed -- every real cleared-story
    container this project tracks, combined.

  BondLevelThresholdCount (600038): cv1 is the bond LEVEL (always 10);
    condition_num is how many DIFFERENT characters need to reach it --
    count of chara_list entries whose _love_rank_level (the same real
    conversion BondLevel(chara)/BondLevel(any) above use) meets cv1.

  One-off "did you ever do X" flags -- ChangeTitle (600009), ChangeHome
    Companions (600010), ChangeProfileChara (600011), LinkAppData (600012),
    EditTrainerCard (600031), JukeboxRequest (600030), PracticeRaceParticipate
    (200028): no natural counter exists for any of these, so the real
    handler that performs the action (honor/change_honor, user/change_
    favorite_character, account/publish_transition_code, user/set_profile_
    card_info, jukebox/play_user_request, practice_race/race_start) calls
    mark_achieved() with a flag name (see ACHIEVEMENT_FLAG_KEY) the first
    time it genuinely happens. ChangeHomeCompanions and ChangeProfileChara
    share ONE flag because they're both set by the SAME real endpoint
    (position1 = profile chara, positions 2-4 = home companions, one call).
    LinkAppData is credited on the PUBLISHING side (account/publish_
    transition_code), not the receiving/chaining side, which re-
    authenticates as a different account and abandons the calling one --
    marking the flag there would credit an account about to be discarded.

Every other condition_type is still LISTED (active-date filtering is real
and correct regardless of whether we can evaluate progress for it, so the
tab isn't permanently empty and every mission's reward stays inspectable)
but sits at exec_count 0 / mission_status NotClear until a future session
pins down what it measures -- same honest posture as everywhere else
un-simulated in this codebase, not a bug. What's left, by why it's left
(updated 2026-08-19 after the daily-reset / claw-machine / concert /
race-set / letter-grade pass below -- see each condition_type's own branch
comment in _exec_count_for for exactly what's evaluated):

  DAILY-RESET MISSIONS ("Daily: ..." text, mission_type==5, 378 rows) now
  have a real claim/reset model (_daily_mission_state/_daily_exec_count
  above) -- a SEPARATE per-day claim set plus a baseline exec_count
  snapshot taken the first time each mission is seen that served day,
  diffed against the live (lifetime, monotonic) counter on every later
  check. Works for ANY condition_type this module can already evaluate,
  with no new per-action event hooks -- a daily mission just reopens the
  next served day instead of staying GotReward forever. Daily rows whose
  own condition_type is still unevaluated (below) are still stuck at 0,
  same as their lifetime counterparts.

  CLAW MACHINE (100022 play count / 100051 lifetime plushies / 100023
  per-session-or-per-career plushie threshold) and GRAND/OUR GRAND CONCERT
  scoring (100070 all-Great-Success / 100074 success-count family / 100075
  songs-before-first-concert / 100077 specific song obtained / 100079 song-
  count threshold / 600006 watch a specific song / 600033 watch N unique
  songs) are now ALL wired -- single_mode_team.py's CRANE_LIFETIME_KEY/
  CRANE_SESSION_KEY and Grand Live's lives_done (now also carrying
  song_ids -- the performed setlist -- and songs_owned -- the running
  total at that Live) cover every one of these condition_types' real
  mechanics.

  TEAM TRIALS / LEGEND RACE condition_types (200005, 200012, 200019, 200024,
  200031, 200033, 200004, 200006, 200007, 200013, 200014, 200017, 200020)
  all require systems this project has explicitly decided not to build
  (user's own words, 2026-08-18: "we will probably not be able to do much
  about this, we need more players and the logic is weird, lets not
  implement it for now if probably ever" -- team_stadium.py's competitive
  racing carries the same reasoning). Left at 0, not guessed at.

  MULTIPLAYER/SOCIAL condition_types. This whole family used to be left at 0
  for want of other real players; the friend graph (social.py) and Clubs
  (circles.py) now exist and are serverwide, so the half of it that is about
  what YOU do is WIRED (2026-09-09 pass):

    600002 "Follow N trainers"  -> social.follow_num, live
    600003 "Get N followers"    -> social.follower_num, live (the house
                                   lenders follow the player back, so this is
                                   reachable without a second human account)
    600004 "Join a Club"        -> latched the first time the viewer is in one
    600024 "Share a Veteran Umamusume in your Club's chat"
                                -> flagged by circle_chat/post_partner
    600026 "Check on your Club" -> flagged by circle/room_enter

  What is STILL left at 0 in this family is only what genuinely requires
  OTHER PEOPLE to act on you, which no amount of server code can honestly
  fabricate: 600018 ("have your Star Umamusume borrowed 100 times by other
  trainers") and 600025 ("have your Veteran Umamusume be invited to N
  practice races"). Both count other players' choices. They become measurable
  the day this server has a real second population -- the lending side
  (house_lenders.py) and the partner-share side (circle_chat/post_partner)
  are already built, so each needs a counter on the RECEIVING end and nothing
  more. FUTURE.

  UNIMPLEMENTED MINIGAMES/SYSTEMS, no server-side backing at all -- still
  FUTURE, unchanged: Trainer exams (700001, "Achieve a C/B/A/... rating on N
  tests" -- a whole new minigame, not just a missing counter) and the Daily
  Sale shop's purchase count (600037 -- no Daily Sale shop tab exists yet
  either). Left at 0, not faked.

  RACE-SET / RANK families (2026-08-19 pass, using real master.mdb data --
  see each branch's own comment): CompleteCareerPlayCount's (100004) cv1
  3-17 "Achieve Career rank X or higher" half turned out to be directly
  resolvable -- text_data spells out cv1=letter for THIS condition_type
  (3=F...17=SS), matching single_mode_rank.id's own numbering 1:1, and
  trained_chara.py already stores each run's real `rank` id -- now
  evaluated as a per-run "rank id >= cv1" gate. Classic Triple Crown
  (100010, cv1==1 ONLY -- user-confirmed 2026-08-19: Satsuki Sho + Tokyo
  Yushun/Japanese Derby + Kikuka Sho), Big Eight (100020 -- the historical
  八大競走: Oka Sho/Satsuki Sho/Tenno Sho Spring/Japanese Oaks/Tokyo Yushun/
  Kikuka Sho/Tenno Sho Autumn/Arima Kinen), URA Finale win (100019, cv1=
  10003 -- single_mode_race_group's Finale round-3 program set) and
  AllTrophiesForChara (100056, cv1=chara_id -- lifetime union of grade<=300
  wins across every run of that character, vs. race_trophy's real 290-
  entry canonical set) are evaluated using each run's real _won_program_ids
  (trained_chara.py, any grade -- not just win_saddle_id_array's G1-only
  subset). 100010's other EIGHT cv1 families (3 Senior Autumn Triple Crown/
  4 Triple Tiara/5 Senior Spring Triple Crown/7 Twin Tenno Sho/8 Dual Grand
  Prix/10 Dual Miles/11 Dual Sprints/12 Dual Dirts) are deliberately still
  left at 0: real, well-known race-set names too, but this session only had a
  user-verified set for Classic Triple Crown/Big Eight -- not worth guessing
  the rest. FUTURE, and cheap: each needs only its race list confirmed.

  100033 ("Win the Unity Cup") IS now evaluated. That note used to read
  "a whole separate scenario this project isn't building" -- it has since been
  built (app/scenarios/unity_cup), so the mission counts finished runs whose
  FINAL team race was won, read off the scenario snapshot frozen into each
  veteran record at graduation (unity_cup_won).

story_event_mission_list is UNCHANGED (still []): its rows key off
story_event_id, whose own active window lives in a master table this
session never inspected, and the real capture only ever showed a single
entry for it -- not worth guessing at half the picture. Flagged as a
known follow-up, not fabricated.

Claim state per viewer (MISSION_STATE_KEY = "mission_state"):
{"claimed": [mission_id, ...]}. That's the ONLY thing persisted --
exec_count/mission_status are recomputed live from real state on every
load, so they can never drift out of sync with what actually happened.
mission_status is GotReward iff the id is in claimed; else Clear iff the
live exec_count has reached condition_num; else NotClear. Receiving
grants EXACTLY ONCE per id (a repeat receive of an already-claimed id
succeeds with nothing added, matching every other first-clear family on
this server -- stories.py, cards.py, ...) and refuses (205) for any id
that is unknown or not actually at Clear.
"""

from __future__ import annotations

import logging

from .. import master_data
from .. import patch
from .. import state as state_store
from . import bond, collection, login_bonus, registry, shop, stories, trained_chara
from .stories import _grant_reward

log = logging.getLogger("uma-server")

MISSION_STATE_KEY = "mission_state"
ACHIEVEMENT_FLAG_KEY = "mission_achievement_flags"  # set of one-off action
                                                    # names (strings) for the
                                                    # "did you ever do X"
                                                    # missions below that
                                                    # have no other natural
                                                    # counter to read from
                                                    # (title change, profile
                                                    # edits, jukebox request,
                                                    # ...). Each real handler
                                                    # that performs the
                                                    # action calls
                                                    # mark_achieved() once.


def mark_achieved(full_state: dict, flag: str) -> None:
    """Record a one-off achievement flag. Caller owns the save -- this only
    mutates full_state in place, matching every other in-place helper this
    project's handlers call mid-request (e.g. shop._add_item).

    IDEMPOTENT: the store is a set of names semantically (every reader asks
    `flag in flags`), so re-marking is a no-op instead of appending a duplicate.
    That matters now that a flag can be set from an endpoint the player hits
    repeatedly -- circle/room_enter fires on every visit to the Club screen, and
    an append-always version grew the list without bound."""
    flags = full_state.setdefault(ACHIEVEMENT_FLAG_KEY, [])
    if flag not in flags:
        flags.append(flag)


_COND_SINGLE_MODE_RACE_WIN = 100003
_COND_COMPLETE_CAREER = 100004        # cv1<=2 only -- see _exec_count_for
_COND_G1_WINS = 100007
_COND_CAREER_WIN_THRESHOLD = 100012
_COND_CAREER_FAN_THRESHOLD = 100028   # cv1==0 plain / cv1==chara_id filtered -- see below
_COND_SUPPORT_CARD_LIMIT_BREAK = 300002
_COND_UNLOCK_STAR = 300003
_COND_POTENTIAL_LEVEL = 300004
_COND_UNLOCK_STAR_COUNT = 300005
_COND_TEAM_RANK = 300006
_COND_ADD_TRAINEES = 400001
_COND_SUPPORT_CARD_KIND_NUM = 400002
_COND_VIEW_CHARA_STORY = 500001
_COND_VIEW_MAIN_STORY = 500002
_COND_FAN_NUM = 600013
_COND_BOND_LEVEL_CHARA = 600014
_COND_BOND_LEVEL_ANY = 600020
_COND_FAN_THRESHOLD_COUNT = 600021
_COND_LOGIN_DAYS_TOTAL = 600001
_COND_LOGIN_TODAY = 600022
_COND_SKILL_COUNT_THRESHOLD = 100041
_COND_SPECIFIC_SKILL_ACQUIRED = 100038
_COND_EPITHET_AND_CAREER_COMPLETE = 100037
_COND_G1_TROPHY_COUNT = 100005
_COND_RAISE_SKILL_HINT = 300007
_COND_VIEW_DISTINCT_STORIES = 500003
_COND_BOND_LEVEL_THRESHOLD_COUNT = 600038
_COND_CHANGE_TITLE = 600009
_COND_CHANGE_HOME_COMPANIONS = 600010
_COND_CHANGE_PROFILE_CHARA = 600011
_COND_LINK_APP_DATA = 600012
_COND_EDIT_TRAINER_CARD = 600031
_COND_JUKEBOX_REQUEST = 600030
_COND_PRACTICE_RACE_PARTICIPATE = 200028
_COND_CLAW_MACHINE_PLAY_COUNT = 100022
_COND_CLAW_MACHINE_PLUSHIE_COUNT = 100051
_COND_CONCERT_SUCCESS_COUNT = 100074   # multi-family via cv1/cv2 -- see branch
_COND_CONCERT_ALL_GREAT = 100070
_COND_SPECIFIC_SONG_OBTAINED = 100077
_COND_SONG_COUNT_THRESHOLD = 100079
_COND_CLAW_MACHINE_PLUSHIE_SESSION = 100023
_COND_SONGS_BEFORE_FIRST_CONCERT = 100075
_COND_WATCH_SPECIFIC_SONG = 600006
_COND_WATCH_UNIQUE_CONCERTS = 600033
_COND_CLASSIC_TRIPLE_CROWN = 100010    # cv1==1 ONLY -- see branch
_COND_BIG_EIGHT = 100020
_COND_URA_FINALE_WIN = 100019
_COND_ALL_TROPHIES_FOR_CHARA = 100056
# Newly measurable now that the systems behind them exist on this server (the
# friend graph + Clubs in social.py/circles.py, and the Unity Cup scenario).
# Every one of these was deliberately left at 0 before -- see the docstring.
_COND_FOLLOW_COUNT = 600002        # "Follow N trainers"
_COND_FOLLOWER_COUNT = 600003      # "Get N followers"
_COND_JOIN_CIRCLE = 600004         # "Join a Club"
_COND_CIRCLE_SHARE_PARTNER = 600024  # "Share a Veteran Umamusume in your Club's chat"
_COND_CIRCLE_CHECK = 600026        # "Check on your Club"
_COND_WIN_UNITY_CUP = 100033       # "Win the Unity Cup"

# Achievement-flag names used by the branches above -- also the exact
# strings the real handlers in user_profile.py/account_link.py/
# jukebox_requests.py/practice_race.py call mark_achieved() with.
FLAG_TITLE_CHANGED = "title_changed"
FLAG_HOME_COMPANIONS_CHANGED = "home_companions_changed"
FLAG_TRAINER_CARD_EDITED = "trainer_card_edited"
FLAG_APP_DATA_LINKED = "app_data_linked"
FLAG_JUKEBOX_REQUESTED = "jukebox_song_requested"
FLAG_PRACTICE_RACE_RUN = "practice_race_participated"
FLAG_CIRCLE_JOINED = "circle_joined"
FLAG_CIRCLE_CHECKED = "circle_checked"
FLAG_CIRCLE_PARTNER_SHARED = "circle_partner_shared"
_G1_GRADE = 100                        # race.grade -- same constant trained_chara.py's
                                       # win_saddle_id_array filter uses

_STATUS_NOT_CLEAR = 0
_STATUS_CLEAR = 1
_STATUS_GOT_REWARD = 2


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _mission_claimed_set(full_state: dict) -> set:
    st = full_state.setdefault(MISSION_STATE_KEY, {})
    return set(st.setdefault("claimed", []))


def _save_claimed(full_state: dict, claimed: set) -> None:
    full_state.setdefault(MISSION_STATE_KEY, {})["claimed"] = sorted(claimed)


# ---------------------------------------------------------- daily missions --
# mission_type==5 is "Daily: ..." missions -- confirmed 2026-08-19 by
# checking EVERY row with that mission_type against text_data: all 378 of
# them start with "Daily:" (and no other mission_type does). Previously
# these went through the SAME lifetime-once `claimed` set as every other
# mission, so a daily mission claimed once stayed at GotReward FOREVER --
# never reopening the next real day, which is plainly wrong for something
# whose own name says "Daily". User-corrected 2026-08-19: "Make daily
# missions work".
_MISSION_TYPE_DAILY = 5


def _daily_mission_state(full_state: dict) -> dict:
    """{"day": served_day, "baseline": {str(mission_id): lifetime exec_count
    captured the first time this mission was evaluated THIS served day},
    "claimed": [mission_id, ...] claimed TODAY}. Rolled fresh (baseline
    wiped, claimed cleared) whenever daily_races._served_day() advances --
    the same lazy on-next-access rollover pattern daily_races.py's own
    _daily_state already uses (there's no day-boundary EVENT to hook, only
    the next real request after servertime crosses it, since client_config
    servertime is "now" -- genuinely live, not frozen)."""
    from . import daily_races
    today = daily_races._served_day()
    st = full_state.setdefault(MISSION_STATE_KEY, {})
    daily = st.setdefault("daily", {})
    if daily.get("day") != today:
        daily["day"] = today
        daily["baseline"] = {}
        daily["claimed"] = []
    return daily


def _daily_exec_count(viewer_id, full_state: dict, row, daily_state: dict) -> int:
    """Progress made SINCE TODAY STARTED, not a lifetime total -- reuses
    _exec_count_for's real (lifetime, monotonically-increasing) counter
    unchanged and diffs it against a baseline snapshot taken the first time
    this mission was seen today. Works for ANY condition_type without new
    per-action event hooks, AS LONG AS the underlying counter never
    decreases within a day (true for every condition_type this project
    actually computes -- wins, completions, login days, ... all only ever
    go up). Deliberately NOT built as new "today only" counters threaded
    through every scoring path in single_mode_team.py/daily_races.py/etc --
    that's a much larger, riskier change for the same real answer this
    diff already gives honestly."""
    mid_key = str(row["id"])
    lifetime = _exec_count_for(viewer_id, full_state, row)
    baseline = daily_state.setdefault("baseline", {})
    if mid_key not in baseline:
        baseline[mid_key] = lifetime
    return max(0, lifetime - baseline[mid_key])


def _parse_mdb_datetime(value) -> int | None:
    """master.mdb's 'YYYY/M/D H:MM:SS' (month/day/hour are NOT always
    zero-padded, e.g. '2025/9/22 14:59:59') -> epoch seconds. Blank/
    unparseable -> None, meaning 'no real bound' to the caller."""
    if not value:
        return None
    import datetime
    try:
        dt = datetime.datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.timestamp())


def _is_active(row, now_ts: int) -> bool:
    if not row["date_check_flg"]:
        return True
    start = _parse_mdb_datetime(row["start_date"])
    end = _parse_mdb_datetime(row["end_date"])
    if start and now_ts < start:
        return False
    if end and now_ts > end:
        return False
    return True


def _genuine_roster(viewer_id, full_state: dict | None = None) -> list:
    """trained_chara entries in [PLAYER_CAREER_ID_BASE, INJECT_ID_BASE) --
    careers YOU actually finished on this server, trained_chara.py's own
    definition of "genuine" (see its id-banding docstring and how it
    filters at line ~805). Deliberately excludes generated house-roster
    padding (HOUSE_ID_BASE+, random flavor stats) and manual cheat injects
    (INJECT_ID_BASE+) -- crediting those would count achievements nobody
    actually earned, exactly what this module's docstring says not to do.
    Shared by SingleModeRaceWin, CompleteCareerPlayCount and AddTrainees."""
    # FAST PATH: the caller already holds the viewer's state, and the roster is
    # a plain key in it -- so ask that dict rather than re-reading and
    # re-parsing the whole state from disk. This function is called from ~25
    # branches of _exec_count_for, which itself runs once per mission row, so
    # going to the store here meant ~400 full state loads (11,600 json.loads)
    # for a single mission/index or honor/index call -- about 1.9 s of the
    # ~2 s those endpoints took.
    #
    # Only trusted when the roster is already at the CURRENT version: a missing
    # or stale one has to go through trained_chara, which (re)seeds and
    # persists it, and that is the one case that legitimately needs a write.
    roster = None
    if full_state is not None and (
            full_state.get(trained_chara.ROSTER_VERSION_KEY)
            == trained_chara.ROSTER_VERSION):
        roster = full_state.get(trained_chara.ROSTER_KEY)
    if roster is None:
        roster = trained_chara._get_or_seed_roster(viewer_id) or []
    return [c for c in roster
           if trained_chara.PLAYER_CAREER_ID_BASE
              <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]


def _love_rank_level(love_point: int) -> int:
    """Current Bond Level for a raw love_point total (bond.py owns the
    love_rank ladder and the grants that move it). Monotonic: a character's
    love_point only ever goes up, so this level is exactly the count of
    level-up events that character has had -- no separate counter needed."""
    return bond.love_rank(love_point)


_program_race_cache: dict | None = None


def _program_race_row(program_id) -> dict | None:
    """program_id -> {"inst": race_instance_id, "race_id": race.id, "grade":
    race.grade} -- the SAME join trained_chara.py's own win_saddle_id_array
    computation uses, cached (single_mode_program is static master data,
    cheap to memoize once per process) since the race-SET mission families
    below each resolve a run's real _won_program_ids through this."""
    global _program_race_cache
    if _program_race_cache is None:
        rows = master_data.query(
            "SELECT p.id AS program_id, ri.id AS inst, rc.id AS race_id, rc.grade AS grade "
            "FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id = p.race_instance_id "
            "JOIN race rc ON rc.id = ri.race_id")
        _program_race_cache = {r["program_id"]: {"inst": r["inst"], "race_id": r["race_id"],
                                                  "grade": r["grade"]} for r in rows}
    return _program_race_cache.get(program_id)


_race_ids_by_name_cache: dict = {}


def _race_ids_by_name(name: str) -> frozenset:
    """race.id set for an exact race NAME. text_data category 33 is the base
    race-name table keyed directly by race.id (confirmed 2026-08-19: e.g.
    index 1006 -> 'Tokyo Yushun (Japanese Derby)', and index 1006's race row
    is grade=100/group=1, matching every real G1 checked). A name can have
    more than one id (course-revision variants across the game's life, e.g.
    'Satsuki Sho' -> {1005, 1028}) -- all count as wins of that race."""
    if name not in _race_ids_by_name_cache:
        rows = master_data.query(
            'SELECT "index" FROM text_data WHERE category=33 AND text=?', (name,))
        _race_ids_by_name_cache[name] = frozenset(r["index"] for r in rows)
    return _race_ids_by_name_cache[name]


def _won_race_ids(roster_entry) -> set:
    """A finished run's own real won-race set, in race.id space (see
    trained_chara.py's _won_program_ids -- any grade, not just G1)."""
    out = set()
    for pid in roster_entry.get("_won_program_ids") or ():
        row = _program_race_row(pid)
        if row:
            out.add(row["race_id"])
    return out


_trophy_instance_cache: frozenset | None = None


def _trophy_instance_ids() -> frozenset:
    """The canonical G1-G3 trophy shelf: race_trophy's 290 race_instance_ids
    (verified 2026-08-19: joining through to race.grade gives exactly 171 G1
    + 43 G2 + 76 G3, nothing else -- this table IS 'every G1 to G3 trophy',
    not a subset). Static master data, cached for the process lifetime."""
    global _trophy_instance_cache
    if _trophy_instance_cache is None:
        rows = master_data.query("SELECT race_instance_id FROM race_trophy")
        _trophy_instance_cache = frozenset(r["race_instance_id"] for r in rows)
    return _trophy_instance_cache


_finale_program_cache: frozenset | None = None


def _finale_program_ids() -> frozenset:
    """single_mode_program ids for the URA Finale's ROUND 3 (single_mode_
    race_group_id=10003 -- single_mode_team.py's own docstring on route
    resolution already identifies 10001/10002/10003 as the Finale's three
    rounds, 41 distance/ground variants each, one picked per career). 'Win
    the URA Finale' (100019, cv1=10003) means winning round 3 specifically."""
    global _finale_program_cache
    if _finale_program_cache is None:
        rows = master_data.query(
            "SELECT race_program_id FROM single_mode_race_group WHERE race_group_id=10003")
        _finale_program_cache = frozenset(r["race_program_id"] for r in rows)
    return _finale_program_cache


_chara_id_cache: dict = {}


def _chara_id_for_card(card_id) -> int | None:
    if card_id not in _chara_id_cache:
        row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        _chara_id_cache[card_id] = row["chara_id"] if row else None
    return _chara_id_cache[card_id]


def _exec_count_for(viewer_id, full_state: dict, row) -> int:
    """See module docstring for exactly what each condition_type means and
    why only these are evaluated. Needs viewer_id (not just full_state)
    because trained_chara's roster accessor seeds itself off the viewer,
    unlike collection.py's chara_list which is a plain state key."""
    ctype = row["condition_type"]
    cval1 = row["condition_value_1"] or 0

    if ctype == _COND_FAN_NUM:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        if cval1:
            return sum(c.get("fan") or 0 for c in charas if c.get("chara_id") == cval1)
        return sum(c.get("fan") or 0 for c in charas)

    if ctype == _COND_SINGLE_MODE_RACE_WIN:
        genuine = _genuine_roster(viewer_id, full_state)
        if cval1:
            return sum(c.get("wins") or 0 for c in genuine if c.get("card_id") == cval1)
        return sum(c.get("wins") or 0 for c in genuine)

    if ctype == _COND_COMPLETE_CAREER:
        # condition_type 100004 is really TWO families sharing one code
        # (confirmed via text_data category 67, the real mission descriptions):
        # cv1 0/1 = "Complete N Career playthroughs" (plain completion count);
        # cv1 3-17 = "Achieve Career rank X or higher in a Career playthrough"
        # -- a PER-RUN gate. Resolved 2026-08-19: text_data category 67's own
        # descriptions for THIS exact condition_type spell out cv1 3=F, 4=F+,
        # 5=E, 6=E+, 7=D, 8=D+, 9=C, 10=C+, 11=B, 12=B+, 13=A, 14=A+, 15=S,
        # 16=S+, 17=SS -- which is EXACTLY single_mode_rank's own id numbering
        # (id 1=G's 0-299 bucket, id 2=G+, id 3=F, ... -- letters climb in
        # lockstep with id), so cv1 IS a single_mode_rank.id directly, no
        # separate letter table needed. trained_chara.py already stores each
        # finished run's own `rank` (that same id, from _rank_for_score) at
        # career-finish time -- "achieved rank X or higher" is just "rank id
        # >= cv1" against that real per-run record, same per-run-gate shape
        # as CareerWinThreshold (100012) above. (cv1==1 is the "any completed
        # career" boundary the plain-count branch already covers identically:
        # G's own bucket starts at score 0, so every finished run already
        # clears it -- no double-counting risk between the two branches.)
        if cval1 <= 1:
            return len(_genuine_roster(viewer_id, full_state))
        return sum(1 for c in _genuine_roster(viewer_id, full_state) if (c.get("rank") or 0) >= cval1)

    if ctype == _COND_ADD_TRAINEES:
        return len(_genuine_roster(viewer_id, full_state))

    if ctype == _COND_G1_WINS:
        # cv1 is literally the G1 grade value (100) on every real row --
        # confirmed against trained_chara.py's OWN win_saddle_id_array,
        # which already filters race_result_list to grade==100 wins at
        # career-finish time (see build_trained_chara_from_career). Reuse
        # that real per-run record rather than re-deriving grade here.
        return sum(len(c.get("win_saddle_id_array") or []) for c in _genuine_roster(viewer_id, full_state))

    if ctype == _COND_CAREER_WIN_THRESHOLD:
        # "Complete a Career playthrough with at least cv1 wins" -- a per-
        # RUN gate (condition_num is always 1: cleared iff at least one
        # completed career meets it), not a lifetime sum.
        return sum(1 for c in _genuine_roster(viewer_id, full_state) if (c.get("wins") or 0) >= cval1)

    if ctype == _COND_CAREER_FAN_THRESHOLD:
        # condition_type 100028 is two families sharing one code (same
        # pattern as CompleteCareerPlayCount/100004 above): cv1==0 = plain
        # "at least cv2 fans in one run"; cv1==<a chara_id> = the same gate
        # but restricted to runs with that specific trainee (real rows use
        # values like 1009/1008/1007, which match card_data.chara_id, not
        # card_id -- card ids in this table run 100xxx+). Per-run gate like
        # CareerWinThreshold above, not a lifetime sum.
        threshold = row["condition_value_2"] or 0
        roster = _genuine_roster(viewer_id, full_state)
        if cval1:
            ids = {r["id"] for r in master_data.query(
                "SELECT id FROM card_data WHERE chara_id=?", (cval1,))}
            roster = [c for c in roster if c.get("card_id") in ids]
        return sum(1 for c in roster if (c.get("fans") or 0) >= threshold)

    if ctype == _COND_UNLOCK_STAR_COUNT:
        # "Unlock cv1-star for condition_num Trainees" (cv1 is always 5 in
        # every real row) -- COUNT of distinct owned cards at that star
        # level, vs. UnlockStar/PotentialLevel above which sum how many
        # times ANY card was raised. Same talent_level convention (starts
        # at 1 on acquisition, so star N == talent_level N).
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return sum(1 for c in cards if (c.get("talent_level") or 1) >= cval1)

    if ctype == _COND_VIEW_CHARA_STORY:
        # cv1 is chara_story_data.story_id (e.g. 41001004), NOT the row's
        # own id -- confirmed via master.mdb (chara_story_data.id=4 has
        # story_id=41001004 for chara 1001 episode 4). stories.py's
        # chara_cleared list stores the PK (episode_id), so resolve cv1 ->
        # PK first.
        st = stories._story_state(full_state)
        row2 = master_data.query_one(
            "SELECT id FROM chara_story_data WHERE story_id=?", (cval1,))
        if not row2:
            return 0
        return 1 if row2["id"] in (st.get("chara_cleared") or []) else 0

    if ctype == _COND_VIEW_MAIN_STORY:
        # cv1 IS main_story_data.id directly (confirmed: real rows 101/201/
        # 218/401 match real table PKs exactly) -- no lookup needed, unlike
        # the chara-story family above.
        st = stories._story_state(full_state)
        return 1 if cval1 in (st.get("main_cleared") or []) else 0

    if ctype == _COND_LOGIN_DAYS_TOTAL:
        return login_bonus.total_login_days(full_state)

    if ctype == _COND_LOGIN_TODAY:
        return 1 if login_bonus.logged_in_today(full_state) else 0

    if ctype == _COND_SKILL_COUNT_THRESHOLD:
        # "Complete a Career playthrough having acquired at least cv2
        # skills" -- per-RUN gate (condition_num always 1), cv1 always 0.
        # trained_chara_from_career already copies the finished run's real
        # skill_array verbatim (single_mode_team.py), so this is exactly
        # what the trainee actually learned, unique skill included.
        threshold = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(len(c.get("skill_array") or []) >= threshold for c in genuine) else 0

    if ctype == _COND_SPECIFIC_SKILL_ACQUIRED:
        # cv2 is the exact skill_id (e.g. 200331 "Professor of Curvature").
        # Any one genuine run having ever learned it clears this -- lifetime
        # OR, not "in the same run", since the mission only asks whether
        # you've ever completed a run with that skill, not repeatedly.
        target_skill = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(
            any((s.get("skill_id") == target_skill) for s in (c.get("skill_array") or []))
            for c in genuine
        ) else 0

    if ctype == _COND_EPITHET_AND_CAREER_COMPLETE:
        # cv1 is the honor_id the epithet grants. "Obtain the epithet X and
        # complete a Career playthrough" -- own it AND have finished at
        # least one genuine run (the epithet itself is what a real career
        # achievement grants, so simply owning it already implies a run
        # happened, but check both rather than assume).
        from . import user_profile
        honors = user_profile._honor_state(full_state, viewer_id).get("honors") or {}
        has_epithet = str(cval1) in honors
        return 1 if has_epithet and _genuine_roster(viewer_id, full_state) else 0

    if ctype == _COND_G1_TROPHY_COUNT:
        # "Obtain N different G1 trophies IN CAREER" (singular run) -- the
        # BEST single genuine run's own distinct G1 win count
        # (win_saddle_id_array, the same real per-run record G1Wins/100007
        # already reuses), not a lifetime sum across careers.
        genuine = _genuine_roster(viewer_id, full_state)
        return max((len(c.get("win_saddle_id_array") or []) for c in genuine), default=0)

    if ctype == _COND_RAISE_SKILL_HINT:
        # "Raise a trainee's skill hint level once" -- condition_num always
        # 1, a plain existence check against the SAME persistent per-card
        # hint levels idle_single_mode.py's _persist_card_hints (and any
        # real card/skill_upgrade purchase, cards.py) already writes.
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return 1 if any(
            (s.get("level") or 0) > 0
            for c in cards for s in (c.get("skill_data_array") or [])
        ) else 0

    if ctype == _COND_VIEW_DISTINCT_STORIES:
        st = stories._story_state(full_state)
        return (len(st.get("chara_cleared") or [])
                + len(st.get("main_cleared") or [])
                + len(st.get("story_event_cleared") or []))

    if ctype == _COND_BOND_LEVEL_THRESHOLD_COUNT:
        # cv1 is the bond LEVEL threshold (always 10 in every real row);
        # condition_num is how many DIFFERENT characters need to reach it.
        # Reuses the same _love_rank_level real conversion BondLevel(chara)
        # /BondLevel(any) above already use.
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        return sum(1 for c in charas
                  if _love_rank_level(c.get("love_point") or 0) >= cval1)

    # One-off "did you ever do X" flags -- see ACHIEVEMENT_FLAG_KEY's
    # comment. Each real handler that performs the action calls
    # mark_achieved() with the matching flag name below; condition_num is
    # always 1 in every real row for this whole family.
    flags = full_state.get(ACHIEVEMENT_FLAG_KEY) or []
    if ctype == _COND_CHANGE_TITLE:
        return 1 if FLAG_TITLE_CHANGED in flags else 0
    if ctype in (_COND_CHANGE_HOME_COMPANIONS, _COND_CHANGE_PROFILE_CHARA):
        # Both real missions are satisfied by the SAME endpoint (user/
        # change_favorite_character sets position1 -- the profile chara --
        # and positions 2-4 -- the home companions -- in one call), so both
        # condition_types check the one flag that call sets.
        return 1 if FLAG_HOME_COMPANIONS_CHANGED in flags else 0
    if ctype == _COND_LINK_APP_DATA:
        return 1 if FLAG_APP_DATA_LINKED in flags else 0
    if ctype == _COND_EDIT_TRAINER_CARD:
        return 1 if FLAG_TRAINER_CARD_EDITED in flags else 0
    if ctype == _COND_JUKEBOX_REQUEST:
        return 1 if FLAG_JUKEBOX_REQUESTED in flags else 0
    if ctype == _COND_PRACTICE_RACE_PARTICIPATE:
        return 1 if FLAG_PRACTICE_RACE_RUN in flags else 0
    # CLUB one-offs. Flags rather than live reads of the circle row, so that
    # LEAVING a Club cannot un-clear a mission that genuinely happened -- the
    # prose is "Join a Club" / "Check on your Club", both of which are events,
    # not states. circles.py sets each one where the action really occurs.
    if ctype == _COND_JOIN_CIRCLE:
        # LATCHED LAZILY rather than flagged at the join sites. There are four
        # ways into a Club (create one, an open join, accepting a scout, and
        # being approved after applying) and the last of those completes inside
        # the LEADER's request, where the applicant's own state is not loaded --
        # so a flag set per handler would miss exactly the most common path.
        # Reading "is in a Club" here catches all four, and latching it means
        # leaving later cannot un-clear a mission that genuinely happened.
        # mission/index saves full_state, which is what persists the latch.
        if FLAG_CIRCLE_JOINED in flags:
            return 1
        from .. import social
        try:
            if social.circle_of(viewer_id) is not None:
                mark_achieved(full_state, FLAG_CIRCLE_JOINED)
                return 1
        except Exception:                                      # noqa: BLE001
            log.exception("missions: circle lookup failed for viewer %s", viewer_id)
        return 0
    if ctype == _COND_CIRCLE_CHECK:
        return 1 if FLAG_CIRCLE_CHECKED in flags else 0
    if ctype == _COND_CIRCLE_SHARE_PARTNER:
        return 1 if FLAG_CIRCLE_PARTNER_SHARED in flags else 0

    # THE FRIEND GRAPH. Live counts, not flags: both are thresholds over a
    # number that moves in both directions (25/50 follows, 100/200 followers),
    # and social.py is serverwide, so these are real edges to real accounts --
    # including the house lenders, which follow the player back.
    if ctype in (_COND_FOLLOW_COUNT, _COND_FOLLOWER_COUNT):
        from .. import social
        try:
            return (social.follow_num(viewer_id) if ctype == _COND_FOLLOW_COUNT
                    else social.follower_num(viewer_id))
        except Exception:                                      # noqa: BLE001
            log.exception("missions: social count failed for viewer %s", viewer_id)
            return 0

    if ctype == _COND_WIN_UNITY_CUP:
        # "Win the Unity Cup" -- the fifth and final team race of a Unity Cup
        # run, won. Counted over every finished run, from the scenario snapshot
        # trained_chara.py freezes into the record at graduation (the live
        # scenario state is gone by the time a mission is ever checked).
        #
        # condition_value_2 is 3 on the one real row and is NOT read: cv2 is a
        # per-family selector in this table (condition_type 100074 uses 1/2/3
        # for concert kinds), not a scenario id, and nothing captured says what
        # 3 selects here. There is exactly one Unity Cup row and one Unity Cup,
        # so reading it would add a guess without adding a distinction.
        return sum(1 for c in _genuine_roster(viewer_id, full_state)
                   if ((c.get("_scenario_facts") or {}).get("unity_cup_won")))

    if ctype == _COND_CLAW_MACHINE_PLAY_COUNT:
        # single_mode_team.py's CRANE_LIFETIME_KEY -- NOT in _CAREER_STATE_
        # KEYS, so it survives career resets (the minigame itself is capped
        # at once per career, so this needs to be a lifetime counter to
        # ever reach the higher tiers -- 5/10/30 plays).
        from . import single_mode_team
        lifetime = full_state.get(single_mode_team.CRANE_LIFETIME_KEY) or {}
        return lifetime.get("play_count") or 0

    if ctype == _COND_CLAW_MACHINE_PLUSHIE_COUNT:
        from . import single_mode_team
        lifetime = full_state.get(single_mode_team.CRANE_LIFETIME_KEY) or {}
        return lifetime.get("plushie_count") or 0

    if ctype == _COND_CLAW_MACHINE_PLUSHIE_SESSION:
        # "Obtain cv1+ plushies from ONE claw machine session" (601304, cv1=
        # 10) / "...from the claw machines in a Career playthrough" (1000027,
        # cv1=3) -- text-distinct but numerically the SAME check: the
        # minigame is capped at one attempt per career (CRANE_PLAYED_KEY), so
        # a career's "session" total and its "in this Career" total are the
        # same single number, trained_chara.py's real per-run
        # _crane_session_plushies. Per-run gate, count of qualifying runs.
        genuine = _genuine_roster(viewer_id, full_state)
        return sum(1 for c in genuine if (c.get("_crane_session_plushies") or 0) >= cval1)

    if ctype == _COND_CONCERT_SUCCESS_COUNT:
        # 100074 is really THREE families sharing one code (confirmed via
        # text_data): cv1=1,cv2=2 = lifetime count of ANY completed concert
        # (grand_live impl's RESULT_NORMAL=1/RESULT_GREAT=2 -- there is no
        # "failure" state, every completed concert is at minimum a Success,
        # so this is just a lifetime count of lives_done entries across
        # every genuine Grand Live run); cv1=2,cv2=1 = lifetime count of
        # GREAT Success specifically (result_state==2); cv1=2,cv2=3 = a
        # "special" concert variant this session couldn't verify which
        # live_type means "special" for, left at 0 rather than guessed.
        genuine = _genuine_roster(viewer_id, full_state)
        all_lives = [l for c in genuine for l in (c.get("grand_live_lives_done") or [])]
        if cval1 == 1:
            return len(all_lives)
        if cval1 == 2 and row["condition_value_2"] == 1:
            return sum(1 for l in all_lives if l.get("result_state") == 2)
        return 0

    if ctype == _COND_CONCERT_ALL_GREAT:
        # Per-run gate: every concert THAT RUN performed was a Great
        # Success, and at least one was performed (an empty lives_done
        # trivially satisfies "all" otherwise, which isn't the real intent).
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            lives = c.get("grand_live_lives_done") or []
            if lives and all(l.get("result_state") == 2 for l in lives):
                return 1
        return 0

    if ctype == _COND_SPECIFIC_SONG_OBTAINED:
        # cv1 is the exact live_id. Lifetime-ever, same as SpecificSkill
        # Acquired (100038) above.
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(cval1 in (c.get("grand_live_songs") or []) for c in genuine) else 0

    if ctype == _COND_SONG_COUNT_THRESHOLD:
        # cv1 is the song-count threshold; per-run gate (condition_num
        # always 1), same shape as SkillCountThreshold (100041) above.
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(len(c.get("grand_live_songs") or []) >= cval1 for c in genuine) else 0

    if ctype == _COND_SONGS_BEFORE_FIRST_CONCERT:
        # "Obtain cv2 songs before the FIRST concert in Our Grand Concert"
        # (1001270, cv1=1, cv2=4) -- lives_done[0] (the run's first performed
        # Live) carries its own songs_owned snapshot (grand_live impl's
        # perform_live, taken before that Live's payout -- st['songs'] at
        # that point already includes everything _learn_song added up
        # through this segment, i.e. everything obtained before this Live).
        threshold = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            lives = c.get("grand_live_lives_done") or []
            if lives and (lives[0].get("songs_owned") or 0) >= threshold:
                return 1
        return 0

    if ctype == _COND_WATCH_SPECIFIC_SONG:
        # "Watch <song>" (cv1=live_id -- confirmed 2026-08-19: grand_live impl's
        # own MAKE_DEBUT_LIVE_ID=1006 matches mission 600603's cv1=1006
        # exactly, same for GIRLS_LEGEND_U_LIVE_ID=1029/mission 600417). Was
        # THIS song ever in a performed setlist (song_ids), not merely
        # learned -- distinct from SpecificSongObtained (100077)'s "owned".
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            for l in (c.get("grand_live_lives_done") or []):
                if cval1 in (l.get("song_ids") or ()):
                    return 1
        return 0

    if ctype == _COND_WATCH_UNIQUE_CONCERTS:
        # "Watch 10 unique Winning Concerts" (600690, cv1=0, condition_num=
        # 10) -- read as the distinct-song version of the check above: count
        # of DIFFERENT live_ids ever appearing in any performed setlist
        # across every genuine run, lifetime.
        genuine = _genuine_roster(viewer_id, full_state)
        seen = set()
        for c in genuine:
            for l in (c.get("grand_live_lives_done") or []):
                seen.update(l.get("song_ids") or ())
        return len(seen)

    if ctype == _COND_CLASSIC_TRIPLE_CROWN:
        # condition_type 100010 covers NINE different named race-set
        # achievements via cv1 (1/3/4/5/7/8/10/11/12 -- Classic Triple Crown/
        # Senior Autumn Triple Crown/Triple Tiara/Senior Spring Triple Crown/
        # Twin Tenno Sho/Dual Grand Prix/Dual Miles/Dual Sprints/Dual Dirts).
        # Only cv1==1 (Classic Triple Crown) is evaluated here -- user-
        # confirmed 2026-08-19 as Satsuki Sho + Tokyo Yushun (Japanese Derby)
        # + Kikuka Sho, the real-world three legs for 3-year-old colts. The
        # other eight are real, well-known race-set names too, but this
        # session only had a verified set for this one -- left at 0 rather
        # than guess the rest (this project's standing rule).
        if cval1 != 1:
            return 0
        targets = [_race_ids_by_name(n) for n in
                  ("Satsuki Sho", "Tokyo Yushun (Japanese Derby)", "Kikuka Sho")]
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            won = _won_race_ids(c)
            if all(won & t for t in targets):
                return 1
        return 0

    if ctype == _COND_BIG_EIGHT:
        # "Win all of the Big Eight races in Career" (600405, condition_num=
        # 8) -- the historical Japanese 'Big Eight' (八大競走): Oka Sho,
        # Satsuki Sho, Tenno Sho (Spring), Japanese Oaks, Tokyo Yushun
        # (Japanese Derby), Kikuka Sho, Tenno Sho (Autumn), Arima Kinen --
        # user-directed 2026-08-19 ("big eight, same thing" as Triple Crown's
        # verified-then-implemented treatment). Per-run: the BEST single
        # run's count of these 8 names won, capped naturally at 8 (matching
        # condition_num) since each target set can only contribute once.
        targets = [_race_ids_by_name(n) for n in (
            "Oka Sho", "Satsuki Sho", "Tenno Sho (Spring)", "Japanese Oaks",
            "Tokyo Yushun (Japanese Derby)", "Kikuka Sho", "Tenno Sho (Autumn)",
            "Arima Kinen")]
        genuine = _genuine_roster(viewer_id, full_state)
        best = 0
        for c in genuine:
            won = _won_race_ids(c)
            best = max(best, sum(1 for t in targets if won & t))
        return best

    if ctype == _COND_URA_FINALE_WIN:
        # "Win the URA Finale in a Career playthrough" (cv1=10003 -- the
        # Finale's ROUND 3 single_mode_race_group id; see _finale_program_ids).
        # Per-run: did this run's _won_program_ids include any of that
        # group's 41 distance/ground program variants.
        finale_ids = _finale_program_ids()
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            if finale_ids & set(c.get("_won_program_ids") or ()):
                return 1
        return 0

    if ctype == _COND_ALL_TROPHIES_FOR_CHARA:
        # "Obtain all G1 to G3 trophies with <character>" (cv1=chara_id,
        # condition_num=1) -- user-confirmed 2026-08-19 ("literally win all
        # G3-G1 races for a character"). Lifetime, ACCOUNT-WIDE union across
        # every genuine run trained as that character (trophies accumulate
        # across careers, unlike the per-run race-set gates above): does the
        # union of every such run's grade<=300 won race_instance_ids cover
        # ALL 290 of race_trophy's canonical entries.
        target = _trophy_instance_ids()
        if not target:
            return 0
        genuine = _genuine_roster(viewer_id, full_state)
        won_instances = set()
        for c in genuine:
            if _chara_id_for_card(c.get("card_id")) != cval1:
                continue
            for pid in c.get("_won_program_ids") or ():
                info = _program_race_row(pid)
                if info and info["grade"] in (100, 200, 300):
                    won_instances.add(info["inst"])
        return 1 if target <= won_instances else 0

    if ctype == _COND_SUPPORT_CARD_KIND_NUM:
        return len(full_state.get(collection.SUPPORT_CARD_KEY) or [])

    if ctype == _COND_SUPPORT_CARD_LIMIT_BREAK:
        cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
        return sum(c.get("limit_break_count") or 0 for c in cards)

    if ctype in (_COND_UNLOCK_STAR, _COND_POTENTIAL_LEVEL):
        # Same underlying stat under two mission_data condition_types (this
        # game's own terminology treats "unlock a star" and "raise Potential
        # Level" as synonyms) -- talent_level starts at 1 on acquisition
        # (shop.py/presents.py's card grant), so N-1 is the number of raises.
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return sum(max(0, (c.get("talent_level") or 1) - 1) for c in cards)

    if ctype == _COND_TEAM_RANK:
        # cv1 IS team_stadium_rank.id (verified: id 6/14/18/24/30 -> team_min_
        # value 27500/100000/160000/220000/265000, matching "Reach Team Rank
        # D/B/A/S/SS" 1:1). condition_num is always 1 for this family, so a
        # binary 0/1 exec_count is exactly right against the generic count >=
        # condition_num check below.
        from . import team_stadium
        rank_row = master_data.query_one(
            "SELECT team_min_value FROM team_stadium_rank WHERE id=?", (cval1,))
        if not rank_row:
            return 0
        ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
        best_point = ts_state.get("best_point") or 0
        return 1 if best_point >= rank_row["team_min_value"] else 0

    if ctype == _COND_BOND_LEVEL_CHARA:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        chara = next((c for c in charas if c.get("chara_id") == cval1), None)
        return _love_rank_level(chara.get("love_point") or 0) if chara else 0

    if ctype == _COND_BOND_LEVEL_ANY:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        return sum(_love_rank_level(c.get("love_point") or 0) for c in charas)

    if ctype == _COND_FAN_THRESHOLD_COUNT:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        threshold = cval1 or 1  # cv1 is always 1,000,000 in every real row, but read it live rather than hardcode
        return sum(1 for c in charas if (c.get("fan") or 0) >= threshold)

    return 0


def _mission_entry(viewer_id, full_state: dict, row, claimed: set, daily_state: dict) -> dict:
    """mission_type/event_id are REAL wire fields (confirmed 2026-08-18 live
    capture, captures/20260818_122351/0020_mission_index.json: entries carry
    {mission_id, exec_count, mission_status, mission_type, event_id}, not
    just the 3-field UserMission dump.cs's own (evidently outdated) class
    reflection showed) -- both come straight off the mission_data row, no
    extra lookup needed.

    Daily missions (mission_type==5) branch onto daily_state's per-day
    claimed set / baseline-diffed exec_count instead of the lifetime
    `claimed` set every other mission uses -- see _daily_mission_state."""
    mid = row["id"]
    is_daily = row["mission_type"] == _MISSION_TYPE_DAILY
    if is_daily:
        if mid in (daily_state.get("claimed") or []):
            return {"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                    "exec_count": row["condition_num"],
                    "mission_type": row["mission_type"], "event_id": row["event_id"]}
        count = _daily_exec_count(viewer_id, full_state, row, daily_state)
    else:
        if mid in claimed:
            return {"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                    "exec_count": row["condition_num"],
                    "mission_type": row["mission_type"], "event_id": row["event_id"]}
        count = _exec_count_for(viewer_id, full_state, row)
    status = _STATUS_CLEAR if count >= row["condition_num"] else _STATUS_NOT_CLEAR
    return {"mission_id": mid, "mission_status": status, "exec_count": count,
            "mission_type": row["mission_type"], "event_id": row["event_id"]}


def build_mission_list(viewer_id, full_state: dict) -> list:
    """Shared by mission/index and honor/index (user_profile.py) -- both
    show the same live, per-account mission_list; there's exactly one
    source of truth for it."""
    now_ts = patch._servertime()
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    rows = master_data.query("SELECT * FROM mission_data")
    return [_mission_entry(viewer_id, full_state, row, claimed, daily_state)
            for row in rows if _is_active(row, now_ts)]


# The CAREER mission board (single_mode_*/start and /load's mission_list) is a
# strict subset of the account's missions, not the whole list. Decoded by
# joining real entries back to mission_data: every one is mission_type 4 with
# condition_type 100003 (SingleModeRaceWin) or 100025 (SingleModeRaceArrival),
# i.e. exactly the race-result missions the in-career board can track. The
# other active type-4 rows (complete a playthrough with X, level up support
# cards, trigger friendship training, ...) live on the account mission tab and
# are absent from every real career capture.
#
# That filter alone is NOT enough, and this used to serve 43 entries where real
# serves 3. Measured over the three captures/bot/20260905_*_icarus sessions:
# 140 of 140 real career responses carry exactly {1001333, 1001334, 1001335},
# all event_id 197 -- the LIMITED-TIME campaign running on the capture date
# (window 2026/09/01 15:00 - 2026/09/08 14:59). The other 40 active rows that
# pass the condition filter belong to events 4 and 120, both of which end at
# the far-future sentinel 2050 -- evergreen sets that are on the account tab
# and never on the career board.
#
# So the board is "the current weekly campaign", and end_date is what separates
# the two: mission_type 4 splits cleanly into 184 sentinel-2050 rows and 571
# rows with a genuine campaign window (events 194-200 are consecutive 7-day
# windows). Filtering to a bounded window reproduces real's 3 entries exactly.
_CAREER_MISSION_TYPE = 4
_CAREER_MISSION_CONDITIONS = (100003, 100025)
# Rows that never expire use this year as their end_date; anything at or past
# it is evergreen, not a campaign.
_EVERGREEN_END_YEAR = 2049


def _is_campaign_row(row) -> bool:
    end = (row["end_date"] or "").strip()
    return bool(row["date_check_flg"]) and end[:4].isdigit() \
        and int(end[:4]) < _EVERGREEN_END_YEAR


def career_mission_list(viewer_id, full_state: dict) -> list:
    """mission_list as the in-career screens want it. Same entry shape and the
    same live progress/claim state as build_mission_list -- only the row filter
    differs."""
    now_ts = patch._servertime()
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    rows = master_data.query(
        "SELECT * FROM mission_data WHERE mission_type=? AND condition_type IN (%s)"
        % ",".join("?" * len(_CAREER_MISSION_CONDITIONS)),
        (_CAREER_MISSION_TYPE,) + _CAREER_MISSION_CONDITIONS)
    return [_mission_entry(viewer_id, full_state, row, claimed, daily_state)
            for row in rows if _is_campaign_row(row) and _is_active(row, now_ts)]


@registry.endpoint("mission/index")
def handle_mission_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    mission_list = build_mission_list(viewer_id, full_state)
    state_store.save_state(viewer_id, full_state)   # persists the seeded claim-state key
    return _ok({"mission_list": mission_list})


def claim_missions(viewer_id, full_state: dict, ids, summary: dict) -> list | None:
    """Claim a batch of missions into an IN-MEMORY full_state -- the caller
    persists, and supplies the reward_summary_info everything is granted into.

    Returns the updated_mission_array, or None if the batch must be refused
    (unknown id, or a mission whose condition is not actually met). Refusal is
    all-or-nothing, hence the None rather than a partial list.

    Factored out of mission/receive below so story_event/receive_mission can
    claim through exactly the same gate + grant path instead of a second,
    drifting copy of it -- an event mission and a normal mission are the same
    mission_data row either way.
    """
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    updated = []
    for mid in ids:
        row = master_data.query_one("SELECT * FROM mission_data WHERE id=?", (mid,))
        if row is None:
            return None
        is_daily = row["mission_type"] == _MISSION_TYPE_DAILY
        already = (mid in (daily_state.get("claimed") or [])) if is_daily else (mid in claimed)
        # updated_mission_array carries condition_type, NOT mission_type/
        # event_id -- a DIFFERENT wire shape than mission_list's entries
        # (confirmed 2026-08-18 live capture, .../0022_mission_receive.json:
        # {mission_id, exec_count, mission_status, condition_type}), so this
        # is deliberately not the same dict shape _mission_entry builds.
        if already:
            updated.append({"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                            "exec_count": row["condition_num"],
                            "condition_type": row["condition_type"]})
            continue
        count = (_daily_exec_count(viewer_id, full_state, row, daily_state) if is_daily
                else _exec_count_for(viewer_id, full_state, row))
        if count < row["condition_num"]:
            return None
        _grant_reward(full_state, row["item_category"], row["item_id"],
                      row["item_num"], summary, viewer_id)
        if is_daily:
            daily_state.setdefault("claimed", []).append(mid)
        else:
            claimed.add(mid)
        updated.append({"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                        "exec_count": row["condition_num"],
                        "condition_type": row["condition_type"]})

    _save_claimed(full_state, claimed)
    return updated


@registry.endpoint("mission/receive")
def handle_mission_receive(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    ids = payload.get("mission_id_array")
    if not isinstance(ids, list) or not ids:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    summary = shop._empty_summary()
    updated = claim_missions(viewer_id, full_state, ids, summary)
    if updated is None:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary, "updated_mission_array": updated})
