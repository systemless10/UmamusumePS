
from __future__ import annotations

import json
import sqlite3
import time

from app import region
from app import state as S

KEEP_ROWS = 2000

_orig_save = S.save_state
_orig_delete = S.delete_state
_pending: dict[str, dict] = {}
_new_viewers: set[str] = set()
_reader: sqlite3.Connection | None = None
_hist: sqlite3.Connection | None = None


def _state_db() -> sqlite3.Connection:
    global _reader
    if _reader is None:
        S.get_state("__admin_probe__")
        _reader = sqlite3.connect(region.data_dir() / "state.sqlite3", check_same_thread=False)
    return _reader


def _db() -> sqlite3.Connection:
    global _hist
    if _hist is None:
        _hist = sqlite3.connect(region.data_dir() / "admin_history.sqlite3", check_same_thread=False)
        _hist.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, viewer_id TEXT NOT NULL,
            label TEXT NOT NULL, keys TEXT NOT NULL, old TEXT NOT NULL, new_viewer INTEGER NOT NULL DEFAULT 0,
            undone_by INTEGER)""")
        _hist.execute("CREATE INDEX IF NOT EXISTS history_viewer ON history(viewer_id, id)")
        _hist.commit()
    return _hist


def _old_text(conn, vid, key):
    row = conn.execute("SELECT value FROM viewer_state_kv WHERE viewer_id=? AND key=?", (vid, key)).fetchone()
    return row[0] if row else None


def _tracked_save(viewer_id, state):
    vid = str(viewer_id)
    conn = _state_db()
    rec = _pending.setdefault(vid, {})
    present = conn.execute("SELECT 1 FROM viewer_state_present WHERE viewer_id=?", (vid,)).fetchone()
    if not present and vid not in rec:
        _new_viewers.add(vid)
    texts = {k: json.dumps(v) for k, v in dict.items(state)}
    for k, t in texts.items():
        if k in rec:
            continue
        old = _old_text(conn, vid, k)
        if old != t:
            rec[k] = old
    stored = {k for (k,) in conn.execute("SELECT key FROM viewer_state_kv WHERE viewer_id=?", (vid,))}
    gone = S._deleted_keys(state, stored, texts)
    for k in gone:
        if k not in rec:
            rec[k] = _old_text(conn, vid, k)
    return _orig_save(viewer_id, state)


def _tracked_delete(viewer_id):
    vid = str(viewer_id)
    rec = _pending.setdefault(vid, {})
    for k, v in _state_db().execute("SELECT key, value FROM viewer_state_kv WHERE viewer_id=?", (vid,)):
        rec.setdefault(k, v)
    return _orig_delete(viewer_id)


def install() -> None:
    S.save_state = _tracked_save
    S.delete_state = _tracked_delete


def begin() -> None:
    _pending.clear()
    _new_viewers.clear()
    S.clear_cache()


def commit(label: str) -> list[int]:
    ids = []
    db = _db()
    for vid, rec in _pending.items():
        rec = {k: v for k, v in rec.items() if not (v is None and _old_text(_state_db(), vid, k) is None)}
        if not rec:
            continue
        cur = db.execute(
            "INSERT INTO history (ts, viewer_id, label, keys, old, new_viewer) VALUES (?,?,?,?,?,?)",
            (time.time(), vid, label, json.dumps(sorted(rec)), json.dumps(rec), int(vid in _new_viewers)))
        ids.append(cur.lastrowid)
    if ids:
        db.execute("DELETE FROM history WHERE id <= (SELECT MAX(id) FROM history) - ?", (KEEP_ROWS,))
        db.commit()
    begin()
    return ids


def rows(viewer: str | None, limit: int = 200) -> list[dict]:
    q = "SELECT id, ts, viewer_id, label, keys, new_viewer, undone_by, length(old) FROM history"
    params: tuple = ()
    if viewer:
        q += " WHERE viewer_id=?"
        params = (viewer,)
    q += " ORDER BY id DESC LIMIT ?"
    out = []
    for r in _db().execute(q, (*params, limit)):
        out.append({"id": r[0], "ts": r[1], "viewer": r[2], "label": r[3], "keys": json.loads(r[4]),
                    "new_viewer": bool(r[5]), "undone_by": r[6], "size": r[7]})
    return out


def entry(entry_id: int) -> dict | None:
    r = _db().execute("SELECT id, viewer_id, label, keys, old, new_viewer FROM history WHERE id=?",
                      (entry_id,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "viewer": r[1], "label": r[2], "keys": json.loads(r[3]),
            "old": json.loads(r[4]), "new_viewer": bool(r[5])}


def later_overlaps(entry_id: int) -> list[dict]:
    e = entry(entry_id)
    if not e:
        return []
    keys = set(e["keys"])
    out = []
    for r in _db().execute("SELECT id, label, keys FROM history WHERE viewer_id=? AND id>? ORDER BY id",
                           (e["viewer"], entry_id)):
        shared = keys & set(json.loads(r[2]))
        if shared:
            out.append({"id": r[0], "label": r[1], "keys": sorted(shared)})
    return out


def mark_undone(entry_id: int, by: int) -> None:
    _db().execute("UPDATE history SET undone_by=? WHERE id=?", (by, entry_id))
    _db().commit()
