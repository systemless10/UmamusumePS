
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from ..venus import endpoints as VE
from . import impl, master as M, producers

log = logging.getLogger("uma-server")

SS_COMMAND_TYPE = 61
INVALID = 102


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


def _refused(where: str, why, code: int | None = None) -> dict:
    log.warning("arc %s refused: %s", where, why)
    return refuse(code) if code is not None else refuse()


_envelope = VE._envelope
_finish = VE._finish


def handle_selection_exec(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    _viewer, full_state, _career_state, chara_info = got
    turn = impl.turn_of(chara_info)
    st = impl.state(full_state)
    lu = st.get("lineup") or {}
    if int(chara_info.get("playing_state") or 1) != 1 or int(lu.get("turn") or 0) != turn \
            or not lu.get("members") or lu.get("spent"):
        return _refused("selection_exec", f"turn {turn}, lineup {lu.get('members')}")
    fields = dict(payload)
    fields.update(command_type=SS_COMMAND_TYPE, command_id=0, command_group_id=0, select_id=0,
                  current_turn=payload.get("current_turn") or turn,
                  current_vital=chara_info.get("vital", 100))
    response = _smt().handle_exec_command(fields)
    data = (response or {}).get("data")
    if isinstance(data, dict):
        after = _smt().state_store.get_state(payload["viewer_id"]) or {}
        result = impl.flag(impl.state(after), "match_result") or {}
        _smt().state_store.save_state(payload["viewer_id"], after)
        for key in list(data):
            if key not in ("chara_info", "home_info", "not_up_parameter_info", "race_condition_array",
                           "unchecked_event_array", "arc_data_set"):
                data.pop(key, None)
        data["selection_result_info"] = list(result.get("results") or ())
        data["result_bonus_params_inc_dec_info_array"] = list(result.get("bonus") or ())
    return response


def handle_potential_level_up(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (1, 16):
        return _refused("potential_level_up", f"playing_state {ps}")
    try:
        impl.level_up(full_state, chara_info, payload.get("potential_level_up_info_array") or ())
    except impl.Invalid as exc:
        return _refused("potential_level_up", exc, INVALID)
    except impl.Refused as exc:
        return _refused("potential_level_up", exc)
    impl.RI.refresh_home(full_state, chara_info)
    response = _envelope(career, chara_info, ("chara_info", "home_info", "not_up_parameter_info"))
    _smt()._note_not_up(response["data"], [])
    return _finish(viewer_id, full_state, career, chara_info, response)


def _race_turn(full_state: dict, chara_info: dict) -> int:
    return int(impl.state(full_state).get("hold_turn") or 0)


def _simulate(full_state: dict, viewer_id, chara_info: dict, sp: dict) -> None:
    VE._simulate(full_state, viewer_id, chara_info, sp)
    sp["race_instance_id"] = M.program_instance(int(sp["program_id"]))
    patch_members(sp.get("race_start_info"))


def patch_members(rsi) -> None:
    for h in (rsi or {}).get("race_horse_data") or ():
        if impl.TRAINEE_SLOT_BASE < int(h.get("single_mode_chara_id") or 0) <= impl.TRAINEE_SLOT_BASE + 15:
            h["card_id"] = 0
            h["npc_type"] = 0
            h["rarity"] = 3


def _ensure_race(full_state, viewer_id, chara_info) -> dict | None:
    st = impl.state(full_state)
    turn = _race_turn(full_state, chara_info)
    sp = st["sprace"]
    if not turn or int(sp.get("turn") or 0) != turn or not sp.get("program_id"):
        return None
    if not sp.get("race_start_info"):
        _simulate(full_state, viewer_id, chara_info, sp)
    return sp


def handle_arc_race_analyze(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 16:
        return _refused("arc_race_analyze", f"playing_state {chara_info.get('playing_state')}", INVALID)
    sp = _ensure_race(full_state, viewer_id, chara_info)
    if sp is None:
        return _refused("arc_race_analyze", "no exhibition", INVALID)
    _smt().state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"race_horse_data_array": copy.deepcopy(
                (sp.get("race_start_info") or {}).get("race_horse_data") or [])}}


def handle_arc_race_entry(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) not in (16, 17):
        return _refused("arc_race_entry", f"playing_state {chara_info.get('playing_state')}", INVALID)
    sp = _ensure_race(full_state, viewer_id, chara_info)
    if sp is None:
        return _refused("arc_race_entry", "no exhibition", INVALID)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 17
    response = _envelope(career, chara_info, ("chara_info",))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_arc_race_start(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (17, 18):
        return _refused("arc_race_start", f"playing_state {ps}")
    st = impl.state(full_state)
    sp = st["sprace"]
    if not sp.get("points_paid"):
        impl.pay_race_points(full_state, int(sp["program_id"]), int(sp.get("player_finish_order") or 99),
                             member_places(sp))
        sp["points_paid"] = True
    sp["stage"] = "started"
    chara_info["playing_state"] = 18
    response = _envelope(career, chara_info, ("chara_info",))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    response["data"]["race_scenario"] = sp.get("race_scenario")
    return _finish(viewer_id, full_state, career, chara_info, response)


def member_places(sp: dict) -> dict:
    st_members = {}
    for chara, place in (sp.get("rival_finish_orders") or {}).items():
        st_members[int(chara)] = int(place)
    return st_members


def handle_arc_race_end(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (18, 19):
        return _refused("arc_race_end", f"playing_state {ps}")
    smt = _smt()
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    program = int(sp.get("program_id") or 0)
    rank = int(sp.get("player_finish_order") or 1)
    cd = career["data"]
    if not sp.get("end_committed"):
        fans = smt._fan_gain_for_race(chara_info, program, rank, full_state=full_state)
        chara_info["fans"] = int(chara_info.get("fans") or 0) + fans
        st["races"] = [r for r in st["races"] if int(r["turn"]) != turn] + [
            {"race_num": M.EXHIBITION_NUM.get(program, 0), "turn": turn, "result_rank": rank}]
        sp["gained_fans"] = fans
        sp["reward_info"] = smt._build_race_reward_info(program, rank, fans, cd.get("race_history"))
        sp["end_committed"] = True
    chara_info["playing_state"] = 19
    reward = copy.deepcopy(sp.get("reward_info") or {})
    reward.update(gained_fans=int(sp.get("gained_fans") or 0), result_rank=rank,
                  result_time=int(sp.get("player_finish_time") or 0))
    response = _envelope(career, chara_info, ("chara_info",))
    data = response["data"]
    data["race_reward_info"] = reward
    data["add_trophy_info"], data["trophy_reward_info"] = smt._race_trophy_award(
        full_state, viewer_id, sp, chara_info, rank)
    data["race_add_reward_info"] = []
    data["reward_summary_info"] = VE._summary(smt._credit_race_reward(full_state, sp))
    data["win_saddle_id_array"] = []
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_arc_race_out(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (19, 5):
        return _refused("arc_race_out", f"playing_state {ps}")
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    if sp.get("stage") != "out":
        ev = producers.race_result_event(full_state, chara_info, {
            "program_id": int(sp.get("program_id") or 0), "rank": int(sp.get("player_finish_order") or 99),
            "turn": turn})
        if ev:
            CE.emit(full_state, ev)
        sp["stage"] = "out"
    chara_info["playing_state"] = 5
    response = _envelope(career, chara_info, ("chara_info", "unchecked_event_array"))
    wire, _ev = CE.serve_next(full_state, "arc_race_out")
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_arc_race_continue(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    smt = _smt()
    st = impl.state(full_state)
    sp = st["sprace"]
    if int(chara_info.get("playing_state") or 1) != 19 or int(sp.get("player_finish_order") or 1) == 1:
        return _refused("arc_race_continue", "nothing to retry")
    ctype = int(payload.get("continue_type") or 1)
    counts = smt._continue_counts(full_state, sp)
    if ctype == 2:
        if counts["available_continue_num"] <= 0 or not smt._spend_alarm_clock(full_state):
            return _refused("arc_race_continue", "no alarm clock")
        sp["continue_paid_used"] = int(sp.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return _refused("arc_race_continue", "no free retry")
        sp["continue_free_used"] = int(sp.get("continue_free_used") or 0) + 1
    if sp.get("end_committed"):
        chara_info["fans"] = max(0, int(chara_info.get("fans") or 0) - int(sp.get("gained_fans") or 0))
        st["races"] = [r for r in st["races"] if int(r["turn"]) != int(sp.get("turn") or 0)]
        sp["end_committed"] = False
    sp["continue_used"] = int(sp.get("continue_used") or 0) + 1
    sp.pop("race_start_info", None)
    _simulate(full_state, viewer_id, chara_info, sp)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 17
    cd = career["data"]
    if isinstance(cd.get("home_info"), dict):
        cd["home_info"].update(smt._continue_counts(full_state, sp))
    response = _envelope(career, chara_info, ("chara_info", "home_info"))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    response["data"]["user_item"] = {"item_id": smt._ALARM_CLOCK_ITEM,
                                     "number": smt._alarm_clock_stock(full_state)}
    return _finish(viewer_id, full_state, career, chara_info, response)


HANDLERS = {
    "selection_exec": handle_selection_exec,
    "potential_level_up": handle_potential_level_up,
    "arc_race_analyze": handle_arc_race_analyze,
    "arc_race_entry": handle_arc_race_entry,
    "arc_race_start": handle_arc_race_start,
    "arc_race_end": handle_arc_race_end,
    "arc_race_out": handle_arc_race_out,
    "arc_race_continue": handle_arc_race_continue,
}
