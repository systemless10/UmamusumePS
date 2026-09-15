"""Trackblazer (single_mode_free) shop-lineup extractor.

WHAT THIS COLLECTS
------------------
The Trackblazer shop restocks on a fixed schedule -- 11 resets, each covering a
window of career turns. What it STOCKS on each reset is rolled server-side and
is not in master.mdb (there is no lineup-group table there), so the only way to
learn the odds is to observe real careers and count.

This tool does the observing half: it reads career data and writes out EVERY
career's shop data, raw, one entry per reset.

It accepts TWO input shapes and will take both in one run, deduplicating by
career:
  * capture sessions -- a directory of NNNN_<endpoint>.json per-request files,
    where the lineup is response.data.free_data_set.pick_up_item_info_array.
  * career logs -- one career_log_*.json per career, holding a `turns` array
    where the lineup is each turn's `server_shop_rows_raw`. Those are the same
    server rows with the same limit_turn semantics; the shop_id, which the log
    does not record, is recovered from the turn number (the 11 windows are
    contiguous and non-overlapping over turns 13-78).
Point `extract` at a folder of either, or a parent of both. Nothing is summed, averaged
or pooled here -- the point is to accumulate a dataset across thousands of
careers first and decide how to slice it afterwards. `report` is a convenience
tally over that dataset, not the dataset itself.

NO master.mdb NEEDED
--------------------
Only item IDs are stored, never names. The reset schedule below is copied out
of master.mdb's `single_mode_free_shop` table (11 rows, static game data), so
the tool runs anywhere with nothing but the capture files.

TRACKBLAZER ONLY
----------------
Every snapshot must satisfy BOTH:
  * chara_info.scenario_id == 4   (4 = "Trackblazer: Start of the Climax";
                                   1 = URA, 2 = Unity Cup, 3 = Grand Live)
  * a `free_data_set` block in the response
Anything else is skipped and counted, so a run reports how much it ignored.

TWO SHOP SLOT KINDS (kept apart in the output)
----------------------------------------------
Every lineup entry is a `pick_up_item_info` carrying a `limit_turn`:
  limit_turn == 0  BASE stock. Rolled when the reset opens and present for the
                   whole window; it stays in the array after being bought out
                   (item_buy_num rises, the row does not vanish). So any single
                   snapshot inside the window is a complete base observation.
  limit_turn >  0  a LIMITED offer appended mid-window, gone after that turn.
                   Verified in captures/bot/20260905_144604_icarus: reset 2
                   (turns 19-24) opened with 9 base rows; two more (1105, 1103,
                   limit_turn=22) appeared on turn 20 and were gone by turn 23.
                   These can only be counted from a career observed on every
                   turn of the window, hence the per-reset coverage flags.

MISSING RESETS ARE RECORDED, NOT GUESSED
----------------------------------------
A career that ended early, or a capture that started late, will not have all 11
resets. Each career carries three explicit lists so a later analysis can pick an
honest denominator:
  resets_seen        windows with data
  resets_missing     windows INSIDE the observed turn span that have no data --
                     a real gap in the capture
  resets_not_reached windows that begin after the last turn observed -- the
                     career simply had not got there
Per reset, `turns_missing` does the same at turn granularity.

USAGE
-----
  python tools/trackblazer_shop.py extract captures/bot
  python tools/trackblazer_shop.py extract path/to/career_logs
      -> ./trackblazer_shop_data.json  (written where the command was run)

  python tools/trackblazer_shop.py extract captures/bot -o mydata.json --append
  python tools/trackblazer_shop.py extract captures/bot --format jsonl
  python tools/trackblazer_shop.py report trackblazer_shop_data.json

--append merges into an existing dataset, keyed by (career, shop_id), so
batches of captures accumulate into one file over time.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 4 = "Trackblazer: Start of the Climax" (master.mdb single_mode_scenario /
# text_data category 119). Nothing else is collected.
SCENARIO_ID_TRACKBLAZER = 4

# Only these endpoints ever carry free_data_set. Everything else in a capture
# session (load_index, trained_chara_load) is multi-megabyte and irrelevant --
# with thousands of careers, not parsing them is the difference between a
# minute and an hour.
ENDPOINT_MARKER = "single_mode_free"

# master.mdb `single_mode_free_shop`, verbatim: shop_id -> (start_turn,
# end_turn, lineup_group_id). Baked in so the tool needs no game install.
# Note resets 4-6 share lineup group 4 and 7-10 share group 5 -- that is the
# game's own grouping, and it is NOT a licence to pool their observations:
# whether the groups really behave identically is one of the things the data
# is meant to answer.
SHOP_WINDOWS = {
    1:  (13, 18, 1),
    2:  (19, 24, 2),
    3:  (25, 30, 3),
    4:  (31, 36, 4),
    5:  (37, 42, 4),
    6:  (43, 48, 4),
    7:  (49, 54, 5),
    8:  (55, 60, 5),
    9:  (61, 66, 5),
    10: (67, 72, 5),
    11: (73, 78, 6),
}

DEFAULT_OUT = "trackblazer_shop_data.json"

# ------------------------------------------------------------- item families --
# Sets of items that are the SAME good in five (or four, or six) flavours: same
# id prefix, same price in single_mode_free_shop_item, differing only in which
# stat or condition they touch. Because they are interchangeable to the roller,
# `report --group` treats each set as one outcome, which multiplies the
# effective sample for that row by the number of variants.
#
# Membership is by price, NOT by id prefix alone. The tiered families are
# deliberately NOT here -- Vita 20/40/65 (35/55/75 coins) and Coaching /
# Motivating / Empowering Megaphone (40/55/70) share a prefix but are different
# strengths at different prices, and the data shows them at plainly different
# rates. Merging those would be wrong.
#
# The equal-chance assumption is TESTED, not assumed: --group runs a chi-square
# goodness-of-fit against uniform within each family and prints the p-value, so
# a family that stops behaving symmetrically shows up instead of being hidden
# by the very aggregation that assumes it.
ITEM_FAMILIES = {
    "Stat Notepad":       [1001, 1002, 1003, 1004, 1005],
    "Stat Manual":        [1101, 1102, 1103, 1104, 1105],
    "Stat Scroll":        [1201, 1202, 1203, 1204, 1205],
    "Condition Cure":     [4101, 4102, 4103, 4104, 4105, 4106],
    "Training App":       [5001, 5002, 5003, 5004, 5005],
    "Ankle Weights":      [9001, 9002, 9003, 9004],
}
_FAMILY_OF = dict((i, name) for name, ids in ITEM_FAMILIES.items() for i in ids)


# ------------------------------------------------------------- type safety --
# Captures come from msgpack, which types things exactly as the server sent
# them -- but a capture re-encoded through another tool, another bot's log, or
# a future client can hand us "13" where we expect 13. Left alone that is not a
# crash, it is silent corruption: `bool("0")` is True, so a stringified
# limit_turn reclassifies EVERY base item as a limited offer and the run still
# reports success. So every field the logic keys on is coerced, and the run
# says how often it had to.

def _int(v):
    """int(v) for anything that is genuinely a whole number, else None.

    Accepts int, integral float (2.0), and numeric strings including "  7 ".
    Rejects bools -- True would otherwise silently become shop_id 1.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return None
            return int(f) if f.is_integer() else None
    return None


