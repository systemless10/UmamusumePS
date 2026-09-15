#!/usr/bin/env python3
"""
Grant a viewer EVERY support card in master.mdb at full MLB (uncap 4, max level).

Testing utility: makes every card available so any deck can be built in-game.
A *deck* is only 6 slots -- this fills the owned COLLECTION, which is what the
deck editor picks from.

Level/exp caps come from master.mdb, not hardcoded:
    support_card_limit  rarity -> max level per uncap (limit_0..limit_4)
    support_card_level  (rarity, level) -> total_exp
At uncap 4 that is R lv40 / SR lv45 / SSR lv50 (exp 40510 / 74990 / 118185).

IMPORTANT -- viewer_id must be the literal "<redacted>" placeholder, never a
real id. patch.py rewrites "<redacted>" to the live numeric viewer_id; a real
value passes through unrewritten and the client rejects the whole response
(it expects an int). This is a documented past bug -- see handoff.md.

Existing cards keep their possess_time/create_time; only uncap+exp are raised
(never lowered). New cards are appended.

Usage:
    server/.venv/bin/python3.14 tools/grant_all_support_cards.py <viewer_id> [--dry-run]

Back up server/data/state.sqlite3 first. The client caches load/index in its
local save, so it must be FULLY restarted to see the change.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from app import master_data as md  # noqa: E402
from app import state as state_store  # noqa: E402
from app.handlers import collection  # noqa: E402

REDACTED = "<redacted>"
MAX_UNCAP = 4
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def max_exp_by_rarity() -> dict[int, int]:
    """rarity -> total_exp at its uncap-4 level cap, straight from master."""
    out = {}
    for rarity, cap in md.query("SELECT rarity, limit_4 FROM support_card_limit"):
        row = md.query_one(
            "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
            (rarity, cap),
        )
        if row:
            out[rarity] = row["total_exp"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("viewer_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    viewer_id = str(args.viewer_id)
    full_state = state_store.get_state(viewer_id)
    if full_state is None:
        sys.exit(f"no state for viewer {viewer_id} -- has this account logged in?")

    owned = full_state.get(collection.SUPPORT_CARD_KEY)
    if owned is None:
        sys.exit(
            f"viewer {viewer_id} has no {collection.SUPPORT_CARD_KEY} yet; "
            "log in once so load/index seeds it, then re-run"
        )

    exp_cap = max_exp_by_rarity()
    all_cards = md.query("SELECT id, rarity FROM support_card_data ORDER BY id")

    by_id = {c["support_card_id"]: c for c in owned}
    now = datetime.now().strftime(TIME_FORMAT)
    added = upgraded = unchanged = 0

    for card in all_cards:
        cid, rarity = card["id"], card["rarity"]
        target_exp = exp_cap.get(rarity)
        if target_exp is None:
            print(f"  ! skipping {cid}: no level cap for rarity {rarity}")
            continue

        entry = by_id.get(cid)
        if entry is None:
            owned.append({
                "viewer_id": REDACTED,
                "support_card_id": cid,
                "exp": target_exp,
                "limit_break_count": MAX_UNCAP,
                "favorite_flag": 0,
                "stock": 0,
                "possess_time": now,
                "create_time": now,
            })
            added += 1
        elif entry.get("limit_break_count", 0) < MAX_UNCAP or entry.get("exp", 0) < target_exp:
            entry["limit_break_count"] = max(entry.get("limit_break_count", 0), MAX_UNCAP)
            entry["exp"] = max(entry.get("exp", 0), target_exp)
            entry["viewer_id"] = REDACTED  # normalize any real id that crept in
            upgraded += 1
        else:
            unchanged += 1

    print(f"master.mdb support cards : {len(all_cards)}")
    print(f"  added (new)            : {added}")
    print(f"  upgraded to MLB        : {upgraded}")
    print(f"  already MLB            : {unchanged}")
    print(f"  collection total now   : {len(owned)}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    full_state[collection.SUPPORT_CARD_KEY] = owned
    state_store.save_state(viewer_id, full_state)
    print("\nwritten. FULLY restart the game client (it caches load/index locally).")


if __name__ == "__main__":
    main()
