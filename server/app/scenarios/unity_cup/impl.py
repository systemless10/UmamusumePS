
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

TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)
CAMP_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}

COMMAND_STAT = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz"}

STATS = ("speed", "stamina", "power", "wiz", "guts")

TARGET_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}
TARGET_TYPE_SKILL_POINT = 30

MEMBER_STATE_NONE = 0
MEMBER_STATE_TEAM = 1
MEMBER_STATE_SEMI = 2

SOUL_NONE = 0
SOUL_EXPLODED = 1
SOUL_SP_EXPLODED = 2

SOUL_THRESHOLD_MAX = 5

MAX_ONE_RACE_MEMBER_COUNT = 3
TEAM_RACE_LAST_ROUND = 5
CALENDAR_ENABLE_TURN = 3
TUTORIAL_GUILD_OPEN_TURN = 4
OPPONENT_ROLL_VERSION = 5

PENDING_BLOCK_VERSION = 2

BOSS_RANK = 100
BOSS_PLUS_RANK = 101
PLAYER_TEAM_MEMBER_DEFAULT_DRESS_ID = 101

PLAYING_STATE_TURN_START = 1
PLAYING_STATE_EVENT_CHAIN = 5
PLAYING_STATE_TEAM_RACE = 7
PLAYING_STATE_TEAM_RACE_RUNNING = 8
PLAYING_STATE_TEAM_RACE_RESULT = 9

TEAM_RACE_REWARDS = {
    1:   (2,  5),
    2:   (2,  8),
    3:   (3,  10),
    4:   (4,  15),
    5:   (5,  20),
    6:   (6,  30),
    7:   (10, 40),
    8:   (15, 50),
    100: (15, 50),
    101: (20, 60),
}

TEAMMATE_GAIN_WIN = 50
TEAMMATE_GAIN_NOT_WIN = 10

TEAM_EDIT_INVALID = 0
TEAM_EDIT_ON = 1
TEAM_EDIT_OFF = 2

TEAM_RACE_LIVE_ID = 1035

TEAM_RACE_TURNS = (24, 36, 48, 60, 72)

SUPPORT_NPC_ROWS = ((101, 9001), (103, 9003), (104, 9004), (106, 9006), (108, 9008))

DECK_SLOTS = 6


@functools.lru_cache(maxsize=1)
def scout_pool() -> dict:
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
    return {row["support_card_id"]: scout_id
            for scout_id, row in scout_pool().items()}


@functools.lru_cache(maxsize=None)
def support_card_chara(card_id) -> int:
    row = master_data.query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (int(card_id or 0),))
    return int(row["chara_id"]) if row else 0


@functools.lru_cache(maxsize=1)
def scout_rows_by_chara() -> dict:
    out: dict = {}
    for scout_id, row in scout_pool().items():
        out.setdefault(row["chara_id"], []).append(scout_id)
    return {k: tuple(v) for k, v in out.items()}


@functools.lru_cache(maxsize=1)
def support_card_charas() -> dict:
    rows = master_data.query(
        "SELECT id, chara_id, support_card_type FROM support_card_data")
    return {int(r["id"]): (int(r["chara_id"]), int(r["support_card_type"]))
            for r in rows}


@functools.lru_cache(maxsize=1)
def _pool_average() -> dict:
    cols = ("speed", "stamina", "pow", "guts", "wiz",
            "speed_limit", "stamina_limit", "pow_limit", "guts_limit", "wiz_limit")
    rows = list(scout_pool().values())
    if not rows:
        return {c: (100 if "limit" not in c else 700) for c in cols}
    return {c: int(round(sum(int(r[c] or 0) for r in rows) / len(rows))) for c in cols}


@functools.lru_cache(maxsize=1)
def special_charas() -> tuple:
    rows = master_data.query(
        "SELECT chara_id FROM single_mode_special_chara WHERE scenario_id=?",
        (SCENARIO_ID,))
    by_chara = scout_rows_by_chara()
    return tuple(by_chara[r["chara_id"]][0] for r in rows
                 if r["chara_id"] in by_chara)


