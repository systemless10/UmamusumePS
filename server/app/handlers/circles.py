"""Circles (the guild system), serverwide across every account on this server.

  circle/make | update | break_up | detail | list | conditional_search
  circle/check_join | direct | room_enter | get_ranking_top
  circle/get_profile_card_info | set_profile_card_info | get_post_partner_data
  circle_chat/polling | send_message | send_stamp | post_partner
  circle_chat/send_item_request | invite_room_match
  circle_user/user_join_request | cancel_join_request | approve_join_request
  circle_user/decline_join_request | scout | cancel_scout | approve_scout
  circle_user/change_leader | change_sub_leader | kick | leave | checked_request
  circle_user/get_profile | set_profile | get_profile_card_info | set_profile_card_info
  circle_item_request/get_request_data | donate | donate_multiple | receive

All shared state lives in social.py (see its docstring for why a guild cannot
live in per-viewer state, and for the membership/join_style encodings). This
module is the wire layer: it decides who is ALLOWED to do a thing, then calls
social.py to do it, then renders the room.

EVIDENCE
--------
The response shapes come from captures/20260818_122351/, taken against the
real server on a real 30-member circle:

  0034_circle_room_enter          the whole room payload, 19 top-level keys
  0036_circle_chat_polling        the 7-key polling subset of it
  0039_circle_chat_send_message   message_type 1, text in `message_data`
  0037_circle_chat_send_stamp     message_type 2, stamp id in `message_id`
  0042_circle_item_request_get_request_data
  0043_circle_item_request_donate_multiple
  0035_circle_item_request_receive

The management endpoints (make/update/break_up/scout/kick/...) were never
captured -- but they are not guesswork either: every response shape below is
the client's own compiled Circle*Response.CommonResponse class, read out of
dump.cs (Documents/game-dumps/dump.cs). Those classes are the exact and
complete set of fields this client deserializes: a field it does not declare
is skipped, and a field it declares but we omit silently becomes null/0.

That last case is what made circle creation appear to do nothing (reported
live 2026-09-08: "when I try making a club, the club doesnt make and it just
sends me back to search club screen"). The circle WAS created -- the row is
in social.sqlite3 -- but circle/make used to answer with the whole room
payload, and CircleMakeResponse{circle_info, circle_user} has no
circle_user_array: the one field naming the CALLER's own membership came back
null, so the client had no membership to open the circle screen with and fell
back to the browse list. circle/check_join had the same bug in its purest
form -- the client reads ONE boolean, is_join_circle, and this answered with
is_joined/can_join/..., so it read false and kept routing to the search
screen. (That is what the glossary's "AMBIGUOUS FALSE" note on check_join was
actually recording.)

So every handler below returns EXACTLY its dump.cs class's field set. Where a
capture also exists (room_enter, chat polling, the item requests) the capture
and the dump agree, which is the cross-check that the dump is being read
right.

TWO RECRUITMENT FLOWS
---------------------
They are symmetric and both land in social.circle_pending:

  player applies  -> circle_user/user_join_request  -> leader approves
                     (approve_join_request) or declines (decline_join_request);
                     the player can withdraw (cancel_join_request)
  circle scouts   -> circle_user/scout              -> player accepts
                     (approve_scout); either side can cancel (cancel_scout)

Approving either one calls social.add_member, which settles every other
pending row for that player in one go -- so a player who applied to three
circles and got into one is no longer waiting on the other two.
"""

from __future__ import annotations

import logging
import time

from .. import social
from .. import state as state_store
from . import directory, registry

log = logging.getLogger("uma-server")

# Real capture: chat_polling_interval 3 (seconds).
CHAT_POLLING_INTERVAL = 3
# Real capture: 100 messages in room_enter's backlog.
CHAT_BACKLOG = 100

MAX_NAME_LEN = 20               # server-defined bounds, same spirit as
MAX_COMMENT_LEN = 200           # user_profile.py's own name/comment caps
# Real capture, load/index common_define: max_circle_scout_num 30.
MAX_SCOUT = 30


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    """result_code 205 -- the same refusal user_profile.py uses. The client
    shows its generic "could not do that" rather than hanging."""
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


# ------------------------------------------------------------- permissions


def _my_circle(viewer_id):
    """(circle, member_row) for the caller, or (None, None) if unaffiliated."""
    member = social.member_row(viewer_id)
    if member is None:
        return None, None
    return social.get_circle(member["circle_id"]), member


def _is_leader(circle, viewer_id) -> bool:
    return circle is not None and str(circle["leader_viewer_id"]) == str(viewer_id)


def _can_manage(circle, member) -> bool:
    """Leader OR sub-leader: who may accept applications, scout, and kick.
    Leadership transfer and disbanding are leader-only and check _is_leader
    directly -- a sub-leader must not be able to remove the leader."""
    return member is not None and member["membership"] >= social.MEMBERSHIP_SUB_LEADER


# ------------------------------------------------------------ room payload
# The single response builder. room_enter serves it whole; chat polling serves
# the 7-key subset the polling capture showed; every uncaptured management
# endpoint serves it whole too (see the module docstring).


def _chat_user_array(viewer_ids) -> list:
    """circle_chat_user_array -- the sender identities the chat log renders
    with. Exactly four fields in the real capture ({viewer_id,
    leader_chara_id, leader_chara_dress_id, name}), sourced from each member's
    published directory card."""
    cards = directory.cards_for(viewer_ids)
    return [{"viewer_id": c["viewer_id"], "leader_chara_id": c.get("leader_chara_id") or 0,
             "leader_chara_dress_id": c.get("leader_chara_dress_id") or 0,
             "name": c.get("name") or ""}
            for c in (cards[v] for v in viewer_ids if v in cards)]


