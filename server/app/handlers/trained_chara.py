"""
trained_chara/load: the "legacy Umamusume" roster used for succession/parent
selection on a new career, and (via practice_race) the opponent pool for
practice races.

The roster is the server-owned "house roster": veterans generated
procedurally from real game reference data (data/training_ref/uma_data.json +
master.mdb) rather than copied from characters captured off the real account.
This is what lets the server run offline -- no captured account roster is
needed to populate succession/opponents. See generate_house_roster /
build_generated_veteran.

The roster keeps growing, all offline-safe:
  - finishing a career (single_mode_team.handle_finish) appends a real record
    built from that run's actual final stats/aptitudes/skills
    (build_trained_chara_from_career);
  - the generated veteran pool itself is re-generated on a version bump.

trained_chara_id bands keep these separable so a re-seed never destroys
player progress:
  - < PLAYER_CAREER_ID_BASE            : legacy/captured ids (pre-house; live
                                         only in ROSTER_BACKUP_KEY backups)
  - [PLAYER_CAREER_ID_BASE, INJECT_ID_BASE) : careers finished on this server
                                         -- PRESERVED on re-seed
  - [INJECT_ID_BASE, HOUSE_ID_BASE)   : manual injects (e.g. maxed cheat umas)
                                         -- PRESERVED on re-seed
  - >= HOUSE_ID_BASE                   : generated house veterans -- REPLACED
                                         on a re-seed

Migration: a viewer whose stored ROSTER_VERSION_KEY doesn't match is re-seeded
once; the first pre-house roster is stashed under ROSTER_BACKUP_KEY so the
original captured characters stay recoverable.

Fields with no tracked source yet (race_result_list, win_saddle_id_array,
factor_info_array, and support_card_list for *generated* veterans) ship as
honest empty placeholders -- see build_generated_veteran. The race sim only
consumes stats/aptitudes/skills/running_style, so empty placeholders race and
display fine; populating them needs per-race result / inheritance tracking, a
bigger follow-up.
"""

from __future__ import annotations

import copy
import functools
import json
import random
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .. import master_data
from .. import rating_formula
from .. import state as state_store
from ..fixtures import store as fixtures

ROSTER_KEY = "legacy_roster"

# Bumped whenever the seeding rule changes; a viewer whose stored
# ROSTER_VERSION_KEY doesn't match gets re-seeded once (old roster backed up).
# v1 = top-N curated from the captured account roster; v2 = procedurally
# generated from uma_data.json (needs no captured data -> offline-capable).
ROSTER_VERSION = "house_v6"
ROSTER_VERSION_KEY = "roster_version"
ROSTER_BACKUP_KEY = "legacy_roster_pre_house"

# trained_chara_id bands (int32-safe, clearly separated) so a re-seed can
# regenerate the house pool without touching player-owned entries. The preserve
# band is [PLAYER_CAREER_ID_BASE, HOUSE_ID_BASE); careers and injects get
# disjoint sub-bands within it so career auto-allocation (max+1) never lands on
# an inject or spills into the generated band.
#
# SCALED DOWN 10x 2026-08-18 (was 1_000_000/1_900_000/2_000_000): a real
# capture of an established, heavily-played real account's OWN trained_chara_
# array (captures/20260818_122351/0009_trained_chara_load.json, 194 real
# entries) showed real trained_chara_id values only ever reach the low
# thousands (observed range 176-2417) -- our old bands were ~400-800x larger
# than ANY id the real server has ever been seen to produce. User-suspected,
# then confirmed: this is the leading suspect for veterans not rendering in
# the client's own Veteran Roster screen (server-verified byte-correct
# response, no client-side error logged, so a client-side assumption about
# "plausible" id magnitude -- e.g. a lookup keyed by a smaller int type, or a
# sanity bound on what counts as a real character slot -- is the remaining
# explanation). Still comfortably disjoint from every observed real id (100_000
# vs. the observed max of 2417, ~40x headroom) while no longer being wildly
# outside anything a real client has ever had to handle.
PLAYER_CAREER_ID_BASE = 100_000     # careers finished on this server (PRESERVED, auto-allocated)
INJECT_ID_BASE = 190_000            # manual injects, e.g. maxed cheat umas (PRESERVED, hand-assigned)
HOUSE_ID_BASE = 200_000             # generated house veterans (REPLACED on re-seed)

INT32_MAX = 2_147_483_647
MAXED_INJECT_ID = 199_999           # fixed id for the maxed inject (top of the preserve band)

# BUG FIXED 2026-08-20 (live-reported: client hangs on a repeating crash
# right after the title screen, HUD drawn over a still-100%-loading splash --
# Player.log: OverflowException in System.Decimal.ToInt32, from
# Gallop.TeamStadiumUtil.GetRankScore <- Header.UpdateTeamRank <-
# Header.SetupUI, i.e. the VERY FIRST header the client builds after login).
# Team Stadium's own rank-score math multiplies/sums a member's rank_score,
# so pinning it to literal INT32_MAX (as _max_out_veteran_fields below used
# to, for EVERY roster veteran when a bulk "max my whole roster" cheat ran)
# leaves that math nowhere to go -- any positive factor at all overflows
# Int32, every single boot, permanently. Real observed ceilings top out
# around 300k (rank_score) / 900k (fans) even on a legitimately maxed
# career; this is a "cheat" value, deliberately far above any real one.
#
# rank_score itself is now capped further, at rating_formula.MAX_RANK_SCORE
# (999,999) -- the same overflow class of bug, just tightened once it became
# clear ANY per-uma score anywhere near int32 territory is asking for
# trouble downstream, not just literal INT32_MAX. fans is a different,
# legitimately-large-in-real-accounts quantity (out of scope for that cap)
# and keeps its own, much higher ceiling here.
MAXED_VETERAN_FAN_CEILING = 99_999_999

# The 6 real starter trained_chara (see _load_starter_trained_chara) use
# their OWN real ids 1..6, NOT a private-server band offset at all -- found
# 2026-08-18 via a live, directly-comparable A/B test (same starter cards,
# same fresh-signup flow, captured against the REAL server just to settle
# this): a genuinely fresh real account's own trained_chara_array shows
# trained_chara_id 1, 2, 3, 4, 5, 6 (captures/20260818_174911/
# 0015_trained_chara_load.json) -- not any large offset. This was the actual
# cause of veterans not rendering in the client's Veteran Roster screen: the
# PREVIOUS fix (scaling our whole band scheme down 10x, to ~190_100) was
# still ~190_000 away from what the real client has ever actually been
# handed for this exact case, even though it looked "safely large" by every
# other measure (int32-safe, disjoint from observed real ids up to 2417,
# matching field shapes/types exactly) -- this is a case where NO band
# offset was ever correct here; the fix is to not remap these ids at all.
# Safe from collision: nothing else on this server ever allocates an id below
# PLAYER_CAREER_ID_BASE (100_000), so 1..STARTER_ROSTER_SIZE stays a
# permanently reserved, collision-free sub-range. Explicitly protected in
# _get_or_seed_roster's re-seed preserve filter (below PLAYER_CAREER_ID_BASE,
# which that filter would otherwise treat as "not player-owned" and drop).
STARTER_ROSTER_SIZE = 6

# Size of the generated veteran pool. Must stay well above a full race field
# (practice_race draws player + 17 opponents = an 18-horse gate).
HOUSE_ROSTER_SIZE = 60

# Hard capacity of a viewer's veteran box (user-specified: 260). The real
# server answers 2051/2511 when the box is full; we enforce at career finish.
VETERAN_CAPACITY = 260

_STAT_KEYS = ("speed", "stamina", "power", "wiz", "guts")

# --- procedural-generation reference data ------------------------------------
_REF_DIR = Path(__file__).resolve().parents[2] / "data" / "training_ref"
with open(_REF_DIR / "uma_data.json", encoding="utf-8") as _uma_f:
    _UMA_DATA = [u for u in json.load(_uma_f) if u.get("cardId")]

_APT_LETTER_TO_VALUE = {"S": 8, "A": 7, "B": 6, "C": 5, "D": 4, "E": 3, "F": 2, "G": 1}

