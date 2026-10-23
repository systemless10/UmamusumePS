
from __future__ import annotations

import copy
import functools
import json
import random
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .. import master_data
from .. import region
from .. import rating_formula
from .. import state as state_store
from ..fixtures import store as fixtures
from ..result_codes import ResultCode, refuse

ROSTER_KEY = "legacy_roster"

ROSTER_VERSION = "house_v6"
ROSTER_VERSION_KEY = "roster_version"
ROSTER_BACKUP_KEY = "legacy_roster_pre_house"

PLAYER_CAREER_ID_BASE = 100_000
INJECT_ID_BASE = 190_000
HOUSE_ID_BASE = 200_000

INT32_MAX = 2_147_483_647
MAXED_INJECT_ID = 199_999

MAXED_VETERAN_FAN_CEILING = 99_999_999

STARTER_ROSTER_SIZE = 6

HOUSE_ROSTER_SIZE = 60

VETERAN_CAPACITY = 260

_STAT_KEYS = ("speed", "stamina", "power", "wiz", "guts")

_REF_DIR = Path(__file__).resolve().parents[2] / "data" / "training_ref"
with open(_REF_DIR / "uma_data.json", encoding="utf-8") as _uma_f:
    _UMA_DATA = [u for u in json.load(_uma_f) if u.get("cardId")]

_APT_LETTER_TO_VALUE = {"S": 8, "A": 7, "B": 6, "C": 5, "D": 4, "E": 3, "F": 2, "G": 1}

_GEN_STAT_BANDS = {
    "speed": (1000, 1350),
    "stamina": (400, 950),
    "power": (700, 1150),
    "wiz": (650, 1150),
    "guts": (350, 850),
}
_GEN_STAT_CAP = 1500
_GEN_TALENT_KEY = {
    "speed": "talentSpeed", "stamina": "talentStamina", "power": "talentPower",
    "guts": "talentGuts", "wiz": "talentWisdom",
}
_GEN_SCENARIO_ID = 2
_GEN_ROUTE_ID = 20
_GEN_ARRIVE_ROUTE_RACE_ID = 228


def seed_roster_from_fixture() -> list[dict]:
    pair = fixtures.first("trained_chara/load")
    return pair.response_copy()["data"]["trained_chara_array"] if pair else []


_ANCESTRY_POSITIONS = (10, 20, 11, 12, 21, 22)
_blue_factors_cache = None
_pink_factors_cache = None
_green_cards_cache = None


def _blue_factors():
    global _blue_factors_cache
    if _blue_factors_cache is None:
        _blue_factors_cache = [r["factor_id"] for r in master_data.query("SELECT factor_id FROM succession_factor WHERE factor_type=1")]
    return _blue_factors_cache


def _pink_factors():
    global _pink_factors_cache
    if _pink_factors_cache is None:
        _pink_factors_cache = [r["factor_id"] for r in master_data.query("SELECT factor_id FROM succession_factor WHERE factor_type=2")]
    return _pink_factors_cache


def _green_cards():
    global _green_cards_cache
    if _green_cards_cache is None:
        _green_cards_cache = {r["factor_group_id"] for r in master_data.query(
            "SELECT DISTINCT factor_group_id FROM succession_factor WHERE factor_type=3")}
    return _green_cards_cache


def _weighted_star(r):
    x = r.random()
    return 1 if x < 0.70 else (2 if x < 0.93 else 3)


def _green_factor(card_id, star):
    return card_id * 100 + star if card_id in _green_cards() else None


def _generate_sparks(card_id, rarity, r):
    sparks = [
        {"factor_id": r.choice(_blue_factors()), "level": 0},
        {"factor_id": r.choice(_pink_factors()), "level": 0},
    ]
    if rarity >= 3:
        g = _green_factor(card_id, _weighted_star(r))
        if g is not None:
            sparks.append({"factor_id": g, "level": 0})
    return sparks


def _ancestry_entry_from(uma: dict, position_id: int) -> dict:
    return {
        "position_id": position_id,
        "card_id": uma.get("card_id"),
        "rank": uma.get("rank", 0),
        "rarity": uma.get("rarity", 3),
        "talent_level": uma.get("talent_level", 1),
        "factor_info_array": copy.deepcopy(uma.get("factor_info_array") or []),
        "win_saddle_id_array": copy.deepcopy(uma.get("win_saddle_id_array") or []),
        "owner_viewer_id": uma.get("owner_viewer_id", 0),
    }


