from app import custom_support as C, master_data as M
from . import op
from .common import OpError


def _run(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise OpError(str(e)) from e


@op("custom_support.list")
def listing(viewer, args):
    s = C.status()
    s["manifest"] = C.manifest()
    s["effects"] = C.EFFECTS
    s["history"] = C.history()
    return s


@op("custom_support.template")
def template(viewer, args):
    return _run(C.template, int(args.get("id", 0)))


@op("custom_support.save")
def save(viewer, args):
    return _run(C.save_draft, args.get("definition"), int(args.get("version", 0)))


@op("custom_support.validate")
def validate(viewer, args):
    d = _run(C.validate, args.get("definition"))
    return {"valid": True, "skills": sum(h["hint_gain_type"] == 0 for h in d["hints"]),
            "effects": len(d["effects"])}


@op("custom_support.publish")
def publish(viewer, args):
    return _run(C.publish)


@op("custom_support.enabled")
def enabled(viewer, args):
    _run(C.set_enabled, args.get("enabled") is True)
    return {"enabled": args.get("enabled") is True}


@op("custom_support.activate")
def activate(viewer, args):
    return _run(C.activate, args.get("digest", ""))


@op("custom_support.retire")
def retire(viewer, args):
    _run(C.retire, int(args.get("id", 0)))
    return {"retired": True}


@op("custom_support.export")
def export(viewer, args):
    return {"protocol": C.PROTOCOL, "region": C.region.CURRENT_REGION.get(),
            "cards": C.status()["cards"]}


@op("custom_support.art")
def art(viewer, args):
    sid = int(args.get("id", 0))
    d = next((d for d in C.status()["cards"] if d["id"] == sid), None)
    if not d:
        raise OpError("Custom card not found")
    return {"art": d.get("art", ""), "donor": d["art_donor"]}
