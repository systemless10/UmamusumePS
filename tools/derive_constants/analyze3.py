"""Placement diagnostics + hint levels. Usage: python analyze3.py reduced decks.json cardparams.json"""
import gzip, pickle, glob, sys, json, os
from collections import Counter, defaultdict
from multiprocessing import Pool

CAMP = {37, 38, 39, 40, 61, 62, 63, 64}
BASE_FAC = [101, 105, 102, 103, 106]
TYPE_FAC = {"speed": 101, "stamina": 105, "power": 102, "guts": 103, "wisdom": 106}
DECKS = CARDS = None


def init(dp, cp):
    global DECKS, CARDS
    DECKS = json.load(open(dp)); CARDS = json.load(open(cp))


def one(path):
    c = pickle.load(gzip.open(path))
    name = os.path.basename(path)[:-7]
    deck = {d["position"]: (d["support_card_id"], d.get("limit_break_count", 0))
            for d in ((DECKS.get(name) or {}).get("deck") or [])}
    out = {"cells": Counter(), "occupancy": Counter(), "tipdelta": Counter(),
           "never": Counter(), "pos_seen": Counter()}
    T = c["turns"]
    seen_pos = Counter()
    for i, t in enumerate(T):
        turn = t["t"]; b = t["board"]
        if b and turn not in CAMP and deck:
            trains = [x for x in b if x[0] == 1 and x[1] in BASE_FAC]
            if len(trains) == 5:
                where = {}
                for x in trains:
                    out["occupancy"][len(x[5])] += 1
                    for p in x[5]:
                        where[p] = x[1]
                        seen_pos[p] += 1
                blk = (turn - 1) // 12
                for p, card in deck.items():
                    info = CARDS.get(f"{card[0]}:{card[1]}", {})
                    typ = info.get("type")
                    if typ not in TYPE_FAC:
                        continue
                    f = where.get(p)
                    kind = "away" if f is None else ("own" if f == TYPE_FAC[typ] else "other")
                    out["cells"][(c["sc"], p, blk, info.get("rarity"), kind)] += 1
        evs = [(pl.get("event_id"), pl.get("choice_number")) for d, ep, pl in t["calls"]
               if d == "REQ" and ep == "check_event"]
        bought = any(d == "REQ" and ep == "gain_skills" for d, ep, pl in t["calls"])
        reveals = {e for e, ch in evs if isinstance(e, int) and 20001 <= e <= 20099}
        if len(reveals) == 1 and not bought and i + 1 < len(T):
            a, bn = t["tips"], T[i + 1]["tips"]
            if a is not None and bn is not None:
                la = {g: lv for g, rr, lv in a}; lb = {g: lv for g, rr, lv in bn}
                inc = tuple(sorted(lb[g] - la.get(g, 0) for g in lb if lb[g] > la.get(g, 0)))
                out["tipdelta"][inc] += 1
    for p in deck:
        if seen_pos[p] == 0 and any(t["board"] for t in T):
            out["never"][(p, deck[p])] += 1
    out["pos_seen"] = Counter({p: n for p, n in seen_pos.items()})
    return out


if __name__ == "__main__":
    red, dp, cp = sys.argv[1:4]
    cells = Counter(); occ = Counter(); tip = Counter(); never = Counter(); posvals = Counter()
    with Pool(max(2, os.cpu_count() - 2), initializer=init, initargs=(dp, cp)) as p:
        for r in p.imap_unordered(one, sorted(glob.glob(red + "/*.pkl.gz")), chunksize=8):
            cells.update(r["cells"]); occ.update(r["occupancy"]); tip.update(r["tipdelta"])
            never.update(r["never"]); posvals.update(r["pos_seen"])

    def implied(sel):
        a = sum(v for k, v in cells.items() if sel(k) and k[-1] == "away")
        o = sum(v for k, v in cells.items() if sel(k) and k[-1] == "other") / 4
        return (round(100 * a / o, 1) if o else None, a, round(o))

    print("partners per facility (count of facilities):", sorted(occ.items()))
    print("partner ids seen on boards:", posvals.most_common(12))
    print("deck positions NEVER seen on any board in their career (pos, card):", never.most_common(15))
    for sc in (1, 2, 3, 4):
        print(f"scenario {sc}: implied away weight (away, other-mean) = {implied(lambda k: k[0] == sc)}")
    for pos in range(1, 7):
        print(f"position {pos}: {implied(lambda k: k[1] == pos)}")
    for blk in range(7):
        print(f"turn block {blk*12+1}-{blk*12+12}: {implied(lambda k: k[2] == blk)}")
    for rar in (1, 2, 3):
        print(f"rarity {rar}: {implied(lambda k: k[3] == rar)}")
    print("hint level increments after exactly one reveal:", tip.most_common(15))
