
from __future__ import annotations

import logging

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

INVITE = (213009, 400013000)
FORMED = (213001, 400013001)
TUTORIAL = (213031, 400013002)
SEASON_START = (213002, 400013003)
REPORTER = (213816, 400000100)
PLUS_ONE = (213807, 400013114)
SUMMER_PLUS_ONE = (213808, 400013116)
STORY_BEATS = {5: (213022, 400013004), 21: (213023, 400013008), 33: (213024, 400013012),
               45: (213026, 400013017), 54: (213027, 400013021), 65: (213021, 400013026)}
ARRIVAL_STORIES = {37: (213025, 400013016), 61: (213028, 400013025)}
LINK_BEAT = (213029, 400013027)
BC_DAY = (213020, 400013028)
BC_WIN = (213903, 400013029)
ENDING = (213905, 400013030)
GOAL_REWARD_EVENTS = {1: 213900, 2: 213901, 3: 213902, 4: 213904, 5: 213907}
GOAL_REWARD_STORY = 400013104
CASINO_ENDING = {True: (40, 801139022), False: (41, 801139023)}
CASINO_OUTINGS = frozenset({21229, 21230, 21231, 21232, 21233})
SKILL_BEYOND = 210381
PRIO_ARRIVAL = CE.PRIO_TRAILING + 5

LINK_HINTS = ((1081, 202952, 202951), (1132, 204112, 204111), (1136, 202092, 202091),
              (1138, 203502, 203501), (1140, 203102, 203101), (1139, 201661, 201662))

GOAL_BASE = {1: (9, 107, 137), 2: (6, 90, 90), 3: (3, 90, 90), 4: (3, 75, 75), 5: (3, 75, 75)}
BC_WIN_BASE = (41, 153)
GOAL_FANS = {1: 2000, 2: 14000, 3: 40000, 4: 60000, 5: 30000}

SCREEN_EVENTS = frozenset({INVITE[0], SEASON_START[0],
                           *(e for e, _s in impl.REVIEW_PRE.values()),
                           *(e for e, _s in impl.REVIEW_EXCELLENT.values()),
                           *(e for e, _s in impl.REVIEW_GOOD.values()),
                           *(e for e, _s in impl.UNION_OPEN.values())})


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def _beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("breeders: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        once_key=once or f"breeders:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="breeders", show_clear=show_clear,
        show_clear_sort_id=sort_id)


def invite_event() -> CE.Event | None:
    return _beat(*INVITE, priority=CE.PRIO_FRONT, resolver="breeders_invite")


def formed_event() -> CE.Event | None:
    return _beat(*FORMED, priority=CE.PRIO_FRONT, resolver="breeders_formed")


def link_choices(chara_info: dict) -> list:
    linked = impl.linked_charas(chara_info)
    return [CE.Choice(effects=[{"type": "skill_hint", "skill_id": gold if chara in linked else normal,
                                "value": "+1"}])
            for chara, normal, gold in LINK_HINTS]


def _stat_effects(pay: dict) -> list:
    out = []
    if pay.get("all"):
        out.append({"type": "all_stats", "value": f"+{pay['all']}"})
    for stat in ("speed", "stamina", "power", "guts", "wiz"):
        if pay.get(stat):
            out.append({"type": stat, "value": f"+{pay[stat]}"})
    if pay.get("sp"):
        out.append({"type": "skill_points", "value": f"+{pay['sp']}"})
    return out


def goal_reward_event(full_state: dict, chara_info: dict, sort_id: int, rank: int, turn: int):
    from ...handlers import single_mode_team as smt
    event_id = GOAL_REWARD_EVENTS.get(sort_id)
    if not event_id:
        return None
    base_stat, sp_early, sp_late = GOAL_BASE.get(min(5, max(1, int(rank or 1))), GOAL_BASE[5])
    stat = smt._scale_race_reward(base_stat, chara_info, full_state)
    sp = smt._scale_race_reward(sp_early if sort_id <= 2 else sp_late, chara_info, full_state)
    effects = ([{"type": "random_stats", "value": f"+{stat}", "stat_count": 4}] if sort_id <= 2
               else [{"type": "all_stats", "value": f"+{stat}"}])
    effects.append({"type": "skill_points", "value": f"+{sp}"})
    if GOAL_FANS.get(sort_id):
        effects.append({"type": "fans", "value": f"+{GOAL_FANS[sort_id]}"})
    return _beat(event_id, GOAL_REWARD_STORY, timing=3, priority=CE.PRIO_FRONT,
                 choices=[CE.Choice(effects=effects)], once=f"breeders:goal:{sort_id}",
                 show_clear=1, sort_id=sort_id, payload={"turn": int(turn)})


