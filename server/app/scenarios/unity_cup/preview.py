
from __future__ import annotations

import copy
import logging
import random

from ... import event_engine, master_data, training_formula
from . import impl as unity_cup

log = logging.getLogger("uma-server")

PREVIEW_KEY = "unity_cup_partner_preview"

AWAY_CHANCE = 0.10
GUIDE_CHANCE = 0.50

MAX_PER_FACILITY = 5

DECK_TARGET_MAX = 6


def scout_placements(full_state: dict, chara_info: dict, turn,
                     occupied: dict = None) -> dict:
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
            cmd = min(unity_cup.TRAINING_COMMAND_IDS,
                      key=lambda c: (taken.get(c, 0), c))
            taken[cmd] = taken.get(cmd, 0) + 1
            out.setdefault(cmd, []).append(member["target_id"])
    return out


def _raises_gauge(st: dict, turn, target_id) -> bool:
    return random.Random(
        "%s:guide:%s:%s" % (st.get("seed", 0), turn, target_id)
    ).random() < GUIDE_CHANCE


def command_info(full_state: dict, chara_info: dict, home_commands):
    st = unity_cup.state(full_state)
    turn = chara_info.get("turn")
    cache = full_state.get(PREVIEW_KEY)
    if isinstance(cache, dict) and cache.get("turn") == turn:
        return copy.deepcopy(cache.get("commands") or []), False

    unity_cup.ensure_roster(st, turn, chara_info)
    members = unity_cup.active_members(st)
    by_id = {c.get("command_id"): c for c in (home_commands or [])
             if isinstance(c, dict)}

    if not members:
        return [_empty(cmd) for cmd in unity_cup.TRAINING_COMMAND_IDS], False

    by_target = {m["target_id"]: m for m in members}
    commands, placements = [], {}
    for cmd in unity_cup.TRAINING_COMMAND_IDS:
        entry = by_id.get(cmd)
        served = cmd
        if entry is None:
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
        commands.append({
            "command_type": 1,
            "command_id": served,
            "guide_event_partner_array": guide,
            "soul_event_partner_array": soul,
            "sp_soul_event_partner_array": sp_soul,
            "params_inc_dec_info_array": _bonus_params(cmd, taking, entry),
        })
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


_SPECIAL_MAIN = {2: (2, 0, 1), 3: (4, 1, 3), 4: (6, 3, 5), 5: (10, 5, 7)}
_SPECIAL_GUTS = {2: (2, 0, 0, 1), 3: (3, 1, 1, 3), 4: (5, 2, 2, 5),
                 5: (10, 3, 3, 7)}
_SPECIAL_WIZ = {2: (1, 0, 0), 3: (2, 0, 2), 4: (4, 1, 4), 5: (6, 2, 6)}

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

TARGET_TYPE_ENERGY = 10
_WIZ_BURST_ENERGY = 5


def _special_training_gains(stat: str, flames: int) -> dict:
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
    if not here:
        return []
    stat = unity_cup.COMMAND_STAT.get(
        unity_cup.CAMP_BASE.get(command_id, command_id))
    if stat is None:
        return []

    gains = _special_training_gains(stat, len(here))
    linked = sum(1 for m in here if _is_scenario_linked(m))
    if linked and gains:
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
    if payload.get("command_type") != 1:
        return
    command_id = payload.get("command_id")
    base = unity_cup.CAMP_BASE.get(command_id, command_id)
    if base not in unity_cup.TRAINING_COMMAND_IDS:
        return
    cache = full_state.get(PREVIEW_KEY) or {}
    if cache.get("turn") != payload.get("current_turn"):
        return
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

    unity_cup.count_unity_training(st, partners)

    full_state.pop(PREVIEW_KEY, None)

    if soul_tips or sp_soul_tips:
        st["command_result"] = {"skill_tips_array": None,
                                "soul_skill_tips_array": soul_tips or None,
                                "sp_soul_skill_tips_array": sp_soul_tips or None}


_BURST_TIP_BASE_LEVEL = 2
_SCENARIO_LINK_TIP_BONUS = 1

IGNITED_SPIRIT = {"speed": 210012, "stamina": 210022, "power": 210032,
                  "guts": 210042, "wiz": 210052}
_EXTREME_TIP_BASE_LEVEL = 1
_MAX_TIP_LEVEL = 5


def _member_card(member: dict) -> int:
    return int(member.get("support_card_id")
               or unity_cup.scout_support_card(member.get("scout_id")) or 0)


def _hint_bonus(support_card_id: int) -> int:
    if not support_card_id:
        return 0
    return int(round(training_formula.master_effect(
        support_card_id, training_formula.EFFECT_HINT_LEVEL)))


def _is_scenario_linked(member: dict) -> bool:
    if int(member.get("target_id") or 0) > DECK_TARGET_MAX:
        return False
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
    card_id = _member_card(member)
    bonus = _hint_bonus(card_id)
    learned = {s.get("skill_id") for s in (chara_info or {}).get("skill_array") or ()}
    tips = []

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
    level = max(1, int(level))
    event_engine._apply_skill_hint(chara_info, skill_id, level)
    group_id, rarity = master_data.skill_tip_key(skill_id)
    return {"group_id": group_id, "rarity": rarity,
            "level": _tip_level(chara_info, skill_id) or level}
