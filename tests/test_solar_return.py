"""Tests for net-metered solar grid export (return) parsing and statistics."""

import asyncio
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401

from custom_components.pseg.const import DOMAIN
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.pseg_client import PsegClient, select_meter_series_pair


X0 = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp() * 1000)
HOUR_MS = 3_600_000


def _solar_chart_payload() -> dict:
    return {
        "AjaxResults": [],
        "Data": {
            "series": [
                {"visible": False, "data": [[X0, 1.0]]},
                {
                    "name": "Meter #12345 (Residential Service (RS))",
                    "data": [
                        {"x": X0, "y": 1.25, "hs": {"start": "10/20/2026", "end": "10/20/2026"}},
                        {"x": X0 + HOUR_MS, "y": 0.0, "hs": {"start": "10/20/2026", "end": "10/20/2026"}},
                    ],
                },
                {
                    "name": "Meter #12345 (Residential Service (RS))",
                    "data": [
                        {"x": X0, "y": 0.0, "hs": {"start": "10/20/2026", "end": "10/20/2026"}},
                        {"x": X0 + HOUR_MS, "y": -2.5, "hs": {"start": "10/20/2026", "end": "10/20/2026"}},
                    ],
                },
                {"name": "Temperature", "data": [{"x": X0, "y": 65, "tr": "65°F"}]},
            ]
        },
    }


class TestSolarReturnParsing(unittest.TestCase):
    """Test delivered + received series selection and parsing."""

    def test_single_series_account_returns_empty_received(self):
        delivered = {"name": "Meter #111", "data": [{"x": X0, "y": 1.5}]}
        sel_del, sel_rec = select_meter_series_pair([delivered])
        self.assertIs(sel_del, delivered)
        self.assertEqual(sel_rec, {})

    def test_solar_account_splits_delivered_and_received_by_sign(self):
        payload = _solar_chart_payload()
        series = payload["Data"]["series"]
        sel_del, sel_rec = select_meter_series_pair(series)
        self.assertIs(sel_del, series[1])
        self.assertIs(sel_rec, series[2])

    def test_parse_chart_data_extracts_positive_return_hourly_records(self):
        client = PsegClient(cookie="MM_SID=abc", user_configured_rate=0.2)
        parsed = client._parse_chart_data(_solar_chart_payload())
        self.assertEqual([r["kwh"] for r in parsed["hourly_records"]], [1.25, 0.0])
        self.assertEqual([r["kwh"] for r in parsed["return_hourly_records"]], [0.0, 2.5])
        self.assertEqual([r["cost"] for r in parsed["return_hourly_records"]], [0.0, 0.5])

    def test_fetch_history_range_records_return_kwh(self):
        client = PsegClient(cookie="MM_SID=abc", user_configured_rate=0.2)
        client.session = MagicMock()
        post_resp = MagicMock(status_code=200, is_redirect=False, url="https://mysmartenergy.nj.pseg.com/Dashboard/Chart")
        get_resp = MagicMock(status_code=200, is_redirect=False, url="https://mysmartenergy.nj.pseg.com/Dashboard/ChartData")
        get_resp.json.return_value = _solar_chart_payload()
        client.session.post.return_value = post_resp
        client.session.get.return_value = get_resp
        with patch.object(client, "_get_verification_token", return_value="tok"):
            records = client.fetch_history_range("2026-10-20", "2026-10-20")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["return_kwh"], 2.5)
        self.assertEqual(records[0]["return_cost"], 0.5)


