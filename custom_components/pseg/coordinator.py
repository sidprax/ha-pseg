"""DataUpdateCoordinator for the PSE&G Energy integration."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
import logging
import random
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
    statistics_during_period,
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
    CONF_KEEP_ALIVE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_AUTH_SERVER_URL,
    DEFAULT_ELECTRIC_RATE,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    KEEPALIVE_MAX_SECONDS,
    KEEPALIVE_MIN_SECONDS,
)
from .pseg_client import PsegAuthError, PsegClient, PsegConnectionError
from .util import get_valid_auth_server_url, is_valid_auth_server_url, sanitize_url_for_logging

_LOGGER = logging.getLogger(__name__)


class ReauthStatus:
    """Status object returned by async_reauthenticate for granular error handling."""

    def __init__(self, status: str):
        self.status = status

    def __bool__(self) -> bool:
        return self.status == "success"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.status == other
        if isinstance(other, ReauthStatus):
            return self.status == other.status
        return False

    def __repr__(self) -> str:
        return f"ReauthStatus({self.status!r})"


def keep_alive_enabled(data: Dict[str, Any], options: Dict[str, Any]) -> bool:
    """Return whether to keep the MyMeter session alive between scheduled updates.

    On by default for pasted cookies, which cannot be renewed without the user.
    Off by default for companion add-on sign-ins, which sign in again when a
    session has expired.
    """
    if CONF_KEEP_ALIVE in options:
        return bool(options[CONF_KEEP_ALIVE])
    return data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE or not data.get(CONF_USERNAME)


def next_keep_alive_delay() -> float:
    """Return seconds until the next keep-alive, varied so requests don't come on a fixed beat."""
    return random.uniform(KEEPALIVE_MIN_SECONDS, KEEPALIVE_MAX_SECONDS)


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
        self.next_update_time: Optional[datetime] = None
        self._last_valid_data: Optional[Dict[str, Any]] = None
        self.auth_required: bool = False
        self.status: str = "connected"
        self.last_successful_update: Optional[str] = None
        self._last_reauth_attempt: Optional[datetime] = None
        self._reauth_failure_count: int = 0
        self._reauth_lock = asyncio.Lock()
        # MyMeter keeps the chart range and interval in the session between the
        # POST /Dashboard/Chart setup and the GET /Dashboard/ChartData read, so only
        # one fetch (and its statistics import) may use the session at a time
        self._portal_lock = asyncio.Lock()

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
        # Remove the custom repair left by older versions.
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            f"reauth_required_{self.config_entry.entry_id}",
        )

    def _raise_auth_issues(self, err_msg: str, addon_offline: bool = False) -> None:
        """Raise a persistent notification when authentication is required."""
        entry_id = self.config_entry.entry_id if self.config_entry else "default"
        integration_page = "[Settings > Devices & Services > PSE&G](/config/integrations/integration/pseg)"
        is_cookie_mode = (
            self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE
            or not self.entry_data.get(CONF_USERNAME)
        )
        if is_cookie_mode:
            msg = (
                "Your PSE&G browser cookie has expired. "
                f"Go to {integration_page}, click **Reconfigure**, and update your cookie."
            )
        elif addon_offline:
            msg = (
                "Your PSE&G session has expired and the companion service is not reachable. "
                "Please verify the companion add-on or Docker container is running, or "
                f"go to {integration_page}, click **Reconfigure**, and use a Browser Session Cookie."
            )
        else:
            msg = "Your PSE&G session has expired."
            if self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_OKTA:
                msg += " Sign-in may require two-factor verification."
            msg += (
                f" Go to {integration_page} and click **Reconfigure** to complete sign-in."
            )
        persistent_notification.async_create(
            self.hass,
            title="PSE&G Authentication Required",
            message=msg,
            notification_id=f"pseg_reauth_{entry_id}",
        )

    async def async_reauthenticate(self) -> ReauthStatus:
        """Attempt silent re-authentication with credentials via companion add-on."""
        username = self.entry_data.get(CONF_USERNAME)
        password = self.entry_data.get(CONF_PASSWORD)
        if not username or not password:
            return ReauthStatus("failed")

        async with self._reauth_lock:
            # If an in-flight re-auth completed while waiting for the lock, do not repeat
            if not self.auth_required:
                return ReauthStatus("success")

            # Guard against rapid retry hammering and utility account lockout
            now = dt_util.utcnow()
            if self._last_reauth_attempt and (now - self._last_reauth_attempt).total_seconds() < 60:
                _LOGGER.warning("PSE&G re-authentication cooldown active; skipping automated attempt")
                return ReauthStatus("skipped_cooldown")

            if self._reauth_failure_count >= 3:
                _LOGGER.warning("Maximum automated re-authentication attempts reached (3); manual reconfiguration required")
                return ReauthStatus("skipped_max_failures")

            # Reset _addon_connected inside the lock, after guards have passed
            self._addon_connected = False

            configured_url = get_valid_auth_server_url(
                self.config_entry.options if self.config_entry else None,
                self.entry_data,
                DEFAULT_AUTH_SERVER_URL,
            )

            # Only probe user-configured companion URL (and local loopback alias if local)
            candidate_urls = [configured_url]
            if "127.0.0.1" in configured_url and "http://localhost:8000" not in candidate_urls:
                candidate_urls.append("http://localhost:8000")
            elif "localhost" in configured_url and "http://127.0.0.1:8000" not in candidate_urls:
                candidate_urls.append("http://127.0.0.1:8000")

            session = async_get_clientsession(self.hass)
            auth_type = self.entry_data.get(CONF_AUTH_TYPE, AUTH_TYPE_MYMETER)

            try:
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
                                    return ReauthStatus("success")
                                elif res_json.get("mfa_required"):
                                    _LOGGER.warning("Silent refresh requires two-factor OTP verification")
                                    return ReauthStatus("failed")
                                else:
                                    _LOGGER.warning(
                                        "Companion returned login failure: %s",
                                        res_json.get("error", "authentication failed"),
                                    )
                                    self._reauth_failure_count += 1
                                    return ReauthStatus("failed")
                            elif 300 <= resp.status < 400:
                                _LOGGER.warning(
                                    "Companion URL %s returned redirect HTTP %s; redirects disallowed for security",
                                    sanitize_url_for_logging(auth_url),
                                    resp.status,
                                )
                                return ReauthStatus("failed")
                            elif resp.status in (401, 403):
                                try:
                                    err_json = await resp.json()
                                except Exception:
                                    err_json = {}
                                if isinstance(err_json, dict) and (
                                    err_json.get("recaptcha_blocked") is True
                                    or "recaptcha_blocked" in str(err_json.get("error", ""))
                                ):
                                    _LOGGER.warning(
                                        "Companion login temporarily blocked by Google reCAPTCHA; will retry on next poll"
                                    )
                                    return ReauthStatus("recaptcha_blocked")
                                _LOGGER.warning(
                                    "Companion returned portal auth rejection HTTP %s during login",
                                    resp.status,
                                )
                                self._reauth_failure_count += 1
                                return ReauthStatus("failed")
                            elif resp.status >= 500:
                                _LOGGER.warning(
                                    "Companion service error HTTP %s during login; will retry on next poll",
                                    resp.status,
                                )
                                return ReauthStatus("companion_error")
                            else:
                                _LOGGER.warning(
                                    "Companion returned unexpected HTTP %s during login",
                                    resp.status,
                                )
                                self._reauth_failure_count += 1
                                return ReauthStatus("failed")
                    except (aiohttp.ClientConnectorError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
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

                return ReauthStatus("failed")
            finally:
                # Stamp the cooldown timestamp when the attempt finishes
                self._last_reauth_attempt = dt_util.utcnow()

    def _store_renewed_cookie(self) -> None:
        """Save a cookie MyMeter renewed, so a restart starts from the current session."""
        cookie = self.client.cookie
        if not cookie or cookie == self.entry_data.get(CONF_COOKIE):
            return
        self.entry_data[CONF_COOKIE] = cookie
        if self.config_entry:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_COOKIE: cookie},
            )
        _LOGGER.debug("Stored renewed MyMeter cookie")

    async def async_keep_alive(self, timeout: float = 20) -> bool:
        """Make a request between scheduled updates so MyMeter keeps the session alive.

        Returns False only when the portal could not be reached, so the caller can retry soon.
        """
        # Re-authentication is already pending; pinging a dead session would only repeat it
        if self.auth_required or not self.client.cookie:
            return True
        # An update or backfill is talking to the portal, which keeps the session alive
        # on its own; waiting would only queue pings behind a long backfill
        if self._portal_lock.locked():
            _LOGGER.debug("Keep-alive: skipped, the PSE&G portal is already in use")
            return True
        expired: PsegAuthError | None = None
        async with self._portal_lock:
            try:
                await self.hass.async_add_executor_job(self.client.keep_alive, timeout)
            except PsegConnectionError as err:
                _LOGGER.debug("Keep-alive: PSE&G portal not reachable: %s", err)
                return False
            except PsegAuthError as err:
                if self.hass.is_stopping:
                    return True
                # No more pings until the session is renewed; the regular update path
                # re-authenticates via the add-on or asks the user to
                self.auth_required = True
                expired = err
            else:
                self._store_renewed_cookie()
                _LOGGER.debug("Keep-alive: MyMeter session still valid")
        if expired is not None:
            # Outside the lock: the refresh can run right away and takes the lock itself
            _LOGGER.info("Keep-alive: MyMeter session expired (%s), refreshing", expired)
            await self.async_request_refresh()
        return True

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from PSE&G."""
        async with self._portal_lock:
            return await self._async_fetch_and_record()

    async def _async_fetch_and_record(self) -> Dict[str, Any]:
        """Fetch the latest data and record statistics; the caller holds the portal lock."""
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
            return_records = data.get("return_hourly_records")
            if return_records:
                try:
                    await self.async_record_return_statistics(return_records, is_backfill=False)
                except Exception as stat_err:
                    _LOGGER.warning("Could not record return statistics into LTS: %s", stat_err)
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
            self._reauth_failure_count = 0
            self._dismiss_auth_issues()
            self._store_renewed_cookie()
            return data
        except PsegConnectionError as err:
            self.status = "error"
            _LOGGER.warning("Connection error communicating with PSE&G portal: %s", err)
            raise UpdateFailed(f"Connection error communicating with PSE&G portal: {err}") from err
        except PsegAuthError as err:
            self.auth_required = True
            self.status = "auth_required"
            _LOGGER.warning("Session error: %s. Attempting silent re-authentication...", err)
            reauth_result = await self.async_reauthenticate()
            if reauth_result:
                data = await self.hass.async_add_executor_job(
                    self.client.fetch_energy_data, 14
                )
                records = data.get("hourly_records") or data.get("history_14d")
                if records:
                    try:
                        await self.async_record_statistics(records, is_backfill=False)
                    except Exception as stat_err:
                        _LOGGER.warning("Could not record statistics into LTS: %s", stat_err)
                return_records = data.get("return_hourly_records")
                if return_records:
                    try:
                        await self.async_record_return_statistics(return_records, is_backfill=False)
                    except Exception as stat_err:
                        _LOGGER.warning("Could not record return statistics into LTS: %s", stat_err)
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
                self._reauth_failure_count = 0
                self._dismiss_auth_issues()
                return data

            if reauth_result == "skipped_cooldown":
                _LOGGER.info("PSE&G re-authentication cooldown active; pausing automated retry")
                raise UpdateFailed("PSE&G re-authentication cooldown active; temporary pause before retry") from err

            if reauth_result == "recaptcha_blocked":
                self.status = "error"
                raise UpdateFailed(
                    "PSE&G companion login temporarily blocked by Google reCAPTCHA; will retry automatically"
                ) from err

            if reauth_result == "companion_error":
                self.status = "error"
                raise UpdateFailed(
                    "PSE&G companion service returned a temporary error; will retry automatically"
                ) from err

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

    def _normalize_stat_records(
        self,
        records: list[dict[str, Any]],
        tz: Any,
    ) -> list[dict[str, Any]]:
        """Normalize raw usage records to sorted UTC start datetimes and floats."""
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

        return sorted(normalized_records, key=lambda x: x["start"])

    async def _apply_historical_rates_to_return_records(
        self,
        sorted_records: list[dict[str, Any]],
        tz: Any,
    ) -> None:
        """Align backfilled return compensation with each day's existing consumption rate."""
        consumption_id = f"{DOMAIN}:electricity_consumption"
        cost_id = f"{DOMAIN}:electricity_cost"
        query_start = datetime.min.replace(tzinfo=dt_util.UTC)
        try:
            existing = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                query_start,
                None,
                {consumption_id, cost_id},
                "hour",
                None,
                {"state"},
            )
            if not isinstance(existing, dict):
                return
        except Exception as err:
            _LOGGER.debug("Could not query historical rates for return backfill: %s", err)
            return

        daily_kwh: dict[date, float] = {}
        daily_cost: dict[date, float] = {}
        for stat_id, target in ((consumption_id, daily_kwh), (cost_id, daily_cost)):
            for row in existing.get(stat_id, []) or []:
                start = row.get("start")
                if isinstance(start, (int, float)):
                    start_dt = datetime.fromtimestamp(start, tz=dt_util.UTC)
                elif isinstance(start, datetime):
                    start_dt = dt_util.as_utc(start)
                else:
                    continue
                day = start_dt.astimezone(tz).date()
                target[day] = target.get(day, 0.0) + float(row.get("state") or 0.0)

        for record in sorted_records:
            day = record["start"].astimezone(tz).date()
            day_kwh = daily_kwh.get(day, 0.0)
            day_cost = daily_cost.get(day, 0.0)
            if day_kwh > 0 and day_cost > 0:
                record["cost"] = round(record["kwh"] * (day_cost / day_kwh), 4)

    async def async_record_return_statistics(
        self,
        records: list[dict[str, Any]],
        is_backfill: bool = False,
    ) -> int:
        """Import grid-return (solar export) energy and compensation statistics into LTS."""
        if not records:
            return 0

        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        sorted_records = self._normalize_stat_records(records, tz)
        if not sorted_records:
            return 0

        return_id = f"{DOMAIN}:electricity_return"
        comp_id = f"{DOMAIN}:electricity_return_compensation"
        return_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": "energy",
            "name": "PSE&G Electricity Returned",
            "source": DOMAIN,
            "statistic_id": return_id,
            "unit_of_measurement": "kWh",
        }
        comp_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": None,
            "name": "PSE&G Electricity Return Compensation",
            "source": DOMAIN,
            "statistic_id": comp_id,
            "unit_of_measurement": "USD",
        }

        if is_backfill:
            await self._apply_historical_rates_to_return_records(sorted_records, tz)
            return await self._async_record_backfill_series(
                sorted_records,
                tz,
                [(return_id, "kwh", return_meta), (comp_id, "cost", comp_meta)],
            )

        async def _query_last_stat(stat_id: str) -> tuple[Optional[datetime], float]:
            try:
                last_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    stat_id,
                    True,
                    {"sum", "start"},
                )
                if stat_id in last_stats and last_stats[stat_id]:
                    last_stat = last_stats[stat_id][0]
                    stat_start = last_stat.get("start")
                    start_dt: Optional[datetime] = None
                    if isinstance(stat_start, (int, float)):
                        start_dt = datetime.fromtimestamp(stat_start, tz=dt_util.UTC)
                    elif isinstance(stat_start, datetime):
                        start_dt = dt_util.as_utc(stat_start)
                    return start_dt, float(last_stat.get("sum") or 0.0)
            except Exception as err:
                _LOGGER.debug("Could not query last statistic for %s: %s", stat_id, err)
            return None, 0.0

        def _build_series_stats(
            last_start_dt: Optional[datetime],
            last_sum: float,
            value_key: str,
        ) -> tuple[list[StatisticData], int]:
            stats: list[StatisticData] = []
            if last_start_dt is None:
                first_start = sorted_records[0]["start"]
                step = timedelta(hours=1)
                if len(sorted_records) > 1 and (sorted_records[1]["start"] - sorted_records[0]["start"]) >= timedelta(days=1):
                    step = timedelta(days=1)
                baseline_dt = first_start - step
                stats.append(StatisticData(start=baseline_dt, state=0.0, sum=0.0))
                running = 0.0
                to_process = sorted_records
            else:
                to_process = [r for r in sorted_records if r["start"] > last_start_dt]
                running = last_sum

            if not to_process and len(stats) <= 1:
                return [], 0

            for r in to_process:
                running += r[value_key]
                stats.append(
                    StatisticData(start=r["start"], state=r[value_key], sum=round(running, 4))
                )
            return stats, len(to_process)

        inserted_max = 0
        for stat_id, value_key, meta in ((return_id, "kwh", return_meta), (comp_id, "cost", comp_meta)):
            last_start_dt, last_sum = await _query_last_stat(stat_id)
            stats, count = _build_series_stats(last_start_dt, last_sum, value_key)
            if stats:
                async_add_external_statistics(self.hass, meta, stats)
                _LOGGER.info(
                    "Added %d return statistics into LTS for %s (%s to %s)",
                    len(stats),
                    stat_id,
                    stats[0]["start"].isoformat(),
                    stats[-1]["start"].isoformat(),
                )
                inserted_max = max(inserted_max, count)
        return inserted_max

    async def async_record_statistics(
        self,
        records: list[dict[str, Any]],
        is_backfill: bool = False,
    ) -> int:
        """Import energy statistics and return the number of records inserted."""
        if not records:
            return 0

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
            return 0

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

        if is_backfill:
            return await self._async_record_backfill_statistics(
                sorted_records,
                tz,
                consumption_id,
                cost_id,
                consumption_meta,
                cost_meta,
            )

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
            return 0

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
        return len(records_to_process)

    async def _async_record_backfill_statistics(
        self,
        records: list[dict[str, Any]],
        tz: Any,
        consumption_id: str,
        cost_id: str,
        consumption_meta: StatisticMetaData,
        cost_meta: StatisticMetaData,
    ) -> int:
        """Insert missing daily rows, rechain sums, and return the inserted count."""
        return await self._async_record_backfill_series(
            records,
            tz,
            [
                (consumption_id, "kwh", consumption_meta),
                (cost_id, "cost", cost_meta),
            ],
        )

    async def _async_record_backfill_series(
        self,
        records: list[dict[str, Any]],
        tz: Any,
        series_specs: list[tuple[str, str, StatisticMetaData]],
    ) -> int:
        """Insert missing daily rows, rechain sums for one or more statistics, and return inserted count."""
        query_start = datetime.min.replace(tzinfo=dt_util.UTC)
        stat_ids = {stat_id for stat_id, _, _ in series_specs}

        try:
            existing = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                query_start,
                None,
                stat_ids,
                "hour",
                None,
                {"state", "sum"},
            )
            if not isinstance(existing, dict):
                raise TypeError("Recorder returned an invalid statistics result")

            existing_by_id: dict[str, list[dict[str, Any]]] = {}
            existing_days_by_id: dict[str, set[date]] = {}
            for statistic_id in stat_ids:
                rows = existing.get(statistic_id, [])
                if not isinstance(rows, list):
                    raise TypeError(f"Recorder returned invalid rows for {statistic_id}")
                normalized_rows = []
                stat_days: set[date] = set()
                for row in rows:
                    start = row["start"]
                    if isinstance(start, (int, float)):
                        start = datetime.fromtimestamp(start, tz=dt_util.UTC)
                    elif isinstance(start, datetime):
                        start = dt_util.as_utc(start)
                    else:
                        raise TypeError(f"Recorder returned an invalid start for {statistic_id}")
                    normalized_rows.append(
                        {
                            "start": start,
                            "state": float(row["state"]),
                            "sum": float(row["sum"]),
                        }
                    )
                    if float(row["state"]) != 0:
                        stat_days.add(start.astimezone(tz).date())
                existing_by_id[statistic_id] = normalized_rows
                existing_days_by_id[statistic_id] = stat_days
        except Exception as err:
            _LOGGER.error(
                "Could not read existing PSE&G statistics; backfill was not recorded: %s",
                err,
            )
            raise HomeAssistantError(
                "Could not read existing PSE&G statistics; backfill was not recorded."
            ) from err

        def rechain_for_stat(
            statistic_id: str,
            key: str,
            records_to_insert: list[dict[str, Any]],
        ) -> list[StatisticData]:
            """Keep existing interval amounts and rebuild sums after the insert point."""
            first_insert = records_to_insert[0]["start"]
            first_insert_day = first_insert.astimezone(tz).date()
            anchor_start = dt_util.as_utc(
                datetime.combine(
                    first_insert_day - timedelta(days=1),
                    time.min,
                    tzinfo=tz,
                )
            )
            rows = existing_by_id[statistic_id]
            prior_rows = [row for row in rows if row["start"] < first_insert]
            running = (
                max(prior_rows, key=lambda row: row["start"])["sum"]
                if prior_rows
                else 0.0
            )
            merged = {
                row["start"]: {"start": row["start"], "state": row["state"]}
                for row in rows
                if row["start"] >= first_insert
            }
            for record in records_to_insert:
                merged[record["start"]] = {
                    "start": record["start"],
                    "state": record[key],
                }

            output: list[StatisticData] = []
            if not prior_rows:
                merged[anchor_start] = {
                    "start": anchor_start,
                    "state": 0.0,
                }
            for start in sorted(merged):
                row = merged[start]
                running += row["state"]
                output.append(
                    StatisticData(
                        start=start,
                        state=row["state"],
                        sum=round(running, 4),
                    )
                )
            return output

        inserted_max = 0
        for stat_id, key, meta in series_specs:
            stat_existing_days = existing_days_by_id[stat_id]
            records_to_insert = [
                record
                for record in records
                if record["start"].astimezone(tz).date() not in stat_existing_days
            ]
            if not records_to_insert:
                _LOGGER.debug("Backfill contains no days without existing statistics for %s", stat_id)
                continue
            stats = rechain_for_stat(stat_id, key, records_to_insert)
            async_add_external_statistics(self.hass, meta, stats)
            inserted_max = max(inserted_max, len(records_to_insert))
        return inserted_max

    async def async_backfill_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Fetch and import historical data into Long-Term Statistics."""
        async with self._portal_lock:
            return await self._async_backfill_history(start_date, end_date)

    async def _async_backfill_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Run the backfill; the caller holds the portal lock."""
        now = dt_util.now()
        if not end_date:
            end_date_str = now.strftime("%Y-%m-%d")
        elif hasattr(end_date, "strftime"):
            end_date_str = end_date.strftime("%Y-%m-%d")
        else:
            end_date_str = str(end_date).strip()

        if not start_date:
            start_dt = now - timedelta(days=730)
            start_date_str = start_dt.strftime("%Y-%m-%d")
        elif hasattr(start_date, "strftime"):
            start_date_str = start_dt.strftime("%Y-%m-%d")
        else:
            start_date_str = str(start_date).strip()

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
            self.auth_required = True
            self.status = "auth_required"
            _LOGGER.warning(
                "Session expired during history backfill (%s). Attempting silent re-authentication...",
                auth_err,
            )
            reauth_res = await self.async_reauthenticate()
            if reauth_res:
                records = await self.hass.async_add_executor_job(
                    self.client.fetch_history_range,
                    start_date_str,
                    end_date_str,
                    90,
                )
            elif reauth_res == "skipped_cooldown":
                raise HomeAssistantError("PSE&G re-authentication cooldown active; please retry backfill in a moment") from auth_err
            elif reauth_res == "recaptcha_blocked":
                raise HomeAssistantError(
                    "PSE&G companion login temporarily blocked by Google reCAPTCHA; please retry backfill later"
                ) from auth_err
            elif reauth_res == "companion_error":
                raise HomeAssistantError(
                    "PSE&G companion service returned a temporary error; please retry backfill in a moment"
                ) from auth_err
            else:
                self.auth_required = True
                self.status = "auth_required"
                is_cookie_mode = (
                    self.entry_data.get(CONF_AUTH_TYPE) == AUTH_TYPE_COOKIE
                    or not self.entry_data.get(CONF_USERNAME)
                )
                addon_offline = not getattr(self, "_addon_connected", False) if not is_cookie_mode else False
                self._raise_auth_issues(str(auth_err), addon_offline=addon_offline)
                if self.config_entry:
                    self.config_entry.async_start_reauth(self.hass)
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

        inserted_count = await self.async_record_statistics(records, is_backfill=True)

        return_records = [
            {
                "date": r["date"],
                "start": r.get("start"),
                "kwh": r["return_kwh"],
                "cost": r.get("return_cost", 0.0),
            }
            for r in records
            if "return_kwh" in r
        ]
        if return_records:
            await self.async_record_return_statistics(return_records, is_backfill=True)

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

        _LOGGER.info("Completed PSE&G backfill: daily statistics imported: %d", inserted_count)
        return inserted_count
