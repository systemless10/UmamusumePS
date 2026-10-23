
from __future__ import annotations

import logging

from ... import career_events as CE
from ... import event_engine, master_data
from ...handlers.career_producers import story_playable
from . import impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = (205001, 400005001)
WORLD = (205002, 400005002)
TUTORIAL = (205028, 400005000)
REPORTER = (205034, 400000100)
STORY_BEATS = {13: (205003, 400005003), 16: (205004, 400005004), 18: (205005, 400005005),
               20: (205006, 400005006), 29: (205010, 400005010), 35: (205011, 400005011),
               40: (205014, 400005013), 43: (205029, 400005014), 49: (205018, 400005018),
               56: (205019, 400005019), 60: (205030, 400005020), 65: (205031, 400005021),
               67: (205032, 400005022)}
ARRIVAL_BEATS = {37: (205013, 400005012), 73: (205033, 400005026)}
ANNOUNCE = {24: (205007, 400005007), 48: (205015, 400005015), 72: (205020, 400005023)}
RESULT = {24: ((205008, 400005008), (205009, 400005009)),
          48: ((205016, 400005016), (205017, 400005017)),
          72: ((205021, 400005024), (205022, 400005025))}
RESULT_TIMING = 13
RESULT_SHOW_CLEAR = 6
KNOWLEDGE = {M.DARLEY: (205106, 400005101), M.GODOLPHIN: (205107, 400005102),
             M.BYERLEY: (205108, 400005103)}
FIRST_WISDOM = (205109, 400005104)
LEVEL_UP = (205110, 205111, 400005100)
GODOLPHIN = (205112, 400005106)
LEGACY = (205023, 400005105)
FINAL_ENTRY = (205024, 400005027)
FINAL_WIN = (205025, 400005028)
FINAL_WIN_AFTER = (205026, 400005029)
FINAL_LOSS_AFTER = (205027, 400005030)
MAGAZINE = (2, 400000091)
CARD_ENDING = {True: (24, 830137030), False: (23, 830137031)}
CARD_TRAINING = (21074, 21084, 830137003)
CARD_TRAINING_CHANCE = 0.3
TRAINEE_ENDING_WIN, TRAINEE_ENDING_LOSS = 6, 5

LEGACY_SKILLS = {M.DARLEY: (210261, 210262), M.GODOLPHIN: (210271, 210272),
                 M.BYERLEY: (210281, 210282)}
GOOD_GROUND = 200152
FINAL_WIN_BASE = (20, 80)
FINAL_AFTER_WIN = (20, 60)
FINAL_AFTER_LOSS = (15, 40)
CARD_ENDING_PAY = {True: (12, 12), False: (8, 0)}

SCREEN_EVENTS = frozenset(e for e, _s in ANNOUNCE.values())
PRIO_LAST = CE.PRIO_TRAILING + 40
PRIO_ARRIVAL = PRIO_LAST + 20


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False,
         once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
         show_clear=0, sort_id=0, hold=None, support_card_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("venus: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        support_card_id=support_card_id,
        once_key=once or f"venus:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="venus", show_clear=show_clear,
        show_clear_sort_id=sort_id, hold_until=hold)


def knowledge_event(goddess: int, sid: int, count: int, tag: str) -> CE.Event | None:
    event_id, story = KNOWLEDGE[goddess]
    return beat(event_id, story, from_endpoint=True, priority=CE.PRIO_TRAILING,
                resolver="venus_knowledge", once=f"venus:knowledge:{tag}",
                payload={"spirit_id": int(sid), "count": int(count)})


def levelup_events(full_state: dict, turn: int, hold: str | None = None) -> list:
    out = []
    event_id = LEVEL_UP[0] if int(turn) < impl.LATE_LEVELUP_TURN else LEVEL_UP[1]
    for cmd, lv in impl.owed_levels(full_state):
        ev = beat(event_id, LEVEL_UP[2], priority=PRIO_LAST, resolver="venus_levelup",
                  once=f"venus:levelup:{cmd}:{lv}", payload={"command_id": cmd, "level": lv},
                  hold=hold)
        if ev:
            out.append(ev)
    return out


