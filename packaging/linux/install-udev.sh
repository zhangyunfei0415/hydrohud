#!/usr/bin/env bash
# Install the HydroHUD udev rule so a normal user can write to the panel's
# hidraw nodes (TCOMAS LG600 1a2c:1520, LG700 1a2c:151c).
#
# Usage: sudo bash packaging/linux/install-udev.sh
set -eu

RULES_NAME="99-hydrohud-lg600.rules"
RULES_DIR="/etc/udev/rules.d"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: this script must run as root (try: sudo bash $0)" >&2
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/$RULES_NAME" ]; then
    echo "error: $RULES_NAME not found next to this script ($SCRIPT_DIR)" >&2
    exit 1
fi

install -D -m 0644 "$SCRIPT_DIR/$RULES_NAME" "$RULES_DIR/$RULES_NAME"
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw

echo "installed $RULES_DIR/$RULES_NAME"
echo "verify the nodes with:  ls -l /dev/hidraw*"
echo "then smoke-test with:   python3 -m hydrohud --smoke-shot out.png"
echo "and run normally with:  python3 -m hydrohud"
