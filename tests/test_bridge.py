"""Unit tests for opentrack_bridge.py -- pure stdlib, no Bluetooth needed.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import math
import os
import pathlib
import socket
import struct
import tempfile
import time
import unittest

_path = pathlib.Path(__file__).resolve().parent.parent / "opentrack_bridge.py"
_spec = importlib.util.spec_from_file_location("opentrack_bridge", _path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def ht_packet(op=0x44, sub=0x00, length=70):
    p = bytearray(length)
    p[:10] = bridge.HT_PREFIX
    p[10], p[11] = op, sub
    return bytes(p)


class WrapS16Test(unittest.TestCase):
    def test_identity_in_range(self):
        for d in (0, 100, -100, 32767, -32768):
            self.assertEqual(bridge.wrap_s16(d), d)

    def test_wraps_across_the_seam(self):
        self.assertEqual(bridge.wrap_s16(40000), -25536)
        self.assertEqual(bridge.wrap_s16(-40000), 25536)
        self.assertEqual(bridge.wrap_s16(65536), 0)


class IsHtTest(unittest.TestCase):
    def test_accepts_both_sensor_opcodes(self):
        self.assertTrue(bridge.is_ht(ht_packet(op=0x44)))
        self.assertTrue(bridge.is_ht(ht_packet(op=0x45)))

    def test_rejects_wrong_opcode_subtype_length_prefix(self):
        self.assertFalse(bridge.is_ht(ht_packet(op=0x46)))
        self.assertFalse(bridge.is_ht(ht_packet(sub=0x01)))
        self.assertFalse(bridge.is_ht(ht_packet(length=69)))
        self.assertFalse(bridge.is_ht(b"\x00" * 70))


class CalibratorTest(unittest.TestCase):
    def test_still_head_calibrates_in_one_batch(self):
        c = bridge.Calibrator()
        noise = [3, -2, 0, 5, -4, 1, 2, -1, 0, 3]
        n = None
        for i in range(bridge.CALIB_N):
            self.assertIsNone(n)
            n = c.feed(1000 + noise[i], -500 + noise[i])
        self.assertIsNotNone(n)
        self.assertLessEqual(abs(n[0] - 1000), 5)
        self.assertLessEqual(abs(n[1] + 500), 5)

    def test_moving_batch_is_discarded_then_still_batch_wins(self):
        c = bridge.Calibrator()
        n = None
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(bridge.CALIB_N):
                n = c.feed(i * 300, i * 300)  # sweeping: spread >> CALIB_STILL_MAX
        self.assertIsNone(n)
        self.assertEqual(c.tries, 1)
        for _ in range(bridge.CALIB_N):
            n = c.feed(2000, 2000)
        self.assertEqual(n, (2000, 2000))

    def test_never_still_accepts_after_the_retry_cap(self):
        c = bridge.Calibrator()
        n, feeds = None, 0
        with contextlib.redirect_stdout(io.StringIO()):
            while n is None:
                n = c.feed(feeds * 300 % 5000, 0)
                feeds += 1
        self.assertEqual(feeds, bridge.CALIB_N * (bridge.CALIB_MAX_TRIES + 1))

    def test_neutral_near_the_seam_keeps_later_deltas_small(self):
        c = bridge.Calibrator()
        seam = [32760, -32766, 32762, -32764, 32765,
                -32767, 32761, -32765, 32763, -32768]
        n = None
        for v in seam:
            n = c.feed(v, 0)
        self.assertIsNotNone(n)
        self.assertLess(abs(bridge.wrap_s16(-32760 - n[0])), 100)


class UdpOutputTest(unittest.TestCase):
    def test_freepie_payload_on_the_wire(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        try:
            out = bridge.UdpOutput("localhost", rx.getsockname()[1])
            self.assertEqual(out.dest[0], "127.0.0.1")  # resolved once, up front
            out.send(10.0, -5.0, 0.0)
            data, _ = rx.recvfrom(64)
        finally:
            rx.close()
        pad, flags, *fl = struct.unpack("<BB12f", data)
        self.assertEqual(flags, 0x02)
        self.assertAlmostEqual(fl[0], math.radians(10.0), places=6)
        self.assertAlmostEqual(fl[1], math.radians(-5.0), places=6)
        self.assertAlmostEqual(fl[2], 0.0, places=6)

    def test_unresolvable_host_falls_back_and_never_raises(self):
        out = bridge.UdpOutput("no.such.host.invalid", 4242)
        self.assertEqual(out.dest[0], "no.such.host.invalid")
        with contextlib.redirect_stdout(io.StringIO()):
            out.send(1.0, 2.0, 3.0)  # drops the frame, must not raise


class SerialHatireOutputTest(unittest.TestCase):
    def test_frame_layout_and_counter(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            out = bridge.SerialHatireOutput(path)
            out.send(1.5, -2.5, 0.0)   # send(yaw, pitch, roll)
            out.send(0.0, 0.0, 0.0)
            data = pathlib.Path(path).read_bytes()
        finally:
            if out.fd is not None:
                os.close(out.fd)
            os.unlink(path)
        self.assertEqual(len(data), 60)  # two 30-byte frames
        begin, cpt, yaw, roll, pitch, tx, ty, tz, end = struct.unpack(
            bridge.HATIRE_FMT, data[:30])
        self.assertEqual((begin, cpt, end),
                         (bridge.HATIRE_BEGIN, 0, bridge.HATIRE_END))
        self.assertAlmostEqual(yaw, 1.5, places=5)
        self.assertAlmostEqual(roll, 0.0, places=5)   # frame order: yaw, roll, pitch
        self.assertAlmostEqual(pitch, -2.5, places=5)
        self.assertEqual(struct.unpack_from("<HH", data, 30)[1], 1)

    def test_counter_rolls_over_at_1000(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            out = bridge.SerialHatireOutput(path)
            out.cpt = 999
            out.send(0.0, 0.0, 0.0)
            self.assertEqual(out.cpt, 0)
        finally:
            if out.fd is not None:
                os.close(out.fd)
            os.unlink(path)

    def test_write_error_drops_the_frame_and_schedules_a_reopen(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            out = bridge.SerialHatireOutput(path)
            os.close(out.fd)  # simulate the device going away under us
            with contextlib.redirect_stdout(io.StringIO()):
                out.send(0.0, 0.0, 0.0)  # must not raise
            self.assertIsNone(out.fd)
        finally:
            os.unlink(path)


class RecenterFlagTest(unittest.TestCase):
    def test_request_consume_cycle(self):
        self.assertFalse(bridge.consume_recenter())
        bridge.request_recenter()
        self.assertTrue(bridge.consume_recenter())
        self.assertFalse(bridge.consume_recenter())


class CommandScannerTest(unittest.TestCase):
    def test_whole_token_matches_and_clears(self):
        sc = bridge.CommandScanner(b"RECENTER")
        self.assertTrue(sc.feed(b"RECENTER"))
        self.assertEqual(len(sc.buf), 0)  # a hit consumes the buffer

    def test_token_split_across_reads(self):
        sc = bridge.CommandScanner(b"RECENTER")
        self.assertFalse(sc.feed(b"\x00\xaaREC"))
        self.assertFalse(sc.feed(b"ENT"))
        self.assertTrue(sc.feed(b"ER\x55"))

    def test_token_inside_binary_garbage(self):
        sc = bridge.CommandScanner(b"RECENTER")
        self.assertTrue(sc.feed(b"\xaa\xaa\x00\nRECENTER\x55\x55"))

    def test_garbage_keeps_the_buffer_bounded(self):
        sc = bridge.CommandScanner(b"RECENTER")
        for _ in range(1000):
            self.assertFalse(sc.feed(b"\x00" * 100))
        self.assertLessEqual(len(sc.buf), len(b"RECENTER") - 1)

    def test_empty_token_never_matches_or_buffers(self):
        sc = bridge.CommandScanner(b"")
        self.assertFalse(sc.feed(b"anything"))
        self.assertEqual(len(sc.buf), 0)


class SerialCommandTest(unittest.TestCase):
    def _make(self, content, token="RECENTER"):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        return bridge.SerialHatireOutput(path, token.encode()), path

    def _cleanup(self, out, path):
        if out.fd is not None:
            os.close(out.fd)
        os.unlink(path)

    def test_token_in_the_rx_stream_recenters_once(self):
        out, path = self._make(b"\xaa\x00garbage RECENTER trailing")
        try:
            self.assertEqual(out.poll_events(), {"recenter"})
            out.next_poll = 0.0
            self.assertEqual(set(out.poll_events()), set())  # already drained
        finally:
            self._cleanup(out, path)

    def test_poll_is_rate_limited_then_catches_up(self):
        out, path = self._make(b"")
        try:
            out.poll_events()  # consumes the cadence slot
            with open(path, "ab") as f:
                f.write(b"RECENTER")
            self.assertEqual(set(out.poll_events()), set())  # inside POLL_SECS
            out.next_poll = 0.0
            self.assertEqual(out.poll_events(), {"recenter"})
        finally:
            self._cleanup(out, path)

    def test_empty_token_skips_reading_entirely(self):
        out, path = self._make(b"RECENTER", token="")
        try:
            self.assertEqual(set(out.poll_events()), set())
        finally:
            self._cleanup(out, path)

    def test_reopen_clears_a_partial_token(self):
        out, path = self._make(b"RECEN")
        try:
            out.poll_events()  # buffers the partial token
            self.assertTrue(out.scanner.buf)
            os.close(out.fd)
            out.fd = None
            out._open()  # a reopen must reset the scanner
            self.assertEqual(len(out.scanner.buf), 0)
        finally:
            self._cleanup(out, path)

    def test_resumed_reads_after_long_stop_recenter_once(self):
        out, path = self._make(b"")
        try:
            out.drop_since = time.monotonic() - out.ARM_SECS - 1.0
            out.send(0.0, 0.0, 0.0)  # write succeeds again -> arm the event
            self.assertIsNone(out.drop_since)
            self.assertEqual(out.poll_events(), {"recenter"})
            out.next_poll = 0.0
            self.assertEqual(set(out.poll_events()), set())  # fired once
        finally:
            self._cleanup(out, path)

    def test_short_hiccup_does_not_recenter(self):
        out, path = self._make(b"")
        try:
            out.drop_since = time.monotonic() - 0.5  # brief stall, not a stop
            out.send(0.0, 0.0, 0.0)
            self.assertIsNone(out.drop_since)  # spell over...
            self.assertEqual(set(out.poll_events()), set())  # ...but no event
        finally:
            self._cleanup(out, path)

    def test_auto_recenter_can_be_disabled(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        out = bridge.SerialHatireOutput(path, b"", auto_recenter=False)
        try:
            out.drop_since = time.monotonic() - out.ARM_SECS - 1.0
            out.send(0.0, 0.0, 0.0)
            self.assertEqual(set(out.poll_events()), set())
        finally:
            self._cleanup(out, path)

    def test_reopen_counts_as_a_stop(self):
        out, path = self._make(b"")
        try:
            os.close(out.fd)  # device dies under us
            with contextlib.redirect_stdout(io.StringIO()):
                out.send(0.0, 0.0, 0.0)  # write fails -> _close()
            self.assertIsNotNone(out.drop_since)  # the spell started
        finally:
            self._cleanup(out, path)

    def test_udp_output_has_no_events(self):
        out = bridge.UdpOutput("127.0.0.1", 4242)
        try:
            self.assertEqual(out.poll_events(), ())
        finally:
            out.sock.close()

    def test_multi_output_unions_events(self):
        class Fake:
            def __init__(self, ev):
                self.ev = ev

            def poll_events(self):
                return self.ev

        m = bridge.MultiOutput([Fake(set()), Fake({"recenter"}), Fake(())])
        self.assertEqual(m.poll_events(), {"recenter"})


if __name__ == "__main__":
    unittest.main()