def _real_ancestry(roster: list[dict], parent_1, parent_2) -> list | None:
    by_id = {c.get("trained_chara_id"): c for c in roster}
    parents = [(10, by_id.get(parent_1)), (20, by_id.get(parent_2))]
    if not any(p for _pos, p in parents):
        return None
    out = []
    for pos, parent in parents:
        if not parent:
            continue
        out.append(_ancestry_entry_from(parent, pos))
        gp = {e.get("position_id"): e for e in parent.get("succession_chara_array") or []}
        for offset, src_pos in ((1, 10), (2, 20)):
            g = gp.get(src_pos)
            if g:
                entry = copy.deepcopy(g)
                entry["position_id"] = pos + offset
                out.append(entry)
    return out or None


def _generate_ancestry(card_id, rarity, rng=None):
    r = rng if rng is not None else random
    green_cards = list(_green_cards())

    ancestry = []
    for pos in _ANCESTRY_POSITIONS:
        acard = r.choice(green_cards)
        ararity = r.choice([3, 4])
        ancestry.append({
            "position_id": pos,
            "card_id": acard,
            "rank": r.randint(8, 20),
            "rarity": ararity,
            "talent_level": r.randint(1, 5),
            "factor_info_array": _generate_sparks(acard, ararity, r),
            "win_saddle_id_array": [],
            "owner_viewer_id": 0,
        })
    own_factors = _generate_sparks(card_id, rarity, r)
    return ancestry, own_factors


_support_cards_cache = None


def _support_cards():
    global _support_cards_cache
    if _support_cards_cache is None:
        rows = master_data.query("SELECT id, rarity FROM support_card_data")
        ids = [r["id"] for r in rows]
        rar = {r["id"]: r["rarity"] for r in rows}
        maxexp = {r["rarity"]: r["mx"] for r in master_data.query(
            "SELECT rarity, MAX(total_exp) mx FROM support_card_level GROUP BY rarity")}
        _support_cards_cache = (ids, maxexp, rar)
    return _support_cards_cache


def _random_support_cards(r, count=6):
    ids, maxexp, rar = _support_cards()
    if not ids:
        return []
    picks = r.sample(ids, min(count, len(ids)))
    return [
        {
            "position": pos,
            "support_card_id": sid,
            "exp": maxexp.get(rar.get(sid), 0) or 0,
            "limit_break_count": r.randint(0, 4),
        }
        for pos, sid in enumerate(picks, start=1)
    ]


_DEBUT_PROGRAM_ID = 1067
_DEBUT_TURN = 12


def _debut_race_result(r, running_style):
    return [{
        "turn": _DEBUT_TURN,
        "program_id": _DEBUT_PROGRAM_ID,
        "weather": 1,
        "ground_condition": 1,
        "running_style": running_style or 2,
        "popularity": r.randint(1, 5),
        "result_rank": 1,
        "result_time": r.randint(1_150_000, 1_200_000),
        "prize_money": 0,
    }]


def _career_race_results(career_state: dict, chara_info: dict) -> list:
    history = (career_state.get("data") or {}).get("race_history") or []
    out = []
    for h in history:
        pid = h.get("program_id")
        turn = h.get("turn") or 0
        seed = ((int(pid or 0) * 2654435761) ^ (int(turn) * 40503)) & 0xffffffff
        out.append({
            "turn": turn,
            "program_id": pid,
            "weather": h.get("weather", 1),
            "ground_condition": h.get("ground_condition", 1),
            "running_style": (h.get("running_style")
                              or chara_info.get("race_running_style") or 2),
            "popularity": h.get("frame_order") or 1,
            "result_rank": h.get("result_rank") or 1,
            "result_time": 1_150_000 + seed % 50_000,
            "prize_money": 0,
        })
    return out


def _apt_value(uma: dict, key: str) -> int:
    return _APT_LETTER_TO_VALUE.get(str(uma.get(key, "G")).upper(), 1)


def _gen_skill_array(uma: dict) -> list[dict]:
    raw = str(uma.get("skillIds") or "")
    ids = [int(tok) for tok in raw.split(",") if tok.strip().isdigit()]
    return [{"skill_id": sid, "level": 1} for sid in ids]


_card_dress_cache = None
_race_dress_by_card_cache = None


