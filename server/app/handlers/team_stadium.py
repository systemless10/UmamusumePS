
from __future__ import annotations

import bisect
import copy
import gzip
import json
import math
from fractions import Fraction
import secrets
import functools
import logging
import random
from datetime import datetime, timezone
from datetime import time as _dt_time
from pathlib import Path

log = logging.getLogger("uma-server")

from .. import config, region
from .. import master_data
from .. import patch
from .. import rating_formula
from .. import state as state_store
from ..simulation import race_simulator
from . import (bond, practice_race, presents, registry, single_mode_team, stamina,
               trained_chara)
from ..result_codes import ResultCode, refuse

TEAM_STADIUM_STATE_KEY = "team_stadium_state"
_VALID_DISTANCE_TYPES = range(1, 6)
_VALID_MEMBER_IDS = range(1, 4)
_VALID_RUNNING_STYLES = range(1, 6)
_STATS = ("speed", "stamina", "power", "guts", "wiz")


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _team_stadium_state(full_state: dict) -> dict:
    st = full_state.get(TEAM_STADIUM_STATE_KEY)
    if isinstance(st, dict):
        return st
    st = {
        "team_data_array": [],
        "team_class": 1, "best_team_class": 1,
        "best_point": 0,
        "team_evaluation_point": 0,
        "evaluation_peak": 0,
        "rank": 0,
        "granted_rank": 0,
        "consecutive_win_count": 0,
        "current_match": None,
    }
    full_state[TEAM_STADIUM_STATE_KEY] = st
    return st


def _rank_for(evaluation_point: int):
    return master_data.query_one(
        "SELECT * FROM team_stadium_rank WHERE team_min_value <= ? "
        "ORDER BY team_min_value DESC LIMIT 1", (evaluation_point,))


@functools.lru_cache(maxsize=1)
def _rank_table_ceiling() -> int:
    row = master_data.query_one("SELECT MAX(team_max_value) AS m FROM team_stadium_rank")
    return int(row["m"]) if row and row["m"] else 0


def rank_display_point(st: dict) -> int:
    ceiling = _rank_table_ceiling()
    peak = int(st.get("evaluation_peak") or 0)
    return min(peak, ceiling) if ceiling else peak


def _grant_rank_rewards(full_state: dict, st: dict) -> list:
    granted = st.get("granted_rank") or 0
    best_row = _rank_for(st.get("evaluation_peak") or 0)
    target = best_row["id"] if best_row else 0
    if target <= granted:
        return []
    rewards = []
    for row in master_data.query(
            "SELECT * FROM team_stadium_rank WHERE id > ? AND id <= ? ORDER BY id",
            (granted, target)):
        cat, iid, num = row["item_category"] or 0, row["item_id"] or 0, row["item_num"] or 0
        if not num:
            continue
        presents.send(full_state, cat, iid, num, message_id=44)
        rewards.append({"item_type": cat, "item_id": iid, "item_num": num})
    st["granted_rank"] = target
    return rewards


_BOX_GOLD, _BOX_RED = 1, 2

_WIN_BOX_GOLD_CHANCE = 0.15
_WIN_BOX_CONTENTS = {
    _BOX_RED: [(30, 110, 300)],
    _BOX_GOLD: [(30, 110, 500)],
}


def _credit(full_state: dict, category: int, item_id: int, num: int) -> None:
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


def _roll_win_boxes(rounds: list, rng: random.Random, guaranteed=False) -> list:
    boxes = []
    for r in rounds:
        if r.get("win_type") != 1:
            continue
        if not guaranteed and rng.random() >= float(config.get("team_trials_win_box_chance", 0.35)):
            continue
        color = _BOX_GOLD if rng.random() < _WIN_BOX_GOLD_CHANCE else _BOX_RED
        boxes.append({"round": r["round"], "box_color_type": color})
    return boxes


def _open_win_boxes(full_state: dict, boxes: list, team_class: int) -> list:
    contents = []
    scale = max(1, team_class)
    for box in boxes:
        for category, item_id, base in _WIN_BOX_CONTENTS[box["box_color_type"]]:
            num = base
            _credit(full_state, category, item_id, num)
            contents.append({"round": box["round"], "item_category": category,
                             "item_id": item_id, "item_num": num,
                             "box_color_type": box["box_color_type"]})
    return contents


def _class_change_rewards(full_state: dict, new_class: int, class_reward_type: int) -> list:
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
        out.append({"round": 0, "item_category": category, "item_id": item_id,
                    "item_num": num,
                    "box_color_type": _BOX_GOLD if class_reward_type != 4 else _BOX_RED})
    return out


def _parse_hms(value: str) -> _dt_time:
    h, m, s = (int(x) for x in value.split(":"))
    return _dt_time(h, m, s)


def _schedule_now():
    from zoneinfo import ZoneInfo
    name = config.get("team_trials_timezone", "Asia/Tokyo" if region.PROCESS_REGION == "jp" else "UTC")
    return datetime.fromtimestamp(patch._servertime(), ZoneInfo(name))


def _week_second(day, value):
    t = _parse_hms(value)
    return ((int(day) + 5) % 7) * 86400 + t.hour * 3600 + t.minute * 60 + t.second


def _current_term_state() -> int:
    row = master_data.query_one("SELECT * FROM team_stadium LIMIT 1")
    if not row:
        return 1
    now = _schedule_now()
    current = now.weekday() * 86400 + now.hour * 3600 + now.minute * 60 + now.second
    for prefix, status in (("interval", 2), ("calc", 3)):
        lo = _week_second(row[f"{prefix}_start_date"], row[f"{prefix}_start_time"])
        hi = _week_second(row[f"{prefix}_end_date"], row[f"{prefix}_end_time"])
        if (lo <= current <= hi) if lo <= hi else (current >= lo or current <= hi):
            return status
    return 1


def _term_id() -> int:
    from datetime import timedelta
    now = _schedule_now()
    row = master_data.query_one("SELECT * FROM team_stadium LIMIT 1")
    weekday = (int(row["race_start_date"]) + 5) % 7 if row else 0
    t = _parse_hms(row["race_start_time"]) if row else _dt_time(0)
    start = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if now < start:
        start -= timedelta(days=7)
    return int(start.strftime("%Y%m%d"))


def _leaderboard(viewer_id, st, *, all_classes=False, term=None):
    term = st.get("term_id", _term_id()) if term is None else term
    states = state_store.all_states_for_key(TEAM_STADIUM_STATE_KEY)
    states[str(viewer_id)] = st
    entries = []
    for oid, other in states.items():
        if not isinstance(other, dict):
            continue
        snapshot = other if other.get("term_id", _term_id()) == term else other.get("weekly_history", {}).get(str(term))
        if not snapshot or not snapshot.get("best_point"):
            continue
        if not all_classes and snapshot.get("team_class", 1) != st.get("team_class", 1):
            continue
        entries.append({"term_id": term, "viewer_id": int(oid),
                        "team_class": snapshot.get("team_class", 1), "best_point": snapshot["best_point"]})
    entries.sort(key=lambda e: (-e["best_point"], e["viewer_id"]))
    previous, rank = None, 0
    for i, entry in enumerate(entries, 1):
        if entry["best_point"] != previous:
            rank, previous = i, entry["best_point"]
        entry["rank"] = rank
    return entries


def _ranking_entry(viewer_id, st):
    return next((e for e in _leaderboard(viewer_id, st) if str(e["viewer_id"]) == str(viewer_id)),
                {"term_id": st.get("term_id", _term_id()), "viewer_id": int(viewer_id),
                 "team_class": st.get("team_class", 1), "best_point": st.get("best_point", 0), "rank": 0})


def _border_line(viewer_id, st):
    entries = _leaderboard(viewer_id, st)
    row = _team_class_row(st.get("team_class", 1))
    n = len(entries)
    promotes = bool(row and row["class_up_range"])
    return {"promotion_rank": n if promotes else 0, "demotion_rank": 0,
            "keep_rank": n, "promotion_point": 1 if promotes else 0,
            "demotion_point": 0, "keep_point": 1 if n else 0}


