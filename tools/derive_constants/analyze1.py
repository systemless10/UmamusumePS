"""Placement, hints, support events, Extra Training / Acupuncture from reduced corpus."""
import gzip, pickle, glob, math, sys
from collections import Counter, defaultdict
from multiprocessing import Pool

CAMP = {37, 38, 39, 40, 61, 62, 63, 64}
BASE_FAC = [101, 105, 102, 103, 106]
STAT_OF = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz",
           601: "speed", 602: "stamina", 603: "power", 604: "guts", 605: "wiz"}


def wilson(k, n, z=1.96):
    if not n:
        return "n=0"
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return f"{k}/{n} = {100*p:.2f}% [{100*(c-m)/d:.2f},{100*(c+m)/d:.2f}]"


def one(path):
    c = pickle.load(gzip.open(path))
    r = {"sc": c["sc"], "partner_vals": Counter(), "fac_counts": defaultdict(Counter),
         "hint": Counter(), "hint_bond": Counter(), "hint_cp": defaultdict(Counter),
         "ev_turn": Counter(), "ev_total": Counter(), "turns": Counter(),
         "xt": [], "cmd_ids": Counter(), "n_multi_hint_fac": 0}
    prev_chara = None
    for t in c["turns"]:
        turn = t["t"]
        r["turns"][turn] += 1
        b = t["board"]
        if b and turn not in CAMP:
            trains = [x for x in b if x[0] == 1 and x[1] in BASE_FAC]
            if len(trains) == 5:
                present = set()
                for ct, cid, en, fr, lv, partners, hints, gains in trains:
                    for p in partners:
                        r["partner_vals"][p] += 1
                        present.add(p)
                        r["fac_counts"][p][cid] += 1
                        if isinstance(p, int) and 1 <= p <= 6:
                            h = p in hints
                            r["hint"][(p, h)] += 1
                            r["hint_cp"][p][h] += 1
                            bond = (t["bond"] or {}).get(str(p))
                            if bond is not None:
                                r["hint_bond"][(bond >= 80, h)] += 1
                    if len(hints) > 1:
                        r["n_multi_hint_fac"] += 1
                for p in range(1, 7):
                    if p not in present:
                        r["fac_counts"][p]["away"] += 1
        # events and commands, in call order
        exec_ct = exec_cid = None
        exec_res = None
        evs = []
        for d, ep, payload in t["calls"]:
            if d == "REQ" and ep == "exec_command":
                exec_ct, exec_cid = payload.get("command_type"), payload.get("command_id")
                r["cmd_ids"][(exec_ct, exec_cid)] += 1
            elif d == "RES" and ep == "exec_command" and payload[0]:
                exec_res = payload[0]
            elif d == "REQ" and ep == "check_event":
                e = payload.get("event_id")
                evs.append(e)
                r["ev_total"][e] += 1
                r["ev_turn"][(e, turn)] += 1
        if exec_ct == 1 and exec_res and t["stats"]:
            st = t["stats"]
            stat = STAT_OF.get(exec_cid)
            key = "wit" if stat == "wiz" else stat
            gained = None
            if stat and st.get(key) is not None and exec_res.get(stat) is not None:
                gained = exec_res[stat] - st[key]
            r["xt"].append((turn, exec_cid, gained, 7017 in evs, 7020 in evs,
                            tuple(e for e in evs if e in (7004, 7003, 7022, 7023, 7002))))
    return r


