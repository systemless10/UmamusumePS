
from __future__ import annotations

import logging
import random

from ... import career_events as CE
from . import impl as unity_cup

log = logging.getLogger("uma-server")

SCENARIO_ID = unity_cup.SCENARIO_ID

BEATS: dict = {
    1:  [(201024, 400002400, 0, 1, 0, 0)],
    2:  [(201025, 400002401, 0, 6, 1, 0),
         (201114, 400002214, 0, 6, 1, 0),
         (201103, 400002041, 0, 1, 0, 0)],
    4:  [(201026, 400002402, 0, 6, 1, 0),
         (201160, 400002222, 0, 6, 1, 0)],
    5:  [(201027, 400002403, 0, 6, 0, 0),
         (201161, 400002221, 0, 6, 1, 0)],
    6:  [(201158, 400002219, 0, 6, 1, 0)],
    7:  [(201001, 400002100, 0, 6, 1, 0),
         (201159, 400002220, 0, 6, 1, 0)],
    13: [(101007, 400000100, 0, 6, 1, 0)],
    18: [(201028, 400002404, 0, 6, 1, 0)],
    21: [(201093, 400002116, 0, 10, 0, 0)],
    24: [(201029, 400002405, 0, 10, 1, 0)],
    33: [(201002, 400002101, 0, 6, 1, 0),
         (201095, 400002116, 0, 10, 0, 0)],
    36: [(201034, 400002409, 0, 10, 1, 0)],
    41: [(201039, 400002414, 0, 6, 0, 0)],
    43: [(201040, 400002415, 0, 6, 0, 0)],
    44: [(201041, 400002416, 0, 3, 0, 0)],
    45: [(201097, 400002116, 0, 10, 0, 0)],
    48: [(201042, 400002417, 0, 3, 1, 0)],
    49: [(201046, 400002421, 0, 6, 0, 0)],
    50: [(201047, 400002422, 0, 6, 1, 0)],
    54: [(201048, 400002423, 0, 3, 0, 0)],
    57: [(201003, 400002102, 0, 6, 1, 0),
         (201098, 400002116, 0, 10, 0, 0)],
    60: [(201049, 400002424, 0, 3, 1, 0)],
    65: [(201053, 400002428, 0, 6, 0, 0)],
    68: [(201054, 400002429, 0, 3, 0, 0)],

    72: [(201055, 400002430, 0, 3, 1, 0)],
}

BEAT_EFFECTS = {
    201028: [{"type": "mood", "value": "+1"}],
}

ACTING_DIRECTOR_EVENT = 201026
ACTING_DIRECTOR_TURN = 4


POWER_UP_EVENTS = {
    2: (201010, 400002109),
    3: (201077, 400002118),
    4: (201011, 400002110),
    5: (201078, 400002119),
    6: (201012, 400002111),
    7: (201013, 400002112),
    8: (201014, 400002113),
    9: (201015, 400002121),
}
POWER_UP_TIMING = 10
POWER_UP_KEY = "unity_cup_power_up"

POWER_UP_GAIN = {2: 4, 3: 4, 4: 5, 5: 5, 6: 6, 7: 7, 8: 8, 9: 10}

ITS_ON_SKILL = 200461
ITS_ON_LEVELS = (8, 9)
ITS_ON_HINT = 1
ITS_ON_HINT_LINKED = 3

RACE_GATE_EVENTS = frozenset({
    201029,
    201034,
    201042,
    201049,
    201165,
    201055,
    201163,
})

TEAM_RACE_RESULT_EVENTS = {
    1: ((201030, 400002406), (201031, 400002407), (201032, 400002408)),
    2: ((201035, 400002410), (201036, 400002411), (201037, 400002412)),
    3: ((201043, 400002418), (201044, 400002419), (201045, 400002420)),
    4: ((201050, 400002425), (201051, 400002426), (201052, 400002427)),
    5: ((201056, 400002431), (201057, 400002432), (201058, 400002433)),
}

TEAM_RACE_JOIN_EVENTS = {
    1: (201146, 400002216),
    2: (201147, 400002216),
    3: (201148, 400002216),
}

def team_race_join_round(event_id):
    for round_no, (ev, _story) in TEAM_RACE_JOIN_EVENTS.items():
        if ev == event_id:
            return round_no
    return 0


RESULT_SHOW_CLEAR = 4


