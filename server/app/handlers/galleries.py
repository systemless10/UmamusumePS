"""Gallery, theatre-preset, serial-code and profile-stat endpoints.

The odds and ends of Tier 1: small, self-contained calls that hang off features
already built. Grouped here rather than scattered because each is a handful of
lines and none of them owns enough state to deserve a module.

  gallery/play_event              replay one career event from the gallery
  gallery/save_gallery_data       persist the gallery's own view state
  talk_gallery/index              the voice/talk gallery list
  photo/set_activity              photo-mode pose selection
  note/use_gallery_key            spend gallery keys to unlock archive stories
  live_theater/performers_preset_index|update   saved performer line-ups
  serial_code/register            redeem a promo code
  support_card_ranking/get_ranking  the support-card exam leaderboard
  user/get_profile_info           the trainer-profile stat panel

Shapes are read off dump.cs; none of these has been captured. The rule is the
same as everywhere else in this pass: the declared fields are always present and
always the right type (an absent array deserializes to null, not empty, and the
client does not guard), and anything the state cannot support refuses rather
than reporting a success that did not happen.
"""

from __future__ import annotations

import copy
import logging

from .. import master_data, state as state_store
from . import registry

log = logging.getLogger("uma-server")

GALLERY_VIEW_KEY = "gallery_view_state"
PHOTO_ACTIVITY_KEY = "photo_activity_type"
THEATER_PRESET_KEY = "live_theater_presets"
SERIAL_CODE_KEY = "serial_codes_redeemed"


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


# ================================================================ gallery ===

@registry.endpoint("gallery/play_event")
def handle_play_event(payload: dict) -> dict:
    """{story_id} -> {event_id, chara_id, story_id, event_contents_info{
    support_card_id, choice_array, month, race_id}}.

    Replaying a career event from the gallery. Every answerable field comes out
    of single_mode_story_data -- the same table the live career reads its events
    from -- so the gallery shows the event the career would have shown:

        event_id       <- the row's own `id`
        chara_id       <- card_chara_id, or support_chara_id for a support event
        support_card_id<- support_card_id (0 on a chara event)
        race_id        <- past_race_id

    `month` has no column in this table and is left 0: the career's own event
    scheduling lives in single_mode_story_data's callers, not here, and guessing
    a month would put a date on the replay that the event never had.

    choice_array is EMPTY, and that is a real gap rather than an oversight --
    Global's master.mdb ships no per-story choice table at all (the only
    choice-shaped table, single_mode_event_choice_reward, has 36 rows of reward
    templates and no story_id to join on). The scene still plays; what it cannot
    do is re-offer the original choices.

    An unknown story refuses. Echoing the requested id back wrapped in zeros
    would open the scene viewer on an event with no script -- a blank screen
    rather than an error the player can read."""
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
    """-> {}. The gallery persisting its own view state (what has been opened).

    The request declares no fields in dump.cs and neither does the response, so
    there is nothing to store from it beyond the fact that it happened -- which
    is recorded so a later "what's new" badge has a last-seen to compare to."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[GALLERY_VIEW_KEY] = {"saved": True}
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("talk_gallery/index")
def handle_talk_gallery_index(payload: dict) -> dict:
    """-> {}. TalkGalleryIndexResponse declares no CommonResponse fields at all,
    so an empty data IS the shape.

    The list itself does not come from here: load/index already carries
    talk_gallery_list (see load.py, where it is built from the account's own
    released home-story triggers), and this call is the screen opening."""
    return _ok({})


@registry.endpoint("photo/set_activity")
def handle_set_activity(payload: dict) -> dict:
    """{action_type} -> {}. Photo-mode pose/activity choice. Stored so the
    picker reopens on what was picked."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    full_state[PHOTO_ACTIVITY_KEY] = int(payload.get("action_type") or 0)
    state_store.save_state(viewer_id, full_state)
    return _ok({})


# ---------------------------------------------------------- gallery keys --

_GALLERY_KEY_ITEM = 30          # item_data 'Gallery Key'