TEAM_NAME_NONE = 0
TEAM_NAME_DEFAULT = 5

TEAM_NAME_SKILL = {
    1: 200681,
    2: 201121,
    3: 200471,
    4: 200741,
    5: 200491,
}
TEAM_NAME_SKILL_LEVEL = {5: 1}
TEAM_NAME_SKILL_LEVEL_DEFAULT = 3


@functools.lru_cache(maxsize=1)
def team_name_ids() -> tuple:
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_team_name ORDER BY id"))


def count_burst(st: dict, extreme: bool) -> None:
    st["bursts"] = int(st.get("bursts") or 0) + 1
    if extreme:
        st["extreme_bursts"] = int(st.get("extreme_bursts") or 0) + 1


def count_unity_training(st: dict, partners: int) -> None:
    if partners <= 0:
        return
    st["unity_trainings"] = int(st.get("unity_trainings") or 0) + 1
    st["unity_training_max_chars"] = max(
        int(st.get("unity_training_max_chars") or 0), int(partners))


ELITE_RANK = 8
ELITE_LEAGUE_RANK = 10
ELITE_TEAM_RANK = 7


def elite_available(st: dict, race_index: int) -> bool:
    return (race_index == ELITE_RACE_INDEX
            and int(st.get("team_rank") or 30) <= ELITE_LEAGUE_RANK
            and team_power(ratcheted_ranks(st)) >= ELITE_TEAM_RANK
            and int(st.get("extreme_bursts") or 0) >= 1)


ELITE_RACE_INDEX = 3


def elite_set_ids() -> tuple:
    return team_race_sets_of_rank(ELITE_RANK)


def beat_elite(st: dict) -> bool:
    return bool(st.get("beat_elite"))


def note_elite_result(st: dict, set_id: int, won: bool) -> None:
    if won and int(set_id or 0) in elite_set_ids():
        st["beat_elite"] = True


BOSS_RANK_STRENGTHENED = 101


def finals_rank(st: dict) -> int:
    return BOSS_RANK_STRENGTHENED if beat_elite(st) else BOSS_RANK


def trainee_is_scenario_chara(chara_info: dict) -> bool:
    chara_id = int((chara_info or {}).get("card_id") or 0) // 100
    if not chara_id:
        chara_id = int((chara_info or {}).get("chara_id") or 0)
    pool = scout_pool()
    return chara_id in {int(pool.get(row, {}).get("chara_id") or 0)
                        for row in special_charas()}


@functools.lru_cache(maxsize=1)
def team_name_charas() -> tuple:
    return tuple((int(r["id"]) + 1, int(r["chara_id"])) for r in master_data.query(
        "SELECT id, chara_id FROM single_mode_team_name ORDER BY id"))


def team_name_options(chara_info) -> list:
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
    name_id = int(name_id or 0)
    if name_id not in TEAM_NAME_SKILL:
        name_id = TEAM_NAME_DEFAULT
    st["team_name_id"] = name_id
    log.info("unity cup: team named (team_name_id %s)", name_id)
    return name_id


def team_name_reward(st: dict) -> tuple:
    name_id = int(st.get("team_name_id") or 0)
    if not name_id or not won_final(st):
        return (0, 0)
    return (TEAM_NAME_SKILL.get(name_id, 0),
            TEAM_NAME_SKILL_LEVEL.get(name_id, TEAM_NAME_SKILL_LEVEL_DEFAULT))


def won_final(st: dict) -> bool:
    for race in st.get("races") or ():
        if int(race.get("race_num") or 0) >= len(TEAM_RACE_TURNS):
            return int(race.get("result_state") or 0) == 1
    return False


@functools.lru_cache(maxsize=None)
def member_type(scout_id: int) -> str:
    if scout_id is None:
        return "speed"
    row = scout_pool().get(int(scout_id))
    if not row:
        return "speed"
    limits = (("speed", row["speed_limit"]), ("stamina", row["stamina_limit"]),
              ("power", row["pow_limit"]), ("guts", row["guts_limit"]),
              ("wiz", row["wiz_limit"]))
    return max(limits, key=lambda kv: kv[1])[0]


