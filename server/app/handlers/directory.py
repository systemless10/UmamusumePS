"""Cross-account player directory: the `user_info_summary` block every social
screen renders another player with.

WHAT THIS FIXES
---------------
friend/index, friend/search and pre_single_mode/index all serve arrays of
OTHER accounts' profile summaries. Until now this project served them empty,
for a documented and correct reason (user_profile.py's friend/index
docstring): there were no other real accounts to show, and inventing fake
players is exactly the failure mode this codebase has been burned by.

That reason has expired. This server hosts ~200 real accounts, and
team_stadium.py already set the precedent -- its opponents come from real
other accounts on this server, never fabricated ones. The same rule applies
here: everything below is derived from a REAL account's own persisted state,
and an account with nothing to show is simply absent from the list rather
than padded out with invented numbers.

WHY A PUBLISHED CARD AND NOT A LIVE READ
----------------------------------------
The obvious implementation -- read each account's user_info when building a
list -- is a performance trap. user_info lives inside `load_index`, which is
a LAZY key in state.py precisely because it is ~3.1 MB on a real account and
almost nothing reads it. A friend/index response carries up to 100 summaries;
doing that with 100 blob reads costs seconds, and state.py's own docstring
documents the same mistake being fixed once already.

So each account PUBLISHES a compact summary (~1 KB) into its own state under
DIRECTORY_KEY whenever it logs in, and list building is one
all_states_for_key query over that small key -- the identical technique
team_stadium._real_opponent_candidates uses for matchmaking, for the identical
reason. An account that has not logged in since this feature existed has no
card yet, so build_card falls back to a one-off extract_json_path read
(SQLite-side, no blob parse in Python) and publishes the result, which means
the slow path runs at most once per account.

STALENESS IS THE POINT, NOT A DEFECT
------------------------------------
A published card is a snapshot of who that player was at their last login.
That is what the real game shows too -- a friend list renders your friends'
last known state, not a live read of their account, and last_login_time is
right there in the payload saying so.
"""

from __future__ import annotations

import copy
import logging
import time

from .. import state as state_store
from .. import accounts, social

log = logging.getLogger("uma-server")

DIRECTORY_KEY = "social_directory_card"

# The trained_chara_id the player put up as their Star Umamusume. Its own key
# rather than user_info's partner_chara_id: see _partner_trained_chara.
PARTNER_KEY = "star_partner_chara_id"

# Bumped when the card's shape changes, so stale cards published by an older
# build are rebuilt instead of served with missing fields. Same convention as
# trained_chara.ROSTER_VERSION.
CARD_VERSION = 4

_ZERO_TIME = "0000-00-00 00:00:00"

# An account with no honor of its own still needs one that resolves to a real
# master.mdb row -- single_mode_team.py's hard-won lesson: an id that does not
# resolve renders as nothing rather than erroring. 100101 "Rookie Trainer" is
# every real account's default (user_profile._DEFAULT_HONOR_ID).
_DEFAULT_HONOR_ID = 100101

# The subset of a roster entry that user_info_summary.user_trained_chara
# carries. Taken field-for-field from a real summary entry in
# captures/20260907_204921/0023_friend_index.json -- the full roster entry is
# far larger (skill_array, race_result_list, support_card_list...) and the
# summary form deliberately is not.
_TRAINED_CHARA_FIELDS = (
    "trained_chara_id", "card_id", "rank_score", "rank",
    "proper_ground_turf", "proper_ground_dirt",
    "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi",
    "proper_distance_short", "proper_distance_mile",
    "proper_distance_middle", "proper_distance_long",
    "rarity", "talent_level", "register_time",
)


def _epoch_to_stamp(epoch) -> str:
    if not epoch:
        return _ZERO_TIME
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch)))


def _summary_trained_chara(viewer_id, entry: dict) -> dict:
    out = {"viewer_id": social._wire_id(viewer_id)}
    for k in _TRAINED_CHARA_FIELDS:
        if k in entry:
            out[k] = entry[k]
    out["factor_info_array"] = entry.get("factor_info_array") or []
    return out


def _best_trained_chara(roster: list) -> dict | None:
    """The account's strongest horse -- what a real summary shows off. Prefers
    a genuine career graduate over a seeded starter, since that is what the
    player actually earned; falls back to the best of whatever exists so a
    brand-new account still renders."""
    if not roster:
        return None
    from . import trained_chara
    genuine = [c for c in roster
               if trained_chara.PLAYER_CAREER_ID_BASE <= (c.get("trained_chara_id") or 0)]
    pool = genuine or roster
    return max(pool, key=lambda c: c.get("rank_score") or 0)


