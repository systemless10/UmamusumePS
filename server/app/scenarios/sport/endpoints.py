
from __future__ import annotations

import logging

from ... import career_events as CE
from ...result_codes import refuse
from ..cook.endpoints import _envelope, _next_events
from . import impl

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


def _refused(where: str, why) -> dict:
    log.warning("sport %s refused: %s", where, why)
    return refuse()


def handle_competition(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != impl.PS_COMPETITION:
        return _refused("competition", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    if not impl.compe_pending(st, turn):
        return _refused("competition", f"no competition pending on turn {turn}")
    from . import producers
    result = impl.run_competition(st, turn)
    ev = producers.compe_result_event(chara_info, result, turn)
    if ev:
        CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "competition")
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "unchecked_event_array"))
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    log.info("sport: competition %s -> %s (%s wins)", result["compe_type"], result["result_state"],
             len(result["win_command_id_array"]))
    return response


def handle_competition_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != impl.PS_COMPETITION_LIVE:
        return _refused("competition_live", f"playing_state {chara_info.get('playing_state')}")
    from ... import concerts
    st = impl.state(full_state)
    song = concerts.on_scenario(full_state, "sport_live")
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


def handle_use_item(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("use_item", f"playing_state {chara_info.get('playing_state')}")
    if int(payload.get("item_id") or 0) != impl.ITEM_CHANGE:
        return _refused("use_item", f"item {payload.get('item_id')}")
    turn = impl.turn_of(chara_info)
    why = impl.use_item(full_state, turn, int(payload.get("select_id_1") or 0),
                        int(payload.get("select_id_2") or 0))
    if why:
        return _refused("use_item", why)
    impl.RI.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info"))
    _smt().state_store.save_state(viewer_id, full_state)
    return response


HANDLERS = {
    "competition": handle_competition,
    "competition_live": handle_competition_live,
    "use_item": handle_use_item,
}
