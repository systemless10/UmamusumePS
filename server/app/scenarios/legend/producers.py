
from __future__ import annotations

import logging

from ... import career_events as CE
from ... import event_engine
from ...handlers.career_producers import story_playable
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = ((210034, 400010000), (210053, 400010001), (210054, 400010002))
APPEAR_LEGEND = 210053
REPORTER = (210071, 400000100)
STORY_BEATS = {12: (210037, 400010003, 0), 16: (210038, 400010004, 0), 20: (210039, 400010005, 0),
               29: (210003, 400010008, 0), 32: (210040, 400010009, 1), 45: (210042, 400010011, 0),
               51: (210043, 400010014, 0), 58: (210044, 400010015, 0), 66: (210046, 400010017, 0)}
ARRIVAL_BEATS = {37: (210041, 400010010, 0), 61: (210045, 400010016, 0), 72: (210048, 400010019, 1)}
ANNOUNCE = {24: (210001, 400010006), 48: (210004, 400010012), 72: (210007, 400010020)}
BOSS_ANNOUNCE = (210049, 400010021)
RESULT = {24: (210002, 400010007), 48: (210005, 400010013), 72: (210008, 400010022)}
BOSS_RESULT = (210009, 400010023)
RESULT_TIMING = {24: 18, 48: 18, 72: 19}
MAGAZINE = (210097, 400000091)
ENDING_ALL = (210051, 400010025)
ENDING_LOST = (210052, 400010026)
ENDING_BOSS = (210050, 400010024)
END_TIMING = 19
CARD_ENDING = {True: (34, 830241030), False: (33, 830241031)}
LOTTERY = (210032, 400010112)
GAUGE_UP = (210033, 400010113)
GAUGE_UP_SUMMER = (210093, 400010114)
GUIDANCE = {M.ST_LITE: (210026, 400010102), M.SPEED: (210027, 400010103),
            M.HAISEIKO: (210028, 400010104)}
SUPER_START = (210058, 400010106)
SUPER_END = (210030, 400010110)
ZONE_START = (210010, 400010107)
ZONE_START_SUMMER = (210092, 400010108)
ZONE_END = (210011, 400010111)
HAISEIKO_START = (210031, 400010109)
POPULARITY = {1: ((210063, 400010115), (210064, 400010116), 3),
              2: ((210065, 400010117), (210066, 400010118), 6),
              3: ((210067, 400010119), (210068, 400010120), 3),
              4: ((210069, 400010121), (210070, 400010122), 6)}
GOAL_WIN, GOAL_LOSS = (210059, 400010200), (210060, 400010201)
LINK_RACE = (210047, 400010018)
LINK_SKILLS = {1: (1115, 202901, 202902), 2: (1086, 202501, 202502), 3: (1116, 201103, 201102),
               4: (1070, 202471, 202472), 5: (1046, 203101, 203103), 7: (0, 202801, 202802)}
CARD_TRAINING = (21151, 21159, 830241003)
CARD_TRAINING_CHANCE = 0.25
CARD_NEW_YEAR = (21157, 830241032)
CARD_DATE_EVENTS = {21149: "first", 21152: "date", 21153: "date", 21154: "date", 21156: "date"}
CARD_DATE4 = 21155
CARD_ENDING_PAY = {True: (14, 14), False: (8, 0)}
DIRECTOR_APPRAISAL = [(90, 210101, 5), (70, 210100, 4), (40, 210099, 3), (0, 210098, 2)]
LEVEL_UP_EVENTS = {101: 210012, 102: 210013, 103: 210014, 105: 210015, 106: 210016}

