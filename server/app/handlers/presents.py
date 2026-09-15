"""Present box (mail) -- the operations-gift inbox.

Endpoints (self-registered; main.py never lists them):
  present/index       -> the viewer's UNCLAIMED presents + the badge counts [cap]
  present/receive_all -> claim everything claimable, apply it to the owned
                         containers, answer with reward_summary_info [cap]
  present/history     -> the CLAIMED presents (the receipt log tab) [req cap]
  present/receive     -> claim ONE present. SYNTHESIZED-UNVERIFIED: never
                         captured; modeled as the single-present variant of
                         receive_all (request assumed {present_id}).

Wire truth (dumps 20260720_145320 tx 60-62, 20260721_132421 tx 1-4,
20260723_134126 tx 11-13 + 37-39; NB the dump shift -- the request stored in
file N belongs to op N+1, so a matched pair is request N-1 + response N):

  present row: {present_id, state (0 = unclaimed), reward_type, reward_id,
    reward_count, message_id, message_param_value_1..4, reward_limit_time,
    receive_time ("0000-00-00 00:00:00" until claimed), create_time,
    free_message}
  reward_limit_time 4133948399 is the "never expires" sentinel and is what
  splits no_receive_present_num into no_time_limit vs time_limit buckets
  (20260721 tx 2: limits {epoch, epoch, sentinel} -> counts {1 permanent,
  2 limited}).

  reward_type -> where the reward lands (proven by diffing the 52-present
  receive_all in 20260723 tx 11/12 against its summary):
    90        -> carats: coin_info_state.fcoin, summed into add_fcoin
    51        -> support card: support_card_collection (+stock if owned),
                 add_support_card_list (new only, incl. possess_time) +
                 add_support_card_num_array (always, item_type 51)
    70        -> outfit: cloth_list_state + add_cloth_list
    80        -> music: music_list_state + add_music_list (acquisition_time)
    50        -> uma card [synthesized by analogy with shop.py's category 50;
                 never captured in mail]
    102       -> card piece [synthesized, same analogy]
    all else  -> plain inventory item, item_id = reward_id (observed types
                 20/21/30/34/40/41/91/93/97/103/150/172 all landed in
                 add_item_list, aggregated per item in first-seen order)

The box is fully server-side state (NO master.mdb table for presents --
verified): everything lives under PRESENT_BOX_KEY in the viewer blob. A viewer
with no state has an empty box; nothing is seeded from the capture player.
Claimed presents stay in the list with state=1 + receive_time as the claim
ledger, so a second receive_all claims zero.

admin.py `send-mail` calls admin_send() below to append a present.
"""

from __future__ import annotations

import copy
import time

from .. import master_data
from .. import state as state_store
from . import collection, registry, shop

PRESENT_BOX_KEY = "present_box_state"  # {"next_present_id": int, "presents": [wire rows]}
NO_LIMIT_TIME = 4133948399    # captured sentinel: present never expires
_FIRST_PRESENT_ID = 900000001  # far above the captured 353xxxxxx range
_CANNED_MESSAGE_ID = 44        # param-less canned text on real ops mail [cap]
_FREE_MESSAGE_ID = 99999       # every free_message mail in the captures uses this [cap]
_TYPE_CARD = 50
_TYPE_SUPPORT = 51
_TYPE_CLOTH = 70
_TYPE_MUSIC = 80
_TYPE_CARAT = 90
_TYPE_PIECE = 102
_INT32_MAX = 2_147_483_647   # dump.cs CoinInfo.fcoin/coin -- see patch.py


def _clamp_int32(value: int) -> int:
    """The client's CoinInfo.fcoin/coin are signed int32 (patch.py clamps every
    coin_info it serves for exactly that reason). Used to report a carat grant
    as the delta the client's own wallet can actually hold."""
    return min(value, _INT32_MAX)


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty_summary() -> dict:
    # Exact key set + order of the captured receive_all reward_summary_info.
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