# Fields whose TYPE the logic depends on. Normalised in place: the value is
# unchanged, only its type, so nothing is lost from the verbatim server row.
_INT_FIELDS = ("shop_item_id", "item_id", "limit_turn", "coin_num",
               "original_coin_num", "item_buy_num", "limit_buy_count")


def _normalize_item(it, stats):
    out = dict(it)
    for k in _INT_FIELDS:
        if k not in out:
            continue
        v = _int(out[k])
        if v is None:
            if out[k] is not None:
                stats["uncoercible"][k] += 1
            continue
        if type(out[k]) is not int:
            stats["coerced"][k] += 1
        out[k] = v
    return out


# ----------------------------------------------------------------- extract --

def _absorb_items(rec, items, turn, stats):
    """Fold one turn's lineup rows into a reset record.

    Shared by both readers: the capture format's pick_up_item_info_array and
    the career_log format's server_shop_rows_raw are the same rows, so the
    classification and de-duplication rules must be too.
    """
    base_by_sid = dict((e["shop_item_id"], e) for e in rec["base"])
    ltd_by_sid = dict((e["shop_item_id"], e) for e in rec["limited"])
    for slot, it in enumerate(items):
        if not isinstance(it, dict):
            stats["bad_item_row"] += 1
            continue
        it = _normalize_item(it, stats)
        sid = it.get("shop_item_id")
        if sid is None:
            stats["no_shop_item_id"] += 1
            continue
        # Explicit > 0, never bool(): a stringified "0" is truthy and would
        # silently reclassify the entire base lineup as limited offers.
        limit_turn = it.get("limit_turn")
        limited = isinstance(limit_turn, int) and limit_turn > 0
        seen = ltd_by_sid if limited else base_by_sid
        if sid in seen:
            # Already recorded; just extend the window we saw it live in.
            # Purchases mutate item_buy_num, so the first sighting is the one
            # that reflects the lineup as it was rolled.
            seen[sid]["last_seen_turn"] = turn
            continue
        # The server's row, type-normalised but otherwise verbatim so nothing
        # is lost, then annotated.
        entry = it
        # Slot index is the order the server generated the lineup in.
        # shop_item_id runs career-global (reset 2 starts at 7, not 1), so
        # within one reset the slot ordering is the only positional signal.
        entry["slot"] = slot
        entry["first_seen_turn"] = turn
        entry["last_seen_turn"] = turn
        (rec["limited"] if limited else rec["base"]).append(entry)
        seen[sid] = entry


def iter_capture_dirs(paths):
    """Yield every capture session directory under the given paths.

    A session directory is one that directly contains NNNN_<endpoint>.json
    files, so both `captures/bot` (a parent of many sessions) and a single
    session path work as arguments.
    """
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.isdir(p):
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            if any(ENDPOINT_MARKER in f and f.endswith(".json") for f in filenames):
                yield dirpath
                dirnames[:] = []  # a session dir has no nested sessions


# ------------------------------------------------------- career_log format --
# The bot writes a second, entirely different shape: one career_log_*.json per
# career, holding a `turns` array rather than a directory of per-request
# captures. The shop lineup lives in each turn's `server_shop_rows_raw`, which
# is the server's pick_up_item_info array verbatim -- same fields, same
# limit_turn semantics, no expired rows left lingering (verified against
# career_log_20260826_153524.json: 726 rows, none stale).
#
# Two things the capture format supplies are absent here and are recovered:
#   shop_id     not in the log at all -- derived from the turn via SHOP_WINDOWS,
#               which is safe because the 11 windows are contiguous and
#               non-overlapping across turns 13-78.
#   scenario_id top-level on the document instead of per-snapshot.

