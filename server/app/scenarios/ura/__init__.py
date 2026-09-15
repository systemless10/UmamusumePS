"""URA FINALE (scenario 1) -- the base career.

base.Scenario's defaults ARE URA's behaviour, deliberately: every hook there was
written by taking what shared career code already did unconditionally and making
that the default. So this class overrides nothing, and that emptiness is the
proof the abstraction is honest -- if URA needed overrides, the defaults would
be describing some scenario nobody plays.

What the package DOES own is URA's own fixed story chain (producers.py), which
used to sit in handlers/career_producers.py behind an `is_active` test.
"""

from __future__ import annotations

from ..base import Scenario
from ..registry import register
from . import producers                     # noqa: F401 -- registers producers


class Ura(Scenario):
    scenario_id = 1
    name = "URA Finale"
    endpoint_prefix = "single_mode"
    data_set_key = "ura_data_set"


register(Ura())
