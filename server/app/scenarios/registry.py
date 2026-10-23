
from __future__ import annotations

import importlib
import logging

from .base import Scenario

log = logging.getLogger("uma-server")

_SCENARIOS: dict = {}

_SCENARIO_MODULES = [
    "app.scenarios.ura",
    "app.scenarios.unity_cup",
    "app.scenarios.grand_live",
    "app.scenarios.trackblazer",
    "app.scenarios.venus",
    "app.scenarios.arc",
    "app.scenarios.sport",
    "app.scenarios.cook",
    "app.scenarios.mecha",
    "app.scenarios.legend",
    "app.scenarios.pioneer",
    "app.scenarios.onsen",
    "app.scenarios.breeders",
    "app.scenarios.ramen",
]

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
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _SCENARIO_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pass
        except Exception:
            log.exception("scenarios: failed to load %s", mod)


def get(scenario_id) -> Scenario:
    autoload()
    try:
        sid = int(scenario_id or 0)
    except (TypeError, ValueError):
        return _DEFAULT
    return _SCENARIOS.get(sid, _DEFAULT)


def for_chara(chara_info) -> Scenario:
    if not isinstance(chara_info, dict):
        return _DEFAULT
    return get(chara_info.get("scenario_id"))


def all_scenarios() -> list:
    autoload()
    return [_SCENARIOS[k] for k in sorted(_SCENARIOS)]


def implemented_ids() -> list:
    autoload()
    return sorted(_SCENARIOS)


def state_keys() -> tuple:
    keys = []
    for scen in all_scenarios():
        keys.extend(scen.state_keys)
    return tuple(keys)


def reset_all(full_state: dict) -> None:
    for scen in all_scenarios():
        try:
            scen.reset(full_state)
        except Exception:
            log.exception("scenarios: reset failed for %r", scen)
