"""GRAND LIVE (scenario 3) -- the per-facility TOKEN preview, and banking it.

The scenario's command_info() and award_training_gains() hooks. Lifted out of
handlers/single_mode_team.py, where they sat among the shared career code they
borrow from; that code is reached through _smt(), a deferred import, because
single_mode_team imports the scenario registry.
"""

from __future__ import annotations

import copy
import logging
import random

from ... import training_formula
from . import impl as grand_live

log = logging.getLogger("uma-server")

# full_state key for the cached per-turn preview. The client is shown this roll
# and then trains against it, so exec_command banks THIS, never a fresh roll.
PREVIEW_KEY = "grand_live_token_preview"


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def command_info(full_state: dict, chara_info: dict, home_commands):
    """live_data_set.command_info_array -- the per-facility TOKEN preview, the
    exact parallel of home_info's per-facility STAT preview.

    The roll is CACHED per turn: the client shows this preview and then trains
    against it, so re-rolling the colour on the next response would mean the
    player banks a different token than the one they were promised.

    Returns (commands, rolled) -- `rolled` tells the caller a NEW roll was made
    and therefore needs persisting."""
    smt = _smt()
    turn = chara_info.get("turn")
    cache = full_state.get(PREVIEW_KEY)
    if isinstance(cache, dict) and cache.get("turn") == turn:
        return copy.deepcopy(cache.get("commands") or []), False

    rng = random.Random(f"{chara_info.get('single_mode_chara_id')}:{turn}")
    by_id = {c.get("command_id"): c for c in (home_commands or [])
             if isinstance(c, dict)}
    # Before the lessons unlock the facilities show NO token icons at all --
    # 0 of 5 through turn 4 in the capture, 5 of 5 from turn 5, i.e. the turn
    # after "Bring Back the Grand Concert!". Showing them early advertised a
    # currency the player can't spend yet (live-reported).
    if not grand_live.tokens_visible(full_state, turn):
        commands = [{"command_type": 1, "command_id": cmd,
                     "performance_inc_dec_info_array": [],
                     "params_inc_dec_info_array": []}
                    for cmd in grand_live.TRAINING_COMMAND_IDS]
        return commands, False
    commands = []
    for cmd in grand_live.TRAINING_COMMAND_IDS:
        entry = by_id.get(cmd)
        served = cmd
        if entry is None:                       # summer camp serves 601-605
            camp_id = smt._CAMP_ID_BY_BASE.get(cmd)
            entry = by_id.get(camp_id)
            if entry is not None:
                served = camp_id
        partners = list((entry or {}).get("training_partner_array") or [])
        rainbow = _is_rainbow_training(chara_info, partners, cmd)
        # Facility level feeds the token formula's base (S + F), so read the
        # level the screen is actually showing rather than assuming 1.
        level = (entry or {}).get("level") or 1
        # THE BONUS-ONLY BREAKDOWN, not the combined total (home_info already
        # carries that). Real capture proof (2026-08-16, a fresh real-account
        # training screen where the client actually rendered the floating
        # music-note badge): this facility's own params_inc_dec_info_array
        # here held ONLY the training_bonus contribution for whichever of its
        # stats has one active -- e.g. a facility whose home_info total was
        # Speed +11 carried just {target_type:1, value:1} here, matching
        # training_bonus_array's speed entry exactly, not the +11. Hardcoding
        # this to [] (as it always was) starved the client of the ONE signal
        # it needs to draw that badge at all -- the combined total alone
        # can't be un-mixed back into "how much was bonus" client-side.
        # target_type here follows home_info's own encoding (30 for skill
        # points), so translate to training_bonus's native master_bonus index
        # (6 for skill points) only for the lookup, per _MASTER_BONUS_INDEX.
        #
        # Scaled by the SAME friendship multiplier training_formula applies to
        # this exact song bonus (user-reported 2026-08-16: the badge showed
        # the raw bonus even when a rainbow was active, under-reporting what
        # training actually grants). Reuses training_formula's own helper
        # rather than recomputing the card-type/bond check here, so this can
        # never drift from what calculate_training_gain does.
        bonus_params = []
        gl_bonus = grand_live.state(full_state).get("training_bonus") or {}
        if any(gl_bonus.values()):
            pos_to_sid = {c["position"]: c["support_card_id"]
                         for c in chara_info.get("support_card_array", [])}
            in_facility = [(pos_to_sid.get(p), smt._bond_of(chara_info, p)) for p in partners]
            fte = training_formula.friendship_multiplier(
                in_facility, served, grand_live.friendship_bonus_pct(full_state),
                pure_passion_cards=smt._pure_passion_cards(chara_info))
            for p in (entry or {}).get("params_inc_dec_info_array") or []:
                # p.get("value") guards against a stat that's fully capped --
                # home_info now carries those at value 0 (rather than omitted)
                # so the result screen can show "in superb form" there, but
                # that 0 must NOT be read here as "this facility has an active
                # song bonus": the stat can't actually gain anything, so no
                # badge should promise one.
                if not p.get("value"):
                    continue
                tt = p.get("target_type")
                key = "6" if tt == 30 else str(tt)
                val = gl_bonus.get(key)
                if val:
                    bonus_params.append({"target_type": tt, "value": int(val * fte)})
        commands.append({
            "command_type": 1, "command_id": served,
            "performance_inc_dec_info_array": grand_live.token_preview(
                cmd, _support_chara_ids(chara_info, partners), rainbow, rng,
                facility_level=level),
            "params_inc_dec_info_array": bonus_params,
        })
    full_state[PREVIEW_KEY] = {"turn": turn,
                                     "commands": copy.deepcopy(commands)}
    return commands, True


