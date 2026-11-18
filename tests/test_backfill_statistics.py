"""Regression tests for backfill statistics reconciliation."""

import asyncio
from datetime import UTC, datetime
from itertools import pairwise
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import tests.conftest  # noqa: F401
from custom_components.pseg import coordinator as coordinator_module
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator

CONSUMPTION_ID = "pseg:electricity_consumption"
COST_ID = "pseg:electricity_cost"
NY = ZoneInfo("America/New_York")


class Recorder:
    """In-memory implementation of the public recorder statistics APIs."""

    def __init__(self):
        self.rows = {CONSUMPTION_ID: {}, COST_ID: {}}
        self.writes = []

    async def async_add_executor_job(self, function, *args):
        return function(*args)

    def statistics_during_period(self, hass, start, end, statistic_ids, period, units, types):
        assert period == "hour"
        assert types == {"state", "sum"}
        return {
            statistic_id: [
                dict(row)
                for timestamp, row in sorted(self.rows[statistic_id].items())
                if start <= timestamp and (end is None or timestamp < end)
            ]
            for statistic_id in sorted(statistic_ids)
        }

    def get_last_statistics(self, hass, number, statistic_id, include_start, types):
        rows = sorted(self.rows[statistic_id].values(), key=lambda row: row["start"], reverse=True)
        return {statistic_id: [dict(row) for row in rows[:number]]}

    def add_external_statistics(self, hass, metadata, rows):
        statistic_id = metadata["statistic_id"]
        self.writes.append((statistic_id, [dict(row) for row in rows]))
        for row in rows:
            self.rows[statistic_id][row["start"]] = dict(row)

    def seed(self, statistic_id, start, state, total):
        self.rows[statistic_id][start] = {
            "start": start,
            "state": state,
            "sum": total,
        }


def setup_coordinator(monkeypatch, recorder=None):
    recorder = recorder or Recorder()
    hass = MagicMock()
    hass.config.time_zone = "America/New_York"
    coordinator = object.__new__(PsegDataUpdateCoordinator)
    coordinator.hass = hass
    monkeypatch.setattr(coordinator_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(
        coordinator_module,
        "async_add_external_statistics",
        recorder.add_external_statistics,
    )
    monkeypatch.setattr(
        coordinator_module,
        "statistics_during_period",
        recorder.statistics_during_period,
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_last_statistics",
        recorder.get_last_statistics,
    )
    monkeypatch.setattr(coordinator_module, "StatisticData", lambda **row: row)
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "get_time_zone",
        lambda _name: NY,
        raising=False,
    )
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "as_utc",
        lambda value: value.astimezone(UTC),
        raising=False,
    )
    monkeypatch.setattr(coordinator_module.dt_util, "UTC", UTC)
    return coordinator, recorder


def local_start(day, hour=0):
    return datetime.fromisoformat(f"{day}T{hour:02}:00:00").replace(tzinfo=NY).astimezone(UTC)


def day_row(day, kwh, cost=None):
    return {"date": day, "kwh": kwh, "cost": cost if cost is not None else kwh / 4}


def hourly_row(start, kwh, cost=None):
    return {
        "start": start,
        "kwh": kwh,
        "cost": cost if cost is not None else kwh / 4,
    }


def stored_rows(recorder, statistic_id):
    return sorted(recorder.rows[statistic_id].values(), key=lambda row: row["start"])


def run_record(coordinator, rows, is_backfill):
    asyncio.run(coordinator.async_record_statistics(rows, is_backfill=is_backfill))


