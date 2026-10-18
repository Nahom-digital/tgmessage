#!/usr/bin/env bash
# tgmessage installer - installs BOTH parts of the app:
#   1. the Telegram bridge (tgmessage.service)
#   2. the self-healing watcher (watcher.service + watcher-boot.service)
#
#   sudo ./install.sh --token '123456789:AA...'   install + start (both parts)
#   sudo ./install.sh                             install using existing .env
#   sudo ./install.sh --no-watcher                Telegram bridge only
#   sudo ./install.sh --uninstall                 remove services + links
#   sudo ./install.sh --uninstall --keep-watcher  keep the watcher installed
#
# What it does:
#   1. fixes permissions on this folder
#   2. writes .env (token / port) without clobbering what is already there
#   3. symlinks this folder's `tgmessage` into /usr/local/bin
#   4. installs a systemd service from tgmessage.service.in and enables it
#      (so the daemon comes back after a reboot)
#   5. runs watcher/install.sh: installs+enables the watcher units, symlinks
#      the `watcher` command, does one boot-repair pass (skip: --no-watcher)
#   6. falls back to a cron @reboot line when there is no systemd

set -euo pipefail

TG_HOME="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PY="${TG_PYTHON:-python3}"
UNIT="/etc/systemd/system/tgmessage.service"
BIN_LINK="/usr/local/bin/tgmessage"
TOKEN=""
PORT=""
DO_UNINSTALL=0
NO_START=0
NO_WATCHER=0
KEEP_WATCHER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --no-watcher) NO_WATCHER=1; shift ;;
    --keep-watcher) KEEP_WATCHER=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$DO_UNINSTALL" = 1 ]; then
  say "uninstalling tgmessage"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now tgmessage.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload 2>/dev/null || true
  fi
  [ -L "$BIN_LINK" ] && rm -f "$BIN_LINK"
  # drop the cron fallback if we added one
  if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v 'tgmessage.py daemon' | grep -v 'watcher/watcher.py' | crontab - 2>/dev/null || true
  fi
  if [ "$KEEP_WATCHER" = 1 ]; then
    say "watcher kept (--keep-watcher)"
  elif [ -f "$TG_HOME/watcher/watcher.py" ] && command -v systemctl >/dev/null 2>&1; then
    say "uninstalling the watcher too (--keep-watcher to keep it)"
    systemctl disable --now watcher.service watcher-boot.service 2>/dev/null || true
    rm -f /etc/systemd/system/watcher.service /etc/systemd/system/watcher-boot.service
    systemctl daemon-reload 2>/dev/null || true
    rm -f /usr/local/bin/watcher
  fi
  say "removed services and bin links (your .env, state/ and files/ are untouched)"
  exit 0
fi

[ "$(id -u)" = 0 ] || die "run me with sudo (I touch /usr/local/bin and /etc/systemd/system)"

say "installing from $TG_HOME"
mkdir -p "$TG_HOME/state"
chmod +x "$TG_HOME/tgmessage" "$TG_HOME/tgmessage.py" 2>/dev/null || true

# ---- .env ----------------------------------------------------------------
if [ ! -f "$TG_HOME/.env" ]; then
  cp "$TG_HOME/.env.example" "$TG_HOME/.env"
  say "created $TG_HOME/.env"
fi

set_env() { # key value  -> rewrite in place, preserving the rest of the file
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$val" '
    BEGIN { done = 0 }
    $0 ~ "^[[:space:]]*" k "=" { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$TG_HOME/.env" > "$tmp"
  cat "$tmp" > "$TG_HOME/.env"
  rm -f "$tmp"
}

if [ -n "$TOKEN" ]; then
  case "$TOKEN" in
    *:*) ;;
    *) die "--token does not look like a bot token (expected 123456789:AA...)" ;;
  esac
  set_env TG_BOT_TOKEN "$TOKEN"
  say "saved token ***${TOKEN: -6}"
fi
if [ -n "$PORT" ]; then
  set_env TG_PORT "$PORT"
  say "saved port $PORT"
fi
chmod 600 "$TG_HOME/.env"

TOKEN_NOW="$(awk -F= '/^TG_BOT_TOKEN=/{print $2}' "$TG_HOME/.env" | head -1)"
[ -n "$TOKEN_NOW" ] || warn "no token in .env yet - set one with: tgmessage --token '<token>'"

# ---- /usr/local/bin ------------------------------------------------------
ln -sf "$TG_HOME/tgmessage" "$BIN_LINK"
say "linked $BIN_LINK -> $TG_HOME/tgmessage"

# ---- service -------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  sed "s|@TG_HOME@|$TG_HOME|g" "$TG_HOME/tgmessage.service.in" > "$UNIT"
  systemctl daemon-reload
  systemctl enable tgmessage.service >/dev/null 2>&1 || true
  if [ "$NO_START" = 1 ]; then
    say "service installed and enabled (not started, --no-start)"
  else
    systemctl restart tgmessage.service
    sleep 1
    say "service: $(systemctl is-active tgmessage.service) / $(systemctl is-enabled tgmessage.service) on boot"
  fi

  # ---- watcher (the other half of this app) --------------------------------
  if [ "$NO_WATCHER" = 1 ]; then
    warn "watcher skipped (--no-watcher)"
  elif [ -f "$TG_HOME/watcher/install.sh" ]; then
    say "installing the watcher (part of this app) ..."
    if bash "$TG_HOME/watcher/install.sh"; then
      say "watcher: $(systemctl is-active watcher.service) / $(systemctl is-enabled watcher.service) on boot"
    else
      warn "the watcher installer failed - see the output above"
    fi
  else
    warn "no watcher/ folder next to this installer - installed the bridge only"
  fi
else
  warn "no systemd here - using a cron @reboot line instead"
  if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v 'tgmessage.py daemon' | grep -v 'watcher/watcher.py'
      echo "@reboot cd $TG_HOME && TG_HOME=$TG_HOME $PY $TG_HOME/tgmessage.py daemon >> $TG_HOME/state/daemon.log 2>&1"
    ) | crontab -
    say "added cron @reboot entry"
    [ "$NO_START" = 1 ] || "$TG_HOME/tgmessage" up
  else
    warn "no cron either - start it by hand with: tgmessage up"
  fi
  if [ "$NO_WATCHER" != 1 ] && [ -f "$TG_HOME/watcher/install.sh" ]; then
    warn "the watcher needs systemd - run watcher/install.sh when systemd is available"
  fi
fi

echo
say "done. usage:"
cat <<EOF
  tgmessage --token '<new token>'   save/replace the token and restart
  tgmessage                         open the Me>/Bot> chat UI
  tgmessage "hello"                 send one message
  tgmessage status                  health check of the bot bridge
  watcher check                     machine health report (network, clock, services)
  watcher log                       follow the watcher log (or send /logs to the bot)
EOF
echo
"$TG_HOME/tgmessage" status || true
