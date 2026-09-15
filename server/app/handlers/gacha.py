"""Gacha: banners, odds, pulls, and real carat cost.

Wire format recovered from 15 REAL captured pulls (14 ten-pulls on support
banner 30111 + 1 single chara draw), master.mdb for pools/odds/prices:

- gacha/index -> per-banner USER STATE only (pity counters, free-draw stock,
  daily-draw flags). Banner metadata/art is client-side from master.
- gacha/exec  -> {gacha_id, draw_type, draw_num, current_num, item_id}; the
  response returns gacha_result_list (one entry per card drawn),
  reward_summary_info (what to add to the collections), item_list (spent
  tickets), coin_info (post-deduction wallet, null when nothing was spent)
  and limit_item_info (pity counter, +1 per draw).

Odds come straight from master.mdb gacha_available (per-card `odds` out of
1,000,000; buckets sum to 790000/180000/30000 = 79% R / 18% SR / 3% SSR),
so the default banners are the REAL current pools at the REAL rates. Any of
that can be overridden per banner in data/gacha_banners.json -- see
_banner_config: put a file there to swap pools, pickups, odds or prices
without touching code. On top of that, data/gacha_odds.json (hot-reloaded,
see _odds_config below and CONFIG_GUIDE.md) layers per-banner visibility,
per-rarity tier rates and per-card weight rewrites over whatever pool the
banner ends up with.

Costs (master gacha_data, confirmed on the wire): 150 carats single /
1500 ten-pull; carats live in coin_info_state {fcoin, coin} and the client
shows their SUM, so a pull spends free carats first, then paid.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import os
import random
from datetime import datetime

from .. import config
from .. import master_data
from .. import state as state_store
from . import collection
from . import registry
from . import shop

log = logging.getLogger("uma-server")

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"   # matches load.py's TIME_FORMAT

# server/data/gacha_banners.json  (this file is server/app/handlers/gacha.py)
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "gacha_banners.json")
PITY_KEY = "gacha_pity"            # {str(gacha_id): draws_so_far}
CONVERTED_KEY = "gacha_pity_converted"  # {str(gacha_id): points already SPENT on
                                        # limit exchanges (reported back as
                                        # limit_item_list.converted_item_num)}
_SINGLE_COST = 150
_MULTI_COST = 1500
_EXCHANGE_COST = 200               # exchange points for one banner card (spark)
_GODDESS_STATUE = 115              # dupe-chara conversion item (gacha_piece)


def _banner_config() -> dict:
    """Optional server-side overrides, keyed by str(gacha_id). Absent by
    default -- banners then come straight from master.mdb."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return {str(b["gacha_id"]): b for b in json.load(fh).get("banners", [])}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# LAYERED CUSTOM-ODDS CONFIG -- server/data/gacha_odds.json (hot-reloaded on
# mtime change, the app/config.py pattern; full format doc in CONFIG_GUIDE.md,
# copyable starter in data/gacha_odds.json.example).
#
# Top-level keys are BANNER SELECTORS: "30111" (exact id), "301*" (id prefix),
# "*" (every banner); "_"-prefixed keys are comments. Section keys:
#   on           -- true forces the banner into gacha/index, false hides it
#                   (and refuses exec). While the file EXISTS, unmatched
#                   banners default to master start_date/end_date vs
#                   servertime; with no file at all every pooled banner stays
#                   listed (the long-standing private-server behaviour).
#   rarity_rates -- {"1": pct, "2": pct, "3": pct}: tier-first draw replacing
#                   the flat 79/18/3; unspecified tiers keep their default
#                   share, weights normalized over the tiers present. The
#                   force_ssr_rate testing knob still wins when set.
#   custom_odds  -- ordered [{"items": SEL, "weight": W}] per-card weight
#                   rewrites; the LAST matching entry decides a card's weight.
#
# Precedence per banner: master defaults -> "*" section -> matching prefix
# sections (in file order) -> exact-id section.
# ---------------------------------------------------------------------------
_ODDS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "gacha_odds.json")
_odds_cache: dict = {"path": None, "mtime": None, "data": None}
_DEFAULT_RARITY_RATES = {1: 79.0, 2: 18.0, 3: 3.0}     # the real master split
_DEFAULT_BAND_TOTALS = {1: 790000.0, 2: 180000.0, 3: 30000.0}
_WEIGHT_WORDS = {"weightr": 1, "weightsr": 2, "weightssr": 3}


