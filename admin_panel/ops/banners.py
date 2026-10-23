
from __future__ import annotations

import json
import os
import re
import time

from app import master_data
from app.handlers import gacha

from . import op
from .common import OpError

REAL_RATES = {"1": 79, "2": 18, "3": 3}
FOREVER = 2**31 - 1
_BOOST = re.compile(r"^x(\d+(?:\.\d+)?)$")


def _read() -> dict:
    try:
        with open(gacha.odds_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as ex:
        raise OpError(f"{gacha.odds_path()} isn't valid JSON: {ex}") from ex
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = gacha.odds_path()
    data = {k: v for k, v in data.items() if not isinstance(v, dict) or v or k == "*"}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _now() -> int:
    return gacha._servertime()


def _texts(cat: int) -> dict:
    return {r["index"]: r["text"] for r in master_data.query(
        "SELECT [index], text FROM text_data WHERE category=?", (cat,))}


def _catalog() -> dict:
    banners = {r["id"]: dict(r, ported=False) for r in master_data.query(
        "SELECT id, card_type, start_date, end_date, cost_type FROM gacha_data")}
    pools: dict = {}
    for r in master_data.query("SELECT gacha_id, card_id, rarity, odds, is_pickup FROM gacha_available"):
        pools.setdefault(r["gacha_id"], []).append(dict(r))
    title, desc = _texts(26), _texts(13)
    names = {**_texts(4), **_texts(75)}
    try:
        from app import custom_support as C
        bundles = C.unreleased_bundles()
    except Exception:
        bundles = ()
    for b in bundles:
        t = b.get("tables") or {}
        for r in t.get("gacha_data") or []:
            banners[r["id"]] = {k: r.get(k) for k in ("id", "card_type", "start_date", "end_date", "cost_type")} | {"ported": True}
        for r in t.get("gacha_available") or []:
            pools.setdefault(r["gacha_id"], [])
            if not any(c["card_id"] == r["card_id"] for c in pools[r["gacha_id"]]):
                pools[r["gacha_id"]].append({k: r[k] for k in ("gacha_id", "card_id", "rarity", "odds", "is_pickup")})
        for r in t.get("text_data") or []:
            {26: title, 13: desc, 4: names, 75: names}.get(r["category"], {}).setdefault(r["index"], r["text"])
    return {"banners": banners, "pools": pools, "title": title, "desc": desc, "names": names}


def _split(name: str) -> tuple[str, str]:
    m = re.match(r"^(\[.*?\])\s*(.*)$", name or "")
    return (m.group(2), m.group(1)) if m else (name or "", "")


def _state(sec: dict) -> str:
    return "auto" if "on" not in sec else ("on" if sec["on"] else "off")


def _boosts(sec: dict) -> dict:
    out = {}
    for e in sec.get("custom_odds") or []:
        if isinstance(e, dict) and str(e.get("items", "")).isdigit():
            m = _BOOST.match(str(e.get("weight", "")))
            if m:
                out[str(e["items"])] = float(m.group(1))
    return out


def _advanced(sec: dict) -> int:
    n = 0
    for e in sec.get("custom_odds") or []:
        ok = isinstance(e, dict) and str(e.get("items", "")).isdigit() and _BOOST.match(str(e.get("weight", "")))
        n += not ok
    return n


def _listed(data: dict, gid: int, b: dict, now: int) -> tuple[bool, str]:
    sec = data.get(str(gid)) or {}
    start, end = b.get("start_date") or 0, b.get("end_date") or 0
    if end >= FOREVER or end > now + 20 * 365 * 86400:
        end = 0
    st = _state(sec)
    if st == "off":
        return False, "Hidden"
    if st == "on":
        at = sec.get("toggled_at")
        if not at or not end:
            return True, "Toggled on"
        if int(at) > end:
            return True, "Toggled on (brought back)"
        return (now <= end), ("Toggled on until it ends" if now <= end else "Ended")
    if start > now:
        return False, "Upcoming"
    if (data.get("*") or {}).get("on") is True or not data:
        return True, "Every banner listed"
    if start <= now and (not end or now <= end):
        return True, "Running now"
    return False, "Ended"


@op("banners.list")
def list_(viewer, args):
    data, cat, now = _read(), _catalog(), _now()
    out = []
    for gid, b in cat["banners"].items():
        pool = cat["pools"].get(gid) or []
        if not pool:
            continue
        sec = data.get(str(gid)) or {}
        listed, why = _listed(data, gid, b, now)
        picks = sorted([c for c in pool if c["is_pickup"]], key=lambda c: (-c["rarity"], c["card_id"]))
        cards = sorted(pool, key=lambda c: (-c["rarity"], -c["is_pickup"], c["card_id"]))
        total = sum(c["odds"] for c in pool) or 1

        def card(c):
            name, title = _split(cat["names"].get(c["card_id"], ""))
            return {"id": c["card_id"], "name": name or f"Card {c['card_id']}", "title": title,
                    "rarity": c["rarity"], "pickup": bool(c["is_pickup"]), "pct": c["odds"] / total * 100}
        end = b.get("end_date") or 0
        out.append({
            "id": gid, "kind": "support" if b.get("card_type") == 2 else "uma",
            "title": cat["title"].get(gid) or ("Support Card Scout" if b.get("card_type") == 2 else "Trainee Scout"),
            "subtitle": cat["desc"].get(gid, ""), "ported": b["ported"],
            "start": b.get("start_date") or 0, "end": 0 if end >= FOREVER or end > now + 20 * 365 * 86400 else end,
            "pickups": [card(c) for c in picks[:6]],
            "cards": [card(c) for c in cards] if args.get("id") == gid else None,
            "state": _state(sec), "listed": listed, "why": why,
            "rates": sec.get("rarity_rates") or None, "boosts": _boosts(sec), "advanced": _advanced(sec),
        })
    out.sort(key=lambda r: (not r["listed"], r["start"] > now, r["start"] if r["start"] > now else -r["start"], -r["id"]))
    star = data.get("*") or {}
    return {"path": gacha.odds_path(), "now": now, "exists": bool(data),
            "mode": "all" if not data or star.get("on") is True else "schedule",
            "rates": star.get("rarity_rates") or None, "real_rates": REAL_RATES, "banners": out}


@op("banners.cards")
def cards(viewer, args):
    gid = int(args["id"])
    r = list_(viewer, {"id": gid})
    return next((b for b in r["banners"] if b["id"] == gid), None)


def _rates(v):
    if not v:
        return None
    try:
        out = {k: float(v[k]) for k in ("1", "2", "3") if v.get(k) not in (None, "")}
    except (TypeError, ValueError) as ex:
        raise OpError("Rates must be numbers.") from ex
    if any(x < 0 or x > 100 for x in out.values()):
        raise OpError("Each rate must be between 0 and 100%.")
    if out and not any(out.values()):
        raise OpError("At least one rarity needs a rate above 0%.")
    return None if out == {k: float(x) for k, x in REAL_RATES.items()} else (out or None)


@op("banners.mode", label="Banner listing")
def mode(viewer, args):
    data = _read()
    star = dict(data.get("*") or {})
    if args.get("mode") == "all":
        star["on"] = True
    elif args.get("mode") == "schedule":
        star.pop("on", None)
    else:
        raise OpError("Unknown mode.")
    if "rates" in args:
        r = _rates(args["rates"])
        star.pop("rarity_rates", None)
        if r:
            star["rarity_rates"] = r
    data["*"] = star
    if data == {"*": {"on": True}} and os.path.exists(gacha.odds_path()):
        os.remove(gacha.odds_path())
    elif data != {"*": {"on": True}}:
        _write(data)
    return list_(viewer, {})


@op("banners.set", label="Banner settings")
def set_(viewer, args):
    gid = int(args["id"])
    data = _read()
    if not data:
        data = {"*": {"on": True}}
    sec = dict(data.get(str(gid)) or {})
    if "state" in args:
        st = args["state"]
        if st == "on":
            if sec.get("on") is not True:
                sec["toggled_at"] = _now()
            sec["on"] = True
        elif st == "off":
            sec["on"] = False
            sec.pop("toggled_at", None)
        elif st == "auto":
            sec.pop("on", None)
            sec.pop("toggled_at", None)
        else:
            raise OpError("Unknown state.")
    if "rates" in args:
        r = _rates(args["rates"])
        sec.pop("rarity_rates", None)
        if r:
            sec["rarity_rates"] = r
    if "boosts" in args:
        kept = [e for e in sec.get("custom_odds") or []
                if not (isinstance(e, dict) and str(e.get("items", "")).isdigit() and _BOOST.match(str(e.get("weight", ""))))]
        new = []
        for cid, x in (args["boosts"] or {}).items():
            try:
                x = float(x)
            except (TypeError, ValueError) as ex:
                raise OpError("Boosts must be numbers.") from ex
            if not 0 <= x <= 1000:
                raise OpError("A boost must be between ×0 and ×1000.")
            if x != 1:
                new.append({"items": str(int(cid)), "weight": f"x{x:g}"})
        sec.pop("custom_odds", None)
        if kept + new:
            sec["custom_odds"] = kept + new
    data[str(gid)] = sec
    _write(data)
    return cards(viewer, {"id": gid})


@op("banners.real", label="Real banner lineup")
def real(viewer, args):
    data, cat = _read(), _catalog()
    star = dict(data.get("*") or {})
    star.pop("on", None)
    data["*"] = star
    for key in [k for k in data if k.isdigit()]:
        sec = dict(data[key])
        if sec.get("on") is True:
            sec.pop("on", None)
            sec.pop("toggled_at", None)
        if args.get("rates"):
            for k in ("rarity_rates", "custom_odds"):
                sec.pop(k, None)
        data[key] = sec
    if args.get("rates"):
        star.pop("rarity_rates", None)
    for gid, b in cat["banners"].items():
        if b["ported"] and cat["pools"].get(gid):
            data[str(gid)] = dict(data.get(str(gid)) or {}, on=False)
    _write(data)
    return list_(viewer, {})


@op("banners.all", label="Enable all banners")
def enable_all(viewer, args):
    data = _read()
    data["*"] = dict(data.get("*") or {}, on=True)
    for key in [k for k in data if k.isdigit()]:
        sec = dict(data[key])
        if sec.get("on") is False:
            sec.pop("on", None)
        data[key] = sec
    if data == {"*": {"on": True}} or all(not v for k, v in data.items() if k != "*") and data["*"] == {"on": True}:
        if os.path.exists(gacha.odds_path()):
            os.remove(gacha.odds_path())
    else:
        _write(data)
    return list_(viewer, {})
