"""Account records + per-viewer serialization.

The state blob (state.py) was always per-viewer; what was missing for
multi-account is (1) somewhere to PERSIST what tool/signup mints -- it used to
hand out viewer_id + auth_key and forget them -- (2) a session record, and
(3) a per-viewer lock so two in-flight requests for one account can't
read-modify-write over each other (state.get/save lock separately, so the
handler body was a race window).

Enforcement is controlled by the `auth_enforce` config knob (client_config.json).
Originally left off deliberately (user: "for now just skip" the gate) while only
the plumbing mattered. Turned ON 2026-08-18 once account linking (a real
"log back into my account with a password" feature) made sid validation
actually matter -- but ONLY after fixing a real bug that would have made
turning it on break nearly every request: main.py used to only update the
stored sid at tool/start_session, never on the calls after, so the stored
value was frozen while the sid we actually SENT kept rolling forward every
response -- a guaranteed mismatch on every single non-start_session call,
regardless of client behavior. See main.py's dispatch() for the fix.
"""

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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_lock = threading.Lock()

_viewer_locks: dict = {}
_viewer_locks_guard = threading.Lock()


def _db_path() -> Path:
    return _DATA_DIR / f"accounts{region.db_suffix()}.sqlite3"


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
    # transition_password: the real game's "data link" system (real capture
    # 2026-08-18, captures/20260818_074808 + .../20260818_081331) -- a
    # player calls account/publish_transition_code with a password of their
    # choice, then account/get_by_transition_code (preview) or .../chain_by_
    # transition_code (commit) FROM ANY OTHER ACCOUNT, giving {password,
    # input_viewer_id} where input_viewer_id is that FIRST account's own
    # viewer_id (the "code" IS the trainer id -- no separate code is ever
    # generated or returned). Migration-safe add for accounts.sqlite3 files
    # that predate this column.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "transition_password" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN transition_password TEXT")
    # pending_deletion_at: unix timestamp the account/deletion_request grace
    # period ends at, NULL when no deletion is pending. Migration-safe add,
    # same pattern as transition_password above.
    if "pending_deletion_at" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN pending_deletion_at INTEGER")
    return conn


def viewer_lock(viewer_id) -> threading.RLock:
    """One re-entrant lock per viewer. Serializes a viewer's requests through
    the dispatcher so read-modify-write handler bodies stop racing each other;
    requests for DIFFERENT viewers stay fully concurrent. Keyed with the
    region too, so a JP and a Global account that happen to share a numeric
    viewer_id never contend on (or otherwise interact through) the same lock."""
    key = f"{region.CURRENT_REGION.get()}:{viewer_id}"
    with _viewer_locks_guard:
        lk = _viewer_locks.get(key)
        if lk is None:
            lk = _viewer_locks[key] = threading.RLock()
        return lk


def record_signup(viewer_id, auth_key: str) -> None:
    """Persist what tool/signup minted. Before this, signup handed out
    credentials and forgot them -- there was nothing to validate against."""
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, auth_key, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " auth_key=excluded.auth_key, last_seen=excluded.last_seen",
            (str(viewer_id), auth_key, now, now))


def touch(viewer_id, sid: str | None = None) -> None:
    """Upsert last_seen (and the active sid). Existing accounts that predate
    this table are grandfathered in on first sight."""
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
            " pending_deletion_at FROM accounts WHERE viewer_id=?", (str(viewer_id),)).fetchone()
    if not row:
        return None
    return dict(zip(("viewer_id", "auth_key", "sid", "created_at", "last_seen",
                     "transition_password", "pending_deletion_at"), row))


# account/deletion_request grace period. No master.mdb table configures this
# (checked -- no config/flag table names anything like it), so this is a
# clearly-documented best-effort default (common industry norm for this kind
# of feature) rather than an unconfirmed guess dressed up as fact. Adjust
# freely if a real capture ever pins the actual number.
DELETION_GRACE_PERIOD_SECONDS = 14 * 86400


