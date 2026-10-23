
from __future__ import annotations

import logging

from .. import master_data, social
from .. import state as state_store
from . import collection, directory, trained_chara

log = logging.getLogger("uma-server")

STATE_MARKER_KEY = "house_lender"

_TEMPLATE_CHARA_ID = 1032
_TEMPLATE_DRESS_ID = 103201


def lender_viewer_id(index: int) -> int:
    from . import single_mode_team
    return single_mode_team._FRIEND_SENTINEL_VIEWER_BASE + index


def lender_ids() -> list:
    from . import single_mode_team
    return [str(lender_viewer_id(i))
            for i in range(len(single_mode_team._DEFAULT_FRIEND_CARDS))]


def _build_state(index: int, card_id: int, name: str) -> dict:
    from . import single_mode_team as smt

    vid = str(lender_viewer_id(index))
    stamp = social.now()
    row = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (card_id,))
    if row is None:
        raise ValueError(f"no support_card_data row for id {card_id}")
    top = master_data.query_one(
        "SELECT MAX(level) AS lv FROM support_card_level WHERE rarity=?", (row["rarity"],))
    level = (top["lv"] if top else None) or 1
    exp_row = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (row["rarity"], level))
    exp = exp_row["total_exp"] if exp_row else 0

    horse = {**smt._FRIEND_TEMPLATE_TRAINED_CHARA,
             "viewer_id": int(vid),
             "trained_chara_id": 9001 + index,
             "register_time": stamp}
    support_card = {
        "viewer_id": int(vid), "support_card_id": card_id, "exp": exp,
        "limit_break_count": smt._DEFAULT_FRIEND_LIMIT_BREAK,
        "favorite_flag": 0, "stock": 0,
        "possess_time": stamp, "create_time": stamp,
    }
    card = {
        "version": directory.CARD_VERSION,
        "viewer_id": int(vid),
        "name": name,
        "honor_id": 100101, "honor_data": {"honor_id": 100101},
        "last_login_time": stamp,
        "leader_chara_id": _TEMPLATE_CHARA_ID,
        "leader_chara_dress_id": _TEMPLATE_DRESS_ID,
        "support_card_id": card_id,
        "partner_chara_id": _TEMPLATE_CHARA_ID,
        "comment": "",
        "fan": 1, "rank_score": 1,
        "team_stadium_win_count": 0, "single_mode_play_count": 1,
        "team_evaluation_point": 0, "best_team_evaluation_point": 0,
        "directory_level": 1,
        "user_support_card": support_card,
        "user_trained_chara": directory._summary_trained_chara(vid, horse),
    }
    return {
        STATE_MARKER_KEY: {"index": index, "support_card_id": card_id, "name": name},
        directory.DIRECTORY_KEY: card,
        trained_chara.ROSTER_KEY: [horse],
        collection.SUPPORT_CARD_KEY: [support_card],
    }


def ensure_accounts() -> list:
    from . import single_mode_team

    ids = []
    for index, (card_id, name) in enumerate(single_mode_team._DEFAULT_FRIEND_CARDS):
        vid = str(lender_viewer_id(index))
        ids.append(vid)
        existing = state_store.get_state(vid)
        card = (existing or {}).get(directory.DIRECTORY_KEY)
        if (existing is not None and isinstance(card, dict)
                and card.get("version") == directory.CARD_VERSION
                and card.get("support_card_id") == card_id):
            continue
        try:
            fresh = _build_state(index, card_id, name)
        except Exception:
            log.exception("house_lenders: could not build lender for card %s", card_id)
            continue
        merged = dict(existing or {})
        merged.update(fresh)
        state_store.save_state(vid, merged)
    return ids


def ensure_followed(viewer_id) -> None:
    me = str(viewer_id)
    if me in set(lender_ids()):
        return
    mine = social.following(me)
    theirs = social.followers(me)
    for vid in lender_ids():
        try:
            if vid not in mine:
                social.follow(me, vid)
            if vid not in theirs:
                social.follow(vid, me)
        except Exception:
            log.exception("house_lenders: could not link %s <-> %s", me, vid)
