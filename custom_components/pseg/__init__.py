"""The PSE&G Energy integration."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.components.frontend import add_extra_js_url
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIE, DOMAIN, KEEPALIVE_RETRY_SECONDS
from .coordinator import (
    PsegDataUpdateCoordinator,
    keep_alive_enabled,
    next_keep_alive_delay,
)
from .pseg_client import PsegClient
from .util import is_valid_date_str

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up PSE&G integration wide configuration and register Lovelace and brand resources."""
    component_path = Path(__file__).parent
    frontend_path = component_path / "frontend" / "pseg-energy-card.js"
    icon_path = component_path / "icon.png"
    logo_path = component_path / "logo.png"

    def _get_static_paths() -> list[tuple[str, str]]:
        paths = []
        if frontend_path.exists():
            paths.append(("/pseg/pseg-energy-card.js", str(frontend_path)))
        if icon_path.exists():
            paths.append(("/pseg/icon.png", str(icon_path)))
        if logo_path.exists():
            paths.append(("/pseg/logo.png", str(logo_path)))
        return paths

    static_paths = await hass.async_add_executor_job(_get_static_paths)

    if static_paths:
        try:
            from homeassistant.components.http import StaticPathConfig
            configs = [StaticPathConfig(url, path, False) for url, path in static_paths]
            await hass.http.async_register_static_paths(configs)
            _LOGGER.debug("Registered static paths for PSE&G resources")
            if frontend_path.exists():
                add_extra_js_url(hass, "/pseg/pseg-energy-card.js")
                _LOGGER.debug("Registered Lovelace extra JS URL for pseg-energy-card")
        except Exception as err:
            _LOGGER.debug("Could not register static paths: %s", err)

    _async_register_backfill_service(hass)
    return True


async def _async_handle_backfill_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle backfill history service call with validated date inputs."""
    raw_start = call.data.get("start_date")
    raw_end = call.data.get("end_date")

    start_date = str(raw_start).strip() if raw_start is not None else None
    end_date = str(raw_end).strip() if raw_end is not None else None

    if start_date and not is_valid_date_str(start_date):
        raise HomeAssistantError(f"Invalid start_date '{raw_start}'; expected YYYY-MM-DD")
    if end_date and not is_valid_date_str(end_date):
        raise HomeAssistantError(f"Invalid end_date '{raw_end}'; expected YYYY-MM-DD")

    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        raise HomeAssistantError("No active PSE&G integration entry found for backfill")

    total_imported = 0
    for coord in coordinators:
        try:
            count = await coord.async_backfill_history(start_date=start_date, end_date=end_date)
            total_imported += count
            _LOGGER.info("PSE&G backfill service completed: %d records imported", count)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Error running PSE&G backfill service: %s", err)
            raise HomeAssistantError(f"PSE&G backfill failed: {err}") from err


def _async_register_backfill_service(hass: HomeAssistant) -> None:
    """Register the backfill_history service if not already registered."""
    if not hass.services.has_service(DOMAIN, "backfill_history"):
        async def _backfill(call: ServiceCall) -> None:
            await _async_handle_backfill_service(hass, call)

        hass.services.async_register(DOMAIN, "backfill_history", _backfill)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PSE&G from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    cookie = entry.data.get(CONF_COOKIE, "")
    client = PsegClient(cookie)
    coordinator = PsegDataUpdateCoordinator(hass, client, dict(entry.data), config_entry=entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Initial data refresh
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        coordinator.auth_required = True
        coordinator.status = "auth_required"
        raise

    _async_register_backfill_service(hass)

    if keep_alive_enabled(entry.data, entry.options):
        _async_start_keep_alive(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _async_start_keep_alive(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: PsegDataUpdateCoordinator
) -> None:
    """Keep the MyMeter session alive between the scheduled updates until the entry unloads."""
    state: dict[str, Any] = {"cancel": None, "retrying": False, "stopped": False}

    @callback
    def async_schedule(delay: float) -> None:
        state["cancel"] = async_call_later(hass, delay, async_keep_alive)

    async def async_keep_alive(now: datetime) -> None:
        reachable = await coordinator.async_keep_alive()
        if state["stopped"]:
            return
        # One missed ping already means about twice the usual gap without a request, close
        # to the idle timeout, so retry once soon instead of waiting for the next delay
        state["retrying"] = not reachable and not state["retrying"]
        async_schedule(KEEPALIVE_RETRY_SECONDS if state["retrying"] else next_keep_alive_delay())

    @callback
    def async_stop_keep_alive() -> None:
        state["stopped"] = True
        if state["cancel"]:
            state["cancel"]()

    async def async_keep_alive_on_stop(event: Event) -> None:
        """Touch the session right before shutdown, so a restart only has to beat the idle timeout."""
        # Short timeout so a slow portal can't hold up the shutdown
        await coordinator.async_keep_alive(timeout=10)

    async_schedule(next_keep_alive_delay())
    entry.async_on_unload(async_stop_keep_alive)
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_keep_alive_on_stop)
    )


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload PSE&G config entry only when options are updated."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator and coordinator.current_options == entry.options:
        return
    if coordinator:
        coordinator.current_options = dict(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PSE&G config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "backfill_history")

    return unload_ok
