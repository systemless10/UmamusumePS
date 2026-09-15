"""Account meta endpoints: trophies, home-screen favorites, trainer name,
epithets (honors), friend list, photo library.

  user/get_trophy_info           -> the trophy room (won-race trophies)
  user/change_favorite_character -> the 4 home-screen character slots
  user/change_name               -> rename the trainer
  honor/index                    -> earned epithet list
  friend/index                   -> friend list / recommendations
  photo/library                  -> photo-mode gallery ids

Capture ground truth (request in file N-1, response in file N):
  get_trophy_info : 20260717_180912 op 0039 (167 trophies) and
                    20260726_122439 op 0009. Empty request; response
                    {user_trophy_info_array, last_checked_time(epoch int)}.
  change_favorite : 20260726_122439 op 0007 -- request {set_position_info:
                    {position1..4_chara_id, position1..4_cloth_id}}, response
                    {home_position_info: same 8 keys}.
  change_name     : 20260723_134126 op 0008 -- request {name}, response
                    {user_info: <the full account user_info>}.
  honor/index     : 20260717_180912 op 0038 -- empty request; response
                    {honor_list [{honor_id, create_time}], last_checked_time,
                    mission_list, story_event_mission_list}.
  friend/index    : 20260816_202505 op 0051 -- empty request; response
                    {last_friend_checked_time, friend_list, recommend_list,
                    user_info_summary_list, follower_info_summary_list,
                    follower_num}. See handle_friend_index for why the social
                    lists serve empty on a private server.
  photo/library   : 20260816_202505 op 0053 -- empty request; response
                    {unique_id: "", unique_id_circle: ""} (real account, both
                    empty -- nothing to derive).

user/get_profile_card_info / user/set_profile_card_info -> the trainer's
customizable profile card (background, featured character/dress/support
card/honor, comment, layout toggles). NEVER CAPTURED against the real
server (no ENDPOINT_KEYS.md ground truth exists for either) -- the request/
response shapes below come straight from the client's own compiled classes
(dump.cs: UserGetProfileCardInfoResponse.CommonResponse,
UserSetProfileCardInfoRequest, UserProfileCardInfo), not a fixture. Treat
the exact field set as more trustworthy than a guess (it's the real wire
contract) but the DEFAULTS/validation below as ours.
  get: empty request; response {profile_card_info: UserProfileCardInfo,
       image_file_status, image_file_url, image_unique_id}.
  set: request {profile_card_info: UserProfileCardInfo, image_file,
       trained_chara_id, support_card_id, honor_id}; response {user_info}
       (the class has no dedicated echo field -- it returns the account's
       whole user_info, same shape user/change_name returns).
  UserProfileCardInfo: chara_id, dress_id, bg_id, card_bg_id, theme_id,
       support_card_id, illustration_type, comment,
       is_trainer_info_aligned_right, show_back_side, show_trainer_id,
       image_offset_x/y, image_rotate, image_scale, image_file_hash.
Persisted in PROFILE_CARD_STATE_KEY; chara_id/dress_id/support_card_id/
comment are ALSO mirrored onto the stored load blob's user_info (same
pattern as change_name/change_favorite_character) so every other screen
that reads the login snapshot picks up the change too, not just this
endpoint. No real image-upload backend exists (see photo/library above),
so image_file is accepted but never actually stored/served -- image_file_*
stays honestly empty rather than fabricating a working upload pipeline.

Trophies [mdb]: master.mdb race_trophy (288 rows: trophy_id,
race_instance_id, ...). The viewer's list = the load/index seed's
login_trophy_info_array (seeded once into "trophy_state") + wins derived
LIVE from the veteran roster's race_result_list (result_rank 1 ->
single_mode_program.race_instance_id -> race_trophy) + anything other
systems add via grant_trophy(). Roster-derived wins are recomputed per call,
never persisted, so they can't double-count.

Honors [mdb]: master.mdb honor_data (533 rows; names = text_data category 65,
descriptions = 66). Earned set lives in "honor_state", seeded from the load
blob's honor_info.honor_list (falling back to the default epithet 100101
"Rookie Trainer" -- the one honor with a 1970 create_time in the capture).
grant_honor(viewer_id, honor_id) is the hook for other systems.
NOTE: the honor EQUIP endpoint was never captured -- see ENDPOINT_KEYS.md.
mission_list is real (missions.py) as of 2026-08-18; story_event_mission_list
is still served empty (see missions.py's docstring for why).
"""

from __future__ import annotations

import copy
import logging
import time

from .. import master_data
from .. import state as state_store
from . import load as load_mod
from . import collection, honors, missions, registry, trained_chara

log = logging.getLogger("uma-server")

TROPHY_STATE_KEY = "trophy_state"
HOME_STATE_KEY = "home_state"
HONOR_STATE_KEY = "honor_state"

