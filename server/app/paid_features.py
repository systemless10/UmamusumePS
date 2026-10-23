
from __future__ import annotations

import time

STATE_KEY = "paid_features"
FEATURES = ("umasuku", "umaplan", "reroll_pass", "order_pass")

REROLL_TICKET_ITEM = 260
ORDER_TICKET_ITEM = 262

_SUBSCRIPTIONS = (("umasuku", 0, "jp.co.cygames.umamusume.subs001"),
                  ("umaplan", 1, "jp.co.cygames.umamusume.subs004"))


def end_time(full_state: dict, feature: str) -> int:
    held = (full_state or {}).get(STATE_KEY)
    return int((held or {}).get(feature) or 0) if isinstance(held, dict) else 0


def active(full_state: dict, feature: str) -> bool:
    return end_time(full_state, feature) > time.time()


def grant(full_state: dict, feature: str, days: int) -> int:
    if feature not in FEATURES:
        raise ValueError(f"unknown paid feature {feature!r} (one of {', '.join(FEATURES)})")
    held = full_state.setdefault(STATE_KEY, {})
    base = max(int(time.time()), end_time(full_state, feature))
    held[feature] = base + int(days) * 86400
    return held[feature]


def revoke(full_state: dict, feature: str) -> None:
    (full_state.get(STATE_KEY) or {}).pop(feature, None)


def item_count(full_state: dict, item_id: int) -> int:
    for it in full_state.get("item_list_state") or ():
        if it.get("item_id") == item_id:
            return int(it.get("number") or 0)
    return 0


def spend_item(full_state: dict, item_id: int, num: int = 1) -> bool:
    for it in full_state.get("item_list_state") or ():
        if it.get("item_id") == item_id and int(it.get("number") or 0) >= num:
            it["number"] = int(it["number"]) - num
            return True
    return False


def spend_coin(full_state: dict, cost: int) -> bool:
    wallet = full_state.setdefault("coin_info_state", {"fcoin": 0, "coin": 0})
    fcoin, coin = int(wallet.get("fcoin") or 0), int(wallet.get("coin") or 0)
    if fcoin + coin < cost:
        return False
    free = min(fcoin, cost)
    wallet["fcoin"], wallet["coin"] = fcoin - free, coin - (cost - free)
    return True


def _pass_info(full_state: dict, feature: str) -> dict:
    return {"pass_end_time": end_time(full_state, feature), "repurchase_time": 0,
            "notice_status": 0}


def _purchase_info(full_state: dict, feature: str) -> list:
    end = end_time(full_state, feature)
    if end <= time.time():
        return []
    return [{"product_type": 1, "purchase_time": int(time.time()),
             "pass_start_time": int(time.time()), "pass_end_time": end}]


def _subscription(full_state: dict, feature: str, sub_type: int, product: str) -> dict:
    valid = active(full_state, feature)
    return {"product_id": product if valid else "", "subscription_type": sub_type,
            "is_valid": valid, "order_id": "", "subscription_end_date":
            end_time(full_state, feature) if valid else 0,
            "is_in_grace_period": False, "is_in_account_hold": False}


def stamp(full_state: dict, data: dict) -> None:
    if not isinstance(data, dict):
        return
    for key in ("single_mode_start_common", "single_mode_load_common"):
        common = data.get(key)
        if isinstance(common, dict):
            common["is_umaplan"] = active(full_state, "umaplan")
    if "factor_premium_pass_info" in data:
        data["factor_premium_pass_info"] = _pass_info(full_state, "reroll_pass")
    if "factor_order_pass_info" in data:
        data["factor_order_pass_info"] = _pass_info(full_state, "order_pass")
    if "factor_premium_pass_purchase_info_array" in data:
        data["factor_premium_pass_purchase_info_array"] = _purchase_info(full_state, "reroll_pass")
    if "factor_order_pass_purchase_info_array" in data:
        data["factor_order_pass_purchase_info_array"] = _purchase_info(full_state, "order_pass")
    subs = [_subscription(full_state, f, t, p) for f, t, p in _SUBSCRIPTIONS]
    if "user_subscription_info_array" in data:
        data["user_subscription_info_array"] = subs
    if "user_subscription_info" in data:
        data["user_subscription_info"] = subs[0]
    if "subscription_is_valid" in data:
        data["subscription_is_valid"] = active(full_state, "umaplan")
