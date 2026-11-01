from __future__ import annotations

import contextvars
import logging

from . import config, region

log = logging.getLogger("uma-server")

MODES = ("client", "custom")
_CLIENT: contextvars.ContextVar = contextvars.ContextVar("CLIENT_VERSIONS", default=("", ""))


def mode() -> str:
    m = str(config.get("version_source", "client"))
    return m if m in MODES else "client"


def _res_number(v: str) -> int:
    head = str(v or "").split(":", 1)[0]
    return int(head) if head.isdigit() else 0


def _app_tuple(v: str) -> tuple:
    try:
        return tuple(int(p) for p in str(v or "").split("."))
    except ValueError:
        return ()


def observe(headers: dict) -> contextvars.Token:
    res = str(headers.get("res-ver") or headers.get("RES-VER") or "")
    app = str(headers.get("app-ver") or headers.get("APP-VER") or "")
    token = _CLIENT.set((res, app))
    if region.PROCESS_REGION == "global":
        res = res.split(":", 1)[0]
    if mode() == "client":
        current = str(config.get("resource_version", ""))
        if _res_number(res) > _res_number(current):
            config.set_value("resource_version", res)
            log.info("resource_version %s -> %s (client RES-VER)", current, res)
        current = str(config.get("app_version", ""))
        if _app_tuple(app) and _app_tuple(app) > _app_tuple(current):
            config.set_value("app_version", app)
            log.info("app_version %s -> %s (client APP-VER)", current, app)
    return token


def reset(token: contextvars.Token) -> None:
    _CLIENT.reset(token)


def client() -> tuple[str, str]:
    return _CLIENT.get()


def resource_version(default: str = "") -> str:
    return str(config.get("resource_version", default))


def app_version(default: str = "") -> str:
    return str(config.get("app_version", default))
