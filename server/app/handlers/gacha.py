
from __future__ import annotations

import copy
import functools
import json
import logging
import os
import random
from datetime import datetime

from .. import config
from .. import banner_lineup, date_sync
from .. import master_data
from .. import state as state_store
from . import collection
from . import registry
from . import shop
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "gacha_banners.json")
PITY_KEY = "gacha_pity"
DAILY_KEY = "gacha_daily_draw"
PAID_DRAWS_KEY = "gacha_paid_draws"
CONVERTED_KEY = "gacha_pity_converted"


def limit_points(full_state: dict, gacha_id) -> int:
    g = str(gacha_id)
    return max(0, ((full_state.get(PITY_KEY) or {}).get(g) or 0)
               - ((full_state.get(CONVERTED_KEY) or {}).get(g) or 0))


def limit_item_info(full_state: dict, gacha_id) -> dict:
    return {"gacha_id": int(gacha_id), "num": limit_points(full_state, gacha_id),
            "converted_item_num": 0}
_SINGLE_COST = 150
_MULTI_COST = 1500
_EXCHANGE_COST = 200
_GODDESS_STATUE = 115


def _banner_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return {str(b["gacha_id"]): b for b in json.load(fh).get("banners", [])}
    except Exception:
        return {}


_ODDS_PATH: str | None = None


def odds_path() -> str:
    from .. import region
    return _ODDS_PATH or str(region.data_dir() / "gacha_odds.json")
_odds_cache: dict = {"path": None, "mtime": None, "data": None}
_DEFAULT_RARITY_RATES = {1: 79.0, 2: 18.0, 3: 3.0}
_DEFAULT_BAND_TOTALS = {1: 790000.0, 2: 180000.0, 3: 30000.0}
_WEIGHT_WORDS = {"weightr": 1, "weightsr": 2, "weightssr": 3}


