r"""Career-mode differential harness: replay a REAL capture session's exact
request sequence through OUR server's in-process request pipeline
(app.main._handle_decoded -- the same function main.py's HTTP route calls,
minus wire crypto) and structurally diff our response against what the real
Cygames server actually sent for that same request.

WHY THIS WORKS DESPITE RNG
---------------------------
Our server's training-gain / event RNG cannot reproduce the real server's
literal numbers -- there is no shared seed. So per-record VALUE equality on
things like stat deltas, mood rolls, race times is neither expected nor
diagnostic. What IS diagnostic and RNG-independent:
  * top-level and nested KEY PRESENCE/ABSENCE (schema shape)
  * key TYPE (int vs str vs list vs dict vs None)
  * array-of-object SHAPE (the set of keys each element has)
  * fields that are deterministic even though the response as a whole isn't
    (command availability lists, training partner arrays for a given turn,
    race program schedules, aptitude/talent fields, evaluation_info_array
    shape, disable flags, etc.)
Because we feed the REAL session's exact command/event choices back in, our
server's turn counter and playing_state should track the real session's
turn-for-turn even though internal stat state diverges numerically. So a
structural diff at "turn N, playing_state P" is meaningful even when the
leaf values differ.

USAGE
-----
    server/.venv/Scripts/python.exe tools/career_capture_diff.py <session_dir> [--out report.json]
    server/.venv/Scripts/python.exe tools/career_capture_diff.py --all-scenario3 --out report_all.json

Writes one JSON report per run: every record's structural diff, plus an
aggregated schema (path -> {seen_in_real, seen_in_ours, sample turns}) across
all endpoints of the session, so field-coverage gaps show up even where
individual-record diffing is drowned out by RNG noise.
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "server"))

os.environ.setdefault("UMA_LOG_LEVEL", "CRITICAL")

from app import region as region_mod          # noqa: E402
from app import main as app_main               # noqa: E402
from app import state as state_mod             # noqa: E402

# NOTE the ORDER: "single_mode/" is a prefix of every other family, so the more
# specific ones have to come first wherever this tuple is used for matching.
# single_mode_free/ (Trackblazer) was missing entirely, which is why that
# scenario was never diffed -- its 1,128 captured records simply never
# matched a prefix and the harness reported nothing to do.
CAREER_ENDPOINT_PREFIXES = (
    "single_mode_live/", "single_mode_team/", "single_mode_free/",
    "single_mode/", "pre_single_mode/",
)


def _load_session(session_dir: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(session_dir, "*.json")),
                    key=lambda p: os.path.basename(p))
    out = []
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if d.get("upstream") != "real":
            continue
        if "request" not in d or "response" not in d:
            continue
        d["_file"] = os.path.basename(f)
        out.append(d)
    out.sort(key=lambda d: d.get("seq", 0))
    return out


# ---------------------------------------------------------------------------
# structural diff
# ---------------------------------------------------------------------------

_NOISY_LEAF_KEYS = {
    # RNG / server-clock / identity fields that are EXPECTED to differ and
    # would otherwise drown every record in false positives.
    "servertime", "sid", "start_time", "create_time", "register_time",
    "viewer_id", "result_time", "chara_seed",
}


def _type_tag(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _list_shape(lst):
    """Union of keys across dict elements of a list, for array-of-object
    fields (e.g. command_info_array) -- so a per-index diff doesn't explode
    on ordinary reordering/count differences."""
    keys = set()
    scalar = False
    for el in lst:
        if isinstance(el, dict):
            keys |= set(el.keys())
        else:
            scalar = True
    return keys, scalar


def diff_tree(real, ours, path="$", out=None):
    """Yield dicts describing structural differences. Value-only leaf
    mismatches on known-noisy keys are skipped; everything else (missing/
    extra keys, type mismatches, array-of-object key-shape mismatches, and
    deterministic-looking leaf mismatches) is reported."""
    if out is None:
        out = []

    if isinstance(real, dict) and isinstance(ours, dict):
        rk, ok = set(real.keys()), set(ours.keys())
        for k in sorted(rk - ok):
            out.append({"path": f"{path}.{k}", "kind": "missing_in_ours",
                        "real_type": _type_tag(real[k]),
                        "real_sample": _shorten(real[k])})
        for k in sorted(ok - rk):
            out.append({"path": f"{path}.{k}", "kind": "extra_in_ours",
                        "ours_type": _type_tag(ours[k]),
                        "ours_sample": _shorten(ours[k])})
        for k in sorted(rk & ok):
            diff_tree(real[k], ours[k], f"{path}.{k}", out)
        return out

    if isinstance(real, list) and isinstance(ours, list):
        if len(real) == 0 and len(ours) == 0:
            return out
        rshape, rscalar = _list_shape(real)
        oshape, oscalar = _list_shape(ours)
        if rshape or oshape:
            missing = rshape - oshape
            extra = oshape - rshape
            if missing:
                out.append({"path": f"{path}[]", "kind": "array_elem_missing_keys",
                            "keys": sorted(missing)})
            if extra:
                out.append({"path": f"{path}[]", "kind": "array_elem_extra_keys",
                            "keys": sorted(extra)})
        elif rscalar or oscalar:
            pass  # scalar arrays: length differences are expected (RNG/counts), skip
        return out

    rtag, otag = _type_tag(real), _type_tag(ours)
    leaf_key = path.rsplit(".", 1)[-1].split("[")[0]
    if rtag != otag:
        out.append({"path": path, "kind": "type_mismatch",
                    "real_type": rtag, "ours_type": otag,
                    "real_sample": _shorten(real), "ours_sample": _shorten(ours)})
        return out
    if leaf_key in _NOISY_LEAF_KEYS:
        return out
    # Both dict-vs-list or list-vs-dict already caught above via type tag.
    return out


def _shorten(v, limit=120):
    s = repr(v)
    return s if len(s) <= limit else s[:limit] + "...>"


# ---------------------------------------------------------------------------
# schema aggregation (path -> seen-in-real / seen-in-ours)
# ---------------------------------------------------------------------------

def collect_paths(obj, path="$", into=None):
    if into is None:
        into = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            into.add(p)
            collect_paths(v, p, into)
    elif isinstance(obj, list):
        if obj:
            el = obj[0]
            collect_paths(el, f"{path}[]", into)
    return into


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def run_session(session_dir: str, region_name: str, verbose=False) -> dict:
    records = _load_session(session_dir)
    session_name = os.path.basename(os.path.normpath(session_dir))
    results = {
        "session": session_name,
        "region": region_name,
        "n_records": len(records),
        "record_results": [],
        "exceptions": [],
        "schema_real": set(),
        "schema_ours": set(),
    }

    with region_mod.use_region(region_name):
        for rec in records:
            endpoint = rec["endpoint"]
            payload = copy.deepcopy(rec["request"])
            real_resp = rec["response"]
            entry = {
                "file": rec["_file"], "seq": rec.get("seq"), "endpoint": endpoint,
            }
            # SEED real identity-bearing state before replaying single_mode
            # calls: the real account's succession roster / owned cards /
            # decks (load/index's `data.trained_chara` etc.) are NOT
            # reproducible by our own from-scratch handler on a fresh
            # harness viewer, so a bare replay spuriously fails handle_start's
            # (CORRECT, real-behavior-matching) "parent must be in this
            # viewer's own roster" check for every session that doesn't start
            # at true account creation -- which is all of them, since these
            # are long-lived real accounts. Overwriting the harness viewer's
            # load_index state with the REAL captured blob right when it's
            # seen keeps the rest of the replay faithful to what the real
            # client's downstream calls actually depended on.
            if endpoint == "load/index":
                real_data = (real_resp or {}).get("data")
                if isinstance(real_data, dict):
                    vid = payload["viewer_id"]
                    cur = state_mod.get_state(vid) or {}
                    cur["load_index"] = {"data": copy.deepcopy(real_data)}
                    state_mod.save_state(vid, cur)
            try:
                our_resp = app_main._handle_decoded(endpoint, payload, None)
            except Exception as exc:
                entry["exception"] = f"{type(exc).__name__}: {exc}"
                entry["traceback"] = traceback.format_exc()
                results["exceptions"].append(entry)
                results["record_results"].append(entry)
                if verbose:
                    print(f"  !! EXCEPTION {rec['_file']} {endpoint}: {exc}")
                continue

            entry["our_result_code"] = (our_resp.get("data_headers") or {}).get("result_code")
            entry["real_result_code"] = (real_resp.get("data_headers") or {}).get("result_code")
            if endpoint.startswith(CAREER_ENDPOINT_PREFIXES):
                real_data = (real_resp or {}).get("data") or {}
                our_data = (our_resp or {}).get("data") or {}
                diffs = diff_tree(real_data, our_data, "$")
                entry["n_diffs"] = len(diffs)
                entry["diffs"] = diffs
                results["schema_real"] |= collect_paths(real_data)
                results["schema_ours"] |= collect_paths(our_data)
                if verbose and diffs:
                    print(f"  {rec['_file']} {endpoint}: {len(diffs)} diffs")
            results["record_results"].append(entry)

    results["schema_real"] = sorted(results["schema_real"])
    results["schema_ours"] = sorted(results["schema_ours"])
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", nargs="?", help="captures/<session> folder")
    ap.add_argument("--all-scenario3", action="store_true",
                    help="run every known scenario-3 (Grand Live) real session")
    ap.add_argument("--out", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    sessions = []
    if args.all_scenario3:
        for name in ["20260816_140811", "20260816_144637", "20260821_165708",
                     "20260826_083820", "20260828_084012", "20260904_232507",
                     "20260907_204921"]:
            sessions.append(os.path.join(_ROOT, "captures", name))
    elif args.session_dir:
        sessions.append(args.session_dir)
    else:
        raise SystemExit("pass a session dir or --all-scenario3")

    all_results = []
    for i, sd in enumerate(sessions):
        region_name = f"harness{i}_{os.path.basename(os.path.normpath(sd))}"
        print(f"=== {sd} (region={region_name}) ===")
        res = run_session(sd, region_name, verbose=args.verbose)
        print(f"  {res['n_records']} records, {len(res['exceptions'])} exceptions, "
              f"{len(res['schema_real'])} real-schema-paths, {len(res['schema_ours'])} ours-schema-paths")
        all_results.append(res)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(all_results, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
