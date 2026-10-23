from __future__ import annotations

import gzip
import json
import random
import threading
from functools import lru_cache
from pathlib import Path

from . import champions_pool as POOL
from . import region

MODES = ("cm", "loh")
LOH_DIR = Path(__file__).resolve().parents[1] / "data" / "heroes_opponents"

_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "data": None}


def _path() -> Path:
    return region.data_dir() / "opponent_mix.json"


def gen_dir() -> Path:
    return region.data_dir() / "opponent_pools"


def load() -> dict:
    p = _path()
    try:
        mtime = p.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    with _lock:
        if _cache["path"] == str(p) and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            data = {}
        _cache.update(path=str(p), mtime=mtime, data=data)
        return data


def _save(data: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)


def default_mix(mode: str) -> dict:
    if mode == "loh":
        return {"sources": [{"pool": "loh:auto", "weight": 1, "band": "round"},
                            {"pool": "real:auto", "weight": 0, "band": "round"}], "pinned": []}
    from . import champions_meeting as CM
    cfg = CM.pool_config()
    snap = cfg.get("snapshot") or "auto"
    return {"sources": [{"pool": f"real:{snap}", "weight": 1, "band": "round"}],
            "pinned": [{"pool": f"real:{snap}", "id": t} for t in cfg.get("pinned") or ()]}


def mix(mode: str) -> dict:
    m = (load().get("mix") or {}).get(mode)
    return json.loads(json.dumps(m)) if m and m.get("sources") else default_mix(mode)


