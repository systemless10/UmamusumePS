
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ...result_codes import refuse
from ..venus import endpoints as VE
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


def _refused(where: str, why) -> dict:
    log.warning("legend %s refused: %s", where, why)
    return refuse()


def _no_data() -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


_envelope = VE._envelope
_finish = VE._finish


def _simulate(full_state: dict, viewer_id, chara_info: dict, sp: dict) -> None:
    impl.SIM_STATE["full_state"] = full_state
    try:
        VE._simulate(full_state, viewer_id, chara_info, sp)
    finally:
        impl.SIM_STATE.clear()


def handle_select_buff(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    try:
        impl.choose(full_state, int(payload.get("buff_id") or 0))
    except impl.Refused as exc:
        return _refused("select_buff", exc)
    _smt().state_store.save_state(viewer_id, full_state)
    return _no_data()


def handle_exchange_buff(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    try:
        impl.exchange(full_state, int(payload.get("before_buff_id") or 0),
                      int(payload.get("after_buff_id") or 0))
    except impl.Refused as exc:
        return _refused("exchange_buff", exc)
    _smt().state_store.save_state(viewer_id, full_state)
    return _no_data()


def handle_race_entry(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) not in (28, 29):
        return _refused("legend_race_entry", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    sp = st["sprace"]
    if not sp.get("program_id"):
        return _refused("legend_race_entry", "no Dream Fest announced")
    if not sp.get("race_start_info"):
        _simulate(full_state, viewer_id, chara_info, sp)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 29
    response = _envelope(career, chara_info, ("chara_info",))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_race_start(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (29, 30):
        return _refused("legend_race_start", f"playing_state {ps}")
    sp = impl.state(full_state)["sprace"]
    sp["stage"] = "started"
    chara_info["playing_state"] = 30
    response = _envelope(career, chara_info, ("chara_info",))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    response["data"]["race_scenario"] = sp.get("race_scenario")
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_race_end(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (30, 31):
        return _refused("legend_race_end", f"playing_state {ps}")
    smt = _smt()
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    program = int(sp.get("program_id") or 0)
    rank = int(sp.get("player_finish_order") or 1)
    group = int(sp.get("group") or impl.sprace_group(turn))
    cd = career["data"]
    if not sp.get("end_committed"):
        fans = smt._fan_gain_for_race(chara_info, program, rank, full_state=full_state)
        chara_info["fans"] = int(chara_info.get("fans") or 0) + fans
        ctx = dict(sp, turn=turn, program_id=program)
        smt._append_race_history(cd, ctx, chara_info, rank)
        st["races"] = [r for r in st["races"] if int(r["turn"]) != turn] + [
            {"race_type": M.RACE_TYPE.get(group, 1), "turn": turn, "result_rank": rank}]
        sp["gained_fans"] = fans
        sp["reward_info"] = smt._build_race_reward_info(program, rank, fans, cd.get("race_history"))
        if group == impl.LEGEND_RACE:
            st["route_id"] = impl.cm_route(cd.get("race_history") or [])
            sp["snapshot"] = impl.race_result_info(sp.get("race_start_info") or {}, sp.get("race_scenario"),
                                                   int(sp.get("race_instance_id") or 0))
            chara_info["state"] = 3
        sp["end_committed"] = True
    chara_info["playing_state"] = 31
    reward = copy.deepcopy(sp.get("reward_info") or {})
    reward.update(gained_fans=int(sp.get("gained_fans") or 0), result_rank=rank,
                  result_time=int(sp.get("player_finish_time") or 0))
    response = _envelope(career, chara_info, ("chara_info",))
    data = response["data"]
    data["race_reward_info"] = reward
    data["add_trophy_info"], data["trophy_reward_info"] = smt._race_trophy_award(
        full_state, viewer_id, sp, chara_info, rank)
    data["race_add_reward_info"] = []
    data["race_history"] = smt._wire_race_history(cd.get("race_history") or [])
    data["reward_summary_info"] = VE._summary(smt._credit_race_reward(full_state, sp))
    data["win_saddle_id_array"] = VE._win_saddles(cd.get("race_history") or [])
    data["add_music"] = None
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_race_out(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (31, 5):
        return _refused("legend_race_out", f"playing_state {ps}")
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    won = int(sp.get("player_finish_order") or 1) == 1
    if sp.get("stage") != "out":
        ev = producers.result_event(full_state, chara_info, turn, won)
        if ev:
            CE.emit(full_state, ev)
        sp["stage"] = "out"
    chara_info["playing_state"] = 5
    response = _envelope(career, chara_info, ("chara_info", "unchecked_event_array"))
    wire, _ev = CE.serve_next(full_state, "legend_race_out")
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_race_continue(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    smt = _smt()
    st = impl.state(full_state)
    sp = st["sprace"]
    if int(chara_info.get("playing_state") or 1) != 31 or int(sp.get("player_finish_order") or 1) == 1:
        return _refused("legend_race_continue", "nothing to retry")
    ctype = int(payload.get("continue_type") or 1)
    counts = smt._continue_counts(full_state, sp)
    if ctype == 2:
        if counts["available_continue_num"] <= 0 or not smt._spend_alarm_clock(full_state):
            return _refused("legend_race_continue", "no alarm clock")
        sp["continue_paid_used"] = int(sp.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return _refused("legend_race_continue", "no free retry")
        sp["continue_free_used"] = int(sp.get("continue_free_used") or 0) + 1
    cd = career["data"]
    if sp.get("end_committed"):
        turn = int(sp.get("turn") or 0)
        chara_info["fans"] = max(0, int(chara_info.get("fans") or 0) - int(sp.get("gained_fans") or 0))
        hist = cd.get("race_history") or []
        if hist and hist[-1].get("program_id") == sp.get("program_id") and hist[-1].get("turn") == turn:
            hist.pop()
        st["races"] = [r for r in st["races"] if int(r["turn"]) != turn]
        if int(chara_info.get("state") or 0) == 3:
            chara_info["state"] = 0
        sp["end_committed"] = False
    sp["continue_used"] = int(sp.get("continue_used") or 0) + 1
    sp.pop("race_start_info", None)
    _simulate(full_state, viewer_id, chara_info, sp)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 29
    if isinstance(cd.get("home_info"), dict):
        cd["home_info"].update(smt._continue_counts(full_state, sp))
    response = _envelope(career, chara_info, ("chara_info", "home_info"))
    response["data"]["race_start_info"] = copy.deepcopy(sp.get("race_start_info") or {})
    response["data"]["user_item"] = {"item_id": smt._ALARM_CLOCK_ITEM,
                                     "number": smt._alarm_clock_stock(full_state)}
    return _finish(viewer_id, full_state, career, chara_info, response)


def _screen_end(payload: dict, where: str, want_ps: int, home: bool) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (want_ps, 5):
        return _refused(where, f"playing_state {ps}")
    chara_info["playing_state"] = 5
    if where == "popularity_end":
        impl.state(full_state)["popularity"] = {}
    keys = ("chara_info", "unchecked_event_array") + (("home_info",) if home else ())
    response = _envelope(career, chara_info, keys)
    pending = CE.active(full_state)
    wire = pending.wire_entry() if pending else CE.serve_next(full_state, where)[0]
    response["data"]["unchecked_event_array"] = [wire] if wire else []
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_popularity_end(payload: dict) -> dict:
    return _screen_end(payload, "popularity_end", 33, home=True)


def handle_cm_end(payload: dict) -> dict:
    return _screen_end(payload, "cm_end", 32, home=False)


HANDLERS = {
    "select_buff": handle_select_buff,
    "exchange_buff": handle_exchange_buff,
    "legend_race_entry": handle_race_entry,
    "legend_race_start": handle_race_start,
    "legend_race_end": handle_race_end,
    "legend_race_out": handle_race_out,
    "legend_race_continue": handle_race_continue,
    "popularity_end": handle_popularity_end,
    "cm_end": handle_cm_end,
}

