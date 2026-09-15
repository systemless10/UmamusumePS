"""UNITY CUP (scenario 2) -- the per-facility TEAM overlay, and banking it.

The scenario's command_info() and award_training_gains() hooks.

command_info_array here is the exact parallel of Grand Live's token preview: it
shadows the five training commands with the teammates standing in each one and
what they will contribute. The client renders the teammate portraits, the soul
flame on anyone about to detonate, and the bonus-stat badge from it.

    {"command_type": 1, "command_id": 103,
     "guide_event_partner_array":   [5, 6, 1019],   # joining this training
     "soul_event_partner_array":    [],             # about to Explode
     "sp_soul_event_partner_array": [1010],         # about to SpExplode
     "params_inc_dec_info_array":   [{target_type, value}, ...]}

THE ROLL IS CACHED PER TURN, for the same reason Grand Live's is: the client is
shown this preview and then trains against it. Re-rolling on the next response
would place different teammates in the facility than the ones the player was
promised -- and here that is worse than a cosmetic drift, because the preview
is also what decides who banks growth and whose gauge ticks.
"""

from __future__ import annotations

import copy
import logging
import random

from ... import event_engine, master_data, training_formula
from . import impl as unity_cup

log = logging.getLogger("uma-server")

PREVIEW_KEY = "unity_cup_partner_preview"

# WHO SHOWS UP, and who actually raises their gauge. Both measured off
# captures/bot/20260905_152744_icarus, over 201 turns and four roster sizes.
#
#   present  84% / 97% / 92% / 87% of the roster at 4 / 6 / 10 / 13 scouts
#   guides   51% of present deck members (559/1095), 46% of present scouts
#            (652/1403) -- the same coin either way, so ONE constant
#
# The distinction matters and used to be missing entirely: a teammate standing
# in a facility is NOT necessarily raising their gauge there. Capture 0187 has
# 1056 in facility 103's training_partner_array with an EMPTY
# guide_event_partner_array and an empty bonus, next to 3 and 6 in 102 who are
# guides and do carry one. Present is the portrait; guide is the flame.
#
# The old model rolled 2-5 placements TOTAL across all five facilities. That is
# roughly a fifth of what the game does, and it is why the flame almost never
# appeared on anybody (user-reported 2026-09-05).
AWAY_CHANCE = 0.10          # a teammate who sits the turn out entirely
GUIDE_CHANCE = 0.50         # of those present, who raises their gauge here

# The client draws at most five bodies in a facility -- the same cap
# single_mode_team._MAX_FACILITY_PARTNERS applies to cards, NPCs and Grand
# Live's supporters. The capture's per-facility partner histogram tops out at
# exactly 5, never 6.
MAX_PER_FACILITY = 5

# Deck teammates carry their DECK POSITION as target_id, so ids 1-6 are both a
# support card and a teammate. That is not a collision to work around, it is
# the mechanic: a deck teammate is standing wherever her card is standing, and
# is therefore never placed by the roll below.
DECK_TARGET_MAX = 6


