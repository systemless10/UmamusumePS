
from __future__ import annotations

import functools
import logging
import re
import time

from ... import epithets, event_engine, master_data, region, scenarios
from ... import state as state_store
from ...handlers import collection
from ...handlers import single_mode_team as smt

log = logging.getLogger(__name__)

HANDLERS: dict = {}

KEY = "jp_skill_upgrade"
FORCE_KEY = "skill_upgrade_force_met"

_WIRE_TYPE = {1: 1, 2: 2, 3: 0}

_ABILITY_COLS = tuple((f"ability_type_{i}_{j}", f"float_ability_value_{i}_{j}")
                      for i in (1, 2) for j in (1, 2, 3))

_STATS = {"スピード": "speed", "スタミナ": "stamina", "パワー": "power",
          "根性": "guts", "賢さ": "wiz"}
_TAGS = {"逃げ": "101", "先行": "102", "差し": "103", "追込": "104",
         "短距離": "201", "マイル": "202", "中距離": "203", "長距離": "204",
         "ダート": "502"}
_EFFECTS = {"速度が上がる": {21, 22, 27}, "加速力が上がる": {31},
            "持久力が回復する": {9}, "コース取りがうまくなる": {28},
            "能力を引き出す": {1, 2, 3, 4, 5}}
_STYLES = {"逃げ": 1, "先行": 2, "差し": 3, "追込": 4}
_YEARS = {"ジュニア": 0, "クラシック": 1, "シニア": 2}
_GROUNDS = {"良": 1, "稍重": 2, "重": 3, "不良": 4}
_BANDS = {"短距離": "sprint", "マイル": "mile", "中距離": "medium", "長距離": "long"}
_GRADE_WORDS = (("GⅡまたはGⅠ", {"g1", "g2"}), ("重賞", {"g1", "g2", "g3"}),
                ("GⅠ", {"g1"}), ("GⅡ", {"g2"}), ("GⅢ", {"g3"}), ("レース", None))

_REST_COMMAND_ID = 701
_REST_COMMAND_TYPE = 7
_OUTING_COMMAND_TYPE = 3
_PAL_OUTING_GROUP = 390
_CAMP_GROUP = 304
_RECREATION_GROUPS = {301, 390, 304}


def available() -> bool:
    return master_data.has_table("skill_upgrade_condition")


@functools.lru_cache(maxsize=1)
def _jp_master():
    import sqlite3
    path = master_data.MASTER_MDB_PATH.as_posix().replace(
        "UmamusumePrettyDerby/UmamusumePrettyDerby_Data",
        "UmamusumePrettyDerby_Jpn/UmamusumePrettyDerby_Jpn_Data")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _text_query(sql: str, params: tuple = ()) -> list:
    if region.PROCESS_REGION == "jp":
        return master_data.query(sql, params)
    return _jp_master().execute(sql, params).fetchall()


def _jp_text(category: int, index: int) -> str:
    row = next(iter(_text_query(
        "SELECT text FROM main.text_data WHERE category=? AND [index]=?", (category, index))), None)
    return row["text"] if row else ""


def _norm(text: str) -> str:
    text = (text or "").replace("\\n", "").replace("\n", "").strip()
    text = re.sub(r"\s+", "", text)
    text = text.translate(str.maketrans("０１２３４５６７８９()ｍ〇", "0123456789（）m○"))
    return text.replace("G1", "GⅠ").replace("G2", "GⅡ").replace("G3", "GⅢ")


@functools.lru_cache(maxsize=None)
def _conditions(description_id: int) -> tuple:
    return tuple(dict(r) for r in master_data.query(
        "SELECT id, num, sub_num FROM skill_upgrade_condition "
        "WHERE description_id=? ORDER BY id", (description_id,)))


@functools.lru_cache(maxsize=None)
def _card_base_skill(card_id: int, rank: int) -> int:
    row = master_data.query_one(
        "SELECT a.skill_id FROM available_skill_set a JOIN skill_data s ON s.id=a.skill_id "
        "WHERE a.available_skill_set_id=? AND a.need_rank=? AND s.rarity=2", (card_id, rank))
    return int(row["skill_id"]) if row else 0