def _shop_id_for_turn(turn):
    for shop_id, (start, end, _g) in SHOP_WINDOWS.items():
        if start <= turn <= end:
            return shop_id
    return None


def looks_like_career_log(path):
    """Cheap sniff -- a career log is one JSON object with a `turns` array.

    Reads only the head of the file: with thousands of logs, fully parsing each
    one just to classify it would dominate the run.
    """
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return '"turns"' in head and '"scenario_id"' in head


def iter_career_logs(paths):
    """Yield every career_log-shaped .json file under the given paths."""
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            if p.endswith(".json") and looks_like_career_log(p):
                yield p
            continue
        for dirpath, _dirnames, filenames in os.walk(p):
            # A capture session directory is handled by the other reader; its
            # per-request files must not be sniffed as career logs.
            if any(ENDPOINT_MARKER in f and f.endswith(".json") for f in filenames):
                continue
            for name in sorted(filenames):
                if not name.endswith(".json"):
                    continue
                full = os.path.join(dirpath, name)
                if looks_like_career_log(full):
                    yield full


def _head_scenario_id(path):
    """scenario_id read straight out of the file head, without parsing it.

    Career logs are multi-megabyte and a real folder of them is mostly other
    scenarios (a 2053-log folder was 1741 Trackblazer, 234 not). json.load on
    every one just to discard it costs minutes and hundreds of MB of parsing,
    so the cheap read decides first. None means "could not tell" -- then the
    full parse settles it rather than guessing.
    """
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    m = re.search(r'"scenario_id"\s*:\s*(\d+)', head)
    return int(m.group(1)) if m else None


def extract_career_log(path, stats):
    """One career_log_*.json -> {career_key: career record}, the same shape the
    capture reader produces, so everything downstream is unchanged."""
    head_sid = _head_scenario_id(path)
    if head_sid is not None and head_sid != SCENARIO_ID_TRACKBLAZER:
        stats["other_scenario"] += 1
        stats["other_scenario_ids"][head_sid] += 1
        return {}
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        stats["unreadable"] += 1
        return {}
    if not isinstance(doc, dict):
        stats["unreadable"] += 1
        return {}

    scenario_id = _int(doc.get("scenario_id"))
    if scenario_id != SCENARIO_ID_TRACKBLAZER:
        stats["other_scenario"] += 1
        stats["other_scenario_ids"][doc.get("scenario_id")] += 1
        return {}

    ident = doc.get("identity") or {}
    started = doc.get("started_at") or os.path.basename(path)
    key = "%s@%s" % (_int(ident.get("single_mode_chara_id")), started)
    career = {
        "career": key,
        "session": os.path.basename(path),
        "single_mode_chara_id": _int(ident.get("single_mode_chara_id")),
        "start_time": started,
        "card_id": _int(ident.get("card_id")),
        "scenario_id": SCENARIO_ID_TRACKBLAZER,
        "turns_observed": [],
        "resets": {},
    }

    for t in doc.get("turns") or []:
        if not isinstance(t, dict):
            continue
        turn = _int(t.get("turn"))
        if turn is None:
            stats["no_turn"] += 1
            continue
        stats["snapshots"] += 1
        if turn not in career["turns_observed"]:
            career["turns_observed"].append(turn)

        items = t.get("server_shop_rows_raw")
        if items is None:
            continue          # turns before the shop opens carry no rows
        if not isinstance(items, list):
            stats["bad_item_array"] += 1
            continue
        shop_id = _shop_id_for_turn(turn)
        if shop_id is None or not items:
            continue

        start_turn, end_turn, group = SHOP_WINDOWS[shop_id]
        rec = career["resets"].get(shop_id)
        if rec is None:
            rec = career["resets"][shop_id] = {
                "shop_id": shop_id, "start_turn": start_turn,
                "end_turn": end_turn, "lineup_group_id": group,
                "turns_seen": [], "base": [], "limited": [],
            }
        if turn not in rec["turns_seen"]:
            rec["turns_seen"].append(turn)
        _absorb_items(rec, items, turn, stats)
    return {key: career}


def _snapshots(session_dir, stats):
    """Yield (turn, free_data_set, chara_info) for every TRACKBLAZER snapshot in
    a session, in capture order. Non-Trackblazer snapshots are counted and
    dropped."""
    for name in sorted(os.listdir(session_dir)):
        if not (name.endswith(".json") and ENDPOINT_MARKER in name):
            continue
        try:
            with io.open(os.path.join(session_dir, name), encoding="utf-8",
                         errors="replace") as fh:
                rec = json.load(fh)
        except (ValueError, OSError):
            stats["unreadable"] += 1
            continue
        data = (rec.get("response") or {}).get("data") or {}
        fds = data.get("free_data_set")
        ci = data.get("chara_info")
        if not isinstance(fds, dict) or not isinstance(ci, dict):
            continue
        scenario_id = _int(ci.get("scenario_id"))
        if scenario_id != SCENARIO_ID_TRACKBLAZER:
            # A free_data_set without the Trackblazer scenario id should not
            # exist; if it ever does, it is not ours to interpret.
            stats["other_scenario"] += 1
            stats["other_scenario_ids"][ci.get("scenario_id")] += 1
            continue
        turn = _int(ci.get("turn"))
        if turn is None:
            stats["no_turn"] += 1
            continue
        stats["snapshots"] += 1
        yield turn, fds, ci


