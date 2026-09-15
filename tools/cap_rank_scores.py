#!/usr/bin/env python3
"""
One-time backfill: clamp every persisted per-uma rank_score to
rating_formula.MAX_RANK_SCORE (999,999).

Context: rank_score (the trained_chara "career rank score" -- what shows on
the veteran roster / post-career results screen, and what Team Stadium's own
rank-score math multiplies per teammate) used to only be clamped to INT32_MAX
on generation. A rank_score anywhere near int32 territory has already caused
a live OverflowException crash once (see trained_chara.py's
MAXED_VETERAN_FAN_CEILING comment) once Team Stadium's math applies any
factor on top of it. rating_formula.get_rating/get_career_score now clamp new
scores to 999,999 going forward; this script backfills every score already
sitting in a persisted account from before that change.

Scope: this walks every viewer's ENTIRE persisted state (including the lazy
load_index cache) and clamps rank_score on every dict that also carries
trained_chara_id (the trained_chara record signature), plus the
career_factor_roll stash (the post-career spark-roll screen's cached
rank_score, keyed by "factors"+"rank_score"). It deliberately does NOT touch
account-wide aggregates that legitimately sum many umas' scores and already
exceed 999,999 on real captured accounts -- user_info.rank_score (profile
total), note_archive's directory_card/scenario_high_scores totals, and Team
Stadium's best_point/evaluation totals. Those are a different quantity (a
sum across a whole roster), not a single uma's score, and are out of scope
for this cap.

Usage:
    server/.venv/bin/python3.14 tools/cap_rank_scores.py [--dry-run]
    server/.venv/bin/python3.14 tools/cap_rank_scores.py <viewer_id> [--dry-run]

Back up server/data/state*.sqlite3 first. The client caches load/index
locally, so any affected account needs a full client restart to see it.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from app import rating_formula  # noqa: E402
from app import state as state_store  # noqa: E402

MAX_RANK_SCORE = rating_formula.MAX_RANK_SCORE


def _clamp_in_place(node, path: str, hits: list) -> None:
    """Recursively walk node, clamping rank_score on any dict that looks like
    a trained_chara record (has trained_chara_id) or the career_factor_roll
    stash (has factors + rank_score, no trained_chara_id)."""
    if isinstance(node, dict):
        score = node.get("rank_score")
        is_trained_chara = "trained_chara_id" in node
        is_factor_roll_stash = "factors" in node and "rank_score" in node
        if (is_trained_chara or is_factor_roll_stash) and isinstance(score, int) and score > MAX_RANK_SCORE:
            hits.append((path, score))
            node["rank_score"] = MAX_RANK_SCORE
        for k, v in node.items():
            _clamp_in_place(v, f"{path}.{k}", hits)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _clamp_in_place(v, f"{path}[{i}]", hits)


def _process(viewer_id: str, dry_run: bool) -> int:
    full_state = state_store.get_state(viewer_id)
    if full_state is None:
        return 0
    # load_index is lazy (excluded from get_state) but is its own cached
    # snapshot of trained_chara-shaped data the client may still be reading.
    load_index = state_store.load_lazy_key(viewer_id, "load_index")
    if load_index is not None:
        full_state["load_index"] = load_index

    hits: list = []
    _clamp_in_place(full_state, viewer_id, hits)

    if hits:
        print(f"viewer {viewer_id}: {len(hits)} score(s) clamped to {MAX_RANK_SCORE}")
        for path, old in hits:
            print(f"  {path}: {old} -> {MAX_RANK_SCORE}")
        if not dry_run:
            state_store.save_state(viewer_id, full_state)
    return len(hits)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("viewer_id", nargs="?", help="limit to one viewer; default: every account")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    viewer_ids = [str(args.viewer_id)] if args.viewer_id else state_store.all_viewer_ids()

    total = 0
    for vid in viewer_ids:
        total += _process(vid, args.dry_run)

    print(f"\n{total} score(s) over {MAX_RANK_SCORE} found across {len(viewer_ids)} account(s).")
    if args.dry_run:
        print("--dry-run: nothing written")
    elif total:
        print("written. FULLY restart the game client for any affected account (it caches load/index locally).")


if __name__ == "__main__":
    main()