def _lead_support_card(support_cards: list, preferred_id: int) -> dict | None:
    """The card this account is shown lending. Prefers the one their own
    user_info already names (support_card_id -- the card the player chose to
    feature), else their most-invested one, so the pick is theirs and not
    ours whenever they have expressed a preference."""
    if not support_cards:
        return None
    chosen = None
    if preferred_id:
        chosen = next((c for c in support_cards
                       if c.get("support_card_id") == preferred_id), None)
    if chosen is None:
        chosen = max(support_cards, key=lambda c: (c.get("limit_break_count") or 0,
                                                  c.get("exp") or 0))
    return chosen


def _partner_trained_chara(roster: list, partner_chara_id: int) -> dict | None:
    """The horse this player has SET as their Star Umamusume.

    `user_info.partner_chara_id` is NOT a chara_id despite the name -- the
    real capture (captures/20260816_140811/0012_friend_search.json) has
    partner_chara_id == 3194 and practice_partner_info.trained_chara_id ==
    3194 for card 101401. It names one specific TRAINED CHARA, i.e. one
    finished career, which is exactly what "the one other people borrow"
    has to mean.

    The id passed here comes from PARTNER_KEY, written only by
    user/change_practice_partner -- NOT from user_info, whose
    partner_chara_id every seeded account carries as a default (1, the first
    starter) that no player ever chose. Honouring that default would pin
    everyone's Star Umamusume to a starter they did not pick; an explicit
    key is the only way to tell a real choice from a seed. Falls back to the
    strongest horse until the player makes one.
    """
    if partner_chara_id:
        chosen = next((c for c in roster
                       if c.get("trained_chara_id") == partner_chara_id), None)
        if chosen is not None:
            return chosen
    return _best_trained_chara(roster)


def archive_entries(roster: list) -> list:
    """The Archive (directory_card_array): one entry per DISTINCT card_id,
    each carrying that umamusume's BEST career.

    Capture-shaped from the real friend/search payload: 33 entries, 33
    distinct card_id, ascending by card_id, `directory_ranking` 1 on both the
    entry and the full trained_chara nested inside it (the client's detail
    popup reads the nested copy, which is why the whole record is repeated
    there). That ranking is uniformly 1 in the friend/search capture, unlike
    note/index's own array where it is 1..N -- so the two are built
    separately even though both mean "one best run per uma"; note_archive
    owns the note/index form.

    Only genuine career graduates are archived -- a seeded starter is not a
    career the player ran, and this screen is the record of what they
    actually did.
    """
    from . import note_archive, trained_chara
    genuine = [c for c in roster
               if trained_chara.PLAYER_CAREER_ID_BASE
               <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]
    best = note_archive._best_per_card(genuine)
    out = []
    for card_id in sorted(best):
        entry = copy.deepcopy(best[card_id])
        entry["directory_ranking"] = 1
        entry["card_id"] = card_id
        out.append({"card_id": card_id, "directory_ranking": 1,
                    "trained_chara": entry})
    return out


def directory_level(full_state: dict, roster: list) -> int:
    """Archive Level, the same number the Uma Note screen shows.

    note_archive.py already owns this ladder and its scoring, calibrated
    against three real note/index captures (see its module docstring), so
    this reads that rather than inventing a second answer: the level stored
    at the player's last visit to the note, else the level their current
    account state scores right now (an account that has never opened the note
    still has a real archive).
    """
    from . import note_archive
    note = full_state.get(note_archive.NOTE_STATE_KEY) or {}
    stored = note.get("level")
    if stored:
        return stored
    try:
        summary = note_archive._score_summary(full_state, note, roster)
        return note_archive._rank_for(note_archive._total_score(summary))
    except Exception:
        log.exception("directory: archive level scoring failed")
        return 1


