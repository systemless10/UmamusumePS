
from __future__ import annotations

import copy
import functools
import logging
import random

from ... import event_engine
from ... import master_data
from ..base import cap_bonus_for_scenario

log = logging.getLogger(__name__)

SCENARIO_ID = 3
STATE_KEY = "grand_live"

DANCE, PASSION, VOCAL, VISUAL, MENTAL = 1, 2, 3, 4, 5
PERF_TYPES = (DANCE, PASSION, VOCAL, VISUAL, MENTAL)
PERF_NAMES = {DANCE: "dance", PASSION: "passion", VOCAL: "vocal",
              VISUAL: "visual", MENTAL: "mental"}

_PERF_WIRE_TARGET = {DANCE: 77, VOCAL: 78, PASSION: 79, VISUAL: 80, MENTAL: 81}


def perf_wire_target(perf_type: int) -> int:
    return _PERF_WIRE_TARGET.get(perf_type, 0)

START_TOKEN_CAP = 200
TOKEN_CAP_PER_LIVE = 50
MAX_TOKEN_CAP = START_TOKEN_CAP + 4 * TOKEN_CAP_PER_LIVE

TOKEN_BASE_WISDOM = 5
TOKEN_BASE_OTHER = 9
TOKEN_MULT_TENTHS = (10, 11, 13, 15, 17, 20)
TOKEN_PER_LINK = 2

_TOKEN_DISTRIBUTION = {
    101: ((DANCE, 65), (PASSION, 1), (VOCAL, 3), (VISUAL, 25), (MENTAL, 6)),
    105: ((DANCE, 6), (PASSION, 56), (VOCAL, 33), (VISUAL, 2), (MENTAL, 3)),
    102: ((DANCE, 4), (PASSION, 1), (VOCAL, 66), (VISUAL, 1), (MENTAL, 28)),
    103: ((DANCE, 35), (PASSION, 6), (VOCAL, 3), (VISUAL, 55), (MENTAL, 1)),
    106: ((DANCE, 0), (PASSION, 34), (VOCAL, 2), (VISUAL, 2), (MENTAL, 62)),
}
TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)
CAMP_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}

@functools.lru_cache(maxsize=1)
def link_chara_ids() -> frozenset:
    try:
        rows = master_data.query(
            "SELECT chara_id FROM single_mode_special_chara WHERE scenario_id=?",
            (SCENARIO_ID,))
        ids = frozenset(int(r["chara_id"]) for r in rows)
    except Exception:
        ids = frozenset()
    return ids or frozenset({9008, 1002, 1026, 1032, 1046})

PLAYING_STATE_BACKSTAGE = 10
PLAYING_STATE_NORMAL = 1
PLAYING_STATE_EVENT_CHAIN = 5

LIVE_TURNS = {24: 1, 36: 2, 48: 3, 60: 4, 72: 10}
GRAND_LIVE_TYPE = 10
GREAT_SUCCESS_SONGS = 3
RESULT_GREAT, RESULT_NORMAL = 2, 1

SP_PER_SONG = 25
SP_PER_TECHNIQUE = 5
LIVE_STATS_GREAT = 10
LIVE_STATS_NORMAL = 3
LIVE_FANS = {
    (1, True): 1000, (1, False): 200,
    (2, True): 3000, (2, False): 200,
    (3, True): 5500, (3, False): 200,
    (4, True): 6500, (4, False): 200,
}

GRAND_CONCERT_SONG_GATE = 18
GRAND_CONCERT_STATS_SPECIAL = 15
GRAND_CONCERT_STATS_NORMAL = 12
GRAND_CONCERT_FANS_SPECIAL = 9000
GRAND_CONCERT_FANS_NORMAL = 2000
GRAND_CONCERT_SKILL_SPECIAL = 210071
GRAND_CONCERT_SKILL_NORMAL = 210072

MAKE_DEBUT_LIVE_ID = 1006
GIRLS_LEGEND_U_LIVE_ID = 1029
FREE_LIVE_IDS = (MAKE_DEBUT_LIVE_ID, GIRLS_LEGEND_U_LIVE_ID)

SONG_GATE_GOLD_SKILL = (65, 16)
SONG_GATE_SPECIAL_LIVE = (71, 18)

_PATTERN = {
    1: ((1, 2, 3), (4, 4, 2, 2)),
    2: ((2, 2, 2), (4, 5, 2, 2)),
    3: ((2, 2, 2), (4, 5, 2, 2)),
    4: ((2, 2, 2), (4, 5, 2, 2)),
    5: ((2, 2, 2), (4, 3, 2, 2)),
}
BOARD_SIZE = 3
LESSON_UNLOCK_TURN = 5