_DEFAULT_HONOR_ID = 100101      # "Rookie Trainer" -- everyone's default
_EPOCH_TIME = "1970-01-01 00:00:00"
_MAX_NAME_LEN = 16              # server-defined bound (client caps lower)

_POSITION_KEYS = (
    "position1_chara_id", "position2_chara_id",
    "position3_chara_id", "position4_chara_id",
    "position1_cloth_id", "position2_cloth_id",
    "position3_cloth_id", "position4_cloth_id",
)


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_data(full_state: dict, viewer_id) -> dict:
    """The viewer's stored load/index blob's data dict -- see
    load.get_or_seed_data (shared with tool.py/account_link.py, which need
    the same seed-if-missing behavior before/without a real load/index call)."""
    return load_mod.get_or_seed_data(full_state, viewer_id)


# ---------------------------------------------------------------- trophies

def _trophy_state(full_state: dict, viewer_id) -> dict:
    """{"trophies": {str(trophy_id): {"create_time", "charas": {str(chara_id):
    win_count}}}, "last_checked_time"} -- seeded once from the load blob's
    login_trophy_info_array (same trophy set the real account reported)."""
    st = full_state.get(TROPHY_STATE_KEY)
    if isinstance(st, dict):
        return st
    trophies: dict = {}
    seed = _load_data(full_state, viewer_id).get("login_trophy_info_array") or []
    for t in seed:
        tid = t.get("trophy_id")
        if not tid:
            continue
        # The seed carries no per-chara win counts -> credit 1 win each.
        trophies[str(tid)] = {
            "create_time": _now(),
            "charas": {str(c): 1 for c in (t.get("chara_id_array") or [])},
        }
    st = {"trophies": trophies, "last_checked_time": 0}
    full_state[TROPHY_STATE_KEY] = st
    return st


def grant_trophy(viewer_id, trophy_id: int, chara_id: int = 0,
                 win_count: int = 1) -> bool:
    """Plain hook for other systems (race finish, admin): add a trophy win.
    Validates against master race_trophy; persists into trophy_state."""
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


_program_trophy_cache: dict = {}


def _trophy_for_program(program_id: int):
    """single_mode program -> race_instance -> race_trophy row (memoized)."""
    if program_id in _program_trophy_cache:
        return _program_trophy_cache[program_id]
    row = master_data.query_one(
        "SELECT rt.trophy_id, rt.race_instance_id FROM single_mode_program p "
        "JOIN race_trophy rt ON rt.race_instance_id = p.race_instance_id "
        "WHERE p.id = ?", (program_id,))
    _program_trophy_cache[program_id] = row
    return row


def _roster_wins(roster: list) -> dict:
    """Trophy wins derived live from the veteran roster: every race_result
    with result_rank 1 whose race has a trophy. {trophy_id: {"create_time",
    "charas": {chara_id: count}}}"""
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
            continue        # stale/unknown id in state -> not servable
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


# ---------------------------------------------------- home favorites / name

@registry.endpoint("user/change_favorite_character")
def handle_change_favorite_character(payload: dict) -> dict:
    """Request {set_position_info}; response {home_position_info} (capture
    echoes the 8 keys verbatim). Persisted in home_state AND written into the
    stored load/index blob's home_position_info so login re-serves it."""
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
        return _refuse()        # slot 1 is the favorite -- must be set

    full_state = state_store.get_state(viewer_id) or {}
    owned_charas = {c.get("chara_id") for c in
                    (full_state.get(collection.CHARA_LIST_KEY) or ())}
    # BUG FIXED 2026-08-26 (user-reported): this only checked a chara_id
    # against master chara_data (does the character EXIST at all), never
    # against what the player actually owns -- any chara_id was accepted
    # verbatim. cloth_id ("dress_id"/"mini_dress_id" in chara_collection) is
    # NOT a card_id -- an outfit is its own row in master's `dress_data`
    # (id, chara_id, condition_type, use_home, ...), a completely separate
    # unlock system from gacha card ownership (confirmed live: a real dress
    # id like 101330 has no card_data row at all, so validating it against
    # card_data/card_collection 205'd every non-default outfit). The 2
    # sentinel is the universal default (see gacha.py/presents.py/shop.py's
    # identical new-chara-entry shape) and always passes. For anything else,
    # the guard that actually matches how outfits work here is: it must be a
    # real dress_data row, usable on the home screen, for THIS SAME character
    # -- i.e. you can't decorate the home screen with another uma's outfit.
    # dress_data.condition_type presumably gates finer unlock rules (event/
    # anniversary costumes) but nothing in this codebase has reverse-
    # engineered its values yet, so it is deliberately not enforced here
    # rather than guessing and re-introducing this same false refusal.
    for i in (1, 2, 3, 4):      # every filled slot must be an OWNED character
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
            # BUG FIXED 2026-08-30 (user-reported: 205 switching the home
            # character to an owned uma). dress_data has two kinds of row:
            # character-specific outfits (chara_id = that character) AND
            # GENERAL-PURPOSE ones (chara_id=0, general_purpose=1 -- e.g. the
            # standard uniform, id 101) usable on ANY character. Requiring an
            # exact chara_id match rejected every general-purpose outfit for
            # every character. Confirmed safe to relax: every chara_id=0 row
            # that ISN'T general_purpose also has use_home=0, so it was
            # already refused by that check on its own -- this only admits
            # the rows actually meant to be shared.
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
    """Respond with a user_info, with the DERIVED fields refreshed onto the
    copy that goes out.

    The client applies a served user_info to WorkUserData wholesale, and the
    stored blob's derived fields (best_team_evaluation_point, bonus_follow_num)
    are stale by design -- load/index only ever overlays them onto its own
    response. Echoing the stored copy raw therefore UNDOES what load/index set:
    BUG FIXED 2026-09-09 (live-reported: changing the displayed uma relocked
    Daily Program, because best_team_evaluation_point came back as 0 and the
    client's point->rank scan then read the account as Team Rank 0)."""
    from . import load
    return _ok({"user_info": load.apply_derived_user_info(
        full_state, copy.deepcopy(user_info))})


