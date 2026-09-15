"""Jukebox song requests (the home-screen music player's request queue).

  jukebox/play_user_request  -> the player queues a song themselves
  jukebox/draw_random_request-> "someone requests a song" (a random character)

Capture ground truth (request in file N-1, response in file N):
  play_user_request  : 20260727_190428 ops 0001-0003 -- request {music_id,
                       music_size}, response {request_id} (930 then 931:
                       server-side incrementing counter; the load seed's
                       jukebox_request_history sits at 928, so ours continues
                       from whatever the stored blob last knew).
  draw_random_request: 20260723_134126 op 0017 -- empty request, response
                       {request_history {request_type 1, request_value
                       1007103, music_id, requester_id 1007, request_id,
                       requester_request_id None}, next_random_request_time
                       (+15 min), add_music_array []}.

Validation [mdb]: master.mdb jukebox_music_data (38 rows). condition_type
0 = always playable, 1 = gated behind owning the song -- gated requests are
checked against the viewer's music_list_state (collection.py; falls back to
the load blob's music_list before first login). Unknown music id or a locked
song -> 205 refusal.

Plausible-from-state bits (no capture contradicts them, documented as
server-defined): the random requester is one of the viewer's own characters
(fallback 1001); request_value mirrors the captured requester*1000+103
pattern (a per-chara voice-line id); the random cooldown is the captured 15
minutes; add_music_array stays empty (we never gift songs from a draw).
A short request history persists in "jukebox_state".
"""

from __future__ import annotations

import random
import time

from .. import master_data
from .. import social
from .. import state as state_store
from . import registry

JUKEBOX_STATE_KEY = "jukebox_state"

_HISTORY_LIMIT = 100
_RANDOM_COOLDOWN_SEC = 15 * 60      # capture: 20:42 -> next at 20:57
_REQUEST_VALUE_SUFFIX = 103         # capture: requester 1007 -> value 1007103
_FALLBACK_REQUESTER = 1001


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _jukebox_state(full_state: dict, viewer_id) -> dict:
    st = full_state.get(JUKEBOX_STATE_KEY)
    if isinstance(st, dict):
        return st
    # Continue the request-id sequence from the stored load blob's last known
    # jukebox_request_history (the real account's counter), else start at 1.
    # Only ONE field is needed out of the 3.1 MB load_index blob, so pull it
    # straight out with SQLite instead of parsing the whole thing (~13 ms vs
    # ~50 ms). Falls back to the seeding accessor when the viewer has no stored
    # blob yet, which is the one case that must still seed one.
    hist = state_store.extract_json_path(
        viewer_id, "load_index", "$.data.jukebox_request_history")
    if hist is None:
        from .load import get_or_seed_blob
        hist = ((get_or_seed_blob(full_state, viewer_id).get("data") or {})
                .get("jukebox_request_history") or {})
    # Real accounts store [] here when there is no history, and the old
    # `... or {}` chain quietly turned that falsy list into a dict before
    # anything called .get() on it. Keep that coercion explicit.
    if not isinstance(hist, dict):
        hist = {}
    st = {"next_request_id": (hist.get("request_id") or 0) + 1,
          "history": [], "random_count": 0}
    full_state[JUKEBOX_STATE_KEY] = st
    return st


def _owned_music_ids(full_state: dict, viewer_id) -> set:
    """Songs the viewer owns: live music_list_state once collection.py seeded
    it, else the load blob's music_list (read-only pre-login fallback)."""
    music = full_state.get("music_list_state")
    if music is None:
        # Same targeted read as _jukebox_state above.
        music = state_store.extract_json_path(
            viewer_id, "load_index", "$.data.music_list")
        if music is None:
            from .load import get_or_seed_blob
            music = ((get_or_seed_blob(full_state, viewer_id).get("data") or {})
                     .get("music_list") or [])
        if not isinstance(music, list):
            music = []          # same coercion as the `or []` chain above
    return {m.get("music_id") for m in music if isinstance(m, dict)}


