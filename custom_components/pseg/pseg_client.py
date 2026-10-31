"""PSEG API Client for Home Assistant (New Jersey).

Uses session cookies to communicate directly with the PSE&G MySmartEnergy portal (MyMeter platform).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from .const import DEFAULT_ELECTRIC_RATE

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mysmartenergy.nj.pseg.com"
# Requested by the keep-alive between scheduled updates: the dashboard's widget
# loader with an empty region, about 100 bytes, instead of the whole dashboard page
KEEPALIVE_PATH = "/Widget/LoadWidgets"
# The portal's local time zone; MyMeter timestamps are wall-clock times in it.
PORTAL_TIMEZONE = ZoneInfo("America/New_York")


def portal_time_to_utc(x_ms: float, fold: int = 0) -> datetime:
    """Convert a MyMeter chart timestamp to an aware UTC datetime.

    MyMeter encodes the portal's local wall-clock time as if it were UTC: the
    point for midnight on October 24 in New Jersey has x = 2026-10-24T00:00Z.
    Reading it as a real UTC moment puts every point 4 hours (5 in winter) too
    early, so daily statistics land on the previous day.

    `fold=1` selects the second occurrence of a wall-clock time that repeats
    when the clocks go back (both 1 AM hours carry the same x).
    """
    wall_clock = datetime.fromtimestamp(x_ms / 1000, tz=timezone.utc).replace(
        tzinfo=PORTAL_TIMEZONE, fold=fold
    )
    return wall_clock.astimezone(timezone.utc)


def point_value(pt: Dict[str, Any]) -> Optional[float]:
    """Return a chart point's `y` as a float, or None when it has no reading.

    A missing, null, non-numeric or non-finite `y` means the portal has no
    value for that interval. It must not be read as 0, which would record zero
    usage for an hour or day the portal has not reported yet.
    """
    y = pt.get("y")
    if y is None or isinstance(y, bool):
        return None
    try:
        value = float(y)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def is_login_url(url: str) -> bool:
    """Return whether the portal sent a request to its sign-in page.

    MyMeter redirects requests of an expired session to the portal root, where
    the sign-in form lives, sometimes with the original query string appended.
    """
    url = url.lower()
    return urlparse(url).path in ("", "/") or "signin" in url or "login" in url


class PsegAuthError(Exception):
    """Exception raised for authentication or session failures."""


class PsegConnectionError(Exception):
    """Exception raised for network or communication failures with PSE&G."""


class PsegClient:
    """Client for PSE&G MySmartEnergy."""

    def __init__(self, cookie: str, user_configured_rate: Optional[float] = None) -> None:
        self.cookie = cookie
        try:
            self.user_configured_rate = float(user_configured_rate) if user_configured_rate and float(user_configured_rate) > 0 else None
        except (ValueError, TypeError):
            self.user_configured_rate = None

        self.effective_rate = self.user_configured_rate or DEFAULT_ELECTRIC_RATE
        self.rate_source = "user_configured" if self.user_configured_rate else "default"
        self.rate_7d_avg: Optional[float] = None
        self.days_used_for_rate: int = 0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{BASE_URL}/Dashboard",
        })
        self.update_cookie(cookie)

    def update_cookie(self, new_cookie: str) -> None:
        """Update active session cookies.

        The cookies go into the session's cookie jar rather than a fixed Cookie
        header: MyMeter extends a session by sending a new MM_SID with
        Set-Cookie (its expiry is part of the value), and only the jar picks
        that up. A fixed header would keep sending the original until it runs out.
        """
        self.cookie = new_cookie
        self.session.cookies.clear()
        host = urlparse(BASE_URL).hostname
        for part in new_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                self.session.cookies.set(name, value, domain=host, path="/")

    def _remember_renewed_cookies(self) -> None:
        """Take over cookies MyMeter renewed; call only after a response showed a valid session.

        A renewal sent with a Domain attribute is stored next to the host-only cookie
        from update_cookie(), so the jar can hold two copies of a name. The copy whose
        value differs from the stored one is the renewal; the other is dropped.
        """
        stored = dict(
            part.strip().partition("=")[::2] for part in self.cookie.split(";") if "=" in part
        )
        copies: Dict[str, list] = {}
        for c in self.session.cookies:
            copies.setdefault(c.name, []).append(c)
        for name, same_name in copies.items():
            if len(same_name) < 2:
                continue
            renewed = [c for c in same_name if c.value != stored.get(name)] or same_name
            for c in same_name:
                if c is not renewed[-1]:
                    self.session.cookies.clear(c.domain, c.path, c.name)
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)
        if cookie and cookie != self.cookie:
            _LOGGER.debug("MyMeter renewed the session cookie")
            self.cookie = cookie

    def test_connection(self, timeout: float = 20) -> bool:
        """Validate whether current session cookie is authorized."""
        try:
            resp = self.session.get(f"{BASE_URL}/Dashboard", allow_redirects=True, timeout=timeout)
            if resp.status_code in (401, 403):
                raise PsegAuthError(f"HTTP status {resp.status_code} received from portal")
            if resp.status_code != 200:
                raise PsegConnectionError(f"HTTP status {resp.status_code} received from portal")
            if is_login_url(resp.url):
                raise PsegAuthError("Session cookie expired or rejected by portal")
            self._remember_renewed_cookies()
            return True
        except requests.RequestException as err:
            raise PsegConnectionError(f"Failed to connect to PSE&G: {err}") from err

    def keep_alive(self, timeout: float = 20) -> None:
        """Make one request so MyMeter does not end the session for being idle.

        Sent like the dashboard's own widget requests, but with an empty region, so no
        widgets are returned. MyMeter still counts it as activity, and answers it for an
        expired session with a JSON redirect to the sign-in page
        (or an HTTP redirect, which is not followed, so the sign-in page isn't downloaded).
        """
        try:
            resp = self.session.get(
                f"{BASE_URL}{KEEPALIVE_PATH}",
                params={"Region": "", "_": int(time.time() * 1000)},
                headers={"X-Requested-With": "XMLHttpRequest"},
                allow_redirects=False,
                timeout=timeout,
            )
        except requests.RequestException as err:
            raise PsegConnectionError(f"Failed to connect to PSE&G: {err}") from err
        if resp.status_code in (401, 403):
            raise PsegAuthError(f"HTTP status {resp.status_code} received from portal")
        if resp.is_redirect and is_login_url(resp.headers.get("Location", "")):
            raise PsegAuthError("Session expired: Redirected to login")
        if resp.status_code != 200:
            raise PsegConnectionError(f"HTTP status {resp.status_code} received from portal")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and any(
            isinstance(ar, dict) and ar.get("Action") == "Redirect"
            for ar in data.get("AjaxResults") or []
        ):
            raise PsegAuthError("Session expired: Redirected to login")
        self._remember_renewed_cookies()

    def _get_verification_token(self) -> str:
        """Extract __RequestVerificationToken from the /Dashboard page."""
        try:
            resp = self.session.get(f"{BASE_URL}/Dashboard", timeout=25)
            if resp.status_code in (401, 403):
                raise PsegAuthError(f"HTTP status {resp.status_code} received from portal")
            if resp.status_code != 200:
                raise PsegConnectionError(f"HTTP status {resp.status_code} received from portal")
            if is_login_url(resp.url):
                raise PsegAuthError("Session expired: Redirected to login")
            self._remember_renewed_cookies()
            dash_html = resp.text
        except requests.RequestException as err:
            raise PsegConnectionError(f"Network error on Dashboard: {err}") from err

        token_match = re.search(
            r'name=[\"\']__RequestVerificationToken[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']',
            dash_html,
        )
        if not token_match:
            raise PsegAuthError("Session invalid: __RequestVerificationToken not found on /Dashboard")
        return token_match.group(1)

    def fetch_history_range(
        self,
        start_date_str: str,
        end_date_str: str,
        chunk_days: int = 90,
    ) -> list[dict[str, Any]]:
        """Fetch historical usage and cost data across a date range in polite chunks."""
        token = self._get_verification_token()

        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError as err:
            raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {err}") from err

        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt

        headers2 = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
        }

        records_map: dict[str, dict[str, Any]] = {}
        curr_start = start_dt

        while curr_start <= end_dt:
            curr_end = min(curr_start + timedelta(days=chunk_days), end_dt)
            chunk_start_str = curr_start.strftime("%Y-%m-%d")
            chunk_end_str = curr_end.strftime("%Y-%m-%d")
            _LOGGER.info("Fetching PSE&G history chunk from %s to %s...", chunk_start_str, chunk_end_str)

            chart_setup_data_kwh = {
                "__RequestVerificationToken": token,
                "UsageInterval": "6",  # 6 = Daily
                "UsageType": "1",  # 1 = Consumption (kWh)
                "jsTargetName": "StorageType",
                "EnableHoverChart": "true",
                "Start": chunk_start_str,
                "End": chunk_end_str,
                "IsRangeOpen": "False",
                "MaintainMaxDate": "true",
                "SelectedViaDateRange": "False",
                "ChartComparison": "1",
                "ChartComparison2": "0",
                "ChartComparison3": "0",
                "ChartComparison4": "0",
            }

            try:
                resp2 = self.session.post(
                    f"{BASE_URL}/Dashboard/Chart",
                    data=chart_setup_data_kwh,
                    headers=headers2,
                    timeout=25,
                )
                resp2.raise_for_status()

                ts = int(time.time() * 1000)
                resp3 = self.session.get(
                    f"{BASE_URL}/Dashboard/ChartData",
                    params={"_": ts},
                    headers=headers2,
                    timeout=25,
                )
                resp3.raise_for_status()
                data = resp3.json()
            except requests.RequestException as err:
                raise PsegConnectionError(f"Failed to retrieve chart chunk ({chunk_start_str} to {chunk_end_str}): {err}") from err
            except json.JSONDecodeError as err:
                raise PsegConnectionError(f"Invalid JSON returned from chart chunk ({chunk_start_str} to {chunk_end_str}): {err}") from err

            chart_data = data.get("Data")
            if not chart_data or not isinstance(chart_data, dict):
                ajax_results = data.get("AjaxResults") or []
                for ar in ajax_results:
                    if isinstance(ar, dict) and ar.get("Action") == "Redirect":
                        raise PsegAuthError("Session expired during history backfill: Redirected to login")
                _LOGGER.warning("No chart data returned for range %s to %s", chunk_start_str, chunk_end_str)
            else:
                series_list = chart_data.get("series", [])
                meter_series = {}
                for s in series_list:
                    if "Meter" in str(s.get("name") or ""):
                        meter_series = s
                        break

                pts = meter_series.get("data", [])
                for pt in pts:
                    if isinstance(pt, dict):
                        value = point_value(pt)
                        if value is None:
                            continue
                        kwh = round(value, 4)
                        hs = pt.get("hs", {})
                        p_start = hs.get("start", "")
                        try:
                            dt = datetime.strptime(p_start, "%m/%d/%Y")
                            date_iso = dt.strftime("%Y-%m-%d")
                        except Exception:
                            date_iso = p_start

                        x = pt.get("x", 0)
                        start_utc = portal_time_to_utc(x) if x else None
                        cost_val = round(kwh * self.effective_rate, 4)

                        records_map[date_iso] = {
                            "date": date_iso,
                            "start": start_utc,
                            "kwh": kwh,
                            "cost": cost_val,
                        }

            curr_start = curr_end + timedelta(days=1)
            if curr_start <= end_dt:
                time.sleep(0.75)

        sorted_records = [records_map[d] for d in sorted(records_map.keys())]
        _LOGGER.info(
            "Fetched %d historical records across range %s to %s",
            len(sorted_records),
            start_date_str,
            end_date_str,
        )
        return sorted_records

    def fetch_energy_data(self, days_back: int = 14) -> Dict[str, Any]:
        """Fetch usage and temperature data using the 3-step MyMeter chart sequence."""
        token = self._get_verification_token()

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days_back)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        chart_setup_data_kwh = {
            "__RequestVerificationToken": token,
            "UsageInterval": "5",  # 5 = Hourly
            "UsageType": "1",  # 1 = Consumption (kWh)
            "jsTargetName": "StorageType",
            "EnableHoverChart": "true",
            "Start": start_str,
            "End": end_str,
            "IsRangeOpen": "False",
            "MaintainMaxDate": "true",
            "SelectedViaDateRange": "False",
            "ChartComparison": "1",
            "ChartComparison2": "0",
            "ChartComparison3": "0",
            "ChartComparison4": "0",
        }

        headers2 = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
        }

        # Step 2: Query kWh consumption data
        try:
            resp2 = self.session.post(f"{BASE_URL}/Dashboard/Chart", data=chart_setup_data_kwh, headers=headers2, timeout=25)
            resp2.raise_for_status()
        except requests.RequestException as err:
            raise PsegConnectionError(f"Failed to set chart context: {err}") from err

        ts = int(time.time() * 1000)
        try:
            resp3 = self.session.get(f"{BASE_URL}/Dashboard/ChartData", params={"_": ts}, headers=headers2, timeout=25)
            resp3.raise_for_status()
            kwh_data = resp3.json()
        except requests.RequestException as err:
            raise PsegConnectionError(f"Failed to retrieve chart data: {err}") from err
        except json.JSONDecodeError as err:
            raise PsegConnectionError(f"Invalid JSON returned from chart data: {err}") from err

        return self._parse_chart_data(kwh_data)

    def _parse_chart_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse series into clean sensor model and calculate costs using effective rate."""
        chart_data = data.get("Data")
        if not chart_data or not isinstance(chart_data, dict):
            ajax_results = data.get("AjaxResults") or []
            for ar in ajax_results:
                if isinstance(ar, dict) and ar.get("Action") == "Redirect":
                    raise PsegAuthError("Session expired: Redirected to login portal")
            raise PsegConnectionError("No chart data returned from MyMeter. Unexpected portal response.")

        series_list = chart_data.get("series", [])
        if not series_list:
            raise PsegConnectionError("No series data returned from MyMeter. Unexpected portal response.")

        nav_series = series_list[0].get("data", [])
        meter_series = {}
        temp_series = {}

        for s in series_list:
            name = str(s.get("name") or "")
            if "Meter" in name:
                meter_series = s
            elif "Temperature" in name:
                temp_series = s

        meter_name = meter_series.get("name") or "PSE&G Electric Meter"
        detailed_pts = meter_series.get("data", [])
        temp_pts = temp_series.get("data", [])

        # Parse kWh data points and aggregate daily totals
        daily_kwh_map: dict[str, float] = {}
        parsed_pts = []
        prev_x = None
        for pt in detailed_pts:
            if isinstance(pt, dict):
                x = pt.get("x", 0)
                # A repeated timestamp is the second 1 AM hour of the night
                # the clocks go back.
                start_utc = portal_time_to_utc(x, fold=1 if x == prev_x else 0)
                prev_x = x
                value = point_value(pt)
                if value is None:
                    continue
                kwh = round(value, 4)

                hs = pt.get("hs", {})
                start_str = hs.get("start", "")
                try:
                    date_iso = datetime.strptime(start_str, "%m/%d/%Y").strftime("%Y-%m-%d")
                except Exception:
                    date_iso = start_utc.astimezone(PORTAL_TIMEZONE).strftime("%Y-%m-%d")

                daily_kwh_map[date_iso] = round(daily_kwh_map.get(date_iso, 0.0) + kwh, 4)
                parsed_pts.append({
                    "start": start_utc,
                    "date": date_iso,
                    "kwh": kwh,
                    "x": x,
                })

        sorted_dates = sorted(daily_kwh_map.keys())

        # Hierarchy: 1) User configured override, 2) Default rate
        if self.user_configured_rate is not None and self.user_configured_rate > 0:
            effective_rate = self.user_configured_rate
            rate_source = "user_configured"
        else:
            effective_rate = DEFAULT_ELECTRIC_RATE
            rate_source = "default"

        self.effective_rate = effective_rate
        self.rate_source = rate_source
        self.rate_7d_avg = None
        self.days_used_for_rate = 0

        _LOGGER.debug(
            "PSE&G Rate Resolved: %s $/kWh (source=%s)",
            effective_rate,
            rate_source,
        )

        # Build final hourly records with calculated cost
        hourly_records = []
        for p in parsed_pts:
            p_kwh = p["kwh"]
            cost_val = round(p_kwh * effective_rate, 4)
            hourly_records.append({
                "start": p["start"],
                "date": p["date"],
                "kwh": p_kwh,
                "cost": cost_val,
                "is_exact_cost": False,
            })

        # Build daily history (14 days)
        history_14d = []
        for d in sorted_dates:
            day_kwh = round(daily_kwh_map[d], 2)
            day_cost = round(day_kwh * effective_rate, 2)
            history_14d.append({
                "date": d,
                "kwh": day_kwh,
                "cost": day_cost,
                "is_exact_cost": False,
            })

        latest_record = history_14d[-1] if history_14d else {"date": "None", "kwh": 0.0, "cost": 0.0, "is_exact_cost": False}
        prev_record = history_14d[-2] if len(history_14d) > 1 else {"date": "None", "kwh": 0.0, "cost": 0.0, "is_exact_cost": False}

        latest_temp = temp_pts[-1] if temp_pts else {}
        temp_range = latest_temp.get("tr", "")
        temp_value = point_value(latest_temp)

        return {
            "meter": meter_name,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "latest_date": latest_record["date"],
            "latest_kwh": latest_record["kwh"],
            "latest_cost": latest_record["cost"],
            "is_exact_latest_cost": latest_record.get("is_exact_cost", False),
            "previous_date": prev_record["date"],
            "previous_kwh": prev_record["kwh"],
            "previous_cost": prev_record["cost"],
            "history_14d": history_14d,
            "hourly_records": hourly_records,
            "temperature_range": temp_range,
            "temperature_avg": round(temp_value, 1) if temp_value is not None else None,
            "total_records": len(nav_series),
            "effective_rate": effective_rate,
            "rate_source": rate_source,
            "rate_7d_avg": None,
            "days_used_for_rate": 0,
        }