def _card_dress_data():
    global _card_dress_cache
    if _card_dress_cache is None:
        valid = {r["id"] for r in master_data.query("SELECT id FROM dress_data")}
        card_to_dress = {}
        pair = fixtures.first("trained_chara/load")
        if pair:
            for e in pair.response["data"]["trained_chara_array"]:
                c, cloth = e.get("card_id"), e.get("race_cloth_id")
                if c and cloth in valid:
                    card_to_dress[c] = cloth
        _card_dress_cache = (valid, card_to_dress)
    return _card_dress_cache


def _race_dress_by_card() -> dict:
    global _race_dress_by_card_cache
    if _race_dress_by_card_cache is None:
        rows = master_data.query(
            "SELECT card_id, race_dress_id FROM card_rarity_data crd "
            "WHERE rarity = (SELECT MAX(rarity) FROM card_rarity_data "
            "                WHERE card_id = crd.card_id)")
        _race_dress_by_card_cache = {r["card_id"]: r["race_dress_id"] for r in rows}
    return _race_dress_by_card_cache


def dress_for_card(card_id: int, chara_id: int = 0) -> int:
    authoritative = _race_dress_by_card().get(card_id)
    if authoritative is not None:
        return authoritative

    valid, card_to_dress = _card_dress_data()
    if card_id in card_to_dress:
        return card_to_dress[card_id]
    if card_id in valid:
        return card_id
    if chara_id:
        row = master_data.query_one(
            "SELECT id FROM dress_data WHERE chara_id=? AND use_race=1 AND general_purpose=0 "
            "ORDER BY id LIMIT 1", (chara_id,))
        if row:
            return row["id"]
        base = chara_id * 100 + 1
        if base in valid:
            return base
    return card_id


def _gen_running_style(uma: dict) -> int:
    apts = {
        1: _apt_value(uma, "aptitudeRunner"),
        2: _apt_value(uma, "aptitudeLeader"),
        3: _apt_value(uma, "aptitudeBetweener"),
        4: _apt_value(uma, "aptitudeChaser"),
    }
    return max(apts, key=apts.get)


