from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import threading
from functools import lru_cache
from pathlib import Path

from . import master_data as M

_APT = {"S": 8, "A": 7, "B": 6, "C": 5, "D": 4, "E": 3, "F": 2, "G": 1}
_STYLE_KEY = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
              3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}
_DISTANCE_KEY = (("proper_distance_short", 1400), ("proper_distance_mile", 1800),
                 ("proper_distance_middle", 2400), ("proper_distance_long", 99999))
_PROPER = ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
           "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
           "proper_running_style_sashi", "proper_running_style_oikomi",
           "proper_ground_turf", "proper_ground_dirt")
_lock = threading.Lock()


def corpus_root() -> Path:
    env = os.environ.get("UMA_CM_CORPUS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "captures" / "cm_teams"


def snapshots() -> list[dict]:
    root = corpus_root()
    try:
        index = json.loads((root / "index.json").read_text())
    except (OSError, ValueError):
        index = []
    out = []
    for e in index:
        m = re.match(r"cm(\d+)", str(e.get("cmId", "")))
        if m and (root / e["snapshot"] / "teams.jsonl.gz").exists():
            out.append({"snapshot": e["snapshot"], "cm": int(m.group(1)),
                        "teams": e.get("sampledTeams"), "course": e.get("course")})
    return sorted(out, key=lambda e: -e["cm"])


def resolve_snapshot(champions_id: int, wanted: str = "auto") -> str | None:
    snaps = snapshots()
    if wanted and wanted != "auto":
        return wanted if any(s["snapshot"] == wanted for s in snaps) else None
    same = [s for s in snaps if s["cm"] == int(champions_id)]
    pick = same or snaps
    return pick[0]["snapshot"] if pick else None


@lru_cache(maxsize=4)
def snapshot_course_keys(snapshot: str) -> tuple[str, str] | None:
    course = next((s["course"] for s in snapshots() if s["snapshot"] == snapshot), None) or ""
    m = re.search(r"([\d,]+)\s*m\s*-\s*(Turf|Dirt)", course)
    if not m:
        return None
    dist = int(m.group(1).replace(",", ""))
    return ("proper_ground_dirt" if m.group(2) == "Dirt" else "proper_ground_turf",
            next(k for k, top in _DISTANCE_KEY if dist <= top))


@lru_cache(maxsize=8)
def teams(snapshot: str) -> tuple:
    keys = snapshot_course_keys(snapshot)
    with _lock:
        path = corpus_root() / snapshot / "teams.jsonl.gz"
        rows = []
        with gzip.open(path, "rt") as fh:
            for line in fh:
                t = json.loads(line)
                if len(t.get("members") or []) == 3:
                    t["course_keys"] = keys
                    rows.append(t)
        return tuple(rows)


def team(snapshot: str, team_id: str) -> dict | None:
    return next((t for t in teams(snapshot) if t["id"] == team_id), None)


def pick(snapshot: str, band, rng: random.Random, n: int, exclude=(), pinned=()) -> list[dict]:
    lo, hi = band
    pool = teams(snapshot)
    chosen, seen = [], set(exclude)
    for tid in pinned:
        t = next((t for t in pool if t["id"] == tid), None)
        if t and t["id"] not in seen and lo <= t["_percentile"] <= hi and len(chosen) < n:
            chosen.append(t)
            seen.add(t["id"])
    widen = 0
    while len(chosen) < n:
        cands = [t for t in pool if t["id"] not in seen
                 and lo - widen <= t["_percentile"] <= hi + widen]
        if len(cands) >= n - len(chosen) or widen >= 100:
            for t in rng.sample(cands, min(len(cands), n - len(chosen))):
                chosen.append(t)
                seen.add(t["id"])
            break
        widen += 5
    return chosen


@M.context_cache(maxsize=4096)
def _card_proper(card_id: int) -> dict:
    row = M.query_one("SELECT * FROM card_rarity_data WHERE card_id=? ORDER BY rarity DESC LIMIT 1", (card_id,))
    if not row:
        return {k: 5 for k in _PROPER}
    return {k: row[k] for k in _PROPER}


@M.context_cache(maxsize=256)
def course_keys(race_instance_id: int) -> tuple[str, str]:
    row = M.query_one("SELECT rcs.distance, rcs.ground FROM race_instance ri JOIN race r ON r.id=ri.race_id "
                      "JOIN race_course_set rcs ON rcs.id=r.course_set WHERE ri.id=?", (race_instance_id,))
    if not row:
        return "proper_ground_turf", "proper_distance_middle"
    ground = "proper_ground_dirt" if row["ground"] == 2 else "proper_ground_turf"
    dist = next(k for k, top in _DISTANCE_KEY if row["distance"] <= top)
    return ground, dist


def _synthetic_id(team_id: str, idx: int) -> int:
    return 700_000_000 + int(hashlib.sha1(f"{team_id}:{idx}".encode()).hexdigest()[:7], 16) % 99_000_000


def trainer_viewer_id(team_id: str) -> int:
    return 900_000_000_000 + int(hashlib.sha1(team_id.encode()).hexdigest()[:9], 16) % 99_999_999_999


def trainer_name(t: dict) -> str:
    names = (t.get("owner") or {}).get("names") or []
    return str(names[0]) if names else f"Trainer {t['id'][:4].upper()}"


def member_chara(t: dict, idx: int, race_instance_id: int) -> dict:
    from .handlers import trained_chara as TC
    m = t["members"][idx]
    card_id = int(m["card"])
    speed, stamina, power, guts, wiz = (int(x) for x in m["stats"])
    proper = dict(_card_proper(card_id))
    style = int(m.get("racingStyle") or 2)
    style = style if style in _STYLE_KEY else 1
    ground_key, dist_key = t.get("course_keys") or course_keys(race_instance_id)
    apt = list(m.get("aptitudes") or [])
    if len(apt) == 3:
        proper[ground_key] = _APT.get(apt[0], proper[ground_key])
        proper[dist_key] = _APT.get(apt[1], proper[dist_key])
        proper[_STYLE_KEY[style]] = _APT.get(apt[2], proper[_STYLE_KEY[style]])
    score = int(m.get("score") or 0)
    tcid = _synthetic_id(t["id"], idx)
    chara_id = int(m.get("chara") or card_id // 100)
    vid = trainer_viewer_id(t["id"])
    out = {
        "viewer_id": vid, "trained_chara_id": tcid, "owner_viewer_id": 0, "owner_trained_chara_id": 0,
        "single_mode_chara_id": tcid, "card_id": card_id, "chara_id": chara_id, "use_type": 0,
        "speed": speed, "stamina": stamina, "power": power, "guts": guts, "wiz": wiz,
        "fans": 300_000, "rank_score": score, "rank": TC._rank_for_score(score),
        **proper,
        "succession_num": 0, "rarity": 5, "is_saved": 1, "is_locked": 0, "talent_level": 5,
        "race_cloth_id": TC.dress_for_card(card_id, chara_id), "chara_grade": 0,
        "running_style": style, "nickname_id": 0, "wins": 0, "scenario_id": 1,
        "create_time": "2026-01-01 00:00:00",
        "skill_array": [{"skill_id": int(s), "level": int(lv)} for s, lv in (m.get("skills") or [])],
        "support_card_list": [{"position": d.get("position", i + 1), "support_card_id": d["id"],
                               "exp": d.get("exp", 0), "limit_break_count": d.get("lb", 0)}
                              for i, d in enumerate(m.get("deck") or [])],
        "race_result_list": [], "win_saddle_id_array": [], "nickname_id_array": [],
        "factor_info_array": [], "factor_extend_array": [],
        "succession_chara_array": [
            {"position_id": p.get("positionId", 10), "card_id": p.get("cardId", 0), "rank": p.get("rank", 0),
             "rarity": 5, "talent_level": 5, "owner_viewer_id": 0, "win_saddle_id_array": [],
             "factor_info_array": [{"factor_id": f["id"], "level": f["level"]} for f in p.get("factors") or []]}
            for p in m.get("parents") or []],
    }
    seen = SEEN.setdefault(vid, {"name": trainer_name(t), "charas": {}})
    seen["charas"][tcid] = out
    return out


SEEN: dict = {}
