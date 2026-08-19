"""Tests for the bridge's main loop, connect retry, and CLI validation.

run() is an infinite loop by design; these tests drive it with a scripted
fake socket and a fake clock (no Bluetooth, no real sleeps) and end it by
raising _Stop from the script -- run() only catches socket.timeout and
OSError, so the sentinel propagates back to the test.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import contextlib
import errno
import importlib.util
import io
import os
import pathlib
import socket
import struct
import unittest
from unittest import mock

_path = pathlib.Path(__file__).resolve().parent.parent / "opentrack_bridge.py"
_spec = importlib.util.spec_from_file_location("opentrack_bridge_run", _path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class _Stop(Exception):
    """Ends run() from inside a socket script."""


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs

    def advance(self, secs):
        self.now += secs


class FakeSocket:
    """recv() plays back a script, one item per call:

    - bytes: returned as the packet
    - an exception class or instance: raised (socket.timeout, OSError, _Stop)
    - any other callable: invoked mid-stream (advance the clock, set a flag,
      make an assertion); its return value, if not None, is treated as the
      item, otherwise recv moves on to the next one
    - script exhausted: _Stop
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.timeout = None
        self.closed = False

    def send(self, data):
        self.sent.append(bytes(data))

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True

    def recv(self, n):
        while True:
            if not self.script:
                raise _Stop()
            item = self.script.pop(0)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item = item()
                if item is None:
                    continue
            return item

    def starts_sent(self):
        """Names of the START packets sent so far, in order."""
        names = {bytes(bridge.START_ALT): "ALT", bytes(bridge.START_DEF): "DEF"}
        return [names[p] for p in self.sent if p in names]


class FakeOutput:
    def __init__(self):
        self.sent = []
        self.pending = set()  # one-shot poll_events payload

    def send(self, yaw, pitch, roll):
        self.sent.append((yaw, pitch, roll))

    def poll_events(self):
        ev, self.pending = self.pending, set()
        return ev


class FakeNotifier:
    def __init__(self):
        self.statuses = []

    def status(self, text):
        self.statuses.append(text)


def ht(o2=0, o3=0, o1=0, h=0, v=0):
    """A 70-byte sensor packet with the orientation triple and accel pair."""
    p = bytearray(70)
    p[:10] = bridge.HT_PREFIX
    p[10], p[11] = 0x44, 0x00
    struct.pack_into("<hhh", p, 43, o1, o2, o3)
    struct.pack_into("<hh", p, 51, h, v)
    return bytes(p)


EAR_IN = bytes.fromhex("040004000600") + bytes([0x00, 0x00])
EAR_OUT = bytes.fromhex("040004000600") + bytes([0x01, 0x01])
BATTERY = bytes.fromhex("040004000400" "03" "0201640201" "0401630101" "0801110201")
STILL = [ht(o2=1000, o3=-500)] * bridge.CALIB_N  # one clean calibration batch


class RunLoopCase(unittest.TestCase):
    """Base: run_bridge() drives run() through one FakeSocket per script.

    self.clock / self.socks / self.out exist before run() starts, so script
    callables can reference them mid-run (advance time, arm events, assert).
    """

    def setUp(self):
        bridge._recenter = False  # a leftover SIGUSR1 flag would leak between tests

    def run_bridge(self, *scripts, starts=None, recalib_secs=0, **runkw):
        self.clock = FakeClock()
        self.socks = [FakeSocket(s) for s in scripts]
        self.out = FakeOutput()
        self.notifier = FakeNotifier()
        connects = []

        def connect(mac):
            if len(connects) >= len(self.socks):
                raise _Stop()
            connects.append(mac)
            return self.socks[len(connects) - 1]

        battery = bridge.BatteryReporter(0, 0, time_fn=self.clock.monotonic)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(_Stop):
                bridge.run("AA:BB:CC:DD:EE:FF", self.out, recalib_secs, False,
                           self.notifier, battery, starts, connect=connect,
                           clock=self.clock.monotonic, sleep=self.clock.sleep,
                           **runkw)
        self.log = buf.getvalue()
        self.connects = len(connects)


