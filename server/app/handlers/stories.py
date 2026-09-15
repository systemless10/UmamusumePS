"""Story system: chara-story reward claims, read-flag persistence, main story.

Endpoints (registry-registered; neither is in main.py HANDLERS):
  character_story/first_clear -> reward claim after READING a chara story
        episode. Request carries `episode_id` = chara_story_data.id (the PK,
        NOT the 41xxxxxx story_id) -- proven by capture 20260717_180912
        transactions 0020-0023 (episode_id 477..480 = chara 1098 eps 1-4,
        reward 90/43/20 matching that row's add_reward_*). Response:
        character_story_data {episode_id, state:1} + reward_array +
        reward_summary_info + release_item_flag.
  read_info/index -> the client reports every id it just read/saw
        (add_*_data_array) and expects back the FULL per-viewer read set.
        Matched pair: 20260730_155008 request 0003 + response 0004 (labels
        lag by one). Persisting the union here is what makes "the next story
        unlocks after reading" stick across sessions.

Unlock gating (master.mdb):
  chara_story_data (434 rows): lock_type_1 == 6 is a BOND gate --
  lock_value_1_1 = chara_id, lock_value_1_2 = required love rank; the
  viewer's love_point (chara_collection, see collection.py) converts to a
  rank via love_rank (rank -> total_point thresholds, max rank 12).
  lock_type_1 == 0 = no gate. ON TOP of that, episodes are SEQUENTIAL:
  episode N needs episode N-1 first_cleared.

  main_story_data (118 rows, 7 parts) has the same reward columns plus
  THREE lock slots and prev_episode_index for the sequence. All 118 rows
  currently carry lock_type 0 in the Global mdb, but the evaluation is
  generic (shared _lock_ok). clear_main_story() is the one reusable
  gate+grant function -- see the TODO at the bottom for the missing
  endpoint.

Rewards are granted EXACTLY ONCE per episode (real first_clear is
first-time-only): cleared ids are tracked in the per-viewer "story_state"
key. A second claim returns the success envelope with no reward. A claim
whose gate is unmet (or whose previous episode is not cleared) refuses
with result_code 205, like every other refusal on this server.

First access seeds story_state from the load/index snapshot (the seed
account's character_story_data_list / main_story_data_list / read arrays),
mirroring collection.py: load/index serves those lists frozen from the
seed, so OUR cleared-set must start in sync with what the client is told
or sequential gating would refuse episodes the client shows as unlocked.
"""

from __future__ import annotations

import copy
import time

from .. import concerts
from .. import master_data
from .. import patch
from .. import state as state_store
from . import bond, collection, registry, shop

STORY_STATE_KEY = "story_state"
_JEWEL_CATEGORY = 90                  # carats/jewels -> coin_info fcoin
_HONOR_CATEGORY = 55                  # GameDefine.ItemCategory.HONOR (dump.cs)
_LOCK_NONE = 0
_LOCK_BOND = 6

# read_info/index request key -> response key (and our story_state.read_info
# key). Response order matches the capture.
_READ_INFO_FAMILIES = {
    "add_home_story_data_array": "home_story_data_array",
    "add_short_episode_data_array": "short_episode_data_array",
    "add_home_poster_data_array": "home_poster_data_array",
    "add_tutorial_guide_data_array": "tutorial_guide_data_array",
    "add_released_episode_data_array": "released_episode_data_array",
    "add_talk_gallery_data_array": "talk_gallery_data_array",
    "add_home_banner_data_array": "home_banner_data_array",
}
_RESPONSE_ORDER = [
    "home_story_data_array", "short_episode_data_array",
    "home_poster_data_array", "tutorial_guide_data_array",
    "released_episode_data_array", "talk_gallery_data_array",
    "home_banner_data_array",
]
# story_state.read_info key -> load/index seed key (talk gallery is named
# differently in the login snapshot).
_SEED_KEYS = {k: k for k in _RESPONSE_ORDER}
_SEED_KEYS["talk_gallery_data_array"] = "talk_gallery_list"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _seed_data() -> dict:
    """The load/index snapshot's data block (read-only source of seeds)."""
    from .load import _load_seed  # lazy: load.py imports collection at top level
    return (_load_seed() or {}).get("data") or {}


