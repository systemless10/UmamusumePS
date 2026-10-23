
from __future__ import annotations

import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from . import region


_lock = Lock()
_conns: dict[str, sqlite3.Connection] = {}

ZERO_TIME = "0000-00-00 00:00:00"

STATE_NONE = 0
STATE_FOLLOW = 1
STATE_FOLLOWER = 2
STATE_MUTUAL = 3

MEMBERSHIP_MEMBER = 1
MEMBERSHIP_SUB_LEADER = 2
MEMBERSHIP_LEADER = 3

JOIN_OPEN = 1
JOIN_APPROVAL = 2
JOIN_CLOSED = 3

MAX_CIRCLE_MEMBERS = 30


def _db_path() -> Path:
    return region.data_dir() / "social.sqlite3"


@contextmanager
def _connect():
    key = region.CURRENT_REGION.get()
    conn = _conns.get(key)
    if conn is None:
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(conn)
        _conns[key] = conn
    with conn:
        yield conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS circle_profile_card (
            circle_id INTEGER PRIMARY KEY,
            info_json TEXT NOT NULL
        )
        """
    )
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


def follow(viewer_id, target_id) -> dict:
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
    s = str(viewer_id)
    return int(s) if s.isdigit() else s


def following(viewer_id) -> dict:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT followee_id, create_time FROM follow_edge"
            " WHERE follower_id = ? AND active = 1", (str(viewer_id),)
        ).fetchall()
    return {r["followee_id"]: r["create_time"] for r in rows}


def followers(viewer_id) -> dict:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT follower_id, create_time FROM follow_edge"
            " WHERE followee_id = ? AND active = 1", (str(viewer_id),)
        ).fetchall()
    return {r["follower_id"]: r["create_time"] for r in rows}


def friend_list(viewer_id) -> list:
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
    return len(followers(viewer_id))


def follow_num(viewer_id) -> int:
    return len(following(viewer_id))


def _new_circle_id(conn) -> int:
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
        conn.execute("DELETE FROM circle_pending WHERE viewer_id = ?", (me,))
    return get_circle(cid)


def remove_member(viewer_id) -> int | None:
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


POINT_PER_DONATION = 10


def add_circle_point(circle_id, point: int) -> int:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circle SET month_point = month_point + ? WHERE circle_id = ?",
            (int(point), int(circle_id)),
        )
        row = conn.execute(
            "SELECT month_point FROM circle WHERE circle_id = ?", (int(circle_id),)
        ).fetchone()
    return row["month_point"] if row else 0


CHAT_SYSTEM_USER_NAME = 0
CHAT_TEXT = 1
CHAT_STAMP = 2
CHAT_SYSTEM_VARIABLE = 3
CHAT_ROOM_MATCH_INVITE = 4
CHAT_ITEM_REQUEST = 5
CHAT_PARTNER_SHARE = 7
CHAT_PARTNER_SHARE_COMMENT = 8

SYS_ORGANIZATION = 1
SYS_JOIN = 2
SYS_LEAVE = 3
SYS_LEADER = 4
SYS_SUB_LEADER = 5
SYS_NAME_CHANGE = 8


def _chat_wire(r) -> dict:
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


def add_pending(circle_id, viewer_id, kind: str) -> None:
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


REQUEST_WINDOW_SECONDS = 8 * 3600


def open_request(circle_id, viewer_id, item_id: int) -> dict:
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
    today = time.strftime("%Y-%m-%d")
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM circle_item_donate"
            " WHERE viewer_id = ? AND create_time >= ?", (str(viewer_id), today)
        ).fetchone()["n"]


def collect_requests(viewer_id) -> list:
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
