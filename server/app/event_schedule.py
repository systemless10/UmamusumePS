from __future__ import annotations

import json
import sqlite3
import datetime as dt

FAMILIES = {
    "training_challenge": ("training_challenge_master", "id"),
    "story_event": ("story_event_data", "story_event_id"),
    "factor_research": ("factor_research_data", "factor_research_event_id"),
    "champions": ("champions_schedule", "id"),
    "legend_race": ("legend_race", "id"),
    "heroes": ("heroes_data", "heroes_id"),
    "challenge_match": ("challenge_match_data", "challenge_match_id"),
    "team_building": ("team_building_data", "team_building_event_id"),
    "map_event": ("map_event_data", "event_id"),
}
APPLY_REVISION = 4
DATES = ("notice_date", "start_date", "middle_date", "middle_date_01", "middle_date_02",
         "ending_date", "end_date", "start_result_date", "end_result_date")


def schedules():
    from . import custom_support as C
    c = C._db()
    if c is None:
        return []
    try:
        r = c.execute("SELECT value FROM settings WHERE key='event_schedules'").fetchone()
        return json.loads(r[0]) if r else []
    finally:
        c.close()


def validate(entries, connection):
    if not isinstance(entries, list):
        raise ValueError("Event schedules must be an array")
    out, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each event schedule must be an object")
        family = entry.get("family")
        if family not in FAMILIES or family in seen:
            raise ValueError("Select at most one event per supported family")
        seen.add(family)
        table, key = FAMILIES[family]
        eid, start, end = (entry.get(k) for k in ("event_id", "start_date", "end_date"))
        if any(type(v) is not int for v in (eid, start, end)) or eid <= 0 or not 0 < start < end < 2**32:
            raise ValueError("Event ID and UTC dates must be positive integers, start < end")
        row = connection.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (eid,)).fetchone()
        if row is None and not _ported(table, key, eid):
            raise ValueError(f"{family} {eid} is absent from this client's master")
        out.append({"family": family, "event_id": eid, "start_date": start, "end_date": end})
    return out


def _ported(table, key, eid):
    from . import custom_support as C
    return any(r.get(key) == eid for b in C.unreleased_bundles() for r in (b.get("tables") or {}).get(table) or [])


def set_schedules(entries):
    from . import custom_support as C, master_data as M
    entries = validate(entries, M.get_connection())
    with C._writing() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('event_schedules',?)", (json.dumps(entries),))
        C._audit(c, "event_schedules", detail={"events": entries})
    return entries


