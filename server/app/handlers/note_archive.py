"""Uma Note / archive (Global's "directory") -- the trained-uma encyclopedia.

  note/index      -> the whole archive screen: per-card best trained charas,
                     archive point score + rank (directory "level"), category
                     score breakdown, and rank-up rewards.
  note/save_voice -> voice-line sync/save from the note's voice player.

Capture ground truth (request stored in file N-1, response in file N):
  note/index      : 20260717_180912 op 0042, 20260723_134126 op 0036 (a
                    then-fresh account -- the calibration capture), and
                    20260726_122439 op 0008.
  note/save_voice : 20260717_183629 op 0004, 20260721_132421 op 0060,
                    20260721_145741 op 0025 (all three respond
                    {state: 0, practice_race_id: 0}; requests carry
                    add_voice_data_array just like note/index).

XP / rank model [mdb]: master.mdb `directory` (200 rows: id == rank_level,
required_point, item_category_1/item_id_1/item_num_1). Rank = highest row whose
required_point <= the archive score. Rewards are per-rank (mostly 50-75 carats,
category 90 item 43); reward_info_array carries the item totals for every rank
newly reached since the last grant. VERIFIED against the fresh-account capture:
before_directory_level 1 -> score 140300 -> rank 12, and the sum of directory
rewards for ranks 2..12 is exactly the captured {item_type 90, item_id 43,
item_num 550}. Granted ranks are tracked in note_state so a reward can never
be granted twice.

Scoring -- SERVER-DEFINED APPROXIMATION. The real score is computed from the
account's collections; we mirror that from OUR per-viewer state with per-unit
values calibrated on the two captures (each category listed with its source):
  album (650/entry), act (750/entry), voice (75/line), nickname (100/title)
  are exact flat fits of the captures; gallery fits score = (number+1) * 125
  in BOTH captures; story is flat 400 (real per-story values vary, no flat
  fit exists: 476 stories -> 183600); directory_card is the sum of the best
  trained rank_score per owned card (the whale account's 1.38M over 157 cards
  is consistent with this). rank_score (the archive total) = directory_card +
  every category score -- an exact identity in all three captures.

What each category COUNTS here (server-defined mapping onto our containers):
  album    = trainee cards owned (card_collection)
  story    = stories first-cleared (story_state chara_cleared + main_cleared)
  act      = support cards owned (support_card_collection)
  voice    = voice lines heard (accumulated from add_voice_data_array)
  nickname = distinct race epithets across the veteran roster
  gallery  = outfits owned (cloth_list_state)
"""

from __future__ import annotations

import copy
import time

from .. import epithets
from .. import master_data
from .. import state as state_store
from . import bond, collection, registry, trained_chara

NOTE_STATE_KEY = "note_state"

# Per-unit archive points (see module docstring for the capture calibration).
_ALBUM_UNIT = 650
_STORY_UNIT = 400
_ACT_UNIT = 750
_VOICE_UNIT = 75
_NICKNAME_UNIT = 100
_GALLERY_UNIT = 125         # score = (number + 1) * 125 in both captures


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _note_state(full_state: dict) -> dict:
    st = full_state.get(NOTE_STATE_KEY)
    if not isinstance(st, dict):
        st = {
            "voices": {},              # "chara_id:data_id" -> first-heard time
            "home_stories": [],        # home story ids synced by the client
            "granted_rank": 1,         # highest rank whose reward was granted
            "level": 1,                # last SERVED rank -> next call's before_
            "scenario_high_scores": {},  # str(scenario_id) -> highest rank_score
        }
        full_state[NOTE_STATE_KEY] = st
    return st


def _absorb_sync(note: dict, payload: dict) -> None:
    """Both note endpoints batch newly heard voice lines / opened home stories
    onto the request (add_voice_data_array / add_home_story_data_array in every
    capture); fold them into note_state so the voice score grows."""
    voices = note.setdefault("voices", {})
    now = _now()
    for v in payload.get("add_voice_data_array") or []:
        if isinstance(v, dict) and v.get("chara_id") and v.get("data_id"):
            # The client never sends a time (see the note/index captures), so
            # the FIRST sync of a line is its create_time -- exactly what the
            # real server reports back on note/get_new_chara_data (capture
            # 20260816_202505: line 1007:95003 synced by op 0021 at 01:29:41
            # comes back with that timestamp on op 0025). Never re-stamp a
            # line we already know: a re-sync must not reset its date.
            voices.setdefault(f"{v['chara_id']}:{v['data_id']}", now)
    stories = note.setdefault("home_stories", [])
    for s in payload.get("add_home_story_data_array") or []:
        sid = s.get("id") if isinstance(s, dict) else s
        if isinstance(sid, int) and sid not in stories:
            stories.append(sid)


