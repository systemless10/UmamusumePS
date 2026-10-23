
from __future__ import annotations

import logging

from ... import career_events as CE
from ... import event_engine
from ...handlers.career_producers import story_playable
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

PRIO_ARRIVAL = CE.PRIO_TRAILING + 5
PRIO_CLOSE = CE.PRIO_TRAILING
PRIO_SCREEN = CE.PRIO_TRAILING + 2

CLOSING = {
    1: (206001, 400006000, 0), 14: (206004, 400006003, 0), 31: (206082, 400006009, 0),
    38: (206017, 400006014, 0)}
ROSTER = (206002, 400006001)
REPORTER = (206100, 400000100)
EYES = (206005, 400006004)
TUTORIAL = (206003, 400006002)
NOTICE = {35: (206339, 400006113), 59: (206340, 400006113)}
EXPEDITION_START = {37: (206016, 400006010), 61: (206025, 400006010)}
NEW_YEAR = {False: (206087, 400006024), True: (206088, 400006025)}
ARC_AFTERMATH = {True: (206084, 400006021), False: (206085, 400006022)}
SOON = (206086, 400006023)
SENIOR_PREVIEW = {False: (206090, 400006042, 0), True: (206095, 400006035, 1)}
GOALS = {24: (206081, 400006100, 3, 20), 54: (206089, 400006102, 7, 50)}
WITH = (206406, 400006005)
WITH_TURN = 25
DECLARE_TURN, CHALLENGE_TURN = 29, 58
SS_END = (206057, 400006112)
UNLOCK = {"event": (206344, 400006105), "other": (206121, 400006105)}
MEMBER_WIN = (206101, 400006104)
MONTHLY = (206083, 400000091)
FINALE = {"won": (206035, 400006044, 20, 60, 1), "lost": (206033, 400006045, 15, 40, 1),
          "double": (206034, 400006043, 25, 60, 3)}
TRUST_HINT = {1070: 20110, 1067: 20045, 1107: 20114, 1049: 20203}

SCREEN_EVENTS = frozenset(e for e, _s in M.EXHIBITION_OPEN.values())


