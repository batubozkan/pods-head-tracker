#!/bin/bash
# Add USB audio to the USB transport: the Pi shows up on the PC as a USB
# sound card ("AirPods Bridge") next to the serial port, and everything the
# PC plays into it is forwarded to the AirPods over Bluetooth A2DP -- game
# audio and head tracking through one cable and one pair of buds.
#
# This REPLACES the g_serial gadget from setup-usb-serial.sh with a
# composite gadget (serial + audio, built by pods-usb-gadget.sh at boot).
# The serial side is unchanged: still /dev/ttyGS0, config untouched. Safe on
# a fresh Pi too -- it does not need setup-usb-serial.sh to have run first.
#
# Run once on the Pi:  sudo ./setup-usb-audio.sh   (then reboot)
#
# Idempotent: safe to re-run; it only adds/removes what's needed.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# Everything this script installs ships next to it in the repo.
SRC=$(dirname "$(readlink -f "$0")")
for f in pods-usb-gadget.sh pods-usb-gadget.service \
         pods-usb-audio.sh pods-usb-audio.service; do
    [ -f "$SRC/$f" ] || { echo "$f not found next to this script - scp all repo files to the Pi" >&2; exit 1; }
done

# BlueALSA: registers the A2DP-source profile with bluetoothd (this is what
# lets the AirPods' audio profile actually connect) and exposes it as an
# ALSA PCM. alsa-utils provides alsaloop, the gadget-to-Bluetooth pump.
missing=()
for p in bluez-alsa-utils libasound2-plugin-bluez alsa-utils; do
    dpkg -s "$p" > /dev/null 2>&1 || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
fi

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
    printf '\n[all]\n# pods-head-tracker: USB gadget\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$BOOTDIR/config.txt"
    echo "added dtoverlay=dwc2,dr_mode=peripheral to $BOOTDIR/config.txt"
fi

# Keep loading the controller at boot, but STOP loading g_serial: only one
# gadget can bind the USB device controller, so a leftover g_serial claims
# it first and pods-usb-gadget.service fails writing UDC. libcomposite is
# not listed here on purpose -- the gadget script modprobes it.
grep -qx 'dwc2' /etc/modules || { echo dwc2 >> /etc/modules; echo "added dwc2 to /etc/modules"; }
if grep -qx 'g_serial' /etc/modules; then
    sed -i '/^g_serial$/d' /etc/modules
    echo "removed g_serial from /etc/modules (replaced by the composite gadget)"
fi

# Stock Raspberry Pi OS never spawns a login console on ttyGS0, but USB-console
# guides tell people to enable one -- and a getty would eat the binary frames.
systemctl disable --now serial-getty@ttyGS0.service 2>/dev/null || true

# We only SEND audio to the AirPods. The packaged bluealsa default also
# registers a2dp-sink (the Pi advertising itself as a speaker), which we
# never use -- restrict the daemon to the source profile. Both the binary
# and the systemd unit are probed because bluez-alsa renamed them upstream
# (bluealsa -> bluealsad); newer Debian packages ship the new names.
BLUEALSA_BIN=$(command -v bluealsad || command -v bluealsa) \
    || { echo "bluealsa daemon not found after install?" >&2; exit 1; }
BLUEALSA_UNIT=""
for u in bluealsa.service bluealsad.service; do
    if systemctl cat "$u" > /dev/null 2>&1; then BLUEALSA_UNIT=$u; break; fi
done
[ -n "$BLUEALSA_UNIT" ] || { echo "bluealsa systemd unit not found after install?" >&2; exit 1; }
mkdir -p "/etc/systemd/system/${BLUEALSA_UNIT}.d"
cat > "/etc/systemd/system/${BLUEALSA_UNIT}.d/pods-head-tracker.conf" <<EOF
# pods-head-tracker: A2DP source only (we send audio to the buds, never
# receive). Written by setup-usb-audio.sh.
[Service]
ExecStart=
ExecStart=$BLUEALSA_BIN -S -p a2dp-source
EOF

install -m 755 "$SRC/pods-usb-gadget.sh" "$SRC/pods-usb-audio.sh" /usr/local/bin/
install -m 644 "$SRC/pods-usb-gadget.service" "$SRC/pods-usb-audio.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pods-usb-gadget.service "$BLUEALSA_UNIT" pods-usb-audio.service

cat <<'EOF'

Done. Next steps:
  1. sudo reboot
  2. After boot, on the Pi:  systemctl status pods-usb-gadget pods-usb-audio
     and check the sound card exists:  arecord -l   (card "UAC2Gadget")
  3. Windows re-enumerates the gadget as a NEW device: expect a NEW COM
     number (re-select it once in OpenTrack's Hatire settings) plus a new
     "AirPods Bridge" playback device in Settings > System > Sound.
  4. Play audio on Windows to the "AirPods Bridge" output (set it as the
     default, or route just the game to it) -- it comes out of the AirPods.
  5. Expect Bluetooth-audio latency of roughly 150-300 ms: fine for
     immersion and music, not for competitive audio cues.
EOF
