
from __future__ import annotations

import functools
import re
import logging

from . import epithet_conditions as ec
from . import master_data

log = logging.getLogger("uma-server")

EPITHET_STATE_KEY = "epithets_owned"

_GRADE_NAMES = {100: "g1", 200: "g2", 300: "g3", 400: "op", 700: "pre_op",
                800: "maiden", 900: "make_debut", 999: "daily"}
_GRADED = ("g1", "g2", "g3")

_AT_OR_ABOVE_FLOORS = ("g1", "g2", "g3", "op", "pre_op", "maiden")

_SURFACES = {1: "turf", 2: "dirt"}

_TIME_OF_DAY = {1: "morning", 2: "daytime", 3: "evening", 4: "nighttime"}

_BANDS = ((1400, "sprint"), (1800, "mile"), (2400, "medium"))

_MOODS = {1: "Awful", 2: "Bad", 3: "Normal", 4: "Good", 5: "Great"}
_MOOD_ORDER = {"Awful": 0, "Bad": 1, "Normal": 2, "Good": 3, "Great": 4}

_DISTANCE_MARGIN_LENGTHS = 10.0


def _mood_name(code):
    return _MOODS.get(code)


@functools.lru_cache(maxsize=None)
def _race_info(program_id: int) -> dict | None:
    if not program_id:
        return None
    prog = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (program_id,))
    if prog is None:
        return None
    inst_id = prog["race_instance_id"]
    inst = master_data.query_one(
        "SELECT race_id, time FROM race_instance WHERE id=?", (inst_id,))
    if inst is None:
        return None
    race = master_data.query_one(
        "SELECT grade, course_set FROM race WHERE id=?", (inst["race_id"],))
    if race is None:
        return None
    course = master_data.query_one(
        "SELECT distance, ground, race_track_id FROM race_course_set WHERE id=?",
        (race["course_set"],))
    distance = course["distance"] if course else 0
    band = "long"
    for limit, name in _BANDS:
        if distance <= limit:
            band = name
            break
    return {
        "instance_id": inst_id,
        "name": _text(28, inst_id),
        "short_name": _text(29, inst_id),
        "grade": _GRADE_NAMES.get(race["grade"] or 0, "other"),
        "distance": distance,
        "band": band,
        "surface": _SURFACES.get(course["ground"] if course else 0, "turf"),
        "track_id": course["race_track_id"] if course else 0,
        "track_name": _text(35, course["race_track_id"]) if course else None,
        "time_of_day": _TIME_OF_DAY.get(inst["time"] or 0, "daytime"),
    }


@functools.lru_cache(maxsize=None)
def _text(category: int, index: int):
    row = master_data.query_one(
        "SELECT text FROM text_data WHERE category=? AND [index]=?", (category, index))
    return row["text"].strip() if row and row["text"] else None


@master_data.context_cache(maxsize=4)
def catalogue() -> tuple:
    out = []
    for row in master_data.query("SELECT id FROM nickname ORDER BY id"):
        nid = row["id"]
        name = _text(130, nid)
        cond = _text(131, nid)
        if not cond:
            continue
        out.append((nid, name, cond, ec.parse(cond)))
    return tuple(out)


def unsupported_epithets() -> list:
    out = []
    for nid, name, _cond, tree in catalogue():
        for part in _walk(tree):
            if isinstance(part, ec.Unparsed):
                out.append((nid, name, part.text))
    return out


def _walk(node):
    if isinstance(node, (ec.All, ec.Any_)):
        for p in node.parts:
            yield from _walk(p)
    else:
        yield node


