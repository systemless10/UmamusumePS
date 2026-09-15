"""
GameTora event-data dumper.

GameTora (https://gametora.com/umamusume) publishes the COMPLETE event database
for every trainee and support card -- every training / story / outing / secret
event, each choice, and each outcome (including random branches with their odds).
It is a strictly richer source than the datamined JSON: it also carries the
"system" events the datamined dump omits (Sleep Deprived, race results, claw
machine, ...).

This module dumps that data on demand and caches it to disk, so the server stays
offline-capable at request time (the engine only ever reads the cache; dumping is
an explicit, separate step). Each character/support page embeds its events in
__NEXT_DATA__.props.pageProps.eventData.en (a JSON string). We normalize it to
the same schema the datamined loader uses, so event_engine consumes both
uniformly.

Slugs: a page URL is /umamusume/{supports|characters}/{id}-{slug} and the id
alone 404s, so we resolve id -> URL from GameTora's sitemap (cached).

Usage (from the server dir):
    python -m app.gametora index                 # (re)build the id->URL index
    python -m app.gametora dump support 30028    # dump one card
    python -m app.gametora dump chara 100101
    python -m app.gametora deck 100101 30028 30066 20016 ...   # a whole run
    python -m app.gametora all [support|chara] [--force]   # bulk dump
    python -m app.gametora report chara          # coverage: what resolves?

`all` takes an optional KIND so the trainee pages can be refreshed on their own
-- they are the source for character events (every trainee story, the year-gated
camp/New Year beats, and the secret events), and unlike the support pages they
have no datamined fallback worth the name: character_event_data.json covers 80
trainee cards where GameTora covers 260.

`report` answers the question that actually matters after a dump -- not "did it
download" but "does each event resolve to a real master.mdb story id", since an
event we can't place is an event the player can never see. It prints per-card
counts and flags anything thin or unresolvable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events", "gametora")
_INDEX_PATH = os.path.join(_DATA_DIR, "_url_index.json")
_SITEMAP = "https://gametora.com/sitemap-0.xml"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PrivateServer-EventDumper"

# GameTora short effect code -> the 'type' string event_engine applies. Decoded
# from GameTora's own effect renderer (the legend object in its JS bundle), so the
# meanings are exact. Notable ones that were previously wrong/dropped: se = obtain
# a STATUS EFFECT (condition, e.g. Practice Perfect), sg = obtain a skill, sr =
# a set of OR skill hints, me = max energy, rs = random stats, he/ha = heal a
# condition.
_CODE_TYPE = {
    "sp": "speed", "st": "stamina", "po": "power", "gu": "guts", "in": "wisdom",
    "en": "energy", "me": "max_energy", "mo": "mood", "pt": "skill_points",
    "sk": "skill_hint", "srh": "skill_hint", "sg": "obtain_skill",
    "5s": "all_stats", "rs": "random_stats", "hi_s": "highest_stat",
    "lo_s": "lowest_stat", "ls": "last_stat", "fe": "full_energy",
    "bo": "bond", "bo_ch": "bond", "bo_l": "bond", "bo_r": "bond",
    "se": "condition", "he": "heal_status", "ha": "heal_status",
    "fa": "fans", "rr": "race_rewards",
    # rc = the trainee's OBJECTIVE RACE CHANGES to this race_instance id. This
    # is the payload of every branching career storyline (Agnes Tachyon's
    # "Report: A Clear Gaze" -> NHK Mile Cup); it was in _SKIP_CODES until
    # 2026-09-04, which is why the branch triggers looked absent from our data
    # while GameTora showed them plainly on the page.
    "rc": "race_change",
    # ra = an objective race is CANCELLED. `d` is the race_instance id.
    # Matikanetannhauser's "There's Always Next Time" reads, in the game's own
    # words: "Guts +5 / Cannot race for 1 turns / Objective race Japan Cup
    # cancelled" -- and `ra` d=101901 is the Japan Cup. It matches her
    # single_mode_route_race row 627, the one row in the table with
    # determine_race=4, i.e. a goal that can be TAKEN AWAY.
    #
    # I first read this backwards, as "race added", because gen=99 looked like
    # "not in the generated route". It is not: the goal is hers from turn 1 and
    # the event removes it. See cancel_route_race.
    "ra": "race_cancel",
    # rl = "Cannot race for `d` turns" -- a race-entry LOCKOUT, not a goal edit.
    # 10 occurrences, always d=1. master.mdb corroborates it exactly:
    # single_mode_race_restrict_turn has 3 rows, and the first is
    # (chara_id 1062, turn 70, gain_id 106211811) -- Matikanetannhauser, the
    # Japan Cup turn, with the gain_id encoding chara 1062 + story suffix 118,
    # which IS "There's Always Next Time". The other two rows follow the same
    # shape (chara 1044 turn 36 suffix 120; chara 1078 turn 44 suffix 118).
    "rl": "race_restrict",
    # ee = "End this Support Card's chain event" -- the outcome DEAD-ENDS the
    # card's chain, so none of its remaining chain events can ever fire in this
    # career. The client has a display line for exactly this (text_data 394 id
    # 12), which is why that id sat unused in event_engine's table.
    #
    # It was in _SKIP_CODES as a formatting marker until 2026-09-05, so the
    # penalty simply did not exist here: an outcome that should have shut the
    # chain down paid its stats and let the chain roll on. Matikanefukukitaru
    # 30078's "Mystery Fortune Ritual!" is the reported case -- rolling the
    # Wit +4 half of its first choice, or picking its second choice at all,
    # both carry `ee`:
    #
    #   {"t":"5s","v":"+7"}, {"t":"bo","v":"+7","d":1056},
    #   {"t":"di"}, {"t":"in","v":"+4"}, {"t":"ee"}
    #
    # Note it hangs off the BRANCH, not the choice: the good half of that same
    # choice has no `ee`. Sweep Tosho's chain is the version where the player
    # picks the dead end outright rather than rolling into it.
    "ee": "chain_end",
    # brian_tryhard = the one named branch flag GameTora models as an effect
    # rather than a race swap: Narita Brian's "Public Appearance" choice, which
    # switches his story arms without changing any race (master.mdb lists the
    # SAME race on both arms of all four of his branch groups).
    "brian_tryhard": "branch_flag",
}
# Markers / formatting / flow codes that carry no reward (dropped).
# `nl` is NOT here any more -- it is the conditional-SEGMENT separator, see
# _split_segments. `di` stays: it separates random branches within a segment.
_SKIP_CODES = {"di", "di_s", "no", "ds", "fd", "et", "sl", "nsl",
               "brp", "brf", "rh"}
# `rh` (2 occurrences, d=100801 Victoria Mile, chara 1091) is still undecoded
# and still skipped.

# Guard codes. Each opens a CONDITIONAL SEGMENT of a choice's reward list:
# "when this holds, these are the rewards". Segments are separated by `nl` and
# a guard sits at the head of its segment. GameTora renders them as the "※ ..."
# lines above each reward block.
#
# Until 2026-09-04 every one of these was either skipped or passed through as a
# pseudo-effect and `nl` was dropped, so a choice's segments were CONCATENATED:
# Agnes Tachyon's mood-gated event applied both mood arms at once, and Nishino
# Flower's "A Shining Place" paid out all four seasons together.
_GUARD_CODES = {
    "mood_min": "mood_min",           # d = mood level, 1..5 (4 = Good or better)
    "mood_max": "mood_max",           # d = mood level (3 = Normal or lower)
    "result_bad": "result_bad",
    "co": "season",                   # d = spring/summer/fall/winter
    "pl": "finish_position",          # d = [min, max], max None = open-ended
    "w_e": "win_streak",              # d = level or [lo, hi]
    "ps_h": "has_skill",              # d = skill id
    "ps_nh": "lacks_skill",
    "se_h": "has_condition",          # d = condition id
    "se_nh": "lacks_condition",
    "bp2": "bond_level",              # v = tier
    "sc": "scripted",                 # d = [condition_name, args...]
    "ct": "count",                    # d = a "※ 2-3" style label
    "s_nore": "no_rest",
    "highest_facility": "highest_facility",
    "fans_minimum": "fans_min",       # d = fan count
    "fans_maximum": "fans_max",
    "result_good": "result_good",
    "result_average": "result_average",
    "success": "success",             # the preceding attempt succeeded / failed
    "fail": "fail",
    "most_trained": "most_trained",   # branches follow, one per facility
    "expensive_races": "expensive_races",
    "other_cases": "otherwise",       # the else arm; always last
}


def _fetch(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"fetch failed {url}: {last}")


# ---- id -> URL index (from the sitemap) -------------------------------------

def build_index() -> dict:
    """Scrape the sitemap into {'support:<id>': url, 'chara:<id>': url} for the
    English (no locale prefix) pages, and cache it."""
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


# ---- page -> normalized events ----------------------------------------------

def _extract_event_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ on page")
    nd = json.loads(m.group(1))
    ev = nd["props"]["pageProps"]["eventData"]
    en = ev.get("en") if isinstance(ev, dict) else None
    if not en:
        raise RuntimeError("no English eventData")
    return json.loads(en) if isinstance(en, str) else en


def _split_branches(effects: list) -> tuple[list, list]:
    """Split a choice's flat reward list into random branches at 'di' markers.
    A 'di' divider introduces the branch that FOLLOWS it; its 'd' ("~90") is that
    branch's odds. The leading segment (before any 'di') is a branch too unless it
    is empty (a list that starts with 'di'). Returns (branches, probs) with None
    odds where GameTora gives none (equal-weighted at apply time). No 'di' -> a
    single deterministic branch."""
    segments = []            # [(prob, [effects])]
    cur_prob, cur = None, []
    for r in effects or ():
        if r.get("t") == "di":
            segments.append((cur_prob, cur))
            cur_prob, cur = _parse_prob(r.get("d")), []
        else:
            cur.append(r)
    segments.append((cur_prob, cur))
    if len(segments) > 1 and not segments[0][1] and segments[0][0] is None:
        segments = segments[1:]     # drop empty leading segment (list began with di)
    return [s[1] for s in segments], [s[0] for s in segments]


def _parse_prob(d):
    if isinstance(d, str) and d.startswith("~"):
        try:
            return int(d[1:]) / 100.0
        except ValueError:
            return None
    return None


def _norm_effect(r: dict):
    """A GameTora reward entry -> one normalized effect, a LIST (for `sr`, a set
    of OR skill hints), or None (markers/no-op)."""
    t = r.get("t")
    if t in _SKIP_CODES:
        return None
    if t == "sr":
        # An OR SET of skill hints: the event grants exactly ONE of these, not
        # all of them. `d` is the alternatives, each {v: level, d: skill_id},
        # and a level may itself carry a "+1/+3" range.
        #
        # This used to return the alternatives as a LIST, which _norm_effects
        # flattened into sibling effects -- and siblings are applied together,
        # so every `sr` event handed out its whole set at once. That is what an
        # SSR chain finale looks like from the inside: Fine Motion's "Lovely
        # Racing Weather ♪" is one `sr` of {Speed Star 200581 "+1/+3", Prepared
        # to Pass 200582 "+1"} -- gold OR its white group-mate -- and we paid
        # both (live-reported 2026-09-05: "if i got gold skill i dont get hint
        # of white skill"). Nishino Flower 30018 and Hishi Akebono 30040 are
        # the same shape. Kept as ONE effect so the OR survives into the engine
        # (event_engine.resolve_or_effects rolls it at commit).
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
    """A choice's raw reward list -> [(guards, rewards), ...], split at `nl`.

    Leading _GUARD_CODES entries of a segment are its guards ("※ Mood of Normal
    or lower"); the rest are its rewards. A segment with no leading guard is
    unconditional -- that is the shape of every ordinary event, which is why
    this returns a single unguarded segment for them and nothing downstream
    changes."""
    out: list = [([], [])]
    for r in rewards or ():
        t = r.get("t")
        if t == "nl":
            out.append(([], []))
        elif t in _GUARD_CODES:
            # A guard opens a segment wherever it appears -- most pages put an
            # `nl` before it, but not all ("Victory!", the Satsuki Sho reports),
            # and without this those guards leaked into the reward list as
            # pseudo-effects.
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
    """Normalize a reward list. (_norm_effect may still return a list; `sr` no
    longer does -- it stays one OR effect, see there.)"""
    out = []
    for r in rewards or ():
        e = _norm_effect(r)
        if e is None:
            continue
        out.extend(e if isinstance(e, list) else [e])
    return out


def _branch_payload(rewards: list) -> dict:
    """The {effects} or {outcomes, probs, random_either} half of a choice (or of
    one conditional segment) -- the `di` random split, normalized."""
    branches, probs = _split_branches([r for r in rewards or () if r.get("t") != "nl"])
    norm = [_norm_effects(br) for br in branches]
    if len(norm) <= 1:
        return {"effects": norm[0] if norm else [], "random_either": False}
    return {"outcomes": norm, "random_either": True, "probs": [p for p in probs]}


def _norm_choice(c: dict) -> dict:
    """One choice. A choice whose rewards are CONDITIONAL carries `segments`
    (each with its `when` guards) in addition to the flat `effects`/`outcomes`,
    which stay populated from the default segment so every existing consumer --
    the reward preview, the datamined-source merge -- keeps working unchanged
    on a choice it cannot evaluate."""
    raw = c.get("r") or []
    segments = _split_segments(raw)
    # Note the absence of a "more than one segment" test: a choice can be a
    # SINGLE guarded segment ("Tragedy on the Green" is just `expensive_races`),
    # and requiring two sent those down the flat path with the guard still in
    # the reward list, where it surfaced as a pseudo-effect.
    if any(guards for guards, _ in segments):
        norm = [dict(_branch_payload(rewards), when=guards)
                for guards, rewards in segments]
        # Preview default: the unconditional arm if there is one (`other_cases`
        # is a guard, so it does not count), else the first. Never a merge of
        # all arms -- that was the concatenation bug.
        base = next((s for s in norm if not s["when"]), norm[0])
        out = {"label": c.get("o", ""), "segments": norm}
        out.update({k: v for k, v in base.items() if k != "when"})
        return out
    return dict(_branch_payload(raw), label=c.get("o", ""))


def _norm_event(e: dict, section: str | None = None) -> dict:
    """One event. `section` and `conditions` are kept because they are the only
    thing distinguishing a SECRET event (which fires solely when its
    precondition holds, e.g. [["autumn_triple_crown_senior"]]) from an ordinary
    one -- dropping them made every secret event either unservable or, worse,
    servable unconditionally. `event_type` (GameTora's own "type" tag, e.g. "ny"
    for a pal/group card's New Year date) was previously dropped entirely --
    live-reported 2026-09-03: every pal/group support card carries exactly one
    "ny"-tagged 'special' event (its New Year outing bonus), silently
    indistinguishable from an ordinary special/finale beat without this."""
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


# Sections that are lists of events. `nyear`/`dance` are NOT -- they are bare
# per-trainee stat codes for the shared "Special Events", see SPECIAL_STAT_KEYS.
SPECIAL_STAT_KEYS = ("nyear", "dance")
_SPECIAL_KEY = "__special__"     # where those codes live in the cached file


def normalize(event_data: dict) -> dict:
    """{normalized_title: event} for every event on the page (across all
    sections: wchoice/nochoice/version/outings/secret/... -- any list of event
    dicts), plus a reserved `__special__` entry.

    That reserved entry carries the page's per-trainee stat codes for the
    SHARED "Special Events" (New Year's Resolutions, Dance Lesson, ...). Those
    events are not on the page as events at all -- no shared-event id appears
    on any character page -- only as bare codes like `nyear: "st"` and
    `dance: ["st", "in"]`, which is why a name-based scrape found nothing and
    45% of trainees were served a blind 'speed' guess for New Year."""
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
    # \w keeps unicode word chars -- a JP-titled page (Light Hello's support
    # card) collapsed all 12 events onto the key "" and the cache kept only
    # the last one. Existing collapsed caches are healed at read time by
    # event_engine (fallback-index merge), not rewritten here.
    return re.sub(r"[^\w]", "", (s or "").lower(), flags=re.UNICODE)


# ---- dump / cache -----------------------------------------------------------

def dump(kind: str, id_: int, force: bool = False) -> dict:
    """Fetch + normalize + cache one card/trainee's events. Returns the
    {title: event} map. Cached at gametora/{kind}_{id}.json."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, f"{kind}_{id_}.json")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    url = _url_for(kind, id_)
    if not url:
        raise RuntimeError(f"no GameTora URL for {kind}:{id_} (rebuild index?)")
    events = normalize(_extract_event_data(_fetch(url)))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"kind": kind, "id": id_, "url": url, "events": events}, fh, ensure_ascii=False)
    return events


def load_cached(kind: str, id_: int) -> dict | None:
    path = os.path.join(_DATA_DIR, f"{kind}_{id_}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("events")


# ---- CLI --------------------------------------------------------------------

def _report(kind: str = "chara") -> int:
    """Coverage of what's on disk: for every cached page, how many of its events
    resolve to a real master.mdb story id.

    Downloading a page is not the win -- placing its events is. An event whose
    title matches nothing in text_data can never be served, so it may as well
    not have been dumped. Import locally: this module must stay importable with
    no master.mdb around (it's the offline dumper)."""
    from . import event_engine, master_data

    event_engine._load()
    prefix = f"{kind}_"
    ids = sorted(int(f[len(prefix):-5]) for f in os.listdir(_DATA_DIR)
                 if f.startswith(prefix) and f.endswith(".json"))
    if not ids:
        print(f"no cached {kind} pages in {_DATA_DIR}")
        return 1

    def bands(id_):
        """The master story-id windows this page's events may live in."""
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
    if cmd == "report":
        return _report(argv[1] if len(argv) > 1 else "chara")
    if cmd == "all":
        global _index_cache
        _index_cache = None
        idx = build_index()
        # optional KIND filter: `all chara` refreshes only the trainee pages.
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
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"  FAIL {key}: {exc}")
            time.sleep(0.4)
        print(f"DONE: ok={ok} fail={fail} skipped={skipped} of {len(keys)}")
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
            except Exception as exc:  # noqa: BLE001
                print(f"  {kind}:{id_} FAILED: {exc}")
            time.sleep(0.5)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
