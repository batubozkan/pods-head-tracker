#!/usr/bin/env python3
"""AirPods -> OpenTrack head-tracking bridge (runs on the Raspberry Pi).

Streams the AirPods' head orientation over Bluetooth and forwards yaw/pitch
to OpenTrack on the PC over Wi-Fi UDP and/or USB serial. Also reports the
buds' battery in the journal and the unit's systemd status line, and
re-centers on demand (SIGUSR1, or a token OpenTrack sends over the serial
port). Setup: README.md.
"""

import argparse
import math
import os
import signal
import socket
import statistics
import struct
import sys
import time

# --- AACP head-tracking protocol (reverse-engineered, from LibrePods) --------
# Raw L2CAP SEQPACKET to the AirPods on PSM 0x1001. BlueZ only opens this
# channel over an encrypted link, hence BT_SECURITY_MEDIUM. Constants are
# intentionally duplicated with airpods_ht_probe.py so each script stays
# single-file deployable (scp one file to the Pi).
AACP_PSM = 0x1001
SOL_BLUETOOTH, BT_SECURITY, BT_SECURITY_MEDIUM = 274, 4, 2

HANDSHAKE = bytes.fromhex("00000400010002000000000000000000")
# Two START variants exist; which one begins the sensor stream depends on
# the AirPods model/firmware. ALT is confirmed on AirPods 4 (ANC), DEF is
# the LibrePods packets.h variant. HT_START selects one, or "both" to
# alternate until data arrives.
START_ALT = bytes([0x04, 0x00, 0x04, 0x00, 0x17, 0x00, 0x00, 0x00, 0x10, 0x00,
                   0x0F, 0x00, 0x08, 0x73, 0x42, 0x0B, 0x08, 0x10, 0x10, 0x02,
                   0x1A, 0x05, 0x01, 0x40, 0x9C, 0x00, 0x00])
START_DEF = bytes([0x04, 0x00, 0x04, 0x00, 0x17, 0x00, 0x00, 0x00, 0x10, 0x00,
                   0x10, 0x00, 0x08, 0xA1, 0x02, 0x42, 0x0B, 0x08, 0x0E, 0x10,
                   0x02, 0x1A, 0x05, 0x01, 0x40, 0x9C, 0x00, 0x00])
HT_PREFIX = bytes([0x04, 0x00, 0x04, 0x00, 0x17, 0x00, 0x00, 0x00, 0x10, 0x00])


def start_variants(mode):
    """(name, packet) list for an HT_START mode. "both" must keep ALT
    first: DEF sent before ALT makes the buds emit a different packet
    family that contaminates the ALT stream (see airpods_ht_probe.py)."""
    return {"alt": [("ALT", START_ALT)],
            "def": [("DEF", START_DEF)],
            "both": [("ALT", START_ALT), ("DEF", START_DEF)]}[mode]

# Battery / ear-status notifications (formats from LibrePods). The buds push
# them on the same L2CAP channel once REQUEST_NOTIFICATIONS is sent (any time
# after the handshake). They differ from head tracking at byte 4 -- the AACP
# opcode (0x04 battery, 0x06 ear, 0x17 head tracking) -- so is_ht() never
# matches them and vice versa.
REQUEST_NOTIFICATIONS = bytes.fromhex("040004000f00ffffffff")
BATTERY_PREFIX = bytes.fromhex("040004000400")
EAR_PREFIX = bytes.fromhex("040004000600")
BATTERY_COMPONENTS = {0x01: "headset", 0x02: "R", 0x04: "L", 0x08: "case"}
BATTERY_STATUSES = {0x01: "charging", 0x02: "discharging", 0x04: "disconnected"}

# --- Orientation -> yaw/pitch (ported from LibrePods HeadOrientation.kt) -----
# Neutral ("facing forward") pose: median of CALIB_N consecutive samples
# taken while the head is still (see Calibrator), captured right after each
# connect -- face forward and hold still until "calibrated" is logged.
CALIB_N = 10
CALIB_STILL_MAX = 800  # max batch spread (raw units, ~4.5 deg) to count as still
CALIB_MAX_TRIES = 6    # batches discarded for movement before accepting anyway
FULL_SCALE = 32000.0   # raw orientation units mapped to 180 degrees

# --- OpenTrack "Hatire Arduino" frame (serial transport) ---------------------
# 30 bytes little-endian: {uint16 0xAAAA; uint16 counter 0-999; float32
# rot[3]; float32 trans[3]; uint16 0x5555}. Angles in DEGREES, rot ordered
# yaw/roll/pitch to match OpenTrack's default axis mapping -- users leave the
# Hatire settings dialog untouched.
HATIRE_FMT = "<HH6fH"
HATIRE_BEGIN, HATIRE_END = 0xAAAA, 0x5555


