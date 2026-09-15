"""
Persistent per-viewer game state, backed by SQLite.

Storage is one row per TOP-LEVEL KEY of the viewer's state dict
(viewer_state_kv), not one JSON blob per viewer. The public API is unchanged
-- get_state still hands back a plain dict and save_state still takes one --
so no caller had to change; only what hits the disk did.

Why: the original design stored the whole dict as one blob and rewrote all of
it on every save. The largest real viewer is ~3.5 MB, of which ~90% is the
`load_index` cache, and a typical request mutates one small key. Every
mutation therefore rewrote ~3.5 MB (plus WAL) to change a few hundred bytes.
That is tolerable against a local SQLite file the page cache absorbs, and is
exactly the shape that gets expensive against a networked store, where the
same write becomes a full TOAST rewrite plus a round trip. save_state now
re-writes only the keys whose serialized form actually changed (measured on
the largest real viewer: 3,501,313 bytes -> 15).

LAZY KEYS (_LAZY_KEYS) go one step further. Splitting the rows removed the
redundant WRITES but not the redundant PARSING: get_state still had to
json.loads every key and save_state still had to json.dumps every key to
diff it -- ~92 ms per request on the largest viewer, ~90% of that
`load_index`, a blob almost every endpoint carries around and never looks
at. A lazy key is not loaded by get_state at all. It materializes only when
a caller asks for it through its own accessor (load.get_or_seed_blob), which
reads the row and drops the parsed value into the state dict, after which it
saves and diffs exactly like any other key. So the handlers that DO use it
are unchanged and their mutations still persist through save_state, while
the handlers that don't never pay for it.

The one asymmetry this forces: save_state must never DELETE a lazy key just
because it is absent from the dict, since absent means "never loaded" far
more often than it means "removed".

The old single-blob `viewer_state` table is migrated on first connect and
then left in place, untouched, as a rollback point -- nothing reads it after
the migration.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from . import region

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

_lock = Lock()

# Keys get_state does not parse. See the module docstring. Add a key here only
# if every reader of it goes through a dedicated accessor that calls
# load_lazy_key -- otherwise readers silently see it as absent.
_LAZY_KEYS = frozenset({"load_index"})
# Precomputed so get_state can exclude lazy keys in SQL. Filtering them out in
# Python instead still makes SQLite read the value -- 3.1 MB of it on the
# largest account -- which defeats most of the point of the key being lazy.
_LAZY_TUPLE = tuple(_LAZY_KEYS)
_SELECT_EAGER = (
    "SELECT key, value FROM viewer_state_kv WHERE viewer_id = ? AND key NOT IN (%s)"
    % ",".join("?" * len(_LAZY_TUPLE))
)

# Per-viewer snapshot of the exact text last read from (or written to) disk,
# keyed "<region>:<viewer_id>" -> {state_key: value_json}. save_state diffs
# against this to decide which rows to write. It is a pure optimization: a
# miss just means we consider the key dirty and write it, so a cold or evicted
# cache costs a redundant write, never a wrong one.
_text_cache: dict[str, dict[str, str]] = {}
# Bounds the cache. Requests are serialized per viewer (main.py's viewer_lock)
# and a request does one get/save round trip, so only the handful of accounts
# actively in flight need to be resident.
_TEXT_CACHE_MAX_VIEWERS = 16

# One connection per region, created on first use. See _connect.
_conns: dict[str, sqlite3.Connection] = {}


def _db_path() -> Path:
    """Region-suffixed path (state.sqlite3 / state_jp.sqlite3, ...) -- see
    region.py's docstring for why this is the only place that branches."""
    return _DATA_DIR / f"state{region.db_suffix()}.sqlite3"


def _cache_key(viewer_id) -> str:
    return f"{region.CURRENT_REGION.get()}:{viewer_id}"


@contextmanager
def _connect():
    """One long-lived connection per region, reused across requests.

    Every public function here holds the module-level _lock for the whole of
    its database work, so a single shared connection is safe despite
    check_same_thread=False. Reusing it matters: opening a connection, setting
    its pragmas and re-checking the schema cost ~2.5 ms, which dwarfed the
    actual query for the many small accounts (most viewers are a few hundred
    bytes) and made them slower than the original blob design.
    """
    key = region.CURRENT_REGION.get()
    conn = _conns.get(key)
    if conn is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        # Under WAL this can only lose the last transactions on an OS/power
        # crash, never corrupt the database -- the right trade for a game
        # server whose state is re-derivable, and worth roughly an order of
        # magnitude on small writes.
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(conn)
        _conns[key] = conn
    with conn:
        yield conn