def emit_levelups(full_state: dict, turn: int, hold: str | None = None) -> None:
    for ev in levelup_events(full_state, turn, hold):
        CE.emit(full_state, ev)


def first_wisdom_event() -> CE.Event | None:
    return beat(*FIRST_WISDOM, choices=[], priority=PRIO_LAST - 1, once="venus:first_wisdom")


def announce_event(turn: int) -> CE.Event | None:
    event_id, story = ANNOUNCE[turn]
    return beat(event_id, story, from_endpoint=True, priority=PRIO_LAST + 10,
                resolver="venus_announce", payload={"turn": turn},
                once=f"venus:announce:{turn}")


def result_event(full_state: dict, chara_info: dict, turn: int, won: bool,
                 running_style: int) -> CE.Event | None:
    group = impl.sprace_group(turn)
    (win_id, win_story), (loss_id, loss_story) = RESULT[turn]
    effects = impl.reward_effects(full_state, chara_info, group, won, running_style, turn)
    return beat(win_id if won else loss_id, win_story if won else loss_story,
                timing=RESULT_TIMING, priority=CE.PRIO_FRONT, resolver="venus_result",
                choices=[CE.Choice(effects=effects)], show_clear=RESULT_SHOW_CLEAR, sort_id=1,
                once=f"venus:result:{turn}", payload={"turn": turn, "won": won})


def godolphin_event(full_state: dict, chara_info: dict, command_id: int, partners,
                    turn: int) -> CE.Event | None:
    from ..ramen import formula as RF
    rng = impl.rng_for(full_state, "godolphin", turn)
    by_pos = RF.deck(chara_info)
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or ()}
    effects: list = []
    cards = 0
    for pos in partners or ():
        sid = by_pos.get(pos)
        if not sid or not RF.is_normal_card(sid):
            continue
        groups = event_engine.card_hint_groups(sid)
        if not groups:
            continue
        cards += 1
        stats = [g for g in groups if g[0] == "stat"]
        skills = [g[1] for g in groups if g[0] == "skill"
                  and event_engine.card_hint_open(chara_info, g[1], learned)]
        if stats:
            effects += [dict(e) for e in stats[0][1]]
            if rng.random() < 0.3:
                effects += [dict(e) for e in stats[0][1]]
        effects.append({"type": "skill_points", "value": "+20"})
        chara = master_data.support_card_chara(sid)
        if chara:
            effects.append({"type": "bond", "char_id": chara, "value": "+5"})
        if skills:
            effects.append({"type": "skill_hint", "skill_id": rng.choice(skills),
                            "value": f"+{rng.randint(2, 5)}"})
    if not cards:
        return None
    return beat(*GODOLPHIN, from_endpoint=True, priority=CE.PRIO_SPECIAL,
                choices=[CE.Choice(effects=_merge(effects))], once=f"venus:godolphin:{turn}")


def _merge(effects: list) -> list:
    out: list = []
    index: dict = {}
    for e in effects:
        t = e.get("type")
        if t in ("speed", "stamina", "power", "guts", "wiz", "wisdom", "skill_points", "skill_point"):
            key = "skill_points" if t in ("skill_points", "skill_point") else t
            v = int(str(e.get("value") or "0").replace("+", ""))
            if key in index:
                cur = int(str(out[index[key]]["value"]).replace("+", ""))
                out[index[key]]["value"] = f"+{cur + v}"
            else:
                index[key] = len(out)
                out.append({"type": key, "value": f"+{v}"})
        else:
            out.append(dict(e))
    return out