def scout_placements(full_state: dict, chara_info: dict, turn,
                     occupied: dict = None) -> dict:
    """{facility command_id: [target_id, ...]} -- the SCOUTED teammates only.

    Feeds home_info's training_partner_array through the scenario's
    partner_placements hook, which is the array the client draws portraits
    from. Deck teammates are deliberately absent: they ride their own support
    card's placement, so rolling them here would draw them twice.

    Deterministic in (seed, turn, target_id) because it is computed TWICE per
    response -- once building home_info and once building the overlay -- and
    the two must agree body for body."""
    if turn is None:
        return {}
    st = unity_cup.state(full_state)
    unity_cup.ensure_roster(st, turn, chara_info)
    members = [m for m in unity_cup.active_members(st)
               if int(m.get("target_id") or 0) > DECK_TARGET_MAX]
    if not members:
        return {}
    taken = dict(occupied or {})
    out = {}
    for member in sorted(members, key=lambda m: int(m["target_id"])):
        rng = random.Random(
            "%s:place:%s:%s" % (st.get("seed", 0), turn, member["target_id"]))
        # A full gauge never sits a turn out. The capture has no teammate who
        # reached soul 5 and then failed to fire, so the burst is not something
        # the away roll gets to cancel.
        if not unity_cup.burst_ready(member) and rng.random() < AWAY_CHANCE:
            continue
        stat = unity_cup.member_soul_type(member)
        preferred = next((c for c, v in unity_cup.COMMAND_STAT.items() if v == stat),
                         unity_cup.TRAINING_COMMAND_IDS[0])
        rest = [c for c in unity_cup.TRAINING_COMMAND_IDS if c != preferred]
        rng.shuffle(rest)
        order = ([preferred] + rest) if rng.random() < 0.45 else (rest + [preferred])
        for cmd in order:
            if taken.get(cmd, 0) < MAX_PER_FACILITY:
                taken[cmd] = taken.get(cmd, 0) + 1
                out.setdefault(cmd, []).append(member["target_id"])
                break
        else:
            # EVERY facility already full. Standing this teammate down is a
            # second, invisible away-roll on top of AWAY_CHANCE, and the
            # capture says there is no such thing: across 265 real turns,
            # exactly 0.903 of the state-1 roster is placed each turn, which is
            # the 10% away roll and nothing else. The teammates who fall
            # through here are the LAST ones in target_id order, so the drop is
            # not even random -- the same few never train, never fill a gauge
            # and never burst. Put her in the emptiest facility instead: the
            # cap is what the client can DRAW, and one extra body over it is a
            # far smaller divergence than a teammate who sits out the career.
            cmd = min(unity_cup.TRAINING_COMMAND_IDS,
                      key=lambda c: (taken.get(c, 0), c))
            taken[cmd] = taken.get(cmd, 0) + 1
            out.setdefault(cmd, []).append(member["target_id"])
    return out


def _raises_gauge(st: dict, turn, target_id) -> bool:
    """Whether this teammate carries the gauge flame in the facility she is
    standing in this turn. Deterministic for the same reason placement is."""
    return random.Random(
        "%s:guide:%s:%s" % (st.get("seed", 0), turn, target_id)
    ).random() < GUIDE_CHANCE


def command_info(full_state: dict, chara_info: dict, home_commands):
    """team_data_set.command_info_array -- who is in each facility this turn.

    Returns (commands, rolled); `rolled` tells the caller a NEW roll was made
    and therefore needs persisting.

    WHO IS HERE IS READ OFF home_info, never rolled again. The facility's
    training_partner_array is what the client actually draws, so intersecting
    it with the roster is the one derivation that cannot disagree with the
    screen. Rolling it independently is what produced the user-reported desync
    of 2026-09-05: a bonus badge on a facility with nobody standing in it."""
    st = unity_cup.state(full_state)
    turn = chara_info.get("turn")
    cache = full_state.get(PREVIEW_KEY)
    if isinstance(cache, dict) and cache.get("turn") == turn:
        return copy.deepcopy(cache.get("commands") or []), False

    unity_cup.ensure_roster(st, turn, chara_info)
    members = unity_cup.active_members(st)
    by_id = {c.get("command_id"): c for c in (home_commands or [])
             if isinstance(c, dict)}

    # Before the team exists there is nothing to overlay, but the array must
    # still carry all five commands -- the capture serves five empty entries on
    # turns 1-2 rather than an empty array, and the client reads the length.
    if not members:
        return [_empty(cmd) for cmd in unity_cup.TRAINING_COMMAND_IDS], False

    by_target = {m["target_id"]: m for m in members}
    commands, placements = [], {}
    for cmd in unity_cup.TRAINING_COMMAND_IDS:
        entry = by_id.get(cmd)
        served = cmd
        if entry is None:                        # summer camp serves 601-605
            camp_id = _camp_id(cmd)
            if camp_id in by_id:
                entry, served = by_id[camp_id], camp_id
        here = [by_target[t]
                for t in ((entry or {}).get("training_partner_array") or [])
                if t in by_target]
        guide, soul, sp_soul, taking = [], [], [], []
        for member in here:
            tier = unity_cup.burst_ready(member)
            if tier == unity_cup.SOUL_EXPLODED:
                soul.append(member["target_id"])
                taking.append(member)
            elif tier == unity_cup.SOUL_SP_EXPLODED:
                sp_soul.append(member["target_id"])
                taking.append(member)
            elif _raises_gauge(st, turn, member["target_id"]):
                guide.append(member["target_id"])
                taking.append(member)
            # else: present and drawn, but contributes nothing this turn.
        commands.append({
            "command_type": 1,
            "command_id": served,
            "guide_event_partner_array": guide,
            "soul_event_partner_array": soul,
            "sp_soul_event_partner_array": sp_soul,
            "params_inc_dec_info_array": _bonus_params(cmd, taking, entry),
        })
        # ONLY the participants are banked. A teammate who is merely standing
        # here banks no growth and no gauge -- which is exactly why her
        # facility shows no bonus for her.
        placements[str(cmd)] = [m["target_id"] for m in taking]

    full_state[PREVIEW_KEY] = {"turn": turn, "commands": copy.deepcopy(commands),
                               "placements": placements}
    return commands, True


