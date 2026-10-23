
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

from app import custom_support as C, event_schedule as E, master_data as M

from . import op
from .common import OpError, future_bundles, load, region_name

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "server/data/heroes_opponents"


def _global():
    if region_name() != "global":
        raise OpError("League of Heroes is served on Global only.")


def _heroes_rows() -> list[dict]:
    rows = {}
    with C.use_pack(""):
        if M.has_table("heroes_data"):
            rows = {r["heroes_id"]: dict(r) for r in M.query("SELECT * FROM heroes_data")}
    stages = {}
    for b in future_bundles():
        t = b.get("tables") or {}
        for r in t.get("heroes_data") or []:
            rows.setdefault(r["heroes_id"], dict(r, ported=True))
        for r in t.get("heroes_stage_schedule") or []:
            stages.setdefault(r["heroes_id"], []).append(r)
    out = []
    for hid, r in sorted(rows.items()):
        main = [s for s in stages.get(hid, []) if s["stage"] == 1]
        out.append({"id": hid, "start_date": r["start_date"], "end_date": r["end_date"],
                    "span": int(r["end_date"]) - int(r["start_date"]), "ported": bool(r.get("ported")),
                    "stages": sorted(({"stage": s["stage"], "step": s["stage_step"], "start": s["start_date"],
                                       "end": s["end_date"]} for s in stages.get(hid, [])),
                                     key=lambda s: s["start"]),
                    "main_days": round((max((s["end_date"] for s in main), default=0)
                                        - min((s["start_date"] for s in main), default=0)) / 86400, 1) if main else None,
                    "generated": (GENERATED / f"heroes_{hid}.jsonl.gz").exists()})
    return out


def _generated_info(hid) -> dict | None:
    p = GENERATED / f"heroes_{hid}.jsonl.gz"
    if not p.exists():
        return None
    n = 0
    with gzip.open(p, "rt") as fh:
        for line in fh:
            n += 1
    return {"file": p.name, "teams": n, "modified": p.stat().st_mtime, "size": p.stat().st_size}


@op("pvp.heroes")
def heroes_status(viewer, args):
    _global()
    from app.handlers import heroes as H
    sched = next((e for e in E.schedules() if e["family"] == "heroes"), None)
    published = next((e for e in (C.manifest() or {}).get("events") or [] if e["family"] == "heroes"), None)
    now = int(time.time())
    act = H.active()
    stage = H.stage_at(act["heroes_id"]) if act else (0, 0)
    st = (load(viewer).get(H.STATE_KEY) or {}) if viewer else {}
    from .common import future_content
    return {"heroes": _heroes_rows(), "future_content": future_content(), "schedule": sched, "published": published, "now": now,
            "active": {"id": act["heroes_id"], "start": act["start_date"], "end": act["end_date"],
                       "stage": stage[0], "step": stage[1]} if act else None,
            "viewer": {k: st.get(k) for k in ("heroes_id", "league_score", "rank", "tickets") if k in st} if st else None,
            "enabled": C.is_enabled(), "digest": (C.manifest() or {}).get("digest", "")}


@op("pvp.heroes_start", label="League of Heroes: serve LoH {heroes_id}")
def heroes_start(viewer, args):
    _global()
    hid = int(args.get("heroes_id") or 0)
    row = next((h for h in _heroes_rows() if h["id"] == hid), None)
    if not row:
        raise OpError(f"No League of Heroes {hid} (not in this master or a ported umapack).")
    start = int(args.get("start_date") or time.time())
    days = args.get("days")
    span = int(float(days) * 86400) if days else row["span"]
    entries = [e for e in E.schedules() if e["family"] != "heroes"]
    entries.append({"family": "heroes", "event_id": hid, "start_date": start, "end_date": start + max(86400, span)})
    try:
        E.set_schedules(entries)
        m = C.publish()
        C.set_enabled(True)
    except ValueError as e:
        raise OpError(str(e)) from e
    return {"digest": m["digest"]}


@op("pvp.heroes_stop", label="League of Heroes: stop")
def heroes_stop(viewer, args):
    _global()
    from .sync import pack_needed
    E.set_schedules([e for e in E.schedules() if e["family"] != "heroes"])
    if not pack_needed():
        C.set_enabled(False)
        return {"digest": ""}
    m = C.publish()
    return {"digest": m["digest"]}


@op("pvp.heroes_reset_viewer", label="League of Heroes: reset this trainer")
def heroes_reset_viewer(viewer, args):
    _global()
    from app.handlers import heroes as H
    from .common import save
    full = load(viewer)
    full.pop(H.STATE_KEY, None)
    save(viewer, full)
    return {"ok": True}


