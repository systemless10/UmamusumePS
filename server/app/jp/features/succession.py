
from __future__ import annotations

import copy
import logging
import re

from ... import social
from ... import state as state_store
from ...handlers import collection, directory, trained_chara, user_profile
from ...handlers import single_mode_team as smt
from ...result_codes import refuse

log = logging.getLogger("uma-server")

SLOTS = 6
NO_RECORD = -1
_FOLLOWING = (social.STATE_FOLLOW, social.STATE_MUTUAL)

_SUMMARY_FIELDS = (
    "viewer_id", "name", "honor_data", "last_login_time", "leader_chara_id",
    "leader_chara_dress_id", "support_card_id", "support_card_id_array",
    "partner_chara_id", "partner_chara_id_array", "comment", "fan", "rank_score",
    "team_stadium_win_count", "single_mode_play_count", "team_evaluation_point",
    "user_support_card", "user_trained_chara_array", "circle_info", "circle_user",
    "friend_state")
_DIGEST_FIELDS = (
    "trained_chara_id", "card_id", "rank_score", "rank", "proper_ground_turf",
    "proper_ground_dirt", "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi", "proper_distance_short",
    "proper_distance_mile", "proper_distance_middle", "proper_distance_long", "rarity",
    "talent_level", "register_time")
_INTERNAL = ("grand_live_songs", "grand_live_lives_done")


def _ok(data) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def _slots(values) -> list:
    out = [int(v or 0) for v in (values or [])][:SLOTS]
    return out + [0] * (SLOTS - len(out))


def wire_trained_chara(viewer_id, entry: dict) -> dict:
    out = {k: copy.deepcopy(v) for k, v in entry.items()
           if not k.startswith("_") and k not in _INTERNAL}
    out["viewer_id"] = social._wire_id(viewer_id)
    out.setdefault("total_skill_pt", NO_RECORD)
    return out


def _digest(entry: dict) -> dict:
    out = {"viewer_id": entry["viewer_id"]}
    for k in _DIGEST_FIELDS:
        out[k] = entry.get(k)
    out["skill_count"] = len(entry.get("skill_array") or [])
    out["factor_info_array"] = [{"factor_id": f.get("factor_id"), "level": f.get("level", 0)}
                                for f in entry.get("factor_info_array") or []]
    out["factor_extend_array"] = copy.deepcopy(entry.get("factor_extend_array") or [])
    out["race_result_list"] = copy.deepcopy(entry.get("race_result_list") or [])
    return out


def _user_info(viewer_id) -> dict:
    ui = state_store.extract_json_path(str(viewer_id), "load_index", "$.data.user_info")
    return ui if isinstance(ui, dict) else {}


def _entry(me, other, card: dict, friend_state: int):
    ui = _user_info(other)
    if not ui:
        return None
    partners = [t for t in _slots(ui.get("partner_chara_id_array")) if t]
    roster = {c.get("trained_chara_id"): c
              for c in (state_store.get_state(other) or {}).get(trained_chara.ROSTER_KEY) or []}
    horses = [wire_trained_chara(other, roster[t]) for t in partners if t in roster]
    base = directory.summary(other, card, friend_state)
    summary = {k: base.get(k) for k in _SUMMARY_FIELDS}
    summary["support_card_id"] = int(ui.get("support_card_id") or base.get("support_card_id") or 0)
    summary["support_card_id_array"] = _slots(ui.get("support_card_id_array")
                                              or [summary["support_card_id"]])
    summary["partner_chara_id"] = int(ui.get("partner_chara_id") or 0)
    summary["partner_chara_id_array"] = _slots(ui.get("partner_chara_id_array"))
    usc = base.get("user_support_card") or {}
    summary["user_support_card"] = {k: usc.get(k, 0) for k in
                                    ("support_card_id", "exp", "limit_break_count")}
    summary["user_trained_chara_array"] = [_digest(h) for h in horses]
    summary["friend_state"] = friend_state
    return summary, horses


def handle_get_succession_trained_chara(payload: dict) -> dict:
    me = payload["viewer_id"]
    followed = sorted(social.following(me), key=lambda v: (not str(v).isdigit(),
                                                           int(v) if str(v).isdigit() else 0, str(v)))
    cards = directory.cards_for(followed)
    users, horses = [], []
    for other in followed:
        card = cards.get(str(other))
        if card is None:
            continue
        state = social.friend_data(me, other)["state"]
        if state not in _FOLLOWING:
            continue
        entry = _entry(me, other, card, state)
        if entry is None:
            continue
        summary, theirs = entry
        users.append(summary)
        horses.extend(theirs)
    friend_info = None
    if payload.get("is_get_friend_info"):
        from ... import dispatch
        friend_index = dispatch.HANDLERS.get("friend/index") or dispatch.registry.get("friend/index")
        from .. import schema
        friend_info = schema.conform("friend/index", friend_index({"viewer_id": me}),
                                     True).get("data")
    return _ok({"succession_trained_chara_data": {"succession_trained_chara_array": horses,
                                                  "summary_user_info_array": users},
                "friend_load_info": friend_info})


