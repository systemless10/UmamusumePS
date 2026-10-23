
from __future__ import annotations

from ...handlers import daily_races, factor_research

from . import (account, archive, auto_play, const_walking, exhibition, jobs, misc, modes,
               passes, plushie, profile_slots, roadmap, skill_filter, skill_set, skill_upgrade, schedule_book, stepup,
               succession, support_cards, trainer_skill)

HANDLERS: dict = {}
for _module in (account, archive, auto_play, const_walking, exhibition, factor_research, jobs, misc, modes,
                passes, plushie, profile_slots, roadmap, skill_filter, skill_set, skill_upgrade, schedule_book, stepup,
                succession, support_cards, trainer_skill):
    for _ep, _fn in _module.HANDLERS.items():
        assert _ep not in HANDLERS, f"two JP feature handlers for {_ep}"
        HANDLERS[_ep] = _fn

HANDLERS["daily_legend_race_skip/race_skip"] = daily_races.handle_legend_race_skip
