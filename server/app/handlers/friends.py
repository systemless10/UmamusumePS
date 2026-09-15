"""The serverwide follow/friend system.

  friend/follow                   -> start following another real account
  friend/un_follow                -> stop following them
  friend/un_follower              -> drop somebody who follows ME
  friend/simple_search            -> look one account up by trainer id
  friend/renew_recommend_list     -> reroll the "people you might know" list
  friend/load                     -> one account's full public profile
  friend/get_team_stadium_team_data -> a friend's fielded Team Trials squad

friend/index and friend/search live in user_profile.py (they predate this
module and are documented there); both now read this module's graph instead
of serving empty. Everything stateful is in social.py -- see its docstring
for why the edges cannot live in per-viewer state.

THE MODEL, IN ONE LINE
----------------------
There is no separate "friend request". Following is unilateral and instant;
a mutual pair is what the game calls a friend (state 3). This is not an
approximation -- captures/20260907_204921/0023_friend_index.json shows an
account with 69 one-way follows, 17 one-way followers and 1 mutual pair, all
in a single friend_list, which is only possible if follow is one-way and
friendship is emergent. See social.py.

WHAT "SERVERWIDE" CHANGED
-------------------------
user_profile.py's friend/index docstring correctly refused to fabricate other
players, because there were none. There are ~200 real accounts on this
server, so the honest list is now non-empty, and every entry in it is a real
account's own persisted state (handlers/directory.py). Nothing here invents a
player; accounts that have never actually played are excluded rather than
padded.
"""

from __future__ import annotations

import logging
import random

from .. import social
from .. import state as state_store
from . import directory, registry

log = logging.getLogger("uma-server")

# How many "people you might know" entries a refresh offers. The real capture
# carried exactly 30.
RECOMMEND_SIZE = 30
# user_info_summary_list was capped at 100 in the real capture (87 friends +
# 30 recommends would have been 117), so the same ceiling applies here.
SUMMARY_CAP = 100

RECOMMEND_KEY = "friend_recommend_list"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _target(payload: dict):
    """Every one of these endpoints names the other account with the same
    field, and the client sends it as an int while state.py keys viewers by
    string -- normalize once, here, rather than in seven handlers."""
    for key in ("friend_viewer_id", "target_viewer_id", "viewer_id_to"):
        if payload.get(key):
            return str(payload[key])
    return None


# --------------------------------------------------------------- recommends


def _recommend_ids(viewer_id, full_state: dict, reroll: bool = False) -> list:
    """The stored "people you might know" set, rerolled on demand.

    Persisted rather than recomputed per call so the list does not reshuffle
    every time the player opens the screen -- friend/renew_recommend_list
    exists precisely because rerolling is meant to be an explicit action.
    Anyone the player has since followed (or who now follows them) is filtered
    out on read, so a stale stored list never re-offers an existing friend.
    """
    me = str(viewer_id)
    stored = full_state.get(RECOMMEND_KEY)
    known = set(social.following(me)) | set(social.followers(me)) | {me}
    if reroll or not isinstance(stored, list):
        pool = [vid for vid in directory.all_cards(exclude=me) if vid not in known]
        # Random rather than "strongest first": a recommend list ordered by
        # rank_score would show the same handful of top accounts to everyone
        # forever, and nobody else would ever be discoverable.
        random.shuffle(pool)
        stored = pool[:RECOMMEND_SIZE]
        full_state[RECOMMEND_KEY] = stored
    return [vid for vid in stored if vid not in known]


# ------------------------------------------------------------ friend/index
# Response shape (captures/20260907_204921/0023_friend_index.json, real
# server): {last_friend_checked_time, friend_list, recommend_list,
# user_info_summary_list, follower_info_summary_list, follower_num}.
#
# recommend_list is NOT a list of profiles -- it carries the same relation
# stubs friend_list does ({friend_viewer_id, state: 0, follow_time: "",
# follower_time: ""}, with EMPTY STRINGS rather than the zero timestamp).
# Every profile for both lists comes from the single user_info_summary_list.


def build_index(viewer_id, full_state: dict, injected: list) -> dict:
    """friend/index's data payload. Shared with user_profile.handle_friend_index,
    which owns the endpoint registration and the last_friend_checked_time
    bookkeeping; `injected` is its synthetic borrow-card lenders, which are
    NOT real accounts and so are appended rather than resolved through the
    directory."""
    me = str(viewer_id)
    # Cheap and idempotent, and repeated here rather than left to load/index
    # alone so an account that has not logged in since this feature shipped
    # still gets its lenders the moment it opens the friend screen.
    from . import house_lenders
    house_lenders.ensure_accounts()
    house_lenders.ensure_followed(me)

    friends = social.friend_list(me)
    followers = social.followers(me)
    recommends = _recommend_ids(me, full_state)

    # One bulk query for everyone shown, friends and recommends together.
    wanted = [str(e["friend_viewer_id"]) for e in friends] + recommends
    cards = directory.cards_for(wanted)

    # A followed account with no directory card (never actually played) still
    # belongs in friend_list -- the edge is real -- but has no profile to put
    # in the summary list. The client tolerates that; it is the same
    # relation-without-profile case recommend_list is built on.
    state_by_id = {str(e["friend_viewer_id"]): e["state"] for e in friends}
    summaries = [directory.summary(vid, cards[vid], state_by_id[vid])
                 for vid in (str(e["friend_viewer_id"]) for e in friends)
                 if vid in cards]
    summaries += [directory.summary(vid, cards[vid], social.STATE_NONE)
                  for vid in recommends if vid in cards]
    # The house lenders now exist as REAL accounts at the SAME viewer_ids
    # their legacy synthetic entries use (house_lenders.py), so an account
    # seeded before that change carries both -- the real follow edge AND the
    # old injected copy. Showing both would list every lender twice. The real
    # edge wins; only injected entries with no real edge behind them (an
    # admin's own hand-added `add-friend-card` lender, say) still appear.
    already = {str(e["friend_viewer_id"]) for e in friends}
    injected = [e for e in injected if str(e["viewer_id"]) not in already]
    summaries += [dict(e) for e in injected]

    return {
        "friend_list": friends + [
            {"friend_viewer_id": e["viewer_id"], "state": social.STATE_FOLLOW,
             "follow_time": e["last_login_time"], "follower_time": e["last_login_time"]}
            for e in injected],
        # Empty strings, not ZERO_TIME -- that is what the real server sends
        # for a relation that does not exist at all.
        "recommend_list": [{"friend_viewer_id": social._wire_id(vid), "state": social.STATE_NONE,
                            "follow_time": "", "follower_time": ""} for vid in recommends],
        "user_info_summary_list": summaries[:SUMMARY_CAP],
        "follower_info_summary_list": [
            directory.follower_summary(vid, cards[vid])
            for vid in followers if vid in cards
            and state_by_id.get(vid) == social.STATE_FOLLOWER],
        "follower_num": len(followers),
    }


