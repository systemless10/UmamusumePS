
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from . import region


_lock = Lock()

_LAZY_KEYS = frozenset({
    "load_index", "legacy_roster", "legacy_roster_pre_house",
    "team_stadium_state", "gacha_history", "present_box_state",
    "event_gallery", "directory_hidden_chara",
})

_text_cache: dict[str, dict[str, str]] = {}

_data_versions: dict[str, int] = {}

_conns: dict[str, sqlite3.Connection] = {}


def _db_path() -> Path:
    return region.data_dir() / "state.sqlite3"


def _cache_key(viewer_id) -> str:
    return f"{region.CURRENT_REGION.get()}:{viewer_id}"


@contextmanager
def _connect():
    key = region.CURRENT_REGION.get()
    conn = _conns.get(key)
    if conn is None:
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(conn)
        _conns[key] = conn
    with conn:
        yield conn


def _init_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS viewer_state_present (viewer_id TEXT PRIMARY KEY)"
    )
    _migrate_blob_table(conn)


def _migrate_blob_table(conn: sqlite3.Connection) -> None:
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


def _sync_external_writes(conn: sqlite3.Connection) -> None:
    reg = region.CURRENT_REGION.get()
    version = conn.execute("PRAGMA data_version").fetchone()[0]
    seen = _data_versions.get(reg)
    if seen == version:
        return
    _data_versions[reg] = version
    if seen is not None:
        prefix = f"{reg}:"
        for ck in [ck for ck in _text_cache if ck.startswith(prefix)]:
            del _text_cache[ck]


def _cached_rows(conn: sqlite3.Connection, viewer_id) -> dict[str, str]:
    ck = _cache_key(viewer_id)
    texts = _text_cache.get(ck)
    if texts is None:
        texts = dict(conn.execute(
            "SELECT key, value FROM viewer_state_kv WHERE viewer_id = ?",
            (str(viewer_id),)))
        _text_cache[ck] = texts
    return texts


def clear_cache() -> None:
    with _lock:
        _text_cache.clear()
        _data_versions.clear()


def close() -> None:
    with _lock:
        for conn in _conns.values():
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        _conns.clear()
        _text_cache.clear()
        _data_versions.clear()


