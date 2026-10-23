
from __future__ import annotations

import copy
import functools
import json
import logging

from .. import region

log = logging.getLogger("uma-server")

_reported: set = set()


@functools.lru_cache(maxsize=1)
def _schemas() -> dict:
    path = region.data_dir() / "response_schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("endpoints") or {}
    except (OSError, ValueError):
        log.error("JP: no response schema at %s -- run tools/build_jp_schema.py", path)
        return {}


def has_schema(endpoint: str) -> bool:
    return endpoint in _schemas()


_CAREER_DONORS = ("single_mode_onsen", "single_mode_ramen")


@functools.lru_cache(maxsize=256)
def _career_donor(endpoint: str) -> dict | None:
    family, _, call = endpoint.partition("/")
    if not call or not (family == "single_mode" or family.startswith("single_mode_")):
        return None
    for donor in _CAREER_DONORS:
        schema = _schemas().get(f"{donor}/{call}")
        if schema and schema.get("t") == "map":
            return dict(schema, f={k: v for k, v in schema["f"].items()
                                   if not k.endswith("_data_set")})
    return None


def schema_for(endpoint: str) -> dict | None:
    return _schemas().get(endpoint) or _career_donor(endpoint)


def jp_choices(node) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "choice_array" and isinstance(value, list):
                for choice in value:
                    if (isinstance(choice, dict) and "select_index" in choice
                            and not choice.get("select_index_info_array")):
                        choice["select_index_info_array"] = [
                            {"branch_number": 1, "select_index": choice.pop("select_index")}]
            else:
                jp_choices(value)
    elif isinstance(node, list):
        for item in node:
            jp_choices(item)


def blank(schema: dict):
    t = schema.get("t")
    if t == "map":
        return {k: blank(s) for k, s in schema["f"].items()}
    if t == "array":
        return []
    if t == "nil":
        return None
    return copy.copy(schema.get("d"))


_SCALARS = ("int", "float", "str", "bool")


def _conform(value, schema: dict, path: str, added: list, clashes: list):
    t = schema.get("t")
    if value is None and t in _SCALARS:
        added.append(path.rstrip(".") or "<data>")
        return copy.copy(schema.get("d"))
    if t == "map":
        if isinstance(value, list) and not value:
            added.append(path or "<data>")
            return blank(schema)
        if not isinstance(value, dict):
            if value is not None:
                clashes.append(f"{path}: map expected")
            return value
        for k, s in schema["f"].items():
            if k not in value:
                value[k] = blank(s)
                added.append(f"{path}{k}")
            else:
                value[k] = _conform(value[k], s, f"{path}{k}.", added, clashes)
        return value
    if t == "array":
        if isinstance(value, dict) and not value:
            added.append(path.rstrip(".") or "<data>")
            return []
        if not isinstance(value, list):
            if value is not None:
                clashes.append(f"{path.rstrip('.')}: array expected")
            return value
        elem = schema.get("e")
        if elem and elem.get("t") in ("map", "array"):
            for i, item in enumerate(value):
                value[i] = _conform(item, elem, f"{path.rstrip('.')}[].", added, clashes)
        return value
    return value


def conform(endpoint: str, resp: dict, handled: bool) -> dict:
    if (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return resp
    jp_choices(resp.get("data"))
    schema = schema_for(endpoint)
    if not schema:
        return resp
    added: list = []
    clashes: list = []
    data = resp.get("data")
    if data is None:
        data = {}
    resp["data"] = _conform(data, schema, "", added, clashes)
    key = (endpoint, handled)
    if key not in _reported and (added or clashes or not handled):
        _reported.add(key)
        if not handled:
            log.warning("JP %s: no handler yet -- served the real reply's shape, empty", endpoint)
        elif added:
            log.info("JP %s: completed %d field(s) to the JP shape: %s", endpoint, len(added),
                     ", ".join(added[:12]) + (" ..." if len(added) > 12 else ""))
        for c in clashes[:8]:
            log.warning("JP %s: type clash left as-is: %s", endpoint, c)
    return resp
