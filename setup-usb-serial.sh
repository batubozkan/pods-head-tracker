#!/bin/bash
# Turn the Pi Zero into a USB CDC-ACM serial gadget (/dev/ttyGS0) so tracking
# data can travel over the USB cable instead of Wi-Fi.
#
# Run once on the Pi:  sudo ./setup-usb-serial.sh   (then reboot)
#
# Idempotent: safe to re-run; it only appends what's missing.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# Bookworm keeps config.txt in /boot/firmware; older releases in /boot.
BOOTDIR=/boot/firmware
[ -f "$BOOTDIR/config.txt" ] || BOOTDIR=/boot
[ -f "$BOOTDIR/config.txt" ] || { echo "config.txt not found in /boot/firmware or /boot" >&2; exit 1; }

# dwc2 in peripheral mode: the Zero's inner "USB" port becomes a gadget port
# (it can no longer host keyboards/hubs -- that's the trade).
#
# Match the exact peripheral line, not just "dtoverlay=dwc2": stock Raspberry
# Pi OS ships an inert "dtoverlay=dwc2,dr_mode=host" under a [cm5] hardware
# filter, which a looser check false-matches. Append under an explicit [all]
# header so the line can't land inside such a filtered section.
if ! grep -q '^dtoverlay=dwc2,dr_mode=peripheral' "$BOOTDIR/config.txt"; then
    printf '\n[all]\n# pods-head-tracker: USB serial gadget\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$BOOTDIR/config.txt"
    echo "added dtoverlay=dwc2,dr_mode=peripheral to $BOOTDIR/config.txt"
fi

# Load the controller + the plain serial gadget at boot. /etc/modules instead
# of cmdline.txt: appending lines here is safely idempotent, editing the
# single-line cmdline.txt programmatically is how you brick a boot.
grep -qx 'dwc2' /etc/modules     || { echo dwc2     >> /etc/modules; echo "added dwc2 to /etc/modules"; }
grep -qx 'g_serial' /etc/modules || { echo g_serial >> /etc/modules; echo "added g_serial to /etc/modules"; }

# Stock Raspberry Pi OS never spawns a login console on ttyGS0, but USB-console
# guides tell people to enable one -- and a getty would eat the binary frames.
systemctl disable --now serial-getty@ttyGS0.service 2>/dev/null || true

cat <<'EOF'

Done. Next steps:
  1. sudo reboot
  2. After boot, check the gadget port exists:  ls -l /dev/ttyGS0
  3. Cable the Pi Zero's inner "USB" port (NOT "PWR") to a data USB port on
     the PC, using a data-capable cable. The PC port also powers the Pi.
  4. Windows shows it as "USB Serial Device (COMx)" in Device Manager
     (driver is inbox on Windows 10/11 -- nothing to install).
  5. Set TRANSPORT=serial in /etc/pods-head-tracker.conf and restart the
     service; point OpenTrack's "Hatire Arduino" input at that COM port.
EOF
