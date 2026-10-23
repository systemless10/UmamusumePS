
from __future__ import annotations

import functools

from app import master_data as M

from . import op
from .common import APTITUDES, card_name, chara_name, region_name, skill_name, text

SUPPORT_TYPES = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz"}
FACTOR_KINDS = {1: "blue", 2: "pink", 3: "green", 4: "white", 5: "race", 6: "scenario", 7: "other"}
GRADES = {100: "G1", 200: "G2", 300: "G3", 400: "OP", 700: "Pre-OP", 800: "Maiden", 900: "Debut", 999: "Daily"}

ITEM_GROUPS = [
    ("Recovery", {20, 21}),
    ("Training", {11, 34, 110, 160, 164}),
    ("Race", {140, 150, 163, 170}),
    ("Scouting", {40, 41, 42, 169}),
    ("Currency", {30, 90, 91, 93, 94, 97, 98, 99, 100, 103, 161, 165, 166}),
]


def item_group(category) -> str:
    return next((g for g, cats in ITEM_GROUPS if category in cats), "Other")


def support_type(row) -> str:
    if row["support_card_type"] == 2:
        return "friend"
    if row["support_card_type"] == 3:
        return "group"
    return SUPPORT_TYPES.get(row["command_id"], "other")


@M.context_cache(maxsize=16)
def support_catalog() -> dict:
    cards = []
    for r in M.query("SELECT * FROM support_card_data ORDER BY id"):
        cards.append({
            "id": r["id"], "chara_id": r["chara_id"], "rarity": r["rarity"], "type": support_type(r),
            "name": text(77, r["id"]) or chara_name(r["chara_id"]),
            "title": (text(76, r["id"]) or "").strip("[]"),
            "start": r["start_date"],
            "custom": 9000 <= r["id"] % 10000 <= 9999,
        })
    limits = {r["rarity"]: [r[f"limit_{i}"] for i in range(5)] for r in M.query("SELECT * FROM support_card_limit")}
    levels: dict[int, list] = {}
    for r in M.query("SELECT rarity, level, total_exp FROM support_card_level ORDER BY rarity, level"):
        levels.setdefault(r["rarity"], []).append([r["level"], r["total_exp"]])
    return {"cards": cards, "limits": limits, "levels": levels}


@functools.lru_cache(maxsize=1)
def max_talent() -> int:
    if M.has_table("card_talent_level_upgrade_money"):
        row = M.query_one("SELECT MAX(upgrade_talent_level) AS m FROM card_talent_level_upgrade_money")
        if row and row["m"]:
            return int(row["m"])
    row = M.query_one("SELECT MAX(talent_level) AS m FROM card_talent_upgrade") \
        if M.has_table("card_talent_upgrade") else None
    return int(row["m"]) if row and row["m"] else 5


@functools.lru_cache(maxsize=1)
def card_max_talents() -> dict:
    import time
    out: dict[int, int] = {}
    if M.has_table("card_talent_level_upgrade_item"):
        for r in M.query("SELECT talent_group_id, MAX(upgrade_talent_level) AS m FROM card_talent_level_upgrade_item "
                         "WHERE COALESCE(start_date, 0) <= ? GROUP BY talent_group_id", (int(time.time()),)):
            out[int(r["talent_group_id"])] = int(r["m"])
    elif M.has_table("card_talent_upgrade"):
        for r in M.query("SELECT talent_group_id, MAX(talent_level) AS m FROM card_talent_upgrade GROUP BY talent_group_id"):
            out[int(r["talent_group_id"])] = int(r["m"])
    return out


def max_hint_level(talent_level: int) -> int:
    from app.handlers import cards
    return cards._max_hint_level(int(talent_level or 1))


@M.context_cache(maxsize=16)
def uma_catalog() -> dict:
    rar: dict[int, dict] = {}
    for r in M.query("SELECT * FROM card_rarity_data ORDER BY card_id, rarity"):
        rar.setdefault(r["card_id"], {})[r["rarity"]] = [r[a] for a in APTITUDES]
    cards = []
    for r in M.query("SELECT * FROM card_data WHERE default_rarity > 0 ORDER BY id"):
        rows = rar.get(r["id"], {})
        top = max(rows) if rows else r["default_rarity"]
        cards.append({
            "id": r["id"], "chara_id": r["chara_id"], "name": chara_name(r["chara_id"]),
            "title": (text(5, r["id"]) or "").strip("[]"),
            "default_rarity": r["default_rarity"], "max_rarity": top,
            "style": r["running_style"], "piece_id": r["get_piece_id"],
            "talent": [r["talent_speed"], r["talent_stamina"], r["talent_pow"], r["talent_guts"], r["talent_wiz"]],
            "apt": rows.get(top) or [1] * len(APTITUDES),
            "limited": r["limited_chara"],
            "max_talent": min(max_talent(), card_max_talents().get(int(r["talent_group_id"] or 0), 5)),
        })
    talent = max_talent()
    return {"cards": cards, "aptitudes": list(APTITUDES), "max_talent": talent,
            "hint_caps": {t: max_hint_level(t) for t in range(1, talent + 1)}}


def skill_catalog() -> list:
    from .common import future_content
    return _skill_catalog(future_content())


