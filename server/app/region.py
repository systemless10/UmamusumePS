
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from pathlib import Path

REGIONS = ("global", "jp")

PROCESS_REGION = os.environ.get("UMA_REGION", "global").strip().lower()
if PROCESS_REGION not in REGIONS:
    raise RuntimeError(f"UMA_REGION={PROCESS_REGION!r}; expected one of {REGIONS}")

CURRENT_REGION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "CURRENT_REGION", default=PROCESS_REGION
)

_DATA_ROOT = Path(os.environ.get("UMA_DATA_ROOT") or Path(__file__).resolve().parents[1] / "data")


def data_dir() -> Path:
    region = CURRENT_REGION.get()
    d = _DATA_ROOT if region == "global" else _DATA_ROOT / region
    d.mkdir(parents=True, exist_ok=True)
    return d


def seeds_dir() -> Path:
    return data_dir() / "seeds"


@contextmanager
def use_region(name: str):
    token = CURRENT_REGION.set(name)
    try:
        yield
    finally:
        CURRENT_REGION.reset(token)
