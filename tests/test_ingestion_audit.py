"""Ingestion regressions and executable specifications for unresolved defects.

These use real datetime/zoneinfo behavior, unlike the repository's default
datetime stubs. Recorder reads/writes remain mocked: this is not an HA DB test.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pseg import coordinator as module
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.pseg_client import PsegClient, portal_time_to_utc

UTC = timezone.utc
START = datetime(2026, 7, 1, 4, tzinfo=UTC)
ENERGY = "pseg:electricity_consumption_hourly"
COST = "pseg:electricity_cost_hourly"


def x(day, hour=0):
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp() * 1000


def parse(points):
    return PsegClient("test=1")._parse_chart_data(
        {"Data": {"series": [{"name": "Meter", "data": points}]}}
    )


@pytest.fixture
def recorder():
    hass = MagicMock()
    hass.config.time_zone = "Asia/Tokyo"  # Utility time must not depend on HA time.
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    instance = MagicMock()
    instance.async_block_till_done = AsyncMock()
    instance.statistics_rows = {}

    def read(fn, hass, start, *args):
        if fn is not module._read_statistics_window:
            return instance.statistics_rows
        existing = {}
        predecessors = {}
        for statistic_id in args[0]:
            rows = instance.statistics_rows.get(statistic_id, [])
            split = next((i for i, row in enumerate(rows) if row["start"] >= start.timestamp()), len(rows))
            existing[statistic_id] = rows[split:]
            if split:
                predecessors[statistic_id] = rows[split - 1]
        return existing, predecessors

    instance.async_add_executor_job = AsyncMock(side_effect=read)
    coord = PsegDataUpdateCoordinator(hass, MagicMock(), {})
    with (
        patch.object(module, "get_instance", return_value=instance),
        patch.object(module, "async_add_external_statistics") as write,
        patch.object(module, "StatisticData", side_effect=lambda **kw: kw),
        patch.object(module.dt_util, "as_utc", side_effect=lambda d: d.astimezone(UTC)),
    ):
        def persist(hass, metadata, rows):
            database = instance.statistics_rows
            id_ = metadata["statistic_id"]
            by_start = {
                (r["start"].timestamp() if isinstance(r["start"], datetime) else r["start"]): r
                for r in database.get(id_, [])
            }
            by_start.update({r["start"].timestamp(): r for r in rows})
            database[id_] = [by_start[start] for start in sorted(by_start)]
        write.side_effect = persist
        yield coord, instance, write


def record(hour=0, kwh=1, cost=.25):
    return {"start": START + timedelta(hours=hour), "kwh": kwh, "cost": cost}


def existing(instance, hour=2, energy=4, cost=1):
    instance.statistics_rows = {
        ENERGY: [{"start": (START + timedelta(hours=hour)).timestamp(), "sum": energy, "state": energy}],
        COST: [{"start": (START + timedelta(hours=hour)).timestamp(), "sum": cost, "state": cost}],
    }


def test_dst_fall_back_unsorted_points_are_distinct():
    data = parse([
        {"x": x("2026-11-01", 1), "y": 1},
        {"x": x("2026-11-01", 2), "y": 3},
        {"x": x("2026-11-01", 1), "y": 2},
    ])
    starts = [r["start"] for r in data["hourly_records"]]
    assert [d.hour for d in starts] == [5, 6, 7]
    assert data["latest_kwh"] == 6


def test_missing_first_fall_back_hour_keeps_second_fold():
    data = parse([
        {"x": x("2026-11-01", 1), "y": None},
        {"x": x("2026-11-01", 1), "y": 2},
    ])
    assert data["hourly_records"][0]["start"].hour == 6


def test_duplicate_normal_hour_is_replaced_not_added():
    data = parse([{"x": x("2026-07-01", 1), "y": 1},
                  {"x": x("2026-07-01", 1), "y": 2}])
    assert len(data["hourly_records"]) == 1
    assert data["latest_kwh"] == 2


def test_spring_gap_is_rejected():
    with pytest.raises(ValueError):
        portal_time_to_utc(x("2026-03-08", 2))
    data = parse([{"x": x("2026-03-08", h), "y": 1} for h in (1, 2, 3)])
    assert [r["start"].hour for r in data["hourly_records"]] == [6, 7]


@pytest.mark.parametrize("bad", [None, True, "oops", float("nan"), float("inf")])
def test_invalid_timestamps_do_not_create_epoch_or_crash(bad):
    assert parse([{"x": bad, "y": 1}])["hourly_records"] == []


def test_timestamp_controls_day_even_when_hover_text_disagrees():
    assert parse([{"x": x("2026-07-01"), "y": 1,
                   "hs": {"start": "06/30/2026"}}])["latest_date"] == "2026-07-01"


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_bad_statistics_values_abort_before_write(recorder, value):
    import asyncio
    coord, _, write = recorder
    with pytest.raises(ValueError):
        asyncio.run(coord.async_record_statistics([record(kwh=value)]))
    write.assert_not_called()


def test_duplicate_statistics_timestamp_counted_once(recorder):
    import asyncio
    coord, _, write = recorder
    asyncio.run(coord.async_record_statistics([record(kwh=1), record(kwh=2)]))
    assert write.call_args_list[0].args[2][-1]["sum"] == 2


def test_daily_date_uses_portal_timezone(recorder):
    import asyncio
    coord, _, write = recorder
    asyncio.run(coord.async_record_statistics([{"date": "2026-07-01", "kwh": 1, "cost": .25}]))
    assert write.call_args_list[0].args[2][-1]["start"] == START


def test_recorder_read_failure_does_not_reset_sums(recorder):
    import asyncio
    coord, instance, write = recorder
    instance.async_add_executor_job.side_effect = RuntimeError("database unavailable")
    with pytest.raises(module.HomeAssistantError):
        asyncio.run(coord.async_record_statistics([record()]))
    write.assert_not_called()


@pytest.mark.parametrize("start", [START.replace(tzinfo=None), START + timedelta(minutes=1)])
def test_invalid_statistics_time_rejected(recorder, start):
    import asyncio
    coord, _, write = recorder
    with pytest.raises(ValueError):
        asyncio.run(coord.async_record_statistics([dict(record(), start=start)]))
    write.assert_not_called()


def test_date_object_backfill_and_old_sensor_state(recorder):
    import asyncio
    coord, _, _ = recorder
    coord.client.fetch_history_range.return_value = [{"date": "2025-01-01", "kwh": 1, "cost": .25}]
    coord._last_valid_data = {"latest_date": "2026-07-01", "latest_kwh": 10}
    coord.async_record_statistics = AsyncMock()
    coord.async_set_updated_data = MagicMock()
    assert asyncio.run(coord.async_backfill_history(date(2025, 1, 1), date(2025, 1, 2))) == 1
    coord.client.fetch_history_range.assert_called_once_with("2025-01-01", "2025-01-02", 90)
    assert coord._last_valid_data["latest_date"] == "2026-07-01"
    coord.async_set_updated_data.assert_not_called()


def test_daily_backfill_does_not_replace_current_poll_model(recorder):
    import asyncio
    coord, _, _ = recorder
    coord.client.fetch_history_range.return_value = [{"date": "2026-07-02", "kwh": 20, "cost": 5}]
    coord._last_valid_data = {"latest_date": "2026-07-01", "latest_kwh": 10}
    coord.async_record_statistics = AsyncMock()
    coord.async_set_updated_data = MagicMock()
    asyncio.run(coord.async_backfill_history("2026-07-02", "2026-07-02"))
    assert coord._last_valid_data == {"latest_date": "2026-07-01", "latest_kwh": 10}
    coord.async_set_updated_data.assert_not_called()


@pytest.mark.parametrize("chunk", [0, -1, True, 1.5])
def test_bad_history_chunk_rejected_before_network(chunk):
    client = PsegClient("test=1")
    with (patch.object(client, "_get_verification_token") as token,
          patch.object(client.session, "post", side_effect=AssertionError("Network before validation"))):
        with pytest.raises(ValueError):
            client.fetch_history_range("2026-01-01", "2026-01-02", chunk)
        token.assert_not_called()


def test_hourly_request_selects_hourly_interval():
    client = PsegClient("test=1")
    client.session = MagicMock()
    client.session.get.return_value.json.return_value = {
        "Data": {"series": [{"name": "Meter", "data": []}]}
    }
    with patch.object(client, "_get_verification_token", return_value="token"):
        client.fetch_energy_data()
    assert client.session.post.call_args.kwargs["data"]["UsageInterval"] == "5"


def test_history_chunks_are_daily_and_bounded():
    client = PsegClient("test=1")
    client.session = MagicMock()
    client.session.get.return_value.json.return_value = {
        "Data": {"series": [{"name": "Meter", "data": [
            {"x": x("2026-01-01"), "y": 1},
            {"x": x("2026-04-01"), "y": 2},
            {"x": x("2025-12-31"), "y": 99},
            {"x": x("2026-04-02"), "y": 99},
            {"x": None, "y": 99},
        ]}]}
    }
    with (patch.object(client, "_get_verification_token", return_value="token"),
          patch("custom_components.pseg.pseg_client.time.sleep")):
        records = client.fetch_history_range("2026-01-01", "2026-04-01", 90)
    ranges = [call.kwargs["data"] for call in client.session.post.call_args_list]
    assert [(r["Start"], r["End"]) for r in ranges] == [
        ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-04-01")
    ]
    assert all(r["UsageInterval"] == "6" for r in ranges)
    assert [r["date"] for r in records] == ["2026-01-01", "2026-04-01"]


@pytest.mark.parametrize("day,expected_hour", [("2026-01-15", 5), ("2026-07-01", 4)])
def test_local_midnight_utc_offset(day, expected_hour):
    assert portal_time_to_utc(x(day)).hour == expected_hour


def test_incremental_append_preserves_cumulative_baseline(recorder):
    import asyncio
    coord, instance, write = recorder
    existing(instance, hour=0, energy=10, cost=2)
    asyncio.run(coord.async_record_statistics([record(hour=1, kwh=2, cost=.5)]))
    assert write.call_args_list[0].args[2][-1]["sum"] == 12
    assert write.call_args_list[1].args[2][-1]["sum"] == 2.5


def test_empty_hourly_result_never_falls_back_to_daily(recorder):
    import asyncio
    coord, _, write = recorder
    coord.client.fetch_energy_data.return_value = {
        "hourly_records": [], "history_14d": [{"date": "2026-07-01", "kwh": 10, "cost": 2.5}]
    }
    asyncio.run(coord._async_fetch_and_record())
    write.assert_not_called()
    assert coord.statistics_import_status == "no_data"


def test_empty_response_is_missing_consumption_not_zero():
    result = parse([])
    assert result["latest_kwh"] is None
    assert result["latest_cost"] is None
    assert result["latest_date"] is None
    assert result["consumption_data_available"] is False
    assert result["chart_diagnostics"]["parsed_hour_count"] == 0


def test_explicit_zero_is_available_consumption():
    result = parse([{"x": x("2026-07-01"), "y": 0}])
    assert result["latest_kwh"] == 0
    assert result["consumption_data_available"] is True


def test_import_failure_is_distinct_from_portal_connectivity(recorder):
    import asyncio
    coord, instance, _ = recorder
    instance.async_add_executor_job.side_effect = RuntimeError("database unavailable")
    coord.client.fetch_energy_data.return_value = {"hourly_records": [record()]}
    asyncio.run(coord._async_fetch_and_record())
    assert coord.status == "connected"
    assert coord.statistics_import_status == "error"
    assert "database unavailable" in coord.last_statistics_error


def test_late_gap_should_be_filled(recorder):
    import asyncio
    coord, instance, write = recorder
    existing(instance)
    asyncio.run(coord.async_record_statistics([record(hour=1)]))
    assert write.call_count == 2


def test_corrected_hour_should_be_replaced(recorder):
    import asyncio
    coord, instance, write = recorder
    existing(instance)
    asyncio.run(coord.async_record_statistics([record(hour=0, kwh=5)]))
    assert write.call_count == 2


def test_daily_backfill_should_not_overwrite_existing_hour(recorder):
    import asyncio
    coord, instance, write = recorder
    existing(instance, hour=0, energy=10)
    asyncio.run(coord.async_record_statistics([record(kwh=20)], is_backfill=True))
    # Safe policy requires separate daily series or explicit resolution-aware merge.
    assert all(call.args[1]["statistic_id"] not in {ENERGY, COST}
               for call in write.call_args_list)
