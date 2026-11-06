"""Reconcile interval readings with an existing cumulative statistics series.

The integration stores each interval amount in ``state`` and its cumulative
total in ``sum``. Rebuild the changed suffix so a correction does not become
an opposite change at the following, uncorrected row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("Missing or invalid statistic value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite statistic value")
    return result


def _start(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive statistic timestamp")
        result = value.astimezone(timezone.utc)
    else:
        result = datetime.fromtimestamp(_number(value), tz=timezone.utc)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Statistic timestamp is not hour aligned")
    return result


def reconcile_statistics(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], key: str,
    *, predecessor: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return replacement rows from the first change through the latest row.

    Omitted intervals are retained, never invented as zero. Existing sums must
    agree with their interval states. Refuse to guess a repair of legacy mixed
    daily/hourly or otherwise inconsistent history. When a predecessor exists,
    use its committed sum as the baseline and leave that row untouched.
    """
    old = {_start(row["start"]): row for row in existing}
    ordered = sorted(old)
    amounts = {start: _number(old[start].get("state")) for start in ordered}
    offset = 0.0
    if predecessor is not None:
        _number(predecessor.get("state"))
        offset = _number(predecessor.get("sum"))
    elif ordered:
        offset = _number(old[ordered[0]].get("sum")) - amounts[ordered[0]]
    running = offset
    for start in ordered:
        running += amounts[start]
        if not math.isclose(running, _number(old[start].get("sum")), abs_tol=.00011, rel_tol=0):
            raise ValueError("Existing statistics are inconsistent; source replay is required")

    earliest_change = None
    # Last valid observation of an interval wins; duplicate inputs never add up.
    updates = {_start(row["start"]): _number(row[key]) for row in incoming}
    for start, value in updates.items():
        if start not in amounts or not math.isclose(amounts[start], value, abs_tol=.00001, rel_tol=0):
            earliest_change = min(earliest_change, start) if earliest_change else start
        amounts[start] = value
    if earliest_change is None:
        return []
    first = min(amounts)
    if predecessor is None:
        zero_baseline = math.isclose(offset, 0, abs_tol=.00001)
        if ordered and first < ordered[0] and not zero_baseline:
            raise ValueError("Cannot prepend readings before an unknown cumulative baseline")
        if not ordered or first < ordered[0] or (zero_baseline and amounts[first] != 0):
            # A preceding zero anchor lets HA account for the first real interval.
            earliest_change = first - timedelta(hours=1)
            amounts[earliest_change] = 0.0
    running = offset
    output = []
    for start in sorted(amounts):
        running += amounts[start]
        if start >= earliest_change:
            output.append({"start": start, "state": amounts[start], "sum": round(running, 4)})
    return output
