#!/usr/bin/env python3
"""One-off cleanup: redact sensitive data out of every record under captures/.

See capture_redact.py for exactly what gets redacted and why. Safe to re-run
(already-redacted records are skipped) and safe to run on a tree that mixes
redacted and raw sessions.

Usage:
    tools/redact_captures.py            redact everything under captures/
    tools/redact_captures.py --dry-run  report what would change, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

from capture_redact import IdMapper, redact_record  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--captures-dir", default=os.path.join(_ROOT, "captures"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mapper = IdMapper()
    total = changed_count = skipped = errors = 0

    for dirpath, _dirnames, filenames in os.walk(args.captures_dir):
        for fname in sorted(filenames):
            if not fname.endswith(".json"):
                continue
            total += 1
            path = os.path.join(dirpath, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except Exception as exc:
                print(f"  !! {path}: failed to parse ({exc})")
                errors += 1
                continue

            changed = redact_record(rec, mapper)
            if not changed:
                skipped += 1
                continue

            changed_count += 1
            if args.dry_run:
                continue
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)

    mode = "would redact" if args.dry_run else "redacted"
    print(f"{total} files scanned, {mode} {changed_count}, "
          f"already clean {skipped}, unreadable {errors}")
    print(f"{mapper.mapped_count} distinct real viewer ids mapped to synthetic ids")


if __name__ == "__main__":
    main()
