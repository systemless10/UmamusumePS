"""UNITY CUP (scenario 2) -- the mechanics: the team, the soul gauge, the ranks.

Unity Cup is the Aoharu Cup. The trainee's own career is URA's, unchanged --
same 78 turns, same goal races, same URA Finale (see docs/UNITY_CUP.md §0).
What this scenario adds is a SECOND roster running alongside the trainee: up to
19 teammates with their own stats and caps, a per-teammate "soul" gauge that
detonates twice per run, and five 5-round team races at fixed turns.

WIRE SHAPE
    team_data_set = {team_info, command_info_array, evaluation_info_array,
                     scenario_progress, frame_order_info_array, race_result_array,
                     final_win_type, opponent_info_array, event_effect_info,
                     not_up_team_parameter_info, team_race_history_array,
                     command_result}
    Only team_info / command_info_array / evaluation_info_array /
    scenario_progress / not_up_team_parameter_info / team_race_history_array are
    always present; the rest are None outside a team race.

WHAT IS GROUND TRUTH AND WHAT IS A KNOB
    Derived and verified against captures/bot/20260905_152744_icarus/ (one full
    78-turn run) plus master.mdb:
      * rank_score            = rating_formula.get_rating + get_skill_score
      * team_power            = max(the five stat ranks), SPlus if all S
      * team_rank             = a LADDER position, never computed
      * the soul state machine and both burst vector tables
      * the teammate type lookup (single_mode_scout_chara argmax)
    KNOBs, marked KNOB below -- shape observed, generating function not:
      * SOUL_FILL_CHANCE      how often the gauge ticks
      * teammate ordinary growth magnitudes
      * STAT_RANK_THRESHOLDS  fitted to the observed bands, not derived
"""

from __future__ import annotations

import copy
import functools
import logging
import random

from ... import master_data
from ... import rating_formula

log = logging.getLogger("uma-server")

SCENARIO_ID = 2
STATE_KEY = "unity_cup"

# The five training facilities, in the order the capture serves them.
TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)   # speed, stamina, power, guts, wiz
CAMP_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}

# command_id -> the stat that facility trains. Drives which burst vector a
# teammate's type maps onto and which stat their ordinary growth favours.
COMMAND_STAT = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz"}

STATS = ("speed", "stamina", "power", "wiz", "guts")

# home_info's params_inc_dec_info_array target_type encoding, reused verbatim
# in team_data_set.command_info_array.
TARGET_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}
TARGET_TYPE_SKILL_POINT = 30

# SingleModeScenarioTeamRaceDefine.InterestState
MEMBER_STATE_NONE = 0        # the 9xxx support NPCs -- not teammates
MEMBER_STATE_TEAM = 1        # TeamMember
MEMBER_STATE_SEMI = 2        # SemiMember (recruited, not yet joined)

# SingleModeScenarioTeamRaceDefine.SoulEventState
SOUL_NONE = 0
SOUL_EXPLODED = 1
SOUL_SP_EXPLODED = 2

SOUL_THRESHOLD_MAX = 5       # dump.cs AOHARU_SOUL_TRETHOLD_ID_MAX

# SingleModeScenarioTeamRaceDefine
MAX_ONE_RACE_MEMBER_COUNT = 3
TEAM_RACE_LAST_ROUND = 5
CALENDAR_ENABLE_TURN = 3
TUTORIAL_GUILD_OPEN_TURN = 4
# Bumped whenever the SHAPE of a rolled opponent offer changes. A career that
# rolled its offers under an older build has them cached in its own state, and
# the cache was keyed on the turn alone -- so a bad roll survived the fix that
# corrected it and kept crashing the client. Any cached roll whose stamp is not
# this one is discarded and re-rolled.
OPPONENT_ROLL_VERSION = 5      # 5: boss seats this build can actually draw
                               # 4: named opponents this build can actually draw
                               # 3: boss teams seated the way the capture seats them

# Bumped whenever the SHAPE of a simulated team-race block changes. The block
# is written at team_race_start and read back by the client, and a career that
# crashed mid-race resumes straight into it on load:
#
#     SingleModeChangeViewManager.ChangeViewTeamRaceRaceList
#       -> SingleModeScenarioTeamRaceRaceListViewController.RegisterDownload
#          -> RegisterDownloadRaceSkipCutin  =>  NullReferenceException
#
# So a block built by an older build outlives the fix that corrected it and
# re-crashes the client on every single load -- the career cannot be opened at
# all (user-reported 2026-09-05). Any pending block whose stamp is not this one
# is thrown away and the player is put back on opponent select to race again.
PENDING_BLOCK_VERSION = 2

BOSS_RANK = 100
BOSS_PLUS_RANK = 101
# dump.cs PLAYER_TEAM_MEMBER_DEFAULT_DRESS_ID. Every teammate races in it --
# capture-confirmed on all three of our runners in team race 1 round 2.
PLAYER_TEAM_MEMBER_DEFAULT_DRESS_ID = 101

# ------------------------------------------------ the team-race screen ---
# chara_info.playing_state THROUGH A TEAM RACE. This is the client's cue to
# open the race screen, exactly as Grand Live's 10 is its cue to open the
# backstage one, and Unity Cup simply never set it -- the "Before the Nth
# Round" beat played, the chain went empty as it should, and the turn then
# advanced with no race (user-reported twice, 2026-09-05).
#
# The whole lifecycle, straight off capture run 1 (files 0111-0119), and the
# same at all five races:
#
#     check_event resolving 201029  5 -> 7    the SELECT screen opens
#     team_race_start               7 -> 8    racing
#     team_race_end                 8 -> 9    the result sequence
#     team_race_out                 9 -> 5    back to the event chain
#
# SingleModeDefine.PlayingState (dump.cs 489612): 1 TurnStart, 5 TurnEnd,
# 7 TeamRaceTop, 8 TeamRacePlaying, 9 TeamRaceResult.
PLAYING_STATE_TURN_START = 1
PLAYING_STATE_EVENT_CHAIN = 5
PLAYING_STATE_TEAM_RACE = 7
PLAYING_STATE_TEAM_RACE_RUNNING = 8
PLAYING_STATE_TEAM_RACE_RESULT = 9

# ---------------------------------------------- the team-race pay-out ----
# What a finished team race pays, keyed on the RANK OF THE OPPONENT SET faced
# (single_mode_team_race_set.rank), as (trainee stat gain, trainee skill
# points). The trainee's stats are paid ONLY when the ladder position actually
# improves; the skill points are paid either way.
#
# Not master data -- there is no table for it; derived from 15 team races
# across the three runs in captures/bot/20260905_183334_icarus, which pin six
# of the ten tiers exactly:
#
#     set rank  3 -> +5  / 17 SP      4 -> +6  / 25 SP
#     set rank  6 -> +10 / 51 SP      8 -> +25 / 85 SP
#     set rank 100 -> +25 / 85 SP   101 -> +34 / 102 SP
#
# WHY KEYED ON THE OPPONENT TIER and not on the race number or the win count:
# run 1 lost its third race, and its fourth race then paid 10/51 -- the values
# of a rank-6 set, which is what it faced -- where the race number would have
# demanded 25/85. The same run's loss is what separates the stats from the
# skill points: it paid 51 SP (rank 6's) with the stat gain at exactly ZERO.
#
# DERIVED, NOT OBSERVED: tiers 1, 2, 5 and 7 appear in no capture (the ladder
# our runs climbed never offered them), so they are interpolated between the
# neighbours that were observed. They are reachable only by a team doing far
# worse or far better than any recorded run.
TEAM_RACE_REWARDS = {
    1:   (3,  10),      # derived
    2:   (4,  13),      # derived
    3:   (5,  17),
    4:   (6,  25),
    5:   (8,  38),      # derived
    6:   (10, 51),
    7:   (17, 68),      # derived
    8:   (25, 85),
    100: (25, 85),      # Boss
    101: (34, 102),     # Boss+
}

# What every TEAMMATE gains, all five stats, when the race is settled. Flat,
# and it turns on WINNING -- not on the ladder moving. 14 of the 15 captured
# races were won and paid 50; the one loss paid 10.
#
# Those are two different questions, and the fifth race is what separates them:
# a team already sitting at rank 2 wins its final race, the ladder cannot move
# (win_up_rank is 2 again), and it is still paid in full -- 50 to every
# teammate and +34/102 SP to the trainee. Reading the ladder instead of the
# result would have paid that race as a loss.
TEAMMATE_GAIN_WIN = 50
TEAMMATE_GAIN_NOT_WIN = 10

# SingleModeScenarioTeamRaceDefine.TeamEditFlag
TEAM_EDIT_INVALID = 0
TEAM_EDIT_ON = 1
TEAM_EDIT_OFF = 2

# dump.cs SINGLE_MODE_TEAM_RACE_LIVE_ID -- the Unity Cup song, granted once per
# account by team_race_end_out's add_music.
TEAM_RACE_LIVE_ID = 1035

# single_mode_aoharu_schedule: the five team-race turns.
TEAM_RACE_TURNS = (24, 36, 48, 60, 72)

# The 9xxx support NPCs that occupy evaluation_info_array rows in this scenario
# WITHOUT being teammates (member_state 0). From single_mode_unique_chara
# scenario 2 -- note 9002 (Otonashi) is absent here, unlike URA.
SUPPORT_NPC_ROWS = ((101, 9001), (103, 9003), (104, 9004), (106, 9006), (108, 9008))

# Deck positions. A teammate who came from the deck carries her POSITION as
# her target_id, so 1..6 are always legal evaluation rows even when a slot
# holds a pal card that seats no teammate.
DECK_SLOTS = 6


# ============================================================== master data ==

@functools.lru_cache(maxsize=1)
def scout_pool() -> dict:
    """single_mode_scout_chara keyed by its OWN row id: the recruitable
    teammate pool, with each entry's starting stats, aptitudes and caps.

    KEYED BY ROW, NOT BY CHARA, and that distinction is the whole soul-type
    mechanic. The table is one row per SUPPORT CARD (238 rows, 79 distinct
    charas), and the caps differ between a chara's cards -- Matikanefukukitaru
    is wiz_limit 750 as her R/SR (rows 44, 84) but speed_limit 750 as her SSR
    (row 178). So a teammate's burst type follows the card version that
    joined, not the character. Collapsing this to a chara_id dict silently
    keeps whichever row happened to be last and gets three of four observed
    types wrong."""
    rows = master_data.query(
        "SELECT id, support_card_id, chara_id, speed, stamina, pow, guts, wiz, "
        "proper_distance_short, proper_distance_mile, proper_distance_middle, "
        "proper_distance_long, proper_running_style_nige, proper_running_style_senko, "
        "proper_running_style_sashi, proper_running_style_oikomi, "
        "proper_ground_turf, proper_ground_dirt, "
        "speed_limit, stamina_limit, pow_limit, guts_limit, wiz_limit "
        "FROM single_mode_scout_chara ORDER BY id")
    return {r["id"]: dict(r) for r in rows}


@functools.lru_cache(maxsize=1)
def scout_rows_by_support_card() -> dict:
    """{support_card_id: scout_id}. THE join that builds the opening roster.

    238 of the game's 250 support cards have a scout row; the 12 without are
    the friend/group cards (Tazuna, Kiryuin, ...), which is exactly why those
    characters appear in evaluation_info_array as member_state 0 NPCs rather
    than as teammates."""
    return {row["support_card_id"]: scout_id
            for scout_id, row in scout_pool().items()}


@functools.lru_cache(maxsize=None)
def support_card_chara(card_id) -> int:
    """The character a support card belongs to. Used for deck slots that seat
    no teammate (pal/group cards), which still need a row of their own."""
    row = master_data.query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (int(card_id or 0),))
    return int(row["chara_id"]) if row else 0


@functools.lru_cache(maxsize=1)
def scout_rows_by_chara() -> dict:
    """{chara_id: (scout_id, ...)} in id order, so a caller that only knows a
    character (the special charas) can pick one of their cards."""
    out: dict = {}
    for scout_id, row in scout_pool().items():
        out.setdefault(row["chara_id"], []).append(scout_id)
    return {k: tuple(v) for k, v in out.items()}


@functools.lru_cache(maxsize=1)
def support_card_charas() -> dict:
    """{support_card_id: (chara_id, support_card_type)} for every card.

    support_card_type is what separates the three kinds, and the split lines up
    exactly with single_mode_scout_chara: 238 type-1 cards, all of which have a
    scout row, plus 10 type-2 PAL cards (Tazuna 9001, Kiryuin 9004, Riko 9006,
    Sasami 9005, Light Hello 9008) and 2 type-3 GROUP cards, none of which do.
    """
    rows = master_data.query(
        "SELECT id, chara_id, support_card_type FROM support_card_data")
    return {int(r["id"]): (int(r["chara_id"]), int(r["support_card_type"]))
            for r in rows}


@functools.lru_cache(maxsize=1)
def _pool_average() -> dict:
    """Mean starting stats and caps across single_mode_scout_chara.

    The baseline for a teammate who has no scout row of her own -- a pal or
    group card's character. DERIVED, not invented: there is no master row to
    read for these, and the pool mean is the one defensible stand-in. It is
    also close to what a real teammate joins at (the pool is tightly clustered:
    every row's caps are one 750, one 720 and three 700)."""
    cols = ("speed", "stamina", "pow", "guts", "wiz",
            "speed_limit", "stamina_limit", "pow_limit", "guts_limit", "wiz_limit")
    rows = list(scout_pool().values())
    if not rows:
        return {c: (100 if "limit" not in c else 700) for c in cols}
    return {c: int(round(sum(int(r[c] or 0) for r in rows) / len(rows))) for c in cols}


@functools.lru_cache(maxsize=1)
def special_charas() -> tuple:
    """single_mode_special_chara for this scenario -- the four named teammates
    who join on top of the random pool (Taiki Shuttle, Rice Shower, Haru Urara,
    Matikanefukukitaru). 9006 (Riko Kashimoto) is in the table too but is a
    support NPC, not a teammate, so anything outside the scout pool is dropped."""
    rows = master_data.query(
        "SELECT chara_id FROM single_mode_special_chara WHERE scenario_id=?",
        (SCENARIO_ID,))
    by_chara = scout_rows_by_chara()
    # Their LOWEST scout row -- the original R card. That is the version the
    # capture's four specials joined as (their observed burst types match those
    # rows' caps, not their SSR ones).
    return tuple(by_chara[r["chara_id"]][0] for r in rows
                 if r["chara_id"] in by_chara)


# ------------------------------------------------------------ team name --
# team_info.team_name_id indexes text_data category 195 DIRECTLY:
#
#     0  Name Pending      <- unnamed, which is what the player sees until the
#     1  Happy Hoppers        naming event resolves
#     2  Sunny Runners
#     3  Carrot Pudding
#     4  Blue Bloom
#     5  Team Carrot       <- the default, picked when no uma name is available
#
# 1-4 are the four scenario umas in single_mode_team_name id order (0 -> Taiki
# Shuttle, 1 -> Matikanefukukitaru, 2 -> Haru Urara, 3 -> Rice Shower), so the
# name id is that row's id + 1. That ordering is GameTora's own table, and the
# capture settles the rest: team_name_id runs 0 for 187 responses and then 5
# for the remaining 801, flipping exactly when "A Team at Last" resolves.
TEAM_NAME_NONE = 0
TEAM_NAME_DEFAULT = 5

# Winning the Unity Cup finals pays a GOLD HINT for the skill that belongs to
# the team's name -- level 3 for an uma name, level 1 for Team Carrot. Measured
# straight off the corpus: 95 runs finished with a rarity-2 level-3 tip in the
# Mile Maven group (every one of them named after Taiki Shuttle) and 20 with a
# rarity-2 level-1 tip in the No Stopping Me! group (every one Team Carrot).
TEAM_NAME_SKILL = {
    1: 200681,      # Happy Hoppers  -> Mile Maven
    2: 201121,      # Sunny Runners  -> Clairvoyance
    3: 200471,      # Carrot Pudding -> Indomitable
    4: 200741,      # Blue Bloom     -> Cooldown
    5: 200491,      # Team Carrot    -> No Stopping Me!
}
TEAM_NAME_SKILL_LEVEL = {5: 1}
TEAM_NAME_SKILL_LEVEL_DEFAULT = 3


