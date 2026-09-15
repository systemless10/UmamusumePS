"""
Career (URA) event system -- Stage 2, built incrementally.

The real URA career fires events every turn: a response's unchecked_event_array
queues an event; the client calls single_mode/check_event(event_id, choice) to
resolve it, which applies the event's effect (stats/skill points/etc.) and
queues the NEXT event (events chain one at a time). Choice events also call
single_mode/get_choice_reward. Confirmed against the real captures (UmaDumpy
20260720_110112 = a full career to make-debut; Icarus 20260720_111233).

This module owns the event data + resolution. It starts with the career-start
intro chain (character intro -> Tazuna, +120 skill points) and the scenario
schedule scaffold; more events/effects get filled in from the captures over
time. Effects are chara_info deltas; the handlers (single_mode_team) apply them
and thread the pending-event queue through the career state.
"""

from __future__ import annotations

import copy
import math
import random

STAT_KEYS = ("speed", "stamina", "power", "guts", "wiz")

# Where the chained event queue for a viewer lives (on career_state["data"]).
PENDING_EVENTS_KEY = "pending_event_ids"

# Fully-built event_entry dicts queued behind whatever's currently displaying
# this response (see single_mode_team._queue_turn_event / _drain_pending).
# Multiple things can legitimately want to fire the same turn -- a scenario
# cutscene AND a training failure, a duel invite AND a support-card
# coincidence, Rest's flavor event AND an outing story -- and the real game
# plays them one after another rather than dropping all but one.
EXTRA_EVENTS_KEY = "extra_turn_events"

# event_id -> chara_info effect deltas. Sourced from the real captures; expand
# incrementally. Scenario events use chara_id 0; character events are per-uma.
EVENT_EFFECTS: dict[int, dict] = {
    3000: {"skill_point": 120},   # character intro -- grants +120 SP
    # Trackblazer's own intro beat. Same +120 SP: the three real 20260905
    # single_mode_free captures all go 0 -> 120 skill_point across the 4000 ack.
    4000: {"skill_point": 120},
    # 1001 (Tazuna's Training Tips) is purely cosmetic, no effect
}

# Career-start intro: character intro cutscene (+120 SP) -> Tazuna's tips
# (cosmetic). After this the normal turn flow begins.
INTRO_CHAIN = [3000, 1001]

# ... but not in every scenario: Trackblazer opens on the trainee's
# "Self-Introduction" beat (4000) instead of "Introducing <trainee>!" (3000).
# A scenario overrides this whole chain via Scenario.intro_chain, which is
# where that one's evidence is written up (scenarios/trackblazer/__init__.py).

# URA scenario events (chara_id 0, same for every trainee) -> their story asset
# id. From the real capture (UmaDumpy 20260720_110112).
_SCENARIO_STORY = {
    1001: 400000400,   # Tazuna's Training Tips (cosmetic)
    1013: 400001401,   # scenario story (turn 2)
    1014: 400001402,   # Director Akikawa appears (turn 3) -- her unlock cutscene
    1015: 400001403,   # scenario story (turn 4, first)
    102005: 400001422,  # Happy Meek's unlock cutscene (turn 4, SECOND event)
    1016: 400001404,   # Reporter Otonashi's unlock ("A Quirky Correspondent?") --
                       # fires the turn after the debut race (capture: won turn
                       # 12, 1016 last in turn 13's chain, is_appear from 14)
    1002: 400000009,   # Director's Appraisal (bond 0-39)   +2 SP
    1003: 400000010,   # Director's Appraisal (bond 40-69)  +3 SP
    1004: 400000011,   # Director's Appraisal (bond 70-89)  +4 SP
    1005: 400000012,   # Director's Appraisal (bond 90+)    +5 SP
    1007: 400000031,   # Reporter's Appraisal (bond 0-39)   +2 trained stat
    1008: 400000032,   # Reporter's Appraisal (bond 40-69)  +3 trained stat
    1009: 400000033,   # Reporter's Appraisal (bond 70-89)  +4 trained stat
    1010: 400000034,   # Reporter's Appraisal (bond 90+)    +5 trained stat
    1026: 400000005,
    # GRAND LIVE's ids for the same three recurring families. Straight off
    # master.mdb single_mode_story_data, whose `id` IS the event id here:
    # 202056-202060 "Training Level Up" all carry story 400000005 (one id per
    # facility -- see GrandLive.facility_levelup_event), 202061-202064 are the
    # Director's Appraisal tiers on URA's own 400000009-012, and 202065-202068
    # are the Event Producer's Appraisal, this scenario's stand-in for the
    # Reporter's, on 400003100-103.
    202056: 400000005, 202057: 400000005, 202058: 400000005,
    202059: 400000005, 202060: 400000005,
    202061: 400000009, 202062: 400000010, 202063: 400000011, 202064: 400000012,
    202065: 400003100, 202066: 400003101, 202067: 400003102, 202068: 400003103,
    # TRACKBLAZER's ids for the same two families -- same master derivation.
    203019: 400000005, 203020: 400000005, 203021: 400000005,
    203022: 400000005, 203023: 400000005,
    203046: 400000009, 203047: 400000010, 203048: 400000011, 203049: 400000012,
}