def _evolutions(chara_info: dict, full_state: dict) -> list[dict]:
    now = int(time.time())
    out = []
    card_id = int(chara_info.get("card_id") or 0)
    talent = int(chara_info.get("talent_level") or 1)
    for r in master_data.query(
            "SELECT id, rank, skill_id FROM skill_upgrade_description "
            "WHERE card_id=? AND rank<=? AND start_date<=?", (card_id, talent, now)):
        out.append({"description_id": r["id"], "wire_type": 1, "skill_id": r["skill_id"],
                    "base_skill_id": _card_base_skill(card_id, r["rank"]), "rank": r["rank"]})
    for r in () if not master_data.has_table("skill_upgrade_speciality") else master_data.query(
            "SELECT skill_id, base_skill_id FROM skill_upgrade_speciality "
            "WHERE scenario_id=? AND start_date<=?",
            (int(chara_info.get("scenario_id") or 0), now)):
        out.append({"description_id": r["skill_id"], "wire_type": 2,
                    "skill_id": r["skill_id"], "base_skill_id": r["base_skill_id"]})
    owned = {c.get("card_id"): int(c.get("rarity") or 0)
             for c in full_state.get(collection.CARD_LIST_KEY) or () if isinstance(c, dict)}
    for r in () if not master_data.has_table("skill_upgrade_succession_skill") else master_data.query(
            "SELECT card_id, base_skill_id, skill_id FROM skill_upgrade_succession_skill "
            "WHERE start_date<=?", (now,)):
        if owned.get(r["card_id"], 0) >= 3:
            out.append({"description_id": r["base_skill_id"], "wire_type": 0,
                        "skill_id": r["skill_id"], "base_skill_id": r["base_skill_id"]})
    for e in out:
        e["conditions"] = _conditions(e["description_id"])
    return [e for e in out if e["conditions"] and e["base_skill_id"]]


@functools.lru_cache(maxsize=1)
def _race_names() -> dict:
    names: dict = {}
    for r in _text_query(
            "SELECT [index], text FROM main.text_data WHERE category IN (28, 29)"):
        names.setdefault(_norm(r["text"]), set()).add(int(r["index"]))
    return names


@functools.lru_cache(maxsize=None)
def _track_ids(name: str) -> frozenset:
    return frozenset(int(r["index"]) for r in _text_query(
        "SELECT [index] FROM main.text_data WHERE category=35 AND text=?", (name,)))


@functools.lru_cache(maxsize=None)
def _race(program_id: int) -> dict | None:
    info = epithets._race_info(program_id)
    return dict(info) if info else None


def _resolve_race(name: str):
    year = None
    m = re.fullmatch(r"(.+?)（(ジュニア|クラシック|シニア)級）", name)
    if m:
        name, year = m.group(1), _YEARS[m.group(2)]
    ids = _race_names().get(name)
    if not ids and name.endswith("カップ"):
        ids = _race_names().get(name[:-3] + "C")
    return (frozenset(ids), year) if ids else None


def _race_list(blob: str):
    parts = [p for p in blob.split("、") if p]
    resolved = [_resolve_race(p) for p in parts]
    return tuple(resolved) if parts and all(resolved) else None


def _capped(n, total):
    return min(int(n), total), total