def handle_change_practice_partner(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    raw = payload.get("trained_chara_id_array")
    if not isinstance(raw, list) or len(raw) != SLOTS:
        return refuse()
    picks = _slots(raw)
    chosen = [t for t in picks if t]
    owned = {c.get("trained_chara_id") for c in trained_chara._get_or_seed_roster(viewer_id)}
    if not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= owned:
        return refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = user_profile._load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["partner_chara_id_array"] = picks
    return user_profile._save_and_republish(viewer_id, full_state, user_info)


def handle_change_support_card(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    raw = payload.get("support_card_id_array")
    if not isinstance(raw, list) or len(raw) != SLOTS:
        return refuse()
    picks = _slots(raw)
    chosen = [c for c in picks if c]
    full_state = state_store.get_state(viewer_id) or {}
    owned = {c.get("support_card_id") for c in full_state.get(collection.SUPPORT_CARD_KEY) or []}
    if not picks[0] or len(set(chosen)) != len(chosen) or not set(chosen) <= owned:
        return refuse()
    user_info = user_profile._load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["support_card_id_array"] = picks
    user_info["support_card_id"] = picks[0]
    return user_profile._save_and_republish(viewer_id, full_state, user_info)


SPENT_KEY = "jp_career_skill_point_spent"
_CAREER = re.compile(r"^single_mode(_[a-z]+)?/(gain_skills|finish|start)$")


def _career_sp(full_state: dict):
    ci = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("chara_info") or {}
    sp = ci.get("skill_point")
    return int(sp) if isinstance(sp, int) else None


def before(endpoint: str, payload: dict):
    m = _CAREER.match(endpoint)
    if not m:
        return None
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    if m.group(2) == "gain_skills":
        return {"sp": _career_sp(full_state)}
    if m.group(2) == "finish":
        roster = full_state.get(trained_chara.ROSTER_KEY) or []
        return {"sp": _career_sp(full_state),
                "top": max((c.get("trained_chara_id") or 0 for c in roster), default=0)}
    return None


def after(endpoint: str, payload: dict, resp: dict, noted) -> None:
    m = _CAREER.match(endpoint)
    if not m or (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    action = m.group(2)
    if action == "start":
        full_state[SPENT_KEY] = 0
    elif action == "gain_skills":
        held, now = (noted or {}).get("sp"), _career_sp(full_state)
        if held is None or now is None or SPENT_KEY not in full_state:
            return
        full_state[SPENT_KEY] += max(0, held - now)
    elif action == "finish":
        spent = full_state.pop(SPENT_KEY, None)
        left = (noted or {}).get("sp")
        if spent is not None and left is not None:
            top = (noted or {}).get("top", 0)
            for c in full_state.get(trained_chara.ROSTER_KEY) or []:
                if (c.get("trained_chara_id") or 0) > top:
                    c["total_skill_pt"] = spent + left
                    _restamp(resp.get("data"), c["trained_chara_id"], spent + left)
    state_store.save_state(viewer_id, full_state)


def _restamp(node, trained_chara_id: int, value: int) -> None:
    if isinstance(node, dict):
        if node.get("trained_chara_id") == trained_chara_id and "skill_array" in node:
            node["total_skill_pt"] = value
        for v in node.values():
            _restamp(v, trained_chara_id, value)
    elif isinstance(node, list):
        for v in node:
            _restamp(v, trained_chara_id, value)


def mark_unrecorded(node) -> None:
    if isinstance(node, dict):
        if "trained_chara_id" in node and "skill_array" in node and "support_card_list" in node:
            node.setdefault("total_skill_pt", NO_RECORD)
        for v in node.values():
            mark_unrecorded(v)
    elif isinstance(node, list):
        for v in node:
            mark_unrecorded(v)


HANDLERS = {
    "pre_single_mode/get_succession_trained_chara": handle_get_succession_trained_chara,
    "user/change_practice_partner": handle_change_practice_partner,
    "user/change_support_card": handle_change_support_card,
}
