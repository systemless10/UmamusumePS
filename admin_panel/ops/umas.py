
from __future__ import annotations

import functools

from app import master_data as M
from app.handlers import collection

from . import op
from .catalog import max_hint_level, uma_catalog
from .common import OpError, find, ids_arg, load, now_str, save, skill_name

KEY = collection.CARD_LIST_KEY
CHARA_KEY = collection.CHARA_LIST_KEY


@functools.lru_cache(maxsize=None)
def awakening_skills(card_id: int) -> list[dict]:
    row = M.query_one("SELECT available_skill_set_id FROM card_data WHERE id=?", (card_id,))
    if not row:
        return []
    out = []
    for r in M.query("SELECT a.skill_id, a.need_rank, s.rarity FROM available_skill_set a "
                     "LEFT JOIN skill_data s ON s.id = a.skill_id WHERE a.available_skill_set_id=? "
                     "ORDER BY a.need_rank, a.id", (row["available_skill_set_id"],)):
        out.append({"skill_id": r["skill_id"], "need_rank": r["need_rank"], "rarity": r["rarity"] or 1,
                    "name": skill_name(r["skill_id"])})
    return out


def _cards() -> dict:
    return {c["id"]: c for c in uma_catalog()["cards"]}


def _ensure_chara(full_state, chara_id) -> None:
    charas = full_state.setdefault(CHARA_KEY, [])
    if not find(charas, "chara_id", chara_id):
        charas.append({"chara_id": chara_id, "training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                       "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0})


def _set_hints(entry: dict, level, all_skills: bool) -> None:
    talent = int(entry.get("talent_level") or 1)
    cap = max_hint_level(talent)
    skills = entry.setdefault("skill_data_array", [])
    by_id = {s.get("skill_id"): s for s in skills if isinstance(s, dict)}
    for sk in awakening_skills(entry["card_id"]):
        if not all_skills and sk["need_rank"] > talent:
            continue
        want = cap if level == "max" else max(0, min(cap, int(level)))
        cur = by_id.get(sk["skill_id"])
        if want <= 0:
            if cur:
                skills.remove(cur)
            continue
        if cur:
            cur["level"] = want
        else:
            cur = {"skill_id": sk["skill_id"], "level": want}
            skills.append(cur)
            by_id[sk["skill_id"]] = cur


def _clamp_hints(entry: dict) -> None:
    cap = max_hint_level(int(entry.get("talent_level") or 1))
    for s in entry.get("skill_data_array") or []:
        if isinstance(s, dict) and (s.get("level") or 0) > cap:
            s["level"] = cap


@op("umas.owned")
def owned(viewer, args):
    full_state = load(viewer)
    out = []
    for e in full_state.get(KEY) or []:
        hints = [s.get("level") or 0 for s in e.get("skill_data_array") or [] if isinstance(s, dict)]
        out.append({"id": e.get("card_id"), "rarity": e.get("rarity"), "talent": e.get("talent_level"),
                    "hints": len(hints), "hint_max": max(hints, default=0), "created": e.get("create_time")})
    charas = {c.get("chara_id"): {k: c.get(k) for k in ("love_point", "fan", "training_num", "max_grade")}
              for c in full_state.get(CHARA_KEY) or [] if isinstance(c, dict)}
    pieces = {p.get("piece_id"): p.get("piece_num") for p in full_state.get("piece_list_state") or []}
    return {"cards": out, "charas": charas, "pieces": pieces}


@op("umas.detail")
def detail(viewer, args):
    full_state = load(viewer)
    card_id = int(args.get("card_id") or 0)
    meta = _cards().get(card_id)
    if not meta:
        raise OpError(f"Card {card_id} isn't in this region's master.")
    entry = find(full_state.get(KEY) or [], "card_id", card_id)
    levels = {s.get("skill_id"): s.get("level") for s in (entry or {}).get("skill_data_array") or []
              if isinstance(s, dict)}
    chara = find(full_state.get(CHARA_KEY) or [], "chara_id", meta["chara_id"])
    piece = find(full_state.get("piece_list_state") or [], "piece_id", meta["piece_id"])
    return {
        "card": meta, "owned": entry is not None, "entry": entry,
        "skills": [{**s, "level": levels.get(s["skill_id"], 0)} for s in awakening_skills(card_id)],
        "extra_hints": [{"skill_id": k, "level": v, "name": skill_name(k)} for k, v in levels.items()
                        if k not in {s["skill_id"] for s in awakening_skills(card_id)}],
        "chara": chara, "pieces": (piece or {}).get("piece_num") or 0,
        "dress": (full_state.get("card_dress_state") or {}).get(str(card_id)),
    }


