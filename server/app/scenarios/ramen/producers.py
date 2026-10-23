
from __future__ import annotations

import logging
import random

from ... import career_events as CE
from ...handlers.career_producers import story_playable
from . import formula, impl, master as M
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")

OPENING = (214001, 400014000)
REGION_OPEN = (214002, 400014001)
REGION_FOLLOW = {1: (214017, 400014102), 2: (214018, 400014102), 3: (214019, 400014102)}
REPORTER = (214048, 400000100)
STORY_BEATS = {16: (214032, 400014003), 20: (214033, 400014004),
               32: (214034, 400014007), 43: (214035, 400014010),
               52: (214036, 400014013)}
JAMBOREE_OPEN = {24: (214003, 400014005), 48: (214004, 400014011),
                 72: (214005, 400014018)}
ULTIMATE = (214026, 400014017)
SUMMER_START = {37: (214020, 400014008), 61: (214021, 400014014)}
SUMMER_DAYS = {38: (214024, 400014103), 39: (214024, 400014103), 40: (214024, 400014103),
               62: (214025, 400014103), 63: (214025, 400014103), 64: (214025, 400014103)}
SUMMER_END = {40: (214022, 400014009), 64: (214023, 400014015)}
LINK_BEAT = (214030, 400014016)
TRAINING_TIPS = (214029, 400014101)
ENDING = (214037, 400014022)
GAUGE_EVENTS = {"rest": 214012, "outing": 214013, "race": 214014, "infirmary": 214015,
                "summer": 214074, "summer_race": 214075}
GAUGE_STORY = 400014100

SUMMER_START_ENERGY, SUMMER_START_SECRETS = 15, 2
SUMMER_DAY_ENERGY, SUMMER_DAY_SECRETS = 10, 1
REGION_SECRETS = 2
REGION_TRICKS = {2: (1, 2, 3, 1, 2, 3), 1: (1, 2, 3), 0: (1, 2, 3)}
ENDING_STATS, ENDING_SP = 40, 200
ENDING_HINT = (210391, 2)
STORY_ENDING_TRIGGER = 10

JAMBOREE_OPEN_EVENTS = frozenset(e for e, _s in JAMBOREE_OPEN.values())
RESULT_EVENTS = frozenset(impl.RESULT_STORIES)


def _ack(n: int = 1) -> list:
    return [CE.Choice(effects=[]) for _ in range(n)]


def _beat(event_id, story_id, *, choices=None, timing=1, from_endpoint=False,
          once=None, priority=CE.PRIO_SCENARIO, resolver=None, payload=None,
          show_clear=0, sort_id=0) -> CE.Event | None:
    if not story_playable(story_id):
        log.warning("ramen: story %s missing from master; skipping %s", story_id, event_id)
        return None
    return CE.Event(
        event_id=event_id, story_id=story_id, play_timing=timing,
        timing_from_endpoint=from_endpoint,
        choices=_ack() if choices is None else choices, chara_id=0,
        once_key=once or f"ramen:{event_id}", priority=priority, resolver=resolver,
        payload=payload or {}, source="ramen", show_clear=show_clear,
        show_clear_sort_id=sort_id)


def opening_event() -> CE.Event | None:
    return _beat(*OPENING, priority=CE.PRIO_FRONT)


def region_follow_event(select_type: int) -> CE.Event | None:
    event_id, story_id = REGION_FOLLOW[select_type]
    return _beat(event_id, story_id, priority=CE.PRIO_FRONT, resolver="ramen_region_follow",
                 payload={"select_type": select_type})


def ending_event() -> CE.Event | None:
    skill_id, level = ENDING_HINT
    return _beat(*ENDING, timing=3, priority=CE.PRIO_FRONT, choices=[CE.Choice(effects=[
        {"type": "all_stats", "value": f"+{ENDING_STATS}"},
        {"type": "skill_point", "value": f"+{ENDING_SP}"},
        {"type": "skill_hint", "skill_id": skill_id, "value": f"+{level}"}])])


def link_choices(chara_info: dict) -> list:
    linked = impl.linked_charas(chara_info)
    out = []
    for charas, normal, upgraded in impl.LINK_HINTS:
        skill_id = upgraded if linked & set(charas) else normal
        out.append(CE.Choice(effects=[{"type": "skill_hint", "skill_id": skill_id,
                                       "value": "+1"}]))
    return out


