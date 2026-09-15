"""The house support-card lenders, as REAL accounts on the server.

WHAT THESE ARE
--------------
single_mode_team.py seeds every account with a set of always-borrowable
"friends" lending one MLB SSR support card each -- Light Hello, Fine Motion,
Riko Kashimoto, Kitasan Black, Heirs to the Throne, Team Sirius. That exists
because a career deck slot can REQUIRE a borrowed card, so with nobody to
borrow from, career start is a hard blocker rather than a cosmetic gap
(user-corrected 2026-08-19).

Those lenders were per-account synthetic entries living inside each viewer's
own state: they had no accounts of their own, so they could be borrowed from
and nothing else. They could not be followed, searched, opened in Trainer
Info, or scouted into a circle, and they existed once per account rather than
once on the server.

This module gives them real, shared server-side identities at the SAME
viewer_ids the synthetic entries already use (900000500000 + their index in
single_mode_team._DEFAULT_FRIEND_CARDS -- verified against the live database:
every account that has them already agrees on those ids, so this is
continuity, not a renumbering). They now have persisted state, a directory
card, a roster and a support-card collection, which makes them first-class
participants in the serverwide social graph: everyone follows them, they
follow everyone back (so they read as mutual friends, state 3), and every
screen that renders a real player renders them too.

WHY THEY HAVE NO LOGIN BLOB
---------------------------
A real account's directory card is derived from its load/index blob, which is
~3.1 MB. These accounts never log in, so seeding six of those would cost
~18 MB to store facts we already know. build_card is bypassed and their cards
are written directly instead -- the one place in this codebase where a
directory card is authored rather than derived, precisely because there is no
player behind it to derive it from.

THIS IS NOT FABRICATING PLAYERS
-------------------------------
The rule this project follows (user_profile.py, single_mode_team.py) is: do
not invent other real players. These are not presented as other players --
they are the house's own lending accounts, they were already an explicit,
user-directed exception to that rule, and their numbers are deliberately
minimal and obviously non-competitive (1 fan, rank_score 1) rather than
dressed up to look like a real trainer's career.
"""

from __future__ import annotations

import logging

from .. import master_data, social
from .. import state as state_store
from . import collection, directory, trained_chara

log = logging.getLogger("uma-server")

STATE_MARKER_KEY = "house_lender"

# The character a lender is shown partnered with. These mirror the values
# inside single_mode_team.inject_friend_support_card (chara 1032 with a real
# dress_data row for it), where they are function-local -- and they are NOT
# arbitrary: single_mode_team.py documents at length that an id here which
# does not resolve to a real master.mdb row renders as nothing at all rather
# than erroring, so both must stay real rows.
_TEMPLATE_CHARA_ID = 1032
_TEMPLATE_DRESS_ID = 103201


def lender_viewer_id(index: int) -> int:
    """Stable id for the Nth default lending card. Matches the ids already in
    the live database (see the module docstring) -- do not change these
    without migrating existing accounts' injected entries, which key off
    exactly these values."""
    from . import single_mode_team
    return single_mode_team._FRIEND_SENTINEL_VIEWER_BASE + index


def lender_ids() -> list:
    from . import single_mode_team
    return [str(lender_viewer_id(i))
            for i in range(len(single_mode_team._DEFAULT_FRIEND_CARDS))]


