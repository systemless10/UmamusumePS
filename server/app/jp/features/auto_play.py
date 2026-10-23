
from __future__ import annotations

import functools
import json
import logging

from ... import region
from ... import state as state_store

log = logging.getLogger("uma-server")

STATE_KEY = "jp_auto_play_plan"
CUSTOM_KEY = "jp_auto_play_custom"
DEFAULT_KEY = "jp_auto_play_default"
_COMMONS = ("single_mode_load_common", "single_mode_start_common")
_OK = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


@functools.lru_cache(maxsize=1)
def _plans() -> dict:
    try:
        return json.loads((region.seeds_dir() / "auto_play_plans.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.error("JP: no auto_play_plans.json seed -- careers keep the seed's plans")
        return {}


def _scenario_of(plan_id: int) -> int:
    return plan_id // 10000


def _ack() -> dict:
    return json.loads(json.dumps(_OK))


def _edit(payload: dict, key: str, plan_id: int, patch: dict, base: dict | None = None) -> None:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    saved = full_state.setdefault(key, {})
    plan = saved.get(str(plan_id)) or (json.loads(json.dumps(base)) if base else {"plan_id": plan_id})
    plan.update(patch)
    saved[str(plan_id)] = plan
    state_store.save_state(viewer_id, full_state)


def handle_select_plan(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    plan_id = int(payload.get("plan_id") or 0)
    full_state = state_store.get_state(viewer_id) or {}
    if plan_id:
        full_state.setdefault(STATE_KEY, {})[str(_scenario_of(plan_id))] = plan_id
        state_store.save_state(viewer_id, full_state)
    return _ack()


def handle_save_custom_plan(payload: dict) -> dict:
    plan = payload.get("custom_plan")
    if isinstance(plan, dict) and int(plan.get("plan_id") or 0):
        _edit(payload, CUSTOM_KEY, int(plan["plan_id"]), plan)
    return _ack()


def handle_save_default_plan(payload: dict) -> dict:
    plan = payload.get("default_plan")
    if isinstance(plan, dict) and int(plan.get("plan_id") or 0):
        _edit(payload, DEFAULT_KEY, int(plan["plan_id"]), plan)
    return _ack()


def handle_save_plan_name(payload: dict) -> dict:
    plan_id = int(payload.get("plan_id") or 0)
    if plan_id:
        _edit(payload, CUSTOM_KEY, plan_id, {"plan_name": payload.get("plan_name") or ""})
    return _ack()


def handle_save_plan_description(payload: dict) -> dict:
    plan_id = int(payload.get("plan_id") or 0)
    if plan_id:
        _edit(payload, CUSTOM_KEY, plan_id,
              {"plan_description": payload.get("plan_description") or ""})
    return _ack()


HANDLERS = {"single_mode/auto_play_select_plan": handle_select_plan,
            "single_mode/auto_play_save_custom_plan": handle_save_custom_plan,
            "single_mode/auto_play_save_default_plan": handle_save_default_plan,
            "single_mode/auto_play_save_plan_name": handle_save_plan_name,
            "single_mode/auto_play_save_plan_description": handle_save_plan_description}


def _overlay(plans: list, saved: dict, scenario_id: int) -> list:
    out, seen = [], set()
    for plan in plans:
        pid = plan.get("plan_id")
        seen.add(str(pid))
        mine = saved.get(str(pid))
        out.append(json.loads(json.dumps(mine)) if mine and "plan_setting_array" in mine
                   else {**plan, **(mine or {})})
    for pid, mine in sorted(saved.items()):
        if pid not in seen and _scenario_of(int(pid)) == scenario_id and "plan_setting_array" in mine:
            out.append(json.loads(json.dumps(mine)))
    return out


def after(endpoint: str, payload: dict, resp: dict) -> None:
    data = resp.get("data")
    if not isinstance(data, dict) or not endpoint.startswith("single_mode"):
        return
    for key in _COMMONS:
        common = data.get(key)
        if isinstance(common, dict) and "auto_play_custom_plan_array" in common:
            break
    else:
        return
    scenario_id = int((common.get("chara_info") or {}).get("scenario_id") or 0)
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    saved_custom = {k: v for k, v in (full_state.get(CUSTOM_KEY) or {}).items()
                    if _scenario_of(int(k)) == scenario_id}
    saved_default = {k: v for k, v in (full_state.get(DEFAULT_KEY) or {}).items()
                     if _scenario_of(int(k)) == scenario_id}
    plans = _plans().get(str(scenario_id))
    if plans:
        default = json.loads(json.dumps(plans["default"]))
        custom = json.loads(json.dumps(plans["custom"]))
        selected = plans["selected"]
    elif saved_custom or saved_default:
        default = [p for p in common.get("auto_play_default_plan_array") or []
                   if _scenario_of(int(p.get("plan_id") or 0)) == scenario_id]
        custom = [p for p in common.get("auto_play_custom_plan_array") or []
                  if _scenario_of(int(p.get("plan_id") or 0)) == scenario_id]
        selected = 0
    else:
        return
    default = _overlay(default, saved_default, scenario_id)
    custom = _overlay(custom, saved_custom, scenario_id)
    chosen = int((full_state.get(STATE_KEY) or {}).get(str(scenario_id)) or 0)
    common["auto_play_default_plan_array"] = default
    common["auto_play_custom_plan_array"] = custom
    seed_pick = int(common.get("auto_play_selected_plan_id") or 0)
    if _scenario_of(seed_pick) != scenario_id:
        seed_pick = 0
    common["auto_play_selected_plan_id"] = chosen or selected or seed_pick
