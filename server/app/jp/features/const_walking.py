
from __future__ import annotations

import random
import re
import time

from ... import master_data
from ... import state as state_store
from ...handlers import bond, collection, daily_races, missions, shop
from ...result_codes import ResultCode, refuse
from . import schedule_book

STATE_KEY = "jp_const_walking"
TICKET_ITEM_ID = 282
LOVE_POINT_PER_WALK = 15

_FINISH = re.compile(r"^single_mode(_[a-z_]+)?/finish$")


def _ok(data: dict | None) -> dict:
    env = {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}}
    if data is not None:
        env["data"] = data
    return env


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _campaign_ids() -> list[int]:
    if not master_data.has_table("constant_walking_data"):
        return []
    return [int(r["campaign_id"]) for r in master_data.query(
        "SELECT campaign_id FROM constant_walking_data WHERE start_time <= ? ORDER BY id",
        (int(time.time()),))]


def _pairs(chara_id: int) -> list[tuple[int, int]]:
    pairs = []
    for cid in _campaign_ids():
        row = master_data.query_one(
            "SELECT location_1, location_2, location_3 FROM campaign_walking_chara "
            "WHERE campaign_id=? AND chara_id=?", (cid, chara_id))
        if row:
            pairs += [(cid, int(row[k])) for k in ("location_1", "location_2", "location_3")
                      if row[k]]
    return pairs


def _state(full_state: dict) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    st.setdefault("acts", [])
    st.setdefault("ticket_day", None)
    st.setdefault("login_time", None)
    st.setdefault("pending", None)
    st.setdefault("walks", {})
    return st


def _received_today(st: dict) -> bool:
    return st.get("ticket_day") == daily_races._served_day()


def _owns(full_state: dict, chara_id: int) -> bool:
    return any(c.get("chara_id") == chara_id
               for c in full_state.get(collection.CHARA_LIST_KEY) or [])


def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    now = _now_str()
    previous = st["login_time"] or now
    st["login_time"] = now
    state_store.save_state(viewer_id, full_state)
    pending = st["pending"]
    return _ok({
        "chara_walking_act_array": [
            {"chara_id": a["chara_id"], "data_id": a["data_id"],
             "create_time": a["create_time"], "new_flag": 0} for a in st["acts"]],
        "resume_state": 1 if pending else 0,
        "resume_chara_id": pending["chara_id"] if pending else 0,
        "resume_location_id_array": [p[1] for p in pending["picks"]] if pending else [],
        "resume_campaign_id_array": [p[0] for p in pending["picks"]] if pending else [],
        "is_received_daily_ticket": _received_today(st),
        "const_walking_login_time": previous,
    })


def handle_go_walking(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    chara_id = int(payload.get("chara_id") or 0)
    walking_num = int(payload.get("walking_num") or 0)
    location_id = int(payload.get("location_id") or 0)
    campaign_id = int(payload.get("campaign_id") or 0)

    pairs = _pairs(chara_id)
    if walking_num < 1 or not pairs or not _owns(full_state, chara_id):
        return refuse()
    if shop.item_count(full_state, TICKET_ITEM_ID) < walking_num:
        return refuse(ResultCode.NOT_ENOUGH_ERROR)
    if location_id or campaign_id:
        if walking_num != 1 or (campaign_id, location_id) not in pairs:
            return refuse()
        picks = [(campaign_id, location_id)]
    else:
        picks = random.sample(pairs, min(walking_num, len(pairs)))
        picks += random.choices(pairs, k=walking_num - len(picks))

    shop.add_item(full_state, TICKET_ITEM_ID, -walking_num)
    st["pending"] = {"chara_id": chara_id, "picks": [list(p) for p in picks]}
    schedule_book.note_walk(full_state, chara_id)
    state_store.save_state(viewer_id, full_state)
    return _ok(None)


def handle_check_result(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st["pending"]
    if not pending:
        return refuse()
    st["pending"] = None
    chara_id = pending["chara_id"]
    picks = [tuple(p) for p in pending["picks"]]

    love_info, new_profiles = bond.grant(full_state, chara_id, LOVE_POINT_PER_WALK * len(picks))

    for cid, _loc in picks:
        st["walks"][str(cid)] = st["walks"].get(str(cid), 0) + 1

    now = _now_str()
    seen = {(a["chara_id"], a["campaign_id"], a["data_id"]) for a in st["acts"]}
    released: dict[int, list] = {}
    for cid, loc in picks:
        if (chara_id, cid, loc) in seen:
            continue
        seen.add((chara_id, cid, loc))
        st["acts"].append({"chara_id": chara_id, "campaign_id": cid, "data_id": loc,
                           "create_time": now})
        released.setdefault(cid, []).append(
            {"chara_id": chara_id, "data_id": loc, "create_time": now, "new_flag": 1})

    state_store.save_state(viewer_id, full_state)
    return _ok({"result": {
        "chara_id": chara_id,
        "love_point_info": love_info,
        "const_walking_release_array": [{"campaign_id": cid, "new_walking_act_array": acts}
                                        for cid, acts in released.items()],
        "new_chara_profile_array": new_profiles,
    }})


def after(endpoint: str, payload: dict, resp: dict) -> None:
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    data = resp.get("data")
    if not isinstance(data, dict):
        return
    if endpoint == "load/index":
        full_state = state_store.get_state(payload.get("viewer_id")) or {}
        st = _state(full_state)
        data["is_received_daily_walking_ticket"] = _received_today(st)
        if st["login_time"]:
            data["const_walking_login_time"] = st["login_time"]
    elif _FINISH.match(endpoint) and isinstance(data.get("single_mode_finish_common"), dict):
        viewer_id = payload.get("viewer_id")
        full_state = state_store.get_state(viewer_id) or {}
        st = _state(full_state)
        if _received_today(st):
            return
        st["ticket_day"] = daily_races._served_day()
        shop.add_item(full_state, TICKET_ITEM_ID, 1)
        state_store.save_state(viewer_id, full_state)
        common = data["single_mode_finish_common"]
        reward = common.get("daily_reward_info")
        if not isinstance(reward, dict):
            reward = common["daily_reward_info"] = {}
        reward.setdefault("add_item_list", []).append({"item_id": TICKET_ITEM_ID, "number": 1})


HANDLERS = {
    "const_walking/index": handle_index,
    "const_walking/go_walking": handle_go_walking,
    "const_walking/check_result": handle_check_result,
}


_COND_WALK_COUNT = 600045


def _walk_count(viewer_id, full_state: dict, row) -> int:
    walks = (full_state.get(STATE_KEY) or {}).get("walks") or {}
    campaign_id = row["condition_value_1"] or 0
    if campaign_id:
        return walks.get(str(campaign_id), 0)
    return sum(walks.values())


missions.CONDITION_HOOKS[_COND_WALK_COUNT] = _walk_count
