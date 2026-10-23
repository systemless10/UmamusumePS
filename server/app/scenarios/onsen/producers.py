
from __future__ import annotations

import logging

from ... import career_events as CE, event_engine
from ...handlers.career_producers import story_playable
from . import dig as D, impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = (212002, 400012000)
FIRST_PICK = (212003, 400012001)
STORY_BEATS = {
    19: (212032, 400012004), 30: (212033, 400012007), 43: (212034, 400012009),
    47: (212035, 400012010), 54: (212036, 400012013), 67: (212037, 400012015),
    71: (212038, 400012016)}
TOWN_BEAT = (212031, 400012003)
REPORTER = (212016, 400000100)
SUMMER_START = {37: (212029, 400012008), 61: (212030, 400012014)}
LEGEND = (212042, 400012111)
PARTY_OPEN = {24: (212019, 400012105), 48: (212020, 400012106), 72: (212021, 400012107)}
LINK_BEAT = (212041, 400012020)
ENDING = {True: (212039, 400012021), False: (212040, 400012022)}
ENDING_REWARD = {True: (50, 250, 2), False: (20, 100, 1)}
STORY_ENDING_TRIGGERS = (2, 10)
KIYOKO_ENDING = (37, 809050023)
KIYOKO_DONE_ENDING = (38, 809050022)
KIYOKO_DONE_PAY = (("power", 10), ("guts", 10), ("wisdom", 10), ("skill_points", 40))
FINALS_FIRST_TURN = 73
PRIO_ARRIVAL = CE.PRIO_TRAILING + 5

DIG_EVENTS = {"rest": (212004, 6), "race": (212005, 3), "outing": (212006, 6)}
DIG_STORY = 400012104
COMPLETE_EVENTS = {"train": (212000, 212068, 212072), "rest": (212001, 212069, 212073),
                   "race": (212017, 212070, 212074), "outing": (212018, 212071, 212075)}
COMPLETE_STORIES = (400012108, 400012109, 400012110)
COMPLETE_TIMING = {"train": 6, "rest": 6, "race": 3, "outing": 6}

LINK_HINTS = ((1100, 202472, 202471), (1080, 200432, 200431), (1099, 203602, 203601),
              (1026, 202462, 202461), (1003, 202372, 202371), (9050, 200512, 200511))

SCREEN_EVENTS = frozenset({FIRST_PICK[0], LEGEND[0], *(e for e, _s in PARTY_OPEN.values()),
                           *impl.RESULT_STORIES, *(e for evs in COMPLETE_EVENTS.values() for e in evs),
                           ENDING[True][0], ENDING[False][0]})


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def _beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("onsen: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        once_key=once or f"onsen:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="onsen")


def opening_event() -> CE.Event | None:
    return _beat(*OPENING, priority=CE.PRIO_FRONT)


def reporter_event() -> CE.Event | None:
    return _beat(*REPORTER, timing=6, priority=CE.PRIO_FRONT)


def ending_event(full_state: dict) -> CE.Event | None:
    good = impl.last_result(impl.state(full_state), 3) >= 2
    stats, sp, hint = ENDING_REWARD[good]
    return _beat(*ENDING[good], timing=3, priority=CE.PRIO_FRONT, resolver="onsen_ending",
                 choices=[CE.Choice(effects=[
                     {"type": "all_stats", "value": f"+{stats}"},
                     {"type": "skill_point", "value": f"+{sp}"},
                     {"type": "skill_hint", "skill_id": impl.SKILL_HOYO, "value": f"+{hint}"}])])


def kiyoko_dates_done(chara_info: dict, k) -> bool:
    row = next((e for e in chara_info.get("evaluation_info_array") or ()
                if isinstance(e, dict) and int(e.get("target_id") or 0) == k[0]), None)
    return int((row or {}).get("story_step") or 0) >= 5


def kiyoko_done_ending(chara_info: dict, k) -> CE.Event | None:
    pay = event_engine.boost_effects(
        [{"type": t, "value": f"+{v}"} for t, v in KIYOKO_DONE_PAY], k[1], chara_info)
    ev = _beat(*KIYOKO_DONE_ENDING, timing=3, priority=CE.PRIO_FRONT,
               choices=[CE.Choice(effects=pay)])
    if ev:
        ev.support_card_id = k[1]
    return ev


def kiyoko_ending(chara_info: dict) -> CE.Event | None:
    k = impl.kiyoko_card(chara_info)
    if not k or k[2] == 1:
        return None
    if kiyoko_dates_done(chara_info, k):
        return kiyoko_done_ending(chara_info, k)
    ev = _beat(*KIYOKO_ENDING, timing=3, priority=CE.PRIO_FRONT)
    if ev:
        ev.support_card_id = k[1]
    return ev


def complete_event(st: dict, kind: str, turn: int) -> CE.Event | None:
    first, later, legend = COMPLETE_EVENTS[kind]
    sel = int(st["selected"])
    if sel == M.LEGEND_ONSEN:
        event_id, story = legend, COMPLETE_STORIES[2]
    elif int(st["completions"]) == 0:
        event_id, story = first, COMPLETE_STORIES[0]
    else:
        event_id, story = later, COMPLETE_STORIES[1]
    return _beat(event_id, story, timing=COMPLETE_TIMING[kind], priority=CE.PRIO_FRONT,
                 once=f"onsen:complete:{sel}", resolver="onsen_complete",
                 payload={"onsen_id": sel, "turn": int(turn)})


def link_choices(chara_info: dict) -> list:
    linked = impl.linked_charas(chara_info)
    return [CE.Choice(effects=[{"type": "skill_hint", "skill_id": gold if chara in linked else normal,
                                "value": "+1"}])
            for chara, normal, gold in LINK_HINTS]