class ConnectionTest(RunLoopCase):
    def test_handshake_notifications_start_on_connect(self):
        self.run_bridge([_Stop])
        self.assertEqual(self.socks[0].sent,
                         [bridge.HANDSHAKE, bridge.REQUEST_NOTIFICATIONS,
                          bridge.START_ALT])

    def test_reconnects_after_link_drop(self):
        self.run_bridge([OSError(errno.ECONNRESET, "reset")], [_Stop])
        self.assertEqual(self.connects, 2)
        self.assertTrue(self.socks[0].closed)
        self.assertIn("link dropped", self.log)
        self.assertIn("reconnecting", " ".join(self.notifier.statuses))
        self.assertIn(2, self.clock.sleeps)
        # The second socket got the full handshake again.
        self.assertEqual(self.socks[1].sent[0], bridge.HANDSHAKE)

    def test_link_drop_during_handshake_reconnects(self):
        # Regression guard for 720e7ae: the send()s before the recv loop
        # must sit inside the reconnect guard too, not escape as an
        # unhandled OSError.
        dead = FakeSocket()
        dead.send = mock.Mock(side_effect=OSError(errno.ENOTCONN, "gone"))
        socks = [dead]
        clock = FakeClock()

        def connect(mac):
            if not socks:
                raise _Stop()
            return socks.pop()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(_Stop):
                bridge.run("AA:BB:CC:DD:EE:FF", FakeOutput(), 0, False,
                           FakeNotifier(),
                           bridge.BatteryReporter(0, 0, time_fn=clock.monotonic),
                           None, connect=connect, clock=clock.monotonic,
                           sleep=clock.sleep)
        self.assertIn("link dropped", buf.getvalue())


class StartWatchdogTest(RunLoopCase):
    def test_resends_start_after_ht_silence(self):
        self.run_bridge([lambda: self.clock.advance(4.1), socket.timeout,
                         _Stop])
        self.assertEqual(self.socks[0].starts_sent(), ["ALT", "ALT"])
        self.assertIn("no data - re-sent start ALT", self.log)
        self.assertIn("idle - waiting for buds in-ear",
                      " ".join(self.notifier.statuses))

    def test_notifications_do_not_reset_the_watchdog(self):
        # Battery/ear packets keep recv fed; the re-send must key off the
        # last head-tracking packet, not off recv timeouts.
        self.run_bridge([BATTERY, lambda: self.clock.advance(4.1), BATTERY,
                         _Stop])
        self.assertEqual(self.socks[0].starts_sent(), ["ALT", "ALT"])

    def test_ear_out_stretches_the_cadence_to_30s(self):
        def push_past_30s():
            self.assertEqual(self.socks[0].starts_sent(), ["ALT"])
            self.clock.advance(26.0)

        self.run_bridge([EAR_OUT, lambda: self.clock.advance(5.0),
                         socket.timeout,   # 5 s of silence: below the 30 s bar
                         push_past_30s,
                         socket.timeout, _Stop])
        self.assertEqual(self.socks[0].starts_sent(), ["ALT", "ALT"])
        self.assertIn("buds out of ear", self.log)

    def test_ht_start_both_alternates_alt_first(self):
        self.run_bridge([lambda: self.clock.advance(4.1), socket.timeout,
                         lambda: self.clock.advance(4.1), socket.timeout,
                         _Stop],
                        starts=bridge.start_variants("both"))
        self.assertEqual(self.socks[0].starts_sent(), ["ALT", "DEF", "ALT"])
        self.assertIn("re-sent start DEF", self.log)

    def test_both_logs_the_variant_that_started_the_stream(self):
        self.run_bridge([lambda: self.clock.advance(4.1), socket.timeout]
                        + [ht(o2=1000, o3=-500)] * 3 + [_Stop],
                        starts=bridge.start_variants("both"))
        self.assertIn("stream started after start DEF", self.log)


