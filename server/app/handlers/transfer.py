"""
transfer/* -- the "Transfer Event" feature (dump.cs: TransferIndexRequest,
TransferDetailRequest, TransferExecMultiRequest). NEVER captured from the real
server -- built from dump.cs field shapes + master.mdb's transfer_event_*
tables. transfer_rotation_* tables exist in the schema but are entirely EMPTY
in this master.mdb snapshot, so the always-on schedule comes straight from
transfer_event_data's own start_date/end_date windows, not a rotation.

Mechanic (inferred, no real capture confirms sequencing or exact formulas):
  - transfer_event_data: which transfer_event_id is "live" right now (a
    servertime window, same pattern as team_stadium.py's _current_term_state).
  - transfer_event_detail: multiple time-boxed "requests" within that event,
    each themed around a trainer_type (which in-game NPC is asking -- pure
    flavor, not a stat gate) and scored against up to 5 condition{type,
    value1,value2} checks (dump.cs: TransferEventDefine.ConditionType --
    Speed/Stamina/Power/Guts/Wiz, GroundTurf/Dirt, DistanceShort/Mile/Middle/
    Long, RunStyleNige/Senko/Sashi/Oikomi, Skill, InheritChara, FanNum,
    RaceHistory). value1 is read as the threshold/target each condition's
    matching trained_chara field must reach.
  - Submitting (exec_multi) hands a trained_chara over for good -- CONSUMED,
    removed from the owned roster -- in exchange for rewards scaled by how
    many of the detail's defined conditions that specific horse satisfies:
    dump.cs's TransferEventDefine.RewardRank Premium=1 (all conditions met)
    down to Regular=3 (guaranteed baseline, matches its own Min=3). Every
    real row in transfer_event_reward has odds=1000000 (100%) -- there is no
    lottery despite the column name, so all reward rows for the achieved
    rank are granted together.
  - transfer/detail's response is a PER-TRAINED-CHARA preview (dump.cs:
    TransferEventRewardInfo{reward_array, trained_chara_id}) -- for every
    OWNED trained_chara, what it would earn if submitted right now -- not a
    flat reward table. transfer/index's response is much thinner
    (TransferEventDetailInfo{transfer_detail_id, exists_trained_chara,
    remaining_num} per period): just "can you do this, how many times".
  - remaining_num/cooldown: transfer_event_detail.cool_time (seconds) gates
    resubmitting to the SAME detail_id -- tracked per-viewer since no capture
    exists to confirm the real cap shape; modeled as "1 submission per
    cool_time window" rather than a fixed lifetime count, the more common
    real-game convention for this kind of repeatable request.
"""

from __future__ import annotations

import copy

from .. import master_data
from .. import patch
from .. import state as state_store
from . import registry, trained_chara

TRANSFER_STATE_KEY = "transfer_state"   # {detail_id (str): {"last_submit_at": int}}


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _transfer_state(full_state: dict) -> dict:
    st = full_state.get(TRANSFER_STATE_KEY)
    if not isinstance(st, dict):
        st = {}
        full_state[TRANSFER_STATE_KEY] = st
    return st


# ============================================================ master.mdb ====

def _active_event():
    """The transfer_event_data row whose window brackets "now", or None."""
    now = patch._servertime()
    return master_data.query_one(
        "SELECT * FROM transfer_event_data WHERE start_date<=? AND end_date>=?"
        " ORDER BY start_date DESC LIMIT 1", (now, now))


def _active_details(event_id: int) -> list:
    """transfer_event_detail rows for this event whose OWN (further time-
    boxed) window also brackets "now" -- each capture-sampled detail_id in
    this table spans roughly a day within the event's longer overall run."""
    now = patch._servertime()
    return master_data.query(
        "SELECT * FROM transfer_event_detail WHERE transfer_event_id=?"
        " AND start_date<=? AND end_date>=? ORDER BY transfer_detail_id",
        (event_id, now, now))


def _detail_row(detail_id: int):
    return master_data.query_one(
        "SELECT * FROM transfer_event_detail WHERE transfer_detail_id=?", (detail_id,))


def _reward_rows(detail_id: int, reward_rank: int) -> list:
    return master_data.query(
        "SELECT * FROM transfer_event_reward WHERE transfer_detail_id=? AND reward_rank=?",
        (detail_id, reward_rank))