def _odds_config() -> dict | None:
    """Parsed gacha_odds.json, or None when absent/unreadable (layer off).
    Re-read when the file's mtime (or a test-patched _ODDS_PATH) changes --
    edits apply on the next request, no server restart."""
    path = _ODDS_PATH
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
        except Exception:  # noqa: BLE001 - a bad hand-edit must not break pulls
            _odds_cache["data"] = None
        _odds_cache.update(path=path, mtime=mtime)
    return _odds_cache["data"]


def _resolve_odds(gacha_id) -> dict | None:
    """Merge every section matching this banner, in precedence order ("*",
    then prefix selectors in file order, then the exact id). Later sections
    overwrite "on" and merge "rarity_rates" per tier; their custom_odds
    entries are APPENDED, so later sections rewrite earlier ones per card.
    Returns {"on": bool|None, "rarity_rates": dict|None, "custom_odds": list}
    or None when there is no config file / nothing matches."""
    cfg = _odds_config()
    if not cfg:
        return None
    gid = str(gacha_id)
    matched = []                       # (precedence_class, section)
    for sel, sec in cfg.items():       # dict preserves the file's key order
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
    matched.sort(key=lambda t: t[0])   # stable: file order kept within a class
    out = {"on": None, "rarity_rates": None, "custom_odds": []}
    for _, sec in matched:
        if "on" in sec:
            out["on"] = bool(sec["on"])
        rates = sec.get("rarity_rates")
        if isinstance(rates, dict):
            out["rarity_rates"] = {**(out["rarity_rates"] or {}), **rates}
        entries = sec.get("custom_odds")
        if isinstance(entries, list):
            out["custom_odds"] += [e for e in entries if isinstance(e, dict)]
    return out


def _item_match(sel, card) -> bool:
    """custom_odds item selector: "*" = every card; "1*"/"2*"/"3*" = every
    card of that RARITY; any longer prefix ("3011*") = card-id prefix;
    anything else = exact card id."""
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
    """Weight vocabulary -> a concrete odds number, or None to leave the card
    alone. number = absolute weight; "weightR"/"weightSR"/"weightSSR" = this
    pool's average MASTER weight for that band ("give this SSR the weight a
    normal R has"); "x2"/"x0.5" = multiplier on the card's own MASTER weight
    (deliberately not the layered value, so multipliers are order-stable)."""
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
    """Copy of `pool` with per-card odds rewritten by the custom_odds entries
    in order -- the LAST entry to match a card decides its weight. "items"
    accepts a single selector or a list of selectors (any-of)."""
    if not entries:
        return pool
    band_avg: dict = {}
    for rar in (1, 2, 3):
        band = [float(c.get("odds") or 0) for c in pool
                if (c.get("rarity") or 1) == rar]
        if band and sum(band) > 0:
            band_avg[rar] = sum(band) / len(band)
        else:                          # band absent/odds-free: real split share
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
    """rarity_rates tier-first pick: split the candidates by rarity and choose
    the TIER by the configured percentages (unspecified tiers keep the real
    79/18/3 shares; weights normalized over the tiers actually present, so
    they need not sum to 100). Returns the chosen sub-pool, or None when
    there is nothing to choose between."""
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
    """Frozen servertime for banner date-window checks (see patch.py)."""
    from .. import patch               # lazy: patch imports nothing of ours
    return patch._servertime()


def _banner(gacha_id: int) -> dict | None:
    cfg = _banner_config().get(str(gacha_id))
    row = master_data.query_one(
        "SELECT id, type, card_type, cost_single, draw_guarantee_rarity, "
        "draw_guarantee_num, additional_piece_target_rarity_1, additional_piece_num_1, "
        "additional_piece_target_rarity_2, additional_piece_num_2, "
        "additional_piece_target_card_id_1, only_once_flag FROM gacha_data WHERE id=?", (gacha_id,))
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
              "guarantee_num": b.get("draw_guarantee_num") or 1})
    # gacha_odds.json layers on top of whatever pool we ended up with (master
    # or gacha_banners.json): rewrite per-card weights, attach tier rates and
    # the resolved on/off flag for handle_index / handle_exec to honour.
    odds = _resolve_odds(gacha_id)
    if odds:
        b["rarity_rates"] = odds["rarity_rates"]
        b["odds_on"] = odds["on"]
        if odds["custom_odds"]:
            b["pool"] = _apply_custom_odds(b["pool"], odds["custom_odds"])
    return b if b["pool"] else None