def card_training_event(full_state: dict, chara_info: dict, turn: int) -> CE.Event | None:
    k = impl.link_card(chara_info)
    if not k:
        return None
    from ...handlers import single_mode_team as smt
    entry = smt._pal_card_entry(full_state, M.LINK_CARD)
    met = bool(entry.get("unlocked"))
    passion = M.PURE_PASSION in set(chara_info.get("chara_effect_id_array") or ())
    variant = "b" if (met and passion) else ("c" if met else "a")
    sp_r = 9 if variant == "b" else 4
    extra_sp = [{"type": "skill_points", "value": "+4"}] if variant == "b" else []
    lines = {M.DARLEY: [{"type": "skill_points", "value": f"+{sp_r}"}],
             M.GODOLPHIN: [{"type": "speed", "value": "+4"}] + extra_sp,
             M.BYERLEY: [{"type": "stamina", "value": "+4"}] + extra_sp}
    choices = []
    for g in M.GODDESSES:
        eff = [{"type": "bond", "char_id": M.DARLEY, "value": "+5"}] + lines[g]
        eff.append({"type": "venus_fragment", "goddess": g})
        if variant == "c":
            eff.append({"type": "venus_passion"})
        choices.append(CE.Choice(effects=eff))
    event_id = CARD_TRAINING[1] if variant == "b" else CARD_TRAINING[0]
    return beat(event_id, CARD_TRAINING[2], from_endpoint=True, priority=CE.PRIO_NORMAL,
                choices=choices, resolver="venus_card_training", support_card_id=M.LINK_CARD,
                once=f"venus:card003:{turn}", payload={"variant": variant, "turn": turn})


def legacy_entry_and_event(full_state: dict, chara_info: dict) -> CE.Event | None:
    st = impl.state(full_state)
    if not story_playable(LEGACY[1]):
        return None
    choices = []
    for g_index, g in enumerate(M.GODDESSES, start=1):
        lv = impl.level(st, g)
        choices.append({"select_index": 1, "receive_item_id": g_index, "target_race_id": 0,
                        "gain_select_id_index": 2 * g_index - 1 if lv >= 4 else 2 * g_index,
                        "select_icon": 0})
    entry = {"event_id": LEGACY[0], "chara_id": 0, "story_id": LEGACY[1], "play_timing": 6,
             "event_contents_info": {"support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
                                     "choice_array": choices, "tips_training_partner_id": None},
             "succession_event_info": None, "minigame_result": None}
    return CE.Event(event_id=LEGACY[0], story_id=LEGACY[1], play_timing=6, raw=entry,
                    once_key="venus:legacy", priority=PRIO_LAST, resolver="venus_legacy",
                    source="venus")


def legacy_effects(st: dict, gain_index: int) -> list:
    g_index = (int(gain_index) + 1) // 2
    if not 1 <= g_index <= 3:
        return []
    g = M.GODDESSES[g_index - 1]
    lv = impl.level(st, g)
    gold, normal = LEGACY_SKILLS[g]
    if lv < 4:
        return [{"type": "skill_hint", "skill_id": normal, "value": "+1"}]
    total = sum(impl.level(st, x) for x in M.GODDESSES)
    out = [{"type": "all_stats", "value": "+10"}, {"type": "skill_points", "value": "+40"},
           {"type": "skill_hint", "skill_id": gold, "value": "+3" if total >= 12 else "+1"}]
    if lv >= 5:
        out.append({"type": "skill_hint", "skill_id": normal, "value": "+1"})
    return out