def _ensure_week(viewer_id, full_state, st):
    current = _term_id()
    if "term_id" not in st:
        st["term_id"] = current
        return []
    if st["term_id"] == current or (st.get("current_match") or {}).get("rounds"):
        return []
    old_class = st.get("team_class", 1)
    rank = _ranking_entry(viewer_id, st)["rank"]
    border = _border_line(viewer_id, st)
    st.setdefault("weekly_history", {})[str(st["term_id"])] = {
        "best_point": st.get("best_point", 0), "team_class": old_class}
    rewards = []
    if rank:
        new_class = old_class
        if border["promotion_rank"] and rank <= border["promotion_rank"]:
            new_class = min(6, old_class + 1)
        elif border["demotion_rank"] and rank >= border["demotion_rank"]:
            new_class = max(1, old_class - 1)
        reason = (1 if new_class > st.get("best_team_class", 1) else 2) if new_class > old_class else (4 if new_class < old_class else 3)
        rewards = [{"item_type": r["item_category"], "item_id": r["item_id"], "item_num": r["item_num"]}
                   for r in _class_change_rewards(full_state, new_class, reason)]
        st["team_class"] = new_class
        st["best_team_class"] = max(st.get("best_team_class", 1), new_class)
    st["term_id"], st["best_point"] = current, 0
    st["current_match"] = None
    st["team_class_change_state"] = 1 if st["team_class"] != old_class else 0
    st.setdefault("pending_weekly_rewards", []).extend(rewards)
    return []


def _consume_start_items(full_state, item_ids):
    from . import shop
    if not isinstance(item_ids or [], list):
        return None
    ids = item_ids or []
    if any(type(i) is not int for i in ids) or len(ids) != len(set(ids)):
        return None
    for item in ids:
        row = master_data.query_one("SELECT item_category FROM item_data WHERE id=?", (item,))
        if not row or (item != 116 and row["item_category"] != 21) or shop.item_count(full_state, item) < 1:
            return None
    used = stamina.consume_recovery_items(full_state, [i for i in ids if i != 116])
    if 116 in ids:
        shop.add_item(full_state, 116, -1)
        used.append(116)
    return used


def _fill_race_npcs(horses, team_ids, ris, rng):
    pool = _race_npc_pool()
    if not pool:
        raise RuntimeError("No NPC templates for Team Trials")
    remaining = sorted(set(range(1, 13)) - {ri["frame_order"] for ri in ris})
    target = sum(sum(h.get(k, 0) for k in _STATS) for h in horses) / len(horses) if horses else 2250
    factor = float(config.get("team_trials_npc_strength", 0.85))
    for gate in remaining:
        row = dict(rng.choice(pool))
        raw = sum(row[k] for k in ("speed", "stamina", "pow", "guts", "wiz"))
        scale = target * factor / max(raw, 1)
        chara = dict(row)
        for key in _STATS:
            chara[key] = max(1, int(row["pow" if key == "power" else key] * scale))
        chara["pow"] = chara["power"]
        chara.update({"trained_chara_id": 0, "viewer_id": 0, "team_id": 0, "team_member_id": 0,
                      "running_style": rng.randint(1, 4), "motivation": rng.randint(1, 5),
                      "skill_array": single_mode_team._npc_skills(row)})
        horses.append(chara)
        team_ids.append(0)
        ris.append({"viewer_id": 0, "member_id": 0, "trained_chara_id": 0,
                    "running_style": chara["running_style"], "motivation": chara["motivation"],
                    "frame_order": gate})


_PROPER_TYPE_DISTANCE = 1
_PROPER_TYPE_GROUND = 2
_PROPER_TYPE_STYLE = 3

_DISTANCE_TYPE_COLUMN = {
    1: "proper_distance_short", 2: "proper_distance_mile",
    3: "proper_distance_middle", 4: "proper_distance_long",
    5: "proper_distance_mile",
}
_STYLE_COLUMN = {
    1: "proper_running_style_nige", 2: "proper_running_style_senko",
    3: "proper_running_style_sashi", 4: "proper_running_style_oikomi",
    5: "proper_running_style_oikomi",
}
_GROUND_COLUMN = {
    1: "proper_ground_turf", 2: "proper_ground_turf", 3: "proper_ground_turf",
    4: "proper_ground_turf", 5: "proper_ground_dirt",
}


@functools.lru_cache(maxsize=None)
def _evaluation_rate(proper_type: int, proper_rank: int) -> Fraction:
    row = master_data.query_one(
        "SELECT rate FROM team_stadium_evaluation_rate WHERE proper_type=? AND proper_rank=?",
        (proper_type, max(1, min(8, proper_rank or 1))))
    return Fraction(row["rate"], 10000) if row else Fraction(1)


def _slot_evaluation_point(chara: dict, distance_type: int, running_style: int) -> int:
    stats = [chara.get(s, 0) or 0 for s in ("speed", "stamina", "power", "wiz", "guts")]
    base = chara.get("rank_score") or (rating_formula.get_rating(stats) + rating_formula.get_skill_score(chara.get("skill_array")))
    rate = _evaluation_rate(_PROPER_TYPE_DISTANCE,
                            chara.get(_DISTANCE_TYPE_COLUMN.get(distance_type, 3), 1))
    rate *= _evaluation_rate(_PROPER_TYPE_GROUND,
                             chara.get(_GROUND_COLUMN.get(distance_type, "proper_ground_turf"), 1))
    rate *= _evaluation_rate(_PROPER_TYPE_STYLE,
                             chara.get(_STYLE_COLUMN.get(running_style, 2), 1))
    return int(base * rate)


def _team_evaluation_point(team_data_array: list, roster_by_id: dict) -> int:
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
    if isinstance(roster, dict):
        return roster
    return {c.get("trained_chara_id"): c for c in roster or ()}


def _roster_by_id(viewer_id) -> dict:
    return {c.get("trained_chara_id"): c for c in trained_chara._get_or_seed_roster(viewer_id)}


_OPPONENT_OFFER_SIZE = 3


_OPPONENT_EP_BAND = 2.5


def _real_opponent_candidates(viewer_id, my_evaluation_point: int, my_team_class: int = 1,
                              limit: int = _OPPONENT_OFFER_SIZE) -> list:
    me = str(viewer_id)
    scored = []
    candidates = state_store.all_states_for_key(TEAM_STADIUM_STATE_KEY)
    eligible = []
    for other_id in sorted(candidates):
        st = candidates[other_id]
        if other_id == me:
            continue
        if not isinstance(st, dict) or not st.get("team_data_array"):
            continue
        if config.get("team_trials_same_class", True) and (st.get("team_class") or 1) != my_team_class:
            continue
        eligible.append((other_id, st))

    ids = [oid for oid, _ in eligible]
    _rosters = state_store.all_states_for_key(trained_chara.ROSTER_KEY, ids)
    _versions = state_store.all_states_for_key(trained_chara.ROSTER_VERSION_KEY, ids)

    for other_id, st in eligible:
        if _versions.get(other_id) == trained_chara.ROSTER_VERSION:
            roster = _index_roster(_rosters.get(other_id))
        else:
            roster = _roster_by_id(other_id)
        ep = _team_evaluation_point(st["team_data_array"], roster)
        band = float(config.get("team_trials_opponent_band", _OPPONENT_EP_BAND))
        lo, hi = my_evaluation_point / max(1, band), my_evaluation_point * max(1, band)
        if my_evaluation_point > 0 and not (lo <= ep <= hi):
            continue
        scored.append((abs(ep - my_evaluation_point), other_id, st, roster, ep))

    above = sorted((c for c in scored if c[4] > my_evaluation_point), key=lambda c: c[0])
    below = sorted((c for c in scored if c[4] <= my_evaluation_point), key=lambda c: c[0])
    picked, seen = [], set()

    def take(pool):
        for cand in random.sample(pool, len(pool)):
            if cand[1] not in seen:
                seen.add(cand[1])
                picked.append(cand)
                return True
        return False

    take(above)
    take(sorted(scored, key=lambda c: c[0]))
    take(below)
    for cand in sorted(scored, key=lambda c: c[0]):
        if len(picked) >= limit:
            break
        if cand[1] not in seen:
            seen.add(cand[1])
            picked.append(cand)
    picked.sort(key=lambda c: c[4], reverse=True)
    return picked[:limit]


