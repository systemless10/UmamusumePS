"""Client-infrastructure endpoints: attestation, telemetry, settings, banners,
and the tutorial's own siblings.

None of these is gameplay, and most of them are acknowledgements the client
posts and barely reads. They still belong in real handlers rather than on
main.py's NOOP_SUCCESS, for one reason: NOOP_SUCCESS answers with an EMPTY
`data`, and MessagePack C# leaves an absent field at its type default -- null
for a string or an array, which the client then uses without a guard. That is
the exact mechanism behind present/history's blank tab and the practice-race
track-select crash. Where dump.cs declares a field, something real goes in it.

Every shape here is read field-by-field off dump.cs; none has been captured.
Where a response genuinely declares no fields at all (ToolDeviceAttest,
ToolSendLog, UpdateForceDisplayStatus, Tutorial) an empty `data` IS the correct
answer, and these handlers exist only so the log stops calling them unhandled
and so the few that DO carry state actually keep it.
"""

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


# =========================================================== attestation ====

@registry.endpoint("tool/device_attest")
def handle_device_attest(payload: dict) -> dict:
    """{credential, error_code, error_message, attestation_type} -> {}.

    Play Integrity / App Attest. The response declares no fields, so accepting
    is literally an empty success -- and accepting is the only sensible policy
    for a private server: the credential can only be validated against Google's
    or Apple's attestation service, and a private server has no business
    locking its own players out on the word of one.

    attestation_type is worth logging rather than dropping: it is how an iOS
    client is told apart from an Android one here (type 1 = iOS), which matters
    because the iOS client takes a different path at account/index."""
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
    """-> {token}. A one-shot nonce the client folds into the next attestation.

    Freshly random per call rather than a constant: the token's whole purpose
    is to be unrepeatable, and a null or fixed string is the kind of thing a
    client-side check can reject outright. Nothing here verifies it later --
    see handle_device_attest for why that is deliberate."""
    return _ok({"token": secrets.token_hex(16)})


@registry.endpoint("tool/get_pre_download_resource_version")
def handle_get_pre_download_resource_version(payload: dict) -> dict:
    """-> {resource_version}. The version the client should PRE-download, asked
    ahead of an update.

    Answers the version we actually serve, from the same config key
    tool/start_session reports. Naming any other version starts an asset
    download for a bundle this server does not have; a null one (the old no-op)
    is a null string on a code path that compares it against the local one."""
    from . import load as load_handler       # noqa: F401  (kept for symmetry)
    from .. import main as _main
    version = str(config.get("resource_version", _main.GAME_RESOURCE_VERSION))
    return _ok({"resource_version": version})


@registry.endpoint("tool/send_log")
def handle_send_log(payload: dict) -> dict:
    """{log_key, log_message} -> {}. Client telemetry.

    Not silently dropped: the client only posts here when something on its side
    is worth reporting, so the message goes to our log where it can be read
    next to whatever the server was doing at the time. This is the same
    reasoning that makes Player.log worth reading."""
    log.info("client log [key=%s]: %s", payload.get("log_key"),
             str(payload.get("log_message"))[:2000])
    return _ok({})


# =============================================================== settings ===

@registry.endpoint("option/change_option")
def handle_change_option(payload: dict) -> dict:
    """{option_info_array:[{type, value}]} -> {option_info_array}.

    Account-level client settings. The response echoes the options, so it has
    to echo what is STORED, not what was asked for -- otherwise the screen
    confirms a setting that did not persist and reverts on the next login.
    Incoming entries are merged by `type` into the stored set, and the whole
    merged set comes back."""
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
    """{event_id} -> {}. The client acknowledging a forced popup/banner.

    The ack is recorded even though the response is empty: whatever decides to
    force a display next needs to know this one has been seen, and an ack that
    goes nowhere means the same popup every launch."""
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
    """{banner_id} -> {url}. Where a home-screen banner should send the player.

    Every real destination is a Cygames web page this server has no equivalent
    for, so there is nothing honest to point at -- but the field must still be a
    STRING. An empty string is the answer that leaves the banner inert; a null
    (the old no-op) is a null string handed to whatever opens the URL."""
    log.info("banner_url: no destination for banner %s", payload.get("banner_id"))
    return _ok({"url": ""})


# =============================================================== tutorial ===

@registry.endpoint("tutorial")
def handle_tutorial(payload: dict) -> dict:
    """{step} -> {}. Tutorial progress ping. Recorded so the step survives a
    reconnect mid-tutorial; the response genuinely declares no fields."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[TUTORIAL_STEP_KEY] = int(payload.get("step") or 0)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("tutorial/team_edit")
def handle_tutorial_team_edit(payload: dict) -> dict:
    """{team_data_array, team_evaluation_point} -> {reward_info_array,
    before_rank, after_rank}. The tutorial's one-off Team Trials line-up step.

    Saves through the SAME team_stadium state the real team editor writes, so
    the team the tutorial built is the team the player then has -- storing it
    anywhere else would mean the tutorial's line-up silently vanished the moment
    it ended.

    reward_info_array is empty and before_rank == after_rank: the tutorial's
    own completion rewards are not this endpoint's to grant (they arrive with
    the tutorial's finish), and inventing a rank jump here would put the account
    on a Team Trials rank it never earned."""
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
    """-> the tutorial career's graduation payload: {trained_chara,
    directory_card_array, directory_ranking, trained_chara_id, love_point_info,
    reward_item_info, support_card_data_array, limited_shop_info,
    update_user_chara_info, new_chara_profile_array}.

    PARTIAL, and deliberately so. This server skips the tutorial
    (tutorial/skip is the path every account here takes), so no tutorial career
    ever actually runs and there is no graduate for this call to hand back. What
    it does do is answer with the account's REAL state in every field that has
    one -- roster, archive, owned support cards -- and a well-formed empty in
    the rest, because the alternative on the no-op fallback is ten null fields
    on the screen that renders a brand new account's first horse.

    Fabricating a trained chara here would be worse than partial: it would put a
    career on the roster that was never trained. If the tutorial career is ever
    genuinely wired up, the graduate belongs in trained_chara.build_trained_chara
    _from_career like every other one, and this handler should read it from
    there rather than growing its own."""
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
        # Empty objects, NOT null: these are nested structs, and MessagePack C#
        # hands an absent one back as a null reference the client then walks.
        "love_point_info": {},
        "reward_item_info": {},
        "support_card_data_array": list(full_state.get(collection.SUPPORT_CARD_KEY) or []),
        "limited_shop_info": {},
        "update_user_chara_info": {},
        "new_chara_profile_array": [],
    })