@functools.lru_cache(maxsize=1)
def team_name_ids() -> tuple:
    """single_mode_team_name -- the team name is picked from these charas."""
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_team_name ORDER BY id"))


def count_burst(st: dict, extreme: bool) -> None:
    """Tally one Spirit Burst for the career. Both kinds count toward the
    total; only the purple ones count as Extreme."""
    st["bursts"] = int(st.get("bursts") or 0) + 1
    if extreme:
        st["extreme_bursts"] = int(st.get("extreme_bursts") or 0) + 1


def count_unity_training(st: dict, partners: int) -> None:
    """Tally one Unity Training -- a training the player took with at least one
    teammate standing on it. Epithets 152/153/155 are the only readers: a run
    total (153 wants 50, 155 wants 65) and the widest single one (152 wants 3
    teammates on one facility at once).

    Called from preview.py's award pass, which is the one place that knows how
    many teammates were actually ON the facility that was trained -- and it pays
    against the preview the client was shown, so this counts what the player
    saw rather than a re-roll."""
    if partners <= 0:
        return
    st["unity_trainings"] = int(st.get("unity_trainings") or 0) + 1
    st["unity_training_max_chars"] = max(
        int(st.get("unity_training_max_chars") or 0), int(partners))


# ------------------------------------------------------------ elite teams --
# THE FOURTH RACE CAN OFFER A FOURTH TEAM. text_data category 194 keys the
# opposing team's NAME by its single_mode_team_race_set id, and rank 8 (sets
# 803-814) is the only band whose twelve names are all Greek gods -- Kairos,
# Hermes, Persephone, Aeon, Hephaestus, Dionysus, Dysnomia, Hestia, Hymenaeus,
# Ares, Demeter, Tyche. Those are GameTora's 強豪チーム, the elite teams, and
# every one of them also carries a super_team_chara_id (the star uma the client
# draws) that no ordinary set has.
#
# So rank 8 is NOT the top of race 4's ordinary board -- it is an EXTRA offer
# appended to it, which is why the one capture that reached the top of the
# ladder was served FOUR teams where every other board has three (the
# "KNOWN GAP" endpoints._roll_opponents used to carry). The ordinary board for
# race 4 is 7/6/5.
ELITE_RANK = 8
ELITE_LEAGUE_RANK = 10       # ladder POSITION, so <= is "10th or better"
ELITE_TEAM_RANK = 7          # TeamParameterRank 7 = A


def elite_available(st: dict, race_index: int) -> bool:
    """GameTora, "S and S+ Team Ranks": the powerhouse team shows up on the
    fourth Unity Cup race if the league rank is 10 or higher, the team rank is
    A or higher, and at least one Extreme Spirit Burst has been triggered."""
    return (race_index == ELITE_RACE_INDEX
            and int(st.get("team_rank") or 30) <= ELITE_LEAGUE_RANK
            and team_power(ratcheted_ranks(st)) >= ELITE_TEAM_RANK
            and int(st.get("extreme_bursts") or 0) >= 1)


ELITE_RACE_INDEX = 3          # the fourth team race, turn 60


def elite_set_ids() -> tuple:
    return team_race_sets_of_rank(ELITE_RANK)


def beat_elite(st: dict) -> bool:
    return bool(st.get("beat_elite"))


def note_elite_result(st: dict, set_id: int, won: bool) -> None:
    """Remember a WIN over an elite team -- it is what unlocks the strengthened
    Team Zenith in the finals."""
    if won and int(set_id or 0) in elite_set_ids():
        st["beat_elite"] = True


# Team Zenith twice over: set 902 (rank 100) is the ordinary finals opponent,
# set 1000 (rank 101) the strengthened one you earn by beating an elite team.
# Both are named "Team Zenith" in category 194; the client tells them apart by
# the blue rather than red flames on the pre-race screen.
BOSS_RANK_STRENGTHENED = 101


def finals_rank(st: dict) -> int:
    return BOSS_RANK_STRENGTHENED if beat_elite(st) else BOSS_RANK


def trainee_is_scenario_chara(chara_info: dict) -> bool:
    """Whether the uma being TRAINED is one of the scenario's four story
    characters -- the "scenario-linked" test for rewards the trainee earns
    herself, as opposed to preview._is_scenario_linked which asks it of a
    TEAMMATE."""
    chara_id = int((chara_info or {}).get("card_id") or 0) // 100
    if not chara_id:
        chara_id = int((chara_info or {}).get("chara_id") or 0)
    pool = scout_pool()
    return chara_id in {int(pool.get(row, {}).get("chara_id") or 0)
                        for row in special_charas()}


@functools.lru_cache(maxsize=1)
def team_name_charas() -> tuple:
    """((team_name_id, chara_id), ...) for the four nameable umas, in the order
    the choice list offers them."""
    return tuple((int(r["id"]) + 1, int(r["chara_id"])) for r in master_data.query(
        "SELECT id, chara_id FROM single_mode_team_name ORDER BY id"))


def team_name_options(chara_info) -> list:
    """The uma team names this career may pick, in choice order.

    "The Character must be either your trained uma, or one of your support
    cards" (GameTora). Pals count -- the rule is about the CHARACTER, and a
    scenario uma who is only in the deck as a pal card is still in the deck."""
    if not isinstance(chara_info, dict):
        return []
    have = {trainee_chara(chara_info)}
    for card in chara_info.get("support_card_array") or ():
        chara = support_card_chara(card.get("support_card_id"))
        if chara:
            have.add(int(chara))
    return [name_id for name_id, chara in team_name_charas() if chara in have]


def team_named(st: dict) -> bool:
    return int(st.get("team_name_id") or TEAM_NAME_NONE) != TEAM_NAME_NONE


def name_team(st: dict, name_id) -> int:
    """Commit the team's name. Returns what was actually stored."""
    name_id = int(name_id or 0)
    if name_id not in TEAM_NAME_SKILL:
        name_id = TEAM_NAME_DEFAULT
    st["team_name_id"] = name_id
    log.info("unity cup: team named (team_name_id %s)", name_id)
    return name_id


def team_name_reward(st: dict) -> tuple:
    """(skill_id, hint_level) the finals pay for this team's name, or (0, 0).

    Only a WON Unity Cup finals pays: the corpus's 26 runs that reached the end
    without a rarity-2 team-name tip are exactly the ones served "Three Years of
    Hard Work!" instead of "A Present from Director Akikawa!"."""
    name_id = int(st.get("team_name_id") or 0)
    if not name_id or not won_final(st):
        return (0, 0)
    return (TEAM_NAME_SKILL.get(name_id, 0),
            TEAM_NAME_SKILL_LEVEL.get(name_id, TEAM_NAME_SKILL_LEVEL_DEFAULT))


def won_final(st: dict) -> bool:
    """Whether the FIFTH round of the Unity Cup was won."""
    for race in st.get("races") or ():
        if int(race.get("race_num") or 0) >= len(TEAM_RACE_TURNS):
            return int(race.get("result_state") or 0) == 1
    return False


@functools.lru_cache(maxsize=None)
def member_type(scout_id: int) -> str:
    """This teammate's soul TYPE: the stat their single_mode_scout_chara row
    caps highest.

    Every row in that table has exactly one 750 (primary) and one 720
    (secondary), the rest 700 -- so the argmax is unambiguous. Verified 4/4
    against observed bursts: Taiki Shuttle -> speed, Haru Urara -> guts,
    Matikanefukukitaru -> wiz, Rice Shower -> stamina, each on their lowest
    (R card) row. Takes a SCOUT ROW id, not a chara_id -- see scout_pool()."""
    if scout_id is None:
        # A pal or group card's character has no scout row at all. Her type
        # comes from her own caps instead -- see member_soul_type, which is
        # what every caller should be using.
        return "speed"
    row = scout_pool().get(int(scout_id))
    if not row:
        return "speed"
    limits = (("speed", row["speed_limit"]), ("stamina", row["stamina_limit"]),
              ("power", row["pow_limit"]), ("guts", row["guts_limit"]),
              ("wiz", row["wiz_limit"]))
    return max(limits, key=lambda kv: kv[1])[0]


def member_soul_type(member: dict) -> str:
    """This teammate's soul type, however she joined.

    A scouted or deck teammate has a single_mode_scout_chara row and the type
    is that row's highest cap. A PAL/GROUP card's character has no row, so it
    falls back to her own live caps -- which is the same question asked of the
    same numbers, just read off the member instead of the master table.

    Everything that used to call member_type(member["scout_id"]) must come
    through here: passing None straight into member_type raised TypeError and
    took the whole training payout down with it."""
    scout_id = member.get("scout_id")
    if scout_id is not None:
        return member_type(scout_id)
    limits = tuple((stat, int(member.get(stat + "_limit_base") or 0))
                   for stat in STATS)
    return max(limits, key=lambda kv: kv[1])[0] if limits else "speed"


@functools.lru_cache(maxsize=None)
def team_race_set(set_id: int) -> dict | None:
    row = master_data.query_one(
        "SELECT * FROM single_mode_team_race_set WHERE id=?", (int(set_id),))
    return dict(row) if row else None


@functools.lru_cache(maxsize=None)
def team_race_sets_of_rank(rank: int) -> tuple:
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_team_race_set WHERE rank=? ORDER BY id",
        (int(rank),)))


# ---------------------------------------------------------- opponent npcs --
# EVERY npc_id ON THE WIRE MUST BE A single_mode_npc ROW. The client resolves
# each SingleModeNpcTeamData through that master table to draw the portrait,
# and a fabricated id makes the lookup null:
#
#     NullReferenceException
#       PartsSingleModeScenarioTeamRaceOpponent+<>c.<SetupCharaImage>b__24_4
#         (SingleModeNpcTeamData data)
#       System.Linq.Enumerable.Count[TSource] (...)
#       PartsSingleModeScenarioTeamRaceOpponent.SetupCharaImage (...)
#       ... SingleModeScenarioTeamRaceOpponentSelectViewController.InitializeView
#
# (user-reported 2026-09-05: the opponent-select screen softlocked). We used to
# ship chara_id * 100, which is not an id in any master table.
#
# The corpus (32 distinct sets across 15 captured races) says an opposing team
# is drawn from exactly three id spaces:
#
#   MOBS      id < 1000, mob_id > 0 -- the faceless field. Observed 2..599.
#   NAMED     chara_id * 1000 + 100 -- a real trainee running for the other
#             team. Rank 8's themed teams use the +101/+102 dress variants.
#   BOSS      3000100..3014100 (rank 100) and 3000101..3014101 (rank 101), a
#             fixed fifteen. Never mixed with anything else.
MOB_NPC_MAX_ID = 1000
BOSS_NPC_IDS = tuple(range(3000100, 3015000, 100))


@functools.lru_cache(maxsize=None)
def mob_npc_pool() -> tuple:
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id < ? AND mob_id > 0 "
        "AND npc_group_id > 0 ORDER BY id", (MOB_NPC_MAX_ID,)))


