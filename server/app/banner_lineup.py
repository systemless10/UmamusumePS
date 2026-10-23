from __future__ import annotations

import json

from .date_sync import PERMANENT, _stamp


def setting() -> dict | None:
    from . import custom_support as C
    c = C._db()
    if c is None:
        return None
    try:
        r = c.execute("SELECT value FROM settings WHERE key='banner_lineup'").fetchone()
        return json.loads(r[0]) if r and r[0] else None
    finally:
        c.close()


def validate(value, connection) -> dict | None:
    if not value or not (value.get("banners") or value.get("only")):
        return None
    out, seen = [], set()
    for b in value.get("banners") or []:
        gid, start, end = (b.get(k) for k in ("gacha_id", "start_date", "end_date"))
        if any(type(v) is not int for v in (gid, start, end)) or not 0 < start < end < 2**32:
            raise ValueError("Each banner needs a start before its end")
        if gid in seen:
            raise ValueError(f"Banner {gid} is listed twice")
        if connection.execute("SELECT 1 FROM gacha_data WHERE id=?", (gid,)).fetchone() is None:
            raise ValueError(f"Banner {gid} is absent from this client's master")
        seen.add(gid)
        out.append({"gacha_id": gid, "start_date": start, "end_date": end})
    return {"only": bool(value.get("only")), "banners": sorted(out, key=lambda b: (b["start_date"], b["gacha_id"]))}


def set_lineup(value) -> dict | None:
    from . import custom_support as C, master_data as M
    value = validate(value, M.get_connection())
    with C._writing() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('banner_lineup',?)", (json.dumps(value) if value else "",))
        C._audit(c, "banner_lineup", detail={"lineup": value})
    return value


def apply(connection, lineup) -> None:
    if not lineup:
        return
    picked = {b["gacha_id"]: b for b in lineup["banners"]}
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if lineup["only"]:
        for gid, end in connection.execute("SELECT id, end_date FROM gacha_data").fetchall():
            end = _stamp(end)
            if gid not in picked and end and end < PERMANENT:
                connection.execute("UPDATE gacha_data SET start_date=1, end_date=1 WHERE id=?", (gid,))
                if "gacha_free_campaign" in tables:
                    connection.execute("UPDATE gacha_free_campaign SET start_date=1, end_date=1 WHERE gacha_id=?", (gid,))
    for gid, b in picked.items():
        connection.execute("UPDATE gacha_data SET start_date=?, end_date=? WHERE id=?", (b["start_date"], b["end_date"], gid))
        if "gacha_free_campaign" in tables:
            old = connection.execute("SELECT MIN(start_date) FROM gacha_free_campaign WHERE gacha_id=?", (gid,)).fetchone()[0]
            for cid, s, e in connection.execute("SELECT id, start_date, end_date FROM gacha_free_campaign WHERE gacha_id=?", (gid,)).fetchall():
                d = b["start_date"] - (old or s)
                connection.execute("UPDATE gacha_free_campaign SET start_date=?, end_date=? WHERE id=?",
                                   (max(b["start_date"], s + d), min(b["end_date"], e + d), cid))


def pack_lineup() -> dict | None:
    from . import custom_support as C
    digest = C.CURRENT_PACK.get()
    return (C.manifest(digest) or {}).get("banners") if digest else None