# play_timing per event -- the client plays it in this context. 1 = start-of-turn
# intro; 6 = post-command cutscene. The unlock cutscenes (1014/1015/102005 and
# the recurring 1002/1026) are timing 6; using the wrong timing plays them in the
# wrong context and the NPC never visually unlocks. From the real capture.
_PLAY_TIMING = {
    1014: 6, 1015: 6, 102005: 6, 1016: 6,
    1002: 6, 1003: 6, 1004: 6, 1005: 6,
    1007: 6, 1008: 6, 1009: 6, 1010: 6,
    1026: 6,
    # Grand Live's level-up / appraisal families -- same post-command context
    # as URA's, and what the beats that used to carry them already served.
    202056: 6, 202057: 6, 202058: 6, 202059: 6, 202060: 6,
    202061: 6, 202062: 6, 202063: 6, 202064: 6,
    202065: 6, 202066: 6, 202067: 6, 202068: 6,
    203019: 6, 203020: 6, 203021: 6, 203022: 6, 203023: 6,
    203046: 6, 203047: 6, 203048: 6, 203049: 6,
}

# exec_command current_turn -> [scenario event_ids] fired that turn (chained in
# unchecked_event_array, each resolved via check_event, next queued). Matches the
# captured URA scenario progression. Turn 4 fires TWO events: the scenario story
# (1015) THEN Happy Meek's unlock cutscene (102005), which is what visually
# introduces her. Support-card / trainee-specific events are deck/uma dependent
# and intentionally NOT scheduled here.
SCENARIO_SCHEDULE: dict[int, list[int]] = {
    2: [1013],
    3: [1014],            # Director Akikawa unlock cutscene
    4: [1015, 102005],    # scenario story -> Happy Meek unlock cutscene
}


# character event_id -> story suffix (the last 3 digits of 50<chara><nnn>).
# Confirmed from captures; expand as needed. 3000 = the career intro cutscene.
_CHARA_EVENT_STORY_SUFFIX = {3000: 400, 4000: 900}


def _story_id_for(event_id: int, chara_id: int) -> int:
    """The story asset id for an event. Scenario events (chara_id 0) use a fixed
    captured story id; character events (per-uma) use 50<chara><nnn>."""
    if event_id in _SCENARIO_STORY:
        return _SCENARIO_STORY[event_id]
    if event_id in _CHARA_EVENT_STORY_SUFFIX and chara_id:
        return int(f"50{chara_id}{_CHARA_EVENT_STORY_SUFFIX[event_id]:03d}")
    return 0


def _wire_story_id(story_id: int) -> int:
    """event_engine.wire_story_id, imported lazily -- that module imports this
    one, so a top-level import would be a cycle."""
    from .. import event_engine
    return event_engine.wire_story_id(story_id)


def is_scenario_event(event_id: int) -> bool:
    return event_id in _SCENARIO_STORY


def event_entry(event_id: int, chara_id: int) -> dict:
    """One unchecked_event_array entry for the client to play."""
    # Scenario events (in _SCENARIO_STORY) are chara_id 0 regardless of id -- e.g.
    # 102005 is >3000 but is a scenario cutscene, not a character event.
    is_scenario = event_id in _SCENARIO_STORY
    return {
        "event_id": event_id,
        "chara_id": 0 if is_scenario else chara_id,
        "story_id": _wire_story_id(_story_id_for(event_id, chara_id)),
        "play_timing": _PLAY_TIMING.get(event_id, 1),
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [{
                "select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                "gain_select_id_index": 1, "select_icon": 0,
            }],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None,
        "minigame_result": None,
    }


# Stat -> not_up_parameter_info status code (SingleModeDefine.
# ParameterGainLimitType), the same 1-5 numbering the gain displays use.
_STAT_STATUS_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}


def apply_effect(chara_info: dict, event_id: int) -> list:
    """Apply an event's effect deltas to chara_info in place (stats clamped to
    their caps).

    Returns the not_up_parameter_info status codes for the gains that landed on
    something ALREADY at its ceiling, which is what makes the client print
    "<stat> is in superb form" / "Energy is full." on the outcome instead of
    nothing at all (see single_mode_team._note_not_up)."""
    eff = EVENT_EFFECTS.get(event_id)
    if not eff:
        return []
    not_up = []
    for key, val in eff.items():
        if key == "skill_point":
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + val
        elif key in STAT_KEYS:
            cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}", 9999)
            if val > 0 and chara_info.get(key, 0) >= cap:
                not_up.append(_STAT_STATUS_TYPE[key])
            chara_info[key] = min(chara_info.get(key, 0) + val, cap)
        elif key == "vital":
            max_vital = chara_info.get("max_vital", 100)
            if val > 0 and chara_info.get("vital", 100) >= max_vital:
                not_up.append(6)          # energy already full ("Energy is full.")
            chara_info["vital"] = max(0, min(max_vital,
                                             chara_info.get("vital", 100) + val))
    return sorted(not_up)


