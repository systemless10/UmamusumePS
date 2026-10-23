
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events", "gametora")
_INDEX_PATH = os.path.join(_DATA_DIR, "_url_index.json")
_SUPPLEMENT_PATH = os.path.join(os.path.dirname(_DATA_DIR), "gametora_supplement.json")
_SITEMAP = "https://gametora.com/sitemap-0.xml"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PrivateServer-EventDumper"

_CODE_TYPE = {
    "sp": "speed", "st": "stamina", "po": "power", "gu": "guts", "in": "wisdom",
    "en": "energy", "me": "max_energy", "mo": "mood", "pt": "skill_points",
    "sk": "skill_hint", "srh": "skill_hint", "sg": "obtain_skill",
    "5s": "all_stats", "rs": "random_stats", "hi_s": "highest_stat",
    "lo_s": "lowest_stat", "ls": "last_stat", "fe": "full_energy",
    "bo": "bond", "bo_ch": "bond", "bo_l": "bond", "bo_r": "bond",
    "se": "condition", "he": "heal_status", "ha": "heal_status",
    "fa": "fans", "rr": "race_rewards",
    "rc": "race_change",
    "ra": "race_cancel",
    "rl": "race_restrict",
    "ee": "chain_end",
    "brian_tryhard": "branch_flag",
}
_SKIP_CODES = {"di", "di_s", "no", "ds", "fd", "et", "sl", "nsl",
               "brp", "brf", "rh"}

_GUARD_CODES = {
    "mood_min": "mood_min",
    "mood_max": "mood_max",
    "result_bad": "result_bad",
    "co": "season",
    "pl": "finish_position",
    "w_e": "win_streak",
    "ps_h": "has_skill",
    "ps_nh": "lacks_skill",
    "se_h": "has_condition",
    "se_nh": "lacks_condition",
    "bp2": "bond_level",
    "sc": "scripted",
    "ct": "count",
    "s_nore": "no_rest",
    "highest_facility": "highest_facility",
    "fans_minimum": "fans_min",
    "fans_maximum": "fans_max",
    "result_good": "result_good",
    "result_average": "result_average",
    "success": "success",
    "fail": "fail",
    "most_trained": "most_trained",
    "expensive_races": "expensive_races",
    "other_cases": "otherwise",
}


def _fetch(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"fetch failed {url}: {last}")


def build_index() -> dict:
    xml = _fetch(_SITEMAP)
    index = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
        m = re.match(r"https://gametora\.com/umamusume/(supports|characters)/(\d+)-", loc)
        if not m:
            continue
        kind = "support" if m.group(1) == "supports" else "chara"
        index[f"{kind}:{m.group(2)}"] = loc
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_INDEX_PATH, "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return index


_index_cache: dict | None = None


def _url_for(kind: str, id_: int) -> str | None:
    global _index_cache
    if _index_cache is None:
        if os.path.exists(_INDEX_PATH):
            with open(_INDEX_PATH, encoding="utf-8") as fh:
                _index_cache = json.load(fh)
        else:
            _index_cache = build_index()
    return _index_cache.get(f"{kind}:{id_}")


def _page_event_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ on page")
    ev = json.loads(m.group(1))["props"]["pageProps"]["eventData"]
    if not isinstance(ev, dict):
        return {}
    return {lang: (json.loads(v) if isinstance(v, str) else v)
            for lang, v in ev.items() if v}


def _extract_event_data(html: str) -> dict:
    en = _page_event_data(html).get("en")
    if not en:
        raise RuntimeError("no English eventData")
    return en


def _split_branches(effects: list) -> tuple[list, list]:
    segments = []
    cur_prob, cur = None, []
    for r in effects or ():
        if r.get("t") == "di":
            segments.append((cur_prob, cur))
            cur_prob, cur = _parse_prob(r.get("d")), []
        else:
            cur.append(r)
    segments.append((cur_prob, cur))
    if len(segments) > 1 and not segments[0][1] and segments[0][0] is None:
        segments = segments[1:]
    return [s[1] for s in segments], [s[0] for s in segments]


def _parse_prob(d):
    if isinstance(d, str) and d.startswith("~"):
        try:
            return int(d[1:]) / 100.0
        except ValueError:
            return None
    return None