def _add_item(full_state: dict, item_id: int, delta: int) -> int:
    """Delegates to shop._add_item (2026-08-24) so a claimed present's item
    grant is clamped to the item's real master.mdb limit_num the same way
    every other grant path already is -- see shop._add_item's own docstring
    for the live-reported bug (a mailed quantity past an item's real cap
    rendered as 0 in-game) this fixes here too.

    Returns the delta ACTUALLY applied (new total - old total), which is what
    reward_summary_info must report -- see _grant."""
    before = shop._item_count(full_state, item_id)
    return shop._add_item(full_state, item_id, delta) - before


def _summary_add_item(summary: dict, item_id: int, num: int) -> None:
    """add_item_list aggregates per item in first-seen order (capture-proven:
    5x 50000 Monies presents -> one {item_id:110, number:250000} line)."""
    for i in summary["add_item_list"]:
        if i.get("item_id") == item_id:
            i["number"] += num
            return
    summary["add_item_list"].append({"item_id": item_id, "number": num})


def _is_time_limited(present: dict) -> bool:
    return (present.get("reward_limit_time") or NO_LIMIT_TIME) < NO_LIMIT_TIME


def _claimable(present: dict, now_epoch: int) -> bool:
    return ((present.get("state") or 0) == 0
            and (present.get("reward_limit_time") or NO_LIMIT_TIME) >= now_epoch)


# The client's own filter vocabulary, from dump.cs PresentListBase:
#   FilterLimit    (time_filter_type): All 0, Limited 1, UnLimited 2
#   FilterCategory (category_filter_type[]): All 0, Card 1, Support 2, Item 3,
#                                            Other 99
#
# BUG FIXED 2026-09-10: _match_filters used to treat category_filter_type as a
# list of reward_types ("the nonzero vocab is a guess" -- it was wrong). It is
# a list of these five UI buckets, so filtering the box by "Support Card"
# asked for category 2 and matched reward_type 2, i.e. nothing at all. The
# time_filter_type guess, by contrast, matches FilterLimit exactly.
_CAT_ALL = 0
_CAT_CARD = 1
_CAT_SUPPORT = 2
_CAT_ITEM = 3
_CAT_OTHER = 99

# Which bucket a reward_type shows under. Card/Support are the two the UI
# names outright; everything the client renders with a count and an item icon
# (plain inventory, carats, card pieces) is Item, and the collectibles that
# have neither (outfits, music) fall to Other. INFERRED from the enum -- no
# capture exercises a nonzero filter, and a wrong bucket can only over- or
# under-filter a list, never corrupt state.
_CATEGORY_OF_TYPE = {
    _TYPE_CARD: _CAT_CARD,
    _TYPE_SUPPORT: _CAT_SUPPORT,
    _TYPE_CARAT: _CAT_ITEM,
    _TYPE_PIECE: _CAT_ITEM,
    _TYPE_CLOTH: _CAT_OTHER,
    _TYPE_MUSIC: _CAT_OTHER,
}


def _category_of(present: dict) -> int:
    """The FilterCategory bucket a present belongs to. Every reward_type not
    named above is a plain inventory item (see _grant's else branch), which is
    the Item bucket."""
    return _CATEGORY_OF_TYPE.get(present.get("reward_type") or 0, _CAT_ITEM)


def _match_category(present: dict, categories: list) -> bool:
    """An empty list, or one containing All, means no category filtering (the
    captures send [] for both index and history)."""
    if not categories or _CAT_ALL in categories:
        return True
    return _category_of(present) in categories


def _match_filters(present: dict, time_filter: int, categories: list) -> bool:
    """index's filter pair: FilterLimit + FilterCategory[]."""
    if time_filter == 1 and not _is_time_limited(present):
        return False
    if time_filter == 2 and _is_time_limited(present):
        return False
    return _match_category(present, categories)


def _pending(full_state: dict, now_epoch: int) -> list:
    box = full_state.get(PRESENT_BOX_KEY) or {}
    return [p for p in box.get("presents", []) if _claimable(p, now_epoch)]


def _pending_counts(pending: list) -> dict:
    limited = sum(1 for p in pending if _is_time_limited(p))
    return {"no_time_limit_present_num": len(pending) - limited,
            "time_limit_present_num": limited}


