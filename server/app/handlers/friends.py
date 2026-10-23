
from __future__ import annotations

import logging
import random

from .. import social
from .. import state as state_store
from . import directory, registry
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

RECOMMEND_SIZE = 30
SUMMARY_CAP = 100

RECOMMEND_KEY = "friend_recommend_list"

MAX_FOLLOW = 30
MAX_FOLLOWER = 1000


def _follow_cap(viewer_id) -> int:
    from . import load, shop
    full_state = state_store.get_state(viewer_id) or {}
    bonus = full_state.get(shop.BONUS_FOLLOW_KEY)
    if not bonus:
        data = load.get_or_seed_data(full_state, viewer_id)
        bonus = (data.get("user_info") or {}).get("bonus_follow_num")
    return MAX_FOLLOW + int(bonus or 0)


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _target(payload: dict):
    for key in ("friend_viewer_id", "target_viewer_id", "viewer_id_to"):
        if payload.get(key):
            return str(payload[key])
    return None


def _recommend_ids(viewer_id, full_state: dict, reroll: bool = False) -> list:
    me = str(viewer_id)
    stored = full_state.get(RECOMMEND_KEY)
    known = set(social.following(me)) | set(social.followers(me)) | {me}
    if reroll or not isinstance(stored, list):
        pool = [vid for vid in directory.all_cards(exclude=me) if vid not in known]
        random.shuffle(pool)
        stored = pool[:RECOMMEND_SIZE]
        full_state[RECOMMEND_KEY] = stored
    return [vid for vid in stored if vid not in known]


def build_index(viewer_id, full_state: dict, injected: list) -> dict:
    me = str(viewer_id)
    from . import house_lenders
    house_lenders.ensure_accounts()
    house_lenders.ensure_followed(me)

    friends = social.friend_list(me)
    followers = social.followers(me)
    recommends = _recommend_ids(me, full_state)

    wanted = [str(e["friend_viewer_id"]) for e in friends] + recommends
    cards = directory.cards_for(wanted)

    state_by_id = {str(e["friend_viewer_id"]): e["state"] for e in friends}
    summaries = [directory.summary(vid, cards[vid], state_by_id[vid])
                 for vid in (str(e["friend_viewer_id"]) for e in friends)
                 if vid in cards]
    summaries += [directory.summary(vid, cards[vid], social.STATE_NONE)
                  for vid in recommends if vid in cards]
    already = {str(e["friend_viewer_id"]) for e in friends}
    injected = [e for e in injected if str(e["viewer_id"]) not in already]
    summaries += [dict(e) for e in injected]

    return {
        "friend_list": friends + [
            {"friend_viewer_id": e["viewer_id"], "state": social.STATE_FOLLOW,
             "follow_time": e["last_login_time"], "follower_time": e["last_login_time"]}
            for e in injected],
        "recommend_list": [{"friend_viewer_id": social._wire_id(vid), "state": social.STATE_NONE,
                            "follow_time": "", "follower_time": ""} for vid in recommends],
        "user_info_summary_list": summaries[:SUMMARY_CAP],
        "follower_info_summary_list": [
            directory.follower_summary(vid, cards[vid])
            for vid in followers if vid in cards
            and state_by_id.get(vid) == social.STATE_FOLLOWER],
        "follower_num": len(followers),
    }


@registry.endpoint("friend/follow")
def handle_follow(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    if state_store.get_state(target) is None:
        return _refuse(ResultCode.SEARCH_USER_NOT_FOUND)
    if target not in social.following(viewer_id):
        if social.follow_num(viewer_id) >= _follow_cap(viewer_id):
            return _refuse(ResultCode.FRIEND_FOLLOW_COUNT_OVER_ERROR)
        if social.follower_num(target) >= MAX_FOLLOWER:
            return _refuse(ResultCode.FRIEND_FOLLOW_USER_FOLLOW_COUNT_OVER_ERROR)
    try:
        data = social.follow(viewer_id, target)
    except ValueError:
        return _refuse()
    return _ok({"friend_data": data})


@registry.endpoint("friend/un_follow")
def handle_un_follow(payload: dict) -> dict:
    target = _target(payload)
    if target is None:
        return _refuse()
    return _ok({"friend_data": social.unfollow(payload["viewer_id"], target)})


@registry.endpoint("friend/un_follower")
def handle_un_follower(payload: dict) -> dict:
    target = _target(payload)
    if target is None:
        return _refuse()
    return _ok({"friend_data": social.remove_follower(payload["viewer_id"], target)})


@registry.endpoint("friend/un_follow_multi")
def handle_un_follow_multi(payload: dict) -> dict:
    me = payload["viewer_id"]
    ids = payload.get("friend_viewer_id_array") or payload.get("viewer_id_array") or []
    return _ok({"friend_data_array": [social.unfollow(me, t) for t in ids]})


@registry.endpoint("friend/un_follower_multi")
def handle_un_follower_multi(payload: dict) -> dict:
    me = payload["viewer_id"]
    ids = payload.get("friend_viewer_id_array") or payload.get("viewer_id_array") or []
    return _ok({"friend_data_array": [social.remove_follower(me, t) for t in ids]})


@registry.endpoint("friend/renew_recommend_list")
def handle_renew_recommend_list(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    recommends = _recommend_ids(viewer_id, full_state, reroll=True)
    state_store.save_state(viewer_id, full_state)
    cards = directory.cards_for(recommends)
    return _ok({
        "recommend_list": [{"friend_viewer_id": social._wire_id(vid),
                            "state": social.STATE_NONE,
                            "follow_time": "", "follower_time": ""}
                           for vid in recommends],
        "user_info_summary_list": [directory.summary(vid, cards[vid], social.STATE_NONE)
                                   for vid in recommends if vid in cards],
    })


@registry.endpoint("friend/simple_search")
def handle_simple_search(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    cards = directory.cards_for([target])
    card = cards.get(target)
    if card is None:
        return _refuse(ResultCode.SEARCH_USER_NOT_FOUND)
    rel = social.friend_data(viewer_id, target)
    return _ok({
        "friend_info": rel,
        "user_info_summary": directory.summary(target, card, rel["state"]),
        "user_info_summary_list": [directory.summary(target, card, rel["state"])],
    })


@registry.endpoint("friend/load")
def handle_load(payload: dict) -> dict:
    from . import user_profile
    return user_profile.handle_friend_search(payload)


@registry.endpoint("friend/get_team_stadium_team_data")
def handle_get_team_stadium_team_data(payload: dict) -> dict:
    target = _target(payload)
    if target is None:
        return _refuse()
    from . import team_stadium, trained_chara
    st = state_store.get_state(target) or {}
    ts = st.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    team = ts.get("team_data_array") or []
    if not team:
        return _ok({"team_data_array": [], "trained_chara_array": [],
                    "team_evaluation_point": 0})
    roster = st.get(trained_chara.ROSTER_KEY) or []
    return _ok({
        "team_data_array": team_stadium._wire_team_data_array(team),
        "trained_chara_array": team_stadium._wire_trained_chara_array(team, roster),
        "team_evaluation_point": ts.get("team_evaluation_point") or 0,
    })