def _trainer_name_for(viewer_id) -> str:
    return f"Trainer{str(viewer_id)[-4:]}"


def _user_info_at_friend(viewer_id):
    full_state = state_store.get_state(viewer_id) or {}
    info = copy.deepcopy((full_state.get("load_index", {}).get("data") or {}).get("user_info") or {})
    info.update(viewer_id=int(viewer_id), name=info.get("name") or _trainer_name_for(viewer_id))
    return info


def _wire_item_info(full_state: dict, used_item_ids: list) -> list:
    from . import shop
    return [{"item_id": item_id, "number": shop.item_count(full_state, item_id)}
            for item_id in dict.fromkeys(used_item_ids or ())]


def _wire_team_data_array(team_data_array: list) -> list:
    return [{"distance_type": e.get("distance_type"), "member_id": e.get("member_id"),
             "trained_chara_id": e.get("trained_chara_id") or 0,
             "running_style": e.get("running_style") or 0}
            for e in team_data_array or ()]


def _wire_trained_chara_array(team_data_array: list, roster) -> list:
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
    team_data_array = st["team_data_array"]
    user_info = _user_info_at_friend(opponent_viewer_id)
    if _MOB_OPPONENT_VIEWER_BASE <= int(opponent_viewer_id) < _MOB_OPPONENT_VIEWER_BASE + 1000:
        roster_by_id = _index_roster(roster)
        leader_slot = next((slot for slot in team_data_array or ()
                            if slot.get("trained_chara_id") in roster_by_id), None)
        leader = roster_by_id.get(leader_slot["trained_chara_id"]) if leader_slot else None
        if leader:
            card = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?",
                                         (leader.get("card_id"),))
            chara_id = card["chara_id"] if card else 0
            dress_id = (leader.get("race_cloth_id") or
                        trained_chara.dress_for_card(leader.get("card_id") or 0, chara_id))
            if chara_id and dress_id:
                user_info["leader_chara_id"] = chara_id
                user_info["leader_chara_dress_id"] = dress_id
                user_info["partner_chara_id"] = leader["trained_chara_id"]
    return {
        "strength": strength,
        "opponent_viewer_id": int(opponent_viewer_id),
        "evaluation_point": evaluation_point,
        "user_info": user_info,
        "team_data_array": _wire_team_data_array(team_data_array),
        "trained_chara_array": _wire_trained_chara_array(team_data_array, roster),
        "winning_reward_guarantee_status": int(random.SystemRandom().random() <
            float(config.get("team_trials_guarantee_chance", 0))),
    }


_MOB_CHARA_ID_BASE = 700_000
_MOB_CHARA_ID_STRIDE = 100
_MOB_TALENT_LEVEL = 5


@functools.lru_cache(maxsize=512)
def _card_for_chara(chara_id: int) -> int:
    if not chara_id:
        return 0
    row = master_data.query_one(
        "SELECT id FROM card_data WHERE chara_id=? ORDER BY default_rarity DESC, id",
        (int(chara_id),))
    return int(row["id"]) if row else 0


@functools.lru_cache(maxsize=1)
def _race_npc_pool() -> tuple:
    return tuple(dict(r) for r in master_data.query("SELECT * FROM single_mode_npc WHERE mob_id>0"))


@functools.lru_cache(maxsize=1)
def _npc_pool() -> tuple:
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
                       viewer_id: int = 0, offer_index: int = 0, team_class: int = 6) -> tuple:
    rows = _npc_pool()
    if not rows:
        return [], {}
    rng = random.Random(secrets.randbits(64))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    team_data_array = []
    roster = {}
    used_charas = set()
    members = _unit_max_num(team_class)
    next_id = _MOB_CHARA_ID_BASE + offer_index * _MOB_CHARA_ID_STRIDE
    for distance_type in _VALID_DISTANCE_TYPES:
        for member_id in range(1, members + 1):
            row = dict(rng.choice([r for r in rows if r["chara_id"] not in used_charas] or rows))
            used_charas.add(row["chara_id"])
            total = row["speed"] + row["stamina"] + row["pow"] + row["guts"] + row["wiz"]
            target = max(1, evaluation_point / (5 * members))
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
                "skill_array": single_mode_team._npc_skills(row),
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
    original = {cid: {k: c[k] for k in _STATS} for cid, c in roster.items()}
    lo, hi = 0.0, 2.0
    for _ in range(18):
        scale = (lo + hi) / 2
        for cid, c in roster.items():
            for key in _STATS:
                c[key] = max(1, int(original[cid][key] * scale))
            c["rank_score"] = rating_formula.get_rating([c[k] for k in ("speed", "stamina", "power", "wiz", "guts")]) + rating_formula.get_skill_score(c["skill_array"])
        if _team_evaluation_point(team_data_array, roster) < evaluation_point:
            lo = scale
        else:
            hi = scale
    for c in roster.values():
        c["rank_score"] = rating_formula.get_rating([c[k] for k in ("speed", "stamina", "power", "wiz", "guts")]) + rating_formula.get_skill_score(c["skill_array"])
        c["rank"] = trained_chara._rank_for_score(c["rank_score"])
    return team_data_array, roster


_MOB_OPPONENT_VIEWER_BASE = 900_000_600_000


_MOB_OFFER_SPREAD = (0.015, 0.0, -0.015)
_MOB_OFFER_MIN_DELTA = 250


def _mob_offer_targets(my_evaluation_point: int, count: int, start_index: int) -> list:
    targets = []
    for i in range(start_index, start_index + count):
        frac = _MOB_OFFER_SPREAD[min(i, len(_MOB_OFFER_SPREAD) - 1)]
        delta = my_evaluation_point * frac
        if frac and abs(delta) < _MOB_OFFER_MIN_DELTA:
            delta = _MOB_OFFER_MIN_DELTA if frac > 0 else -_MOB_OFFER_MIN_DELTA
        targets.append(max(1, int(round(my_evaluation_point + delta + random.SystemRandom().uniform(-max(50, my_evaluation_point * .005), max(50, my_evaluation_point * .005))))))
    return targets


_CORPUS_PATH = Path(__file__).resolve().parents[2] / "data" / "tt_opponents" / "tt_teams.jsonl.gz"
_CORPUS_BANDS_LOW = ((5250, 6000), (1000, 2000), (-3750, -3000))
_CORPUS_BANDS_CLASS6 = ((2408, 5386), (-2340, 1398), (-6195, -3616))
_APT_LETTERS = " GFEDCBAS"
_APT_COLUMNS = ("proper_ground_turf", "proper_ground_dirt", "proper_running_style_nige",
                "proper_running_style_senko", "proper_running_style_sashi",
                "proper_running_style_oikomi", "proper_distance_short", "proper_distance_mile",
                "proper_distance_middle", "proper_distance_long")


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple:
    if not _CORPUS_PATH.exists():
        return ()
    with gzip.open(_CORPUS_PATH, "rt", encoding="utf-8") as f:
        teams = [json.loads(line) for line in f]
    teams.sort(key=lambda t: t["score"])
    return tuple(teams)


def _corpus_pick(lo: int, hi: int, taken: set, rng: random.Random):
    teams = _corpus()
    scores = [t["score"] for t in teams]
    i, j = bisect.bisect_left(scores, lo), bisect.bisect_right(scores, hi)
    pool = [k for k in range(i, j) if k not in taken]
    if pool:
        return rng.choice(pool)
    mid = (lo + hi) // 2
    near = sorted((k for k in range(len(teams)) if k not in taken),
                  key=lambda k: abs(scores[k] - mid))
    return near[0] if near else None