# Stat roll bands for a generated veteran, taken from the real roster's own
# spread (speed avg ~1115/max 1400, wiz to ~1880, ...). These are strong,
# succession-worthy legends -- not fresh trainees.
_GEN_STAT_BANDS = {
    "speed": (1000, 1350),
    "stamina": (400, 950),
    "power": (700, 1150),
    "wiz": (650, 1150),
    "guts": (350, 850),
}
_GEN_STAT_CAP = 1500
_GEN_TALENT_KEY = {
    "speed": "talentSpeed", "stamina": "talentStamina", "power": "talentPower",
    "guts": "talentGuts", "wiz": "talentWisdom",
}
# A real, internally-consistent (scenario, route, arrive_route_race) triple from
# a real scenario-2 roster entry, so every generated veteran carries a
# known-valid combination rather than a guessed one.
_GEN_SCENARIO_ID = 2
_GEN_ROUTE_ID = 20
_GEN_ARRIVE_ROUTE_RACE_ID = 228


def seed_roster_from_fixture() -> list[dict]:
    """The one real captured trained_chara_array (~236 real entries from the
    actual account) used to seed a viewer's roster on first access."""
    pair = fixtures.first("trained_chara/load")
    return pair.response_copy()["data"]["trained_chara_array"] if pair else []


# Sparks (succession factors), from master.mdb succession_factor.factor_type:
#   1 = BLUE  (stat)      factor_id = stat*100+star (101 Speed*1 .. 503 Wit*3)
#   2 = PINK  (aptitude)  factor_id = aptitude_group*100+star (1101 Turf*1 ..)
#   3 = GREEN (unique)    factor_id = card_id*100+star; factor_group_id = card_id.
#                         Only 3*+ cards have one.
# Every uma/ancestor gets at least one blue + one pink, plus its own green if it
# is a 3*+ card. All generated from master data, not copied from any account.
_ANCESTRY_POSITIONS = (10, 20, 11, 12, 21, 22)  # 2 parents + their 2 parents each (grandparents)
_blue_factors_cache = None
_pink_factors_cache = None
_green_cards_cache = None


def _blue_factors():
    global _blue_factors_cache
    if _blue_factors_cache is None:
        _blue_factors_cache = [r["factor_id"] for r in master_data.query("SELECT factor_id FROM succession_factor WHERE factor_type=1")]
    return _blue_factors_cache


def _pink_factors():
    global _pink_factors_cache
    if _pink_factors_cache is None:
        _pink_factors_cache = [r["factor_id"] for r in master_data.query("SELECT factor_id FROM succession_factor WHERE factor_type=2")]
    return _pink_factors_cache


def _green_cards():
    """card_ids that have a green (unique) spark -- i.e. 3*+ cards with a unique
    skill (factor_group_id == card_id in the type-3 factors)."""
    global _green_cards_cache
    if _green_cards_cache is None:
        _green_cards_cache = {r["factor_group_id"] for r in master_data.query(
            "SELECT DISTINCT factor_group_id FROM succession_factor WHERE factor_type=3")}
    return _green_cards_cache


def _weighted_star(r):
    x = r.random()
    return 1 if x < 0.70 else (2 if x < 0.93 else 3)


def _green_factor(card_id, star):
    return card_id * 100 + star if card_id in _green_cards() else None


def _generate_sparks(card_id, rarity, r):
    """One blue (stat) + one pink (aptitude) spark, plus the card's own green
    (unique) spark if it is 3*+ and has one."""
    sparks = [
        {"factor_id": r.choice(_blue_factors()), "level": 0},
        {"factor_id": r.choice(_pink_factors()), "level": 0},
    ]
    if rarity >= 3:
        g = _green_factor(card_id, _weighted_star(r))
        if g is not None:
            sparks.append({"factor_id": g, "level": 0})
    return sparks


def _ancestry_entry_from(uma: dict, position_id: int) -> dict:
    """One succession_chara_array entry describing a REAL roster uma."""
    return {
        "position_id": position_id,
        "card_id": uma.get("card_id"),
        "rank": uma.get("rank", 0),
        "rarity": uma.get("rarity", 3),
        "talent_level": uma.get("talent_level", 1),
        "factor_info_array": copy.deepcopy(uma.get("factor_info_array") or []),
        "win_saddle_id_array": copy.deepcopy(uma.get("win_saddle_id_array") or []),
        "owner_viewer_id": uma.get("owner_viewer_id", 0),
    }


def _real_ancestry(roster: list[dict], parent_1, parent_2) -> list | None:
    """The finished uma's REAL lineage: the two parents the player picked at
    career start, plus THEIR parents as the grandparents.

    Live-reported: "after finishing a career, the parents of the finished
    umamusume are randomly generated, they should actually be the 2 you
    selected at the start of the career, thats how the loop continues". The
    ids were being stored correctly in succession_trained_chara_id_1/2 -- it is
    the DISPLAYED lineage (succession_chara_array) that was invented, so an
    inheritance chain never actually chained: every finished career showed
    strangers as its parents and their sparks were unrelated to the ones the
    player had bred for.

    Grandparents come from each parent's own position-10/20 entries. Returns
    None when neither parent resolves (a first-generation career with no picks),
    so the caller falls back to generating a plausible ancestry -- the client
    NullRefs on an empty/short array."""
    by_id = {c.get("trained_chara_id"): c for c in roster}
    parents = [(10, by_id.get(parent_1)), (20, by_id.get(parent_2))]
    if not any(p for _pos, p in parents):
        return None
    out = []
    for pos, parent in parents:
        if not parent:
            continue
        out.append(_ancestry_entry_from(parent, pos))
        # this parent's own parents become the grandparents at pos+1 / pos+2
        gp = {e.get("position_id"): e for e in parent.get("succession_chara_array") or []}
        for offset, src_pos in ((1, 10), (2, 20)):
            g = gp.get(src_pos)
            if g:
                entry = copy.deepcopy(g)
                entry["position_id"] = pos + offset
                out.append(entry)
    return out or None


def _generate_ancestry(card_id, rarity, rng=None):
    """DYNAMICALLY generate a valid 6-entry succession_chara_array (2 parents +
    4 grandparents) plus the uma's own spark list, from real master data
    (character cards + succession factors) -- generated per veteran, NOT copied
    from any account. Each entry carries proper blue/pink/green sparks. Required
    or the client's SingleModeUtils.CalcRelation NullRefs on the career
    succession-pick screen (it reads each candidate's ancestry)."""
    r = rng if rng is not None else random
    green_cards = list(_green_cards())  # 3*+ cards with a unique, valid as ancestors

    ancestry = []
    for pos in _ANCESTRY_POSITIONS:
        acard = r.choice(green_cards)
        ararity = r.choice([3, 4])
        ancestry.append({
            "position_id": pos,
            "card_id": acard,
            "rank": r.randint(8, 20),
            "rarity": ararity,
            "talent_level": r.randint(1, 5),
            "factor_info_array": _generate_sparks(acard, ararity, r),
            "win_saddle_id_array": [],
            "owner_viewer_id": 0,
        })
    own_factors = _generate_sparks(card_id, rarity, r)
    return ancestry, own_factors


_support_cards_cache = None


def _support_cards():
    """(all support_card_ids, rarity -> max total_exp, card_id -> rarity), cached.
    Source of the random deck a generated veteran is given."""
    global _support_cards_cache
    if _support_cards_cache is None:
        rows = master_data.query("SELECT id, rarity FROM support_card_data")
        ids = [r["id"] for r in rows]
        rar = {r["id"]: r["rarity"] for r in rows}
        maxexp = {r["rarity"]: r["mx"] for r in master_data.query(
            "SELECT rarity, MAX(total_exp) mx FROM support_card_level GROUP BY rarity")}
        _support_cards_cache = (ids, maxexp, rar)
    return _support_cards_cache


def _random_support_cards(r, count=6):
    """A random 6-card support deck ([{position, support_card_id, exp,
    limit_break_count}]). exp is maxed for the card's rarity so the deck reads as
    a fully-trained one; limit breaks are rolled 0-4. Generated veterans had no
    deck data before -- careers still supply their real deck."""
    ids, maxexp, rar = _support_cards()
    if not ids:
        return []
    picks = r.sample(ids, min(count, len(ids)))
    return [
        {
            "position": pos,
            "support_card_id": sid,
            "exp": maxexp.get(rar.get(sid), 0) or 0,
            "limit_break_count": r.randint(0, 4),
        }
        for pos, sid in enumerate(picks, start=1)
    ]


