"""
Patches session-dependent fields in a fixture-derived response so it matches
the *current* request instead of the captured one (the captures were
sanitized -- viewer_id/sid/device_id/steam_id/etc. show up as the literal
string "<redacted>" -- and sid must reflect the live session regardless).
"""

from __future__ import annotations

import base64
import os
import secrets
import time

from . import accounts, config

# request payload field -> response field(s) it should be echoed into
ECHO_FIELDS = (
    "viewer_id",
    "device_id",
    "device_name",
    "graphics_device_name",
    "ip_address",
    "platform_os_version",
    "steam_id",
)

REDACTED = "<redacted>"

# dump.cs's CoinInfo.fcoin/coin are both a real signed C# int32 (max
# 2,147,483,647), but this server has granted several accounts absurd carat
# amounts (admin cheats, mail gifts) that sit within a couple hundred
# million of that ceiling. Once a balance actually crosses it, every
# response echoing coin_info corrupts client-side on msgpack decode --
# confirmed as the mechanism behind a real "could not receive parameters
# from server" report for a similarly-oversized int (limited_shop_info.
# close_time) and strongly suspected as the cause of an intermittent
# "softlock when pulling" report, since several live accounts' fcoin sat
# right at ~2,000,000,000+ (a mail gift earlier this session put one there
# directly) -- close enough that ordinary play income could tip it over the
# line unpredictably, matching "random". Clamped at the wire boundary only;
# the real stored balance is untouched, so nothing is lost, it just never
# advertises more than a client int32 can hold.
_INT32_MAX = 2_147_483_647
_INT32_FIELDS = frozenset({"fcoin", "coin"})


def _clamp_int32(key, value):
    if isinstance(value, int) and not isinstance(value, bool) and value > _INT32_MAX:
        return _INT32_MAX
    return value

# Every captured fixture's servertime sits in the 2026-07-17 ~19:49-19:51 UTC
# window (operation-day 2026-07-17 JST; daily reset is 05:00 JST). Fixture
# responses keep those frozen values; the ONLY place real (advancing) time
# leaked in was the servertime fallback below (was int(time.time())). That made
# the constant background calls the client fires -- tool/send_log,
# note/save_voice, support_card_deck/change_party, ... -- report *today* while
# fixture responses reported 7/17, so servertime flip-flopped across a day
# boundary every few requests and the client kept popping "Date Changed / It's a
# new day". Pinning the fallback to a value in the SAME frozen operation-day as
# the fixtures makes servertime consistent everywhere -> no spurious new-day.
# It must ALSO be late enough for the content you want visible: the client hides
# anything whose master.mdb start_date is in the future relative to servertime.
# Sitting on the 7/17 capture day hid career scenario 3 entirely (it releases
# 2026-07-22 22:00 UTC) even though the server listed it in
# single_mode_scenario_id_array -- the client filtered it straight back out.
# The default is now inside operation-day 2026-07-23 (daily reset is 05:00 JST =
# 20:00 UTC), which is past that release. Override via client_config.json
# "servertime"; set it back to 1784317775 if an "It's a new day" loop appears.
FROZEN_SERVERTIME = 1784808000  # 2026-07-23 12:00 UTC


def _servertime() -> int:
    """The servertime to report, honouring the client_config override.

    `"servertime": "now"` serves REAL current time, which is what the actual
    Cygames server does -- verified 2026-08-11 by capturing a real successful
    login (tools/capture_proxy.py --upstream real): its data_headers.servertime
    was the true epoch, to the second.

    That matters because the frozen default can drift into the PAST relative to
    the client's own records. On the Linux/Proton box the frozen value sat at
    2026-07-23 while the client's SaveData.db had
    information_last_open_time = 2026-08-11 -- i.e. the server claimed time ran
    backwards 19 days, the same class of session inconsistency README.md's
    login-loop postmortem blames for breaking the client. Freezing is still the
    right default for reproducible fixture replay; "now" is right when you want
    to behave like the real server.
    """
    raw = config.get("servertime")
    if isinstance(raw, str) and raw.strip().lower() == "now":
        return int(time.time())
    return config.get_int("servertime", FROZEN_SERVERTIME)

# Fields the client base64-decodes (see uma_api/client.py's
# `base64.b64decode(d["auth_key"]).hex()`) -- these need a placeholder that's
# actually valid base64, not just an opaque token.
BASE64_FIELDS = {"auth_key"}


