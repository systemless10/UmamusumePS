
from __future__ import annotations

import copy
import functools
import json

from ... import region, social
from ... import state as state_store
from ...handlers import collection, trained_chara, user_profile
from . import succession


def _default_partners(node: dict) -> list:
    utc = node.get("user_trained_chara")
    if isinstance(utc, dict) and utc.get("trained_chara_id"):
        return [utc]
    return []


def _support_card_data(owner, full_state: dict, ids: list) -> list:
    owned = {c.get("support_card_id"): c
             for c in full_state.get(collection.SUPPORT_CARD_KEY) or []}
    out = []
    for sid in ids:
        card = owned.get(sid)
        if not sid or card is None:
            continue
        out.append({
            "viewer_id": social._wire_id(owner), "support_card_id": sid,
            "exp": card.get("exp") or 0, "limit_break_count": card.get("limit_break_count") or 0,
            "favorite_flag": card.get("favorite_flag") or 0, "stock": card.get("stock") or 0,
            "possess_time": card.get("possess_time") or card.get("create_time") or "",
            "create_time": card.get("create_time") or "",
        })
    return out


def handle_friend_search(payload: dict) -> dict:
    resp = user_profile.handle_friend_search(payload)
    data = resp.get("data")
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1 or not isinstance(data, dict):
        return resp
    target = payload.get("friend_viewer_id")
    summary = data.get("user_info_summary") or {}
    partner = data.pop("practice_partner_info", None) or {}
    single_support = data.pop("support_card_data", None)

    partner_ids = [t for t in summary.get("partner_chara_id_array") or [] if t]
    support_ids = [c for c in summary.get("support_card_id_array") or [] if c]
    target_state = (state_store.get_state(str(target)) or {}) if (partner_ids or support_ids) else {}
    if partner_ids:
        by_id = {c.get("trained_chara_id"): c
                 for c in target_state.get(trained_chara.ROSTER_KEY) or []}
        partners = [succession.wire_trained_chara(target, by_id[t]) for t in partner_ids if t in by_id]
    else:
        partners = [partner] if partner else []
    if support_ids:
        supports = _support_card_data(target, target_state, support_ids)
    else:
        supports = [single_support] if single_support else []

    summary["partner_chara_id_array"] = succession._slots(p.get("trained_chara_id") for p in partners)
    summary["user_trained_chara_array"] = [
        succession._digest(dict(p, viewer_id=social._wire_id(target))) for p in partners]
    if partners:
        summary["partner_chara_id"] = partners[0].get("trained_chara_id") or 0
    summary["support_card_id_array"] = succession._slots(
        support_ids or [s.get("support_card_id") for s in supports])
    data["partner_chara_info_array"] = partners
    data["support_card_data_array"] = supports
    data.setdefault("friend_trained_chara_favorite_array", [])
    for entry in data.get("directory_card_array") or []:
        tc = entry.get("trained_chara")
        if isinstance(tc, dict):
            tc.setdefault("directory_ranking", entry.get("directory_ranking") or 0)
    return resp


HANDLERS = {
    "friend/search": handle_friend_search,
}


@functools.lru_cache(maxsize=1)
def _endpoints() -> frozenset:
    path = region.data_dir() / "response_schema.json"
    try:
        eps = json.loads(path.read_text(encoding="utf-8")).get("endpoints") or {}
    except (OSError, ValueError):
        return frozenset()
    return frozenset(ep for ep, s in eps.items() if "partner_chara_id_array" in json.dumps(s))


def _profiles(node, out: list) -> None:
    if isinstance(node, dict):
        if "viewer_id" in node and "leader_chara_id" in node and "support_card_id" in node:
            out.append(node)
        for v in node.values():
            _profiles(v, out)
    elif isinstance(node, list):
        for v in node:
            _profiles(v, out)


def fill(endpoint: str, resp: dict) -> None:
    if endpoint not in _endpoints() or not isinstance(resp.get("data"), dict):
        return
    nodes: list = []
    _profiles(resp["data"], nodes)
    for n in nodes:
        if not n.get("support_card_id_array"):
            n["support_card_id_array"] = succession._slots([n.get("support_card_id")])
        is_summary = any(k in n for k in ("user_trained_chara", "user_trained_chara_array",
                                          "friend_state", "user_support_card"))
        if not n.get("partner_chara_id_array"):
            partners = _default_partners(n)
            if partners:
                n["partner_chara_id_array"] = succession._slots(
                    [partners[0]["trained_chara_id"]])
            else:
                n["partner_chara_id_array"] = succession._slots(
                    [] if is_summary else [n.get("partner_chara_id")])
            if is_summary and not n.get("user_trained_chara_array"):
                n["user_trained_chara_array"] = copy.deepcopy(partners)