# ========================================================== conditions ====
# TransferEventDefine.ConditionType (dump.cs) -- see module docstring.
_STAT_FIELD = {100: "speed", 110: "stamina", 120: "power", 130: "guts", 140: "wiz"}
_APTITUDE_FIELD = {
    200: "proper_ground_turf", 210: "proper_ground_dirt",
    300: "proper_distance_short", 310: "proper_distance_mile",
    320: "proper_distance_middle", 330: "proper_distance_long",
    400: "proper_running_style_nige", 410: "proper_running_style_senko",
    420: "proper_running_style_sashi", 430: "proper_running_style_oikomi",
}
_COND_SKILL = 500
_COND_INHERIT_CHARA = 600
_COND_FAN_NUM = 700
_COND_RACE_HISTORY = 800


def _condition_met(chara: dict, ctype: int, value1: int) -> bool:
    """One condition{type,value1} check against an owned trained_chara dict.
    value2 is currently unused everywhere it's been observed (always 0 in
    every real transfer_event_detail row) -- not modeled."""
    if ctype in _STAT_FIELD:
        return (chara.get(_STAT_FIELD[ctype]) or 0) >= value1
    if ctype in _APTITUDE_FIELD:
        return (chara.get(_APTITUDE_FIELD[ctype]) or 0) >= value1
    if ctype == _COND_FAN_NUM:
        return (chara.get("fans") or 0) >= value1
    if ctype == _COND_SKILL:
        # INFERRED: value1 read as a required skill_id the horse must own.
        return any(s.get("skill_id") == value1 for s in (chara.get("skill_array") or []))
    if ctype == _COND_INHERIT_CHARA:
        # INFERRED: value1 read as a required card_id somewhere in succession.
        return any(c.get("card_id") == value1 for c in (chara.get("succession_chara_array") or []))
    if ctype == _COND_RACE_HISTORY:
        # INFERRED: value1 read as a required program_id (race) previously run.
        return any(r.get("program_id") == value1 for r in (chara.get("race_result_list") or []))
    return True   # unrecognized code -- generous default, never a hard blocker


def _achieved_rank(detail_row, chara: dict) -> int:
    """TransferEventDefine.RewardRank: 1 (Premium/Max) when every DEFINED
    condition is met, 3 (Regular/Min) when none are, 2 (Special) for partial
    credit. A detail with zero defined conditions trivially maxes out."""
    total = met = 0
    for i in range(1, 6):
        ctype = detail_row[f"condition{i}_type"] or 0
        if not ctype:
            continue
        total += 1
        if _condition_met(chara, ctype, detail_row[f"condition{i}_value1"] or 0):
            met += 1
    if total == 0 or met == total:
        return 1
    if met == 0:
        return 3
    return 2


def _piece_id_for_card(card_id: int) -> int:
    """Same lookup daily_races.py's own _piece_id_for_card performs (no
    shared helper exists in this codebase)."""
    row = master_data.query_one("SELECT get_piece_id FROM card_data WHERE id=?", (card_id,))
    return row["get_piece_id"] if row and row["get_piece_id"] else card_id


def _resolve_reward_item(row, chara: dict) -> tuple[int, int, int]:
    """(item_type, item_id, item_num). item_category=102 (piece) rows with
    item_id=0 in transfer_event_reward -- confirmed real, not a data gap --
    read as "a piece of the card being traded in": a pity-refund convention,
    since the submitted trained_chara itself is otherwise gone for good."""
    cat, iid, num = row["item_category"], row["item_id"], row["item_num"]
    if cat == _ITEM_TYPE_PIECE and not iid:
        iid = _piece_id_for_card(chara.get("card_id"))
    return cat, iid, num


def _reward_array_for(detail_row, chara: dict) -> list:
    rank = _achieved_rank(detail_row, chara)
    out = []
    for r in _reward_rows(detail_row["transfer_detail_id"], rank):
        cat, iid, num = _resolve_reward_item(r, chara)
        out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out


# =============================================================== reward ====

def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_fcoin": 0}


_ITEM_TYPE_CARAT = 90
_ITEM_TYPE_PIECE = 102


def _grant(full_state: dict, category: int, item_id: int, num: int, summary: dict) -> None:
    """Mirrors daily_races.py's own _grant (no shared helper exists in this
    codebase -- every feature that grants rewards keeps its own copy)."""
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
        items = full_state.setdefault("item_list_state", [])
        entry = next((i for i in items if i.get("item_id") == item_id), None)
        if entry:
            entry["number"] = (entry.get("number") or 0) + num
        else:
            items.append({"item_id": item_id, "number": num})
        summary["add_item_list"].append({"item_id": item_id, "number": num})


# ============================================================= endpoints ====

