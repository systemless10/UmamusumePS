#!/usr/bin/env python3
"""
Extract support_card_unique_effect (master.mdb) into a clean, decoded JSON
asset -- the per-card "unique effect" (a.k.a. hobby/character-award) bonus
every SSR+ support card gets on top of its normal support_card_effect_table
values, gated behind a real card level (the `lv` column) and, for most of
them, an in-training friendship-gauge threshold.

WHY THIS EXISTS
---------------
training_formula.py's unique_effect() already reads this table LIVE via SQL
every time it's called -- that's fine for the STANDARD effect types (the
same 0-30 vocabulary support_card_effect_table uses: speed/stamina/power/
guts/wit bonus, training effectiveness, skill point bonus, ...), which
unique_effect() already handles generically. What it does NOT decode is the
101-114 "Character Award" range: bespoke, one-mechanic-per-card-or-family
effects the raw type/value columns alone don't explain -- you need the
REAL in-game description text (text_data category 155, keyed by support_
card_id) to know what a given type code actually DOES.

This script pulls every unique-effect row, matches it against that real
description text, and -- ONLY where the row's raw fields are independently
VERIFIED to reproduce the description (see DECODING NOTES below) -- emits a
decoded interpretation alongside the raw fields. Rows this session could not
verify are still fully exported (never dropped), just left undecoded with
their real description attached, so nothing is guessed at or fabricated.

Re-runnable against ANY master.mdb (set MASTER_MDB_PATH before running --
same env var server/app/master_data.py already honours) -- this is meant to
be run again against the JP server's own master.mdb once that's available,
not a one-off transcription of THIS server's data.

USAGE
-----
    server/.venv/Scripts/python.exe tools/extract_support_card_unique_effects.py
    MASTER_MDB_PATH=/path/to/jp/master.mdb  server/.venv/Scripts/python.exe tools/extract_support_card_unique_effects.py --out data/training_ref/support_card_unique_effects.jp.json

DECODING NOTES (what's actually verified, not guessed)
--------------------------------------------------------
Every row has TWO effect slots (type_0/value_0/value_0_1..4, and type_1/
value_1/value_1_1..4 -- type_1 IS commonly used, 124 of 163 real cards have
one; an early pass of this script wrongly assumed it was always unused off
a 2-card sample, corrected once the full run showed otherwise). A slot's
`type` is either:

  * one of the SAME 0-30 codes support_card_effect_table/training_formula.py
    already use for the standard (non-unique) per-card bonuses -- EFFECT_
    FRIENDSHIP=1, EFFECT_MOOD=2, EFFECT_STAT_BONUS_BASE=3..7, EFFECT_
    TRAINING=8, EFFECT_SKILL_POINT=30, EFFECT_WIT_RECOVERY=31 are all
    already-real constants there. PLUS two genuinely NEW ones this pass
    decoded that training_formula.py has NEVER named at all (not a unique-
    effect-only gap -- these are standard 0-30 codes, so they'd also occur
    in plain support_card_effect_table rows this project has been reading
    blind): type 15 = "race_bonus", type 19 = "specialty_priority" --
    verified against 6 real cards each, where the OTHER slot's own
    (already-known) effect name lines up exactly with the description's
    first half every single time (e.g. type_0=2 "Mood Effect" + type_1=15
    -> real description "Mood Effect and Race Bonus", repeated
    consistently across every sample checked). These slots decode exactly
    like a normal support_card_effect_table row and are marked
    "kind": "standard".

  * 101 -- VERIFIED against all 20 real cards that use it (every single one
    matches its own real description text exactly): a "friendship-gated
    dual bonus" template. value_0 is the friendship-gauge THRESHOLD (80 or
    100 in every real row); value_0_1/value_0_2 are (sub_effect_type,
    sub_effect_value) for the first bonus; value_0_3/value_0_4 are the SAME
    pair for an optional second bonus (sub_effect_type 0 = no second
    bonus). Marked "kind": "friendship_gated_dual_bonus".

  * 102-114 -- genuinely bespoke, ONE new mechanic each (confirmed: each
    code's real description text is materially different from every other
    code's, e.g. 102 "when not on preferred training", 104 "based on fans
    gained up to 200,000", 108 "failure rate may be reduced to 0%"). This
    session did not verify a formula for any of these against the raw
    value_0.._4 columns (each has only 1-2 real cards to check against,
    not enough to be confident a guessed formula is actually right) --
    exported with "kind": "bespoke_undecoded" and the full raw values +
    real description, for a future pass (or the JP-server data-mining
    pass this was requested for) to pin down properly.

FOLLOW-UP THIS FOUND: RESOLVED 2026-08-19. EFFECT_RACE_BONUS=15 and
EFFECT_SPECIALTY_PRIORITY=19 are now real named constants in training_
formula.py. Race Bonus's BASE value (cards.json's own "race_bonus" key,
deck-wide sum) and Specialty Priority's BASE value (card_effect's max-level
"specialty_priority", added to the card's own facility weight) were both
already wired into single_mode_team.py (_race_bonus_pct / _roll_
distribution) from an earlier pass -- what was actually still missing was
each one's UNIQUE-effect half (support_card_unique_effect rows using type
15/19, e.g. card 20003's unique +5% Race Bonus at level 25, Kitasan Black
SSR 30028's unique +20 Specialty Priority weight at level 30), now added
via unique_effect() at both call sites, gated by each card's real level
(single_mode_team._deck_support_card_levels, read from chara_info's own
support_card_array exp rather than threading full_state through the whole
call chain).

idle_mode_sub_rate is exported verbatim per card -- name suggests an idle-
training-specific reduction, not independently verified against any
capture this session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "server"))

from app import master_data  # noqa: E402

# The SAME vocabulary training_formula.py's EFFECT_* constants use --
# duplicated here (not imported) so this script stays runnable standalone
# against a bare master.mdb with no server package needed beyond master_data.
_STANDARD_EFFECT_NAMES = {
    0: "none",
    1: "friendship_bonus",              # training_formula.py EFFECT_FRIENDSHIP
    2: "mood_effect",                   # training_formula.py EFFECT_MOOD
    3: "speed_bonus",
    4: "stamina_bonus",
    5: "power_bonus",
    6: "guts_bonus",
    7: "wit_bonus",
    8: "training_effectiveness",
    9: "initial_speed",
    10: "initial_stamina",
    11: "initial_power",
    12: "initial_guts",
    13: "initial_wit",
    14: "initial_bond",
    15: "race_bonus",                   # NEWLY decoded this pass -- see
                                        # DECODING NOTES. NOT in training_
                                        # formula.py's EFFECT_* constants at
                                        # all yet (a genuine pre-existing
                                        # gap, not specific to unique
                                        # effects -- this is a standard 0-30
                                        # code, appears on plain support_
                                        # card_effect_table rows too).
    17: "hint_level",
    18: "hint_frequency",
    19: "specialty_priority",           # NEWLY decoded this pass, same
                                        # "missing from training_formula.py
                                        # entirely" situation as 15.
    25: "event_recovery",
    26: "event_effect",
    27: "failure_rate_down",
    28: "energy_cost_down",
    30: "skill_point_bonus",
    31: "wit_friendship_recovery",
}

# Verified 2026-08-19 (see module docstring) against all 20 real type-101
# cards this master.mdb has -- every one matches its own real text_data
# description exactly.
_FRIENDSHIP_GATED_DUAL_BONUS = 101

# Decoded AND FULLY WIRED into training_formula.py's live calculate_
# training_gain as of 2026-08-19. 102/103/104 verified via internal
# master.mdb consistency (see their own comments there); 108/109/110/111/
# 114 verified against gametora.com's own per-card "Unique Effect" text,
# fetched directly one card at a time; 113 predates this pass entirely.
# 105/106/112 finished this same pass (105 at career-seed time, 106 via a
# new per-career stack counter, 112 as a failure-roll proc) -- see each
# constant's own comment in training_formula.py for the exact gametora
# quote and wiring. 107 is also wired but with a TEMPORARY, user-directed
# placeholder rate (gametora never exposed real numbers for 30094 -- see
# training_formula._low_energy_friendship_bonus's docstring); flagged in
# its own name below so it's never mistaken for a confirmed formula.
_IMPLEMENTED_BESPOKE = {
    102: "training_bonus_if_not_preferred_facility",
    103: "training_bonus_if_deck_type_count",
    104: "training_bonus_if_fans_gained",
    105: "initial_stat_deck_composition (gametora-confirmed, career-seed only)",
    106: "friendship_bonus_stacking (gametora-confirmed, per-career counter)",
    107: "friendship_bonus_if_low_energy (TEMPORARY placeholder rate -- "
        "gametora exposed no numbers for this card, see training_formula.py)",
    108: "training_bonus_if_max_energy (gametora-confirmed)",
    109: "training_bonus_if_deck_bond_sum (gametora-confirmed)",
    110: "training_bonus_if_cards_in_facility (gametora-confirmed)",
    111: "training_bonus_if_facility_level (gametora-confirmed)",
    112: "failure_rate_zero_chance (gametora-confirmed proc)",
    113: "friendship_energy_cost_down",
    114: "training_bonus_if_current_energy (gametora-confirmed)",
}

# Formula CONFIRMED (gametora.com, fetched directly) but NOT wired into
# calculate_training_gain -- each needs something outside that function's
# reach (a different code path entirely, or new persistent state it has
# nowhere to keep). Each entry: (confirmed_formula, why_not_wired). Empty
# as of 2026-08-19 -- 105/106/112 (the last three that needed this) are
# now wired; kept as an empty dict rather than removed so a FUTURE decode
# that hits the same "needs code outside calculate_training_gain" wall has
# an obvious place to land.
_CONFIRMED_NOT_WIRED = {}

# Types gametora's own page did not expose a precise formula for (asked
# directly, with different prompts -- the page only ever returned a
# qualitative description, no numbers) -- left as a structural read only,
# per this project's "don't guess in live gameplay" rule. Entry:
# (hypothesis, reason_unconfirmed). Empty as of 2026-08-19: 107 (the one
# entry this held) now has a user-directed TEMPORARY formula instead (see
# _IMPLEMENTED_BESPOKE) rather than staying unresolved.
_STRUCTURAL_HYPOTHESES = {}


def _effect_name(type_code: int | None) -> str:
    if type_code is None:
        return "none"
    return _STANDARD_EFFECT_NAMES.get(type_code, f"unknown_{type_code}")


def _decode_slot(type_code, values: list) -> dict | None:
    """One effect slot (type_N + its value_N.._4 columns) -> a decoded dict,
    or None if the slot is unused (type 0)."""
    if not type_code:
        return None
    if type_code == _FRIENDSHIP_GATED_DUAL_BONUS:
        threshold, t1, v1, t2, v2 = values
        bonuses = [{"effect_type": _effect_name(t1), "effect_type_code": t1, "value": v1}]
        if t2:
            bonuses.append({"effect_type": _effect_name(t2), "effect_type_code": t2, "value": v2})
        return {
            "kind": "friendship_gated_dual_bonus",
            "friendship_threshold": threshold,
            "bonuses": bonuses,
        }
    if type_code in _STANDARD_EFFECT_NAMES:
        return {
            "kind": "standard",
            "effect_type": _effect_name(type_code),
            "effect_type_code": type_code,
            "value": values[0],
        }
    if type_code in _IMPLEMENTED_BESPOKE:
        return {
            "kind": "implemented",
            "name": _IMPLEMENTED_BESPOKE[type_code],
            "effect_type_code": type_code,
            "raw_values": values,
            "wired_in": "training_formula.py",
        }
    if type_code in _CONFIRMED_NOT_WIRED:
        formula, reason = _CONFIRMED_NOT_WIRED[type_code]
        return {
            "kind": "confirmed_not_wired",
            "effect_type_code": type_code,
            "raw_values": values,
            "confirmed_formula": formula,
            "not_wired_reason": reason,
        }
    if type_code in _STRUCTURAL_HYPOTHESES:
        hypothesis, out_of_scope = _STRUCTURAL_HYPOTHESES[type_code]
        return {
            "kind": "bespoke_structural_hypothesis",
            "effect_type_code": type_code,
            "raw_values": values,
            "hypothesis": hypothesis,
            "out_of_scope_reason": out_of_scope,
        }
    return {
        "kind": "bespoke_undecoded",
        "effect_type_code": type_code,
        "raw_values": values,
    }


def _text(category: int, index: int) -> str | None:
    row = master_data.query_one(
        'SELECT text FROM text_data WHERE category=? AND "index"=?', (category, index))
    return row["text"] if row else None


def extract() -> list[dict]:
    rows = master_data.query(
        "SELECT * FROM support_card_unique_effect ORDER BY id, lv")
    out = []
    for r in rows:
        d = dict(r)
        card = master_data.query_one(
            "SELECT chara_id, rarity FROM support_card_data WHERE id=?", (d["id"],))
        slot0 = _decode_slot(d["type_0"], [d["value_0"], d["value_0_1"], d["value_0_2"],
                                           d["value_0_3"], d["value_0_4"]])
        slot1 = _decode_slot(d["type_1"], [d["value_1"], d["value_1_1"], d["value_1_2"],
                                           d["value_1_3"], d["value_1_4"]])
        out.append({
            "support_card_id": d["id"],
            "chara_id": card["chara_id"] if card else None,
            "rarity": card["rarity"] if card else None,
            "unlock_level": d["lv"],
            "unique_skill_name": _text(150, d["id"]),
            "description": _text(155, d["id"]),
            "idle_mode_sub_rate": d["idle_mode_sub_rate"],
            "effects": [s for s in (slot0, slot1) if s is not None],
            "_raw": {k: d[k] for k in (
                "type_0", "value_0", "value_0_1", "value_0_2", "value_0_3", "value_0_4",
                "type_1", "value_1", "value_1_1", "value_1_2", "value_1_3", "value_1_4",
            )},
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(_ROOT / "data" / "training_ref"
                                         / "support_card_unique_effects.json"),
                    help="output JSON path")
    args = ap.parse_args()

    data = extract()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    from collections import Counter
    kinds = Counter(e["kind"] for c in data for e in c["effects"])
    print(f"master.mdb: {master_data.MASTER_MDB_PATH}")
    print(f"{len(data)} cards with a unique effect -> {out_path}")
    for kind in ("standard", "friendship_gated_dual_bonus", "implemented",
                "confirmed_not_wired", "bespoke_structural_hypothesis",
                "bespoke_undecoded"):
        if kinds.get(kind):
            print(f"  {kinds[kind]:>3} {kind}")


if __name__ == "__main__":
    main()
