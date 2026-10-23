
from __future__ import annotations

import copy
import logging
import random

from ... import training_formula
from . import impl as grand_live

log = logging.getLogger("uma-server")

PREVIEW_KEY = "grand_live_token_preview"


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def command_info(full_state: dict, chara_info: dict, home_commands):
    smt = _smt()
    turn = chara_info.get("turn")
    cache = full_state.get(PREVIEW_KEY)
    if isinstance(cache, dict) and cache.get("turn") == turn:
        return copy.deepcopy(cache.get("commands") or []), False

    rng = random.Random(f"{chara_info.get('single_mode_chara_id')}:{turn}")
    by_id = {c.get("command_id"): c for c in (home_commands or [])
             if isinstance(c, dict)}
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
        if entry is None:
            camp_id = smt._CAMP_ID_BY_BASE.get(cmd)
            entry = by_id.get(camp_id)
            if entry is not None:
                served = camp_id
        partners = list((entry or {}).get("training_partner_array") or [])
        rainbow = _is_rainbow_training(chara_info, partners, cmd)
        level = (entry or {}).get("level") or 1
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
                facility_level=level,
                supporter_count=_supporter_count(partners)),
            "params_inc_dec_info_array": bonus_params,
        })
    full_state[PREVIEW_KEY] = {"turn": turn,
                                     "commands": copy.deepcopy(commands)}
    return commands, True


def _support_chara_ids(chara_info: dict, positions) -> list:
    by_pos = {c.get("position"): c.get("support_card_id")
              for c in (chara_info.get("support_card_array") or [])}
    out = []
    for pos in positions:
        card_id = by_pos.get(pos)
        if card_id:
            out.append(int(card_id) // 100)
    return out


def _supporter_count(positions) -> int:
    return sum(1 for pos in positions if int(pos or 0) >= 1000)


def _is_rainbow_training(chara_info: dict, positions, command_id: int) -> bool:
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
    if payload.get("command_type") != 1:
        return
    command_id = payload.get("command_id")
    if grand_live.CAMP_BASE.get(command_id, command_id) not in grand_live.TRAINING_COMMAND_IDS:
        return
    cache = full_state.get(PREVIEW_KEY) or {}
    if cache.get("turn") != payload.get("current_turn"):
        return
    for entry in cache.get("commands") or ():
        if entry.get("command_id") == command_id:
            grand_live.award_tokens(full_state, entry.get("performance_inc_dec_info_array"))
            return
