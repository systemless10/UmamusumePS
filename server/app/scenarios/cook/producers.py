
from __future__ import annotations

import logging

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = ((208043, 400008000, 1), (208006, 400008001, 1), (208009, 400008002, 0))
REPORTER = (208053, 400000100)
STORY_BEATS = {
    13: (208044, 400008003), 29: (208045, 400008007), 33: (208046, 400008008),
    47: (208047, 400008013), 55: (208048, 400008017), 65: (208049, 400008021),
    71: (208023, 400008023)}
ARRIVALS = {35: (208010, 400008009)}
ADVICE = (208022, 400008022)
NISHINO = 1051
NISHINO_BEATS = {32: (208140, 400008340), 43: (208141, 400008341),
                 66: (208142, 400008342), 70: (208143, 400008343)}

TASTING_OPEN = {1: (208001, 400008004), 2: (208002, 400008010), 3: (208003, 400008014),
                4: (208004, 400008018), 5: (208005, 400008024)}
TASTING_RESULT = {
    (1, 2): (208011, 400008005, 5, 40, None), (1, 1): (208012, 400008006, 3, 30, None),
    (2, 2): (208013, 400008011, 10, 50, None), (2, 1): (208014, 400008012, 5, 40, None),
    (3, 2): (208015, 400008015, 15, 60, None), (3, 1): (208016, 400008016, 10, 50, None),
    (4, 2): (208017, 400008019, 20, 70, None), (4, 1): (208018, 400008020, 15, 60, None),
    (5, 3): (208019, 400008025, 25, 80, (202791, 1)), (5, 2): (208020, 400008026, 25, 80, (202791, 1)),
    (5, 1): (208021, 400008027, 20, 70, None)}
TASTING_RESULT_TIMING = 16
TASTING_SHOW_CLEAR = 801

ENDING = {
    3: (208029, 400008028, {"stamina": 30, "speed": 60, "power": 60, "guts": 60, "wiz": 60}, 150, (210321, 3)),
    2: (208038, 400008028, {"all": 30}, 100, (210321, 1)),
    1: (208030, 400008029, {"all": 25}, 80, (210322, 1))}
STORY_ENDING_TRIGGERS = (2, 10)

CARE = (208007, 400008100)
CARE_CAMP = (208050, 400008101)
CARE_YAYOI = (208054, 400008100)
CARE_FAIL = (208031, 400008100)
AWAKEN_STORY = 400008102

ADVICE_HINTS = ((1104, 201262, 201261), (1001, 201352, 201351), (1051, 200362, 200361),
                (1028, 203012, 203011), (1030, 201342, 201341), (9002, 203022, 203021))

PRIO_ARRIVAL = CE.PRIO_TRAILING + 5

SCREEN_EVENTS = frozenset({*(e for e, _s in TASTING_OPEN.values()),
                           TASTING_RESULT[(5, 3)][0], TASTING_RESULT[(5, 2)][0],
                           TASTING_RESULT[(5, 1)][0]})