BOARD_ENERGY_RATE = 10 / 48
BOARD_NO_HINT_RATE = 3 / 38
BOARD_PAIR_AS_SINGLE_RATE = 0.26

_SONG_UNLOCK = {
    0: (1038, 1044, 1040, 1003, 1057, 1042, 1047, 1046),
    1: (1023, 1032, 1011),
    2: (1012, 1043, 1045, 1034),
    3: (1024, 1020, 1039, 1021, 1014, 1041),
}

STAT_SPEED, STAT_STAMINA, STAT_POWER, STAT_GUTS, STAT_WIT, STAT_SP = 1, 2, 3, 4, 5, 6
_STAT_BY_INDEX = {STAT_SPEED: "speed", STAT_STAMINA: "stamina", STAT_POWER: "power",
                  STAT_GUTS: "guts", STAT_WIT: "wiz", STAT_SP: "skill_point"}


GAIN_STAT, GAIN_HINT, GAIN_ENERGY, GAIN_TRAINING_BONUS = 1, 2, 3, 5
STAT_MODE_FLAT, STAT_MODE_RANGE = 1, 2
MB_SONG = 4

SQUARE_TECHNIQUE_TYPES = (1, 2, 3)
SQUARE_SONG_TYPE = 4


def is_active(chara_info) -> bool:
    return bool(chara_info) and int(chara_info.get("scenario_id") or 0) == SCENARIO_ID


@functools.lru_cache(maxsize=1)
def _squares() -> dict:
    out = {}
    for r in master_data.query("SELECT * FROM single_mode_live_square"):
        d = dict(r)
        cost = {}
        for i in range(1, 6):
            t, v = d[f"perf_type_{i}"], d[f"perf_value_{i}"]
            if t and v:
                cost[int(t)] = cost.get(int(t), 0) + int(v)
        out[int(d["id"])] = {
            "id": int(d["id"]), "type": int(d["square_type"]), "cost": cost,
            "master_bonus_id": int(d["master_bonus_id"]),
        }
    return out


@functools.lru_cache(maxsize=1)
def _master_bonuses() -> dict:
    out = {}
    for r in master_data.query("SELECT * FROM single_mode_live_master_bonus"):
        d = dict(r)
        gains = []
        for k in (1, 2, 3):
            t = d[f"master_bonus_gain_type_{k}"]
            if t:
                gains.append((int(t), [int(d[f"master_bonus_gain_value_{k}_{j}"])
                                       for j in range(1, 5)]))
        out[int(d["id"])] = {
            "song_live_id": (int(d["master_bonus_type_value"])
                             if int(d["master_bonus_type"]) == MB_SONG else None),
            "gains": gains,
        }
    return out


@functools.lru_cache(maxsize=1)
def _song_squares() -> dict:
    out = {}
    for sid, sq in _squares().items():
        if sq["type"] != SQUARE_SONG_TYPE:
            continue
        live_id = _master_bonuses().get(sq["master_bonus_id"], {}).get("song_live_id")
        if live_id:
            out[live_id] = sid
    return out


@functools.lru_cache(maxsize=1)
def _song_bonuses() -> dict:
    return {int(r["live_id"]): (int(r["live_bonus_type"]), int(r["live_bonus_value"]))
            for r in master_data.query("SELECT * FROM single_mode_live_song_list")}


def _is_offerable_square(sq: dict) -> bool:
    bonus = _master_bonuses().get(sq["master_bonus_id"])
    if not bonus:
        return True
    for gain_type, vals in bonus["gains"]:
        if gain_type == GAIN_HINT and not _hint_skill_ids(vals[1]):
            return False
        if gain_type == GAIN_STAT and vals[1] == STAT_MODE_RANGE:
            return False
    return True


_TIER_SINGLE = {1: 5, 2: 8, 3: 12}
_TIER_PAIR = {2: 4, 3: 6}


def _tier_for_segment(segment: int) -> int:
    if segment <= 1:
        return 1
    return 2 if segment <= 3 else 3


_HINT_MAX_LEVEL = {1: 1, 2: 2, 3: 3}
_ENERGY_MAX_GAIN = {1: 20, 2: 30, 3: 30}