# Scenario NPCs that become VERSUS TRAINING PARTNERS when an event unlocks them:
# they get added to ura_data_set.evaluation_info_array (member_state 1) and are
# placed in a random facility each turn (versus_event_partner_id). Confirmed
# from the capture: Happy Meek appears from ~turn 5 after her turn-4 event.
# Director Akikawa (event 1014) is a background scenario NPC -- she never appears
# as a versus partner (member_state stays 0), so her event is cutscene-only.
# event_id -> [(target_id, chara_id)].
NPC_UNLOCKS: dict[int, list[tuple[int, int]]] = {
    1014: [(102, 9002)],      # Director Akikawa unlock cutscene (turn 3)
    102005: [(2001, 2001)],   # Happy Meek unlock cutscene (turn 4, 2nd event)
    1016: [(103, 9003)],      # Reporter Otonashi -- turn after the debut race
    # ...and her Unity Cup / Grand Live id. Those scenarios disable the fixed
    # schedule 1016 rides on and run their own turn-13 beat instead (event
    # 101007, story 400000100 -- the SAME story title, "A Quirky
    # Correspondent?"), so without this entry the reporter never appeared in
    # either scenario. Keyed by event_id, so URA is untouched.
    101007: [(103, 9003)],
    # Light Hello (9008) -- GRAND LIVE's own turn-4 unlock beat, "Bring Back
    # the Grand Concert!" (scenarios.grand_live.producers.GL_UNLOCK_EVENT). Same mechanism
    # as Director/Reporter above: this event resolving is what flips her
    # evaluation row's is_appear 0->1 and fires "Light Hello can now appear in
    # training", not "joined your cause" (that message is a different flag on
    # a different array -- see grand_live's _live_members, user-corrected
    # 2026-08-20 after an earlier pass wired her through that one instead).
    # Target_id 105 is her slot in the 101-106 NPC band.
    #
    # AND THE DIRECTOR UNLOCKS WITH HER, in this same event. Grand Live
    # disables the fixed 1013/1014/1015 schedule that introduces Akikawa in
    # URA, so 1014 never fires here and target 102 was left at is_appear 0 for
    # the WHOLE career -- while APPRAISAL_EVENTS happily kept paying her
    # 202061-202064 appraisals for a partner the training screen never showed.
    # User-reported, and the corpus is unanimous: in all three real Grand Live
    # sessions the `check_event(event_id=202002)` response flips exactly two
    # rows at once, Light Hello's own (her deck position when she is a card,
    # which she is in every capture -- support card 30052) and **102**:
    #     20260828_084012/0055  turn 4  flipped [3, 102]
    #     20260904_232507/0084  turn 4  flipped [2, 102]
    #     20260907_204921/0046  turn 4  flipped [2, 102]
    # (The reporter is separate and already right: 101007 on turn 13 flips 103
    # on turn 14, 3/3.)
    202002: [(105, 9008), (102, 9002)],
}

# The Director (102) and the Reporter (103) periodically fire an "Appraisal"
# event after you train the facility they're standing in: the Director pays
# skill points, the Reporter pays the just-trained facility's own stat. The
# reward tier follows the bond gauge via master's single_mode_evaluation
# thresholds (rows for chara 9002/9003: 0 / 40 / 70 / 90). Amounts observed
# live: tier1 +2, tier2 +3 (Director tier3 +4 observed; the +1-per-tier
# pattern extends both series to +5 at 90+).
# target_id -> [(min_bond, event_id, amount)] descending.
APPRAISAL_EVENTS: dict[int, list[tuple[int, int, int]]] = {
    102: [(90, 1005, 5), (70, 1004, 4), (40, 1003, 3), (0, 1002, 2)],
    103: [(90, 1010, 5), (70, 1009, 4), (40, 1008, 3), (0, 1007, 2)],
}
APPRAISAL_BOND_GAIN = 7   # resolving an Appraisal also deepens the bond


def appraisal_for(target_id: int, bond: int, table: dict | None = None):
    """(event_id, amount) of the Appraisal this NPC fires at this bond level,
    or None if the target has no appraisal series.

    `table` is the scenario's own series (Scenario.appraisal_events); None
    means URA's, which is what every caller passed before scenarios had ids of
    their own here."""
    for min_bond, event_id, amount in (table or APPRAISAL_EVENTS).get(target_id, ()):
        if bond >= min_bond:
            return event_id, amount
    return None
# Of the unlocked NPCs, only these are VERSUS/duel partners (get the duel icon,
# ura_data_set.versus_event_partner_id). Director is a normal training partner
# (no duel), so she must NOT get it -- otherwise the duel icon lands on her /
# on whatever support card shares her facility.
VERSUS_NPCS = {2001}   # Happy Meek
# on full_state: list of [target_id, chara_id] currently appearing as partners.
UNLOCKED_NPCS_KEY = "unlocked_versus_npcs"

_URA_FACILITY_COMMANDS = [101, 105, 102, 103, 106]
# The only target_ids that may ever hold NPC evaluation rows (wire-verified).
_REAL_NPC_TARGETS = {102, 103, 2001}


def npcs_unlocked_by(event_id: int) -> list:
    return NPC_UNLOCKS.get(event_id, [])


def npc_placements(turn: int, target_ids) -> dict:
    """{facility command_id: NPC target_id} for this turn. Each NPC rolls a
    facility or 'away' (weight 50); at most ONE NPC per facility (the versus
    slot is single-valued -- two in one facility NullRefs the cutscene), so a
    collision sends the later NPC 'away' this turn. Deterministic by turn so the
    home_info training_partner_array and ura_data_set versus_event_partner_id
    agree (a mismatch NullRefs the training cutscene)."""
    import random
    placement = {}
    for tid in target_ids or ():
        pick = random.Random((int(turn) << 12) ^ int(tid)).choices(
            range(6), weights=[100, 100, 100, 100, 100, 50], k=1)[0]
        if pick == 5:
            continue
        fac = _URA_FACILITY_COMMANDS[pick]
        if fac not in placement:
            placement[fac] = tid
    return placement


MAX_DUELS = 5                          # up to 5 duels per career (versus_level cap)
DUEL_LAST_TURN_KEY = "duel_last_turn"  # on full_state: the turn a duel last resolved


def _has_meek(unlocked) -> bool:
    return any((n[0] if isinstance(n, (list, tuple)) else n) == 2001 for n in (unlocked or ()))


def meek_facility(turn) -> int:
    """The facility Happy Meek stands in this turn (rotates by turn, like a
    support card landing in any facility)."""
    return _URA_FACILITY_COMMANDS[int(turn) % len(_URA_FACILITY_COMMANDS)]