def _best_per_card(roster: list) -> dict:
    """card_id -> the highest-rank_score trained record (the directory shows
    one best veteran per card)."""
    best: dict = {}
    for rec in roster:
        cid = rec.get("card_id")
        if not cid:
            continue
        if cid not in best or (rec.get("rank_score") or 0) > (best[cid].get("rank_score") or 0):
            best[cid] = rec
    return best


def _score_summary(full_state: dict, note: dict, roster: list) -> dict:
    """The archive point breakdown, computed from the viewer's actual account
    state (see module docstring for the mapping + calibration)."""
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
    directory_card = sum((rec.get("rank_score") or 0)
                         for rec in _best_per_card(roster).values())
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
    """rank_score = directory_card + every category score (exact identity in
    all three captures)."""
    return summary["directory_card"] + sum(
        summary[k]["score"] for k in ("album", "story", "act", "voice",
                                      "nickname", "gallery"))


def _rank_for(score: int) -> int:
    row = master_data.query_one(
        "SELECT MAX(id) AS r FROM directory WHERE required_point <= ?", (score,))
    return (row["r"] if row and row["r"] else None) or 1


def _pending_rank_rewards(note: dict, rank: int) -> list:
    """Every (category, item_id, num) newly due for ranks not yet granted,
    aggregated per item (matching the captured reward_info_array shape).
    Pure -- no state mutation, so the caller can send these to the mailbox
    BEFORE marking them granted."""
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
    """Send every newly-reached rank's directory reward to the viewer's
    MAILBOX (present box) rather than crediting the wallet/inventory
    directly. User-corrected 2026-08-19: 'Its supposed to send your archive
    rewards to your mailbox for you to claim' -- this module previously
    auto-applied fcoin/items straight into state, the same 'silent instant
    credit' mistake team_stadium.py's own rank rewards just made too (see
    that module's own _grant_rank_rewards for the mirrored fix). The wallet
    genuinely updated either way, which is why an earlier look at this
    account's state (granted_rank already caught up) read as 'this already
    works' -- the actual gap was that nothing ever showed up as a claimable
    present, so there was never a mailbox notification or claim animation,
    which is what actually reads as 'doesn't reward' to a player.

    presents.admin_send does its own independent read-modify-write (see its
    own docstring, same reasoning login_bonus.py's apply_and_report already
    documents) -- so this returns the aggregated reward_info_array for the
    RESPONSE (still real-capture-verified shape) but does NOT touch
    full_state itself; the caller must re-fetch full_state after this
    returns before doing anything else with it, exactly like login_bonus.py's
    own call site."""
    from . import presents  # lazy: avoids a top-level import cycle
    rewards = _pending_rank_rewards(note, rank)
    if not rewards:
        return []
    for cat, iid, num in rewards:
        presents.admin_send(viewer_id, item_category=cat, item_id=iid, item_num=num,
                            message=f"Uma Archive Rank {rank} Reward")
    note["granted_rank"] = rank
    return [{"item_type": cat, "item_id": iid, "item_num": num} for cat, iid, num in rewards]


_release_cache: list | None = None


def _release_card_array() -> list:
    """Cards visible in the archive. The real list is game-wide (identical 92
    ids in a fresh-account and a whale-account capture) = released trainee
    cards. Approximation: every card_data id below the 9M trial-card band --
    95 ids vs the real 92 (three not-yet-released alt cards slip in; harmless
    on a private server, they just show as viewable entries)."""
    global _release_cache
    if _release_cache is None:
        _release_cache = [r["id"] for r in master_data.query(
            "SELECT id FROM card_data WHERE id < 9000000 ORDER BY id")]
    return list(_release_cache)


