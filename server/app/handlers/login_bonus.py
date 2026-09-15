"""Daily login bonus -- data.login_bonus_list (load/index's own field, no
dedicated endpoint: dump.cs has no LoginBonus*Request/Response class at
all, only LoginBonusData = {login_bonus_id, total_count} embedded directly
in LoginResponse.CommonResponse) plus its real reward table (master.mdb
login_bonus_data + login_bonus_detail).

Confirmed live via captures/20260818_081331/0009_load_index.json and
captures/20260811_141604/0006_load_index.json (same 3 real campaign ids,
~6 days apart, every total_count advanced by exactly 1 over that gap) --
there is no separate "claim" call; the reward is granted automatically by
the SERVER on login, and total_count simply reports how many days have
been granted so far. The CLIENT's own popup logic appears to compare this
served total_count against its local cache (same "compare progression
against SaveData.db" behavior load.py's own module docstring already
documents for fan/rank/etc.) -- so the bug this fixes (user-reported
2026-08-18: "the daily reward popup shows... doesn't actually send
rewards... pops up more than once per day") was that load/index served
the FROZEN seed's total_count forever (a snapshot that never advances =
client always thinks "not claimed today" = popup every single login)
while never granting anything to the mailbox at all.

Three real campaigns confirmed currently active (login_bonus_data, ids
match the two captures above exactly):
  11001  type 1, count_num 7,  start 2025/9/22,  end 2099 (evergreen)
  20001  type 2, count_num 10, start 2021/2/24,  end 2099 (evergreen)
  30047  type 3, count_num 10, start 2026/7/22,  end 2026/8/28 (time-boxed)
These line up with the THREE distinct popups reported: a 7-day "new
trainee" welcome sequence (11001), a 10-day evergreen "normal daily"
cycle (20001), and a time-boxed 10-day "anniversary" one (30047) --
`type` plausibly distinguishes one-time-vs-repeating cadence, but this
session found no confirmed source for what happens once total_count
reaches count_num (wrap back to day 1? stop?). Rather than guess, grants
CAP at count_num and simply stop -- the honest failure mode (a campaign
stops paying out once its verified reward table is exhausted) instead of
a fabricated wrap-around sequence nobody's confirmed exists.

Per-viewer state (LOGIN_BONUS_STATE_KEY): {"campaigns": {str(login_bonus_
id): {"total_count": int, "last_claim_day": int}}}; "day" is daily_races.
_served_day() (this server's one existing daily-reset clock, 05:00 JST via
the servertime this server already reports -- reused rather than
reinvented) so a campaign advances at most once per served day.

apply_and_report(viewer_id), called from load.py's handle_load_index,
does its OWN independent read-modify-write (like presents.admin_send,
which it calls) rather than sharing load.py's already-fetched full_state
object: state.py's blob is saved whole, not merged, so if this ran on a
stale copy fetched before admin_send's own save, saving afterward would
silently revert the very presents it just granted. Reward slots (1-5) are
granted as SEPARATE present-box entries via presents.admin_send -- some
days pay out more than one item (e.g. 30047 day 1: a support card AND
carats), matching how every other multi-item reward on this server is
mailed as one present per item, not one present per day.
"""

from __future__ import annotations

import datetime

from .. import master_data
from .. import patch
from .. import state as state_store
from . import daily_races, presents

LOGIN_BONUS_STATE_KEY = "login_bonus_state"
LOGIN_DAYS_KEY = "login_days_recorded"   # {"days": [daily_races._served_day(), ...]} --
                                         # every distinct calendar day (same 05:00 JST
                                         # reset clock the campaigns above use) this
                                         # account has genuinely had load/index called
                                         # on. Feeds missions.py's 600001 ("log in for a
                                         # total of N days") / 600022 ("Daily: Log in")
                                         # condition types -- nothing tracked THIS
                                         # granularly existed before (login_bonus_state
                                         # above is per-CAMPAIGN, not a plain day count).


