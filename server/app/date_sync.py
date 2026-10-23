from __future__ import annotations

import datetime as dt
import json
import sqlite3

GROUPS = {
    "banners": ("Scout banners", ("gacha_data", "banner_data")),
    "story_event": ("Story events", ("story_event_data",)),
    "legend_race": ("Legend Races", ("legend_race",)),
    "training_challenge": ("Trainer Aptitude Test", ("training_challenge_master",)),
    "factor_research": ("Agnes Tachyon Research", ("factor_research_data",)),
    "transfer": ("Transfer events", ("transfer_event_data", "transfer_event_detail")),
    "home": ("Home theme", ("home_event_schedule", "home_poster_data")),
    "shops": ("Event shops", ("item_exchange_top", "item_exchange")),
}
STORY_PREFIX = "story_event_"
DATE_COLUMNS = ("notice_date", "pre_start_date", "start_date", "middle_date_01", "middle_date_02",
                "ending_date", "end_date", "start_result_date", "end_result_date")
PERMANENT = 1893456000
TEXT_FORMAT = "%Y/%m/%d %H:%M:%S"
DAY = 86400


def _tz():
    from . import region
    return dt.timezone(dt.timedelta(hours=9)) if region.CURRENT_REGION.get() == "jp" else dt.timezone.utc