def _empty(command_id: int) -> dict:
    return {"command_type": 1, "command_id": command_id,
            "guide_event_partner_array": [], "soul_event_partner_array": [],
            "sp_soul_event_partner_array": [], "params_inc_dec_info_array": []}


def _camp_id(command_id: int) -> int:
    for camp, base in unity_cup.CAMP_BASE.items():
        if base == command_id:
            return camp
    return command_id


# ---------------------------------------------------- what the team pays --
# GameTora's Unity Cup tables, verbatim (docs/Unity Cup Scenario _ GameTora.html,
# "Trainee Stat Gains", "Spirit Burst Bonus Values", "Extreme Spirit Burst").
#
# THE OLD NUMBERS WERE INVENTED. _bonus_params used to pay a flat 2/1/+1sp per
# participating teammate, 7/+4sp per burst and 12/+8sp per Extreme burst --
# constants fitted by eye to one capture's badge. The real rule is two
# independent pieces that ADD:
#
#   1. Special Training, ONE lookup on the number of white flames in the
#      facility (not per teammate, and nothing at all below two flames), and
#   2. each bursting teammate's own vector, from her own table.
#
# Bursts count as flames for (1) -- GameTora: "Spirit Bursts also count as
# Special Training" -- so a lone burst still pays no Special Training share.

# flames -> (primary, secondary, skill points), for the speed/stamina/power
# facilities. Below 2 flames the trainee gets nothing.
_SPECIAL_MAIN = {2: (2, 0, 1), 3: (4, 1, 3), 4: (6, 3, 5), 5: (10, 5, 7)}
# flames -> (guts, speed, power, skill points)
_SPECIAL_GUTS = {2: (2, 0, 0, 1), 3: (4, 1, 1, 3), 4: (6, 2, 2, 5),
                 5: (10, 3, 3, 7)}
# flames -> (wiz, speed, skill points)
_SPECIAL_WIZ = {2: (1, 0, 0), 3: (2, 0, 2), 4: (4, 1, 4), 5: (6, 2, 6)}

