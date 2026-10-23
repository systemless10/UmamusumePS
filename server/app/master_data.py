
from __future__ import annotations

import functools
import logging
import os
import sqlite3
from pathlib import Path


def context_cache(maxsize=128):
    def decorate(fn):
        @functools.lru_cache(maxsize=maxsize)
        def cached(revision, current_region, args, kwargs):
            return fn(*args, **dict(kwargs))
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            from . import custom_support
            return cached(custom_support.CURRENT_PACK.get(), region.CURRENT_REGION.get(),
                          args, tuple(sorted(kwargs.items())))
        wrapped.cache_clear = cached.cache_clear
        wrapped.cache_info = cached.cache_info
        return wrapped
    return decorate

from . import region

log = logging.getLogger("uma-server")

_GAME_DIRS = {
    "global": ("UmamusumePrettyDerby", "UmamusumePrettyDerby_Data"),
    "jp": ("UmamusumePrettyDerby_Jpn", "UmamusumePrettyDerby_Jpn_Data"),
}
_INSTALL, _DATA = _GAME_DIRS[region.PROCESS_REGION]
_RELATIVE_MDB = f"{_DATA}/Persistent/master/master.mdb"

_CANDIDATES = [
    p for p in [
        os.environ.get("MASTER_MDB_PATH"),
        rf"C:\Program Files (x86)\Steam\steamapps\common\{_INSTALL}\\"
        + _RELATIVE_MDB.replace("/", "\\"),
        os.path.expanduser(
            f"~/.local/share/Steam/steamapps/common/{_INSTALL}/" + _RELATIVE_MDB
        ),
    ]
    if p
]


def _resolve_mdb_path() -> Path:
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return Path(_CANDIDATES[0])


MASTER_MDB_PATH = _resolve_mdb_path()

_EN_TEXT_CANDIDATES = [
    p for p in [
        os.environ.get("UMA_EN_TEXT_MDB"),
        r"C:\Program Files (x86)\Steam\steamapps\common\UmamusumePrettyDerby"
        r"\UmamusumePrettyDerby_Data\Persistent\master\master.mdb",
        os.path.expanduser("~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby/"
                           "UmamusumePrettyDerby_Data/Persistent/master/master.mdb"),
    ]
    if p
]

_conn: sqlite3.Connection | None = None


def _overlay_english_text(conn: sqlite3.Connection) -> None:
    source = next((Path(p) for p in _EN_TEXT_CANDIDATES if Path(p).is_file()), None)
    if source is None:
        log.warning("master_data: no Global master.mdb for English text (set UMA_EN_TEXT_MDB) "
                    "-- events, epithets and race stories will not resolve on JP")
        return
    conn.execute("ATTACH DATABASE ? AS en", (f"file:{source.as_posix()}?mode=ro",))
    conn.execute(
        "CREATE TEMP VIEW text_data AS "
        "SELECT j.id, j.category, j.[index], COALESCE(NULLIF(e.text, ''), j.text) AS text "
        "FROM main.text_data j LEFT JOIN en.text_data e "
        "ON e.category = j.category AND e.[index] = j.[index]")


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        uri = f"file:{MASTER_MDB_PATH.as_posix()}?mode=ro"
        _conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        if region.PROCESS_REGION == "jp":
            _overlay_english_text(_conn)
    return _conn


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    from . import custom_support
    return (custom_support.connection() or get_connection()).execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    from . import custom_support
    return (custom_support.connection() or get_connection()).execute(sql, params).fetchone()


@functools.lru_cache(maxsize=None)
def has_table(name: str) -> bool:
    return query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)) is not None


def has_column(table: str, column: str) -> bool:
    return any(r["name"] == column for r in query(f"PRAGMA table_info('{table}')"))


@context_cache(maxsize=1024)
def support_card_chara(support_card_id: int) -> int:
    row = query_one(
        "SELECT chara_id FROM support_card_data WHERE id=?", (int(support_card_id),))
    return int(row["chara_id"]) if row else 0


TRAINEE_CHARA_MIN, TRAINEE_CHARA_MAX = 1000, 1999


def is_trainee_chara(chara_id) -> bool:
    return TRAINEE_CHARA_MIN <= int(chara_id or 0) <= TRAINEE_CHARA_MAX


@functools.lru_cache(maxsize=1)
def drawable_charas() -> frozenset:
    return frozenset(int(r["chara_id"]) for r in
                     query("SELECT DISTINCT chara_id FROM card_data"))


def has_portrait_art(chara_id) -> bool:
    return int(chara_id or 0) in drawable_charas()


@functools.lru_cache(maxsize=4096)
def skill_tip_key(skill_id) -> tuple[int, int]:
    sid = int(skill_id)
    row = query_one("SELECT group_id, rarity FROM skill_data WHERE id=?", (sid,))
    if row is None:
        return sid // 10, 1
    return int(row["group_id"]), int(row["rarity"] or 1)
