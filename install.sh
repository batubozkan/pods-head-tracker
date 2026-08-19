#!/bin/bash
# Install (or update) the bridge on the Pi: copies opentrack_bridge.py, the
# probe, and the systemd unit into place, creates /etc/pods-head-tracker.conf
# from the template on first run, and enables + starts the service once the
# config holds a real MAC. Nothing here you couldn't type by hand -- it just
# runs the install/systemctl steps from the README in the right order.
#
# Run on the Pi:  sudo ./install.sh                        (re-run any time to update)
#                 sudo ./install.sh --uninstall [--purge]  (--purge also removes the config)
#
# Idempotent: safe to re-run; it never overwrites an existing config.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now pods-head-tracker 2>/dev/null || true
    rm -f /usr/local/bin/opentrack_bridge.py /usr/local/bin/airpods_ht_probe.py \
          /etc/systemd/system/pods-head-tracker.service
    systemctl daemon-reload
    if [ "${2:-}" = "--purge" ]; then
        rm -f /etc/pods-head-tracker.conf
        echo "removed /etc/pods-head-tracker.conf"
    elif [ -f /etc/pods-head-tracker.conf ]; then
        echo "kept /etc/pods-head-tracker.conf (remove it with: sudo ./install.sh --uninstall --purge)"
    fi
    echo "bridge uninstalled. The USB gadget/audio units (pods-usb-*) come from"
    echo "the setup-usb-*.sh scripts and were not touched."
    exit 0
elif [ $# -gt 0 ]; then
    echo "usage: sudo ./install.sh [--uninstall [--purge]]" >&2
    exit 1
fi

SRC=$(dirname "$(readlink -f "$0")")
for f in opentrack_bridge.py airpods_ht_probe.py pods-head-tracker.service pods-head-tracker.conf.example; do
    [ -f "$SRC/$f" ] || { echo "$f not found next to this script - scp all repo files to the Pi" >&2; exit 1; }
done

install -m 755 "$SRC/opentrack_bridge.py" /usr/local/bin/
install -m 755 "$SRC/airpods_ht_probe.py" /usr/local/bin/
install -m 644 "$SRC/pods-head-tracker.service" /etc/systemd/system/

if [ ! -f /etc/pods-head-tracker.conf ]; then
    install -m 644 "$SRC/pods-head-tracker.conf.example" /etc/pods-head-tracker.conf
    echo "created /etc/pods-head-tracker.conf from the template"
fi

systemctl daemon-reload

# Don't enable the service while the config still holds the template's
# placeholder MAC -- it would just loop failing in the journal.
if grep -q '^AIRPODS_MAC=XX' /etc/pods-head-tracker.conf; then
    # Suggest bonded AirPods for AIRPODS_MAC. Suggestion only, never
    # auto-written: the user must confirm it is the Classic address, not
    # the LE one (see the README's pairing section). BlueZ >= 5.65 lists
    # bonded devices with "devices Paired"; older releases used
    # "paired-devices".
    pods=$( { bluetoothctl devices Paired 2>/dev/null \
              || bluetoothctl paired-devices 2>/dev/null; } | grep -i airpods || true)
    if [ -n "$pods" ]; then
        echo
        echo "Paired AirPods found - AIRPODS_MAC is probably one of these:"
        echo "$pods" | sed 's/^Device /  /'
    fi
    cat <<'EOF'

Installed, but not started yet. Next:
  1. sudo nano /etc/pods-head-tracker.conf   (set AIRPODS_MAC, TRANSPORT, UDP_HOST)
  2. sudo ./install.sh                       (run again: enables + starts)
EOF
else
    systemctl enable pods-head-tracker
    systemctl restart pods-head-tracker
    echo "bridge installed and (re)started - check: systemctl status pods-head-tracker"
fi
