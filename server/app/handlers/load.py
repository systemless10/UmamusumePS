"""
load/index: account/session bootstrap data.

Static fixture replay for this endpoint hit a real ceiling: the client
appears to compare returned progression values (fan count, rank score,
update_time, ...) against what its local SaveData.db cache already knows,
and treats a mismatch -- in either direction -- as a trigger to resync,
which surfaced as an infinite "new day" login loop. A frozen snapshot can
only ever be right for the one instant it was captured.

Fix: make this endpoint genuinely stateful. The first call for a viewer
seeds persistent state from data/seeds/load_index_fresh.json (a real
capture from moments before this account's local cache was last known
good -- much closer to "current" than the original hours-old fixture).
Every call after that returns the SAME persisted state (not a fresh
re-read of the seed), so values stay internally consistent across the
session instead of jumping around; update_time is refreshed to "now" on
every call so the client never sees it as stale.

user_info.fan and .best_team_evaluation_point are derived live (chara_list's
own fan totals; team_stadium_state's best_point) rather than frozen from the
seed -- see handle_load_index's tail. rank_score and everything else in
user_info still doesn't advance over time or in response to gameplay (e.g.
single_mode_team progress feeding back in here) -- this is the seed-once-
and-persist step, not full simulation, for anything not called out above.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from pathlib import Path

from .. import state as state_store
from . import collection, stamina, trained_chara

log = logging.getLogger("uma-server")

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "seeds" / "load_index_fresh.json"
# Only source of the single_mode_chara_light FIELD SHAPE (see
# _chara_light_template below) -- never loaded as the actual login seed.
_CHARA_LIGHT_SHAPE_PATH = (Path(__file__).resolve().parents[2] / "data" / "seeds"
                           / "load_index_fresh_v1_established_account.json")
STATE_KEY = "load_index"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SEEN_ANNOUNCE_KEY = "seen_announce_ids"

_seed_cache: dict | None = None
_chara_light_shape_cache: dict | None = None


def _chara_light_template() -> dict:
    """Field-shape dict for single_mode_chara_light (every key the real
    struct has, from the one fixture that happens to carry a populated one).

    BUG FIXED 2026-08-23 (live-reported: "sometimes the career doesn't get
    reported in main menu and it starts from beginning, but when it gets to
    introduction event it puts me back to the old career"). _overlay_live_data
    used to read this shape from the CURRENT response's own
    data["single_mode_chara_light"] -- but that always traces back to
    load_index_fresh.json's frozen value, which is null (a fresh/no-career
    account, correctly). Since `isinstance(template, dict)` was required
    before building a live summary, this made single_mode_chara_light
    ALWAYS null on every load/index call, active career or not -- home never
    showed Continue regardless of real progress. The client then re-ran
    single_mode/start's fresh-intro flow, which (correctly, see that
    function's own history) detects the still-live persisted career and
    hands it back instead of the new one the intro was building -- landing
    the player back on their old run partway through what looked like a new
    one. Fix: get the shape from a fixture that actually HAS one populated,
    independent of whatever this account's own frozen snapshot says."""
    global _chara_light_shape_cache
    if _chara_light_shape_cache is None:
        with open(_CHARA_LIGHT_SHAPE_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _chara_light_shape_cache = doc["data"]["data"]["single_mode_chara_light"]
    return copy.deepcopy(_chara_light_shape_cache)


# STORY PROGRESS the snapshot owner had already read/cleared -- 274 chara-
# story episodes, 118 main-story episodes, 412 released/read episodes, ... --
# is not a gameplay container, it's that one player's own personal progress.
# Zeroed here, at the single source both callers share (this function and
# stories.py's _seed_data, which reads _load_seed() directly and does NOT go
# through _neutralize_identity below), so every fresh account starts having
# read nothing (user-supplied 2026-08-16: "make each account start with no
# story done") -- and since stories.py's own gating derives its cleared-set
# from exactly these lists, this fixes our internal unlock state too, not
# just what the client is told.
_STORY_PROGRESS_KEYS = (
    "character_story_data_list", "main_story_data_list",
    "home_story_data_array", "short_episode_data_array",
    "home_poster_data_array", "tutorial_guide_data_array",
    "released_episode_data_array", "talk_gallery_list",
    "home_banner_data_array", "viewed_story_array",
)


def _load_seed() -> dict:
    global _seed_cache
    if _seed_cache is None:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _seed_cache = doc["data"]
        inner = _seed_cache.get("data")
        if isinstance(inner, dict):
            for key in _STORY_PROGRESS_KEYS:
                if key in inner:
                    inner[key] = []
    return copy.deepcopy(_seed_cache)


def _neutralize_identity(seed: dict, viewer_id) -> dict:
    """A NEW account must not be a clone of the snapshot's owner. The seed is
    one real player's login snapshot; before this, every fresh viewer logged in
    AS that player -- their trainer name, 190M fans, register date, honours.

    Gameplay containers (cards, items, music...) are deliberately KEPT: on a
    private server a fresh account starting with the full collection is the
    point, and empty containers are the classic source of client NullRefs.
    Only the IDENTITY is zeroed."""
    data = seed.get("data") or {}
    user_info = data.get("user_info")
    if isinstance(user_info, dict):
        user_info.update({
            "name": f"Trainer{str(viewer_id)[-4:]}",
            "comment": "",
            "fan": 0,
            "rank_score": 0,
            "best_team_evaluation_point": 0,
            "register_time": datetime.now().strftime(TIME_FORMAT),
            # STRING, not int. VERIFIED 2026-08-11 against a live real capture
            # (tools/capture_proxy.py --upstream real): the real server sends
            # "" for an unset birthday, never a bare 0. The raw fixture this
            # seed came from actually had a real string value ('0712') here --
            # neutralizing it to int 0 was itself the bug: a small int encodes
            # as msgpack's "positive fixint", which is the EXACT wire type the
            # client's LoginResponse deserialize error names (code:0
            # format:positive fixint) for this endpoint (load/index IS
            # Gallop.LoginTask's payload -- see docs/LINUX_LOGIN_FIX.md).
            "birth_day": "",
        })
    # Also leaking a real captured account's actual birthdate on every fresh
    # account otherwise (the raw fixture had '20001201' here, untouched by
    # this function). The real server sends null when unset -- same fix
    # class as user_info.birth_day above, found in the same real-capture diff.
    if "user_birth" in data:
        data["user_birth"] = None
    # Story progress is already zeroed at the source -- see
    # _load_seed()/_STORY_PROGRESS_KEYS, which both this function and
    # stories.py's _seed_data() read from.
    return seed


def get_or_seed_data(full_state: dict, viewer_id) -> dict:
    """The viewer's stored load/index blob's data dict, seeding it exactly
    the way load/index would if this endpoint fires first (same seed, same
    identity neutralization). Shared by every handler that needs user_info
    (or any other load/index-served field) before/without a real load/index
    call -- signup, tutorial/skip, change_name/change_sex, account linking."""
    return get_or_seed_blob(full_state, viewer_id).setdefault("data", {})


def get_or_seed_blob(full_state: dict, viewer_id) -> dict:
    """The viewer's whole stored load/index blob, materialized into full_state
    on first use.

    `load_index` is a LAZY key in state.py: get_state deliberately does not
    parse it (it is ~90% of a large account's state and almost no endpoint
    reads it), so it is absent from full_state until something actually asks.
    Routing every reader through here is what makes that safe -- the value
    lands in the dict, so the caller's own save_state(full_state) persists any
    mutation exactly as it did when get_state loaded it eagerly."""
    blob = full_state.get(STATE_KEY)
    if blob is None:
        blob = state_store.load_lazy_key(viewer_id, STATE_KEY)
        if blob is None:
            blob = _neutralize_identity(_load_seed(), viewer_id)
        full_state[STATE_KEY] = blob
    return blob


def _reload_state(viewer_id, previous: dict) -> dict:
    """Re-read the viewer's state, carrying an already-materialized load_index
    forward.

    handle_load_index re-reads state several times so it can see writes made by
    the helpers it calls in between (login_bonus, presents, campaign_walking,
    ...). Each fresh get_state omits the lazy load_index key by design, so
    anything downstream that then asked for the blob re-parsed 3.1 MB from
    scratch -- that was 2 of the 4 full-blob parses this endpoint was doing.

    Carrying the parsed object over is safe precisely because it IS lazy: the
    helpers in between hold state dicts that never contained load_index, and
    save_state never deletes a lazy key that is absent, so the stored blob
    cannot have changed underneath us.
    """
    fresh = state_store.get_state(viewer_id)
    if fresh is None:
        return previous
    if STATE_KEY in previous and STATE_KEY not in fresh:
        fresh[STATE_KEY] = previous[STATE_KEY]
    return fresh


def apply_derived_user_info(full_state: dict, user_info: dict) -> dict:
    """Overlay every user_info field this server DERIVES from live state, in
    place, and return user_info.

    The stored load blob's own copies of these fields are stale by design --
    they are only ever refreshed onto the RESPONSE, never written back -- so
    EVERY endpoint that hands the client a user_info has to run it through
    here. That is not cosmetic: the client applies a served user_info to
    WorkUserData wholesale, so a stale field does not merely look wrong on one
    screen, it overwrites the good value load/index had established.

    BUG FIXED 2026-09-09 (live-reported: changing the displayed uma relocked
    Daily Program). user/change_leader_card and its siblings echo the STORED
    user_info, whose best_team_evaluation_point is 0, which dropped the
    client's Team Rank to 0 -- the same lock the 2026-09-08 fix below removed,
    arriving through a different door.
    """
    # Real per-account Team Stadium roster strength (team_stadium.py), not the
    # hardcoded 999999 this used to unconditionally overwrite every call with
    # regardless of the account's actual state.
    #
    # BUG FIXED 2026-09-08 (live-reported: "Daily Program locked at Team Rank E
    # on a top-of-ladder account"). This used to serve best_point, the weekly
    # Team Trial score -- a different number entirely, and one with no
    # team_stadium_rank band, so the client's point->rank scan matched nothing
    # and read the account as rank 0. See team_stadium.rank_display_point.
    from . import team_stadium
    ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
    user_info["best_team_evaluation_point"] = team_stadium.rank_display_point(ts_state)
    # Follow Slot Boosts bought via item/exchangeAddFrame (shop.py) -- the
    # real load/index reports the lifetime total here.
    from . import shop
    bonus = full_state.get(shop.BONUS_FOLLOW_KEY)
    if bonus:
        user_info["bonus_follow_num"] = bonus
    return user_info


def handle_load_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    blob = get_or_seed_blob(full_state, viewer_id)
    # An independent copy of the blob, re-parsed from its stored JSON text
    # rather than copy.deepcopy'd off the live object. deepcopy has to walk
    # every one of the ~422,000 Python objects in this 3.1 MB structure (137 ms
    # measured); re-parsing the text costs 54 ms, and state.py is holding that
    # text anyway. The response is mutated below (update_time, the live-data
    # overlay) and the stored copy must not be, so it does have to be a real
    # copy -- this is the same copy, made a cheaper way.
    #
    # load_lazy_key returns None only when the blob was just seeded above and
    # has not been persisted yet, in which case there is no stored text to
    # parse and deepcopy is the correct fallback. Safe here because this runs
    # before the save_state below, so disk and `blob` are still in agreement.
    response = state_store.load_lazy_key(viewer_id, STATE_KEY)
    if response is None:
        response = copy.deepcopy(blob)

    user_info = response.get("data", {}).get("user_info")
    if isinstance(user_info, dict) and "update_time" in user_info:
        user_info["update_time"] = datetime.now().strftime(TIME_FORMAT)
        apply_derived_user_info(full_state, user_info)

    # Persist (update_time intentionally excluded from what's stored --
    # only the response we hand back gets the refreshed timestamp, so it
    # keeps being "now" relative to each individual call).
    state_store.save_state(viewer_id, full_state)

    _overlay_live_data(response, viewer_id, full_state)

    # circle_data -- the home screen's own copy of "which circle am I in".
    # It used to come straight out of the frozen seed, so EVERY account
    # inherited the snapshot owner's membership ("Espresso 5", circle 301102622
    # -- a circle that does not exist on this server) while an account that
    # really did create or join one was never told about it. Overlaid from the
    # live social graph, in the exact two shapes the real captures use:
    # {circle_info: {circle_id, name}, circle_user: <row>} for a member, and
    # circle_info null with a circle_id-0 circle_user for everyone else.
    data = response.get("data")
    if isinstance(data, dict):
        from .. import social
        from . import circles
        member = social.member_row(viewer_id)
        circle = social.get_circle(member["circle_id"]) if member else None
        if circle is None:
            data["circle_data"] = {"circle_info": None,
                                   "circle_user": circles.no_circle_user(viewer_id)}
        else:
            data["circle_data"] = {
                "circle_info": {"circle_id": circle["circle_id"],
                                "name": circle["name"]},
                "circle_user": member,
            }

    # Real daily login bonus -- see login_bonus.py's module docstring. Runs
    # BEFORE the present-count badge below so a just-granted reward is
    # reflected in present_num on the SAME response that reports it, not one
    # login later.
    data = response.get("data")
    if isinstance(data, dict) and "login_bonus_list" in data:
        from . import login_bonus, presents
        box_before = len((state_store.get_state(viewer_id) or {})
                         .get(presents.PRESENT_BOX_KEY, {}).get("presents") or [])
        data["login_bonus_list"] = login_bonus.apply_and_report(viewer_id)
        # notifications.add_present_num -- confirmed 2026-08-19 against a real
        # capture (captures/20260819_092154/0060_load_index.json, the login-
        # bonus grant call): real data_headers.notifications carries this
        # DELTA (how many presents THIS call just added), not the running
        # total menu_badge_info.present_num already reports. User-reported
        # 2026-08-19: "it gives me the rewards, but the animation doesnt play
        # anymore" -- once login_bonus_list stopped being re-sent every call
        # (the fix for the popup repeating), the animation lost its OTHER
        # trigger too, because this delta was never populated at all: the
        # reward silently landed in the mailbox with nothing telling the
        # client new mail had just arrived.
        box_after = len((state_store.get_state(viewer_id) or {})
                        .get(presents.PRESENT_BOX_KEY, {}).get("presents") or [])
        added = box_after - box_before
        if added > 0:
            notifs = response.setdefault("data_headers", {}).setdefault("notifications", {})
            notifs["add_present_num"] = (notifs.get("add_present_num") or 0) + added

    # Account-wide fan count, same derivation missions.py's FanNum trusts:
    # the sum of the (now live, just-overlaid) chara_list's own fan fields --
    # not the frozen seed value, which never moved no matter how many fans
    # were actually earned since.
    if isinstance(user_info, dict) and isinstance(data, dict) and \
            isinstance(data.get("chara_list"), list):
        user_info["fan"] = sum(c.get("fan") or 0 for c in data["chara_list"])

    # menu_badge_info (the home-screen "N unclaimed" badges) was ALSO frozen
    # from the captured seed account's own counts at capture time (present_num
    # 44, mission_num 4, ...) -- same class of bug as best_team_evaluation_point
    # above, and why presents/missions looked "already full" regardless of
    # your real, empty-or-claimed state. Overlay with real live counts.
    # Re-fetch full_state fresh here: login_bonus.apply_and_report (above)
    # may have just granted new presents through its own independent
    # save (see its module docstring) that THIS function's own full_state
    # copy -- fetched before that ran -- wouldn't reflect.
    badge = data.get("menu_badge_info") if isinstance(data, dict) else None
    if isinstance(badge, dict):
        import time as _time
        from . import presents, missions
        full_state = _reload_state(viewer_id, full_state)
        badge["present_num"] = len(presents._pending(full_state, int(_time.time())))
        mission_list = missions.build_mission_list(viewer_id, full_state)
        badge["mission_num"] = sum(
            1 for m in mission_list if m["mission_status"] == missions._STATUS_CLEAR)
        # Sub-badges for systems this server doesn't implement (Legend Race,
        # Training Challenge, Champions/Challenge Match, Team Building, the
        # "view limited mission" counter) -- honestly zero rather than the
        # frozen seed's arbitrary nonzero values, which would show a
        # permanent false "something to claim" badge for features that don't
        # exist here.
        badge["legend_mission_num"] = 0
        badge["training_challenge_mission_num"] = 0
        badge["challenge_match_mission_num"] = 0
        badge["team_building_mission_num"] = 0
        badge["view_limited_mission_num"] = 0

    # directory_card_num -- the ARCHIVE button's "!" badge, and the last count
    # in this family still frozen (at 0, so the badge never appeared at all).
    # Live-reported 2026-09-03: "whenever there's a new archive level reached
    # it should have a ! symbol beside the archive button".
    #
    # note_archive tracks the last archive rank it actually SERVED as
    # note_state["level"], and it only updates that when the player opens the
    # archive. So "levels gained since you last looked" is exactly
    # current_rank - level, which is what the badge should count.
    if isinstance(data, dict) and "directory_card_num" in data:
        try:
            from . import note_archive, trained_chara as _tc
            full_state = _reload_state(viewer_id, full_state)
            note = note_archive._note_state(full_state)
            roster = _tc._get_or_seed_roster(viewer_id)
            rank = note_archive._rank_for(
                note_archive._total_score(
                    note_archive._score_summary(full_state, note, roster)))
            data["directory_card_num"] = max(0, rank - (note.get("level") or 1))
        except Exception:
            log.exception("directory badge computation failed; leaving it at 0")
            data["directory_card_num"] = 0

    # support_user_num -- ALSO frozen from the captured seed (0, same as
    # every other count above) -- found 2026-08-19 after the Borrow Card
    # screen kept showing "no Support Cards to borrow" even once friend_
    # support_card_data (pre_single_mode/index) genuinely carried an
    # injected lender with a correct card, correct mutual friend_state,
    # and correct every other field this session's earlier fixes checked:
    # the CLIENT likely short-circuits the Borrow list off THIS count,
    # from the cached login snapshot, before ever looking at the actual
    # array -- so a frozen 0 here would hide a real, correct lender
    # regardless of anything in pre_single_mode/index. Computed from the
    # same injected-friend list that endpoint already uses.
    if isinstance(data, dict) and "support_user_num" in data:
        from . import single_mode_team
        full_state = _reload_state(viewer_id, full_state)
        data["support_user_num"] = len(
            full_state.get(single_mode_team.FRIEND_SUPPORT_INJECT_KEY) or [])

    # campaign_walking_load_info -- frozen at null in the seed (this feature
    # didn't exist yet); serve the real per-viewer gauge/counters so the
    # home-screen outing icon and its resume-in-progress state are live.
    if isinstance(data, dict) and "campaign_walking_load_info" in data:
        from . import campaign_walking
        data["campaign_walking_load_info"] = campaign_walking.load_info(viewer_id)

    # chara_profile_array -- the profile-screen unlock set, frozen from the
    # captured seed (237 rows describing the CAPTURE account's bond ranks and
    # costume cards) and never touched by anything, so the profile screen
    # showed someone else's unlocks and never reacted to this player's own
    # bond. Derived live from bond ranks + owned cards instead; bond.py owns
    # the derivation and the "already announced" set (new_flag).
    if isinstance(data, dict) and "chara_profile_array" in data:
        from . import bond
        full_state = _reload_state(viewer_id, full_state)
        data["chara_profile_array"] = bond.chara_profile_array(full_state)
        state_store.save_state(viewer_id, full_state)

    # unread_announce_id_array -- frozen from the captured seed ([30116,
    # 30117], real master.mdb announce_data rows) and never touched by
    # anything: every load/index call re-served the SAME "unread"
    # announcement ids forever, regardless of whether the client already
    # showed them -- so a news popup (a concert announcement, a story
    # notice) kept reappearing on every relaunch instead of the one time it
    # should. User-reported 2026-08-19: "supposed to be one time but keep
    # showing up... every time I restart the game". Track dismissal
    # server-side, same "frozen seed masquerading as live state" bug class
    # as menu_badge_info/support_user_num above: once an id has been served
    # once, mark it seen and never serve it again.
    if isinstance(data, dict) and "unread_announce_id_array" in data:
        full_state = _reload_state(viewer_id, full_state)
        seen = full_state.setdefault(SEEN_ANNOUNCE_KEY, [])
        ids = data["unread_announce_id_array"] or []
        data["unread_announce_id_array"] = [i for i in ids if i not in seen]
        seen.extend(i for i in ids if i not in seen)
        state_store.save_state(viewer_id, full_state)

    # Refresh this account's public directory card -- the compact profile every
    # OTHER player's friend list, circle roster and recommend list renders it
    # with (handlers/directory.py). Login is the right and only moment for it:
    # the card is a snapshot of who this player was when they last logged in,
    # which is exactly what the last_login_time beside it advertises.
    #
    # Deliberately last, after every block above has finished mutating state,
    # so the card is built from the fully-updated login snapshot rather than a
    # half-refreshed one. Cheap (it reads keys already in hand) and non-fatal:
    # a failure here must never break a login.
    try:
        from . import directory, house_lenders
        full_state = _reload_state(viewer_id, full_state)
        if directory.publish(viewer_id, full_state):
            state_store.save_state(viewer_id, full_state)
        # The house support-card lenders exist as real accounts and are
        # mutual friends of everyone, so the Borrow Card screen -- which
        # filters against your ACTUAL friend list -- keeps finding them now
        # that the friend list is a real social graph rather than a
        # per-account synthetic array. Both calls are idempotent and cheap
        # after the first login.
        house_lenders.ensure_accounts()
        house_lenders.ensure_followed(viewer_id)
    except Exception:
        log.exception("load/index: failed to publish directory card for %s", viewer_id)
    return response


def _overlay_live_data(response: dict, viewer_id, full_state: dict | None = None) -> None:
    """load/index embeds several per-viewer collections as a login snapshot the
    client caches locally. Left alone they're the frozen seed. Overlay the live,
    per-viewer state so what login/home screens see is dynamic and persistent:

      * data.trained_chara            -> the live house roster (SEPARATE from
        trained_chara/load, but the client reads this copy on screens like the
        practice-race trainee picker). Dependent arrays reference entries by
        trained_chara_id, so remap them onto the live roster (same counts/shape)
        rather than leaving dangling refs. practice_partner_* arrays are other
        users' borrowed partners and are deliberately left untouched.
      * data.card_list / support_card_list / chara_list -> the owned collection
        (see collection.py) -- trainable cards' potential/hint levels, support
        cards' uncap levels, character meta -- served from state so they persist
        and can be modified instead of being the frozen fixture."""
    data = response.get("data")
    if not isinstance(data, dict):
        return

    # 1. legacy/veteran roster (also cached here as the login snapshot)
    if isinstance(data.get("trained_chara"), list):
        roster = trained_chara._get_or_seed_roster(viewer_id)
        if roster:
            data["trained_chara"] = copy.deepcopy(roster)
            ids = [c["trained_chara_id"] for c in roster]
            # Remap ONLY the slots the fixture actually fills, and give each a
            # DISTINCT roster member.
            #
            # BUG FIXED 2026-09-03 (live-reported: "the default team trial
            # composition has many umas appearing twice, and the second and
            # third row already have duplicate umas in them -- they should be
            # empty and 5 umas only should be in the first row").
            #
            # The fixture is already right: 15 team_data_array entries, 5 per
            # member_id (the row), and every row-2/row-3 slot carries
            # trained_chara_id 0 -- i.e. EMPTY, because rows 2 and 3 only
            # unlock at team class 2 and 3. This loop overwrote every entry
            # that merely HAD the key, which filled those empty slots, and
            # `ids[i % len(ids)]` wrapped the roster positionally, which is
            # where the duplicates came from.
            #
            # An occupied slot with no distinct member left is emptied rather
            # than duplicated: the same uma cannot hold two team slots, so a
            # short roster must leave gaps.
            for arr_key in ("trained_chara_favorite_array", "team_data_array"):
                arr = data.get(arr_key)
                if not isinstance(arr, list):
                    continue
                unused = list(ids)
                for entry in arr:
                    if not isinstance(entry, dict) or "trained_chara_id" not in entry:
                        continue
                    if not entry.get("trained_chara_id"):
                        continue          # empty slot -- must stay empty
                    if unused:
                        entry["trained_chara_id"] = unused.pop(0)
                    else:
                        entry["trained_chara_id"] = 0
                        if "running_style" in entry:
                            entry["running_style"] = 0

    # 2. owned collection + all currencies / items / pieces / cosmetics: every
    # dynamic container, seeded once from this snapshot then served from state
    # (see collection.py). Covers card_list/support_card_list/chara_list plus
    # coin_info, tp_info, rp_info, item_list, piece_list, cloth_list, music_list.
    # One state read for all 11 containers rather than one per container --
    # see collection.get_or_seed_many.
    _seeds = {k: data[k] for k in collection.DYNAMIC_CONTAINERS if k in data}
    for data_key, value in collection.get_or_seed_many(viewer_id, _seeds).items():
        data[data_key] = copy.deepcopy(value)

    # TP/RP regenerate against the wall clock, so the pair seeded above is a
    # snapshot of whenever it was last written -- possibly days ago, and (on a
    # brand-new account) a real player's frozen mid-refill values straight out
    # of the capture. Tick both to NOW before serving: load/index is where the
    # client caches these for every screen that spends them, so a stale pool
    # here is a stale pool everywhere.
    #
    # This read is the one _shared below goes on to use, so ticking the pools
    # costs no extra parse of the account -- see that block's own note.
    _shared = state_store.get_state(viewer_id) or {}
    stamina.refresh(_shared)
    state_store.save_state(viewer_id, _shared)
    data["tp_info"] = stamina.tp_info(_shared)
    data["rp_info"] = stamina.rp_info(_shared)

    # The store's age gate, once this account has confirmed it. _neutralize_
    # identity zeroes user_birth to null at seed time (correct -- a fresh
    # account genuinely has not confirmed), but nothing ever set it again, so
    # payment/update_birth's answer never made it back into the login snapshot
    # and the confirmation dialog reappeared on every visit to the store. Both
    # halves are served together because the client reads them as a pair --
    # see payment.USER_BIRTH_KEY for the YYYYMM/YYYYMMDD split.
    from . import payment
    _birth = payment.user_birth(_shared)
    if _birth:
        data["user_birth"] = _birth["user_birth"]
        data["optin_user_birth"] = _birth["optin_user_birth"]

    # 3. single_mode_chara_light -- the home screen's "career in progress"
    # summary (drives Continue vs Start Career). Derive it from the LIVE career
    # state instead of the frozen snapshot: no active career -> null (VERIFIED
    # 2026-08-11 against a live capture of the real server via tools/capture_proxy.py
    # -- it sends nil, not a zeroed struct; our old zeroed placeholder was ALSO
    # leaking the frozen fixture's leftover stats/succession ids, e.g. speed:155,
    # succession_trained_chara_id_1:1776, on every fresh account); active career
    # -> a summary built from the run's chara_info so the home reflects real
    # progress turn to turn.
    # ONE state read shared by every read-only lookup below (career summary,
    # daily races, jukebox, stories, team stadium). Each of these used to call
    # get_state separately, parsing the whole account five times to read five
    # keys. It is the read taken by the TP/RP tick above -- the last of the two
    # things in this function that write (container seeding is the other), so
    # by this point it is still current. The blob is carried over from the
    # caller so a consumer that needs it (jukebox's history fallback) does not
    # re-parse 3.1 MB.
    if full_state and STATE_KEY in full_state and STATE_KEY not in _shared:
        _shared[STATE_KEY] = full_state[STATE_KEY]

    if "single_mode_chara_light" in data:
        from . import single_mode_team
        sm_state = _shared
        career = sm_state.get(single_mode_team.STATE_KEY)
        chara_info = career.get("data", {}).get("chara_info") if isinstance(career, dict) else None
        data["single_mode_chara_light"] = (
            _career_chara_light(_chara_light_template(), chara_info)
            if chara_info else None
        )

    # 3b. daily race / daily legend race playing info -- served FROZEN from the
    # fixture until now, which is the same "frozen seed masquerading as live
    # state" bug class as tool/start_session's unread_information_exists and
    # this file's own unread_announce_id_array. The legend block's
    # `new_flag: 1` in particular is a permanent badge: the client shows the
    # daily tag off it, so it never cleared no matter what the player did
    # (live-reported 2026-09-03: "the daily tag shows even when there isn't a
    # daily race running"). Serve both from the live per-day daily-race state
    # instead, which daily_races.py already maintains and rolls per day.
    from . import daily_races
    drs = daily_races._daily_state(_shared)
    # The seed froze daily_race_ticket_max_num at 6 because it was captured
    # mid-campaign; the real value is 3 plus any active RaceCount campaign.
    common_define = data.get("common_define")
    if isinstance(common_define, dict):
        common_define["daily_race_ticket_max_num"] = daily_races.ticket_cap()
    info = data.get("daily_race_playing_info")
    if isinstance(info, dict) and isinstance(info.get("daily_race_record_array"), list):
        for rec in info["daily_race_record_array"]:
            if not isinstance(rec, dict):
                continue
            live = (drs.get("daily") or {}).get(str(rec.get("daily_race_id"))) or {}
            rec["is_played"] = 1 if live.get("is_played") else 0
            rec["is_cleared"] = 1 if live.get("is_cleared") else 0
        info["state"] = 0
        info["trained_chara_id"] = 0
    legend = data.get("daily_legend_race_playing_info")
    if isinstance(legend, dict):
        legend["state"] = 0
        legend["trained_chara_id"] = 0
        legend["daily_legend_race_record"] = [
            {"daily_legend_race_id": int(rid),
             "is_played": 1 if v.get("is_played") else 0,
             "is_cleared": 1 if v.get("is_cleared") else 0}
            for rid, v in sorted((drs.get("legend") or {}).items())]
        # new_flag is "there is something new here", not a constant. This
        # server runs no daily-legend rotation, so there is never anything new
        # to advertise -- 0, not the fixture's permanent 1. (Flipping it to
        # "1 when nothing has been played" would be worse: that is exactly the
        # always-on badge being reported.)
        legend["new_flag"] = 0

    # 4. border_line -- a single leftover object in the frozen fixture (a
    # team-stadium ranking border) that the real server sends as an EMPTY
    # LIST when there is none, not a populated struct. VERIFIED 2026-08-11:
    # a live real capture (captures/20260816_202505/0006_load_index.json)
    # sends [] here even for an account with unrelated live history elsewhere
    # -- left alone this is a genuine wire-type mismatch (map where the
    # client expects an array), not merely stale content. We don't yet track
    # any live team-stadium standing to serve instead, so always [].
    if isinstance(data.get("border_line"), dict):
        data["border_line"] = []

    # 4b. jukebox_request_history -- despite living right next to border_line
    # in the fixture, this one is NOT list-shaped: the client's own response
    # class (dump.cs) declares it a single `JukeboxRequest` object, and two
    # real captures confirm it: a fresh/no-history account gets null
    # (data/seeds/load_index_fresh.json, the original 20260717 capture), an
    # account WITH history gets a real populated object
    # (captures/20260816_202505/0006_load_index.json) -- never []. An earlier
    # version of this function wrongly generalized border_line's
    # always-empty-list behavior onto this field too, which meant a song set
    # via jukebox/play_user_request would persist correctly server-side (see
    # jukebox_requests.py) but never show up again here on the next
    # load/index -- forced to [] regardless of live state. Mirror
    # jukebox.handle_index's own logic instead: latest live history entry, or
    # null if there isn't one yet.
    from .jukebox_requests import _jukebox_state
    jukebox_st = _jukebox_state(_shared, viewer_id)
    jukebox_history = jukebox_st.get("history") or []
    data["jukebox_request_history"] = jukebox_history[-1] if jukebox_history else None

    # 4c. main_story_data_list / character_story_data_list -- same class of
    # bug as jukebox_request_history above, but the ORIGINAL fix here (only
    # correcting an existing entry's `state` flag, never adding/removing
    # entries) turned out to be wrong about the real shape: these lists
    # start EMPTY for a fresh account (this file's own _STORY_PROGRESS_KEYS
    # zeroing, a few lines up) and NEVER had anything for the "correct
    # state on existing entries" loop to act on, so a genuinely-cleared
    # episode never appeared here at all. Checked against real captures
    # from THIS same session (captures/*/*/load_index.json): the list
    # actually GROWS one entry per cleared episode across sessions (274 ->
    # 293 -> 294 -> 295 chara entries over several real captures days
    # apart) -- it's a running list of what's been cleared, not a fixed
    # enumeration with a flag. User-reported 2026-08-19: "the game lets me
    # view the next one, but when I restart... all my progress is gone...
    # (I dont get its rewards tho)" -- exactly this: stories.py's OWN
    # reward-gate (chara_cleared/main_cleared) was always correct, hence no
    # re-grant, but this list stayed permanently empty so the CLIENT's own
    # unlock display had nothing to show as cleared after a fresh load.
    # Rebuilt from live state instead: one {episode_id, state:1} entry per
    # id in chara_cleared/main_cleared, nothing for uncleared ones (every
    # entry in every real capture checked was state:1 -- there's no
    # confirmed 0-state entry shape to reproduce, so this doesn't invent
    # one). Ignores the pre-neutralized seed's own list content entirely
    # now; state_1-only entries are the whole list.
    from . import stories
    story_st = stories._story_state(_shared)
    if isinstance(data.get("main_story_data_list"), list):
        data["main_story_data_list"] = [
            {"episode_id": eid, "state": 1}
            for eid in sorted(set(story_st.get("main_cleared") or []))]
    if isinstance(data.get("character_story_data_list"), list):
        data["character_story_data_list"] = [
            {"episode_id": eid, "state": 1}
            for eid in sorted(set(story_st.get("chara_cleared") or []))]

    # 4c-bis. The READ / ALREADY-ANNOUNCED arrays that live right next to
    # those two -- released_episode_data_array, home_story_data_array,
    # short_episode_data_array, home_poster_data_array,
    # tutorial_guide_data_array, talk_gallery_list, home_banner_data_array.
    # read_info/index (stories.py) already persists every id the client
    # reports here, but load/index kept serving the seed's zeroed [] on
    # EVERY login, so the login snapshot the client caches said "you have
    # been shown nothing, ever". The client re-announces newly RELEASED
    # story episodes off exactly that set, which is why a story unlock
    # popped its "new story" notification again on every home entry and
    # after every career finish instead of once -- user-reported
    # 2026-09-08: "a notification pops up about Maruzensky story unlocks
    # and I keep seeing it pop up... it should only show once but it shows
    # every time" (chara 1004 -> the 41004xxx episode ids sitting in that
    # account's own persisted released_episode_data_array, acknowledged and
    # then forgotten again by this very endpoint). Same "frozen seed
    # masquerading as live state" bug class as unread_announce_id_array and
    # the two story lists above; the live set is the whole answer, so
    # replace outright rather than patching entries.
    #
    # Shapes are the real server's (captures/20260811_141230/0001_load_index
    # .json, an account with 412 released episodes): [{id}] everywhere except
    # talk_gallery_list, which is [{home_story_trigger_id, new_flag}] -- the
    # same two shapes read_info/index itself serves.
    read = story_st.get("read_info") or {}
    for state_key, load_key in stories._SEED_KEYS.items():
        if not isinstance(data.get(load_key), list):
            continue
        ids = sorted(set(read.get(state_key) or []))
        data[load_key] = (
            [{"home_story_trigger_id": i, "new_flag": 0} for i in ids]
            if load_key == "talk_gallery_list" else [{"id": i} for i in ids])

    # 4d. team_data_array -- same class of bug again: this is the Team Stadium
    # roster (see team_stadium.py), frozen at capture time from an account
    # that had already set one, and never synced with the live
    # team_stadium_state team_stadium/team_edit actually persists into. A
    # roster saved via team_edit would apply server-side (verified) but the
    # next load/index kept showing the OLD frozen 15 slots, so the client's
    # roster screen appeared to have "reverted". Unlike jukebox's single
    # object or story's fixed id-set-with-a-flag, this one's content (which
    # horse, which style) IS exactly what team_edit owns, so replace it
    # outright with the live state rather than patching fields in place.
    from .team_stadium import TEAM_STADIUM_STATE_KEY
    team_st = _shared.get(TEAM_STADIUM_STATE_KEY)
    if isinstance(team_st, dict):
        data["team_data_array"] = copy.deepcopy(team_st.get("team_data_array") or [])


def _career_chara_light(template: dict, chara_info: dict) -> dict:
    """Summary of the active career for the home screen: copy every field the
    chara_info and the light share, so stats/turn/etc. track the live run."""
    scl = copy.deepcopy(template)
    for key in list(scl.keys()):
        if key in chara_info:
            scl[key] = copy.deepcopy(chara_info[key])
    if not scl.get("playing_state"):
        scl["playing_state"] = 1  # TurnStart
    return scl
