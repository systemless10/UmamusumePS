
from __future__ import annotations

import functools
from datetime import datetime

from app import concerts
from app import master_data as M
from app import paid_features as PF
from app.handlers import collection
from app.handlers import presents as P
from app.handlers import single_mode_team as T

from . import op
from .catalog import item_catalog, misc_catalog, support_catalog
from .common import OpError, TIME_FORMAT, find, ids_arg, int_arg, load, now_str, opt_int, save, set_item, text

CARAT_ITEM_ID = 43
MONEY_ITEM_ID = 59
INT32 = 2_147_483_647


@op("items.owned")
def items_owned(viewer, args):
    full_state = load(viewer)
    return {"items": {str(i.get("item_id")): i.get("number") or 0 for i in full_state.get("item_list_state") or []},
            "wallet": full_state.get("coin_info_state") or {},
            "tp": full_state.get("tp_info_state") or {}, "rp": full_state.get("rp_info_state") or {}}


@op("items.set", label="Items: set {changes} item(s)")
def items_set(viewer, args):
    full_state = load(viewer)
    changes = args.get("changes") or {}
    known = {i["id"] for i in item_catalog()}
    for iid, n in changes.items():
        iid, n = int(iid), int(n)
        if iid not in known:
            raise OpError(f"Item {iid} isn't in this region's master.")
        set_item(full_state, iid, max(0, min(INT32, n)))
    save(viewer, full_state)
    return {"changed": len(changes)}


@op("items.bulk", label="Items: {action}")
def items_bulk(viewer, args):
    full_state = load(viewer)
    action, group = args.get("action"), args.get("group") or ""
    if action not in ("give", "owned", "clear"):
        raise OpError("action is give, owned or clear.")
    count = int_arg(args, "count", 0, 0, INT32) if action != "clear" else 0
    cat = [i for i in item_catalog() if not group or i["group"] == group]
    owned = {i.get("item_id") for i in full_state.get("item_list_state") or []}
    n = 0
    for it in cat:
        if it["id"] == CARAT_ITEM_ID:
            continue
        if action == "give":
            if (find(full_state.get("item_list_state") or [], "item_id", it["id"]) or {}).get("number", 0) < count:
                set_item(full_state, it["id"], count)
                n += 1
        elif action == "owned" and it["id"] in owned:
            set_item(full_state, it["id"], count)
            n += 1
        elif action == "clear" and it["id"] in owned:
            set_item(full_state, it["id"], 0)
            n += 1
    save(viewer, full_state)
    return {"changed": n}


@op("wallet.set", label="Wallet")
def wallet_set(viewer, args):
    full_state = load(viewer)
    coin = full_state.setdefault("coin_info_state", {})
    for k in ("fcoin", "coin"):
        v = opt_int(args, k, 0, INT32)
        if v is not None:
            coin[k] = v
    tp = full_state.setdefault("tp_info_state", {})
    rp = full_state.setdefault("rp_info_state", {})
    for src, dst, field in (("tp", tp, "current_tp"), ("max_tp", tp, "max_tp"),
                            ("rp", rp, "current_rp"), ("max_rp", rp, "max_rp")):
        v = opt_int(args, src, 0, 99999)
        if v is not None:
            dst[field] = v
    money = opt_int(args, "money", 0, INT32)
    if money is not None:
        set_item(full_state, MONEY_ITEM_ID, money)
    save(viewer, full_state)
    return {"ok": True}


@op("pieces.set", label="Star pieces: {changes} uma(s)")
def pieces_set(viewer, args):
    full_state = load(viewer)
    pieces = full_state.setdefault("piece_list_state", [])
    for pid, n in (args.get("changes") or {}).items():
        pid, n = int(pid), max(0, int(n))
        p = find(pieces, "piece_id", pid)
        if p:
            p["piece_num"] = n
        elif n:
            pieces.append({"piece_id": pid, "piece_num": n})
    save(viewer, full_state)
    return {"ok": True}


