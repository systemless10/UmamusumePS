from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

from . import region
import logging

log = logging.getLogger("uma-server")

DEFAULT_STAGE_LEN = 3600
HOLD_MARGIN = 30

STAGES = [
    ("league", "League select"),
    ("round1", "Round 1"),
    ("round2", "Round 2"),
    ("final_entry", "Finals registration"),
    ("final_race", "Finals matching"),
    ("final_result", "Finals race & results"),
    ("ended", "Ended"),
]
STAGE_NAMES = [s for s, _ in STAGES]

DEFAULT_POOL = {
    "snapshot": "auto",
    "bands": {"round1": [0, 100], "round2_a": [45, 100], "round2_b": [0, 65],
              "final_a": [70, 100], "final_b": [30, 85]},
    "final_win_pressure": 0.7,
    "pinned": [],
    "mood": "random",
}

_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "data": None}


def _path() -> Path:
    return region.data_dir() / "champions.json"


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
            return _cache["data"] or {}
        _cache.update(path=str(p), mtime=mtime, data=data)
        return data


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)
    with _lock:
        _cache.update(path=None, mtime=None, data=None)


def pool_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_POOL))
    user = load().get("pool") or {}
    for k, v in user.items():
        if k == "bands" and isinstance(v, dict):
            cfg["bands"].update(v)
        else:
            cfg[k] = v
    return cfg


def layout(start: int, end: int) -> dict:
    w = (end - start + 1) // 6
    s = start
    win = {
        "league": (s, s + w),
        "round1": (s + w, s + 2 * w),
        "round2": (s + 2 * w, s + 3 * w),
        "final_entry": (s + 3 * w, s + 4 * w),
        "final_race": (s + 4 * w, s + 5 * w),
        "final_result": (s + 5 * w, s + 6 * w),
        "ended": (s + 6 * w, 2 ** 31 - 1),
    }
    return {"stage_len": w, "interval": 0, "windows": win}


def round_rows(start: int, end: int) -> dict:
    lay = layout(start, end)
    w, s = lay["stage_len"], start

    def row(a, b, interval=None):
        ia, ib = interval or (b - 1, b - 1)
        return {"start_date": a, "end_date": b - 1, "interval_start_time": ia, "interval_end_time": ib}

    return {
        (0, 0): row(s, s + w),
        (1, 0): row(s + w, s + 2 * w),
        (2, 0): row(s + 2 * w, s + 3 * w),
        (3, 1): row(s + 3 * w, s + 4 * w),
        (3, 2): row(s + 4 * w, s + 5 * w),
        (3, 3): row(s + 5 * w, s + 6 * w),
    }


