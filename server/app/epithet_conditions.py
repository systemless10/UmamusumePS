"""Epithet (nickname) unlock conditions, parsed out of their English prose.

WHY THIS EXISTS
---------------
master.mdb's `nickname` table (250 rows) carries no machine-readable unlock
rule. Its `receive_condition_type` is a coarse bucket (3 = ordinary, 1/2 =
career-completion variants) and nothing else in the schema says what earns an
epithet. The ONLY statement of each rule is English prose in text_data
category 131, paired with the epithet's display name in category 130:

    id 1   "Rainy Runner"   :: Run in Rainy weather at least 4 times and win 3 times
    id 52  "Triple Crown"   :: Win the Satsuki Sho, Japanese Derby, and Kikuka Sho
    id 103 "Speed Queen"    :: Attain at least 1,200 Speed

So the prose IS the data source and has to be parsed into something evaluable.
This module is that parser plus the condition type it produces.

THE GRAMMAR
-----------
A condition is a conjunction of CLAUSES. Each clause is a BASE (a countable
achievement) optionally carrying MODIFIERS that constrain it ("as favorite",
"using Front Runner", "by at least 5 lengths", "while undefeated"). Embedded
newlines and the <n> token are cosmetic line-wrapping and carry no meaning.

Clause splitting is the subtle part. ", and " / ", " / " and " join clauses,
but the same separators appear INSIDE a race list -- "Win the Satsuki Sho,
Japanese Derby, and Kikuka Sho" is ONE clause naming three races, not three
clauses. Splitting naively turns two thirds of the corpus into bare race names
that match nothing. So a separator only splits when what follows actually
starts a new clause (see _CLAUSE_LEAD).

DESIGN
------
`Condition` is a small AST: All/Any over leaf `Predicate`s, each a named kind
plus parameters, evaluated against a flat `facts` mapping produced by the
career tracker. Parsing is deliberately CONSERVATIVE: a clause no rule matches
becomes `Unparsed`, which evaluates False forever and is reported by
`coverage()`. An epithet is awarded only when every clause parsed AND
evaluated true -- a rule this module cannot read must never silently award.

Numbers carry thousands separators ("550,000 fans", "1,200 Speed") and every
spelled threshold is inclusive ("at least N", "N or higher").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------- AST ------

@dataclass(frozen=True)
class Predicate:
    kind: str
    params: tuple = ()

    def evaluate(self, facts: dict) -> bool:
        fn = _EVALUATORS.get(self.kind)
        return bool(fn(facts, *self.params)) if fn else False


@dataclass(frozen=True)
class Unparsed:
    """A clause no rule understood. Never true -- see the module docstring."""
    text: str

    def evaluate(self, facts: dict) -> bool:
        return False


@dataclass(frozen=True)
class ScopedRaces:
    """A race list whose modifiers bind to THOSE races, not to the run.

    "Win the Tulip Sho and Shuka Sho using End Closer, and the Takarazuka
    Kinen ... using Late Surger or End Closer" is two race groups with
    different styles. Evaluating the modifiers globally would let a style used
    in any race satisfy either group, which is both wrong and unable to
    express the second group at all.

    Each constraint is evaluated against a per-race VIEW: the run-level facts
    overlaid with that one race's own margin, favourite, style, mood and so on
    (see facts_for_race). Run-level predicates inside the group -- "while
    undefeated" -- therefore still read the run, because the view inherits it.
    """
    races: tuple
    constraints: tuple = field(default_factory=tuple)

    def evaluate(self, facts: dict) -> bool:
        builder = facts.get("_race_view")
        for name in self.races:
            views = builder(name) if builder else []
            if not any(all(c.evaluate(v) for c in self.constraints) for v in views):
                return False
        return True


@dataclass(frozen=True)
class All:
    parts: tuple = field(default_factory=tuple)

    def evaluate(self, facts: dict) -> bool:
        return all(p.evaluate(facts) for p in self.parts)


@dataclass(frozen=True)
class Any_:
    parts: tuple = field(default_factory=tuple)

    def evaluate(self, facts: dict) -> bool:
        return any(p.evaluate(facts) for p in self.parts)


# --------------------------------------------------------- normalizing -----

_WRAP = re.compile(r"(?:\\n|<n>|\n)+")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _SPACE.sub(" ", _WRAP.sub(" ", text or "")).strip()


def _num(s: str) -> int:
    return int(str(s).replace(",", "").strip())


def _names(blob: str) -> list[str]:
    """Split a race list. Trailing scenario/era qualifiers stay attached to
    their race ("Arima Kinen (Senior Year)") -- they select a specific running
    of that race and are part of its identity here."""
    # Strip whitespace only -- NOT trailing periods. Race names are routinely
    # abbreviated in this text ("Hanshin J.F.", "Mile Ch.", "NHK Mile C.") and
    # eating the final dot turns every one of them into an unknown race.
    return [n.strip() for n in re.split(r",\s*(?:and\s+)?|\s+and\s+", blob) if n.strip()]


# A separator only ends a clause when a new clause actually follows. Every
# clause opens with one of these; anything else after a comma is a list item.
_CLAUSE_LEAD = (r"(?:Win|win|Run|run|Complete|complete|Obtain|obtain|Attain|attain|"
                r"Acquire|acquire|Place|place|Reach|reach|Perform|perform|Receive|receive|"
                r"Accomplish|accomplish|Use|use|Experience|experience|Trigger|trigger|"
                # "End the Grand Concert", but NOT the running style "End
                # Closer" -- a bare End|end here split every style list.
                r"Get|get|End the|end the|Put|put|Activate|activate|Achieve|achieve|As|as|"
                r"having|while|including|have|then|with(?:out)?\s|and\s+(?:win|complete|obtain)|"
                # "..., and THE Takarazuka Kinen ... using Late Surger" starts a
                # second scoped race group. Safe as a split point because items
                # WITHIN a race list never repeat "the" ("the Satsuki Sho,
                # Japanese Derby, and Kikuka Sho"), so ", and the " only ever
                # appears between groups.
                r"the\s+\w)")

_CLAUSE_SPLIT = re.compile(r"(?:,\s+and\s+|,\s+|\s+and\s+)(?=%s)" % _CLAUSE_LEAD)


# --------------------------------------------------------- modifiers -------
# Stripped off a clause before its base is matched; each contributes its own
# predicate. Order matters only in that longer forms precede shorter ones.

_STYLE = r"(?:Front Runner|Pace Chaser|Late Surger|End Closer)"
_MOOD = r"(?:Great|Good|Normal|Bad|Awful)"
_STATS = r"(?:Speed|Stamina|Power|Guts|Wits|Wit)"
_GRADE = r"(?:G1|G2|G3|OP|Pre-OP|Maiden|Make Debut)"
# Career conditions (chara_effect_id_array). Named here so "without A, B, or C"
# only matches an actual condition list and cannot swallow arbitrary prose.
_COND = (r"(?:Night Owl|Slacker|Skin Outbreak|Skin Breakout|Slow Metabolism|Migraine|"
         r"Practice Poor|Fast Learner|Charming|Hot Topic|Practice Perfect|"
         r"Under the Weather|Shining Brightly)")

_MODIFIERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\s+by an average of at least (\d+) lengths"), "avg_margin_at_least"),
    (re.compile(r"\s+by at least (\d+) lengths?"), "margin_at_least"),
    (re.compile(r"\s+as at least (\d+)(?:st|nd|rd|th) favou?rite"), "favorite_at_least"),
    (re.compile(r"\s+as the (\d+)(?:st|nd|rd|th) to (\d+)(?:st|nd|rd|th) favou?rite"),
     "favorite_between"),
    (re.compile(r"\s+as (?:the )?favou?rite"), "as_favorite"),
    # A four-style list means "won one with EACH", not "any of these" -- match
    # the full list before the one/two-style form so it isn't truncated.
    (re.compile(r"\s+(?:using|with|as a|as) (%s), (%s), (%s), and (%s)"
                % (_STYLE, _STYLE, _STYLE, _STYLE)), "running_style_each"),
    (re.compile(r"\s+(?:using|with|as a|as) (%s)(?: or (%s))?" % (_STYLE, _STYLE), re.I),
     "running_style"),
    (re.compile(r",? all on (turf|dirt)"), "all_races_surface"),
    (re.compile(r"\s+and never failing a Training", re.I), "never_failed_training_mod"),
    (re.compile(r"\s+without having won a (turf|dirt) (%s) before" % _GRADE),
     "no_prior_surface_grade_win"),
    (re.compile(r"\s+and\s*$"), "noop_dangling_and"),
    (re.compile(r"\s+while maintaining a winning streak"), "winning_streak"),
    # \s* not \s+ : these also stand alone as their own clause ("..., while
    # undefeated"), where there is no preceding space to consume.
    (re.compile(r"\s*while undefeated"), "undefeated"),
    (re.compile(r"\s+undefeated"), "undefeated"),
    (re.compile(r"\s+in (junior|classic|senior) year"), "in_year"),
    (re.compile(r"\s*while running in all races with an? (%s) [Mm]ood" % _MOOD),
     "mood_all_races"),
    (re.compile(r"\s+without training a single time"), "never_trained_mod"),
    # \s* : also stands alone as its own clause after splitting.
    (re.compile(r"\s*with at least (\d+) wins? in (Rainy|Cloudy|Snowy|Sunny) weather"),
     "wins_in_weather"),
    (re.compile(r"\s+with an? (%s) [Mm]ood in all races" % _MOOD), "mood_all_races"),
    (re.compile(r"\s+when (\d+)(?:st|nd|rd|th) favou?rite or below"), "favorite_at_worst"),
    (re.compile(r"\s+by overtaking at least (\d+) runners in the final stretch"),
     "overtake_at_least"),
    (re.compile(r"\s+while maintaining the lead (\d+) meters from the start until the finish"),
     "led_from_start"),
    (re.compile(r"\s+by a distance"), "margin_distance"),
    (re.compile(r"\s+by at least (\d+) 1/2 lengths"), "margin_at_least_half"),
    (re.compile(r"\s+without ever losing at the (\w+) level or below"), "no_loss_at_or_below"),
    (re.compile(r"\s+without failing a single training", re.I), "never_failed_training_mod"),
    (re.compile(r"\s*with the (.+?) condition"), "has_condition"),
    (re.compile(r"\s*without (%s(?:, %s)*(?:,? or %s)?)\b"
                % (_COND, _COND, _COND), re.I), "without_conditions"),
    (re.compile(r"\s+against an Umamusume with the (\w+) name"), "beat_family_name"),
    (re.compile(r"\s+with an? (%s) [Mm]ood or lower" % _MOOD), "mood_at_most"),
    (re.compile(r"\s+with a [Mm]ood of (%s) or lower" % _MOOD), "mood_at_most"),
    (re.compile(r"\s+with an? (%s) [Mm]ood" % _MOOD), "mood_exactly"),
    (re.compile(r"\s+with a [Mm]ood of (%s)" % _MOOD), "mood_exactly"),
    (re.compile(r"\s+of ([\d,]+) meters or less"), "distance_at_most"),
    (re.compile(r"\s+of ([\d,]+) meters or more"), "distance_at_least"),
    (re.compile(r"\s+over ([\d,]+) meters or more"), "distance_at_least"),
    (re.compile(r"\s+over ([\d,]+) meters"), "distance_at_least"),
    (re.compile(r"\s+in (Rainy|Cloudy|Snowy|Sunny) weather"), "weather_is"),
    (re.compile(r"\s+at (?:the )?(.+?) [Rr]acecourse"), "at_racecourse"),
    # "(Trackblazer only)", "(Our Grand Concert only)", ... -- a scenario gate
    # on the whole clause. Captured generically: the scenario list grows with
    # every release and a hardcoded set silently drops the newest one's
    # epithets on the floor.
    # [^()] matters: with a plain (.+?) this spans from an earlier race
    # qualifier all the way to " only)", so "Win the Osaka Hai, Tenno Sho
    # (Spring), and Takarazuka Kinen (Trackblazer only)" lost its whole race
    # list instead of just the scenario gate.
    (re.compile(r"\s*\(([^()]+?) only\)\.?"), "scenario_only"),
    (re.compile(r"\s+in a row"), "consecutive"),
]

_SURFACE = re.compile(r"\s+(turf|dirt)\s+")
_BAND = re.compile(r"\s+(sprint|mile|medium|long)\s+")


def _strip_modifiers(clause: str):
    """Peel every recognised modifier off `clause`, returning
    (remaining_text, [Predicate, ...])."""
    found = []
    text = clause
    for pattern, kind in _MODIFIERS:
        m = pattern.search(text)
        while m:
            found.append(Predicate(kind, tuple(g for g in m.groups() if g is not None)))
            text = (text[:m.start()] + " " + text[m.end():]).strip()
            m = pattern.search(text)
    # surface / distance-band adjectives sit inside the noun phrase
    m = _SURFACE.search(text)
    if m:
        found.append(Predicate("surface_is", (m.group(1),)))
        text = (text[:m.start()] + " " + text[m.end():]).strip()
    m = _BAND.search(text)
    if m:
        found.append(Predicate("distance_band", (m.group(1),)))
        text = (text[:m.start()] + " " + text[m.end():]).strip()
    return _SPACE.sub(" ", text).strip(" ,"), found


# ------------------------------------------------------------- bases -------

_BASES: list[tuple[re.Pattern, str]] = [
    # totals
    (re.compile(r"(?:having )?obtain(?:ed)? at least ([\d,]+) fans", re.I), "fans_at_least"),
    (re.compile(r"attain at least ([\d,]+) (%s)" % _STATS, re.I), "stat_at_least_rev"),
    (re.compile(r"attain (%s) of ([\d,]+) or higher" % _STATS, re.I), "stat_at_least"),
    (re.compile(r"attain (%s) and (%s) of ([\d,]+) or higher" % (_STATS, _STATS), re.I),
     "two_stats_at_least"),
    (re.compile(r"attain at least ([\d,]+) (%s) and (%s)" % (_STATS, _STATS), re.I),
     "two_stats_at_least_rev"),
    (re.compile(r"acquire (?:at least )?([\d,]+)(?: or more)? skills", re.I), "skills_at_least"),
    (re.compile(r"use at least ([\d,]+) skills in a single race", re.I), "skills_in_race"),
    # named races
    (re.compile(r"win the (.+?) (\d+) times?", re.I), "win_race_times"),
    (re.compile(r"win the (.+?) twice", re.I), "win_race_twice"),
    (re.compile(r"win (\d+) of the (.+)", re.I), "win_n_of_races"),
    (re.compile(r"(?:including )?(?:a )?wins? in the (.+)", re.I), "win_races"),
    (re.compile(r"including the (.+)", re.I), "win_races"),
    (re.compile(r"win (?:the )?(.+?) or (.+)", re.I), "win_either_race"),
    (re.compile(r"run in a (Make Debut) race", re.I), "run_race_named"),
    # counted wins / runs
    (re.compile(r"win (?:at least )?(\d+) (%s)(?: races?)?" % _GRADE, re.I), "win_grade_count"),
    (re.compile(r"win (?:at least )?(\d+) graded races?", re.I), "win_graded_count"),
    (re.compile(r"win (?:at least )?(\d+) races?", re.I), "win_count"),
    (re.compile(r"win (?:at least )?(\d+) times?", re.I), "win_count"),
    # bare count, e.g. "Run at least 4 nighttime races and win 3"
    (re.compile(r"win (?:at least )?(\d+)", re.I), "win_count"),
    (re.compile(r"win a (%s)(?: race)?" % _GRADE, re.I), "win_grade_once"),
    (re.compile(r"win a graded race", re.I), "win_graded_once"),
    (re.compile(r"win a race", re.I), "win_any_once"),
    # racecourse / region
    (re.compile(r"win (\d+) graded races held at (.+)", re.I), "win_graded_at_courses"),
    (re.compile(r"win a graded race at each of the (\d+) central racecourses", re.I),
     "win_all_central_courses"),
    (re.compile(r"win a graded race at (\d+) or more different regional racecourses", re.I),
     "win_regional_courses"),
    (re.compile(r"win (\d+) regional races", re.I), "win_regional_count"),
    (re.compile(r"win (\d+) races that have (.+) in the name", re.I), "win_races_named_like"),
    # time of day
    (re.compile(r"run at least (\d+) (evening|nighttime|daytime|morning) races", re.I),
     "run_timeofday_count"),
    (re.compile(r"win (\d+) (evening|nighttime|daytime|morning) races", re.I),
     "win_timeofday_count"),
    # prerequisites on other epithets
    (re.compile(r"obtain the (.+?) epithet", re.I), "has_epithet"),
    (re.compile(r"obtain the team title \"(.+?)\"", re.I), "has_team_title"),
    # sequencing
    (re.compile(r"win a race after losing (\d+) or more times in a row", re.I),
     "win_after_losing_streak"),
    (re.compile(r"win a race after becoming Rushed (\d+) times", re.I), "win_after_rushed"),
    (re.compile(r"win a (%s) without having won a graded race before" % _GRADE, re.I),
     "first_graded_win_is"),
    (re.compile(r"win your first (%s) in (junior|classic|senior) year" % _GRADE, re.I),
     "first_grade_win_year"),
    # scenario-specific counters
    (re.compile(r"activate Unity Training at least (\d+) times", re.I), "unity_training_count"),
    (re.compile(r"activate Unity Training with at least (\d+) characters simultaneously", re.I),
     "unity_training_chars"),
    (re.compile(r"trigger at least (\d+) Spirit Bursts", re.I), "spirit_bursts"),
    (re.compile(r"(?:complete a Career playthrough )?with a total score of at least ([\d,]+) "
                r"in (\w+)", re.I), "concert_score"),
    (re.compile(r"(?:having )?obtained at least (\d+) songs", re.I), "songs_obtained"),
    (re.compile(r"(?:having )?scored a total of at least ([\d,]+) performance points", re.I),
     "performance_points"),
    (re.compile(r"end the Grand Concert with a Great Success rating", re.I),
     "grand_concert_great_success"),
    (re.compile(r"put on a special Grand Concert", re.I), "special_grand_concert"),
    (re.compile(r"put on a Grand Concert", re.I), "grand_concert"),
    (re.compile(r"(?:having )?earned at least ([\d,]+) Pro Shop coins", re.I), "pro_shop_coins"),
    (re.compile(r"(?:having )?purchased at least (\d+) items from the Pro Shop", re.I),
     "pro_shop_items"),
    (re.compile(r"(?:complete a Career playthrough )?with at least ([\d,]+) Result Pts", re.I),
     "result_points"),
    (re.compile(r"(?:having )?won all 3 races of the TS Climax", re.I), "ts_climax_all"),
    (re.compile(r"(?:complete a Career )?with Independent Training", re.I),
     "independent_training"),
    # career-completion variants that name their own totals
    (re.compile(r"(?:complete a Career playthrough )?with (?:at least )?(\d+) (%s) wins" % _GRADE,
                re.I), "win_grade_count_rev"),
    (re.compile(r"(?:complete a Career playthrough )?with all Career goals completed", re.I),
     "all_career_goals"),
    (re.compile(r"(?:having )?won at least (\d+) (%s) races" % _GRADE, re.I),
     "win_grade_count_rev"),
    (re.compile(r"(?:having )?run in (?:at least )?(\d+) graded races", re.I),
     "run_graded_count"),
    (re.compile(r"run in (?:at least )?(\d+) graded races", re.I), "run_graded_count"),
    (re.compile(r"have a win rate of (\d+)% or higher", re.I), "win_rate_at_least"),
    (re.compile(r"placing top (\d+) in all of them", re.I), "place_top_all"),
    (re.compile(r"obtain at least (\d+) skills that activate only in (turf|dirt) races", re.I),
     "surface_only_skills"),
    # beating specific rivals
    (re.compile(r"win against (.+?) at least (\d+) times(?: each)?", re.I), "beat_rivals"),
    (re.compile(r"win against the duo in the URA Final", re.I), "beat_ura_duo"),
    (re.compile(r"(?:having )?won at least (\d+) times each against (.+)", re.I),
     "beat_rivals_rev"),
    # multi-grade ("G2 or above", "G1 or G2", "OP level or higher")
    (re.compile(r"win (?:at least )?(\d+) (%s) or above races?" % _GRADE, re.I),
     "win_grade_or_above"),
    (re.compile(r"win (?:at least )?(\d+) (%s) level or higher races?" % _GRADE, re.I),
     "win_grade_or_above"),
    (re.compile(r"win (?:at least )?(\d+) (%s) or (%s) races?" % (_GRADE, _GRADE), re.I),
     "win_either_grade"),
    (re.compile(r"run in (%s) or (%s) races at least (\d+) times" % (_GRADE, _GRADE), re.I),
     "run_either_grade"),
    # surface / band combinations
    (re.compile(r"win (?:at least )?(\d+) (%s) races? on turf and dirt each" % _GRADE, re.I),
     "win_grade_each_surface"),
    (re.compile(r"win (?:at least )?(\d+) turf and (\d+) dirt (%s) races?" % _GRADE, re.I),
     "win_grade_turf_and_dirt"),
    (re.compile(r"win (?:at least )?(\d+) (%s) on both turf and dirt" % _GRADE, re.I),
     "win_grade_each_surface"),
    # "Win a sprint, mile, medium, long, and dirt graded race" -- the list mixes
    # distance bands with a surface, so capture it whole and classify per token
    # in the evaluator rather than trying to split the two in the pattern.
    (re.compile(r"win a (?:(turf|dirt) )?([\w, ]+?) (?:graded )?races?", re.I),
     "win_band_set"),
    (re.compile(r"win both a (\w+) and (\w+) (%s) race" % _GRADE, re.I), "win_two_bands"),
    (re.compile(r"win (?:at least )?(\d+) (standard|non-standard) distance races?", re.I),
     "win_distance_kind"),
    # totals with a qualifier
    (re.compile(r"obtain at least ([\d,]+) fans in (junior|classic|senior) year", re.I),
     "fans_at_least_year"),
    (re.compile(r"attain at least (\w+) in (.+?) aptitude", re.I), "aptitude_at_least"),
    (re.compile(r"attain below ([\d,]+) (%s)" % _STATS, re.I), "stat_below"),
    # career-completion continuations
    (re.compile(r"(?:having )?placed top (\d+) in all races", re.I), "place_top_all"),
    (re.compile(r"(?:having )?accomplished all goals", re.I), "all_career_goals"),
    (re.compile(r"complete the (?:Career )?playthrough", re.I), "career_complete"),
    (re.compile(r"including (?:at least )?(\d+) (%s) races?" % _GRADE, re.I),
     "win_grade_count_incl"),
    (re.compile(r"including (?:at least )?(\d+) wins", re.I), "win_count"),
    (re.compile(r"never winning any", re.I), "never_won_grade"),
    (re.compile(r"achieve Career rank (\w+) or higher", re.I), "career_rank_at_least"),
    # legacy compounds
    (re.compile(r"receive Inspiration from (\d+) Legacy Umamusume who were undefeated "
                r"in their careers", re.I), "legacy_n_undefeated"),
    (re.compile(r"receive Inspiration from (\d+) Legacy Umamusume with Career rank (\w+) "
                r"or lower", re.I), "legacy_n_rank_at_most"),
    (re.compile(r"receive Inspiration from (\d+) Legacy Umamusume with at least ([\d,]+) fans",
                re.I), "legacy_n_fans"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with no Career G1 wins", re.I),
     "legacy_no_g1"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with the (\w+) name", re.I),
     "legacy_family_name"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with the (.+?) and a Legacy "
                r"Umamusume with the (.+)", re.I), "legacy_two_titles"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with at least ([\d,]+) (%s) "
                r"and a Legacy Umamusume with under ([\d,]+) (%s)" % (_STATS, _STATS), re.I),
     "legacy_two_stat_bounds"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with a career win in the (.+?) "
                r"and at least ([\d,]+) (%s)" % _STATS, re.I), "legacy_win_and_stat"),
    (re.compile(r"(?:after )?receiving Inspiration from two Legacies with no career graded "
                r"race wins", re.I), "legacy_two_ungraded"),
    # misc one-offs
    (re.compile(r"experience a moment where you feel an irreplaceable bond", re.I),
     "irreplaceable_bond"),
    (re.compile(r"win the URA Finale finals", re.I), "won_ura_finals"),
    (re.compile(r"win (?:at least )?(\d+) graded races held at (.+)", re.I),
     "win_graded_at_courses"),
    # bare continuations left by clause splitting
    (re.compile(r"(?:with )?(?:at least )?([\d,]+) fans", re.I), "fans_at_least"),
    (re.compile(r"as a (%s) and the number one favou?rite" % _STYLE, re.I),
     "style_and_top_favorite"),
    (re.compile(r"(\d+) (%s) wins" % _GRADE, re.I), "win_grade_count_rev"),
    (re.compile(r"as (?:the )?number one favou?rite", re.I), "as_favorite"),
    (re.compile(r"as a (%s)" % _STYLE, re.I), "won_with_style"),
    (re.compile(r"have a win rate of (\d+)% or higher", re.I), "win_rate_at_least"),
    # the URA Finale is a scenario final, not a row in the race table
    (re.compile(r"win the URA Finale(?: finals)?", re.I), "won_ura_finals"),
    (re.compile(r"place (\d+)(?:st|nd|rd|th) in a (%s)(?: race)? (\d+) times" % _GRADE, re.I),
     "place_grade_count"),
    (re.compile(r"place top (\d+) in all races", re.I), "place_top_all"),
    (re.compile(r"run in (?:at least )?(\d+) races?", re.I), "run_count"),
    (re.compile(r"run in (?:at least )?(\d+) (%s)(?: races?)?" % _GRADE, re.I), "run_grade_count"),
    (re.compile(r"run in (?:at least )?(\d+) times?", re.I), "run_count"),
    (re.compile(r"run in (Rainy|Cloudy|Snowy|Sunny) weather at least (\d+) times", re.I),
     "run_weather_count"),
    (re.compile(r"run in all races", re.I), "ran_all_races"),
    # training
    (re.compile(r"reach (%s) Training Level (\d+)" % _STATS, re.I), "training_level"),
    (re.compile(r"perform (%s) Training (\d+) times" % _STATS, re.I), "training_count"),
    (re.compile(r"(?:complete (?:a|the) Career playthrough )?with all Trainings? levels? "
                r"(?:at )?(\d+) or higher", re.I), "all_training_level"),
    (re.compile(r"never failing a Training", re.I), "never_failed_training"),
    (re.compile(r"without training a single time", re.I), "never_trained"),
    # career completion
    (re.compile(r"complete (?:a|the) (?:Career )?playthrough undefeated with at least (\d+) wins",
                re.I), "career_undefeated_wins"),
    (re.compile(r"complete (?:a|the) (?:Career )?playthrough with a win rate of (\d+)% or higher",
                re.I), "win_rate_at_least"),
    (re.compile(r"complete (?:a|the) (?:Career )?playthrough", re.I), "career_complete"),
    (re.compile(r"accomplish all Career goals", re.I), "all_career_goals"),
    (re.compile(r"achieve Career rank (\w+) or higher", re.I), "career_rank_at_least"),
    (re.compile(r"undefeated", re.I), "undefeated"),
    (re.compile(r"having run in (?:at least )?(\d+) races?", re.I), "run_count"),
    (re.compile(r"having run in (?:at least )?(\d+) graded races?", re.I), "run_graded_count"),
    (re.compile(r"after running in (?:at least )?(\d+) races?", re.I), "run_count"),
    (re.compile(r"all on (turf|dirt)", re.I), "all_races_surface"),
    # legacy / inheritance
    (re.compile(r"receive Inspiration from (\d+) Legacy Umamusume with at least ([\d,]+) "
                r"combined G1 wins", re.I), "legacy_combined_g1"),
    (re.compile(r"receive Inspiration from (\d+) Legacy Umamusume with at least ([\d,]+) (%s)"
                % _STATS, re.I), "legacy_n_stat"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with (?:a )?career wins? in the "
                r"(.+)", re.I), "legacy_career_win"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with Career rank (\w+) or higher",
                re.I), "legacy_rank_at_least"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with at least (\d+) career G1 wins",
                re.I), "legacy_g1_wins"),
    (re.compile(r"receive Inspiration from two Legacies with no career graded race wins", re.I),
     "legacy_two_ungraded"),
    (re.compile(r"receive Inspiration from a Legacy Umamusume with (.+?)-Distance Aptitude "
                r"(\w+) or higher", re.I), "legacy_aptitude"),
    # "..., in particular winning the Shuka Sho by at least 3 1/2 lengths" --
    # a second, narrower constraint on ONE race already named in the list.
    (re.compile(r"in particular winning the (.+)", re.I), "win_races"),
    # A continuation group left by the split above -- "the Takarazuka Kinen,
    # Queen Elizabeth II Cup, and Arima Kinen" with its own modifiers, where
    # the governing verb stayed with the first group. Race-name validation is
    # what keeps this from swallowing arbitrary text.
    (re.compile(r"the (.+)", re.I), "win_races"),
    # LAST RESORT: the broadest named-race form. Every more specific "win the
    # ..." shape above must be tried first or this swallows them.
    (re.compile(r"(?:and |then )?win (?:the )?(.+)", re.I), "win_races"),
]


# ------------------------------------------------------ race validation ----
# The broad "win the ..." base would otherwise swallow anything: "Win a race
# after becoming Rushed 3 times" parsed as a win against a race literally named
# "a race after becoming Rushed 3 times". That never evaluates true, so it can
# not wrongly award -- but it silently makes the epithet UNEARNABLE while
# reporting itself as understood, which is exactly the failure this module's
# conservative design exists to prevent. So a race-list parse is only accepted
# when every name in it is a real race.
# 29 is the short/abbreviated name ("Hanshin J.F.", "Mile Ch."), 28 the full
# one ("Hopeful Stakes", "Tokyo Yushun (Japanese Derby)"). Epithet prose draws
# on both, so both are accepted.
_RACE_NAME_CATEGORIES = (28, 29)
_race_names: frozenset | None = None

# Era/scenario qualifiers select a specific running of a race and are not part
# of the name as master.mdb stores it.
_QUALIFIER = re.compile(r"\s*\((?:Junior|Classic|Senior) Year\)\s*$", re.I)

# kind -> which capture group(s) hold the race list. Not always the last one:
# win_race_times is (name, count), so validating groups[-1] checked the COUNT
# against the race table and rejected every otherwise-good parse.
_RACE_PREDICATES = {
    "win_races": (0,),
    "win_n_of_races": (1,),
    "win_either_race": (0, 1),
    "win_race_times": (0,),
    "win_race_twice": (0,),
    "legacy_career_win": (0,),
    # run_race_named is deliberately NOT validated: its argument is a race
    # CLASS ("Make Debut"), not a name in the race table.
}


def race_names() -> frozenset:
    """Every real race short-name, from master.mdb text_data category 29.
    Empty (so validation is skipped) if master.mdb is unavailable -- this
    module must stay importable and testable without the game installed."""
    global _race_names
    if _race_names is None:
        try:
            from . import master_data
            placeholders = ",".join("?" * len(_RACE_NAME_CATEGORIES))
            _race_names = frozenset(
                _fold(r["text"]) for r in
                master_data.query(
                    "SELECT DISTINCT text FROM text_data WHERE category IN (%s)" % placeholders,
                    _RACE_NAME_CATEGORIES)
                if r["text"] and r["text"].strip())
        except Exception:
            _race_names = frozenset()
    return _race_names


# master.mdb is inconsistent about punctuation between categories: the race
# table writes "JBC Ladies’ Classic" with a typographic apostrophe while the
# epithet prose writes "JBC Ladies' Classic" with an ASCII one -- and
# "Ladies' Prelude" uses ASCII in BOTH. Comparing raw text therefore rejects a
# real race for a reason that has nothing to do with the race. Fold the
# punctuation that varies before comparing.
_PUNCT_FOLD = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def _fold(name: str) -> str:
    return _SPACE.sub(" ", (name or "").translate(_PUNCT_FOLD)).strip()


def _is_real_race(name: str) -> bool:
    known = race_names()
    if not known:
        return True   # no master.mdb -- cannot validate, do not reject
    return _fold(_QUALIFIER.sub("", name)) in known


def _race_parse_ok(kind: str, groups: tuple) -> bool:
    slots = _RACE_PREDICATES.get(kind)
    if slots is None:
        return True
    names = []
    for i in slots:
        if i >= len(groups):
            return False
        names.extend(_names(groups[i]))
    return bool(names) and all(_is_real_race(n) for n in names)


def _match_base(text: str):
    for pattern, kind in _BASES:
        m = pattern.fullmatch(text)
        if m and _race_parse_ok(kind, m.groups()):
            return kind, m.groups()
    return None


# "Complete a Career playthrough having X" is a career-completion requirement
# AND an X requirement. Peeling the prefix lets every "having .../with .../
# after ..." base be written once instead of twice.
_CAREER_PREFIX = re.compile(
    r"^complete (?:a|the) (?:Career )?(?:playthrough|Career)\s+(?=having|with|after|undefeated)",
    re.I)


def parse_clause(clause: str):
    m = _CAREER_PREFIX.match(clause)
    if m:
        rest = clause[m.end():].strip()
        # Re-SPLIT the remainder, don't hand it to parse_clause whole: the tail
        # is frequently several clauses ("with a win rate of 85% or higher, 7 G1
        # wins, and at least 320,000 fans"), and treating it as one made the
        # entire epithet unparseable.
        inner = [parse_clause(c) for c in _CLAUSE_SPLIT.split(rest) if c.strip()]
        if inner and not any(isinstance(p, Unparsed) for p in inner):
            return All((Predicate("career_complete"),) + tuple(inner))

    # Try the WHOLE clause against the base table first. Modifier stripping is
    # a fallback, never the first move: several bases legitimately contain text
    # a modifier pattern would also match ("Run in Rainy weather at least 4
    # times" is a base, but " in Rainy weather" is also a modifier), and
    # stripping first mangles them into something no base recognises.
    hit = _match_base(clause)
    if hit is not None:
        kind, groups = hit
        return Predicate(kind, groups)

    remainder, mods = _strip_modifiers(clause)
    hit = _match_base(remainder) if remainder else None
    # A race list carrying modifiers scopes them to those races -- see
    # ScopedRaces. Only for the plain "win the <list>" form: a count-based base
    # ("win 3 of the ...") has no single race to attach a margin to.
    if hit is not None and mods and hit[0] == "win_races":
        races = tuple(_names(hit[1][0]))
        if races:
            return ScopedRaces(races, tuple(mods))
    if hit is None:
        # A clause that was ONLY modifiers is still meaningful ("while
        # undefeated" standing alone attaches to the previous clause's subject
        # in prose, and as a conjunct here it means the same thing).
        if mods and not remainder:
            return All(tuple(mods))
        return Unparsed(clause)
    kind, groups = hit
    # Keep None groups. Dropping them silently re-indexes every later argument
    # when a pattern has an OPTIONAL group, so an evaluator expecting
    # (surface, races) was called with (races,) alone. Evaluators guard for
    # None instead.
    base = Predicate(kind, groups)
    if mods:
        base, mods = _fuse_surface_count(base, mods)
        base, mods = _fuse_consecutive(base, mods)
    return All((base,) + tuple(mods)) if mods else base


# "dirt"/"turf" is peeled off the noun phrase as its own conjunct by
# _strip_modifiers, which is right for "win the Japan Dirt Derby" but WRONG the
# moment the clause counts. "Win 9 G1 dirt races" parsed as
# win_grade_count(9, G1) AND surface_is(dirt) -- i.e. "won 9 G1s of ANY surface,
# and won on dirt at least once". A career with 27 turf G1s and 4 dirt G1s
# satisfied it, and every dirt epithet in Trackblazer fired on turf runs (it
# awarded Dirt G1 Dominator, Powerhouse, Achiever, Eat My Dust, Playing Dirty
# and Dirty Work to a 4-dirt-win career, ~78 careers in the corpus each).
#
# The count and the surface have to meet. build_facts already publishes the
# joint tallies -- wins_grade_by_surface[(grade, surface)] and
# wins_surface_<s> -- so fuse the pair into one predicate that reads them.
#
# Distance bands are deliberately NOT fused: facts carry distance_bands_won as
# a set with no per-band counts, and the only band clauses that exist ("win a
# turf sprint, mile, medium, and long race") are once-each anyway.
# "in a row" is peeled off as a bare `consecutive` modifier, and that evaluator
# reads a GLOBAL fact -- "this career won two races back to back at some point"
# -- which is true of very nearly every career. Conjoined with a named-race
# count it therefore collapsed "win the Queen Elizabeth II Cup 2 times in a
# row" into "win it twice, ever": Goddess fired in 100 of 203 real careers
# against the real server's 16. Scoped to a race, "in a row" has to mean
# back-to-back YEARS of that race.
def _fuse_consecutive(base: "Predicate", mods: list):
    if base.kind != "win_race_times":
        return base, mods
    con = next((m for m in mods if getattr(m, "kind", None) == "consecutive"), None)
    if con is None:
        return base, mods
    return (Predicate("win_race_times_in_a_row", base.params),
            [m for m in mods if m is not con])


_SURFACE_FUSE = {"win_grade_count": "win_grade_surface_count",
                 "win_count": "win_surface_count"}


def _fuse_surface_count(base: "Predicate", mods: list):
    """Merge a `surface_is` modifier into a counted-win base predicate."""
    joint = _SURFACE_FUSE.get(base.kind)
    if joint is None:
        return base, mods
    surface = next((m for m in mods if getattr(m, "kind", None) == "surface_is"), None)
    if surface is None:
        return base, mods
    rest = [m for m in mods if m is not surface]
    return Predicate(joint, base.params + surface.params), rest


def parse(text: str) -> All:
    whole = normalize(text)
    return All(tuple(parse_clause(c) for c in _CLAUSE_SPLIT.split(whole) if c.strip()))


# -------------------------------------------------------- evaluation -------
# Every evaluator reads the flat `facts` mapping the career tracker produces.
# A missing fact is falsy, never an exception: a career run recorded before a
# given fact existed must not crash the award pass.

def _f(facts, key, default=0):
    return facts.get(key, default) or default


def _won_in_a_row(facts, name: str, times: int) -> bool:
    """True when `name` was won in `times` CONSECUTIVE career years."""
    years = (facts.get("race_win_years") or {}).get(name.strip())
    if not years or times <= 1:
        return bool(years) and (times <= 1 or len(years) >= times)
    run = 1
    for prev, cur in zip(years, years[1:]):
        run = run + 1 if cur == prev + 1 else 1
        if run >= times:
            return True
    return False


def _has_epithets(facts, phrase: str) -> bool:
    """True when the career already holds every epithet `phrase` names.

    An epithet condition may reference OTHER epithets, and the phrase is not
    always a single name -- "Obtain the Spring Champion and Fall Champion
    epithets, and either the Stunning or Lady epithet" is one capture. It reads
    as a conjunction of groups, each group a disjunction:

        (Spring Champion) AND (Fall Champion) AND (Stunning OR Lady)

    `epithets_owned` is filled in by epithets.build_facts, which resolves the
    reference-free epithets first and then feeds them back -- so this reads
    what the SAME career has earned, which is what "obtain" means here."""
    owned = facts.get("epithets_owned") or ()
    if not phrase:
        return False
    text = re.sub(r"\bepithets?\b", " ", phrase, flags=re.I)
    text = re.sub(r"\beither\b|\bthe\b", " ", text, flags=re.I)
    for group in re.split(r"\s*,?\s+and\s+|\s*,\s*", text):
        names = [n.strip() for n in re.split(r"\s+or\s+", group) if n.strip()]
        if not names:
            continue
        if not any(n in owned for n in names):
            return False
    return True


def _won(facts):
    return facts.get("races_won_names") or ()


_BANDS = ("sprint", "mile", "medium", "long")
_SURFACE_WORDS = ("turf", "dirt")


# The epithet prose and the condition table disagree on one name: the epithet
# says "Skin Breakout", master.mdb text_data 142 says "Skin Outbreak". Same
# condition (user-confirmed).
_CONDITION_ALIASES = {"skin breakout": "skin outbreak"}


def _has_condition(facts, name: str) -> bool:
    key = _fold(name or "").lower().strip()
    key = _CONDITION_ALIASES.get(key, key)
    held = {_CONDITION_ALIASES.get(_fold(c).lower().strip(), _fold(c).lower().strip())
            for c in (facts.get("conditions") or ())}
    return key in held


def _scenario_ok(facts, label) -> bool:
    """"(Trackblazer only)" and friends. The run must BE that scenario, and the
    server must actually implement it -- an epithet for a scenario this server
    cannot play must never be awarded. See app/epithets.py for why comparing
    the label to the full title directly does not work."""
    try:
        from . import epithets
        want = epithets.scenario_id_for_label(label)
    except Exception:
        return False
    if want is None:
        return False
    if want not in (facts.get("implemented_scenarios") or ()):
        return False
    return facts.get("scenario_id") == want


def _band_set_ok(facts, surface, blob) -> bool:
    """"a sprint, mile, medium, long, and dirt graded race" -- every listed
    distance band must have been won, and every listed surface too. The list
    mixes both, so classify token by token; an unrecognised token means the
    clause is asking for something this cannot check, so refuse."""
    bands_won = facts.get("distance_bands_won") or ()
    surfaces_won = facts.get("surfaces_won") or ()
    if surface and surface.lower() not in surfaces_won:
        return False
    for token in _names(blob or ""):
        t = token.lower()
        if t in _BANDS:
            if t not in bands_won:
                return False
        elif t in _SURFACE_WORDS:
            if t not in surfaces_won:
                return False
        else:
            return False
    return True


_EVALUATORS = {
    # totals
    "fans_at_least": lambda f, n: _f(f, "fans") >= _num(n),
    "stat_at_least": lambda f, stat, n: _f(f, "stat_" + stat.lower()) >= _num(n),
    "stat_at_least_rev": lambda f, n, stat: _f(f, "stat_" + stat.lower()) >= _num(n),
    "two_stats_at_least": lambda f, a, b, n: (_f(f, "stat_" + a.lower()) >= _num(n)
                                              and _f(f, "stat_" + b.lower()) >= _num(n)),
    "two_stats_at_least_rev": lambda f, n, a, b: (_f(f, "stat_" + a.lower()) >= _num(n)
                                                  and _f(f, "stat_" + b.lower()) >= _num(n)),
    "skills_at_least": lambda f, n: _f(f, "skill_count") >= _num(n),
    "skills_in_race": lambda f, n: _f(f, "max_skills_used_in_race") >= _num(n),
    # named races
    "win_races": lambda f, blob: all(n in _won(f) for n in _names(blob)),
    "win_either_race": lambda f, a, b: (a.strip() in _won(f) or b.strip() in _won(f)),
    "win_n_of_races": lambda f, n, blob: (
        sum(1 for x in _names(blob) if x in _won(f)) >= _num(n)),
    "win_race_times": lambda f, name, n: (
        (f.get("race_win_counts") or {}).get(name.strip(), 0) >= _num(n)),
    "win_race_twice": lambda f, name: (
        (f.get("race_win_counts") or {}).get(name.strip(), 0) >= 2),
    "run_race_named": lambda f, name: name in (f.get("races_run_names") or ()),
    # counted
    "win_grade_count": lambda f, n, g: (
        _f(f, "wins_grade_" + g.lower().replace(" ", "_").replace("-", "_")) >= _num(n)),
    "win_grade_once": lambda f, g: (
        _f(f, "wins_grade_" + g.lower().replace(" ", "_").replace("-", "_")) >= 1),
    "win_graded_count": lambda f, n: _f(f, "wins_graded") >= _num(n),
    "win_graded_once": lambda f: _f(f, "wins_graded") >= 1,
    "win_count": lambda f, n: _f(f, "wins_total") >= _num(n),
    # Counted wins scoped to a surface -- see _fuse_surface_count. Grades are
    # lowercased to match build_facts' wins_grade_by_surface keys.
    "win_grade_surface_count": lambda f, n, g, s: (
        (f.get("wins_grade_by_surface") or {}).get(
            (g.lower().replace(" ", "_").replace("-", "_"), s.lower()), 0) >= _num(n)),
    "win_surface_count": lambda f, n, s: _f(f, "wins_surface_" + s.lower()) >= _num(n),
    "win_any_once": lambda f: _f(f, "wins_total") >= 1,
    "place_grade_count": lambda f, place, g, n: (
        _f(f, "place_%s_grade_%s" % (place, g.lower())) >= _num(n)),
    "place_top_all": lambda f, n: _f(f, "worst_finish", 99) <= _num(n),
    "run_count": lambda f, n: _f(f, "races_run") >= _num(n),
    "run_graded_count": lambda f, n: _f(f, "graded_races_run") >= _num(n),
    "run_grade_count": lambda f, n, g: (
        _f(f, "runs_grade_" + g.lower().replace(" ", "_")) >= _num(n)),
    "run_weather_count": lambda f, w, n: _f(f, "runs_weather_" + w.lower()) >= _num(n),
    "ran_all_races": lambda f: bool(f.get("ran_all_races")),
    # training
    "training_level": lambda f, stat, lv: _f(f, "training_level_" + stat.lower()) >= _num(lv),
    "training_count": lambda f, stat, n: _f(f, "training_count_" + stat.lower()) >= _num(n),
    "all_training_level": lambda f, lv: _f(f, "min_training_level") >= _num(lv),
    "never_failed_training": lambda f: not f.get("training_failures"),
    "never_trained": lambda f: not f.get("training_count_total"),
    # career
    "career_complete": lambda f: bool(f.get("career_complete")),
    "career_undefeated_wins": lambda f, n: (bool(f.get("undefeated"))
                                            and _f(f, "wins_total") >= _num(n)),
    "win_rate_at_least": lambda f, pct: _f(f, "win_rate_pct") >= _num(pct),
    "all_career_goals": lambda f: bool(f.get("all_goals_cleared")),
    "career_rank_at_least": lambda f, rank: (
        _RANK_ORDER.get(str(f.get("career_rank", "")).upper(), -1)
        >= _RANK_ORDER.get(rank.upper(), 99)),
    "undefeated": lambda f: bool(f.get("undefeated")),
    "all_races_surface": lambda f, s: (f.get("only_surface") == s.lower()),
    # legacy
    "legacy_career_win": lambda f, blob: all(
        n in (f.get("legacy_race_wins") or ()) for n in _names(blob)),
    "legacy_rank_at_least": lambda f, rank: (
        _RANK_ORDER.get(str(f.get("legacy_best_rank", "")).upper(), -1)
        >= _RANK_ORDER.get(rank.upper(), 99)),
    "legacy_g1_wins": lambda f, n: _f(f, "legacy_g1_wins") >= _num(n),
    "legacy_combined_g1": lambda f, count, n: (_f(f, "legacy_count") >= _num(count)
                                               and _f(f, "legacy_g1_wins") >= _num(n)),
    # "Receive Inspiration from 2 Legacy Umamusume with at least 1,200 Speed" --
    # a COUNT of legacies each clearing the bar, not a total and not one of them.
    # legacy_stat_* is a per-legacy LIST (epithets.py builds it that way, and
    # legacy_two_stat_bounds below already read it as one); this used to compare
    # the list itself against an int, which raised TypeError the moment a run
    # actually had legacies.
    "legacy_n_stat": lambda f, count, n, stat: (
        sum(1 for v in (f.get("legacy_stat_" + stat.lower()) or ()) if v >= _num(n))
        >= _num(count)),
    "legacy_two_ungraded": lambda f: bool(f.get("legacy_two_ungraded")),
    "legacy_aptitude": lambda f, band, grade: (
        (f.get("legacy_aptitudes") or {}).get(band.lower(), "") >= grade.upper()),
    # modifiers
    "margin_at_least": lambda f, n: _f(f, "best_win_margin") >= _num(n),
    "avg_margin_at_least": lambda f, n: _f(f, "avg_win_margin") >= _num(n),
    "as_favorite": lambda f: bool(f.get("won_as_favorite")),
    "favorite_at_least": lambda f, n: _f(f, "best_favorite_position", 99) <= _num(n),
    "favorite_between": lambda f, lo, hi: (
        _num(lo) <= _f(f, "best_favorite_position", 99) <= _num(hi)),
    "running_style": lambda f, *styles: any(
        s and s.lower().replace(" ", "_") in (f.get("styles_won_with") or ()) for s in styles),
    "winning_streak": lambda f: bool(f.get("winning_streak")),
    "consecutive": lambda f: bool(f.get("consecutive")),
    "win_race_times_in_a_row": lambda f, name, n: _won_in_a_row(f, name, _num(n)),
    "mood_at_most": lambda f, mood: (
        _MOOD_ORDER.get(str(f.get("race_mood", "")).title(), 99)
        <= _MOOD_ORDER.get(mood.title(), -1)),
    "mood_exactly": lambda f, mood: str(f.get("race_mood", "")).title() == mood.title(),
    "distance_at_most": lambda f, n: _f(f, "min_race_distance", 0) <= _num(n),
    "distance_at_least": lambda f, n: _f(f, "max_race_distance") >= _num(n),
    "distance_band": lambda f, band: band.lower() in (f.get("distance_bands_won") or ()),
    "surface_is": lambda f, s: s.lower() in (f.get("surfaces_won") or ()),
    "weather_is": lambda f, w: _f(f, "runs_weather_" + w.lower()) >= 1,
    "at_racecourse": lambda f, name: name.strip() in (f.get("racecourses_won") or ()),
    "scenario_only": lambda f, name: _scenario_ok(f, name),
    # racecourse / region
    "win_graded_at_courses": lambda f, n, blob: sum(
        1 for c in (f.get("graded_wins_by_course") or {})
        if c in _names(blob)) and sum(
        (f.get("graded_wins_by_course") or {}).get(c, 0) for c in _names(blob)) >= _num(n),
    "win_all_central_courses": lambda f, n: (
        len(f.get("central_courses_won") or ()) >= _num(n)),
    "win_regional_courses": lambda f, n: (
        len(f.get("regional_courses_won") or ()) >= _num(n)),
    "win_regional_count": lambda f, n: _f(f, "wins_regional") >= _num(n),
    "win_races_named_like": lambda f, n, token: sum(
        1 for name in _won(f) if token.strip('"') in name) >= _num(n),
    # time of day
    "run_timeofday_count": lambda f, n, tod: _f(f, "runs_time_" + tod.lower()) >= _num(n),
    "win_timeofday_count": lambda f, n, tod: _f(f, "wins_time_" + tod.lower()) >= _num(n),
    # prerequisites
    "has_epithet": lambda f, name: _has_epithets(f, name),
    "has_team_title": lambda f, name: name in (f.get("team_titles") or ()),
    # sequencing
    "win_after_losing_streak": lambda f, n: _f(f, "longest_losing_streak_before_win") >= _num(n),
    "win_after_rushed": lambda f, n: _f(f, "wins_after_rushed_count") >= _num(n),
    "first_graded_win_is": lambda f, g: (
        str(f.get("first_graded_win_grade", "")).upper() == g.upper()),
    "first_grade_win_year": lambda f, g, year: (
        str(f.get("first_%s_win_year" % g.lower(), "")).lower() == year.lower()),
    # scenario counters
    "unity_training_count": lambda f, n: _f(f, "unity_training_count") >= _num(n),
    "unity_training_chars": lambda f, n: _f(f, "unity_training_max_chars") >= _num(n),
    "spirit_bursts": lambda f, n: _f(f, "spirit_bursts") >= _num(n),
    "concert_score": lambda f, n, kind: _f(f, "concert_score_" + kind.lower()) >= _num(n),
    "songs_obtained": lambda f, n: _f(f, "songs_obtained") >= _num(n),
    "performance_points": lambda f, n: _f(f, "performance_points") >= _num(n),
    "grand_concert_great_success": lambda f: bool(f.get("grand_concert_great_success")),
    "special_grand_concert": lambda f: bool(f.get("special_grand_concert")),
    "grand_concert": lambda f: bool(f.get("grand_concert")),
    "pro_shop_coins": lambda f, n: _f(f, "pro_shop_coins") >= _num(n),
    "pro_shop_items": lambda f, n: _f(f, "pro_shop_items") >= _num(n),
    "result_points": lambda f, n: _f(f, "result_points") >= _num(n),
    "ts_climax_all": lambda f: bool(f.get("ts_climax_all_won")),
    "independent_training": lambda f: bool(f.get("independent_training")),
    "win_grade_count_rev": lambda f, n, g: (
        _f(f, "wins_grade_" + g.lower().replace(" ", "_").replace("-", "_")) >= _num(n)),
    "surface_only_skills": lambda f, n, s: (
        _f(f, "skills_%s_only" % s.lower()) >= _num(n)),
    # rivals
    "beat_rivals": lambda f, blob, n: all(
        (f.get("wins_against") or {}).get(r, 0) >= _num(n) for r in _names(blob)),
    "beat_rivals_rev": lambda f, n, blob: all(
        (f.get("wins_against") or {}).get(r, 0) >= _num(n) for r in _names(blob)),
    "beat_ura_duo": lambda f: bool(f.get("beat_ura_duo")),
    "beat_family_name": lambda f, name: name in (f.get("beaten_family_names") or ()),
    # multi-grade
    "win_grade_or_above": lambda f, n, g: _f(f, "wins_at_or_above_" + g.lower()) >= _num(n),
    "win_either_grade": lambda f, n, a, b: (
        _f(f, "wins_grade_" + a.lower()) + _f(f, "wins_grade_" + b.lower())) >= _num(n),
    "run_either_grade": lambda f, a, b, n: (
        _f(f, "runs_grade_" + a.lower()) + _f(f, "runs_grade_" + b.lower())) >= _num(n),
    # surface / band combinations
    "win_grade_each_surface": lambda f, n, g: all(
        (f.get("wins_grade_by_surface") or {}).get((g.lower(), s), 0) >= _num(n)
        for s in ("turf", "dirt")),
    "win_grade_turf_and_dirt": lambda f, t, d, g: (
        (f.get("wins_grade_by_surface") or {}).get((g.lower(), "turf"), 0) >= _num(t)
        and (f.get("wins_grade_by_surface") or {}).get((g.lower(), "dirt"), 0) >= _num(d)),
    "win_band_set": lambda f, surface, blob: _band_set_ok(f, surface, blob),
    "win_two_bands": lambda f, a, b, g: (
        a.lower() in (f.get("distance_bands_won") or ())
        and b.lower() in (f.get("distance_bands_won") or ())
        and _f(f, "wins_grade_" + g.lower()) >= 1),
    "win_distance_kind": lambda f, n, kind: (
        _f(f, "wins_distance_" + kind.lower().replace("-", "_")) >= _num(n)),
    # qualified totals
    "fans_at_least_year": lambda f, n, year: (
        _f(f, "fans_by_year_" + year.lower()) >= _num(n)),
    "aptitude_at_least": lambda f, grade, what: (
        (f.get("aptitudes") or {}).get(what.lower().strip(), "G") >= grade.upper()),
    "stat_below": lambda f, n, stat: _f(f, "stat_" + stat.lower(), 10 ** 9) < _num(n),
    "win_grade_count_incl": lambda f, n, g: (
        _f(f, "wins_grade_" + g.lower().replace(" ", "_")) >= _num(n)),
    "never_won_grade": lambda f: not f.get("wins_graded"),
    # legacy compounds
    "legacy_n_undefeated": lambda f, n: _f(f, "legacy_undefeated_count") >= _num(n),
    "legacy_n_rank_at_most": lambda f, n, rank: (
        sum(1 for r in (f.get("legacy_ranks") or ())
            if _RANK_ORDER.get(str(r).upper(), 99) <= _RANK_ORDER.get(rank.upper(), -1))
        >= _num(n)),
    "legacy_n_fans": lambda f, n, fans: (
        sum(1 for v in (f.get("legacy_fans") or ()) if v >= _num(fans)) >= _num(n)),
    "legacy_no_g1": lambda f: _f(f, "legacy_g1_wins") == 0,
    "legacy_family_name": lambda f, name: name in (f.get("legacy_family_names") or ()),
    "legacy_two_titles": lambda f, a, b: (
        a.strip() in (f.get("legacy_titles") or ())
        and b.strip() in (f.get("legacy_titles") or ())),
    "legacy_two_stat_bounds": lambda f, hi, sa, lo, sb: (
        any(v >= _num(hi) for v in (f.get("legacy_stat_" + sa.lower()) or ()))
        and any(v < _num(lo) for v in (f.get("legacy_stat_" + sb.lower()) or ()))),
    "legacy_win_and_stat": lambda f, blob, n, stat: (
        all(x in (f.get("legacy_race_wins") or ()) for x in _names(blob))
        and _f(f, "legacy_best_" + stat.lower()) >= _num(n)),
    # modifiers added alongside these bases
    "in_year": lambda f, year: (str(f.get("race_year", "")).lower() == year.lower()),
    "mood_all_races": lambda f, mood: (
        str(f.get("min_race_mood", "")).title() == mood.title()),
    "favorite_at_worst": lambda f, n: _f(f, "worst_favorite_position") >= _num(n),
    "overtake_at_least": lambda f, n: _f(f, "max_overtakes_final_stretch") >= _num(n),
    "led_from_start": lambda f, m: _f(f, "led_from_meters") >= _num(m),
    "margin_distance": lambda f: bool(f.get("won_by_distance")),
    "margin_at_least_half": lambda f, n: _f(f, "best_win_margin") >= _num(n) + 0.5,
    "no_loss_at_or_below": lambda f, level: not (f.get("losses_at_or_below") or {}).get(
        level.lower()),
    "never_failed_training_mod": lambda f: not f.get("training_failures"),
    "fast_learner": lambda f: _has_condition(f, "Fast Learner"),
    "has_condition": lambda f, name: _has_condition(f, name),
    "without_conditions": lambda f, blob: not any(
        _has_condition(f, n) for n in _names(blob)),
    # misc
    "irreplaceable_bond": lambda f: bool(f.get("irreplaceable_bond")),
    "won_ura_finals": lambda f: bool(f.get("won_ura_finals")),
    "running_style_each": lambda f, *styles: all(
        s and s.lower().replace(" ", "_") in (f.get("styles_won_with") or ())
        for s in styles),
    "won_with_style": lambda f, style: (
        style.lower().replace(" ", "_") in (f.get("styles_won_with") or ())),
    "no_prior_surface_grade_win": lambda f, s, g: bool(
        (f.get("first_%s_%s_win_was_first" % (s.lower(), g.lower()))) or
        not (f.get("wins_grade_by_surface") or {}).get((g.lower(), s.lower()))),
    "noop_dangling_and": lambda f: True,
    "style_and_top_favorite": lambda f, style: (
        style.lower().replace(" ", "_") in (f.get("styles_won_with") or ())
        and bool(f.get("won_as_favorite"))),
    "never_trained_mod": lambda f: not f.get("training_count_total"),
    "wins_in_weather": lambda f, n, w: _f(f, "wins_weather_" + w.lower()) >= _num(n),
}

_RANK_ORDER = {"G": 0, "F": 1, "E": 2, "D": 3, "C": 4, "B": 5, "A": 6, "S": 7,
               "SS": 8, "UG": 9, "UF": 10, "UE": 11}
_MOOD_ORDER = {"Awful": 0, "Bad": 1, "Normal": 2, "Good": 3, "Great": 4}


# ---------------------------------------------------------- coverage -------

def coverage(entries) -> dict:
    """{total, fully_parsed, partial, unparsed_clauses} over an iterable of
    {'id', 'condition'} rows -- how much of the corpus this parser actually
    understands. Any clause listed in unparsed_clauses is one no epithet can
    currently be awarded on."""
    total = full = 0
    unparsed: dict[str, int] = {}

    def bad_parts(node):
        if isinstance(node, Unparsed):
            return [node]
        if isinstance(node, (All, Any_)):
            out = []
            for p in node.parts:
                out.extend(bad_parts(p))
            return out
        return []

    for e in entries:
        cond = e.get("condition")
        if not cond:
            continue
        total += 1
        bad = bad_parts(parse(cond))
        if not bad:
            full += 1
        for p in bad:
            unparsed[p.text] = unparsed.get(p.text, 0) + 1
    return {"total": total, "fully_parsed": full, "partial": total - full,
            "unparsed_clauses": unparsed}