@op("pvp.opponents")
def opponents(viewer, args):
    _global()
    from app import champions_pool as POOL
    loh = [dict(_generated_info(h["id"]) or {}, heroes_id=h["id"]) for h in _heroes_rows()]
    meta = {}
    try:
        meta = json.loads((GENERATED / "heroes_meta.json").read_text()).get("heroes") or {}
    except (OSError, ValueError):
        pass
    for x in loh:
        m = meta.get(str(x["heroes_id"])) or {}
        x["source_umas"] = len(m.get("usage") or {})
        x["comps"] = len(m.get("comps") or {})
    return {"cm": POOL.snapshots(), "loh": loh, "corpus": str(POOL.corpus_root()),
            "generated_dir": str(GENERATED)}


def _os():
    _global()
    from app import opponent_sources as OS
    return OS


@op("pvp.opp_state")
def opp_state(viewer, args):
    OS = _os()
    from .common import text
    cms = []
    with C.use_pack((C.manifest() or {}).get("digest", "")):
        ids = {r["id"] for r in M.query("SELECT id FROM champions_schedule")}
        for cm in sorted(ids, reverse=True):
            cms.append({"id": cm, "name": text(206, cm) or f"CM {cm}"})
    return {"pools": OS.catalog(), "mix": {m: OS.mix(m) for m in OS.MODES}, "lists": OS.lists(),
            "cms": cms, "heroes": [h["id"] for h in _heroes_rows()]}


@op("pvp.opp_mix", label="Opponents: {mode} mix")
def opp_mix(viewer, args):
    OS = _os()
    try:
        return OS.set_mix(args["mode"], None if args.get("reset") else args["mix"])
    except (ValueError, KeyError) as e:
        raise OpError(str(e)) from e


@op("pvp.opp_list", label="Opponents: team list {name}")
def opp_list(viewer, args):
    OS = _os()
    try:
        with C.use_pack((C.manifest() or {}).get("digest", "")):
            return {"refs": OS.set_list(str(args["name"]), None if args.get("delete") else args.get("refs") or [])}
    except (ValueError, KeyError) as e:
        raise OpError(str(e)) from e


@op("pvp.opp_teams")
def opp_teams(viewer, args):
    OS = _os()
    from .common import text
    lo, hi = float(args.get("min", 0)), float(args.get("max", 100))
    with C.use_pack((C.manifest() or {}).get("digest", "")):
        rows = sorted((t for t in OS.teams(str(args["pool"]), {}) if lo <= t.get("_percentile", 50) <= hi),
                      key=lambda t: -t.get("_percentile", 50))
        limit = max(1, min(500, int(args.get("limit") or 60)))
        out = []
        for t in rows[:limit]:
            out.append({"id": t["id"], "pool": t.get("_pool") or args["pool"], "percentile": t.get("_percentile"),
                        "template": t.get("_template"),
                        "members": [{"card_id": int(m["card"]), "name": text(4, int(m["card"])),
                                     "style": int(m.get("racingStyle") or 0),
                                     "role": "debuffer" if int(m.get("style") or 0) == 6 else m.get("_role")
                                     or ("runaway" if int(m.get("racingStyle") or 0) == 5 else "ace"),
                                     "stats": m["stats"], "score": m.get("score"), "skills": len(m.get("skills") or []),
                                     "aptitudes": m.get("aptitudes")} for m in t["members"]]})
    return {"pool": args["pool"], "count": len(rows), "teams": out}


@op("pvp.opp_pool_spec")
def opp_pool_spec(viewer, args):
    OS = _os()
    name = str(args.get("name") or "")
    try:
        return json.loads((OS.gen_dir() / f"{name}.json").read_text())
    except (OSError, ValueError) as e:
        raise OpError(f"No generated pool {name}") from e


@op("pvp.opp_pool_delete", label="Opponents: delete pool gen:{name}")
def opp_pool_delete(viewer, args):
    OS = _os()
    name = str(args.get("name") or "")
    for mode in OS.MODES:
        if any(s["pool"] == f"gen:{name}" for s in OS.mix(mode)["sources"]):
            raise OpError(f"gen:{name} is in the {mode} mix; take it out first.")
    for suffix in (".jsonl.gz", ".json"):
        (OS.gen_dir() / f"{name}{suffix}").unlink(missing_ok=True)
    return {"ok": True}


