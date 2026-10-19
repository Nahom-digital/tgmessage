#!/usr/bin/env bash
#
# install.sh - install/refresh the watcher service.
#
# What it does (idempotent, safe to re-run):
#   * writes /etc/default/watcher if it does not exist yet
#   * installs watcher.service + watcher-boot.service
#   * links /usr/local/bin/watcher -> /root/tgmessage/watcher/watcher.sh
#   * enables the watcher units (services you add with
#     `tgmessage service add <name>` are enabled by the boot-repair pass)
#   * runs one boot-repair pass
#   * starts the watcher daemon
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" = "0" ] || { echo "run me as root" >&2; exit 1; }

echo "== watcher installer =="
echo "directory: $DIR"

# ---------------------------------------------------------------- config ---- #
if [ ! -f /etc/default/watcher ]; then
    cat > /etc/default/watcher <<'EOF'
# watcher configuration (read by /root/tgmessage/watcher/watcher.py)
# Change values, then: systemctl restart watcher

# seconds between check cycles
WATCH_INTERVAL=30
# 1 = send Telegram notifications, 0 = log only
NOTIFY=1
# where the tgmessage bot token + permissions live
TG_HOME=/root/tgmessage
# fallback address if DHCP fails - set these for YOUR network
STATIC_IP=192.0.2.100/24
STATIC_GW=192.0.2.1
# resolvers written if DNS is broken
DNS_SERVERS="1.1.1.1 8.8.8.8"
# warn (and vacuum journal) when the root disk passes this usage
DISK_WARN_PCT=92
EOF
    echo "wrote /etc/default/watcher"
else
    echo "/etc/default/watcher already exists (kept)"
fi

mkdir -p "$DIR/state" "$DIR/logs"

# --------------------------------------------------------------- binaries --- #
chmod 755 "$DIR/watcher.py" "$DIR/watcher.sh" "$DIR/install.sh" "$DIR/prep.sh" 2>/dev/null || true
ln -sf "$DIR/watcher.sh" /usr/local/bin/watcher

# --------------------------------------------------------------- units ------ #
install -m 0644 "$DIR/systemd/watcher.service"      /etc/systemd/system/watcher.service
install -m 0644 "$DIR/systemd/watcher-boot.service" /etc/systemd/system/watcher-boot.service
systemctl daemon-reload

# ------------------------------------------------- enable everything -------- #
echo "enabling watcher units for boot..."
systemctl enable watcher.service watcher-boot.service >/dev/null 2>&1
echo "  enabled: watcher.service"
echo "  enabled: watcher-boot.service"
echo "  (add services to watch with: tgmessage service add <name>)"

# --------------------------------------------------------------- start ------ #
echo "running one boot-repair pass..."
"$DIR/watcher.sh" boot-repair || true

echo "starting watcher daemon..."
systemctl restart watcher.service
sleep 2
systemctl --no-pager --lines=5 status watcher.service || true

echo
echo "watcher installed."
echo "  tgmessage service add <name>  - watch a systemd service (down -> restart)"
echo "  tgmessage allow mang <id>     - send watcher reports to your Telegram chat"
echo "  watcher check                 - status now"
echo "  watcher log                   - follow log"
echo "  watcher prep                  - pre-download NIC firmware for the old PC (do this while online)"
