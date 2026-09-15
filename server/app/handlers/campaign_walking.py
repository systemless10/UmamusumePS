"""Campaign Walking ("Character Outing") -- fill a gauge by doing daily
activities, then send one of your umas out to one of her 3 locations for a
reward. Home screen icon; unrelated to Team Trials/URA despite reusing
their action hooks to fill the gauge.

Ground truth (master.mdb + a real capture, captures/20260904_232507/000[4-7]
-- request stored in file N, response in file N+1):

  campaign_walking_data (1 row, id=1, campaign_id=226): gauge_max=10,
  walking_limit=2 (max walks per DAY), gauge_up_singlemode=10,
  gauge_up_teamstadium=2, gauge_up_dailyrace=2, gauge_up_dailylegendrace=2,
  gauge_up_login=10. The capture's very first index of a session already
  showed walking_gauge=10 (a fresh login fills it outright) and one
  single_mode turn's worth of play does the same -- both columns are 10,
  i.e. "the WHOLE bar", where a stadium/daily race match is only worth a
  fifth of it (2/10, five matches to fill).

  campaign_walking_chara (66 rows, campaign_id=226): chara_id ->
  location_1/2/3, the 3 spots THAT character can be sent to.

  campaign_walking_location (67 rows): id -> reward_set_id (only 3 distinct
  values -- 3, 31 and 33 locations share reward_set 1/3/2 respectively, one
  per location "genre": Meal/Outdoor/Indoor) + love_point (flat 50 in every
  row, confirmed against the capture's love_point_pool_before/after: 1427 ->
  1477 -- that account's chara was at her love_rank_limit, which is why the
  50 landed in the POOL there; bond.py owns the cap/overflow split).

  campaign_walking_reward_set (15 rows total: 5 per reward_set_id): each row
  IS a reward_id (referenced directly -- go_walking's request `reward_id` is
  this table's own `id`, confirmed: reward_id 14 -> {reward_set_id 3,
  item_category 34, item_id 95, item_num 5}, exactly the item the capture's
  check_result granted), limit_num 1 -- an ACCOUNT-WIDE one-time reward,
  never re-granted once claimed by ANY character. 3 reward_sets x 5 rows =
  the 15 total rewards this campaign ever pays out; once every id has been
  claimed (by any combination of characters/locations), outings keep working
  for the flavor (love point + the memory log) but stop minting new items.

  Wire shapes (dump.cs, exact):
    CampaignWalkingIndexResponse.CommonResponse
      { walking_gauge, today_walking_num,
        CampaignWalkingReward[] walking_reward_info {reward_id, exchange_count},
        NoteDataForDisplay[] chara_walking_act_array {chara_id, data_id, create_time, new_flag} }
    CampaignWalkingGoWalkingRequest  { chara_id, location_id, reward_id }
    CampaignWalkingGoWalkingResponse.CommonResponse { today_walking_num }
    CampaignWalkingCheckResultResponse.CommonResponse
      { CampaignWalkingResult result {chara_id, love_point_info, reward_list,
        new_walking_act, new_chara_profile_array}, reward_summary_info }
    CampaignWalkingLoadInfo (embedded in load/index)
      { walking_gauge, today_walking_num, show_login_bonus, show_tips,
        resume_state, resume_chara_id, resume_location_id }

  chara_walking_act_array is the account's PERSISTENT "memory" log -- one
  entry per (chara_id, location_id) pair ever visited, data_id == the
  location_id (capture: go_walking(chara 1068, location 13, ...) ->
  check_result's new_walking_act has data_id 13). new_flag is 1 ONLY on the
  entry check_result just created; every entry index later reports back has
  new_flag 0 (same one-shot pattern as limited_shop's appear_flag /
  note_archive's granted-rank tracking).

  go_walking gates on the gauge being FULL (>= gauge_max) and consumes it
  entirely (capture: gauge 10 before, 0 after go_walking+check_result), and
  on today_walking_num < walking_limit (bumped 1 -> 2 across the capture's
  two calls). It stashes the pick as `pending`; check_result (a SEPARATE
  call, played after the walking cutscene) is what actually grants the
  reward and appends the memory log entry -- go_walking's own response
  carries nothing but the bumped counter.

No capture exists for what happens if go_walking is called against an
inactive campaign, an unowned chara/location pairing, or a mismatched
reward_id -- refused with response_code 205 (this server's usual "no" for
front-end mistakes the real client wouldn't produce), same convention as
presents.py/shop.py.
"""

from __future__ import annotations

import functools
import time

from .. import master_data
from .. import state as state_store
from . import bond, daily_races, registry, shop

STATE_KEY = "campaign_walking_state"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- master.mdb

