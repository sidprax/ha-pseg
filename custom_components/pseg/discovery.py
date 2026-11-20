"""Parse service and meter information from MyMeter chart responses."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup


def _chart_fragments(response: Any):
    """Yield HTML fragments from chart replacement actions."""
    if not isinstance(response, dict) or not isinstance(response.get("AjaxResults"), list):
        return
    for result in response["AjaxResults"]:
        if (
            isinstance(result, dict)
            and result.get("Action") == "Replace"
            and isinstance(result.get("Value"), str)
        ):
            yield BeautifulSoup(result["Value"], "html.parser")


def parse_services(response: Any) -> list[dict[str, Any]]:
    """Return all service menu entries, including non-usage entries."""
    services = []
    for fragment in _chart_fragments(response):
        for item in fragment.select("li.setServiceTypeChartButton"):
            type_id = item.get("data-value")
            label = item.find("a")
            if not isinstance(type_id, str) or not type_id.isdecimal() or label is None:
                continue
            text = label.get_text(" ", strip=True)
            if text:
                services.append({
                    "type_id": int(type_id),
                    "label": text,
                    "selected": "current" in item.get("class", []),
                })
    return services


def parse_meters(response: Any) -> list[dict[str, Any]]:
    """Return meters for the service selected in a chart response."""
    meters = []
    for fragment in _chart_fragments(response):
        for item in fragment.select("form.meterFilterForm div.meterFilter"):
            meter_id = item.select_one('span[id^="meterId-"]')
            if meter_id is None or item.select_one('input[name="meterIds"]') is None:
                continue
            internal_id = meter_id.get("id", "")[len("meterId-"):]
            number = meter_id.get_text(" ", strip=True).removeprefix("Meter #").strip()
            if not internal_id or not number:
                continue
            legend = item.select_one(".meter-range-legend")
            start_date = end_date = None
            if legend:
                start, separator, end = legend.get_text(" ", strip=True).partition(" - ")
                if separator:
                    start_date = start.strip() or None
                    end_date = end.strip() or None
            rate = item.find_next_sibling(class_="meterRateCodeSeries")
            description = rate.select_one(".meterRateCodeDescription") if rate else None
            meters.append({
                "meter_number": number,
                "internal_id": internal_id,
                "start_date": start_date,
                "end_date": None if end_date and end_date.casefold() == "present" else end_date,
                "active": end_date is None or end_date.casefold() == "present",
                "rate_code": description.get_text(" ", strip=True) if description else None,
            })
    return meters