def build_generated_veteran(viewer_id, uma: dict, trained_chara_id: int, rng: random.Random) -> dict:
    stats = {}
    for stat, (lo, hi) in _GEN_STAT_BANDS.items():
        talent = uma.get(_GEN_TALENT_KEY[stat], 0) or 0
        stats[stat] = min(_GEN_STAT_CAP, rng.randint(lo, hi) + int(talent * rng.uniform(2.0, 6.0)))

    rank_score = rating_formula.get_rating([stats[k] for k in _STAT_KEYS])
    fans = rng.randint(80_000, 900_000)
    card_id = uma["cardId"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ancestry, own_factors = _generate_ancestry(card_id, 3, rng)

    return {
        "viewer_id": "<redacted>",
        "trained_chara_id": trained_chara_id,
        "owner_viewer_id": 0,
        "owner_trained_chara_id": 0,
        "single_mode_chara_id": trained_chara_id,
        "chara_seed": rng.getrandbits(31),
        "card_id": card_id,
        "succession_trained_chara_id_1": 0,
        "succession_trained_chara_id_2": 0,
        "use_type": 0,
        "speed": stats["speed"],
        "stamina": stats["stamina"],
        "power": stats["power"],
        "wiz": stats["wiz"],
        "guts": stats["guts"],
        "fans": fans,
        "rank_score": rank_score,
        "rank": _rank_for_score(rank_score),
        "scenario_id": _GEN_SCENARIO_ID,
        "route_id": _GEN_ROUTE_ID,
        "arrive_route_race_id": _GEN_ARRIVE_ROUTE_RACE_ID,
        "proper_ground_turf": _apt_value(uma, "aptitudeTurf"),
        "proper_ground_dirt": _apt_value(uma, "aptitudeDirt"),
        "proper_running_style_nige": _apt_value(uma, "aptitudeRunner"),
        "proper_running_style_senko": _apt_value(uma, "aptitudeLeader"),
        "proper_running_style_sashi": _apt_value(uma, "aptitudeBetweener"),
        "proper_running_style_oikomi": _apt_value(uma, "aptitudeChaser"),
        "proper_distance_short": _apt_value(uma, "aptitudeShort"),
        "proper_distance_mile": _apt_value(uma, "aptitudeMile"),
        "proper_distance_middle": _apt_value(uma, "aptitudeMiddle"),
        "proper_distance_long": _apt_value(uma, "aptitudeLong"),
        "succession_num": 0,
        "rarity": 3,
        "is_saved": 1,
        "is_locked": 0,
        "talent_level": rng.choice([3, 4, 5]),
        "race_cloth_id": dress_for_card(card_id, uma.get("charaId", 0)),
        "chara_grade": _chara_grade_for(fans),
        "running_style": _gen_running_style(uma),
        "nickname_id": 0,
        "wins": 0,
        "register_time": now,
        "create_time": now,
        "skill_array": _gen_skill_array(uma),
        "support_card_list": _random_support_cards(rng),
        "race_result_list": _debut_race_result(rng, _gen_running_style(uma)),
        "win_saddle_id_array": [],
        "nickname_id_array": [],
        "factor_info_array": own_factors,
        "factor_extend_array": [],
        "succession_chara_array": ancestry,
    }


def generate_house_roster(viewer_id, count: int = HOUSE_ROSTER_SIZE) -> list[dict]:
    rng = random.Random(zlib.crc32(str(viewer_id).encode()))
    umas = _UMA_DATA
    return [
        build_generated_veteran(viewer_id, umas[i % len(umas)], HOUSE_ID_BASE + i + 1, rng)
        for i in range(count)
    ]


_ALL_APTITUDE_FIELDS = (
    "proper_ground_turf", "proper_ground_dirt",
    "proper_distance_short", "proper_distance_mile",
    "proper_distance_middle", "proper_distance_long",
    "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi",
)


def _all_skill_ids() -> list[int]:
    return [int(r["id"]) for r in master_data.query("SELECT id FROM skill_data ORDER BY id")]


def _max_rank_id() -> int:
    row = master_data.query_one("SELECT MAX(id) AS m FROM single_mode_rank")
    return row["m"] if row and row["m"] is not None else 1


def _max_out_veteran_fields(rec: dict) -> None:
    for stat in _STAT_KEYS:
        rec[stat] = 9999
    for field in _ALL_APTITUDE_FIELDS:
        rec[field] = 8
    rec["rank_score"] = rating_formula.MAX_RANK_SCORE
    rec["fans"] = MAXED_VETERAN_FAN_CEILING
    rec["rank"] = _max_rank_id()
    rec["chara_grade"] = _chara_grade_for(MAXED_VETERAN_FAN_CEILING)
    rec["rarity"] = 5
    rec["talent_level"] = 5
    _max_out_lineage_saddles(rec)
    skill_meta = {
        r["id"]: (r["rarity"], r["grade_value"] or 0)
        for r in master_data.query("SELECT id, rarity, grade_value FROM skill_data")
    }
    existing_ids = {s.get("skill_id") for s in (rec.get("skill_array") or [])
                    if isinstance(s, dict)}
    all_ids = sorted(existing_ids | set(_all_skill_ids()))
    rec["skill_array"] = [
        {"skill_id": sid, "level": 6 if skill_meta.get(sid, (1, 0))[0] in (3, 4, 5) else 1}
        for sid in all_ids
        if skill_meta.get(sid, (1, 0))[1] >= 0
    ]


def build_maxed_veteran(viewer_id, card_id: int, trained_chara_id: int = MAXED_INJECT_ID) -> dict:
    uma = next((u for u in _UMA_DATA if u.get("cardId") == card_id), None)
    rng = random.Random(trained_chara_id)
    rec = build_generated_veteran(viewer_id, uma or _UMA_DATA[0], trained_chara_id, rng)
    rec["card_id"] = card_id
    rec["race_cloth_id"] = dress_for_card(card_id, uma.get("charaId", 0) if uma else 0)
    rec["running_style"] = 1
    _max_out_veteran_fields(rec)
    return rec


_starter_trained_chara_cache: list | None = None


def _load_starter_trained_chara(viewer_id) -> list[dict]:
    global _starter_trained_chara_cache
    if _starter_trained_chara_cache is None:
        path = region.seeds_dir() / "starter_trained_chara.json"
        with open(path, "r", encoding="utf-8") as f:
            _starter_trained_chara_cache = json.load(f)
    template = copy.deepcopy(_starter_trained_chara_cache)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for e in template:
        e["viewer_id"] = viewer_id
        e["owner_viewer_id"] = 0
        e["register_time"] = now
        e["create_time"] = now
    return template


def _get_or_seed_roster(viewer_id) -> list[dict]:
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY)

    if roster is not None and full_state.get(ROSTER_VERSION_KEY) == ROSTER_VERSION:
        if any([_max_out_lineage_saddles(c) if _is_maxed(c) else _migrate_legacy_saddles(c)
                for c in roster]):
            state_store.save_state(viewer_id, full_state)
        return roster

    if roster is None:
        full_state[ROSTER_KEY] = _load_starter_trained_chara(viewer_id)
        full_state[ROSTER_VERSION_KEY] = ROSTER_VERSION
        state_store.save_state(viewer_id, full_state)
        return full_state[ROSTER_KEY]

    if ROSTER_BACKUP_KEY not in full_state:
        full_state[ROSTER_BACKUP_KEY] = roster

    preserved = [
        e for e in roster
        if PLAYER_CAREER_ID_BASE <= e.get("trained_chara_id", 0) < HOUSE_ID_BASE
        or 1 <= e.get("trained_chara_id", 0) <= STARTER_ROSTER_SIZE
    ]
    full_state[ROSTER_KEY] = generate_house_roster(viewer_id) + preserved
    full_state[ROSTER_VERSION_KEY] = ROSTER_VERSION
    state_store.save_state(viewer_id, full_state)
    return full_state[ROSTER_KEY]