@op("charas.list")
def charas_list(viewer, args):
    full_state = load(viewer)
    pieces = {p.get("piece_id"): p.get("piece_num") for p in full_state.get("piece_list_state") or []}
    return {"charas": full_state.get(collection.CHARA_LIST_KEY) or [], "pieces": pieces,
            "epithets": full_state.get("epithets_owned") or {}}


@op("charas.set", label="Characters: {field} on {ids}")
def charas_set(viewer, args):
    full_state = load(viewer)
    field = args.get("field")
    if field not in ("love_point", "fan", "training_num", "max_grade", "love_point_pool"):
        raise OpError("Unknown field.")
    value = int_arg(args, "value", 0, 0, INT32)
    charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
    n = 0
    for cid in ids_arg(args):
        row = find(charas, "chara_id", cid)
        if row is None:
            if not args.get("add_missing"):
                continue
            row = {"chara_id": cid, "training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                   "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0}
            charas.append(row)
        row[field] = value
        n += 1
    save(viewer, full_state)
    return {"changed": n}


@op("mail.list")
def mail_list(viewer, args):
    full_state = load(viewer)
    return (full_state.get(P.PRESENT_BOX_KEY) or {}).get("presents") or []


@op("mail.send", label="Mail: send {num}x {item_id}")
def mail_send(viewer, args):
    full_state = load(viewer)
    category = int_arg(args, "category", None, 0)
    item = int_arg(args, "item_id", None, 0)
    if category == 90:
        item = CARAT_ITEM_ID
    num = int_arg(args, "num", None, 1, INT32)
    copies = int_arg(args, "copies", 1, 1, 100)
    for _ in range(copies):
        P.send(full_state, category, item, num, str(args.get("message") or ""))
    save(viewer, full_state)
    return {"sent": copies}


@op("mail.delete", label="Mail: delete {ids}")
def mail_delete(viewer, args):
    full_state = load(viewer)
    box = full_state.get(P.PRESENT_BOX_KEY) or {}
    gone = set(ids_arg(args))
    before = len(box.get("presents") or [])
    box["presents"] = [p for p in box.get("presents") or [] if p.get("present_id") not in gone]
    full_state[P.PRESENT_BOX_KEY] = box
    save(viewer, full_state)
    return {"removed": before - len(box["presents"])}


@op("mail.clear", label="Mail: clear")
def mail_clear(viewer, args):
    full_state = load(viewer)
    box = full_state.get(P.PRESENT_BOX_KEY) or {}
    presents = box.get("presents") or []
    box["presents"] = [] if args.get("all") else [p for p in presents if p.get("state")]
    full_state[P.PRESENT_BOX_KEY] = box
    save(viewer, full_state)
    return {"removed": len(presents) - len(box["presents"])}


@op("paid.list")
def paid_list(viewer, args):
    full_state = load(viewer)
    return {"features": [{"name": n, "end": PF.end_time(full_state, n), "active": PF.active(full_state, n)}
                         for n in PF.FEATURES],
            "tickets": {"reroll": PF.item_count(full_state, PF.REROLL_TICKET_ITEM),
                        "order": PF.item_count(full_state, PF.ORDER_TICKET_ITEM)},
            "ticket_items": {"reroll": PF.REROLL_TICKET_ITEM, "order": PF.ORDER_TICKET_ITEM}}


@op("paid.grant", label="Paid feature: {name}")
def paid_grant(viewer, args):
    full_state = load(viewer)
    names = PF.FEATURES if args.get("name") == "all" else (args.get("name"),)
    for n in names:
        if n not in PF.FEATURES:
            raise OpError(f"Unknown feature {n}.")
        if args.get("revoke"):
            PF.revoke(full_state, n)
        else:
            PF.grant(full_state, n, int_arg(args, "days", 30, 1, 36500))
    save(viewer, full_state)
    return {"ok": True}


@op("honors.list")
def honors_list(viewer, args):
    full_state = load(viewer)
    return {"owned": (full_state.get("honor_state") or {}).get("honors") or {}}