def _init_schema(conn: sqlite3.Connection) -> None:
    # WAL lets a reader run concurrently with the writer instead of blocking on
    # it. It persists in the file header, so this once-per-process call is
    # enough -- see _connect for why it is not set per connection.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS viewer_state_kv (
            viewer_id TEXT NOT NULL,
            key       TEXT NOT NULL,
            value     TEXT NOT NULL,
            PRIMARY KEY (viewer_id, key)
        )
        """
    )
    # A viewer whose state is an EMPTY dict has no kv rows at all, which would
    # otherwise be indistinguishable from a viewer that has never been seen --
    # get_state would return None instead of {}, and the account would vanish
    # from all_viewer_ids(). Three such viewers exist in the real data. This
    # table is the explicit "this viewer has state" record that the row count
    # can no longer imply.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS viewer_state_present (viewer_id TEXT PRIMARY KEY)"
    )
    _migrate_blob_table(conn)


def _migrate_blob_table(conn: sqlite3.Connection) -> None:
    """Split any rows still in the legacy single-blob `viewer_state` table into
    per-key rows. Idempotent: a viewer already recorded in viewer_state_present
    is skipped, so a half-finished run resumes cleanly.

    The legacy table is READ ONLY here and deliberately not dropped -- it stays
    as an on-disk rollback point.
    """
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='viewer_state'"
    ).fetchone()
    if not legacy:
        return
    done = {r[0] for r in conn.execute("SELECT viewer_id FROM viewer_state_present")}
    with conn:
        for viewer_id, blob in conn.execute("SELECT viewer_id, blob FROM viewer_state"):
            if viewer_id in done:
                continue
            try:
                state = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if not isinstance(state, dict):
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO viewer_state_kv (viewer_id, key, value)"
                " VALUES (?,?,?)",
                [(viewer_id, k, json.dumps(v)) for k, v in state.items()],
            )
            conn.execute(
                "INSERT OR IGNORE INTO viewer_state_present (viewer_id) VALUES (?)",
                (viewer_id,),
            )


def _remember(viewer_id, texts: dict[str, str]) -> None:
    ck = _cache_key(viewer_id)
    if ck not in _text_cache and len(_text_cache) >= _TEXT_CACHE_MAX_VIEWERS:
        _text_cache.pop(next(iter(_text_cache)), None)
    _text_cache[ck] = texts


def get_state(viewer_id: str) -> dict | None:
    """The viewer's state as a plain dict, reassembled from its per-key rows.
    Keys in _LAZY_KEYS are omitted -- fetch those via load_lazy_key. None (not
    {}) when the viewer has no persisted state at all, which is the distinction
    callers' `or {}` idiom relies on."""
    vid = str(viewer_id)
    with _lock, _connect() as conn:
        present = conn.execute(
            "SELECT 1 FROM viewer_state_present WHERE viewer_id = ?", (vid,)
        ).fetchone()
        if not present:
            return None
        texts = dict(conn.execute(_SELECT_EAGER, (vid, *_LAZY_TUPLE)).fetchall())
        _remember(viewer_id, texts)
    return {k: json.loads(v) for k, v in texts.items()}


def load_lazy_key(viewer_id: str, key: str):
    """Materialize one lazy key on demand. Returns None when the viewer has no
    stored value for it. The raw text is folded into the diff cache so that if
    the caller puts the value into the state dict, a later save_state can tell
    whether it was actually mutated."""
    vid = str(viewer_id)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT value FROM viewer_state_kv WHERE viewer_id = ? AND key = ?",
            (vid, key),
        ).fetchone()
        if row is None:
            return None
        cached = _text_cache.get(_cache_key(viewer_id))
        if cached is not None:
            cached[key] = row[0]
    return json.loads(row[0])