def _race_filter(body: str):
    f = {}
    m = re.match(r"(ジュニア|クラシック|シニア)級で", body)
    if m:
        f["year"], body = _YEARS[m.group(1)], body[m.end():]
    m = re.match(r"作戦「(逃げ|先行|差し|追込)」(?:かつ(\d+)番人気)?で", body)
    if m:
        f["style"] = _STYLES[m.group(1)]
        if m.group(2):
            f["popularity"] = int(m.group(2))
        body = body[m.end():]
    m = re.match(r"((?:「[^」]+」)+)のバ場状態で", body)
    if m:
        f["grounds"] = {_GROUNDS[g] for g in re.findall(r"「([^」]+)」", m.group(1))}
        body = body[m.end():]
    m = re.match(r"(.+?)レース場で", body)
    if m:
        f["tracks"] = _track_ids(m.group(1))
        body = body[m.end():]
    m = re.match(r"(短距離|マイル|中距離|長距離)(?:の)?", body)
    if m:
        f["band"], body = _BANDS[m.group(1)], body[m.end():]
    m = re.match(r"(\d+)mの", body)
    if m:
        f["distance"], body = int(m.group(1)), body[m.end():]
    m = re.match(r"(芝|ダート)(?:の)?", body)
    if m:
        f["surface"], body = ("turf" if m.group(1) == "芝" else "dirt"), body[m.end():]
    f["grades"] = None
    for word, grades in _GRADE_WORDS:
        if body == word:
            f["grades"], body = grades, ""
            break
    return f if body == "" else None


def _race_matches(rec: dict, f: dict) -> bool:
    info = rec["info"]
    if f.get("grades") is not None and info["grade"] not in f["grades"]:
        return False
    for key in ("band", "surface", "distance"):
        if key in f and info[key] != f[key]:
            return False
    if "tracks" in f and info["track_id"] not in f["tracks"]:
        return False
    if "year" in f and rec["year"] != f["year"]:
        return False
    if "style" in f and rec["style"] != f["style"]:
        return False
    if "popularity" in f and not (rec["popularity"] and rec["popularity"] <= f["popularity"]):
        return False
    if "grounds" in f and rec["ground"] not in f["grounds"]:
        return False
    return True


def _named_hits(ctx, race, won_only=True, max_rank=1):
    ids, year = race
    return [r for r in ctx["races"] if r["instance"] in ids
            and (year is None or r["year"] == year)
            and (not won_only or r["rank"] <= max_rank)]


def _back_to_back(ctx, race) -> int:
    years = sorted({r["year"] for r in _named_hits(ctx, race)})
    best = run = 0
    for i, y in enumerate(years):
        run = run + 1 if i and y == years[i - 1] + 1 else 1
        best = max(best, run)
    return min(best, 2)


def _event_ids(title: str) -> frozenset:
    return _event_titles().get(title, frozenset())


@functools.lru_cache(maxsize=1)
def _event_titles() -> dict:
    out: dict = {}
    for r in _text_query("SELECT [index], text FROM main.text_data WHERE category=181"):
        out.setdefault(_norm(r["text"]), set()).add(int(r["index"]))
    return {k: frozenset(v) for k, v in out.items()}


@functools.lru_cache(maxsize=None)
def _skill_ids_named(name: str) -> frozenset:
    return frozenset(int(r["index"]) for r in _text_query(
        "SELECT [index] FROM main.text_data WHERE category=47 AND text=?", (name,)))


@functools.lru_cache(maxsize=None)
def _condition_ids_named(name: str) -> frozenset:
    return frozenset(int(r["index"]) for r in _text_query(
        "SELECT [index] FROM main.text_data WHERE category=142 AND text=?", (name,)))


@functools.lru_cache(maxsize=None)
def _skill_row(skill_id: int) -> dict:
    row = master_data.query_one("SELECT * FROM skill_data WHERE id=?", (skill_id,))
    if row is None:
        return {"tags": frozenset(), "effects": frozenset(), "group_id": 0}
    effects = {int(row[a]) for a, v in _ABILITY_COLS if row[a] and (row[v] or 0) > 0}
    return {"tags": frozenset((row["tag_id"] or "").split("/")),
            "effects": frozenset(effects), "group_id": int(row["group_id"] or 0)}


