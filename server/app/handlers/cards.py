"""Trainee-card and support-card upgrades -- the "make my cards better"
endpoints, self-registered (registry.autoload imports this module).

  card/rarity_upgrade      -> star-up a trainee card with PIECES
  card/talent_strengthen   -> raise talent (awakening) level with items + money
  card/skill_upgrade       -> level up a skill hint on a trainee card
  card/sell_piece          -> convert unneeded trainee-card pieces into Clovers
  support_card/limit_break -> raise a support card's uncap 0..4
  support_card/strengthen  -> level a support card with Support Pt + money
  support_card/sell        -> convert spare dupes into Cleats (R/SR/SSR)

Costs come live from master.mdb:
  need_piece_num_data     : pieces per star, keyed by the card's DEFAULT rarity
                            (1/2/3 -> 25/50/150 per step; the table has no rows
                            past rarity 3, so the target rarity can't be the key)
  card_talent_upgrade     : talent materials, keyed talent_group_id+talent_level
                            (levels 2..5, six item slots). NO money column --
                            the money cost lives nowhere in master; the ladder
                            below is pinned by the capture (lvl 4 = 150,000).
  support_card_limit_break: uncap ITEM per rarity (2 -> 145, 3 -> 144). The
                            capture shows dupes (stock) are the other route:
                            request carries material_support_card_num.
  support_card_limit      : level cap per rarity+uncap (30/35/40/45/50 for SSR)
  support_card_level      : exp curve (rarity, level, total_exp)

Currencies: "money" (Monies) is ITEM 59 in item_list -- NOT coin_info, which is
carats (verified: the strengthen capture returns item 59 = old money - use_money).
Support Pt (global exp) is item 110. All balances are validated against OUR
state, never the client's claimed current_* values, and every change persists
into the collection containers load/index serves (see collection.py), so the
client's next refetch sees them without a restart.

Capture ground truth: UmaDumpy 20260717_180912 ops 0016-0019 (label-lag: the
request NAMED op N belongs to op N+1; e.g. the limit_break request is the data
in the file named circle_item_request_receive).
"""

from __future__ import annotations

import copy

from .. import event_engine
from .. import gametora
from .. import master_data
from .. import state as state_store
from . import collection, note_archive, registry, shop
from .stories import _grant_reward

_MONEY_ITEM_ID = 59        # Monies (item_category 91)
_GLOBAL_EXP_ITEM_ID = 110  # Support Pt (item_category 30)
# Cleats (item_data id, item_category 160 -- "amulet in the shape of a cleat,
# exchanged at the shop for various items", text_data cat 23 #48-50): the
# real currency support_card/sell converts unwanted cards into. Keyed by the
# card's rarity (1=R/2=SR/3=SSR, support_card_data.rarity).
_CLEAT_ITEM_BY_RARITY = {1: 48, 2: 49, 3: 50}   # Silver / Gold / Rainbow Cleat
_CLEAT_ITEM_CATEGORY = 160
_CLEATS_PER_CARD = 10  # user-supplied 2026-08-24: 10 cleats of its own rarity, per copy sold
# Clover (item 57, item_category 99, text_data cat 23/24 #57 -- "a lucky
# four-leaf clover... exchanged at the shop for various items"): what "Star
# Piece Storage" converts unneeded trainee-card pieces into (text_data cat 69
# #8040: "You can trade in unneeded Star Pieces for Clovers"). No master.mdb
# rate table and no real capture exist for this exchange -- user-supplied
# 2026-08-24 rate: 1 piece = 1 Clover, any card's piece, no rarity weighting.
_CLOVER_ITEM_ID = 57
_CLOVER_ITEM_CATEGORY = 99
_CLOVERS_PER_PIECE = 1
_MAX_LIMIT_BREAK = 4
_MAX_DECK_NAME_LEN = 30   # server-defined; nothing in master or any capture
                          # pins a deck-name cap.

# Talent (awakening) money cost per NEW level. master.mdb card_talent_upgrade
# has no money column; level 4 = 150,000 is pinned by the capture (money item
# 59 dropped exactly 150,000), the rest is the known awakening ladder.
_TALENT_MONEY = {2: 10000, 3: 50000, 4: 150000, 5: 500000}


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


@registry.endpoint("card/get_release_card_array")
def handle_get_release_card_array(payload: dict) -> dict:
    """card/get_release_card_array -- the gacha/scout screen's "can this card
    be piece-exchanged" list. Had NO handler at all (not in HANDLERS, not
    registered) so it fell through to main.py's NOOP_SUCCESS, whose empty
    `data: {}` leaves data.release_card_array completely absent. Live-
    reported 2026-08-25 (softlock right after tapping Scout): Player.log
    showed ArgumentNullException in List<T>..ctor, from Gallop.WorkCardData.
    UpdateReleaseCardWithHavingPiece -- the client builds a List<int> straight
    from this field with no null check, and a MISSING key deserializes to a
    null array, not an empty one. Real capture (captures/20260818_081331/
    0017_card_get_release_card_array.json) confirms both the field name (note
    the trailing _array the endpoint path also carries -- easy to mistarget)
    and the shape (a flat int[] of card_ids); note_archive._release_card_
    array() already reconstructs this exact list from master.mdb for the
    Uma Archive screen, so reuse it here rather than duplicating the query."""
    return _ok({"release_card_array": note_archive._release_card_array()})