def _directory_card_array(viewer_id, roster: list) -> list:
    """One entry per owned card: the best trained veteran, ranked by
    rank_score (directory_ranking 1 = best). Row shape straight from the
    capture: {card_id, directory_ranking, trained_chara: {full record +
    viewer_id + directory_ranking}}."""
    ordered = sorted(_best_per_card(roster).values(),
                     key=lambda r: -(r.get("rank_score") or 0))
    out = []
    for i, rec in enumerate(ordered, start=1):
        tc = copy.deepcopy(rec)
        tc["viewer_id"] = viewer_id
        tc["directory_ranking"] = i
        out.append({"card_id": rec["card_id"], "directory_ranking": i,
                    "trained_chara": tc})
    return out


@registry.endpoint("note/get_nickname_data")
def handle_get_nickname_data(payload: dict) -> dict:
    """note/get_nickname_data -- the epithets (nicknames) earned for one chara.

    Shape from the Il2Cpp dump AND a real capture of the live Cygames server:
      NoteGetNicknameDataRequest  { int chara_id }
      ...Response.CommonResponse  { int[] nickname_id_array }
    A flat array of unlocked ids; the client resolves names itself. Ids come
    from app/epithets.py, awarded at career finish."""
    viewer_id = payload["viewer_id"]
    chara_id = payload.get("chara_id") or 0
    full_state = state_store.get_state(viewer_id) or {}
    return _ok({"nickname_id_array": epithets.owned_ids(full_state, chara_id)})


@registry.endpoint("note/get_scenario_record")
def handle_get_scenario_record(payload: dict) -> dict:
    """note/get_scenario_record -- the career scenario record page.

    Shape from the Il2Cpp dump:
      NoteGetScenarioRecordRequest { int scenario_id }
      ...CommonResponse           { ScenarioRecord[] scenario_record_array }
      ScenarioRecord              { scenario_id, directory_ranking, trained_chara }

    A per-scenario leaderboard of trained charas -- the same thing note/index's
    directory_card_array builds, filtered to one scenario and re-ranked within
    it. Built from real career graduates only; the seeded fixture veterans are
    not this account's runs. scenario_id 0/absent means "every scenario"."""
    viewer_id = payload["viewer_id"]
    wanted = payload.get("scenario_id") or 0
    roster = trained_chara._get_or_seed_roster(viewer_id)
    genuine = [c for c in roster
               if trained_chara.PLAYER_CAREER_ID_BASE
               <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE
               and (not wanted or c.get("scenario_id") == wanted)]
    genuine.sort(key=lambda r: -(r.get("rank_score") or 0))
    records = []
    for i, rec in enumerate(genuine, start=1):
        tc = copy.deepcopy(rec)
        tc["viewer_id"] = viewer_id
        tc["directory_ranking"] = i
        records.append({"scenario_id": rec.get("scenario_id") or wanted,
                        "directory_ranking": i, "trained_chara": tc})
    return _ok({"scenario_record_array": records})


@registry.endpoint("note/index")
def handle_note_index(payload: dict) -> dict:
    """note/index: request {add_voice_data_array, add_home_story_data_array,
    request_directory_card}. Response keys exactly as captured."""
    viewer_id = payload["viewer_id"]
    # Roster first: _get_or_seed_roster does its own read-modify-write, so it
    # must finish before we snapshot the state we mutate below.
    roster = trained_chara._get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    _absorb_sync(note, payload)

    before_level = note.get("level") or 1
    summary = _score_summary(full_state, note, roster)
    rank_score = _total_score(summary)
    rank = _rank_for(rank_score)

    # Persist the absorbed sync (voices/home_stories) and the served level
    # BEFORE any mailbox sends below -- those each do their OWN independent
    # fetch/save (see _grant_rank_rewards's docstring), so saving again
    # after them with this stale full_state would clobber the present-box
    # entries they just wrote.
    note["level"] = rank
    state_store.save_state(viewer_id, full_state)

    rewards = _grant_rank_rewards(viewer_id, note, rank)
    if rewards:
        # granted_rank changed on THIS note dict, but the save above already
        # ran before admin_send's own writes -- re-fetch fresh and persist
        # just that field rather than clobbering what admin_send committed.
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
            {"scenario_id": int(sid), "highest_rank_score": score}
            for sid, score in sorted(
                (note.get("scenario_high_scores") or {}).items(),
                key=lambda kv: int(kv[0]))],
        "directory_card_array": (_directory_card_array(viewer_id, roster)
                                 if payload.get("request_directory_card") else []),
    })


