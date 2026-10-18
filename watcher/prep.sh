#!/usr/bin/env bash
#
# prep.sh - pre-download everything the OLD PC may need, while this PC
# still has its working internet.  Run it once from here:
#
#     watcher prep
#
# It installs NIC/WiFi firmware packages (so the old network card works
# offline after the disk is moved) plus a few tools the watcher uses,
# and the BIOS-boot GRUB binaries (in case the old PC boots in BIOS mode
# instead of UEFI - the watcher cannot repair that, but README.md
# explains what to do).
#
set -uo pipefail   # NOTE: no -e here, one missing package must not stop the rest

[ "$(id -u)" = "0" ] || { echo "run me as root" >&2; exit 1; }

echo "== watcher prep: fetching driver/firmware packages =="
echo "(this can take a few minutes on a slow connection)"
echo

if ! getent -s dns hosts deb.debian.org >/dev/null 2>&1; then
    echo "no internet / DNS right now - run this while the disk is still online!" >&2
    exit 1
fi

apt-get update || true

PACKAGES=(
    firmware-linux          # meta: free firmware
    firmware-misc-nonfree   # everything merged, incl. many NIC/WiFi firmwares
    firmware-realtek        # very common old-PC NIC firmware
    firmware-iwlwifi        # intel wifi (already present, keep)
    firmware-atheros
    firmware-brcm80211
    firmware-netxen
    grub-pc-bin             # GRUB for BIOS-mode PCs (disk boots UEFI today)
    ethtool iputils-ping pciutils usbutils
)

for p in "${PACKAGES[@]}"; do
    if apt-cache show "$p" >/dev/null 2>&1; then
        echo "--> installing $p"
        apt-get install -y --no-install-recommends "$p" || echo "  !! failed: $p"
    else
        echo "--> $p not in this Debian release (skip)"
    fi
done

echo
echo "prep done.  Optional but recommended:"
echo "  * in the old PC's BIOS set 'After Power Loss' -> 'Power On'"
echo "  * the disk boots UEFI and BIOS now; just make sure the old PC's boot"
echo "    order has this disk first (pick it once in the boot menu if needed)"
echo "  * if a BIOS-only machine still will not boot it, run the rescue script"
echo "    /root/tgmessage/watcher/rescue-bios-boot.sh from any live Linux USB"