def _beat(event_id, story_id, *, choices=None, n_choices=1, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("cook: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=[CE.Choice(effects=[]) for _ in range(n_choices)] if choices is None else choices,
        chara_id=0, once_key=once or f"cook:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="cook", show_clear=show_clear, show_clear_sort_id=sort_id)


def _stat_effects(stats: dict, sp: int, hint) -> list:
    if "all" in stats:
        out = [{"type": "all_stats", "value": f"+{stats['all']}"}]
    else:
        out = [{"type": k, "value": f"+{v}"} for k, v in stats.items()]
    out.append({"type": "skill_point", "value": f"+{sp}"})
    if hint:
        out.append({"type": "skill_hint", "skill_id": hint[0], "value": f"+{hint[1]}"})
    return out


def closing_pending(full_state: dict, chara_info: dict, turn: int) -> bool:
    ids = []
    if turn in STORY_BEATS:
        ids.append(STORY_BEATS[turn][0])
    if turn == 13:
        ids.append(REPORTER[0])
    if turn == 67:
        ids.append(ADVICE[0])
    if impl.trainee_chara(chara_info) == NISHINO and turn in NISHINO_BEATS:
        ids.append(NISHINO_BEATS[turn][0])
    return any(not CE.already_fired(full_state, f"cook:{e}") for e in ids)


def opening_events() -> list:
    out = [_beat(e, s, n_choices=n, priority=CE.PRIO_FRONT,
                 resolver="cook_friends" if e == 208006 else None)
           for e, s, n in OPENING]
    return [e for e in out if e is not None]


def ending_event(full_state: dict) -> CE.Event | None:
    result = impl.last_result(impl.state(full_state)) or 1
    event_id, story, stats, sp, hint = ENDING[result]
    return _beat(event_id, story, timing=3, priority=CE.PRIO_FRONT, once="cook:ending",
                 choices=[CE.Choice(effects=_stat_effects(stats, sp, hint))])


def advice_choices(chara_info: dict) -> list:
    linked = impl.linked(chara_info)
    return [CE.Choice(effects=[{"type": "skill_hint", "skill_id": gold if chara in linked else white,
                                "value": "+1"}])
            for chara, white, gold in ADVICE_HINTS]


def tasting_result_event(chara_info: dict, cook_type: int, result: int, turn: int) -> CE.Event | None:
    event_id, story, stats, sp, hint = TASTING_RESULT[(int(cook_type), int(result))]
    return _beat(event_id, story, timing=TASTING_RESULT_TIMING, priority=CE.PRIO_FRONT,
                 resolver="cook_tasting_result", once=f"cook:tasting:{cook_type}",
                 choices=[CE.Choice(effects=_stat_effects({"all": stats}, sp, hint))],
                 show_clear=TASTING_SHOW_CLEAR, sort_id=impl.RI.goal_sort_id(chara_info, turn),
                 payload={"cook_type": int(cook_type), "result": int(result), "turn": int(turn)})


def _raced(st: dict, turn: int) -> bool:
    lc = st["last_cmd"]
    return bool(lc) and int(lc.get("turn") or 0) == int(turn) and int(lc.get("command_type") or 0) == 4


@CE.producer("cook_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def cook_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    close = 3 if _raced(st, played) else 6
    out = []
    if arrived in ARRIVALS:
        out.append(_beat(*ARRIVALS[arrived], n_choices=0, priority=PRIO_ARRIVAL))
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], n_choices=0, timing=close, from_endpoint=close == 6,
                         priority=CE.PRIO_TRAILING))
    if played == 13:
        out.append(_beat(*REPORTER, timing=close, from_endpoint=close == 6, priority=CE.PRIO_TRAILING))
    if played == 67:
        out.append(_beat(*ADVICE, timing=close, from_endpoint=close == 6, priority=CE.PRIO_TRAILING,
                         choices=advice_choices(ctx.chara_info)))
    if impl.trainee_chara(ctx.chara_info) == NISHINO and played in NISHINO_BEATS:
        out.append(_beat(*NISHINO_BEATS[played], n_choices=0, timing=close, from_endpoint=close == 6,
                         priority=CE.PRIO_TRAILING))
    ct = M.tasting_for_turn(played)
    if ct and impl.tasting_pending(st, played):
        out.append(_beat(*TASTING_OPEN[ct], timing=close, from_endpoint=close == 6,
                         priority=CE.PRIO_TRAILING, resolver="cook_tasting_open",
                         payload={"turn": played}))
    return [e for e in out if e is not None]


@CE.producer("cook_due", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def cook_due(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    due, st["due"] = list(st["due"]), []
    out = []
    for d in due:
        kind, turn = d.get("kind"), int(d.get("turn") or ctx.turn or 0)
        camp = impl.zone(turn) == "camp"
        payload = {"key": kind, "turn": turn, "spec": d.get("spec")}
        if kind in ("rest", "outing"):
            ev = CARE_CAMP if camp and kind == "outing" else CARE
            out.append(_beat(*ev, timing=6, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                             resolver="cook_care", payload=payload, once=f"cook:care:{turn}"))
        elif kind == "yayoi":
            out.append(_beat(*CARE_YAYOI, timing=6, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                             resolver="cook_care", payload=payload, once=f"cook:care:{turn}"))
        elif kind == "race":
            ev = CARE_CAMP if camp else CARE
            out.append(_beat(*ev, timing=3, priority=CE.PRIO_SPECIAL, resolver="cook_care",
                             payload=payload, once=f"cook:care:{turn}"))
        elif kind == "fail":
            out.append(_beat(*CARE_FAIL, timing=6, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                             resolver="cook_care", payload=payload, once=f"cook:care:{turn}"))
        elif kind == "awaken":
            out.append(_beat(int(d["event_id"]), AWAKEN_STORY, timing=6, from_endpoint=True,
                             priority=CE.PRIO_SPECIAL, resolver="cook_awaken"))
    return [e for e in out if e is not None]


@CE.resolver("cook_friends")
def _friends(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    impl.recruit_friends(full_state, chara_info)
    n = (impl.OPENING_MATERIALS_LINK if impl.trainee_chara(chara_info) in impl.LINK_TRAINEES
         else impl.OPENING_MATERIALS)
    impl.flag(st, "event_gain", impl.add_materials(st, n))
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("cook_care")
def _care(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    p = event.payload or {}
    turn = int(p.get("turn") or impl.turn_of(chara_info))
    if p.get("spec"):
        impl.land_spec(st, p["spec"], turn)
    else:
        log.warning("cook: no %s care for turn %s", p.get("key"), turn)
    return {}


@CE.resolver("cook_awaken")
def _awaken(full_state, chara_info, event, choice_number, **kw):
    impl.RI.raise_all_facilities(full_state, chara_info)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("cook_tasting_open")
def _tasting_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    chara_info["playing_state"] = 23
    return {}


@CE.resolver("cook_tasting_result")
def _tasting_result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    p = event.payload or {}
    ct = int(p.get("cook_type") or 0)
    impl.unlock_after_tasting(full_state, ct, int(p.get("result") or 1))
    if ct == 5:
        chara_info["playing_state"] = 24
    else:
        st["hold_turn"] = 0
        chara_info["playing_state"] = 1
    impl.RI.refresh_home(full_state, chara_info)
    return {}
