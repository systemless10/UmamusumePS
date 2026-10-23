
from __future__ import annotations

import logging

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from ..ramen import formula as RF
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = (211006, 400011000)
TUTORIAL = (211029, 400011001)
TEAM = (211079, 400011029)
REPORTER = (211022, 400000100)
STORY_BEATS = {
    11: (211030, 400011002), 17: (211031, 400011006), 29: (211032, 400011010),
    41: (211033, 400011014), 47: (211034, 400011015), 53: (211035, 400011019),
    67: (211037, 400011024), 68: (211038, 400011025)}
SUMMER_BEATS = {35: (211069, 400011104), 59: (211078, 400011104)}
CAMP_TICKET = (211068, 400011108)
FREE_TICKETS = (211023, 400011110)
LINK_BEAT = (211036, 400011023)
CHECK_POINT_OPEN = {12: (211001, 400011003), 24: (211002, 400011007), 36: (211003, 400011011),
                    48: (211004, 400011016), 60: (211005, 400011020)}
LIVE_OPEN = (211039, 400011026)
LIVE_SHOW_CLEAR = 1105
COMPLETE = (211070, 400011109)
TICKET_EVENTS = {"train": (211067, 400011107, 6), "event": (211076, 400011107, 10)}
RACE_POINTS = (211074, 400011105)
TUCKER_REWARD = (211075, 400011111)
TUCKER_REWARD_SP = 3
ENDING = {True: (211040, 400011027), False: (211041, 400011028)}
ENDING_REWARD = {True: (55, 540, 210361, 1), False: (45, 420, 210362, 3)}
STORY_ENDING_TRIGGER = 10
LEVELUP_EVENTS = {101: 211024, 102: 211025, 103: 211026, 105: 211027, 106: 211028}
TRAINEE_LINK_EVENTS = {1027: {13: (211058, 400011316), 46: (211059, 400011317),
                              68: (211060, 400011318), 70: (211061, 400011319)}}

SCREEN_EVENTS = frozenset({TEAM[0], LIVE_OPEN[0], *(e for e, _s in CHECK_POINT_OPEN.values()),
                           *(e for e, _s in impl.RESULT_EVENTS.values())})


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def _beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("pioneer: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        once_key=once or f"pioneer:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="pioneer", show_clear=show_clear,
        show_clear_sort_id=sort_id)


def opening_event() -> CE.Event | None:
    return _beat(*OPENING, priority=CE.PRIO_FRONT)


def ending_event(full_state: dict) -> CE.Event | None:
    good = impl.all_great(impl.state(full_state))
    stats, sp, skill, level = ENDING_REWARD[good]
    return _beat(*ENDING[good], timing=3, priority=CE.PRIO_FRONT, choices=[CE.Choice(effects=[
        {"type": "all_stats", "value": f"+{stats}"},
        {"type": "skill_point", "value": f"+{sp}"},
        {"type": "skill_hint", "skill_id": skill, "value": f"+{level}"}])])


def complete_event() -> CE.Event | None:
    return _beat(*COMPLETE, choices=[], priority=CE.PRIO_FRONT)


def ticket_event(full_state: dict, chara_info: dict, how: str, batch: int, cp: int,
                 turn: int) -> CE.Event | None:
    event_id, story_id, timing = TICKET_EVENTS[how]
    return _beat(event_id, story_id, timing=timing, priority=CE.PRIO_SPECIAL,
                 once=f"pioneer:ticket:{cp}:{batch}", resolver="pioneer_ticket",
                 payload={"batch": batch, "cp": cp, "turn": int(turn)},
                 choices=[CE.Choice(effects=impl.random_hint(full_state, chara_info,
                                                             f"ticket{cp}:{batch}"))])


def emit_tickets(full_state: dict, chara_info: dict, crossed, how: str, turn: int) -> None:
    st = impl.state(full_state)
    for batch in crossed or ():
        if turn in impl.SUMMER_TURNS:
            continue
        st["due"] = [d for d in st["due"] if not (d.get("kind") == "ticket" and d.get("batch") == batch)]
        ev = ticket_event(full_state, chara_info, how, batch, impl.current_plan(st), turn)
        if ev:
            CE.emit(full_state, ev)


def link_choices(chara_info: dict) -> list:
    linked = impl.linked_charas(chara_info)
    return [CE.Choice(effects=[{"type": "skill_hint", "skill_id": gold if chara in linked else white,
                                "value": "+1"}])
            for chara, white, gold in impl.LINK_HINTS]


def _raced(ctx: CE.Ctx, turn: int) -> bool:
    return any(int(r.get("turn") or 0) == turn for r in ctx.race_history
               if isinstance(r, dict))


@CE.producer("pioneer_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def pioneer_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    if arrived == impl.FIRST_PLAN_TURN:
        out.append(_beat(*TUTORIAL, choices=[]))
        out.append(_beat(*TEAM, resolver="pioneer_team"))
    if arrived in SUMMER_BEATS:
        out.append(_beat(*SUMMER_BEATS[arrived], choices=[]))
    if arrived == 41 and int(st["camp_ticket"]):
        out.append(_beat(*CAMP_TICKET, resolver="pioneer_camp_ticket",
                         choices=[CE.Choice(effects=impl.random_hint(ctx.full_state, ctx.chara_info,
                                                                     "camp"))]))
    if played == 13:
        out.append(_beat(*REPORTER, from_endpoint=True))
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], choices=[], from_endpoint=True))
    trainee = int(ctx.chara_info.get("card_id") or 0) // 100
    if played in TRAINEE_LINK_EVENTS.get(trainee, {}):
        out.append(_beat(*TRAINEE_LINK_EVENTS[trainee][played], choices=[], from_endpoint=True))
    if played == 64:
        out.append(_beat(*FREE_TICKETS, from_endpoint=True, resolver="pioneer_free_tickets"))
    if played == 65:
        out.append(_beat(*LINK_BEAT, from_endpoint=True, choices=link_choices(ctx.chara_info)))
    if played in CHECK_POINT_OPEN and impl.check_point_due(st, played):
        out.append(_beat(*CHECK_POINT_OPEN[played], timing=10 if _raced(ctx, played) else 6,
                         priority=CE.PRIO_TRAILING, resolver="pioneer_check_point_open",
                         payload={"turn": played}))
    if played == impl.LIVE_TURN and not st["live"]:
        out.append(_beat(*LIVE_OPEN, timing=3 if _raced(ctx, played) else 6,
                         priority=CE.PRIO_TRAILING, resolver="pioneer_live_open",
                         show_clear=LIVE_SHOW_CLEAR, sort_id=1, payload={"turn": played}))
    return [e for e in out if e is not None]