@registry.endpoint("user/change_name")
def handle_change_name(payload: dict) -> dict:
    """Request {name}; the capture's response returns the full user_info.
    The new name lands in the stored load blob so every later login shows it."""
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
    """Request {sex}; NEVER CAPTURED before the 2026-08-18 real account-
    creation session (captures/20260818_081331/0007_user_change_sex.json) --
    same response shape as change_name (full user_info), so implemented the
    same way. 1 = male (the client sent this after the trainer picked "male"
    in-game); 2 is the only other value seen in any real capture (fresh-
    account load/index snapshots), so by elimination that's female -- no
    other values ever observed, so those two are all this accepts.

    Also where skip-tutorial-by-default actually hooks in (NOT tool/signup,
    see its docstring): the real capture's own onboarding order is country
    -> signup -> change_name -> change_sex -> [real tutorial content] ->
    tutorial/skip. change_sex is the last step BEFORE tutorial content in
    that real sequence, so this is where the account crosses from "still
    being onboarded" (must still show name/sex prompts -- tutorial_step=0)
    to "onboarding done, skip the content" (tutorial_step=1000), without an
    extra client action."""
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


# --------------------------------------------- star umamusume / lend card
# The two things the Trainer Info screen lets a player put up for everyone
# else to borrow. Both were previously read-only on this server -- the
# profile showed whatever directory.py picked as "best", which is why the
# player could not change them.
#
# Neither request body is pinned by a capture in this repo (the glossary has
# user/change_practice_partner as WIRE-JP with 2 observations and
# user/change_support_card as DUMP-only, and no stored request survives), so
# both accept every plausible spelling of the one id they carry rather than
# betting on a single one -- the same defensive tactic friends.py/circles.py
# use for their target ids. The response is the full user_info, matching
# change_name/set_profile_card_info, which is the shape every other
# user/change_* on this server returns.

def _incoming_id(payload: dict, *keys) -> int | None:
    """First of `keys` present as a positive int; None if none is."""
    for k in keys:
        v = payload.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def _save_and_republish(viewer_id, full_state: dict, user_info: dict) -> dict:
    """Persist a profile change AND refresh this account's directory card, so
    other players' friend lists/search popups show the new pick immediately
    instead of at this account's next login."""
    from . import directory
    try:
        directory.publish(viewer_id, full_state)
    except Exception:
        log.exception("failed to republish directory card for %s", viewer_id)
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


@registry.endpoint("user/change_practice_partner")
def handle_change_practice_partner(payload: dict) -> dict:
    """Set the Star Umamusume -- the horse other players borrow as a practice
    partner. Despite the field name, `partner_chara_id` holds a
    TRAINED_CHARA_ID (capture 20260816_140811/0012: partner_chara_id 3194 ==
    practice_partner_info.trained_chara_id 3194), so this validates against
    the roster, not chara_data."""
    viewer_id = payload["viewer_id"]
    trained_chara_id = _incoming_id(payload, "trained_chara_id",
                                    "partner_chara_id", "practice_partner_id",
                                    "chara_id")
    if trained_chara_id is None:
        return _refuse()
    roster = trained_chara._get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == trained_chara_id for c in roster):
        return _refuse()          # can only lend a horse you actually own
    from . import directory
    full_state = state_store.get_state(viewer_id) or {}
    # The pick lives in its own key (directory.PARTNER_KEY) because every
    # seeded account already carries a default user_info.partner_chara_id
    # nobody chose; user_info is still mirrored so screens reading the login
    # snapshot agree with the profile.
    full_state[directory.PARTNER_KEY] = trained_chara_id
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["partner_chara_id"] = trained_chara_id
    return _save_and_republish(viewer_id, full_state, user_info)


