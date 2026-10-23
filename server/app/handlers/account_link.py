
from __future__ import annotations

from .. import accounts
from . import load as load_mod
from . import missions, registry
from ..result_codes import ResultCode, refuse


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _display_name(viewer_id) -> str:
    from .. import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    data = load_mod.get_or_seed_data(full_state, viewer_id)
    return (data.get("user_info") or {}).get("name") or ""


@registry.endpoint("account/publish_transition_code")
def handle_publish_transition_code(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    password = payload.get("password")
    if not isinstance(password, str) or not password:
        return _refuse()
    accounts.set_transition_password(viewer_id, password)
    from .. import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    missions.mark_achieved(full_state, missions.FLAG_APP_DATA_LINKED)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("account/get_by_transition_code")
def handle_get_by_transition_code(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target_id = payload.get("input_viewer_id")
    password = payload.get("password")
    if not target_id or not accounts.verify_transition_password(target_id, password):
        return _refuse()
    return _ok({
        "target_viewer_id": int(target_id),
        "name": _display_name(target_id),
        "request_user_name": _display_name(viewer_id),
    })


@registry.endpoint("account/chain_by_transition_code")
def handle_chain_by_transition_code(payload: dict) -> dict:
    target_id = payload.get("input_viewer_id")
    password = payload.get("password")
    if not target_id or not accounts.verify_transition_password(target_id, password):
        return _refuse()
    target = accounts.get_account(target_id)
    if not target or not target.get("auth_key"):
        return _refuse()
    accounts.claim_reset_password(target_id, password)
    return _ok({
        "chained_viewer_id": int(target_id),
        "chained_user_name": _display_name(target_id),
        "auth_key": target["auth_key"],
    })


@registry.endpoint("account/chain_disconnect")
def handle_chain_disconnect(payload: dict) -> dict:
    return _ok({})


@registry.endpoint("account/steam_chain_disconnect")
def handle_steam_chain_disconnect(payload: dict) -> dict:
    return _ok({})


@registry.endpoint("account/deletion_request")
def handle_deletion_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    accounts.request_deletion(viewer_id)
    return _ok({})


@registry.endpoint("account/deletion_cancel")
def handle_deletion_cancel(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    accounts.cancel_deletion(viewer_id)
    return _ok({})
