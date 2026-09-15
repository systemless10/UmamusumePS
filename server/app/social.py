"""Serverwide social graph: the follow/friend edges and the Circle (guild)
system, shared across every account on this server.

WHY THIS IS NOT PER-VIEWER STATE
--------------------------------
Everything else in this project lives in state.py, keyed by viewer_id, and
main.py serializes requests per viewer (accounts.viewer_lock). That model is
exactly wrong for a social graph, because every interesting write here
touches TWO accounts at once:

  * A follows B  -> B's follower list changed, and B is not the viewer whose
    lock we hold.
  * A joins C's circle -> the circle's member list changed, and it is shared
    by up to 30 accounts, none of whose locks we hold.

Doing that through state.py would mean a handler running under A's lock
read-modify-writing B's state blob with no lock on B at all -- a genuine
lost-update race the moment two accounts interact with the same third party
(two players joining one circle, or A following B while B follows A). So the
shared graph gets its own store, its own lock, and its own tables, and the
per-viewer blobs stay strictly per-viewer. A viewer's own state never holds
an edge; it is always derived from here.

The store is relational rather than a JSON blob for the same reason: the
questions asked of it ("who follows me", "who is in this circle", "which
circles match this keyword") are queries across accounts, not lookups by
account, and every one of them would otherwise be a full scan of all 200
viewers' state.

THE FOLLOW MODEL (capture-proven, not inferred)
-----------------------------------------------
Followers and friends are the same table -- one directed edge, follower ->
followee -- and `state` is a VIEW of the two possible directions between a
pair, computed per request from whichever account is asking:

  state 1  I follow them          follow_time set, follower_time 0000-00-00
  state 2  they follow me         follower_time set, follow_time 0000-00-00
  state 3  mutual ("friend")      both set
  state 0  no edge either way     both empty strings (recommend_list only)

Confirmed against captures/20260907_204921/0023_friend_index.json (real
server, real account): friend_list carried 69 state-1, 17 state-2 and 1
state-3 entries, with exactly the zero/non-zero time pattern above, and
follower_num was 18 -- i.e. the state-2 count plus the state-3 count, which
is what makes "follower" and "mutual friend" the same edge seen from two
sides rather than two separate lists. friend_list is the UNION of both
directions, NOT just the people I follow.

`follow_time` survives an unfollow: captures/20260907_204921/0026_friend_un_
follow.json returns state 0 with the ORIGINAL 2025-11-20 follow_time, not a
cleared one. So an unfollow deactivates the edge (active=0) instead of
deleting it, and re-following the same account restores that first timestamp.

THE CIRCLE MODEL
----------------
Shapes come from captures/20260818_122351/0034_circle_room_enter.json (a
real 30-member circle on the live server):

  circle_info  {circle_id, leader_viewer_id, name, comment, member_num,
                join_style, policy, make_time}
  circle_user  {viewer_id, circle_id, membership, join_time,
                penalty_end_time, item_request_end_time, last_check_post_id,
                ranking_result_check_time}

membership took values 1, 2 and 3 in that capture, on a circle with one
leader and (per the client's own UI) sub-leaders -- MEMBERSHIP_LEADER=3,
MEMBERSHIP_SUB_LEADER=2, MEMBERSHIP_MEMBER=1 is the reading, with the leader
independently identified by circle_info.leader_viewer_id, which is the field
we actually trust and keep authoritative. Membership is stored so the client
renders the right badges; leadership decisions are made from
leader_viewer_id.

circle_id is INT32-SHAPED in every real example (301102622, 379955687,
274238899 -- all under 2^31), never viewer_id-shaped. single_mode_team.py
documents this the hard way: reusing the 12-digit viewer_id scheme for a
circle_id overflowed the client's narrower integer type and produced "Could
not receive parameters from server". _new_circle_id stays inside int32.
"""

from __future__ import annotations

import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from . import region

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_lock = Lock()
_conns: dict[str, sqlite3.Connection] = {}

ZERO_TIME = "0000-00-00 00:00:00"

# friend_data.state, as served to the asking viewer. See the module docstring.
STATE_NONE = 0
STATE_FOLLOW = 1
STATE_FOLLOWER = 2
STATE_MUTUAL = 3

# circle_user.membership. leader_viewer_id stays authoritative for who leads.
MEMBERSHIP_MEMBER = 1
MEMBERSHIP_SUB_LEADER = 2
MEMBERSHIP_LEADER = 3

# circle_info.join_style. The real capture's circle used 3. The client's own
# create-circle UI offers three policies, and CircleCheckJoin/ApproveJoinRequest
# only make sense for the middle one, so: 1 open (auto-join), 2 approval
# required, 3 closed/invite-only.
JOIN_OPEN = 1
JOIN_APPROVAL = 2
JOIN_CLOSED = 3

MAX_CIRCLE_MEMBERS = 30         # real capture: member_num 30 on a full circle


def _db_path() -> Path:
    """Region-suffixed, exactly like state.py's -- Global and JP are separate
    servers and must never share a social graph."""
    return _DATA_DIR / f"social{region.db_suffix()}.sqlite3"