SSR_RARITY = 3          # gacha_available.rarity: 1 = R, 2 = SR, 3 = SSR


def forced_ssr_rate():
    """TESTING KNOB -- `force_ssr_rate` in client_config.json.

    Percent chance that any single draw is an SSR, replacing the real ~3%.
    Applies to EVERY banner, single and multi alike. Null (the default) means
    the real master.mdb odds decide, which is the only setting for real play.

    Read through app.config, so it re-reads on file change with no server
    restart -- set it back to null to restore the true rates."""
    value = config.get("force_ssr_rate")
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _draw_one(pool, min_rarity=0, rarity_rates=None):
    cands = [c for c in pool if (c.get("rarity") or 1) >= min_rarity] or pool

    # SSR-RATE OVERRIDE: split the candidates by rarity and pick the TIER first,
    # then draw inside it with the normal odds. Weighting within a tier is left
    # alone on purpose -- pickup cards stay likelier among the SSRs, and the R/SR
    # split keeps its real shape -- so only the SSR rate itself changes.
    rate = forced_ssr_rate()
    if rate is not None:
        ssr = [c for c in cands if (c.get("rarity") or 1) >= SSR_RARITY]
        rest = [c for c in cands if (c.get("rarity") or 1) < SSR_RARITY]
        if ssr and rest:
            cands = ssr if random.uniform(0, 100) < rate else rest
        # If a sub-pool is empty there is nothing to choose between (e.g. the
        # 10-pull's guaranteed-SR slot already filtered to high rarities), so
        # leave `cands` as-is rather than forcing an impossible tier.
    elif rarity_rates:
        # RARITY_RATES (gacha_odds.json): the same tier-first idea, but with a
        # full per-tier split instead of only the SSR rate. force_ssr_rate
        # deliberately wins above (backward-compatible testing knob).
        cands = _pick_tier(cands, rarity_rates) or cands

    total = sum(float(c.get("odds") or 0) for c in cands)
    if total <= 0:
        # No candidate carries any weight (an odds-free custom pool, or every
        # weight zeroed by custom_odds) -- draw uniformly. NB the old code gave
        # a 0-odds card in an otherwise-weighted pool the AVERAGE weight; with
        # custom_odds an explicit 0 must mean "never", so it no longer does.
        return random.choice(cands)
    r = random.uniform(0, total)
    upto = 0.0
    for c in cands:
        upto += float(c.get("odds") or 0)
        if r <= upto:
            return c
    return cands[-1]


def _spend_carats(full_state: dict, amount: int) -> dict | None:
    """Deduct carats, free first then paid. Returns the new coin_info, or
    None if the player can't afford it. Thin wrapper over shop.spend_carats
    (the single shared implementation, also used by _pay/daily_races.py's
    recovery-ticket purchase)."""
    return shop.spend_carats(full_state, amount)


