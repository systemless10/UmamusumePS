
from __future__ import annotations

import json
import logging
import os
import random

from ... import master_data
from . import impl

log = logging.getLogger("uma-server")

_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "data", "trackblazer_shop_model.json")

_LIMIT_BUY_COUNT = 1

_LIMITED_LIFETIME = 2

_OFFER_CHANCE_WIN = 0.75
_OFFER_CHANCE_PLACED = 0.40

_RIVAL_OFFER_BONUS = _OFFER_CHANCE_WIN - _OFFER_CHANCE_PLACED

_OFFER_COUNT_WIN = {1: 21.3, 2: 34.2, 3: 28.2, 4: 12.9, 5: 3.0, 6: 0.3}
_OFFER_COUNT_PLACED = {2: 72.5, 3: 25.2, 4: 2.3}

PENDING_KEY = "pending_offers"

_DURATIONAL = (11, 12, 13, 14)


_model_cache: dict | None = None


def model() -> dict:
    global _model_cache
    if _model_cache is None:
        try:
            with open(_MODEL_PATH, "r", encoding="utf-8") as fh:
                _model_cache = json.load(fh)
        except Exception:
            log.exception("trackblazer: could not load the shop model at %s",
                          _MODEL_PATH)
            _model_cache = {"resets": {}}
    return _model_cache


def _catalogue() -> dict:
    rows = master_data.query(
        "SELECT item_id, coin_num, effect_group_id, effect_group, effect_priority, "
        "limit_num FROM single_mode_free_shop_item")
    return {int(r["item_id"]): {"price": int(r["coin_num"]),
                                "effect_group_id": int(r["effect_group_id"]),
                                "group": int(r["effect_group"] or 0),
                                "priority": int(r["effect_priority"] or 0),
                                "limit": int(r["limit_num"] or 5)}
            for r in rows}


def _effects_of(item_id) -> list:
    cat = _catalogue().get(int(item_id))
    if not cat:
        return []
    return [dict(r) for r in master_data.query(
        "SELECT effect_type, effect_value_1, effect_value_2, effect_value_3, "
        "effect_value_4, turn FROM single_mode_free_shop_effect "
        "WHERE effect_group_id=? ORDER BY id", (cat["effect_group_id"],))]


def window_for_turn(turn: int):
    return master_data.query_one(
        "SELECT id, start_turn, end_turn FROM single_mode_free_shop "
        "WHERE start_turn<=? AND end_turn>=?", (int(turn), int(turn)))


def _draw(rng: random.Random, weights: dict):
    total = sum(weights.values())
    if total <= 0:
        return None
    roll = rng.random() * total
    for item_id, w in weights.items():
        roll -= w
        if roll <= 0:
            return int(item_id)
    return int(next(iter(weights)))


def _offer(st: dict, item_id: int, limit_turn: int = 0) -> dict:
    price = _catalogue().get(int(item_id), {}).get("price", 0)
    entry = {
        "shop_item_id": int(st.get("next_shop_item_id") or 1),
        "item_id": int(item_id),
        "coin_num": price,
        "original_coin_num": price,
        "item_buy_num": 0,
        "limit_buy_count": _LIMIT_BUY_COUNT,
        "limit_turn": int(limit_turn),
    }
    st["next_shop_item_id"] = entry["shop_item_id"] + 1
    return entry


def _roll_base_stock(st: dict, shop_id: int, rng: random.Random) -> list:
    reset = (model().get("resets") or {}).get(str(int(shop_id))) or {}
    out = []
    for slot in reset.get("slots") or []:
        if rng.random() > float(slot.get("fill_p") or 0):
            continue
        item_id = _draw(rng, slot.get("items") or {})
        if item_id is not None:
            out.append(_offer(st, item_id))
    return out


def refresh(full_state: dict, turn: int) -> bool:
    st = impl.state(full_state)
    turn = int(turn or 0)
    window = window_for_turn(turn)
    shop_id = int(window["id"]) if window else 0
    changed = False
    if shop_id != int(st.get("shop_id") or 0):
        changed = True
        st["shop_id"] = shop_id
        limited = [o for o in (st.get("offers") or []) if int(o.get("limit_turn") or 0)]
        base = []
        if shop_id:
            rng = random.Random((shop_id << 16) ^ int(st.get("seed") or 0)
                                ^ random.randrange(1 << 30))
            base = _roll_base_stock(st, shop_id, rng)
        st["offers"] = base + limited
    live = [o for o in (st.get("offers") or [])
            if not int(o.get("limit_turn") or 0) or int(o["limit_turn"]) >= turn]
    if len(live) != len(st.get("offers") or []):
        changed = True
    st["offers"] = live
    return changed


