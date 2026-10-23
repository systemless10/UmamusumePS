
from __future__ import annotations

import json
import re

from app import state as state_store

from . import op
from .common import OpError, load, save
from .users import _q


def _parse(raw):
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError as ex:
        raise OpError(f"Not valid JSON: {ex}") from None


def _path(path: str) -> list:
    parts = []
    for seg in re.split(r"[.\[\]]+", str(path or "")):
        if seg:
            parts.append(int(seg) if re.fullmatch(r"-?\d+", seg) else seg)
    return parts


def _dig(root, parts, create=False):
    node = root
    for i, key in enumerate(parts[:-1]):
        if isinstance(node, list):
            if not isinstance(key, int) or not -len(node) <= key < len(node):
                raise OpError(f"{'.'.join(map(str, parts[:i + 1]))}: no such index")
            node = node[key]
        elif isinstance(node, dict):
            if key not in node and str(key) in node:
                key = str(key)
            if key not in node:
                if not create:
                    raise OpError(f"{'.'.join(map(str, parts[:i + 1]))}: no such key")
                node[key] = [] if isinstance(parts[i + 1], int) else {}
            node = node[key]
        else:
            raise OpError(f"{'.'.join(map(str, parts[:i + 1]))}: not a list or object")
    last = parts[-1]
    if isinstance(node, dict) and last not in node and str(last) in node:
        last = str(last)
    return node, last


def _kind(v) -> str:
    return "list" if isinstance(v, list) else "object" if isinstance(v, dict) else \
        "null" if v is None else type(v).__name__


@op("save.keys")
def keys(viewer, args):
    rows = _q("SELECT key, length(value), json_type(value),"
              " CASE json_type(value) WHEN 'array' THEN json_array_length(value)"
              " WHEN 'object' THEN (SELECT COUNT(*) FROM json_each(value)) END"
              " FROM viewer_state_kv WHERE viewer_id=? ORDER BY key", (viewer,))
    if not rows and state_store.get_state(viewer) is None:
        raise OpError(f"No save for viewer {viewer}.")
    return [{"key": k, "size": n, "type": t, "count": c, "lazy": k in state_store._LAZY_KEYS}
            for k, n, t, c in rows]


@op("save.get")
def get(viewer, args):
    full_state = load(viewer)
    key = str(args.get("key") or "")
    if key not in full_state:
        raise OpError(f"No key {key}.")
    value = full_state[key]
    path = args.get("path")
    if path:
        node, last = _dig(value, _path(path))
        try:
            value = node[last]
        except (KeyError, IndexError, TypeError):
            raise OpError(f"No {path} in {key}.") from None
    return {"key": key, "path": path or "", "value": value, "type": _kind(value)}


@op("save.set", label="Edit {key}")
def set_(viewer, args):
    full_state = load(viewer)
    key = str(args.get("key") or "").strip()
    if not key:
        raise OpError("Give a key name.")
    value = _parse(args.get("value"))
    path = args.get("path")
    if path:
        if key not in full_state:
            raise OpError(f"No key {key}.")
        node, last = _dig(full_state[key], _path(path), create=bool(args.get("create")))
        if isinstance(node, list):
            if not isinstance(last, int) or not -len(node) <= last < len(node):
                raise OpError("Index out of range.")
        elif isinstance(node, dict):
            if last not in node and not args.get("create"):
                raise OpError(f"No {path} in {key}; tick create to add it.")
        else:
            raise OpError("That path is inside a plain value.")
        node[last] = value
    else:
        if key not in full_state and not args.get("create"):
            raise OpError(f"No key {key}; tick create to add it.")
        full_state[key] = value
    save(viewer, full_state)
    return {"key": key, "path": path or ""}


@op("save.delete", label="Delete {key}")
def delete(viewer, args):
    full_state = load(viewer)
    key = str(args.get("key") or "")
    path = args.get("path")
    if key not in full_state:
        raise OpError(f"No key {key}.")
    if path:
        node, last = _dig(full_state[key], _path(path))
        try:
            del node[last]
        except (KeyError, IndexError, TypeError):
            raise OpError(f"No {path} in {key}.") from None
    else:
        del full_state[key]
    save(viewer, full_state)
    return {"deleted": f"{key}{'.' + path if path else ''}"}


@op("save.export")
def export(viewer, args):
    full_state = load(viewer)
    only = args.get("keys")
    data = full_state.copy()
    if only:
        data = {k: v for k, v in data.items() if k in set(only)}
    return data


@op("save.import", label="Import save ({mode})")
def import_(viewer, args):
    data = _parse(args.get("data"))
    if not isinstance(data, dict):
        raise OpError("The file must hold a JSON object of state keys.")
    mode = args.get("mode") or "merge"
    full_state = state_store.get_state(viewer)
    if full_state is None:
        full_state = {}
    if mode == "replace":
        for k in list(full_state.keys()):
            del full_state[k]
    elif mode != "merge":
        raise OpError("mode is merge or replace.")
    full_state.update(data)
    save(viewer, full_state)
    return {"keys": len(data), "mode": mode}