@op("umas.apply", label="Umas: {action} {ids}")
def apply(viewer, args):
    full_state = load(viewer)
    ids = ids_arg(args)
    action = args.get("action")
    cards = _cards()
    unknown = [i for i in ids if i not in cards]
    if unknown:
        raise OpError(f"Not trainee cards in this region's master: {unknown[:8]}")
    entries = full_state.setdefault(KEY, [])
    by_id = {e.get("card_id"): e for e in entries if isinstance(e, dict)}

    if action == "remove":
        gone = set(ids)
        full_state[KEY] = [e for e in entries if e.get("card_id") not in gone]
        save(viewer, full_state)
        return {"removed": len(entries) - len(full_state[KEY])}
    if action not in ("give", "set"):
        raise OpError("action is give, set or remove.")

    rarity, talent, hints = args.get("rarity"), args.get("talent"), args.get("hints")
    added = changed = 0
    for cid in ids:
        meta = cards[cid]
        e = by_id.get(cid)
        if e is None:
            if action == "set" and not args.get("give_missing"):
                continue
            e = {"card_id": cid, "rarity": meta["default_rarity"], "talent_level": 1,
                 "create_time": now_str(), "skill_data_array": []}
            entries.append(e)
            by_id[cid] = e
            _ensure_chara(full_state, meta["chara_id"])
            added += 1
        elif action == "give":
            continue
        if rarity not in (None, ""):
            r = meta["max_rarity"] if rarity == "max" else int(rarity)
            e["rarity"] = max(meta["default_rarity"], min(meta["max_rarity"], r))
        if talent not in (None, ""):
            top = meta["max_talent"]
            t = top if talent == "max" else int(talent)
            e["talent_level"] = max(1, min(top, t))
            _clamp_hints(e)
        if hints not in (None, ""):
            _set_hints(e, hints, bool(args.get("all_skills")))
        changed += 1
    save(viewer, full_state)
    return {"added": added, "changed": changed}


@op("umas.save", label="Edit uma {card_id}")
def save_one(viewer, args):
    full_state = load(viewer)
    card_id = int(args.get("card_id") or 0)
    meta = _cards().get(card_id)
    if not meta:
        raise OpError(f"Card {card_id} isn't in this region's master.")
    entries = full_state.setdefault(KEY, [])
    e = find(entries, "card_id", card_id)
    if e is None:
        e = {"card_id": card_id, "rarity": meta["default_rarity"], "talent_level": 1,
             "create_time": now_str(), "skill_data_array": []}
        entries.append(e)
        _ensure_chara(full_state, meta["chara_id"])
    if args.get("rarity") is not None:
        e["rarity"] = max(meta["default_rarity"], min(meta["max_rarity"], int(args["rarity"])))
    if args.get("talent_level") is not None:
        e["talent_level"] = max(1, min(meta["max_talent"], int(args["talent_level"])))
    if args.get("create_time"):
        e["create_time"] = str(args["create_time"])
    if isinstance(args.get("skills"), list):
        cap = max_hint_level(int(e.get("talent_level") or 1))
        e["skill_data_array"] = [{"skill_id": int(s["skill_id"]), "level": max(1, min(cap, int(s["level"])))}
                                 for s in args["skills"] if int(s.get("level") or 0) > 0]
    chara = args.get("chara")
    if isinstance(chara, dict):
        _ensure_chara(full_state, meta["chara_id"])
        row = find(full_state[CHARA_KEY], "chara_id", meta["chara_id"])
        for k in ("love_point", "fan", "training_num", "max_grade", "love_point_pool"):
            if chara.get(k) not in (None, ""):
                row[k] = int(chara[k])
    if args.get("pieces") is not None and meta["piece_id"]:
        pieces = full_state.setdefault("piece_list_state", [])
        p = find(pieces, "piece_id", meta["piece_id"])
        n = max(0, int(args["pieces"]))
        if p:
            p["piece_num"] = n
        elif n:
            pieces.append({"piece_id": meta["piece_id"], "piece_num": n})
    save(viewer, full_state)
    return {"card_id": card_id}