# Junior-class Make Debut race (grade 900). program 1067 is the make-debut race
# observed in a real captured career (June, 2nd half -> turn 12).
_DEBUT_PROGRAM_ID = 1067
_DEBUT_TURN = 12


def _debut_race_result(r, running_style):
    """One historical race entry: the Junior Make Debut, won (result_rank 1).
    Just enough for a roster entry to have race history, per design (no full
    career log is tracked for generated veterans)."""
    return [{
        "turn": _DEBUT_TURN,
        "program_id": _DEBUT_PROGRAM_ID,
        "weather": 1,           # sunny
        "ground_condition": 1,  # firm
        "running_style": running_style or 2,
        "popularity": r.randint(1, 5),
        "result_rank": 1,       # win
        "result_time": r.randint(1_150_000, 1_200_000),
        "prize_money": 0,
    }]


def _career_race_results(career_state: dict, chara_info: dict) -> list:
    """The finished uma's REAL race record, built from the career's own
    race_history.

    It used to ship `_debut_race_result(...)` -- one fabricated 'Junior Make
    Debut, won' entry -- for every finished career, no matter what the player
    actually ran (live-reported: the races the uma ran in were not reported
    correctly at the end of a run). race_history is the trusted log; the three
    fields it doesn't carry are derived rather than invented at random:
      popularity   the horse's frame order, which is what we simulated with
      result_time  deterministic per (program, turn) so a career's record does
                   not reshuffle every time the roster is re-read
      prize_money  0 -- we model no prize table
    """
    history = (career_state.get("data") or {}).get("race_history") or []
    out = []
    for h in history:
        pid = h.get("program_id")
        turn = h.get("turn") or 0
        seed = ((int(pid or 0) * 2654435761) ^ (int(turn) * 40503)) & 0xffffffff
        out.append({
            "turn": turn,
            "program_id": pid,
            "weather": h.get("weather", 1),
            "ground_condition": h.get("ground_condition", 1),
            "running_style": (h.get("running_style")
                              or chara_info.get("race_running_style") or 2),
            "popularity": h.get("frame_order") or 1,
            "result_rank": h.get("result_rank") or 1,
            "result_time": 1_150_000 + seed % 50_000,
            "prize_money": 0,
        })
    return out


def _apt_value(uma: dict, key: str) -> int:
    """uma_data.json aptitude letter (S..G) -> the numeric 8..1 the wire
    proper_* fields use (8 = S = best)."""
    return _APT_LETTER_TO_VALUE.get(str(uma.get(key, "G")).upper(), 1)


def _gen_skill_array(uma: dict) -> list[dict]:
    """uma_data.json stores skillIds as a comma-separated string; the wire
    skill_array is [{skill_id, level}]. A generated veteran gets its own
    character's full listed kit at level 1."""
    raw = str(uma.get("skillIds") or "")
    ids = [int(tok) for tok in raw.split(",") if tok.strip().isdigit()]
    return [{"skill_id": sid, "level": 1} for sid in ids]


_card_dress_cache = None
_race_dress_by_card_cache = None


def _card_dress_data():
    """(valid_dress_ids, card_id -> race_dress_id) FALLBACK ONLY, for card_ids
    with no card_rarity_data row (see dress_for_card). The card->dress map here
    is the real (card_id, race_cloth_id) pairs from one captured roster
    snapshot -- necessarily incomplete for any card released after that capture,
    which is what made this the sole source originally: any card missing from
    it fell through to a same-character heuristic that could land on a
    DIFFERENT valid costume for that character (e.g. Mejiro McQueen card 101303
    resolving to costume 101302's dress instead of its own 101330)."""
    global _card_dress_cache
    if _card_dress_cache is None:
        valid = {r["id"] for r in master_data.query("SELECT id FROM dress_data")}
        card_to_dress = {}
        pair = fixtures.first("trained_chara/load")
        if pair:
            for e in pair.response["data"]["trained_chara_array"]:
                c, cloth = e.get("card_id"), e.get("race_cloth_id")
                if c and cloth in valid:
                    card_to_dress[c] = cloth
        _card_dress_cache = (valid, card_to_dress)
    return _card_dress_cache


def _race_dress_by_card() -> dict:
    """card_id -> race_dress_id, straight from master.mdb's card_rarity_data --
    the AUTHORITATIVE per-card mapping (this is literally what card_rarity_data
    exists for), so it's exact for every card and needs no per-account capture.
    A card_id can carry multiple rarity rows whose race_dress_id sometimes
    differs (below rarity 3 many cards wear a shared generic placeholder dress,
    id 101, before their real costume unlocks) -- MAX(rarity) always resolves to
    the character's real, final costume (verified: 0/93 cards still show the
    placeholder at their max rarity), and every career trainee in this codebase
    is built at 5-star, so that's the one we want anyway."""
    global _race_dress_by_card_cache
    if _race_dress_by_card_cache is None:
        rows = master_data.query(
            "SELECT card_id, race_dress_id FROM card_rarity_data crd "
            "WHERE rarity = (SELECT MAX(rarity) FROM card_rarity_data "
            "                WHERE card_id = crd.card_id)")
        _race_dress_by_card_cache = {r["card_id"]: r["race_dress_id"] for r in rows}
    return _race_dress_by_card_cache


def dress_for_card(card_id: int, chara_id: int = 0) -> int:
    """Valid dress_data id for a card's race outfit. card_rarity_data.race_dress_id
    is the authoritative source (see _race_dress_by_card); the captured-roster /
    same-character fallbacks below only run for the handful of card_ids with no
    card_rarity_data row at all. An invalid dress here makes the client NullRef
    on the result cutin (the 'racing does nothing' crash), so the result is
    always a real dress_data id."""
    authoritative = _race_dress_by_card().get(card_id)
    if authoritative is not None:
        return authoritative

    valid, card_to_dress = _card_dress_data()
    if card_id in card_to_dress:
        return card_to_dress[card_id]
    if card_id in valid:  # base card is its own dress
        return card_id
    if chara_id:
        row = master_data.query_one(
            "SELECT id FROM dress_data WHERE chara_id=? AND use_race=1 AND general_purpose=0 "
            "ORDER BY id LIMIT 1", (chara_id,))
        if row:
            return row["id"]
        base = chara_id * 100 + 1
        if base in valid:
            return base
    return card_id


def _gen_running_style(uma: dict) -> int:
    """Running style (1 Nige / 2 Senkou / 3 Sashi / 4 Oikomi) matching the
    character's strongest style aptitude."""
    apts = {
        1: _apt_value(uma, "aptitudeRunner"),
        2: _apt_value(uma, "aptitudeLeader"),
        3: _apt_value(uma, "aptitudeBetweener"),
        4: _apt_value(uma, "aptitudeChaser"),
    }
    return max(apts, key=apts.get)