def all_states_for_key(key: str, viewer_ids=None) -> dict:
    """{viewer_id: value} for ONE state key across every viewer, in a single
    query.

    The per-key row layout is what makes this cheap, and it is the right shape
    for any feature that needs one field from every account (team_stadium
    matchmaking scanning for opponents, say). Doing it with get_state per
    viewer means parsing all 200 accounts' entire state to reach one field:
    measured at 180 ms, against ~25 ms here for an identical result.

    Naming the key explicitly means lazy keys are readable through this too --
    there is no ambiguity about what is being loaded. Pass viewer_ids to
    restrict the scan to specific accounts.
    """
    with _lock, _connect() as conn:
        if viewer_ids is None:
            rows = conn.execute(
                "SELECT viewer_id, value FROM viewer_state_kv WHERE key = ?", (key,)
            ).fetchall()
        else:
            # Chunked to stay under SQLite's bound-variable limit (999 by
            # default on older builds) no matter how many accounts are asked
            # for at once.
            ids = [str(v) for v in viewer_ids]
            rows = []
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                rows += conn.execute(
                    "SELECT viewer_id, value FROM viewer_state_kv WHERE key = ?"
                    " AND viewer_id IN (%s)" % ",".join("?" * len(chunk)),
                    (key, *chunk),
                ).fetchall()
    return {vid: json.loads(v) for vid, v in rows}


def extract_json_path(viewer_id: str, key: str, path: str):
    """One value from deep inside a stored key, extracted by SQLite rather than
    parsed in Python.

    For a caller that needs a single field out of a large value -- jukebox's
    request-id fallback reads one small object out of the 3.1 MB load_index
    blob -- parsing the whole thing to reach it costs ~50 ms and builds
    hundreds of thousands of throwaway Python objects. json_extract does the
    walk inside SQLite and returns just that subtree: ~13 ms, same value.

    `path` is a SQLite JSON path, e.g. "$.data.music_list". Returns None when
    the key or the path is absent -- callers that need seed-if-missing
    behaviour must fall back to their own accessor, since this deliberately
    never writes.
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT json_extract(value, ?) FROM viewer_state_kv"
            " WHERE viewer_id = ? AND key = ?",
            (path, str(viewer_id), key),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    raw = row[0]
    if not isinstance(raw, str):
        return raw          # json_extract returns scalars natively typed
    try:
        return json.loads(raw)
    except ValueError:
        return raw          # a bare JSON string scalar


def save_state(viewer_id: str, state: dict) -> None:
    """Persist only what changed. Every top-level value present in the dict is
    re-serialized (the only way to know whether a handler mutated it in place),
    but a key whose text matches what is already stored is not written, and
    keys dropped from the dict are deleted -- except lazy keys, whose absence
    means "not loaded this request", never "deleted"."""
    vid = str(viewer_id)
    texts = {k: json.dumps(v) for k, v in state.items()}
    with _lock, _connect() as conn:
        previous = _text_cache.get(_cache_key(viewer_id))
        if previous is None:
            previous = {
                k: v
                for k, v in conn.execute(
                    "SELECT key, value FROM viewer_state_kv WHERE viewer_id = ?", (vid,)
                )
            }
        changed = [(vid, k, t) for k, t in texts.items() if previous.get(k) != t]
        removed = [(vid, k) for k in previous
                   if k not in texts and k not in _LAZY_KEYS]
        if changed:
            conn.executemany(
                "INSERT INTO viewer_state_kv (viewer_id, key, value) VALUES (?,?,?)"
                " ON CONFLICT(viewer_id, key) DO UPDATE SET value = excluded.value",
                changed,
            )
        if removed:
            conn.executemany(
                "DELETE FROM viewer_state_kv WHERE viewer_id = ? AND key = ?", removed
            )
        conn.execute(
            "INSERT OR IGNORE INTO viewer_state_present (viewer_id) VALUES (?)", (vid,)
        )
        # Carry forward the text of any lazy key this request materialized but
        # did not hand back in `state`, so a later save in the same request
        # still diffs it instead of blindly rewriting it.
        kept = {k: v for k, v in previous.items()
                if k in _LAZY_KEYS and k not in texts}
        kept.update(texts)
        _remember(viewer_id, kept)


def delete_state(viewer_id: str) -> None:
    vid = str(viewer_id)
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM viewer_state_kv WHERE viewer_id = ?", (vid,))
        conn.execute("DELETE FROM viewer_state_present WHERE viewer_id = ?", (vid,))
        _text_cache.pop(_cache_key(viewer_id), None)


def all_viewer_ids() -> list[str]:
    """Every viewer_id with persisted state -- for features that need to look
    across accounts on this server (e.g. team_stadium opponent matchmaking
    against other real accounts' submitted rosters, not fabricated players)."""
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT viewer_id FROM viewer_state_present").fetchall()
    return [r[0] for r in rows]
