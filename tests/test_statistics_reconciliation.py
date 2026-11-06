"""Regression checks for real interval sequences and persisted replacements."""

import asyncio
from datetime import datetime, timedelta, timezone
import random
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import column, create_engine, table
from sqlalchemy.orm import Session

from custom_components.pseg import coordinator as module
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator
from custom_components.pseg.statistics import reconcile_statistics

START = datetime(2026, 7, 1, 4, tzinfo=timezone.utc)
ENERGY = "pseg:electricity_consumption_hourly"
COST = "pseg:electricity_cost_hourly"


def reading(hour, amount):
    return {"start": START + timedelta(hours=hour), "kwh": amount, "cost": amount * .25}


def apply(old, updates, key="kwh"):
    replacements = reconcile_statistics(old, updates, key)
    merged = {row["start"]: row for row in old}
    merged.update({row["start"]: row for row in replacements})
    return [merged[start] for start in sorted(merged)]


def test_gap_arriving_after_later_hours_rebases_the_whole_suffix():
    old = apply([], [reading(0, 1), reading(2, 3), reading(1000, 7)])
    result = apply(old, [reading(1, 2)])
    assert [r["sum"] for r in result] == [0, 1, 3, 6, 13]
    assert not any(r["start"] == START + timedelta(hours=3) for r in result)


def test_correction_updates_later_rows_outside_fetch_window():
    old = apply([], [reading(0, 1), reading(1, 2), reading(1000, 3)])
    replacements = reconcile_statistics(old, [reading(0, 5)], "kwh")
    assert [r["sum"] for r in replacements] == [5, 7, 10]
    assert replacements[-1]["state"] == 3


def test_reimport_is_idempotent_and_missing_does_not_delete():
    old = apply([], [reading(0, 1), reading(1, 0), reading(2, 3)])
    assert reconcile_statistics(old, [reading(0, 1), reading(2, 3)], "kwh") == []
    assert apply(old, []) == old


def test_prepending_older_hours_does_not_restart_later_totals():
    old = apply([], [reading(24, 4)])
    result = apply(old, [reading(0, 2)])
    assert result[0]["sum"] == 0
    assert result[-1]["sum"] == 6


def test_late_reading_can_replace_the_old_zero_anchor():
    old = apply([], [reading(1, 3)])
    result = apply(old, [reading(0, 2)])
    assert [r["start"] for r in result] == [START - timedelta(hours=1), START, START + timedelta(hours=1)]
    assert [r["sum"] for r in result] == [0, 2, 5]


def test_unknown_baseline_is_preserved_for_later_corrections():
    old = [{"start": START, "state": 2, "sum": 102},
           {"start": START + timedelta(hours=2), "state": 3, "sum": 105}]
    assert apply(old, [reading(0, 4)])[-1]["sum"] == 107
    with pytest.raises(ValueError, match="unknown cumulative baseline"):
        apply(old, [reading(-2, 1)])


def test_inconsistent_legacy_history_requires_replay():
    old = [{"start": START, "state": 20, "sum": 20},
           {"start": START + timedelta(hours=1), "state": 1, "sum": 100}]
    with pytest.raises(ValueError, match="inconsistent"):
        reconcile_statistics(old, [reading(2, 1)], "kwh")


def test_zero_negative_and_downward_corrections_are_preserved():
    old = apply([], [reading(0, 3), reading(1, 2)])
    result = apply(old, [reading(0, 0), reading(2, -1)])
    assert [row["sum"] for row in result] == [0, 0, 2, 1]


def test_fall_back_hours_are_distinct_utc_intervals():
    start = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
    updates = [{"start": start + timedelta(hours=i), "kwh": 1} for i in range(25)]
    result = apply([], updates)
    assert len(result) == 26
    assert result[-1]["sum"] == 25


def test_random_reordering_gaps_and_corrections_match_reference_totals():
    rng = random.Random(47)
    reference = {}
    persisted = []
    for _ in range(100):
        batch = [reading(rng.randrange(100), rng.randrange(100) / 10) for _ in range(8)]
        for row in batch:
            reference[row["start"]] = row["kwh"]
        persisted = apply(persisted, batch)
        assert persisted[-1]["sum"] == pytest.approx(sum(reference.values()), abs=.0001)
        assert reconcile_statistics(persisted, batch, "kwh") == []


