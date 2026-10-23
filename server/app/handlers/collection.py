
from __future__ import annotations

import copy

from .. import state as state_store

CARD_LIST_KEY = "card_collection"
SUPPORT_CARD_KEY = "support_card_collection"
CHARA_LIST_KEY = "chara_collection"
SUPPORT_DECK_KEY = "support_card_deck_state"

DYNAMIC_CONTAINERS = {
    "card_list": CARD_LIST_KEY,
    "support_card_list": SUPPORT_CARD_KEY,
    "chara_list": CHARA_LIST_KEY,
    "coin_info": "coin_info_state",
    "tp_info": "tp_info_state",
    "rp_info": "rp_info_state",
    "item_list": "item_list_state",
    "piece_list": "piece_list_state",
    "cloth_list": "cloth_list_state",
    "music_list": "music_list_state",
    "support_card_deck_array": SUPPORT_DECK_KEY,
}


def echo_support_card(summary: dict, card: dict) -> None:
    lst = summary["add_support_card_list"]
    lst[:] = [e for e in lst if e.get("support_card_id") != card.get("support_card_id")]
    lst.append(copy.deepcopy(card))


def _get_or_seed(viewer_id, state_key: str, seed_value):
    full_state = state_store.get_state(viewer_id) or {}
    stored = full_state.get(state_key)
    if stored is not None:
        return stored
    seeded = copy.deepcopy(seed_value)
    full_state[state_key] = seeded
    state_store.save_state(viewer_id, full_state)
    return seeded


def get_or_seed_many(viewer_id, seeds: dict) -> dict:
    full_state = state_store.get_state(viewer_id) or {}
    out, seeded_any = {}, False
    for data_key, seed_value in seeds.items():
        state_key = DYNAMIC_CONTAINERS[data_key]
        stored = full_state.get(state_key)
        if stored is None:
            stored = copy.deepcopy(seed_value)
            full_state[state_key] = stored
            seeded_any = True
        out[data_key] = stored
    if seeded_any:
        state_store.save_state(viewer_id, full_state)
    return out


def get_or_seed(viewer_id, data_key: str, seed_value):
    return _get_or_seed(viewer_id, DYNAMIC_CONTAINERS[data_key], seed_value)


def get_card_list(viewer_id, seed_value):
    return get_or_seed(viewer_id, "card_list", seed_value)


def get_support_cards(viewer_id, seed_value):
    return get_or_seed(viewer_id, "support_card_list", seed_value)


def get_chara_list(viewer_id, seed_value):
    return get_or_seed(viewer_id, "chara_list", seed_value)


def _deck_seed():
    from .load import _load_seed
    return _load_seed().get("support_card_deck_array", [])


def handle_change_party(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    get_or_seed(viewer_id, "support_card_deck_array", _deck_seed())
    full_state = state_store.get_state(viewer_id) or {}
    decks = full_state.get(SUPPORT_DECK_KEY) or []

    by_id = {d.get("deck_id"): d for d in decks}
    for upd in payload.get("support_card_deck_array", []):
        did = upd.get("deck_id")
        if did is None:
            continue
        cards = upd.get("support_card_id_array", [])
        if did in by_id:
            by_id[did]["support_card_id_array"] = cards
        else:
            new_deck = {"deck_id": did, "name": f"Deck {did}", "support_card_id_array": cards}
            decks.append(new_deck)
            by_id[did] = new_deck

    full_state[SUPPORT_DECK_KEY] = decks
    state_store.save_state(viewer_id, full_state)

    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {"support_card_deck_array": copy.deepcopy(decks)},
    }