def s16(p, off):
    return struct.unpack_from("<h", p, off)[0]


def wrap_s16(d):
    """Wrap a difference of two int16 readings back into [-32768, 32768).

    The raw orientation values live on a 16-bit circle (FULL_SCALE 32000 =
    180 deg, so the seam sits at ~184 deg). A neutral pose calibrated near
    that seam would otherwise turn a small head move into a ~360 deg jump.
    Identity for in-range differences."""
    return (d + 32768.0) % 65536.0 - 32768.0


class Calibrator:
    """Neutral-pose capture: the median of CALIB_N consecutive samples taken
    while the head is still.

    A batch that spreads more than CALIB_STILL_MAX (the head was moving) is
    discarded and collection restarts -- at most CALIB_MAX_TRIES times, then
    the next batch is accepted regardless, so a fidgety start can only delay
    tracking, never block it. Samples are folded into wrap-safe deltas from
    the batch's first sample and reduced by median, so one outlier or a raw
    origin near the +/-32768 seam can't skew the result."""

    def __init__(self):
        self.ref = None
        self.samples = []
        self.tries = 0

    def feed(self, *vals):
        """Add one sample (any number of axes); returns the neutral tuple
        once captured."""
        if self.ref is None:
            self.ref = vals
        self.samples.append(tuple(wrap_s16(v - r)
                                  for v, r in zip(vals, self.ref)))
        if len(self.samples) < CALIB_N:
            return None
        axes = list(zip(*self.samples))
        spread = max(max(a) - min(a) for a in axes)
        if spread > CALIB_STILL_MAX and self.tries < CALIB_MAX_TRIES:
            self.tries += 1
            self.ref, self.samples = None, []
            print("moving during calibration - retrying (face forward, hold still)",
                  flush=True)
            return None
        return tuple(r + statistics.median(a) for r, a in zip(self.ref, axes))


def is_ht(p):
    return len(p) >= 70 and p[:10] == HT_PREFIX and p[10] in (0x44, 0x45) and p[11] == 0


def decode_battery(p):
    """Decode a battery notification into {component: (level, status)}.

    Layout (per LibrePods): prefix, a record count, then count 5-byte
    records of [component, 0x01, level, status, 0x01]. A disconnected
    component carries no data and is skipped. Anything malformed decodes
    to None -- the caller drops it silently, like every other unknown
    packet on the channel."""
    if len(p) < 7 or p[:6] != BATTERY_PREFIX:
        return None
    count = p[6]
    if count > 3 or len(p) != 7 + 5 * count:
        return None
    out = {}
    for off in range(7, 7 + 5 * count, 5):
        comp, spacer, level, status, end = p[off:off + 5]
        if spacer != 0x01 or end != 0x01:
            return None
        if status == 0x04:  # disconnected
            continue
        name = BATTERY_COMPONENTS.get(comp)
        if name and level <= 100:
            out[name] = (level, BATTERY_STATUSES.get(status, "?"))
    return out


def decode_ear(p):
    """Decode an ear-detection notification into the two raw pod states:
    0x00 in ear, 0x01 out of ear, 0x02 in case. The bytes are ordered
    primary/secondary, NOT left/right -- roles swap when the primary pod is
    removed, so only "is any byte 0x00" is safe to interpret."""
    if len(p) < 8 or p[:6] != EAR_PREFIX:
        return None
    return p[6], p[7]


# --- Runtime re-centering ------------------------------------------------------
# The neutral pose can be re-captured without restarting the service:
# SIGUSR1 (sudo systemctl kill --signal=SIGUSR1 pods-head-tracker), or the
# RECENTER_CMD token arriving on the serial port from OpenTrack's Hatire
# dialog. Both only set a flag; run() consumes it between samples.
_recenter = False


def request_recenter(signum=None, frame=None):
    global _recenter
    _recenter = True


def consume_recenter():
    global _recenter
    was, _recenter = _recenter, False
    return was


class CommandScanner:
    """Finds a byte token in a stream that arrives in arbitrary chunks.

    Keeps the last len(token)-1 bytes between feeds, so a token split
    across reads still matches while garbage can never grow the buffer.
    A hit clears the buffer -- one token on the wire fires once."""

    def __init__(self, token):
        self.token = bytes(token)
        self.buf = bytearray()

    def reset(self):
        del self.buf[:]

    def feed(self, chunk):
        if not self.token:
            return False
        self.buf += chunk
        if self.token in self.buf:
            self.reset()
            return True
        keep = len(self.token) - 1
        if len(self.buf) > keep:
            del self.buf[:len(self.buf) - keep]
        return False