def member_soul_type(member: dict) -> str:
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


MOB_NPC_MAX_ID = 1000
BOSS_NPC_IDS = tuple(range(3000100, 3015000, 100))


@functools.lru_cache(maxsize=None)
def mob_npc_pool() -> tuple:
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id < ? AND mob_id > 0 "
        "AND npc_group_id > 0 ORDER BY id", (MOB_NPC_MAX_ID,)))


@functools.lru_cache(maxsize=None)
def named_npc_pool(suffix: int = 100) -> tuple:
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id >= 1000000 AND id < 2000000 "
        "AND id % 1000 = ? ORDER BY id", (int(suffix),))
        if master_data.has_portrait_art(r["id"] // 1000))


@functools.lru_cache(maxsize=None)
def boss_npc_ids(rank: int) -> tuple:
    suffix = 101 if int(rank) >= BOSS_PLUS_RANK else 100
    ids = tuple(i + (suffix - 100) for i in BOSS_NPC_IDS)
    have = {r["id"] for r in master_data.query(
        "SELECT id FROM single_mode_npc WHERE id >= 3000000 AND id < 3100000")}
    return tuple(i for i in ids if i in have)


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
            while pool[i] in used:
                i = (i + 1) % len(pool)
            used.add(pool[i])
            out[npc] = pool[i]
    return out


def boss_art_substitute(npc_id: int) -> int:
    return _boss_art_substitutes().get(int(npc_id), int(npc_id))


def boss_stat_source(npc_id: int, rank: int) -> int:
    if int(rank) < BOSS_RANK:
        return int(npc_id)
    for original, substitute in _boss_art_substitutes().items():
        if substitute == int(npc_id):
            return original
    return int(npc_id)


OPPONENT_VIEWER_ID = 900000000093

_TEAM_NAME_WORDS = (
    "Swiftwind", "Carrot", "Thunder", "Meteor", "Aurora", "Crescent",
    "Vanguard", "Horizon", "Sunrise", "Comet", "Tempest", "Zenith",
)


def opponent_team_name(set_id: int) -> str:
    return "Team " + _TEAM_NAME_WORDS[int(set_id) % len(_TEAM_NAME_WORDS)]


@functools.lru_cache(maxsize=None)
def npc_row(npc_id: int) -> dict | None:
    row = master_data.query_one(
        "SELECT * FROM single_mode_npc WHERE id=?", (int(npc_id),))
    return dict(row) if row else None


@functools.lru_cache(maxsize=None)
def npc_running_style(npc_id: int) -> int:
    row = npc_row(npc_id)
    if not row:
        return 2
    return max(_STYLE_APTITUDE, key=lambda s: row.get(_STYLE_APTITUDE[s], 1))


OPPONENT_TEAM_SHAPE = {
    1: (10, 1), 2: (10, 2), 3: (10, 2), 4: (13, 3), 5: (13, 3),
    6: (13, 5), 7: (15, 6), 8: (8, 8), 100: (15, 15), 101: (15, 15),
}

OPPONENT_RANK_BAND = {
    1: (34, 35), 2: (28, 31), 3: (23, 25), 4: (18, 21), 5: (14, 16),
    6: (8, 12), 7: (4, 7), 8: (3, 3), 100: (1, 1), 101: (1, 1),
}


def final_rank_stat_bonus(team_rank: int) -> int:
    row = master_data.query_one(
        "SELECT add_status FROM race_single_mode_team_status "
        "WHERE team_rank_threshold >= ? ORDER BY team_rank_threshold LIMIT 1",
        (int(team_rank),))
    return int(row["add_status"]) if row else 0


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

SOUL_FILL_CHANCE = 1.0


def soul_of(member: dict) -> int:
    return int(member.get("soul") or 1)


def burst_state(member: dict) -> int:
    return int(member.get("burst") or SOUL_NONE)


def burst_ready(member: dict) -> int | None:
    if soul_of(member) < SOUL_THRESHOLD_MAX:
        return None
    state = burst_state(member)
    if state == SOUL_NONE:
        return SOUL_EXPLODED
    if state == SOUL_EXPLODED:
        return SOUL_SP_EXPLODED
    return None


def advance_soul(member: dict, rng: random.Random) -> None:
    if soul_of(member) >= SOUL_THRESHOLD_MAX:
        return
    if rng.random() < SOUL_FILL_CHANCE:
        member["soul"] = soul_of(member) + 1


def fire_burst(member: dict, tier: int) -> dict:
    vector = BURST_VECTORS[member_soul_type(member)][tier]
    gains = {}
    for stat, gain in zip(STATS, vector):
        member[stat] = int(member.get(stat) or 0) + gain
        member[stat + "_limit"] = int(member.get(stat + "_limit") or 0) + gain
        gains[stat] = gain
    member["burst"] = tier
    return gains


GROWTH_MAIN = (44, 58)
GROWTH_OFF = (4, 11)
GROWTH_SECONDARY = (14, 30)
SECONDARY_STAT = {"speed": "power", "stamina": "guts", "power": "stamina",
                  "guts": "power", "wiz": "speed"}


def grow_member(member: dict, command_id: int, rng: random.Random,
                capped: list | None = None) -> dict:
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


STAT_LIMIT_CODE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

_CAPPED_KEY = "not_up_team_parameters"


def record_capped(st: dict, target_id, codes) -> None:
    if not codes:
        return
    rows = st.setdefault(_CAPPED_KEY, [])
    for code in sorted(set(codes)):
        rows.append({"training_partner_id": target_id, "product_code": code})


def _take_capped(st: dict) -> list:
    return st.pop(_CAPPED_KEY, None) or []


def rank_score(member: dict) -> int:
    stats = [member.get(s, 0) or 0 for s in STATS]
    skills = [{"skill_id": s} for s in member_race_skills(member)]
    total = rating_formula.get_rating(stats) + rating_formula.get_skill_score(skills)
    return min(rating_formula.MAX_RANK_SCORE, total)


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
    active = [m for m in members if m.get("state") == MEMBER_STATE_TEAM]
    if not active:
        return {s: 1 for s in STATS}
    n = len(active) + (1 if trainee else 0)
    return {s: stat_rank((sum(m.get(s, 0) or 0 for m in active)
                          + (int(trainee.get(s) or 0) if trainee else 0)) / n)
            for s in STATS}


def ratcheted_ranks(st: dict) -> dict:
    now = stat_ranks(st.get("members") or [], st.get("trainee_stats"))
    hi = st.setdefault("ranks_hi", {})
    for stat, rank in now.items():
        if rank > int(hi.get(stat) or 1):
            hi[stat] = rank
    return {stat: max(int(hi.get(stat) or 1), 1) for stat in STATS}


RACE_HOLD_KEY = "race_turn_hold"
SETTLE_NOT_UP_KEY = "settle_not_up"

POST_RACE_LAST_KEY = "post_race_last"
POST_RACE_CHAIN_KEY = "post_race_chain"


def race_turn_pending(st: dict, turn) -> bool:
    turn = int(turn or 0)
    if turn not in TEAM_RACE_TURNS:
        return False
    round_no = TEAM_RACE_TURNS.index(turn) + 1
    return len(st.get("races") or ()) < round_no


def team_race_due(st: dict, turn) -> bool:
    due = sum(1 for t in TEAM_RACE_TURNS if t <= int(turn or 0))
    return len(st.get("races") or ()) < due


def begin_race_hold(st: dict, turn) -> None:
    st[RACE_HOLD_KEY] = int(turn or 0)


def race_hold(st: dict, current_turn=None) -> int:
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
    index = int(race_num or 0) - 1
    if 0 <= index < len(TEAM_RACE_TURNS):
        return TEAM_RACE_TURNS[index]
    return int(fallback or 0)


def best_stat(st: dict) -> str:
    ranks = ratcheted_ranks(st)
    return max(STATS, key=lambda s: (int(ranks.get(s) or 1), -STATS.index(s)))


def team_power(ranks: dict) -> int:
    values = [ranks.get(s, 1) for s in STATS]
    return 9 if all(v == 8 for v in values) else max(values)


def earned_power(st: dict) -> int:
    return team_power(ratcheted_ranks(st))


def refresh_power(st: dict) -> None:
    ratcheted_ranks(st)


def team_race_reward(set_rank, race_bonus_pct: float = 0) -> tuple:
    stat, sp = TEAM_RACE_REWARDS.get(int(set_rank or 0), (0, 0))
    mult = 100 + int(race_bonus_pct or 0)
    return stat * mult // 100, sp * mult // 100


def bank_race_award(st: dict, new_rank: int, set_rank: int, won: bool,
                    final: bool, result: int = 0) -> None:
    st["pending_award"] = {"rank": int(new_rank), "set_rank": int(set_rank or 0),
                           "won": bool(won), "final": bool(final),
                           "result": int(result or (1 if won else 2))}


def pending_rank(st: dict) -> int:
    award = st.get("pending_award")
    if award:
        return int(award.get("rank") or st.get("team_rank") or 30)
    return int(st.get("team_rank") or 30)


def settle_race_award(st: dict, chara_info) -> bool:
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

    race_bonus = 0
    if isinstance(chara_info, dict):
        from ...handlers.single_mode_team import _race_bonus_pct
        race_bonus = _race_bonus_pct(chara_info)
    stat_gain, skill_points = team_race_reward(award.get("set_rank"), race_bonus)
    if isinstance(chara_info, dict):
        if won and stat_gain:
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
    if award.get("final"):
        st["rank_state"] = int(award.get("result") or (1 if won else 2))
    refresh_power(st)
    log.info("unity cup: race settled, rank -> %s (%s), teammates +%s, "
             "trainee +%s / %s SP", new_rank, "won" if won else "not won",
             gain, stat_gain if won else 0, skill_points)
    return True


def power_up_resolved(st: dict, level: int) -> None:
    level = int(level or 0)
    if level > int(st.get("power") or 1):
        st["power"] = level


VETERAN_JOIN_FRACTION = 0.74


_MEMBER_SKILLS_VERSION = 1


@functools.lru_cache(maxsize=256)
def _chara_race_skills(chara_id: int) -> tuple:
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
    if int(member.get("skills_version") or 0) != _MEMBER_SKILLS_VERSION:
        member["skills"] = list(_chara_race_skills(int(member.get("chara_id") or 0)))
        member["skills_version"] = _MEMBER_SKILLS_VERSION
    return member.get("skills") or []


def _new_member(scout_id, target_id: int, state: int,
                veteran: bool = False, chara_id=None,
                support_card_id: int = 0) -> dict:
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


ROSTER_GROWTH_AFTER_RACE = (2, 6, 9)

JOIN_WAVES_KEY = "join_waves_done"


def join_wave_resolved(st: dict, round_no) -> None:
    round_no = int(round_no or 0)
    if round_no > int(st.get(JOIN_WAVES_KEY) or 0):
        st[JOIN_WAVES_KEY] = round_no


def _rng_for(st: dict, salt: str) -> random.Random:
    return random.Random(f"{st.get('seed', 0)}:{salt}")


def deck_scout_rows(chara_info) -> list:
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
    st["scout_enabled"] = True


RIKO_CHARA_ID = 9006
RIKO_NPC_TARGET = 106
RIKO_OUTING_KEY = "riko_outing"


def riko_target(chara_info) -> int:
    for card in (chara_info or {}).get("support_card_array") or ():
        if support_card_chara(card.get("support_card_id") or 0) == RIKO_CHARA_ID:
            return int(card.get("position") or 0) or RIKO_NPC_TARGET
    return RIKO_NPC_TARGET


def apply_riko_gate(st: dict, chara_info) -> None:
    if not isinstance(chara_info, dict):
        return
    target = riko_target(chara_info)
    appear = 1 if st.get(RIKO_OUTING_KEY) else 0
    for row in chara_info.get("evaluation_info_array") or ():
        if isinstance(row, dict) and row.get("target_id") == target:
            row["is_appear"] = appear


def acting_director_resolved(st: dict) -> None:
    st[RIKO_OUTING_KEY] = True


def team_support_resolved(st: dict, chara_info=None) -> None:
    st["team_support_done"] = True
    ensure_roster(st, CALENDAR_ENABLE_TURN, chara_info)


def ensure_roster(st: dict, turn: int, chara_info=None) -> None:
    members = st.setdefault("members", [])
    if turn is None:
        return
    turn = int(turn)

    pool_rows = scout_pool()
    taken_charas = {m["chara_id"] for m in members}
    trainee = trainee_chara(chara_info)
    if trainee:
        taken_charas.add(trainee)

    def add(scout_id, target_id=None, state=MEMBER_STATE_SEMI, veteran=False,
            card_id=0):
        if scout_id is None:
            return False
        chara_id = pool_rows.get(scout_id, {}).get("chara_id")
        if not chara_id or chara_id in taken_charas:
            return False
        taken_charas.add(chara_id)
        members.append(_new_member(
            scout_id, chara_id if target_id is None else target_id,
            state, veteran=veteran, support_card_id=card_id))
        return True

    if not members:
        if not isinstance(chara_info, dict):
            return
        for position, scout_id, card_id in deck_scout_rows(chara_info):
            add(scout_id, target_id=position, card_id=card_id)
        st["deck_size"] = len(members)
        for scout_id in special_charas():
            add(scout_id)
        st["base_size"] = len(members)
        if not members:
            return

    if members and isinstance(chara_info, dict):
        pool_charas = {row["chara_id"] for row in pool_rows.values()}
        unrenderable = [(m, "no single_mode_scout_chara row")
                        for m in members if m["chara_id"] not in pool_charas]
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
                    continue
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
                continue
            member = _new_member(scout_id, position, joined,
                                 support_card_id=card_id)
            taken_charas.add(member["chara_id"])
            members.insert(size, member)
            size += 1
            seated.add(position)
            st["deck_size"] = size
            st["base_size"] = int(st.get("base_size") or len(members)) + 1
            log.info("unity cup: seated missing deck slot %s as chara %s",
                     position, member["chara_id"])

    deck_size = int(st.get("deck_size") or 0)
    if st.get("team_support_done"):
        for m in members[:deck_size]:
            if m.get("state") == MEMBER_STATE_SEMI:
                m["state"] = MEMBER_STATE_TEAM
        if st.get("scout_join_done") or (
                turn >= TUTORIAL_GUILD_OPEN_TURN and st.get("announced")):
            for m in members[deck_size:]:
                if m.get("state") == MEMBER_STATE_SEMI:
                    m["state"] = MEMBER_STATE_TEAM

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
        add(scout_id, state=MEMBER_STATE_TEAM, veteran=True)


def scout_support_card(scout_id) -> int:
    return int((scout_pool().get(int(scout_id or 0)) or {}).get("support_card_id") or 0)


def unannounced_scouts(st: dict) -> list:
    if not st.get("team_support_done"):
        return []
    done = set(st.get("announced") or ())
    deck_size = int(st.get("deck_size") or 0)
    return [m for m in (st.get("members") or ())[deck_size:]
            if m.get("state") in (MEMBER_STATE_TEAM, MEMBER_STATE_SEMI)
            and m.get("target_id") not in done]


def scout_join_resolved(st: dict, event_id, announced_by_event: dict) -> None:
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


_DISTANCE_APTITUDE = {1: "proper_distance_short", 2: "proper_distance_mile",
                      3: "proper_distance_middle", 4: "proper_distance_long",
                      5: "proper_distance_middle"}
_STYLE_APTITUDE = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
                   3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}


