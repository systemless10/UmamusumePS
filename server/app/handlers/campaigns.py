
from __future__ import annotations

import logging
import time

from .. import master_data
from .idle_single_mode import _time_str

log = logging.getLogger(__name__)

USER_KEY = "user_campaigns"
RAFFLE_KEY = "raffle_campaign"

TARGET_SINGLE_MODE = 1
TARGET_RAFFLE = 11
EFFECT_SINGLE_REWARD = 1
EFFECT_CONST_WALKING_UP = 21
_RACE_LISTED_EFFECTS = (EFFECT_SINGLE_REWARD, EFFECT_CONST_WALKING_UP)

RAFFLE_BEFORE, RAFFLE_RAFFLED, RAFFLE_STARTED = 1, 2, 3
_RAFFLE_DRAW_VALUE = 3


def _now(now=None) -> int:
    return int(now if now is not None else time.time())


def _row(campaign_id: int):
    return master_data.query_one("SELECT * FROM campaign_data WHERE campaign_id=?",
                                 (int(campaign_id),))


def _held(full_state: dict, now: int) -> dict:
    out = {}
    for e in (full_state or {}).get(USER_KEY) or ():
        if int(e.get("end_ts") or 0) > now:
            out[int(e["campaign_id"])] = int(e["end_ts"])
    return out


def grant(full_state: dict, campaign_id: int, end_ts: int) -> None:
    lst = [e for e in full_state.get(USER_KEY) or () if e.get("campaign_id") != campaign_id]
    lst.append({"campaign_id": int(campaign_id), "end_ts": int(end_ts)})
    full_state[USER_KEY] = lst


def admin_grant(full_state: dict, opener_id: int, seconds: int, now=None) -> list:
    now = _now(now)
    ids = [r["campaign_id"] for r in master_data.query(
        "SELECT campaign_id FROM campaign_data WHERE open_flag_id=?", (int(opener_id),))]
    for cid in ids:
        grant(full_state, cid, now + int(seconds))
    return ids


def user_campaign_info_array(full_state: dict, now=None) -> list:
    held = _held(full_state, _now(now))
    return [{"campaign_id": cid, "end_time": _time_str(end)} for cid, end in sorted(held.items())]


def active_single_mode(full_state: dict, scenario_id: int, now=None) -> list:
    now = _now(now)
    held = _held(full_state, now)
    rows = master_data.query(
        "SELECT * FROM campaign_data WHERE target_type=? AND (target_id=0 OR target_id=?) "
        "AND start_time<=? AND end_time>=?",
        (TARGET_SINGLE_MODE, int(scenario_id or 0), now, now))
    rows = [r for r in rows if not r["open_flag_id"] or r["campaign_id"] in held]
    return sorted(rows, key=lambda r: (r["effect_type_1"], r["campaign_id"]))


def race_reward_campaigns(full_state: dict, scenario_id: int, now=None) -> tuple:
    rows = active_single_mode(full_state, scenario_id, now)
    pct = 100 + sum(r["effect_value_1"] - 100 for r in rows
                    if r["effect_type_1"] == EFFECT_SINGLE_REWARD and r["effect_value_1"] > 100)
    return pct, [r["campaign_id"] for r in rows if r["effect_type_1"] in _RACE_LISTED_EFFECTS]


def _open_raffle(now: int):
    return master_data.query_one(
        "SELECT * FROM campaign_data WHERE target_type=? AND start_time<=? AND end_time>=? "
        "ORDER BY campaign_id DESC LIMIT 1", (TARGET_RAFFLE, now, now))


def raffle_response_data(full_state: dict, now=None):
    row = _open_raffle(_now(now))
    if not row:
        return []
    st = (full_state.get(RAFFLE_KEY) or {}).get(str(row["campaign_id"])) or {}
    return {"campaign_id": row["campaign_id"], "state": st.get("state", RAFFLE_BEFORE),
            "campaign_story_condition_value": st.get("value", 0)}


def _set_raffle(full_state: dict, cid: int, **kw) -> None:
    allst = full_state.setdefault(RAFFLE_KEY, {})
    allst.setdefault(str(cid), {}).update(kw)


def raffle_index(full_state: dict, now=None) -> dict:
    now = _now(now)
    row = _open_raffle(now)
    if row:
        st = (full_state.get(RAFFLE_KEY) or {}).get(str(row["campaign_id"])) or {}
        if st.get("state", RAFFLE_BEFORE) == RAFFLE_BEFORE:
            _set_raffle(full_state, row["campaign_id"], state=RAFFLE_RAFFLED,
                        value=_RAFFLE_DRAW_VALUE)
    return {"response_data": raffle_response_data(full_state, now)}


def raffle_activate(full_state: dict, now=None) -> dict:
    now = _now(now)
    row = _open_raffle(now)
    if row:
        st = (full_state.get(RAFFLE_KEY) or {}).get(str(row["campaign_id"])) or {}
        if st.get("state") == RAFFLE_RAFFLED:
            admin_grant(full_state, row["campaign_id"], row["effect_value_1"], now)
            _set_raffle(full_state, row["campaign_id"], state=RAFFLE_STARTED)
    return {"response_data": raffle_response_data(full_state, now),
            "user_campaign_info_array": user_campaign_info_array(full_state, now)}


from .. import state as state_store
from . import registry


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


@registry.endpoint("raffle/index")
def handle_raffle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    out = raffle_index(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok(out)


@registry.endpoint("raffle/activate")
def handle_raffle_activate(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    out = raffle_activate(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok(out)
