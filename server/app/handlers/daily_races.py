
from __future__ import annotations

import copy
import functools
import logging
import random
import zlib

from .. import master_data
from .. import patch
from .. import race_conditions
from .. import rating_formula
from .. import state as state_store
from ..simulation import race_simulator
from . import limited_shop, practice_race, registry, shop, trained_chara
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

STATE_KEY = "daily_race_state"

_DROP_NORMAL_BY_DIFFICULTY = {1: 1000, 2: 2000, 3: 3000, 4: 4000,
                              5: 5000}
_DROP_RARE_VALUES = (600, 800, 900, 1200)
_DROP_RARE_VALUES_BY_DIFFICULTY = {5: (2900, 3000, 3200)}
_DROP_RARE_ROLLS_BY_RANK = {1: 5, 2: 4, 3: 3, 4: 2, 5: 2}
_SUPPORT_PT_RATE = 0.25
_SUPPORT_PT_NORMAL_BY_DIFFICULTY = {2: 400, 3: 600, 4: 800}
_SUPPORT_PT_RARE_VALUES_BY_DIFFICULTY = {2: (70, 80, 100),
                                        3: (100, 120, 150),
                                        4: (150, 200, 260)}
_ITEM_RACE_CATEGORY = 11
_ITEM_RACE_NORMAL = (93, 44, 1)
_MEDAL_ITEM = (161, 134)
_MEDAL_DAILY, _MEDAL_LEGEND = 10, 30

_LEGEND_WIN_DROP = (91, 59, 4000)
_LEGEND_LOSS_DROP = (30, 110, 1000)
_TROPHY_REWARD = (90, 43, 100)

_TICKET_ITEM_ID = 96
_LEGEND_TICKET_ITEM_ID = 168
_TICKET_CAP = 3

_RACE_ITEM_CATEGORY = 150
_EFFECT_MOOD, _EFFECT_WEATHER, _EFFECT_GROUND, _EFFECT_BRACKET = 10, 20, 30, 40
_MOOD_GREAT = 5
_BRACKETS = {1: range(1, 4), 2: range(6, 9)}

_CAMPAIGN_TARGET_DAILY_RACE = 2
_CAMPAIGN_EFFECT_RACE_COUNT = 2

_ITEM_TYPE_CARAT = 90
_ITEM_TYPE_MONEY = 91
_ITEM_TYPE_PIECE = 102

_RESET_UTC_HOUR = 20


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _already_played() -> dict:
    return refuse(ResultCode.NOT_ENOUGH_ERROR)


@functools.lru_cache(maxsize=1)
def _billing() -> tuple:
    row = master_data.query_one(
        "SELECT frequency, pay_cost FROM daily_race_billing ORDER BY id LIMIT 1")
    if row is None:
        return (0, 0)
    return ((row["frequency"] or 0), (row["pay_cost"] or 0))


def _served_day() -> int:
    return (patch._servertime() - _RESET_UTC_HOUR * 3600) // 86400


def ticket_cap(now: int | None = None) -> int:
    now = int(now if now is not None else patch._servertime())
    try:
        row = master_data.query_one(
            "SELECT COALESCE(SUM(effect_value_1), 0) AS bonus FROM campaign_data "
            "WHERE target_type=? AND effect_type_1=? AND start_time<=? AND end_time>=?",
            (_CAMPAIGN_TARGET_DAILY_RACE, _CAMPAIGN_EFFECT_RACE_COUNT, now, now))
    except Exception:
        log.exception("daily race RaceCount campaign lookup failed; using the base cap")
        return _TICKET_CAP
    return _TICKET_CAP + int(row["bonus"] if row else 0)


def _daily_state(full_state: dict) -> dict:
    drs = full_state.setdefault(STATE_KEY, {})
    day = _served_day()
    if drs.get("day") != day:
        drs["day"] = day
        drs["daily"] = {}
        drs["legend"] = {}
        drs["purchase_num"] = 0
        drs.pop("pending", None)
    drs.setdefault("daily", {})
    drs.setdefault("legend", {})
    drs.setdefault("purchase_num", 0)
    drs.setdefault("daily_cleared_ever", [])
    drs.setdefault("legend_cleared_ever", [])
    drs.setdefault("entries_total", 0)
    if drs.get("ticket_day") != day:
        drs["ticket_day"] = day
        for item_id, cap in ((_TICKET_ITEM_ID, ticket_cap()),
                             (_LEGEND_TICKET_ITEM_ID, _legend_ticket_cap())):
            have = _item_count(full_state, item_id)
            if have < cap:
                _add_item(full_state, item_id, cap - have)
    return drs


def refresh_tickets(full_state: dict) -> bool:
    stale = (full_state.get(STATE_KEY) or {}).get("ticket_day") != _served_day()
    _daily_state(full_state)
    return stale


@functools.lru_cache(maxsize=1)
def _legend_ticket_cap() -> int:
    row = master_data.query_one(
        "SELECT limit_num FROM item_data WHERE id=?", (_LEGEND_TICKET_ITEM_ID,))
    return (row["limit_num"] if row else 0) or 1


def _items(full_state: dict) -> list:
    return full_state.setdefault("item_list_state", [])


def _item_count(full_state: dict, item_id: int) -> int:
    return next((i.get("number", 0) for i in _items(full_state)
                 if i.get("item_id") == item_id), 0)


def _add_item(full_state: dict, item_id: int, delta: int) -> int:
    for i in _items(full_state):
        if i.get("item_id") == item_id:
            i["number"] = max(0, (i.get("number") or 0) + delta)
            return i["number"]
    if delta > 0:
        _items(full_state).append({"item_id": item_id, "number": delta})
        return delta
    return 0


def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