SCREEN_EVENTS = frozenset(e for e, _s in ANNOUNCE.values()) | {BOSS_ANNOUNCE[0]}
PRIO_GAUGE = CE.PRIO_TRAILING + 20
PRIO_LOTTERY = CE.PRIO_TRAILING + 25
PRIO_POP = CE.PRIO_TRAILING + 30
PRIO_ANNOUNCE = CE.PRIO_TRAILING + 50
PRIO_ARRIVAL = CE.PRIO_TRAILING + 70


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False, once=None,
         priority=CE.PRIO_SCENARIO, resolver=None, payload=None, show_clear=0, sort_id=0,
         support_card_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("legend: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        support_card_id=support_card_id,
        once_key=once or f"legend:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="legend", show_clear=show_clear, show_clear_sort_id=sort_id)


def _n(choices: int) -> list:
    return [] if not choices else _ack(choices)


def opening_events() -> list:
    out = []
    for i, (eid, sid) in enumerate(OPENING):
        out.append(beat(eid, sid, choices=[] if sid == 400010002 else None,
                        priority=CE.PRIO_FRONT + i, once=f"legend:open:{eid}"))
    return [e for e in out if e]


def gauge_event(legend: int, turn: int, summer: bool, tag: str) -> CE.Event | None:
    eid, sid = GAUGE_UP_SUMMER if summer else GAUGE_UP
    return beat(eid, sid, from_endpoint=True, priority=PRIO_GAUGE, resolver="legend_gauge",
                once=f"legend:gauge:{tag}", payload={"legend": int(legend), "turn": int(turn)})


def lottery_event(full_state: dict, turn: int) -> CE.Event | None:
    impl.roll_offers(full_state, turn)
    return beat(*LOTTERY, from_endpoint=True, priority=PRIO_LOTTERY, resolver="legend_lottery",
                once=f"legend:lottery:{turn}", payload={"turn": int(turn)})


def announce_event(full_state: dict, turn: int) -> CE.Event | None:
    eid, sid = ANNOUNCE[turn]
    return beat(eid, sid, from_endpoint=True, priority=PRIO_ANNOUNCE, resolver="legend_announce",
                payload={"turn": turn}, once=f"legend:announce:{turn}")


def result_event(full_state: dict, chara_info: dict, turn: int, won: bool) -> CE.Event | None:
    st = impl.state(full_state)
    group = impl.sprace_group(turn)
    eid, sid = BOSS_RESULT if (turn == impl.FINAL_TURN and st.get("boss")) else RESULT[turn]
    effects = impl.reward_effects(full_state, chara_info, group, won)
    return beat(eid, sid, timing=RESULT_TIMING[turn], priority=CE.PRIO_FRONT, resolver="legend_result",
                choices=[CE.Choice(effects=effects)], once=f"legend:result:{turn}",
                payload={"turn": turn, "won": won})


def popularity_open(ptype: int, played: int) -> CE.Event | None:
    (a_eid, a_sid), _b, timing = POPULARITY[ptype]
    prio = CE.PRIO_FRONT + 1 if ptype in (1, 3) else PRIO_POP
    return beat(a_eid, a_sid, timing=timing, priority=prio, resolver="legend_pop_open",
                once=f"legend:pop:{ptype}", payload={"type": ptype})


def popularity_close(ptype: int) -> CE.Event | None:
    _a, (b_eid, b_sid), timing = POPULARITY[ptype]
    stats, sp = impl.POPULARITY_REWARD[ptype]
    effects = [dict(e) for e in stats] + [{"type": "skill_points", "value": f"+{sp}"}]
    return beat(b_eid, b_sid, timing=timing, priority=CE.PRIO_FRONT, resolver="legend_pop_close",
                choices=[CE.Choice(effects=effects)], once=f"legend:pop_b:{ptype}",
                payload={"type": ptype})


def goal_event(full_state: dict, chara_info: dict, goal: dict) -> CE.Event | None:
    from ...handlers import single_mode_team as smt
    rank = int(goal.get("rank") or 1)
    ev = smt._goal_race_reward(chara_info, int(goal.get("program_id") or 0), None, rank)
    effects = (ev.get("choices") or [{}])[0].get("effects") or []
    eid, sid = GOAL_WIN if rank == 1 else GOAL_LOSS
    return beat(eid, sid, timing=3, priority=CE.PRIO_FRONT, choices=[CE.Choice(effects=effects)],
                once=f"legend:goal:{goal.get('turn')}", show_clear=1,
                sort_id=int(goal.get("sort") or 1))


def card_training_event(full_state: dict, chara_info: dict, turn: int) -> CE.Event | None:
    if not impl.group_card(chara_info):
        return None
    passion = M.PASSION in set(chara_info.get("chara_effect_id_array") or ())
    choices = [
        CE.Choice(effects=[{"type": "speed", "value": "+6"}, {"type": "skill_points", "value": "+6"},
                           {"type": "bond", "char_id": M.SPEED, "value": "+7"}]),
        CE.Choice(effects=[{"type": "energy", "value": "+3"}, {"type": "skill_points", "value": "+9"}]),
        CE.Choice(effects=[{"type": "wiz", "value": "+3"}, {"type": "skill_points", "value": "+6"},
                           {"type": "bond", "char_id": M.SPEED, "value": "+5"}]),
    ]
    return beat(CARD_TRAINING[1] if passion else CARD_TRAINING[0], CARD_TRAINING[2],
                from_endpoint=True, priority=CE.PRIO_NORMAL, choices=choices,
                resolver="legend_card_training", support_card_id=M.GROUP_CARD,
                once=f"legend:card003:{turn}", payload={"turn": turn})


def card_new_year_event(full_state: dict, chara_info: dict) -> CE.Event | None:
    from ...handlers import single_mode_team as smt
    if not impl.group_card(chara_info):
        return None
    entry = smt._pal_card_entry(full_state, M.GROUP_CARD)
    if not entry.get("unlocked") or entry.get("new_year_fired"):
        return None
    effects = [{"type": "all_stats", "value": "+4"}, {"type": "skill_points", "value": "+6"},
               {"type": "max_energy", "value": "+4"},
               {"type": "skill_hint", "skill_id": 203502, "value": "+3"}]
    return beat(*CARD_NEW_YEAR, priority=PRIO_ARRIVAL + 1, choices=[CE.Choice(effects=effects)],
                resolver="legend_card_new_year", support_card_id=M.GROUP_CARD,
                once="legend:card_new_year")


def link_race_entry_and_event(chara_info: dict) -> CE.Event | None:
    if not story_playable(LINK_RACE[1]):
        return None
    choices = [{"select_index": i, "receive_item_id": 0, "target_race_id": 0,
                "gain_select_id_index": g, "select_icon": 0}
               for i, g in enumerate(sorted(LINK_SKILLS), start=1)]
    entry = {"event_id": LINK_RACE[0], "chara_id": 0, "story_id": LINK_RACE[1], "play_timing": 6,
             "event_contents_info": {"support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
                                     "choice_array": choices, "tips_training_partner_id": None},
             "succession_event_info": None, "minigame_result": None}
    return CE.Event(event_id=LINK_RACE[0], story_id=LINK_RACE[1], play_timing=6, raw=entry,
                    timing_from_endpoint=True, once_key="legend:link_race",
                    priority=CE.PRIO_TRAILING, resolver="legend_link_race", source="legend")


def link_effects(chara_info: dict, gain_index: int) -> list:
    row = LINK_SKILLS.get(int(gain_index or 0))
    if not row:
        return []
    chara, gold, white = row
    linked = (impl.group_card(chara_info) is not None) if chara == 0 else (
        chara in {impl.formula.RF.card_meta(s)[3] for s in impl.formula.RF.deck(chara_info).values()}
        or int(chara_info.get("card_id") or 0) // 100 == chara)
    return [{"type": "skill_hint", "skill_id": gold if linked else white, "value": "+1"}]


def ending_events(full_state: dict, chara_info: dict) -> list:
    from ...handlers import single_mode_team as smt
    st = impl.state(full_state)
    boss_won = st.get("boss") and any(int(r["race_type"]) == 3 and int(r["result_rank"]) == 1
                                      for r in st["races"])
    if boss_won:
        eid, sid = ENDING_BOSS
        eff = [{"type": "all_stats", "value": "+45"}, {"type": "skill_points", "value": "+245"},
               {"type": "skill_hint", "skill_id": impl.NEW_LEGEND, "value": "+3"}]
    elif impl.all_won(st):
        eid, sid = ENDING_ALL
        eff = [{"type": "all_stats", "value": "+45"}, {"type": "skill_points", "value": "+245"},
               {"type": "skill_hint", "skill_id": impl.NEW_LEGEND, "value": "+1"}]
    else:
        eid, sid = ENDING_LOST
        eff = [{"type": "all_stats", "value": "+30"}, {"type": "skill_points", "value": "+170"},
               {"type": "skill_hint", "skill_id": impl.ERA_CHANGER, "value": "+3"}]
    out = [beat(eid, sid, timing=END_TIMING, priority=CE.PRIO_FRONT, choices=[CE.Choice(effects=eff)],
                once="legend:ending")]
    if impl.group_card(chara_info):
        entry = smt._pal_card_entry(full_state, M.GROUP_CARD)
        steps = entry.get("steps") or {}
        dated = all(int(steps.get(str(g), 0)) >= 1 for g in M.LEGENDS) and \
            int(steps.get(str(M.SPEED), 0)) >= 3
        stats, sp = CARD_ENDING_PAY[dated]
        pay = [{"type": "all_stats", "value": f"+{stats}"}]
        if sp:
            pay.append({"type": "skill_points", "value": f"+{sp}"})
        out.append(beat(*CARD_ENDING[dated], timing=END_TIMING, priority=CE.PRIO_FRONT + 1,
                        support_card_id=M.GROUP_CARD, choices=[CE.Choice(effects=pay)],
                        once="legend:card_ending"))
    return [e for e in out if e]


def _turn_start(full_state: dict, chara_info: dict, turn: int) -> list:
    st = impl.state(full_state)
    out = []
    s46, s47 = st["s9046"], st["s9047"]
    if int(s46["remain"]) > 0:
        if int(s46["remain"]) <= 1:
            out.append(beat(*SUPER_END, priority=PRIO_ARRIVAL, resolver="legend_super_end",
                            once=f"legend:super_end:{turn}"))
        else:
            s46["remain"] = int(s46["remain"]) - 1
    elif s46.get("start_due") and st["masterly"] == M.ST_LITE:
        out.append(beat(*SUPER_START, priority=PRIO_ARRIVAL, resolver="legend_super_start",
                        once=f"legend:super_start:{turn}"))
    if int(s47["zone"]) > 0:
        s47["zone"] = int(s47["zone"]) + 1
    elif s47.get("start_due") and st["masterly"] == M.SPEED:
        out.append(beat(*(ZONE_START_SUMMER if turn in impl.SUMMER_TURNS else ZONE_START),
                        priority=PRIO_ARRIVAL, resolver="legend_zone_start",
                        once=f"legend:zone_start:{turn}"))
    return out


@CE.producer("legend_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def legend_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    full_state, ci = ctx.full_state, ctx.chara_info
    st = impl.state(full_state)
    out: list = []
    if int(ci.get("state") or 0) == 2:
        st["due"] = []
        return out
    if played in STORY_BEATS and arrived > played:
        eid, sid, n = STORY_BEATS[played]
        out.append(beat(eid, sid, choices=_n(n), from_endpoint=True, priority=CE.PRIO_TRAILING))
    if played == 13 and arrived > played:
        out.append(beat(*REPORTER, from_endpoint=True, priority=CE.PRIO_TRAILING + 1))
    if played == 67 and arrived > played:
        out.append(link_race_entry_and_event(ci))
    goal = st.get("goal_due") or {}
    if goal:
        st["goal_due"] = {}
        out.append(goal_event(full_state, ci, goal))
    end = st["s9047"].get("end") or {}
    if end and not end.get("queued"):
        end["queued"] = True
        stats, sp = M.zone_reward(int(end["n"]), int(end["end_type"]))
        eff = [{"type": "all_stats", "value": f"+{stats}"}]
        if sp:
            eff.append({"type": "skill_points", "value": f"+{sp}"})
        out.append(beat(*ZONE_END, timing=6, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                        choices=[CE.Choice(effects=eff)], resolver="legend_zone_end",
                        once=f"legend:zone_end:{end.get('turn', played)}:{end['n']}"))
    for due in st.get("due") or ():
        out.append(gauge_event(int(due["legend"]), int(due["turn"]), bool(due.get("summer")),
                               f"{due['turn']}:{due.get('kind')}"))
    st["due"] = []
    if int(st.get("card003_due") or 0) == played and played:
        st["card003_due"] = 0
        out.append(card_training_event(full_state, ci, played))
    for ptype in list(st.get("pop_due") or ()):
        if ptype not in st["pop_done"]:
            out.append(popularity_open(int(ptype), played))
    st["pop_due"] = []
    if played == 43 and arrived > played and 2 not in st["pop_done"]:
        out.append(popularity_open(2, played))
    if played == impl.FINAL_TURN and arrived > played and 4 not in st["pop_done"] and impl.stella_won(st):
        impl.race_legend(full_state)
        out.append(popularity_open(4, played))
    if played in M.lottery_turns() and arrived > played and not CE.already_fired(
            full_state, f"legend:lottery:{played}"):
        out.append(lottery_event(full_state, played))
    if impl.sprace_pending(st, played) and arrived > played:
        out.append(announce_event(full_state, played))
    if arrived > int(st.get("advanced_to") or 1) and arrived <= impl.FINAL_TURN:
        for t in range(int(st.get("advanced_to") or 1) + 1, arrived + 1):
            out += _turn_start(full_state, ci, t)
        st["advanced_to"] = arrived
    if arrived in ARRIVAL_BEATS and arrived > played:
        eid, sid, n = ARRIVAL_BEATS[arrived]
        resolver = "legend_camp_open" if arrived == impl.GUIDANCE_TURN else None
        out.append(beat(eid, sid, choices=_n(n), priority=PRIO_ARRIVAL - 1, resolver=resolver))
    if arrived == 25 and arrived > played:
        out.append(card_new_year_event(full_state, ci))
    return [e for e in out if e is not None]


@CE.resolver("legend_gauge")
def _gauge(full_state, chara_info, event, choice_number, **kw):
    impl.add_gauge(full_state, int((event.payload or {}).get("legend") or 0), 1)
    return {}


@CE.resolver("legend_lottery")
def _lottery(full_state, chara_info, event, choice_number, **kw):
    impl.answer_lottery(full_state, chara_info, int((event.payload or {}).get("turn") or 0))
    return {}


@CE.resolver("legend_camp_open")
def _camp_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    if st["masterly"]:
        return {}
    legend = impl.pick_guidance(full_state)
    ev = beat(*GUIDANCE[legend], priority=CE.PRIO_FRONT, resolver="legend_guidance",
              payload={"legend": legend}, once="legend:guidance")
    if ev:
        CE.emit(full_state, ev)
    return {}


@CE.resolver("legend_guidance")
def _guidance(full_state, chara_info, event, choice_number, **kw):
    legend = int((event.payload or {}).get("legend") or 0)
    impl.grant_guidance(full_state, chara_info, legend)
    impl.raise_all_facilities(full_state, chara_info)
    turn = impl.turn_of(chara_info)
    if legend == M.ST_LITE:
        nxt = beat(*SUPER_START, priority=CE.PRIO_FRONT + 1, resolver="legend_super_start",
                   once=f"legend:super_start:{turn}")
    elif legend == M.SPEED:
        nxt = beat(*(ZONE_START_SUMMER if turn in impl.SUMMER_TURNS else ZONE_START),
                   timing=11 if turn in impl.SUMMER_TURNS else 1, priority=CE.PRIO_FRONT + 1,
                   resolver="legend_zone_start", once=f"legend:zone_start:{turn}")
    else:
        nxt = beat(*HAISEIKO_START, timing=11, priority=CE.PRIO_FRONT + 1, resolver="legend_haiseiko")
    if nxt:
        CE.emit(full_state, nxt)
    return {}


@CE.resolver("legend_super_start")
def _super_start(full_state, chara_info, event, choice_number, **kw):
    impl.start_super(full_state, chara_info)
    return {}


@CE.resolver("legend_super_end")
def _super_end(full_state, chara_info, event, choice_number, **kw):
    impl.end_super(full_state)
    return {}


@CE.resolver("legend_zone_start")
def _zone_start(full_state, chara_info, event, choice_number, **kw):
    impl.start_zone(full_state)
    return {}


@CE.resolver("legend_zone_end")
def _zone_end(full_state, chara_info, event, choice_number, **kw):
    impl.state(full_state)["s9047"]["end"] = {}
    return {}


@CE.resolver("legend_haiseiko")
def _haiseiko(full_state, chara_info, event, choice_number, **kw):
    impl.recruit_guests(full_state, chara_info)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("legend_announce")
def _announce(full_state, chara_info, event, choice_number, **kw):
    turn = int((event.payload or {}).get("turn") or 0)
    impl.announce(full_state, chara_info, turn)
    chara_info["playing_state"] = 28
    return {}


@CE.resolver("legend_result")
def _result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    p = event.payload or {}
    turn, won = int(p.get("turn") or 0), bool(p.get("won"))
    st["sprace"]["stage"] = "done"
    if turn not in st["done_races"]:
        st["done_races"].append(turn)
    group = impl.sprace_group(turn)
    if won and group in (impl.STELLA, impl.PRIDE):
        impl.raise_all_facilities(full_state, chara_info)
    if turn == impl.FINAL_TURN:
        chara_info["playing_state"] = 5
        ev = beat(*MAGAZINE, timing=END_TIMING, priority=CE.PRIO_FRONT, resolver="legend_magazine",
                  choices=[CE.Choice(effects=_smt().reporter_issue_effects(chara_info))],
                  once="legend:magazine")
        if ev:
            CE.emit(full_state, ev)
        return {}
    st["hold_turn"] = 0
    chara_info["playing_state"] = 1
    return {}


def _smt():
    from ...handlers import single_mode_team as smt
    return smt

@CE.resolver("legend_magazine")
def _magazine(full_state, chara_info, event, choice_number, **kw):
    from ...handlers import single_mode_team as smt
    history = ((full_state.get(smt.STATE_KEY) or {}).get("data") or {}).get("race_history") or []
    impl.pick_cm(full_state, history)
    chara_info["playing_state"] = 32
    for ev in ending_events(full_state, chara_info):
        CE.emit(full_state, ev)
    return {}


@CE.resolver("legend_pop_open")
def _pop_open(full_state, chara_info, event, choice_number, **kw):
    ptype = int((event.payload or {}).get("type") or 0)
    st = impl.state(full_state)
    if ptype not in st["pop_done"]:
        st["pop_done"].append(ptype)
    impl.start_popularity(full_state, ptype)
    chara_info["playing_state"] = 33
    ev = popularity_close(ptype)
    if ev:
        CE.emit(full_state, ev)
    return {}


@CE.resolver("legend_pop_close")
def _pop_close(full_state, chara_info, event, choice_number, **kw):
    impl.state(full_state)["popularity"] = {}
    return {}


@CE.resolver("legend_card_training")
def _card_training(full_state, chara_info, event, choice_number, **kw):
    idx = max(1, min(3, int(choice_number or 1)))
    impl.add_gauge(full_state, M.LEGENDS[idx - 1], 1)
    return {}


@CE.resolver("legend_card_new_year")
def _card_new_year(full_state, chara_info, event, choice_number, **kw):
    from ...handlers import single_mode_team as smt
    smt._pal_card_entry(full_state, M.GROUP_CARD)["new_year_fired"] = 1
    impl.add_all_gauges(full_state, 2)
    return {}


@CE.resolver("legend_link_race")
def _link_race(full_state, chara_info, event, choice_number, **kw):
    effects = link_effects(chara_info, int(choice_number or 0))
    applied = event_engine.apply_choice(chara_info, {"choices": [{"effects": effects}]}, 1,
                                        full_state=full_state) or {}
    return {"not_up": applied.get("not_up")}