def career_key(ci, session_dir):
    """Stable identity for one career run.

    start_time is minted when the career begins and single_mode_chara_id is the
    per-run id, so the pair separates two careers captured in one session and
    joins one career split across several sessions.
    """
    st = ci.get("start_time") or os.path.basename(session_dir)
    return "%s@%s" % (ci.get("single_mode_chara_id"), st)


def extract_session(session_dir, stats):
    """career_key -> career record for one capture session."""
    careers = {}
    for turn, fds, ci in _snapshots(session_dir, stats):
        shop_id = _int(fds.get("shop_id"))
        items = fds.get("pick_up_item_info_array") or []
        if not isinstance(items, list):
            stats["bad_item_array"] += 1
            items = []

        key = career_key(ci, session_dir)
        career = careers.get(key)
        if career is None:
            career = careers[key] = {
                "career": key,
                "session": os.path.basename(session_dir),
                "single_mode_chara_id": _int(ci.get("single_mode_chara_id")),
                "start_time": ci.get("start_time"),
                "card_id": _int(ci.get("card_id")),
                "scenario_id": SCENARIO_ID_TRACKBLAZER,
                "turns_observed": [],
                "resets": {},
            }
        if turn not in career["turns_observed"]:
            career["turns_observed"].append(turn)

        if not shop_id or not items:
            continue  # a turn outside any shop window, or a shop not yet open

        rec = career["resets"].get(shop_id)
        if rec is None:
            start_turn, end_turn, group = SHOP_WINDOWS.get(shop_id, (None, None, None))
            rec = career["resets"][shop_id] = {
                "shop_id": shop_id,
                "start_turn": start_turn,
                "end_turn": end_turn,
                "lineup_group_id": group,
                "turns_seen": [],
                "base": [],
                "limited": [],
            }
        if turn not in rec["turns_seen"]:
            rec["turns_seen"].append(turn)

        _absorb_items(rec, items, turn, stats)
    return careers


def finalize(career):
    """Sort, and work out exactly what this career does and does not cover."""
    turns = sorted(career["turns_observed"])
    career["turns_observed"] = turns
    career["first_turn"] = turns[0] if turns else None
    career["last_turn"] = turns[-1] if turns else None
    turn_set = set(turns)

    for rec in career["resets"].values():
        rec["turns_seen"] = sorted(rec["turns_seen"])
        rec["base"].sort(key=lambda e: e["slot"])
        rec["limited"].sort(key=lambda e: (e["first_seen_turn"], e["slot"]))
        rec["base_size"] = len(rec["base"])
        rec["limited_size"] = len(rec["limited"])

        start, end = rec["start_turn"], rec["end_turn"]
        if start is None:
            rec["turns_missing"] = []
            rec["base_complete"] = False
            rec["limited_complete"] = False
            continue
        # A career can end (or a capture stop) before the window closes; only
        # turns up to the last one this career reached can be demanded.
        last = career["last_turn"]
        expected = [t for t in range(start, end + 1) if t <= last]
        rec["turns_missing"] = [t for t in expected if t not in rec["turns_seen"]]
        rec["window_complete"] = (end <= last)
        # Base stock is fixed for the window, so one snapshot is enough.
        rec["base_complete"] = bool(rec["turns_seen"])
        # A limited offer only shows on the turns it is live, so any unobserved
        # turn inside the window means one could have been missed.
        rec["limited_complete"] = (not rec["turns_missing"]
                                   and bool(rec["turns_seen"])
                                   and rec["turns_seen"][0] <= start)

    seen_ids = sorted(career["resets"])
    career["resets_seen"] = seen_ids
    last = career["last_turn"]
    missing, not_reached = [], []
    for shop_id, (start, end, _g) in sorted(SHOP_WINDOWS.items()):
        if shop_id in career["resets"]:
            continue
        if last is None or start > last:
            not_reached.append(shop_id)
        elif turn_set and any(start <= t <= end for t in turn_set):
            # We were observing during this window and still got nothing --
            # a genuine hole, not simply a career that stopped short.
            missing.append(shop_id)
        else:
            not_reached.append(shop_id)
    career["resets_missing"] = missing
    career["resets_not_reached"] = not_reached
    return career


def _career_rows(career):
    """The career flattened to one row per reset, for --format jsonl."""
    shared = dict((k, career[k]) for k in
                  ("career", "session", "single_mode_chara_id", "start_time",
                   "card_id", "scenario_id", "first_turn", "last_turn",
                   "resets_seen", "resets_missing", "resets_not_reached"))
    for shop_id in sorted(career["resets"]):
        row = dict(shared)
        row.update(career["resets"][shop_id])
        yield row


