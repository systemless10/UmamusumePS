#!/usr/bin/env bash
# Start the private Umamusume server (Linux port of start-server.ps1).
#
# Usage:
#   ./start-server.sh              # normal run: use this while PLAYING
#   ./start-server.sh --reload     # auto-restart on code edits (DEV ONLY)
#   ./start-server.sh --force      # kill an existing/orphaned server first
set -euo pipefail

RELOAD=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --reload) RELOAD=1 ;;
        --force) FORCE=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 1 ;;
    esac
done

ROOT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SERVER_DIR="$ROOT_DIR/server"
# python3.14, not the plain `python`/`python3` copies: only this exact binary
# was granted CAP_NET_BIND_SERVICE (via setcap) so the server can bind port
# 443 without running as root -- capabilities are per-inode, and venv makes
# separate copies (not hardlinks) of python/python3/python3.14.
VENV_PY="$SERVER_DIR/.venv/bin/python3.14"

# --- is one already running? ------------------------------------------------
existing_pids=$(pgrep -f "$VENV_PY -m uvicorn app.main:app" || true)
if [[ -n "$existing_pids" ]]; then
    echo "Found existing server process(es): $existing_pids"

    # STALE CODE CHECK: compare newest source .py against when the process
    # started. Python only imports a module once, so a running server keeps
    # executing whatever the source said when it started -- has bitten this
    # project before (see server/README.md / start-server.ps1).
    newest_py=$(find "$SERVER_DIR/app" -name '*.py' -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f1)
    for pid in $existing_pids; do
        start_epoch=$(stat -c %Y "/proc/$pid" 2>/dev/null || true)
        if [[ -n "$start_epoch" && -n "$newest_py" ]]; then
            if awk -v a="$newest_py" -v b="$start_epoch" 'BEGIN{exit !(a>b)}'; then
                echo "STALE: pid $pid predates your newest source edit. It is running OLD code."
                echo "  Restart it (--force) or use --reload."
            fi
        fi
    done

    if [[ "$FORCE" -eq 1 ]]; then
        echo "Stopping them (--force)..."
        kill $existing_pids 2>/dev/null || true
        sleep 0.7
    else
        echo "If port 443 fails to bind, one of these is holding it."
        echo "Re-run with --force to stop them first."
    fi
fi

# --- the DNS redirect must be ON or the client talks to the REAL server -----
redirect_state=$("$ROOT_DIR/toggle-redirect.sh" status 2>/dev/null | awk '{print $2}')
if [[ "$redirect_state" == "on" ]]; then
    echo "DNS redirect: on"
else
    echo "DNS redirect is NOT on -- the game will reach the REAL server."
    echo "  Run:  ./toggle-redirect.sh on"
fi

# --- warn about live TESTING KNOBS -----------------------------------------
cfg_path="$SERVER_DIR/client_config.json"
if [[ -f "$cfg_path" ]]; then
    "$VENV_PY" - "$cfg_path" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
for knob in ("force_failure_rate", "force_training_gain"):
    val = cfg.get(knob)
    if val is not None:
        print(f"TESTING KNOB LIVE: {knob} = {val}  (set it to null for real play)")
EOF
fi

# --- launch ------------------------------------------------------------------
cd "$SERVER_DIR"
uvicorn_args=(-m uvicorn app.main:app --host 0.0.0.0 --port 443
              --ssl-keyfile certs/server.key.pem --ssl-certfile certs/server.cert.pem)

if [[ "$RELOAD" -eq 1 ]]; then
    # Watch ONLY the app package -- without --reload-dir uvicorn watches the
    # whole working directory, so writing a log/fixture/scratch file would
    # restart the server for no reason.
    uvicorn_args+=(--reload --reload-dir app)
    echo
    echo "--reload is ON. Read this once:"
    echo "  * Saving any .py under server/app restarts the worker."
    echo "  * Career state lives in SQLite, so nothing is LOST -- but a"
    echo "    request in flight at that moment fails, and the client can"
    echo "    sit there stuck. Avoid it while actually playing."
    echo "  * The reloader runs a parent + a worker. If a worker is ever"
    echo "    orphaned it keeps port 443 and the next start races it --"
    echo "    recover with --force."
    echo
fi

echo
echo "Starting: $VENV_PY ${uvicorn_args[*]}"
echo "Ctrl-C to stop."
echo
exec "$VENV_PY" "${uvicorn_args[@]}"
