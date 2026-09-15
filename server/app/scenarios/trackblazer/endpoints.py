"""TRACKBLAZER (scenario 4) -- the two endpoints URA has no counterpart for.

    single_mode_free/multi_item_exchange   buy, BY SHOP SLOT
        {"exchange_item_info_array": [{"shop_item_id": 5, "current_num": 0}],
         "current_turn": 13}
    single_mode_free/multi_item_use        use, BY ITEM ID
        {"use_item_info_array": [{"item_id": 3101, "use_num": 1, "current_num": 1}],
         "current_turn": 13}

Both take an ARRAY: the client batches a whole shopping trip into one call (the
capture's first purchase buys four slots at once). Note the two different keys
-- buying names the slot because the same item can be offered twice in one
lineup at different prices, using names the item because inventory is by item.

Every other endpoint Trackblazer posts to reuses the shared career handlers
verbatim; main.py aliases them onto this scenario's endpoint_prefix. The
free_data_set both responses carry is NOT built here -- it is attached by the
same chokepoint that serves it on every other response
(single_mode_team._attach_scenario_data_set), which is why these handlers only
have to move state and hand back a training-state envelope.

Observed response shapes: exchange returns {chara_info, free_data_set}; use
returns {chara_info, home_info, free_data_set}, because a used item can change
what the training screen shows (a stat charm moves the caps, a megaphone moves
every gain) and the client redraws from that same response.
"""

from __future__ import annotations

import copy
import logging

from . import impl, shop

log = logging.getLogger("uma-server")

_REFUSE = {"response_code": 1,
           "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _smt():
    """handlers.single_mode_team, imported on use -- a top-level import would
    close the cycle through the scenario registry."""
    from ...handlers import single_mode_team
    return single_mode_team


def _envelope(career: dict, keys) -> dict:
    """A training-state response carrying only `keys`. free_data_set is added
    later by the response chokepoint, the same as on every other response."""
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    chara_info = career["data"]["chara_info"]
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    for key in list(data):
        if key not in keys:
            data.pop(key, None)
    return response


def _refresh_previews(full_state: dict, career: dict, chara_info: dict) -> None:
    """Rebuild the training screen from the state the items just changed.

    An item's whole point is visible on the buttons -- a megaphone's +40%, an
    ankle weight's energy cost, the Good-Luck Charm's 0% failure -- so leaving
    the previous turn's array in place would make the player wait a turn for
    something they bought this one."""
    smt = _smt()
    career_home = career["data"].get("home_info")
    if not isinstance(career_home, dict):
        return
    smt._refresh_command_info(
        chara_info, career_home, turn=chara_info.get("turn", 1),
        unlocked_npcs=[n[0] for n in
                       full_state.get(smt.single_mode_events.UNLOCKED_NPCS_KEY, [])],
        facility_levels=smt._facility_levels(career["data"]),
        race_history=career["data"].get("race_history", []),
        training_bonus=smt._training_bonus(full_state, chara_info),
        friendship_bonus=smt._friendship_bonus(full_state, chara_info),
        specialty_bonus=smt._specialty_bonus(full_state, chara_info),
        support_card_levels=smt._support_card_levels(full_state),
        friendship_stacks=smt._friendship_stacks(full_state),
        full_state=full_state)


def _exchange(payload: dict, keys, refresh: bool = False) -> dict:
    """Buy every slot the request names, in order, stopping at none of them.

    A slot that cannot be bought (coins short, already bought to its
    limit_buy_count, no longer in the lineup) is SKIPPED rather than failing the
    whole call: the client re-reads the lineup and the coin balance out of the
    response either way, so a partial basket self-corrects on screen instead of
    leaving the player with an error and no idea which item was the problem."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return copy.deepcopy(_REFUSE)

    bought = 0
    for entry in payload.get("exchange_item_info_array") or []:
        if shop.buy(full_state, entry.get("shop_item_id")):
            bought += 1
        else:
            log.info("trackblazer: shop slot %s refused", entry.get("shop_item_id"))
    log.info("trackblazer: bought %s item(s), %s coins left",
             bought, impl.state(full_state).get("coins"))
    if refresh:
        _refresh_previews(full_state, career, career["data"]["chara_info"])
    response = _envelope(career, keys)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_multi_item_exchange(payload: dict) -> dict:
    """single_mode_free/multi_item_exchange -- buy by shop slot. Capture-backed;
    the observed response carries {chara_info, free_data_set}."""
    return _exchange(payload, ("chara_info",))


def handle_multi_item_use(payload: dict) -> dict:
    """Use every item the request names.

    The instant effects (stats, energy, motivation, bond, conditions, a facility
    level, the reshuffle) land on the PERSISTED chara_info, so the response's
    copy and the next turn's agree; the durational ones go into
    item_effect_array and are read back by the training preview."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return copy.deepcopy(_REFUSE)

    chara_info = career["data"]["chara_info"]
    turn = int(payload.get("current_turn") or chara_info.get("turn") or 1)
    for entry in payload.get("use_item_info_array") or []:
        item_id = entry.get("item_id")
        count = max(1, int(entry.get("use_num") or 1))
        if not shop.use(full_state, chara_info, career["data"], item_id, turn, count):
            log.info("trackblazer: item %s x%s refused", item_id, count)
    career["data"]["chara_info"] = chara_info
    _refresh_previews(full_state, career, chara_info)
    response = _envelope(career, ("chara_info", "home_info"))
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_multi_item_exchange2(payload: dict) -> dict:
    """single_mode_free/multi_item_exchange2 -- the same shopping trip as
    multi_item_exchange, answered with a fuller envelope.

    dump.cs has the two requests identical ({exchange_item_info_array,
    current_turn}); the only difference is the response, where "2" adds
    home_info to {chara_info, free_data_set}. That reads as a later revision of
    the same call made when buying could move the training screen -- a stat
    charm shifts the caps, a megaphone shifts every gain -- so this one refreshes
    the previews before answering, exactly as multi_item_use does. Never seen in
    a capture; the client we serve posts the original. Sharing the buy loop
    means whichever one it posts behaves identically."""
    return _exchange(payload, ("chara_info", "home_info"), refresh=True)
