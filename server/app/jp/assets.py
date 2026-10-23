from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
import re

from .. import master_data

log = logging.getLogger("uma-server")
_META_KEY = "9c2bab97bcf8c0c4f1a9ea7881a213f6c9ebf9d8d4c6a8e43ce5a259bde7e9fd"


@functools.lru_cache(maxsize=4)
def _gacha_art(path: str, mtime: int | None, size: int | None) -> frozenset[int] | None:
    conn = None
    try:
        import apsw
        conn = apsw.Connection(path, flags=apsw.SQLITE_OPEN_READONLY)
        conn.execute(f"PRAGMA hexkey='{_META_KEY}'")
        normal, cursor = set(), set()
        for (name,) in conn.execute(
                "SELECT n FROM a WHERE n GLOB 'gachaselect/*/img_bnr_gacha_*' AND l > 0"):
            match = re.fullmatch(r"gachaselect/(\d+)/img_bnr_gacha_(\d+)(_cursor)?", name)
            if match and match[1] == match[2]:
                (cursor if match[3] else normal).add(int(match[1]))
        return frozenset(normal & cursor)
    except Exception as exc:
        log.warning("JP banner art check unavailable for %s: %s. Set UMA_ASSET_META_PATH "
                    "to the client's meta to enable it; keeping the server listing.", path, exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def filter_gacha_index(data: dict) -> None:
    path = Path(os.environ.get("UMA_ASSET_META_PATH") or
                master_data.MASTER_MDB_PATH.parent.parent / "meta").expanduser().resolve()
    try:
        stat = path.stat()
        stamp, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        stamp = size = None
    available = _gacha_art(str(path), stamp, size)
    if available is None:
        return
    rows = data.get("gacha_info_list") or []
    missing = [g["id"] for g in rows if g["id"] not in available]
    if missing:
        log.warning("JP gacha omitted banners with no indexed thumbnail/cursor art: %s. "
                    "Update the client's matching meta and assets to display these banners.", missing)
        data["gacha_info_list"] = [g for g in rows if g["id"] in available]