def _stamp(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        try:
            return int(dt.datetime.strptime(value, TEXT_FORMAT).replace(tzinfo=_tz()).timestamp())
        except ValueError:
            return None
    return None


def _shift_cell(value, delta):
    stamp = _stamp(value)
    if not stamp or stamp >= PERMANENT:
        return value
    if isinstance(value, int):
        return stamp + delta
    return dt.datetime.fromtimestamp(stamp + delta, _tz()).strftime(TEXT_FORMAT)


def _settings_db(write=False):
    from . import custom_support as C
    return C._db(write)


def setting() -> dict | None:
    c = _settings_db()
    if c is None:
        return None
    try:
        r = c.execute("SELECT value FROM settings WHERE key='date_sync'").fetchone()
        return json.loads(r[0]) if r and r[0] else None
    finally:
        c.close()


def earliest_day(connection) -> dt.date:
    r = connection.execute("SELECT MIN(start_date) FROM gacha_data WHERE start_date>0 AND end_date<?",
                           (PERMANENT,)).fetchone()
    return dt.datetime.fromtimestamp(r[0] or 0, dt.timezone.utc).date()


def validate(day, now: int, connection, groups=None) -> dict | None:
    if day in (None, ""):
        return None
    try:
        picked = dt.date.fromisoformat(str(day))
    except ValueError as exc:
        raise ValueError("Pick a day as YYYY-MM-DD") from exc
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    first = earliest_day(connection)
    if not first <= picked < today:
        raise ValueError(f"Pick a day from {first} to {today - dt.timedelta(days=1)}")
    value = {"day": picked.isoformat(), "delta": (today - picked).days * DAY, "set_at": now}
    if groups is not None:
        groups = sorted({str(g) for g in groups})
        if not groups or any(g not in GROUPS for g in groups):
            raise ValueError("Pick at least one known event type to bring over")
        if len(groups) < len(GROUPS):
            value["groups"] = groups
    return value


def set_day(day, groups=None) -> dict | None:
    from . import custom_support as C, master_data as M, patch
    value = validate(day, patch._servertime(), M.get_connection(), groups)
    with C._writing() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('date_sync',?)", (json.dumps(value) if value else "",))
        C._audit(c, "date_sync", detail={"sync": value})
    return value


def _tables(connection, group):
    names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    tables = [t for t in GROUPS[group][1] if t in names]
    if group == "story_event":
        tables += sorted(t for t in names if t.startswith(STORY_PREFIX) and t not in tables)
    return tables


def apply(connection, sync):
    if not sync:
        return
    delta = int(sync["delta"])
    for group in sync.get("groups") or GROUPS:
        for table in _tables(connection, group):
            cols = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
            dates = [k for k in DATE_COLUMNS if k in cols]
            if "end_date" not in dates:
                continue
            selected = ",".join(f'"{k}"' for k in dates)
            rows = connection.execute(f'SELECT rowid, {selected} FROM "{table}"').fetchall()
            for row in map(tuple, rows):
                end = _stamp(row[dates.index("end_date") + 1])
                if not end or end >= PERMANENT:
                    continue
                connection.execute(f'UPDATE "{table}" SET ' + ",".join(f'"{k}"=?' for k in dates) + " WHERE rowid=?",
                                   (*[_shift_cell(v, delta) for v in row[1:]], row[0]))


def pack_sync() -> dict | None:
    from . import custom_support as C
    digest = C.CURRENT_PACK.get()
    if not digest:
        return None
    return (C.manifest(digest) or {}).get("sync")


_KEYS = {"gacha_data": "id", "story_event_data": "story_event_id", "legend_race": "id",
         "training_challenge_master": "id", "factor_research_data": "factor_research_event_id",
         "transfer_event_data": "transfer_event_id", "home_event_schedule": "event_id"}
_SHOWN = {"banners": "gacha_data", "story_event": "story_event_data", "legend_race": "legend_race",
          "training_challenge": "training_challenge_master", "factor_research": "factor_research_data",
          "transfer": "transfer_event_data", "home": "home_event_schedule"}


def _text(connection, category, index):
    r = connection.execute('SELECT text FROM text_data WHERE category=? AND "index"=?', (category, index)).fetchone()
    return r[0] if r else ""


def _label(connection, table, row):
    key = row[_KEYS[table]]
    if table == "gacha_data":
        trainee = row["card_type"] == 1
        top = connection.execute("SELECT MAX(rarity) FROM gacha_available WHERE gacha_id=? AND is_pickup=1", (key,)).fetchone()[0]
        cards = [r[0] for r in connection.execute(
            "SELECT card_id FROM gacha_available WHERE gacha_id=? AND is_pickup=1 AND rarity=? ORDER BY recommend_order, card_id",
            (key, top))] if top else []
        names = []
        for cid in cards:
            if trainee:
                chara = _text(connection, 6, cid // 100) or str(cid)
                names.append(chara if cid % 100 == 1 else f"{chara} Alt.")
            else:
                names.append(_text(connection, 77, cid) or str(cid))
        kind = "Uma banner" if trainee else "Support banner"
        if not names:
            return f"{kind} {key}"
        shown = " + ".join(names[:2]) + (f" + {len(names) - 2} more" if len(names) > 2 else "")
        return f"{kind}: {shown}"
    if table == "story_event_data":
        return _text(connection, 189, key) or f"Story event {key}"
    if table == "legend_race":
        race = _text(connection, 29, row["race_instance_id"]) or _text(connection, 28, row["race_instance_id"])
        boss = connection.execute("SELECT chara_id FROM legend_race_boss_npc WHERE id=?",
                                  (row["legend_race_boss_npc_id"],)).fetchone()
        who = _text(connection, 6, boss[0]) if boss else ""
        return "Legend Race" + (f": vs {who}" if who else f" {key}") + (f" · {race}" if race else "")
    return {"training_challenge_master": "Trainer Aptitude Test", "factor_research_data": "Tachyon research",
            "transfer_event_data": "Transfer event", "home_event_schedule": "Home screen theme"}[table] + f" #{key}"


def _rows(connection, table):
    try:
        connection.row_factory = sqlite3.Row
        return connection.execute(f'SELECT * FROM "{table}"').fetchall()
    except sqlite3.OperationalError:
        return []


def live(connection, now: int) -> dict:
    out = {}
    for group, table in _SHOWN.items():
        items = []
        for row in _rows(connection, table):
            start, end = _stamp(row["start_date"]) or 0, _stamp(row["end_date"]) or 0
            if 0 < start <= now <= end and end < PERMANENT:
                items.append({"key": row[_KEYS[table]], "label": _label(connection, table, row),
                              "start": start, "end": end})
        out[group] = sorted(items, key=lambda i: (i["start"], i["key"]))
    return out


def upcoming(connection, now: int) -> list:
    out = []
    for group, table in _SHOWN.items():
        for row in _rows(connection, table):
            start, end = _stamp(row["start_date"]) or 0, _stamp(row["end_date"]) or 0
            if start > now and end < PERMANENT:
                out.append({"group": group, "key": row[_KEYS[table]], "label": _label(connection, table, row),
                            "start": start, "end": end})
    return sorted(out, key=lambda i: (i["start"], i["group"], i["key"]))


def timeline(connection) -> list:
    out = []
    for group, table in _SHOWN.items():
        for row in _rows(connection, table):
            start, end = _stamp(row["start_date"]) or 0, _stamp(row["end_date"]) or 0
            if 0 < start < end < PERMANENT:
                out.append({"group": group, "key": row[_KEYS[table]], "label": _label(connection, table, row),
                            "start": start, "end": end})
    return sorted(out, key=lambda i: (i["start"], i["group"], i["key"]))
