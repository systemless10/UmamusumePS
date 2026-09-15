"""Career scenarios, one package each.

Shared career code imports THIS module and nothing deeper:

    from .. import scenarios
    scen = scenarios.for_chara(chara_info)

See base.py for the hook surface and registry.py for how to add a scenario.
"""

from __future__ import annotations

from .base import Scenario, cap_bonus_for_scenario, restricted_support_cards
from .registry import (all_scenarios, autoload, for_chara, get,
                       implemented_ids, register, reset_all, state_keys)

# Import every scenario package NOW, not lazily on the first for_chara(). Their
# producers and resolvers register with career_events by import side effect, the
# same way handlers/registry.py's feature modules do -- and career_events.poll
# must never run against a half-populated registry just because nothing happened
# to ask for a scenario first.
autoload()

__all__ = ["Scenario", "cap_bonus_for_scenario", "all_scenarios", "autoload",
           "for_chara", "get", "implemented_ids", "register", "reset_all",
           "state_keys"]
