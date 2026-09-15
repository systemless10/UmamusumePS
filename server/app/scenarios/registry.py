"""Scenario lookup: scenario_id -> Scenario instance.

Modelled on handlers/registry.py -- a module registers itself by being imported,
and a broken scenario is logged, not fatal, so one half-built scenario cannot
take the server down.

ADDING A SCENARIO
-----------------
    1. app/scenarios/<name>/__init__.py defining a Scenario subclass and calling
       registry.register(MyScenario()) at import time.
    2. One line in _SCENARIO_MODULES below.

That is the whole checklist. Shared career code never names a scenario: it does
`scenarios.for_chara(chara_info)` and calls hooks, which fall back to base.py's
URA-equivalent defaults for anything the new scenario has not overridden.
"""

from __future__ import annotations

import importlib
import logging

from .base import Scenario

log = logging.getLogger("uma-server")

_SCENARIOS: dict = {}

# Imported for their registration side effect, in id order.
_SCENARIO_MODULES = [
    "app.scenarios.ura",
    "app.scenarios.unity_cup",
    "app.scenarios.grand_live",
    "app.scenarios.trackblazer",
]

# What an unknown scenario_id gets. Base = URA's behaviour, which is the right
# fallback: an unimplemented scenario then plays as a plain career rather than
# crashing, and every hook returns its neutral value.
_DEFAULT = Scenario()

_loaded = False


def register(scenario: Scenario) -> Scenario:
    sid = int(scenario.scenario_id)
    if sid in _SCENARIOS:
        log.warning("scenarios: duplicate registration for id %s (keeping %r)",
                    sid, _SCENARIOS[sid])
        return _SCENARIOS[sid]
    _SCENARIOS[sid] = scenario
    return scenario


def autoload() -> None:
    """Import every scenario package once, for its registration side effect."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _SCENARIO_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pass          # scenario not built yet -- fine
        except Exception:
            log.exception("scenarios: failed to load %s", mod)


def get(scenario_id) -> Scenario:
    """The Scenario for this id. NEVER None -- an unknown id gets the base
    (URA-equivalent) behaviour, so no caller needs a null check."""
    autoload()
    try:
        sid = int(scenario_id or 0)
    except (TypeError, ValueError):
        return _DEFAULT
    return _SCENARIOS.get(sid, _DEFAULT)


def for_chara(chara_info) -> Scenario:
    """The Scenario this career is being played in. NEVER None."""
    if not isinstance(chara_info, dict):
        return _DEFAULT
    return get(chara_info.get("scenario_id"))


def all_scenarios() -> list:
    """Every registered scenario, in id order."""
    autoload()
    return [_SCENARIOS[k] for k in sorted(_SCENARIOS)]


def implemented_ids() -> list:
    """The scenario ids this server actually implements. main.py intersects
    master.mdb's released list with this, so a scenario that exists in the game
    data but has no package here is never advertised to the client."""
    autoload()
    return sorted(_SCENARIOS)


def state_keys() -> tuple:
    """Every full_state key owned by any scenario. Cleared for EVERY career
    start -- a Grand Live run's songs must not still be sitting there when a
    URA career begins."""
    keys = []
    for scen in all_scenarios():
        keys.extend(scen.state_keys)
    return tuple(keys)


def reset_all(full_state: dict) -> None:
    """Clear every scenario's state. Called once per career start."""
    for scen in all_scenarios():
        try:
            scen.reset(full_state)
        except Exception:                              # noqa: BLE001
            log.exception("scenarios: reset failed for %r", scen)