def _corpus_offer_indices(my_evaluation_point: int, my_team_class: int, count: int,
                          start_index: int, rng: random.Random) -> list:
    teams = _corpus()
    if not teams:
        return []
    bands = _CORPUS_BANDS_CLASS6 if my_team_class >= 6 else _CORPUS_BANDS_LOW
    if my_evaluation_point + bands[-1][0] > teams[-1]["score"]:
        top = list(range(len(teams) - 1, max(-1, len(teams) - 1 - _OPPONENT_OFFER_SIZE), -1))
        return top[start_index:start_index + count]
    out, taken = [], set()
    for pos in range(start_index, start_index + count):
        lo, hi = bands[min(pos, len(bands) - 1)]
        k = _corpus_pick(my_evaluation_point + lo, my_evaluation_point + hi, taken, rng)
        if k is not None:
            taken.add(k)
            out.append(k)
    return out


def _corpus_opponent_team(team: dict, viewer_id: int, offer_index: int) -> tuple:
    rng = random.Random(secrets.randbits(64))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    team_data_array, roster = [], {}
    next_id = _MOB_CHARA_ID_BASE + offer_index * _MOB_CHARA_ID_STRIDE
    for m in team["members"]:
        card_id = int(m["card"])
        chara_id = card_id // 100
        speed, stamina, power, wiz, guts = (int(v or 0) for v in m["stats"])
        skills = [{"skill_id": s, "level": lv} for s, lv in m.get("skills") or ()]
        rank_score = m.get("rank_score") or (
            rating_formula.get_rating([speed, stamina, power, wiz, guts])
            + rating_formula.get_skill_score(skills))
        fans = rng.randint(100_000, 600_000)
        chara = {
            "trained_chara_id": next_id, "viewer_id": int(viewer_id),
            "owner_viewer_id": 0, "owner_trained_chara_id": 0,
            "single_mode_chara_id": next_id, "card_id": card_id,
            "running_style": m["style"],
            "speed": speed, "stamina": stamina, "power": power, "guts": guts, "wiz": wiz,
            **{col: max(1, _APT_LETTERS.find(ch)) for col, ch in zip(_APT_COLUMNS, m["apt"])},
            "skill_array": skills,
            "rarity": m.get("rarity") or 3, "talent_level": m.get("talent") or 1,
            "rank_score": rank_score, "rank": trained_chara._rank_for_score(rank_score),
            "fans": fans, "chara_grade": trained_chara._chara_grade_for(fans),
            "race_cloth_id": trained_chara.dress_for_card(card_id, chara_id),
            "scenario_id": 1, "succession_num": 0,
            "succession_trained_chara_id_1": 0, "succession_trained_chara_id_2": 0,
            "is_saved": 1, "is_locked": 0, "nickname_id": 0, "wins": 0,
            "register_time": now, "create_time": now,
            "support_card_list": [{"position": i, "support_card_id": sid, "limit_break_count": lb,
                                   "exp": 0} for i, (sid, lb) in enumerate(m.get("deck") or (), 1)],
            "race_result_list": [], "win_saddle_id_array": [], "nickname_id_array": [],
            "factor_info_array": [{"factor_id": fid, "level": 0} for fid in m.get("factors") or ()],
            "factor_extend_array": [], "succession_chara_array": [],
        }
        roster[next_id] = chara
        team_data_array.append({"distance_type": m["dist"], "member_id": m["slot"],
                                "trained_chara_id": next_id, "running_style": m["style"]})
        next_id += 1
    return team_data_array, roster


def _opponent_offers(viewer_id, my_evaluation_point: int, my_team_class: int = 1) -> list:
    offers = []
    for _dist, other_id, st, roster, ep in _real_opponent_candidates(
            viewer_id, my_evaluation_point, my_team_class):
        offers.append((str(other_id), st["team_data_array"], list(roster.values()),
                       st.get("team_class", 1), ep, roster, st))
    missing = max(0, _OPPONENT_OFFER_SIZE - len(offers))
    real_only = config.get("team_trials_real_opponents_only", False) is True
    if missing and not real_only and config.get("team_trials_corpus_opponents", True) is not False:
        start = _OPPONENT_OFFER_SIZE - missing
        for i, k in enumerate(_corpus_offer_indices(
                my_evaluation_point, my_team_class, missing, start, random.Random(secrets.randbits(64)))):
            mob_viewer = _MOB_OPPONENT_VIEWER_BASE + start + i
            team_data_array, roster = _corpus_opponent_team(_corpus()[k], mob_viewer, start + i)
            offers.append((str(mob_viewer), team_data_array, list(roster.values()),
                           my_team_class, _team_evaluation_point(team_data_array, roster),
                           roster, {"team_data_array": team_data_array,
                                    "team_class": my_team_class}))
        missing = max(0, _OPPONENT_OFFER_SIZE - len(offers))
    if missing and not real_only:
        start = _OPPONENT_OFFER_SIZE - missing
        for i, target in enumerate(_mob_offer_targets(my_evaluation_point, missing, start)):
            mob_viewer = _MOB_OPPONENT_VIEWER_BASE + start + i
            team_data_array, roster = _mob_opponent_team(
                {}, target, viewer_id=mob_viewer, offer_index=start + i, team_class=my_team_class)
            offers.append((str(mob_viewer), team_data_array, list(roster.values()),
                           my_team_class, _team_evaluation_point(team_data_array, roster),
                           roster, {"team_data_array": team_data_array,
                                    "team_class": my_team_class}))
    offers.sort(key=lambda o: o[4], reverse=True)
    return [(key, _build_opponent(int(key), st, roster, ep, strength=pos),
             tda, roster_list, team_class, ep)
            for pos, (key, tda, roster_list, team_class, ep, roster, st)
            in enumerate(offers, start=1)]


@functools.lru_cache(maxsize=1)
def _course_pools_by_distance_type() -> dict:
    course_data = race_simulator._course_data()
    pools = {i: [] for i in range(1, 6)}
    for row in master_data.query(
            "SELECT ri.id AS instance_id, r.course_set FROM race_instance ri "
            "JOIN race r ON r.id=ri.race_id "
            "WHERE r.[group]=9 AND r.grade=100 AND r.entry_num=12"):
        course = course_data.get(str(row["course_set"]))
        if course:
            category = 5 if course["surface"] == 2 else course["distanceType"]
            pools[category].append(row["instance_id"])
    return pools


def _pick_course(distance_type: int, seed_rng: random.Random) -> int | None:
    pool = _course_pools_by_distance_type().get(distance_type)
    return seed_rng.choice(pool) if pool else None


NO_SCORE = (0, 0)


@functools.lru_cache(maxsize=None)
def _raw_score_row(condition_type: int, value1: int, value2: int = 0) -> tuple:
    row = master_data.query_one(
        "SELECT id, score FROM team_stadium_raw_score WHERE condition_type=? "
        "AND condition_value_1=? AND condition_value_2=? ORDER BY priority LIMIT 1",
        (condition_type, value1, value2))
    return (row["id"], row["score"]) if row else NO_SCORE


@functools.lru_cache(maxsize=1)
def _placement_scores() -> dict:
    return {r["condition_value_1"]: (r["id"], r["score"]) for r in master_data.query(
        "SELECT id, condition_value_1, score FROM team_stadium_raw_score WHERE condition_type=3")}


def _place_score(place: int) -> tuple:
    return _placement_scores().get(place, NO_SCORE)


def _raw_score(condition_type: int, value1: int, value2: int = 0) -> int:
    return _raw_score_row(condition_type, value1, value2)[1]


LENGTH_METERS = 2.5
_MARGIN_LADDER = [
    (0.5, 3), (0.75, 4), (1, 5), (1.25, 6), (1.5, 7), (1.75, 8), (2, 9),
    (2.5, 10), (3, 11), (3.5, 12), (4, 13), (5, 14), (6, 15), (7, 16),
    (8, 17), (9, 18), (10, 19),
]


def _margin_index(length: float) -> int:
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
    if deciseconds < 1:
        return NO_SCORE
    row = master_data.query_one(
        "SELECT id, score FROM team_stadium_raw_score WHERE condition_type=4 "
        "AND condition_value_1<=? AND condition_value_2>? ORDER BY priority LIMIT 1",
        (deciseconds, deciseconds))
    return (row["id"], row["score"]) if row else NO_SCORE


