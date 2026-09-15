"""payment/* -- the carat store, made to work WITHOUT a real transaction.

WHAT THE REAL FLOW LOOKS LIKE (captures/20260825_094909, a purchase the
player opened and then cancelled -- the only payment capture in the corpus):

    payment/item_list           -> the 20-product catalog (below)
    payment/start               -> {} , result_code 1
    payment/steam_micro_txn_init-> {} , result_code 1
    ... Steam's own overlay ...
    payment/cancel              -> {} , result_code 1

All three of start/init/cancel answer with NO `data` key at all, not an
empty object -- that is reproduced here exactly.

WHY THE STEAM STEP IS THE PROBLEM. On this build the money leg is Steam's:
after payment/start the client calls payment/steam_micro_txn_init and then
waits on Steam's own purchase overlay for an order this server never placed
with Steam and cannot place. Left alone, the flow either opens a real
purchase box or hangs waiting for a callback that never comes. The server
cannot drive the client past that on its own.

So the store is wired in two modes, `payment_mode` in client_config.json:

  "instant" (default) -- payment/start credits the carats itself and then
      answers PAYMENT_ALREADY_ERROR (301, GallopResultCode in dump.cs:
      "this purchase is already done"). The client stops there: it never
      reaches steam_micro_txn_init, so no purchase box opens, and the carats
      are on the account from that moment. The cost is a one-off error
      dialog where a "thanks for your purchase" popup would be, and the
      client's cached wallet only catches up on the next load/index.

  "receipt" -- start/init/cancel behave exactly like the real captures and
      the carats are granted by payment/finish or payment/dummy instead.
      This is the honest shape, and the right mode if a client build is ever
      driven down its dummy-payment path; on a stock Steam client it means
      the Steam overlay still appears.

  "off" -- catalog only; nothing is ever credited.

NOTHING HERE TOUCHES REAL MONEY, in any mode. There is no Steam order, no
receipt validation and no external call: "buying" is a local carat grant
against a hand-written catalog, which is the only thing a private server
could do anyway.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("uma-server")

from .. import config
from .. import state as state_store
from . import registry, shop

# Per-viewer purchase counts: {store_product_id: times bought}. Feeds the
# catalog's number_of_product_purchased, which is what enforces limit_num on
# the client's own store screen.
PURCHASE_STATE_KEY = "payment_purchase_state"

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "seeds" / "payment_item_list.json"
_catalog_cache: dict | None = None

# GallopResultCode.PAYMENT_ALREADY_ERROR -- "this purchase has already been
# completed". The one payment-family refusal that means the transaction
# SUCCEEDED elsewhere, which is exactly true in "instant" mode.
_PAYMENT_ALREADY_ERROR = 301


def _ok(data=None) -> dict:
    """A success envelope. `data` omitted entirely when None -- payment/start,
    steam_micro_txn_init and cancel all answer with no data key at all in the
    real capture, and matching that is free."""
    env = {"response_code": 1,
           "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        env["data"] = data
    return env


def _refuse(result_code: int = 205) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": result_code, "notifications": {}},
            "data": {}}


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
    """Credit one purchase of `product` and record it against the limit.

    charge_num is PAID carats (CoinInfo.coin) and free_num is bonus/FREE
    carats (CoinInfo.fcoin) -- the client shows those as two separate
    numbers, so a purchase must not fold them together (see shop.py's own
    note on the same distinction)."""
    charge = int(product.get("charge_num") or 0)
    free = int(product.get("free_num") or 0)
    if charge:
        shop.grant_carats(full_state, charge, paid=True)
    if free:
        shop.grant_carats(full_state, free, paid=False)
    if product.get("item_pack_id"):
        # Bundled packs (item_pack_id 1002/1003/9000..) carry items beyond
        # carats, and this master.mdb has no table describing their contents
        # -- the carats are granted, the rest is not, and saying so in the
        # log beats silently shorting the pack.
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
    """limit_num 0 means unlimited; anything else caps lifetime purchases."""
    limit = int(product.get("limit_num") or 0)
    if limit <= 0:
        return False
    bought = int(_purchases(full_state).get(str(product.get("store_product_id"))) or 0)
    return bought >= limit


# ------------------------------------------------------------ catalog --
@registry.endpoint("payment/item_list")
def handle_item_list(payload: dict) -> dict:
    """The store shelf: {data: PaymentPurchaseItemParam[], season_pack_info,
    last_checked_time}.

    The 20 products are the real captured catalog verbatim (prices, carat
    amounts, limits, display order) EXCEPT number_of_product_purchased, which
    is that one capture owner's own history and is refilled per viewer here --
    otherwise every fresh account would log in already having bought the
    limited packs."""
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
    """The "where did my paid carats come from" breakdown. Real purchases
    would each be a row here; a private server has no payment records to
    show, and an empty array is a correct answer rather than a stub."""
    return _ok({"coin_break_down_info_array": []})


# ------------------------------------------------------------ purchase --
@registry.endpoint("payment/start")
def handle_start(payload: dict) -> dict:
    """PaymentStartRequest {payment: {product_id, price, currency_code,
    error_message}, isalertagree, isalertactive}.

    In "instant" mode this is the whole purchase: grant, then answer 301 so
    the client never walks on to the Steam leg. In every other mode it is the
    real capture's bare success and the money leg proceeds as normal."""
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
    # Credited above; 301 is what stops the flow before steam_micro_txn_init.
    return _refuse(_PAYMENT_ALREADY_ERROR)


