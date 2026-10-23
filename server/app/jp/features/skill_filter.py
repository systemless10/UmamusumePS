
from __future__ import annotations

import copy
import logging

from ... import scenarios
from ... import state as state_store
from ...handlers import single_mode_team
from ...result_codes import refuse

log = logging.getLogger("uma-server")

STATE_KEY = "jp_skill_filter_setting"
_COMMONS = ("single_mode_load_common", "single_mode_start_common")
DEFAULT = [{"set_type": 101, "set_value": 0}, {"set_type": 102, "set_value": 0},
           {"set_type": 103, "set_value": 0}, {"set_type": 104, "set_value": 1},
           {"set_type": 105, "set_value": 1}, {"set_type": 201, "set_value": 0},
           {"set_type": 301, "set_value": 0}, {"set_type": 401, "set_value": 0}]
_OK = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


def handle_save(payload: dict) -> dict:
    rows = payload.get("setting_array")
    if not isinstance(rows, list):
        return refuse()
    saved = [{"set_type": int(r.get("set_type") or 0), "set_value": int(r.get("set_value") or 0)}
             for r in rows if isinstance(r, dict)]
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    merged = {r["set_type"]: r for r in full_state.get(STATE_KEY) or DEFAULT}
    merged.update({r["set_type"]: r for r in saved})
    full_state[STATE_KEY] = sorted(merged.values(), key=lambda r: r["set_type"])
    state_store.save_state(viewer_id, full_state)
    return {**copy.deepcopy(_OK), "data": {}}


def handle_factor_reload(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    stashed = full_state.get("career_factor_roll")
    if not isinstance(stashed, dict) or not stashed.get("rolls"):
        log.warning("factor_reload refused: no spark roll for this career")
        return refuse()
    return single_mode_team.jp_factor_lottery_reply(full_state, stashed)


def after(endpoint: str, payload: dict, resp: dict) -> None:
    data = resp.get("data")
    if not isinstance(data, dict) or not endpoint.startswith("single_mode"):
        return
    for key in _COMMONS:
        common = data.get(key)
        if isinstance(common, dict) and "chara_info" in common:
            full_state = state_store.get_state(payload.get("viewer_id")) or {}
            common["skill_filter_setting_array"] = copy.deepcopy(
                full_state.get(STATE_KEY) or DEFAULT)


HANDLERS = {
    **{f"{s.endpoint_prefix}/save_skill_filter_set": handle_save
       for s in scenarios.all_scenarios()},
    **{f"{s.endpoint_prefix}/factor_reload": handle_factor_reload
       for s in scenarios.all_scenarios()},
}