def _odds_config() -> dict | None:
    path = odds_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _odds_cache.update(path=path, mtime=None, data=None)
        return None
    if _odds_cache["path"] != path or _odds_cache["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            _odds_cache["data"] = data if isinstance(data, dict) else None
        except Exception:
            _odds_cache["data"] = None
        _odds_cache.update(path=path, mtime=mtime)
    return _odds_cache["data"]


def _resolve_odds(gacha_id) -> dict | None:
    cfg = _odds_config()
    if not cfg:
        return None
    gid = str(gacha_id)
    matched = []
    for sel, sec in cfg.items():
        if not isinstance(sec, dict) or sel.startswith("_"):
            continue
        if sel == "*":
            matched.append((0, sec))
        elif sel.endswith("*") and gid.startswith(sel[:-1]):
            matched.append((1, sec))
        elif sel == gid:
            matched.append((2, sec))
    if not matched:
        return None
    matched.sort(key=lambda t: t[0])
    out = {"on": None, "rarity_rates": None, "custom_odds": [], "toggled_at": None}
    for cls, sec in matched:
        if "on" in sec:
            out["on"] = bool(sec["on"])
            out["toggled_at"] = sec.get("toggled_at") if cls == 2 else None
        rates = sec.get("rarity_rates")
        if isinstance(rates, dict):
            out["rarity_rates"] = {**(out["rarity_rates"] or {}), **rates}
        entries = sec.get("custom_odds")
        if isinstance(entries, list):
            out["custom_odds"] += [e for e in entries if isinstance(e, dict)]
    return out


def _pinned(gacha_id, end) -> bool:
    r = _resolve_odds(gacha_id)
    if not r or r["on"] is not True:
        return False
    at = r.get("toggled_at")
    if not at or not end or end >= 2**31 - 1:
        return True
    return int(at) > end or _servertime() <= end


def _item_match(sel, card) -> bool:
    sel = str(sel).strip()
    cid = str(card.get("card_id"))
    if sel == "*":
        return True
    if sel.endswith("*"):
        prefix = sel[:-1]
        if prefix in ("1", "2", "3"):
            return (card.get("rarity") or 1) == int(prefix)
        return cid.startswith(prefix)
    return cid == sel


def _resolve_weight(value, master_odds, rarity, band_avg):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    if word in _WEIGHT_WORDS:
        return band_avg.get(_WEIGHT_WORDS[word], 0.0)
    if word.startswith("x"):
        try:
            mult = float(word[1:])
        except ValueError:
            return None
        base = master_odds if master_odds else band_avg.get(rarity, 0.0)
        return max(0.0, mult * base)
    try:
        return max(0.0, float(word))
    except ValueError:
        return None


def _apply_custom_odds(pool, entries):
    if not entries:
        return pool
    band_avg: dict = {}
    for rar in (1, 2, 3):
        band = [float(c.get("odds") or 0) for c in pool
                if (c.get("rarity") or 1) == rar]
        if band and sum(band) > 0:
            band_avg[rar] = sum(band) / len(band)
        else:
            band_avg[rar] = _DEFAULT_BAND_TOTALS[rar] / max(1, len(band))
    out = [dict(c) for c in pool]
    master = [float(c.get("odds") or 0) for c in pool]
    for e in entries:
        sel, value = e.get("items"), e.get("weight")
        if sel is None or value is None:
            continue
        sels = sel if isinstance(sel, list) else [sel]
        for i, c in enumerate(out):
            if any(_item_match(s, c) for s in sels):
                w = _resolve_weight(value, master[i], c.get("rarity") or 1, band_avg)
                if w is not None:
                    c["odds"] = w
    return out


def _pick_tier(cands, rates):
    tiers: dict = {}
    for c in cands:
        tiers.setdefault(c.get("rarity") or 1, []).append(c)
    if len(tiers) < 2:
        return None
    keys = sorted(tiers)
    weights = []
    for rar in keys:
        value = rates.get(str(rar), rates.get(rar))
        if value is None:
            value = _DEFAULT_RARITY_RATES.get(rar, 0.0)
        try:
            weights.append(max(0.0, float(value)))
        except (TypeError, ValueError):
            weights.append(_DEFAULT_RARITY_RATES.get(rar, 0.0))
    total = sum(weights)
    if total <= 0:
        return None
    r = random.uniform(0, total)
    upto = 0.0
    for rar, w in zip(keys, weights):
        upto += w
        if r <= upto:
            return tiers[rar]
    return tiers[keys[-1]]


def _servertime() -> int:
    from .. import patch
    return patch._servertime()


DRAW_DAILY = 2
DRAW_PAID = 3
PAID_COIN = 92
_RESET_UTC_HOUR = 20


def game_day() -> int:
    return (_servertime() - _RESET_UTC_HOUR * 3600) // 86400


def daily_draw_done(full_state: dict, gacha_id) -> bool:
    return (full_state.get(DAILY_KEY) or {}).get(str(gacha_id)) == game_day()


def paid_draws_used(full_state: dict, gacha_id) -> int:
    return int((full_state.get(PAID_DRAWS_KEY) or {}).get(str(gacha_id)) or 0)


def _banner(gacha_id: int) -> dict | None:
    cfg = _banner_config().get(str(gacha_id))
    row = master_data.query_one(
        "SELECT id, type, card_type, cost_single, draw_guarantee_rarity, "
        "draw_guarantee_num, additional_piece_target_rarity_1, additional_piece_num_1, "
        "additional_piece_target_rarity_2, additional_piece_num_2, "
        "additional_piece_target_card_id_1, only_once_flag, daily_pay_cost, cost_type "
        "FROM gacha_data WHERE id=?", (gacha_id,))
    if not row and not cfg:
        return None
    b = dict(row) if row else {}
    pool = (cfg or {}).get("pool")
    if not pool:
        pool = [dict(r) for r in master_data.query(
            "SELECT card_id, rarity, odds, is_pickup FROM gacha_available WHERE gacha_id=?",
            (gacha_id,))]
    b.update({"gacha_id": gacha_id, "pool": pool,
              "card_type": (cfg or {}).get("card_type", b.get("card_type", 1)),
              "single_cost": (cfg or {}).get("single_cost", _SINGLE_COST),
              "multi_cost": (cfg or {}).get("multi_cost", _MULTI_COST),
              "guarantee_rarity": b.get("draw_guarantee_rarity") or 2,
              "guarantee_num": b.get("draw_guarantee_num") or 1,
              "daily_cost": b.get("daily_pay_cost") or 0,
              "paid_only": b.get("cost_type") == PAID_COIN})
    for hook in BANNER_HOOKS:
        b = hook(b)
    odds = _resolve_odds(gacha_id)
    if odds:
        b["rarity_rates"] = odds["rarity_rates"]
        b["odds_on"] = odds["on"]
        if odds["custom_odds"]:
            b["pool"] = _apply_custom_odds(b["pool"], odds["custom_odds"])
    return b if b["pool"] else None


POOL_HOOKS: list = []

BANNER_HOOKS: list = []


def _player_pool(full_state: dict, banner: dict) -> list:
    pool = banner["pool"]
    for hook in POOL_HOOKS:
        pool = hook(full_state, banner["gacha_id"], pool)
    return pool


SSR_RARITY = 3


def forced_ssr_rate():
    value = config.get("force_ssr_rate")
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _draw_one(pool, min_rarity=0, rarity_rates=None):
    cands = [c for c in pool if (c.get("rarity") or 1) >= min_rarity] or pool

    rate = forced_ssr_rate()
    if rate is not None:
        ssr = [c for c in cands if (c.get("rarity") or 1) >= SSR_RARITY]
        rest = [c for c in cands if (c.get("rarity") or 1) < SSR_RARITY]
        if ssr and rest:
            cands = ssr if random.uniform(0, 100) < rate else rest
    elif rarity_rates:
        cands = _pick_tier(cands, rarity_rates) or cands

    total = sum(float(c.get("odds") or 0) for c in cands)
    if total <= 0:
        return random.choice(cands)
    r = random.uniform(0, total)
    upto = 0.0
    for c in cands:
        upto += float(c.get("odds") or 0)
        if r <= upto:
            return c
    return cands[-1]


def _spend_carats(full_state: dict, amount: int) -> dict | None:
    return shop.spend_carats(full_state, amount)


def _card_dresses(card_id: int, rarity: int) -> list:
    out = []
    for row in master_data.query(
            "SELECT get_dress_id_1, get_dress_id_2 FROM card_rarity_data "
            "WHERE card_id=? AND rarity<=? ORDER BY rarity", (card_id, rarity or 3)):
        for dress in (row["get_dress_id_1"], row["get_dress_id_2"]):
            if dress and dress >= 1000 and dress not in out:
                out.append(dress)
    return out


def _grant(full_state: dict, banner: dict, card, new_charas: list) -> dict:
    card_id = card["card_id"]
    rarity = card.get("rarity") or 1
    summary_add = {}
    if banner["card_type"] == 2:
        cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
        owned = next((c for c in cards if c.get("support_card_id") == card_id), None)
        if owned is None:
            now = datetime.now().strftime(_TIME_FORMAT)
            cards.append({"viewer_id": "<redacted>", "support_card_id": card_id, "exp": 0,
                          "limit_break_count": 0, "favorite_flag": 0, "stock": 0,
                          "possess_time": now, "create_time": now})
            new_flag = 1
        else:
            new_flag = 0
            owned["stock"] = (owned.get("stock") or 0) + 1
        return {"card_type": 2, "card_id": card_id, "piece_id": 0,
                "common_item_category": 0, "common_item_id": 0, "convert_piece_num": 0,
                "convert_common_item_num": 0, "additional_piece_num": 0,
                "new_flag": new_flag, "win_prize": 0}

    cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
    owned = next((c for c in cards if c.get("card_id") == card_id), None)
    pieces = full_state.setdefault("piece_list_state", [])
    items = full_state.setdefault("item_list_state", [])
    add_piece = (banner.get("additional_piece_num_1") or 0) \
        if card_id == banner.get("additional_piece_target_card_id_1") \
        else (banner.get("additional_piece_num_2") or 0)
    convert = 0
    if owned is None:
        cards.append({"card_id": card_id, "rarity": rarity, "talent_level": 1,
                      "create_time": datetime.now().strftime(_TIME_FORMAT),
                      "skill_data_array": []})
        cloth = full_state.setdefault("cloth_list_state", [])
        for dress in _card_dresses(card_id, rarity):
            if not any(c.get("cloth_id") == dress for c in cloth):
                cloth.append({"cloth_id": dress})
        chara = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        chara_id = chara["chara_id"] if chara else card_id // 100
        charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
        if not any(c.get("chara_id") == chara_id for c in charas):
            entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                     "fan": 1, "max_grade": 0, "dress_id": 2, "mini_dress_id": 2,
                     "love_point_pool": 0}
            charas.append(entry)
            new_charas.append({k: v for k, v in entry.items() if k != "love_point_pool"})
        new_flag = 1
    else:
        new_flag = 0
        row = master_data.query_one(
            "SELECT piece_num FROM gacha_piece WHERE rarity=? LIMIT 1", (rarity,))
        convert = row["piece_num"] if row else 1
        it = next((i for i in items if i.get("item_id") == _GODDESS_STATUE), None)
        if it:
            it["number"] = (it.get("number") or 0) + convert
        else:
            items.append({"item_id": _GODDESS_STATUE, "number": convert})
    if add_piece:
        p = next((p for p in pieces if p.get("piece_id") == card_id), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + add_piece
        else:
            pieces.append({"piece_id": card_id, "piece_num": add_piece})
    return {"card_type": 1, "card_id": card_id, "piece_id": card_id,
            "common_item_category": 97 if convert else 0,
            "common_item_id": _GODDESS_STATUE if convert else 0,
            "convert_piece_num": 0, "convert_common_item_num": convert,
            "additional_piece_num": add_piece, "new_flag": new_flag, "win_prize": 0}


@functools.lru_cache(maxsize=1)
def _ticket_gated_banners() -> dict:
    return {r["add_value_1"]: r["id"] for r in master_data.query(
        "SELECT id, add_value_1 FROM item_data WHERE item_category=40 AND add_value_1 > 0")}


@functools.lru_cache(maxsize=None)
def _once_only_banners() -> frozenset:
    return frozenset(r["id"] for r in master_data.query(
        "SELECT id FROM gacha_data WHERE only_once_flag=1"))


def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    pity = full_state.get(PITY_KEY) or {}
    ids = [r["id"] for r in master_data.query(
        "SELECT DISTINCT g.id AS id FROM gacha_data g "
        "JOIN gacha_available a ON a.gacha_id = g.id ORDER BY g.id")]
    ids += [int(g) for g in _banner_config() if int(g) not in ids]
    ids = ids or [30110, 30111]
    if _odds_config() is not None or date_sync.pack_sync() or banner_lineup.pack_lineup():
        windows = {r["id"]: (r["start_date"] or 0, r["end_date"] or 0)
                   for r in master_data.query(
                       "SELECT id, start_date, end_date FROM gacha_data")}
        now = _servertime()
        kept = []
        for gid in ids:
            resolved = _resolve_odds(gid)
            on = resolved["on"] if resolved else None
            if on is False:
                continue
            if gid not in windows:
                kept.append(gid)
                continue
            start, end = windows[gid]
            if on is True:
                if _pinned(gid, end):
                    kept.append(gid)
                continue
            if start <= now and (not end or now <= end):
                kept.append(gid)
        ids = kept
    starts = {r["id"]: r["start_date"] or 0 for r in master_data.query("SELECT id, start_date FROM gacha_data")}
    now = _servertime()
    ends = {r["id"]: r["end_date"] or 0 for r in master_data.query("SELECT id, end_date FROM gacha_data")}
    ids = [gid for gid in ids if starts.get(gid, 0) <= now or _pinned(gid, ends.get(gid, 0))]
    max_age = config.get("banner_max_age_days")
    if max_age is not None:
        try:
            cutoff = _servertime() - int(max_age) * 86400
        except (TypeError, ValueError):
            log.warning("client_config.json banner_max_age_days is not an integer; ignoring")
        else:
            ids = [gid for gid in ids
                   if gid not in ends or not ends[gid] or ends[gid] >= cutoff
                   or _pinned(gid, ends[gid])]
    gated = _ticket_gated_banners()
    ids = [gid for gid in ids
          if gid not in gated or shop._item_count(full_state, gated[gid]) > 0]
    once_ids = _once_only_banners()
    ids = [gid for gid in ids if gid not in once_ids or not (pity.get(str(gid)) or 0)]
    picks = full_state.get(PRIZE_PICK_KEY) or {}
    info = [{"id": gid, "is_daily_draw_end": int(daily_draw_done(full_state, gid)),
             "prize_selected_card_type": (picks.get(str(gid)) or {}).get("card_type", 0),
             "prize_selected_card_id": (picks.get(str(gid)) or {}).get("card_id", 0),
             "is_campaign_draw_enable_single": 0,
             "is_campaign_draw_enable_multi": 0, "remain_stock_num_single": 0,
             "remain_stock_num_multi": 0, "web_text": ""} for gid in ids]
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"gacha_info_list": info,
                     "limit_item_info": {
                         "expired_limit_item_list": [],
                         "limit_item_list": [limit_item_info(full_state, g) for g in pity],
                         "reward_summary_info": None}}}