def _utc_text(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y/%m/%d %H:%M:%S")


def apply_schedule(connection, entry: dict) -> None:
    cm, start, end = entry["event_id"], entry["start_date"], entry["end_date"]
    rows = round_rows(start, end)
    for (rnd, detail), d in rows.items():
        connection.execute(
            "UPDATE champions_round_schedule SET start_date=?, end_date=?, interval_start_time=?, "
            "interval_end_time=? WHERE champions_id=? AND round=? AND round_detail=?",
            (d["start_date"], d["end_date"], d["interval_start_time"], d["interval_end_time"], cm, rnd, detail))
    r1, r2 = rows[(1, 0)], rows[(2, 0)]
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "item_data" in tables:
        connection.execute("UPDATE item_data SET start_date=?, end_date=? WHERE item_category=163 AND effect_type_1=?",
                           (_utc_text(start), _utc_text(r2["end_date"]), cm))
    if "mission_data" in tables:
        connection.execute("UPDATE mission_data SET start_date=?, end_date=? WHERE condition_type=200012 AND condition_value_1=?",
                           (_utc_text(r1["start_date"]), _utc_text(r2["end_date"]), cm))


def ticket_mission_active(row, now: int) -> bool | None:
    run = active()
    if not run or row["condition_type"] != 200012 or row["condition_value_1"] != run["champions_id"]:
        return None
    win = layout(run["start"], run["end"])["windows"]
    if not win["round1"][0] <= now < win["round2"][1]:
        return False
    from . import master_data as M
    ids = [r["id"] for r in M.query("SELECT id FROM mission_data WHERE condition_type=200012 "
                                     "AND condition_value_1=? ORDER BY id", (run["champions_id"],))]
    day = lambda t: (t - 22 * 3600) // 86400
    k = min(len(ids) - 1, max(0, day(now) - day(win["round1"][0])))
    return row["id"] == ids[k]


def _clock() -> dict:
    return load().get("clock") or {}


def served_time(real: float | None = None) -> int | None:
    data = load()
    clock = data.get("clock")
    if not clock:
        return None
    t = time.time() if real is None else real
    if clock.get("mode") == "cm":
        v = clock["s0"] + (t - clock["r0"])
        hold = clock.get("hold_until")
        if hold:
            v = min(v, hold)
        return int(v)
    hw = clock.get("hw") or 0
    return int(hw) if hw > t else None


def active() -> dict | None:
    data = load()
    run = data.get("run")
    if not run or (data.get("clock") or {}).get("mode") != "cm":
        return None
    return run


def stage_at(run: dict, ts: int) -> str:
    win = layout(run["start"], run["end"])["windows"]
    for name in STAGE_NAMES:
        a, b = win[name]
        if a <= ts < b:
            return name
    return "league" if ts < run["start"] else "ended"


def current_stage(now: int | None = None) -> str | None:
    run = active()
    if not run:
        return None
    if now is None:
        now = served_time() or int(time.time())
    return stage_at(run, now)


def _now_served() -> int:
    return served_time() or int(time.time())


def _hold_for(run: dict, stage: str, now: int | None = None) -> int | None:
    a, b = layout(run["start"], run["end"])["windows"][stage]
    if stage == "ended":
        return None
    return min(b - HOLD_MARGIN, max((a + b) // 2, now or 0))


def start(champions_id: int, stage_len: int = DEFAULT_STAGE_LEN, hold: bool = True) -> dict:
    from . import custom_support as C, event_schedule as E, master_data as M
    if region.CURRENT_REGION.get() != "global":
        raise ValueError("Champions Meeting is served on Global only")
    if not M.query_one("SELECT id FROM champions_schedule WHERE id=?", (int(champions_id),)) \
            and not E._ported("champions_schedule", "id", int(champions_id)):
        raise ValueError(f"CM {champions_id} is not in this master")
    stage_len = max(600, int(stage_len))
    t0 = max(int(time.time()), _now_served()) + 5
    run = {"champions_id": int(champions_id), "start": t0, "end": t0 + 6 * stage_len - 1,
           "hold": bool(hold), "run_id": f"{champions_id}-{t0}"}
    before = E.schedules()
    entries = [e for e in before if e["family"] != "champions"]
    entries.append({"family": "champions", "event_id": run["champions_id"],
                    "start_date": run["start"], "end_date": run["end"]})
    E.set_schedules(entries)
    try:
        m = C.publish()
    except Exception:
        E.set_schedules(before)
        raise
    C.set_enabled(True)
    _refresh_ios()
    data = dict(load())
    data["run"] = run
    data["clock"] = {"mode": "cm", "r0": time.time(), "s0": t0 + 1,
                     "hold_until": _hold_for(run, "league") if hold else None}
    _save(data)
    return {**run, "digest": m["digest"]}


def goto(stage: str) -> dict:
    run = active()
    if not run:
        raise ValueError("No Champions Meeting is running")
    if stage not in STAGE_NAMES:
        raise ValueError(f"Unknown stage {stage}")
    a, _ = layout(run["start"], run["end"])["windows"][stage]
    now = _now_served()
    if a + 1 < now and stage_at(run, now) != stage:
        raise ValueError("That stage is already over; the clock only runs forward. Start the CM again to replay it.")
    data = dict(load())
    data["clock"] = {"mode": "cm", "r0": time.time(), "s0": max(now, a + 1),
                     "hold_until": _hold_for(run, stage, max(now, a + 1)) if run.get("hold", True) else None}
    _save(data)
    return status()


def advance() -> dict:
    stage = current_stage()
    if stage is None:
        raise ValueError("No Champions Meeting is running")
    i = STAGE_NAMES.index(stage)
    if i + 1 >= len(STAGE_NAMES):
        raise ValueError("The CM has ended")
    return goto(STAGE_NAMES[i + 1])


def set_hold(hold: bool) -> dict:
    run = active()
    if not run:
        raise ValueError("No Champions Meeting is running")
    data = dict(load())
    now = _now_served()
    data["run"] = dict(run, hold=bool(hold))
    data["clock"] = {"mode": "cm", "r0": time.time(), "s0": now,
                     "hold_until": _hold_for(run, stage_at(run, now), now) if hold else None}
    _save(data)
    return status()


def stop() -> dict:
    from . import custom_support as C, date_sync as D, event_schedule as E
    data = dict(load())
    hw = _now_served()
    data["clock"] = {"mode": "real", "hw": hw if hw > time.time() else 0}
    data.pop("run", None)
    _save(data)
    entries = [e for e in E.schedules() if e["family"] != "champions"]
    E.set_schedules(entries)
    if (D.setting() or entries or C.unreleased_bundles()
            or any(not d["retired"] for d in C.status()["cards"])):
        C.publish()
    else:
        C.set_enabled(False)
    _refresh_ios()
    return status()


def _refresh_ios():
    try:
        from . import ios_cdn
    except ImportError:
        return
    try:
        ios_cdn.refresh_master()
    except Exception:
        log.exception("ios overlay master refresh failed")


def set_pool(pool: dict) -> dict:
    data = dict(load())
    cur = data.get("pool") or {}
    for k, v in (pool or {}).items():
        if v is None:
            cur.pop(k, None)
        elif k == "bands":
            cur.setdefault("bands", {}).update(v)
        else:
            cur[k] = v
    data["pool"] = cur
    _save(data)
    return pool_config()


def options() -> dict:
    return dict(load().get("options") or {})


def set_options(**opts) -> dict:
    data = dict(load())
    cur = dict(data.get("options") or {})
    cur.update({k: bool(v) for k, v in opts.items() if v is not None})
    data["options"] = cur
    _save(data)
    return cur


def status() -> dict:
    data = load()
    run = active()
    now = _now_served()
    out = {"running": bool(run), "served_time": now, "real_time": int(time.time()),
           "clock": data.get("clock") or {}, "pool": pool_config(), "options": options()}
    if run:
        lay = layout(run["start"], run["end"])
        stage = stage_at(run, now)
        out.update(run=run, stage=stage, stage_len=lay["stage_len"],
                   windows={k: list(v) for k, v in lay["windows"].items()},
                   stage_ends_in=max(0, lay["windows"][stage][1] - now))
    return out
