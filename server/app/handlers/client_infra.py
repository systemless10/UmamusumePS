
from __future__ import annotations

import logging
import secrets

from .. import config, state as state_store
from . import registry

log = logging.getLogger("uma-server")

OPTION_KEY = "client_option_state"
FORCE_DISPLAY_KEY = "force_display_acked"
TUTORIAL_STEP_KEY = "tutorial_step"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@registry.endpoint("tool/device_attest")
def handle_device_attest(payload: dict) -> dict:
    kind = payload.get("attestation_type")
    err = payload.get("error_code") or 0
    if err:
        log.info("tool/device_attest: client reported attestation error %s (%s), "
                 "accepting anyway", err, payload.get("error_message"))
    else:
        log.info("tool/device_attest: accepted (attestation_type=%s%s)",
                 kind, ", iOS" if kind == 1 else "")
    return _ok({})


@registry.endpoint("tool/get_verify_token")
def handle_get_verify_token(payload: dict) -> dict:
    return _ok({"token": secrets.token_hex(16)})


@registry.endpoint("tool/get_pre_download_resource_version")
def handle_get_pre_download_resource_version(payload: dict) -> dict:
    from . import load as load_handler
    from .. import dispatch as _main
    version = str(config.get("resource_version", _main.GAME_RESOURCE_VERSION))
    return _ok({"resource_version": version})


@registry.endpoint("tool/send_log")
def handle_send_log(payload: dict) -> dict:
    log.info("client log [key=%s]: %s", payload.get("log_key"),
             str(payload.get("log_message"))[:2000])
    return _ok({})


@registry.endpoint("option/change_option")
def handle_change_option(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    stored = full_state.setdefault(OPTION_KEY, {})
    for entry in payload.get("option_info_array") or []:
        if entry.get("type") is None:
            continue
        stored[str(int(entry["type"]))] = int(entry.get("value") or 0)
    state_store.save_state(viewer_id, full_state)
    return _ok({"option_info_array": [{"type": int(t), "value": v}
                                      for t, v in sorted(stored.items(),
                                                         key=lambda kv: int(kv[0]))]})


@registry.endpoint("update_force_display_status")
def handle_update_force_display_status(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event_id = payload.get("event_id")
    if event_id:
        full_state = state_store.get_state(viewer_id) or {}
        seen = full_state.setdefault(FORCE_DISPLAY_KEY, [])
        if event_id not in seen:
            seen.append(event_id)
            state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("banner_url")
def handle_banner_url(payload: dict) -> dict:
    log.info("banner_url: no destination for banner %s", payload.get("banner_id"))
    return _ok({"url": ""})


@registry.endpoint("tutorial")
def handle_tutorial(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[TUTORIAL_STEP_KEY] = int(payload.get("step") or 0)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("tutorial/team_edit")
def handle_tutorial_team_edit(payload: dict) -> dict:
    from . import team_stadium
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ts = full_state.setdefault(team_stadium.TEAM_STADIUM_STATE_KEY, {})
    team = payload.get("team_data_array") or []
    if team:
        ts["team_data_array"] = team
    points = int(payload.get("team_evaluation_point") or 0)
    if points:
        ts["team_evaluation_point"] = points
    rank = int(ts.get("team_rank") or 0)
    state_store.save_state(viewer_id, full_state)
    log.info("tutorial/team_edit: stored %s member(s), evaluation %s",
             len(team), points)
    return _ok({"reward_info_array": [], "before_rank": rank, "after_rank": rank})


@registry.endpoint("tutorial/single_mode_finish")
def handle_tutorial_single_mode_finish(payload: dict) -> dict:
    from . import collection, directory, trained_chara
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    newest = max(roster, key=lambda c: c.get("trained_chara_id") or 0) if roster else None
    log.info("tutorial/single_mode_finish: no tutorial career on this server "
             "(tutorial/skip is the live path) -- serving real state, %s roster entr%s",
             len(roster), "y" if len(roster) == 1 else "ies")
    return _ok({
        "trained_chara": list(roster),
        "directory_card_array": directory.archive_entries(roster),
        "directory_ranking": 0,
        "trained_chara_id": (newest or {}).get("trained_chara_id") or 0,
        "love_point_info": {},
        "reward_item_info": {},
        "support_card_data_array": list(full_state.get(collection.SUPPORT_CARD_KEY) or []),
        "limited_shop_info": {},
        "update_user_chara_info": {},
        "new_chara_profile_array": [],
    })
