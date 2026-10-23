
from __future__ import annotations

import logging
import time

from .. import profile_images, social
from .. import state as state_store
from . import directory, registry
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

CHAT_POLLING_INTERVAL = 3
CHAT_BACKLOG = 100

MAX_NAME_LEN = 20
MAX_COMMENT_LEN = 200
MAX_SCOUT = 30


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _my_circle(viewer_id):
    member = social.member_row(viewer_id)
    if member is None:
        return None, None
    return social.get_circle(member["circle_id"]), member


def _is_leader(circle, viewer_id) -> bool:
    return circle is not None and str(circle["leader_viewer_id"]) == str(viewer_id)


def _can_manage(circle, member) -> bool:
    return member is not None and member["membership"] >= social.MEMBERSHIP_SUB_LEADER


def _refuse_join(exc: ValueError) -> dict:
    code = {"no such circle": ResultCode.CIRCLE_NOT_EXIST,
            "already in a circle": ResultCode.CIRCLE_ALREADY_JOIN,
            "circle is full": ResultCode.CIRCLE_MEMBER_NUM_ERROR}.get(str(exc))
    return _refuse(code or ResultCode.PARAM_ERROR)


def _refuse_unauthorized(circle) -> dict:
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    return _refuse(ResultCode.CIRCLE_NO_OPERATION_AUTHORITY)


def _chat_user_array(viewer_ids) -> list:
    cards = directory.cards_for(viewer_ids)
    return [{"viewer_id": c["viewer_id"], "leader_chara_id": c.get("leader_chara_id") or 0,
             "leader_chara_dress_id": c.get("leader_chara_dress_id") or 0,
             "name": c.get("name") or ""}
            for c in (cards[v] for v in viewer_ids if v in cards)]


def _post_partner_array(circle_id) -> list:
    from . import trained_chara
    out = []
    ids = social.member_ids(circle_id)
    rosters = state_store.all_states_for_key(trained_chara.ROSTER_KEY, ids)
    for vid in ids:
        best = directory._best_trained_chara(rosters.get(vid) or [])
        if best is not None:
            out.append({"practice_partner_info": dict(best),
                        "post_comment_id": 0, "post_time": social.now()})
    return out


def _room_payload(viewer_id, circle, member, full: bool = True) -> dict:
    cid = circle["circle_id"]
    after = (member or {}).get("last_check_post_id") or 0 if not full else 0
    messages = social.chat_since(cid, after_post_id=after, limit=CHAT_BACKLOG)
    senders = sorted({str(m["viewer_id"]) for m in messages})

    payload = {
        "circle_chat_message_array": messages,
        "circle_chat_user_array": _chat_user_array(senders),
        "circle_item_request_array": social.requests_for_circle(cid),
        "circle_item_donate_array": social.donations_for_circle(cid),
        "room_match_info_array": [],
        "circle_post_partner_array": _post_partner_array(cid),
        "chat_polling_interval": CHAT_POLLING_INTERVAL,
    }
    if not full:
        return payload

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
        "change_leader": _is_leader(circle, viewer_id),
        "is_scout_able": _can_manage(circle, member)
        and circle["member_num"] < social.MAX_CIRCLE_MEMBERS,
        "is_show_ranking_result": False,
        "is_calculate": False,
        "daily_donated_count": social.donated_today(viewer_id),
        "daily_post_partner_count": 0,
        "circle_ranking_this_month": {"circle_id": cid, "monthly": _monthly(),
                                      "rank": rank, "point": point},
        "circle_ranking_last_month": {"circle_id": cid, "monthly": _monthly(-1),
                                      "rank": rank, "point": last_point},
        "room_info_array": [],
        "room_user_array": [],
    })
    return payload


def _monthly(offset: int = 0) -> int:
    import time
    t = time.localtime()
    y, m = t.tm_year, t.tm_mon + offset
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y * 100 + m


def _room(viewer_id) -> dict:
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_CHAT_NO_JOIN_USER)
    return _ok(_room_payload(viewer_id, circle, member))


