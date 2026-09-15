"""
Read-only access to the game's master.mdb (plain SQLite3, no encryption --
see README). Opened once, read-only, connection reused across requests.
"""

from __future__ import annotations

import os
import functools
import sqlite3
from pathlib import Path

_RELATIVE_MDB = "UmamusumePrettyDerby_Data/Persistent/master/master.mdb"

# Candidates checked in order; first one that exists on disk wins. Covers a
# Windows Steam install, a native Linux Steam install, and an env var
# override for anything else (a second Steam library, a Proton prefix, ...).
_CANDIDATES = [
    p for p in [
        os.environ.get("MASTER_MDB_PATH"),
        r"C:\Program Files (x86)\Steam\steamapps\common\UmamusumePrettyDerby\\"
        + _RELATIVE_MDB.replace("/", "\\"),
        os.path.expanduser(
            "~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby/" + _RELATIVE_MDB
        ),
    ]
    if p
]


def _resolve_mdb_path() -> Path:
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    # Nothing found: fall back to the first candidate so the resulting error
    # (file not found) points at a real, informative path.
    return Path(_CANDIDATES[0])


MASTER_MDB_PATH = _resolve_mdb_path()

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # uri=True + mode=ro: fail fast on a locked/missing file rather than
        # silently creating an empty db at the wrong path.
        uri = f"file:{MASTER_MDB_PATH.as_posix()}?mode=ro"
        _conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_connection().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_connection().execute(sql, params).fetchone()


# ------------------------------------------------------- character lookups --
# Generic master.mdb reads that several unrelated features need. They lived in
# the Grand Live module, so shared career code had to import a SCENARIO to ask
# a plain master-data question -- one of the couplings that made Grand Live look
# load-bearing when it was not.

@functools.lru_cache(maxsize=1024)
def support_card_chara(support_card_id: int) -> int:
    """The CHARACTER a support card belongs to, from support_card_data.

    NOT `support_card_id // 100`. That works for a trainee card (101303 -> 1013)
    and is nonsense for a support card: 30028 // 100 = 300, which is not a
    character at all. Feeding those bogus ids to the client made it look up
    characters that do not exist, and the resulting null crashed the Live screen
    outright -- Player.log:

        NullReferenceException
          at Gallop.SingleModeScenarioLiveTopChara..ctor(bool isGrandlive,
                                                         List prevCharaInfos)
          at Gallop.SingleModeScenarioLiveTopController.InitializeView()
    """
    row = query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (int(support_card_id),))
    return int(row["chara_id"]) if row else 0


# Trainee characters live in the 1000-1999 band; the 9xxx band is NPCs (Tazuna
# 9001, the Director 9002, Light Hello 9008, ...).
TRAINEE_CHARA_MIN, TRAINEE_CHARA_MAX = 1000, 1999


def is_trainee_chara(chara_id) -> bool:
    """Whether this chara_id is a playable trainee rather than a scenario NPC."""
    return TRAINEE_CHARA_MIN <= int(chara_id or 0) <= TRAINEE_CHARA_MAX


# ------------------------------------------------- portrait art this build has
# master.mdb ships EVERY character, including ones this (Global) build has no
# art for: those rows carry the not-released sentinel
# chara_data.start_date = 2524608000 (2050-01-01) and, exactly and only those,
# have no card_data row either -- the two agree on all 96 rows (66 released, 30
# not). A screen that draws such a chara gets a BLANK WHITE PLANE: no error, no
# placeholder, no Player.log line. The opponent panel registers a texture
# download per runner
# (PartsSingleModeScenarioTeamRaceOpponent.RegisterDownload/SetupCharaImage)
# and simply draws nothing when the path does not resolve.
#
# User-reported 2026-09-07: Unity Cup opponent teams had white gaps where Twin
# Turbo, Sirius Symboli, Narita Top Road and the like should have been -- we
# were picking named opponents from the whole single_mode_npc table, 18 of
# whose 84 characters this build cannot draw. A MOB is always renderable, so
# an undrawable named runner should become one rather than a hole.
#
# card_data is the test rather than start_date because it is what the UI
# actually needs (a chara icon is a card icon). Scenario NPCs with no card at
# all -- Happy Meek (2001), Bitter Glasse (2002), Little Cocon (2003) -- are
# therefore "undrawable" here too, which is right for the two Unity Cup girls
# and conservative for Meek, whose art DOES ship with URA; her own builder
# (single_mode_team._happy_meek_opponent) does not consult this.

@functools.lru_cache(maxsize=1)
def drawable_charas() -> frozenset:
    """The characters whose portrait this client build can actually draw."""
    return frozenset(int(r["chara_id"]) for r in
                     query("SELECT DISTINCT chara_id FROM card_data"))


def has_portrait_art(chara_id) -> bool:
    """Whether the client can draw this character at all -- see above. False
    means: ship her as a mob, or do not ship her."""
    return int(chara_id or 0) in drawable_charas()


# ------------------------------------------------------------ skill lookups --

@functools.lru_cache(maxsize=4096)
def skill_tip_key(skill_id) -> tuple[int, int]:
    """(group_id, rarity) for a skill -- the key chara_info.skill_tips_array
    entries are identified by on the wire.

    A gold skill and its white group-mate share a group_id (200581 "Speed Star"
    rarity=2 and 200582 "Prepared to Pass" rarity=1 both group to 20058), so the
    group ALONE does not say which of the pair a hint is for; the client picks
    by (group_id, rarity). Callers that filled in a fixed rarity=1 therefore
    turned every gold hint into its white group-mate -- e.g. the Fine Motion SSR
    (30010) chain finale hints Speed Star and always rendered/granted Prepared
    to Pass instead. Always take the rarity from the skill's own row rather than
    from whichever "slot" a caller thinks it fills.

    (rarity=1 within a group is not unique -- the ○/× debuff pairs collide, e.g.
    group 20033's "Corner Adept ○" 200332 and "Corner Adept ×" 200333 -- but
    that ambiguity is the game's own wire shape, not something to work around.)

    Unknown skill ids fall back to the id//10 group the whole DB follows and
    rarity 1, so a missing master row degrades instead of raising."""
    sid = int(skill_id)
    row = query_one("SELECT group_id, rarity FROM skill_data WHERE id=?", (sid,))
    if row is None:
        return sid // 10, 1
    return int(row["group_id"]), int(row["rarity"] or 1)