def bc_win_events(full_state: dict, chara_info: dict) -> list:
    from ...handlers import single_mode_team as smt
    st = impl.state(full_state)
    skill = M.bc_programs().get(int(st["bc_program"]), (0, 0, 0))[2]
    stat = smt._scale_race_reward(BC_WIN_BASE[0], chara_info, full_state)
    sp = smt._scale_race_reward(BC_WIN_BASE[1], chara_info, full_state)
    win = [{"type": "all_stats", "value": f"+{stat}"}, {"type": "skill_points", "value": f"+{sp}"},
           {"type": "unique_skill_level", "value": "+1"}]
    if skill:
        win.append({"type": "skill_hint", "skill_id": skill, "value": "+3"})
    out = [_beat(*BC_WIN, timing=3, priority=CE.PRIO_FRONT, choices=[CE.Choice(effects=win)],
                 show_clear=1, sort_id=6)]
    k = impl.casino_card(chara_info)
    if k:
        done = int(st.get("outings") or 0) >= 5
        stats, sp_c = (13, 58) if done else (10, 45)
        ev = _beat(*CASINO_ENDING[done], timing=3, priority=CE.PRIO_FRONT + 1,
                   choices=[CE.Choice(effects=[{"type": "all_stats", "value": f"+{stats}"},
                                               {"type": "skill_points", "value": f"+{sp_c}"}])])
        if ev:
            ev.support_card_id = k[1]
            out.append(ev)
    end = [{"type": "all_stats", "value": "+40"}, {"type": "skill_points", "value": "+100"},
           {"type": "skill_hint", "skill_id": SKILL_BEYOND, "value": "+1"}]
    if skill:
        end.insert(2, {"type": "skill_hint", "skill_id": skill, "value": "+2"})
    out.append(_beat(*ENDING, timing=3, priority=CE.PRIO_FRONT + 2, choices=[CE.Choice(effects=end)]))
    return [e for e in out if e is not None]


def _blocked(turn: int) -> bool:
    return turn in impl.SUMMER_TURNS or turn in M.review_turns() or turn >= impl.FINAL_TURN


@CE.producer("breeders_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def breeders_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    if arrived == M.MEETING_TURN:
        out.append(_beat(*SEASON_START, resolver="breeders_season_start", priority=PRIO_ARRIVAL))
    if arrived in ARRIVAL_STORIES:
        out.append(_beat(*ARRIVAL_STORIES[arrived], choices=[], priority=PRIO_ARRIVAL))
    if arrived in impl.SUMMER_TURNS and st["formed"]:
        out.append(_beat(*SUMMER_PLUS_ONE, resolver="breeders_plus_one", priority=PRIO_ARRIVAL + 1,
                         once=f"breeders:summer:{arrived}", payload={"tag": f"summer{arrived}"}))
    if arrived and st["formed"] and not _blocked(arrived):
        rank = impl.team_rank(st)
        for n in (1, 2, 3):
            if n in st["union_done"] or st["union_event"]:
                continue
            if n == 2 and arrived != impl.UNION_TURN[2]:
                if arrived > impl.UNION_TURN[2]:
                    st["union_done"].append(2)
                continue
            if n == 3 and arrived < impl.UNION3_FROM:
                continue
            if rank >= impl.UNION_NEED[n]:
                out.append(_beat(*impl.UNION_OPEN[n], resolver="breeders_union_open",
                                 priority=PRIO_ARRIVAL + 2, payload={"union": n}))
                break
        pair = impl.BUILDING.get(min((int(m["chara_id"]) for m in st["members"]), default=0))
        for i, need in enumerate(impl.BUILDING_NEED):
            if pair and i not in st["building_done"] and rank >= need:
                if i == 1 and 0 not in st["building_done"]:
                    break
                ev_id, story = pair[i]
                out.append(_beat(ev_id, story, resolver="breeders_building",
                                 priority=PRIO_ARRIVAL + 3, payload={"index": i},
                                 choices=[CE.Choice(effects=_stat_effects(impl.BUILDING_PAY[i]))]))
                break
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], choices=[], timing=6, from_endpoint=True,
                         priority=CE.PRIO_TRAILING))
    if played == 13:
        out.append(_beat(*REPORTER, timing=6, from_endpoint=True, priority=CE.PRIO_UNLOCK))
    if played == 66:
        out.append(_beat(*LINK_BEAT, timing=6, from_endpoint=True, priority=CE.PRIO_TRAILING,
                         choices=link_choices(ctx.chara_info)))
    goal = st.get("goal_due") or {}
    if goal:
        st["goal_due"] = {}
        if goal.get("bc"):
            out += bc_win_events(ctx.full_state, ctx.chara_info)
        else:
            out.append(goal_reward_event(ctx.full_state, ctx.chara_info, int(goal["sort"]),
                                         int(goal["rank"]), int(goal.get("turn") or played)))
    for due in st.get("plus1_due") or ():
        timing = 3 if due.get("race") else 6
        out.append(_beat(*PLUS_ONE, timing=timing, from_endpoint=not due.get("race"),
                         priority=CE.PRIO_SPECIAL, resolver="breeders_plus_one",
                         once=f"breeders:plus1:{due.get('turn')}:{due.get('kind')}",
                         payload={"tag": f"{due.get('turn')}:{due.get('kind')}"}))
    st["plus1_due"] = []
    if played and impl.review_pending(st, played):
        sid = M.review_turns()[played]
        out.append(_beat(*impl.REVIEW_PRE[sid], from_endpoint=True, priority=CE.PRIO_TRAILING + 1,
                         resolver="breeders_review_open", payload={"turn": played},
                         once=f"breeders:review_pre:{sid}"))
    return [e for e in out if e is not None]


