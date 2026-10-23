
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("uma-server")

_REGISTRY: dict = {}

_FEATURE_MODULES = [
    "app.handlers.heroes",
    "app.handlers.cards",
    "app.handlers.stories",
    "app.handlers.presents",
    "app.handlers.daily_races",
    "app.handlers.legend_race",
    "app.handlers.challenge_match",
    "app.handlers.team_building",
    "app.handlers.note_archive",
    "app.handlers.user_profile",
    "app.handlers.jukebox_requests",
    "app.handlers.main_story_race",
    "app.handlers.team_stadium",
    "app.handlers.account_link",
    "app.handlers.missions",
    "app.handlers.idle_single_mode",
    "app.handlers.story_event",
    "app.handlers.training_challenge",
    "app.handlers.limited_shop",
    "app.handlers.transfer",
    "app.handlers.campaign_walking",
    "app.handlers.map_event",
    "app.handlers.stamina",
    "app.handlers.payment",
    "app.handlers.friends",
    "app.handlers.circles",
    "app.handlers.practice_race_lobby",
    "app.handlers.room_match",
    "app.handlers.client_infra",
    "app.handlers.galleries",
    "app.handlers.champions",
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
    for mod in _FEATURE_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pass
        except Exception:
            log.exception("registry: failed to load %s", mod)
