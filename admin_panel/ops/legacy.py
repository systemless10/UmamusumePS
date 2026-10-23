
from __future__ import annotations

import copy
import random

from app.handlers import trained_chara as TC

from . import op
from .catalog import rank_label
from .common import APTITUDES, STATS, OpError, card_name, ids_arg, load, now_str, save

KEY = TC.ROSTER_KEY


def _band(tid: int) -> str:
    if 1 <= tid <= TC.STARTER_ROSTER_SIZE:
        return "starter"
    if TC.PLAYER_CAREER_ID_BASE <= tid < TC.INJECT_ID_BASE:
        return "career"
    if TC.INJECT_ID_BASE <= tid < TC.HOUSE_ID_BASE:
        return "inject"
    if tid >= TC.HOUSE_ID_BASE:
        return "house"
    return "legacy"


def _factors(arr) -> list[int]:
    return [f.get("factor_id") for f in arr or [] if isinstance(f, dict) and f.get("factor_id")]


def _summary(rec: dict) -> dict:
    tid = rec.get("trained_chara_id") or 0
    parents = {}
    for p in rec.get("succession_chara_array") or []:
        if isinstance(p, dict):
            parents[str(p.get("position_id"))] = {"card_id": p.get("card_id"),
                                                  "factors": _factors(p.get("factor_info_array"))}
    return {
        "id": tid, "card_id": rec.get("card_id"), "name": card_name(rec.get("card_id")),
        "stats": [rec.get(s) or 0 for s in STATS], "apt": [rec.get(a) or 1 for a in APTITUDES],
        "score": rec.get("rank_score") or 0, "rank": rec.get("rank") or 1,
        "rank_label": rank_label(rec.get("rank") or 1),
        "scenario": rec.get("scenario_id"), "style": rec.get("running_style"),
        "talent": rec.get("talent_level"), "rarity": rec.get("rarity"), "fans": rec.get("fans"),
        "wins": rec.get("wins"), "nickname": rec.get("nickname_id"),
        "locked": bool(rec.get("is_locked")), "saved": bool(rec.get("is_saved")),
        "created": rec.get("create_time"), "skills": len(rec.get("skill_array") or []),
        "skill_ids": [s.get("skill_id") for s in rec.get("skill_array") or [] if isinstance(s, dict)],
        "factors": _factors(rec.get("factor_info_array")), "parents": parents,
        "band": _band(tid),
        "p1": rec.get("succession_trained_chara_id_1") or 0,
        "p2": rec.get("succession_trained_chara_id_2") or 0,
    }


def _relink(roster: list[dict], tid: int) -> None:
    seen, queue = set(), [tid]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        rec = next((r for r in roster if r.get("trained_chara_id") == cur), None)
        if rec is None:
            continue
        real = TC._real_ancestry(roster, rec.get("succession_trained_chara_id_1") or 0,
                                 rec.get("succession_trained_chara_id_2") or 0)
        if real is not None:
            rec["succession_chara_array"] = real
        queue += [r.get("trained_chara_id") for r in roster
                  if cur in ((r.get("succession_trained_chara_id_1") or 0), (r.get("succession_trained_chara_id_2") or 0))]


def _roster(viewer, seed: bool):
    if seed:
        TC._get_or_seed_roster(viewer)
    full_state = load(viewer)
    roster = full_state.get(KEY)
    if roster is None:
        roster = []
    return full_state, roster


def _next_id(roster) -> int:
    used = {r.get("trained_chara_id") for r in roster}
    nid = max([t for t in used if TC.INJECT_ID_BASE <= (t or 0) < TC.MAXED_INJECT_ID] or [TC.INJECT_ID_BASE - 1]) + 1
    while nid in used:
        nid += 1
    if nid >= TC.HOUSE_ID_BASE:
        raise OpError("The manual-inject id band is full.")
    return nid


@op("legacy.list")
def list_(viewer, args):
    full_state = load(viewer)
    roster = full_state.get(KEY) or []
    return {"veterans": [_summary(r) for r in roster if isinstance(r, dict)],
            "capacity": TC.VETERAN_CAPACITY, "version_ok": full_state.get(TC.ROSTER_VERSION_KEY) == TC.ROSTER_VERSION}