@pytest.mark.parametrize("starting", [False, True])
def test_first_refresh_never_waits_for_recorder_before_startup(starting):
    hass = MagicMock()
    hass.is_running = starting
    hass.state = module.CoreState.starting if starting else module.CoreState.not_running
    entry = MagicMock()
    coord = PsegDataUpdateCoordinator(hass, MagicMock(), {}, config_entry=entry)
    coord.async_record_statistics = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    cancel = MagicMock()
    with patch.object(module, "async_at_started", return_value=cancel) as after_start:
        async def run():
            await coord._async_import_poll_data({"hourly_records": [reading(0, 1)]})
            await coord._async_import_poll_data({"hourly_records": [reading(1, 2)]})
            assert coord.statistics_import_status == "pending_startup"
            coord.async_record_statistics.assert_not_awaited()
            after_start.assert_called_once()
            entry.async_on_unload.assert_called_once_with(cancel)
            hass.is_running = True
            hass.state = module.CoreState.running
            await after_start.call_args.args[1](hass)
            coord.async_request_refresh.assert_awaited_once()
            assert not coord._startup_import_scheduled
            await coord._async_import_poll_data({"hourly_records": [reading(1, 2)]})
            coord.async_record_statistics.assert_awaited_once()
            assert coord.statistics_import_status == "synchronized"
        asyncio.run(run())


class SQLiteRecorder:
    """Small persistence adapter, not a substitute for HA Recorder itself.

    Reads cannot see queued imports until the commit barrier runs. This tests
    the coordinator's ordering and SQLite upserts across separate invocations.
    """

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE stats (id TEXT, start REAL, state REAL, total REAL, PRIMARY KEY(id,start))")
        self.db.execute("CREATE TABLE stat_meta (id TEXT PRIMARY KEY, statistic_id TEXT UNIQUE)")
        self.db.execute("""
            CREATE TRIGGER register_statistic AFTER INSERT ON stats BEGIN
                INSERT OR IGNORE INTO stat_meta VALUES (NEW.id, NEW.id);
            END
        """)
        self.engine = create_engine("sqlite://", creator=lambda: self.db)
        self.reads = []
        self.pending = []
        self.barriers = 0

    def get_session(self):
        return Session(self.engine)

    def write(self, hass, metadata, rows):
        self.pending.extend((metadata["statistic_id"], r["start"].timestamp(), r["state"], r["sum"]) for r in rows)

    async def async_block_till_done(self):
        self.barriers += 1
        self.db.executemany("INSERT OR REPLACE INTO stats VALUES (?,?,?,?)", self.pending)
        self.db.commit()
        self.pending.clear()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def statistics_during_period(self, hass, start, end, ids, period, units, types):
        self.reads.append((start, ids))
        assert end is None
        assert period == "hour" and units == {"energy": "kWh"} and types == {"state", "sum"}
        return {id_: [{"start": s, "state": v, "sum": total} for s, v, total in
                      self.db.execute(
                          "SELECT start,state,total FROM stats WHERE id=? AND start>=? ORDER BY start",
                          (id_, start.timestamp()),
                      )]
                for id_ in ids}


@pytest.fixture
def sqlite_recorder(monkeypatch):
    recorder = SQLiteRecorder()
    rows = table("stats", column("id"), column("start"))
    meta = table("stat_meta", column("id"), column("statistic_id"))
    monkeypatch.setattr(module, "Statistics", SimpleNamespace(start_ts=rows.c.start, metadata_id=rows.c.id))
    # Join uses the table itself while its columns are exposed on the model.
    monkeypatch.setattr(module, "StatisticsMeta", meta)
    monkeypatch.setattr(meta, "id", meta.c.id, raising=False)
    monkeypatch.setattr(meta, "statistic_id", meta.c.statistic_id, raising=False)
    monkeypatch.setattr(module, "get_instance", lambda hass: recorder)
    monkeypatch.setattr(module, "statistics_during_period", recorder.statistics_during_period)
    monkeypatch.setattr(module, "async_add_external_statistics", recorder.write)
    yield recorder
    recorder.engine.dispose()
    recorder.db.close()