def _load_existing(path):
    """Existing dataset -> {career_key: career record}. Reads either format."""
    careers = {}
    if not os.path.exists(path):
        return careers
    with io.open(path, encoding="utf-8") as fh:
        # Both formats start with '{', so sniff on content: a jsonl row is a
        # complete object on its own line and carries shop_id, while a document
        # is either pretty-printed (first line won't parse) or one big object
        # with no shop_id at the top level.
        first = fh.readline()
        try:
            probe = json.loads(first)
        except ValueError:
            probe = None
        fh.seek(0)
        if not (isinstance(probe, dict) and "shop_id" in probe):
            doc = json.load(fh)
            for c in doc.get("careers", []):
                c["resets"] = dict((int(k), v) for k, v in c.get("resets", {}).items())
                careers[c["career"]] = c
        else:  # jsonl: one row per reset, regrouped by career
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                c = careers.setdefault(row["career"], {
                    "career": row["career"],
                    "session": row.get("session"),
                    "single_mode_chara_id": row.get("single_mode_chara_id"),
                    "start_time": row.get("start_time"),
                    "card_id": row.get("card_id"),
                    "scenario_id": row.get("scenario_id"),
                    "turns_observed": [],
                    "first_turn": row.get("first_turn"),
                    "last_turn": row.get("last_turn"),
                    "resets_seen": row.get("resets_seen", []),
                    "resets_missing": row.get("resets_missing", []),
                    "resets_not_reached": row.get("resets_not_reached", []),
                    "resets": {},
                })
                c["resets"][row["shop_id"]] = row
    return careers


def cmd_extract(args):
    out_path = args.out or DEFAULT_OUT
    # Relative paths resolve against the working directory, so the default
    # lands the dataset wherever the command was run from.
    out_path = os.path.abspath(out_path)
    # Settle the extension BEFORE reading the existing dataset, or --append
    # with --format jsonl would look for the .json name and find nothing.
    if args.format == "jsonl" and out_path.endswith(".json"):
        out_path += "l"

    stats = {"snapshots": 0, "other_scenario": 0, "unreadable": 0,
             "no_turn": 0, "bad_item_array": 0, "bad_item_row": 0,
             "no_shop_item_id": 0, "other_scenario_ids": Counter(),
             "coerced": Counter(), "uncoercible": Counter()}
    careers = _load_existing(out_path) if args.append else {}
    known_before = set(careers)

    def _found(found):
        """Merge one reader's careers into the accumulating dataset."""
        for key, career in found.items():
            career = finalize(career)
            old = careers.get(key)
            if old:
                # Same career seen in another batch: merge reset-wise, keeping
                # whichever observation covered more turns.
                for shop_id, rec in career["resets"].items():
                    prev = old["resets"].get(shop_id)
                    if not prev or len(rec["turns_seen"]) > len(prev.get("turns_seen") or []):
                        old["resets"][shop_id] = rec
                old["turns_observed"] = sorted(
                    set(old.get("turns_observed") or []) | set(career["turns_observed"]))
                careers[key] = finalize(old)
            else:
                careers[key] = career

    sessions = 0
    for session_dir in iter_capture_dirs(args.paths):
        sessions += 1
        _found(extract_session(session_dir, stats))
    logs = 0
    for log_path in iter_career_logs(args.paths):
        logs += 1
        _found(extract_career_log(log_path, stats))

    ordered = [careers[k] for k in sorted(careers)]
    if args.format == "jsonl":
        with io.open(out_path, "w", encoding="utf-8") as fh:
            for career in ordered:
                for row in _career_rows(career):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        doc = {
            "scenario_id": SCENARIO_ID_TRACKBLAZER,
            "scenario": "Trackblazer: Start of the Climax",
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "shop_windows": dict(
                (str(k), {"start_turn": v[0], "end_turn": v[1],
                          "lineup_group_id": v[2]})
                for k, v in sorted(SHOP_WINDOWS.items())),
            "career_count": len(ordered),
            "careers": ordered,
        }
        with io.open(out_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2 if args.indent else None)

    resets = sum(len(c["resets"]) for c in ordered)
    if sessions:
        print("capture sessions    : %d" % sessions)
    if logs:
        print("career logs         : %d" % logs)
    if not sessions and not logs:
        print("no capture sessions or career logs found under: %s"
              % ", ".join(args.paths))
    print("trackblazer snapshots: %d" % stats["snapshots"])
    if stats["other_scenario"]:
        print("skipped (not scenario 4): %d  %s"
              % (stats["other_scenario"], dict(stats["other_scenario_ids"])))
    if stats["unreadable"]:
        print("unreadable files    : %d" % stats["unreadable"])
    for key, label in (("no_turn", "snapshots with no usable turn"),
                       ("bad_item_array", "pick_up_item_info_array not a list"),
                       ("bad_item_row", "item rows that were not objects"),
                       ("no_shop_item_id", "item rows with no shop_item_id")):
        if stats[key]:
            print("WARNING: %s: %d" % (label, stats[key]))
    if stats["coerced"]:
        # Not fatal -- the values were recovered -- but it means the capture
        # source is not typing fields the way the live server does, which is
        # worth knowing before trusting a batch.
        print("note: type-coerced fields: %s" % dict(stats["coerced"]))
    if stats["uncoercible"]:
        print("WARNING: fields that could not be read as numbers: %s"
              % dict(stats["uncoercible"]))
    print("careers             : %d (%d new)"
          % (len(ordered), len(ordered) - len(known_before)))
    print("reset records       : %d" % resets)
    print("wrote               : %s" % out_path)

    per_shop = Counter()
    full = Counter()
    n_base_items = n_ltd_items = 0
    for c in ordered:
        for shop_id, rec in c["resets"].items():
            per_shop[shop_id] += 1
            n_base_items += rec.get("base_size") or 0
            n_ltd_items += rec.get("limited_size") or 0
            if rec.get("limited_complete"):
                full[shop_id] += 1
    # Base stock is the bulk of every real lineup. Zero of it, or limited
    # offers outnumbering it, means the limit_turn field is not being read the
    # way this tool expects -- exactly the shape a schema change would take,
    # and otherwise a silent corruption that still prints a healthy summary.
    if resets and not n_base_items:
        print("WARNING: not a single base item was recorded across %d resets."
              " Check that pick_up_item_info rows still carry limit_turn=0"
              " for base stock." % resets)
    elif resets >= 10 and not n_ltd_items:
        # One career can legitimately be offered nothing (reset 1 never was in
        # the reference capture), but across a batch this size some window
        # should have produced a limited offer. None at all suggests limit_turn
        # has been renamed, so every offer is being filed as base stock.
        print("WARNING: no limited offers found in any of %d resets. If the"
              " capture is sound, check whether the limit_turn field was"
              " renamed." % resets)
    elif n_ltd_items > n_base_items:
        print("WARNING: limited offers (%d) outnumber base stock (%d), which"
              " is not how the shop behaves. Suspect the limit_turn field."
              % (n_ltd_items, n_base_items))
    for shop_id in sorted(SHOP_WINDOWS):
        print("  reset %2d (t%2d-%2d): %6d careers, %6d with every turn covered"
              % (shop_id, SHOP_WINDOWS[shop_id][0], SHOP_WINDOWS[shop_id][1],
                 per_shop[shop_id], full[shop_id]))