# ----------------------------------------------------- follow / un_follow
# Capture ground truth (captures/20260907_204921/0028_friend_follow.json and
# 0026_friend_un_follow.json, real server): request {friend_viewer_id};
# response data {friend_data: {friend_viewer_id, state, follow_time,
# follower_time}} and NOTHING else. un_follow's reply carries state 0 with the
# ORIGINAL follow_time preserved -- social.py models that directly.


@registry.endpoint("friend/follow")
def handle_follow(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    # Only real accounts on this server can be followed. Refusing an unknown
    # id is what keeps the graph honest: an edge to a viewer_id with no state
    # would render as a blank friend forever.
    if state_store.get_state(target) is None:
        return _refuse()
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
    """Drop a FOLLOWER: deactivates their edge to me, never mine to them. A
    mutual pair therefore becomes state 1 (I still follow them), not state 0
    -- removing a follower is not supposed to also unfollow them."""
    target = _target(payload)
    if target is None:
        return _refuse()
    return _ok({"friend_data": social.remove_follower(payload["viewer_id"], target)})


@registry.endpoint("friend/un_follow_multi")
def handle_un_follow_multi(payload: dict) -> dict:
    """The bulk form of un_follow (JP-only in the glossary, but harmless to
    serve on both). Request carries an array of ids under whichever of the two
    plausible names the client uses; both are accepted rather than guessing."""
    me = payload["viewer_id"]
    ids = payload.get("friend_viewer_id_array") or payload.get("viewer_id_array") or []
    return _ok({"friend_data_array": [social.unfollow(me, t) for t in ids]})


@registry.endpoint("friend/un_follower_multi")
def handle_un_follower_multi(payload: dict) -> dict:
    me = payload["viewer_id"]
    ids = payload.get("friend_viewer_id_array") or payload.get("viewer_id_array") or []
    return _ok({"friend_data_array": [social.remove_follower(me, t) for t in ids]})


# ------------------------------------------------- friend/renew_recommend_list


@registry.endpoint("friend/renew_recommend_list")
def handle_renew_recommend_list(payload: dict) -> dict:
    """Reroll the suggestion list. Returns the same two lists friend/index
    serves for it, so the client can swap them in without a full refresh."""
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


# ------------------------------------------------------ friend/simple_search
# The "add by trainer ID" box: one id in, that account's summary out. Distinct
# from friend/search, which is the rich Trainer-Info popup for someone already
# on a list (user_profile.py owns that one).


@registry.endpoint("friend/simple_search")
def handle_simple_search(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    cards = directory.cards_for([target])
    card = cards.get(target)
    if card is None:
        # No such account, or one that has never played. Refusing is what
        # makes the client show "trainer not found" instead of an empty card.
        return _refuse()
    rel = social.friend_data(viewer_id, target)
    return _ok({
        "friend_info": rel,
        "user_info_summary": directory.summary(target, card, rel["state"]),
        "user_info_summary_list": [directory.summary(target, card, rel["state"])],
    })


# ------------------------------------------------------------- friend/load
# Never captured (glossary: DUMP on both regions). Serves the same profile
# payload friend/search does, which is the only shape this project has real
# evidence for -- user_profile.handle_friend_search owns that builder, so this
# delegates to it rather than inventing a second, divergent one.


@registry.endpoint("friend/load")
def handle_load(payload: dict) -> dict:
    from . import user_profile
    return user_profile.handle_friend_search(payload)


# ------------------------------------------- friend/get_team_stadium_team_data
# A friend's fielded Team Trials squad, for the "view their team" button.
# Never captured; the response reuses team_stadium.py's OWN wire builders
# (the same ones that already render an opponent's team on a screen this
# client is known to display correctly) rather than a new hand-built shape.


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
        # They have never fielded a team. Empty arrays, not a refusal: the
        # account exists and the screen should open showing nothing.
        return _ok({"team_data_array": [], "trained_chara_array": [],
                    "team_evaluation_point": 0})
    roster = st.get(trained_chara.ROSTER_KEY) or []
    return _ok({
        "team_data_array": team_stadium._wire_team_data_array(team),
        "trained_chara_array": team_stadium._wire_trained_chara_array(team, roster),
        "team_evaluation_point": ts.get("team_evaluation_point") or 0,
    })
