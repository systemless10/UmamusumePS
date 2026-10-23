
from __future__ import annotations

from app.handlers import collection

from . import op
from .catalog import support_catalog
from .common import OpError, ids_arg, load, now_str, opt_int, save

KEY = collection.SUPPORT_CARD_KEY


def _exp_for(rarity: int, lb: int, level) -> int | None:
    cat = support_catalog()
    cap = cat["limits"].get(rarity, [50] * 5)[max(0, min(4, lb))]
    table = cat["levels"].get(rarity) or []
    if level == "max":
        level = cap
    level = max(1, min(cap, int(level)))
    exp = 0
    for lv, total in table:
        if lv <= level:
            exp = total
    return exp


def _level_of(rarity: int, exp: int) -> int:
    level = 1
    for lv, total in support_catalog()["levels"].get(rarity) or []:
        if exp >= total:
            level = lv
    return level


@op("supports.owned")
def owned(viewer, args):
    full_state = load(viewer)
    rarity = {c["id"]: c["rarity"] for c in support_catalog()["cards"]}
    out = []
    for e in full_state.get(KEY) or []:
        sid = e.get("support_card_id")
        out.append({"id": sid, "exp": e.get("exp") or 0, "lb": e.get("limit_break_count") or 0,
                    "stock": e.get("stock") or 0, "favorite": e.get("favorite_flag") or 0,
                    "level": _level_of(rarity.get(sid, 1), e.get("exp") or 0),
                    "created": e.get("create_time")})
    decks = full_state.get(collection.SUPPORT_DECK_KEY) or []
    return {"cards": out, "decks": decks}


@op("supports.apply", label="Support cards: {action} {ids}")
def apply(viewer, args):
    full_state = load(viewer)
    ids = ids_arg(args)
    action = args.get("action")
    known = {c["id"]: c for c in support_catalog()["cards"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise OpError(f"Not support cards in this region's master: {unknown[:8]}")
    entries = full_state.setdefault(KEY, [])
    by_id = {e.get("support_card_id"): e for e in entries if isinstance(e, dict)}
    lb = opt_int(args, "lb", 0, 4)
    level = args.get("level")
    if level not in (None, "", "max"):
        level = opt_int(args, "level", 1, 50)
    stock = opt_int(args, "stock", 0, 999)
    favorite = args.get("favorite")
    added = changed = removed = 0

    if action == "remove":
        gone = set(ids)
        full_state[KEY] = [e for e in entries if e.get("support_card_id") not in gone]
        removed = len(entries) - len(full_state[KEY])
        for deck in full_state.get(collection.SUPPORT_DECK_KEY) or []:
            arr = deck.get("support_card_id_array") or []
            deck["support_card_id_array"] = [0 if x in gone else x for x in arr]
        save(viewer, full_state)
        return {"removed": removed}

    if action not in ("give", "set"):
        raise OpError("action is give, set or remove.")
    for sid in ids:
        e = by_id.get(sid)
        if e is None:
            if action == "set" and not args.get("give_missing"):
                continue
            e = {"viewer_id": "<redacted>", "support_card_id": sid, "exp": 0, "limit_break_count": 0,
                 "favorite_flag": 0, "stock": 0, "possess_time": now_str(), "create_time": now_str()}
            entries.append(e)
            by_id[sid] = e
            added += 1
        elif action == "give":
            continue
        rarity = known[sid]["rarity"]
        if lb is not None:
            e["limit_break_count"] = lb
        cur_lb = int(e.get("limit_break_count") or 0)
        if level not in (None, ""):
            e["exp"] = _exp_for(rarity, cur_lb, level)
        elif lb is not None:
            e["exp"] = min(int(e.get("exp") or 0), _exp_for(rarity, cur_lb, "max"))
        if stock is not None:
            e["stock"] = stock
        if favorite is not None and favorite != "":
            e["favorite_flag"] = 1 if favorite in (1, True, "1", "true") else 0
        changed += 1
    save(viewer, full_state)
    return {"added": added, "changed": changed}
