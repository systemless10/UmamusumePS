
from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import producers


class Ura(Scenario):
    scenario_id = 1
    name = "URA Finale"
    endpoint_prefix = "single_mode"
    data_set_key = "ura_data_set"


register(Ura())