@functools.lru_cache(maxsize=None)
def named_npc_pool(suffix: int = 100) -> tuple:
    """Trainee npcs, one id per character at the given dress suffix.

    ONLY characters this build has art for. single_mode_npc carries the whole
    JP roster, and a character the client cannot draw leaves a blank white
    plane on the opponent panel instead of a portrait -- user-reported
    2026-09-07; see master_data.has_portrait_art. 18 of the table's 84
    characters are in that state here, which is why roughly a fifth of every
    named opponent came up empty."""
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id >= 1000000 AND id < 2000000 "
        "AND id % 1000 = ? ORDER BY id", (int(suffix),))
        if master_data.has_portrait_art(r["id"] // 1000))


@functools.lru_cache(maxsize=None)
def boss_npc_ids(rank: int) -> tuple:
    """rank 100 -> the Boss fifteen, 101 -> the Boss+ fifteen."""
    suffix = 101 if int(rank) >= BOSS_PLUS_RANK else 100
    ids = tuple(i + (suffix - 100) for i in BOSS_NPC_IDS)
    have = {r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id >= 3000000 AND id < 3100000")}
    return tuple(i for i in ids if i in have)


# TWO OF THE BOSS FIFTEEN ARE UNDRAWABLE, and unlike the ordinary teams they
# cannot simply be dropped from a pool: the fifteen are fixed, and rows *04 and
# *06 (Bitter Glasse 2002 and Little Cocon 2003) are the team's two STRONGEST
# runners -- at rank 101, speed 862 and 972 against a mob wall of 985/694.
#
# Filtering the named POOL (named_npc_pool) fixed the ordinary teams on
# 2026-09-07 but never touched these, because the boss fifteen bypass the pool
# entirely. _opponent_horse's own guard anonymises the RACE BODY, which is why
# the race itself looked right -- but the opponent-select panel and the race
# list resolve the portrait from the npc_id on the wire themselves
# (PartsSingleModeScenarioTeamRaceOpponent.SetupCharaImage -> single_mode_npc
# -> chara_id -> card art), so those two seats still drew blank white planes.
# There is no mob_id or chara_id field on a team_data_array seat to override
# it with; the id IS the identity. So the id itself has to become a drawable
# one, and the stats have to be carried across separately.
#
# The substitute is a real mob npc row (always renderable, and the boss cutscene
# maps its first six seats through mob data anyway), chosen by a stable index so
# a given boss row always yields the same face across restarts and re-rolls.
# boss_stat_source inverts it: _opponent_horse reads the ORIGINAL row for stats,
# aptitudes and running style, so the final is exactly as hard as before and
# only the two faces are anonymous. If the art ever ships, has_portrait_art goes
# true on its own and both maps empty out.
@functools.lru_cache(maxsize=1)
def _boss_art_substitutes() -> dict:
    pool = mob_npc_pool()
    out, used = {}, set()
    for rank in (BOSS_RANK, BOSS_PLUS_RANK):
        for npc in boss_npc_ids(rank):
            row = npc_row(npc) or {}
            if int(row.get("mob_id") or 0):
                continue
            if master_data.has_portrait_art(row.get("chara_id")):
                continue
            if not pool:
                continue
            i = npc % len(pool)
            while pool[i] in used:          # keep the fifteen distinct
                i = (i + 1) % len(pool)
            used.add(pool[i])
            out[npc] = pool[i]
    return out


def boss_art_substitute(npc_id: int) -> int:
    """The drawable mob npc row to field in place of an undrawable boss seat,
    or the id unchanged when the client can already draw it."""
    return _boss_art_substitutes().get(int(npc_id), int(npc_id))


def boss_stat_source(npc_id: int, rank: int) -> int:
    """Inverse of boss_art_substitute: the row a substituted seat takes its
    stats and aptitudes from. Identity for every seat that was not substituted.

    RANK-GATED, and it has to be: the substitutes come from the same
    mob_npc_pool the ordinary teams field, so inverting by value alone would
    hand boss stats to whichever rank-3 mob happened to share the id."""
    if int(rank) < BOSS_RANK:
        return int(npc_id)
    for original, substitute in _boss_art_substitutes().items():
        if substitute == int(npc_id):
            return original
    return int(npc_id)


# The opposing team's trainer. The capture gives them a real-looking viewer_id
# (9000000000xx) and a team name; the neutral field gets viewer_id 0 and no
# name at all, which is how the client tells "the other team" from "the rest of
# the field" when it tints the gates.
OPPONENT_VIEWER_ID = 900000000093

_TEAM_NAME_WORDS = (
    "Swiftwind", "Carrot", "Thunder", "Meteor", "Aurora", "Crescent",
    "Vanguard", "Horizon", "Sunrise", "Comet", "Tempest", "Zenith",
)


def opponent_team_name(set_id: int) -> str:
    """A stable name for the team behind a given race set.

    Cosmetic, and ours: the real names live in the account service, not in
    master.mdb. Keying on the set id keeps a team called the same thing every
    time the player is offered it."""
    return "Team " + _TEAM_NAME_WORDS[int(set_id) % len(_TEAM_NAME_WORDS)]


@functools.lru_cache(maxsize=None)
def npc_row(npc_id: int) -> dict | None:
    row = master_data.query_one(
        "SELECT * FROM single_mode_npc WHERE id=?", (int(npc_id),))
    return dict(row) if row else None


@functools.lru_cache(maxsize=None)
def npc_running_style(npc_id: int) -> int:
    """The npc's own best aptitude. Matches the capture on 626 of 652 runners;
    the rest are aptitude ties, where either answer is as good."""
    row = npc_row(npc_id)
    if not row:
        return 2
    return max(_STYLE_APTITUDE, key=lambda s: row.get(_STYLE_APTITUDE[s], 1))


# (total runners, how many of them are NAMED) per opposing team, by the set's
# rank. Straight off the corpus -- rank 8's themed teams are all-named and
# SMALLER than the tiers below them, which is not a typo: 7 or 8 runners.
OPPONENT_TEAM_SHAPE = {
    1: (10, 1), 2: (10, 2), 3: (10, 2), 4: (13, 3), 5: (13, 3),
    6: (13, 5), 7: (15, 6), 8: (8, 8), 100: (15, 15), 101: (15, 15),
}

# The opposing team's own ladder rank, shown on the offer card. Observed bands
# per tier; the exact value inside a band wobbles between rolls.
OPPONENT_RANK_BAND = {
    1: (34, 35), 2: (28, 31), 3: (23, 25), 4: (18, 21), 5: (14, 16),
    6: (8, 12), 7: (4, 7), 8: (3, 3), 100: (1, 1), 101: (1, 1),
}


def final_rank_stat_bonus(team_rank: int) -> int:
    """race_single_mode_team_status: the flat stat bonus the trainee is paid at
    graduation for the team's final ladder rank. Ranks 1-6 -> +50, 7-11 -> +30,
    12-16 -> +20, 17-21 -> +10, 22+ -> 0.

    The thresholds are INCLUSIVE upper bounds (rows 6/11/16/21/26/99999), so
    the lookup is >=, not >. With > , every boundary rank paid the tier below
    it -- a team that finished exactly 6th got +30 instead of +50."""
    row = master_data.query_one(
        "SELECT add_status FROM race_single_mode_team_status "
        "WHERE team_rank_threshold >= ? ORDER BY team_rank_threshold LIMIT 1",
        (int(team_rank),))
    return int(row["add_status"]) if row else 0


# ================================================================ the soul ==

# A burst's 5-stat grant, by teammate type and tier. CONFIRMED: 19/19 observed
# bursts matched exactly, and the vector depends only on the type -- not the
# command that fired it, not the turn, not team rank (partner 1010 bursting at
# turn 17 and partner 2 bursting ~40 turns later produced identical vectors).
#
# The SAME amount is added to the teammate's CAPS. That is the whole reason a
# burst matters: ordinary training never raises a teammate's ceiling.
#                       speed stam  pow  wiz guts
BURST_VECTORS = {
    "speed":   {SOUL_EXPLODED:    (180,  80, 110,  70,  80),
                SOUL_SP_EXPLODED: (210,  80, 130,  80,  80)},
    "stamina": {SOUL_EXPLODED:    ( 90, 160,  80,  70, 110),
                SOUL_SP_EXPLODED: ( 90, 190,  80,  80, 130)},
    "power":   {SOUL_EXPLODED:    ( 90, 120, 150,  70,  80),
                SOUL_SP_EXPLODED: ( 90, 140, 180,  80,  80)},
    "wiz":     {SOUL_EXPLODED:    (130,  80,  80, 150,  80),
                SOUL_SP_EXPLODED: (150,  80,  80, 180,  80)},
    "guts":    {SOUL_EXPLODED:    (100,  80,  90,  70, 150),
                SOUL_SP_EXPLODED: (120,  80, 100,  80, 180)},
}

# The chance one participating teammate's gauge advances a threshold.
#
# The raw gauge is never on the wire (only the bucketed soul_threshold_id) and
# no master.mdb table or dump.cs path holds it, so this cannot be read off
# directly -- but it CAN be pinned by the capture's own totals, which is what
# 0.85 is:
#
#   final histogram, 19 teammates: (2,0)x3 (3,0)x2 (4,0)x3 (5,2)x11
#   threshold steps achieved                     = 60
#   burst participations (11 members x 2)        = 22   <- consume a
#                                                          participation but
#                                                          advance no threshold
#   guide_partner_count at graduation            = 93
#   => 60 steps / (93 - 22) non-burst participations = 0.85
#
# A coin flip was the first estimate (from participations-between-increments,
# [0,0,0,1,1,1,1,1,2,2]) but it is too slow: simulated over a full run it fills
# 5-7 of 19 gauges where the capture fills 11, and it does so while spending
# MORE participations than the capture did -- the arithmetic above is the
# tighter constraint. See docs/UNITY_CUP.md §5.2.
SOUL_FILL_CHANCE = 0.85


def soul_of(member: dict) -> int:
    return int(member.get("soul") or 1)


def burst_state(member: dict) -> int:
    return int(member.get("burst") or SOUL_NONE)


def burst_ready(member: dict) -> int | None:
    """Which burst this teammate would fire if they trained right now, or None.

    The state machine (CONFIRMED, 47 transitions, zero deviations):
        soul_threshold_id 1 -> 2 -> 3 -> 4 -> 5, then at 5
        soul_event_state  0 -> 1 (Exploded) -> 2 (SpExploded) -> terminal.
    Exactly two bursts per teammate per run, ever."""
    if soul_of(member) < SOUL_THRESHOLD_MAX:
        return None
    state = burst_state(member)
    if state == SOUL_NONE:
        return SOUL_EXPLODED
    if state == SOUL_EXPLODED:
        return SOUL_SP_EXPLODED
    return None                      # already SpExploded -- terminal


def advance_soul(member: dict, rng: random.Random) -> None:
    """Tick one teammate's gauge for a training they took part in.

    Only the THRESHOLD advances here. The burst itself is fired by
    fire_burst(), which the caller reaches through burst_ready() so that the
    preview and the payout agree on who is about to detonate."""
    if soul_of(member) >= SOUL_THRESHOLD_MAX:
        return
    if rng.random() < SOUL_FILL_CHANCE:
        member["soul"] = soul_of(member) + 1


def fire_burst(member: dict, tier: int) -> dict:
    """Detonate: grant the type's vector to stats AND to caps, and move the
    state machine on. Returns {stat: gain} for the caller's own logging."""
    vector = BURST_VECTORS[member_soul_type(member)][tier]
    gains = {}
    for stat, gain in zip(STATS, vector):
        member[stat] = int(member.get(stat) or 0) + gain
        member[stat + "_limit"] = int(member.get(stat + "_limit") or 0) + gain
        gains[stat] = gain
    member["burst"] = tier
    return gains


# Ordinary (non-burst) growth for a teammate who joined a training. The shape
# is observed, the generating function is not, but the BUDGET is pinned by the
# capture: total roster stat gain 37,943, minus ~19,000 of post-race team-wide
# grants and ~12,100 of burst vectors, leaves ~6,800 over the 71 non-burst
# participations -- about 96 stat points each, which is what these ranges sum
# to. Individual observed deltas match the shape (same Wit training, same turn:
# {spd 37, sta 14, pow 9, wiz 66, guts 9} and {spd 38, sta 9, pow 10, wiz 65,
# guts 9}) -- those are late-run, facility-levelled trainings at the top of the
# range. Caps are NOT touched here: only bursts raise a teammate's ceiling.
GROWTH_MAIN = (44, 58)
GROWTH_OFF = (4, 11)
# The facility's secondary stat also grows noticeably (a Speed training moves
# Power, a Wit training moves Speed) -- the same pairing the trainee's own
# training table uses.
GROWTH_SECONDARY = (14, 30)
SECONDARY_STAT = {"speed": "power", "stamina": "guts", "power": "stamina",
                  "guts": "power", "wiz": "speed"}


def grow_member(member: dict, command_id: int, rng: random.Random,
                capped: list | None = None) -> dict:
    """Ordinary growth for one teammate who joined this facility's training.

    Every stat is clamped to that teammate's own cap -- the caps are what a
    soul burst exists to lift, so ignoring them here would make bursts
    pointless.

    `capped`, if given, collects the ParameterGainLimitType codes (1-5, the
    same numbering as event_engine._STAT_KEY_IDX) of stats that were ALREADY
    at their ceiling when this training tried to raise them. That is the
    "already at its ceiling" rule not_up_info uses for the trainee's own
    display, not "reached the cap on this training": a stat that merely
    arrives at its cap still gains, and the client cuts the number off itself.
    See _record_capped for where the codes go on the wire."""
    main = COMMAND_STAT.get(CAMP_BASE.get(command_id, command_id))
    if main is None:
        return {}
    secondary = SECONDARY_STAT.get(main)
    gains = {}
    for stat in STATS:
        if stat == main:
            lo, hi = GROWTH_MAIN
        elif stat == secondary:
            lo, hi = GROWTH_SECONDARY
        else:
            lo, hi = GROWTH_OFF
        gain = rng.randint(lo, hi)
        cap = int(member.get(stat + "_limit") or 0)
        cur = int(member.get(stat) or 0)
        if capped is not None and gain > 0 and cap and cur >= cap:
            capped.append(STAT_LIMIT_CODE[stat])
        gain = max(0, min(gain, cap - cur))
        if gain:
            member[stat] = cur + gain
            gains[stat] = gain
    return gains


# not_up_team_parameter_info.status_array -- the "this teammate is capped"
# notice. Client type is TeamParameterCode {training_partner_id, code}
# (dump.cs:768279) but the WIRE key is product_code, which is what the real
# captures carry. The code is a ParameterGainLimitType, the same 1-5 stat
# numbering NotUpParameterInfo.status_type_array uses for the trainee (see
# event_engine.not_up_info) -- confirmed by the two values real ever sends
# here, 1 and 3, against teammates whose speed and power caps were reached.
#
# Rare by nature: 10 of 1317 real team_data_set records carry a non-empty
# status_array, all in late career, on check_event and exec_command. We served
# a hardcoded [] for the whole run, so the notice never appeared at all.
STAT_LIMIT_CODE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

_CAPPED_KEY = "not_up_team_parameters"


def record_capped(st: dict, target_id, codes) -> None:
    """Queue one teammate's capped-stat codes for the next response."""
    if not codes:
        return
    rows = st.setdefault(_CAPPED_KEY, [])
    for code in sorted(set(codes)):
        rows.append({"training_partner_id": target_id, "product_code": code})


def _take_capped(st: dict) -> list:
    """Consume the queued notices. Popped, not read: the real panel belongs to
    the training response that produced it, exactly like command_result."""
    return st.pop(_CAPPED_KEY, None) or []


# ================================================== scoring: the three ranks ==

def rank_score(member: dict) -> int:
    """A teammate's rank_score -- the SAME formula as the trainee's own career
    rank score, and as team_stadium's _slot_evaluation_point base term.

    CONFIRMED: get_rating(stats) alone leaves a residual that is constant per
    teammate across a whole 78-turn run (+/-2), i.e. a term independent of
    stats; that residual is their skill score. Where the capture's skill
    snapshot was contemporaneous with the stats the match is exact (teammate
    1022: residual 778, get_skill_score 778; teammate 1052 with no skills:
    residual 1, score 0).

    NOTE no aptitude rate is applied. team_stadium scales its slot score by
    one; teammate rank_score is the unscaled base."""
    stats = [member.get(s, 0) or 0 for s in STATS]
    skills = [{"skill_id": s} for s in member_race_skills(member)]
    total = rating_formula.get_rating(stats) + rating_formula.get_skill_score(skills)
    return min(rating_formula.MAX_RANK_SCORE, total)


# KNOB (fitted, not derived). Lower bound of each TeamParameterRank on the
# team AVERAGE of that stat: G=1, F=2, E=3, D=4, C=5, B=6, A=7, S=8.
#
# Fitted to the observed per-rank bands across 321 capture snapshots (zero
# overlaps between adjacent ranks -- see docs/UNITY_CUP.md §3). A single shared
# table on the plain roster average is SLIGHTLY inconsistent with the data
# (speed crossed to C at avg 337.7 while wiz was still D at 338.3), so either
# the ranks also recompute on a lag or the denominator is not the roster count.
# One run cannot separate those; this table reproduces every observation to
# within a few points.
# TeamParameterRank cutoffs on the team's per-stat average. REFITTED against
# the whole capture once the trainee was included in that average (see
# stat_ranks): with her in, every boundary from 2 upward separates into a clean
# gap with no overlapping observation at all --
#     2->3 (199.73, 201.27]   3->4 (253.36, 262.36]   4->5 (338.31, 342.08]
#     5->6 (418.92, 424.06]   6->7 (504.95, 514.24]   7->8 (607.95, 616.25]
# -- and these seven values sit inside their windows, scoring 1574/1580
# rank observations (99.6%). The six misses are all the 1->2 boundary on the
# turn-4 intake, where four fresh members dilute the average below 150 while
# the wire still reads rank 2 for a few requests.
#
# The previous 250 / 430 / 620 fell OUTSIDE their windows, which is what made
# the old average (roster only, no trainee) look defensible: two errors partly
# cancelling.
STAT_RANK_THRESHOLDS = (150, 200, 260, 340, 420, 510, 610)


def stat_rank(average: float) -> int:
    rank = 1
    for threshold in STAT_RANK_THRESHOLDS:
        if average >= threshold:
            rank += 1
        else:
            break
    return min(rank, 8)


def stat_ranks(members: list, trainee: dict | None = None) -> dict:
    """{stat: TeamParameterRank} from the team's per-stat averages.

    THE TRAINEE IS PART OF THE TEAM. She races for it in every team race, and
    she is in this average: at capture 0135 the six members' own speed average
    is 142.0 -- below every candidate cutoff -- yet the wire says speed_rank 2,
    while wiz averages HIGHER at 143.3 and still reads rank 1. Only folding her
    199 speed in as a seventh member separates them (150.1 vs 144.6).
    Roster-only scored 1450/1580 against the capture; with her, 1574/1580.

    SemiMembers are excluded -- they have stats but have not joined. That is
    why every rank reads 1 through turns 1-2 while all ten sit at
    member_state 2, and speed only becomes 2 at 0135 when the deck six flip."""
    active = [m for m in members if m.get("state") == MEMBER_STATE_TEAM]
    if not active:
        return {s: 1 for s in STATS}
    n = len(active) + (1 if trainee else 0)
    return {s: stat_rank((sum(m.get(s, 0) or 0 for m in active)
                          + (int(trainee.get(s) or 0) if trainee else 0)) / n)
            for s in STATS}


def ratcheted_ranks(st: dict) -> dict:
    """The five TeamParameterRanks as the wire reports them: a HIGH-WATER MARK.

    A rank never goes down. The team average genuinely can -- four fresh
    members join on turn 4 and dilute it, dropping speed from 150.1 to 146.3 --
    but the capture keeps reporting speed_rank 2 straight through that. Taking
    the running max instead of the instantaneous value takes the fit from
    1574/1580 to 1580/1580: EVERY rank observation in the capture, exactly.

    Idempotent, so it is safe to call on every response."""
    now = stat_ranks(st.get("members") or [], st.get("trainee_stats"))
    hi = st.setdefault("ranks_hi", {})
    for stat, rank in now.items():
        if rank > int(hi.get(stat) or 1):
            hi[stat] = rank
    return {stat: max(int(hi.get(stat) or 1), 1) for stat in STATS}


# ------------------------------------------------- holding the race turn --
# A TEAM-RACE TURN DOES NOT ADVANCE. Capture 0212-0227 puts it beyond doubt:
# an ordinary turn's exec_command reports the NEXT turn (req 22 -> chara_info
# 23), but the exec_command that lands on turn 24 reports 24, and 24 is what
# every response carries -- the gate (201029), the race screen, team_edit,
# opponent_list, team_race_start/end/out, and the post-race beats -- until the
# check_event that acknowledges the last of them, which finally reports 25.
#
# We advanced on exec_command like any other turn, so the whole Unity Cup slid
# one turn late: the race opened at the start of turn 25, the history row was
# banked at 25, and the standings screen had a result belonging to no round.
# Worse, a player who RACED on turn 24 advanced past it before the gate could
# claim the display slot, and the Unity Cup simply never happened
# (user-reported 2026-09-06).
#
# The machinery for this already exists and Grand Live's concerts use it:
# interstitial_pending reserves the display slot for the set-piece and starts
# the hold, held_turn serves the frozen number, and the run's own persisted
# turn keeps advancing underneath untouched.
RACE_HOLD_KEY = "race_turn_hold"
# status_type_array codes the team-race pay-out owes this response -- see
# settle_award and attach().
SETTLE_NOT_UP_KEY = "settle_not_up"

POST_RACE_LAST_KEY = "post_race_last"
# The result beats still owed, ONE PER RESPONSE -- see next_post_race_beat.
POST_RACE_CHAIN_KEY = "post_race_chain"


def race_turn_pending(st: dict, turn) -> bool:
    """Whether `turn` is a team-race turn whose race has not been run yet."""
    turn = int(turn or 0)
    if turn not in TEAM_RACE_TURNS:
        return False
    round_no = TEAM_RACE_TURNS.index(turn) + 1
    return len(st.get("races") or ()) < round_no


def begin_race_hold(st: dict, turn) -> None:
    st[RACE_HOLD_KEY] = int(turn or 0)


def race_hold(st: dict, current_turn=None) -> int:
    """The frozen turn, releasing itself once the round is fully played out.

    The self-heal is deliberately narrow -- the race banked, its post-race
    beats already handed out, and none of them still awaiting a resolution --
    so a client that drops out mid-chain cannot strand the career on a turn it
    has already finished, while a race still on screen keeps its hold.

    THE SECOND VALVE IS THE BRICK GUARD, and it is not narrow. A hold whose
    last beat is never acknowledged is unreleasable: the client is served turn
    60 forever, and every turn it plays goes onto the run's REAL turn
    underneath. That is a softlock (user-reported 2026-09-06, round four: the
    result beat resolved, the beat after it was lost, and the goal race for
    turn 60 kept coming back). `current_turn` is that run's own turn -- once it
    is two clear turns past the held one the client has demonstrably moved on,
    so the hold is stale whatever the chain thinks. One turn is NOT enough:
    during a legitimate hold the run advances exactly one turn underneath.

    AND IT MUST NOT ASK WHETHER THE ROUND WAS RACED. It used to, which quietly
    made the guard narrow again and left the one hold it was written for --
    a round that never OPENED -- unreleasable: `raced` is false precisely then,
    so both valves stayed shut and the served turn froze for good
    (user-reported 2026-09-06, round five: live save 802445340143 sat on
    race_turn_hold 72 with four rounds banked, pending None and the run's own
    turn already at 73, training turn after training turn going onto a turn the
    client would never be shown). Dropping `raced` costs the legitimate case
    nothing, because the +2 margin is what protects a race still on screen:
    during a real hold the run sits exactly one turn ahead, so reaching held+2
    already means the client moved on without it. The unrun round is not lost
    either -- once the served turn is past it, unity_cup_missed_race re-offers
    it.
    """
    held = int(st.get(RACE_HOLD_KEY) or 0)
    if not held:
        return 0
    if held in TEAM_RACE_TURNS:
        round_no = TEAM_RACE_TURNS.index(held) + 1
        raced = len(st.get("races") or ()) >= round_no
        if (not st.get("pending")
                and raced
                and round_no in (st.get("post_race_served") or ())
                and not st.get(POST_RACE_LAST_KEY)):
            release_race_hold(st)
            return 0
        if current_turn and int(current_turn) >= held + 2:
            release_race_hold(st)
            return 0
    return held


def next_post_race_beat(st: dict, resolved_event_id):
    """The next result beat to serve now that `resolved_event_id` resolved, or
    None if that id is not the head of the chain.

    ONE BEAT PER RESPONSE, which is what the capture shows: on turn 24 the
    result beat 201030 arrives on team_race_out and the join beat 201146 on the
    check_event straight after. We used to ship the whole array at once and the
    client played only its head -- harmless for a two-beat round it happened to
    finish, fatal on round four, whose elite-win beat (201166) is the one the
    hold waits for.
    """
    chain = list(st.get(POST_RACE_CHAIN_KEY) or ())
    if not chain or not resolved_event_id:
        return None
    if int(resolved_event_id) != int(chain[0].get("event_id") or 0):
        return None
    chain.pop(0)
    st[POST_RACE_CHAIN_KEY] = chain
    return copy.deepcopy(chain[0]) if chain else None


def release_race_hold(st: dict) -> None:
    st.pop(RACE_HOLD_KEY, None)
    st.pop(POST_RACE_LAST_KEY, None)
    st.pop(POST_RACE_CHAIN_KEY, None)


def scheduled_race_turn(race_num: int, fallback: int = 0) -> int:
    """The turn round `race_num` is SCHEDULED for -- what the wire carries."""
    index = int(race_num or 0) - 1
    if 0 <= index < len(TEAM_RACE_TURNS):
        return TEAM_RACE_TURNS[index]
    return int(fallback or 0)


def best_stat(st: dict) -> str:
    """The stat the team ranks HIGHEST in -- what "Team Zenith Declares War"
    picks its Burning/Ignited Spirit skill from ("The team's unbelievably
    passionate focus on Wit has had a positive effect on ...").

    Ties break in STATS order, which is the order the client lists them in."""
    ranks = ratcheted_ranks(st)
    return max(STATS, key=lambda s: (int(ranks.get(s) or 1), -STATS.index(s)))


def team_power(ranks: dict) -> int:
    """TeamTotalPower: max of the five stat ranks, except all-S -> SPlus.

    CONFIRMED on 308/321 snapshots; the 13 exceptions are all inside the window
    before the first recompute (team_power is initialised to 1 at start).

    Callers must recompute this LAZILY -- see refresh_power(). It visibly lags
    the stat ranks by up to a turn in the capture."""
    values = [ranks.get(s, 1) for s in STATS]
    return 9 if all(v == 8 for v in values) else max(values)


def earned_power(st: dict) -> int:
    """The team_power the roster has EARNED -- max of the five ratcheted stat
    ranks, all-8 -> 9. This is not what goes on the wire; see refresh_power."""
    return team_power(ratcheted_ranks(st))


def refresh_power(st: dict) -> None:
    """Keep the rank high-water marks current. DELIBERATELY DOES NOT MOVE
    team_power.

    team_power is not a derived number on the wire -- it is a value the player
    is AWARDED, and it only moves when the "Team Power Increased" cutscene is
    acknowledged (single_mode_story_data 201010/201077/201011/201078/201012/
    201013/201014/201015, text_data 181 titles them exactly that). Four
    independent capture runs agree: all 8 events fire once each, in the same
    level order every time, and in every one of the 32 transitions team_power
    increments on the response IMMEDIATELY after the event is acknowledged --
    never before.

    That is why the lag looked irregular when read as a schedule: the turns are
    different in all four runs (power 2 at turn 4/4/5, power 3 at turn 21/19/17
    -- live-reported as "it takes some training, 1-2 turns of clicking"). The
    trigger is the stats, and the award is the event.

    Recomputing team_power here instead is what made the rank jump to F the
    instant the six support cards joined, with no training and no cutscene."""
    ratcheted_ranks(st)


def team_race_reward(set_rank) -> tuple:
    """(trainee stat gain, trainee skill points) for beating this opponent
    tier. See TEAM_RACE_REWARDS."""
    return TEAM_RACE_REWARDS.get(int(set_rank or 0), (0, 0))


def bank_race_award(st: dict, new_rank: int, set_rank: int, won: bool,
                    final: bool) -> None:
    """Hold a finished race's pay-out until the request AFTER team_race_out.

    THE LADDER DOES NOT MOVE AT team_race_end. The capture is explicit: on
    team_race_end and team_race_out the team is still at its OLD rank (30) and
    the NEW one rides alongside as tmp_team_rank (22) -- that pair is what the
    client animates the rank-up screen from and to. Only the check_event after
    race_out carries the committed rank, the teammates' new stats and the
    event_effect_info summary panel.

    Moving it at race_end, as this did, made tmp_team_rank and team_rank
    identical, so the screen animated 22 -> 22, the reward panel had nothing to
    show, and the whole rank-up went by invisibly (user-reported 2026-09-05:
    "when your team rank raises, you should get stats")."""
    st["pending_award"] = {"rank": int(new_rank), "set_rank": int(set_rank or 0),
                           "won": bool(won), "final": bool(final)}


def pending_rank(st: dict) -> int:
    """The rank to SERVE as tmp_team_rank -- where the ladder is about to land
    if a race is settled, else where it already is."""
    award = st.get("pending_award")
    if award:
        return int(award.get("rank") or st.get("team_rank") or 30)
    return int(st.get("team_rank") or 30)


def settle_race_award(st: dict, chara_info) -> bool:
    """Commit a banked race pay-out. True if anything changed.

    Everything the rank-up screen shows happens here, in ONE response:
      * the ladder moves,
      * every teammate gains 50 (10 if the race was not won) in all five stats,
      * event_effect_info carries that same number as the summary panel,
      * and the TRAINEE is paid -- stats only on a win, skill points either
        way. See TEAM_RACE_REWARDS for where the numbers come from.

    A DRAW is treated as not-a-win. No capture contains one, so that is the
    conservative reading rather than an observed rule.
    """
    award = st.pop("pending_award", None)
    if not award:
        return False
    new_rank = int(award.get("rank") or st.get("team_rank") or 30)
    won = bool(award.get("won"))
    st["team_rank"] = new_rank

    gain = TEAMMATE_GAIN_WIN if won else TEAMMATE_GAIN_NOT_WIN
    for member in active_members(st):
        for stat in STATS:
            cap = int(member.get(stat + "_limit") or 0)
            member[stat] = min(cap, int(member.get(stat) or 0) + gain)
    st["event_effect_info"] = dict({"is_summarize_team_member": True},
                                   **{"gain_" + stat: gain for stat in STATS})

    stat_gain, skill_points = team_race_reward(award.get("set_rank"))
    if isinstance(chara_info, dict):
        if won and stat_gain:
            # "<stat> is in superb form" for anything the pay-out lands on that
            # is ALREADY at its cap -- the same notice an event reward earns
            # (event_engine.not_up_info). This pay-out writes the stats
            # directly rather than through a choice, so it has to name its own
            # capped ones or the panel silently shows nothing at all
            # (user-reported 2026-09-07).
            from ...event_engine import capped_stat_indexes
            capped = sorted(capped_stat_indexes(chara_info))
            if capped:
                st[SETTLE_NOT_UP_KEY] = capped
            for stat in STATS:
                cap = int(chara_info.get("max_" + stat) or 0)
                chara_info[stat] = min(cap, int(chara_info.get(stat) or 0) + stat_gain)
        if skill_points:
            chara_info["skill_point"] = (int(chara_info.get("skill_point") or 0)
                                         + skill_points)
    # The final race locks the ladder -- team_rank_state 1 from turn 72 on.
    if award.get("final"):
        st["rank_state"] = 1
    refresh_power(st)
    log.info("unity cup: race settled, rank -> %s (%s), teammates +%s, "
             "trainee +%s / %s SP", new_rank, "won" if won else "not won",
             gain, stat_gain if won else 0, skill_points)
    return True


def power_up_resolved(st: dict, level: int) -> None:
    """A "Team Power Increased" cutscene was acknowledged: bank the level.

    Idempotent and monotonic -- replaying an event never lowers team_power, and
    the level comes from the event's own id, so it cannot outrun the map."""
    level = int(level or 0)
    if level > int(st.get("power") or 1):
        st["power"] = level


# How far toward their cap a MID-RUN scout joins at. The opening roster joins
# as rookies at their master-row base stats, but a teammate scouted after a
# team race arrives already trained -- the capture has them entering at roughly
# three quarters of their own limits rather than at 140-ish like the turn-3
# intake.
VETERAN_JOIN_FRACTION = 0.74


# A TEAMMATE'S OWN RACE SKILLS. These were never populated -- _new_member
# seeded "skills": [] and nothing ever appended to it -- so every teammate ran
# all five team races with no skills at all, and their rank_score carried no
# skill term. race_simulator feeds skill_array straight into the physics
# engine, so this is the same class of bug as the career-race mob one
# single_mode_team._npc_skill_array was written for and the opponent-side one
# fixed after it; our OWN team was the last side still running bare.
#
# Measured over the two 20260905 sessions, mean skills per runner:
#     team_id 0 (neutral mobs)  2.07   -- ours already matches (2.05)
#     team_id 2 (their team)    3.78
#     team_id 1 (OUR team)      5.72   -- ours was 0.00
# and our side rises 4.55 / 5.25 / 5.72 / 6.28 / 6.45 over the five races,
# i.e. teammates JOIN already skilled rather than accruing from nothing.
#
# The source is not invented: 76 of the 79 scoutable charas have a
# single_mode_npc row carrying a skill_set_id, mean 5.42 skills (range 4-8),
# which lands inside the measured band -- and it is the same table and the same
# join the opponent side already uses. The three charas with no npc row keep an
# empty list, which real also has runners for (the 0 bucket).
_MEMBER_SKILLS_VERSION = 1


@functools.lru_cache(maxsize=256)
def _chara_race_skills(chara_id: int) -> tuple:
    """The skill ids a named uma races with, from her own single_mode_npc row."""
    if not chara_id:
        return ()
    row = master_data.query_one(
        "SELECT skill_set_id FROM single_mode_npc "
        "WHERE chara_id=? AND skill_set_id>0 ORDER BY id", (chara_id,))
    if row is None:
        return ()
    from ...handlers import single_mode_team as smt
    return tuple(int(s["skill_id"])
                 for s in smt._npc_skill_array(int(row["skill_set_id"]) or 0))


def member_race_skills(member: dict) -> list:
    """A teammate's skill ids, resolved on first read.

    Lazy and version-stamped rather than assigned once at join, so careers
    saved before this existed pick their skills up on the next response
    instead of racing bare for the rest of the run."""
    if int(member.get("skills_version") or 0) != _MEMBER_SKILLS_VERSION:
        member["skills"] = list(_chara_race_skills(int(member.get("chara_id") or 0)))
        member["skills_version"] = _MEMBER_SKILLS_VERSION
    return member.get("skills") or []


def _new_member(scout_id, target_id: int, state: int,
                veteran: bool = False, chara_id=None,
                support_card_id: int = 0) -> dict:
    """A teammate at their single_mode_scout_chara starting stats and caps.

    speed_limit_base is kept alongside speed_limit because the wire carries
    both: the base is the master row's value and never moves, the live limit is
    what soul bursts push up.

    scout_id MAY BE None: a pal or group card's character joins the team like
    anyone else (user-confirmed 2026-09-05) but has no scout row, so she takes
    the pool average and carries her own card id instead. Everything that reads
    scout_pool() by scout_id already tolerates a miss, so no other caller needs
    to know the difference."""
    row = scout_pool().get(int(scout_id)) if scout_id is not None else None
    if not row:
        row = dict(_pool_average())
        row["chara_id"] = int(chara_id or 0)
    member = {"target_id": int(target_id),
              "scout_id": int(scout_id) if scout_id is not None else None,
              "chara_id": int(chara_id if chara_id is not None
                              else row.get("chara_id") or 0),
              "support_card_id": int(support_card_id or 0),
              "state": state, "soul": 1, "burst": SOUL_NONE, "skills": []}
    for stat, col, lim in (("speed", "speed", "speed_limit"),
                           ("stamina", "stamina", "stamina_limit"),
                           ("power", "pow", "pow_limit"),
                           ("wiz", "wiz", "wiz_limit"),
                           ("guts", "guts", "guts_limit")):
        cap = int(row.get(lim) or 700)
        base = int(row.get(col) or 100)
        member[stat] = max(base, int(cap * VETERAN_JOIN_FRACTION)) if veteran else base
        member[stat + "_limit"] = cap
        member[stat + "_limit_base"] = cap
    return member


# Roster growth after each team race, as counts ADDED to the opening roster.
# Capture: 10 members after the turn-4 intake, then 12 / 16 / 19 after team
# races 1 / 2 / 3. Races 4 and 5 recruited nobody. Expressed as increments so a
# deck with fewer scoutable cards still grows by the right amount.
ROSTER_GROWTH_AFTER_RACE = (2, 6, 9)

# How many "After the Race: New Members Join!" beats have been ACKNOWLEDGED.
# The wave is keyed on this and not on len(st["races"]), for the same reason
# the deck six are keyed on team_support_done: the client renders the "X joined
# the team!" list by diffing team_chara_info_array against the previous
# response, so the intake has to land on the response that resolves the beat.
# Growing the roster at team_race_out instead left no delta by the time 201146
# played, and the event showed its two story boxes and nothing else
# (user-reported 2026-09-07). Capture 20260905_152744 moves 10 -> 12 -> 16 -> 19
# on exactly the acks of 201146 / 201147 / 201148.
JOIN_WAVES_KEY = "join_waves_done"


def join_wave_resolved(st: dict, round_no) -> None:
    """A join beat was acknowledged: its intake is now due. Idempotent -- the
    marker only ever moves forward, so a replayed ack recruits nobody twice."""
    round_no = int(round_no or 0)
    if round_no > int(st.get(JOIN_WAVES_KEY) or 0):
        st[JOIN_WAVES_KEY] = round_no


def _rng_for(st: dict, salt: str) -> random.Random:
    return random.Random(f"{st.get('seed', 0)}:{salt}")


def deck_scout_rows(chara_info) -> list:
    """[(deck_position, scout_id)] for the player's own equipped support cards.

    THE OPENING ROSTER IS THE PLAYER'S DECK. The six teammates who join on
    turn 3 are the characters of the six support cards the player brought, and
    their training_partner_id is the card's DECK POSITION (1-6), not a chara
    id. Capture proof: the run's deck was [20027, 20031, 20012, 30074, 30070,
    30010] and evaluation_info_array's target_id 1..6 mapped to charas 1051,
    1043, 1032, 1055, 1029, 1022 -- exactly those cards' chara_ids, 6 for 6.

    Getting this wrong is not cosmetic. The client resolves each TeamMember's
    MasterScoutChara / SupportCardId through the deck slot, so inventing random
    charas here makes that lookup null and the client throws
    NullReferenceException in WorkSingleModeScenarioTeamRace.TeamMember
    .get_SupportCardId, from TrainingParamChangeSupportMemberA2U
    .RegisterDownload -- the training-result panel never finishes downloading
    and the career softlocks on whatever turn a support partner first shows up
    in the trained facility (live-reported: turn 8).

    A DECK SLOT HOLDING A PAL/GROUP CARD SEATS NOBODY, and this is a hard
    client constraint, not a preference. TeamMember.MasterScoutChara is
    readonly and resolved through MasterSingleModeScoutChara.GetWithCharaId
    (dump.cs); master.mdb has 238 single_mode_scout_chara rows covering only
    79 distinct chara_ids, and NO 9xxx character has one -- not Tazuna (9001),
    not Light Hello (9008). Seat one anyway and MasterScoutChara is null, so
    the client's own auto-build dies the first time the race screen opens:

        NullReferenceException
          SingleModeScenarioTeamRaceUtils.GetDistanceProperRate (TeamMember, Int32)
          SingleModeScenarioTeamRaceUtils.GetTeamMemberRankScore (...)
          SingleModeScenarioTeamRaceDeckBuilder.RunTeamBuild (...)
          SingleModeScenarioTeamRaceDeckBuilder.Build () / AutoBuild (...)
          SingleModeScenarioTeamRaceUtils.TeamAutoBuild (...)
          SingleModeScenarioTeamRaceTopViewController+<InitializeView>d__6.MoveNext ()

    (user-reported 2026-09-05: the cup opened and then softlocked). The same
    null is what TeamMember.get_SupportCardId was throwing on earlier. Such a
    slot still yields (position, None); the caller must skip it. IsEquip-
    FriendSupportCard() exists for a member who happens to also hold a friend
    card, not for a friend card that became a member."""
    if not isinstance(chara_info, dict):
        return []
    by_card = scout_rows_by_support_card()
    out = []
    for card in chara_info.get("support_card_array") or ():
        card_id = int(card.get("support_card_id") or 0)
        out.append((int(card.get("position") or 0),
                    by_card.get(card_id), card_id))
    return sorted(out)


def scenario_intro_resolved(st: dict) -> None:
    """"What's the Unity Cup?" (201025) fired: the scouting UI unlocks.

    is_scout_enable is not a turn check. The capture flips it False -> True on
    the very response that follows 201025 being acknowledged (0133 exec_command
    turn 2 is still False, 0134 check_event turn 2 is True), i.e. the moment the
    player has been TOLD what the Unity Cup is."""
    st["scout_enabled"] = True


# ---------------------------------------------------------- pal card gate --
# Riko Kashimoto is this scenario's ACTING DIRECTOR, and "The Acting Director"
# (201026, story 400002402, turn 4) is the beat that puts her in the role. Until
# it resolves she is not in training AT ALL -- the same shape as Light Hello in
# Grand Live.
#
# THIS GATES HER PRESENCE, NOT HER OUTING. Recreation with a pal card is earned
# the ordinary way in every scenario: meet her in a facility, then win her own
# unlock event on the per-turn roll, then pick the choice that opens it (hers is
# the 50/50 gamble). This gate simply means none of that can begin before turn
# 4. Every other pal or group card in the deck runs the standard flow from turn
# 1, untouched.
RIKO_CHARA_ID = 9006
RIKO_NPC_TARGET = 106            # her slot in the 101-108 support-NPC band
RIKO_OUTING_KEY = "riko_outing"


def riko_target(chara_info) -> int:
    """Which evaluation row IS Riko this run: her DECK POSITION when the player
    brought her card (that slot is her whole presence -- the "2 Light Hellos"
    rule), else her support-NPC row 106."""
    for card in (chara_info or {}).get("support_card_array") or ():
        if support_card_chara(card.get("support_card_id") or 0) == RIKO_CHARA_ID:
            return int(card.get("position") or 0) or RIKO_NPC_TARGET
    return RIKO_NPC_TARGET


def apply_riko_gate(st: dict, chara_info) -> None:
    """Hold Riko off the training screen until "The Acting Director".

    is_appear is the gate the placement roll reads (single_mode_team.
    _roll_distribution skips a deck position whose row says 0), so this is what
    keeps her out of the facilities, out of bond, and therefore out of the
    per-turn outing roll -- exactly how Grand Live holds Light Hello back until
    turn 4.

    Reasserted on EVERY response rather than flipped once: the shared career
    code strips this scenario's NPC rows and _sync_chara_evaluation mints them
    again, so a one-time flip would be rebuilt away on the next response, and a
    career saved before this fix heals itself instead of staying wrong.

    Capture 0142 -> 0143 (20260905_152744_icarus) is the exact frame: the
    response carrying 201026 has 106 is_appear 0, the response acknowledging it
    has 1, and it stays 1 for the remaining 366 responses of the run."""
    if not isinstance(chara_info, dict):
        return
    target = riko_target(chara_info)
    appear = 1 if st.get(RIKO_OUTING_KEY) else 0
    for row in chara_info.get("evaluation_info_array") or ():
        if isinstance(row, dict) and row.get("target_id") == target:
            row["is_appear"] = appear


def acting_director_resolved(st: dict) -> None:
    """"The Acting Director" fired: Riko joins the training screen from here on.

    (The state key is spelled riko_outing for the careers already carrying it.)"""
    st[RIKO_OUTING_KEY] = True


def team_support_resolved(st: dict, chara_info=None) -> None:
    """The "Team Support" beat fired: the player's six support cards JOIN.

    They do not appear here -- they have existed since turn 1 as SemiMembers
    (see ensure_roster). This only promotes them to full members."""
    st["team_support_done"] = True
    ensure_roster(st, CALENDAR_ENABLE_TURN, chara_info)


def ensure_roster(st: dict, turn: int, chara_info=None) -> None:
    """Bring the roster up to the size and the STATES this turn should have.

    TWO PHASES, AND THE FIRST ONE IS WHY THIS WAS REWRITTEN.

    Every member the run starts with -- the player's six deck charas and the
    scenario's four specials -- exists from turn 1 as a SemiMember
    (member_state 2). They are not added by the story beats; the beats only
    PROMOTE them to member_state 1. Capture proof, in order:

        0127 team_start      state2 = [1,2,3,4,5,6,1010,1030,1052,1056]
        0135 (Team Support)  state1 = [1..6]      state2 = the four specials
        0139 (turn 4)        state1 = all ten     state2 = []

    Growing the roster only on promotion instead -- the previous behaviour --
    left evaluation_info_array holding nothing but the five NPCs while the
    "Team Support" cutscene played. That array is where the client resolves a
    joining member's chara: TeamEvaluationInfo pairs target_id with chara_id,
    and with no row to join against the client drew the GENERIC PORTRAIT and
    left the name out of the message (live-reported). The names have to be on
    the wire BEFORE the event that announces them.

    After the opening intake, one scouting wave per team race, drawn from the
    run's own seed so replaying a turn never reshuffles the team."""
    members = st.setdefault("members", [])
    if turn is None:
        return
    turn = int(turn)

    pool_rows = scout_pool()
    # A character can only be on the team ONCE, even though they own several
    # scout rows. THE TRAINEE COUNTS: she races for this team herself, so
    # scouting her own character puts two of her on the same track.
    taken_charas = {m["chara_id"] for m in members}
    trainee = trainee_chara(chara_info)
    if trainee:
        taken_charas.add(trainee)

    def add(scout_id, target_id=None, state=MEMBER_STATE_SEMI, veteran=False,
            card_id=0):
        """target_id defaults to the chara_id; only DECK members get a
        positional one."""
        if scout_id is None:
            # A pal or group card: no scout row, so no teammate. She stays the
            # support NPC she already is (SUPPORT_NPC_ROWS) and her deck slot
            # fields nobody. See deck_scout_rows for why the client cannot take
            # anything else here.
            return False
        chara_id = pool_rows.get(scout_id, {}).get("chara_id")
        if not chara_id or chara_id in taken_charas:
            return False
        taken_charas.add(chara_id)
        members.append(_new_member(
            scout_id, chara_id if target_id is None else target_id,
            state, veteran=veteran, support_card_id=card_id))
        return True

    # ------------------------------------------------------------- seeding --
    if not members:
        if not isinstance(chara_info, dict):
            return                     # no deck yet -- nothing to seed from
        for position, scout_id, card_id in deck_scout_rows(chara_info):
            # veteran=False: the deck joins as rookies, at their scout row's
            # base stats -- the capture has all six at 700 total on turn 3.
            add(scout_id, target_id=position, card_id=card_id)
        st["deck_size"] = len(members)
        for scout_id in special_charas():
            add(scout_id)              # rookies too: all ten at 700 total
        st["base_size"] = len(members)
        if not members:
            return

    # -------------------------------------------------------------- repair --
    # A career seeded before friend/group slots were filled is missing a
    # teammate, and the client will not open it: TeamMember.get_SupportCardId
    # throws on the hole. Seat the missing slots in place rather than asking
    # the player to restart the run.
    if members and isinstance(chara_info, dict):
        # A career seeded while pal cards were still being seated carries a
        # teammate the client cannot render at all. Evict her, so the save
        # heals itself on the next load instead of staying softlocked.
        pool_charas = {row["chara_id"] for row in pool_rows.values()}
        unrenderable = [(m, "no single_mode_scout_chara row")
                        for m in members if m["chara_id"] not in pool_charas]
        # A DECK MEMBER MUST MATCH THE CARD IN HER SLOT. The seat is derived
        # from the support card standing in that deck position, so the two can
        # only ever disagree if the seat is stale -- left behind by a build
        # that filled the slot differently, most often the friend slot after a
        # pal card was evicted from it. The leftover is a byte-for-byte clone
        # of another slot, which puts the SAME uma on the team twice; that
        # never happens in the real game (0 duplicates across 1317 captured
        # team arrays) and the client cannot build a race deck out of it.
        #
        # The eviction above cannot catch this one on its own: it only removes
        # a member with no scout row at all, and a clone of a real slot has a
        # perfectly good one.
        seat_card = {position: card_id
                     for position, _scout, card_id in deck_scout_rows(chara_info)}
        for m in members[:int(st.get("deck_size") or 0)]:
            want = seat_card.get(m["target_id"])
            if want is not None and int(m.get("support_card_id") or 0) != int(want):
                unrenderable.append(
                    (m, "deck slot %s holds card %s, not %s"
                     % (m["target_id"], want, m.get("support_card_id"))))
        if unrenderable:
            deck_before = int(st.get("deck_size") or 0)
            for m, why in unrenderable:
                if m not in members:
                    continue           # already taken out by an earlier reason
                log.warning("unity cup: evicting teammate chara %s (target %s) "
                            "-- %s", m["chara_id"], m.get("target_id"), why)
                if members.index(m) < deck_before:
                    st["deck_size"] = int(st.get("deck_size") or 1) - 1
                st["base_size"] = max(0, int(st.get("base_size") or len(members)) - 1)
                members.remove(m)
            taken_charas = {m["chara_id"] for m in members}
            if trainee:
                taken_charas.add(trainee)

        size = int(st.get("deck_size") or 0)
        seated = {m["target_id"] for m in members[:size]}
        joined = members[0].get("state", MEMBER_STATE_TEAM) if members else MEMBER_STATE_TEAM
        for position, scout_id, card_id in deck_scout_rows(chara_info):
            if position in seated:
                continue
            if scout_id is None:
                continue                  # pal/group card -- seats nobody
            member = _new_member(scout_id, position, joined,
                                 support_card_id=card_id)
            taken_charas.add(member["chara_id"])
            members.insert(size, member)      # deck members stay the first block
            size += 1
            seated.add(position)
            st["deck_size"] = size
            st["base_size"] = int(st.get("base_size") or len(members)) + 1
            log.info("unity cup: seated missing deck slot %s as chara %s",
                     position, member["chara_id"])

    # ---------------------------------------------------------- promotions --
    deck_size = int(st.get("deck_size") or 0)
    if st.get("team_support_done"):
        for m in members[:deck_size]:
            if m.get("state") == MEMBER_STATE_SEMI:
                m["state"] = MEMBER_STATE_TEAM
        # THE SPECIALS JOIN WHEN "The Word Spreads" RESOLVES, not on a turn.
        # The client renders its "NEW MEMBERS!" panel -- the "X joined the
        # team!" list -- from the member_state 2 -> 1 flip landing in the SAME
        # response that acknowledges the event (capture: still 2 at 0138 with
        # 201020 served, all four at 1 in 0139). Flipping them on a turn check
        # put the transition on a different response, so the story text played
        # but the join list never appeared (user-reported 2026-09-05) -- the
        # same class of bug the deck six had before their promotion was hung
        # off Team Support.
        #
        # TUTORIAL_GUILD_OPEN_TURN survives only as a rescue for careers saved
        # before this flag existed, where the event has already been consumed
        # and would never fire again.
        if st.get("scout_join_done") or (
                turn >= TUTORIAL_GUILD_OPEN_TURN and st.get("announced")):
            for m in members[deck_size:]:
                if m.get("state") == MEMBER_STATE_SEMI:
                    m["state"] = MEMBER_STATE_TEAM

    # -------------------------------------------------------- scout waves --
    waves = int(st.get(JOIN_WAVES_KEY) or 0)
    if not waves:
        return
    base = int(st.get("base_size") or len(members))
    grown = ROSTER_GROWTH_AFTER_RACE[min(waves, len(ROSTER_GROWTH_AFTER_RACE)) - 1]
    target = base + grown
    if len(members) >= target:
        return
    pool = [sid for sid in sorted(pool_rows)
            if pool_rows[sid]["chara_id"] not in taken_charas]
    rng = _rng_for(st, f"roster:{waves}")
    rng.shuffle(pool)
    for scout_id in pool:
        if len(members) >= target:
            break
        # Mid-run scouts join near their cap, and join outright -- there is no
        # SemiMember step for them: 0227 has 1054/1019 at state 1 already.
        add(scout_id, state=MEMBER_STATE_TEAM, veteran=True)


def scout_support_card(scout_id) -> int:
    """The R support card a scouted teammate joined as.

    This is what fills the <support> placeholder in "The Word Spreads": the
    client resolves that token from event_contents_info.support_card_id, and
    serving 0 is why the message read "<support> has joined the team!" with a
    blank portrait (user-reported 2026-09-05). Capture 0138 carries 10008 for
    Taiki Shuttle, which is single_mode_scout_chara row 8's support_card_id --
    so the pool row the teammate joined from already holds the answer."""
    return int((scout_pool().get(int(scout_id or 0)) or {}).get("support_card_id") or 0)


def unannounced_scouts(st: dict) -> list:
    """Teammates who have not yet had their "The Word Spreads" moment, in join
    order. The deck six are never announced this way -- Team Support is their
    arrival.

    STILL A SemiMember WHEN ANNOUNCED. The announcement is what precedes the
    join, not what follows it: capture 0138 serves the event on turn 3 with the
    four specials still at member_state 2, and they only flip to 1 in 0139 --
    the response to acknowledging it. Requiring MEMBER_STATE_TEAM here served
    it a turn late, after they were already on the team screen.

    Gated on Team Support having resolved, which is what stops it firing on
    turn 1 while the whole roster is still seeded but unannounced."""
    if not st.get("team_support_done"):
        return []
    done = set(st.get("announced") or ())
    deck_size = int(st.get("deck_size") or 0)
    return [m for m in (st.get("members") or ())[deck_size:]
            if m.get("state") in (MEMBER_STATE_TEAM, MEMBER_STATE_SEMI)
            and m.get("target_id") not in done]


def scout_join_resolved(st: dict, event_id, announced_by_event: dict) -> None:
    """"The Word Spreads" resolved: every teammate it announced joins NOW.

    ALL of them, not just the one the story text names. The event introduces a
    single character by name ("Taiki Shuttle hears of our team's
    achievements...") but the panel behind it lists the whole intake, and the
    capture flips all four specials to member_state 1 together in the response
    that acknowledges it.

    The promotion itself is left to ensure_roster, which runs on the very next
    attach() -- i.e. the same response -- so there is exactly one place that
    decides who is on the team."""
    if (announced_by_event or {}).get(event_id) is None:
        return
    done = st.setdefault("announced", [])
    for member in unannounced_scouts(st):
        if member["target_id"] not in done:
            done.append(member["target_id"])
    st["scout_join_done"] = True


def active_members(st: dict) -> list:
    return [m for m in (st.get("members") or ())
            if m.get("state") == MEMBER_STATE_TEAM]


def member_by_target(st: dict, target_id) -> dict | None:
    for m in st.get("members") or ():
        if m.get("target_id") == target_id:
            return m
    return None


# ================================================================== lineup ==

_DISTANCE_APTITUDE = {1: "proper_distance_short", 2: "proper_distance_mile",
                      3: "proper_distance_middle", 4: "proper_distance_long",
                      5: "proper_distance_middle"}
_STYLE_APTITUDE = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
                   3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}


# THE ORDER THE FIFTEEN SEATS FILL, and it is the client's, not ours.
#
# ONE RUNNER PER GROUP FIRST, THEN GROUP BY GROUP TO THE BOTTOM. An empty group
# is a round the team does not contest at all, so every group gets its leader
# before anything else -- but after that the fill is DEPTH-first, packing group
# 1 to three runners before group 2 gets a second.
#
# The corpus settles it. team_data_array group sizes, by roster size:
#     11 -> [3, 3, 3, 1, 1]   13 -> [3, 3, 3, 3, 1]
#     14 -> [3, 3, 3, 3, 2]   15 -> [3, 3, 3, 3, 3]
# (bot/20260905_*, 20 team_race_start responses, no exceptions). Filling
# breadth-first instead -- every group's 2nd seat before any group's 3rd --
# spreads an 11-member roster as [3, 2, 2, 2, 2], which is a HOLE IN EVERY ONE
# OF THE FIVE ROUNDS: the race list draws a blank white card for each seat no
# member_id claims, so the player saw gaps scattered over the whole screen
# instead of two short rounds at the end (user-reported 2026-09-06, with a
# screenshot of how it is meant to look).
LINEUP_SLOT_ORDER = ([(d, 1) for d in (1, 2, 3, 4, 5)] +
                     [(d, m) for d in (1, 2, 3, 4, 5) for m in (2, 3)])

# A LINEUP THE SERVER BUILT IS REBUILT WHEN THIS CHANGES. lineup_of keeps every
# stored seat it can, which is right for seats the PLAYER chose and wrong for
# seats an older build filled: a lineup frozen in the breadth-first shape has
# all fifteen slots accounted for, so the reconcile finds nothing to fill and
# the holes never close. Same lesson as OPPONENT_ROLL_VERSION and
# PENDING_BLOCK_VERSION -- state the server writes and later serves back
# verbatim needs a stamp, or fixing the producer does nothing for careers that
# already stored its output. A lineup the player saved themselves (lineup_saved)
# is never discarded.
LINEUP_SHAPE_VERSION = 2

# The smallest legal lineup, and the one real actually serves for the first
# third of the run: 11 seats = the trainee plus the ten teammates the client's
# "you need 10 team members" check counts. Taking the first 11 of
# LINEUP_SLOT_ORDER gives group sizes [3, 3, 3, 1, 1], which is the captured
# 11-seat shape above exactly. lineup_of never auto-fills past this -- see the
# comment on its fill loop for why growth is a team_edit event, not a
# recruitment one.
MIN_LINEUP_SEATS = 11


def best_running_style(scout_id: int) -> int:
    # None for a pal/group card's character, who has no scout row: she gets the
    # neutral aptitude default rather than crashing the lineup builder, which
    # runs on every team race.
    row = (scout_pool().get(int(scout_id)) or {}) if scout_id is not None else {}
    return max(_STYLE_APTITUDE, key=lambda s: row.get(_STYLE_APTITUDE[s], 1))


def auto_lineup(st: dict) -> list:
    """Fill the 5 distance groups x 3 slots from the roster, best aptitude
    first.

    Seats fill in LINEUP_SLOT_ORDER: one runner per group first (an empty group
    is a round the team does not contest at all, and with a 12-member roster --
    the size between the first and second team races -- a pure group-by-group
    fill leaves group 5 empty and silently forfeits round 5), then depth-first
    down the groups, which is the shape every captured team_data_array has.

    The client can overwrite all of this via team_edit; this is what it sees
    until it does, and what a team race falls back to."""
    members = active_members(st)
    if not members:
        return []
    lineup = []
    used = set()
    for distance_type, member_id in LINEUP_SLOT_ORDER:
        column = _DISTANCE_APTITUDE[distance_type]
        pick = max(
            (m for m in members if m["chara_id"] not in used),
            key=lambda m: (scout_pool().get(m.get("scout_id")) or {}).get(column, 1),
            default=None)
        if pick is None:
            break
        used.add(pick["chara_id"])
        lineup.append({"distance_type": distance_type, "member_id": member_id,
                       "chara_id": pick["chara_id"],
                       "running_style": best_running_style(pick.get("scout_id"))})
    lineup.sort(key=lambda e: (e["distance_type"], e["member_id"]))
    return lineup


# distance_type -> the trainee's own aptitude column. Group 5 is a middle-
# distance round in every captured set, so it shares the column.
_TRAINEE_DISTANCE_APTITUDE = {
    1: "proper_distance_short", 2: "proper_distance_mile",
    3: "proper_distance_middle", 4: "proper_distance_long",
    5: "proper_distance_middle"}


def trainee_distance_group(chara_info) -> int:
    if not isinstance(chara_info, dict):
        return 3
    return max(_TRAINEE_DISTANCE_APTITUDE,
               key=lambda d: chara_info.get(_TRAINEE_DISTANCE_APTITUDE[d], 1))


def trainee_running_style(chara_info) -> int:
    if not isinstance(chara_info, dict):
        return 2
    return max(_STYLE_APTITUDE,
               key=lambda s: chara_info.get(_STYLE_APTITUDE[s], 1))


def discard_stale_pending(st: dict, chara_info=None) -> bool:
    """Throw away a simulated race left over from an older build.

    The race it described was never acknowledged (team_race_end would have
    cleared it), so nothing is lost by re-running it: the player lands back on
    opponent select and picks again. See PENDING_BLOCK_VERSION."""
    pending = st.get("pending")
    if not isinstance(pending, dict) or not pending:
        return False
    if int(pending.get("version") or 0) == PENDING_BLOCK_VERSION:
        return False
    log.warning("unity cup: discarding a stale race block (version %s, set %s, "
                "turn %s) -- re-racing from opponent select",
                pending.get("version"), pending.get("set_id"),
                pending.get("turn"))
    st["pending"] = None
    st["last_result"] = None
    st.pop("pending_award", None)
    if isinstance(chara_info, dict) and int(
            chara_info.get("playing_state") or 0) in (
                PLAYING_STATE_TEAM_RACE, PLAYING_STATE_TEAM_RACE_RUNNING,
                PLAYING_STATE_TEAM_RACE_RESULT):
        chara_info["playing_state"] = PLAYING_STATE_TEAM_RACE
    return True


def recover_stuck_screen(st: dict, chara_info, endpoint: str = "") -> bool:
    """On LOAD, refuse to resume into a team-race screen with nothing to show.

    playing_state 7/8/9 tells the client to reopen the team race, and it does
    that before asking whether there is a race to reopen:

        SingleModeChangeViewManager.ChangeViewTeamRaceRaceList
          -> SingleModeScenarioTeamRaceRaceListViewController.RegisterDownload
             =>  NullReferenceException

    With no pending block there is no lineup, no rounds and no result for that
    view to download, so the career cannot be opened at all -- every load walks
    straight back into the same dead screen (user-reported 2026-09-05, twice).
    A career parked there has already lost the race it was in the middle of, so
    put it back on an ordinary turn; unity_cup_missed_race re-offers the round.

    LOAD ONLY. Mid-session the gate event legitimately sets playing_state 7
    before team_race_start has produced a block, and resetting it there would
    close the screen the player just opened."""
    if endpoint.rsplit("/", 1)[-1] != "load":
        return False
    if not isinstance(chara_info, dict):
        return False
    if int(chara_info.get("playing_state") or 0) not in (
            PLAYING_STATE_TEAM_RACE, PLAYING_STATE_TEAM_RACE_RUNNING,
            PLAYING_STATE_TEAM_RACE_RESULT):
        return False
    if st.get("pending") or st.get("pending_award"):
        return False
    log.warning("unity cup: career resumed into the team-race screen with no "
                "race to show -- returning it to the turn")
    chara_info["playing_state"] = PLAYING_STATE_TURN_START
    st["opponents"] = None
    return True


def offers(st: dict) -> list:
    """The rolled opponent offers, or nothing if the roll is from an older
    build. THE ONLY WAY TO READ st["opponents"] -- see OPPONENT_ROLL_VERSION;
    reading the dict directly is how a bad roll kept reaching the client after
    the code that produced it was fixed."""
    cached = st.get("opponents")
    if not isinstance(cached, dict):
        return []
    if int(cached.get("version") or 0) != OPPONENT_ROLL_VERSION:
        return []
    return cached.get("list") or []


def lineup_of(st: dict, chara_info=None) -> list:
    """The stored lineup, RECONCILED WITH THE CURRENT ROSTER.

    THE TRAINEE IS ONE OF THE ELEVEN. The capture's team_data_array has 11
    seats for a ten-teammate roster, and the extra one is the player's own uma
    (chara 1068 at distance_type 3, member_id 1) -- she races for this team
    herself. Leaving her out of team_data_array while still entering her in the
    race gave the client a RunRaceDeckData with no matching TeamMember, and it
    NullRefs building the victory cut-in:

        SingleModeScenarioTeamRaceUtils.GetTeamRaceResultCutinWinTypePattern
          (Int32 memberCount, Int32[] cardIdArray, Int32[] charaIdArray, ...)
        SingleModeScenarioTeamRaceRaceListViewController.RegisterDownloadRaceSkipCutin

    (user-reported 2026-09-05: the race list softlocked right after picking an
    opponent). She is also the eleventh head the "you need 10 team members"
    check counts, which is what makes a nine-teammate roster -- one whose deck
    holds a pal card, seating nobody -- still a legal team.

    A lineup is not a one-time computation. The roster grows all career long --
    six on turn 3, ten once the specials join, then one scouting wave per team
    race -- and this used to cache whatever it first produced and never look
    again. The client counts the seats in team_data_array, so a lineup frozen
    at six left the race button greyed out with "you need 10 team members" even
    though the roster had them; opening team edit and pressing save with no
    changes fixed it, because that submitted the full lineup (user-reported).

    So: keep every seat the player chose, drop anyone no longer on the roster,
    and fill the empty seats with whoever is unseated -- in LINEUP_SLOT_ORDER,
    the same rule auto_lineup uses."""
    members = active_members(st)
    if not members:
        return st.get("lineup") or []
    if (not st.get("lineup_saved")
            and int(st.get("lineup_version") or 0) != LINEUP_SHAPE_VERSION):
        st.pop("lineup", None)
    st["lineup_version"] = LINEUP_SHAPE_VERSION
    trainee = trainee_chara(chara_info)
    roster = {m["chara_id"]: m for m in members}
    if trainee:
        roster[trainee] = None      # a seat, but not a scoutable teammate

    kept, taken, seated = [], set(), set()
    stored = list(st.get("lineup") or ())

    # HER SEAT IS RESERVED BEFORE ANYONE ELSE IS RECONCILED. The trainee goes
    # in first, in the group her own aptitude suits: she is the team ace, and a
    # seat taken by aptitude is one the player would have chosen anyway.
    #
    # This used to run AFTER the stored lineup was folded back in, and took
    # whatever slot happened to be spare. That is fine early on, when the deck
    # is bigger than the roster -- but the deck caps at fifteen and the roster
    # keeps growing past it, and from the moment fifteen teammates fill every
    # slot there is no spare left and the trainee is silently dropped. She then
    # stays dropped: the reconcile keeps all fifteen stored seats on every
    # later response, so nothing ever gives her one back.
    #
    # The corpus does not have a single response shaped like that -- the
    # trainee is one of the seats in 972 of 972 -- and the client will not race
    # a deck she is missing from: it decides the deck is invalid and runs its
    # own TeamAutoBuild on the team-race screen, which is where the round-5
    # NullReferenceException came from (user-reported 2026-09-06).
    #
    # A lineup the player saved WITH her in it is left exactly as they built
    # it; this only reserves a seat nobody assigned her.
    if trainee and not any(int(e.get("chara_id") or 0) == trainee
                           for e in stored):
        slot = (trainee_distance_group(chara_info), 1)
        taken.add(slot)
        seated.add(trainee)
        kept.append({"distance_type": slot[0], "member_id": slot[1],
                     "chara_id": trainee,
                     "running_style": trainee_running_style(chara_info)})

    for entry in stored:
        chara_id = int(entry.get("chara_id") or 0)
        slot = (int(entry.get("distance_type") or 0),
                int(entry.get("member_id") or 0))
        if chara_id not in roster or chara_id in seated or slot in taken:
            # (roster carries the trainee too -- see above)
            continue
        kept.append(entry)
        taken.add(slot)
        seated.add(chara_id)

    # HOW MANY SEATS THE AUTO-FILL MAY ADD. Filling every slot in
    # LINEUP_SLOT_ORDER grows the lineup the instant the roster does, and real
    # does not: measured over the three 20260905 sessions, team_data_array is
    # 11 seats from the turn-24 team_edit through turn 35, 13 from turn 36 and
    # 15 from turn 48 -- while the roster behind it has already gone 10 -> 12
    # after race 1 (see ROSTER_GROWTH_AFTER_RACE). So a recruit does NOT take a
    # race seat when they join; the lineup only grows when the player next
    # opens team edit and saves one. We were showing 13 seats from turn 25.
    #
    # Topping up to MIN_LINEUP_SEATS is still required and is why the fill
    # exists at all: the client greys the race button out with "you need 10
    # team members" below that, which is the frozen-at-six bug in this
    # function's own docstring. So fill to the minimum, never past it -- a
    # player-saved lineup is already in `kept` above and sets its own size.
    target = max(MIN_LINEUP_SEATS, len(kept))
    for distance_type, member_id in LINEUP_SLOT_ORDER:
        if len(kept) >= target:
            break
        if (distance_type, member_id) in taken:
            continue
        column = _DISTANCE_APTITUDE[distance_type]
        pick = max((m for m in members if m["chara_id"] not in seated),
                   key=lambda m: (scout_pool().get(m.get("scout_id"))
                                  or {}).get(column, 1),
                   default=None)
        if pick is None:
            break
        seated.add(pick["chara_id"])
        taken.add((distance_type, member_id))
        kept.append({"distance_type": distance_type,
                     "member_id": member_id,
                     "chara_id": pick["chara_id"],
                     "running_style": best_running_style(pick.get("scout_id"))})

    kept.sort(key=lambda e: (e["distance_type"], e["member_id"]))
    st["lineup"] = kept
    return kept


# ================================================================== progress ==

# scenario_progress as the capture reports it: 0 from the start, 1 from turn
# 19, 2 from turn 30, 3 from turn 50. Observed, not derived -- it does not line
# up with single_mode_aoharu_schedule's notice turns, and one run is not enough
# to tell what it actually keys on. Reproduces the capture exactly.
PROGRESS_TURNS = ((50, 3), (30, 2), (19, 1))


def scenario_progress(turn) -> int:
    for threshold, value in PROGRESS_TURNS:
        if (turn or 0) >= threshold:
            return value
    return 0


def team_title(st: dict) -> int:
    """1 before the first team race, then +1 per race completed (capture: 1,
    then 2 from turn 24, 3 from 36, 4 from 48, 5 from 60, 6 from 72)."""
    return 1 + len(st.get("races") or ())


# The team title's displayed NAME, text_data category 193 keyed by the title id
# team_title() returns: 1 On the Brink ... 6 Superstardom. Six rows, and the
# ladder tops out at 6 (five team races), so "Superstardom" is the last rung
# and is reachable -- which is what epithet 154 asks for by name.
TEAM_TITLE_TEXT_CATEGORY = 193


def team_title_name(title_id: int) -> str:
    """The title's display name, or "" for an id master.mdb has no row for."""
    row = master_data.query_one(
        'SELECT text FROM text_data WHERE category=? AND "index"=?',
        (TEAM_TITLE_TEXT_CATEGORY, int(title_id or 0)))
    return (row["text"] if row else "") or ""


# ==================================================================== state ==

def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = new_state()
        full_state[STATE_KEY] = st
    return st


def new_state() -> dict:
    return {"members": [], "lineup": [], "team_rank": 30, "team_name_id": 0,
            "races": [], "guide_count": 0, "power": 1, "rank_state": 0,
            "seed": random.randrange(1 << 30), "pending": None,
            "opponents": None, "last_result": None,
            "bursts": 0, "extreme_bursts": 0, "beat_elite": False,
            "unity_trainings": 0, "unity_training_max_chars": 0}


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


def trainee_chara(chara_info) -> int:
    """The player's own chara_id, from her card_id (106801 -> 1068)."""
    if not isinstance(chara_info, dict):
        return 0
    return int(chara_info.get("card_id") or 0) // 100


def is_active(chara_info) -> bool:
    return isinstance(chara_info, dict) and \
        int(chara_info.get("scenario_id") or 0) == SCENARIO_ID


# ============================================================ the wire shape ==

def _team_info(st: dict, command_info_array=None, chara_info=None) -> dict:
    members = active_members(st)
    turn = chara_info.get("turn") if isinstance(chara_info, dict) else None
    # ACTIVE only. A SemiMember has stats but is not on the team yet, and the
    # capture holds every rank at 1 through turns 1-2 while all ten sit at
    # member_state 2 -- speed_rank only becomes 2 at 0135, the response where
    # the deck six flip to member_state 1.
    ranks = ratcheted_ranks(st)
    return {
        "team_name_id": int(st.get("team_name_id") or 0),
        "speed_rank": ranks["speed"], "stamina_rank": ranks["stamina"],
        "power_rank": ranks["power"], "guts_rank": ranks["guts"],
        "wiz_rank": ranks["wiz"],
        # LAZY on purpose -- see refresh_power().
        "team_power": int(st.get("power") or 1),
        "team_rank": int(st.get("team_rank") or 30),
        "team_rank_state": int(st.get("rank_state") or 0),
        "team_title": team_title(st),
        "guide_partner_count": int(st.get("guide_count") or 0),
                # scout_enabled is set by "What's the Unity Cup?" resolving. The
        # `or members` is a rescue for careers saved before that flag existed:
        # anyone with a joined member has self-evidently seen the intro. It
        # cannot change capture behaviour -- at 0134 the flag is already True
        # while the roster is still all SemiMembers.
        "is_scout_enable": bool(st.get("scout_enabled") or members),
        "team_chara_info_array": [
            # THE INTERNAL target_id, which for a deck member is their DECK
            # POSITION (1-6) and for everyone else is their chara_id.
            #
            # This was briefly changed to m["chara_id"] on the theory that
            # SingleModeTeamCharaInfo (dump.cs 767315) has no chara_id field
            # and so must carry the chara identity itself. It does lack that
            # field -- but the identity is not missing, it lives in the
            # PAIRING: TeamEvaluationInfo (dump.cs 768263) carries BOTH
            # target_id and chara_id, and the client joins the two arrays on
            # target_id to resolve a member. The real server proves it --
            # capture 0244_single_mode_team_load has
            #     training_partner_id: [1, 2, 3, 4, 5, 6, 1010, 1019, ...]
            #     evaluation: (1, 1051) (2, 1043) ... (1010, 1010) ...
            # i.e. positional ids here, chara ids only in the evaluation rows.
            # Sending chara_ids here makes both arrays key on the same value
            # and the deck's positional link is lost.
            #
            # The get_SupportCardId() NRE that change was aimed at had a
            # different cause: chara_info.evaluation_info_array was missing
            # every non-deck row, so the client had nothing to resolve a
            # scouted teammate against. See _sync_chara_evaluation.
            {"training_partner_id": m["target_id"],
             "speed": m["speed"], "stamina": m["stamina"], "power": m["power"],
             "wiz": m["wiz"], "guts": m["guts"],
             "speed_limit": m["speed_limit"], "stamina_limit": m["stamina_limit"],
             "power_limit": m["power_limit"], "wiz_limit": m["wiz_limit"],
             "guts_limit": m["guts_limit"],
             "speed_limit_base": m["speed_limit_base"],
             "stamina_limit_base": m["stamina_limit_base"],
             "power_limit_base": m["power_limit_base"],
             "wiz_limit_base": m["wiz_limit_base"],
             "guts_limit_base": m["guts_limit_base"],
             "rank_score": rank_score(m)}
            for m in members],
        "team_data_array": (lineup_of(st, chara_info)
                            if members and lineup_served(st, turn) else []),
        # TeamEditFlag.On unless the player toggled it off via
        # save_team_edit_flag (the capture never toggles, so it is On there).
        "team_edit_flag": int(st.get("team_edit_flag") or TEAM_EDIT_ON),
    }


def lineup_served(st: dict, turn) -> bool:
    """Whether team_data_array goes on the wire AT ALL this response.

    IT IS EMPTY UNTIL THE FIRST TEAM RACE, and that is not cosmetic. The
    capture is unambiguous: 81 consecutive responses carry [] and the array
    only becomes 11 seats at the team_edit of turn 24 -- the first moment the
    player's client has built a deck -- and is never empty again.

    Serving a lineup before then is what softlocked the career on LOAD
    (user-reported 2026-09-06, turn 12, client Player.log):

        NullReferenceException
          WorkSingleModeScenarioTeamRace+TeamMember.get_SupportCardId ()
          TrainingParamChangeSupportMemberA2U.RegisterDownload (...)
          TrainingParamChangeUI.RegistDownload (...)
          StoryViewController.RegistSingleModeResources (..., Int32 storyId)
          SingleModeMainViewController.RegisterDownload (...)
          SingleModeChangeViewManager+<SendSingleModeLoadRequest>d__61.MoveNext

    ApplyDeckDataList resolves every seat through GetTeamMemberByCharaId, and
    on the main view -- before the team-race context is built -- the seats do
    not all resolve; the nulls then blow up the first static RegisterDownload
    that walks the deck list. Every other field of our team_data_set matches
    the capture key for key at that turn; this array was the only difference.

    After the first race turn the array is required: it is what the race
    screen counts to decide the team is full, and a missing one greys the
    race button out with "you need 10 team members" (user-reported
    2026-09-05). A lineup the PLAYER saved is served from that moment on,
    whenever it was saved."""
    if st.get("lineup_saved"):
        return True
    return turn is not None and int(turn) >= TEAM_RACE_TURNS[0]


def _evaluation_info_array(st: dict, chara_info=None) -> list:
    """One row per teammate, per OCCUPIED DECK SLOT, and per support NPC.

    The NPC rows carry member_state 0 and a soul that never moves -- they are
    not teammates, they just share the array.

    THE TWO EVALUATION ARRAYS MUST CARRY THE SAME TARGETS. team_data_set's and
    chara_info's are checked against each other by the client, and in 1140
    captured responses the two target sets are identical -- 1140 of 1140, never
    once off by a single row.

    A DECK SLOT HOLDING A PAL CARD IS EXACTLY WHERE THAT BROKE. The slot has a
    bond gauge, so the shared career code gives it a chara_info row (target 6,
    the deck position); it seats no teammate, so this array had nothing at
    target 6. The client resolves each chara_info row through this one, got
    null for that slot, and read straight through it:

        NullReferenceException
          WorkSingleModeScenarioTeamRace+TeamMember.get_SupportCardId ()
          TrainingParamChangeSupportMemberA2U.RegisterDownload (...)
          TrainingParamChangeUI.RegistDownload (...)
          StoryViewController.RegistSingleModeResources (..., Int32 storyId)
          SingleModeMainViewController.RegisterDownload (...)
          SingleModeChangeViewManager+<SendSingleModeLoadRequest>d__61

    -- from the MAIN VIEW's download registration, so the career would not open
    at all (user-reported 2026-09-06, four times, on two different careers that
    both carried a pal card).

    So a pal slot gets its row here too, at member_state 0: present, resolvable,
    and not a teammate. She then does NOT also get her separate support-NPC row
    -- 9008 cannot be both target 6 and target 108, the same "2 Light Hellos"
    duplicate the on_team guard below exists to prevent."""
    rows = [{"target_id": m["target_id"], "chara_id": m["chara_id"],
             "member_state": m.get("state", MEMBER_STATE_TEAM),
             "soul_threshold_id": soul_of(m),
             "soul_event_state": burst_state(m)}
            for m in (st.get("members") or ())]
    seated = {r["target_id"] for r in rows}
    # Deck slots that seat nobody -- a pal or group card. They are support
    # partners with a bond, so chara_info has them; they belong here too.
    in_deck = set()
    for position, scout_id, card_id in deck_scout_rows(chara_info):
        if position in seated or not position:
            continue
        chara_id = support_card_chara(card_id)
        if not chara_id:
            continue
        in_deck.add(chara_id)
        rows.append({"target_id": position, "chara_id": chara_id,
                     "member_state": MEMBER_STATE_NONE,
                     "soul_threshold_id": 1, "soul_event_state": SOUL_NONE})
    # A pal character who is ON the team -- or standing in the deck -- must not
    # ALSO appear as the cardless support NPC she is without her card; that is
    # the "2 Light Hellos" shape of bug the shared code already guards against
    # elsewhere.
    on_team = {m["chara_id"] for m in (st.get("members") or ())} | in_deck
    rows += [{"target_id": tid, "chara_id": cid,
              "member_state": MEMBER_STATE_NONE,
              "soul_threshold_id": 1, "soul_event_state": SOUL_NONE}
             for tid, cid in SUPPORT_NPC_ROWS if cid not in on_team]
    rows.sort(key=lambda r: r["target_id"])
    return rows


# final_win_type IS SERVED ONLY WHILE THE RACE IS ON SCREEN. Capture
# 0223-0236, one round of the Unity Cup, is unambiguous:
#
#     team_race_start   final_win_type 1   (with race_result_array)
#     team_race_end     final_win_type 1   (race_result_array now null)
#     team_race_out     final_win_type null
#     everything after  final_win_type null
#
# We fell back to st["last_result"], which is set at team_race_end and never
# cleared, so every response for the rest of the career kept announcing a
# finished race. The client reads that as "a team race result is still
# outstanding" and parks on the Unity Cup standings screen -- the softlock the
# player hit twice (2026-09-06), with the round drawn UNRANKED because the
# result it is waiting on has not been acknowledged.
_WIN_TYPE_ENDPOINTS = ("/team_race_start", "/team_race_end",
                       "/team_race_continue")


# opponent_info_array is an OPPONENT-SCREEN field, and which opponents it
# carries depends on the endpoint. Measured over 1317 real team_data_set
# records in the three captures/bot/20260905_*_icarus sessions:
#
#     opponent_list      the board -- 3 offers (13x), 1 (4x), 4 (3x)
#     team_race_start    exactly ONE, the offer the player picked (20/20;
#                        its team_race_set_id always equals the request's)
#     everything else    null (1277 records, no exceptions)
#
# We served offers(st) on every endpoint, so the whole board leaked into every
# training response for as long as a roll was cached, and team_race_start
# announced three opponents for a race against one. An earlier reading of this
# recorded a single blended distribution ("real {1:18, 3:10, 4:2}") and missed
# that the two endpoints disagree by construction.
#
# team_race_continue is NOT in the corpus (the capture bot never retried a
# round), so it is an inference: it re-enters the same race screen against the
# same opponent, and this file's own history is full of NullRefs caused by
# withholding a field that screen reads. Grouped with team_race_start for that
# reason -- an extra opponent the client ignores is the cheaper mistake.
_OPPONENT_LIST_ENDPOINTS = ("/opponent_list",)
_OPPONENT_ONE_ENDPOINTS = ("/team_race_start", "/team_race_continue")


def _opponent_info_array(st: dict, endpoint: str, pending: dict):
    if endpoint.endswith(_OPPONENT_LIST_ENDPOINTS):
        return offers(st) or None
    if endpoint.endswith(_OPPONENT_ONE_ENDPOINTS):
        set_id = int(pending.get("set_id") or 0)
        chosen = [o for o in offers(st)
                  if int(o.get("team_race_set_id") or 0) == set_id]
        return chosen or None
    return None


def build_team_data_set(full_state: dict, chara_info: dict,
                        command_info_array=None, endpoint: str = "") -> dict:
    st = state(full_state)
    turn = chara_info.get("turn") if isinstance(chara_info, dict) else None
    pending = st.get("pending") or {}
    return {
        "team_info": _team_info(st, command_info_array, chara_info),
        "command_info_array": command_info_array or [],
        "evaluation_info_array": _evaluation_info_array(st, chara_info),
        "scenario_progress": scenario_progress(turn),
        "frame_order_info_array": pending.get("frame_order_info_array"),
        "race_result_array": pending.get("race_result_array"),
        "final_win_type": (pending.get("final_win_type")
                           or (st.get("last_result")
                               if endpoint.endswith(_WIN_TYPE_ENDPOINTS) else None)),
        "opponent_info_array": _opponent_info_array(st, endpoint, pending),
        "event_effect_info": st.pop("event_effect_info", None),
        "not_up_team_parameter_info": {"status_array": _take_capped(st)},
        "team_race_history_array": list(st.get("races") or ()),
        "command_result": _command_result(st, endpoint),
    }


# command_result is an EXEC_COMMAND-ONLY field, and on exec_command it is
# always present. Real Unity Cup captures (one full 78-turn career, 192
# exec_command responses) carry the three-key envelope on every single one --
# usually with all three arrays null, carrying a payload on the turns a hint
# actually landed -- and null on every other endpoint (check_event, load,
# race_*, team_race_*, gain_skills, start: 0 objects between them). Serving
# null on exec_command, as this did whenever no soul burst fired, is what left
# the "you got a hint!" popup unable to render at all.
_COMMAND_RESULT_ENDPOINTS = ("exec_command",)
_EMPTY_COMMAND_RESULT = {"skill_tips_array": None, "soul_skill_tips_array": None,
                         "sp_soul_skill_tips_array": None}


def _command_result(st: dict, endpoint: str):
    if not (endpoint or "").endswith(_COMMAND_RESULT_ENDPOINTS):
        # Leave a banked result alone: it belongs to the exec_command response
        # that is about to be built, and popping it here would drop the hint.
        return None
    banked = st.pop("command_result", None)
    return banked if isinstance(banked, dict) else dict(_EMPTY_COMMAND_RESULT)


def _sync_chara_evaluation(data: dict, st: dict) -> None:
    """Put the team back into chara_info.evaluation_info_array.

    THE CLIENT RESOLVES A TEAMMATE THROUGH THIS ARRAY. Its rows are the bond
    shape (target_id / training_partner_id / evaluation / is_outing /
    story_step / is_appear / group_outing_info_array), NOT team_data_set's soul
    shape, and the capture carries 15 of them where the shared career code
    leaves only 6: the five scenario NPCs and every non-deck teammate are
    dropped by single_mode_events._reconcile_npc_appearance, whose
    _REAL_NPC_TARGETS filter ({102, 103, 2001}) strips any target_id >= 100 it
    does not recognise. That filter is right for URA -- it exists to stop junk
    facility-marker rows being announced as nameless partners -- so rather than
    widen it for everyone, this restores what Unity Cup needs afterwards.
    attach() is the last thing to touch a response, so this wins.

    Without these rows the client has nothing to resolve a scouted teammate
    against: it draws the generic portrait with no name, and the moment one
    appears in a training it throws NullReferenceException in
    WorkSingleModeScenarioTeamRace.TeamMember.get_SupportCardId (via
    TrainingParamChangeSupportMemberA2U.RegisterDownload) and the career
    softlocks -- the same crash the deck-position fix cured for the deck six,
    with the same cause for everybody else.

    A non-deck row's target_id IS the chara_id, which is how the client looks
    the scout row up; the deck six keep their positional 1..6 target and are
    resolved through the deck instead.

    is_appear FOLLOWS THE MEMBER'S STATE: 1 once she has actually JOINED
    (member_state 1), 0 while she is still a SemiMember waiting for the event
    that introduces her. Both halves are capture-proven, in the same run:

        0128-0138 (turns 1-3)  1010/1030/1052/1056 -> is_appear 0
        0139      (turn 4)     all four            -> is_appear 1

    and 0139 is the response that acknowledges "The Word Spreads", the very
    same response where team_data_set flips them member_state 2 -> 1. Capture
    0244 then has is_appear 1 for all six scouted teammates (1010, 1019, 1030,
    1052, 1054, 1056); 0 stays reserved for support NPCs who have not unlocked
    (101, 104, 108).

    Flat 1 here -- the previous behaviour -- announced the four specials on
    TURN 1. is_appear is what the client watches to say "X will now appear in
    training", so serving the row already flipped, on the debut event, spoiled
    all four joins at once and then never played their actual join (user-
    reported 2026-09-06, screenshot of the debut cutscene listing Taiki
    Shuttle, Rice Shower, Haru Urara and Matikanefukukitaru). It is the same
    failure single_mode_events._reconcile_npc_appearance was written for: a
    sticky flag flipped at the wrong moment is announced wherever the player
    happens to be standing.

    A SemiMember is never placed in a facility (scout_placements filters on
    active_members), so 0 here cannot strand a drawn portrait -- the crash the
    flat 1 was covering for only involves teammates who have joined.

    The flag was harmless for as long as scouted teammates never appeared in
    home_info's training_partner_array. The moment they do -- which is what
    makes their portraits show up in a facility at all -- the client tries to
    resolve one flagged "not appearing", gets null out of
    WorkSingleModeScenarioTeamRace.TeamMember.get_SupportCardId, and throws
    NullReferenceException from TrainingParamChangeSupportMemberA2U
    .RegisterDownload before the career view can even open (user-reported
    2026-09-05, client Player.log).

    `evaluation` stays 0 on those rows forever: a scouted teammate owns no
    support card, so she has no bond gauge -- exactly the "cardless supporter
    was given one" bug _ensure_supporter_eval_rows already documents. The
    capture agrees, 0 for all six at turn 27.

    Both fields are REASSERTED on rows that already exist, not just on rows
    created here, so a career saved before this fix repairs itself on its next
    response instead of staying uncrashable-only-by-luck."""
    ci = data.get("chara_info")
    if not isinstance(ci, dict):
        return
    rows = list(ci.get("evaluation_info_array") or [])
    have = {r.get("target_id") for r in rows if isinstance(r, dict)}
    deck_targets = {m["target_id"] for m in (st.get("members") or ())
                    if m["target_id"] != m["chara_id"]}

    def row(target_id):
        return {"target_id": target_id, "training_partner_id": target_id,
                "evaluation": 0, "is_outing": 0, "story_step": 0,
                "is_appear": 0, "group_outing_info_array": []}

    # ONE SOURCE OF TRUTH FOR WHO IS ON THE WIRE. The target set here is the
    # team_data_set array's, verbatim -- the client checks the two against each
    # other and the capture has them identical in 1140 of 1140 responses. Every
    # target that array carries needs a row here, and nothing else may be here:
    # a row on one side with no partner on the other is the null that softlocks
    # the load (see _evaluation_info_array for the stack).
    legal = [r["target_id"] for r in _evaluation_info_array(st, ci)]
    for _tid in legal:
        if _tid not in have:
            rows.append(row(_tid))
            have.add(_tid)
    appear = {}
    # A JOINED MEMBER IS NEVER SERVED is_appear 0. Across the corpus the two
    # arrays agree 19132 times out of 19132: every member_state 1 row has
    # is_appear 1, without a single exception. (member_state 2 is genuinely
    # ambiguous -- both 0 and 1 occur -- so a SemiMember is left alone.)
    #
    # A DECK SLOT normally gets that 1 from the shared bond code and so is left
    # out of `appear` entirely, which is what keeps its real evaluation gauge
    # off the zeroing loop below. The exception is a slot the bond code does
    # not treat as a partner -- a pal or group card in the friend slot -- whose
    # row stays is_appear 0 while the member sitting on it is fully joined.
    # The client resolves that row, is told she is not appearing, and reads
    # through the null:
    #
    #     NullReferenceException
    #       SingleModeScenarioTeamRaceUtils.GetDistanceProperRate (TeamMember, Int32)
    #       SingleModeScenarioTeamRaceUtils.GetTeamMemberRankScore (...)
    #       SingleModeScenarioTeamRaceDeckBuilder.RunTeamBuild / Build / AutoBuild
    #       SingleModeScenarioTeamRaceUtils.TeamAutoBuild (...)
    #       SingleModeScenarioTeamRaceTopViewController+<InitializeView>d__6
    #
    # -- the team-race screen dies in its own auto-build the moment it opens
    # (user-reported 2026-09-06, entering Unity Cup round 5; client Player.log).
    # So a joined deck member has the flag forced up, and nothing else about
    # her row is touched.
    force_appear = set()
    for member in st.get("members") or ():
        tid = member["target_id"]
        joined = member.get("state") == MEMBER_STATE_TEAM
        if tid in deck_targets:
            if joined:
                force_appear.add(tid)
            continue
        appear[tid] = 1 if joined else 0
        if tid not in have:
            rows.append(row(tid))
            have.add(tid)
    # Reassert on every scouted row, however it got here -- a career saved
    # while the flag was flat 1 heals on its next response.
    for r in rows:
        if isinstance(r, dict) and r.get("target_id") in appear:
            r["is_appear"] = appear[r["target_id"]]
            r["evaluation"] = 0
        elif isinstance(r, dict) and r.get("target_id") in force_appear:
            r["is_appear"] = 1
    # ...and URA's own NPCs go, for the same reason. The shared career code
    # mints rows for its partner ids on every response -- 102 and 2001 (Happy
    # Meek) come from single_mode_events._reconcile_npc_appearance and
    # apply_versus_state -- but no Unity Cup response has ever carried them:
    # across 1140 captured responses the only targets that appear are the six
    # deck positions, this scenario's support NPCs and the teammates' chara
    # ids. attach() runs last, so this is where they come back off.
    keep = set(legal)
    rows = [r for r in rows
            if isinstance(r, dict) and r.get("target_id") in keep]
    rows.sort(key=lambda r: r.get("target_id") or 0)
    ci["evaluation_info_array"] = rows
    # ...and Riko stays off the screen until her own beat. Last, so it wins
    # over anything the rebuild above reasserted.
    apply_riko_gate(st, ci)


def _mirror_load_common(data: dict) -> None:
    """single_mode_load_common carries its OWN copy of chara_info and
    home_info, and on a load THAT is the copy the client builds from.

    In the real response the two are identical. Capture 0128 (a
    single_mode_team/load) serves the same 15 evaluation rows in both, and the
    same training_partner_array in both. Ours did not: the scenario patch runs
    on data["chara_info"], and single_mode_load_common.chara_info is a
    different dict, so it went out with only the six deck rows -- no scenario
    NPCs, none of the four teammates.

    That is the whole load softlock. WorkSingleModeData.ApplySingleModeLoadResponse
    reads this block, so the client built its partner state from six rows while
    team_data_set described fourteen; the members with no row on that side came
    back null and it read straight through one:

        NullReferenceException
          WorkSingleModeScenarioTeamRace+TeamMember.get_SupportCardId ()
          TrainingParamChangeSupportMemberA2U.RegisterDownload (...)
          TrainingParamChangeUI.RegistDownload (...)
          StoryViewController.RegistSingleModeResources (..., Int32 storyId)
          SingleModeMainViewController.RegisterDownload (...)
          SingleModeChangeViewManager+<SendSingleModeLoadRequest>d__61

    LOAD ONLY, which is exactly the shape of the bug: every mid-session
    response carries no single_mode_load_common at all, so every one of them
    was correct and only entering the career failed (user-reported 2026-09-06,
    five times across two careers)."""
    block = data.get("single_mode_load_common")
    if not isinstance(block, dict):
        return
    for key in ("chara_info", "home_info"):
        if isinstance(data.get(key), dict) and key in block:
            block[key] = data[key]


# TeamParameterRank (stat_rank's 1..8) -> training facility level. GameTora,
# "Training Levels": F/G -> 1, D/E -> 2, B/C -> 3, A -> 4, S -> 5.
#
# UNITY CUP DOES NOT COUNT TRAININGS. URA levels a facility every four trains
# in it (single_mode_team._record_facility_train); here the level is a pure
# function of the team's rank IN THAT STAT, so strengthening the team is what
# strengthens the facility. Leaving URA's counter in charge meant a team that
# had raced its way to rank A still trained at level 1 until the player had
# ground four sessions out of the facility.
RANK_TRAINING_LEVEL = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 5}


