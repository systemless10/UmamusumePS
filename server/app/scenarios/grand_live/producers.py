
from __future__ import annotations

import logging
import random

from ... import career_events as CE
from ...handlers.career_producers import story_playable as _story_playable
from . import impl as grand_live
from .impl import SCENARIO_ID

log = logging.getLogger("uma-server")


GRAND_LIVE_BEATS: dict = {
    2:  [(202001, 400003400, 0, 6)],
    4:  [(202002, 400003401, 1, 6),
         (202035, 400003228, 1, 6),
         (202069, 400003027, 0, 1)],
    13: [(101007, 400000100, 1, 6)],
    18: [(202004, 400003403, 0, 6)],
    24: [(202005, 400003430, 1, 6),
         (202009, 400003425, 1, 12), (202037, 400003230, 1, 12)],
    25: [(202076, 400003028, 0, 1)],
    30: [(202010, 400003412, 0, 6)],
    36: [(202011, 400003432, 1, 6),
         (202017, 400003426, 1, 12), (202038, 400003230, 1, 12)],
    42: [(202018, 400003404, 0, 6)],
    48: [(202019, 400003435, 1, 6),
         (202039, 400003230, 1, 12)],
    50: [(202022, 400003405, 0, 6)],
    51: [(202023, 400003406, 0, 6)],
    54: [(202024, 400003407, 0, 6)],
    57: [(202025, 400003408, 0, 6)],
    60: [(202026, 400003437, 1, 3),
         (202040, 400003230, 1, 12)],
    65: [(202030, 400003409, 0, 6)],
    66: [(202031, 400003410, 0, 6)],
    67: [(202032, 400003411, 0, 6)],
    69: [(202075, 400003428, 5, 6)],
    71: [(202034, 400003413, 1, 6)],
    72: [(202100, 400003460, 1, 3)],
}

GL_UNLOCK_EVENT = 202002
GL_GIRLS_LEGEND_EVENT = 202034

POST_LIVE_TIMING = 12

SUPPORTER_GRANTS: dict = {
    202009: [1002, 1032],
    202037: [1066, 1024, 1014],
    202017: [1026],
    202038: [1072, 1019],
    202039: [1059, 1023, 1027],
    202040: [1071],
}

LINK_BONUS = {
    202002: (1046, "power"),
    202009: (1032, "wiz"),
    202017: (1026, "guts"),
}
LINK_BONUS_AMOUNT = 10
SCENARIO_LINK_CHARAS = frozenset({1046, 1032, 1026, 1002})
AFTER_CONCERT_SP = 10
AFTER_CONCERT_SP_EVENTS = frozenset({202025})


def _deck_has_chara(chara_info, chara_id) -> bool:
    return any(grand_live.support_card_chara(c.get("support_card_id") or 0) == chara_id
               for c in (chara_info or {}).get("support_card_array") or ())


def link_bonus(full_state, chara_info, event_id) -> dict:
    pick = LINK_BONUS.get(int(event_id or 0))
    if not pick or not _deck_has_chara(chara_info, pick[0]):
        return {}
    paid = grand_live.state(full_state).setdefault("link_bonus_paid", [])
    if int(event_id) in paid:
        return {}
    paid.append(int(event_id))
    stat = pick[1]
    not_up = grand_live._apply_stats(
        chara_info, {stat: LINK_BONUS_AMOUNT, "skill_point": LINK_BONUS_AMOUNT})
    return {"link_bonus": stat, "not_up": not_up or None}

SUPPORTER_FIRST = 1046

TRAINING_PARTNERS_EVENT = 202035

CLOSER_TOGETHER_EVENT = 202075
CLOSER_TOGETHER_SONG_GATE = 16
CLOSER_TOGETHER_CHOICES = {
    1: (1046, 202281, 202282),
    2: (1026, 200431, 200432),
    3: (1032, 201701, 201702),
    4: (1002, 200711, 200712),
    5: (None, 200501, None),
}


def supporters_for(full_state) -> list:
    return list(grand_live.state(full_state).get("supporters") or ())

CONCERT_BEGINS_EVENTS = frozenset({202005, 202011, 202019, 202026, 202100})


def post_live_beats(turn: int) -> list:
    return [b for b in GRAND_LIVE_BEATS.get(turn, ())
            if b[3] == POST_LIVE_TIMING]

_GL_STORY_ONLY = {"effects": []}


