"""Sensors for the PSE&G Energy integration."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DAYS_USED_FOR_RATE,
    ATTR_HISTORY_14D,
    ATTR_IS_EXACT_PORTAL_COST,
    ATTR_LATEST_COST,
    ATTR_LATEST_DATE,
    ATTR_METER,
    ATTR_PREVIOUS_COST,
    ATTR_PREVIOUS_DATE,
    ATTR_PREVIOUS_KWH,
    ATTR_RATE_7D_AVG,
    ATTR_RATE_APPLIED,
    ATTR_RATE_SOURCE,
    ATTR_TEMPERATURE_AVG,
    ATTR_TEMPERATURE_HIGH,
    ATTR_TEMPERATURE_LOW,
    DOMAIN,
)
from .coordinator import PsegDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PSE&G sensor entries."""
    coordinator: PsegDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Automated migration for entity unique IDs and entity IDs from legacy naming
    try:
        ent_reg = er.async_get(hass)
        migrations = [
            ("electricity_yesterday", "electricity_consumption", "sensor.pseg_electricity_consumption"),
            ("electricity_cost_yesterday", "electricity_cost", "sensor.pseg_electricity_cost"),
            ("outdoor_temperature_yesterday", "outdoor_temperature", "sensor.pseg_outdoor_temperature"),
            ("electricity_meter_id", "meter_id", "sensor.pseg_meter_id"),
            ("electricity_latest_date", "latest_reading_date", "sensor.pseg_latest_reading_date"),
        ]
        for old_suffix, new_suffix, target_entity_id in migrations:
            old_uid = f"{entry.entry_id}_{old_suffix}"
            new_uid = f"{entry.entry_id}_{new_suffix}"
            old_ent_id = ent_reg.async_get_entity_id("sensor", DOMAIN, old_uid)
            if old_ent_id:
                _LOGGER.info(
                    "Migrating entity %s (UID: %s) -> (UID: %s, Entity ID: %s)",
                    old_ent_id,
                    old_uid,
                    new_uid,
                    target_entity_id,
                )
                ent_reg.async_update_entity(
                    old_ent_id,
                    new_unique_id=new_uid,
                    new_entity_id=target_entity_id,
                )
    except Exception as err:
        _LOGGER.warning("Could not run entity registry migration: %s", err)

    entities = [
        PsegElectricityConsumptionSensor(coordinator, entry),
        PsegElectricityCostSensor(coordinator, entry),
        PsegElectricityMeterSensor(coordinator, entry),
        PsegElectricityLatestDateSensor(coordinator, entry),
        PsegTemperatureSensor(coordinator, entry),
        PsegNextUpdateSensor(coordinator, entry),
        PsegConnectionStatusSensor(coordinator, entry),
    ]

    async_add_entities(entities)


class PsegBaseSensor(CoordinatorEntity[PsegDataUpdateCoordinator], RestoreEntity, SensorEntity):
    """Base sensor for PSE&G integration with state restoration support."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PsegDataUpdateCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PSE&G",
            manufacturer="Public Service Enterprise Group",
            model="Smart AMI Meter",
        )
        self.entity_id = f"sensor.pseg_{key}"
        self._restored_state = None

    async def async_added_to_hass(self) -> None:
        """Handle entity added to Home Assistant."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            self._restored_state = last_state

    @property
    def available(self) -> bool:
        """Return True if data exists or has previously been loaded.
        Prevents full entity blackout across Home Assistant during session expiration or reauth.
        """
        return self._data is not None or self._restored_state is not None

    @property
    def _data(self) -> Optional[Dict[str, Any]]:
        """Return coordinator data, falling back to cached last valid data."""
        return self.coordinator.data or getattr(self.coordinator, "_last_valid_data", None)


class PsegConnectionStatusSensor(PsegBaseSensor):
    """Sensor for integration connection and authentication status."""

    _attr_name = "Connection Status"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "connection_status")

    @property
    def available(self) -> bool:
        """Status sensor is always available."""
        return True

    @property
    def native_value(self) -> str:
        """Return connection status: connected, auth_required, or error."""
        return self.coordinator.status

    @property
    def icon(self) -> str:
        if self.coordinator.status == "connected":
            return "mdi:check-circle"
        if self.coordinator.status == "auth_required":
            return "mdi:alert-circle"
        return "mdi:alert-octagon"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return {
            "auth_required": self.coordinator.auth_required,
            "last_successful_update": self.coordinator.last_successful_update,
            "statistics_import_status": self.coordinator.statistics_import_status,
            "last_statistics_error": self.coordinator.last_statistics_error,
        }


