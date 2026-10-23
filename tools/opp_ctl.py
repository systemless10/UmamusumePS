#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(arg: str) -> dict:
    key, _, rest = arg.partition("=")
    weight, _, band = (rest or "1").partition("@")
    out = {"pool": key, "weight": float(weight or 1)}
    if band:
        out["band"] = [float(x) for x in band.split("-")] if "-" in band else band
    return out


def _ref(arg: str) -> dict:
    key, _, tid = arg.rpartition("/")
    if not key or not tid:
        raise SystemExit(f"team ref is POOL/ID, not {arg!r}")
    return {"pool": key, "id": tid}


def _team_line(OS, M, t) -> str:
    names = []
    for m in t["members"]:
        r = M.query_one("SELECT text FROM text_data WHERE category=4 AND [index]=?", (int(m["card"]),))
        style = {1: "front", 2: "pace", 3: "late", 4: "end", 5: "runaway"}.get(int(m.get("racingStyle") or 0), "?")
        role = " DEB" if int(m.get("style") or 0) == 6 else ""
        names.append(f"{(r['text'] if r else m['card'])} {style}{role} {'/'.join(str(x) for x in m['stats'])}")
    return f"{t.get('_percentile', 0):6.2f}  {t['id'][:16]}  " + "  |  ".join(names)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cmd = ap.add_subparsers(dest="command", required=True)
    cmd.add_parser("pools")
    p = cmd.add_parser("teams")
    p.add_argument("pool")
    p.add_argument("--min", type=float, default=0)
    p.add_argument("--max", type=float, default=100)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true", help="print whole team ids")
    p = cmd.add_parser("gen")
    p.add_argument("spec", help="spec file, or the JSON itself when it starts with {")
    p.add_argument("--pack", default="", help="pack digest (default: the active one)")
    p = cmd.add_parser("mix")
    p.add_argument("mode", choices=("cm", "loh"))
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--pin", action="append", default=[])
    p.add_argument("--reset", action="store_true")
    p = cmd.add_parser("list")
    p.add_argument("name")
    p.add_argument("--add", action="append", default=[])
    p.add_argument("--drop", action="append", default=[])
    p.add_argument("--delete", action="store_true")
    a = ap.parse_args()

    os.environ.setdefault("UMA_REGION", "global")
    sys.path.insert(0, str(ROOT / "server"))
    from app import custom_support as C, master_data as M, opponent_sources as OS

    if a.command == "pools":
        for e in OS.catalog():
            extra = e.get("course") or ""
            if e.get("references"):
                extra += "  refs " + ", ".join(e["references"])
            print(f"  {e['pool']:<34} {e['kind']:<10} {str(e.get('teams') or ''):>6}  {extra}")
    elif a.command == "teams":
        with C.use_pack((C.manifest() or {}).get("digest", "")):
            rows = sorted((t for t in OS.teams(a.pool, {}) if a.min <= t.get("_percentile", 50) <= a.max),
                          key=lambda t: -t.get("_percentile", 50))
            print(f"{a.pool}: {len(rows)} teams in [{a.min}, {a.max}]")
            for t in rows[:a.limit]:
                line = _team_line(OS, M, t)
                print(line.replace(t["id"][:16], t["id"]) if a.full else line)
    elif a.command == "gen":
        from app import opponent_gen as G
        spec = json.loads(a.spec if a.spec.lstrip().startswith("{") else Path(a.spec).read_text())
        with C.use_pack(a.pack or (C.manifest() or {}).get("digest", "")):
            meta = G.generate(spec)
        meta.pop("_teams", None)
        meta.pop("spec", None)
        print(json.dumps(meta, indent=1))
        print(f"pool gen:{meta['name']} written to {OS.gen_dir()}")
    elif a.command == "mix":
        if a.reset:
            print(json.dumps(OS.set_mix(a.mode, None), indent=1))
        elif a.source or a.pin:
            cur = OS.mix(a.mode)
            new = {"sources": [_source(s) for s in a.source] or cur["sources"],
                   "pinned": [_ref(r) for r in a.pin] if a.pin else cur.get("pinned") or []}
            print(json.dumps(OS.set_mix(a.mode, new), indent=1))
        else:
            print(json.dumps(OS.mix(a.mode), indent=1))
    elif a.command == "list":
        if a.delete:
            OS.set_list(a.name, None)
            print(f"list {a.name} deleted")
        else:
            refs = list(OS.lists().get(a.name) or [])
            drop = {(r["pool"], r["id"]) for r in map(_ref, a.drop)}
            refs = [r for r in refs if (r["pool"], r["id"]) not in drop] + [_ref(x) for x in a.add]
            with C.use_pack((C.manifest() or {}).get("digest", "")):
                print(json.dumps(OS.set_list(a.name, refs), indent=1))


if __name__ == "__main__":
    main()
