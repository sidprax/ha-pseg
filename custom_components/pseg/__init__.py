"""The PSE&G Energy integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.components.frontend import add_extra_js_url
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIE, DOMAIN
from .coordinator import PsegDataUpdateCoordinator
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

    async def async_backfill_service(call):
        """Handle backfill history service call."""
        start_date = call.data.get("start_date")
        end_date = call.data.get("end_date")

        if start_date and not is_valid_date_str(str(start_date)):
            raise HomeAssistantError(f"Invalid start_date '{start_date}'; expected YYYY-MM-DD")
        if end_date and not is_valid_date_str(str(end_date)):
            raise HomeAssistantError(f"Invalid end_date '{end_date}'; expected YYYY-MM-DD")

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

    hass.services.async_register(DOMAIN, "backfill_history", async_backfill_service)

    return True


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

    if not hass.services.has_service(DOMAIN, "backfill_history"):
        async def async_backfill_service(call):
            """Handle backfill history service call."""
            start_date = call.data.get("start_date")
            end_date = call.data.get("end_date")

            if start_date and not is_valid_date_str(str(start_date)):
                raise HomeAssistantError(f"Invalid start_date '{start_date}'; expected YYYY-MM-DD")
            if end_date and not is_valid_date_str(str(end_date)):
                raise HomeAssistantError(f"Invalid end_date '{end_date}'; expected YYYY-MM-DD")

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

        hass.services.async_register(DOMAIN, "backfill_history", async_backfill_service)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


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