@registry.endpoint("note/save_voice")
def handle_save_voice(payload: dict) -> dict:
    """note/save_voice: persists the voice lines the request syncs
    (add_voice_data_array, same wrapper as note/index). All three captures
    respond {state: 0, practice_race_id: 0} verbatim -- echoed here."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    _absorb_sync(note, payload)
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": 0, "practice_race_id": 0})


# ---------------------------------------------------- the character page --
#
# note/get_new_chara_data is the per-character detail page in the note (the
# Global "directory"): request {chara_id}, response
#   {dress_id, mini_dress_id, voice_data_array, valentine_special_data_array,
#    update_user_chara_array, new_chara_profile_array}
# Ground truth: 20260816_202505 op 0025/0027 (chara 1007, one heard line),
# 20260903_204408 op 0004/0005 and 20260904_232507 op 0067 (charas 1061/1068
# on a whale account, 123 heard lines and a real outfit).
#
# note/trainer_note is the same screen's entry point with NO request fields at
# all, answering the account-wide half of the same payload
# (update_user_chara_array + new_chara_profile_array; 20260816_202505 ops
# 0023/0026/0028), and note/save_note_data (op 0024) is its save, which the
# real server answers with a data_headers-only envelope -- no `data` key.


def _voice_rows(note: dict, chara_id: int) -> list:
    """This character's heard voice lines, wire shape. Times come from
    note_state; a line recorded before voices held timestamps is backfilled to
    now by the caller's save so it stops being dateless."""
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
        if not isinstance(when, str):        # legacy value (1) -- backfill
            when = voices[key] = now
        rows.append({"chara_id": chara_id, "data_id": data_id,
                     "create_time": when,
                     # The line is in this array *because* the player already
                     # heard it, which is exactly when the real server drops
                     # the badge: new_flag is 0 in every captured row.
                     "new_flag": 0})
    rows.sort(key=lambda r: r["data_id"])
    return rows


def _chara_row(full_state: dict, chara_id: int) -> dict:
    """This character's chara_collection row, or {} -- NOT bond.chara_entry,
    which creates a row as a side effect; opening the page of a character the
    account does not own must not fabricate one."""
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if not isinstance(charas, list):
        return {}
    return next((c for c in charas
                 if isinstance(c, dict) and c.get("chara_id") == chara_id), {})


def _account_profile_entries(full_state: dict) -> list:
    """Profile rows unlocked but never announced, across every character the
    account has a bond row for -- the account-wide new_chara_profile_array.
    bond.new_profile_entries records what it returns, so the badge is shown
    once and never again."""
    charas = full_state.get(collection.CHARA_LIST_KEY)
    out = []
    for entry in (charas if isinstance(charas, list) else []):
        if isinstance(entry, dict) and entry.get("chara_id"):
            out += bond.new_profile_entries(full_state, entry["chara_id"])
    return out


@registry.endpoint("note/get_new_chara_data")
def handle_get_new_chara_data(payload: dict) -> dict:
    """note/get_new_chara_data -- one character's page in the note."""
    viewer_id = payload["viewer_id"]
    chara_id = int(payload.get("chara_id") or 0)
    full_state = state_store.get_state(viewer_id) or {}
    note = _note_state(full_state)
    # The page's voice player syncs through note/save_voice, but the client
    # batches pending lines onto whatever note call goes out first -- absorb
    # them here too so nothing is lost.
    _absorb_sync(note, payload)

    chara = _chara_row(full_state, chara_id)
    voice_rows = _voice_rows(note, chara_id)
    # Owned characters only: announcing profile text for a character with no
    # bond row would badge rows the player cannot even read yet.
    profiles = (bond.new_profile_entries(full_state, chara_id) if chara else [])

    state_store.save_state(viewer_id, full_state)
    return _ok({
        # Her home / mini (lobby) outfit. bond.CHARA_LIST_TEMPLATE's default 2
        # is the same pair every captured un-dressed character reports.
        "dress_id": chara.get("dress_id") or 2,
        "mini_dress_id": chara.get("mini_dress_id") or 2,
        "voice_data_array": voice_rows,
        # No valentine_special master table exists in this build's master.mdb
        # and the key is empty in every capture -- served empty, not invented.
        "valentine_special_data_array": [],
        # Rows the SERVER changed during this call. We change none (the page is
        # read-only), which is why every capture has it empty.
        "update_user_chara_array": [],
        "new_chara_profile_array": profiles,
    })