def _ps(chara_info: dict, value: int) -> None:
    chara_info["playing_state"] = value


@CE.resolver("breeders_invite")
def _invite(full_state, chara_info, event, choice_number, **kw):
    _ps(chara_info, 41)
    return {}


@CE.resolver("breeders_formed")
def _formed(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    impl.form_team(full_state, chara_info)
    k = impl.casino_card(chara_info)
    if k:
        for row in chara_info.get("evaluation_info_array") or ():
            if isinstance(row, dict) and row.get("target_id") == k[0]:
                row["is_appear"] = 1
    else:
        from ...handlers import single_mode_events as sme
        unlocked = full_state.setdefault(sme.UNLOCKED_NPCS_KEY, [])
        if [M.ADVISOR_TARGET, M.CASINO_CHARA] not in [list(x) for x in unlocked]:
            unlocked.append([M.ADVISOR_TARGET, M.CASINO_CHARA])
        arr = chara_info.setdefault("evaluation_info_array", [])
        if not any(isinstance(e, dict) and e.get("target_id") == M.ADVISOR_TARGET for e in arr):
            arr.append({"target_id": M.ADVISOR_TARGET, "training_partner_id": M.ADVISOR_TARGET,
                        "evaluation": 0, "is_outing": 0, "story_step": 0, "is_appear": 1,
                        "group_outing_info_array": []})
    st["limit"] = M.level_limit_at(1)
    CE.emit(full_state, _beat(*TUTORIAL, choices=[], priority=CE.PRIO_FRONT))
    event_id, story = impl.formation_story(st)
    CE.emit(full_state, _beat(event_id, story, choices=[], timing=11, priority=CE.PRIO_FRONT + 1))
    return {}


@CE.resolver("breeders_season_start")
def _season_start(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["deck_id"] = impl.deck_id_of(chara_info)
    st["dp"] = int(st["dp"]) + impl.START_DP.get(st["deck_id"], 5)
    st["stock"] = int(st["stock_max"])
    st["limit"] = M.level_limit_at(M.MEETING_TURN)
    _ps(chara_info, 40)
    return {}


@CE.resolver("breeders_plus_one")
def _plus_one(full_state, chara_info, event, choice_number, **kw):
    impl.plus_one(full_state, (event.payload or {}).get("tag") or event.event_id)
    return {}


@CE.resolver("breeders_review_open")
def _review_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["hold_turn"] = int((event.payload or {}).get("turn") or 0) or st["hold_turn"]
    _ps(chara_info, 43)
    return {}


@CE.resolver("breeders_review_result")
def _review_result(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    impl.review_payout(full_state, chara_info, int(payload.get("schedule_id") or 0),
                       bool(payload.get("excellent")))
    _ps(chara_info, 40)
    return {}


@CE.resolver("breeders_union_open")
def _union_open(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    st["union_event"] = int((event.payload or {}).get("union") or 0)
    _ps(chara_info, 47)
    return {}


@CE.resolver("breeders_building")
def _building(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    i = int((event.payload or {}).get("index") or 0)
    if i not in st["building_done"]:
        st["building_done"].append(i)
    st["progress"] = 1
    return {}


def union_reward_event(n: int) -> CE.Event | None:
    event_id, story = impl.UNION_REWARD[n]
    return _beat(event_id, story, priority=CE.PRIO_FRONT, once=f"breeders:union_reward:{n}",
                 choices=[CE.Choice(effects=_stat_effects(impl.UNION_PAY[n]))])


def bc_day_event() -> CE.Event | None:
    return _beat(*BC_DAY, choices=[], timing=23, priority=CE.PRIO_FRONT)
