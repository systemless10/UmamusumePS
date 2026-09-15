"""PRACTICE RACE -- everything around the race itself.

practice_race.py owns the one endpoint that matters most (race_start, which
actually simulates the race) plus get_follow_user_data. This module is the rest
of the screen: the saved-race shelf, the entry presets, and the practice-partner
system. Sixteen endpoints, none of them ever captured -- every request and
response shape here is read field-by-field off dump.cs
(PracticeRace*Request / PracticeRace*Response.CommonResponse) and the nested
types they name (PracticeRaceSavedRaceInfo, PracticeRacePresetInfo,
PracticePartnerOwnerInfo, PracticePartnerUsedHistory).

Because nothing is capture-backed, the rule throughout is: get the SHAPE exactly
right and never invent a success. Arrays are always present and always arrays
(never absent -- MessagePack C# leaves an absent array null, and the client
iterates these with no null guard; that exact bug is what made picking a track
softlock before get_follow_user_data existed). An action the state cannot
support answers 205 instead of pretending.

THE PARTNER SYSTEM, and why it is real here
-------------------------------------------
A practice partner is somebody else's finished career, borrowed to fill a gate.
Three ways to get one, all three live on this server:

  * your circle        -- search_partner's circle_user_partner_chara_array,
                          built from the real circle membership in social.py
  * the recommend list -- other accounts on this server, via directory
  * a partner ID       -- create_partner_id mints a shareable code for ONE of
                          your own trained charas; get_partner_info resolves
                          somebody else's code by scanning published lobbies

That last one is why this is not a stub feature: get_partner_id and
create_partner_id are two of the endpoints the live client has actually been
caught posting here (5 hits each in server.log, both answered with a no-op), so
the borrow flow is reachable in normal play.

HOUSE NUMBERS. Three limits below have no master.mdb table behind them and no
capture to read them off (SAVED_RACE_LIMIT, PARTNER_ID_LIMIT, PARTNER_LIMIT).
They are labelled where they are defined. Everything else is derived from state.
"""

from __future__ import annotations

import copy
import logging
import random

from .. import social
from .. import state as state_store
from . import directory, registry, trained_chara

log = logging.getLogger("uma-server")

STATE_KEY = "practice_race_lobby"

# --- house numbers (see the module docstring) --------------------------------
# How many finished races the shelf holds. Overflow evicts the oldest
# NON-favourite, never a favourite and never the one just run.
SAVED_RACE_LIMIT = 20
# How many of your own trained charas may have a live partner ID at once.
PARTNER_ID_LIMIT = 5
# How many borrowed partners you may keep.
PARTNER_LIMIT = 30
# How many other accounts the recommend list offers.
RECOMMEND_SIZE = 10

_OK_STATE = 1
_FAIL_STATE = 0


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _blank() -> dict:
    return {"next_race_id": 1, "next_preset_id": 1, "saved": [], "pending": None,
            "presets": [], "partners": [], "partner_ids": {}, "used_history": []}


def state(full_state: dict) -> dict:
    """The lobby sub-state, created on demand and forward-migrated. Callers own
    saving."""
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, copy.deepcopy(v))
    return st


def _load(payload: dict):
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    return viewer_id, full_state, state(full_state)


# ============================================================ saved races ===
#
# race_start stashes the race it just ran as `pending`; race_end is what decides
# whether it goes on the shelf. Splitting it that way is the client's own flow
# (you watch the race, THEN choose "save"), and it is why race_start no longer
# mints a throwaway random practice_race_id.