def build_facts(career_data: dict, chara_info: dict, full_state: dict) -> dict:
    chara_info = chara_info or {}
    history = (career_data or {}).get("race_history") or []

    f: dict = {}
    f["stat_speed"] = chara_info.get("speed") or 0
    f["stat_stamina"] = chara_info.get("stamina") or 0
    f["stat_power"] = chara_info.get("power") or 0
    f["stat_guts"] = chara_info.get("guts") or 0
    f["stat_wits"] = f["stat_wit"] = chara_info.get("wiz") or chara_info.get("wisdom") or 0
    f["fans"] = chara_info.get("fans") or 0
    f["skill_count"] = len(chara_info.get("skill_array") or ())
    f["career_complete"] = True
    f["scenario_name"] = _scenario_name(chara_info.get("scenario_id"))
    f["scenario_id"] = chara_info.get("scenario_id")
    f["implemented_scenarios"] = implemented_scenarios()

    won_names: set = set()
    run_names: set = set()
    race_win_counts: dict = {}
    race_win_years: dict = {}
    styles_won: set = set()
    surfaces_won: set = set()
    bands_won: set = set()
    courses_won: set = set()
    grade_surface_wins: dict = {}
    graded_wins_by_course: dict = {}
    worst_finish = 0
    wins = losses = graded_runs = 0
    streak = best_streak = 0
    losing_streak = longest_losing_before_win = 0

    for entry in history:
        info = _race_info(entry.get("program_id"))
        rank = entry.get("result_rank") or 0
        won = rank == 1
        f["races_run"] = f.get("races_run", 0) + 1
        worst_finish = max(worst_finish, rank)
        weather = _weather_name(entry.get("weather"))
        if weather:
            _bump(f, "runs_weather_" + weather)
        if info is None:
            continue
        grade = info["grade"]
        _bump(f, "runs_grade_" + grade)
        if grade in _GRADED:
            graded_runs += 1
        _bump(f, "runs_time_" + info["time_of_day"])
        for nm in (info["name"], info["short_name"]):
            if nm:
                run_names.add(nm)
        style = _style_name(entry.get("running_style"))

        fav = entry.get("favorite")
        if fav:
            f["best_favorite_position"] = min(f.get("best_favorite_position", 99), fav)
            f["worst_favorite_position"] = max(f.get("worst_favorite_position", 0), fav)
        mood = _mood_name(entry.get("mood"))
        if mood:
            seen = f.setdefault("_moods", [])
            seen.append(mood)

        if won:
            margin = entry.get("win_margin_lengths")
            if margin:
                f["best_win_margin"] = max(f.get("best_win_margin", 0), margin)
                f.setdefault("_margins", []).append(margin)
                if margin >= _DISTANCE_MARGIN_LENGTHS:
                    f["won_by_distance"] = True
            if fav == 1:
                f["won_as_favorite"] = True
            wins += 1
            streak += 1
            best_streak = max(best_streak, streak)
            longest_losing_before_win = max(longest_losing_before_win, losing_streak)
            losing_streak = 0
            _bump(f, "wins_grade_" + grade)
            _bump(f, "wins_surface_" + info["surface"])
            _bump(f, "wins_time_" + info["time_of_day"])
            if weather:
                _bump(f, "wins_weather_" + weather)
            if grade in _GRADED:
                _bump(f, "wins_graded")
                if info["track_name"]:
                    graded_wins_by_course[info["track_name"]] = (
                        graded_wins_by_course.get(info["track_name"], 0) + 1)
            for g in _AT_OR_ABOVE_FLOORS:
                if _grade_at_or_above(grade, g):
                    _bump(f, "wins_at_or_above_" + g)
            for nm in (info["name"], info["short_name"]):
                if nm:
                    won_names.add(nm)
                    race_win_counts[nm] = race_win_counts.get(nm, 0) + 1
                    race_win_years.setdefault(nm, []).append(
                        _year_index(entry.get("turn")))
            if style:
                styles_won.add(style)
            surfaces_won.add(info["surface"])
            bands_won.add(info["band"])
            if info["track_name"]:
                courses_won.add(info["track_name"])
            key = (grade, info["surface"])
            grade_surface_wins[key] = grade_surface_wins.get(key, 0) + 1
            f["max_race_distance"] = max(f.get("max_race_distance", 0), info["distance"])
            lo = f.get("min_race_distance")
            f["min_race_distance"] = info["distance"] if lo is None else min(lo, info["distance"])
        else:
            losses += 1
            streak = 0
            losing_streak += 1
            _bump(f, "losses_grade_" + grade)

    f["wins_total"] = wins
    f["graded_races_run"] = graded_runs
    f["worst_finish"] = worst_finish
    f["undefeated"] = bool(history) and losses == 0
    f["winning_streak"] = best_streak >= 2
    f["consecutive"] = best_streak >= 2
    f["longest_losing_streak_before_win"] = longest_losing_before_win
    f["races_won_names"] = tuple(won_names)
    f["races_run_names"] = tuple(run_names)
    f["race_win_counts"] = race_win_counts
    f["race_win_years"] = {k: sorted(set(v))
                           for k, v in race_win_years.items()}
    f["styles_won_with"] = tuple(styles_won)
    f["surfaces_won"] = tuple(surfaces_won)
    f["distance_bands_won"] = tuple(bands_won)
    f["racecourses_won"] = tuple(courses_won)
    f["graded_wins_by_course"] = graded_wins_by_course
    f["wins_grade_by_surface"] = grade_surface_wins
    f["win_rate_pct"] = int(round(100 * wins / len(history))) if history else 0
    margins = f.pop("_margins", [])
    if margins:
        f["avg_win_margin"] = sum(margins) / len(margins)
    moods = f.pop("_moods", [])
    if moods:
        f["min_race_mood"] = min(moods, key=lambda m: _MOOD_ORDER.get(m, 0))
        f["race_mood"] = f["min_race_mood"]
    if history:
        surfaces_run = {(_race_info(e.get("program_id")) or {}).get("surface")
                        for e in history}
        surfaces_run.discard(None)
        f["only_surface"] = surfaces_run.pop() if len(surfaces_run) == 1 else None

    for stat, key in (("speed", "speed"), ("stamina", "stamina"), ("power", "power"),
                      ("guts", "guts"), ("wits", "wiz")):
        lv = _training_level(career_data, key)
        if lv:
            f["training_level_" + stat] = lv
    levels = [v for k, v in f.items() if k.startswith("training_level_")]
    f["min_training_level"] = min(levels) if levels else 0

    f["independent_training"] = "idle_single_mode_run" in (full_state or {})

    fired = set(_fired_events(full_state))
    f["career_success"] = (chara_info.get("state") != _CAREER_STATE_GOAL_FAILED)
    f["irreplaceable_bond"] = bool(fired & _hot_spring_story_ids()) and f["career_success"]

    f["conditions"] = tuple(
        n for n in (_condition_name(cid)
                    for cid in (chara_info.get("chara_effect_id_array") or ()))
        if n)

    f["epithets_owned"] = tuple(
        _text(130, nid) for nid in all_owned_ids(full_state) if _text(130, nid))

    _race_facts(f, history, chara_info)

    f.update(_legacy_facts(chara_info, full_state))

    f["aptitudes"] = _aptitudes(chara_info)
    for surface, n in _surface_only_skill_counts(chara_info).items():
        f["skills_%s_only" % surface] = n

    from . import rating_formula
    from .handlers import trained_chara as _tc
    try:
        f["career_rank"] = _rank_letter(
            _tc._rank_for_score(rating_formula.get_career_score(chara_info)))
    except Exception:
        f["career_rank"] = ""
    try:
        from .handlers import single_mode_team as _smt
        all_goals = _smt._route_all_goals(
            tuple(chara_info.get("route_race_id_array") or ()))
        f["all_goals_cleared"] = bool(all_goals) and _smt._goals_cleared_count(
            chara_info, history) >= len(all_goals)
    except Exception:
        f["all_goals_cleared"] = False

    tallies = (career_data or {}).get("training_tally") or {}
    if tallies:
        total = 0
        for word, field in _STAT_WORD_FIELD:
            n = int(tallies.get(field) or 0)
            f["training_count_" + word] = n
        for field in set(field for _w, field in _STAT_WORD_FIELD):
            total += int(tallies.get(field) or 0)
        f["training_count_total"] = total
        f["training_failures"] = int(tallies.get("failures") or 0)

    from . import scenarios
    try:
        f.update(scenarios.for_chara(chara_info).snapshot(full_state) or {})
    except Exception:
        log.exception("epithets: scenario snapshot failed for scenario %s",
                      chara_info.get("scenario_id"))

    f["beat_ura_duo"] = _beat_finals_rivals(chara_info, f.get("wins_against") or {})

    per_race = _per_race_records(history)
    f["_race_view"] = lambda name: [
        {**f, **rec} for rec in per_race.get(_fold_name(name), ())]
    f["epithets_owned"] = _owned_names(f)
    return f