def facility_levels(st: dict) -> dict:
    """{str(base command_id): level}, from the team's five stat ranks."""
    ranks = ratcheted_ranks(st)
    return {str(cmd): RANK_TRAINING_LEVEL.get(int(ranks.get(stat) or 1), 1)
            for cmd, stat in COMMAND_STAT.items()}


# The levels the player has actually been SHOWN. facility_levels() is what the
# team has earned; this is what has been announced by a "Growing as a Team"
# cutscene and is therefore safe to serve. See facility_level_up_pending.
FACILITY_SHOWN_KEY = "facility_shown"


def facility_levels_shown(st: dict) -> dict:
    """The announced levels -- what the wire and the training formula use.

    Seeded from the earned levels the first time it is asked for, so a career
    started before the cutscene existed does not suddenly owe thirteen of them.
    """
    shown = st.get(FACILITY_SHOWN_KEY)
    earned = facility_levels(st)
    if not isinstance(shown, dict) or not shown:
        shown = dict(earned)
        st[FACILITY_SHOWN_KEY] = shown
    # A facility can only ever be BEHIND: keys the map has never seen (a new
    # command id) come in at whatever the team has already earned.
    for cmd, level in earned.items():
        shown.setdefault(cmd, level)
    return {k: int(v) for k, v in shown.items()}