def _post_partner_array(circle_id) -> list:
    """circle_post_partner_array -- members' offered practice partners. The
    real entry wraps a FULL trained_chara under practice_partner_info (not the
    trimmed summary form), so each member's strongest horse is sent whole."""
    from . import trained_chara
    out = []
    ids = social.member_ids(circle_id)
    rosters = state_store.all_states_for_key(trained_chara.ROSTER_KEY, ids)
    for vid in ids:
        best = directory._best_trained_chara(rosters.get(vid) or [])
        if best is not None:
            # dump.cs CirclePostPartner{practice_partner_info, post_comment_id,
            # post_time}. There is no comment picker wired up here, so the
            # comment id is 0 -- "no canned message chosen" -- rather than an
            # arbitrary one.
            out.append({"practice_partner_info": dict(best),
                        "post_comment_id": 0, "post_time": social.now()})
    return out


def _room_payload(viewer_id, circle, member, full: bool = True) -> dict:
    """The room screen's data. `full=False` gives the polling subset."""
    cid = circle["circle_id"]
    after = (member or {}).get("last_check_post_id") or 0 if not full else 0
    messages = social.chat_since(cid, after_post_id=after, limit=CHAT_BACKLOG)
    senders = sorted({str(m["viewer_id"]) for m in messages})

    payload = {
        "circle_chat_message_array": messages,
        "circle_chat_user_array": _chat_user_array(senders),
        "circle_item_request_array": social.requests_for_circle(cid),
        "circle_item_donate_array": social.donations_for_circle(cid),
        # Room Match (the co-op race lobby) is a separate feature this server
        # does not implement. Empty arrays are what the real capture itself
        # carried for a circle with no lobby open, so this is the accurate
        # answer here rather than a placeholder.
        "room_match_info_array": [],
        "circle_post_partner_array": _post_partner_array(cid),
        "chat_polling_interval": CHAT_POLLING_INTERVAL,
    }
    if not full:
        return payload

    # circle_chat_user_array belongs to the POLLING payload only. The real
    # room_enter capture has 19 top-level keys and this is not among them --
    # the room screen gets its names from summary_user_info_array, which
    # covers every member and which polling does not carry. Keeping it here
    # too would be one field wider than the shape this client was observed
    # accepting, for no gain.
    payload.pop("circle_chat_user_array", None)

    rank, point, last_point = social.circle_rank(cid)
    members = social.members(cid)
    member_ids = [str(m["viewer_id"]) for m in members]
    cards = directory.cards_for(member_ids)
    payload.update({
        "circle_info": circle,
        "circle_user_array": members,
        "summary_user_info_array": [
            directory.summary(vid, cards[vid],
                              social.friend_data(viewer_id, vid)["state"])
            for vid in member_ids if vid in cards],
        # change_leader: whether the CLIENT should offer the hand-over UI.
        "change_leader": _is_leader(circle, viewer_id),
        "is_scout_able": _can_manage(circle, member)
        and circle["member_num"] < social.MAX_CIRCLE_MEMBERS,
        # No monthly ranking calculation runs on this server, so there is
        # never an unseen result to announce and never a calculation in
        # progress. Both were false in the real capture too.
        "is_show_ranking_result": False,
        "is_calculate": False,
        "daily_donated_count": social.donated_today(viewer_id),
        "daily_post_partner_count": 0,
        "circle_ranking_this_month": {"circle_id": cid, "monthly": _monthly(),
                                      "rank": rank, "point": point},
        "circle_ranking_last_month": {"circle_id": cid, "monthly": _monthly(-1),
                                      "rank": rank, "point": last_point},
        # room_info_array/room_user_array belong to Room Match, like
        # room_match_info_array above.
        "room_info_array": [],
        "room_user_array": [],
    })
    return payload


def _monthly(offset: int = 0) -> int:
    """The YYYYMM integer circle_ranking_* is keyed by (real capture: 202608
    for this month, 202607 for last)."""
    import time
    t = time.localtime()
    y, m = t.tm_year, t.tm_mon + offset
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y * 100 + m


def _room(viewer_id) -> dict:
    """The caller's own room, or a refusal if they are in no circle."""
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    return _ok(_room_payload(viewer_id, circle, member))


# ------------------------------------------------------- shared sub-shapes
# CircleUser / CircleRequest / CircleScout / CircleRanking as dump.cs declares
# them. social.py stores each with only the columns it needs, so the wire form
# is assembled here rather than leaking half-rows to the client.


def no_circle_user(viewer_id) -> dict:
    """The CircleUser of a player in NO circle. Not an invention: this is the
    exact block real load/index sends for an unaffiliated account (see
    captures/20260816_140811/0005_load_index.json) -- circle_id 0,
    membership 0, rather than a null the client would have to special-case."""
    return {"viewer_id": int(viewer_id), "circle_id": 0, "membership": 0,
            "join_time": social.now(), "penalty_end_time": "0000-00-00 00:00:00",
            "item_request_end_time": "0000-00-00 00:00:00",
            "last_check_post_id": 0}


def _ranking(cid, last: bool = False) -> dict:
    """CircleRanking{circle_id, point, monthly, rank}."""
    rank, point, last_point = social.circle_rank(cid)
    return {"circle_id": cid, "monthly": _monthly(-1 if last else 0),
            "rank": rank, "point": last_point if last else point}


def _request_rows(circle_id) -> list:
    """CircleRequest[]{circle_id, viewer_id, update_time} -- who applied."""
    return [{"circle_id": circle_id, "viewer_id": p["viewer_id"],
             "update_time": p["create_time"]}
            for p in social.pending_for_circle(circle_id, "request")]


def _scout_rows(circle_id) -> list:
    """CircleScout[]{circle_id, viewer_id} -- who this circle invited."""
    return [{"circle_id": circle_id, "viewer_id": p["viewer_id"]}
            for p in social.pending_for_circle(circle_id, "scout")]


def _my_request(viewer_id) -> dict:
    """CircleListResponse's SINGULAR circle_request: the one application this
    player has outstanding, which is what makes the browse row read "Applied".
    Empty when they have none -- the field is one object, not an array."""
    mine = social.pending_for_viewer(viewer_id, "request")
    if not mine:
        return {}
    p = mine[0]
    return {"circle_id": p["circle_id"], "viewer_id": int(viewer_id),
            "update_time": p["create_time"]}


def _my_scouts(viewer_id) -> list:
    """CircleScout[] aimed AT this player -- their pending invitations."""
    return [{"circle_id": p["circle_id"], "viewer_id": int(viewer_id)}
            for p in social.pending_for_viewer(viewer_id, "scout")]


