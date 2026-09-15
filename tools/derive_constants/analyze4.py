"""Validated placement + hint levels joined to the revealing card.
Usage: python analyze4.py reduced decks.json cardparams.json ev2chara.json"""
import gzip, pickle, glob, sys, json, os, math
from collections import Counter, defaultdict
from multiprocessing import Pool

CAMP = {37, 38, 39, 40, 61, 62, 63, 64}
BASE_FAC = [101, 105, 102, 103, 106]
TYPE_FAC = {"speed": 101, "stamina": 105, "power": 102, "guts": 103, "wisdom": 106}
DECKS = CARDS = EV = None


def init(dp, cp, ep):
    global DECKS, CARDS, EV
    DECKS = json.load(open(dp)); CARDS = json.load(open(cp))
    EV = {int(k): v for k, v in json.load(open(ep)).items()}


def one(path):
    c = pickle.load(gzip.open(path))
    name = os.path.basename(path)[:-7]
    deck = {d["position"]: (d["support_card_id"], d.get("limit_break_count", 0))
            for d in ((DECKS.get(name) or {}).get("deck") or [])}
    info = {p: CARDS.get(f"{s}:{lb}", {}) for p, (s, lb) in deck.items()}
    T = c["turns"]
    # pass 1: per position facility counts, to validate the deck join
    fac = defaultdict(Counter); boards = []
    for t in T:
        turn = t["t"]; b = t["board"]
        if not b or turn in CAMP:
            continue
        trains = [x for x in b if x[0] == 1 and x[1] in BASE_FAC]
        if len(trains) != 5:
            continue
        where = {p: x[1] for x in trains for p in x[5]}
        boards.append((turn, where))
        for p in deck:
            fac[p][where.get(p, "away")] += 1
    out = {"cells": Counter(), "valid": Counter(), "levels": Counter()}
    for p, cnt in fac.items():
        typ = info[p].get("type")
        if typ not in TYPE_FAC:
            continue
        facs = {f: cnt[f] for f in BASE_FAC}
        ok = sum(facs.values()) >= 20 and max(facs, key=facs.get) == TYPE_FAC[typ]
        out["valid"][ok] += 1
        if not ok:
            continue
        for turn, where in boards:
            f = where.get(p)
            kind = "away" if f is None else ("own" if f == TYPE_FAC[typ] else "other")
            band = "1-12" if turn <= 12 else ("13-72" if turn <= 72 else "73+")
            out["cells"][(band, info[p].get("sp"), kind)] += 1
    # hint levels: one reveal in the turn, no skill bought, tips on both sides
    chara_to_pos = {info[p].get("chara"): p for p in deck if info[p].get("chara")}
    for i, t in enumerate(T[:-1]):
        evs = [(pl.get("event_id")) for d, ep, pl in t["calls"] if d == "REQ" and ep == "check_event"]
        bought = any(d == "REQ" and ep == "gain_skills" for d, ep, pl in t["calls"])
        reveals = {e for e in evs if isinstance(e, int) and 20001 <= e <= 20099}
        other_hint_src = any(e in (10002, 20000) or (isinstance(e, int) and e >= 100000) for e in evs)
        if len(reveals) != 1 or bought:
            continue
        a, bn = t["tips"], T[i + 1]["tips"]
        if a is None or bn is None:
            continue
        e = next(iter(reveals))
        p = chara_to_pos.get(EV.get(e))
        if p is None:
            continue
        la = {g: lv for g, rr, lv in a}; lb = {g: lv for g, rr, lv in bn}
        inc = tuple(sorted(lb[g] - la.get(g, 0) for g in lb if lb[g] > la.get(g, 0)))
        out["levels"][(info[p].get("hl"), other_hint_src, inc)] += 1
    return out


if __name__ == "__main__":
    red, dp, cp, ep = sys.argv[1:5]
    cells = Counter(); valid = Counter(); levels = Counter()
    with Pool(max(2, os.cpu_count() - 2), initializer=init, initargs=(dp, cp, ep)) as p:
        for r in p.imap_unordered(one, sorted(glob.glob(red + "/*.pkl.gz")), chunksize=8):
            cells.update(r["cells"]); valid.update(r["valid"]); levels.update(r["levels"])
    print("deck-position joins validated (argmax facility == card type):", dict(valid))
    for band in ("1-12", "13-72", "73+"):
        a = sum(v for (b, sp, k), v in cells.items() if b == band and k == "away")
        o = sum(v for (b, sp, k), v in cells.items() if b == band and k == "other") / 4
        own = sum(v for (b, sp, k), v in cells.items() if b == band and k == "own")
        n = a + o * 4 + own
        if o:
            print(f"turns {band}: implied AWAY weight {100*a/o:.1f} (away share {100*a/n:.2f}%, n={n})")
    print("turns 13-72 by specialty_priority: sp -> implied away w, implied own extra w, n")
    for sp in sorted({k[1] for k in cells if k[1] is not None}):
        a = cells[("13-72", sp, "away")]; own = cells[("13-72", sp, "own")]; o = cells[("13-72", sp, "other")] / 4
        if o > 100:
            print(f"   sp={sp}: away {100*a/o:.1f}, own extra {100*own/o-100:.1f}, n={a+own+o*4:.0f}")
    print("hint level increments joined to revealing card: (card hint-level bonus, other hint source same turn) -> increments")
    grp = defaultdict(Counter)
    for (hl, other, inc), v in levels.items():
        grp[(hl, other)][inc] += v
    for k in sorted(grp, key=str):
        tot = sum(grp[k].values())
        print(f"   bonus={k[0]} other_src={k[1]} n={tot}: {grp[k].most_common(8)}")