def build_card(viewer_id, full_state: dict | None = None) -> dict | None:
    """Assemble one account's directory card from its persisted state.

    Returns None for an account with no load/index blob at all -- i.e. one
    that has never actually played. Those are excluded from every list rather
    than shown as an empty trainer, which is the same "absent beats
    fabricated" rule the rest of this module follows.
    """
    vid = str(viewer_id)
    if full_state is not None and "load_index" in full_state:
        user_info = (full_state["load_index"].get("data") or {}).get("user_info")
    else:
        # SQLite-side extraction: reaches one small object inside the 3.1 MB
        # blob without parsing the blob in Python (state.py's own docstring
        # measures this at ~13 ms against ~50 ms for a full parse).
        user_info = state_store.extract_json_path(vid, "load_index", "$.data.user_info")
    if not isinstance(user_info, dict):
        return None

    if full_state is None:
        full_state = state_store.get_state(vid) or {}

    from . import collection, team_stadium, trained_chara

    roster = full_state.get(trained_chara.ROSTER_KEY) or []
    chosen_partner_id = full_state.get(PARTNER_KEY) or 0
    best = _partner_trained_chara(roster, chosen_partner_id)
    cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
    chosen_card_id = user_info.get("support_card_id") or 0
    lead = _lead_support_card(cards, chosen_card_id)

    ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    acct = accounts.get_account(vid) or {}

    # Careers actually completed on this server, not a claimed counter: a
    # genuine graduate is exactly what trained_chara's id bands define.
    play_count = sum(1 for c in roster
                     if trained_chara.PLAYER_CAREER_ID_BASE
                     <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE)

    honor_id = user_info.get("honor_id") or _DEFAULT_HONOR_ID
    card = {
        "version": CARD_VERSION,
        "viewer_id": social._wire_id(vid),
        "name": user_info.get("name") or f"Trainer{vid[-4:]}",
        "honor_id": honor_id,
        "honor_data": {"honor_id": honor_id},
        # accounts.last_seen is the truth when it exists, but a real, played
        # account can legitimately have no accounts row at all -- that table
        # postdates some of the accounts on this server, and touch() only
        # grandfathers one in when it is next actually seen. Falling back to
        # user_info's own update_time (which load/index refreshes on every
        # call) keeps those accounts from advertising a 0000-00-00 last login,
        # which reads as "never played" on every social screen.
        "last_login_time": _epoch_to_stamp(acct.get("last_seen"))
        if acct.get("last_seen") else (user_info.get("update_time")
                                       or user_info.get("create_time") or _ZERO_TIME),
        "leader_chara_id": user_info.get("leader_chara_id") or 0,
        "leader_chara_dress_id": user_info.get("leader_chara_dress_id") or 0,
        "support_card_id": user_info.get("support_card_id") or 0,
        "partner_chara_id": user_info.get("partner_chara_id") or 0,
        "comment": user_info.get("comment") or "",
        "fan": user_info.get("fan") or 0,
        "rank_score": user_info.get("rank_score") or 0,
        # This server keeps no CUMULATIVE Team Trials win counter --
        # team_stadium_state tracks consecutive_win_count (reset every day by
        # design) and evaluation points, but never a lifetime total. Serving 0
        # is the honest answer; serving the consecutive streak in a field
        # labelled lifetime wins would be a plausible-looking wrong number,
        # which is worse than an obviously-zero one.
        "team_stadium_win_count": 0,
        "single_mode_play_count": play_count,
        "team_evaluation_point": ts_state.get("team_evaluation_point")
        or user_info.get("best_team_evaluation_point") or 0,
        "best_team_evaluation_point": user_info.get("best_team_evaluation_point") or 0,
        "directory_level": directory_level(full_state, roster),
    }
    if lead is not None:
        card["support_card_id"] = lead.get("support_card_id") or card["support_card_id"]
        card["user_support_card"] = {
            "viewer_id": social._wire_id(vid),
            "support_card_id": lead.get("support_card_id"),
            "exp": lead.get("exp") or 0,
            "limit_break_count": lead.get("limit_break_count") or 0,
            # 1 marks the card the player has put up to be borrowed -- the
            # real capture's support_card_data carries favorite_flag 1 on
            # exactly the card user_info.support_card_id names.
            "favorite_flag": int(lead.get("support_card_id") == chosen_card_id),
            "stock": 0,
            "possess_time": card["last_login_time"],
            "create_time": card["last_login_time"],
        }
    if best is not None:
        card["user_trained_chara"] = _summary_trained_chara(vid, best)
        # partner_chara_id MUST name the horse practice_partner_info actually
        # carries -- the capture has the two equal, and the borrow screen
        # resolves one through the other.
        card["partner_chara_id"] = (best.get("trained_chara_id")
                                    or card["partner_chara_id"])
    return card