def _grant(viewer_id, full_state: dict, present: dict, summary: dict, now: str) -> None:
    """Apply ONE present's reward to the viewer's containers and record it in
    reward_summary_info. Container shapes mirror the load/index seed (see
    collection.py); support/card branches mirror shop.py's grant."""
    rtype = present.get("reward_type") or 0
    rid = present.get("reward_id")
    num = present.get("reward_count") or 1
    if rtype == _TYPE_CARAT:
        wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
        before = wallet.get("fcoin") or 0
        # BUG FIXED 2026-09-11 (live-reported: "claiming a present doesn't
        # update automatically, I need to restart my game -- at least for
        # carats"). The balance used to be STORED unclamped while patch.py
        # clamps every coin_info it puts on the wire to int32, so the two
        # disagreed for any account near the ceiling (several on this server
        # sit at ~2.0 billion and one was already past it). The client applies
        # add_fcoin as a delta to its own cached wallet, and the delta was
        # computed between the two CLAMPED values -- so a claim that pushed
        # the stored balance past int32 reported a short delta, or zero once
        # over, and the meter didn't move. A relaunch then refetched the
        # clamped ceiling from load/index, which is exactly the "only after a
        # restart" the report describes.
        #
        # Saturate the STORED balance instead, so state and wire never
        # disagree and the delta is always exactly what the client can hold.
        # Carats past int32 are unrepresentable in the client either way (see
        # patch.py), so nothing spendable is lost -- it just stops advertising
        # a number the client cannot decode.
        wallet["fcoin"] = _clamp_int32(before + num)
        summary["add_fcoin"] += wallet["fcoin"] - _clamp_int32(before)
    elif rtype == _TYPE_SUPPORT:
        cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
        owned = next((c for c in cards if c.get("support_card_id") == rid), None)
        if owned:
            owned["stock"] = (owned.get("stock") or 0) + num
        else:
            cards.append({"viewer_id": viewer_id, "support_card_id": rid, "exp": 0,
                          "limit_break_count": 0, "favorite_flag": 0, "stock": num - 1,
                          "possess_time": now, "create_time": now})
            summary["add_support_card_list"].append(
                {"support_card_id": rid, "exp": 0, "limit_break_count": 0,
                 "favorite_flag": 0, "stock": 0, "possess_time": now})
        summary["add_support_card_num_array"].append(
            {"support_card_id": rid, "number": num, "item_type": 51})
    elif rtype == _TYPE_CLOTH:
        cloths = full_state.setdefault("cloth_list_state", [])
        if not any(c.get("cloth_id") == rid for c in cloths):
            cloths.append({"cloth_id": rid})
        summary["add_cloth_list"].append({"cloth_id": rid})
    elif rtype == _TYPE_MUSIC:
        musics = full_state.setdefault("music_list_state", [])
        if not any(m.get("music_id") == rid for m in musics):
            musics.append({"music_id": rid, "acquisition_time": now})
        summary["add_music_list"].append({"music_id": rid, "acquisition_time": now})
    elif rtype == _TYPE_PIECE:
        pieces = full_state.setdefault("piece_list_state", [])
        p = next((p for p in pieces if p.get("piece_id") == rid), None)
        if p:
            p["piece_num"] = (p.get("piece_num") or 0) + num
        else:
            pieces.append({"piece_id": rid, "piece_num": num})
        summary["add_piece_list"].append({"piece_id": rid, "piece_num": num})
    elif rtype == _TYPE_CARD:
        # never captured in mail; mirrors shop.py's live-proven card grant
        cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
        if not any(c.get("card_id") == rid for c in cards):
            rar = master_data.query_one(
                "SELECT MIN(rarity) AS r FROM card_rarity_data WHERE card_id=?", (rid,))
            cards.append({"card_id": rid, "rarity": (rar["r"] if rar and rar["r"] else 1),
                          "talent_level": 1, "skill_data_array": []})
            summary["add_card_list"].append(
                {"card_id": rid, "rarity": 3, "talent_level": 1, "create_time": now})
            bonus = _empty_summary()
            bonus["add_cloth_list"] = [{"cloth_id": rid}]
            bonus["add_total_fan"] = 1
            summary["add_card_bonus_info"] = bonus
            chara = master_data.query_one(
                "SELECT chara_id FROM card_data WHERE id=?", (rid,))
            chara_id = chara["chara_id"] if chara else rid // 100
            charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
            entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                     "fan": 1, "max_grade": 0, "dress_id": 2,
                     "mini_dress_id": 2, "love_point_pool": 0}
            if not any(c.get("chara_id") == chara_id for c in charas):
                charas.append(entry)
                summary["add_chara_list"].append({k: v for k, v in entry.items()
                                                  if k != "love_point_pool"})
    else:
        # Every other captured reward_type is a plain inventory item.
        #
        # BUG FIXED 2026-09-03 (live-reported: "claiming an item from gifts
        # doesn't update the count until you restart the game"). The stored
        # count is clamped to the item's real master.mdb limit_num, but
        # reward_summary_info used to report the RAW mailed quantity. The
        # client applies add_item_list as a delta to its own cached item_list,
        # so an admin mail of 99999 of an item capped at 9999 left the client
        # holding a number its UI cannot represent -- it rendered stale/0, and
        # only a relaunch (which refetches the server-clamped item_list) showed
        # the truth. Report the delta ACTUALLY applied.
        applied = _add_item(full_state, rid, num)
        if applied:
            _summary_add_item(summary, rid, applied)