@registry.endpoint("note/use_gallery_key")
def handle_use_gallery_key(payload: dict) -> dict:
    """{gallery_type, client_own_num, story_id_array} -> {item_info_array,
    event_data_array, home_story_data_array}.

    Spend gallery keys to unlock archive entries the account has not seen in a
    career. One key per story, validated against OUR count and never against the
    client's own client_own_num -- the same rule daily_race_skip follows, and
    for the same reason: the client's copy is a display value it has no
    authority over.

    Refuses outright when the keys do not cover the request rather than
    unlocking a prefix of it: a partial unlock with a full charge is the worst
    of both, and the client re-reads the whole list from this response anyway.

    gallery_type selects which archive the ids belong to -- the note's event
    data (event_data_array) or its home stories (home_story_data_array). Both
    arrays are always present, empty where they do not apply."""
    from . import note_archive
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    story_ids = [int(s) for s in (payload.get("story_id_array") or []) if s]
    if not story_ids:
        return _refuse()

    # Each story names its OWN price in single_mode_story_data.
    # available_gallery_key -- not a flat one key per story. A story the table
    # does not know is skipped rather than charged for.
    cost = 0
    known = []
    for sid in story_ids:
        row = master_data.query_one(
            "SELECT available_gallery_key FROM single_mode_story_data "
            "WHERE story_id = ?", (sid,))
        if row is None:
            log.info("note/use_gallery_key: no story %s, skipping", sid)
            continue
        cost += int(row["available_gallery_key"] or 1)
        known.append(sid)
    if not known:
        return _refuse()
    story_ids = known

    items = full_state.setdefault("item_list_state", [])
    key_item = next((i for i in items if i.get("item_id") == _GALLERY_KEY_ITEM), None)
    have = (key_item or {}).get("number") or 0
    if have < cost:
        log.info("note/use_gallery_key: %s keys held, %s needed", have, cost)
        return _refuse()
    key_item["number"] = have - cost

    gallery_type = int(payload.get("gallery_type") or 0)
    events, home_stories = [], []
    # Unlocks land in note_archive's OWN state, not a private list here: the
    # note screen computes its archive score from exactly these collections
    # (_score_summary), so an unlock filed anywhere else would buy the player a
    # story that never shows up in their archive total.
    note = note_archive._note_state(full_state)
    stamp = note_archive._now()
    for sid in story_ids:
        if gallery_type == _GALLERY_TYPE_HOME_STORY:
            stories = note.setdefault("home_stories", [])
            if sid not in stories:
                stories.append(sid)
            home_stories.append({"id": sid})
        else:
            chara_id = sid // 1000 % 10000
            note.setdefault("voices", {}).setdefault(f"{chara_id}:{sid}", stamp)
            events.append({"chara_id": chara_id, "data_id": sid,
                           "create_time": stamp, "new_flag": 1})
    state_store.save_state(viewer_id, full_state)
    log.info("note/use_gallery_key: unlocked %s stor%s for %s key(s), %s left",
             len(story_ids), "y" if len(story_ids) == 1 else "ies",
             cost, key_item["number"])
    return _ok({"item_info_array": [{"item_id": _GALLERY_KEY_ITEM,
                                     "number": key_item["number"]}],
                "event_data_array": events,
                "home_story_data_array": home_stories})


# gallery_type 2 = the home-story archive; anything else is the event archive.
# INFERRED from the response carrying exactly those two arrays -- dump.cs names
# no enum for it.
_GALLERY_TYPE_HOME_STORY = 2


# ========================================================== live theater ====

