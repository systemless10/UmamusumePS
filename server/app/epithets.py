"""Epithet (nickname) awarding: career facts in, earned nickname ids out.

`epithet_conditions` parses each epithet's English unlock rule (text_data
category 131) into an evaluable AST. This module is the other half: it builds
the `facts` mapping those predicates read out of a finished career, decides
which epithets that career earned, and records them on the account.

WHERE THE FACTS COME FROM
-------------------------
Almost everything is DERIVED rather than separately tracked. The career already
persists `race_history` (one entry per race actually run: turn, program_id,
weather, ground_condition, running_style, result_rank, ...), and a program_id
resolves through master.mdb to everything else a condition can ask about:

    single_mode_program.race_instance_id
      -> race_instance.race_id, .time          (time of day)
      -> race.grade, .course_set               (G1/G2/.../Debut)
      -> race_course_set.distance, .ground, .race_track_id
      -> text_data 28/29 keyed by race_instance_id  (race name)

Deriving beats instrumenting here: it means an epithet added later can be
evaluated against careers that were already finished before anyone thought to
record the fact, and there is one source of truth for "what race was that".

The facts this CANNOT derive are the ones the race sim never wrote down --
winning margin, favourite position, overtakes in the final stretch, whether the
lead was held from 200m. Those keys are simply absent, every predicate reading
them is falsy, and the epithets needing them stay unearned rather than being
wrongly awarded. `unsupported_epithets()` reports exactly which ones those are.

WHEN IT RUNS
------------
At career finish (single_mode/finish), against the completed run. Epithets are
per-account and cumulative: once earned they stay, and `award_for_career`
returns only the newly-earned ones so a caller can surface them.

AND, in Trackblazer, DURING the run as well. Scenario 4 announces each epithet
the moment the race that completed it ends, as an "Achievement!" cutscene that
pays real stats -- see `trackblazer_run_epithets` below. Because the facts are
derived rather than instrumented, `build_facts` works just as well against a
PARTIAL race_history, which is what makes the in-run pass possible at all.
"""

from __future__ import annotations

import functools
import re
import logging

from . import epithet_conditions as ec
from . import master_data

log = logging.getLogger("uma-server")

EPITHET_STATE_KEY = "epithets_owned"      # {str(chara_id): [nickname_id, ...]}

# race.grade -> the token the condition prose uses.
_GRADE_NAMES = {100: "g1", 200: "g2", 300: "g3", 400: "op", 700: "pre_op",
                800: "maiden", 900: "make_debut", 999: "daily"}
_GRADED = ("g1", "g2", "g3")

# Floors "win N <grade>-or-higher races" can name. This used to be _GRADED, so
# `wins_at_or_above_op` was never tallied and every OP-floored epithet was
# permanently unearnable -- Trackblazer's "Pro Racer" ("Win 10 OP level or
# higher races") fires in 177 of 203 real careers and in none of ours.
_AT_OR_ABOVE_FLOORS = ("g1", "g2", "g3", "op", "pre_op", "maiden")

# race_course_set.ground
_SURFACES = {1: "turf", 2: "dirt"}

# race_instance.time -- matches race_parameters.RaceTime in the sim engine.
_TIME_OF_DAY = {1: "morning", 2: "daytime", 3: "evening", 4: "nighttime"}

# Distance bands, by the game's own thresholds.
_BANDS = ((1400, "sprint"), (1800, "mile"), (2400, "medium"))

_MOODS = {1: "Awful", 2: "Bad", 3: "Normal", 4: "Good", 5: "Great"}
_MOOD_ORDER = {"Awful": 0, "Bad": 1, "Normal": 2, "Good": 3, "Great": 4}

# "Win by a distance" is the widest margin the game names. Racing's own
# convention: anything past ~10 lengths is reported as "a distance".
_DISTANCE_MARGIN_LENGTHS = 10.0


def _mood_name(code):
    return _MOODS.get(code)


# ------------------------------------------------------- master lookups ----

@functools.lru_cache(maxsize=None)
def _race_info(program_id: int) -> dict | None:
    """Everything a condition can ask about the race behind `program_id`.
    Cached: master.mdb is read-only for the process lifetime."""
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


@functools.lru_cache(maxsize=1)
def catalogue() -> tuple:
    """((nickname_id, name, condition_text, parsed_condition), ...) for every
    epithet in master.mdb, parsed once."""
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
    """[(id, name, clause), ...] for every epithet carrying a clause this
    server cannot evaluate -- either the parser did not understand it, or it
    needs a fact the race sim never records. These can never be awarded; the
    list is the honest inventory of that gap."""
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


# ------------------------------------------------------------- facts -------

