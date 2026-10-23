
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


def _next_events(full_state: dict, chara_info: dict) -> list:
    smt = _smt()
    return smt._drain_pending(full_state, None, int(chara_info.get("card_id") or 0) // 100)


def _refused(where: str, why) -> dict:
    log.warning("cook %s refused: %s", where, why)
    return refuse()


def handle_cooking(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("cooking", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    dish_id = int(payload.get("dish_id") or 0)
    if dish_id not in impl.available_dishes(st):
        return _refused("cooking", f"dish {dish_id} not available")
    if impl.dish_today(st, turn):
        return _refused("cooking", f"already cooked on turn {turn}")
    if not impl.can_afford(st, dish_id):
        return _refused("cooking", f"materials short for dish {dish_id}")
    out = impl.cook(full_state, chara_info, dish_id, turn)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "not_up_parameter_info"))
    _smt()._note_not_up(response["data"], out["not_up"])
    response["data"]["cook_num_array"] = [{"dish_group_id": g, "cook_num": n} for g, n in st["cook_num"]]
    _smt().state_store.save_state(viewer_id, full_state)
    return response


def handle_facility_level_up(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("facility_level_up", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    why = impl.level_up(st, payload.get("facility_level_up_info_array"))
    if why:
        return _refused("facility_level_up", why)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info"))
    _smt().state_store.save_state(viewer_id, full_state)
    log.info("cook: garden %s, %s care points left", st["garden"], st["care_point"])
    return response


def handle_tasting(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 23:
        return _refused("tasting", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    ct = M.tasting_for_turn(turn)
    if not ct or impl.tasted(st, ct):
        return _refused("tasting", f"no tasting pending on turn {turn}")
    from . import producers
    result = impl.commit_tasting(st, ct)
    impl.flag(st, "new_result_first", ct)
    ev = producers.tasting_result_event(chara_info, ct, result, turn)
    if ev:
        CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "tasting")
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_tasting_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 24:
        return _refused("tasting_live", f"playing_state {chara_info.get('playing_state')}")
    from ... import concerts
    st = impl.state(full_state)
    song = concerts.on_scenario(full_state, "cook_live")
    st["hold_turn"] = 0
    chara_info["playing_state"] = 1
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["add_music"] = song
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_flavor_text_read(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    impl.read_flavors(full_state, payload.get("flavor_text_id_array"))
    response = _envelope(career, ())
    commands = (career["data"].get("home_info") or {}).get("command_info_array") or []
    response["data"]["cook_data_set"] = impl.data_set(
        full_state, chara_info, commands, impl.turn_of(chara_info), full=True)
    _smt().state_store.save_state(viewer_id, full_state)
    return response


HANDLERS = {
    "cooking": handle_cooking,
    "facility_level_up": handle_facility_level_up,
    "tasting": handle_tasting,
    "tasting_live": handle_tasting_live,
    "flavor_text_read": handle_flavor_text_read,
}
