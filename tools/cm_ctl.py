#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import sys


def _onoff(v):
    if v is None:
        return None
    if v.lower() in ("on", "1", "true", "yes"):
        return True
    if v.lower() in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("on or off")


def _print_status(s):
    if not s["running"]:
        print("No Champions Meeting running.")
    else:
        run = s["run"]
        print(f"CM {run['champions_id']}  run {run['run_id']}  stage {s['stage']}  "
              f"(ends in {s['stage_ends_in']}s, hold {'on' if run.get('hold', True) else 'off'})")
        for name, (a, b) in s["windows"].items():
            mark = "->" if name == s["stage"] else "  "
            print(f"  {mark} {name:<13} {a} .. {b}")
    print(f"served time {s['served_time']}  real {s['real_time']}  clock {json.dumps(s['clock'])}")
    print("pool " + json.dumps(s["pool"]))
    print("options " + json.dumps(s.get("options") or {}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cmd = parser.add_subparsers(dest="command", required=True)
    cmd.add_parser("list")
    cmd.add_parser("status")
    p = cmd.add_parser("start")
    p.add_argument("champions_id", type=int)
    p.add_argument("--stage-len", type=int, default=3600, help="seconds per stage window (min 600)")
    p.add_argument("--no-hold", action="store_true", help="let stages expire on their own")
    cmd.add_parser("advance")
    p = cmd.add_parser("goto")
    p.add_argument("stage")
    p = cmd.add_parser("hold")
    p.add_argument("value", type=_onoff)
    p = cmd.add_parser("pool")
    p.add_argument("--snapshot", help="corpus snapshot, or auto")
    p.add_argument("--band", action="append", default=[], metavar="KIND=LO,HI",
                   help="round1, round2_a, round2_b, final_a, final_b; percentiles 0-100 (100 = strongest)")
    p.add_argument("--pressure", type=float, help="final_win_pressure 0-1")
    p.add_argument("--mood", help="random or 1-5 (qualifiers; finals are always 5)")
    p.add_argument("--pin", nargs="*", help="team ids that go first when inside the band")
    p.add_argument("--unpin", action="store_true")
    p.add_argument("--reset", action="store_true", help="back to the default pool")
    p = cmd.add_parser("teams")
    p.add_argument("--snapshot", default="auto")
    p.add_argument("--min", type=float, default=0)
    p.add_argument("--max", type=float, default=100)
    p.add_argument("--limit", type=int, default=20)
    p = cmd.add_parser("options")
    p.add_argument("--unlimited-entries", type=_onoff)
    p.add_argument("--any-team-rank", type=_onoff)
    cmd.add_parser("stop")
    args = parser.parse_args()

    os.environ["UMA_REGION"] = "global"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
    from app import champions_meeting as CM, champions_pool as POOL, master_data as M

    try:
        if args.command == "list":
            for r in M.query("SELECT id, resource_id, start_date, end_date FROM champions_schedule ORDER BY id"):
                ri = M.query_one("SELECT race_instance_id FROM champions_race_condition WHERE champions_id=? "
                                 "ORDER BY round_id LIMIT 1", (r["id"],))
                print(f"CM {r['id']:>3}  resource {r['resource_id']:>3}  race {ri['race_instance_id'] if ri else '-'}"
                      f"  master {r['start_date']} .. {r['end_date']}")
            print("\nopponent snapshots:")
            for s in POOL.snapshots():
                print(f"  {s['snapshot']}  cm{s['cm']}  {s['teams']} teams  {s.get('course') or ''}")
        elif args.command == "status":
            _print_status(CM.status())
        elif args.command == "start":
            run = CM.start(args.champions_id, args.stage_len, hold=not args.no_hold)
            print(f"CM {run['champions_id']} published (pack {run['digest'][:12]}). Restart the game.")
            _print_status(CM.status())
        elif args.command == "advance":
            _print_status(CM.advance())
        elif args.command == "goto":
            _print_status(CM.goto(args.stage))
        elif args.command == "hold":
            _print_status(CM.set_hold(args.value))
        elif args.command == "pool":
            if args.reset:
                data = dict(CM.load())
                data.pop("pool", None)
                CM._save(data)
            change = {}
            if args.snapshot:
                change["snapshot"] = args.snapshot
            bands = {}
            for b in args.band:
                kind, _, rng = b.partition("=")
                lo, hi = (float(x) for x in rng.split(","))
                if kind not in CM.DEFAULT_POOL["bands"] or not 0 <= lo <= hi <= 100:
                    parser.error(f"bad band {b}")
                bands[kind] = [lo, hi]
            if bands:
                change["bands"] = bands
            if args.pressure is not None:
                change["final_win_pressure"] = max(0.0, min(1.0, args.pressure))
            if args.mood:
                change["mood"] = args.mood
            if args.pin is not None:
                change["pinned"] = args.pin
            if args.unpin:
                change["pinned"] = []
            print(json.dumps(CM.set_pool(change), indent=2))
        elif args.command == "teams":
            run = CM.active()
            snap = POOL.resolve_snapshot(run["champions_id"] if run else 0, args.snapshot)
            if not snap:
                parser.error("no corpus snapshot")
            rows = [t for t in POOL.teams(snap) if args.min <= t["_percentile"] <= args.max]
            rows.sort(key=lambda t: -t["_percentile"])
            print(f"{snap}: {len(rows)} teams in [{args.min}, {args.max}]")
            for t in rows[:args.limit]:
                names = ", ".join(str(m["card"]) for m in t["members"])
                print(f"  {t['id']}  p{t['_percentile']:.1f}  {POOL.trainer_name(t)[:20]:<20}  cards {names}")
        elif args.command == "options":
            print(json.dumps(CM.set_options(unlimited_entries=args.unlimited_entries,
                                            any_team_rank=args.any_team_rank)))
        elif args.command == "stop":
            _print_status(CM.stop())
            print("Stopped. Restart the game to drop the CM from its schedule.")
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