def handle_limit_exchange(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    gacha_id = payload.get("gacha_id") or 0
    banner = _banner(gacha_id)
    pity = full_state.setdefault(PITY_KEY, {})
    converted = full_state.setdefault(CONVERTED_KEY, {})
    have = (pity.get(str(gacha_id)) or 0) - (converted.get(str(gacha_id)) or 0)

    wanted = payload.get("card_id")
    ex_row = None
    if wanted:
        ex_row = master_data.query_one(
            "SELECT card_id, card_type, pay_item_num FROM gacha_exchange "
            "WHERE gacha_id=? AND card_id=?", (gacha_id, wanted))
    if ex_row is None and not wanted:
        ex_row = master_data.query_one(
            "SELECT card_id, card_type, pay_item_num FROM gacha_exchange "
            "WHERE gacha_id=? ORDER BY disp_order LIMIT 1", (gacha_id,))
    ex_row = dict(ex_row) if ex_row is not None else None
    cost = int((ex_row or {}).get("pay_item_num") if ex_row else
               (_banner_config().get(str(gacha_id)) or {}).get("exchange_cost", _EXCHANGE_COST))
    if not banner or have < cost:
        return refuse()

    pool = _player_pool(full_state, banner)
    pick_id =(ex_row or {}).get("card_id") or wanted
    card = next((c for c in pool if c["card_id"] == pick_id), None)
    if card is None and pick_id:
        rar = master_data.query_one(
            "SELECT rarity FROM gacha_available WHERE gacha_id=? AND card_id=?",
            (gacha_id, pick_id))
        card = {"card_id": pick_id,
                "rarity": (rar["rarity"] if rar is not None else None) or 3,
                "is_pickup": 1}
    if card is None:
        picks = [c for c in pool if c.get("is_pickup")] or pool
        card = max(picks, key=lambda c: c.get("rarity") or 0)

    new_charas: list = []
    result = _grant(full_state, banner, card, new_charas)
    converted[str(gacha_id)] = (converted.get(str(gacha_id)) or 0) + cost
    state_store.save_state(viewer_id, full_state)

    is_support = result["card_type"] == 2
    add_cards = ([] if is_support or not result["new_flag"] else
                 [{"card_id": card["card_id"], "rarity": card.get("rarity") or 3,
                   "talent_level": 1}])
    if is_support:
        entry = next((c for c in full_state.get(collection.SUPPORT_CARD_KEY) or []
                     if c.get("support_card_id") == card["card_id"]), None)
        add_support = [copy.deepcopy(entry) if entry else
                       {"support_card_id": card["card_id"], "exp": 0,
                        "limit_break_count": 0, "favorite_flag": 0, "stock": 0}]
    else:
        add_support = []
    statues = result.get("convert_common_item_num") or 0
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "exchange_result": result,
                "reward_summary_info": {
                    "add_item_list": ([{"item_id": _GODDESS_STATUE, "number": statues}]
                                      if statues else []),
                    "add_piece_list": [], "add_card_list": add_cards,
                    "add_card_bonus_info": None, "add_support_card_list": add_support,
                    "add_support_card_num_array": [
                        {"support_card_id": c["support_card_id"], "number": 1,
                         "item_type": 51} for c in add_support],
                    "add_honor_list": [], "add_chara_list": new_charas,
                    "add_cloth_list": [{"cloth_id": d} for c in add_cards
                                       for d in _card_dresses(c["card_id"], c["rarity"])],
                    "add_music_list": [], "add_story_id_array": [], "add_fcoin": 0,
                    "add_present_num": 0, "add_total_fan": 0,
                    "new_chara_profile_array": [], "force_update_honor_id": 0},
                "limit_item_info": limit_item_info(full_state, gacha_id)}}