@CE.producer("grand_live_beats", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def grand_live_beats(ctx: CE.Ctx) -> list:
    out = []
    for event_id, story_id, n_choices, play_timing in GRAND_LIVE_BEATS.get(ctx.turn, ()):
        if play_timing == POST_LIVE_TIMING:
            continue
        if not _story_playable(story_id):
            log.warning("grand live: story %s missing from master; skipping %s",
                        story_id, event_id)
            continue
        out.append(CE.Event(
            event_id=event_id, story_id=story_id, play_timing=play_timing,
            timing_from_endpoint=play_timing in (3, 6),
            choices=[CE.Choice(effects=[]) for _ in range(n_choices)],
            chara_id=0,
            once_key=f"gl:{event_id}",
            priority=CE.PRIO_SCENARIO,
            resolver="grand_live_beat",
            payload={"turn": ctx.turn},
        ))
    return out


RANDOM_POOL = (
    (202051, 400003300), (202052, 400003301), (202053, 400003302),
    (202054, 400003303), (202055, 400003304),
)
RANDOM_POOL_CHOICES = 2
RANDOM_POOL_TIMING = 1

_DIRECTOR_BOND_10 = {"type": "npc_bond", "target_id": 102, "chara_id": 9002, "value": "+10"}
RANDOM_POOL_EFFECTS = {
    202051: ([{"type": "mood", "value": "+1"}], [{"type": "mood", "value": "+1"}]),
    202053: ([{"type": "performance", "perf": grand_live.PASSION, "value": "+20"}, _DIRECTOR_BOND_10],
             [{"type": "performance", "perf": grand_live.MENTAL, "value": "+20"}, _DIRECTOR_BOND_10]),
    202054: ([{"type": "energy", "value": "+15"}],
             [{"type": "performance", "perf": grand_live.PASSION, "value": "+20"}]),
    202055: ([{"type": "energy", "value": "+10"}],
             [{"type": "guts", "value": "+10"}]),
}
RANDOM_POOL_TURNS = (6, 70)
RANDOM_POOL_CHANCE = 0.9 / (RANDOM_POOL_TURNS[1] - RANDOM_POOL_TURNS[0] + 1)


@CE.producer("grand_live_random_pool", priority=CE.PRIO_SCENARIO,
             scenario=SCENARIO_ID)
def grand_live_random_pool(ctx: CE.Ctx) -> list:
    turn = int(ctx.turn or 0)
    if not (RANDOM_POOL_TURNS[0] <= turn <= RANDOM_POOL_TURNS[1]):
        return []
    remaining = [(e, sid) for e, sid in RANDOM_POOL
                 if not CE.already_fired(ctx.full_state, f"gl:{e}")]
    if not remaining:
        return []
    st = grand_live.state(ctx.full_state)
    rolled = st.get("pool_roll") or {}
    if rolled.get("turn") != turn:
        rng = random.Random(f"glpool:{CE.career_salt(ctx.full_state)}:{turn}")
        pick = rng.choice(remaining)[0] if rng.random() < RANDOM_POOL_CHANCE else 0
        rolled = st["pool_roll"] = {"turn": turn, "event_id": pick}
    hit = next((r for r in remaining if r[0] == rolled.get("event_id")), None)
    if hit is None:
        return []
    event_id, story_id = hit
    if not _story_playable(story_id):
        return []
    return [CE.Event(
        event_id=event_id, story_id=story_id, play_timing=RANDOM_POOL_TIMING,
        choices=[CE.Choice(effects=list(eff)) for eff in
                 RANDOM_POOL_EFFECTS.get(event_id, ([],) * RANDOM_POOL_CHOICES)],
        chara_id=0,
        once_key=f"gl:{event_id}",
        priority=CE.PRIO_SCENARIO,
        resolver="grand_live_beat",
        payload={"turn": turn},
    )]


GRAND_CONCERT_ENDS_EVENTS = frozenset(range(202041, 202051))
CONCERT_ENDS_EVENTS = frozenset({202006, 202012, 202020, 202027}) | GRAND_CONCERT_ENDS_EVENTS
NEW_SUPPORTERS_EVENTS = frozenset({202037, 202038, 202039, 202040})
AFTER_CONCERT_EVENTS = frozenset({202009, 202017, 202025, 202032})


def apply_beat_rewards(full_state, chara_info, event_id) -> dict:
    eid = int(event_id or 0)
    if eid in CONCERT_ENDS_EVENTS:
        paid = grand_live.apply_live_payout(full_state, chara_info)
        if paid:
            log.info("grand live: concert payout on beat %s -- +%s all stats, "
                     "+%s SP, +%s fans%s",
                     eid, paid["stat"], paid["sp"], paid["fans"],
                     f", skill {paid['skill']}" if paid.get("skill") else "")
        if eid in GRAND_CONCERT_ENDS_EVENTS:
            grand_live.state(full_state)["concert_turn"] = 0
        return paid
    granted = []
    for chara_id in SUPPORTER_GRANTS.get(eid, ()):
        if grand_live.add_supporter(full_state, chara_id):
            granted.append(chara_id)
    if eid in AFTER_CONCERT_SP_EVENTS:
        chara_info["skill_point"] = chara_info.get("skill_point", 0) + AFTER_CONCERT_SP
    bonus = link_bonus(full_state, chara_info, eid) if eid in LINK_BONUS and eid != GL_UNLOCK_EVENT else {}
    if eid in NEW_SUPPORTERS_EVENTS:
        if grand_live.settle_lives(full_state):
            log.info("grand live: token caps raised on beat %s", eid)
        grand_live.state(full_state)["concert_turn"] = 0
    if granted:
        log.info("grand live: supporters %s joined on beat %s", granted, eid)
    out = {"supporters": granted} if granted else {}
    if bonus:
        log.info("grand live: link bonus %s on beat %s", bonus.get("link_bonus"), eid)
        out.update(bonus)
    return out


@CE.resolver("grand_live_beat")
def _resolve_grand_live_beat(full_state, chara_info, event, choice_number, **kw):
    turn = (event.payload or {}).get("turn") or chara_info.get("turn") or 0

    if event.event_id in CONCERT_BEGINS_EVENTS:
        chara_info["playing_state"] = grand_live.PLAYING_STATE_BACKSTAGE
        grand_live.state(full_state)["concert_turn"] = int(turn or 0)
        log.info("grand live: backstage screen open (event %s, turn %s)",
                 event.event_id, turn)
        return {"playing_state": grand_live.PLAYING_STATE_BACKSTAGE}

    if event.event_id == GL_UNLOCK_EVENT:
        result = {}
        if grand_live.grant_free_song(full_state, grand_live.MAKE_DEBUT_LIVE_ID,
                                      turn=turn, chara_info=chara_info):
            log.info("grand live: lessons unlocked on turn %s, Make Debut! granted",
                     turn)
            result["unlocked"] = True
            result["song"] = grand_live.MAKE_DEBUT_LIVE_ID
        result["not_up"] = grand_live._apply_stats(
            chara_info, {"guts": 10, "skill_point": 10}) or None
        bonus = link_bonus(full_state, chara_info, GL_UNLOCK_EVENT)
        if bonus.get("not_up"):
            result["not_up"] = list(result["not_up"] or ()) + list(bonus["not_up"])
        joined = []
        trainee = int(chara_info.get("card_id") or 0) // 100
        if trainee and grand_live.add_supporter(full_state, trainee):
            joined.append(trainee)
        if grand_live.add_supporter(full_state, SUPPORTER_FIRST):
            joined.append(SUPPORTER_FIRST)
        if joined:
            result["supporters"] = joined
        return result
    elif event.event_id == GL_GIRLS_LEGEND_EVENT:
        if grand_live.grant_free_song(full_state, grand_live.GIRLS_LEGEND_U_LIVE_ID,
                                      chara_info=chara_info):
            log.info("grand live: Girls' Legend U granted (not counted toward 18)")
            return {"song": grand_live.GIRLS_LEGEND_U_LIVE_ID}
    elif event.event_id == TRAINING_PARTNERS_EVENT:
        return _recruit_training_partners(full_state, chara_info)
    elif event.event_id == CLOSER_TOGETHER_EVENT:
        return _resolve_closer_together(full_state, chara_info, choice_number)
    return {}


def _resolve_closer_together(full_state, chara_info, choice_number) -> dict:
    if grand_live.countable_songs(full_state) < CLOSER_TOGETHER_SONG_GATE:
        return {}
    pick = CLOSER_TOGETHER_CHOICES.get(int(choice_number or 0))
    if not pick:
        return {}
    linked_chara, gold_id, white_id = pick

    is_linked = False
    if linked_chara:
        trainee = int(chara_info.get("card_id") or 0) // 100
        is_linked = trainee == linked_chara or any(
            grand_live.support_card_chara(c.get("support_card_id") or 0) == linked_chara
            for c in chara_info.get("support_card_array") or ())

    skill_id = gold_id if (gold_id and (is_linked or white_id is None)) else white_id
    if not skill_id:
        return {}
    rarity = grand_live.skill_rarity(skill_id)
    grand_live.add_skill_tip(chara_info, skill_id, 1, rarity=rarity)
    log.info("grand live: Closer Together granted skill %s (rarity %s)", skill_id, rarity)
    return {"hint": skill_id}


def _recruit_training_partners(full_state, chara_info) -> dict:
    deck = chara_info.get("support_card_array") or []
    seated = set(grand_live.state(full_state).get("supporters") or ())
    candidates, seen = [], set()
    for card in sorted(deck, key=lambda c: int(c.get("position") or 0)):
        chara_id = grand_live.support_card_chara(card.get("support_card_id") or 0)
        if chara_id in SCENARIO_LINK_CHARAS:
            continue
        if not grand_live.is_trainee_chara(chara_id):
            continue
        if chara_id not in seen and chara_id not in seated:
            seen.add(chara_id)
            candidates.append(chara_id)

    if not candidates:
        log.warning("grand live: training-partners beat found no eligible "
                    "deck characters to recruit (empty/exhausted deck)")
        return {}

    granted = [c for c in candidates if grand_live.add_supporter(full_state, c)]
    if granted:
        log.info("grand live: training partners %s joined as supporters", granted)
    return {"supporters": granted} if granted else {}
