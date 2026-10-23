
from __future__ import annotations

import base64
import logging
import os
import secrets

from .. import accounts
from .. import state as state_store
from . import load as load_mod
from . import registry

log = logging.getLogger("uma-server")

_country_list_cache: list | None = None


def _country_list() -> list:
    global _country_list_cache
    if _country_list_cache is None:
        import json
        path = load_mod.SEED_PATH.parent / "country_list.json"
        with open(path, "r", encoding="utf-8") as f:
            _country_list_cache = json.load(f)
    return _country_list_cache


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@registry.endpoint("tool/pre_signup")
def handle_pre_signup(payload: dict) -> dict:
    return _ok({
        "attest": False,
        "nonce": "",
        "enable_steam_account": False,
        "country_list": _country_list(),
    })


def handle_signup(payload: dict) -> dict:
    viewer_id = secrets.randbelow(900_000_000_000) + 100_000_000_000
    auth_key = base64.b64encode(os.urandom(24)).decode()
    accounts.record_signup(viewer_id, auth_key)

    full_state = state_store.get_state(viewer_id) or {}
    load_mod.get_or_seed_data(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)

    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {
            "viewer_id": viewer_id,
            "auth_key": auth_key,
        },
    }


def _skip_tutorial(full_state: dict, viewer_id) -> None:
    data = load_mod.get_or_seed_data(full_state, viewer_id)
    user_info = data.setdefault("user_info", {})
    user_info["tutorial_step"] = 1000
    if user_info.get("tutorial_step") != 1000:
        raise RuntimeError(
            f"tutorial_step write did not stick for viewer {viewer_id!r} "
            f"(still {user_info.get('tutorial_step')!r}) -- refusing to silently "
            f"leave this account showing the real tutorial")


@registry.endpoint("tutorial/skip")
def handle_tutorial_skip(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    _skip_tutorial(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({"step": 1000})