def _find(entries: list, key: str, value) -> dict | None:
    return next((e for e in entries if isinstance(e, dict) and e.get(key) == value), None)


def _card_entry(full_state: dict, card_id: int) -> dict | None:
    """The owned trainee-card entry ({card_id, rarity, talent_level,
    create_time, skill_data_array} -- exactly the card_data the client expects
    back)."""
    return _find(full_state.setdefault(collection.CARD_LIST_KEY, []), "card_id", card_id)


def _support_entry(full_state: dict, support_card_id: int) -> dict | None:
    return _find(full_state.setdefault(collection.SUPPORT_CARD_KEY, []),
                 "support_card_id", support_card_id)


# ---------------------------------------------------------------- trainee card

@registry.endpoint("card/rarity_upgrade")
def handle_rarity_upgrade(payload: dict) -> dict:
    """card/rarity_upgrade: {card_id, now_rarity, new_rarity} -- star-up with
    pieces. Cost per step is need_piece_num_data keyed by the card's
    default_rarity; the piece id is card_data.get_piece_id. Response (capture):
    card_data + piece_data (remaining pieces) + reward_result_info (empty for
    the captured 3->4; newly unlocked dresses from card_rarity_data land in
    add_cloth_list)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    now_rarity = payload.get("now_rarity")
    new_rarity = payload.get("new_rarity")

    entry = _card_entry(full_state, card_id)
    master = master_data.query_one(
        "SELECT default_rarity, get_piece_id FROM card_data WHERE id=?", (card_id,))
    if entry is None or master is None:
        return _refuse()
    if entry.get("rarity") != now_rarity or not new_rarity or new_rarity <= now_rarity:
        return _refuse()  # server state is the authority on the current rarity
    # target star must actually exist for this card
    if not master_data.query_one(
            "SELECT id FROM card_rarity_data WHERE card_id=? AND rarity=?",
            (card_id, new_rarity)):
        return _refuse()

    need = master_data.query_one(
        "SELECT piece_num FROM need_piece_num_data WHERE rarity=?",
        (master["default_rarity"],))
    if need is None:
        return _refuse()
    cost = need["piece_num"] * (new_rarity - now_rarity)

    piece_id = master["get_piece_id"] or card_id
    pieces = full_state.setdefault("piece_list_state", [])
    piece = _find(pieces, "piece_id", piece_id)
    if piece is None or (piece.get("piece_num") or 0) < cost:
        return _refuse()
    piece["piece_num"] -= cost
    entry["rarity"] = new_rarity

    # dresses unlocked by the newly reached star(s) -- 3* grants the alternate
    # outfit (get_dress_id_2); the captured 3->4 granted nothing (all empty).
    summary = shop._empty_summary()
    cloth = full_state.setdefault("cloth_list_state", [])
    for r in range(now_rarity + 1, new_rarity + 1):
        row = master_data.query_one(
            "SELECT get_dress_id_1, get_dress_id_2 FROM card_rarity_data "
            "WHERE card_id=? AND rarity=?", (card_id, r))
        for key in ("get_dress_id_1", "get_dress_id_2"):
            dress = row[key] if row else 0
            if dress and not _find(cloth, "cloth_id", dress):
                cloth.append({"cloth_id": dress})
                summary["add_cloth_list"].append({"cloth_id": dress})

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "card_data": copy.deepcopy(entry),
        "piece_data": [{"piece_id": piece_id, "piece_num": piece["piece_num"]}],
        "reward_result_info": summary,
    })


@registry.endpoint("card/talent_strengthen")
def handle_talent_strengthen(payload: dict) -> dict:
    """card/talent_strengthen: {card_id, talent_level, new_talent_level} --
    awakening. Materials are card_talent_upgrade rows (talent_group_id, one row
    per NEW level 2..5, six item slots) plus the money ladder. Response
    (capture): card_data + item_data_array with the new counts of everything
    consumed, money (item 59) last."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    now_level = payload.get("talent_level")
    new_level = payload.get("new_talent_level")

    entry = _card_entry(full_state, card_id)
    master = master_data.query_one(
        "SELECT talent_group_id FROM card_data WHERE id=?", (card_id,))
    if entry is None or master is None:
        return _refuse()
    if entry.get("talent_level") != now_level or not new_level \
            or new_level <= now_level or new_level > 5:
        return _refuse()

    # tally the full bill (one master row per new level), then pay atomically
    costs: dict[int, int] = {}   # item_id -> total needed (insertion = display order)
    money = 0
    for level in range(now_level + 1, new_level + 1):
        row = master_data.query_one(
            "SELECT * FROM card_talent_upgrade WHERE talent_group_id=? AND talent_level=?",
            (master["talent_group_id"], level))
        if row is None:
            return _refuse()
        for i in range(1, 7):
            if row[f"item_category_{i}"]:
                iid = row[f"item_id_{i}"]
                costs[iid] = costs.get(iid, 0) + row[f"item_num_{i}"]
        money += _TALENT_MONEY.get(level, 0)
    costs[_MONEY_ITEM_ID] = costs.get(_MONEY_ITEM_ID, 0) + money

    if any(shop._item_count(full_state, iid) < num for iid, num in costs.items()):
        return _refuse()
    item_data_array = []
    for iid, num in costs.items():
        item_data_array.append({"item_id": iid,
                                "number": shop._add_item(full_state, iid, -num)})
    entry["talent_level"] = new_level

    state_store.save_state(viewer_id, full_state)
    return _ok({"card_data": copy.deepcopy(entry),
                "item_data_array": item_data_array})


