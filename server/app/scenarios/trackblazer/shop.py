"""TRACKBLAZER (scenario 4) -- the Pro Shop.

The single biggest addition over URA, and it has no analogue there at all.

WHAT IS MASTER DATA AND WHAT IS NOT
-----------------------------------
Master data gives the SCHEDULE and the CATALOGUE, and nothing else:

    single_mode_free_shop         11 six-turn windows, turns 13-18 .. 73-78,
                                  each naming a lineup_group_id
    single_mode_free_shop_item    53 goods -- price, effect group, the
                                  mutual-exclusion group and priority
    single_mode_free_shop_effect  64 typed effects behind those groups

The shop BUTTON is master data too, and not a rule anyone had to invent:
single_mode_turn for turn_set 4 is byte-identical to URA's turn_set 1 except for
BGM and one column -- `unique_command` flips to 1 on turns 13-78. Turn 13 is
exactly where the first shop window opens, i.e. the turn after the Debut.

What is NOT in master.mdb is the STOCK: there is no lineup-group table, and
max_lineup_num is a useless 99 on all eleven rows. The per-slot fill
probabilities and item pools were measured from 1,741 careers and live in
server/app/data/trackblazer_shop_model.json. docs/TRACKBLAZER_SHOP.md carries
the derivation; the model is loaded here rather than re-fitted.

TWO INDEPENDENT SYSTEMS sharing one display:

    BASE STOCK     rolled once when a reset opens, lasts the whole 6-turn
                   window, `limit_turn == 0`, drawn from per-slot pools
    LIMITED OFFERS appear the turn after a RACE, expire two turns later,
                   `limit_turn > 0`, drawn from a different pool entirely

They are different pools -- tested per reset and rejected at p ~ 0 for all
eleven. The clearest case: Stat Scrolls (1201-1205) appear only as limited
offers, never in base stock, across all 1,741 careers.

THE EFFECT VOCABULARY (decoded from single_mode_free_shop_effect, and
independently confirmed against `item_effect_array` on the wire -- a gold Cleat
Hammer shows as {item_id: 11002, effect_type: 14, effect_value_2: 35,
begin_turn: 74, end_turn: 74}):

    1  instant param add   v1 target (1-5 stats, 10 vital, 11 max vital,
                           20 motivation), v2 amount
    2  facility level +1   v1 command_id 101/102/103/105/106
    3  bond               (1, 0, n) every card +n; (2, chara_id, n) that NPC +n
    6  conditions         (1, id) grant a good one; (2, id) remove a bad one
    10 reshuffle          re-roll this turn's support placement
    11 training bonus %   v1 0 = all facilities, else a command_id; v2 percent
    12 energy cost %      paired with 11 on the ankle weights
    13 failure rate 0
    14 race / fan bonus % v1 6 = race bonus, 40 = fan bonus

Types 11-14 are DURATIONAL and ride item_effect_array with begin_turn/end_turn
attached; 1, 2, 3, 6 and 10 land the moment the item is used. effect_group +
effect_priority encode mutual exclusion -- all three megaphones share group 100,
the four ankle weights sit in groups 200-500 (one per facility, and there is no
Wit one), the hammers share group 700, and the condition heals share group 1000
with Miracle Cure (4201) at priority 1 carrying all six removals at once.
"""

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

# Every capture shows limit_buy_count 1 on every offer, base stock included --
# master's limit_num 5 is the per-career cap on the ITEM, not the per-offer one.
_LIMIT_BUY_COUNT = 1

# A limited offer is visible on the turn it appears and the two after it: the
# capture shows offers with limit_turn 73 and limit_turn 75 side by side in the
# same lineup, which only works if limit_turn is the LAST valid turn and the
# window is three turns wide.
_LIMITED_LIFETIME = 2

# The chance a race produces a limited offer, by placement (TRACKBLAZER_SHOP.md
# section 1): grade-independent, and a Debut never produces one.
_OFFER_CHANCE_WIN = 0.75
_OFFER_CHANCE_PLACED = 0.40

# Winning a Rival Race "raises the odds of new shop items appearing" (MANT). No
# number is published, so the win chance is simply not reduced and a placed
# finish is lifted to the win rate. KNOB.
_RIVAL_OFFER_BONUS = _OFFER_CHANCE_WIN - _OFFER_CHANCE_PLACED

