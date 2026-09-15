"""Deck-joined derivations. Usage: python analyze2.py reduced decks.json cardparams.json"""
import gzip, pickle, glob, math, sys, json, os
from collections import Counter, defaultdict
from multiprocessing import Pool

CAMP = {37, 38, 39, 40, 61, 62, 63, 64}
BASE_FAC = [101, 105, 102, 103, 106]
TYPE_FAC = {"speed": 101, "stamina": 105, "power": 102, "guts": 103, "wisdom": 106}
DIG = ("vital", "motivation", "speed", "stamina", "power", "guts", "wiz", "fans")
DECKS = CARDS = None


def init(dp, cp):
    global DECKS, CARDS
    DECKS = json.load(open(dp)); CARDS = json.load(open(cp))


def wilson(k, n, z=1.96):
    if not n:
        return "n=0"
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return f"{k}/{n} = {100*p:.2f}% [{100*(c-m)/d:.2f},{100*(c+m)/d:.2f}]"


def delta(a, b):
    if not a or not b:
        return None
    try:
        return tuple((b.get(k) or 0) - (a.get(k) or 0) for k in DIG)
    except TypeError:
        return None


def one(path):
    c = pickle.load(gzip.open(path))
    name = os.path.basename(path)[:-7]
    deck = {d["position"]: (d["support_card_id"], d.get("limit_break_count", 0))
            for d in ((DECKS.get(name) or {}).get("deck") or [])}
    r = {"sc": c["sc"], "place": defaultdict(Counter), "hint": Counter(),
         "ev_by_turn": [], "effects": defaultdict(Counter), "tipdelta": Counter(),
         "caps": [], "final": c["final_turn"], "outcome": c["outcome"]}
    T = c["turns"]
    last_chara = None
    for i, t in enumerate(T):
        turn = t["t"]
        b = t["board"]
        if b and turn not in CAMP and deck:
            trains = [x for x in b if x[0] == 1 and x[1] in BASE_FAC]
            if len(trains) == 5:
                present = {}
                for ct, cid, en, fr, lv, partners, hints, gains in trains:
                    for p in partners:
                        if p in deck:
                            present[p] = cid
                            r["hint"][(deck[p], p in hints)] += 1
                for p, card in deck.items():
                    r["place"][card][present.get(p, "away")] += 1
        exec_ct = None
        evs = []
        gained_skill = False
        pending = None      # (event_id, choice, chara_before)
        for d, ep, payload in t["calls"]:
            if d == "REQ":
                if ep == "exec_command":
                    exec_ct = payload.get("command_type")
                elif ep == "gain_skills":
                    gained_skill = True
                elif ep == "check_event":
                    e = payload.get("event_id")
                    evs.append((e, payload.get("choice_number")))
                    pending = (e, payload.get("choice_number"), last_chara)
            else:
                ch = payload[0]
                if ep == "check_event" and pending and ch:
                    dl = delta(pending[2], ch)
                    if dl is not None:
                        r["effects"][(pending[0], pending[1])][dl] += 1
                    pending = None
                if ch:
                    last_chara = ch
        r["ev_by_turn"].append((turn, exec_ct, tuple(evs)))
        # hint level: exactly one hint-for-growth reveal, no skill bought, tips both sides
        reveals = [e for e, ch in evs if isinstance(e, int) and 20001 <= e <= 20099 and ch]
        if len(set(reveals)) == 1 and not gained_skill and i + 1 < len(T):
            a, bnext = t["tips"], T[i + 1]["tips"]
            if a is not None and bnext is not None:
                la = {g: lv for g, rr, lv in a}
                lb = {g: lv for g, rr, lv in bnext}
                inc = [lb[g] - la.get(g, 0) for g in lb if lb[g] > la.get(g, 0)]
                r["tipdelta"][tuple(sorted(inc))] += 1
        if c["sc"] == 3 and t["caps"]:
            r["caps"].append((turn, t["caps"], tuple(e for e, _ in evs)))
    return r


