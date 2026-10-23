
from __future__ import annotations

import importlib

OPS: dict[str, tuple] = {}


def op(name: str, label: str | None = None):
    def wrap(fn):
        OPS[name] = (fn, label)
        return fn
    return wrap


def load_all() -> None:
    for mod in ("catalog", "users", "save", "supports", "custom_support", "events", "sync", "umas", "legacy",
                "career_ops", "career_tuning", "misc", "history_ops", "races", "umamoe", "champions",
                "system", "studio", "pvp", "banners"):
        importlib.import_module(f"{__name__}.{mod}")
