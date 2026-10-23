
from __future__ import annotations

from ... import paid_features
from ... import state as state_store

HANDLERS: dict = {}

_FIELDS = ("factor_premium_pass_info", "factor_order_pass_info",
           "user_subscription_info_array", "subscription_is_valid",
           "single_mode_start_common", "single_mode_load_common")


def after(endpoint: str, payload: dict, resp: dict) -> None:
    data = resp.get("data")
    if not isinstance(data, dict) or not any(k in data for k in _FIELDS):
        return
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    paid_features.stamp(full_state, data)