def build_facts(career_data: dict, chara_info: dict, full_state: dict) -> dict:
    """The flat mapping epithet_conditions' predicates evaluate against.

    Reads the career's own persisted race_history plus its final chara_info.
    Anything the sim never recorded is simply left out -- see the module
    docstring on why absence is the right behaviour."""
    chara_info = chara_info or {}
    history = (career_data or {}).get("race_history") or []

    f: dict = {}
    # --- final stats / totals ---
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

    # --- per-race walk ---
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
            # Unknown program: it still counts as a race run, but nothing
            # grade/surface specific can be claimed about it.
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

        # Margin / favourite / mood, recorded per race since 2026-09-03. Older
        # entries carry None, which stays None rather than being read as zero.
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
                    # Which career year each win landed in, so "N times in a
                    # row" can mean back-to-back YEARS of that race rather
                    # than "won it N times, and won two races in a row at some
                    # point" -- see win_race_times_in_a_row.
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
        # "with a Great Mood in all races" reads the WORST mood of the run.
        f["min_race_mood"] = min(moods, key=lambda m: _MOOD_ORDER.get(m, 0))
        f["race_mood"] = f["min_race_mood"]
    if history:
        surfaces_run = {(_race_info(e.get("program_id")) or {}).get("surface")
                        for e in history}
        surfaces_run.discard(None)
        f["only_surface"] = surfaces_run.pop() if len(surfaces_run) == 1 else None

    # --- training ---
    for stat, key in (("speed", "speed"), ("stamina", "stamina"), ("power", "power"),
                      ("guts", "guts"), ("wits", "wiz")):
        lv = _training_level(career_data, key)
        if lv:
            f["training_level_" + stat] = lv
    levels = [v for k, v in f.items() if k.startswith("training_level_")]
    f["min_training_level"] = min(levels) if levels else 0

    # --- was this run an Independent Training (the idle/auto career)? ---
    # "Independent Training" is the game's own name for the hands-off career --
    # text_data 63/361-362 describe it as the mode you "sneak in... when short on
    # time" and set a training focus and race agenda for up front. That is
    # idle_single_mode.py, and its in-flight marker is still on the state when
    # epithets are awarded (handle_finish evaluates them BEFORE
    # clear_active_career drops the per-career keys). Epithet 394's only reader.
    #
    # String literal, matching the same dodge single_mode_team._CAREER_STATE_KEYS
    # uses for this exact key -- importing idle_single_mode here would pull a
    # handler (and single_mode_team with it) into this domain module.
    f["independent_training"] = "idle_single_mode_run" in (full_state or {})

    # --- career events that fired this run ---
    # "Steamy Solidarity" (id 97) reads "Experience a moment where you feel an
    # irreplaceable bond", which is the Hot Spring Getaway event -- you need the
    # Hot Spring Ticket for it, and the run has to be seen through to a
    # successful finish (user-explained 2026-09-03). The event is per-trainee,
    # so it has one story_id per card; they all share the title, which is what
    # _hot_spring_story_ids matches on.
    fired = set(_fired_events(full_state))
    f["career_success"] = (chara_info.get("state") != _CAREER_STATE_GOAL_FAILED)
    f["irreplaceable_bond"] = bool(fired & _hot_spring_story_ids()) and f["career_success"]

    # --- career conditions held at the finish ---
    # chara_effect_id_array -> the condition NAMES the prose uses (text_data
    # 142). "with the Fast Learner condition" / "without Slow Metabolism, Skin
    # Breakout, or Slacker" both read this.
    f["conditions"] = tuple(
        n for n in (_condition_name(cid)
                    for cid in (chara_info.get("chara_effect_id_array") or ()))
        if n)

    # --- account-wide context ---
    f["epithets_owned"] = tuple(
        _text(130, nid) for nid in all_owned_ids(full_state) if _text(130, nid))

    # --- second history walk: placements, courses, finals, rivals beaten ---
    _race_facts(f, history, chara_info)

    # --- the legacies this run inherited from ---
    f.update(_legacy_facts(chara_info, full_state))

    # --- this trainee's own aptitudes and skills at the finish ---
    f["aptitudes"] = _aptitudes(chara_info)
    for surface, n in _surface_only_skill_counts(chara_info).items():
        f["skills_%s_only" % surface] = n

    # --- career rank / goals ---
    # The rank is the LETTER the prose compares, derived from the same
    # rank_score trained_chara.py grades the finished run with, so the epithet
    # and the career-result screen can never disagree about what rank this run
    # reached.
    from . import rating_formula
    from .handlers import trained_chara as _tc
    try:
        f["career_rank"] = _rank_letter(
            _tc._rank_for_score(rating_formula.get_career_score(chara_info)))
    except Exception:                                         # noqa: BLE001
        f["career_rank"] = ""
    # "Accomplish all Career goals" -- the route's own goal list, counted by the
    # same helper the in-career goal banner uses, so "all goals" means exactly
    # what the client was told all along. turn=None asks the unfiltered
    # question ("is everything done"), which is the right one at graduation.
    try:
        from .handlers import single_mode_team as _smt
        all_goals = _smt._route_all_goals(
            tuple(chara_info.get("route_race_id_array") or ()))
        f["all_goals_cleared"] = bool(all_goals) and _smt._goals_cleared_count(
            chara_info, history) >= len(all_goals)
    except Exception:                                         # noqa: BLE001
        f["all_goals_cleared"] = False

    # --- training tallies ---
    # Counted per facility as the run is played (single_mode_team records them
    # on the career); absent on a run saved before that existed, which reads as
    # "nothing counted" rather than as a zero that would falsely satisfy the two
    # never-trained / never-failed epithets below.
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

    # --- whatever this run's scenario froze for its own epithets ---
    # Taken from the live scenario state, which is still present at graduation
    # (this runs before the next career's reset). Each scenario names its facts
    # with the key the prose implies -- spirit_bursts, result_points,
    # pro_shop_coins, concert_score_dance, ... -- so they land straight into the
    # fact map with no per-scenario translation here. Scenario-PRIVATE keys are
    # prefixed with the scenario's own name and simply go unread.
    from . import scenarios
    try:
        f.update(scenarios.for_chara(chara_info).snapshot(full_state) or {})
    except Exception:                                         # noqa: BLE001
        log.exception("epithets: scenario snapshot failed for scenario %s",
                      chara_info.get("scenario_id"))

    # "Win against the duo in the URA Final" (epithet 162). Generic rather than
    # Unity-Cup-specific: a scenario declares its OWN final challengers as
    # finals_rival_npc_ids (base.Scenario), so this reads whoever those are and
    # asks whether every one of them finished behind her. URA itself declares
    # none, which correctly leaves the fact False there.
    f["beat_ura_duo"] = _beat_finals_rivals(chara_info, f.get("wins_against") or {})

    # --- per-race views, for scoped clauses (epithet_conditions.ScopedRaces) ---
    # "Win the Tulip Sho and Shuka Sho USING END CLOSER, and the Takarazuka
    # Kinen ... USING LATE SURGER" needs each modifier checked against the race
    # it was written about, not against the run as a whole. Exposed as a
    # builder so the AST can ask for one race's view without this function
    # materialising 20 dicts every evaluation.
    per_race = _per_race_records(history)
    f["_race_view"] = lambda name: [
        {**f, **rec} for rec in per_race.get(_fold_name(name), ())]
    # --- epithets this same career has already earned -----------------------
    # Some conditions are written in terms of OTHER epithets ("Obtain the Lady
    # epithet and win the Queen Elizabeth II Cup"). `has_epithet` reads this
    # key, and nothing used to fill it, so every one of those epithets was
    # permanently unearnable -- Trackblazer alone loses Legendary, Phenomenal,
    # Incredible, Goddess and Heroine that way, and Heroine fires in 90 of 203
    # real careers.
    #
    # Resolved by fixpoint rather than one pass: an epithet may reference an
    # epithet that itself references a third (Legendary -> Spring Champion,
    # and -> Stunning). Rounds are capped so a circular reference in a future
    # master.mdb settles instead of looping.
    f["epithets_owned"] = _owned_names(f)
    return f