@registry.endpoint("transfer/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event = _active_event()
    if not event:
        return _ok({"transfer_event_id": 0, "transfer_event_detail_array": []})

    full_state = state_store.get_state(viewer_id) or {}
    st = _transfer_state(full_state)
    roster = trained_chara._get_or_seed_roster(viewer_id)
    now = patch._servertime()

    detail_array = []
    for row in _active_details(event["transfer_event_id"]):
        did = row["transfer_detail_id"]
        last_submit = (st.get(str(did)) or {}).get("last_submit_at") or 0
        cool_time = row["cool_time"] or 0
        ready = (now - last_submit) >= cool_time
        detail_array.append({
            "transfer_detail_id": did,
            "exists_trained_chara": bool(roster),
            "remaining_num": 1 if ready else 0,
        })
    return _ok({"transfer_event_id": event["transfer_event_id"],
               "transfer_event_detail_array": detail_array})


def transfer_event_info(viewer_id) -> dict | None:
    """The `transfer_event_info` block other endpoints embed -- notably
    single_mode_*/finish, whose real responses carry it whenever a Transfer
    event is running (and null when none is). Same {transfer_event_id,
    transfer_event_home_detail_info} shape handle_exec_multi returns, built
    from the account's live roster and per-detail cooldowns exactly as
    transfer/index builds its own array."""
    event = _active_event()
    if not event:
        return None
    full_state = state_store.get_state(viewer_id) or {}
    st = _transfer_state(full_state)
    roster = trained_chara._get_or_seed_roster(viewer_id)
    now = patch._servertime()
    details = []
    for row in _active_details(event["transfer_event_id"]):
        did = row["transfer_detail_id"]
        last_submit = (st.get(str(did)) or {}).get("last_submit_at") or 0
        ready = (now - last_submit) >= (row["cool_time"] or 0)
        details.append({"transfer_detail_id": did,
                        "exists_trained_chara": bool(roster),
                        "remaining_num": 1 if ready else 0})
    return {"transfer_event_id": event["transfer_event_id"],
            "transfer_event_home_detail_info": details}


@registry.endpoint("transfer/detail")
def handle_detail(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    detail_id = payload.get("transfer_detail_id")
    row = _detail_row(detail_id)
    if not row:
        return _refuse()

    roster = trained_chara._get_or_seed_roster(viewer_id)
    reward_info = [
        {"reward_array": _reward_array_for(row, chara),
         "trained_chara_id": chara.get("trained_chara_id")}
        for chara in roster
    ]
    return _ok({
        "transfer_event_reward_info": reward_info,
        "room_match_entry_chara_id_array": [],   # RoomMatch doesn't exist yet
    })


@registry.endpoint("transfer/exec_multi")
def handle_exec_multi(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event_id = payload.get("transfer_event_id")
    detail_id = payload.get("transfer_detail_id")
    ids = payload.get("trained_chara_ids")
    if not isinstance(ids, list) or not ids:
        return _refuse()

    row = _detail_row(detail_id)
    if not row:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _transfer_state(full_state)
    now = patch._servertime()
    last_submit = (st.get(str(detail_id)) or {}).get("last_submit_at") or 0
    if (now - last_submit) < (row["cool_time"] or 0):
        return _refuse()   # on cooldown -- matches index's remaining_num=0

    roster = full_state.get(trained_chara.ROSTER_KEY)
    if roster is None:
        roster = trained_chara._get_or_seed_roster(viewer_id)
    by_id = {c.get("trained_chara_id"): c for c in roster}
    submitted = [by_id[i] for i in ids if i in by_id]
    if not submitted:
        return _refuse()

    summary = _empty_summary()
    best_rank = 3   # worst tier; the response's single `rank` is the best
                    # reached across a multi-submit (matches "Max"/"Min"
                    # enum naming -- lower value = better here)
    for chara in submitted:
        rank = _achieved_rank(row, chara)
        best_rank = min(best_rank, rank)
        for r in _reward_rows(detail_id, rank):
            cat, iid, num = _resolve_reward_item(r, chara)
            _grant(full_state, cat, iid, num, summary)

    # Consume: submitted trained_chara are handed over for good.
    submitted_ids = {c.get("trained_chara_id") for c in submitted}
    full_state[trained_chara.ROSTER_KEY] = [
        c for c in roster if c.get("trained_chara_id") not in submitted_ids]

    st[str(detail_id)] = {"last_submit_at": now}
    state_store.save_state(viewer_id, full_state)

    remaining_roster = full_state[trained_chara.ROSTER_KEY]
    home_details = [{
        "transfer_detail_id": detail_id,
        "exists_trained_chara": bool(remaining_roster),
        "remaining_num": 0,
    }]
    return _ok({
        "rank": best_rank,
        "reward_summary_info": summary,
        "trained_chara_array": copy.deepcopy(remaining_roster),
        "transfer_event_info": {"transfer_event_id": event_id,
                                "transfer_event_home_detail_info": home_details},
    })