def no_circle_user(viewer_id) -> dict:
    return {"viewer_id": int(viewer_id), "circle_id": 0, "membership": 0,
            "join_time": social.now(), "penalty_end_time": "0000-00-00 00:00:00",
            "item_request_end_time": "0000-00-00 00:00:00",
            "last_check_post_id": 0}


def _ranking(cid, last: bool = False) -> dict:
    rank, point, last_point = social.circle_rank(cid)
    return {"circle_id": cid, "monthly": _monthly(-1 if last else 0),
            "rank": rank, "point": last_point if last else point}


def _request_rows(circle_id) -> list:
    return [{"circle_id": circle_id, "viewer_id": p["viewer_id"],
             "update_time": p["create_time"]}
            for p in social.pending_for_circle(circle_id, "request")]


def _scout_rows(circle_id) -> list:
    return [{"circle_id": circle_id, "viewer_id": p["viewer_id"]}
            for p in social.pending_for_circle(circle_id, "scout")]


def _my_request(viewer_id) -> dict:
    mine = social.pending_for_viewer(viewer_id, "request")
    if not mine:
        return {}
    p = mine[0]
    return {"circle_id": p["circle_id"], "viewer_id": int(viewer_id),
            "update_time": p["create_time"]}


def _my_scouts(viewer_id) -> list:
    return [{"circle_id": p["circle_id"], "viewer_id": int(viewer_id)}
            for p in social.pending_for_viewer(viewer_id, "scout")]


@registry.endpoint("circle/make")
def handle_make(payload: dict) -> dict:
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
        return _refuse(ResultCode.CIRCLE_ALREADY_JOIN)
    return _ok({"circle_info": circle,
                "circle_user": social.member_row(viewer_id) or {}})


@registry.endpoint("circle/update")
def handle_update(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    return _ok({"circle_info": _apply_update(circle["circle_id"], payload)})


def _apply_update(circle_id, payload: dict) -> dict:
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
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse_unauthorized(circle)
    social.break_up_circle(circle["circle_id"])
    return _ok({"circle_user": no_circle_user(viewer_id)})


def _circle_detail(viewer_id, circle) -> dict:
    cid = circle["circle_id"]
    ids = social.member_ids(cid)
    cards = directory.cards_for(ids)
    return {
        "circle_info": circle,
        "circle_user_array": social.members(cid),
        "user_friend_array": [social.friend_data(viewer_id, vid) for vid in ids],
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
        return _refuse(ResultCode.CIRCLE_NOT_EXIST)
    return _ok(_circle_detail(viewer_id, circle))


@registry.endpoint("circle/direct")
def handle_direct(payload: dict) -> dict:
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
    viewer_id = payload["viewer_id"]
    circles = [c for c in social.search_circles(limit=60)
               if c["member_num"] < social.MAX_CIRCLE_MEMBERS
               and c["join_style"] != social.JOIN_CLOSED][:30]
    data = _browse(viewer_id, circles)["data"]
    data["recommend_circle_id_array"] = [c["circle_id"] for c in circles[:5]]
    data["circle_request"] = _my_request(viewer_id)
    data["circle_scout_array"] = _my_scouts(viewer_id)
    return _ok(data)


@registry.endpoint("circle/conditional_search")
def handle_conditional_search(payload: dict) -> dict:
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
    return _ok({"is_join_circle": social.member_row(payload["viewer_id"]) is not None})


def _mark_mission_flag(viewer_id, flag: str) -> None:
    from . import missions
    try:
        full_state = state_store.get_state(viewer_id) or {}
        before = list(full_state.get(missions.ACHIEVEMENT_FLAG_KEY) or ())
        missions.mark_achieved(full_state, flag)
        if list(full_state.get(missions.ACHIEVEMENT_FLAG_KEY) or ()) != before:
            state_store.save_state(viewer_id, full_state)
    except Exception:
        log.exception("circles: recording mission flag %s failed for viewer %s",
                      flag, viewer_id)


@registry.endpoint("circle/room_enter")
def handle_room_enter(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
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
        "is_calculate": False,
    })


@registry.endpoint("circle_user/user_join_request")
def handle_user_join_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    cid = payload.get("circle_id")
    circle = social.get_circle(cid) if cid else None
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_EXIST)
    if social.member_row(viewer_id) is not None:
        return _refuse(ResultCode.CIRCLE_ALREADY_JOIN)
    if circle["member_num"] >= social.MAX_CIRCLE_MEMBERS:
        return _refuse(ResultCode.CIRCLE_MEMBER_NUM_ERROR)
    if circle["join_style"] == social.JOIN_CLOSED:
        return _refuse(ResultCode.CIRCLE_REQUEST_NOT_PERMITTED)
    if circle["join_style"] == social.JOIN_OPEN:
        try:
            social.add_member(viewer_id, circle["circle_id"])
        except ValueError as exc:
            return _refuse_join(exc)
        _system_post(circle["circle_id"], viewer_id)
        return _ok({"circle_user": social.member_row(viewer_id) or {}})
    if any(p["circle_id"] != circle["circle_id"]
           for p in social.pending_for_viewer(viewer_id, "request")):
        return _refuse(ResultCode.CIRCLE_USER_REQUEST_COUNT_ERROR)
    social.add_pending(circle["circle_id"], viewer_id, "request")
    return _ok({"circle_user": no_circle_user(viewer_id)})