LINEUP_SLOT_ORDER = ([(d, 1) for d in (1, 2, 3, 4, 5)] +
                     [(d, m) for d in (1, 2, 3, 4, 5) for m in (2, 3)])

LINEUP_SHAPE_VERSION = 2

MIN_LINEUP_SEATS = 11


def best_running_style(scout_id: int) -> int:
    row = (scout_pool().get(int(scout_id)) or {}) if scout_id is not None else {}
    return max(_STYLE_APTITUDE, key=lambda s: row.get(_STYLE_APTITUDE[s], 1))


def auto_lineup(st: dict) -> list:
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
    if int(chara_info.get("playing_state") or 0) == PLAYING_STATE_TEAM_RACE and \
            team_race_due(st, chara_info.get("turn")):
        return False
    log.warning("unity cup: career resumed into the team-race screen with no "
                "race to show -- returning it to the turn")
    chara_info["playing_state"] = PLAYING_STATE_TURN_START
    st["opponents"] = None
    return True


def offers(st: dict) -> list:
    cached = st.get("opponents")
    if not isinstance(cached, dict):
        return []
    if int(cached.get("version") or 0) != OPPONENT_ROLL_VERSION:
        return []
    return cached.get("list") or []


def lineup_of(st: dict, chara_info=None) -> list:
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
        roster[trainee] = None

    kept, taken, seated = [], set(), set()
    stored = list(st.get("lineup") or ())

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
            continue
        kept.append(entry)
        taken.add(slot)
        seated.add(chara_id)

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


