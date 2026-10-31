"""Unit tests for PSE&G API client."""

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path for direct unittest invocation
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub Home Assistant and aiohttp packages if not installed in current environment
for mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.frontend",
    "homeassistant.components.sensor",
    "homeassistant.components.button",
    "homeassistant.components.recorder",
    "homeassistant.components.recorder.statistics",
    "homeassistant.components.recorder.models",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.event",
    "homeassistant.helpers.typing",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
]:
    if mod not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[mod] = m

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = MagicMock()

from custom_components.pseg.const import DEFAULT_ELECTRIC_RATE
from custom_components.pseg.pseg_client import (
    PsegClient,
    PsegAuthError,
    PsegConnectionError,
    point_value,
    portal_time_to_utc,
)


class TestPointValue(unittest.TestCase):
    """Test point_value utility function."""

    def test_valid_numbers(self):
        self.assertEqual(point_value({"y": 0}), 0.0)
        self.assertEqual(point_value({"y": 0.0}), 0.0)
        self.assertEqual(point_value({"y": 1.25}), 1.25)
        self.assertEqual(point_value({"y": -0.5}), -0.5)

    def test_numeric_strings(self):
        self.assertEqual(point_value({"y": "0"}), 0.0)
        self.assertEqual(point_value({"y": "3.14"}), 3.14)
        self.assertEqual(point_value({"y": "-2.5"}), -2.5)

    def test_missing_or_null(self):
        self.assertIsNone(point_value({}))
        self.assertIsNone(point_value({"y": None}))

    def test_booleans(self):
        # In Python, bool is an instance of int; point_value must reject bools
        self.assertIsNone(point_value({"y": True}))
        self.assertIsNone(point_value({"y": False}))

    def test_invalid_strings(self):
        self.assertIsNone(point_value({"y": ""}))
        self.assertIsNone(point_value({"y": "n/a"}))
        self.assertIsNone(point_value({"y": "null"}))
        self.assertIsNone(point_value({"y": "invalid"}))

    def test_non_finite_floats(self):
        self.assertIsNone(point_value({"y": float("nan")}))
        self.assertIsNone(point_value({"y": float("inf")}))
        self.assertIsNone(point_value({"y": float("-inf")}))

    def test_non_dict_point_or_non_primitive(self):
        self.assertIsNone(point_value({"y": []}))
        self.assertIsNone(point_value({"y": {}}))


class TestPortalTimeToUtc(unittest.TestCase):
    """Test portal_time_to_utc timestamp conversion."""

    def test_edt_conversion(self):
        # 2026-07-01 12:00 EDT (UTC-4) -> 2026-07-01 16:00 UTC
        # x is timestamp in ms of 2026-07-01 12:00:00
        x_ms = 1782907200000  # 2026-07-01 12:00:00 UTC wall clock
        utc_dt = portal_time_to_utc(x_ms)
        self.assertEqual(utc_dt.hour, 16)
        self.assertEqual(utc_dt.day, 1)
        self.assertEqual(utc_dt.month, 7)

    def test_est_conversion(self):
        # 2026-01-15 12:00 EST (UTC-5) -> 2026-01-15 17:00 UTC
        x_ms = 1768478400000  # 2026-01-15 12:00:00 UTC wall clock
        utc_dt = portal_time_to_utc(x_ms)
        self.assertEqual(utc_dt.hour, 17)
        self.assertEqual(utc_dt.day, 15)
        self.assertEqual(utc_dt.month, 1)


class TestParseChartData(unittest.TestCase):
    """Test _parse_chart_data response handling."""

    def setUp(self):
        self.client = PsegClient(cookie="test_cookie")

    def test_valid_chart_data(self):
        data = {
            "Data": {
                "series": [
                    {
                        "name": "PSE&G Electric Meter",
                        "data": [
                            {"x": 1782907200000, "y": 0.5, "hs": {"start": "07/01/2026"}},
                            {"x": 1782910800000, "y": 0.0, "hs": {"start": "07/01/2026"}},
                            {"x": 1782914400000, "y": 1.5, "hs": {"start": "07/01/2026"}},
                        ],
                    },
                    {
                        "name": "Temperature",
                        "data": [
                            {"x": 1782907200000, "y": 75.2, "tr": "70-80"},
                        ],
                    },
                ]
            }
        }
        res = self.client._parse_chart_data(data)
        self.assertEqual(res["meter"], "PSE&G Electric Meter")
        self.assertEqual(res["latest_kwh"], 2.0)
        self.assertEqual(len(res["hourly_records"]), 3)
        self.assertEqual(res["temperature_avg"], 75.2)
        self.assertEqual(res["temperature_range"], "70-80")
        self.assertEqual(res["rate_source"], "default")
        self.assertEqual(res["effective_rate"], DEFAULT_ELECTRIC_RATE)

    def test_missing_points_skipped(self):
        """Verify null/invalid readings are skipped and do not skew daily sums or zero out days."""
        data = {
            "Data": {
                "series": [
                    {
                        "name": "PSE&G Electric Meter",
                        "data": [
                            {"x": 1782907200000, "y": 1.0, "hs": {"start": "07/01/2026"}},
                            {"x": 1782910800000, "y": None, "hs": {"start": "07/01/2026"}},
                            {"x": 1782914400000, "y": "n/a", "hs": {"start": "07/01/2026"}},
                            {"x": 1782918000000, "y": True, "hs": {"start": "07/01/2026"}},
                            {"x": 1782993600000, "y": None, "hs": {"start": "07/02/2026"}},
                        ],
                    },
                    {
                        "name": "Temperature",
                        "data": [
                            {"x": 1782907200000, "y": None, "tr": "65-75"},
                        ],
                    },
                ]
            }
        }
        res = self.client._parse_chart_data(data)
        # Day 07/01/2026 has 1.0 kWh; Day 07/02/2026 only has None so it shouldn't produce a day
        self.assertEqual(len(res["hourly_records"]), 1)
        self.assertEqual(res["hourly_records"][0]["kwh"], 1.0)
        self.assertEqual(len(res["history_14d"]), 1)
        self.assertEqual(res["latest_kwh"], 1.0)
        # Temperature point with None should produce None instead of throwing TypeError
        self.assertIsNone(res["temperature_avg"])
        self.assertEqual(res["temperature_range"], "65-75")

    def test_user_configured_rate_override(self):
        custom_client = PsegClient(cookie="test_cookie", user_configured_rate=0.185)
        data = {
            "Data": {
                "series": [
                    {
                        "name": "Meter",
                        "data": [
                            {"x": 1782907200000, "y": 10.0, "hs": {"start": "07/01/2026"}},
                        ],
                    }
                ]
            }
        }
        res = custom_client._parse_chart_data(data)
        self.assertEqual(res["rate_source"], "user_configured")
        self.assertEqual(res["effective_rate"], 0.185)
        self.assertEqual(res["latest_cost"], 1.85)

    def test_auth_redirect_detection(self):
        data = {
            "AjaxResults": [
                {"Action": "Redirect"}
            ]
        }
        with self.assertRaises(PsegAuthError):
            self.client._parse_chart_data(data)

    def test_missing_data_structure(self):
        with self.assertRaises(PsegConnectionError):
            self.client._parse_chart_data({})


class TestClientMethods(unittest.TestCase):
    """Test client session and update methods."""

    def test_update_cookie(self):
        client = PsegClient(cookie="old_cookie=123")
        client.update_cookie("new_cookie=456")
        self.assertEqual(client.cookie, "new_cookie=456")
        self.assertEqual(client.session.cookies.get_dict(), {"new_cookie": "456"})


if __name__ == "__main__":
    unittest.main()