_OWNED_ROUNDS = 4


def _owned_names(facts: dict) -> frozenset:
    owned: frozenset = frozenset()
    for _round in range(_OWNED_ROUNDS):
        facts["epithets_owned"] = owned
        grown = set(owned)
        for _nid, name, _cond, tree in catalogue():
            if not name or name in grown:
                continue
            try:
                if tree.evaluate(facts):
                    grown.add(name)
            except Exception:
                continue
        if len(grown) == len(owned):
            break
        owned = frozenset(grown)
    facts["epithets_owned"] = owned
    return owned


def _fold_name(name: str) -> str:
    from . import epithet_conditions as _ec
    return _ec._fold(_ec._QUALIFIER.sub("", name or "")).lower().strip()


def _per_race_records(history) -> dict:
    out: dict = {}
    for entry in history:
        info = _race_info(entry.get("program_id"))
        if info is None:
            continue
        won = (entry.get("result_rank") or 0) == 1
        if not won:
            continue
        fav = entry.get("favorite")
        margin = entry.get("win_margin_lengths") or 0
        style = _style_name(entry.get("running_style"))
        mood = _mood_name(entry.get("mood"))
        overlay = {
            "races_won_names": (info["name"], info["short_name"]),
            "best_win_margin": margin,
            "avg_win_margin": margin,
            "won_by_distance": margin >= _DISTANCE_MARGIN_LENGTHS,
            "won_as_favorite": fav == 1,
            "best_favorite_position": fav or 99,
            "worst_favorite_position": fav or 0,
            "styles_won_with": (style,) if style else (),
            "surfaces_won": (info["surface"],),
            "distance_bands_won": (info["band"],),
            "racecourses_won": ((info["track_name"],) if info["track_name"] else ()),
            "race_mood": mood,
            "min_race_mood": mood,
            "max_race_distance": info["distance"],
            "min_race_distance": info["distance"],
            "max_overtakes_final_stretch": int(entry.get("overtakes") or 0),
            "max_skills_used_in_race": int(entry.get("skills_used") or 0),
        }
        for nm in (info["name"], info["short_name"]):
            if nm:
                out.setdefault(_fold_name(nm), []).append(overlay)
    return {k: tuple(v) for k, v in out.items()}