def _grant(full_state: dict, banner: dict, card, new_charas: list) -> dict:
    """Apply one drawn card to the collections; return its gacha_result_list
    entry (shape verified against the real capture). Any character newly
    unlocked by the draw is appended to `new_charas` for the response."""
    card_id = card["card_id"]
    rarity = card.get("rarity") or 1
    summary_add = {}
    if banner["card_type"] == 2:                       # SUPPORT card
        cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
        owned = next((c for c in cards if c.get("support_card_id") == card_id), None)
        if owned is None:
            cards.append({"viewer_id": "<redacted>", "support_card_id": card_id, "exp": 0,
                          "limit_break_count": 0, "favorite_flag": 0, "stock": 0})
            new_flag = 1
        else:
            # A gacha dupe is a COPY, not a free auto-uncap -- it always
            # banks into `stock`; the player then spends stock manually via
            # support_card/limit_break (cards.py, material_support_card_num
            # route), the same real endpoint an ITEM-based uncap also goes
            # through. Confirmed against a real capture with actual dupes
            # (captures/20260816_140811/0030_gacha_exec.json): every dupe
            # there raised `stock` while `limit_break_count` stayed exactly
            # 0. User-reported 2026-08-19: "Why are cards auto uncapping? I
            # need to uncap them manually with my copies" -- this
            # auto-raise-then-overflow-to-stock logic was simply wrong,
            # not a real fallback behavior.
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
        chara = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        chara_id = chara["chara_id"] if chara else card_id // 100
        charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
        if not any(c.get("chara_id") == chara_id for c in charas):
            entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                     "fan": 1, "max_grade": 0, "dress_id": 2, "mini_dress_id": 2,
                     "love_point_pool": 0}
            charas.append(entry)
            # Announce the new character in the response -- the client applies
            # add_chara_list live; without it the card only appears after a
            # full restart (same class of bug as the skill screen).
            new_charas.append({k: v for k, v in entry.items() if k != "love_point_pool"})
        new_flag = 1
    else:                                              # dupe -> Goddess Statue
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
    """{gacha_id: required ticket item_id} for every banner whose master
    cost_type is GACHA_TICKET(40) and has a real dedicated one-off ticket
    linking to it (item_data.add_value_1 = gacha_id) -- e.g. item 155 "SR+
    Make Debut Scout Race 2" -> gacha_id 20008. Cached: master.mdb is
    read-only for the process lifetime.

    User-confirmed 2026-08-24 (live-reported: "the asset it says you pull
    with is white and trying to pull on it does nothing"): traced to the
    player owning ZERO of the banner's required ticket -- the pull was
    correctly refusing, but the banner stayed listed anyway with nothing
    that could ever pull it, showing an empty/unowned ticket icon. handle_
    index uses this to drop such a banner from the list entirely until the
    player actually owns at least one matching ticket, same as the real
    game only ever surfaces a ticket-exchange banner once you hold the
    ticket for it."""
    return {r["add_value_1"]: r["id"] for r in master_data.query(
        "SELECT id, add_value_1 FROM item_data WHERE item_category=40 AND add_value_1 > 0")}


@functools.lru_cache(maxsize=None)
def _once_only_banners() -> frozenset:
    """gacha_id set for every banner with master gacha_data.only_once_flag
    set -- the discounted single-pull "first purchase" style offers. Cached:
    master.mdb is read-only for the process lifetime."""
    return frozenset(r["id"] for r in master_data.query(
        "SELECT id FROM gacha_data WHERE only_once_flag=1"))


def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    pity = full_state.get(PITY_KEY) or {}
    # EVERY banner that actually has a card pool -- the real server only lists
    # the handful currently running, but on a private server there's no reason
    # to hide the rest (132 banners across the game's history are pullable).
    # Config overrides are additive: a custom gacha_id not in master still
    # shows up.
    ids = [r["id"] for r in master_data.query(
        "SELECT DISTINCT g.id AS id FROM gacha_data g "
        "JOIN gacha_available a ON a.gacha_id = g.id ORDER BY g.id")]
    ids += [int(g) for g in _banner_config() if int(g) not in ids]
    ids = ids or [30110, 30111]
    # gacha_odds.json visibility layer. Only while the file EXISTS: an
    # explicit "on" wins (true = force-listed, false = hidden); unmatched
    # banners fall back to master start_date/end_date vs the frozen
    # servertime (config-only banners master doesn't know stay listed). With
    # no file the everything-listed behaviour above is untouched.
    if _odds_config() is not None:
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
            if on is True or gid not in windows:
                kept.append(gid)
                continue
            start, end = windows[gid]
            if start <= now and (not end or now <= end):
                kept.append(gid)
        ids = kept
    # TICKET-GATED BANNERS: never list one the player has no way to pull.
    # See _ticket_gated_banners' own docstring for the live-reported bug
    # this fixes (a banner stuck visible with an empty ticket icon and a
    # pull that could only ever refuse).
    gated = _ticket_gated_banners()
    ids = [gid for gid in ids
          if gid not in gated or shop._item_count(full_state, gated[gid]) > 0]
    # ONLY-ONCE BANNERS (master gacha_data.only_once_flag -- the discounted
    # single-pull "first purchase" style offers): once the account has
    # pulled on one at all, drop it from the list for good, same as the
    # real game never re-offers a one-time deal after it's used. handle_exec
    # backs this with its own refusal below so a client that already cached
    # the listing can't replay the pull.
    once_ids = _once_only_banners()
    ids = [gid for gid in ids if gid not in once_ids or not (pity.get(str(gid)) or 0)]
    converted = full_state.get(CONVERTED_KEY) or {}
    # prize_selected_* used to be hardcoded 0 because nothing set them; they
    # are now whatever gacha/select_prize stored for that banner.
    picks = full_state.get(PRIZE_PICK_KEY) or {}
    info = [{"id": gid, "is_daily_draw_end": 0,
             "prize_selected_card_type": (picks.get(str(gid)) or {}).get("card_type", 0),
             "prize_selected_card_id": (picks.get(str(gid)) or {}).get("card_id", 0),
             "is_campaign_draw_enable_single": 0,
             "is_campaign_draw_enable_multi": 0, "remain_stock_num_single": 0,
             "remain_stock_num_multi": 0, "web_text": ""} for gid in ids]
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"gacha_info_list": info,
                     "limit_item_info": {
                         "expired_limit_item_list": [],
                         "limit_item_list": [{"gacha_id": int(g), "num": n,
                                              "converted_item_num": converted.get(str(g), 0)}
                                             for g, n in pity.items()],
                         "reward_summary_info": None}}}


