
from __future__ import annotations

import copy
import functools
import json
import logging
import random

from .. import region, social
from .. import state as state_store
from . import directory, registry, trained_chara
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

STATE_KEY = "practice_race_lobby"

_SAVED_RACE_LIMITS = {"global": 5, "jp": 20}


def saved_race_limit() -> int:
    return _SAVED_RACE_LIMITS[region.CURRENT_REGION.get()]


PARTNER_ID_LIMIT = 5
PARTNER_LIMIT = 30
RECOMMEND_SIZE = 10

_OK_STATE = 1
_FAIL_STATE = 0


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _blank() -> dict:
    return {"next_race_id": 1, "next_preset_id": 1, "saved": [], "pending": None,
            "presets": [], "partners": [], "partner_ids": {}, "used_history": []}


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    seed_presets(st, "practice_race")
    return st


@functools.lru_cache(maxsize=4)
def _preset_defaults(path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {kind: (int(n), (doc.get("preset_name") or {}).get(kind) or "")
            for kind, n in (doc.get("preset_slots") or {}).items()}


def seed_presets(container: dict, kind: str) -> None:
    n, pattern = _preset_defaults(region.seeds_dir() / "exhibition.json").get(kind, (0, ""))
    have = {p["preset_id"] for p in container["presets"]}
    for i in range(1, n + 1):
        if i not in have:
            container["presets"].append({"preset_id": i, "name": pattern.format(n=i),
                                         "preset_chara_array": []})
    container["presets"].sort(key=lambda p: p["preset_id"])
    container["next_preset_id"] = max(int(container.get("next_preset_id") or 1), n + 1)


def _load(payload: dict):
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    return viewer_id, full_state, state(full_state)


def remember_race(viewer_id, request: dict, response_data: dict) -> int:
    full_state = state_store.get_state(viewer_id) or {}
    st = state(full_state)
    race_id = int(st["next_race_id"])
    st["next_race_id"] = race_id + 1
    entries = request.get("entry_chara_array") or []
    info = response_data.get("race_result_info") or {}
    horses = info.get("race_horse_data_array") or []
    st["pending"] = {
        "practice_race_id": race_id,
        "race_instance_id": info.get("race_instance_id") or request.get("race_instance_id") or 0,
        "entry_num": len(horses) or int(request.get("entry_num") or len(entries) or 0),
        "user_entry_num": len(entries),
        "is_favorite": 0,
        "save_time": social.now(),
        "before_season": int(request.get("season") or 0),
        "before_weather": int(request.get("weather") or 0),
        "before_ground_condition": int(request.get("ground_condition") or 0),
        "before_motivation": int(request.get("motivation") or 0),
        "payload": {
            "trained_chara_array": response_data.get("trained_chara_array") or [],
            "race_result_info": response_data.get("race_result_info") or {},
            "entry_info_array": response_data.get("entry_info_array") or [],
            "practice_partner_owner_info_array":
                response_data.get("practice_partner_owner_info_array") or [],
        },
    }
    _record_partner_use(st, viewer_id, entries)
    state_store.save_state(viewer_id, full_state)
    return race_id


def _meta(entry: dict) -> dict:
    return {"practice_race_id": entry["practice_race_id"],
            "race_instance_id": entry["race_instance_id"],
            "entry_num": entry["entry_num"],
            "user_entry_num": entry["user_entry_num"],
            "is_favorite": entry["is_favorite"],
            "save_time": entry["save_time"]}


@registry.endpoint("practice_race/index")
def handle_index(payload: dict) -> dict:
    _, _, st = _load(payload)
    pending = st.get("pending")
    if isinstance(pending, dict):
        return _ok({"state": _OK_STATE,
                    "practice_race_id": pending["practice_race_id"]})
    return _ok({"state": 0, "practice_race_id": 0})


@registry.endpoint("practice_race/get_saved_race_list")
def handle_get_saved_race_list(payload: dict) -> dict:
    _, _, st = _load(payload)
    return _ok({"saved_race_array": [_meta(e) for e in st["saved"]]})


@registry.endpoint("practice_race/race_end")
def handle_race_end(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    pending = st.get("pending")
    if not isinstance(pending, dict):
        log.warning("practice_race/race_end: nothing pending to save")
        return _ok({"state": _FAIL_STATE})

    st["pending"] = None
    if not int(payload.get("is_save") or 0):
        state_store.save_state(viewer_id, full_state)
        return _ok({"state": _FAIL_STATE})

    overwrite = int(payload.get("overwrite_race_id") or 0)
    saved = st["saved"]
    slot = next((i for i, e in enumerate(saved)
                 if e["practice_race_id"] == overwrite), None) if overwrite else None
    if slot is not None:
        saved[slot] = pending
    else:
        saved.append(pending)
        limit = saved_race_limit()
        while len(saved) > limit:
            victim = next((i for i, e in enumerate(saved) if not e["is_favorite"]), None)
            if victim is None:
                victim = 0
            dropped = saved.pop(victim)
            log.info("practice_race: shelf full (%s), dropped race %s",
                     limit, dropped["practice_race_id"])
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": _OK_STATE})


@registry.endpoint("practice_race/delete_race")
def handle_delete_race(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    race_id = int(payload.get("practice_race_id") or 0)
    before = len(st["saved"])
    st["saved"] = [e for e in st["saved"] if e["practice_race_id"] != race_id]
    if len(st["saved"]) == before:
        return _ok({"state": _FAIL_STATE})
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": _OK_STATE})


@registry.endpoint("practice_race/change_favorite_race")
def handle_change_favorite_race(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    race_id = int(payload.get("practice_race_id") or 0)
    entry = next((e for e in st["saved"] if e["practice_race_id"] == race_id), None)
    if entry is None:
        return _refuse()
    entry["is_favorite"] = 1 if int(payload.get("is_favorite") or 0) else 0
    state_store.save_state(viewer_id, full_state)
    return _ok({"practice_race_id": race_id, "is_favorite": entry["is_favorite"]})


@registry.endpoint("practice_race/race_replay")
def handle_race_replay(payload: dict) -> dict:
    _, _, st = _load(payload)
    race_id = int(payload.get("practice_race_id") or 0)
    entry = next((e for e in st["saved"] if e["practice_race_id"] == race_id), None)
    if entry is None and isinstance(st.get("pending"), dict) \
            and st["pending"]["practice_race_id"] == race_id:
        entry = st["pending"]
    if entry is None:
        return _refuse()
    data = dict(entry["payload"])
    for key in ("before_season", "before_weather", "before_ground_condition",
                "before_motivation"):
        data[key] = entry.get(key, 0)
    return _ok(data)


def _preset_wire(preset: dict) -> dict:
    return {"preset_id": preset["preset_id"], "name": preset.get("name") or "",
            "preset_chara_array": preset.get("preset_chara_array") or []}


@registry.endpoint("practice_race/get_preset_array")
def handle_get_preset_array(payload: dict) -> dict:
    _, _, st = _load(payload)
    return _ok({"preset_info_array": [_preset_wire(p) for p in st["presets"]]})


@registry.endpoint("practice_race/save_preset")
def handle_save_preset(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    mine = {c.get("trained_chara_id") for c in roster}
    borrowed = {p["chara"].get("trained_chara_id") for p in st["partners"]}

    excluded = False
    out = []
    for incoming in payload.get("preset_info_array") or []:
        charas = incoming.get("preset_chara_array") or []
        for entry in charas:
            tid = entry.get("trained_chara_id")
            if tid and tid not in mine and tid not in borrowed:
                excluded = True
        pid = int(incoming.get("preset_id") or 0)
        existing = next((p for p in st["presets"] if p["preset_id"] == pid), None) \
            if pid else None
        if existing is None:
            pid = int(st["next_preset_id"])
            st["next_preset_id"] = pid + 1
            existing = {"preset_id": pid, "name": "", "preset_chara_array": []}
            st["presets"].append(existing)
        existing["preset_chara_array"] = charas
        out.append(existing)

    state_store.save_state(viewer_id, full_state)
    return _ok({"add_partner_info_array": [copy.deepcopy(p["chara"])
                                           for p in st["partners"]],
                "practice_partner_owner_info_array": [copy.deepcopy(p["owner"])
                                                      for p in st["partners"]],
                "preset_info_array": [_preset_wire(p) for p in st["presets"]],
                "is_excluded_flag": 1 if excluded else 0})


@registry.endpoint("practice_race/change_preset_name")
def handle_change_preset_name(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    pid = int(payload.get("preset_id") or 0)
    preset = next((p for p in st["presets"] if p["preset_id"] == pid), None)
    if preset is None:
        return _refuse()
    preset["name"] = str(payload.get("name") or "")[:32]
    state_store.save_state(viewer_id, full_state)
    return _ok({"preset_info": _preset_wire(preset)})


def _owner_info(me, owner_viewer_id, chara: dict, owner_name: str = "") -> dict:
    tid = chara.get("trained_chara_id") or 0
    return {"partner_trained_chara_id": tid,
            "owner_viewer_id": social._wire_id(owner_viewer_id),
            "owner_name": owner_name or "",
            "owner_trained_chara_id": tid,
            "friend_state": social.friend_data(me, owner_viewer_id)["state"]}


def _best_of(viewer_id, rosters: dict, cards: dict):
    roster = rosters.get(str(viewer_id)) or []
    if not roster:
        return None, ""
    card = cards.get(str(viewer_id)) or {}
    chara = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    return chara, card.get("name") or ""


@registry.endpoint("practice_race/search_partner")
def handle_search_partner(payload: dict) -> dict:
    viewer_id, _, _ = _load(payload)
    me = str(viewer_id)

    circle = social.circle_of(me)
    circle_ids = [v for v in (social.member_ids(circle["circle_id"]) if circle else [])
                  if str(v) != me]
    pool = [v for v in directory.all_cards(exclude=me) if v not in set(map(str, circle_ids))]
    random.shuffle(pool)
    recommend_ids = pool[:RECOMMEND_SIZE]

    everyone = [str(v) for v in circle_ids] + [str(v) for v in recommend_ids]
    rosters = state_store.all_states_for_key(trained_chara.ROSTER_KEY, everyone)
    cards = directory.cards_for(everyone)

    circle_charas, recommend_charas, owners = [], [], []
    for vid, bucket in ([(v, circle_charas) for v in circle_ids]
                        + [(v, recommend_charas) for v in recommend_ids]):
        chara, name = _best_of(vid, rosters, cards)
        if chara is None:
            continue
        bucket.append(copy.deepcopy(chara))
        owners.append(_owner_info(me, vid, chara, name))

    return _ok({"circle_user_partner_chara_array": circle_charas,
                "recommend_partner_chara_array": recommend_charas,
                "practice_partner_owner_info_array": owners})


@registry.endpoint("practice_race/save_partner")
def handle_save_partner(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    me = str(viewer_id)
    target = payload.get("target_viewer_id")
    tid = payload.get("target_trained_chara_id")
    if not target or not tid:
        return _refuse()

    roster = (state_store.all_states_for_key(trained_chara.ROSTER_KEY,
                                             [str(target)]).get(str(target)) or [])
    chara = next((c for c in roster if c.get("trained_chara_id") == tid), None)
    if chara is None:
        log.warning("practice_race/save_partner: %s has no trained chara %s",
                    target, tid)
        return _refuse(ResultCode.PRACTICE_RACE_SAVE_PARTNER_NO_TRAINED_CHARA_ERROR)
    if len(st["partners"]) >= PARTNER_LIMIT:
        log.warning("practice_race/save_partner: partner list full (%s)", PARTNER_LIMIT)
        return _refuse()

    name = (directory.cards_for([str(target)]).get(str(target)) or {}).get("name") or ""
    owner = _owner_info(me, target, chara, name)
    st["partners"] = [p for p in st["partners"]
                      if p["chara"].get("trained_chara_id") != tid]
    st["partners"].append({"chara": copy.deepcopy(chara), "owner": owner})
    state_store.save_state(viewer_id, full_state)
    return _ok({"add_partner_info": copy.deepcopy(chara),
                "practice_partner_owner_info": owner})


@registry.endpoint("practice_race/delete_partner")
def handle_delete_partner(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    drop = {int(t) for t in (payload.get("target_trained_chara_id_array") or []) if t}
    st["partners"] = [p for p in st["partners"]
                      if p["chara"].get("trained_chara_id") not in drop]
    state_store.save_state(viewer_id, full_state)
    return _ok({"practice_partner_chara_array": [copy.deepcopy(p["chara"])
                                                 for p in st["partners"]]})


def _issued(st, tid) -> dict | None:
    return (st.get("partner_ids") or {}).get(str(tid))


@registry.endpoint("practice_race/get_partner_id")
def handle_get_partner_id(payload: dict) -> dict:
    viewer_id, _, st = _load(payload)
    tid = payload.get("target_trained_chara_id")
    issued = _issued(st, tid) or {}
    return _ok({
        "partner_id": issued.get("partner_id") or 0,
        "register_time": issued.get("register_time") or "",
        "is_reach_partner_id_create_num_max":
            1 if len(st["partner_ids"]) >= PARTNER_ID_LIMIT else 0,
        "is_exist_used_history":
            1 if any(h.get("trained_chara_id") == tid
                     for h in st["used_history"]) else 0,
        "daily_post_partner_count": len(st["partner_ids"]),
        "is_already_circle_post_chara": 1 if _is_circle_posted(viewer_id, tid) else 0,
    })


def _is_circle_posted(viewer_id, tid) -> bool:
    circle = social.circle_of(str(viewer_id))
    if not circle:
        return False
    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    card = directory.cards_for([str(viewer_id)]).get(str(viewer_id)) or {}
    best = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    return bool(best) and best.get("trained_chara_id") == tid


@registry.endpoint("practice_race/create_partner_id")
def handle_create_partner_id(payload: dict) -> dict:
    viewer_id, full_state, st = _load(payload)
    tid = payload.get("target_trained_chara_id")
    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    if not tid or not any(c.get("trained_chara_id") == tid for c in roster):
        log.warning("practice_race/create_partner_id: %s not in viewer %s roster",
                    tid, viewer_id)
        return _refuse()

    existing = _issued(st, tid)
    if existing:
        return _ok({"partner_id": existing["partner_id"],
                    "register_time": existing["register_time"],
                    "is_reach_partner_id_create_num_max":
                        1 if len(st["partner_ids"]) >= PARTNER_ID_LIMIT else 0})
    if len(st["partner_ids"]) >= PARTNER_ID_LIMIT:
        return _refuse(ResultCode.PRACTICE_RACE_DAILY_CREATE_LIMIT_PARTNER_ID_ERROR)

    issued = {"partner_id": random.randint(10_000_000, 99_999_999),
              "register_time": social.now(),
              "trained_chara_id": tid}
    st["partner_ids"][str(tid)] = issued
    state_store.save_state(viewer_id, full_state)
    log.info("practice_race: minted partner id %s for trained chara %s",
             issued["partner_id"], tid)
    return _ok({"partner_id": issued["partner_id"],
                "register_time": issued["register_time"],
                "is_reach_partner_id_create_num_max":
                    1 if len(st["partner_ids"]) >= PARTNER_ID_LIMIT else 0})


@registry.endpoint("practice_race/get_partner_info")
def handle_get_partner_info(payload: dict) -> dict:
    viewer_id, _, _ = _load(payload)
    me = str(viewer_id)
    partner_id = int(payload.get("partner_id") or 0)
    want_time = payload.get("register_time") or ""
    if not partner_id:
        return _refuse(ResultCode.PRACTICE_RACE_INVALID_PARTNER_ID_ERROR)

    lobbies = state_store.all_states_for_key(STATE_KEY)
    for owner_id, lobby in lobbies.items():
        if not isinstance(lobby, dict):
            continue
        for issued in (lobby.get("partner_ids") or {}).values():
            if issued.get("partner_id") != partner_id:
                continue
            if want_time and issued.get("register_time") != want_time:
                continue
            roster = (state_store.all_states_for_key(
                trained_chara.ROSTER_KEY, [str(owner_id)]).get(str(owner_id)) or [])
            chara = next((c for c in roster
                          if c.get("trained_chara_id") == issued.get("trained_chara_id")),
                         None)
            if chara is None:
                log.warning("practice_race: partner id %s resolves to a career "
                            "viewer %s no longer has", partner_id, owner_id)
                return _refuse(ResultCode.PRACTICE_RACE_SAVE_PARTNER_NO_TRAINED_CHARA_ERROR)
            name = (directory.cards_for([str(owner_id)]).get(str(owner_id))
                    or {}).get("name") or ""
            return _ok({"practice_partner_info": copy.deepcopy(chara),
                        "practice_partner_owner_info":
                            _owner_info(me, owner_id, chara, name)})
    log.info("practice_race: partner id %s not found", partner_id)
    return _refuse(ResultCode.PRACTICE_RACE_INVALID_PARTNER_ID_ERROR)


_HISTORY_LIMIT = 50
_HISTORY_TYPE_PARTNER = 1


def _record_partner_use(st: dict, viewer_id, entries) -> None:
    borrowed = {p["chara"].get("trained_chara_id"): p for p in st["partners"]}
    circle = social.circle_of(str(viewer_id))
    circle_name = (circle or {}).get("name") or ""
    for entry in entries or []:
        tid = entry.get("trained_chara_id")
        partner = borrowed.get(tid)
        if partner is None:
            continue
        st["used_history"].insert(0, {
            "trained_chara_id": tid,
            "practice_exec_viewer_id": social._wire_id(viewer_id),
            "name": partner["chara"].get("name") or "",
            "history_type": _HISTORY_TYPE_PARTNER,
            "circle_name": circle_name,
            "use_time": social.now()})
    del st["used_history"][_HISTORY_LIMIT:]


@registry.endpoint("practice_race/used_history")
def handle_used_history(payload: dict) -> dict:
    _, _, st = _load(payload)
    tid = payload.get("target_trained_chara_id")
    rows = st["used_history"]
    if tid:
        rows = [h for h in rows if h.get("trained_chara_id") == tid]
    return _ok({"practice_used_history_array": copy.deepcopy(rows),
                "used_count_total": len(rows)})
