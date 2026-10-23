
from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("uma-server")

from .. import config
from .. import region
from .. import state as state_store
from . import registry, shop
from ..result_codes import ResultCode, refuse

PURCHASE_STATE_KEY = "payment_purchase_state"

_CATALOG_PATH = region.seeds_dir() / "payment_item_list.json"
_catalog_cache: dict | None = None

_PAYMENT_ALREADY_ERROR = ResultCode.PAYMENT_ALREADY_ERROR


def _ok(data=None) -> dict:
    env = {"response_code": 1,
           "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        env["data"] = data
    return env


def _refuse(result_code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(result_code)


def _mode() -> str:
    return str(config.get("payment_mode", "instant")).lower()


def _catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            _catalog_cache = json.load(fh)
    return copy.deepcopy(_catalog_cache)


def _purchases(full_state: dict) -> dict:
    return full_state.setdefault(PURCHASE_STATE_KEY, {})


def _product(store_product_id: str) -> dict | None:
    for item in _catalog().get("data") or []:
        if item.get("store_product_id") == store_product_id:
            return item
    return None


def _grant(viewer_id, full_state: dict, product: dict) -> dict:
    charge = int(product.get("charge_num") or 0)
    free = int(product.get("free_num") or 0)
    if charge:
        shop.grant_carats(full_state, charge, paid=True)
    if free:
        shop.grant_carats(full_state, free, paid=False)
    if product.get("item_pack_id"):
        log.warning("payment: product %s carries item_pack_id %s, whose contents "
                    "are not in master.mdb -- granted carats only",
                    product.get("store_product_id"), product.get("item_pack_id"))
    counts = _purchases(full_state)
    key = str(product.get("store_product_id"))
    counts[key] = int(counts.get(key) or 0) + 1
    log.info("payment: viewer %s granted %s paid + %s free carats for %s (purchase #%s)",
             viewer_id, charge, free, key, counts[key])
    return {"charge": charge, "free": free, "times": counts[key]}


def _limit_reached(full_state: dict, product: dict) -> bool:
    limit = int(product.get("limit_num") or 0)
    if limit <= 0:
        return False
    bought = int(_purchases(full_state).get(str(product.get("store_product_id"))) or 0)
    return bought >= limit


@registry.endpoint("payment/item_list")
def handle_item_list(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    counts = _purchases(full_state)
    catalog = _catalog()
    for item in catalog.get("data") or []:
        item["number_of_product_purchased"] = int(
            counts.get(str(item.get("store_product_id"))) or 0)
    catalog["last_checked_time"] = int(time.time())
    return _ok(catalog)


@registry.endpoint("payment/get_coin_break_down_info")
def handle_get_coin_break_down_info(payload: dict) -> dict:
    return _ok({"coin_break_down_info_array": []})


@registry.endpoint("payment/start")
def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    product_id = ((payload.get("payment") or {}).get("product_id") or "")
    mode = _mode()
    if mode != "instant":
        return _ok()

    product = _product(product_id)
    if not product:
        log.warning("payment/start: viewer %s asked for unknown product %r",
                    viewer_id, product_id)
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if _limit_reached(full_state, product):
        log.info("payment/start refused: viewer %s is at product %s's limit of %s",
                 viewer_id, product_id, product.get("limit_num"))
        return _refuse(_PAYMENT_ALREADY_ERROR)
    _grant(viewer_id, full_state, product)
    state_store.save_state(viewer_id, full_state)
    return _refuse(_PAYMENT_ALREADY_ERROR)


def _finish(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    pay = payload.get("payment") or {}
    product_id = pay.get("product_id") or ""
    full_state = state_store.get_state(viewer_id) or {}

    wallet = full_state.get("coin_info_state") or {}
    before_paid = int(wallet.get("coin") or 0)
    before_free = int(wallet.get("fcoin") or 0)

    product = _product(product_id)
    if _mode() == "off" or not product:
        if not product:
            log.warning("payment finish: viewer %s sent unknown product %r",
                        viewer_id, product_id)
        granted = {"times": 0}
    else:
        granted = _grant(viewer_id, full_state, product)
        state_store.save_state(viewer_id, full_state)

    wallet = full_state.get("coin_info_state") or {}
    return _ok({
        "first_time": granted.get("times", 0) == 1,
        "purchased_times_data": {
            "product_id": product_id,
            "number_of_product_purchased": granted.get("times", 0),
            "limit_of_product_purchased": int((product or {}).get("limit_num") or 0),
        },
        "purchase_id": pay.get("purchase_id") or "",
        "before_paid_coin": before_paid,
        "before_free_coin": before_free,
        "after_paid_coin": int(wallet.get("coin") or 0),
        "after_free_coin": int(wallet.get("fcoin") or 0),
        "season_pack_info": _catalog().get("season_pack_info"),
    })


@registry.endpoint("payment/finish")
def handle_finish(payload: dict) -> dict:
    return _finish(payload)


@registry.endpoint("payment/dummy")
def handle_dummy(payload: dict) -> dict:
    return _finish(payload)


@registry.endpoint("payment/steam_micro_txn_init")
def handle_steam_micro_txn_init(payload: dict) -> dict:
    return _ok()


@registry.endpoint("payment/cancel")
def handle_cancel(payload: dict) -> dict:
    return _ok()


@registry.endpoint("payment/send_log")
def handle_send_log(payload: dict) -> dict:
    log.info("payment/send_log from viewer %s: key=%s %s",
             payload.get("viewer_id"), payload.get("log_key"),
             payload.get("log_message"))
    return _ok()


USER_BIRTH_KEY = "user_birth_state"


def user_birth(full_state: dict) -> dict | None:
    stored = full_state.get(USER_BIRTH_KEY)
    return stored if isinstance(stored, dict) and stored.get("user_birth") else None


@registry.endpoint("payment/update_birth")
def handle_update_birth(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    raw = payload.get("user_birth")
    try:
        optin = int(raw)
    except (TypeError, ValueError):
        log.warning("payment/update_birth: viewer %s sent an unusable user_birth %r",
                    viewer_id, raw)
        return _refuse()

    text = str(optin)
    record = {"user_birth": text if len(text) >= 8 else text + "01",
              "optin_user_birth": optin}

    full_state = state_store.get_state(viewer_id) or {}
    full_state[USER_BIRTH_KEY] = record
    state_store.save_state(viewer_id, full_state)
    log.info("payment/update_birth: viewer %s confirmed age -> %s",
             viewer_id, record["user_birth"])
    return _ok({"user_birth": record["user_birth"]})