@CE.producer("onsen_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def onsen_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    if arrived == 3:
        out.append(_beat(*FIRST_PICK, resolver="onsen_first_pick", priority=PRIO_ARRIVAL))
    if arrived in SUMMER_START:
        out.append(_beat(*SUMMER_START[arrived], resolver="onsen_summer", priority=PRIO_ARRIVAL))
    if played == 13:
        out.append(_beat(*TOWN_BEAT, choices=[], timing=6, from_endpoint=True))
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], choices=[], from_endpoint=True,
                         priority=CE.PRIO_TRAILING))
    if played == 65:
        out.append(_beat(*LEGEND, from_endpoint=True, priority=CE.PRIO_TRAILING,
                         resolver="onsen_legend", payload={"turn": played}))
    if played in PARTY_OPEN and impl.party_pending(st, played):
        out.append(_beat(*PARTY_OPEN[played], from_endpoint=True, priority=CE.PRIO_TRAILING,
                         resolver="onsen_party_open", payload={"turn": played}))
    if played == FINALS_FIRST_TURN:
        out.append(_beat(*LINK_BEAT, from_endpoint=True, timing=6,
                         choices=link_choices(ctx.chara_info)))
    return [e for e in out if e is not None]


@CE.producer("onsen_dig", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def onsen_dig(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    out = []
    due = st.get("complete_due") or {}
    if due:
        st["complete_due"] = {}
        out.append(complete_event(st, due.get("kind") or "train", int(due.get("turn") or ctx.turn)))
    due = st.get("dig_due") or {}
    if due:
        st["dig_due"] = {}
        kind = due.get("kind")
        event_id, timing = DIG_EVENTS[kind]
        played = int(due.get("turn") or ctx.turn or 0)
        out.append(_beat(event_id, DIG_STORY, from_endpoint=kind != "race", timing=timing,
                         once=f"onsen:dig:{played}:{event_id}", priority=CE.PRIO_SPECIAL,
                         resolver="onsen_dig",
                         payload={"kind": kind, "base": int(due.get("base") or 0), "turn": played}))
    return [e for e in out if e is not None]


def _ps(chara_info: dict, value: int) -> None:
    chara_info["playing_state"] = value


@CE.resolver("onsen_first_pick")
def _first_pick(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    k = impl.kiyoko_card(chara_info)
    if k:
        for row in chara_info.get("evaluation_info_array") or ():
            if isinstance(row, dict) and row.get("target_id") == k[0]:
                row["is_appear"] = 1
    impl.open_springs(st, impl.SPRINGS_OPENED_BY[3])
    impl.add_tickets(st, impl.FIRST_PICK_TICKETS)
    _ps(chara_info, 36)
    return {}


@CE.resolver("onsen_summer")
def _summer(full_state, chara_info, event, choice_number, **kw):
    impl.add_tickets(impl.state(full_state), impl.ticket_num(chara_info, M.TICKET_SUMMER_CAMP))
    return {}


@CE.resolver("onsen_dig")
def _dig(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    payload = event.payload or {}
    turn = int(payload.get("turn") or impl.turn_of(chara_info))
    digs = impl.dig_plan(full_state, chara_info, int(payload.get("base") or 0), turn)
    impl.apply_dig(full_state, chara_info, digs, pay=True)
    if impl.completed_now(st):
        ev = complete_event(st, payload.get("kind") or "race", turn)
        if ev:
            CE.emit(full_state, ev)
    return {}


@CE.resolver("onsen_complete")
def _complete(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    payload = event.payload or {}
    onsen = int(payload.get("onsen_id") or st["selected"])
    if onsen not in st["dug"]:
        st["dug"].append(onsen)
        st["completions"] = int(st["completions"]) + 1
    impl.add_tickets(st, impl.ticket_num(chara_info, M.TICKET_DIG_COMPLETE))
    st["hold_turn"] = int(payload.get("turn") or 0) or st["hold_turn"]
    _ps(chara_info, 36)
    log.info("onsen: spring %s finished, rank %s", onsen, impl.rank(st))
    return {}


@CE.resolver("onsen_legend")
def _legend(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    impl.open_springs(st, impl.SPRINGS_OPENED_BY[65])
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    _ps(chara_info, 36)
    return {}


@CE.resolver("onsen_party_open")
def _party_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    _ps(chara_info, 37)
    return {}


@CE.resolver("onsen_result")
def _result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    payload = event.payload or {}
    sched, result = int(payload.get("schedule_id") or 0), int(payload.get("result_type") or 0)
    if sched < 3 and result >= 2:
        RI_raise(full_state, chara_info)
    if sched == 1:
        impl.open_springs(st, impl.SPRINGS_OPENED_BY[24])
    elif sched == 2:
        impl.open_springs(st, impl.SPRINGS_OPENED_BY[48])
    if sched == 3:
        impl.add_tickets(st, impl.ticket_num(chara_info, M.TICKET_URA_FINAL_BEFORE))
        st["hold_turn"] = 0
        _ps(chara_info, 1)
    else:
        _ps(chara_info, 36)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


def RI_raise(full_state, chara_info) -> None:
    impl.RI.raise_all_facilities(full_state, chara_info)


@CE.resolver("onsen_ending")
def _ending(full_state, chara_info, event, choice_number, **kw):
    _ps(chara_info, 42)
    k = impl.kiyoko_card(chara_info)
    if k and k[2] == 1:
        if kiyoko_dates_done(chara_info, k):
            ev = kiyoko_done_ending(chara_info, k)
        else:
            ev = _beat(*KIYOKO_ENDING, timing=3, priority=CE.PRIO_FRONT)
            if ev:
                ev.support_card_id = k[1]
        if ev:
            CE.emit(full_state, ev)
    return {}
