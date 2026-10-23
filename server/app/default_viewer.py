
from __future__ import annotations

import sqlite3

from app import region


def _path():
    return region.data_dir() / "default_viewer.txt"


def chosen() -> str:
    try:
        return "".join(_path().read_text().split())
    except OSError:
        return ""


def get() -> str:
    vid = chosen()
    if vid:
        return vid
    for name, sql in (("accounts.sqlite3", "SELECT viewer_id FROM accounts ORDER BY last_seen DESC LIMIT 1"),
                      ("state.sqlite3", "SELECT viewer_id FROM viewer_state_present LIMIT 1")):
        path = region.data_dir() / name
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute(sql).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row:
            return str(row[0])
    return ""


def set(vid: str | None) -> None:
    vid = "".join(str(vid or "").split())
    if vid:
        _path().write_text(vid + "\n")
    else:
        _path().unlink(missing_ok=True)