def handle_limit_exchange(payload: dict) -> dict:
    """gacha/limit_exchange -- redeem exchange points (1 per draw on that
    banner) for a chosen card. Previously unhandled (no-op success), so the
    exchange button did nothing.

    Wire shape from the Il2Cpp dump (no real capture exists):
    GachaLimitExchangeRequest = {gacha_id, card_type, card_id, current_num}
    (current_num = the client's own point count, advisory). Response data =
    {exchange_result: ONE GachaResultData (the same 10-field object _grant
    already emits), reward_summary_info, limit_item_info: {gacha_id, num,
    converted_item_num}}. Master table gacha_exchange (gacha_id, card_id,
    card_type, pay_item_num, disp_order) lists the legal picks -- all 258
    rows game-wide cost 200. Points: `num` stays LIFETIME draws;
    converted_item_num grows by the cost (client shows num - converted)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    gacha_id = payload.get("gacha_id") or 0
    banner = _banner(gacha_id)
    pity = full_state.setdefault(PITY_KEY, {})
    converted = full_state.setdefault(CONVERTED_KEY, {})
    have = (pity.get(str(gacha_id)) or 0) - (converted.get(str(gacha_id)) or 0)

    # The legal exchange row for this pick (real master data). A config-only
    # custom banner has no rows -- fall back to the requested/pickup card at
    # the flat 200 the whole real table uses.
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
    ex_row = dict(ex_row) if ex_row is not None else None  # sqlite3.Row has no .get
    cost = int((ex_row or {}).get("pay_item_num") if ex_row else
               (_banner_config().get(str(gacha_id)) or {}).get("exchange_cost", _EXCHANGE_COST))
    if not banner or have < cost:
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    pool = banner["pool"]
    pick_id = (ex_row or {}).get("card_id") or wanted
    card = next((c for c in pool if c["card_id"] == pick_id), None)
    if card is None and pick_id:
        # Legal per master but absent from a config-overridden pool -- grant it
        # anyway with the master row's own card_type/rarity.
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
    # Same live-apply bug as handle_exec's add_support (see its comment):
    # an exchange onto an ALREADY-OWNED support card is a legal real move
    # (raises its limit break same as a gacha dupe) and must not be
    # silently dropped just because new_flag is 0.
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
                    "add_cloth_list": [{"cloth_id": c["card_id"]} for c in add_cards],
                    "add_music_list": [], "add_story_id_array": [], "add_fcoin": 0,
                    "add_present_num": 0, "add_total_fan": 0,
                    "new_chara_profile_array": [], "force_update_honor_id": 0},
                "limit_item_info": {"gacha_id": gacha_id,
                                    "num": pity.get(str(gacha_id)) or 0,
                                    "converted_item_num": converted[str(gacha_id)]}}}


def handle_exec(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    gacha_id = payload.get("gacha_id") or 30110
    draw_num = int(payload.get("draw_num") or 1)
    banner = _banner(gacha_id)
    if not banner or banner.get("odds_on") is False:
        # odds_on False = explicitly disabled in gacha_odds.json. (Date-hidden
        # banners stay pullable on purpose -- only an explicit "on": false
        # refuses; hiding is an index-listing concern.)
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    pity = full_state.get(PITY_KEY) or {}
    if gacha_id in _once_only_banners() and (pity.get(str(gacha_id)) or 0):
        # Backs handle_index's listing filter: a client that already had the
        # banner cached before its one pull retired it can't replay the pull.
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    coin_info = None
    spent_items = []
    if not payload.get("item_id"):                     # carats (not a ticket/free draw)
        cost = banner["multi_cost"] if draw_num >= 10 else banner["single_cost"] * draw_num
        coin_info = _spend_carats(full_state, cost)
        if coin_info is None:
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    else:
        # TICKET draw (item_id e.g. 111 = single-draw tickets, one per draw).
        # These were never deducted -- the else-branch simply didn't exist, and
        # item_list always went out empty, so ticket counts never moved either
        # server- or client-side (live-reported: 'spending single pulls doesnt
        # actually spend them' -- raw dumps show item_id 111, draw_num 10,
        # current_num frozen at 10002 across six consecutive ten-draws).
        item_id = int(payload["item_id"])
        items = full_state.setdefault("item_list_state", [])
        it = next((i for i in items if i.get("item_id") == item_id), None)
        if it is None or (it.get("number") or 0) < draw_num:
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
        it["number"] = (it.get("number") or 0) - draw_num
        # item_list carries the ticket's POST-SPEND count. BUG FIXED
        # 2026-08-24 (live-reported: "my ticket items cosmetically displays
        # 0 even if I have more than 0... but restart it works fine"): this
        # used to emit {"item_id", "number"} -- the real wire struct (every
        # other item_list producer in this codebase, and a real capture,
        # captures/20260811_141604/0011_gacha_exec.json, item_id 114 draw
        # confirm it) is {"item_type", "item_id", "item_num"}. The typed
        # client never found "item_num" in that map, applied 0 for it, and
        # the WRONG-looking ticket count/greyed-out texture that followed
        # was purely a client-side live-apply artifact -- the server's own
        # item_list_state count was always correct, hence a full reload
        # (restart) showing the right number all along.
        cat_row = master_data.query_one(
            "SELECT item_category FROM item_data WHERE id=?", (item_id,))
        item_type = (cat_row["item_category"] if cat_row else None) or 40
        spent_items = [{"item_type": item_type, "item_id": item_id,
                        "item_num": it["number"]}]

    rates = banner.get("rarity_rates")
    drawn = [_draw_one(banner["pool"], rarity_rates=rates) for _ in range(draw_num)]
    if draw_num >= 10:
        # The LAST slot of a ten-pull is ALWAYS rolled from the 2-star-or-
        # better sub-pool (master: draw_guarantee_rarity/num) -- not merely a
        # fallback when the other nine all missed. Treating it as a fallback
        # yields strictly fewer SR+ than the real banner.
        need = banner["guarantee_rarity"]
        for i in range(banner["guarantee_num"] or 1):
            drawn[-(i + 1)] = _draw_one(banner["pool"], min_rarity=need,
                                        rarity_rates=rates)

    new_charas: list = []
    results = [_grant(full_state, banner, c, new_charas) for c in drawn]
    pity = full_state.setdefault(PITY_KEY, {})
    pity[str(gacha_id)] = (pity.get(str(gacha_id)) or 0) + draw_num
    _record_history(full_state, gacha_id, banner, draw_num, payload, coin_info,
                    spent_items, results)
    state_store.save_state(viewer_id, full_state)

    # Real rarity/talent_level/create_time, looked up fresh from the
    # card_collection entry _grant() just wrote -- NOT a hardcoded guess.
    # BUG FIXED 2026-08-20 (live-reported: gacha pull "sometimes softlocks
    # when I click pull"): this used to hardcode {"rarity": 3, "talent_level":
    # 1} for every new card regardless of the card actually drawn, and
    # omitted create_time entirely. A real capture (captures/20260811_141604/
    # 0016_gacha_exec.json) shows add_card_list entries carry the DRAWN
    # card's real rarity (1 or 2 there, not 3) plus create_time -- most new
    # cards are R-rarity (79% base odds), so this mismatched the client's own
    # master-data expectations for the common case, not an edge case, which
    # is why it only "sometimes" happened: only when a pull's new card(s)
    # actually needed rendering/talent lookups against their true rarity.
    cards_now = full_state.get(collection.CARD_LIST_KEY) or []
    add_cards = []
    for r in results:
        if r["card_type"] != 1 or not r["new_flag"]:
            continue
        entry = next((c for c in cards_now if c.get("card_id") == r["card_id"]), None)
        # Real capture shape is exactly these 4 fields (no skill_data_array) --
        # trim rather than deepcopy the whole collection entry.
        add_cards.append({
            "card_id": r["card_id"],
            "rarity": (entry or {}).get("rarity", 1),
            "talent_level": (entry or {}).get("talent_level", 1),
            "create_time": (entry or {}).get("create_time")
                or datetime.now().strftime(_TIME_FORMAT),
        })
    # EVERY drawn support card, dupes included -- NOT just new_flag ones.
    # user-reported 2026-08-19: "whenever I got a dupe instead of being
    # shown another copy and being able to limit break it nothing
    # happened". _grant() itself was already updating limit_break_count/
    # stock correctly in state (verified directly) -- the bug was here:
    # filtering this list to new_flag meant the client's live-apply signal
    # never mentioned a dupe at all, so the change was real server-side but
    # invisible until some unrelated full reload. Confirmed against a real
    # capture with actual dupes (captures/20260816_140811/0030_gacha_exec.
    # json): reward_summary_info.add_support_card_list/add_support_card_
    # num_array there list ALL 10 drawn cards, dupes included, each entry
    # sourced from that card's CURRENT post-pull state (real limit_break_
    # count/stock/exp), not hardcoded zeros -- so look each one up fresh
    # from the collection this same call just updated, rather than
    # fabricating a blank new-card shape for cards that already existed.
    # Merged by support_card_id, summed into add_support_card_num_array's
    # own `number` -- NOT one entry per draw. BUG FOUND live 2026-08-25 (a
    # real 10-pull on this server drew support_card_id 20049 twice in the
    # same batch, debug_responses/0004_RAW_gacha_exec.json): this used to
    # append a SEPARATE add_support_card_list/add_support_card_num_array
    # entry per draw even when the same card repeated within one response --
    # the exact "naive client-side Dictionary.Add() per entry throws on
    # duplicate keys" bug already fixed for add_piece_list just below (see
    # its own comment); it was never carried over to support cards. The
    # softlock this caused (NullReferenceException in WorkDataUtil.
    # SetRewardSummaryInfo -> ArgumentOutOfRangeException loop in
    # GachaMainViewController.UpdateView, from Player.log) only ever hit
    # "sometimes" because a within-batch dupe needs an already-mostly-owned
    # pool to be likely.
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
    # Merged by piece_id, summed -- NOT one entry per draw. BUG FIXED
    # 2026-08-20 (live-reported: gacha pull softlocks the client): a ten-pull
    # commonly draws the same card (-> same piece_id) more than once, and
    # this used to emit a SEPARATE add_piece_list entry per draw even when
    # the piece_id repeated within the same response. A real capture
    # (captures/20260811_141604/0016_gacha_exec.json) confirms the real
    # server merges these into one entry per unique piece_id with the
    # SUMMED piece_num (e.g. three draws of piece 105201 -> one entry,
    # piece_num 15) -- exactly the same "merge, don't repeat" shape
    # add_item_list already used for goddess statues just below. Duplicate
    # keys in a single live-apply batch are exactly the kind of thing a
    # naive client-side Dictionary.Add() per entry throws on.
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
                    "add_cloth_list": [{"cloth_id": c["card_id"]} for c in add_cards],
                    "add_music_list": [], "add_story_id_array": [], "add_fcoin": 0,
                    "add_present_num": 0, "add_total_fan": 0,
                    "new_chara_profile_array": [], "force_update_honor_id": 0},
                "item_list": spent_items, "coin_info": copy.deepcopy(coin_info),
                "limit_item_info": {"gacha_id": gacha_id,
                                    "num": pity[str(gacha_id)], "converted_item_num": 0}}}


# ---------------------------------------------------------------------------
# Pull history (gacha/get_history, get_prize_history) and the prize pick
# (gacha/select_prize).
#
# Shapes off dump.cs; none captured. The history could not be reconstructed
# after the fact -- a pull's cost, its draw_type and what came out of it are
# only knowable at the moment it happens -- so handle_exec records it inline
# above rather than this module inventing a plausible past.

HISTORY_KEY = "gacha_history"
PRIZE_PICK_KEY = "gacha_prize_pick"

# How many pulls the history keeps. The real client shows a bounded list and no
# master row sets the bound, so this is a house number chosen to keep the state
# blob from growing without limit on a long-lived account.
HISTORY_LIMIT = 100


def _record_history(full_state, gacha_id, banner, draw_num, payload, coin_info,
                    spent_items, results) -> None:
    """One GachaExecHistory row plus its GachaRewardHistory rows, newest first.

    The reward rows are the SAME dicts handle_exec already built for the
    response -- _grant returns exactly GachaRewardHistory's field set
    (card_type, card_id, piece_id, common_item_category, common_item_id,
    convert_piece_num, convert_common_item_num, additional_piece_num) -- so the
    history reports what the player actually got rather than a re-derivation
    that could disagree with it.
    """
    hist = full_state.setdefault(HISTORY_KEY, {"next_id": 1, "execs": [], "rewards": {}})
    hist.setdefault("next_id", 1)
    hist.setdefault("execs", [])
    hist.setdefault("rewards", {})
    exec_id = int(hist["next_id"])
    hist["next_id"] = exec_id + 1

    if payload.get("item_id"):
        cost_id, cost_count = int(payload["item_id"]), draw_num
    elif coin_info is not None:
        cost_id = 0          # carats are not an item_id
        cost_count = (banner["multi_cost"] if draw_num >= 10
                      else banner["single_cost"] * draw_num)
    else:
        cost_id, cost_count = 0, 0        # free / campaign draw

    hist["execs"].insert(0, {
        "exec_history_id": exec_id,
        "gacha_id": int(gacha_id),
        # gacha_card_type: 1 = trainable cards, 2 = support cards. Taken from
        # what the banner actually pays out rather than assumed -- a support
        # banner and a chara banner are different rows here.
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
    """-> {gacha_exec_history_array, gacha_reward_history_array}.

    Both arrays flat and newest-first; the reward rows carry the
    exec_history_id that joins them back to their pull. An account that has
    never pulled gets two empty arrays, never null."""
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
    """{gacha_id} -> {prize_history_array:[{result_info_array, exec_time}]}.

    The PRIZE history is not the pull history: it lists the guaranteed-prize
    payouts a banner has handed out, and nothing on this server awards one --
    _draw_one rolls from the banner pool and `win_prize` is 0 on every result
    it has ever produced. So the honest answer is an empty array.

    Reported empty rather than refused: the banner exists and the screen should
    open showing no prizes yet, which is the truth."""
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"prize_history_array": []}}


@registry.endpoint("gacha/select_prize")
def handle_select_prize(payload: dict) -> dict:
    """{gacha_id, card_type, card_id} -> {}. Choosing which card a banner's
    guaranteed prize will be.

    This is a PICK, not a grant -- it sets the card the prize would pay out,
    which gacha/index then reports back as prize_selected_card_type /
    prize_selected_card_id (those two fields already exist there and have always
    gone out as 0, because nothing ever set them). Stored per banner.

    Refuses an unknown banner rather than storing a pick against a banner the
    account can never pull on."""
    viewer_id = payload["viewer_id"]
    gacha_id = payload.get("gacha_id")
    if not _banner(gacha_id):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    full_state = state_store.get_state(viewer_id) or {}
    picks = full_state.setdefault(PRIZE_PICK_KEY, {})
    picks[str(int(gacha_id))] = {"card_type": int(payload.get("card_type") or 0),
                                 "card_id": int(payload.get("card_id") or 0)}
    state_store.save_state(viewer_id, full_state)
    log.info("gacha/select_prize: banner %s prize set to card_type=%s card_id=%s",
             gacha_id, payload.get("card_type"), payload.get("card_id"))
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}