_DURATIONAL = (11, 12, 13, 14)


# ------------------------------------------------------------- the model ----

_model_cache: dict | None = None


def model() -> dict:
    global _model_cache
    if _model_cache is None:
        try:
            with open(_MODEL_PATH, "r", encoding="utf-8") as fh:
                _model_cache = json.load(fh)
        except Exception:                                     # noqa: BLE001
            log.exception("trackblazer: could not load the shop model at %s",
                          _MODEL_PATH)
            _model_cache = {"resets": {}}
    return _model_cache


def _catalogue() -> dict:
    """{item_id: {price, effect_group_id, effect_group, effect_priority}}."""
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
    """The single_mode_free_shop row covering `turn`, or None before turn 13."""
    return master_data.query_one(
        "SELECT id, start_turn, end_turn FROM single_mode_free_shop "
        "WHERE start_turn<=? AND end_turn>=?", (int(turn), int(turn)))


# ----------------------------------------------------------- generation ----

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
    """N ordered slots, each filling with its own p_j and drawing from its own
    pool. Empty slots are simply omitted, which is what makes real lineups vary
    in length while staying ordered by item category."""
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
    """Open the reset window `turn` falls in (rolling its base stock once) and
    expire any limited offer whose limit_turn has passed. True when it changed
    something and the state therefore has to be persisted.

    Called from the per-response tick rather than from a turn hook, so a career
    resumed mid-window comes back with its lineup intact and one that skips a
    window never carries the old one forward."""
    st = impl.state(full_state)
    turn = int(turn or 0)
    window = window_for_turn(turn)
    shop_id = int(window["id"]) if window else 0
    changed = False
    if shop_id != int(st.get("shop_id") or 0):
        changed = True
        st["shop_id"] = shop_id
        # The limited offers survive a reset boundary -- they are their own
        # system, on their own two-turn clock (see the module docstring).
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
    """The measured post-race chance of a limited offer. Grade-independent."""
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
    """Roll this race's limited offer. The lineup it draws from is the current
    reset's `limited_pool`, which is a different pool from any slot's."""
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
    item_id = _draw(rng, pool)
    if item_id is None:
        return False
    st.setdefault("offers", []).append(
        _offer(st, item_id, limit_turn=int(turn) + _LIMITED_LIFETIME))
    return True


# ------------------------------------------------------------- buying ------

def buy(full_state: dict, shop_item_id: int) -> bool:
    """Buy one of an offer BY SLOT, which is what the request carries -- the
    client sends shop_item_id, never the item id, because the same item can be
    offered twice in one lineup at different prices."""
    st = impl.state(full_state)
    for offer in st.get("offers") or []:
        if int(offer.get("shop_item_id") or 0) != int(shop_item_id):
            continue
        if int(offer.get("item_buy_num") or 0) >= int(offer.get("limit_buy_count") or 1):
            return False
        if not impl.spend_coins(st, int(offer.get("coin_num") or 0)):
            return False
        offer["item_buy_num"] = int(offer.get("item_buy_num") or 0) + 1
        # A bought-out base offer STAYS in the lineup with its counter raised;
        # that is what makes any single snapshot inside a window a complete
        # observation of its base stock.
        key = str(int(offer["item_id"]))
        items = st.setdefault("items", {})
        items[key] = int(items.get(key) or 0) + 1
        # LIFETIME PURCHASE COUNT for epithet 181 ("purchased at least 20 items
        # from the Pro Shop"). It cannot be recovered from either neighbour at
        # graduation: `offers` is regenerated every reset window (shop_id 1-11),
        # so summing item_buy_num sees only the LAST window, and `items` is
        # consumed by using the items it holds.
        st["items_bought"] = int(st.get("items_bought") or 0) + 1
        return True
    return False


# -------------------------------------------------------------- using ------

# The Director is target 102 and the Reporter 103; a type-3 effect names them by
# CHARA id instead, so the two id spaces have to be bridged here.
_CHARA_TO_TARGET = {9002: 102, 9003: 103}

_PARAM_STATS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
_BOND_MAX = 100