# facility stat -> {stat: gain} for a Spirit Burst, and the scenario-linked
# variant. "skill_point" rides in the same dict; the caller splits it out.
_BURST = {
    "speed":   {"speed": 15, "power": 7, "skill_point": 5},
    "stamina": {"stamina": 15, "guts": 7, "skill_point": 5},
    "power":   {"stamina": 7, "power": 15, "skill_point": 5},
    "guts":    {"speed": 3, "power": 3, "guts": 15, "skill_point": 5},
    "wiz":     {"speed": 2, "wiz": 15, "skill_point": 5},
}
_BURST_LINKED = {
    "speed":   {"speed": 20, "power": 10, "skill_point": 10},
    "stamina": {"stamina": 20, "guts": 10, "skill_point": 10},
    "power":   {"stamina": 10, "power": 20, "skill_point": 10},
    "guts":    {"speed": 5, "power": 5, "guts": 20, "skill_point": 10},
    "wiz":     {"speed": 5, "wiz": 20, "skill_point": 10},
}
_EXTREME = {
    "speed":   {"speed": 20, "power": 10, "skill_point": 15},
    "stamina": {"stamina": 20, "guts": 10, "skill_point": 15},
    "power":   {"stamina": 10, "power": 20, "skill_point": 15},
    "guts":    {"speed": 5, "power": 5, "guts": 20, "skill_point": 15},
    "wiz":     {"speed": 5, "wiz": 15, "skill_point": 15},
}
_EXTREME_LINKED = {
    "speed":   {"speed": 25, "power": 15, "skill_point": 20},
    "stamina": {"stamina": 25, "guts": 15, "skill_point": 20},
    "power":   {"stamina": 15, "power": 25, "skill_point": 20},
    "guts":    {"speed": 8, "power": 8, "guts": 25, "skill_point": 20},
    "wiz":     {"speed": 8, "wiz": 25, "skill_point": 20},
}

# A Spirit Burst on the WIT facility hands back 5 more energy. target_type 10
# is the energy row of params_inc_dec_info_array (single_mode_team serves the
# facility's own cost there and _merge_preview_params sums the two).
TARGET_TYPE_ENERGY = 10
_WIZ_BURST_ENERGY = 5


def _special_training_gains(stat: str, flames: int) -> dict:
    """The trainee's Special Training share for `flames` white flames."""
    flames = min(int(flames or 0), 5)
    if flames < 2:
        return {}
    if stat == "guts":
        guts, speed, power, sp = _SPECIAL_GUTS[flames]
        return {"guts": guts, "speed": speed, "power": power, "skill_point": sp}
    if stat == "wiz":
        wiz, speed, sp = _SPECIAL_WIZ[flames]
        return {"wiz": wiz, "speed": speed, "skill_point": sp}
    primary, secondary, sp = _SPECIAL_MAIN[flames]
    other = unity_cup.SECONDARY_STAT.get(stat)
    return {stat: primary, other: secondary, "skill_point": sp}


def _bonus_params(command_id: int, here: list, entry) -> list:
    """The BONUS-ONLY breakdown this facility's teammates contribute, in
    home_info's own target_type encoding.

    Same rule Grand Live's badge follows: home_info already carries the
    combined total, so what belongs here is only the scenario's own share --
    the client cannot un-mix the total back into "how much was the team".

    Bursting teammates dominate it, which is what makes the soul flame worth
    chasing: a plain four-flame speed facility pays {spd 6, pow 3, skillpt 5},
    and one Extreme burst in it adds {spd 20, pow 10, skillpt 15} on top.

    SCENARIO-LINKED CARDS pay +1 on every stat this facility's SPECIAL TRAINING
    already gives, once per linked participant, and swap their own burst vector
    for the linked table. The two are counted separately on purpose (GameTora's
    own note): a stat a burst gives but Special Training does not is NOT
    bumped."""
    if not here:
        return []
    stat = unity_cup.COMMAND_STAT.get(
        unity_cup.CAMP_BASE.get(command_id, command_id))
    if stat is None:
        return []

    gains = _special_training_gains(stat, len(here))
    linked = sum(1 for m in here if _is_scenario_linked(m))
    if linked and gains:
        # Only the stats this facility actually pays. A zero row stays zero:
        # GameTora's worked example lifts +2 guts to +3 guts / +2 spd / +2 pow
        # only because the third body had already turned speed and power on.
        for key, value in list(gains.items()):
            if value:
                gains[key] = value + linked

    energy = 0
    for member in here:
        tier = unity_cup.burst_ready(member)
        if tier not in (unity_cup.SOUL_EXPLODED, unity_cup.SOUL_SP_EXPLODED):
            continue
        extreme = tier == unity_cup.SOUL_SP_EXPLODED
        if _is_scenario_linked(member):
            table = _EXTREME_LINKED if extreme else _BURST_LINKED
        else:
            table = _EXTREME if extreme else _BURST
        for key, value in table[stat].items():
            gains[key] = gains.get(key, 0) + value
        if stat == "wiz":
            energy += _WIZ_BURST_ENERGY

    params = [{"target_type": unity_cup.TARGET_TYPE[key], "value": int(value)}
              for key, value in gains.items()
              if key in unity_cup.TARGET_TYPE and value]
    if gains.get("skill_point"):
        params.append({"target_type": unity_cup.TARGET_TYPE_SKILL_POINT,
                       "value": int(gains["skill_point"])})
    if energy:
        params.append({"target_type": TARGET_TYPE_ENERGY, "value": energy})
    return params