@registry.endpoint("jukebox/play_user_request")
def handle_play_user_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    music_id = payload.get("music_id")
    music_size = payload.get("music_size", 1)
    # Real server ground truth (captures/20260816_202505/0008): music_size can
    # legitimately be 0 (e.g. the short-size request path) and still succeeds.
    if not isinstance(music_id, int) or not isinstance(music_size, int) \
            or music_size < 0:
        return _refuse()
    row = master_data.query_one(
        "SELECT music_id, condition_type FROM jukebox_music_data "
        "WHERE music_id=?", (music_id,))
    if row is None:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    if row["condition_type"] and music_id not in _owned_music_ids(full_state, viewer_id):
        return _refuse()        # gated song the viewer hasn't unlocked

    st = _jukebox_state(full_state, viewer_id)
    request_id = st["next_request_id"]
    st["next_request_id"] = request_id + 1
    # History row shape = the load blob's jukebox_request_history (request_type
    # 0 with request_value 0 = a user request, requester = the viewer).
    st["history"].append({
        "request_id": request_id, "request_type": 0, "request_value": 0,
        "requester_id": viewer_id, "requester_request_id": request_id,
        "music_id": music_id, "request_time": _now(),
    })
    del st["history"][:-_HISTORY_LIMIT]
    from . import missions
    missions.mark_achieved(full_state, missions.FLAG_JUKEBOX_REQUESTED)
    state_store.save_state(viewer_id, full_state)
    return _ok({"request_id": request_id})


@registry.endpoint("jukebox/draw_random_request")
def handle_draw_random_request(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)

    owned = _owned_music_ids(full_state, viewer_id)
    pool = [r["music_id"] for r in master_data.query(
        "SELECT music_id, condition_type FROM jukebox_music_data "
        "WHERE is_hidden=0 AND request_type=0")
        if not r["condition_type"] or r["music_id"] in owned]
    if not pool:
        return _refuse()

    charas = full_state.get("chara_collection") or []
    chara_ids = [c.get("chara_id") for c in charas
                 if isinstance(c, dict) and c.get("chara_id")]
    requester = random.choice(chara_ids) if chara_ids else _FALLBACK_REQUESTER

    st["random_count"] = (st.get("random_count") or 0) + 1
    entry = {
        "request_type": 1,
        "request_value": requester * 1000 + _REQUEST_VALUE_SUFFIX,
        "music_id": random.choice(pool),
        "requester_id": requester,
        "request_id": st["random_count"],
        "requester_request_id": None,
    }
    next_time = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() + _RANDOM_COOLDOWN_SEC))
    st["last_random"] = entry
    st["next_random_request_time"] = next_time
    st["history"].append(dict(entry, request_time=_now()))
    del st["history"][:-_HISTORY_LIMIT]
    state_store.save_state(viewer_id, full_state)
    return _ok({"request_history": entry,
                "next_random_request_time": next_time,
                "add_music_array": []})


# --------------------------------------------------------------------------
# Jukebox settings, likes and history. dump.cs shapes (never captured here):
#   JukeboxChangePlayMusicRequest     {play_music_flag}     -> {}
#   JukeboxChangeRandomRequestRequest {random_request_flag} -> {}
#   JukeboxChangeUserRequestRequest   {user_request_flag}   -> {}
#   JukeboxExecLikeRequest            {request_viewer_id, request_id} -> {}
#   JukeboxHistoryRequest             {}
#     -> {like_array, latest_request_history, request_history_array,
#         summary_user_info_array}
#
# The three Change* calls are the home-screen music panel's own toggles. They
# are stored, not no-opped: a toggle that answers success and forgets is worse
# than one that refuses, because the panel reopens showing the old state.

# What a fresh account has these set to. All three default ON -- the home
# screen plays music and accepts requests out of the box.
_SETTING_DEFAULTS = {"play_music_flag": 1,
                     "random_request_flag": 1,
                     "user_request_flag": 1}


def settings(full_state: dict) -> dict:
    """The three jukebox toggles, defaults filled in. Public so whatever
    later serves them (a jukebox/index rebuild) reads one source."""
    st = full_state.get(JUKEBOX_STATE_KEY)
    stored = (st.get("settings") or {}) if isinstance(st, dict) else {}
    return {name: int(stored.get(name, default))
            for name, default in _SETTING_DEFAULTS.items()}


def _change_setting(payload: dict, field: str) -> dict:
    viewer_id = payload["viewer_id"]
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        return _refuse()
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)
    st.setdefault("settings", {})[field] = 1 if value else 0
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("jukebox/change_play_music")
def handle_change_play_music(payload: dict) -> dict:
    """jukebox/change_play_music -- "play music on the home screen" on/off."""
    return _change_setting(payload, "play_music_flag")


@registry.endpoint("jukebox/change_random_request")
def handle_change_random_request(payload: dict) -> dict:
    """jukebox/change_random_request -- whether characters may request songs
    on their own (the jukebox/draw_random_request path above)."""
    return _change_setting(payload, "random_request_flag")