@contextmanager
def _connect():
    key = region.CURRENT_REGION.get()
    conn = _conns.get(key)
    if conn is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(conn)
        _conns[key] = conn
    with conn:
        yield conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    # One row per DIRECTED edge. active=0 is an unfollowed edge kept only so
    # its original follow_time survives -- see the module docstring.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS follow_edge (
            follower_id TEXT NOT NULL,
            followee_id TEXT NOT NULL,
            create_time TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (follower_id, followee_id)
        )
        """
    )
    # The reverse lookup ("who follows me") is as hot as the forward one --
    # friend/index does both on every call.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS follow_edge_followee"
        " ON follow_edge (followee_id, active)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle (
            circle_id        INTEGER PRIMARY KEY,
            leader_viewer_id TEXT    NOT NULL,
            name             TEXT    NOT NULL,
            comment          TEXT    NOT NULL DEFAULT '',
            join_style       INTEGER NOT NULL DEFAULT 1,
            policy           INTEGER NOT NULL DEFAULT 0,
            make_time        TEXT    NOT NULL,
            month_point      INTEGER NOT NULL DEFAULT 0,
            last_month_point INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # A viewer is in at most one circle, which is why viewer_id -- not
    # (circle_id, viewer_id) -- is the primary key: it makes "already in a
    # circle" a constraint the database enforces rather than something every
    # join path has to remember to check.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_member (
            viewer_id                 TEXT PRIMARY KEY,
            circle_id                 INTEGER NOT NULL,
            membership                INTEGER NOT NULL DEFAULT 1,
            join_time                 TEXT NOT NULL,
            penalty_end_time          TEXT NOT NULL DEFAULT '0000-00-00 00:00:00',
            item_request_end_time     TEXT NOT NULL DEFAULT '0000-00-00 00:00:00',
            last_check_post_id        INTEGER NOT NULL DEFAULT 0,
            ranking_result_check_time TEXT NOT NULL DEFAULT '0000-00-00 00:00:00'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_member_circle ON circle_member (circle_id)"
    )
    # post_id is server-global and monotonic in the real capture (14738 ..
    # 15307 across one circle), and circle_user.last_check_post_id is compared
    # against it, so it must be an INTEGER PRIMARY KEY (rowid) rather than a
    # per-circle counter.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_chat (
            post_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            circle_id    INTEGER NOT NULL,
            viewer_id    TEXT    NOT NULL,
            message_type INTEGER NOT NULL,
            message_id   INTEGER NOT NULL DEFAULT 0,
            message      TEXT    NOT NULL DEFAULT '',
            display      INTEGER NOT NULL DEFAULT 1,
            create_time  TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_chat_circle ON circle_chat (circle_id, post_id)"
    )
    # The two recruitment flows share one table because they are the same
    # pending (circle, viewer) pair seen from two sides -- kind says which
    # side raised it, and therefore which side may approve it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_pending (
            circle_id   INTEGER NOT NULL,
            viewer_id   TEXT    NOT NULL,
            kind        TEXT    NOT NULL,   -- 'request' (player applied) | 'scout' (circle invited)
            create_time TEXT    NOT NULL,
            checked     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (circle_id, viewer_id, kind)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_pending_viewer ON circle_pending (viewer_id, kind)"
    )
    # Circle-level profile card (CircleGetProfileCardInfo/SetProfileCardInfo),
    # distinct from user_profile.py's personal one. Stored as JSON text: it is
    # a display blob, never queried by field.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_profile_card (
            circle_id INTEGER PRIMARY KEY,
            info_json TEXT NOT NULL
        )
        """
    )
    # Item requests ("please send me a Mile Piece") and the donations against
    # them. Shapes from the room_enter capture:
    #   circle_item_request {request_id, viewer_id, item_id, end_time}
    #   circle_item_donate  {donate_id, request_id, viewer_id, item_num,
    #                        create_time}
    # request_id/donate_id are server-global autoincrement ids there (495355,
    # 1328700 ...), not per-circle counters, so both are INTEGER PRIMARY KEY.
    # circle_id is denormalized onto both so the room screen can fetch a
    # circle's requests and donations without joining.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_item_request (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            circle_id  INTEGER NOT NULL,
            viewer_id  TEXT    NOT NULL,
            item_id    INTEGER NOT NULL,
            end_time   TEXT    NOT NULL,
            received   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_item_request_circle"
        " ON circle_item_request (circle_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_item_donate (
            donate_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            circle_id   INTEGER NOT NULL,
            request_id  INTEGER NOT NULL,
            viewer_id   TEXT    NOT NULL,
            item_num    INTEGER NOT NULL,
            create_time TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_item_donate_circle"
        " ON circle_item_donate (circle_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS circle_item_donate_request"
        " ON circle_item_donate (request_id)"
    )
    # circle_user/set_profile: a member's own note inside the circle.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_user_profile (
            viewer_id TEXT PRIMARY KEY,
            info_json TEXT NOT NULL
        )
        """
    )


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- follows


def follow(viewer_id, target_id) -> dict:
    """Make viewer_id follow target_id and return the friend_data block the
    client expects back.

    Re-following an account restores the ORIGINAL follow_time rather than
    stamping a new one, because the real server's un_follow response hands
    back the old timestamp (0026_friend_un_follow.json) -- it deactivates the
    edge, it does not forget it.
    """
    me, them = str(viewer_id), str(target_id)
    if me == them:
        raise ValueError("cannot follow yourself")
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO follow_edge (follower_id, followee_id, create_time, active)"
            " VALUES (?,?,?,1)"
            " ON CONFLICT(follower_id, followee_id) DO UPDATE SET active = 1",
            (me, them, now()),
        )
    return friend_data(me, them)


def unfollow(viewer_id, target_id) -> dict:
    me, them = str(viewer_id), str(target_id)
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE follow_edge SET active = 0 WHERE follower_id = ? AND followee_id = ?",
            (me, them),
        )
    return friend_data(me, them)


def remove_follower(viewer_id, target_id) -> dict:
    """friend/un_follower -- drop somebody who follows ME. The mirror image of
    unfollow: it deactivates THEIR edge to me, never mine to them, so a mutual
    pair degrades to state 1 (I still follow them) rather than to 0."""
    me, them = str(viewer_id), str(target_id)
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE follow_edge SET active = 0 WHERE follower_id = ? AND followee_id = ?",
            (them, me),
        )
    return friend_data(me, them)


def _edge_times(conn, me: str, them: str) -> tuple:
    rows = conn.execute(
        "SELECT follower_id, create_time FROM follow_edge"
        " WHERE active = 1 AND ((follower_id = ? AND followee_id = ?)"
        "                    OR (follower_id = ? AND followee_id = ?))",
        (me, them, them, me),
    ).fetchall()
    follow_time = follower_time = None
    for r in rows:
        if r["follower_id"] == me:
            follow_time = r["create_time"]
        else:
            follower_time = r["create_time"]
    return follow_time, follower_time


def friend_data(viewer_id, target_id) -> dict:
    """The {friend_viewer_id, state, follow_time, follower_time} block, seen
    from viewer_id's side.

    An INACTIVE edge still reports its stored follow_time with state 0 --
    that is precisely what the real un_follow response does. Only a pair that
    has never had an edge at all gets the zero timestamp.
    """
    me, them = str(viewer_id), str(target_id)
    with _lock, _connect() as conn:
        follow_time, follower_time = _edge_times(conn, me, them)
        if follow_time is None:
            row = conn.execute(
                "SELECT create_time FROM follow_edge"
                " WHERE follower_id = ? AND followee_id = ?", (me, them)
            ).fetchone()
            stale_follow = row["create_time"] if row else None
        else:
            stale_follow = None
    state = STATE_NONE
    if follow_time and follower_time:
        state = STATE_MUTUAL
    elif follow_time:
        state = STATE_FOLLOW
    elif follower_time:
        state = STATE_FOLLOWER
    return {
        "friend_viewer_id": _wire_id(them),
        "state": state,
        "follow_time": follow_time or stale_follow or ZERO_TIME,
        "follower_time": follower_time or ZERO_TIME,
    }


def _wire_id(viewer_id):
    """viewer_ids are TEXT in the stores (state.py keys them that way, and some
    real accounts are not numeric at all -- 'test123' exists) but the client's
    friend_viewer_id field is an integer. Convert where we can, and leave the
    non-numeric ones alone rather than crashing: they simply never match a
    real client-side id, which is the correct outcome for a test account."""
    s = str(viewer_id)
    return int(s) if s.isdigit() else s


def following(viewer_id) -> dict:
    """{followee_id: follow_time} for every account this viewer follows."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT followee_id, create_time FROM follow_edge"
            " WHERE follower_id = ? AND active = 1", (str(viewer_id),)
        ).fetchall()
    return {r["followee_id"]: r["create_time"] for r in rows}


