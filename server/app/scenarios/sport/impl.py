
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ... import training_formula as tf
from ..ramen import impl as RI
from . import formula as F

log = logging.getLogger("uma-server")

SCENARIO_ID = 7
STATE_KEY = "sport"

GENRES = (1, 2, 3)
FACILITIES = (101, 105, 102, 103, 106)
STAT_OF_FACILITY = {cmd: stat for stat, cmd in F.STATCMD.items()}
CAMP_BASE = {601: 101, 602: 105, 603: 102, 604: 103, 605: 106}
START_RANK = 1

COMPETITIONS = {24: (1, 10), 36: (2, 20), 48: (3, 30), 60: (4, 40), 72: (5, 50)}
WIN_NUM = 12
RESULT_WIN, RESULT_LOSS = 1, 2

COMPE_TIERS = ((20, 51), (15, 41), (10, 31), (5, 21), (1, 11))

STANCE_STEP = 50
STANCE_MAX_LEVEL = 10
STANCE_TURNS = 2

ITEM_CHANGE = 6
ITEM_REFILL_TURNS = frozenset({1, 13, 25, 37, 49, 61, 73})
ITEM_FULL = (6, 6, 6)

PS_COMPETITION, PS_COMPETITION_LIVE = 21, 22


def _blank() -> dict:
    return {
        "started": False,
        "ranks": {str(s): START_RANK for s in F.SPORTS},
        "offers": {},
        "energy": {},
        "after_bonus": False,
        "stances": [],
        "items": [], "effected": [], "effected_turn": 0, "refilled": [],
        "results": [],
        "preview": {"turn": 0, "rows": {}, "order": [], "tgr": []},
        "previews": {},
        "hold_turn": 0, "flags": {},
    }


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    return st


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


def turn_of(chara_info: dict) -> int:
    return int((chara_info or {}).get("turn") or 1)


