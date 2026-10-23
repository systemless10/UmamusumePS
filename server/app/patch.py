
from __future__ import annotations

import base64
import os
import secrets
import time

from . import accounts, config

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

_INT32_MAX = 2_147_483_647
_INT32_FIELDS = frozenset({"fcoin", "coin"})


def _clamp_int32(key, value):
    if isinstance(value, int) and not isinstance(value, bool) and value > _INT32_MAX:
        return _INT32_MAX
    return value

FROZEN_SERVERTIME = 1784808000


def _servertime() -> int:
    raw = config.get("servertime")
    if isinstance(raw, str) and raw.strip().lower() == "now":
        from . import champions_meeting
        cm = champions_meeting.served_time()
        return cm if cm is not None else int(time.time())
    return config.get_int("servertime", FROZEN_SERVERTIME)

BASE64_FIELDS = {"auth_key"}


def make_sid() -> str:
    return secrets.token_hex(21)


def _placeholder_for(key: str, real_viewer_id) -> object:
    if key in BASE64_FIELDS:
        return base64.b64encode(os.urandom(16)).decode()
    if key in ("viewer_id", "owner_viewer_id"):
        return real_viewer_id if real_viewer_id is not None else secrets.randbelow(900_000_000_000) + 100_000_000_000
    return secrets.token_hex(16)


def patch_response(response: dict, request_payload: dict, sid: str) -> dict:
    overrides = {k: request_payload[k] for k in ECHO_FIELDS if k in request_payload}
    overrides["sid"] = sid

    real_viewer_id = request_payload.get("viewer_id")

    def scrub_redacted(node):
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

    data_headers = response.get("data_headers")
    if isinstance(data_headers, dict):
        for k, v in overrides.items():
            if k in data_headers:
                data_headers[k] = v
        data_headers.setdefault("viewer_id", overrides.get("viewer_id", real_viewer_id))
        data_headers.setdefault("sid", sid)
        data_headers["servertime"] = _servertime()

        if real_viewer_id is not None:
            days_left = accounts.deletion_days_remaining(real_viewer_id)
            if days_left is not None:
                data_headers["account_deletion_cancellation_period"] = str(days_left)

    return response