@functools.lru_cache(maxsize=1)
def _stat_squares_by_tier() -> dict:
    out = {1: [], 2: [], 3: []}
    for sid, sq in _squares().items():
        if sq["type"] not in SQUARE_TECHNIQUE_TYPES or not _is_offerable_square(sq):
            continue
        gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
        stat_gains = [v for t, v in gains if t == GAIN_STAT]
        if not stat_gains or len(stat_gains) != len(gains):
            continue
        values = {v[2] for v in stat_gains}
        if len(values) != 1:
            continue
        value = values.pop()
        table = _TIER_SINGLE if len(stat_gains) == 1 else _TIER_PAIR
        tier = next((t for t, val in table.items() if val == value), None)
        if tier:
            out[tier].append(sid)
    return {t: tuple(sorted(v)) for t, v in out.items()}


def _square_gain_types(sq: dict) -> set:
    gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
    return {t for t, _v in gains}


@functools.lru_cache(maxsize=1)
def _energy_squares() -> tuple:
    return tuple(sorted(sid for sid, sq in _squares().items()
                        if sq["type"] in SQUARE_TECHNIQUE_TYPES
                        and _is_offerable_square(sq)
                        and GAIN_ENERGY in _square_gain_types(sq)))


@functools.lru_cache(maxsize=1)
def _hint_squares() -> tuple:
    return tuple(sorted(sid for sid, sq in _squares().items()
                        if sq["type"] in SQUARE_TECHNIQUE_TYPES
                        and _is_offerable_square(sq)
                        and GAIN_HINT in _square_gain_types(sq)))


def _hint_square_suits(sq: dict, chara_info: dict | None) -> bool:
    if chara_info is None:
        return True
    gains = (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ()
    for gain_type, vals in gains:
        if gain_type != GAIN_HINT:
            continue
        field = _hint_apt_field(vals[1])
        if field and int(chara_info.get(field) or 0) < _HIGH_APTITUDE_GRADE:
            return False
    return True


def _square_gain(sq: dict, gain_type: int):
    for t, vals in (_master_bonuses().get(sq["master_bonus_id"]) or {}).get("gains") or ():
        if t == gain_type:
            return vals
    return None


def _offered_squares(st, chara_info: dict | None) -> dict:
    tier = _tier_for_segment(_segment(st))
    single, pair = [], []
    for sid in _stat_squares_by_tier().get(tier, ()):
        gains = (_master_bonuses().get(_squares()[sid]["master_bonus_id"]) or {}).get("gains") or ()
        (pair if len(gains) > 1 else single).append(sid)
    hint = [sid for sid in _hint_squares()
            if (_square_gain(_squares()[sid], GAIN_HINT) or [0, 0, 99])[2] <= _HINT_MAX_LEVEL[tier]
            and _hint_square_suits(_squares()[sid], chara_info)]
    energy = [sid for sid in _energy_squares()
              if (_square_gain(_squares()[sid], GAIN_ENERGY) or [99])[0] <= _ENERGY_MAX_GAIN[tier]]
    return {"single": single, "pair": pair, "hint": hint, "energy": energy}


def _roll_technique_board(pools: dict, r) -> list:
    slots = ["single",
             "single" if r.random() < BOARD_NO_HINT_RATE else "hint",
             "pair" if r.random() >= BOARD_PAIR_AS_SINGLE_RATE else "single"]
    if r.random() < BOARD_ENERGY_RATE:
        slots[r.randrange(len(slots))] = "energy"
    board = []
    for kind in slots:
        pool = [s for s in pools.get(kind) or () if s not in board]
        if not pool:
            pool = [s for s in pools["single"] if s not in board]
        if pool:
            board.append(r.choice(pool))
    return board


@functools.lru_cache(maxsize=1)
def scenario_cap_bonus() -> dict:
    return cap_bonus_for_scenario(SCENARIO_ID)


def _blank() -> dict:
    return {
        "tokens": {str(t): 0 for t in PERF_TYPES},
        "token_cap": START_TOKEN_CAP,
        "songs": [],
        "pending_songs": [],
        "active_songs": [],
        "training_bonus": {},
        "board": [],
        "reserve_square_id": 0,
        "lives_done": [],
        "segment_songs": 0,
        "segment_techniques": 0,
        "techniques_since_song": 0,
        "unlocked": False,
        "unlocked_turn": 0,
        "activate_after_turn": 0,
        "raise_cap_after_turn": 0,
    }


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, v)
    return st


def reset(full_state: dict) -> None:
    full_state[STATE_KEY] = _blank()


def _tokens(st) -> dict:
    return {int(k): int(v) for k, v in (st.get("tokens") or {}).items()}


def _set_tokens(st, tokens: dict) -> None:
    cap = st.get("token_cap", START_TOKEN_CAP)
    st["tokens"] = {str(t): max(0, min(cap, int(tokens.get(t, 0)))) for t in PERF_TYPES}