def _grant(full_state: dict, category: int, item_id: int, num: int, summary: dict) -> None:
    if not item_id or num <= 0:
        return
    if category == _ITEM_TYPE_CARAT:
        wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
        wallet["fcoin"] = (wallet.get("fcoin") or 0) + num
        summary["add_fcoin"] += num
    elif category == _ITEM_TYPE_PIECE:
        pieces = full_state.setdefault("piece_list_state", [])
        p = next((p for p in pieces if p.get("piece_id") == item_id), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + num
        else:
            pieces.append({"piece_id": item_id, "piece_num": num})
        summary["add_piece_list"].append({"piece_id": item_id, "piece_num": num})
    else:
        _add_item(full_state, item_id, num)
        summary["add_item_list"].append({"item_id": item_id, "number": num})


def _grant_row_items(full_state: dict, row, prefix: str, summary: dict) -> list[dict]:
    entries = []
    for i in (1, 2, 3):
        cat = row[f"{prefix}_item_category_{i}"] or 0
        iid = row[f"{prefix}_item_id_{i}"] or 0
        num = row[f"{prefix}_item_num_{i}"] or 0
        if not iid or num <= 0:
            continue
        if cat == _ITEM_TYPE_PIECE:
            iid = _piece_id_for_card(iid)
        _grant(full_state, cat, iid, num, summary)
        entries.append({"item_type": cat, "item_id": iid, "item_num": num})
    return entries


def _piece_id_for_card(card_id: int) -> int:
    row = master_data.query_one(
        "SELECT get_piece_id FROM card_data WHERE id=?", (card_id,))
    return row["get_piece_id"] if row and row["get_piece_id"] else card_id


def _daily_race_row(daily_race_id):
    return master_data.query_one(
        "SELECT * FROM daily_race WHERE id=?", (daily_race_id,))


def _legend_race_row(daily_legend_race_id):
    return master_data.query_one(
        "SELECT * FROM daily_legend_race WHERE id=?", (daily_legend_race_id,))


def _boss_row(boss_npc_id):
    return master_data.query_one(
        "SELECT * FROM legend_race_boss_npc WHERE id=?", (boss_npc_id,))


def _daily_npc_group(race_instance_id: int) -> int:
    return 5800 + (race_instance_id - 580000)


def _legend_npc_group(boss_npc_id: int) -> int:
    return (boss_npc_id - 103) // 2 + 20


def _npc_rows(table: str, group_id: int) -> list:
    return master_data.query(
        f"SELECT * FROM {table} WHERE npc_group_id=? ORDER BY id", (group_id,))


_skill_set_cache: dict = {}


def _skill_array_from_set(skill_set_id: int) -> list[dict]:
    if skill_set_id in _skill_set_cache:
        return copy.deepcopy(_skill_set_cache[skill_set_id])
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    skills = []
    if row is not None:
        for i in range(1, 21):
            sid = row[f"skill_id{i}"] or 0
            if sid:
                skills.append({"skill_id": sid, "level": row[f"skill_level{i}"] or 1})
    _skill_set_cache[skill_set_id] = skills
    return copy.deepcopy(skills)


def _base_card_for_chara(chara_id: int) -> int:
    row = master_data.query_one(
        "SELECT id FROM card_data WHERE chara_id=? ORDER BY id LIMIT 1", (chara_id,))
    return row["id"] if row else 0


def _best_running_style(row) -> int:
    apts = {1: row["proper_running_style_nige"], 2: row["proper_running_style_senko"],
            3: row["proper_running_style_sashi"], 4: row["proper_running_style_oikomi"]}
    return max(apts, key=lambda k: apts[k] or 0)


_NPC_STAT_AND_APT = ("speed", "stamina", "pow", "guts", "wiz",
                     "proper_distance_short", "proper_distance_mile",
                     "proper_distance_middle", "proper_distance_long",
                     "proper_running_style_nige", "proper_running_style_senko",
                     "proper_running_style_sashi", "proper_running_style_oikomi",
                     "proper_ground_turf", "proper_ground_dirt")


def _npc_chara(row) -> dict:
    chara = {k: row[k] or 0 for k in _NPC_STAT_AND_APT}
    chara_id = row["chara_id"] or 0
    dress = row["race_dress_id"] or 0
    named = chara_id > 1
    chara.update({
        "viewer_id": 0,
        "trainer_name": None,
        "trained_chara_id": row["id"],
        "single_mode_chara_id": row["id"],
        "chara_id": chara_id if named else 1,
        "card_id": 0,
        "mob_id": row["mob_id"] or 0,
        "race_dress_id": dress if dress >= 8 else None,
        "running_style": _best_running_style(row),
        "skill_array": _skill_array_from_set(row["skill_set_id"] or 0),
        "motivation": 3,
        "rarity": 3 if named else 1,
        "talent_level": 1,
        "npc_type": 0,
        "rank": _rank_for_stats(chara),
        "fans": 0,
        "wins": 0,
    })
    return chara


def _boss_chara(row) -> dict:
    chara = {k: row[k] or 0 for k in _NPC_STAT_AND_APT}
    chara.update({
        "viewer_id": 0,
        "trainer_name": None,
        "trained_chara_id": row["id"],
        "single_mode_chara_id": row["id"],
        "chara_id": row["chara_id"],
        "card_id": (row["card_rarity_data_id"] or 0) // 100 or _base_card_for_chara(row["chara_id"]),
        "mob_id": 0,
        "race_dress_id": row["race_dress_id"],
        "nickname_id": row["nickname_id"] or 0,
        "running_style": _best_running_style(row),
        "skill_array": _skill_array_from_set(row["skill_set_id"] or 0),
        "motivation": 3,
        "rarity": 3,
        "talent_level": 1,
        "npc_type": 3,
        "rank": _rank_for_stats(chara),
        "fans": 0,
        "wins": 0,
    })
    return chara


def index_boss_data(boss_npc_id: int, boss=None) -> dict:
    boss = boss if boss is not None else _boss_row(boss_npc_id)
    if boss is None:
        return {}
    wire = _race_horse_array([_boss_chara(boss)])[0]
    wire.update({
        "frame_order": None,
        "running_style": None,
        "trainer_name": None,
        "trained_chara_id": boss["id"],
        "single_mode_chara_id": boss["id"],
        "npc_type": 3,
        "talent_level": 1,
        "motivation": 0,
        "popularity": 0,
        "popularity_mark_rank_array": [0, 0, 0],
    })
    return wire


def _rank_for_stats(chara: dict) -> int:
    score = rating_formula.get_rating(
        [chara.get(k, 0) for k in ("speed", "stamina", "pow", "guts", "wiz")])
    return trained_chara._rank_for_score(score)


def _entry_num(race_instance_id: int) -> int:
    row = master_data.query_one(
        "SELECT r.entry_num AS n FROM race_instance ri JOIN race r ON r.id=ri.race_id "
        "WHERE ri.id=?", (race_instance_id,))
    return (row["n"] if row else 18) or 18


def _record_last_start(drs: dict, family: str, race_id, trained_chara_id) -> None:
    drs.setdefault("last_start", {})[family] = {
        "race_id": int(race_id or 0), "trained_chara_id": int(trained_chara_id or 0)}


def _last_start_info(drs: dict, family: str) -> dict:
    return dict((drs.get("last_start") or {}).get(family)
                or {"race_id": 0, "trained_chara_id": 0})


def _find_veteran(viewer_id, trained_chara_id):
    roster = trained_chara._get_or_seed_roster(viewer_id)
    return next((c for c in roster if c.get("trained_chara_id") == trained_chara_id), None)


def _field_seed(viewer_id, race_id: int, extra: int = 0) -> int:
    return zlib.crc32(f"{viewer_id}:{race_id}:{_served_day()}:{extra}".encode())


def _roll_daily_conditions(viewer_id, race_id: int) -> tuple:
    seed = _field_seed(viewer_id, race_id, extra=100)
    rng = random.Random(seed)
    season = rng.randint(1, 5)
    row = _daily_race_row(race_id)
    cond = race_conditions.roll(row["race_instance_id"] if row else 0, rng, season=season)
    return season, cond["weather"], cond["ground_condition"]


def _daily_opponents(race_row, viewer_id) -> list[dict]:
    group = _daily_npc_group(race_row["race_instance_id"])
    pool = _npc_rows("daily_race_npc", group)
    if not pool:
        return []
    rng = random.Random(_field_seed(viewer_id, race_row["id"]))
    real = [r for r in pool if (r["chara_id"] or 0) > 1]
    mobs = [r for r in pool if (r["chara_id"] or 0) <= 1]
    want = _entry_num(race_row["race_instance_id"]) - 1
    n_real = min(len(real), race_row["unique_chara_npc_max"] or 0, want)
    picked = rng.sample(real, n_real) + rng.sample(mobs, min(len(mobs), want - n_real))
    rng.shuffle(picked)
    return [_npc_chara(r) for r in picked]


def _legend_opponents(legend_row, viewer_id) -> list[dict]:
    boss = _boss_row(legend_row["legend_race_boss_npc_id"])
    if boss is None:
        return []
    group = _legend_npc_group(boss["id"])
    pool = _npc_rows("legend_race_npc", group)
    if not pool:
        near = master_data.query_one(
            "SELECT npc_group_id AS g FROM legend_race_npc "
            "ORDER BY ABS(npc_group_id - ?) LIMIT 1", (group,))
        pool = _npc_rows("legend_race_npc", near["g"]) if near else []
    rng = random.Random(_field_seed(viewer_id, legend_row["id"]))
    want = _entry_num(legend_row["race_instance_id"]) - 2
    picked = rng.sample(pool, min(len(pool), max(0, want)))
    return [_boss_chara(boss)] + [_npc_chara(r) for r in picked]


def _race_horse_array(horses: list[dict], gate_assignment: list[int] | None = None) -> list[dict]:
    gates = list(gate_assignment) if gate_assignment else list(range(len(horses)))

    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}

    pop = _rank_by(lambda i: -(horses[i].get("speed") or 0))
    sta = _rank_by(lambda i: -(horses[i].get("stamina") or 0))
    pw = _rank_by(lambda i: -(horses[i].get("pow", horses[i].get("power", 0)) or 0))
    return [
        practice_race._build_race_horse_entry(
            chara,
            frame_order=gates[i] + 1,
            final_grade=chara.get("rank", 1),
            popularity=pop[i],
            popularity_mark_rank_array=[pop[i], sta[i], pw[i]],
        )
        for i, chara in enumerate(horses)
    ]