def request_deletion(viewer_id) -> int:
    """account/deletion_request: start the grace-period countdown. Returns the
    unix timestamp deletion becomes final at. Upserts a bare row too, same as
    the transition_password setters above."""
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
    """account/deletion_cancel: clear a pending deletion. No-op (not an
    error) if none was pending -- matches the real game accepting a cancel
    tap even if the countdown already lapsed client-side."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET pending_deletion_at=NULL WHERE viewer_id=?",
            (str(viewer_id),))


def deletion_days_remaining(viewer_id) -> int | None:
    """None = no deletion pending. Ceiling-rounds so 'the last few hours'
    still reads as 1 day remaining rather than 0 (0 would read as 'already
    deleted' to a player)."""
    acct = get_account(viewer_id)
    pending_at = acct.get("pending_deletion_at") if acct else None
    if not pending_at:
        return None
    remaining = pending_at - int(time.time())
    if remaining <= 0:
        return 0
    return -(-remaining // 86400)   # ceiling division


def delete_account(viewer_id) -> None:
    """The actual wipe once the grace period has elapsed (checked lazily at
    tool/start_session -- see main.py). Removes the account row outright;
    the caller is responsible for also clearing state.py's viewer_state row
    (state.delete_state) since that's a separate store this module doesn't
    own."""
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE viewer_id=?", (str(viewer_id),))


_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    """salt:hash, both hex -- PBKDF2-HMAC-SHA256. A fresh random salt per
    password (not reused across accounts), so two accounts publishing the
    same password never produce the same stored value."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split(":", 1)
    except ValueError:
        return False   # not our format (e.g. a pre-hashing plaintext value)
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    # Constant-time: a password check is exactly the kind of comparison a
    # timing side-channel targets.
    return hmac.compare_digest(candidate, stored)


# Admin "reset the password" escape hatch: stored in the transition_password
# column but deliberately NOT a "salt:hash" value, so _verify_password's own
# format check (split(":", 1)) would already refuse it if it were ever fed in
# there directly -- verify_transition_password below special-cases it BEFORE
# reaching _verify_password instead. While an account's transition_password
# equals this sentinel, account/get_by_transition_code and .../chain_by_
# transition_code accept ANY non-empty password for it (the real account/
# publish_transition_code flow requires knowing the CURRENT password to set a
# new one, which is exactly what an admin doing this for a locked-out player
# doesn't have). claim_reset_password below closes the window automatically:
# the first successful chain_by (the commit step, not the non-committing
# preview) persists whatever password was actually typed as the new real one.
_RESET_SENTINEL = "<admin-reset:accept-any-password>"


def reset_transition_password(viewer_id) -> None:
    """Admin action: put this account into the 'accept any password' state
    for account_link's transition-code login, for a player who forgot theirs
    (or never set one). Upserts a bare row too, same as set_transition_password."""
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (viewer_id, transition_password, created_at, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(viewer_id) DO UPDATE SET"
            " transition_password=excluded.transition_password,"
            " last_seen=excluded.last_seen",
            (str(viewer_id), _RESET_SENTINEL, now, now))


def claim_reset_password(viewer_id, password: str) -> None:
    """Call after a successful transition-code COMMIT (chain_by_transition_
    code, never the non-committing preview) while the account was in the
    admin-reset state: persists whatever password was actually used as the
    new real one, closing the 'any password works' window so only that
    password verifies from here on -- an ordinary password, not a special
    reset state, exactly as if the player had just published it themselves."""
    acct = get_account(viewer_id)
    if acct and acct.get("transition_password") == _RESET_SENTINEL:
        set_transition_password(viewer_id, password)


def set_transition_password(viewer_id, password: str) -> None:
    """account/publish_transition_code: the caller sets/replaces the
    password guarding their own account. Upserts a bare account row too --
    an account that predates record_signup (grandfathered via touch()) can
    still publish one. Stored hashed (see _hash_password) -- never plaintext."""
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
    """account/get_by_transition_code and .../chain_by_transition_code both
    gate on this: the target account must exist, have PUBLISHED a password
    (None = never published = nothing to link to), and it must match --
    UNLESS an admin put it in the reset state (_RESET_SENTINEL), in which
    case any non-empty password verifies (see reset_transition_password /
    claim_reset_password)."""
    acct = get_account(viewer_id)
    stored = acct.get("transition_password") if acct else None
    if not stored or not password:
        return False
    if stored == _RESET_SENTINEL:
        return True
    return _verify_password(password, stored)


# The client NEVER re-sends our data_headers.sid string verbatim. Per the
# reference client (uma_api/client.py): on every response it computes
# self.sid = next_sid(dh["sid"]) = sm5(dh["sid"].encode()) = MD5(that
# string + SALT), and THAT 16-byte digest -- not the string -- is what
# rides in the wire header's fixed 16-byte sid field on its NEXT request
# (crypto.py's decoded.sid). Comparing decoded.sid against our stored
# string directly (what this used to do) could never match regardless of
# any bookkeeping fix -- different lengths, one raw bytes, one hex text.
# This replicates the client's own transform so the comparison is apples
# to apples.
_SID_SALT = b"co!=Y;(UQCGxJ_n82"


def _expected_wire_sid(stored_sid_string: str) -> bytes:
    return hashlib.md5(stored_sid_string.encode() + _SID_SALT).digest()


def check_session(viewer_id, presented_sid: bytes | None, enforce: bool) -> bool:
    """Compare the wire-header sid the client presented (raw 16 bytes)
    against MD5(our last-issued data_headers.sid string + SALT) -- the
    value the reference client's own next_sid() would have produced from
    it. Soft by default: a mismatch is logged, and only rejected when the
    `auth_enforce` knob is on. start_session itself always passes (that
    call is how a client legitimately rotates its sid, and its FIRST-ever
    call has no prior data_headers.sid to check against at all)."""
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
