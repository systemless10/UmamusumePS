from __future__ import annotations

import gzip
import json
import logging
import re
import struct
import threading
from pathlib import Path

from . import custom_support as C

log = logging.getLogger("uma-server")
MAGIC = b"UMAPACK1"
STEM = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_lock = threading.Lock()


def _settings_path() -> Path:
    return C.root() / "umapack_sync.json"


def settings() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_settings(s: dict) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=1))


def read_pack(path: Path) -> tuple[dict, dict]:
    with open(path, "rb") as fh:
        if fh.read(8) != MAGIC:
            raise ValueError("not a umapack")
        h = json.loads(fh.read(struct.unpack("<I", fh.read(4))[0]))
        d = h.get("data")
        if not d:
            return h, {}
        fh.seek(d["offset"])
        raw = fh.read(d["length"])
    if len(raw) != d["length"]:
        raise ValueError("pack is truncated")
    bundles = json.loads(gzip.decompress(raw))
    if not isinstance(bundles, dict):
        raise ValueError("bad data section")
    return h, bundles


def _check(stem: str, b) -> str | None:
    if not STEM.match(stem):
        return "bad bundle name"
    if not isinstance(b, dict) or not isinstance(b.get("tables", {}), dict):
        return "bad bundle"
    if not any(k in b for k in ("chara_id", "support_cards", "scenario_id", "tables")):
        return "bad bundle"
    return None


def _label(stem: str, b: dict) -> str:
    if "chara_id" in b:
        return f"trainee {b['chara_id']}"
    if "scenario_id" in b:
        return f"scenario {b['scenario_id']}"
    if b.get("support_cards"):
        return f"support cards {', '.join(map(str, b['support_cards']))}"
    return stem


def _fingerprint(folder: Path) -> list:
    return sorted([p.name, p.stat().st_size, int(p.stat().st_mtime)] for p in folder.glob("*.umapack"))


def sync(folder: str | Path) -> dict:
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"No folder at {folder}")
    with _lock:
        d = C.root() / "unreleased"
        have = {p.stem for p in d.glob("*.json")} | {p.stem for p in (d / "archived").glob("*.json")}
        report = {"folder": str(folder), "packs": [], "added": [], "errors": [], "digest": None}
        new = {}
        for p in sorted(folder.glob("*.umapack")):
            try:
                _h, bundles = read_pack(p)
            except (OSError, ValueError, struct.error) as e:
                report["errors"].append(f"{p.name}: {e}")
                continue
            report["packs"].append({"name": p.name, "bundles": len(bundles)})
            for stem, b in bundles.items():
                if stem in have or stem in new:
                    continue
                why = _check(stem, b)
                if why:
                    report["errors"].append(f"{p.name}: {stem}: {why}")
                    continue
                new[stem] = (p.name, b)
        written = []
        if new:
            d.mkdir(parents=True, exist_ok=True)
            try:
                for stem, (_pack, b) in new.items():
                    f = d / f"{stem}.json"
                    f.write_text(json.dumps(b, ensure_ascii=False, separators=(",", ":")))
                    written.append(f)
                m = C.publish()
                if not C.is_enabled():
                    C.set_enabled(True)
                report["digest"] = m["digest"]
            except Exception as e:
                for f in written:
                    f.unlink(missing_ok=True)
                log.warning("umapack sync: publish failed, nothing added: %s", e)
                raise ValueError(f"Could not add the pack content ({e}); nothing was changed") from e
            report["added"] = [{"stem": s, "pack": pk, "what": _label(s, b)} for s, (pk, b) in new.items()]
            log.info("umapack sync: added %s from %s", sorted(new), folder)
        s = settings()
        s.update(folder=str(folder), fingerprint=_fingerprint(folder))
        _save_settings(s)
        return report


def auto_sync() -> None:
    s = settings()
    folder = Path(s["folder"]) if s.get("folder") else None
    if not folder or not folder.is_dir():
        return
    try:
        if _fingerprint(folder) == s.get("fingerprint"):
            return
        sync(folder)
    except Exception as e:
        log.warning("umapack auto-sync of %s failed: %s", folder, e)
        s["fingerprint"] = _fingerprint(folder)
        _save_settings(s)
