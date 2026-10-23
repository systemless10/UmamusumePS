
from __future__ import annotations

import logging

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from . import impl
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = ((207019, 400007000, 1, None), (207105, 400007105, 1, "sport_items"),
           (207029, 400007001, 0, None))
STORY_BEATS = {
    12: (207020, 400007002, 1), 13: (101007, 400000100, 1),
    25: (207021, 400007006, 0), 41: (207022, 400007010, 0), 42: (207023, 400007011, 0),
    50: (207024, 400007015, 0), 51: (207106, 400007106, 0), 65: (207025, 400007019, 0),
    66: (207026, 400007020, 1)}
MOOD_BEATS = frozenset({207026})
ARRIVALS = {35: (207104, 400007104), 59: (207104, 400007104)}

COMPE_OPEN = {1: (207004, 400007003), 2: (207007, 400007007), 3: (207010, 400007012),
              4: (207013, 400007016), 5: (207016, 400007021)}
COMPE_RESULT = {
    (1, 1): (207005, 400007004, 5, 40, 1000, None), (1, 2): (207006, 400007005, 3, 30, 100, None),
    (2, 1): (207008, 400007008, 10, 50, 5000, None), (2, 2): (207009, 400007009, 5, 40, 500, None),
    (3, 1): (207011, 400007013, 15, 60, 15000, None), (3, 2): (207012, 400007014, 10, 50, 1500, None),
    (4, 1): (207014, 400007017, 20, 70, 20000, (210302, 1)), (4, 2): (207015, 400007018, 15, 60, 2000, None),
    (5, 1): (207017, 400007022, 25, 80, 22000, (200511, 1)), (5, 2): (207018, 400007023, 20, 70, 5000, None)}
RESULT_TIMING = 15
RESULT_SHOW_CLEAR = 701

ENDING = {1: (207027, 400007024, 30, 90, (210301, 1)), 2: (207028, 400007025, 20, 70, None)}
STORY_ENDING_TRIGGERS = (2, 10)

PRIO_ARRIVAL = CE.PRIO_TRAILING + 5

SCREEN_EVENTS = frozenset({*(e for e, _s in COMPE_OPEN.values()),
                           COMPE_RESULT[(5, 1)][0], COMPE_RESULT[(5, 2)][0]})


def _beat(event_id, story_id, *, choices=None, n_choices=1, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("sport: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=[CE.Choice(effects=[]) for _ in range(n_choices)] if choices is None else choices,
        chara_id=0, once_key=once or f"sport:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="sport", show_clear=show_clear, show_clear_sort_id=sort_id)


def _stat_effects(stats: int, sp: int, fans: int, hint) -> list:
    out = [{"type": "all_stats", "value": f"+{stats}"}, {"type": "skill_point", "value": f"+{sp}"}]
    if fans:
        out.append({"type": "fans", "value": f"+{fans}"})
    if hint:
        out.append({"type": "skill_hint", "skill_id": hint[0], "value": f"+{hint[1]}"})
    return out


def _raced(st: dict, turn: int) -> bool:
    return int(st.get("raced_turn") or 0) == int(turn)


def closing_pending(full_state: dict, chara_info: dict, turn: int) -> bool:
    beat = STORY_BEATS.get(int(turn))
    return bool(beat) and not CE.already_fired(full_state, f"sport:{beat[0]}")


def opening_events() -> list:
    out = [_beat(e, s, n_choices=n, priority=CE.PRIO_FRONT, resolver=res) for e, s, n, res in OPENING]
    return [e for e in out if e is not None]


def ending_event(full_state: dict) -> CE.Event | None:
    res = impl.last_result(impl.state(full_state))
    event_id, story, stats, sp, hint = ENDING[1 if int(res.get("result_state") or 2) == 1 else 2]
    return _beat(event_id, story, timing=3, priority=CE.PRIO_FRONT, once="sport:ending",
                 choices=[CE.Choice(effects=_stat_effects(stats, sp, 0, hint))])


def compe_result_event(chara_info: dict, result: dict, turn: int) -> CE.Event | None:
    ct, rs = int(result["compe_type"]), int(result["result_state"])
    event_id, story, stats, sp, fans, hint = COMPE_RESULT[(ct, rs)]
    return _beat(event_id, story, timing=RESULT_TIMING, priority=CE.PRIO_FRONT,
                 resolver="sport_compe_result", once=f"sport:compe:{ct}",
                 choices=[CE.Choice(effects=_stat_effects(stats, sp, fans, hint))],
                 show_clear=RESULT_SHOW_CLEAR, sort_id=impl.RI.goal_sort_id(chara_info, turn),
                 payload={"compe_type": ct, "turn": int(turn)})


@CE.producer("sport_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def sport_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    close = 3 if _raced(st, played) else 6
    out = []
    if arrived in ARRIVALS:
        out.append(_beat(*ARRIVALS[arrived], n_choices=0, priority=PRIO_ARRIVAL,
                         once=f"sport:arrival:{arrived}"))
    if played in STORY_BEATS:
        e, s, n = STORY_BEATS[played]
        choices = ([CE.Choice(effects=[{"type": "motivation", "value": "+1"}])]
                   if e in MOOD_BEATS else None)
        out.append(_beat(e, s, n_choices=n, choices=choices, timing=close, from_endpoint=close == 6,
                         priority=CE.PRIO_TRAILING))
    ct = impl.COMPETITIONS.get(played)
    if ct and impl.compe_pending(st, played):
        out.append(_beat(*COMPE_OPEN[ct[0]], timing=close, from_endpoint=close == 6,
                         priority=CE.PRIO_TRAILING, resolver="sport_compe_open",
                         payload={"turn": played}))
    return [e for e in out if e is not None]


@CE.resolver("sport_items")
def _items(full_state, chara_info, event, choice_number, **kw):
    impl.refill_items(impl.state(full_state), 1)
    return {}


@CE.resolver("sport_compe_open")
def _compe_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    chara_info["playing_state"] = impl.PS_COMPETITION
    return {}


@CE.resolver("sport_compe_result")
def _compe_result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    if int((event.payload or {}).get("compe_type") or 0) == 5:
        chara_info["playing_state"] = impl.PS_COMPETITION_LIVE
    else:
        st["hold_turn"] = 0
        chara_info["playing_state"] = 1
        impl.RI.refresh_home(full_state, chara_info)
    return {}