def _norm_effect(r: dict):
    t = r.get("t")
    if t in _SKIP_CODES:
        return None
    if t == "sr":
        opts = [{"skill_id": s.get("d"), "value": s.get("v")}
                for s in (r.get("d") or []) if isinstance(s, dict)]
        return {"type": "skill_hint_or", "options": opts} if opts else None
    typ = _CODE_TYPE.get(t, t)
    out = {"type": typ}
    if r.get("v") is not None:
        out["value"] = r["v"]
    if typ in ("skill_hint", "obtain_skill"):
        out["skill_id"] = r.get("d")
    elif typ == "condition":
        out["condition_id"] = r.get("d")
    elif typ == "bond":
        if r.get("d") is not None:
            out["char_id"] = r.get("d")
    elif typ == "random_stats":
        out["stat_count"] = r.get("d")
    elif typ in ("race_change", "race_cancel"):
        out["race_instance_id"] = r.get("d")
    elif typ == "race_restrict":
        out["turns"] = r.get("d")
    elif typ == "branch_flag":
        out["flag"] = t
    return out


def _split_segments(rewards: list) -> list:
    out: list = [([], [])]
    for r in rewards or ():
        t = r.get("t")
        if t == "nl":
            out.append(([], []))
        elif t in _GUARD_CODES:
            if out[-1][0] or out[-1][1]:
                out.append(([], []))
            g = {"kind": _GUARD_CODES[t]}
            if r.get("d") is not None:
                g["value"] = r["d"]
            if r.get("v") is not None:
                g["amount"] = r["v"]
            out[-1][0].append(g)
        else:
            out[-1][1].append(r)
    return [(gu, rw) for gu, rw in out if gu or rw]


def _norm_effects(rewards: list) -> list:
    out = []
    for r in rewards or ():
        e = _norm_effect(r)
        if e is None:
            continue
        out.extend(e if isinstance(e, list) else [e])
    return out


def _branch_payload(rewards: list) -> dict:
    branches, probs = _split_branches([r for r in rewards or () if r.get("t") != "nl"])
    norm = [_norm_effects(br) for br in branches]
    if len(norm) <= 1:
        return {"effects": norm[0] if norm else [], "random_either": False}
    return {"outcomes": norm, "random_either": True, "probs": [p for p in probs]}


def _norm_choice(c: dict) -> dict:
    raw = c.get("r") or []
    segments = _split_segments(raw)
    if any(guards for guards, _ in segments):
        norm = [dict(_branch_payload(rewards), when=guards)
                for guards, rewards in segments]
        base = next((s for s in norm if not s["when"]), norm[0])
        out = {"label": c.get("o", ""), "segments": norm}
        out.update({k: v for k, v in base.items() if k != "when"})
        return out
    return dict(_branch_payload(raw), label=c.get("o", ""))


def _norm_event(e: dict, section: str | None = None) -> dict:
    out = {"name": e.get("n"), "id": e.get("i"),
           "choices": [_norm_choice(c) for c in e.get("c") or []]}
    if section:
        out["section"] = section
    cond = e.get("conditions")
    if cond:
        out["conditions"] = cond
    et = e.get("type")
    if et:
        out["event_type"] = et
    return out


SPECIAL_STAT_KEYS = ("nyear", "dance")
_SPECIAL_KEY = "__special__"


def normalize(event_data: dict) -> dict:
    out = {}
    for sec, items in event_data.items():
        if not (isinstance(items, list) and items and isinstance(items[0], dict)):
            continue
        for e in items:
            if not e.get("n"):
                continue
            out[_norm_title(e["n"])] = _norm_event(e, sec)
    special = {k: event_data[k] for k in SPECIAL_STAT_KEYS if event_data.get(k)}
    if special:
        out[_SPECIAL_KEY] = special
    return out


def _norm_title(s: str | None) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower(), flags=re.UNICODE)


def _supplement_for(kind: str, id_: int) -> list:
    try:
        with open(_SUPPLEMENT_PATH, encoding="utf-8") as fh:
            return list(json.load(fh).get(f"{kind}:{id_}") or ())
    except (OSError, ValueError):
        return []