def remember_race(viewer_id, request: dict, response_data: dict) -> int:
    """Called by practice_race.handle_race_start. Stashes the finished race as
    the pending one and returns the practice_race_id to report for it.

    The whole response payload is kept, not just a summary: race_replay has to
    hand back trained_chara_array / race_result_info / entry_info_array
    verbatim, and re-simulating would produce a DIFFERENT race than the one the
    player watched."""
    full_state = state_store.get_state(viewer_id) or {}
    st = state(full_state)
    race_id = int(st["next_race_id"])
    st["next_race_id"] = race_id + 1
    entries = request.get("entry_chara_array") or []
    st["pending"] = {
        "practice_race_id": race_id,
        "race_instance_id": request.get("race_instance_id") or 0,
        "entry_num": int(request.get("entry_num") or len(entries) or 0),
        "user_entry_num": len(entries),
        "is_favorite": 0,
        "save_time": social.now(),
        # The conditions the race was set up with -- race_replay reports these
        # as before_* so the replay screen can redraw the setup it ran under.
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
    """One PracticeRaceSavedRaceInfo row -- the shelf listing, without the race
    payload hanging off it."""
    return {"practice_race_id": entry["practice_race_id"],
            "race_instance_id": entry["race_instance_id"],
            "entry_num": entry["entry_num"],
            "user_entry_num": entry["user_entry_num"],
            "is_favorite": entry["is_favorite"],
            "save_time": entry["save_time"]}


@registry.endpoint("practice_race/index")
def handle_index(payload: dict) -> dict:
    """PracticeRaceIndexResponse{state, practice_race_id} -- the resume probe
    the screen opens with.

    INFERRED: with only two ints and no capture, the reading that fits is "is
    there a race waiting to be saved, and which one" -- the same pair race_start
    answers with, where state is 1 and practice_race_id names the race just run.
    So: a pending race reports itself, and an empty shelf reports 0/0."""
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
    """PracticeRaceRaceEndRequest{is_save, overwrite_race_id} -> {state}.

    is_save 0 discards the race that was just watched -- still a success, the
    player simply chose not to keep it. overwrite_race_id names a shelf slot to
    replace (that is what the client offers when the shelf is full); 0 appends.

    Refuses when there is no pending race: answering "saved" with nothing to
    save would put a row on the shelf that replays to an empty screen."""
    viewer_id, full_state, st = _load(payload)
    pending = st.get("pending")
    if not isinstance(pending, dict):
        log.warning("practice_race/race_end: nothing pending to save")
        return _ok({"state": _FAIL_STATE})

    st["pending"] = None
    if not int(payload.get("is_save") or 0):
        state_store.save_state(viewer_id, full_state)
        return _ok({"state": _OK_STATE})

    overwrite = int(payload.get("overwrite_race_id") or 0)
    saved = st["saved"]
    slot = next((i for i, e in enumerate(saved)
                 if e["practice_race_id"] == overwrite), None) if overwrite else None
    if slot is not None:
        saved[slot] = pending
    else:
        saved.append(pending)
        while len(saved) > SAVED_RACE_LIMIT:
            victim = next((i for i, e in enumerate(saved) if not e["is_favorite"]), None)
            if victim is None:
                # Everything is favourited -- drop the OLDEST rather than the
                # race just run, which is the one the player asked to keep.
                victim = 0
            dropped = saved.pop(victim)
            log.info("practice_race: shelf full (%s), dropped race %s",
                     SAVED_RACE_LIMIT, dropped["practice_race_id"])
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
    """Response echoes {practice_race_id, is_favorite} -- so the echo has to be
    what was actually stored, not what was asked for. An unknown race refuses
    rather than echoing a flag nothing holds."""
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
    """The stored race, played back verbatim. The race_scenario blob and its
    random_seed are what the client renders from, so this MUST be the same
    bytes the player watched the first time -- re-simulating would quietly show
    a different race under the same name."""
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


# ================================================================ presets ===

def _preset_wire(preset: dict) -> dict:
    return {"preset_id": preset["preset_id"], "name": preset.get("name") or "",
            "preset_chara_array": preset.get("preset_chara_array") or []}


@registry.endpoint("practice_race/get_preset_array")
def handle_get_preset_array(payload: dict) -> dict:
    _, _, st = _load(payload)
    return _ok({"preset_info_array": [_preset_wire(p) for p in st["presets"]]})


@registry.endpoint("practice_race/save_preset")
def handle_save_preset(payload: dict) -> dict:
    """PracticeRaceSavePresetRequest{preset_info_array:[{preset_id,
    preset_chara_array}]} -- the client sends the whole set it wants stored.

    preset_id 0 means "new". The response carries more than the presets: it also
    re-states the borrowed partners (add_partner_info_array /
    practice_partner_owner_info_array), because a preset can reference somebody
    else's horse and the screen redraws those from this same response.

    is_excluded_flag reports that at least one entry could NOT be resolved to a
    horse any more -- a borrowed partner that has since been deleted, say. That
    is a real condition worth reporting honestly: the alternative is a preset
    that silently loads one horse short."""
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


# =============================================================== partners ===

def _owner_info(me, owner_viewer_id, chara: dict, owner_name: str = "") -> dict:
    """PracticePartnerOwnerInfo{partner_trained_chara_id, owner_viewer_id,
    owner_name, owner_trained_chara_id, friend_state}.

    partner_trained_chara_id and owner_trained_chara_id are the same horse seen
    from the two sides -- the borrower's handle for it and the owner's own id
    for it. Nothing here re-keys borrowed horses, so they are equal; keeping
    both fields rather than one is what the wire declares."""
    tid = chara.get("trained_chara_id") or 0
    return {"partner_trained_chara_id": tid,
            "owner_viewer_id": social._wire_id(owner_viewer_id),
            "owner_name": owner_name or "",
            "owner_trained_chara_id": tid,
            "friend_state": social.friend_data(me, owner_viewer_id)["state"]}


def _best_of(viewer_id, rosters: dict, cards: dict):
    """(chara, owner_name) for one account's offered partner: the horse they
    have SET as their Star Umamusume, falling back to their strongest. Same
    choice directory makes for every other borrow surface, so the horse a player
    offers is consistent across circle, recommend and partner-ID."""
    roster = rosters.get(str(viewer_id)) or []
    if not roster:
        return None, ""
    card = cards.get(str(viewer_id)) or {}
    chara = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    return chara, card.get("name") or ""


@registry.endpoint("practice_race/search_partner")
def handle_search_partner(payload: dict) -> dict:
    """PracticeRaceSearchPartnerResponse{circle_user_partner_chara_array,
    recommend_partner_chara_array, practice_partner_owner_info_array}.

    Both lists are REAL accounts on this server, not filler: the circle list is
    the caller's actual circle membership out of social.py, and the recommend
    list is other published accounts out of directory. Anyone already in the
    circle list is kept out of the recommend list so the same trainer is not
    offered twice on one screen.

    The owner array covers BOTH lists -- it is the lookup the client uses to put
    a name against whichever horse gets picked, so it has to span everything on
    offer."""
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
    """Borrow one horse and keep it. Stores a SNAPSHOT rather than a reference:
    the owner can retire or overwrite that career tomorrow, and a saved partner
    that silently changes stats between races would make every replay on the
    shelf a lie about what ran."""
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
        return _refuse()
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
    """Returns what is LEFT (practice_partner_chara_array), not what went -- the
    screen redraws the whole list from this."""
    viewer_id, full_state, st = _load(payload)
    drop = {int(t) for t in (payload.get("target_trained_chara_id_array") or []) if t}
    st["partners"] = [p for p in st["partners"]
                      if p["chara"].get("trained_chara_id") not in drop]
    state_store.save_state(viewer_id, full_state)
    return _ok({"practice_partner_chara_array": [copy.deepcopy(p["chara"])
                                                 for p in st["partners"]]})


# ------------------------------------------------------------ partner IDs --

def _issued(st, tid) -> dict | None:
    return (st.get("partner_ids") or {}).get(str(tid))


@registry.endpoint("practice_race/get_partner_id")
def handle_get_partner_id(payload: dict) -> dict:
    """Live-hit 5x against the no-op fallback before this existed.

    partner_id 0 means "this horse has no code yet", which is the client's cue
    to offer create_partner_id -- so an un-minted horse is a normal answer here,
    not a refusal.

    is_already_circle_post_chara is answered from the real circle post list
    (circles._post_partner_array offers each member's Star Umamusume), so the
    screen does not offer to post a horse that is already up there."""
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
        # Nothing on this server meters how many times a day a horse may be
        # posted, so the count is what has actually been minted, not a quota.
        "daily_post_partner_count": len(st["partner_ids"]),
        "is_already_circle_post_chara": 1 if _is_circle_posted(viewer_id, tid) else 0,
    })


