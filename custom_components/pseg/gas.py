"""Parse billing-period gas usage from MyMeter chart data."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any


def _numeric_value(point: Any) -> float | None:
    """Return a finite numeric chart value."""
    if not isinstance(point, dict):
        return None
    value = point.get("y")
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _billing_date(value: Any) -> date | None:
    """Parse a portal-local billing date."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_gas_data(data: Any) -> list[dict[str, Any]]:
    """Choose the best gas meter series and return its valid billing periods."""
    chart_data = data.get("Data") if isinstance(data, dict) else None
    series_list = chart_data.get("series") if isinstance(chart_data, dict) else None
    if not isinstance(series_list, list):
        raise TypeError("Gas chart response is missing chart series")

    candidates = []
    for series in series_list:
        if not isinstance(series, dict):
            continue
        meter = series.get("name")
        if not isinstance(meter, str) or not meter.strip():
            continue
        tooltip = series.get("tooltip")
        suffix = tooltip.get("valueSuffix", "") if isinstance(tooltip, dict) else ""
        if "ccf" not in str(suffix).casefold():
            continue

        records = []
        for point in series.get("data") or []:
            ccf = _numeric_value(point)
            hs = point.get("hs") if isinstance(point, dict) else None
            start = _billing_date(hs.get("start")) if isinstance(hs, dict) else None
            end = _billing_date(hs.get("end")) if isinstance(hs, dict) else None
            if ccf is None or start is None or end is None:
                continue
            records.append({
                "start": start,
                "end": end,
                "ccf": ccf,
                "meter": meter,
            })
        if records:
            candidates.append(records)

    if not candidates:
        return []

    positive = [
        records for records in candidates
        if any(record["ccf"] > 0 for record in records)
    ]
    selected = max(
        positive or candidates,
        key=lambda records: max(record["start"] for record in records),
    )
    return sorted(selected, key=lambda record: record["start"])