class CalibrationAndMathTest(RunLoopCase):
    def test_calibrates_then_streams(self):
        self.run_bridge(STILL + [ht(o2=1000, o3=-500), _Stop])
        self.assertIn("calibrated neutral=", self.log)
        self.assertIn("streaming", " ".join(self.notifier.statuses))
        self.assertEqual(len(self.out.sent), 1)
        yaw, pitch, roll = self.out.sent[0]
        self.assertAlmostEqual(yaw, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)
        self.assertEqual(roll, 0.0)

    def test_pitch_yaw_math_end_to_end(self):
        self.run_bridge(STILL + [ht(o2=1000 + 640, o3=-500 + 320), _Stop])
        yaw, pitch, _ = self.out.sent[-1]
        self.assertAlmostEqual(pitch, (640 + 320) / 2 / bridge.FULL_SCALE * 180.0,
                               places=4)
        self.assertAlmostEqual(yaw, (640 - 320) / 2 / bridge.FULL_SCALE * 180.0,
                               places=4)

    def test_sigusr1_recenter_recaptures_neutral(self):
        self.run_bridge(STILL + [bridge.request_recenter]
                        + [ht(o2=2000, o3=500)] * (bridge.CALIB_N + 1) + [_Stop])
        self.assertIn("recenter requested", self.log)
        self.assertEqual(self.log.count("calibrated neutral="), 2)
        yaw, pitch, _ = self.out.sent[-1]  # the new neutral applies
        self.assertAlmostEqual(yaw, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)

    def test_output_recenter_event_recaptures_neutral(self):
        def arm_event():
            self.out.pending = {"recenter"}

        self.run_bridge(STILL + [arm_event]
                        + [ht(o2=2000, o3=500)] * bridge.CALIB_N + [_Stop])
        self.assertIn("recenter requested", self.log)
        self.assertEqual(self.log.count("calibrated neutral="), 2)

    def test_periodic_recalibration(self):
        self.run_bridge(STILL + [lambda: self.clock.advance(61.0),
                                 ht(o2=1000, o3=-500), _Stop],
                        recalib_secs=60)
        self.assertIn("recalibrating - hold still", self.log)

    def test_battery_packets_reach_reporter_and_status(self):
        self.run_bridge([BATTERY, _Stop])
        self.assertIn("battery: L 99% R 100% case 17% (L charging)", self.log)
        self.assertIn("battery L 99%", " ".join(self.notifier.statuses))


class EarPauseTest(RunLoopCase):
    def test_ear_out_freezes_output_and_holds_the_pose(self):
        self.run_bridge(STILL + [ht(o2=1000, o3=-500),   # one real sample
                                 EAR_OUT,
                                 ht(o2=5000, o3=3000),   # removal garbage
                                 socket.timeout, _Stop])
        self.assertIn("paused - buds out of ear", " ".join(self.notifier.statuses))
        # The garbage sample was swallowed; every send is the held pose.
        self.assertEqual(len(self.out.sent), 3)  # 1 real + 2 keepalives
        for yaw, pitch, _ in self.out.sent:
            self.assertAlmostEqual(yaw, 0.0, places=3)
            self.assertAlmostEqual(pitch, 0.0, places=3)

    def test_ear_out_before_calibration_sends_nothing(self):
        self.run_bridge([EAR_OUT, socket.timeout, _Stop])
        self.assertEqual(self.out.sent, [])

    def test_ear_return_recenters_by_default(self):
        self.run_bridge(STILL + [EAR_OUT, EAR_IN]
                        + [ht(o2=2000, o3=500)] * (bridge.CALIB_N + 1) + [_Stop])
        self.assertIn("buds back in ear - recentering", self.log)
        self.assertEqual(self.log.count("calibrated neutral="), 2)
        yaw, pitch, _ = self.out.sent[-1]  # the new neutral applies
        self.assertAlmostEqual(yaw, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)

    def test_ear_pause_disabled_keeps_streaming(self):
        self.run_bridge(STILL + [EAR_OUT, ht(o2=1000 + 640, o3=-500 + 320),
                                 _Stop],
                        ear_pause=False)
        self.assertNotIn("paused", " ".join(self.notifier.statuses))
        pitch = self.out.sent[-1][1]  # the sample still converts and sends
        self.assertAlmostEqual(pitch, (640 + 320) / 2 / bridge.FULL_SCALE * 180.0,
                               places=4)

    def test_ear_recenter_disabled_keeps_the_old_neutral(self):
        self.run_bridge(STILL + [EAR_OUT, EAR_IN, ht(o2=1000, o3=-500), _Stop],
                        ear_recenter=False)
        self.assertEqual(self.log.count("calibrated neutral="), 1)
        yaw, pitch, _ = self.out.sent[-1]  # old neutral still zeroes the pose
        self.assertAlmostEqual(yaw, 0.0, places=3)
        self.assertAlmostEqual(pitch, 0.0, places=3)