class TestSolarReturnStatistics(unittest.TestCase):
    """Test pseg:electricity_return and pseg:electricity_return_compensation LTS recording."""

    def test_records_return_statistics_with_anchor_and_sum(self):
        hass = MagicMock()
        hass.config.time_zone = "America/New_York"

        async def run_exec(func, *args):
            return func(*args)

        hass.async_add_executor_job = run_exec
        coord = PsegDataUpdateCoordinator(hass, PsegClient(cookie="MM_SID=abc"), {})
        records = [
            {"start": datetime(2026, 10, 20, 12, tzinfo=timezone.utc), "kwh": 0.0, "cost": 0.0},
            {"start": datetime(2026, 10, 20, 13, tzinfo=timezone.utc), "kwh": 2.5, "cost": 0.5},
        ]

        loop = asyncio.new_event_loop()
        try:
            with (
                patch("custom_components.pseg.coordinator.dt_util.get_time_zone", create=True, return_value=timezone.utc),
                patch("custom_components.pseg.coordinator.dt_util.as_utc", create=True, side_effect=lambda dt: dt.astimezone(timezone.utc)),
                patch("custom_components.pseg.coordinator.dt_util.UTC", create=True, new=timezone.utc),
                patch("custom_components.pseg.coordinator.StatisticData", side_effect=lambda **kw: kw),
                patch("custom_components.pseg.coordinator.get_instance", return_value=hass),
                patch("custom_components.pseg.coordinator.get_last_statistics", return_value={}),
                patch("custom_components.pseg.coordinator.async_add_external_statistics") as add_stats,
            ):
                count = loop.run_until_complete(coord.async_record_return_statistics(records, is_backfill=False))
        finally:
            loop.close()

        self.assertEqual(count, 2)
        self.assertEqual(add_stats.call_count, 2)
        return_meta, return_stats = add_stats.call_args_list[0][0][1], add_stats.call_args_list[0][0][2]
        comp_meta, comp_stats = add_stats.call_args_list[1][0][1], add_stats.call_args_list[1][0][2]
        self.assertEqual(return_meta["statistic_id"], f"{DOMAIN}:electricity_return")
        self.assertEqual(return_meta["unit_of_measurement"], "kWh")
        self.assertEqual([s["sum"] for s in return_stats], [0.0, 0.0, 2.5])
        self.assertEqual(comp_meta["statistic_id"], f"{DOMAIN}:electricity_return_compensation")
        self.assertEqual(comp_meta["unit_of_measurement"], "USD")
        self.assertEqual([s["sum"] for s in comp_stats], [0.0, 0.0, 0.5])

    def test_poll_seeds_compensation_when_return_already_exists(self):
        hass = MagicMock()
        hass.config.time_zone = "America/New_York"

        async def run_exec(func, *args):
            return func(*args)

        hass.async_add_executor_job = run_exec
        coord = PsegDataUpdateCoordinator(hass, PsegClient(cookie="MM_SID=abc"), {})
        t0 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        t1 = datetime(2026, 10, 20, 13, tzinfo=timezone.utc)
        records = [
            {"start": t0, "kwh": 1.0, "cost": 0.2},
            {"start": t1, "kwh": 2.5, "cost": 0.5},
        ]

        def fake_last_stats(_hass, _num, stat_id, *_args):
            if stat_id == f"{DOMAIN}:electricity_return":
                return {stat_id: [{"start": t1, "sum": 3.5}]}
            return {}

        loop = asyncio.new_event_loop()
        try:
            with (
                patch("custom_components.pseg.coordinator.dt_util.get_time_zone", create=True, return_value=timezone.utc),
                patch("custom_components.pseg.coordinator.dt_util.as_utc", create=True, side_effect=lambda dt: dt.astimezone(timezone.utc)),
                patch("custom_components.pseg.coordinator.dt_util.UTC", create=True, new=timezone.utc),
                patch("custom_components.pseg.coordinator.StatisticData", side_effect=lambda **kw: kw),
                patch("custom_components.pseg.coordinator.get_instance", return_value=hass),
                patch("custom_components.pseg.coordinator.get_last_statistics", side_effect=fake_last_stats),
                patch("custom_components.pseg.coordinator.async_add_external_statistics") as add_stats,
            ):
                count = loop.run_until_complete(coord.async_record_return_statistics(records, is_backfill=False))
        finally:
            loop.close()

        self.assertEqual(count, 2)
        add_stats.assert_called_once()
        comp_meta, comp_stats = add_stats.call_args[0][1], add_stats.call_args[0][2]
        self.assertEqual(comp_meta["statistic_id"], f"{DOMAIN}:electricity_return_compensation")
        self.assertEqual([s["sum"] for s in comp_stats], [0.0, 0.2, 0.7])

    def test_backfill_populates_compensation_when_return_already_exists(self):
        hass = MagicMock()
        hass.config.time_zone = "America/New_York"

        async def run_exec(func, *args):
            return func(*args)

        hass.async_add_executor_job = run_exec
        coord = PsegDataUpdateCoordinator(hass, PsegClient(cookie="MM_SID=abc"), {})
        t0 = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
        records = [{"start": t0, "kwh": 5.0, "cost": 1.0}]

        def fake_period(*_args):
            return {
                f"{DOMAIN}:electricity_return": [{"start": t0, "state": 5.0, "sum": 5.0}],
                f"{DOMAIN}:electricity_return_compensation": [],
            }

        loop = asyncio.new_event_loop()
        try:
            with (
                patch("custom_components.pseg.coordinator.dt_util.get_time_zone", create=True, return_value=timezone.utc),
                patch("custom_components.pseg.coordinator.dt_util.as_utc", create=True, side_effect=lambda dt: dt.astimezone(timezone.utc)),
                patch("custom_components.pseg.coordinator.dt_util.UTC", create=True, new=timezone.utc),
                patch("custom_components.pseg.coordinator.StatisticData", side_effect=lambda **kw: kw),
                patch("custom_components.pseg.coordinator.get_instance", return_value=hass),
                patch("custom_components.pseg.coordinator.statistics_during_period", side_effect=fake_period),
                patch("custom_components.pseg.coordinator.async_add_external_statistics") as add_stats,
            ):
                count = loop.run_until_complete(coord.async_record_return_statistics(records, is_backfill=True))
        finally:
            loop.close()

        self.assertEqual(count, 1)
        add_stats.assert_called_once()
        comp_meta, comp_stats = add_stats.call_args[0][1], add_stats.call_args[0][2]
        self.assertEqual(comp_meta["statistic_id"], f"{DOMAIN}:electricity_return_compensation")
        self.assertEqual([s["sum"] for s in comp_stats], [0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
