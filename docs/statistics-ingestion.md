# Statistics ingestion

Regular polls reconcile hourly readings into `pseg:electricity_consumption_hourly`
(kWh) and `pseg:electricity_cost_hourly` (USD). A late hour fills a gap, a corrected
hour replaces its prior amount, and every later cumulative total is updated.
Missing readings do not erase existing data. Reimporting unchanged readings
does not add consumption. Energy and cost reconcile independently.

Historical backfills request daily totals, not historical hourly detail. They
now write **alternative** series:

- `pseg:electricity_consumption_daily`
- `pseg:electricity_cost_daily`

Choose hourly or daily statistics for an overlapping period in the Energy
dashboard. Adding both as consumption sources would double-count the same
energy. Daily totals are never spread into invented hourly readings. Existing
statistics are not automatically deleted or migrated.

The original unsuffixed statistics IDs are preserved as legacy history. Older
backfills put daily totals there and could reset cumulative sums between
chunks. After Home Assistant starts, the integration checks for the original
IDs and shows an Energy source upgrade notice in Repairs if either exists.
Follow the [Energy dashboard upgrade steps](../README.md#upgrade-note-for-existing-energy-dashboards)
after the first successful hourly import.

Both fetch paths keep readings that appear in only one meter series for a given
hour or backfill day. This preserves non-overlapping history across a meter
replacement and lets an empty series coexist with a populated one. Repeated
observations within one series are deduplicated. If several series have valid
readings for the same interval, the importer skips that interval. Hourly
diagnostics count these overlaps, and backfill logs the number of days skipped.
An omitted interval leaves any previously imported reading in place.

The initial refresh defers statistics until Home Assistant has fully started,
then schedules a fresh poll. Recorder waits for startup before processing its
queue, so waiting for a commit during config-entry setup would deadlock startup.
The connection sensor reports `pending_startup` during that short interval.

Reconciliation waits for queued Recorder writes before reading and after
writing. It reads from the last stored row before the earliest incoming
interval through the latest stored row, separately for energy and cost.
An indexed lookup finds the baseline even across a gap. Recent polls leave
older history outside the read, while an older correction still rebuilds all
affected later totals. Database errors, nonfinite values, or inconsistent
cumulative totals in the read window abort the import. Earlier rows outside
the window are not revalidated on every poll.

Connection status describes portal connectivity. `statistics_import_status`
separately reports `no_data`, `synchronized`, or `error`. Empty consumption is
unknown rather than zero, and the card displays an explicit no-data message.
Diagnostic counts summarize meter/point parsing without dumping account or
cookie data.

The current external IDs remain shared across entries; this patch targets one
configured account. Account-specific statistics and migration remain separate
work. Costs remain estimates based on the current configured/default rate.