def _fallback_rank(player: dict, opponents: list[dict], seed: int) -> int:
    rng = random.Random(seed)

    def score(c):
        base = ((c.get("speed") or 0) + 0.8 * (c.get("pow", c.get("power", 0)) or 0)
                + 0.6 * (c.get("stamina") or 0) + 0.4 * (c.get("guts") or 0)
                + 0.4 * (c.get("wiz", c.get("wisdom", 0)) or 0))
        return base * rng.uniform(0.95, 1.05)

    scores = [score(player)] + [score(o) for o in opponents]
    return 1 + sum(1 for s in scores[1:] if s > scores[0])


def _run_race(player: dict, opponents: list[dict], race_instance_id: int,
              season: int, weather: int, ground: int,
              gate_assignment: list[int] | None = None, race_type: int = 10) -> dict:
    try:
        result = race_simulator.simulate_race(
            player, opponents, race_instance_id,
            ground_condition=ground, weather=weather, season=season,
            gate_assignment=gate_assignment, race_context={"race_type": race_type})
    except Exception:
        log.exception("daily race simulation failed, using fallback outcome")
        result = None
    if result is not None:
        rank = result["sim_results"][0]["finishOrder"] + 1
        return {"rank": rank, "race_scenario": result["race_scenario"],
                "seed": result["seed"], "horses": result["horses"],
                "sim_results": result["sim_results"]}
    seed = random.randint(0, 2**31 - 1)
    return {"rank": _fallback_rank(player, opponents, seed),
            "race_scenario": "", "seed": seed,
            "horses": [player] + opponents, "sim_results": None}