_OWNED_ROUNDS = 4


def _owned_names(facts: dict) -> frozenset:
    """Names of every epithet `facts` already satisfies, ignoring the ones that
    cannot be decided yet. Grown a round at a time so cross-references resolve.

    `facts` is mutated in place round by round -- the predicates read
    `epithets_owned` off it, so the growing set has to be visible to them."""
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
    """{folded race name: (one facts-overlay per running of it, ...)}.

    Keyed by BOTH the full and short name, and folded the same way the race
    validator folds, so "Mile Ch." and "Mile Championship" both find it."""
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
            # This race's own running, for the modifiers that bind to it.
            "max_overtakes_final_stretch": int(entry.get("overtakes") or 0),
            "max_skills_used_in_race": int(entry.get("skills_used") or 0),
        }
        for nm in (info["name"], info["short_name"]):
            if nm:
                out.setdefault(_fold_name(nm), []).append(overlay)
    return {k: tuple(v) for k, v in out.items()}


# chara_info.state 2 = the run reached the end but FAILED its goals. The real
# game still hands over the trained uma, so it is a legacy -- but it is not
# "successfully beating the career".
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
    """Every story_id whose career-event title is the Hot Spring Getaway. One
    per trainee card, so this is a set rather than a single id."""
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


# Scenario gating ------------------------------------------------------------
# 38 epithets carry a "(<Scenario> only)" clause. The label in the prose is a
# SHORT name ("Trackblazer", "Unity Cup", "Our Grand Concert") while text_data
# 119 holds the full title ("Trackblazer: Start of the Climax", and a
# newline-wrapped "Brighter Together / Our Grand Concert"), so comparing them
# directly is False for EVERY scenario -- including a legitimate Our Grand
# Concert run, whose epithets could therefore never be earned.
#
# Resolving the label to a scenario_id fixes that, and gating on the server's
# IMPLEMENTED scenario list makes the other half deliberate rather than
# accidental (user-flagged 2026-09-03).
#
# WHAT THAT GATE LETS THROUGH NOW: all four of URA, Unity Cup, Our Grand Concert
# and Trackblazer are implemented, so their "(X only)" epithets are earnable --
# the gate is dynamic (implemented_scenarios reads the scenario registry), so
# building a scenario is all it takes to open its epithets; nothing here needed
# editing when Unity Cup and Trackblazer landed. The scenarios still NOT built
# are the ones whose labels resolve to no scenario_id at all -- "Grandmasters"
# (epithets 269-277) and "League of Heroes" (280) -- and _scenario_ok refuses a
# None id outright, so those 10 stay correctly unearnable until someone writes
# app/scenarios/grandmasters/ and app/scenarios/league_of_heroes/.