def training_failure_override(full_state: dict, chara_info: dict, command_id,
                              partner_ids):
    """0 when a teammate in this facility is about to SpExplode, else None.

    THE PURPLE BURST ONLY. Capture-confirmed 157/157 facility-showings across
    two runs (min = max = 0), while the blue burst leaves the rate alone -- so
    this keys on the SpExploded tier, not on "any burst".

    Read straight from the roster rather than from the cached overlay: this
    runs while home_info is being built, which is BEFORE the overlay for the
    same turn exists."""
    if not partner_ids:
        return None
    st = unity_cup.state(full_state)
    ids = set(partner_ids)
    for member in unity_cup.active_members(st):
        if member["target_id"] in ids and \
                unity_cup.burst_ready(member) == unity_cup.SOUL_SP_EXPLODED:
            return 0
    return None


def training_award_bonus(full_state: dict, chara_info: dict, command_id) -> list:
    """The teammate bonus the client is already displaying for this facility,
    handed back so the server actually pays it.

    Read out of the SAME cached preview the client was shown -- never
    recomputed -- so the number awarded is the number on the button."""
    base = unity_cup.CAMP_BASE.get(command_id, command_id)
    if base not in unity_cup.TRAINING_COMMAND_IDS:
        return []
    cache = full_state.get(PREVIEW_KEY) or {}
    if cache.get("turn") != (chara_info or {}).get("turn"):
        return []
    for entry in cache.get("commands") or ():
        if entry.get("command_id") in (command_id, base):
            return [dict(p) for p in entry.get("params_inc_dec_info_array") or ()]
    return []