def _parse_mdb_datetime(value) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.timestamp())


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
            message=f"Login Bonus (campaign {login_bonus_id}) -- Day {count}")


def apply_and_report(viewer_id) -> list:
    """Advance every active campaign at most once for today, grant what's
    newly due, and return the login_bonus_list array load/index serves --
    ONLY the campaigns that were newly granted THIS call, never a standing
    status list. Confirmed 2026-08-19 against a real capture (fresh real
    account, captures/20260819_092154/): 0060_load_index.json (the call
    right after tutorial completion, the FIRST grant) carries all 3 active
    campaigns at total_count 1; 0065_load_index.json (a genuine relaunch --
    tool_start_session at 0064 -- with nothing new to grant, same day)
    carries login_bonus_list: [] -- completely empty, not a repeat of the
    same 3 entries. Previously this returned the full status of every
    active campaign on EVERY call regardless of whether anything was new,
    which is exactly why user-reported 2026-08-19 "theres still 2 daily
    login event/reward screens that appear multiple times" (every relaunch,
    same session) kept happening: the client was being handed the same
    'you have a reward' payload it had already shown, every single time,
    because this function never stopped reporting campaigns once they'd
    already been paid out for the day."""
    now_ts = patch._servertime()
    today = daily_races._served_day()
    active = _active_campaigns(now_ts)

    full_state = state_store.get_state(viewer_id) or {}
    campaigns = (full_state.get(LOGIN_BONUS_STATE_KEY) or {}).get("campaigns") or {}

    # Record today as a genuine login day BEFORE any grant/save cycle below --
    # this must persist even on the to_grant-empty early return, and must be
    # visible to the RE-fetch after granting (see that fetch's own comment),
    # so save it here immediately rather than folding it into either of the
    # two campaign saves below.
    days = full_state.setdefault(LOGIN_DAYS_KEY, {}).setdefault("days", [])
    if today not in days:
        days.append(today)
        # First real login of the day -- campaign_walking's gauge_up_login
        # (independent of whether any login_bonus campaign above is active).
        from . import campaign_walking
        campaign_walking.add_gauge(full_state, "login")
        state_store.save_state(viewer_id, full_state)

    to_grant = []   # [(login_bonus_id, new_total_count)]
    for row in active:
        lbid = row["id"]
        c = campaigns.get(str(lbid)) or {}
        total = c.get("total_count") or 0
        if c.get("last_claim_day") != today and total < row["count_num"]:
            to_grant.append((lbid, total + 1))

    if not to_grant:
        return []

    for lbid, new_total in to_grant:
        _grant_day(viewer_id, lbid, new_total)

    # Re-fetch AFTER granting (see module docstring) so this save can't
    # clobber what the admin_send calls above just persisted.
    full_state = state_store.get_state(viewer_id) or {}
    live_campaigns = full_state.setdefault(LOGIN_BONUS_STATE_KEY, {}).setdefault("campaigns", {})
    for lbid, new_total in to_grant:
        live_campaigns[str(lbid)] = {"total_count": new_total, "last_claim_day": today}
    state_store.save_state(viewer_id, full_state)
    return [{"login_bonus_id": lbid, "total_count": new_total} for lbid, new_total in to_grant]


def total_login_days(full_state: dict) -> int:
    """Count of distinct calendar days apply_and_report has ever recorded
    for this account -- missions.py's 600001 ('Log in for a total of N
    days')."""
    return len((full_state.get(LOGIN_DAYS_KEY) or {}).get("days") or [])


def logged_in_today(full_state: dict) -> bool:
    """missions.py's 600022 ('Daily: Log in') -- true once load/index has
    run today (apply_and_report always runs before this could be checked,
    since both are called from the same handle_load_index)."""
    today = daily_races._served_day()
    return today in ((full_state.get(LOGIN_DAYS_KEY) or {}).get("days") or [])
