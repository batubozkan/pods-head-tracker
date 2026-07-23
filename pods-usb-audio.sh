#!/bin/bash
# Move host-playback audio from the UAC2 gadget's ALSA capture card to the
# AirPods over Bluetooth A2DP (BlueALSA).
#
# One attempt per step, exit on any failure: the systemd unit's
# Restart=always is the retry loop, so "AirPods in the case" or "cable
# unplugged" just means a quiet retry every few seconds -- the same
# philosophy the tracking bridge applies to an unreachable link.
set -euo pipefail

# AIRPODS_MAC arrives via the unit's EnvironmentFile -- the same
# /etc/pods-head-tracker.conf the bridge uses; nothing extra to configure.
: "${AIRPODS_MAC:?AIRPODS_MAC not set - see /etc/pods-head-tracker.conf}"
LATENCY_MS="${AUDIO_LATENCY_MS:-50}"

# Unit ordering starts us after the gadget unit, but USB enumeration is
# asynchronous -- give the sound card a moment to appear.
for _ in $(seq 30); do
    [ -e /proc/asound/UAC2Gadget ] && break
    sleep 1
done
[ -e /proc/asound/UAC2Gadget ] || {
    echo "no UAC2Gadget sound card - is pods-usb-gadget.service healthy?" >&2
    exit 1
}

# WAIT for the buds to be on the Bluetooth link instead of paging them.
# Every active connect attempt to absent buds is a multi-second page that
# monopolizes the shared 2.4 GHz radio (Wi-Fi/SSH starves) -- and it's
# never needed: AirPods leaving the case page their bonded hosts on their
# own, and the tracking bridge is paging them continuously anyway. "info"
# only reads bluetoothd's cached state; the radio stays quiet.
bluetoothctl info "$AIRPODS_MAC" 2>/dev/null | grep -q "Connected: yes" || {
    echo "AirPods not on the Bluetooth link yet (in case?) - waiting" >&2
    exit 1
}

# The link is up, so connecting the A2DP profile rides the existing ACL --
# no paging. With bluealsa registering A2DP source, this is the call that
# used to fail with br-connection-profile-unavailable.
bluetoothctl connect "$AIRPODS_MAC" > /dev/null || {
    echo "A2DP profile connect failed - retrying" >&2
    exit 1
}

# Make the buds render at a known volume. In bluealsa's default soft-volume
# mode nothing ever sets the AirPods' own (AVRCP absolute) volume, and for a
# never-used source it can sit at zero: a perfectly healthy running stream,
# rendered as silence (cost a real debugging session). Absolute mode + a
# fixed ceiling fixes that; day-to-day loudness is the normal Windows volume
# slider, which scales the USB stream before it reaches us.
PCM="/org/bluealsa/hci0/dev_${AIRPODS_MAC//:/_}/a2dpsrc/sink"
for _ in $(seq 10); do
    bluealsa-cli info "$PCM" > /dev/null 2>&1 && break
    sleep 1
done
VOL="${AUDIO_VOLUME:-70}"
bluealsa-cli soft-volume "$PCM" false 2>/dev/null || true
bluealsa-cli volume "$PCM" "$VOL" "$VOL" 2>/dev/null || true

# The two ends tick on independent clocks (Windows' USB clock vs the
# AirPods' sink clock); -S 1 lets alsaloop rate-adjust instead of drifting
# into an xrun every few minutes. -t is the loop latency in microseconds:
# lower = snappier, higher = fewer dropouts (AUDIO_LATENCY_MS in the conf).
#
# The pipeline below is a watchdog: when the host stops its USB stream
# (audio idle) and later restarts it, an ALREADY-OPEN capture handle can
# miss the resume and starve forever (f_uac2/dwc2 wart) -- while a fresh
# open works every time. alsaloop just logs "underrun" endlessly in that
# state, so a burst of underruns = time to die and let the unit's restart
# loop re-open everything. Idle cycling this way is harmless and silent.
alsaloop \
    -C hw:CARD=UAC2Gadget,DEV=0 \
    -P "bluealsa:DEV=${AIRPODS_MAC},PROFILE=a2dp" \
    -r 48000 -f S16_LE -c 2 \
    -t $((LATENCY_MS * 1000)) -S 1 2>&1 | {
    n=0
    while IFS= read -r line; do
        printf '%s\n' "$line"
        case $line in
        *underrun*)
            n=$((n + 1))
            if [ "$n" -ge 8 ]; then
                echo "sustained underruns - reopening the capture path" >&2
                pkill -x alsaloop 2>/dev/null || true
                exit 1
            fi
            ;;
        esac
    done
    exit 1  # alsaloop exited on its own; restart takes it from here
}