def award_training_gains(full_state: dict, chara_info: dict, payload: dict) -> None:
    """Bank the team's side of a real training action.

    Pays against the preview the client was actually SHOWN, never a fresh roll
    -- the same contract Grand Live's token banking has, and for a stronger
    reason: the preview already told the player which teammates were about to
    detonate, so re-rolling here would fire a different set.

    Only command_type 1 on a training facility pays. Rest, outings, the
    infirmary and races place no teammates and bank nothing."""
    if payload.get("command_type") != 1:
        return
    command_id = payload.get("command_id")
    base = unity_cup.CAMP_BASE.get(command_id, command_id)
    if base not in unity_cup.TRAINING_COMMAND_IDS:
        return
    cache = full_state.get(PREVIEW_KEY) or {}
    if cache.get("turn") != payload.get("current_turn"):
        return                       # no preview was shown this turn
    target_ids = (cache.get("placements") or {}).get(str(base)) or \
        (cache.get("placements") or {}).get(str(command_id)) or []
    if not target_ids:
        return

    st = unity_cup.state(full_state)
    rng = random.Random(f"{st.get('seed', 0)}:award:{payload.get('current_turn')}:{command_id}")
    soul_tips, sp_soul_tips = [], []
    partners = 0
    for target_id in target_ids:
        member = unity_cup.member_by_target(st, target_id)
        if member is None:
            continue
        partners += 1
        tier = unity_cup.burst_ready(member)
        if tier is not None:
            gains = unity_cup.fire_burst(member, tier)
            log.info("unity cup: teammate %s burst tier %s -> %s",
                     member["chara_id"], tier, gains)
            # Counted for the career, not for the turn: "Team Zenith Declares
            # War" scales its reward off the TOTAL number of bursts, and the
            # elite team on race 4 requires at least one Extreme.
            unity_cup.count_burst(st, tier == unity_cup.SOUL_SP_EXPLODED)
            tips = _burst_skill_tips(
                member, rng, chara_info, unity_cup.COMMAND_STAT.get(base, "speed"),
                tier == unity_cup.SOUL_SP_EXPLODED)
            if tips:
                (sp_soul_tips if tier == unity_cup.SOUL_SP_EXPLODED
                 else soul_tips).append(
                    {"training_partner_id": member["target_id"],
                     "skill_tips_array": tips})
        else:
            capped: list = []
            unity_cup.grow_member(member, base, rng, capped)
            unity_cup.record_capped(st, member["target_id"], capped)
            unity_cup.advance_soul(member, rng)
        st["guide_count"] = int(st.get("guide_count") or 0) + 1

    # THIS training was a Unity Training: teammates stood on it and were just
    # paid. Counted once for the facility, with the number of teammates on it
    # -- epithets 152/153/155 (see impl.count_unity_training).
    unity_cup.count_unity_training(st, partners)

    # THE PREVIEW THAT WAS JUST PAID IS NOW STALE. It still lists the teammates
    # who were ABOUT to detonate, and it is rebuilt from the same cache on the
    # very next response -- so the training-result screen would be served an
    # overlay still flagging a burst that has already fired, while the
    # evaluation row underneath it already reads soul_event_state 1. Dropping
    # it here forces the next attach() to rebuild against the post-burst state.
    #
    # Safe to drop only AFTER the payout above: the cache is the contract for
    # what this training pays, and it has now been honoured.
    full_state.pop(PREVIEW_KEY, None)

    # command_result is consumed by the very next build_team_data_set (it is
    # popped there): it belongs to THIS response only, the same way the client
    # renders the hint cards once off the training result screen.
    if soul_tips or sp_soul_tips:
        st["command_result"] = {"skill_tips_array": None,
                                "soul_skill_tips_array": soul_tips or None,
                                "sp_soul_skill_tips_array": sp_soul_tips or None}


# A burst hands the trainee a skill hint, and it comes out of THE BURSTING
# TEAMMATE'S SUPPORT CARD HINT POOL -- her R card when she is not in the deck
# (user-stated 2026-09-06, and GameTora's post-update rules say the same). The
# old version drew a group id out of member["skills"], which is the teammate's
# RACE skill list, so the tip pointed at whatever she happened to run with.
#
# LEVEL = the card's Hint Lv. Bonus + 2, so never below 2, and one higher again
# for a scenario-linked card.
_BURST_TIP_BASE_LEVEL = 2
_SCENARIO_LINK_TIP_BONUS = 1

# An Extreme burst ALSO teaches the Ignited Spirit skill of the facility it
# fired on -- the white half of each Burning/Ignited pair (rarity 1; the gold
# Burning half comes from "Team Zenith Declares War"). Level 1 plus the card's
# hint bonus, and if that one is maxed or already learned, a different Ignited
# skill instead.
IGNITED_SPIRIT = {"speed": 210012, "stamina": 210022, "power": 210032,
                  "guts": 210042, "wiz": 210052}
_EXTREME_TIP_BASE_LEVEL = 1
_MAX_TIP_LEVEL = 5


def _member_card(member: dict) -> int:
    """The support card a teammate's hints come from: her deck card if she is
    in the deck, otherwise the R card her scout row names."""
    return int(member.get("support_card_id")
               or unity_cup.scout_support_card(member.get("scout_id")) or 0)