_CAREER_STATE_GOAL_FAILED = 2

_HOT_SPRING_EVENT_TITLE = "Hot Spring Getaway"
_CONDITION_NAME_CATEGORY = 142


def _condition_name(condition_id):
    return _text(_CONDITION_NAME_CATEGORY, condition_id) if condition_id else None


def _fired_events(full_state: dict) -> list:
    from . import event_engine
    return (full_state or {}).get(event_engine.FIRED_EVENTS_KEY) or []


@functools.lru_cache(maxsize=1)
def _hot_spring_story_ids() -> frozenset:
    return frozenset(
        r["index"] for r in master_data.query(
            "SELECT [index] FROM text_data WHERE category=181 AND text=?",
            (_HOT_SPRING_EVENT_TITLE,)))


def _bump(f: dict, key: str, by: int = 1) -> None:
    f[key] = f.get(key, 0) + by


def _weather_name(code):
    return {1: "sunny", 2: "cloudy", 3: "rainy", 4: "snowy"}.get(code)


def _style_name(code):
    return {1: "front_runner", 2: "pace_chaser", 3: "late_surger",
            4: "end_closer"}.get(code)


def _grade_at_or_above(grade: str, floor: str) -> bool:
    order = ["daily", "make_debut", "maiden", "pre_op", "op", "g3", "g2", "g1"]
    try:
        return order.index(grade) >= order.index(floor)
    except ValueError:
        return False


def _scenario_name(scenario_id):
    return _text(119, scenario_id) if scenario_id else None


@master_data.context_cache(maxsize=4)
def _scenario_ids_by_label() -> dict:
    out = {}
    for row in master_data.query("SELECT id FROM single_mode_scenario ORDER BY id"):
        title = _text(119, row["id"]) or ""
        flat = " ".join(title.replace("\n", " ").split()).lower()
        if flat:
            out[flat] = row["id"]
    return out


def scenario_id_for_label(label: str):
    want = " ".join((label or "").replace("\n", " ").split()).lower()
    if not want:
        return None
    by_label = _scenario_ids_by_label()
    for flat, sid in by_label.items():
        if flat == want or flat.startswith(want) or want in flat:
            return sid
    return None


