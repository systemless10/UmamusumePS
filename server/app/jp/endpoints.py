
from __future__ import annotations

import base64
import contextvars
import logging
import os
import secrets

from .. import accounts, config
from .. import state as state_store
from ..handlers import load as load_mod
from ..handlers import account_link

log = logging.getLogger("uma-server")

CURRENT_REQUEST: contextvars.ContextVar = contextvars.ContextVar("JP_CURRENT_REQUEST", default=None)

SERVER_LIST = {
    "resource_server": "prd-storage-app-umamusume.akamaized.net/dl/",
    "resource_server_cf": "prd-storage-app-umamusume.akamaized.net/dl/",
    "resource_server_login": "prd-storage-app-umamusume.akamaized.net/dl/",
    "resource_server_ingame": "prd-storage-game-umamusume.akamaized.net/dl/",
}

SERVER_LIST_ENDPOINTS = frozenset({
    "tool/get_pre_download_resource_version", "tool/start_session",
    "tool/pre_signup", "tool/signup",
    "account/index", "account/get_by_transition_code",
    "account/chain_by_transition_code", "account/steam_chain_disconnect",
})


def _ok(data: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def resource_version() -> str:
    return str(config.get("resource_version", ""))


def handle_pre_signup(payload: dict) -> dict:
    return _ok({"attest": False, "nonce": ""})


def _mint_viewer_id() -> int:
    while True:
        vid = secrets.randbelow(900_000_000) + 100_000_000
        if accounts.get_account(vid) is None:
            return vid


def handle_signup(payload: dict) -> dict:
    decoded = CURRENT_REQUEST.get()
    tail50 = os.urandom(50)
    auth_key = base64.b64encode(tail50).decode()
    viewer_id = _mint_viewer_id()
    accounts.record_signup(viewer_id, auth_key, decoded.udid.hex() if decoded else None)
    full_state = state_store.get_state(viewer_id) or {}
    load_mod.get_or_seed_data(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    log.info("JP signup: minted viewer %s", viewer_id)
    return _ok({"viewer_id": viewer_id, "auth_key": auth_key})


def handle_start_session(payload: dict) -> dict:
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    data = load_mod.get_or_seed_data(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    step = int(((data.get("user_info") or {}).get("tutorial_step")) or 0)
    return _ok({"attest": False, "nonce": "", "terms_updated": False,
                "is_tutorial": step < 1000, "resource_version": resource_version()})


def remember_session(decoded) -> None:
    viewer_id = decoded.payload.get("viewer_id") if isinstance(decoded.payload, dict) else None
    if viewer_id and decoded.tail50 is not None:
        accounts.record_signup(viewer_id, base64.b64encode(decoded.tail50).decode(),
                               decoded.udid.hex())


def handle_chain_by_transition_code(payload: dict) -> dict:
    decoded = CURRENT_REQUEST.get()
    target_id = payload.get("input_viewer_id")
    target = accounts.get_account(target_id) if target_id else None
    if (decoded is not None and target and not target.get("auth_key")
            and accounts.verify_transition_password(target_id, payload.get("password"))):
        accounts.record_signup(target_id, base64.b64encode(os.urandom(50)).decode(),
                               decoded.udid.hex())
    resp = account_link.handle_chain_by_transition_code(payload)
    if decoded is not None and resp["data_headers"]["result_code"] == 1:
        data = resp["data"]
        accounts.record_signup(data["chained_viewer_id"], data["auth_key"], decoded.udid.hex())
    return resp


def handle_announce(payload: dict) -> dict:
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    seen = full_state.setdefault(load_mod.SEEN_ANNOUNCE_KEY, [])
    for item in payload.get("add_announce_data_array") or []:
        i = item.get("id") if isinstance(item, dict) else item
        if i is not None and i not in seen:
            seen.append(i)
    state_store.save_state(viewer_id, full_state)
    return _ok({"announce_data_array": [{"id": i} for i in seen]})


HANDLERS = {
    "tool/pre_signup": handle_pre_signup,
    "tool/signup": handle_signup,
    "tool/start_session": handle_start_session,
    "account/chain_by_transition_code": handle_chain_by_transition_code,
    "read_info/announce": handle_announce,
}
