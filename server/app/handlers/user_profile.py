
from __future__ import annotations

import copy
import logging
import time

from .. import master_data
from .. import state as state_store
from . import load as load_mod
from .. import profile_images
from . import collection, honors, missions, registry, trained_chara
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

TROPHY_STATE_KEY = "trophy_state"
HOME_STATE_KEY = "home_state"
HONOR_STATE_KEY = "honor_state"

_DEFAULT_HONOR_ID = 100101
_EPOCH_TIME = "1970-01-01 00:00:00"
_MAX_NAME_LEN = 16

_POSITION_KEYS = (
    "position1_chara_id", "position2_chara_id",
    "position3_chara_id", "position4_chara_id",
    "position1_cloth_id", "position2_cloth_id",
    "position3_cloth_id", "position4_cloth_id",
)


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_data(full_state: dict, viewer_id) -> dict:
    return load_mod.get_or_seed_data(full_state, viewer_id)


def _trophy_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(TROPHY_STATE_KEY)
    if isinstance(st, dict):
        return st
    trophies: dict = {}
    seed = _load_data(full_state, viewer_id).get("login_trophy_info_array") or []
    for t in seed:
        tid = t.get("trophy_id")
        if not tid:
            continue
        trophies[str(tid)] = {
            "create_time": _now(),
            "charas": {str(c): 1 for c in (t.get("chara_id_array") or [])},
        }
    st = {"trophies": trophies, "last_checked_time": 0}
    full_state[TROPHY_STATE_KEY] = st
    return st


def grant_trophy(viewer_id, trophy_id: int, chara_id: int = 0,
                 win_count: int = 1) -> bool:
    if not master_data.query_one(
            "SELECT trophy_id FROM race_trophy WHERE trophy_id=?", (trophy_id,)):
        return False
    full_state = state_store.get_state(viewer_id) or {}
    st = _trophy_state(full_state, viewer_id)
    entry = st["trophies"].setdefault(
        str(trophy_id), {"create_time": _now(), "charas": {}})
    if chara_id:
        charas = entry.setdefault("charas", {})
        charas[str(chara_id)] = (charas.get(str(chara_id)) or 0) + max(1, win_count)
    state_store.save_state(viewer_id, full_state)
    return True


_TROPHY_CARATS_BY_GRADE = {100: 100, 200: 50, 300: 30}
_TROPHY_CARATS_BY_ID = {9022: 150, 9032: 150, 9042: 150}
_TROPHY_PRESENT_MESSAGE_ID = 43
_AWARDED_KEY = "awarded"


def award_race_trophy(full_state: dict, viewer_id, race_instance_id: int,
                      chara_id: int) -> tuple:
    from . import presents
    row = master_data.query_one(
        "SELECT rt.trophy_id, r.grade FROM race_trophy rt "
        "JOIN race_instance ri ON ri.id = rt.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE rt.race_instance_id = ?",
        (race_instance_id,))
    if not row or not chara_id:
        return [], None
    tid, key = int(row["trophy_id"]), str(row["trophy_id"])
    st = _trophy_state(full_state, viewer_id)
    awarded = st.setdefault(_AWARDED_KEY, {})
    holders = {str(c) for c in awarded.get(key) or []}
    holders |= set((st["trophies"].get(key) or {}).get("charas", {}))
    roster = full_state.get(trained_chara.ROSTER_KEY) or []
    held = key in st["trophies"] or key in awarded
    for_roster = _roster_wins(roster).get(key)
    if for_roster:
        held = True
        holders |= set(for_roster["charas"])
    if str(chara_id) in holders:
        return [], None
    awarded.setdefault(key, []).append(int(chara_id))
    reward = None
    carats = _TROPHY_CARATS_BY_ID.get(tid, _TROPHY_CARATS_BY_GRADE.get(row["grade"]))
    if not held and carats:
        presents.send(full_state, 90, 43, carats, message_id=_TROPHY_PRESENT_MESSAGE_ID)
        reward = {"item_id": 43, "item_num": carats, "item_type": 90}
    return {"trophy_id": tid, "chara_id_array": [int(chara_id)]}, reward


_program_trophy_cache: dict = {}


def _trophy_for_program(program_id: int):
    if program_id in _program_trophy_cache:
        return _program_trophy_cache[program_id]
    row = master_data.query_one(
        "SELECT rt.trophy_id, rt.race_instance_id FROM single_mode_program p "
        "JOIN race_trophy rt ON rt.race_instance_id = p.race_instance_id "
        "WHERE p.id = ?", (program_id,))
    _program_trophy_cache[program_id] = row
    return row