class LazyState(dict):

    __slots__ = ("_vid", "_region", "_pending", "_snap", "_dropped")

    def __init__(self, viewer_id, snapshot: dict[str, str]) -> None:
        dict.__init__(self)
        self._vid = viewer_id
        self._region = region.CURRENT_REGION.get()
        self._pending = set(snapshot)
        self._snap = {k: t for k, t in snapshot.items() if k not in _LAZY_KEYS}
        self._dropped = set()

    def _load(self, key) -> None:
        if key in self._pending:
            self._pending.discard(key)
            text = self._snap.pop(key, None)
            if text is not None:
                dict.__setitem__(self, key, json.loads(text))
                return
            token = region.CURRENT_REGION.set(self._region)
            try:
                value = load_lazy_key(self._vid, key)
            finally:
                region.CURRENT_REGION.reset(token)
            if value is not None:
                dict.__setitem__(self, key, value)

    def _load_all(self) -> None:
        for key in list(self._pending):
            self._load(key)

    def __getitem__(self, key):
        self._load(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._load(key)
        return dict.get(self, key, default)

    def setdefault(self, key, default=None):
        self._load(key)
        self._dropped.discard(key)
        return dict.setdefault(self, key, default)

    def __contains__(self, key) -> bool:
        return dict.__contains__(self, key) or key in self._pending

    def __setitem__(self, key, value) -> None:
        self._pending.discard(key)
        self._dropped.discard(key)
        dict.__setitem__(self, key, value)

    def update(self, *args, **kwargs) -> None:
        other = dict(*args, **kwargs)
        self._pending.difference_update(other)
        self._dropped.difference_update(other)
        dict.update(self, other)

    def __delitem__(self, key) -> None:
        self._load(key)
        dict.__delitem__(self, key)
        self._dropped.add(key)

    def pop(self, key, *default):
        self._load(key)
        if dict.__contains__(self, key):
            self._dropped.add(key)
        return dict.pop(self, key, *default)

    def popitem(self):
        self._load_all()
        key, value = dict.popitem(self)
        self._dropped.add(key)
        return key, value

    def clear(self) -> None:
        self._dropped |= set(dict.keys(self)) | self._pending
        self._pending.clear()
        self._snap.clear()
        dict.clear(self)

    def __len__(self) -> int:
        return dict.__len__(self) + len(self._pending)

    def __iter__(self):
        self._load_all()
        return dict.__iter__(self)

    def keys(self):
        self._load_all()
        return dict.keys(self)

    def values(self):
        self._load_all()
        return dict.values(self)

    def items(self):
        self._load_all()
        return dict.items(self)

    def __eq__(self, other):
        self._load_all()
        if isinstance(other, LazyState):
            other._load_all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    __hash__ = None

    def copy(self) -> dict:
        self._load_all()
        return dict(dict.items(self))

    def __reduce_ex__(self, protocol):
        return (dict, (self.copy(),))


def get_state(viewer_id: str) -> dict | None:
    vid = str(viewer_id)
    with _lock, _connect() as conn:
        _sync_external_writes(conn)
        if _cache_key(viewer_id) not in _text_cache:
            present = conn.execute(
                "SELECT 1 FROM viewer_state_present WHERE viewer_id = ?", (vid,)
            ).fetchone()
            if not present:
                return None
        snapshot = dict(_cached_rows(conn, viewer_id))
    return LazyState(viewer_id, snapshot)


def load_lazy_key(viewer_id: str, key: str):
    vid = str(viewer_id)
    with _lock, _connect() as conn:
        _sync_external_writes(conn)
        cached = _text_cache.get(_cache_key(viewer_id))
        if cached is not None:
            text = cached.get(key)
        else:
            row = conn.execute(
                "SELECT value FROM viewer_state_kv WHERE viewer_id = ? AND key = ?",
                (vid, key),
            ).fetchone()
            text = row[0] if row else None
    return None if text is None else json.loads(text)


def all_states_for_key(key: str, viewer_ids=None) -> dict:
    with _lock, _connect() as conn:
        if viewer_ids is None:
            rows = conn.execute(
                "SELECT viewer_id, value FROM viewer_state_kv WHERE key = ?", (key,)
            ).fetchall()
        else:
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
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def save_state(viewer_id: str, state: dict) -> None:
    vid = str(viewer_id)
    texts = {k: json.dumps(v) for k, v in dict.items(state)}
    with _lock:
        with _connect() as conn:
            _sync_external_writes(conn)
            previous = _cached_rows(conn, viewer_id)
            changed = [(vid, k, t) for k, t in texts.items() if previous.get(k) != t]
            gone = _deleted_keys(state, previous, texts)
            if changed:
                conn.executemany(
                    "INSERT INTO viewer_state_kv (viewer_id, key, value) VALUES (?,?,?)"
                    " ON CONFLICT(viewer_id, key) DO UPDATE SET value = excluded.value",
                    changed,
                )
            if gone:
                conn.executemany(
                    "DELETE FROM viewer_state_kv WHERE viewer_id = ? AND key = ?",
                    [(vid, k) for k in gone],
                )
            conn.execute(
                "INSERT OR IGNORE INTO viewer_state_present (viewer_id) VALUES (?)", (vid,)
            )
        previous.update((k, t) for _, k, t in changed)
        for k in gone:
            previous.pop(k, None)
    if isinstance(state, LazyState):
        state._dropped.clear()


def _deleted_keys(state: dict, stored, texts) -> set:
    if isinstance(state, LazyState):
        return {k for k in state._dropped if k not in texts and k in stored}
    return {k for k in stored if k not in texts and k not in _LAZY_KEYS}


def delete_state(viewer_id: str) -> None:
    vid = str(viewer_id)
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM viewer_state_kv WHERE viewer_id = ?", (vid,))
            conn.execute("DELETE FROM viewer_state_present WHERE viewer_id = ?", (vid,))
        _text_cache.pop(_cache_key(viewer_id), None)


def all_viewer_ids() -> list[str]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT viewer_id FROM viewer_state_present").fetchall()
    return [r[0] for r in rows]