def _parse(text: str):
    t = _norm(text)
    m = re.fullmatch(r"基礎能力\[(.+?)\]が(\d+)以上になる", t)
    if m and m.group(1) in _STATS:
        stat, n = _STATS[m.group(1)], int(m.group(2))
        return (lambda c: _capped(c["ci"].get(stat) or 0, n)), True
    m = re.fullmatch(r"ファン数が(\d+)人以上になる", t)
    if m:
        n = int(m.group(1))
        return (lambda c: _capped(c["ci"].get("fans") or 0, n)), True
    m = re.fullmatch(r"＜(.+?)＞(?:の)?スキルを(\d+)個以上所持する", t)
    if m and m.group(1) in _TAGS:
        tag, n = _TAGS[m.group(1)], int(m.group(2))
        return (lambda c: _capped(sum(tag in _skill_row(s)["tags"] for s in c["skills"]), n)), True
    m = re.fullmatch(r"(.+?)スキルを(\d+)個以上所持する", t)
    if m and m.group(1) in _EFFECTS:
        eff, n = _EFFECTS[m.group(1)], int(m.group(2))
        return (lambda c: _capped(sum(bool(eff & _skill_row(s)["effects"])
                                      for s in c["skills"]), n)), True
    m = re.fullmatch(r"スキル「(.+)」を所持する", t)
    if m:
        ids = _skill_ids_named(m.group(1))
        return (lambda c: (int(bool(ids & set(c["skills"]))), 1)), bool(ids)

    m = re.fullmatch(r"(.+?)を二連覇する", t)
    if m and _race_list(m.group(1)):
        races = _race_list(m.group(1))
        return (lambda c: (sum(_back_to_back(c, r) for r in races), 2 * len(races))), True
    m = re.fullmatch(r"(.+?)に出走する", t)
    if m and _race_list(m.group(1)):
        races = _race_list(m.group(1))
        return (lambda c: (sum(bool(_named_hits(c, r, won_only=False)) for r in races),
                           len(races))), True
    m = re.fullmatch(r"(.+?)で(\d+)着以内になる", t)
    if m and _race_list(m.group(1)):
        races, place = _race_list(m.group(1)), int(m.group(2))
        return (lambda c: (int(all(_named_hits(c, r, max_rank=place) for r in races)), 1)), True
    m = re.fullmatch(r"(.+?)を勝利する", t)
    if m and _race_list(m.group(1)):
        races = _race_list(m.group(1))
        return (lambda c: (sum(bool(_named_hits(c, r)) for r in races), len(races))), True
    m = re.fullmatch(r"(.+?)を?(\d+)勝以上する", t) or re.fullmatch(r"(.+?)を勝利する()", t)
    if m:
        f = _race_filter(m.group(1).removesuffix("を"))
        if f is not None:
            n = int(m.group(2) or 1)
            return (lambda c: _capped(sum(r["rank"] == 1 and _race_matches(r, f)
                                          for r in c["races"]), n)), True
    m = re.fullmatch(r"(.+?)で(\d+)着以内を(\d+)回以上とる", t)
    if m and _race_filter(m.group(1)) is not None:
        f, place, n = _race_filter(m.group(1)), int(m.group(2)), int(m.group(3))
        return (lambda c: _capped(sum(r["rank"] <= place and _race_matches(r, f)
                                      for r in c["races"]), n)), True
    m = re.fullmatch(r"(.+?)で(\d+)着を(\d+)回以上とる", t)
    if m and _race_filter(m.group(1)) is not None:
        f, place, n = _race_filter(m.group(1)), int(m.group(2)), int(m.group(3))
        return (lambda c: _capped(sum(r["rank"] == place and _race_matches(r, f)
                                      for r in c["races"]), n)), True

    for pattern, key in ((r"育成ウマ娘とのお出かけを(\d+)回以上行う", "outings"),
                         (r"友人タイプまたはグループタイプとのお出かけを(\d+)回以上行う", "pal_outings"),
                         (r"合宿中に「お休み＆お出かけ」を(\d+)回以上行う", "camp_rests"),
                         (r"(\d+)回以上お休みする", "rests"),
                         (r"やる気が上がる育成イベントを(\d+)回以上発生させる", "motivation_ups")):
        m = re.fullmatch(pattern, t)
        if m:
            n = int(m.group(1))
            return (lambda c, key=key: _capped(c["tally"].get(key, 0), n)), True
    m = re.fullmatch(r"トレーニングに\d+度も失敗しない", t)
    if m:
        return (lambda c: (int(c["failures"] == 0), 1)), True
    m = re.fullmatch(r"育成中に(\d+)回以上「(.+)」になる", t)
    if m and _condition_ids_named(m.group(2)):
        n, name = int(m.group(1)), m.group(2)
        return (lambda c: _capped(c["tally"].get("onsets", {}).get(name, 0), n)), True
    m = re.fullmatch(r"「(.+)」を持つ状態で育成を完了する", t)
    if m and _condition_ids_named(m.group(1)):
        ids = _condition_ids_named(m.group(1))
        return (lambda c: (int(bool(ids & c["effects"])), 1)), True

    m = re.fullmatch(r"育成イベント((?:「[^」]+」)+)(?:のいずれか)?(.*)", t)
    if m:
        ids = frozenset().union(*(_event_ids(x) for x in re.findall(r"「([^」]+)」", m.group(1))))
        times = re.fullmatch(r"を(\d+)回以上(?:目撃|発生させ)る", m.group(2))
        n = int(times.group(1)) if times else 1
        return (lambda c: _capped(sum(1 for s in c["fired"] if s in ids), n)), bool(ids)

    if re.fullmatch(r"育成開始時に★3以上の育成ウマ娘.+を解放している", t):
        return (lambda c: (1, 1)), True
    if t == "育成開始時に継承してヒントを獲得している":
        return (lambda c: (int(c["base_group"] in c["start_tip_groups"]), 1)), True

    if t == "すべてのラーメン・ジャンボリーを大盛況以上で完了する":
        return (lambda c: (int(len([r for r in c["ramen"] if r["result_state"] >= 2]) >= 3), 1)), True
    if t == "超ラーメン・ジャンボリー極を大盛況以上で完了する":
        return (lambda c: (int(any(r["check_point_type"] == 3 and r["result_state"] >= 2
                                   for r in c["ramen"])), 1)), True

    if t == "すべての湯浴み会をいい湯以上で完了する":
        return (lambda c: (int(len([r for r in c["onsen"] if r["result_type"] >= 2]) >= 3), 1)), True
    if t == "第3回湯浴み会をいい湯以上で完了する":
        return (lambda c: (int(any(r["schedule_id"] == 3 and r["result_type"] >= 2
                                   for r in c["onsen"])), 1)), True

    if t == "すべての振り返りをExcellent!で完了する":
        return (lambda c: (int(len([r for r in c["breeders"] if r["result_type"] >= 2]) >= 5), 1)), True
    if t == "第5回振り返りをExcellent!で完了する":
        return (lambda c: (int(any(r["schedule_id"] == 5 and r["result_type"] >= 2
                                   for r in c["breeders"])), 1)), True

    if t == "すべてのトレセン島評価会を大好評で完了する":
        return (lambda c: (int(len([r for r in c["pioneer"] if r["result_state"] >= 2]) >= 5), 1)), True
    if t == "最終評価会を大好評で完了する":
        return (lambda c: (int(any(r["check_point_type"] == 5 and r["result_state"] >= 2
                                   for r in c["pioneer"])), 1)), True

    if t == "すべてのUpgradeExamをS評価で完了する":
        return (lambda c: (int(len([r for r in c["mecha"] if r["result_type"] >= 3]) >= 5), 1)), True
    if t == "第5回UpgradeExamをS評価で完了する":
        return (lambda c: (int(any(r["schedule_id"] == 5 and r["result_type"] >= 3
                                   for r in c["mecha"])), 1)), True

    m = re.fullmatch(r"期待度(\d+)[％%]以上になる", t)
    if m:
        n = int(m.group(1))
        return (lambda c: _capped(int(c["arc"].get("rate") or 0) // 10, n)), True
    m = re.fullmatch(r"海外適性Lvの合計が(\d+)以上になる", t)
    if m:
        n = int(m.group(1))
        return (lambda c: _capped(int(c["arc"].get("levels") or 0), n)), True

    return (lambda c: (1, 1)), False


@functools.lru_cache(maxsize=None)
def _compiled(condition_id: int):
    return _parse(_jp_text(290, condition_id))


_SCENARIO_ROWS_ON_WIRE = frozenset({6})


@functools.lru_cache(maxsize=None)
def _compiled_scenario(row_id: int):
    return _parse(_jp_text(326, row_id))


def _scenario_rows(chara_info: dict, ctx: dict) -> list[dict]:
    scenario = int(chara_info.get("scenario_id") or 0)
    if scenario not in _SCENARIO_ROWS_ON_WIRE or not master_data.has_table("skill_up_scenario_condition"):
        return []
    talent = int(chara_info.get("talent_level") or 1)
    rows = []
    for r in master_data.query(
            "SELECT id, rank, condition_id FROM skill_up_scenario_condition "
            "WHERE scenario_id=? AND rank<=? ORDER BY condition_id",
            (scenario, talent)):
        fn, _ok = _compiled_scenario(int(r["id"]))
        try:
            cur, total = fn(ctx)
        except Exception:
            log.exception("scenario evolution condition %s failed", r["id"])
            cur, total = 0, 1
        rows.append({"condition_id": int(r["condition_id"]), "total_count": int(total),
                     "current_count": int(min(cur, total)), "rank": int(r["rank"])})
    return rows


def unparsed() -> list[tuple[int, str]]:
    out = []
    for r in master_data.query("SELECT id FROM skill_upgrade_condition ORDER BY id"):
        if not _compiled(r["id"])[1]:
            out.append((r["id"], _jp_text(290, r["id"])))
    return out


def _tracker(full_state: dict) -> dict:
    return full_state.setdefault(KEY, {"tally": {}, "start_tip_groups": None,
                                       "effects_seen": []})


def _context(chara_info: dict, career_data: dict, full_state: dict) -> dict:
    races = []
    for e in career_data.get("race_history") or ():
        info = _race(e.get("program_id"))
        if info is None:
            continue
        races.append({"info": info, "instance": info["instance_id"],
                      "rank": int(e.get("result_rank") or 99),
                      "year": epithets._year_index(e.get("turn")),
                      "style": e.get("running_style"),
                      "popularity": e.get("popularity"),
                      "ground": e.get("ground_condition")})
    disabled = set(chara_info.get("disable_skill_id_array") or ())
    tracker = _tracker(full_state)
    start_groups = tracker.get("start_tip_groups")
    if start_groups is None:
        start_groups = [t.get("group_id") for t in chara_info.get("skill_tips_array") or ()]
    try:
        snap = scenarios.for_chara(chara_info).snapshot(full_state) or {}
    except Exception:
        snap = {}
    return {"ci": chara_info, "races": races,
            "skills": [s.get("skill_id") for s in chara_info.get("skill_array") or ()
                       if s.get("skill_id") not in disabled],
            "effects": set(chara_info.get("chara_effect_id_array") or ()),
            "fired": list(full_state.get(event_engine.FIRED_EVENTS_KEY) or ()),
            "failures": int(((career_data.get(smt.TRAINING_TALLY_KEY) or {})
                             .get("failures")) or 0),
            "tally": tracker.get("tally") or {},
            "start_tip_groups": set(start_groups),
            "ramen": list(snap.get("ramen_results") or ()),
            "onsen": list(snap.get("onsen_results") or ()),
            "breeders": list(snap.get("breeders_reviews") or ()),
            "pioneer": list(snap.get("pioneer_results") or ()),
            "mecha": list(snap.get("mecha_results") or ()),
            "arc": {"rate": snap.get("arc_rate"), "levels": snap.get("arc_level_sum")},
            "base_group": 0, "force": bool(full_state.get(FORCE_KEY))}


def _progress(evo: dict, ctx: dict) -> list[dict]:
    ctx["base_group"] = _skill_row(evo["base_skill_id"])["group_id"]
    rows = []
    for cond in evo["conditions"]:
        fn, _ok = _compiled(cond["id"])
        try:
            cur, total = fn(ctx)
        except Exception:
            log.exception("skill evolution condition %s failed", cond["id"])
            cur, total = 0, 1
        if ctx.get("force"):
            cur = total
        rows.append({"condition_id": cond["id"], "total_count": int(total),
                     "current_count": int(min(cur, total))})
    return rows


def _met(evo: dict, rows: list[dict], scenario_rows=()) -> bool:
    if evo["wire_type"] == 1 and any(r["rank"] == evo.get("rank") and r["current_count"] >= r["total_count"]
                                     for r in scenario_rows):
        return True
    by_id = {r["condition_id"]: r for r in rows}
    groups: dict = {}
    for cond in evo["conditions"]:
        r = by_id[cond["id"]]
        groups.setdefault(cond["num"], []).append(r["current_count"] >= r["total_count"])
    return all(any(g) for g in groups.values())


def upgrade_info(chara_info: dict, career_data: dict, full_state: dict) -> list[dict]:
    ctx = _context(chara_info, career_data, full_state)
    rows = []
    for evo in _evolutions(chara_info, full_state):
        rows += _progress(evo, ctx)
    seen = {r["condition_id"] for r in rows}
    rows += [{k: v for k, v in r.items() if k != "rank"}
             for r in _scenario_rows(chara_info, ctx) if r["condition_id"] not in seen]
    return sorted(rows, key=lambda r: r["condition_id"])


def _apply_picks(viewer_id, picks: list) -> None:
    full_state = state_store.get_state(viewer_id) or {}
    career = (full_state.get(smt.STATE_KEY) or {}).get("data") or {}
    ci = career.get("chara_info")
    if not isinstance(ci, dict) or not picks:
        return
    ctx = _context(ci, career, full_state)
    evos = {(e["wire_type"], e["skill_id"]): e for e in _evolutions(ci, full_state)}
    scenario_rows = _scenario_rows(ci, ctx)
    skills = ci.setdefault("skill_array", [])
    disabled = ci.setdefault("disable_skill_id_array", [])
    for pick in picks:
        key = (int(pick.get("upgrade_type") or 0), int(pick.get("skill_id") or 0))
        evo = evos.get(key)
        if evo is None:
            log.warning("skill evolution %s refused: not offered to this career", key)
            continue
        owned = next((s for s in skills if s.get("skill_id") == evo["base_skill_id"]), None)
        if owned is None:
            log.warning("skill evolution %s refused: base skill %s not learned",
                        key, evo["base_skill_id"])
            continue
        if not _met(evo, _progress(evo, ctx), scenario_rows):
            log.warning("skill evolution %s refused: conditions not met", key)
            continue
        skills.remove(owned)
        skills.append({"skill_id": evo["skill_id"], "level": owned.get("level") or 1})
        disabled.append(evo["base_skill_id"])
        log.info("skill evolution: %s -> %s", evo["base_skill_id"], evo["skill_id"])
    unique = [s for s in skills if 100000 <= (s.get("skill_id") or 0) < 200000]
    rest = sorted((s for s in skills if s not in unique), key=lambda s: s.get("skill_id") or 0)
    ci["skill_array"] = unique + rest
    ci["disable_skill_id_array"] = sorted(set(disabled))
    state_store.save_state(viewer_id, full_state)


def _is_career(endpoint: str) -> bool:
    return endpoint.startswith("single_mode") and "/" in endpoint


def _action(endpoint: str) -> str:
    return endpoint.rsplit("/", 1)[-1]


def before(endpoint: str, payload: dict):
    if not _is_career(endpoint) or not available():
        return None
    viewer_id = payload.get("viewer_id")
    if _action(endpoint) == "factor_select":
        try:
            picks = payload.get("gain_skill_upgrade_info_array")
            if picks is None and region.PROCESS_REGION != "jp":
                picks = [{"upgrade_type": 1, "skill_id": s}
                         for s in payload.get("skill_id_array") or ()]
            _apply_picks(viewer_id, list(picks or ()))
        except Exception:
            log.exception("skill evolution: applying picks failed")
        return None
    ci = (((state_store.get_state(viewer_id) or {}).get(smt.STATE_KEY) or {})
          .get("data") or {}).get("chara_info") or {}
    return {"motivation": ci.get("motivation")}


def _chara_infos(data):
    if not isinstance(data, dict):
        return
    if isinstance(data.get("chara_info"), dict):
        yield data["chara_info"]
    for k, v in data.items():
        if k.endswith("_common") and isinstance(v, dict) and isinstance(v.get("chara_info"), dict):
            yield v["chara_info"]


def _tally(endpoint: str, payload: dict, data: dict, full_state: dict, noted) -> None:
    tracker = _tracker(full_state)
    tally = tracker.setdefault("tally", {})
    action = _action(endpoint)
    if action == "exec_command":
        ctype = payload.get("command_type")
        cid = payload.get("command_id")
        group = payload.get("command_group_id")
        camp = group == _CAMP_GROUP or cid == _CAMP_GROUP or (
            (group in _RECREATION_GROUPS or ctype == _OUTING_COMMAND_TYPE)
            and smt._is_camp_turn(payload.get("current_turn")))
        if camp:
            tally["camp_rests"] = tally.get("camp_rests", 0) + 1
            tally["rests"] = tally.get("rests", 0) + 1
        elif cid == _REST_COMMAND_ID or ctype == _REST_COMMAND_TYPE:
            tally["rests"] = tally.get("rests", 0) + 1
        elif group == _PAL_OUTING_GROUP:
            tally["pal_outings"] = tally.get("pal_outings", 0) + 1
        elif group in _RECREATION_GROUPS or ctype == _OUTING_COMMAND_TYPE:
            tally["outings"] = tally.get("outings", 0) + 1
    ci = next(_chara_infos(data), None)
    if ci is None:
        return
    if action == "check_event" and noted and noted.get("motivation") is not None:
        if (ci.get("motivation") or 0) > (noted["motivation"] or 0):
            tally["motivation_ups"] = tally.get("motivation_ups", 0) + 1
    now = set(ci.get("chara_effect_id_array") or ())
    seen = set(tracker.get("effects_seen") or ())
    for cid in now - seen:
        name = _jp_text(142, cid)
        if name:
            onsets = tally.setdefault("onsets", {})
            onsets[name] = onsets.get(name, 0) + 1
    tracker["effects_seen"] = sorted(now)


def after(endpoint: str, payload: dict, resp: dict, noted) -> None:
    if not _is_career(endpoint) or not available():
        return
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    data = resp.get("data")
    if not isinstance(data, dict):
        return
    viewer_id = payload.get("viewer_id")
    full_state = state_store.get_state(viewer_id) or {}
    career = (full_state.get(smt.STATE_KEY) or {}).get("data")
    if _action(endpoint) == "start":
        full_state.pop(KEY, None)
    if not isinstance(career, dict) or not isinstance(career.get("chara_info"), dict):
        for ci in _chara_infos(data):
            ci["skill_upgrade_info_array"] = []
        return
    try:
        tracker = _tracker(full_state)
        if tracker.get("start_tip_groups") is None:
            tracker["start_tip_groups"] = sorted(
                {t.get("group_id") for t in career["chara_info"].get("skill_tips_array") or ()})
        _tally(endpoint, payload, data, full_state, noted)
        state_store.save_state(viewer_id, full_state)
        for ci in _chara_infos(data):
            ci["skill_upgrade_info_array"] = upgrade_info(ci, career, full_state)
    except Exception:
        log.exception("skill evolution: stamping %s failed", endpoint)