def test_backfill_preserves_hourly_days_and_rechains_later_rows(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    early = local_start("2026-10-29", 9)
    later = local_start("2026-10-31", 2)
    recorder.seed(CONSUMPTION_ID, early, 2, 2)
    recorder.seed(CONSUMPTION_ID, later, 4, 6)
    recorder.seed(COST_ID, early, 0.5, 0.5)
    recorder.seed(COST_ID, later, 1, 1.5)

    run_record(
        coordinator,
        [
            day_row("2026-10-29", 20, 5),
            day_row("2026-10-30", 3, 0.75),
            day_row("2026-10-31", 30, 7.5),
        ],
        is_backfill=True,
    )

    consumption = stored_rows(recorder, CONSUMPTION_ID)
    cost = stored_rows(recorder, COST_ID)
    assert [row["start"] for row in consumption] == [early, local_start("2026-10-30"), later]
    assert [row["state"] for row in consumption] == [2, 3, 4]
    assert [row["sum"] for row in consumption] == [2, 5, 9]
    assert [row["state"] for row in cost] == [0.5, 0.75, 1]
    assert [row["sum"] for row in cost] == [0.5, 1.25, 2.25]
    assert all(
        left["sum"] <= right["sum"]
        for left, right in pairwise(consumption)
    )
    assert len(recorder.writes[-2][1]) == len(recorder.writes[-1][1]) == 2


def test_backfill_on_empty_statistics_adds_zero_anchor(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)

    run_record(
        coordinator,
        [day_row("2026-11-02", 4, 1), day_row("2026-11-03", 2, 0.5)],
        is_backfill=True,
    )

    consumption = stored_rows(recorder, CONSUMPTION_ID)
    assert [row["start"] for row in consumption] == [
        local_start("2026-11-01"),
        local_start("2026-11-02"),
        local_start("2026-11-03"),
    ]
    assert [row["state"] for row in consumption] == [0, 4, 2]
    assert [row["sum"] for row in consumption] == [0, 4, 6]


def test_backfill_uses_predecessor_outside_query_window(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    early = local_start("2024-12-01", 8)
    later = local_start("2026-10-31", 2)
    recorder.seed(CONSUMPTION_ID, early, 10, 100)
    recorder.seed(COST_ID, early, 2, 20)
    recorder.seed(CONSUMPTION_ID, later, 4, 104)
    recorder.seed(COST_ID, later, 1, 21)

    run_record(coordinator, [day_row("2026-10-30", 3, 0.75)], is_backfill=True)

    consumption = stored_rows(recorder, CONSUMPTION_ID)
    cost = stored_rows(recorder, COST_ID)
    assert [row["start"] for row in consumption] == [early, local_start("2026-10-30"), later]
    assert [row["sum"] for row in consumption] == [100, 103, 107]
    assert [row["start"] for row in cost] == [early, local_start("2026-10-30"), later]
    assert [row["sum"] for row in cost] == [20, 20.75, 21.75]


def test_backfill_inserts_day_with_only_fresh_poll_anchor(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    anchor = local_start("2026-10-29", 23)
    hourly = local_start("2026-10-30")
    run_record(coordinator, [hourly_row(hourly, 2, 0.5)], is_backfill=False)

    run_record(coordinator, [day_row("2026-10-29", 3, 0.75)], is_backfill=True)

    for statistic_id, states, totals in (
        (CONSUMPTION_ID, [0, 3, 0, 2], [0, 3, 3, 5]),
        (COST_ID, [0, 0.75, 0, 0.5], [0, 0.75, 0.75, 1.25]),
    ):
        rows = stored_rows(recorder, statistic_id)
        assert [row["start"] for row in rows] == [
            local_start("2026-10-28"),
            local_start("2026-10-29"),
            anchor,
            hourly,
        ]
        assert [row["state"] for row in rows] == states
        assert [row["sum"] for row in rows] == totals


def test_backfill_service_reports_only_inserted_days(monkeypatch, caplog):
    coordinator, recorder = setup_coordinator(monkeypatch)
    coordinator.client = MagicMock()
    coordinator.client.fetch_history_range.return_value = [
        day_row("2026-10-29", 3),
        day_row("2026-10-30", 4),
    ]
    coordinator.hass.async_add_executor_job = recorder.async_add_executor_job
    coordinator._last_valid_data = None
    coordinator._portal_lock = asyncio.Lock()
    recorder.seed(CONSUMPTION_ID, local_start("2026-10-30", 1), 2, 2)
    recorder.seed(COST_ID, local_start("2026-10-30", 1), 0.5, 0.5)

    async def backfill_twice():
        return (
            await coordinator.async_backfill_history("2026-10-29", "2026-10-30"),
            await coordinator.async_backfill_history("2026-10-29", "2026-10-30"),
        )

    with caplog.at_level("INFO"):
        assert asyncio.run(backfill_twice()) == (1, 0)
    assert "daily statistics imported: 1" in caplog.text
    assert "daily statistics imported: 0" in caplog.text


def test_repeating_backfill_does_not_change_rows(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    records = [day_row("2026-10-29", 2), day_row("2026-10-30", 3)]

    run_record(coordinator, records, is_backfill=True)
    first = {
        statistic_id: stored_rows(recorder, statistic_id)
        for statistic_id in (CONSUMPTION_ID, COST_ID)
    }
    write_count = len(recorder.writes)
    run_record(coordinator, records, is_backfill=True)

    assert {
        statistic_id: stored_rows(recorder, statistic_id)
        for statistic_id in (CONSUMPTION_ID, COST_ID)
    } == first
    assert len(recorder.writes) == write_count


def test_poll_after_backfill_continues_from_rechained_total(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    recorder.seed(CONSUMPTION_ID, local_start("2026-10-29", 1), 2, 2)
    recorder.seed(COST_ID, local_start("2026-10-29", 1), 0.5, 0.5)

    run_record(coordinator, [day_row("2026-10-30", 3, 0.75)], is_backfill=True)
    run_record(
        coordinator,
        [hourly_row(local_start("2026-10-31", 1), 1, 0.25)],
        is_backfill=False,
    )

    assert stored_rows(recorder, CONSUMPTION_ID)[-1]["sum"] == 6
    assert stored_rows(recorder, COST_ID)[-1]["sum"] == 1.5


def test_statistics_read_failure_aborts_without_writes(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)

    def fail_read(*args):
        raise RuntimeError("recorder unavailable")

    monkeypatch.setattr(coordinator_module, "statistics_during_period", fail_read)
    with pytest.raises(coordinator_module.HomeAssistantError, match="backfill was not recorded"):
        run_record(coordinator, [day_row("2026-10-30", 3)], is_backfill=True)

    assert recorder.writes == []


def test_fall_back_day_uses_local_day_for_existing_row_detection(monkeypatch):
    coordinator, recorder = setup_coordinator(monkeypatch)
    first_one_am = datetime(2026, 11, 1, 5, tzinfo=UTC)
    second_one_am = datetime(2026, 11, 1, 6, tzinfo=UTC)
    recorder.seed(CONSUMPTION_ID, first_one_am, 1, 1)
    recorder.seed(CONSUMPTION_ID, second_one_am, 2, 3)
    recorder.seed(COST_ID, first_one_am, 0.25, 0.25)
    recorder.seed(COST_ID, second_one_am, 0.5, 0.75)

    run_record(
        coordinator,
        [day_row("2026-11-01", 10, 2.5), day_row("2026-11-02", 4, 1)],
        is_backfill=True,
    )

    assert [row["start"] for row in stored_rows(recorder, CONSUMPTION_ID)] == [
        first_one_am,
        second_one_am,
        local_start("2026-11-02"),
    ]
    assert [row["sum"] for row in stored_rows(recorder, CONSUMPTION_ID)] == [1, 3, 7]
