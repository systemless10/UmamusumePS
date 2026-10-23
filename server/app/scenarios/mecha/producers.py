
from __future__ import annotations

import logging

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from . import formula, impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = ((209027, 400009000, 0), (209026, 400009001, 1), (209083, 400009002, 0))
TUNING_TEST = (209033, 400009102)
SUEASHI = (209045, 400009003)
REPORTER = (209035, 400000100)
STORY_BEATS = {
    16: (209046, 400009004), 29: (209047, 400009008), 32: (209048, 400009009),
    54: (209049, 400009017), 56: (209050, 400009018), 66: (209051, 400009022),
    69: (209052, 400009023)}
SUMMER_START = {37: (209044, 400009013)}
UGE_OPEN = {24: (209001, 400009005), 36: (209002, 400009010), 48: (209003, 400009014),
            60: (209004, 400009019), 72: (209005, 400009024)}
UGE_RESULT = {2: {1: (209006, 400009006), 2: (209008, 400009011), 3: (209010, 400009015),
                  4: (209012, 400009020), 5: (209014, 400009025)},
              1: {1: (209053, 400009007), 2: (209054, 400009012), 3: (209055, 400009016),
                  4: (209056, 400009021), 5: (209057, 400009026)}}
UGE_RESULT_TIMING = 17
UGE_SHOW_CLEAR = 901
REPORT = (209062, 400009027)
ENDING = (209041, 400009028)
ENDING_REWARD = {True: (45, 175, 3), False: (40, 125, 1)}
STORY_ENDING_TRIGGERS = (2, 10)
MAINTENANCE = {"outing": 209036, "rest": 209037, "infirmary": 209043}
MAINTENANCE_STORY = 400009100
RACE_WATCH = {True: 209038, False: 209039}
RACE_WATCH_STORY = 400009101
HEAD_HINTS = {False: (209040, 400009103), True: (209084, 400009104)}
PRIO_ARRIVAL = CE.PRIO_TRAILING + 5

REPORT_HINTS = ((1023, 203302, 203301), (1050, 201452, 201453), (1036, 200332, 200331),
                (1083, 200592, 200591), (1084, 203312, 203311), (0, 200721, 200721))

SCREEN_EVENTS = frozenset({TUNING_TEST[0], *(e for e, _s in UGE_OPEN.values()),
                           *(e for grade in UGE_RESULT.values() for e, _s in grade.values())})