@registry.endpoint("live_theater/performers_preset_index")
def handle_performers_preset_index(payload: dict) -> dict:
    """-> {preset_array:[{preset_id, preset_name, member_info_array}]}. The
    saved performer line-ups for Live Theater."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    return _ok({"preset_array": copy.deepcopy(full_state.get(THEATER_PRESET_KEY) or [])})


@registry.endpoint("live_theater/performers_preset_update")
def handle_performers_preset_update(payload: dict) -> dict:
    """{preset:{preset_id, preset_name, member_info_array}} -> {}.

    One preset at a time (singular `preset`, unlike the index's array). A
    preset_id of 0 creates; anything else replaces in place. The response
    declares no fields, so the screen redraws from performers_preset_index --
    which is exactly why this has to actually persist."""
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


# =========================================================== serial code ====

@registry.endpoint("serial_code/register")
def handle_serial_code_register(payload: dict) -> dict:
    """{serial_code} -> {reward_array, campaign_name}.

    Promo codes are issued by the operator, and this server is not one -- there
    is no code list in master.mdb and no campaign to belong to. So every code is
    rejected, with the one exception of a code the admin has put in
    client_config.json's `serial_codes` map ({code: {name, rewards:[{item_type,
    item_id, item_num}]}}), which is how a private server can still run a
    giveaway if it wants one.

    Rejecting is the honest answer and also the safe one: reporting a reward
    array the account was never actually given is the worst possible outcome
    here. Codes already redeemed by this account are rejected too -- a promo
    code that pays out twice is a duplication bug."""
    from .. import config
    from . import presents
    viewer_id = payload["viewer_id"]
    code = str(payload.get("serial_code") or "").strip()
    catalogue = config.get("serial_codes") or {}
    entry = catalogue.get(code) if isinstance(catalogue, dict) else None
    if not code or not isinstance(entry, dict):
        log.info("serial_code/register: no campaign for code %r", code)
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    used = full_state.setdefault(SERIAL_CODE_KEY, [])
    if code in used:
        log.info("serial_code/register: %r already redeemed by %s", code, viewer_id)
        return _refuse()

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


# ================================================== support card ranking ====

@registry.endpoint("support_card_ranking/get_ranking")
def handle_support_card_ranking(payload: dict) -> dict:
    """-> {groups:[{exam_index, items}], is_counting, first_access_flag}.

    The support-card leaderboard that hangs off training_challenge -- which is
    NOT built (see docs/ENDPOINT_GAP_AUDIT_2026-09-11.md, Tier 2). With no exams
    run there are no scores to rank, and inventing a board would put made-up
    accounts on a leaderboard.

    is_counting True is the honest state for that: it is the game's own "results
    are still being tallied" mode, which renders an empty board as pending
    rather than as a board where nobody scored. Empty groups, never null."""
    return _ok({"groups": [], "is_counting": True, "first_access_flag": False})


# ========================================================== profile stats ===

@registry.endpoint("user/get_profile_info")
def handle_get_profile_info(payload: dict) -> dict:
    """-> {voice_num, act_num, good_end_num, team_stadium_win_count,
    single_mode_play_count, rank_score, chara_event_num, support_event_num,
    scenario_event_num, home_event_num, main_story_num, chara_story_num,
    highest_rank_score}. The trainer-profile stat panel.

    Live-hit against the no-op fallback before this existed. Distinct from the
    already-built user/get_profile_card_info, which is the CARD other players
    see; this is the owner's own counters.

    Every number is computed from state this server already tracks truthfully --
    the career roster, the note archive, the story-clear sets -- rather than
    stored as a separate tally that could drift from it. The request's
    add_voice_data_array / add_home_story_data_array are the client flushing
    newly-seen entries on its way in, so they are folded into the archive before
    the counts are taken."""
    from . import collection, directory, note_archive, trained_chara
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    # Fold in what the client is reporting as newly seen on the way in -- the
    # same add_voice_data_array / add_home_story_data_array batching every
    # note/* endpoint does, through note_archive's own absorber so one code path
    # owns the archive.
    note = note_archive._note_state(full_state)
    note_archive._absorb_sync(note, payload)
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
        # "act" is the support-card collection -- note_archive._score_summary
        # scores it off exactly this list.
        "act_num": len(full_state.get(collection.SUPPORT_CARD_KEY) or []),
        # A "good end" is a career that reached the end rather than being given
        # up, which is precisely what earns a roster entry in the first place.
        "good_end_num": len(genuine),
        "team_stadium_win_count": card.get("team_stadium_win_count") or 0,
        "single_mode_play_count": len(genuine),
        "rank_score": card.get("rank_score") or 0,
        # The three per-source EVENT tallies have no honest source on this
        # server. Career events are tracked per-career (event_engine's
        # FIRED_EVENTS_KEY) and deliberately cleared at the start of every run --
        # see the career-reset list in single_mode_team, where NOT clearing it
        # was a real bug that stopped cards' events ever firing again. There is
        # no account-lifetime tally to read, and counting the current career's
        # would report a number that falls back to near zero every time a new
        # career starts, which is worse than admitting zero. If these ever
        # matter, they need their own lifetime counters written where events
        # actually fire, not a guess here.
        "chara_event_num": 0,
        "support_event_num": 0,
        "scenario_event_num": 0,
        # Home stories, unlike the above, ARE tracked account-wide: the client
        # syncs them and note_archive keeps them.
        "home_event_num": len(note.get("home_stories") or []),
        "main_story_num": len(story_st.get("main_cleared") or []),
        "chara_story_num": len(story_st.get("chara_cleared") or []),
        "highest_rank_score": max(scores) if scores else 0,
    })
