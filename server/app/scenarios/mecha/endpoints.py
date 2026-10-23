
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from . import formula, impl

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


def _refused(where: str, why) -> dict:
    log.warning("mecha %s refused: %s", where, why)
    return refuse()


def handle_exec_command(payload: dict) -> dict:
    fields = dict(payload)
    cid = fields.get("command_id")
    if isinstance(cid, int) and cid in formula.SERVER_ID:
        fields["command_id"] = formula.SERVER_ID[cid]
    return _smt().handle_exec_command(fields)


def handle_overdrive(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("overdrive", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    why = impl.can_overdrive(st, turn)
    if why:
        return _refused("overdrive", why)
    not_up = impl.activate_overdrive(st, chara_info, turn)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "not_up_parameter_info"))
    _smt()._note_not_up(response["data"], not_up)
    _smt().state_store.save_state(viewer_id, full_state)
    return response


def handle_tuning(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 26:
        return _refused("tuning", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    why = impl.set_chips(st, payload.get("board_info_array"))
    if why:
        return _refused("tuning", why)
    st["hold_turn"] = 0
    chara_info["playing_state"] = 1
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = _next_events(full_state, chara_info)
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    log.info("mecha: tuning %s, %s EN left", {k: v for k, v in st["chips"].items() if v}, st["tp"])
    return response


def handle_upgrade_race(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 25:
        return _refused("upgrade_race", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    if not impl.uge_pending(st, turn):
        return _refused("upgrade_race", f"no UGE pending on turn {turn}")
    from . import producers
    sid, grade = impl.commit_uge(st, turn)
    ev = producers.uge_result_event(full_state, chara_info, sid, grade, turn)
    if ev:
        CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "upgrade_race")
    chara_info["playing_state"] = 5
    impl.flag(st, "new_result_first", sid)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_mecha_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 27:
        return _refused("mecha_live", f"playing_state {chara_info.get('playing_state')}")
    from ... import concerts
    song = concerts.on_scenario(full_state, "mecha_live")
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    shown = copy.deepcopy(full_state.get(_smt().DISPLAY_EVENT_KEY) or [])
    data["unchecked_event_array"] = shown or _next_events(full_state, chara_info)
    data["add_music"] = song
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


HANDLERS = {
    "exec_command": handle_exec_command,
    "overdrive": handle_overdrive,
    "tuning": handle_tuning,
    "upgrade_race": handle_upgrade_race,
    "mecha_live": handle_mecha_live,
}
