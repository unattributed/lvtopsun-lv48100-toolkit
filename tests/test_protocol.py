"""Offline safety and protocol regression tests."""

from __future__ import annotations

import unittest

from battery import collect_lvtopsun_lv48100_two_battery_report_data as report
from battery import query_lvtopsun_lv48100_readonly_status_identity as identity
from battery import query_lvtopsun_lv48100_second_pack_readonly as second_pack
from battery import validate_lvtopsun_lv48100_battery2_live_telemetry as validate


class RequestGenerationTests(unittest.TestCase):
    def test_built_in_vectors(self) -> None:
        report.verify_request_generation()
        validate.verify_request_generation()

    def test_status_request_is_consistent_across_collectors(self) -> None:
        expected = identity.build_request(1, 0x44)
        self.assertEqual(expected, report.build_request(1, 0x44, b""))
        self.assertEqual(
            expected,
            second_pack.build_request(1, 0x44, b""),
        )
        self.assertEqual(expected, validate.build_request(1, 0x44, b""))

    def test_request_has_pace_delimiters(self) -> None:
        request = identity.build_request(1, 0xC1)
        self.assertTrue(request.startswith(b"~"))
        self.assertTrue(request.endswith(b"\r"))
        self.assertTrue(request[1:-1].decode("ascii").isalnum())


class AllowlistTests(unittest.TestCase):
    def test_collectors_expose_only_read_cid2_constants(self) -> None:
        allowed = {0x90, 0x42, 0x44, 0xC1, 0xC2}
        modules = (report, identity, second_pack, validate)

        for module in modules:
            constants = {
                value
                for name, value in vars(module).items()
                if name.startswith("CID2_")
            }
            self.assertTrue(constants)
            self.assertLessEqual(constants, allowed)


if __name__ == "__main__":
    unittest.main()
