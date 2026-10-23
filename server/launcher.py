"""One-stop setup + start for the UmaPS server. Called by the four root launchers
(start-global/start-jp, .ps1 for Windows and .sh for macOS/Linux).

    python server/launcher.py --region global|jp [--no-admin] [--reset-venv] [--yes]

Each run, idempotently:
  1. creates server/.venv and installs server/requirements.txt (again only when
     that file changes),
  2. mints a self-signed TLS key/cert for the region (the redirect plugin
     accepts the configured server's certificate, so nothing is installed into
     any trust store),
  3. checks for Rust -- offering to install it via rustup -- and builds the
     race engines when their sources are newer than the binaries,
  4. finds the game install, points MASTER_MDB_PATH at its master.mdb and
     extracts the race course data from its assets (tools/extract_race_assets.py)
     (installing plugin/hachimi_redirect.dll is left to the user; see README),
  5. starts the server, plus the admin panel (opened in your browser) unless
     --no-admin or --reload is given.

Only the standard library is used before the venv exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parent
VENV = SERVER / ".venv"
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
MIN_PY = (3, 11)

# Unprivileged ports on loopback: no root/admin, no setcap. The plugin is told
# the exact address in its Hachimi menu, so the official port 443 isn't needed.
REGIONS = {
    "global": dict(app="app.main:app", port=8443, admin_port=8189, cert="server", install="UmamusumePrettyDerby",
                   data="UmamusumePrettyDerby_Data", exe="UmamusumePrettyDerby.exe"),
    "jp": dict(app="app.jp.server:app", port=8444, admin_port=8190, cert="jp", install="UmamusumePrettyDerby_Jpn",
               data="UmamusumePrettyDerby_Jpn_Data", exe="UmamusumePrettyDerby_Jpn.exe"),
}
HOST = "127.0.0.1"

ASSUME_YES = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n== {msg}")


def ask(question: str) -> bool:
    if ASSUME_YES:
        say(f"{question} [Y/n] y (--yes)")
        return True
    try:
        answer = input(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def die(msg: str) -> "None":
    say(f"\nERROR: {msg}")
    sys.exit(1)


def run(cmd: list, **kw) -> None:
    say("  $ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


# --- 1. venv -----------------------------------------------------------------

def ensure_venv(reset: bool) -> None:
    step("Python environment")
    if sys.version_info < MIN_PY:
        die(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required (this is {platform.python_version()}).")
    if reset and VENV.exists():
        shutil.rmtree(VENV)
    if not VENV_PY.exists():
        say(f"  creating {VENV}")
        run([sys.executable, "-m", "venv", VENV])
    req = SERVER / "requirements.txt"
    stamp = VENV / ".requirements.sha256"
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text().strip() != digest:
        run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"])
        run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", req])
        stamp.write_text(digest)
    say("  ok")


def in_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


# --- 2. certificates -----------------------------------------------------------

def ensure_cert(name: str) -> tuple[Path, Path]:
    step("TLS certificate")
    certs = SERVER / "certs"
    key_path, cert_path = certs / f"{name}.key.pem", certs / f"{name}.cert.pem"
    if key_path.exists() and cert_path.exists():
        say(f"  ok ({cert_path.relative_to(ROOT)})")
        return key_path, cert_path

    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    certs.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"UmaPS local {name}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address(HOST)),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if not WINDOWS:
        key_path.chmod(0o600)
    say(f"  created {cert_path.relative_to(ROOT)}")
    return key_path, cert_path


# --- 3. Rust + race engines ----------------------------------------------------

def find_cargo() -> str | None:
    found = shutil.which("cargo")
    if found:
        return found
    home = Path.home() / ".cargo" / "bin" / ("cargo.exe" if WINDOWS else "cargo")
    return str(home) if home.exists() else None


def install_rust() -> None:
    if WINDOWS:
        # The GNU host toolchain ships its own linker, so no Visual Studio
        # Build Tools are needed for the race engines.
        exe = Path(tempfile.gettempdir()) / "rustup-init.exe"
        say("  downloading rustup-init.exe ...")
        urllib.request.urlretrieve("https://win.rustup.rs/x86_64", exe)
        run([exe, "-y", "--profile", "minimal", "--default-host", "x86_64-pc-windows-gnu"])
    else:
        if not shutil.which("curl"):
            die("curl is needed to install Rust. Install curl (or Rust from https://rustup.rs) and re-run.")
        run(["sh", "-c", "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal"])


def newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
    return newest


def ensure_engines() -> None:
    step("Race engines (Rust)")
    cargo = find_cargo()
    if not cargo:
        say("  Rust is not installed. The race simulator is written in Rust and has to be")
        say("  built once on this machine (a few minutes).")
        if not ask("  Install Rust now via rustup (https://rustup.rs)?"):
            die("Rust is required. Install it from https://rustup.rs and re-run this launcher.")
        install_rust()
        cargo = find_cargo()
        if not cargo:
            die("Rust was installed but cargo isn't found. Open a new terminal and re-run.")
    for crate in ("race_exact_rs", "race_engine_rs"):
        root = SERVER / crate
        binary = root / "target" / "release" / (crate + (".exe" if WINDOWS else ""))
        sources = [root / "src", root / "Cargo.toml", root / "Cargo.lock"]
        if binary.exists() and binary.stat().st_mtime >= newest_mtime(sources):
            say(f"  {crate}: up to date")
            continue
        say(f"  building {crate} ...")
        cmd = [cargo, "build", "--release", "--quiet", "--manifest-path", root / "Cargo.toml",
               "--target-dir", root / "target"]
        if (root / "Cargo.lock").exists():
            cmd.insert(3, "--locked")
        # Compiler warnings are developer noise for someone just running the server.
        run(cmd, env=dict(os.environ, RUSTFLAGS="-Awarnings"))
        binary.touch()  # cargo skips relinking an unchanged crate; mark it fresh


# --- 4. game install, master.mdb, Hachimi plugin ---------------------------------

def steam_roots() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    if WINDOWS:
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.insert(0, Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
    elif MAC:
        prefix = Path(os.environ.get("UMA_WINEPREFIX", home / "Games" / "steam"))
        roots.append(prefix / "drive_c" / "Program Files (x86)" / "Steam")
    else:
        roots += [home / ".local/share/Steam", home / ".steam/steam",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    # Extra libraries listed in libraryfolders.vdf.
    libraries: list[Path] = []
    for root in roots:
        libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for line in vdf.read_text(errors="ignore").splitlines():
                parts = line.strip().split('"')
                if len(parts) >= 4 and parts[1] == "path":
                    lib = parts[3].replace("\\\\", "\\")
                    if MAC or not WINDOWS:
                        # Wine-side paths (C:\..., Z:\...) inside a prefix.
                        if lib[1:3] == ":\\":
                            drive = lib[0].lower()
                            rest = lib[3:].replace("\\", "/")
                            lib = ("/" + rest) if drive == "z" else str(root.parents[2] / f"drive_{drive}" / rest)
                    libraries.append(Path(lib))
    seen, unique = set(), []
    for lib in libraries:
        key = str(lib).lower()
        if key not in seen:
            seen.add(key)
            unique.append(lib)
    return unique


def find_game(info: dict) -> Path | None:
    override = os.environ.get("UMA_GAME_DIR")
    if override:
        return Path(override)
    for lib in steam_roots():
        game = lib / "steamapps" / "common" / info["install"]
        if (game / info["exe"]).exists():
            return game
    return None


RACE_ASSETS = {"global": ("race_param_define_global.json", "Global"), "jp": ("race_param_define.json", "JP")}


def _persistent_dir(guess: Path | None) -> Path | None:
    """The game's Persistent folder (the one holding meta, dat/ and master/).
    --persistent wins; else the install's own; else the folder --mdb sits in
    (Persistent/master/master.mdb), for installs that keep data elsewhere."""
    cands = []
    if os.environ.get("UMA_PERSISTENT_DIR"):
        cands.append(Path(os.environ["UMA_PERSISTENT_DIR"]))
    if guess:
        cands.append(guess)
    if os.environ.get("MASTER_MDB_PATH"):
        cands.append(Path(os.environ["MASTER_MDB_PATH"]).resolve().parent.parent)
    for c in cands:
        if (c / "meta").exists():
            return c
    return guess


def ensure_race_assets(region: str, persistent: Path | None) -> None:
    """Course tables + lane curves for the race engine, extracted from the
    user's own install (tools/extract_race_assets.py). Re-extracted after a
    game update changes the asset index."""
    name, label = RACE_ASSETS[region]
    out = SERVER / "data" / name
    persistent = _persistent_dir(persistent)
    meta = persistent / "meta" if persistent else None
    if out.exists() and not (meta and meta.exists() and meta.stat().st_mtime > out.stat().st_mtime):
        say("  race assets: up to date")
        return
    if not (meta and meta.exists()):
        die(f"server/data/{name} is missing and the game's data folder wasn't found to extract it. "
            "Pass --persistent with the folder that holds the game's 'meta' file and 'dat' folder "
            "(usually ...\\Persistent, next to master\\master.mdb).")
    say("  extracting race course data from your game install ...")
    run([VENV_PY, ROOT / "tools" / "extract_race_assets.py", "--region", label,
         "--persistent", persistent, "--output", out])


def setup_game(region: str, info: dict) -> None:
    step("Game install")
    game = find_game(info)
    if not game:
        say(f"  {info['install']} not found in any Steam library.")
        say("  Set UMA_GAME_DIR to the game folder (the one with the .exe) and re-run,")
        say("  or point at a master.mdb directly with --mdb.")
        if not os.environ.get("MASTER_MDB_PATH"):
            die("master.mdb is required -- the server reads all game data from it.")
        ensure_race_assets(region, None)
        return
    say(f"  found {game}")
    mdb = game / info["data"] / "Persistent" / "master" / "master.mdb"
    if not os.environ.get("MASTER_MDB_PATH"):
        if not mdb.exists():
            die(f"{mdb} is missing. Start the game once so it downloads its data, then re-run.")
        os.environ["MASTER_MDB_PATH"] = str(mdb)
    if region == "jp" and not os.environ.get("UMA_EN_TEXT_MDB"):
        gl = find_game(REGIONS["global"]) if not os.environ.get("UMA_GAME_DIR") else None
        gl_mdb = gl and gl / REGIONS["global"]["data"] / "Persistent" / "master" / "master.mdb"
        if gl_mdb and gl_mdb.exists():
            os.environ["UMA_EN_TEXT_MDB"] = str(gl_mdb)
        else:
            say("  note: Global isn't installed, so JP career events match on Japanese text only.")
    ensure_race_assets(region, game / info["data"] / "Persistent")


# --- 5. start --------------------------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def warn_knobs() -> None:
    cfg_path = SERVER / "client_config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for knob in ("force_failure_rate", "force_training_gain"):
            if cfg.get(knob) is not None:
                say(f"  TESTING KNOB LIVE: {knob} = {cfg[knob]} (set it to null in server/client_config.json)")


def open_panel_when_ready(port: int, server: subprocess.Popen) -> None:
    """The server hosts the admin panel itself (app/admin_lifecycle.py); open
    it in the browser once it answers."""
    import time
    import webbrowser
    url = f"http://localhost:{port}"
    for _ in range(240):
        if server.poll() is not None:
            return
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                say(f"  admin panel: {url} (opening in your browser)")
                webbrowser.open(url)
                return
        time.sleep(0.5)


def start(region: str, info: dict, key: Path, cert: Path, reload: bool, admin: bool) -> None:
    import threading
    os.environ["UMA_REGION"] = region
    if not admin:
        os.environ["UMA_ADMIN_ENABLED"] = "0"
    if not port_free(info["port"]):
        die(f"port {info['port']} is in use -- is the {region} server already running?")
    arm_port = int(os.environ.get("UMA_JP_ARM_PORT", "8090"))
    if region == "jp" and not port_free(arm_port):
        die(f"port {arm_port} (JP arming channel) is in use -- is another JP server running?")
    warn_knobs()
    args = [str(VENV_PY), "-m", "uvicorn", info["app"], "--host", HOST, "--port", str(info["port"]),
            "--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
    if reload:
        args += ["--reload", "--reload-dir", "app"]
    step(f"Starting the {region} server on https://{HOST}:{info['port']}  (Ctrl-C to stop)")
    say(f"  In the game's Hachimi menu, set the redirect server to https://{HOST}:{info['port']}")
    server = subprocess.Popen(args, cwd=SERVER)
    if admin:
        panel_port = int(os.environ.get("UMA_ADMIN_PORT", info["admin_port"]))
        threading.Thread(target=open_panel_when_ready, args=(panel_port, server), daemon=True).start()
    try:
        code = server.wait()
    except KeyboardInterrupt:
        code = server.wait()  # Ctrl-C reached uvicorn too; let it save and exit
    sys.exit(code)


def main() -> None:
    global ASSUME_YES
    ap = argparse.ArgumentParser(description="Set up and start the UmaPS server.")
    ap.add_argument("--region", choices=REGIONS, required=True)
    ap.add_argument("--no-admin", action="store_true", help="don't start the admin panel")
    ap.add_argument("--reset-venv", action="store_true", help="recreate server/.venv from scratch")
    ap.add_argument("--reload", action="store_true", help="dev only: restart on code edits")
    ap.add_argument("--mdb", metavar="FILE", help="use this master.mdb instead of the game install's")
    ap.add_argument("--persistent", metavar="DIR",
                    help="the game's Persistent folder (holds meta, dat/, master/), if not next to --mdb or the game")
    ap.add_argument("--game-dir", metavar="DIR", help="game folder (the one with the .exe), if not auto-detected")
    ap.add_argument("--yes", "-y", action="store_true", help="answer yes to every prompt")
    args = ap.parse_args()
    ASSUME_YES = args.yes
    if args.mdb:
        mdb = Path(args.mdb).expanduser().resolve()
        if not mdb.is_file():
            die(f"--mdb: {mdb} not found")
        os.environ["MASTER_MDB_PATH"] = str(mdb)
    if args.game_dir:
        os.environ["UMA_GAME_DIR"] = str(Path(args.game_dir).expanduser().resolve())
    if args.persistent:
        os.environ["UMA_PERSISTENT_DIR"] = str(Path(args.persistent).expanduser().resolve())
    info = REGIONS[args.region]

    if not in_venv():
        ensure_venv(args.reset_venv)
        env = dict(os.environ, UMAPS_LAUNCHER_STAGE2="1")
        rest = [a for a in sys.argv[1:] if a != "--reset-venv"]
        sys.exit(subprocess.call([str(VENV_PY), __file__, *rest], env=env))

    key, cert = ensure_cert(info["cert"])
    ensure_engines()
    setup_game(args.region, info)
    start(args.region, info, key, cert, args.reload, not (args.no_admin or args.reload))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"command failed with exit code {e.returncode}")
    except KeyboardInterrupt:
        pass
