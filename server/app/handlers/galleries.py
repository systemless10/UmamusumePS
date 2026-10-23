
from __future__ import annotations

import copy
import logging

from .. import master_data, state as state_store
from . import registry
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

GALLERY_VIEW_KEY = "gallery_view_state"
PHOTO_ACTIVITY_KEY = "photo_activity_type"
THEATER_PRESET_KEY = "live_theater_presets"
SERIAL_CODE_KEY = "serial_codes_redeemed"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


@registry.endpoint("gallery/play_event")
def handle_play_event(payload: dict) -> dict:
    story_id = payload.get("story_id")
    if not story_id:
        return _refuse()
    row = master_data.query_one(
        "SELECT id, story_id, card_chara_id, support_chara_id, support_card_id, "
        "past_race_id FROM single_mode_story_data WHERE story_id = ?", (story_id,))
    if row is None:
        log.info("gallery/play_event: no single_mode_story_data row for %s", story_id)
        return _refuse()
    return _ok({
        "event_id": row["id"] or 0,
        "chara_id": (row["card_chara_id"] or row["support_chara_id"]) or 0,
        "story_id": int(story_id),
        "event_contents_info": {
            "support_card_id": row["support_card_id"] or 0,
            "choice_array": [],
            "month": 0,
            "race_id": row["past_race_id"] or 0,
        },
    })


