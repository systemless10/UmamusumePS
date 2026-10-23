
from __future__ import annotations

import logging
import random
import zlib

from . import master_data

log = logging.getLogger("uma-server")

_DEFAULT_AREA = 999


@master_data.context_cache(maxsize=1)
def _season_windows() -> tuple:
    rows = master_data.query(
        "SELECT season, start_date, end_date FROM season_data WHERE type=1 ORDER BY id")
    out = []
    for r in rows:
        def mmdd(text):
            month, day = text.split(" ")[0].split("-")
            return int(month) * 100 + int(day)
        out.append((mmdd(r["start_date"]), mmdd(r["end_date"]), int(r["season"])))
    return tuple(out)


def season_for_date(date: int) -> int:
    date = int(date or 0)
    for start, end, season in _season_windows():
        if start <= end:
            if start <= date <= end:
                return season
        elif date >= start or date <= end:
            return season
    if date % 100 == 29 and date // 100 == 2:
        return 4
    return 1


@master_data.context_cache(maxsize=1)
def _condition_rows() -> tuple:
    columns = {r["name"] for r in master_data.query("PRAGMA table_info(race_condition)")}
    sub_weather = "sub_weather" if "sub_weather" in columns else "0 AS sub_weather"
    ground_type = "ground_type" if "ground_type" in columns else "999 AS ground_type"
    return tuple(dict(r) for r in master_data.query(
        f"SELECT id, area, season, weather, {sub_weather}, {ground_type}, ground, rate "
        "FROM race_condition ORDER BY id"))


@master_data.context_cache(maxsize=4096)
def _race_instance_info(race_instance_id: int) -> tuple | None:
    row = master_data.query_one(
        "SELECT ri.date AS date, rt.area AS area, rcs.ground AS ground "
        "FROM race_instance ri JOIN race ON race.id = ri.race_id "
        "JOIN race_course_set rcs ON rcs.id = race.course_set "
        "JOIN race_track rt ON rt.id = rcs.race_track_id WHERE ri.id = ?",
        (int(race_instance_id or 0),))
    if row is None:
        return None
    return int(row["date"] or 0), int(row["area"] or 0), int(row["ground"] or 0)


@master_data.context_cache(maxsize=4096)
def program_race_instance(program_id: int) -> int:
    row = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id = ?", (int(program_id or 0),))
    return int(row["race_instance_id"]) if row else 0


def roll(race_instance_id: int, rng: random.Random, season: int | None = None) -> dict:
    info = _race_instance_info(race_instance_id)
    if info is None:
        return {"season": season or 1, "weather": 1, "sub_weather": 0, "ground_condition": 1}
    date, area, surface = info
    season = season or season_for_date(date)
    rows = _condition_rows()
    if not any(r["area"] == area for r in rows):
        area = _DEFAULT_AREA if any(r["area"] == _DEFAULT_AREA for r in rows) else 0
    cands = [r for r in rows if r["area"] == area and r["season"] == season
             and r["ground_type"] in (999, surface) and r["rate"] > 0]
    if not cands:
        return {"season": season, "weather": 1, "sub_weather": 0, "ground_condition": 1}
    pick = rng.choices(cands, weights=[r["rate"] for r in cands])[0]
    return {"season": season, "weather": pick["weather"],
            "sub_weather": pick["sub_weather"], "ground_condition": pick["ground"]}


@master_data.context_cache(maxsize=1)
def _unique_rows() -> dict:
    try:
        rows = master_data.query(
            "SELECT chara_id, year, race_program_id, race_condition_id, frame_order "
            "FROM single_mode_race_unique_condition")
    except Exception:
        return {}
    return {(int(r["chara_id"]), int(r["year"]), int(r["race_program_id"])): dict(r) for r in rows}


def unique_condition(chara_id: int, year: int, program_id: int) -> dict | None:
    return _unique_rows().get((int(chara_id or 0), int(year or 0), int(program_id or 0)))


def career_conditions(chara_info: dict | None, program_id: int) -> dict:
    program_id = int(program_id or 0)
    chara_info = chara_info or {}
    key = f"{chara_info.get('start_time') or ''}|{chara_info.get('card_id') or 0}|{program_id}|cond"
    cond = roll(program_race_instance(program_id), random.Random(zlib.crc32(key.encode())))
    turn = int(chara_info.get("turn") or 0)
    year = (turn - 1) // 24 + 1 if turn > 0 else 1
    row = unique_condition((chara_info.get("card_id") or 0) // 100, year, program_id)
    if row:
        fixed = row["race_condition_id"] and next(
            (r for r in _condition_rows() if r["id"] == row["race_condition_id"]), None)
        if fixed:
            cond.update(season=fixed["season"], weather=fixed["weather"],
                        sub_weather=fixed["sub_weather"], ground_condition=fixed["ground"])
        if row["frame_order"]:
            cond["frame_order"] = int(row["frame_order"])
    return cond