@op("honors.set", label="Titles: {action} {ids}")
def honors_set(viewer, args):
    full_state = load(viewer)
    st = full_state.setdefault("honor_state", {})
    owned = st.setdefault("honors", {})
    ids = ids_arg(args)
    if args.get("all"):
        ids = [h["id"] for h in misc_catalog()["honors"]]
    for hid in ids:
        if args.get("action") == "remove":
            owned.pop(str(hid), None)
        else:
            owned.setdefault(str(hid), now_str())
    save(viewer, full_state)
    return {"owned": len(owned)}


@op("epithets.set", label="Epithets: chara {chara_id}")
def epithets_set(viewer, args):
    full_state = load(viewer)
    st = full_state.setdefault("epithets_owned", {})
    chara = str(int_arg(args, "chara_id", None, 1))
    st[chara] = sorted({int(x) for x in args.get("ids") or []})
    save(viewer, full_state)
    return {"owned": len(st[chara])}


@functools.lru_cache(maxsize=1)
def _songs() -> list:
    rows = M.query("SELECT music_id, start_date FROM live_data ORDER BY music_id") if M.has_table("live_data") else []
    seen, out = set(), []
    for r in rows:
        if r["music_id"] in seen:
            continue
        seen.add(r["music_id"])
        out.append({"id": r["music_id"], "name": text(16, r["music_id"]) or f"Song {r['music_id']}",
                    "released": bool(r["start_date"] and r["start_date"] <= datetime.now().timestamp())})
    return out


@op("music.list")
def music_list(viewer, args):
    full_state = load(viewer)
    return {"songs": _songs(), "owned": sorted(concerts.owned(full_state))}


@op("music.set", label="Songs: {action} {ids}")
def music_set(viewer, args):
    full_state = load(viewer)
    ids = [s["id"] for s in _songs() if s["released"]] if args.get("all") else ids_arg(args)
    n = 0
    if args.get("action") == "remove":
        gone = set(ids)
        lst = full_state.get(concerts.MUSIC_LIST_KEY) or []
        full_state[concerts.MUSIC_LIST_KEY] = [m for m in lst if m.get("music_id") not in gone]
        n = len(lst) - len(full_state[concerts.MUSIC_LIST_KEY])
    else:
        for mid in ids:
            if concerts.grant(full_state, mid):
                n += 1
    save(viewer, full_state)
    return {"changed": n}


@op("friends.list")
def friends_list(viewer, args):
    full_state = load(viewer)
    out = []
    for i, f in enumerate(full_state.get(T.FRIEND_SUPPORT_INJECT_KEY) or []):
        usc = f.get("user_support_card") or {}
        out.append({"index": i, "name": f.get("name"), "viewer_id": f.get("viewer_id"),
                    "support_card_id": usc.get("support_card_id") or f.get("support_card_id"),
                    "lb": usc.get("limit_break_count"), "exp": usc.get("exp")})
    return out


@op("friends.add", label="Friend card {support_card_id}")
def friends_add(viewer, args):
    sid = int_arg(args, "support_card_id", None, 1)
    if sid not in {c["id"] for c in support_catalog()["cards"]}:
        raise OpError("Not a support card in this region's master.")
    entry = T.inject_friend_support_card(viewer, sid, level=opt_int(args, "level", 1, 50),
                                         limit_break_count=opt_int(args, "lb", 0, 4),
                                         name=str(args.get("name") or "Friend"))
    return {"name": entry.get("name")}


@op("friends.remove", label="Remove friend cards")
def friends_remove(viewer, args):
    full_state = load(viewer)
    lst = full_state.get(T.FRIEND_SUPPORT_INJECT_KEY) or []
    drop = set(ids_arg(args, "indexes"))
    full_state[T.FRIEND_SUPPORT_INJECT_KEY] = [f for i, f in enumerate(lst) if i not in drop]
    save(viewer, full_state)
    return {"removed": len(lst) - len(full_state[T.FRIEND_SUPPORT_INJECT_KEY])}


__all__ = ["TIME_FORMAT"]