def _extract_ids(entries) -> list:
    """Read-info arrays arrive as [{'id': N}] (talk gallery as
    [{'home_story_trigger_id': N, ...}]); the seed uses the same shapes.
    Bare ints are tolerated so state written by tests/admin also parses."""
    ids = []
    for e in entries or []:
        if isinstance(e, dict):
            v = e.get("id", e.get("home_story_trigger_id", e.get("episode_id")))
        else:
            v = e
        if isinstance(v, int):
            ids.append(v)
    return ids


def _story_state(full_state: dict) -> dict:
    """Get (or seed, once) the per-viewer story state:
      chara_cleared / main_cleared : first_cleared episode ids (the mdb PKs)
      read_info                    : {response_key: [ids...]} for read_info/index
    """
    st = full_state.get(STORY_STATE_KEY)
    if isinstance(st, dict):
        return st
    seed = _seed_data()
    st = {
        "chara_cleared": [e.get("episode_id") for e in seed.get("character_story_data_list") or []
                          if e.get("state")],
        "main_cleared": [e.get("episode_id") for e in seed.get("main_story_data_list") or []
                         if e.get("state")],
        "read_info": {key: sorted(set(_extract_ids(seed.get(seed_key) or [])))
                      for key, seed_key in _SEED_KEYS.items()},
    }
    full_state[STORY_STATE_KEY] = st
    return st


# ---------------------------------------------------------------- gates

def _love_rank(full_state: dict, chara_id: int) -> int:
    """The viewer's bond rank with chara_id: love_point (chara_collection,
    falling back to the frozen seed before first load/index) -> love_rank
    (highest rank whose total_point threshold is reached)."""
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if charas is None:
        charas = _seed_data().get("chara_list") or []
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    return bond.love_rank((entry or {}).get("love_point") or 0)


def _lock_ok(full_state: dict, lock_type: int, value_1: int, value_2: int) -> bool:
    """One lock slot. Type 0 = open; type 6 = bond (love rank of chara
    value_1 must be >= value_2). Anything we can't evaluate stays LOCKED --
    wrongly granting is worse than wrongly refusing."""
    lock_type = lock_type or _LOCK_NONE
    if lock_type == _LOCK_NONE:
        return True
    if lock_type == _LOCK_BOND:
        return _love_rank(full_state, value_1) >= (value_2 or 0)
    return False