def offer_chance(result_rank: int, is_debut: bool, is_rival_win: bool) -> float:
    if is_debut:
        return 0.0
    if int(result_rank) == 1:
        return _OFFER_CHANCE_WIN + (_RIVAL_OFFER_BONUS if is_rival_win else 0.0)
    if 2 <= int(result_rank) <= 5:
        return _OFFER_CHANCE_PLACED
    return 0.0


def maybe_add_limited_offer(full_state: dict, turn: int, result_rank: int,
                            is_debut: bool = False,
                            is_rival_win: bool = False) -> bool:
    st = impl.state(full_state)
    shop_id = int(st.get("shop_id") or 0)
    if not shop_id:
        return False
    pool = ((model().get("resets") or {}).get(str(shop_id)) or {}).get("limited_pool") or {}
    if not pool:
        return False
    rng = random.Random(random.randrange(1 << 30))
    if rng.random() > offer_chance(result_rank, is_debut, is_rival_win):
        return False
    counts = _OFFER_COUNT_WIN if int(result_rank) == 1 else _OFFER_COUNT_PLACED
    n = rng.choices(list(counts), weights=list(counts.values()))[0]
    items = [i for i in (_draw(rng, pool) for _ in range(n)) if i is not None]
    if not items:
        return False
    st.setdefault(PENDING_KEY, []).extend(
        {"item_id": i, "limit_turn": int(turn) + _LIMITED_LIFETIME} for i in items)
    return True


def publish_pending_offers(full_state: dict) -> bool:
    st = impl.state(full_state)
    pending = st.pop(PENDING_KEY, None)
    if not pending:
        return False
    offers = st.setdefault("offers", [])
    for p in pending:
        offers.append(_offer(st, int(p["item_id"]), limit_turn=int(p["limit_turn"])))
    return True


def buy(full_state: dict, shop_item_id: int) -> bool:
    st = impl.state(full_state)
    for offer in st.get("offers") or []:
        if int(offer.get("shop_item_id") or 0) != int(shop_item_id):
            continue
        if int(offer.get("item_buy_num") or 0) >= int(offer.get("limit_buy_count") or 1):
            return False
        if not impl.spend_coins(st, int(offer.get("coin_num") or 0)):
            return False
        offer["item_buy_num"] = int(offer.get("item_buy_num") or 0) + 1
        key = str(int(offer["item_id"]))
        items = st.setdefault("items", {})
        items[key] = int(items.get(key) or 0) + 1
        st["items_bought"] = int(st.get("items_bought") or 0) + 1
        return True
    return False


_CHARA_TO_TARGET = {9002: 102, 9003: 103}

_PARAM_STATS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
_BOND_MAX = 100


def _add_param(chara_info: dict, target: int, amount: int) -> None:
    stat = _PARAM_STATS.get(target)
    if stat:
        cap_key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[stat] = min(int(chara_info.get(stat) or 0) + amount,
                               int(chara_info.get(cap_key) or 9999))
    elif target == 10:
        chara_info["vital"] = max(0, min(int(chara_info.get("vital") or 0) + amount,
                                         int(chara_info.get("max_vital") or 100)))
    elif target == 11:
        chara_info["max_vital"] = int(chara_info.get("max_vital") or 100) + amount
    elif target == 20:
        chara_info["motivation"] = max(1, min(5, int(chara_info.get("motivation") or 3)
                                              + amount))


def _bond(chara_info: dict, target_id: int, amount: int) -> None:
    for e in chara_info.get("evaluation_info_array") or []:
        if e.get("target_id") == target_id:
            e["evaluation"] = min(_BOND_MAX, int(e.get("evaluation") or 0) + amount)
            return