def _is_circle_posted(viewer_id, tid) -> bool:
    """Is this horse the one the caller's circle already shows for them?"""
    circle = social.circle_of(str(viewer_id))
    if not circle:
        return False
    roster = trained_chara._get_or_seed_roster(viewer_id) or []
    card = directory.cards_for([str(viewer_id)]).get(str(viewer_id)) or {}
    best = directory._partner_trained_chara(roster, card.get("partner_chara_id") or 0)
    return bool(best) and best.get("trained_chara_id") == tid


@registry.endpoint("practice_race/create_partner_id")
def handle_create_partner_id(payload: dict) -> dict:
    """Mint a shareable code for one of YOUR OWN trained charas. Live-hit 5x
    against the no-op fallback before this existed.

    Refuses for a horse that is not in the caller's roster -- minting a code for
    somebody else's career would let get_partner_info hand it out under the
    wrong owner. Re-minting a horse that already has a code returns the existing
    one rather than orphaning it: any code already shared has to keep resolving.
    """
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
        return _refuse()

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
    """Resolve somebody else's partner code. This is the one endpoint here that
    reads ACROSS accounts: it scans every lobby state on the server for the code.

    register_time is part of the request, and is checked -- it is what stops a
    recycled id from resolving to whatever horse holds that number now. A code
    whose owner has since deleted the career refuses rather than returning an
    empty horse."""
    viewer_id, _, _ = _load(payload)
    me = str(viewer_id)
    partner_id = int(payload.get("partner_id") or 0)
    want_time = payload.get("register_time") or ""
    if not partner_id:
        return _refuse()

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
                return _refuse()
            name = (directory.cards_for([str(owner_id)]).get(str(owner_id))
                    or {}).get("name") or ""
            return _ok({"practice_partner_info": copy.deepcopy(chara),
                        "practice_partner_owner_info":
                            _owner_info(me, owner_id, chara, name)})
    log.info("practice_race: partner id %s not found", partner_id)
    return _refuse()


# ============================================================ use history ===

_HISTORY_LIMIT = 50
_HISTORY_TYPE_PARTNER = 1


def _record_partner_use(st: dict, viewer_id, entries) -> None:
    """Log every BORROWED horse a race used, for used_history. Own horses are
    not logged -- the screen exists to show who has been borrowing whom."""
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
    """PracticeRaceUsedHistoryRequest{target_trained_chara_id} -> the times that
    ONE horse was used, plus a total. A target of 0 asks for everything."""
    _, _, st = _load(payload)
    tid = payload.get("target_trained_chara_id")
    rows = st["used_history"]
    if tid:
        rows = [h for h in rows if h.get("trained_chara_id") == tid]
    return _ok({"practice_used_history_array": copy.deepcopy(rows),
                "used_count_total": len(rows)})
