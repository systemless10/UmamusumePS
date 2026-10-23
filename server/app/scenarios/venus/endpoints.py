
from __future__ import annotations

import copy
import logging

from ... import career_events as CE
from ... import master_data
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


def _envelope(career: dict, chara_info: dict, keys) -> dict:
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    home = career["data"].get("home_info")
    if isinstance(home, dict):
        data["home_info"] = copy.deepcopy(home)
    data["unchecked_event_array"] = []
    for key in list(data):
        if key not in keys:
            data.pop(key, None)
    return response


def _refused(where: str, why) -> dict:
    log.warning("venus %s refused: %s", where, why)
    return refuse()


def _finish(viewer_id, full_state: dict, career: dict, chara_info: dict, response: dict) -> dict:
    smt = _smt()
    career["data"]["chara_info"] = chara_info
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _race_turn(full_state: dict, chara_info: dict, payload: dict) -> int:
    return int(impl.state(full_state).get("hold_turn") or payload.get("current_turn")
               or impl.turn_of(chara_info))


def handle_spirit_use(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("spirit_use", f"playing_state {chara_info.get('playing_state')}")
    turn = impl.turn_of(chara_info)
    try:
        impl.use_wisdom(full_state, chara_info, turn)
    except impl.Refused as exc:
        return _refused("spirit_use", exc)
    impl.RI.refresh_home(full_state, chara_info)
    home = career["data"].get("home_info")
    if isinstance(home, dict):
        impl.refresh_preview(full_state, chara_info, home, _smt()._support_card_levels(full_state))
    response = _envelope(career, chara_info, ("chara_info", "home_info"))
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_spirit_history(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    st = impl.state(full_state)
    response = _envelope(career, chara_info, ())
    response["data"]["spirit_history_info_array"] = copy.deepcopy(st["uses"])
    return response


def handle_race_entry(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 12:
        return _refused("venus_race_entry", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = _race_turn(full_state, chara_info, payload)
    if int(sp.get("turn") or 0) != turn or not sp.get("program_id"):
        return _refused("venus_race_entry", f"no special race on turn {turn}")
    if not sp.get("race_start_info"):
        _simulate(full_state, viewer_id, chara_info, sp)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 13
    response = _envelope(career, chara_info, ("chara_info",))
    return _finish(viewer_id, full_state, career, chara_info, response)


def _simulate(full_state: dict, viewer_id, chara_info: dict, sp: dict) -> None:
    smt = _smt()
    from ...fixtures import templates as fixtures
    program = int(sp["program_id"])
    style = smt._player_running_style(full_state, chara_info)
    pair = fixtures.first("single_mode/race_entry") or fixtures.first("single_mode_team/race_entry")
    rsi = copy.deepcopy(((pair.response_copy() if pair else {}).get("data") or {}).get("race_start_info") or {})
    smt._patch_player_horse(rsi, chara_info, running_style=style)
    rsi["program_id"] = program
    rsi.update({k: v for k, v in smt._career_race_conditions(program, chara_info).items()
                if k != "frame_order"})
    rsi["continue_num"] = int(sp.get("continue_used") or 0)
    rsi.setdefault("is_force_running_style", 0)
    rsi.setdefault("sub_weather", 0)
    sim = smt._simulate_career_race(chara_info, viewer_id, M.program_instance(program),
                                    running_style=style, program_id=program)
    if sim:
        rsi["race_horse_data"] = sim["race_horse_data"]
        rsi["random_seed"] = sim["random_seed"]
    sp.update({
        "race_start_info": rsi, "race_scenario": (sim or {}).get("race_scenario"),
        "player_finish_order": int((sim or {}).get("player_finish_order") or 1),
        "player_finish_time": (sim or {}).get("player_finish_time"),
        "player_popularity": (sim or {}).get("player_popularity"),
        "rival_finish_orders": (sim or {}).get("rival_finish_orders"),
        "running_style": style, "race_instance_id": M.program_instance(program),
    })
    log.info("venus: special race %s simulated -> finish %s", program, sp["player_finish_order"])


def handle_race_start(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (13, 14):
        return _refused("venus_race_start", f"playing_state {ps}")
    impl.state(full_state)["sprace"]["stage"] = "started"
    chara_info["playing_state"] = 14
    response = _envelope(career, chara_info, ("chara_info",))
    return _finish(viewer_id, full_state, career, chara_info, response)


def handle_race_end(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (14, 15):
        return _refused("venus_race_end", f"playing_state {ps}")
    smt = _smt()
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    program = int(sp.get("program_id") or 0)
    rank = int(sp.get("player_finish_order") or 1)
    won = rank == 1
    cd = career["data"]
    if not sp.get("end_committed"):
        fans = smt._fan_gain_for_race(chara_info, program, rank, full_state=full_state)
        chara_info["fans"] = int(chara_info.get("fans") or 0) + fans
        group = int(sp.get("group") or impl.sprace_group(turn))
        if group != 50001:
            ctx = dict(sp, turn=turn, program_id=program)
            smt._append_race_history(cd, ctx, chara_info, rank)
        st["races"] = [r for r in st["races"] if int(r["turn"]) != turn] + [
            {"race_num": impl.race_num(group, program), "turn": turn, "result_rank": rank}]
        impl.add_race_points(full_state, won)
        sp["gained_fans"] = fans
        sp["reward_info"] = smt._build_race_reward_info(program, rank, fans, cd.get("race_history"))
        sp["end_committed"] = True
    chara_info["playing_state"] = 15
    reward = copy.deepcopy(sp.get("reward_info") or {})
    reward.update(gained_fans=int(sp.get("gained_fans") or 0), result_rank=rank,
                  result_time=int(sp.get("player_finish_time") or 0))
    sp["race_reward_info"] = reward
    response = _envelope(career, chara_info, ("chara_info",))
    data = response["data"]
    data["add_trophy_info"], data["trophy_reward_info"] = smt._race_trophy_award(
        full_state, viewer_id, sp, chara_info, rank)
    data["race_add_reward_info"] = []
    data["race_history"] = smt._wire_race_history(cd.get("race_history") or [])
    data["reward_summary_info"] = _summary(smt._credit_race_reward(full_state, sp))
    data["win_saddle_id_array"] = _win_saddles(cd.get("race_history") or [])
    return _finish(viewer_id, full_state, career, chara_info, response)


def _summary(credited: dict) -> dict:
    return {"add_card_bonus_info": None, "add_card_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_fcoin": credited["add_fcoin"], "add_honor_list": [],
            "add_item_list": copy.deepcopy(credited["add_item_list"]),
            "add_music_list": [], "add_piece_list": [], "add_present_num": 0,
            "add_story_id_array": [], "add_support_card_limit_break_array": [],
            "add_support_card_list": [], "add_support_card_num_array": [],
            "add_total_fan": 0, "force_update_honor_id": 0, "new_chara_profile_array": []}


def _win_saddles(history: list) -> list:
    try:
        from ...handlers import trained_chara
        return trained_chara._win_saddles_from_results(history)
    except Exception:
        return []


def handle_race_out(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    ps = int(chara_info.get("playing_state") or 1)
    if ps not in (15, 5):
        return _refused("venus_race_out", f"playing_state {ps}")
    st = impl.state(full_state)
    sp = st["sprace"]
    turn = int(sp.get("turn") or 0)
    won = int(sp.get("player_finish_order") or 1) == 1
    if sp.get("stage") != "out":
        ev = producers.result_event(full_state, chara_info, turn, won, int(sp.get("running_style") or 3))
        if ev:
            CE.emit(full_state, ev)
        sp["stage"] = "out"
    chara_info["playing_state"] = 5
    response = _envelope(career, chara_info, ("chara_info", "unchecked_event_array"))
    wire, _ev = CE.serve_next(full_state, "venus_race_out")
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
    if int(chara_info.get("playing_state") or 1) != 15 or int(sp.get("player_finish_order") or 1) == 1:
        return _refused("venus_race_continue", "nothing to retry")
    ctype = int(payload.get("continue_type") or 1)
    counts = smt._continue_counts(full_state, sp)
    if ctype == 2:
        if counts["available_continue_num"] <= 0 or not smt._spend_alarm_clock(full_state):
            return _refused("venus_race_continue", "no alarm clock")
        sp["continue_paid_used"] = int(sp.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return _refused("venus_race_continue", "no free retry")
        sp["continue_free_used"] = int(sp.get("continue_free_used") or 0) + 1
    cd = career["data"]
    if sp.get("end_committed"):
        turn = int(sp.get("turn") or 0)
        chara_info["fans"] = max(0, int(chara_info.get("fans") or 0) - int(sp.get("gained_fans") or 0))
        hist = cd.get("race_history") or []
        if hist and hist[-1].get("program_id") == sp.get("program_id") and hist[-1].get("turn") == turn:
            hist.pop()
        st["races"] = [r for r in st["races"] if int(r["turn"]) != turn]
        for cmd in impl.formula.TRAINING_COMMANDS:
            st["points"][str(cmd)] = int(st["points"].get(str(cmd), 0)) - impl.POINTS_RACE_LOSS
        sp["end_committed"] = False
    sp["continue_used"] = int(sp.get("continue_used") or 0) + 1
    sp.pop("race_start_info", None)
    _simulate(full_state, viewer_id, chara_info, sp)
    sp["stage"] = "entered"
    chara_info["playing_state"] = 13
    if isinstance(cd.get("home_info"), dict):
        cd["home_info"].update(smt._continue_counts(full_state, sp))
    response = _envelope(career, chara_info, ("chara_info", "home_info"))
    response["data"]["user_item"] = {"item_id": smt._ALARM_CLOCK_ITEM,
                                     "number": smt._alarm_clock_stock(full_state)}
    return _finish(viewer_id, full_state, career, chara_info, response)


HANDLERS = {
    "spirit_use": handle_spirit_use,
    "venus_spirit_history": handle_spirit_history,
    "venus_race_entry": handle_race_entry,
    "venus_race_start": handle_race_start,
    "venus_race_end": handle_race_end,
    "venus_race_out": handle_race_out,
    "venus_race_continue": handle_race_continue,
}