class ConnectL2capTest(unittest.TestCase):
    class _Sock:
        def __init__(self, err=None):
            self.err = err
            self.closed = False

        def connect(self, addr):
            if self.err is not None:
                raise self.err

        def close(self):
            self.closed = True

    def _factory(self, socks):
        it = iter(socks)
        return lambda: next(it)

    def test_backs_off_hard_on_host_down(self):
        socks = [self._Sock(OSError(112, "Host is down")),
                 self._Sock(OSError(112, "Host is down")), self._Sock()]
        sleeps = []
        s = bridge.connect_l2cap("AA:BB:CC:DD:EE:FF",
                                 sock_factory=self._factory(socks),
                                 sleep=sleeps.append)
        self.assertIs(s, socks[2])
        self.assertEqual(sleeps, [10.0, 10.0])
        self.assertTrue(all(k.closed for k in socks[:2]))

    def test_short_backoff_on_other_errors(self):
        socks = [self._Sock(OSError(errno.ECONNREFUSED, "refused")),
                 self._Sock()]
        sleeps = []
        bridge.connect_l2cap("AA:BB:CC:DD:EE:FF",
                             sock_factory=self._factory(socks),
                             sleep=sleeps.append)
        self.assertEqual(sleeps, [1.5])

    def test_gives_up_after_eight_tries(self):
        socks = [self._Sock(OSError(112, "Host is down")) for _ in range(9)]
        sleeps = []
        with self.assertRaises(SystemExit):
            bridge.connect_l2cap("AA:BB:CC:DD:EE:FF",
                                 sock_factory=self._factory(socks),
                                 sleep=sleeps.append)
        self.assertEqual(len(sleeps), 8)
        self.assertFalse(socks[8].closed)  # the ninth socket was never made


class MainArgparseTest(unittest.TestCase):
    """CLI/config validation via main() with run() patched out."""

    MAC = "AA:BB:CC:DD:EE:FF"

    def _main(self, argv, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(bridge, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            bridge.main(argv)
        return run

    def test_valid_udp_invocation_reaches_run(self):
        run = self._main([self.MAC, "127.0.0.1"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], self.MAC)

    def test_serial_transport_needs_no_pc_ip(self):
        run = self._main([self.MAC, "--transport", "serial",
                          "--serial-dev", os.devnull])
        run.assert_called_once()

    def test_rejects_invalid_transport(self):
        with self.assertRaises(SystemExit):
            self._main([self.MAC, "127.0.0.1", "--transport", "bogus"])

    def test_rejects_invalid_ht_start(self):
        with self.assertRaises(SystemExit):
            self._main([self.MAC, "127.0.0.1", "--ht-start", "bogus"])

    def test_rejects_invalid_port(self):
        with self.assertRaises(SystemExit):
            self._main([self.MAC, "127.0.0.1", "--port", "0"])

    def test_rejects_missing_mac(self):
        with self.assertRaises(SystemExit):
            self._main([])

    def test_udp_requires_pc_ip(self):
        with self.assertRaises(SystemExit):
            self._main([self.MAC])

    def test_env_supplies_mac_and_host(self):
        run = self._main([], env={"AIRPODS_MAC": self.MAC,
                                  "UDP_HOST": "127.0.0.1"})
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], self.MAC)

    def test_invalid_env_value_fails_like_a_flag(self):
        with self.assertRaises(SystemExit):
            self._main([], env={"AIRPODS_MAC": self.MAC,
                                "UDP_HOST": "127.0.0.1", "HT_START": "bogus"})

    def test_ht_start_env_selects_the_variants(self):
        run = self._main([], env={"AIRPODS_MAC": self.MAC,
                                  "UDP_HOST": "127.0.0.1", "HT_START": "both"})
        starts = run.call_args[0][6]
        self.assertEqual([n for n, _ in starts], ["ALT", "DEF"])

    def test_ear_knobs_reach_run(self):
        run = self._main([self.MAC, "127.0.0.1"])
        self.assertEqual(run.call_args[0][7:9], (True, True))
        run = self._main([self.MAC, "127.0.0.1", "--no-ear-pause",
                          "--no-ear-recenter"])
        self.assertEqual(run.call_args[0][7:9], (False, False))
        run = self._main([], env={"AIRPODS_MAC": self.MAC,
                                  "UDP_HOST": "127.0.0.1", "EAR_PAUSE": "0"})
        self.assertEqual(run.call_args[0][7:9], (False, True))


if __name__ == "__main__":
    unittest.main()