registry.endpoint("circle_user/join_request")(handle_user_join_request)


@registry.endpoint("circle_user/cancel_join_request")
def handle_cancel_join_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    cid = payload.get("circle_id")
    if cid:
        social.drop_pending(cid, viewer_id, "request")
    else:
        for p in social.pending_for_viewer(viewer_id, "request"):
            social.drop_pending(p["circle_id"], viewer_id, "request")
    return _ok({})


@registry.endpoint("circle_user/approve_join_request")
def handle_approve_join_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None:
        return _refuse()
    pending = social.pending_for_circle(circle["circle_id"], "request")
    if not any(str(p["viewer_id"]) == target for p in pending):
        return _refuse()
    try:
        social.add_member(target, circle["circle_id"])
    except ValueError as exc:
        social.drop_pending(circle["circle_id"], target, "request")
        return _refuse_join(exc)
    _system_post(circle["circle_id"], target)
    return _ok({"circle_user": social.member_row(target) or {}})


@registry.endpoint("circle_user/decline_join_request")
def handle_decline_join_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None:
        return _refuse()
    social.drop_pending(circle["circle_id"], target, "request")
    return _ok({})


@registry.endpoint("circle_user/checked_request")
def handle_checked_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    social.mark_pending_checked(circle["circle_id"])
    return _ok({"last_checked_time": int(time.time())})


def _target(payload: dict):
    for key in ("target_viewer_id", "viewer_id_to", "friend_viewer_id", "member_viewer_id"):
        if payload.get(key):
            return str(payload[key])
    return None