if __name__ == "__main__":
    files = sorted(glob.glob(sys.argv[1] + "/*.pkl.gz"))
    agg_partner = Counter(); hint = Counter(); hint_bond = Counter()
    ratios_away, ratios_spec = [], []
    pooled = Counter()
    cp_rates = []
    ev_total = Counter(); ev_turn = Counter(); turns = Counter()
    xt = []; cmd_ids = Counter(); multi = 0
    with Pool(max(2, __import__("os").cpu_count() - 2)) as p:
        for r in p.imap_unordered(one, files, chunksize=8):
            agg_partner.update(r["partner_vals"]); hint.update(r["hint"]); hint_bond.update(r["hint_bond"])
            ev_total.update(r["ev_total"]); ev_turn.update(r["ev_turn"]); turns.update(r["turns"])
            cmd_ids.update(r["cmd_ids"]); multi += r["n_multi_hint_fac"]
            xt.extend(r["xt"])
            for pos in range(1, 7):
                fc = r["fac_counts"].get(pos)
                if not fc:
                    continue
                facs = [fc[f] for f in BASE_FAC]
                n = sum(facs) + fc["away"]
                if n < 30:
                    continue
                spec = max(range(5), key=lambda i: facs[i])
                others = [facs[i] for i in range(5) if i != spec]
                om = sum(others) / 4
                if om > 0:
                    ratios_away.append(fc["away"] / om)
                    ratios_spec.append(facs[spec] / om)
                pooled["away"] += fc["away"]; pooled["spec"] += facs[spec]; pooled["other"] += sum(others)
                pooled["n"] += n
            for pos, hc in r["hint_cp"].items():
                if sum(hc.values()) >= 40:
                    cp_rates.append(hc[True] / sum(hc.values()))

    def med(xs):
        xs = sorted(xs); return xs[len(xs) // 2] if xs else None

    print("== PLACEMENT (U-014)")
    print("partner values seen:", agg_partner.most_common(15))
    print("pooled:", dict(pooled), " away/other-mean =", round(pooled["away"] / (pooled["other"] / 4), 4),
          " spec/other-mean =", round(pooled["spec"] / (pooled["other"] / 4), 4),
          " away share =", wilson(pooled["away"], pooled["n"]))
    print("per career-position: median away/other", round(med(ratios_away), 3), " median spec/other", round(med(ratios_spec), 3),
          " n", len(ratios_away))
    qs = sorted(ratios_away)
    print("away/other quantiles 10/25/50/75/90:", [round(qs[int(len(qs) * q)], 3) for q in (.1, .25, .5, .75, .9)])
    qs = sorted(ratios_spec)
    print("spec/other quantiles 10/25/50/75/90:", [round(qs[int(len(qs) * q)], 3) for q in (.1, .25, .5, .75, .9)])

    print("== HINTS (U-004)")
    k = sum(v for (p, h), v in hint.items() if h); n = sum(hint.values())
    print("all placed deck cards:", wilson(k, n))
    for pos in range(1, 7):
        print("  position", pos, wilson(hint[(pos, True)], hint[(pos, True)] + hint[(pos, False)]))
    print("  bond<80:", wilson(hint_bond[(False, True)], hint_bond[(False, True)] + hint_bond[(False, False)]),
          " bond>=80:", wilson(hint_bond[(True, True)], hint_bond[(True, True)] + hint_bond[(True, False)]))
    print("  facilities with >1 hinting card:", multi)
    hist = Counter(round(x * 100) for x in cp_rates)
    print("  per career-position hint-rate histogram (%:count):", sorted(hist.items()))

    print("== SUPPORT EVENTS (U-002/U-003)")
    for e in (10002, 20000):
        last = max((t for (ee, t) in ev_turn if ee == e), default=None)
        byblk = {b: sum(v for (ee, t), v in ev_turn.items() if ee == e and t and (t - 1) // 6 == b) for b in range(14)}
        print(f"event {e}: total {ev_total[e]}, last turn {last}, per-6-turn-block {byblk}")
    ord_ids = [e for e in ev_total if isinstance(e, int) and 20001 <= e <= 20099]
    last = max((t for (ee, t) in ev_turn if ee in ord_ids), default=None)
    print(f"events 20001-20099 (hint-for-growth #3): total {sum(ev_total[e] for e in ord_ids)}, last turn {last}")
    for tt in range(66, 79):
        print(f"  turn {tt}: careers-turns {turns[tt]}, 10002 {ev_turn[(10002, tt)]}, 20000 {ev_turn[(20000, tt)]}, 2000x {sum(ev_turn[(e, tt)] for e in ord_ids)}")

    print("== EXTRA TRAINING / ACUPUNCTURE (U-013)")
    print("training command ids:", [(k, v) for k, v in cmd_ids.most_common(20) if k[0] == 1])
    ok = [x for x in xt if x[2] is not None]
    succ = [x for x in ok if x[2] > 0]; fail = [x for x in ok if x[2] <= 0]
    print("training turns with gain info:", len(ok), " success", len(succ), " fail", len(fail))
    print("fail-turn event ids:", Counter(e for x in fail for e in x[5]).most_common(), " succ-turn:", Counter(e for x in succ for e in x[5]).most_common())
    print("Extra Training | success, non-camp:", wilson(sum(x[3] for x in succ if x[0] not in CAMP), sum(1 for x in succ if x[0] not in CAMP)))
    print("Extra Training | failure:", wilson(sum(x[3] for x in fail), len(fail)))
    print("Extra Training | camp:", wilson(sum(x[3] for x in ok if x[0] in CAMP), sum(1 for x in ok if x[0] in CAMP)))
    for b in range(7):
        sub = [x for x in succ if x[0] not in CAMP and (x[0] - 1) // 12 == b]
        print(f"  ET by 12-turn block {b}:", wilson(sum(x[3] for x in sub), len(sub)))
    print("Acupuncture | all training turns:", wilson(sum(x[4] for x in ok), len(ok)))
    print("Acupuncture turns:", sorted(Counter(x[0] for x in xt if x[4]).items()))
    print("7020 total acks:", ev_total[7020], " 7017 total acks:", ev_total[7017])