def _normal_reward_amount(cat: int, difficulty: int) -> int:
    base = _DROP_NORMAL_BY_DIFFICULTY.get(difficulty or 1, 1000)
    if cat != 30:
        return base
    confirmed = _SUPPORT_PT_NORMAL_BY_DIFFICULTY.get(difficulty)
    return confirmed if confirmed is not None else max(1, int(base * _SUPPORT_PT_RATE))


def _rare_reward_pool(cat: int, difficulty: int) -> tuple:
    if cat != 30:
        return _DROP_RARE_VALUES_BY_DIFFICULTY.get(difficulty, _DROP_RARE_VALUES)
    confirmed = _SUPPORT_PT_RARE_VALUES_BY_DIFFICULTY.get(difficulty)
    if confirmed is not None:
        return confirmed
    return tuple(max(1, int(v * _SUPPORT_PT_RATE)) for v in _DROP_RARE_VALUES)


def _medal_enabled() -> bool:
    return master_data.has_table("training_report_pass")


def _run_rewards(race_row, rank: int) -> tuple[list, list]:
    cat = race_row["pick_up_item_category_1"] or _ITEM_TYPE_MONEY
    iid = race_row["pick_up_item_id_1"] or 59
    difficulty = race_row["difficulty"] or 1
    rolls = _DROP_RARE_ROLLS_BY_RANK.get(rank, 1)
    rng = random.Random()
    if cat == _ITEM_RACE_CATEGORY:
        normal = [dict(zip(("item_type", "item_id", "item_num"), _ITEM_RACE_NORMAL))]
        picks = [(race_row[f"pick_up_item_category_{i}"], race_row[f"pick_up_item_id_{i}"])
                 for i in (1, 2, 3) if race_row[f"pick_up_item_id_{i}"]]
        rare = [{"item_type": c, "item_id": i, "item_num": 1}
                for c, i in (rng.choice(picks) for _ in range(rolls))]
    else:
        normal = [{"item_type": cat, "item_id": iid,
                   "item_num": _normal_reward_amount(cat, difficulty)}]
        pool = _rare_reward_pool(cat, difficulty)
        rare = [{"item_type": cat, "item_id": iid, "item_num": rng.choice(pool)}
                for _ in range(rolls)]
    if _medal_enabled():
        normal.append({"item_type": _MEDAL_ITEM[0], "item_id": _MEDAL_ITEM[1],
                       "item_num": _MEDAL_DAILY})
    return normal, rare


def _award_daily(full_state: dict, drs: dict, race_row, rank: int) -> dict:
    summary = _empty_summary()
    rid = race_row["id"]
    first_clear = []
    if rank == 1 and rid not in drs["daily_cleared_ever"]:
        first_clear = _grant_row_items(full_state, race_row, "first_clear", summary)
        drs["daily_cleared_ever"].append(rid)

    normal, rare = _run_rewards(race_row, rank)
    for r in normal + rare:
        _grant(full_state, r["item_type"], r["item_id"], r["item_num"], summary)

    rec = drs["daily"].setdefault(str(rid), {"is_played": 0, "is_cleared": 0})
    rec["is_played"] = 1
    drs["entries_total"] += 1
    if rank == 1:
        rec["is_cleared"] = 1
    return {"first_clear_reward_array": first_clear, "normal_reward_array": normal,
            "rare_reward_array": rare, "bonus_reward_array": [],
            "reward_summary_info": summary}


def _skip_reward_entry(full_state: dict, drs: dict, race_row, rank: int) -> dict:
    summary = _empty_summary()
    rid = race_row["id"]
    normal, rare = _run_rewards(race_row, rank)
    if rank == 1 and rid not in drs["daily_cleared_ever"]:
        _grant_row_items(full_state, race_row, "first_clear", summary)
        drs["daily_cleared_ever"].append(rid)
    for r in normal + rare:
        _grant(full_state, r["item_type"], r["item_id"], r["item_num"], summary)

    rec = drs["daily"].setdefault(str(rid), {"is_played": 0, "is_cleared": 0})
    rec["is_played"] = 1
    if rank == 1:
        rec["is_cleared"] = 1
    return {"rank": rank, "normal_reward_array": normal, "rare_reward_array": rare,
            "bonus_reward_array": [], "reward_summary_info": summary}


def _legend_trophy(full_state: dict, viewer_id, legend_row, player: dict | None):
    from . import user_profile
    row = master_data.query_one(
        "SELECT trophy_id FROM race_trophy WHERE race_instance_id=? ORDER BY id LIMIT 1",
        (legend_row["race_instance_id"],))
    if row is None:
        return None, None
    st = user_profile._trophy_state(full_state, viewer_id)
    if str(row["trophy_id"]) in st["trophies"]:
        return None, None
    chara_id = (player or {}).get("chara_id") or ((player or {}).get("card_id") or 0) // 100
    st["trophies"][str(row["trophy_id"])] = {
        "create_time": user_profile._now(),
        "charas": {str(chara_id): 1} if chara_id else {}}
    cat, iid, num = _TROPHY_REWARD
    wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
    wallet["fcoin"] = (wallet.get("fcoin") or 0) + num
    return ({"trophy_id": row["trophy_id"], "chara_id_array": [chara_id] if chara_id else []},
            {"item_type": cat, "item_id": iid, "item_num": num})