def _receive(payload: dict, wanted_ids=None) -> dict:
    """Shared claim loop: receive_all when wanted_ids is None, single/select
    receive otherwise. Claim = grant + state 1 + receive_time (the ledger)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    box = full_state.get(PRESENT_BOX_KEY) or {}
    presents = box.get("presents", [])
    now_epoch = int(time.time())
    now = _now()
    time_filter = payload.get("time_filter_type") or 0
    categories = payload.get("category_filter_type") or []
    summary = _empty_summary()
    claimed = 0
    for p in presents:
        if not _claimable(p, now_epoch):
            continue
        if wanted_ids is not None and p.get("present_id") not in wanted_ids:
            continue
        if wanted_ids is None and not _match_filters(p, time_filter, categories):
            continue
        _grant(viewer_id, full_state, p, summary, now)
        p["state"] = 1
        p["receive_time"] = now
        claimed += 1
    if claimed:
        state_store.save_state(viewer_id, full_state)
    if wanted_ids is not None and not claimed:
        return _refuse()  # claiming a missing/claimed/expired present = desync
    return _ok({"reward_summary_info": summary,
                "receive_present_num": claimed,
                "rest_present_num": len(_pending(full_state, now_epoch)),
                "no_receive_support_card_count": 0,
                "no_receive_item_flg": False})


@registry.endpoint("present/index")
def handle_index(payload: dict) -> dict:
    """present/index: {time_filter_type, category_filter_type[], offset, limit,
    is_asc} -> the page of unclaimed presents + the badge counts. Read-only:
    a fresh viewer gets an empty box without materializing any state."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_epoch = int(time.time())
    pending = _pending(full_state, now_epoch)
    time_filter = payload.get("time_filter_type") or 0
    categories = payload.get("category_filter_type") or []
    shown = [p for p in pending if _match_filters(p, time_filter, categories)]
    shown.sort(key=lambda p: (p.get("create_time") or "", p.get("present_id") or 0),
               reverse=not payload.get("is_asc", True))
    offset = int(payload.get("offset") or 0)
    limit = int(payload.get("limit") or 100)
    return _ok({"present_array": copy.deepcopy(shown[offset:offset + limit]),
                "no_receive_present_num": _pending_counts(pending)})


# History has no time_filter_type -- a claimed present's expiry is spent
# history, so the client's limited/permanent toggle isn't offered on this tab.
_HISTORY_MAX_LIMIT = 500      # dump.cs PresentListHistory.PRESENT_HISTORY_SHOW_LIMIT


