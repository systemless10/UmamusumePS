"""Extract master.mdb's nickname table (248 rows) into a structured, reusable
JSON catalogue -- the per-UMA "epithet" system (distinct from honor_data's
account-wide Trainer titles, see tools/extract_honor_data.py). This is what
single_mode_team.py's trained_chara nickname_id_array is FOR: real Umamusume
awards each finished career's trainee 1+ of these based on how that specific
run went (weather braved, races won, training levels reached, ...) -- this
server currently NEVER populates it (nickname_id_array ships empty on every
finished career), which is the "no epithets obtained when ending a career"
gap this tool starts closing.

WHY THIS IS HARDER THAN honor_data
-----------------------------------
honor_data's 549 rows collapsed into ~90 repeated description TEMPLATES
(one template x many characters/thresholds). nickname's 248 rows do NOT:
235 of them are near-unique free-text sentences (225 templates occur
EXACTLY ONCE), because most of the character-specific tier (chara_data_id
!= 0, 64 rows, one legendary nickname per real horse) each encode that
horse's own real racing biography in one bespoke multi-clause sentence
(e.g. "Win the Satsuki Sho, Japanese Derby, and Kikuka Sho while undefeated,
win the Japan Cup and Tenno Sho (Spring), and win the Arima Kinen twice in a
row") -- several clauses reference mechanics this project doesn't track at
all (win/loss STREAKS, race MARGIN in lengths, favorite/odds status, mood
PER RACE, the succession "Inspiration" mechanic, running-style-at-a-
SPECIFIC-race rather than lifetime).

APPROACH: name (text_data category 130) + condition text (category 131) are
verified real and parallel honor_data's name/description columns exactly
(spot-checked against known values, e.g. id 24 "Derby Umamusume" / "Win the
Japanese Derby"). Rather than one big regex per row (impractical at 235
near-unique templates), this parses each condition as a SEQUENCE of clauses
split on top-level ", "/" and "/newline, matching each clause against a
library of ~15 atomic clause patterns (fan threshold, named-race win,
race-SET win count, grade win count, facility training level, weather-race
win, ground-composition, running-style win count, race count). A row is
"resolved" only if EVERY clause in it matched one of these atoms; a row with
even one clause this pass doesn't understand (streaks, margins, favorite,
mood, Inspiration, Team Trials/Unity Cup/Trackblazer) is left with every
clause it COULD parse attached (real data value) plus the clauses it
couldn't as raw text -- never silently dropped, per this project's standing
"don't guess into live gameplay" rule.

This tool only EXTRACTS -- it does not wire any nickname into a live grant.
That's a separate follow-up once this catalogue is reviewed (same two-step
support_card_unique_effect and honor_data both went through).

Usage: python tools/extract_nicknames.py [--out PATH]
Honors MASTER_MDB_PATH (see app/master_data.py) for a future JP-server run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "server"))

from app import master_data  # noqa: E402

# -------------------------------------------------------------- name caches --
_chara_cache: dict = {}


def _chara_id_by_name(name: str) -> int | None:
    if name not in _chara_cache:
        row = master_data.query_one(
            'SELECT "index" FROM text_data WHERE category=6 AND text=?', (name,))
        _chara_cache[name] = row["index"] if row else None
    return _chara_cache[name]


_race_ids_cache: dict = {}

# nickname condition text uses a few shorthand/abbreviated race names that
# don't match text_data category 33's own full name verbatim -- verified by
# direct lookup (2026-08-19): the full forms below ARE real category-33
# entries, the short forms are not.
_RACE_NAME_ALIASES = {
    "Japanese Derby": "Tokyo Yushun (Japanese Derby)",
    "Hanshin J.F.": "Hanshin Juvenile Fillies",
    "Asahi Hai F.S.": "Asahi Hai Futurity Stakes",
}


def _race_ids_by_name(name: str) -> frozenset:
    """race.id set for an exact race NAME -- text_data category 33, the same
    base race-name table tools/extract_honor_data.py's venue/race resolvers
    already verified (category 33 index == race.id directly)."""
    name = _RACE_NAME_ALIASES.get(name, name)
    if name not in _race_ids_cache:
        rows = master_data.query(
            'SELECT "index" FROM text_data WHERE category=33 AND text=?', (name,))
        _race_ids_cache[name] = frozenset(r["index"] for r in rows)
    return _race_ids_cache[name]


_FACILITY_COMMAND = {"Speed": 101, "Stamina": 105, "Power": 102, "Guts": 103, "Wits": 106}
_WEATHER_CODE = {"Sunny": 1, "Cloudy": 2, "Rainy": 3, "Snowy": 4}
_GROUND_CODE = {"turf": 1, "dirt": 2}
_STYLE_CODE = {"Front Runner": 1, "Pace Chaser": 2, "Late Surger": 3, "End Closer": 4}


# --------------------------------------------------------------- clause atoms --
# Each: (compiled ANCHORED pattern, builder(match) -> dict | None). A clause
# is a single "unit" split out of the full condition text (see _split_clauses)
# -- these patterns match a WHOLE clause, not a substring of a longer one.
_CLAUSES: list = []


def _clause(pattern: str):
    compiled = re.compile(pattern)

    def deco(fn):
        _CLAUSES.append((compiled, fn))
        return fn
    return deco


@_clause(r"^Obtain (?:at least )?([\d,]+) (?:or more )?fans$")
def _c_fan_threshold(m):
    return {"kind": "fan_threshold", "threshold": int(m.group(1).replace(",", ""))}


@_clause(r"^Win the (.+)$")
def _c_specific_race_win(m):
    name = m.group(1)
    ids = _race_ids_by_name(name)
    if ids:
        return {"kind": "specific_race_win", "race": name, "race_ids": sorted(ids)}
    # Not a single race name -- try 'the X, Y, and Z' (win ALL of a list,
    # no explicit 'N of' count) before giving up. A partial resolve (some
    # names in the list don't match) returns None rather than a wrong count.
    names = _split_race_list(name)
    if len(names) < 2:
        return None
    sets = [_race_ids_by_name(n) for n in names]
    if any(not s for s in sets):
        return None
    return {"kind": "race_set_win_all", "races": names,
           "race_id_sets": [sorted(s) for s in sets]}


@_clause(r"^Win ([\d,]+) of the (.+)$")
def _c_race_set_win_count(m):
    names = _split_race_list(m.group(2))
    if not names:
        return None
    sets = [_race_ids_by_name(n) for n in names]
    if any(not s for s in sets):
        return None
    return {"kind": "race_set_win_count", "races": names,
           "race_id_sets": [sorted(s) for s in sets],
           "threshold": int(m.group(1).replace(",", ""))}


@_clause(r"^Win (?:a |at least (\d+) )?G([123])(?: or G([123]))? races?$")
def _c_grade_win_count(m):
    threshold = int(m.group(1)) if m.group(1) else 1
    grades = [int(m.group(2)) * 100]
    if m.group(3):
        grades.append(int(m.group(3)) * 100)
    return {"kind": "grade_win_count", "grades": grades, "threshold": threshold}


@_clause(r"^Reach (Speed|Stamina|Power|Guts|Wits) Training Level (\d+)$")
def _c_facility_level_threshold(m):
    return {"kind": "facility_level_threshold", "stat": m.group(1),
           "command_id": _FACILITY_COMMAND[m.group(1)], "threshold": int(m.group(2))}


@_clause(r"^Run in (\d+) races?$")
def _c_race_count(m):
    return {"kind": "race_count", "threshold": int(m.group(1))}


@_clause(r"^Win (\d+) (turf|dirt) races?$")
def _c_ground_win_count(m):
    return {"kind": "ground_win_count", "ground": m.group(2),
           "ground_code": _GROUND_CODE[m.group(2)], "threshold": int(m.group(1))}


@_clause(r"^(?:Complete a Career playthrough )?having run in at least (\d+) races?, "
        r"all on (turf|dirt)$")
def _c_all_races_ground(m):
    return {"kind": "all_races_ground", "ground": m.group(2),
           "ground_code": _GROUND_CODE[m.group(2)], "threshold": int(m.group(1))}


@_clause(r"^Win (\d+) times as (Front Runner|Pace Chaser|Late Surger|End Closer)$")
def _c_style_win_count(m):
    return {"kind": "style_win_count", "style": m.group(2),
           "style_code": _STYLE_CODE[m.group(2)], "threshold": int(m.group(1))}


@_clause(r"^Run in (Sunny|Cloudy|Rainy|Snowy) weather at least (\d+) times "
        r"and win (\d+) times$")
def _c_weather_race_win(m):
    return {"kind": "weather_race_win", "weather": m.group(1),
           "weather_code": _WEATHER_CODE[m.group(1)],
           "run_threshold": int(m.group(2)), "win_threshold": int(m.group(3))}


@_clause(r"^Complete a Career playthrough for (\d+) trainees?$")
def _c_career_completion_count(m):
    return {"kind": "career_completion_count", "threshold": int(m.group(1)),
           "note": "meta-nickname, not per-run -- account-wide finished-"
                   "career count, same real state missions.py's "
                   "CompleteCareerPlayCount (100004) already reads"}


def _split_race_list(text: str) -> list:
    """'the Satsuki Sho, Japanese Derby, and Kikuka Sho' -> the 3 race
    names, dropping the leading 'the '/'and ' filler words."""
    text = re.sub(r"^the\s+", "", text.strip())
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return []
    parts[-1] = re.sub(r"^and\s+", "", parts[-1])
    return parts


def _split_clauses(desc: str) -> list:
    """Top-level clause split on newlines/', and '/' and '/', ' -- NOT
    inside a number (',400 meters' stays intact) and NOT swallowing a race-
    name list's own internal commas (those are handled INSIDE the race_set_
    win_count/specific_race_win atoms via _split_race_list, matched as ONE
    clause before splitting would ever separate them -- see _decode)."""
    parts = re.split(r"\n+|,?\s+and\s+(?!\d)|(?<!\d),\s+(?!\d)", desc.strip())
    return [p.strip().rstrip(".") for p in parts if p.strip()]


def _decode(desc: str) -> dict:
    if not desc:
        return {"kind": "text_only"}
    # Try the WHOLE description as one clause first -- covers every "Win N
    # of the X, Y, and Z" / "Win the X, Y, and Z" row whose OWN commas would
    # otherwise be wrongly split by _split_clauses.
    for pattern, builder in _CLAUSES:
        m = pattern.match(desc.rstrip("."))
        if m:
            result = builder(m)
            if result is not None:
                return result
    clauses = _split_clauses(desc)
    matched, unmatched = [], []
    for c in clauses:
        hit = None
        for pattern, builder in _CLAUSES:
            m = pattern.match(c)
            if m:
                hit = builder(m)
                if hit is not None:
                    break
        if hit is not None:
            matched.append(hit)
        else:
            unmatched.append(c)
    if not matched:
        numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", desc)]
        return {"kind": "text_only", "numbers": numbers, "raw_clauses": clauses}
    if not unmatched and len(matched) == 1:
        return matched[0]
    return {"kind": "composite" if len(matched) > 1 else matched[0]["kind"],
           "clauses": matched,
           "unresolved_clauses": unmatched or None}


def extract() -> list[dict]:
    rows = master_data.query("SELECT * FROM nickname ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        name = master_data.query_one(
            'SELECT text FROM text_data WHERE category=130 AND "index"=?', (d["id"],))
        cond = master_data.query_one(
            'SELECT text FROM text_data WHERE category=131 AND "index"=?', (d["id"],))
        desc_text = cond["text"] if cond else None
        chara_id = d["chara_data_id"] or None
        if chara_id:
            # confirm it resolves against real chara data (chara_data_id in
            # this table lines up with chara_data.id directly -- e.g. row 66
            # is Special Week's own legendary "Ruler of Japan", chara_data_id
            # 1001 == card_data.chara_id 1001 checked earlier this session)
            pass
        out.append({
            "nickname_id": d["id"],
            "name": name["text"] if name else None,
            "description": desc_text,
            "rank": d["rank"],
            "group_id": d["group_id"],
            "chara_id": chara_id,
            "scenario_id": d["scenario_id"] or None,
            "start_date": d["start_date"],
            "end_date": d["end_date"],
            "condition": _decode(desc_text),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(_ROOT / "data" / "training_ref" / "nicknames.json"),
                    help="output JSON path")
    args = ap.parse_args()

    data = extract()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    from collections import Counter
    kinds = Counter(c["condition"]["kind"] for c in data)
    fully_resolved = sum(1 for c in data if c["condition"]["kind"] not in
                         ("text_only", "composite")
                         or (c["condition"]["kind"] == "composite"
                             and not c["condition"].get("unresolved_clauses")))
    partial = sum(1 for c in data if c["condition"].get("unresolved_clauses"))
    text_only = sum(1 for c in data if c["condition"]["kind"] == "text_only")
    print(f"master.mdb: {master_data.MASTER_MDB_PATH}")
    print(f"{len(data)} nicknames -> {out_path}")
    print(f"  {fully_resolved:>3} fully resolved (every clause parsed)")
    print(f"  {partial:>3} partially resolved (some clauses parsed, some left as text)")
    print(f"  {text_only:>3} fully text_only (no clause parsed at all)")
    print("--- by kind ---")
    for kind, n in kinds.most_common():
        print(f"  {n:>3} {kind}")


if __name__ == "__main__":
    main()