@CE.producer("ramen_calendar", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def ramen_calendar(ctx: CE.Ctx) -> list:
    played, arrived = int(ctx.turn or 0), int(ctx.advanced_turn or 0)
    st = impl.state(ctx.full_state)
    out = []
    if arrived == min(M.region_select_turns() or {3: 1}):
        out.append(_beat(*REGION_OPEN, resolver="ramen_region_open"))
    if played == 13:
        out.append(_beat(*REPORTER, from_endpoint=True))
    if played in STORY_BEATS:
        out.append(_beat(*STORY_BEATS[played], choices=[], from_endpoint=True))
    if arrived in SUMMER_START:
        out.append(_beat(*SUMMER_START[arrived], resolver="ramen_secrets",
                         payload={"secrets": SUMMER_START_SECRETS},
                         choices=[CE.Choice(effects=[{"type": "energy",
                                                      "value": f"+{SUMMER_START_ENERGY}"}])]))
    if arrived in SUMMER_DAYS:
        event_id, story_id = SUMMER_DAYS[arrived]
        out.append(_beat(event_id, story_id, once=f"ramen:{event_id}:{arrived}",
                         resolver="ramen_secrets", payload={"secrets": SUMMER_DAY_SECRETS},
                         choices=[CE.Choice(effects=[{"type": "energy",
                                                      "value": f"+{SUMMER_DAY_ENERGY}"}])]))
    if played in SUMMER_END:
        out.append(_beat(*SUMMER_END[played], from_endpoint=True))
    if played == 65:
        out.append(_beat(*LINK_BEAT, from_endpoint=True, choices=link_choices(ctx.chara_info)))
    if played in JAMBOREE_OPEN and not _held(st, played):
        if played == 72:
            out.append(_beat(*ULTIMATE, from_endpoint=True, priority=CE.PRIO_TRAILING,
                             choices=_ack(3), resolver="ramen_ultimate",
                             show_clear=impl.ULTIMATE_SHOW_CLEAR,
                             sort_id=impl.goal_sort_id(ctx.chara_info, played)))
        out.append(_beat(*JAMBOREE_OPEN[played], from_endpoint=True,
                         priority=CE.PRIO_TRAILING, resolver="ramen_jamboree_open"))
    return [e for e in out if e is not None]


def _held(st: dict, turn: int) -> bool:
    cp = M.check_point_for_turn(turn)
    return any(r["check_point_type"] == cp for r in st["results"])


@CE.producer("ramen_gauge", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def ramen_gauge(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    kind = st.get("gauge_due") or ""
    if not kind:
        return []
    st["gauge_due"] = ""
    played = int(ctx.turn or 0)
    if not impl.gauge_active(st, played):
        return []
    if played in impl.SUMMER_TURNS:
        kind = "summer_race" if kind == "race" else "summer"
    event_id = GAUGE_EVENTS[kind]
    race = kind in ("race", "summer_race")
    return [_beat(event_id, GAUGE_STORY, from_endpoint=not race, timing=3 if race else 6,
                  once=f"ramen:gauge:{played}:{event_id}", priority=CE.PRIO_SPECIAL,
                  resolver="ramen_gauge", payload={"turn": played})]


@CE.producer("ramen_training_tips", priority=CE.PRIO_SPECIAL, scenario=SCENARIO_ID)
def ramen_training_tips(ctx: CE.Ctx) -> list:
    st = impl.state(ctx.full_state)
    played = int(ctx.turn or 0)
    trained = st["marks"].get("trained") or {}
    if trained.get("turn") != played:
        return []
    if not impl.has_effect(st, played, M.EFFECT_TRAINING_TIPS):
        return []
    cmd = int(trained.get("command_id") or 0)
    partners = (st["preview"].get("partners") or {}).get(str(cmd)) or []
    by_pos = formula.deck(ctx.chara_info)
    cards = [by_pos[p] for p in partners if p in by_pos and formula.is_normal_card(by_pos[p])]
    if not cards:
        return []
    rng = random.Random(f"ramen:tips:{CE.career_salt(ctx.full_state)}:{played}")
    effects = []
    for sid in cards:
        effects += impl.card_hint_effects(ctx.chara_info, sid, rng)
    return [_beat(*TRAINING_TIPS, from_endpoint=True, once=f"ramen:tips:{played}",
                  priority=CE.PRIO_SPECIAL, choices=[CE.Choice(effects=effects)])]


def _set_playing_state(chara_info: dict, value: int) -> None:
    chara_info["playing_state"] = value


@CE.resolver("ramen_region_open")
def _region_open(full_state, chara_info, event, choice_number, **kw):
    impl.recruit_npcs(full_state, chara_info)
    _set_playing_state(chara_info, 45)
    ev = region_follow_event(1)
    if ev:
        CE.emit(full_state, ev)
    return {}


@CE.resolver("ramen_region_follow")
def _region_follow(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    impl.add_tricks(st, REGION_TRICKS[impl.link_type(chara_info)])
    impl.add_secrets(st, REGION_SECRETS)
    return {}


@CE.resolver("ramen_secrets")
def _secrets(full_state, chara_info, event, choice_number, **kw):
    impl.add_secrets(impl.state(full_state), int((event.payload or {}).get("secrets") or 0))
    return {}


@CE.resolver("ramen_jamboree_open")
def _jamboree_open(full_state, chara_info, event, choice_number, **kw):
    _set_playing_state(chara_info, 46)
    return {}


@CE.resolver("ramen_ultimate")
def _ultimate(full_state, chara_info, event, choice_number, **kw):
    kind = max(1, min(3, int(choice_number or 1)))
    impl.state(full_state)["uraf"]["type"] = kind
    log.info("ramen: Ultimate Ramen %s chosen", kind)
    return {}


@CE.resolver("ramen_result")
def _result(full_state, chara_info, event, choice_number, **kw):
    payload = event.payload or {}
    cp, result = int(payload.get("check_point_type") or 0), int(payload.get("result_state") or 0)
    st = impl.state(full_state)
    if impl.JAMBOREE_REWARD.get((cp, result), (0, 0, 0, False))[3]:
        impl.raise_all_facilities(full_state, chara_info)
    st["tricks"] = []
    _set_playing_state(chara_info, 48 if cp == 3 else 45)
    impl.refresh_home(full_state, chara_info)
    return {}


@CE.resolver("ramen_gauge")
def _gauge(full_state, chara_info, event, choice_number, **kw):
    st = impl.state(full_state)
    turn = int((event.payload or {}).get("turn") or impl.turn_of(chara_info))
    impl.spend_gauge(st, impl.plain_gauge(st, chara_info, turn))
    impl.flag(st, "gauge_gained", True)
    return {}