def set_mix(mode: str, new: dict | None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    data = dict(load())
    mixes = dict(data.get("mix") or {})
    if new is None:
        mixes.pop(mode, None)
    else:
        sources = []
        for s in new.get("sources") or []:
            key = str(s["pool"])
            _check_key(key)
            band = s.get("band", "any" if key.startswith("list:") else "round")
            if isinstance(band, list):
                lo, hi = float(band[0]), float(band[1])
                if not 0 <= lo <= hi <= 100:
                    raise ValueError(f"bad band {band}")
                band = [lo, hi]
            elif band not in ("round", "any"):
                raise ValueError(f"band must be 'round', 'any' or [lo, hi], not {band!r}")
            w = float(s.get("weight", 1))
            if w < 0:
                raise ValueError("weight must be >= 0")
            sources.append({"pool": key, "weight": w, "band": band})
        if not sources:
            raise ValueError("a mix needs at least one source")
        pinned = [{"pool": str(p["pool"]), "id": str(p["id"])} for p in new.get("pinned") or []]
        mixes[mode] = {"sources": sources, "pinned": pinned}
    data["mix"] = mixes
    _save(data)
    return mix(mode)


def lists() -> dict:
    return dict(load().get("lists") or {})


def set_list(name: str, refs: list | None) -> list:
    if not name or ":" in name:
        raise ValueError("bad list name")
    data = dict(load())
    ls = dict(data.get("lists") or {})
    if refs is None:
        ls.pop(name, None)
    else:
        out = []
        for r in refs:
            key, tid = str(r["pool"]), str(r["id"])
            if key.startswith("list:"):
                raise ValueError("a list cannot hold another list")
            if team(key, tid, context={}) is None:
                raise ValueError(f"no team {tid} in {key}")
            out.append({"pool": key, "id": tid})
        ls[name] = out
    data["lists"] = ls
    _save(data)
    return ls.get(name, [])


def _check_key(key: str) -> None:
    kind, _, name = key.partition(":")
    if kind not in ("real", "gen", "loh", "list") or not name:
        raise ValueError(f"bad pool key {key!r} (real:/gen:/loh:/list:)")
    if kind == "gen" and not (gen_dir() / f"{name}.jsonl.gz").exists():
        raise ValueError(f"no generated set {name} (data/opponent_pools)")
    if kind == "real" and name != "auto" and not any(s["snapshot"] == name for s in POOL.snapshots()):
        raise ValueError(f"no CM team snapshot {name}")
    if kind == "loh" and name != "auto" and not (LOH_DIR / f"heroes_{name}.jsonl.gz").exists():
        raise ValueError(f"no LoH sheet set {name}")
    if kind == "list" and name not in lists():
        raise ValueError(f"no team list {name}")


def resolve(key: str, context: dict) -> str | None:
    kind, _, name = key.partition(":")
    if name != "auto":
        return key
    if kind == "real":
        snap = POOL.resolve_snapshot(int(context.get("champions_id") or 0), "auto")
        return f"real:{snap}" if snap else None
    if kind == "loh":
        hid = context.get("heroes_id")
        return f"loh:{hid}" if hid and (LOH_DIR / f"heroes_{hid}.jsonl.gz").exists() else None
    return None


@lru_cache(maxsize=16)
def _file_pool(path: str, mtime: int) -> tuple:
    with gzip.open(path, "rt") as fh:
        return tuple(json.loads(line) for line in fh)


def teams(key: str, context: dict | None = None) -> tuple:
    key = resolve(key, context or {})
    if not key:
        return ()
    kind, _, name = key.partition(":")
    if kind == "real":
        rows = POOL.teams(name)
    elif kind in ("gen", "loh"):
        p = gen_dir() / f"{name}.jsonl.gz" if kind == "gen" else LOH_DIR / f"heroes_{name}.jsonl.gz"
        if not p.exists():
            return ()
        rows = _file_pool(str(p), p.stat().st_mtime_ns)
    elif kind == "list":
        out = []
        for r in lists().get(name) or []:
            t = team(r["pool"], r["id"], context)
            if t:
                out.append(t)
        return tuple(out)
    else:
        return ()
    if rows and rows[0].get("_pool") == key:
        return rows
    for t in rows:
        t["_pool"] = key
    return rows


@lru_cache(maxsize=64)
def _index(key: str, n: int) -> dict:
    return {t["id"]: t for t in teams(key)}


def team(key: str, tid: str, context: dict | None = None) -> dict | None:
    key = resolve(key, context or {}) if key else None
    if not key:
        return None
    if key.startswith("list:"):
        return next((t for t in teams(key, context) if t["id"] == tid), None)
    rows = teams(key)
    return _index(key, len(rows)).get(tid)


def catalog() -> list[dict]:
    out = []
    for s in POOL.snapshots():
        out.append({"pool": f"real:{s['snapshot']}", "kind": "real", "teams": s.get("teams"),
                    "course": s.get("course"), "cm": s["cm"]})
    for p in sorted(gen_dir().glob("*.jsonl.gz")):
        name = p.name[:-len(".jsonl.gz")]
        meta = {}
        try:
            meta = json.loads((gen_dir() / f"{name}.json").read_text())
        except (OSError, ValueError):
            pass
        out.append({"pool": f"gen:{name}", "kind": "generated", "teams": meta.get("teams"),
                    "course": (meta.get("course") or {}).get("label"), "references": meta.get("references"),
                    "spec": meta.get("spec")})
    for p in sorted(LOH_DIR.glob("heroes_*.jsonl.gz")):
        hid = p.name[len("heroes_"):-len(".jsonl.gz")]
        out.append({"pool": f"loh:{hid}", "kind": "loh_sheet", "generated": True, "course": f"LoH {hid}"})
    for name, refs in lists().items():
        out.append({"pool": f"list:{name}", "kind": "list", "teams": len(refs)})
    return out


def _in(t, band) -> bool:
    return band[0] <= float(t.get("_percentile", 50)) <= band[1]


def pick(mode: str, band, rng: random.Random, n: int, context: dict, exclude=()) -> list[dict]:
    m = mix(mode)
    lo, hi = float(band[0]), float(band[1])
    chosen, seen = [], set(exclude)

    def ident(t):
        return (t.get("_pool"), t["id"])

    for ref in m.get("pinned") or ():
        t = team(ref["pool"], ref["id"], context)
        if t and ident(t) not in seen and _in(t, (lo, hi)) and len(chosen) < n:
            chosen.append(t)
            seen.add(ident(t))

    srcs, fallback = [], []
    for s in m["sources"]:
        rows = teams(s["pool"], context)
        if rows:
            b = s.get("band", "round")
            sb = (0.0, 100.0) if b == "any" else (lo, hi) if b == "round" else (float(b[0]), float(b[1]))
            w = float(s.get("weight", 1))
            (srcs if w > 0 else fallback).append((rows, sb, w or 1.0))
    srcs = srcs or fallback
    if not srcs:
        raise RuntimeError(f"{mode} opponent mix has no usable pool ({[s['pool'] for s in m['sources']]})")

    while len(chosen) < n:
        options = []
        for rows, sb, w in srcs:
            widen = 0
            while True:
                cands = [t for t in rows if ident(t) not in seen and sb[0] - widen <= t.get("_percentile", 50) <= sb[1] + widen]
                if cands or widen >= 100:
                    break
                widen += 5
            if cands:
                options.append((cands, w * (1.0 if widen == 0 else 0.25)))
        if not options:
            break
        cands = rng.choices([c for c, _ in options], weights=[w for _, w in options])[0]
        t = rng.choice(cands)
        chosen.append(t)
        seen.add(ident(t))
    return chosen