def _segment(st) -> int:
    return min(len(st.get("lives_done") or ()) + 1, 5)


def _songs_needed(st) -> int:
    initial, loop = _PATTERN[_segment(st)]
    n = st.get("segment_songs", 0)
    if n < len(initial):
        return initial[n]
    return loop[(n - len(initial)) % len(loop)]


def _available_songs(st) -> list:
    done = len(st.get("lives_done") or ())
    owned = set(st.get("songs") or ())
    out = []
    for tier in range(0, min(done, 3) + 1):
        out += [s for s in _SONG_UNLOCK.get(tier, ()) if s not in owned]
    return [s for s in out if s in _song_squares()]


def _roll_board(st, rng=None, chara_info: dict | None = None) -> list:
    r = rng or random
    grand_done = any(int(x.get("live_type") or 0) == GRAND_LIVE_TYPE
                     for x in (st.get("lives_done") or ()))
    if not grand_done and st.get("techniques_since_song", 0) >= _songs_needed(st):
        songs = _available_songs(st)
        if songs:
            picks = r.sample(songs, min(BOARD_SIZE, len(songs)))
            return [_song_squares()[s] for s in picks]
    return _roll_technique_board(_offered_squares(st, chara_info), r)


def ensure_board(st, rng=None, chara_info: dict | None = None) -> list:
    if not st.get("board"):
        st["board"] = _roll_board(st, rng, chara_info)
    return st["board"]


def reroll_board(st, chara_info: dict | None = None, keep: int = 0,
                 rng=None) -> list:
    fresh = _roll_board(st, rng, chara_info)
    keep = int(keep or 0)
    if not keep or keep in fresh:
        return fresh
    old = [int(s) for s in (st.get("board") or [])]
    slot = old.index(keep) if keep in old else 0
    slot = min(slot, len(fresh) - 1) if fresh else 0
    if not fresh:
        return [keep]
    fresh[slot] = keep
    return fresh


def can_afford(st, square_id: int) -> bool:
    sq = _squares().get(int(square_id))
    if not sq:
        return False
    have = _tokens(st)
    return all(have.get(t, 0) >= v for t, v in sq["cost"].items())


def _hint_skill_ids(tag: int) -> tuple:
    return _hint_skill_ids_cached(int(tag))


@functools.lru_cache(maxsize=64)
def _hint_skill_ids_cached(tag: int) -> tuple:
    want = str(tag)
    out = []
    for r in master_data.query(
            "SELECT id, tag_id FROM skill_data "
            "WHERE disable_singlemode=0 AND rarity=1 AND tag_id LIKE ?",
            (f"%{want}%",)):
        if want in str(r["tag_id"] or "").split("/"):
            out.append(int(r["id"]))
    return tuple(sorted(out))


def skill_rarity(skill_id: int) -> int:
    row = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (int(skill_id),))
    return int(row["rarity"]) if row else 1


def add_skill_tip(chara_info: dict, skill_id: int, level: int, rarity: int = 1) -> None:
    group_id = int(skill_id) // 10
    tips = chara_info.setdefault("skill_tips_array", [])
    for t in tips:
        if t.get("group_id") == group_id and t.get("rarity") == rarity:
            t["level"] = min(5, t.get("level", 0) + int(level))
            return
    tips.append({"group_id": group_id, "rarity": rarity, "level": min(5, max(1, int(level)))})


_STYLE_APT_FIELD = {101: "proper_running_style_nige", 102: "proper_running_style_senko",
                    103: "proper_running_style_sashi", 104: "proper_running_style_oikomi"}
_DISTANCE_APT_FIELD = {201: "proper_distance_short", 202: "proper_distance_mile",
                       203: "proper_distance_middle", 204: "proper_distance_long"}
_GROUND_APT_FIELD = {501: "proper_ground_turf", 502: "proper_ground_dirt"}
_HIGH_APTITUDE_GRADE = 7


def _hint_apt_field(tag: int):
    return (_STYLE_APT_FIELD.get(tag) or _DISTANCE_APT_FIELD.get(tag)
            or _GROUND_APT_FIELD.get(tag))


def _grant_hint(chara_info: dict, tag: int, level: int, rng=None) -> int | None:
    apt_field = _hint_apt_field(tag)
    if apt_field and int(chara_info.get(apt_field) or 0) < _HIGH_APTITUDE_GRADE:
        return None
    r = rng or random
    candidates = _hint_skill_ids(tag)
    if not candidates:
        return None
    owned = {int(s.get("skill_id")) for s in (chara_info.get("skill_array") or [])
             if s.get("skill_id")}
    pool = [s for s in candidates if s not in owned]
    if not pool:
        return None
    skill_id = r.choice(pool)
    add_skill_tip(chara_info, skill_id, level, rarity=1)
    return skill_id