def build_generated_veteran(viewer_id, uma: dict, trained_chara_id: int, rng: random.Random) -> dict:
    """One procedurally-generated veteran for base character `uma` (a
    uma_data.json entry). Same wire schema as build_trained_chara_from_career;
    only the source differs -- rolled stats plus the character's own
    aptitudes/skills, not an actual played career."""
    stats = {}
    for stat, (lo, hi) in _GEN_STAT_BANDS.items():
        talent = uma.get(_GEN_TALENT_KEY[stat], 0) or 0
        stats[stat] = min(_GEN_STAT_CAP, rng.randint(lo, hi) + int(talent * rng.uniform(2.0, 6.0)))

    rank_score = rating_formula.get_rating([stats[k] for k in _STAT_KEYS])
    fans = rng.randint(80_000, 900_000)
    card_id = uma["cardId"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ancestry, own_factors = _generate_ancestry(card_id, 3, rng)

    return {
        # patch.py rewrites the "<redacted>" placeholder to the live numeric
        # viewer_id at response time. A real string here (e.g. "802445340143")
        # is NOT rewritten -> client gets a string where it wants an int ->
        # "Could not receive parameters from server". Must stay the placeholder.
        "viewer_id": "<redacted>",
        "trained_chara_id": trained_chara_id,
        "owner_viewer_id": 0,
        "owner_trained_chara_id": 0,
        "single_mode_chara_id": trained_chara_id,
        "chara_seed": rng.getrandbits(31),
        "card_id": card_id,
        "succession_trained_chara_id_1": 0,
        "succession_trained_chara_id_2": 0,
        "use_type": 0,
        "speed": stats["speed"],
        "stamina": stats["stamina"],
        "power": stats["power"],
        "wiz": stats["wiz"],
        "guts": stats["guts"],
        "fans": fans,
        "rank_score": rank_score,
        "rank": _rank_for_score(rank_score),
        "scenario_id": _GEN_SCENARIO_ID,
        "route_id": _GEN_ROUTE_ID,
        "arrive_route_race_id": _GEN_ARRIVE_ROUTE_RACE_ID,
        "proper_ground_turf": _apt_value(uma, "aptitudeTurf"),
        "proper_ground_dirt": _apt_value(uma, "aptitudeDirt"),
        "proper_running_style_nige": _apt_value(uma, "aptitudeRunner"),
        "proper_running_style_senko": _apt_value(uma, "aptitudeLeader"),
        "proper_running_style_sashi": _apt_value(uma, "aptitudeBetweener"),
        "proper_running_style_oikomi": _apt_value(uma, "aptitudeChaser"),
        "proper_distance_short": _apt_value(uma, "aptitudeShort"),
        "proper_distance_mile": _apt_value(uma, "aptitudeMile"),
        "proper_distance_middle": _apt_value(uma, "aptitudeMiddle"),
        "proper_distance_long": _apt_value(uma, "aptitudeLong"),
        "succession_num": 0,
        "rarity": 3,
        "is_saved": 1,
        "is_locked": 0,
        "talent_level": rng.choice([3, 4, 5]),
        "race_cloth_id": dress_for_card(card_id, uma.get("charaId", 0)),
        "chara_grade": _chara_grade_for(fans),
        "running_style": _gen_running_style(uma),
        "nickname_id": 0,
        "wins": 0,
        "register_time": now,
        "create_time": now,
        "skill_array": _gen_skill_array(uma),
        "support_card_list": _random_support_cards(rng),
        "race_result_list": _debut_race_result(rng, _gen_running_style(uma)),
        "win_saddle_id_array": [],
        "nickname_id_array": [],
        "factor_info_array": own_factors,
        "factor_extend_array": [],
        "succession_chara_array": ancestry,
    }


def generate_house_roster(viewer_id, count: int = HOUSE_ROSTER_SIZE) -> list[dict]:
    """The generated house veteran pool: `count` veterans built from the base
    characters in uma_data.json, ids assigned in the HOUSE_ID_BASE band.
    Deterministic per viewer (seeded by viewer_id) so a viewer's roster is
    stable across restarts and same-version re-seeds."""
    rng = random.Random(zlib.crc32(str(viewer_id).encode()))
    umas = _UMA_DATA
    return [
        build_generated_veteran(viewer_id, umas[i % len(umas)], HOUSE_ID_BASE + i + 1, rng)
        for i in range(count)
    ]


_ALL_APTITUDE_FIELDS = (
    "proper_ground_turf", "proper_ground_dirt",
    "proper_distance_short", "proper_distance_mile",
    "proper_distance_middle", "proper_distance_long",
    "proper_running_style_nige", "proper_running_style_senko",
    "proper_running_style_sashi", "proper_running_style_oikomi",
)


def _all_skill_ids() -> list[int]:
    return [int(r["id"]) for r in master_data.query("SELECT id FROM skill_data ORDER BY id")]


def _max_rank_id() -> int:
    row = master_data.query_one("SELECT MAX(id) AS m FROM single_mode_rank")
    return row["m"] if row and row["m"] is not None else 1


def _max_out_veteran_fields(rec: dict) -> None:
    """Apply this codebase's established "fully maxed veteran" convention to
    an EXISTING trained_chara record IN PLACE: 9999 in every stat, S (=8) in
    every aptitude, every non-negative skill in the game at its real max
    level, and rank_score/fans/rank/chara_grade/rarity/talent_level pinned to
    the same ceilings build_maxed_veteran uses for its single synthetic
    "cheat" inject. Factored out of build_maxed_veteran (which still calls
    this) so a bulk "max my whole roster" cheat (admin.py's max-roster) uses
    the exact same definition of "maxed" as that one hand-picked veteran,
    rather than a second, possibly-drifting convention. Unlike
    build_maxed_veteran this does NOT touch card_id/race_cloth_id/running_
    style/ancestry/etc -- it only raises the "how good is this uma" fields on
    whatever record is passed in, real roster veteran or synthetic alike."""
    for stat in _STAT_KEYS:
        rec[stat] = 9999
    for field in _ALL_APTITUDE_FIELDS:
        rec[field] = 8  # S
    rec["rank_score"] = rating_formula.MAX_RANK_SCORE
    rec["fans"] = MAXED_VETERAN_FAN_CEILING
    rec["rank"] = _max_rank_id()
    rec["chara_grade"] = _chara_grade_for(MAXED_VETERAN_FAN_CEILING)
    rec["rarity"] = 5
    rec["talent_level"] = 5
    # Every skill EXCEPT negative/debuff ones (grade_value < 0 -- the "×"
    # skills, Packphobia, Defeatist, etc.), which would only hurt her --
    # UNION'd with whatever this record already had (so a real veteran's own
    # earned skill_ids are never dropped, only topped up/extended).
    # Unique-tier skills (rarity 3/4/5 -- character uniques + evolved uniques,
    # which level 1->6 in game) get max level 6; white/gold (rarity 1/2) stay 1.
    skill_meta = {
        r["id"]: (r["rarity"], r["grade_value"] or 0)
        for r in master_data.query("SELECT id, rarity, grade_value FROM skill_data")
    }
    existing_ids = {s.get("skill_id") for s in (rec.get("skill_array") or [])
                    if isinstance(s, dict)}
    all_ids = sorted(existing_ids | set(_all_skill_ids()))
    rec["skill_array"] = [
        {"skill_id": sid, "level": 6 if skill_meta.get(sid, (1, 0))[0] in (3, 4, 5) else 1}
        for sid in all_ids
        if skill_meta.get(sid, (1, 0))[1] >= 0  # drop negative/debuff skills
    ]


def build_maxed_veteran(viewer_id, card_id: int, trained_chara_id: int = MAXED_INJECT_ID) -> dict:
    """A fully-maxed 'cheat' veteran: 9999 in every stat, S (=8) in every
    aptitude, every skill in the game, and rank_score/fans pinned to their
    respective ceilings (rating_formula.MAX_RANK_SCORE / MAXED_VETERAN_FAN_
    CEILING). Built off build_generated_veteran so the wire schema stays
    identical to a real record, then overridden (see _max_out_veteran_fields).
    Assigned an id in the inject sub-band so a house re-seed preserves it."""
    uma = next((u for u in _UMA_DATA if u.get("cardId") == card_id), None)
    rng = random.Random(trained_chara_id)
    # build_generated_veteran needs a base uma for schema; if this card isn't in
    # uma_data.json, borrow any entry's shape and swap the card_id/cloth.
    rec = build_generated_veteran(viewer_id, uma or _UMA_DATA[0], trained_chara_id, rng)
    rec["card_id"] = card_id
    rec["race_cloth_id"] = dress_for_card(card_id, uma.get("charaId", 0) if uma else 0)
    rec["running_style"] = 1  # every style is S; Maruzensky runs front (Nige)
    _max_out_veteran_fields(rec)
    return rec


_starter_trained_chara_cache: list | None = None


def _load_starter_trained_chara(viewer_id) -> list[dict]:
    """The REAL 6 starter trained_chara a fresh account gets (5 basic, one
    per starter card, + 1 elevated "tutorial run" veteran on card 100901) --
    captured 2026-08-18 against the real Cygames server
    (captures/20260818_081331/0009_load_index.json, trained_chara array).
    Every field (skills, aptitudes, factor_info_array, succession_chara_
    array, race_result_list) is real, not generated.

    trained_chara_id is now used EXACTLY as captured -- 1..6, no remapping
    at all. This was wrong for a long time (previously offset into a
    private-server band, most recently ~190_100+): a second, independent
    live capture of a DIFFERENT fresh real account (captures/20260818_
    174911/0015_trained_chara_load.json, same starter cards, same flow)
    confirmed the real server itself hands out trained_chara_id 1..6 for
    these, not any offset scheme -- see STARTER_ROSTER_SIZE's comment for
    why this was the actual cause of veterans not rendering client-side."""
    global _starter_trained_chara_cache
    if _starter_trained_chara_cache is None:
        path = Path(__file__).resolve().parents[2] / "data" / "seeds" / "starter_trained_chara.json"
        with open(path, "r", encoding="utf-8") as f:
            _starter_trained_chara_cache = json.load(f)
    template = copy.deepcopy(_starter_trained_chara_cache)

    # owner_viewer_id is 0 in EVERY real capture (this template's own, plus
    # two independent fresh-account captures used to verify this fix) -- it
    # means "not borrowed from someone else", not "who owns this". Used to
    # get overwritten with the account's own viewer_id here, which produced
    # a nonsensical self-referential "borrowed from myself" value on every
    # single starter -- found 2026-08-18 after trained_chara_id (fixed
    # separately, see STARTER_ROSTER_SIZE) turned out NOT to be the cause:
    # even a byte-verified, 100%-real captured response replayed verbatim
    # through this server's own pipeline still rendered as empty, which
    # proved the bug had to be in something this project's own field-diffing
    # kept blindly excluding as an "expected to differ per account" identity
    # field -- owner_viewer_id was wrongly bucketed with viewer_id (which
    # SHOULD differ per account) instead of being recognized as a constant
    # semantic flag that should stay 0 regardless of who owns the entry.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for e in template:
        e["viewer_id"] = viewer_id
        # Must be a LITERAL 0, not left alone as this project's earlier fix
        # assumed -- found 2026-08-19 (a fresh account made after that fix
        # STILL had no veterans): the seed JSON itself stores owner_viewer_id
        # as the SAME "<redacted>" placeholder viewer_id uses (the capture-
        # sanitizer redacted every *_viewer_id-shaped field indiscriminately,
        # not just ones that actually held the account's own id), and
        # patch.py's scrub_redacted rewrites ANY "<redacted>" value under
        # key "owner_viewer_id" to the real requesting viewer_id, same as it
        # does for "viewer_id" (see _placeholder_for -- both keys share one
        # branch there). So merely NOT touching this field here still let
        # patch.py stamp it with the account's own id at response time,
        # reproducing the exact self-referential "borrowed from myself" bug
        # the previous fix believed it had already closed. Setting it
        # explicitly bypasses that: a real 0 is never "<redacted>", so
        # scrub_redacted has nothing to rewrite.
        e["owner_viewer_id"] = 0
        e["register_time"] = now
        e["create_time"] = now
    return template


def _get_or_seed_roster(viewer_id) -> list[dict]:
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY)

    # Current-version roster already seeded (possibly grown by finishes/injects)
    # -> use as-is. Appends never change the version, so they accumulate on top
    # of the generated base.
    if roster is not None and full_state.get(ROSTER_VERSION_KEY) == ROSTER_VERSION:
        return roster

    # A TRULY new account (never seeded at all): the real 6 starter
    # trained_chara ONLY -- no generated house-roster padding. User-specified
    # 2026-08-18: "everything starts at 0" except the real starter set: this
    # is that set, not a convenience pool invented for offline play (that's
    # what generate_house_roster is for on RE-seeds below, a different,
    # already-established account behavior this deliberately leaves alone).
    if roster is None:
        full_state[ROSTER_KEY] = _load_starter_trained_chara(viewer_id)
        full_state[ROSTER_VERSION_KEY] = ROSTER_VERSION
        state_store.save_state(viewer_id, full_state)
        return full_state[ROSTER_KEY]

    # Back up the first pre-house roster once (the captured account characters)
    # so the original stays recoverable.
    if ROSTER_BACKUP_KEY not in full_state:
        full_state[ROSTER_BACKUP_KEY] = roster

    # (Re)seed: regenerate the house veteran pool, but keep everything already
    # in the preserve band (careers finished on this server + manual injects)
    # so a version bump never destroys player-owned entries. The 6 real
    # starters (ids 1..STARTER_ROSTER_SIZE, see that constant's comment) sit
    # BELOW PLAYER_CAREER_ID_BASE and must be preserved explicitly too, or a
    # re-seed would silently drop them (that band is otherwise "legacy/
    # captured ids", not something this filter normally protects).
    preserved = [
        e for e in roster
        if PLAYER_CAREER_ID_BASE <= e.get("trained_chara_id", 0) < HOUSE_ID_BASE
        or 1 <= e.get("trained_chara_id", 0) <= STARTER_ROSTER_SIZE
    ]
    full_state[ROSTER_KEY] = generate_house_roster(viewer_id) + preserved
    full_state[ROSTER_VERSION_KEY] = ROSTER_VERSION
    state_store.save_state(viewer_id, full_state)
    return full_state[ROSTER_KEY]


