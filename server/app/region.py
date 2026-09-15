"""
Single source of truth for "which region is this request for" -- the only
thing that should ever differ between Global and JP in the shared game-logic
code path (handlers/, state.py, accounts.py, session.py). Set once per
request by whichever entry point (main.py's Global route vs jp_bridge.py's
JP route) decoded the wire; every storage module downstream reads it to pick
its own separate file/namespace, so there is never a scattered
`if region == "jp"` in handler code -- only here, and in the couple of
storage modules that key off it.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

CURRENT_REGION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "CURRENT_REGION", default="global"
)


def db_suffix() -> str:
    """Appended to a data file's stem: "" for global (unchanged filenames,
    so existing installs/saves keep working), "_jp" for JP."""
    region = CURRENT_REGION.get()
    return "" if region == "global" else f"_{region}"


@contextmanager
def use_region(name: str):
    token = CURRENT_REGION.set(name)
    try:
        yield
    finally:
        CURRENT_REGION.reset(token)