def publish(viewer_id, full_state: dict) -> bool:
    """Refresh this account's own card in its own state. Mutates full_state
    only -- the CALLER's save_state persists it, so this never writes behind
    a handler's back. Returns whether anything changed.

    Called from load/index, i.e. once per login, which is exactly the
    freshness the real game's own last_login_time advertises."""
    card = build_card(viewer_id, full_state)
    if card is None:
        return False
    if full_state.get(DIRECTORY_KEY) == card:
        return False
    full_state[DIRECTORY_KEY] = card
    return True


def _publish_now(viewer_id) -> dict | None:
    """Build and persist a card for an account that has none yet, in its own
    independent read/save round trip.

    Safe despite running under a DIFFERENT viewer's lock (main.py serializes
    per viewer): DIRECTORY_KEY is derived data, owned by this module alone, and
    state.py's save_state writes only the keys whose text actually changed --
    so a concurrent request on the target account cannot lose a real mutation
    to this write, and the worst case is two builds racing to store the same
    derived value.
    """
    st = state_store.get_state(viewer_id)
    if st is None:
        return None
    card = build_card(viewer_id, st)
    if card is None:
        return None
    st[DIRECTORY_KEY] = card
    state_store.save_state(viewer_id, st)
    return card


def cards_for(viewer_ids) -> dict:
    """{viewer_id: card} for the accounts asked for, in ONE query for the
    already-published ones plus a one-off build for any that have never
    published. Missing/unplayed accounts are simply absent from the result."""
    ids = [str(v) for v in viewer_ids]
    if not ids:
        return {}
    found = state_store.all_states_for_key(DIRECTORY_KEY, ids)
    out = {}
    for vid in ids:
        card = found.get(vid)
        if not isinstance(card, dict) or card.get("version") != CARD_VERSION:
            try:
                card = _publish_now(vid)
            except Exception:
                # One malformed account must never take down a friend list.
                log.exception("directory: failed to build card for %s", vid)
                card = None
        if card:
            out[vid] = card
    return out


def all_cards(exclude=None) -> dict:
    """Every published card on the server except the caller's own.

    Deliberately does NOT build cards for unpublished accounts: this feeds
    browse/recommend screens, where a one-off build per never-seen account
    would turn one request into ~200 blob reads. Those accounts appear here
    the first time they log in, or immediately if something looks them up by
    id through cards_for.
    """
    cards = state_store.all_states_for_key(DIRECTORY_KEY)
    me = str(exclude) if exclude is not None else None
    return {vid: c for vid, c in cards.items()
            if vid != me and isinstance(c, dict) and c.get("version") == CARD_VERSION}


def summary(viewer_id, viewer_card: dict, friend_state: int = 0) -> dict:
    """One card as the wire's user_info_summary: the stored card plus the
    per-viewer relationship fields, which are NOT part of the card (they
    differ for every account asking about it) and the circle block, which is
    shared state that changes without the subject logging back in.

    A member gets their real circle. A player in NO circle gets circle_info
    and circle_user as null, which is what the real server sends: 61 entries
    in captures/20260816_140811/0034_pre_single_mode_index.json, including
    friend_support_card_data's own list. This used to serve single_mode_
    team.py's stand-in "Friends" circle instead, so the Trainer Info popup
    showed a club named "Friends" that did not exist. Tapping its details
    then asked circle/detail for that fake circle_id and got a 205.
    """
    out = {k: v for k, v in viewer_card.items() if k != "version"}
    out["friend_state"] = friend_state
    out["state"] = friend_state
    circle = social.circle_of(viewer_id)
    member = social.member_row(viewer_id) if circle is not None else None
    if circle is not None and member is not None:
        out["circle_info"] = {"circle_id": circle["circle_id"], "name": circle["name"]}
        out["circle_user"] = member
    else:
        out["circle_info"] = None
        out["circle_user"] = None
    return out


def follower_summary(viewer_id, viewer_card: dict) -> dict:
    """The TRIMMED form follower_info_summary_list carries -- seven fields, not
    the full summary. Field set taken exactly from the real capture's
    follower_info_summary_list[0]."""
    out = {
        "viewer_id": viewer_card["viewer_id"],
        "honor_id": viewer_card["honor_id"],
        "honor_data": viewer_card["honor_data"],
        "name": viewer_card["name"],
        "last_login_time": viewer_card["last_login_time"],
        "support_card_id": viewer_card.get("support_card_id") or 0,
    }
    if "user_support_card" in viewer_card:
        out["user_support_card"] = viewer_card["user_support_card"]
    return out