def _support_chara_ids(chara_info: dict, positions) -> list:
    """The character ids of the SUPPORT CARDS standing in these deck positions.

    Scenario NPCs (Tazuna 101, the Director 102, the Reporter 103, ...) share the
    partner list but are NOT support cards, so they are dropped: they must not
    count toward C in the token formula, which is exponential -- an NPC sneaking
    in multiplies the whole facility's payout by 1.15."""
    by_pos = {c.get("position"): c.get("support_card_id")
              for c in (chara_info.get("support_card_array") or [])}
    out = []
    for pos in positions:
        card_id = by_pos.get(pos)
        if card_id:
            out.append(int(card_id) // 100)
    return out


def _is_rainbow_training(chara_info: dict, positions, command_id: int) -> bool:
    """Friendship (rainbow) training: a support at bond >= 80 standing in its
    OWN specialty facility. That is what doubles token throughput, so it decides
    whether this facility pays one colour or two.

    The specialty match is NOT optional -- bond >= 80 alone would call a Speed
    card parked in the Guts facility a rainbow and hand out double tokens for a
    training that isn't one. Same rule training_formula applies for the stat
    gains (`card_type_num == fac and bond >= 80`), read from the same tables so
    the two can't drift."""
    smt = _smt()
    facility = training_formula._facility_index_for(command_id)
    if facility is None:
        return False
    by_pos = {c.get("position"): c.get("support_card_id")
              for c in (chara_info.get("support_card_array") or [])}
    for pos in positions:
        if smt._bond_of(chara_info, pos) < _RAINBOW_BOND:
            continue
        card = training_formula.CARD_BY_ID.get(str(by_pos.get(pos)))
        if card is None:
            continue
        card_type = card.get("type")
        if card_type in training_formula.CARD_TYPE_NAMES and \
                training_formula.CARD_TYPE_NAMES.index(card_type) == facility:
            return True
    return False


_RAINBOW_BOND = 80


def award_tokens(full_state: dict, chara_info: dict, payload: dict) -> None:
    """Credit this turn's performance tokens after a real facility training.

    Only command_type 1 on a training facility pays: Rest, outings, the
    infirmary and races produce no tokens, which is exactly why pressing Rest is
    the scenario's actual loss condition.

    Reached as the scenario's award_training_gains hook, so it runs only for a
    Grand Live career -- the is_active guard it used to open with is now the
    registry lookup that found this scenario."""
    if payload.get("command_type") != 1:
        return
    command_id = payload.get("command_id")
    if grand_live.CAMP_BASE.get(command_id, command_id) not in grand_live.TRAINING_COMMAND_IDS:
        return
    cache = full_state.get(PREVIEW_KEY) or {}
    if cache.get("turn") != payload.get("current_turn"):
        return          # no preview was shown for this turn; promise nothing
    for entry in cache.get("commands") or ():
        if entry.get("command_id") == command_id:
            grand_live.award_tokens(full_state, entry.get("performance_inc_dec_info_array"))
            return