def _award_legend(full_state: dict, drs: dict, legend_row, rank: int,
                  viewer_id=None, player: dict | None = None) -> dict:
    summary = _empty_summary()
    rid = legend_row["id"]
    first_clear, victory, drop = [], [], []
    trophy, trophy_reward = None, None
    if rank == 1:
        if rid not in drs["legend_cleared_ever"]:
            first_clear = _grant_row_items(full_state, legend_row, "first_clear", summary)
            drs["legend_cleared_ever"].append(rid)
        victory = _grant_row_items(full_state, legend_row, "pick_up", summary)
        if viewer_id is not None:
            trophy, trophy_reward = _legend_trophy(full_state, viewer_id, legend_row, player)
    if _medal_enabled():
        drop.append({"item_type": _MEDAL_ITEM[0], "item_id": _MEDAL_ITEM[1],
                     "item_num": _MEDAL_LEGEND})
    cat, iid, num = _LEGEND_WIN_DROP if rank == 1 else _LEGEND_LOSS_DROP
    drop.append({"item_type": cat, "item_id": iid, "item_num": num})
    for r in drop:
        _grant(full_state, r["item_type"], r["item_id"], r["item_num"], summary)

    rec = drs["legend"].setdefault(str(rid), {"is_played": 0, "is_cleared": 0})
    rec["is_played"] = 1
    if rank == 1:
        rec["is_cleared"] = 1
    return {"first_clear_reward_array": first_clear, "drop_reward_array": drop,
            "victory_reward_array": victory, "reward_summary_info": summary,
            "add_trophy_info": trophy, "trophy_reward_info": trophy_reward}


def _do_race_entry(payload: dict, family: str) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)

    if family == "daily":
        race_id = payload.get("daily_race_id")
        row = _daily_race_row(race_id)
        if row is not None and _item_count(full_state, _TICKET_ITEM_ID) < (row["cost_num"] or 1):
            return _already_played()
    else:
        race_id = payload.get("daily_legend_race_id")
        row = _legend_race_row(race_id)
        if row is not None and _item_count(full_state, _LEGEND_TICKET_ITEM_ID) < (row["cost_num"] or 1):
            return _already_played()

    player = _find_veteran(viewer_id, payload.get("trained_chara_id"))
    if row is None or player is None:
        return _refuse()

    opponents = (_daily_opponents(row, viewer_id) if family == "daily"
                 else _legend_opponents(row, viewer_id))
    if not opponents:
        return _refuse()

    horses = [dict(copy.deepcopy(player), npc_type=11)] + opponents
    _record_last_start(drs, family, race_id, payload.get("trained_chara_id"))
    if family == "daily":
        season, weather, ground = _roll_daily_conditions(viewer_id, race_id)
    else:
        season = (row["season"] if row else 1) or 1
        weather = (row["weather"] if row else 1) or 1
        ground = (row["ground"] if row else 1) or 1
    seed = random.randint(0, 2**31 - 1)
    course_info = race_simulator.get_course_for_race_instance(row["race_instance_id"])
    course_entry_num = course_info[2] if course_info else len(horses)
    gate_assignment = random.sample(range(max(course_entry_num, len(horses))), len(horses))
    drs["pending"] = {
        "family": family,
        "race_id": race_id,
        "trained_chara_id": payload.get("trained_chara_id"),
        "item_id_array": [],
        "result_rank": None,
        "season": season,
        "weather": weather,
        "ground": ground,
        "gate_assignment": gate_assignment,
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "race_horse_data_array": copy.deepcopy(
            _race_horse_array(horses, gate_assignment)),
        "race_scenario": None,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_horse_data_array": _race_horse_array(horses, gate_assignment),
        "season": season,
        "weather": weather,
        "ground_condition": ground,
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "state": 1,
        "trained_chara_id": payload.get("trained_chara_id"),
    })


def _bracket(gate: int, field_size: int) -> int:
    base, extra = divmod(max(field_size, 8), 8)
    single = 8 - extra
    if gate < single * base:
        return gate // base + 1
    return single + (gate - single * base) // (base + 1) + 1


def apply_race_items(full_state: dict, pending: dict, item_ids) -> list[int]:
    used = []
    horses = pending.get("race_horse_data_array") or []
    gates = pending.get("gate_assignment") or []
    for item_id in dict.fromkeys(item_ids or []):
        if type(item_id) is not int or _item_count(full_state, item_id) < 1:
            continue
        row = master_data.query_one("SELECT * FROM item_data WHERE id=?", (item_id,))
        if not row or row["item_category"] != _RACE_ITEM_CATEGORY:
            continue
        for n in (1, 2):
            effect, target, value = (row[f"effect_type_{n}"], row[f"effect_target_{n}"],
                                     row[f"effect_value_{n}"])
            if effect == _EFFECT_MOOD:
                pending["motivation"] = _MOOD_GREAT
                if horses:
                    horses[0].update(motivation=_MOOD_GREAT, motivation_change_flag=1)
            elif effect == _EFFECT_WEATHER:
                pending["weather"] = value
            elif effect == _EFFECT_GROUND:
                pending["ground"] = value
            elif effect == _EFFECT_BRACKET and target in _BRACKETS and gates and horses:
                _move_to_bracket(horses, gates, _BRACKETS[target])
        _add_item(full_state, item_id, -1)
        used.append(item_id)
    if used and horses:
        horses[0]["item_id_array"] = list(used)
    pending["item_id_array"] = list(used)
    return used