def _beat(event_id, story_id, *, choices=None, n_choices=1, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("arc: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=[CE.Choice(effects=[]) for _ in range(n_choices)] if choices is None else choices,
        chara_id=0, once_key=once or f"arc:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="arc", show_clear=show_clear, show_clear_sort_id=sort_id)


def _one(effects) -> list:
    return [CE.Choice(effects=list(effects))]


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def race_bonus(full_state: dict, chara_info: dict) -> int:
    return int(_smt()._race_bonus_pct(chara_info, full_state))


def scaled(base: int, rb: int) -> int:
    return int(base) * (100 + int(rb)) // 100


@CE.producer("arc_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def arc_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    fs, ci = ctx.full_state, ctx.chara_info
    st = impl.state(fs)
    out = []
    if played in CLOSING:
        e, s, n = CLOSING[played]
        out.append(_beat(e, s, n_choices=n, timing=6, from_endpoint=True, priority=PRIO_CLOSE))
    if played == 2:
        out.append(_beat(*ROSTER, timing=6, from_endpoint=True, priority=PRIO_CLOSE, resolver="arc_roster"))
    if played == 10:
        out.append(_beat(*REPORTER, timing=6, from_endpoint=True, priority=PRIO_CLOSE))
    if played == 19:
        out.append(_beat(*EYES, timing=6, from_endpoint=True, priority=PRIO_CLOSE,
                         resolver="arc_gexp", payload={"gexp": 100}))
    if played in GOALS and int(st["tpoints"]) >= _goal_points(ci, played):
        e, s, stats, sp = GOALS[played]
        out.append(_beat(e, s, timing=6, from_endpoint=True, priority=PRIO_CLOSE, show_clear=1,
                         sort_id=impl.RI.goal_sort_id(ci, played),
                         choices=_one([{"type": "all_stats", "value": f"+{stats}"},
                                       {"type": "skill_point", "value": f"+{sp}"}])))
    if played == WITH_TURN and st["roster"]:
        ev = with_event(fs, ci)
        if ev:
            out.append(ev)
    if played == DECLARE_TURN and st["with"]:
        e = M.LINK_CHAIN.get(int(st["with"]), (0, 0, 0))[0]
        story = _story_of(e)
        if story:
            out.append(_beat(e, story, timing=6, from_endpoint=True, priority=PRIO_CLOSE,
                             choices=_one([{"type": "random_stats", "value": "+10", "stat_count": 1},
                                           {"type": "mood", "value": "+1"}])))
    if played == CHALLENGE_TURN and st["with"]:
        e = M.LINK_CHAIN.get(int(st["with"]), (0, 0, 0))[1]
        story = _story_of(e)
        if story:
            out.append(_beat(e, story, n_choices=0, timing=6, from_endpoint=True, priority=PRIO_CLOSE))
    if played == 44:
        out.append(_beat(*ARC_AFTERMATH[bool(st["classic_won"])], timing=6, from_endpoint=True,
                         priority=PRIO_CLOSE, choices=_one([{"type": "mood", "value": "-2"}])))
    if played == 45:
        out.append(_beat(*SOON, timing=6, from_endpoint=True, priority=PRIO_CLOSE,
                         choices=_one([{"type": "mood", "value": "+4"}]),
                         resolver="arc_gexp", payload={"gexp": 30}))
    if played == 64:
        e, s, n = SENIOR_PREVIEW[bool(st["classic_won"])]
        out.append(_beat(e, s, n_choices=n, timing=6, from_endpoint=True, priority=PRIO_CLOSE))
    if played in M.EXHIBITIONS and played not in [int(t) for t in st["done_exhibitions"]] \
            and int(ci.get("state") or 0) != 2:
        e, s = M.EXHIBITION_OPEN[M.EXHIBITIONS[played]]
        out.append(_beat(e, s, timing=6, from_endpoint=True, priority=PRIO_SCREEN,
                         resolver="arc_exhibition_open", payload={"turn": played}))
    if arrived == impl.ROSTER_TURN:
        out.append(_beat(*TUTORIAL, n_choices=0, priority=PRIO_ARRIVAL))
    if arrived in NOTICE:
        out.append(_beat(*NOTICE[arrived], n_choices=0, priority=PRIO_ARRIVAL))
    if arrived in EXPEDITION_START:
        out.append(_beat(*EXPEDITION_START[arrived], priority=PRIO_ARRIVAL, resolver="arc_expedition",
                         once=f"arc:exped:{arrived}"))
    if arrived == 49:
        out.append(_beat(*NEW_YEAR[bool(st["classic_won"])], priority=PRIO_ARRIVAL,
                         choices=_one([{"type": "energy", "value": "+30"}])))
    if not impl.is_expedition(arrived):
        out += threshold_events(fs, ci)
    out += due_events(fs, ci, played)
    return [e for e in out if e is not None]


def _goal_points(chara_info: dict, turn: int) -> int:
    from ... import master_data
    ids = tuple(chara_info.get("route_race_id_array") or ())
    if ids:
        marks = ",".join("?" * len(ids))
        r = master_data.query_one(
            f"SELECT condition_value_1 FROM single_mode_route_race WHERE id IN ({marks}) "
            f"AND turn=? AND condition_type=11", ids + (int(turn),))
        if r:
            return int(r["condition_value_1"])
    return 7000 if int(turn) == 24 else 42000


def _story_of(event_id: int) -> int:
    from ... import master_data
    r = master_data.query_one("SELECT story_id FROM single_mode_story_data WHERE id=?", (int(event_id),))
    return int(r["story_id"]) if r else 0


def threshold_events(full_state: dict, chara_info: dict) -> list:
    st = impl.state(full_state)
    rate = impl.approval_rate(st)
    out = []
    for need, event_id, stats, fans, facilities in impl.THRESHOLD_EVENTS:
        if rate < need or event_id in st["thresholds"]:
            continue
        st["thresholds"].append(event_id)
        out.append(_beat(event_id, 400006111, priority=PRIO_ARRIVAL,
                         resolver="arc_facilities" if facilities else None,
                         choices=_one([{"type": "all_stats", "value": f"+{stats}"},
                                       {"type": "fans", "value": f"+{fans}"}])))
    return out


def due_events(full_state: dict, chara_info: dict, played: int) -> list:
    st = impl.state(full_state)
    due, st["due"] = list(st["due"]), []
    out = []
    for d in due:
        kind = d.get("kind")
        if kind == "race_result":
            out.append(race_result_event(full_state, chara_info, d))
        elif kind == "member_win":
            out.append(_beat(*MEMBER_WIN, timing=3, priority=CE.PRIO_TRAILING,
                             once=f"arc:mwin:{d.get('turn')}", resolver="arc_member_win",
                             payload={"charas": list(d.get("charas") or ())}))
    return [e for e in out if e is not None]


def ss_end_event(due: dict) -> CE.Event | None:
    if not due:
        return None
    return _beat(*SS_END, timing=6, from_endpoint=True, priority=CE.PRIO_FRONT,
                 once=f"arc:ss:{due.get('turn')}", resolver="arc_ss_end",
                 payload={"n": int(due.get("n") or 0)})


def unlock_event(full_state: dict, chara_info: dict) -> CE.Event | None:
    pids = impl.unlockable(full_state, chara_info)
    if not pids:
        return None
    kind = "event" if set(pids) <= {3, 6} else "other"
    e, s = UNLOCK[kind]
    return _beat(e, s, timing=6, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                 once=f"arc:unlock:{'-'.join(map(str, pids))}", resolver="arc_unlock",
                 payload={"pids": pids})


def emit_unlocks(full_state: dict, chara_info: dict) -> None:
    ev = unlock_event(full_state, chara_info)
    if ev:
        CE.emit(full_state, ev)


def with_options(full_state: dict, chara_info: dict) -> list:
    st = impl.state(full_state)
    if st["with_options"]:
        return list(st["with_options"])
    trainee = int(chara_info.get("card_id") or 0) // 100
    fixed = M.WITH_FIXED.get(trainee)
    deck = sorted({M.WITH_INDEX[c] for c in impl.deck_all_charas(chara_info)
                   if c in M.WITH_INDEX and c != trainee and c != fixed})
    taken = set(deck) | ({M.WITH_INDEX[fixed]} if fixed else set())
    pool = [i for c, i in M.WITH_INDEX.items() if i not in taken and c != trainee]
    rng = impl.rng_for(full_state, "with")
    rng.shuffle(pool)
    head = [M.WITH_INDEX[fixed]] if fixed else []
    room = 5 - len(head) - len(deck)
    opts = head + deck[:5 - len(head)] + sorted(pool[:max(0, room)])
    st["with_options"] = opts[:5]
    return list(st["with_options"])


def with_event(full_state: dict, chara_info: dict) -> CE.Event | None:
    if not story_playable(WITH[1]):
        return None
    opts = with_options(full_state, chara_info)
    entry = {"event_id": WITH[0], "chara_id": 0, "story_id": WITH[1], "play_timing": 6,
             "event_contents_info": {
                 "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
                 "choice_array": [{"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                                   "gain_select_id_index": i, "select_icon": 0} for i in opts],
                 "tips_training_partner_id": None},
             "succession_event_info": None, "minigame_result": None}
    return CE.Event(event_id=WITH[0], story_id=WITH[1], play_timing=6, timing_from_endpoint=True,
                    raw=entry, once_key="arc:with", priority=PRIO_CLOSE, resolver="arc_with",
                    source="arc", payload={"options": opts})


def goal_race_entry_events(full_state: dict, chara_info: dict, program_id: int) -> list | None:
    program_id = int(program_id or 0)
    st = impl.state(full_state)
    pres = []
    if program_id == M.ARC_SENIOR and st["classic_won"]:
        pres.append(M.PRE_ARC_AFTER_WIN)
    else:
        if program_id == M.ARC_SENIOR:
            link = M.PRE_ARC_LINK.get(int(chara_info.get("card_id") or 0) // 100)
            if link:
                pres.append(link)
        if M.PRE_RACE.get(program_id):
            pres.append(M.PRE_RACE[program_id])
    if not pres:
        return None
    evs = [_beat(*pre, n_choices=0, timing=2, priority=CE.PRIO_FRONT) for pre in pres]
    return [ev.wire_entry() for ev in evs if ev]


def race_result_event(full_state: dict, chara_info: dict, d: dict) -> CE.Event | None:
    st = impl.state(full_state)
    program = int(d.get("program_id") or 0)
    won = int(d.get("rank") or 99) == 1
    rb = race_bonus(full_state, chara_info)
    timing = 14 if program in M.EXHIBITION_NUM else 3
    once = f"arc:result:{d.get('turn')}:{program}"
    if program == M.ARC_SENIOR:
        if won and st["classic_won"]:
            row = (M.ARC_SENIOR_WON_DOUBLE if int(st["pot"].get("10", 0)) >= 2
                   else M.ARC_SENIOR_WON_DOUBLE_NO_LV2)
        elif won:
            row = M.ARC_SENIOR_WON
        elif st["classic_won"]:
            row = M.ARC_SENIOR_LOST_AFTER_WIN
        else:
            row = M.ARC_SENIOR_LOST
        e, s, stats, sp = row
        return _beat(e, s, timing=3, priority=CE.PRIO_TRAILING, once=once, resolver="arc_senior_result",
                     payload={"rank": int(d.get("rank") or 99)},
                     choices=_one([{"type": "all_stats", "value": f"+{scaled(stats, rb)}"},
                                   {"type": "skill_point", "value": f"+{scaled(sp, rb)}"}]))
    table = M.RACE_RESULT.get(program)
    row = (table or {}).get(won) or (table or {}).get(True)
    if not row:
        return None
    e, s, stats, sp, gexp = row
    effects = []
    if stats:
        effects.append({"type": "all_stats", "value": f"+{scaled(stats, rb)}"})
    effects.append({"type": "skill_point", "value": f"+{scaled(sp, rb)}"})
    if program == M.FOY and won:
        effects.append({"type": "skill_hint", "skill_id": M.SKILL_ARC_WHITE, "value": "+1"})
    if program in M.EXHIBITION_NUM and won and M.EXHIBITION_NUM[program] >= 2:
        effects.append({"type": "unique_skill_level", "value": "+1"})
    return _beat(e, s, timing=timing, priority=CE.PRIO_TRAILING, once=once,
                 resolver="arc_race_result", choices=_one(effects),
                 payload={"gexp": gexp, "program_id": program, "turn": int(d.get("turn") or 0),
                          "won": won})


@CE.resolver("arc_roster")
def _roster(full_state, chara_info, event, choice_number, **kw):
    impl.start_roster_turn(full_state, chara_info)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("arc_gexp")
def _gexp(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["gexp"] = int(st["gexp"]) + int((event.payload or {}).get("gexp") or 0)
    return {}


@CE.resolver("arc_facilities")
def _facilities(full_state, chara_info, event, choice_number, **kw):
    impl.RI.raise_all_facilities(full_state, chara_info)
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("arc_expedition")
def _expedition(full_state, chara_info, event, choice_number, **kw):
    impl.state(full_state)["expedition_started"] = True
    emit_unlocks(full_state, chara_info)
    return {}


@CE.resolver("arc_unlock")
def _unlock(full_state, chara_info, event, choice_number, **kw):
    impl.unlock(full_state, chara_info, (event.payload or {}).get("pids") or ())
    return {}


@CE.resolver("arc_ss_end")
def _ss_end(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["gexp"] = int(st["gexp"]) + 10 * int((event.payload or {}).get("n") or 0)
    emit_unlocks(full_state, chara_info)
    return {}


@CE.resolver("arc_member_win")
def _member_win(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    impl.flag(st, "not_up_boost", impl.raise_gauges(st, (event.payload or {}).get("charas") or ()))
    return {}


@CE.resolver("arc_with")
def _with(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    opts = list((event.payload or {}).get("options") or ())
    pick = int(choice_number or 0)
    if pick not in opts:
        pick = opts[max(0, min(len(opts) - 1, pick - 1))] if opts else 0
    st["with"] = M.WITH_CHARA.get(pick, 0)
    log.info("arc: With -> %s (option %s of %s)", st["with"], pick, opts)
    return {}


@CE.resolver("arc_exhibition_open")
def _exhibition_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    turn = int((event.payload or {}).get("turn") or 0)
    st["hold_turn"] = turn
    st["sprace"] = {"turn": turn, "program_id": M.EXHIBITIONS[turn], "stage": "top"}
    chara_info["playing_state"] = 16
    return {}


@CE.resolver("arc_race_result")
def _race_result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    p = event.payload or {}
    st["gexp"] = int(st["gexp"]) + int(p.get("gexp") or 0)
    program = int(p.get("program_id") or 0)
    if program in M.EXHIBITION_NUM:
        turn = int(p.get("turn") or 0)
        if turn not in [int(t) for t in st["done_exhibitions"]]:
            st["done_exhibitions"].append(turn)
        st["hold_turn"] = 0
        st["sprace"]["stage"] = "done"
        chara_info["playing_state"] = 1
        if program == 2904 and st["with"]:
            ev = trust_event(full_state, chara_info, turn)
            if ev:
                CE.emit(full_state, ev)
    emit_unlocks(full_state, chara_info)
    return {}


def trust_event(full_state: dict, chara_info: dict, turn: int) -> CE.Event | None:
    st = impl.state(full_state)
    x = int(st["with"])
    e = M.LINK_CHAIN.get(x, (0, 0, 0))[2]
    story = _story_of(e)
    if not story:
        return None
    effects = [{"type": "all_stats", "value": "+10"}, {"type": "mood", "value": "+1"}]
    group = TRUST_HINT.get(x)
    if group:
        trainee = int(chara_info.get("card_id") or 0) // 100
        linked = x in impl.deck_all_charas(chara_info) or M.WITH_FIXED.get(trainee) == x
        effects.append({"type": "skill_hint", "skill_id": group * 10 + (1 if linked else 2), "value": "+1"})
    return _beat(e, story, timing=14, priority=CE.PRIO_TRAILING, once=f"arc:trust:{turn}",
                 resolver="arc_trust", choices=_one(effects))


@CE.resolver("arc_trust")
def _trust(full_state, chara_info, event, choice_number, **kw):
    impl.state(full_state)["trust_seen"] = True
    emit_unlocks(full_state, chara_info)
    return {}


@CE.resolver("arc_senior_result")
def _senior_result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    rank = int((event.payload or {}).get("rank") or 99)
    ev = _beat(*MONTHLY, timing=3, priority=CE.PRIO_TRAILING, resolver="arc_monthly",
               payload={"rank": rank},
               choices=_one(_smt().reporter_issue_effects(chara_info)))
    if ev:
        CE.emit(full_state, ev)
    st["senior_won"] = rank == 1
    return {}


@CE.resolver("arc_monthly")
def _monthly(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    key = ("double" if st["senior_won"] and st["classic_won"] else
           "won" if st["senior_won"] else "lost")
    for fe in _smt().friend_ending_ce_events(full_state, chara_info, CE.PRIO_TRAILING, "arc"):
        CE.emit(full_state, fe)
    e, s, stats, sp, hint = FINALE[key]
    skill = M.SKILL_ARC_GOLD if key != "lost" else M.SKILL_ARC_WHITE
    ev = _beat(e, s, timing=3, priority=CE.PRIO_TRAILING,
               choices=_one([{"type": "all_stats", "value": f"+{stats}"},
                             {"type": "skill_point", "value": f"+{sp}"},
                             {"type": "skill_hint", "skill_id": skill, "value": f"+{hint}"}]))
    if ev:
        CE.emit(full_state, ev)
    return {}


def choice_reward_preview(full_state: dict, chara_info: dict, event_id):
    ev = CE.find(full_state, event_id)
    if ev is None or ev.source != "arc":
        return None
    if ev.raw is not None:
        opts = (ev.payload or {}).get("options") or []
        return [{"gain_param_array": [], "select_index": i} for i in opts]
    if not ev.choices:
        return None
    return event_engine.choice_reward_array(ev.engine_event(), chara_info or {})
