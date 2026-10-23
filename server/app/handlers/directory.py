
from __future__ import annotations

import logging
import time

from .. import state as state_store
from .. import accounts, social

log = logging.getLogger("uma-server")

DIRECTORY_KEY = "social_directory_card"

PARTNER_KEY = "star_partner_chara_id"

CARD_VERSION = 5

_ZERO_TIME = "0000-00-00 00:00:00"

_DEFAULT_HONOR_ID = 100101

_TRAINED_CHARA_FIELDS = (
    "trained_chara_id", "card_id", "rank_score", "rank",
    "proper_ground_turf", "proper_ground_dirt",
    "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi",
    "proper_distance_short", "proper_distance_mile",
    "proper_distance_middle", "proper_distance_long",
    "rarity", "talent_level", "register_time",
)


def _epoch_to_stamp(epoch) -> str:
    if not epoch:
        return _ZERO_TIME
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch)))


def _summary_trained_chara(viewer_id, entry: dict) -> dict:
    out = {"viewer_id": social._wire_id(viewer_id)}
    for k in _TRAINED_CHARA_FIELDS:
        if k in entry:
            out[k] = entry[k]
    out["factor_info_array"] = entry.get("factor_info_array") or []
    return out


def _best_trained_chara(roster: list) -> dict | None:
    if not roster:
        return None
    from . import trained_chara
    genuine = [c for c in roster
               if trained_chara.PLAYER_CAREER_ID_BASE <= (c.get("trained_chara_id") or 0)]
    pool = genuine or roster
    return max(pool, key=lambda c: c.get("rank_score") or 0)


def _lead_support_card(support_cards: list, preferred_id: int) -> dict | None:
    if not support_cards:
        return None
    chosen = None
    if preferred_id:
        chosen = next((c for c in support_cards
                       if c.get("support_card_id") == preferred_id), None)
    if chosen is None:
        chosen = max(support_cards, key=lambda c: (c.get("limit_break_count") or 0,
                                                  c.get("exp") or 0))
    return chosen


def _partner_trained_chara(roster: list, partner_chara_id: int) -> dict | None:
    if partner_chara_id:
        chosen = next((c for c in roster
                       if c.get("trained_chara_id") == partner_chara_id), None)
        if chosen is not None:
            return chosen
    return _best_trained_chara(roster)


def archive_entries(roster: list, full_state: dict | None = None) -> list:
    from . import archive_directory
    horses = (archive_directory.pool(full_state, roster) if full_state is not None
              else [c for c in roster if archive_directory.eligible(c)])
    return archive_directory.top_per_card(None, horses)


def directory_level(full_state: dict, roster: list) -> int:
    from . import note_archive
    note = full_state.get(note_archive.NOTE_STATE_KEY) or {}
    stored = note.get("level")
    if stored:
        return stored
    try:
        summary = note_archive._score_summary(full_state, note, roster)
        return note_archive._rank_for(note_archive._total_score(summary))
    except Exception:
        log.exception("directory: archive level scoring failed")
        return 1


