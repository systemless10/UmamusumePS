
from __future__ import annotations

from ... import accounts
from ... import state as state_store
from ...handlers import cards, collection, shop
from ...result_codes import refuse

MAX_LIMIT_BREAK = 4
SUPPORT_PT_ITEM_ID = 110
_ITEM_CATEGORY = 30

_OWN_CARD_KEYS = ("add_support_card_list", "support_card_list")


def normalize(full_state: dict) -> tuple[dict, bool]:
    moved, changed = {}, False
    for c in full_state.get(collection.SUPPORT_CARD_KEY) or []:
        sid = c.get("support_card_id")
        lb = int(c.get("limit_break_count") or 0)
        stock = int(c.get("stock") or 0)
        spend = min(stock, max(0, MAX_LIMIT_BREAK - lb))
        if spend:
            moved[sid] = lb
            lb += spend
            c["limit_break_count"] = lb
            c["stock"] = stock - spend
            changed = True
        cap = cards._support_level_cap(sid, lb)
        if cap is not None and c.get("exp") != cap[1]:
            c["exp"] = cap[1]
            changed = True
    items = shop._items(full_state)
    kept = [i for i in items if i.get("item_id") != SUPPORT_PT_ITEM_ID]
    if len(kept) != len(items):
        items[:] = kept
        changed = True
    return moved, changed


def _is_support_pt(entry: dict) -> bool:
    if entry.get("item_id") != SUPPORT_PT_ITEM_ID:
        return False
    cat = entry.get("item_category", entry.get("item_type", _ITEM_CATEGORY))
    return cat == _ITEM_CATEGORY


def _walk(node, found: dict):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _OWN_CARD_KEYS and isinstance(v, list):
                found["entries"].extend(e for e in v if isinstance(e, dict))
            elif k == "support_card_data" and isinstance(v, dict) and "limit_break_count" in v:
                found["entries"].append(v)
            if k == "add_support_card_list" and v:
                found["summaries"].append(node)
            _walk(v, found)
    elif isinstance(node, list):
        if any(isinstance(e, dict) and _is_support_pt(e) for e in node):
            node[:] = [e for e in node if not (isinstance(e, dict) and _is_support_pt(e))]
        for e in node:
            _walk(e, found)


def _limit_break_array(summary: dict, owned: dict, moved: dict) -> list:
    order = [e.get("support_card_id") for e in summary.get("add_support_card_num_array") or []]
    order += [e.get("support_card_id") for e in summary["add_support_card_list"]]
    out, seen = [], set()
    for sid in order:
        card = owned.get(sid)
        if sid in seen or card is None:
            continue
        seen.add(sid)
        after = card.get("limit_break_count") or 0
        out.append({"support_card_id": sid,
                    "before_limit_break_count": moved.get(sid, after),
                    "after_limit_break_count": after,
                    "is_send_stock": after >= MAX_LIMIT_BREAK and (card.get("stock") or 0) > 0,
                    "is_send_present": False})
    return out


def apply(endpoint: str, payload: dict, resp: dict) -> dict:
    data = resp.get("data")
    viewer_id = (payload or {}).get("viewer_id")
    if not isinstance(data, dict) or not viewer_id \
            or (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return resp
    found = {"entries": [], "summaries": []}
    _walk(data, found)
    if not found["entries"] and endpoint != "load/index":
        return resp
    with accounts.viewer_lock(viewer_id):
        full_state = state_store.get_state(viewer_id)
        if not full_state:
            return resp
        moved, changed = normalize(full_state)
        if changed:
            state_store.save_state(viewer_id, full_state)
    owned = {c.get("support_card_id"): c for c in full_state.get(collection.SUPPORT_CARD_KEY) or []}
    for e in found["entries"]:
        card = owned.get(e.get("support_card_id"))
        if card is not None:
            for k in ("exp", "limit_break_count", "stock"):
                e[k] = card.get(k) or 0
    for summary in found["summaries"]:
        summary["add_support_card_limit_break_array"] = _limit_break_array(summary, owned, moved)
    return resp


def _refuse_levelling(payload: dict) -> dict:
    return refuse()


HANDLERS = {
    "support_card/strengthen": _refuse_levelling,
    "support_card/limit_break": _refuse_levelling,
}
