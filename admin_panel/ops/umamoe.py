
from __future__ import annotations

import functools
import json
import random
import re
import urllib.error
import urllib.request
from datetime import datetime

from app import master_data as M
from app.handlers import trained_chara as TC

from . import op
from .catalog import rank_label
from .common import APTITUDES, STATS, OpError, card_name, load, now_str, save, TIME_FORMAT
from .legacy import KEY, _next_id, _roster

API = "https://uma.moe/api"
IMPORTS_KEY = "umamoe_imports"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_ACCOUNT = re.compile(r"(?<!\d)\d{9,12}(?!\d)")
_PARENT_POSITIONS = (10, 20, 11, 12, 21, 22)


def _key(args) -> str:
    key = str(args.get("api_key") or "").strip()
    if not key:
        raise OpError("Enter your uma.moe API key.")
    return key


def _get(path: str, key: str):
    req = urllib.request.Request(f"{API}{path}", headers={
        "X-API-Key": key, "Accept": "application/json", "User-Agent": "UmaPS-AdminPanel"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        try:
            body = json.loads(ex.read() or b"{}")
            detail = body.get("details") or body.get("error") or ""
        except ValueError:
            detail = ""
        reason = {401: "the API key was rejected", 403: "the key was refused, or this trainer hides their profile/veterans",
                  404: "trainer or veteran not found", 429: "rate limited, try again shortly"}.get(ex.code, f"HTTP {ex.code}")
        raise OpError(f"uma.moe: {reason}." + (f" ({detail})" if detail else "")) from None
    except (urllib.error.URLError, TimeoutError) as ex:
        raise OpError(f"Could not reach uma.moe: {getattr(ex, 'reason', ex)}") from None
    except ValueError:
        raise OpError("uma.moe returned something that isn't JSON.") from None


def _target(args):
    raw = str(args.get("target") or "").strip()
    m = _UUID.search(raw)
    if m:
        return "veteran", m.group(0).lower()
    m = _ACCOUNT.search(raw)
    if m:
        return "profile", m.group(0)
    raise OpError("Enter a trainer id (9–12 digits), a uma.moe profile link, or a veteran UUID.")


def _fetch_sources(args) -> tuple[dict, list[tuple[str, str, dict]]]:
    key = _key(args)
    kind, ident = _target(args)
    if kind == "veteran":
        vet = _get(f"/v4/user/profile/veterans/{ident}", key)
        return {"account_id": vet.get("account_id"), "name": None}, [(f"vet:{vet.get('id')}", "veteran", vet)]
    prof = _get(f"/v4/user/profile/{ident}", key)
    trainer = prof.get("trainer") or {}
    out, seen = [], set()
    vets = sorted(prof.get("veterans") or [], key=lambda v: not v.get("is_pinned"))
    for v in vets:
        out.append((f"vet:{v.get('id')}", "veteran", v))
        seen.add(v.get("trained_chara_id"))
    for t in prof.get("team_stadium") or []:
        if t.get("trained_chara_id") in seen:
            continue
        seen.add(t.get("trained_chara_id"))
        out.append((f"ts:{t.get('id')}", "team", t))
    inh = prof.get("inheritance") or {}
    if inh.get("main_parent_id"):
        own = set(_star_factor_ids(inh, "main"))
        match = next((i for i, (_, kind, raw) in enumerate(out) if _card(kind, raw) == inh["main_parent_id"]
                      and set(_own_factor_ids(kind, raw)) == own), None)
        if match is not None:
            key, kind, raw = out.pop(match)
            out.insert(0, (key, kind, dict(raw, _star=inh)))
        else:
            out.insert(0, (f"star:{inh.get('inheritance_id')}", "star", inh))
    return {"account_id": trainer.get("account_id") or ident, "name": trainer.get("name")}, out


def _card(kind: str, raw: dict) -> int:
    return _int(raw.get("main_parent_id") if kind == "star" else raw.get("card_id"))


def _own_factor_ids(kind: str, raw: dict) -> list[int]:
    if kind == "star":
        return _star_factor_ids(raw, "main")
    if kind == "team":
        return [f for f in raw.get("factors") or [] if isinstance(f, int)]
    return [f.get("factor_id") for f in _jsonish(raw.get("factor_info_array")) if isinstance(f, dict)]


def _star_factor_ids(inh: dict, side: str) -> list[int]:
    ids = [inh.get(f"{side}_{c}_factors") for c in ("blue", "pink", "green")]
    return [f for f in ids + list(inh.get(f"{side}_white_factors") or []) if isinstance(f, int) and f > 0]


@functools.lru_cache(maxsize=None)
def _ids(table: str, column: str = "id") -> frozenset:
    return frozenset(r[column] for r in M.query(f"SELECT {column} FROM {table}"))


def _jsonish(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def _int(v, default=0, lo=None, hi=None) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _time(v) -> str:
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).strftime(TIME_FORMAT)
        except ValueError:
            pass
    return now_str()


def _skills(pairs) -> list[dict]:
    known = _ids("skill_data")
    out = {}
    for sid, level in pairs:
        if sid in known:
            out[sid] = {"skill_id": sid, "level": _int(level, 1, 1, 6)}
    return list(out.values())


def _factors(ids) -> list[dict]:
    known = _ids("succession_factor", "factor_id")
    return [{"factor_id": f, "level": 0} for f in ids if f in known]


def _saddles(ids) -> list[int]:
    known = _ids("single_mode_wins_saddle")
    return [s for s in ids if isinstance(s, int) and s in known]


def _supports(entries) -> list[dict]:
    known = _ids("support_card_data")
    _, maxexp, rar = TC._support_cards()
    out = []
    for e in entries:
        sid = _int(e.get("support_card_id")) if isinstance(e, dict) else _int(e)
        if sid not in known or len(out) >= 6:
            continue
        out.append({"position": len(out) + 1, "support_card_id": sid,
                    "exp": _int(e.get("exp"), 0, 0) if isinstance(e, dict) else maxexp.get(rar.get(sid), 0) or 0,
                    "limit_break_count": _int(e.get("limit_break_count"), 0, 0, 4) if isinstance(e, dict) else 4})
    return out


def _parents(entries) -> list[dict] | None:
    cards = _ids("card_data")
    by_pos = {}
    for p in entries:
        if not isinstance(p, dict) or p.get("position_id") not in _PARENT_POSITIONS or p.get("card_id") not in cards:
            continue
        info = _jsonish(p.get("factor_info_array")) or [{"factor_id": f} for f in _jsonish(p.get("factor_id_array"))]
        by_pos[p["position_id"]] = {
            "position_id": p["position_id"], "card_id": p["card_id"],
            "rank": _int(p.get("rank"), 1, 1), "rarity": _int(p.get("rarity"), 3, 1, 5),
            "talent_level": _int(p.get("talent_level"), 1, 1, 5),
            "factor_info_array": _factors(f.get("factor_id") for f in info if isinstance(f, dict)),
            "win_saddle_id_array": _saddles(_jsonish(p.get("win_saddle_id_array"))),
            "owner_viewer_id": 0,
        }
    return [by_pos[p] for p in _PARENT_POSITIONS] if len(by_pos) == 6 else None


def _problem(kind: str, raw: dict) -> str | None:
    card_id = _card(kind, raw)
    if card_id not in _ids("card_data"):
        return f"Card {card_id} isn't in this region's master data."
    return None


def _star_parents(rec: dict, inh: dict) -> None:
    by_pos = {p.get("position_id"): p for p in rec.get("succession_chara_array") or []}
    for pos, side in ((10, "left"), (20, "right")):
        card_id = _int(inh.get(f"parent_{side}_id"))
        if pos in by_pos and card_id in _ids("card_data"):
            by_pos[pos].update(card_id=card_id, factor_info_array=_factors(_star_factor_ids(inh, side)),
                               win_saddle_id_array=_saddles(inh.get(f"{side}_win_saddles") or []))


def _build_star(rec: dict, inh: dict) -> None:
    rec["factor_info_array"] = _factors(_star_factor_ids(inh, "main"))
    rec["win_saddle_id_array"] = _saddles(inh.get("main_win_saddles") or [])
    rec["wins"] = _int(inh.get("win_count"), 0, 0)
    rec["rank_score"] = _int(inh.get("parent_rank"), rec["rank_score"], 0)
    rank = _int(inh.get("parent_rarity"))
    rec["rank"] = rank if rank in _ids("single_mode_rank") else TC._rank_for_score(rec["rank_score"])
    _star_parents(rec, inh)


def _build(viewer, kind: str, raw: dict, tid: int) -> dict:
    card_id = _card(kind, raw)
    uma = next((u for u in TC._UMA_DATA if u.get("cardId") == card_id), None)
    rec = TC.build_generated_veteran(viewer, uma or TC._UMA_DATA[0], tid, random.Random(tid))
    rec["card_id"] = card_id
    rec["race_cloth_id"] = TC.dress_for_card(card_id, card_id // 100)
    for s in STATS:
        rec[s] = _int(raw.get(s), rec[s], 0, 9999)
    for a in APTITUDES:
        rec[a] = _int(raw.get(a), rec[a], 1, 8)
    rec["running_style"] = _int(raw.get("running_style"), rec["running_style"], 1, 4)
    rec["rarity"] = _int(raw.get("rarity"), rec["rarity"], 1, 5)
    rec["talent_level"] = _int(raw.get("talent_level"), rec["talent_level"], 1, 5)
    rec["fans"] = _int(raw.get("fans"), rec["fans"], 0)
    rec["rank_score"] = _int(raw.get("rank_score"), rec["rank_score"], 0)
    rec["rank"] = TC._rank_for_score(rec["rank_score"])
    rec["chara_grade"] = TC._chara_grade_for(rec["fans"])
    scenario = _int(raw.get("scenario_id"))
    if scenario in _ids("single_mode_scenario"):
        rec["scenario_id"] = scenario
    rec["is_locked"] = 1

    if kind == "star":
        _build_star(rec, raw)
        rec["create_time"] = rec["register_time"] = now_str()
    elif kind == "veteran":
        rank = _int(raw.get("rank"))
        if rank in _ids("single_mode_rank"):
            rec["rank"] = rank
        grade = _int(raw.get("chara_grade"))
        if grade > 0:
            rec["chara_grade"] = grade
        rec["wins"] = _int(raw.get("wins"), 0, 0)
        rec["succession_num"] = _int(raw.get("succession_num"), 0, 0)
        nick = _int(raw.get("nickname_id"))
        if nick in _ids("nickname"):
            rec["nickname_id"] = nick
            rec["nickname_id_array"] = [nick]
        rec["skill_array"] = _skills((s.get("skill_id"), s.get("level")) for s in _jsonish(raw.get("skill_array"))
                                     if isinstance(s, dict)) or rec["skill_array"]
        sup = _supports(_jsonish(raw.get("support_card_list")))
        rec["support_card_list"] = sup or rec["support_card_list"]
        rec["factor_info_array"] = _factors(f.get("factor_id") for f in _jsonish(raw.get("factor_info_array"))
                                            if isinstance(f, dict))
        rec["win_saddle_id_array"] = _saddles(_jsonish(raw.get("win_saddle_id_array")))
        parents = _parents(_jsonish(raw.get("succession_chara_array")))
        if parents:
            rec["succession_chara_array"] = parents
        rec["create_time"] = rec["register_time"] = _time(raw.get("create_time"))
    else:
        rec["skill_array"] = _skills((s // 10, s % 10) for s in raw.get("skills") or [] if isinstance(s, int)) \
            or rec["skill_array"]
        rec["support_card_list"] = _supports(raw.get("support_cards") or []) or rec["support_card_list"]
        rec["factor_info_array"] = _factors(raw.get("factors") or [])
        rec["create_time"] = rec["register_time"] = _time(raw.get("creation_time"))
        if raw.get("_star"):
            rec["win_saddle_id_array"] = _saddles(raw["_star"].get("main_win_saddles") or [])
            rec["wins"] = _int(raw["_star"].get("win_count"), 0, 0)
            _star_parents(rec, raw["_star"])
    return rec


def _summary(key: str, kind: str, raw: dict, imported: dict) -> dict:
    card_id = _card(kind, raw)
    star = raw if kind == "star" else raw.get("_star")
    out = {
        "key": key, "kind": kind, "star": bool(star), "pinned": bool(raw.get("is_pinned")), "card_id": card_id,
        "name": card_name(card_id), "problem": _problem(kind, raw), "imported": imported.get(key),
        "distance": raw.get("distance_type"), "parents": None,
    }
    if star:
        out["parents"] = [card_name(star.get("parent_left_id")), card_name(star.get("parent_right_id"))]
        out["spark_stars"] = {c: _int(star.get(f"{c}_stars_sum")) for c in ("blue", "pink", "green", "white")}
    if kind == "star":
        score = _int(raw.get("parent_rank"))
        rank = _int(raw.get("parent_rarity"))
        return {**out, "stats": None, "apt": None, "score": score, "skills": None, "created": None,
                "rank_label": rank_label(rank if rank in _ids("single_mode_rank") else TC._rank_for_score(score))}
    score = _int(raw.get("rank_score"))
    skills = _jsonish(raw.get("skill_array")) if kind == "veteran" else raw.get("skills") or []
    return {**out, "stats": [_int(raw.get(s)) for s in STATS],
            "apt": [_int(raw.get(a), 1, 1, 8) for a in APTITUDES], "score": score,
            "rank_label": rank_label(TC._rank_for_score(score)), "skills": len(skills),
            "created": _time(raw.get("create_time") or raw.get("creation_time"))}


def _imported(full_state) -> dict:
    live = {r.get("trained_chara_id") for r in full_state.get(KEY) or []}
    return {v["source"]: int(tid) for tid, v in (full_state.get(IMPORTS_KEY) or {}).items()
            if int(tid) in live and isinstance(v, dict)}


@op("umamoe.fetch")
def fetch(viewer, args):
    trainer, sources = _fetch_sources(args)
    imported = _imported(load(viewer))
    return {"trainer": trainer,
            "entries": [_summary(key, kind, raw, imported) for key, kind, raw in sources]}


@op("umamoe.import", label="uma.moe: import {keys} uma(s)")
def import_(viewer, args):
    keys = list(dict.fromkeys(str(k) for k in args.get("keys") or []))
    if not keys:
        raise OpError("Pick at least one uma to import.")
    replace = bool(args.get("replace"))
    trainer, sources = _fetch_sources(args)
    by_key = {key: (kind, raw) for key, kind, raw in sources}
    full_state, roster = _roster(viewer, seed=True)
    origins = full_state.setdefault(IMPORTS_KEY, {})
    imported = _imported(full_state)
    added, replaced, skipped = [], [], []
    for key in keys:
        if key not in by_key:
            skipped.append({"key": key, "reason": "No longer on the trainer's profile."})
            continue
        kind, raw = by_key[key]
        problem = _problem(kind, raw)
        if problem:
            skipped.append({"key": key, "reason": problem})
            continue
        old = imported.get(key)
        if old is not None and not replace:
            skipped.append({"key": key, "reason": f"Already imported as #{old}."})
            continue
        if old is None and len(roster) >= TC.VETERAN_CAPACITY:
            skipped.append({"key": key, "reason": f"Roster is full ({TC.VETERAN_CAPACITY})."})
            continue
        tid = old if old is not None else _next_id(roster)
        rec = _build(viewer, kind, raw, tid)
        if old is not None:
            idx = next(i for i, r in enumerate(roster) if r.get("trained_chara_id") == old)
            rec["is_locked"] = roster[idx].get("is_locked", 1)
            roster[idx] = rec
            replaced.append(tid)
        else:
            roster.append(rec)
            added.append(tid)
            imported[key] = tid
        origins[str(tid)] = {"source": key, "account_id": trainer.get("account_id"),
                             "trainer": trainer.get("name"), "imported": now_str()}
    if added or replaced:
        full_state[KEY] = roster
        save(viewer, full_state)
    return {"added": added, "replaced": replaced, "skipped": skipped}