def handle_trained_chara_load(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    roster = _get_or_seed_roster(viewer_id)
    from . import room_match
    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {
            "trained_chara_array": copy.deepcopy(roster),
            "trained_chara_favorite_array": [],
            "room_match_entry_chara_id_array": room_match.entered_chara_ids(viewer_id),
        },
    }


def handle_change_nickname(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    tcid = payload.get("trained_chara_id")
    nickname_id = payload.get("nickname_id")
    if not isinstance(tcid, int) or not isinstance(nickname_id, int):
        return refuse()
    if nickname_id and not master_data.query_one(
            "SELECT id FROM nickname WHERE id=?", (nickname_id,)):
        return refuse()

    full_state = state_store.get_state(viewer_id) or {}
    roster = _get_or_seed_roster(viewer_id)
    entry = next((c for c in roster if c.get("trained_chara_id") == tcid), None)
    if entry is None:
        return refuse()
    entry["nickname_id"] = nickname_id
    full_state[ROSTER_KEY] = roster
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"trained_chara_info": copy.deepcopy(entry)}}


def _rank_for_score(rank_score: int) -> int:
    row = master_data.query_one(
        "SELECT id FROM single_mode_rank WHERE ? BETWEEN min_value AND max_value",
        (rank_score,),
    )
    if row:
        return row["id"]
    bounds = master_data.query_one(
        "SELECT MIN(min_value) AS lo, MAX(max_value) AS hi FROM single_mode_rank")
    if bounds and bounds["hi"] is not None and rank_score > bounds["hi"]:
        return _max_rank_id()
    return 1


def _chara_grade_for(fans: int) -> int:
    row = master_data.query_one(
        "SELECT id FROM single_mode_chara_grade WHERE need_fan_count <= ? "
        "ORDER BY need_fan_count DESC, id DESC LIMIT 1",
        (fans,),
    )
    return row["id"] if row else 1


_SADDLE_TYPE_G1 = 3
_SADDLE_COND_SAME_YEAR = 1
_LEGACY_SADDLE_FLOOR = 1000


@functools.lru_cache(maxsize=1)
def _saddle_rows() -> tuple:
    rows = master_data.query(
        "SELECT id, condition, win_saddle_type, race_instance_id_1, race_instance_id_2, "
        "race_instance_id_3, race_instance_id_4, race_instance_id_5, race_instance_id_6, "
        "race_instance_id_7, race_instance_id_8 FROM single_mode_wins_saddle ORDER BY id")
    return tuple(
        (r["id"], r["condition"], r["win_saddle_type"],
         frozenset(r[f"race_instance_id_{i}"] for i in range(1, 9) if r[f"race_instance_id_{i}"]))
        for r in rows)


def _saddles_for_wins(wins: dict) -> list[int]:
    out = []
    for sid, cond, _type, races in _saddle_rows():
        if not races or not races <= wins.keys():
            continue
        if cond == _SADDLE_COND_SAME_YEAR:
            years = [wins[ri] for ri in races]
            if None not in set().union(*years) and not set.intersection(*map(set, years)):
                continue
        out.append(sid)
    return out


