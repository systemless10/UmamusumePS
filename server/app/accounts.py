
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import region

log = logging.getLogger("uma-server")

_lock = threading.Lock()

_viewer_locks: dict = {}
_viewer_locks_guard = threading.Lock()


def _db_path() -> Path:
    return region.data_dir() / "accounts.sqlite3"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            viewer_id  TEXT PRIMARY KEY,
            auth_key   TEXT,
            sid        TEXT,
            created_at INTEGER,
            last_seen  INTEGER,
            note       TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "transition_password" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN transition_password TEXT")
    if "pending_deletion_at" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN pending_deletion_at INTEGER")
    if "udid" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN udid TEXT")
    return conn


def viewer_lock(viewer_id) -> threading.RLock:
    key = f"{region.CURRENT_REGION.get()}:{viewer_id}"
    with _viewer_locks_guard:
        lk = _viewer_locks.get(key)
        if lk is None:
            lk = _viewer_locks[key] = threading.RLock()
        return lk


def record_signup(viewer_id, auth_key: str, udid: str | None = None) -> None:
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, auth_key, udid, created_at, last_seen)"
            " VALUES (?,?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " auth_key=excluded.auth_key, last_seen=excluded.last_seen,"
            " udid=COALESCE(excluded.udid, accounts.udid)",
            (str(viewer_id), auth_key, udid, now, now))


def touch(viewer_id, sid: str | None = None) -> None:
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, sid, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " last_seen=excluded.last_seen,"
            " sid=COALESCE(excluded.sid, accounts.sid)",
            (str(viewer_id), sid, now, now))


def get_account(viewer_id) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT viewer_id, auth_key, sid, created_at, last_seen, transition_password,"
            " pending_deletion_at, udid FROM accounts WHERE viewer_id=?",
            (str(viewer_id),)).fetchone()
    if not row:
        return None
    return dict(zip(("viewer_id", "auth_key", "sid", "created_at", "last_seen",
                     "transition_password", "pending_deletion_at", "udid"), row))


def viewer_for_udid(udid: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT viewer_id FROM accounts WHERE udid=? ORDER BY created_at DESC",
            (udid,)).fetchone()
    return row[0] if row else None


DELETION_GRACE_PERIOD_SECONDS = 14 * 86400


def request_deletion(viewer_id) -> int:
    now = int(time.time())
    deletion_at = now + DELETION_GRACE_PERIOD_SECONDS
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, pending_deletion_at, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " pending_deletion_at=excluded.pending_deletion_at,"
            " last_seen=excluded.last_seen",
            (str(viewer_id), deletion_at, now, now))
    return deletion_at


def cancel_deletion(viewer_id) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET pending_deletion_at=NULL WHERE viewer_id=?",
            (str(viewer_id),))


def deletion_days_remaining(viewer_id) -> int | None:
    acct = get_account(viewer_id)
    pending_at = acct.get("pending_deletion_at") if acct else None
    if not pending_at:
        return None
    remaining = pending_at - int(time.time())
    if remaining <= 0:
        return 0
    return -(-remaining // 86400)


def delete_account(viewer_id) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE viewer_id=?", (str(viewer_id),))


_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split(":", 1)
    except ValueError:
        return False
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored)


_RESET_SENTINEL = "<admin-reset:accept-any-password>"


def reset_transition_password(viewer_id) -> None:
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, transition_password, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " transition_password=excluded.transition_password,"
            " last_seen=excluded.last_seen",
            (str(viewer_id), _RESET_SENTINEL, now, now))


def claim_reset_password(viewer_id, password: str) -> None:
    acct = get_account(viewer_id)
    if acct and acct.get("transition_password") == _RESET_SENTINEL:
        set_transition_password(viewer_id, password)


def set_transition_password(viewer_id, password: str) -> None:
    now = int(time.time())
    hashed = _hash_password(password)
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, transition_password, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " transition_password=excluded.transition_password,"
            " last_seen=excluded.last_seen",
            (str(viewer_id), hashed, now, now))


def verify_transition_password(viewer_id, password: str) -> bool:
    acct = get_account(viewer_id)
    stored = acct.get("transition_password") if acct else None
    if not stored or not password:
        return False
    if stored == _RESET_SENTINEL:
        return True
    return _verify_password(password, stored)


_SID_SALTS = {"global": b"co!=Y;(UQCGxJ_n82", "jp": b"r!I@mt8e5i="}


def _expected_wire_sid(stored_sid_string: str) -> bytes:
    return hashlib.md5(stored_sid_string.encode()
                       + _SID_SALTS[region.CURRENT_REGION.get()]).digest()


def check_session(viewer_id, presented_sid: bytes | None, enforce: bool) -> bool:
    if not viewer_id:
        return True
    acct = get_account(viewer_id)
    stored = acct.get("sid") if acct else None
    if not stored or not presented_sid:
        return True
    if presented_sid == _expected_wire_sid(stored):
        return True
    log.warning("sid mismatch for viewer %s (enforce=%s)", viewer_id, enforce)
    return not enforce


def all_accounts() -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT viewer_id, created_at, last_seen FROM accounts"
            " ORDER BY last_seen DESC").fetchall()
    return [dict(zip(("viewer_id", "created_at", "last_seen"), r)) for r in rows]