def build_card(viewer_id, full_state: dict | None = None) -> dict | None:
    vid = str(viewer_id)
    if full_state is not None and "load_index" in full_state:
        user_info = (full_state["load_index"].get("data") or {}).get("user_info")
    else:
        user_info = state_store.extract_json_path(vid, "load_index", "$.data.user_info")
    if not isinstance(user_info, dict):
        return None

    if full_state is None:
        full_state = state_store.get_state(vid) or {}

    from . import collection, team_stadium, trained_chara

    roster = full_state.get(trained_chara.ROSTER_KEY) or []
    chosen_partner_id = full_state.get(PARTNER_KEY) or 0
    best = _partner_trained_chara(roster, chosen_partner_id)
    cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
    chosen_card_id = user_info.get("support_card_id") or 0
    lead = _lead_support_card(cards, chosen_card_id)

    ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    acct = accounts.get_account(vid) or {}

    play_count = sum(1 for c in roster
                     if trained_chara.PLAYER_CAREER_ID_BASE
                     <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE)

    honor_id = user_info.get("honor_id") or _DEFAULT_HONOR_ID
    card = {
        "version": CARD_VERSION,
        "viewer_id": social._wire_id(vid),
        "name": user_info.get("name") or f"Trainer{vid[-4:]}",
        "honor_id": honor_id,
        "honor_data": {"honor_id": honor_id},
        "last_login_time": _epoch_to_stamp(acct.get("last_seen"))
        if acct.get("last_seen") else (user_info.get("update_time")
                                       or user_info.get("create_time") or _ZERO_TIME),
        "leader_chara_id": user_info.get("leader_chara_id") or 0,
        "leader_chara_dress_id": user_info.get("leader_chara_dress_id") or 0,
        "support_card_id": user_info.get("support_card_id") or 0,
        "partner_chara_id": user_info.get("partner_chara_id") or 0,
        "comment": user_info.get("comment") or "",
        "fan": user_info.get("fan") or 0,
        "rank_score": user_info.get("rank_score") or 0,
        "team_stadium_win_count": 0,
        "single_mode_play_count": play_count,
        "team_evaluation_point": ts_state.get("team_evaluation_point")
        or user_info.get("best_team_evaluation_point") or 0,
        "best_team_evaluation_point": user_info.get("best_team_evaluation_point") or 0,
        "directory_level": directory_level(full_state, roster),
    }
    if lead is not None:
        card["support_card_id"] = lead.get("support_card_id") or card["support_card_id"]
        card["user_support_card"] = {
            "viewer_id": social._wire_id(vid),
            "support_card_id": lead.get("support_card_id"),
            "exp": lead.get("exp") or 0,
            "limit_break_count": lead.get("limit_break_count") or 0,
            "favorite_flag": int(lead.get("support_card_id") == chosen_card_id),
            "stock": 0,
            "possess_time": card["last_login_time"],
            "create_time": card["last_login_time"],
        }
    if "support_card_id_array" in user_info:
        card["support_card_id_array"] = list(user_info["support_card_id_array"])
    if "partner_chara_id_array" in user_info:
        by_id = {c.get("trained_chara_id"): c for c in roster}
        card["partner_chara_id_array"] = list(user_info["partner_chara_id_array"])
        card["user_trained_chara_array"] = [
            dict(_summary_trained_chara(vid, by_id[t]),
                 skill_count=len(by_id[t].get("skill_array") or []),
                 factor_extend_array=by_id[t].get("factor_extend_array") or [],
                 race_result_list=by_id[t].get("race_result_list") or [])
            for t in card["partner_chara_id_array"] if t in by_id]
    if best is not None:
        card["user_trained_chara"] = _summary_trained_chara(vid, best)
        card["partner_chara_id"] = (best.get("trained_chara_id")
                                    or card["partner_chara_id"])
    return card


def publish(viewer_id, full_state: dict) -> bool:
    card = build_card(viewer_id, full_state)
    if card is None:
        return False
    if full_state.get(DIRECTORY_KEY) == card:
        return False
    full_state[DIRECTORY_KEY] = card
    return True


def _publish_now(viewer_id) -> dict | None:
    st = state_store.get_state(viewer_id)
    if st is None:
        return None
    card = build_card(viewer_id, st)
    if card is None:
        return None
    st[DIRECTORY_KEY] = card
    state_store.save_state(viewer_id, st)
    return card


def cards_for(viewer_ids) -> dict:
    ids = [str(v) for v in viewer_ids]
    if not ids:
        return {}
    found = state_store.all_states_for_key(DIRECTORY_KEY, ids)
    out = {}
    for vid in ids:
        card = found.get(vid)
        if not isinstance(card, dict) or card.get("version") != CARD_VERSION:
            try:
                card = _publish_now(vid)
            except Exception:
                log.exception("directory: failed to build card for %s", vid)
                card = None
        if card:
            out[vid] = card
    return out


def all_cards(exclude=None) -> dict:
    cards = state_store.all_states_for_key(DIRECTORY_KEY)
    me = str(exclude) if exclude is not None else None
    return {vid: c for vid, c in cards.items()
            if vid != me and isinstance(c, dict) and c.get("version") == CARD_VERSION}


def summary(viewer_id, viewer_card: dict, friend_state: int = 0) -> dict:
    out = {k: v for k, v in viewer_card.items() if k != "version"}
    out["friend_state"] = friend_state
    out["state"] = friend_state
    circle = social.circle_of(viewer_id)
    member = social.member_row(viewer_id) if circle is not None else None
    if circle is not None and member is not None:
        out["circle_info"] = {"circle_id": circle["circle_id"], "name": circle["name"]}
        out["circle_user"] = member
    else:
        out["circle_info"] = None
        out["circle_user"] = None
    return out


def follower_summary(viewer_id, viewer_card: dict) -> dict:
    out = {
        "viewer_id": viewer_card["viewer_id"],
        "honor_id": viewer_card["honor_id"],
        "honor_data": viewer_card["honor_data"],
        "name": viewer_card["name"],
        "last_login_time": viewer_card["last_login_time"],
        "support_card_id": viewer_card.get("support_card_id") or 0,
    }
    if "user_support_card" in viewer_card:
        out["user_support_card"] = viewer_card["user_support_card"]
    return out