def _apply_instant(effect: dict, chara_info: dict, career_data: dict,
                   full_state: dict) -> None:
    kind = int(effect["effect_type"])
    v1 = int(effect.get("effect_value_1") or 0)
    v2 = int(effect.get("effect_value_2") or 0)
    v3 = int(effect.get("effect_value_3") or 0)
    if kind == 1:
        _add_param(chara_info, v1, v2)
    elif kind == 2:
        levels = career_data.setdefault("facility_levels", {})
        key = str(v1)
        levels[key] = min(5, int(levels.get(key) or 1) + max(1, v2))
        for entry in chara_info.get("training_level_info_array") or []:
            if entry.get("command_id") == v1:
                entry["level"] = levels[key]
    elif kind == 3:
        if v1 == 1:
            for c in chara_info.get("support_card_array") or []:
                _bond(chara_info, c.get("position"), v3)
        else:
            _bond(chara_info, _CHARA_TO_TARGET.get(v2, v2), v3)
    elif kind == 6:
        conds = list(chara_info.get("chara_effect_id_array") or [])
        if v1 == 1 and v2 not in conds:
            conds.append(v2)
        elif v1 == 2:
            conds = [c for c in conds if int(c) != v2]
        chara_info["chara_effect_id_array"] = conds
    elif kind == 10:
        st = impl.state(full_state)
        st["placement_salt"] = int(st.get("placement_salt") or 0) + 1


def _supersedes(st: dict, item_id: int, turn: int) -> None:
    cat = _catalogue()
    mine = cat.get(int(item_id)) or {}
    group = mine.get("group") or 0
    if not group:
        return
    kept = []
    for eff in st.get("effects") or []:
        other = cat.get(int(eff.get("item_id") or 0)) or {}
        if other.get("group") == group and int(eff.get("end_turn") or 0) >= turn:
            if (other.get("priority") or 0) > (mine.get("priority") or 0):
                st["absorbed"] = True
                return
            continue
        kept.append(eff)
    st["effects"] = kept


def use(full_state: dict, chara_info: dict, career_data: dict,
        item_id: int, turn: int, count: int = 1) -> bool:
    st = impl.state(full_state)
    key = str(int(item_id))
    items = st.setdefault("items", {})
    have = int(items.get(key) or 0)
    if have < count:
        return False
    effects = _effects_of(item_id)
    if not effects:
        return False
    items[key] = have - count
    if items[key] <= 0:
        items.pop(key, None)
    for _ in range(count):
        st.pop("absorbed", None)
        _supersedes(st, item_id, turn)
        if st.pop("absorbed", None):
            continue
        for eff in effects:
            kind = int(eff["effect_type"])
            if kind in _DURATIONAL:
                duration = max(1, int(eff.get("turn") or 1))
                st.setdefault("effects", []).append({
                    "use_id": int(st.get("next_use_id") or 1),
                    "item_id": int(item_id),
                    "effect_type": kind,
                    "effect_value_1": int(eff.get("effect_value_1") or 0),
                    "effect_value_2": int(eff.get("effect_value_2") or 0),
                    "effect_value_3": int(eff.get("effect_value_3") or 0),
                    "effect_value_4": int(eff.get("effect_value_4") or 0),
                    "begin_turn": int(turn),
                    "end_turn": int(turn) + duration - 1,
                })
                st["next_use_id"] = int(st.get("next_use_id") or 1) + 1
            else:
                _apply_instant(eff, chara_info, career_data, full_state)
    return True


def active_effects(st: dict, turn: int) -> list:
    turn = int(turn or 0)
    live = [e for e in (st.get("effects") or []) if int(e.get("end_turn") or 0) >= turn]
    if len(live) != len(st.get("effects") or []):
        st["effects"] = live
    return [dict(e) for e in live if int(e.get("begin_turn") or 0) <= turn]


def _effect_values(st: dict, turn: int, kind: int, command_id=None) -> int:
    total = 0
    for eff in active_effects(st, turn):
        if int(eff["effect_type"]) != kind:
            continue
        scope = int(eff.get("effect_value_1") or 0)
        if command_id is not None and scope and scope != int(command_id):
            continue
        total += int(eff.get("effect_value_2") or 0)
    return total


def training_bonus_pct(st: dict, turn: int, command_id) -> int:
    return _effect_values(st, turn, 11, command_id)


def energy_cost_pct(st: dict, turn: int, command_id) -> int:
    return _effect_values(st, turn, 12, command_id)


def zero_failure(st: dict, turn: int) -> bool:
    return any(int(e["effect_type"]) == 13 for e in active_effects(st, turn))


def race_bonus_pct(st: dict, turn: int) -> int:
    return sum(int(e.get("effect_value_2") or 0) for e in active_effects(st, turn)
               if int(e["effect_type"]) == 14 and int(e.get("effect_value_1") or 0) == 6)


def fan_bonus_pct(st: dict, turn: int) -> int:
    return sum(int(e.get("effect_value_2") or 0) for e in active_effects(st, turn)
               if int(e["effect_type"]) == 14 and int(e.get("effect_value_1") or 0) == 40)
