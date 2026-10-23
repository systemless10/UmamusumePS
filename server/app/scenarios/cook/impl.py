
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ..ramen import impl as RI
from . import formula, master as M

log = logging.getLogger("uma-server")

SCENARIO_ID = 8
STATE_KEY = "cook"
ACCOUNT_KEY = "cook_account"

CAMP_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
FINALS_FIRST_TURN = 73
CYCLE = 4
COIN_BASE = 100
SUCCESS_BASE_POINT = 1500
FRIENDS = 7
OPENING_MATERIALS = 50
OPENING_MATERIALS_LINK = 75
YAYOI_MATERIALS = 40
YAYOI = 9002
NO_RACE_ROW_TURNS = frozenset({13})

LINK_CHARAS = (1001, 1028, 1030, 1051, 1104, 9002)
LINK_TRAINEES = (1001, 1028, 1030, 1051, 1104)

REST_TAG_RATE = 0.15
RACE_TAG_RATE = 0.36

AWAKEN = ((2000, 208026), (7000, 208027), (12000, 208028))

TIER_OF_TASTING = {1: 2, 2: 2, 3: 3, 4: 3, 5: 4}
NEW_SET = {1: (11, 10), 3: (21, 20)}
UPGRADE_SET = {2: 12, 4: 22}
FINALS_DISH = {3: 32, 2: 31, 1: 30}


def _blank() -> dict:
    return {
        "started": False,
        "materials": {str(m): 0 for m in M.MATERIALS},
        "garden": {str(m): 1 for m in M.MATERIALS},
        "garden_level": 1,
        "care_point": 0,
        "power": 0, "success_point": 0, "success_rate": 0,
        "history": [],
        "sets": {"1": 1},
        "results": [], "new_result": 0,
        "cook_num": [],
        "dish": {},
        "friends": [], "roster": False,
        "rolls": {},
        "preview": {"turn": 0, "rows": {}},
        "last_cmd": {},
        "cook_flags": {"new_dish": 0, "dish_effect_up": 0, "garden_level_up": 0},
        "awakened": [],
        "hold_turn": 0, "flags": {}, "due": [],
        "yayoi_due": False,
    }


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    return st


def account(full_state: dict) -> dict:
    acc = full_state.get(ACCOUNT_KEY)
    if not isinstance(acc, dict):
        acc = {"degree": 0, "unlocked": [], "unread": []}
        full_state[ACCOUNT_KEY] = acc
    return acc


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


def turn_of(chara_info: dict) -> int:
    return int((chara_info or {}).get("turn") or 1)


def flag(st: dict, name: str, value=None):
    if value is not None:
        st["flags"][name] = value
        return value
    return st["flags"].pop(name, None)


def rng_for(full_state: dict, *parts) -> random.Random:
    return random.Random(":".join(["cook", str(CE.career_salt(full_state))] + [str(p) for p in parts]))


def career_over(chara_info: dict) -> bool:
    return RI.career_over(chara_info)


def zone(turn: int) -> str:
    turn = int(turn or 0)
    if turn >= FINALS_FIRST_TURN:
        return "finals"
    if turn in CAMP_TURNS:
        return "camp"
    return "normal"


def linked(chara_info: dict) -> set:
    return RI.linked_charas(chara_info)


def trainee_chara(chara_info: dict) -> int:
    return int((chara_info or {}).get("card_id") or 0) // 100


def ensure_started(st: dict, chara_info: dict) -> bool:
    if st["started"] or not isinstance(chara_info, dict) or not chara_info.get("support_card_array"):
        return False
    st["started"] = True
    return True


def garden_lv(st: dict, material_id) -> int:
    return int(st["garden"].get(str(int(material_id)), 1))


def passive(st: dict, material_id) -> int:
    return M.harvest_amount(int(material_id), garden_lv(st, material_id), 1)


def chosen(st: dict, material_id) -> int:
    return M.harvest_amount(int(material_id), garden_lv(st, material_id), 2)


def cap(st: dict, material_id) -> int:
    return M.stock_cap(int(material_id), garden_lv(st, material_id))


def tags(st: dict) -> int:
    return sum(1 for h in st["history"] if int(h["boost_type"]) == 2)


def raw_pool(st: dict) -> dict:
    out = {m: passive(st, m) for m in M.MATERIALS}
    for h in st["history"]:
        out[int(h["material_id"])] += int(h["add"])
    return out


