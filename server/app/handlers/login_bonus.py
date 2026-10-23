
from __future__ import annotations

import datetime

from .. import master_data
from .. import patch
from .. import state as state_store
from . import daily_races, presents

LOGIN_BONUS_STATE_KEY = "login_bonus_state"
_BONUS_TYPE_NORMAL = 1
_BONUS_TYPE_MULTI_STEP = 4
LOGIN_DAYS_KEY = "login_days_recorded"


def _parse_mdb_datetime(value) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    from .missions import mdb_tz
    return int(dt.replace(tzinfo=mdb_tz()).timestamp())


def _active_campaigns(now_ts: int) -> list:
    rows = master_data.query("SELECT * FROM login_bonus_data")
    active = []
    for row in rows:
        start = _parse_mdb_datetime(row["start_date"])
        end = _parse_mdb_datetime(row["end_date"])
        if start and now_ts < start:
            continue
        if end and now_ts > end:
            continue
        active.append(row)
    return active


def _grant_day(viewer_id, login_bonus_id: int, count: int) -> None:
    row = master_data.query_one(
        "SELECT * FROM login_bonus_detail WHERE login_bonus_id=? AND count=?",
        (login_bonus_id, count))
    if not row:
        return
    for suffix in ("", "_2", "_3", "_4", "_5"):
        cat = row[f"item_category{suffix}"]
        if not cat:
            continue
        presents.admin_send(
            viewer_id, item_category=cat, item_id=row[f"item_id{suffix}"],
            item_num=row[f"item_num{suffix}"],
            message_id=1007, message_params=(login_bonus_id, count))


def apply_and_report(viewer_id) -> list:
    now_ts = patch._servertime()
    today = daily_races._served_day()
    active = _active_campaigns(now_ts)

    full_state = state_store.get_state(viewer_id) or {}
    campaigns = (full_state.get(LOGIN_BONUS_STATE_KEY) or {}).get("campaigns") or {}

    days = full_state.setdefault(LOGIN_DAYS_KEY, {}).setdefault("days", [])
    if today not in days:
        days.append(today)
        from . import campaign_walking
        campaign_walking.add_gauge(full_state, "login")
        from . import map_event
        map_event.add_gauge(full_state, "login")
        state_store.save_state(viewer_id, full_state)

    def _claimed(r):
        return campaigns.get(str(r["id"])) or {}
    current_step = {}
    for row in sorted((r for r in active if r["type"] == _BONUS_TYPE_MULTI_STEP),
                      key=lambda r: r["step"]):
        g = row["group_id"]
        if g in current_step:
            continue
        if any(_claimed(o).get("last_claim_day") == today for o in active
               if o["type"] == _BONUS_TYPE_MULTI_STEP and o["group_id"] == g):
            current_step[g] = None
        elif (_claimed(row).get("total_count") or 0) < row["count_num"]:
            current_step[g] = row["id"]
    active = [r for r in active if r["type"] != _BONUS_TYPE_MULTI_STEP
              or current_step.get(r["group_id"]) == r["id"]]

    to_grant = []
    for row in active:
        lbid = row["id"]
        c = campaigns.get(str(lbid)) or {}
        total = c.get("total_count") or 0
        if c.get("last_claim_day") == today:
            continue
        if total >= row["count_num"]:
            if row["type"] != _BONUS_TYPE_NORMAL:
                continue
            total = 0
        to_grant.append((lbid, total + 1))

    if not to_grant:
        return []

    for lbid, new_total in to_grant:
        _grant_day(viewer_id, lbid, new_total)

    full_state = state_store.get_state(viewer_id) or {}
    live_campaigns = full_state.setdefault(LOGIN_BONUS_STATE_KEY, {}).setdefault("campaigns", {})
    for lbid, new_total in to_grant:
        live_campaigns[str(lbid)] = {"total_count": new_total, "last_claim_day": today}
    state_store.save_state(viewer_id, full_state)
    return [{"login_bonus_id": lbid, "total_count": new_total} for lbid, new_total in to_grant]


def total_login_days(full_state: dict) -> int:
    return len((full_state.get(LOGIN_DAYS_KEY) or {}).get("days") or [])


def logged_in_today(full_state: dict) -> bool:
    today = daily_races._served_day()
    return today in ((full_state.get(LOGIN_DAYS_KEY) or {}).get("days") or [])