def _win_saddles_from_results(race_results: list) -> list[int]:
    wins: dict = {}
    for r in race_results or ():
        if r.get("result_rank") != 1:
            continue
        row = master_data.query_one(
            "SELECT race_instance_id FROM single_mode_program WHERE id=?", (r.get("program_id"),))
        if row:
            turn = r.get("turn") or 0
            wins.setdefault(row["race_instance_id"], set()).add((turn - 1) // 24 if turn else None)
    return _saddles_for_wins(wins)


def g1_win_count(rec: dict) -> int:
    g1 = {sid for sid, _c, t, _r in _saddle_rows() if t == _SADDLE_TYPE_G1}
    return sum(1 for s in rec.get("win_saddle_id_array") or () if s in g1)


def _is_maxed(rec: dict) -> bool:
    return all((rec.get(k) or 0) >= 9999 for k in _STAT_KEYS)


def _max_out_lineage_saddles(rec: dict) -> bool:
    every = [sid for sid, _c, _t, _r in _saddle_rows()]
    changed = False
    for entry in [rec] + [a for a in rec.get("succession_chara_array") or () if isinstance(a, dict)]:
        if entry.get("win_saddle_id_array") != every:
            entry["win_saddle_id_array"] = list(every)
            changed = True
    return changed


def _migrate_legacy_saddles(rec: dict) -> bool:
    changed = False
    arr = rec.get("win_saddle_id_array") or []
    if any(s >= _LEGACY_SADDLE_FLOOR for s in arr):
        if rec.get("race_result_list"):
            rec["win_saddle_id_array"] = _win_saddles_from_results(rec["race_result_list"])
        else:
            rec["win_saddle_id_array"] = _saddles_for_wins({ri: {None} for ri in arr})
        changed = True
    for anc in rec.get("succession_chara_array") or ():
        if isinstance(anc, dict) and _migrate_legacy_saddles(anc):
            changed = True
    return changed


def _succession_num_for(roster: list[dict], parent_id_1, parent_id_2) -> int:
    by_id = {c.get("trained_chara_id"): c for c in roster}
    parents = [by_id.get(parent_id_1), by_id.get(parent_id_2)]
    parent_nums = [p.get("succession_num", 0) for p in parents if p]
    return (max(parent_nums) + 1) if parent_nums else 1


def build_trained_chara_from_career(viewer_id, career_state: dict, start_chara: dict,
                                    roster: list[dict], factor_info_array=None,
                                    full_state: dict | None = None) -> dict:
    chara_info = career_state["data"]["chara_info"]
    start_chara = start_chara or {}

    scenario_snapshot: dict = {}
    if full_state is not None:
        from .. import scenarios
        scenario_snapshot = dict(
            scenarios.for_chara(chara_info).snapshot(full_state) or {})
    scenario_data = scenario_snapshot.pop("scenario_data_array", None)
    grand_live_songs = scenario_snapshot.pop("grand_live_songs", None) or []
    grand_live_lives_done = scenario_snapshot.pop("grand_live_lives_done", None) or []

    crane_session_plushies = (full_state or {}).get("crane_session_plushies") or 0

    stats = {k: chara_info.get(k, 0) for k in _STAT_KEYS}
    rank_score = rating_formula.get_career_score(chara_info)
    fans = chara_info.get("fans", 0)

    support_card_list = [
        {
            "position": c.get("position"),
            "support_card_id": c.get("support_card_id"),
            "exp": c.get("exp", 0),
            "limit_break_count": c.get("limit_break_count", 0),
        }
        for c in chara_info.get("support_card_array", [])
    ]

    parent_1 = start_chara.get("succession_trained_chara_id_1", 0) or 0
    parent_2 = start_chara.get("succession_trained_chara_id_2", 0) or 0

    race_results = _career_race_results(career_state, chara_info)
    win_count = sum(1 for r in race_results if r.get("result_rank") == 1)
    win_saddles = _win_saddles_from_results(race_results)

    won_program_ids = [r.get("program_id") for r in race_results if r.get("result_rank") == 1]

    career_ids = [
        c.get("trained_chara_id", 0) for c in roster
        if PLAYER_CAREER_ID_BASE <= c.get("trained_chara_id", 0) < INJECT_ID_BASE
    ]
    new_id = (max(career_ids) + 1) if career_ids else (PLAYER_CAREER_ID_BASE + 1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ancestry, own_factors = _generate_ancestry(chara_info.get("card_id"), chara_info.get("rarity", 3))
    lineage_roster = roster
    if full_state is not None and full_state.get("career_parent_snapshot") is not None:
        lineage_roster = full_state["career_parent_snapshot"]
    real = _real_ancestry(lineage_roster, parent_1, parent_2)
    if real:
        ancestry = real
    if factor_info_array:
        own_factors = copy.deepcopy(factor_info_array)

    rec = {
        "viewer_id": "<redacted>",
        "trained_chara_id": new_id,
        "owner_viewer_id": 0,
        "owner_trained_chara_id": 0,
        "single_mode_chara_id": chara_info.get("single_mode_chara_id"),
        "chara_seed": random.getrandbits(31),
        "card_id": chara_info.get("card_id"),
        "succession_trained_chara_id_1": parent_1,
        "succession_trained_chara_id_2": parent_2,
        "use_type": 0,
        "speed": stats["speed"],
        "stamina": stats["stamina"],
        "power": stats["power"],
        "wiz": stats["wiz"],
        "guts": stats["guts"],
        "fans": fans,
        "rank_score": rank_score,
        "rank": _rank_for_score(rank_score),
        "scenario_id": chara_info.get("scenario_id"),
        "route_id": chara_info.get("route_id"),
        "arrive_route_race_id": chara_info.get("arrive_route_race_id", 0),
        "proper_ground_turf": chara_info.get("proper_ground_turf"),
        "proper_ground_dirt": chara_info.get("proper_ground_dirt"),
        "proper_running_style_nige": chara_info.get("proper_running_style_nige"),
        "proper_running_style_senko": chara_info.get("proper_running_style_senko"),
        "proper_running_style_sashi": chara_info.get("proper_running_style_sashi"),
        "proper_running_style_oikomi": chara_info.get("proper_running_style_oikomi"),
        "proper_distance_short": chara_info.get("proper_distance_short"),
        "proper_distance_mile": chara_info.get("proper_distance_mile"),
        "proper_distance_middle": chara_info.get("proper_distance_middle"),
        "proper_distance_long": chara_info.get("proper_distance_long"),
        "succession_num": _succession_num_for(roster, parent_1, parent_2),
        "rarity": chara_info.get("rarity"),
        "is_saved": 1,
        "is_locked": 0,
        "talent_level": chara_info.get("talent_level"),
        "race_cloth_id": dress_for_card(chara_info.get("card_id"), chara_info.get("chara_id", 0)),
        "chara_grade": _chara_grade_for(fans),
        "running_style": chara_info.get("race_running_style"),
        "nickname_id": 0,
        "wins": win_count,
        "register_time": now,
        "create_time": now,
        "skill_array": copy.deepcopy(chara_info.get("skill_array", [])),
        "support_card_list": support_card_list,
        "race_result_list": race_results,
        "win_saddle_id_array": win_saddles,
        "nickname_id_array": copy.deepcopy(chara_info.get("nickname_id_array", [])),
        "factor_info_array": own_factors,
        "factor_extend_array": [],
        "succession_chara_array": ancestry,
        **({"scenario_data_array": scenario_data} if scenario_data else {}),
        "grand_live_songs": grand_live_songs,
        "grand_live_lives_done": grand_live_lives_done,
        "_won_program_ids": won_program_ids,
        "_crane_session_plushies": crane_session_plushies,
        "_scenario_facts": scenario_snapshot,
    }
    if _is_maxed(rec):
        _max_out_lineage_saddles(rec)
    return rec


MEMO_KEY = "trained_chara_memos"

_MAX_MEMO_LEN = 200


def _envelope(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _int_id_array(payload: dict, key: str) -> list | None:
    ids = payload.get(key)
    if not isinstance(ids, list) or not ids:
        return None
    out = []
    for i in ids:
        if not isinstance(i, int) or isinstance(i, bool):
            return None
        if i not in out:
            out.append(i)
    return out


def handle_change_lock_multi(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    ids = _int_id_array(payload, "trained_chara_id_array")
    lock_flag = payload.get("lock_flag")
    if ids is None or not isinstance(lock_flag, int) or isinstance(lock_flag, bool):
        return _refuse()

    _get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY) or []
    by_id = {c.get("trained_chara_id"): c for c in roster}
    if any(i not in by_id for i in ids):
        return _refuse()

    for i in ids:
        by_id[i]["is_locked"] = 1 if lock_flag else 0
    full_state[ROSTER_KEY] = roster
    state_store.save_state(viewer_id, full_state)
    return _envelope({})


def handle_change_memo(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    tcid = payload.get("trained_chara_id")
    memo = payload.get("memo")
    if not isinstance(tcid, int) or isinstance(tcid, bool):
        return _refuse()
    if memo is None:
        memo = ""
    if not isinstance(memo, str) or len(memo) > _MAX_MEMO_LEN:
        return _refuse()

    roster = _get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == tcid for c in roster):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    memos = full_state.setdefault(MEMO_KEY, {})
    if memo.strip():
        memos[str(tcid)] = memo
    else:
        memos.pop(str(tcid), None)
    state_store.save_state(viewer_id, full_state)
    return _envelope({})


def memo_for(full_state: dict, trained_chara_id) -> str:
    return (full_state.get(MEMO_KEY) or {}).get(str(trained_chara_id), "")


@functools.lru_cache(maxsize=1)
def _trade_item_by_rank() -> dict:
    return {r["trained_chara_rank"]: (r["trade_item_category"],
                                      r["trade_item_id"],
                                      r["trade_item_num"])
            for r in master_data.query(
                "SELECT trained_chara_rank, trade_item_category, trade_item_id, "
                "trade_item_num FROM trained_chara_trade_item")}


def transfer_trade_item(entry: dict):
    table = _trade_item_by_rank()
    if not table:
        return None
    rank = entry.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
        rank = _rank_for_score(entry.get("rank_score") or 0)
    if rank in table:
        return table[rank]
    top = max(table)
    return table[top] if rank > top else table[min(table)]


def handle_remove(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    ids = _int_id_array(payload, "trained_chara_id_array")
    if ids is None:
        return _refuse()

    _get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY) or []
    by_id = {c.get("trained_chara_id"): c for c in roster}
    if any(i not in by_id for i in ids):
        return _refuse()
    if any(by_id[i].get("is_locked") for i in ids):
        return _refuse()
    if any(1 <= i <= STARTER_ROSTER_SIZE for i in ids):
        return _refuse()

    payouts = [p for p in (transfer_trade_item(by_id[i]) for i in ids) if p]

    from . import archive_directory
    archive_directory.retire(full_state, roster, ids)
    doomed = set(ids)
    full_state[ROSTER_KEY] = [c for c in roster
                              if c.get("trained_chara_id") not in doomed]
    memos = full_state.get(MEMO_KEY)
    if isinstance(memos, dict):
        for i in ids:
            memos.pop(str(i), None)

    from . import transfer
    from . import shop

    owed: dict = {}
    for _category, item_id, num in payouts:
        owed[item_id] = owed.get(item_id, 0) + (num or 0)

    summary = shop._empty_summary()
    for item_id, num in owed.items():
        if num <= 0:
            continue
        before = shop._item_count(full_state, item_id)
        credited = shop._add_item(full_state, item_id, num) - before
        if credited > 0:
            summary["add_item_list"].append(
                {"item_id": item_id, "number": credited})

    state_store.save_state(viewer_id, full_state)
    return _envelope({
        "trained_chara_array": copy.deepcopy(full_state[ROSTER_KEY]),
        "reward_summary_info": summary,
        "transfer_event_info": transfer.transfer_event_info(viewer_id),
    })


_HISTORY_TYPE_OWN_CAREER = 1


def _epoch_of(create_time) -> int:
    if not isinstance(create_time, str) or not create_time.strip():
        return 0
    try:
        stamp = datetime.strptime(create_time.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())


def handle_get_succession_history_array(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = payload.get("target_trained_chara_id")
    if not isinstance(target, int) or isinstance(target, bool):
        return _refuse()
    if target == 0:
        return _envelope({"succession_history_array": []})

    roster = _get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == target for c in roster):
        return _refuse()

    name = state_store.extract_json_path(
        viewer_id, "load_index", "$.data.user_info.name") or ""
    from .. import social
    circle = social.circle_of(viewer_id) or {}
    circle_name = circle.get("name") or ""

    children = [c for c in roster
                if target in ((c.get("succession_trained_chara_id_1") or 0),
                              (c.get("succession_trained_chara_id_2") or 0))]
    children.sort(key=lambda c: (_epoch_of(c.get("create_time")),
                                 c.get("trained_chara_id") or 0))
    out = []
    for i, child in enumerate(children, start=1):
        out.append({
            "id": i,
            "viewer_id": "<redacted>",
            "trained_chara_id": child.get("trained_chara_id"),
            "hisotry_type": _HISTORY_TYPE_OWN_CAREER,
            "succession_card_id": child.get("card_id") or 0,
            "date": _epoch_of(child.get("create_time")),
            "user_name": name,
            "circle_name": circle_name,
        })
    return _envelope({"succession_history_array": out})
