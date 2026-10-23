
from __future__ import annotations

import copy
import logging

from ... import career_events
from ... import event_engine
from . import impl as grand_live
from . import producers
from ...result_codes import refuse

log = logging.getLogger("uma-server")


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


_LIVE_ONLY_DROP = ("race_condition_array", "race_start_info", "race_running_style",
                   "event_effected_factor_array", "not_down_parameter_info")


def _grand_live_envelope(full_state: dict, career: dict, drop=()) -> dict:
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    chara_info = career["data"]["chara_info"]
    if chara_info.get("playing_state") not in (grand_live.PLAYING_STATE_BACKSTAGE,
                                               grand_live.PLAYING_STATE_EVENT_CHAIN):
        chara_info["playing_state"] = 1
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    for key in drop:
        data.pop(key, None)
    return response


def handle_master_square(payload: dict) -> dict:
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return refuse()

    chara_info = career["data"]["chara_info"]
    st = grand_live.state(full_state)
    square_id = payload.get("square_id")

    if square_id:
        try:
            result = grand_live.apply_square(st, chara_info, square_id)
            log.info("grand live lesson %s -> %s", square_id, result)
        except ValueError as exc:
            log.warning("grand live lesson %s refused: %s", square_id, exc)
            return refuse()

    career["data"]["chara_info"] = chara_info
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        smt._refresh_command_info(
            chara_info, career_home, turn=chara_info.get("turn", 1),
            unlocked_npcs=[n[0] for n in
                           full_state.get(smt.single_mode_events.UNLOCKED_NPCS_KEY, [])],
            facility_levels=smt._facility_levels(career["data"]),
            race_history=career["data"].get("race_history", []),
            training_bonus=smt._training_bonus(full_state, chara_info),
            friendship_bonus=smt._friendship_bonus(full_state, chara_info),
            specialty_bonus=smt._specialty_bonus(full_state, chara_info),
            support_card_levels=smt._support_card_levels(full_state),
            friendship_stacks=smt._friendship_stacks(full_state),
            full_state=full_state)
    response = _grand_live_envelope(full_state, career, drop=_LIVE_ONLY_DROP)
    smt.state_store.save_state(viewer_id, full_state)
    return response


_LIVE_END_EVENTS = {
    1:  (202006, 400003441),
    2:  (202012, 400003443),
    3:  (202020, 400003446),
    4:  (202027, 400003447),
    10: (202041, 400003417),
}
_GRAND_FINALE_SPECIAL = (202046, 400003418)

_LIVE_START_KEYS = ("chara_info", "unchecked_event_array", "live_data_set", "add_music")


def handle_live_start(payload: dict) -> dict:
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return refuse()

    chara_info = career["data"]["chara_info"]
    turn = payload.get("current_turn") or chara_info.get("turn")
    live_type = grand_live.pending_live_for(full_state, turn)

    result = {}
    song = None
    if live_type is not None:
        live_turn = grand_live.live_turn_of(live_type) or turn
        result = grand_live.perform_live(full_state, chara_info, live_turn)
        log.info("grand live: LIVE %s on turn %s -> %s", live_type, turn, result)
        if live_type == grand_live.GRAND_LIVE_TYPE:
            grand_live.grant_free_song(full_state, grand_live.GIRLS_LEGEND_U_LIVE_ID,
                                       chara_info=chara_info)
            from ... import concerts
            song = concerts.on_scenario(
                full_state,
                "special_grand_concert" if result.get("great") else "grand_concert")
        career["data"]["chara_info"] = chara_info
    if chara_info.get("playing_state") == grand_live.PLAYING_STATE_BACKSTAGE:
        chara_info["playing_state"] = grand_live.PLAYING_STATE_EVENT_CHAIN
        career["data"]["chara_info"] = chara_info

    response = _grand_live_envelope(full_state, career)
    data = response["data"]
    for key in [k for k in data if k not in _LIVE_START_KEYS]:
        del data[key]
    data["add_music"] = song

    if live_type is not None:
        live_turn = grand_live.live_turn_of(live_type)
        if live_turn:
            data["chara_info"]["turn"] = live_turn

    end = _LIVE_END_EVENTS.get(live_type)
    if live_type == grand_live.GRAND_LIVE_TYPE and result.get("great"):
        end = _GRAND_FINALE_SPECIAL
    if end:
        event_id, story_id = end
        ev = {"choices": [{"effects": []}]}
        entry = event_engine.career_event_entry(
            ev, event_id, story_id,
            chara_id=0,
            support_card_id=0, play_timing=producers.POST_LIVE_TIMING)
        data["unchecked_event_array"] = [entry]
        smt._push_career_ctx(full_state, {
            "event_id": event_id, "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": chara_info.get("card_id"), "event": ev})

        beats_turn = grand_live.live_turn_of(live_type) or turn
        for beat_id, beat_story, n_choices, timing in \
                producers.post_live_beats(beats_turn):
            career_events.emit(full_state, career_events.Event(
                event_id=beat_id, story_id=beat_story, play_timing=timing,
                choices=[career_events.Choice(effects=[])
                         for _ in range(n_choices)],
                chara_id=0,
                once_key=f"gl:{beat_id}", source="grand_live_post_live",
                priority=career_events.PRIO_SCENARIO))

    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_reserve_square(payload: dict) -> dict:
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return refuse()

    st = grand_live.state(full_state)
    square_id = int(payload.get("square_id") or 0)
    board = [int(s) for s in (st.get("board") or [])]
    st["reserve_square_id"] = square_id if square_id in board else 0
    log.info("grand live reserve square %s -> %s (board %s)",
             square_id, st["reserve_square_id"], board)
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}


def handle_lottery_square(payload: dict) -> dict:
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return refuse()

    chara_info = career["data"]["chara_info"]
    st = grand_live.state(full_state)
    reserved = int(st.get("reserve_square_id") or 0)
    st["board"] = grand_live.reroll_board(st, chara_info, keep=reserved)
    log.info("grand live lottery square %s -> board %s (kept %s)",
             payload.get("square_id"), st["board"], reserved)
    response = _grand_live_envelope(full_state, career, drop=_LIVE_ONLY_DROP)
    smt.state_store.save_state(viewer_id, full_state)
    return response
