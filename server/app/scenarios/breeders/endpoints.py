
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from . import impl, master as M, producers

log = logging.getLogger("uma-server")


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def _career(payload: dict):
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return None
    chara_info = career["data"].get("chara_info") or {}
    if int(chara_info.get("scenario_id") or 0) != impl.SCENARIO_ID:
        return None
    return viewer_id, full_state, career, chara_info


def _envelope(career: dict, keys) -> dict:
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    data["chara_info"] = copy.deepcopy(career["data"]["chara_info"])
    home = career["data"].get("home_info")
    if isinstance(home, dict):
        data["home_info"] = copy.deepcopy(home)
    data["unchecked_event_array"] = []
    for key in list(data):
        if key not in keys:
            data.pop(key, None)
    return response


def _next_events(full_state: dict, chara_info: dict) -> list:
    smt = _smt()
    return smt._drain_pending(full_state, None, int(chara_info.get("card_id") or 0) // 100)


def _serve_one(full_state: dict, endpoint: str) -> list:
    wire, _served = CE.serve_next(full_state, endpoint)
    return [wire] if wire else []


def _refused(where: str, why) -> dict:
    log.warning("breeders %s refused: %s", where, why)
    return refuse()


def _finish(viewer_id, full_state: dict, career: dict, chara_info: dict, response: dict) -> dict:
    smt = _smt()
    career["data"]["chara_info"] = chara_info
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _refresh(full_state: dict, chara_info: dict) -> None:
    smt = _smt()
    impl.RI.refresh_home(full_state, chara_info)
    home = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("home_info")
    if isinstance(home, dict):
        impl.refresh_preview(full_state, chara_info, home, smt._support_card_levels(full_state))


def handle_select_bc_race(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 41:
        return _refused("select_bc_race", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    program = int(payload.get("program_id") or 0)
    track = impl.roll_track(full_state)
    if program not in M.track_programs(track):
        return _refused("select_bc_race", f"program {program} not on track {track}")
    group, flag, _skill = M.bc_programs()[program]
    st["bc_program"], st["bc_group"] = program, group
    route = [int(x) for x in (chara_info.get("route_race_id_array") or [])[:1]] + impl.bc_route(flag)
    chara_info["route_race_id_array"] = route
    chara_info["route_id"] = impl.ROUTE_ID
    full_state[impl.LAST_PICK_KEY] = group
    chara_info["playing_state"] = 1
    CE.emit(full_state, producers.formed_event())
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    response["data"]["chara_info"] = copy.deepcopy(chara_info)
    response["data"]["unchecked_event_array"] = _serve_one(full_state, "select_bc_race")
    log.info("breeders: BC %s (group %s, flag %s) on track %s", program, group, flag, track)
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_team_meeting(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 40:
        return _refused("team_meeting", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    try:
        spent = impl.apply_meeting(st, impl.served_turn(full_state, chara_info),
                                   payload.get("enhance_group_level_array") or [])
    except impl.Refused as exc:
        return _refused("team_meeting", exc)
    st["hold_turn"] = 0
    st["flags"].pop("overflow", None)
    chara_info["playing_state"] = 1
    _refresh(full_state, chara_info)
    smt = _smt()
    response = _envelope(career, ("chara_info", "home_info", "race_condition_array",
                                  "unchecked_event_array"))
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["race_condition_array"] = smt._build_race_condition_array(
        chara_info, chara_info.get("turn"), career["data"].get("race_history", []))
    data["breeders_data_set_team_meeting"] = {"enhance_group_array": impl.enhance_groups(st)}
    log.info("breeders: meeting -> %s (%s DP spent, %s left)", st["levels"], spent, st["dp"])
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_team_review(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 43:
        return _refused("team_review", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or impl.turn_of(chara_info))
    if not impl.review_pending(st, turn):
        return _refused("team_review", f"no Reflection pending on turn {turn}")
    _result, ev = impl.commit_review(full_state, chara_info, turn)
    CE.emit(full_state, ev)
    chara_info["playing_state"] = 5
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    data["unchecked_event_array"] = _serve_one(full_state, "team_review")
    data["breeders_data_set_team_review"] = {
        "team_review_result_array": impl.review_result_array(st)}
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_team_sp_training(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("team_sp_training", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    if impl.is_goal_turn(chara_info, turn):
        return _refused("team_sp_training", f"goal race on turn {turn}")
    home = career["data"].get("home_info") or {}
    tips = sorted({p for c in home.get("command_info_array") or () if c.get("command_type") == 1
                   for p in c.get("tips_event_partner_array") or ()})
    try:
        impl.activate_dreams(full_state, chara_info, turn, tips)
    except impl.Refused as exc:
        return _refused("team_sp_training", exc)
    _refresh(full_state, chara_info)
    response = _envelope(career, ("chara_info", "home_info"))
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    data["active_effect_ids"] = impl.active_effect_ids(st, chara_info)
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_finish_team_union_event(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 47:
        return _refused("finish_team_union_event", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    n = int(st["union_event"] or 0)
    st["union_event"] = 0
    if n and n not in st["union_done"]:
        st["union_done"].append(n)
    ev = producers.union_reward_event(n) if n else None
    if ev:
        CE.emit(full_state, ev)
    chara_info["playing_state"] = 1
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    data["unchecked_event_array"] = _serve_one(full_state, "finish_team_union_event")
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_bc_race_entry(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    if turn != impl.FINAL_TURN or int(chara_info.get("playing_state") or 1) != 1 or not st["bc_program"]:
        return _refused("bc_race_entry", f"turn {turn}, ps {chara_info.get('playing_state')}")
    smt = _smt()
    entered = smt._resolve_and_enter_race({"viewer_id": viewer_id, "program_id": st["bc_program"],
                                           "current_turn": turn})
    rsi = ((entered or {}).get("data") or {}).get("race_start_info")
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    impl.state(full_state)["bc_ctx"] = full_state.pop(smt.RACE_CTX_KEY, None)
    chara_info["playing_state"] = 44
    CE.emit(full_state, producers.bc_day_event())
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    data["unchecked_event_array"] = _serve_one(full_state, "bc_race_entry")
    data["race_start_info"] = copy.deepcopy(rsi)
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_member_bc_race(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 44:
        return _refused("member_bc_race", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    parked = st.pop("bc_ctx", None)
    if isinstance(parked, dict):
        full_state[_smt().RACE_CTX_KEY] = parked
    if not st["bc_results"]:
        rng = impl.rng_for(full_state, "member-bc")
        groups: dict = {}
        for m in st["members"]:
            g = M.members().get(int(m["chara_id"]), (0, 0))[0]
            if g and g != int(st["bc_group"]):
                groups.setdefault(g, []).append(int(m["member_id"]))
        st["bc_results"] = [{"bc_group_id": g, "win_member_id": rng.choice(ids)}
                            for g, ids in groups.items()]
    response = _envelope(career, ("chara_info", "home_info"))
    response["data"]["chara_info"] = copy.deepcopy(chara_info)
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_change_dress_option(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    impl.state(full_state)["dress"] = int(payload.get("value") or 0)
    response = _envelope(career, ())
    return _finish(viewer_id, full_state, career, chara_info, response)


HANDLERS = {
    "select_bc_race": handle_select_bc_race,
    "team_meeting": handle_team_meeting,
    "team_review": handle_team_review,
    "team_sp_training": handle_team_sp_training,
    "finish_team_union_event": handle_finish_team_union_event,
    "bc_race_entry": handle_bc_race_entry,
    "member_bc_race": handle_member_bc_race,
    "change_dress_option": handle_change_dress_option,
}