if __name__ == "__main__":
    red, dp, cp = sys.argv[1:4]
    CARDS_MAIN = json.load(open(cp))
    files = sorted(glob.glob(red + "/*.pkl.gz"))
    place = defaultdict(Counter); hint = Counter(); effects = defaultdict(Counter)
    tipdelta = Counter(); per_career = []; caps_runs = []
    if os.path.exists("agg2.pkl"):
        place, hint, effects, tipdelta, per_career, caps_runs = pickle.load(open("agg2.pkl", "rb"))
        files = []
    with Pool(max(2, os.cpu_count() - 2), initializer=init, initargs=(dp, cp)) as p:
        for r in p.imap_unordered(one, files, chunksize=8):
            for k, v in r["place"].items():
                place[tuple(k)].update(v)
            hint.update({(tuple(k[0]), k[1]): v for k, v in r["hint"].items()})
            for k, v in r["effects"].items():
                effects[k].update(v)
            tipdelta.update(r["tipdelta"])
            per_career.append((r["sc"], r["final"], r["outcome"], r["ev_by_turn"]))
            if r["caps"]:
                caps_runs.append(r["caps"])
    if files:
        pickle.dump((place, hint, effects, tipdelta, per_career, caps_runs), open("agg2.pkl", "wb"))

    def cp_of(card):
        return CARDS_MAIN.get(f"{card[0]}:{card[1]}", {})

    print("== PLACEMENT by card type (U-014)")
    bytype = defaultdict(Counter)
    rows = []
    for card, cnt in place.items():
        info = cp_of(card); typ = info.get("type")
        n = sum(cnt.values())
        if n < 200 or not typ:
            continue
        if typ in TYPE_FAC:
            own = cnt[TYPE_FAC[typ]]
            others = sum(cnt[f] for f in BASE_FAC if f != TYPE_FAC[typ]) / 4
            if others <= 0:
                print("   (never in an off-type facility)", typ, card, dict(cnt))
                continue
            rows.append((typ, card, info.get("sp"), n, round(100 * cnt["away"] / others, 1),
                         round(100 * own / others - 100, 1)))
            bytype["deck"]["away"] += cnt["away"]; bytype["deck"]["own"] += own
            bytype["deck"]["other4"] += others * 4; bytype["deck"]["n"] += n
        else:
            bytype[typ]["away"] += cnt["away"]; bytype[typ]["n"] += n
            for f in BASE_FAC:
                bytype[typ][f] += cnt[f]
    for typ, v in bytype.items():
        if typ == "deck":
            om = v["other4"] / 4
            print(f"stat cards pooled: implied AWAY weight = {100*v['away']/om:.1f}, implied own-facility extra = {100*v['own']/om-100:.1f}, away share {wilson(v['away'], v['n'])}")
        else:
            print(f"{typ} cards: away share {wilson(v['away'], v['n'])}; facility counts {[v[f] for f in BASE_FAC]}")
    print("per card (type, card:lb, specialty_priority@level, n, implied AWAY weight, implied own extra weight):")
    for row in sorted(rows, key=lambda x: (x[2] or 0)):
        print("  ", row)
    sp_groups = defaultdict(lambda: [0, 0, 0.0])
    for card, cnt in place.items():
        info = cp_of(card); typ = info.get("type")
        if typ in TYPE_FAC:
            g = sp_groups[info.get("sp")]
            g[0] += cnt["away"]; g[1] += cnt[TYPE_FAC[typ]]
            g[2] += sum(cnt[f] for f in BASE_FAC if f != TYPE_FAC[typ]) / 4
    print("by specialty_priority value: sp -> (implied away w, implied own extra w)")
    for sp, (aw, own, om) in sorted(sp_groups.items(), key=lambda x: x[0] or 0):
        if om > 50:
            print(f"   sp={sp}: away {100*aw/om:.1f}, own extra {100*own/om-100:.1f}  (other-mean n={om:.0f})")

    print("== HINT RATE per card (U-004): rate vs hint_frequency")
    hf_groups = defaultdict(lambda: [0, 0])
    num = den = 0.0
    for (card, h), v in hint.items():
        info = cp_of(card)
        hf = info.get("hf", 0) or 0
        if info.get("type") not in TYPE_FAC:
            hf_groups[("nonstat", info.get("type"))][h] += v
            continue
        hf_groups[hf][h] += v
        den += v * (1 + hf / 100)
        if h:
            num += v
    for hf, (no, yes) in sorted(hf_groups.items(), key=lambda x: str(x[0])):
        tot = no + yes
        extra = "" if isinstance(hf, tuple) else f"  -> implied base {100*yes/tot/(1+hf/100):.2f}%"
        print(f"   hint_frequency {hf}: {wilson(yes, tot)}{extra}")
    print(f"   pooled fit base = {100*num/den:.3f}%")

    print("== SUPPORT EVENT CUTOFF (U-003)")
    tab = defaultdict(Counter)
    for sc, fin, out, evt in per_career:
        for turn, ct, evs in evt:
            if turn is None or not 66 <= turn <= 76:
                continue
            ids = [e for e, _ in evs]
            tab[(sc, turn)]["turns"] += 1
            tab[(sc, turn)]["train"] += ct == 1
            tab[(sc, turn)]["cmd"] += ct is not None
            tab[(sc, turn)]["sup"] += any(e in (10002, 20000) for e in ids)
    for sc in (1, 2, 3, 4):
        print(f"  scenario {sc}: " + "  ".join(
            f"t{tt}:{tab[(sc, tt)]['sup']}/{tab[(sc, tt)]['cmd']}cmd/{tab[(sc, tt)]['train']}tr" for tt in range(66, 77)))

    print("== SUPPORT EVENT RATE by command type (U-002)")
    rate = defaultdict(Counter)
    for sc, fin, out, evt in per_career:
        for turn, ct, evs in evt:
            if turn is None or turn > 71 or turn in CAMP or turn < 2:
                continue
            ids = [e for e, _ in evs]
            rate[ct]["n"] += 1
            rate[ct]["chain"] += any(e == 10002 for e in ids)
            rate[ct]["random"] += any(e == 20000 for e in ids)
    for ct, v in rate.items():
        print(f"  command_type {ct}: chain {wilson(v['chain'], v['n'])}  random {wilson(v['random'], v['n'])}")
    tot = Counter()
    for sc, fin, out, evt in per_career:
        if out == "completed" and fin == 78:
            ids = [e for _, _, evs in evt for e, _ in evs]
            tot[(sc, "careers")] += 1
            tot[(sc, "chain")] += sum(1 for e in ids if e == 10002)
            tot[(sc, "random")] += sum(1 for e in ids if e == 20000)
    for sc in (1, 2, 3, 4):
        n = tot[(sc, "careers")]
        if n:
            print(f"  completed scenario {sc}: n={n}, chain acks/career {tot[(sc,'chain')]/n:.2f}, random acks/career {tot[(sc,'random')]/n:.2f}")

    print("== EXTRA TRAINING / ACUPUNCTURE per career")
    for eid in (7017, 7020):
        dist = Counter(); acks_per_turn = Counter(); cts = Counter(); turns_hist = Counter()
        for sc, fin, out, evt in per_career:
            if fin != 78:
                continue
            k = 0
            for turn, ct, evs in evt:
                a = [e for e, _ in evs if e == eid]
                if a:
                    k += 1; acks_per_turn[len(a)] += 1; cts[ct] += 1; turns_hist[(turn - 1) // 12] += 1
            dist[k] += 1
        print(f"  {eid}: distinct turns per full career {sorted(dist.items())}; acks per firing turn {dict(acks_per_turn)}; command type of turn {dict(cts)}; by 12-turn block {sorted(turns_hist.items())}")
    k = n = 0
    for sc, fin, out, evt in per_career:
        if fin != 78:
            continue
        n += sum(1 for turn, ct, evs in evt if turn and turn <= 72)
        k += sum(1 for turn, ct, evs in evt if any(e == 7020 for e, _ in evs))
    print("  acupuncture per turn (turns 1-72, full careers):", wilson(k, n))

    print("== HINT LEVELS from tips (U-004 _HINT_LEVEL_WEIGHTS)")
    print("  level increments seen after exactly one reveal:", tipdelta.most_common(12))

    print("== GRAND LIVE caps (U-047)")
    capchg = Counter()
    for run in caps_runs:
        for (t0, c0, e0), (t1, c1, e1) in zip(run, run[1:]):
            d = tuple(c1[k] - c0[k] for k in sorted(c1) if k in c0)
            if any(d):
                capchg[(t1, d)] += 1
    print("  (turn, cap delta) counts:", sorted(capchg.items())[:60])

    out = {f"{e}|{ch}": {"n": sum(v.values()), "top": [[list(k), c] for k, c in v.most_common(8)]}
           for (e, ch), v in effects.items() if sum(v.values()) >= 5}
    json.dump(out, open("event_effects.json", "w"))
    print("event effect table keys:", len(out), "-> event_effects.json (delta order", DIG, ")")
