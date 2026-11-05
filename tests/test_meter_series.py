"""Sanitized reproductions of the live portal's multiple-meter response shape."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.pseg.pseg_client import PsegClient


def test_client_remains_compatible_with_released_portal_constants(monkeypatch):
    from pathlib import Path
    from custom_components.pseg import const, pseg_client

    monkeypatch.delattr(const, "DEFAULT_PORTAL_URL", raising=False)
    namespace = {"__name__": "custom_components.pseg.compat_client",
                 "__package__": "custom_components.pseg"}
    exec(compile(Path(pseg_client.__file__).read_text(encoding="utf-8"),
                 pseg_client.__file__, "exec"), namespace)
    assert namespace["BASE_URL"] == "https://mysmartenergy.nj.pseg.com"
    assert namespace["PsegClient"]("test=1").base_url == namespace["BASE_URL"]

X = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000


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


def test_replacement_meter_history_is_retained_and_concurrent_usage_is_added():
    meters = [series("Meter #OLD", [{"x": X, "y": 1}, {"x": X + 3600000, "y": 2}]),
              series("Meter #NEW", [{"x": X + 3600000, "y": 3}, {"x": X + 7200000, "y": 4}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert [r["kwh"] for r in result["hourly_records"]] == [1, 5, 4]
    assert result["latest_kwh"] == 10
    assert result["meter"] == "Meter #NEW"


def test_duplicate_series_for_same_meter_are_not_double_counted():
    meters = [series("Meter #ONE", [{"x": X, "y": 1}]),
              series("Meter #ONE", [{"x": X, "y": 2}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert result["latest_kwh"] == 2


def test_daily_backfill_retains_all_meter_days_and_ignores_empty_meter():
    client = PsegClient("test=1")
    client.session = MagicMock()
    client.session.get.return_value.json.return_value = response([
        series("Meter #OLD", [{"x": X, "y": 10}]),
        series("Meter #NEW", [{"x": X, "y": 20}, {"x": X + 86400000, "y": 30}]),
        series("Meter #EMPTY", []),
    ])
    with patch.object(client, "_get_verification_token", return_value="token"):
        records = client.fetch_history_range("2026-10-20", "2026-10-21")
    assert [r["kwh"] for r in records] == [30, 30]


def test_repeated_fall_back_hours_are_counted_per_meter():
    repeated = datetime(2026, 11, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    meters = [series("Meter #ONE", [{"x": repeated, "y": 1}, {"x": repeated, "y": 2}]),
              series("Meter #TWO", [{"x": repeated, "y": 3}, {"x": repeated, "y": 4}])]
    result = PsegClient("test=1")._parse_chart_data(response(meters))
    assert [r["start"].hour for r in result["hourly_records"]] == [5, 6]
    assert [r["kwh"] for r in result["hourly_records"]] == [4, 6]
