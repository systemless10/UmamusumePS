
from __future__ import annotations

import json
import logging
import os
import threading
import tempfile

from ... import region
from pathlib import Path

log = logging.getLogger("uma-server")

_JSON_PATH = Path(__file__).resolve().parent / "data" / "skill_data.json"
_CACHE_PATH = Path(os.environ.get("UMA_RACE_SKILL_TABLE")
                   or Path(__file__).resolve().parents[3] / "data" / f"race_skill_table_{region.PROCESS_REGION}.json")

_table: dict | None = None
_lock = threading.Lock()


def _alternative(row, n: int) -> dict | None:
    condition = row[f"condition_{n}"] or ""
    if not condition:
        return None
    effects = []
    for j in (1, 2, 3):
        ability = row[f"ability_type_{n}_{j}"]
        if not ability:
            continue
        effects.append({
            "type": int(ability),
            "modifier": float(row[f"float_ability_value_{n}_{j}"]),
            "target": int(row[f"target_type_{n}_{j}"]),
            "targetValue": int(row[f"target_value_{n}_{j}"]),
            "valueUsage": int(row[f"ability_value_usage_{n}_{j}"]),
            "levelUsage": int(row[f"ability_value_level_usage_{n}_{j}"]),
            "additionalActivate": int(row[f"additional_activate_type_{n}_{j}"] or 0),
        })
    return {
        "precondition": row[f"precondition_{n}"] or "",
        "condition": condition,
        "baseDuration": float(row[f"float_ability_time_{n}"]),
        "timeUsage": int(row[f"ability_time_usage_{n}"]),
        "cooldown": float(row[f"float_cooldown_time_{n}"]),
        "effects": effects,
    }


def _from_master() -> dict:
    from ... import master_data
    out = {}
    for row in master_data.query("SELECT * FROM skill_data"):
        alts = [a for a in (_alternative(row, 1), _alternative(row, 2)) if a]
        if not alts:
            continue
        out[str(row["id"])] = {
            "rarity": int(row["rarity"]),
            "wisdomCheck": int(row["activate_lot"]),
            "tags": [int(t) for t in str(row["tag_id"] or "").split("/") if t.strip().isdigit()],
            "alternatives": alts,
        }
    return out


def build_table() -> dict:
    table = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
    try:
        master = _from_master()
    except Exception:
        log.warning("race engine: master.mdb skill_data unavailable, using the "
                    "bundled JP skill table", exc_info=True)
        return table
    table.update(master)
    return table


def skills() -> dict:
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = build_table()
    return _table


def ensure_file() -> Path:
    table = skills()
    encoded = json.dumps(table, separators=(",", ":"))
    with _lock:
        try:
            fresh = _CACHE_PATH.read_text(encoding="utf-8") == encoded
        except OSError:
            fresh = False
        if not fresh:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_CACHE_PATH.parent,
                                             prefix=_CACHE_PATH.name + ".", suffix=".tmp", delete=False) as f:
                temporary = Path(f.name)
                f.write(encoded)
            try:
                os.replace(temporary, _CACHE_PATH)
            finally:
                temporary.unlink(missing_ok=True)
    return _CACHE_PATH
