
from __future__ import annotations

import contextvars
import copy

from .. import master_data

HIDDEN_KEY = "directory_hidden_chara"
PER_GROUP = 3
_STARTER_MAX_ID = 6
_SCENARIO_RECORD_MESSAGE_ID = 50

LAST_RETIRE: contextvars.ContextVar = contextvars.ContextVar("LAST_RETIRE", default=None)


def eligible(rec: dict) -> bool:
    from . import trained_chara
    tcid = rec.get("trained_chara_id") or 0
    if tcid <= _STARTER_MAX_ID or tcid >= trained_chara.HOUSE_ID_BASE:
        return False
    return not rec.get("owner_viewer_id") and not rec.get("use_type")


def hidden(full_state: dict) -> list:
    rows = full_state.get(HIDDEN_KEY)
    return rows if isinstance(rows, list) else []


def pool(full_state: dict, roster: list) -> list:
    return [r for r in roster if eligible(r)] + hidden(full_state)


def _order(rec: dict):
    return (-(rec.get("rank_score") or 0), rec.get("trained_chara_id") or 0)


def _groups(horses: list) -> dict:
    out: dict = {}
    for rec in horses:
        out.setdefault((rec.get("card_id"), rec.get("scenario_id")), []).append(rec)
    for members in out.values():
        members.sort(key=_order)
    return out


def ranked(horses: list) -> list:
    groups = _groups(horses)
    out = []
    for key in sorted(groups, key=lambda k: (k[0] or 0, k[1] or 0)):
        out.extend((rec, i) for i, rec in enumerate(groups[key][:PER_GROUP], start=1))
    return out


def ranking_of(horses: list, trained_chara_id) -> int:
    for rec, rank in ranked(horses):
        if rec.get("trained_chara_id") == trained_chara_id:
            return rank
    return 0


def _wire(viewer_id, rec: dict, rank: int) -> dict:
    tc = copy.deepcopy(rec)
    if viewer_id is not None:
        tc["viewer_id"] = viewer_id
    tc["directory_ranking"] = rank
    return tc


def card_array(viewer_id, horses: list, card_id=None) -> list:
    return [{"card_id": rec.get("card_id"), "directory_ranking": rank,
             "trained_chara": _wire(viewer_id, rec, rank)}
            for rec, rank in ranked(horses)
            if card_id is None or rec.get("card_id") == card_id]


def top_per_card(viewer_id, horses: list) -> list:
    best: dict = {}
    for rec in horses:
        cid = rec.get("card_id")
        if cid and (cid not in best or _order(rec) < _order(best[cid])):
            best[cid] = rec
    return [{"card_id": cid, "directory_ranking": 1,
             "trained_chara": _wire(viewer_id, best[cid], 1)}
            for cid in sorted(best)]


def entry_count(horses: list) -> int:
    return len(ranked(horses))


def card_score(horses: list) -> int:
    per_card: dict = {}
    for rec, _ in ranked(horses):
        per_card.setdefault(rec.get("card_id"), []).append(rec.get("rank_score") or 0)
    return sum(sum(sorted(v, reverse=True)[:PER_GROUP]) for v in per_card.values())


def scenario_top(viewer_id, horses: list, scenario_id) -> list:
    members = sorted((r for r in horses if r.get("scenario_id") == scenario_id),
                     key=_order)[:PER_GROUP]
    return [{"scenario_id": scenario_id, "directory_ranking": i,
             "trained_chara": _wire(viewer_id, rec, i)}
            for i, rec in enumerate(members, start=1)]


def scenario_highs(full_state: dict, horses: list) -> dict:
    from . import note_archive
    ledger = note_archive._note_state(full_state).setdefault("scenario_high_scores", {})
    highs = {int(k): v for k, v in ledger.items()}
    for rec in horses:
        sid = rec.get("scenario_id")
        if sid:
            highs[sid] = max(highs.get(sid, 0), rec.get("rank_score") or 0)
    return highs


def retire(full_state: dict, roster: list, ids) -> tuple[list, list]:
    doomed = set(ids)
    horses = pool(full_state, roster)
    in_top = {rec.get("trained_chara_id") for rec, _ in ranked(horses)}
    store = full_state.setdefault(HIDDEN_KEY, [])
    kept, deleted = [], []
    for rec in roster:
        tcid = rec.get("trained_chara_id")
        if tcid not in doomed:
            continue
        if eligible(rec) and tcid in in_top:
            store.append(copy.deepcopy(rec))
            kept.append(tcid)
        else:
            deleted.append(tcid)
    LAST_RETIRE.set((kept, deleted))
    return kept, deleted


def prune_hidden(full_state: dict, roster: list) -> None:
    store = hidden(full_state)
    if not store:
        return
    in_top = {rec.get("trained_chara_id") for rec, _ in ranked(pool(full_state, roster))}
    full_state[HIDDEN_KEY] = [r for r in store if r.get("trained_chara_id") in in_top]


def settle_scenario_record(full_state: dict, horses_before: list, new_rec: dict) -> list:
    from . import note_archive, presents
    sid = new_rec.get("scenario_id")
    score = new_rec.get("rank_score") or 0
    if not sid:
        return []
    prev = scenario_highs(full_state, horses_before).get(sid, 0)
    ledger = note_archive._note_state(full_state).setdefault("scenario_high_scores", {})
    ledger[str(sid)] = max(prev, score)
    if score <= prev:
        return []
    out = []
    for row in master_data.query(
            "SELECT reward_item_category, reward_item_id, reward_num "
            "FROM single_mode_scenario_record WHERE scenario_id = ? "
            "AND need_record_min > ? AND need_record_min <= ? ORDER BY need_record_min",
            (sid, prev, score)):
        cat, iid, num = (row["reward_item_category"], row["reward_item_id"],
                         row["reward_num"])
        presents.send(full_state, cat, iid, num, message_id=_SCENARIO_RECORD_MESSAGE_ID)
        out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out
