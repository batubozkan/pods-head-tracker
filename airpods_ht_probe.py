#!/usr/bin/env python3
"""Diagnostic probe (runs on the Raspberry Pi): open the AirPods' AACP
channel, start head tracking, and print/decode every inbound packet.

Run it with the service stopped (only one L2CAP connection at a time) to
confirm the sensor stream works before involving OpenTrack -- continuous
"HT ..." lines mean success.
"""

import argparse
import socket
import struct
import sys
import time

# --- AACP head-tracking protocol (reverse-engineered, from LibrePods) --------
# Constants are intentionally duplicated with opentrack_bridge.py so each
# script stays single-file deployable (scp one file to the Pi).
AACP_PSM = 0x1001

# BlueZ requires an encrypted link for PSM 0x1001; a default raw L2CAP socket
# only requests "low" security and connect() returns EACCES. Setting
# BT_SECURITY=MEDIUM elevates the link so the channel opens.
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_MEDIUM = 2

HANDSHAKE = bytes.fromhex("00000400010002000000000000000000")
FEATURE_FLAGS = bytes.fromhex("040004004D00D70000000000000000")
NOTIFICATIONS = bytes.fromhex("040004000F00FFFFFFFF")
# Built from verified byte lists (identical to the firmware's packets.h).
START_DEF = bytes([0x04,0x00,0x04,0x00,0x17,0x00,0x00,0x00,0x10,0x00,0x10,0x00,
                   0x08,0xA1,0x02,0x42,0x0B,0x08,0x0E,0x10,0x02,0x1A,0x05,0x01,
                   0x40,0x9C,0x00,0x00])
START_ALT = bytes([0x04,0x00,0x04,0x00,0x17,0x00,0x00,0x00,0x10,0x00,0x0F,0x00,
                   0x08,0x73,0x42,0x0B,0x08,0x10,0x10,0x02,0x1A,0x05,0x01,0x40,
                   0x9C,0x00,0x00])

# Sensor packets: >=70 bytes, HT_PREFIX then opcode 0x44/0x45; orientation
# int16s at offsets 43/45/47, acceleration at 51/53.
HT_PREFIX = bytes([0x04,0x00,0x04,0x00,0x17,0x00,0x00,0x00,0x10,0x00])
HT_MIN = 70


def is_ht_sensor(p: bytes) -> bool:
    return (len(p) >= HT_MIN and p[:10] == HT_PREFIX
            and p[10] in (0x44, 0x45) and p[11] == 0x00)


def s16(p: bytes, off: int) -> int:
    return struct.unpack_from("<h", p, off)[0]


def connect(mac: str) -> socket.socket:
    print(f"connecting to {mac} PSM 0x{AACP_PSM:04X} ...", flush=True)
    last = None
    for attempt in range(5):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_MEDIUM, 0))
        try:
            s.connect((mac, AACP_PSM))
            print(f"L2CAP channel OPEN (attempt {attempt + 1})", flush=True)
            return s
        except OSError as e:
            last = e
            s.close()
            time.sleep(1.0)  # link may still be elevating to encrypted
    raise last


def send_burst(s: socket.socket, burst: str, start_pkt: bytes):
    s.send(HANDSHAKE); print("sent handshake", flush=True); time.sleep(0.2)
    if burst == "full":
        s.send(FEATURE_FLAGS); print("sent feature flags", flush=True); time.sleep(0.2)
        s.send(NOTIFICATIONS); print("sent notifications", flush=True); time.sleep(0.2)
    s.send(start_pkt); print(f"sent START ({len(start_pkt)}B)", flush=True)


def run(mac, variant, burst, seconds):
    variants = {"def": [START_DEF], "alt": [START_ALT], "both": [START_DEF, START_ALT]}[variant]
    s = connect(mac)
    s.settimeout(2.0)
    ht_count = 0
    ear_seen = set()
    for start_pkt in variants:
        print(f"\n=== burst={burst} variant={'DEF' if start_pkt is START_DEF else 'ALT'} ===", flush=True)
        send_burst(s, burst, start_pkt)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                p = s.recv(2048)
            except socket.timeout:
                continue
            if not p:
                continue
            if is_ht_sensor(p):
                ht_count += 1
                h, v = s16(p, 51), s16(p, 53)
                o1, o2, o3 = s16(p, 43), s16(p, 45), s16(p, 47)
                if ht_count % 5 == 1:  # throttle console
                    print(f"HT  h={h:6d} v={v:6d}   o=({o1:6d},{o2:6d},{o3:6d})   [{ht_count} total]",
                          flush=True)
            else:
                op = p[4] if len(p) > 5 else None
                if op == 0x06 and len(p) >= 8:  # ear detection
                    st = p[6:8].hex()
                    if st not in ear_seen:
                        ear_seen.add(st)
                        print(f"  ear-detection: {st}", flush=True)
                elif len(p) > 90:
                    print(f"  big non-HT packet ({len(p)}B): {p[:20].hex()}...", flush=True)
                else:
                    desc = f"op=0x{op:02x}" if op is not None else "runt"
                    print(f"  rx {desc} ({len(p)}B): {p.hex()}", flush=True)
        if ht_count:
            break
    s.close()
    print(f"\nRESULT: {ht_count} head-tracking sensor packets received.", flush=True)
    if ht_count:
        print(">>> SUCCESS - the Pi/BlueZ platform gets head tracking. Bridge it next.", flush=True)
    else:
        print(">>> NO sensor stream (same wall as the Pico). Capture-and-diff needed.", flush=True)
    return 0 if ht_count else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mac")
    # ALT confirmed as the streaming variant on hardware (2026-07-22); DEF
    # emits a different packet family and contaminates the ALT stream if sent
    # first, so default to ALT only.
    ap.add_argument("--variant", choices=["def", "alt", "both"], default="alt")
    ap.add_argument("--burst", choices=["minimal", "full"], default="minimal")
    ap.add_argument("--seconds", type=int, default=15)
    a = ap.parse_args()
    try:
        sys.exit(run(a.mac, a.variant, a.burst, a.seconds))
    except PermissionError:
        print("permission denied opening L2CAP socket - try: sudo python3 ...", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"socket error: {e}", file=sys.stderr)
        sys.exit(2)
