"""Self-registration for endpoint handlers.

main.py's HANDLERS dict predates this and stays authoritative for everything
already in it. NEW feature modules register here instead, so adding an
endpoint never means editing main.py -- the module does

    from . import registry

    @registry.endpoint("daily_race/index")
    def handle_index(payload: dict) -> dict:
        ...

and merely being imported (see _autoload below) makes it live. Handlers keep
the same contract as HANDLERS entries: (payload) -> full response envelope.
main.py consults HANDLERS first, then this registry, then fixture fallback.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger("uma-server")

_REGISTRY: dict = {}

# Feature modules that self-register. Import errors are logged, not fatal --
# one broken feature must not take the server down.
_FEATURE_MODULES = [
    "app.handlers.cards",
    "app.handlers.stories",
    "app.handlers.presents",
    "app.handlers.daily_races",
    "app.handlers.legend_race",
    "app.handlers.note_archive",
    "app.handlers.user_profile",
    "app.handlers.jukebox_requests",
    "app.handlers.main_story_race",
    "app.handlers.team_stadium",
    "app.handlers.account_link",
    "app.handlers.missions",
    "app.handlers.idle_single_mode",
    "app.handlers.story_event",
    "app.handlers.limited_shop",
    "app.handlers.transfer",
    "app.handlers.campaign_walking",
    "app.handlers.stamina",
    "app.handlers.payment",
    "app.handlers.friends",
    "app.handlers.circles",
    "app.handlers.practice_race_lobby",
    "app.handlers.client_infra",
    "app.handlers.galleries",
]


def endpoint(path: str):
    def deco(fn):
        if path in _REGISTRY:
            log.warning("registry: duplicate handler for %s (keeping first)", path)
            return fn
        _REGISTRY[path] = fn
        return fn
    return deco


def get(path: str):
    return _REGISTRY.get(path)


def registered() -> list:
    return sorted(_REGISTRY)


def autoload() -> None:
    """Import every feature module for its registration side effect."""
    for mod in _FEATURE_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pass          # feature not built yet -- fine
        except Exception:
            log.exception("registry: failed to load %s", mod)
