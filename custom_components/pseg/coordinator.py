"""DataUpdateCoordinator for the PSE&G Energy integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, Dict, Optional

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_TYPE_COOKIE,
    AUTH_TYPE_MYMETER,
    AUTH_TYPE_OKTA,
    CONF_AUTH_SERVER_URL,
    CONF_AUTH_TYPE,
    CONF_COOKIE,
    CONF_ELECTRIC_RATE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_AUTH_SERVER_URL,
    DEFAULT_ELECTRIC_RATE,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
from .pseg_client import PsegAuthError, PsegClient, PsegConnectionError
from .util import is_valid_auth_server_url, sanitize_url_for_logging

_LOGGER = logging.getLogger(__name__)


class PsegDataUpdateCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Class to manage fetching PSE&G energy data from MyMeter."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PsegClient,
        entry_data: Dict[str, Any],
        config_entry: Optional[ConfigEntry] = None,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.entry_data = entry_data
        self.config_entry = config_entry
        self.current_options: dict[str, Any] = dict(config_entry.options) if config_entry else {}
        self.next_update_time: Optional[datetime] = None
        self._last_valid_data: Optional[Dict[str, Any]] = None
        self.auth_required: bool = False
        self.status: str = "connected"
        self.last_successful_update: Optional[str] = None
        self._last_reauth_attempt: Optional[datetime] = None
        self._reauth_failure_count: int = 0
        self._reauth_lock = asyncio.Lock()

        # Resolve optional user-configured rate override (None enables auto-derived 7-day average)
        user_rate = None
        if config_entry and config_entry.options.get(CONF_ELECTRIC_RATE) is not None:
            user_rate = config_entry.options.get(CONF_ELECTRIC_RATE)
        elif entry_data.get(CONF_ELECTRIC_RATE) is not None:
            user_rate = entry_data.get(CONF_ELECTRIC_RATE)

        try:
            self.user_configured_rate = float(user_rate) if user_rate is not None and float(user_rate) > 0 else None
        except (ValueError, TypeError):
            self.user_configured_rate = None

        self.client.user_configured_rate = self.user_configured_rate
        self.effective_rate = self.user_configured_rate or DEFAULT_ELECTRIC_RATE
        self.rate_source = "user_configured" if self.user_configured_rate else "default"
        self.rate_7d_avg: Optional[float] = None
        self.days_used_for_rate: int = 0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL_HOURS),
        )

    @property
    def electric_rate(self) -> float:
        """Return effective rate for backwards compatibility."""
        return self.effective_rate

    def _dismiss_auth_issues(self) -> None:
        """Dismiss persistent notification and repairs issue once authenticated."""
        if not self.config_entry:
            return
        persistent_notification.async_dismiss(
            self.hass,
            notification_id=f"pseg_reauth_{self.config_entry.entry_id}",
        )
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            f"reauth_required_{self.config_entry.entry_id}",
        )

    def _raise_auth_issues(self, err_msg: str, addon_offline: bool = False) -> None:
        """Raise persistent notification and repairs issue when authentication is required."""
        entry_id = self.config_entry.entry_id if self.config_entry else "default"
        is_cookie_mode = (
            self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE
            or not self.entry_data.get(CONF_USERNAME)
        )
        if is_cookie_mode:
            msg = (
                "Your PSE&G browser cookie has expired. "
                "Please go to **Settings > Devices & Services > PSE&G** and click **Reconfigure** to update your cookie."
            )
        elif addon_offline:
            msg = (
                "Your PSE&G session has expired and the companion service is not reachable. "
                "Please verify the companion add-on or Docker container is running, or go to "
                "**Settings > Devices & Services > PSE&G** and click **Reconfigure** to authenticate with a Browser Session Cookie."
            )
        else:
            msg = (
                "Your PSE&G session has expired and requires two-factor verification. "
                "Please go to **Settings > Devices & Services > PSE&G** and click **Reconfigure** to complete sign-in."
            )
        persistent_notification.async_create(
            self.hass,
            title="PSE&G Authentication Required",
            message=msg,
            notification_id=f"pseg_reauth_{entry_id}",
        )
        if self.config_entry:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"reauth_required_{entry_id}",
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="reauth_required",
                data={"entry_id": entry_id},
            )

    async def async_reauthenticate(self) -> bool:
        """Attempt silent re-authentication with credentials via companion add-on."""
        self._addon_connected = False
        username = self.entry_data.get(CONF_USERNAME)
        password = self.entry_data.get(CONF_PASSWORD)
        if not username or not password:
            return False

        async with self._reauth_lock:
            # If an in-flight re-auth completed while waiting for the lock, do not repeat
            if not self.auth_required:
                return True

            # Guard against rapid retry hammering and utility account lockout
            now = dt_util.utcnow()
            if self._last_reauth_attempt and (now - self._last_reauth_attempt).total_seconds() < 60:
                _LOGGER.warning("PSE&G re-authentication cooldown active; skipping automated attempt")
                return False

            if self._reauth_failure_count >= 3:
                _LOGGER.warning("Maximum automated re-authentication attempts reached (3); manual reconfiguration required")
                return False

            self._last_reauth_attempt = now

            configured_url = (
                (self.config_entry.options.get(CONF_AUTH_SERVER_URL) if self.config_entry else None)
                or self.entry_data.get(CONF_AUTH_SERVER_URL)
                or DEFAULT_AUTH_SERVER_URL
            )

            if not is_valid_auth_server_url(configured_url):
                _LOGGER.warning(
                    "Configured auth_server_url '%s' is untrusted or invalid; falling back to default %s",
                    sanitize_url_for_logging(configured_url),
                    DEFAULT_AUTH_SERVER_URL,
                )
                configured_url = DEFAULT_AUTH_SERVER_URL

            # Only probe user-configured companion URL (and local loopback alias if local)
            candidate_urls = [configured_url]
            if "127.0.0.1" in configured_url and "http://localhost:8000" not in candidate_urls:
                candidate_urls.append("http://localhost:8000")
            elif "localhost" in configured_url and "http://127.0.0.1:8000" not in candidate_urls:
                candidate_urls.append("http://127.0.0.1:8000")

            session = async_get_clientsession(self.hass)
            auth_type = self.entry_data.get(CONF_AUTH_TYPE, AUTH_TYPE_MYMETER)

            for auth_url in candidate_urls:
                if not is_valid_auth_server_url(auth_url):
                    continue
                try:
                    async with session.post(
                        f"{auth_url}/login",
                        json={
                            "username": username,
                            "password": password,
                            "auth_type": auth_type,
                            "dispatch_mfa": False,
                        },
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        self._addon_connected = True
                        if resp.status == 200:
                            res_json = await resp.json()
                            if res_json.get("success") and res_json.get("cookies"):
                                new_cookies = res_json["cookies"]
                                self.client.update_cookie(new_cookies)
                                self.entry_data[CONF_COOKIE] = new_cookies
                                if self.config_entry:
                                    self.hass.config_entries.async_update_entry(
                                        self.config_entry,
                                        data={**self.config_entry.data, CONF_COOKIE: new_cookies},
                                    )
                                _LOGGER.info(
                                    "Silent re-authentication successful via %s",
                                    sanitize_url_for_logging(auth_url),
                                )
                                self.auth_required = False
                                self.status = "connected"
                                self._reauth_failure_count = 0
                                self._dismiss_auth_issues()
                                return True
                            elif res_json.get("mfa_required"):
                                _LOGGER.warning("Silent refresh requires two-factor OTP verification")
                                return False
                            else:
                                _LOGGER.warning(
                                    "Companion returned login failure: %s",
                                    res_json.get("error", "authentication failed"),
                                )
                                self._reauth_failure_count += 1
                                return False
                        else:
                            _LOGGER.warning("Companion returned HTTP %s during login", resp.status)
                            self._reauth_failure_count += 1
                            return False
                except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
                    _LOGGER.debug(
                        "Could not reach auth server at %s",
                        sanitize_url_for_logging(auth_url),
                    )
                    continue
                except Exception as refresh_err:
                    _LOGGER.warning(
                        "Silent refresh via %s failed: %s",
                        sanitize_url_for_logging(auth_url),
                        refresh_err,
                    )
                    continue

            return False

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from PSE&G."""
        try:
            data = await self.hass.async_add_executor_job(
                self.client.fetch_energy_data, 14
            )
            records = data.get("hourly_records") or data.get("history_14d")
            if records:
                try:
                    await self.async_record_statistics(records, is_backfill=False)
                except Exception as stat_err:
                    _LOGGER.warning("Could not record statistics into LTS: %s", stat_err)
            self.next_update_time = dt_util.utcnow() + self.update_interval
            data["next_update"] = self.next_update_time.isoformat()
            self._last_valid_data = data
            self.last_successful_update = dt_util.utcnow().isoformat()
            self.effective_rate = data.get("effective_rate", self.effective_rate)
            self.rate_source = data.get("rate_source", self.rate_source)
            self.rate_7d_avg = data.get("rate_7d_avg")
            self.days_used_for_rate = data.get("days_used_for_rate", 0)
            self.auth_required = False
            self.status = "connected"
            self._dismiss_auth_issues()
            return data
        except PsegConnectionError as err:
            self.status = "error"
            _LOGGER.warning("Connection error communicating with PSE&G portal: %s", err)
            raise UpdateFailed(f"Connection error communicating with PSE&G portal: {err}") from err
        except PsegAuthError as err:
            self.auth_required = True
            self.status = "auth_required"
            _LOGGER.warning("Session error: %s. Attempting silent re-authentication...", err)
            if await self.async_reauthenticate():
                data = await self.hass.async_add_executor_job(
                    self.client.fetch_energy_data, 14
                )
                records = data.get("hourly_records") or data.get("history_14d")
                if records:
                    try:
                        await self.async_record_statistics(records, is_backfill=False)
                    except Exception as stat_err:
                        _LOGGER.warning("Could not record statistics into LTS: %s", stat_err)
                self.next_update_time = dt_util.utcnow() + self.update_interval
                data["next_update"] = self.next_update_time.isoformat()
                self._last_valid_data = data
                self.last_successful_update = dt_util.utcnow().isoformat()
                self.auth_required = False
                self.status = "connected"
                self._dismiss_auth_issues()
                return data

            # Silent re-authentication could not complete -> alert user and trigger Reconfigure
            username = self.entry_data.get(CONF_USERNAME)
            is_cookie_mode = (
                self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE
                or not username
            )
            addon_offline = not getattr(self, "_addon_connected", False) if not is_cookie_mode else False
            self._raise_auth_issues(str(err), addon_offline=addon_offline)
            if is_cookie_mode:
                raise ConfigEntryAuthFailed(
                    "PSE&G browser cookie has expired. Please update your cookie under Settings > Devices & Services > PSE&G > Reconfigure."
                ) from err
            if addon_offline:
                raise ConfigEntryAuthFailed(
                    "PSE&G companion service is unreachable. Please verify the companion add-on or Docker container is running, or re-authenticate under Settings > Devices & Services > PSE&G > Reconfigure."
                ) from err
            raise ConfigEntryAuthFailed(
                "Session expired or MFA required. Please re-authenticate under Settings > Devices & Services > PSE&G."
            ) from err
        except Exception as err:
            self.status = "error"
            raise UpdateFailed(f"Error communicating with PSE&G: {err}") from err

    async def async_record_statistics(
        self,
        records: list[dict[str, Any]],
        is_backfill: bool = False,
    ) -> None:
        """Import energy consumption and cost records into Home Assistant external long-term statistics."""
        if not records:
            return

        tz = dt_util.get_time_zone(self.hass.config.time_zone)

        # Normalize records to UTC start datetime and float values
        normalized_records = []
        for r in records:
            start_dt = None
            if "start" in r and r["start"]:
                if isinstance(r["start"], datetime):
                    start_dt = dt_util.as_utc(r["start"])
                elif isinstance(r["start"], (int, float)):
                    start_dt = datetime.fromtimestamp(r["start"], tz=dt_util.UTC)
            elif "date" in r and r["date"]:
                local_dt = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=tz)
                start_dt = dt_util.as_utc(local_dt)

            if not start_dt:
                continue

            kwh = float(r.get("kwh") or 0.0)
            cost = float(r.get("cost") or 0.0)
            normalized_records.append({"start": start_dt, "kwh": kwh, "cost": cost})

        if not normalized_records:
            return

        sorted_records = sorted(normalized_records, key=lambda x: x["start"])

        consumption_id = f"{DOMAIN}:electricity_consumption"
        cost_id = f"{DOMAIN}:electricity_cost"

        consumption_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": "energy",
            "name": "PSE&G Electricity Consumption",
            "source": DOMAIN,
            "statistic_id": consumption_id,
            "unit_of_measurement": "kWh",
        }

        cost_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": None,
            "name": "PSE&G Electricity Cost",
            "source": DOMAIN,
            "statistic_id": cost_id,
            "unit_of_measurement": "USD",
        }

        # Check existing external statistics in Home Assistant
        last_stat_start_dt: Optional[datetime] = None
        last_consumption_sum = 0.0
        last_cost_sum = 0.0

        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                consumption_id,
                True,
                {"sum", "start"},
            )
            if consumption_id in last_stats and last_stats[consumption_id]:
                last_stat = last_stats[consumption_id][0]
                stat_start = last_stat.get("start")
                if isinstance(stat_start, (int, float)):
                    last_stat_start_dt = datetime.fromtimestamp(stat_start, tz=dt_util.UTC)
                elif isinstance(stat_start, datetime):
                    last_stat_start_dt = dt_util.as_utc(stat_start)
                last_consumption_sum = float(last_stat.get("sum") or 0.0)
        except Exception as err:
            _LOGGER.debug("Could not query last consumption statistic: %s", err)

        try:
            last_cost_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                cost_id,
                True,
                {"sum", "start"},
            )
            if cost_id in last_cost_stats and last_cost_stats[cost_id]:
                last_cost_stat = last_cost_stats[cost_id][0]
                last_cost_sum = float(last_cost_stat.get("sum") or 0.0)
        except Exception as err:
            _LOGGER.debug("Could not query last cost statistic: %s", err)

        consumption_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []

        if is_backfill or last_stat_start_dt is None:
            # Full backfill or fresh initialization
            first_start = sorted_records[0]["start"]
            step = timedelta(hours=1)
            if len(sorted_records) > 1:
                diff = sorted_records[1]["start"] - sorted_records[0]["start"]
                if diff >= timedelta(days=1):
                    step = timedelta(days=1)

            baseline_dt = first_start - step
            consumption_stats.append(
                StatisticData(
                    start=baseline_dt,
                    state=0.0,
                    sum=0.0,
                )
            )
            cost_stats.append(
                StatisticData(
                    start=baseline_dt,
                    state=0.0,
                    sum=0.0,
                )
            )
            running_consumption = 0.0
            running_cost = 0.0
            records_to_process = sorted_records
        else:
            # Incremental append: strictly process records newer than the latest recorded statistic
            records_to_process = [r for r in sorted_records if r["start"] > last_stat_start_dt]
            running_consumption = last_consumption_sum
            running_cost = last_cost_sum

        if not records_to_process and len(consumption_stats) <= 1:
            _LOGGER.debug("No new statistics to record (all records <= %s)", last_stat_start_dt)
            return

        for r in records_to_process:
            start_utc = r["start"]
            kwh = r["kwh"]
            cost = r["cost"]

            running_consumption += kwh
            running_cost += cost

            consumption_stats.append(
                StatisticData(
                    start=start_utc,
                    state=kwh,
                    sum=round(running_consumption, 4),
                )
            )
            cost_stats.append(
                StatisticData(
                    start=start_utc,
                    state=cost,
                    sum=round(running_cost, 4),
                )
            )

        async_add_external_statistics(self.hass, consumption_meta, consumption_stats)
        async_add_external_statistics(self.hass, cost_meta, cost_stats)
        _LOGGER.info(
            "Added %d external statistics into LTS for %s and %s (%s to %s)",
            len(consumption_stats),
            consumption_id,
            cost_id,
            consumption_stats[0]["start"].isoformat(),
            consumption_stats[-1]["start"].isoformat(),
        )

    async def async_backfill_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Fetch and import historical data into Long-Term Statistics."""
        now = dt_util.now()
        if not end_date:
            end_date_str = now.strftime("%Y-%m-%d")
        elif hasattr(end_date, "strftime"):
            end_date_str = end_date.strftime("%Y-%m-%d")
        else:
            end_date_str = str(end_date)

        if not start_date:
            start_dt = now - timedelta(days=730)
            start_date_str = start_dt.strftime("%Y-%m-%d")
        elif hasattr(start_date, "strftime"):
            start_date_str = start_date.strftime("%Y-%m-%d")
        else:
            start_date_str = str(start_date)

        _LOGGER.info(
            "Starting PSE&G historical backfill from %s to %s...",
            start_date_str,
            end_date_str,
        )

        try:
            records = await self.hass.async_add_executor_job(
                self.client.fetch_history_range,
                start_date_str,
                end_date_str,
                90,
            )
        except PsegAuthError as auth_err:
            _LOGGER.warning(
                "Session expired during history backfill (%s). Attempting silent re-authentication...",
                auth_err,
            )
            if await self.async_reauthenticate():
                records = await self.hass.async_add_executor_job(
                    self.client.fetch_history_range,
                    start_date_str,
                    end_date_str,
                    90,
                )
            else:
                self.auth_required = True
                self.status = "auth_required"
                is_cookie_mode = (
                    self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE
                    or not self.entry_data.get(CONF_USERNAME)
                )
                addon_offline = not getattr(self, "_addon_connected", False) if not is_cookie_mode else False
                self._raise_auth_issues(str(auth_err), addon_offline=addon_offline)
                if is_cookie_mode:
                    raise HomeAssistantError(
                        "PSE&G session cookie has expired. Please re-authenticate under Settings > Devices & Services > PSE&G > Reconfigure."
                    ) from auth_err
                if addon_offline:
                    raise HomeAssistantError(
                        "PSE&G companion service is unreachable. Please verify the companion add-on or Docker container is running, or re-authenticate under Settings > Devices & Services > PSE&G > Reconfigure."
                    ) from auth_err
                raise HomeAssistantError(
                    "Session expired or authentication failed. Please re-authenticate under Settings > Devices & Services > PSE&G."
                ) from auth_err

        if not records:
            _LOGGER.warning("No historical records returned from PSE&G for %s to %s", start_date_str, end_date_str)
            return 0

        await self.async_record_statistics(records, is_backfill=True)

        # Update coordinator state if backfill includes latest readings
        latest_record = records[-1]
        if self._last_valid_data is not None:
            self._last_valid_data["history_14d"] = records[-14:]
            self._last_valid_data["latest_date"] = latest_record["date"]
            self._last_valid_data["latest_kwh"] = latest_record["kwh"]
            self._last_valid_data["latest_cost"] = latest_record["cost"]
            if len(records) > 1:
                prev_record = records[-2]
                self._last_valid_data["previous_date"] = prev_record["date"]
                self._last_valid_data["previous_kwh"] = prev_record["kwh"]
                self._last_valid_data["previous_cost"] = prev_record["cost"]
            self.async_set_updated_data(self._last_valid_data)

        _LOGGER.info("Completed PSE&G backfill: imported %d days of statistics", len(records))
        return len(records)
