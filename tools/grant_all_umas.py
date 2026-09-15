#!/usr/bin/env python3
"""
Grant a viewer EVERY trainable uma (trainee card) at max stars + max potential.

Testing utility, mirror of grant_all_support_cards.py. Sets, per card:
    rarity       -> 5   (star rating; max confirmed for all cards in
                         card_rarity_data -- zero cards cap below 5)
    talent_level -> 5   (this is "potential"/awakening; card_talent_upgrade
                         tops out at 5)

Only cards with `card_data.default_rarity > 0` are granted. The two rows with
default_rarity 0 (9100101, 9101101) are non-trainable NPC variants -- granting
them would put cards in the list the player can never legitimately own.

`skill_data_array` (skill HINT levels) is deliberately left ALONE: hints are a
separate system from potential, and inventing hint levels would change SP costs
in a way that isn't what "max potential" asks for.

IMPORTANT: unlike support cards, these entries carry no viewer_id field at all
in the real shape, so there is no "<redacted>" placeholder to preserve here --
do not add one.

Existing cards keep their create_time; values are only ever raised, never
lowered, so re-running is safe.

Usage:
    server/.venv/bin/python3.14 tools/grant_all_umas.py <viewer_id> [--dry-run]

Back up server/data/state.sqlite3 first. The client caches load/index locally,
so it must be FULLY restarted to see the change.
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

MAX_TALENT = 5
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("viewer_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    viewer_id = str(args.viewer_id)
    full_state = state_store.get_state(viewer_id)
    if full_state is None:
        sys.exit(f"no state for viewer {viewer_id} -- has this account logged in?")

    owned = full_state.get(collection.CARD_LIST_KEY)
    if owned is None:
        sys.exit(
            f"viewer {viewer_id} has no {collection.CARD_LIST_KEY} yet; "
            "log in once so load/index seeds it, then re-run"
        )
    charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
    chara_by_id = {c.get("chara_id"): c for c in charas}

    # max star rating per card, from master (not assumed to be 5 for all)
    max_rarity = {
        r["card_id"]: r["m"]
        for r in md.query("SELECT card_id, MAX(rarity) AS m FROM card_rarity_data GROUP BY card_id")
    }
    trainable = md.query("SELECT id, chara_id FROM card_data WHERE default_rarity > 0 ORDER BY id")

    by_id = {c["card_id"]: c for c in owned}
    now = datetime.now().strftime(TIME_FORMAT)
    added = upgraded = unchanged = chara_added = 0

    for row in trainable:
        cid, chara_id = row["id"], row["chara_id"]
        target_rarity = max_rarity.get(cid)
        if target_rarity is None:
            print(f"  ! skipping {cid}: no card_rarity_data rows")
            continue

        entry = by_id.get(cid)
        if entry is None:
            owned.append({
                "card_id": cid,
                "rarity": target_rarity,
                "talent_level": MAX_TALENT,
                "create_time": now,
                "skill_data_array": [],
            })
            added += 1
        elif entry.get("rarity", 0) < target_rarity or entry.get("talent_level", 0) < MAX_TALENT:
            entry["rarity"] = max(entry.get("rarity", 0), target_rarity)
            entry["talent_level"] = max(entry.get("talent_level", 0), MAX_TALENT)
            upgraded += 1
        else:
            unchanged += 1

        # BUG FIXED 2026-08-26 (user-reported): granting a card here never
        # mirrored the character into chara_collection, the list every OTHER
        # ownership path (gacha.py/presents.py/shop.py) populates the instant
        # a chara's first card is obtained. Every screen that offers "which
        # character do you own" (home-screen decoration, etc.) reads THAT
        # list, not card_collection -- so a card granted only here was owned
        # but never selectable anywhere. Same entry shape as those three.
        if chara_id and chara_id not in chara_by_id:
            new_entry = {"chara_id": chara_id, "training_num": 0, "love_point": 0,
                        "fan": 1, "max_grade": 0, "dress_id": 2, "mini_dress_id": 2,
                        "love_point_pool": 0}
            charas.append(new_entry)
            chara_by_id[chara_id] = new_entry
            chara_added += 1

    print(f"trainable umas in master : {len(trainable)}")
    print(f"  added (new)            : {added}")
    print(f"  upgraded to 5*/talent 5: {upgraded}")
    print(f"  already maxed          : {unchanged}")
    print(f"  collection total now   : {len(owned)}")
    print(f"  chara_collection added : {chara_added} (now {len(charas)}) -- "
          f"this is what makes them selectable in-game (home screen, etc.)")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    full_state[collection.CARD_LIST_KEY] = owned
    state_store.save_state(viewer_id, full_state)
    print("\nwritten. FULLY restart the game client (it caches load/index locally).")


if __name__ == "__main__":
    main()