def _hint_bonus(support_card_id: int) -> int:
    if not support_card_id:
        return 0
    return int(round(training_formula.master_effect(
        support_card_id, training_formula.EFFECT_HINT_LEVEL)))


def _is_scenario_linked(member: dict) -> bool:
    return int(member.get("chara_id") or 0) in {
        int(unity_cup.scout_pool().get(s, {}).get("chara_id") or 0)
        for s in unity_cup.special_charas()}


def _tip_level(chara_info: dict, skill_id: int) -> int:
    group_id, rarity = master_data.skill_tip_key(skill_id)
    for tip in (chara_info or {}).get("skill_tips_array") or ():
        if tip.get("group_id") == group_id and tip.get("rarity") == rarity:
            return int(tip.get("level") or 0)
    return 0


def _aptitude_hint(chara_info: dict, rng: random.Random, learned: set):
    """The documented fallback when a card's pool is spent: "a random hint based
    on your trainee's A aptitudes".

    APPROXIMATE -- the trainee's own learnable pool, whites only, is the stand-in
    for that aptitude-keyed pool. Nothing in master.mdb maps an aptitude to a
    skill list directly, and no capture pins the real one down."""
    card_id = int((chara_info or {}).get("card_id") or 0)
    if not card_id:
        return None
    rows = master_data.query(
        "SELECT s.id FROM available_skill_set a JOIN skill_data s "
        "ON s.id = a.skill_id WHERE a.available_skill_set_id=? AND s.rarity=1",
        (card_id,))
    pool = [int(r["id"]) for r in rows if int(r["id"]) not in learned]
    return pool[rng.randrange(len(pool))] if pool else None


def _burst_skill_tips(member: dict, rng: random.Random, chara_info: dict,
                      stat: str, extreme: bool) -> list:
    """The tips ONE bursting teammate hands over, applied to chara_info as they
    are built so the hint is really granted and not merely displayed."""
    card_id = _member_card(member)
    bonus = _hint_bonus(card_id)
    learned = {s.get("skill_id") for s in (chara_info or {}).get("skill_array") or ()}
    tips = []

    # THE TWO BURSTS TEACH DIFFERENT THINGS, and only one of them draws from
    # the card. A blue (ordinary) burst rolls the support card's own hint pool;
    # a purple (Extreme) burst teaches the facility's Ignited Spirit skill and
    # NOTHING ELSE -- it does not also roll the pool (user-reported
    # 2026-09-06). Handing out both was giving every purple two hints.
    if not extreme:
        level = _BURST_TIP_BASE_LEVEL + bonus
        if _is_scenario_linked(member):
            level += _SCENARIO_LINK_TIP_BONUS
        pool = [sid for sid in event_engine.card_hint_skills(card_id)
                if sid not in learned and _tip_level(chara_info, sid) < _MAX_TIP_LEVEL]
        skill_id = (pool[rng.randrange(len(pool))] if pool
                    else _aptitude_hint(chara_info, rng, learned))
        if skill_id:
            tips.append(_grant(chara_info, skill_id, level))
    else:
        want = IGNITED_SPIRIT.get(stat)
        spent = [s for s in IGNITED_SPIRIT.values()
                 if s in learned or _tip_level(chara_info, s) >= _MAX_TIP_LEVEL]
        if want in spent:
            left = [s for s in IGNITED_SPIRIT.values() if s not in spent]
            want = left[rng.randrange(len(left))] if left else None
        if want:
            tips.append(_grant(chara_info, want, _EXTREME_TIP_BASE_LEVEL + bonus))
    return tips


def _grant(chara_info: dict, skill_id: int, level: int) -> dict:
    """Raise the hint and return the {group_id, rarity, level} the client draws."""
    level = max(1, int(level))
    event_engine._apply_skill_hint(chara_info, skill_id, level)
    group_id, rarity = master_data.skill_tip_key(skill_id)
    return {"group_id": group_id, "rarity": rarity,
            "level": _tip_level(chara_info, skill_id) or level}
