"""
Entry point. One catch-all route dispatches every API call either to a real
stateful handler (app/handlers/) or, if none exists yet, to fixture replay
(the closest captured real response, with session-dependent fields patched).

Run with:
    uvicorn app.main:app --reload --port 8080
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

import msgpack
from fastapi import FastAPI, Request, Response

from . import accounts
from . import config
from . import crypto
from . import jp_bridge
from . import jp_crypto
from . import master_data
from . import region
from . import scenarios
from . import state as state_store
from .fixtures import store as fixtures
from .handlers import (collection, gacha, jukebox, live_theater, load, practice_race,
                       registry, shop, single_mode_team, tool, trained_chara)
from .patch import make_sid, patch_response
from . import session as session_store

# The one other hostname this server answers for -- see dispatch()'s
# docstring for why the Host-header check lives in exactly one place.
JP_HOST = "api.games.umamusume.jp"

# Feature modules self-register their endpoints (handlers/registry.py) so new
# features never edit this file. Loaded once at import.
registry.autoload()

# Console keeps its level (UMA_LOG_LEVEL, default INFO); the FILE always
# records DEBUG. Diagnosing a live incident used to mean having had the right
# level set before it happened -- the 2026-09-05 debut report had no server-side
# record at all because nothing was ever written to disk.
_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "server.log", maxBytes=20_000_000, backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
logging.basicConfig(level=logging.DEBUG,
                    handlers=[logging.StreamHandler(), _file_handler])
logging.getLogger().handlers[0].setLevel(
    os.environ.get("UMA_LOG_LEVEL", "INFO").upper())
log = logging.getLogger("uma-server")

# TEMP DEBUG: monotonic request counter for naming dumped response snapshots.
import itertools as _itertools
_seq_counter = _itertools.count(1)
def _request_seq() -> str:
    return f"{next(_seq_counter):04d}"

# Printed (not logged) so it shows regardless of UMA_LOG_LEVEL, and ASCII-only
# so a redirected stdout on a Windows code page can't choke on it.
_BANNER = """
UmaPS
UmaPS is free software, released under the MIT License.
If you paid for this, you have been scammed.
"""

from contextlib import asynccontextmanager as _asynccontextmanager

@_asynccontextmanager
async def _lifespan(_app: FastAPI):
    print(_BANNER, flush=True)
    yield

app = FastAPI(title="Umamusume private server (dev)", lifespan=_lifespan)
# The Hachimi plugin posts to this plain-HTTP side channel, not the game
# client -- see jp_bridge.py's module docstring for why it's a separate
# listener rather than a route on this (HTTPS-only) app.
jp_bridge.start()

# GallopResultCode.SESSION_ERROR (dump.cs) -- the real client's own constant
# for "your session/sid is invalid", distinct from PARAM_ERROR (205, every
# ordinary business-logic refusal on this server) and AUTH_ERROR (202,
# credential-based failures like a bad transition-code password). Used only
# by the sid-mismatch short-circuit in dispatch() below.
_SESSION_ERROR = 201

# endpoint (as seen in the captures' "endpoint" field, slash form) -> handler
# a handler takes the request payload dict and returns the full response
# envelope dict ({"response_code", "data_headers", "data"}).
HANDLERS = {
    # The live client's main career (URA and others via SingleModeURAAPI etc.)
    # posts to the single_mode/* family; our original capture set is the
    # scenario-2 single_mode_team/* family. Both use the SAME request/response
    # types (SingleModeStartRequest -> SingleModeStartCommon, ...), so the same
    # handlers serve both -- single_mode_team/* fixtures act as the structural
    # template regardless of which prefix came in. Without the single_mode/*
    # routes, a real career start hit the no-op fallback (empty
    # single_mode_start_common -> client NullRef in ApplySingleModeStartResponse
    # -> "start career does nothing").
    "single_mode/start": single_mode_team.handle_start,
    "single_mode/load": single_mode_team.handle_load,
    "single_mode/exec_command": single_mode_team.handle_exec_command,
    "single_mode/check_event": single_mode_team.handle_ura_check_event,
    "single_mode/get_choice_reward": single_mode_team.handle_get_choice_reward,
    "single_mode/race_entry": single_mode_team.handle_ura_race_entry,
    "single_mode/change_running_style": single_mode_team.handle_change_running_style,
    "single_mode/race_start": single_mode_team.handle_ura_race_start,
    "single_mode/race_end": single_mode_team.handle_ura_race_end,
    "single_mode/race_out": single_mode_team.handle_ura_race_out,
    "single_mode/race_analyze": single_mode_team.handle_race_analyze,
    "single_mode/finish": single_mode_team.handle_finish,
    "single_mode/multi_race_reserve": single_mode_team.handle_multi_race_reserve,
    "single_mode/gain_skills": single_mode_team.handle_gain_skills,
    "single_mode/minigame_end": single_mode_team.handle_minigame_end,
    "single_mode/continue": single_mode_team.handle_continue,
    "single_mode/factor_select": single_mode_team.handle_factor_select,
    # Aliased onto every scenario prefix by _install_scenario_routes below, so
    # the paid spark re-roll and the skip setting exist for URA, Aoharu, Grand
    # Live and Trackblazer from this one pair of lines.
    "single_mode/factor_lottery": single_mode_team.handle_factor_lottery,
    "single_mode/change_short_cut": single_mode_team.handle_change_short_cut,
    "pre_single_mode/index": single_mode_team.handle_pre_single_mode_index,
    "pre_single_mode/friend_support_card_reload":
        single_mode_team.handle_friend_support_card_reload,
    # race/analyze is the TOP-LEVEL twin of single_mode*/race_analyze -- dump.cs
    # gives both the same request ({program_id, current_turn}) and the same
    # single-field response ({race_horse_data_array}), so it is the same screen
    # and the same handler, not a second implementation.
    "race/analyze": single_mode_team.handle_race_analyze,
    "live_theater/index": live_theater.handle_index,
    "live_theater/live_start": live_theater.handle_live_start,
    "gacha/index": gacha.handle_index,
    "gacha/exec": gacha.handle_exec,
    "gacha/limit_exchange": gacha.handle_limit_exchange,
    "item/show_exchange": shop.handle_show_exchange,
    "item/exchange": shop.handle_exchange,
    "item/exchange_multi": shop.handle_exchange_multi,
    "item/exchangeAddFrame": shop.handle_exchange_add_frame,
    "item/check_aniv_shop": shop.handle_check_aniv_shop,
    "jukebox/index": jukebox.handle_index,
    "single_mode_team/start": single_mode_team.handle_start,
    "single_mode_team/load": single_mode_team.handle_load,
    "single_mode_team/exec_command": single_mode_team.handle_exec_command,
    # The race flow uses the SAME handlers as single_mode/* for the same reason
    # start/load/exec_command/check_event above do. It used to point at the
    # naive capture-replay pair (handle_race_entry / _handle_race_flow_step),
    # which served the captured MARUZENSKY race responses verbatim -- her
    # chara_info, stats, aptitudes and bonds -- because the race endpoints are
    # exactly the ones _sync_chara_info skips, so nothing patched them on the
    # way out. That was fixed for single_mode/* on 2026-08-20 (see
    # handle_ura_race_start's docstring: "the race itself still ran as swimsuit
    # Maruzensky") but the team-prefix twin was left on the old handlers, so
    # any career posting single_mode_team/* still got the whole leak -- live
    # report 2026-09-05: a Mayano Top Gun run took on Maru's state at the
    # turn-12 debut and softlocked.
    "single_mode_team/race_entry": single_mode_team.handle_ura_race_entry,
    "single_mode_team/race_start": single_mode_team.handle_ura_race_start,
    "single_mode_team/race_end": single_mode_team.handle_ura_race_end,
    "single_mode_team/race_out": single_mode_team.handle_ura_race_out,
    "single_mode_team/finish": single_mode_team.handle_finish,
    "support_card_deck/change_party": collection.handle_change_party,
    "tool/signup": tool.handle_signup,
    "load/index": load.handle_load_index,
    "practice_race/race_start": practice_race.handle_race_start,
    "practice_race/get_follow_user_data": practice_race.handle_get_follow_user_data,
    "trained_chara/load": trained_chara.handle_trained_chara_load,
    "trained_chara/change_nickname": trained_chara.handle_change_nickname,
    "trained_chara/change_lock_multi": trained_chara.handle_change_lock_multi,
    "trained_chara/change_memo": trained_chara.handle_change_memo,
    "trained_chara/remove": trained_chara.handle_remove,
    "trained_chara/get_succession_history_array":
        trained_chara.handle_get_succession_history_array,
}


# A bare, well-formed success envelope for endpoints we have neither a
# handler nor a captured fixture for. The real client is unforgiving about
# malformed responses (a response_code:0/error body reliably surfaces as a
# generic "Could not receive parameters from server" and derails the whole
# session) -- an empty-but-valid success is a much safer default than an
# error for background/non-critical calls (voice note saves, telemetry,
# etc.). Endpoints where an empty response actually breaks something belong
# in HANDLERS or need a fixture captured, not a better fallback here.
NOOP_SUCCESS = {
    "response_code": 1,
    "data_headers": {"result_code": 1, "notifications": {}},
    "data": {},
}


# Career-START endpoints of scenarios we DON'T implement. These must REFUSE,
# never no-op: answering success fabricates a started career in the client's
# local state, which then layers that scenario's UI over the real URA run --
# live-hit: an accidental Unity Cup start got the no-op, the client believed a
# Trackblazer career existed ('Continue Career: Unity Cup' on the home dialog),
# and winning a later URA goal race rendered Trackblazer's Twinkle Star Climax
# screen over the URA career and softlocked. result_code 205 makes the client
# show a plain error and stay on the setup screen instead.
# (single_mode_live/start and single_mode_free/start are NOT here any more --
# Grand Live and Trackblazer are implemented, see HANDLERS above. Everything
# still listed is a scenario we genuinely don't serve.)
_UNSUPPORTED_STARTS = ("single_mode_team/start_team",
                       "single_mode_venus/start",
                       "single_mode_pioneer/start", "single_mode_arc/start",
                       "single_mode_grand_masters/start",
                       "single_mode_project_l/start")
_REFUSE = {"response_code": 1,
           "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


# DEBUG BISECTION HOOK. Set UMA_REPLAY_NEWEST to a comma-separated endpoint
# list to bypass its handler and serve the NEWEST captured response verbatim
# (patch.py still stamps viewer_id/sid/servertime). This answers "is our
# generated response the problem, or the endpoint itself?" in one launch --
# if a real recorded response also fails, the content is not the fault.
# Off unless the env var is set; safe to leave in place.
_REPLAY_NEWEST = {e.strip() for e in os.environ.get("UMA_REPLAY_NEWEST", "").split(",") if e.strip()}

# Same idea, but from a LITERAL file (tools/capture_proxy.py output) instead of
# the fixture index -- lets a specific real, TODAY-fresh capture (same client
# build) be replayed verbatim, which is a much stronger test than anything in
# the (2026-07) fixture set. Format: "endpoint=/path/to/capture.json,...".
_REPLAY_FILE: dict[str, str] = {}
for _entry in os.environ.get("UMA_REPLAY_FILE", "").split(","):
    _entry = _entry.strip()
    if "=" in _entry:
        _ep, _path = _entry.split("=", 1)
        _REPLAY_FILE[_ep.strip()] = _path.strip()


def _file_capture(endpoint: str):
    path = _REPLAY_FILE.get(endpoint)
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("response")


def _newest_capture(endpoint: str):
    pairs = fixtures.all_for(endpoint)
    return max(pairs, key=lambda p: p.ts).response if pairs else None


# Endpoints that fell through to the no-op success this process run. The
# request logger below dumps each one's REQUEST payload exactly once, so a
# blank/hanging screen can be traced to its endpoint by opening it once --
# no proxy, no client mod.
_UNHANDLED_SEEN: set[str] = set()
_UNHANDLED_DUMPED: set[str] = set()


def _fallback_response(endpoint: str) -> dict:
    if endpoint in _REPLAY_FILE:
        resp = _file_capture(endpoint)
        if resp is not None:
            log.warning("UMA_REPLAY_FILE: serving literal capture file for %s", endpoint)
            return copy.deepcopy(resp)
    if endpoint in _REPLAY_NEWEST:
        resp = _newest_capture(endpoint)
        if resp is not None:
            log.warning("UMA_REPLAY_NEWEST: serving newest raw capture for %s", endpoint)
            return copy.deepcopy(resp)
    if any(endpoint.endswith(u) for u in _UNSUPPORTED_STARTS):
        log.warning("REFUSING unsupported scenario start %s (result_code 205)", endpoint)
        return _REFUSE
    pair = fixtures.first(endpoint)
    if pair is not None:
        log.info("fixture replay for %s (no handler / no exact match)", endpoint)
        return pair.response
    log.warning("no handler/fixture for %s -- returning no-op success", endpoint)
    _UNHANDLED_SEEN.add(endpoint)
    return NOOP_SUCCESS


# ---- client-version compatibility -------------------------------------------
# Our fixtures were captured from an OLDER build, so a game update leaves them
# subtly stale. These are the drift points that actually break the client;
# re-check them (diff a fresh capture's response keys against the fixtures)
# whenever the game updates again.
#
# resource_version: the client compares this to its local assets -- serving the
# old one makes it re-download the resource bundle on every launch (the "80MB
# download"). Bump to the value a fresh tool/start_session returns.
#
# The live value is read from client_config.json (next to the server package) so
# it can be tweaked without editing code; this is only the fallback used when
# that file is missing or unreadable.
GAME_RESOURCE_VERSION = "10007550"   # 2026-08-25, authoritative -- read straight
                                      # off a real client's own RES-VER request
                                      # header in the newest capture_proxy.py
                                      # --upstream real session (captures/
                                      # 20260825_094909/0203_support_card_deck_
                                      # change_party.json, timestamp 12:05,
                                      # APP-VER still 1.34.0 unchanged). Was
                                      # 10007400.

def _resource_version() -> str:
    return str(config.get("resource_version", GAME_RESOURCE_VERSION))

# Fields the newer client expects that pre-update fixtures don't contain.
# pre_single_mode/index gained default_running_style_array; without it the career
# setup screen never finishes initializing (Start does nothing, UI unclickable).
_ADDED_RESPONSE_FIELDS = {
    "pre_single_mode/index": {"default_running_style_array": []},
    # load/index IS the client's LoginTask -- established from the game's own
    # il2cpp metadata (global-metadata.dat), where LoginRequest declares
    # exactly one field, `adid`, which is what the client posts to load/index.
    # That metadata also lists every field Gallop.LoginResponse declares, and
    # diffing it against what we actually serve found these EIGHT missing --
    # all of them additions in builds newer than our 2026-07 captures. A
    # response that omits fields the client's formatter declares is what
    # surfaces as the generic "Could not receive parameters from server".
    #
    # Provenance of each value (setdefault, so a real handler value always wins):
    #   transfer_event_info      nullable dict, 19 captures, null is valid  [cap]
    #   idle_single_mode_load_info nullable dict, 9 captures, null is valid [cap]
    #   option_info_array        list, 8 captures, all [{"type":3,"value":0}] [cap]
    # The remaining five appear in NO capture (they postdate every dump we
    # have), so their types are inferred from their names and are the least
    # certain part of this entry -- an `_array` gets [], an int-ish id/rank
    # gets 0. If the client starts complaining about one of these BY NAME,
    # that names the type to correct.
    # ONLY the two fields a real server response actually carries. Verified
    # against a live capture (tools/capture_proxy.py --upstream real).
    #
    # 2026-08-11 ROOT CAUSE, for the record: six more fields used to be listed
    # here, invented from the il2cpp metadata's field list with types GUESSED
    # from their names ("an int-ish id/rank gets 0"). One of those guesses was
    # `recheck_steam_order_id: 0` -- and C# declares it `public string`
    # (dump.cs, LoginResponse.CommonResponse). It serialized as msgpack
    # positive-fixint 0x00 as the LAST field of the map, so the client's
    # generated formatter walked the whole response, reached it, tried to read
    # a string, and reported exactly:
    #     LoginResponse Deserialize error at field: code is invalid.
    #     code:0 format:positive fixint
    # ("code" there is the deserializer's word for the msgpack TYPE BYTE, not
    # a field name -- which is why grepping every capture for a field called
    # "code" found nothing.) Proven by hooking Gallop.LoginTask.Deserialize in
    # the live client and dumping the exact bytes it received.
    #
    # The real server sends NONE of those six. Absent keys are fine: the
    # formatter is AutomataDictionary/map-based and simply leaves unset fields
    # at their C# defaults. Do not "helpfully" add fields the real server
    # omits -- and never infer a wire type from a field NAME.
    "load/index": {
        "idle_single_mode_load_info": None,
        "option_info_array": [{"type": 3, "value": 0}],
    },
}


def _install_scenario_routes() -> None:
    """Give every registered scenario its own endpoint family.

    A scenario posts its career to its own prefix (URA single_mode/*, Grand Live
    single_mode_live/*), but the requests and responses are the SAME types --
    the only wire difference is the data_set the scenario's attach() swaps in at
    single_mode_team's one chokepoint. So each prefix is aliased onto the very
    same handler objects: one code path, no per-scenario branch in between, and
    URA behaviour that cannot drift because there is nothing scenario-specific
    on the way. Endpoints a scenario has and the shared family does not (Grand
    Live's master_square / live_start) come from its extra_endpoints().

    This used to be 18 hand-written "single_mode_live/..." lines. Adding a
    scenario now adds none.
    """
    shared = {ep.split("/", 1)[1]: fn for ep, fn in HANDLERS.items()
              if ep.startswith("single_mode/")}
    for scen in scenarios.all_scenarios():
        prefix = scen.endpoint_prefix
        if prefix == "single_mode":
            continue                     # the family `shared` was read from
        routes = dict(shared)
        routes.update(scen.extra_endpoints())
        for suffix, fn in routes.items():
            HANDLERS.setdefault(f"{prefix}/{suffix}", fn)


_install_scenario_routes()


_scenario_ids_cache: list | None = None


def _scenario_ids() -> list:
    """The career scenario list to serve. client_config.json's "scenario_ids"
    wins when set (a list, for experimenting); otherwise it's auto-detected from
    master.mdb."""
    override = config.get("scenario_ids")
    if isinstance(override, list) and override:
        try:
            return [int(x) for x in override]
        except (TypeError, ValueError):
            log.warning("client_config.json scenario_ids not a list of ints: %r", override)
    # Released in master.mdb AND implemented here. Either half alone is wrong:
    # master.mdb lists scenarios we have no package for (advertising one puts
    # the player into a career nothing serves), and the registry knows nothing
    # about release dates or which region's data this is.
    implemented = set(scenarios.implemented_ids())
    return [sid for sid in _available_scenario_ids() if sid in implemented]


def _available_scenario_ids() -> list:
    """Career scenarios currently released, straight from master.mdb. The
    fixtures freeze whatever set existed when they were captured (e.g. [1,2,4]),
    so a game update that adds a scenario silently keeps it hidden -- reading
    master.mdb (which the update refreshes) picks new ones up automatically.
    Uses real time, not the frozen servertime, since a scenario released after
    the fixture capture would otherwise still be filtered out."""
    global _scenario_ids_cache
    if _scenario_ids_cache is None:
        now = int(time.time())
        try:
            rows = master_data.query(
                "SELECT id FROM single_mode_scenario "
                "WHERE start_date <= ? AND end_date > ? ORDER BY id", (now, now))
            _scenario_ids_cache = [r["id"] for r in rows]
        except Exception:  # noqa: BLE001 - never break a response over this
            log.exception("could not read scenario list from master.mdb")
            _scenario_ids_cache = []
    return _scenario_ids_cache


def _apply_version_compat(endpoint: str, response: dict) -> dict:
    """Bring a fixture-derived response up to the current client build."""
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    if "resource_version" in data:
        # ALWAYS serve the true authoritative version, never the client's own
        # stale self-report. A prior version of this echoed client_res_ver back
        # when present, reasoning "that can never trigger a download" -- but
        # that's backwards: the client staying on a stale version (confirmed
        # live: it reports 10006910 while the real Cygames server's own
        # tool/start_session response says 10007310) IS the bug, not something
        # to preserve. Telling it the real current value is what makes it
        # actually catch up, exactly like the real server does.
        data["resource_version"] = _resource_version()
    if "single_mode_scenario_id_array" in data:
        ids = _scenario_ids()
        if ids:
            data["single_mode_scenario_id_array"] = list(ids)
    for field, default in _ADDED_RESPONSE_FIELDS.get(endpoint, {}).items():
        data.setdefault(field, copy.deepcopy(default))
    return response


def _handle_decoded(endpoint_path: str, payload: dict, presented_sid: bytes | None) -> dict:
    """The shared game-logic core: decoded request in, patched response dict
    out. Region-agnostic by construction -- everything that differs between
    Global and JP (wire crypto, which DB file gets used) lives at the
    boundary around this function (region.use_region, crypto.py vs
    jp_crypto.py/jp_bridge.py), never inside it."""
    endpoint = endpoint_path.strip("/")
    if endpoint.startswith("umamusume/"):
        # The real client's BASE_URL includes a "/umamusume/" prefix
        # (https://api.games.umamusume.com/umamusume/{ep}); our fixtures and
        # HANDLERS are keyed by the bare endpoint, so strip it here rather
        # than everywhere else.
        endpoint = endpoint[len("umamusume/") :]
    viewer_id = payload.get("viewer_id", "unknown")

    # Full payload at DEBUG (noisy -- some endpoints carry huge arrays), just
    # the endpoint name at INFO. Skip the boilerplate device-profile fields
    # every request carries so the interesting bits aren't buried.
    if log.isEnabledFor(logging.DEBUG):
        _boilerplate = {
            "device", "device_id", "device_name", "graphics_device_name",
            "ip_address", "platform_os_version", "carrier", "keychain",
            "locale", "button_info", "dmm_viewer_id", "dmm_onetime_token",
            "steam_id", "steam_session_ticket",
        }
        interesting = {k: v for k, v in payload.items() if k not in _boilerplate}
        log.debug("REQUEST %s payload=%s", endpoint, interesting)
    else:
        log.info("REQUEST %s", endpoint)

    # RAW bytes, not hex -- accounts.check_session compares this against an
    # MD5 digest it computes itself (see its docstring for why a hex string
    # here would never match anything). Callers pass this in (Global reads it
    # from decoded.sid; JP has no equivalent yet, so passes None).
    handler = HANDLERS.get(endpoint) or registry.get(endpoint)
    if endpoint in _REPLAY_NEWEST or endpoint in _REPLAY_FILE:
        handler = None  # force the raw-capture path in _fallback_response
    # ONE request at a time per viewer: every handler is a read-modify-write
    # over the viewer's state blob, and get_state/save_state lock separately,
    # so two in-flight requests for the same account raced. Different viewers
    # stay fully concurrent. Session/sid bookkeeping is IN this same lock too
    # (moved 2026-08-18, alongside the frozen-sid bug fixed below) -- it was
    # previously outside it, so two genuinely-concurrent requests for one
    # viewer could each read the sid stored by the OTHER before either had
    # written its own roll, a real race that would cause spurious mismatches
    # once auth_enforce is on, independent of the frozen-sid bug.
    with accounts.viewer_lock(viewer_id):
        # Lazy account-deletion wipe: account/deletion_request starts a grace
        # period (accounts.DELETION_GRACE_PERIOD_SECONDS); tool/start_session
        # is the natural place to check whether it has elapsed, since it's
        # the one call guaranteed to happen at the start of every session.
        # deletion_days_remaining returns 0 once the deadline has passed (but
        # the row hasn't been swept yet) -- actually deleting here means the
        # very next thing this endpoint does (touch/start_session below) acts
        # on a genuinely fresh/nonexistent account, exactly as if the real
        # account had been permanently removed.
        if endpoint == "tool/start_session" and accounts.deletion_days_remaining(viewer_id) == 0:
            accounts.delete_account(viewer_id)
            state_store.delete_state(viewer_id)
        # tool/start_session establishes a fresh session BASE; every other
        # call rolls a new sid on the current base (session.py's docstring --
        # verified against a real login that the real sid changes on every
        # response, not just at session start).
        sid = (session_store.start_session(viewer_id) if endpoint == "tool/start_session"
               else session_store.get_or_create_sid(viewer_id))
        session_ok = True
        if endpoint == "tool/start_session":
            accounts.touch(viewer_id, sid)
        else:
            # REAL BUG, found 2026-08-18: this used to call accounts.touch(viewer_id)
            # with NO sid on every non-start_session call, so the stored "last
            # known sid" only ever got set once, at start_session, and stayed
            # frozen there for the rest of the session -- while `sid` above
            # rolls a NEW value on every single call (by design, matching the
            # real server's own behavior). The client correctly tracks and
            # re-presents whatever sid it was last given, so from the SECOND
            # call onward `presented_sid` was always one step ahead of our
            # frozen stored value: a guaranteed mismatch on nearly every
            # request, regardless of whether the client did anything wrong.
            # This is why "sid mismatch" warnings appeared on almost every
            # logged request all session -- enforce=False (the default) only
            # ever WARNED on it, so gameplay was unaffected. Check the
            # PRESENTED sid against what we stored from the PREVIOUS
            # response (the real check) BEFORE overwriting it with the new
            # one we're about to send back for the NEXT call to be checked
            # against.
            #
            # SECOND real bug, found the same day auth_enforce was first
            # tested live: check_session's return value was never actually
            # READ anywhere -- the call above executed but nothing branched
            # on its result, so auth_enforce had ZERO effect on whether a
            # request proceeded regardless of its config value; only the
            # WARNING's own "(enforce=True)" text differed. Every request
            # kept returning 200 even with mismatches logged (confirmed live
            # 2026-08-18: three straight "sid mismatch ... enforce=True"
            # warnings, three straight 200 OKs). So enforcement wasn't just
            # silently failing, it was never wired up in the first place.
            #
            # Now actually acts on it: a rejected session short-circuits
            # BEFORE the handler runs, with GallopResultCode.SESSION_ERROR
            # (201, dump.cs's own real result-code constant for exactly
            # this -- not the generic PARAM_ERROR/205 every other refusal on
            # this server uses) so a genuine auth failure is distinguishable
            # from an ordinary business-logic refusal, both in the log and
            # to the client, instead of surfacing as an unlabeled
            # "Connection Error Occurred".
            session_ok = accounts.check_session(
                viewer_id, presented_sid, enforce=bool(config.get("auth_enforce", False)))
            if session_ok:
                accounts.touch(viewer_id, sid)
            else:
                # NEWEST LOGIN WINS. A second client's tool/start_session
                # mints a fresh session base and stores its sid, so the first
                # client's next request presents a sid from the old session
                # and lands here. It gets SESSION_ERROR (201), which returns
                # it to the title screen. Two things must NOT happen to it:
                #   * touch(): storing this response's sid would overwrite the
                #     new client's stored sid, and the NEW client would be
                #     kicked on its next request instead of the old one.
                #   * a valid sid: `sid` above was rolled on the new session's
                #     base, so handing it back would let the kicked client
                #     carry on inside the new session. It gets a throwaway.
                log.warning("kicking stale session for viewer %s from %s "
                            "(logged in from another client)", viewer_id, endpoint)
                sid = make_sid()

        if not session_ok:
            # Skip the handler entirely -- still flows through the normal
            # patch_response/encode tail below so data_headers gets the
            # correct viewer_id/servertime/sid like every other response.
            response = {"response_code": 1,
                       "data_headers": {"result_code": _SESSION_ERROR, "notifications": {}},
                       "data": {}}
        else:
            # Which endpoint FAMILY this arrived on. See
            # single_mode_team.CURRENT_ENDPOINT -- the career handlers are
            # shared across all four scenario prefixes, and one of them
            # (handle_load with nothing persisted) has no career to read
            # the scenario off.
            single_mode_team.CURRENT_ENDPOINT.set(endpoint)
            response = handler(payload) if handler is not None else _fallback_response(endpoint)
            response = _apply_version_compat(endpoint, response)
            response = single_mode_team.sync_chara_info(viewer_id, endpoint, response)
            if endpoint == "tool/start_session":
                # tool/start_session is a fixture replay (no handler): its
                # frozen data_headers.notifications carries whatever the
                # captured account genuinely had unread AT CAPTURE TIME
                # ({"unread_information_exists": 1}) and, left alone, serves
                # that identically on literally the FIRST call of every
                # single launch forever -- the same "frozen seed
                # masquerading as live state" bug class as load.py's
                # unread_announce_id_array fix (see that one's comment for
                # the full user report this and it share: "supposed to be
                # one time but keep showing up... every time I restart the
                # game"). No real handler here to compute this honestly, so
                # just stop repeating the one-time flag after its first use.
                notifs = (response.get("data_headers") or {}).get("notifications")
                if isinstance(notifs, dict) and notifs.get("unread_information_exists"):
                    full_state = state_store.get_state(viewer_id) or {}
                    if full_state.get("seen_unread_information_flag"):
                        notifs["unread_information_exists"] = 0
                    else:
                        full_state["seen_unread_information_flag"] = True
                        state_store.save_state(viewer_id, full_state)

    # TEMP DEBUG: dump single_mode training-screen responses so we can diff the
    # exact response that crashes the client against a known-good capture.
    if endpoint.startswith("single_mode") and ("check_event" in endpoint or "exec_command" in endpoint or "get_choice_reward" in endpoint or "load" in endpoint):
        try:
            import json as _json, os as _os
            _dbg = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "debug_responses")
            _os.makedirs(_dbg, exist_ok=True)
            _d = (response or {}).get("data", {})
            _ci = _d.get("chara_info", {}) if isinstance(_d, dict) else {}
            _hi = _d.get("home_info", {}) if isinstance(_d, dict) else {}
            _smlc = _d.get("single_mode_load_common", {}) if isinstance(_d, dict) else {}
            _boiler = {"device", "device_id", "device_name", "graphics_device_name",
                       "ip_address", "platform_os_version", "carrier", "keychain", "locale",
                       "button_info", "dmm_viewer_id", "dmm_onetime_token", "steam_id",
                       "steam_session_ticket", "viewer_id"}
            _snap = {
                "endpoint": endpoint,
                "req_full": {k: v for k, v in payload.items() if k not in _boiler},
                "req": {k: payload.get(k) for k in ("current_turn", "command_id", "event_id", "choice_number")},
                "turn": _ci.get("turn"),
                "playing_state": _ci.get("playing_state"),
                "state": _ci.get("state"),
                "race_program_id": _ci.get("race_program_id"),
                "stats": {s: _ci.get(s) for s in ("speed", "stamina", "power", "guts", "wiz", "skill_point")} if _ci else None,
                "data_top_level_keys": sorted(_d.keys()) if isinstance(_d, dict) else None,
                "has_team_data_set": "team_data_set" in _d if isinstance(_d, dict) else None,
                "chara_info_full": _ci,
                "home_info_full": _hi,
                "load_common_present": bool(_smlc),
                "load_common_turn": (_smlc.get("chara_info") or {}).get("turn") if isinstance(_smlc, dict) else None,
                "load_common_has_team_data_set": "team_data_set" in _smlc if isinstance(_smlc, dict) else None,
                "resp_choice_reward_array": _d.get("choice_reward_array") if isinstance(_d, dict) else None,
                "unchecked_event_array": _d.get("unchecked_event_array"),
            }
            _fn = _os.path.join(_dbg, f"{_request_seq()}_{endpoint.replace('/', '_')}.json")
            with open(_fn, "w", encoding="utf-8") as _fh:
                _json.dump(_snap, _fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # TEMP DEBUG: endpoints whose REAL request shape is unverified (no genuine
    # capture -- multi_race_reserve was never captured at all; gain_skills'
    # one capture has a mislabeled request body, see handle_gain_skills).
    # Dump the FULL raw request+response verbatim (not the chara_info-focused
    # summary above) so the first live use confirms the true field names.
    if ("multi_race_reserve" in endpoint or "gain_skills" in endpoint
            or "minigame_end" in endpoint or "gacha/exec" in endpoint
            or "gacha/limit_exchange" in endpoint
            or "jukebox/" in endpoint
            or "item/exchange" in endpoint
            # 2026-09-11 live report: "claiming a present doesn't update until
            # I restart, at least for carats". Our receive_all body already
            # matches the real server's capture field-for-field (including
            # add_fcoin, which IS the live-apply channel -- the real session
            # calls only present/index afterwards, never a wallet refetch), so
            # the divergence has to be in what actually goes out. Dump it.
            or "present/" in endpoint
            # 2026-09-11: legend_race/* just shipped and its start UI came
            # back broken. Only legend_race/index was ever captured, so the
            # other eight are dump.cs shapes -- log the real traffic.
            or "legend_race/" in endpoint
            # every never-before-seen unhandled endpoint, once per run
            or (endpoint in _UNHANDLED_SEEN and endpoint not in _UNHANDLED_DUMPED)):
        _UNHANDLED_DUMPED.add(endpoint)
        try:
            import json as _json, os as _os
            _dbg = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "debug_responses")
            _os.makedirs(_dbg, exist_ok=True)
            _boiler = {"device", "device_id", "device_name", "graphics_device_name",
                       "ip_address", "platform_os_version", "carrier", "keychain", "locale",
                       "button_info", "dmm_viewer_id", "dmm_onetime_token", "steam_id",
                       "steam_session_ticket", "viewer_id"}
            _snap = {
                "endpoint": endpoint,
                "request": {k: v for k, v in payload.items() if k not in _boiler},
                "response": response,
            }
            _fn = _os.path.join(_dbg, f"{_request_seq()}_RAW_{endpoint.replace('/', '_')}.json")
            with open(_fn, "w", encoding="utf-8") as _fh:
                _json.dump(_snap, _fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    patched = patch_response(response, payload, sid)
    # tool/signup is the ONE endpoint where the response's identity is NOT
    # the request's: the request carries viewer_id=0 (no account exists
    # yet), but the response mints a brand new one. patch_response's
    # identity-echo (by design, correct for every OTHER endpoint) stamps
    # data_headers.viewer_id from the REQUEST's viewer_id -- so signup's own
    # data_headers.viewer_id came back 0 even though data.viewer_id (the
    # nested field, which patch.py deliberately never echoes into) correctly
    # had the new id. Real-server verified wrong: a real capture's signup
    # response has data_headers.viewer_id == the new id, not 0. Live-
    # reported symptom this caused: the client kept sending viewer_id=0 on
    # every later call (writing progress into a single shared "viewer 0"
    # state bucket instead of the real new account) and re-showed country/
    # ToS/birthday on every subsequent launch, since every response kept
    # telling it "you are still viewer 0."
    if endpoint == "tool/signup":
        new_viewer_id = (patched.get("data") or {}).get("viewer_id")
        if new_viewer_id and isinstance(patched.get("data_headers"), dict):
            patched["data_headers"]["viewer_id"] = new_viewer_id
    return patched


@app.post("/{endpoint_path:path}")
async def dispatch(endpoint_path: str, request: Request):
    """The one branch point between regions -- everything past this either
    calls _dispatch_global or _dispatch_jp, both of which immediately hand
    off to the shared _handle_decoded above. Never add a second one of these
    checks anywhere else; region.py's CURRENT_REGION is what the rest of the
    code (state.py, accounts.py, session.py) reads instead."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host == JP_HOST:
        return await _dispatch_jp(endpoint_path, request)
    return await _dispatch_global(endpoint_path, request)


async def _dispatch_global(endpoint_path: str, request: Request) -> Response:
    raw = await request.body()
    decoded = crypto.decode_request(raw, dict(request.headers))
    with region.use_region("global"):
        patched = _handle_decoded(endpoint_path, decoded.payload, decoded.sid or None)
    body = crypto.encode_response(patched, decoded.udid_raw)
    # VERIFIED 2026-08-11 against a live real-server capture
    # (tools/capture_proxy.py --upstream real): the real server's
    # Content-Type is "application/x-msgpack; charset=utf-8", not
    # "text/plain". Content itself was independently proven byte-correct
    # (a real captured response replayed verbatim through this exact code
    # path still failed client-side -- see docs/LINUX_LOGIN_FIX.md), so this
    # header is the next remaining, evidence-backed difference between what
    # we send and what the real server sends.
    return Response(content=body, media_type="application/x-msgpack; charset=utf-8")


async def _dispatch_jp(endpoint_path: str, request: Request) -> Response:
    """No wire decryption here at all -- by the time this real, still-sealed
    request arrives, jp_bridge already has its plaintext from the Hachimi
    side channel (see jp_bridge.py's module docstring for why that's not a
    shortcut but the only way this is possible). SID is the correlation key;
    it rides in the clear as a normal header on the real request, same as
    Global's."""
    sid_hex = request.headers.get("SID", "")
    pending = jp_bridge.pop_pending(sid_hex)
    if pending is None:
        # No side-channel submission ever arrived for this SID -- the
        # Hachimi plugin isn't loaded, or this request predates it.
        log.warning("JP request with no side-channel submission for sid=%s", sid_hex[:8])
        return Response(status_code=502)

    with region.use_region("jp"):
        patched = _handle_decoded(endpoint_path, pending["payload"], None)

    packed = msgpack.packb(patched, use_bin_type=True)
    wire = jp_crypto.seal_reply(packed, pending["sbox"], pending["sid_raw"], pending["tail16"])
    body = base64.b64encode(wire)
    return Response(content=body, media_type="application/x-msgpack; charset=utf-8")