@registry.endpoint("user/change_leader_card")
def handle_change_leader_card(payload: dict) -> dict:
    """Set the leader umamusume -- the one standing on the trainer card and
    every screen that renders `user_info.leader_chara_id` /
    `leader_chara_dress_id`.

    "Card" here is a card_data id (a trainee card, e.g. 100901), which is how
    the player picks a specific art/outfit variant rather than a bare
    character; the chara_id is derived from it (card 100901 -> chara 1009,
    exactly as card_data says). A bare chara_id is accepted too, since the
    request body is not pinned by any capture in this repo (the glossary has
    this as WIRE-JP with 2 observations and no stored request), and both
    spellings resolve to the same account field.

    Only a card the account actually owns can be its leader -- until it is
    pulled, its art is not something this trainer has. The outfit is optional
    and validated the same way: an owned outfit, a general-purpose one (the
    default uniform, dress 101, which is chara_id 0 in dress_data), or one
    belonging to that character.
    """
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
        # Not a card id -- the only other thing this can be is a chara_id,
        # owned if ANY of that character's cards is.
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
    """Set the Career Support card this account lends out. Validated against
    the account's OWN collection -- an id it does not own would render as a
    card nobody can actually borrow."""
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


# ---------------------------------------------------------------- honors

def _honor_state(full_state: dict, viewer_id) -> dict:
    """{"honors": {str(honor_id): create_time}, "last_checked_time"} -- seeded
    from the load blob's honor_info.honor_list; a bare account still gets the
    default epithet (capture: honor 100101 with a 1970 create_time)."""
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
    """Mutates an ALREADY-LOADED _honor_state dict in place -- True iff this
    call actually granted it (False if already owned). Caller owns
    persisting full_state, same in-place-helper pattern missions.
    mark_achieved uses. Split out of grant_honor() so honor/index's bulk
    auto-grant pass (honors.newly_earned_honor_ids) can batch every newly-
    met epithet into the SAME state_store.save_state call that request
    already makes at the end, instead of each one doing its own separate
    get_state/save_state round-trip (state.py's get_state does a fresh
    json.loads per call, not a shared reference -- a separate round-trip
    per grant would race the caller's own pending full_state save)."""
    if str(honor_id) not in st["honors"]:
        st["honors"][str(honor_id)] = _now()
        return True
    return False


def grant_honor(viewer_id, honor_id: int) -> bool:
    """Plain hook for other systems (career finish, missions, admin): earn an
    epithet. Validates against master honor_data; idempotent. Does its own
    load/save -- fine for a single ad-hoc grant, but NOT what honor/index's
    bulk pass uses (see _grant_honor_in_state)."""
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
    # Auto-grant every epithet whose real condition is now met (honors.py --
    # 402 of 549 real conditions, everything the 2026-08-19 extraction pass
    # resolved to a complete state hook). Real epithets have no claim step,
    # so this recomputes+grants on every load rather than needing a
    # receive-style endpoint. owned count is BEFORE this pass's own grants
    # (title_collector reads it), and every newly-met id is folded into the
    # SAME state_store.save_state below -- see _grant_honor_in_state.
    newly_earned = honors.newly_earned_honor_ids(
        viewer_id, full_state, set(st["honors"].keys()), len(st["honors"]))
    for hid in newly_earned:
        _grant_honor_in_state(st, hid)
    # mission_list is real now -- see missions.py for what's genuinely
    # tracked vs. honestly still exec_count 0. story_event_mission_list
    # stays empty: its rows key off story_event_id, whose own active window
    # lives in a master table this project hasn't inspected yet (see
    # missions.py's docstring) -- not worth guessing at. Built before the
    # save below so build_mission_list's own state (mission_state's claim
    # set, seeded on first access) actually persists.
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
    """honor/change_honor -- set the equipped/displayed epithet. Was the
    ENDPOINT_KEYS.md-flagged gap ("the honor EQUIP endpoint was never
    captured"), closed 2026-08-18 via a real capture (captures/20260818_
    122351/0031+0032_honor_change_honor.json, real server): request
    {honor_id}; response carries NO data payload at all -- just the bare
    result_code 1 envelope, notifications {trophy_badge_flag, circle_
    action_flag} (both real-server-only concerns -- circle isn't
    implemented here, and there's no separate "trophy badge" tracking
    beyond user/get_trophy_info -- so notifications stays the standard
    empty {} every other endpoint on this server already uses).

    Same "can only equip what you've earned" gate profile_card's honor_id
    already enforces, mirrored here since this IS the equip action that
    field only echoes."""
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