def followers(viewer_id) -> dict:
    """{follower_id: follower_time} for every account following this viewer."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT follower_id, create_time FROM follow_edge"
            " WHERE followee_id = ? AND active = 1", (str(viewer_id),)
        ).fetchall()
    return {r["follower_id"]: r["create_time"] for r in rows}


def friend_list(viewer_id) -> list:
    """friend_list as the client wants it: the UNION of both directions, one
    entry per other account, state computed per pair.

    Union, not "people I follow" -- the real capture's 87 entries were 69
    state-1 plus 17 state-2 plus 1 state-3, and the state-2 entries are people
    the account does NOT follow. Sorted by viewer_id so the list is stable
    across calls (the real one is not obviously ordered, and an unstable order
    would make the UI reshuffle on every refresh).
    """
    out_following = following(viewer_id)
    out_followers = followers(viewer_id)
    entries = []
    for other in sorted(set(out_following) | set(out_followers)):
        ft = out_following.get(other)
        rt = out_followers.get(other)
        state = STATE_MUTUAL if (ft and rt) else (STATE_FOLLOW if ft else STATE_FOLLOWER)
        entries.append({
            "friend_viewer_id": _wire_id(other),
            "state": state,
            "follow_time": ft or ZERO_TIME,
            "follower_time": rt or ZERO_TIME,
        })
    return entries


def follower_num(viewer_id) -> int:
    """Capture-proven: follower_num (18) == state-2 count (17) + state-3 count
    (1), i.e. simply how many accounts follow me."""
    return len(followers(viewer_id))


def follow_num(viewer_id) -> int:
    return len(following(viewer_id))


# ---------------------------------------------------------------- circles


def _new_circle_id(conn) -> int:
    """A fresh int32-shaped circle_id. Real ones look like 301102622 -- nine
    digits, comfortably inside int32 -- and a viewer_id-shaped value here is a
    documented client-side overflow bug (module docstring)."""
    for _ in range(64):
        cid = random.randint(100_000_000, 999_999_999)
        if conn.execute("SELECT 1 FROM circle WHERE circle_id = ?", (cid,)).fetchone() is None:
            return cid
    raise RuntimeError("could not allocate a free circle_id")


def _circle_row_to_info(row, member_num: int) -> dict:
    return {
        "circle_id": row["circle_id"],
        "leader_viewer_id": _wire_id(row["leader_viewer_id"]),
        "name": row["name"],
        "comment": row["comment"],
        "member_num": member_num,
        "join_style": row["join_style"],
        "policy": row["policy"],
        "make_time": row["make_time"],
    }


def _member_row_to_wire(row) -> dict:
    return {
        "viewer_id": _wire_id(row["viewer_id"]),
        "circle_id": row["circle_id"],
        "membership": row["membership"],
        "join_time": row["join_time"],
        "penalty_end_time": row["penalty_end_time"],
        "item_request_end_time": row["item_request_end_time"],
        "last_check_post_id": row["last_check_post_id"],
        "ranking_result_check_time": row["ranking_result_check_time"],
    }


def create_circle(viewer_id, name: str, comment: str = "",
                  join_style: int = JOIN_OPEN, policy: int = 0) -> dict:
    """circle/make. The creator becomes the leader and its first member.
    Raises if they are already in a circle -- the client's own UI forbids it,
    and circle_member's primary key enforces it regardless."""
    me = str(viewer_id)
    stamp = now()
    with _lock, _connect() as conn:
        if conn.execute("SELECT 1 FROM circle_member WHERE viewer_id = ?", (me,)).fetchone():
            raise ValueError("already in a circle")
        cid = _new_circle_id(conn)
        conn.execute(
            "INSERT INTO circle (circle_id, leader_viewer_id, name, comment,"
            " join_style, policy, make_time) VALUES (?,?,?,?,?,?,?)",
            (cid, me, name, comment, join_style, policy, stamp),
        )
        conn.execute(
            "INSERT INTO circle_member (viewer_id, circle_id, membership, join_time,"
            " ranking_result_check_time) VALUES (?,?,?,?,?)",
            (me, cid, MEMBERSHIP_LEADER, stamp, stamp),
        )
        # A player who applied elsewhere while unaffiliated should not keep a
        # live application once they lead their own circle.
        conn.execute("DELETE FROM circle_pending WHERE viewer_id = ?", (me,))
    return get_circle(cid)