def make_sid() -> str:
    """A session id shaped like the real server's.

    Verified 2026-08-11 from a real captured login: the real
    data_headers.sid is 42 hex chars (21 bytes), e.g.
    "84f7cc46b15516e79a97dd36b302d06d442464843f" -- ours was 32 (16 bytes).
    The client hashes whatever it gets (uma_api/client.py next_sid = md5 over
    the string + salt), so length is not obviously load-bearing, but matching
    the real wire shape removes one more difference for free.
    """
    return secrets.token_hex(21)


def _placeholder_for(key: str, real_viewer_id) -> object:
    if key in BASE64_FIELDS:
        return base64.b64encode(os.urandom(16)).decode()
    if key in ("viewer_id", "owner_viewer_id"):
        # These are msgpack-typed as integers client-side -- a hex-string
        # placeholder here isn't just semantically wrong, it's a type
        # mismatch that breaks deserialization outright (this was the actual
        # cause of the client's generic "Could not receive parameters from
        # server" error). Stamping the real requesting viewer_id everywhere
        # is not fully accurate for fields that name some *other* user (a
        # friend's card lender, etc.) but it's numerically valid, which is
        # what actually matters for the client not to choke.
        return real_viewer_id if real_viewer_id is not None else secrets.randbelow(900_000_000_000) + 100_000_000_000
    return secrets.token_hex(16)


def patch_response(response: dict, request_payload: dict, sid: str) -> dict:
    overrides = {k: request_payload[k] for k in ECHO_FIELDS if k in request_payload}
    overrides["sid"] = sid
    # Deliberately NOT overriding servertime to the real current time: fixture
    # responses are riddled with other timestamps frozen at capture time
    # (daily-reset hour, last-login/update_time, campaign windows, ...) that
    # we have no general way to shift forward. A servertime that races ahead
    # of those makes the client conclude a day boundary was crossed, which
    # triggers its "new day" flow and — since the *next* fixture replay has
    # the exact same frozen timestamps — loops forever. Letting servertime
    # stay frozen at the fixture's own captured value keeps everything
    # internally consistent instead.

    real_viewer_id = request_payload.get("viewer_id")

    def scrub_redacted(node):
        # "<redacted>" placeholders can turn up anywhere in the tree (the
        # captures were sanitized before being written to disk), so this
        # pass is unconditionally recursive.
        if isinstance(node, dict):
            return {
                k: (_placeholder_for(k, real_viewer_id) if v == REDACTED
                    else _clamp_int32(k, v) if k in _INT32_FIELDS else scrub_redacted(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [scrub_redacted(v) for v in node]
        return node

    response = scrub_redacted(response)

    # Session-identity fields are only echoed within data_headers, never
    # walked into the rest of the tree -- a blanket walk would clobber
    # legitimately-generated identity fields elsewhere (e.g. tool/signup's
    # freshly-minted data.viewer_id, which must NOT be overwritten with
    # whatever viewer_id the pre-signup request sent, typically 0/empty).
    data_headers = response.get("data_headers")
    if isinstance(data_headers, dict):
        for k, v in overrides.items():
            if k in data_headers:
                data_headers[k] = v
        # viewer_id and sid are load-bearing on every single response (the
        # client's session/sid-rolling logic reads them unconditionally) --
        # a response missing them isn't just semantically incomplete, it's
        # malformed enough to derail the client. Handlers with nothing else
        # to say here (the NOOP_SUCCESS fallback, mainly) still need these
        # set, so add-if-missing rather than only overwrite-if-present.
        data_headers.setdefault("viewer_id", overrides.get("viewer_id", real_viewer_id))
        data_headers.setdefault("sid", sid)
        # servertime is FORCED to one frozen value on EVERY response. We now
        # mix capture sources from different days (the 7/17 load/index seed vs
        # the 7/20 URA single_mode/* captures), so keeping each fixture's own
        # frozen servertime made it flip 7/17<->7/20 across requests and the
        # client popped "It's a new day" on reaching the (7/20) training screen.
        # It must stay on the load/index reset day (7/17) -- setting it ahead
        # would make the client think the daily reset was missed. Overriding all
        # to FROZEN_SERVERTIME keeps every response on the same operation-day.
        data_headers["servertime"] = _servertime()

        # account_deletion_cancellation_period (dump.cs: DataHeader, the same
        # class backing viewer_id/sid/servertime/notifications on EVERY
        # response) -- the real game surfaces a pending account/deletion_
        # request globally through the envelope rather than a dedicated poll,
        # so a player sees "N days left to cancel" no matter what screen
        # they're on. Absent (not just empty-string) when nothing is pending,
        # matching how every other optional envelope field here behaves.
        if real_viewer_id is not None:
            days_left = accounts.deletion_days_remaining(real_viewer_id)
            if days_left is not None:
                data_headers["account_deletion_cancellation_period"] = str(days_left)

    return response