@registry.endpoint("gallery/save_gallery_data")
def handle_save_gallery_data(payload: dict) -> dict:
    from .. import event_gallery
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[GALLERY_VIEW_KEY] = {"saved": True}
    event_gallery.mark_viewed(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("talk_gallery/index")
def handle_talk_gallery_index(payload: dict) -> dict:
    return _ok({})


@registry.endpoint("photo/set_activity")
def handle_set_activity(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[PHOTO_ACTIVITY_KEY] = int(payload.get("action_type") or 0)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


_GALLERY_KEY_ITEM = 190
_GALLERY_TYPE_HOME_STORY = 1


def _gallery_key_cost(gallery_type: int, story_id) -> int | None:
    if gallery_type == _GALLERY_TYPE_HOME_STORY:
        row = master_data.query_one(
            "SELECT available_gallery_key FROM home_story_trigger WHERE id = ?", (story_id,))
    else:
        row = master_data.query_one(
            "SELECT available_gallery_key FROM single_mode_story_data "
            "WHERE story_id = ?", (story_id,))
    return None if row is None else int(row["available_gallery_key"] or 0)


@registry.endpoint("note/use_gallery_key")
def handle_use_gallery_key(payload: dict) -> dict:
    from .. import event_gallery
    from . import stories
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    gallery_type = int(payload.get("gallery_type") or 0)
    story_ids = [int(s) for s in (payload.get("story_id_array") or []) if s]
    if not story_ids:
        return _refuse()

    cost = 0
    for sid in story_ids:
        price = _gallery_key_cost(gallery_type, sid)
        if price is None:
            log.info("note/use_gallery_key: type %s has no entry %s", gallery_type, sid)
            return _refuse()
        cost += price

    items = full_state.setdefault("item_list_state", [])
    key_item = next((i for i in items if i.get("item_id") == _GALLERY_KEY_ITEM), None)
    have = (key_item or {}).get("number") or 0
    if have < cost:
        log.info("note/use_gallery_key: %s keys held, %s needed", have, cost)
        return _refuse()
    if key_item is not None:
        key_item["number"] = have - cost

    events, home_stories = [], []
    if gallery_type == _GALLERY_TYPE_HOME_STORY:
        stories.add_home_stories(full_state, story_ids)
        home_stories = [{"id": sid} for sid in story_ids]
    else:
        for sid in story_ids:
            events += event_gallery.record(full_state, sid)
    state_store.save_state(viewer_id, full_state)
    log.info("note/use_gallery_key: unlocked %s entr%s for %s key(s), %s left",
             len(story_ids), "y" if len(story_ids) == 1 else "ies", cost, have - cost)
    return _ok({"item_info_array": [{"item_id": _GALLERY_KEY_ITEM, "number": have - cost}],
                "event_data_array": events,
                "home_story_data_array": home_stories})


@registry.endpoint("live_theater/performers_preset_index")
def handle_performers_preset_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    return _ok({"preset_array": copy.deepcopy(full_state.get(THEATER_PRESET_KEY) or [])})


@registry.endpoint("live_theater/performers_preset_update")
def handle_performers_preset_update(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    incoming = payload.get("preset") or {}
    presets = full_state.setdefault(THEATER_PRESET_KEY, [])
    pid = int(incoming.get("preset_id") or 0)
    entry = next((p for p in presets if p.get("preset_id") == pid), None) if pid else None
    if entry is None:
        pid = max((p.get("preset_id") or 0) for p in presets) + 1 if presets else 1
        entry = {"preset_id": pid}
        presets.append(entry)
    entry["preset_name"] = str(incoming.get("preset_name") or "")[:32]
    entry["member_info_array"] = incoming.get("member_info_array") or []
    state_store.save_state(viewer_id, full_state)
    log.info("live_theater: saved performer preset %s (%s member(s))",
             pid, len(entry["member_info_array"]))
    return _ok({})


@registry.endpoint("serial_code/register")
def handle_serial_code_register(payload: dict) -> dict:
    from .. import config
    from . import presents
    viewer_id = payload["viewer_id"]
    code = str(payload.get("serial_code") or "").strip()
    catalogue = config.get("serial_codes") or {}
    entry = catalogue.get(code) if isinstance(catalogue, dict) else None
    if not code or not isinstance(entry, dict):
        log.info("serial_code/register: no campaign for code %r", code)
        return _refuse(ResultCode.SERIAL_CODE_TIME_LIMIT_OVER)

    full_state = state_store.get_state(viewer_id) or {}
    used = full_state.setdefault(SERIAL_CODE_KEY, [])
    if code in used:
        log.info("serial_code/register: %r already redeemed by %s", code, viewer_id)
        return _refuse(ResultCode.SERIAL_CODE_LIMIT_OVER)

    rewards = []
    for r in entry.get("rewards") or []:
        item_type = int(r.get("item_type") or 0)
        item_id = int(r.get("item_id") or 0)
        item_num = int(r.get("item_num") or 0)
        if not item_id or item_num <= 0:
            continue
        presents.send(full_state, item_type, item_id, item_num,
                      message=entry.get("name") or "Serial code reward")
        rewards.append({"item_type": item_type, "item_id": item_id,
                        "item_num": item_num})
    used.append(code)
    state_store.save_state(viewer_id, full_state)
    log.info("serial_code/register: %r redeemed by %s (%s reward(s) mailed)",
             code, viewer_id, len(rewards))
    return _ok({"reward_array": rewards, "campaign_name": entry.get("name") or ""})


@registry.endpoint("support_card_ranking/get_ranking")
def handle_support_card_ranking(payload: dict) -> dict:
    from .training_challenge import support_ranking
    return support_ranking(payload)


@registry.endpoint("user/get_profile_info")
def handle_get_profile_info(payload: dict) -> dict:
    from . import collection, directory, note_archive, trained_chara
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    note = note_archive._note_state(full_state)
    note_archive._absorb_sync(note, payload, full_state)
    state_store.save_state(viewer_id, full_state)

    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    genuine = [c for c in roster
               if trained_chara.PLAYER_CAREER_ID_BASE
               <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]
    scores = [c.get("rank_score") or 0 for c in genuine]
    card = directory.cards_for([str(viewer_id)]).get(str(viewer_id)) or {}
    story_st = full_state.get("story_state") or {}

    return _ok({
        "voice_num": len(note.get("voices") or {}),
        "act_num": len(full_state.get(collection.SUPPORT_CARD_KEY) or []),
        "good_end_num": len(genuine),
        "team_stadium_win_count": card.get("team_stadium_win_count") or 0,
        "single_mode_play_count": len(genuine),
        "rank_score": card.get("rank_score") or 0,
        "chara_event_num": 0,
        "support_event_num": 0,
        "scenario_event_num": 0,
        "home_event_num": len(note_archive.home_story_ids(full_state)),
        "main_story_num": len(story_st.get("main_cleared") or []),
        "chara_story_num": len(story_st.get("chara_cleared") or []),
        "highest_rank_score": max(scores) if scores else 0,
    })
