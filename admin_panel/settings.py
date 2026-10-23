
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE = Path(os.environ.get("UMA_PANEL_SETTINGS") or Path(__file__).resolve().parent / "settings.json")
REGIONS = ("global", "jp")
GAME = {"global": "UmamusumePrettyDerby", "jp": "UmamusumePrettyDerby_Jpn"}
DEFAULT = {"game_install": True,
           "regions": {r: {"enabled": True, "game_dir": "", "master": ""} for r in REGIONS}}

_BASE_ENV = {k: os.environ.get(k) for k in
             [f"MASTER_MDB_PATH_{r.upper()}" for r in REGIONS] + [f"UMA_GAME_DIR_{r.upper()}" for r in REGIONS]
             + ["UMA_GLOBAL_PERSISTENT", "UMA_JP_PERSISTENT", "UMA_EN_TEXT_MDB", "UMA_NO_GAME_INSTALL"]}


def load() -> dict:
    s = json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return s
    s["game_install"] = bool(raw.get("game_install", True))
    for r in REGIONS:
        got = (raw.get("regions") or {}).get(r) or {}
        s["regions"][r] = {"enabled": bool(got.get("enabled", True)),
                           "game_dir": str(got.get("game_dir") or "").strip(),
                           "master": str(got.get("master") or "").strip()}
    return s


def exists() -> bool:
    return FILE.is_file()


def save(s: dict) -> None:
    FILE.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")


def _steam_roots() -> list[Path]:
    home = Path.home()
    wine = Path(os.environ.get("UMA_WINEPREFIX", home / "Games/steam")) / "drive_c/Program Files (x86)/Steam"
    return [home / ".local/share/Steam", home / ".steam/steam", Path("C:/Program Files (x86)/Steam"),
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam", wine]


def detect_game_dir(r: str) -> Path | None:
    for steam in _steam_roots():
        p = steam / "steamapps/common" / GAME[r]
        if (p / f"{GAME[r]}_Data").is_dir():
            return p
    return None


def master_in(game_dir: Path, r: str) -> Path:
    return game_dir / f"{GAME[r]}_Data/Persistent/master/master.mdb"


def detect_master(r: str, game_dir: Path | None = None) -> Path | None:
    if r == "global" and (ROOT / "master.mdb").is_file():
        return ROOT / "master.mdb"
    d = game_dir or detect_game_dir(r)
    if d and master_in(d, r).is_file():
        return master_in(d, r)
    return None


def check_master(path: str | Path, want: str | None = None) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "error": "File not found."}
    try:
        c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        tables = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        row = c.execute("SELECT text FROM text_data WHERE category=6 AND [index]=1001").fetchone()
        c.close()
    except sqlite3.DatabaseError as e:
        return {"ok": False, "error": f"Not a readable master.mdb ({e})."}
    if not row:
        return {"ok": False, "error": "No text_data: not a master.mdb."}
    region = "global" if row[0].isascii() else "jp"
    out = {"ok": True, "region": region, "tables": tables, "size": p.stat().st_size,
           "modified": int(p.stat().st_mtime), "sample": row[0]}
    if want and region != want:
        out.update(ok=False, error=f"This is the {'Global' if region == 'global' else 'JP'} master.")
    return out


def check_game_dir(path: str | Path, r: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"ok": False, "error": "Folder not found."}
    if not (p / f"{GAME[r]}_Data").is_dir():
        return {"ok": False, "error": f"No {GAME[r]}_Data folder here: not the {'Global' if r == 'global' else 'JP'} game."}
    m = master_in(p, r)
    return {"ok": True, "master": str(m) if m.is_file() else None, "hachimi": (p / "hachimi").is_dir()}


def resolve(s: dict | None = None) -> dict:
    s = s or load()
    out = {"game_install": s["game_install"], "regions": {}}
    for r in REGIONS:
        cfg = s["regions"][r]
        game_path = cfg["game_dir"] or _BASE_ENV.get(f"UMA_GAME_DIR_{r.upper()}")
        gd = Path(game_path).expanduser() if game_path else detect_game_dir(r)
        game = check_game_dir(gd, r) if gd and s["game_install"] else {"ok": False}
        master_path = cfg["master"] or _BASE_ENV.get(f"MASTER_MDB_PATH_{r.upper()}")
        if not master_path and r == os.environ.get("UMA_REGION", "global"):
            master_path = os.environ.get("MASTER_MDB_PATH")
        if master_path:
            master = Path(master_path).expanduser()
        elif s["game_install"]:
            master = detect_master(r, gd if game.get("ok") else None)
        else:
            master = ROOT / "master.mdb" if r == "global" and (ROOT / "master.mdb").is_file() else None
        mc = check_master(master, r) if master else {"ok": False, "error": "Not found."}
        out["regions"][r] = {
            "enabled": cfg["enabled"],
            "usable": cfg["enabled"] and mc["ok"],
            "master": str(master) if master else None, "master_check": mc,
            "master_auto": not cfg["master"],
            "game_dir": str(gd) if gd else None, "game_auto": not cfg["game_dir"],
            "game": bool(game.get("ok")), "game_check": game,
            "hachimi": bool(game.get("hachimi")),
        }
    regs = out["regions"]
    out["both_masters"] = regs["global"]["master_check"]["ok"] and regs["jp"]["master_check"]["ok"]
    out["usable"] = [r for r in REGIONS if regs[r]["usable"]]
    return out


def apply(s: dict | None = None) -> dict:
    eff = resolve(s)
    for k, v in _BASE_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for r, e in eff["regions"].items():
        if e["master_check"]["ok"]:
            os.environ[f"MASTER_MDB_PATH_{r.upper()}"] = e["master"]
        if e["game"]:
            os.environ[f"UMA_GAME_DIR_{r.upper()}"] = e["game_dir"]
            os.environ[f"UMA_{r.upper()}_PERSISTENT"] = str(Path(e["game_dir"]) / f"{GAME[r]}_Data/Persistent")
    if eff["regions"]["global"]["master_check"]["ok"] and not _BASE_ENV["UMA_EN_TEXT_MDB"]:
        os.environ["UMA_EN_TEXT_MDB"] = eff["regions"]["global"]["master"]
    if not eff["game_install"]:
        os.environ["UMA_NO_GAME_INSTALL"] = "1"
    return eff
