
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from pathlib import Path

from .. import concerts
from .. import region
from .. import state as state_store
from . import collection, stamina, trained_chara

log = logging.getLogger("uma-server")

SEED_PATH = region.seeds_dir() / "load_index_fresh.json"
_CHARA_LIGHT_SHAPE_PATH = region.seeds_dir() / "load_index_fresh_v1_established_account.json"
STATE_KEY = "load_index"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SEEN_ANNOUNCE_KEY = "seen_announce_ids"

_seed_cache: dict | None = None
_chara_light_shape_cache: dict | None = None


def _chara_light_template() -> dict:
    global _chara_light_shape_cache
    if _chara_light_shape_cache is None:
        with open(_CHARA_LIGHT_SHAPE_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _chara_light_shape_cache = doc["data"]["data"]["single_mode_chara_light"]
    return copy.deepcopy(_chara_light_shape_cache)


_STORY_PROGRESS_KEYS = (
    "character_story_data_list", "main_story_data_list",
    "home_story_data_array", "short_episode_data_array",
    "home_poster_data_array", "tutorial_guide_data_array",
    "released_episode_data_array", "talk_gallery_list",
    "home_banner_data_array", "viewed_story_array",
)


def _load_seed() -> dict:
    global _seed_cache
    if _seed_cache is None:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _seed_cache = doc["data"]
        inner = _seed_cache.get("data")
        if isinstance(inner, dict):
            for key in _STORY_PROGRESS_KEYS:
                if key in inner:
                    inner[key] = []
    return copy.deepcopy(_seed_cache)


def _neutralize_identity(seed: dict, viewer_id) -> dict:
    data = seed.get("data") or {}
    user_info = data.get("user_info")
    if isinstance(user_info, dict):
        user_info.update({
            "name": f"Trainer{str(viewer_id)[-4:]}",
            "comment": "",
            "fan": 0,
            "rank_score": 0,
            "best_team_evaluation_point": 0,
            "register_time": datetime.now().strftime(TIME_FORMAT),
            "birth_day": "",
        })
    if "user_birth" in data:
        data["user_birth"] = None
    return seed


def get_or_seed_data(full_state: dict, viewer_id) -> dict:
    return get_or_seed_blob(full_state, viewer_id).setdefault("data", {})


def get_or_seed_blob(full_state: dict, viewer_id) -> dict:
    blob = full_state.get(STATE_KEY)
    if blob is None:
        blob = state_store.load_lazy_key(viewer_id, STATE_KEY)
        if blob is None:
            blob = _neutralize_identity(_load_seed(), viewer_id)
        full_state[STATE_KEY] = blob
    return blob


def _reload_state(viewer_id, previous: dict) -> dict:
    fresh = state_store.get_state(viewer_id)
    if fresh is None:
        return previous
    if dict.__contains__(previous, STATE_KEY) and not dict.__contains__(fresh, STATE_KEY):
        fresh[STATE_KEY] = previous[STATE_KEY]
    return fresh


def apply_derived_user_info(full_state: dict, user_info: dict) -> dict:
    from . import team_stadium
    ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    user_info["best_team_evaluation_point"] = team_stadium.rank_display_point(ts_state)
    from . import shop
    bonus = full_state.get(shop.BONUS_FOLLOW_KEY)
    if bonus:
        user_info["bonus_follow_num"] = bonus
    sale = full_state.get(shop.BONUS_LIMITED_SALE_KEY)
    if sale:
        user_info["bonus_limited_sale_open_num"] = sale
    return user_info


def handle_load_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    blob = get_or_seed_blob(full_state, viewer_id)
    response = state_store.load_lazy_key(viewer_id, STATE_KEY)
    if response is None:
        response = copy.deepcopy(blob)

    user_info = response.get("data", {}).get("user_info")
    if isinstance(user_info, dict) and "update_time" in user_info:
        user_info["update_time"] = datetime.now().strftime(TIME_FORMAT)
        apply_derived_user_info(full_state, user_info)

    try:
        if full_state.get(concerts.MUSIC_LIST_KEY) is None:
            full_state[concerts.MUSIC_LIST_KEY] = copy.deepcopy(
                (blob.get("data") or {}).get("music_list") or [])
        concerts.on_login(full_state)
    except Exception:
        log.exception("concerts: login pass failed for viewer %s", viewer_id)

    state_store.save_state(viewer_id, full_state)

    _overlay_live_data(response, viewer_id, full_state)

    data = response.get("data")
    if isinstance(data, dict):
        from .. import social
        from . import circles
        member = social.member_row(viewer_id)
        circle = social.get_circle(member["circle_id"]) if member else None
        if circle is None:
            data["circle_data"] = {"circle_info": None,
                                   "circle_user": circles.no_circle_user(viewer_id)}
        else:
            data["circle_data"] = {
                "circle_info": {"circle_id": circle["circle_id"],
                                "name": circle["name"]},
                "circle_user": member,
            }

    data = response.get("data")
    if isinstance(data, dict) and "login_bonus_list" in data:
        from . import login_bonus, presents
        box_before = len((state_store.get_state(viewer_id) or {})
                         .get(presents.PRESENT_BOX_KEY, {}).get("presents") or [])
        data["login_bonus_list"] = login_bonus.apply_and_report(viewer_id)
        box_after = len((state_store.get_state(viewer_id) or {})
                        .get(presents.PRESENT_BOX_KEY, {}).get("presents") or [])
        added = box_after - box_before
        if added > 0:
            notifs = response.setdefault("data_headers", {}).setdefault("notifications", {})
            notifs["add_present_num"] = (notifs.get("add_present_num") or 0) + added

    if isinstance(user_info, dict) and isinstance(data, dict) and \
            isinstance(data.get("chara_list"), list):
        user_info["fan"] = sum(c.get("fan") or 0 for c in data["chara_list"])

    badge = data.get("menu_badge_info") if isinstance(data, dict) else None
    if isinstance(badge, dict):
        import time as _time
        from . import presents, missions
        full_state = _reload_state(viewer_id, full_state)
        badge["present_num"] = len(presents._pending(full_state, int(_time.time())))
        badge.update(missions.badge_counts(viewer_id, full_state))
        badge["view_limited_mission_num"] = 0

    if isinstance(data, dict) and "directory_card_num" in data:
        try:
            from . import archive_directory, trained_chara as _tc
            full_state = _reload_state(viewer_id, full_state)
            roster = _tc._get_or_seed_roster(viewer_id)
            data["directory_card_num"] = archive_directory.entry_count(
                archive_directory.pool(full_state, roster))
        except Exception:
            log.exception("directory_card_num computation failed; leaving it at 0")
            data["directory_card_num"] = 0

    if isinstance(data, dict) and "support_user_num" in data:
        from . import single_mode_team
        full_state = _reload_state(viewer_id, full_state)
        data["support_user_num"] = len(
            full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or [])

    if isinstance(data, dict) and "campaign_walking_load_info" in data:
        from . import campaign_walking
        data["campaign_walking_load_info"] = campaign_walking.load_info(viewer_id)
    if isinstance(data, dict):
        from . import map_event
        info = map_event.load_info(viewer_id)
        if info is not None:
            data["map_event_load_info"] = info

    if isinstance(data, dict) and "chara_profile_array" in data:
        from . import bond
        full_state = _reload_state(viewer_id, full_state)
        data["chara_profile_array"] = bond.chara_profile_array(full_state)
        state_store.save_state(viewer_id, full_state)

    if isinstance(data, dict) and "unread_announce_id_array" in data:
        full_state = _reload_state(viewer_id, full_state)
        seen = full_state.setdefault(SEEN_ANNOUNCE_KEY, [])
        ids = data["unread_announce_id_array"] or []
        data["unread_announce_id_array"] = [i for i in ids if i not in seen]
        seen.extend(i for i in ids if i not in seen)
        state_store.save_state(viewer_id, full_state)

    try:
        from . import directory, house_lenders
        full_state = _reload_state(viewer_id, full_state)
        if directory.publish(viewer_id, full_state):
            state_store.save_state(viewer_id, full_state)
        house_lenders.ensure_accounts()
        house_lenders.ensure_followed(viewer_id)
    except Exception:
        log.exception("load/index: failed to publish directory card for %s", viewer_id)
    return response


def _overlay_live_data(response: dict, viewer_id, full_state: dict | None = None) -> None:
    data = response.get("data")
    if not isinstance(data, dict):
        return

    if isinstance(data.get("trained_chara"), list):
        roster = trained_chara._get_or_seed_roster(viewer_id)
        if roster:
            data["trained_chara"] = json.loads(json.dumps(roster))
            ids = [c["trained_chara_id"] for c in roster]
            for arr_key in ("trained_chara_favorite_array", "team_data_array"):
                arr = data.get(arr_key)
                if not isinstance(arr, list):
                    continue
                unused = list(ids)
                for entry in arr:
                    if not isinstance(entry, dict) or "trained_chara_id" not in entry:
                        continue
                    if not entry.get("trained_chara_id"):
                        continue
                    if unused:
                        entry["trained_chara_id"] = unused.pop(0)
                    else:
                        entry["trained_chara_id"] = 0
                        if "running_style" in entry:
                            entry["running_style"] = 0

    _seeds = {k: data[k] for k in collection.DYNAMIC_CONTAINERS if k in data}
    for data_key, value in collection.get_or_seed_many(viewer_id, _seeds).items():
        data[data_key] = copy.deepcopy(value)

    if isinstance(data.get("card_list"), list):
        from . import cards as _cards
        _st = state_store.get_state(viewer_id)
        _owned = (_st or {}).get(collection.CARD_LIST_KEY)
        if isinstance(_owned, list) and _cards.clamp_card_talents(_owned):
            state_store.save_state(viewer_id, _st)
            log.info("load/index: clamped over-cap potential for %s", viewer_id)
            _cards.clamp_card_talents(data["card_list"])

    if isinstance(data.get("card_list"), list):
        from .. import master_data
        known = {r[0] for r in master_data.query("SELECT id FROM card_data")}
        hidden = [c.get("card_id") for c in data["card_list"] if c.get("card_id") not in known]
        if hidden:
            log.info("load/index: hiding %d cards missing from master: %s", len(hidden), hidden)
            data["card_list"] = [c for c in data["card_list"] if c.get("card_id") in known]

    _shared = state_store.get_state(viewer_id) or {}
    from . import training_challenge
    training_challenge.overlay_login(data, _shared)
    from . import story_event
    story_event.overlay_login(data, _shared, viewer_id)
    from . import factor_research
    expired = factor_research.expire_reports(_shared)
    if region.CURRENT_REGION.get() == "jp" or factor_research.current() is not None:
        data["factor_research_load_info"] = factor_research.load_info(_shared)
        expired = True
    if expired:
        data["item_list"] = copy.deepcopy(_shared.get("item_list_state") or [])
    stamina.refresh(_shared)
    from . import daily_races
    if daily_races.refresh_tickets(_shared) and isinstance(data.get("item_list"), list):
        _owned = {i.get("item_id"): i.get("number")
                  for i in _shared.get("item_list_state") or []}
        for _tid in (daily_races._TICKET_ITEM_ID, daily_races._LEGEND_TICKET_ITEM_ID):
            _entry = next((i for i in data["item_list"] if i.get("item_id") == _tid), None)
            if _entry is not None:
                _entry["number"] = _owned.get(_tid, 0)
            elif _owned.get(_tid):
                data["item_list"].append({"item_id": _tid, "number": _owned[_tid]})
    state_store.save_state(viewer_id, _shared)
    data["tp_info"] = stamina.tp_info(_shared)
    data["rp_info"] = stamina.rp_info(_shared)

    from . import payment
    _birth = payment.user_birth(_shared)
    if _birth:
        data["user_birth"] = _birth["user_birth"]
        data["optin_user_birth"] = _birth["optin_user_birth"]

    if full_state and STATE_KEY in full_state and STATE_KEY not in _shared:
        _shared[STATE_KEY] = full_state[STATE_KEY]

    if "single_mode_chara_light" in data:
        from . import single_mode_team
        sm_state = _shared
        career = sm_state.get(single_mode_team.STATE_KEY)
        chara_info = career.get("data", {}).get("chara_info") if isinstance(career, dict) else None
        data["single_mode_chara_light"] = (
            _career_chara_light(_chara_light_template(), chara_info)
            if chara_info else None
        )

    from . import daily_races
    drs = daily_races._daily_state(_shared)
    common_define = data.get("common_define")
    if isinstance(common_define, dict):
        common_define["daily_race_ticket_max_num"] = daily_races.ticket_cap()
    info = data.get("daily_race_playing_info")
    if isinstance(info, dict) and isinstance(info.get("daily_race_record_array"), list):
        for rec in info["daily_race_record_array"]:
            if not isinstance(rec, dict):
                continue
            live = (drs.get("daily") or {}).get(str(rec.get("daily_race_id"))) or {}
            rec["is_played"] = 1 if live.get("is_played") else 0
            rec["is_cleared"] = 1 if (live.get("is_cleared")
                                      or rec.get("daily_race_id") in drs["daily_cleared_ever"]) else 0
        info["state"] = 0
        info["trained_chara_id"] = 0
    legend = data.get("daily_legend_race_playing_info")
    if isinstance(legend, dict):
        legend["state"] = 0
        legend["trained_chara_id"] = 0
        _today = drs.get("legend") or {}
        _ever = {int(x) for x in drs["legend_cleared_ever"]}
        legend["daily_legend_race_record"] = [
            {"daily_legend_race_id": rid,
             "is_played": 1 if _today.get(str(rid), {}).get("is_played") else 0,
             "is_cleared": 1 if (_today.get(str(rid), {}).get("is_cleared") or rid in _ever) else 0}
            for rid in sorted({int(k) for k in _today} | _ever)]
        legend["new_flag"] = 0

    event_legend = data.get("legend_race_playing_info")
    if isinstance(event_legend, dict):
        from . import legend_race
        try:
            open_races = legend_race.active_races()
            st = legend_race._state(_shared)
            if open_races:
                event_legend.update(state=0, trained_chara_id=0, next_group_id=None,
                                    group_id=open_races[0]["group_id"] if open_races else 0,
                                    legend_race_record_array=[
                                        {k: v for k, v in legend_race._record(r, st).items() if k != "boss_data"}
                                        for r in open_races])
        except Exception:
            log.exception("load/index: legend_race_playing_info")

    if isinstance(data.get("challenge_match_load_info"), dict):
        from . import challenge_match
        try:
            data["challenge_match_load_info"] = challenge_match.load_info(_shared)
        except Exception:
            log.exception("load/index: challenge_match_load_info")

    from . import team_building
    try:
        tb = team_building.load_info(viewer_id, _shared)
        if tb:
            data["team_building_load_info"] = tb
            state_store.save_state(viewer_id, _shared)
    except Exception:
        log.exception("load/index: team_building_load_info")

    from . import champions
    try:
        cm_info = champions.load_info(viewer_id, _shared)
    except Exception:
        log.exception("load/index: champions_info")
        cm_info = None
    if cm_info:
        data["champions_info"] = cm_info
        if champions.CM.options().get("any_team_rank") and isinstance(common_define, dict):
            common_define["need_team_rank_play_champions"] = 0
        state_store.save_state(viewer_id, _shared)

    from . import heroes
    try:
        h_info = heroes.load_info(viewer_id, _shared)
    except Exception:
        log.exception("load/index: heroes_load_info")
        h_info = None
    if h_info:
        data["heroes_load_info"] = h_info
        state_store.save_state(viewer_id, _shared)

    if isinstance(data.get("border_line"), dict):
        data["border_line"] = []

    from .jukebox_requests import _jukebox_state
    jukebox_st = _jukebox_state(_shared, viewer_id)
    jukebox_history = jukebox_st.get("history") or []
    data["jukebox_request_history"] = jukebox_history[-1] if jukebox_history else None

    from . import stories
    story_st = stories._story_state(_shared)
    if isinstance(data.get("main_story_data_list"), list):
        data["main_story_data_list"] = [
            {"episode_id": eid, "state": 1}
            for eid in sorted(set(story_st.get("main_cleared") or []))]
    if isinstance(data.get("character_story_data_list"), list):
        data["character_story_data_list"] = [
            {"episode_id": eid, "state": 1}
            for eid in sorted(set(story_st.get("chara_cleared") or []))]

    read = story_st.get("read_info") or {}
    for state_key, load_key in stories._SEED_KEYS.items():
        if not isinstance(data.get(load_key), list):
            continue
        ids = sorted(set(read.get(state_key) or []))
        data[load_key] = (
            [{"home_story_trigger_id": i, "new_flag": 0} for i in ids]
            if load_key == "talk_gallery_list" else [{"id": i} for i in ids])

    extra = data.get("extra_story_data_list")
    if isinstance(extra, dict):
        for load_key, cleared_key in (("story_event", "story_event_cleared"),
                                      ("story_extra", "extra_cleared")):
            if isinstance(extra.get(load_key), list):
                extra[load_key] = [
                    {"episode_id": eid, "state": 1}
                    for eid in sorted(set(story_st.get(cleared_key) or []))]
    if isinstance(data.get("story_favorite_array"), list):
        from .user_profile import STORY_FAVORITE_KEY
        favs = _shared.get(STORY_FAVORITE_KEY)
        data["story_favorite_array"] = copy.deepcopy(favs if isinstance(favs, list) else [])
    if isinstance(data.get("event_data_array"), list):
        from .. import event_gallery
        data["event_data_array"] = event_gallery.wire_array(_shared)

    from .team_stadium import TEAM_STADIUM_STATE_KEY
    team_st = _shared.get(TEAM_STADIUM_STATE_KEY)
    if isinstance(team_st, dict):
        data["team_data_array"] = copy.deepcopy(team_st.get("team_data_array") or [])


def _career_chara_light(template: dict, chara_info: dict) -> dict:
    scl = copy.deepcopy(template)
    for key in list(scl.keys()):
        if key in chara_info:
            scl[key] = copy.deepcopy(chara_info[key])
    if not scl.get("playing_state"):
        scl["playing_state"] = 1
    return scl
