"""Regenerate server/data/trackblazer_event_pool.json.

Trackblazer's random story pool is ~126 one-shot events with no fixed turn, and
the only description of them that exists is the corpus: captures/bot_logs holds
1,741 real scenario-4 careers. This script measures each id's career rate and
turn window there, joins it to master.mdb for the story the wire carries, and
writes the table the producer reads.

WHY A DATA FILE. The alternative is 126 hand-written literals that nobody can
re-derive; this is the same shape as trackblazer_shop_model.json, which was
fitted from the same careers.

WHAT IS EXCLUDED. Ids already served from a schedule, ids that share a story
with another id (those are the recurring FAMILIES -- Training Level Up, the
Director's Appraisal, the two Achievement series -- not unique stories), and
anything seen on two turns or fewer (a fixed-turn beat, not a pool event).
"""

from __future__ import annotations

import collections
import glob
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

MASTER = ("C:/Program Files (x86)/Steam/steamapps/common/UmamusumePrettyDerby/"
          "UmamusumePrettyDerby_Data/Persistent/master/master.mdb")
OUT = os.path.join(ROOT, "server", "data", "trackblazer_event_pool.json")
BAND = (203000, 204000)
MIN_DISTINCT_TURNS = 3


def _served() -> set:
    from app import scenarios
    from app.scenarios.trackblazer import producers as T
    tb = [s for s in scenarios.all_scenarios() if s.scenario_id == 4][0]
    out = {T.ENDING_BEAT[0], tb.RIVAL_WIN_EVENT}
    for beats in T.FIXED_BEATS.values():
        out.update(b[0] for b in beats)
    for group in T.SEASONAL_GROUPS.values():
        out.update(group)
    for name in dir(T):
        v = getattr(T, name)
        if isinstance(v, dict) and name.endswith("BEATS"):
            for beats in v.values():
                if isinstance(beats, (list, tuple)):
                    out.update(b[0] for b in beats
                               if isinstance(b, (list, tuple)) and isinstance(b[0], int))
    for r in (tb.finals_events or {}).values():
        out.add(r["pre"][0])
        out.add(r["post"][0])
    out.update(tb.LEVELUP_EVENTS.values())
    for series in tb.appraisal_events().values():
        out.update(e for _b, e, _a in series)
    return out


def _corpus():
    acks = collections.Counter()
    careers = collections.Counter()
    turns = collections.defaultdict(set)
    n = 0
    for f in glob.glob(os.path.join(ROOT, "captures", "bot_logs", "career_log_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if d.get("scenario_id") != 4:
            continue
        n += 1
        seen = set()
        for t in d.get("turns") or []:
            for call in t.get("api_calls") or []:
                if call.get("direction") != "REQ":
                    continue
                if not (call.get("endpoint") or "").endswith("check_event"):
                    continue
                data = call.get("data") or {}
                e = data.get("event_id")
                if not isinstance(e, int) or not BAND[0] <= e < BAND[1]:
                    continue
                acks[e] += 1
                seen.add(e)
                if data.get("current_turn"):
                    turns[e].add(int(data["current_turn"]))
        for e in seen:
            careers[e] += 1
    return n, acks, careers, turns


def _wire_shapes() -> dict:
    """(event_id -> captured choice count), from every non-local capture."""
    shapes = collections.defaultdict(collections.Counter)

    def walk(o):
        if isinstance(o, dict):
            if "event_id" in o and "event_contents_info" in o:
                try:
                    e = int(o["event_id"])
                except Exception:
                    e = None
                ca = (o.get("event_contents_info") or {}).get("choice_array")
                if e is not None and BAND[0] <= e < BAND[1] and isinstance(ca, list):
                    shapes[e][len(ca)] += 1
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "captures")):
        if "bot_logs" in dirpath:
            continue
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(dirpath, fn), encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict) and d.get("upstream") == "local":
                continue      # our own output, not the real server
            walk(d)
    return {e: c.most_common(1)[0][0] for e, c in shapes.items()}


def main() -> None:
    con = sqlite3.connect("file:" + MASTER + "?mode=ro", uri=True)
    info = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT id, story_id, short_story_id FROM single_mode_story_data "
        "WHERE id BETWEEN ? AND ?", (BAND[0], BAND[1] - 1))}
    wire_of = {e: (sh or st) for e, (st, sh) in info.items()}
    shared = collections.Counter(wire_of.values())

    served = _served()
    shapes = _wire_shapes()
    n, acks, careers, turns = _corpus()

    events = {}
    for e in sorted(acks):
        if e in served or e not in wire_of:
            continue
        ts = sorted(turns[e])
        if len(ts) < MIN_DISTINCT_TURNS:
            continue                      # a fixed-turn beat, not a pool event
        if shared[wire_of[e]] > 1:
            continue                      # a recurring family, not a unique story
        events[str(e)] = {
            "story_id": wire_of[e],
            # Captured choice count where one exists; 1 otherwise -- see the
            # producer for why 1 is the safe floor.
            "choices": shapes.get(e, 1),
            "choices_captured": e in shapes,
            "turns": [ts[0], ts[-1]],
            "rate": round(careers[e] / n, 4),
        }
    json.dump({"_source": __doc__.strip().splitlines()[0],
               "_generator": "tools/gen_trackblazer_event_pool.py",
               "careers": n, "events": events},
              open(OUT, "w"), indent=1, sort_keys=True)
    print("careers %d -> %d pool events (%d acks)"
          % (n, len(events), sum(acks[int(k)] for k in events)))


if __name__ == "__main__":
    main()