def _beat(event_id, story_id, *, choices=None, n_choices=1, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("mecha: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=[CE.Choice(effects=[]) for _ in range(n_choices)] if choices is None else choices,
        chara_id=0, once_key=once or f"mecha:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="mecha", show_clear=show_clear, show_clear_sort_id=sort_id)


def closing_pending(full_state: dict, turn: int) -> bool:
    ids = []
    if turn == 13:
        ids = [SUEASHI[0], REPORTER[0]]
    elif turn in STORY_BEATS:
        ids = [STORY_BEATS[turn][0]]
    elif turn == impl.FINALS_FIRST_TURN:
        ids = [REPORT[0]]
    return any(not CE.already_fired(full_state, f"mecha:{e}") for e in ids)


def opening_events() -> list:
    out = [_beat(e, s, n_choices=n, priority=CE.PRIO_FRONT,
                 resolver="mecha_roster" if e == 209026 else None)
           for e, s, n in OPENING]
    return [e for e in out if e is not None]


def ending_event(full_state: dict) -> CE.Event | None:
    stats, sp, hint = ENDING_REWARD[impl.last_uge_s(impl.state(full_state))]
    return _beat(*ENDING, timing=3, priority=CE.PRIO_FRONT, resolver="mecha_ending",
                 choices=[CE.Choice(effects=[
                     {"type": "all_stats", "value": f"+{stats}"},
                     {"type": "skill_point", "value": f"+{sp}"},
                     {"type": "skill_hint", "skill_id": impl.SKILL_KESHIKI, "value": f"+{hint}"}])])


def report_choices(chara_info: dict) -> list:
    linked = impl.linked(chara_info)
    return [CE.Choice(effects=[{"type": "skill_hint",
                                "skill_id": gold if (not chara or chara in linked) else white,
                                "value": "+1"}])
            for chara, white, gold in REPORT_HINTS]


def uge_result_event(full_state: dict, chara_info: dict, schedule_id: int, grade: int,
                     turn: int) -> CE.Event | None:
    event_id, story = UGE_RESULT[2 if grade >= 2 else 1][int(schedule_id)]
    return _beat(event_id, story, timing=UGE_RESULT_TIMING, priority=CE.PRIO_FRONT,
                 resolver="mecha_uge_result", once=f"mecha:uge:{schedule_id}",
                 choices=[CE.Choice(effects=impl.uge_reward_effects(schedule_id, grade))],
                 show_clear=UGE_SHOW_CLEAR, sort_id=impl.RI.goal_sort_id(chara_info, turn),
                 payload={"schedule_id": int(schedule_id), "result_type": int(grade), "turn": int(turn)})


def head_hint_event(full_state: dict, chara_info: dict, due: dict) -> CE.Event | None:
    st = impl.state(full_state)
    by_pos = formula.RF.deck(chara_info)
    rng = impl.rng_for(full_state, "head", due.get("turn"))
    effects: list = []
    cards = 0
    for pos in due.get("partners") or ():
        sid = by_pos.get(int(pos))
        if sid and formula.RF.is_normal_card(sid):
            cards += 1
            effects += impl.RI.card_hint_effects(chara_info, sid, rng)
    if not cards:
        return None
    late = impl.uges_done(st) >= impl.HEAD_HINTS_LATE_UGES
    return _beat(*HEAD_HINTS[late], from_endpoint=True, timing=6, priority=CE.PRIO_SCENARIO,
                 once=f"mecha:head:{due.get('turn')}", choices=[CE.Choice(effects=effects)])


@CE.producer("mecha_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def mecha_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    if arrived == impl.TUNING_TURN:
        out.append(_beat(*TUNING_TEST, resolver="mecha_first_tuning", priority=PRIO_ARRIVAL))
    if arrived in SUMMER_START:
        out.append(_beat(*SUMMER_START[arrived], n_choices=0, priority=PRIO_ARRIVAL))
    if played == 13:
        out.append(_beat(*SUEASHI, timing=6, from_endpoint=True, priority=CE.PRIO_TRAILING,
                         choices=[CE.Choice(effects=[{"type": "skill_hint", "skill_id": impl.SKILL_SUEASHI,
                                                      "value": "+3"}])]))
        out.append(_beat(*REPORTER, timing=6, from_endpoint=True, priority=CE.PRIO_TRAILING))
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], n_choices=0, timing=6, from_endpoint=True,
                         priority=CE.PRIO_TRAILING))
    if played in UGE_OPEN and impl.uge_pending(st, played):
        out.append(_beat(*UGE_OPEN[played], timing=6, from_endpoint=True, priority=CE.PRIO_TRAILING,
                         resolver="mecha_uge_open", payload={"turn": played}))
    if played == impl.FINALS_FIRST_TURN:
        out.append(_beat(*REPORT, timing=6, from_endpoint=True, priority=CE.PRIO_TRAILING,
                         choices=report_choices(ctx.chara_info)))
    return [e for e in out if e is not None]


@CE.producer("mecha_due", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def mecha_due(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    due, st["due"] = list(st["due"]), []
    out = []
    for d in due:
        kind, turn = d.get("kind"), int(d.get("turn") or ctx.turn or 0)
        if kind in MAINTENANCE:
            out.append(_beat(MAINTENANCE[kind], MAINTENANCE_STORY, timing=6, from_endpoint=True,
                             priority=CE.PRIO_SPECIAL, resolver="mecha_gauge",
                             once=f"mecha:maint:{turn}"))
        elif kind == "race":
            out.append(_beat(RACE_WATCH[bool(d.get("goal"))], RACE_WATCH_STORY, timing=3,
                             priority=CE.PRIO_SPECIAL, resolver="mecha_race_watch",
                             once=f"mecha:race:{turn}"))
    return [e for e in out if e is not None]


def _ps(chara_info: dict, value: int) -> None:
    chara_info["playing_state"] = value


@CE.resolver("mecha_roster")
def _roster(full_state, chara_info, event, choice_number, **kw):
    impl.recruit_members(full_state, chara_info)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("mecha_first_tuning")
def _first_tuning(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["tp"] = int(st["tp"]) + impl.EN_FIRST + impl.link_en(chara_info)
    _ps(chara_info, 26)
    return {}


@CE.resolver("mecha_uge_open")
def _uge_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    _ps(chara_info, 25)
    return {}


@CE.resolver("mecha_uge_result")
def _uge_result(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    impl.apply_uge_result(full_state, chara_info, int(payload.get("schedule_id") or 0),
                          int(payload.get("result_type") or 1))
    impl.RI.refresh_home(full_state, chara_info)
    _ps(chara_info, 26)
    return {}


@CE.resolver("mecha_gauge")
def _gauge(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    if impl.add_energy(st, 1):
        impl.flag(st, "max_flag", True)
    return {}


@CE.resolver("mecha_race_watch")
def _race_watch(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    capped = impl.add_research(st, [(s, impl.RACE_WATCH_RESEARCH) for s in M.RESEARCH_ORDER])
    impl.flag(st, "not_up", capped)
    if impl.add_energy(st, 1):
        impl.flag(st, "max_flag", True)
    return {}


@CE.resolver("mecha_ending")
def _ending(full_state, chara_info, event, choice_number, **kw):
    _ps(chara_info, 27)
    return {}