_max_hint_rarity_cache: int | None = None


def _hint_upgrade_row(default_rarity: int, level: int):
    """card_talent_hint_upgrade row for (rarity, level), falling back to the
    highest rarity this table actually has data for -- see
    handle_skill_upgrade's docstring DATA GAP note."""
    row = master_data.query_one(
        "SELECT * FROM card_talent_hint_upgrade WHERE rarity=? AND talent_level=?",
        (default_rarity, level))
    if row is not None:
        return row
    global _max_hint_rarity_cache
    if _max_hint_rarity_cache is None:
        r = master_data.query_one("SELECT MAX(rarity) AS r FROM card_talent_hint_upgrade")
        _max_hint_rarity_cache = (r["r"] if r and r["r"] is not None else 0)
    if default_rarity <= _max_hint_rarity_cache:
        return None      # a genuinely-missing LOWER tier -- stay locked, don't guess
    return master_data.query_one(
        "SELECT * FROM card_talent_hint_upgrade WHERE rarity=? AND talent_level=?",
        (_max_hint_rarity_cache, level))


@registry.endpoint("card/skill_upgrade")
def handle_skill_upgrade(payload: dict) -> dict:
    """card/skill_upgrade: {card_id, skill_id, skill_level, new_skill_level} --
    level up a skill hint on an owned trainee card. NEVER CAPTURED before this
    session; ground truth is
    captures/20260817_093008/0009_card_skill_upgrade.json (real server, card
    106001 default_rarity 1, skill_level 0 -> 1). Costs come from
    card_talent_hint_upgrade -- despite the name (shared schema/column names
    with card_talent_upgrade, a DIFFERENT table for the DIFFERENT
    talent_strengthen feature above), its (item_id_1, item_id_2, money_num)
    for rarity=1/level=1 is EXACTLY the item id set the capture's post-
    purchase item_data_array shows (160, 44, money item 59) -- the capture
    only has the post-purchase balance, not the pre-purchase one, so the
    quantities (4x160, 5x44, 10000 money) are corroborated by column match,
    not independently reproduced. Keyed by the card's default_rarity, same
    convention as need_piece_num_data/card_talent_upgrade. Multi-level jumps
    tally every intermediate row, mirroring handle_talent_strengthen. Response
    (capture): card_data + item_data_array (remaining balances, insertion
    order pay1/pay2/money like talent_strengthen).

    DATA GAP: card_talent_hint_upgrade only has rows for default_rarity 1
    and 2 (3 levels each) in this server's master.mdb -- but 80 of its 99
    cards are default_rarity 3 (the common release tier), which has NO row
    at all. That's not a code bug, the master data for this table is just
    sparse. Rather than refuse the feature outright for the vast majority of
    cards, an uncovered rarity falls back to the HIGHEST rarity tier we do
    have real numbers for (rarity 2) -- likely an UNDERCHARGE relative to
    the real cost (real per-rarity costs escalate, per the captured
    rarity1->rarity2 jump: item_num_1 x2, item_num_2 x4, money x10), but
    grounded in genuine captured values rather than extrapolating an
    unverified curve from two data points into a guessed one. This is
    server-defined, NOT capture-verified, for any card above rarity 2."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    card_id = payload.get("card_id")
    skill_id = payload.get("skill_id")
    now_level = payload.get("skill_level")
    new_level = payload.get("new_skill_level")

    entry = _card_entry(full_state, card_id)
    master = master_data.query_one(
        "SELECT default_rarity FROM card_data WHERE id=?", (card_id,))
    if entry is None or master is None or not isinstance(skill_id, int):
        return _refuse()
    if not isinstance(now_level, int) or not isinstance(new_level, int) \
            or new_level <= now_level:
        return _refuse()

    skills = entry.setdefault("skill_data_array", [])
    current = next((s for s in skills if s.get("skill_id") == skill_id), None)
    if (current.get("level") if current else 0) != now_level:
        return _refuse()   # server state is the authority on the current level

    costs: dict[int, int] = {}       # item_id -> total needed (insertion = display order)
    money = 0
    for level in range(now_level + 1, new_level + 1):
        row = _hint_upgrade_row(master["default_rarity"], level)
        if row is None:
            return _refuse()
        costs[row["item_id_1"]] = costs.get(row["item_id_1"], 0) + row["item_num_1"]
        costs[row["item_id_2"]] = costs.get(row["item_id_2"], 0) + row["item_num_2"]
        money += row["money_num"] or 0
    costs[_MONEY_ITEM_ID] = costs.get(_MONEY_ITEM_ID, 0) + money

    if any(shop._item_count(full_state, iid) < num for iid, num in costs.items()):
        return _refuse()
    item_data_array = []
    for iid, num in costs.items():
        item_data_array.append({"item_id": iid,
                                "number": shop._add_item(full_state, iid, -num)})

    if current:
        current["level"] = new_level
    else:
        skills.append({"skill_id": skill_id, "level": new_level})

    state_store.save_state(viewer_id, full_state)
    return _ok({"card_data": copy.deepcopy(entry), "item_data_array": item_data_array})


# ---------------------------------------------------------------- support card

_EVENT_TITLE_CATEGORY = 181
_SUPPORT_STORY_BASE = 800000000


def _event_story_ids(support_card_id: int) -> dict:
    """{normalized event title -> story_id} for every event this card shows.

    story_id = 800000000 + owner*1000 + index, and a card's events are split
    across TWO owners: its CHARA id and its own CARD id. Capture-proven (card
    20008 -> 801025002 under chara 1025 AND 820008002 under card 20008).
    Looking at only one range is what made this table seem almost empty."""
    row = master_data.query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (support_card_id,))
    owners = [support_card_id]
    if row and row["chara_id"]:
        owners.append(row["chara_id"])
    out = {}
    for owner in owners:
        lo = _SUPPORT_STORY_BASE + owner * 1000
        for r in master_data.query(
                "SELECT [index], text FROM text_data WHERE category=? "
                "AND [index]>=? AND [index]<? ORDER BY [index]",
                (_EVENT_TITLE_CATEGORY, lo, lo + 1000)):
            key = event_engine.title_key(r["text"])
            if key:
                out.setdefault(key, r["index"])
    return out


@registry.endpoint("support_card/get_support_card_event_skill")
def handle_get_support_card_event_skill(payload: dict) -> dict:
    """support_card/get_support_card_event_skill -- the card detail screen's
    career-events tab (live-reported: every event showed "no outcome").

    Wire shape from the Il2Cpp dump, confirmed against a real capture:
      Request  { int support_card_id }
      Response { EventSkill[] event_skill_list }, EventSkill { story_id, skill_id_array }

    The tab only wants the SKILLS each event can teach, keyed by story_id. The
    skill_hint effects are already vendored in data/events/gametora; the
    story_id keying comes from master.mdb (see _event_story_ids).

    Validated against both real captures: card 20008 -> [{801025002,[200142]},
    {820008002,[201432]}], card 20011 -> [{801029002,[200342]}]."""
    support_card_id = payload.get("support_card_id") or 0
    events = gametora.load_cached("support", support_card_id) or {}
    story_ids = _event_story_ids(support_card_id)
    out = []
    for event in events.values():
        if not isinstance(event, dict):
            continue
        skills = []
        for choice in event.get("choices") or ():
            for effect in choice.get("effects") or ():
                sid = effect.get("skill_id")
                if sid and sid not in skills:
                    skills.append(sid)
        if not skills:
            continue
        story_id = story_ids.get(event_engine.title_key(event.get("name") or ""))
        if story_id is None:
            continue
        out.append({"story_id": story_id, "skill_id_array": skills})
    out.sort(key=lambda e: e["story_id"])
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"event_skill_list": out}}


@registry.endpoint("support_card/limit_break")
def handle_limit_break(payload: dict) -> dict:
    """support_card/limit_break: {support_card_id, material_support_card_num}.
    Two routes: material_support_card_num > 0 burns that many DUPES from stock
    (the capture: an SR with stock 1 sent 1, stock -> 0, uncap -> 1); otherwise
    the uncap item from support_card_limit_break (rarity 2 -> 145, 3 -> 144;
    rarity 1 has no row -- R cards only uncap with dupes). Response (capture):
    just the updated support_card_data."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    support_card_id = payload.get("support_card_id")

    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    lb = entry.get("limit_break_count") or 0
    if lb >= _MAX_LIMIT_BREAK:
        return _refuse()

    materials = int(payload.get("material_support_card_num") or 0)
    if materials > 0:                              # dupe route: +1 uncap each
        if lb + materials > _MAX_LIMIT_BREAK or (entry.get("stock") or 0) < materials:
            return _refuse()
        entry["stock"] = entry["stock"] - materials
        entry["limit_break_count"] = lb + materials
    else:                                          # item route: +1 uncap
        rarity = master_data.query_one(
            "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
        if rarity is None:
            return _refuse()
        row = master_data.query_one(
            "SELECT item_id, item_num FROM support_card_limit_break WHERE rarity=?",
            (rarity["rarity"],))
        if row is None:                            # rarity 1: no item exists
            return _refuse()
        if shop._item_count(full_state, row["item_id"]) < row["item_num"]:
            return _refuse()
        shop._add_item(full_state, row["item_id"], -row["item_num"])
        entry["limit_break_count"] = lb + 1

    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_data": copy.deepcopy(entry)})


def _support_level_cap(support_card_id: int, limit_break_count: int):
    """(cap_level, cap_total_exp) for this card at this uncap, or None."""
    rarity = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
    if rarity is None:
        return None
    limits = master_data.query_one(
        "SELECT * FROM support_card_limit WHERE rarity=?", (rarity["rarity"],))
    if limits is None:
        return None
    cap_level = limits[f"limit_{max(0, min(_MAX_LIMIT_BREAK, limit_break_count))}"]
    cap = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (rarity["rarity"], cap_level))
    return (cap_level, cap["total_exp"]) if cap else None


