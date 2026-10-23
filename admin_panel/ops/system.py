
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

from app import master_data as M
from app import region

from . import op
from .common import OpError, region_name

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
PROFILE = SERVER / "launch_profile.json"

GAME = {"global": ("UmamusumePrettyDerby", "UmamusumePrettyDerby.exe", "api.games.umamusume.com", "127.0.0.1"),
        "jp": ("UmamusumePrettyDerby_Jpn", "UmamusumePrettyDerby_Jpn.exe", "api.games.umamusume.jp", "127.0.0.2")}
APPS = {"global": "app.main:app", "jp": "app.jp.server:app"}

CFG_KEYS = {
    "enabled": (False, "Plugin redirect", "Send the game's API traffic to `server` (no hosts edit needed)."),
    "server": ("", "Server address", "https://host[:port] the plugin sends traffic to."),
    "future_content": (False, "Future Global content", "Admin panel shows unreleased Global content."),
    "custom_support": (False, "Custom support cards", "Load the private master pack and custom card art."),
    "server_events": (False, "Server events", "Load the pack's re-dated events, ports and day sync."),
    "unreleased_trainees": (False, "Unreleased trainees", "Show ported trainees."),
    "big_fields": (True, "Big fields", "Let races with more than 18 runners load."),
    "capture": (False, "Capture (real server)", "umacap-format logs while the redirect is OFF."),
    "client_log": (False, "Client log", "Game errors + managed exceptions to hachimi/clientlog."),
}


def no_game_install() -> bool:
    return os.environ.get("UMA_NO_GAME_INSTALL") == "1"


def game_dir(reg: str | None = None) -> Path:
    reg = reg or region_name()
    if no_game_install():
        return ROOT / ".no-game-install"
    env = os.environ.get(f"UMA_GAME_DIR_{reg.upper()}")
    if env:
        return Path(env)
    folder = GAME[reg][0]
    for steam in (Path.home() / ".local/share/Steam", Path.home() / ".steam/steam",
                  Path("C:/Program Files (x86)/Steam")):
        p = steam / "steamapps/common" / folder
        if p.is_dir():
            return p
    return Path.home() / ".local/share/Steam/steamapps/common" / folder


def hachimi_dir(reg: str | None = None) -> Path:
    return game_dir(reg) / "hachimi"


def game_master(reg: str) -> Path | None:
    explicit = os.environ.get(f"MASTER_MDB_PATH_{reg.upper()}")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    d = game_dir(reg)
    p = d / f"{GAME[reg][0]}_Data/Persistent/master/master.mdb"
    if reg == "global" and not p.is_file() and (ROOT / "master.mdb").is_file():
        return ROOT / "master.mdb"
    return p if p.is_file() else None