# ------------------------------------------------------------ circle/make


@registry.endpoint("circle/make")
def handle_make(payload: dict) -> dict:
    """CircleMakeRequest{name, comment, join_style, policy}. The creator
    becomes leader and first member."""
    viewer_id = payload["viewer_id"]
    name = (payload.get("name") or "").strip()[:MAX_NAME_LEN]
    if not name:
        return _refuse()
    try:
        circle = social.create_circle(
            viewer_id, name,
            comment=(payload.get("comment") or "")[:MAX_COMMENT_LEN],
            join_style=payload.get("join_style") or social.JOIN_OPEN,
            policy=payload.get("policy") or 0,
        )
    except ValueError:
        return _refuse()          # already in a circle
    # CircleMakeResponse{circle_info, circle_user}. circle_user is what the
    # client moves to the circle screen on; without it the creation is
    # invisible to the UI (see the module docstring).
    return _ok({"circle_info": circle,
                "circle_user": social.member_row(viewer_id) or {}})


@registry.endpoint("circle/update")
def handle_update(payload: dict) -> dict:
    """Edit name/comment/join_style/policy. Management right required -- a
    plain member must not be able to rename the circle."""
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    return _ok({"circle_info": _apply_update(circle["circle_id"], payload)})


def _apply_update(circle_id, payload: dict) -> dict:
    """The {name, comment, join_style, policy} edit, shared by circle/update
    and circle/set_profile_card_info -- whose request carries the same four
    fields alongside the card itself, so the settings screen saves both at
    once."""
    name = payload.get("name")
    return social.update_circle(
        circle_id,
        name=(name.strip()[:MAX_NAME_LEN] or None) if isinstance(name, str) else None,
        comment=(payload["comment"][:MAX_COMMENT_LEN]
                 if isinstance(payload.get("comment"), str) else None),
        join_style=payload.get("join_style"),
        policy=payload.get("policy"),
    )


@registry.endpoint("circle/break_up")
def handle_break_up(payload: dict) -> dict:
    """Dissolve the circle. LEADER ONLY -- deliberately stricter than
    _can_manage: a sub-leader being able to delete the guild out from under
    everyone is not a power the client's own UI offers."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse()
    social.break_up_circle(circle["circle_id"])
    # CircleBreakUpResponse{circle_user}: the caller's standing AFTER the
    # dissolve, which is "in no circle".
    return _ok({"circle_user": no_circle_user(viewer_id)})


# -------------------------------------------------- browse / detail / join


def _circle_detail(viewer_id, circle) -> dict:
    """The public view of a circle for someone who is not (yet) in it: its
    info, its roster, and the caller's own standing with it."""
    cid = circle["circle_id"]
    ids = social.member_ids(cid)
    cards = directory.cards_for(ids)
    return {
        "circle_info": circle,
        "circle_user_array": social.members(cid),
        # user_friend_array: the caller's OWN friendship standing with each
        # member, which is how the roster marks the people they already know.
        "user_friend_array": [social.friend_data(viewer_id, vid) for vid in ids],
        # Whether THIS viewer has something pending with this circle is not a
        # boolean here (that was invented): the client reads it off these two
        # arrays, which are also what a leader's approval queue renders from.
        "circle_request_array": _request_rows(cid),
        "circle_scout_array": _scout_rows(cid),
        "circle_ranking_this_month": _ranking(cid),
        "circle_ranking_last_month": _ranking(cid, last=True),
        "is_calculate": False,
        "summary_user_info_array": [
            directory.summary(vid, cards[vid],
                              social.friend_data(viewer_id, vid)["state"])
            for vid in ids if vid in cards],
    }