def _move_to_bracket(horses: list[dict], gates: list[int], brackets: range) -> None:
    field = max(max(gates) + 1, len(gates))
    if _bracket(gates[0], field) in brackets:
        return
    choices = [g for g in range(field) if _bracket(g, field) in brackets]
    if not choices:
        return
    target = random.choice(choices)
    if target in gates:
        other = gates.index(target)
        gates[other] = gates[0]
        if other < len(horses):
            horses[other]["frame_order"] = gates[0] + 1
    gates[0] = target
    horses[0].update(frame_order=target + 1, frame_order_change_flag=1)


def reflect_reply(full_state: dict, pending: dict, ticket: int, used: list[int]) -> dict:
    info = [{"item_id": i, "number": _item_count(full_state, i)} for i in [ticket] + used]
    if not used:
        return {"weather": None, "ground_condition": None, "race_horse_data_array": None,
                "item_info_array": info, "state": 2}
    return {"weather": pending.get("weather"), "ground_condition": pending.get("ground"),
            "race_horse_data_array": copy.deepcopy(pending.get("race_horse_data_array") or []),
            "item_info_array": info, "state": 2}


def _do_reflect_item_effect(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending")
    ticket = (_LEGEND_TICKET_ITEM_ID
              if (pending or {}).get("family") == "legend" else _TICKET_ITEM_ID)
    used = []
    if pending is not None:
        used = apply_race_items(full_state, pending, payload.get("item_id_array"))
        _add_item(full_state, ticket, -1)
        state_store.save_state(viewer_id, full_state)
    return _ok(reflect_reply(full_state, pending or {}, ticket, used))


def _do_race_start(payload: dict, family: str) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending")
    if not pending or pending.get("family") != family:
        return _refuse()

    if family == "daily":
        row = _daily_race_row(pending["race_id"])
        season = pending.get("season", 1)
        weather = pending.get("weather", 1)
        ground = pending.get("ground", 1)
    else:
        row = _legend_race_row(pending["race_id"])
        season = pending.get("season") or (row["season"] if row else 1) or 1
        weather = pending.get("weather") or (row["weather"] if row else 1) or 1
        ground = pending.get("ground") or (row["ground"] if row else 1) or 1
    player = _find_veteran(viewer_id, pending["trained_chara_id"])
    if row is None or player is None:
        return _refuse()

    player = dict(player, npc_type=11)
    if pending.get("motivation"):
        player["motivation"] = pending["motivation"]
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    opponents = (_daily_opponents(row, viewer_id) if family == "daily"
                 else _legend_opponents(row, viewer_id))

    outcome = _run_race(player, opponents, row["race_instance_id"], season, weather, ground,
                        gate_assignment=pending.get("gate_assignment"))
    pending["result_rank"] = outcome["rank"]
    pending["running_style"] = payload.get("running_style")
    pending["race_scenario"] = outcome["race_scenario"]
    state_store.save_state(viewer_id, full_state)

    log.info("%s_race/race_start: race=%s player=%s rank=%d simulated=%s",
             family if family == "daily" else "daily_legend",
             pending["race_id"], pending["trained_chara_id"], outcome["rank"],
             outcome["sim_results"] is not None)
    return _ok({
        "race_scenario": outcome["race_scenario"],
        "state": 4,
        "running_style": payload.get("running_style") or player.get("running_style") or 1,
    })


def _do_replay_check(payload: dict, family: str) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending")
    if not pending or pending.get("family") != family or pending.get("result_rank") is None:
        return _refuse()

    rank = pending["result_rank"]
    if family == "daily":
        row = _daily_race_row(pending["race_id"])
        if row is None:
            return _refuse()
        reward = _award_daily(full_state, drs, row, rank)
        body = {
            "rank": rank,
            "first_clear_reward_array": reward["first_clear_reward_array"],
            "normal_reward_array": reward["normal_reward_array"],
            "rare_reward_array": reward["rare_reward_array"],
            "bonus_reward_array": reward["bonus_reward_array"],
            "reward_summary_info": reward["reward_summary_info"],
            "state": 0,
        }
    else:
        row = _legend_race_row(pending["race_id"])
        if row is None:
            return _refuse()
        reward = _award_legend(full_state, drs, row, rank, viewer_id,
                               _find_veteran(viewer_id, pending.get("trained_chara_id")))
        body = {
            "rank": rank,
            "first_clear_reward_array": reward["first_clear_reward_array"],
            "drop_reward_array": reward["drop_reward_array"],
            "victory_reward_array": reward["victory_reward_array"],
            "reward_summary_info": reward["reward_summary_info"],
            "state": 0,
            "add_trophy_info": reward["add_trophy_info"],
            "trophy_reward_info": reward["trophy_reward_info"],
        }

    drs.pop("pending", None)
    shop_info = limited_shop.roll(full_state, "daily_race" if family == "daily" else "legend_race")
    from . import campaign_walking
    walk_gauge_info = campaign_walking.gauge_info(
        full_state, "dailyrace" if family == "daily" else "dailylegendrace")
    from . import factor_research
    research_gauge = factor_research.add_gauge(full_state, 30 if family == "daily" else 35)
    state_store.save_state(viewer_id, full_state)
    body.update({
        "limited_shop_info": shop_info,
        "campaign_walking_gauge_info": walk_gauge_info,
        "factor_research_gauge_info": research_gauge,
    })
    return _ok(body)


@registry.endpoint("daily_race/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)

    records = []
    for row in master_data.query("SELECT id FROM daily_race ORDER BY id"):
        rec = drs["daily"].get(str(row["id"]), {})
        records.append({
            "daily_race_id": row["id"],
            "is_cleared": 1 if (row["id"] in drs["daily_cleared_ever"]
                                or rec.get("is_cleared")) else 0,
            "is_played": rec.get("is_played", 0),
        })
    state_store.save_state(viewer_id, full_state)
    return _ok({"daily_race_record_array": records,
                "purchase_num": drs["purchase_num"],
                "subscription_is_valid": False,
                "last_start_info": _last_start_info(drs, "daily")})


@registry.endpoint("daily_race/race_entry")
def handle_race_entry(payload: dict) -> dict:
    return _do_race_entry(payload, "daily")


@registry.endpoint("daily_race/reflect_item_effect")
def handle_reflect_item_effect(payload: dict) -> dict:
    return _do_reflect_item_effect(payload)


@registry.endpoint("daily_race/race_start")
def handle_race_start(payload: dict) -> dict:
    return _do_race_start(payload, "daily")


@registry.endpoint("daily_race/replay_check")
def handle_replay_check(payload: dict) -> dict:
    return _do_replay_check(payload, "daily")


@registry.endpoint("daily_race/recovery_ticket")
def handle_recovery_ticket(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)

    frequency, cost = _billing()
    if (drs.get("purchase_num") or 0) >= frequency:
        return _refuse()
    wallet = shop.spend_carats(full_state, cost)
    if wallet is None:
        return _refuse()

    summary = _empty_summary()
    cap = ticket_cap()
    _add_item(full_state, _TICKET_ITEM_ID, cap)
    summary["add_item_list"].append({"item_id": _TICKET_ITEM_ID, "number": cap})
    drs["purchase_num"] = (drs.get("purchase_num") or 0) + 1
    state_store.save_state(viewer_id, full_state)
    return _ok({"coin_info": copy.deepcopy(wallet),
                "reward_summary_info": summary,
                "purchase_num": drs["purchase_num"]})


@registry.endpoint("daily_race_skip/race_skip")
def handle_race_skip(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)

    row = _daily_race_row(payload.get("daily_race_id"))
    player = _find_veteran(viewer_id, payload.get("trained_chara_id"))
    count = int(payload.get("race_skip_count") or 0)
    if row is None or player is None or count <= 0:
        return _refuse()
    if _item_count(full_state, _TICKET_ITEM_ID) < count:
        return _refuse()
    _add_item(full_state, _TICKET_ITEM_ID, -count)
    _record_last_start(drs, "daily", row["id"], payload.get("trained_chara_id"))
    drs["entries_total"] += count

    player = dict(player)
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    opponents = _daily_opponents(row, viewer_id)

    from . import campaign_walking
    rewards = []
    shop_info = None
    any_appeared = False
    walk_before = walk_after = None
    for i in range(count):
        rank = _fallback_rank(player, opponents, _field_seed(viewer_id, row["id"], extra=i + 1))
        rewards.append(_skip_reward_entry(full_state, drs, row, rank))
        rolled = limited_shop.roll(full_state, "daily_race")
        if rolled is not None:
            any_appeared = any_appeared or bool(rolled["appear_flag"])
            shop_info = rolled
        walked = campaign_walking.add_gauge(full_state, "dailyrace")
        if walked is not None:
            walk_before = walk_before if walk_before is not None else walked[0]
            walk_after = walked[1]
    if shop_info is not None:
        shop_info = dict(shop_info)
        shop_info["appear_flag"] = 1 if any_appeared else 0
    walk_gauge_info = ({"before_gauge": walk_before, "after_gauge": walk_after}
                       if walk_after is not None else None)

    from . import factor_research
    research_gauge = factor_research.add_gauge(full_state, 30 * count)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_reward_array": rewards,
        "item_info_array": [{"item_id": _TICKET_ITEM_ID,
                             "number": _item_count(full_state, _TICKET_ITEM_ID)}],
        "limited_shop_info": shop_info,
        "campaign_walking_gauge_info": walk_gauge_info,
        "factor_research_gauge_info": research_gauge,
    })