PROGRESS_TURNS = ((50, 3), (30, 2), (19, 1))


def scenario_progress(turn) -> int:
    for threshold, value in PROGRESS_TURNS:
        if (turn or 0) >= threshold:
            return value
    return 0


def team_title(st: dict) -> int:
    return 1 + len(st.get("races") or ())


TEAM_TITLE_TEXT_CATEGORY = 193


def team_title_name(title_id: int) -> str:
    row = master_data.query_one(
        'SELECT text FROM text_data WHERE category=? AND "index"=?',
        (TEAM_TITLE_TEXT_CATEGORY, int(title_id or 0)))
    return (row["text"] if row else "") or ""


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
    if not isinstance(chara_info, dict):
        return 0
    return int(chara_info.get("card_id") or 0) // 100


def is_active(chara_info) -> bool:
    return isinstance(chara_info, dict) and \
        int(chara_info.get("scenario_id") or 0) == SCENARIO_ID


def _team_info(st: dict, command_info_array=None, chara_info=None) -> dict:
    members = active_members(st)
    turn = chara_info.get("turn") if isinstance(chara_info, dict) else None
    ranks = ratcheted_ranks(st)
    return {
        "team_name_id": int(st.get("team_name_id") or 0),
        "speed_rank": ranks["speed"], "stamina_rank": ranks["stamina"],
        "power_rank": ranks["power"], "guts_rank": ranks["guts"],
        "wiz_rank": ranks["wiz"],
        "team_power": int(st.get("power") or 1),
        "team_rank": int(st.get("team_rank") or 30),
        "team_rank_state": int(st.get("rank_state") or 0),
        "team_title": team_title(st),
        "guide_partner_count": int(st.get("guide_count") or 0),
        "is_scout_enable": bool(st.get("scout_enabled") or members),
        "team_chara_info_array": [
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
        "team_edit_flag": int(st.get("team_edit_flag") or TEAM_EDIT_ON),
    }


def lineup_served(st: dict, turn) -> bool:
    if st.get("lineup_saved"):
        return True
    return turn is not None and int(turn) >= TEAM_RACE_TURNS[0]


def _evaluation_info_array(st: dict, chara_info=None) -> list:
    rows = [{"target_id": m["target_id"], "chara_id": m["chara_id"],
             "member_state": m.get("state", MEMBER_STATE_TEAM),
             "soul_threshold_id": soul_of(m),
             "soul_event_state": burst_state(m)}
            for m in (st.get("members") or ())]
    seated = {r["target_id"] for r in rows}
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
    on_team = {m["chara_id"] for m in (st.get("members") or ())} | in_deck
    rows += [{"target_id": tid, "chara_id": cid,
              "member_state": MEMBER_STATE_NONE,
              "soul_threshold_id": 1, "soul_event_state": SOUL_NONE}
             for tid, cid in SUPPORT_NPC_ROWS if cid not in on_team]
    rows.sort(key=lambda r: r["target_id"])
    return rows


_WIN_TYPE_ENDPOINTS = ("/team_race_start", "/team_race_end",
                       "/team_race_continue")


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


_COMMAND_RESULT_ENDPOINTS = ("exec_command",)
_EMPTY_COMMAND_RESULT = {"skill_tips_array": None, "soul_skill_tips_array": None,
                         "sp_soul_skill_tips_array": None}


def _command_result(st: dict, endpoint: str):
    if not (endpoint or "").endswith(_COMMAND_RESULT_ENDPOINTS):
        return None
    banked = st.pop("command_result", None)
    return banked if isinstance(banked, dict) else dict(_EMPTY_COMMAND_RESULT)


def _sync_chara_evaluation(data: dict, st: dict) -> None:
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

    legal = [r["target_id"] for r in _evaluation_info_array(st, ci)]
    for _tid in legal:
        if _tid not in have:
            rows.append(row(_tid))
            have.add(_tid)
    appear = {}
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
    for r in rows:
        if isinstance(r, dict) and r.get("target_id") in appear:
            r["is_appear"] = appear[r["target_id"]]
            r["evaluation"] = 0
        elif isinstance(r, dict) and r.get("target_id") in force_appear:
            r["is_appear"] = 1
    keep = set(legal)
    rows = [r for r in rows
            if isinstance(r, dict) and r.get("target_id") in keep]
    rows.sort(key=lambda r: r.get("target_id") or 0)
    ci["evaluation_info_array"] = rows
    apply_riko_gate(st, ci)


def _mirror_load_common(data: dict) -> None:
    block = data.get("single_mode_load_common")
    if not isinstance(block, dict):
        return
    for key in ("chara_info", "home_info"):
        if isinstance(data.get(key), dict) and key in block:
            block[key] = data[key]


RANK_TRAINING_LEVEL = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 5}