@registry.endpoint("jukebox/change_user_request")
def handle_change_user_request(payload: dict) -> dict:
    """jukebox/change_user_request -- whether the player's own requests
    (jukebox/play_user_request above) are accepted."""
    return _change_setting(payload, "user_request_flag")


# How many umamusume spontaneously like -- per DAY, across the whole log, not
# per request. User-specified 2026-09-10 ("maybe like 7-10", then "the list is
# like 500 things long ... make it only randomly generate every day (on
# reset)").
#
# BUG FIXED 2026-09-10 (live-reported): this used to roll 7-10 likes for EVERY
# request and keep them forever, so with the log capped at _HISTORY_LIMIT=100
# the like list grew to ~500-1000 entries -- every request the account had ever
# made, each with its own permanent crowd. The daily crowd is a handful of
# umamusume reacting to what is in the box right now.
_UMA_LIKES_MIN = 7
_UMA_LIKES_MAX = 10


def _request_key(entry: dict) -> str:
    """Identity of one request inside this account's log. request_id alone is
    not unique -- user requests continue next_request_id while random draws
    count separately in random_count, so the two id spaces overlap (both are
    captured server behaviour, not something to renumber here). The requester
    disambiguates them."""
    return f"{entry.get('requester_id') or 0}:{entry.get('request_id') or 0}"


def _uma_like_pool(full_state: dict) -> list:
    """Characters that may be shown liking a request.

    Restricted to master_data.drawable_charas() -- a chara_id with no card_data
    row renders as a blank white plane rather than a portrait, silently, so a
    like from one would show up as a hole in the list."""
    return sorted(master_data.drawable_charas())


def _roll_uma_likes(st: dict, history, full_state: dict) -> None:
    """Roll the day's umamusume likes, once per daily reset, and prune likes
    whose request has aged out of the log.

    The crowd is rerolled when daily_races._served_day() advances -- this
    server's one daily-reset clock (05:00 JST), the same boundary the daily
    missions and daily races roll on. Within a day the list is stable, so
    reopening the screen shows the same faces; the next reset replaces them
    with a fresh 7-10 rather than adding to them.

    The player's own likes are NEVER touched here: they are a record of
    something the player did, not generated flavour, and they survive the
    reroll (see handle_exec_like)."""
    from . import daily_races

    likes = st.setdefault("likes", [])
    live = {_request_key(h) for h in history}
    # Prune first, on both paths: the log is capped at _HISTORY_LIMIT, and a
    # like on a request nobody can see any more is dead weight.
    likes = [l for l in likes if l.get("request_key") in live]
    st.pop("like_rolled", None)    # per-request ledger; superseded by like_day

    today = daily_races._served_day()
    if st.get("like_day") != today:
        st["like_day"] = today
        # Drop only the generated crowd -- a like with a chara behind it. The
        # player's own likes (like_viewer_id set) stay.
        likes = [l for l in likes if not l.get("like_chara_id")]
        pool = _uma_like_pool(full_state)
        entries = list(history)
        if pool and entries:
            count = min(len(pool), random.randint(_UMA_LIKES_MIN, _UMA_LIKES_MAX))
            # Distinct charas account-wide for the day, so the same face never
            # appears twice on one request either.
            for chara_id in random.sample(pool, count):
                entry = random.choice(entries)
                likes.append({"request_key": _request_key(entry),
                              "request_id": entry.get("request_id"),
                              "like_chara_id": chara_id,
                              "like_viewer_id": 0})
    st["likes"] = likes


