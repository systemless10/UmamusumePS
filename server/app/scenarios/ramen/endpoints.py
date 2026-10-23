
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
    log.warning("ramen %s refused: %s", where, why)
    return refuse()


def handle_region_select_check(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    _viewer, full_state, _career_state, chara_info = got
    kind = impl.selection_type(full_state, chara_info)
    regions = payload.get("region_id_array") or []
    if not impl.valid_regions(regions, kind):
        return _refused("region_select_check", f"{regions} for selection {kind}")
    base = impl.split_base(regions, impl.gauge_total(chara_info))
    return {"response_code": 1, "data_headers": {"result_code": 1},
            "data": {"reduce_base_turn_info_array": impl.reduce_base_array(base)}}


def handle_select_region(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    kind = impl.selection_type(full_state, chara_info)
    regions = payload.get("region_id_array") or []
    if not impl.valid_regions(regions, kind):
        return _refused("select_region", f"{regions} for selection {kind}")
    impl.select_regions(full_state, chara_info, regions)
    chara_info["playing_state"] = 1
    st = impl.state(full_state)
    st["hold_turn"] = 0
    if kind > 1:
        ev = _producers().region_follow_event(kind)
        if ev:
            CE.emit(full_state, ev)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "race_condition_array",
                                  "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["reduce_base_turn_info_array"] = impl.reduce_base_array(
        {int(f): v for f, v in st["base"].items()})
    data["expected_check_point_pt"] = impl.expected_pt(st, impl.turn_of(chara_info))
    smt = _smt()
    if "race_condition_array" in data:
        data["race_condition_array"] = smt._build_race_condition_array(
            chara_info, chara_info.get("turn"), career["data"].get("race_history", []))
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    log.info("ramen: regions %s selected (selection %s)", regions, kind)
    return response


def _producers():
    from . import producers
    return producers


def handle_tasting(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 1:
        return _refused("tasting", f"playing_state {chara_info.get('playing_state')}")
    turn = impl.turn_of(chara_info)
    try:
        info = impl.taste(full_state, chara_info, payload.get("used_feeling_array"), turn)
    except impl.Refused as exc:
        return _refused("tasting", exc)
    impl.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "not_up_parameter_info"))
    data = response["data"]
    st = impl.state(full_state)
    capped = impl.flag(st, "not_up_bond")
    if capped:
        _smt()._note_not_up(data, {"evaluation_chara_id_array": capped})
    data["last_tasting_info"] = info
    data["check_point_pt"] = int(st["pt"])
    data["expected_check_point_pt"] = impl.expected_pt(st, turn)
    _smt().state_store.save_state(viewer_id, full_state)
    return response


def handle_check_point(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 46:
        return _refused("check_point", f"playing_state {chara_info.get('playing_state')}")
    st = impl.state(full_state)
    turn = int(st.get("hold_turn") or payload.get("current_turn") or 0)
    cp = M.check_point_for_turn(turn)
    if not cp:
        return _refused("check_point", f"no Jamboree on turn {turn}")
    _result, ev = impl.commit_check_point(full_state, chara_info,
                                          cp, payload.get("twinkle_text_id_array"))
    CE.emit(full_state, ev)
    wire, _served = CE.serve_next(full_state, "check_point")
    chara_info["playing_state"] = 5
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = [wire] if wire else []
    data["check_point_info_array"] = impl.check_point_info(st, cp)
    data["check_point_pt"] = 0
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_ramen_live(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    if int(chara_info.get("playing_state") or 1) != 48:
        return _refused("ramen_live", f"playing_state {chara_info.get('playing_state')}")
    chara_info["playing_state"] = 1
    impl.state(full_state)["hold_turn"] = 0
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "unchecked_event_array"))
    data = response["data"]
    data["unchecked_event_array"] = _next_events(full_state, chara_info)
    data["add_music"] = None
    smt = _smt()
    smt._remember_display(full_state, response)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_uraf_effect_apply(payload: dict) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, career, chara_info = got
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    if turn <= impl.LAST_GAUGE_TURN or not st["uraf"].get("type"):
        return _refused("uraf_effect_apply", f"turn {turn}, uraf {st['uraf']}")
    if impl.uraf_active(st, turn):
        return _refused("uraf_effect_apply", f"already applied on turn {turn}")
    not_up = impl.apply_uraf(full_state, chara_info, turn)
    impl.refresh_home(full_state, chara_info)
    career["data"]["chara_info"] = chara_info
    response = _envelope(career, ("chara_info", "home_info", "not_up_parameter_info"))
    _smt()._note_not_up(response["data"], not_up)
    _smt().state_store.save_state(viewer_id, full_state)
    return response


def handle_uraf_effect_select_event_check(payload: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1}, "data": {}}


def _save_auto_select(payload: dict, key: str) -> dict:
    got = _career(payload)
    if got is None:
        return refuse()
    viewer_id, full_state, _career_state, _ci = got
    fields = {k: v for k, v in payload.items() if k not in ("viewer_id",)
              and not k.endswith("_request_common")}
    impl.state(full_state)["auto_select"][key] = fields
    _smt().state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1}, "data": {}}


def handle_default_auto_select_save(payload: dict) -> dict:
    return _save_auto_select(payload, "default")


def handle_auto_select_set_save(payload: dict) -> dict:
    return _save_auto_select(payload, f"set:{payload.get('auto_select_set_id', 0)}")


def handle_auto_select_set_name_save(payload: dict) -> dict:
    return _save_auto_select(payload, f"name:{payload.get('auto_select_set_id', 0)}")


HANDLERS = {
    "region_select_check": handle_region_select_check,
    "select_region": handle_select_region,
    "tasting": handle_tasting,
    "check_point": handle_check_point,
    "ramen_live": handle_ramen_live,
    "uraf_effect_apply": handle_uraf_effect_apply,
    "uraf_effect_select_event_check": handle_uraf_effect_select_event_check,
    "default_auto_select_save": handle_default_auto_select_save,
    "auto_select_set_save": handle_auto_select_set_save,
    "auto_select_set_name_save": handle_auto_select_set_name_save,
}
