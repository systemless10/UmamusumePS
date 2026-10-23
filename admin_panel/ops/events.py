from app import custom_support as C, event_schedule as E, master_data as M
from . import op
from .common import OpError, future_bundles

_NAME_CATEGORY = {"story_event": 189}


def _names(category):
    return {r["index"]: r["text"] for r in M.query(
        'SELECT "index", text FROM text_data WHERE category=?', (category,))}


@op("events.list")
def listing(viewer, args):
    manifest = C.manifest() or {}
    families = {}
    for family, (table, key) in E.FAMILIES.items():
        if not M.has_table(table):
            continue
        names = _names(_NAME_CATEGORY[family]) if family in _NAME_CATEGORY else {}
        with C.use_pack(""):
            rows = M.query(f'SELECT * FROM "{table}" ORDER BY "{key}"')
        rows = [dict(r) for r in rows]
        have = {r[key] for r in rows}
        rows += sorted((r for b in future_bundles() for r in (b.get("tables") or {}).get(table) or []
                        if r[key] not in have), key=lambda r: r[key])
        if family == "legend_race":
            bosses = {r["id"]: r["chara_id"] for r in M.query("SELECT id, chara_id FROM legend_race_boss_npc")}
            bosses.update({r["id"]: r["chara_id"] for b in future_bundles()
                           for r in (b.get("tables") or {}).get("legend_race_boss_npc") or []})
            charas = _names(6)
            names = {r["id"]: "vs " + charas.get(bosses.get(r["legend_race_boss_npc_id"]), "?") for r in rows}
        families[family] = [{"id": r[key], "name": names.get(r[key], ""),
                             "start_date": r["start_date"], "end_date": r["end_date"]} for r in rows]
    return {"families": families, "schedules": E.schedules(),
            "published": manifest.get("events", []), "digest": manifest.get("digest", ""),
            "enabled": C.is_enabled()}


@op("events.set")
def set_schedule(viewer, args):
    family = args.get("family")
    if family not in E.FAMILIES:
        raise OpError("Unknown event family")
    entries = [e for e in E.schedules() if e["family"] != family]
    if args.get("event_id"):
        entries.append({"family": family, "event_id": int(args["event_id"]),
                        "start_date": int(args.get("start_date", 0)),
                        "end_date": int(args.get("end_date", 0))})
    try:
        E.set_schedules(entries)
        from .sync import pack_needed
        if not pack_needed():
            C.set_enabled(False)
            return {"digest": "", "events": []}
        m = C.publish()
        C.set_enabled(True)
        return {"digest": m["digest"], "events": m["events"]}
    except ValueError as e:
        raise OpError(str(e)) from e