@registry.endpoint("jukebox/exec_like")
def handle_exec_like(payload: dict) -> dict:
    """jukebox/exec_like -- the PLAYER likes a song someone requested.

    JukeboxLike is {request_id, sort_id, like_chara_id, like_viewer_id}: the
    two id fields are the two kinds of liker, and exactly one is set. An
    umamusume liking a request fills like_chara_id; a real player liking one
    fills like_viewer_id. This endpoint is only ever the player, so it writes
    like_viewer_id = the caller and leaves like_chara_id 0.

    BUG FIXED 2026-09-10 (live-reported): this used to store the REQUESTER's
    chara_id as like_chara_id and the request's owner as like_viewer_id, so
    opening the history showed the player's own like credited to a random
    umamusume instead of to the player. Neither field is about the request --
    both are about who pressed like.

    `request_viewer_id` in the REQUEST identifies whose request is being
    liked, not who is liking it; it is used to resolve the target row (see
    _request_key) and never stored as the liker.

    Liking the same request twice is idempotent, not an error -- the client
    can resend on a retry, and the second call must not double-count or 205."""
    viewer_id = payload["viewer_id"]
    request_id = payload.get("request_id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        return _refuse()
    target = payload.get("request_viewer_id")
    if target is not None and (not isinstance(target, int) or isinstance(target, bool)):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)
    # When the client names a requester, match on it too so an id collision
    # across the two id spaces cannot like the wrong row; a bare id still
    # falls back to the first match.
    history = st.get("history") or ()
    entry = None
    if target:
        entry = next((h for h in history
                      if h.get("request_id") == request_id
                      and h.get("requester_id") == target), None)
    if entry is None:
        entry = next((h for h in history
                      if h.get("request_id") == request_id), None)
    if entry is None:
        return _refuse()        # nothing by that id to react to

    key = _request_key(entry)
    likes = st.setdefault("likes", [])
    mine = social._wire_id(viewer_id)
    if not any(l.get("request_key") == key and l.get("like_viewer_id") == mine
               for l in likes):
        likes.append({"request_key": key,
                      "request_id": entry.get("request_id"),
                      "like_chara_id": 0,
                      "like_viewer_id": mine})
    state_store.save_state(viewer_id, full_state)
    return _ok({})


@registry.endpoint("jukebox/history")
def handle_history(payload: dict) -> dict:
    """jukebox/history -- the request log screen.

    Served from the same "jukebox_state" history both request endpoints above
    already append to, newest first (the log reads top-down).

    Umamusume likes are rolled here (see _roll_uma_likes): 7-10 of them per
    DAY across the whole log, persisted, so the crowd stays the same every
    time the screen is reopened and is replaced -- not added to -- at the next
    daily reset. That is generated flavour, not captured behaviour: on the
    real server those likes come from other real players' umamusume, and there
    are none here.

    sort_id is assigned at serve time from the final ordering rather than
    stored, so it is always a dense 1..N over exactly what is being served --
    a stored counter would leave gaps once pruning drops likes for requests
    that aged out of the log.

    KNOWN AMBIGUITY, inherent to the wire: JukeboxLike carries only
    request_id, so the client groups likes by that alone -- but the two id
    spaces overlap (see _request_key). On a real account they do not collide
    in practice: captures show user requests continuing the account counter
    (the stored jukebox_request_history sits at 928, so the next is 929) while
    random draws start at 1 and climb slowly (1,2,3,4 across one session), so
    a collision needs a brand-new account whose own counter is still in single
    digits. Likes are still keyed internally by requester+id so the SERVER
    never confuses them; a fresh account can just see one crowd rendered
    against both rows, which is what the real server would also produce for
    the same collision.

    summary_user_info_array is the UserInfoAtFriend block for every real
    VIEWER in the log, so the client can put a name against a request or a
    like. On this server that is only ever this account -- the same reasoning
    friend/index and trained_chara/load already use for not fabricating other
    players. Umamusume contribute a chara_id, not a viewer_id, and need no
    row."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _jukebox_state(full_state, viewer_id)

    history = list(st.get("history") or ())
    _roll_uma_likes(st, history, full_state)

    rows = [{"request_id": h.get("request_id"),
             "request_type": h.get("request_type") or 0,
             "request_value": h.get("request_value") or 0,
             # A user request stores the raw viewer_id, which is TEXT in this
             # project's stores while JukeboxRequest.requester_id is a ulong --
             # same conversion social.py does for friend_viewer_id.
             "requester_id": social._wire_id(h.get("requester_id") or 0),
             "music_id": h.get("music_id"),
             "request_time": h.get("request_time") or _now()}
            for h in reversed(history)]

    # Newest request first, matching the log itself; the player's own like
    # leads each request's crowd, then its umamusume.
    order = {_request_key(h): i for i, h in enumerate(reversed(history))}
    stored = st.get("likes") or []
    stored = sorted(stored, key=lambda l: (order.get(l.get("request_key"), 1 << 30),
                                           0 if l.get("like_viewer_id") else 1,
                                           l.get("like_chara_id") or 0))
    likes = [{"request_id": l.get("request_id"),
              "sort_id": i,
              "like_chara_id": l.get("like_chara_id") or 0,
              "like_viewer_id": l.get("like_viewer_id") or 0}
             for i, l in enumerate(stored, start=1)]

    from . import directory
    card = directory.build_card(viewer_id, full_state)
    own = directory.summary(viewer_id, card) if card else None
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "like_array": likes,
        "latest_request_history": rows[0] if rows else None,
        "request_history_array": rows,
        "summary_user_info_array": [own] if own else [],
    })