ELITE_RACE_ROUND = 4
ROUND_FOUR_GATE = (201049, 400002424)
ELITE_ROUND_FOUR_GATE = (201165, 400002446)
ELITE_WIN_EVENT = (201166, 400002447)
FINALS_GATE = (201055, 400002430)
ZENITH_STRONG_GATE = (201163, 400002448)
ZENITH_STRONG_WIN = (201164, 400002449)


def post_race_beats_for_round(round_no, result_state, beat_elite=False) -> list:
    outcomes = TEAM_RACE_RESULT_EVENTS.get(int(round_no or 0))
    if not outcomes:
        return []
    index = {1: 0, 3: 1, 2: 2}.get(int(result_state or 1), 0)
    event_id, story_id = outcomes[index]
    if int(round_no or 0) == 5 and beat_elite and index == 0:
        event_id, story_id = ZENITH_STRONG_WIN
    if int(round_no or 0) == ELITE_RACE_ROUND and beat_elite and index == 0:
        event_id, story_id = ELITE_WIN_EVENT
    out = [(event_id, story_id, 0, POST_RACE_TIMING, 1, RESULT_SHOW_CLEAR)]
    join = TEAM_RACE_JOIN_EVENTS.get(int(round_no or 0))
    if join:
        out.append((join[0], join[1], 0, POST_RACE_TIMING, 1, 0))
    if int(round_no or 0) == len(unity_cup.TEAM_RACE_TURNS):
        out.append((URA_INVITE_EVENT, URA_INVITE_STORY, 0, POST_RACE_TIMING, 0, 0))
    return out


def _p(stat, n):
    return {"type": stat, "value": "+%d" % n}


RANDOM_POOL = (
    (201066, 400002300, 0, 9006, [_p("energy", 10)],
     [_p("energy", 15), {"type": "mood", "value": "+1"}]),
    (201067, 400002301, 0, 9006, [_p("wisdom", 10)],
     [_p("wisdom", 20), _p("skill_points", 10)]),
    (201068, 400002302, 1010, 1010, [_p("speed", 10)],
     [_p("speed", 20), _p("skill_points", 10)]),
    (201069, 400002303, 1030, 1030, [_p("stamina", 10)],
     [_p("stamina", 20), _p("skill_points", 10)]),
    (201070, 400002304, 1056, 1056, [_p("wisdom", 10)],
     [_p("wisdom", 20), _p("skill_points", 10)]),
    (201071, 400002305, 1052, 1052, [_p("guts", 10)],
     [_p("guts", 20), _p("skill_points", 10)]),
)
RANDOM_POOL_TURNS = (4, 71)
RANDOM_POOL_TIMING = 1
RANDOM_POOL_CHANCE = 1.75 / (RANDOM_POOL_TURNS[1] - RANDOM_POOL_TURNS[0] + 1)


def _linked(chara_info: dict, chara_id: int) -> bool:
    if int((chara_info or {}).get("card_id") or 0) // 100 == chara_id:
        return True
    from ... import master_data
    return any(master_data.support_card_chara(c.get("support_card_id") or 0) == chara_id
               for c in (chara_info or {}).get("support_card_array") or ())


@CE.producer("unity_cup_random_pool", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_random_pool(ctx: CE.Ctx) -> list:
    turn = int(ctx.turn or 0)
    if not (RANDOM_POOL_TURNS[0] <= turn <= RANDOM_POOL_TURNS[1]):
        return []
    remaining = [row for row in RANDOM_POOL
                 if not CE.already_fired(ctx.full_state, "unity_cup_pool:%d" % row[0])
                 and not CE.already_fired(ctx.full_state, "unity_cup_beat:%d" % row[0])]
    if not remaining:
        return []
    st = unity_cup.state(ctx.full_state)
    rolled = st.get("pool_roll") or {}
    if rolled.get("turn") != turn:
        rng = random.Random("ucpool:%s:%s" % (CE.career_salt(ctx.full_state), turn))
        pick = rng.choice(remaining)[0] if rng.random() < RANDOM_POOL_CHANCE else 0
        rolled = st["pool_roll"] = {"turn": turn, "event_id": pick}
    row = next((r for r in remaining if r[0] == rolled.get("event_id")), None)
    if row is None:
        return []
    event_id, story_id, chara_id, link, base, linked = row
    effects = linked if _linked(ctx.chara_info, link) else base
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=RANDOM_POOL_TIMING,
        choices=[CE.Choice(effects=list(effects))],
        chara_id=chara_id or None,
        once_key="unity_cup_pool:%d" % event_id,
        priority=CE.PRIO_SCENARIO,
    )]