def _merge_supplement(events: dict, kind: str, id_: int) -> int:
    added = 0
    for ev in _supplement_for(kind, id_):
        key = _norm_title(ev.get("name"))
        if key and key not in events:
            events[key] = json.loads(json.dumps(ev))
            added += 1
    return added


def apply_supplement() -> int:
    try:
        with open(_SUPPLEMENT_PATH, encoding="utf-8") as fh:
            keys = [k for k in json.load(fh) if ":" in k]
    except (OSError, ValueError):
        return 0
    total = 0
    for key in keys:
        kind, id_ = key.split(":")
        path = os.path.join(_DATA_DIR, f"{kind}_{id_}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        added = sum(_merge_supplement(doc[lang], kind, int(id_))
                    for lang in ("events", "events_ja") if isinstance(doc.get(lang), dict))
        if added:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
            total += added
    return total


def dump(kind: str, id_: int, force: bool = False) -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, f"{kind}_{id_}.json")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    url = _url_for(kind, id_)
    if not url:
        raise RuntimeError(f"no GameTora URL for {kind}:{id_} (rebuild index?)")
    langs = _page_event_data(_fetch(url))
    if not langs.get("en"):
        raise RuntimeError("no English eventData")
    events = normalize(langs["en"])
    _merge_supplement(events, kind, id_)
    doc = {"kind": kind, "id": id_, "url": url, "events": events}
    if langs.get("ja"):
        doc["events_ja"] = normalize(langs["ja"])
        _merge_supplement(doc["events_ja"], kind, id_)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    return events


def dump_ja(kind: str, id_: int, force: bool = False) -> int:
    path = os.path.join(_DATA_DIR, f"{kind}_{id_}.json")
    if not os.path.exists(path):
        dump(kind, id_)
        with open(path, encoding="utf-8") as fh:
            return len(json.load(fh).get("events_ja") or {})
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("events_ja") and not force:
        return len(doc["events_ja"])
    url = doc.get("url") or _url_for(kind, id_)
    if not url:
        raise RuntimeError(f"no GameTora URL for {kind}:{id_} (rebuild index?)")
    ja = _page_event_data(_fetch(url)).get("ja")
    if not ja:
        raise RuntimeError("no Japanese eventData")
    doc["events_ja"] = normalize(ja)
    _merge_supplement(doc["events_ja"], kind, id_)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    return len(doc["events_ja"])


def _jp_events(doc: dict) -> dict | None:
    en = doc.get("events") or {}
    ja = doc.get("events_ja")
    if not ja:
        return en or None
    by_id = {(e.get("section"), e.get("id")): e for k, e in ja.items()
             if isinstance(e, dict) and e.get("id") is not None}
    out = {}
    for key, ev in en.items():
        twin = by_id.get((ev.get("section"), ev.get("id"))) if isinstance(ev, dict) else None
        out[key] = twin if twin is not None else ev
    out.update(ja)
    return out


def _read_cached(path: str, jp: bool):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return _jp_events(doc) if jp else doc.get("events")


def load_cached(kind: str, id_: int) -> dict | None:
    from . import region
    return _read_cached(os.path.join(_DATA_DIR, f"{kind}_{id_}.json"),
                        region.PROCESS_REGION == "jp")