@registry.endpoint("support_card/strengthen")
def handle_support_strengthen(payload: dict) -> dict:
    """support_card/strengthen: {support_card_id, use_global_exp, use_money,
    current_*}. Pours Support Pt (item 110) into the card's exp, money (item
    59) alongside (the client bills 2 Monies per 1 Pt). The exp may not pass
    the uncap's level cap (support_card_limit x support_card_level); the
    captured spend lands EXACTLY on the SSR-uncap-0 cap of 23,885 = level 30.
    current_* are the client's claimed balances -- ignored, our state decides.
    Response (capture): support_card_data + item_data_array [Pt, money]."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    support_card_id = payload.get("support_card_id")
    use_exp = int(payload.get("use_global_exp") or 0)
    use_money = int(payload.get("use_money") or 0)

    entry = _support_entry(full_state, support_card_id)
    if entry is None or use_exp <= 0 or use_money < 0:
        return _refuse()
    cap = _support_level_cap(support_card_id, entry.get("limit_break_count") or 0)
    if cap is None or (entry.get("exp") or 0) + use_exp > cap[1]:
        return _refuse()
    if shop._item_count(full_state, _GLOBAL_EXP_ITEM_ID) < use_exp \
            or shop._item_count(full_state, _MONEY_ITEM_ID) < use_money:
        return _refuse()

    exp_left = shop._add_item(full_state, _GLOBAL_EXP_ITEM_ID, -use_exp)
    money_left = shop._add_item(full_state, _MONEY_ITEM_ID, -use_money)
    entry["exp"] = (entry.get("exp") or 0) + use_exp

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "support_card_data": copy.deepcopy(entry),
        "item_data_array": [{"item_id": _GLOBAL_EXP_ITEM_ID, "number": exp_left},
                            {"item_id": _MONEY_ITEM_ID, "number": money_left}],
    })


@registry.endpoint("support_card/sell")
def handle_support_card_sell(payload: dict) -> dict:
    """support_card/sell: {sell_support_card_info_array: [{support_card_id,
    stock, client_own_stock}]} -- dump.cs SupportCardSellRequest/
    SellSupportCardInfo. Converts spare DUPLICATE copies of owned support
    cards into Cleats (item 48/49/50 -- Silver/Gold/Rainbow, matching R/SR/
    SSR), the real currency the shop's Cleat Exchange takes. USER-SUPPLIED
    yield (2026-08-24): 10 Cleats of the card's own rarity, per copy sold.

    `stock` here is the request's HOW-MANY-TO-SELL count (dump.cs field name,
    confusingly the same name as the OWNED entry's own stock counter --
    handle_limit_break's spare-dupes field). This only ever spends that same
    stock counter, exactly like limit_break's material_support_card_num route
    already does -- selling can never remove the base owned copy, so a card
    already equipped in an active career's deck (which references the base
    copy, not a specific dupe) is never affected. client_own_stock is the
    client's claimed balance -- accepted but not trusted, same as every other
    current_*/claimed value in this module; our own entry['stock'] decides.

    Every line is validated BEFORE any state changes: a partial sell (some
    cards convert, one silently fails) would short the player on cleats with
    no way to tell which line was skipped.

    Response (dump.cs SupportCardSellResponse.CommonResponse): reward_summary_info
    + reward_info_array only -- no support_card_data, since selling changes
    nothing about the card itself (uncap/exp/level), only its stock, which
    the client re-reads via its next load/index."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    requests = payload.get("sell_support_card_info_array") or []
    if not requests:
        return _refuse()

    plan = []   # (entry, sell_count, cleat_item_id)
    for req in requests:
        support_card_id = req.get("support_card_id")
        sell_count = int(req.get("stock") or 0)
        entry = _support_entry(full_state, support_card_id)
        if entry is None or sell_count <= 0 or (entry.get("stock") or 0) < sell_count:
            return _refuse()
        # The padlock support_card/change_lock sets. Without this check that
        # endpoint would be decorative -- a lock the player sets and the one
        # destructive path ignores.
        if entry.get("is_locked"):
            return _refuse()
        rarity = master_data.query_one(
            "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
        cleat_item_id = _CLEAT_ITEM_BY_RARITY.get(rarity["rarity"]) if rarity else None
        if cleat_item_id is None:
            return _refuse()
        plan.append((entry, sell_count, cleat_item_id))

    summary = shop._empty_summary()
    reward_info_array = []
    for entry, sell_count, cleat_item_id in plan:
        entry["stock"] = (entry.get("stock") or 0) - sell_count
        reward_info_array.extend(_grant_reward(
            full_state, _CLEAT_ITEM_CATEGORY, cleat_item_id,
            sell_count * _CLEATS_PER_CARD, summary, viewer_id))

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "reward_info_array": reward_info_array,
    })