def _ranged_gain(vals, token_cost: int) -> int:
    half = int(token_cost) // 2
    return max(vals[2], min(vals[3], half))


def apply_square(st, chara_info: dict, square_id: int, rng=None) -> dict:
    r = rng or random
    square_id = int(square_id)
    sq = _squares().get(square_id)
    if sq is None:
        raise ValueError(f"unknown square {square_id}")
    if not can_afford(st, square_id):
        raise ValueError(f"cannot afford square {square_id}")

    have = _tokens(st)
    for t, v in sq["cost"].items():
        have[t] = have.get(t, 0) - v
    _set_tokens(st, have)

    bonus = _master_bonuses().get(sq["master_bonus_id"], {"gains": [], "song_live_id": None})
    result = {"square_id": square_id, "song": None, "stats": {}, "energy": 0,
              "hint": None, "training_bonus": {}}

    spent = sum(sq["cost"].values())
    for gain_type, vals in bonus["gains"]:
        if gain_type == GAIN_STAT:
            stat = _STAT_BY_INDEX.get(vals[0])
            if not stat:
                continue
            amount = vals[2] if vals[1] == STAT_MODE_FLAT else _ranged_gain(vals, spent)
            result["stats"][stat] = result["stats"].get(stat, 0) + amount
        elif gain_type == GAIN_HINT:
            result["hint"] = _grant_hint(chara_info, vals[1], vals[2], r)
        elif gain_type == GAIN_ENERGY:
            result["energy"] += vals[0]
        elif gain_type == GAIN_TRAINING_BONUS:
            key = str(vals[0])
            st["training_bonus"][key] = st["training_bonus"].get(key, 0) + vals[1]
            result["training_bonus"][key] = vals[1]

    _apply_stats(chara_info, result["stats"])
    if result["energy"]:
        chara_info["vital"] = min(chara_info.get("max_vital", 100),
                                  chara_info.get("vital", 0) + result["energy"])

    song = bonus["song_live_id"]
    if song:
        st["bought_songs"] = _bought_songs(st) + 1
        _learn_song(st, song)
        result["song"] = song
        if st.get("techniques_since_song", 0) < _songs_needed(st):
            st["techniques_since_song"] = st.get("techniques_since_song", 0) + 1
        else:
            st["segment_songs"] = st.get("segment_songs", 0) + 1
            st["techniques_since_song"] = 0
    else:
        st["segment_techniques"] = st.get("segment_techniques", 0) + 1
        st["techniques_since_song"] = st.get("techniques_since_song", 0) + 1

    if st.get("reserve_square_id") == square_id:
        st["reserve_square_id"] = 0
    st["board"] = _roll_board(st, r, chara_info)
    return result


def _apply_stats(chara_info: dict, stats: dict) -> list:
    owed = event_engine.capped_stat_codes(
        chara_info, [s for s, a in stats.items()
                     if s != "skill_point" and (a or 0) > 0])
    for stat, amount in stats.items():
        if not amount:
            continue
        if stat == "skill_point":
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + amount
            continue
        event_engine.add_stat(chara_info, stat, amount)
    return owed


def _bought_songs(st) -> int:
    if "bought_songs" in st:
        return int(st["bought_songs"] or 0)
    return len([s for s in (st.get("pending_songs") or ()) if s not in FREE_LIVE_IDS])


def _learn_song(st, live_id: int) -> None:
    live_id = int(live_id)
    if live_id in (st.get("songs") or ()):
        return
    st.setdefault("songs", []).append(live_id)
    st.setdefault("pending_songs", []).append(live_id)


def grant_free_song(full_state: dict, live_id: int, turn: int = 0,
                    chara_info: dict = None) -> bool:
    st = state(full_state)
    if int(live_id) in (st.get("songs") or ()):
        return False
    _learn_song(st, live_id)
    if int(live_id) == MAKE_DEBUT_LIVE_ID:
        st["unlocked"] = True
        st["unlocked_turn"] = int(turn or 0)
        have = _tokens(st)
        _set_tokens(st, {t: have.get(t, 0) + 10 for t in PERF_TYPES})
        ensure_board(st, chara_info=chara_info)
    elif int(live_id) == GIRLS_LEGEND_U_LIVE_ID and chara_info is not None:
        event_engine.stash_not_up(full_state, _apply_stats(
            chara_info, {s: 10 for s in
                         ("speed", "stamina", "power", "guts", "wiz")}))
    return True


