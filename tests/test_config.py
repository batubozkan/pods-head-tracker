"""Tests for --config file loading and the CLI > env > file precedence.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

_path = pathlib.Path(__file__).resolve().parent.parent / "opentrack_bridge.py"
_spec = importlib.util.spec_from_file_location("opentrack_bridge_cfg", _path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class LoadConfigTest(unittest.TestCase):
    def _load(self, content):
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as f:
            f.write(content)
            path = f.name
        try:
            return bridge.load_config(path)
        finally:
            os.unlink(path)

    def test_parses_like_environmentfile(self):
        conf = self._load(
            "# a comment\n"
            "\n"
            "AIRPODS_MAC=AA:BB:CC:DD:EE:FF\n"
            "  UDP_HOST = 192.0.2.10  \n"
            "QUOTED=\"with spaces\"\n"
            "SINGLE='also quoted'\n"
            "EMPTY=\n"
            "not a key value line\n")
        self.assertEqual(conf["AIRPODS_MAC"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(conf["UDP_HOST"], "192.0.2.10")
        self.assertEqual(conf["QUOTED"], "with spaces")
        self.assertEqual(conf["SINGLE"], "also quoted")
        self.assertEqual(conf["EMPTY"], "")
        self.assertNotIn("not a key value line", conf)

    def test_keeps_equals_inside_the_value(self):
        self.assertEqual(self._load("RECENTER_CMD=A=B\n"),
                         {"RECENTER_CMD": "A=B"})

    def test_missing_file_fails_loud(self):
        with self.assertRaises(SystemExit):
            bridge.load_config("/nonexistent/pods.conf")


class ConfigPrecedenceTest(unittest.TestCase):
    """CLI > env > file, exercised through main() with run() patched."""

    MAC = "AA:BB:CC:DD:EE:FF"

    def setUp(self):
        fd, self.conf = tempfile.mkstemp(suffix=".conf")
        os.close(fd)
        self.addCleanup(os.unlink, self.conf)
        self._write("AIRPODS_MAC={}\nUDP_HOST=127.0.0.1\nHT_START=def\n"
                    .format(self.MAC))

    def _write(self, content):
        pathlib.Path(self.conf).write_text(content)

    def _main(self, argv, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(bridge, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            bridge.main(argv)
        return run

    def _starts(self, run):
        return [n for n, _ in run.call_args[0][6]]

    def test_file_supplies_everything(self):
        run = self._main(["--config", self.conf])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], self.MAC)
        self.assertEqual(self._starts(run), ["DEF"])

    def test_env_beats_file(self):
        run = self._main(["--config", self.conf], env={"HT_START": "alt"})
        self.assertEqual(self._starts(run), ["ALT"])

    def test_cli_beats_env_and_file(self):
        run = self._main(["--config", self.conf, "--ht-start", "both"],
                         env={"HT_START": "def"})
        self.assertEqual(self._starts(run), ["ALT", "DEF"])

    def test_invalid_file_value_fails_like_a_flag(self):
        self._write("AIRPODS_MAC={}\nUDP_HOST=127.0.0.1\nHT_START=bogus\n"
                    .format(self.MAC))
        with self.assertRaises(SystemExit):
            self._main(["--config", self.conf])

    def test_config_does_not_leak_into_the_next_invocation(self):
        self._main(["--config", self.conf])
        with self.assertRaises(SystemExit):  # no file, no env: MAC missing
            self._main([])


if __name__ == "__main__":
    unittest.main()