@functools.lru_cache(maxsize=1)
def _scenario_ids_by_label() -> dict:
    out = {}
    for row in master_data.query("SELECT id FROM single_mode_scenario ORDER BY id"):
        title = _text(119, row["id"]) or ""
        flat = " ".join(title.replace("\n", " ").split()).lower()
        if flat:
            out[flat] = row["id"]
    return out


def scenario_id_for_label(label: str):
    """The scenario_id an epithet's "(X only)" label refers to, or None."""
    want = " ".join((label or "").replace("\n", " ").split()).lower()
    if not want:
        return None
    by_label = _scenario_ids_by_label()
    for flat, sid in by_label.items():
        # the prose label is a prefix or a contained phrase of the full title
        if flat == want or flat.startswith(want) or want in flat:
            return sid
    return None


def implemented_scenarios() -> set:
    """Scenario ids this server actually implements.

    The scenario registry is the authority -- a scenario has a package here or
    it does not. client_config.json's scenario_ids can only NARROW that (it is
    an experiment knob for which of the implemented scenarios to advertise);
    it must never widen it, or an epithet gated on Trackblazer would become
    earnable just because someone listed 4 there."""
    from . import config
    from . import scenarios
    implemented = set(scenarios.implemented_ids())
    ids = config.get("scenario_ids") or []
    try:
        advertised = {int(i) for i in ids}
    except (TypeError, ValueError):
        return implemented
    return (advertised & implemented) if advertised else implemented


# ------------------------------------------------------- surface-only skills --
# "Obtain at least 5 skills that activate only in dirt races" (epithet 247).
# skill_data.condition_1 / condition_2 are the skill's two ALTERNATIVE trigger
# conditions (either may fire), each an &-joined list of terms. ground_type==1 is
# turf and ==2 is dirt, so a skill is surface-ONLY when every alternative it
# actually has pins that surface -- one unpinned alternative means it can fire on
# any ground, and a skill pinned to the other surface obviously does not count.
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
    """{surface: how many of her skills fire only on that surface}."""
    ids = [s.get("skill_id") for s in (chara_info.get("skill_array") or ())
           if s.get("skill_id")]
    return {surface: sum(1 for sid in ids if _skill_surface_only(sid, surface))
            for surface in _GROUND_TERM}


# --------------------------------------------------- courses and distances ---
# race_track ids split cleanly: 10001-10010 are the ten CENTRAL (JRA) courses --
# Sapporo, Hakodate, Niigata, Fukushima, Nakayama, Tokyo, Chukyo, Kyoto, Hanshin,
# Kokura, which is exactly the set epithet 23 means by "each of the 10 central
# racecourses" -- 101xx are the REGIONAL ones (Oi, Kawasaki, Funabashi, Morioka)
# and 10201 is Longchamp, which is neither.
_CENTRAL_TRACK_IDS = frozenset(range(10001, 10011))
_REGIONAL_TRACK_IDS = frozenset(range(10101, 10200))

# "Standard distance" is the 根幹距離 / non-根幹距離 split: a standard distance is a
# whole multiple of 400m (1600, 2000, 2400, ...), everything else is
# non-standard. Epithets 208/209 are the only readers.
_STANDARD_DISTANCE_STEP = 400

# Career calendar: 24 turns a year, Junior 1-24, Classic 25-48, Senior 49+ (the
# URA Finals turns 73-78 are still the senior year). The same arithmetic
# single_mode_team._year_permissions uses.
_TURNS_PER_YEAR = 24
_YEAR_NAMES = ("junior", "classic", "senior")


def _year_index(turn) -> int:
    """0/1/2 for junior/classic/senior. Same split as _career_year, as a number
    so consecutive years can be compared."""
    idx = (int(turn or 1) - 1) // _TURNS_PER_YEAR
    return min(max(idx, 0), len(_YEAR_NAMES) - 1)


def _career_year(turn) -> str:
    """The career year a turn falls in, as the word the prose uses."""
    idx = (int(turn or 1) - 1) // _TURNS_PER_YEAR
    return _YEAR_NAMES[min(max(idx, 0), len(_YEAR_NAMES) - 1)]