def _build_state(index: int, card_id: int, name: str) -> dict:
    """One lender's whole persisted state: the card they lend, one horse, and
    the directory card every social screen renders them with.

    The support card's real level/exp come from master.mdb through
    single_mode_team.inject_friend_support_card's own lookup, so a lender
    lends exactly the card the borrow screen has always offered -- same id,
    same MLB, same exp -- rather than a second, divergent definition of it.
    """
    from . import single_mode_team as smt

    vid = str(lender_viewer_id(index))
    stamp = social.now()
    row = master_data.query_one(
        "SELECT rarity FROM support_card_data WHERE id=?", (card_id,))
    if row is None:
        raise ValueError(f"no support_card_data row for id {card_id}")
    top = master_data.query_one(
        "SELECT MAX(level) AS lv FROM support_card_level WHERE rarity=?", (row["rarity"],))
    level = (top["lv"] if top else None) or 1
    exp_row = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (row["rarity"], level))
    exp = exp_row["total_exp"] if exp_row else 0

    horse = {**smt._FRIEND_TEMPLATE_TRAINED_CHARA,
             "viewer_id": int(vid),
             "trained_chara_id": 9001 + index,
             "register_time": stamp}
    support_card = {
        "viewer_id": int(vid), "support_card_id": card_id, "exp": exp,
        "limit_break_count": smt._DEFAULT_FRIEND_LIMIT_BREAK,
        "favorite_flag": 0, "stock": 0,
        "possess_time": stamp, "create_time": stamp,
    }
    card = {
        "version": directory.CARD_VERSION,
        "viewer_id": int(vid),
        "name": name,
        "honor_id": 100101, "honor_data": {"honor_id": 100101},
        "last_login_time": stamp,
        "leader_chara_id": _TEMPLATE_CHARA_ID,
        "leader_chara_dress_id": _TEMPLATE_DRESS_ID,
        "support_card_id": card_id,
        "partner_chara_id": _TEMPLATE_CHARA_ID,
        "comment": "",
        # Deliberately minimal: these are the house's lending accounts, not
        # rival trainers, and inflating them would put fake players at the top
        # of every ranking-adjacent list on the server.
        "fan": 1, "rank_score": 1,
        "team_stadium_win_count": 0, "single_mode_play_count": 1,
        "team_evaluation_point": 0, "best_team_evaluation_point": 0,
        "directory_level": 1,
        "user_support_card": support_card,
        "user_trained_chara": directory._summary_trained_chara(vid, horse),
    }
    return {
        STATE_MARKER_KEY: {"index": index, "support_card_id": card_id, "name": name},
        directory.DIRECTORY_KEY: card,
        trained_chara.ROSTER_KEY: [horse],
        collection.SUPPORT_CARD_KEY: [support_card],
    }


def ensure_accounts() -> list:
    """Create (or refresh) every house lender's account. Idempotent -- safe to
    call on every login, and cheap: it rewrites a lender's state only when the
    card version or the definition actually changed, because state.py's
    save_state writes only keys whose serialized text differs."""
    from . import single_mode_team

    ids = []
    for index, (card_id, name) in enumerate(single_mode_team._DEFAULT_FRIEND_CARDS):
        vid = str(lender_viewer_id(index))
        ids.append(vid)
        existing = state_store.get_state(vid)
        card = (existing or {}).get(directory.DIRECTORY_KEY)
        if (existing is not None and isinstance(card, dict)
                and card.get("version") == directory.CARD_VERSION
                and card.get("support_card_id") == card_id):
            continue
        try:
            fresh = _build_state(index, card_id, name)
        except Exception:
            log.exception("house_lenders: could not build lender for card %s", card_id)
            continue
        # Merge rather than replace: a lender that has picked up real social
        # state (followers, a circle membership) must not lose it to a
        # definition refresh.
        merged = dict(existing or {})
        merged.update(fresh)
        state_store.save_state(vid, merged)
    return ids


def ensure_followed(viewer_id) -> None:
    """Make this viewer and every house lender mutual friends.

    Mutual (state 3), not one-way: these are meant to read as friends you can
    borrow from, and the Borrow Card screen filters against the ACTUAL friend
    list (user-reported 2026-08-18). A one-way follow would put them in the
    list but not make them friends.

    Skipped for the lenders themselves, so they do not follow each other.
    """
    me = str(viewer_id)
    if me in set(lender_ids()):
        return
    mine = social.following(me)
    theirs = social.followers(me)
    for vid in lender_ids():
        try:
            if vid not in mine:
                social.follow(me, vid)
            if vid not in theirs:
                social.follow(vid, me)
        except Exception:
            log.exception("house_lenders: could not link %s <-> %s", me, vid)