def update_circle(circle_id, name=None, comment=None,
                  join_style=None, policy=None) -> dict:
    sets, args = [], []
    for col, val in (("name", name), ("comment", comment),
                     ("join_style", join_style), ("policy", policy)):
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(val)
    if sets:
        with _lock, _connect() as conn:
            conn.execute(
                f"UPDATE circle SET {', '.join(sets)} WHERE circle_id = ?",
                (*args, int(circle_id)),
            )
    return get_circle(circle_id)


def break_up_circle(circle_id) -> None:
    """circle/break_up -- the leader dissolves the circle. Everything keyed to
    it goes with it; leaving orphaned members behind would strand every one of
    them in a circle that no longer exists, with no UI path out."""
    cid = int(circle_id)
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM circle_member WHERE circle_id = ?", (cid,))
        conn.execute("DELETE FROM circle_chat WHERE circle_id = ?", (cid,))
        conn.execute("DELETE FROM circle_pending WHERE circle_id = ?", (cid,))
        conn.execute("DELETE FROM circle_profile_card WHERE circle_id = ?", (cid,))
        conn.execute("DELETE FROM circle WHERE circle_id = ?", (cid,))


def get_circle(circle_id) -> dict | None:
    cid = int(circle_id)
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM circle WHERE circle_id = ?", (cid,)).fetchone()
        if row is None:
            return None
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM circle_member WHERE circle_id = ?", (cid,)
        ).fetchone()["n"]
    return _circle_row_to_info(row, n)


def circle_of(viewer_id) -> dict | None:
    """The circle this viewer belongs to, or None. The single most-asked
    question here -- room_enter, chat polling and every membership check start
    with it."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT circle_id FROM circle_member WHERE viewer_id = ?", (str(viewer_id),)
        ).fetchone()
    return get_circle(row["circle_id"]) if row else None


def member_row(viewer_id) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM circle_member WHERE viewer_id = ?", (str(viewer_id),)
        ).fetchone()
    return _member_row_to_wire(row) if row else None


def members(circle_id) -> list:
    """circle_user_array. Ordered leader first, then sub-leaders, then by join
    time -- the real capture's array is not in viewer_id order, and a member
    list that reshuffles between calls is worse than one in a fixed order."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM circle_member WHERE circle_id = ?"
            " ORDER BY membership DESC, join_time ASC", (int(circle_id),)
        ).fetchall()
    return [_member_row_to_wire(r) for r in rows]