def facility_level_up_pending(st: dict) -> dict:
    """{command_id(str): new level} for every facility the team has earned a
    level in but has not been shown yet, or {} when nothing is owed."""
    shown = facility_levels_shown(st)
    return {cmd: lvl for cmd, lvl in facility_levels(st).items()
            if lvl > int(shown.get(cmd) or 0)}


def facility_level_up_resolved(st: dict) -> None:
    """The cutscene has been acknowledged -- the levels it announced are now
    the levels the client sees. The capture bumps them on the response AFTER
    the event, never on the response that carries it."""
    st[FACILITY_SHOWN_KEY] = dict(facility_levels(st))


def _sync_facility_levels(full_state: dict, chara_info: dict, st: dict,
                          endpoint: str = "") -> None:
    """Push those levels onto the career and into the response.

    Two places have to agree: career_data["facility_levels"], which
    training_formula scales the gains by, and chara_info's
    training_level_info_array, which is what the client actually PRINTS on the
    facility button.

    ANNOUNCED levels, not earned ones -- the level-up rides on the "Growing as
    a Team" cutscene (see unity_cup_facility_level), and serving the new number
    before the cutscene plays would make the animation announce a level the
    button already showed."""
    levels = facility_levels_shown(st)
    from ...handlers import single_mode_team as smt
    career = (full_state or {}).get(smt.STATE_KEY)
    if isinstance(career, dict) and isinstance(career.get("data"), dict):
        career["data"][smt.FACILITY_LEVELS_KEY] = dict(levels)
    # THE CUTSCENE IS "GROWING AS A TEAM", not URA's Director beat: Unity Cup
    # has no four-training counter and no Director, and 201121 tracks the
    # level-ups exactly -- thirteen servings, thirteen level-ups, each landing
    # on the response right after its event resolves (captures 20260905_152744
    # and 20260905_183334, 1:1 with no exceptions). See unity_cup_facility_level.
    array = chara_info.setdefault("training_level_info_array", [])
    by_cmd = {e.get("command_id"): e for e in array if isinstance(e, dict)}
    for cmd in TRAINING_COMMAND_IDS:
        level = levels[str(cmd)]
        if cmd in by_cmd:
            by_cmd[cmd]["level"] = level
        else:
            array.append({"command_id": cmd, "level": level})