def final_events(full_state: dict, chara_info: dict, won: bool, sort_id: int) -> list:
    from ...handlers import single_mode_team as smt
    chara = int(chara_info.get("card_id") or 0) // 100
    out = []
    prio = CE.PRIO_FRONT
    if won:
        stat = smt._scale_race_reward(FINAL_WIN_BASE[0], chara_info, full_state)
        sp = smt._scale_race_reward(FINAL_WIN_BASE[1], chara_info, full_state)
        out.append(beat(*FINAL_WIN, timing=3, priority=prio, show_clear=1, sort_id=sort_id,
                        choices=[CE.Choice(effects=[{"type": "all_stats", "value": f"+{stat}"},
                                                    {"type": "skill_points", "value": f"+{sp}"}])]))
    out.append(beat(smt.reporter_issue_event_id(chara_info), MAGAZINE[1], timing=3,
                    priority=prio + 1, once="venus:magazine",
                    choices=[CE.Choice(effects=smt.reporter_issue_effects(chara_info))]))
    if won:
        out.append(beat(*FINAL_WIN_AFTER, timing=3, priority=prio + 2, choices=[CE.Choice(effects=[
            {"type": "all_stats", "value": f"+{FINAL_AFTER_WIN[0]}"},
            {"type": "skill_points", "value": f"+{FINAL_AFTER_WIN[1]}"},
            {"type": "skill_hint", "skill_id": GOOD_GROUND, "value": "+1"}])]))
    else:
        out.append(beat(*FINAL_LOSS_AFTER, timing=3, priority=prio + 2, choices=[CE.Choice(effects=[
            {"type": "all_stats", "value": f"+{FINAL_AFTER_LOSS[0]}"},
            {"type": "skill_points", "value": f"+{FINAL_AFTER_LOSS[1]}"}])]))
    out.extend(smt.friend_ending_ce_events(full_state, chara_info, prio + 3, "venus"))
    if impl.link_card(chara_info):
        done = bool(smt._pal_state(full_state).get(str(M.LINK_CARD), {}).get("finale"))
        stats, sp = CARD_ENDING_PAY[done]
        eff = [{"type": "all_stats", "value": f"+{stats}"}]
        if sp:
            eff.append({"type": "skill_points", "value": f"+{sp}"})
        out.append(beat(*CARD_ENDING[done], timing=3, priority=prio + 3,
                        support_card_id=M.LINK_CARD, choices=[CE.Choice(effects=eff)]))
    hot = 500000000 + chara * 1000 + smt._HOT_SPRING_SUFFIX
    if chara and won and full_state.get(smt.HOT_SPRING_KEY):
        out.append(beat(smt._HOT_SPRING_EVENT_ID, hot, timing=3, priority=prio + 4,
                        once="venus:hot_spring"))
    story = 500000000 + chara * 1000 + (111 if won else 110)
    if chara:
        out.append(beat(TRAINEE_ENDING_WIN if won else TRAINEE_ENDING_LOSS, story, timing=3,
                        priority=prio + 4, once="venus:trainee_ending",
                        choices=([CE.Choice(effects=[{"type": "all_stats", "value": "+5"},
                                                     {"type": "skill_points", "value": "+20"}])]
                                 if won else [])))
    return [e for e in out if e is not None]