_STRONG_START_THRESHOLD = 0.02


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
    if rarity in (2, 6):
        return _raw_score_row(8, rarity)
    if rarity in (3, 4, 5):
        return _raw_score_row(8, rarity, max(1, min(10, level or 1)))
    return NO_SCORE


def _positioning_score(is_mid_phase: bool) -> tuple:
    return _raw_score_row(9, 1 if is_mid_phase else 2)


def _team_result_score(places: list, team_size: int) -> tuple:
    if not places:
        return NO_SCORE
    if team_size >= 3 and all(p <= 3 for p in places):
        return _raw_score_row(11, 3, 3)
    if team_size >= 3 and all(p <= 5 for p in places):
        return _raw_score_row(11, 3, 5)
    if team_size == 2 and all(p <= 2 for p in places):
        return _raw_score_row(11, 2, 2)
    return NO_SCORE


@functools.lru_cache(maxsize=None)
def _score_bonus_rate_raw(condition_type: int, value1: int) -> int:
    row = master_data.query_one(
        "SELECT score_rate FROM team_stadium_score_bonus WHERE condition_type=? AND condition_value_1=?",
        (condition_type, value1))
    return row["score_rate"] if row else 0


def _ace_bonus_rate() -> float:
    return Fraction(_score_bonus_rate_raw(1, 1), 10000)


def _opponent_bonus_rate(opponent_evaluation_point: int, own_evaluation_point: int) -> float:
    divisor = _score_bonus_rate_raw(3, 0) + own_evaluation_point - opponent_evaluation_point
    return Fraction(opponent_evaluation_point, divisor) if divisor > 0 else 0


def _consecutive_bonus_rate(count: int) -> float:
    if count < 2:
        return 0.0
    return Fraction(_score_bonus_rate_raw(4, min(count, 5)), 10000)


def _support_bonus_raw(full_state: dict) -> int:
    cards = full_state.get("support_card_collection") or []
    rows = [dict(r) for r in master_data.query("SELECT * FROM support_card_team_score_bonus")]
    total = 0
    for card in cards:
        row = master_data.query_one("SELECT rarity FROM support_card_data WHERE id=?",
                                    (card.get("support_card_id"),))
        if not row:
            continue
        rarity = row["rarity"]
        if region.PROCESS_REGION == "jp":
            active = [r for r in rows if r["start_date"] <= patch._servertime() <= r["end_date"]
                      and r["rarity"] == rarity
                      and r["limit_break_count"] == card.get("limit_break_count", 0)]
            total += max((r["score_rate_per_card"] for r in active), default=0)
        else:
            level = master_data.query_one(
                "SELECT MAX(level) AS level FROM support_card_level WHERE rarity=? AND total_exp<=?",
                (rarity, card.get("exp", 0)))["level"] or 1
            total += sum(r["score_rate"] for r in rows if r["level"] <= level)
    return total


@functools.lru_cache(maxsize=None)
def _score_bonus_id(condition_type: int, value1: int) -> int:
    row = master_data.query_one(
        "SELECT id FROM team_stadium_score_bonus WHERE condition_type=? AND condition_value_1=?",
        (condition_type, value1))
    return row["id"] if row else 0


def _apply_line_bonuses(lines: list, specs: list) -> int:
    total = 0
    for line in lines:
        raw = line["score"]
        if raw <= 0:
            line["bonus_array"] = []
            line["bonus_num"] = 0
            total += raw
            continue
        bonus_array = []
        added = 0
        for bonus_id, rate in specs:
            amount = math.ceil(raw * rate)
            if amount:
                descriptor = master_data.query_one("SELECT * FROM team_stadium_score_bonus WHERE id=?", (bonus_id,))
                bonus_array.append({"score_bonus_id": bonus_id, "bonus_score": int(amount),
                    "condition_type": descriptor["condition_type"],
                    "condition_value_1": descriptor["condition_value_1"],
                    "condition_value_2": descriptor["condition_value_2"],
                    "score_rate": descriptor["score_rate"]})
                added += int(amount)
        line["bonus_array"] = bonus_array
        line["bonus_num"] = len(bonus_array)
        line["score"] = raw + added
        total += line["score"]
    return total


def _position_at(frames: list, horse_idx: int, t: float) -> float:
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
    totals = [sum(h.get(s, 0) or 0 for s in _STATS) for h in horses]
    order = sorted(range(len(horses)), key=lambda i: -totals[i])
    ranks = [0] * len(horses)
    for rank, idx in enumerate(order, start=1):
        ranks[idx] = rank
    return ranks


def _standard_course_time(course: dict) -> float:
    reference = config.get("team_trials_target_times", {}).get(str(course.get("id")))
    if reference is not None:
        return float(reference)
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
    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}
    pop = {i: rank for i, rank in enumerate(_favorite_ranks(horses))}
    sta = _rank_by(lambda i: -(horses[i].get("stamina") or 0))
    pw = _rank_by(lambda i: -(horses[i].get("pow", horses[i].get("power", 0)) or 0))
    out = []
    for i, chara in enumerate(horses):
        chara = dict(chara)
        chara["viewer_id"] = ris[i]["viewer_id"]
        entry = practice_race._build_race_horse_entry(
            chara, frame_order=ris[i]["frame_order"], final_grade=chara.get("rank", 1),
            popularity=pop[i], popularity_mark_rank_array=[pop[i], sta[i], pw[i]])
        entry["item_id_array"] = chara.get("item_id_array", [])
        entry["motivation_change_flag"] = int(116 in entry["item_id_array"])
        out.append(entry)
    return sorted(out, key=lambda h: h["frame_order"])


@functools.lru_cache(maxsize=None)
def _team_class_row(team_class: int) -> dict | None:
    return master_data.query_one("SELECT * FROM team_stadium_class WHERE team_class=?", (team_class,))


def _unit_max_num(team_class: int) -> int:
    row = _team_class_row(team_class)
    return row["unit_max_num"] if row else 3


