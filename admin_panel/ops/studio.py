
from __future__ import annotations

import json

from . import op
from .common import OpError, region_name
from .system import ROOT, SERVER

TOOLS = ROOT / "tools"

def server_python() -> str:
    for cand in (SERVER / ".venv/bin/python3.14", SERVER / ".venv/bin/python3", SERVER / ".venv/Scripts/python.exe"):
        if cand.exists():
            return str(cand)
    import sys
    return sys.executable


def _global_only():
    if region_name() != "global":
        raise OpError("This builds Global content: switch the dock to GL.")


def _step(argv, label, cwd=ROOT, env=None):
    return {"argv": [str(a) for a in argv], "label": label, "cwd": str(cwd), "env": env or {}}


def recipe(name: str, params: dict) -> dict:
    if name == "hosts":
        state = params.get("state")
        if state not in ("on", "off"):
            raise OpError("state must be on or off.")
        return {"title": f"Hosts redirect {state} ({region_name()})",
                "steps": [_step(["bash", ROOT / "toggle-redirect.sh", state, region_name()], "toggle-redirect")]}
    if name == "loh_opponents":
        _global_only()
        teams = max(100, min(20000, int(params.get("teams") or 3000)))
        argv = [server_python(), TOOLS / "gen_loh_opponents.py", "--teams", teams]
        if params.get("pack"):
            argv += ["--pack", str(params["pack"])]
        if params.get("fetch"):
            argv.append("--fetch")
        return {"title": f"Generate LoH opponents ({teams} teams)", "exclusive": True,
                "steps": [_step(argv, "gen_loh_opponents", cwd=ROOT, env={"UMA_REGION": "global"})]}
    if name == "pvp_opponents":
        _global_only()
        spec = params.get("spec")
        if not isinstance(spec, dict) or not spec.get("name") or not spec.get("target"):
            raise OpError("The generator needs a spec with a name and a target.")
        argv = [server_python(), TOOLS / "opp_ctl.py", "gen", json.dumps(spec)]
        if params.get("pack"):
            argv += ["--pack", str(params["pack"])]
        return {"title": f"Generate opponents gen:{spec['name']} ({spec.get('teams', 3000)} teams)", "exclusive": True,
                "steps": [_step(argv, "opp_ctl gen", cwd=ROOT, env={"UMA_REGION": "global"})]}
    raise OpError(f"Unknown recipe {name}.")


@op("jobs.command")
def jobs_command(viewer, args):
    plan = recipe(str(args.get("recipe") or ""), args.get("params") or {})
    if not plan["steps"]:
        raise OpError("Nothing to run.")
    return plan
