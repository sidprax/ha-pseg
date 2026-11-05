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
chunks. They cannot safely be treated as an hourly baseline. Select the new
"PSE&G Electricity Consumption (Hourly)" source in Energy for hourly usage;
do not add the overlapping legacy source to the same consumption total.

Both fetch paths retain readings from all named meter series. A retired meter
with no data cannot replace a meter with readings. Observations of the same
meter and interval are deduplicated; usage from distinct meters is added at
matching intervals. This also preserves history across meter replacement.

The initial refresh defers statistics until Home Assistant has fully started,
then schedules a fresh poll. Recorder waits for startup before processing its
queue, so waiting for a commit during config-entry setup would deadlock startup.
The connection sensor reports `pending_startup` during that short interval.

Reconciliation waits for queued Recorder writes before reading and after
writing. It reads the full existing series and rewrites only the changed
suffix. Work and memory therefore grow with retained history. Database errors,
nonfinite values, or inconsistent legacy cumulative totals abort the import.
This is a conservative guard, not an automatic repair of mixed daily/hourly
history. Legacy daily-only rows that happen to be internally consistent cannot
be distinguished from sparse hourly data without a provider replay.

Connection status describes portal connectivity. `statistics_import_status`
separately reports `no_data`, `synchronized`, or `error`. Empty consumption is
unknown rather than zero, and the card displays an explicit no-data message.
Diagnostic counts summarize meter/point parsing without dumping account or
cookie data.

The current external IDs remain shared across entries; this patch targets one
configured account. Account-specific statistics and migration remain separate
work. Costs remain estimates based on the current configured/default rate.