def _finish(payload: dict) -> dict:
    """The body shared by payment/finish and payment/dummy -- both answer
    PaymentFinishResponse.CommonResponse and differ only in which one the
    client's build happens to call."""
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
    """The client's own no-real-store purchase path. Whether a stock build
    ever calls it is a client-side question this server cannot answer -- but
    if one does, it lands here and works."""
    return _finish(payload)


# ------------------------------------------------------- housekeeping --
@registry.endpoint("payment/steam_micro_txn_init")
def handle_steam_micro_txn_init(payload: dict) -> dict:
    """Only reached in "receipt" mode (in "instant" the 301 above stops the
    flow first). There is no Steam order to initialise, so this is the
    capture's bare success and nothing more -- the overlay that follows is
    Steam's, not ours."""
    return _ok()


@registry.endpoint("payment/cancel")
def handle_cancel(payload: dict) -> dict:
    """Player backed out of the store dialog. Nothing was ever charged, so
    there is nothing to roll back -- capture-confirmed bare success."""
    return _ok()


@registry.endpoint("payment/send_log")
def handle_send_log(payload: dict) -> dict:
    """The client reporting a store//receipt error to the operator. Recorded
    in our own log so a failed purchase attempt leaves a trace here too."""
    log.info("payment/send_log from viewer %s: key=%s %s",
             payload.get("viewer_id"), payload.get("log_key"),
             payload.get("log_message"))
    return _ok()


# load/index carries the age gate as a PAIR, and the corpus pins both halves
# and the relationship between them: `optin_user_birth` is an int YYYYMM (the
# value the confirmation dialog is prefilled with, and the one the client posts
# back here) and `user_birth` is a string YYYYMMDD (null until confirmed). The
# one account in the corpus that has confirmed shows optin 200012 alongside
# user_birth "20001201" -- month + "01", the client never asking for a day.
USER_BIRTH_KEY = "user_birth_state"          # {"user_birth": str, "optin_user_birth": int}


def user_birth(full_state: dict) -> dict | None:
    """The confirmed age-gate pair, or None if this account never confirmed."""
    stored = full_state.get(USER_BIRTH_KEY)
    return stored if isinstance(stored, dict) and stored.get("user_birth") else None


@registry.endpoint("payment/update_birth")
def handle_update_birth(payload: dict) -> dict:
    """PaymentUpdateBirthRequest {user_birth: int YYYYMM} ->
    {user_birth: string YYYYMMDD}.

    The store's age confirmation. Nothing here enforces a spending limit --
    the point is purely that the answer STICKS: the response field and
    load/index's own user_birth are what tell the client it has been asked
    already, and while they stayed null the dialog reappeared on every visit
    to the store no matter how many times it was confirmed."""
    viewer_id = payload["viewer_id"]
    raw = payload.get("user_birth")
    try:
        optin = int(raw)
    except (TypeError, ValueError):
        log.warning("payment/update_birth: viewer %s sent an unusable user_birth %r",
                    viewer_id, raw)
        return _refuse()

    # YYYYMM -> "YYYYMMDD". A value that is already a full date is left alone,
    # so a client that ever sends one is not silently corrupted.
    text = str(optin)
    record = {"user_birth": text if len(text) >= 8 else text + "01",
              "optin_user_birth": optin}

    full_state = state_store.get_state(viewer_id) or {}
    full_state[USER_BIRTH_KEY] = record
    state_store.save_state(viewer_id, full_state)
    log.info("payment/update_birth: viewer %s confirmed age -> %s",
             viewer_id, record["user_birth"])
    return _ok({"user_birth": record["user_birth"]})