TT_KNOBS = {
    "team_trials_rp_cost": ("int", 0, 5, "RP per match", "RP one Team Trials match costs."),
    "team_trials_same_class": ("bool", None, None, "Same-class opponents only",
                               "Real local trainers are offered only from your own class."),
    "team_trials_opponent_band": ("float", 1, 100, "Opponent band ×", "Real trainers are offered only if their team rating is within yours ÷ this to yours × this (default 2.5)."),
    "team_trials_real_opponents_only": ("bool", None, None, "Real trainers only",
                                        "Never fill missing offers from the real-team corpus or mobs."),
    "team_trials_corpus_opponents": ("bool", None, None, "Use real-team corpus",
                                     "Fill offers from the 5,070 real Global teams (else generated mobs)."),
    "team_trials_npc_strength": ("float", 0, 2, "Race filler strength", "Stat total of the NPCs filling empty gates, as a share of the field average (default 0.85)."),
    "team_trials_win_box_chance": ("float", 0, 1, "Win-box chance", "Chance a won race drops a reward box."),
    "team_trials_guarantee_chance": ("float", 0, 1, "Guaranteed-reward chance", "Chance an offered opponent shows the guaranteed-reward mark."),
    "team_trials_fan_base": ("int", 0, 100000, "Fans per runner", "Flat fans each runner gets."),
    "team_trials_fan_place_pool": ("int", 0, 100000, "Fan place pool", "Extra fans divided by finish place."),
    "team_trials_friend_per_runner": ("int", 0, 1000, "Friend pts per runner", ""),
    "team_trials_friend_per_win": ("int", 0, 1000, "Friend pts per win", ""),
    "team_trials_timezone": ("str", None, None, "Week timezone", "IANA zone the weekly schedule runs in (Global UTC, JP Asia/Tokyo)."),
}


def _cfg_read() -> dict:
    from app import config
    try:
        with open(config.CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except FileNotFoundError:
        return {}
    except ValueError as ex:
        raise OpError(f"{config.CONFIG_PATH} isn't valid JSON: {ex}") from ex


@op("pvp.tt")
def tt_status(viewer, args):
    from app.handlers import stamina, team_stadium as TS
    data = _cfg_read()
    out = {"knobs": [{"key": k, "kind": kind, "min": lo, "max": hi, "label": label, "help": help_, "value": data.get(k)}
                     for k, (kind, lo, hi, label, help_) in TT_KNOBS.items()],
           "term_id": TS._term_id(), "term_state": TS._current_term_state(), "viewer": None}
    if viewer:
        full = load(viewer)
        st = full.get(TS.TEAM_STADIUM_STATE_KEY) or {}
        rp = stamina.RP.read(full)
        out["viewer"] = {"team_class": st.get("team_class", 1), "best_team_class": st.get("best_team_class", 1),
                         "best_point": st.get("best_point", 0), "term_id": st.get("term_id"),
                         "members": len(st.get("team_data_array") or []), "in_match": bool(st.get("current_match")),
                         "rp": rp.get("current_rp"), "max_rp": rp.get("max_rp")}
    return out


@op("pvp.tt_set", label="Team Trials: settings")
def tt_set(viewer, args):
    from app import config
    data = _cfg_read()
    for k, v in (args.get("values") or {}).items():
        if k not in TT_KNOBS:
            raise OpError(f"Unknown setting {k}.")
        kind, lo, hi, label, _ = TT_KNOBS[k]
        if v is None or v == "":
            data.pop(k, None)
            continue
        if kind == "bool":
            data[k] = bool(v)
        elif kind == "str":
            if k == "team_trials_timezone":
                from zoneinfo import ZoneInfo
                try:
                    ZoneInfo(str(v))
                except Exception as ex:
                    raise OpError(f"{v} is not a timezone.") from ex
            data[k] = str(v)
        else:
            try:
                v = int(v) if kind == "int" else float(v)
            except (TypeError, ValueError) as ex:
                raise OpError(f"{label} must be a number.") from ex
            if not lo <= v <= hi:
                raise OpError(f"{label} must be between {lo} and {hi}.")
            data[k] = v
    tmp = config.CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    import os
    os.replace(tmp, config.CONFIG_PATH)
    return tt_status(viewer, {})


@op("pvp.tt_trainer", label="Team Trials: {action}")
def tt_trainer(viewer, args):
    from app.handlers import stamina, team_stadium as TS
    from .common import save
    if not viewer:
        raise OpError("Pick a trainer first.")
    full = load(viewer)
    st = TS._team_stadium_state(full)
    a = args.get("action")
    if a == "class":
        c = int(args.get("team_class") or 0)
        if not 1 <= c <= 6:
            raise OpError("Class must be 1-6.")
        st["team_class"] = c
        st["best_team_class"] = max(c, st.get("best_team_class") or 1)
    elif a == "refill":
        rp = stamina.RP.read(full)
        stamina.RP.add(full, max(0, int(rp.get("max_rp") or 0) - int(rp.get("current_rp") or 0)))
    elif a == "clear_match":
        st["current_match"] = None
    elif a == "reset_week":
        st["best_point"], st["current_match"] = 0, None
    else:
        raise OpError(f"Unknown action {a}.")
    save(viewer, full)
    return tt_status(viewer, {})