@CE.producer("venus_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def venus_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    final = st.get("final") or {}
    if final.get("due"):
        final["due"] = False
        return final_events(ctx.full_state, ctx.chara_info, bool(final.get("won")),
                            int(final.get("sort_id") or 1))
    if int(ctx.chara_info.get("state") or 0) == 2:
        st["due"] = []
        return out
    if played == 1:
        out.append(beat(*OPENING, from_endpoint=True, priority=CE.PRIO_TRAILING))
    if played == 2:
        out.append(beat(*WORLD, from_endpoint=True, priority=CE.PRIO_TRAILING,
                        resolver="venus_world"))
    if played in STORY_BEATS:
        out.append(beat(*STORY_BEATS[played], choices=[], from_endpoint=True,
                        priority=CE.PRIO_TRAILING))
    if played == 13:
        out.append(beat(*REPORTER, from_endpoint=True, priority=CE.PRIO_TRAILING + 1))
    for due in st.get("due") or ():
        ev = knowledge_event(int(due["goddess"]), int(due["spirit_id"]), int(due["count"]),
                             f"{due.get('turn')}:{due.get('kind')}")
        if ev:
            out.append(ev)
    st["due"] = []
    god = st.get("godolphin") or {}
    if god and int(god.get("turn") or 0) == played:
        ev = godolphin_event(ctx.full_state, ctx.chara_info, int(god["command_id"]),
                             god.get("partners") or (), played)
        if ev:
            out.append(ev)
        st["godolphin"] = {}
    if int(st.get("card003_due") or 0) == played:
        st["card003_due"] = 0
        out.append(card_training_event(ctx.full_state, ctx.chara_info, played))
    if played == 77:
        out.append(legacy_entry_and_event(ctx.full_state, ctx.chara_info))
    if impl.sprace_pending(st, played):
        out.append(announce_event(played))
    if arrived == impl.FIRST_OFFER_TURN:
        out.append(beat(*TUTORIAL, choices=[], priority=PRIO_ARRIVAL))
    if arrived in ARRIVAL_BEATS:
        out.append(beat(*ARRIVAL_BEATS[arrived], choices=[], priority=PRIO_ARRIVAL))
    if impl.wisdom_ready(st) and not st.get("wisdom_seen"):
        st["wisdom_seen"] = True
        out.append(first_wisdom_event())
    if not impl.sprace_pending(st, played):
        out += levelup_events(ctx.full_state, arrived)
    return [e for e in out if e is not None]


@CE.resolver("venus_world")
def _world(full_state, chara_info, event, choice_number, **kw):
    k = impl.link_card(chara_info)
    if k:
        for row in chara_info.get("evaluation_info_array") or ():
            if isinstance(row, dict) and row.get("target_id") == k[0]:
                row["is_appear"] = 1
    return {}


@CE.resolver("venus_knowledge")
def _knowledge(full_state, chara_info, event, choice_number, **kw):
    p = event.payload or {}
    impl.add_fragment(full_state, int(p.get("spirit_id") or 0), int(p.get("count") or 1))
    _after_fragments(full_state, chara_info)
    return {}


def _after_fragments(full_state: dict, chara_info: dict) -> None:
    st = impl.state(full_state)
    turn = impl.turn_of(chara_info)
    if impl.wisdom_ready(st) and not st.get("wisdom_seen"):
        st["wisdom_seen"] = True
        ev = first_wisdom_event()
        if ev:
            CE.emit(full_state, ev)
    if not impl.sprace_pending(st, impl.served_turn(full_state, chara_info)):
        emit_levelups(full_state, turn)


@CE.resolver("venus_levelup")
def _levelup(full_state, chara_info, event, choice_number, **kw):
    p = event.payload or {}
    impl.raise_facility(full_state, chara_info, int(p.get("command_id") or 0), int(p.get("level") or 1))
    impl.RI.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("venus_announce")
def _announce(full_state, chara_info, event, choice_number, **kw):
    turn = int((event.payload or {}).get("turn") or 0)
    impl.announce(full_state, chara_info, turn)
    chara_info["playing_state"] = 12
    return {}


@CE.resolver("venus_result")
def _result(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    turn = int((event.payload or {}).get("turn") or 0)
    st["hold_turn"] = 0
    st["sprace"]["stage"] = "done"
    if turn not in st["done_races"]:
        st["done_races"].append(turn)
    chara_info["playing_state"] = 1
    emit_levelups(full_state, impl.turn_of(chara_info), hold="venus_arrivals_done")
    return {}


@CE.hold_gate("venus_arrivals_done")
def _arrivals_done(full_state) -> bool:
    from ...handlers import single_mode_events as sme
    return not (full_state.get(sme.EXTRA_EVENTS_KEY) or full_state.get(sme.PENDING_EVENTS_KEY))


@CE.resolver("venus_card_training")
def _card_training(full_state, chara_info, event, choice_number, **kw):
    idx = max(1, min(3, int(choice_number or 1)))
    goddess = M.GODDESSES[idx - 1]
    rng = impl.rng_for(full_state, "card003", event.payload.get("turn"), idx)
    impl.add_fragment(full_state, M.spirit_id(goddess, rng.randint(1, 6)), 1)
    if (event.payload or {}).get("variant") == "c":
        from ...handlers import conditions, single_mode_team as smt
        smt._grant_pure_passion(full_state, chara_info, M.LINK_CARD, impl.turn_of(chara_info))
        for cond in (conditions.NIGHT_OWL, conditions.SLACKER):
            conditions.remove(chara_info, cond)
    _after_fragments(full_state, chara_info)
    return {}


@CE.resolver("venus_legacy")
def _legacy(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    effects = legacy_effects(st, int(choice_number or 0))
    applied = event_engine.apply_choice(chara_info, {"choices": [{"effects": effects}]}, 1,
                                        full_state=full_state) or {}
    return {"not_up": applied.get("not_up")}