def handle_trained_chara_load(payload: dict) -> dict:
    """trained_chara/load -- the Veteran Roster screen (ENDPOINT_KEYS.md).

    REBUILT 2026-08-18 from a fresh real capture (captures/20260818_122351/
    0009_trained_chara_load.json, real server, real account) after the
    roster showing empty in-game ("Registered 0/260", no portraits) despite
    this endpoint correctly returning all 6 real starters. Root cause: the
    envelope was built from an OLD fixture template with EXTRA fields the
    current client no longer sends -- real data.keys() is exactly
    {trained_chara_array, trained_chara_favorite_array, room_match_entry_
    chara_id_array}, but the old fixture also carried a team_data_array
    this endpoint doesn't have in the current client build (that field
    lives in load/index instead, which DOES still have it -- confirmed
    separately). MessagePack C# formatters are array-positional (field
    COUNT/order, not names -- see missions.py's docstring for the same
    class of bug), so shipping a field the client's current formatter
    doesn't declare wasn't harmless extra data, it was a wire-format
    mismatch that could fail the whole response's deserialization -- which
    would explain an EMPTY-looking roster despite trained_chara_array
    itself being populated correctly.

    Now built directly from the 3 real fields instead of a stale template:
    trained_chara_array is the live roster (unchanged); trained_chara_
    favorite_array/room_match_entry_chara_id_array serve genuinely empty
    (no favorite-tracking state exists yet on this server, and room match
    is a multiplayer feature with no other real players to fabricate --
    same reasoning as friend/index) rather than remapping a real
    established account's 20 favorites onto ids that mean nothing here."""
    viewer_id = payload["viewer_id"]
    roster = _get_or_seed_roster(viewer_id)
    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {
            "trained_chara_array": copy.deepcopy(roster),
            "trained_chara_favorite_array": [],
            "room_match_entry_chara_id_array": [],
        },
    }


def handle_change_nickname(payload: dict) -> dict:
    """trained_chara/change_nickname -- set one veteran's displayed epithet.
    NEVER had a handler before this session's real capture (captures/
    20260818_122351/0014_trained_chara_change_nickname.json, real server):
    request {trained_chara_id, nickname_id}; response data.trained_chara_
    info is the FULL updated TrainedChara object (same shape as roster
    entries -- not just the two changed fields)."""
    viewer_id = payload["viewer_id"]
    tcid = payload.get("trained_chara_id")
    nickname_id = payload.get("nickname_id")
    if not isinstance(tcid, int) or not isinstance(nickname_id, int):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    if nickname_id and not master_data.query_one(
            "SELECT id FROM nickname WHERE id=?", (nickname_id,)):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    full_state = state_store.get_state(viewer_id) or {}
    roster = _get_or_seed_roster(viewer_id)
    entry = next((c for c in roster if c.get("trained_chara_id") == tcid), None)
    if entry is None:
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    entry["nickname_id"] = nickname_id
    full_state[ROSTER_KEY] = roster
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"trained_chara_info": copy.deepcopy(entry)}}


def _rank_for_score(rank_score: int) -> int:
    """Rank id for a career score, CLAMPED at both ends of single_mode_rank.

    The table's top row ends at 99,999. A score above that matched no row and
    fell through to `1` -- the LOWEST rank -- so the better the run, the worse
    the rank it reported: a maxed trainee came back rank 1 (G) with a 119,865
    score (live-reported). Above the table means the best rank, not the worst.
    (build_maxed_veteran already sidestepped this by calling _max_rank_id()
    directly, which was the clue that the lookup couldn't express a big score.)"""
    row = master_data.query_one(
        "SELECT id FROM single_mode_rank WHERE ? BETWEEN min_value AND max_value",
        (rank_score,),
    )
    if row:
        return row["id"]
    bounds = master_data.query_one(
        "SELECT MIN(min_value) AS lo, MAX(max_value) AS hi FROM single_mode_rank")
    if bounds and bounds["hi"] is not None and rank_score > bounds["hi"]:
        return _max_rank_id()
    return 1


