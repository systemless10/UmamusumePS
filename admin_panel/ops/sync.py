import time

from app import banner_lineup as B, custom_support as C, date_sync as D, event_schedule as E, master_data as M, patch
from . import op
from .common import OpError


def pack_needed() -> bool:
    return bool(D.setting() or B.setting() or E.schedules() or C.unreleased_bundles()
                or any(not d["retired"] for d in C.status()["cards"]))


def _view(digest, now):
    with C.use_pack(digest):
        return D.live(C.connection() or M.get_connection(), now)


@op("sync.get")
def get(viewer, args):
    real, served = int(time.time()), patch._servertime()
    manifest = (C.manifest() or {}) if C.is_enabled() else {}
    digest = manifest.get("digest", "")
    stored, live_sync = D.setting(), manifest.get("sync")
    official, server = _view("", real), _view(digest, served)
    with C.use_pack(""):
        coming = D.upcoming(M.get_connection(), real)
        first = D.earliest_day(M.get_connection())
        launch = int(time.mktime(first.timetuple())) if first else 0
        timeline = [i for i in D.timeline(M.get_connection()) if i["end"] > launch]
    return {"groups": {g: label for g, (label, _) in D.GROUPS.items() if g in official},
            "all_groups": {g: label for g, (label, _) in D.GROUPS.items()}, "timeline": timeline,
            "official": official, "server": server, "coming": coming,
            "sync": stored, "published": live_sync, "digest": digest, "enabled": C.is_enabled(),
            "real_time": real, "server_time": served,
            "game_time": served - (live_sync or {}).get("delta", 0),
            "first_day": first.isoformat(), "reruns": sorted(E.FAMILIES),
            "lineup": B.setting(), "lineup_live": manifest.get("banners")}


@op("sync.banners")
def set_banners(viewer, args):
    try:
        B.set_lineup({"only": bool(args.get("only")), "banners": args.get("banners") or []})
        if not pack_needed():
            C.set_enabled(False)
            return {"digest": ""}
        m = C.publish()
        C.set_enabled(True)
        return {"digest": m["digest"]}
    except ValueError as e:
        raise OpError(str(e)) from e


@op("sync.set")
def set_day(viewer, args):
    try:
        sync = D.set_day(args.get("day"), args.get("groups"))
        if args.get("reruns") and not sync:
            E.set_schedules([])
        stop = set(args.get("stop_families") or ())
        if stop:
            E.set_schedules([e for e in E.schedules() if e["family"] not in stop])
        if not pack_needed():
            C.set_enabled(False)
            return {"sync": None, "digest": ""}
        m = C.publish()
        C.set_enabled(True)
        return {"sync": m.get("sync"), "digest": m["digest"]}
    except ValueError as e:
        raise OpError(str(e)) from e