def facility_levels(st: dict) -> dict:
    ranks = ratcheted_ranks(st)
    return {str(cmd): RANK_TRAINING_LEVEL.get(int(ranks.get(stat) or 1), 1)
            for cmd, stat in COMMAND_STAT.items()}


FACILITY_SHOWN_KEY = "facility_shown"


def facility_levels_shown(st: dict) -> dict:
    shown = st.get(FACILITY_SHOWN_KEY)
    earned = facility_levels(st)
    if not isinstance(shown, dict) or not shown:
        shown = dict(earned)
        st[FACILITY_SHOWN_KEY] = shown
    for cmd, level in earned.items():
        shown.setdefault(cmd, level)
    return {k: int(v) for k, v in shown.items()}


def facility_level_up_pending(st: dict) -> dict:
    shown = facility_levels_shown(st)
    return {cmd: lvl for cmd, lvl in facility_levels(st).items()
            if lvl > int(shown.get(cmd) or 0)}


def facility_level_up_resolved(st: dict) -> None:
    st[FACILITY_SHOWN_KEY] = dict(facility_levels(st))


def _sync_facility_levels(full_state: dict, chara_info: dict, st: dict,
                          endpoint: str = "") -> None:
    levels = facility_levels_shown(st)
    from ...handlers import single_mode_team as smt
    career = (full_state or {}).get(smt.STATE_KEY)
    if isinstance(career, dict) and isinstance(career.get("data"), dict):
        career["data"][smt.FACILITY_LEVELS_KEY] = dict(levels)
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
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    st = state(full_state)
    turn = chara_info.get("turn")
    st["trainee_stats"] = {s: int(chara_info.get(s) or 0) for s in STATS}
    ensure_roster(st, turn, chara_info)
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
    owed = st.pop(SETTLE_NOT_UP_KEY, None)
    if owed:
        info = data.setdefault("not_up_parameter_info", {})
        info["status_type_array"] = sorted(
            set(info.get("status_type_array") or ()) | set(owed))
    _mirror_load_common(data)
    if endpoint.endswith("/team_race_out"):
        data["tmp_team_rank"] = pending_rank(st)
    return response