def _roster_wins(roster: list) -> dict:
    wins: dict = {}
    for rec in roster:
        chara_id = (rec.get("card_id") or 0) // 100
        for rr in rec.get("race_result_list") or []:
            if rr.get("result_rank") != 1 or not rr.get("program_id"):
                continue
            row = _trophy_for_program(rr["program_id"])
            if not row:
                continue
            entry = wins.setdefault(str(row["trophy_id"]), {
                "create_time": rec.get("create_time") or _now(), "charas": {}})
            key = str(chara_id)
            entry["charas"][key] = (entry["charas"].get(key) or 0) + 1
    return wins


@registry.endpoint("user/get_trophy_info")
def handle_get_trophy_info(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    st = _trophy_state(full_state, viewer_id)

    merged = copy.deepcopy(st["trophies"])
    for tid, win in _roster_wins(roster).items():
        entry = merged.setdefault(
            tid, {"create_time": win["create_time"], "charas": {}})
        for cid, n in win["charas"].items():
            entry["charas"][cid] = (entry["charas"].get(cid) or 0) + n

    array = []
    for tid in sorted(merged, key=int):
        row = master_data.query_one(
            "SELECT race_instance_id FROM race_trophy WHERE trophy_id=?", (int(tid),))
        if not row:
            continue
        entry = merged[tid]
        array.append({
            "trophy_id": int(tid),
            "create_time": entry.get("create_time") or _now(),
            "race_instance_info_array": [{
                "race_instance_id": row["race_instance_id"],
                "trophy_chara_info_array": [
                    {"chara_id": int(c), "win_count": n}
                    for c, n in sorted(entry.get("charas", {}).items(),
                                       key=lambda kv: int(kv[0]))],
            }],
        })

    now = int(time.time())
    last = st.get("last_checked_time") or now
    st["last_checked_time"] = now
    state_store.save_state(viewer_id, full_state)
    return _ok({"user_trophy_info_array": array, "last_checked_time": last})


@registry.endpoint("user/change_favorite_character")
def handle_change_favorite_character(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    spi = payload.get("set_position_info")
    if not isinstance(spi, dict):
        log.info("change_favorite_character refused: set_position_info not a dict: %r", spi)
        return _refuse()
    info = {}
    for key in _POSITION_KEYS:
        v = spi.get(key, 0)
        if not isinstance(v, int) or v < 0:
            log.info("change_favorite_character refused: bad %s=%r", key, v)
            return _refuse()
        info[key] = v
    if not info["position1_chara_id"]:
        log.info("change_favorite_character refused: position1_chara_id not set")
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    owned_charas = {c.get("chara_id") for c in
                    (full_state.get(collection.CHARA_LIST_KEY) or ())}
    for i in (1, 2, 3, 4):
        cid = info[f"position{i}_chara_id"]
        if not cid:
            continue
        if not master_data.query_one("SELECT id FROM chara_data WHERE id=?", (cid,)):
            log.info("change_favorite_character refused: chara_id %s not in chara_data", cid)
            return _refuse()
        if cid not in owned_charas:
            log.info("change_favorite_character refused: chara_id %s not owned (owned=%s)",
                     cid, sorted(owned_charas))
            return _refuse()
        cloth = info[f"position{i}_cloth_id"]
        if cloth and cloth != 2:
            dress = master_data.query_one(
                "SELECT chara_id, general_purpose, use_home FROM dress_data WHERE id=?", (cloth,))
            owns_dress = dress and (dress["chara_id"] == cid or dress["general_purpose"])
            if not dress or not owns_dress or not dress["use_home"]:
                log.info("change_favorite_character refused: cloth_id %s invalid for chara %s "
                         "(dress_row=%s)", cloth, cid, dict(dress) if dress else None)
                return _refuse()

    full_state.setdefault(HOME_STATE_KEY, {})["home_position_info"] = copy.deepcopy(info)
    _load_data(full_state, viewer_id)["home_position_info"] = copy.deepcopy(info)
    missions.mark_achieved(full_state, missions.FLAG_HOME_COMPANIONS_CHANGED)
    state_store.save_state(viewer_id, full_state)
    return _ok({"home_position_info": copy.deepcopy(info)})


def _ok_user_info(full_state: dict, user_info: dict) -> dict:
    from . import load
    return _ok({"user_info": load.apply_derived_user_info(
        full_state, copy.deepcopy(user_info))})


@registry.endpoint("user/change_name")
def handle_change_name(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    name = payload.get("name")
    if not isinstance(name, str):
        return _refuse()
    name = name.strip()
    if not name or len(name) > _MAX_NAME_LEN:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["name"] = name
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


@registry.endpoint("user/change_sex")
def handle_change_sex(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    sex = payload.get("sex")
    if sex not in (1, 2):
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["sex"] = sex
    from . import tool as tool_mod
    tool_mod._skip_tutorial(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


def _incoming_id(payload: dict, *keys) -> int | None:
    for k in keys:
        v = payload.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def _save_and_republish(viewer_id, full_state: dict, user_info: dict) -> dict:
    from . import directory
    try:
        directory.publish(viewer_id, full_state)
    except Exception:
        log.exception("failed to republish directory card for %s", viewer_id)
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


@registry.endpoint("user/change_practice_partner")
def handle_change_practice_partner(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    trained_chara_id = _incoming_id(payload, "trained_chara_id",
                                    "partner_chara_id", "practice_partner_id",
                                    "chara_id")
    if trained_chara_id is None:
        return _refuse()
    roster = trained_chara._get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == trained_chara_id for c in roster):
        return _refuse()
    from . import directory
    full_state = state_store.get_state(viewer_id) or {}
    full_state[directory.PARTNER_KEY] = trained_chara_id
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["partner_chara_id"] = trained_chara_id
    return _save_and_republish(viewer_id, full_state, user_info)


@registry.endpoint("user/change_leader_card")
def handle_change_leader_card(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    requested = _incoming_id(payload, "card_id", "leader_card_id",
                             "leader_chara_id", "chara_id")
    if requested is None:
        return _refuse()

    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?",
                                (requested,))
    full_state = state_store.get_state(viewer_id) or {}
    owned_cards = full_state.get(collection.CARD_LIST_KEY) or []
    if row is not None:
        card_id, chara_id = requested, row["chara_id"]
        if not any(c.get("card_id") == card_id for c in owned_cards):
            log.info("change_leader_card refused: card %s not owned", card_id)
            return _refuse()
    else:
        if not master_data.query_one("SELECT id FROM chara_data WHERE id=?",
                                     (requested,)):
            return _refuse()
        chara_id = requested
        if not any((c.get("card_id") or 0) // 100 == chara_id for c in owned_cards):
            log.info("change_leader_card refused: chara %s not owned", chara_id)
            return _refuse()

    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    dress_id = _incoming_id(payload, "dress_id", "leader_chara_dress_id")
    if dress_id is not None:
        dress = master_data.query_one(
            "SELECT chara_id, general_purpose FROM dress_data WHERE id=?", (dress_id,))
        if dress is None:
            return _refuse()
        owned_cloth = any(c.get("cloth_id") == dress_id
                          for c in full_state.get("cloth_list_state") or [])
        if not (owned_cloth or dress["general_purpose"]
                or dress["chara_id"] == chara_id):
            log.info("change_leader_card refused: dress %s not available to chara %s",
                     dress_id, chara_id)
            return _refuse()
        user_info["leader_chara_dress_id"] = dress_id

    user_info["leader_chara_id"] = chara_id
    return _save_and_republish(viewer_id, full_state, user_info)


@registry.endpoint("user/change_support_card")
def handle_change_support_card(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    support_card_id = _incoming_id(payload, "support_card_id", "card_id")
    if support_card_id is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    owned = full_state.get(collection.SUPPORT_CARD_KEY) or []
    if not any(c.get("support_card_id") == support_card_id for c in owned):
        return _refuse()
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["support_card_id"] = support_card_id
    return _save_and_republish(viewer_id, full_state, user_info)


def _honor_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(HONOR_STATE_KEY)
    if isinstance(st, dict):
        return st
    honors: dict = {}
    seed = (_load_data(full_state, viewer_id).get("honor_info") or {})
    for h in seed.get("honor_list") or []:
        if isinstance(h, dict) and h.get("honor_id"):
            honors[str(h["honor_id"])] = h.get("create_time") or _EPOCH_TIME
    if not honors:
        honors[str(_DEFAULT_HONOR_ID)] = _EPOCH_TIME
    st = {"honors": honors, "last_checked_time": 0}
    full_state[HONOR_STATE_KEY] = st
    return st


def _grant_honor_in_state(st: dict, honor_id: int) -> bool:
    if str(honor_id) not in st["honors"]:
        st["honors"][str(honor_id)] = _now()
        return True
    return False


def grant_honor(viewer_id, honor_id: int) -> bool:
    if not master_data.query_one(
            "SELECT id FROM honor_data WHERE id=?", (honor_id,)):
        return False
    full_state = state_store.get_state(viewer_id) or {}
    st = _honor_state(full_state, viewer_id)
    if _grant_honor_in_state(st, honor_id):
        state_store.save_state(viewer_id, full_state)
    return True


@registry.endpoint("honor/index")
def handle_honor_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _honor_state(full_state, viewer_id)
    now = int(time.time())
    last = st.get("last_checked_time") or now
    st["last_checked_time"] = now
    newly_earned = honors.newly_earned_honor_ids(
        viewer_id, full_state, set(st["honors"].keys()), len(st["honors"]))
    for hid in newly_earned:
        _grant_honor_in_state(st, hid)
    mission_list = missions.build_mission_list(viewer_id, full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "honor_list": [
            {"honor_id": int(hid), "create_time": ct}
            for hid, ct in sorted(st["honors"].items(), key=lambda kv: int(kv[0]))],
        "last_checked_time": last,
        "mission_list": mission_list,
        "story_event_mission_list": [],
    })


@registry.endpoint("honor/change_honor")
def handle_change_honor(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    honor_id = payload.get("honor_id")
    if not isinstance(honor_id, int):
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _honor_state(full_state, viewer_id)
    if str(honor_id) not in st["honors"]:
        return _refuse()
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["honor_id"] = honor_id
    user_info["honor_data"] = {"honor_id": honor_id}
    missions.mark_achieved(full_state, missions.FLAG_TITLE_CHANGED)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


FRIEND_STATE_KEY = "friend_state"


@registry.endpoint("friend/index")
def handle_friend_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = full_state.get(FRIEND_STATE_KEY)
    if not isinstance(st, dict):
        st = {"last_checked_time": None}
        full_state[FRIEND_STATE_KEY] = st
    last = st.get("last_checked_time") or "0000-00-00 00:00:00"
    st["last_checked_time"] = _now()

    from . import friends, single_mode_team
    injected = full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or []
    data = friends.build_index(viewer_id, full_state, copy.deepcopy(injected))
    state_store.save_state(viewer_id, full_state)

    data["last_friend_checked_time"] = last
    return _ok(data)


def _pvp_friend_search(viewer_id, target_id) -> dict:
    from .. import champions_pool as POOL, social
    from . import trained_chara
    try:
        vid = int(target_id)
    except (TypeError, ValueError):
        return _refuse()
    seen = POOL.SEEN.get(vid)
    charas = list((seen or {}).get("charas", {}).values())
    if charas:
        charas = [copy.deepcopy(c) for c in sorted(charas, key=lambda c: -int(c.get("rank_score") or 0))]
    else:
        charas = [copy.deepcopy(trained_chara._load_starter_trained_chara(vid)[0])]
    partner = charas[0]
    name = (seen or {}).get("name") or f"Trainer {vid % 10000:04d}"
    now = "2026-01-01 00:00:00"
    summary = {
        "viewer_id": vid, "name": name, "honor_id": 100101, "honor_data": {"honor_id": 100101},
        "last_login_time": now, "leader_chara_id": partner.get("chara_id") or partner["card_id"] // 100,
        "leader_chara_dress_id": partner.get("race_cloth_id") or 2, "support_card_id": 0,
        "partner_chara_id": partner.get("chara_id") or partner["card_id"] // 100,
        "comment": "", "fan": int(partner.get("fans") or 1), "directory_level": 1,
        "rank_score": int(partner.get("rank_score") or 1), "team_stadium_win_count": 0,
        "single_mode_play_count": len(charas), "team_evaluation_point": 0, "best_team_evaluation_point": 0,
        "friend_state": 0, "state": 0, "circle_info": None, "circle_user": None,
        "user_trained_chara": copy.deepcopy(partner),
    }
    return _ok({
        "friend_info": social.friend_data(viewer_id, vid),
        "user_info_summary": summary,
        "practice_partner_info": copy.deepcopy(partner),
        "directory_card_array": [{"card_id": c["card_id"], "directory_ranking": i + 1,
                                  "trained_chara": c} for i, c in enumerate(charas)],
        "follower_num": 0, "own_follow_num": 0, "enable_circle_scout": 0,
        **copy.deepcopy(_FRIEND_SEARCH_NEUTRAL_STATS),
    })


def _real_friend_search(viewer_id, target_id) -> dict:
    from . import directory, team_stadium, trained_chara
    from .. import social

    target = str(target_id)
    card = directory.cards_for([target]).get(target)
    if card is None:
        return _pvp_friend_search(viewer_id, target_id)
    rel = social.friend_data(viewer_id, target)

    target_state = state_store.get_state(target) or {}
    roster = target_state.get(trained_chara.ROSTER_KEY) or []
    partner = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    partner_block = copy.deepcopy(partner) if partner is not None else {}
    directory_array = directory.archive_entries(roster, target_state)

    ts = target_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    stats = copy.deepcopy(_FRIEND_SEARCH_NEUTRAL_STATS)
    stats["team_stadium_user"] = {
        "team_class": ts.get("rank") or 1,
        "best_team_class": ts.get("granted_rank") or ts.get("rank") or 1,
        "team_class_state": 0,
        "best_point": ts.get("best_point") or 0,
    }

    data = {
        "friend_info": rel,
        "user_info_summary": directory.summary(target, card, rel["state"]),
        "practice_partner_info": partner_block,
        "directory_card_array": directory_array,
        "follower_num": social.follower_num(target),
        "own_follow_num": social.follow_num(target),
        "enable_circle_scout": int(_can_scout(viewer_id, target)),
        **stats,
    }
    if "user_support_card" in card:
        data["support_card_data"] = copy.deepcopy(card["user_support_card"])
    return _ok(data)


def _can_scout(viewer_id, target_id) -> bool:
    from .. import social
    mine = social.member_row(viewer_id)
    if mine is None or mine["membership"] < social.MEMBERSHIP_SUB_LEADER:
        return False
    if social.member_row(target_id) is not None:
        return False
    circle = social.circle_of(viewer_id)
    return circle is not None and circle["member_num"] < social.MAX_CIRCLE_MEMBERS


_FRIEND_SEARCH_NEUTRAL_STATS = {
    "release_num_info": {
        "voice_num": 0, "act_num": 0, "good_end_num": 0, "chara_event_num": 0,
        "support_event_num": 0, "scenario_event_num": 0, "home_event_num": 0,
        "music_num": 0, "main_story_num": 0, "chara_story_num": 0,
        "card_num": 1, "support_card_num": 1,
    },
    "trophy_num_info": {"grade_1": 0, "grade_2": 0, "grade_3": 0, "grade_ex": 0},
    "team_stadium_user": {"team_class": 1, "best_team_class": 1,
                          "team_class_state": 0, "best_point": 0},
}


@registry.endpoint("friend/search")
def handle_friend_search(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target_id = payload.get("friend_viewer_id")
    full_state = state_store.get_state(viewer_id) or {}

    from . import single_mode_team, trained_chara
    injected = full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or []
    match = next((e for e in injected if e["viewer_id"] == target_id), None)
    if match is None:
        return _real_friend_search(viewer_id, target_id)

    usc = match["user_support_card"]
    partner = trained_chara._load_starter_trained_chara(target_id)[0]

    return _ok({
        "friend_info": {"friend_viewer_id": target_id, "state": 1,
                        "follow_time": match["last_login_time"],
                        "follower_time": match["last_login_time"]},
        "user_info_summary": copy.deepcopy(match),
        "practice_partner_info": copy.deepcopy(partner),
        "directory_card_array": [{
            "card_id": partner["card_id"], "directory_ranking": 1,
            "trained_chara": copy.deepcopy(partner),
        }],
        "support_card_data": {
            "viewer_id": target_id, "support_card_id": usc["support_card_id"],
            "exp": usc["exp"], "limit_break_count": usc["limit_break_count"],
            "favorite_flag": 0, "stock": 0,
            "possess_time": match["last_login_time"], "create_time": match["last_login_time"],
        },
        "follower_num": 1, "own_follow_num": 1, "enable_circle_scout": 0,
        **copy.deepcopy(_FRIEND_SEARCH_NEUTRAL_STATS),
    })


@registry.endpoint("photo/library")
def handle_photo_library(payload: dict) -> dict:
    return _ok({"unique_id": "", "unique_id_circle": ""})


PROFILE_CARD_STATE_KEY = "profile_card_state"

_PROFILE_CARD_INT_FIELDS = (
    "dress_id", "bg_id", "card_bg_id", "theme_id", "illustration_type",
    "image_offset_x", "image_offset_y", "image_rotate", "image_scale",
)
_PROFILE_CARD_BOOL_FIELDS = (
    "is_trainer_info_aligned_right", "show_back_side", "show_trainer_id",
)
_MAX_COMMENT_LEN = 200
_FALLBACK_CHARA_ID = 1001


PROFILE_CARD_DEFAULTS = {
    "dress_id": 2, "bg_id": 2001, "card_bg_id": 1, "theme_id": 1,
    "illustration_type": 1, "image_offset_x": 1073741824,
    "image_offset_y": 1073741824, "image_rotate": 0, "image_scale": 10000,
}
_NONZERO_FIELDS = ("dress_id", "bg_id", "card_bg_id", "theme_id", "illustration_type",
                   "image_scale")


def repair_card_info(info: dict) -> dict:
    for k, v in PROFILE_CARD_DEFAULTS.items():
        if not isinstance(info.get(k), int) or (k in _NONZERO_FIELDS and info.get(k) == 0):
            info[k] = v
    if info.get("image_offset_x") == 0 and info.get("image_offset_y") == 0:
        info["image_offset_x"] = info["image_offset_y"] = PROFILE_CARD_DEFAULTS["image_offset_x"]
    return info


def _default_profile_card_info(full_state: dict, viewer_id) -> dict:
    home = (full_state.get(HOME_STATE_KEY) or {}).get("home_position_info") or {}
    chara_id = home.get("position1_chara_id")
    if not chara_id:
        roster = trained_chara._get_or_seed_roster(viewer_id)
        card_id = (roster[0].get("card_id") if roster else None) or 0
        chara_id = (card_id // 100) if card_id else _FALLBACK_CHARA_ID
    info = {"chara_id": chara_id, "support_card_id": 0, "comment": "",
            "image_file_hash": ""}
    info.update(PROFILE_CARD_DEFAULTS)
    info.update({k: False for k in _PROFILE_CARD_BOOL_FIELDS})
    info["show_trainer_id"] = True
    return info


def _profile_card_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(PROFILE_CARD_STATE_KEY)
    if not isinstance(st, dict):
        st = {"info": _default_profile_card_info(full_state, viewer_id),
              "trained_chara_id": 0, "honor_id": 0}
        full_state[PROFILE_CARD_STATE_KEY] = st
    repair_card_info(st.setdefault("info", {}))
    return st


def profile_card_wire(st: dict, fallback_chara: int | None = None) -> dict:
    info = copy.deepcopy(st["info"])
    from .. import custom_support
    hidden = custom_support._hidden_trainees()[1]
    if info.get("chara_id") in hidden:
        info["chara_id"] = fallback_chara if fallback_chara and fallback_chara not in hidden else 1001
        info["dress_id"] = PROFILE_CARD_DEFAULTS["dress_id"]
    photo = st.get("photo") or {}
    has = profile_images.exists(photo.get("path")) and info.get("image_file_hash") == photo.get("hash")
    info["image_path"] = photo["path"] if has else None
    return {"profile_card_info": info, **profile_images.wire(photo if has else None)}


def _owns_chara(viewer_id, chara_id: int) -> bool:
    cards = (state_store.get_state(viewer_id) or {}).get(collection.CARD_LIST_KEY)
    return isinstance(cards, list) and any(
        isinstance(c, dict) and (c.get("card_id") or 0) // 100 == chara_id for c in cards)


@registry.endpoint("user/get_profile_card_info")
def handle_get_profile_card_info(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _profile_card_state(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)
    leader = (_load_data(full_state, viewer_id).get("user_info") or {}).get("leader_chara_id")
    return _ok(profile_card_wire(st, leader))


@registry.endpoint("user/set_profile_card_info")
def handle_set_profile_card_info(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    incoming = payload.get("profile_card_info")
    if not isinstance(incoming, dict):
        return _refuse()

    chara_id = incoming.get("chara_id")
    if not isinstance(chara_id, int) or not (master_data.query_one(
            "SELECT id FROM chara_data WHERE id=?", (chara_id,))
            or _owns_chara(viewer_id, chara_id)):
        return _refuse()

    support_card_id = incoming.get("support_card_id") or 0
    if not isinstance(support_card_id, int):
        return _refuse()
    if support_card_id and not master_data.query_one(
            "SELECT id FROM support_card_data WHERE id=?", (support_card_id,)):
        return _refuse()

    comment = incoming.get("comment", "")
    if not isinstance(comment, str) or len(comment) > _MAX_COMMENT_LEN:
        return _refuse()

    info = {"chara_id": chara_id, "support_card_id": support_card_id,
            "comment": comment,
            "image_file_hash": str(incoming.get("image_file_hash") or "").upper()}
    for key in _PROFILE_CARD_INT_FIELDS:
        v = incoming.get(key, 0)
        if not isinstance(v, int):
            return _refuse()
        info[key] = v
    for key in _PROFILE_CARD_BOOL_FIELDS:
        info[key] = bool(incoming.get(key, False))

    full_state = state_store.get_state(viewer_id) or {}

    trained_chara_id = payload.get("trained_chara_id") or 0
    if trained_chara_id:
        roster = trained_chara._get_or_seed_roster(viewer_id)
        if not any(c.get("trained_chara_id") == trained_chara_id for c in roster):
            return _refuse()

    honor_id = payload.get("honor_id") or 0
    if honor_id and str(honor_id) not in _honor_state(full_state, viewer_id)["honors"]:
        return _refuse()

    st = _profile_card_state(full_state, viewer_id)
    if payload.get("image_file") and info["image_file_hash"]:
        saved = profile_images.store(str(viewer_id), payload["image_file"],
                                     info["image_file_hash"])
        if saved is None:
            return _refuse()
        info["image_file_hash"] = saved["hash"]
        st["photo"] = saved
    repair_card_info(info)
    st["info"] = info
    st["trained_chara_id"] = trained_chara_id
    st["honor_id"] = honor_id

    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    top_support = payload.get("support_card_id")
    if isinstance(top_support, int) and top_support:
        user_info["support_card_id"] = top_support
    if trained_chara_id:
        user_info["partner_chara_id"] = trained_chara_id
    if honor_id:
        user_info["honor_id"] = honor_id
    user_info["comment"] = comment

    missions.mark_achieved(full_state, missions.FLAG_TRAINER_CARD_EDITED)
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


CARD_DRESS_KEY = "card_dress_state"
STORY_FAVORITE_KEY = "story_favorite_state"
DMA_STATE_KEY = "dma_state"

_MAX_COMMENT_LEN = 200


def _valid_birth_day(value) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            time.strptime(value, fmt)
        except ValueError:
            continue
        return value
    return None


@registry.endpoint("user/change_comment")
def handle_change_comment(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    comment = payload.get("comment")
    if comment is None:
        comment = ""
    if not isinstance(comment, str) or len(comment) > _MAX_COMMENT_LEN:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["comment"] = comment
    return _save_and_republish(viewer_id, full_state, user_info)


@registry.endpoint("user/set_birth_day")
def handle_set_birth_day(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    birth_day = _valid_birth_day(payload.get("birth_day"))
    if birth_day is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["birth_day"] = birth_day
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


def _dress_ok(full_state: dict, dress_id: int, chara_id: int) -> bool:
    dress = master_data.query_one(
        "SELECT chara_id, general_purpose FROM dress_data WHERE id=?", (dress_id,))
    if dress is None:
        return False
    owned = any(c.get("cloth_id") == dress_id
                for c in full_state.get("cloth_list_state") or [])
    return bool(owned or dress["general_purpose"] or dress["chara_id"] == chara_id)


def card_dress(full_state: dict, card_id: int) -> int:
    try:
        return int((full_state.get(CARD_DRESS_KEY) or {}).get(str(card_id)) or 0)
    except (TypeError, ValueError):
        return 0


def _sync_leader_dress(full_state: dict, viewer_id, chara_id: int,
                       dress_id: int) -> None:
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    if user_info.get("leader_chara_id") == chara_id:
        user_info["leader_chara_dress_id"] = dress_id


def _chara_of_card(card_id: int) -> int | None:
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    return row["chara_id"] if row else None


@registry.endpoint("user/change_card_dress")
def handle_change_card_dress(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    card_id = _incoming_id(payload, "card_id")
    dress_id = _incoming_id(payload, "dress_id")
    if card_id is None or dress_id is None:
        return _refuse()

    chara_id = _chara_of_card(card_id)
    if chara_id is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    if not any(c.get("card_id") == card_id
               for c in full_state.get(collection.CARD_LIST_KEY) or []):
        log.info("change_card_dress refused: card %s not owned", card_id)
        return _refuse()
    if not _dress_ok(full_state, dress_id, chara_id):
        log.info("change_card_dress refused: dress %s not available to chara %s",
                 dress_id, chara_id)
        return _refuse()

    full_state.setdefault(CARD_DRESS_KEY, {})[str(card_id)] = dress_id
    _sync_leader_dress(full_state, viewer_id, chara_id, dress_id)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("user/reset_card_dress")
def handle_reset_card_dress(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    card_id = _incoming_id(payload, "card_id")
    if card_id is None:
        return _refuse()
    chara_id = _chara_of_card(card_id)
    if chara_id is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    chosen = full_state.get(CARD_DRESS_KEY)
    if isinstance(chosen, dict):
        chosen.pop(str(card_id), None)
    _sync_leader_dress(full_state, viewer_id, chara_id,
                       trained_chara.dress_for_card(card_id, chara_id))
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("user/reset_all_card_dress")
def handle_reset_all_card_dress(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[CARD_DRESS_KEY] = {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    leader = user_info.get("leader_chara_id") or 0
    if leader:
        card = next((c.get("card_id") for c in
                     full_state.get(collection.CARD_LIST_KEY) or []
                     if (c.get("card_id") or 0) // 100 == leader), None)
        if card:
            user_info["leader_chara_dress_id"] = trained_chara.dress_for_card(
                card, leader)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


def _story_favorite_rows(payload) -> list | None:
    rows = payload.get("story_favorite_array")
    if rows is None:
        return []
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            entry = {"episode_type": int(row.get("episode_type") or 0),
                     "episode_id": int(row.get("episode_id") or 0),
                     "is_mark": 1 if row.get("is_mark") else 0}
        except (TypeError, ValueError):
            return None
        if entry["episode_id"] <= 0:
            return None
        out.append(entry)
    return out


@registry.endpoint("user/change_story_favorite")
def handle_change_story_favorite(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    rows = _story_favorite_rows(payload)
    if rows is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    stored = full_state.get(STORY_FAVORITE_KEY)
    current = {(e["episode_type"], e["episode_id"]): e
               for e in (stored if isinstance(stored, list) else [])
               if isinstance(e, dict) and "episode_id" in e}
    for row in rows:
        key = (row["episode_type"], row["episode_id"])
        if row["is_mark"]:
            current[key] = row
        else:
            current.pop(key, None)

    out = [current[k] for k in sorted(current)]
    full_state[STORY_FAVORITE_KEY] = out
    state_store.save_state(viewer_id, full_state)
    return _ok({"story_favorite_array": copy.deepcopy(out)})


@registry.endpoint("user/change_dma_state")
def handle_change_dma_state(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    target = payload.get("target_viewer_id")
    dma_state = payload.get("dma_state")
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        return _refuse()
    if not isinstance(dma_state, int) or isinstance(dma_state, bool):
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    full_state.setdefault(DMA_STATE_KEY, {})[str(target)] = dma_state
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("user/get_trophy_detail")
def handle_get_trophy_detail(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    trophy_id = payload.get("trophy_id")
    if not isinstance(trophy_id, int) or isinstance(trophy_id, bool):
        return _refuse()
    row = master_data.query_one(
        "SELECT race_instance_id FROM race_trophy WHERE trophy_id=?", (trophy_id,))
    if row is None:
        return _refuse()

    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    st = _trophy_state(full_state, viewer_id)
    merged = copy.deepcopy(st["trophies"])
    for tid, win in _roster_wins(roster).items():
        entry = merged.setdefault(tid, {"create_time": win["create_time"],
                                        "charas": {}})
        for cid, n in win["charas"].items():
            entry["charas"][cid] = (entry["charas"].get(cid) or 0) + n

    entry = merged.get(str(trophy_id))
    if entry is None:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({"user_trophy_info": {
        "trophy_id": trophy_id,
        "create_time": entry.get("create_time") or _now(),
        "race_instance_info_array": [{
            "race_instance_id": row["race_instance_id"],
            "trophy_chara_info_array": [
                {"chara_id": int(c), "win_count": n}
                for c, n in sorted(entry.get("charas", {}).items(),
                                   key=lambda kv: int(kv[0]))],
        }],
    }})