@functools.lru_cache(maxsize=2)
def _skill_catalog(future: bool) -> list:
    rows = {r["id"]: dict(r) for r in M.query("SELECT id, rarity, group_id, grade_value, icon_id FROM skill_data")}
    try:
        cost = {r["id"]: r["need_skill_point"] for r in M.query("SELECT id, need_skill_point FROM single_mode_skill_need_point")}
    except Exception:
        cost = {}
    extra_text = {}
    if future:
        from app import custom_support as C
        for b in C.unreleased_bundles():
            t = b.get("tables") or {}
            for r in t.get("skill_data") or []:
                rows.setdefault(r["id"], r)
            for r in t.get("single_mode_skill_need_point") or []:
                cost.setdefault(r["id"], r["need_skill_point"])
            for r in t.get("text_data") or []:
                if r.get("category") in (47, 48) and r.get("text"):
                    extra_text.setdefault((r["category"], r["index"]), r["text"])
    out = []
    for sid, r in sorted(rows.items()):
        name = text(47, sid) or extra_text.get((47, sid), "")
        if not name:
            continue
        out.append({"id": sid, "name": name, "rarity": r["rarity"], "group": r["group_id"],
                    "grade": r["grade_value"], "icon": r["icon_id"], "cost": cost.get(sid),
                    "desc": (text(48, sid) or extra_text.get((48, sid), "")).replace("\\n", " ")})
    return out


@functools.lru_cache(maxsize=1)
def item_catalog() -> list:
    out = []
    for r in M.query("SELECT id, item_category, limit_num, sort, rare FROM item_data ORDER BY sort, id"):
        out.append({"id": r["id"], "name": text(23, r["id"]) or f"Item {r['id']}",
                    "desc": (text(24, r["id"]) or "").replace("\\n", "\n"),
                    "category": r["item_category"], "group": item_group(r["item_category"]),
                    "limit": r["limit_num"], "rare": r["rare"]})
    return out


@functools.lru_cache(maxsize=1)
def factor_catalog() -> list:
    out = []
    for r in M.query("SELECT factor_id, factor_group_id, rarity, factor_type FROM succession_factor ORDER BY factor_id"):
        out.append({"id": r["factor_id"], "group": r["factor_group_id"], "stars": r["rarity"],
                    "kind": FACTOR_KINDS.get(r["factor_type"], "other"),
                    "name": text(147, r["factor_id"]) or f"Factor {r['factor_id']}"})
    return out


@functools.lru_cache(maxsize=1)
def program_catalog() -> list:
    out = []
    for r in M.query("""SELECT p.id, p.month, p.half, p.race_instance_id, r.grade
                        FROM single_mode_program p
                        JOIN race_instance ri ON ri.id = p.race_instance_id
                        JOIN race r ON r.id = ri.race_id ORDER BY p.id"""):
        out.append({"id": r["id"], "name": text(28, r["race_instance_id"]) or f"Program {r['id']}",
                    "grade": GRADES.get(r["grade"], str(r["grade"])), "month": r["month"], "half": r["half"]})
    return out


def rank_label(rank_id: int) -> str:
    base = ["G", "G+", "F", "F+", "E", "E+", "D", "D+", "C", "C+", "B", "B+", "A", "A+", "S", "S+", "SS", "SS+"]
    if 1 <= rank_id <= len(base):
        return base[rank_id - 1]
    if rank_id >= 99:
        n = rank_id - 99
        letters = ["LG", "LF", "LE", "LD", "LC", "LB", "LA", "LS"]
        if n // 25 < len(letters):
            return letters[n // 25] + ("" if n % 25 == 0 else str(n % 25))
        return f"#{rank_id}"
    n = rank_id - len(base) - 1
    series = ["UG", "UF", "UE", "UD", "UC", "UB", "UA", "US"]
    if 0 <= n // 10 < len(series):
        return series[n // 10] + ("" if n % 10 == 0 else str(n % 10))
    return f"#{rank_id}"


@functools.lru_cache(maxsize=1)
def ranks() -> list:
    out = {r["id"]: {"id": r["id"], "min": r["min_value"], "max": r["max_value"], "label": rank_label(r["id"])}
           for r in M.query("SELECT id, min_value, max_value FROM single_mode_rank ORDER BY id")}
    return [out[k] for k in sorted(out)]


@M.context_cache(maxsize=16)
def misc_catalog() -> dict:
    from app import scenarios
    charas = sorted({c["chara_id"] for c in uma_catalog()["cards"]})
    return {
        "charas": {c: chara_name(c) for c in charas},
        "honors": [{"id": r["id"], "name": text(65, r["id"]) or f"Title {r['id']}", "rank": r["rank"]}
                   for r in M.query("SELECT id, rank FROM honor_data ORDER BY id")],
        "nicknames": [{"id": r["id"], "name": text(130, r["id"]) or f"Epithet {r['id']}",
                       "chara": r["chara_data_id"]}
                      for r in M.query("SELECT id, chara_data_id FROM nickname ORDER BY id")],
        "conditions": [{"id": r["id"], "name": text(142, r["id"]) or f"Condition {r['id']}",
                        "type": r["effect_type"]}
                       for r in M.query("SELECT id, effect_type FROM single_mode_chara_effect ORDER BY id")],
        "scenarios": {s.scenario_id: s.name for s in scenarios.all_scenarios()},
        "ranks": ranks(),
    }


@op("catalog.core")
def core(viewer, args):
    from .common import hidden_ported
    hide_t, hide_s = hidden_ported()
    sup, umas = support_catalog(), uma_catalog()
    if hide_s:
        sup = {**sup, "cards": [c for c in sup["cards"] if c["id"] not in hide_s]}
    if hide_t:
        umas = {**umas, "cards": [c for c in umas["cards"] if c["id"] not in hide_t]}
    return {"region": region_name(), "supports": sup, "umas": umas,
            "items": item_catalog(), "misc": misc_catalog()}


@op("catalog.skills")
def skills(viewer, args):
    return skill_catalog()


@op("catalog.factors")
def factors(viewer, args):
    return factor_catalog()


@op("catalog.programs")
def programs(viewer, args):
    return program_catalog()


__all__ = ["card_name", "skill_name"]