def handle_exec(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    gacha_id = payload.get("gacha_id") or 30110
    draw_num = int(payload.get("draw_num") or 1)
    banner = _banner(gacha_id)
    if not banner or banner.get("odds_on") is False:
        return refuse(ResultCode.GACHA_NOT_IN_TERM)
    pity = full_state.get(PITY_KEY) or {}
    if gacha_id in _once_only_banners() and (pity.get(str(gacha_id)) or 0):
        return refuse(ResultCode.GACHA_FIRST_TIME_ONLY_DRAW_END)

    draw_type = int(payload.get("draw_type") or 1)
    if draw_type == DRAW_DAILY:
        if not banner["daily_cost"] or draw_num != 1 or payload.get("item_id"):
            return refuse(ResultCode.PARAM_ERROR)
        if daily_draw_done(full_state, gacha_id):
            return refuse(ResultCode.GACHA_DAILY_DRAW_END)
    elif draw_type == DRAW_PAID:
        if not banner.get("paid_button") or draw_num != 10 or payload.get("item_id"):
            return refuse(ResultCode.PARAM_ERROR)
        banner.update({"paid_only": True, "guarantee_rarity": SSR_RARITY})
    limit = banner.get("paid_limit")
    if limit and limit[0] == draw_type and paid_draws_used(full_state, gacha_id) >= limit[1]:
        return refuse(ResultCode.GACHA_NOT_IN_TERM)

    banner["pool"] = _player_pool(full_state, banner)
    promised = None
    if banner.get("promised_card"):
        want = int(payload.get("draw_guarantee_card_id") or 0)
        promised = next((c for c in banner["pool"] if c["card_id"] == want
                         and (c.get("rarity") or 1) >= banner["guarantee_rarity"]), None)
        if promised is None or draw_num < 10:
            return refuse(ResultCode.GACHA_UNSELECTABLE_CARD_SELECTED)

    coin_info = None
    spent_items = []
    if not payload.get("item_id"):
        cost = banner["multi_cost"] if draw_num >= 10 else banner["single_cost"] * draw_num
        paid_only = banner.get("paid_only")
        if draw_type == DRAW_DAILY:
            cost, paid_only = banner["daily_cost"], True
        coin_info = (shop.spend_paid_carats(full_state, cost) if paid_only
                     else _spend_carats(full_state, cost))
        if coin_info is None:
            return refuse()
    else:
        item_id = int(payload["item_id"])
        items = full_state.setdefault("item_list_state", [])
        it = next((i for i in items if i.get("item_id") == item_id), None)
        if it is None or (it.get("number") or 0) < draw_num:
            return refuse()
        it["number"] = (it.get("number") or 0) - draw_num
        cat_row = master_data.query_one(
            "SELECT item_category FROM item_data WHERE id=?", (item_id,))
        item_type = (cat_row["item_category"] if cat_row else None) or 40
        spent_items = [{"item_type": item_type, "item_id": item_id,
                        "item_num": it["number"]}]

    rates = banner.get("rarity_rates")
    drawn = [_draw_one(banner["pool"], rarity_rates=rates) for _ in range(draw_num)]
    if draw_num >= 10:
        need = banner["guarantee_rarity"]
        for i in range(banner["guarantee_num"] or 1):
            drawn[-(i + 1)] = _draw_one(banner["pool"], min_rarity=need,
                                        rarity_rates=rates)
        if promised:
            drawn[-1] = promised

    new_charas: list = []
    results = [_grant(full_state, banner, c, new_charas) for c in drawn]
    pity = full_state.setdefault(PITY_KEY, {})
    if banner.get("counts_pity", True):
        pity[str(gacha_id)] = (pity.get(str(gacha_id)) or 0) + draw_num
    if draw_type == DRAW_DAILY:
        full_state.setdefault(DAILY_KEY, {})[str(gacha_id)] = game_day()
    if limit and limit[0] == draw_type:
        used = full_state.setdefault(PAID_DRAWS_KEY, {})
        used[str(gacha_id)] = paid_draws_used(full_state, gacha_id) + 1
    _record_history(full_state, gacha_id, banner, draw_num, payload, coin_info,
                    spent_items, results)
    state_store.save_state(viewer_id, full_state)

    cards_now = full_state.get(collection.CARD_LIST_KEY) or []
    add_cards = []
    for r in results:
        if r["card_type"] != 1 or not r["new_flag"]:
            continue
        entry = next((c for c in cards_now if c.get("card_id") == r["card_id"]), None)
        add_cards.append({
            "card_id": r["card_id"],
            "rarity": (entry or {}).get("rarity", 1),
            "talent_level": (entry or {}).get("talent_level", 1),
            "create_time": (entry or {}).get("create_time")
                or datetime.now().strftime(_TIME_FORMAT),
        })
    support_cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
    add_support: list = []
    support_counts: dict = {}
    seen_support_ids: set = set()
    for r in results:
        if r["card_type"] != 2:
            continue
        support_counts[r["card_id"]] = support_counts.get(r["card_id"], 0) + 1
        if r["card_id"] in seen_support_ids:
            continue
        seen_support_ids.add(r["card_id"])
        entry = next((c for c in support_cards if c.get("support_card_id") == r["card_id"]), None)
        add_support.append(copy.deepcopy(entry) if entry else
                           {"support_card_id": r["card_id"], "exp": 0,
                            "limit_break_count": 0, "favorite_flag": 0, "stock": 0})
    piece_totals: dict = {}
    for r in results:
        num = r.get("additional_piece_num")
        if num:
            piece_totals[r["piece_id"]] = piece_totals.get(r["piece_id"], 0) + num
    add_pieces = [{"piece_id": pid, "piece_num": num} for pid, num in piece_totals.items()]
    statues = sum(r.get("convert_common_item_num") or 0 for r in results)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "gacha_result_list": results,
                "bonus_item_array": [],
                "reward_summary_info": {
                    "add_item_list": ([{"item_id": _GODDESS_STATUE, "number": statues}]
                                      if statues else []),
                    "add_piece_list": add_pieces, "add_card_list": add_cards,
                    "add_card_bonus_info": None, "add_support_card_list": add_support,
                    "add_support_card_num_array": [
                        {"support_card_id": sid, "number": num, "item_type": 51}
                        for sid, num in support_counts.items()],
                    "add_honor_list": [], "add_chara_list": new_charas,
                    "add_cloth_list": [{"cloth_id": d} for c in add_cards
                                       for d in _card_dresses(c["card_id"], c["rarity"])],
                    "add_music_list": [], "add_story_id_array": [], "add_fcoin": 0,
                    "add_present_num": 0, "add_total_fan": 0,
                    "new_chara_profile_array": [], "force_update_honor_id": 0},
                "item_list": spent_items, "coin_info": copy.deepcopy(coin_info),
                "limit_item_info": limit_item_info(full_state, gacha_id)}}