def _chara_episode_unlocked(full_state: dict, row, cleared: list) -> bool:
    """Bond gate + sequential gate for one chara_story_data row."""
    if not _lock_ok(full_state, row["lock_type_1"],
                    row["lock_value_1_1"], row["lock_value_1_2"]):
        return False
    if (row["episode_index"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM chara_story_data WHERE chara_id=? AND episode_index=?",
            (row["chara_id"], row["episode_index"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


# --------------------------------------------------------------- rewards

def _wallet(full_state: dict) -> dict:
    """coin_info_state, seeded from the snapshot if load/index hasn't run
    yet (same value collection.get_or_seed would have installed)."""
    wallet = full_state.get("coin_info_state")
    if not isinstance(wallet, dict):
        wallet = copy.deepcopy(_seed_data().get("coin_info") or {"fcoin": 0, "coin": 0})
        full_state["coin_info_state"] = wallet
    return wallet


def _grant_reward(full_state: dict, category: int, reward_id: int, num: int,
                  summary: dict, viewer_id=None) -> list:
    """Grant one add_reward_* triple into the viewer's containers and record
    it in reward_summary_info. Returns the reward_array entries. Category 90
    (jewels -- every chara-story reward, most main-story ones) goes to
    fcoin; category 55 (honors -- the item_category every mission_data row
    that pays out an epithet uses, per dump.cs's GameDefine.ItemCategory)
    goes through user_profile.grant_honor instead of shop._grant, which has
    no honor branch and would otherwise silently misfile a honor_id as a
    plain inventory item; cards/support cards/items reuse shop._grant so
    the containers and summary keys stay identical to a purchase.

    viewer_id is ONLY needed for the honor case -- grant_honor persists by
    viewer, and full_state alone (unlike collection.py's containers) has no
    viewer_id embedded in it to recover one from. Every current caller has
    a viewer_id in scope already; a caller that omits it just can't grant
    honor rewards (num/category still validate, but the branch no-ops)."""
    category = category or 0
    if not category or not num or num <= 0:
        return []
    if category == _JEWEL_CATEGORY:
        wallet = _wallet(full_state)
        wallet["fcoin"] = (wallet.get("fcoin") or 0) + num
        summary["add_fcoin"] += num
    elif category == _HONOR_CATEGORY:
        from . import user_profile  # lazy: avoids a top-level import cycle
        if viewer_id and user_profile.grant_honor(viewer_id, reward_id):
            summary.setdefault("add_honor_list", []).append(
                {"honor_id": reward_id, "create_time": time.strftime("%Y-%m-%d %H:%M:%S")})
    else:
        shop._grant(full_state,
                    {"change_item_category": category, "change_item_id": reward_id,
                     "change_item_num": num, "additional_piece_num": 0},
                    1, summary)
    return [{"item_type": category, "item_id": reward_id, "item_num": num}]


# ---------------------------------------- character_story/first_clear

@registry.endpoint("character_story/first_clear")
def handle_first_clear(payload: dict) -> dict:
    """Claim the first-read reward for one chara story episode. Gate is
    evaluated against OUR state (bond + sequence), never the client's word;
    reward is one-time -- a repeat claim succeeds with nothing in it."""
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM chara_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("chara_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        if not _chara_episode_unlocked(full_state, row, cleared):
            return _refuse()
        reward_array = _grant_reward(
            full_state, row["add_reward_category_1"], row["add_reward_id_1"],
            row["add_reward_num_1"], summary, viewer_id)
        cleared.append(episode_id)
        # keep the read-set coherent even if the client's read_info/index
        # report is missed: a claimed episode has been read.
        read = st.setdefault("read_info", {})
        released = set(read.get("released_episode_data_array") or [])
        released.add(row["story_id"])
        read["released_episode_data_array"] = sorted(released)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "character_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "release_item_flag": 0,
    })


# ----------------------------------------------------- read_info/index

@registry.endpoint("read_info/index")
def handle_read_info_index(payload: dict) -> dict:
    """Union everything the client just read/saw into the persistent
    per-viewer read set, then return the WHOLE set in the capture's shape.
    Reading a home story also enters the talk gallery (the two arrays track
    1:1 in every capture: same count, same ids)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    read = st.setdefault("read_info", {})
    for req_key, resp_key in _READ_INFO_FAMILIES.items():
        ids = _extract_ids(payload.get(req_key))
        if not ids:
            continue
        read[resp_key] = sorted(set(read.get(resp_key) or []) | set(ids))
        if resp_key == "home_story_data_array":
            read["talk_gallery_data_array"] = sorted(
                set(read.get("talk_gallery_data_array") or []) | set(ids))
    viewed = _extract_ids(payload.get("add_viewed_story_array"))
    if viewed:
        read["viewed_story_array"] = sorted(
            set(read.get("viewed_story_array") or []) | set(viewed))
    state_store.save_state(viewer_id, full_state)
    data = {}
    for resp_key in _RESPONSE_ORDER:
        ids = read.get(resp_key) or []
        if resp_key == "talk_gallery_data_array":
            data[resp_key] = [{"home_story_trigger_id": i, "new_flag": 0} for i in ids]
        else:
            data[resp_key] = [{"id": i} for i in ids]
    return _ok(data)


# ----------------------------------------------------------- main story

def _main_story_unlocked(full_state: dict, row, cleared: list) -> bool:
    """Sequence (prev_episode_index within the part) + all three lock slots."""
    prev_index = row["prev_episode_index"] or 0
    if prev_index:
        prev = master_data.query_one(
            "SELECT id FROM main_story_data WHERE part_id=? AND episode_index=?",
            (row["part_id"], prev_index))
        if prev and prev["id"] not in cleared:
            return False
    for slot in (1, 2, 3):
        if not _lock_ok(full_state, row[f"lock_type_{slot}"],
                        row[f"lock_value_{slot}_1"], row[f"lock_value_{slot}_2"]):
            return False
    return True


def clear_main_story(full_state: dict, episode_id: int, summary: dict, viewer_id=None):
    """THE reusable main-story first-clear (gate evaluation + one-time reward
    grant), operating on an in-memory full_state -- the caller persists.

    Returns (ok, reward_array): ok False = unknown episode or gate unmet
    (endpoint should refuse 205); ok True with an empty reward_array = it
    was already cleared (success, nothing granted again).
    """
    row = master_data.query_one(
        "SELECT * FROM main_story_data WHERE id=?", (episode_id,))
    if row is None:
        return False, []
    st = _story_state(full_state)
    cleared = st.setdefault("main_cleared", [])
    if episode_id in cleared:
        return True, []
    if not _main_story_unlocked(full_state, row, cleared):
        return False, []
    reward_array = _grant_reward(
        full_state, row["add_reward_category_1"], row["add_reward_id_1"],
        row["add_reward_num_1"], summary, viewer_id)
    cleared.append(episode_id)
    return True, reward_array


# ------------------------------------------- main_story/first_clear
# Capture ground truth (2026-08-16, real server, ops 0013-0015): request
# {episode_id}; response data {main_story_data: {episode_id, state:1},
# reward_array, reward_summary_info, add_music_array: [], release_item_flag:
# 0}. main_story_data has no music-reward columns (see its schema), so
# add_music_array is always empty -- not a gap, just a field this family
# always carries regardless.

@registry.endpoint("main_story/first_clear")
def handle_main_story_first_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    full_state = state_store.get_state(viewer_id) or {}
    summary = shop._empty_summary()
    ok, reward_array = clear_main_story(full_state, episode_id, summary, viewer_id)
    if not ok:
        return _refuse()
    # Six Global songs unlock on a specific main-story episode (see
    # app/concerts.py). main_story_data has no music-reward columns -- which is
    # why the captured add_music_array was empty -- the song is a separate
    # handout keyed off the episode being watched.
    songs = concerts.check_and_grant(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "main_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "add_music_array": [{"music_id": m} for m in songs],
        "release_item_flag": 0,
    })


# ------------------------------------------- story_event/story_clear
# Capture ground truth (2026-08-16, real server, op 0020): request
# {episode_id}; response data {story_data: {episode_id, state:1},
# reward_array, reward_summary_info, new_story_id_list: [], add_music_array:
# []}. Table confirmed by matching the reward exactly: story_event_story_data
# id=123 has add_reward_category_1=90/id=43/num=30, byte-for-byte the
# captured reward. UNLIKE chara/main story it carries a SECOND reward slot
# (add_reward_category_2/id_2/num_2) -- granted too, though the one captured
# episode had it empty so this is untested against a real populated slot.
# Gate is INFERRED, not capture-verified beyond this single episode
# (episode_index_id 1, nothing to chain against): sequential within
# story_event_id by episode_index_id, mirroring chara/main story's own
# within-family sequencing since no contrary evidence exists.

def _story_event_unlocked(row, cleared: list) -> bool:
    if (row["episode_index_id"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM story_event_story_data "
            "WHERE story_event_id=? AND episode_index_id=?",
            (row["story_event_id"], row["episode_index_id"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


@registry.endpoint("story_event/story_clear")
def handle_story_event_story_clear(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM story_event_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("story_event_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        if not _story_event_unlocked(row, cleared):
            return _refuse()
        reward_array = _grant_reward(
            full_state, row["add_reward_category_1"], row["add_reward_id_1"],
            row["add_reward_num_1"], summary, viewer_id)
        reward_array += _grant_reward(
            full_state, row["add_reward_category_2"], row["add_reward_id_2"],
            row["add_reward_num_2"], summary, viewer_id)
        cleared.append(episode_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        "new_story_id_list": [],
        "add_music_array": [],
    })


# ------------------------------------------- extra_story/first_clear
# The fourth story family, alongside chara / main / story_event above.
#
# dump.cs: ExtraStoryFirstClearRequest {episode_id}
#          ExtraStoryFirstClearResponse.CommonResponse
#            {extra_story_data, reward_array, reward_summary_info,
#             add_music_array}
#
# Table [mdb]: story_extra_story_data (16 rows) -- {id, story_extra_id,
# episode_index_id, story_type_1..5 / story_id_1..5, TWO reward slots
# (add_reward_category_1/id_1/num_1 and _2), start_date/notice_end_date/
# end_date as raw epoch ints}. The response field is literally named
# extra_story_data and the table is story_extra_story_data, which is what
# pins this pairing.
#
# Two things differ from the three families above and both are deliberate:
#   * it carries a SECOND reward slot, granted like story_event/story_clear's
#     does (and, as there, untested against a real populated slot -- every row
#     in this build leaves slot 2 empty);
#   * its rows have a real active WINDOW (epoch ints, same convention as
#     story_event_data -- not mission_data's string dates), so an episode
#     outside its window is refused rather than claimable forever.
#
# The gate is INFERRED, not capture-verified: sequential within story_extra_id
# by episode_index_id, exactly mirroring _story_event_unlocked's own
# within-family sequencing, since no contrary evidence exists for either.


def _extra_story_unlocked(row, cleared: list) -> bool:
    if (row["episode_index_id"] or 0) > 1:
        prev = master_data.query_one(
            "SELECT id FROM story_extra_story_data "
            "WHERE story_extra_id=? AND episode_index_id=?",
            (row["story_extra_id"], row["episode_index_id"] - 1))
        if prev and prev["id"] not in cleared:
            return False
    return True


@registry.endpoint("extra_story/first_clear")
def handle_extra_story_first_clear(payload: dict) -> dict:
    """Claim the first-read reward for one extra-story episode. Same contract
    as the three siblings above: the gate is checked against OUR state, the
    reward is one-time, and a repeat claim succeeds with nothing in it."""
    viewer_id = payload["viewer_id"]
    episode_id = payload.get("episode_id")
    row = master_data.query_one(
        "SELECT * FROM story_extra_story_data WHERE id=?", (episode_id,))
    if row is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _story_state(full_state)
    cleared = st.setdefault("extra_cleared", [])
    summary = shop._empty_summary()
    reward_array = []
    if episode_id not in cleared:
        now_ts = patch._servertime()
        if not (row["start_date"] <= now_ts <= row["end_date"]):
            return _refuse()        # outside this episode's live window
        if not _extra_story_unlocked(row, cleared):
            return _refuse()
        for slot in (1, 2):
            if not row[f"add_reward_category_{slot}"]:
                continue
            reward_array += _grant_reward(
                full_state, row[f"add_reward_category_{slot}"],
                row[f"add_reward_id_{slot}"], row[f"add_reward_num_{slot}"],
                summary, viewer_id)
        cleared.append(episode_id)
        # Keep the read-set coherent even if the client's read_info/index
        # report is missed, the same way handle_first_clear does -- an extra
        # story episode can chain up to five story_ids, so all of them count
        # as read.
        read = st.setdefault("read_info", {})
        released = set(read.get("released_episode_data_array") or [])
        for i in range(1, 6):
            sid = row[f"story_id_{i}"]
            if sid:
                released.add(sid)
        read["released_episode_data_array"] = sorted(released)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "extra_story_data": {"episode_id": episode_id, "state": 1},
        "reward_array": reward_array,
        "reward_summary_info": summary,
        # story_extra_story_data has no music-reward column, so this is always
        # empty -- present because the family always carries it, exactly like
        # main_story/first_clear's own add_music_array.
        "add_music_array": [],
    })