def _finals_program_ids() -> frozenset:
    """The URA Finale FINALS programs -- single_mode_race_group 10003, the
    round-3 set (41 distance/ground variants, one picked per career).

    DELEGATED to missions.py rather than re-queried here: "won the URA Finale"
    is also a mission condition (100019), and the two must never drift into
    disagreeing about which programs count. That module owns the definition and
    its own cache."""
    from .handlers import missions as _missions
    return _missions._finale_program_ids()


def _race_facts(f: dict, history, chara_info: dict) -> None:
    """The per-race facts that needed a SECOND walk of the history -- placements
    by grade, the course/distance families, the finals, and the rivals beaten.

    Split out from build_facts' own walk rather than folded into it: every one of
    these reads a race attribute the first walk already resolved, but they are
    each gated on something it does not track (a non-winning placement, a track
    id band, the finals program set, the other runners' finish orders)."""
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

        # Fan high-water per year, for "obtain at least N fans in classic year".
        # _append_race_history records the trainee's fan total AFTER each race;
        # an older entry without it simply does not raise the mark.
        fans_after = entry.get("fans_after")
        if fans_after is not None:
            fans_by_year[year] = max(fans_by_year.get(year, 0), int(fans_after))

        # Rivals beaten in THIS race: the scripted runners who finished behind
        # her. rival_ranks is {chara_id: finish order}, recorded per race since
        # the secret-event work, and is the only record of who else was on the
        # track -- mobs have no card_id and never appear in it.
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
            # "without ever losing at the OP level or below" -- a LOSS at or
            # below the named level, which is the whole condition (epithet 34).
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
    # How the races were RUN, recorded per race by the simulation. A race with
    # no simulation behind it carries None and simply does not contribute,
    # rather than reading as a zero.
    f["wins_after_rushed_count"] = sum(
        1 for e in history
        if (e.get("result_rank") or 0) == 1 and (e.get("rushed_count") or 0) > 0)
    f["max_skills_used_in_race"] = max(
        [int(e.get("skills_used") or 0) for e in history] or [0])
    # Most runners passed over the final straight in any one race. Also exposed
    # per race in _per_race_records, because the prose attaches it to a NAMED
    # race as often as to the run ("win the Satsuki Sho by overtaking at least 4
    # runners in the final stretch").
    f["max_overtakes_final_stretch"] = max(
        [int(e.get("overtakes") or 0) for e in history] or [0])
    f["wins_against"] = {_text(6, cid): n for cid, n in beaten_charas.items()
                         if _text(6, cid)}
    f["beaten_family_names"] = tuple(
        {_family_name(cid) for cid in beaten_charas if _family_name(cid)})

    # "Run in ALL races" (epithets 44/175) -- every race this route ever offered
    # the trainee, which is the route's own race list.
    offered = [r for r in (chara_info.get("route_race_id_array") or ()) if r]
    f["ran_all_races"] = bool(offered) and len(history) >= len(offered)


def _beat_finals_rivals(chara_info: dict, wins_against: dict) -> bool:
    """Did she finish ahead of EVERY one of this scenario's declared final
    challengers? Empty declaration = False: URA names none, so the fact stays
    off there rather than being vacuously true for every URA run."""
    from . import scenarios
    npc_ids = tuple(scenarios.for_chara(chara_info).finals_rival_npc_ids or ())
    if not npc_ids:
        return False
    names = []
    for npc_id in npc_ids:
        row = master_data.query_one(
            "SELECT chara_id FROM single_mode_npc WHERE id=?", (npc_id,))
        if row is None:
            return False          # a challenger we cannot identify: do not claim it
        names.append(_text(6, row["chara_id"]))
    return all(nm and int(wins_against.get(nm) or 0) >= 1 for nm in names)


# --------------------------------------------------------------- legacies ---
# "Receive Inspiration from a Legacy Umamusume with ..." -- THE two parents this
# run inherited from, resolved against the saved roster. 30 of the 250 epithets
# are gated on these facts and not one of them could be earned before, because
# build_facts never produced a single legacy_* key.
#
# A legacy is a finished trained_chara record, so every fact below is read off
# the SAME fields trained_chara.py writes at graduation -- stats, fans, rank,
# race wins, nickname_id_array. Nothing is inferred about a parent that the
# parent's own record does not already state.

def _legacy_records(chara_info: dict, full_state: dict) -> list:
    """The run's two parents as roster records, in slot order, missing ones
    dropped. chara_info carries the ids (single_mode_team._career_chara_info
    copies them off start_chara at career start)."""
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
    """The program ids a finished run WON. trained_chara.py banks these as
    _won_program_ids; race_result_list is the fallback for records written
    before that key existed."""
    programs = list(rec.get("_won_program_ids") or ())
    if not programs:
        programs = [r.get("program_id") for r in (rec.get("race_result_list") or ())
                    if (r.get("result_rank") or 0) == 1]
    return [p for p in programs if p]