HISTORY_KEY = "gacha_history"
PRIZE_PICK_KEY = "gacha_prize_pick"

HISTORY_LIMIT = 100


def _record_history(full_state, gacha_id, banner, draw_num, payload, coin_info,
                    spent_items, results) -> None:
    hist = full_state.setdefault(HISTORY_KEY, {"next_id": 1, "execs": [], "rewards": {}})
    hist.setdefault("next_id", 1)
    hist.setdefault("execs", [])
    hist.setdefault("rewards", {})
    exec_id = int(hist["next_id"])
    hist["next_id"] = exec_id + 1

    if payload.get("item_id"):
        cost_id, cost_count = int(payload["item_id"]), draw_num
    elif coin_info is not None:
        cost_id = 0
        cost_count = (banner["multi_cost"] if draw_num >= 10
                      else banner["single_cost"] * draw_num)
    else:
        cost_id, cost_count = 0, 0

    hist["execs"].insert(0, {
        "exec_history_id": exec_id,
        "gacha_id": int(gacha_id),
        "gacha_card_type": (results[0]["card_type"] if results else 1),
        "draw_type": 2 if draw_num >= 10 else 1,
        "draw_num": draw_num,
        "cost_id": cost_id,
        "cost_count": cost_count,
        "create_time": datetime.now().strftime(_TIME_FORMAT),
    })
    hist["rewards"][str(exec_id)] = [
        {"exec_history_id": exec_id, "disp_order": i,
         "card_type": r["card_type"], "card_id": r["card_id"],
         "piece_id": r["piece_id"],
         "common_item_category": r["common_item_category"],
         "common_item_id": r["common_item_id"],
         "convert_piece_num": r["convert_piece_num"],
         "convert_common_item_num": r["convert_common_item_num"],
         "additional_piece_num": r["additional_piece_num"]}
        for i, r in enumerate(results)]

    dropped = hist["execs"][HISTORY_LIMIT:]
    del hist["execs"][HISTORY_LIMIT:]
    for row in dropped:
        hist["rewards"].pop(str(row["exec_history_id"]), None)


@registry.endpoint("gacha/get_history")
def handle_get_history(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    hist = full_state.get(HISTORY_KEY) or {}
    execs = hist.get("execs") or []
    rewards = hist.get("rewards") or {}
    flat = []
    for row in execs:
        flat.extend(rewards.get(str(row["exec_history_id"])) or [])
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"gacha_exec_history_array": copy.deepcopy(execs),
                     "gacha_reward_history_array": flat}}


@registry.endpoint("gacha/get_prize_history")
def handle_get_prize_history(payload: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"prize_history_array": []}}


@registry.endpoint("gacha/select_prize")
def handle_select_prize(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    gacha_id = payload.get("gacha_id")
    if not _banner(gacha_id):
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    picks = full_state.setdefault(PRIZE_PICK_KEY, {})
    picks[str(int(gacha_id))] = {"card_type": int(payload.get("card_type") or 0),
                                 "card_id": int(payload.get("card_id") or 0)}
    state_store.save_state(viewer_id, full_state)
    log.info("gacha/select_prize: banner %s prize set to card_type=%s card_id=%s",
             gacha_id, payload.get("card_type"), payload.get("card_id"))
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}