def meek_duel_offered(turn, unlocked, versus_level: int = 1, last_duel_turn=None) -> bool:
    """Whether Happy Meek CHALLENGES you to a duel this turn. She's around most
    turns, but only sometimes offers a duel (per the real game) -- and only while
    duels remain and she hasn't already dueled this turn. Deterministic by turn
    so the marker the player sees matches the trigger when they train it."""
    if turn is None or not _has_meek(unlocked):
        return False
    if versus_level > MAX_DUELS:
        return False
    if last_duel_turn is not None and int(turn) <= int(last_duel_turn):
        return False
    return (int(turn) * 7 + 3) % 5 < 2   # ~40% of her appearances offer a duel


# Backwards-compatible name used by the check_event duel trigger: the facility to
# fire a duel in this turn, or None if no duel is offered.
def happy_meek_facility(turn, unlocked, versus_level: int = 1, last_duel_turn=None):
    if meek_duel_offered(turn, unlocked, versus_level, last_duel_turn):
        return meek_facility(turn)
    return None


def _reconcile_npc_appearance(data: dict, unlocked=()) -> None:
    """is_appear is STICKY: 1 from the NPC's unlock onward, 0 before -- NEVER
    placement-based. Capture-proven (20260726_174114, every response): 102
    flips to 1 at her unlock resolution and stays 1 on EVERY later turn,
    placed or not; 103 stays 0 until her own unlock. The old model reset it
    to facility placement each serve, so every re-placement re-flipped 0->1
    and the client re-announced 'X will now appear in training' at whatever
    event was on screen (debut race, claw game, ... -- the whole ghost-unlock
    saga's last head). Rows for placed-but-missing NPCs are still created."""
    ci = data.get("chara_info")
    hi = data.get("home_info")
    if not (isinstance(ci, dict) and isinstance(hi, dict)):
        return
    unlocked_ids = {(n[0] if isinstance(n, (list, tuple)) else n) for n in unlocked or ()}
    # ONLY real NPC targets may have evaluation rows: the wire has exactly
    # 102 / 103 / 2001 (real capture, every response). The old 'any partner id
    # >= 100' rule minted junk rows for facility-marker ids (101/104/106
    # observed live) -- nameless to the client, which announced them as
    # ' will now appear in training.' (the blank line on Meek's cutscene).
    ev = [e for e in ci.get("evaluation_info_array") or []
          if not (isinstance(e, dict) and isinstance(e.get("target_id"), int)
                  and e["target_id"] >= 100 and e["target_id"] not in _REAL_NPC_TARGETS)]
    ci["evaluation_info_array"] = ev
    seen = set()
    for e in ev:
        if isinstance(e, dict) and e.get("target_id") in _REAL_NPC_TARGETS:
            e["is_appear"] = 1 if e["target_id"] in unlocked_ids else 0
            seen.add(e["target_id"])
    for pid in (unlocked_ids & _REAL_NPC_TARGETS) - seen:
        ev.append({"target_id": pid, "training_partner_id": pid, "evaluation": 0,
                   "is_outing": 0, "story_step": 0, "is_appear": 1,
                   "group_outing_info_array": []})


def apply_versus_state(data: dict, turn, unlocked, versus_level: int = 1, last_duel_turn=None):
    """Place Happy Meek onto a served training screen. Verified against the real
    full-career capture (UmaDumpy 110112), there are THREE separate layers:

    1. MEMBERSHIP (always, once unlocked): she must be in ura_data_set
       .evaluation_info_array at member_state 1 on EVERY turn -- even turns she's
       away or has no duel. Missing this desyncs her from chara_info.evaluation
       and the training INIT NullRefs -> the client freezes on ANY facility.
    2. APPEARANCE (her portrait, most turns): her id in home_info[fac]
       .training_partner_array + is_appear=1 in chara_info.evaluation (set by the
       reconcile below).
    3. DUEL (some turns): the ura_data_set versus_event_partner_id marker. Seeing
       her does NOT always mean a duel.

    Returns the duel facility this turn (or None)."""
    # EVERY unlocked NPC keeps a ura_data_set mapping row {target, chara,
    # member_state 0} on EVERY response -- the client resolves announcement
    # NAMES through this mapping, and a flip arriving without it rendered a
    # nameless ' will now appear in training.' line (live-reported on Meek's
    # cutscene: the Director's row re-flipped there with no mapping present).
    uds0 = data.get("ura_data_set")
    if isinstance(uds0, dict):
        # REBUILD the roster from the LIVE deck + unlocked NPCs -- never trust
        # whatever rows rode in. The persisted career and the response seeds
        # each carry rows frozen from DIFFERENT captured decks (live-hit: the
        # capture deck's Tazuna/Kiryuin/Riko charas kept flickering in and out
        # between response kinds, so the client announced 'X will now appear
        # in training' on every single event).
        ci0 = data.get("chara_info") or {}
        rows = []
        from .. import master_data as _md
        for c in ci0.get("support_card_array") or []:
            pos, sid = c.get("position"), c.get("support_card_id")
            if not pos:
                continue
            row = _md.query_one(
                "SELECT chara_id FROM support_card_data WHERE id=?", (sid,))
            rows.append({"target_id": pos,
                         "chara_id": row["chara_id"] if row else 0,
                         "member_state": 0})
        for n in unlocked or ():
            tid, cid = (n[0], n[1]) if isinstance(n, (list, tuple)) else (n, n)
            rows.append({"target_id": tid, "chara_id": cid,
                         "member_state": 1 if tid == 2001 else 0})
        uds0["evaluation_info_array"] = rows
    if not _has_meek(unlocked):
        _reconcile_npc_appearance(data, unlocked)
        return None

    # (1) MEMBERSHIP -- always register her, every turn, or training NullRefs.
    uds = data.get("ura_data_set")
    if isinstance(uds, dict):
        uev = uds.setdefault("evaluation_info_array", [])
        if not any(isinstance(e, dict) and e.get("target_id") == 2001 for e in uev):
            uev.append({"target_id": 2001, "chara_id": 2001, "member_state": 1})
    ci = data.get("chara_info")
    if isinstance(ci, dict):
        cev = ci.setdefault("evaluation_info_array", [])
        if not any(isinstance(e, dict) and e.get("target_id") == 2001 for e in cev):
            cev.append({"target_id": 2001, "training_partner_id": 2001, "evaluation": 0,
                        "is_outing": 0, "story_step": 0, "is_appear": 0,
                        "group_outing_info_array": []})

    duel_fac = None
    if turn is not None:
        fac = meek_facility(turn)
        # (2) APPEARANCE -- add her to her facility's partner list. (Two NPCs CAN
        # share a facility -- real capture facility 102 = [4, 102, 2001].)
        hi = data.get("home_info")
        if isinstance(hi, dict):
            for c in hi.get("command_info_array") or []:
                if isinstance(c, dict) and c.get("command_id") == fac:
                    tp = c.setdefault("training_partner_array", [])
                    if 2001 not in tp:
                        tp.append(2001)
        # (3) DUEL marker -- only some turns.
        if isinstance(uds, dict) and meek_duel_offered(turn, unlocked, versus_level, last_duel_turn):
            for c in uds.get("command_info_array") or []:
                if isinstance(c, dict) and c.get("command_id") == fac:
                    c["versus_event_partner_id"] = 2001
            duel_fac = fac

    # is_appear reconcile (Happy Meek just placed + Director from the build path).
    _reconcile_npc_appearance(data, unlocked)
    return duel_fac


