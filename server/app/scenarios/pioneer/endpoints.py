
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from . import impl, master as M

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


def _null_home_params(data: dict) -> None:
    for c in (data.get("home_info") or {}).get("command_info_array") or ():
        c["params_inc_dec_info_array"] = None


def _next_events(full_state: dict, chara_info: dict) -> list:
    return _smt()._drain_pending(full_state, None, int(chara_info.get("card_id") or 0) // 100)


def _refused(where: str, why) -> dict:
    log.warning("pioneer %s refused: %s", where, why)
    return refuse()


def _ok(data: dict | None = None) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1}, "data": data or {}}


def handle_planning(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 35:
        return _refused("planning", f"playing_state {chara_info.get('playing_state')}")
    plan = impl.validate_plan(full_state, chara_info, payload.get("planning_info_array"))
    if plan is None:
        return _refused("planning", f"illegal plan {payload.get('planning_info_array')} "
                                    f"(cost {impl.planning_cost(full_state, chara_info)})")
    st = impl.state(full_state)
    impl.commit_plan(full_state, chara_info, plan, impl.turn_of(chara_info))
    st["hold_turn"] = 0
    chara_info["playing_state"] = 1
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "race_condition_array",
                                  "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    smt = _smt()
    if "race_condition_array" in data:
        data["race_condition_array"] = smt._build_race_condition_array(
            chara_info, chara_info.get("turn"), career["data"].get("race_history", []))
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    log.info("pioneer: plan %s", [r["target_state_id"] for r in plan])
    return response


def handle_check_point(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 34:
        return _refused("check_point", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    if not impl.check_point_due(st, turn):
        return _refused("check_point", f"no evaluation due on turn {turn}")
    _result, ev = impl.commit_check_point(full_state, chara_info, turn)
    CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "check_point")
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_shima_training_exec(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    _viewer, full_state, _career_state, chara_info = got
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    if (int(chara_info.get("playing_state") or 1) != 1 or int(st["tickets"]) < 1
            or impl.island_turn(turn)):
        return _refused("shima_training_exec",
                        f"turn {turn}, playing_state {chara_info.get('playing_state')}, "
                        f"tickets {st['tickets']}")
    pv = impl.stored(st, turn).get("island") or {}
    fields = dict(payload)
    fields.update(command_type=1, command_id=M.ISLAND_TRAINING, command_group_id=0, select_id=0,
                  current_turn=payload.get("current_turn") or turn,
                  current_vital=chara_info.get("vital", 100))
    response = _smt().handle_exec_command(fields)
    data = (response or {}).get("data")
    if isinstance(data, dict):
        data["result_base_params_inc_dec_info_array"] = [
            dict(p) for p in pv.get("params") or ()]
        data["result_bonus_params_inc_dec_info_array"] = [
            dict(p) for p in pv.get("bonus") or ()]
        data["command_result"] = {"command_id": M.ISLAND_TRAINING, "sub_id": 1, "result_state": 2}
        after = _smt().state_store.get_state(_viewer) or {}
        capped = impl.state(after)["flags"].pop("not_up_bond", None)
        if capped is not None:
            _smt().state_store.save_state(_viewer, after)
        if capped:
            info = data.setdefault("not_up_parameter_info", {})
            info["evaluation_chara_id_array"] = list(capped)
        for key in list(data):
            if key not in ("chara_info", "home_info", "not_up_parameter_info", "not_down_parameter_info",
                           "race_condition_array", "unchecked_event_array", "pioneer_data_set",
                           "command_result", "gain_parameter_info", "gain_partner_support_effect_array",
                           "result_base_params_inc_dec_info_array",
                           "result_bonus_params_inc_dec_info_array"):
                data.pop(key, None)
    return response


def handle_pioneer_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 39:
        return _refused("pioneer_live", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    st["live"] = 1
    chara_info["playing_state"] = 38
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    from ... import concerts
    data["add_music"] = concerts.on_scenario(full_state, "pioneer_live")
    _null_home_params(data)
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_pioneer_complete(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 38:
        return _refused("pioneer_complete", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    st["complete"] = 1
    st["hold_turn"] = 0
    st["tickets"] = 0
    chara_info["playing_state"] = 1
    from . import producers
    ev = producers.complete_event()
    if ev:
        CE.emit(full_state, ev)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["check_point_facility_info_array"] = impl.facility_snapshots(st)
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_priority_facility_save(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, _career_state, _ci = got
    st = impl.state(full_state)
    st["auto"]["is_auto_deploy"] = int(payload.get("is_auto_deploy") or 0)
    st["auto"]["priority_facility_array"] = [
        {"priority": int(p.get("priority") or 0), "facility_state_id": int(p.get("facility_state_id") or 0)}
        for p in payload.get("priority_facility_array") or () if isinstance(p, dict)]
    _smt().state_store.save_state(viewer_id, full_state)
    return _ok()


def handle_auto_planning_dialog_checked(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, _career_state, _ci = got
    impl.state(full_state)["auto"]["is_checked_dialog"] = 1
    _smt().state_store.save_state(viewer_id, full_state)
    return _ok()


HANDLERS = {
    "planning": handle_planning,
    "check_point": handle_check_point,
    "shima_training_exec": handle_shima_training_exec,
    "pioneer_live": handle_pioneer_live,
    "pioneer_complete": handle_pioneer_complete,
    "priority_facility_save": handle_priority_facility_save,
    "auto_planning_dialog_checked": handle_auto_planning_dialog_checked,
}