@registry.endpoint("card/sell_piece")
def handle_sell_piece(payload: dict) -> dict:
    """card/sell_piece: {sell_piece_data_array: [{piece_id, piece_num}]} --
    dump.cs CardSellPieceRequest/PieceData. "Star Piece Storage": converts
    unneeded trainee-card pieces into Clovers (item 57), the real currency
    the shop exchanges for other items (text_data cat 69 #8040: "You can
    trade in unneeded Star Pieces for Clovers"). USER-SUPPLIED rate
    (2026-08-24, no master.mdb table or real capture exists for this
    exchange): 1 Clover per piece, any card's piece, no rarity weighting.

    Every line is validated against our own piece_list_state BEFORE any
    state changes -- same partial-sell protection as support_card/sell.

    Response (dump.cs CardSellPieceResponse.CommonResponse): reward_summary_info
    + piece_data (the REMAINING balance of every piece sold, unlike
    support_card/sell's reward_info_array-only shape -- this endpoint's real
    response explicitly echoes piece_data back)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    requests = payload.get("sell_piece_data_array") or []
    if not requests:
        return _refuse()

    pieces = full_state.setdefault("piece_list_state", [])
    plan = []   # (piece_entry, sell_count)
    for req in requests:
        piece_id = req.get("piece_id")
        sell_count = int(req.get("piece_num") or 0)
        piece = _find(pieces, "piece_id", piece_id)
        if piece is None or sell_count <= 0 or (piece.get("piece_num") or 0) < sell_count:
            return _refuse()
        plan.append((piece, sell_count))

    summary = shop._empty_summary()
    piece_data = []
    total_clovers = 0
    for piece, sell_count in plan:
        piece["piece_num"] -= sell_count
        total_clovers += sell_count * _CLOVERS_PER_PIECE
        piece_data.append({"piece_id": piece["piece_id"], "piece_num": piece["piece_num"]})
    _grant_reward(full_state, _CLOVER_ITEM_CATEGORY, _CLOVER_ITEM_ID,
                  total_clovers, summary, viewer_id)

    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary, "piece_data": piece_data})


# --------------------------------------------------------------------------
# Card-screen extras. dump.cs shapes (never captured on this project, so the
# request fields are exact and the responses carry exactly what is declared):
#   CardUnlockRequest             {card_id}
#     -> {reward_summary_info, piece_data}
#   CardGetCardEventSkillRequest  {card_id}    -> {event_skill_list}
#   SupportCardChangeLockRequest  {support_card_id, lock_flag}
#     -> {support_card_id, is_locked}
#   SupportCardLimitBreakItemRequest {support_card_id, limit_break_item_id,
#                                     limit_break_count}
#     -> {support_card_data, item_data_array}
#   SupportCardDeckChangeNameRequest {deck_id, name} -> {support_card_deck}

_TRAINEE_STORY_BASE = 500000000   # chara-story band; the support band is
                                  # _SUPPORT_STORY_BASE above. Same
                                  # base + owner*1000 + index scheme.


def _chara_event_story_ids(card_id: int) -> dict:
    """{normalized event title -> story_id} for a TRAINEE card's career events.

    The support-card twin (_event_story_ids) has to search two owner bands
    because a support card's events are split between its chara id and its own
    card id. A trainee card's are not: they all live under the CHARA, which is
    also what gametora.py's own dumper assumes for kind="chara" (its `bands`
    helper: 500000000 + (card_id // 100) * 1000). Verified against the cache --
    card 100101 resolves 26 of its 27 dumped events to real story ids."""
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    chara_id = (row["chara_id"] if row and row["chara_id"] else card_id // 100)
    lo = _TRAINEE_STORY_BASE + chara_id * 1000
    out = {}
    for r in master_data.query(
            "SELECT [index], text FROM text_data WHERE category=? "
            "AND [index]>=? AND [index]<? ORDER BY [index]",
            (_EVENT_TITLE_CATEGORY, lo, lo + 1000)):
        key = event_engine.title_key(r["text"])
        if key:
            out.setdefault(key, r["index"])
    return out


@registry.endpoint("card/get_card_event_skill")
def handle_get_card_event_skill(payload: dict) -> dict:
    """card/get_card_event_skill -- the TRAINEE card's career-events tab,
    the exact counterpart of support_card/get_support_card_event_skill above
    (same EventSkill{story_id, skill_id_array} rows, same "which skills can
    this card's events teach" question). Everything but the story-id band is
    shared with that handler; see _chara_event_story_ids for the difference.

    A card with no cached gametora page, or whose events teach no skills,
    serves an EMPTY list rather than refusing: the tab is legitimately empty
    for plenty of cards, and 205 here would break the whole detail screen
    instead of showing "no skills"."""
    card_id = payload.get("card_id") or 0
    events = gametora.load_cached("chara", card_id) or {}
    story_ids = _chara_event_story_ids(card_id)
    out = []
    for event in events.values():
        if not isinstance(event, dict):
            continue
        skills = []
        for choice in event.get("choices") or ():
            for effect in choice.get("effects") or ():
                sid = effect.get("skill_id")
                if sid and sid not in skills:
                    skills.append(sid)
        if not skills:
            continue
        story_id = story_ids.get(event_engine.title_key(event.get("name") or ""))
        if story_id is None:
            continue
        out.append({"story_id": story_id, "skill_id_array": skills})
    out.sort(key=lambda e: e["story_id"])
    return _ok({"event_skill_list": out})


@registry.endpoint("card/unlock")
def handle_card_unlock(payload: dict) -> dict:
    """card/unlock -- spend Star Pieces to obtain a trainee card outright,
    the "Piece Exchange" half of the scout screen that
    card/get_release_card_array above builds the eligibility list for.

    Cost source: need_piece_num_data, keyed by the card's DEFAULT rarity --
    the same table and the same keying handle_rarity_upgrade already uses
    (1/2/3 -> 25/50/150). That this is the unlock price as well as the
    per-star price is SERVER-DEFINED: no capture of this endpoint exists and
    master has no separate unlock-cost table, so the one real piece-cost
    table in the game is used rather than inventing a second number.

    Granting goes through shop._grant with a synthetic exchange row so the
    card arrives exactly the way a shop purchase of the same card does --
    card_collection entry at its minimum rarity, the chara row, the outfit,
    and every add_* list the client needs to show it without a restart. Doing
    it by hand here is how the two paths would drift.

    Refuses on an unknown card, a card outside the piece-exchange list, a card
    already owned, or not enough pieces."""
    viewer_id = payload["viewer_id"]
    card_id = payload.get("card_id")
    if not isinstance(card_id, int) or isinstance(card_id, bool):
        return _refuse()
    master = master_data.query_one(
        "SELECT default_rarity, get_piece_id FROM card_data WHERE id=?", (card_id,))
    if master is None or card_id not in note_archive._release_card_array():
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if _card_entry(full_state, card_id) is not None:
        return _refuse()        # already owned -- rarity_upgrade is that path

    need = master_data.query_one(
        "SELECT piece_num FROM need_piece_num_data WHERE rarity=?",
        (master["default_rarity"],))
    if need is None:
        return _refuse()
    cost = need["piece_num"]

    piece_id = master["get_piece_id"] or card_id
    pieces = full_state.setdefault("piece_list_state", [])
    piece = _find(pieces, "piece_id", piece_id)
    if piece is None or (piece.get("piece_num") or 0) < cost:
        return _refuse()
    piece["piece_num"] -= cost

    summary = shop._empty_summary()
    shop._grant(full_state, {"change_item_category": shop._CARD_CATEGORY,
                             "change_item_id": card_id,
                             "change_item_num": 1,
                             "additional_piece_num": 0}, 1, summary)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "piece_data": [{"piece_id": piece_id, "piece_num": piece["piece_num"]}],
    })


# ------------------------------------------------------------ support cards

@registry.endpoint("support_card/change_lock")
def handle_support_card_change_lock(payload: dict) -> dict:
    """support_card/change_lock -- the padlock that keeps a support card out
    of support_card/sell. Response echoes {support_card_id, is_locked}, the
    two fields the dumped class declares.

    NOTE the field-name shift across the wire: the REQUEST says `lock_flag`
    and the RESPONSE says `is_locked`. They are the same flag; the stored
    entry uses the response spelling so load/index and sell read one name."""
    viewer_id = payload["viewer_id"]
    support_card_id = payload.get("support_card_id")
    lock_flag = payload.get("lock_flag")
    if not isinstance(support_card_id, int) or isinstance(support_card_id, bool):
        return _refuse()
    if not isinstance(lock_flag, int) or isinstance(lock_flag, bool):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    entry["is_locked"] = 1 if lock_flag else 0
    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_id": support_card_id,
                "is_locked": entry["is_locked"]})


@registry.endpoint("support_card/limit_break_item")
def handle_limit_break_item(payload: dict) -> dict:
    """support_card/limit_break_item -- uncap a support card with ITEMS, the
    named-item sibling of support_card/limit_break above.

    limit_break has two routes in one endpoint (burn dupes, or spend the one
    uncap item for that rarity, +1 each call). This one is the item route
    made explicit: the client names WHICH item and HOW MANY uncaps to buy in
    a single call. The item is validated against support_card_limit_break for
    this card's rarity -- naming a different item id is refused rather than
    quietly charged, since that row is the only thing that makes an item an
    uncap item.

    Response carries item_data_array (the remaining balance of what was
    spent) as well as the card, matching the same pay-then-report shape
    handle_talent_strengthen uses."""
    viewer_id = payload["viewer_id"]
    support_card_id = payload.get("support_card_id")
    item_id = payload.get("limit_break_item_id")
    count = payload.get("limit_break_count")
    if not isinstance(support_card_id, int) or isinstance(support_card_id, bool):
        return _refuse()
    if count is None:
        count = 1
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    entry = _support_entry(full_state, support_card_id)
    if entry is None:
        return _refuse()
    lb = entry.get("limit_break_count") or 0
    if lb + count > _MAX_LIMIT_BREAK:
        return _refuse()

    rarity = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (support_card_id,))
    if rarity is None:
        return _refuse()
    row = master_data.query_one(
        "SELECT item_id, item_num FROM support_card_limit_break WHERE rarity=?",
        (rarity["rarity"],))
    if row is None:
        return _refuse()        # rarity 1 has no uncap item -- dupes only
    if item_id is not None and item_id != row["item_id"]:
        return _refuse()        # not THIS card's uncap item

    total = row["item_num"] * count
    if shop._item_count(full_state, row["item_id"]) < total:
        return _refuse()
    remaining = shop._add_item(full_state, row["item_id"], -total)
    entry["limit_break_count"] = lb + count

    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_data": copy.deepcopy(entry),
                "item_data_array": [{"item_id": row["item_id"],
                                     "number": remaining}]})


@registry.endpoint("support_card_deck/change_name")
def handle_deck_change_name(payload: dict) -> dict:
    """support_card_deck/change_name -- rename one support-card deck.

    Response is a SINGLE UserSupportCardDeck, not the whole array that
    support_card_deck/change_party returns -- the dumped field is
    `support_card_deck`, singular. The deck state is seeded through
    collection.get_or_seed first so a rename before the first change_party
    still lands on a real deck rather than materialising a stray one."""
    viewer_id = payload["viewer_id"]
    deck_id = payload.get("deck_id")
    name = payload.get("name")
    if not isinstance(deck_id, int) or isinstance(deck_id, bool):
        return _refuse()
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_DECK_NAME_LEN:
        return _refuse()

    collection.get_or_seed(viewer_id, "support_card_deck_array",
                           collection._deck_seed())
    full_state = state_store.get_state(viewer_id) or {}
    decks = full_state.get(collection.SUPPORT_DECK_KEY) or []
    deck = _find(decks, "deck_id", deck_id)
    if deck is None:
        # Materialize, exactly as change_party does for a deck_id it has not
        # seen. The seed is genuinely EMPTY on this server (the load blob it
        # comes from carries no support_card_deck_array), so deck state only
        # starts existing once the client touches a deck -- and renaming is a
        # legitimate first touch. Refusing here would 205 a rename of a deck
        # the client is already showing.
        deck = {"deck_id": deck_id, "name": "", "support_card_id_array": []}
        decks.append(deck)
    deck["name"] = name.strip()
    full_state[collection.SUPPORT_DECK_KEY] = decks
    state_store.save_state(viewer_id, full_state)
    return _ok({"support_card_deck": copy.deepcopy(deck)})
