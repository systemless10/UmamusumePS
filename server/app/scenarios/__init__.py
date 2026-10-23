
from __future__ import annotations

from .base import Scenario, cap_bonus_for_scenario, restricted_support_cards
from .registry import (all_scenarios, autoload, for_chara, get,
                       implemented_ids, register, reset_all, state_keys)

autoload()

__all__ = ["Scenario", "cap_bonus_for_scenario", "all_scenarios", "autoload",
           "for_chara", "get", "implemented_ids", "register", "reset_all",
           "state_keys"]