def choice_reward(event_id: int, choice_number: int) -> dict:
    """get_choice_reward response. Minimal valid structure for now (the client
    reads choice_reward_array); real per-choice gains get filled in later."""
    return {"choice_reward_array": []}


# ============ Training failure (infirmary) events ============
# On a failed training the client fires a chara-specific 2-choice event: the
# NORMAL "Get Well Soon!" (7014, story 50<chara>713) when fail% < 80, or the
# WORST "Don't Overdo It!" (7015, story 50<chara>714) at fail% >= 80. Choice 1 =
# Top, choice 2 = Bottom (choice_number == the chosen gain_select_id_index).
# Committed via get_choice_reward. Verified against capture 20260721_132421.
# (Wit training never fires this -- it just fails with +5 energy, no penalty.)
NORMAL_FAIL_EVENT = 7014
WORST_FAIL_EVENT = 7015
_FAIL_STORY_SUFFIX = {7014: 713, 7015: 714}
FAIL_CTX_KEY = "active_failure"   # on full_state: {event_id, worst, stat}


def failure_event_entry(event_id: int, chara_id: int, worst: bool) -> dict:
    """The unchecked_event_array entry for a training-failure event (Top/Bottom)."""
    return {
        "event_id": event_id,
        "chara_id": chara_id,
        "story_id": int(f"50{chara_id}{_FAIL_STORY_SUFFIX[event_id]:03d}"),
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [
                {"select_index": 2 if worst else 1, "receive_item_id": 0,
                 "target_race_id": 0, "gain_select_id_index": 1, "select_icon": 0},
                {"select_index": 1, "receive_item_id": 0,
                 "target_race_id": 0, "gain_select_id_index": 2, "select_icon": 0},
            ],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def resolve_failure(chara_info: dict, worst: bool, choice_number: int, trained_stat: str) -> dict:
    """Apply a failure-event choice IN PLACE. Top = choice 1, Bottom = choice 2.
    Outcomes per the guide (umareference / user):
      Normal Top: -1 mood, -5 trained. Bottom: -1 mood, -10 trained; ~15% instead
        gets 'Practice Perfect' (no loss).
      Worst Top: -3 mood, +10 energy, -10 trained, -10 to two other random stats.
        Bottom: -3 mood, -10 trained, -10 to two random; ~3% instead +10 energy +
        'Practice Perfect' (no loss).
    (Poor Practice / Practice Perfect skills themselves are a follow-up.)"""
    import random
    is_bottom = (choice_number == 2)
    others = [s for s in STAT_KEYS if s != trained_stat]
    mv = chara_info.get("max_vital", 100)

    def dock(stat, amt):
        chara_info[stat] = max(0, chara_info.get(stat, 0) - amt)

    def drop_mood(n):
        # Floor is 1, not 0. Mood is a 1-5 enum on the wire -- real
        # chara_info.motivation is 1..5 across 1,887 captured responses and is
        # never 0 -- and every other site that moves it clamps to 1..5
        # (conditions.apply_mood, event_engine, trackblazer/shop). This one
        # did not, so a mood dock at Awful put 0 on the wire, including into
        # a race entry's motivation field.
        chara_info["motivation"] = max(1, chara_info.get("motivation", 2) - n)

    # PRACTICE PERFECT is a pure win -- it gives ONLY the condition (worst also
    # keeps its +10 energy) and NO mood/stat loss. So it must be decided BEFORE any
    # penalty is applied (previously the mood dock happened first, so Perfect still
    # cost a mood level -- the "still gives a move down" bug).
    if worst:
        if is_bottom and random.random() < 0.03:          # Bottom rare: Practice Perfect
            chara_info["vital"] = min(mv, chara_info.get("vital", 0) + 10)
            add_condition(chara_info, COND_PRACTICE_PERFECT)
            return {"outcome": "worst_bottom_perfect"}
        # Mood loss differs by option (user-supplied 2026-07-27): Top -2,
        # Bottom -3. It used to dock 3 for both, over-penalising Top.
        drop_mood(3 if is_bottom else 2)
        if not is_bottom:                                 # Top only: +10 energy
            chara_info["vital"] = min(mv, chara_info.get("vital", 0) + 10)
        dock(trained_stat, 10)
        for s in random.sample(others, 2):
            dock(s, 10)
        if is_bottom or random.random() < 0.50:           # Bottom always / Top 50% Poor
            add_condition(chara_info, COND_PRACTICE_POOR)
        return {"outcome": "worst_bottom" if is_bottom else "worst_top"}

    if is_bottom and random.random() < 0.20:              # Bottom: Practice Perfect (user: 20%)
        add_condition(chara_info, COND_PRACTICE_PERFECT)
        return {"outcome": "normal_bottom_perfect"}
    drop_mood(1)
    if not is_bottom:                                     # Normal Top: -5 trained; 8% Poor
        dock(trained_stat, 5)
        if random.random() < 0.08:
            add_condition(chara_info, COND_PRACTICE_POOR)
        return {"outcome": "normal_top"}
    dock(trained_stat, 10)                                # Normal Bottom: -10 trained; ~65% Poor
    if random.random() < 0.65:
        add_condition(chara_info, COND_PRACTICE_POOR)
    return {"outcome": "normal_bottom"}


# The EXACT official choice_reward_array previews for the failure events, copied
# from the real captures (7014: UmaDumpy 20260721_132421/0026; 7015: 145320/0038).
# They list EVERY possible outcome per option (select_index 1 = Top, 2 = Bottom).
# Display ids: 1 = param up (ev0 target, ev1 amount), 2 = param down (ev0 20=mood),
# 20 = the TRAINED stat down by ev0, 34 = down ev1 to ev0 random stats,
# 37 = Poor Practice status, 9 = Practice Perfect. Stat-independent (display_id 20
# means "whatever you trained"), so the same array works for any facility.
_G = lambda did, e0, e1: {"display_id": did, "effect_value_0": e0, "effect_value_1": e1, "effect_value_2": 0}
_NORMAL_FAIL_PREVIEW = [
    {"select_index": 1, "gain_param_array": [_G(2, 20, 1), _G(20, 5, 0)]},                       # Top: -1 mood, -5 trained
    {"select_index": 1, "gain_param_array": [_G(2, 20, 1), _G(37, 6, 0), _G(20, 5, 0)]},         # Top: + Poor Practice
    {"select_index": 2, "gain_param_array": [_G(2, 20, 1), _G(20, 10, 0)]},                      # Bottom: -1 mood, -10 trained
    {"select_index": 2, "gain_param_array": [_G(20, 10, 0), _G(37, 6, 0), _G(2, 20, 1)]},        # Bottom: + Poor Practice
    {"select_index": 2, "gain_param_array": [_G(9, 10, 0)]},                                     # Bottom: Practice Perfect
]
_WORST_FAIL_PREVIEW = [
    {"select_index": 1, "gain_param_array": [_G(1, 10, 10), _G(20, 10, 0), _G(34, 2, 10), _G(2, 20, 3)]},               # Top: +10 en, -10 trained, -10x2, -3 mood
    {"select_index": 1, "gain_param_array": [_G(1, 10, 10), _G(2, 20, 3), _G(20, 10, 0), _G(34, 2, 10), _G(37, 6, 0)]}, # Top: + Poor Practice
    {"select_index": 2, "gain_param_array": [_G(2, 20, 3), _G(20, 10, 0), _G(34, 2, 10), _G(37, 6, 0)]},                # Bottom: -3 mood, -10 trained, -10x2, Poor Practice
    {"select_index": 2, "gain_param_array": [_G(9, 10, 0), _G(1, 10, 10)]},                                             # Bottom: Practice Perfect, +10 en
]


def failure_preview(worst: bool, trained_stat: str = "speed") -> list:
    """The Top/Bottom outcome preview (all possibilities per option), exactly as
    the official server sends it."""
    return copy.deepcopy(_WORST_FAIL_PREVIEW if worst else _NORMAL_FAIL_PREVIEW)


# ============ Conditions (状態) ============
# Active conditions live in chara_info.chara_effect_id_array (a plain list of ids).
# ids/names are text_data category 142; effect_type in single_mode_chara_effect is
# 1 = good, 2 = bad. Conditions in the same exclusivity group replace each other
# (the 'practice' group: Practice Poor 6 / Practice Perfect 10/11).
CONDITION_NAMES = {
    1: "Night Owl", 2: "Slacker", 3: "Skin Outbreak", 4: "Slow Metabolism",
    5: "Migraine", 6: "Practice Poor", 7: "Fast Learner", 8: "Charming",
    9: "Hot Topic", 10: "Practice Perfect", 11: "Practice Perfect (◎)",
    12: "Under the Weather", 13: "Shining Brightly", 19: "Not Ready",
    20: "Legs of Glass", 21: "Ominous Portent",
}
COND_PRACTICE_POOR = 6
COND_PRACTICE_PERFECT = 10
# Mutually-exclusive groups (single_mode_chara_effect.effect_group_id 6): adding
# one member removes the others.
_COND_EXCLUSIVE_GROUP = {6: "practice", 10: "practice", 11: "practice"}


def conditions(chara_info: dict) -> list:
    return chara_info.setdefault("chara_effect_id_array", [])


def has_condition(chara_info: dict, cond_id: int) -> bool:
    return cond_id in (chara_info.get("chara_effect_id_array") or [])


def add_condition(chara_info: dict, cond_id: int) -> None:
    """Add a condition to chara_effect_id_array, replacing any exclusive-group
    peer (e.g. Practice Perfect overwrites Practice Poor)."""
    arr = conditions(chara_info)
    grp = _COND_EXCLUSIVE_GROUP.get(cond_id)
    if grp:
        arr[:] = [c for c in arr if _COND_EXCLUSIVE_GROUP.get(c) != grp]
    if cond_id not in arr:
        arr.append(cond_id)


def remove_condition(chara_info: dict, cond_id: int) -> None:
    arr = chara_info.get("chara_effect_id_array")
    if arr and cond_id in arr:
        arr.remove(cond_id)


# ============ Happy Meek versus duel ============
# Training in Happy Meek's versus facility fires this 3-choice event. Each choice
# is a stat; pick one, then a strength check vs Happy Meek (single_mode_npc,
# scaling with versus_level) decides win/lose. Win: stat +10, its cap +4, +30 SP,
# and the "Racing Spirit: <stat>" skill hint. Lose: stat +5, +15 SP. Then
# versus_level increments. Decoded from the real capture (UmaDumpy 145320).
DUEL_EVENT_ID = 102001
_DUEL_STORY_ID = 400001418
DUEL_CTX_KEY = "active_duel"          # on full_state: {facility, versus_level, stat_options}
VERSUS_LEVEL_KEY = "versus_level"     # on full_state

_STAT_IDX_TO_KEY = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
# stat index -> (Racing Spirit skill_id, its skill_tips group_id)
_RACING_SPIRIT = {1: (210091, 21009), 2: (210101, 21010), 3: (210111, 21011),
                  4: (210121, 21012), 5: (210131, 21013)}
# Happy Meek opponent stats by versus_level (single_mode_npc, chara 2001).
# Happy Meek's stats per duel level. Levels 1-2 are from the real captures
# (UmaDumpy 20260720_145320); 3-5 extend the level1->level2 delta linearly so
# late-career duels stay a real strength check instead of a free win.
_HAPPY_MEEK_STATS = {
    1: {"speed": 252, "stamina": 231, "power": 249, "guts": 220, "wiz": 228},
    2: {"speed": 403, "stamina": 354, "power": 398, "guts": 352, "wiz": 364},
}
for _lvl in (3, 4, 5):
    _HAPPY_MEEK_STATS[_lvl] = {
        _s: _HAPPY_MEEK_STATS[2][_s] + (_lvl - 2) * (_HAPPY_MEEK_STATS[2][_s] - _HAPPY_MEEK_STATS[1][_s])
        for _s in STAT_KEYS
    }


def happy_meek_stats(level: int) -> dict:
    """Happy Meek's five stats at a duel level (clamped to the table)."""
    return dict(_HAPPY_MEEK_STATS.get(
        level, _HAPPY_MEEK_STATS[max(_HAPPY_MEEK_STATS)]))


def duel_stat_options(versus_level: int) -> list:
    """The 3 stat indices offered this duel. Rotates by versus_level so across
    the (up to 5) duels you can reach all five stats."""
    order = [3, 1, 4, 2, 5]  # power, speed, guts, stamina, wiz (capture's turn-5 set first)
    start = (max(1, versus_level) - 1) % 5
    return [order[(start + i) % 5] for i in range(3)]


def duel_event_entry(stat_options: list, chara_info: dict | None = None,
                     versus_level: int = 1) -> dict:
    """The duel's unchecked_event_array entry (3 stat choices). chara_id is 0 --
    it's a scenario event, NOT a character event (verified against the real
    capture; chara_id 2001 made the choices non-selectable). The client commits a
    choice via get_choice_reward(event_id, choice_number == gain_select_id_index).

    Each option carries its odds icon in select_icon (see duel_icon) when
    chara_info is given -- previously every option was hardcoded to 2, so the
    player got no hint which stat they could actually win with."""
    def icon(s):
        return duel_icon(chara_info, s, versus_level) if chara_info else 2

    return {
        "event_id": DUEL_EVENT_ID, "chara_id": 0, "story_id": _DUEL_STORY_ID,
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            # select_index carries the stat too (== gain_select_id_index): the
            # client returns the chosen option's select_index as choice_number, so
            # all three MUST differ or every pick collapses to one stat (all
            # select_index=1 made every choice resolve to speed).
            "choice_array": [
                {"select_index": s, "receive_item_id": 0, "target_race_id": 0,
                 "gain_select_id_index": s, "select_icon": icon(s)}
                for s in stat_options
            ],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


# choice_array[].select_icon -- the odds hint the client draws on each duel
# option. Confirmed as the carrier from real captures of the duel event
# (102001 / story 400001418), which use values 1-4 on an ascending-quality
# scale (x < triangle < circle < double circle). We show three bands:
#   p < 25%  -> 1 (x)      25-75% -> 3 (circle)      >= 75% -> 4 (double circle)
DUEL_ICON_X, DUEL_ICON_CIRCLE, DUEL_ICON_DOUBLE = 1, 3, 4


_DUEL_SCALE = 100.0   # stat points that swing the duel from even to ~73%


def duel_win_probability(chara_info: dict, stat_index: int, versus_level: int) -> float:
    """Chance of winning this duel option -- the SAME number the roll uses, so
    the icon never lies about the odds.

    Driven by the stat DIFFERENCE, not the ratio. The ratio model
    (mine / (mine + theirs)) is scale-invariant, which gets the late game
    exactly backwards: a 150-point lead is worth p=0.61 against Meek's level-1
    252, but only p=0.54 against her level-5 850, so out-training her paid
    less and less and every duel stayed a coin flip no matter how strong the
    trainee got (live-reported as 'still too hard'). A logistic on the gap
    makes 150 points worth the same everywhere -- about 0.82 -- while an
    even matchup is still 50/50. Clamped so no duel is ever hopeless or free."""
    key = _STAT_IDX_TO_KEY.get(stat_index)
    if not key:
        return 0.0
    npc = _HAPPY_MEEK_STATS.get(versus_level, _HAPPY_MEEK_STATS[max(_HAPPY_MEEK_STATS)])
    gap = (chara_info.get(key, 0) or 0) - (npc.get(key, 0) or 0)
    return min(0.90, max(0.15, 1.0 / (1.0 + math.exp(-gap / _DUEL_SCALE))))


def duel_icon(chara_info: dict, stat_index: int, versus_level: int) -> int:
    p = duel_win_probability(chara_info, stat_index, versus_level)
    if p < 0.25:
        return DUEL_ICON_X
    if p < 0.75:
        return DUEL_ICON_CIRCLE
    return DUEL_ICON_DOUBLE


def _duel_won(chara_info: dict, stat_index: int, versus_level: int) -> bool:
    """Contest the player's dueled stat against Happy Meek's at this level.

    Odds-based, not a hard threshold: p = player/(player+meek), so equal
    stats is a coin flip and being behind still leaves a real chance (and
    being ahead isn't a certainty). The old `player >= meek` gate made the
    FIRST duel unwinnable in practice -- it wants ~250 in the chosen stat on
    a turn where a trainee has ~180-220, so every early duel was a scripted
    loss (live-reported: 'extremely difficult to win... only ever won once
    bc i went back in turns'). Clamped so no duel is ever hopeless or free."""
    return random.random() < duel_win_probability(chara_info, stat_index, versus_level)


# USER-SUPPLIED (2026-07-28) duel reward ranges. A win pays a RANGE, not a fixed
# amount: 10-25 to the dueled stat (previously a flat 10), and a loss pays 5-15
# (previously a flat 5). Cap +4 and the 30/15 SP were already right.
_DUEL_WIN_STAT_RANGE = (10, 25)
_DUEL_LOSE_STAT_RANGE = (5, 15)
_DUEL_WIN_SP = 30
_DUEL_LOSE_SP = 15


def _duel_gains(stat_index: int, won: bool, stat_up: int | None = None) -> list:
    """The gain_param_array for a duel outcome (display + what to apply).

    `stat_up` is the ALREADY-ROLLED amount, so the preview and the applied value
    can never disagree; without one (the preview before any roll) the midpoint
    of the range is shown."""
    if won:
        skill_id, _g = _RACING_SPIRIT[stat_index]
        if stat_up is None:
            stat_up = sum(_DUEL_WIN_STAT_RANGE) // 2
        return [
            {"display_id": 1, "effect_value_0": 50 + stat_index, "effect_value_1": 4, "effect_value_2": 0},  # +cap
            {"display_id": 1, "effect_value_0": stat_index, "effect_value_1": stat_up, "effect_value_2": 0},  # +stat
            {"display_id": 1, "effect_value_0": 30, "effect_value_1": _DUEL_WIN_SP, "effect_value_2": 0},     # +SP
            {"display_id": 6, "effect_value_0": skill_id, "effect_value_1": 1, "effect_value_2": 0},          # skill hint
        ]
    if stat_up is None:
        stat_up = sum(_DUEL_LOSE_STAT_RANGE) // 2
    return [
        {"display_id": 1, "effect_value_0": stat_index, "effect_value_1": stat_up, "effect_value_2": 0},
        {"display_id": 1, "effect_value_0": 30, "effect_value_1": _DUEL_LOSE_SP, "effect_value_2": 0},
    ]


def duel_preview(stat_options: list) -> list:
    """choice_reward_array shown when the duel event appears: each option's win
    and lose gains (the client displays both branches)."""
    arr = []
    for s in stat_options:
        arr.append({"select_index": s, "gain_param_array": _duel_gains(s, won=True)})
        arr.append({"select_index": s, "gain_param_array": _duel_gains(s, won=False)})
    return arr


def resolve_duel(chara_info: dict, stat_index: int, versus_level: int) -> dict:
    """Apply the duel reward for the chosen stat to chara_info (in place) and
    return the choice_reward_array + whether it was a win. Win: stat +10, cap +4,
    +30 SP, Racing Spirit hint. Lose: stat +5, +15 SP."""
    if stat_index not in _STAT_IDX_TO_KEY:
        stat_index = 1
    key = _STAT_IDX_TO_KEY[stat_index]
    won = _duel_won(chara_info, stat_index, versus_level)
    # Roll the amount FIRST so the reward screen shows what actually landed.
    stat_up = random.randint(*(_DUEL_WIN_STAT_RANGE if won else _DUEL_LOSE_STAT_RANGE))
    sp_up = _DUEL_WIN_SP if won else _DUEL_LOSE_SP
    gains = _duel_gains(stat_index, won, stat_up)
    cap_key = "max_wiz" if key == "wiz" else f"max_{key}"
    if won:
        chara_info[cap_key] = chara_info.get(cap_key, 1200) + 4
        _skill_id, group_id = _RACING_SPIRIT[stat_index]
        tips = chara_info.setdefault("skill_tips_array", [])
        if not any(t.get("group_id") == group_id for t in tips):
            tips.append({"group_id": group_id, "rarity": 1, "level": 1})
    chara_info[key] = min(chara_info.get(key, 0) + stat_up, chara_info.get(cap_key, 9999))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + sp_up
    return {"won": won, "choice_reward_array": [{"select_index": stat_index, "gain_param_array": gains}]}
