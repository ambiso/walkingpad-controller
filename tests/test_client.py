"""CLI request shapes for optional auto-stop controls."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wp_client import build_parser, build_request


class AutoStopClientTests(unittest.TestCase):
    def test_query_toggle_and_duration(self):
        for arguments, value, seconds in [
                ([], None, None), (["on"], "on", None),
                (["off"], "off", None), (["on", "30"], "on", 30)]:
            with self.subTest(arguments=arguments):
                args = build_parser().parse_args(["auto-stop", *arguments])
                self.assertEqual(build_request(args), ({
                    "cmd": "auto-stop", "value": value, "seconds": seconds}, False))
