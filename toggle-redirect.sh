#!/usr/bin/env bash
# Toggle the private-server DNS redirect for a Umamusume client domain.
#
# Linux port of toggle-redirect.ps1: flips the /etc/hosts entry that points
# the Umamusume client at the local private server. Editing /etc/hosts needs
# root, so this re-execs itself via pkexec (graphical auth prompt) for just
# the actual file write.
#
# Domain arg selects which client build's host gets redirected: "global" is
# api.games.umamusume.com (the Steam client, default), "jp" is
# api.games.umamusume.jp (the DMM/mobile client).
#
# Usage:
#   ./toggle-redirect.sh                    # flip global, whatever it currently is
#   ./toggle-redirect.sh on                 # force ON  (client -> private server)
#   ./toggle-redirect.sh off                # force OFF (client -> real server)
#   ./toggle-redirect.sh status             # just report, change nothing
#   ./toggle-redirect.sh on jp              # same, but for the JP domain
set -euo pipefail

HOSTS_PATH=/etc/hosts

# $2 means different things depending on invocation: the domain in normal use,
# but the on/off target when re-invoked as --apply-as-root (see bottom of
# file) -- so domain is $3 in that branch instead.
if [[ "${1:-}" == "--apply-as-root" ]]; then
    domain_arg="${3:-global}"
else
    domain_arg="${2:-global}"
fi
case "$domain_arg" in
    global) TARGET_HOST="api.games.umamusume.com" ;;
    jp) TARGET_HOST="api.games.umamusume.jp" ;;
    *) echo "usage: $(basename "$0") [toggle|on|off|status] [global|jp]" >&2; exit 1 ;;
esac
ACTIVE_LINE="127.0.0.1 $TARGET_HOST   # private-server redirect ($domain_arg)"
DISABLED_LINE="# 127.0.0.1 $TARGET_HOST   # private-server redirect ($domain_arg) (disabled)"

get_state() {
    [[ -f "$HOSTS_PATH" ]] || { echo absent; return; }
    local line
    line=$(grep -F "$TARGET_HOST" "$HOSTS_PATH" 2>/dev/null | head -1 || true)
    if [[ -z "$line" ]]; then echo absent
    elif [[ "$line" =~ ^[[:space:]]*# ]]; then echo off
    else echo on
    fi
}

# Internal: runs as root via pkexec, rewrites /etc/hosts collapsing any
# duplicate target-host lines into one (same behavior as the .ps1 original).
apply_as_root() {
    local target="$1" tmp replacement written=0
    tmp=$(mktemp)
    if [[ "$target" == on ]]; then replacement="$ACTIVE_LINE"; else replacement="$DISABLED_LINE"; fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == *"$TARGET_HOST"* ]]; then
            if [[ "$written" -eq 0 ]]; then printf '%s\n' "$replacement" >>"$tmp"; written=1; fi
        else
            printf '%s\n' "$line" >>"$tmp"
        fi
    done < "$HOSTS_PATH"
    [[ "$written" -eq 1 ]] || printf '%s\n' "$replacement" >>"$tmp"
    cat "$tmp" > "$HOSTS_PATH"
    rm -f "$tmp"
}

if [[ "${1:-}" == "--apply-as-root" ]]; then
    apply_as_root "$2"
    exit 0
fi

action="${1:-toggle}"
state=$(get_state)

case "$action" in
    status) echo "redirect: $state"; exit 0 ;;
    on) target=on ;;
    off) target=off ;;
    toggle) if [[ "$state" == on ]]; then target=off; else target=on; fi ;;
    *) echo "usage: $(basename "$0") [toggle|on|off|status]" >&2; exit 1 ;;
esac

if [[ "$state" == "$target" ]]; then
    echo "redirect already $target - nothing to do"
    exit 0
fi

SCRIPT="$(readlink -f "$0")"
pkexec bash "$SCRIPT" --apply-as-root "$target" "$domain_arg"
echo "redirect: $(get_state)"