@CE.producer("pioneer_due", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def pioneer_due(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    out = []
    due, st["due"] = list(st["due"]), []
    for d in due:
        kind = d.get("kind")
        if kind == "ticket":
            out.append(ticket_event(ctx.full_state, ctx.chara_info, d.get("how") or "train",
                                    int(d.get("batch") or 1), int(d.get("cp") or 0),
                                    int(d.get("turn") or ctx.turn)))
        elif kind == "race":
            out.append(_beat(*RACE_POINTS, timing=3, priority=CE.PRIO_SPECIAL,
                             once=f"pioneer:race:{d.get('turn')}", resolver="pioneer_race_points",
                             payload={"points": int(d.get("points") or 0), "turn": int(d.get("turn") or 0)}))
        elif kind == "levelup":
            cmd = int(d.get("command_id") or 0)
            event_id = LEVELUP_EVENTS.get(cmd)
            if event_id:
                out.append(_beat(event_id, 400000005, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                                 once=f"pioneer:levelup:{cmd}:{d.get('level')}"))
        elif kind == "tucker":
            out.append(tucker_reward_event(ctx.chara_info, int(d.get("step") or 0),
                                           int(d.get("turn") or ctx.turn)))
    return [e for e in out if e is not None]


def tucker_reward_event(chara_info: dict, step: int, turn: int) -> CE.Event | None:
    sp = {"type": "skill_point", "value": f"+{TUCKER_REWARD_SP}"}
    return _beat(*TUCKER_REWARD, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                 once=f"pioneer:tucker:{step}", resolver="pioneer_tucker",
                 payload={"turn": turn}, choices=[CE.Choice(effects=[sp]), CE.Choice(effects=[sp])])


def _ps(chara_info: dict, value: int) -> None:
    chara_info["playing_state"] = value


@CE.resolver("pioneer_team")
def _team(full_state, chara_info, event, choice_number, **kw):
    from ...handlers import single_mode_team as smt
    impl.form_team(full_state, chara_info)
    smt._register_npc_unlocks(full_state, chara_info, event.event_id)
    _ps(chara_info, 35)
    return {}


@CE.resolver("pioneer_ticket")
def _ticket(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    impl.ticket_reached(full_state, chara_info, int(payload.get("batch") or 1))
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("pioneer_camp_ticket")
def _camp_ticket(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["camp_ticket"] = 0
    impl.grant_ticket(st)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("pioneer_free_tickets")
def _free_tickets(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["ticket_cap"] = impl.LATE_TICKET_CAP
    impl.grant_ticket(st, impl.LATE_FREE_TICKETS)
    return {}


@CE.resolver("pioneer_race_points")
def _race_points(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    turn = int(payload.get("turn") or impl.turn_of(chara_info))
    crossed = impl.add_points(full_state, chara_info, int(payload.get("points") or 0), "event", turn)
    emit_tickets(full_state, chara_info, crossed, "event", turn)
    return {}


@CE.resolver("pioneer_tucker")
def _tucker(full_state, chara_info, event, choice_number, **kw):
    if int(choice_number or 1) != 1:
        return {}
    t = impl.tucker_card(chara_info)
    if not t:
        return {}
    from ...handlers import single_mode_team as smt
    level = impl.formula.card_level(chara_info, t[1], smt._support_card_levels(full_state))
    turn = int((event.payload or {}).get("turn") or impl.turn_of(chara_info))
    crossed = impl.add_points(full_state, chara_info, M.tucker_reward(level), "event", turn)
    emit_tickets(full_state, chara_info, crossed, "event", turn)
    return {}


@CE.resolver("pioneer_check_point_open")
def _check_point_open(full_state, chara_info, event, choice_number, **kw):
    _ps(chara_info, 34)
    return {}


@CE.resolver("pioneer_result")
def _result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    payload = event.payload or {}
    cp, result = int(payload.get("check_point_type") or 0), int(payload.get("result_state") or 0)
    st["pt"] = 0
    st["crossed"] = 0
    st["built"] = 0
    st["plan"] = []
    if result == impl.RESULT_GREAT:
        for row in chara_info.get("evaluation_info_array") or ():
            if isinstance(row, dict) and row.get("target_id") == 102:
                row["evaluation"] = min(100, int(row.get("evaluation") or 0) + impl.DIRECTOR_BOND)
        if cp in impl.LEVEL_UP_EVALUATIONS:
            impl.grow_team(full_state, chara_info, cp)
    impl.sync_levels(full_state, chara_info)
    if cp >= 5:
        st["hold_turn"] = 0
        _ps(chara_info, 1)
    else:
        _ps(chara_info, 35)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("pioneer_live_open")
def _live_open(full_state, chara_info, event, choice_number, **kw):
    _ps(chara_info, 39)
    return {}