ZENITH_WAR_EVENT = 201099
ZENITH_WAR_STORY = 400002117
ZENITH_WAR_TURN = 69
ZENITH_WAR_TIMING = 10
ZENITH_WAR_KEY = "unity_cup_zenith_war"

SPIRIT_SKILLS = {
    "speed":   (210011, 210012),
    "stamina": (210021, 210022),
    "power":   (210031, 210032),
    "guts":    (210041, 210042),
    "wiz":     (210051, 210052),
}

ZENITH_WAR_BANDS = (
    (13, True, 3, 40),
    (10, True, 1, 30),
    (7, False, 3, 20),
    (4, False, 1, 10),
)


def zenith_war_reward(bursts: int, stat: str) -> list:
    for floor, gold, level, amount in ZENITH_WAR_BANDS:
        if int(bursts or 0) >= floor:
            skill_id = SPIRIT_SKILLS[stat][0 if gold else 1]
            return [{"type": stat, "value": "+%d" % amount},
                    {"type": "skill_point", "value": "+%d" % amount},
                    {"type": "skill_hint", "skill_id": skill_id,
                     "value": "+%d" % level}]
    return []


@CE.producer(ZENITH_WAR_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_zenith_war(ctx: CE.Ctx) -> list:
    if int(ctx.turn or 0) != ZENITH_WAR_TURN:
        return []
    st = unity_cup.state(ctx.full_state)
    stat = unity_cup.best_stat(st)
    effects = zenith_war_reward(st.get("bursts"), stat)
    return [CE.Event(
        event_id=ZENITH_WAR_EVENT,
        story_id=ZENITH_WAR_STORY,
        play_timing=ZENITH_WAR_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key=ZENITH_WAR_KEY,
        priority=CE.PRIO_SCENARIO,
    )]


TEAM_NAME_TURN = 18
TEAM_NAME_EVENT_PICK = (201154, 400002445)
TEAM_NAME_EVENT_FIXED = (201019, 400002444)
TEAM_NAME_TIMING = 10
TEAM_NAME_RESOLVER = "unity_cup_team_name"

FINALS_PRESENT_STORY = 400002434
FINALS_PRESENT_WON = 201104
FINALS_PRESENT_LOST = 201060
FINALS_PRESENT_LOST_NONE = 201059
FINALS_PRESENT_TIMING = 3
FINALS_PRESENT_TURN = 78


def finals_present_beat(name_id: int, won_ura: bool) -> tuple:
    name_id = int(name_id or 0)
    if won_ura:
        event_id = FINALS_PRESENT_WON + name_id
    else:
        event_id = (FINALS_PRESENT_LOST + name_id if name_id
                    else FINALS_PRESENT_LOST_NONE)
    return event_id, FINALS_PRESENT_STORY + name_id


SCOUT_JOIN_EVENT = 201020
SCOUT_JOIN_STORY = 400002205
SCOUT_JOIN_TIMING = 6

TURN_START_TIMING = 1
AFTER_TRAINING_TIMING = 6
POST_RACE_TIMING = 9

TEAM_SUPPORT_EVENT = 201114

SCENARIO_INTRO_EVENT = 201025


def beats_for(turn) -> list:
    return BEATS.get(int(turn or 0), [])


def post_race_beats(turn) -> list:
    return [b for b in beats_for(turn) if b[3] == POST_RACE_TIMING]


def _is_training_poll(ctx) -> bool:
    payload = getattr(ctx, "payload", None) or {}
    return payload.get("command_type") is not None


@CE.producer("unity_cup_beats", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_beats(ctx: CE.Ctx) -> list:
    training = _is_training_poll(ctx)
    out = []
    for event_id, story_id, chara_id, timing, choice_count, show_clear in beats_for(ctx.turn):
        if timing == POST_RACE_TIMING:
            continue
        if (event_id, story_id) == FINALS_GATE and unity_cup.beat_elite(
                unity_cup.state(ctx.full_state)):
            event_id, story_id = ZENITH_STRONG_GATE
        if (event_id, story_id) == ROUND_FOUR_GATE and unity_cup.elite_available(
                unity_cup.state(ctx.full_state), unity_cup.ELITE_RACE_INDEX):
            event_id, story_id = ELITE_ROUND_FOUR_GATE
        if event_id in RACE_GATE_EVENTS:
            timing = gate_timing(training)
        if timing == AFTER_TRAINING_TIMING and not training:
            continue
        out.append(CE.Event(
            event_id=event_id,
            story_id=story_id,
            play_timing=timing,
            choices=([CE.Choice(effects=list(BEAT_EFFECTS.get(event_id, ())))]
                     if choice_count else []),
            chara_id=chara_id or None,
            once_key=f"unity_cup_beat:{event_id}",
            priority=CE.PRIO_SCENARIO,
        ))
    if training and int(ctx.turn or 0) > ACTING_DIRECTOR_TURN:
        late = next((b for b in beats_for(ACTING_DIRECTOR_TURN)
                     if b[0] == ACTING_DIRECTOR_EVENT), None)
        if late:
            out.append(CE.Event(
                event_id=late[0], story_id=late[1],
                play_timing=AFTER_TRAINING_TIMING,
                choices=[CE.Choice(effects=[])] if late[4] else [],
                chara_id=late[2] or None,
                once_key=f"unity_cup_beat:{late[0]}",
                priority=CE.PRIO_SCENARIO,
            ))
    out.sort(key=lambda e: e.play_timing == TURN_START_TIMING)
    return out


RACE_GATE_BY_ROUND = (201029, 201034, 201042, 201049, 201055)

GATE_TIMING_TRAINING = 10
GATE_TIMING_RACE = 3


def gate_timing(training: bool) -> int:
    return GATE_TIMING_TRAINING if training else GATE_TIMING_RACE


def retime_race_gate(full_state: dict, endpoint: str = "") -> None:
    if not endpoint:
        return
    for row in CE.queued_rows(full_state):
        if int(row.get("event_id") or 0) not in RACE_GATE_EVENTS:
            continue
        if CE.servable_on(row, endpoint):
            continue
        log.info("unity cup: gate %s cannot ride a %s (play_timing %s) -- "
                 "re-stamping it %s", row.get("event_id"),
                 endpoint.rsplit("/", 1)[-1], row.get("play_timing"),
                 GATE_TIMING_TRAINING)
        row["play_timing"] = GATE_TIMING_TRAINING


def _gate_spent(ctx, index: int) -> bool:
    gates = [RACE_GATE_BY_ROUND[index]]
    if gates[0] == FINALS_GATE[0]:
        gates.append(ZENITH_STRONG_GATE[0])
    for gate in gates:
        if CE.find(ctx.full_state, gate) is not None:
            return False
        if CE.already_fired(ctx.full_state, "unity_cup_beat:%d" % gate):
            return True
    return False


@CE.producer("unity_cup_missed_race", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def unity_cup_missed_race(ctx: CE.Ctx) -> list:
    from . import impl
    st = impl.state(ctx.full_state)
    if not st.get("members"):
        return []
    run = len(st.get("races") or ())
    for index, race_turn in enumerate(impl.TEAM_RACE_TURNS):
        if index < run:
            continue
        if ctx.turn <= race_turn and not _gate_spent(ctx, index):
            break
        event_id = RACE_GATE_BY_ROUND[index]
        story_id = next((b[1] for b in beats_for(race_turn)
                         if b[0] == event_id), 0)
        if (event_id, story_id) == FINALS_GATE and unity_cup.beat_elite(st):
            event_id, story_id = ZENITH_STRONG_GATE
        once_key = "unity_cup_missed_race:%d" % index
        if bool(st.get("pending")) or int(
                (ctx.chara_info or {}).get("playing_state") or 0) in (
                    impl.PLAYING_STATE_TEAM_RACE,
                    impl.PLAYING_STATE_TEAM_RACE_RUNNING,
                    impl.PLAYING_STATE_TEAM_RACE_RESULT):
            return []
        CE.unmark_fired(ctx.full_state, once_key)
        return [CE.Event(
            event_id=event_id,
            story_id=story_id,
            play_timing=TURN_START_TIMING,
            choices=[CE.Choice(effects=[])],
            once_key=once_key,
            priority=CE.PRIO_SCENARIO,
        )]
    return []


@CE.producer("unity_cup_power_up", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_power_up(ctx: CE.Ctx) -> list:
    st = unity_cup.state(ctx.full_state)
    shown = int(st.get("power") or 1)
    level = shown + 1
    if unity_cup.earned_power(st) < level:
        return []
    beat = POWER_UP_EVENTS.get(level)
    if not beat:
        return []
    event_id, story_id = beat
    gain = POWER_UP_GAIN.get(level, 0)
    effects = [{"type": "all_stats", "value": "+%d" % gain}] if gain else []
    if level in ITS_ON_LEVELS:
        hint = (ITS_ON_HINT_LINKED
                if unity_cup.trainee_is_scenario_chara(ctx.chara_info)
                else ITS_ON_HINT)
        effects.append({"type": "skill_hint", "skill_id": ITS_ON_SKILL,
                        "value": "+%d" % hint})
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=POWER_UP_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key=f"{POWER_UP_KEY}:{level}",
        priority=CE.PRIO_SCENARIO,
    )]


FACILITY_LEVEL_EVENT = (201121, 400002120)
FACILITY_LEVEL_EVENT_POST_CUP = (201162, 400002120)
FACILITY_LEVEL_TIMING = 11
FACILITY_LEVEL_KEY = "unity_cup_facility_level"


@CE.producer(FACILITY_LEVEL_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_facility_level(ctx: CE.Ctx) -> list:
    st = unity_cup.state(ctx.full_state)
    if st.get("pending") or st.get("pending_award"):
        return []
    owed = unity_cup.facility_level_up_pending(st)
    if not owed:
        return []
    post_cup = len(st.get("races") or ()) >= len(unity_cup.TEAM_RACE_TURNS)
    event_id, story_id = (FACILITY_LEVEL_EVENT_POST_CUP if post_cup
                          else FACILITY_LEVEL_EVENT)
    signature = ",".join("%s:%s" % (cmd, owed[cmd]) for cmd in sorted(owed))
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=FACILITY_LEVEL_TIMING,
        choices=[CE.Choice(effects=[])],
        once_key="%s:%s" % (FACILITY_LEVEL_KEY, signature),
        priority=CE.PRIO_SCENARIO,
    )]


@CE.producer("unity_cup_team_name", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_team_name(ctx: CE.Ctx) -> list:
    st = unity_cup.state(ctx.full_state)
    if unity_cup.team_named(st) or int(ctx.turn or 0) < TEAM_NAME_TURN:
        return []
    options = unity_cup.team_name_options(ctx.chara_info)
    event_id, story_id = (TEAM_NAME_EVENT_PICK if options
                          else TEAM_NAME_EVENT_FIXED)
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=TEAM_NAME_TIMING,
        choices=[CE.Choice(effects=[])
                 for _ in options + [unity_cup.TEAM_NAME_DEFAULT]],
        once_key="unity_cup_team_name",
        resolver=TEAM_NAME_RESOLVER,
        priority=CE.PRIO_SCENARIO,
    )]