# --------------------------------------------------------- friend/index
# Capture ground truth (2026-08-16, real server, op 0051): empty request;
# response {last_friend_checked_time, friend_list, recommend_list,
# user_info_summary_list, follower_info_summary_list, follower_num}.
#
# The captured account's own friend_list was empty, but recommend_list /
# user_info_summary_list carried 30 entries each -- OTHER REAL PLAYERS'
# account summaries (name, fan count, rank score, their trained_chara...)
# that Cygames' matchmaking recommended. There is no honest way to populate
# that on a private server with no other real accounts behind it -- inventing
# fake players would be fabricating exactly the kind of data this project
# has been burned by before. So: friend_list/recommend_list/summary lists all
# serve empty (there is genuinely nothing to put in them, not a stub), while
# last_friend_checked_time is real per-viewer state, refreshed each call --
# the "dynamic and correct" part is that it's persisted and viewer-specific,
# not a frozen fixture, even though the social graph itself is necessarily
# empty.
FRIEND_STATE_KEY = "friend_state"


@registry.endpoint("friend/index")
def handle_friend_index(payload: dict) -> dict:
    """SERVERWIDE as of this change. The lists here used to be forced empty
    on purpose, and the reason was right at the time: there were no other
    real accounts, and fabricating players is a failure mode this project has
    been burned by. That premise expired -- this server hosts ~200 real
    accounts, and team_stadium.py already established the precedent that
    other-player features draw on REAL accounts on this server rather than
    invented ones.

    So friend_list/recommend_list/follower lists are now built from the
    actual follow graph (social.py) and real accounts' own published
    profiles (directory.py). Nothing is fabricated: an account that has
    never played has no directory card and is simply absent.

    single_mode_team.inject_friend_support_card's synthetic borrow-card
    lenders (admin.py's `add-friend-card`) still appear too, appended
    alongside the real ones -- they are not real accounts, so they are not
    resolved through the directory. They must keep showing up here or they
    stop counting as friends at all: user-reported 2026-08-18, Borrow Card
    filters its own list against your ACTUAL friend list, not just
    friend_support_card_data in isolation.

    last_friend_checked_time stays exactly as it was: real per-viewer state,
    refreshed on each call."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = full_state.get(FRIEND_STATE_KEY)
    if not isinstance(st, dict):
        st = {"last_checked_time": None}
        full_state[FRIEND_STATE_KEY] = st
    last = st.get("last_checked_time") or "0000-00-00 00:00:00"
    st["last_checked_time"] = _now()

    from . import friends, single_mode_team  # lazy: avoids a heavy top-level import
    injected = full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or []
    # build_index may reroll and store this viewer's recommend list, so the
    # save below has to come after it, not before.
    data = friends.build_index(viewer_id, full_state, copy.deepcopy(injected))
    state_store.save_state(viewer_id, full_state)

    data["last_friend_checked_time"] = last
    return _ok(data)


# --------------------------------------------------------- friend/search
# THE endpoint behind both the "Trainer Info" popup AND the Borrow Card
# picker's actual card data -- request carries friend_viewer_id (WHICH
# friend), completely separate from friend/index's list-level summaries.
# Was entirely unimplemented (fell through to the generic no-op -> a bare
# {} data payload) -- user-reported 2026-08-18: after friend/index and
# pre_single_mode/index's friend_support_card_data were both fixed to
# include the injected friend, "Trainer Info" still rendered blank
# (missing name, broken portrait icons) and Borrow Card still showed
# nothing. Root cause: this endpoint, not those, is what those screens
# actually pull from once you interact with a SPECIFIC friend rather than
# just browsing the list -- an empty {} here explains both symptoms at
# once regardless of how correct the list-level data was.
#
# Real shape confirmed via captures/20260818_122351/0015_friend_search.json
# and captures/20260816_140811/0012_friend_search.json (both real server,
# real accounts): response data = {friend_info, user_info_summary,
# practice_partner_info, directory_card_array, support_card_data,
# release_num_info, trophy_num_info, team_stadium_user, follower_num,
# own_follow_num, enable_circle_scout}. Only servable for a friend WE
# actually injected (single_mode_team.inject_friend_support_card) --
# refuses for anything else, since there's no real other account behind
# this server to look up.
def _real_friend_search(viewer_id, target_id) -> dict:
    """friend/search for a REAL other account on this server.

    Same response shape as the injected-lender path below (that shape is the
    capture-proven one), but every field comes from the target's own state:
    their directory card for the summary, their strongest horse for the
    practice partner and directory card array, their featured support card
    for support_card_data.

    release_num_info/trophy_num_info stay on the neutral template. Those are
    collection-completion counters the real payload carries, and this server
    does not track a per-account count for most of them -- serving a neutral
    row is honest about that, where deriving a plausible-looking number would
    not be. team_stadium_user IS derived, since team_stadium.py genuinely
    tracks it.
    """
    from . import directory, team_stadium, trained_chara
    from .. import social

    target = str(target_id)
    card = directory.cards_for([target]).get(target)
    if card is None:
        return _refuse()
    rel = social.friend_data(viewer_id, target)

    target_state = state_store.get_state(target) or {}
    roster = target_state.get(trained_chara.ROSTER_KEY) or []
    # The Star Umamusume is the horse THIS player set (user_info.partner_
    # chara_id, a trained_chara_id), not our pick of their strongest -- that
    # is what "the thing I can set and other people borrow" means, and
    # user/change_practice_partner is what sets it.
    partner = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    partner_block = copy.deepcopy(partner) if partner is not None else {}
    # The Archive: every umamusume whose career this account has run, each at
    # its best score. Clicking one opens that horse's detail page from the
    # full trained_chara carried inside the entry.
    directory_array = directory.archive_entries(roster)

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
        # Whether the VIEWER may scout this player into their circle: only if
        # the viewer leads or co-leads one and the target is unaffiliated.
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

    from . import single_mode_team, trained_chara  # lazy: avoids a heavy top-level import
    injected = full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or []
    match = next((e for e in injected if e["viewer_id"] == target_id), None)
    if match is None:
        # Not a synthetic lender -- try a REAL account on this server. This is
        # the serverwide half of the same change friend/index got: the popup
        # used to refuse for anything the player hadn't injected, because
        # there was nothing else to look up. Now there is.
        return _real_friend_search(viewer_id, target_id)

    usc = match["user_support_card"]
    # practice_partner_info needs a FULL TrainedChara (speed/stamina/skill_
    # array/succession refs/support_card_list/... -- far more than user_
    # info_summary's trimmed user_trained_chara carries). Reusing the real,
    # already-verified-rendering starter template (trained_chara.py's
    # _load_starter_trained_chara, the SAME data the Veteran Roster fix
    # confirmed actually displays correctly) rather than hand-building a
    # new structure from scratch -- it's unrelated to which card is being
    # lent (the real captures show these as two independent things: the
    # lender's OWN practice partner vs. the specific support card offered),
    # so any complete, real trained_chara works here.
    partner = trained_chara._load_starter_trained_chara(target_id)[0]

    return _ok({
        "friend_info": {"friend_viewer_id": target_id, "state": 1,  # confirmed correct, see inject_friend_support_card
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


# --------------------------------------------------------- photo/library
# Capture ground truth (2026-08-16, real server, op 0053): empty request;
# response {unique_id: "", unique_id_circle: ""} -- both empty strings even
# for a real, long-played account. Nothing to derive dynamically here; this
# is the response for every viewer.
@registry.endpoint("photo/library")
def handle_photo_library(payload: dict) -> dict:
    return _ok({"unique_id": "", "unique_id_circle": ""})


# ------------------------------------------------------------ profile card

PROFILE_CARD_STATE_KEY = "profile_card_state"

# UserProfileCardInfo's int-typed fields (dump.cs) other than chara_id/
# support_card_id, which get their own ownership/existence checks below.
_PROFILE_CARD_INT_FIELDS = (
    "dress_id", "bg_id", "card_bg_id", "theme_id", "illustration_type",
    "image_offset_x", "image_offset_y", "image_rotate", "image_scale",
)
_PROFILE_CARD_BOOL_FIELDS = (
    "is_trainer_info_aligned_right", "show_back_side", "show_trainer_id",
)
_MAX_COMMENT_LEN = 200          # server-defined bound (change_name has one too)
_FALLBACK_CHARA_ID = 1001       # matches jukebox_requests.py's own fallback chara


def _default_profile_card_info(full_state: dict, viewer_id) -> dict:
    """Before the player has ever customized a card: whichever character is
    already the home-screen favorite, else the first owned trained_chara,
    else the same fallback chara jukebox_requests.py uses. Everything else
    starts at its zeroed/off default."""
    home = (full_state.get(HOME_STATE_KEY) or {}).get("home_position_info") or {}
    chara_id = home.get("position1_chara_id")
    if not chara_id:
        roster = trained_chara._get_or_seed_roster(viewer_id)
        card_id = (roster[0].get("card_id") if roster else None) or 0
        chara_id = (card_id // 100) if card_id else _FALLBACK_CHARA_ID
    info = {"chara_id": chara_id, "support_card_id": 0, "comment": "",
            "image_file_hash": ""}
    info.update({k: 0 for k in _PROFILE_CARD_INT_FIELDS})
    info.update({k: False for k in _PROFILE_CARD_BOOL_FIELDS})
    info["show_trainer_id"] = True
    return info


def _profile_card_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(PROFILE_CARD_STATE_KEY)
    if isinstance(st, dict):
        return st
    st = {"info": _default_profile_card_info(full_state, viewer_id),
          "trained_chara_id": 0, "honor_id": 0}
    full_state[PROFILE_CARD_STATE_KEY] = st
    return st


@registry.endpoint("user/get_profile_card_info")
def handle_get_profile_card_info(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _profile_card_state(full_state, viewer_id)
    state_store.save_state(viewer_id, full_state)   # persist the seed on first read
    return _ok({
        "profile_card_info": copy.deepcopy(st["info"]),
        "image_file_status": 0, "image_file_url": "", "image_unique_id": "",
    })


@registry.endpoint("user/set_profile_card_info")
def handle_set_profile_card_info(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    incoming = payload.get("profile_card_info")
    if not isinstance(incoming, dict):
        return _refuse()

    chara_id = incoming.get("chara_id")
    if not isinstance(chara_id, int) or not master_data.query_one(
            "SELECT id FROM chara_data WHERE id=?", (chara_id,)):
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
            "comment": comment, "image_file_hash": ""}
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
        return _refuse()         # can only feature an honor you've earned

    st = _profile_card_state(full_state, viewer_id)
    st["info"] = info
    st["trained_chara_id"] = trained_chara_id
    st["honor_id"] = honor_id

    # Mirror onto user_info so every OTHER screen reading the login
    # snapshot (not just this endpoint) reflects the new card too -- same
    # pattern change_name/change_favorite_character already use.
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["leader_chara_id"] = chara_id
    user_info["leader_chara_dress_id"] = info["dress_id"]
    user_info["support_card_id"] = support_card_id
    user_info["comment"] = comment

    missions.mark_achieved(full_state, missions.FLAG_TRAINER_CARD_EDITED)
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


# --------------------------------------------------------------------------
# Profile odds and ends: comment, birthday, per-card outfits, story bookmarks,
# the per-player DMA toggle, and one trophy's detail popup.
#
# dump.cs shapes (none of these has ever been captured on this project, so the
# REQUEST fields are exact and the responses carry exactly the declared fields
# and nothing else):
#   changeCommentRequest              {comment}          -> {user_info}
#   UserSetBirthDayRequest            {birth_day}        -> {user_info}
#   UserChangeCardDressRequest        {card_id, dress_id}-> {}
#   UserResetCardDressRequest         {card_id}          -> {}
#   UserResetAllCardDressRequest      {}                 -> {}
#   UserChangeStoryFavoriteRequest    {story_favorite_array}
#                                        -> {story_favorite_array}
#   UserChangeDmaStateRequest         {target_viewer_id, dma_state} -> {}
#   GetTrophyDetailRequest            {trophy_id}        -> {user_trophy_info}
#
# `comment` and `birth_day` are both REAL UserInfo fields (checked against the
# dumped class), so both endpoints are plain writes through the same
# _ok_user_info path change_name/change_sex already use -- which is also what
# keeps load/index's derived fields from being clobbered on the way out.

CARD_DRESS_KEY = "card_dress_state"      # {str(card_id): dress_id}
STORY_FAVORITE_KEY = "story_favorite_state"
DMA_STATE_KEY = "dma_state"              # {str(target_viewer_id): dma_state}

_MAX_COMMENT_LEN = 200          # server-defined; the client caps lower.


def _valid_birth_day(value) -> str | None:
    """birth_day is a STRING on the wire (UserInfo.birth_day), and the only
    format any other date field in this project uses is ISO. Accepted as
    "MM-DD" or "YYYY-MM-DD" and normalised to what came in -- a real calendar
    date either way, so a junk string can never reach the client's own date
    parser. An empty string clears it."""
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
    """user/change_comment -- the trainer-card blurb other players see.

    The dumped class is `changeComment` with a LOWERCASE first letter, like
    its two already-implemented siblings changeLeaderCard and
    changePracticePartner (both of which this project serves at
    user/change_leader_card and user/change_practice_partner) -- which is what
    pins the path to user/change_comment rather than a bare top-level one.

    Republished through _save_and_republish because the comment is one of the
    fields another player's friend list renders (UserInfoAtFriend.comment), so
    a stale directory card would show the old blurb until this account's next
    login."""
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
    """user/set_birth_day -- the trainer's own birthday. Not republished: no
    friend-facing structure carries it (UserInfoAtFriend has no birth_day
    field), so it is a local profile field only."""
    viewer_id = payload["viewer_id"]
    birth_day = _valid_birth_day(payload.get("birth_day"))
    if birth_day is None:
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    user_info["birth_day"] = birth_day
    state_store.save_state(viewer_id, full_state)
    return _ok_user_info(full_state, user_info)


# ------------------------------------------------------------ card outfits

def _dress_ok(full_state: dict, dress_id: int, chara_id: int) -> bool:
    """The same availability rule change_favorite_character already enforces:
    an outfit must be a real dress_data row, and either owned, general-purpose,
    or belonging to this character. Anything else is another uma's costume."""
    dress = master_data.query_one(
        "SELECT chara_id, general_purpose FROM dress_data WHERE id=?", (dress_id,))
    if dress is None:
        return False
    owned = any(c.get("cloth_id") == dress_id
                for c in full_state.get("cloth_list_state") or [])
    return bool(owned or dress["general_purpose"] or dress["chara_id"] == chara_id)


