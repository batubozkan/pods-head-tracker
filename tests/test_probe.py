"""Unit tests for airpods_ht_probe.py -- the offline-testable pieces.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import importlib.util
import io
import pathlib
import struct
import unittest

_path = pathlib.Path(__file__).resolve().parent.parent / "airpods_ht_probe.py"
_spec = importlib.util.spec_from_file_location("airpods_ht_probe", _path)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def ht(o1=0, o2=0, o3=0, h=0, v=0):
    p = bytearray(probe.HT_MIN)
    p[:10] = probe.HT_PREFIX
    p[10], p[11] = 0x44, 0x00
    struct.pack_into("<hhh", p, 43, o1, o2, o3)
    struct.pack_into("<hh", p, 51, h, v)
    return bytes(p)


class IsHtSensorTest(unittest.TestCase):
    def test_accepts_sensor_packets(self):
        self.assertTrue(probe.is_ht_sensor(ht()))

    def test_rejects_start_and_short_packets(self):
        self.assertFalse(probe.is_ht_sensor(probe.START_ALT))
        self.assertFalse(probe.is_ht_sensor(probe.START_DEF))
        self.assertFalse(probe.is_ht_sensor(ht()[:probe.HT_MIN - 1]))


class CsvLoggerTest(unittest.TestCase):
    def test_header_and_relative_time(self):
        buf = io.StringIO()
        log = probe.CsvLogger(buf)
        log.row(100.0, 1, 2, 3, 4, 5)
        log.row(100.5, -1, -2, -3, -4, -5)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "t,o1,o2,o3,h,v")
        self.assertEqual(lines[1], "0.0000,1,2,3,4,5")
        self.assertEqual(lines[2], "0.5000,-1,-2,-3,-4,-5")

    def test_label_column(self):
        buf = io.StringIO()
        log = probe.CsvLogger(buf, label="roll")
        log.row(0.0, 1, 2, 3, 4, 5)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "t,o1,o2,o3,h,v,label")
        self.assertTrue(lines[1].endswith(",roll"))


if __name__ == "__main__":
    unittest.main()