def grant_lowest_performance(full_state: dict, chara_info: dict, amount: int) -> int | None:
    if not is_active(chara_info):
        return None
    st = state(full_state)
    tokens = _tokens(st)
    target = min(PERF_TYPES, key=lambda t: tokens.get(t, 0))
    tokens[target] = tokens.get(target, 0) + int(amount)
    _set_tokens(st, tokens)
    return target


def grant_performance(full_state: dict, chara_info: dict, perf_type: int, amount: int) -> bool:
    if not is_active(chara_info) or perf_type not in PERF_TYPES:
        return False
    st = state(full_state)
    tokens = _tokens(st)
    tokens[perf_type] = tokens.get(perf_type, 0) + int(amount)
    _set_tokens(st, tokens)
    return True


def tokens_visible(full_state: dict, turn) -> bool:
    st = state(full_state)
    if not st.get("unlocked"):
        return False
    unlocked_turn = int(st.get("unlocked_turn") or 0)
    return not unlocked_turn or int(turn or 0) > unlocked_turn


def token_gain(command_id: int, support_chara_ids=(), facility_level: int = 1,
               supporter_count: int = 0) -> int:
    is_wit = CAMP_BASE.get(command_id, command_id) == 106
    s = TOKEN_BASE_WISDOM if is_wit else TOKEN_BASE_OTHER
    f = max(1, int(facility_level or 1))
    ids = list(support_chara_ids or ())
    c = min(len(ids) + int(supporter_count or 0), len(TOKEN_MULT_TENTHS) - 1)
    links = link_chara_ids()
    l = sum(1 for chara_id in ids if chara_id in links)
    return (s + f) * TOKEN_MULT_TENTHS[c] // 10 + TOKEN_PER_LINK * l


def _roll_token_types(command_id: int, rainbow: bool, rng=None) -> list:
    r = rng or random
    dist = _TOKEN_DISTRIBUTION.get(CAMP_BASE.get(command_id, command_id))
    if not dist:
        return []
    types = [t for t, _ in dist]
    weights = [w for _, w in dist]
    if not any(weights):
        return []
    first = r.choices(types, weights=weights)[0]
    if not rainbow:
        return [first]
    rest = [(t, w) for t, w in dist if t != first and w]
    if not rest:
        return [first]
    second = r.choices([t for t, _ in rest], weights=[w for _, w in rest])[0]
    return [first, second]


def token_preview(command_id: int, support_chara_ids=(), rainbow: bool = False,
                  rng=None, facility_level: int = 1, supporter_count: int = 0) -> list:
    value = token_gain(command_id, support_chara_ids, facility_level, supporter_count)
    return [{"performance_type": t, "value": value}
            for t in _roll_token_types(command_id, rainbow, rng)]


def award_tokens(full_state: dict, preview: list) -> dict:
    st = state(full_state)
    have = _tokens(st)
    for entry in preview or ():
        t = int(entry.get("performance_type") or 0)
        if t in PERF_TYPES:
            have[t] = have.get(t, 0) + int(entry.get("value") or 0)
    _set_tokens(st, have)
    return st["tokens"]


LIVE_BONUS_SPECIALTY = 1
LIVE_BONUS_SUPPORT_EVENT = 2
LIVE_BONUS_FRIENDSHIP = 3


def live_bonuses(full_state: dict) -> dict:
    st = state(full_state)
    bonuses = _song_bonuses()
    out = {"friendship": 0, "specialty": 0, "support_event": 0}
    for live_id in (st.get("active_songs") or ()):
        kind, value = bonuses.get(int(live_id), (0, 0))
        if kind == LIVE_BONUS_FRIENDSHIP:
            out["friendship"] += value
        elif kind == LIVE_BONUS_SPECIALTY:
            out["specialty"] += value
        elif kind == LIVE_BONUS_SUPPORT_EVENT:
            out["support_event"] += value
    return out


def friendship_bonus_pct(full_state: dict) -> int:
    return live_bonuses(full_state)["friendship"]


def training_bonus_for(full_state: dict) -> dict:
    st = state(full_state)
    out = {}
    for key, val in (st.get("training_bonus") or {}).items():
        stat = _STAT_BY_INDEX.get(int(key))
        if stat and val:
            out[stat] = out.get(stat, 0) + int(val)
    return out


def live_turn_type(turn: int):
    return LIVE_TURNS.get(int(turn or 0))