def card_dress(full_state: dict, card_id: int) -> int:
    """The outfit the player chose for this card, or 0 if they never changed
    it (i.e. the card still wears whatever trained_chara.dress_for_card
    resolves for it)."""
    try:
        return int((full_state.get(CARD_DRESS_KEY) or {}).get(str(card_id)) or 0)
    except (TypeError, ValueError):
        return 0


def _sync_leader_dress(full_state: dict, viewer_id, chara_id: int,
                       dress_id: int) -> None:
    """If the card being redressed is the one standing on the trainer card,
    the leader's outfit follows it. Without this the picker would persist a
    choice that changes nothing anyone can see: no response in this client
    build carries a per-CARD dress field, so user_info.leader_chara_dress_id
    is the one place a per-card outfit is actually rendered."""
    user_info = _load_data(full_state, viewer_id).setdefault("user_info", {})
    if user_info.get("leader_chara_id") == chara_id:
        user_info["leader_chara_dress_id"] = dress_id


def _chara_of_card(card_id: int) -> int | None:
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    return row["chara_id"] if row else None


@registry.endpoint("user/change_card_dress")
def handle_change_card_dress(payload: dict) -> dict:
    """user/change_card_dress -- pick an outfit for one owned trainee card."""
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
    """user/reset_card_dress -- put one card back in its default costume
    (trained_chara.dress_for_card, the same authoritative card_rarity_data
    lookup every race outfit resolves through)."""
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
    """user/reset_all_card_dress -- clear every per-card outfit choice at
    once. The leader's own outfit is reset too, since it is the one the choice
    was rendered through (see _sync_leader_dress)."""
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


