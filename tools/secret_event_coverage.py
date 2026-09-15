"""Coverage report for SECRET career events.

Two questions, and they are different:

    SERVABLE   does the event's GameTora title resolve to a real master.mdb
               story id? If not it can never be sent, whatever its conditions.
    GATEABLE   is every one of its condition clauses in a verb the evaluator
               understands? An event with one unknown clause never fires --
               deliberately (secret_events' whole point), but it should show up
               here as a gap rather than as silence.

Also runs the regression that started all this: with an EMPTY career, NOTHING
may be eligible. A secret event that fires on turn 1 is the random-event bug.

    python tools/secret_event_coverage.py            # summary
    python tools/secret_event_coverage.py --verbs    # + unsupported verb table
    python tools/secret_event_coverage.py --events   # + every ungateable event
"""

from __future__ import annotations

import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import event_engine, master_data                    # noqa: E402
from app.handlers import secret_events                       # noqa: E402
from app.handlers import single_mode_team                     # noqa: E402

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "server", "data", "events", "gametora")


def _story_titles(chara: int) -> dict:
    base = 500000000 + chara * 1000
    out = {}
    for r in master_data.query(
            'SELECT "index" i, text FROM text_data WHERE category=181 '
            'AND "index" BETWEEN ? AND ?', (base, base + 999)):
        if r["text"]:
            out.setdefault(event_engine.title_key(r["text"]), r["i"])
    return out


def main(argv) -> int:
    cards = sorted(int(os.path.basename(f)[6:-5])
                   for f in glob.glob(os.path.join(_CACHE, "chara_*.json")))
    # The REAL deadline resolver: without it every 'do not run race X' clause
    # reads as unanswerable and the empty-career test proves nothing.
    empty = secret_events.Context(turn=1,
                                  race_deadline=single_mode_team._race_deadline)
    total = servable = gateable = 0
    unsupported = collections.Counter()
    ungateable, fires_empty, unservable, unconditional = [], [], [], []
    titles_cache: dict = {}

    for card in cards:
        with open(os.path.join(_CACHE, f"chara_{card}.json"), encoding="utf-8") as fh:
            events = (json.load(fh).get("events") or {})
        chara = card // 100
        titles = titles_cache.setdefault(chara, _story_titles(chara))
        for ev in events.values():
            if not isinstance(ev, dict) or ev.get("section") != "secret":
                continue
            total += 1
            name = ev.get("name") or ""
            sid = titles.get(event_engine.title_key(name))
            if sid:
                servable += 1
            else:
                unservable.append((card, name))
            if secret_events.understood(ev):
                gateable += 1
            elif not (ev.get("conditions") or []):
                # No conditions at all: GameTora files it under `secret`, but
                # it has no precondition, so it is an ordinary random beat and
                # single_mode_team's random pool serves it (Smart Falcon's
                # 'Coming to a City Near You'). Not a gap.
                unconditional.append((card, name))
            else:
                ungateable.append((card, name, ev.get("conditions") or []))
                for verb in secret_events.unsupported_verbs(ev):
                    unsupported[verb] += 1
            if secret_events.is_eligible(ev, empty):
                fires_empty.append((card, name, ev.get("conditions") or []))

    print(f"{len(cards)} trainee pages | {total} secret events")
    print(f"  servable (title -> master story id): {servable} ({servable / max(1, total):.0%})")
    print(f"  gateable (all clauses understood):   {gateable} ({gateable / max(1, total):.0%})")
    print(f"  condition-less -> served as random:   {len(unconditional)}")
    print(f"  neither (a real gap):                {len(ungateable)}")
    print(f"  eligible on an EMPTY career:         {len(fires_empty)} (must be 0)")

    if unsupported and "--verbs" in argv:
        print("\nclauses we cannot evaluate (event count per verb):")
        for verb, n in unsupported.most_common():
            print(f"  {n:4d}  {verb}")
    if "--events" in argv:
        print(f"\n{len(ungateable)} ungateable events:")
        for card, name, cond in ungateable:
            print(f"  {card} {name!r}: {json.dumps(cond, ensure_ascii=False)}")
    if unservable and "--events" in argv:
        print(f"\n{len(unservable)} events whose title resolves to no story id:")
        for card, name in unservable:
            print(f"  {card} {name!r}")
    if fires_empty:
        print("\nFAIL -- these fire with nothing achieved:")
        for card, name, cond in fires_empty:
            print(f"  {card} {name!r}: {json.dumps(cond, ensure_ascii=False)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