def pending_live_for(full_state: dict, turn) -> int | None:
    done = {r.get("live_type") for r in (state(full_state).get("lives_done") or ())}
    for candidate in (int(turn or 0), int(turn or 0) - 1):
        live_type = LIVE_TURNS.get(candidate)
        if live_type is not None and live_type not in done:
            return live_type
    return None


def live_turn_of(live_type: int):
    for turn, lt in LIVE_TURNS.items():
        if lt == live_type:
            return turn
    return None


def songs_learned(full_state: dict) -> int:
    return len(state(full_state).get("songs") or ())


def countable_songs(full_state: dict) -> int:
    return len([s for s in (state(full_state).get("songs") or ())
                if s != GIRLS_LEGEND_U_LIVE_ID])


def perform_live(full_state: dict, chara_info: dict, turn: int) -> dict:
    st = state(full_state)
    live_type = live_turn_type(turn)
    if live_type is None:
        return {}

    songs = len(st.get("pending_songs") or ())
    techniques = st.get("segment_techniques", 0)
    skill = None
    if live_type == GRAND_LIVE_TYPE:
        great = countable_songs(full_state) >= GRAND_CONCERT_SONG_GATE
        stat_gain = GRAND_CONCERT_STATS_SPECIAL if great else GRAND_CONCERT_STATS_NORMAL
        fans = GRAND_CONCERT_FANS_SPECIAL if great else GRAND_CONCERT_FANS_NORMAL
        skill = GRAND_CONCERT_SKILL_SPECIAL if great else GRAND_CONCERT_SKILL_NORMAL
    else:
        great = songs >= GREAT_SUCCESS_SONGS
        stat_gain = LIVE_STATS_GREAT if great else LIVE_STATS_NORMAL
        fans = LIVE_FANS.get((live_type, great), 0)
    result_state = RESULT_GREAT if great else RESULT_NORMAL

    sp = _bought_songs(st) * SP_PER_SONG + techniques * SP_PER_TECHNIQUE
    st["pending_payout"] = {"stat": stat_gain, "sp": sp, "fans": fans, "skill": skill}

    activated = list(st.get("pending_songs") or ())
    st["activate_after_turn"] = int(turn or 0)

    st.setdefault("lives_done", []).append(
        {"live_type": live_type, "result_state": result_state,
         "song_ids": sorted(activated), "songs_owned": len(st.get("songs") or ())})
    if live_type != GRAND_LIVE_TYPE:
        st["raise_cap_after_turn"] = int(turn or 0)

    st["segment_songs"] = 0
    st["bought_songs"] = 0
    st["segment_techniques"] = 0
    st["techniques_since_song"] = 0

    return {"live_type": live_type, "result_state": result_state, "great": great,
            "songs": songs, "techniques": techniques, "skill_point": sp,
            "stat_gain": stat_gain, "activated": activated}


support_card_chara = master_data.support_card_chara
is_trainee_chara = master_data.is_trainee_chara


def _member_state(chara_id, seated: set) -> int:
    return int(int(chara_id or 0) in seated)


def apply_live_payout(full_state: dict, chara_info: dict) -> dict:
    st = state(full_state)
    payout = st.pop("pending_payout", None)
    if not payout:
        return {}
    event_engine.stash_not_up(full_state, _apply_stats(
        chara_info, {s: payout["stat"] for s in
                     ("speed", "stamina", "power", "guts", "wiz")}))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + payout["sp"]
    chara_info["fans"] = chara_info.get("fans", 0) + payout["fans"]
    skill_id = payout.get("skill")
    if skill_id:
        add_skill_tip(chara_info, skill_id, 1, rarity=skill_rarity(skill_id))
    return payout


def add_supporter(full_state: dict, chara_id: int) -> bool:
    st = state(full_state)
    seated = st.setdefault("supporters", [])
    if int(chara_id) in seated:
        return False
    seated.append(int(chara_id))
    return True


def settle_lives(full_state: dict) -> bool:
    st = state(full_state)
    if not st.get("raise_cap_after_turn"):
        return False
    st["token_cap"] = min(MAX_TOKEN_CAP,
                          st.get("token_cap", START_TOKEN_CAP) + TOKEN_CAP_PER_LIVE)
    st["raise_cap_after_turn"] = 0
    _set_tokens(st, _tokens(st))
    st["lives_settled"] = int(st.get("lives_settled") or 0) + 1
    return True


