#!/bin/bash
# Install (or update) the bridge on the Pi: copies opentrack_bridge.py and
# the systemd unit into place, creates /etc/pods-head-tracker.conf from the
# template on first run, and enables + starts the service once the config
# holds a real MAC. Nothing here you couldn't type by hand -- it just runs
# the install/systemctl steps from the README in the right order.
#
# Run on the Pi:  sudo ./install.sh    (re-run any time to update)
#
# Idempotent: safe to re-run; it never overwrites an existing config.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

SRC=$(dirname "$(readlink -f "$0")")
for f in opentrack_bridge.py pods-head-tracker.service pods-head-tracker.conf.example; do
    [ -f "$SRC/$f" ] || { echo "$f not found next to this script - scp all repo files to the Pi" >&2; exit 1; }
done

install -m 755 "$SRC/opentrack_bridge.py" /usr/local/bin/
install -m 644 "$SRC/pods-head-tracker.service" /etc/systemd/system/

if [ ! -f /etc/pods-head-tracker.conf ]; then
    install -m 644 "$SRC/pods-head-tracker.conf.example" /etc/pods-head-tracker.conf
    echo "created /etc/pods-head-tracker.conf from the template"
fi

systemctl daemon-reload

# Don't enable the service while the config still holds the template's
# placeholder MAC -- it would just loop failing in the journal.
if grep -q '^AIRPODS_MAC=XX' /etc/pods-head-tracker.conf; then
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
