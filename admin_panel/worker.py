
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "server"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

_proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)
sys.argv = [sys.argv[0]]


def _send(msg: dict) -> None:
    _proto.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
    _proto.flush()


def main() -> None:
    from admin_panel import history
    history.install()
    from admin_panel.ops import OPS, load_all
    from app import default_viewer
    from admin_panel.ops.common import OpError, region_name, set_prefer_english
    load_all()
    _send({"id": 0, "ok": True, "result": {"ready": True, "region": region_name()}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid = req.get("id")
        name = req.get("op")
        args = req.get("args") or {}
        set_prefer_english(args.pop("_prefer_english", True) is not False)
        viewer = "".join(str(req.get("viewer") or default_viewer.get()).split())
        entry = OPS.get(name)
        if not entry:
            _send({"id": rid, "ok": False, "error": f"Unknown operation {name}."})
            continue
        fn, label = entry
        history.begin()
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log):
                from app import custom_support
                active = custom_support.manifest()
                with custom_support.use_pack(active["digest"] if active else ""):
                    result = fn(viewer, args)
            ids = history.commit(_label(label, args, name)) if label else []
            if name == "history.undo" and ids:
                history.mark_undone(int(args.get("id")), ids[0])
            _send({"id": rid, "ok": True, "result": result, "log": log.getvalue(), "history": ids})
        except SystemExit as ex:
            ids = history.commit(_label(label, args, name)) if label else []
            msg = ex.code if isinstance(ex.code, str) else f"exited ({ex.code})"
            _send({"id": rid, "ok": False, "error": msg, "detail": log.getvalue(), "history": ids})
        except OpError as ex:
            ids = history.commit(_label(label, args, name)) if label else []
            _send({"id": rid, "ok": False, "error": str(ex), "detail": log.getvalue(), "history": ids})
        except Exception as ex:
            ids = history.commit(_label(label, args, name)) if label else []
            _send({"id": rid, "ok": False, "error": f"{type(ex).__name__}: {ex}",
                   "detail": log.getvalue() + traceback.format_exc(), "history": ids})


def _label(label, args, name) -> str:
    try:
        return label.format_map(_Args(args)) if callable(getattr(label, "format_map", None)) else str(label)
    except Exception:
        return name


class _Args(dict):
    def __missing__(self, key):
        return ""

    def __getitem__(self, key):
        v = dict.get(self, key, "")
        if isinstance(v, (list, dict)):
            return f"{len(v)}"
        return v


if __name__ == "__main__":
    main()