def quantity_rate(n_tags: int) -> int:
    return 100 if n_tags <= 0 else M.material_rates().get((2, min(4, n_tags)), 100)


def coin(period: int, n_tags: int) -> int:
    return COIN_BASE * M.coin_rates().get((period, n_tags), 100) // 100


def shown_pool(st: dict, turn: int) -> dict:
    if zone(turn) != "normal":
        return {m: passive(st, m) // 2 for m in M.MATERIALS}
    rate = quantity_rate(tags(st))
    return {m: v * rate // 100 for m, v in raw_pool(st).items()}


def care_point_gain_num(st: dict, turn: int) -> int:
    z = zone(turn)
    if z == "normal":
        return coin(1, tags(st))
    return coin(2 if z == "camp" else 3, 0)


def care_preview(st: dict, turn: int, material_id: int, add: int, tag: bool,
                 failure: bool = False) -> dict:
    material_id = int(material_id)
    z = zone(turn)
    tag = bool(tag) and not failure
    if z == "normal":
        n = tags(st) + (1 if tag else 0)
        rate = quantity_rate(n)
        pool = {m: (v + (add if m == material_id else 0)) * rate // 100
                for m, v in raw_pool(st).items()}
        return {"material_id": material_id, "boost_type": 2 if tag else 0, "pool": pool,
                "care_point": coin(1, n), "rate": rate, "add": int(add)}
    rate = 150 if tag else 100
    base = 3 if z == "camp" else 5
    pool = {m: (passive(st, m) // 2) * rate // 100 + ((add * rate // 100) if m == material_id else 0)
            for m in M.MATERIALS}
    return {"material_id": material_id, "boost_type": base + (1 if tag else 0), "pool": pool,
            "care_point": coin(2 if z == "camp" else 3, 1 if tag else 0), "rate": rate,
            "add": int(add)}


def _harvest(st: dict, pool: dict, points: int) -> None:
    for m, n in pool.items():
        m = int(m)
        have = int(st["materials"].get(str(m), 0))
        st["materials"][str(m)] = max(have, min(cap(st, m), have + int(n)))
    st["care_point"] = int(st["care_point"]) + int(points)


def apply_care(st: dict, care: dict, turn: int) -> None:
    flag(st, "gain", {"material_id": care["material_id"], "boost_type": care["boost_type"],
                      "material_harvest_info_array": _pool_rows(care["pool"]),
                      "care_point": care["care_point"], "material_quantity_rate": care["rate"]})
    if zone(turn) != "normal":
        _harvest(st, care["pool"], care["care_point"])
        st["history"] = []
        return
    st["history"].append({"material_id": care["material_id"], "boost_type": care["boost_type"],
                          "add": care["add"]})
    if len(st["history"]) >= CYCLE:
        _harvest(st, care["pool"], care["care_point"])
        st["history"] = []


def add_materials(st: dict, n: int) -> list:
    out = []
    for m in M.MATERIALS:
        have = int(st["materials"].get(str(m), 0))
        new = max(have, min(cap(st, m), have + n))
        st["materials"][str(m)] = new
        out.append({"material_id": m, "num": new - have})
    return out


def _pool_rows(pool: dict) -> list:
    return [{"material_id": m, "harvest_num": int(pool.get(m, pool.get(str(m), 0)))} for m in M.MATERIALS]


def rolls(full_state: dict, turn: int) -> dict:
    st = state(full_state)
    key = str(int(turn))
    if key not in st["rolls"]:
        rng = rng_for(full_state, "rolls", turn)
        st["rolls"] = {k: v for k, v in st["rolls"].items() if int(k) >= int(turn) - 2}
        st["rolls"][key] = {"rest_mat": rng.choice(M.MATERIALS), "rest_tag": rng.random() < REST_TAG_RATE,
                            "race_mat": rng.choice(M.MATERIALS), "race_tag": rng.random() < RACE_TAG_RATE}
    return st["rolls"][key]


def _bond(chara_info: dict, target: int) -> int:
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and int(e.get("target_id") or 0) == int(target):
            return int(e.get("evaluation") or 0)
    return 0


def partner_weight(chara_info: dict, partner: int) -> int:
    partner = int(partner)
    by_pos = formula.RF.deck(chara_info)
    if partner in by_pos:
        return 3 if formula.RF.card_meta(by_pos[partner])[3] in LINK_CHARAS else 1
    if partner in (102, 103):
        return 0
    return 1


def is_tag_training(chara_info: dict, command_id: int, partners) -> bool:
    base = formula.base_of(command_id)
    by_pos = formula.RF.deck(chara_info)
    for p in partners or ():
        sid = by_pos.get(int(p))
        if sid and formula.RF.card_meta(sid)[1] == base and _bond(chara_info, int(p)) >= 80:
            return True
    return False


def recruit_friends(full_state: dict, chara_info: dict) -> list:
    st = state(full_state)
    if st["roster"]:
        return list(st["friends"])
    from ... import master_data
    taken = linked(chara_info)
    friends = [c for c in LINK_TRAINEES if c not in taken]
    rng = rng_for(full_state, "friends")
    pool = sorted(c for c in master_data.drawable_charas()
                  if 1000 <= int(c) < 2000 and c not in taken and c not in friends)
    rng.shuffle(pool)
    friends += pool[:max(0, FRIENDS - len(friends))]
    st["friends"] = friends
    st["roster"] = True
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for chara in friends:
        if chara not in have:
            arr.append({"target_id": chara, "training_partner_id": chara, "evaluation": 0,
                        "is_outing": 0, "story_step": 0, "is_appear": 1,
                        "group_outing_info_array": []})
    log.info("cook: cooking friends %s join", friends)
    return friends


def is_cooking_friend(st: dict, chara_info: dict, target: int) -> bool:
    if not st["roster"]:
        return False
    if int(target) >= 1000:
        return int(target) in st["friends"]
    sid = formula.RF.deck(chara_info).get(int(target))
    return bool(sid) and formula.RF.is_normal_card(sid)


@__import__("functools").lru_cache(maxsize=1)
def npc_rows() -> tuple:
    from ... import master_data
    return tuple((int(r["partner_id"]), int(r["chara_id"])) for r in master_data.query(
        "SELECT partner_id, chara_id FROM single_mode_unique_chara WHERE scenario_id=8 "
        "ORDER BY partner_id"))


def evaluation_array(st: dict, chara_info: dict) -> list:
    by_pos = formula.RF.deck(chara_info)
    npc = dict(npc_rows())
    out = []
    for e in sorted((e for e in chara_info.get("evaluation_info_array") or () if isinstance(e, dict)),
                    key=lambda e: int(e.get("target_id") or 0)):
        t = int(e.get("target_id") or 0)
        if t in by_pos:
            chara = formula.RF.card_meta(by_pos[t])[3]
        elif t in npc:
            chara = npc[t]
        elif t >= 1000:
            chara = t
        else:
            continue
        out.append({"target_id": t, "chara_id": chara,
                    "is_cooking_friend": 1 if is_cooking_friend(st, chara_info, t) else 0,
                    "cooking_value": 0})
    return out


def dish_effect_rows(st: dict, dish_id: int, lasting_only: bool = False) -> list:
    dish = M.dishes()[int(dish_id)]
    group = dish["dish_group_id"]
    bonus = {M.GE_DISH_TRAINING: 0, M.GE_DISH_RACE: 0, M.GE_DISH_ENERGY: 0}
    for m in M.MATERIALS:
        for t, v1, v2, _v3, v4 in M.facility_effects(m, garden_lv(st, m)):
            if t in bonus and group in (v1, v2):
                bonus[t] += v4
    add = {M.EFF_TRAINING: bonus[M.GE_DISH_TRAINING], M.EFF_RACE: bonus[M.GE_DISH_RACE],
           M.EFF_ENERGY: bonus[M.GE_DISH_ENERGY]}
    rows = []
    for t, v1, v2, v3, v4 in M.dish_effects().get(dish["effect_group_id"], ()):
        rows.append({"effect_type": t, "effect_value_1": v1 + add.get(t, 0), "effect_value_2": v2,
                     "effect_value_3": v3, "effect_value_4": v4})
    if add[M.EFF_ENERGY] and not any(r["effect_type"] == M.EFF_ENERGY for r in rows):
        rows.append({"effect_type": M.EFF_ENERGY, "effect_value_1": add[M.EFF_ENERGY],
                     "effect_value_2": 0, "effect_value_3": 0, "effect_value_4": 0})
    if lasting_only:
        rows = [r for r in rows if r["effect_type"] in M.LASTING]
    return rows


def available_dishes(st: dict) -> list:
    ids = []
    for tier in sorted(st["sets"], key=int):
        ids += M.dish_ids_of_set(int(st["sets"][tier]))
    return sorted(set(ids))


def dish_today(st: dict, turn: int) -> dict:
    d = st["dish"]
    return d if d and int(d.get("turn") or 0) == int(turn) else {}


def dish_lasting(st: dict, turn: int) -> list:
    return list(dish_today(st, turn).get("lasting") or ())


def dish_race_pct(st: dict, turn: int) -> int:
    return sum(int(e["effect_value_1"]) for e in dish_lasting(st, turn) if e["effect_type"] == M.EFF_RACE)


def can_afford(st: dict, dish_id: int) -> bool:
    cost = M.dish_costs().get(M.dishes()[int(dish_id)]["material_group_id"], {})
    return all(int(st["materials"].get(str(m), 0)) >= n for m, n in cost.items())


def _eligible(row: dict, chara_info: dict) -> bool:
    ct, cv = row["condition_type"], row["condition_value_1"]
    if ct == 3:
        return any(_bond(chara_info, p) < cv for p in formula.RF.deck(chara_info))
    if ct == 5:
        return int(chara_info.get("max_vital") or 100) < cv
    return True


def pick_success_effects(rows: list, chara_info: dict, rng) -> list:
    ok = [r for r in rows if _eligible(r, chara_info)]
    weighted = [r for r in ok if r["rate"] > 0]
    if not weighted:
        return []
    x = rng.random() * sum(r["rate"] for r in weighted)
    first = weighted[-1]
    for r in weighted:
        x -= r["rate"]
        if x < 0:
            first = r
            break
    out = [first]
    for r in ok:
        if r is not first and r["odds"] > 0 and rng.random() < r["odds"] / 10000:
            out.append(r)
    return out


def cook(full_state: dict, chara_info: dict, dish_id: int, turn: int) -> dict:
    st = state(full_state)
    dish = M.dishes()[int(dish_id)]
    rng = rng_for(full_state, "cook", turn)
    for m, n in M.dish_costs().get(dish["material_group_id"], {}).items():
        st["materials"][str(m)] = int(st["materials"].get(str(m), 0)) - n
    before_power = int(st["power"])
    st["power"] = before_power + dish["gain_power"]
    for edge, event_id in AWAKEN:
        if before_power < edge <= st["power"] and event_id not in st["awakened"]:
            st["awakened"].append(event_id)
            st["due"].append({"kind": "awaken", "event_id": event_id})
    great = int(st["success_rate"]) >= 100 or rng.random() * 100 < int(st["success_rate"])
    prev_point = int(st["success_point"])
    st["success_point"] = prev_point + dish["gain_power"]
    if great and prev_point >= SUCCESS_BASE_POINT:
        st["success_point"] -= SUCCESS_BASE_POINT
    st["success_rate"] = 100 if st["success_point"] >= SUCCESS_BASE_POINT else M.success_rate_band(st["power"])
    effects = dish_effect_rows(st, dish_id)
    picks = pick_success_effects(M.success_effects().get(dish["success_effect_group_id"], []),
                                 chara_info, rng) if great else []
    not_up = _apply_dish(full_state, chara_info, effects, picks, turn, rng)
    skill_chara = 0
    if great:
        skill_chara = _great_hint(chara_info, rng)
    for row in st["cook_num"]:
        if row[0] == dish["dish_group_id"]:
            row[1] += 1
            break
    else:
        st["cook_num"].append([dish["dish_group_id"], 1])
    st["dish"] = {"turn": int(turn), "id": int(dish_id), "result": 2 if great else 1,
                  "lasting": [e for e in effects if e["effect_type"] in M.LASTING],
                  "success_ids": [r["id"] for r in picks], "skill_chara": skill_chara,
                  "sub": st["dish"].get("sub") if dish_today(st, turn) else []}
    for r in picks:
        if r["effect_type"] == M.EFF_EXTRA_CARD:
            place_extra_cards(full_state, chara_info, dish, r["effect_value_1"], turn, rng)
    _flavor(full_state, chara_info, dish, great)
    log.info("cook: T%s dish %s %s, power %s, point %s, rate %s, effects %s", turn, dish_id,
             "GREAT" if great else "ok", st["power"], st["success_point"], st["success_rate"],
             [r["id"] for r in picks])
    return {"not_up": not_up}


def _apply_dish(full_state, chara_info, effects, picks, turn, rng) -> list:
    energy = sum(e["effect_value_1"] for e in effects if e["effect_type"] == M.EFF_ENERGY)
    mood = sum(e["effect_value_1"] for e in effects if e["effect_type"] == M.EFF_MOTIVATION)
    bond = sum(e["effect_value_1"] for e in effects if e["effect_type"] == M.EFF_BOND)
    max_up = 0
    for r in picks:
        t, v = r["effect_type"], r["effect_value_1"]
        if t == M.EFF_ENERGY:
            energy += v
        elif t == M.EFF_MOTIVATION:
            mood += v
        elif t == M.EFF_BOND:
            bond += v
        elif t == M.EFF_MAX_ENERGY:
            max_up += v
    not_up = []
    if max_up:
        chara_info["max_vital"] = int(chara_info.get("max_vital") or 100) + max_up
        energy += max_up
    if energy:
        vital, top = int(chara_info.get("vital") or 0), int(chara_info.get("max_vital") or 100)
        if vital >= top:
            not_up.append(6)
        chara_info["vital"] = max(vital, min(top, vital + energy))
    if mood:
        have = int(chara_info.get("motivation") or 3)
        if have >= 5:
            not_up.append(20)
        chara_info["motivation"] = min(5, have + mood)
    if bond:
        by_pos = formula.RF.deck(chara_info)
        for e in chara_info.get("evaluation_info_array") or ():
            if isinstance(e, dict) and int(e.get("target_id") or 0) in by_pos:
                e["evaluation"] = min(100, int(e.get("evaluation") or 0) + bond)
    return sorted(set(not_up))


def _great_hint(chara_info: dict, rng) -> int:
    from ... import event_engine
    by_pos = formula.RF.deck(chara_info)
    cards = [sid for _p, sid in sorted(by_pos.items()) if formula.RF.is_normal_card(sid)]
    if not cards:
        return 0
    sid = rng.choice(cards)
    chara = formula.RF.card_meta(sid)[3]
    effects = RI.card_hint_effects(chara_info, sid, rng)
    if chara in LINK_CHARAS:
        for e in effects:
            if e.get("type") == "skill_hint":
                e["value"] = f"+{min(5, int(str(e['value']).lstrip('+')) + 1)}"
    event_engine._apply_effects(chara_info, effects)
    return chara


def place_extra_cards(full_state: dict, chara_info: dict, dish: dict, n: int, turn: int, rng) -> None:
    st = state(full_state)
    cmds = [c for row in M.dish_effects().get(dish["effect_group_id"], ()) if row[0] == M.EFF_TRAINING
            for c in row[2:5] if c]
    cmds = cmds or list(formula.TRAINING_COMMANDS)
    cmd = rng.choice(cmds)
    here = set(RI._placed_positions(full_state, chara_info, cmd))
    sub = st["dish"].setdefault("sub", [])
    here |= {s["pos"] for s in sub if s["command_id"] == cmd}
    pool = [p for p, sid in sorted(formula.RF.deck(chara_info).items())
            if formula.RF.is_normal_card(sid) and p not in here]
    rng.shuffle(pool)
    for p in pool[:max(0, int(n))]:
        sub.append({"command_id": cmd, "pos": p,
                    "chara_id": formula.RF.card_meta(formula.RF.deck(chara_info)[p])[3]})


def extra_partners(st: dict, turn: int) -> dict:
    out: dict = {}
    for s in dish_today(st, turn).get("sub") or ():
        out.setdefault(int(s["command_id"]), []).append(int(s["pos"]))
    return out


def _flavor(full_state: dict, chara_info: dict, dish: dict, great: bool) -> None:
    rows = [r for r in M.dish_names()
            if r["dish_group_id"] == dish["dish_group_id"] and r["great_success_flag"] == (1 if great else 0)]
    own = [r for r in rows if r["chara_id"] == trainee_chara(chara_info)]
    plain = [r for r in rows if not r["chara_id"]]
    pick = (own or plain or [None])[0]
    if not pick:
        return
    flag(state(full_state), "flavor", pick["id"])
    acc = account(full_state)
    if pick["id"] not in acc["unlocked"]:
        acc["unlocked"].append(pick["id"])
        acc["unread"].append(pick["id"])


def read_flavors(full_state: dict, ids) -> None:
    acc = account(full_state)
    gone = {int(i) for i in ids or ()}
    acc["unread"] = [i for i in acc["unread"] if i not in gone]


def level_up(st: dict, requests) -> str:
    plan = {}
    for r in requests or ():
        fid, lv = int(r.get("facility_id") or 0), int(r.get("facility_level") or 0)
        if fid not in M.MATERIALS:
            return f"facility {fid}"
        if lv <= garden_lv(st, fid) or lv > 5:
            return f"facility {fid} to level {lv}"
        plan[fid] = lv
    cost = 0
    for fid, lv in plan.items():
        for step in range(garden_lv(st, fid) + 1, lv + 1):
            row = M.garden_levels().get((fid, step))
            if not row or row["garden_lv"] > int(st["garden_level"]):
                return f"facility {fid} level {step} needs garden {row and row['garden_lv']}"
            cost += row["coin_num"]
    if cost > int(st["care_point"]):
        return f"costs {cost}, has {st['care_point']}"
    st["care_point"] = int(st["care_point"]) - cost
    for fid, lv in plan.items():
        st["garden"][str(fid)] = lv
    return ""


def tasted(st: dict, cook_type: int) -> int:
    return next((int(r["result_state"]) for r in st["results"] if int(r["cook_type"]) == int(cook_type)), 0)


def tasting_pending(st: dict, turn: int) -> bool:
    ct = M.tasting_for_turn(turn)
    return bool(ct) and not tasted(st, ct)


def tasting_result(st: dict, cook_type: int) -> int:
    row = M.power_data()[int(cook_type)]
    power = int(st["power"])
    if row["great_success_num"] and power >= row["great_success_num"]:
        return 3
    return 2 if power >= row["success_num"] else 1


def commit_tasting(st: dict, cook_type: int) -> int:
    result = tasting_result(st, cook_type)
    st["results"] = [r for r in st["results"] if int(r["cook_type"]) != int(cook_type)] + [
        {"cook_type": int(cook_type), "result_state": result}]
    st["new_result"] = int(cook_type)
    st["cook_flags"]["new_dish"] = 0
    st["cook_flags"]["dish_effect_up"] = 0
    log.info("cook: tasting %s -> %s (power %s)", cook_type, result, st["power"])
    return result


def unlock_after_tasting(full_state: dict, cook_type: int, result: int) -> None:
    st = state(full_state)
    ct = int(cook_type)
    row = M.power_data()[ct]
    if row["garden_lv"] > int(st["garden_level"]):
        st["garden_level"] = row["garden_lv"]
    f = st["cook_flags"]
    if ct in NEW_SET:
        st["sets"][str(TIER_OF_TASTING[ct])] = NEW_SET[ct][0 if result >= 2 else 1]
        f["new_dish"] = 1
        f["garden_level_up"] = 1
    elif ct in UPGRADE_SET:
        if result >= 2:
            st["sets"][str(TIER_OF_TASTING[ct])] = UPGRADE_SET[ct]
            f["dish_effect_up"] = 1
    elif ct == 5:
        st["sets"]["4"] = FINALS_DISH[max(1, min(3, result))]
        f["new_dish"] = 1
    acc = account(full_state)
    degree = {1: 1, 3: 2, 5: 3}.get(ct, 0)
    acc["degree"] = max(int(acc["degree"]), degree)


def result_rows(st: dict) -> list:
    rows = sorted(st["results"], key=lambda r: int(r["cook_type"]))
    new = int(flag(st, "new_result_first") or 0)
    if new:
        rows = [r for r in rows if int(r["cook_type"]) == new] + [
            r for r in rows if int(r["cook_type"]) != new]
    return [{"cook_type": int(r["cook_type"]), "result_state": int(r["result_state"])} for r in rows]


def last_result(st: dict) -> int:
    return tasted(st, 5)


def _home_rows(commands) -> dict:
    return {(int(c.get("command_type") or 0), int(c.get("command_id") or 0)): c for c in commands or ()}


def refresh_preview(full_state: dict, chara_info: dict, commands, card_levels=None,
                    turn: int | None = None) -> None:
    st = state(full_state)
    turn = int(turn or turn_of(chara_info))
    power = int(st["power"])
    lasting = dish_lasting(st, turn)
    r = rolls(full_state, turn)
    rows = {}
    for c in commands or ():
        if int(c.get("command_type") or 0) != 1:
            continue
        cmd = int(c["command_id"])
        partners = list(c.get("training_partner_array") or ())
        lv = int(c.get("level") or 1)
        line = formula.bonus_line(chara_info, cmd, lv, partners, formula.power_effect(power, 2),
                                  formula.dish_pct(lasting, cmd), formula.power_effect(power, 207),
                                  card_levels)
        mat = M.COMMAND_MATERIAL[formula.base_of(cmd)]
        add = chosen(st, mat) + sum(partner_weight(chara_info, p) for p in partners)
        tag = is_tag_training(chara_info, cmd, partners)
        rows[f"1:{cmd}"] = {"line": line,
                            "care": care_preview(st, turn, mat, add, tag),
                            "fail": care_preview(st, turn, mat, add, tag, failure=True)}
    rest_mat = r["rest_mat"]
    rows["rest"] = {"care": care_preview(st, turn, rest_mat, chosen(st, rest_mat), r["rest_tag"])}
    rows["yayoi"] = {"care": care_preview(st, turn, rest_mat, chosen(st, rest_mat), True)}
    race_mat = r["race_mat"]
    rows["race"] = {"care": care_preview(st, turn, race_mat, chosen(st, race_mat), r["race_tag"])}
    st["preview"] = {"turn": turn, "rows": rows}


def care_spec(st: dict, key: str, turn: int, command_id=None) -> dict | None:
    if key == "fail":
        care = training_row(st, command_id, turn).get("care")
        return care and {"material_id": care["material_id"], "add": care["add"],
                         "tag": False, "failure": True}
    care = preview_row(st, key, turn).get("care")
    return care and {"material_id": care["material_id"], "add": care["add"],
                     "tag": care["boost_type"] in (2, 4, 6), "failure": False}


def land_spec(st: dict, spec: dict, turn: int) -> None:
    apply_care(st, care_preview(st, turn, spec["material_id"], spec["add"], spec["tag"],
                                failure=spec.get("failure", False)), turn)


def preview_row(st: dict, key: str, turn: int) -> dict:
    pv = st["preview"]
    if int(pv.get("turn") or 0) != int(turn):
        return {}
    return pv["rows"].get(key) or {}


def training_row(st: dict, command_id, turn: int) -> dict:
    cmd = int(command_id or 0)
    for key in (cmd, formula.BASE_TO_SUMMER.get(cmd), formula.SUMMER_TO_BASE.get(cmd)):
        if key is not None and preview_row(st, f"1:{key}", turn):
            return preview_row(st, f"1:{key}", turn)
    return {}


def _care_row(ctype: int, cid: int, care: dict, fail: dict | None = None) -> dict:
    fail = fail or care
    return {"command_type": ctype, "command_id": cid, "material_id": care["material_id"],
            "boost_type": care["boost_type"], "material_harvest_info_array": _pool_rows(care["pool"]),
            "care_point": care["care_point"],
            "failure_material_harvest_info_array": _pool_rows(fail["pool"]),
            "failure_care_point": fail["care_point"]}


def care_rows(st: dict, commands, turn: int, start: bool = False) -> list:
    if not st["preview"].get("rows") or int(st["preview"].get("turn") or 0) != int(turn):
        return []
    home = _home_rows(commands)
    trainings = [c for (t, c) in home if t == 1]
    enabled = [c for (t, c) in home if t == 1 and int(home[(t, c)].get("is_enable", 1) or 0)]
    race_on = (int((home.get((4, 401)) or {}).get("is_enable") or 0) == 1
               and int(turn) not in NO_RACE_ROW_TURNS and not start)
    rows = []
    race = preview_row(st, "race", turn).get("care")
    if trainings and not enabled and race_on and race:
        return [_care_row(4, 401, race)]
    for cid in (trainings if start else sorted(trainings)):
        pv = training_row(st, cid, turn)
        if pv:
            rows.append(_care_row(1, cid, pv["care"], pv["fail"]))
    rest = preview_row(st, "rest", turn).get("care")
    if rest:
        rows.append(_care_row(3, 0, rest))
    if race_on and race:
        rows.append(_care_row(4, 401, race))
    if rest and zone(turn) != "camp":
        rows.append(_care_row(7, 701, rest))
    return rows


def command_rows(st: dict, commands, turn: int) -> list:
    out = []
    for c in commands or ():
        ctype, cid = int(c.get("command_type") or 0), int(c.get("command_id") or 0)
        line = (training_row(st, cid, turn).get("line") or []) if ctype == 1 else []
        out.append({"command_type": ctype, "command_id": cid,
                    "params_inc_dec_info_array": [dict(p) for p in line]})
    return out


def cook_info(st: dict) -> dict:
    f = st["cook_flags"]
    return {"care_point": int(st["care_point"]), "garden_level": int(st["garden_level"]),
            "cooking_friends_power": int(st["power"]), "cooking_success_point": int(st["success_point"]),
            "cooking_success_base_point": SUCCESS_BASE_POINT, "new_dish_flag": int(f["new_dish"]),
            "dish_effect_up_flag": int(f["dish_effect_up"]), "garden_level_up_flag": int(f["garden_level_up"])}


def dish_info(st: dict, turn: int):
    d = dish_today(st, turn)
    if not d:
        return None
    return {"dish_id": int(d["id"]), "cooking_result_state": int(d["result"]),
            "dish_effect_info_array": [dict(e) for e in d.get("lasting") or ()]}


def last_command_info(st: dict, turn: int):
    lc = st["last_cmd"]
    if lc and int(lc.get("turn") or 0) == int(turn):
        return {"command_type": lc["command_type"], "command_id": lc["command_id"],
                "result_state": lc["result_state"]}
    return []


def set_last_command(st: dict, turn: int, ctype: int, cid: int, result_state: int) -> None:
    st["last_cmd"] = {"turn": int(turn), "command_type": int(ctype), "command_id": int(cid),
                      "result_state": int(result_state)}


def data_set(full_state: dict, chara_info: dict, commands, turn: int, full: bool,
             start: bool = False) -> dict:
    st = state(full_state)
    acc = account(full_state)
    d = dish_today(st, turn)
    pv_turn = int(st["preview"].get("turn") or turn)
    ct = M.tasting_for_turn(turn)
    return {
        "cook_info": cook_info(st),
        "command_info_array": command_rows(st, commands, pv_turn) if full else None,
        "evaluation_info_array": evaluation_array(st, chara_info),
        "material_harvest_info_array": _pool_rows(shown_pool(st, turn)),
        "care_history_info_array": [{"num": i + 1, "material_id": int(h["material_id"]),
                                     "boost_type": int(h["boost_type"])}
                                    for i, h in enumerate(st["history"])],
        "material_info_array": [{"material_id": m, "num": int(st["materials"].get(str(m), 0))}
                                for m in M.MATERIALS],
        "dish_info": dish_info(st, turn),
        "facility_info_array": [{"facility_id": m, "facility_level": garden_lv(st, m)} for m in M.MATERIALS],
        "cooking_success_rate": int(st["success_rate"]),
        "gain_material_info": flag(st, "gain") or [],
        "cook_result_info_array": result_rows(st),
        "cook_power_effect_info_array": formula.power_effect_rows(int(st["power"])),
        "dish_skill_info": ({"chara_id": int(d["skill_chara"])} if d.get("skill_chara") else []),
        "success_effect_id_array": list(d.get("success_ids") or ()),
        "sub_command_chara_info_array": [{"command_id": int(s["command_id"]), "chara_id": int(s["chara_id"])}
                                         for s in d.get("sub") or ()],
        "care_point_gain_num": care_point_gain_num(st, turn),
        "care_special_home_id": None,
        "available_dish_info_array": [{"dish_id": i, "dish_effect_info_array": dish_effect_rows(st, i)}
                                      for i in available_dishes(st)],
        "command_material_care_info_array": care_rows(st, commands, pv_turn, start),
        "tasting_result_state": tasting_result(st, ct) if ct and not tasted(st, ct) else 0,
        "last_command_info": last_command_info(st, turn),
        "event_gain_material_info_array": flag(st, "event_gain") or [],
        "unlock_degree_type": int(acc["degree"]),
        "unlock_flavor_text_id_array": list(acc["unlocked"]),
        "unread_flavor_text_id_array": list(acc["unread"]),
        "display_flavor_text_id": int(flag(st, "flavor") or 0),
    }