def _chara_grade_for(fans: int) -> int:
    """No win/race-count tracking yet (see module docstring), so this picks
    the best grade reachable on fan count alone, i.e. as if win_num/run_num
    requirements are always met -- an optimistic placeholder, not a real
    win-gated grade."""
    row = master_data.query_one(
        "SELECT id FROM single_mode_chara_grade WHERE need_fan_count <= ? "
        "ORDER BY need_fan_count DESC, id DESC LIMIT 1",
        (fans,),
    )
    return row["id"] if row else 1


def _succession_num_for(roster: list[dict], parent_id_1, parent_id_2) -> int:
    by_id = {c.get("trained_chara_id"): c for c in roster}
    parents = [by_id.get(parent_id_1), by_id.get(parent_id_2)]
    parent_nums = [p.get("succession_num", 0) for p in parents if p]
    return (max(parent_nums) + 1) if parent_nums else 1


def build_trained_chara_from_career(viewer_id, career_state: dict, start_chara: dict,
                                    roster: list[dict], factor_info_array=None,
                                    full_state: dict | None = None) -> dict:
    chara_info = career_state["data"]["chara_info"]
    start_chara = start_chara or {}

    # THE SCENARIO SNAPSHOT -- whatever this run's scenario wants frozen into
    # the finished veteran record, taken HERE (finish time, before the NEXT
    # career's reset would clear it). Grand Live contributes songs learned +
    # each concert's result_state; base.Scenario contributes nothing, so a
    # URA record is unchanged. full_state is optional (existing callers that
    # don't pass it just get neither field, same as before). missions.py's
    # SongCountThreshold/SpecificSongObtained/ConcertSuccessCount
    # condition_types read these back.
    # Taken WHOLE, not key by key. This used to copy out exactly two keys
    # (grand_live_songs / grand_live_lives_done), which silently discarded every
    # other scenario's snapshot: Unity Cup and Trackblazer both define one, and
    # their epithets (152-155, 162-163, 180-184) could never be earned because
    # the facts never survived graduation. Forwarding the dict means a new
    # scenario's snapshot works the moment it is written, with no edit here.
    scenario_snapshot: dict = {}
    if full_state is not None:
        from .. import scenarios
        scenario_snapshot = dict(
            scenarios.for_chara(chara_info).snapshot(full_state) or {})
    grand_live_songs = scenario_snapshot.pop("grand_live_songs", None) or []
    grand_live_lives_done = scenario_snapshot.pop("grand_live_lives_done", None) or []

    # This career's one claw-machine session (see single_mode_team.py's
    # CRANE_SESSION_KEY -- "crane_session_plushies" string literal here, not
    # an import, same circular-import dodge _CAREER_STATE_KEYS uses for
    # "idle_single_mode_run"). missions.py's ClawMachinePlushieSession
    # (100023).
    crane_session_plushies = (full_state or {}).get("crane_session_plushies") or 0

    stats = {k: chara_info.get(k, 0) for k in _STAT_KEYS}
    # Skills count toward the score too -- see rating_formula.get_skill_score.
    rank_score = rating_formula.get_career_score(chara_info)
    fans = chara_info.get("fans", 0)

    support_card_list = [
        {
            "position": c.get("position"),
            "support_card_id": c.get("support_card_id"),
            "exp": c.get("exp", 0),
            "limit_break_count": c.get("limit_break_count", 0),
        }
        for c in chara_info.get("support_card_array", [])
    ]

    parent_1 = start_chara.get("succession_trained_chara_id_1", 0) or 0
    parent_2 = start_chara.get("succession_trained_chara_id_2", 0) or 0

    # The uma's ACTUAL race record, plus the two numbers derived from it that
    # were previously hardcoded: `wins` shipped as 0 for every career, and the
    # G1 wins that earn a win saddle were never recorded at all.
    race_results = _career_race_results(career_state, chara_info)
    win_count = sum(1 for r in race_results if r.get("result_rank") == 1)
    win_saddles = []
    for r in race_results:
        if r.get("result_rank") != 1:
            continue
        row = master_data.query_one(
            "SELECT ri.id AS inst, rc.grade AS grade FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id = p.race_instance_id "
            "JOIN race rc ON rc.id = ri.race_id WHERE p.id=?", (r.get("program_id"),))
        if row and row["grade"] == 100 and row["inst"] not in win_saddles:
            win_saddles.append(row["inst"])   # G1 wins only

    # ALL won races' program_ids (any grade, not just G1) -- missions.py's
    # race-SET families (Classic Triple Crown, Big Eight, URA Finale win,
    # AllTrophiesForChara) each need a different resolution of "which race
    # was this" (race.id for the named-race families, race_instance_id for
    # the trophy-shelf family, the raw program_id for the Finale's 41-variant
    # group) -- storing the plain program_id list here and letting missions.py
    # resolve it however each family needs is cheaper than joining N ways at
    # finish time for missions that may never be checked.
    won_program_ids = [r.get("program_id") for r in race_results if r.get("result_rank") == 1]

    # Finished careers are auto-allocated in [PLAYER_CAREER_ID_BASE,
    # INJECT_ID_BASE) -- inside the preserve band (kept across re-seeds) but
    # disjoint from the inject sub-band so max+1 can never hit an inject or
    # spill into the generated band.
    career_ids = [
        c.get("trained_chara_id", 0) for c in roster
        if PLAYER_CAREER_ID_BASE <= c.get("trained_chara_id", 0) < INJECT_ID_BASE
    ]
    new_id = (max(career_ids) + 1) if career_ids else (PLAYER_CAREER_ID_BASE + 1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ancestry, own_factors = _generate_ancestry(chara_info.get("card_id"), chara_info.get("rarity", 3))
    # ...but if the player actually PICKED parents for this run, the lineage is
    # theirs, not an invented one -- that is what makes successive careers a
    # chain instead of unrelated one-offs (see _real_ancestry).
    real = _real_ancestry(roster, parent_1, parent_2)
    if real:
        ancestry = real
    if factor_info_array:
        # The factor_select screen's roll IS this uma's sparks -- serve the
        # same set the player already saw, not an independent re-roll.
        own_factors = copy.deepcopy(factor_info_array)

    return {
        # patch.py rewrites the "<redacted>" placeholder to the live numeric
        # viewer_id at response time. A real string here (e.g. "802445340143")
        # is NOT rewritten -> client gets a string where it wants an int ->
        # "Could not receive parameters from server". Must stay the placeholder.
        "viewer_id": "<redacted>",
        "trained_chara_id": new_id,
        "owner_viewer_id": 0,
        "owner_trained_chara_id": 0,
        "single_mode_chara_id": chara_info.get("single_mode_chara_id"),
        "chara_seed": random.getrandbits(31),
        "card_id": chara_info.get("card_id"),
        "succession_trained_chara_id_1": parent_1,
        "succession_trained_chara_id_2": parent_2,
        "use_type": 0,
        "speed": stats["speed"],
        "stamina": stats["stamina"],
        "power": stats["power"],
        "wiz": stats["wiz"],
        "guts": stats["guts"],
        "fans": fans,
        "rank_score": rank_score,
        "rank": _rank_for_score(rank_score),
        "scenario_id": chara_info.get("scenario_id"),
        "route_id": chara_info.get("route_id"),
        "arrive_route_race_id": chara_info.get("arrive_route_race_id", 0),
        "proper_ground_turf": chara_info.get("proper_ground_turf"),
        "proper_ground_dirt": chara_info.get("proper_ground_dirt"),
        "proper_running_style_nige": chara_info.get("proper_running_style_nige"),
        "proper_running_style_senko": chara_info.get("proper_running_style_senko"),
        "proper_running_style_sashi": chara_info.get("proper_running_style_sashi"),
        "proper_running_style_oikomi": chara_info.get("proper_running_style_oikomi"),
        "proper_distance_short": chara_info.get("proper_distance_short"),
        "proper_distance_mile": chara_info.get("proper_distance_mile"),
        "proper_distance_middle": chara_info.get("proper_distance_middle"),
        "proper_distance_long": chara_info.get("proper_distance_long"),
        "succession_num": _succession_num_for(roster, parent_1, parent_2),
        "rarity": chara_info.get("rarity"),
        "is_saved": 1,
        "is_locked": 0,
        "talent_level": chara_info.get("talent_level"),
        "race_cloth_id": chara_info.get("card_id"),
        "chara_grade": _chara_grade_for(fans),
        "running_style": chara_info.get("race_running_style"),
        "nickname_id": 0,
        "wins": win_count,
        "register_time": now,
        "create_time": now,
        "skill_array": copy.deepcopy(chara_info.get("skill_array", [])),
        "support_card_list": support_card_list,
        "race_result_list": race_results,
        "win_saddle_id_array": win_saddles,
        "nickname_id_array": copy.deepcopy(chara_info.get("nickname_id_array", [])),
        "factor_info_array": own_factors,
        "factor_extend_array": [],
        "succession_chara_array": ancestry,
        # NOT a real wire field -- server-internal only, read back out by
        # missions.py (SongCountThreshold/SpecificSongObtained/
        # ConcertSuccessCount). Extra unknown map keys are safe (this
        # project's own established finding elsewhere -- MessagePack map-
        # mode deserializers skip keys they don't recognize), but if this
        # record is ever round-tripped through something stricter, drop
        # these three rather than the real fields above.
        "grand_live_songs": grand_live_songs,
        "grand_live_lives_done": grand_live_lives_done,
        "_won_program_ids": won_program_ids,
        "_crane_session_plushies": crane_session_plushies,
        # Whatever else this run's scenario froze (Unity Cup's bursts/team
        # titles/Unity Trainings, Trackblazer's Result Pts/Pro Shop totals/
        # Climax legs, Grand Live's performance scores). Nested under ONE
        # server-internal key rather than spread across the record, so a
        # scenario can name a fact whatever its epithet prose calls it without
        # any chance of colliding with a real wire field above.
        "_scenario_facts": scenario_snapshot,
    }


# --------------------------------------------------------------------------
# Roster housekeeping: lock / memo / delete / lineage.
#
# All four come from dump.cs class shapes only -- none has ever been captured
# on this project, so the REQUEST fields below are exact (read straight off
# the Il2Cpp field list) while anything the response has to invent is marked
# server-defined where it matters. They live here rather than in a new module
# because every one of them is a read-modify-write on THIS module's roster.
#
#   TrainedCharaChangeLockMultiRequest        {trained_chara_id_array,
#                                              lock_flag, icon_type}
#   TrainedCharaChangeLockMultiResponse       {}   (no fields at all)
#   TrainedCharaChangeMemoRequest             {trained_chara_id, memo}
#   TrainedCharaChangeMemoResponse            {}
#   TrainedCharaRemoveRequest                 {trained_chara_id_array}
#   TrainedCharaRemoveResponse                {trained_chara_array,
#                                              reward_summary_info,
#                                              transfer_event_info}
#   TrainedCharaGetSuccessionHistoryArrayRequest  {target_viewer_id,
#                                                  target_trained_chara_id}
#   TrainedCharaGetSuccessionHistoryArrayResponse {succession_history_array}

# Memos are NOT a TrainedChara wire field (the dumped class has is_locked but
# no memo -- checked field by field), so they cannot ride on the roster record
# itself without inventing a field the client's positional MessagePack
# formatter never declared. See handle_trained_chara_load's docstring for what
# shipping an undeclared field does to a response. Kept in their own
# account-level map instead: {str(trained_chara_id): memo}.
MEMO_KEY = "trained_chara_memos"

_MAX_MEMO_LEN = 200         # server-defined: no master row or capture pins a
                            # cap, and an unbounded string here is kept forever.


def _envelope(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _int_id_array(payload: dict, key: str) -> list | None:
    """The request's id array, or None if it isn't one. Deduplicated with
    order preserved -- the same id twice in one call is the client being
    sloppy, not a request to delete something twice."""
    ids = payload.get(key)
    if not isinstance(ids, list) or not ids:
        return None
    out = []
    for i in ids:
        if not isinstance(i, int) or isinstance(i, bool):
            return None
        if i not in out:
            out.append(i)
    return out


def handle_change_lock_multi(payload: dict) -> dict:
    """trained_chara/change_lock_multi -- bulk padlock on the roster screen.
    Locked veterans are the ones every destructive path has to skip, which is
    the whole point of the field: handle_remove below refuses outright if any
    id in its batch is locked.

    `icon_type` is in the request and is deliberately NOT stored: TrainedChara
    has no icon field for it to land in, so persisting it would be inventing
    account state no response can ever read back. Every id must resolve, so a
    partially-applied batch cannot leave the roster half-right."""
    viewer_id = payload["viewer_id"]
    ids = _int_id_array(payload, "trained_chara_id_array")
    lock_flag = payload.get("lock_flag")
    if ids is None or not isinstance(lock_flag, int) or isinstance(lock_flag, bool):
        return _refuse()

    _get_or_seed_roster(viewer_id)      # does its own read-modify-write first
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY) or []
    by_id = {c.get("trained_chara_id"): c for c in roster}
    if any(i not in by_id for i in ids):
        return _refuse()

    for i in ids:
        by_id[i]["is_locked"] = 1 if lock_flag else 0
    full_state[ROSTER_KEY] = roster
    state_store.save_state(viewer_id, full_state)
    return _envelope({})


def handle_change_memo(payload: dict) -> dict:
    """trained_chara/change_memo -- the free-text note on one veteran. An
    empty or blank memo clears it rather than storing a blank entry."""
    viewer_id = payload["viewer_id"]
    tcid = payload.get("trained_chara_id")
    memo = payload.get("memo")
    if not isinstance(tcid, int) or isinstance(tcid, bool):
        return _refuse()
    if memo is None:
        memo = ""
    if not isinstance(memo, str) or len(memo) > _MAX_MEMO_LEN:
        return _refuse()

    roster = _get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == tcid for c in roster):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    memos = full_state.setdefault(MEMO_KEY, {})
    if memo.strip():
        memos[str(tcid)] = memo
    else:
        memos.pop(str(tcid), None)
    state_store.save_state(viewer_id, full_state)
    return _envelope({})


