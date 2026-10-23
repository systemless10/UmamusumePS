
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from . import impl, master as M

log = logging.getLogger("uma-server")

PR_COMMAND_TYPE = 12


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
    smt = _smt()
    return smt._drain_pending(full_state, None, int(chara_info.get("card_id") or 0) // 100)


def _refused(where: str, why) -> dict:
    log.warning("onsen %s refused: %s", where, why)
    return refuse()


def _blue_groups(viewer_id, chara_info: dict) -> list:
    from ... import master_data
    smt = _smt()
    roster = smt._roster_by_trained_id(viewer_id)
    lists = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        parent = roster.get(chara_info.get(key)) if chara_info.get(key) else None
        if not parent:
            continue
        lists.append(parent.get("factor_info_array") or [])
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        lists += [by_pos[p].get("factor_info_array") or [] for p in (10, 20) if p in by_pos]
    groups = []
    for factors in lists:
        for f in factors:
            row = master_data.query_one(
                "SELECT factor_type, factor_group_id FROM succession_factor WHERE factor_id=?",
                (f.get("factor_id"),))
            if row and int(row["factor_type"]) == 1:
                groups.append(int(row["factor_group_id"]))
    return groups


def handle_select_dig_onsen(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 36:
        return _refused("select_dig_onsen", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    onsen = int(payload.get("onsen_id") or 0)
    item = int(payload.get("item_id") or 0)
    state_now = impl.spring_state(st, onsen)
    if onsen == M.BASE_ONSEN or onsen in st["dug"] or state_now == 0:
        return _refused("select_dig_onsen", f"spring {onsen} (state {state_now}, dug {st['dug']})")
    if str(item) not in st["items"]:
        return _refused("select_dig_onsen", f"item {item}")
    if not st["enabled"]:
        st["enabled"] = True
        try:
            st["blue"] = _blue_groups(viewer_id, chara_info)
        except Exception:
            log.exception("onsen: lineage blue factors unreadable; no gene bonus")
            st["blue"] = []
    st["selected"] = onsen
    st["items"][str(item)] = min(M.max_item_level(), int(st["items"][str(item)]) + 1)
    st["flags"].pop("not_gain_ticket", None)
    hold = int(st.get("hold_turn") or 0)
    if hold and impl.party_pending(st, hold):
        chara_info["playing_state"] = 5
    else:
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
    log.info("onsen: spring %s selected, tool %s -> level %s", onsen, item, st["items"][str(item)])
    return response


def handle_bathing(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("bathing", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    st["flags"].pop("not_gain_ticket", None)
    try:
        not_up = impl.bathe(full_state, chara_info, impl.turn_of(chara_info))
    except impl.Refused as exc:
        return _refused("bathing", exc)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "not_up_parameter_info"))
    _smt()._note_not_up(response["data"], not_up)
    _smt().state_store.save_state(viewer_id, full_state)
    return response


def handle_assistant_exec(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    _viewer, full_state, _career_state, chara_info = got
    turn = impl.turn_of(chara_info)
    if int(chara_info.get("playing_state") or 1) != 1 or not impl.pr_enabled(turn):
        return _refused("assistant_exec", f"turn {turn}, playing_state {chara_info.get('playing_state')}")
    fields = dict(payload)
    fields.update(command_type=PR_COMMAND_TYPE, command_id=0, command_group_id=0, select_id=0,
                  current_turn=payload.get("current_turn") or turn,
                  current_vital=chara_info.get("vital", 100))
    response = _smt().handle_exec_command(fields)
    data = (response or {}).get("data")
    if isinstance(data, dict):
        for key in list(data):
            if key not in ("chara_info", "home_info", "not_up_parameter_info",
                           "race_condition_array", "unchecked_event_array", "onsen_data_set"):
                data.pop(key, None)
    return response


def handle_check_dug_result(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 37:
        return _refused("check_dug_result", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    if not impl.party_pending(st, turn):
        return _refused("check_dug_result", f"no party pending on turn {turn}")
    _result, ev = impl.commit_party(full_state, chara_info, turn)
    CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "check_dug_result")
    chara_info["playing_state"] = 5
    impl.flag(st, "new_result_first", st["new_result"])
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    _null_home_params(response["data"])
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_onsen_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 42:
        return _refused("onsen_live", f"playing_state {chara_info.get('playing_state')}")
    from . import producers
    ev = producers.kiyoko_ending(chara_info)
    if ev:
        CE.emit(full_state, ev)
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["add_music"] = None
    _null_home_params(data)
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


HANDLERS = {
    "select_dig_onsen": handle_select_dig_onsen,
    "bathing": handle_bathing,
    "assistant_exec": handle_assistant_exec,
    "check_dug_result": handle_check_dug_result,
    "onsen_live": handle_onsen_live,
}