@registry.endpoint("note/trainer_note")
def handle_trainer_note(payload: dict) -> dict:
    """note/trainer_note -- the note screen opening. No request fields; the
    account-wide new-badge half of the character page."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    profiles = _account_profile_entries(full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({"update_user_chara_array": [], "new_chara_profile_array": profiles})


@registry.endpoint("note/save_note_data")
def handle_save_note_data(payload: dict) -> dict:
    """note/save_note_data -- the note screen's save. The captured response
    carries data_headers and NO `data` key at all; mirrored verbatim."""
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}}


# --------------------------------------------------------------------------
# dump.cs shapes:
#   NoteGetDirectoryCardRequest  {card_id} -> {directory_card_array}
#   NoteSaveCharaDataRequest     {chara_id, dress_id, mini_dress_id} -> {}
#
# NOT added here: NoteLoadRequest. Its fields are
# {add_voice_data_array, add_home_story_data_array, request_directory_card}
# and its response is {reward_info_array, before_directory_level, rank_score,
# release_card_array, score_summary, scenario_record_highest_score_array,
# directory_card_array} -- i.e. it IS the class behind note/index above, not a
# second endpoint. An audit that reads class names alone lists it as missing;
# the field sets say otherwise.


@registry.endpoint("note/get_directory_card")
def handle_get_directory_card(payload: dict) -> dict:
    """note/get_directory_card -- ONE card's archive page, the single-card
    read of what note/index serves in bulk when request_directory_card is set.

    Built from the same _directory_card_array so the page and the grid behind
    it can never disagree about which veteran is a card's best, then filtered
    to the requested card. The response type is still an ARRAY (DirectoryCard[])
    -- a card the account has no trained veteran for is legitimately empty,
    which is why this serves [] rather than refusing. An unknown card_id does
    refuse: there is no such page."""
    viewer_id = payload["viewer_id"]
    card_id = payload.get("card_id")
    if not isinstance(card_id, int) or isinstance(card_id, bool):
        return _refuse()
    if not master_data.query_one("SELECT id FROM card_data WHERE id=?", (card_id,)):
        return _refuse()
    roster = trained_chara._get_or_seed_roster(viewer_id)
    cards = [c for c in _directory_card_array(viewer_id, roster)
             if c.get("card_id") == card_id]
    return _ok({"directory_card_array": cards})


@registry.endpoint("note/save_chara_data")
def handle_save_chara_data(payload: dict) -> dict:
    """note/save_chara_data -- the outfit pair shown on a character's note
    page, saved from that page's dress pickers.

    Writes the same two fields note/get_new_chara_data reads back
    (chara_collection's dress_id / mini_dress_id), so the choice survives
    reopening the page -- the whole point of the endpoint, and exactly what
    main.py's NOOP_SUCCESS fallback was silently discarding.

    Only a character the account actually has a row for can be dressed, and
    only in an outfit that is real, and hers (or general-purpose) -- the same
    rule user_profile.handle_change_favorite_character enforces for the home
    screen, and for the same reason: dress_data rows belong to characters."""
    viewer_id = payload["viewer_id"]
    chara_id = payload.get("chara_id")
    if not isinstance(chara_id, int) or isinstance(chara_id, bool):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    chara = _chara_row(full_state, chara_id)
    if not chara:
        return _refuse()        # not an owned character -- no page to save

    for field in ("dress_id", "mini_dress_id"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return _refuse()
        # 2 is the universal default every un-dressed character reports (see
        # handle_get_new_chara_data), and has no dress_data row of its own.
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