def implemented_scenarios() -> set:
    from . import config
    from . import scenarios
    implemented = set(scenarios.implemented_ids())
    ids = config.get("scenario_ids") or []
    try:
        advertised = {int(i) for i in ids}
    except (TypeError, ValueError):
        return implemented
    from . import custom_support
    advertised |= custom_support.pack_scenarios()
    return (advertised & implemented) if advertised else implemented


_GROUND_TERM = {"turf": "ground_type==1", "dirt": "ground_type==2"}


@functools.lru_cache(maxsize=None)
def _skill_surface_only(skill_id: int, surface: str) -> bool:
    row = master_data.query_one(
        "SELECT condition_1, condition_2 FROM skill_data WHERE id=?", (skill_id,))
    if row is None:
        return False
    alternatives = [c for c in (row["condition_1"], row["condition_2"]) if c]
    if not alternatives:
        return False
    return all(_GROUND_TERM[surface] in c for c in alternatives)


def _surface_only_skill_counts(chara_info: dict) -> dict:
    ids = [s.get("skill_id") for s in (chara_info.get("skill_array") or ())
           if s.get("skill_id")]
    return {surface: sum(1 for sid in ids if _skill_surface_only(sid, surface))
            for surface in _GROUND_TERM}


_CENTRAL_TRACK_IDS = frozenset(range(10001, 10011))
_REGIONAL_TRACK_IDS = frozenset(range(10101, 10200))

_STANDARD_DISTANCE_STEP = 400

_TURNS_PER_YEAR = 24
_YEAR_NAMES = ("junior", "classic", "senior")


def _year_index(turn) -> int:
    idx = (int(turn or 1) - 1) // _TURNS_PER_YEAR
    return min(max(idx, 0), len(_YEAR_NAMES) - 1)


def _career_year(turn) -> str:
    idx = (int(turn or 1) - 1) // _TURNS_PER_YEAR
    return _YEAR_NAMES[min(max(idx, 0), len(_YEAR_NAMES) - 1)]


def _finals_program_ids() -> frozenset:
    from .handlers import missions as _missions
    return _missions._finale_program_ids()


def _race_facts(f: dict, history, chara_info: dict) -> None:
    central_won, regional_won = set(), set()
    regional_wins = 0
    beaten_charas = {}
    first_graded_win = None
    first_win_year_by_grade = {}
    fans_by_year = {}
    losses_at_or_below = {}
    won_finals = False

    for entry in history:
        info = _race_info(entry.get("program_id"))
        rank = entry.get("result_rank") or 0
        year = _career_year(entry.get("turn"))

        fans_after = entry.get("fans_after")
        if fans_after is not None:
            fans_by_year[year] = max(fans_by_year.get(year, 0), int(fans_after))

        if rank:
            for chara_id, rival_rank in (entry.get("rival_ranks") or {}).items():
                if rival_rank and rank < int(rival_rank):
                    cid = int(chara_id)
                    beaten_charas[cid] = beaten_charas.get(cid, 0) + 1

        if info is None:
            continue
        grade = info["grade"]
        graded = grade in _GRADED
        if rank == 1:
            if entry.get("program_id") in _finals_program_ids():
                won_finals = True
            track = info["track_id"] or 0
            if track in _REGIONAL_TRACK_IDS:
                regional_wins += 1
                if graded:
                    regional_won.add(track)
            elif track in _CENTRAL_TRACK_IDS and graded:
                central_won.add(track)
            if info["distance"]:
                kind = ("standard" if info["distance"] % _STANDARD_DISTANCE_STEP == 0
                        else "non_standard")
                _bump(f, "wins_distance_" + kind)
            if graded and first_graded_win is None:
                first_graded_win = grade
            first_win_year_by_grade.setdefault(grade, year)
        else:
            for floor in _GRADE_NAMES.values():
                if _grade_at_or_above(floor, grade):
                    losses_at_or_below[floor] = losses_at_or_below.get(floor, 0) + 1
        if rank:
            _bump(f, "place_%s_grade_%s" % (rank, grade))

    f["central_courses_won"] = tuple(central_won)
    f["regional_courses_won"] = tuple(regional_won)
    f["wins_regional"] = regional_wins
    f["won_ura_finals"] = won_finals
    f["first_graded_win_grade"] = first_graded_win or ""
    for grade, year in first_win_year_by_grade.items():
        f["first_%s_win_year" % grade] = year
    for year, fans in fans_by_year.items():
        f["fans_by_year_" + year] = fans
    f["losses_at_or_below"] = losses_at_or_below
    f["wins_after_rushed_count"] = sum(
        1 for e in history
        if (e.get("result_rank") or 0) == 1 and (e.get("rushed_count") or 0) > 0)
    f["max_skills_used_in_race"] = max(
        [int(e.get("skills_used") or 0) for e in history] or [0])
    f["max_overtakes_final_stretch"] = max(
        [int(e.get("overtakes") or 0) for e in history] or [0])
    f["wins_against"] = {_text(6, cid): n for cid, n in beaten_charas.items()
                         if _text(6, cid)}
    f["beaten_family_names"] = tuple(
        {_family_name(cid) for cid in beaten_charas if _family_name(cid)})

    offered = [r for r in (chara_info.get("route_race_id_array") or ()) if r]
    f["ran_all_races"] = bool(offered) and len(history) >= len(offered)