def game_running(reg: str | None = None) -> bool:
    exe = GAME[reg or region_name()][1]
    try:
        out = subprocess.run(["pgrep", "-f", exe], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


def _need_closed():
    if game_running():
        raise OpError(f"Close the {region_name().upper()} game first (packs and redirect.cfg are read at launch).")


def read_cfg(reg: str | None = None) -> tuple[dict, list[str]]:
    path = hachimi_dir(reg) / "redirect.cfg"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    vals = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals, lines


def write_cfg(changes: dict, reg: str | None = None) -> None:
    path = hachimi_dir(reg) / "redirect.cfg"
    if not path.parent.is_dir():
        raise OpError(f"No Hachimi install at {path.parent}.")
    _, lines = read_cfg(reg)
    if not lines:
        lines = ["# Server redirect (Hachimi plugin hachimi_redirect)"]
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in changes:
            if k not in done:
                out.append(f"{k}={_cfg_text(changes[k])}")
                done.add(k)
            continue
        out.append(line)
    out += [f"{k}={_cfg_text(v)}" for k, v in changes.items() if k not in done]
    if path.exists():
        shutil.copy2(path, path.with_name("redirect.cfg.bak-admin"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cfg_text(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v).strip()


def _cfg_view(reg: str) -> list[dict]:
    vals, _ = read_cfg(reg)
    out = []
    for k, (default, label, help_) in CFG_KEYS.items():
        raw = vals.get(k)
        if isinstance(default, bool):
            value = default if raw is None else raw.lower() in ("1", "true", "yes", "on")
        else:
            value = raw or default
        out.append({"key": k, "label": label, "help": help_, "value": value, "set": raw is not None})
    return out


def _default_profile(reg: str) -> dict:
    return {"mode": "hosts", "host": GAME[reg][3], "port": 443, "ios_certs": False}


def read_profile() -> dict:
    try:
        data = json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {r: {**_default_profile(r), **(data.get(r) or {})} for r in GAME}


def hosts_state(reg: str) -> str:
    host = GAME[reg][2]
    try:
        for line in Path("/etc/hosts").read_text().splitlines():
            if host in line:
                return "off" if line.lstrip().startswith("#") else "on"
    except OSError:
        return "unknown"
    return "absent"


def server_procs(reg: str) -> list[dict]:
    try:
        out = subprocess.run(["pgrep", "-af", f"uvicorn {APPS[reg]}"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if "pgrep" in cmd:
            continue
        try:
            started = Path(f"/proc/{pid}").stat().st_mtime
        except OSError:
            started = None
        host = port = None
        parts = cmd.split()
        if "--host" in parts:
            host = parts[parts.index("--host") + 1]
        if "--port" in parts:
            port = parts[parts.index("--port") + 1]
        procs.append({"pid": int(pid), "host": host, "port": port, "started": started,
                      "ios_certs": "certs/ios/" in cmd})
    return procs


def _ips() -> list[str]:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        return [ip for ip in out.split() if ":" not in ip]
    except (OSError, subprocess.SubprocessError):
        return []


def start_command(reg: str, p: dict) -> str:
    env = []
    if p["mode"] != "hosts" or p["host"] != GAME[reg][3]:
        env.append(f"UMA_HOST={p['host']}")
    if int(p["port"]) != 443:
        env.append(f"UMA_PORT={p['port']}")
    flags = (["--region jp"] if reg == "jp" else []) + (["--ios-certs"] if p.get("ios_certs") and reg == "global" else [])
    return " ".join(env + ["./start-server.sh"] + flags)


def plugin_server(p: dict) -> str:
    return f"https://{p['host']}" + ("" if int(p["port"]) == 443 else f":{p['port']}")


@op("system.get")
def system_get(viewer, args):
    reg = region_name()
    prof = read_profile()
    p = prof[reg]
    cfg = _cfg_view(reg)
    cfgv = {c["key"]: c["value"] for c in cfg}
    hosts = hosts_state(reg)
    procs = server_procs(reg)
    warnings = []
    if p["mode"] == "hosts" and hosts != "on":
        warnings.append("Hosts mode, but the /etc/hosts redirect is not on: the game reaches the REAL server.")
    if p["mode"] == "hosts" and cfgv["enabled"]:
        warnings.append("Hosts mode, but the plugin redirect is also on (it wins; check its server address).")
    if p["mode"] in ("plugin", "lan") and not cfgv["enabled"] and hosts != "on":
        warnings.append("Plugin mode, but the plugin redirect is off and hosts is off: the game reaches the REAL server.")
    if p["mode"] in ("plugin", "lan") and cfgv["enabled"] and cfgv["server"].rstrip("/") != plugin_server(p):
        warnings.append(f"redirect.cfg server is {cfgv['server'] or 'unset'}, profile says {plugin_server(p)}.")
    if p["mode"] == "plugin" and hosts == "on":
        warnings.append("Plugin mode with /etc/hosts still redirected: turn hosts off to keep DNS untouched.")
    if procs and all(str(x["port"] or 443) != str(p["port"]) for x in procs):
        warnings.append("The running server listens on another port than the profile: restart it.")
    return {"region": reg, "profile": p, "modes": ["hosts", "plugin", "lan"],
            "hosts": hosts, "domain": GAME[reg][2], "cfg": cfg,
            "cfg_path": str(hachimi_dir(reg) / "redirect.cfg"), "hachimi": hachimi_dir(reg).is_dir(),
            "game_dir": str(game_dir(reg)), "game_running": game_running(reg),
            "servers": procs, "ips": _ips(), "start_command": start_command(reg, p),
            "plugin_server": plugin_server(p), "warnings": warnings}


@op("system.set_profile")
def system_set_profile(viewer, args):
    reg = region_name()
    mode = args.get("mode")
    if mode not in ("hosts", "plugin", "lan"):
        raise OpError("Mode must be hosts, plugin or lan.")
    host = str(args.get("host") or "").strip() or GAME[reg][3]
    if any(c in host for c in "/ :") and not host.startswith("["):
        raise OpError("Host is an IP or name, without scheme or port.")
    try:
        port = int(args.get("port") or 443)
    except ValueError:
        raise OpError("Port must be a number.") from None
    if not 1 <= port <= 65535:
        raise OpError("Port must be 1-65535.")
    p = {"mode": mode, "host": host, "port": port,
         "ios_certs": bool(args.get("ios_certs")) and reg == "global"}
    try:
        data = json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[reg] = p
    tmp = PROFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, PROFILE)
    if args.get("apply_cfg"):
        _need_closed()
        write_cfg({"enabled": mode != "hosts", **({"server": plugin_server(p)} if mode != "hosts" else {})}, reg)
    return system_get(viewer, {})


@op("system.set_cfg")
def system_set_cfg(viewer, args):
    vals = args.get("values") or {}
    bad = [k for k in vals if k not in CFG_KEYS]
    if bad:
        raise OpError(f"Unknown setting {bad[0]}.")
    _need_closed()
    clean = {}
    for k, v in vals.items():
        if isinstance(CFG_KEYS[k][0], bool):
            clean[k] = bool(v)
        else:
            v = str(v or "").strip()
            if k == "server" and v and not v.startswith("https://"):
                raise OpError("The server must be https:// (the client refuses plain http).")
            clean[k] = v
    write_cfg(clean)
    return system_get(viewer, {})


@op("system.future")
def future(viewer, args):
    from .common import future_content
    return {"future": future_content()}