def _record_won_race_names(rec: dict) -> set:
    """Every race name this finished run won, both full and short."""
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
    """Wins at graded level (G3 and up) in a finished run."""
    return sum(1 for pid in _record_won_programs(rec)
               if (_race_info(pid) or {}).get("grade") in _GRADED)


def _record_undefeated(rec: dict) -> bool:
    results = rec.get("race_result_list") or ()
    return bool(results) and all((r.get("result_rank") or 0) == 1 for r in results)


def _rank_order(letter) -> int:
    from . import epithet_conditions as _ec
    return _ec._RANK_ORDER.get(str(letter or "").upper(), -1)


# single_mode_rank.id -> the BARE career-rank letter. The table itself holds only
# score bands (the letters live client-side), but missions.py pinned the
# numbering against text_data category 67's own mission descriptions for
# condition_type 100004: id 3=F, 4=F+, 5=E, 6=E+ ... 15=S, 16=S+, 17=SS. So the
# letters climb one per TWO ids starting from G at id 1, and the "+" half-steps
# collapse onto the same letter -- which is what the epithet prose wants, since
# it only ever says "rank S or higher", never "S+".
_RANK_LETTERS = ("G", "F", "E", "D", "C", "B", "A", "S", "SS", "UG", "UF", "UE")


def _rank_letter(rank_id) -> str:
    """The letter for a single_mode_rank.id, or "" when there is no rank.

    Above SS+ (id 18) the real ladder's naming was never captured, so this keeps
    stepping one letter per two ids and clamps at the top letter the condition
    evaluator knows. Only the ORDER matters to every reader (both "X or higher"
    and "X or lower" compare through _RANK_ORDER), so a monotone extension is
    correct in both directions even where the NAME would be a guess."""
    rid = int(rank_id or 0)
    if rid <= 0:
        return ""
    return _RANK_LETTERS[min((rid - 1) // 2, len(_RANK_LETTERS) - 1)]


def _family_name(chara_id) -> str:
    """A character's FAMILY name -- the first word of her full name ("Mejiro
    McQueen" -> "Mejiro"). text_data category 6 is the chara name table."""
    parts = (_text(6, chara_id) or "").split()
    return parts[0] if parts else ""


# ------------------------------------------------------------- aptitudes ----
# chara_info and trained_chara both store each aptitude as 1-8; the prose
# compares letters. The band words are the ones the epithet text uses, which is
# what the evaluator keys on ("at least S in Front Runner aptitude" -> "front
# runner"; "Sprint-Distance Aptitude A or higher" -> "sprint").
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
    """{band word: letter} for every aptitude on a chara_info or a finished
    trained_chara -- both carry the same proper_* field names."""
    out = {}
    for field, band in _APTITUDE_FIELDS.items():
        letter = _APTITUDE_LETTERS.get(int((rec or {}).get(field) or 0))
        if letter:
            out[band] = letter
    return out


# The five stat words the prose uses, mapped to the field that holds them. "wit"
# / "wits" / "wiz" all name the same stat and all three appear -- in the prose,
# in the evaluator's key-building, and in the record's own field name.
_STAT_WORD_FIELD = (("speed", "speed"), ("stamina", "stamina"), ("power", "power"),
                    ("guts", "guts"), ("wit", "wiz"), ("wits", "wiz"), ("wiz", "wiz"))


def _legacy_facts(chara_info: dict, full_state: dict) -> dict:
    """Every legacy_* fact the condition evaluator reads, from the two parents.

    The per-stat keys are LISTS (one value per legacy), not totals: the prose is
    always "Receive Inspiration from 2 Legacy Umamusume with at least 1,200
    Speed", i.e. a COUNT of legacies each clearing the bar. legacy_best_* is the
    max, for the one clause that asks a single legacy for a win AND a stat."""
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
        # win_saddle_id_array IS the G1-win set trained_chara.py builds, so its
        # length is the count -- no re-joining master.mdb for it.
        g1_total += len(rec.get("win_saddle_id_array") or ())
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
    # "two Legacies with no career graded race wins" (epithet 9)
    f["legacy_two_ungraded"] = (len(legacies) >= 2
                                and not any(_record_graded_wins(r) for r in legacies))
    for word, values in stat_lists.items():
        f["legacy_stat_" + word] = tuple(values)
        f["legacy_best_" + word] = max(values) if values else 0
    # Aptitudes, best across the legacies.
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


# ----------------------------------------------------------- awarding ------

def earned_ids(facts: dict) -> list:
    """Nickname ids whose parsed condition evaluates true against `facts`."""
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
    """Evaluate the finished career and record every epithet it earned against
    the trainee's chara_id. Returns the NEWLY earned ids (already-owned ones are
    not repeated). Mutates full_state; the caller owns the save."""
    chara_info = chara_info or {}
    chara_id = (chara_info.get("card_id") or 0) // 100
    if not chara_id:
        return []
    try:
        if facts is None:
            facts = build_facts(career_data, chara_info, full_state)
        earned = earned_ids(facts)
    except Exception:
        # An epithet pass must never take the career-finish flow down with it.
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


# ------------------------------------ Trackblazer in-run "Achievement!" -----
#
# Scenario 4 does not wait for graduation. The moment a race completes an
# epithet, the client is served an "Achievement!" cutscene from the 2038xx
# band, and that cutscene PAYS -- two random stats, or a skill hint. Over 1,741
# real Trackblazer careers in captures/bot_logs that is 11,355 events, a median
# of 6.5 per career, and only 80 careers that see none at all. Roughly 130 stat
# points a run, which we were granting none of.
#
# WHICH EPITHETS. master.mdb answers this exactly, no guessing: the band is 36
# rows (203800..203835) and `nickname` has 36 rows with scenario_id=4 AND
# receive_condition_type=3. The other four scenario-4 epithets (Leading the
# Charge, Product Power, Climax King, Moneymaker) are receive_condition_type=1
# -- "complete a Career playthrough having..." -- so they cannot be announced
# mid-run, and they are exactly the four the wiki lists with reward "None".
# 36 == 36, and the split falls out of master rather than out of a reading of
# the wiki's Gold/Silver/Bronze tabs.
#
# WHICH ROW IS WHICH EPITHET. Nothing in master.mdb joins the two -- every
# column of every table was scanned for a reference into the band and only
# single_mode_story_data.id comes back. The mapping below is positional (both
# lists in ascending id, 203800+k <-> the k-th in-run epithet) and is
# corroborated, not assumed:
#
#   * REWARD. Measured per event id from the stat deltas across each ack in
#     the corpus -- ZERO variance, one fixed reward per id (n up to 1626).
#     With the client's achievement order (below), ALL 25 ids ever seen match
#     the wiki's reward for the epithet they land on -- which is itself a
#     check on that order, since the two sources are independent.
#   * IMPLICATION. "Mile a Minute" strictly contains "Breakneck Miler" (its
#     race list is a superset), and 203801 implies 203802 in 186/186 careers.
#     Likewise "Legendary" requires the Spring Champion and Fall Champion
#     epithets, which between them force Shield Bearer -- and 203800 implies
#     exactly {203815, 203816, 203817} in 32/32 careers, the three slots this
#     mapping assigns to those three epithets.
#   * FREQUENCY. Nested pairs come out in the right order (203809 is a strict
#     subset of 203810, 40/40; 203813 of 203814, 383/383).
#
# WHAT IS STILL OPEN, honestly: the bot corpus runs essentially ONE route (188
# of the 203 all-win careers share a preset), so epithets that always co-fire
# on that route cannot be told apart by any set- or turn-based method, and a
# per-turn replay disagrees with the positional order inside the trio
# {Shield Bearer, Spring Champion, Fall Champion}. That trio shares one story
# id AND one reward, so an internal permutation is invisible to the player --
# only the opaque event_id would differ. Rows marked `m?` are that trio.
#
# Stat rewards are "2 random stats +N": the corpus shows exactly two stats
# moving, by the same amount, every time.

# skill_id for the three hint rewards (text_data category 47).
_SKILL_HOMESTRETCH_HASTE = 200512
_SKILL_MILE_STRAIGHTAWAYS = 201032      # "Mile Straightaways (circle)"
_SKILL_TOP_PICK = 201672

_TRACKBLAZER_EVENT_BASE = 203800

# event_id -> reward. ("stats", N) is two random stats +N; ("hint", skill_id).
# `m` = measured from the corpus, `w` = from the wiki (id never seen in it).
_TRACKBLAZER_REWARDS: dict = {
    203800: ("hint", _SKILL_HOMESTRETCH_HASTE),   # m  Legendary
    203801: ("hint", _SKILL_MILE_STRAIGHTAWAYS),  # m  Mile a Minute
    203802: ("stats", 15),                        # m  Breakneck Miler
    203803: ("stats", 15),                        # m  Phenomenal
    203804: ("stats", 15),                        # m  Incredible
    203805: ("stats", 10),                        # m  Stunning
    203806: ("stats", 15),                        # w  Sprint Speedster
    203807: ("stats", 10),                        # w  Sprint Go-Getter
    203808: ("hint", _SKILL_TOP_PICK),            # w  Dirt G1 Dominator
    203809: ("stats", 15),                        # m  Dirt G1 Powerhouse
    203810: ("stats", 10),                        # m  Dirt G1 Star
    203811: ("stats", 10),                        # m  Dirt G1 Achiever
    203812: ("stats", 15),                        # m  Goddess
    203813: ("stats", 10),                        # m  Heroine
    203814: ("stats", 10),                        # m  Lady
    203815: ("stats", 10),                        # m  Spring Champion
    203816: ("stats", 10),                        # m  Fall Champion
    203817: ("stats", 10),                        # m  Shield Bearer
    203818: ("stats", 10),                        # w  Eat My Dust
    203819: ("stats", 10),                        # w  Playing Dirty
    203820: ("stats", 5),                         # m  Dirty Work
    203821: ("stats", 10),                        # m  Standard Distance Leader
    203822: ("stats", 10),                        # m  Non-Standard Distance Leader
    203823: ("stats", 10),                        # w  Dirt Sprinter
    203824: ("stats", 5),                         # w  Umatastic
    203825: ("stats", 5),                         # w  Kicking Up Dust
    203826: ("stats", 5),                         # m  Globe-Trotter
    203827: ("stats", 5),                         # m  Junior Jewel
    203828: ("stats", 5),                         # m  Turf Tussler
    203829: ("stats", 5),                         # w  Dirt Dancer
    203830: ("stats", 5),                         # m  Pro Racer
    203831: ("stats", 5),                         # w  Hokkaido Hotshot
    203832: ("stats", 5),                         # m  Tohoku Top Dog
    203833: ("stats", 5),                         # m  Kanto Conqueror
    203834: ("stats", 5),                         # m  West Japan Whiz
    203835: ("stats", 5),                         # w  Kokura Constable
}

_STAT_EFFECT_TYPES = ("speed", "stamina", "power", "guts", "wisdom")

# THE ORDER IS THE CLIENT'S OWN, and it is authoritative.
#
# The 2038xx band is served with an ACHIEVEMENT ID -- a 1-based index the
# client uses to fill the <achievement_name> / <achievement_info> tags in the
# cutscene (TextUtil.ReplaceAchievementTag, MasterString categories
# SingleModeAchievementName=247 and SingleModeAchievementInfo=249). Both
# categories hold exactly 36 rows, indexed 1..36, and index k names the epithet
# that row k announces. So the client ships the epithet<->row mapping, and this
# reads it instead of guessing:
#
#     achievement_id k  <->  event_id 203800 + k - 1  <->  text_data(247, k)
#
# This REPLACED a mapping inferred from the bot-log corpus, which had the
# 7..18 stretch wrong (it ran in nickname-id order, which differs from the
# client's order for exactly those twelve). The correction is self-checking:
# with this order, ALL 25 event ids whose reward was measured from the corpus
# now agree with the wiki's reward for the epithet they land on, where the old
# order disagreed on four. It also keeps every structural fact the corpus gave
# up -- 203809 is a strict subset of 203810 (Dirt G1 Powerhouse inside Dirt G1
# Star, 40/40), 203813 inside 203814 (Heroine inside Lady, 383/383), and
# Legendary at 203800 implying exactly {203815, 203816, 203817} = Spring
# Champion, Fall Champion, Shield Bearer (32/32).
_ACHIEVEMENT_NAME_CATEGORY = 247
_ACHIEVEMENT_INFO_CATEGORY = 249


@functools.lru_cache(maxsize=1)
def trackblazer_run_epithets() -> tuple:
    """((nickname_id, event_id, story_id, parsed_condition), ...) for every
    epithet Trackblazer announces DURING the run, in the CLIENT's achievement
    order (see the note above).

    Empty rather than raising if master.mdb is missing the band -- an absent
    table must not take the race turn down with it."""
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
    """text_data 247/249 carry Unity rich-text colour tags around the value."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def trackblazer_achievement_id(event_id) -> int:
    """The 1-based achievement index the client needs to fill
    <achievement_name>/<achievement_info>. 0 for anything outside the band --
    the wire field is only meaningful on a response that shows one."""
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return 0
    index = event_id - _TRACKBLAZER_EVENT_BASE + 1
    return index if 1 <= index <= len(trackblazer_run_epithets()) else 0


def trackblazer_reward_effects(event_id: int, rng) -> list:
    """The effect list an "Achievement!" event pays, in event_engine's own
    format. `rng` is passed in so the caller owns determinism."""
    kind, value = _TRACKBLAZER_REWARDS.get(event_id, ("stats", 5))
    if kind == "hint":
        return [{"type": "skill_hint", "skill_id": value, "value": 1}]
    # Two DISTINCT stats, same amount each -- the corpus never shows one stat
    # taking both halves.
    return [{"type": stat, "value": "+%d" % value}
            for stat in rng.sample(_STAT_EFFECT_TYPES, 2)]


def newly_completed_in_run(career_data: dict, chara_info: dict,
                           already_fired) -> list:
    """The epithets this career has just satisfied and not yet announced.

    Evaluated against the race_history AS IT STANDS, so calling it after each
    race walks the run forward one race at a time. Returns
    [(nickname_id, event_id, story_id), ...] in master id order -- the caller
    chains one event per entry, which is what the real server does when one
    race completes several epithets at once."""
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