def _l2cap_socket():
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_MEDIUM, 0))
    return s


def connect_l2cap(mac, sock_factory=_l2cap_socket, sleep=time.sleep):
    """Open the encrypted AACP L2CAP channel, retrying while the link
    re-establishes from the stored bond. sock_factory and sleep only exist
    as injection points for the tests -- no Bluetooth on the CI box."""
    last = None
    for _ in range(8):
        s = sock_factory()
        try:
            s.connect((mac, AACP_PSM))
            return s
        except OSError as e:
            last = e
            s.close()
            # errno 112 (Host is down) = we paged, nobody answered: the buds
            # are in the case. Back off hard -- every page is ~5 s of owning
            # the shared 2.4 GHz radio, and a tight retry loop starves Wi-Fi
            # (SSH dies while the buds are away). Reconnect stays fast:
            # AirPods leaving the case page their bonded hosts themselves,
            # so the next attempt rides that link without paging at all.
            sleep(10.0 if e.errno == 112 else 1.5)
    raise SystemExit(f"could not open L2CAP channel: {last}")


class _Output:
    """Outputs drop frames on error -- send() never raises, so a transport
    hiccup can't bounce the Bluetooth link -- and rate-limit their logging."""

    WARN_SECS = 5.0
    _last_warn = 0.0

    def _warn(self, msg):
        now = time.monotonic()
        if now - self._last_warn >= self.WARN_SECS:
            self._last_warn = now
            print(msg, flush=True)

    def poll_events(self):
        """Transport-borne events ("recenter"); most outputs have none."""
        return ()


class UdpOutput(_Output):
    """OpenTrack "FreePIE UDP receiver" sender.

    Payload (50 bytes): {uint8 pad; uint8 flags=0x02 (orientation); float32
    fl[12]} with yaw/pitch/roll in fl[0..2] in RADIANS (OpenTrack converts
    back to degrees). OpenTrack's receiver port must match UDP_PORT/--port.
    """

    def __init__(self, host, port):
        # Resolve a hostname once here instead of letting sendto() re-resolve
        # it on every frame (dozens of lookups per second on a Pi Zero). If
        # resolution fails now -- DNS not up yet at boot -- keep the name and
        # let the per-send warning path handle it as before.
        try:
            host = socket.getaddrinfo(host, port, socket.AF_INET,
                                      socket.SOCK_DGRAM)[0][4][0]
        except OSError:
            pass
        self.dest = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, yaw, pitch, roll):
        fl = [0.0] * 12
        fl[0] = math.radians(yaw)
        fl[1] = math.radians(pitch)
        fl[2] = math.radians(roll)
        try:
            self.sock.sendto(struct.pack("<BB12f", 0, 0x02, *fl), self.dest)
        except OSError as e:
            self._warn(f"udp: send to {self.dest[0]}:{self.dest[1]} failed ({e}) - dropping frames")