def _report(kind: str = "chara") -> int:
    from . import event_engine, master_data

    event_engine._load()
    prefix = f"{kind}_"
    ids = sorted(int(f[len(prefix):-5]) for f in os.listdir(_DATA_DIR)
                 if f.startswith(prefix) and f.endswith(".json"))
    if not ids:
        print(f"no cached {kind} pages in {_DATA_DIR}")
        return 1

    def bands(id_):
        if kind == "chara":
            return [500000000 + (id_ // 100) * 1000]
        return [800000000 + id_ * 1000,
                800000000 + (event_engine.card_chara_id(id_) or 0) * 1000]

    total = placed = 0
    thin, unplaced_titles = [], []
    for id_ in ids:
        events = load_cached(kind, id_) or {}
        titles = [e.get("name") for e in events.values() if e.get("name")]
        known = {}
        for base in bands(id_):
            if not base % 1000000000:
                continue
            for r in master_data.query(
                    'SELECT "index" i, text FROM text_data WHERE category=181 '
                    'AND "index" BETWEEN ? AND ?', (base, base + 999)):
                if r["text"]:
                    known.setdefault(event_engine.title_key(r["text"]), r["i"])
        hit = [t for t in titles if event_engine.title_key(t) in known]
        total += len(titles)
        placed += len(hit)
        if len(hit) < 5:
            thin.append((len(hit), len(titles), id_))
        unplaced_titles += [(id_, t) for t in titles
                            if event_engine.title_key(t) not in known]

    print(f"{len(ids)} cached {kind} pages | {total} events | "
          f"{placed} resolve to a story id ({placed / max(1, total):.0%})")
    if thin:
        thin.sort()
        print(f"\n{len(thin)} pages resolve fewer than 5 events:")
        for hit, n, id_ in thin[:25]:
            print(f"   {id_}: {hit} of {n}")
    if unplaced_titles:
        print(f"\n{len(unplaced_titles)} events resolve to NOTHING, e.g.:")
        for id_, t in unplaced_titles[:15]:
            print(f"   {id_}: {t!r}")
    return 0


def _main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "index":
        idx = build_index()
        print(f"indexed {len(idx)} pages -> {_INDEX_PATH}")
        return 0
    if cmd == "dump" and len(argv) >= 3:
        events = dump(argv[1], int(argv[2]), force="--force" in argv)
        print(f"dumped {argv[1]}:{argv[2]} -> {len(events)} events")
        return 0
    if cmd == "supplement":
        print(f"merged {apply_supplement()} supplement events into the cache")
        return 0
    if cmd == "report":
        return _report(argv[1] if len(argv) > 1 else "chara")
    if cmd == "all":
        global _index_cache
        _index_cache = None
        idx = build_index()
        want = next((a for a in argv[1:] if a in ("support", "chara")), None)
        keys = [k for k in idx if not want or k.startswith(f"{want}:")]
        ok = fail = skipped = 0
        for i, key in enumerate(keys):
            kind, sid = key.split(":")
            path = os.path.join(_DATA_DIR, f"{kind}_{sid}.json")
            if os.path.exists(path) and "--force" not in argv:
                skipped += 1
                continue
            try:
                n = len(dump(kind, int(sid), force="--force" in argv))
                ok += 1
                if ok % 25 == 0:
                    print(f"  [{i + 1}/{len(keys)}] ok={ok} fail={fail} skip={skipped} (last {key}: {n} events)")
            except Exception as exc:
                fail += 1
                print(f"  FAIL {key}: {exc}")
            time.sleep(0.4)
        print(f"DONE: ok={ok} fail={fail} skipped={skipped} of {len(keys)}")
        return 0
    if cmd == "all-ja":
        _index_cache = None
        idx = build_index()
        want = next((a for a in argv[1:] if a in ("support", "chara")), None)
        keys = [k for k in idx if not want or k.startswith(f"{want}:")]
        ok = fail = 0
        for i, key in enumerate(keys):
            kind, sid = key.split(":")
            path = os.path.join(_DATA_DIR, f"{kind}_{sid}.json")
            had = False
            if os.path.exists(path) and "--force" not in argv:
                with open(path, encoding="utf-8") as fh:
                    had = bool(json.load(fh).get("events_ja"))
            if had:
                continue
            try:
                dump_ja(kind, int(sid), force="--force" in argv)
                ok += 1
                if ok % 25 == 0:
                    print(f"  [{i + 1}/{len(keys)}] ok={ok} fail={fail}")
            except Exception as exc:
                fail += 1
                print(f"  FAIL {key}: {exc}")
            time.sleep(0.4)
        print(f"DONE: ok={ok} fail={fail} of {len(keys)}")
        return 0
    if cmd == "deck" and len(argv) >= 2:
        for tok in argv[1:]:
            if not tok.isdigit():
                continue
            id_ = int(tok)
            kind = "chara" if id_ >= 100000 else "support"
            try:
                events = dump(kind, id_)
                print(f"  {kind}:{id_} -> {len(events)} events")
            except Exception as exc:
                print(f"  {kind}:{id_} FAILED: {exc}")
            time.sleep(0.5)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