def apply(connection, entries):
    connection.row_factory = sqlite3.Row
    entries = validate(entries, connection)
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for entry in entries:
        family, eid = entry["family"], entry["event_id"]
        table, key = FAMILIES[family]
        row = dict(connection.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (eid,)).fetchone())
        old_start = row["start_date"]
        old_end = row.get("end_result_date", row["end_date"])
        span = max(1, old_end - old_start)

        def shift(value):
            if isinstance(value, str):
                if not value:
                    return value
                try:
                    stamp = dt.datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                except ValueError as exc:
                    raise ValueError(f"Unrecognised event date: {value}") from exc
                return dt.datetime.fromtimestamp(shift(int(stamp.timestamp())), dt.timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
            if value <= old_start:
                return entry["start_date"]
            return min(entry["end_date"], entry["start_date"] +
                       (value - old_start) * (entry["end_date"] - entry["start_date"]) // span)

        date_cols = [k for k in DATES if k in row]
        connection.execute(f'UPDATE "{table}" SET ' + ','.join(f'"{k}"=1' for k in date_cols) +
                           f' WHERE "{key}"<>?', (eid,))
        connection.execute(f'UPDATE "{table}" SET ' + ','.join(f'"{k}"=?' for k in date_cols) +
                           f' WHERE "{key}"=?', (*[shift(row[k]) for k in date_cols], eid))

        if family == "story_event":
            for related in tables:
                if not related.startswith("story_event_") or related == table:
                    continue
                cols = [r[1] for r in connection.execute(f'PRAGMA table_info("{related}")')]
                dates = [k for k in DATES if k in cols]
                if "story_event_id" not in cols or not dates or "id" not in cols:
                    continue
                for r in connection.execute(f'SELECT * FROM "{related}" WHERE story_event_id=?', (eid,)).fetchall():
                    connection.execute(f'UPDATE "{related}" SET ' + ','.join(f'"{k}"=?' for k in dates) +
                                       ' WHERE id=?', (*[shift(r[k]) for k in dates], r["id"]))
        shop_id = row.get("shop_id") or row.get("item_exchange_top_id")
        if shop_id and "item_exchange_top" in tables:
            connection.execute("UPDATE item_exchange_top SET start_date=?,end_date=? WHERE id=?",
                               (entry["start_date"], entry["end_date"], shop_id))
            if "item_exchange" in tables:
                connection.execute("UPDATE item_exchange SET start_date=?,end_date=? WHERE item_exchange_top_id=?",
                                   (entry["start_date"], entry["end_date"], shop_id))
        if family == "training_challenge" and "mission_data" in tables:
            for mission in connection.execute("SELECT * FROM mission_data WHERE mission_type=12 AND condition_value_1=?", (eid,)).fetchall():
                connection.execute("UPDATE mission_data SET start_date=?,end_date=? WHERE id=?",
                    (shift(mission["start_date"]), shift(mission["end_date"]), mission["id"]))
        if family == "map_event":
            for related, cols in (("map_event_area_data", ("start_date",)),
                                  ("map_event_map_point", ("start_date", "movie_start_date")),
                                  ("map_event_story_data", ("start_date",))):
                if related not in tables:
                    continue
                for r in connection.execute(f'SELECT * FROM "{related}" WHERE event_id=?', (eid,)).fetchall():
                    connection.execute(f'UPDATE "{related}" SET ' + ','.join(f'"{k}"=?' for k in cols) + ' WHERE id=?',
                                       (*[shift(r[k]) if r[k] not in ("", None, 0) else r[k] for k in cols], r["id"]))
        if family == "heroes" and "heroes_stage_schedule" in tables:
            for r in connection.execute("SELECT * FROM heroes_stage_schedule").fetchall():
                if r["heroes_id"] == eid:
                    connection.execute("UPDATE heroes_stage_schedule SET start_date=?,end_date=? WHERE id=?",
                                       (shift(r["start_date"]), shift(r["end_date"]), r["id"]))
                else:
                    connection.execute("UPDATE heroes_stage_schedule SET start_date=1,end_date=1 WHERE id=?", (r["id"],))
        if family == "heroes" and "item_data" in tables:
            from . import region
            tz = dt.timezone(dt.timedelta(hours=9)) if region.CURRENT_REGION.get() == "jp" else dt.timezone.utc
            fmt = lambda value: dt.datetime.fromtimestamp(value, tz).strftime("%Y/%m/%d %H:%M:%S")
            connection.execute("UPDATE item_data SET start_date=?,end_date=? WHERE id=?",
                               (fmt(entry["start_date"]), fmt(entry["end_date"]), 3000 + eid))
        if family == "challenge_match" and "mission_data" in tables and "challenge_match_race" in tables:
            inst = [r[0] for r in connection.execute(
                "SELECT race_instance_id FROM challenge_match_race WHERE challenge_match_id=?", (eid,))]
            if inst:
                q = ",".join("?" * len(inst))
                for mission in connection.execute(
                        f"SELECT * FROM mission_data WHERE condition_type=200024 AND condition_value_1 IN ({q})",
                        inst).fetchall():
                    connection.execute("UPDATE mission_data SET start_date=?,end_date=? WHERE id=?",
                        (shift(mission["start_date"]), shift(mission["end_date"]), mission["id"]))
        if family == "challenge_match":
            for tbl, col, rid in (("skill_data", "id", row.get("bonus_skill_id")),
                                  ("succession_factor", "factor_id", row.get("bonus_factor_id"))):
                if not rid or tbl not in tables:
                    continue
                cols = [r[1] for r in connection.execute(f'PRAGMA table_info("{tbl}")')]
                for k in ("start_date", "end_date"):
                    if k in cols:
                        cur = connection.execute(f'SELECT "{k}" FROM "{tbl}" WHERE "{col}"=?', (rid,)).fetchone()
                        if cur is not None and cur[0] and cur[0] > 1:
                            connection.execute(f'UPDATE "{tbl}" SET "{k}"=? WHERE "{col}"=?', (shift(cur[0]), rid))
            if "item_data" in tables:
                from . import region
                tz = dt.timezone(dt.timedelta(hours=9)) if region.CURRENT_REGION.get() == "jp" else dt.timezone.utc
                fmt = lambda value: dt.datetime.fromtimestamp(value, tz).strftime("%Y/%m/%d %H:%M:%S")
                connection.execute("UPDATE item_data SET start_date=?,end_date=? WHERE id=159",
                                   (fmt(entry["start_date"]), fmt(entry["end_date"])))
        if family == "team_building":
            n = eid - 1000
            if "item_data" in tables:
                for iid in (1000 + n, 2000 + n):
                    cur = connection.execute("SELECT start_date,end_date FROM item_data WHERE id=?", (iid,)).fetchone()
                    if cur is not None:
                        connection.execute("UPDATE item_data SET start_date=?,end_date=? WHERE id=?",
                                           (shift(cur[0]), shift(cur[1]), iid))
            if "mission_data" in tables:
                for mission in connection.execute(
                        "SELECT * FROM mission_data WHERE id/1000=? AND mission_type IN (14,15)",
                        (2400 + n,)).fetchall():
                    connection.execute("UPDATE mission_data SET start_date=?,end_date=? WHERE id=?",
                        (shift(mission["start_date"]), shift(mission["end_date"]), mission["id"]))
        if family == "champions":
            from . import champions_meeting
            champions_meeting.apply_schedule(connection, entry)
        if family == "factor_research" and "item_data" in tables:
            from . import region
            tz = dt.timezone(dt.timedelta(hours=9)) if region.CURRENT_REGION.get() == "jp" else dt.timezone.utc
            fmt = lambda value: dt.datetime.fromtimestamp(value, tz).strftime("%Y/%m/%d %H:%M:%S")
            connection.execute("UPDATE item_data SET start_date=?,end_date=? WHERE "
                               "item_category IN (174,194,195) AND effect_type_1=?",
                               (fmt(entry["start_date"]), fmt(entry["end_date"]), eid))
    return entries
