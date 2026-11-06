"""Sanitized reproductions of the live portal's multiple-meter response shape."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.pseg.pseg_client import PsegClient


X = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000
HOUR = 3_600_000
DAY = 24 * HOUR


def series(name, data):
    return {"name": name, "data": data, "type": "column", "visible": True,
            "showInLegend": False, "showHoverChart": True,
            "dataGrouping": {"enabled": False}, "tooltip": {"valueSuffix": " kWh"}}


def response(meters):
    # The unnamed, invisible daily navigator is not hourly consumption.
    return {"AjaxResults": [], "Data": {"usageType": "Consumption", "series": [
        {"visible": False, "data": [[X, 99]]},
        *meters,
        {"name": "Temperature", "data": [{"x": X, "y": 60}]},
        {"name": "Energy Marker", "data": []},
    ]}}


@pytest.mark.parametrize("empty_first", [False, True])
def test_empty_meter_never_hides_a_meter_with_readings(empty_first):
    meters = [series("Meter #ACTIVE", [{"x": X, "y": .39119999999999994,
                                       "hs": {"start": "10/20/2026", "end": "10/20/2026"}}]),
              series("Meter #RETIRED", [])]
    if empty_first:
        meters.reverse()
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert result["meter"] == "Meter #ACTIVE"
    assert result["latest_kwh"] == .39
    assert result["hourly_records"][0]["kwh"] == .3912
    assert result["hourly_records"][0]["start"].hour == 4
    assert result["chart_diagnostics"]["meters_with_readings"] == 1
    assert result["chart_diagnostics"]["meter_series_count"] == 2


@pytest.mark.parametrize("new_name", ["Meter #NEW", "Meter #OLD"])
def test_hourly_replacement_history_is_retained_around_ambiguous_overlap(new_name, caplog):
    meters = [series("Meter #OLD", [{"x": X, "y": 1}, {"x": X + HOUR, "y": 2}]),
              series(new_name, [{"x": X + HOUR, "y": 3}, {"x": X + 2 * HOUR, "y": 4}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert [r["start"].hour for r in result["hourly_records"]] == [4, 6]
    assert [r["kwh"] for r in result["hourly_records"]] == [1, 4]
    assert result["meter"] == new_name
    assert result["chart_diagnostics"]["ambiguous_hour_count"] == 1
    assert result["chart_diagnostics"]["meters_with_readings"] == 2
    assert "overlapping PSE&G meter readings" in caplog.text


def test_only_valid_readings_can_make_an_hour_ambiguous():
    meters = [series("Meter #ONE", [{"x": X, "y": 1}]),
              series("Meter #TWO", [{"x": X, "y": None}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert result["latest_kwh"] == 1
    assert result["chart_diagnostics"]["ambiguous_hour_count"] == 0


def test_overlapping_series_alone_do_not_report_consumption():
    meters = [series("Meter #ONE", [{"x": X, "y": 1}]),
              series("Meter #TWO", [{"x": X, "y": 2}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert result["hourly_records"] == []
    assert result["consumption_data_available"] is False
    assert result["chart_diagnostics"]["ambiguous_hour_count"] == 1


def test_daily_backfill_keeps_unambiguous_days_and_ignores_empty_meter(caplog):
    client = PsegClient("test=1")
    client.session = MagicMock()
    client.session.get.return_value.json.return_value = response([
        series("Meter #OLD", [{"x": X, "y": 10}, {"x": X + DAY, "y": 20}]),
        series("Meter #NEW", [{"x": X + DAY, "y": 30}, {"x": X + 2 * DAY, "y": 40}]),
        series("Meter #EMPTY", []),
    ])
    with patch.object(client, "_get_verification_token", return_value="token"):
        records = client.fetch_history_range("2026-10-20", "2026-10-22")
    assert [(r["date"], r["kwh"]) for r in records] == [
        ("2026-10-20", 10), ("2026-10-22", 40)
    ]
    assert "overlapping PSE&G meter readings" in caplog.text


def test_fall_back_hours_are_assigned_within_each_series():
    repeated = datetime(2026, 11, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    meters = [series("Meter #ONE", [{"x": repeated, "y": 1}, {"x": repeated, "y": 2}]),
              series("Meter #ONE", [{"x": repeated, "y": 3}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert [r["start"].hour for r in result["hourly_records"]] == [6]
    assert [r["kwh"] for r in result["hourly_records"]] == [2]
    assert result["chart_diagnostics"]["ambiguous_hour_count"] == 1