def test_coordinator_persists_corrections_gaps_and_isolates_daily_history(sqlite_recorder):
    recorder = sqlite_recorder
    # Released backfills may have reset sums between daily chunks. They must
    # neither block clean hourly imports nor be silently rewritten as hourly.
    legacy = "pseg:electricity_consumption"
    legacy_rows = [(legacy, START.timestamp(), 10, 10),
                   (legacy, (START + timedelta(days=1)).timestamp(), 20, 0)]
    recorder.db.executemany("INSERT INTO stats VALUES (?,?,?,?)", legacy_rows)
    recorder.db.commit()
    coord = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})
    with (patch.object(module, "get_instance", return_value=recorder),
          patch.object(module, "async_add_external_statistics", side_effect=recorder.write),
          patch.object(module.dt_util, "as_utc", side_effect=lambda d: d.astimezone(timezone.utc))):
        async def run():
            await coord.async_record_statistics([reading(0, 1), reading(2, 3)])
            # A different invocation, after restart, must read committed data.
            fresh = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})
            await fresh.async_record_statistics([reading(1, 2), reading(0, 5)])
            await fresh.async_record_statistics([reading(1, 2), reading(0, 5)])
            before = list(recorder.db.execute("SELECT * FROM stats WHERE id=? ORDER BY start", (ENERGY,)))
            await fresh.async_record_statistics([reading(0, 20), reading(24, 30)], is_backfill=True)
            await fresh.async_record_statistics([reading(0, 21)], is_backfill=True)
            assert list(recorder.db.execute("SELECT * FROM stats WHERE id=? ORDER BY start", (ENERGY,))) == before
        asyncio.run(run())
    assert recorder.db.execute("SELECT total FROM stats WHERE id=? ORDER BY start DESC LIMIT 1", (ENERGY,)).fetchone()[0] == 10
    assert recorder.db.execute("SELECT total FROM stats WHERE id=? ORDER BY start DESC LIMIT 1", (COST,)).fetchone()[0] == 2.5
    assert recorder.db.execute("SELECT total FROM stats WHERE id=? ORDER BY start DESC LIMIT 1", ("pseg:electricity_consumption_daily",)).fetchone()[0] == 51
    assert not recorder.pending
    assert list(recorder.db.execute("SELECT * FROM stats WHERE id=? ORDER BY start", (legacy,))) == legacy_rows


def test_cost_can_be_repaired_when_energy_is_already_current(sqlite_recorder):
    recorder = sqlite_recorder
    recorder.db.execute("INSERT INTO stats VALUES (?,?,?,?)", (ENERGY, START.timestamp(), 1, 1))
    coord = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})
    with (patch.object(module, "get_instance", return_value=recorder),
          patch.object(module, "async_add_external_statistics", side_effect=recorder.write),
          patch.object(module.dt_util, "as_utc", side_effect=lambda d: d.astimezone(timezone.utc))):
        asyncio.run(coord.async_record_statistics([reading(0, 1)]))
    assert recorder.db.execute("SELECT total FROM stats WHERE id=? ORDER BY start DESC LIMIT 1", (COST,)).fetchone()[0] == .25


def test_recent_import_reads_only_each_predecessor_and_the_affected_suffix(sqlite_recorder):
    recorder = sqlite_recorder
    # Energy reached zero before a long gap. Cost has a different predecessor
    # and has no rows in the incoming window yet.
    older_energy = [(ENERGY, reading(-3000, 0)["start"].timestamp(), 5, 5),
                    (ENERGY, reading(-2000, 0)["start"].timestamp(), -5, 0)]
    older_cost = [(COST, reading(-1000, 0)["start"].timestamp(), 3, 3)]
    recorder.db.executemany("INSERT INTO stats VALUES (?,?,?,?)", [
        *older_energy, *older_cost,
        (ENERGY, START.timestamp(), 2, 2),
        (ENERGY, reading(1000, 0)["start"].timestamp(), 4, 6),
    ])
    coord = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})

    asyncio.run(coord.async_record_statistics([reading(0, 8)]))

    assert set((start, frozenset(ids)) for start, ids in recorder.reads[:2]) == {
        (reading(-2000, 0)["start"], frozenset({ENERGY})),
        (reading(-1000, 0)["start"], frozenset({COST})),
    }
    assert recorder.reads[2:] == [(START, {ENERGY, COST})]
    assert list(recorder.db.execute("SELECT * FROM stats WHERE id=? ORDER BY start", (ENERGY,))) == [
        *older_energy,
        (ENERGY, START.timestamp(), 8, 8),
        (ENERGY, reading(1000, 0)["start"].timestamp(), 4, 12),
    ]
    assert list(recorder.db.execute("SELECT * FROM stats WHERE id=? ORDER BY start", (COST,))) == [
        *older_cost, (COST, START.timestamp(), 2, 5),
    ]


def test_silent_recorder_write_failure_is_detected(sqlite_recorder):
    recorder = sqlite_recorder
    coord = PsegDataUpdateCoordinator(MagicMock(), MagicMock(), {})
    with (patch.object(module, "get_instance", return_value=recorder),
          patch.object(module, "async_add_external_statistics"),
          patch.object(module.dt_util, "as_utc", side_effect=lambda d: d.astimezone(timezone.utc))):
        with pytest.raises(module.HomeAssistantError, match="committed Recorder data"):
            asyncio.run(coord.async_record_statistics([reading(0, 1)]))
