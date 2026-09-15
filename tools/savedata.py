#!/usr/bin/env python3
"""
Reader/decoder for the client's local SaveData.db.

WHAT THIS FILE IS
-----------------
`<game>/UmamusumePrettyDerby_Data/Persistent/d/SaveData.db` is the CLIENT's own
local key/value store. It is **not** something the server sends -- the server's
responses are what the client writes into it. (server/README.md calls it an
"encrypted SQLite cache"; that is wrong and was corrected 2026-08-11.)

Format, established by analysis of two real saves (a fresh Linux install, 27
rows, and the real Windows account, 119 rows):

    plain SQLite3, one table:
        CREATE TABLE `AppSetting` (save_key BLOB PRIMARY KEY, save_value BLOB)

    Both blobs are obfuscated with a **repeating-XOR keystream**, period 44.
    Values are plain C# ToString() output: "True", "False", integers,
    comma-separated arrays.

KEY STATUS -- read this before trusting output
----------------------------------------------
* Bytes 0-8 (`433335d3050286b332`) are EXACT. Derived by known-plaintext from
  two independent cribs that agree byte-for-byte: `udid` (4B row) and
  `viewer_id` (9B row).
* Bytes 9-43 are BEST-EFFORT, recovered by frequency analysis. Positions with
  few samples (only long field names reach them) are sometimes off, which shows
  up as garbled tails on the longest names. Short/medium names decode cleanly.
* Because the only observed corruption is a +-0x20 bit flip, `_repair` recovers
  most of it: valid name chars are [a-z0-9_], and the flipped form of a valid
  char is never itself valid, so the correct case is unambiguous per byte.

=> READING is reliable for short/medium keys and for all short values.
=> WRITING is deliberately NOT implemented. A wrong key byte would silently
   corrupt the client's save. Finish the key first (see "To finish" below).

To finish the key: collect more saves (every extra account adds samples at the
high positions), or lift the routine out of the binary with the Ghidra + SCY
memory-dump workflow already documented in handoff.md.

USAGE
-----
    python3 tools/savedata.py [path/to/SaveData.db]

Defaults to the Linux Steam install path.
"""

from __future__ import annotations

import os
import sqlite3
import sys

# save_key and save_value use DIFFERENT period-44 keystreams.
# KEY  bytes 0-8  exact (cribs `udid` + `viewer_id`, which agree byte-for-byte)
# VKEY bytes 0-4  exact (cribs "True" / "False")
# the remaining bytes of each are best-effort frequency analysis.
KEY = bytes.fromhex(
    "433335d3050286b3321241190a4a930151c11151d1828a91024b468343d21850"
    "d390461142c146905001c502"
)
VKEY = bytes.fromhex(
    "175b2a4818d3e29fccea2b3551278533dc11ba6d6109f2428756f90dbb141ab9"
    "3681fe4926da64973258bfcd"
)

DEFAULT_DB = os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby"
    "/UmamusumePrettyDerby_Data/Persistent/d/SaveData.db"
)

_NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyz0123456789_")


def _xor(blob: bytes, key: bytes = KEY) -> bytes:
    return bytes(c ^ key[i % len(key)] for i, c in enumerate(blob))


def _repair(raw: bytes) -> bytes:
    """Undo the single-bit (0x20) errors left by under-sampled key positions.
    Only applied when the flipped byte is a valid name char and the original
    is not -- so a correctly-decoded byte is never touched."""
    out = bytearray()
    for ch in raw:
        if ch not in _NAME_CHARS and (ch ^ 0x20) in _NAME_CHARS:
            ch ^= 0x20
        out.append(ch)
    return bytes(out)


def decode_key(blob: bytes) -> str:
    return _repair(_xor(blob)).decode("utf-8", "replace")


def decode_value(blob: bytes) -> str:
    # Values are not restricted to the name charset, so no repair pass -- a
    # non-printable result means this row runs past the reliable key prefix.
    return _xor(blob, VKEY).decode("utf-8", "replace")


def read(path: str = DEFAULT_DB) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = []
    for k, v in conn.execute("SELECT save_key, save_value FROM AppSetting"):
        if not isinstance(k, bytes):
            continue
        out.append((decode_key(k), decode_value(v) if isinstance(v, bytes) else str(v)))
    return sorted(out)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    if not os.path.exists(path):
        sys.exit(f"no such save: {path}")
    rows = read(path)
    print(f"{path}\n{len(rows)} rows\n")
    for name, value in rows:
        printable = all(32 <= ord(c) < 127 for c in value)
        shown = value if printable and len(value) <= 60 else (
            f"<{len(value)}B, not cleanly decoded>" if not printable else value[:60] + "..."
        )
        print(f"  {name:<52} = {shown}")


if __name__ == "__main__":
    main()
