"""DataUpdateCoordinator for the PSE&G Energy integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
import random
from typing import Any, Dict, Optional

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started
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
from .pseg_client import PORTAL_TIMEZONE, PsegAuthError, PsegClient, PsegConnectionError
from .statistics import reconcile_statistics
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
        self.current_options: dict[str, Any] = dict(config_entry.options) if config_entry else {}
        self.next_update_time: Optional[datetime] = None
        self._last_valid_data: Optional[Dict[str, Any]] = None
        self.auth_required: bool = False
        self.status: str = "connected"
        self.last_successful_update: Optional[str] = None
        self.statistics_import_status = "not_attempted"
        self._startup_import_scheduled = False
        self.last_statistics_error: str | None = None
        self._last_reauth_attempt: Optional[datetime] = None
        self._reauth_failure_count: int = 0
        self._reauth_lock = asyncio.Lock()
        # MyMeter keeps the chart range and interval in the session between the
        # POST /Dashboard/Chart setup and the GET /Dashboard/ChartData read, so only
        # one fetch (and its statistics import) may use the session at a time
        self._portal_lock = asyncio.Lock()
        self._statistics_lock = asyncio.Lock()

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
                                return ReauthStatus("failed")
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
            await self._async_import_poll_data(data)
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
                await self._async_import_poll_data(data)
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

    async def _async_import_poll_data(self, data: dict[str, Any]) -> None:
        """Report portal data availability separately from Recorder success."""
        records = data.get("hourly_records") or []
        data["consumption_data_available"] = bool(records)
        data["hourly_record_count"] = len(records)
        self.last_statistics_error = None
        if not records:
            self.statistics_import_status = "no_data"
            _LOGGER.warning(
                "Portal responded but no hourly consumption was parsed; chart diagnostics: %s",
                data.get("chart_diagnostics", {}),
            )
            return
        # Recorder processes its queue only after HA has fully started. The
        # first refresh is part of setup, so waiting here would prevent the
        # startup event that releases Recorder. is_running includes starting.
        if not self.hass.is_running or self.hass.state == CoreState.starting:
            self.statistics_import_status = "pending_startup"
            if not self._startup_import_scheduled:
                self._startup_import_scheduled = True

                async def refresh_after_start(hass: HomeAssistant) -> None:
                    self._startup_import_scheduled = False
                    await self.async_request_refresh()

                cancel = async_at_started(self.hass, refresh_after_start)
                if self.config_entry:
                    self.config_entry.async_on_unload(cancel)
            return
        try:
            await self.async_record_statistics(records)
        except Exception as err:
            self.statistics_import_status = "error"
            self.last_statistics_error = str(err)
            _LOGGER.warning("Could not record statistics into LTS: %s", err)
        else:
            self.statistics_import_status = "synchronized"

    async def async_record_statistics(
        self,
        records: list[dict[str, Any]],
        is_backfill: bool = False,
    ) -> None:
        """Serialize imports, including direct callers outside the portal lock."""
        async with self._statistics_lock:
            await self._async_record_statistics(records, is_backfill)

    async def _async_record_statistics(
        self, records: list[dict[str, Any]], is_backfill: bool = False,
    ) -> None:
        """Reconcile interval readings with committed external statistics."""
        if not records:
            return

        # Normalize records to UTC start datetime and float values
        normalized_records = []
        for r in records:
            start_dt = None
            if "start" in r and r["start"]:
                if isinstance(r["start"], datetime):
                    if r["start"].tzinfo is None or r["start"].utcoffset() is None:
                        raise ValueError("Statistics require timezone-aware timestamps")
                    start_dt = dt_util.as_utc(r["start"])
                elif isinstance(r["start"], (int, float)):
                    start_dt = datetime.fromtimestamp(r["start"], tz=dt_util.UTC)
            elif "date" in r and r["date"]:
                local_dt = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=PORTAL_TIMEZONE)
                start_dt = dt_util.as_utc(local_dt)

            if not start_dt:
                continue

            if start_dt.tzinfo is None or start_dt.utcoffset() is None:
                raise ValueError("Statistics require timezone-aware timestamps")
            if start_dt.minute or start_dt.second or start_dt.microsecond:
                raise ValueError("Statistics timestamps must be aligned to an hour")
            if any(isinstance(r.get(key), bool) or r.get(key) is None for key in ("kwh", "cost")):
                raise ValueError("Statistics require numeric consumption and cost")
            kwh = float(r["kwh"])
            cost = float(r["cost"])
            if not math.isfinite(kwh) or not math.isfinite(cost):
                raise ValueError("Statistics require finite consumption and cost")
            normalized_records.append({"start": start_dt, "kwh": kwh, "cost": cost})

        if not normalized_records:
            return

        # Retried or overlapping portal records must not be counted twice.
        by_start = {record["start"]: record for record in normalized_records}
        sorted_records = [by_start[start] for start in sorted(by_start)]

        # Daily totals do not contain hourly detail. Keep them in alternative
        # series so a backfill can never overwrite or double-count hourly rows.
        # Older releases mixed daily totals into the unsuffixed IDs and reset
        # cumulative sums between backfill chunks. Preserve that legacy history
        # and use explicit resolution IDs for all verified imports.
        suffix = "_daily" if is_backfill else "_hourly"
        consumption_id = f"{DOMAIN}:electricity_consumption{suffix}"
        cost_id = f"{DOMAIN}:electricity_cost{suffix}"

        consumption_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": "energy",
            "name": "PSE&G Electricity Consumption" + (" (Daily History)" if is_backfill else " (Hourly)"),
            "source": DOMAIN,
            "statistic_id": consumption_id,
            "unit_of_measurement": "kWh",
        }

        cost_meta: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": 0,
            "unit_class": None,
            "name": "PSE&G Electricity Cost" + (" (Daily History)" if is_backfill else " (Hourly)"),
            "source": DOMAIN,
            "statistic_id": cost_id,
            "unit_of_measurement": "USD",
        }

        recorder = get_instance(self.hass)
        try:
            # An earlier queued import must be committed before its values form
            # the next baseline. Reading in the executor alone is not a barrier.
            await recorder.async_block_till_done()
            existing = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(1970, 1, 1, tzinfo=dt_util.UTC),
                None,
                {consumption_id, cost_id},
                "hour",
                {"energy": "kWh"},
                {"state", "sum"},
            )
            # Validate and build both streams before queuing either write.
            consumption_stats = reconcile_statistics(
                existing.get(consumption_id, []), sorted_records, "kwh"
            )
            cost_stats = reconcile_statistics(
                existing.get(cost_id, []), sorted_records, "cost"
            )
        except Exception as err:
            raise HomeAssistantError(f"Could not reconcile statistics; import aborted: {err}") from err

        if consumption_stats:
            async_add_external_statistics(self.hass, consumption_meta, consumption_stats)
        if cost_stats:
            async_add_external_statistics(self.hass, cost_meta, cost_stats)
        if consumption_stats or cost_stats:
            await recorder.async_block_till_done()
            # A queue barrier is not a result from each import task. Verify the
            # committed suffix before reporting successful synchronization.
            written = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(row["start"] for row in consumption_stats + cost_stats),
                None,
                {consumption_id, cost_id},
                "hour",
                {"energy": "kWh"},
                {"state", "sum"},
            )
            for statistic_id, expected in ((consumption_id, consumption_stats), (cost_id, cost_stats)):
                actual = {
                    (row["start"].timestamp() if isinstance(row["start"], datetime) else row["start"]): row
                    for row in written.get(statistic_id, [])
                }
                for row in expected:
                    stored = actual.get(row["start"].timestamp())
                    if stored is None or any(
                        stored.get(key) is None or not math.isclose(
                            float(stored[key]), row[key], abs_tol=.00001, rel_tol=0,
                        ) for key in ("state", "sum")
                    ):
                        raise HomeAssistantError("Statistics import did not match committed Recorder data")
            _LOGGER.info(
                "Reconciled %d consumption and %d cost statistics (%s resolution)",
                len(consumption_stats), len(cost_stats), "daily" if is_backfill else "hourly",
            )

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
        now = dt_util.utcnow().astimezone(PORTAL_TIMEZONE)
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
            start_date_str = start_date.strftime("%Y-%m-%d")
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

        # Daily backfill is an alternative view. Do not relabel the latest
        # hourly poll's sensor model or its availability with daily-only data.

        _LOGGER.info("Completed PSE&G backfill: imported %d days of statistics", len(records))
        return len(records)
