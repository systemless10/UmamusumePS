import json, math, sys
from collections import Counter, defaultdict

CAMP = {37, 38, 39, 40, 61, 62, 63, 64}
REST_IDS = {7009: 30, 7010: 50, 7011: 70}


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - m) / d, 100 * (c + m) / d)


rows = json.load(open(sys.argv[1]))
rest = [r for r in rows if r[2] == 7]
rec = [r for r in rows if r[2] == 3]
inf = [r for r in rows if r[2] == 8]
print("rest", len(rest), "outing", len(rec), "infirmary", len(inf))

# 1. what events show up on rest turns at all
ev_counter = Counter()
none_rest = 0
multi = 0
for sc, turn, ct, cid, evs, b, a, mh in rest:
    ids = [e for e in evs if e in REST_IDS]
    if not ids:
        none_rest += 1
    if len(ids) > 1:
        multi += 1
    for e in evs:
        ev_counter[e] += 1
print("rest turns with NO rest flavor id:", none_rest, " with >1:", multi)
print("all event ids on rest turns (top 25):", ev_counter.most_common(25))
print("rest command_ids:", Counter(r[3] for r in rest).most_common())
print("rest no-flavor by camp/turn:", Counter((r[1] in CAMP) for r in rest
      if not any(e in REST_IDS for e in r[4])))
print("rest no-flavor sample events:", Counter(tuple(r[4]) for r in rest
      if not any(e in REST_IDS for e in r[4])).most_common(10))


def tiers(subset, label):
    c = Counter()
    for r in subset:
        ids = [e for e in r[4] if e in REST_IDS]
        if len(ids) == 1:
            c[ids[0]] += 1
    n = sum(c.values())
    parts = []
    for e in (7011, 7010, 7009):
        lo, hi = wilson(c[e], n)
        parts.append(f"+{REST_IDS[e]}: {100*c[e]/max(n,1):.2f}% [{lo:.2f},{hi:.2f}]")
    print(f"{label:28s} n={n:6d}  " + "  ".join(parts))


tiers(rest, "ALL")
tiers([r for r in rest if r[1] not in CAMP], "non-camp")
tiers([r for r in rest if r[1] in CAMP], "camp")
for sc in sorted({r[0] for r in rest}):
    tiers([r for r in rest if r[0] == sc], f"scenario {sc}")
for lo, hi in ((1, 24), (25, 48), (49, 72), (73, 78)):
    tiers([r for r in rest if lo <= (r[1] or 0) <= hi], f"turns {lo}-{hi}")
for b0, b1 in ((0, 30), (31, 50), (51, 100)):
    tiers([r for r in rest if r[5] is not None and b0 <= r[5] <= b1], f"vital before {b0}-{b1}")

# 2. vital delta per flavor id (uncapped only)
dd = defaultdict(Counter)
for sc, turn, ct, cid, evs, b, a, mh in rest:
    ids = [e for e in evs if e in REST_IDS]
    if len(ids) == 1 and b is not None and a is not None:
        cap = (mh or 100)
        if a < cap:
            dd[ids[0]][a - b] += 1
for e in (7011, 7010, 7009):
    print("delta for", e, dd[e].most_common(6))

# 3. crane (6002) on outing turns, camp excluded
def rate(subset, eid, label):
    k = sum(1 for r in subset if eid in r[4])
    n = len(subset)
    lo, hi = wilson(k, n)
    print(f"{label:34s} {k}/{n} = {100*k/max(n,1):.2f}% [{lo:.2f},{hi:.2f}]")

rate(rec, 6002, "crane on all outing turns")
rate([r for r in rec if r[1] not in CAMP], 6002, "crane on non-camp outing turns")
rate([r for r in rec if r[1] in CAMP], 6002, "crane on camp outing turns")
print("outing command_ids non-camp:", Counter(r[3] for r in rec if r[1] not in CAMP).most_common(8))
print("outing event ids non-camp (top 20):",
      Counter(e for r in rec if r[1] not in CAMP for e in r[4]).most_common(20))
print("infirmary event ids:", Counter(e for r in inf for e in r[4]).most_common(8))