class SerialHatireOutput(_Output):
    """Hatire-frame writer for the USB serial gadget (/dev/ttyGS0).

    The device is opened non-blocking and switched to raw mode -- the default
    tty line discipline rewrites any 0x0A byte inside the binary floats to
    0x0D 0x0A, corrupting frames. EAGAIN (Windows isn't reading the COM port)
    drops the frame; any other error closes the device and re-opens it at
    most once a second, so an unplugged cable never stalls tracking.

    The port is also read (poll_events): OpenTrack's Hatire dialog can send
    ASCII commands back over the same COM port, and a recenter_cmd token
    seen in that stream re-centers the bridge.

    With auto_recenter, pressing Start in OpenTrack re-centers by itself:
    while no PC program reads the COM port, writes fail with EAGAIN, so
    "frames were being dropped for a while and now go through again" IS
    "OpenTrack (re)started reading" -- no token to configure. The ARM_SECS
    minimum spell keeps a brief PC stall (reader hiccup, not a stop) from
    re-centering mid-game. UDP has no equivalent: FreePIE is one-way, and
    a host firewall eats the ICMP errors that would reveal a closed port.
    """

    REOPEN_SECS = 1.0
    POLL_SECS = 0.2
    ARM_SECS = 3.0

    def __init__(self, dev, recenter_cmd=b"", auto_recenter=True):
        self.dev = dev
        self.fd = None
        self.cpt = 0
        self.next_open = 0.0
        self.next_poll = 0.0
        self.scanner = CommandScanner(recenter_cmd)
        self.auto_recenter = auto_recenter
        self.drop_since = None  # start of the current not-being-read spell
        self._resumed = False
        self._open()

    def _open(self):
        self.next_open = time.monotonic() + self.REOPEN_SECS
        # A fresh port must start with a clean scanner: bytes read before a
        # reopen must not combine with new ones into a phantom command.
        self.scanner.reset()
        try:
            # O_RDWR, not O_WRONLY: the PC writes command tokens into the
            # same port (see poll_events). O_NONBLOCK is absent on non-Unix
            # dev boxes (send() still never blocks there because a regular
            # file can't); O_BINARY is the Windows dev-box equivalent of raw.
            fd = os.open(self.dev, os.O_RDWR
                         | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_BINARY", 0))
        except OSError as e:
            self._warn(f"serial: cannot open {self.dev} ({e}) - will keep retrying")
            return
        try:
            import tty
            tty.setraw(fd)  # binary-safe: stop ONLCR mangling 0x0A bytes
        except Exception:
            # Non-Unix dev box, or SERIAL_DEV pointed at a plain file/FIFO for
            # testing -- raw mode only matters for the real gadget port.
            pass
        self.fd = fd

    def _close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        self.next_open = time.monotonic() + self.REOPEN_SECS
        # A dead port is also "nobody is reading": when writes succeed again
        # after a reopen (cable back, OpenTrack restarted), the resume
        # detection below applies just the same.
        if self.drop_since is None:
            self.drop_since = time.monotonic()

    def send(self, yaw, pitch, roll):
        if self.fd is None:
            if time.monotonic() < self.next_open:
                return
            self._open()
            if self.fd is None:
                return
        frame = struct.pack(HATIRE_FMT, HATIRE_BEGIN, self.cpt,
                            yaw, roll, pitch, 0.0, 0.0, 0.0, HATIRE_END)
        self.cpt = (self.cpt + 1) % 1000
        try:
            # A short write tears one frame; OpenTrack resyncs on the markers.
            os.write(self.fd, frame)
        except BlockingIOError:
            if self.drop_since is None:
                self.drop_since = time.monotonic()
            self._warn("serial: host not reading (OpenTrack stopped?) - dropping frames")
        except OSError as e:
            self._warn(f"serial: write failed ({e}) - reopening {self.dev}")
            self._close()
        else:
            if self.drop_since is not None:
                # Frames flow again after a real stop (not just a hiccup):
                # OpenTrack was started -- the natural moment to re-center.
                if self.auto_recenter and \
                        time.monotonic() - self.drop_since >= self.ARM_SECS:
                    self._resumed = True
                self.drop_since = None

    def poll_events(self):
        """Report {"recenter"} when the host started reading again after a
        stop (auto_recenter) or when the recenter_cmd token arrived on the
        port. Cheap to call per sample -- actual reads are limited to one
        drain per POLL_SECS."""
        events = set()
        if self._resumed:
            self._resumed = False
            events.add("recenter")
        if self.fd is None or not self.scanner.token:
            return events
        now = time.monotonic()
        if now < self.next_poll:
            return events
        self.next_poll = now + self.POLL_SECS
        for _ in range(64):  # bounds one drain; a tty buffers far less
            try:
                chunk = os.read(self.fd, 256)
            except BlockingIOError:
                break
            except OSError as e:
                self._warn(f"serial: read failed ({e}) - reopening {self.dev}")
                self._close()
                break
            if not chunk:  # EOF: SERIAL_DEV points at a plain file (tests)
                break
            if self.scanner.feed(chunk):
                events.add("recenter")
        return events


class MultiOutput:
    def __init__(self, outputs):
        self.outputs = outputs

    def send(self, yaw, pitch, roll):
        for o in self.outputs:
            o.send(yaw, pitch, roll)

    def poll_events(self):
        ev = set()
        for o in self.outputs:
            ev |= set(o.poll_events())
        return ev


class BatteryReporter:
    """Battery logging that respects the journal: one line on any change, a
    heartbeat re-log every log_secs (0 = changes only), and a low-battery
    warning per bud at warn_pct that re-arms only after a further
    REWARN_DROP points down (or a charge) -- a level sitting on the
    threshold can't spam."""

    REWARN_DROP = 5

    def __init__(self, log_secs, warn_pct, time_fn=time.monotonic):
        self.log_secs = log_secs
        self.warn_pct = warn_pct
        self.time_fn = time_fn
        self.state = {}    # component -> (level, status), merged over packets
        self.last_log = None
        self.warned = {}   # component -> level at the last warning

    def update(self, components):
        # Merge: a packet may carry any subset of components (e.g. just the
        # case), and the summary should keep showing the rest.
        state = dict(self.state)
        state.update(components)
        now = self.time_fn()
        heartbeat = (self.log_secs and self.last_log is not None
                     and now - self.last_log >= self.log_secs)
        if state != self.state or self.last_log is None or heartbeat:
            print(f"battery: {self._fmt(state)}", flush=True)
            self.last_log = now
        self.state = state
        self._warn_low()

    def _warn_low(self):
        for name, (level, status) in self.state.items():
            if name == "case" or status == "charging" or level > self.warn_pct:
                self.warned.pop(name, None)  # recovered or charging: re-arm
                continue
            if not self.warn_pct or status != "discharging":
                continue
            last = self.warned.get(name)
            if last is None or level <= last - self.REWARN_DROP:
                self.warned[name] = level
                print(f"battery LOW: {name} at {level}%", flush=True)

    def summary(self):
        """Short form for the systemd status line; None before any data."""
        return self._fmt(self.state) if self.state else None

    @staticmethod
    def _fmt(state):
        names = [n for n in ("headset", "L", "R", "case") if n in state]
        text = " ".join(f"{n} {state[n][0]}%" for n in names)
        charging = [n for n in names if state[n][1] == "charging"]
        if charging:
            text += f" ({', '.join(charging)} charging)"
        return text


class SdNotifier:
    """systemd STATUS= updates over $NOTIFY_SOCKET (sd_notify), stdlib-only.

    No socket in the environment (manual run), no AF_UNIX (dev box), or a
    send error all degrade to a no-op -- the status line is decoration and
    must never take the bridge down. Unchanged texts are not re-sent. Only
    STATUS= is used; Type=simple semantics are untouched (the unit grants
    access with NotifyAccess=main, no Type=notify)."""

    def __init__(self, addr=None):
        if addr is None:
            addr = os.environ.get("NOTIFY_SOCKET", "")
        self.sock, self.addr, self.last = None, None, None
        if not addr or not hasattr(socket, "AF_UNIX"):
            return
        if addr[0] == "@":  # Linux abstract socket namespace
            addr = "\0" + addr[1:]
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addr = addr
        except OSError:
            self.sock = None

    def status(self, text):
        if self.sock is None or text == self.last:
            return
        self.last = text
        try:
            self.sock.sendto(b"STATUS=" + text.encode() + b"\n", self.addr)
        except OSError:
            pass


def format_status(state, battery=None):
    """The unit's Status: line -- current state, plus battery when known."""
    return f"{state} | battery {battery}" if battery else state


# --- Main loop ---------------------------------------------------------------
# Connect, start the stream, convert each sample, hand it to the output.
# Reconnects from scratch whenever the Bluetooth link drops (e.g. the AirPods
# go back in the case).
def run(mac, output, recalib_secs, verbose, notifier, battery, starts=None,
        ear_pause=True, ear_recenter=True, roll_axis=False,
        *, connect=connect_l2cap, clock=time.monotonic, sleep=time.sleep):
    # connect/clock/sleep are injection points for the tests (fake socket,
    # fake time); the defaults are the real thing.
    starts = starts or start_variants("alt")
    state = "starting"

    def set_status(new=None):
        nonlocal state
        if new is not None:
            state = new
        notifier.status(format_status(state, battery.summary()))

    while True:
        set_status("connecting to AirPods")
        s = connect(mac)
        print("L2CAP open -> streaming pitch/yaw", flush=True)
        calib, neutral, last_calib = Calibrator(), None, clock()
        in_ear, dry, start_i = None, False, 0
        held, paused = None, False  # last sent pose; ear-out freeze
        try:
            # Inside the reconnect guard: the link can drop this early too
            # (buds straight back into the case), and that has to loop back
            # to a reconnect, not escape as an unhandled OSError.
            s.send(HANDSHAKE)
            sleep(0.3)
            s.send(REQUEST_NOTIFICATIONS)  # battery + ear-status pushes
            s.send(starts[start_i][1])
            s.settimeout(2)
            last_ht = last_start = clock()
            set_status("calibrating - hold still, face forward")
            while True:
                try:
                    p = s.recv(2048)
                except socket.timeout:
                    p = None
                now = clock()

                # Runtime re-center: SIGUSR1 or the serial RECENTER_CMD token.
                if consume_recenter() or "recenter" in output.poll_events():
                    neutral, calib, last_calib = None, Calibrator(), now
                    print("recenter requested - face forward, hold still",
                          flush=True)
                    set_status("recalibrating - hold still")

                # The stream is dry. AirPods only stream head tracking while
                # in-ear, and won't begin on a start command that was sent
                # while they were in the case -- so re-send it periodically
                # to catch the buds being put in after the channel opened
                # (mirrors the Pico firmware's watchdog). Keyed off the last
                # head-tracking packet, NOT recv timeouts: battery/ear
                # notifications keep recv fed without any tracking flowing.
                # When the buds are known out-of-ear the cadence stretches --
                # a missed ear event can only delay the start, never block it.
                if now - last_ht >= 4.0 and \
                        now - last_start >= (30.0 if in_ear is False else 4.0):
                    start_i = (start_i + 1) % len(starts)
                    s.send(starts[start_i][1])
                    last_start = now
                    dry = True
                    print(f"no data - re-sent start {starts[start_i][0]} "
                          "(buds in-ear?)", flush=True)
                    set_status("idle - waiting for buds in-ear")

                # While paused the last pose is re-sent at loop-wake cadence
                # (bounded by the 2 s recv timeout): both transports then
                # behave exactly like on stream silence -- the view holds --
                # and the serial output's resume detection stays fed.
                if paused and held is not None:
                    output.send(*held)

                if p is None:
                    continue
                if not is_ht(p):
                    levels = decode_battery(p)
                    if levels is not None:
                        if levels:
                            battery.update(levels)
                            set_status()  # refresh the battery part
                        continue
                    ear = decode_ear(p)
                    if ear is not None:
                        was, in_ear = in_ear, 0x00 in ear
                        # Freeze only when NO bud is in an ear: the bytes are
                        # primary/secondary (roles swap), so "any byte 0x00"
                        # is the only safe predicate, and one bud still in
                        # keeps streaming valid data.
                        paused = ear_pause and in_ear is False
                        if was is not in_ear and not in_ear:
                            print("buds out of ear (case?)", flush=True)
                            if paused:
                                set_status("paused - buds out of ear")
                        elif was is False and in_ear:
                            # Out -> in: re-seating a bud moves the sensor on
                            # the head, so a pre-removal neutral is stale.
                            if ear_recenter and neutral is not None:
                                neutral, calib, last_calib = None, Calibrator(), now
                                print("buds back in ear - recentering, face "
                                      "forward and hold still", flush=True)
                                set_status("recalibrating - hold still")
                            else:
                                print("buds in ear", flush=True)
                                set_status("streaming" if neutral is not None
                                           else "calibrating - hold still, "
                                                "face forward")
                        elif was is not in_ear:
                            print("buds in ear", flush=True)
                    continue
                last_ht = now
                if dry:
                    dry = False
                    if len(starts) > 1:
                        # With HT_START=both the journal must show which
                        # variant this model answered -- that is the whole
                        # point of the mode. Stick to the winner for the
                        # rest of the process: reconnects send it first and
                        # never alternate again. Not written back to the
                        # config -- the unit runs with ProtectSystem=full
                        # (/etc is read-only here), and the config belongs
                        # to the user; hence the hint.
                        name = starts[start_i][0]
                        print(f"stream started after start {name}", flush=True)
                        print(f"hint: set HT_START={name.lower()} in "
                              "/etc/pods-head-tracker.conf to make this "
                              "permanent", flush=True)
                        starts = [starts[start_i]]
                        start_i = 0
                    set_status("streaming" if neutral is not None
                               else "calibrating - hold still, face forward")
                if paused:
                    # Sensor data during the removal transition is garbage
                    # (the pull itself is motion). It must not reach the
                    # calibrator or the outputs -- but it did update last_ht
                    # above, so the START watchdog stays quiet.
                    continue
                # Orientation triple sits at offsets 43/45/47. The combined
                # axes at 45/47 make pitch/yaw; o1 at 43 rides through the
                # same calibration and feeds the experimental roll (--roll).
                o1, o2, o3 = s16(p, 43), s16(p, 45), s16(p, 47)

                if neutral is None:
                    neutral = calib.feed(o1, o2, o3)
                    if neutral is not None:
                        print("calibrated neutral=({:.0f}, {:.0f}, {:.0f})"
                              .format(*neutral), flush=True)
                        set_status("streaming")
                    continue

                # Optional periodic recalibration (drift correction).
                if recalib_secs and (clock() - last_calib) >= recalib_secs:
                    neutral, calib, last_calib = None, Calibrator(), clock()
                    print("recalibrating - hold still, face forward", flush=True)
                    set_status("recalibrating - hold still")
                    continue

                o1n = wrap_s16(o1 - neutral[0])
                o2n = wrap_s16(o2 - neutral[1])
                o3n = wrap_s16(o3 - neutral[2])
                pitch = (o2n + o3n) / 2 / FULL_SCALE * 180.0  # degrees
                yaw = (o2n - o3n) / 2 / FULL_SCALE * 180.0
                # Roll, confirmed on hardware (AirPods 4 ANC, probe --csv
                # protocol, 2026-08-21): o1 carries pitch+roll while
                # (o2+o3)/2 carries pitch-roll, so the half-difference
                # isolates roll. Measured: ~0.2 deg std while still, 3-4 deg
                # during pure yaw/pitch sweeps, +/-40 deg on ear-to-shoulder
                # tilts. Positive = right ear toward right shoulder.
                roll = ((o1n - (o2n + o3n) / 2) / 2 / FULL_SCALE * 180.0
                        if roll_axis else 0.0)
                held = (yaw, pitch, roll)
                output.send(*held)
                if verbose:
                    print(f"yaw={yaw:7.2f}  pitch={pitch:7.2f}  "
                          f"roll={roll:7.2f}", flush=True)
        except OSError as e:
            print(f"link dropped ({e}); reconnecting...", flush=True)
            set_status("reconnecting")
            try:
                s.close()
            except OSError:
                pass
            sleep(2)


# --- Configuration -----------------------------------------------------------
# Precedence: CLI arguments > environment variables > --config file, layered
# by explicitness -- the file is the persistent baseline, the environment is
# what the invoking context says right now (the systemd service feeds
# /etc/pods-head-tracker.conf through it via EnvironmentFile=, so its
# ExecStart needs no arguments), and a typed flag is the most deliberate.
# --config exists for manual runs outside systemd, where nothing else would
# read the conf file.
_config = {}


def load_config(path):
    """Read a KEY=VALUE file the way systemd's EnvironmentFile does: blank
    lines and # comments skipped, one layer of matching quotes stripped.
    An unreadable file is a loud SystemExit -- an explicitly requested
    config is a setup error when missing, not a soft default."""
    conf = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                conf[key.strip()] = val
    except OSError as e:
        raise SystemExit(f"cannot read config file {path}: {e}")
    return conf


def env(name, default=None):
    v = os.environ.get(name, "").strip()
    if v:
        return v
    v = _config.get(name, "").strip()
    return v if v else default


def env_num(name, default, cast):
    raw = env(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise SystemExit(f"invalid {name}={raw!r} (check /etc/pods-head-tracker.conf)")


def main(argv=None):
    # --config must be loaded before the main parser is built: its defaults
    # call env() at construction time. Hence a bare pre-pass for that one
    # flag; the main parser re-registers it so --help shows it.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    _config.clear()
    conf_path = pre.parse_known_args(argv)[0].config
    if conf_path:
        _config.update(load_config(conf_path))

    ap = argparse.ArgumentParser(
        description="AirPods -> OpenTrack head-tracking bridge",
        epilog="Defaults come from environment variables (AIRPODS_MAC, "
               "TRANSPORT, UDP_HOST, UDP_PORT, SERIAL_DEV, HT_START, "
               "RECALIBRATE_SECS, VERBOSE); the systemd service loads them "
               "from /etc/pods-head-tracker.conf.")
    ap.add_argument("mac", nargs="?", default=env("AIRPODS_MAC"),
                    help="AirPods Bluetooth Classic MAC [env AIRPODS_MAC]")
    ap.add_argument("pc_ip", nargs="?", default=env("UDP_HOST"),
                    help="PC's IP for the udp transport [env UDP_HOST]")
    ap.add_argument("--config", metavar="FILE",
                    help="read KEY=VALUE settings from FILE (same format as "
                         "/etc/pods-head-tracker.conf) -- for manual runs "
                         "outside systemd; flags and environment variables "
                         "override it")
    ap.add_argument("--transport", default=env("TRANSPORT", "udp"),
                    help="udp, serial, or both [env TRANSPORT] (default: udp)")
    ap.add_argument("--port", type=int, default=env_num("UDP_PORT", "4242", int),
                    help="OpenTrack FreePIE UDP port [env UDP_PORT] (default: 4242)")
    ap.add_argument("--serial-dev", default=env("SERIAL_DEV", "/dev/ttyGS0"),
                    help="USB gadget serial device [env SERIAL_DEV] (default: /dev/ttyGS0)")
    ap.add_argument("--ht-start", default=env("HT_START", "alt"),
                    help="head-tracking START packet: alt, def, or both "
                         "(alternate until data arrives; use if the stream "
                         "never starts on your model) [env HT_START] "
                         "(default: alt)")
    ap.add_argument("--recalibrate-secs", type=float,
                    default=env_num("RECALIBRATE_SECS", "0", float),
                    help="periodically re-zero the neutral pose (0 = never) [env RECALIBRATE_SECS]")
    ap.add_argument("--recenter-cmd", default=env("RECENTER_CMD", "RECENTER"),
                    help="token that re-centers when the PC sends it over the "
                         "serial port; empty disables [env RECENTER_CMD] "
                         "(default: RECENTER)")
    ap.add_argument("--no-recenter-on-start", dest="recenter_on_start",
                    action="store_false",
                    help="do not re-center automatically when the PC starts "
                         "reading the serial port [env RECENTER_ON_START]")
    ap.add_argument("--no-ear-pause", dest="ear_pause", action="store_false",
                    help="keep converting sensor data even while no bud is "
                         "in an ear [env EAR_PAUSE]")
    ap.add_argument("--no-ear-recenter", dest="ear_recenter",
                    action="store_false",
                    help="do not re-capture the neutral pose when the buds "
                         "return to the ears [env EAR_RECENTER]")
    ap.add_argument("--roll", dest="roll_axis", action="store_true",
                    help="derive and send roll (confirmed on AirPods 4; "
                         "verify once on other models, see README) "
                         "[env ROLL]")
    ap.add_argument("--no-roll", dest="roll_axis", action="store_false",
                    help="override ROLL=1 from the config")
    ap.add_argument("--battery-log-secs", type=float,
                    default=env_num("BATTERY_LOG_SECS", "900", float),
                    help="battery heartbeat interval; changes always log, "
                         "0 = changes only [env BATTERY_LOG_SECS] (default: 900)")
    ap.add_argument("--battery-warn-pct", type=int,
                    default=env_num("BATTERY_WARN_PCT", "20", int),
                    help="low-battery warning threshold, 0 = off "
                         "[env BATTERY_WARN_PCT] (default: 20)")
    ap.add_argument("--verbose", action="store_true",
                    help="log every sample [env VERBOSE]")
    ap.add_argument("--no-verbose", dest="verbose", action="store_false",
                    help="override VERBOSE=1 from the config")
    def env_flag(name, default):
        return env(name, default).lower() in ("1", "true", "yes", "on")

    ap.set_defaults(verbose=env_flag("VERBOSE", "0"),
                    recenter_on_start=env_flag("RECENTER_ON_START", "1"),
                    ear_pause=env_flag("EAR_PAUSE", "1"),
                    ear_recenter=env_flag("EAR_RECENTER", "1"),
                    roll_axis=env_flag("ROLL", "0"))
    a = ap.parse_args(argv)

    # Validated by hand, not argparse choices=, so bad values coming from the
    # config file (which bypass argparse) get the same clear error.
    if a.transport not in ("udp", "serial", "both"):
        ap.error(f"invalid transport {a.transport!r} - use udp, serial, or both")
    if a.ht_start not in ("alt", "def", "both"):
        ap.error(f"invalid HT_START {a.ht_start!r} - use alt, def, or both")
    if not 1 <= a.port <= 65535:
        ap.error(f"invalid UDP_PORT {a.port} - must be 1-65535")
    if not a.mac:
        ap.error("AirPods MAC not set - pass it on the command line or set "
                 "AIRPODS_MAC in /etc/pods-head-tracker.conf")
    if a.transport in ("udp", "both") and not a.pc_ip:
        ap.error("PC IP not set - pass it on the command line, set UDP_HOST in "
                 "/etc/pods-head-tracker.conf, or use --transport serial")
    if a.battery_log_secs < 0:
        ap.error(f"invalid BATTERY_LOG_SECS {a.battery_log_secs} - must be >= 0")
    if not 0 <= a.battery_warn_pct <= 100:
        ap.error(f"invalid BATTERY_WARN_PCT {a.battery_warn_pct} - must be 0-100")
    try:
        # OpenTrack sends commands verbatim as Latin-1 bytes, so that is the
        # encoding the token is matched in.
        recenter_cmd = a.recenter_cmd.encode("latin-1")
    except UnicodeEncodeError:
        ap.error(f"invalid RECENTER_CMD {a.recenter_cmd!r} - use plain ASCII")

    outputs = []
    if a.transport in ("udp", "both"):
        outputs.append(UdpOutput(a.pc_ip, a.port))
        print(f"output: FreePIE UDP -> {a.pc_ip}:{a.port}", flush=True)
    if a.transport in ("serial", "both"):
        outputs.append(SerialHatireOutput(a.serial_dev, recenter_cmd,
                                          a.recenter_on_start))
        print(f"output: Hatire serial -> {a.serial_dev}", flush=True)
    out = outputs[0] if len(outputs) == 1 else MultiOutput(outputs)

    # Registered here, not at import time: the tests import this module on
    # platforms without SIGUSR1.
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, request_recenter)

    try:
        run(a.mac, out, a.recalibrate_secs, a.verbose,
            SdNotifier(), BatteryReporter(a.battery_log_secs, a.battery_warn_pct),
            start_variants(a.ht_start), a.ear_pause, a.ear_recenter,
            a.roll_axis)
    except KeyboardInterrupt:
        pass
    except PermissionError:
        print("permission denied - run with sudo", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