@registry.endpoint("daily_legend_race/index")
def handle_legend_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)

    records = []
    for row in master_data.query(
            "SELECT id, legend_race_boss_npc_id FROM daily_legend_race ORDER BY id"):
        rec = drs["legend"].get(str(row["id"]), {})
        records.append({
            "daily_legend_race_id": row["id"],
            "boss_data": index_boss_data(row["legend_race_boss_npc_id"]),
            "is_cleared": 1 if (row["id"] in drs["legend_cleared_ever"]
                                or rec.get("is_cleared")) else 0,
            "is_played": rec.get("is_played", 0),
        })
    state_store.save_state(viewer_id, full_state)
    return _ok({"daily_legend_race_record_array": records,
                "update_item_array": [],
                "last_start_info": _last_start_info(drs, "legend")})


@registry.endpoint("daily_legend_race/race_entry")
def handle_legend_race_entry(payload: dict) -> dict:
    return _do_race_entry(payload, "legend")


@registry.endpoint("daily_legend_race/reflect_item_effect")
def handle_legend_reflect_item_effect(payload: dict) -> dict:
    return _do_reflect_item_effect(payload)


@registry.endpoint("daily_legend_race/race_start")
def handle_legend_race_start(payload: dict) -> dict:
    return _do_race_start(payload, "legend")


@registry.endpoint("daily_legend_race/replay_check")
def handle_legend_replay_check(payload: dict) -> dict:
    return _do_replay_check(payload, "legend")