# ------------------------------------------------------------------ report --
# A plain tally over the dataset, per reset, never pooled across resets. The
# dataset is the deliverable; this is just a look at it.

def load_names():
    """item_id -> display name, or {} if master.mdb is not around.

    Display sugar ONLY -- every number in the report is computed from item IDs,
    so the tool is unchanged without it. text_data category 225 holds the
    Trackblazer shop item names.
    """
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
        from app import master_data
        rows = master_data.query(
            "SELECT i.item_id, t.text FROM single_mode_free_shop_item i"
            " LEFT JOIN text_data t ON t.category=225 AND t.[index]=i.item_id")
        return dict((r["item_id"], r["text"]) for r in rows if r["text"])
    except Exception:
        return {}


def chi2_sf(x, df):
    """Upper tail of the chi-square distribution -- the p-value.

    Hand-rolled (regularised incomplete gamma, series below the crossover and
    continued fraction above) because this tool has no third-party deps and
    scipy is not installed here.
    """
    if df <= 0 or x <= 0:
        return 1.0
    a, xx = df / 2.0, x / 2.0
    if xx < a + 1:
        total = term = 1.0 / a
        n = a
        for _ in range(1000):
            n += 1
            term *= xx / n
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return max(0.0, 1.0 - total * math.exp(-xx + a * math.log(xx) - math.lgamma(a)))
    b, c, d = xx + 1 - a, 1e300, 1.0 / (xx + 1 - a)
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return min(1.0, math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h)


def uniformity(counts):
    """(chi2, p, df) for 'these variants are drawn equally often'.

    None when the sample is too thin for the test to mean anything (the usual
    rule of thumb, an expected count of at least 5 per cell).
    """
    n, k = sum(counts), len(counts)
    if k < 2 or n < 5 * k:
        return None
    exp = n / float(k)
    chi = sum((o - exp) ** 2 / exp for o in counts)
    return (chi, chi2_sf(chi, k - 1), k - 1)


def wilson(k, n, z=1.96):
    """95% confidence interval on a rate. With a few thousand careers the
    interval is what says whether two items are drawn at genuinely different
    weights or just landed differently."""
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _group_rows(rows, fam_careers, fam_copies, per_item, n, names):
    """Collapse family members in `rows` into one row each, leaving every other
    item untouched.

    A family row reports the family as a single outcome AND the per-variant
    figure the family implies, which is the point of grouping: the per-variant
    estimate is pooled over every member, so its effective sample is n * k
    rather than n. It also carries the uniformity test, so the assumption that
    licensed the pooling stays visible next to the number it produced.
    """
    out, seen = [], set()
    for row in rows:
        fam = _FAMILY_OF.get(row["item_id"])
        if fam is None:
            out.append(row)
            continue
        if fam in seen:
            continue
        seen.add(fam)
        members = ITEM_FAMILIES[fam]
        k = len(members)
        careers = fam_careers.get(fam, 0)
        lo, hi = wilson(careers, n)
        # Pooled per-variant rate: every member contributes its own careers, over
        # a denominator of n careers x k members.
        v_hits = sum(per_item.get(i, 0) for i in members)
        vlo, vhi = wilson(v_hits, n * k)
        variant_counts = [per_item.get(i, 0) for i in members]
        u = uniformity(variant_counts)
        grouped = {
            "item_id": None,
            "family": fam,
            "name": "%s (any of %d)" % (fam, k),
            "members": members,
            "member_names": [names.get(i) for i in members] if names else None,
            "careers": careers,
            "rate": round(careers / n, 5) if n else None,
            "ci95": [round(lo, 5), round(hi, 5)],
            "variant_rate": round(v_hits / (n * k), 5) if n else None,
            "variant_ci95": [round(vlo, 5), round(vhi, 5)],
            "variant_careers": dict(zip(members, variant_counts)),
            "uniformity": ({"chi2": round(u[0], 3), "p": round(u[1], 4), "df": u[2]}
                           if u else None),
        }
        if fam_copies is not None:
            total = fam_copies.get(fam, 0)
            grouped["copies"] = total
            grouped["mean_copies"] = round(total / n, 4) if n else None
            grouped["per_variant_mean_copies"] = (
                round(total / (n * k), 4) if n else None)
            grouped["careers_with_duplicate"] = None
        out.append(grouped)
    out.sort(key=lambda r: (-(r["rate"] or 0), str(r.get("family") or r["item_id"])))
    return out