# ------------------------------------------------------- story bookmarks

def _story_favorite_rows(payload) -> list | None:
    """StoryFavorite[] = {episode_type, episode_id, is_mark}. Validated as a
    whole before anything is stored: a half-applied bookmark set would leave
    the client's own list disagreeing with what it just sent."""
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
    """user/change_story_favorite -- star/unstar story episodes.

    The request is a DELTA (only the rows the player just toggled), and the
    response is declared as the same StoryFavorite[] type -- so it returns the
    whole standing set, which is the only reading that lets the client render
    the list without a refetch. Rows with is_mark 0 are dropped rather than
    stored as un-starred entries: the set is "what is starred", so an
    unstarred episode is simply absent.

    episode_id is NOT validated against a master table: this client build
    bookmarks across several story families (chara, main, event, extra) that
    each live in their own table, and episode_type's enum values are not
    pinned by any capture here -- so guessing which table to check would
    refuse real bookmarks. The rows are inert display state either way."""
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


# -------------------------------------------------------------- DMA toggle

@registry.endpoint("user/change_dma_state")
def handle_change_dma_state(payload: dict) -> dict:
    """user/change_dma_state -- the per-player "direct message allowed" flag
    (dma_state) this account sets ON another viewer.

    Stored per target and served back nowhere, because nothing in this build's
    captured responses carries a dma_state field for us to fill -- but storing
    it is still the right answer: the alternative (main.py's NOOP_SUCCESS) is a
    toggle that reports success and forgets, so the next screen shows the old
    value. The target is NOT required to exist on this server -- the flag is
    this account's own preference about an id, not a claim about that id."""
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


# ------------------------------------------------------------ trophy detail

@registry.endpoint("user/get_trophy_detail")
def handle_get_trophy_detail(payload: dict) -> dict:
    """user/get_trophy_detail -- ONE trophy's popup, the same UserTrophyInfo
    row user/get_trophy_info serves in bulk.

    Built by reusing that endpoint's own merge (stored wins + wins recomputed
    off the live roster) and picking the requested row out of it, so the popup
    can never disagree with the shelf behind it. An unknown trophy_id, or one
    this account has not won, refuses -- there is no such popup to open.

    Unlike get_trophy_info this does NOT advance last_checked_time: that is
    the shelf's own "new trophy" badge clock, and opening one trophy is not
    the player having seen the rest."""
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