@registry.endpoint("circle_user/scout")
def handle_scout(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None:
        return _refuse()
    if social.member_row(target) is not None:
        return _refuse(ResultCode.CIRCLE_ALREADY_JOIN)
    if state_store.get_state(target) is None:
        return _refuse(ResultCode.SEARCH_USER_NOT_FOUND)
    if circle["member_num"] >= social.MAX_CIRCLE_MEMBERS:
        return _refuse(ResultCode.CIRCLE_SCOUT_MEMBER_MAX)
    outstanding = social.pending_for_circle(circle["circle_id"], "scout")
    if any(str(p["viewer_id"]) == target for p in outstanding):
        return _refuse(ResultCode.CIRCLE_ALREADY_SCOUT)
    if len(outstanding) >= MAX_SCOUT:
        return _refuse(ResultCode.CIRCLE_SCOUT_COUNT_ERROR)
    social.add_pending(circle["circle_id"], target, "scout")
    outstanding = len(social.pending_for_circle(circle["circle_id"], "scout"))
    return _ok({"is_scout_max": outstanding >= MAX_SCOUT})


@registry.endpoint("circle_user/cancel_scout")
def handle_cancel_scout(payload: dict) -> dict:
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
    return _ok({})


@registry.endpoint("circle_user/approve_scout")
def handle_approve_scout(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    if social.member_row(viewer_id) is not None:
        return _refuse(ResultCode.CIRCLE_ALREADY_JOIN)
    pending = social.pending_for_viewer(viewer_id, "scout")
    cid = payload.get("circle_id") or (pending[0]["circle_id"] if pending else None)
    if cid is None or not any(p["circle_id"] == cid for p in pending):
        return _refuse(ResultCode.CIRCLE_NO_SCOUT_CIRCLE)
    try:
        social.add_member(viewer_id, cid)
    except ValueError as exc:
        social.drop_pending(cid, viewer_id, "scout")
        return _refuse_join(exc)
    _system_post(cid, viewer_id)
    return _ok({"circle_user": social.member_row(viewer_id) or {}})


@registry.endpoint("circle_user/leave")
def handle_leave(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if social.remove_member(viewer_id) is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    if circle is not None:
        _system_post(circle["circle_id"], viewer_id, social.SYS_LEAVE)
    return _ok({"circle_user": no_circle_user(viewer_id)})


@registry.endpoint("circle_user/kick")
def handle_kick(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    victim = social.member_row(target)
    if victim is None or victim["circle_id"] != circle["circle_id"]:
        return _refuse(ResultCode.CIRCLE_NOT_MEMBER)
    if victim["membership"] >= member["membership"]:
        return _refuse(ResultCode.CIRCLE_NO_OPERATION_AUTHORITY)
    social.remove_member(target)
    _system_post(circle["circle_id"], target, social.SYS_LEAVE)
    return _ok({})


@registry.endpoint("circle_user/change_leader")
def handle_change_leader(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None:
        return _refuse()
    try:
        social.change_leader(circle["circle_id"], target)
    except ValueError:
        return _refuse()
    _system_post(circle["circle_id"], target, social.SYS_LEADER)
    return _ok({"circle_user": social.member_row(viewer_id) or {}})


@registry.endpoint("circle_user/change_sub_leader")
def handle_change_sub_leader(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if not _is_leader(circle, viewer_id):
        return _refuse_unauthorized(circle)
    target = _target(payload)
    if target is None or target == str(viewer_id):
        return _refuse()
    victim = social.member_row(target)
    if victim is None or victim["circle_id"] != circle["circle_id"]:
        return _refuse(ResultCode.CIRCLE_NOT_MEMBER)
    new = (social.MEMBERSHIP_MEMBER if victim["membership"] == social.MEMBERSHIP_SUB_LEADER
           else social.MEMBERSHIP_SUB_LEADER)
    social.set_membership(target, new)
    _system_post(circle["circle_id"], target, social.SYS_SUB_LEADER)
    return _ok({})


def _post_and_room(viewer_id, message_type, message_id=0, message="") -> dict:
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_CHAT_NO_JOIN_USER)
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
    try:
        social.post_chat(circle_id, viewer_id, social.CHAT_SYSTEM_USER_NAME,
                         message_id=system_id)
    except Exception:
        log.exception("circle: failed to post system notice for %s", viewer_id)


@registry.endpoint("circle_chat/send_message")
def handle_send_message(payload: dict) -> dict:
    text = (payload.get("message") or "").strip()
    if not text:
        return _refuse()
    return _post_and_room(payload["viewer_id"], social.CHAT_TEXT,
                          message=text[:MAX_COMMENT_LEN])


@registry.endpoint("circle_chat/send_stamp")
def handle_send_stamp(payload: dict) -> dict:
    stamp_id = payload.get("stamp_id")
    if not stamp_id:
        return _refuse()
    return _post_and_room(payload["viewer_id"], social.CHAT_STAMP,
                          message_id=int(stamp_id))


@registry.endpoint("circle_chat/polling")
def handle_polling(payload: dict) -> dict:
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
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_CHAT_NO_JOIN_USER)
    comment_id = int(payload.get("post_comment_id") or 0)
    social.post_chat(circle["circle_id"], viewer_id,
                     social.CHAT_PARTNER_SHARE_COMMENT if comment_id
                     else social.CHAT_PARTNER_SHARE,
                     message_id=comment_id)
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
    return _ok({"circle_post_partner_array": _post_partner_array(circle["circle_id"])})


@registry.endpoint("circle_chat/invite_room_match")
def handle_invite_room_match(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    if social.member_row(viewer_id) is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    return _ok({})


@registry.endpoint("circle_chat/send_item_request")
def handle_send_item_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    item_id = payload.get("item_id")
    if not item_id:
        return _refuse()
    request = social.open_request(circle["circle_id"], viewer_id, int(item_id))
    social.post_chat(circle["circle_id"], viewer_id, social.CHAT_ITEM_REQUEST,
                     message_id=request["request_id"])
    member = social.member_row(viewer_id)
    data = _room_payload(viewer_id, circle, member, full=False)
    data["item_request_end_time"] = (member or {}).get("item_request_end_time") or ""
    return _ok(data)


@registry.endpoint("circle_item_request/get_request_data")
def handle_get_request_data(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _ok({"circle_item_request_array": [], "circle_item_donate_array": []})
    cid = circle["circle_id"]
    return _ok({"circle_item_request_array": social.requests_for_circle(cid),
                "circle_item_donate_array": social.donations_for_circle(cid)})


def _donate(viewer_id, request_ids, item_counts) -> dict:
    from . import shop
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    full_state = state_store.get_state(viewer_id) or {}
    wanted = [(int(c.get("item_id")), int(c.get("number") or 0)) for c in item_counts]
    for item_id, number in wanted:
        if number <= 0 or shop.item_count(full_state, item_id) < number:
            return _refuse(ResultCode.CIRCLE_DONATE_FAILED)

    donations = []
    for rid in request_ids:
        row = social.request_row(rid)
        if row is None or row["circle_id"] != circle["circle_id"] or row["received"]:
            continue
        number = next((n for iid, n in wanted if iid == row["item_id"]), 0)
        if number <= 0:
            continue
        shop.add_item(full_state, row["item_id"], -number)
        donations.append(social.add_donation(circle["circle_id"], rid, viewer_id, number))
    if not donations:
        return _refuse(ResultCode.CIRCLE_NO_ITEM_REQUEST)
    state_store.save_state(viewer_id, full_state)
    total = social.add_circle_point(circle["circle_id"],
                                    social.POINT_PER_DONATION * len(donations))
    log.info("circle %s: %s donated %d time(s), +%d points -> %d",
             circle["circle_id"], viewer_id, len(donations),
             social.POINT_PER_DONATION * len(donations), total)
    return _ok({
        "circle_item_donate_array": donations,
        "daily_donated_count": social.donated_today(viewer_id),
        "current_item_data_array": [
            {"item_id": iid, "number": shop.item_count(full_state, iid)}
            for iid, _n in wanted],
        "reward_summary_info": {},
    })


@registry.endpoint("circle_item_request/donate_multiple")
def handle_donate_multiple(payload: dict) -> dict:
    return _donate(payload["viewer_id"],
                   payload.get("request_id_array") or [],
                   payload.get("client_item_data_array") or [])


@registry.endpoint("circle_item_request/donate")
def handle_donate(payload: dict) -> dict:
    rid = payload.get("request_id")
    items = payload.get("client_item_data_array")
    if items is None and payload.get("item_id"):
        items = [{"item_id": payload["item_id"], "number": payload.get("number") or 1}]
    elif items is None and rid is not None:
        row = social.request_row(rid)
        if row is not None:
            items = [{"item_id": row["item_id"],
                      "number": payload.get("item_num") or 1}]
    out = _donate(payload["viewer_id"],
                  [rid] if rid else (payload.get("request_id_array") or []),
                  items or [])
    out.get("data", {}).pop("current_item_data_array", None)
    return out


@registry.endpoint("circle_item_request/receive")
def handle_receive(payload: dict) -> dict:
    from . import shop
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
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


registry.endpoint("circle/item_request_get_request_data")(handle_get_request_data)
registry.endpoint("circle/item_request_donate_multiple")(handle_donate_multiple)
registry.endpoint("circle/item_request_donate")(handle_donate)
registry.endpoint("circle/item_request_receive")(handle_receive)


@registry.endpoint("circle/get_profile_card_info")
def handle_get_profile_card_info(payload: dict) -> dict:
    import json
    circle, _ = _my_circle(payload["viewer_id"])
    cid = payload.get("circle_id") or (circle["circle_id"] if circle else None)
    if cid is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
    raw = social.get_profile_card(cid)
    stored = json.loads(raw) if raw else {}
    photo = stored.pop("_photo", None) or {}
    info = _circle_card_info(stored, cid)
    has = profile_images.exists(photo.get("path")) and info.get("image_file_hash") == photo.get("hash")
    return _ok({"circle_profile_card_info": info,
                **profile_images.wire(photo if has else None),
                "circle_info": circle or social.get_circle(cid) or {},
                "circle_ranking_last_month": _ranking(cid, last=True)})


def _circle_card_info(stored: dict, cid) -> dict:
    from . import user_profile
    info = {k: v for k, v in stored.items() if not k.startswith("_")}
    if not info.get("chara_id"):
        circle = social.get_circle(cid) or {}
        leader = circle.get("leader_viewer_id")
        lu = {}
        if leader:
            try:
                lu = (user_profile._load_data(state_store.get_state(leader) or {}, leader)
                      .get("user_info") or {})
            except Exception:
                lu = {}
        info["chara_id"] = lu.get("leader_chara_id") or 1001
    info.setdefault("support_card_id", 0)
    info.setdefault("comment", "")
    info.setdefault("is_circle_info_aligned_right", False)
    info["image_file_hash"] = str(info.get("image_file_hash") or "")
    user_profile.repair_card_info(info)
    return info


@registry.endpoint("circle/set_profile_card_info")
def handle_set_profile_card_info(payload: dict) -> dict:
    import json
    viewer_id = payload["viewer_id"]
    circle, member = _my_circle(viewer_id)
    if circle is None or not _can_manage(circle, member):
        return _refuse_unauthorized(circle)
    info = dict(payload.get("circle_profile_card_info") or payload.get("profile_card_info") or {})
    raw = social.get_profile_card(circle["circle_id"])
    photo = (json.loads(raw) if raw else {}).get("_photo")
    info["image_file_hash"] = str(info.get("image_file_hash") or "").upper()
    if payload.get("image_file") and info["image_file_hash"]:
        photo = profile_images.store(f"C{circle['circle_id']}", payload["image_file"],
                                     info["image_file_hash"])
        if photo is None:
            return _refuse()
        info["image_file_hash"] = photo["hash"]
    if photo:
        info["_photo"] = photo
    social.set_profile_card(circle["circle_id"], json.dumps(info))
    return _ok({"circle_info": _apply_update(circle["circle_id"], payload)})


@registry.endpoint("circle_user/get_profile_card_info")
def handle_user_get_profile_card_info(payload: dict) -> dict:
    from . import user_profile
    target = _target(payload) or str(payload["viewer_id"])
    st = state_store.get_state(target) or {}
    card = (st.get(user_profile.PROFILE_CARD_STATE_KEY) or {}).get("info") or {}
    if card:
        card = user_profile.repair_card_info(dict(card))
        photo = (st.get(user_profile.PROFILE_CARD_STATE_KEY) or {}).get("photo") or {}
        card["image_path"] = photo.get("path") if (
            profile_images.exists(photo.get("path"))
            and card.get("image_file_hash") == photo.get("hash")) else None
    return _ok({"profile_card_info": card})


@registry.endpoint("circle_user/set_profile_card_info")
def handle_user_set_profile_card_info(payload: dict) -> dict:
    from . import user_profile
    saved = user_profile.handle_set_profile_card_info(payload)
    if (saved.get("data_headers") or {}).get("result_code") != 1:
        return saved
    return _ok({})


@registry.endpoint("circle_user/get_profile")
def handle_get_profile(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    circle, _ = _my_circle(viewer_id)
    if circle is None:
        return _refuse(ResultCode.CIRCLE_NOT_BELONG)
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
        return _refuse_unauthorized(circle)
    info = {
        "chara_id": int(payload.get("chara_id") or 0),
        "bg_id": int(payload.get("bg_id") or 0),
        "dress_id": int(payload.get("dress_id") or 0),
        "recruit_comment": (payload.get("recruit_comment") or "")[:MAX_COMMENT_LEN],
    }
    social.set_user_profile(circle["circle_id"], json.dumps(info))
    return _ok(_recruit_profile(circle))