@functools.lru_cache(maxsize=1)
def _all_data_rows() -> tuple:
    return tuple(master_data.query("SELECT * FROM campaign_walking_data"))


def _active_row(now_ts: int):
    """The one campaign_walking_data row whose campaign_data window is open
    right now, or None. campaign_walking_data carries no dates of its own --
    it's joined onto campaign_data by campaign_id, exactly like every other
    campaign_* sub-table (walking_chara/location/reward_set) is scoped to it."""
    for row in _all_data_rows():
        camp = master_data.query_one(
            "SELECT start_time, end_time FROM campaign_data WHERE campaign_id=?",
            (row["campaign_id"],))
        if camp and camp["start_time"] <= now_ts <= camp["end_time"]:
            return row
    return None


@functools.lru_cache(maxsize=256)
def _chara_row(campaign_id: int, chara_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_chara WHERE campaign_id=? AND chara_id=?",
        (campaign_id, chara_id))


@functools.lru_cache(maxsize=256)
def _location_row(location_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_location WHERE id=?", (location_id,))


@functools.lru_cache(maxsize=256)
def _reward_row(reward_id: int):
    return master_data.query_one(
        "SELECT * FROM campaign_walking_reward_set WHERE id=?", (reward_id,))


# --------------------------------------------------------------- state

def _served_day() -> int:
    return daily_races._served_day()


def _state(full_state: dict, row) -> dict:
    st = full_state.setdefault(STATE_KEY, {})
    if st.get("campaign_id") != row["campaign_id"]:
        # New (or first-ever) walking campaign -- claimed rewards and the
        # memory log are account-wide/lifetime, so only the gauge/day
        # counters reset, same split limited_shop.py's own _state makes
        # between per-campaign opens and lifetime buying history.
        st.clear()
        st["campaign_id"] = row["campaign_id"]
        st["gauge"] = 0
        st["day"] = _served_day()
        st["today_walking_num"] = 0
        st["claimed_reward_ids"] = []
        st["acts"] = []          # [{chara_id, data_id, create_time}]
        st["pending"] = None
    day = _served_day()
    if st.get("day") != day:
        st["day"] = day
        st["today_walking_num"] = 0
    st.setdefault("claimed_reward_ids", [])
    st.setdefault("acts", [])
    return st


def add_gauge(full_state: dict, kind: str) -> tuple[int, int] | None:
    """Bump the walking gauge for one activity ('singlemode', 'teamstadium',
    'dailyrace', 'dailylegendrace' or 'login') by that column's
    gauge_up_<kind>, capped at gauge_max. Returns (before, after), or None
    when there's no active campaign right now (callers that surface a
    campaign_walking_gauge_info field use that to fall back to the existing
    None-when-absent default), so every caller can call this unconditionally
    without checking availability itself."""
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return None
    st = _state(full_state, row)
    before = st["gauge"]
    amount = row[f"gauge_up_{kind}"] or 0
    st["gauge"] = min(row["gauge_max"], before + amount)
    return (before, st["gauge"])


def gauge_info(full_state: dict, kind: str) -> dict | None:
    """add_gauge, shaped as the wire's CampaignWalkingGaugeInfo
    ({before_gauge, after_gauge}) -- what daily_race/team_stadium responses
    embed. None (not a zeroed dict) when there's no active campaign, so a
    quiet period doesn't masquerade as "the bar moved from 0 to 0"."""
    result = add_gauge(full_state, kind)
    if result is None:
        return None
    before, after = result
    return {"before_gauge": before, "after_gauge": after}


# --------------------------------------------------------------- endpoints

@registry.endpoint("campaign_walking/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _ok({"walking_gauge": 0, "today_walking_num": 0,
                     "walking_reward_info": [], "chara_walking_act_array": []})
    st = _state(full_state, row)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "walking_gauge": st["gauge"],
        "today_walking_num": st["today_walking_num"],
        "walking_reward_info": [{"reward_id": rid, "exchange_count": 1}
                                for rid in st["claimed_reward_ids"]],
        "chara_walking_act_array": [
            {"chara_id": a["chara_id"], "data_id": a["data_id"],
             "create_time": a["create_time"], "new_flag": 0}
            for a in st["acts"]],
    })


def load_info(viewer_id) -> dict:
    """Built for load/index's campaign_walking_load_info -- same counters as
    index plus the resume_* fields dump.cs carries for a go_walking that was
    started but never followed up with check_result (e.g. the client was
    closed mid-cutscene). show_login_bonus/show_tips: no capture ever shows
    either true, and nothing else on this server tracks "has this account
    ever seen the walking tutorial" -- always false rather than fabricating
    a tutorial popup that would show every single login."""
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return {"walking_gauge": 0, "today_walking_num": 0,
                "show_login_bonus": False, "show_tips": False,
                "resume_state": 0, "resume_chara_id": 0, "resume_location_id": 0}
    st = _state(full_state, row)
    pending = st.get("pending")
    return {
        "walking_gauge": st["gauge"],
        "today_walking_num": st["today_walking_num"],
        "show_login_bonus": False,
        "show_tips": False,
        "resume_state": 1 if pending else 0,
        "resume_chara_id": (pending or {}).get("chara_id", 0),
        "resume_location_id": (pending or {}).get("location_id", 0),
    }