class PsegElectricityConsumptionSensor(PsegBaseSensor):
    """Sensor for electricity consumption in kWh."""

    _attr_name = "Electricity Consumption"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = None
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "electricity_consumption")

    @property
    def native_value(self) -> Optional[float]:
        """Return the consumption in kWh."""
        if self._data and "latest_kwh" in self._data:
            return self._data.get("latest_kwh")
        if self._restored_state:
            try:
                return float(self._restored_state.state)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return full history and context attributes for the custom card."""
        attrs = {}
        if self._restored_state and self._restored_state.attributes:
            attrs.update({
                k: v for k, v in self._restored_state.attributes.items()
                if k not in ("state_class", "device_class", "unit_of_measurement", "icon", "friendly_name")
            })
        if self._data:
            attrs.update({
                ATTR_METER: self._data.get("meter"),
                ATTR_LATEST_DATE: self._data.get("latest_date"),
                ATTR_PREVIOUS_DATE: self._data.get("previous_date"),
                ATTR_PREVIOUS_KWH: self._data.get("previous_kwh"),
                ATTR_PREVIOUS_COST: self._data.get("previous_cost"),
                ATTR_LATEST_COST: self._data.get("latest_cost"),
                ATTR_RATE_APPLIED: self.coordinator.effective_rate,
                ATTR_RATE_SOURCE: self.coordinator.rate_source,
                ATTR_RATE_7D_AVG: self.coordinator.rate_7d_avg,
                ATTR_DAYS_USED_FOR_RATE: self.coordinator.days_used_for_rate,
                ATTR_HISTORY_14D: self._data.get("history_14d", []),
                "consumption_data_available": self._data.get("consumption_data_available"),
                "hourly_record_count": self._data.get("hourly_record_count"),
                "chart_diagnostics": self._data.get("chart_diagnostics"),
                "last_updated": self._data.get("last_updated"),
                "temperature_range": self._data.get("temperature_range"),
                "next_update": self._data.get("next_update"),
            })
        attrs["auth_required"] = self.coordinator.auth_required
        attrs["connection_status"] = self.coordinator.status
        return attrs


class PsegElectricityCostSensor(PsegBaseSensor):
    """Sensor for electricity cost in USD."""

    _attr_name = "Electricity Cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = None
    _attr_native_unit_of_measurement = "USD"
    _attr_icon = "mdi:currency-usd"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "electricity_cost")

    @property
    def native_value(self) -> Optional[float]:
        """Return the cost in dollars."""
        if self._data and "latest_cost" in self._data:
            return self._data.get("latest_cost")
        if self._restored_state:
            try:
                return float(self._restored_state.state)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return extra state attributes."""
        attrs: Dict[str, Any] = {}
        if self._restored_state and self._restored_state.attributes:
            attrs.update({
                k: v for k, v in self._restored_state.attributes.items()
                if k not in ("state_class", "device_class", "unit_of_measurement", "icon", "friendly_name")
            })
        is_exact = False
        if self._data:
            is_exact = self._data.get("is_exact_latest_cost", False)
        attrs[ATTR_IS_EXACT_PORTAL_COST] = is_exact
        attrs["estimated"] = not is_exact
        attrs[ATTR_RATE_APPLIED] = self.coordinator.effective_rate
        attrs[ATTR_RATE_SOURCE] = self.coordinator.rate_source
        attrs[ATTR_RATE_7D_AVG] = self.coordinator.rate_7d_avg
        attrs[ATTR_DAYS_USED_FOR_RATE] = self.coordinator.days_used_for_rate
        return attrs


class PsegElectricityMeterSensor(PsegBaseSensor):
    """Sensor for the meter ID/description."""

    _attr_name = "Meter ID"
    _attr_icon = "mdi:meter-electric"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "meter_id")

    @property
    def native_value(self) -> Optional[str]:
        if self._data and "meter" in self._data:
            return self._data.get("meter")
        if self._restored_state:
            return self._restored_state.state
        return None


class PsegElectricityLatestDateSensor(PsegBaseSensor):
    """Sensor for the latest reading date."""

    _attr_name = "Latest Reading Date"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "latest_reading_date")

    @property
    def native_value(self) -> Optional[str]:
        if self._data and "latest_date" in self._data:
            return self._data.get("latest_date")
        if self._restored_state:
            return self._restored_state.state
        return None


class PsegTemperatureSensor(PsegBaseSensor):
    """Sensor for the outdoor average temperature."""

    _attr_name = "Outdoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "outdoor_temperature")

    @property
    def native_value(self) -> Optional[float]:
        if self._data and "temperature_avg" in self._data:
            return self._data.get("temperature_avg")
        if self._restored_state:
            try:
                return float(self._restored_state.state)
            except (ValueError, TypeError):
                pass
        return None


class PsegNextUpdateSensor(PsegBaseSensor):
    """Sensor for the next scheduled data update timestamp."""

    _attr_name = "Next Update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: PsegDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "next_update")

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the next scheduled update time as a timezone-aware datetime."""
        if self.coordinator.next_update_time:
            return self.coordinator.next_update_time
        if self._restored_state:
            try:
                return dt_util.parse_datetime(self._restored_state.state)
            except Exception:
                pass
        return None