def member_ids(circle_id) -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT viewer_id FROM circle_member WHERE circle_id = ?"
            " ORDER BY membership DESC, join_time ASC", (int(circle_id),)
        ).fetchall()
    return [r["viewer_id"] for r in rows]


def member_count(circle_id) -> int:
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM circle_member WHERE circle_id = ?", (int(circle_id),)
        ).fetchone()["n"]


def add_member(viewer_id, circle_id, membership: int = MEMBERSHIP_MEMBER) -> dict:
    """Join. Enforces the 30-member cap and the one-circle-per-viewer rule
    INSIDE the lock, so two accounts joining the last free slot at the same
    moment cannot both succeed -- the exact race the per-viewer lock cannot
    prevent (module docstring)."""
    me, cid = str(viewer_id), int(circle_id)
    stamp = now()
    with _lock, _connect() as conn:
        if conn.execute("SELECT 1 FROM circle WHERE circle_id = ?", (cid,)).fetchone() is None:
            raise ValueError("no such circle")
        if conn.execute("SELECT 1 FROM circle_member WHERE viewer_id = ?", (me,)).fetchone():
            raise ValueError("already in a circle")
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM circle_member WHERE circle_id = ?", (cid,)
        ).fetchone()["n"]
        if n >= MAX_CIRCLE_MEMBERS:
            raise ValueError("circle is full")
        conn.execute(
            "INSERT INTO circle_member (viewer_id, circle_id, membership, join_time,"
            " ranking_result_check_time) VALUES (?,?,?,?,?)",
            (me, cid, membership, stamp, stamp),
        )
        # Joining settles every outstanding application and invitation for
        # this player, in both directions and for every circle -- not just the
        # one they joined.
        conn.execute("DELETE FROM circle_pending WHERE viewer_id = ?", (me,))
    return get_circle(cid)


def remove_member(viewer_id) -> int | None:
    """Leave or kick. Returns the circle they were in, or None.

    If the LEADER leaves, leadership passes to the longest-serving remaining
    member (preferring a sub-leader), and the circle is dissolved only when
    the last member goes. A circle whose leader_viewer_id points at a
    departed account would have no one able to accept applications, promote,
    kick or disband -- permanently stuck.
    """
    me = str(viewer_id)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT circle_id FROM circle_member WHERE viewer_id = ?", (me,)
        ).fetchone()
        if row is None:
            return None
        cid = row["circle_id"]
        conn.execute("DELETE FROM circle_member WHERE viewer_id = ?", (me,))
        circle = conn.execute(
            "SELECT leader_viewer_id FROM circle WHERE circle_id = ?", (cid,)
        ).fetchone()
        if circle is not None and circle["leader_viewer_id"] == me:
            heir = conn.execute(
                "SELECT viewer_id FROM circle_member WHERE circle_id = ?"
                " ORDER BY membership DESC, join_time ASC LIMIT 1", (cid,)
            ).fetchone()
            if heir is None:
                conn.execute("DELETE FROM circle WHERE circle_id = ?", (cid,))
                conn.execute("DELETE FROM circle_chat WHERE circle_id = ?", (cid,))
                conn.execute("DELETE FROM circle_pending WHERE circle_id = ?", (cid,))
                conn.execute("DELETE FROM circle_profile_card WHERE circle_id = ?", (cid,))
            else:
                conn.execute(
                    "UPDATE circle SET leader_viewer_id = ? WHERE circle_id = ?",
                    (heir["viewer_id"], cid),
                )
                conn.execute(
                    "UPDATE circle_member SET membership = ? WHERE viewer_id = ?",
                    (MEMBERSHIP_LEADER, heir["viewer_id"]),
                )
    return cid


def set_membership(viewer_id, membership: int) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circle_member SET membership = ? WHERE viewer_id = ?",
            (membership, str(viewer_id)),
        )


def change_leader(circle_id, new_leader_id) -> None:
    """Transfer leadership. The outgoing leader is demoted to sub-leader
    rather than plain member: the real client shows a former leader as a
    sub-leader after a handover, and demoting them all the way to 1 would
    silently strip privileges the handover never asked to remove."""
    cid, new = int(circle_id), str(new_leader_id)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT leader_viewer_id FROM circle WHERE circle_id = ?", (cid,)
        ).fetchone()
        if row is None:
            raise ValueError("no such circle")
        if conn.execute(
            "SELECT 1 FROM circle_member WHERE viewer_id = ? AND circle_id = ?", (new, cid)
        ).fetchone() is None:
            raise ValueError("new leader is not a member of this circle")
        conn.execute("UPDATE circle SET leader_viewer_id = ? WHERE circle_id = ?", (new, cid))
        conn.execute(
            "UPDATE circle_member SET membership = ? WHERE viewer_id = ?",
            (MEMBERSHIP_SUB_LEADER, row["leader_viewer_id"]),
        )
        conn.execute(
            "UPDATE circle_member SET membership = ? WHERE viewer_id = ?",
            (MEMBERSHIP_LEADER, new),
        )


