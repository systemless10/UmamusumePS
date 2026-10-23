
from __future__ import annotations

import time

from .. import epithets
from .. import master_data
from .. import state as state_store
from . import archive_directory, bond, collection, registry, trained_chara
from ..result_codes import ResultCode, refuse

NOTE_STATE_KEY = "note_state"

_ALBUM_UNIT = 650
_STORY_UNIT = 400
_ACT_UNIT = 750
_VOICE_UNIT = 75
_NICKNAME_UNIT = 100
_GALLERY_UNIT = 125


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _note_state(full_state: dict) -> dict:
    st = full_state.get(NOTE_STATE_KEY)
    if not isinstance(st, dict):
        st = {
            "voices": {},
            "home_stories": [],
            "granted_rank": 1,
            "level": 1,
            "scenario_high_scores": {},
        }
        full_state[NOTE_STATE_KEY] = st
    return st


def _absorb_sync(note: dict, payload: dict, full_state: dict | None = None) -> None:
    voices = note.setdefault("voices", {})
    now = _now()
    for v in payload.get("add_voice_data_array") or []:
        if isinstance(v, dict) and v.get("chara_id") and v.get("data_id"):
            voices.setdefault(f"{v['chara_id']}:{v['data_id']}", now)
    stories = note.setdefault("home_stories", [])
    for s in payload.get("add_home_story_data_array") or []:
        sid = s.get("id") if isinstance(s, dict) else s
        if isinstance(sid, int) and sid not in stories:
            stories.append(sid)
    if full_state is not None:
        from . import stories as _stories
        _stories.add_home_stories(full_state, (
            s.get("id") if isinstance(s, dict) else s
            for s in payload.get("add_home_story_data_array") or []))


def home_story_ids(full_state: dict) -> set:
    from . import stories
    read = (stories._story_state(full_state).get("read_info") or {})
    ids = set(read.get("home_story_data_array") or [])
    ids.update(i for i in (_note_state(full_state).get("home_stories") or [])
               if isinstance(i, int))
    return ids


def _score_summary(full_state: dict, note: dict, roster: list) -> dict:
    albums = len(full_state.get(collection.CARD_LIST_KEY) or [])
    acts = len(full_state.get(collection.SUPPORT_CARD_KEY) or [])
    cloths = len(full_state.get("cloth_list_state") or [])
    story_st = full_state.get("story_state") or {}
    stories = (len(story_st.get("chara_cleared") or [])
               + len(story_st.get("main_cleared") or []))
    voices = len(note.get("voices") or {})
    nicknames: set = set()
    for rec in roster:
        nicknames.update(n for n in (rec.get("nickname_id_array") or [])
                         if isinstance(n, int))
    directory_card = archive_directory.card_score(
        archive_directory.pool(full_state, roster))
    return {
        "directory_card": directory_card,
        "album": {"score": albums * _ALBUM_UNIT, "number": albums},
        "story": {"score": stories * _STORY_UNIT, "number": stories},
        "act": {"score": acts * _ACT_UNIT, "number": acts},
        "voice": {"score": voices * _VOICE_UNIT, "number": voices},
        "nickname": {"score": len(nicknames) * _NICKNAME_UNIT, "number": len(nicknames)},
        "gallery": {"score": (cloths + 1) * _GALLERY_UNIT, "number": cloths},
    }


def _total_score(summary: dict) -> int:
    return summary["directory_card"] + sum(
        summary[k]["score"] for k in ("album", "story", "act", "voice",
                                      "nickname", "gallery"))


def _rank_for(score: int) -> int:
    row = master_data.query_one(
        "SELECT MAX(id) AS r FROM directory WHERE required_point <= ?", (score,))
    return (row["r"] if row and row["r"] else None) or 1


def _pending_rank_rewards(note: dict, rank: int) -> list:
    granted = note.get("granted_rank") or 1
    if rank <= granted:
        return []
    totals: dict = {}
    for row in master_data.query(
            "SELECT * FROM directory WHERE id > ? AND id <= ? ORDER BY id",
            (granted, rank)):
        cat = row["item_category_1"] or 0
        iid = row["item_id_1"] or 0
        num = row["item_num_1"] or 0
        if num:
            totals[(cat, iid)] = totals.get((cat, iid), 0) + num
    return [(cat, iid, num) for (cat, iid), num in totals.items()]


def _grant_rank_rewards(viewer_id, note: dict, rank: int) -> list:
    from . import presents
    rewards = _pending_rank_rewards(note, rank)
    if not rewards:
        return []
    for cat, iid, num in rewards:
        presents.admin_send(viewer_id, item_category=cat, item_id=iid, item_num=num,
                            message_id=42)
    note["granted_rank"] = rank
    return [{"item_type": cat, "item_id": iid, "item_num": num} for cat, iid, num in rewards]


_release_cache: list | None = None


def _release_card_array() -> list:
    global _release_cache
    if _release_cache is None:
        _release_cache = [r["id"] for r in master_data.query(
            "SELECT id FROM card_data WHERE id < 9000000 ORDER BY id")]
    return list(_release_cache)


def _directory_card_array(viewer_id, full_state: dict, roster: list, card_id=None) -> list:
    return archive_directory.card_array(
        viewer_id, archive_directory.pool(full_state, roster), card_id)