@registry.endpoint("campaign_walking/go_walking")
def handle_go_walking(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _refuse()
    st = _state(full_state, row)

    chara_id = payload.get("chara_id")
    location_id = payload.get("location_id")
    reward_id = payload.get("reward_id")

    chara = _chara_row(row["campaign_id"], chara_id)
    if chara is None or location_id not in (
            chara["location_1"], chara["location_2"], chara["location_3"]):
        return _refuse()
    loc = _location_row(location_id)
    reward = _reward_row(reward_id)
    if loc is None or reward is None or reward["reward_set_id"] != loc["reward_set_id"]:
        return _refuse()
    if st["gauge"] < row["gauge_max"] or st["today_walking_num"] >= row["walking_limit"]:
        return _refuse()

    st["gauge"] = 0
    st["today_walking_num"] += 1
    st["pending"] = {"chara_id": chara_id, "location_id": location_id,
                     "reward_id": reward_id}
    state_store.save_state(viewer_id, full_state)
    return _ok({"today_walking_num": st["today_walking_num"]})


def _empty_summary() -> dict:
    return {"add_item_list": [], "add_piece_list": [], "add_card_list": [],
            "add_card_bonus_info": None, "add_support_card_list": [],
            "add_support_card_num_array": [], "add_honor_list": [], "add_chara_list": [],
            "add_cloth_list": [], "add_music_list": [], "add_story_id_array": [],
            "add_fcoin": 0, "add_present_num": 0, "add_total_fan": 0,
            "new_chara_profile_array": [], "force_update_honor_id": 0}


_CATEGORY_CARAT = 90
_CATEGORY_MONEY = 91   # item 59, plain item count like everything below


def _grant_reward(full_state: dict, reward, summary: dict) -> dict:
    """Apply one campaign_walking_reward_set row and record it in
    reward_summary_info. Every category this table actually uses (90 carats,
    91 money-as-item, 20/21/30/34/93/97/103/164 misc items) resolves to
    either the wallet or a plain item count -- none is a piece/card/support/
    cloth/music id (see module docstring's category list)."""
    category, item_id, num = reward["item_category"], reward["item_id"], reward["item_num"]
    if category == _CATEGORY_CARAT:
        shop.grant_carats(full_state, num)
        summary["add_fcoin"] += num
    else:
        shop._add_item(full_state, item_id, num)
        summary["add_item_list"].append({"item_id": item_id, "number": num})
    return {"item_type": category, "item_id": item_id, "item_num": num}


@registry.endpoint("campaign_walking/check_result")
def handle_check_result(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = int(time.time())
    row = _active_row(now_ts)
    if row is None:
        return _refuse()
    st = _state(full_state, row)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    st["pending"] = None

    chara_id = pending["chara_id"]
    location_id = pending["location_id"]
    reward_id = pending["reward_id"]
    loc = _location_row(location_id)
    love_gain = (loc["love_point"] if loc else 0) or 0

    # Bond: campaign_walking_location.love_point, through the shared cap/pool
    # split (bond.py). The capture this handler was written from banked the
    # whole 50 in love_point_pool because THAT account's chara was already at
    # her love_rank_limit -- the pool is overflow, not the destination, so a
    # character with room now genuinely gains bond from an outing.
    love_info, new_profiles = bond.grant(full_state, chara_id, love_gain)

    summary = _empty_summary()
    reward_list = []
    if reward_id not in st["claimed_reward_ids"]:
        reward = _reward_row(reward_id)
        if reward is not None:
            reward_list.append(_grant_reward(full_state, reward, summary))
            st["claimed_reward_ids"].append(reward_id)

    now = _now()
    already = any(a["chara_id"] == chara_id and a["data_id"] == location_id
                  for a in st["acts"])
    if not already:
        st["acts"].append({"chara_id": chara_id, "data_id": location_id, "create_time": now})
    new_walking_act = {"chara_id": chara_id, "data_id": location_id,
                       "create_time": now, "new_flag": 1}

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "result": {
            "chara_id": chara_id,
            "love_point_info": love_info,
            "reward_list": reward_list,
            "new_walking_act": new_walking_act,
            "new_chara_profile_array": new_profiles,
        },
        "reward_summary_info": summary,
    })