@op("legacy.get")
def get(viewer, args):
    full_state = load(viewer)
    tid = int(args.get("id") or 0)
    rec = next((r for r in full_state.get(KEY) or [] if r.get("trained_chara_id") == tid), None)
    if rec is None:
        raise OpError(f"No veteran {tid}.")
    return {"record": rec, "band": _band(tid)}


@op("legacy.save", label="Edit veteran {id}")
def save_one(viewer, args):
    full_state, roster = _roster(viewer, seed=True)
    tid = int(args.get("id") or 0)
    new = args.get("record")
    if not isinstance(new, dict):
        raise OpError("record must be an object.")
    idx = next((i for i, r in enumerate(roster) if r.get("trained_chara_id") == tid), None)
    if idx is None:
        raise OpError(f"No veteran {tid}.")
    new = copy.deepcopy(new)
    new["trained_chara_id"] = tid
    for s in STATS:
        if s in new:
            new[s] = max(0, min(9999, int(new[s] or 0)))
    for a in APTITUDES:
        if a in new:
            new[a] = max(1, min(8, int(new[a] or 1)))
    if args.get("auto_rank"):
        new["rank"] = TC._rank_for_score(int(new.get("rank_score") or 0))
    ids = {r.get("trained_chara_id") for r in roster}
    for k in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        pid = int(new.get(k) or 0)
        if pid == tid or (pid and pid not in ids):
            raise OpError(f"Parent {pid} is not another veteran in this roster.")
    roster[idx] = new
    _relink(roster, tid)
    full_state[KEY] = roster
    save(viewer, full_state)
    return {"id": tid, "rank": new.get("rank"), "rank_label": rank_label(new.get("rank") or 1)}


@op("legacy.apply", label="Veterans: {action} {ids}")
def apply(viewer, args):
    full_state, roster = _roster(viewer, seed=True)
    ids = set(ids_arg(args))
    action = args.get("action")
    hit = [r for r in roster if r.get("trained_chara_id") in ids]
    if action == "delete":
        full_state[KEY] = [r for r in roster if r.get("trained_chara_id") not in ids]
    elif action == "max":
        for r in hit:
            TC._max_out_veteran_fields(r)
    elif action in ("lock", "unlock"):
        for r in hit:
            r["is_locked"] = 1 if action == "lock" else 0
    elif action == "stats":
        value = int(args.get("value") or 0)
        for r in hit:
            for s in STATS:
                r[s] = max(0, min(9999, value))
    elif action == "aptitudes":
        value = max(1, min(8, int(args.get("value") or 8)))
        for r in hit:
            for a in APTITUDES:
                r[a] = value
    elif action == "rerank":
        for r in hit:
            r["rank"] = TC._rank_for_score(int(r.get("rank_score") or 0))
    else:
        raise OpError("Unknown action.")
    save(viewer, full_state)
    return {"changed": len(hit)}


@op("legacy.add", label="Add veteran {card_id}")
def add(viewer, args):
    full_state, roster = _roster(viewer, seed=True)
    card_id = int(args.get("card_id") or 0)
    if not card_id:
        raise OpError("Pick a card.")
    tid = _next_id(roster)
    if args.get("mode") == "generated":
        uma = next((u for u in TC._UMA_DATA if u.get("cardId") == card_id), None)
        rec = TC.build_generated_veteran(viewer, uma or TC._UMA_DATA[0], tid, random.Random(tid))
        rec["card_id"] = card_id
        rec["race_cloth_id"] = TC.dress_for_card(card_id, card_id // 100)
    else:
        rec = TC.build_maxed_veteran(viewer, card_id, tid)
    rec["create_time"] = rec["register_time"] = now_str()
    roster.append(rec)
    full_state[KEY] = roster
    save(viewer, full_state)
    return {"id": tid}


@op("legacy.clone", label="Clone veteran {id}")
def clone(viewer, args):
    full_state, roster = _roster(viewer, seed=True)
    tid = int(args.get("id") or 0)
    rec = next((r for r in roster if r.get("trained_chara_id") == tid), None)
    if rec is None:
        raise OpError(f"No veteran {tid}.")
    new = copy.deepcopy(rec)
    new["trained_chara_id"] = _next_id(roster)
    new["is_locked"] = 0
    new["create_time"] = new["register_time"] = now_str()
    roster.append(new)
    full_state[KEY] = roster
    save(viewer, full_state)
    return {"id": new["trained_chara_id"]}