def search_circles(keyword: str = "", join_style: int = 0, policy: int = 0,
                   member_num: int = 0, limit: int = 30) -> list:
    """circle/conditional_search. Every filter is optional and 0/'' means "do
    not filter", which is how the client sends an unfiltered browse.

    member_num filters to circles with AT LEAST that many members -- the
    client's filter row reads as a minimum size, and a player browsing for a
    circle wants the active ones, not an exact headcount match.
    """
    sql = [
        "SELECT c.*, (SELECT COUNT(*) FROM circle_member m WHERE m.circle_id = c.circle_id)"
        " AS n FROM circle c WHERE 1=1"
    ]
    args = []
    if keyword:
        sql.append(" AND (c.name LIKE ? OR c.comment LIKE ?)")
        args += [f"%{keyword}%", f"%{keyword}%"]
    if join_style:
        sql.append(" AND c.join_style = ?")
        args.append(int(join_style))
    if policy:
        sql.append(" AND c.policy = ?")
        args.append(int(policy))
    if member_num:
        sql.append(" AND n >= ?")
        args.append(int(member_num))
    sql.append(" ORDER BY n DESC, c.make_time ASC LIMIT ?")
    args.append(int(limit))
    with _lock, _connect() as conn:
        rows = conn.execute("".join(sql), args).fetchall()
    return [_circle_row_to_info(r, r["n"]) for r in rows]


def ranking_top(limit: int = 30) -> list:
    """circle/get_ranking_top. Ranked by this month's accumulated point, which
    is the same number circle_ranking_this_month reports."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM circle_member m WHERE m.circle_id = c.circle_id)"
            " AS n FROM circle c ORDER BY c.month_point DESC, c.make_time ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for i, r in enumerate(rows, start=1):
        info = _circle_row_to_info(r, r["n"])
        out.append({"rank": i, "point": r["month_point"], "circle_info": info})
    return out


def circle_rank(circle_id) -> tuple:
    """(rank, this-month point, last-month point) for one circle. Rank is its
    position in the same ordering ranking_top uses, so the two never disagree."""
    cid = int(circle_id)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT month_point, last_month_point FROM circle WHERE circle_id = ?", (cid,)
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        ahead = conn.execute(
            "SELECT COUNT(*) AS n FROM circle WHERE month_point > ?", (row["month_point"],)
        ).fetchone()["n"]
    return (ahead + 1, row["month_point"], row["last_month_point"])


# What one donation is worth to the circle. Every donation pays this, and it
# is the only thing that moves month_point on this server -- so a circle's
# monthly ranking is exactly "how much did we help each other this month".
POINT_PER_DONATION = 10


def add_circle_point(circle_id, point: int) -> int:
    """Credit the circle's monthly point total, and report the new total so
    the caller can log/serve it without a second read."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circle SET month_point = month_point + ? WHERE circle_id = ?",
            (int(point), int(circle_id)),
        )
        row = conn.execute(
            "SELECT month_point FROM circle WHERE circle_id = ?", (int(circle_id),)
        ).fetchone()
    return row["month_point"] if row else 0


# ------------------------------------------------------------ circle chat


# message_type. These are the client's own WorkCircleChatData.MessageType
# constants (dump.cs), not a guess -- and 5 is NOT a generic "system notice",
# which is what this file used to assume:
#
#   0 SYSTEM_MESSAGE_USER_NAME   somebody did something (name substituted in)
#   1 MESSAGE                    text, in message_data (message_id null)
#   2 STAMP                      message_id is the stamp id, no text
#   3 SYSTEM_MESSAGE_VARIABLE    system line with a non-name variable
#   4 ROOM_MATCH_INVITE          message_id is the room id
#   5 ITEM_REQUEST               message_id is the REQUEST_ID
#   6 UNREAD                     the "unread from here" divider
#   7 PRACTICE_PARTNER_SHARE     a partner offer
#   8 PRACTICE_PARTNER_SHARE_COMMENT   the same, with a canned comment
#
# The capture agrees: every message_type-5 row in the room_enter capture has
# a message_id that appears verbatim in circle_item_request_array (e.g. post
# 15003 -> request 493754), and its create_time is exactly the request's
# end_time minus 8 hours.
CHAT_SYSTEM_USER_NAME = 0
CHAT_TEXT = 1
CHAT_STAMP = 2
CHAT_SYSTEM_VARIABLE = 3
CHAT_ROOM_MATCH_INVITE = 4
CHAT_ITEM_REQUEST = 5
CHAT_PARTNER_SHARE = 7
CHAT_PARTNER_SHARE_COMMENT = 8

# WorkCircleChatData.SYSTEM_MESSAGE_ID -- which system line a type-0 post is.
SYS_ORGANIZATION = 1
SYS_JOIN = 2
SYS_LEAVE = 3
SYS_LEADER = 4
SYS_SUB_LEADER = 5
SYS_NAME_CHANGE = 8