def _print_family_detail(it):
    """The per-variant number the grouping buys, plus the test that justifies
    it. Printed under the family row so the assumption is never invisible."""
    u = it.get("uniformity")
    if u is None:
        verdict = "too few to test"
    elif u["p"] < 0.01:
        verdict = "UNEQUAL p=%.4f -- do not pool" % u["p"]
    elif u["p"] < 0.05:
        verdict = "borderline p=%.4f" % u["p"]
    else:
        verdict = "equal-chance holds p=%.2f" % u["p"]
    print("           per variant %.4f (%.3f-%.3f)   %s   %s"
          % (it["variant_rate"], it["variant_ci95"][0], it["variant_ci95"][1],
             list(it["variant_careers"].values()), verdict))


def cmd_report(args):
    careers = _load_existing(os.path.abspath(args.dataset))
    names = load_names() if args.names else {}
    n_base = Counter()
    n_full = Counter()
    item = defaultdict(Counter)     # shop_id -> item_id -> careers
    copies = defaultdict(Counter)   # shop_id -> item_id -> total copies
    dupes = defaultdict(Counter)
    sizes = defaultdict(Counter)
    ltd = defaultdict(Counter)
    ltd_n = defaultdict(Counter)
    # Family aggregates need the UNION over members, which cannot be recovered
    # by summing per-item counts (one career can hold two members), so they are
    # tallied alongside rather than derived afterwards.
    fam_careers = defaultdict(Counter)   # shop_id -> family -> careers with any member
    fam_copies = defaultdict(Counter)    # shop_id -> family -> total member copies
    fam_ltd = defaultdict(Counter)

    for c in careers.values():
        for shop_id, rec in c["resets"].items():
            shop_id = int(shop_id)
            if not rec.get("base_complete"):
                continue
            n_base[shop_id] += 1
            sizes[shop_id][len(rec["base"])] += 1
            per = Counter(e["item_id"] for e in rec["base"])
            for item_id, k in per.items():
                item[shop_id][item_id] += 1
                copies[shop_id][item_id] += k
                if k > 1:
                    dupes[shop_id][item_id] += 1
            fam_here = Counter()
            for item_id, k in per.items():
                fam = _FAMILY_OF.get(item_id)
                if fam:
                    fam_here[fam] += k
            for fam, k in fam_here.items():
                fam_careers[shop_id][fam] += 1
                fam_copies[shop_id][fam] += k
            if rec.get("limited_complete"):
                n_full[shop_id] += 1
                ltd_n[shop_id][len(rec["limited"])] += 1
                for item_id in set(e["item_id"] for e in rec["limited"]):
                    ltd[shop_id][item_id] += 1
                for fam in set(_FAMILY_OF[i] for i in
                               set(e["item_id"] for e in rec["limited"])
                               if i in _FAMILY_OF):
                    fam_ltd[shop_id][fam] += 1

    out = {}
    for shop_id in sorted(SHOP_WINDOWS):
        n, nf = n_base[shop_id], n_full[shop_id]
        start, end, group = SHOP_WINDOWS[shop_id]
        entry = {"shop_id": shop_id, "turns": [start, end],
                 "lineup_group_id": group, "careers": n,
                 "careers_full_coverage": nf,
                 "lineup_size": dict(sorted(sizes[shop_id].items())),
                 "items": [], "limited": [],
                 "limited_offer_count": dict(sorted(ltd_n[shop_id].items()))}
        for item_id, k in item[shop_id].most_common():
            lo, hi = wilson(k, n)
            entry["items"].append({
                "item_id": item_id, "name": names.get(item_id), "careers": k,
                "rate": round(k / n, 5) if n else None,
                "ci95": [round(lo, 5), round(hi, 5)],
                "copies": copies[shop_id][item_id],
                "mean_copies": round(copies[shop_id][item_id] / n, 4) if n else None,
                "careers_with_duplicate": dupes[shop_id][item_id]})
        for item_id, k in ltd[shop_id].most_common():
            lo, hi = wilson(k, nf)
            entry["limited"].append({
                "item_id": item_id, "name": names.get(item_id), "careers": k,
                "rate": round(k / nf, 5) if nf else None,
                "ci95": [round(lo, 5), round(hi, 5)]})

        if args.group:
            entry["items"] = _group_rows(
                entry["items"], fam_careers[shop_id], fam_copies[shop_id],
                item[shop_id], n, names)
            entry["limited"] = _group_rows(
                entry["limited"], fam_ltd[shop_id], None,
                ltd[shop_id], nf, names)

        out[shop_id] = entry

    if args.json:
        path = os.path.abspath(args.json)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("wrote %s" % path)

    if args.csv:
        # One flat row per (reset, kind, item) so the whole thing drops into a
        # spreadsheet without any reshaping. `kind` keeps base stock and
        # limited offers distinguishable -- they have different denominators.
        path = os.path.abspath(args.csv)
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["reset", "start_turn", "end_turn", "lineup_group",
                        "kind", "item_id", "family", "members", "name",
                        "careers", "denominator", "rate", "ci95_low",
                        "ci95_high", "variant_rate", "variant_ci95_low",
                        "variant_ci95_high", "uniformity_p", "copies",
                        "mean_copies", "careers_with_duplicate"])
            for shop_id in sorted(out):
                e = out[shop_id]
                for kind, rows_, denom in (("base", e["items"], e["careers"]),
                                           ("limited", e["limited"],
                                            e["careers_full_coverage"])):
                    for it in rows_:
                        u = it.get("uniformity") or {}
                        w.writerow([shop_id, e["turns"][0], e["turns"][1],
                                    e["lineup_group_id"], kind,
                                    it["item_id"] if it["item_id"] is not None else "",
                                    it.get("family") or "",
                                    len(it["members"]) if it.get("members") else "",
                                    it.get("name") or "", it["careers"], denom,
                                    it["rate"], it["ci95"][0], it["ci95"][1],
                                    it.get("variant_rate", ""),
                                    (it.get("variant_ci95") or ["", ""])[0],
                                    (it.get("variant_ci95") or ["", ""])[1],
                                    u.get("p", ""),
                                    it.get("copies", ""), it.get("mean_copies", ""),
                                    it.get("careers_with_duplicate")
                                    if it.get("careers_with_duplicate") is not None
                                    else ""])
        print("wrote %s" % path)

    for shop_id in sorted(out):
        e = out[shop_id]
        print()
        print("=" * 70)
        print("RESET %d  turns %d-%d  lineup_group %d  careers %d (full coverage %d)"
              % (shop_id, e["turns"][0], e["turns"][1], e["lineup_group_id"],
                 e["careers"], e["careers_full_coverage"]))
        print("=" * 70)
        if not e["careers"]:
            print("  no observations")
            continue
        print("  base lineup size: %s"
              % ", ".join("%sx%s" % (k, v) for k, v in e["lineup_size"].items()))
        print("  %-8s %-28s %8s %17s %11s %6s"
              % ("item_id", "name", "rate", "95% CI", "copies/run", "dupes"))
        for it in e["items"]:
            dup = it.get("careers_with_duplicate")
            print("  %-8s %-28s %8.4f %8.3f-%-8.3f %11.3f %6s"
                  % (it["item_id"] if it["item_id"] is not None else "family",
                     (it.get("name") or "")[:28], it["rate"],
                     it["ci95"][0], it["ci95"][1],
                     it["mean_copies"], "-" if dup is None else dup))
            if it.get("family"):
                _print_family_detail(it)
        if e["limited"]:
            print("  -- limited offers (denominator %d fully covered careers) --"
                  % e["careers_full_coverage"])
            for it in e["limited"]:
                print("  %-8s %-28s %8.4f %8.3f-%-8.3f"
                      % (it["item_id"] if it["item_id"] is not None else "family",
                         (it.get("name") or "")[:28], it["rate"],
                         it["ci95"][0], it["ci95"][1]))
                if it.get("family"):
                    _print_family_detail(it)
            print("  offers per career: %s" % e["limited_offer_count"])
        elif e["careers_full_coverage"]:
            print("  -- no limited offers observed --")
        else:
            print("  -- limited offers: no career covered every turn of the window --")