def memo_for(full_state: dict, trained_chara_id) -> str:
    """Public read, for whatever later grows a place to display memos."""
    return (full_state.get(MEMO_KEY) or {}).get(str(trained_chara_id), "")


# Support Points (item 110, item_category 30 -- text_data cat 23 #110
# "Support Points", the same currency cards.py spends in support_card/
# strengthen as _GLOBAL_EXP_ITEM_ID). Removing a veteran is called
# TRANSFERRING her in the Global client, and transferring pays SP:
#   dump.cs string table -- Common0299 "SP Held",
#     Common0300 "Transfer Complete",
#     Common0301 "Collected the following Support Points through transfer."
#   DialogDecideRetire owns a PartsDiffSp (a before -> after (+added) meter)
#     and calls UpdateDiffSp(List<TrainedCharaData>) to total the selection.
#   Character0056 "Transferred runners won't come back. Proceed?" is this
#     endpoint's own confirm dialog.
# User-confirmed live 2026-09-10 with a screenshot of that meter reading
# "SP Held  152,000 > 153,290 (+1,290)".
# The item itself is NOT hardcoded -- it comes off the rate table below.

# HOW MUCH -- straight out of master.mdb, no formula and no interpolation.
#
# `trained_chara_trade_item` is the rate table: one row per rank, exactly 98
# of them, contiguous over trained_chara_rank 1..98 and monotonically
# increasing, each carrying {trade_item_category, trade_item_id,
# trade_item_num}. Every row in this build is category 30 / item 110 =
# Support Points, but the category and id are read from the row rather than
# assumed, so a build that trades a rank for something else needs no edit
# here.
#
# "Trade" is the same act the client calls TRANSFER: the roster screen totals
# the selection into a before/after SP meter (DialogDecideRetire's
# PartsDiffSp, via UpdateDiffSp) and then calls this endpoint.
#
# Keyed on RANK, not rank_score -- which is why two veterans scoring 74,504
# and 999,999 pay the SAME 1,290: both land in rank 98, the top band
# (single_mode_rank id 98 = min_value 71,400). User-measured live 2026-09-10
# and matching this table exactly at every rank measured:
#   rank  2 (G+)  -> 20      rank 14 (A+)  -> 300
#   rank  4 (F+)  -> 40      rank 16 (S+)  -> 380
#   rank  9 (C)   -> 140     rank 17 (SS)  -> 420
#                            rank 98 (US9) -> 1290
# (An earlier pass here anchored on those seven and interpolated the rest;
# that was wrong at 84 of the 98 ranks. The table is the source.)


