from app import champions_meeting as CM, champions_pool as POOL, custom_support as C, master_data as M
from . import op
from .common import OpError, future_bundles, load, save, text


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ValueError as e:
        raise OpError(str(e)) from e


@op("champions.status")
def status(viewer, args):
    cms = []
    for r in M.query("SELECT id, resource_id, start_date FROM champions_schedule ORDER BY id DESC"):
        ri = M.query_one("SELECT race_instance_id FROM champions_race_condition WHERE champions_id=? "
                         "ORDER BY round_id LIMIT 1", (r["id"],))
        cms.append({"id": r["id"], "start_date": r["start_date"],
                    "race_instance_id": ri["race_instance_id"] if ri else 0,
                    "name": text(206, r["id"]) or f"CM {r['id']}"})
    have = {c["id"] for c in cms}
    for b in future_bundles():
        t = b.get("tables") or {}
        for r in t.get("champions_schedule") or []:
            if r["id"] in have:
                continue
            ri = min((x for x in t.get("champions_race_condition") or [] if x["champions_id"] == r["id"]),
                     key=lambda x: x["round_id"], default=None)
            cms.append({"id": r["id"], "start_date": r["start_date"], "ported": True,
                        "race_instance_id": ri["race_instance_id"] if ri else 0,
                        "name": text(206, r["id"]) or f"CM {r['id']}"})
    cms.sort(key=lambda c: -c["id"])
    s = CM.status()
    s["stages"] = [list(x) for x in CM.STAGES]
    s["cms"] = cms
    s["snapshots"] = POOL.snapshots()
    st = (load(viewer).get("champions_state") or {}) if viewer else {}
    s["viewer"] = {k: st.get(k) for k in ("league", "cum_wins", "rounds", "final")} if st else None
    if s["viewer"] and s["viewer"].get("final"):
        s["viewer"]["final"] = {k: v for k, v in s["viewer"]["final"].items() if k not in ("race", "team")}
    if s["viewer"] and s["viewer"].get("rounds"):
        s["viewer"]["rounds"] = {k: {"round_id": r["round_id"], "best": r["best"], "claimed": r["claimed"],
                                     "entries": [e["wins"] for e in r["entries"]]}
                                 for k, r in s["viewer"]["rounds"].items()}
    return s


@op("champions.start", label="Champions Meeting: serve CM {champions_id}")
def start(viewer, args):
    run = _guard(CM.start, int(args["champions_id"]), int(args.get("stage_len") or 3600),
                 hold=bool(args.get("hold", True)))
    return {"run": run}


@op("champions.goto", label="Champions Meeting: go to {stage}")
def goto(viewer, args):
    return _guard(CM.goto, args["stage"])


@op("champions.advance", label="Champions Meeting: next stage")
def advance(viewer, args):
    return _guard(CM.advance)


@op("champions.hold", label="Champions Meeting: hold {hold}")
def hold(viewer, args):
    return _guard(CM.set_hold, bool(args.get("hold")))


@op("champions.stop", label="Champions Meeting: stop")
def stop(viewer, args):
    return _guard(CM.stop)


@op("champions.pool", label="Champions Meeting: opponent pool")
def pool(viewer, args):
    change = {k: args[k] for k in ("bands", "snapshot", "final_win_pressure", "mood", "pinned") if k in args}
    for kind, band in (change.get("bands") or {}).items():
        lo, hi = float(band[0]), float(band[1])
        if kind not in CM.DEFAULT_POOL["bands"] or not 0 <= lo <= hi <= 100:
            raise OpError(f"Bad band {kind}")
        change["bands"][kind] = [lo, hi]
    return CM.set_pool(change)


@op("champions.options", label="Champions Meeting: options")
def options(viewer, args):
    return CM.set_options(unlimited_entries=args.get("unlimited_entries"), any_team_rank=args.get("any_team_rank"))


@op("champions.teams")
def teams(viewer, args):
    run = CM.active()
    snap = POOL.resolve_snapshot(run["champions_id"] if run else 0, args.get("snapshot") or "auto")
    if not snap:
        raise OpError("No opponent corpus (captures/cm_teams)")
    lo, hi = float(args.get("min", 0)), float(args.get("max", 100))
    rows = sorted((t for t in POOL.teams(snap) if lo <= t["_percentile"] <= hi), key=lambda t: -t["_percentile"])
    limit = int(args.get("limit") or 50)
    return {"snapshot": snap, "count": len(rows), "teams": [
        {"id": t["id"], "percentile": t["_percentile"], "trainer": POOL.trainer_name(t),
         "members": [{"card_id": int(m["card"]), "name": text(4, int(m["card"])), "stats": m["stats"],
                      "style": m.get("racingStyle")} for m in t["members"]]}
        for t in rows[:limit]]}


@op("champions.reset_viewer", label="Champions Meeting: reset this trainer's run")
def reset_viewer(viewer, args):
    full = load(viewer)
    full.pop("champions_state", None)
    save(viewer, full)
    return {"ok": True}
