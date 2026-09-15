"""
Per-viewer dynamic account data, served from state instead of the frozen
load/index fixture -- the foundation for a functional private server (shop,
uncapping, hint/star raising, etc. all read/write this state).

Each container below is seeded ONCE from the cached load/index snapshot into
per-viewer state, then served from there so it persists and can be
injected/modified. Only load/index carries these (verified against every
captured endpoint), so load._overlay_live_data is the single serving point.
Seeding mirrors trained_chara._get_or_seed_roster.

Containers (load/index data-key -> per-viewer state key):
  - card_list         : trainable uma cards you own (talent_level = potential,
                        skill_data_array = skill-HINT levels)
  - support_card_list : owned support cards (limit_break_count = UNCAP, + exp)
  - chara_list        : character meta (fans, affinity, dress)
  - coin_info         : money ({fcoin, coin})
  - tp_info / rp_info : training / race points
  - item_list         : all items -- consumables, racing shoes, upgrade items,
                        tickets, carrots, ... ({item_id, number})
  - piece_list        : character/card copies for star-up / uncap
                        ({piece_id (=card_id), piece_num})
  - cloth_list        : cosmetic sashes/outfits
  - music_list        : unlocked music

The maxed inject support card 30010 lives in support_card_collection.
"""

from __future__ import annotations

import copy

from .. import state as state_store

CARD_LIST_KEY = "card_collection"
SUPPORT_CARD_KEY = "support_card_collection"
CHARA_LIST_KEY = "chara_collection"
SUPPORT_DECK_KEY = "support_card_deck_state"

# load/index data-key -> per-viewer state key
DYNAMIC_CONTAINERS = {
    "card_list": CARD_LIST_KEY,
    "support_card_list": SUPPORT_CARD_KEY,
    "chara_list": CHARA_LIST_KEY,
    "coin_info": "coin_info_state",       # money: {fcoin, coin}
    "tp_info": "tp_info_state",           # training points
    "rp_info": "rp_info_state",           # race points
    "item_list": "item_list_state",       # consumables / shoes / upgrade items / tickets / carrots
    "piece_list": "piece_list_state",     # character/card copies (star-up / uncap)
    "cloth_list": "cloth_list_state",     # cosmetic sashes/outfits
    "music_list": "music_list_state",     # unlocked music
    "support_card_deck_array": SUPPORT_DECK_KEY,  # the 10 saved support decks
}


def _get_or_seed(viewer_id, state_key: str, seed_value):
    """First access copies the cached snapshot into state; later accesses
    return the stored (authoritative, editable) copy. Works for both list and
    dict containers."""
    full_state = state_store.get_state(viewer_id) or {}
    stored = full_state.get(state_key)
    if stored is not None:
        return stored
    seeded = copy.deepcopy(seed_value)
    full_state[state_key] = seeded
    state_store.save_state(viewer_id, full_state)
    return seeded


def get_or_seed_many(viewer_id, seeds: dict) -> dict:
    """Several load/index containers at once, reading the viewer's state ONCE.

    Same semantics as calling get_or_seed per key -- first access copies the
    snapshot into state, later accesses return the stored copy -- but load/index
    seeds all 11 DYNAMIC_CONTAINERS on every single call, and going through
    get_or_seed for each meant 11 full get_state round trips (each parsing the
    account's entire state) plus up to 11 separate save_state calls to build one
    response. This does one read and, at most, one write.
    """
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
    """Seed the container for `data_key` (a load/index field) from `seed_value`
    on first access, then return the stored copy."""
    return _get_or_seed(viewer_id, DYNAMIC_CONTAINERS[data_key], seed_value)


# convenience accessors (used by injection / admin scripts)
def get_card_list(viewer_id, seed_value):
    return get_or_seed(viewer_id, "card_list", seed_value)


def get_support_cards(viewer_id, seed_value):
    return get_or_seed(viewer_id, "support_card_list", seed_value)


def get_chara_list(viewer_id, seed_value):
    return get_or_seed(viewer_id, "chara_list", seed_value)


def _deck_seed():
    """The frozen 10-deck support_card_deck_array from the load/index seed --
    used only if change_party fires before load/index seeded the deck state."""
    from .load import _load_seed  # lazy: load.py imports this module at top level
    return _load_seed().get("support_card_deck_array", [])


def handle_change_party(payload: dict) -> dict:
    """support_card_deck/change_party: the client saves a deck's support-card
    lineup (UserSupportCardDeckForUpdateParty[] -> support_card_deck_array). The
    response MUST echo the full deck set as UserSupportCardDeck[] ({deck_id,
    name, support_card_id_array}); an empty/no-op body makes the client NullRef
    in WorkSupportDeckData.Update and freeze the UI. We persist the edit so the
    deck stays changed (served back via load/index)."""
    viewer_id = payload["viewer_id"]
    # ensure the deck state is seeded, then re-read it for mutation
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
        else:  # unknown deck id -> materialize it so the response is complete
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