@registry.endpoint("present/history")
def handle_history(payload: dict) -> dict:
    """present/history: {category_filter_type[], offset, limit, is_asc} ->
    {present_array} -- the CLAIMED presents, i.e. the box's receipt log.

    Request shape is capture-confirmed (raw log 0001_RAW_present_history:
    {"category_filter_type": [], "offset": 0, "limit": 100, "is_asc": true});
    the response is dump.cs PresentHistoryResponse.CommonResponse, whose only
    field is a PresentData[] -- the same row present/index already serves.

    BUG FIXED 2026-09-10 (live-reported "the present history tab shows
    nothing"): this endpoint had no handler, so it fell through to
    NOOP_SUCCESS and answered a bare {}. present_array then deserialized to
    NULL rather than to an empty list, and the tab rendered blank -- the same
    failure mode as card/get_release_card_array.

    Claimed presents are already retained in the box with state=1 and a
    receive_time (the claim ledger that makes receive_all idempotent), so the
    log is simply those rows. Ordered by when they were CLAIMED, which is what
    this tab is a record of -- not by create_time, which is what index sorts
    on. Unclaimed presents belong to the index tab and are excluded, expired
    ones included: an expired present that was claimed in time is still part
    of the receipt log.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    box = full_state.get(PRESENT_BOX_KEY) or {}
    categories = payload.get("category_filter_type") or []

    claimed = [p for p in box.get("presents", [])
               if (p.get("state") or 0) != 0
               and _match_category(p, categories)]
    claimed.sort(key=lambda p: (p.get("receive_time") or "",
                                p.get("present_id") or 0),
                 reverse=not payload.get("is_asc", True))

    offset = max(0, int(payload.get("offset") or 0))
    limit = int(payload.get("limit") or 100)
    limit = max(1, min(limit, _HISTORY_MAX_LIMIT))
    # present_array must be a LIST even when empty -- see the bug note above.
    return _ok({"present_array": copy.deepcopy(claimed[offset:offset + limit])})


@registry.endpoint("present/receive_all")
def handle_receive_all(payload: dict) -> dict:
    """present/receive_all: {time_filter_type, category_filter_type[], is_asc,
    current_rest_present_num} -> claim every claimable present. Idempotent by
    ledger: a second call finds nothing unclaimed and claims zero."""
    return _receive(payload, wanted_ids=None)


@registry.endpoint("present/receive")
def handle_receive(payload: dict) -> dict:
    """present/receive -- SYNTHESIZED-UNVERIFIED (never captured; raw-log the
    real request the day one appears). Assumed {present_id} (a present_id_array
    is also accepted); answers the receive_all body for the one present, and
    refuses (205) when nothing matched so a desynced client resyncs."""
    ids = set()
    if payload.get("present_id") is not None:
        ids.add(payload["present_id"])
    for pid in payload.get("present_id_array") or []:
        ids.add(pid)
    if not ids:
        return _refuse()
    return _receive(payload, wanted_ids=ids)


def send(full_state: dict, item_category, item_id, item_num, message="") -> dict:
    """Append one present to the viewer's inbox (the admin.py `send-mail`
    hook). item_category = wire reward_type (90 carats, 51 support card, 70
    outfit, 80 music, 50 card, 102 piece, anything else = plain item). Returns
    the created wire row. A custom message rides the captured free_message
    mechanism (message_id 99999); without one, the param-less canned ops text
    (message_id 44) is used."""
    box = full_state.setdefault(PRESENT_BOX_KEY, {})
    presents = box.setdefault("presents", [])
    pid = box.get("next_present_id") or _FIRST_PRESENT_ID
    entry = {
        "present_id": pid,
        "state": 0,
        "reward_type": int(item_category),
        "reward_id": int(item_id),
        "reward_count": int(item_num),
        "message_id": _FREE_MESSAGE_ID if message else _CANNED_MESSAGE_ID,
        "message_param_value_1": 0,
        "message_param_value_2": 0,
        "message_param_value_3": 0,
        "message_param_value_4": 0,
        "reward_limit_time": NO_LIMIT_TIME,
        "receive_time": "0000-00-00 00:00:00",
        "create_time": _now(),
        "free_message": message or "",
    }
    presents.append(entry)
    box["next_present_id"] = pid + 1
    return copy.deepcopy(entry)


def admin_send(viewer_id, item_category, item_id, item_num, message="") -> dict:
    """Load-mutate-save wrapper around send() for callers with no state in
    hand (admin.py's `send-mail`). A handler already holding a full_state it
    will save itself MUST use send() instead: this re-reads the blob from
    SQLite, so mailing through it mid-handler loses either the mail or every
    other change the handler made, depending on who saves last."""
    full_state = state_store.get_state(viewer_id) or {}
    entry = send(full_state, item_category, item_id, item_num, message)
    state_store.save_state(viewer_id, full_state)
    return entry