def handle_legend_race_skip(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    row = _legend_race_row(payload.get("daily_legend_race_id"))
    player = _find_veteran(viewer_id, payload.get("trained_chara_id"))
    count = payload.get("race_skip_count")
    if row is None or player is None or type(count) is not int or count <= 0 or drs.get("pending"):
        return _refuse()
    if row["id"] not in drs["legend_cleared_ever"]:
        return _refuse()
    cost = (row["cost_num"] or 1) * count
    if _item_count(full_state, _LEGEND_TICKET_ITEM_ID) < cost:
        return _already_played()
    player = dict(player)
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    opponents = _legend_opponents(row, viewer_id)
    _add_item(full_state, _LEGEND_TICKET_ITEM_ID, -cost)
    _record_last_start(drs, "legend", row["id"], payload["trained_chara_id"])
    from . import campaign_walking, factor_research
    rewards, trophies, trophy_rewards = [], [], []
    shop_info = None
    walk_before = walk_after = None
    for i in range(count):
        rank = _fallback_rank(player, opponents, _field_seed(viewer_id, row["id"], extra=i + 1))
        reward = _award_legend(full_state, drs, row, rank, viewer_id, player)
        rewards.append({"rank": rank, "normal_reward_array": reward["drop_reward_array"],
                        "rare_reward_array": reward["victory_reward_array"],
                        "bonus_reward_array": reward["first_clear_reward_array"],
                        "reward_summary_info": reward["reward_summary_info"]})
        if reward["add_trophy_info"]:
            trophies.append(reward["add_trophy_info"])
        if reward["trophy_reward_info"]:
            trophy_rewards.append(reward["trophy_reward_info"])
        shop_info = limited_shop.roll(full_state, "legend_race") or shop_info
        walked = campaign_walking.add_gauge(full_state, "dailylegendrace")
        if walked is not None:
            walk_before = walked[0] if walk_before is None else walk_before
            walk_after = walked[1]
    research_gauge = factor_research.add_gauge(full_state, 35 * count)
    state_store.save_state(viewer_id, full_state)
    return _ok({"race_reward_array": rewards, "add_trophy_info_array": trophies,
                "trophy_reward_info_array": trophy_rewards, "limited_shop_info": shop_info,
                "item_info_array": [{"item_id": _LEGEND_TICKET_ITEM_ID,
                                     "number": _item_count(full_state, _LEGEND_TICKET_ITEM_ID)}],
                "campaign_walking_gauge_info": {"before_gauge": walk_before, "after_gauge": walk_after}
                    if walk_after is not None else None,
                "factor_research_gauge_info": research_gauge})


def _preview_rewards(row) -> list:
    out = []
    for prefix in ("first_clear", "pick_up"):
        for i in (1, 2, 3):
            cat = row[f"{prefix}_item_category_{i}"] or 0
            iid = row[f"{prefix}_item_id_{i}"] or 0
            num = row[f"{prefix}_item_num_{i}"] or 0
            if not iid or num <= 0:
                continue
            if cat == _ITEM_TYPE_PIECE:
                iid = _piece_id_for_card(iid)
            out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out


def _do_reset(payload: dict, family: str) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending")
    if isinstance(pending, dict) and pending.get("family") == family:
        drs.pop("pending", None)
        state_store.save_state(viewer_id, full_state)
    return _ok({"state": 0})


def _do_resume(payload: dict, family: str, with_purchase_num: bool) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending") or {}
    if pending.get("family") != family:
        pending = {}
    if not pending:
        state = 0
    elif pending.get("result_rank") is not None:
        state = 4
    else:
        state = 2
    bucket = drs["daily"] if family == "daily" else drs["legend"]
    rec = bucket.get(str(pending.get("race_id"))) or {}
    data = {
        "race_horse_data_array": copy.deepcopy(
            pending.get("race_horse_data_array") or []),
        "season": pending.get("season") or 0,
        "weather": pending.get("weather") or 0,
        "ground_condition": pending.get("ground") or 0,
        "random_seed": pending.get("random_seed") or 0,
        "race_instance_id": pending.get("race_instance_id") or 0,
        "race_scenario": pending.get("race_scenario"),
        "state": state,
        "is_cleared": 1 if rec.get("is_cleared") else 0,
    }
    if with_purchase_num:
        data["purchase_num"] = drs["purchase_num"]
    return _ok(data)


@registry.endpoint("daily_race/get_reward_list")
def handle_get_reward_list(payload: dict) -> dict:
    row = _daily_race_row(payload.get("daily_race_id"))
    if row is None:
        return _refuse()
    return _ok({"reward_array": _preview_rewards(row)})


@registry.endpoint("daily_race/reset")
def handle_reset(payload: dict) -> dict:
    return _do_reset(payload, "daily")


@registry.endpoint("daily_race/resume")
def handle_resume(payload: dict) -> dict:
    return _do_resume(payload, "daily", with_purchase_num=True)


@registry.endpoint("daily_race/pre_replay_check")
def handle_pre_replay_check(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    drs = _daily_state(full_state)
    pending = drs.get("pending") or {}
    ready = (pending.get("family") == "daily"
             and pending.get("result_rank") is not None)
    return _ok({"state": 1 if ready else 0})


@registry.endpoint("daily_legend_race/get_reward_list")
def handle_legend_get_reward_list(payload: dict) -> dict:
    row = _legend_race_row(payload.get("daily_legend_race_id"))
    if row is None:
        return _refuse()
    return _ok({"reward_array": _preview_rewards(row)})


@registry.endpoint("daily_legend_race/reset")
def handle_legend_reset(payload: dict) -> dict:
    return _do_reset(payload, "legend")


@registry.endpoint("daily_legend_race/resume")
def handle_legend_resume(payload: dict) -> dict:
    return _do_resume(payload, "legend", with_purchase_num=False)