def _beat_finals_rivals(chara_info: dict, wins_against: dict) -> bool:
    from . import scenarios
    npc_ids = tuple(scenarios.for_chara(chara_info).finals_rival_npc_ids or ())
    if not npc_ids:
        return False
    names = []
    for npc_id in npc_ids:
        row = master_data.query_one(
            "SELECT chara_id FROM single_mode_npc WHERE id=?", (npc_id,))
        if row is None:
            return False
        names.append(_text(6, row["chara_id"]))
    return all(nm and int(wins_against.get(nm) or 0) >= 1 for nm in names)


def _legacy_records(chara_info: dict, full_state: dict) -> list:
    from .handlers import trained_chara as _tc
    roster = (full_state or {}).get(_tc.ROSTER_KEY)
    if not isinstance(roster, list):
        return []
    by_id = {c.get("trained_chara_id"): c for c in roster if isinstance(c, dict)}
    out = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        rec = by_id.get((chara_info or {}).get(key) or 0)
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _record_won_programs(rec: dict) -> list:
    programs = list(rec.get("_won_program_ids") or ())
    if not programs:
        programs = [r.get("program_id") for r in (rec.get("race_result_list") or ())
                    if (r.get("result_rank") or 0) == 1]
    return [p for p in programs if p]


def _record_won_race_names(rec: dict) -> set:
    names = set()
    for pid in _record_won_programs(rec):
        info = _race_info(pid)
        if info is None:
            continue
        for nm in (info["name"], info["short_name"]):
            if nm:
                names.add(nm)
    return names


def _record_graded_wins(rec: dict) -> int:
    return sum(1 for pid in _record_won_programs(rec)
               if (_race_info(pid) or {}).get("grade") in _GRADED)


def _record_undefeated(rec: dict) -> bool:
    results = rec.get("race_result_list") or ()
    return bool(results) and all((r.get("result_rank") or 0) == 1 for r in results)


def _rank_order(letter) -> int:
    from . import epithet_conditions as _ec
    return _ec._RANK_ORDER.get(str(letter or "").upper(), -1)


_RANK_LETTERS = ("G", "F", "E", "D", "C", "B", "A", "S", "SS", "UG", "UF", "UE")


