
from __future__ import annotations

import json

from app import state as state_store

from .. import history
from . import op
from .common import OpError


@op("history.list")
def list_(viewer, args):
    return history.rows(viewer if args.get("this_viewer") else None, int(args.get("limit") or 200))


@op("history.get")
def get(viewer, args):
    e = history.entry(int(args.get("id") or 0))
    if not e:
        raise OpError("No such history entry.")
    current = {}
    for k in e["keys"]:
        v = state_store.load_lazy_key(e["viewer"], k) if state_store.get_state(e["viewer"]) is not None else None
        current[k] = None if v is None else len(json.dumps(v))
    return {"id": e["id"], "viewer": e["viewer"], "label": e["label"],
            "keys": [{"key": k, "before": None if e["old"][k] is None else len(e["old"][k]),
                      "now": current[k]} for k in e["keys"]],
            "later": history.later_overlaps(e["id"])}


@op("history.undo", label="Undo #{id}")
def undo(viewer, args):
    e = history.entry(int(args.get("id") or 0))
    if not e:
        raise OpError("No such history entry.")
    vid = e["viewer"]
    full_state = state_store.get_state(vid)
    if full_state is None:
        full_state = {}
    for key, old in e["old"].items():
        if old is None:
            full_state.pop(key, None)
        else:
            full_state[key] = json.loads(old)
    if e["new_viewer"] and not any(v is not None for v in e["old"].values()):
        state_store.delete_state(vid)
    else:
        state_store.save_state(vid, full_state)
    return {"viewer": vid, "keys": e["keys"]}
