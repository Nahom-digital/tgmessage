#!/usr/bin/env bash
#
# rescue-bios-boot.sh - make this server disk bootable on a BIOS-only PC.
#
# WHEN TO USE:
#   Only if the old PC is old enough to have NO UEFI (or you keep CSM/legacy
#   mode because UEFI is broken).  This disk is UEFI/GPT today; a BIOS-only
#   PC cannot boot it until a small "BIOS boot" (bios_grub) partition is
#   added and GRUB for BIOS is installed.
#
# HOW TO USE:
#   Run from a Debian/Ubuntu live USB, with the server disk plugged in.
#   1. boot the live USB
#   2. open a terminal (root), copy this script over (or download it)
#   3.   bash rescue-bios-boot.sh            # auto-detect the disk
#        bash rescue-bios-boot.sh /dev/sdb   # or name it yourself
#   4. it creates a 1 MiB BIOS-boot partition in the free space at the end
#      of the disk, chroots into the installed system and installs GRUB
#      for BIOS.  Existing partitions are NOT touched.
#
# After this, the disk boots BOTH ways: BIOS (i386-pc) and UEFI (x86_64-efi).
#
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root"

command -v sgdisk >/dev/null || die "install the 'gdisk' package in the live system first (apt-get install -y gdisk)"

DISK="${1:-}"
if [ -z "$DISK" ]; then
    DISK=$(lsblk -pnro NAME,TYPE | awk '$2=="disk"{print $1}' | while read -r d; do
        # the server disk carries a ~976M vfat ESP with a Debian folder
        if lsblk -pnro NAME,FSTYPE "$d" | grep -q vfat; then echo "$d"; fi
    done | head -n1)
fi
[ -n "$DISK" ] || die "could not auto-detect the disk - pass it: bash rescue-bios-boot.sh /dev/sdX"
[ -b "$DISK" ] || die "$DISK is not a block device"

echo "disk: $DISK"
sgdisk -p "$DISK" | sed -n '1,15p'

# ------------------------------------------------------------------ layout -- #
# find the linux root partition (ext4) and the ESP (vfat) on that disk
ROOT_PART=$(lsblk -pnro NAME,FSTYPE "$DISK" | awk '$2=="ext4"{print $1}' | head -n1)
ESP_PART=$(lsblk -pnro NAME,FSTYPE "$DISK" | awk '$2=="vfat"{print $1}' | head -n1)
[ -n "$ROOT_PART" ] || die "no ext4 root partition found on $DISK"
echo "root:  $ROOT_PART"
echo "esp:   ${ESP_PART:-none}"

# ------------------------------------------------------- bios_grub partition - #
if sgdisk -p "$DISK" | grep -q "21686148"; then
    echo "BIOS boot partition already present - good."
else
    echo "creating a 1 MiB BIOS boot partition in the free space..."
    IDX=4
    while sgdisk -p "$DISK" | awk '{print $1}' | grep -qx "$IDX"; do IDX=$((IDX+1)); done
    sgdisk -n "${IDX}:0:0" -t "${IDX}:21686148-6449-6E6F-744E-656564454649" \
           -c "${IDX}:BIOS boot" "$DISK"
    partprobe "$DISK" 2>/dev/null || true
    sleep 1
fi

# ------------------------------------------------------------- grub install -- #
MNT=/mnt/serverdisk
mkdir -p "$MNT"
mount "$ROOT_PART" "$MNT"
if [ -n "$ESP_PART" ] && [ -d "$MNT/boot/efi" ]; then
    mount "$ESP_PART" "$MNT/boot/efi" || true
fi
for d in dev dev/pts proc sys run; do
    [ -e "$MNT/$d" ] && mount --bind "/$d" "$MNT/$d" || true
done

cleanup() {
    for d in run sys proc dev/pts dev; do
        umount "$MNT/$d" 2>/dev/null || true
    done
    umount "$MNT/boot/efi" 2>/dev/null || true
    umount "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

if [ ! -d "$MNT/usr/lib/grub/i386-pc" ] && [ ! -d "$MNT/usr/lib/grub/i386-pc" ]; then
    echo "note: grub-pc-bin did not look installed in the chroot; trying apt anyway"
fi

echo "installing GRUB for BIOS into $DISK (from inside the installed system)..."
chroot "$MNT" grub-install --target=i386-pc --recheck "$DISK"
chroot "$MNT" update-grub

echo
echo "DONE.  The disk can now boot in BIOS (legacy) mode too."
echo "Set the old PC's BIOS: SATA/AHCI = AHCI (or IDE, the disk boots both),"
echo "boot order: this disk first, and 'Power On after AC loss' = enabled."
