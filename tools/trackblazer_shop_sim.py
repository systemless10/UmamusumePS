"""Simulate Trackblazer (single_mode_free) shop generation.

The real generator lives on Cygames' server. This reproduces it from parameters
fitted to 1,741 observed careers (~1,650 per reset); see
docs/TRACKBLAZER_SHOP.md for the derivation and the evidence behind each rule.

THE MODEL IN ONE PARAGRAPH
--------------------------
A reset's BASE lineup is N ordered slots. Each slot independently fills with
its own probability p_j; if it fills, it draws one item from its own pool q_j.
Unfilled slots are simply omitted, which is why observed lineups vary in length
and why the array is always ordered by item category. Separately, RACING is the
only thing that ever adds a LIMITED offer: the turn after a race, a placement-
dependent roll decides whether offers arrive and how many, and each lasts
exactly 2 turns.

WHY NOT A BINOMIAL
------------------
Because the p_j differ per slot, lineup size is Poisson-binomial, not binomial.
Its variance sum(p_j(1-p_j)) is strictly below a binomial's with the same mean,
which is exactly the underdispersion the real data shows. Fitting a binomial
instead is rejected at p = 1e-23 .. 1e-6; this model fits at p = 0.08 .. 0.91.

USAGE
-----
    from trackblazer_shop_sim import ShopSim
    sim = ShopSim(seed=1)
    sim.base_lineup(3)                      # -> [item_id, ...] in slot order
    sim.race_offers(3, grade='G1', place=1)  # -> [item_id, ...]
    sim.career()                            # -> all 11 resets

    python tools/trackblazer_shop_sim.py --demo
    python tools/trackblazer_shop_sim.py --validate docs/tb_shop_all.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(ROOT, "server", "app", "data", "trackblazer_shop_model.json")

# Placement -> the bucket the offer rules actually use. The coin table has four
# tiers (1st / 2nd-3rd / 4th-5th / 6th+) but 2nd-3rd and 4th-5th behave
# identically (39.2% vs 38.9% over 9,162 races), so the shop uses three.
def place_bucket(place: int) -> str:
    if place == 1:
        return "1st"
    if 2 <= place <= 5:
        return "2-5th"
    return "6th+"


# Debut races never produce an offer -- 1,602 observed wins, not one of them.
NO_OFFER_GRADES = {"Debut"}


class ShopSim:
    def __init__(self, model_path: str = MODEL_PATH, seed=None):
        with io.open(model_path, encoding="utf-8") as fh:
            self.model = json.load(fh)
        self.rng = random.Random(seed)
        self.resets = self.model["resets"]
        self.offers = self.model["race_offers"]

    # ------------------------------------------------------------ base stock --

    def base_lineup(self, shop_id) -> list:
        """One reset's base stock, as item_ids in slot order.

        Each slot is an independent Bernoulli(p_j) followed by a draw from that
        slot's own pool. Empty slots vanish, so the returned list is shorter
        than the slot count and its length varies run to run.
        """
        out = []
        for slot in self.resets[str(shop_id)]["slots"]:
            if self.rng.random() < slot["fill_p"]:
                out.append(self._draw(slot["items"]))
        return out

    # -------------------------------------------------------- race offers --

    def race_offers(self, shop_id, grade: str = "G1", place: int = 1) -> list:
        """Limited offers granted the turn AFTER a race.

        Returns item_ids; each offer expires 2 turns after it appears. An empty
        list is the common case -- a win only pays out 75% of the time.
        """
        if grade in NO_OFFER_GRADES:
            return []
        bucket = place_bucket(place)
        p_any = self.offers["p_any"].get(bucket, 0.0)
        if p_any <= 0 or self.rng.random() >= p_any:
            return []
        counts = self.offers["count_given_any"][bucket]
        n = int(self._draw(counts))
        pool = self.resets[str(shop_id)]["limited_pool"]
        return [self._draw(pool) for _ in range(n)]

    # ------------------------------------------------------------- career --

    def career(self, races=None) -> dict:
        """All 11 resets for one career.

        `races` optionally maps shop_id -> list of (grade, placement); their
        offers are rolled per reset so a simulated career includes both halves
        of the system.
        """
        out = {}
        for shop_id in sorted(self.resets, key=int):
            r = self.resets[shop_id]
            entry = {
                "shop_id": int(shop_id),
                "turns": [r["start_turn"], r["end_turn"]],
                "lineup_group_id": r["lineup_group_id"],
                "base": self.base_lineup(shop_id),
                "limited": [],
            }
            for grade, place in (races or {}).get(int(shop_id), []):
                entry["limited"].extend(self.race_offers(shop_id, grade, place))
            out[int(shop_id)] = entry
        return out

    # ------------------------------------------------------------ internals --

    def _draw(self, weights: dict):
        """Weighted choice over a {key: probability} mapping.

        The stored weights are rounded to 5 decimals so they do not sum to
        exactly 1; scaling by the real total keeps the draw unbiased instead of
        quietly over-picking whatever key happens to sort last.
        """
        total = sum(weights.values())
        x = self.rng.random() * total
        acc = 0.0
        for key, w in weights.items():
            acc += w
            if x < acc:
                return int(key)
        return int(next(reversed(weights)))


# ---------------------------------------------------------------- validate --

def validate(sim, dataset_path, trials=None):
    """Simulate against the real dataset and report where they disagree.

    A generator nobody checked against the data it was fitted to is worth
    nothing, so this is part of the tool rather than a one-off script.
    """
    with io.open(dataset_path, encoding="utf-8") as fh:
        data = json.load(fh)
    real_sizes, real_items, real_n = {}, {}, {}
    for c in data["careers"]:
        for k, r in c["resets"].items():
            if not r.get("base_complete"):
                continue
            k = int(k)
            real_sizes.setdefault(k, Counter())[len(r["base"])] += 1
            real_n[k] = real_n.get(k, 0) + 1
            ic = real_items.setdefault(k, Counter())
            for i in set(e["item_id"] for e in r["base"]):
                ic[i] += 1

    print("%-6s %6s   %-24s   %s" % ("reset", "n", "mean size (real/sim)",
                                     "worst item-rate error"))
    worst_overall = 0.0
    for shop_id in sorted(real_sizes):
        n = trials or real_n[shop_id]
        sim_sizes, sim_items = Counter(), Counter()
        for _ in range(n):
            lu = sim.base_lineup(shop_id)
            sim_sizes[len(lu)] += 1
            for i in set(lu):
                sim_items[i] += 1
        rn = real_n[shop_id]
        rmean = sum(k * v for k, v in real_sizes[shop_id].items()) / rn
        smean = sum(k * v for k, v in sim_sizes.items()) / n
        worst, worst_item = 0.0, None
        for i in set(real_items[shop_id]) | set(sim_items):
            d = abs(real_items[shop_id].get(i, 0) / rn - sim_items.get(i, 0) / n)
            if d > worst:
                worst, worst_item = d, i
        worst_overall = max(worst_overall, worst)
        print("%-6d %6d   %8.3f / %-8.3f       %+.3f pp on item %s"
              % (shop_id, n, rmean, smean, worst * 100, worst_item))
    print()
    print("largest per-item rate error across all resets: %.2f pp" % (worst_overall * 100))


def main():
    ap = argparse.ArgumentParser(description="Simulate the Trackblazer shop")
    ap.add_argument("--model", default=MODEL_PATH)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--demo", action="store_true", help="print a sample career")
    ap.add_argument("--validate", metavar="DATASET",
                    help="compare simulated output against a real dataset")
    ap.add_argument("--trials", type=int, default=None)
    args = ap.parse_args()

    sim = ShopSim(args.model, seed=args.seed)
    if args.validate:
        validate(sim, args.validate, args.trials)
        return 0
    if args.demo or True:
        print("Sample career (base stock per reset):")
        for shop_id, e in sorted(sim.career().items()):
            print("  reset %-2d t%2d-%2d  %2d items  %s"
                  % (shop_id, e["turns"][0], e["turns"][1], len(e["base"]), e["base"]))
        print()
        print("Race offers, reset 5:")
        for grade, place in (("G1", 1), ("G1", 3), ("G1", 9), ("Debut", 1)):
            got = [sim.race_offers(5, grade, place) for _ in range(5)]
            print("  %-6s place %-2d -> %s" % (grade, place, got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
