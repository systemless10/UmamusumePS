"""Crane window + per-eligible-outing rate. Plain recreation only (group 301)."""
import json, glob, os, math
from collections import Counter
from multiprocessing import Pool

LOGS = r"C:\Users\Systemless\Documents\UmaPS\captures\bot_logs"
CAMP = {37, 38, 39, 40, 61, 62, 63, 64}


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - m) / d, 100 * (c + m) / d)


def one(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    rows = []
    for t in d.get("turns") or []:
        calls = t.get("api_calls") or []
        for i, c in enumerate(calls):
            if c.get("direction") != "REQ" or not (c.get("endpoint") or "").endswith("/exec_command"):
                continue
            data = c.get("data") or {}
            if data.get("command_type") != 3 or data.get("command_group_id") == 390:
                continue
            evs = []
            for cj in calls[i + 1:]:
                ep = cj.get("endpoint") or ""
                if cj.get("direction") == "REQ":
                    if ep.endswith("/exec_command") or "race" in ep:
                        break
                    if ep.endswith("/check_event"):
                        evs.append((cj.get("data") or {}).get("event_id"))
            rows.append((data.get("current_turn"), 6002 in evs or 6007 in evs,
                         data.get("command_group_id"), tuple(evs)))
    return (d.get("scenario_id"), rows)


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(LOGS, "career_log_*.json")))
    careers = []
    with Pool(max(2, os.cpu_count() - 2)) as p:
        for r in p.imap_unordered(one, files, chunksize=4):
            if r:
                careers.append(r)
    crane_turns = Counter()
    outing_turns = Counter()
    groups = Counter()
    for sc, rows in careers:
        for turn, crane, grp, evs in rows:
            outing_turns[turn] += 1
            groups[grp] += 1
            if crane:
                crane_turns[turn] += 1
    print("groups:", groups)
    print("crane turns:", sorted(crane_turns.items()))
    print("min/max crane turn:", min(crane_turns), max(crane_turns))
    print("outings per turn 13-30:", {t: outing_turns[t] for t in range(13, 31)})
    print("outings per turn 70-78:", {t: outing_turns[t] for t in range(70, 79)})
    for start in (13, 19, 22, 25, 26):
        for end in (72, 76):
            k = n = 0
            for sc, rows in careers:
                done = False
                for turn, crane, grp, evs in rows:
                    if done or turn in CAMP or not (start <= turn <= end):
                        continue
                    n += 1
                    if crane:
                        k += 1
                        done = True
            lo, hi = wilson(k, n)
            print(f"window {start}-{end}: {k}/{n} = {100*k/max(n,1):.2f}% [{lo:.2f},{hi:.2f}]")
    # same window, but only careers' FIRST eligible outing, to test for a flat vs rising hazard
    k = n = 0
    for sc, rows in careers:
        el = [r for r in rows if 25 <= r[0] <= 72 and r[0] not in CAMP]
        if el:
            n += 1; k += el[0][1]
    lo, hi = wilson(k, n)
    print(f"first eligible outing only: {k}/{n} = {100*k/max(n,1):.2f}% [{lo:.2f},{hi:.2f}]")