def _chat_wire(r) -> dict:
    """One chat row as the client wants it.

    The text field is `message_data`, NOT `message` -- captures/20260818_
    122351/0039_circle_chat_send_message.json is unambiguous, and a text
    message under the wrong key renders as an empty bubble. `message_id` is
    NULL for a text post in that same capture (a stamp puts the stamp_id
    there), so 0 is normalized back to None rather than sent as a real id.
    """
    text = r["message"]
    entry = {"post_id": r["post_id"], "viewer_id": _wire_id(r["viewer_id"]),
             "message_type": r["message_type"],
             "message_id": r["message_id"] or None,
             "display": r["display"], "create_time": r["create_time"]}
    if text:
        entry["message_data"] = text
    return entry


def post_chat(circle_id, viewer_id, message_type: int,
              message_id: int = 0, message: str = "") -> dict:
    stamp = now()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO circle_chat (circle_id, viewer_id, message_type, message_id,"
            " message, display, create_time) VALUES (?,?,?,?,?,1,?)",
            (int(circle_id), str(viewer_id), int(message_type), int(message_id),
             message or "", stamp),
        )
        post_id = cur.lastrowid
    return _chat_wire({"post_id": post_id, "viewer_id": viewer_id,
                       "message_type": int(message_type), "message_id": int(message_id),
                       "message": message or "", "display": 1, "create_time": stamp})


def chat_since(circle_id, after_post_id: int = 0, limit: int = 100) -> list:
    """Messages newer than after_post_id, oldest first.

    The real room_enter capture carried the newest 100 messages, so a cold
    open (after_post_id 0) takes the LAST 100 rather than the first 100 --
    otherwise a long-lived circle opens on its oldest history.
    """
    cid = int(circle_id)
    with _lock, _connect() as conn:
        if after_post_id:
            rows = conn.execute(
                "SELECT * FROM circle_chat WHERE circle_id = ? AND post_id > ?"
                " ORDER BY post_id ASC LIMIT ?", (cid, int(after_post_id), int(limit))
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM circle_chat WHERE circle_id = ?"
                " ORDER BY post_id DESC LIMIT ?", (cid, int(limit))
            ).fetchall()
            rows = list(reversed(rows))
    return [_chat_wire(r) for r in rows]


def set_last_check_post_id(viewer_id, post_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circle_member SET last_check_post_id = ? WHERE viewer_id = ?",
            (int(post_id), str(viewer_id)),
        )


# ------------------------------------------- join requests and scouting


def add_pending(circle_id, viewer_id, kind: str) -> None:
    """kind='request' (the player applied) or 'scout' (the circle invited).
    Both directions can legitimately exist for the same pair at once -- a
    player applying to a circle that independently scouted them -- and either
    one being approved settles both, via add_member's blanket delete."""
    if kind not in ("request", "scout"):
        raise ValueError(f"unknown pending kind {kind!r}")
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO circle_pending (circle_id, viewer_id, kind, create_time)"
            " VALUES (?,?,?,?)", (int(circle_id), str(viewer_id), kind, now()),
        )


def drop_pending(circle_id, viewer_id, kind: str | None = None) -> None:
    args = [int(circle_id), str(viewer_id)]
    sql = "DELETE FROM circle_pending WHERE circle_id = ? AND viewer_id = ?"
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    with _lock, _connect() as conn:
        conn.execute(sql, args)


def pending_for_circle(circle_id, kind: str) -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT viewer_id, create_time, checked FROM circle_pending"
            " WHERE circle_id = ? AND kind = ? ORDER BY create_time ASC",
            (int(circle_id), kind),
        ).fetchall()
    return [{"viewer_id": _wire_id(r["viewer_id"]), "create_time": r["create_time"],
             "checked": r["checked"]} for r in rows]


def pending_for_viewer(viewer_id, kind: str) -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT circle_id, create_time, checked FROM circle_pending"
            " WHERE viewer_id = ? AND kind = ? ORDER BY create_time ASC",
            (str(viewer_id), kind),
        ).fetchall()
    return [{"circle_id": r["circle_id"], "create_time": r["create_time"],
             "checked": r["checked"]} for r in rows]


def mark_pending_checked(circle_id) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circle_pending SET checked = 1 WHERE circle_id = ?", (int(circle_id),)
        )


# ---------------------------------------------------------- profile cards


def get_profile_card(circle_id) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT info_json FROM circle_profile_card WHERE circle_id = ?", (int(circle_id),)
        ).fetchone()
    return row["info_json"] if row else None


def set_profile_card(circle_id, info_json: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO circle_profile_card (circle_id, info_json) VALUES (?,?)"
            " ON CONFLICT(circle_id) DO UPDATE SET info_json = excluded.info_json",
            (int(circle_id), info_json),
        )


def get_user_profile(viewer_id) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT info_json FROM circle_user_profile WHERE viewer_id = ?", (str(viewer_id),)
        ).fetchone()
    return row["info_json"] if row else None


def set_user_profile(viewer_id, info_json: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO circle_user_profile (viewer_id, info_json) VALUES (?,?)"
            " ON CONFLICT(viewer_id) DO UPDATE SET info_json = excluded.info_json",
            (str(viewer_id), info_json),
        )