@registry.endpoint("team_stadium/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    rounds = match.get("rounds") or []
    if rounds and (match.get("term_id", st.get("term_id")) != _term_id()
                   or match.get("last_checked_round", 0) >= len(rounds)):
        handle_all_race_end({"viewer_id": viewer_id})
        full_state = state_store.get_state(viewer_id) or {}
        st = _team_stadium_state(full_state)
    _ensure_week(viewer_id, full_state, st)
    rewards = st.pop("pending_weekly_rewards", []) + _grant_rank_rewards(full_state, st)
    class_change_state = st.pop("team_class_change_state", 0)
    state_store.save_state(viewer_id, full_state)
    match = st.get("current_match")
    race_status = 0
    if match:
        race_status = 2 if match.get("rounds") else 1
    return _ok({
        "team_stadium_id": 1,
        "team_stadium_user": {
            "team_class": st["team_class"], "best_team_class": st["best_team_class"],
            "team_class_state": 0, "best_point": st["best_point"],
        },
        "ranking": _ranking_entry(viewer_id, st),
        "border_line": _border_line(viewer_id, st),
        "team_class_change_state": class_change_state,
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
    if (full_state.get(TEAM_STADIUM_STATE_KEY, {}).get("current_match") or {}).get("chosen"):
        return _refuse()
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
            return _refuse()
        seen_slots.add((dtype, member))
        if tcid:
            if not isinstance(style, int) or style not in _VALID_RUNNING_STYLES:
                return _refuse()
            if tcid not in owned:
                return _refuse()
        elif style != 0:
            return _refuse()
        cleaned.append({"distance_type": dtype, "member_id": member,
                        "trained_chara_id": tcid or 0, "running_style": style})

    st = _team_stadium_state(full_state)
    by_slot = {(e["distance_type"], e["member_id"]): e
              for e in st.get("team_data_array") or []}
    for e in cleaned:
        by_slot[(e["distance_type"], e["member_id"])] = e
    roster = _roster_by_id(viewer_id)
    seen_charas = set()
    for slot in by_slot.values():
        if not slot["trained_chara_id"]:
            continue
        record = roster.get(slot["trained_chara_id"], {})
        chara_id = bond.chara_id_for_card(record.get("card_id", 0))
        if not chara_id or chara_id in seen_charas:
            return _refuse()
        seen_charas.add(chara_id)
    st["team_data_array"] = sorted(by_slot.values(),
                                   key=lambda e: (e["distance_type"], e["member_id"]))

    old_ep = st.get("team_evaluation_point") or 0
    st["team_evaluation_point"] = _team_evaluation_point(
        st["team_data_array"], _roster_by_id(viewer_id))
    st["evaluation_peak"] = max(st.get("evaluation_peak") or 0, st["team_evaluation_point"])

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
        return _refuse(ResultCode.TEAM_STADIUM_NOT_FOUND_TEAM)

    if (st.get("current_match") or {}).get("rounds"):
        return _ok({"opponent_info_array": st["current_match"].get("offers", [])})
    _ensure_week(viewer_id, full_state, st)
    my_ep = _team_evaluation_point(st["team_data_array"], _roster_by_id(viewer_id))
    offers = _opponent_offers(viewer_id, my_ep, st.get("team_class", 1))
    if not offers:
        log.warning("team_stadium/opponent_list: no real opponents eligible for viewer %s; "
                    "generated offers disabled by team_trials_real_opponents_only", viewer_id)
        return _refuse()
    st["current_match"] = {
        "candidates": {key: {"team_data_array": tda, "roster": roster, "team_class": team_class,
                             "evaluation_point": ep}
                       for key, _wire, tda, roster, team_class, ep in offers},
        "chosen": None, "frame_orders": None, "rounds": [], "last_checked_round": 0,
        "seed": secrets.randbits(63), "term_id": st["term_id"],
        "offers": [wire for _key, wire, _tda, _roster, _cls, _ep in offers],
    }
    st["consecutive_win_count"] = 0
    st.pop("last_end_response", None)
    st.pop("last_skip_response", None)
    st.pop("last_completed_start_response", None)
    state_store.save_state(viewer_id, full_state)
    return _ok({"opponent_info_array": [wire for _key, wire, _tda, _roster, _cls, _ep in offers]})


@registry.endpoint("team_stadium/decide_frame_order")
def handle_decide_frame_order(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match")
    candidates = (match or {}).get("candidates") or {}
    if match and "offers" not in match:
        match["offers"] = [_build_opponent(int(k), c, c["roster"], c.get("evaluation_point", 0), strength=i)
                           for i, (k, c) in enumerate(candidates.items(), 1)]
    if (match or {}).get("selection_response"):
        return _ok(copy.deepcopy(match["selection_response"]))
    submitted = payload.get("opponent_info") or {}
    if "opponent_strength" in payload:
        submitted = next((o for o in (match or {}).get("offers", [])
                          if o["strength"] == payload["opponent_strength"]), {})
        if not submitted:
            return _refuse()
    key = str(submitted.get("opponent_viewer_id") or "")
    if key not in candidates:
        if submitted.get("opponent_viewer_id"):
            return _refuse()
        if candidates:
            key = next(iter(candidates))
        else:
            log.warning("team_stadium/decide_frame_order REFUSED (no candidates): "
                        "match_present=%s submitted=%s", bool(match), submitted)
            return _refuse()

    chosen = match["candidates"][key]
    opp_roster = _index_roster(chosen["roster"])
    opp_team_class = chosen.get("team_class", 1)
    opp_evaluation_point = chosen.get("evaluation_point") or 0
    opponent_info = copy.deepcopy(next(o for o in match["offers"] if str(o["opponent_viewer_id"]) == key))
    match["chosen"] = {"key": key, "opponent_info": opponent_info,
                       "team_data_array": chosen["team_data_array"],
                       "roster": chosen["roster"], "team_class": opp_team_class,
                       "evaluation_point": opp_evaluation_point}

    my_roster = _roster_by_id(viewer_id)
    my_unit_max = opp_unit_max = _unit_max_num(st.get("team_class", 1))
    frame_orders = []
    per_distance_seed = random.Random(match.setdefault("seed", secrets.randbits(63)))
    distance_order = list(_VALID_DISTANCE_TYPES)
    per_distance_seed.shuffle(distance_order)
    match["user_team"] = copy.deepcopy(st["team_data_array"])
    match["user_roster"] = list(my_roster.values())
    for race_order, distance_type in enumerate(distance_order, 1):
        mine = [e for e in st["team_data_array"]
               if e["distance_type"] == distance_type and e["trained_chara_id"]][:my_unit_max]
        theirs = [e for e in chosen["team_data_array"]
                 if e["distance_type"] == distance_type and e["trained_chara_id"]][:opp_unit_max]
        field = [(e, True) for e in mine] + [(e, False) for e in theirs]
        gates = per_distance_seed.sample(range(12), len(field))
        random_info = []
        for i, (slot, is_mine) in enumerate(field):
            chara = (my_roster if is_mine else opp_roster).get(slot["trained_chara_id"]) or {}
            random_info.append({
                "viewer_id": int(viewer_id) if is_mine else int(opponent_info.get("opponent_viewer_id") or 0),
                "member_id": slot["member_id"], "trained_chara_id": slot["trained_chara_id"],
                "running_style": slot["running_style"], "frame_order": gates[i] + 1,
                "motivation": per_distance_seed.randint(1, 5),
            })
        frame_orders.append({"distance_type": distance_type, "race_order": race_order,
                             "random_info_array": random_info})
    match["frame_orders"] = frame_orders
    match["selection_response"] = {
        "frame_order_info_array": frame_orders,
        "user_team_data_array_copy": _wire_team_data_array(match["user_team"]),
        "user_trained_chara_array_copy": _wire_trained_chara_array(match["user_team"], my_roster),
        "opponent_info_copy": opponent_info,
        "opponent_chara_info_array_latest_copy": _wire_trained_chara_array(
                [{"trained_chara_id": k} for k in opp_roster], opp_roster),
        "winning_reward_guarantee_status": opponent_info.get("winning_reward_guarantee_status", 0),
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(copy.deepcopy(match["selection_response"]))


def _score_horse(i, horse, ri, place, sim, course, mid_leaders, final_leaders,
                  favorite_ranks, standard_time, winner_idx, margin_len, n,
                  skill_events_by_horse, kakari_by_horse) -> tuple[list, int]:
    result = sim["results"][i]
    lines = []

    def add(row, num=1):
        row_id, score = row
        if row_id and score:
            lines.append({"raw_score_id": row_id, "num": int(num),
                          "score": int(score * num), "bonus_array": []})

    add(_place_score(place))

    if i == winner_idx and n > 1:
        add(_margin_score(margin_len))

    add(_dark_horse_score(favorite_ranks[i], place))

    if standard_time:
        beat = standard_time - result.get("finishTimeDisplay", result["finishTimeRaw"])
        if beat > 0:
            add(_beat_target_time_score(beat))

    add(_strong_start_score(result.get("startDelayTime") or 0.0), 1)

    if i in mid_leaders:
        add(_positioning_score(True), 1)
    if i in final_leaders:
        add(_positioning_score(False))

    for ev in skill_events_by_horse.get(i, []):
        lvl = _skill_level_for(horse, ev["skillId"])
        add(_skill_activation_score(ev["skillId"], lvl))

    if kakari_by_horse.get(i, 0) > 0:
        add(_raw_score_row(5, 0))
        add(_raw_score_row(6, 0), int(kakari_by_horse[i]))
    grouped = {}
    for line in lines:
        key = line["raw_score_id"]
        if key in grouped:
            grouped[key]["num"] += line["num"]
            grouped[key]["score"] += line["score"]
        else:
            grouped[key] = line
    lines = list(grouped.values())
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
        return _refuse()

    if match.get("start_response"):
        response = copy.deepcopy(match["start_response"])
        response["last_checked_round"] = match.get("last_checked_round", 0)
        return _ok(response)
    if match.get("rounds"):
        return _refuse()
    if _current_term_state() != 1:
        return _refuse()
    full_state = copy.deepcopy(full_state)
    st = _team_stadium_state(full_state)
    match = st["current_match"]
    chosen, frame_orders = match["chosen"], match["frame_orders"]
    used_item_ids = _consume_start_items(full_state, payload.get("item_id_array"))
    if used_item_ids is None or stamina.RP.spend(full_state, stamina.team_trials_rp_cost()) is None:
        return _refuse()
    my_roster = _index_roster(match.get("user_roster") or list(_roster_by_id(viewer_id).values()))
    opp_roster = _index_roster(chosen["roster"])
    course_rng = random.Random(match.setdefault("seed", secrets.randbits(63)))

    ace_ids = {e["trained_chara_id"] for e in match.get("user_team", st["team_data_array"]) if e.get("member_id") == 1}
    opp_ace_ids = {e["trained_chara_id"] for e in chosen["team_data_array"] if e.get("member_id") == 1}
    my_ep = _team_evaluation_point(match.get("user_team", st["team_data_array"]), my_roster)
    opponent_ep = chosen.get("evaluation_point") or 0
    ace_rate = _ace_bonus_rate()
    support_raw = _support_bonus_raw(full_state)
    support_rate = Fraction(support_raw, 10000)
    opponent_bonus_for_me = _opponent_bonus_rate(opponent_ep, my_ep)
    opponent_bonus_for_them = _opponent_bonus_rate(my_ep, opponent_ep)
    my_unit_max = opp_unit_max = _unit_max_num(st.get("team_class", 1))

    race_start_params_array = []
    race_result_array = []
    consecutive = st.get("consecutive_win_count") or 0

    plans = []
    season = course_rng.choice([1, 2, 3, 4, 5])
    for fo in frame_orders:
        distance_type = fo["distance_type"]
        course_instance = _pick_course(distance_type, course_rng)
        if course_instance is None:
            return _refuse()
        course_info = race_simulator.get_course_for_race_instance(course_instance)
        if course_info is None:
            return _refuse()
        course_set_id, course, gate_count = course_info
        course = dict(course, id=course_set_id)

        horses, team_ids, ris = [], [], []
        for ri in fo["random_info_array"]:
            is_mine = ri["viewer_id"] == int(viewer_id)
            chara = (my_roster if is_mine else opp_roster).get(ri["trained_chara_id"])
            if not chara:
                continue
            chara = dict(chara)
            chara["running_style"] = ri["running_style"]
            chara["motivation"] = 5 if is_mine and 116 in used_item_ids else ri["motivation"]
            chara["team_id"] = 1 if is_mine else 2
            chara["is_viewer"] = is_mine
            chara["team_member_id"] = ri["member_id"]
            chara["item_id_array"] = [i for i in used_item_ids if i == 116] if is_mine else []
            horses.append(chara)
            team_ids.append(1 if is_mine else 2)
            ris.append(ri)
        _fill_race_npcs(horses, team_ids, ris, course_rng)
        ordered = sorted(zip(horses, team_ids, ris), key=lambda entry: entry[2]["frame_order"])
        horses, team_ids, ris = map(list, zip(*ordered))
        n, gate_count = 12, 12

        gate_assignment = [ri["frame_order"] - 1 for ri in ris]
        weather, ground = course_rng.randint(1, 3), course_rng.randint(1, 4)
        seed = course_rng.randint(0, 2**31 - 1)
        plans.append({
            "distance_type": distance_type, "round": fo["race_order"], "course_instance": course_instance,
            "course": course, "gate_count": gate_count, "n": n,
            "horses": horses, "team_ids": team_ids, "ris": ris,
            "weather": weather, "ground": ground, "season": season, "seed": seed,
            "job": {"horses": horses, "course_set_id": course_set_id,
                    "ground": ground, "weather": weather, "season": season,
                    "seed": seed, "gate_count": gate_count,
                    "gate_assignment": gate_assignment, "n": n,
                    "race_context": {**race_simulator.race_context_for_instance(course_instance), "race_type": 14}},
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

        finish_order = sorted(range(n), key=lambda i: sim["results"][i]["finishOrder"])
        if sorted(r["finishOrder"] for r in sim["results"]) != list(range(n)):
            raise RuntimeError("Team Trials simulation returned invalid finish order")
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

        provisional = []
        for i, (horse, team_id, ri) in enumerate(zip(horses, team_ids, ris)):
            place = place_of[i]
            lines, raw_subtotal = _score_horse(
                i, horse, ri, place, sim, course, mid_leaders, final_leaders,
                favorite_ranks, standard_time, winner_idx, margin_len, n,
                skill_events_by_horse, kakari_by_horse)
            is_mine = team_id == 1
            ace_set = ace_ids if is_mine else opp_ace_ids
            specs = []
            if support_rate:
                specs.append((_score_bonus_id(5, 0), support_rate))
            if ri["trained_chara_id"] in ace_set and ace_rate:
                specs.append((_score_bonus_id(1, 1), ace_rate))
            opp_rate = opponent_bonus_for_me if is_mine else opponent_bonus_for_them
            if opp_rate:
                specs.append((_score_bonus_id(3, 0), opp_rate))
            provisional.append([lines, raw_subtotal, specs])

        winning_team = team_ids[winner_idx]
        win_type = winning_team if winning_team in (1, 2) else 3

        if win_type == 1:
            consecutive += 1
            bonus_rate = _consecutive_bonus_rate(consecutive)
            if bonus_rate:
                streak_id = _score_bonus_id(4, min(consecutive, 5))
                for i in range(n):
                    if team_ids[i] == 1:
                        provisional[i][2].append((streak_id, bonus_rate))
        else:
            consecutive = 0

        team_scores = {0: 0, 1: 0, 2: 0}
        chara_result_array = []
        places_by_team = {0: [], 1: [], 2: []}
        for i, (horse, team_id, ri) in enumerate(zip(horses, team_ids, ris)):
            lines, _raw_subtotal, specs = provisional[i]
            if team_id != 1:
                lines = []
            total = _apply_line_bonuses(lines, specs)
            team_scores[team_id] += total
            places_by_team[team_id].append(place_of[i])
            chara_result_array.append({
                "viewer_id": ri["viewer_id"], "frame_order": ri["frame_order"],
                "trained_chara_id": ri["trained_chara_id"], "team_id": team_id,
                "finish_order": place_of[i],
                "finish_time": int(round(sim["results"][i].get("finishTimeDisplay", sim["results"][i]["finishTimeRaw"]) * 10000)),
                "score_array": lines,
            })

        my_tr_id, my_team_result = _team_result_score(
            places_by_team[1], min(my_unit_max, len(places_by_team[1])))
        team_score_array = []
        if my_team_result:
            team_score_array.append({"raw_score_id": my_tr_id, "num": 1,
                                     "score": my_team_result, "bonus_array": []})
        team_specs = [(_score_bonus_id(3, 0), opponent_bonus_for_me),
                      (_score_bonus_id(5, 0), support_rate)]
        if consecutive >= 2:
            team_specs.append((_score_bonus_id(4, min(consecutive, 5)),
                               _consecutive_bonus_rate(consecutive)))
        my_score = team_scores[1] + _apply_line_bonuses(team_score_array, team_specs)

        race_start_params_array.append({
            "round": plan["round"], "race_instance_id": course_instance,
            "season": season, "weather": weather, "ground_condition": ground,
            "random_seed": seed,
            "race_horse_data_array": _team_race_horse_array(horses, ris),
            "self_evaluate": my_ep, "opponent_evaluate": opponent_ep,
        })
        race_result_array.append({
            "distance_type": distance_type, "race_scenario": scenario_b64,
            "round": plan["round"],
            "team_score_array": team_score_array,
            "team_total_score": my_score, "win_type": win_type,
            "current_consecutive_win_count": consecutive,
            "bonus_rate_by_next_win": (_score_bonus_rate_raw(4, min(consecutive + 1, 5))
                                       if consecutive >= 1 else 0),
            "chara_result_array": chara_result_array,
        })

    match["rounds"] = race_result_array
    match["win_boxes"] = _roll_win_boxes(
        race_result_array, random.Random(course_rng.randint(0, 2**31 - 1)),
        bool(chosen["opponent_info"].get("winning_reward_guarantee_status")))
    st["consecutive_win_count"] = consecutive
    match["start_response"] = {
        "use_item_id_array": used_item_ids, "rp_info": stamina.rp_info(full_state),
        "race_start_params_array": race_start_params_array, "race_result_array": race_result_array,
        "item_info_array": _wire_item_info(full_state, used_item_ids),
        "is_include_unsupported_race": False, "winning_reward_info_array": match["win_boxes"],
        "winning_reward_guarantee_status": chosen["opponent_info"].get("winning_reward_guarantee_status", 0),
        "last_checked_round": match.get("last_checked_round", 0), "support_card_bonus": support_raw,
        "user_team_data_array_copy": [], "user_trained_chara_array_copy": [],
        "opponent_info_copy": None, "opponent_chara_info_array_latest_copy": None,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(copy.deepcopy(match["start_response"]))


@registry.endpoint("team_stadium/replay_check")
def handle_replay_check(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    round_num = payload.get("round")
    if type(round_num) is not int or round_num not in range(1, 6) or not match.get("rounds"):
        return _refuse()
    match["last_checked_round"] = max(match.get("last_checked_round", 0), round_num)
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


@registry.endpoint("team_stadium/all_race_end")
def handle_all_race_end(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    match = st.get("current_match") or {}
    rounds = match.get("rounds") or []
    if not rounds and st.get("last_end_response"):
        return _ok(copy.deepcopy(st["last_end_response"]))
    if not rounds:
        return _refuse()

    wins = sum(1 for r in rounds if r["win_type"] == 1)
    losses = sum(r["win_type"] == 2 for r in rounds)
    match_won = wins > losses
    bonus = 0
    if wins >= 3:
        chosen = match.get("chosen") or {}
        params = ((match.get("start_response") or {}).get("race_start_params_array") or [{}])[0]
        opponent_bonus = _opponent_bonus_rate(chosen.get("evaluation_point") or 0, params.get("self_evaluate") or 0)
        raw = _raw_score(10, 1, 1)
        bonus = raw + math.ceil(raw * opponent_bonus)
    final_total = sum(r["team_total_score"] for r in rounds) + bonus

    st["team_evaluation_point"] = _team_evaluation_point(
        st["team_data_array"], _roster_by_id(viewer_id))
    st["evaluation_peak"] = max(st.get("evaluation_peak") or 0, st["team_evaluation_point"])
    old_best_point = st.get("best_point") or 0
    st["best_point"] = max(old_best_point, final_total)
    is_update_high_score = final_total > old_best_point

    cur_class = st.get("team_class", 1)
    winning_reward_content_array = _open_win_boxes(
        full_state, match.get("win_boxes") or [], cur_class)

    st["best_team_class"] = max(st.get("best_team_class", 1), st["team_class"])

    rank = _ranking_entry(viewer_id, st)["rank"]
    _grant_rank_rewards(full_state, st)

    mvp_chara_id = 0
    best_score = -1
    for r in rounds:
        for cr in r["chara_result_array"]:
            if cr["team_id"] == 1:
                total_line = sum(s["score"] for s in cr["score_array"])
                if total_line > best_score:
                    best_score, mvp_chara_id = total_line, cr["trained_chara_id"]

    st["last_completed_start_response"] = copy.deepcopy(match.get("start_response"))
    st["current_match"] = None

    add_love_point = 2 if match_won else 1
    my_roster = _roster_by_id(viewer_id)
    love_charas = []
    selected_roster = _index_roster(match.get("user_roster") or list(my_roster.values()))
    for result in (h for r in rounds for h in r["chara_result_array"] if h["team_id"] == 1):
        record = selected_roster.get(result["trained_chara_id"])
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
    from . import factor_research
    research_gauge = factor_research.add_gauge(full_state, 30)
    from . import map_event
    map_gauge = map_event.gauge_info(full_state, "teamstadium")

    fan_info = []
    fielded = []
    selected_roster = _index_roster(match.get("user_roster") or list(my_roster.values()))
    for race in rounds:
        for result in race["chara_result_array"]:
            if result["team_id"] != 1:
                continue
            fielded.append(result)
            record = selected_roster.get(result["trained_chara_id"], {})
            cid = bond.chara_id_for_card(record.get("card_id", 0))
            if cid:
                fans = max(0, config.get_int("team_trials_fan_base", 100) +
                           config.get_int("team_trials_fan_place_pool", 400) // result["finish_order"])
                bond.chara_entry(full_state, cid)["fan"] += fans
                fan_info.append({"chara_id": cid, "add_fan": fans})
    friend_points = max(0, len(fielded) * config.get_int("team_trials_friend_per_runner", 5) +
                        wins * config.get_int("team_trials_friend_per_win", 2))
    _credit(full_state, 103, 98, friend_points)
    update_user_chara_array = [dict(bond.chara_entry(full_state, c)) for c in love_charas]
    st["last_end_response"] = {
        "add_friend_point": friend_points, "add_fan_info_array": fan_info,
        "add_love_point": add_love_point,
        "update_user_chara_array": update_user_chara_array,
        "reward_summary_info": None,
        "campaign_walking_gauge_info": walk_gauge_info,
        "winning_reward_content_array": winning_reward_content_array,
        "total_score_info": {"final_total_score": final_total, "all_race_result_score_bonus": bonus},
        "final_win_type": 1 if wins > losses else (2 if losses > wins else 3),
        "is_update_high_score": int(is_update_high_score),
        "ranking_rank": rank,
        "mvp_chara_id": mvp_chara_id,
        "circle_point": 0,
        "new_chara_profile_array": new_chara_profile_array,
        "ranking": {"term_id": st.get("term_id", _term_id()), "viewer_id": viewer_id, "team_class": st["team_class"],
                   "best_point": st["best_point"], "rank": rank},
        "border_line": _border_line(viewer_id, st),
        "campaign_id_array": [], "limited_shop_info": None,
        **({"add_trainer_medal": 0, "map_event_gauge_info": None, "factor_research_gauge_info": research_gauge}
           if region.PROCESS_REGION == "jp" else
           {"factor_research_gauge_info": research_gauge} if research_gauge is not None else {}),
        **({"map_event_gauge_info": map_gauge} if map_gauge is not None else {}),
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(copy.deepcopy(st["last_end_response"]))


@registry.endpoint("team_stadium/user_detail")
def handle_user_detail(payload: dict) -> dict:
    viewer_id = payload.get("target_viewer_id") or payload["viewer_id"]
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
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _team_stadium_state(full_state)
    _ensure_week(viewer_id, full_state, st)
    state_store.save_state(viewer_id, full_state)
    kind = payload.get("ranking_type", 1)
    if kind not in (1, 2, 3):
        return _refuse()
    entries = _leaderboard(viewer_id, st, all_classes=kind != 1)
    if kind == 3:
        from .. import social
        friends = {str(v) for v in social.following(viewer_id)} | {str(viewer_id)}
        entries = [e for e in entries if str(e["viewer_id"]) in friends]
    return _ok({"ranking_array": entries,
                "summary_user_info_array": [_user_info_at_friend(e["viewer_id"]) for e in entries],
                "user_friend_array": []})


@registry.endpoint("team_stadium/skip_race_skip")
def handle_skip_race_skip(payload: dict) -> dict:
    full_state = state_store.get_state(payload["viewer_id"]) or {}
    st = _team_stadium_state(full_state)
    if st.get("last_end_response") and st.get("last_completed_start_response"):
        return _ok({**copy.deepcopy(st["last_completed_start_response"]),
                    **copy.deepcopy(st["last_end_response"])})
    started = handle_start(payload)
    if started.get("data_headers", {}).get("result_code") != 1:
        return started
    ended = handle_all_race_end(payload)
    if ended.get("data_headers", {}).get("result_code") != 1:
        return ended
    return _ok({**started["data"], **ended["data"]})