def _settle_activation(st: dict, turn) -> None:
    cap_after = int(st.get("raise_cap_after_turn") or 0)
    if cap_after and int(turn or 0) > cap_after + 1:
        st["token_cap"] = min(MAX_TOKEN_CAP,
                              st.get("token_cap", START_TOKEN_CAP) + TOKEN_CAP_PER_LIVE)
        st["raise_cap_after_turn"] = 0
        _set_tokens(st, _tokens(st))
        st["lives_settled"] = int(st.get("lives_settled") or 0) + 1

    after = int(st.get("activate_after_turn") or 0)
    if not after or int(turn or 0) <= after:
        return
    moving = list(st.get("pending_songs") or ())
    if moving:
        st["active_songs"] = list(st.get("active_songs") or ()) + moving
    st["pending_songs"] = []
    st["activate_after_turn"] = 0
    if moving:
        log.info("grand live: live bonuses now active for %s", moving)


def _live_members(chara_info: dict, full_state: dict = None) -> list:
    from . import producers as career_producers
    seated = {int(c) for c in career_producers.supporters_for(full_state or {})}

    out = []
    trainee = int(chara_info.get("card_id") or 0) // 100
    if trainee:
        out.append({"target_id": 0, "chara_id": trainee,
                    "member_state": _member_state(trainee, seated)})
    for card in (chara_info.get("support_card_array") or []):
        chara_id = support_card_chara(card.get("support_card_id") or 0)
        if chara_id:
            out.append({"target_id": int(card.get("position") or 0),
                        "chara_id": chara_id,
                        "member_state": _member_state(chara_id, seated)})
    for target_id, chara_id in ((101, 9001), (102, 9002), (103, 9003),
                                (104, 9004), (105, 9008), (106, 9006)):
        out.append({"target_id": target_id, "chara_id": chara_id, "member_state": 0})

    seen = {r["chara_id"] for r in out}
    for chara_id in career_producers.supporters_for(full_state or {}):
        if chara_id not in seen:
            seen.add(chara_id)
            out.append({"target_id": chara_id, "chara_id": chara_id,
                        "member_state": 1})
    return out


def build_live_data_set(full_state: dict, chara_info: dict,
                        command_info_array=None) -> dict:
    st = state(full_state)
    _settle_activation(st, chara_info.get("turn"))
    ensure_board(st, chara_info=chara_info)
    tokens = _tokens(st)
    cap = st.get("token_cap", START_TOKEN_CAP)

    perf = {PERF_NAMES[t]: tokens.get(t, 0) for t in PERF_TYPES}
    perf.update({f"max_{PERF_NAMES[t]}": cap for t in PERF_TYPES})

    if command_info_array is None:
        from . import preview as _preview
        cached = (full_state.get(_preview.PREVIEW_KEY) or {}).get("commands")
        command_info_array = copy.deepcopy(cached) if cached else [
            {"command_type": 1, "command_id": cmd,
             "performance_inc_dec_info_array": [], "params_inc_dec_info_array": []}
            for cmd in TRAINING_COMMAND_IDS]

    return {
        "live_performance_info": perf,
        "command_info_array": list(command_info_array),
        "evaluation_info_array": _live_members(chara_info, full_state),
        "next_square_info_array": [{"square_id": sid, "square_num": i + 1}
                                   for i, sid in enumerate(st.get("board") or ())],
        "master_live_id_array": sorted(st.get("songs") or ()),
        "next_live_id_array": sorted(st.get("pending_songs") or ()),
        "effected_live_id_array": sorted(st.get("active_songs") or ()),
        "not_up_parameter_info": {
            "performance_type_array": [t for t in PERF_TYPES if tokens.get(t, 0) >= cap]},
        "live_result_array": [{"live_type": int(r.get("live_type") or 0),
                               "result_state": int(r.get("result_state") or 0)}
                              for r in (st.get("lives_done") or ())],
        "reserve_square_id": int(st.get("reserve_square_id") or 0),
        "training_bonus_array": [
            {"target_type": int(k), "effect_value": int(v)}
            for k, v in sorted((st.get("training_bonus") or {}).items(),
                               key=lambda kv: int(kv[0])) if v],
    }


def attach(response: dict, full_state: dict, chara_info: dict,
           command_info_array=None, endpoint: str = "") -> dict:
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    data.pop("ura_data_set", None)
    data.pop("team_data_set", None)
    data["live_data_set"] = build_live_data_set(full_state, chara_info,
                                                command_info_array)
    if endpoint.endswith("/live_start"):
        results = data["live_data_set"].get("live_result_array") or []
        if len(results) > 1:
            data["live_data_set"]["live_result_array"] = results[-1:] + results[:-1]
    return response