def main():
    ap = argparse.ArgumentParser(
        description="Trackblazer (scenario 4) shop lineup extractor")
    sub = ap.add_subparsers(dest="cmd")

    ex = sub.add_parser("extract", help="pull shop lineups out of capture dirs")
    ex.add_argument("paths", nargs="+",
                    help="capture session dirs, or any parent of them")
    ex.add_argument("-o", "--out", default=None,
                    help="output path (default ./%s, in the working directory)"
                         % DEFAULT_OUT)
    ex.add_argument("--format", choices=("json", "jsonl"), default="json",
                    help="one document (default) or one line per reset")
    ex.add_argument("--append", action="store_true",
                    help="merge into an existing dataset instead of replacing it")
    ex.add_argument("--indent", action="store_true", default=True)
    ex.add_argument("--compact", dest="indent", action="store_false",
                    help="no pretty-printing (much smaller at scale)")
    ex.set_defaults(func=cmd_extract)

    rp = sub.add_parser("report", help="tally rates over a dataset, per reset")
    rp.add_argument("dataset", nargs="?", default=DEFAULT_OUT)
    rp.add_argument("--json", help="also write the tally as JSON")
    rp.add_argument("--csv", help="also write a flat one-row-per-item CSV")
    rp.add_argument("--group", action="store_true",
                    help="merge interchangeable item families (the five stat"
                         " notepads, etc) into one row, with a chi-square check"
                         " that they really are drawn equally")
    rp.add_argument("--names", action="store_true",
                    help="label items using master.mdb if it is installed"
                         " (display only; ignored if absent)")
    rp.set_defaults(func=cmd_report)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