@registry.endpoint("note/get_nickname_data")
def handle_get_nickname_data(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    chara_id = payload.get("chara_id") or 0
    full_state = state_store.get_state(viewer_id) or {}
    return _ok({"nickname_id_array": epithets.owned_ids(full_state, chara_id)})


@registry.endpoint("note/get_scenario_record")
def handle_get_scenario_record(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    wanted = payload.get("scenario_id") or 0
    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    horses = archive_directory.pool(full_state, roster)
    scenarios = [wanted] if wanted else sorted(
        {r.get("scenario_id") for r in horses if r.get("scenario_id")})
    records = []
    for sid in scenarios:
        records.extend(archive_directory.scenario_top(viewer_id, horses, sid))
    return _ok({"scenario_record_array": records})


@registry.endpoint("note/index")
def handle_note_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    _absorb_sync(note, payload, full_state)

    before_level = note.get("level") or 1
    summary = _score_summary(full_state, note, roster)
    highs = archive_directory.scenario_highs(
        full_state, archive_directory.pool(full_state, roster))
    rank_score = _total_score(summary)
    rank = _rank_for(rank_score)

    note["level"] = rank
    state_store.save_state(viewer_id, full_state)

    rewards = _grant_rank_rewards(viewer_id, note, rank)
    if rewards:
        full_state = state_store.get_state(viewer_id) or {}
        _note_state(full_state)["granted_rank"] = rank
        state_store.save_state(viewer_id, full_state)

    return _ok({
        "reward_info_array": rewards,
        "before_directory_level": before_level,
        "rank_score": rank_score,
        "release_card_array": _release_card_array(),
        "score_summary": summary,
        "scenario_record_highest_score_array": [
            {"scenario_id": sid, "highest_rank_score": score}
            for sid, score in sorted(highs.items())],
        "directory_card_array": (_directory_card_array(viewer_id, full_state, roster)
                                 if payload.get("request_directory_card") else []),
    })


@registry.endpoint("note/save_voice")
def handle_save_voice(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    _absorb_sync(note, payload, full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": 0, "practice_race_id": 0})


def _voice_rows(note: dict, chara_id: int) -> list:
    now = _now()
    rows = []
    prefix = f"{chara_id}:"
    voices = note.get("voices") or {}
    for key, when in voices.items():
        if not key.startswith(prefix):
            continue
        try:
            data_id = int(key.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        if not isinstance(when, str):
            when = voices[key] = now
        rows.append({"chara_id": chara_id, "data_id": data_id,
                     "create_time": when,
                     "new_flag": 0})
    rows.sort(key=lambda r: r["data_id"])
    return rows


def _chara_row(full_state: dict, chara_id: int) -> dict:
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if not isinstance(charas, list):
        return {}
    return next((c for c in charas
                 if isinstance(c, dict) and c.get("chara_id") == chara_id), {})


def _account_profile_entries(full_state: dict) -> list:
    charas = full_state.get(collection.CHARA_LIST_KEY)
    out = []
    for entry in (charas if isinstance(charas, list) else []):
        if isinstance(entry, dict) and entry.get("chara_id"):
            out += bond.new_profile_entries(full_state, entry["chara_id"])
    return out


@registry.endpoint("note/get_new_chara_data")
def handle_get_new_chara_data(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    chara_id = int(payload.get("chara_id") or 0)
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    _absorb_sync(note, payload, full_state)

    chara = _chara_row(full_state, chara_id)
    voice_rows = _voice_rows(note, chara_id)
    profiles = (bond.new_profile_entries(full_state, chara_id) if chara else [])

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "dress_id": chara.get("dress_id") or 2,
        "mini_dress_id": chara.get("mini_dress_id") or 2,
        "voice_data_array": voice_rows,
        "valentine_special_data_array": [],
        "update_user_chara_array": [],
        "new_chara_profile_array": profiles,
    })


@registry.endpoint("note/trainer_note")
def handle_trainer_note(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    profiles = _account_profile_entries(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({"update_user_chara_array": [], "new_chara_profile_array": profiles})


@registry.endpoint("note/save_note_data")
def handle_save_note_data(payload: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}}


@registry.endpoint("note/get_directory_card")
def handle_get_directory_card(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    card_id = payload.get("card_id")
    if not isinstance(card_id, int) or isinstance(card_id, bool):
        return _refuse()
    if not master_data.query_one("SELECT id FROM card_data WHERE id=?", (card_id,)):
        return _refuse()
    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    cards = _directory_card_array(viewer_id, full_state, roster, card_id)
    return _ok({"directory_card_array": cards})


@registry.endpoint("note/save_chara_data")
def handle_save_chara_data(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    chara_id = payload.get("chara_id")
    if not isinstance(chara_id, int) or isinstance(chara_id, bool):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    chara = _chara_row(full_state, chara_id)
    if not chara:
        return _refuse()

    for field in ("dress_id", "mini_dress_id"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return _refuse()
        if value != 2:
            row = master_data.query_one(
                "SELECT chara_id, general_purpose FROM dress_data WHERE id=?",
                (value,))
            if row is None or not (row["general_purpose"]
                                   or row["chara_id"] == chara_id):
                return _refuse()
        chara[field] = value

    state_store.save_state(viewer_id, full_state)
    return _ok({})