@functools.lru_cache(maxsize=1)
def _trade_item_by_rank() -> dict:
    """{rank: (item_category, item_id, item_num)} from trained_chara_trade_item.
    Cached -- master.mdb is read-only for the process lifetime, same as every
    other master lookup in this module."""
    return {r["trained_chara_rank"]: (r["trade_item_category"],
                                      r["trade_item_id"],
                                      r["trade_item_num"])
            for r in master_data.query(
                "SELECT trained_chara_rank, trade_item_category, trade_item_id, "
                "trade_item_num FROM trained_chara_trade_item")}


def transfer_trade_item(entry: dict):
    """(item_category, item_id, item_num) paid for transferring one veteran,
    or None if its rank has no row.

    Falls back to deriving the rank from rank_score when the record carries no
    rank field, so an older roster entry still prices; a rank above the
    table's top clamps to it (the top band is open-ended -- single_mode_rank
    98 runs to 99,999, and careers here can score past that)."""
    table = _trade_item_by_rank()
    if not table:
        return None
    rank = entry.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
        rank = _rank_for_score(entry.get("rank_score") or 0)
    if rank in table:
        return table[rank]
    top = max(table)
    return table[top] if rank > top else table[min(table)]


def handle_remove(payload: dict) -> dict:
    """trained_chara/remove -- transfer veterans off the roster for good.

    This is the Global client's "transfer" action, not a silent delete: the
    roster screen totals the Support Points the selection is worth in a
    before/after meter (DialogDecideRetire's PartsDiffSp) and then calls here,
    so the response has to actually pay that out.

    THE destructive endpoint in this module, so the guards are the point:

      * every id must resolve (no silently-ignored typo),
      * no id may be LOCKED (that is what the padlock is for), and
      * the six real starters (ids 1..STARTER_ROSTER_SIZE) are refused --
        they are seeded once and never regenerated, unlike the house pool,
        so transferring one is unrecoverable short of a state wipe. Generated
        house veterans (>= HOUSE_ID_BASE) ARE transferable: a re-seed rebuilds
        that band by design.

    A refusal is all-or-nothing -- a batch that removes half of what the
    player selected and then 205s is worse than one that removes nothing. The
    payout is totalled the same way: every veteran is priced BEFORE anything
    is removed, so the credit matches exactly the set that went.

    APPLIED IMMEDIATELY, not at next login. Two separate things have to
    happen, and only doing one of them is the classic failure here:
      1. the SP is added to item_list_state and persisted, so it survives; and
      2. it rides back in reward_summary_info.add_item_list as the AMOUNT
         GAINED, which is the channel the client applies to its own live
         wallet (the real JP capture of this endpoint carries exactly that,
         and shop._grant documents the same restart-vs-live distinction for
         card purchases).
    Persisting without (2) is what would make the points "show up after a
    restart" -- the state would be right but the running client would never
    be told.

    transfer_event_info is the same live block transfer.py builds for every
    other roster-shrinking response, and is null when no Transfer event is
    running."""
    viewer_id = payload["viewer_id"]
    ids = _int_id_array(payload, "trained_chara_id_array")
    if ids is None:
        return _refuse()

    _get_or_seed_roster(viewer_id)
    full_state = state_store.get_state(viewer_id) or {}
    roster = full_state.get(ROSTER_KEY) or []
    by_id = {c.get("trained_chara_id"): c for c in roster}
    if any(i not in by_id for i in ids):
        return _refuse()
    if any(by_id[i].get("is_locked") for i in ids):
        return _refuse()
    if any(1 <= i <= STARTER_ROSTER_SIZE for i in ids):
        return _refuse()

    # Price the whole selection before removing any of it.
    payouts = [p for p in (transfer_trade_item(by_id[i]) for i in ids) if p]

    doomed = set(ids)
    full_state[ROSTER_KEY] = [c for c in roster
                              if c.get("trained_chara_id") not in doomed]
    memos = full_state.get(MEMO_KEY)
    if isinstance(memos, dict):
        for i in ids:
            memos.pop(str(i), None)

    # Local imports: transfer.py already imports THIS module at top level, and
    # shop.py is reached for its clamped inventory arithmetic + summary shape.
    from . import transfer
    from . import shop

    # Total per item first: a mixed batch must pay ONE line per item, not one
    # per veteran, or the client renders the same currency several times over.
    owed: dict = {}
    for _category, item_id, num in payouts:
        owed[item_id] = owed.get(item_id, 0) + (num or 0)

    summary = shop._empty_summary()
    for item_id, num in owed.items():
        if num <= 0:
            continue
        # Report what was actually credited rather than what was owed --
        # shop._add_item clamps to the item's real limit_num, and a client
        # told it gained more than it did shows a number that unwinds on the
        # next load/index.
        before = shop._item_count(full_state, item_id)
        credited = shop._add_item(full_state, item_id, num) - before
        if credited > 0:
            summary["add_item_list"].append(
                {"item_id": item_id, "number": credited})

    state_store.save_state(viewer_id, full_state)
    return _envelope({
        "trained_chara_array": copy.deepcopy(full_state[ROSTER_KEY]),
        "reward_summary_info": summary,
        "transfer_event_info": transfer.transfer_event_info(viewer_id),
    })


# SuccessionHistory (dump.cs) -- note `hisotry_type`, the real misspelling on
# the wire. Spelling it correctly here would put the value in a field the
# client's formatter does not have.
_HISTORY_TYPE_OWN_CAREER = 1


def _epoch_of(create_time) -> int:
    """A record's "%Y-%m-%d %H:%M:%S" create_time as the int epoch
    SuccessionHistory.date wants. 0 when the record has no usable timestamp
    (the captured starter set) -- an honest "unknown", not a fabricated date."""
    if not isinstance(create_time, str) or not create_time.strip():
        return 0
    try:
        stamp = datetime.strptime(create_time.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())


def handle_get_succession_history_array(payload: dict) -> dict:
    """trained_chara/get_succession_history_array -- "who has this veteran
    been a parent to", the lineage panel on a veteran's detail screen.

    On the real server this spans other players (that is what user_name and
    circle_name are for -- a borrowed parent records the lender). Here there
    are no other players' careers to draw on, so the history is built from
    the one genuinely real source this account has: its OWN finished careers.
    Every roster record stores the two parents the player actually picked
    (succession_trained_chara_id_1/2, written by
    build_trained_chara_from_career), so "every career that used this veteran
    as a parent" is a real query over real data rather than an invented list
    -- and it is exactly what the panel is meant to show.

    target_viewer_id is accepted and ignored: the only roster this server can
    answer for is the caller's own, and refusing a foreign id would break the
    panel rather than tell the player anything.

    `id` is a positional row number over the returned set, not a stored key --
    nothing else on the wire refers back to it."""
    viewer_id = payload["viewer_id"]
    target = payload.get("target_trained_chara_id")
    if not isinstance(target, int) or isinstance(target, bool):
        return _refuse()

    roster = _get_or_seed_roster(viewer_id)
    if not any(c.get("trained_chara_id") == target for c in roster):
        return _refuse()

    name = state_store.extract_json_path(
        viewer_id, "load_index", "$.data.user_info.name") or ""
    from .. import social
    circle = social.circle_of(viewer_id) or {}
    circle_name = circle.get("name") or ""

    children = [c for c in roster
                if target in ((c.get("succession_trained_chara_id_1") or 0),
                              (c.get("succession_trained_chara_id_2") or 0))]
    children.sort(key=lambda c: (_epoch_of(c.get("create_time")),
                                 c.get("trained_chara_id") or 0))
    out = []
    for i, child in enumerate(children, start=1):
        out.append({
            "id": i,
            # patch.py rewrites the placeholder to the live numeric viewer_id
            # -- see build_trained_chara_from_career for why a real string here
            # would reach the client as a string where it wants an int.
            "viewer_id": "<redacted>",
            "trained_chara_id": child.get("trained_chara_id"),
            "hisotry_type": _HISTORY_TYPE_OWN_CAREER,
            "succession_card_id": child.get("card_id") or 0,
            "date": _epoch_of(child.get("create_time")),
            "user_name": name,
            "circle_name": circle_name,
        })
    return _envelope({"succession_history_array": out})
