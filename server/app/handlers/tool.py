"""
Account creation: tool/pre_signup -> tool/signup -> tutorial/skip. Ground
truth for all three is a REAL account creation captured 2026-08-18 against
the actual Cygames server (captures/20260818_081331/, ops 0002/0003/0008) --
the first genuine capture of this flow this project has ever had (the
original tool/signup implementation below predates it and was built from
scratch, since every earlier capture was from an existing account).

tool/pre_signup -- request is just the usual device/steam boilerplate.
Response: {attest: false, nonce: "", enable_steam_account: false,
country_list: [...167 {country, country_type}...]}. THIS is the fix for the
long-standing "country/region selection screen stuck on an empty list" bug
(see README/handoff) -- we had no handler at all before, so the screen that
needs country_list to populate its dropdown got an empty one and could never
proceed. The list is static (same account, but there's no reason to expect
it varies per-request) so it's replayed verbatim from
data/seeds/country_list.json.

tool/signup: the real client (uma_api/client.py's signup()) requires
data.viewer_id and data.auth_key (base64) back before it proceeds past
account setup. Request also carries {country, optin_user_birth, dma_state,
...} which the real server presumably validates/stores server-side, but
none of it comes back in the response and we have no evidence it gates
anything else captured so far, so it's accepted but not further acted on.

Skip-tutorial-by-default (user-specified 2026-08-18: "by default lets make
sure the tutorial is skipped for now, throw an error if not, I want it
skipped by default"): the REAL flow only reaches tutorial_step=1000 and a
fully-stocked account (5 starter cards, 10 support cards, starter currency,
6 starter trained_chara) after the CLIENT explicitly calls tutorial/skip --
watching the actual tutorial is the default there. We invert that default:
data/seeds/load_index_fresh.json IS that post-skip snapshot (see load.py),
so every fresh account starts already past it with no client action needed.
tutorial/skip below is still implemented (idempotent, real shape) for
whenever the client calls it anyway, and handle_signup asserts the seed
it just handed out is genuinely post-skip -- loud failure (not a silent
fallback) if a future seed swap ever regresses this.
"""

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
    # Persist what we mint. Signup used to hand out credentials and forget
    # them -- there was no account record at all, so nothing could ever be
    # validated and every new client was a stranger on its second request.
    accounts.record_signup(viewer_id, auth_key)

    # Seed the fresh load/index blob NOW (rather than waiting for the
    # client's first load/index call), so name/change_sex/tutorial_skip --
    # all of which read-modify-write user_info -- have something to work
    # with from their very first call.
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
    """Set tutorial_step=1000 (skipped) and verify it stuck -- shared by
    handle_change_sex (see below: skip-by-default hooks HERE, not signup)
    and handle_tutorial_skip. User-specified 2026-08-18: "throw an error if
    not [skipped]" -- loud failure, not a silent fallback, since a failed
    write here means the account is stuck showing the real tutorial with no
    obvious cause."""
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
    """Request {}; response {step: 1000} (capture: op 0008)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    _skip_tutorial(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({"step": 1000})
