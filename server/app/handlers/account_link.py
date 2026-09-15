"""Account linking ("data link" in the real client's UI): publish a
password on an account, then use it FROM ANY OTHER ACCOUNT to preview and
finally switch into it -- the real game's mechanism for "delete your local
data, get a fresh trainer id, then log back into your old one whenever you
want." Ground truth is entirely real captures: account/publish_transition_
code from captures/20260818_074808/0003 (an established account publishing
a password), and account/get_by_transition_code + .../chain_by_transition_
code from captures/20260818_081331/0013-0014 (a FRESH signup account
linking back into that established one, both unredacted -- this project's
own capture_proxy.py, unlike the older UmaDumpy tool, doesn't sanitize).

The "code" is not a separately generated value -- input_viewer_id in both
lookup calls is just the target account's own viewer_id (the "trainer id"
every player already sees on their profile), paired with whatever password
that account published. publish_transition_code's own response carries no
data at all beyond the envelope; get_by is a non-committing PREVIEW (shows
both names so the client can render a confirmation dialog); chain_by is the
COMMIT and is what actually hands back the target account's auth_key --
the client re-authenticates as that account from here on, abandoning
whatever session it called this from.
"""

from __future__ import annotations

from .. import accounts
from . import load as load_mod
from . import missions, registry


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _display_name(viewer_id) -> str:
    """The account's trainer name, from its own load/index blob -- seeds it
    (via the same fresh-account seed everything else uses) if this is
    somehow the first thing ever asked of that viewer_id."""
    from .. import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    data = load_mod.get_or_seed_data(full_state, viewer_id)
    return (data.get("user_info") or {}).get("name") or ""


@registry.endpoint("account/publish_transition_code")
def handle_publish_transition_code(payload: dict) -> dict:
    """Request {password}; response carries no data (capture: envelope only).

    Also the real trigger for missions.py's "Link App Data" (600012) --
    publishing a transition code IS the real client's "Link App Data"
    action (it's what makes an account portable/recoverable), not the
    receiving side (account/chain_by_transition_code), which re-
    authenticates as a DIFFERENT (target) account and abandons the calling
    one -- marking the flag there would credit an account about to be
    discarded."""
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
    """Request {password, input_viewer_id}; response {target_viewer_id,
    name (target's), request_user_name (the CALLING account's own name)} --
    a preview, commits nothing. Capture: op 0013."""
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
    """Request {password, input_viewer_id}; response {chained_viewer_id,
    chained_user_name, auth_key (the TARGET account's real one)} -- the
    commit. Capture: op 0014. The client re-authenticates as the target
    account from here on; nothing about the CALLING account is touched or
    deleted (matches the real flow -- the abandoned throwaway account just
    stops being used, it isn't explicitly torn down)."""
    target_id = payload.get("input_viewer_id")
    password = payload.get("password")
    if not target_id or not accounts.verify_transition_password(target_id, password):
        return _refuse()
    target = accounts.get_account(target_id)
    if not target or not target.get("auth_key"):
        return _refuse()   # target predates record_signup -- nothing to hand back
    # If this login only succeeded because the account was in the admin
    # "accept any password" reset state, persist whatever password was just
    # used as the new real one -- closes the reset window on first use, same
    # as if the player had published it themselves. No-op for a normal,
    # already-real password (claim_reset_password checks the sentinel itself).
    accounts.claim_reset_password(target_id, password)
    return _ok({
        "chained_viewer_id": int(target_id),
        "chained_user_name": _display_name(target_id),
        "auth_key": target["auth_key"],
    })


@registry.endpoint("account/chain_disconnect")
def handle_chain_disconnect(payload: dict) -> dict:
    """Request {openid, openid_type}; response envelope only (dump.cs:
    AccountChainDisconnectResponse.CommonResponse has zero fields). Real
    purpose is unlinking a Google/Facebook/Apple/Cygames-ID account -- this
    server never implements that linking in the first place (no real external
    identity provider to link against), so there is never anything to
    disconnect. Always succeeds: an unlink of a link that was never really
    established is harmlessly a no-op both ways."""
    return _ok({})


@registry.endpoint("account/steam_chain_disconnect")
def handle_steam_chain_disconnect(payload: dict) -> dict:
    """Same shape/reasoning as chain_disconnect, Steam-specific (dump.cs:
    AccountSteamChainDisconnectRequest takes no fields at all)."""
    return _ok({})


@registry.endpoint("account/deletion_request")
def handle_deletion_request(payload: dict) -> dict:
    """Starts the real account-deletion grace-period countdown (dump.cs:
    AccountDeletionRequestRequest/Response both carry no fields -- the
    countdown itself surfaces globally afterward via data_headers.
    account_deletion_cancellation_period, see patch.py). This is a REAL
    deletion, not wire theater: once the grace period elapses, the account
    is actually wiped (see main.py's tool/start_session handling)."""
    viewer_id = payload["viewer_id"]
    accounts.request_deletion(viewer_id)
    return _ok({})


@registry.endpoint("account/deletion_cancel")
def handle_deletion_cancel(payload: dict) -> dict:
    """Cancels a pending deletion (dump.cs: AccountDeletionCancelRequest/
    Response both carry no fields)."""
    viewer_id = payload["viewer_id"]
    accounts.cancel_deletion(viewer_id)
    return _ok({})
