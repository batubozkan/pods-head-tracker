#!/bin/bash
# Build the composite USB gadget: CDC-ACM serial (/dev/ttyGS0, tracking
# frames) + UAC2 audio (a USB "speaker" on Windows whose samples surface on
# an ALSA capture card here on the Pi).
#
# This replaces the single-function g_serial module. A composite gadget only
# exists through configfs, and a configfs gadget only exists if something
# builds it every boot -- so the "no boot script" simplicity g_serial bought
# us is the price of audio. pods-usb-gadget.service runs this at boot;
# by hand:  sudo pods-usb-gadget.sh start|stop
set -euo pipefail

G=/sys/kernel/config/usb_gadget/pods

start() {
    modprobe libcomposite

    # Already built and bound -- nothing to do. NOTE: an unbound UDC file
    # still holds a newline, so test for non-blank CONTENT, not file size;
    # [ -s ] here once made start happily "succeed" over a half-torn tree.
    if [ -d "$G" ] && grep -q '[^[:space:]]' "$G/UDC" 2>/dev/null; then
        exit 0
    fi

    mkdir -p "$G"
    cd "$G"

    # Linux Foundation's "Multifunction Composite Gadget" ID pair -- the
    # conventional choice for ad-hoc configfs composites; both functions use
    # inbox Windows 10/11 class drivers, so no one needs a custom VID/PID.
    echo 0x1d6b > idVendor
    echo 0x0104 > idProduct
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB

    mkdir -p strings/0x409
    # A stable serial string keeps Windows treating this as the same device
    # across reboots (stable COM number). Derived at runtime so nothing
    # device-specific is hardcoded in this file.
    tr -d '\0' < /proc/device-tree/serial-number > strings/0x409/serialnumber \
        2>/dev/null || echo 0123456789 > strings/0x409/serialnumber
    echo "pods-head-tracker" > strings/0x409/manufacturer
    echo "AirPods Bridge"    > strings/0x409/product

    mkdir -p configs/c.1/strings/0x409
    echo "ACM serial + UAC2 audio" > configs/c.1/strings/0x409/configuration
    echo 250 > configs/c.1/MaxPower   # in 2 mA units -> 500 mA, same as g_serial

    # Serial function. The FIRST acm instance becomes /dev/ttyGS0 whatever
    # the instance is named; calling it GS0 just documents the pairing with
    # the untouched SERIAL_DEV=/dev/ttyGS0 in /etc/pods-head-tracker.conf.
    mkdir -p functions/acm.GS0

    # Audio function: UAC2, driven by Windows' inbox usbaudio2.sys.
    # f_uac2's prefixes are from the GADGET's perspective: c_* configures the
    # gadget's CAPTURE side, which is the host's PLAYBACK direction -- the
    # only direction we want. 48 kHz S16_LE stereo end-to-end (Windows
    # default, and SBC negotiates 48 kHz on AirPods) so nothing resamples.
    mkdir -p functions/uac2.0
    # Windows names the playback device after the audio-function interface
    # string (default "Source/Sink"), not the product string.
    echo "AirPods Bridge" > functions/uac2.0/function_name 2>/dev/null || true
    echo 3     > functions/uac2.0/c_chmask   # stereo
    echo 48000 > functions/uac2.0/c_srate
    echo 2     > functions/uac2.0/c_ssize    # bytes/sample = S16_LE
    # No microphone path (that would be HFP-grade audio anyway). If an older
    # kernel refuses to bind with a zero mask, set p_chmask=1 (+p_srate=48000,
    # p_ssize=2) and simply never use the host-capture side.
    echo 0     > functions/uac2.0/p_chmask
    # More USB requests in flight smooths isochronous delivery on dwc2 at the
    # cost of a little RAM; attribute is absent on very old kernels.
    echo 4     > functions/uac2.0/req_number 2>/dev/null || true

    # Guarded so start() can self-heal a partially torn-down tree (e.g. a
    # stop that got killed by systemd's timeout mid-teardown).
    [ -L configs/c.1/acm.GS0 ] || ln -s functions/acm.GS0 configs/c.1/
    [ -L configs/c.1/uac2.0 ]  || ln -s functions/uac2.0  configs/c.1/

    # Bind to the (only) USB device controller. If this write fails, dmesg
    # says why -- the classic cause is g_serial still loaded from
    # /etc/modules and holding the UDC (setup-usb-audio.sh removes it).
    ls /sys/class/udc | head -n1 > UDC
}

stop() {
    [ -d "$G" ] || exit 0
    # The tracking bridge keeps /dev/ttyGS0 open; tearing the port down
    # while it's held can block in the kernel until systemd's stop-timeout
    # kills us mid-teardown (leaving a half-torn tree). Evict holders first
    # -- the bridge is Restart=always and reopens the port on its own.
    { command -v fuser > /dev/null && fuser -k /dev/ttyGS0 2>/dev/null; } || true
    # Unbind, then tear down leaf-to-root. Every step is tolerant: whatever
    # a killed previous teardown left behind, delete what's there and let
    # start() rebuild the rest.
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G"/configs/c.1/acm.GS0 "$G"/configs/c.1/uac2.0 2>/dev/null || true
    rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 \
          "$G"/functions/acm.GS0 "$G"/functions/uac2.0 \
          "$G"/strings/0x409 "$G" 2>/dev/null || true
}

case "${1:-start}" in
    start) start ;;
    stop)  stop ;;
    *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
