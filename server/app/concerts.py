"""Concert (music) unlocking -- deciding which songs an account has earned.

WHY THIS EXISTS
---------------
master.mdb does NOT carry a per-song unlock rule. `live_data.condition_type`
is display behaviour only (LiveConditionType: Common / UserGet /
SecretUntilUserGet, per the Il2Cpp dump), and text_data category 252 spells out
an acquisition line for just four songs. Every other song is a REWARD granted
by the story, scenario or event that hands it out.

The complete list of those handouts is documented on the Concert Theater wiki
page vendored at `docs/Game_Concert Theater - Umamusume Wiki.html` ("How to
Acquire", one line per song). `tools/gen_concert_unlocks.py` turns that page
into `app/data/concert_unlocks.json`, a structured table this module evaluates.

GLOBAL ONLY
-----------
The generator keeps a song only if its title resolves to a music_id present in
`live_data` -- which is the Global song set. JP-only songs (later scenarios,
later anniversary stories) never resolve and are dropped at generation time, so
nothing here can grant a song this region does not have. 25 of the wiki's 50
songs survive that filter.

HOW IT GRANTS
-------------
Through the present box, like every other earned reward on this server (see
presents._grant): the mailbox is the one path that reports a grant to the
client honestly, and load/index -- which is where music_list actually comes
from -- is cached by the client until relaunch, so a direct write would not
show up until a restart anyway.

Songs already granted are tracked by a watermark so re-running the check is
idempotent.
"""

from __future__ import annotations

import functools
import json
import logging
import os

from . import master_data

log = logging.getLogger("uma-server")

CONCERT_STATE_KEY = "concerts_granted"     # [music_id, ...] already handed out
_MUSIC_REWARD_TYPE = 80                    # presents reward_type for music
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "concert_unlocks.json")

# Grade order for "ranked G2 or lower". race.grade is 100=G1 .. 900=Debut, so a
# LOWER-ranked race has a HIGHER number.
_GRADE_VALUE = {"g1": 100, "g2": 200, "g3": 300, "op": 400, "pre_op": 700,
                "maiden": 800, "make_debut": 900}

# Kinds this server has no progress source for yet. Listed rather than silently
# evaluated false so the gap is visible in one place.
UNTRACKED_KINDS = frozenset({"extra_story_episode", "story_event_episode"})


@functools.lru_cache(maxsize=1)
def table() -> tuple:
    try:
        with open(_DATA, encoding="utf-8") as fh:
            return tuple(json.load(fh).get("songs") or ())
    except Exception:
        log.exception("concert unlock table unreadable at %s", _DATA)
        return ()


def untracked_songs() -> list:
    """[(music_id, title, kind), ...] whose unlock this server cannot detect."""
    return [(s["music_id"], s["title"], s["kind"]) for s in table()
            if s["kind"] in UNTRACKED_KINDS or s["kind"] == "unknown"]


# ------------------------------------------------------------ evaluation ---

def _main_story_episode_id(part_id: int, episode_index: int):
    row = master_data.query_one(
        "SELECT id FROM main_story_data WHERE part_id=? AND episode_index=?",
        (part_id, episode_index))
    return row["id"] if row else None


def _main_story_cleared(full_state: dict) -> set:
    from .handlers import stories
    st = (full_state or {}).get(stories.STORY_STATE_KEY) or {}
    return set(st.get("main_cleared") or ())


def _earned(song: dict, full_state: dict, facts: dict) -> bool:
    kind, params = song["kind"], song.get("params") or {}
    if kind in UNTRACKED_KINDS or kind == "unknown":
        return False
    if kind == "default":
        return True

    if kind == "main_story_episode":
        eid = _main_story_episode_id(params.get("chapter"), params.get("episode"))
        return eid is not None and eid in _main_story_cleared(full_state)
    if kind == "main_story_finale":
        # The finale is its own part; its part_id is not the act number, so
        # match on episode_index across the parts flagged as a finale by having
        # no next part. Resolved by episode alone against every cleared id.
        cleared = _main_story_cleared(full_state)
        rows = master_data.query(
            "SELECT id FROM main_story_data WHERE episode_index=?", (params.get("episode"),))
        return any(r["id"] in cleared for r in rows)

    # everything below reads the career facts (app/epithets.build_facts)
    if not facts:
        return False
    if kind == "win_any_race":
        won = set(facts.get("races_won_names") or ())
        return any(r in won for r in params.get("races") or ())
    if kind == "win_grade_at_most":
        floor = _GRADE_VALUE.get(str(params.get("grade", "")).lower(), 0)
        return any(facts.get("wins_grade_" + name)
                   for name, value in _GRADE_VALUE.items() if value >= floor)
    if kind == "ura_finals":
        return bool(facts.get("won_ura_finals"))
    if kind == "unity_cup_finals":
        return bool(facts.get("beat_ura_duo") or facts.get("unity_cup_finals"))
    if kind == "ts_climax":
        return bool(facts.get("ts_climax_all_won"))
    if kind == "grand_concert":
        return bool(facts.get("grand_concert"))
    if kind == "special_grand_concert":
        return bool(facts.get("special_grand_concert"))
    return False


# -------------------------------------------------------------- granting ---

def check_and_grant(full_state: dict, facts: dict | None = None) -> list:
    """Mail every song this account has newly earned. Returns the music_ids
    granted. Mutates full_state (present box + watermark); the caller saves.

    Safe to call often: a song already in the watermark is never re-granted,
    and songs the account already owns in music_list_state are absorbed into
    the watermark on first pass rather than mailed again."""
    from .handlers import presents

    granted = full_state.setdefault(CONCERT_STATE_KEY, [])
    if not granted:
        # First run on an existing account: whatever it already owns counts as
        # granted, so switching this feature on does not spam the mailbox with
        # songs the player has had for weeks.
        owned = {m.get("music_id") for m in (full_state.get("music_list_state") or ())
                 if isinstance(m, dict)}
        granted.extend(sorted(i for i in owned if i))

    new = []
    for song in table():
        mid = song["music_id"]
        if mid in granted:
            continue
        try:
            if not _earned(song, full_state, facts or {}):
                continue
        except Exception:
            log.exception("concert unlock check failed for music %s", mid)
            continue
        presents.send(full_state, _MUSIC_REWARD_TYPE, mid, 1,
                      message="%s unlocked" % (song.get("title") or "New song"))
        granted.append(mid)
        new.append(mid)
    if new:
        log.info("concerts: granted %s", [(m, _title(m)) for m in new])
    return new


def _title(music_id: int):
    row = master_data.query_one(
        "SELECT text FROM text_data WHERE category=16 AND [index]=?", (music_id,))
    return row["text"] if row else None