def _rank_letter(rank_id) -> str:
    rid = int(rank_id or 0)
    if rid <= 0:
        return ""
    return _RANK_LETTERS[min((rid - 1) // 2, len(_RANK_LETTERS) - 1)]


def _family_name(chara_id) -> str:
    parts = (_text(6, chara_id) or "").split()
    return parts[0] if parts else ""


_APTITUDE_LETTERS = {1: "G", 2: "F", 3: "E", 4: "D", 5: "C", 6: "B", 7: "A", 8: "S"}
_APTITUDE_FIELDS = {
    "proper_distance_short": "sprint",
    "proper_distance_mile": "mile",
    "proper_distance_middle": "medium",
    "proper_distance_long": "long",
    "proper_ground_turf": "turf",
    "proper_ground_dirt": "dirt",
    "proper_running_style_nige": "front runner",
    "proper_running_style_senko": "pace chaser",
    "proper_running_style_sashi": "late surger",
    "proper_running_style_oikomi": "end closer",
}


def _aptitudes(rec: dict) -> dict:
    out = {}
    for field, band in _APTITUDE_FIELDS.items():
        letter = _APTITUDE_LETTERS.get(int((rec or {}).get(field) or 0))
        if letter:
            out[band] = letter
    return out


_STAT_WORD_FIELD = (("speed", "speed"), ("stamina", "stamina"), ("power", "power"),
                    ("guts", "guts"), ("wit", "wiz"), ("wits", "wiz"), ("wiz", "wiz"))


def _legacy_facts(chara_info: dict, full_state: dict) -> dict:
    legacies = _legacy_records(chara_info, full_state)
    f = {"legacy_count": len(legacies)}
    won_names = set()
    titles = set()
    families = set()
    ranks, fans = [], []
    g1_total = 0
    undefeated = 0
    stat_lists = {word: [] for word, _field in _STAT_WORD_FIELD}
    for rec in legacies:
        won_names |= _record_won_race_names(rec)
        from .handlers import trained_chara as _tc
        g1_total += _tc.g1_win_count(rec)
        ranks.append(_rank_letter(rec.get("rank")))
        fans.append(int(rec.get("fans") or 0))
        if _record_undefeated(rec):
            undefeated += 1
        for nid in rec.get("nickname_id_array") or ():
            nm = _text(130, nid)
            if nm:
                titles.add(nm)
        fam = _family_name(int(rec.get("card_id") or 0) // 100)
        if fam:
            families.add(fam)
        for word, field in _STAT_WORD_FIELD:
            stat_lists[word].append(int(rec.get(field) or 0))
    f["legacy_race_wins"] = tuple(won_names)
    f["legacy_g1_wins"] = g1_total
    f["legacy_ranks"] = tuple(r for r in ranks if r)
    f["legacy_best_rank"] = max((r for r in ranks if r), key=_rank_order, default="")
    f["legacy_fans"] = tuple(fans)
    f["legacy_titles"] = tuple(titles)
    f["legacy_family_names"] = tuple(families)
    f["legacy_undefeated_count"] = undefeated
    f["legacy_two_ungraded"] = (len(legacies) >= 2
                                and not any(_record_graded_wins(r) for r in legacies))
    for word, values in stat_lists.items():
        f["legacy_stat_" + word] = tuple(values)
        f["legacy_best_" + word] = max(values) if values else 0
    apt = {}
    for rec in legacies:
        for band, value in _aptitudes(rec).items():
            if _rank_order(value) > _rank_order(apt.get(band, "")):
                apt[band] = value
    f["legacy_aptitudes"] = apt
    return f


def _training_level(career_data: dict, key: str) -> int:
    levels = ((career_data or {}).get("data") or career_data or {}).get("facility_levels")
    if isinstance(levels, dict):
        return levels.get(key) or 0
    return 0


def earned_ids(facts: dict) -> list:
    return [nid for nid, _name, _cond, tree in catalogue() if tree.evaluate(facts)]


def all_owned_ids(full_state: dict) -> list:
    owned = (full_state or {}).get(EPITHET_STATE_KEY) or {}
    out: list = []
    for ids in owned.values():
        for i in ids:
            if i not in out:
                out.append(i)
    return out


def owned_ids(full_state: dict, chara_id) -> list:
    owned = (full_state or {}).get(EPITHET_STATE_KEY) or {}
    return list(owned.get(str(chara_id)) or ())


def award_for_career(full_state: dict, career_data: dict, chara_info: dict,
                     facts: dict | None = None) -> list:
    chara_info = chara_info or {}
    chara_id = (chara_info.get("card_id") or 0) // 100
    if not chara_id:
        return []
    try:
        if facts is None:
            facts = build_facts(career_data, chara_info, full_state)
        earned = earned_ids(facts)
    except Exception:
        log.exception("epithet evaluation failed for chara %s", chara_id)
        return []
    owned = full_state.setdefault(EPITHET_STATE_KEY, {})
    have = owned.setdefault(str(chara_id), [])
    new = [i for i in earned if i not in have]
    have.extend(new)
    if new:
        log.info("epithets: chara %s earned %s", chara_id,
                 [(i, _text(130, i)) for i in new])
    return new


_SKILL_HOMESTRETCH_HASTE = 200512
_SKILL_MILE_STRAIGHTAWAYS = 201032
_SKILL_TOP_PICK = 201672

_TRACKBLAZER_EVENT_BASE = 203800

_TRACKBLAZER_REWARDS: dict = {
    203800: ("hint", _SKILL_HOMESTRETCH_HASTE),
    203801: ("hint", _SKILL_MILE_STRAIGHTAWAYS),
    203802: ("stats", 15),
    203803: ("stats", 15),
    203804: ("stats", 15),
    203805: ("stats", 10),
    203806: ("stats", 15),
    203807: ("stats", 10),
    203808: ("hint", _SKILL_TOP_PICK),
    203809: ("stats", 15),
    203810: ("stats", 10),
    203811: ("stats", 10),
    203812: ("stats", 15),
    203813: ("stats", 10),
    203814: ("stats", 10),
    203815: ("stats", 10),
    203816: ("stats", 10),
    203817: ("stats", 10),
    203818: ("stats", 10),
    203819: ("stats", 10),
    203820: ("stats", 5),
    203821: ("stats", 10),
    203822: ("stats", 10),
    203823: ("stats", 10),
    203824: ("stats", 5),
    203825: ("stats", 5),
    203826: ("stats", 5),
    203827: ("stats", 5),
    203828: ("stats", 5),
    203829: ("stats", 5),
    203830: ("stats", 5),
    203831: ("stats", 5),
    203832: ("stats", 5),
    203833: ("stats", 5),
    203834: ("stats", 5),
    203835: ("stats", 5),
}

_STAT_EFFECT_TYPES = ("speed", "stamina", "power", "guts", "wisdom")

_ACHIEVEMENT_NAME_CATEGORY = 247
_ACHIEVEMENT_INFO_CATEGORY = 249


@functools.lru_cache(maxsize=1)
def trackblazer_run_epithets() -> tuple:
    try:
        names = [_strip_markup(r["text"]) for r in master_data.query(
            "SELECT text FROM text_data WHERE category=? ORDER BY [index]",
            (_ACHIEVEMENT_NAME_CATEGORY,))]
    except Exception:
        log.exception("trackblazer achievement name table unavailable")
        return ()
    by_name = {}
    for nid, name, _cond, tree in catalogue():
        if name:
            by_name.setdefault(name.strip(), (nid, tree))
    out = []
    for k, name in enumerate(names, 1):
        hit = by_name.get(name)
        if hit is None:
            log.warning("achievement %s (%r) matches no nickname row", k, name)
            continue
        event_id = _TRACKBLAZER_EVENT_BASE + k - 1
        row = master_data.query_one(
            "SELECT story_id FROM single_mode_story_data WHERE id=?", (event_id,))
        if row is None:
            continue
        out.append((hit[0], event_id, row["story_id"], hit[1]))
    return tuple(out)


def _strip_markup(text) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def trackblazer_achievement_id(event_id) -> int:
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return 0
    index = event_id - _TRACKBLAZER_EVENT_BASE + 1
    return index if 1 <= index <= len(trackblazer_run_epithets()) else 0


def trackblazer_reward_effects(event_id: int, rng) -> list:
    kind, value = _TRACKBLAZER_REWARDS.get(event_id, ("stats", 5))
    if kind == "hint":
        return [{"type": "skill_hint", "skill_id": value, "value": 1}]
    return [{"type": stat, "value": "+%d" % value}
            for stat in rng.sample(_STAT_EFFECT_TYPES, 2)]


def newly_completed_in_run(career_data: dict, chara_info: dict,
                           already_fired) -> list:
    fired = set(already_fired or ())
    try:
        facts = build_facts(career_data, chara_info, {})
    except Exception:
        log.exception("in-run epithet facts failed")
        return []
    out = []
    for nid, event_id, story_id, tree in trackblazer_run_epithets():
        if nid in fired:
            continue
        try:
            if tree.evaluate(facts):
                out.append((nid, event_id, story_id))
        except Exception:
            log.exception("in-run epithet %s failed to evaluate", nid)
    return out