def rng_for(full_state: dict, *parts) -> random.Random:
    return random.Random(":".join(["sport", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def career_over(chara_info: dict) -> bool:
    return RI.career_over(chara_info)


def ensure_started(st: dict, chara_info: dict) -> bool:
    if st["started"]:
        return False
    st["started"] = True
    return True


def ranks(st: dict) -> dict:
    return {int(s): int(r) for s, r in st["ranks"].items()}


def genre_total(st: dict, genre: int) -> int:
    return sum(r for s, r in ranks(st).items() if F.genre(s) == genre)


def sport_for(genre: int, facility: int) -> int:
    return 2000 + genre * 100 + STAT_OF_FACILITY[facility]


def facility_of(command_id) -> int:
    cid = int(command_id or 0)
    if cid in F.SPORTS:
        return F.STATCMD[F.main_stat(cid)]
    return CAMP_BASE.get(cid, cid)


def offers(full_state: dict, turn: int) -> dict:
    st = state(full_state)
    key = str(int(turn))
    if key not in st["offers"]:
        rng = rng_for(full_state, "offer", turn)
        st["offers"] = {k: v for k, v in st["offers"].items() if int(k) >= int(turn) - 1}
        st["offers"][key] = {str(f): rng.choice(GENRES) for f in FACILITIES}
    return {int(f): int(g) for f, g in st["offers"][key].items()}


def energy_extra(full_state: dict, turn: int, sport: int) -> int:
    st = state(full_state)
    key = str(int(turn))
    if key not in st["energy"]:
        st["energy"] = {k: v for k, v in st["energy"].items() if int(k) >= int(turn) - 1}
        st["energy"][key] = {}
    row = st["energy"][key]
    if str(sport) not in row:
        row[str(sport)] = F.roll_energy_extra(rng_for(full_state, "energy", turn, sport))
    return int(row[str(sport)])


def use_item(full_state: dict, turn: int, from_genre: int, to_genre: int) -> str:
    st = state(full_state)
    if ITEM_CHANGE not in st["items"]:
        return "no item 6 held"
    if from_genre not in GENRES or to_genre not in GENRES or from_genre == to_genre:
        return f"bad genres {from_genre} -> {to_genre}"
    cur = offers(full_state, turn)
    if from_genre not in cur.values():
        return f"no facility of genre {from_genre}"
    st["offers"][str(int(turn))] = {str(f): (to_genre if g == from_genre else g) for f, g in cur.items()}
    st["items"].remove(ITEM_CHANGE)
    if int(st["effected_turn"]) != int(turn):
        st["effected"], st["effected_turn"] = [], int(turn)
    st["effected"].append(ITEM_CHANGE)
    return ""


def refill_items(st: dict, turn: int) -> bool:
    turn = int(turn)
    if turn not in ITEM_REFILL_TURNS or turn in st["refilled"]:
        return False
    st["refilled"].append(turn)
    st["items"] = list(ITEM_FULL)
    return True


def effected_items(st: dict, turn: int) -> list:
    return list(st["effected"]) if int(st["effected_turn"]) == int(turn) else []


def compe_wins(st: dict) -> dict:
    out = {g: 0 for g in GENRES}
    for r in st["results"]:
        for s in r.get("win_command_id_array") or ():
            out[F.genre(int(s))] += 1
    return out


def compe_effects(st: dict) -> list:
    out = []
    for g, n in compe_wins(st).items():
        for need, suffix in COMPE_TIERS:
            if n >= need:
                out.append(g * 1000 + suffix)
                break
    return out


def compe_pct(st: dict) -> int:
    from ... import master_data
    total = 0
    for eid in compe_effects(st):
        row = master_data.query_one(
            "SELECT effect_type, effect_value_2 FROM single_mode_sport_compe_effect WHERE id=?", (eid,))
        if row and int(row["effect_type"]) == 5:
            total += int(row["effect_value_2"])
    return total


def compe_pending(st: dict, turn: int) -> bool:
    ct = COMPETITIONS.get(int(turn or 0))
    return bool(ct) and not any(int(r["compe_type"]) == ct[0] for r in st["results"])


def run_competition(st: dict, turn: int) -> dict:
    ct, opp = COMPETITIONS[int(turn)]
    r = ranks(st)
    wins = [s for s in F.SPORTS if r[s] >= opp]
    res = {"compe_type": ct, "result_state": RESULT_WIN if len(wins) >= WIN_NUM else RESULT_LOSS,
           "win_command_id_array": wins}
    st["results"].append(res)
    st["flags"]["new_result"] = ct
    return res


def result_rows(st: dict) -> list:
    rows = [dict(r) for r in sorted(st["results"], key=lambda r: r["compe_type"])]
    new = st["flags"].pop("new_result", None)
    if new:
        rows = [r for r in rows if r["compe_type"] == new] + [r for r in rows if r["compe_type"] != new]
    return rows


def last_result(st: dict) -> dict:
    return max(st["results"], key=lambda r: r["compe_type"]) if st["results"] else {}


def after_training(st: dict, sport: int, gains: list) -> None:
    st["stances"] = [[sid, rem - 1] for sid, rem in st["stances"] if rem - 1 > 0]
    st["after_bonus"] = False
    if not gains:
        return
    before = {g: genre_total(st, g) for g in GENRES}
    for g in gains:
        s = str(int(g["command_id"]))
        st["ranks"][s] = min(F.MAX_RANK, int(st["ranks"].get(s, START_RANK)) + int(g["gain_rank"]))
    for g in GENRES:
        after = genre_total(st, g)
        if after // STANCE_STEP > before[g] // STANCE_STEP:
            left = sum(rem for sid, rem in st["stances"] if sid // 100 == g)
            st["stances"] = [[sid, rem] for sid, rem in st["stances"] if sid // 100 != g]
            st["stances"].append([g * 100 + min(STANCE_MAX_LEVEL, after // STANCE_STEP), STANCE_TURNS + left])


def after_other(st: dict, command_id: int) -> None:
    st["after_bonus"] = int(command_id or 0) != 801


def stance_ids(st: dict) -> list:
    return [int(sid) for sid, _r in st["stances"]]


def convert_commands(full_state: dict, chara_info: dict, turn: int, commands: list,
                     card_levels=None) -> list:
    st = state(full_state)
    turn = int(turn)
    if turn > 1:
        refill_items(st, turn)
    camp = turn in F.SUMMER_TURNS
    offer = offers(full_state, turn)
    r = ranks(st)
    out = [dict(c) for c in commands]
    trainings = []
    for c in out:
        if c.get("command_type") != 1:
            continue
        fac = facility_of(c.get("command_id"))
        if fac not in offer:
            continue
        sport = sport_for(offer[fac], fac)
        level = 5 if camp else F.level_of(r[sport])
        partners = list(c.get("training_partner_array") or [])
        c["command_id"] = sport
        c["level"] = level
        energy = F.ENERGY - energy_extra(full_state, turn, sport)
        c["params_inc_dec_info_array"] = F.home_params(chara_info, sport, level, partners, card_levels,
                                                       energy=energy)
        if c.get("is_enable"):
            from ...handlers import single_mode_team as smt
            c["failure_rate"] = tf.failure_rate(
                int(chara_info.get("vital") or 0), command_id=sport, facility_level=level,
                conditions=chara_info.get("chara_effect_id_array"),
                facility_cards=smt._facility_failure_cards(chara_info, partners))
        trainings.append((sport, level, partners))
    _store_preview(full_state, chara_info, turn, trainings, card_levels, camp)
    return out


def _store_preview(full_state, chara_info, turn, trainings, card_levels, camp) -> None:
    st = state(full_state)
    r = ranks(st)
    offered = {s: p for s, _lv, p in trainings}
    by_stat = {F.main_stat(s): p for s, _lv, p in trainings}
    pct = compe_pct(st)
    stances = stance_ids(st)
    rows = {}
    for sport, level, partners in trainings:
        linked = [(o, F.level_of(r[o])) for o in offered if o != sport and F.genre(o) == F.genre(sport)]
        rows[str(sport)] = {
            "bonus": F.bonus_params(chara_info, sport, level, partners, linked, pct, card_levels,
                                    summer=camp, stances=stances),
            "gains": F.gain_sport_rank_array(chara_info, sport, offered, r, st["after_bonus"]),
        }
    tgr = []
    for s in F.SPORTS:
        g = F.self_rank_gain(chara_info, s, by_stat.get(F.main_stat(s), ()), st["after_bonus"])
        tgr.append({"command_id": s, "gain_rank": max(0, min(g, F.MAX_RANK - r[s]))})
    st["preview"] = {"turn": int(turn), "rows": rows, "order": [s for s, _l, _p in trainings], "tgr": tgr}
    st["previews"] = {k: v for k, v in st["previews"].items() if int(k) >= int(turn) - 1}
    st["previews"][str(int(turn))] = rows


def rebuild(full_state: dict) -> None:
    from ...handlers import single_mode_team as smt
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return
    ci = career["data"].get("chara_info")
    if isinstance(ci, dict):
        RI.refresh_home(full_state, ci)
        state(full_state)["flags"]["rebuilt"] = True


def training_row(st: dict, command_id, turn=None) -> dict:
    rows = st["previews"].get(str(int(turn))) if turn else None
    if rows is None:
        rows = st["preview"]["rows"]
    return rows.get(str(int(command_id or 0))) or {}


def data_set(full_state: dict) -> dict:
    st = state(full_state)
    r = ranks(st)
    pv = st["preview"]
    turn = int(pv.get("turn") or 0)
    return {
        "training_array": [{"command_type": 1, "command_id": s, "sport_rank": r[s]} for s in F.SPORTS],
        "command_info_array": [
            {"command_type": 1, "command_id": s,
             "params_inc_dec_info_array": [dict(p) for p in pv["rows"][str(s)]["bonus"]],
             "gain_sport_rank_array": [dict(g) for g in pv["rows"][str(s)]["gains"]]}
            for s in pv.get("order") or () if str(s) in pv["rows"]],
        "item_id_array": list(st["items"]),
        "effected_item_id_array": effected_items(st, turn),
        "competition_result_array": result_rows(st),
        "effected_stance_array": [{"stance_id": sid, "remain_count": rem} for sid, rem in st["stances"]],
        "compe_effect_id_array": compe_effects(st),
        "training_gain_rank_array": [dict(g) for g in pv.get("tgr") or ()],
    }
