
from __future__ import annotations

import copy
import logging

from . import impl, shop
from ...result_codes import refuse

log = logging.getLogger("uma-server")

_REFUSE = refuse()


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def _envelope(career: dict, keys) -> dict:
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
    return _exchange(payload, ("chara_info",))


def handle_multi_item_use(payload: dict) -> dict:
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
    return _exchange(payload, ("chara_info", "home_info"), refresh=True)