def _add_param(chara_info: dict, target: int, amount: int) -> None:
    stat = _PARAM_STATS.get(target)
    if stat:
        cap_key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[stat] = min(int(chara_info.get(stat) or 0) + amount,
                               int(chara_info.get(cap_key) or 9999))
    elif target == 10:                                   # energy
        chara_info["vital"] = max(0, min(int(chara_info.get("vital") or 0) + amount,
                                         int(chara_info.get("max_vital") or 100)))
    elif target == 11:                                   # max energy
        chara_info["max_vital"] = int(chara_info.get("max_vital") or 100) + amount
    elif target == 20:                                   # motivation, 1-5
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
        if v1 == 1:                                      # every support card
            for c in chara_info.get("support_card_array") or []:
                _bond(chara_info, c.get("position"), v3)
        else:                                            # one named character
            _bond(chara_info, _CHARA_TO_TARGET.get(v2, v2), v3)
    elif kind == 6:
        conds = list(chara_info.get("chara_effect_id_array") or [])
        if v1 == 1 and v2 not in conds:
            conds.append(v2)
        elif v1 == 2:
            conds = [c for c in conds if int(c) != v2]
        chara_info["chara_effect_id_array"] = conds
    elif kind == 10:
        # THE RESET WHISTLE. The placement roll is seeded by (card_id, turn) so
        # a turn's layout is stable when rebuilt -- which is right, and is also
        # exactly why re-rolling it needs a salt the scenario can move. See
        # Scenario.placement_salt.
        st = impl.state(full_state)
        st["placement_salt"] = int(st.get("placement_salt") or 0) + 1


def _supersedes(st: dict, item_id: int, turn: int) -> None:
    """Drop any active effect this item's mutual-exclusion group outranks.

    effect_group + effect_priority is how master data says "only one megaphone
    at a time": a stronger one replaces a weaker one rather than stacking, and a
    weaker one used under a stronger one does nothing."""
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
                # An active stronger item wins: this use is absorbed.
                st["absorbed"] = True
                return
            continue                                     # weaker: superseded
        kept.append(eff)
    st["effects"] = kept


def use(full_state: dict, chara_info: dict, career_data: dict,
        item_id: int, turn: int, count: int = 1) -> bool:
    """Consume `count` of an owned item and apply its effects."""
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


# ------------------------------------------------------- active effects ----

def active_effects(st: dict, turn: int) -> list:
    """The item_effect_array for this turn, and the expiry sweep that keeps it
    honest -- an effect whose end_turn has passed is dropped, not merely hidden.

    NOTE the held-turn hazard: a scenario that holds the SERVED turn while the
    persisted one advances would expire these against the wrong number. This
    scenario holds no turn, and if one is ever added it must read the persisted
    turn here, not the served one."""
    turn = int(turn or 0)
    live = [e for e in (st.get("effects") or []) if int(e.get("end_turn") or 0) >= turn]
    if len(live) != len(st.get("effects") or []):
        st["effects"] = live
    return [dict(e) for e in live if int(e.get("begin_turn") or 0) <= turn]


def _effect_values(st: dict, turn: int, kind: int, command_id=None) -> int:
    """Summed percentage across every active effect of one type that applies to
    this facility. effect_value_1 == 0 means "all facilities"."""
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
    """Megaphones (all facilities) and ankle weights (one facility)."""
    return _effect_values(st, turn, 11, command_id)


def energy_cost_pct(st: dict, turn: int, command_id) -> int:
    """The ankle weights' other half -- always paired with their type 11."""
    return _effect_values(st, turn, 12, command_id)


def zero_failure(st: dict, turn: int) -> bool:
    """The Good-Luck Charm. Type 13 carries no value; its presence is the rule."""
    return any(int(e["effect_type"]) == 13 for e in active_effects(st, turn))


def race_bonus_pct(st: dict, turn: int) -> int:
    """Cleat Hammers -- effect_value_1 6 is the race bonus."""
    return sum(int(e.get("effect_value_2") or 0) for e in active_effects(st, turn)
               if int(e["effect_type"]) == 14 and int(e.get("effect_value_1") or 0) == 6)


def fan_bonus_pct(st: dict, turn: int) -> int:
    """Glow Sticks -- effect_value_1 40 is the fan bonus."""
    return sum(int(e.get("effect_value_2") or 0) for e in active_effects(st, turn)
               if int(e["effect_type"]) == 14 and int(e.get("effect_value_1") or 0) == 40)
