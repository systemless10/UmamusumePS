from __future__ import annotations

import re

from . import master_data as M

_VARS = ("rotation", "track_id", "season")
_ALT = re.compile(r"^(?:(rotation|track_id|season)==(\d+))(?:@\1==\d+)*$")
_RANK = {4: 0, 1: 1, 2: 2, 3: 3}


@M.context_cache(maxsize=4)
def families() -> dict:
    fam, by_skill = {v: {} for v in _VARS}, {}
    for r in M.query("SELECT id, group_id, condition_1, condition_2, ability_type_1_1 FROM skill_data "
                     "WHERE ability_type_1_1 BETWEEN 1 AND 5"):
        cond = r["condition_1"] or ""
        m = _ALT.match(cond)
        if not m or r["condition_2"]:
            continue
        var = m.group(1)
        values = {int(x) for x in re.findall(r"==(\d+)", cond)}
        f = fam[var].setdefault(r["group_id"], {"values": values, "tiers": {}})
        f["tiers"].setdefault(r["id"] % 10, r["id"])
        by_skill[r["id"]] = (var, r["group_id"], r["id"] % 10)
    return {"families": fam, "skills": by_skill}


@M.context_cache(maxsize=64)
def course_values(race_instance_id: int, season: int) -> dict:
    row = M.query_one("SELECT rcs.race_track_id, rcs.turn FROM race_instance ri JOIN race r ON r.id=ri.race_id "
                      "JOIN race_course_set rcs ON rcs.id=r.course_set WHERE ri.id=?", (race_instance_id,))
    out = {"season": int(season)} if season else {}
    if row:
        out["track_id"] = int(row["race_track_id"])
        if row["turn"] in (1, 2):
            out["rotation"] = int(row["turn"])
    return out


def _target(var: str, value: int):
    for gid, f in families()["families"][var].items():
        if value in f["values"]:
            return gid, f
    return None, None


def adapt(skills: list[dict], race_instance_id: int, season: int) -> list[dict]:
    info, want = families(), course_values(race_instance_id, season)
    out, best = [], {}
    for s in skills:
        sid = int(s["skill_id"])
        hit = info["skills"].get(sid)
        if not hit or hit[0] not in want:
            out.append(s)
            continue
        var, gid, tier = hit
        tgid, target = _target(var, want[var])
        new = sid
        if target is not None and gid != tgid and tier != 3:
            new = target["tiers"].get(tier) or (target["tiers"].get(1) if tier == 4 else None) or sid
        moved = {**s, "skill_id": new}
        if new % 10 != 3 and info["skills"].get(new, (None, None))[1] == tgid:
            if tgid in best:
                i = best[tgid]
                if _RANK[new % 10] < _RANK[int(out[i]["skill_id"]) % 10]:
                    out[i] = moved
                continue
            best[tgid] = len(out)
        out.append(moved)
    return out