@registry.endpoint("circle/detail")
def handle_detail(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    cid = payload.get("circle_id")
    circle = social.get_circle(cid) if cid else social.circle_of(viewer_id)
    if circle is None:
        return _refuse()
    return _ok(_circle_detail(viewer_id, circle))


@registry.endpoint("circle/direct")
def handle_direct(payload: dict) -> dict:
    """NOT "open a circle by id" -- that reading was wrong. The request is
    empty and dump.cs gives CircleDirectResponse{reward_info_array, after_rank,
    best_team_evaluation_point}: this is the monthly circle-ranking result
    hand-out, claimed once the month's standings settle.

    No monthly ranking calculation runs on this server (see is_calculate,
    which is false everywhere for the same reason), so there is never a
    settled result to pay out. The honest answer is an empty reward list with
    the player's real current standing, not a fabricated payout.
    """
    from . import team_stadium
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    ts_state = (state_store.get_state(viewer_id) or {}).get(
        team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    return _ok({
        "reward_info_array": [],
        "after_rank": _ranking(circle["circle_id"])["rank"] if circle else 0,
        "best_team_evaluation_point": team_stadium.rank_display_point(ts_state),
    })


def _browse(viewer_id, circles) -> dict:
    """The shared body of circle/list, conditional_search and get_ranking_top.

    leader_info_array -- NOT summary_user_info_array, which is what this used
    to send. Both carry UserInfoAtFriend, but only the browse list's own field
    name reaches the client, so under the wrong name every row rendered with
    no leader at all.
    """
    return _ok({
        "circle_info_array": circles,
        "leader_info_array": _leader_summaries(viewer_id, circles),
        "circle_ranking_array": [_ranking(c["circle_id"]) for c in circles],
    })


def _leader_summaries(viewer_id, circles) -> list:
    leader_ids = [str(c["leader_viewer_id"]) for c in circles]
    cards = directory.cards_for(leader_ids)
    seen, out = set(), []
    for vid in leader_ids:
        if vid in cards and vid not in seen:
            seen.add(vid)
            out.append(directory.summary(vid, cards[vid],
                                         social.friend_data(viewer_id, vid)["state"]))
    return out


@registry.endpoint("circle/list")
def handle_list(payload: dict) -> dict:
    """The unfiltered browse list: circles with room to join, fullest first
    (search_circles orders by member count), so a new player lands on active
    circles rather than empty ones."""
    viewer_id = payload["viewer_id"]
    circles = [c for c in social.search_circles(limit=60)
               if c["member_num"] < social.MAX_CIRCLE_MEMBERS
               and c["join_style"] != social.JOIN_CLOSED][:30]
    data = _browse(viewer_id, circles)["data"]
    # CircleListResponse carries three fields conditional_search does not:
    # the recommendations, and this player's own outstanding application and
    # invitations -- which is how the browse screen marks a row "Applied" or
    # "Invited" instead of offering to apply again.
    data["recommend_circle_id_array"] = [c["circle_id"] for c in circles[:5]]
    data["circle_request"] = _my_request(viewer_id)
    data["circle_scout_array"] = _my_scouts(viewer_id)
    return _ok(data)


@registry.endpoint("circle/conditional_search")
def handle_conditional_search(payload: dict) -> dict:
    """CircleConditionalSearchRequest{keyword, join_style, policy, member_num}.
    Every field is optional; 0/"" means "do not filter" (social.search_circles)."""
    viewer_id = payload["viewer_id"]
    circles = social.search_circles(
        keyword=(payload.get("keyword") or "").strip(),
        join_style=payload.get("join_style") or 0,
        policy=payload.get("policy") or 0,
        member_num=payload.get("member_num") or 0,
        limit=30,
    )
    return _browse(viewer_id, circles)


@registry.endpoint("circle/check_join")
def handle_check_join(payload: dict) -> dict:
    """Asked before the circle UI opens: is this player in a circle?

    ONE boolean, is_join_circle -- that is the entire
    CircleCheckJoinResponse.CommonResponse in dump.cs. This used to answer
    with five richer, differently-named facts, none of which the client can
    read, so it always saw false and always routed to the search screen. The
    glossary's "AMBIGUOUS FALSE" note was recording exactly that.
    """
    return _ok({"is_join_circle": social.member_row(payload["viewer_id"]) is not None})


def _mark_mission_flag(viewer_id, flag: str) -> None:
    """Credit a one-off Club mission. Own read-modify-save, because this module
    otherwise never touches the per-viewer state store (its whole world is
    social.py's own database) -- and because a failure to record a mission flag
    must never fail the Club request it rode in on.

    Idempotent both ways: missions.mark_achieved dedupes the flag, and these
    endpoints are hit repeatedly."""
    from . import missions
    try:
        full_state = state_store.get_state(viewer_id) or {}
        before = list(full_state.get(missions.ACHIEVEMENT_FLAG_KEY) or ())
        missions.mark_achieved(full_state, flag)
        if list(full_state.get(missions.ACHIEVEMENT_FLAG_KEY) or ()) != before:
            state_store.save_state(viewer_id, full_state)
    except Exception:                                          # noqa: BLE001
        log.exception("circles: recording mission flag %s failed for viewer %s",
                      flag, viewer_id)


@registry.endpoint("circle/room_enter")
def handle_room_enter(payload: dict) -> dict:
    """The circle home screen. Empty request; the caller's own circle."""
    viewer_id = payload["viewer_id"]
    # "Check on your Club" (mission 3000001, condition_type 600026) -- opening
    # the Club screen IS the check, and this is the only endpoint that does it.
    # Only credited when there is actually a Club to check.
    if social.circle_of(viewer_id) is not None:
        from . import missions
        _mark_mission_flag(viewer_id, missions.FLAG_CIRCLE_CHECKED)
    return _room(viewer_id)


@registry.endpoint("circle/get_ranking_top")
def handle_get_ranking_top(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    top = social.ranking_top(limit=30)
    circles = [e["circle_info"] for e in top]
    return _ok({
        "circle_info_array": circles,
        "leader_info_array": _leader_summaries(viewer_id, circles),
        "circle_ranking_array": [
            {"circle_id": e["circle_info"]["circle_id"], "monthly": _monthly(),
             "rank": e["rank"], "point": e["point"]} for e in top],
        # No monthly settlement job runs here, so a ranking calculation is
        # never in progress. (own_circle_ranking, which this used to add, is
        # not a field the client declares -- it was never read.)
        "is_calculate": False,
    })


# ---------------------------------------------------------- join requests


@registry.endpoint("circle_user/user_join_request")
def handle_user_join_request(payload: dict) -> dict:
    """A player applies to a circle.

    An OPEN circle (join_style 1) admits them immediately -- that is what
    "open" means, and routing an open join through an approval queue nobody
    ever looks at would silently strand the player. Approval-required
    circles get a pending row; closed ones refuse.
    """
    viewer_id = payload["viewer_id"]
    cid = payload.get("circle_id")
    circle = social.get_circle(cid) if cid else None
    if circle is None or social.member_row(viewer_id) is not None:
        return _refuse()
    if circle["member_num"] >= social.MAX_CIRCLE_MEMBERS:
        return _refuse()
    if circle["join_style"] == social.JOIN_CLOSED:
        return _refuse()
    if circle["join_style"] == social.JOIN_OPEN:
        try:
            social.add_member(viewer_id, circle["circle_id"])
        except ValueError:
            return _refuse()
        _system_post(circle["circle_id"], viewer_id)
        return _ok({"circle_user": social.member_row(viewer_id) or {}})
    social.add_pending(circle["circle_id"], viewer_id, "request")
    # CircleUserUserJoinRequestResponse{circle_user} and nothing else. An
    # application is not a membership, so the honest answer while it waits for
    # approval is the unaffiliated CircleUser -- circle/list's circle_request
    # is what tells the browse screen the application exists.
    return _ok({"circle_user": no_circle_user(viewer_id)})


# The glossary lists this spelling as the one actually seen on the wire
# (circle_user/join_request, 26 JP observations) alongside dump.cs's
# user_join_request. Same operation; register both rather than betting on one.
registry.endpoint("circle_user/join_request")(handle_user_join_request)


@registry.endpoint("circle_user/cancel_join_request")
def handle_cancel_join_request(payload: dict) -> dict:
    """The applicant withdraws. Drops every pending application this player
    has when no circle_id is named, which is what the "cancel" button on a
    screen showing all of them means."""
    viewer_id = payload["viewer_id"]
    cid = payload.get("circle_id")
    if cid:
        social.drop_pending(cid, viewer_id, "request")
    else:
        for p in social.pending_for_viewer(viewer_id, "request"):
            social.drop_pending(p["circle_id"], viewer_id, "request")
    return _ok({})          # CircleUserCancelJoinRequestResponse is empty


@registry.endpoint("circle_user/approve_join_request")
def handle_approve_join_request(payload: dict) -> dict:
    """A leader/sub-leader admits an applicant."""
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    target = _target(payload)
    if target is None:
        return _refuse()
    pending = social.pending_for_circle(circle["circle_id"], "request")
    if not any(str(p["viewer_id"]) == target for p in pending):
        return _refuse()
    try:
        social.add_member(target, circle["circle_id"])
    except ValueError:
        # They joined elsewhere, or the circle filled up, between applying and
        # now. Clear the dead row so the leader's queue does not keep it.
        social.drop_pending(circle["circle_id"], target, "request")
        return _refuse()
    _system_post(circle["circle_id"], target)
    # circle_user is the ADMITTED player's new row -- the approval screen
    # updates that member, not the leader who pressed the button.
    return _ok({"circle_user": social.member_row(target) or {}})


@registry.endpoint("circle_user/decline_join_request")
def handle_decline_join_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    target = _target(payload)
    if target is None:
        return _refuse()
    social.drop_pending(circle["circle_id"], target, "request")
    return _ok({})          # CircleUserDeclineJoinRequestResponse is empty


@registry.endpoint("circle_user/checked_request")
def handle_checked_request(payload: dict) -> dict:
    """Marks the pending-applications list as seen, clearing its badge."""
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    social.mark_pending_checked(circle["circle_id"])
    # CircleUserCheckedRequestResponse{last_checked_time}: a unix timestamp,
    # the moment the badge was cleared.
    return _ok({"last_checked_time": int(time.time())})


# --------------------------------------------------------------- scouting


def _target(payload: dict):
    for key in ("target_viewer_id", "viewer_id_to", "friend_viewer_id", "member_viewer_id"):
        if payload.get(key):
            return str(payload[key])
    return None


@registry.endpoint("circle_user/scout")
def handle_scout(payload: dict) -> dict:
    """CircleUserScoutRequest{target_viewer_id} -- invite a specific player."""
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    target = _target(payload)
    if target is None or social.member_row(target) is not None:
        return _refuse()          # no such target, or already in a circle
    if state_store.get_state(target) is None:
        return _refuse()          # not a real account on this server
    if circle["member_num"] >= social.MAX_CIRCLE_MEMBERS:
        return _refuse()
    social.add_pending(circle["circle_id"], target, "scout")
    # CircleUserScoutResponse{is_scout_max}: whether the circle has now used
    # up its invitation slots. max_circle_scout_num is 30 in the real
    # load/index common_define, same as the member cap.
    outstanding = len(social.pending_for_circle(circle["circle_id"], "scout"))
    return _ok({"is_scout_max": outstanding >= MAX_SCOUT})


@registry.endpoint("circle_user/cancel_scout")
def handle_cancel_scout(payload: dict) -> dict:
    """Either side withdraws an invitation: the circle rescinds it, or the
    invited player dismisses it. Which one is calling decides which pending
    row is dropped."""
    viewer_id = payload["viewer_id"]
    target = _target(payload)
    circle, member = _my_circle(viewer_id)
    if target is not None and circle is not None and _can_manage(circle, member):
        social.drop_pending(circle["circle_id"], target, "scout")
        return _ok({})
    cid = payload.get("circle_id")
    if cid:
        social.drop_pending(cid, viewer_id, "scout")
    else:
        for p in social.pending_for_viewer(viewer_id, "scout"):
            social.drop_pending(p["circle_id"], viewer_id, "scout")
    return _ok({})          # CircleUserCancelScoutResponse is empty


@registry.endpoint("circle_user/approve_scout")
def handle_approve_scout(payload: dict) -> dict:
    """The invited player accepts. The circle_id may be omitted when only one
    invitation is outstanding, which is the common case."""
    viewer_id = payload["viewer_id"]
    if social.member_row(viewer_id) is not None:
        return _refuse()
    pending = social.pending_for_viewer(viewer_id, "scout")
    cid = payload.get("circle_id") or (pending[0]["circle_id"] if pending else None)
    if cid is None or not any(p["circle_id"] == cid for p in pending):
        return _refuse()
    try:
        social.add_member(viewer_id, cid)
    except ValueError:
        social.drop_pending(cid, viewer_id, "scout")
        return _refuse()
    _system_post(cid, viewer_id)
    return _ok({"circle_user": social.member_row(viewer_id) or {}})


# ------------------------------------------------- membership management


@registry.endpoint("circle_user/leave")
def handle_leave(payload: dict) -> dict:
    """Leave voluntarily. If the leader leaves, social.remove_member hands the
    circle to the longest-serving member rather than orphaning it."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if social.remove_member(viewer_id) is None:
        return _refuse()
    if circle is not None:
        _system_post(circle["circle_id"], viewer_id, social.SYS_LEAVE)
    # CircleUserLeaveResponse{circle_user}: what the caller is now, which is
    # unaffiliated. Returning the row they just gave up would put the client
    # straight back into the circle it thinks it left.
    return _ok({"circle_user": no_circle_user(viewer_id)})


@registry.endpoint("circle_user/kick")
def handle_kick(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    victim = social.member_row(target)
    if victim is None or victim["circle_id"] != circle["circle_id"]:
        return _refuse()
    # A sub-leader must not be able to kick the leader, and neither may anyone
    # kick a peer of equal rank -- otherwise two sub-leaders can remove each
    # other, and a sub-leader can decapitate the circle.
    if victim["membership"] >= member["membership"]:
        return _refuse()
    social.remove_member(target)
    _system_post(circle["circle_id"], target, social.SYS_LEAVE)
    return _ok({})          # CircleUserKickResponse is empty


@registry.endpoint("circle_user/change_leader")
def handle_change_leader(payload: dict) -> dict:
    """Hand the circle over. LEADER ONLY."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse()
    target = _target(payload)
    if target is None:
        return _refuse()
    try:
        social.change_leader(circle["circle_id"], target)
    except ValueError:
        return _refuse()
    _system_post(circle["circle_id"], target, social.SYS_LEADER)
    # The caller's OWN row, now demoted out of leadership -- the screen that
    # sent this is the ex-leader's.
    return _ok({"circle_user": social.member_row(viewer_id) or {}})


@registry.endpoint("circle_user/change_sub_leader")
def handle_change_sub_leader(payload: dict) -> dict:
    """Promote a member to sub-leader, or demote one back. LEADER ONLY --
    sub-leaders appointing sub-leaders is not a power the client offers.

    The request has no separate promote/demote verb in dump.cs, so this
    TOGGLES: a plain member becomes a sub-leader, an existing sub-leader
    becomes a plain member again. That matches a single UI button per member,
    and it is the only reading that makes demotion reachable at all.
    """
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse()
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    victim = social.member_row(target)
    if victim is None or victim["circle_id"] != circle["circle_id"]:
        return _refuse()
    new = (social.MEMBERSHIP_MEMBER if victim["membership"] == social.MEMBERSHIP_SUB_LEADER
           else social.MEMBERSHIP_SUB_LEADER)
    social.set_membership(target, new)
    _system_post(circle["circle_id"], target, social.SYS_SUB_LEADER)
    return _ok({})          # CircleUserChangeSubLeaderResponse is empty


# ----------------------------------------------------------------- chat


def _post_and_room(viewer_id, message_type, message_id=0, message="") -> dict:
    """Post to the caller's circle, then answer with the POLLING subset --
    which is what both send_message and send_stamp returned in the real
    captures (7 keys, not the full room), each carrying just the one new
    message and its sender."""
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    entry = social.post_chat(circle["circle_id"], viewer_id, message_type,
                             message_id=message_id, message=message)
    return _ok({
        "circle_chat_message_array": [entry],
        "circle_chat_user_array": _chat_user_array([str(viewer_id)]),
        "circle_item_request_array": [],
        "circle_item_donate_array": [],
        "room_match_info_array": [],
        "circle_post_partner_array": [],
        "chat_polling_interval": CHAT_POLLING_INTERVAL,
    })


def _system_post(circle_id, viewer_id, system_id=social.SYS_JOIN) -> None:
    """A system line about a member: message_type 0
    (SYSTEM_MESSAGE_USER_NAME), with message_id saying WHICH line
    (SYS_JOIN/LEAVE/LEADER/...) and the name coming from the poster's own
    viewer_id.

    This used to post type 5, which is not "system notice" at all -- it is
    ITEM_REQUEST, and a type-5 card whose message_id names no live request is
    exactly what the client answers with "This item request has ended". Every
    join notice was rendering as a dead item-request card.

    Best-effort: a chat write must never be what fails a join."""
    try:
        social.post_chat(circle_id, viewer_id, social.CHAT_SYSTEM_USER_NAME,
                         message_id=system_id)
    except Exception:
        log.exception("circle: failed to post system notice for %s", viewer_id)


@registry.endpoint("circle_chat/send_message")
def handle_send_message(payload: dict) -> dict:
    """Request {message}. Real response: message_type 1, the text under
    `message_data`, message_id null."""
    text = (payload.get("message") or "").strip()
    if not text:
        return _refuse()
    return _post_and_room(payload["viewer_id"], social.CHAT_TEXT,
                          message=text[:MAX_COMMENT_LEN])


@registry.endpoint("circle_chat/send_stamp")
def handle_send_stamp(payload: dict) -> dict:
    """Request {stamp_id}. Real response: message_type 2, the stamp id under
    `message_id`, no text field at all."""
    stamp_id = payload.get("stamp_id")
    if not stamp_id:
        return _refuse()
    return _post_and_room(payload["viewer_id"], social.CHAT_STAMP,
                          message_id=int(stamp_id))


@registry.endpoint("circle_chat/polling")
def handle_polling(payload: dict) -> dict:
    """Empty request. Returns everything new since this member's
    last_check_post_id, and advances it -- so the next poll is genuinely
    incremental instead of re-sending the same backlog every 3 seconds.

    A viewer in no circle gets the empty polling payload rather than a
    refusal: the client polls this on a timer, and refusing on every tick
    would be a steady stream of errors for a perfectly normal state.
    """
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _ok({"circle_chat_message_array": [], "circle_chat_user_array": [],
                    "circle_item_request_array": [], "circle_item_donate_array": [],
                    "room_match_info_array": [], "circle_post_partner_array": [],
                    "chat_polling_interval": CHAT_POLLING_INTERVAL})
    data = _room_payload(viewer_id, circle, member, full=False)
    messages = data["circle_chat_message_array"]
    if messages:
        social.set_last_check_post_id(viewer_id, messages[-1]["post_id"])
    return _ok(data)


@registry.endpoint("circle_chat/post_partner")
def handle_post_partner(payload: dict) -> dict:
    """Offer your practice partner to the circle. Request {trained_chara_id,
    comment}; the response is the ordinary polling payload (dump.cs:
    CircleChatPostPartnerResponse is field-for-field CircleChatPollingResponse),
    with the offer now in circle_post_partner_array.

    The offer IS the roster entry -- _post_partner_array derives it live from
    the member's strongest horse -- so this posts the notice and re-polls."""
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    # PRACTICE_PARTNER_SHARE, or the _COMMENT variant when the player picked
    # one of the canned comments (its id rides in message_id).
    comment_id = int(payload.get("post_comment_id") or 0)
    social.post_chat(circle["circle_id"], viewer_id,
                     social.CHAT_PARTNER_SHARE_COMMENT if comment_id
                     else social.CHAT_PARTNER_SHARE,
                     message_id=comment_id)
    # "Share a Veteran Umamusume in your Club's chat" (mission 600706,
    # condition_type 600024) -- this post is that share.
    from . import missions
    _mark_mission_flag(viewer_id, missions.FLAG_CIRCLE_PARTNER_SHARED)
    return _ok(_room_payload(viewer_id, circle, social.member_row(viewer_id),
                             full=False))


@registry.endpoint("circle/get_post_partner_data")
def handle_get_post_partner_data(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _ok({"circle_post_partner_array": []})
    # CircleGetPostPartnerDataResponse{circle_post_partner_array} -- one field.
    # daily_post_partner_count belongs to room_enter, not here.
    return _ok({"circle_post_partner_array": _post_partner_array(circle["circle_id"])})


@registry.endpoint("circle_chat/invite_room_match")
def handle_invite_room_match(payload: dict) -> dict:
    """Room Match (the co-op race lobby) is not implemented on this server --
    there is no lobby to invite anyone into.

    So this posts NOTHING. A type-4 (ROOM_MATCH_INVITE) card is a live button:
    its message_id is a room id, and tapping it tries to enter that room. A
    card for a room that does not exist is a dead end in everyone's chat
    forever -- the same failure mode as the type-5 cards that reported "this
    item request has ended". An empty success leaves the chat clean."""
    viewer_id = payload["viewer_id"]
    if social.member_row(viewer_id) is None:
        return _refuse()
    return _ok({})          # CircleChatInviteRoomMatchResponse is empty


# -------------------------------------------------------- item requests


@registry.endpoint("circle_chat/send_item_request")
def handle_send_item_request(payload: dict) -> dict:
    """Ask the circle for an item. One live request per member (see
    social.open_request), announced in chat like the real server's
    message_type-5 notices."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    item_id = payload.get("item_id")
    if not item_id:
        return _refuse()
    request = social.open_request(circle["circle_id"], viewer_id, int(item_id))
    # message_type 5 carries the REQUEST_ID in message_id -- that is how the
    # chat card finds the request it stands for. Posting it as 0 (which is
    # what happened while _system_post owned this) leaves the card pointing at
    # no request, and tapping it reports "This item request has ended" even
    # though the request is live and hours from expiring.
    social.post_chat(circle["circle_id"], viewer_id, social.CHAT_ITEM_REQUEST,
                     message_id=request["request_id"])
    member = social.member_row(viewer_id)
    data = _room_payload(viewer_id, circle, member, full=False)
    # The polling payload plus the one extra field dump.cs adds here: when
    # this member may ask again. social.py holds it on the member row.
    data["item_request_end_time"] = (member or {}).get("item_request_end_time") or ""
    return _ok(data)


@registry.endpoint("circle_item_request/get_request_data")
def handle_get_request_data(payload: dict) -> dict:
    """Real response (0042): {circle_item_request_array, circle_item_donate_array}
    and nothing else."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _ok({"circle_item_request_array": [], "circle_item_donate_array": []})
    cid = circle["circle_id"]
    return _ok({"circle_item_request_array": social.requests_for_circle(cid),
                "circle_item_donate_array": social.donations_for_circle(cid)})


def _donate(viewer_id, request_ids, item_counts) -> dict:
    """Shared by donate and donate_multiple. Spends the donor's own items --
    a donation that costs the giver nothing is not a donation -- and refuses
    the whole batch if they cannot cover it, rather than partially charging."""
    from . import shop
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    wanted = [(int(c.get("item_id")), int(c.get("number") or 0)) for c in item_counts]
    for item_id, number in wanted:
        if number <= 0 or shop.item_count(full_state, item_id) < number:
            return _refuse()

    donations = []
    for rid in request_ids:
        row = social.request_row(rid)
        if row is None or row["circle_id"] != circle["circle_id"] or row["received"]:
            continue
        # The donated amount is whatever the donor offered for that item.
        number = next((n for iid, n in wanted if iid == row["item_id"]), 0)
        if number <= 0:
            continue
        shop.add_item(full_state, row["item_id"], -number)
        donations.append(social.add_donation(circle["circle_id"], rid, viewer_id, number))
    if not donations:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    # +10 circle points PER DONATION (social.POINT_PER_DONATION), which is what
    # circle_ranking_this_month/get_ranking_top report. add_circle_point existed
    # but nothing had ever called it, so donating moved no points at all and
    # every circle sat on 0 forever.
    total = social.add_circle_point(circle["circle_id"],
                                    social.POINT_PER_DONATION * len(donations))
    log.info("circle %s: %s donated %d time(s), +%d points -> %d",
             circle["circle_id"], viewer_id, len(donations),
             social.POINT_PER_DONATION * len(donations), total)
    return _ok({
        "circle_item_donate_array": donations,
        "daily_donated_count": social.donated_today(viewer_id),
        # The real response echoes the donor's post-spend counts for the items
        # involved, so the client's inventory badge updates without a reload.
        "current_item_data_array": [
            {"item_id": iid, "number": shop.item_count(full_state, iid)}
            for iid, _n in wanted],
        # The circle points are credited above; this is the ITEM payout to the
        # donor, which the real server does not make for donating (the giver
        # gives). Honestly empty rather than an invented reward -- and the
        # response has nowhere to report circle points anyway: dump.cs's
        # CircleItemRequestDonate*Response declares no such field, so the new
        # total reaches the client through circle_ranking_this_month on the
        # next room_enter/polling, which is where the circle screen reads it.
        "reward_summary_info": {},
    })


@registry.endpoint("circle_item_request/donate_multiple")
def handle_donate_multiple(payload: dict) -> dict:
    """Request (0043): {request_id_array, client_item_data_array:[{item_id,
    number}]}."""
    return _donate(payload["viewer_id"],
                   payload.get("request_id_array") or [],
                   payload.get("client_item_data_array") or [])


@registry.endpoint("circle_item_request/donate")
def handle_donate(payload: dict) -> dict:
    """The singular form dump.cs carries (CircleItemRequestDonate), distinct
    from donate_multiple. Same operation on one request; accepts either the
    singular or the array field names."""
    rid = payload.get("request_id")
    items = payload.get("client_item_data_array")
    if items is None and payload.get("item_id"):
        items = [{"item_id": payload["item_id"], "number": payload.get("number") or 1}]
    elif items is None and rid is not None:
        # dump.cs CircleItemRequestDonateRequest{request_id, item_num,
        # client_own_num}: the singular form names no item at all -- the
        # request being donated to already says which item it wants.
        row = social.request_row(rid)
        if row is not None:
            items = [{"item_id": row["item_id"],
                      "number": payload.get("item_num") or 1}]
    out = _donate(payload["viewer_id"],
                  [rid] if rid else (payload.get("request_id_array") or []),
                  items or [])
    # current_item_data_array is on donate_multiple only.
    out.get("data", {}).pop("current_item_data_array", None)
    return out


@registry.endpoint("circle_item_request/receive")
def handle_receive(payload: dict) -> dict:
    """Collect what the circle donated to your requests. Real response (0035):
    {circle_item_donate_array, circle_user_array, reward_summary_info}.

    social.collect_requests marks the requests received and returns their
    donations in ONE locked step, so a double tap cannot pay out twice."""
    from . import shop
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    collected = social.collect_requests(viewer_id)
    if not collected:
        return _ok({"circle_item_donate_array": [], "circle_user_array": [],
                    "reward_summary_info": {}})
    full_state = state_store.get_state(viewer_id) or {}
    gained: dict = {}
    for d in collected:
        item_id = d.pop("item_id", None)
        if item_id:
            shop.add_item(full_state, item_id, d["item_num"])
            gained[item_id] = gained.get(item_id, 0) + d["item_num"]
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "circle_item_donate_array": collected,
        "circle_user_array": social.members(circle["circle_id"]),
        "reward_summary_info": {
            "item_data_array": [{"item_id": i, "number": n} for i, n in gained.items()]},
    })


# The glossary also lists these under a flat circle/ prefix. Same handlers --
# a spelling difference between the dump and the wire must not be the reason a
# player cannot receive their items.
registry.endpoint("circle/item_request_get_request_data")(handle_get_request_data)
registry.endpoint("circle/item_request_donate_multiple")(handle_donate_multiple)
registry.endpoint("circle/item_request_donate")(handle_donate)
registry.endpoint("circle/item_request_receive")(handle_receive)


# --------------------------------------------------------- profile cards
# A circle's own profile card, and a member's note inside the circle. Both are
# opaque display blobs: stored as sent, echoed back as stored. Neither was
# captured, and neither drives any logic, so passing the client's own
# structure straight through is strictly safer than imposing a schema on it.


@registry.endpoint("circle/get_profile_card_info")
def handle_get_profile_card_info(payload: dict) -> dict:
    import json
    circle, _ = _my_circle(payload["viewer_id"])
    cid = payload.get("circle_id") or (circle["circle_id"] if circle else None)
    if cid is None:
        return _refuse()
    raw = social.get_profile_card(cid)
    # The card, plus the circle it belongs to and last month's standing --
    # the card screen renders all three together.
    return _ok({"circle_profile_card_info": json.loads(raw) if raw else {},
                "image_file_status": 0, "image_file_url": "", "image_unique_id": "",
                "circle_info": circle or social.get_circle(cid) or {},
                "circle_ranking_last_month": _ranking(cid, last=True)})


@registry.endpoint("circle/set_profile_card_info")
def handle_set_profile_card_info(payload: dict) -> dict:
    import json
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()
    info = payload.get("circle_profile_card_info") or payload.get("profile_card_info") or {}
    social.set_profile_card(circle["circle_id"], json.dumps(info))
    # The request carries {name, comment, join_style, policy} alongside the
    # card, so this screen saves the circle's settings too -- and the response
    # is the updated CircleInfo, nothing else.
    return _ok({"circle_info": _apply_update(circle["circle_id"], payload)})


@registry.endpoint("circle_user/get_profile_card_info")
def handle_user_get_profile_card_info(payload: dict) -> dict:
    """A MEMBER's personal profile card as seen from inside the circle. The
    personal card already has an owner (user_profile.py); this reads that same
    stored card for whichever member was asked about, rather than keeping a
    second, divergent copy of the same thing."""
    from . import user_profile
    target = _target(payload) or str(payload["viewer_id"])
    st = state_store.get_state(target) or {}
    card = (st.get(user_profile.PROFILE_CARD_STATE_KEY) or {}).get("info") or {}
    return _ok({"profile_card_info": card})


@registry.endpoint("circle_user/set_profile_card_info")
def handle_user_set_profile_card_info(payload: dict) -> dict:
    """Saves through the personal card's own owner, then answers with the
    empty body dump.cs declares -- user_profile's own response shape belongs
    to user/set_profile_card_info, not to this one."""
    from . import user_profile
    saved = user_profile.handle_set_profile_card_info(payload)
    if (saved.get("data_headers") or {}).get("result_code") != 1:
        return saved
    return _ok({})


@registry.endpoint("circle_user/get_profile")
def handle_get_profile(payload: dict) -> dict:
    """The circle's RECRUITMENT profile, not a member's personal note -- the
    request is empty and dump.cs gives back {circle_info,
    circle_ranking_last_month, chara_id, bg_id, recruit_comment, dress_id}:
    the umamusume, outfit and background a circle advertises itself with, plus
    the pitch written under them.

    Stored per CIRCLE rather than per leader, so a leadership change does not
    silently blank the circle's advert."""
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse()
    return _ok(_recruit_profile(circle))


def _recruit_profile(circle) -> dict:
    import json
    raw = social.get_user_profile(circle["circle_id"])
    saved = json.loads(raw) if raw else {}
    return {
        "circle_info": circle,
        "circle_ranking_last_month": _ranking(circle["circle_id"], last=True),
        "chara_id": saved.get("chara_id") or 0,
        "bg_id": saved.get("bg_id") or 0,
        "recruit_comment": saved.get("recruit_comment") or "",
        "dress_id": saved.get("dress_id") or 0,
    }


@registry.endpoint("circle_user/set_profile")
def handle_set_profile(payload: dict) -> dict:
    import json
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse()          # the advert is the leadership's to write
    info = {
        "chara_id": int(payload.get("chara_id") or 0),
        "bg_id": int(payload.get("bg_id") or 0),
        "dress_id": int(payload.get("dress_id") or 0),
        "recruit_comment": (payload.get("recruit_comment") or "")[:MAX_COMMENT_LEN],
    }
    social.set_user_profile(circle["circle_id"], json.dumps(info))
    return _ok(_recruit_profile(circle))