@CE.resolver(TEAM_NAME_RESOLVER)
def _resolve_team_name(full_state, chara_info, ev, choice_number, **kwargs):
    options = unity_cup.team_name_options(chara_info) + [unity_cup.TEAM_NAME_DEFAULT]
    index = max(1, int(choice_number or 1)) - 1
    name_id = options[index] if index < len(options) else unity_cup.TEAM_NAME_DEFAULT
    return {"team_name_id": unity_cup.name_team(unity_cup.state(full_state), name_id)}


FINALS_PRESENT_HOLD = "unity_cup_finals_present"


@CE.hold_gate(FINALS_PRESENT_HOLD)
def _ending_chain_reached_present(full_state: dict) -> bool:
    from ...handlers import single_mode_team as smt
    from ...handlers import single_mode_events
    if smt.ENDING_PAYOUT_KEY not in full_state:
        return False
    if not CE.already_fired(full_state, "unity_cup_beat:%d" % FINALE_VICTORY[0]):
        return False
    return not any(
        int((e or {}).get("event_id") or 0) in smt._REPORTER_ISSUE_IDS
        for e in (full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or ()))


@CE.producer("unity_cup_finals_present", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_finals_present(ctx: CE.Ctx) -> list:
    if int(ctx.turn or 0) < FINALS_PRESENT_TURN:
        return []
    st = unity_cup.state(ctx.full_state)
    skill_id, level = unity_cup.team_name_reward(st)
    name_id = int(st.get("team_name_id") or 0) if skill_id else 0
    won_ura = _won_ura_finals(ctx)
    event_id, story_id = finals_present_beat(name_id, won_ura)
    effects = ([{"type": "skill_hint", "skill_id": skill_id, "value": level}]
               if skill_id else [])
    return [CE.Event(
        event_id=event_id,
        story_id=story_id,
        play_timing=FINALS_PRESENT_TIMING,
        choices=[CE.Choice(effects=effects)],
        once_key="unity_cup_finals_present",
        hold_until=FINALS_PRESENT_HOLD,
        priority=CE.PRIO_SCENARIO,
    )]


URA_INVITE_EVENT = 101008
URA_INVITE_STORY = 400000110
URA_INVITE_KEY = "unity_cup_ura_invite"


@CE.producer(URA_INVITE_KEY, priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_ura_invite(ctx: CE.Ctx) -> list:
    st = unity_cup.state(ctx.full_state)
    if len(st.get("races") or ()) < len(unity_cup.TEAM_RACE_TURNS):
        return []
    if st.get("pending") or st.get("pending_award"):
        return []
    return [CE.Event(
        event_id=URA_INVITE_EVENT,
        story_id=URA_INVITE_STORY,
        play_timing=POST_RACE_TIMING,
        choices=[],
        once_key=URA_INVITE_KEY,
        priority=CE.PRIO_SCENARIO,
    )]


FINALE_REUNION = (201110, 400000107)
FINALE_VICTORY = (201111, 400000108)
FINALE_ROUND = 3

FINALE_VICTORY_SP = ((3, 136), (30, 85))


def finale_victory_sp(team_rank: int) -> int:
    for ceiling, sp in FINALE_VICTORY_SP:
        if int(team_rank or 30) <= ceiling:
            return sp
    return FINALE_VICTORY_SP[-1][1]


def _in_finals_round_three(ctx: CE.Ctx) -> bool:
    from ...handlers import single_mode_team as smt
    race_ctx = ctx.full_state.get(smt.RACE_CTX_KEY) or {}
    return smt._finals_round_for_program(race_ctx.get("program_id")) == FINALE_ROUND


def _finals_round_three_raced(ctx: CE.Ctx) -> bool:
    from ...handlers import single_mode_team as smt
    for race in ctx.race_history or ():
        if isinstance(race, dict) and smt._finals_round_for_program(
                race.get("program_id")) == FINALE_ROUND:
            return True
    return False


@CE.producer("unity_cup_finale_bookends", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def unity_cup_finale_bookends(ctx: CE.Ctx) -> list:
    if not unity_cup.won_final(unity_cup.state(ctx.full_state)):
        return []
    if _finals_round_three_raced(ctx):
        st = unity_cup.state(ctx.full_state)
        sp = finale_victory_sp(st.get("team_rank"))
        return [CE.Event(
            event_id=FINALE_VICTORY[0], story_id=FINALE_VICTORY[1],
            play_timing=3,
            choices=[CE.Choice(effects=[{"type": "skill_point",
                                         "value": "+%d" % sp}])],
            once_key="unity_cup_beat:%d" % FINALE_VICTORY[0],
            priority=CE.PRIO_SCENARIO,
        )]
    if not _in_finals_round_three(ctx):
        return []
    return [CE.Event(
        event_id=FINALE_REUNION[0], story_id=FINALE_REUNION[1],
        play_timing=2,
        choices=[CE.Choice(effects=[])],
        once_key="unity_cup_beat:%d" % FINALE_REUNION[0],
        priority=CE.PRIO_SCENARIO,
    )]


def _won_ura_finals(ctx: CE.Ctx) -> bool:
    for race in reversed(ctx.race_history or ()):
        if isinstance(race, dict):
            return int(race.get("result_rank") or race.get("rank") or 0) == 1
    return False


@CE.producer("unity_cup_scout_join", priority=CE.PRIO_SCENARIO, scenario=SCENARIO_ID)
def unity_cup_scout_join(ctx: CE.Ctx) -> list:
    if not _is_training_poll(ctx):
        return []
    st = unity_cup.state(ctx.full_state)
    pending = unity_cup.unannounced_scouts(st)
    if not pending:
        return []
    member = pending[0]
    return [CE.Event(
        event_id=SCOUT_JOIN_EVENT,
        story_id=SCOUT_JOIN_STORY,
        play_timing=SCOUT_JOIN_TIMING,
        chara_id=member["chara_id"],
        support_card_id=unity_cup.scout_support_card(member.get("scout_id")),
        choices=[CE.Choice(effects=[])],
        once_key="unity_cup_scout_join",
        priority=CE.PRIO_SCENARIO,
    )]


def scout_join_events(st: dict) -> dict:
    pending = unity_cup.unannounced_scouts(st)
    return {SCOUT_JOIN_EVENT: pending[0]["target_id"]} if pending else {}


def power_up_level(event_id) -> int:
    for level, (eid, _story) in POWER_UP_EVENTS.items():
        if eid == event_id:
            return level
    return 0
