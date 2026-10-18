#!/usr/bin/env bash
#
# watcher.sh - shell front-end for the watcher service.
#
#   watcher              show this help
#   watcher run          run the watchdog loop in the foreground (systemd uses this)
#   watcher once         full check WITH repairs + Telegram alerts, then exit
#   watcher check        show status, change nothing
#   watcher boot-repair  one quick repair pass (used by watcher-boot.service)
#   watcher start|stop|restart   control the watcher systemd service
#   watcher status       systemd status of the watcher service
#   watcher log [n]      follow the watcher log file
#   watcher notify TEXT  send one Telegram message
#   watcher install      install/refresh the systemd units (see install.sh)
#   watcher prep         pre-install NIC firmware/drivers for the old PC (needs internet)
#   watcher version      print version
#
set -euo pipefail

# Resolve this script's real folder, following the /usr/local/bin symlink.
SELF="${BASH_SOURCE[0]}"
while [ -L "$SELF" ]; do
    TARGET="$(readlink "$SELF")"
    case "$TARGET" in
        /*) SELF="$TARGET" ;;
        *) SELF="$(dirname "$SELF")/$TARGET" ;;
    esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
PY="python3"

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

cmd="${1:-help}"
shift || true

case "$cmd" in
    run)
        exec "$PY" -u "$DIR/watcher.py" --daemon
        ;;
    once)
        exec "$PY" -u "$DIR/watcher.py" --once
        ;;
    check|status-report)
        exec "$PY" -u "$DIR/watcher.py" --check
        ;;
    boot-repair)
        exec "$PY" -u "$DIR/watcher.py" --boot
        ;;
    notify)
        [ $# -ge 1 ] || { echo "usage: watcher notify TEXT" >&2; exit 2; }
        exec "$PY" -u "$DIR/watcher.py" --notify "$*"
        ;;
    start|stop|restart)
        exec systemctl "$cmd" watcher.service
        ;;
    status)
        exec systemctl status watcher.service --no-pager
        ;;
    log)
        n="${1:-100}"
        exec tail -n "$n" -f "$DIR/logs/watcher.log"
        ;;
    install)
        exec bash "$DIR/install.sh"
        ;;
    prep)
        exec bash "$DIR/prep.sh"
        ;;
    version)
        echo "watcher 1.0"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "unknown command: $cmd" >&2
        usage >&2
        exit 2
        ;;
esac