# ------------------------------------------------------------ item requests
# A member asks the circle for an item; other members donate toward it; the
# asker later collects what was donated. The daily donation cap is per
# DONOR, not per circle -- daily_donated_count is served to the viewer doing
# the donating in the real donate_multiple response.

# How long an open request stays live. The real capture's end_times sit
# roughly a day past their creation and the client shows a countdown, so a
# flat 24h window is the reading; nothing in master.mdb configures it.
# 8 hours, measured off the real capture rather than assumed: every
# message_type-5 chat post announcing a request sits exactly 8h before that
# request's own end_time (post 15003 12:07:26 -> request 493754 end 20:07:26,
# and so on for every pair in the room_enter/get_request_data captures).
REQUEST_WINDOW_SECONDS = 8 * 3600


def open_request(circle_id, viewer_id, item_id: int) -> dict:
    """Raise an item request. One live request per member at a time, which is
    what circle_user.item_request_end_time (a single timestamp per member,
    not a list) implies -- so an existing live one is replaced rather than
    stacked."""
    cid, me = int(circle_id), str(viewer_id)
    end = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time() + REQUEST_WINDOW_SECONDS))
    with _lock, _connect() as conn:
        conn.execute(
            "DELETE FROM circle_item_request WHERE viewer_id = ? AND received = 0", (me,)
        )
        cur = conn.execute(
            "INSERT INTO circle_item_request (circle_id, viewer_id, item_id, end_time)"
            " VALUES (?,?,?,?)", (cid, me, int(item_id), end),
        )
        conn.execute(
            "UPDATE circle_member SET item_request_end_time = ? WHERE viewer_id = ?",
            (end, me),
        )
        rid = cur.lastrowid
    return {"request_id": rid, "viewer_id": _wire_id(me),
            "item_id": int(item_id), "end_time": end}


def requests_for_circle(circle_id) -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT request_id, viewer_id, item_id, end_time FROM circle_item_request"
            " WHERE circle_id = ? AND received = 0 ORDER BY request_id ASC",
            (int(circle_id),),
        ).fetchall()
    return [{"request_id": r["request_id"], "viewer_id": _wire_id(r["viewer_id"]),
             "item_id": r["item_id"], "end_time": r["end_time"]} for r in rows]


def request_row(request_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM circle_item_request WHERE request_id = ?", (int(request_id),)
        ).fetchone()
    return dict(row) if row else None


def donations_for_circle(circle_id) -> list:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT donate_id, request_id, viewer_id, item_num, create_time"
            " FROM circle_item_donate WHERE circle_id = ? ORDER BY donate_id ASC",
            (int(circle_id),),
        ).fetchall()
    return [{"donate_id": r["donate_id"], "request_id": r["request_id"],
             "viewer_id": _wire_id(r["viewer_id"]), "item_num": r["item_num"],
             "create_time": r["create_time"]} for r in rows]


def add_donation(circle_id, request_id, viewer_id, item_num: int) -> dict:
    stamp = now()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO circle_item_donate (circle_id, request_id, viewer_id,"
            " item_num, create_time) VALUES (?,?,?,?,?)",
            (int(circle_id), int(request_id), str(viewer_id), int(item_num), stamp),
        )
        did = cur.lastrowid
    return {"donate_id": did, "request_id": int(request_id),
            "viewer_id": _wire_id(viewer_id), "item_num": int(item_num),
            "create_time": stamp}


def donated_today(viewer_id) -> int:
    """How many donations this account has made today -- the number the real
    donate_multiple response returns as daily_donated_count."""
    today = time.strftime("%Y-%m-%d")
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM circle_item_donate"
            " WHERE viewer_id = ? AND create_time >= ?", (str(viewer_id), today)
        ).fetchone()["n"]


def collect_requests(viewer_id) -> list:
    """Mark this member's fulfilled requests received and hand back the
    donations against them, so the caller can actually grant the items. Doing
    both inside one lock is what stops a double-tap on Receive from paying
    out twice."""
    me = str(viewer_id)
    with _lock, _connect() as conn:
        reqs = conn.execute(
            "SELECT request_id, item_id FROM circle_item_request"
            " WHERE viewer_id = ? AND received = 0", (me,)
        ).fetchall()
        if not reqs:
            return []
        ids = [r["request_id"] for r in reqs]
        marks = ",".join("?" * len(ids))
        donations = conn.execute(
            "SELECT donate_id, request_id, viewer_id, item_num, create_time"
            " FROM circle_item_donate WHERE request_id IN (%s)" % marks, ids
        ).fetchall()
        conn.execute(
            "UPDATE circle_item_request SET received = 1 WHERE request_id IN (%s)" % marks,
            ids,
        )
    by_request = {r["request_id"]: r["item_id"] for r in reqs}
    return [{"donate_id": d["donate_id"], "request_id": d["request_id"],
             "viewer_id": _wire_id(d["viewer_id"]), "item_num": d["item_num"],
             "create_time": d["create_time"],
             "item_id": by_request.get(d["request_id"])} for d in donations]
