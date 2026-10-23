
from __future__ import annotations

import copy
import functools
import json

from ... import region
from ... import state as state_store
from ...handlers import load as load_mod
from ...handlers import shop, single_mode_team
from . import roadmap
from ...result_codes import ResultCode, refuse


def _ok(data=None) -> dict:
    env = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}
    env["data"] = data if data is not None else {}
    return env


def _state(viewer_id) -> dict:
    return state_store.get_state(viewer_id) or {}


def handle_training_roadmap_load(payload: dict) -> dict:
    full_state = _state(payload["viewer_id"])
    rotations = [{"deck_num": d["deck_num"], "deck_name": d["deck_name"] or "",
                  "race_array": d["race_array"]}
                 for d in single_mode_team._reserved_decks(full_state)]
    return _ok({
        "plan_array": [{"plan_id": i, "title": f"シート{i}"} for i in range(1, 11)],
        "character_array": [],
        "succession_plan_array": [{"plan_id": i, "title": f"シート{i}"} for i in range(1, 11)],
        "succession_character_array": [],
        "selected_character_identifier": roadmap.selected(full_state),
        "compare_source_array": [],
        "compare_friend_trained_chara_array": [],
        "single_mode_reserved_race_array": copy.deepcopy(rotations),
        "generate_succession_reserved_race_array": [
            {"deck_num": i, "deck_name": "", "race_array": []} for i in range(1, 9)],
        "generate_succession_support_card_deck_array": [
            {"deck_id": i, "name": f"デッキ{i}", "support_card_id_array": [0] * 6,
             "campaign_rental_support_card_id_array": []} for i in range(1, 11)],
        "tutorial_info": {"tutorial_step": 0, "tutorial_share_id": 0},
    })


SUCCESSION_DECK_SETS = 5
SUCCESSION_DECKS_PER_SET = 5


def handle_pre_single_mode_index(payload: dict) -> dict:
    resp = single_mode_team.handle_pre_single_mode_index(payload)
    data = resp.setdefault("data", {})
    for key in ("succession_trained_chara_data", "event_succession_trained_chara_data"):
        data[key] = {"succession_trained_chara_array": [], "summary_user_info_array": []}
    data["succession_deck"] = [
        {"deck_set_id": s, "deck_id": d, "deck_name": f"編成{d}",
         "succession_trained_chara_1": [], "succession_trained_chara_2": []}
        for s in range(1, SUCCESSION_DECK_SETS + 1)
        for d in range(1, SUCCESSION_DECKS_PER_SET + 1)]
    return resp


def handle_succession_deck_last_used(payload: dict) -> dict:
    card_id = int(payload.get("card_id") or 0)
    last = (_state(payload["viewer_id"]).get("jp_succession_last_used") or {}).get(str(card_id))
    return _ok({"succession_deck_last_used": copy.deepcopy(last) or
                {"card_id": card_id, "last_start_time": 0}})


EXTEND_SAVE_COST = 30
EXTEND_SAVE_MAX = 1000


def handle_extend_save_capacity(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    add = int(payload.get("add_count") or 0)
    full_state = _state(viewer_id)
    data = load_mod.get_or_seed_data(full_state, viewer_id)
    now_num = int(data.get("trained_chara_extend_save_num") or 0)
    if add <= 0 or now_num + add > EXTEND_SAVE_MAX:
        return refuse(ResultCode.PARAM_ERROR)
    coin = shop.spend_carats(full_state, EXTEND_SAVE_COST * add)
    if coin is None:
        return refuse(ResultCode.PARAM_ERROR)
    data["trained_chara_extend_save_num"] = now_num + add
    state_store.save_state(viewer_id, full_state)
    return _ok({"coin_info": copy.deepcopy(coin), "trained_chara_extend_save_num": now_num + add})


@functools.lru_cache(maxsize=1)
def _extend_master() -> dict:
    return json.loads((region.seeds_dir() / "support_card_extend_master_info.json")
                      .read_text(encoding="utf-8"))


def handle_extend_master_info(payload: dict) -> dict:
    data = copy.deepcopy(_extend_master())
    from ... import custom_support
    rows = data.get("event_skill_info_array") or []
    by_id = {r.get("support_card_id"): r for r in rows}
    for cid, d in custom_support.definitions().items():
        if cid not in by_id:
            donor = by_id.get(int(d.get("event_donor") or 0)) or {}
            rows.append({"support_card_id": cid,
                         "event_skill_id_array": list(donor.get("event_skill_id_array") or [])})
    return _ok(data)


def _ack(payload: dict) -> dict:
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}


HANDLERS = {
    "training_roadmap/load": handle_training_roadmap_load,
    "pre_single_mode/index": handle_pre_single_mode_index,
    "succession_deck/get_succession_deck_last_used": handle_succession_deck_last_used,
    "trained_chara/extend_save_capacity": handle_extend_save_capacity,
    "support_card/get_extend_master_info": handle_extend_master_info,
    "user/load_real_fanfare": _ack,
    "read_info/notify_product": _ack,
    "read_info/umaplan_bonus_received_dialog": _ack,
    "training_report/save_purchase_dialog_read_time": _ack,
}