def attach(response: dict, full_state: dict, chara_info: dict,
           command_info_array=None, endpoint: str = "") -> dict:
    """Swap URA's ura_data_set for Unity Cup's team_data_set.

    Reached as the scenario's attach hook from single_mode_team's one response
    chokepoint, so no handler knows this scenario exists."""
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    st = state(full_state)
    turn = chara_info.get("turn")
    # stat_ranks counts the trainee as a team member (she races for the team),
    # so her current stats have to be reachable from st -- refresh_power is
    # called from endpoints.py with no chara_info in scope.
    st["trainee_stats"] = {s: int(chara_info.get(s) or 0) for s in STATS}
    ensure_roster(st, turn, chara_info)
    # THE LAZY RECOMPUTE. team_power is refreshed once per turn, not on every
    # response -- see refresh_power() for why that lag is correct.
    if st.get("power_turn") != turn:
        st["power_turn"] = turn
        refresh_power(st)
    _sync_facility_levels(full_state, chara_info, st, endpoint)
    data.pop("ura_data_set", None)
    data.pop("live_data_set", None)
    data["team_data_set"] = build_team_data_set(full_state, chara_info,
                                                command_info_array,
                                                endpoint=endpoint)
    _sync_chara_evaluation(data, st)
    # The team-race pay-out's "<stat> is in superb form" notices, claimed once.
    # settle() runs on this same response, just before attach.
    owed = st.pop(SETTLE_NOT_UP_KEY, None)
    if owed:
        info = data.setdefault("not_up_parameter_info", {})
        info["status_type_array"] = sorted(
            set(info.get("status_type_array") or ()) | set(owed))
    _mirror_load_common(data)
    if endpoint.endswith("/team_race_out"):
        # The rank the client animates TO. Still pending at this point -- see
        # bank_race_award.
        data["tmp_team_rank"] = pending_rank(st)
    return response
